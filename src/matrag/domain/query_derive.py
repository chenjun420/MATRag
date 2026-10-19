"""计算类问题的确定性接线: 问题文本 → (已知值, 目标参数) → 多跳推导。

流程 (**全确定性, 不调 LLM**):

1. **已知值池** (:func:`facts_pool`): 对**全部公式型规则**做"单位 + 紧邻标签
   角色守护"约束的文本绑定 (输入侧 :func:`matrag.domain.evaluate.extract_facts`,
   输出侧 :func:`matrag.domain.evaluate.extract_output_values`), 合并为参数值池
   (同名冲突保守剔除); 可选外部 ``facts`` 覆盖。
2. **目标参数** (:func:`target_candidates`): 用术语层 (``power_terms.yaml``)
   把问题里的中文短语映射到本体参数 (只取真实存在的参数名; 候选按"出现位置靠后
   优先、名称短者优先"排序, 排除已知值), 逐个用
   :func:`matrag.domain.planner.derive_value` 试推 —— **目标是不是可推理由
   规划器现场判定, 不是写死的短语表**。
3. 返回 :class:`QueryDerivation`; 应答层以"规则推导"形态逐跳展示
   (与"规格书数据""规则常识"严格分区, 逐跳可审计)。

限制 (保守): 推理只使用规则库与问题中给出的数值; 目标短语必须能经术语层
落到真实参数名; 规划器不支持的反解形状 (如指数含目标) 一律放弃。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from matrag.domain.evaluate import extract_facts, extract_output_values
from matrag.domain.planner import DEFAULT_MAX_DEPTH, Derivation, derive_value
from matrag.domain.rules_loader import DomainRule, load_rules
from matrag.domain.terms import Term, load_terms
from matrag.domain.vocab import normalize_name

__all__ = [
    "QueryDerivation",
    "derivation_entries",
    "derive_from_text",
    "facts_pool",
    "target_candidates",
]

#: 目标候选上限 (按提及顺序尝试推导的次数, 防问题里提及过多概念时空转)。
_TARGET_LIMIT = 4


@dataclass(frozen=True)
class QueryDerivation:
    """一次"从问题文本出发"的推导结果。

    Attributes:
        query: 原始问题。
        target: 被推导的目标参数名。
        derivation: 推导链与轨迹 (:class:`Derivation`)。
        facts: 本次使用的已知值池。
    """

    query: str
    target: str
    derivation: Derivation
    facts: dict[str, Any]


def facts_pool(
    query: str,
    *,
    rules: Sequence[DomainRule] | None = None,
    facts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """从问题文本构建参数值池 (公式型规则输入/输出绑定 + 显式 ``facts`` 覆盖)。

    输入侧经 :func:`matrag.domain.evaluate.extract_facts`, 输出侧经
    :func:`matrag.domain.evaluate.extract_output_values` (带「输入」/「输出」
    标签的数量只参与输入侧绑定, 防止角色污染)。绑定约束: 单位一致 +
    ``input_``/``output_`` 前缀参数的**紧邻标签角色词**; 跨规则同名冲突
    (两处绑定到不同值) 保守剔除该参数。
    """
    selected = list(load_rules()) if rules is None else list(rules)
    hints = [{"rule_id": rule.rule_id} for rule in selected if rule.is_derive]
    bindings = extract_facts(query, hints, rules=selected)
    outputs = extract_output_values(query, rules=selected)
    pool: dict[str, Any] = {}
    conflicted: set[str] = set()
    for bound in (*bindings.values(), outputs):
        for name, value in bound.items():
            if name in pool and pool[name] != value:
                conflicted.add(name)
            else:
                pool.setdefault(name, value)
    for name in conflicted:
        pool.pop(name, None)
    if facts:
        pool.update(dict(facts))
    return pool


def target_candidates(
    query: str,
    facts_keys: set[str],
    *,
    terms: Sequence[Term] | None = None,
    rules: Sequence[DomainRule] | None = None,
    limit: int = _TARGET_LIMIT,
) -> list[str]:
    """问题中"被提及且仍无值"的参数候选 (位置靠后优先, 名称短者优先)。

    短语 → 参数的映射全部来自术语层的 ``properties`` (只保留真实参数名);
    找不到映射的短语不产生候选 (宁缺毋滥)。
    """
    term_list = list(load_terms()) if terms is None else list(terms)
    selected = list(load_rules()) if rules is None else list(rules)
    param_names: set[str] = set()
    for rule in selected:
        if not rule.is_derive:
            continue
        param_names.update(rule.input_params)
        if rule.output_key:
            param_names.add(rule.output_key)
    normalized = normalize_name(query)
    mentions: list[tuple[int, str]] = []
    for term in term_list:
        best = -1
        for name in term.all_names():
            key = normalize_name(name)
            if len(key) < 2:
                continue
            pos = normalized.rfind(key)
            if pos >= 0:
                best = max(best, pos + len(key))
        if best >= 0:
            mentions.append((best, term.term_id))
    mentions.sort(key=lambda item: (-item[0], item[1]))
    terms_by_id = {term.term_id: term for term in term_list}
    out: list[str] = []
    for _, term_id in mentions:
        term = terms_by_id[term_id]
        candidates = sorted(
            (p for p in term.properties if p in param_names),
            key=lambda name: (len(name), name),
        )
        for name in candidates:
            if name in facts_keys or name in out:
                continue
            out.append(name)
            if len(out) >= limit:
                return out
    return out


def derive_from_text(
    query: str,
    *,
    rules: Sequence[DomainRule] | None = None,
    terms: Sequence[Term] | None = None,
    facts: Mapping[str, Any] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> QueryDerivation | None:
    """从问题文本推导目标参数 (无可行目标时返回 ``None``)。

    Args:
        query: 用户问题 (自然语言, 可含"输入功率100W"这类中文紧邻数字)。
        rules: 规则集; ``None`` 表示加载默认规则库。
        terms: 术语表; ``None`` 表示加载默认术语表。
        facts: 外部显式事实 (覆盖文本抽取值), 如测试给定值。
        max_depth: 推导链最大步数。
    """
    selected = list(load_rules()) if rules is None else list(rules)
    pool = facts_pool(query, rules=selected, facts=facts)
    if not pool:
        return None
    candidates = target_candidates(
        query, set(pool), terms=terms, rules=selected
    )
    for target in candidates:
        derivation = derive_value(
            target, pool, rules=selected, max_depth=max_depth
        )
        if derivation is not None and derivation.plan:
            return QueryDerivation(
                query=query, target=target, derivation=derivation, facts=pool
            )
    return None


def derivation_entries(
    derivation: Derivation,
    *,
    rules: Sequence[DomainRule] | None = None,
    source: str = "derivation",
) -> list[dict[str, Any]]:
    """把推导链渲染成应答层的求值条目 (逐跳一条, 与既有求值通道同形)。

    每条: ``{rule_id, kind, display, target, mode, source}``;
    ``display`` 与既有求值行风格一致 (``K-XXX（formula·正向）: ...``)。
    """
    selected = list(load_rules()) if rules is None else list(rules)
    kinds = {
        rule.rule_id: ("formula" if rule.is_derive else "constraint")
        for rule in selected
    }
    entries: list[dict[str, Any]] = []
    for step, text in zip(derivation.plan, derivation.steps, strict=True):
        kind = kinds.get(step.rule_id, "formula")
        mode_cn = "反解" if step.mode == "invert" else "代入"
        body = text
        prefix = f"{step.rule_id} ["
        if text.startswith(prefix):
            body = text.split("]: ", 1)[1] if "]: " in text else text
        entries.append(
            {
                "rule_id": step.rule_id,
                "kind": kind,
                "display": f"{step.rule_id}（{kind}·{mode_cn}）: {body}",
                "target": step.target,
                "mode": step.mode,
                "source": source,
            }
        )
    return entries
