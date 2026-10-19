"""LLM 编排的多跳推导: 模型只选规则与顺序, 数值全部由确定性引擎执行。

与 :mod:`matrag.domain.planner` (纯符号搜索) 的分工与对比设计:

- **规划器 (planner)**: 无 LLM; 从目标出发做有界 DFS, 支持表达式反解与
  等价视图, 完全确定性 —— 适合"目标明确、规则图上可达"的问题;
- **本模块 (LLM 编排)**: LLM 读题后**只输出计划** (目标参数 + 规则应用顺序),
  引擎逐跳校验并执行: 输入齐备 → 正向代入; 恰缺一个输入且输出已知 → 反解;
  任何校验/执行失败 → 可回退到规划器 (``fallback=True``)。
  数值永不经过 LLM, 计划与每跳轨迹可审计。

两种编排产出同形的 :class:`matrag.domain.planner.Derivation`, 便于对比评估。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from matrag.domain.planner import (
    DEFAULT_MAX_DEPTH,
    MODE_FORWARD,
    MODE_INVERT,
    Derivation,
    PlanStep,
    default_equivalences,
    fill_equivalences,
    invert_expression,
)
from matrag.domain.query_derive import (
    QueryDerivation,
    derive_from_text,
    facts_pool,
    target_candidates,
)
from matrag.domain.rules import SafeExpression
from matrag.domain.rules_loader import DomainRule, load_rules
from matrag.domain.terms import Term

logger = logging.getLogger(__name__)

__all__ = ["OrchestrationResult", "orchestrate_from_text"]

#: 送入 LLM 的规则清单上限 (裁剪后; 控制提示词规模与 JSON 失败率)。
_CATALOG_LIMIT = 24

_STRATEGY_LLM = "llm"
_STRATEGY_PLANNER = "planner(回退)"

_SYSTEM_PROMPT = """你是电源领域的推导编排器。给定问题、已解析的已知数值与可用规则清单, 你只负责两件事:
(1) 选定目标参数 (从"目标候选"中选择);
(2) 给出规则应用的顺序 (hops)。
硬性约束:
- 数值计算由确定性引擎执行, 你不做任何算术, 也不要输出任何数值结果;
- 每条规则需要其 inputs 全部已知才能执行, 执行后其 output 变为已知;
- 也允许"反解"用法: 若某条规则的输出现已已知、且恰好缺一个输入, 该输入可由引擎反解求出 —— 你照常列出该规则即可;
- 只用清单里存在的 rule_id; 顺序必须满足依赖 (先算中间量);
- 信息不足无法到达目标时: target 置 null、hops 置空, 并在 reason 中写明缺什么。
只输出 JSON。"""

_SCHEMA = (
    '{"target": "目标参数名(或 null)", "hops": ["规则id", "规则id", "..."], '
    '"reason": "为什么这样排链 (一句话)"}'
)


@dataclass(frozen=True)
class OrchestrationResult:
    """一次编排结果 (LLM 或回退规划器)。

    Attributes:
        strategy: ``"llm"`` 或 ``"planner(回退)"``。
        target: 目标参数名。
        derivation: 推导链与逐跳轨迹 (与规划器同形)。
        facts: 本次使用的已知值池。
        llm_calls: 发生的 LLM 调用次数 (修复重试不计入)。
        reason: LLM 给出的计划理由, 或回退原因。
    """

    strategy: str
    target: str
    derivation: Derivation
    facts: dict[str, Any]
    llm_calls: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict。"""
        return {
            "strategy": self.strategy,
            "target": self.target,
            "derivation": self.derivation.to_dict(),
            "facts": dict(self.facts),
            "llm_calls": self.llm_calls,
            "reason": self.reason,
        }


def _prune_catalog(
    pool: Mapping[str, Any],
    candidates: Sequence[str],
    rules: Sequence[DomainRule],
    *,
    limit: int = _CATALOG_LIMIT,
) -> list[DomainRule]:
    """按"已知参数 / 目标候选"相关性裁剪规则清单 (1 跳邻居展开, 确定性排序)。"""
    derive = [r for r in rules if r.is_derive and r.output_key]
    relevant = set(pool) | set(candidates)
    selected: dict[str, DomainRule] = {}
    for rule in derive:
        if set(rule.input_params) & relevant or rule.output_key in relevant:
            selected[rule.rule_id] = rule
    frontier = relevant | {r.output_key for r in selected.values()}
    for rule in derive:
        if rule.rule_id in selected:
            continue
        if set(rule.input_params) & frontier:
            selected[rule.rule_id] = rule
    ordered = sorted(selected.values(), key=lambda r: r.rule_id)
    if len(ordered) > limit:
        direct = [
            r
            for r in ordered
            if set(r.input_params) & relevant or r.output_key in relevant
        ]
        direct_ids = {r.rule_id for r in direct}
        others = [r for r in ordered if r.rule_id not in direct_ids]
        ordered = (direct + others)[:limit]
    return ordered


def _execute_hops(
    hops: Sequence[str],
    target: str,
    pool: Mapping[str, Any],
    index: Mapping[str, DomainRule],
    aliases: Mapping[str, Sequence[str]],
) -> Derivation | None:
    """按 LLM 给出的规则序列逐跳执行 (正向/反解自动判定); 失败返回 ``None``。"""
    working = dict(pool)
    fill_equivalences(working, aliases)
    plan: list[PlanStep] = []
    steps: list[str] = []
    for rule_id in hops:
        rule = index.get(rule_id)
        if rule is None or not rule.is_derive or not rule.output_key:
            return None
        expr = str((rule.derive or {}).get("expr", ""))
        inputs = rule.input_params
        missing = [name for name in inputs if name not in working]
        try:
            if not missing:
                env = {name: working[name] for name in inputs}
                value = SafeExpression(expr).evaluate(env)
                working[rule.output_key] = value
                plan.append(PlanStep(rule.rule_id, MODE_FORWARD, rule.output_key))
                given = "、".join(f"{name}={working[name]}" for name in inputs)
                steps.append(
                    f"{rule.rule_id} [正向]: 代入 {given} → {rule.output_key} = {value}"
                )
            elif len(missing) == 1 and rule.output_key in working:
                solve_for = missing[0]
                known = {
                    name: working[name] for name in inputs if name != solve_for
                }
                value = invert_expression(
                    expr, solve_for, known, working[rule.output_key]
                )
                if value is None:
                    return None
                working[solve_for] = value
                plan.append(PlanStep(rule.rule_id, MODE_INVERT, solve_for))
                given = "、".join(
                    f"{name}={working[name]}" for name in inputs if name != solve_for
                )
                steps.append(
                    f"{rule.rule_id} [反解]: 由 {rule.output_key}="
                    f"{working[rule.output_key]} 与 {given} → {solve_for} = {value}"
                )
            else:
                return None
        except Exception as exc:  # noqa: BLE001 —— 执行期数值失败 → 该计划作废, 留痕
            logger.warning("llm_planner.plan_aborted: target=%s err=%s", target, exc)
            return None
        fill_equivalences(working, aliases)
    if target not in working:
        return None
    return Derivation(
        target=target, value=working[target], plan=tuple(plan), steps=tuple(steps)
    )


def orchestrate_from_text(
    query: str,
    *,
    llm: Any,
    rules: Sequence[DomainRule] | None = None,
    terms: Sequence[Term] | None = None,
    facts: Mapping[str, Any] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    fallback: bool = True,
) -> OrchestrationResult | None:
    """LLM 编排多跳推导: 模型选计划, 引擎执行; 失败可回退规划器。

    Args:
        query: 用户问题。
        llm: LLM 门面 (需实现 ``complete_json``)。
        rules: 规则集; ``None`` 表示加载默认规则库。
        terms: 术语表; ``None`` 表示加载默认术语表。
        facts: 外部显式事实 (覆盖文本抽取值)。
        max_depth: 计划最大跳数。
        fallback: LLM 失败 (调用异常 / JSON 非法 / 校验或执行失败) 时是否
            回退到确定性规划器 (:func:`derive_from_text`)。

    Returns:
        :class:`OrchestrationResult`; LLM 与回退都失败 (或关闭回退) 时 ``None``。
    """
    if max_depth < 1:
        raise ValueError(f"max_depth 须 >= 1, 实际是 {max_depth!r}")
    selected = list(load_rules()) if rules is None else list(rules)
    index = {rule.rule_id: rule for rule in selected}
    aliases = default_equivalences(selected)

    pool = facts_pool(query, rules=selected, facts=facts)
    if not pool:
        return None
    candidates = target_candidates(query, set(pool), terms=terms, rules=selected)

    def _fallback(reason: str) -> OrchestrationResult | None:
        if not fallback:
            return None
        found: QueryDerivation | None = derive_from_text(
            query, rules=selected, terms=terms, facts=facts, max_depth=max_depth
        )
        if found is None:
            return None
        return OrchestrationResult(
            strategy=_STRATEGY_PLANNER,
            target=found.target,
            derivation=found.derivation,
            facts=found.facts,
            llm_calls=0,
            reason=reason,
        )

    catalog = _prune_catalog(pool, candidates, selected)
    if not catalog:
        return _fallback("无相关规则可编排")
    catalog_json = json.dumps(
        [
            {
                "rule_id": rule.rule_id,
                "output": rule.output_key,
                "inputs": list(rule.input_params),
            }
            for rule in catalog
        ],
        ensure_ascii=False,
    )
    user_prompt = (
        f"问题: {query}\n"
        f"已知值 (参数名->数值, 引擎已解析): "
        f"{json.dumps(dict(pool), ensure_ascii=False)}\n"
        f"目标候选 (按问题提及顺序): {json.dumps(candidates, ensure_ascii=False)}\n"
        f"规则清单 (JSON, 仅公式型): {catalog_json}\n"
        f"最多 {max_depth} 跳。"
    )

    try:
        plan = llm.complete_json(
            _SYSTEM_PROMPT, user_prompt, schema_hint=_SCHEMA, max_tokens=1000
        )
    except Exception as exc:  # noqa: BLE001 —— LLM 失败 → 回退必须留痕
        return _fallback(f"LLM 调用失败: {type(exc).__name__}: {exc}")

    if not isinstance(plan, Mapping):
        return _fallback(f"LLM 输出不是 JSON 对象: {type(plan).__name__}")
    target = plan.get("target")
    hops = plan.get("hops")
    reason = str(plan.get("reason") or "")
    if not isinstance(target, str) or not target.strip():
        return _fallback(f"LLM 未给出目标: {reason or '无说明'}")
    target = target.strip()
    param_names: set[str] = set()
    for rule in selected:
        if not rule.is_derive:
            continue
        param_names.update(rule.input_params)
        if rule.output_key:
            param_names.add(rule.output_key)
    if target not in param_names:
        return _fallback(f"LLM 目标 {target!r} 不是规则库参数")
    if not isinstance(hops, list) or not all(isinstance(h, str) for h in hops):
        return _fallback(f"hops 形状非法: {hops!r}")
    if not 1 <= len(hops) <= max_depth:
        return _fallback(f"hops 数量非法: {len(hops)}")
    if len(set(hops)) != len(hops):
        return _fallback("hops 含重复规则")

    derivation = _execute_hops(hops, target, pool, index, aliases)
    if derivation is None:
        return _fallback(f"LLM 计划校验/执行失败: hops={hops}, target={target}")
    return OrchestrationResult(
        strategy=_STRATEGY_LLM,
        target=target,
        derivation=derivation,
        facts=pool,
        llm_calls=1,
        reason=reason,
    )
