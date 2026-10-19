"""确定性规则求值服务: 把「规则提示 + 实测事实」变成可复核的计算结果。

定位: :mod:`matrag.retrieve.rule_hint` 回答"哪几条规则与问题相关"; 本模块回答
"把这些实测值代入后算出什么、是否触发约束"。求值引擎直接复用
:func:`matrag.domain.rules.evaluate_rule` (semantica Datalog 前推 + SHACL),
本模块只做事实装配、逐条求值与结果打包。

两档能力:

1. **显式代入**: 调用方给出 ``facts`` (CLI ``--fact name=value``), 逐条求值,
   输入齐备但未被提示命中的规则也会补算 (去重);
2. **自动抽取**: 从查询文本扫描「数字 + 单位」token, 按单位把数值绑定到规则
   输入参数。绑定是**保守**的: 该规则输入里恰好一个参数与之同底单位、且该
   底单位在查询里恰好一个数值时才绑定; 同单位多参数 / 多数值一律不绑,
   宁可少算 (自动抽取结果以 ``facts_used`` 逐条可对账)。

设计约束:

- **缺输入不硬算**: 事实不齐 → ``applicable=False`` 且 ``missing_inputs``
  明确列出缺哪些键; **绝不用**规则自带 ``test.given`` 冒充实测值;
- **求值成功与自测比对分离**: ``ok`` 只表示"引擎算出了结论"。与规则自测期望
  的比对属于规则集自身的校验 (见 :func:`matrag.domain.rules.verify_rule`),
  不在本层做 —— 否则用户代入自己的数值会被误报成"与预期不符";
- **失败可见**: 未知 rule_id / 求值异常 → ``ok=False`` + ``detail``/``error``,
  不抛出、不吞掉; 单条失败不影响其余规则;
- **确定性**: 同一 (规则提示, 事实) 两次求值结果逐位一致; 不修改传入的
  ``facts`` (内部先拷贝)。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from matrag.domain.rules import evaluate_rule
from matrag.domain.rules_loader import DomainRule, load_rules

__all__ = [
    "KIND_BY_DOMAIN",
    "EvaluationResult",
    "evaluate_for_answer",
    "evaluate_hints",
    "extract_facts",
    "extract_output_values",
    "parse_facts",
    "render_evaluations",
]

#: ``DomainRule.kind`` (``derive``/``constraint``) → 对外类型标签
#: (与 :mod:`matrag.retrieve.rule_hint` 的口径一致, 便于跨模块对账)。
KIND_BY_DOMAIN: dict[str, str] = {"derive": "formula", "constraint": "constraint"}


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """从 dict / 对象统一取字段 (鸭子类型)。"""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _kind_of(rule: DomainRule) -> str:
    """规则类型标签 (``formula`` / ``constraint``)。"""
    return KIND_BY_DOMAIN.get(rule.kind, rule.kind)


def _fmt(value: Any) -> str:
    """数值/布尔的一行渲染 (浮点取 6 位有效数字)。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


@dataclass(frozen=True)
class EvaluationResult:
    """一条规则的求值结果 (可直接进答案上下文 / 推理链 / CLI 展示)。

    - ``applicable``: 引擎是否在给定事实下激活了该规则 (输入齐备且结论可用);
    - ``ok``: 求值是否成功 (``applicable`` 且无异常); 与自测期望无关;
    - ``actual``: 公式型 = 算出的数值; 约束型 = 是否违反 (bool);
    - ``missing_inputs``: 缺哪些输入键 (事实不齐时的明确清单);
    - ``facts_used``: 实际代入了哪些键值 (对账"算了什么")。
    """

    rule_id: str
    statement: str
    kind: str
    output_key: str
    unit: str
    applicable: bool
    ok: bool
    actual: Any
    missing_inputs: tuple[str, ...]
    facts_used: dict[str, Any]
    detail: str
    error: str

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict (含一行渲染 ``display``)。"""
        return {
            "rule_id": self.rule_id,
            "statement": self.statement,
            "kind": self.kind,
            "output_key": self.output_key,
            "unit": self.unit,
            "applicable": self.applicable,
            "ok": self.ok,
            "actual": self.actual,
            "missing_inputs": list(self.missing_inputs),
            "facts_used": dict(self.facts_used),
            "detail": self.detail,
            "error": self.error,
            "display": self.display(),
        }

    def display(self) -> str:
        """一行中文摘要 (上下文分区 / 推理链 / CLI 共用)。"""
        head = f"{self.rule_id}（{self.kind or '规则'}）"
        if not self.applicable:
            if self.missing_inputs:
                return f"{head}: 缺少输入 {'、'.join(self.missing_inputs)}, 未求值"
            reason = self.detail or self.error or "输入不齐"
            return f"{head}: 未求值（{reason}）"
        subs = "、".join(f"{key}={_fmt(value)}" for key, value in self.facts_used.items())
        if self.kind == "constraint":
            if self.actual is True:
                verdict = "触发（violation=true）"
            elif self.actual is False:
                verdict = "未触发（violation=false）"
            else:
                verdict = f"判定={_fmt(self.actual)}"
            return f"{head}: 代入 {subs} → 约束{verdict}"
        unit = f" {self.unit}" if self.unit else ""
        return f"{head}: 代入 {subs} → {self.output_key} = {_fmt(self.actual)}{unit}"


def _rule_index(rules: Sequence[DomainRule] | None) -> dict[str, DomainRule]:
    """规则集 → ``{rule_id: DomainRule}`` 索引。"""
    selected = list(load_rules()) if rules is None else list(rules)
    return {rule.rule_id: rule for rule in selected}


def _evaluate_one(rule: DomainRule, facts: Mapping[str, Any]) -> EvaluationResult:
    """单条规则求值 (输入不足不抛错, 以结果字段呈现)。"""
    raw = evaluate_rule(rule, facts)
    applicable = bool(raw.get("applicable"))
    error = str(raw.get("error") or "")
    return EvaluationResult(
        rule_id=rule.rule_id,
        statement=rule.statement,
        kind=_kind_of(rule),
        output_key=rule.output_key,
        unit=str(rule.variables.get(rule.output_key) or ""),
        applicable=applicable,
        ok=bool(applicable and not error),
        actual=raw.get("actual"),
        missing_inputs=tuple(str(name) for name in (raw.get("missing_inputs") or ())),
        facts_used={name: facts[name] for name in rule.input_params if name in facts},
        detail=str(raw.get("detail") or ""),
        error=error,
    )


def evaluate_hints(
    hints: Sequence[Any],
    facts: Mapping[str, Any],
    *,
    rules: Sequence[DomainRule] | None = None,
) -> list[EvaluationResult]:
    """逐条对规则提示代入事实求值。

    Args:
        hints: 规则提示 (``RuleHint`` / dict / 任意带 ``rule_id`` 的对象),
            鸭子类型; 重复 ``rule_id`` 只求值一次 (保留首现位置)。
        facts: 实测事实 (键 = 规则输入参数名, 值 = 数值/布尔/字符串);
            **空映射 → 返回空列表** (没有可代入的值, 不求值)。
        rules: 规则集; ``None`` 时用 :func:`matrag.domain.rules_loader.load_rules`
            现加载。

    Returns:
        与 ``hints`` 顺序一致的 :class:`EvaluationResult` 列表;
        单条规则失败不中断, 以 ``ok=False`` + ``detail`` 呈现。
    """
    if not facts:
        return []
    index = _rule_index(rules)
    bag = dict(facts)  # 防御性拷贝: 本层不修改调用方映射, 求值层也不回写
    results: list[EvaluationResult] = []
    seen: set[str] = set()
    for hint in hints or []:
        rule_id = str(_field(hint, "rule_id") or "").strip()
        if not rule_id or rule_id in seen:
            continue
        seen.add(rule_id)
        rule = index.get(rule_id)
        if rule is None:
            results.append(
                EvaluationResult(
                    rule_id=rule_id,
                    statement=str(_field(hint, "statement") or ""),
                    kind=str(_field(hint, "kind") or ""),
                    output_key="",
                    unit="",
                    applicable=False,
                    ok=False,
                    actual=None,
                    missing_inputs=(),
                    facts_used={},
                    detail=f"规则集中不存在 {rule_id}",
                    error="unknown_rule",
                )
            )
            continue
        results.append(_evaluate_one(rule, bag))
    return results


# --------------------------------------------------------------------------- #
# 从查询文本自动绑定输入值 (数字 + 单位 token → 规则输入参数)
# --------------------------------------------------------------------------- #

#: 查询里可识别的单位 token → (底单位, 换算到基准值的倍率)。
#: 只收工程实践里真正出现、且与本域参数单位对得上的写法; **大小写敏感**
#: (mΩ/MΩ、ms/Ms 不混淆; "MV" 之类歧义写法一律不收)。
_TOKEN_UNITS: dict[str, tuple[str, float]] = {
    "mVpp": ("v", 1e-3),
    "Vpp": ("v", 1),
    "mV": ("v", 1e-3),
    "kV": ("v", 1e3),
    "V": ("v", 1),
    "v": ("v", 1),
    "mA": ("a", 1e-3),
    "A": ("a", 1),
    "kHz": ("hz", 1e3),
    "KHz": ("hz", 1e3),
    "MHz": ("hz", 1e6),
    "Hz": ("hz", 1),
    "hz": ("hz", 1),
    "mF": ("f", 1e-3),
    "uF": ("f", 1e-6),
    "µF": ("f", 1e-6),
    "μF": ("f", 1e-6),
    "nF": ("f", 1e-9),
    "pF": ("f", 1e-12),
    "F": ("f", 1),
    "mΩ": ("ohm", 1e-3),
    "kΩ": ("ohm", 1e3),
    "Ω": ("ohm", 1),
    "ms": ("s", 1e-3),
    "us": ("s", 1e-6),
    "µs": ("s", 1e-6),
    "μs": ("s", 1e-6),
    "s": ("s", 1),
    "mW": ("w", 1e-3),
    "kW": ("w", 1e3),
    "W": ("w", 1),
    "%": ("%", 1),
    "℃": ("c", 1),
    "°C": ("c", 1),
}

#: 「数字 + 单位」token。前缀边界只查 ASCII 字母数字/点/连字符(中文等非 ASCII
#: 字符不算边界, 因此 "输入功率100W" 这类中文紧邻数字照常命中); 负号只认"前面
#: 不是 ASCII 字母数字/点/连字符"的情形 (避免把 "85-300V" 这类范围的后半段当成
#: 负值, 也拦住 "D54A" 这类型号片段); 单位后不允许紧跟字母数字 (避免 "5V4" 这类
#: 支路名被误当数值)。
_QUANTITY_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-])(-?\d+(?:\.\d+)?)\s*(?P<unit>"
    + "|".join(re.escape(unit) for unit in sorted(_TOKEN_UNITS, key=len, reverse=True))
    + r")(?![A-Za-z0-9])"
)


@dataclass(frozen=True)
class _Quantity:
    """查询里扫到的一个数量 (已换算到底单位)。

    ``start`` 是匹配起点 (供角色词守护 :func:`_role_word_ok` 检查数量前的窗口)。
    """

    value: float
    base: str
    scale: float
    start: int = 0


def _scan_quantities(text: str) -> list[_Quantity]:
    """扫描查询文本里的「数字 + 单位」token。"""
    out: list[_Quantity] = []
    for match in _QUANTITY_RE.finditer(text or ""):
        base, scale = _TOKEN_UNITS[match.group("unit")]
        out.append(_Quantity(float(match.group(1)), base, scale, match.start()))
    return out


def _declared_base(unit_text: Any) -> tuple[str, float] | None:
    """规则里声明的单位 → (底单位, 倍率); 不能用作单位绑定的返回 ``None``。

    - 去掉括注 (``V(pp)``→``V``、``A(I_RATED, …)``→``A``);
    - 含 ``/`` 或逗号 (复合单位/枚举) 一律不绑定;
    - 只认 :data:`_TOKEN_UNITS` 里存在的单位写法。
    """
    if not unit_text:
        return None
    text = re.sub(r"\(.*?\)", "", str(unit_text).strip()).strip()
    if not text or any(ch in text for ch in "/,，、"):
        return None
    return _TOKEN_UNITS.get(text)


#: ``input_`` / ``output_`` 前缀参数的角色词 (中文语境): 绑定前要求数量
#: **紧邻的标签词**中出现对应角色词 (如 "输入功率100W" 的标签是"输入功率")。
#: 不允许隔句远配 —— 固定宽度窗口会把 "输出电压54V、输入功率644W" 里的
#: ``output_power`` 误判通过, 产出看似确定实为角色错配的计算值。含英文查询
#: 对这类参数保守不绑: 宁可少绑, 不可错绑。
_ROLE_WORDS: dict[str, str] = {"input_": "输入", "output_": "输出"}
#: 标签与数字之间的连接符 (跳过后再取紧邻标签词, 如 "输出电压为 12V")。
_LABEL_CONNECTORS = frozenset(" \t（(【[）)]}】:：=约是为")


def _role_label(text: str, start: int) -> str:
    """取数字前**紧邻的标签词** (先跳过连接符, 遇分隔符/标点即停)。"""
    i = start
    while i > 0 and text[i - 1] in _LABEL_CONNECTORS:
        i -= 1
    j = i
    while j > 0 and (text[j - 1].isalnum() or text[j - 1] == "_"):
        j -= 1
    return text[j:i]


def _role_word_ok(text: str, start: int, param: str) -> bool:
    """角色词守护: 前缀参数须在其数量紧邻标签词内出现对应角色词。"""
    for prefix, word in _ROLE_WORDS.items():
        if param.startswith(prefix):
            return word in _role_label(text, start)
    return True


def _bind_rule(
    rule: DomainRule,
    quantities: Sequence[_Quantity],
    text: str,
    *,
    params: Sequence[str] | None = None,
) -> dict[str, Any]:
    """把数量 token 绑定到单条规则的参数 (保守规则, 不确定就不绑)。

    ``params`` 默认取规则的输入参数; 传输出参数 (如 ``[rule.output_key]``)
    可用于绑定输出侧数值 (:func:`extract_output_values`)。``text`` 是原始
    查询文本, 供 :func:`_role_word_ok` 做角色词守护。
    """
    declared: dict[str, tuple[str, float]] = {}
    variables = rule.variables or {}
    for param in rule.input_params if params is None else params:
        parsed = _declared_base(variables.get(param))
        if parsed is not None:
            declared[param] = parsed
    if not declared:
        return {}
    by_base: dict[str, list[_Quantity]] = defaultdict(list)
    for quantity in quantities:
        by_base[quantity.base].append(quantity)
    bound: dict[str, Any] = {}
    for param, (base, param_scale) in declared.items():
        group = by_base.get(base) or []
        if len(group) != 1:
            continue  # 该底单位没有数值, 或数值多于一个无法配对 → 不绑
        if sum(1 for other in declared.values() if other[0] == base) != 1:
            continue  # 规则里多个输入同底单位 → 歧义, 不绑
        quantity = group[0]
        if not _role_word_ok(text, quantity.start, param):
            continue  # 角色词不在数量附近 (如把输入功率错绑成输出功率) → 不绑
        bound[param] = quantity.value * quantity.scale / param_scale
    return bound


def extract_facts(
    query: str,
    hints: Sequence[Any],
    *,
    rules: Sequence[DomainRule] | None = None,
) -> dict[str, dict[str, Any]]:
    """从查询文本为每条提示规则绑定输入值。

    只做确定性绑定: 「数字 + 单位」token 仅当"该规则输入里恰好一个参数与之
    同底单位"且"该底单位在查询里恰好一个数值"时才绑定; 绑不上的规则不出现在
    结果里。``input_``/``output_`` 前缀参数另需**角色词守护** (数量前的窗口内
    须出现「输入」/「输出」, 否则不绑, 防止角色错配; 英文查询对这类参数保守
    不绑)。返回 ``{rule_id: {param: value}}`` (值已换算到参数声明的单位)。
    """
    quantities = _scan_quantities(query)
    if not quantities:
        return {}
    index = _rule_index(rules)
    out: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for hint in hints or []:
        rule_id = str(_field(hint, "rule_id") or "").strip()
        if not rule_id or rule_id in seen:
            continue
        seen.add(rule_id)
        rule = index.get(rule_id)
        if rule is None:
            continue
        bound = _bind_rule(rule, quantities, query)
        if bound:
            out[rule_id] = bound
    return out


def extract_output_values(
    query: str,
    *,
    rules: Sequence[DomainRule] | None = None,
) -> dict[str, Any]:
    """从查询文本绑定公式规则**输出参数**的数值 (返回 ``{param: value}``)。

    与 :func:`extract_facts` 的输入侧绑定同款保守约束: 「数字 + 单位」token
    仅当该参数声明的单位与之同底单位、且该底单位在查询里恰好一个数值时
    才绑定; ``input_``/``output_`` 前缀参数同样受角色词守护。同一参数被
    多条规则绑定到不同值 → 保守剔除。

    **角色词过滤**: 紧邻标签带「输入」/「输出」的数量不参与输出侧绑定 ——
    它们已由输入侧绑定与角色守护负责, 再绑到通用输出参数会造成角色污染
    (如 "输入功率100W" 被当成通用 ``power``, 让 U·I=P 反解出与效率语义
    矛盾的捷径值)。

    用途: :func:`matrag.domain.query_derive.facts_pool` 需要把出现在输出侧
    的量 (如 "功率20W" 之于 K-ELEC-005 的 ``power``) 也纳入已知值池 ——
    否则池里只有"某规则恰好把该参数当输入"的路径, 反解时会漏值。
    """
    quantities = [
        q
        for q in _scan_quantities(query)
        if not any(word in _role_label(query, q.start) for word in _ROLE_WORDS.values())
    ]
    if not quantities:
        return {}
    selected = list(load_rules()) if rules is None else list(rules)
    out: dict[str, Any] = {}
    conflicted: set[str] = set()
    for rule in selected:
        if not rule.is_derive:
            continue
        output_key = rule.output_key or ""
        if not output_key:
            continue
        bound = _bind_rule(rule, quantities, query, params=[output_key])
        for name, value in bound.items():
            if name in out and out[name] != value:
                conflicted.add(name)
            else:
                out.setdefault(name, value)
    for name in conflicted:
        out.pop(name, None)
    return out


def evaluate_for_answer(
    hints: Sequence[Any] | None = None,
    *,
    query: str | None = None,
    facts: Mapping[str, Any] | None = None,
    rules: Sequence[DomainRule] | None = None,
    max_depth: int = 3,
) -> list[EvaluationResult]:
    """应答层统一入口: 把「提示规则 + 输入值」求值成可展示结果 (支持链式推理)。

    - ``facts`` 显式给出 (非 ``None``) 时: 先逐条代入提示规则, 再补"输入齐备"
      的其他规则 (按 rule_id 去重); 派生规则的输出值会**回灌**输入池, 供下游
      规则继续代入 (链式, 如 ``Pin×η → Pout`` 再接 ``Pout/Vout → Iout``), 最多
      ``max_depth`` 轮; 只有产生绑定的结果才保留 (``applicable`` 或至少一个
      输入被代入), 避免满屏"未求值";
    - ``facts`` 为 ``None`` 且给定 ``query`` 时: 从查询文本自动抽取 (每条规则
      独立绑定), 绑定结果合并为链式池 (同名冲突保守剔除该参数), 但只对
      **提示规则**做链式展开 (查询路径不扩大规则面);
    - 传空映射 ``{}`` → 显式关闭求值; 两者都没有 / 什么都绑不上 → 空列表。
    - 链式回灌只接受**派生规则的成功输出**, 同名值先到者胜 (保证确定性);
      ``max_depth=1`` 即旧版单轮行为。

    Raises:
        ValueError: ``max_depth < 1`` (静默取 0 会让求值整体失效, 属调用方错误)。
    """
    if max_depth < 1:
        raise ValueError(f"max_depth 须 >= 1, 实际是 {max_depth!r}")
    selected = list(load_rules()) if rules is None else list(rules)
    index = {rule.rule_id: rule for rule in selected}
    results: list[EvaluationResult] = []
    seen: set[str] = set()

    def take(
        rule: DomainRule, bag: Mapping[str, Any], *, chain: dict[str, Any] | None
    ) -> None:
        """求值一条规则; ``chain`` 非 None 时把派生输出回灌进链式池。"""
        if rule.rule_id in seen:
            return
        seen.add(rule.rule_id)
        result = _evaluate_one(rule, bag)
        if result.applicable or result.facts_used:
            results.append(result)
        if (
            chain is not None
            and result.applicable
            and result.ok
            and rule.is_derive
            and rule.output_key
            and rule.output_key not in chain
        ):
            # 派生值回灌链式池 (同名已有值时保留先到者, 保证确定性)
            chain[rule.output_key] = result.actual

    def run_chain(order: Sequence[DomainRule], pool: dict[str, Any]) -> None:
        """链式点火 (按轮批量回灌): 每轮用冻结池求值, 轮末合并派生值 ——
        一轮 = 一跳, 最多 ``max_depth`` 轮; 无新派生值即提前收敛。"""
        for _ in range(max_depth):
            staged: dict[str, Any] = {}
            for rule in order:
                if rule.rule_id in seen or not rule.input_params:
                    continue
                if set(rule.input_params) <= set(pool):
                    take(rule, pool, chain=staged)
            new_vals = {k: v for k, v in staged.items() if k not in pool}
            if not new_vals:
                break
            pool.update(new_vals)

    if facts is not None:
        if not facts:
            return []
        pool: dict[str, Any] = dict(facts)
        staged: dict[str, Any] = {}
        for hint in hints or []:
            rule_id = str(_field(hint, "rule_id") or "").strip()
            rule = index.get(rule_id)
            if rule is not None:
                take(rule, pool, chain=staged)
        pool.update({k: v for k, v in staged.items() if k not in pool})
        run_chain(selected, pool)
        return results

    if query:
        bindings = extract_facts(query, hints or [], rules=selected)
        hint_ids: list[str] = []
        for hint in hints or []:
            rule_id = str(_field(hint, "rule_id") or "").strip()
            if rule_id and rule_id not in hint_ids:
                hint_ids.append(rule_id)
        hint_rules = [index[rid] for rid in hint_ids if rid in index]
        # 绑定合并为链式池: 同名冲突 (两规则绑定到不同值) 保守剔除该参数
        pool = {}
        conflicted: set[str] = set()
        for bound in bindings.values():
            for name, value in bound.items():
                if name in pool and pool[name] != value:
                    conflicted.add(name)
                else:
                    pool.setdefault(name, value)
        for name in conflicted:
            pool.pop(name, None)
        run_chain(hint_rules, pool)
        # 旧语义回退: 未进入链式求值但绑定了部分输入的提示规则,
        # 保留一条"缺少输入, 未求值"的痕迹 (不产出任何计算值)。
        for rule_id, bound in bindings.items():
            rule = index.get(rule_id)
            if rule is not None and rule.rule_id not in seen and bound:
                take(rule, bound, chain=None)
    return results


def parse_facts(pairs: Sequence[str]) -> dict[str, Any]:
    """把 CLI 的 ``name=value`` 参数解析成事实映射。

    - 值是纯数字 → ``float``; ``true``/``false`` (大小写不敏感) → ``bool``;
      其余按字符串 (如 ``default_state=normally_closed``);
    - 重复键后者覆盖前者;
    - 非法项 (缺 ``=`` / 空参数名) → ``ValueError`` (fail-fast, 不静默跳过)。
    """
    out: dict[str, Any] = {}
    for item in pairs or []:
        text = str(item)
        if "=" not in text:
            raise ValueError(f"事实参数须为 name=value 形式: {text!r}")
        name, _, raw = text.partition("=")
        name = name.strip()
        if not name:
            raise ValueError(f"事实参数缺少参数名: {text!r}")
        out[name] = _parse_scalar(raw.strip())
    return out


def _parse_scalar(raw: str) -> Any:
    """标量解析: 布尔 → 数字 → 字符串 (保底)。"""
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return float(raw)
    except ValueError:
        return raw


def render_evaluations(results: Sequence[EvaluationResult]) -> list[str]:
    """把求值结果渲染成行列表 (上下文分区 / 推理链共用同一口径)。"""
    return [result.display() for result in results]
