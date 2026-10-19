"""tests for :mod:`matrag.domain.llm_planner`（LLM 编排多跳推导）。

全部离线: LLM 用脚本化替身 (按序返回预置 JSON / 异常), 规则库与术语表用真实
数据; 不联网。
"""

from __future__ import annotations

import pytest

from matrag.domain.llm_planner import OrchestrationResult, orchestrate_from_text

Q1 = "输入功率100W、整机效率90%、输出电压12V，输出电流是多少？"


class ScriptedLLM:
    """按序返回预置结果的 LLM 替身 (dict = JSON 输出, Exception = 调用失败)。"""

    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def complete_json(
        self, system: str, user: str, *, schema_hint: str = "", max_tokens: int | None = None
    ) -> dict:
        self.calls.append(user)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]


def test_llm_forward_chain_executes() -> None:
    """LLM 正链 [K-PWR-126, K-PWR-127] → 引擎逐跳执行得 7.5A。"""
    llm = ScriptedLLM(
        [{"target": "output_current", "hops": ["K-PWR-126", "K-PWR-127"], "reason": "r"}]
    )
    result = orchestrate_from_text(Q1, llm=llm)
    assert result is not None
    assert result.strategy == "llm"
    assert result.target == "output_current"
    assert result.derivation.value == pytest.approx(7.5)
    assert result.llm_calls == 1
    assert [s.rule_id for s in result.derivation.plan] == ["K-PWR-126", "K-PWR-127"]


def test_llm_inversion_chain_executes() -> None:
    """LLM 指定 [K-PWR-001, K-PWR-127]: 引擎自动以"反解"方式执行第一跳。"""
    llm = ScriptedLLM(
        [{"target": "output_current", "hops": ["K-PWR-001", "K-PWR-127"], "reason": "r"}]
    )
    result = orchestrate_from_text(Q1, llm=llm)
    assert result is not None
    assert result.strategy == "llm"
    assert result.derivation.value == pytest.approx(7.5)
    assert result.derivation.plan[0].mode == "invert"
    assert result.derivation.plan[0].target == "output_power"


def test_unknown_rule_falls_back_to_planner() -> None:
    """幻觉规则 id → 校验失败 → 回退规划器 (结果仍正确, 留痕原因)。"""
    llm = ScriptedLLM([{"target": "output_current", "hops": ["K-NOPE-999"], "reason": "r"}])
    result = orchestrate_from_text(Q1, llm=llm)
    assert result is not None
    assert result.strategy == "planner(回退)"
    assert result.derivation.value == pytest.approx(7.5)
    assert "K-NOPE-999" in result.reason


def test_incomplete_hops_fall_back() -> None:
    """可选链输入不齐 (只有 127) → 执行失败 → 回退。"""
    llm = ScriptedLLM([{"target": "output_current", "hops": ["K-PWR-127"], "reason": "r"}])
    result = orchestrate_from_text(Q1, llm=llm)
    assert result is not None
    assert result.strategy == "planner(回退)"


def test_llm_exception_falls_back() -> None:
    llm = ScriptedLLM([RuntimeError("boom")])
    result = orchestrate_from_text(Q1, llm=llm)
    assert result is not None
    assert result.strategy == "planner(回退)"
    assert "RuntimeError" in result.reason


def test_fallback_disabled_returns_none() -> None:
    llm = ScriptedLLM([RuntimeError("boom")])
    assert orchestrate_from_text(Q1, llm=llm, fallback=False) is None


def test_null_target_returns_none_when_infeasible() -> None:
    """信息不足: LLM 给 null 目标 + 空 hops → 规划器也失败 → None (不猜)。"""
    llm = ScriptedLLM([{"target": None, "hops": [], "reason": "缺效率"}])
    q = "输入功率100W、输出电压12V，输出电流是多少"
    assert orchestrate_from_text(q, llm=llm) is None


def test_target_outside_rule_base_falls_back() -> None:
    llm = ScriptedLLM([{"target": "output_warp", "hops": ["K-PWR-126"], "reason": "r"}])
    result = orchestrate_from_text(Q1, llm=llm)
    assert result is not None
    assert result.strategy == "planner(回退)"
    assert "output_warp" in result.reason


def test_hops_shape_violations_fall_back() -> None:
    for bad in ({"target": "output_current", "hops": "K-PWR-126", "reason": "r"},
                {"target": "output_current", "hops": ["K-PWR-126", "K-PWR-126"], "reason": "r"},
                {"target": "output_current", "hops": ["K-PWR-126"] * 6, "reason": "r"}):
        llm = ScriptedLLM([bad])
        result = orchestrate_from_text(Q1, llm=llm)
        assert result is not None and result.strategy == "planner(回退)"


def test_max_depth_validation() -> None:
    llm = ScriptedLLM([])
    with pytest.raises(ValueError, match="max_depth"):
        orchestrate_from_text(Q1, llm=llm, max_depth=0)


def test_result_is_deterministic_and_serializable() -> None:
    plan = {"target": "output_current", "hops": ["K-PWR-126", "K-PWR-127"], "reason": "r"}
    first = orchestrate_from_text(Q1, llm=ScriptedLLM([dict(plan)]))
    second = orchestrate_from_text(Q1, llm=ScriptedLLM([dict(plan)]))
    assert first is not None and second is not None
    assert isinstance(first, OrchestrationResult)
    assert first.to_dict() == second.to_dict()
