"""电力域通用术语表 (中英缩写 + 定义) 的加载、索引与质量门。

定位: 本模块是**通用知识层**的术语侧, 只做四件事 —— 读 YAML、建别名索引、
按文本查术语、跑质量门。它不碰知识图谱组装 (节点/关系由集成方实现),
也不碰规则求值 (:mod:`matrag.domain.rules` 的职责)。

设计要点:

1. **类别闭集复用**: ``category`` 一律取自
   :data:`matrag.domain.vocab.CATEGORIES`, 本模块不另造分类字符串;
2. **别名归一复用**: 索引键一律经 :func:`matrag.domain.vocab.normalize_name`
   (NFKC + 连接符统一 + 去空白 + 小写), 因此 ``"Hold-Up Time"`` 与
   ``"hold up time"`` 指向同一术语;
3. **冲突不静默**: 两个术语归一后同名时 :func:`build_term_index` 直接
   :class:`ValueError` (而不是后者覆盖前者), 否则查询结果会随机漂移;
4. **fail-fast**: 文件缺失 ``FileNotFoundError``, 结构非法 ``ValueError``,
   绝不返回空列表假装成功。

术语源文件: ``data/domain/source/power_terms.yaml``。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from matrag.domain.vocab import CATEGORIES, normalize_name

__all__ = [
    "DEFAULT_TERMS_PATH",
    "ISSUE_ABBR_DUPLICATE",
    "ISSUE_ALIAS_CONFLICT",
    "ISSUE_CATEGORY_UNKNOWN",
    "ISSUE_DEFINITION_TOO_SHORT",
    "ISSUE_SOURCE_MISSING",
    "MIN_DEFINITION_CHARS",
    "SOURCE_KINDS",
    "Term",
    "TermIssue",
    "build_term_index",
    "find_term",
    "load_terms",
    "term_stats",
    "validate_terms",
]

#: 术语源文件默认路径 (``data/domain/source/power_terms.yaml``)。
DEFAULT_TERMS_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "domain"
    / "source"
    / "power_terms.yaml"
)

#: 定义被认为"过短"的字符数下限 (低于此值判质量门问题, 不算加载错误)。
MIN_DEFINITION_CHARS = 10

#: 质量门 issue 类型。
ISSUE_CATEGORY_UNKNOWN = "unknown_category"
ISSUE_SOURCE_MISSING = "source_missing"
ISSUE_DEFINITION_TOO_SHORT = "definition_too_short"
ISSUE_ABBR_DUPLICATE = "abbr_duplicate"
ISSUE_ALIAS_CONFLICT = "alias_conflict"

#: 允许的 ``source.kind`` 取值 (与源文件约定一致)。
SOURCE_KINDS: tuple[str, ...] = ("spec_table", "card", "standard", "websearch")


# --------------------------------------------------------------------------- #
# 值对象
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Term:
    """一个领域术语 (中英缩写 + 定义 + 关联)。

    Attributes:
        term_id: 稳定 id (``T-XXX``, 全局唯一)。
        zh: 中文名。
        en: 英文全名 (无则空串)。
        abbr: 缩写 (无则空串)。
        category: 类别, 必须 ∈ :data:`matrag.domain.vocab.CATEGORIES`。
        definition: 中文定义 (质量门要求 ≥ :data:`MIN_DEFINITION_CHARS` 字)。
        aliases: 其他常见写法/中文别名。
        properties: 关联的本体属性名 (``ontology.properties()`` 里的真实键),
            不确定时为空元组 (宁缺毋滥)。
        related_rules: 关联的域规则 id (``power_rules.yaml`` 里的真实 id),
            不确定时为空元组 (宁缺毋滥)。
        source: 出处 dict (含 ``kind`` / ``name`` 等, 原样保留)。
    """

    term_id: str
    zh: str
    en: str
    abbr: str
    category: str
    definition: str
    aliases: tuple[str, ...] = ()
    properties: tuple[str, ...] = ()
    related_rules: tuple[str, ...] = ()
    source: dict[str, Any] = field(default_factory=dict)

    def all_names(self) -> tuple[str, ...]:
        """全部可检索名称: ``(zh, en, abbr)`` + ``aliases``, 去空并按序去重。"""
        names: list[str] = []
        for name in (self.zh, self.en, self.abbr, *self.aliases):
            text = str(name).strip() if name is not None else ""
            if text and text not in names:
                names.append(text)
        return tuple(names)

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict (字段顺序与源文件一致)。"""
        return {
            "term_id": self.term_id,
            "zh": self.zh,
            "en": self.en,
            "abbr": self.abbr,
            "category": self.category,
            "definition": self.definition,
            "aliases": list(self.aliases),
            "properties": list(self.properties),
            "related_rules": list(self.related_rules),
            "source": dict(self.source),
        }


@dataclass(frozen=True)
class TermIssue:
    """术语质量门的一条问题 (类型见模块级 ``ISSUE_*`` 常量)。

    Attributes:
        kind: 问题类型。
        detail: 人类可读的中文说明 (含具体术语 id 与取值)。
        involved: 涉及的术语 id (已排序)。
    """

    kind: str
    detail: str
    involved: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict。"""
        return {
            "kind": self.kind,
            "detail": self.detail,
            "involved": list(self.involved),
        }


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #


def _as_str_tuple(raw: Any, *, where: str) -> tuple[str, ...]:
    """把 YAML 里的列表字段转成去空的字符串元组; 结构非法即 ValueError。"""
    if raw is None:
        return ()
    if isinstance(raw, str):
        raise ValueError(f"{where} 应为列表, 实际是字符串: {raw!r}")
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{where} 应为列表, 实际是 {type(raw).__name__}: {raw!r}")
    out: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _build_term(raw: dict[str, Any], *, index: int) -> Term:
    """把原始 dict 组装成 :class:`Term`; 结构不合法即 ValueError。

    ``aliases`` / ``properties`` / ``related_rules`` 允许缺省 (取空元组),
    其余字段必须给出且为非空字符串。
    """
    where = f"术语列表第 {index} 项"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 不是映射: {raw!r}")
    term_id = raw.get("term_id")
    if not isinstance(term_id, str) or not term_id.strip():
        raise ValueError(f"{where} 缺少合法的 term_id: {term_id!r}")
    term_id = term_id.strip()
    for key in ("zh", "category", "definition"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"术语 {term_id} 缺少合法字段 {key}: {value!r}")
    source = raw.get("source")
    if source is None:
        source_dict: dict[str, Any] = {}
    elif isinstance(source, dict):
        source_dict = dict(source)
    else:
        raise ValueError(
            f"术语 {term_id} 的 source 应为映射, 实际是 "
            f"{type(source).__name__}: {source!r}"
        )
    return Term(
        term_id=term_id,
        zh=str(raw["zh"]).strip(),
        en=str(raw.get("en", "")).strip(),
        abbr=str(raw.get("abbr", "")).strip(),
        category=str(raw["category"]).strip(),
        definition=str(raw["definition"]).strip(),
        aliases=_as_str_tuple(raw.get("aliases"), where=f"术语 {term_id} 的 aliases"),
        properties=_as_str_tuple(
            raw.get("properties"), where=f"术语 {term_id} 的 properties"
        ),
        related_rules=_as_str_tuple(
            raw.get("related_rules"), where=f"术语 {term_id} 的 related_rules"
        ),
        source=source_dict,
    )


def load_terms(path: Path | str | None = None) -> list[Term]:
    """加载术语表。

    Args:
        path: YAML 路径; ``None`` 表示 :data:`DEFAULT_TERMS_PATH`。

    Returns:
        :class:`Term` 列表, **顺序与文件一致** (确定性, 测试可固化)。

    Raises:
        FileNotFoundError: 文件不存在 (fail-fast, 不返回空列表)。
        ValueError: 顶层不是 ``{terms: [...]}``、条目结构非法或 ``term_id`` 重复。
    """
    file = Path(path) if path is not None else DEFAULT_TERMS_PATH
    if not file.is_file():
        raise FileNotFoundError(f"术语表文件不存在: {file}")
    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "terms" not in data:
        raise ValueError(f"术语表缺少顶层 terms 键: {file}")
    raw_terms = data["terms"]
    if not isinstance(raw_terms, list) or not raw_terms:
        raise ValueError(f"术语表 terms 为空或不是列表: {file}")

    terms = [_build_term(item, index=i) for i, item in enumerate(raw_terms, start=1)]
    seen: dict[str, int] = {}
    for term in terms:
        if term.term_id in seen:
            raise ValueError(
                f"术语 id 重复: {term.term_id} "
                f"(第 {seen[term.term_id]} 项与后续项)"
            )
        seen[term.term_id] = len(seen) + 1
    return terms


# --------------------------------------------------------------------------- #
# 索引与查询
# --------------------------------------------------------------------------- #


def build_term_index(terms: Sequence[Term] | None = None) -> dict[str, str]:
    """构建「归一化名称 → term_id」索引 (含 zh / en / abbr / aliases)。

    归一化用 :func:`matrag.domain.vocab.normalize_name`, 因此大小写、空白、
    全角与连接符差异都不影响命中; 归一后的空串直接跳过 (不建空键)。

    Raises:
        ValueError: 两个不同术语归一后同名 (不静默覆盖, 否则查询结果随机漂移)。
    """
    items = list(terms) if terms is not None else load_terms()
    index: dict[str, str] = {}
    owners: dict[str, str] = {}
    for term in items:
        for name in term.all_names():
            key = normalize_name(name)
            if not key:
                continue
            existing = owners.get(key)
            if existing is not None and existing != term.term_id:
                raise ValueError(
                    f"术语名称归一后冲突: {name!r} (键 {key!r}) 同时属于 "
                    f"{existing} 与 {term.term_id}"
                )
            owners[key] = term.term_id
            index[key] = term.term_id
    return index


def find_term(
    text: str,
    *,
    index: dict[str, str] | None = None,
    terms: Sequence[Term] | None = None,
) -> Term | None:
    """按文本查术语 (归一后精确匹配 zh/en/abbr/aliases 之一)。

    Args:
        text: 待查文本, 允许大小写/空白/全角形态差异。
        index: 预构建索引 (优先使用, 避免重复建表)。
        terms: 术语列表 (未给 index 时用于建索引; 两者都没给则加载默认文件)。

    Returns:
        命中的 :class:`Term`; 未命中返回 ``None`` (查询无结果属正常语义)。
    """
    items = list(terms) if terms is not None else None
    table = dict(index) if index is not None else build_term_index(items)
    term_id = table.get(normalize_name(text))
    if items is None:
        items = load_terms()
    if term_id is None:
        return None
    for term in items:
        if term.term_id == term_id:
            return term
    return None


# --------------------------------------------------------------------------- #
# 质量门
# --------------------------------------------------------------------------- #


def _duplicates(values: Iterable[str]) -> list[str]:
    """返回出现次数 >1 的值 (保持首次出现顺序, 去重)。"""
    seen: set[str] = set()
    dup: list[str] = []
    for value in values:
        if value in seen and value not in dup:
            dup.append(value)
        seen.add(value)
    return dup


def validate_terms(terms: Sequence[Term] | None = None) -> list[TermIssue]:
    """术语表质量门: **空列表 = 通过**。

    检出的类型 (常量见模块顶部):

    - :data:`ISSUE_CATEGORY_UNKNOWN` —— ``category`` 不在
      :data:`matrag.domain.vocab.CATEGORIES` 中;
    - :data:`ISSUE_SOURCE_MISSING` —— 缺 ``source`` 或缺 ``source.name``;
    - :data:`ISSUE_DEFINITION_TOO_SHORT` —— 定义短于
      :data:`MIN_DEFINITION_CHARS` 字 (形如 ``"见标准"`` 的占位定义);
    - :data:`ISSUE_ABBR_DUPLICATE` —— 非空 ``abbr`` 归一后在**缩写域内**重复
      (只与其它术语的 abbr 比, 不牵连名称);
    - :data:`ISSUE_ALIAS_CONFLICT` —— 名称/别名与**别的**术语的名称/别名归一后
      相同 (缩写由上面那条单独负责, 避免同一事实报两次)。

    Returns:
        issue 列表, 按 ``(kind, detail, involved)`` 排序保证确定性。
    """
    items = list(terms) if terms is not None else load_terms()
    issues: list[TermIssue] = []

    for term in items:
        if term.category not in CATEGORIES:
            issues.append(
                TermIssue(
                    kind=ISSUE_CATEGORY_UNKNOWN,
                    detail=(
                        f"术语 {term.term_id} 的 category={term.category!r} "
                        f"不在 vocab.CATEGORIES 闭集内"
                    ),
                    involved=(term.term_id,),
                )
            )
        source = term.source
        if not isinstance(source, dict) or not str(source.get("name", "")).strip():
            issues.append(
                TermIssue(
                    kind=ISSUE_SOURCE_MISSING,
                    detail=(
                        f"术语 {term.term_id} 缺少 source 或 source.name: "
                        f"{source!r}"
                    ),
                    involved=(term.term_id,),
                )
            )
        if len(term.definition) < MIN_DEFINITION_CHARS:
            issues.append(
                TermIssue(
                    kind=ISSUE_DEFINITION_TOO_SHORT,
                    detail=(
                        f"术语 {term.term_id} 的 definition 仅 "
                        f"{len(term.definition)} 字 (<{MIN_DEFINITION_CHARS}): "
                        f"{term.definition!r}"
                    ),
                    involved=(term.term_id,),
                )
            )

    abbr_keys: list[tuple[str, str]] = []
    for term in items:
        if term.abbr:
            abbr_keys.append((normalize_name(term.abbr), term.term_id))
    for key in _duplicates([key for key, _ in abbr_keys]):
        owners = sorted(tid for k, tid in abbr_keys if k == key)
        issues.append(
            TermIssue(
                kind=ISSUE_ABBR_DUPLICATE,
                detail=f"缩写 {key!r} 归先后被多个术语共用: {owners}",
                involved=tuple(owners),
            )
        )

    owner: dict[str, str] = {}
    for term in items:
        # 缩写域与名称域分开查重: 否则两个术语仅缩写相同就会同时报
        # abbr_duplicate 与 alias_conflict, 同一事实报两次。
        names = (term.zh, term.en, *term.aliases)
        for name in names:
            key = normalize_name(name)
            if not key:
                continue
            holder = owner.get(key)
            if holder is None:
                owner[key] = term.term_id
            elif holder != term.term_id:
                issues.append(
                    TermIssue(
                        kind=ISSUE_ALIAS_CONFLICT,
                        detail=(
                            f"名称/别名 {name!r} (键 {key!r}) 同时属于 "
                            f"{holder} 与 {term.term_id}"
                        ),
                        involved=tuple(sorted({holder, term.term_id})),
                    )
                )

    issues.sort(key=lambda issue: (issue.kind, issue.detail, issue.involved))
    return issues


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #


def term_stats(terms: Sequence[Term] | None = None) -> dict[str, Any]:
    """术语表统计摘要 (纯数据, 无时间戳, 可 JSON 序列化, 确定性)。

    Returns:
        ``{"count", "by_category", "with_properties", "with_rules",
        "sources_by_kind", "aliases_count"}``:

        - ``by_category``: 类别 → 条数 (只含出现过的类别, 键已排序);
        - ``with_properties`` / ``with_rules``: 填了 ``properties`` /
          ``related_rules`` 的条数 (0 表示该层尚未与本体/规则关联);
        - ``sources_by_kind``: ``source.kind`` → 条数 (缺失 kind 记为 ``""``);
        - ``aliases_count``: 全部别名条目数 (不含 zh/en/abbr)。
    """
    items = list(terms) if terms is not None else load_terms()
    by_category: dict[str, int] = {}
    sources_by_kind: dict[str, int] = {}
    for term in items:
        by_category[term.category] = by_category.get(term.category, 0) + 1
        kind = term.source.get("kind") if isinstance(term.source, dict) else None
        key = str(kind).strip() if kind is not None else ""
        sources_by_kind[key] = sources_by_kind.get(key, 0) + 1
    return {
        "count": len(items),
        "by_category": {k: by_category[k] for k in sorted(by_category)},
        "with_properties": sum(1 for t in items if t.properties),
        "with_rules": sum(1 for t in items if t.related_rules),
        "sources_by_kind": {k: sources_by_kind[k] for k in sorted(sources_by_kind)},
        "aliases_count": sum(len(t.aliases) for t in items),
    }
