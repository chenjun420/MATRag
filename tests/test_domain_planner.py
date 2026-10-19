"""tests for :mod:`matrag.domain.planner` (目标导向推导引擎)。

全部离线确定性: 用真实规则库 ``data/domain/generated/power_rules.yaml`` 做
真实链路用例, 用小 fixture 规则验证搜索/反解边界; 不调 LLM、不联网。
"""

from __future__ import annotations

import json
import math

import pytest

from matrag.domain.planner import (
    MODE_FORWARD,
    MODE_INVERT,
    Derivation,
    default_equivalences,
    derive_value,
    invert_expression,
    plan_derivation,
)
from matrag.domain.rules_loader import DomainRule, load_rules


def _rule(
    rule_id: str,
    output: str,
    inputs: tuple[str, ...],
    expr: str,
) -> DomainRule:
    """构造最小公式型规则 (专测规划/反解, 与真实规则集无关)。"""
    return DomainRule(
        rule_id=rule_id,
        statement=f"{output} = {expr}",
        category="test",
        scope="test",
        derive={"output": output, "expr": expr, "inputs": list(inputs)},
        variables={name: "" for name in (*inputs, output)},
    )


# --------------------------------------------------------------------------- #
# invert_expression
# --------------------------------------------------------------------------- #
def test_invert_solves_multiplication() -> None:
    assert invert_expression("a * b", "b", {"a": 2.0}, 10.0) == pytest.approx(5.0)


def test_invert_solves_numerator_with_constant() -> None:
    # a / b * 100 = 40, b=50 → a = 20
    assert invert_expression("a / b * 100", "a", {"b": 50.0}, 40.0) == pytest.approx(
        20.0
    )


def test_invert_solves_denominator() -> None:
    assert invert_expression("a / b", "b", {"a": 10.0}, 2.0) == pytest.approx(5.0)


def test_invert_solves_subtraction() -> None:
    assert invert_expression("a - b", "b", {"a": 10.0}, 4.0) == pytest.approx(6.0)


def test_invert_solves_power_root() -> None:
    assert invert_expression("a ** 2", "a", {}, 9.0) == pytest.approx(3.0)


def test_invert_rejects_multiple_occurrence() -> None:
    assert invert_expression("x * x", "x", {}, 4.0) is None


def test_invert_rejects_unknown_variable() -> None:
    assert invert_expression("a * b", "b", {}, 10.0) is None


def test_invert_rejects_zero_denominator() -> None:
    assert invert_expression("a / b", "a", {"b": 0.0}, 5.0) is None


def test_invert_rejects_zero_result_denominator_solve() -> None:
    assert invert_expression("a / b", "b", {"a": 1.0}, 0.0) is None


def test_invert_rejects_exponent_target() -> None:
    assert invert_expression("a ** b", "b", {"a": 2.0}, 8.0) is None


def test_invert_rejects_non_numeric_result() -> None:
    assert invert_expression("a * b", "b", {"a": 2.0}, "10") is None
    assert invert_expression("a * b", "b", {"a": 2.0}, True) is None


def test_invert_rejects_non_numeric_env() -> None:
    assert invert_expression("a * b", "b", {"a": "x"}, 10.0) is None


def test_invert_solves_nested_denominator() -> None:
    """嵌套分母: 100 / (a + b) = 2 → b = 20 (SymPy 求解 + 回代验证)。"""
    assert invert_expression("100 / (a + b)", "b", {"a": 30.0}, 2.0) == pytest.approx(
        20.0
    )


def test_invert_non_integer_root() -> None:
    """非完全平方根: a² = 2 → √2 (SymPy 实根 + 正根语义)。"""
    assert invert_expression("a ** 2", "a", {}, 2.0) == pytest.approx(math.sqrt(2.0))


def test_invert_odd_root_negative_result() -> None:
    """奇次幂负结果有唯一实根 (旧版一律拒绝; SymPy 版按实根求解并验证)。"""
    assert invert_expression("a ** 3", "a", {}, -8.0) == pytest.approx(-2.0)


def test_invert_even_power_negative_result_rejected() -> None:
    """偶次幂负结果无实根 → None (复数域拒绝)。"""
    assert invert_expression("a ** 2", "a", {}, -4.0) is None


def test_invert_rejects_non_whitelisted_structure() -> None:
    """白名单外的表达式结构 (如生成式和) → None, 不做任何变形。"""
    assert invert_expression("sum(t for t in xs) ** 2", "xs", {}, 4.0) is None


# --------------------------------------------------------------------------- #
# plan_derivation
# --------------------------------------------------------------------------- #
def test_plan_forward_chain() -> None:
    rules = [_rule("R-1", "b", ("a",), "a * 2"), _rule("R-2", "c", ("b",), "b + 1")]
    plan = plan_derivation("c", {"a"}, rules=rules)
    assert plan is not None
    assert [s.rule_id for s in plan] == ["R-1", "R-2"]
    assert [s.mode for s in plan] == [MODE_FORWARD, MODE_FORWARD]


def test_plan_invert_then_forward() -> None:
    """无任何"手写重组式": 反解 η=Pout/Pin×100 得到 Pout, 再正向算 Iout。"""
    rules = [
        _rule("E", "eta", ("p_out", "p_in"), "p_out / p_in * 100"),
        _rule("I", "i_out", ("p_out", "v_out"), "p_out / v_out"),
    ]
    plan = plan_derivation("i_out", {"p_in", "eta", "v_out"}, rules=rules)
    assert [s.rule_id for s in plan] == ["E", "I"]
    assert plan[0].mode == MODE_INVERT and plan[0].target == "p_out"
    assert plan[1].mode == MODE_FORWARD and plan[1].target == "i_out"


def test_plan_returns_none_when_insufficient() -> None:
    rules = [_rule("E", "eta", ("p_out", "p_in"), "p_out / p_in * 100")]
    assert plan_derivation("eta", {"p_in"}, rules=rules) is None


def test_plan_respects_max_depth() -> None:
    rules = [_rule("R-1", "b", ("a",), "a * 2"), _rule("R-2", "c", ("b",), "b + 1")]
    assert plan_derivation("c", {"a"}, rules=rules, max_depth=1) is None
    assert plan_derivation("c", {"a"}, rules=rules, max_depth=2) is not None


def test_plan_cycle_safe() -> None:
    rules = [_rule("R-1", "b", ("a",), "a + 1"), _rule("R-2", "a", ("b",), "b + 1")]
    assert plan_derivation("b", set(), rules=rules, max_depth=4) is None


def test_plan_max_depth_validation() -> None:
    with pytest.raises(ValueError, match="max_depth"):
        plan_derivation("x", set(), rules=[], max_depth=0)


# --------------------------------------------------------------------------- #
# derive_value (真实规则库)
# --------------------------------------------------------------------------- #
def test_real_chain_from_power_and_efficiency_to_current() -> None:
    """Pin + η + Vout → Iout (真实规则库, 多跳链式推导)。"""
    d = derive_value(
        "output_current",
        {"input_power": 100.0, "efficiency": 90.0, "output_voltage": 12.0},
    )
    assert d is not None
    assert d.value == pytest.approx(7.5)
    assert len(d.plan) == 2


def test_real_chain_without_manual_synonym_rules() -> None:
    """核心: 把"手写重组式" K-PWR-126 从规则集中剔除后, 引擎仍能推导出 7.5A
    —— 链路 (反解 K-PWR-001 → 正向 K-PWR-127) 是引擎按问题现场搜出来的,
    不是写死的。"""
    all_rules = {r.rule_id: r for r in load_rules()}
    reduced = [all_rules["K-PWR-001"], all_rules["K-PWR-127"]]
    d = derive_value(
        "output_current",
        {"input_power": 100.0, "efficiency": 90.0, "output_voltage": 12.0},
        rules=reduced,
    )
    assert d is not None
    assert d.value == pytest.approx(7.5)
    assert [s.rule_id for s in d.plan] == ["K-PWR-001", "K-PWR-127"]
    assert d.plan[0].mode == MODE_INVERT
    assert d.plan[0].target == "output_power"


def test_real_ohms_law_inversion_single_step() -> None:
    d = derive_value("current", {"power": 90.0, "voltage": 12.0})
    assert d is not None
    assert d.value == pytest.approx(7.5)
    assert [s.rule_id for s in d.plan] == ["K-ELEC-001"]
    assert d.plan[0].mode == MODE_INVERT


def test_real_resistance_root_inversion() -> None:
    d = derive_value("resistance", {"voltage": 10.0, "power": 20.0})
    assert d is not None
    assert d.value == pytest.approx(5.0)
    assert d.plan[0].mode == MODE_INVERT


def test_real_insufficient_facts_returns_none() -> None:
    assert (
        derive_value("output_current", {"input_power": 100.0, "output_voltage": 12.0})
        is None
    )


def test_real_derivation_is_deterministic_and_serializable() -> None:
    facts = {"input_power": 100.0, "efficiency": 90.0, "output_voltage": 12.0}
    first = derive_value("output_current", facts)
    second = derive_value("output_current", facts)
    assert first is not None and second is not None
    assert first.to_dict() == second.to_dict()
    assert json.loads(json.dumps(first.to_dict(), ensure_ascii=False))["value"] == 7.5
    assert isinstance(first, Derivation)


def test_execution_failure_returns_none() -> None:
    """规划可解但执行数值失败 (除零) → None, 不输出半成品。"""
    rules = [_rule("R-1", "y", ("a", "b"), "a / b")]
    # 反解 a: y=a/b, b=0 → 无解
    assert derive_value("a", {"y": 5.0, "b": 0.0}, rules=rules) is None


# --------------------------------------------------------------------------- #
# 等价视图 (output_x ↔ x, 由规则库命名自动推导)
# --------------------------------------------------------------------------- #
def test_default_equivalences_from_rule_base() -> None:
    """等价视图来自规则库命名本身, 不是手写清单; 输入侧不参与。"""
    rules = list(load_rules())
    eq = default_equivalences(rules)
    assert eq["output_power"] == ("power",)
    assert eq["power"] == ("output_power",)
    assert eq["output_voltage"] == ("voltage",)
    assert "input_power" not in eq  # 交流输入功率含 PF, 与通用 P=U×I 不等价


def test_derive_uses_equivalence_view_across_namespaces() -> None:
    """跨命名视图: 已知 power/voltage (通用量) → 推导 output_current = 7.5。"""
    d = derive_value("output_current", {"power": 90.0, "voltage": 12.0})
    assert d is not None
    assert d.value == pytest.approx(7.5)
    assert d.plan[0].rule_id == "K-ELEC-001"
    assert d.plan[0].mode == MODE_INVERT


def test_derive_equivalence_can_be_disabled() -> None:
    """显式关闭等价视图 (``equivalences={}``) → 跨命名推导不再成立。"""
    assert (
        derive_value(
            "output_current", {"power": 90.0, "voltage": 12.0}, equivalences={}
        )
        is None
    )


def test_derive_does_not_override_existing_values() -> None:
    """等价填充不覆盖已有值: output_power=90 已给, 旁证 power=123 不参与。"""
    d = derive_value(
        "loss", {"input_power": 100.0, "output_power": 90.0, "power": 123.0}
    )
    assert d is not None
    assert d.value == pytest.approx(10.0)
