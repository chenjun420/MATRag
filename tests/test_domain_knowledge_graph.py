"""``matrag.domain.knowledge_graph`` 的离线测试。

四条主线:

1. **合成小样本** —— 直接构造 ``Term`` / ``Axiom`` / ``OntologyClass`` /
   ``OntologyProperty`` / ``OntologyRuleLink`` / ``PowerOntology`` /
   ``SpecParameter`` / ``SpecStandard`` 实例, 逐个验证匹配规则 (精确/包含/
   ASCII 边界保护/name→sub_name→scope 回退/单字符键只精确匹配/占位符跳过)、
   边语义、质量门与覆盖率计数, **不靠真实数据碰巧触发**;
2. **真实数据 smoke** —— 三份规格书 + 168 术语 + 76 公理 + 132 规则 + 35 产测方法构建的图:
   节点/边数、0 issue、已知命中样例 (数字按 2026-10 实测写死);
3. **确定性** —— 两次构建的 ``to_json()`` 逐字节相等;
4. **落盘** —— ``write_knowledge_graph`` 产物可读回、stats 与内存图一致。

全部离线: 不涉及 LLM / 网络。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from matrag.domain.axioms import Axiom
from matrag.domain.knowledge_graph import (
    DEFAULT_KG_FILENAME,
    ISSUE_DANGLING_EDGE,
    ISSUE_MISSING_CLASS,
    ISSUE_MISSING_PROPERTY,
    ISSUE_MISSING_RULE,
    ISSUE_MISSING_TERM,
    KIND_AXIOM,
    KIND_CATEGORY,
    KIND_CLASS,
    KIND_METHOD,
    KIND_PARAM,
    KIND_PRODUCT,
    KIND_PROPERTY,
    KIND_RULE,
    KIND_STANDARD,
    KIND_TERM,
    REL_APPLIES_TO,
    REL_CONFORMS_TO,
    REL_CONSUMES,
    REL_DENOTES,
    REL_GUIDED_BY,
    REL_HAS_PARAMETER,
    REL_HAS_PROPERTY,
    REL_IN_CATEGORY,
    REL_IN_CLASS,
    REL_INSTANCE_OF,
    REL_MEASURES,
    REL_PRODUCES,
    REL_RELATED_RULE,
    KgEdge,
    KgNode,
    KnowledgeGraph,
    build_knowledge_graph,
    write_knowledge_graph,
)
from matrag.domain.ontology import (
    AXIS_CATEGORY,
    AXIS_SUBJECT,
    CATEGORY_NS,
    DEFAULT_GENERATED_DIR,
    ORIGIN_SHACL_PATH,
    POWER_NS,
    OntologyClass,
    OntologyProperty,
    OntologyRuleLink,
    PowerOntology,
)
from matrag.domain.spec_params import SpecParameter, SpecStandard
from matrag.domain.terms import Term
from matrag.domain.test_methods import TestMethod
from matrag.domain.vocab import CATEGORY_LABELS, normalize_name

# --------------------------------------------------------------------------- #
# 构造工具 (凭空造数据, 反证"无按 id 分支")
# --------------------------------------------------------------------------- #

_XSD_DECIMAL = "http://www.w3.org/2001/XMLSchema#decimal"


def _term(
    term_id: str,
    *,
    zh: str,
    en: str = "",
    abbr: str = "",
    category: str = "general",
    aliases: tuple[str, ...] = (),
    properties: tuple[str, ...] = (),
    related_rules: tuple[str, ...] = (),
) -> Term:
    """凭空造一条术语。"""
    return Term(
        term_id=term_id,
        zh=zh,
        en=en,
        abbr=abbr,
        category=category,
        definition=f"{zh}的构造定义, 仅用于测试。",
        aliases=aliases,
        properties=properties,
        related_rules=related_rules,
        source={"kind": "card", "name": "fixture"},
    )


def _axiom(
    axiom_id: str,
    *,
    statement: str = "构造公理",
    expr: str = "V = I * R",
    variables: dict[str, str] | None = None,
    category: str = "general",
    properties: tuple[str, ...] = (),
    related_rules: tuple[str, ...] = (),
) -> Axiom:
    """凭空造一条公理/公式。"""
    return Axiom(
        axiom_id=axiom_id,
        kind="axiom",
        statement=statement,
        expr=expr,
        variables=dict(variables or {"V": "电压 (V)"}),
        category=category,
        properties=properties,
        related_rules=related_rules,
        source={"kind": "card", "name": "fixture"},
    )


def _param(
    param_id: str,
    *,
    name: str = "",
    sub_name: str = "",
    product_code: str = "P-1",
    unit: str = "",
    minimum: float | None = None,
    typical: float | None = None,
    maximum: float | None = None,
    category: str = "output",
    condition: str = "",
    requirement: str = "",
    scope: str = "",
    flags: tuple[str, ...] = (),
) -> SpecParameter:
    """凭空造一条规格书参数实例。"""
    return SpecParameter(
        param_id=param_id,
        product_code=product_code,
        doc_id=f"{product_code} 规格书",
        section_path="构造章节",
        table_id="t0-0",
        row_index=0,
        name=name,
        sub_name=sub_name,
        branch="",
        code="",
        grade="",
        unit=unit,
        minimum_raw="" if minimum is None else str(minimum),
        typical_raw="" if typical is None else str(typical),
        maximum_raw="" if maximum is None else str(maximum),
        minimum=minimum,
        typical=typical,
        maximum=maximum,
        note="",
        condition=condition,
        requirement=requirement,
        scope=scope,
        category=category,
        flags=flags,
    )


def _standard(
    standard_id: str,
    *,
    code: str = "GB 4943-2011",
    name: str = "信息技术设备安全",
    product_code: str = "P-1",
) -> SpecStandard:
    """凭空造一条标准引用。"""
    return SpecStandard(
        standard_id=standard_id,
        product_code=product_code,
        doc_id=f"{product_code} 规格书",
        section_path="构造章节",
        table_id="t0-9",
        row_index=0,
        code=code,
        name=name,
    )


def _ontology(*, rule1_classes: tuple[str, ...] = ("Protection",)) -> PowerOntology:
    """构造一个小本体: 1 个被测对象类型 + 1 个知识主题 + 2 属性 + 2 规则。"""
    trip = OntologyProperty(
        name="trip",
        uri=POWER_NS + "trip",
        origins=(ORIGIN_SHACL_PATH,),
        declared_names=("trip",),
        unit="V",
        units=("V",),
        range_iri=_XSD_DECIMAL,
        domain_classes=("Protection",),
        categories=("protection",),
        rule_ids=("R-PROT-1", "R-PROT-2"),
    )
    hysteresis = OntologyProperty(
        name="hysteresis",
        uri=POWER_NS + "hysteresis",
        origins=(ORIGIN_SHACL_PATH,),
        declared_names=("hysteresis",),
        unit="V",
        units=("V",),
        range_iri=_XSD_DECIMAL,
        domain_classes=("Protection",),
        categories=("protection",),
        rule_ids=("R-PROT-1",),
    )
    classes = {
        "Protection": OntologyClass(
            name="Protection",
            uri=POWER_NS + "Protection",
            axis=AXIS_SUBJECT,
            source_uri=POWER_NS + "Protection",
            comment="被测对象类型",
            rule_ids=("R-PROT-1", "R-PROT-2"),
        ),
        "protection": OntologyClass(
            name="protection",
            uri=CATEGORY_NS + "protection",
            axis=AXIS_CATEGORY,
            source_uri="protection",
            comment="知识主题",
            rule_ids=("R-PROT-1", "R-PROT-2"),
        ),
    }
    links = {
        "R-PROT-1": OntologyRuleLink(
            rule_id="R-PROT-1",
            uri=POWER_NS + "Rule_r_prot_1",
            kind="constraint",
            category="protection",
            scope="protection",
            statement="过压保护动作 (构造)",
            class_names=rule1_classes,
            consumes=("trip", "hysteresis"),
            produces=("violation",),
        ),
        "R-PROT-2": OntologyRuleLink(
            rule_id="R-PROT-2",
            uri=POWER_NS + "Rule_r_prot_2",
            kind="derive",
            category="protection",
            scope="protection",
            statement="回差推导 (构造)",
            class_names=("Protection",),
            consumes=("trip",),
            produces=("hysteresis",),
        ),
    }
    return PowerOntology(
        classes_by_name=classes,
        properties_by_name={"trip": trip, "hysteresis": hysteresis},
        links_by_rule=links,
        subject_class_names=("Protection",),
        category_names=("protection",),
    )


def _terms() -> tuple[Term, ...]:
    """小样本术语集: 含 ASCII 缩写别名 ``RE`` (边界保护的哨兵)。"""
    return (
        _term(
            "T-OVP",
            zh="过压保护",
            en="Over Voltage Protection",
            abbr="OVP",
            category="protection",
            aliases=("过电压保护",),
            properties=("trip", "hysteresis"),
            related_rules=("R-PROT-1",),
        ),
        _term(
            "T-OTP",
            zh="过温保护",
            en="Over Temperature Protection",
            abbr="OTP",
            category="protection",
            properties=("trip",),
            related_rules=("R-PROT-2",),
        ),
        _term(
            "T-RE",
            zh="辐射发射",
            en="Radiated Emission",
            abbr="RE",
            category="emc",
        ),
    )


def _axioms() -> tuple[Axiom, ...]:
    """小样本公理集 (挂属性与规则)。"""
    return (
        _axiom(
            "A-1",
            statement="欧姆定律",
            properties=("trip", "hysteresis"),
            related_rules=("R-PROT-1",),
        ),
    )


def _params() -> tuple[SpecParameter, ...]:
    """小样本参数集: 精确 / 包含 / 边界负例 / 边界正例 / 回退 / 占位符。"""
    return (
        _param(
            "p-exact",
            name="过压保护",
            unit="V",
            minimum=30.0,
            maximum=33.0,
            category="protection",
        ),
        _param(
            "p-contains",
            name="输入过压保护点",
            unit="V",
            category="protection",
            condition="25°C",
        ),
        _param("p-boundary-bad", name="reset电压", category="output"),
        _param("p-boundary-ok", name="RE 测试项", unit="dB", category="emc"),
        _param(
            "p-subfallback",
            name="-",
            sub_name="过温保护",
            unit="°C",
            category="protection",
        ),
        _param("p-placeholder", name="/", category="other"),
    )


def _standards() -> tuple[SpecStandard, ...]:
    """小样本标准集。"""
    return (_standard("S-1"),)


@pytest.fixture(scope="module")
def small_ontology() -> PowerOntology:
    """小样本本体。"""
    return _ontology()


@pytest.fixture(scope="module")
def small_graph(small_ontology: PowerOntology) -> KnowledgeGraph:
    """小样本知识图谱 (模块内只建一次)。"""
    return build_knowledge_graph(
        ontology=small_ontology,
        terms=_terms(),
        axioms=_axioms(),
        methods=(),
        params=_params(),
        standards=_standards(),
    )


def _relation_edges(
    graph: KnowledgeGraph, node_id: str, relation: str
) -> list[tuple[str, dict[str, Any]]]:
    """某节点某一类关系的 ``(目标, 边属性)`` 列表。"""
    return [
        (edge.target, edge.attrs) for edge in graph.edges_out(node_id, relation)
    ]


# --------------------------------------------------------------------------- #
# 1. 合成小样本: 节点 / 边语义
# --------------------------------------------------------------------------- #


def test_small_graph_node_ids_and_kinds(small_graph: KnowledgeGraph) -> None:
    """节点 id 前缀 = kind, 数量与构造输入一致。"""
    stats = small_graph.stats()
    assert stats["nodes"] == {
        KIND_AXIOM: 1,
        KIND_CATEGORY: 5,
        KIND_CLASS: 1,
        KIND_PARAM: 6,
        KIND_PRODUCT: 1,
        KIND_PROPERTY: 2,
        KIND_RULE: 2,
        KIND_STANDARD: 1,
        KIND_TERM: 3,
    }
    assert stats["totals"]["nodes"] == len(small_graph.nodes_by_id) == 22
    assert small_graph.get("product:P-1") is not None
    assert small_graph.get("param:p-exact") is not None
    assert small_graph.get("standard:S-1") is not None
    assert small_graph.get("term:T-OVP") is not None
    assert small_graph.get("rule:R-PROT-1") is not None
    assert small_graph.get("axiom:A-1") is not None
    assert small_graph.get("property:trip") is not None
    assert small_graph.get("class:Protection") is not None
    assert small_graph.get("category:protection") is not None
    assert small_graph.get("no-such-node") is None


def test_category_axis_class_is_not_a_class_node(small_graph: KnowledgeGraph) -> None:
    """知识主题只由 category 节点承载, category 轴的本体类不重复建 class 节点。"""
    assert small_graph.get("class:protection") is None
    category = small_graph.get("category:protection")
    assert category is not None
    assert category.label == CATEGORY_LABELS["protection"] == "保护特性"
    assert category.attrs["label"] == "保护特性"
    # 未知类别也建节点, 但 label 退回原名 (不静默丢)。
    assert small_graph.get("category:emc").label == CATEGORY_LABELS["emc"]


def test_class_and_property_nodes_carry_ontology_attrs(
    small_graph: KnowledgeGraph,
) -> None:
    """class/property 节点的属性来自本体对象 (axis / unit / rule_ids …)。"""
    protection = small_graph.get("class:Protection")
    assert protection is not None
    assert protection.attrs["axis"] == AXIS_SUBJECT
    assert protection.attrs["rule_ids"] == ["R-PROT-1", "R-PROT-2"]
    trip = small_graph.get("property:trip")
    assert trip is not None
    assert trip.label == "trip"
    assert trip.attrs["unit"] == "V"
    assert trip.attrs["domain_classes"] == ["Protection"]
    assert trip.attrs["categories"] == ["protection"]
    assert trip.attrs["rule_ids"] == ["R-PROT-1", "R-PROT-2"]


def test_param_node_attrs_include_condition_only_when_present(
    small_graph: KnowledgeGraph,
) -> None:
    """参数节点带来源定位字段; ``condition`` 有才带。"""
    node = small_graph.get("param:p-contains")
    assert node is not None
    assert node.label == "输入过压保护点"
    assert node.attrs["product_code"] == "P-1"
    assert node.attrs["doc_id"] == "P-1 规格书"
    assert node.attrs["table_id"] == "t0-0"
    assert node.attrs["unit"] == "V"
    assert node.attrs["category"] == "protection"
    assert node.attrs["flags"] == []
    assert node.attrs["condition"] == "25°C"
    assert "condition" not in small_graph.get("param:p-exact").attrs  # type: ignore[union-attr]


def test_exact_and_contains_matching(small_graph: KnowledgeGraph) -> None:
    """精确命中走 exact; 参数名里包含术语名走 contains (带归一命中键)。"""
    assert _relation_edges(small_graph, "param:p-exact", REL_DENOTES) == [
        ("term:T-OVP", {"match": "exact", "key": "过压保护", "field": "name"})
    ]
    assert _relation_edges(small_graph, "param:p-contains", REL_DENOTES) == [
        ("term:T-OVP", {"match": "contains", "key": "过压保护", "field": "name"})
    ]


def test_ascii_alias_is_boundary_protected(small_graph: KnowledgeGraph) -> None:
    """纯 ASCII 别名 ``re`` 不得命中 ``reset`` 内部; 词边界上仍应正常命中。"""
    assert _relation_edges(small_graph, "param:p-boundary-bad", REL_DENOTES) == []
    assert _relation_edges(small_graph, "param:p-boundary-ok", REL_DENOTES) == [
        ("term:T-RE", {"match": "contains", "key": "re", "field": "name"})
    ]


def test_sub_name_fallback_and_placeholder_skip(small_graph: KnowledgeGraph) -> None:
    """name 是占位符时回退 sub_name (field=sub_name); 两者都不行则不匹配。"""
    assert _relation_edges(small_graph, "param:p-subfallback", REL_DENOTES) == [
        ("term:T-OTP", {"match": "exact", "key": "过温保护", "field": "sub_name"})
    ]
    assert _relation_edges(small_graph, "param:p-placeholder", REL_DENOTES) == []


def test_instance_of_edges_carry_param_values(small_graph: KnowledgeGraph) -> None:
    """instance_of 经命中术语的 properties 展开, 带 via_term 与量值。"""
    edges = _relation_edges(small_graph, "param:p-exact", REL_INSTANCE_OF)
    assert [target for target, _ in edges] == ["property:hysteresis", "property:trip"]
    for _target, attrs in edges:
        assert attrs == {
            "via_term": "T-OVP",
            "unit": "V",
            "minimum": 30.0,
            "typical": None,
            "maximum": 33.0,
        }
    # 无 properties 的术语 (T-RE) 不给 instance_of。
    assert _relation_edges(small_graph, "param:p-boundary-ok", REL_INSTANCE_OF) == []


def test_term_and_axiom_level_edges_are_complete(small_graph: KnowledgeGraph) -> None:
    """术语/公理的 has_property 与 related_rule 是全量的 (与参数是否命中无关)。"""
    assert [target for target, _ in _relation_edges(small_graph, "term:T-OVP", REL_HAS_PROPERTY)] == [
        "property:hysteresis",
        "property:trip",
    ]
    assert _relation_edges(small_graph, "term:T-OVP", REL_RELATED_RULE) == [
        ("rule:R-PROT-1", {})
    ]
    assert [target for target, _ in _relation_edges(small_graph, "term:T-OTP", REL_HAS_PROPERTY)] == [
        "property:trip"
    ]
    assert _relation_edges(small_graph, "axiom:A-1", REL_RELATED_RULE) == [
        ("rule:R-PROT-1", {})
    ]
    assert [target for target, _ in _relation_edges(small_graph, "axiom:A-1", REL_HAS_PROPERTY)] == [
        "property:hysteresis",
        "property:trip",
    ]


def test_rule_edges_skip_violation_and_complete_taxonomy(
    small_graph: KnowledgeGraph,
) -> None:
    """约束型产出的 ``violation`` 不是属性, 正常跳过; 其余 rule 边全量。"""
    assert [target for target, _ in _relation_edges(small_graph, "rule:R-PROT-1", REL_CONSUMES)] == [
        "property:hysteresis",
        "property:trip",
    ]
    assert _relation_edges(small_graph, "rule:R-PROT-1", REL_PRODUCES) == []
    assert _relation_edges(small_graph, "rule:R-PROT-2", REL_PRODUCES) == [
        ("property:hysteresis", {})
    ]
    assert _relation_edges(small_graph, "rule:R-PROT-1", REL_APPLIES_TO) == [
        ("class:Protection", {})
    ]
    assert _relation_edges(small_graph, "property:trip", REL_IN_CLASS) == [
        ("class:Protection", {})
    ]


def test_product_standard_and_category_edges(small_graph: KnowledgeGraph) -> None:
    """产品拥有参数 / 符合标准, 各层都挂 in_category。"""
    assert len(_relation_edges(small_graph, "product:P-1", REL_HAS_PARAMETER)) == 6
    assert _relation_edges(small_graph, "product:P-1", REL_CONFORMS_TO) == [
        ("standard:S-1", {})
    ]
    assert _relation_edges(small_graph, "param:p-exact", REL_IN_CATEGORY) == [
        ("category:protection", {})
    ]
    assert _relation_edges(small_graph, "term:T-RE", REL_IN_CATEGORY) == [
        ("category:emc", {})
    ]
    assert _relation_edges(small_graph, "axiom:A-1", REL_IN_CATEGORY) == [
        ("category:general", {})
    ]
    assert _relation_edges(small_graph, "rule:R-PROT-1", REL_IN_CATEGORY) == [
        ("category:protection", {})
    ]
    assert _relation_edges(small_graph, "property:trip", REL_IN_CATEGORY) == [
        ("category:protection", {})
    ]


def test_small_graph_stats_and_edge_order(small_graph: KnowledgeGraph) -> None:
    """统计按 kind/relation 计数; 边序是规范排序 (确定性)。"""
    stats = small_graph.stats()
    assert stats["edges"] == {
        REL_APPLIES_TO: 2,
        REL_CONFORMS_TO: 1,
        REL_CONSUMES: 3,
        REL_DENOTES: 4,
        REL_HAS_PARAMETER: 6,
        REL_HAS_PROPERTY: 5,
        REL_IN_CATEGORY: 14,
        REL_IN_CLASS: 2,
        REL_INSTANCE_OF: 5,
        REL_PRODUCES: 1,
        REL_RELATED_RULE: 3,
    }
    assert stats["totals"] == {"nodes": 22, "edges": 46}
    keys = [(edge.source, edge.relation, edge.target) for edge in small_graph.edges]
    assert keys == sorted(keys)


def test_small_graph_query_helpers(small_graph: KnowledgeGraph) -> None:
    """nodes 按 id 排序; edges_in/edges_out/neighbors 与方向守卫。"""
    assert [node.node_id for node in small_graph.nodes(KIND_TERM)] == [
        "term:T-OTP",
        "term:T-OVP",
        "term:T-RE",
    ]
    assert small_graph.nodes("no-such-kind") == []
    assert small_graph.edges_out("no-such-node") == []
    assert small_graph.edges_in("no-such-node") == []
    incoming = small_graph.edges_in("property:trip", REL_INSTANCE_OF)
    assert [edge.source for edge in incoming] == [
        "param:p-contains",
        "param:p-exact",
        "param:p-subfallback",
    ]
    both = small_graph.neighbors("term:T-OVP", direction="both")
    assert [(edge.source, edge.relation, edge.target) for edge in both] == [
        ("param:p-contains", REL_DENOTES, "term:T-OVP"),
        ("param:p-exact", REL_DENOTES, "term:T-OVP"),
        ("term:T-OVP", REL_HAS_PROPERTY, "property:hysteresis"),
        ("term:T-OVP", REL_HAS_PROPERTY, "property:trip"),
        ("term:T-OVP", REL_IN_CATEGORY, "category:protection"),
        ("term:T-OVP", REL_RELATED_RULE, "rule:R-PROT-1"),
    ]
    with pytest.raises(ValueError):
        small_graph.neighbors("term:T-OVP", direction="sideways")


def test_to_dict_is_a_deep_copy(small_graph: KnowledgeGraph) -> None:
    """to_dict 深拷贝: 调用方改不动图内部 (attrs 是嵌套 dict/list)。"""
    node = small_graph.get("term:T-OVP")
    assert node is not None
    exported = node.to_dict()
    exported["attrs"]["aliases"].append("脏数据")
    exported["attrs"]["source"]["name"] = "脏数据"
    assert node.attrs["aliases"] == ["过电压保护"]
    assert node.attrs["source"]["name"] == "fixture"
    # KgEdge 同理 (attrs 默认空 dict, 每条边独立)。
    edge = small_graph.edges_out("param:p-exact", REL_DENOTES)[0]
    edge_dict = edge.to_dict()
    edge_dict["attrs"]["match"] = "脏数据"
    assert edge.attrs["match"] == "exact"


# --------------------------------------------------------------------------- #
# 2. 合成小样本: 质量门与覆盖率
# --------------------------------------------------------------------------- #


def test_clean_fixture_has_no_issues(small_graph: KnowledgeGraph) -> None:
    """干净 fixture → 空清单 = 通过。"""
    assert small_graph.validate() == []


def test_validate_detects_dangling_edge() -> None:
    """边的端点节点不存在 → dangling_edge (直接构造图验证)。"""
    graph = KnowledgeGraph(
        nodes_by_id={
            "product:P-X": KgNode(
                node_id="product:P-X", kind=KIND_PRODUCT, label="P-X"
            )
        },
        edges=(
            KgEdge(
                source="product:P-X",
                relation=REL_HAS_PARAMETER,
                target="param:不存在",
            ),
        ),
    )
    issues = graph.validate()
    assert [issue.kind for issue in issues] == [ISSUE_DANGLING_EDGE]
    assert issues[0].involved == ("param:不存在",)
    assert "product:P-X" in issues[0].detail
    assert graph.coverage()["params_total"] == 0


def test_constructor_rejects_single_edge_or_foreign_elements() -> None:
    """直接构造图时, 单条边 / 非 KgEdge 元素立刻报错 (fail-fast)。"""
    node = KgNode(node_id="product:P-X", kind=KIND_PRODUCT, label="P-X")
    edge = KgEdge(
        source="product:P-X", relation=REL_HAS_PARAMETER, target="param:x"
    )
    with pytest.raises(TypeError, match="序列"):
        KnowledgeGraph(nodes_by_id={"product:P-X": node}, edges=edge)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="非 KgEdge"):
        KnowledgeGraph(
            nodes_by_id={"product:P-X": node},
            edges=(edge, "not-an-edge"),  # type: ignore[arg-type]
        )
    # list 会被规整成 tuple (最小容错, 图内部统一按只读元组遍历)。
    graph = KnowledgeGraph(nodes_by_id={"product:P-X": node}, edges=[edge])  # type: ignore[arg-type]
    assert graph.edges == (edge,)


def test_validate_detects_missing_property_rule_and_class() -> None:
    """缺属性 / 缺规则 / 缺类三类断引用同一次全报出 (互不吞并)。"""
    bad_term = _term(
        "T-BAD",
        zh="坏术语",
        category="general",
        properties=("no_such_property",),
        related_rules=("R-NOPE-999",),
    )
    bad_axiom = _axiom(
        "A-BAD",
        statement="坏公理",
        properties=("no_such_property",),
        related_rules=("R-NOPE-999",),
    )
    graph = build_knowledge_graph(
        ontology=_ontology(rule1_classes=("NoSuchClass",)),
        terms=(*_terms(), bad_term),
        axioms=(*_axioms(), bad_axiom),
        methods=(),
        params=_params(),
        standards=_standards(),
    )
    kinds = {issue.kind for issue in graph.validate()}
    assert kinds == {
        ISSUE_MISSING_PROPERTY,
        ISSUE_MISSING_RULE,
        ISSUE_MISSING_CLASS,
    }, [issue.detail for issue in graph.validate()]
    missing_property = [
        issue for issue in graph.validate() if issue.kind == ISSUE_MISSING_PROPERTY
    ]
    assert len(missing_property) == 2
    assert missing_property[0].involved == ("A-BAD", "no_such_property")
    assert missing_property[1].involved == ("T-BAD", "no_such_property")
    missing_class = [
        issue for issue in graph.validate() if issue.kind == ISSUE_MISSING_CLASS
    ]
    assert missing_class[0].involved == ("R-PROT-1", "NoSuchClass")
    # 断引用不产生悬空边 (边在构建期已被过滤), 但问题必须留痕。
    assert graph.get("rule:R-PROT-1") is not None
    assert graph.edges_out("rule:R-PROT-1", REL_APPLIES_TO) == []
    assert graph.edges_out("term:T-BAD", REL_HAS_PROPERTY) == []
    assert graph.edges_out("term:T-BAD", REL_RELATED_RULE) == []
    assert graph.edges_out("axiom:A-BAD", REL_HAS_PROPERTY) == []


def test_coverage_counts_on_small_graph(small_graph: KnowledgeGraph) -> None:
    """覆盖率每一项都数得出来 (占位符/未命中/属性证据/可达规则)。"""
    coverage = small_graph.coverage()
    assert coverage["params_total"] == 6
    assert coverage["params_with_term"] == 4
    assert coverage["params_with_property"] == 3
    assert coverage["params_placeholder"] == 2
    assert coverage["params_unmatched"] == 2
    assert coverage["unmatched_by_category"] == {"other": 1, "output": 1}
    assert coverage["unmatched_params"] == [
        {"product_code": "P-1", "name": "/", "category": "other"},
        {"product_code": "P-1", "name": "reset电压", "category": "output"},
    ]
    assert coverage["products"] == {
        "P-1": {"params": 6, "with_term": 4, "with_property": 3}
    }
    assert coverage["properties_total"] == 2
    assert coverage["properties_with_spec_evidence"] == 2
    assert coverage["properties_without_spec_evidence"] == []
    assert coverage["rules_reachable_from_products"] == 2


def test_coverage_reports_properties_without_spec_evidence(
    small_ontology: PowerOntology,
) -> None:
    """本体里没被任何规格书参数落地的属性 → 明确列进"概念缺口"清单。"""
    extra = OntologyProperty(
        name="only_in_ontology",
        uri=POWER_NS + "only_in_ontology",
        unit="",
        rule_ids=("R-PROT-1",),
    )
    ontology = PowerOntology(
        classes_by_name=small_ontology.classes_by_name,
        properties_by_name={**small_ontology.properties_by_name, extra.name: extra},
        links_by_rule=small_ontology.links_by_rule,
        subject_class_names=small_ontology.subject_class_names,
        category_names=small_ontology.category_names,
    )
    graph = build_knowledge_graph(
        ontology=ontology,
        terms=_terms(),
        axioms=_axioms(),
        methods=(),
        params=_params(),
        standards=_standards(),
    )
    coverage = graph.coverage()
    assert coverage["properties_total"] == 3
    assert coverage["properties_with_spec_evidence"] == 2
    assert coverage["properties_without_spec_evidence"] == ["only_in_ontology"]
    assert coverage["params_total"] == 6


def test_unmatched_params_sorted_by_normalized_name() -> None:
    """未命中清单按归一后名字排序 (跨大小写/空白差异时仍然稳定)。"""
    params = (
        _param("a", name="Zeta 项目", category="other"),
        _param("b", name="ALPHA 项目", category="other"),
        _param("c", name="beta项目", category="other"),
    )
    graph = build_knowledge_graph(
        ontology=_ontology(),
        terms=_terms(),
        axioms=(),
        methods=(),
        params=params,
        standards=(),
    )
    names = [item["name"] for item in graph.coverage()["unmatched_params"]]
    assert names == ["ALPHA 项目", "beta项目", "Zeta 项目"]
    normalized = [normalize_name(name) for name in names]
    assert normalized == sorted(normalized)


def test_build_rejects_duplicate_node_ids() -> None:
    """同一 param_id 两条实例会算出同一节点 id → fail-fast, 不静默覆盖。"""
    duplicate = (
        _param("same-id", name="过压保护", category="protection"),
        _param("same-id", name="过温保护", category="protection"),
    )
    with pytest.raises(ValueError, match="节点 id 重复"):
        build_knowledge_graph(
            ontology=_ontology(),
            terms=_terms(),
            axioms=(),
            params=duplicate,
            standards=(),
        )


def test_build_with_empty_inputs_is_allowed() -> None:
    """显式传空序列是"这一层没有数据"的正常语义 (与"加载失败"不同)。"""
    graph = build_knowledge_graph(ontology=_ontology(), terms=(), axioms=(), methods=(), params=(), standards=())
    assert graph.nodes(KIND_PARAM) == []
    assert graph.nodes(KIND_TERM) == []
    assert graph.coverage()["params_total"] == 0
    assert graph.validate() == []


def test_single_char_keys_are_exact_only() -> None:
    """单字符匹配键 (如 abbr ``A``) 只参与精确匹配, 不参与包含匹配。"""
    terms = (
        _term("T-CHAR", zh="单字符键", en="Single Char Key", abbr="A", category="other"),
    )
    params = (
        _param("p-exact", name="A", category="other"),
        _param("p-contains", name="Data 项目", category="other"),
    )
    graph = build_knowledge_graph(
        ontology=_ontology(),
        terms=terms,
        axioms=(),
        methods=(),
        params=params,
        standards=(),
    )
    hits = graph.edges_out("param:p-exact", REL_DENOTES)
    assert [(edge.target, edge.attrs["match"]) for edge in hits] == [
        ("term:T-CHAR", "exact")
    ]
    assert graph.edges_out("param:p-contains", REL_DENOTES) == []


def test_scope_fallback_matches_after_name_and_sub_name() -> None:
    """name/sub_name 未命中时按表级 scope 回退锚定 (denotes 边 field=scope)。"""
    terms = (
        _term(
            "T-YX",
            zh="遥信",
            en="Telesignal",
            abbr="YX",
            category="communication",
            aliases=("遥信功能",),
        ),
    )
    params = (
        _param(
            "p-scope",
            name="电源在位",
            scope="4.3.4.1 遥信功能",
            category="communication",
        ),
        _param("p-no-scope", name="无锚点行", category="other"),
    )
    graph = build_knowledge_graph(
        ontology=_ontology(),
        terms=terms,
        axioms=(),
        methods=(),
        params=params,
        standards=(),
    )
    hits = graph.edges_out("param:p-scope", REL_DENOTES)
    assert [(edge.target, edge.attrs["field"]) for edge in hits] == [
        ("term:T-YX", "scope")
    ]
    assert graph.edges_out("param:p-no-scope", REL_DENOTES) == []


# --------------------------------------------------------------------------- #
# 3. 真实数据 smoke (数字按 2026-10 实测写死)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def real_graph() -> KnowledgeGraph:
    """真实数据构建的知识图谱 (模块内只建一次)。"""
    return build_knowledge_graph()


def test_real_graph_node_counts(real_graph: KnowledgeGraph) -> None:
    """真实数据: 3 产品 / 372 参数 / 92 标准 / 168 术语 / 132 规则 / 76 公理 / 35 方法。"""
    stats = real_graph.stats()
    assert stats["nodes"][KIND_PRODUCT] == 3
    assert stats["nodes"][KIND_PARAM] == 372
    assert stats["nodes"][KIND_STANDARD] == 92
    assert stats["nodes"][KIND_TERM] == 168
    assert stats["nodes"][KIND_RULE] == 132
    assert stats["nodes"][KIND_AXIOM] == 76
    assert stats["nodes"][KIND_METHOD] == 35
    # class 只收 subject 轴 (44), 知识主题由 category 节点承载 (36 个)。
    assert stats["nodes"][KIND_CLASS] == 44
    assert stats["nodes"][KIND_PROPERTY] == 267
    assert stats["nodes"][KIND_CATEGORY] == 36
    assert all(
        node.attrs["axis"] == AXIS_SUBJECT for node in real_graph.nodes(KIND_CLASS)
    )


def test_real_graph_edge_counts(real_graph: KnowledgeGraph) -> None:
    """真实数据的边分布 (2026-10 实测, 数据变化时会提醒更新)。"""
    assert real_graph.stats()["edges"] == {
        REL_APPLIES_TO: 77,
        REL_CONFORMS_TO: 92,
        REL_CONSUMES: 261,
        REL_DENOTES: 362,
        REL_GUIDED_BY: 101,
        REL_HAS_PARAMETER: 372,
        REL_HAS_PROPERTY: 571,
        REL_IN_CATEGORY: 1024,
        REL_IN_CLASS: 140,
        REL_INSTANCE_OF: 625,
        REL_MEASURES: 105,
        REL_PRODUCES: 55,
        REL_RELATED_RULE: 289,
    }
    assert real_graph.stats()["totals"] == {"nodes": 1225, "edges": 4074}


def test_real_validate_is_clean(real_graph: KnowledgeGraph) -> None:
    """真实数据质量门为 0 issue (悬空边/缺属性/缺规则/缺类全干净)。"""
    assert real_graph.validate() == []


def test_real_coverage_numbers(real_graph: KnowledgeGraph) -> None:
    """真实覆盖率: 372 参数中 362 命中术语, 104/267 属性有规格书证据。"""
    coverage = real_graph.coverage()
    assert coverage["params_total"] == 372
    assert coverage["params_with_term"] >= 50  # 任务底线
    assert coverage["params_with_term"] == 362
    assert coverage["params_with_property"] == 267
    assert coverage["params_placeholder"] == 0
    assert coverage["params_unmatched"] == 10
    assert coverage["params_with_term"] + coverage["params_unmatched"] == 372
    assert coverage["unmatched_by_category"] == {
        "other": 4,
        "reliability": 5,
        "safety": 1,
    }
    assert coverage["products"] == {
        "CY0177-75V-20A": {"params": 50, "with_term": 50, "with_property": 38},
        "GDC800T5054-UC": {"params": 106, "with_term": 106, "with_property": 79},
        "PA601-D54A": {"params": 216, "with_term": 206, "with_property": 150},
    }
    assert coverage["properties_total"] == 267
    assert coverage["properties_with_spec_evidence"] == 104
    without = coverage["properties_without_spec_evidence"]
    assert len(without) == 163
    assert without == sorted(without)
    assert len(set(without)) == len(without)
    assert (
        coverage["properties_with_spec_evidence"] + len(without)
        == coverage["properties_total"]
    )
    assert "trip" not in without
    assert "can_inject_open" in without
    assert coverage["rules_reachable_from_products"] == 73
    assert coverage["methods_total"] == 35
    assert coverage["methods_with_term"] == 35
    assert coverage["methods_with_rule"] == 34


def test_real_known_term_matches(real_graph: KnowledgeGraph) -> None:
    """真实数据里的已知命中样例 (逐条实测, 不是猜的)。"""
    expected = {
        ("CY0177-75V-20A", "输入欠压保护", "欠压点"): ("term:T-UVLO", "exact"),
        ("CY0177-75V-20A", "过温保护", "-"): ("term:T-OTP", "exact"),
        ("PA601-D54A", "输出短路保护", "-54V"): ("term:T-SCP", "exact"),
        (
            "GDC800T5054-UC",
            "输入欠压保护(50V)\n保护点\n恢复点\n回差",
            "",
        ): ("term:T-UVLO", "contains"),
        ("PA601-D54A", "整机效率", "整机效率"): ("term:T-EFFICIENCY", "exact"),
        ("CY0177-75V-20A", "效率", ""): ("term:T-EFFICIENCY", "exact"),
        (
            "GDC800T5054-UC",
            "输出电压延迟时间",
            "",
        ): ("term:T-STARTUPTIME", "exact"),
        ("CY0177-75V-20A", "电压调整率(恒压)", ""): ("term:T-LINEREG", "contains"),
        ("PA601-D54A", "防水防尘等级", ""): ("term:T-IP", "exact"),
        ("PA601-D54A", "环保要求", ""): ("term:T-ROHS", "exact"),
        (
            "PA601-D54A",
            "防护器件不在位浪涌防起火要求",
            "",
        ): ("term:T-SURGE", "contains"),
    }
    for (product_code, name, sub_name), (target, method) in expected.items():
        hits = [
            node
            for node in real_graph.nodes(KIND_PARAM)
            if node.attrs["product_code"] == product_code
            and node.attrs["name"] == name
            and node.attrs["sub_name"] == sub_name
        ]
        assert hits, f"未找到参数 {product_code}|{name}|{sub_name}"
        edges = real_graph.edges_out(hits[0].node_id, REL_DENOTES)
        assert len(edges) == 1, f"{name} 应只命中一个术语: {edges}"
        assert edges[0].target == target
        assert edges[0].attrs["match"] == method


def test_real_scope_fallback_anchors_monitoring_rows(
    real_graph: KnowledgeGraph,
) -> None:
    """遥信/遥控/遥测表: name 无术语时经 scope 回退锚定 (field=scope, 实测)。"""
    expected = {
        ("PA601-D54A", "电源在位"): ("term:T-TELESIGNAL", "遥信功能"),
        ("PA601-D54A", "电源开关机信号"): ("term:T-TELECONTROL", "遥控功能"),
        ("PA601-D54A", "输入电压"): ("term:T-TELEMETRY", "遥测功能"),
    }
    for (product_code, name), (target, scope) in expected.items():
        hits = [
            node
            for node in real_graph.nodes(KIND_PARAM)
            if node.attrs["product_code"] == product_code
            and node.attrs["name"] == name
        ]
        assert len(hits) == 1, f"{product_code}|{name} 命中 {len(hits)} 条"
        node = hits[0]
        assert node.attrs["scope"] == scope
        edges = real_graph.edges_out(node.node_id, REL_DENOTES)
        assert len(edges) == 1, f"{name} 应只命中一个术语: {edges}"
        assert edges[0].target == target
        assert edges[0].attrs["field"] == "scope"


def test_real_instance_of_expands_term_properties(real_graph: KnowledgeGraph) -> None:
    """CY0177 的「输入欠压保护」经 T-UVLO 落到 turn_on/turn_off/hysteresis。"""
    node = next(
        item
        for item in real_graph.nodes(KIND_PARAM)
        if item.attrs["product_code"] == "CY0177-75V-20A"
        and item.attrs["name"] == "输入欠压保护"
        and item.attrs["sub_name"] == "欠压点"
    )
    targets = [
        edge.target
        for edge in real_graph.edges_out(node.node_id, REL_INSTANCE_OF)
    ]
    assert targets == [
        "property:hysteresis",
        "property:turn_off_threshold",
        "property:turn_on_threshold",
    ]
    for edge in real_graph.edges_out(node.node_id, REL_INSTANCE_OF):
        assert edge.attrs["via_term"] == "T-UVLO"
        assert edge.attrs["unit"] == "Vac"
        assert edge.attrs["minimum"] == 85.0


def test_real_method_node_and_links(real_graph: KnowledgeGraph) -> None:
    """产测方法节点: M-RIPPLE-NOISE 的 measures/guided_by 边与源卡落地。"""
    method_id = "method:M-RIPPLE-NOISE"
    node = real_graph.get(method_id)
    assert node is not None
    assert node.kind == KIND_METHOD
    assert node.label == "输出纹波与噪声测试"
    assert node.attrs["category"] == "output"
    assert node.attrs["source"]["card_id"] == "power-method-ripple-noise"
    assert node.attrs["instruments"], "方法节点应带所需仪表字段"
    measures = [edge.target for edge in real_graph.edges_out(method_id, REL_MEASURES)]
    assert measures == [
        "term:T-ESR",
        "term:T-RIPPLE",
        "term:T-RIPPLE-CURRENT",
        "term:T-RIPPLEVOLT",
        "term:T-SWITCHFREQ",
        "term:T-VPP",
    ]
    guided = [edge.target for edge in real_graph.edges_out(method_id, REL_GUIDED_BY)]
    assert guided == [
        "rule:K-PWR-101",
        "rule:K-PWR-102",
        "rule:K-PWR-103",
        "rule:K-PWR-104",
    ]


def test_validate_detects_missing_method_refs() -> None:
    """方法引用缺席术语/规则 → ISSUE_MISSING_TERM / ISSUE_MISSING_RULE (fail-loud)。"""
    bad = TestMethod(
        method_id="M-BAD-999",
        zh="坏方法",
        en="Bad Method",
        category="general",
        definition="故意引用不存在的术语与规则, 验证质量门逐条报出。",
        measures=("T-NOPE-999",),
        rules=("K-NOPE-999",),
    )
    graph = build_knowledge_graph(
        ontology=_ontology(),
        terms=_terms(),
        axioms=(),
        methods=(bad,),
        params=_params(),
        standards=_standards(),
    )
    issues = {issue.kind: issue.involved for issue in graph.validate()}
    assert issues[ISSUE_MISSING_TERM] == ("M-BAD-999", "T-NOPE-999")
    assert issues[ISSUE_MISSING_RULE] == ("M-BAD-999", "K-NOPE-999")
    # 断引用不产生悬空边: measures/guided_by 边在构建期被过滤。
    assert graph.edges_out("method:M-BAD-999", REL_MEASURES) == []
    assert graph.edges_out("method:M-BAD-999", REL_GUIDED_BY) == []


def test_real_two_builds_are_byte_identical() -> None:
    """确定性: 同一输入两次构建, to_json 逐字节相等。"""
    first = build_knowledge_graph()
    second = build_knowledge_graph()
    assert first.to_json() == second.to_json()
    assert first.stats() == second.stats()
    assert first.coverage() == second.coverage()
    assert first.validate() == second.validate()


def test_real_to_json_roundtrips(real_graph: KnowledgeGraph) -> None:
    """to_json 可被 json.loads 读回, 且等价于 to_dict。"""
    loaded = json.loads(real_graph.to_json())
    assert loaded == real_graph.to_dict()
    assert len(loaded["nodes"]) == 1225
    assert len(loaded["edges"]) == 4074
    # 节点按 id 排序、边按 (source, relation, target) 排序。
    node_ids = [node["node_id"] for node in loaded["nodes"]]
    assert node_ids == sorted(node_ids)
    tuples = [
        (edge["source"], edge["relation"], edge["target"])
        for edge in loaded["edges"]
    ]
    assert tuples == sorted(tuples)


# --------------------------------------------------------------------------- #
# 4. 落盘
# --------------------------------------------------------------------------- #


def test_write_knowledge_graph_persists_and_is_readable(tmp_path: Path) -> None:
    """落盘: 文件存在、可 json.loads 读回、stats/coverage 与内存图一致。"""
    graph = build_knowledge_graph()
    result = write_knowledge_graph(tmp_path)
    json_path = Path(result["json"])
    assert json_path.is_file()
    assert json_path.name == DEFAULT_KG_FILENAME
    assert result["out_dir"] == str(tmp_path)
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert set(loaded) == {"stats", "coverage", "nodes", "edges"}
    assert loaded["stats"] == result["stats"] == graph.stats()
    assert loaded["coverage"] == result["coverage"] == graph.coverage()
    assert loaded["nodes"] == graph.to_dict()["nodes"]
    assert loaded["edges"] == graph.to_dict()["edges"]
    assert result["stats"]["totals"] == {"nodes": 1225, "edges": 4074}
    assert result["coverage"]["params_total"] == 372


def test_write_knowledge_graph_accepts_prebuilt_inputs(tmp_path: Path) -> None:
    """落盘也可接受预构建的输入 (与 build 用同一套参数, 结果一致)。"""
    result = write_knowledge_graph(
        tmp_path,
        ontology=_ontology(),
        terms=_terms(),
        axioms=_axioms(),
        methods=(),
        params=_params(),
        standards=_standards(),
    )
    loaded = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
    assert loaded["stats"]["totals"] == {"nodes": 22, "edges": 46}
    assert loaded["coverage"]["params_total"] == 6


def test_default_out_dir_is_the_ontology_generated_dir() -> None:
    """默认输出目录与本体产物同址 (data/domain/generated)。"""
    assert DEFAULT_GENERATED_DIR.name == "generated"
    assert DEFAULT_GENERATED_DIR.parent.name == "domain"
    assert DEFAULT_GENERATED_DIR == (
        Path(__file__).resolve().parents[1] / "data" / "domain" / "generated"
    )
