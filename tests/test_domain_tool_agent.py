"""tests for :mod:`matrag.domain.tool_agent`（Agent 工具循环）。

全部离线：模型用 Pydantic AI 的 ``FunctionModel`` 脚本化替身（按序返回预置的
工具调用/最终输出），规则库与术语表用真实数据；沙箱用真实 Monty；不联网。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from matrag.domain.tool_agent import (
    ToolAgentError,
    ToolSupportError,
    compute_with_agent,
    verify_tool_support,
)

#: 规则内链可推：540W × 90% → output_power=486 → /54V → output_current=9.0A。
Q_RULES = "输入功率540W、整机效率90%、输出电压54V，输出电流是多少？"
#: 规则库外：电能 = 54V × 11.1A × 2h = 1198.8Wh（需脚本）。
Q_SCRIPT = "输出电压54V、输出电流11.1A，连续工作2小时，求输出电能（Wh）。"


def _tool(name: str, args: dict[str, Any]) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args)])


def _final(**payload: Any) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name="final_result", args=payload)])


def _scripted_model(responses: list[ModelResponse]) -> FunctionModel:
    """按序返回预置响应的模型替身。"""
    seq = list(responses)

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert seq, "脚本化模型没有更多响应"
        return seq.pop(0)

    return FunctionModel(fn)


def test_rules_tool_chain_returns_traceable_value() -> None:
    """规则工具（中文术语目标）→ 引擎 2 跳得 9.0；输出可追溯、条目带逐跳轨迹。"""
    model = _scripted_model(
        [
            _tool("derive_with_rules", {"target": "输出电流"}),
            _final(value=9.0, target="output_current", method="rules", explanation="链式推导"),
        ]
    )
    result = compute_with_agent(Q_RULES, model=model)

    assert result is not None
    assert result.strategy == "agent"
    assert result.value == pytest.approx(9.0)
    assert result.method == "rules"
    assert [item["rule_id"] for item in result.entries] == ["K-PWR-126", "K-PWR-127"]
    assert all(item["source"] == "derivation-agent" for item in result.entries)
    assert result.llm_calls == 2
    assert any("derive_with_rules" in step for step in result.steps)


def test_script_tool_covers_out_of_rules_computation() -> None:
    """规则库外（电能）→ run_python 沙箱；条目标 llm-script 且注明非规格书数据。"""
    model = _scripted_model(
        [
            _tool(
                "run_python",
                {
                    "script": "E = V * I * t",
                    "inputs": {"V": 54, "I": 11.1, "t": 2},
                    "output_var": "E",
                },
            ),
            _final(value=1198.8, target="电能（Wh）", method="script", explanation="沙箱计算"),
        ]
    )
    result = compute_with_agent(Q_SCRIPT, model=model)

    assert result is not None
    assert result.value == pytest.approx(1198.8)
    assert result.method == "script"
    assert result.entries[0]["source"] == "llm-script"
    display = str(result.entries[0]["display"])
    assert "Monty 沙箱" in display and "非规格书数据" in display


def test_script_crosschecked_against_rule_chain() -> None:
    """输出变量恰为规则参数且可推 → 自动对拍并留痕（一致）。"""
    model = _scripted_model(
        [
            _tool(
                "run_python",
                {
                    "script": "output_current = P * eff / 100 / U",
                    "inputs": {"P": 540, "eff": 90, "U": 54},
                    "output_var": "output_current",
                },
            ),
            _final(value=9.0, target="output_current", method="script"),
        ]
    )
    result = compute_with_agent(Q_RULES, model=model)

    assert result is not None
    display = str(result.entries[0]["display"])
    assert "对拍（一致" in display
    assert any("对拍" in step for step in result.steps)


def test_fabricated_value_rejected() -> None:
    """输出数值不来自任何工具 → 拒答（fail-fast）。"""
    model = _scripted_model([_final(value=999.0, target="x", method="script")])

    with pytest.raises(ToolAgentError, match="无法追溯"):
        compute_with_agent(Q_RULES, model=model, max_retries=0)


def test_fabricated_value_retries_with_feedback_then_succeeds() -> None:
    """首次输出不可追溯 → 附纠错反馈重跑；第二次来自工具即成功。"""
    seen: list[list[ModelMessage]] = []
    responses = [
        _final(value=999.0, target="x", method="script"),
        _tool("derive_with_rules", {"target": "输出电流"}),
        _final(value=9.0, target="output_current", method="rules"),
    ]

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(list(messages))
        return responses.pop(0)

    result = compute_with_agent(Q_RULES, model=FunctionModel(fn), max_retries=1)

    assert result is not None and result.value == pytest.approx(9.0)
    assert len(seen) == 3
    assert any("纠错" in str(message) for message in seen[1])
    assert result.llm_calls == 3


def test_tool_args_accept_json_string_facts() -> None:
    """真实模型会把 facts 双重编码成 JSON 字符串 → 工具容忍并执行。"""
    model = _scripted_model(
        [
            _tool(
                "derive_with_rules",
                {"target": "output_current", "facts": '{"output_voltage": 54.0}'},
            ),
            _final(value=9.0, target="output_current", method="rules"),
        ]
    )
    result = compute_with_agent(Q_RULES, model=model)

    assert result is not None
    assert result.value == pytest.approx(9.0)


def test_tool_args_bad_shape_returns_ok_false() -> None:
    """facts 不是合法 JSON → ok=false（错误作为数据返回给模型，不抛异常）。"""
    model = _scripted_model(
        [
            _tool(
                "derive_with_rules",
                {"target": "output_current", "facts": "not json"},
            ),
            _final(value=None, target="output_current", method="none", explanation="缺值"),
        ]
    )
    result = compute_with_agent(Q_RULES, model=model)

    assert result is not None and result.value is None
    assert any("不是合法 JSON" in step for step in result.steps)


def test_tool_failure_then_recovery_keeps_audit() -> None:
    """工具失败返回 ok=false（不抛异常），模型换 run_python 恢复；失败也留审计。"""
    model = _scripted_model(
        [
            _tool("derive_with_rules", {"target": "不存在的东西"}),
            _tool(
                "run_python",
                {"script": "C = P / U", "inputs": {"P": 486, "U": 54}, "output_var": "C"},
            ),
            _final(value=9.0, target="C", method="script"),
        ]
    )
    result = compute_with_agent(Q_RULES, model=model)

    assert result is not None
    assert result.value == pytest.approx(9.0)
    assert [item["rule_id"] for item in result.entries] == ["script-1"]
    assert any("失败" in step for step in result.steps)


def test_rules_tool_rejects_fabricated_fact() -> None:
    """facts 编造题干未给出的前提（efficiency 未给 → 自设 100）→ ok=false 不采信。"""
    model = _scripted_model(
        [
            _tool(
                "derive_with_rules",
                {"target": "output_current", "facts": {"efficiency": 100}},
            ),
            _final(value=None, target="output_current", method="none", explanation="前提不足"),
        ]
    )
    result = compute_with_agent("输入功率100W、输出电压12V，输出电流是多少？", model=model)

    assert result is not None and result.value is None
    assert any("禁止编造前提" in step for step in result.steps)


def test_rules_tool_rejects_altered_fact() -> None:
    """facts 篡改题干已知值（efficiency 90 → 100）→ ok=false 不采信。"""
    model = _scripted_model(
        [
            _tool(
                "derive_with_rules",
                {"target": "输出电流", "facts": {"efficiency": 100}},
            ),
            _final(value=None, target="output_current", method="none", explanation="前提被拒"),
        ]
    )
    result = compute_with_agent(Q_RULES, model=model)

    assert result is not None and result.value is None
    assert any("不一致" in step for step in result.steps)


def test_script_tool_rejects_special_cased_undefined() -> None:
    """输出变量是规则参数、规则链结构可达但数值无定义（除零）→ 特判兜底被拒。"""
    model = _scripted_model(
        [
            _tool(
                "run_python",
                {
                    "script": (
                        "if output_voltage == 0:\n"
                        "    output_current = 0.0\n"
                        "else:\n"
                        "    output_current = output_power / output_voltage"
                    ),
                    "inputs": {"output_power": 90, "output_voltage": 0},
                    "output_var": "output_current",
                },
            ),
            _final(value=None, target="output_current", method="none", explanation="除零无定义"),
        ]
    )
    result = compute_with_agent("输出功率90W、输出电压0V，输出电流是多少？", model=model)

    assert result is not None and result.value is None
    assert any("无定义" in step and "拒绝采信" in step for step in result.steps)


def test_rejected_script_value_not_traceable() -> None:
    """被拒的脚本结果不进入可追溯集合（模型拿它当答案 → 拒答）。"""
    model = _scripted_model(
        [
            _tool(
                "run_python",
                {
                    "script": "output_current = 0.0",
                    "inputs": {"output_power": 90, "output_voltage": 0},
                    "output_var": "output_current",
                },
            ),
            _final(value=0.0, target="output_current", method="script", explanation="兜底值"),
        ]
    )

    with pytest.raises(ToolAgentError, match="无法追溯"):
        compute_with_agent("输出功率90W、输出电压0V，输出电流是多少？", model=model, max_retries=0)


def test_script_tool_rejects_altered_input_param() -> None:
    """inputs 键是规则参数时必须与题干已知值一致（54V 被篡改成 60V → 整次拒绝）。"""
    model = _scripted_model(
        [
            _tool(
                "run_python",
                {
                    "script": "output_current = 540 * 0.9 / output_voltage",
                    "inputs": {"output_voltage": 60},
                    "output_var": "output_current",
                },
            ),
            _final(value=None, target="output_current", method="none", explanation="被拒"),
        ]
    )
    result = compute_with_agent(Q_RULES, model=model)

    assert result is not None and result.value is None
    assert any("inputs" in step and "不一致" in step for step in result.steps)


def test_script_tool_rejects_mismatch_with_rule_chain() -> None:
    """脚本结果与规则链对拍不一致 → 拒绝采信（规则链为准）。"""
    model = _scripted_model(
        [
            _tool(
                "run_python",
                {
                    "script": "output_power = 999",
                    "inputs": {},
                    "output_var": "output_power",
                },
            ),
            _final(value=None, target="output_power", method="none", explanation="不一致被拒"),
        ]
    )
    result = compute_with_agent(Q_RULES, model=model)

    assert result is not None and result.value is None
    assert any("不一致" in step and "拒绝采信" in step for step in result.steps)


def test_script_tool_rejects_unreachable_param_formula() -> None:
    """输出变量是规则参数但规则链不可达（Q08 实机套路）→ 脚本自创公式被拒。"""
    model = _scripted_model(
        [
            _tool(
                "run_python",
                {
                    "script": "output_power = input_power\noutput_current = output_power / output_voltage",
                    "inputs": {"input_power": 100, "output_voltage": 12},
                    "output_var": "output_current",
                },
            ),
            _final(value=None, target="output_current", method="none", explanation="规则链不可达"),
        ]
    )
    result = compute_with_agent("输入功率100W、输出电压12V，输出电流是多少？", model=model)

    assert result is not None and result.value is None
    assert any("自创公式" in step for step in result.steps)


def test_misattributed_param_target_rejected() -> None:
    """脚本值改名输出后 target 仍冒充规则参数 → 归属校验拒答（fail-fast）。"""
    value = 100 / 12
    model = _scripted_model(
        [
            _tool(
                "run_python",
                {
                    "script": "current_a = input_power / output_voltage",
                    "inputs": {"input_power": 100, "output_voltage": 12},
                    "output_var": "current_a",
                },
            ),
            _final(value=value, target="output_current", method="script"),
        ]
    )

    with pytest.raises(ToolAgentError, match="规则参数不得用脚本自创公式"):
        compute_with_agent("输入功率100W、输出电压12V，输出电流是多少？", model=model, max_retries=0)


def test_misattributed_then_relabeled_recovers() -> None:
    """归属校验附纠错反馈：模型把 target 改写成非规则参数名后通过。"""
    value = 100 / 12
    seen: list[list[ModelMessage]] = []
    responses = [
        _tool(
            "run_python",
            {
                "script": "current_a = input_power / output_voltage",
                "inputs": {"input_power": 100, "output_voltage": 12},
                "output_var": "current_a",
            },
        ),
        _final(value=value, target="output_current", method="script"),
        _final(value=value, target="估算电流（A，非规则参数）", method="script"),
    ]

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(list(messages))
        return responses.pop(0)

    result = compute_with_agent(
        "输入功率100W、输出电压12V，输出电流是多少？", model=FunctionModel(fn), max_retries=1
    )

    assert result is not None and result.value == pytest.approx(value)
    assert len(seen) == 3
    assert any("纠错" in str(message) for message in seen[2])
    assert result.target == "估算电流（A，非规则参数）"


def test_renamed_script_estimate_for_unreachable_param_rejected() -> None:
    """改名输出 + 假设前提（Q08 实机套路二）→ 归属门 ③ 拒绝估算值。"""
    value = 100 / 12 * 1000
    model = _scripted_model(
        [
            _tool(
                "run_python",
                {
                    "script": "output_current_ma = input_power / output_voltage * 1000",
                    "inputs": {"input_power": 100, "output_voltage": 12},
                    "output_var": "output_current_ma",
                },
            ),
            _final(
                value=value,
                target="output_current_ma（输出电流，单位 mA）",
                method="script",
                explanation="按输出功率=输入功率假设估算",
            ),
        ]
    )

    with pytest.raises(ToolAgentError, match="规则链到达"):
        compute_with_agent("输入功率100W、输出电压12V，输出电流是多少？", model=model, max_retries=0)


def test_missing_llm_and_empty_facts_return_none() -> None:
    """LLM 未接线 / 题干无数值 → 返回 None（由调用方留痕，不抛错）。"""
    assert compute_with_agent(Q_RULES) is None
    assert compute_with_agent("输出电流是多少？", model=_scripted_model([])) is None


def test_invalid_arguments_raise_value_error() -> None:
    with pytest.raises(ValueError):
        compute_with_agent("", model=_scripted_model([]))
    with pytest.raises(ValueError):
        compute_with_agent(Q_RULES, model=_scripted_model([]), max_retries=-1)


def test_verify_tool_support_passes_when_model_calls_tool() -> None:
    model = _scripted_model(
        [_tool("ping", {}), ModelResponse(parts=[TextPart("pong")])]
    )
    verify_tool_support(model, cache=False)


def test_verify_tool_support_rejects_model_without_tool_calls() -> None:
    """模型只回文本不调工具 → 预检失败（无 tools 能力，拒绝启动）。"""
    model = _scripted_model([ModelResponse(parts=[TextPart("pong")])])

    with pytest.raises(ToolSupportError, match="不支持 tools"):
        verify_tool_support(model, cache=False)


def test_verify_tool_support_wraps_call_failure() -> None:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("boom")

    with pytest.raises(ToolSupportError, match="预检失败"):
        verify_tool_support(FunctionModel(fn), cache=False)
