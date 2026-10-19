"""tests for :mod:`matrag.domain.script_sandbox`（Monty 沙箱执行层）。

全部离线：真实 Monty（本地 Rust 解释器，无网络），不调 LLM；超时用例用 0.2s
限额保持测试快速。
"""

from __future__ import annotations

import pytest

from matrag.domain.script_sandbox import (
    MontySandbox,
    ScriptRejectedError,
    ScriptSandboxError,
)


def test_run_returns_output_value() -> None:
    """正常脚本：末行追加输出变量读出数值（电能 54×11.1×2）。"""
    outcome = MontySandbox().run("E = V * I * t", {"V": 54, "I": 11.1, "t": 2}, "E")

    assert outcome.value == pytest.approx(1198.8)
    assert outcome.output_var == "E"
    assert outcome.inputs == {"V": 54.0, "I": 11.1, "t": 2.0}


def test_run_collects_stdout_instead_of_process_stdout(capsys: pytest.CaptureFixture) -> None:
    """脚本内 print 被收集进 outcome.stdout，不污染进程 stdout。"""
    outcome = MontySandbox().run("print('dbg')\nE = 2 * 3", {}, "E")

    assert outcome.value == 6.0
    assert "dbg" in outcome.stdout
    assert capsys.readouterr().out == ""


def test_empty_script_rejected() -> None:
    with pytest.raises(ScriptRejectedError):
        MontySandbox().run("   ", {}, "E")


def test_bad_output_var_rejected() -> None:
    """输出变量名必须是合法且非关键字的标识符。"""
    with pytest.raises(ScriptRejectedError):
        MontySandbox().run("E = 1", {}, "1bad")
    with pytest.raises(ScriptRejectedError):
        MontySandbox().run("E = 1", {}, "for")


def test_non_numeric_input_rejected() -> None:
    with pytest.raises(ScriptRejectedError):
        MontySandbox().run("E = V", {"V": "54"}, "E")
    with pytest.raises(ScriptRejectedError):
        MontySandbox().run("E = V", {"V": float("nan")}, "E")
    with pytest.raises(ScriptRejectedError):
        MontySandbox().run("E = V", {"V": True}, "E")


def test_undefined_name_rejected() -> None:
    """脚本引用了未提供的名字（含输出变量未定义）→ 契约拒绝。"""
    with pytest.raises(ScriptRejectedError):
        MontySandbox().run("E = V * 2", {}, "E")


def test_undefined_output_var_rejected() -> None:
    with pytest.raises(ScriptRejectedError):
        MontySandbox().run("F = 1", {}, "E")


def test_syntax_error_rejected() -> None:
    with pytest.raises(ScriptRejectedError):
        MontySandbox().run("E = = 1", {}, "E")


def test_bool_output_rejected() -> None:
    """布尔不是数值输出（拒绝把判定当真值用）。"""
    with pytest.raises(ScriptRejectedError):
        MontySandbox().run("E = V > 1", {"V": 5}, "E")


def test_division_by_zero_is_sandbox_error() -> None:
    with pytest.raises(ScriptSandboxError):
        MontySandbox().run("E = 1 / 0", {}, "E")


def test_timeout_hits_limit() -> None:
    """死循环被解释器时间限额掐断（引擎可终止性不依赖脚本自觉）。"""
    sandbox = MontySandbox(timeout_secs=0.2)

    with pytest.raises(ScriptSandboxError):
        sandbox.run("while True:\n    pass\nE = 1", {}, "E")


def test_invalid_limits_raise_value_error() -> None:
    with pytest.raises(ValueError):
        MontySandbox(timeout_secs=0)
    with pytest.raises(ValueError):
        MontySandbox(max_memory_bytes=0)
