"""查询理解: 把用户的自然语言问题变成"可检索的结构化查询"。

定位: P1-5（混合检索）的第一棒。**离线确定性优先**——分词用 jieba、过滤条件
用正则抽取、型号走别名表，全部可离线复现; LLM 只在"确定性结果不够"时补位
（调用方显式传入 ``llm`` 才启用，绝不主动触碰 LLM）。

产出契约（下游 :mod:`matrag.retrieve.routing` / :mod:`matrag.retrieve.retriever` 消费）::

    ParsedQuery(
        raw="PA601-D54A 表11 输出特性的最大输出功率是多少",
        keywords=["PA601", "D54A", "表", "11", "输出特性", "最大", "输出功率"],
        filters={"doc_id": "PA601-D54A", "table": 11},
        product_code="PA601-D54A",
    )

设计约束:

- **数据驱动、禁硬编码业务**: 型号只按"形状"识别（见 ``_MODEL_TOKEN_RE`` 的
  形状注释），规范化一律走 ``model_aliases.yaml`` 别名表; 章节名过滤走
  ``section_lexicon.json`` 章节词典（见 :mod:`matrag.retrieve.lexicon`）。
  代码里不出现任何具体型号/章节名/规格值字面量;
- **fail-fast，不静默吞错**: 别名表存在但结构非法 → 直接 ``ValueError``;
  分词器退化/LLM 补位失败 → 必须留痕（见 :func:`tokenizer_provenance` 与
  ``ParsedQuery.provenance``）;
- 不 import 公共契约模块 ``matrag/index/*``（P1-4 并行开发中）: 分词器采用
  "优先 ``KeywordIndex.tokenize``、取不到退化为内置 jieba"策略并留痕。
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from matrag.config import settings
from matrag.retrieve.lexicon import (
    SectionLexicon,
    load_lexicon,
    match_sections,
)

# ---- 路径约定（与 matrag.domain.cards 同风格: 显式参数 > 环境变量 > 仓库默认）----
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ALIAS_PATH = _REPO_ROOT / "data" / "domain" / "generated" / "model_aliases.yaml"
MODEL_ALIASES_PATH_ENV = "MATRAG_MODEL_ALIASES_PATH"

#: 章节词典文件名（落在 settings.store_dir，与 index.db 同目录，建索引时生成）
LEXICON_FILENAME = "section_lexicon.json"
#: 章节词典路径的环境变量覆盖（测试/排查用，语义同 MODEL_ALIASES_PATH_ENV）
LEXICON_PATH_ENV = "MATRAG_SECTION_LEXICON_PATH"

# ---- 过滤条件：查询侧键名（与 index where 键名的关系见 build_where）----
# 注意: P1-4 向量腿只支持"等值"过滤，关键词腿的白名单键是
# {doc_id, kind, table_id, section_path, caption, page}（其它键直接 ValueError）。
# 因此下面这些查询侧键**不都是**可直接透传的 where 键: ``doc_id`` 可直传;
# ``table`` 仅当用户给的已经是索引口径（``t<页>-<序号>``）时才转成 ``table_id``;
# ``section``（裸章节号）与 ``section_contains``（章节名）在等值口径下都表达不了，
# 由 ``build_where`` 统一转成 ``where["section_contains"]`` 的**包含**过滤
# （见 :mod:`matrag.retrieve.routing` / :mod:`matrag.retrieve.lexicon`）。
# 未表达的条件一律由 ``build_where`` 记录到 notes，绝不静默丢弃。
FILTER_DOC_ID = "doc_id"
#: 型号过滤键: doc_id 第一段（"PA601-D54A 定制电源技术规格书" -> "PA601-D54A"）。
#: doc_id 带后缀不能直接等值, 过滤一律走 product_code（两腿均已支持）。
FILTER_PRODUCT_CODE = "product_code"    # 型号 → 文档 id（两腿均可等值过滤）
FILTER_SECTION = "section"  # 章节号（查询侧键名，如 "4.3"）
#: 章节名过滤键（查询侧键名，如 "输入特性"）: 查询命中的规范化章节标题，
#: 数据来自 :mod:`matrag.retrieve.lexicon` 的章节词典。查询侧键名与 index
#: where 键名一致（"section_contains"），``build_where`` 原样透传。
FILTER_SECTION_CONTAINS = "section_contains"
FILTER_TABLE = "table"      # 表号（查询侧键名: 整数 11，或索引口径 "t0-6"）

# ---- 形状规则（业务无关: 只认"像型号"的 token，不认任何具体型号）----
# 形状 = 至少 2 个大写字母开头 + 至少 1 个数字 + 至少 1 个连字符段
# （如 字母2+/数字+/连字符段，形式 ``XX123-YY`` / ``XX123-VV-NN``）。
# 两个下限都是防误判: 单个大写字母 + 数字 + 连字符（``T731-2000`` 这类标准
# 编号）不得当成产品型号; 标准编号里的 ``GB4943.1`` 因不含连字符段也不会命中。
_MODEL_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9\-])([A-Z]{2,}[0-9][A-Z0-9]*(?:-[A-Z0-9]+)+)(?![A-Za-z0-9\-])")
# 次形状: 无连字符的"字母+数字" token（用户口语里的简称，如 ``XX123``）。
# 它单独**不构成**型号，只作为别名表的查表键: 命中别名表才算型号并生成
# doc_id 过滤，否则忽略——把"是不是产品编号"的判断交给数据而不是正则。
_MODEL_ALIAS_RE = re.compile(r"(?<![A-Za-z0-9\-])([A-Z]{2,}[0-9][A-Z0-9]*)(?![A-Za-z0-9\-])")
# 章节号: "4.3" / "4.3.2" / "第4.3节" / "4.3.4"; 前后不得再挨着字母数字，
# 否则会从 "GB4943.1" / "12.5V" 里抠出假章节号。首段限 1~2 位数字:
# 真实规格书章节号不会到三位，而标准编号（"GB/T 17626.2"）恰好是长数字段。
_SECTION_RE = re.compile(r"(?<![A-Za-z0-9.])([0-9]{1,2}(?:\.[0-9]+)+)(?![A-Za-z0-9.])")
# 表号: "表11" / "表 11" / "表t0-6"（后者即索引口径的 table_id）
_TABLE_RE = re.compile(r"表\s*([A-Za-z0-9][A-Za-z0-9-]*)")
# 索引口径表号形状（t<页>-<序号>，来自 P1-4 chunking 的 TableModel.id）
_TABLE_ID_SHAPE = re.compile(r"^t[0-9]+-[0-9]+$")
# 公开别名 (P1-I8): 同模块内 _TABLE_ID_SHAPE 仍可用; 跨模块 (routing 等) 走此别名。
TABLE_ID_SHAPE = _TABLE_ID_SHAPE

# 查询口语里的填充词: 对"去哪里找"没有贡献，进入关键词只会稀释 BM25。
STOPWORDS: frozenset[str] = frozenset(
    """
    的 了 和 与 及 或 在 有 无 是 否 为 被 把 将 对于 关于 根据 按照
    我 我们 你 您 他 她 它 咱 咱们 请 麻烦 帮 帮忙
    一下 一个 这个 那个 这些 那些 什么 怎么 怎样 如何 为什么 是多少 多少 几
    哪 哪些 哪个 哪里 哪儿 吗 呢 吧 啊 呀 哦 嗯 是否可以 能否 能不能
    想 想要 知道 告诉 说明 介绍 解释 查 查询 查看 查一下 找 寻找
    列 列出来 给出 给出列出 该 本 次 上述 相关 对应 具体 来说 而言 谢谢
    a an the of to in on for with is are be or and
    """.split()
)

# token 至少要含一个中日韩字符/字母/数字，纯标点（如分词剩下的 "-"）直接丢。
_TOKEN_HAS_CONTENT_RE = re.compile(r"[\w\u4e00-\u9fff]")


@dataclass
class ModelAlias:
    """一条型号别名记录（``model_aliases.yaml`` 的一行）。

    - ``alias``: 用户的可能写法（简称/口语/旧编号）;
    - ``product_code``: 规范化产品编号（与语料 ``doc_id`` 一致）;
    - ``note``: 可选备注（来源/人工复核记录）。
    """

    alias: str
    product_code: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化 dict。"""
        return {"alias": self.alias, "product_code": self.product_code, "note": self.note}


@dataclass
class ParsedQuery:
    """解析后的查询。

    - ``raw``: 原始查询文本（向量腿/引用回溯都用它，保证可解释）;
    - ``keywords``: jieba 分词去停用词后的词项（关键词腿用）;
    - ``filters``: 从查询里抽出的 where 条件（键名见 ``FILTER_*`` 常量）;
    - ``product_code``: 命中型号（别名表命中则填规范化编号，否则填形状识别
      出的候选——语料 ``doc_id`` 本身就是产品编号）;
    - ``provenance``: 溯源信息（分词器来源/别名表路径/LLM 补位内容），
      "退化必须留痕"的承载字段。
    """

    raw: str
    keywords: list[str] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
    product_code: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 分词器: 复用 P1-4 契约, 缺失时退化并留痕
# ---------------------------------------------------------------------------

_tokenizer: Callable[[str], list[str]] | None = None
_tokenizer_source: str = ""
_tokenizer_error: str | None = None


def _builtin_tokenize(text: str) -> list[str]:
    """内置分词: jieba 精确模式 + 停用词 + 纯标点剔除 + 去重保序。

    仅在 P1-4 的 ``KeywordIndex.tokenize`` 取不到时使用（退化路径）。
    """
    import jieba

    jieba.initialize()
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in jieba.cut(text):
        tok = raw.strip()
        if not _TOKEN_HAS_CONTENT_RE.search(tok):
            continue
        if tok in STOPWORDS or tok.lower() in STOPWORDS:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        tokens.append(tok)
    return tokens


def _resolve_tokenizer() -> Callable[[str], list[str]]:
    """解析分词器: 优先 ``matrag.index.keyword.KeywordIndex.tokenize``（P1-4 契约）。

    P1-4 与 P1-5 并行开发，该模块可能尚未落盘; 取不到时退化为内置 jieba 实现。
    **退化不是静默的**: :func:`tokenizer_provenance` 随时可查来源与原因。
    """
    global _tokenizer, _tokenizer_source, _tokenizer_error
    if _tokenizer is not None:
        return _tokenizer
    try:
        from matrag.index.keyword import KeywordIndex

        _tokenizer = KeywordIndex.tokenize
        _tokenizer_source = "keyword_index"
        _tokenizer_error = None
    except Exception as exc:  # noqa: BLE001 —— 退化原因必须原样留痕, 因此记录而非抛出
        _tokenizer = _builtin_tokenize
        _tokenizer_source = "builtin_jieba"
        _tokenizer_error = f"{type(exc).__name__}: {exc}"
    return _tokenizer


def reset_tokenizer_cache() -> None:
    """清空分词器解析缓存（测试/热替换用; 生产不需要）。"""
    global _tokenizer, _tokenizer_source, _tokenizer_error
    _tokenizer, _tokenizer_source, _tokenizer_error = None, "", None


# 公开别名 (P1-I8): 同模块内 _resolve_tokenizer 仍可用; 跨模块 (rule_hint 等) 走此别名。
resolve_tokenizer = _resolve_tokenizer


def tokenizer_provenance() -> dict[str, Any]:
    """分词器溯源: 当前来源（``keyword_index`` / ``builtin_jieba``）与退化原因。"""
    _resolve_tokenizer()
    return {"source": _tokenizer_source, "fallback_reason": _tokenizer_error}


# ---------------------------------------------------------------------------
# 章节词典（按"章节名"过滤检索的数据来源）
# ---------------------------------------------------------------------------


def lexicon_path(path: Path | str | None = None) -> Path:
    """解析章节词典路径: 显式参数 > 环境变量 ``MATRAG_SECTION_LEXICON_PATH`` > 仓库默认。

    默认落在 ``settings.store_dir / "section_lexicon.json"``，与 ``index.db``
    同目录（建索引时由 ``scripts/build_index.py`` 生成）。
    """
    if path is not None:
        return Path(path)
    override = os.environ.get(LEXICON_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return settings.store_dir / LEXICON_FILENAME


# 模块级词典缓存（按路径缓存: 不同路径互不污染，热替换测试可靠）。
_LEXICON_CACHE: dict[Path, SectionLexicon] = {}


def _lexicon_cached(path: Path) -> SectionLexicon:
    """按路径惰性加载并缓存章节词典（词典不变，无需每次读盘）。"""
    key = Path(path)
    lex = _LEXICON_CACHE.get(key)
    if lex is None:
        lex = load_lexicon(key)
        _LEXICON_CACHE[key] = lex
    return lex


def section_title_hits(query: str, *, path: Path | str | None = None) -> list[str]:
    """查询命中的章节标题（最长优先去重叠）。

    词典文件不存在 → :func:`load_lexicon` 返回空词典 → 空列表（"没有词典"
    是正常状态，不是错误; 因此不产生过滤条件，也不留痕）。
    """
    return match_sections(query, _lexicon_cached(lexicon_path(path)))


def reset_lexicon_cache() -> None:
    """清空章节词典缓存（测试/热替换用; 生产不需要）。"""
    _LEXICON_CACHE.clear()


# ---------------------------------------------------------------------------
# 型号别名表
# ---------------------------------------------------------------------------


def resolve_alias_path(path: Path | str | None = None) -> Path:
    """解析别名表路径: 显式参数 > 环境变量 ``MATRAG_MODEL_ALIASES_PATH`` > 仓库默认。"""
    if path is not None:
        return Path(path)
    override = os.environ.get(MODEL_ALIASES_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return DEFAULT_ALIAS_PATH


def load_model_aliases(path: Path | str | None = None) -> dict[str, str]:
    """加载型号别名表 → ``{别名: product_code}``。

    文件缺失返回 ``{}``（未配置别名的正常状态，不是错误）; 文件存在但结构非法
    则 ``ValueError`` fail-fast——错误数据比没有数据更危险。

    支持两种形状（数据驱动，不假定生成方怎么写）::

        aliases:                                          # 顶层映射式
          PA601: PA601-D54A
        - {alias: PA601, product_code: PA601-D54A, note: 简称}   # 记录列表式
    """
    file = resolve_alias_path(path)
    if not file.is_file():
        return {}
    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    if data is None:
        return {}
    entries: list[Any]
    mapping = data.get("aliases") if isinstance(data, dict) else data
    if isinstance(mapping, dict):
        entries = [{"alias": k, "product_code": v} for k, v in mapping.items()]
    elif isinstance(mapping, list):
        entries = mapping
    else:
        raise ValueError(f"别名表结构非法（应为 list 或 aliases 映射）: {file}")

    aliases: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"别名表存在非映射记录: {entry!r} ({file})")
        if "alias" not in entry or "product_code" not in entry:
            raise ValueError(f"别名记录缺少 alias/product_code 字段: {entry!r} ({file})")
        alias, code = str(entry["alias"]).strip(), str(entry["product_code"]).strip()
        if not alias or not code:
            raise ValueError(f"别名记录字段为空: {entry!r} ({file})")
        if alias in aliases and aliases[alias] != code:
            raise ValueError(f"别名 {alias!r} 映射冲突: {aliases[alias]!r} vs {code!r} ({file})")
        aliases[alias] = code
    return aliases


def load_model_alias_records(path: Path | str | None = None) -> list[ModelAlias]:
    """加载结构化别名记录（调试/CLI 打印用; 校验与 :func:`load_model_aliases` 同源）。"""
    return [
        ModelAlias(alias=alias, product_code=code)
        for alias, code in load_model_aliases(path).items()
    ]


# ---------------------------------------------------------------------------
# 过滤条件抽取
# ---------------------------------------------------------------------------


def _extract_filters(query: str) -> tuple[dict[str, Any], str | None]:
    """从查询文本抽取 ``(filters, product_code)``。

    - 型号: 形状规则识别候选 token，再经别名表规范化（命中别名表则填规范化
      产品编号）;
    - 章节号: ``4.3`` / ``4.3.2`` / ``第4.3节`` / ``4.3.4``;
    - 章节名: 命中章节词典（``section_lexicon.json``）的规范化标题，如
      "输入特性" → ``filters["section_contains"]``（:func:`section_title_hits`，
      多个命中取最长者; 词典缺失 → 不加此条件）;
    - 表号: ``表11`` / ``表 11``（整数），或索引口径的 ``表t0-6``（保持字符串，
      :func:`matrag.retrieve.routing.build_where` 会把它转成 ``table_id``）。

    纯数据驱动（正则 + 别名表 + 章节词典），无任何按具体型号/章节名/规格值
    写死的分支。
    """
    filters: dict[str, Any] = {}
    product_code: str | None = None

    aliases = load_model_aliases()
    model = _MODEL_TOKEN_RE.search(query)
    if model:
        candidate = model.group(1)
        product_code = aliases.get(candidate, candidate)
        filters[FILTER_PRODUCT_CODE] = product_code
    else:
        # 无连字符的简称只在别名表里查得到才算型号（数据驱动防误判）
        alias_hit = _MODEL_ALIAS_RE.search(query)
        if alias_hit and alias_hit.group(1) in aliases:
            product_code = aliases[alias_hit.group(1)]
            filters[FILTER_PRODUCT_CODE] = product_code

    section = _SECTION_RE.search(query)
    if section:
        filters[FILTER_SECTION] = section.group(1)

    # 章节名（"输入特性 下的参数" → "输入特性"）: 命中词典才过滤, 多个命中取最长
    section_titles = section_title_hits(query)
    if section_titles:
        filters[FILTER_SECTION_CONTAINS] = section_titles[0]

    table = _TABLE_RE.search(query)
    if table:
        token = table.group(1)
        filters[FILTER_TABLE] = token if _TABLE_ID_SHAPE.match(token) else (
            int(token) if token.isdigit() else token
        )

    return filters, product_code


# ---------------------------------------------------------------------------
# LLM 补位（可选增强, 默认关）
# ---------------------------------------------------------------------------


def _llm_complete_json(llm: Any, system: str, user: str, schema_hint: str) -> dict[str, Any]:
    """取 LLM 的 JSON 输出。鸭子类型: 兼容 ``complete_json(system, user, schema_hint=...)``
    与只收两个位置参数的替身（对齐 ``matrag.llm.LLMClient.complete_json``）。"""
    try:
        return llm.complete_json(system, user, schema_hint=schema_hint)
    except TypeError:  # 替身签名更窄（测试用）
        return llm.complete_json(system, user)


def _llm_fill(query: str, parsed: ParsedQuery, llm: Any, notes: dict[str, Any]) -> None:
    """LLM 补位: 只填确定性路径空着的字段（keywords/filters/product_code），不改写
    已由正则/分词确定的结果; 调用失败留痕后继续用确定性结果。

    Args:
        query: 原始查询。
        parsed: 待补位的解析结果（原地修改）。
        llm: 显式传入的 LLM 门面。
        notes: provenance 记录（原址写入 ``llm_status``）。
    """
    try:
        data = _llm_complete_json(
            llm,
            system="你是电源规格书检索系统的查询理解器。只从问题中抽取检索线索，只输出 JSON，不要解释。",
            user=f"问题: {query}",
            schema_hint=(
                '{"keywords": ["最多8个中文/英文检索词"], '
                '"product_code": "提到的产品编号，没有则 null", '
                '"section": "章节号如4.3，没有则 null", '
                '"table": "表号整数，没有则 null"}'
            ),
        )
    except Exception as exc:  # noqa: BLE001 —— LLM 是可选增强, 失败留痕而非让检索崩掉
        notes["llm_status"] = {"used": False, "error": f"{type(exc).__name__}: {exc}"}
        return

    if not isinstance(data, dict):
        notes["llm_status"] = {"used": False, "error": "LLM 未返回 JSON 对象"}
        return

    filled: dict[str, Any] = {}
    for kw in (data.get("keywords") or []):
        kw = str(kw).strip()
        if kw and kw not in parsed.keywords and kw not in STOPWORDS:
            parsed.keywords.append(kw)
            filled.setdefault("keywords", []).append(kw)

    if parsed.product_code is None and data.get("product_code"):
        parsed.product_code = str(data["product_code"]).strip() or None
        if parsed.product_code:
            filled["product_code"] = parsed.product_code
            parsed.filters.setdefault(FILTER_PRODUCT_CODE, parsed.product_code)
    # 注意: FILTER_SECTION_CONTAINS（章节名）只由确定性路径（章节词典）填，LLM 不补位——
    # 章节名过滤是 LIKE 包含匹配，编造一个不存在的标题会让召回直接归零（比不过滤更糟），
    # 因此这里只"不覆盖"已由词典确定的结果（FILTER_SECTION/表号同理）。
    if FILTER_SECTION not in parsed.filters and data.get("section"):
        parsed.filters[FILTER_SECTION] = str(data["section"]).strip()
        filled["section"] = parsed.filters[FILTER_SECTION]
    if FILTER_TABLE not in parsed.filters and data.get("table") is not None:
        try:
            parsed.filters[FILTER_TABLE] = int(data["table"])
            filled["table"] = parsed.filters[FILTER_TABLE]
        except (TypeError, ValueError):
            notes.setdefault("llm_dropped", []).append({"table": data.get("table")})
    notes["llm_status"] = {"used": True, "filled": filled}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def parse_query(query: str, *, llm: Any = None) -> ParsedQuery:
    """解析查询: 分词 + 抽取过滤条件（+ 可选 LLM 补位）。

    Args:
        query: 用户原始问题。
        llm: 可选 LLM 门面（``matrag.llm.LLMClient`` 或鸭子类型替身）。
            **默认 ``None``——绝不主动触碰 LLM**; 传入后只在确定性结果缺字段
            时补位，失败留痕不抛。

    Returns:
        :class:`ParsedQuery`。``llm=None`` 时结果完全离线确定性。
    """
    text = (query or "").strip()
    filters, product_code = _extract_filters(text)
    parsed = ParsedQuery(
        raw=text,
        keywords=_resolve_tokenizer()(text),
        filters=filters,
        product_code=product_code,
    )

    provenance: dict[str, Any] = {
        "tokenizer": tokenizer_provenance(),
        "alias_path": str(resolve_alias_path()),
        "alias_hit": product_code is not None,
        # 章节词典: 路径 + 本次命中的全部标题（最长在前），未命中/无词典记空列表
        "lexicon_path": str(lexicon_path()),
        "section_title_hits": section_title_hits(text),
    }
    if llm is not None:
        _llm_fill(text, parsed, llm, provenance)
    parsed.provenance = provenance
    return parsed
