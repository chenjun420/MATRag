"""域知识图谱: 领域知识层 (术语/公理/本体) 与产品层 (规格书参数/标准) 的统一实体-关系图。

定位: 本模块是"本体 + KG"集成的**汇合点**。它不重新抽取任何数据, 只把仓库里
已经建好的四份产物连成一张可查询的图:

============================  ==============================================
数据层                        来源模块
============================  ==============================================
领域术语 (129 条)             :mod:`matrag.domain.terms`
域公理/公式 (76 条)           :mod:`matrag.domain.axioms`
电力域本体 (类/属性/规则链接)  :mod:`matrag.domain.ontology`
产品参数/标准 (三份真实规格书)  :mod:`matrag.domain.spec_params`
产测方法 (31 条)              :mod:`matrag.domain.test_methods`
============================  ==============================================

十类节点 (id 前缀即 kind)::

    product:<product_code>      产品 (规格书对应的机型)
    param:<param_id>            规格书参数实例 (param_id 全局唯一)
    standard:<standard_id>      规格书引用的标准
    term:<term_id>              领域术语
    rule:<rule_id>              域规则
    axiom:<axiom_id>            公理/定理/工程公式
    method:<method_id>          产测方法 (通用知识, 不绑定型号)
    class:<name>                被测对象类型 (只收 axis=subject 的本体类)
    property:<name>             本体量纲属性
    category:<name>             知识主题 (含 vocab.CATEGORY_LABELS 中文标签)

**为什么 category 轴的本体类不建 class 节点**: 类别类 (``rule.category``) 与
category 节点是同一份信息 (知识主题), 二者都建会让"知识主题"在图里出现两套
节点 id, 查询与统计立刻分叉。因此 ``class`` 只收 ``axis=subject`` 的
被测对象类型 (可实例化), 知识主题统一由 ``category`` 节点承载。

十三条关系 (relation 常量, snake_case)::

    has_parameter   product  → param        产品拥有该参数
    conforms_to     product  → standard     产品符合该标准
    denotes         param    → term         参数名指称某术语 (match/key/field)
    instance_of     param    → property     参数是该量纲属性的实例 (经命中术语)
    in_category     param/term/axiom/rule/property → category
    related_rule    term/axiom → rule       术语/公理关联的域规则
    has_property    term/axiom → property   术语/公理关联的量纲属性
    consumes        rule     → property     规则消费的输入
    produces        rule     → property     规则产出的输出
    applies_to      rule     → class        规则约束的被测对象类型
    in_class        property → class        属性出现的被测对象类型
    measures        method   → term         方法关联/测量的术语
    guided_by       method   → rule         方法判据引用的域规则

参数名 → 术语的匹配 (保守、确定性):

1. 名字先经 :func:`matrag.domain.vocab.normalize_name`; 空或
   :func:`matrag.domain.vocab.is_empty_value` 判定的占位符 (``-``/``/``/
   ``N/A`` …) **不做匹配** (记入 coverage 的 placeholder 计数);
2. 精确: 命中 :func:`matrag.domain.terms.build_term_index` 的键 → ``exact``;
3. 包含: 候选为全部术语名称 (zh/en/abbr/aliases) 的归一键中, 是参数名归一
   文本子串者; 取 ``(键长降序, 键字典序, term_id 字典序)`` 最优者 → ``contains``。
   **ASCII 边界保护**: 纯 ASCII 字母数字键 (如 ``re``) 的命中位置前后不得
   再贴 ASCII 字母数字, 否则该出现位置无效 —— 修掉 ``re`` 误命中 ``reset``
   内部这类已知误配;
4. ``name`` 不中且 ``sub_name`` 非空非占位时, 用同法再试 ``sub_name``
   (``field="sub_name"``);
5. 命中术语后展开 ``term.properties`` / ``term.related_rules``; 这些术语级
   关联**与参数是否命中无关**, 全量建边 (术语节点自己的边该有就有)。

质量门 :meth:`KnowledgeGraph.validate` (**空列表 = 通过**) 与覆盖率
:meth:`KnowledgeGraph.coverage` (**"结合实际规格书优化本体抽取"的反馈件**)
是本模块的两个出口: 前者查悬空边与断掉的引用 (缺属性/缺规则/缺类), 后者量化
"术语/属性在真实规格书上的落地程度", 直接回答"本体还缺哪些概念"。

确定性: 全部输出 (节点序、边序、coverage 的每张表) 都按稳定键排序, 无随机、
无未排序集合泄漏; 同一输入两次构建的 :meth:`KnowledgeGraph.to_json` 逐字节相同。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from matrag.domain.axioms import Axiom, load_axioms
from matrag.domain.ontology import DEFAULT_GENERATED_DIR, PowerOntology, build_ontology
from matrag.domain.spec_params import (
    SpecParameter,
    SpecStandard,
    extract_spec_parameters,
    extract_standards,
)
from matrag.domain.terms import Term, build_term_index, load_terms
from matrag.domain.test_methods import TestMethod, load_test_methods
from matrag.domain.vocab import CATEGORY_LABELS, is_empty_value, normalize_name

__all__ = [
    "DEFAULT_KG_FILENAME",
    "ISSUE_DANGLING_EDGE",
    "ISSUE_MISSING_CLASS",
    "ISSUE_MISSING_PROPERTY",
    "ISSUE_MISSING_RULE",
    "ISSUE_MISSING_TERM",
    "KINDS",
    "KIND_AXIOM",
    "KIND_CATEGORY",
    "KIND_CLASS",
    "KIND_METHOD",
    "KIND_PARAM",
    "KIND_PRODUCT",
    "KIND_PROPERTY",
    "KIND_RULE",
    "KIND_STANDARD",
    "KIND_TERM",
    "RELATIONS",
    "REL_APPLIES_TO",
    "REL_CONFORMS_TO",
    "REL_CONSUMES",
    "REL_DENOTES",
    "REL_GUIDED_BY",
    "REL_HAS_PARAMETER",
    "REL_HAS_PROPERTY",
    "REL_IN_CATEGORY",
    "REL_IN_CLASS",
    "REL_INSTANCE_OF",
    "REL_MEASURES",
    "REL_PRODUCES",
    "REL_RELATED_RULE",
    "KnowledgeGraph",
    "KgEdge",
    "KgIssue",
    "KgNode",
    "build_knowledge_graph",
    "write_knowledge_graph",
]

# --------------------------------------------------------------------------- #
# 节点 / 关系 / 问题 常量
# --------------------------------------------------------------------------- #

KIND_PRODUCT = "product"
KIND_PARAM = "param"
KIND_STANDARD = "standard"
KIND_TERM = "term"
KIND_RULE = "rule"
KIND_AXIOM = "axiom"
KIND_METHOD = "method"
KIND_CLASS = "class"
KIND_PROPERTY = "property"
KIND_CATEGORY = "category"

#: 节点类型闭集 (顺序固定, 供稳定遍历)。
KINDS: tuple[str, ...] = (
    KIND_PRODUCT,
    KIND_PARAM,
    KIND_STANDARD,
    KIND_TERM,
    KIND_RULE,
    KIND_AXIOM,
    KIND_METHOD,
    KIND_CLASS,
    KIND_PROPERTY,
    KIND_CATEGORY,
)

REL_HAS_PARAMETER = "has_parameter"
REL_CONFORMS_TO = "conforms_to"
REL_DENOTES = "denotes"
REL_INSTANCE_OF = "instance_of"
REL_IN_CATEGORY = "in_category"
REL_RELATED_RULE = "related_rule"
REL_HAS_PROPERTY = "has_property"
REL_CONSUMES = "consumes"
REL_PRODUCES = "produces"
REL_APPLIES_TO = "applies_to"
REL_IN_CLASS = "in_class"

#: 产测方法 → 术语 (方法关联/测量的术语, 即 ``TestMethod.measures``)。
REL_MEASURES = "measures"
#: 产测方法 → 规则 (方法判据引用的域规则, 即 ``TestMethod.rules``)。
REL_GUIDED_BY = "guided_by"

#: 关系闭集 (顺序固定, 供稳定遍历)。
RELATIONS: tuple[str, ...] = (
    REL_HAS_PARAMETER,
    REL_CONFORMS_TO,
    REL_DENOTES,
    REL_INSTANCE_OF,
    REL_IN_CATEGORY,
    REL_RELATED_RULE,
    REL_HAS_PROPERTY,
    REL_CONSUMES,
    REL_PRODUCES,
    REL_APPLIES_TO,
    REL_IN_CLASS,
    REL_MEASURES,
    REL_GUIDED_BY,
)

#: 质量门 issue 类型 (常量见模块顶部 ``ISSUE_*``)。
#: 边的某个端点节点不存在 (悬空边)。
ISSUE_DANGLING_EDGE = "dangling_edge"
#: 术语/公理声明的属性不在本体属性表里。
ISSUE_MISSING_PROPERTY = "missing_property"
#: 术语/公理声明的关联规则不在规则集里。
ISSUE_MISSING_RULE = "missing_rule"
#: 规则声明的被测对象类型不在本体 subject 轴类表里。
ISSUE_MISSING_CLASS = "missing_class"
#: 产测方法声明的关联术语不在图里的术语节点中。
ISSUE_MISSING_TERM = "missing_term"

#: 落盘文件名 (与 ``power_ontology.json`` 同目录同风格)。
DEFAULT_KG_FILENAME = "power_knowledge_graph.json"

#: 参数名 → 术语的匹配方式 (``denotes`` 边的 ``match`` 属性取值)。
_MATCH_EXACT = "exact"
_MATCH_CONTAINS = "contains"

#: 包含匹配键的最短长度 (单字符键如可用度 abbr ``A`` 只参与精确匹配)。
_MIN_CONTAINS_KEY_LEN = 2

#: 匹配发生在参数的哪个字段上 (``denotes`` 边的 ``field`` 属性取值)。
_FIELD_NAME = "name"
_FIELD_SUB_NAME = "sub_name"
_FIELD_SCOPE = "scope"

#: ``neighbors`` 允许的方向取值。
_DIRECTIONS: tuple[str, ...] = ("out", "in", "both")


# --------------------------------------------------------------------------- #
# 值对象
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class KgNode:
    """一个图谱节点。

    Attributes:
        node_id: 稳定 id, 形如 ``"param:CY0177-75V-20A|t1-0|3"`` (前缀即 kind)。
        kind: :data:`KINDS` 之一。
        label: 人类可读短标签 (术语取中文名、属性取参数名、类别取中文标签)。
        attrs: 结构化属性, **只含 JSON 安全值** (tuple 已转 list)。
    """

    node_id: str
    kind: str
    label: str
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict (``attrs`` 深拷贝, 调用方改不脏内部)。"""
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "label": self.label,
            "attrs": deepcopy(self.attrs),
        }


@dataclass(frozen=True)
class KgEdge:
    """一条有向关系边。

    Attributes:
        source: 起点 ``node_id``。
        relation: :data:`RELATIONS` 之一。
        target: 终点 ``node_id``。
        attrs: 边属性 (默认空 dict), 只含 JSON 安全值。
    """

    source: str
    relation: str
    target: str
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict (``attrs`` 深拷贝)。"""
        return {
            "source": self.source,
            "relation": self.relation,
            "target": self.target,
            "attrs": deepcopy(self.attrs),
        }


@dataclass(frozen=True)
class KgIssue:
    """一条图谱质量门问题 (类型见模块级 ``ISSUE_*`` 常量)。

    Attributes:
        kind: 问题类型。
        detail: 人类可读的中文说明 (含具体 id 与取值)。
        involved: 涉及的节点 id / 属性名 / 类名 (固定顺序, 便于断言)。
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


@dataclass(frozen=True)
class _TermMatch:
    """一次成功的"参数名 → 术语"匹配 (内部记录, 不对外导出)。"""

    term_id: str
    method: str
    key: str
    field: str


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def _node_id(kind: str, key: str) -> str:
    """拼节点 id (``kind:key``)。"""
    return f"{kind}:{key}"


def _json_safe(value: Any) -> Any:
    """把值递归转成 JSON 安全形式 (tuple/set → 稳定排序的 list, dict 递归)。

    set / frozenset 必须排序后再导出, 否则同一输入两次运行会因集合迭代顺序而
    产生不同字节 (确定性红线); tuple 保持原顺序 (调用方已保证有序)。
    """
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return [_json_safe(item) for item in sorted(value, key=str)]
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _edge_sort_key(edge: KgEdge) -> tuple[str, str, str]:
    """边的规范排序键 (同一输入 → 同一序)。"""
    return (edge.source, edge.relation, edge.target)


def _is_ascii_alnum(text: str) -> bool:
    """文本是否为**非空**的纯 ASCII 字母数字串。"""
    return bool(text) and text.isascii() and text.isalnum()


def _boundary_ok(text: str, key: str, pos: int) -> bool:
    """包含匹配的边界检查。

    纯 ASCII 字母数字键 (``re`` / ``n`` / ``s`` …) 只有出现在**词边界**上才算
    命中: 命中位置的前一个字符与后一个字符都不得是 ASCII 字母数字。
    中文键与含连接符的键 (``v_bus`` / ``y1/y2``) 不受此限制。
    """
    if not _is_ascii_alnum(key):
        return True
    before = text[pos - 1] if pos > 0 else ""
    after = text[pos + len(key)] if pos + len(key) < len(text) else ""
    return not _is_ascii_alnum(before) and not _is_ascii_alnum(after)


def _contains_hit(text: str, key: str) -> bool:
    """``key`` 是否在 ``text`` 中有**至少一个**合法出现位置 (含边界检查)。"""
    start = 0
    while True:
        pos = text.find(key, start)
        if pos < 0:
            return False
        if _boundary_ok(text, key, pos):
            return True
        start = pos + 1


# 公开别名 (P1-I8 跨模块 API 边界修复)：
# 同包内 _contains_hit 为内部契约；外部 (graph_index 等) 走此别名。
contains_hit = _contains_hit


def _term_match_keys(
    terms: Sequence[Term], index: dict[str, str]
) -> tuple[tuple[str, str], ...]:
    """全部可参与包含匹配的 ``(归一键, term_id)``, 按最优优先排序。

    排序键 ``(键长降序, 键字典序, term_id 字典序)`` —— 与匹配规则一致, 因此
    遍历时**第一个**有合法出现位置者即为最优候选。

    长度小于 :data:`_MIN_CONTAINS_KEY_LEN` 的键 (如可用度 abbr ``A``、电阻
    abbr ``R``) 只参与精确匹配, 不进入包含匹配 —— 否则任何含该字母的文本都
    会误命中。
    """
    pairs = {
        (normalize_name(name), term.term_id)
        for term in terms
        for name in term.all_names()
        if len(normalize_name(name)) >= _MIN_CONTAINS_KEY_LEN
    }
    return tuple(sorted(pairs, key=lambda item: (-len(item[0]), item[0], item[1])))


def _match_text(
    text: str, *, index: dict[str, str], keys: Sequence[tuple[str, str]]
) -> tuple[str, str, str] | None:
    """把一段文本匹配到术语; 返回 ``(term_id, method, key)`` 或 ``None``。"""
    normalized = normalize_name(text)
    if not normalized:
        return None
    exact = index.get(normalized)
    if exact is not None:
        return (exact, _MATCH_EXACT, normalized)
    for key, term_id in keys:
        if _contains_hit(normalized, key):
            return (term_id, _MATCH_CONTAINS, key)
    return None


def _is_placeholder(text: str) -> bool:
    """文本是否为空或占位符 (``-``/``/``/``N/A``/``无`` …)。"""
    return is_empty_value(text) or not normalize_name(text)


def _match_param(
    param: SpecParameter, *, index: dict[str, str], keys: Sequence[tuple[str, str]]
) -> _TermMatch | None:
    """把一条参数实例匹配到术语 (``name`` → ``sub_name`` → ``scope``)。

    占位符名字**不参与匹配** (直接回退下级), 否则 ``-`` 这类占位符会被当成
    普通文本进入包含匹配, 产生无意义的命中; ``scope`` 是抽取层记录的表级
    作用域 (要求型表的章节叶名, 如 "遥信功能"), 只在名字与子项都未命中时
    作为兜底锚点, 命中时 ``field`` 记为 ``scope``。
    """
    hit: tuple[str, str, str] | None = None
    field_name = _FIELD_NAME
    if not _is_placeholder(param.name):
        hit = _match_text(param.name, index=index, keys=keys)
    if hit is None and param.sub_name and not is_empty_value(param.sub_name):
        hit = _match_text(param.sub_name, index=index, keys=keys)
        if hit is not None:
            field_name = _FIELD_SUB_NAME
    if hit is None and param.scope and not is_empty_value(param.scope):
        hit = _match_text(param.scope, index=index, keys=keys)
        if hit is not None:
            field_name = _FIELD_SCOPE
    if hit is None:
        return None
    term_id, method, key = hit
    return _TermMatch(term_id=term_id, method=method, key=key, field=field_name)


def _param_label(param: SpecParameter) -> str:
    """参数节点的可读标签: 首个非占位名字 → ``sub_name`` → ``param_id``。"""
    for text in (param.name, param.sub_name):
        if text and not is_empty_value(text):
            return text
    return param.param_id


# --------------------------------------------------------------------------- #
# 图谱
# --------------------------------------------------------------------------- #


@dataclass(eq=False)
class KnowledgeGraph:
    """域知识图谱 (构建后按只读使用)。

    ``eq=False`` 的原因与 :class:`matrag.domain.ontology.PowerOntology` 一致:
    内部是 dict, 对象相等没有意义; "确定性"应由 :meth:`to_json` 的逐位输出判断。

    Attributes:
        nodes_by_id: ``node_id`` → :class:`KgNode`。
        edges: 全部关系边 (**规范排序**, 同一输入逐位一致)。
        terms / axioms / ontology / methods: 构建期源数据引用, 供
            :meth:`validate` 做"引用是否断掉"的检查与调用方回溯;
            直接构造 (不传) 时为空。
    """

    nodes_by_id: dict[str, KgNode]
    edges: tuple[KgEdge, ...]
    terms: tuple[Term, ...] = field(default=(), repr=False, compare=False)
    axioms: tuple[Axiom, ...] = field(default=(), repr=False, compare=False)
    ontology: PowerOntology | None = field(default=None, repr=False, compare=False)
    methods: tuple[TestMethod, ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        """把 ``edges`` 规整成 tuple 并校验元素类型。

        直接构造时最常见的错误是把**单条边**或 list 传进来: list 顺手规整成
        tuple, 单条边则立刻抛 (fail-fast, 否则错误会拖到某个遍历深处才炸)。
        """
        if isinstance(self.edges, KgEdge):
            raise TypeError("edges 应为 KgEdge 序列 (单条边也要写成 (edge,))")
        edges = tuple(self.edges)
        for edge in edges:
            if not isinstance(edge, KgEdge):
                raise TypeError(f"edges 含非 KgEdge 元素: {edge!r}")
        self.edges = edges

    # ------------------------------------------------------------ 查询 -- #

    def nodes(self, kind: str | None = None) -> list[KgNode]:
        """全部节点 (按 ``node_id`` 排序); ``kind`` 给定时只返回该类型。

        未知 ``kind`` 返回空列表 —— 这是"查询无结果"的正常语义。
        """
        items = [
            node
            for node in self.nodes_by_id.values()
            if kind is None or node.kind == kind
        ]
        items.sort(key=lambda node: node.node_id)
        return items

    def get(self, node_id: str) -> KgNode | None:
        """按 id 取节点; 不存在返回 ``None``。"""
        return self.nodes_by_id.get(node_id)

    def edges_out(self, node_id: str, relation: str | None = None) -> list[KgEdge]:
        """以 ``node_id`` 为**起点**的边 (可再按 relation 过滤, 保持规范序)。

        未知 ``node_id`` 返回空列表 (查询语义, 不是错误)。
        """
        return [
            edge
            for edge in self.edges
            if edge.source == node_id
            and (relation is None or edge.relation == relation)
        ]

    def edges_in(self, node_id: str, relation: str | None = None) -> list[KgEdge]:
        """以 ``node_id`` 为**终点**的边 (可再按 relation 过滤, 保持规范序)。"""
        return [
            edge
            for edge in self.edges
            if edge.target == node_id
            and (relation is None or edge.relation == relation)
        ]

    def neighbors(
        self,
        node_id: str,
        relation: str | None = None,
        direction: str = "out",
    ) -> list[KgEdge]:
        """与 ``node_id`` 相邻的关系边 (返回**边**, 相邻节点取 ``source``/``target``)。

        Args:
            node_id: 节点 id。
            relation: 只看某一类关系; ``None`` 表示全部。
            direction: ``"out"`` (出边) / ``"in"`` (入边) / ``"both"`` (两者并集)。

        Raises:
            ValueError: ``direction`` 不在 ``("out", "in", "both")`` 内 (fail-fast,
                拼错方向静默返回空集会让调用方误判"没有邻居")。
        """
        if direction not in _DIRECTIONS:
            raise ValueError(
                f"direction 必须是 {list(_DIRECTIONS)} 之一, 实际是 {direction!r}"
            )
        if direction == "out":
            return self.edges_out(node_id, relation)
        if direction == "in":
            return self.edges_in(node_id, relation)
        merged: dict[tuple[str, str, str], KgEdge] = {}
        for edge in (*self.edges_out(node_id, relation), *self.edges_in(node_id, relation)):
            merged.setdefault(_edge_sort_key(edge), edge)
        return [merged[key] for key in sorted(merged)]

    # -------------------------------------------------------- 统计/导出 -- #

    def stats(self) -> dict[str, Any]:
        """统计摘要 (纯数据, 无时间戳, 确定性)。

        Returns:
            ``{"nodes": {kind: 计数}, "edges": {relation: 计数},
            "totals": {"nodes": n, "edges": m}}``; 计数表**只含出现过的**
            kind / relation (缺失即 0, 不写占位), 键已排序。
        """
        by_kind: dict[str, int] = {}
        for node in self.nodes_by_id.values():
            by_kind[node.kind] = by_kind.get(node.kind, 0) + 1
        by_relation: dict[str, int] = {}
        for edge in self.edges:
            by_relation[edge.relation] = by_relation.get(edge.relation, 0) + 1
        return {
            "nodes": {key: by_kind[key] for key in sorted(by_kind)},
            "edges": {key: by_relation[key] for key in sorted(by_relation)},
            "totals": {"nodes": len(self.nodes_by_id), "edges": len(self.edges)},
        }

    def coverage(self) -> dict[str, Any]:
        """覆盖率报告 —— "结合实际规格书优化本体抽取"的反馈件。

        全部数值直接从图结构导出 (与 :meth:`nodes` / ``edges`` 一致, 不另算一套),
        可 JSON 序列化且确定性排序。字段含义:

        - ``params_total`` / ``params_with_term`` / ``params_with_property``:
          参数总数 / 至少命中一个术语的 / 至少挂上一个本体属性的;
        - ``params_placeholder``: 名字是空或占位符 (``-``/``/``/``N/A`` …) 的
          参数数 (它们天然无法按名字匹配, 是抽取层的已知形态);
        - ``params_unmatched``: 没有任何 ``denotes`` 边的参数数;
        - ``unmatched_by_category``: 未命中参数的类别分布;
        - ``unmatched_params``: 未命中参数清单 (归一后按名排序), 每条含
          ``product_code`` / ``name`` / ``category``;
        - ``products``: 产品 → ``{"params", "with_term", "with_property"}``;
        - ``properties_total`` / ``properties_with_spec_evidence``: 本体属性总数 /
          被至少一条规格书参数落地的;
        - ``properties_without_spec_evidence``: 没有规格书证据的属性名 (已排序) ——
          这份清单就是"本体里有、真实规格书里没抽到"的概念缺口;
        - ``rules_reachable_from_products``: 经
          ``product → param → term → rule`` 可达的规则数;
        - ``methods_total`` / ``methods_with_term`` / ``methods_with_rule``:
          产测方法总数 / 至少挂上一个术语 (``measures``) 的 / 至少挂上一条规则
          (``guided_by``) 的。
        """
        param_nodes = self.nodes(KIND_PARAM)
        property_nodes = self.nodes(KIND_PROPERTY)

        params_with_term: set[str] = set()
        params_with_property: set[str] = set()
        properties_with_evidence: set[str] = set()
        product_by_param: dict[str, str] = {}
        term_by_param: dict[str, str] = {}
        rules_by_term: dict[str, set[str]] = {}

        for edge in self.edges:
            if edge.relation == REL_HAS_PARAMETER:
                product_by_param[edge.target] = edge.source
            elif edge.relation == REL_DENOTES:
                params_with_term.add(edge.source)
                term_by_param[edge.source] = edge.target
            elif edge.relation == REL_INSTANCE_OF:
                params_with_property.add(edge.source)
                properties_with_evidence.add(edge.target)
            elif edge.relation == REL_RELATED_RULE:
                rules_by_term.setdefault(edge.source, set()).add(edge.target)

        product_nodes = self.nodes(KIND_PRODUCT)
        product_counts: dict[str, dict[str, int]] = {
            node.node_id: {"params": 0, "with_term": 0, "with_property": 0}
            for node in product_nodes
        }
        product_codes = {
            node.node_id: str(node.attrs.get("product_code", node.label))
            for node in product_nodes
        }
        for param_id, product_id in product_by_param.items():
            bucket = product_counts.get(product_id)
            if bucket is None:
                continue
            bucket["params"] += 1
            if param_id in params_with_term:
                bucket["with_term"] += 1
            if param_id in params_with_property:
                bucket["with_property"] += 1

        products: dict[str, dict[str, int]] = {}
        for product_id in sorted(product_counts, key=lambda key: (product_codes[key], key)):
            code = product_codes[product_id]
            if code in products:
                raise ValueError(
                    f"coverage: product_code 重复 {code!r} "
                    f"({product_id} 与已有产品节点), 拒绝静默覆盖"
                )
            products[code] = {
                "params": product_counts[product_id]["params"],
                "with_term": product_counts[product_id]["with_term"],
                "with_property": product_counts[product_id]["with_property"],
            }

        unmatched: list[dict[str, Any]] = []
        placeholder = 0
        for node in param_nodes:
            name = str(node.attrs.get("name", ""))
            if _is_placeholder(name):
                placeholder += 1
            if node.node_id in params_with_term:
                continue
            unmatched.append(
                {
                    "product_code": str(node.attrs.get("product_code", "")),
                    "name": name,
                    "category": str(node.attrs.get("category", "")),
                }
            )
        unmatched.sort(
            key=lambda item: (
                normalize_name(item["name"]),
                item["product_code"],
                item["name"],
            )
        )

        by_category: dict[str, int] = {}
        for item in unmatched:
            by_category[item["category"]] = by_category.get(item["category"], 0) + 1

        reachable_terms = {
            term_id
            for param_id, term_id in term_by_param.items()
            if param_id in product_by_param
        }
        reachable_rules: set[str] = set()
        for term_id in reachable_terms:
            reachable_rules.update(rules_by_term.get(term_id, ()))

        without_evidence = sorted(
            node.label
            for node in property_nodes
            if node.node_id not in properties_with_evidence
        )

        method_nodes = self.nodes(KIND_METHOD)
        methods_with_term = {
            edge.source for edge in self.edges if edge.relation == REL_MEASURES
        }
        methods_with_rule = {
            edge.source for edge in self.edges if edge.relation == REL_GUIDED_BY
        }
        return {
            "params_total": len(param_nodes),
            "params_with_term": len(params_with_term),
            "params_with_property": len(params_with_property),
            "params_placeholder": placeholder,
            "params_unmatched": len(unmatched),
            "unmatched_by_category": {
                key: by_category[key] for key in sorted(by_category)
            },
            "unmatched_params": unmatched,
            "products": products,
            "properties_total": len(property_nodes),
            "properties_with_spec_evidence": len(properties_with_evidence),
            "properties_without_spec_evidence": without_evidence,
            "rules_reachable_from_products": len(reachable_rules),
            "methods_total": len(method_nodes),
            "methods_with_term": len(methods_with_term),
            "methods_with_rule": len(methods_with_rule),
        }

    # ---------------------------------------------------------- 质量门 -- #

    def validate(self) -> list[KgIssue]:
        """图谱质量门: **空列表 = 通过**。

        检出的类型 (常量见模块顶部):

        - :data:`ISSUE_DANGLING_EDGE` —— 边的 ``source``/``target`` 不是本图节点;
        - :data:`ISSUE_MISSING_PROPERTY` —— 术语/公理声明的属性不在属性节点里;
        - :data:`ISSUE_MISSING_TERM` —— 产测方法 ``measures`` 里的术语不在术语
          节点里;
        - :data:`ISSUE_MISSING_RULE` —— 术语/公理/产测方法声明的规则不在规则
          节点里;
        - :data:`ISSUE_MISSING_CLASS` —— 规则声明的 ``class_names`` 不在 class
          节点里 (类别轴类名不建 class 节点, 因此拼错名或用错轴都会被抓到)。

        Returns:
            issue 列表, 按 ``(kind, detail, involved)`` 排序保证确定性。
        """
        issues: list[KgIssue] = []
        known = set(self.nodes_by_id)

        for edge in self.edges:
            missing = sorted(
                endpoint
                for endpoint in (edge.source, edge.target)
                if endpoint not in known
            )
            if missing:
                issues.append(
                    KgIssue(
                        kind=ISSUE_DANGLING_EDGE,
                        detail=(
                            f"关系 {edge.source} -[{edge.relation}]-> {edge.target} "
                            f"的端点节点不存在: {missing}"
                        ),
                        involved=tuple(missing),
                    )
                )

        issues.extend(self._validate_references())
        issues.sort(key=lambda issue: (issue.kind, issue.detail, issue.involved))
        return issues

    def _validate_references(self) -> list[KgIssue]:
        """检查术语/公理/产测方法/规则对属性、术语、规则、类的引用是否都落到了节点上。

        检查一律以**图里的节点**为唯一事实来源 (不另查本体表), 这样
        :meth:`validate` 的结论与 ``edges`` 完全一致: 引用能连上边就是好, 连不上
        就必须报出来, 不存在"表里有但图里没有"的灰色地带。
        """
        issues: list[KgIssue] = []
        for term in self.terms:
            issues.extend(
                self._reference_issues(
                    owner_id=term.term_id,
                    owner_kind="术语",
                    properties=term.properties,
                    related_rules=term.related_rules,
                )
            )
        for axiom in self.axioms:
            issues.extend(
                self._reference_issues(
                    owner_id=axiom.axiom_id,
                    owner_kind="公理",
                    properties=axiom.properties,
                    related_rules=axiom.related_rules,
                )
            )
        for method in self.methods:
            for term_id in method.measures:
                if _node_id(KIND_TERM, term_id) in self.nodes_by_id:
                    continue
                issues.append(
                    KgIssue(
                        kind=ISSUE_MISSING_TERM,
                        detail=(
                            f"产测方法 {method.method_id} 的 measures 引用了不存在"
                            f"的术语 {term_id!r}"
                        ),
                        involved=(method.method_id, term_id),
                    )
                )
            for rule_id in method.rules:
                if _node_id(KIND_RULE, rule_id) in self.nodes_by_id:
                    continue
                issues.append(
                    KgIssue(
                        kind=ISSUE_MISSING_RULE,
                        detail=(
                            f"产测方法 {method.method_id} 的 rules 引用了不存在的"
                            f"域规则 {rule_id!r}"
                        ),
                        involved=(method.method_id, rule_id),
                    )
                )
        if self.ontology is not None:
            for rule_id in sorted(self.ontology.links_by_rule):
                link = self.ontology.links_by_rule[rule_id]
                for class_name in link.class_names:
                    if _node_id(KIND_CLASS, class_name) in self.nodes_by_id:
                        continue
                    issues.append(
                        KgIssue(
                            kind=ISSUE_MISSING_CLASS,
                            detail=(
                                f"规则 {rule_id} 的 class_names 引用了不存在的被测"
                                f"对象类型 {class_name!r} (class 节点里没有; "
                                f"类别轴类名不会建 class 节点)"
                            ),
                            involved=(rule_id, class_name),
                        )
                    )
        return issues

    def _reference_issues(
        self,
        *,
        owner_id: str,
        owner_kind: str,
        properties: Sequence[str],
        related_rules: Sequence[str],
    ) -> list[KgIssue]:
        """单个术语/公理的引用检查 (缺属性 / 缺规则)。"""
        issues: list[KgIssue] = []
        for name in properties:
            if _node_id(KIND_PROPERTY, name) in self.nodes_by_id:
                continue
            issues.append(
                KgIssue(
                    kind=ISSUE_MISSING_PROPERTY,
                    detail=(
                        f"{owner_kind} {owner_id} 的 properties 引用了不存在的"
                        f"本体属性 {name!r}"
                    ),
                    involved=(owner_id, name),
                )
            )
        for rule_id in related_rules:
            if _node_id(KIND_RULE, rule_id) in self.nodes_by_id:
                continue
            issues.append(
                KgIssue(
                    kind=ISSUE_MISSING_RULE,
                    detail=(
                        f"{owner_kind} {owner_id} 的 related_rules 引用了不存在的"
                        f"域规则 {rule_id!r}"
                    ),
                    involved=(owner_id, rule_id),
                )
            )
        return issues

    # ------------------------------------------------------------ 导出 -- #

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict (节点按 id、边按规范键排序)。"""
        return {
            "nodes": [node.to_dict() for node in self.nodes()],
            "edges": [edge.to_dict() for edge in self.edges],
        }

    def to_json(self, indent: int = 2) -> str:
        """导出为确定性的 JSON 文本 (``sort_keys=True``, UTF-8 原样输出)。"""
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
        )


# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #


def build_knowledge_graph(
    *,
    ontology: PowerOntology | None = None,
    terms: Sequence[Term] | None = None,
    axioms: Sequence[Axiom] | None = None,
    methods: Sequence[TestMethod] | None = None,
    params: Sequence[SpecParameter] | None = None,
    standards: Sequence[SpecStandard] | None = None,
) -> KnowledgeGraph:
    """把五层数据连成域知识图谱 (纯数据驱动, 无任何按 id 的分支)。

    Args:
        ontology: 电力域本体; ``None`` 表示 :func:`matrag.domain.ontology.build_ontology`。
        terms: 术语表; ``None`` 表示 :func:`matrag.domain.terms.load_terms`。
        axioms: 公理表; ``None`` 表示 :func:`matrag.domain.axioms.load_axioms`。
        methods: 产测方法表; ``None`` 表示
            :func:`matrag.domain.test_methods.load_test_methods`。
        params: 规格书参数实例; ``None`` 表示
            :func:`matrag.domain.spec_params.extract_spec_parameters`。
        standards: 规格书标准引用; ``None`` 表示
            :func:`matrag.domain.spec_params.extract_standards`。

    Returns:
        构造完成的 :class:`KnowledgeGraph` (节点/边均已规范排序)。

    Raises:
        ValueError: 两个节点算出同一个 ``node_id`` (数据错误, 绝不静默覆盖)。
        matrag.domain.ontology.OntologyBuildError: ``ontology=None`` 且规则集为空
            (由 :func:`matrag.domain.ontology.build_ontology` 抛出)。
    """
    onto = ontology if ontology is not None else build_ontology()
    term_list = tuple(terms) if terms is not None else tuple(load_terms())
    axiom_list = tuple(axioms) if axioms is not None else tuple(load_axioms())
    param_list = tuple(params) if params is not None else tuple(extract_spec_parameters())
    standard_list = (
        tuple(standards) if standards is not None else tuple(extract_standards())
    )
    method_list = tuple(methods) if methods is not None else tuple(load_test_methods())

    nodes: dict[str, KgNode] = {}
    edges: list[KgEdge] = []

    def add_node(node: KgNode) -> None:
        """登记节点; 重复 id 直接抛 (fail-fast, 不许后者覆盖前者)。"""
        existing = nodes.get(node.node_id)
        if existing is not None:
            raise ValueError(
                f"节点 id 重复: {node.node_id!r} "
                f"({existing.kind}/{node.kind}, label={existing.label!r}/{node.label!r})"
            )
        nodes[node.node_id] = node

    property_names = set(onto.properties())
    rule_ids = set(onto.rules())
    subject_class_names = set(onto.subject_classes())
    subject_classes = onto.subject_classes()
    properties = onto.properties()
    terms_by_id = {term.term_id: term for term in term_list}

    # --- 1. 节点: 产品 / 参数 / 标准 ---------------------------------------- #
    product_codes = {param.product_code for param in param_list}
    product_codes.update(standard.product_code for standard in standard_list)
    doc_ids_by_product: dict[str, set[str]] = {code: set() for code in product_codes}
    param_count_by_product: dict[str, int] = {code: 0 for code in product_codes}
    for param in param_list:
        doc_ids_by_product[param.product_code].add(param.doc_id)
        param_count_by_product[param.product_code] += 1
    for standard in standard_list:
        doc_ids_by_product[standard.product_code].add(standard.doc_id)

    for code in sorted(product_codes):
        add_node(
            KgNode(
                node_id=_node_id(KIND_PRODUCT, code),
                kind=KIND_PRODUCT,
                label=code,
                attrs={
                    "product_code": code,
                    "doc_ids": sorted(doc_ids_by_product[code]),
                    "param_count": param_count_by_product[code],
                },
            )
        )

    for param in param_list:
        attrs: dict[str, Any] = {
            "product_code": param.product_code,
            "doc_id": param.doc_id,
            "section_path": param.section_path,
            "table_id": param.table_id,
            "row_index": param.row_index,
            "name": param.name,
            "sub_name": param.sub_name,
            "unit": param.unit,
            "minimum": param.minimum,
            "typical": param.typical,
            "maximum": param.maximum,
            "category": param.category,
            "flags": list(param.flags),
        }
        if param.condition:
            attrs["condition"] = param.condition
        if param.requirement:
            attrs["requirement"] = param.requirement
        if param.scope:
            attrs["scope"] = param.scope
        add_node(
            KgNode(
                node_id=_node_id(KIND_PARAM, param.param_id),
                kind=KIND_PARAM,
                label=_param_label(param),
                attrs=attrs,
            )
        )
        edges.append(
            KgEdge(
                source=_node_id(KIND_PRODUCT, param.product_code),
                relation=REL_HAS_PARAMETER,
                target=_node_id(KIND_PARAM, param.param_id),
            )
        )

    for standard in standard_list:
        add_node(
            KgNode(
                node_id=_node_id(KIND_STANDARD, standard.standard_id),
                kind=KIND_STANDARD,
                label=standard.code or standard.name or standard.standard_id,
                attrs={
                    "product_code": standard.product_code,
                    "doc_id": standard.doc_id,
                    "code": standard.code,
                    "name": standard.name,
                    "section_path": standard.section_path,
                    "table_id": standard.table_id,
                },
            )
        )
        edges.append(
            KgEdge(
                source=_node_id(KIND_PRODUCT, standard.product_code),
                relation=REL_CONFORMS_TO,
                target=_node_id(KIND_STANDARD, standard.standard_id),
            )
        )

    # --- 2. 节点: 术语 / 规则 / 公理 ---------------------------------------- #
    for term in term_list:
        add_node(
            KgNode(
                node_id=_node_id(KIND_TERM, term.term_id),
                kind=KIND_TERM,
                label=term.zh,
                attrs={
                    "zh": term.zh,
                    "en": term.en,
                    "abbr": term.abbr,
                    "category": term.category,
                    "definition": term.definition,
                    "aliases": list(term.aliases),
                    "properties": list(term.properties),
                    "related_rules": list(term.related_rules),
                    "source": _json_safe(term.source),
                },
            )
        )
        term_node = _node_id(KIND_TERM, term.term_id)
        for name in term.properties:
            if name not in property_names:
                continue
            edges.append(
                KgEdge(
                    source=term_node,
                    relation=REL_HAS_PROPERTY,
                    target=_node_id(KIND_PROPERTY, name),
                )
            )
        for rule_id in term.related_rules:
            if rule_id not in rule_ids:
                continue
            edges.append(
                KgEdge(
                    source=term_node,
                    relation=REL_RELATED_RULE,
                    target=_node_id(KIND_RULE, rule_id),
                )
            )

    for rule_id in sorted(onto.links_by_rule):
        link = onto.links_by_rule[rule_id]
        add_node(
            KgNode(
                node_id=_node_id(KIND_RULE, rule_id),
                kind=KIND_RULE,
                label=rule_id,
                attrs={
                    "kind": link.kind,
                    "category": link.category,
                    "scope": link.scope,
                    "statement": link.statement,
                    "class_names": list(link.class_names),
                    "consumes": list(link.consumes),
                    "produces": list(link.produces),
                },
            )
        )
        rule_node = _node_id(KIND_RULE, rule_id)
        for name in link.consumes:
            if name not in property_names:
                continue
            edges.append(
                KgEdge(
                    source=rule_node,
                    relation=REL_CONSUMES,
                    target=_node_id(KIND_PROPERTY, name),
                )
            )
        for name in link.produces:
            # 约束型产出的 ``violation`` 不是量纲属性, 天然不在属性表里,
            # 跳过是正常路径 (不算问题, 也不会产生悬空边)。
            if name not in property_names:
                continue
            edges.append(
                KgEdge(
                    source=rule_node,
                    relation=REL_PRODUCES,
                    target=_node_id(KIND_PROPERTY, name),
                )
            )
        for class_name in link.class_names:
            if class_name not in subject_class_names:
                continue
            edges.append(
                KgEdge(
                    source=rule_node,
                    relation=REL_APPLIES_TO,
                    target=_node_id(KIND_CLASS, class_name),
                )
            )

    for axiom in axiom_list:
        add_node(
            KgNode(
                node_id=_node_id(KIND_AXIOM, axiom.axiom_id),
                kind=KIND_AXIOM,
                label=axiom.statement,
                attrs={
                    "kind": axiom.kind,
                    "statement": axiom.statement,
                    "expr": axiom.expr,
                    "variables": _json_safe(axiom.variables),
                    "category": axiom.category,
                    "aliases": list(axiom.aliases),
                    "properties": list(axiom.properties),
                    "related_rules": list(axiom.related_rules),
                    "source": _json_safe(axiom.source),
                },
            )
        )
        axiom_node = _node_id(KIND_AXIOM, axiom.axiom_id)
        for name in axiom.properties:
            if name not in property_names:
                continue
            edges.append(
                KgEdge(
                    source=axiom_node,
                    relation=REL_HAS_PROPERTY,
                    target=_node_id(KIND_PROPERTY, name),
                )
            )
        for rule_id in axiom.related_rules:
            if rule_id not in rule_ids:
                continue
            edges.append(
                KgEdge(
                    source=axiom_node,
                    relation=REL_RELATED_RULE,
                    target=_node_id(KIND_RULE, rule_id),
                )
            )

    # --- 2b. 节点: 产测方法 (measures / guided_by 链接) --------------------- #
    for method in method_list:
        add_node(
            KgNode(
                node_id=_node_id(KIND_METHOD, method.method_id),
                kind=KIND_METHOD,
                label=method.zh,
                attrs={
                    "zh": method.zh,
                    "en": method.en,
                    "aliases": list(method.aliases),
                    "category": method.category,
                    "definition": method.definition,
                    "parameters": list(method.parameters),
                    "instruments": list(method.instruments),
                    "interfaces": list(method.interfaces),
                    "metrics": list(method.metrics),
                    "procedure": list(method.procedure),
                    "criteria": list(method.criteria),
                    "measures": list(method.measures),
                    "rules": list(method.rules),
                    "standards": list(method.standards),
                    "source": {
                        "kind": "card",
                        "name": method.zh,
                        "card_id": method.card_id,
                        "url": "",
                        "retrieved": method.retrieved,
                    },
                },
            )
        )
        method_node = _node_id(KIND_METHOD, method.method_id)
        for term_id in method.measures:
            if term_id not in terms_by_id:
                continue
            edges.append(
                KgEdge(
                    source=method_node,
                    relation=REL_MEASURES,
                    target=_node_id(KIND_TERM, term_id),
                )
            )
        for rule_id in method.rules:
            if rule_id not in rule_ids:
                continue
            edges.append(
                KgEdge(
                    source=method_node,
                    relation=REL_GUIDED_BY,
                    target=_node_id(KIND_RULE, rule_id),
                )
            )

    # --- 3. 节点: 被测对象类型 / 属性 --------------------------------------- #
    for name in sorted(subject_classes):
        cls = subject_classes[name]
        add_node(
            KgNode(
                node_id=_node_id(KIND_CLASS, name),
                kind=KIND_CLASS,
                label=name,
                attrs={
                    "axis": cls.axis,
                    "comment": cls.comment,
                    "rule_ids": list(cls.rule_ids),
                },
            )
        )

    for name in sorted(properties):
        prop = properties[name]
        add_node(
            KgNode(
                node_id=_node_id(KIND_PROPERTY, name),
                kind=KIND_PROPERTY,
                label=name,
                attrs={
                    "unit": prop.unit,
                    "units": list(prop.units),
                    "origins": list(prop.origins),
                    "declared_names": list(prop.declared_names),
                    "range_iri": prop.range_iri,
                    "domain_classes": list(prop.domain_classes),
                    "categories": list(prop.categories),
                    "rule_ids": list(prop.rule_ids),
                },
            )
        )
        prop_node = _node_id(KIND_PROPERTY, name)
        for class_name in prop.domain_classes:
            if class_name not in subject_class_names:
                continue
            edges.append(
                KgEdge(
                    source=prop_node,
                    relation=REL_IN_CLASS,
                    target=_node_id(KIND_CLASS, class_name),
                )
            )

    # --- 4. 节点: 知识主题 (category) --------------------------------------- #
    categories: set[str] = set()
    for param in param_list:
        if param.category:
            categories.add(param.category)
    for term in term_list:
        if term.category:
            categories.add(term.category)
    for axiom in axiom_list:
        if axiom.category:
            categories.add(axiom.category)
    for link in onto.links_by_rule.values():
        if link.category:
            categories.add(link.category)
    for prop in properties.values():
        categories.update(name for name in prop.categories if name)

    for name in sorted(categories):
        add_node(
            KgNode(
                node_id=_node_id(KIND_CATEGORY, name),
                kind=KIND_CATEGORY,
                label=CATEGORY_LABELS.get(name, name),
                attrs={"label": CATEGORY_LABELS.get(name, name)},
            )
        )

    # --- 5. 边: 参数 → 术语 / 属性, 以及全部 in_category -------------------- #
    term_index = build_term_index(term_list)
    match_keys = _term_match_keys(term_list, term_index)

    for param in param_list:
        param_node = _node_id(KIND_PARAM, param.param_id)
        if param.category:
            edges.append(
                KgEdge(
                    source=param_node,
                    relation=REL_IN_CATEGORY,
                    target=_node_id(KIND_CATEGORY, param.category),
                )
            )
        match = _match_param(param, index=term_index, keys=match_keys)
        if match is None:
            continue
        term_node = _node_id(KIND_TERM, match.term_id)
        edges.append(
            KgEdge(
                source=param_node,
                relation=REL_DENOTES,
                target=term_node,
                attrs={
                    "match": match.method,
                    "key": match.key,
                    "field": match.field,
                },
            )
        )
        term = terms_by_id[match.term_id]
        for name in term.properties:
            if name not in property_names:
                continue
            edges.append(
                KgEdge(
                    source=param_node,
                    relation=REL_INSTANCE_OF,
                    target=_node_id(KIND_PROPERTY, name),
                    attrs={
                        "via_term": match.term_id,
                        "unit": param.unit,
                        "minimum": param.minimum,
                        "typical": param.typical,
                        "maximum": param.maximum,
                    },
                )
            )

    for term in term_list:
        if not term.category:
            continue
        edges.append(
            KgEdge(
                source=_node_id(KIND_TERM, term.term_id),
                relation=REL_IN_CATEGORY,
                target=_node_id(KIND_CATEGORY, term.category),
            )
        )
    for axiom in axiom_list:
        if not axiom.category:
            continue
        edges.append(
            KgEdge(
                source=_node_id(KIND_AXIOM, axiom.axiom_id),
                relation=REL_IN_CATEGORY,
                target=_node_id(KIND_CATEGORY, axiom.category),
            )
        )
    for rule_id in sorted(onto.links_by_rule):
        category = onto.links_by_rule[rule_id].category
        if not category:
            continue
        edges.append(
            KgEdge(
                source=_node_id(KIND_RULE, rule_id),
                relation=REL_IN_CATEGORY,
                target=_node_id(KIND_CATEGORY, category),
            )
        )
    for name in sorted(properties):
        prop_node = _node_id(KIND_PROPERTY, name)
        for category in properties[name].categories:
            if not category:
                continue
            edges.append(
                KgEdge(
                    source=prop_node,
                    relation=REL_IN_CATEGORY,
                    target=_node_id(KIND_CATEGORY, category),
                )
            )

    return KnowledgeGraph(
        nodes_by_id=nodes,
        edges=tuple(sorted(edges, key=_edge_sort_key)),
        terms=term_list,
        axioms=axiom_list,
        methods=method_list,
        ontology=onto,
    )


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #


def write_knowledge_graph(
    out_dir: Path | str | None = None,
    *,
    ontology: PowerOntology | None = None,
    terms: Sequence[Term] | None = None,
    axioms: Sequence[Axiom] | None = None,
    methods: Sequence[TestMethod] | None = None,
    params: Sequence[SpecParameter] | None = None,
    standards: Sequence[SpecStandard] | None = None,
) -> dict[str, Any]:
    """把知识图谱写成 ``power_knowledge_graph.json``。

    文件内容为 ``{"stats", "coverage", "nodes", "edges"}`` (确定性: 与
    :meth:`KnowledgeGraph.to_json` 同一套排序与 JSON 参数)。

    Args:
        out_dir: 输出目录; 默认 :data:`matrag.domain.ontology.DEFAULT_GENERATED_DIR`
            (``data/domain/generated``, 与本体产物同址)。
        ontology / terms / axioms / methods / params / standards: 同
            :func:`build_knowledge_graph`。

    Returns:
        ``{"json": 路径, "out_dir": 目录, "stats": ..., "coverage": ...}``,
        供 CLI 打印与测试断言。
    """
    graph = build_knowledge_graph(
        ontology=ontology,
        terms=terms,
        axioms=axioms,
        methods=methods,
        params=params,
        standards=standards,
    )
    target_dir = Path(out_dir) if out_dir is not None else DEFAULT_GENERATED_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    export = graph.to_dict()
    stats = graph.stats()
    coverage = graph.coverage()
    payload = {
        "stats": stats,
        "coverage": coverage,
        "nodes": export["nodes"],
        "edges": export["edges"],
    }
    json_path = target_dir / DEFAULT_KG_FILENAME
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return {
        "json": str(json_path),
        "out_dir": str(target_dir),
        "stats": stats,
        "coverage": coverage,
    }
