"""tests for :mod:`matrag.domain.query_derive`（问题文本 → 确定性多跳推导）。

全部离线: 用真实规则库 + 真实术语表; 不调 LLM、不联网。
"""

from __future__ import annotations

import pytest

from matrag.domain.query_derive import (
    QueryDerivation,
    derivation_entries,
    derive_from_text,
    facts_pool,
)


def test_derives_current_from_power_and_efficiency() -> None:
    """Pin + η + Vout → Iout: 两跳链 (K-PWR-126 → K-PWR-127)。"""
    d = derive_from_text("输入功率100W、整机效率90%、输出电压12V，输出电流是多少？")
    assert d is not None and isinstance(d, QueryDerivation)
    assert d.target == "output_current"
    assert d.derivation.value == pytest.approx(7.5)
    assert [s.rule_id for s in d.derivation.plan] == ["K-PWR-126", "K-PWR-127"]


def test_derives_efficiency_across_name_views() -> None:
    """跨命名视图: 54V/11.1A (通用量) + 644W → 整机效率。

    54×11.1=599.4W 经 K-ELEC-001 算出, 等价视图填给 output_power, 再由
    K-PWR-001 得 93.07%; 链路完全由规划器现场推导。
    """
    d = derive_from_text("输出电流11.1A、输出电压54V、输入功率644W，求整机效率")
    assert d is not None
    assert d.target == "efficiency"
    assert d.derivation.value == pytest.approx(93.07453416149069)
    assert [s.rule_id for s in d.derivation.plan] == ["K-ELEC-001", "K-PWR-001"]


def test_derives_generic_power_from_voltage_and_current() -> None:
    """通用"功率"也能作为目标 (经 T-POWER 术语落到 power 参数)。"""
    d = derive_from_text("已知电压 54V、电流 11.1A，求功率")
    assert d is not None
    assert d.target == "power"
    assert d.derivation.value == pytest.approx(599.4)


def test_derives_resistance_from_voltage_and_power() -> None:
    """"功率20W" 作为输出侧数值入池后, 电阻经 K-ELEC-005 反解 = U²/P = 5Ω。

    (修复前: power 只在其它规则的输入侧出现, 池里拿不到 → 无法反解。)
    """
    d = derive_from_text("电压10V、功率20W，求电阻")
    assert d is not None
    assert d.target == "resistance"
    assert d.derivation.value == pytest.approx(5.0)
    assert [s.rule_id for s in d.derivation.plan] == ["K-ELEC-005"]


def test_facts_pool_includes_output_side_values() -> None:
    """已知值池包含输出侧参数 (power=20), 与输入侧绑定合并。"""
    pool = facts_pool("电压10V、功率20W，求电阻")
    assert pool["power"] == pytest.approx(20.0)
    assert pool["voltage"] == pytest.approx(10.0)


def test_returns_none_when_insufficient() -> None:
    """缺效率时不得硬推 (不猜、不假设) —— 目标无可行链 → None。"""
    assert derive_from_text("输入功率100W、输出电压12V，输出电流是多少") is None


def test_returns_none_without_numbers_or_target() -> None:
    assert derive_from_text("输出纹波超标可能是什么原因") is None


def test_entries_are_per_hop_and_auditable() -> None:
    """推导条目逐跳一条, 与求值同形 (rule_id/kind/display/mode/target/source)。"""
    d = derive_from_text("输入功率100W、整机效率90%、输出电压12V，输出电流是多少？")
    assert d is not None
    entries = derivation_entries(d.derivation)
    assert [e["rule_id"] for e in entries] == ["K-PWR-126", "K-PWR-127"]
    assert all(e["kind"] == "formula" for e in entries)
    assert all(e["source"] == "derivation" for e in entries)
    assert entries[0]["target"] == "output_power"
    assert entries[1]["mode"] == "forward"
    assert "→ output_current = 7.5" in entries[1]["display"]


def test_explicit_facts_override_text_extraction() -> None:
    """外部显式 facts 覆盖文本抽取: 文本缺效率, 由 facts 补上后仍可推导。"""
    d = derive_from_text(
        "输入功率100W、输出电压12V，输出电流是多少",
        facts={"efficiency": 90.0},
    )
    assert d is not None
    assert d.derivation.value == pytest.approx(7.5)


def test_deterministic_repeat() -> None:
    q = "输入功率100W、整机效率90%、输出电压12V，输出电流是多少？"
    first = derive_from_text(q)
    second = derive_from_text(q)
    assert first is not None and second is not None
    assert first.derivation.to_dict() == second.derivation.to_dict()
