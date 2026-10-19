"""目标导向的规则链规划与求解 (通用推导引擎, 不写死任何链路)。

设计原则: 规则库 (:mod:`matrag.domain.rules_loader`) 是**数据**, 本模块是
**通用引擎** —— 给定「目标参数 + 已知值」, 引擎在规则图上动态搜索推导链:

- **正向** (forward): 规则的输出可由其输入算出 (复用
  :class:`matrag.domain.rules.SafeExpression` 白名单求值);
- **反解** (invert): 规则的输出已知时, 对表达式做**单变量反演**求某个输入 ——
  求解核心为 SymPy (``sympy.solve``), 本模块只做保守守卫与**数值回代验证**
  (目标变量在表达式中恰好出现一次、不得在幂指数上、其余变量必须数值; 不满足
  即保守放弃, 绝不做非法变形);
- **规划**: 从目标参数出发做有界 DFS, 产出 ``PlanStep`` 序列 —— 任何链路
  (无论几跳、正向还是反解) 都是**按问题现场推导**的, 引擎里没有针对具体
  问题/参数/规则组合的分支;
- **等价视图**: 规则库里 ``output_`` 前缀参数与其去前缀基名 (如
  ``output_power ↔ power``) 视为同一物理量的两种命名视图, 由规则库**自动
  推导** (输入侧 ``input_`` 不参与 —— 交流输入功率含 PF, 与通用 ``P=U×I``
  不等价); 规划与执行双向填充, 冲突时保留原值;
- **求解**: 按规划逐跳确定性执行并留轨迹 (每跳的 rule_id / 模式 / 数值),
  供审计与展示。

示例 (不依赖任何"手写重组式"规则)::

    derive_value("output_current",
                 {"input_power": 100, "efficiency": 90, "output_voltage": 12})
    # 规划: [倒解 K-PWR-001: η = Pout/Pin×100 ⟹ Pout = 90] → [正向 K-PWR-127]
    # 结果: 7.5

全部离线确定性: 不调 LLM、不联网。
"""

from __future__ import annotations

import ast
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from matrag.domain.rules import SafeExpression
from matrag.domain.rules_loader import DomainRule, load_rules

logger = logging.getLogger(__name__)

__all__ = [
    "MODE_FORWARD",
    "MODE_INVERT",
    "Derivation",
    "PlanStep",
    "default_equivalences",
    "derive_value",
    "fill_equivalences",
    "invert_expression",
    "plan_derivation",
]

#: 规划步模式: 正向代入 / 表达式反解。
MODE_FORWARD = "forward"
MODE_INVERT = "invert"

#: 默认最大推导步数 (跳数上限, 防循环与爆炸)。
DEFAULT_MAX_DEPTH = 4


@dataclass(frozen=True)
class PlanStep:
    """推导链的一步。

    Attributes:
        rule_id: 应用的规则 id。
        mode: :data:`MODE_FORWARD` 或 :data:`MODE_INVERT`。
        target: 本步求出的参数名 (正向=规则输出; 反解=被反演的输入之一)。
    """

    rule_id: str
    mode: str
    target: str


@dataclass(frozen=True)
class Derivation:
    """一次成功的推导: 目标值 + 规划 + 人类可读轨迹。

    Attributes:
        target: 目标参数名。
        value: 求得的数值。
        plan: 推导链 (按执行顺序)。
        steps: 逐步轨迹行 (含每跳代入/反解的数值, 供审计展示)。
    """

    target: str
    value: Any
    plan: tuple[PlanStep, ...]
    steps: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict。"""
        return {
            "target": self.target,
            "value": self.value,
            "plan": [
                {"rule_id": s.rule_id, "mode": s.mode, "target": s.target}
                for s in self.plan
            ],
            "steps": list(self.steps),
        }


# --------------------------------------------------------------------------- #
# 表达式反解 (单变量, 保守; 求解核心 = SymPy)
# --------------------------------------------------------------------------- #


def _sympy() -> Any:
    """惰性导入 SymPy (首次约 1s, 不拖慢其它导入路径)。"""
    import sympy

    return sympy


def _whitelisted(node: ast.AST) -> bool:
    """结构白名单: 仅 数字常量 / 变量 / ``+ - * / **`` (与 SafeExpression 同口径)。"""
    if isinstance(node, ast.Constant):
        return not isinstance(node.value, bool) and isinstance(node.value, (int, float))
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.UnaryOp):
        return isinstance(node.op, (ast.UAdd, ast.USub)) and _whitelisted(node.operand)
    if isinstance(node, ast.BinOp):
        return isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
        ) and _whitelisted(node.left) and _whitelisted(node.right)
    return False


def _parse_whitelisted(expr: object) -> ast.expr | None:
    """解析表达式并做结构白名单校验; 任何违规 (含语法错误) 返回 ``None``。"""
    if not isinstance(expr, str) or not expr.strip():
        return None
    try:
        tree = ast.parse(expr, mode="eval").body
    except (SyntaxError, ValueError):
        return None
    return tree if _whitelisted(tree) else None


def _count_name(node: ast.AST, name: str) -> int:
    return sum(
        1 for child in ast.walk(node) if isinstance(child, ast.Name) and child.id == name
    )


def _names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _target_in_exponent(node: ast.AST, target: str) -> bool:
    """目标变量是否出现在幂指数上 (需求对数反解 —— 保守放弃, 与旧版语义一致)。"""
    return any(
        isinstance(child, ast.BinOp)
        and isinstance(child.op, ast.Pow)
        and _count_name(child.right, target) > 0
        for child in ast.walk(node)
    )


def _real_solutions(items: Sequence[Any]) -> list[float]:
    """把 SymPy 解列表过滤为有限实根 (复数/非数值/非有限值一律剔除)。"""
    reals: list[float] = []
    for item in items:
        if getattr(item, "is_real", None) is False:
            continue
        try:
            value = float(item)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            reals.append(value)
    return reals


def _verified(
    expr: str, target: str, env: Mapping[str, float], wanted: float, solved: float
) -> bool:
    """把候选解代回原式做数值验证 (相对容差 1e-6; 失败/不确定 → 不通过)。"""
    try:
        value = SafeExpression(expr).evaluate({**env, target: solved})
    except Exception:  # noqa: BLE001 —— 验证失败即不通过 (宁可不解, 不可错解)
        return False
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return abs(float(value) - wanted) <= 1e-6 * max(1.0, abs(wanted))


def invert_expression(
    expr: str,
    target: str,
    known: Mapping[str, Any],
    result: Any,
) -> float | None:
    """对 ``expr = result`` 反解 ``target``; 不可安全反解时返回 ``None``。

    求解核心为 SymPy 的 ``sympy.solve`` (不自研级数变形), 本函数只做保守守卫:

    - ``target`` 在表达式中**恰好出现一次**, 且不得出现在幂指数上
      (需求对数反解 —— 保守放弃);
    - 表达式仅由数字常量、已知变量与 ``+ - * / **`` 组成 (其它节点一律拒绝);
    - 其余变量全部必须是有限实数 (布尔/字符串/缺失 → 拒绝), ``result`` 同理;
    - 实根必须唯一; ``±`` 对称对取正值 (对应旧版「正根」语义), 其余多解保守拒绝
      (奇次幂负结果有唯一实根, 允许);
    - 候选解必须**代回原式数值验证**通过 (相对容差 1e-6), 否则返回 ``None``。

    Args:
        expr: 规则表达式 (``derive.expr``)。
        target: 要被反解的变量名 (表达式中恰好出现一次)。
        known: 其余变量的已知值 (数值)。
        result: 表达式整体应等于的值。

    Returns:
        ``target`` 的解; 不可反解 / 不可唯一确定 / 验证不过时为 ``None``。
    """
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        return None
    wanted = float(result)
    if not math.isfinite(wanted):
        return None
    tree = _parse_whitelisted(expr)
    if tree is None:
        return None
    if _count_name(tree, target) != 1 or _target_in_exponent(tree, target):
        return None
    env: dict[str, float] = {}
    for name in _names(tree):
        if name == target:
            continue
        value = known.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        env[name] = number

    sympy = _sympy()
    symbol = sympy.Symbol(target)
    symbols = {name: sympy.Symbol(name) for name in _names(tree)}
    try:
        expression = sympy.sympify(expr, locals=symbols)
        equation = sympy.Eq(
            expression.subs({symbols[name]: value for name, value in env.items()}),
            sympy.Float(wanted),
        )
        raw_solutions = sympy.solve(equation, symbol)
    except Exception as exc:  # noqa: BLE001 —— SymPy 处理不了 → 保守放弃, 但留痕
        logger.warning("planner.solve_skipped: expr=%r target=%r err=%s", expr, target, exc)
        return None
    reals = _real_solutions(raw_solutions)
    if len(reals) == 1:
        solved = reals[0]
    elif (
        len(reals) == 2
        and math.isclose(reals[0], -reals[1], rel_tol=1e-12, abs_tol=1e-12)
        and max(reals) > 0
    ):
        solved = max(reals)  # ± 对称对 → 取正根 (旧版语义)
    else:
        return None
    if not _verified(expr, target, env, wanted, solved):
        return None
    return solved


# --------------------------------------------------------------------------- #
# 规划 (有界 DFS, 正向优先)
# --------------------------------------------------------------------------- #


def default_equivalences(
    rules: Sequence[DomainRule],
) -> dict[str, tuple[str, ...]]:
    """从规则库自动推导"等价视图"表 (对称映射): ``output_x ↔ x``。

    仅当两种写法都在规则参数中出现过才建立映射; 输入侧 ``input_x`` 不参与
    (交流输入功率含功率因数, 与通用 ``P=U×I`` 不等价)。
    """
    names: set[str] = set()
    for rule in rules:
        if not rule.is_derive:
            continue
        names.update(rule.input_params)
        if rule.output_key:
            names.add(rule.output_key)
    pairs: dict[str, list[str]] = {}
    for name in sorted(names):
        if not name.startswith("output_"):
            continue
        base = name[len("output_"):]
        if base in names:
            pairs.setdefault(name, []).append(base)
            pairs.setdefault(base, []).append(name)
    return {key: tuple(sorted(set(values))) for key, values in pairs.items()}


def fill_equivalences(pool: dict[str, Any], aliases: Mapping[str, Sequence[str]]) -> None:
    """把池中已有值的等价视图补齐 (缺侧填充, 不覆盖已有值)。"""
    for name in list(pool):
        for alias in aliases.get(name, ()):
            if alias not in pool:
                pool[alias] = pool[name]


def _plan_for_target(
    target: str,
    known: frozenset[str],
    rules: Sequence[DomainRule],
    budget: int,
    visiting: frozenset[str],
    aliases: Mapping[str, Sequence[str]],
) -> tuple[PlanStep, ...] | None:
    """从 ``target`` 出发搜索一条 ≤ ``budget`` 步的推导链 (无则 ``None``)。

    顺序: ① 正向(产出 target 的规则, 输入全部可规划); ② 反解(target 是某规则
    的输入, 该规则输出已知/可规划且其余输入可规划); ③ 等价视图(target 的去
    前缀基名等价物可规划; 执行阶段按等价填充 target)。规则按 id 排序保证确定性。
    """
    if target in known:
        return ()
    if budget <= 0 or target in visiting:
        return None

    derive_rules = [r for r in rules if r.is_derive and r.output_key]

    # ① 正向: target == rule.output
    for rule in sorted(derive_rules, key=lambda r: r.rule_id):
        if rule.output_key != target:
            continue
        sub_budget = budget - 1
        prefix: tuple[PlanStep, ...] = ()
        ok = True
        branch_visiting = visiting | {target}
        for param in rule.input_params:
            if param == target:
                ok = False  # 自环, 放弃
                break
            piece = _plan_for_target(
                param, known, rules, sub_budget, branch_visiting, aliases
            )
            if piece is None:
                ok = False
                break
            prefix = prefix + piece
            sub_budget = max(0, sub_budget - len(piece))
        if ok and len(prefix) + 1 <= budget:
            return prefix + (PlanStep(rule.rule_id, MODE_FORWARD, target),)

    # ② 反解: target 是 rule 的输入, 规则输出已知/可规划, 其余输入可规划
    for rule in sorted(derive_rules, key=lambda r: r.rule_id):
        if target not in rule.input_params or rule.output_key == target:
            continue
        sub_budget = budget - 1
        prefix: tuple[PlanStep, ...] = ()
        ok = True
        branch_visiting = visiting | {target}
        for param in (rule.output_key, *rule.input_params):
            if param == target:
                continue
            piece = _plan_for_target(
                param, known, rules, sub_budget, branch_visiting, aliases
            )
            if piece is None:
                ok = False
                break
            prefix = prefix + piece
            sub_budget = max(0, sub_budget - len(piece))
        if ok and len(prefix) + 1 <= budget:
            return prefix + (PlanStep(rule.rule_id, MODE_INVERT, target),)

    # ③ 等价视图: 由去前缀基名等价物满足 (如 output_power ↔ power)
    for alias in aliases.get(target, ()):
        if alias in visiting:
            continue
        piece = _plan_for_target(
            alias, known, rules, budget, visiting | {target}, aliases
        )
        if piece is not None:
            return piece

    return None


def plan_derivation(
    target: str,
    known: Sequence[str] | frozenset[str],
    *,
    rules: Sequence[DomainRule] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    equivalences: Mapping[str, Sequence[str]] | None = None,
) -> tuple[PlanStep, ...] | None:
    """为目标参数规划一条推导链 (纯结构搜索, 不需要数值)。

    Args:
        target: 目标参数名。
        known: 已知参数名集合 (问题里给出的量)。
        rules: 规则集; ``None`` 表示加载默认规则库。
        max_depth: 最大步数 (跳数), 防循环与组合爆炸。
        equivalences: 等价视图表 (对称); ``None`` 表示由规则库自动推导
            (:func:`default_equivalences`)。

    Returns:
        ``PlanStep`` 序列 (执行顺序, 依赖在前); 无法在 ``max_depth`` 步内
        到达目标时返回 ``None``。

    Raises:
        ValueError: ``max_depth < 1``。
    """
    if max_depth < 1:
        raise ValueError(f"max_depth 须 >= 1, 实际是 {max_depth!r}")
    selected = list(load_rules()) if rules is None else list(rules)
    aliases = (
        default_equivalences(selected) if equivalences is None else dict(equivalences)
    )
    # 迭代加深: 从 1 步起逐层放宽, 首个命中的即"预算内最短"链 (确定性)
    for budget in range(1, max_depth + 1):
        plan = _plan_for_target(
            target, frozenset(known), selected, budget, frozenset(), aliases
        )
        if plan is not None:
            return plan
    return None


# --------------------------------------------------------------------------- #
# 求解 (按规划逐跳确定性执行)
# --------------------------------------------------------------------------- #


def derive_value(
    target: str,
    facts: Mapping[str, Any],
    *,
    rules: Sequence[DomainRule] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    equivalences: Mapping[str, Sequence[str]] | None = None,
) -> Derivation | None:
    """由已知值出发, 动态规划并执行推导链求 ``target``; 失败返回 ``None``。

    规划与执行都是确定性的; 每跳留轨迹 (规则 id / 模式 / 数值)。规划可解但
    执行失败 (如除零) 时同样返回 ``None`` —— 不输出半成品结论。

    Args:
        target: 目标参数名。
        facts: 已知参数值 (数值)。
        rules: 规则集; ``None`` 表示加载默认规则库。
        max_depth: 最大步数。
        equivalences: 等价视图表; ``None`` 表示由规则库自动推导。
    """
    selected = list(load_rules()) if rules is None else list(rules)
    index = {rule.rule_id: rule for rule in selected}
    aliases = (
        default_equivalences(selected) if equivalences is None else dict(equivalences)
    )
    plan = plan_derivation(
        target, tuple(facts), rules=selected, max_depth=max_depth, equivalences=aliases
    )
    if plan is None:
        return None
    pool: dict[str, Any] = dict(facts)
    fill_equivalences(pool, aliases)
    steps: list[str] = []
    for step in plan:
        rule = index[step.rule_id]
        expr = str((rule.derive or {}).get("expr", ""))
        try:
            if step.mode == MODE_FORWARD:
                env = {name: pool[name] for name in rule.input_params}
                value = SafeExpression(expr).evaluate(env)
                pool[step.target] = value
                given = "、".join(f"{name}={pool[name]}" for name in rule.input_params)
                steps.append(
                    f"{rule.rule_id} [正向]: 代入 {given} → {step.target} = {value}"
                )
            else:
                known = {
                    name: pool[name]
                    for name in rule.input_params
                    if name != step.target
                }
                out_value = pool.get(rule.output_key)
                value = invert_expression(
                    expr, step.target, known, out_value
                )
                if value is None:
                    return None
                pool[step.target] = value
                given = "、".join(f"{name}={pool[name]}" for name in rule.input_params if name != step.target)
                steps.append(
                    f"{rule.rule_id} [反解]: 由 {rule.output_key}={out_value} 与 {given}"
                    f" → {step.target} = {value}"
                )
            fill_equivalences(pool, aliases)
        except Exception as exc:  # noqa: BLE001 —— 数值执行失败 (如除零) → 放弃推导, 留痕
            logger.warning("planner.derive_failed: target=%s err=%s", target, exc)
            return None
    if target not in pool:
        return None
    return Derivation(
        target=target, value=pool[target], plan=plan, steps=tuple(steps)
    )
