"""电力域本体测试。

三条主线:

1. **构建正确性** —— 类/属性/量纲从数据里抽出来了吗? 两个轴分开了吗?
   camelCase 的 ``sh:path`` 归一了吗?
2. **质量门真能检出问题** —— 用**小规模构造 fixture** (不依赖真实数据
   碰巧触发) 逐个验证 ``validate()`` 的 5 类 issue; 真实数据只用来
   "跑一次并把结果钉成已知问题清单"。
3. **产物可靠** —— 确定性 (逐位相同)、Turtle 能被 rdflib 往返、
   ``write_ontology`` 落盘可读回。

全部离线: 不涉及 LLM / 网络。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF

from matrag.domain.ontology import (
    CATEGORY_NS,
    DEFAULT_GENERATED_DIR,
    ISSUE_RULE_INCONSISTENT,
    ISSUE_SHAPE_UNPARSEABLE,
    ISSUE_UNCLASSIFIED_CONSTRAINT,
    ISSUE_UNIT_CONFLICT,
    ISSUE_UNIT_MISSING,
    POWER_NS,
    OntologyBuildError,
    OntologyClass,
    OntologyIssue,
    build_ontology,
    normalize_param_name,
    write_ontology,
)
from matrag.domain.rules_loader import DomainRule, load_rules

# --------------------------------------------------------------------------- #
# 构造 fixture 工具 (凭空造规则, 反证"无 rule_id 分支")
# --------------------------------------------------------------------------- #

_REAL_RULES = load_rules()


def derive_rule(
    rule_id: str,
    *,
    statement: str = "构造的公式",
    category: str = "electrical",
    scope: str = "dc",
    inputs: list[str],
    output: str,
    given: dict[str, Any],
    expect: dict[str, Any],
    variables: dict[str, str] | None = None,
) -> DomainRule:
    """凭空造一条公式型规则 (不来自真实 rules.yaml)。"""
    expr = " + ".join(inputs) if len(inputs) > 1 else (
        inputs[0] if inputs else "0"
    )
    return DomainRule(
        rule_id=rule_id,
        statement=statement,
        category=category,
        scope=scope,
        derive={"expr": expr, "inputs": list(inputs), "output": output},
        test={"given": dict(given), "expect": dict(expect)},
        variables=dict(variables or {}),
    )


def constraint_rule(
    rule_id: str,
    *,
    shape: str,
    statement: str = "构造的约束",
    category: str = "protection",
    scope: str = "protection",
    given: dict[str, Any] | None = None,
    expect: dict[str, Any] | None = None,
) -> DomainRule:
    """凭空造一条约束型规则 (shape 由测试自己给)。"""
    return DomainRule(
        rule_id=rule_id,
        statement=statement,
        category=category,
        scope=scope,
        constraint={"shape": shape},
        test={
            "given": dict(given or {}),
            "expect": dict(expect or {"violation": True}),
        },
    )


SHAPE_WITH_TARGET = """\
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ps: <https://matrag.example.org/power#> .
ps:ProtectionTripShape a sh:NodeShape ;
    sh:targetClass ps:Protection ;
    sh:property [
        sh:path ps:tripValue ;
        sh:minCount 1
    ] .
"""

# 故意缺 sh:targetClass —— 用来触发"无法归类"。
SHAPE_WITHOUT_TARGET = """\
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ps: <https://matrag.example.org/power#> .
ps:OrphanShape a sh:NodeShape ;
    sh:property [
        sh:path ps:someValue ;
        sh:minCount 1
    ] .
"""

# 不是合法 Turtle —— 用来触发 shape 解析失败。
SHAPE_BROKEN = "@prefix sh: <<<not valid turtle>>> . ps:X sh:targetClass ps:Y"


@pytest.fixture(scope="module")
def real_ontology() -> Any:
    """真实 132 条规则构建出的本体 (只建一次, 供多条用例复用)。"""
    return build_ontology()


@pytest.fixture(scope="module")
def real_issues(real_ontology: Any) -> list[OntologyIssue]:
    """真实规则上的质量门结果 (只跑一次)。"""
    return real_ontology.validate()


# --------------------------------------------------------------------------- #
# 1. 构建确定性
# --------------------------------------------------------------------------- #


def test_build_is_deterministic_byte_for_byte() -> None:
    """同一输入两次构建, to_dict / to_turtle / stats 必须逐位相同。"""
    rules = _REAL_RULES[:20]
    first = build_ontology(rules)
    second = build_ontology(rules)
    assert first.to_turtle() == second.to_turtle()
    assert first.to_dict() == second.to_dict()
    assert first.stats() == second.stats()
    assert first.validate() == second.validate()


def test_build_turtle_is_stable_across_repeated_calls() -> None:
    """to_turtle() 走缓存, 重复调用也必须一致 (不会因序列化器内部顺序抖动)。"""
    onto = build_ontology(_REAL_RULES[:10])
    assert onto.to_turtle() == onto.to_turtle()


def test_build_rejects_empty_rule_set() -> None:
    """空规则集必须 fail-fast, 严禁静默返回空本体。"""
    with pytest.raises(OntologyBuildError):
        build_ontology([])


def test_build_defaults_to_load_rules() -> None:
    """rules=None 走 load_rules(), 与显式传入全量结果一致。"""
    implicit = build_ontology()
    explicit = build_ontology(load_rules())
    assert implicit.to_turtle() == explicit.to_turtle()


# --------------------------------------------------------------------------- #
# 2. 类 / 属性 / 单位查询
# --------------------------------------------------------------------------- #


def test_classes_split_into_subject_and_category_axes() -> None:
    """subject 轴 (被测对象类型) 与 category 轴 (知识主题) 必须分开建。"""
    onto = build_ontology(_REAL_RULES)
    subjects = onto.subject_classes()
    categories = onto.category_classes()
    assert subjects, "应从 sh:targetClass 抽出被测对象类型"
    assert categories, "应从 rule.category 建出知识主题轴"
    # 两个轴不得重名 (构建期已硬校验, 这里再钉一次)。
    assert not (set(subjects) & set(categories))
    # subject 轴来自 SHACL targetClass 的 IRI。
    assert subjects["Protection"].uri == POWER_NS + "Protection"
    assert subjects["Protection"].axis == "subject"
    # category 轴独立命名空间。
    assert categories["protection"].uri == CATEGORY_NS + "protection"
    assert categories["protection"].axis == "category"


def test_subject_class_carries_its_constraint_rules() -> None:
    """被测对象类型应挂上引用它的约束型规则。"""
    onto = build_ontology(_REAL_RULES)
    assert onto.rules_for_class("Protection") == ["K-PROT-001"]
    # Product 被 7 条规则引用。
    assert len(onto.rules_for_class("Product")) == 7


def test_rules_for_category_matches_category_axis() -> None:
    """rules_for_category 与 rules_for_class 在 category 短名上同义。"""
    onto = build_ontology(_REAL_RULES)
    assert onto.rules_for_category("safety") == onto.rules_for_class("safety")
    assert len(onto.rules_for_category("safety")) == 12


def test_unknown_lookups_return_empty_not_crash() -> None:
    """未知短名/参数返回空, 但绝不抛 —— 这是查询语义, 不是降级。"""
    onto = build_ontology(_REAL_RULES[:5])
    assert onto.rules_for_class("NoSuchClass") == []
    assert onto.rules_for_category("no-such-category") == []
    assert onto.unit_of("no_such_param") == ""
    assert onto.units_of("no_such_param") == []


def test_properties_cover_all_declared_inputs_and_outputs() -> None:
    """属性表 = shape 的 sh:path ∪ derive.inputs/output ∪ 全部 input_params。

    约束型规则的 ``violation`` **不在**属性表里 (它是布尔判定, 不是量纲),
    由 :func:`test_constraint_verdict_is_not_a_dimensional_property` 单独钉。
    """
    onto = build_ontology(_REAL_RULES)
    props = onto.properties()
    expected: set[str] = set()
    for rule in _REAL_RULES:
        expected.update(normalize_param_name(p) for p in rule.input_params)
        if rule.is_derive:
            expected.add(normalize_param_name(rule.output_key))
    assert expected == set(props)
    assert "violation" not in props
    # SHACL 的 camelCase path 归一后必须落进属性表。
    assert "contact_resistance" in props
    assert props["contact_resistance"].origins == (
        "given_key",
        "shacl_path",
    )
    # 归并前的原始写法全部保留, 可回溯。
    assert props["contact_resistance"].declared_names == (
        "contactResistance",
        "contact_resistance",
    )


def test_params_of_covers_consumes_and_produces() -> None:
    """params_of = 消费 ∪ 产出。"""
    onto = build_ontology(_REAL_RULES)
    assert onto.params_of("K-ELEC-001") == ["current", "power", "voltage"]
    assert onto.consumes_of("K-ELEC-001") == ["current", "voltage"]
    assert onto.produces_of("K-ELEC-001") == ["power"]


def test_params_of_unknown_rule_raises() -> None:
    """查询不存在的规则必须抛 KeyError, 不能静默返回空。"""
    onto = build_ontology(_REAL_RULES[:3])
    with pytest.raises(KeyError):
        onto.params_of("K-NOPE-999")
    with pytest.raises(KeyError):
        onto.link_of("K-NOPE-999")


def test_unit_lookup_and_empty_unit_is_visible() -> None:
    """单位能查到; 未声明单位的参数 unit='' 但**仍在表里、可见**。"""
    onto = build_ontology(_REAL_RULES)
    assert onto.unit_of("voltage") == "V"
    assert onto.unit_of("current") == "A"
    # cap_class 是分类值 (Y2 等), 没有物理单位 —— 空串, 不丢弃。
    assert onto.unit_of("cap_class") == ""
    assert "cap_class" in onto.properties()
    assert "cap_class" in onto.properties_without_unit()
    # 空串单位同样写进 to_dict / to_turtle 可见的属性表。
    assert onto.to_dict()["properties"], "to_dict 必须含属性表"
    ttl = onto.to_turtle()
    assert "power:cap_class " in ttl, "未声明单位的参数仍须出现在 Turtle 图里"


def test_unit_conflict_is_exposed_not_silently_collapsed() -> None:
    """单位冲突时 unit_of 给规范值, units_of 给全量, 冲突不被吞掉。"""
    rules = [
        derive_rule(
            "X-101",
            inputs=["voltage"],
            output="power",
            given={"voltage": 54},
            expect={"power": 54},
            variables={"voltage": "V", "power": "W"},
        ),
        derive_rule(
            "X-102",
            inputs=["voltage"],
            output="ripple",
            given={"voltage": 54},
            expect={"ripple": 54},
            variables={"voltage": "mV", "ripple": "mV"},
        ),
    ]
    onto = build_ontology(rules)
    assert onto.units_of("voltage") == ["V", "mV"]
    assert onto.unit_of("voltage") == "V"  # 字典序最小者
    prop = onto.properties()["voltage"]
    assert prop.unit_conflict is True
    # 每条规则自己的声明都能单独查到 (冲突的原始证据)。
    assert onto.unit_of_rule("X-101", "voltage") == "V"
    assert onto.unit_of_rule("X-102", "voltage") == "mV"
    assert "voltage" in onto.properties_with_unit_conflict()


def test_unit_of_rule_returns_per_rule_declaration() -> None:
    """unit_of_rule 取的是该规则自己的声明, 不是全局归并值。"""
    onto = build_ontology(_REAL_RULES)
    assert onto.unit_of_rule("K-ELEC-001", "voltage") == "V"
    assert onto.unit_of_rule("K-ELEC-001", "not_declared") == ""


def test_normalize_param_name_matches_rules_convention() -> None:
    """本地复刻的 camel->snake 必须与 rules._camel_to_snake 对真实属性名一致。

    本体模块刻意不 import ``rules._camel_to_snake`` (私有符号无兼容承诺),
    代价是算法可能漂移; 这个用例就是漂移的哨兵 —— 两条归一路径对真实
    shape 的全部 ``sh:path`` 必须给出同一结果, 否则本体里的属性名会和
    约束求值层对不上。
    """
    from matrag.domain.rules import _camel_to_snake as reference

    assert normalize_param_name("percentGaugeRR") == "percent_gauge_rr"
    assert normalize_param_name("contactResistance") == "contact_resistance"
    assert normalize_param_name("hysteresis") == "hysteresis"

    sh_path = URIRef("http://www.w3.org/ns/shacl#path")
    real_paths: set[str] = set()
    for rule in _REAL_RULES:
        if not rule.is_constraint:
            continue
        graph = Graph()
        graph.parse(data=rule.constraint["shape"], format="turtle")
        for _s, _p, obj in graph.triples((None, sh_path, None)):
            real_paths.add(str(obj).rsplit("#", 1)[-1])
    assert real_paths, "真实规则集应含 sh:path 声明"
    for path in sorted(real_paths):
        assert normalize_param_name(path) == reference(path), path


# --------------------------------------------------------------------------- #
# 3. validate() 真能检出 4+ 类问题 (构造 fixture, 不靠真实数据碰运气)
# --------------------------------------------------------------------------- #


def test_validate_detects_unit_conflict() -> None:
    """同一参数在两条规则里单位不同 → unit_conflict。"""
    rules = [
        derive_rule(
            "X-001",
            inputs=["voltage"],
            output="power",
            given={"voltage": 54},
            expect={"power": 54},
            variables={"voltage": "V", "power": "W"},
        ),
        derive_rule(
            "X-002",
            inputs=["voltage"],
            output="ripple",
            given={"voltage": 54},
            expect={"ripple": 54},
            variables={"voltage": "mV", "ripple": "mVpp"},
        ),
    ]
    issues = build_ontology(rules).validate()
    conflicts = [i for i in issues if i.kind == ISSUE_UNIT_CONFLICT]
    assert len(conflicts) == 1, [i.detail for i in issues]
    assert "voltage" in conflicts[0].involved
    assert "X-001" in conflicts[0].involved
    assert "X-002" in conflicts[0].involved
    assert "mV" in conflicts[0].detail and "V" in conflicts[0].detail


def test_validate_detects_unit_missing() -> None:
    """被 ≥2 条规则引用的参数没有声明单位 → unit_missing。"""
    rules = [
        constraint_rule(
            "Y-001",
            shape=SHAPE_WITH_TARGET,
            given={"trip_value": 1.0},
            category="protection",
        ),
        constraint_rule(
            "Y-002",
            shape=SHAPE_WITH_TARGET,
            given={"trip_value": 2.0},
            category="protection",
        ),
    ]
    onto = build_ontology(rules)
    issues = onto.validate()
    missing = [i for i in issues if i.kind == ISSUE_UNIT_MISSING]
    assert len(missing) == 1, [i.detail for i in issues]
    assert missing[0].involved[0] == "trip_value"
    assert onto.unit_of("trip_value") == ""
    assert onto.properties_without_unit() == ["trip_value"]


def test_camelcase_given_key_merges_with_shacl_path() -> None:
    """camelCase 的事实键与 camelCase 的 sh:path 归并成**同一个**属性。

    这是"同义参数名如何归并"的回归测试: 如果不做归一,
    ``tripValue`` 与 ``trip_value`` 会变成两个本体属性, 同一物理量被拆开。
    """
    rules = [
        constraint_rule(
            "N-001",
            shape=SHAPE_WITH_TARGET,
            given={"tripValue": 1.0},
            category="protection",
        )
    ]
    onto = build_ontology(rules)
    props = onto.properties()
    assert "trip_value" in props
    assert "tripValue" not in props
    assert props["trip_value"].origins == ("given_key", "shacl_path")
    assert props["trip_value"].declared_names == ("tripValue",)
    # 查询对 camelCase 容错。
    assert onto.unit_of("tripValue") == onto.unit_of("trip_value") == ""
    assert onto.link_of("N-001").consumes == ("trip_value",)


def test_constraint_verdict_is_not_a_dimensional_property() -> None:
    """约束型的 ``violation`` 是布尔判定, 不建成量纲属性 (不污染单位门)。"""
    rules = [
        constraint_rule("V-001", shape=SHAPE_WITH_TARGET, given={"trip_value": 1.0}),
        constraint_rule("V-002", shape=SHAPE_WITH_TARGET, given={"trip_value": 2.0}),
    ]
    onto = build_ontology(rules)
    assert "violation" not in onto.properties()
    assert onto.produces_of("V-001") == ["violation"]
    # TTL 里写成字面量 verdict, 不产生悬空属性引用。
    assert "power:verdict" in onto.to_turtle()


def test_validate_ignores_unit_missing_for_single_rule() -> None:
    """只被一条规则引用的参数缺单位不算 issue (避免噪声淹没真问题)。"""
    rules = [
        constraint_rule(
            "Y-010", shape=SHAPE_WITH_TARGET, given={"trip_value": 1.0}
        )
    ]
    onto = build_ontology(rules)
    assert [i for i in onto.validate() if i.kind == ISSUE_UNIT_MISSING] == []
    # 但该参数依然可见、单位依然为空串。
    assert onto.unit_of("trip_value") == ""
    assert onto.properties_without_unit() == ["trip_value"]


def test_validate_detects_unit_missing_declared_empty() -> None:
    """variables 里写了空串单位也算"未声明", 且 detail 能区分。"""
    rules = [
        derive_rule(
            "Y-020",
            inputs=["current"],
            output="power",
            given={"current": 1.0},
            expect={"power": 1.0},
            variables={"current": ""},
        ),
        derive_rule(
            "Y-021",
            inputs=["current"],
            output="power2",
            given={"current": 1.0},
            expect={"power2": 1.0},
        ),
    ]
    onto = build_ontology(rules)
    missing = [i for i in onto.validate() if i.kind == ISSUE_UNIT_MISSING]
    assert len(missing) == 1
    assert "声明为空串" in missing[0].detail


def test_validate_detects_unclassified_constraint() -> None:
    """约束型规则缺 sh:targetClass → unclassified_constraint。"""
    rules = [constraint_rule("Z-001", shape=SHAPE_WITHOUT_TARGET)]
    onto = build_ontology(rules)
    issues = onto.validate()
    unclassified = [i for i in issues if i.kind == ISSUE_UNCLASSIFIED_CONSTRAINT]
    assert len(unclassified) == 1
    assert unclassified[0].involved == ("Z-001",)
    assert onto.rules_for_class("Z-001") == []  # 挂不上去


def test_validate_detects_rule_inconsistent() -> None:
    """derive.inputs 与 test.given 键集不一致 → rule_inconsistent。"""
    rules = [
        derive_rule(
            "W-001",
            inputs=["voltage", "current"],
            output="power",
            given={"voltage": 54},  # 少了 current
            expect={"power": 599.4},
        )
    ]
    onto = build_ontology(rules)
    issues = onto.validate()
    bad = [i for i in issues if i.kind == ISSUE_RULE_INCONSISTENT]
    assert len(bad) == 1
    assert "W-001" in bad[0].involved
    assert "current" in bad[0].involved
    assert "inputs 中有而 given 中无" in bad[0].detail


def test_validate_detects_extra_given_key() -> None:
    """given 多出 inputs 没声明的键也要报 (方向反过来)。"""
    rules = [
        derive_rule(
            "W-002",
            inputs=["voltage"],
            output="power",
            given={"voltage": 54, "rogue": 1},
            expect={"power": 54},
        )
    ]
    issues = build_ontology(rules).validate()
    bad = [i for i in issues if i.kind == ISSUE_RULE_INCONSISTENT]
    assert len(bad) == 1
    assert "rogue" in bad[0].involved
    assert "given 中有而 inputs 中无" in bad[0].detail


def test_validate_detects_unparseable_shape() -> None:
    """shape 不是合法 Turtle → shape_unparseable, 且规则仍进本体 (不静默丢弃)。"""
    rules = [constraint_rule("Q-001", shape=SHAPE_BROKEN)]
    onto = build_ontology(rules)
    issues = onto.validate()
    broken = [i for i in issues if i.kind == ISSUE_SHAPE_UNPARSEABLE]
    assert len(broken) == 1
    assert broken[0].involved == ("Q-001",)
    # 规则本身没丢, 仍可查。
    assert "Q-001" in onto.rules()
    assert onto.parse_failures[0][0] == "Q-001"


def test_validate_detects_all_four_kinds_at_once() -> None:
    """一个 fixture 同时触发全部 5 类 issue, 证明它们互不吞并。"""
    rules = [
        # 单位冲突 (voltage: V vs mV)
        derive_rule(
            "M-001",
            inputs=["voltage"],
            output="power",
            given={"voltage": 54},
            expect={"power": 54},
            variables={"voltage": "V"},
        ),
        derive_rule(
            "M-002",
            inputs=["voltage"],
            output="ripple",
            given={"voltage": 54},
            expect={"ripple": 54},
            variables={"voltage": "mV"},
        ),
        # 单位缺失 (current 被两条规则引用, 都没单位)
        derive_rule(
            "M-003",
            inputs=["current"],
            output="loss",
            given={"current": 1.0},
            expect={"loss": 1.0},
        ),
        derive_rule(
            "M-004",
            inputs=["current"],
            output="heat",
            given={"current": 1.0},
            expect={"heat": 1.0},
        ),
        # 无法归类
        constraint_rule("M-005", shape=SHAPE_WITHOUT_TARGET),
        # 规则不自洽
        derive_rule(
            "M-006",
            inputs=["a", "b"],
            output="c",
            given={"a": 1},
            expect={"c": 3},
        ),
        # shape 不可解析
        constraint_rule("M-007", shape=SHAPE_BROKEN),
    ]
    onto = build_ontology(rules)
    kinds = {i.kind for i in onto.validate()}
    assert kinds == {
        ISSUE_UNIT_CONFLICT,
        ISSUE_UNIT_MISSING,
        ISSUE_UNCLASSIFIED_CONSTRAINT,
        ISSUE_RULE_INCONSISTENT,
        ISSUE_SHAPE_UNPARSEABLE,
    }
    assert onto.stats()["issue_count"] == len(onto.validate())


def test_validate_clean_fixture_is_empty() -> None:
    """干净 fixture → 空清单 = 通过 (空清单确实代表没问题)。"""
    rules = [
        derive_rule(
            "C-001",
            inputs=["voltage"],
            output="power",
            given={"voltage": 54},
            expect={"power": 54},
            variables={"voltage": "V", "power": "W"},
        ),
        constraint_rule(
            "C-002",
            shape=SHAPE_WITH_TARGET,
            given={"tripValue": 1.0},
            category="protection",
        ),
    ]
    onto = build_ontology(rules)
    assert onto.validate() == [], [i.detail for i in onto.validate()]


# --------------------------------------------------------------------------- #
# 4. Turtle 往返
# --------------------------------------------------------------------------- #


def test_turtle_parses_with_rdflib_and_roundtrips() -> None:
    """to_turtle() 必须能被 rdflib 重新解析, 且往返输出一致。"""
    onto = build_ontology(_REAL_RULES[:30])
    ttl = onto.to_turtle()
    graph = Graph()
    graph.parse(data=ttl, format="turtle")
    assert len(graph) > 0
    # 重新序列化与原文一致 (往返稳定)。
    assert graph.serialize(format="turtle") == ttl


def test_turtle_contains_expected_triples() -> None:
    """图里应能看到 owl:Class / 单位 / 规则个体 / 关联。"""
    onto = build_ontology(_REAL_RULES)
    ttl = onto.to_turtle()
    graph = Graph()
    graph.parse(data=ttl, format="turtle")
    power = Namespace(POWER_NS)
    category = Namespace(CATEGORY_NS)
    # 类与属性
    assert (power["Protection"], RDF.type, OWL.Class) in graph
    assert (power["voltage"], RDF.type, OWL.DatatypeProperty) in graph
    # 单位三元组
    assert (power["voltage"], power["hasUnit"], Literal("V")) in graph
    # 规则个体
    assert (
        power["Rule_k_elec_001"],
        power["ruleId"],
        Literal("K-ELEC-001"),
    ) in graph
    # 规则 ↔ 参数 / 类 / 分类 的关联
    assert (
        power["Rule_k_elec_001"],
        power["consumes"],
        power["voltage"],
    ) in graph
    assert (
        power["Rule_k_prot_001"],
        power["constrainsClass"],
        power["Protection"],
    ) in graph
    assert (
        power["Rule_k_prot_001"],
        power["inCategory"],
        category["protection"],
    ) in graph
    # 约束型产出写成字面量 verdict, 不指向不存在的属性
    assert (
        power["Rule_k_prot_001"],
        power["verdict"],
        Literal("violation"),
    ) in graph


# --------------------------------------------------------------------------- #
# 5. write_ontology 落盘
# --------------------------------------------------------------------------- #


def test_write_ontology_persists_and_is_readable(tmp_path: Path) -> None:
    """落盘后 .ttl / .json 都能读回, 内容与内存本体一致。"""
    rules = _REAL_RULES[:25]
    onto = build_ontology(rules)
    result = write_ontology(tmp_path, rules)
    ttl_path = Path(result["turtle"])
    json_path = Path(result["json"])
    assert ttl_path.is_file()
    assert json_path.is_file()
    assert result["out_dir"] == str(tmp_path)
    # TTL 可读回且与内存一致。
    assert ttl_path.read_text(encoding="utf-8") == onto.to_turtle()
    # JSON 可读回且与内存一致。
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded == onto.to_dict()
    assert result["issue_counts"] == onto.stats()["issue_counts"]


def test_write_ontology_defaults_to_generated_dir() -> None:
    """默认输出目录与 rules_loader 一致。"""
    assert DEFAULT_GENERATED_DIR.name == "generated"
    assert DEFAULT_GENERATED_DIR.parent.name == "domain"
    assert DEFAULT_GENERATED_DIR == (
        Path(__file__).resolve().parents[1] / "data" / "domain" / "generated"
    )


# --------------------------------------------------------------------------- #
# 6. 真实 132 条规则 —— 质量门全绿 (2026-10 单位修复后回归哨兵)
# --------------------------------------------------------------------------- #


def test_real_rule_set_shape_and_ontology_stats(real_ontology: Any) -> None:
    """真实数据: 132 条 → 44 个被测对象类型 + 26 个知识主题 = 70 个类。"""
    stats = real_ontology.stats()
    assert stats["rule_count"] == 132
    assert stats["derive_rule_count"] == 55
    assert stats["constraint_rule_count"] == 77
    assert stats["subject_class_count"] == 44
    assert stats["category_count"] == 26
    assert stats["class_count"] == 70
    assert stats["property_count"] == len(real_ontology.properties())
    assert stats["shape_parse_failure"] == 0


def test_real_quality_gate_is_clean(real_ontology: Any, real_issues: list) -> None:
    """真实数据质量门全绿 (2026-10 单位修复后, 原 16 条 issue → 0)。

    修复前: ``unit_conflict`` 5 (efficiency/rated_current/setpoint/
    droop_slope/components) + ``unit_missing`` 11 (被 ≥2 条规则引用却缺单位)。
    本用例为回归哨兵: 任一问题重新引入都会在此暴露。
    """
    counts: dict[str, int] = {}
    for issue in real_issues:
        counts[issue.kind] = counts.get(issue.kind, 0) + 1
    assert counts == {}, [i.detail for i in real_issues]
    assert real_ontology.stats()["issue_count"] == 0


def test_real_unit_conflicts_are_resolved(real_ontology: Any) -> None:
    """真实数据不再有单位冲突 (修复前 5 处: components/droop_slope/efficiency/
    rated_current/setpoint; 2026-10 统一单位后全消)。"""
    assert real_ontology.properties_with_unit_conflict() == []


def test_real_constraints_all_classified(real_ontology: Any) -> None:
    """真实数据里 77 条约束型规则全部能归到被测对象类型 (无孤儿)。"""
    constraint_rules = [
        link
        for link in real_ontology.rules().values()
        if link.kind == "constraint"
    ]
    assert len(constraint_rules) == 77
    unclassified = [r for r in constraint_rules if not r.class_names]
    assert unclassified == []


def test_real_derive_rules_are_self_consistent(real_ontology: Any) -> None:
    """真实数据里 55 条公式型规则的 inputs 与 given 完全一致。"""
    inconsistent = real_ontology.issues_by_kind().get(ISSUE_RULE_INCONSISTENT, [])
    assert inconsistent == []


def test_real_stats_issue_counts_embedded(real_ontology: Any) -> None:
    """stats() 自带真实质量门结果, 供 CLI/CI 直接判断。"""
    stats = real_ontology.stats()
    assert stats["issue_counts"] == {}
    assert stats["property_with_unit_conflict"] == 0
    assert stats["property_count"] == 267
    assert stats["property_with_unit"] == 155
    assert stats["property_without_unit"] == 112
    assert stats["shape_parse_failure"] == 0


def test_real_unit_missing_params_are_resolved(real_ontology: Any) -> None:
    """真实数据不再有被 ≥2 条规则引用却缺单位的参数 (修复前 11 个)。"""
    missing = [
        issue.involved[0]
        for issue in real_ontology.validate()
        if issue.kind == ISSUE_UNIT_MISSING
    ]
    assert missing == []


# --------------------------------------------------------------------------- #
# 7. 值对象
# --------------------------------------------------------------------------- #


def test_ontology_class_value_object() -> None:
    """OntologyClass 是不可变值对象, 带 to_dict。"""
    cls = OntologyClass(
        name="Protection",
        uri=POWER_NS + "Protection",
        axis="subject",
        source_uri=POWER_NS + "Protection",
        comment="被测对象类型",
        rule_ids=("K-PROT-001",),
    )
    assert cls.to_dict()["axis"] == "subject"
    with pytest.raises(AttributeError):
        cls.name = "x"  # type: ignore[misc]


def test_ontology_issue_value_object() -> None:
    """OntologyIssue.to_dict 可 JSON 序列化。"""
    issue = OntologyIssue(
        kind=ISSUE_UNIT_CONFLICT,
        detail="单位冲突",
        involved=("voltage", "X-001"),
    )
    assert json.loads(json.dumps(issue.to_dict()))["kind"] == ISSUE_UNIT_CONFLICT
