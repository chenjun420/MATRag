"""Agent 工具循环（原生 tool calling + 规则推导 + Monty 沙箱脚本）。

三层信任阶梯的 L3 实现（由 Pydantic AI 承载原生 function calling）：

- 工具 ``derive_with_rules`` → 规则库确定性推导（L1 引擎，逐跳轨迹可审计；
  ``facts`` 仅可复述题干已解析的已知值，编造/篡改前提即整次调用被拒）；
- 工具 ``run_python`` → Monty 沙箱执行 LLM 生成的计算脚本（覆盖规则库外：
  单位换算/均值/条件逻辑等；``inputs`` 中的规则参数必须与题干已知值一致；
  输出恰为规则参数时与规则链强制对拍：规则链不可达、数值无定义
  （如除零——禁止特判/兜底绕过）或结果不一致时均拒绝采信）；
- 最终输出经**可追溯性校验**：``value`` 必须原样来自某次工具结果，否则拒答
  （fail-fast；被拒的工具结果不进入可追溯集合；target 解析为规则参数时还须
  通过归属校验：值来自规则链或与规则链对拍一致的脚本；不做“无 tools 能力”
  的降级路径）。

启动预检：:func:`verify_tool_support` 做一次最小原生工具往返，模型不支持 tools
能力即 :class:`ToolSupportError` 拒绝启动——服务启动时应显式调用；默认模型
（生产路径）在 :func:`compute_with_agent` 内也会自动预检，结果按模型名缓存
（整个进程只查一次）；注入模型（测试替身）跳过预检，由调用方自行决定。
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ToolCallPart

from matrag.config import get_settings
from matrag.domain.planner import (
    default_equivalences,
    derive_value,
    fill_equivalences,
    plan_derivation,
)
from matrag.domain.query_derive import derivation_entries, facts_pool, target_candidates
from matrag.domain.rules_loader import DomainRule, load_rules
from matrag.domain.script_sandbox import (
    MontySandbox,
    ScriptRejectedError,
    ScriptRunner,
    ScriptSandboxError,
)
from matrag.domain.terms import Term, load_terms
from matrag.domain.vocab import normalize_name
from matrag.errors import MatragError, ProviderNotConfiguredError

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

__all__ = [
    "AgentAnswer",
    "AgentComputation",
    "ToolAgentError",
    "ToolSupportError",
    "compute_with_agent",
    "verify_tool_support",
]

#: 输出值可追溯性容差（相对值）。
_TRACE_TOL = 1e-6
#: 默认纠错重试次数（value 无法追溯时附反馈重跑）。
DEFAULT_MAX_RETRIES = 1
#: 本编排策略在求值条目里的标记。
_STRATEGY = "agent"


class ToolSupportError(MatragError):
    """LLM 不支持原生 tools 调用（预检失败）——拒绝启动，无降级路径。"""


class ToolAgentError(MatragError):
    """Agent 工具循环运行失败（调用异常/输出无法追溯到工具结果）。"""


class AgentAnswer(BaseModel):
    """Agent 最终结构化输出（数值必须来自工具返回）。"""

    value: float | None = Field(
        default=None,
        description="数值结果；必须原样等于某次工具返回的 value；无法完成时置 null",
    )
    target: str = Field(default="", description="目标说明（参数名或术语）")
    method: Literal["rules", "script", "none"] = Field(
        default="none", description="数值来源：rules=规则库推导；script=沙箱脚本；none=未完成"
    )
    explanation: str = Field(default="", description="中文一句话说明依据/缺口")


@dataclass
class _AgentDeps:
    """一次 Agent 运行的上下文（工具产物与审计轨迹都落在这里）。"""

    rules: list[DomainRule]
    terms: Sequence[Term] | None
    facts: dict[str, Any]
    sandbox: ScriptRunner
    param_names: frozenset[str]
    observed: list[float] = field(default_factory=list)
    rule_results: dict[str, float] = field(default_factory=dict)
    rule_entries: list[dict[str, Any]] = field(default_factory=list)
    script_entries: list[dict[str, Any]] = field(default_factory=list)
    audit: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentComputation:
    """一次 Agent 工具循环的结果（供应答层合并）。

    Attributes:
        strategy: 固定 ``"agent"``（与 llm_planner 的 ``"llm"``/``"planner(回退)"`` 区分）。
        target: 目标参数名/术语。
        value: 数值结果；``None`` 表示未完成（见 ``reason``）。
        method: ``"rules"`` / ``"script"`` / ``"none"``。
        entries: 与求值通道同形的逐跳条目（规则来源 ``derivation-agent``，
            脚本来源 ``llm-script``）。
        steps: 工具调用审计轨迹。
        llm_calls: 发生的模型请求次数。
        reason: Agent 的中文解释（或未完成原因）。
    """

    strategy: str
    target: str
    value: float | None
    method: str
    entries: list[dict[str, Any]]
    steps: list[str]
    llm_calls: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict。"""
        return {
            "strategy": self.strategy,
            "target": self.target,
            "value": self.value,
            "method": self.method,
            "entries": [dict(item) for item in self.entries],
            "steps": list(self.steps),
            "llm_calls": self.llm_calls,
            "reason": self.reason,
        }


_SYSTEM_PROMPT = """\
你是电子产品产测计算 Agent。给定问题与已知数值，你的职责是求出问题要求的数值。

# 可用工具（所有数值必须来自工具返回，严禁心算/编造）
1. `derive_with_rules(target, facts)`：按领域规则库做确定性推导（**首选**）。
   - `target`：参数名（如 output_current）或术语名（如 输出电流）；
   - `facts`：补充的 参数名→数值 映射（题干已解析的已知值引擎会自动带入）。
2. `run_python(script, inputs, output_var)`：在沙箱执行纯计算脚本（仅当规则库
   覆盖不了时使用：单位换算/均值/条件逻辑等）。
   - `script`：仅含赋值与算术的极短脚本（禁止 import/函数定义/循环/print）；
   - `inputs`：脚本变量→数值；`output_var`：输出变量名（脚本中已定义）。

# 硬性规则
1. 先想清楚“用哪条规则/怎么算”再调用工具；每次工具返回都要检查是否符合预期。
2. 规则库能推的必须用 derive_with_rules（可多轮、可组合多目标）；推不出才写脚本。
3. 收尾输出结构化结果：value 必须**原样等于**某次工具返回的数值（不改写、
   不四舍五入）；method 标注来源（rules/script）；target 写目标参数/术语；
   explanation 用中文说明依据。
4. 信息不足/无法完成：value 置 null，explanation 说明缺什么。
5. facts/inputs 只允许复述"已知值"里已给出的条目（名字与数值都要一致）；
   严禁编造、篡改或替换前提——引擎会逐一校验，违者整次调用被拒。
6. 规则链在给定数值下无定义（如除数为 0）时：如实置 value=null 说明无法计算；
   严禁用脚本特判/兜底（条件分支替换、极大值、inf/nan 之类）绕过。
   当 output_var 是规则参数名时，脚本结果会与规则链对拍：无定义场景与
   不一致的结果都会被拒绝。
7. 规则参数（规则清单里的参数名，如 output_current）的值只能来自
   derive_with_rules 的规则链。规则链不可达/无定义时如实拒答（value=null）；
   严禁输出任何数值估算——即使注明"假设""仅供参考"，也视为编造前提并会被
   归属校验拦下；也不许把输出变量改名冒充该参数值（target 指向规则参数时
   会做归属校验）。规则库外的量（单位换算/均值/组合量）才用 run_python，
   且输出变量取带单位后缀的新名字（如 output_current_ma），target 也写成
   对应的新名字。
"""


def _catalog_block(rules: Sequence[DomainRule]) -> str:
    """规则库清单（只有公式型；每条一行：``规则id: 输出 = 表达式``）。"""
    lines = ["# 规则库清单（公式型规则）"]
    for rule in sorted(
        (r for r in rules if r.is_derive and r.output_key), key=lambda r: r.rule_id
    ):
        expr = str((rule.derive or {}).get("expr", "")).strip()
        lines.append(f"- {rule.rule_id}: {rule.output_key} = {expr}")
    return "\n".join(lines)


def _resolve_target(
    target: str,
    rules: Sequence[DomainRule],
    terms: Sequence[Term] | None,
    param_names: frozenset[str],
) -> str | None:
    """把 target 解析为规则参数名（直用参数名，或经术语 properties 映射）。"""
    name = str(target).strip()
    if name in param_names:
        return name
    term_list = list(load_terms()) if terms is None else list(terms)
    key = normalize_name(name)
    for term in term_list:
        if any(normalize_name(alias) == key for alias in term.all_names()):
            candidates = sorted(
                (p for p in term.properties if p in param_names),
                key=lambda prop: (len(prop), prop),
            )
            if candidates:
                return candidates[0]
    return None


def _coerce_number_map(raw: object, *, what: str) -> tuple[dict[str, float], str]:
    """把工具参数 facts/inputs 归一化为 ``名字 -> float``。

    容忍真实模型常见的两种偏差: 整个映射被双重编码成 JSON 字符串；数值写成
    数字字符串（``"540"``）。形状非法时返回 ``(空映射, 错误说明)``——由工具
    以 ``{"ok": False, ...}`` 返回给模型纠正，不抛异常。
    """
    data: object = raw
    if data is None or data == "":
        return {}, ""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            return {}, f"{what} 不是合法 JSON: {exc}"
    if not isinstance(data, Mapping):
        return {}, f"{what} 须为 对象或 JSON 字符串: {type(data).__name__}"
    out: dict[str, float] = {}
    for key, value in data.items():
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return {}, f"{what}[{key!r}] 不是数值: {value!r}"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return {}, f"{what}[{key!r}] 不是数值: {value!r}"
        if not math.isfinite(number):
            return {}, f"{what}[{key!r}] 不是有限数值: {value!r}"
        out[str(key)] = number
    return out, ""


def _close(left: float, right: float) -> bool:
    """两个数值是否在相对容差 :data:`_TRACE_TOL` 内相等。"""
    return abs(left - right) <= _TRACE_TOL * max(1.0, abs(left), abs(right))


def _grounded_view(deps: _AgentDeps) -> dict[str, Any]:
    """题干已解析值 + 等价视图（facts/inputs 校验基准，与 derive_value 同口径）。"""
    view: dict[str, Any] = dict(deps.facts)
    fill_equivalences(view, default_equivalences(deps.rules))
    return view


def _tool_derive_with_rules(
    ctx: RunContext[_AgentDeps],
    target: str,
    facts: Mapping[str, Any] | str | None = None,
) -> dict[str, Any]:
    """按领域规则库确定性推导目标参数（首选工具）。"""
    deps = ctx.deps
    resolved = _resolve_target(target, deps.rules, deps.terms, deps.param_names)
    if resolved is None:
        deps.audit.append(f"derive_with_rules(target={target!r}) ⇒ 失败: 目标无法解析")
        return {
            "ok": False,
            "error": f"target {target!r} 既不是规则库参数也不是已知术语；请改用参数名（见系统提示规则清单）",
        }
    extra, shape_error = _coerce_number_map(facts, what="facts")
    if shape_error:
        deps.audit.append(f"derive_with_rules(target={target!r}) ⇒ 失败: {shape_error}")
        return {"ok": False, "error": shape_error}
    if extra:
        grounded = _grounded_view(deps)
        for key, value in extra.items():
            if key not in grounded:
                known = "、".join(sorted(grounded)) or "无"
                deps.audit.append(
                    f"derive_with_rules(target={target!r}) ⇒ 失败: facts[{key!r}] "
                    "不是题干已解析值（禁止编造前提）"
                )
                return {
                    "ok": False,
                    "error": (
                        f"facts[{key!r}] 不是已知值——facts 只能复述题干已解析的"
                        f"已知值（禁止编造/篡改前提）。已知: {known}"
                    ),
                }
            if not _close(value, float(grounded[key])):
                deps.audit.append(
                    f"derive_with_rules(target={target!r}) ⇒ 失败: facts[{key!r}]="
                    f"{value} 与题干已知值 {grounded[key]} 不一致"
                )
                return {
                    "ok": False,
                    "error": (
                        f"facts[{key!r}]={value} 与题干已解析值 {grounded[key]} 不一致；"
                        "facts 不得篡改已知前提"
                    ),
                }
    pool: dict[str, Any] = dict(deps.facts)
    pool.update(extra)
    derivation = derive_value(resolved, pool, rules=deps.rules)
    if derivation is None or not derivation.plan:
        given = "、".join(sorted(pool)) or "无"
        deps.audit.append(
            f"derive_with_rules(target={target!r}) ⇒ 失败: 已知值不足以推导 {resolved}"
        )
        return {
            "ok": False,
            "error": f"规则库无法从已知值推导 {resolved}（已知: {given}）；可补充 facts 或改用 run_python",
        }
    deps.rule_entries.extend(
        derivation_entries(derivation, rules=deps.rules, source="derivation-agent")
    )
    deps.observed.append(float(derivation.value))
    deps.rule_results[resolved] = float(derivation.value)
    hops = " → ".join(step.rule_id for step in derivation.plan)
    deps.audit.append(
        f"derive_with_rules(target={target!r}) ⇒ {resolved} = {derivation.value}"
        f"（{len(derivation.plan)} 跳: {hops}）"
    )
    return {
        "ok": True,
        "target": resolved,
        "value": float(derivation.value),
        "rules": [step.rule_id for step in derivation.plan],
        "steps": list(derivation.steps),
    }


def _tool_run_python(
    ctx: RunContext[_AgentDeps],
    script: str,
    output_var: str,
    inputs: Mapping[str, Any] | str | None = None,
) -> dict[str, Any]:
    """在沙箱执行纯计算脚本（无文件/网络/环境变量访问；资源限额内）。"""
    deps = ctx.deps
    clean, shape_error = _coerce_number_map(inputs, what="inputs")
    if shape_error:
        deps.audit.append(f"run_python(output_var={output_var!r}) ⇒ 失败: {shape_error}")
        return {"ok": False, "error": shape_error}
    if clean:
        grounded = _grounded_view(deps)
        for key, value in clean.items():
            if key not in deps.param_names:
                continue  # 非规则参数名 → 允许作为脚本局部变量
            if key not in grounded:
                deps.audit.append(
                    f"run_python(output_var={output_var!r}) ⇒ 失败: inputs[{key!r}] "
                    "是规则参数但题干未给出该值（禁止编造前提）"
                )
                return {
                    "ok": False,
                    "error": (
                        f"inputs[{key!r}] 是规则参数但题干未给出该值——禁止编造前提；"
                        "局部中间量可改用任意非规则参数名"
                    ),
                }
            if not _close(value, float(grounded[key])):
                deps.audit.append(
                    f"run_python(output_var={output_var!r}) ⇒ 失败: inputs[{key!r}]="
                    f"{value} 与题干已知值 {grounded[key]} 不一致"
                )
                return {
                    "ok": False,
                    "error": (
                        f"inputs[{key!r}]={value} 与题干已解析值 {grounded[key]} 不一致；"
                        "禁止篡改已知前提（局部中间量可改用任意非规则参数名）"
                    ),
                }
    try:
        outcome = deps.sandbox.run(script, clean, output_var)
    except ScriptRejectedError as exc:
        deps.audit.append(f"run_python(output_var={output_var!r}) ⇒ 被沙箱拒绝: {exc}")
        return {"ok": False, "error": f"脚本被沙箱拒绝: {exc}"}
    except ScriptSandboxError as exc:
        deps.audit.append(f"run_python(output_var={output_var!r}) ⇒ 沙箱执行失败: {exc}")
        return {"ok": False, "error": f"沙箱执行失败: {exc}"}

    crosschecked = False
    crosscheck = ""
    if outcome.output_var in deps.param_names:
        base: dict[str, Any] = dict(deps.facts)
        base.update(outcome.inputs)
        plan = plan_derivation(outcome.output_var, tuple(base), rules=deps.rules)
        if plan is None:
            deps.audit.append(
                f"run_python(output_var={outcome.output_var!r}) ⇒ 拒绝采信: "
                "规则链无法由已知值到达该参数——规则参数不得用脚本自创公式替代推导"
            )
            return {
                "ok": False,
                "error": (
                    f"{outcome.output_var} 是规则参数，但规则链无法由已知值到达它："
                    "脚本自创公式不予采信。请如实报告缺什么；若该值属规则库外量"
                    "（单位换算/均值等），请改用不会与规则参数混淆的输出变量名"
                ),
            }
        reference = derive_value(outcome.output_var, base, rules=deps.rules)
        if reference is None:
            deps.audit.append(
                f"run_python(output_var={outcome.output_var!r}) ⇒ 拒绝采信: "
                "规则链结构可达但按已知值数值无定义（如除零）——禁止特判/兜底绕过"
            )
            return {
                "ok": False,
                "error": (
                    f"{outcome.output_var} 由规则链结构可达、但按已知值数值无定义"
                    "（如除零）：脚本特判/兜底结果不予采信，请如实报告无法计算"
                ),
            }
        if not _close(outcome.value, float(reference.value)):
            deps.audit.append(
                f"run_python 对拍 {outcome.output_var}: 脚本={outcome.value} "
                f"规则={reference.value} ⇒ 不一致(拒绝采信)"
            )
            return {
                "ok": False,
                "error": (
                    f"脚本结果 {outcome.value} 与规则链推导 {reference.value} 不一致，"
                    "不予采信；请核对输入或改用 derive_with_rules"
                ),
            }
        crosschecked = True
        crosscheck = f"；已与规则链对拍（一致，规则值={reference.value}）"
        deps.audit.append(
            f"run_python 对拍 {outcome.output_var}: 脚本={outcome.value} "
            f"规则={reference.value} ⇒ 一致"
        )
    deps.observed.append(outcome.value)
    index = len(deps.script_entries) + 1
    deps.script_entries.append(
        {
            "rule_id": f"script-{index}",
            "kind": "script",
            "display": (
                f"script-{index}（LLM 生成脚本·Monty 沙箱执行·非规格书数据）: "
                f"{outcome.output_var} = {outcome.value}{crosscheck}"
            ),
            "target": outcome.output_var,
            "mode": "script",
            "source": "llm-script",
            "value": outcome.value,
            "crosschecked": crosschecked,
            "script": outcome.script,
        }
    )
    deps.audit.append(
        f"run_python(output_var={outcome.output_var!r}) ⇒ {outcome.value}（沙箱）"
    )
    return {
        "ok": True,
        "value": outcome.value,
        "output_var": outcome.output_var,
        "note": "沙箱执行成功（无文件/网络/环境变量访问；资源限额内）",
    }


def _build_default_model() -> Any:
    """按 settings 构建 OpenAI 兼容模型客户端（Pydantic AI，显式关闭思考模式）。

    平台特性: qwen 在 thinking 模式下拒绝 `tool_choice=required/object`（结构化输出
    必须设置），故本路径固定 ``extra_body={"enable_thinking": False}``（实机验证）。

    Raises:
        ProviderNotConfiguredError: LLM 未配置（fail-closed）。
    """
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider
    from pydantic_ai.settings import ModelSettings

    settings = get_settings()
    if not (settings.llm_base and settings.llm_api_key and settings.llm_model):
        raise ProviderNotConfiguredError(
            "LLM 未配置 (MATRAG_LLM_BASE/API_KEY/MODEL) —— Agent 路径 fail-closed"
        )
    provider = OpenAIProvider(
        base_url=settings.llm_base, api_key=settings.llm_api_key
    )
    model_settings = ModelSettings(
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        extra_body={"enable_thinking": False},
    )
    return OpenAIChatModel(
        settings.llm_model, provider=provider, settings=model_settings
    )


#: 已通过 tools 预检的模型名（整个进程只查一次）。
_VERIFIED_MODELS: set[str] = set()


def verify_tool_support(model: Any = None, *, cache: bool = True) -> None:
    """启动预检：模型必须支持原生 tool calling，否则 :class:`ToolSupportError`。

    做一次最小工具往返（``ping``）；服务启动时应调用本函数以**拒绝启动**不支持
    tools 的模型（用户要求：不做无 tools 能力的降级）。

    Args:
        model: Pydantic AI 模型对象；``None`` 表示按 settings 构建默认模型。
        cache: 是否按模型名缓存预检结果（默认开启）。

    Raises:
        ToolSupportError: 模型未发起任何工具调用，或调用过程异常。
    """
    resolved = _build_default_model() if model is None else model
    key = str(getattr(resolved, "model_name", "") or repr(resolved))
    if cache and key in _VERIFIED_MODELS:
        return
    agent: Agent[None, str] = Agent(
        resolved,
        output_type=str,
        system_prompt="你必须先调用 ping 工具，然后把它的返回内容原样回复。",
    )

    @agent.tool_plain
    def ping() -> str:
        """连通性探针工具。"""
        return "pong"

    try:
        result = agent.run_sync("请调用 ping 工具。")
    except Exception as exc:  # noqa: BLE001 —— 预检失败即拒绝启动
        raise ToolSupportError(
            f"LLM tools 能力预检失败（模型 {key}）: {type(exc).__name__}: {exc}"
        ) from exc
    called = any(
        isinstance(part, ToolCallPart) and part.tool_name == "ping"
        for message in result.new_messages()
        for part in getattr(message, "parts", ())
    )
    if not called:
        raise ToolSupportError(
            f"模型 {key} 未发起任何工具调用 —— 当前 LLM 不支持 tools 能力，"
            "拒绝启动（无降级路径）"
        )
    _VERIFIED_MODELS.add(key)


def _traceable(value: float, observed: Sequence[float]) -> bool:
    """输出值是否原样来自某次工具结果（相对容差 :data:`_TRACE_TOL`）。"""
    return any(
        abs(float(value) - item) <= _TRACE_TOL * max(1.0, abs(item))
        for item in observed
    )


def _backed_by_rules_or_crosscheck(
    value: float, deps: _AgentDeps, param: str | None = None
) -> bool:
    """value 是否被规则链结果或对拍一致的脚本结果背书（``param`` 限定参数名）。"""
    if param is None:
        if any(_close(item, value) for item in deps.rule_results.values()):
            return True
    else:
        rule_value = deps.rule_results.get(param)
        if rule_value is not None and _close(rule_value, value):
            return True
    return any(
        bool(item.get("crosschecked"))
        and (param is None or str(item.get("target") or "") == param)
        and _close(float(item["value"]), value)
        for item in deps.script_entries
    )


def _mentions_param(text: str, param: str, terms: Sequence[Term] | None) -> bool:
    """target 文本是否指向参数 ``param``（参数名或其术语别名做子串命中）。"""
    key = normalize_name(text)
    if not key:
        return False
    if normalize_name(param) in key:
        return True
    term_list = list(load_terms()) if terms is None else list(terms)
    for term in term_list:
        if param not in term.properties:
            continue
        if any(
            len(alias_key := normalize_name(alias)) >= 2 and alias_key in key
            for alias in term.all_names()
        ):
            return True
    return False


def _answer_problem(
    answer: AgentAnswer,
    deps: _AgentDeps,
    rules: Sequence[DomainRule],
    terms: Sequence[Term] | None,
    param_names: frozenset[str],
    unreachable: Sequence[str],
) -> str:
    """输出验收（可追溯性 + 规则参数归属背书）；返回问题说明，空串=通过。

    三道门: ① ``value`` 必须原样来自某次工具返回; ② ``target`` 解析为规则参数时,
    ``value`` 必须有规则链/对拍脚本背书（防"脚本自创公式冒充规则参数值"）;
    ③ ``target`` 指向"规则链不可达"的规则参数时, 脚本估算值不得作答
    （防"改名输出变量 + 假设前提"绕过 ②）。
    """
    if answer.value is None:
        return ""
    value = float(answer.value)
    if not _traceable(value, deps.observed):
        return (
            f"value={answer.value!r} 无法追溯到任何工具返回的数值"
            f"（工具值: {deps.observed!r}）"
        )
    target = str(answer.target or "").strip()
    if not target:
        for item in (*deps.rule_entries, *deps.script_entries):
            if item.get("target"):
                target = str(item["target"])
                break
    resolved = (
        _resolve_target(target, rules, terms, param_names) if target else None
    )
    if resolved is not None:
        if _backed_by_rules_or_crosscheck(value, deps, resolved):
            return ""
        return (
            f"target={target!r} 解析为规则参数 {resolved}，但 value={answer.value!r} 既不是"
            "规则链推导值，也不是与规则链对拍一致的脚本结果——规则参数不得用脚本自创公式"
        )
    mentioned = [name for name in unreachable if _mentions_param(target, name, terms)]
    if not mentioned:
        # 兜底: 逐条检查产出该值的脚本条目的输出变量名(Q08 实机套路二:
        # 最终 target 改名, 但脚本输出变量仍叫 output_current_ma)
        for item in deps.script_entries:
            if not _close(float(item["value"]), value):
                continue
            source_name = str(item.get("target") or "")
            for name in unreachable:
                if _mentions_param(source_name, name, terms):
                    mentioned.append(name)
    if mentioned and not _backed_by_rules_or_crosscheck(value, deps):
        return (
            f"target={target!r} 指向规则参数 {'/'.join(mentioned)}，但题干已知值无法由"
            f"规则链到达它（缺前提）——脚本估算值（value={answer.value!r}）不得作为答案"
        )
    return ""


def _build_agent(model: Any, *, catalog: str) -> Agent[_AgentDeps, AgentAnswer]:
    """构建 Agent（两个工具 + 结构化输出）。"""
    agent: Agent[_AgentDeps, AgentAnswer] = Agent(
        model,
        output_type=AgentAnswer,
        deps_type=_AgentDeps,
        system_prompt=f"{_SYSTEM_PROMPT}\n\n{catalog}",
    )
    agent.tool(
        _tool_derive_with_rules,
        name="derive_with_rules",
        description=(
            "按领域规则库确定性推导目标参数（首选）。target: 参数名或术语名; "
            "facts: 参数名->数值（对象）——仅可复述题干已知值，编造/篡改会被引擎拒绝。"
        ),
        retries=2,
    )
    agent.tool(
        _tool_run_python,
        name="run_python",
        description=(
            "在沙箱执行纯计算脚本（规则库覆盖不了时用）。"
            "script: 仅赋值/算术; output_var: 输出变量名; inputs: 变量->数值（对象，"
            "规则参数必须与题干已知值一致）。output_var 是规则参数时结果会与规则链"
            "对拍：规则链不可达、数值无定义（如除零）或不一致一律拒绝；规则库外量请用"
            "带单位后缀的新变量名（如 output_current_ma）避免与规则参数混淆。"
        ),
        retries=2,
    )
    return agent


def compute_with_agent(
    query: str,
    *,
    model: Any = None,
    llm: Any = None,
    rules: Sequence[DomainRule] | None = None,
    terms: Sequence[Term] | None = None,
    facts: Mapping[str, Any] | None = None,
    sandbox: ScriptRunner | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> AgentComputation | None:
    """Agent 工具循环：LLM 选工具/写脚本，数值只来自工具返回。

    Args:
        query: 用户问题。
        model: 注入的 Pydantic AI 模型（测试用）；``None`` 表示按 settings 构建。
        llm: LLM 门面（应答层传入）；``None`` 且 ``model`` 也为 ``None`` 时直接
            返回 ``None``（表示"LLM 未接线"，由调用方留痕；--no-llm 场景）。
        rules: 规则集；``None`` 表示加载默认规则库。
        terms: 术语表；``None`` 表示加载默认术语表。
        facts: 外部显式事实（覆盖文本抽取值）。
        sandbox: 脚本沙箱；``None`` 表示 :class:`~matrag.domain.script_sandbox.MontySandbox`。
        max_retries: 输出无法追溯时的纠错重跑次数（>= 0）。

    Returns:
        :class:`AgentComputation`；无已知值可算时 ``None``。

    Raises:
        ValueError: ``query`` 为空或 ``max_retries`` 非法。
        ToolSupportError: 模型不支持 tools 能力（预检失败，不降级）。
        ToolAgentError: 运行失败或输出数值无法追溯到任何工具结果。
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 不能为空")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError(f"max_retries 须为 >= 0 的整数: {max_retries!r}")
    if model is None and llm is None:
        return None
    selected = list(load_rules()) if rules is None else list(rules)
    pool = facts_pool(query, rules=selected, facts=facts)
    if not pool:
        return None
    candidates = target_candidates(query, set(pool), terms=terms, rules=selected)
    if model is None:
        model = _build_default_model()
        # 默认模型（生产路径）运行前自动预检：不支持 tools 能力即报错（无降级）。
        # 注入模型（测试替身/高级用法）跳过，由调用方自行决定是否预检。
        verify_tool_support(model)

    param_names = frozenset(
        name
        for rule in selected
        if rule.is_derive
        for name in (*rule.input_params, rule.output_key)
        if name
    )
    deps = _AgentDeps(
        rules=selected,
        terms=terms,
        facts=pool,
        sandbox=sandbox if sandbox is not None else MontySandbox(),
        param_names=param_names,
    )
    agent = _build_agent(model, catalog=_catalog_block(selected))
    user_prompt = (
        f"问题: {query}\n"
        f"已知值（参数名->数值，引擎已从题干解析）: {json.dumps(pool, ensure_ascii=False)}\n"
        f"未解目标候选（按问题提及顺序）: {json.dumps(candidates, ensure_ascii=False)}"
    )
    #: 问题提及但"题干已知值无法由规则链到达"的规则参数（归属门 ③ 用）。
    unreachable = tuple(
        name
        for name in (candidate for candidate in candidates if candidate in param_names)
        if plan_derivation(name, tuple(pool), rules=selected) is None
    )

    llm_calls = 0
    feedback = ""
    answer: AgentAnswer | None = None
    for attempt in range(max_retries + 1):
        try:
            result = agent.run_sync(user_prompt + feedback, deps=deps)
        except Exception as exc:  # noqa: BLE001 —— 运行失败必须上抛, 不静默
            raise ToolAgentError(f"Agent 运行失败: {type(exc).__name__}: {exc}") from exc
        llm_calls += max(1, int(getattr(result.usage, "requests", 0) or 0))
        answer = result.output
        problem = _answer_problem(
            answer, deps, selected, terms, param_names, unreachable
        )
        if not problem:
            break
        if attempt >= max_retries:
            raise ToolAgentError(f"输出不可采信: {problem}")
        feedback = (
            f"\n\n[纠错] {problem}。禁止自算：请重新调用工具，并把工具返回的原始数值"
            "原样作为 value。若目标是规则参数，必须用 derive_with_rules 推导（或与规则链"
            "对拍一致的脚本）；若该值确属规则库外量（单位换算/均值/组合量），target 写"
            "对应的新名字并在 explanation 说明依据；规则链不可达时不得输出估算值"
            "（即使注明假设），必须 value=null 如实说明缺口。"
        )
    assert answer is not None  # max_retries >= 0 且循环至少执行一次
    entries = list(deps.rule_entries) + list(deps.script_entries)
    target = str(answer.target or "").strip()
    if not target and entries:
        target = str(entries[0].get("target") or "")
    return AgentComputation(
        strategy=_STRATEGY,
        target=target,
        value=answer.value,
        method=str(answer.method) if answer.value is not None else "none",
        entries=entries,
        steps=list(deps.audit),
        llm_calls=llm_calls,
        reason=str(answer.explanation or ""),
    )
