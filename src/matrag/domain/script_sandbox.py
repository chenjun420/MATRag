"""LLM 生成脚本的沙箱执行层（Monty 后端 + 可插拔运行接口）。

对应「LLM 写脚本」信任分级 T2 的执行侧：

- **隔离交给成熟组件**: `pydantic-monty`（Rust 解释器 + worker 进程池）——沙箱内
  无文件系统/网络/环境变量访问，时间与内存限额由解释器强制；本模块不做任何自研
  AST 白名单，只做“契约层”校验与错误归类。
- **契约 fail-fast**: 输入必须是 `参数名(标识符) -> 有限实数`；``output_var`` 必须
  是标识符；返回值必须是有限实数。契约违规 → :class:`ScriptRejectedError`；执行期失败
  （除零/超时/崩溃）→ :class:`ScriptSandboxError`。两类错误都原样抛给调用方，
  本层不吞错、不降级。
- **可插拔**: :class:`ScriptRunner` 定义运行接口（未来可换 CPython-WASI + wasmtime
  后端而不改调用方）。
"""

from __future__ import annotations

import keyword
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import pydantic_monty as monty

from matrag.errors import MatragError

__all__ = [
    "DEFAULT_MAX_MEMORY_BYTES",
    "DEFAULT_TIMEOUT_SECS",
    "MontySandbox",
    "SandboxOutcome",
    "ScriptRejectedError",
    "ScriptRunner",
    "ScriptSandboxError",
]

#: 单次脚本执行的墙钟上限（秒）。
DEFAULT_TIMEOUT_SECS = 3.0
#: 单次脚本执行的沙箱堆内存上限（字节，64 MiB）。
DEFAULT_MAX_MEMORY_BYTES = 64 * 1024 * 1024


class ScriptSandboxError(MatragError):
    """沙箱执行失败（运行期异常/超时/崩溃）：脚本合法但没跑出结果。"""


class ScriptRejectedError(MatragError):
    """沙箱契约违规（语法错误/未定义名字/非数值输出/非法输入）：拒绝执行或拒绝结果。"""


@dataclass(frozen=True)
class SandboxOutcome:
    """一次成功的沙箱执行结果。

    ``stdout`` 收集脚本内的 ``print`` 输出（不落进程 stdout，供审计/调试）。
    """

    value: float
    output_var: str
    script: str
    inputs: dict[str, float]
    stdout: str = ""


class ScriptRunner(Protocol):
    """脚本运行接口（可插拔后端契约）。"""

    def run(
        self, script: str, inputs: Mapping[str, float], output_var: str
    ) -> SandboxOutcome:
        """在沙箱中执行 ``script``，返回 ``output_var`` 的数值结果。"""
        ...


def _check_identifier(name: object, *, what: str) -> str:
    """校验名字是合法且非关键字的标识符（fail-fast）。"""
    text = str(name).strip()
    if not text.isidentifier() or keyword.iskeyword(text):
        raise ScriptRejectedError(f"{what} 必须是合法标识符: {name!r}")
    return text


class MontySandbox:
    """Monty 后端：纯计算脚本，无任何宿主能力（文件/网络/环境变量）。

    Args:
        timeout_secs: 单次执行墙钟上限（秒，> 0）。
        max_memory_bytes: 沙箱堆内存上限（字节，> 0）。

    Raises:
        ValueError: 任一参数非正数。
    """

    def __init__(
        self,
        *,
        timeout_secs: float = DEFAULT_TIMEOUT_SECS,
        max_memory_bytes: int = DEFAULT_MAX_MEMORY_BYTES,
    ) -> None:
        if (
            isinstance(timeout_secs, bool)
            or not isinstance(timeout_secs, (int, float))
            or timeout_secs <= 0
        ):
            raise ValueError(f"timeout_secs 须为 > 0 的数值: {timeout_secs!r}")
        if (
            isinstance(max_memory_bytes, bool)
            or not isinstance(max_memory_bytes, int)
            or max_memory_bytes <= 0
        ):
            raise ValueError(f"max_memory_bytes 须为 > 0 的整数: {max_memory_bytes!r}")
        self._timeout = float(timeout_secs)
        self._max_memory = int(max_memory_bytes)

    def run(
        self, script: str, inputs: Mapping[str, float], output_var: str
    ) -> SandboxOutcome:
        """执行脚本并取回 ``output_var`` 的数值（见 :class:`ScriptRunner`）。

        Raises:
            ScriptRejectedError: 契约违规（空脚本/非法名字/非法输入/非数值或未定义输出）。
            ScriptSandboxError: 执行期失败（除零/超时/沙箱崩溃等）。
        """
        if not isinstance(script, str) or not script.strip():
            raise ScriptRejectedError("script 不能为空")
        out_name = _check_identifier(output_var, what="output_var")
        if not isinstance(inputs, Mapping):
            raise ScriptRejectedError(f"inputs 须为 映射: {inputs!r}")
        clean: dict[str, float] = {}
        for key, value in inputs.items():
            name = _check_identifier(key, what="inputs 的键")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ScriptRejectedError(f"inputs[{name!r}] 必须是数值: {value!r}")
            number = float(value)
            if not math.isfinite(number):
                raise ScriptRejectedError(f"inputs[{name!r}] 必须是有限数值: {value!r}")
            clean[name] = number

        # 末行表达式即返回值：把 output_var 作为尾表达式读出结果（Monty 契约）。
        code = f"{script.rstrip()}\n{out_name}"
        printed: list[str] = []

        def _collect(stream: str, text: str) -> None:  # noqa: ARG001 —— 只收集正文
            printed.append(text)

        limits = {
            "max_feed_duration_secs": self._timeout,
            "max_memory": self._max_memory,
        }
        try:
            with monty.Monty() as pool:
                with pool.checkout(limits=limits) as session:
                    value = session.feed_run(
                        code, inputs=clean, print_callback=_collect
                    )
        except monty.MontySyntaxError as exc:
            raise ScriptRejectedError(f"脚本语法非法: {exc}") from exc
        except monty.MontyRuntimeError as exc:
            if isinstance(exc.exception(), NameError):
                raise ScriptRejectedError(
                    f"脚本引用了未定义名字（输出变量 {out_name!r} 必须已在脚本中定义）: {exc}"
                ) from exc
            raise ScriptSandboxError(f"沙箱执行失败: {exc}") from exc
        except monty.MontyError as exc:
            raise ScriptSandboxError(f"沙箱失败（{type(exc).__name__}）: {exc}") from exc

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ScriptRejectedError(f"输出变量 {out_name!r} 不是数值: {value!r}")
        result = float(value)
        if not math.isfinite(result):
            raise ScriptRejectedError(f"输出变量 {out_name!r} 不是有限数值: {value!r}")
        return SandboxOutcome(
            value=result,
            output_var=out_name,
            script=script,
            inputs=clean,
            stdout="".join(printed),
        )
