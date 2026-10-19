"""问题路由测试（P1-5 ``matrag.retrieve.routing``，全离线）。

覆盖: 各类关键词路由、legs 约定（FORMULA 带 graph 预留腿）、UNKNOWN 不抛、
等命中数的优先级、置信度、``build_where`` 直传、规则表导出、
"路由表不含任何具体型号/规格值"的防回退保护。
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from matrag.retrieve.query import (
    FILTER_DOC_ID,
    FILTER_SECTION,
    FILTER_SECTION_CONTAINS,
    FILTER_TABLE,
    ParsedQuery,
)
from matrag.retrieve.routing import (
    CATALOG_KEYWORDS,
    DEFAULT_LEGS,
    FAULT_KEYWORDS,
    FORMULA_KEYWORDS,
    GRAPH_LEGS,
    PROCEDURE_KEYWORDS,
    SPEC_VALUE_KEYWORDS,
    QueryKind,
    RoutedQuery,
    build_where,
    kind_legs,
    route,
    routing_rules,
)


class FakeLLM:
    """鸭子类型 LLM 替身: 记录调用并按脚本返回。"""

    def __init__(self, payload: Any = None, *, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, system: str, user: str, schema_hint: str = "") -> Any:
        self.calls.append({"system": system, "user": user, "schema_hint": schema_hint})
        if self.error is not None:
            raise self.error
        return self.payload


# --------------------------------------------------------------------------- 各类路由


@pytest.mark.parametrize(
    ("query", "kind"),
    [
        ("输出电压是多少", QueryKind.SPEC_VALUE),
        ("最大输出功率的额定值", QueryKind.SPEC_VALUE),
        ("效率指标达到多少", QueryKind.SPEC_VALUE),
        ("保持时间范围多大", QueryKind.SPEC_VALUE),
        ("怎么测输出特性", QueryKind.PROCEDURE),
        ("测试方法是什么", QueryKind.PROCEDURE),
        ("老化试验的步骤", QueryKind.PROCEDURE),
        ("如何做上电检查", QueryKind.PROCEDURE),
        ("效率计算公式", QueryKind.FORMULA),
        ("推导功率计算公式", QueryKind.FORMULA),
        ("占空比怎么算", QueryKind.FORMULA),
        ("损耗换算成热阻", QueryKind.FORMULA),
        ("输出电压故障", QueryKind.FAULT),
        ("模块不良分析", QueryKind.FAULT),
        ("无输出异常", QueryKind.FAULT),
        ("过压保护动作原因", QueryKind.FAULT),
        ("有哪些型号", QueryKind.CATALOG),
        ("全部型号列表", QueryKind.CATALOG),
        ("保护功能有几种", QueryKind.CATALOG),
    ],
)
def test_keyword_routing(query: str, kind: QueryKind) -> None:
    """各类型关键词 → 对应 QueryKind，且 legs 符合约定。"""
    routed = route(query)
    assert routed.kind is kind
    assert routed.legs == kind_legs(kind)
    assert routed.confidence > 0
    assert routed.reason


def test_legs_convention() -> None:
    """只有 FORMULA 加 graph 预留腿，其余一律默认两腿。"""
    assert DEFAULT_LEGS == ("vector", "keyword")
    assert GRAPH_LEGS == ("vector", "keyword", "graph")
    for kind in QueryKind:
        legs = kind_legs(kind)
        assert legs[0] == "vector" and legs[1] == "keyword"
    assert kind_legs(QueryKind.FORMULA) == GRAPH_LEGS
    for kind in QueryKind:
        if kind is not QueryKind.FORMULA:
            assert kind_legs(kind) == DEFAULT_LEGS, kind


def test_unknown_does_not_raise_and_falls_back() -> None:
    """关键词全落空 → UNKNOWN + 默认两腿 + 置信度 0，不抛。"""
    routed = route("嗯……那个……你懂的吧")
    assert routed.kind is QueryKind.UNKNOWN
    assert routed.legs == DEFAULT_LEGS
    assert routed.confidence == 0.0
    assert "默认两腿" in routed.reason


def test_empty_query_does_not_raise() -> None:
    """空查询同样给 UNKNOWN，不抛。"""
    routed = route("")
    assert routed.kind is QueryKind.UNKNOWN
    assert routed.legs == DEFAULT_LEGS


def test_highest_hit_count_wins() -> None:
    """命中数多者胜（同为两条命中时按优先级表）。"""
    routed = route("计算 推导 公式 是多少")  # FORMULA 3 条 vs SPEC_VALUE 1 条
    assert routed.kind is QueryKind.FORMULA
    assert routed.confidence > route("计算").confidence


def test_priority_breaks_ties_fault_over_procedure() -> None:
    """等命中数时按优先级: FAULT(0) 高于 PROCEDURE(1)。"""
    routed = route("故障时怎么测")
    assert routed.kind is QueryKind.FAULT
    assert routed.confidence < 1.0
    assert "优先级" in routed.reason


def test_single_intent_gets_confidence_bonus() -> None:
    """单一意图比多意图置信度更高。"""
    single = route("故障")
    multi = route("故障时计算一下")
    assert single.confidence > multi.confidence


def test_confidence_is_bounded() -> None:
    """置信度永远落在 [0, 1]。"""
    for query in ("", "是多少", "故障 故障 故障 故障 故障 不良 异常", "怎么测 步骤 操作 流程"):
        assert 0.0 <= route(query).confidence <= 1.0


def test_routed_query_shape() -> None:
    """RoutedQuery 字段齐备（kind/legs/confidence/reason）。"""
    routed = route("效率计算公式")
    assert isinstance(routed, RoutedQuery)
    assert isinstance(routed.kind, QueryKind)
    assert isinstance(routed.legs, tuple)
    assert isinstance(routed.confidence, float)
    assert isinstance(routed.reason, str) and routed.reason


def test_query_kind_values() -> None:
    """QueryKind 取值稳定（标签直出，可直接进报告）。"""
    assert [k.value for k in QueryKind] == [
        "spec_value", "procedure", "formula", "fault", "catalog", "unknown",
    ]
    assert QueryKind.FORMULA == "formula"  # StrEnum


# --------------------------------------------------------------------------- LLM 补位


def test_llm_not_called_when_keywords_match() -> None:
    """关键词已命中 → 不调 LLM（哪怕传了 llm）。"""
    llm = FakeLLM({"kind": "fault"})
    routed = route("输出电压是多少", llm=llm)
    assert routed.kind is QueryKind.SPEC_VALUE
    assert llm.calls == []


def test_llm_fills_unknown_kind() -> None:
    """关键词全落空 + 传 llm → LLM 补判, 置信度中等。"""
    llm = FakeLLM({"kind": "fault"})
    routed = route("这块儿坏了哎", llm=llm)
    assert routed.kind is QueryKind.FAULT
    assert routed.legs == DEFAULT_LEGS
    assert routed.confidence == 0.6
    assert llm.calls and "坏" in llm.calls[0]["user"]


def test_llm_invalid_kind_is_rejected() -> None:
    """LLM 返回不在枚举内的类型 → 退回 UNKNOWN。"""
    routed = route("这块儿坏了哎", llm=FakeLLM({"kind": "quantum"}))
    assert routed.kind is QueryKind.UNKNOWN
    assert "quantum" in routed.reason


@pytest.mark.parametrize("payload", [["not", "dict"], None])
def test_llm_non_dict_response(payload: Any) -> None:
    """LLM 返回非 JSON 对象 → 退回 UNKNOWN 并说明。"""
    routed = route("这块儿坏了哎", llm=FakeLLM(payload))
    assert routed.kind is QueryKind.UNKNOWN
    assert "JSON" in routed.reason


def test_llm_failure_leaves_trace() -> None:
    """LLM 挂了 → UNKNOWN, reason 里留痕，不抛。"""
    llm = FakeLLM(error=RuntimeError("MaaS 配额耗尽"))
    routed = route("这块儿坏了哎", llm=llm)
    assert routed.kind is QueryKind.UNKNOWN
    assert "RuntimeError" in routed.reason


def test_llm_narrow_signature_supported() -> None:
    """鸭子类型: 只收 (system, user) 的替身也能补位。"""
    class NarrowLLM:
        def complete_json(self, system: str, user: str) -> dict[str, Any]:
            return {"kind": "formula"}

    assert route("这块儿坏了哎", llm=NarrowLLM()).kind is QueryKind.FORMULA


# --------------------------------------------------------------------------- build_where / 规则表


def test_build_where_passes_doc_id() -> None:
    """doc_id（型号）可等值过滤 → 直传。"""
    parsed = ParsedQuery(raw="x", filters={FILTER_DOC_ID: "PA601-D54A"})
    assert build_where(parsed) == {FILTER_DOC_ID: "PA601-D54A"}


def test_build_where_keeps_index_form_table_id() -> None:
    """索引口径表号（t<页>-<序号>）→ 转成 table_id 透传。"""
    parsed = ParsedQuery(raw="x", filters={FILTER_TABLE: "t0-6"})
    assert build_where(parsed) == {"table_id": "t0-6"}


def test_build_where_drops_unexpressible_conditions_with_trace() -> None:
    """口语表号（整数）在 where 里表达不了 → 丢弃但留痕（不静默）。

    章节号不再丢: 它转成 ``section_contains`` 包含过滤（比章节名更精确）。
    """
    notes: list[dict] = []
    parsed = ParsedQuery(
        raw="x", filters={FILTER_DOC_ID: "PA601-D54A", FILTER_SECTION: "4.3", FILTER_TABLE: 11}
    )
    assert build_where(parsed, notes=notes) == {
        FILTER_DOC_ID: "PA601-D54A",
        "section_contains": "4.3",
    }
    dropped = [n for n in notes if n["kind"] == "where_key_dropped"]
    assert {n["key"] for n in dropped} == {FILTER_TABLE}
    assert notes  # 不给 notes 时也能安静返回（调用方不关心）


def test_build_where_maps_section_and_section_contains() -> None:
    """章节号与章节名都转成 where 的 ``section_contains``（包含过滤，不再丢弃）。"""
    numbered = ParsedQuery(raw="x", filters={FILTER_SECTION: "4.3.2"})
    assert build_where(numbered) == {"section_contains": "4.3.2"}
    titled = ParsedQuery(raw="x", filters={FILTER_SECTION_CONTAINS: "输入特性"})
    assert build_where(titled) == {"section_contains": "输入特性"}
    # 章节号与章节名同时出现: 章节号更精确，优先; 章节名被合并并留痕
    notes: list[dict] = []
    both = ParsedQuery(
        raw="x",
        filters={FILTER_SECTION_CONTAINS: "输入特性", FILTER_SECTION: "4.3.1"},
    )
    assert build_where(both, notes=notes) == {"section_contains": "4.3.1"}
    merged = [n for n in notes if n["kind"] == "where_key_merged"]
    assert merged and merged[0]["key"] == FILTER_SECTION_CONTAINS


def test_build_where_empty_returns_none() -> None:
    """无可表达条件 → None（让 index 走全库检索）。"""
    assert build_where(ParsedQuery(raw="x")) is None
    assert build_where(ParsedQuery(raw="x", filters={})) is None


def test_routing_rules_table_is_exportable() -> None:
    """规则表可导出（CLI/报告/测试不必靠读代码还原规则）。"""
    rules = routing_rules()
    assert len(rules) == 5  # UNKNOWN 不是规则，是兜底
    by_kind = {rule["kind"]: rule for rule in rules}
    assert set(by_kind) == {"fault", "procedure", "formula", "catalog", "spec_value"}
    assert "graph" in by_kind["formula"]["legs"]
    assert all("graph" not in rule["legs"] for kind, rule in by_kind.items() if kind != "formula")
    # 优先级序号唯一且从 0 起
    assert sorted(rule["priority"] for rule in rules) == [0, 1, 2, 3, 4]


def test_routing_tables_contain_no_product_codes() -> None:
    """防回退保护: 关键词表里不得出现任何具体型号/规格值。

    判据（数据驱动，不写死型号字面量）: 关键词不得同时含数字与连字符——
    产品编号必然长这样，纯问法不会。
    """
    tables = (
        SPEC_VALUE_KEYWORDS, PROCEDURE_KEYWORDS, FORMULA_KEYWORDS,
        FAULT_KEYWORDS, CATALOG_KEYWORDS,
    )
    for table in tables:
        assert table, "关键词表不能为空"
        for keyword in table:
            assert not re.search(r"[0-9]", keyword), keyword  # 无规格值
            assert "-" not in keyword, keyword               # 无型号
            assert keyword.strip(), keyword


def test_routing_is_data_driven(monkeypatch: pytest.MonkeyPatch) -> None:
    """判定完全由表驱动: 换成只有一条规则的合成表后，旧关键词全部失效，
    新关键词立即生效——证明没有按 kind 写死的隐藏分支。"""
    from matrag.retrieve import routing

    synthetic = ((QueryKind.CATALOG, frozenset({"外星语"})),)
    monkeypatch.setattr(routing, "_KIND_PRIORITY", synthetic)
    monkeypatch.setattr(routing, "_PRIORITY_INDEX", {QueryKind.CATALOG: 0})

    assert route("外星语 是什么").kind is QueryKind.CATALOG
    assert route("输出电压是多少").kind is QueryKind.UNKNOWN  # 旧关键词已不在表里
    assert route("效率计算公式").kind is QueryKind.UNKNOWN
