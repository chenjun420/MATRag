"""LLM 驱动的语义查询理解: 在正则抽取之上做"有据可依"的语义推理, 产出检索计划。

定位: 让系统从"关键词匹配"升级到"理解用户到底想查什么"——判定意图、抽出实体、
改写查询、多跳拆解子问题、提示是否需要确定性规则参与。但**不放弃确定性底线**:
正则抽到的精确引用（型号/章节号/章节名/表号）永远优先, LLM 只能补充, 不能覆盖。

流程::

    parse_query(正则层) ──► _regex_plan(确定性基线)
                            │
                            ├─ llm is None / 未配置 ─► 基线 + degradations[llm_unavailable]
                            │
                            └─ 有 llm ─► complete_json(接地提示) ──► _plan_from_llm(语义字段并入)
                                          │
                                          ├─ 失败/非 JSON ─► 基线 + degradations[llm_failed]
                                          │
                                          └─ 成功 ─► merge_regex(正则优先) ──► QueryPlan

四条硬底线:

1. **接地（grounding）**: 提示词里只给 LLM 三样东西——用户原查询、章节词表
   （来自 :mod:`matrag.retrieve.lexicon` 的 ``section_lexicon.json``, 不是模型
   的记忆）、正则层已抽到的结构化线索。提示词显式要求"章节名只能从词表里选"。
2. **正则优先**: ``product_code`` / ``section`` / ``section_contains`` / ``table``
   是从用户原文里**精确引用**出来的, 语义模型猜错一次就是零召回; 因此 LLM 提供
   的同名字段一律忽略并留痕（见 :func:`merge_regex`）。
3. **不静默降级**: LLM 不可用 / 超时 / 输出不可解析 / 字段被丢弃, 全部写进
   ``QueryPlan.degradations``; ``reasoning`` 在没做语义推理时明说"未做语义推理"。
4. **数据驱动, 禁硬编码业务清单**: 章节名来自词典, 规则域词来自
   :data:`RULE_HINT_KEYWORDS` 常量表, 不含任何具体型号/章节/规则 id 分支。

本模块不 import 公共契约模块, 不改 :mod:`matrag.retrieve.query` /
:mod:`matrag.retrieve.routing` / :mod:`matrag.retrieve.retriever` 的行为, 只是
在它们之上加一层语义规划。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from matrag.retrieve.catalog import ProductCatalog, load_catalog
from matrag.retrieve.lexicon import SectionLexicon, match_sections, normalize_title
from matrag.retrieve.query import (
    FILTER_DOC_ID,
    FILTER_PRODUCT_CODE,
    FILTER_SECTION,
    FILTER_SECTION_CONTAINS,
    FILTER_TABLE,
    STOPWORDS,
    ParsedQuery,
    _lexicon_cached,
    parse_query,
)
from matrag.retrieve.query import (
    lexicon_path as resolve_lexicon_path,
)
from matrag.retrieve.routing import QueryKind, RoutedQuery, route

# ---------------------------------------------------------------------------
# 溯源标签（provenance 的取值域）
# ---------------------------------------------------------------------------
PROV_REGEX = "regex"      # 正则/词典确定性抽取
PROV_LLM = "llm"          # LLM 语义推理产出
PROV_MERGED = "merged"    # 两侧一致或确实融合

#: ``QueryPlan.kind`` 允许的取值: 路由层的 6 个 kind + "mixed"（多意图混合）。
KIND_MIXED = "mixed"
_VALID_KINDS: frozenset[str] = frozenset(k.value for k in QueryKind) | {KIND_MIXED}

#: ``QueryPlan.entities`` 的实体槽位（固定四类, 保证下游取值形状稳定）。
ENTITY_KEYS: tuple[str, ...] = ("parameter", "component", "standard", "phenomenon")

#: 精确引用键: 正则抽到即定, LLM **不得覆盖**（覆盖会直接归零召回）。
PRECISION_FILTER_KEYS: frozenset[str] = frozenset(
    {FILTER_DOC_ID, FILTER_PRODUCT_CODE, FILTER_SECTION, FILTER_SECTION_CONTAINS, FILTER_TABLE}
)

#: 需要确定性规则（域知识规则卡/计算式）参与的领域词表: 命中即置 ``needs_rules``。
#: 故意只写"领域词", 不写任何规则 id/公式分支——规则本身在 P1-6 域知识层,
#: 本层只负责"这类问题别只靠向量/关键词硬答"。
RULE_HINT_KEYWORDS: frozenset[str] = frozenset(
    """
    效率 纹波 保持时间 浪涌 回差 门限 精度 降额
    保护 过压 欠压 过流 短路 过温 过热 环路 动态响应 转换效率
    上升时间 下降时间 建立时间 恢复时间 纹波系数 抑制比 共模 差模
    空载 满载 负载率 软启动 启动时间 充电 放电 电池 均衡 效率曲线 降额曲线
    """.split()
)

# ---- 容量上限（防止 LLM 输出把检索撑爆, 也保证 prompt 稳定可控）----
#: 提示词里最多给 LLM 的章节词表条数。
MAX_LEXICON_TITLES = 60
#: 改写变体最多几条（``retrieval_queries`` 除原查询外的上限）。
MAX_REWRITES = 3
#: 多跳拆解出的子问题最多几条。
MAX_SUB_QUERIES = 4
#: 关键词最多几条（含正则分词结果）。
MAX_KEYWORDS = 16
#: 单个实体槽位最多几条。
MAX_ENTITY_ITEMS = 6
#: ``reasoning`` 最多保留多少字符。
MAX_REASONING = 600
#: 降级记录 ``detail`` 的截断长度（原样截断, 便于排查又不撑爆日志）。
MAX_DETAIL = 300
#: 无 LLM 时的置信度基线与上限: 只做了正则抽取, 不做语义推理 → 封顶 0.6。
REGEX_CONF_BASE = 0.30
REGEX_CONF_CAP = 0.60

#: provenance 里逐字段记录的字段名（``filters`` 另按 ``filters.<键>`` 细记）。
_PLAN_FIELDS: tuple[str, ...] = (
    "intent",
    "kind",
    "filters",
    "entities",
    "keywords",
    "retrieval_queries",
    "sub_queries",
    "reasoning",
    "needs_rules",
    "confidence",
)

#: ``QueryKind`` → 中文标签（只用于无 LLM 时合成 intent 句式, 不参与判定）。
_KIND_LABELS: dict[str, str] = {
    QueryKind.SPEC_VALUE.value: "规格值/限值",
    QueryKind.PROCEDURE.value: "测试方法/操作步骤",
    QueryKind.FORMULA.value: "公式/换算",
    QueryKind.FAULT.value: "故障现象/原因",
    QueryKind.CATALOG.value: "型号清单/目录",
    QueryKind.UNKNOWN.value: "未识别意图",
}


# ---------------------------------------------------------------------------
# 提示词（接地: 词表 + 正则线索, 不允许模型凭记忆编章节名）
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "你是电子产测规格书检索规划器（不是问答助手）。"
    "你的任务是把用户的自然语言问题变成可供向量/关键词检索执行的查询计划，"
    "而不是回答问题本身。硬性约束：\n"
    "1. 章节名只能原样取自【可用的章节词表】；没有词表依据时不要输出任何章节名。\n"
    "2. 实体（参数/器件/标准/现象）只在用户问题里明确出现或其直接指代时才输出，"
    "禁止编造型号、章节号、表号、标准号与规格值。\n"
    "3. 型号、章节号、表号已由确定性正则层抽出（见【正则层线索】），"
    "不得改写、不得补充、不得与之冲突。\n"
    "4. 只输出 JSON 对象，不要解释，不要 Markdown 代码块。"
)

_SCHEMA_HINT = (
    '{"intent": "一句话说明用户想查什么（不超过40字）",'
    ' "kind": "spec_value|procedure|formula|fault|catalog|mixed|unknown",'
    ' "entities": {"parameter": [], "component": [], "standard": [], "phenomenon": []},'
    ' "keywords": ["检索关键词"],'
    ' "rewrite_queries": ["与原查询同义的检索改写，1~3条"],'
    ' "sub_queries": ["多跳问题拆出的子问题，2~4条；单跳问题给空数组"],'
    ' "needs_rules": true,'
    ' "reasoning": "为什么这么理解、打算检索什么（一到两句）",'
    ' "confidence": 0.0,'
    ' "filters": {"product_code": "仅正则线索缺失时才填，否则省略",'
    ' "section": "章节号，仅缺失时填", "section_contains": "章节名，必须来自章节词表",'
    ' "table": "表号，仅缺失时填"}}'
)


def _build_user_prompt(query: str, parsed: ParsedQuery, titles: list[str]) -> str:
    """拼 user 提示: 原查询 + 正则层线索 + 可用章节词表。

    Args:
        query: 用户原始问题。
        parsed: 正则层解析结果（其 filters/keywords 即"已确定的线索"）。
        titles: 可用章节词表（已按"命中优先 + 确定性排序 + 截断"处理）。

    Returns:
        user 提示全文（接地三要素俱全, 供 StubLLM 断言）。
    """
    lines = [f"用户查询: {query}", "", "【正则层线索（已确定，不得改写）】"]
    clue_keys = [FILTER_PRODUCT_CODE, FILTER_SECTION, FILTER_SECTION_CONTAINS, FILTER_TABLE]
    clue_lines = [f"- {key}: {parsed.filters[key]}" for key in clue_keys if key in parsed.filters]
    if not clue_lines and parsed.product_code:
        clue_lines.append(f"- {FILTER_PRODUCT_CODE}: {parsed.product_code}")
    lines.extend(clue_lines or ["- （无精确引用，型号/章节号/章节名/表号都没抽到）"])
    lines.append(f"- keywords: {'、'.join(parsed.keywords) if parsed.keywords else '（无）'}")

    lines.append("")
    lines.append(f"【可用的章节词表（共 {len(titles)} 条，只能从中选章节名）】")
    if titles:
        lines.extend(f"{i}. {title}" for i, title in enumerate(titles, start=1))
    else:
        lines.append("（没有章节词表：不要输出任何章节名）")
    lines.append("")
    lines.append("请输出 JSON。")
    return "\n".join(lines)


def _lexicon_titles(query: str, lex: SectionLexicon, limit: int = MAX_LEXICON_TITLES) -> list[str]:
    """给提示词用的章节词表: 正则命中优先, 其余按字典序, 截断到 ``limit``。

    "命中优先"保证正则已确认的章节名一定出现在提示词里（接地闭环）;
    "字典序"保证同一份词典每次拼出同样的提示词（可测、可复现）。
    """
    hits = match_sections(query, lex)
    ordered = list(dict.fromkeys([*hits, *sorted(lex.titles)]))
    return ordered[:limit]


# ---------------------------------------------------------------------------
# 计划对象
# ---------------------------------------------------------------------------


@dataclass
class QueryPlan:
    """一次查询的完整执行计划（语义层产出, 检索层的唯一输入契约）。

    - ``raw``: 用户原查询（``retrieval_queries[0]``, 保证可解释可回溯）;
    - ``kind``: 问题类型, 取值 = :class:`matrag.retrieve.routing.QueryKind` 的 value
      或 ``"mixed"`` / ``"unknown"``;
    - ``intent``: 一句话意图（无 LLM 时由正则线索 + 原文合成）;
    - ``filters``: 最终 where 之前的过滤条件, 键名沿用
      :mod:`matrag.retrieve.query` 的 ``FILTER_*`` 常量;
    - ``entities``: 语义实体槽位（参数/器件/标准/现象）, 可全空;
    - ``keywords``: 检索关键词（正则分词 + LLM 补充, 去重保序）;
    - ``retrieval_queries``: 检索用查询串, **index 0 必为原查询**, 其后是 LLM 改写变体;
    - ``sub_queries``: 多跳问题的子问题拆解, 单跳为空;
    - ``reasoning``: "为什么这么理解、要检索什么"的简述;
    - ``needs_rules``: 是否需要确定性规则参与（效率/纹波/保持时间/保护门限类）;
    - ``provenance``: 字段级来源（``regex`` / ``llm`` / ``merged``）; 过滤条件额外按
      ``filters.<键>`` 细记, 便于回答"这个条件是谁给的";
    - ``degradations``: 降级留痕（LLM 不可用/失败/字段被丢弃）, **禁止静默**;
    - ``confidence``: 整体置信度（0~1）。
    """

    raw: str
    kind: str = QueryKind.UNKNOWN.value
    intent: str = ""
    filters: dict[str, Any] = field(default_factory=dict)
    entities: dict[str, list[str]] = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)
    retrieval_queries: list[str] = field(default_factory=list)
    sub_queries: list[str] = field(default_factory=list)
    reasoning: str = ""
    needs_rules: bool = False
    provenance: dict[str, str] = field(default_factory=dict)
    degradations: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """导出为 JSON 可序列化 dict（CLI 报告 / 日志 / 网关透出用）。"""
        return {
            "raw": self.raw,
            "kind": self.kind,
            "intent": self.intent,
            "filters": dict(self.filters),
            "entities": {key: list(value) for key, value in self.entities.items()},
            "keywords": list(self.keywords),
            "retrieval_queries": list(self.retrieval_queries),
            "sub_queries": list(self.sub_queries),
            "reasoning": self.reasoning,
            "needs_rules": self.needs_rules,
            "provenance": dict(self.provenance),
            "degradations": [dict(item) for item in self.degradations],
            "confidence": self.confidence,
        }


# ---------------------------------------------------------------------------
# 小工具（清洗 / 归一, 全部对脏输入"丢弃 + 留痕", 不猜不补）
# ---------------------------------------------------------------------------


def _clean_text(value: Any, limit: int = MAX_REASONING) -> str:
    """把 LLM 的任意标量转成去空白文本; 非标量/空 → ``""``。"""
    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return ""
    text = " ".join(str(value).split())
    return text[:limit]


def _string_list(value: Any, cap: int) -> list[str]:
    """把 LLM 的数组字段转成"去空白 + 去重保序 + 截断"的字符串列表。

    非 list（字符串/数字/对象）一律当无效 → 返回空列表, 由调用方留痕。
    """
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = " ".join(str(item).split()) if not isinstance(item, (dict, list, tuple)) else ""
        if not text or text in out:
            continue
        out.append(text)
        if len(out) >= cap:
            break
    return out


def _coerce_confidence(value: Any) -> float | None:
    """置信度归一到 0~1; 无法解析 → ``None``（调用方保留正则基线）。"""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return round(min(max(number, 0.0), 1.0), 3)


def _normalize_kind(value: Any) -> str | None:
    """LLM 的 kind → 合法 kind 值; 未知（含 ``unknown`` 之外的自由发挥）→ ``None``。"""
    text = _clean_text(value, limit=32).lower()
    if text in _VALID_KINDS:
        return text
    return None


def _dropped(field: str, value: Any, detail: str) -> dict[str, Any]:
    """统一构造"字段被丢弃"的留痕记录。"""
    return {"kind": "llm_field_dropped", "field": field, "value": value, "detail": detail}


def _truncate(text: str, limit: int = MAX_DETAIL) -> str:
    """原样截断（保留前缀便于定位问题）; 超长时标出截断事实。"""
    return text if len(text) <= limit else text[:limit] + "…(截断)"


def rule_hint_hits(text: str) -> list[str]:
    """命中的规则域关键词（子串匹配, 按 :data:`RULE_HINT_KEYWORDS` 声明序去重保序）。"""
    lowered = (text or "").lower()
    return [word for word in RULE_HINT_KEYWORDS if word.lower() in lowered]


# ---------------------------------------------------------------------------
# 正则层 → 计划
# ---------------------------------------------------------------------------


def _scope_text(filters: dict[str, Any]) -> str:
    """把精确引用拼成一句人读的限定语（无引用 → 空串）。"""
    parts: list[str] = []
    if filters.get(FILTER_PRODUCT_CODE):
        parts.append(f"型号 {filters[FILTER_PRODUCT_CODE]}")
    if filters.get(FILTER_SECTION):
        parts.append(f"章节号 {filters[FILTER_SECTION]}")
    if filters.get(FILTER_SECTION_CONTAINS):
        parts.append(f"章节 {filters[FILTER_SECTION_CONTAINS]}")
    if filters.get(FILTER_TABLE) is not None:
        parts.append(f"表 {filters[FILTER_TABLE]}")
    return "、".join(parts)


def _regex_intent(parsed: ParsedQuery, kind: str) -> str:
    """无 LLM 时的意图句: 精确引用 + 路由判定 + 原文关键词。"""
    label = _KIND_LABELS.get(kind, kind)
    scope = _scope_text(parsed.filters)
    head = f"查找{label}（限定 {scope}）" if scope else f"按原查询查找{label}"
    tail = "、".join(parsed.keywords[:6])
    return f"{head}（关键词: {tail}）" if tail else head


def _regex_confidence(parsed: ParsedQuery, routed: RoutedQuery) -> float:
    """正则层置信度: 精确引用越多、路由越明确越高, 但**封顶** :data:`REGEX_CONF_CAP`
    ——没做语义推理就不该给高置信度（否则下游会当成已推理的结论用）。"""
    score = REGEX_CONF_BASE + 0.1 * len(parsed.filters)
    if routed.kind is not QueryKind.UNKNOWN:
        score += 0.1
    return round(min(score, REGEX_CONF_CAP), 3)


def _regex_plan(parsed: ParsedQuery, routed: RoutedQuery) -> QueryPlan:
    """确定性基线计划: 只用正则 + 分词 + 路由, 不碰 LLM。"""
    filters = dict(parsed.filters)
    provenance = {name: PROV_REGEX for name in _PLAN_FIELDS}
    provenance.update({f"filters.{key}": PROV_REGEX for key in filters})
    scope = _scope_text(filters) or "无精确引用"
    return QueryPlan(
        raw=parsed.raw,
        kind=routed.kind.value,
        intent=_regex_intent(parsed, routed.kind.value),
        filters=filters,
        entities={},
        keywords=list(parsed.keywords),
        retrieval_queries=[parsed.raw] if parsed.raw else [],
        sub_queries=[],
        reasoning=(
            f"未做语义推理（未启用 LLM）：仅正则抽取精确引用（{scope}）+ "
            f"关键词路由判定意图 {routed.kind.value}（{routed.reason}）"
        ),
        needs_rules=bool(rule_hint_hits(parsed.raw)),
        provenance=provenance,
        degradations=[],
        confidence=_regex_confidence(parsed, routed),
    )


def regex_only_plan(query: str) -> QueryPlan:
    """无 LLM 路径的计划: 正则抽取 + 分词 + 关键词路由。

    全程离线确定性, **绝不触碰 LLM**; ``degradations`` 为空（未启用 LLM 这件事
    由 :func:`understand` 在决定跳过 LLM 时补记 ``llm_unavailable``）。

    Args:
        query: 用户原始问题。

    Returns:
        :class:`QueryPlan`，``provenance`` 全为 ``"regex"``。
    """
    parsed = parse_query(query)
    return _regex_plan(parsed, route(parsed.raw))


# ---------------------------------------------------------------------------
# LLM 层 → 计划
# ---------------------------------------------------------------------------


def _llm_filters(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """抽 LLM 提出的过滤条件: 优先 ``filters`` 对象, 兼容与 query.py 同形的顶层键。

    归一: 表号数字串 → int（与 ``_extract_filters`` 同口径）; 空串/None 直接丢弃。

    Returns:
        ``(filters, 被丢弃的候选键)``——后者用于留痕, 保证脏输入不被静默吞掉。
    """
    candidates: dict[str, Any] = {}
    dropped: list[str] = []
    nested = data.get("filters")
    if isinstance(nested, dict):
        candidates.update(nested)
    elif nested is not None:
        dropped.append("filters")
    for key in (FILTER_PRODUCT_CODE, FILTER_SECTION, FILTER_TABLE):
        if key in data:
            candidates[key] = data[key]

    filters: dict[str, Any] = {}
    for key, value in candidates.items():
        text = _clean_text(value, limit=120)
        if key == FILTER_TABLE and text.isdigit():
            filters[key] = int(text)
        elif text:
            filters[key] = text
        else:
            dropped.append(str(key))
    return filters, dropped


def _title_in_lexicon(title: str, lex: SectionLexicon) -> bool:
    """章节名是否在词典里（规范化后比较）; 词典为空 → ``False``（无法验证即拒绝）。"""
    if not lex.titles:
        return False
    normalized = normalize_title(title)
    return any(normalized == known or normalized in known for known in lex.titles)


def _section_grounded_in_query(title: str, raw_query: str, filters: dict[str, Any]) -> bool:
    """LLM 提的章节名是否有依据: 查询文本里出现, 或查询已确定型号。

    错过滤比不过滤更糟: 公式/领域知识类问题（如"纹波怎么算"）本身没有型号,
    LLM 容易顺手挂一个任意文档的章节名, 直接把召回归零（评测里整轮 0 命中）。
    保守口径: 无依据就丢弃过滤并留痕, 靠向量/关键词腿找。
    """
    if filters.get(FILTER_PRODUCT_CODE):
        return True  # 型号已定 -> 由产品目录接地校验负责
    normalized_title = normalize_title(title)
    normalized_query = normalize_title(raw_query)
    return bool(normalized_title) and normalized_title in normalized_query


def _section_belongs_to_product(title: str, filters: dict[str, Any],
                                 lex: SectionLexicon, catalog: ProductCatalog | None) -> bool:
    """LLM 提的章节名是否属于查询已确定的型号。

    词表里有 ≠ 属于本型号: PA601 的"4.3.3 保护功能"也在词典里, 但对
    CY0177 的查询它是零召回的错过滤。核对方式: 该标题登记的 section_path
    首段(文档根)须包含查询型号或该型号的 doc_id 前缀。
    """
    if catalog is None or not len(catalog):
        return True  # 无法验证 -> 不拦截（缺目录的开发环境不应丢掉全部过滤）
    public_roots = {d.split(" ")[0] for d in catalog.public_doc_ids} | set(catalog.public_doc_ids)
    code = str(filters.get(FILTER_PRODUCT_CODE) or "").strip()
    if not code:
        return True  # 查询本身没定型号 -> 不做跨型号校验
    resolved = catalog.resolve(code) or code
    doc_ids = catalog.doc_ids(resolved)
    needles = [resolved, *doc_ids]
    normalized = normalize_title(title)
    for key, paths in lex.titles.items():
        if normalize_title(key) != normalized and normalized not in normalize_title(key):
            continue
        for path in paths:
            root = path.split(" > ")[0]
            if any(n and n in root for n in needles):
                return True
            # 公共知识的章节属于**所有**型号，不能因为"不属于该型号"而拒绝
            if any(root == p or root.startswith(p) for p in public_roots if p):
                return True
    return False


def _plan_from_llm(base: QueryPlan, data: dict[str, Any], lex: SectionLexicon,
                     catalog: ProductCatalog | None = None) -> QueryPlan:
    """把 LLM 的 JSON 语义字段并入正则基线计划（**原地修改** ``base`` 并返回它）。

    规则:
    - 精确引用键（:data:`PRECISION_FILTER_KEYS`）上正则已有值 → 忽略 LLM 值并留痕;
    - ``section_contains`` 必须是词典里的标题 → 词表外的一律拒绝（防编造章节名零召回）;
    - 其余 LLM 字段非法/缺失 → 保留正则基线并追加 ``llm_field_dropped``。
    """
    plan = base
    degradations = plan.degradations
    provenance = plan.provenance

    # ---- intent ----
    intent = _clean_text(data.get("intent"), limit=200)
    if intent:
        plan.intent = intent
        provenance["intent"] = PROV_LLM
    else:
        degradations.append(_dropped("intent", data.get("intent"), "LLM 未给出可用 intent"))

    # ---- kind ----
    kind = _normalize_kind(data.get("kind"))
    if kind is None:
        degradations.append(_dropped("kind", data.get("kind"), "kind 不在合法取值域内"))
    elif kind == base.kind:
        provenance["kind"] = PROV_MERGED
    else:
        plan.kind = kind
        provenance["kind"] = PROV_LLM

    # ---- filters（精确引用优先, LLM 只补新增键）----
    llm_filters, dropped_filter_keys = _llm_filters(data)
    if dropped_filter_keys:
        degradations.append(_dropped("filters", dropped_filter_keys, "过滤条件为空或类型非法, 已忽略"))
    for key, value in llm_filters.items():
        if key in PRECISION_FILTER_KEYS and key in plan.filters:
            degradations.append({
                "kind": "llm_filter_ignored",
                "key": key,
                "value": value,
                "kept": plan.filters[key],
                "detail": "精确引用由正则/词典确定, LLM 值不覆盖",
            })
            continue
        if key == FILTER_SECTION_CONTAINS and not _title_in_lexicon(str(value), lex):
            degradations.append({
                "kind": "llm_filter_ignored",
                "key": key,
                "value": value,
                "detail": "章节名不在章节词表内, 拒绝编造标题（会造成零召回）",
            })
            continue
        if key == FILTER_SECTION_CONTAINS and not _section_grounded_in_query(
            str(value), plan.raw, plan.filters
        ):
            degradations.append({
                "kind": "llm_section_ungrounded",
                "key": key,
                "value": value,
                "detail": "章节名既不在查询文本里, 也不属于已确定的型号 \u2014\u2014 拒绝虚构过滤（过滤不对只会归零）",
            })
            continue
        if key == FILTER_SECTION_CONTAINS and not _section_belongs_to_product(
            str(value), plan.filters, lex, catalog
        ):
            degradations.append({
                "kind": "llm_section_out_of_product",
                "key": key,
                "value": value,
                "detail": "该章节名在词典内, 但属于其他型号的语料（横过滤零召回）",
            })
            continue
        if key == FILTER_PRODUCT_CODE and catalog is not None and len(catalog):
            resolved = catalog.resolve(str(value))
            if resolved is None:
                degradations.append({
                    "kind": "llm_filter_ignored",
                    "key": key,
                    "value": value,
                    "detail": "型号不在产品目录内, 拒绝（错误过滤零召回）",
                })
                continue
            if resolved != value:
                degradations.append({
                    "kind": "llm_product_resolved",
                    "key": key,
                    "value": value,
                    "resolved": resolved,
                    "detail": "型号规范化到目录中的真实型号",
                })
                value = resolved
        plan.filters[key] = value
        provenance[f"filters.{key}"] = PROV_LLM
        provenance["filters"] = PROV_MERGED
    provenance.setdefault("filters", PROV_REGEX)

    # ---- entities ----
    raw_entities = data.get("entities")
    entities: dict[str, list[str]] = {}
    if isinstance(raw_entities, dict):
        for key in ENTITY_KEYS:
            items = _string_list(raw_entities.get(key), MAX_ENTITY_ITEMS)
            if items:
                entities[key] = items
        unknown_keys = sorted(str(key) for key in raw_entities if key not in ENTITY_KEYS)
        if unknown_keys:
            degradations.append(_dropped("entities", unknown_keys, "未知实体槽位, 仅接受 ENTITY_KEYS"))
        plan.entities = entities
        provenance["entities"] = PROV_LLM if entities else PROV_REGEX
    elif raw_entities is not None:
        degradations.append(_dropped("entities", raw_entities, "entities 不是对象"))

    # ---- keywords（正则在前, LLM 只补增量）----
    llm_keywords = [kw for kw in _string_list(data.get("keywords"), MAX_KEYWORDS) if kw not in STOPWORDS]
    merged_keywords = list(base.keywords)
    for kw in llm_keywords:
        if kw not in merged_keywords:
            merged_keywords.append(kw)
        if len(merged_keywords) >= MAX_KEYWORDS:
            break
    plan.keywords = merged_keywords
    provenance["keywords"] = PROV_MERGED if len(merged_keywords) > len(base.keywords) else PROV_REGEX

    # ---- retrieval_queries（原查询必在 index 0, 改写去重保序, 上限 MAX_REWRITES）----
    rewrites = [
        item
        for item in _string_list(data.get("rewrite_queries"), MAX_REWRITES * 2)
        if item != plan.raw
    ][:MAX_REWRITES]
    plan.retrieval_queries = ([plan.raw] if plan.raw else []) + rewrites
    provenance["retrieval_queries"] = PROV_LLM if rewrites else PROV_REGEX

    # ---- sub_queries（与已有查询串去重, 上限 MAX_SUB_QUERIES）----
    seen = set(plan.retrieval_queries)
    sub_queries: list[str] = []
    for item in _string_list(data.get("sub_queries"), MAX_SUB_QUERIES * 2):
        if item in seen:
            continue
        seen.add(item)
        sub_queries.append(item)
        if len(sub_queries) >= MAX_SUB_QUERIES:
            break
    plan.sub_queries = sub_queries
    provenance["sub_queries"] = PROV_LLM if sub_queries else PROV_REGEX

    # ---- needs_rules（确定性词表 ∪ LLM 判断）----
    hits = rule_hint_hits(plan.raw)
    llm_flag = data.get("needs_rules")
    if not isinstance(llm_flag, bool):
        llm_flag = None
        degradations.append(_dropped("needs_rules", llm_flag, "needs_rules 不是布尔值"))
    plan.needs_rules = bool(hits) or bool(llm_flag)
    if llm_flag is None:
        provenance["needs_rules"] = PROV_REGEX
    elif hits and llm_flag:
        provenance["needs_rules"] = PROV_MERGED
    else:
        provenance["needs_rules"] = PROV_LLM if llm_flag else PROV_REGEX

    # ---- reasoning ----
    reasoning = _clean_text(data.get("reasoning"))
    if reasoning:
        plan.reasoning = reasoning
        provenance["reasoning"] = PROV_LLM
    else:
        plan.reasoning = f"{base.reasoning}；LLM 未给出 reasoning, 语义字段来自其 JSON 其余部分"
        provenance["reasoning"] = PROV_MERGED

    # ---- confidence ----
    confidence = _coerce_confidence(data.get("confidence"))
    plan.confidence = round(max(confidence, base.confidence), 3) if confidence is not None else base.confidence
    if confidence is None:
        degradations.append(_dropped("confidence", data.get("confidence"), "置信度无法解析为 0~1 数值, 用正则基线"))
        provenance["confidence"] = PROV_REGEX
    else:
        provenance["confidence"] = PROV_LLM

    return plan


def _llm_call(llm: Any, system: str, user: str) -> dict[str, Any]:
    """调 ``llm.complete_json``（鸭子类型: 兼容只收两个位置参数的替身）。"""
    try:
        return llm.complete_json(system, user, schema_hint=_SCHEMA_HINT)
    except TypeError:  # 替身签名更窄（测试用）
        return llm.complete_json(system, user)


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------


def merge_regex(plan: QueryPlan, parsed: ParsedQuery) -> QueryPlan:
    """把正则层的确定性结果并入计划: **精确引用正则优先**, LLM 值一律让位。

    这是 :func:`understand` 的最后一道闸门——即使 LLM 输出通过了 JSON 校验,
    只要它给出的型号/章节号/章节名/表号与正则结果不同, 这里就强制回填正则值,
    并在 ``provenance`` 里把对应 ``filters.<键>`` 标回 ``"regex"``。

    Args:
        plan: 待合并的计划（通常已带上 LLM 的语义字段）。
        parsed: 正则层解析结果。

    Returns:
        新的 :class:`QueryPlan`（不修改入参; 关键词=正则在前 + 计划里的增量）。
    """
    filters = dict(plan.filters)
    provenance = dict(plan.provenance)
    for key, value in parsed.filters.items():
        if key in PRECISION_FILTER_KEYS:
            filters[key] = value  # 正则优先: 精确引用不可被语义模型覆盖
            provenance[f"filters.{key}"] = PROV_REGEX
    provenance.setdefault("filters", PROV_REGEX)

    keywords = list(parsed.keywords)
    for kw in plan.keywords:
        if kw not in keywords:
            keywords.append(kw)
    if keywords != plan.keywords:
        provenance["keywords"] = PROV_REGEX if plan.keywords == list(parsed.keywords) else PROV_MERGED

    raw = plan.raw or parsed.raw
    # 原查询必须在 index 0; 计划里已有的改写变体保序保留（不得被合并过程抹掉）
    queries = [item for item in plan.retrieval_queries if item != raw]
    retrieval_queries = ([raw] + queries) if raw else queries
    return QueryPlan(
        raw=raw,
        kind=plan.kind,
        intent=plan.intent,
        filters=filters,
        entities={key: list(value) for key, value in plan.entities.items()},
        keywords=keywords,
        retrieval_queries=retrieval_queries,
        sub_queries=list(plan.sub_queries),
        reasoning=plan.reasoning,
        needs_rules=plan.needs_rules,
        provenance=provenance,
        degradations=[dict(item) for item in plan.degradations],
        confidence=plan.confidence,
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def understand(query: str, *, llm: Any = None, lexicon_path: Path | str | None = None,
               catalog_path: Path | str | None = None) -> QueryPlan:
    """理解查询: 正则抽取 → （可选）LLM 语义推理 → 合并, 产出 :class:`QueryPlan`。

    Args:
        query: 用户原始问题。
        llm: 可选 LLM 门面（:class:`matrag.llm.LLMClient` 或鸭子类型替身,
            需暴露 ``complete_json(system, user, schema_hint=...)`` 与 ``configured``）。
            **默认 ``None``——绝不主动触碰 LLM**; 传入且 ``configured`` 为真才做语义推理。
        lexicon_path: 提示词里"章节词表"的来源路径（``None`` 走
            :func:`matrag.retrieve.query.lexicon_path` 的解析: 显式 > 环境变量 > 默认）。
            仅影响提示词接地与 ``section_contains`` 校验, 不影响正则层。

    Returns:
        :class:`QueryPlan`。LLM 不可用 / 调用失败 / 输出不可解析时**不抛异常**,
        退回正则结果并在 ``degradations`` 与 ``reasoning`` 里如实留痕。
    """
    text = (query or "").strip()
    parsed = parse_query(text)          # 正则层: 绝不带 llm, 保证基线是纯确定性结果
    base = _regex_plan(parsed, route(text))

    if llm is None:
        base.degradations.append({
            "kind": "llm_unavailable",
            "detail": "未传入 LLM 门面: 本次只做正则抽取与关键词路由, 未做语义推理",
        })
        return base
    # ``LLMClient`` 一定有 ``configured``; 鸭子类型替身没这个属性时按"可用"处理,
    # 真调用失败会走下面的 llm_failed 分支（同样留痕）。
    if not getattr(llm, "configured", True):
        base.degradations.append({
            "kind": "llm_unavailable",
            "detail": "LLM 门面未配置 (MATRAG_LLM_BASE/API_KEY/MODEL): 未做语义推理",
        })
        return base

    lex = _lexicon_cached(resolve_lexicon_path(lexicon_path))
    user_prompt = _build_user_prompt(text, parsed, _lexicon_titles(text, lex))
    try:
        data = _llm_call(llm, _SYSTEM_PROMPT, user_prompt)
    except Exception as exc:  # noqa: BLE001 —— LLM 是可选增强, 失败留痕而非让检索崩掉
        detail = f"{type(exc).__name__}: {exc}"
        raw = getattr(exc, "raw", "")
        base.degradations.append({
            "kind": "llm_failed",
            "detail": _truncate(detail),
            "fallback": "退回正则抽取结果（检索照常可用）",
            **({"raw": _truncate(str(raw), 200)} if raw else {}),
        })
        return base

    if not isinstance(data, dict):
        base.degradations.append({
            "kind": "llm_failed",
            "detail": _truncate(f"LLM 未返回 JSON 对象（type={type(data).__name__}）"),
            "fallback": "退回正则抽取结果（检索照常可用）",
        })
        return base

    return merge_regex(_plan_from_llm(base, data, lex, load_catalog(catalog_path)), parsed)


def iter_degradation_kinds(plan: QueryPlan) -> Iterable[str]:
    """计划里出现过的降级类型（排查/指标聚合用, 顺序保留）。"""
    seen: list[str] = []
    for item in plan.degradations:
        kind = str(item.get("kind", ""))
        if kind and kind not in seen:
            seen.append(kind)
    return seen
