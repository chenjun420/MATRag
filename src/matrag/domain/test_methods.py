"""产测方法层: 电源产品产测的"方法"实体（通用知识 / 公共知识, 不绑定型号）。

背景: 很多实体（术语 / 域规则 / 保护项 / 安规项）都是通过产品硬件测试方法发生关联;
本模块把"方法"建成一等实体, 通过 ``measures``（术语 id）与 ``rules``（域规则 id）
链接到领域知识图谱（:mod:`matrag.domain.knowledge_graph`）。

- 字段: 基本信息（zh/en/aliases/category/definition）+ 测试条件与参数（parameters）/
  所需仪表（instruments）/ 接口与接线（interfaces）/ 测试指标（metrics）+
  步骤与判据（procedure/criteria）+ 术语与规则链接（measures/rules）;
- 源文件: ``data/domain/source/power_test_methods.yaml``（websearch 补齐, 含来源 URL）;
- 每条方法同时渲染一张公共知识卡片（``power-method-<slug>``）: 与域知识卡片走同一条
  chunk 管线（``SourceKind.DOMAIN`` → ``scope=public``）, 供检索与引用;
- 验证: :func:`validate_test_methods` 检查 id / 类别 / 文本 / 引用完整性
  （measures 对术语表核对, rules 对规则集核对）。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from matrag.domain.cards import DOMAIN_NAME, DomainCard
from matrag.domain.rules_loader import load_rules
from matrag.domain.terms import load_terms
from matrag.domain.vocab import CATEGORIES

__all__ = [
    "DEFAULT_METHOD_REF_ALIASES_PATH",
    "DEFAULT_TEST_METHODS_PATH",
    "ISSUE_CATEGORY_UNKNOWN",
    "ISSUE_CRITERIA_EMPTY",
    "ISSUE_DEFINITION_TOO_SHORT",
    "ISSUE_MEASURE_UNKNOWN",
    "ISSUE_PROCEDURE_EMPTY",
    "ISSUE_RULE_UNKNOWN",
    "ISSUE_SOURCE_MISSING",
    "ISSUE_TEXT_MISSING",
    "METHOD_ID_PATTERN",
    "METHOD_REF_ALIASES_PATH_ENV",
    "METHOD_SOURCE_LABEL",
    "MIN_DEFINITION_CHARS",
    "TEST_METHODS_PATH_ENV",
    "TestMethod",
    "TestMethodIssue",
    "build_method_cards",
    "build_reverse_aliases",
    "load_method_ref_aliases",
    "load_test_methods",
    "resolve_external_refs_for",
    "resolve_method_ref_aliases_path",
    "resolve_methods_by_external_ref",
    "resolve_test_methods_path",
    "test_method_stats",
    "validate_test_methods",
]

#: 源文件默认路径（``data/domain/source/power_test_methods.yaml``）。
DEFAULT_TEST_METHODS_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "domain"
    / "source"
    / "power_test_methods.yaml"
)

#: 环境变量覆盖（与 ``matrag.domain.cards`` 的 ``MATRAG_DOMAIN_*`` 约定一致）。
TEST_METHODS_PATH_ENV = "MATRAG_DOMAIN_TEST_METHODS_PATH"

#: 卡片来源标签（检索引用时可见）。
METHOD_SOURCE_LABEL = "MATRag power_test_methods.yaml（websearch 补齐）"

#: ``method_id`` 格式（如 ``M-RIPPLE-NOISE``）。
METHOD_ID_PATTERN = re.compile(r"^M-[A-Z0-9-]+$")

#: definition 最短字数（低于此值视为信息量不足）。
MIN_DEFINITION_CHARS = 20

#: 质量门 issue 类型。
ISSUE_TEXT_MISSING = "text_missing"                 # zh/en 缺失
ISSUE_DEFINITION_TOO_SHORT = "definition_too_short"  # definition 过短
ISSUE_CATEGORY_UNKNOWN = "unknown_category"          # 类别不在闭集
ISSUE_PROCEDURE_EMPTY = "procedure_empty"            # 无测试步骤
ISSUE_CRITERIA_EMPTY = "criteria_empty"              # 无判据
ISSUE_SOURCE_MISSING = "source_missing"              # 无参考来源
ISSUE_MEASURE_UNKNOWN = "measure_unknown"            # measures 引用了不存在的术语
ISSUE_RULE_UNKNOWN = "rule_unknown"                  # rules 引用了不存在的规则

_REQUIRED_KEYS = (
    "method_id",
    "zh",
    "en",
    "category",
    "definition",
    "parameters",
    "instruments",
    "interfaces",
    "metrics",
    "procedure",
    "criteria",
    "measures",
    "references",
    "retrieved",
)


@dataclass(frozen=True)
class TestMethod:
    """一条电源产品产测方法（通用知识, 不绑定型号）。

    Attributes:
        method_id: 稳定 id（``M-XXX``, 全局唯一）。
        zh: 中文名。
        en: 英文名。
        aliases: 别名（检索链接用）。
        category: 类别, 必须 ∈ :data:`matrag.domain.vocab.CATEGORIES`。
        definition: 一句话定义（含适用说明）。
        parameters: 测试条件与参数（输入电压/负载/温度等工况）。
        instruments: 所需仪表（含关键规格要求）。
        interfaces: 接口与接线（被测端口 ↔ 仪器）。
        metrics: 测试指标（针对本方法的量化验收指标）。
        procedure: 测试步骤（有序）。
        criteria: 判据（通常引用域规则 id）。
        measures: 关联术语 id（``T-*``）。
        rules: 关联域规则 id（``K-*``）。
        standards: 引用标准（可空）。
        references: websearch 检索来源（URL）。
        retrieved: 检索日期（``YYYY-MM-DD``）。
    """

    #: 供 pytest 收集器识别: 名字以 ``Test`` 开头但不是测试类, 不要收集。
    __test__ = False

    method_id: str
    zh: str
    en: str
    aliases: tuple[str, ...] = ()
    category: str = ""
    definition: str = ""
    parameters: tuple[str, ...] = ()
    instruments: tuple[str, ...] = ()
    interfaces: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()
    procedure: tuple[str, ...] = ()
    criteria: tuple[str, ...] = ()
    measures: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()
    standards: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    retrieved: str = ""

    @property
    def card_id(self) -> str:
        """公共知识卡片 id: ``power-method-<slug>``（slug 由 method_id 去掉 ``M-`` 小写）。"""
        slug = self.method_id[2:] if self.method_id.startswith("M-") else self.method_id
        return f"power-method-{slug.lower()}"

    def names(self) -> tuple[str, ...]:
        """全部可检索名称: ``(zh, en, *aliases)``, 去空并按序去重。"""
        out: list[str] = []
        for name in (self.zh, self.en, *self.aliases):
            if name and name not in out:
                out.append(name)
        return tuple(out)

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict（字段顺序与源文件一致）。"""
        return {
            "method_id": self.method_id,
            "zh": self.zh,
            "en": self.en,
            "aliases": list(self.aliases),
            "category": self.category,
            "definition": self.definition,
            "parameters": list(self.parameters),
            "instruments": list(self.instruments),
            "interfaces": list(self.interfaces),
            "metrics": list(self.metrics),
            "procedure": list(self.procedure),
            "criteria": list(self.criteria),
            "measures": list(self.measures),
            "rules": list(self.rules),
            "standards": list(self.standards),
            "references": list(self.references),
            "retrieved": self.retrieved,
        }


@dataclass(frozen=True)
class TestMethodIssue:
    """产测方法质量门的一条问题（类型见模块级 ``ISSUE_*`` 常量）。"""

    #: 同上: 避免 pytest 把本类当测试类收集。
    __test__ = False

    kind: str
    detail: str
    involved: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict。"""
        return {"kind": self.kind, "detail": self.detail, "involved": list(self.involved)}


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #


def resolve_test_methods_path(path: Path | str | None = None) -> Path:
    """解析源文件路径: 显式参数 > 环境变量 > 仓库默认路径。"""
    if path is not None:
        return Path(path)
    override = os.environ.get(TEST_METHODS_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return DEFAULT_TEST_METHODS_PATH


# --------------------------------------------------------------------------- #
# M-* ↔ external_ref 命名空间别名 (P3-4 stub)
# --------------------------------------------------------------------------- #

#: method_ref_aliases.yaml 默认路径（与 power_test_methods.yaml 同目录）。
DEFAULT_METHOD_REF_ALIASES_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "domain"
    / "source"
    / "method_ref_aliases.yaml"
)

#: 环境变量覆盖 (MATRAG_DOMAIN_* 约定)。
METHOD_REF_ALIASES_PATH_ENV = "MATRAG_DOMAIN_METHOD_REF_ALIASES_PATH"


def resolve_method_ref_aliases_path(path: Path | str | None = None) -> Path:
    """解析 method_ref_aliases 源文件路径: 显式参数 > 环境变量 > 仓库默认路径。"""
    if path is not None:
        return Path(path)
    override = os.environ.get(METHOD_REF_ALIASES_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return DEFAULT_METHOD_REF_ALIASES_PATH


def load_method_ref_aliases(path: Path | str | None = None) -> dict[str, tuple[str, ...]]:
    """加载 M-* -> external_refs 别名表。

    Returns:
        ``{method_id: (external_ref, ...)}``; 缺文件或 aliases 为空 dict 时返回空映射,
        并 ``logger.warning`` 一次性留痕（fail-fast 纪律: 降级可查）。

    Note:
        数据缺失**不**阻断: A.1 #2 已把这条映射列为 WARN 级别长期 backlog。
        业务方在 P3-4 内填入真实映射后, 本函数返回非空字典, 上层 RAG 可以做
        双向 cross-namespace 召回。
    """
    import logging

    logger = logging.getLogger(__name__)
    resolved = resolve_method_ref_aliases_path(path)
    if not resolved.exists():
        logger.warning(
            "method_ref_aliases 源文件不存在: %s (返回空映射, RAG 召回无法做 cross-namespace)",
            resolved,
        )
        return {}
    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.warning("method_ref_aliases 解析失败: %s (返回空映射): %s", resolved, exc)
        return {}
    raw_aliases = data.get("aliases", {})
    if not isinstance(raw_aliases, dict):
        logger.warning("method_ref_aliases.aliases 非 dict: %s", type(raw_aliases).__name__)
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for method_id, payload in raw_aliases.items():
        if not isinstance(payload, dict):
            continue
        ext_refs = payload.get("external_refs", ())
        if not isinstance(ext_refs, (list, tuple)):
            continue
        out[str(method_id)] = tuple(str(r) for r in ext_refs if r)
    return out


def build_reverse_aliases(
    forward: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, tuple[str, ...]]:
    """由正向 M-* -> external_refs 推导反向 external_ref -> M-* 映射。

    ``forward=None`` 时默认重新调用 :func:`load_method_ref_aliases`。
    """
    if forward is None:
        forward = load_method_ref_aliases()
    reverse: dict[str, list[str]] = {}
    for method_id, refs in forward.items():
        for ref in refs:
            reverse.setdefault(ref, []).append(method_id)
    return {k: tuple(sorted(v)) for k, v in reverse.items()}


def resolve_external_refs_for(
    method_id: str, *, aliases: Mapping[str, Sequence[str]] | None = None
) -> tuple[str, ...]:
    """返回 ``method_id`` 对应的所有外部 standard refs; 未匹配返回空 tuple。"""
    if aliases is None:
        aliases = load_method_ref_aliases()
    return tuple(aliases.get(method_id, ()))


def resolve_methods_by_external_ref(
    external_ref: str, *, reverse: Mapping[str, Sequence[str]] | None = None
) -> tuple[str, ...]:
    """返回 ``external_ref`` 对应的所有 M-* method_ids; 未匹配返回空 tuple。"""
    if reverse is None:
        reverse = build_reverse_aliases()
    return tuple(reverse.get(external_ref, ()))


def _require_str(raw: dict[str, Any], key: str, *, where: str) -> str:
    """取必填字符串字段（缺失/空/非字符串 → ValueError, fail-fast）。"""
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} 缺少非空字符串字段 {key!r}: {value!r}")
    return value.strip()


def _str_tuple(raw: dict[str, Any], key: str, *, where: str, required: bool = False) -> tuple[str, ...]:
    """取字符串列表字段（缺省 → 空元组; 类型错误/空项 → ValueError）。"""
    value = raw.get(key)
    if value is None:
        if required:
            raise ValueError(f"{where} 缺少必填列表字段 {key!r}")
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{where} 的 {key!r} 应为列表, 实际是 {type(value).__name__}: {value!r}")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{where} 的 {key!r} 含非法条目: {item!r}")
        out.append(item.strip())
    if required and not out:
        raise ValueError(f"{where} 的 {key!r} 不能为空列表")
    return tuple(out)


def _build_method(raw: Any, *, index: int) -> TestMethod:
    """把一条 YAML 记录构造成 :class:`TestMethod`（结构错误直接抛）。"""
    where = f"第 {index + 1} 条产测方法"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 应为映射, 实际是 {type(raw).__name__}: {raw!r}")
    method_id = _require_str(raw, "method_id", where=where)
    if not METHOD_ID_PATTERN.match(method_id):
        raise ValueError(f"{where} 的 method_id 格式非法（应匹配 {METHOD_ID_PATTERN.pattern}）: {method_id!r}")
    where = f"产测方法 {method_id}"
    return TestMethod(
        method_id=method_id,
        zh=_require_str(raw, "zh", where=where),
        en=_require_str(raw, "en", where=where),
        aliases=_str_tuple(raw, "aliases", where=where),
        category=_require_str(raw, "category", where=where),
        definition=_require_str(raw, "definition", where=where),
        parameters=_str_tuple(raw, "parameters", where=where, required=True),
        instruments=_str_tuple(raw, "instruments", where=where, required=True),
        interfaces=_str_tuple(raw, "interfaces", where=where, required=True),
        metrics=_str_tuple(raw, "metrics", where=where, required=True),
        procedure=_str_tuple(raw, "procedure", where=where, required=True),
        criteria=_str_tuple(raw, "criteria", where=where, required=True),
        measures=_str_tuple(raw, "measures", where=where, required=True),
        rules=_str_tuple(raw, "rules", where=where),
        standards=_str_tuple(raw, "standards", where=where),
        references=_str_tuple(raw, "references", where=where, required=True),
        retrieved=_require_str(raw, "retrieved", where=where),
    )


def load_test_methods(path: Path | str | None = None) -> list[TestMethod]:
    """加载产测方法源文件。

    Args:
        path: 源文件路径; ``None`` 表示环境变量 / 默认路径。

    Returns:
        :class:`TestMethod` 列表（按源文件顺序）。

    Raises:
        FileNotFoundError: 源文件缺失（不静默返回空列表）。
        ValueError: 结构非法（缺字段/类型错误/id 格式错误/id 重复）。
    """
    resolved = resolve_test_methods_path(path)
    if not resolved.is_file():
        raise FileNotFoundError(f"产测方法源文件不存在: {resolved}（可用 {TEST_METHODS_PATH_ENV} 覆盖）")
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"产测方法源文件应为列表: {resolved}")
    methods = [_build_method(item, index=i) for i, item in enumerate(raw)]
    ids = [m.method_id for m in methods]
    duplicates = sorted({x for x in ids if ids.count(x) > 1})
    if duplicates:
        raise ValueError(f"method_id 重复: {duplicates}")
    return methods


# --------------------------------------------------------------------------- #
# 质量门 / 统计
# --------------------------------------------------------------------------- #


def _ids_of(items: Sequence[Any] | None, attr: str) -> set[str] | None:
    """从对象序列或字符串序列抽取 id 集合（``None`` → ``None``）。"""
    if items is None:
        return None
    out: set[str] = set()
    for item in items:
        out.add(str(getattr(item, attr, item)))
    return out


def validate_test_methods(
    methods: Sequence[TestMethod] | None = None,
    *,
    terms: Sequence[Any] | None = None,
    rules: Sequence[Any] | None = None,
) -> list[TestMethodIssue]:
    """产测方法质量门（空列表 = 通过）。

    检查引用时以**术语表 / 规则集**为准（调用方可注入; 缺省现加载）。
    单条方法内部的结构性问题在 :func:`load_test_methods` 已 fail-fast, 这里检查
    跨文件引用与内容质量。
    """
    items = list(methods) if methods is not None else load_test_methods()
    term_ids = _ids_of(terms, "term_id")
    if term_ids is None:
        term_ids = {t.term_id for t in load_terms()}
    rule_ids = _ids_of(rules, "rule_id")
    if rule_ids is None:
        rule_ids = {r.rule_id for r in load_rules()}

    issues: list[TestMethodIssue] = []
    for method in items:
        where = f"产测方法 {method.method_id}"
        if not method.zh.strip():
            issues.append(TestMethodIssue(ISSUE_TEXT_MISSING, f"{where} 缺少中文名", (method.method_id,)))
        if not method.en.strip():
            issues.append(TestMethodIssue(ISSUE_TEXT_MISSING, f"{where} 缺少英文名", (method.method_id,)))
        if len(method.definition.strip()) < MIN_DEFINITION_CHARS:
            issues.append(
                TestMethodIssue(
                    ISSUE_DEFINITION_TOO_SHORT,
                    f"{where} 的 definition 少于 {MIN_DEFINITION_CHARS} 字: {method.definition!r}",
                    (method.method_id,),
                )
            )
        if method.category not in CATEGORIES:
            issues.append(
                TestMethodIssue(
                    ISSUE_CATEGORY_UNKNOWN,
                    f"{where} 的类别不在闭集: {method.category!r}",
                    (method.method_id, method.category),
                )
            )
        if not method.procedure:
            issues.append(TestMethodIssue(ISSUE_PROCEDURE_EMPTY, f"{where} 缺少测试步骤", (method.method_id,)))
        if not method.criteria:
            issues.append(TestMethodIssue(ISSUE_CRITERIA_EMPTY, f"{where} 缺少判据", (method.method_id,)))
        if not method.references:
            issues.append(TestMethodIssue(ISSUE_SOURCE_MISSING, f"{where} 缺少参考来源", (method.method_id,)))
        for term_id in method.measures:
            if term_id not in term_ids:
                issues.append(
                    TestMethodIssue(
                        ISSUE_MEASURE_UNKNOWN,
                        f"{where} 的 measures 引用了不存在的术语: {term_id!r}",
                        (method.method_id, term_id),
                    )
                )
        for rule_id in method.rules:
            if rule_id not in rule_ids:
                issues.append(
                    TestMethodIssue(
                        ISSUE_RULE_UNKNOWN,
                        f"{where} 的 rules 引用了不存在的规则: {rule_id!r}",
                        (method.method_id, rule_id),
                    )
                )
    return issues


def test_method_stats(methods: Sequence[TestMethod] | None = None) -> dict[str, Any]:
    """统计摘要（纯数据, 确定性）。

    Returns:
        ``{"total", "by_category", "measures_count", "rules_count"}``:
        ``by_category`` 为类别 → 条数（键已排序）; ``measures_count`` /
        ``rules_count`` 为去重后的关联术语 / 规则数。
    """
    items = list(methods) if methods is not None else load_test_methods()
    by_category: dict[str, int] = {}
    measures: set[str] = set()
    rules: set[str] = set()
    for method in items:
        by_category[method.category] = by_category.get(method.category, 0) + 1
        measures.update(method.measures)
        rules.update(method.rules)
    return {
        "total": len(items),
        "by_category": {k: by_category[k] for k in sorted(by_category)},
        "measures_count": len(measures),
        "rules_count": len(rules),
    }


# --------------------------------------------------------------------------- #
# 公共知识卡片
# --------------------------------------------------------------------------- #


def _render_body(method: TestMethod) -> str:
    """把一条方法渲染成卡片正文（供 chunk/检索/引用）。"""
    lines: list[str] = [
        "（电源产品产测通用方法 · 公共知识 · 不绑定型号; 具体限值以产品规格书为准）",
        "",
        f"用途: {method.definition}",
        "",
    ]
    if method.parameters:
        lines.append("测试条件与参数:")
        lines.extend(f"- {item}" for item in method.parameters)
        lines.append("")
    if method.instruments:
        lines.append("所需仪表:")
        lines.extend(f"- {item}" for item in method.instruments)
        lines.append("")
    if method.interfaces:
        lines.append("接口与接线:")
        lines.extend(f"- {item}" for item in method.interfaces)
        lines.append("")
    if method.metrics:
        lines.append("测试指标:")
        lines.extend(f"- {item}" for item in method.metrics)
        lines.append("")
    lines.append("测试步骤:")
    for i, step in enumerate(method.procedure, 1):
        lines.append(f"{i}. {step}")
    lines.append("")
    lines.append("判据:")
    for item in method.criteria:
        lines.append(f"- {item}")
    links: list[str] = []
    if method.measures:
        links.append("关联术语: " + "、".join(method.measures))
    if method.rules:
        links.append("关联规则: " + "、".join(method.rules))
    if method.standards:
        links.append("引用标准: " + "、".join(method.standards))
    if method.aliases:
        links.append("别名: " + "、".join(method.aliases))
    if links:
        lines.append("")
        lines.extend(links)
    if method.references:
        lines.append("")
        lines.append("参考来源:")
        for url in method.references:
            lines.append(f"- {url}")
    return "\n".join(lines)


def build_method_cards(methods: Sequence[TestMethod] | None = None) -> list[DomainCard]:
    """把产测方法渲染为公共知识卡片（与域知识卡片同一管线）。

    Returns:
        :class:`matrag.domain.cards.DomainCard` 列表; ``card_id`` 为
        ``power-method-<slug>``（重复运行时逐位一致）。
    """
    items = list(methods) if methods is not None else load_test_methods()
    return [
        DomainCard(
            card_id=method.card_id,
            title=f"## {method.zh}",
            section_path=method.zh,
            body=_render_body(method),
            source=METHOD_SOURCE_LABEL,
            domain=DOMAIN_NAME,
        )
        for method in items
    ]
