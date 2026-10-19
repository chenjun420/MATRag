"""z3 spike: 把公式型领域规则编码为 z3 实数约束, 验证可行性与性能 (P3-0d 后续评估)。

背景: 推理三级 (L1 确定性 planner / L2 LLM 编排 / L3 Agent 工具循环) 中的规则
求解由 planner(SymPy 数值反解) 承担。本 spike 探索"把 55 条公式规则整体编码为
z3 约束、由 SMT 求解器直接求解"的替代路线, 回答:

1. 覆盖率 —— 规则表达式 (白名单 AST: 四则/比较/三元/abs/min/max/sqrt) 有多少条
   可原样编码进实数算术 (QFLRA/QFNRA);
2. 正确性 —— 对 data/eval/derive_questions.yaml 的 16 题, z3 求出的目标值与
   L1 planner 的推导值是否一致; 目标是否唯一 (唯一性用 "取模型值后加 != 再查
   unsat" 判定; 多解 = 与"拒答"决策一致);
3. 性能 —— 每题建模+求解+唯一性判定的墙钟耗时。

已知语义差异 (spike 结论要用的反例):
- z3 实数除法是全函数, 除零不会报"无定义" —— B13 (0V 除) 会得到无法折算的
  90/0 项而非拒绝; Python 引擎则 fail-fast 返回 None。信任边界要求"无定义
  必须拒答", 用 z3 需额外界定 (显式守卫约束), 否则语义被放宽。
- z3 无 exp/log; 规则表达式若含此类函数则不可编码 (本库公式规则实际未用)。

用法: ``python scripts/z3_spike.py`` (需已 pip 安装 z3-solver; 不调 LLM, 离线)。

产物: 控制台报告 (覆盖率/逐题对照/耗时), 结论人工回填 TODO.md。
"""

from __future__ import annotations

import ast
import itertools
import math
import time
from fractions import Fraction
from pathlib import Path
from typing import Any

import yaml
import z3

from matrag.domain.planner import derive_value
from matrag.domain.query_derive import facts_pool, target_candidates
from matrag.domain.rules_loader import DomainRule, load_rules

#: 每题的 z3 求解限额 (含建模后的 sat/唯一性两次 check)。
_TIMEOUT_MS = 10_000
#: 白名单常量 (与 matrag.domain.rules.SafeExpression 对齐)。
_SAFE_CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau}
#: 评估集路径。
_QUESTIONS = Path("data/eval/derive_questions.yaml")
#: sqrt 辅助变量计数器 (跨 encoder 唯一, 避免同名变量碰撞造成虚假 unsat)。
_SQRT_IDS = itertools.count(1)


class _UnsupportedError(Exception):
    """规则表达式含无法编码进 z3 的构造。"""


def _real(value: float | int | Fraction) -> z3.ArithRef:
    """float/int/Fraction → z3 实数值 (经 Fraction 保精度, 避免二进制→十进制误差)。"""
    if isinstance(value, Fraction):
        return z3.RealVal(value)
    if isinstance(value, int):
        return z3.RealVal(value)
    return z3.RealVal(Fraction(value))


class _Encoder:
    """规则表达式 AST → z3 表达式 (变量按名字缓存共享; 不支持的构造抛 _UnsupportedError)。"""

    def __init__(self, cache: dict[str, z3.ArithRef]) -> None:
        self._cache = cache
        self.aux: list[z3.BoolRef] = []  # sqrt 等引入的辅助约束

    def var(self, name: str) -> z3.ArithRef:
        if name not in self._cache:
            self._cache[name] = z3.Real(name)
        return self._cache[name]

    def encode(self, source: str) -> Any:
        return self._visit(ast.parse(source, mode="eval").body)

    def _visit(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return z3.BoolVal(node.value)
            if isinstance(node.value, (int, float)):
                return _real(node.value)
            raise _UnsupportedError(f"常量 {type(node.value).__name__}")
        if isinstance(node, ast.Name):
            if node.id in _SAFE_CONSTANTS:
                return _real(_SAFE_CONSTANTS[node.id])
            return self.var(node.id)
        if isinstance(node, ast.BinOp):
            return self._binop(node)
        if isinstance(node, ast.UnaryOp):
            operand = self._visit(node.operand)
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return operand
            if isinstance(node.op, ast.Not):
                return z3.Not(operand)
            raise _UnsupportedError(f"一元运算 {type(node.op).__name__}")
        if isinstance(node, ast.IfExp):
            test = self._visit(node.test)
            if not isinstance(test, z3.BoolRef):
                # Python 真值语义: 非布尔表达式按 "!= 0" 处理 (如 0.5 if stagger else ...)
                test = test != 0
            return z3.If(test, self._visit(node.body), self._visit(node.orelse))
        if isinstance(node, ast.Compare):
            left = self._visit(node.left)
            pieces = []
            for op, right_node in zip(node.ops, node.comparators, strict=True):
                right = self._visit(right_node)
                pieces.append(_compare(op, left, right))
                left = right
            expr = pieces[0]
            for piece in pieces[1:]:
                expr = z3.And(expr, piece)
            return expr
        if isinstance(node, ast.BoolOp):
            values = [self._visit(item) for item in node.values]
            return z3.And(*values) if isinstance(node.op, ast.And) else z3.Or(*values)
        if isinstance(node, ast.Call):
            return self._call(node)
        raise _UnsupportedError(f"语法 {type(node).__name__}")

    def _binop(self, node: ast.BinOp) -> Any:
        left = self._visit(node.left)
        right = self._visit(node.right)
        op = node.op
        if isinstance(op, ast.Add):
            return left + right
        if isinstance(op, ast.Sub):
            return left - right
        if isinstance(op, ast.Mult):
            return left * right
        if isinstance(op, ast.Div):
            return left / right  # 注意: z3 实数除法对 0 是全函数 (无"无定义"语义)
        if isinstance(op, ast.Pow):
            exponent = node.right
            if isinstance(exponent, ast.Constant) and exponent.value == 0.5:
                return self._sqrt(left)
            if (
                isinstance(exponent, ast.Constant)
                and isinstance(exponent.value, int)
                and exponent.value >= 0
            ):
                return left ** exponent.value
            raise _UnsupportedError("幂指数非字面量")
        raise _UnsupportedError(f"二元运算 {type(op).__name__}")

    def _call(self, node: ast.Call) -> Any:
        name = node.func.id
        args = [self._visit(arg) for arg in node.args]
        if name == "abs":
            return z3.If(args[0] >= 0, args[0], -args[0])
        if name == "min":
            result = args[0]
            for item in args[1:]:
                result = z3.If(result <= item, result, item)
            return result
        if name == "max":
            result = args[0]
            for item in args[1:]:
                result = z3.If(result >= item, result, item)
            return result
        if name == "sqrt":
            return self._sqrt(args[0])
        raise _UnsupportedError(f"函数 {name}()")

    def _sqrt(self, expr: Any) -> Any:
        root = z3.Real(f"__sqrt{next(_SQRT_IDS)}")
        self.aux.append(root * root == expr)
        self.aux.append(root >= 0)
        return root


def _compare(op: ast.cmpop, left: Any, right: Any) -> Any:
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    if isinstance(op, ast.Lt):
        return left < right
    if isinstance(op, ast.LtE):
        return left <= right
    if isinstance(op, ast.Gt):
        return left > right
    if isinstance(op, ast.GtE):
        return left >= right
    raise _UnsupportedError(f"比较 {type(op).__name__}")


def _formula_rules(rules: list[DomainRule]) -> list[DomainRule]:
    return [rule for rule in rules if rule.is_derive and rule.output_key]


def _encode_rule(
    rule: DomainRule, cache: dict[str, z3.ArithRef]
) -> tuple[z3.ArithRef, Any, list[Any]]:
    """编码单条规则 → ``(输出变量, 表达式, 辅助约束)``；不支持即抛 ``_UnsupportedError``。"""
    encoder = _Encoder(cache)
    expr = encoder.encode(str((rule.derive or {}).get("expr", "")))
    return encoder.var(rule.output_key), expr, encoder.aux


def _coverage(rules: list[DomainRule]) -> tuple[int, list[str]]:
    ok = 0
    bad: list[str] = []
    for rule in _formula_rules(rules):
        try:
            _encode_rule(rule, {})
        except (_UnsupportedError, SyntaxError, ValueError) as exc:
            bad.append(f"{rule.rule_id}({exc})")
        else:
            ok += 1
    return ok, bad


def _to_float(value: Any) -> float | None:
    try:
        if z3.is_rational_value(value):
            return float(value.as_fraction())
        if z3.is_algebraic_value(value):
            return float(value.as_decimal(16).rstrip("?"))
        return float(str(value))
    except (ValueError, TypeError):
        return None  # 如除零项 90/0: z3 不定义 → 无法折算


def _unique_value(solver: z3.Solver, target: z3.ArithRef, model_value: Any) -> str:
    """取模型值后追加 ``target != 模型值`` 再查: unsat = 唯一, sat = 多解。"""
    solver.push()
    solver.add(target != model_value)
    verdict = solver.check()
    solver.pop()
    if verdict == z3.unsat:
        return "唯一"
    if verdict == z3.sat:
        return "多解"
    return "唯一性超时"


def _solve(rules: list[DomainRule], pool: dict[str, Any], target: str) -> str:
    """建全库方程系统 + 已知值约束, 解出 target 并判唯一性; 返回判读文本。"""
    cache: dict[str, z3.ArithRef] = {}
    solver = z3.Solver()
    solver.set("timeout", _TIMEOUT_MS)
    for rule in _formula_rules(rules):
        encoder = _Encoder(cache)
        try:
            expr = encoder.encode(str((rule.derive or {}).get("expr", "")))
        except (_UnsupportedError, SyntaxError, ValueError):
            continue
        solver.add(encoder.var(rule.output_key) == expr)
        solver.add(*encoder.aux)
    for name, value in pool.items():
        solver.add(_Encoder(cache).var(name) == _real(float(value)))
    target_var = _Encoder(cache).var(target)
    if solver.check() != z3.sat:
        return "未解(unsat)"
    model_value = solver.model().eval(target_var, model_completion=False)
    if model_value is None:
        return "未定(无约束)"
    verdict = _unique_value(solver, target_var, model_value)
    if verdict != "唯一":
        return verdict
    number = _to_float(model_value)
    return f"{number!r} 唯一" if number is not None else f"非数值项({model_value})"


def _run_question(question: dict[str, Any], rules: list[DomainRule]) -> dict[str, Any]:
    qid = str(question["id"])
    text = str(question["question"])
    expect = question.get("expect", {})
    started = time.perf_counter()
    pool = facts_pool(text, rules=rules)
    row: dict[str, Any] = {
        "id": qid,
        "kind": question.get("kind", ""),
        "expect": expect.get("deterministic"),
        "target": None,
        "planner": None,
        "z3": "",
        "ms": 0.0,
    }
    if not pool:
        row["z3"] = "(题干无数值)"
        row["ms"] = round((time.perf_counter() - started) * 1000, 1)
        return row
    candidates = target_candidates(text, set(pool), rules=rules)
    for candidate in candidates:  # 与 L1 相同的目标解析: 取首个可推导链的候选
        derivation = derive_value(candidate, pool, rules=rules)
        if derivation is not None and derivation.plan:
            row["target"] = candidate
            row["planner"] = float(derivation.value)
            break
    if row["target"] is None and candidates:
        row["target"] = candidates[0]
    row["z3"] = "(无候选目标)" if row["target"] is None else _solve(
        rules, pool, str(row["target"])
    )
    row["ms"] = round((time.perf_counter() - started) * 1000, 1)
    return row


def main() -> int:
    rules = load_rules()
    questions = yaml.safe_load(_QUESTIONS.read_text(encoding="utf-8"))["questions"]
    formula = _formula_rules(rules)
    ok, bad = _coverage(rules)
    print(f"公式规则编码覆盖: {ok}/{len(formula)}")
    for item in bad:
        print(f"  不可编码: {item}")
    print(f"评估题: {len(questions)} 条")
    total_ms = 0.0
    for question in questions:
        row = _run_question(question, rules)
        total_ms += float(row["ms"])
        print(
            f"{row['id']:<4}{row['kind']:<8}expect={row['expect']!s:<22}"
            f"target={row['target']!s:<18}planner={row['planner']!s:<22}"
            f"z3={row['z3']!s:<24}{row['ms']}ms"
        )
    print(f"合计: {total_ms:.0f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
