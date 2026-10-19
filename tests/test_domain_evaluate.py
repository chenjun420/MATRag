"""tests for :mod:`matrag.domain.evaluate`（确定性规则求值服务）。

全部离线：只用真实规则集 ``data/domain/generated/power_rules.yaml`` 与
临时构造的 :class:`DomainRule`，不调 LLM、不联网。
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from matrag.domain.evaluate import (
    EvaluationResult,
    evaluate_for_answer,
    evaluate_hints,
    extract_facts,
    extract_output_values,
    parse_facts,
    render_evaluations,
)
from matrag.domain.rules_loader import DomainRule

ELEC = {"rule_id": "K-ELEC-001"}          # P = U × I（公式型, 输入 voltage/current）
RIPPLE = {"rule_id": "K-PWR-101"}         # 纹波公式（4 输入）
RIPPLE_MARK = {"rule_id": "K-PWR-102"}    # 纹波超标约束（bool 判定）
PROT = {"rule_id": "K-PROT-001"}          # 保护回差约束


def _fake_derive(
    rule_id: str = "K-TEST-001",
    inputs: tuple[str, ...] = ("v_in",),
    unit: str = "V",
    output: str = "v_out",
    expr: str = "v_in * 2",
) -> DomainRule:
    """构造一条最小公式型规则（专测抽取/绑定边界, 与真实规则集无关）。"""
    return DomainRule(
        rule_id=rule_id,
        statement="测试公式: v_out = v_in × 2",
        category="test",
        scope="test",
        derive={"output": output, "expr": expr, "inputs": list(inputs)},
        test={"given": {inputs[0]: 1}, "expect": {output: 2}},
        variables={name: unit for name in (*inputs, output)},
    )


# --------------------------------------------------------------------------- #
# parse_facts
# --------------------------------------------------------------------------- #
def test_parse_facts_scalar_types() -> None:
    facts = parse_facts(["voltage=54", "gain=0.5", "enabled=true", "mode=abc"])
    assert facts == {"voltage": 54.0, "gain": 0.5, "enabled": True, "mode": "abc"}


def test_parse_facts_bool_case_insensitive() -> None:
    assert parse_facts(["a=FALSE"]) == {"a": False}


def test_parse_facts_duplicate_last_wins() -> None:
    assert parse_facts(["voltage=54", "voltage=60"]) == {"voltage": 60.0}


def test_parse_facts_missing_equals_raises() -> None:
    with pytest.raises(ValueError, match="name=value"):
        parse_facts(["voltage54"])


def test_parse_facts_empty_name_raises() -> None:
    with pytest.raises(ValueError, match="缺少参数名"):
        parse_facts(["=54"])


# --------------------------------------------------------------------------- #
# evaluate_hints
# --------------------------------------------------------------------------- #
def test_evaluate_hints_formula_applicable() -> None:
    results = evaluate_hints([ELEC], {"voltage": 54, "current": 11.1})
    assert len(results) == 1
    result = results[0]
    assert result.applicable and result.ok
    assert result.actual == pytest.approx(599.4, rel=1e-9)
    assert result.unit == "W"
    assert result.facts_used == {"voltage": 54, "current": 11.1}
    assert "power" in result.display()


def test_evaluate_hints_constraint_violation_true() -> None:
    results = evaluate_hints([RIPPLE_MARK], {"measured_ripple_mvpp": 120, "ripple_limit_mvpp": 100})
    assert results[0].applicable and results[0].ok
    assert results[0].actual is True  # 违反
    assert "触发" in results[0].display()


def test_evaluate_hints_constraint_violation_false() -> None:
    results = evaluate_hints([RIPPLE_MARK], {"measured_ripple_mvpp": 80, "ripple_limit_mvpp": 100})
    assert results[0].applicable
    assert results[0].actual is False
    assert "未触发" in results[0].display()


def test_evaluate_hints_constraint_protection_hysteresis() -> None:
    results = evaluate_hints([PROT], {"trip": 12, "recovery": 10, "hysteresis": 3})
    assert results[0].applicable
    assert results[0].actual is True


def test_evaluate_hints_missing_inputs_listed_not_applicable() -> None:
    results = evaluate_hints([RIPPLE], {"switching_frequency": 150000})
    assert len(results) == 1
    result = results[0]
    assert result.applicable is False and result.ok is False
    assert set(result.missing_inputs) == {"ripple_current", "capacitance", "esr"}
    assert result.facts_used == {"switching_frequency": 150000}
    assert "未求值" in result.display()


def test_evaluate_hints_empty_facts_returns_empty() -> None:
    assert evaluate_hints([ELEC], {}) == []


def test_evaluate_hints_unknown_rule_marked() -> None:
    results = evaluate_hints([{"rule_id": "K-NOPE-001"}], {"voltage": 1})
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].error == "unknown_rule"
    assert "不存在" in results[0].detail


def test_evaluate_hints_duplicate_rule_ids_deduped() -> None:
    results = evaluate_hints([ELEC, dict(ELEC)], {"voltage": 54, "current": 11.1})
    assert len(results) == 1


def test_evaluate_hints_does_not_mutate_facts() -> None:
    facts = {"voltage": 54, "current": 11.1}
    evaluate_hints([ELEC], facts)
    assert facts == {"voltage": 54, "current": 11.1}


def test_evaluate_hints_deterministic_and_jsonable() -> None:
    first = evaluate_hints([ELEC], {"voltage": 54, "current": 11.1})[0].to_dict()
    second = evaluate_hints([ELEC], {"voltage": 54, "current": 11.1})[0].to_dict()
    assert first == second
    assert json.loads(json.dumps(first, ensure_ascii=False))["rule_id"] == "K-ELEC-001"


def test_render_evaluations_lines() -> None:
    lines = render_evaluations(evaluate_hints([ELEC], {"voltage": 54, "current": 11.1}))
    assert len(lines) == 1
    assert "K-ELEC-001" in lines[0]


# --------------------------------------------------------------------------- #
# extract_facts（查询文本 → 规则输入绑定）
# --------------------------------------------------------------------------- #
def test_extract_facts_binds_by_unit() -> None:
    bound = extract_facts("已知电压 54V、电流 11.1A，求功率", [ELEC])
    assert bound == {"K-ELEC-001": {"voltage": 54.0, "current": 11.1}}


def test_extract_facts_converts_khz() -> None:
    bound = extract_facts("开关频率 150kHz", [RIPPLE])
    assert bound["K-PWR-101"]["switching_frequency"] == pytest.approx(150000.0)


def test_extract_facts_binds_all_four_ripple_inputs() -> None:
    bound = extract_facts(
        "纹波电流 0.62A、开关频率 150kHz、输出电容 220uF、ESR 0.1Ω", [RIPPLE]
    )
    facts = bound["K-PWR-101"]
    assert facts["ripple_current"] == pytest.approx(0.62)
    assert facts["switching_frequency"] == pytest.approx(150000.0)
    assert facts["capacitance"] == pytest.approx(2.2e-4, rel=1e-9)
    assert facts["esr"] == pytest.approx(0.1)


def test_extract_facts_mvpp_to_v_pp() -> None:
    rule = _fake_derive(inputs=("ripple",), unit="V(pp)", output="doubled")
    bound = extract_facts("输出纹波 120mVpp", [{"rule_id": "K-TEST-001"}], rules=[rule])
    assert bound["K-TEST-001"]["ripple"] == pytest.approx(0.12)


def test_extract_facts_range_not_bound() -> None:
    rule = _fake_derive()
    bound = extract_facts("输入电压范围 85-300V", [{"rule_id": "K-TEST-001"}], rules=[rule])
    assert bound == {}


def test_extract_facts_negative_value_bound() -> None:
    rule = _fake_derive(unit="℃")
    bound = extract_facts("工作温度 -40℃", [{"rule_id": "K-TEST-001"}], rules=[rule])
    assert bound["K-TEST-001"]["v_in"] == pytest.approx(-40.0)


def test_extract_facts_ambiguous_same_unit_not_bound() -> None:
    rule = _fake_derive(inputs=("v_a", "v_b"), unit="V", output="v_out", expr="v_a + v_b")
    bound = extract_facts("电压 54V", [{"rule_id": "K-TEST-001"}], rules=[rule])
    assert bound == {}


def test_extract_facts_two_values_same_unit_not_bound() -> None:
    rule = _fake_derive()
    bound = extract_facts("输入 54V 和 60V", [{"rule_id": "K-TEST-001"}], rules=[rule])
    assert bound == {}


def test_extract_facts_unit_mismatch_no_binding() -> None:
    rule = _fake_derive()
    bound = extract_facts("频率 150kHz", [{"rule_id": "K-TEST-001"}], rules=[rule])
    assert bound == {}


def test_extract_facts_no_numbers_returns_empty() -> None:
    assert extract_facts("输出纹波超标可能是什么原因", [RIPPLE]) == {}


# --------------------------------------------------------------------------- #
# evaluate_for_answer
# --------------------------------------------------------------------------- #
def test_evaluate_for_answer_auto_extract_from_query() -> None:
    results = evaluate_for_answer([ELEC], query="已知电压 54V、电流 11.1A，求功率")
    assert len(results) == 1
    assert results[0].applicable
    assert results[0].actual == pytest.approx(599.4, rel=1e-9)


def test_evaluate_for_answer_explicit_facts_cover_unhinted_rule() -> None:
    results = evaluate_for_answer([], facts={"voltage": 54, "current": 11.1})
    ids = [result.rule_id for result in results]
    assert "K-ELEC-001" in ids  # 输入齐备的规则被补算


def test_evaluate_for_answer_empty_facts_disables() -> None:
    assert evaluate_for_answer([ELEC], facts={}) == []


def test_evaluate_for_answer_no_facts_no_query_returns_empty() -> None:
    assert evaluate_for_answer([ELEC]) == []


def test_evaluate_for_answer_query_without_numbers_returns_empty() -> None:
    assert evaluate_for_answer([ELEC], query="功率怎么算") == []


def test_evaluate_for_answer_dedupes_hint_and_covered() -> None:
    results = evaluate_for_answer([ELEC], facts={"voltage": 54, "current": 11.1})
    assert [result.rule_id for result in results].count("K-ELEC-001") == 1


def test_evaluation_result_frozen() -> None:
    result = evaluate_hints([ELEC], {"voltage": 54, "current": 11.1})[0]
    assert isinstance(result, EvaluationResult)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.rule_id = "other"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 2026-10-14 新增: 中文紧邻数字扫描 / 角色词守护 / 链式求值
# --------------------------------------------------------------------------- #
CHAIN_1 = {"rule_id": "K-PWR-126"}  # 输出功率 = 输入功率 × 效率/100 (重组式)
CHAIN_2 = {"rule_id": "K-PWR-127"}  # 输出电流 = 输出功率 / 输出电压 (重组式)


def test_extract_facts_cjk_adjacent_number_binds() -> None:
    """中文紧邻数字 (无空格, 如 "输入功率100W") 现在可扫描并绑定。"""
    bound = extract_facts("输入功率100W、整机效率90%，输出功率是多少", [CHAIN_1])
    assert bound == {"K-PWR-126": {"input_power": 100.0, "efficiency": 90.0}}


def test_extract_facts_role_word_guard_blocks_misbinding() -> None:
    """角色守护: "输入功率100W" 不得被错绑为 output_power (角色错配);
    output_voltage 属合法绑定, 但链路缺 output_power → 不产出任何计算值。"""
    bound = extract_facts("输入功率100W、输出电压12V", [CHAIN_2])
    assert bound == {"K-PWR-127": {"output_voltage": 12.0}}
    assert "output_power" not in bound.get("K-PWR-127", {})
    results = evaluate_for_answer(
        [CHAIN_2], query="输入功率100W、输出电压12V，输出电流是多少"
    )
    # 关键回归: 不得出现 100/12=8.33A 的错误计算值 (只剩"缺少输入"痕迹)。
    assert all(not r.applicable for r in results)
    assert all(
        r.rule_id != "K-PWR-127" or not r.applicable for r in results
    )


def test_extract_facts_role_word_present_still_binds() -> None:
    """角色词在场时照常绑定 (回归: 守护不能误杀正确场景)。"""
    bound = extract_facts("输出功率90W、输出电压12V", [CHAIN_2])
    assert bound == {"K-PWR-127": {"output_power": 90.0, "output_voltage": 12.0}}


def test_extract_facts_role_guard_uses_adjacent_label_not_window() -> None:
    """守护用"紧邻标签词": 隔句出现的角色词不算数。

    "输出电压54V、输入功率644W" 里 644W 的标签是"输入功率" → `output_power`
    被拒 (旧版固定宽度窗口会把更早的"输出"误判通过, 造成角色错配计算值)。
    """
    bound = extract_facts("输出电压54V、输入功率644W", [{"rule_id": "K-PWR-115"}])
    assert bound == {}
    # 标签就位时照常绑定
    ok = extract_facts("输出功率644W、输出电压54V", [{"rule_id": "K-PWR-115"}])
    assert ok == {"K-PWR-115": {"output_power": 644.0}}


# --------------------------------------------------------------------------- #
# extract_output_values（公式输出参数绑定, 供 facts_pool 补全已知值池）
# --------------------------------------------------------------------------- #
def test_extract_output_values_binds_real_output_param() -> None:
    """"功率20W" 是 K-ELEC-005 的输出参数 power (P=U²/R): 输出侧绑定可见。"""
    values = extract_output_values("电压10V、功率20W")
    assert values["power"] == 20.0
    assert values["voltage"] == 10.0  # voltage 同为多条规则的输出参数


def test_extract_output_values_skips_role_labeled_quantities() -> None:
    """带 输入/输出 标签的数量不参与输出侧绑定 (角色污染防护)。

    "输入功率100W" 若被绑到通用 ``power``, U·I=P 会反解出与效率语义矛盾
    的捷径值 (8.33A 回归教训); 这类数量已由输入侧绑定/角色守护负责。
    """
    assert extract_output_values("输入功率100W、输出电压12V") == {}
    # 无角色词的数值照常绑定, 但 output_ 前缀参数仍受角色词守护
    ok = extract_output_values("功率90W")
    assert ok["power"] == 90.0
    assert "output_power" not in ok


def test_extract_output_values_two_values_same_unit_not_bound() -> None:
    """同底单位多个数值 → 无法配对, 一律不绑 (保守)。"""
    values = extract_output_values("功率20W、功率30W")
    assert "power" not in values


def test_extract_output_values_no_numbers_returns_empty() -> None:
    assert extract_output_values("输出纹波超标可能是什么原因") == {}


def test_evaluate_for_answer_chains_two_hops_from_facts() -> None:
    """显式 facts 三输入 → 链式两跳: 126 得 Pout=90W, 127 接力得 Iout=7.5A。"""
    results = evaluate_for_answer(
        None,
        facts={"input_power": 100.0, "efficiency": 90.0, "output_voltage": 12.0},
    )
    by_id = {r.rule_id: r for r in results}
    assert by_id["K-PWR-126"].applicable
    assert by_id["K-PWR-126"].actual == pytest.approx(90.0)
    assert by_id["K-PWR-127"].applicable
    assert by_id["K-PWR-127"].actual == pytest.approx(7.5)


def test_evaluate_for_answer_chains_on_query_text() -> None:
    """查询文本路径: 绑定合并后按提示规则链式; 输出电流=7.5 而非错绑的 8.33。"""
    query = "输入功率100W、整机效率90%、输出电压12V，输出电流是多少"
    results = evaluate_for_answer([CHAIN_1, CHAIN_2], query=query)
    by_id = {r.rule_id: r for r in results}
    assert by_id["K-PWR-126"].actual == pytest.approx(90.0)
    assert by_id["K-PWR-127"].actual == pytest.approx(7.5)


def test_evaluate_for_answer_max_depth_one_disables_chaining() -> None:
    """max_depth=1 → 单轮: Pout 可算, 依赖它的 K-PWR-127 不接力。"""
    results = evaluate_for_answer(
        None,
        facts={"input_power": 100.0, "efficiency": 90.0, "output_voltage": 12.0},
        max_depth=1,
    )
    ids = {r.rule_id for r in results}
    assert "K-PWR-126" in ids
    assert "K-PWR-127" not in ids


def test_evaluate_for_answer_max_depth_validation() -> None:
    """max_depth<1 → ValueError (fail-fast, 不静默失效)。"""
    with pytest.raises(ValueError, match="max_depth"):
        evaluate_for_answer(
            [CHAIN_1], facts={"input_power": 1.0, "efficiency": 90.0}, max_depth=0
        )
