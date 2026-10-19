"""确定性推理规则引擎：把 power 域的 130 条规则（``power_rules.yaml``）落到 semantica 上。

实测结论 (semantica 0.7.0, 见 ``tests/test_domain_rules.py``)::

    # semantica.reasoning.reasoner.Reasoner.add_rule
    def add_rule(self, rule_def: Union[str, Rule]) -> Rule: ...
    # semantica.reasoning.datalog_reasoner.DatalogReasoner
    def add_rule(self, rule_str: str) -> None: ...
    def add_fact(self, fact: Any) -> None: ...

- ``Reasoner`` 只认 ``IF a AND b THEN c`` 形式的**字符串模式匹配** (条件/结论都是
  ``Predicate(?var)`` 文本, 靠正则 ``(?P<var>.+?)`` 绑定), 没有算术也没有比较运算;
- ``DatalogReasoner`` 是 Horn 子句 + 半朴素不动点求值, 变量以大写字母开头,
  **同样只有合一 (unification), 没有算术/比较**;
- 因此 130 条规则按"引擎管规则激活与连接, 适配层管算术/布尔求值"分工:
  - 公式型规则 (53 条): ``derive.expr`` 是 Python 风格表达式, 由
    :class:`SafeExpression` (白名单 AST) 求值;
  - 约束型规则 (77 条): ``constraint.shape`` 是 SHACL (Turtle, 含 sh:sparql /
    sh:property), 由 semantica 的 ``run_shacl_validation`` 求值。
- 结论 ("规则成立 + 取值") 由 semantica ``DatalogReasoner`` 的前推得出并回查,
  引擎不只是装饰: 事实库里缺少某条规则的输入参数时, 该规则不会激活。
"""

from __future__ import annotations

import ast
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from semantica.reasoning import DatalogReasoner

from matrag.domain.rules_loader import (
    DomainRule,
    load_rules,
    render_datalog,
    rule_slug,
)
from matrag.errors import MatragError


class RuleEvaluationError(MatragError):
    """规则求值失败 (表达式非法/事实缺失/SHACL 结构不认识)。"""


# --------------------------------------------------------------------------- #
# 通用表达式求值器 (白名单 AST)
# --------------------------------------------------------------------------- #

_SAFE_FUNCTIONS: dict[str, Any] = {
    "abs": abs,
    "min": min,
    "max": max,
    "sum": sum,
    "round": round,
    "ceil": math.ceil,
    "floor": math.floor,
    "sqrt": math.sqrt,
    "exp": math.exp,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
}
_SAFE_CONSTANTS: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}
_BIN_OPS: dict[type, Any] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
}
_COMPARE_OPS: dict[type, Any] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}
_ALLOWED_NODES = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.List,
    ast.Tuple,
    ast.Subscript,
    ast.Slice,
    ast.GeneratorExp,
    ast.ListComp,
    ast.SetComp,
    ast.comprehension,
    ast.And,
    ast.Or,
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
)


class SafeExpression:
    """白名单 AST 表达式求值器 (引擎适配层, 与具体规则完全无关)。

    只放行: 常量/变量/四则运算 (+ - * / // % **)/一元负号/布尔与比较/三元条件/
    白名单函数 (abs,min,max,sum,sqrt,exp,log*,ceil,floor,round) 与常量 (pi,e,tau)/
    列表下标/推导式 (``sum(t * t for t in components)``)。
    属性访问、lambda、import、星号解包等一律拒绝。
    """

    __slots__ = ("source", "_tree", "_names")

    def __init__(self, source: str) -> None:
        self.source = source
        try:
            self._tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:  # pragma: no cover - 由调用方测试覆盖
            raise RuleEvaluationError(f"表达式语法错误: {source!r} ({exc})") from exc
        self._names = frozenset(self._collect_names(self._tree))
        for node in ast.walk(self._tree):
            if not isinstance(node, _ALLOWED_NODES):
                raise RuleEvaluationError(
                    f"表达式含未放行的语法节点 {type(node).__name__}: {source!r}"
                )
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in _SAFE_FUNCTIONS:
                    raise RuleEvaluationError(
                        f"表达式调用了未放行的函数: {source!r}"
                    )

    @property
    def names(self) -> frozenset[str]:
        """表达式引用的变量名。"""
        return self._names

    @staticmethod
    def _collect_names(tree: ast.AST) -> list[str]:
        """收集自由变量 (排除白名单函数名与推导式内部绑定的名字)。"""
        names: list[str] = []

        def visit(node: ast.AST, bound: frozenset[str]) -> None:
            if isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Load) and node.id not in bound:
                    names.append(node.id)
                return
            if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
                local = set(bound)
                for generator in node.generators:
                    visit(generator.iter, frozenset(local))
                    local |= {
                        n.id for n in ast.walk(generator.target) if isinstance(n, ast.Name)
                    }
                for generator in node.generators:
                    for condition in generator.ifs:
                        visit(condition, frozenset(local))
                visit(node.elt, frozenset(local))
                return
            if isinstance(node, ast.Call):
                for argument in node.args:
                    visit(argument, bound)
                return
            for child in ast.iter_child_nodes(node):
                visit(child, bound)

        visit(tree, frozenset())
        return names

    def evaluate(self, env: Mapping[str, Any]) -> Any:
        """在给定变量环境里求值; 缺变量报错 (fail-fast)。"""
        for name in self._names:
            if name not in env and name not in _SAFE_CONSTANTS:
                raise RuleEvaluationError(
                    f"表达式 {self.source!r} 缺少变量 {name}: 已知 {sorted(env)}"
                )
        return self._visit(self._tree, dict(env))

    # -- 递归求值 ---------------------------------------------------------- #
    def _visit(self, node: ast.AST, env: dict[str, Any]) -> Any:
        if isinstance(node, ast.Expression):
            return self._visit(node.body, env)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            if node.id in _SAFE_CONSTANTS:
                return _SAFE_CONSTANTS[node.id]
            raise RuleEvaluationError(f"未定义变量: {node.id}")
        if isinstance(node, ast.BinOp):
            op = _BIN_OPS.get(type(node.op))
            if op is None:
                raise RuleEvaluationError(f"不支持的运算符: {type(node.op).__name__}")
            return op(self._visit(node.left, env), self._visit(node.right, env))
        if isinstance(node, ast.UnaryOp):
            value = self._visit(node.operand, env)
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return +value
            if isinstance(node.op, ast.Not):
                return not value
            raise RuleEvaluationError(f"不支持的一元运算: {type(node.op).__name__}")
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result: Any = True
                for item in node.values:
                    result = self._visit(item, env)
                    if not result:
                        return result
                return result
            result = False
            for item in node.values:
                result = self._visit(item, env)
                if result:
                    return result
            return result
        if isinstance(node, ast.Compare):
            left = self._visit(node.left, env)
            for op_node, comparator in zip(node.ops, node.comparators, strict=True):
                op = _COMPARE_OPS.get(type(op_node))
                if op is None:
                    raise RuleEvaluationError(
                        f"不支持的比较运算: {type(op_node).__name__}"
                    )
                right = self._visit(comparator, env)
                if not op(left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            branch = node.body if self._visit(node.test, env) else node.orelse
            return self._visit(branch, env)
        if isinstance(node, ast.Call):
            args = [self._visit(arg, env) for arg in node.args]
            return _SAFE_FUNCTIONS[node.func.id](*args)  # type: ignore[union-attr]
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self._visit(elt, env) for elt in node.elts]
        if isinstance(node, ast.Subscript):
            container = self._visit(node.value, env)
            key = self._visit(node.slice, env)
            return container[key]
        if isinstance(node, ast.Slice):
            lower = self._visit(node.lower, env) if node.lower else None
            upper = self._visit(node.upper, env) if node.upper else None
            return slice(lower, upper)
        if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
            return self._eval_comprehension(node, env)
        raise RuleEvaluationError(f"不支持的语法节点: {type(node).__name__}")

    def _eval_comprehension(self, node: ast.AST, env: dict[str, Any]) -> list[Any]:
        """展开推导式: ``sum(t * t for t in components)`` 之类。"""
        results: list[Any] = []

        def walk(generators: list[ast.comprehension], index: int, local: dict[str, Any]) -> None:
            if index >= len(generators):
                results.append(self._visit(node.elt, {**env, **local}))  # type: ignore[attr-defined]
                return
            generator = generators[index]
            for item in self._visit(generator.iter, {**env, **local}):
                inner = dict(local)
                inner.update(self._bind_target(generator.target, item))
                conditions = {**env, **inner}
                if all(self._visit(cond, conditions) for cond in generator.ifs):
                    walk(generators, index + 1, inner)

        walk(node.generators, 0, {})  # type: ignore[attr-defined]
        return results

    @staticmethod
    def _bind_target(target: ast.AST, value: Any) -> dict[str, Any]:
        """把推导式目标 (``t`` 或 ``a, b``) 绑定到具体值。"""
        names = [n.id for n in ast.walk(target) if isinstance(n, ast.Name)]
        if len(names) == 1:
            return {names[0]: value}
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (list, tuple)):
            return dict(zip(names, value, strict=False))
        raise RuleEvaluationError("推导式目标与取值形状不匹配")


# --------------------------------------------------------------------------- #
# SHACL 适配层 (约束型规则)
# --------------------------------------------------------------------------- #

_SH = "http://www.w3.org/ns/shacl#"
_PREFIX_RE = re.compile(r"@prefix\s+([A-Za-z_][\w\-]*)\s*:\s*<([^>]+)>")
_NOT_EXISTS_RE = re.compile(r"FILTER\s+NOT\s+EXISTS\s*\{", re.IGNORECASE)
_OPTIONAL_RE = re.compile(r"OPTIONAL\s*\{", re.IGNORECASE)
_PATTERN_RE_TEMPLATE = r"\?this\s+{ns}:(\w+)\s+([^\s.]+)"
_FALSE_LITERAL_RE = "{ns}:{prop}\\s+\"false\"\\^\\^xsd:boolean"
_KNOWN_PREFIXES = {"sh", "xsd", "rdf", "rdfs", "owl", "dcterms", "skos"}


def _camel_to_snake(name: str) -> str:
    """``percentGaugeRR`` → ``percent_gauge_rr`` (大写串按一个词处理)。"""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", spaced).lower()


def _match_fact_key(candidates: Sequence[str], facts: Mapping[str, Any]) -> str | None:
    """在 ``facts`` 的键里找 ``candidates`` 的对应键: 精确 → 前缀 → 后缀。

    命名不匹配时只允许唯一解 (如形状属性 ``protectionTrip`` 对事实键 ``trip``,
    或 ``currentRating`` 对 ``rating``); 找不到或有歧义都返回 None, 由上层判成
    "该参数未提供"。
    """
    keys = [c for c in candidates if c]
    for cand in keys:
        if cand in facts:
            return cand
    for cand in keys:
        hits = [k for k in facts if cand.startswith(k + "_")]
        if len(hits) == 1:
            return hits[0]
    for cand in keys:
        hits = [k for k in facts if cand.endswith("_" + k)]
        if len(hits) == 1:
            return hits[0]
    return None


def _extract_prefix(shape_text: str) -> tuple[str, str]:
    """从 Turtle 前缀声明里取域命名空间 (跳过 sh/xsd 等通用前缀)。"""
    for name, uri in _PREFIX_RE.findall(shape_text):
        if name not in _KNOWN_PREFIXES:
            return name, uri
    raise RuleEvaluationError("SHACL 形状里找不到域命名空间前缀声明")


def _not_exists_spans(text: str) -> list[tuple[int, int]]:
    """定位所有 ``FILTER NOT EXISTS { ... }`` 片段 (含花括号, 按括号配平)。"""
    return _block_spans(text, _NOT_EXISTS_RE)


def _optional_spans(text: str) -> list[tuple[int, int]]:
    """定位所有 ``OPTIONAL { ... }`` 片段 (含花括号, 按括号配平)。"""
    return _block_spans(text, _OPTIONAL_RE)


def _block_spans(text: str, opener: re.Pattern[str]) -> list[tuple[int, int]]:
    """按括号配平找出所有 ``opener ... { ... }`` 片段的 [start, end) 区间。"""
    spans: list[tuple[int, int]] = []
    for match in opener.finditer(text):
        depth = 0
        for pos in range(match.end() - 1, len(text)):
            char = text[pos]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    spans.append((match.start(), pos + 1))
                    break
    return spans


def _inside(position: int, spans: Sequence[tuple[int, int]]) -> bool:
    """判断位置是否落在某个片段内。"""
    return any(start <= position < end for start, end in spans)


def _mask(text: str, spans: Sequence[tuple[int, int]]) -> str:
    """把 NOT EXISTS 片段替换为等长占位符 (长度不变, 便于回填)。"""
    out: list[str] = []
    last = 0
    for start, end in spans:
        out.append(text[last:start])
        out.append("\x00" * (end - start))
        last = end
    out.append(text[last:])
    return "".join(out)


def _unmask(text: str, spans: Sequence[tuple[int, int]], original: str) -> str:
    """把占位符还原为原始片段。"""
    out: list[str] = []
    last = 0
    for start, end in spans:
        out.append(text[last:start])
        out.append(original[start:end])
        last = end
    out.append(text[last:])
    return "".join(out)


def _filter_expressions(text: str) -> list[str]:
    """取出所有 ``FILTER ( ... )`` 的表达式文本 (按括号配平)。"""
    expressions: list[str] = []
    for match in re.finditer(r"FILTER\s*\(", text, re.IGNORECASE):
        depth = 0
        for pos in range(match.end() - 1, len(text)):
            char = text[pos]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    expressions.append(text[match.end() : pos])
                    break
    return expressions


def _add_property(
    rule: DomainRule,
    property_values: list[tuple[str, Any]],
    prop: str,
    value: Any,
) -> None:
    """登记 (属性, 取值); 同一属性被绑定到不同取值时报错 (fail-fast)。"""
    for existing_prop, existing_value in property_values:
        if existing_prop == prop and existing_value != value:
            raise RuleEvaluationError(
                f"规则 {rule.rule_id}: 属性 {prop} 同时绑到 {existing_value!r} 与 {value!r}"
            )
    if all(existing_prop != prop for existing_prop, _ in property_values):
        property_values.append((prop, value))


@dataclass(frozen=True)
class ShapePlan:
    """一条约束规则在给定事实下的 SHACL 求值计划 (纯数据, 无规则 id 分支)。"""

    shapes_turtle: str
    namespace: str
    prefix: str
    target_class: str
    select: str
    property_values: tuple[tuple[str, Any], ...]  # (属性名, 取值)
    property_paths: tuple[tuple[str, Any], ...]  # (路径 IRI, 取值)
    optional_vars: tuple[str, ...]


def plan_shape(rule: DomainRule, facts: Mapping[str, Any]) -> ShapePlan:
    """按事实规划 SHACL 求值: 属性→事实键映射 + 未提供参数的 OPTIONAL 化。"""
    shape_text = str(rule.constraint["shape"]) if rule.constraint else ""
    if not shape_text:
        raise RuleEvaluationError(f"规则 {rule.rule_id} 没有 constraint.shape")

    # rdflib 只在 SHACL 路径上导入: 纯公式规则的调用方不必背这个依赖。
    try:
        from rdflib import Graph, Literal, Namespace
    except ImportError as exc:  # pragma: no cover - 依赖缺失时
        raise RuleEvaluationError(
            "约束型规则求值需要 rdflib (semantica[shacl] 已声明)"
        ) from exc

    prefix, namespace = _extract_prefix(shape_text)
    sh = Namespace(_SH)
    ns = Namespace(namespace)
    shapes = Graph()
    shapes.parse(data=shape_text, format="turtle")
    shapes.bind("sh", sh)
    shapes.bind(prefix, ns)

    target = next(shapes.objects(None, sh.targetClass), None)
    if target is None:
        raise RuleEvaluationError(f"规则 {rule.rule_id} 的形状缺少 sh:targetClass")
    select = None
    for _subj, node in shapes.subject_objects(sh.sparql):
        value = shapes.value(node, sh.select)
        if value is not None:
            select = str(value)
    query = (select or "").replace("$this", "?this")
    pattern_re = re.compile(_PATTERN_RE_TEMPLATE.format(ns=re.escape(prefix)))

    spans = _not_exists_spans(query)
    optional_spans = _optional_spans(query)
    editable = _mask(query, spans)
    filters = _filter_expressions(editable)

    property_values: list[tuple[str, Any]] = []
    optional: list[str] = []
    rewritten = editable
    for match in pattern_re.finditer(editable):
        prop, obj = match.group(1), match.group(2)
        is_var = obj.startswith("?")
        var = obj[1:] if is_var else ""
        key = None
        if is_var and var in facts:
            key = var
        else:
            key = _match_fact_key([_camel_to_snake(prop), var], facts)
        if key is None:
            if not is_var or _inside(match.start(), optional_spans):
                # 常量对象模式 (如 ps:protectionMode 'hiccup') 没有证据就不判违反;
                # 形状自己声明为 OPTIONAL 的模式本来就可缺省, 都不改写。
                continue
            optional.append(var)
            pattern = f"?this {prefix}:{prop} {obj}"
            rewritten = rewritten.replace(pattern, f"OPTIONAL {{ {pattern} }}")
            continue
        value = facts[key]
        if isinstance(value, bool) and value is False:
            # 布尔"能力缺失": 仅当形状显式匹配 "false"^^xsd:boolean 时才落三元组,
            # 否则视为该能力不存在 (NOT EXISTS 类约束依赖这一点)。
            if re.search(
                _FALSE_LITERAL_RE.format(ns=re.escape(prefix), prop=re.escape(prop)), query
            ):
                _add_property(rule, property_values, prop, value)
            continue
        _add_property(rule, property_values, prop, value)

    for var in optional:
        token = f"?{var}"
        if any(token in expression for expression in filters):
            raise RuleEvaluationError(
                f"规则 {rule.rule_id}: 变量 {token} 未提供事实且出现在 FILTER 中, 无法求值"
            )

    final_select = _unmask(rewritten, spans, query)
    if select is not None and final_select != select:
        for _subj, node in shapes.subject_objects(sh.sparql):
            shapes.remove((node, sh.select, None))
            shapes.add((node, sh.select, Literal(final_select)))

    path_values: list[tuple[str, Any]] = []
    for _subj, node in shapes.subject_objects(sh.property):
        path = shapes.value(node, sh.path)
        if path is None:
            continue
        local = str(path).split("#")[-1]
        key = _match_fact_key([_camel_to_snake(local)], facts)
        if key is None:
            continue
        value = facts[key]
        if isinstance(value, bool) and value is False:
            continue
        path_values.append((str(path), value))

    return ShapePlan(
        shapes_turtle=shapes.serialize(format="turtle"),
        namespace=namespace,
        prefix=prefix,
        target_class=str(target),
        select=final_select,
        property_values=tuple(property_values),
        property_paths=tuple(path_values),
        optional_vars=tuple(optional),
    )


def evaluate_constraint(rule: DomainRule, facts: Mapping[str, Any]) -> bool:
    """求值约束型规则: 返回是否违反 (True = 违反)。"""
    plan = plan_shape(rule, facts)
    try:
        from rdflib import Graph, Literal, Namespace, URIRef
        from rdflib.namespace import RDF
        from semantica.ontology.ontology_validator import run_shacl_validation
    except ImportError as exc:  # pragma: no cover - 依赖缺失时
        raise RuleEvaluationError(
            "约束型规则求值需要 semantica[shacl] (rdflib/pyshacl)"
        ) from exc

    namespace = Namespace(plan.namespace)
    data = Graph()
    data.bind(plan.prefix, namespace)
    subject = URIRef("urn:dut")
    data.add((subject, RDF.type, URIRef(plan.target_class)))
    for prop, value in plan.property_values:
        data.add((subject, namespace[prop], Literal(value)))
    for path, value in plan.property_paths:
        data.add((subject, URIRef(path), Literal(value)))

    report = run_shacl_validation(
        data.serialize(format="turtle"), plan.shapes_turtle
    )
    return not report.conforms


# --------------------------------------------------------------------------- #
# Datalog 事实编解码
# --------------------------------------------------------------------------- #


def encode_value(value: Any) -> str:
    """把结论取值编码成 Datalog 常量 (小写开头, 无逗号/括号)。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(float(value))
    if isinstance(value, str):
        if re.fullmatch(r"[a-z0-9_][a-z0-9_.\-]*", value):
            return value
        raise RuleEvaluationError(f"字符串结论无法编码为 Datalog 常量: {value!r}")
    raise RuleEvaluationError(f"结论取值类型不支持: {type(value).__name__}")


def decode_value(text: str) -> Any:
    """把 Datalog 常量解码回 Python 值。

    约定: ``true``/``false`` 为布尔, 可解析为浮点的一律按数字解, 其余按字符串。
    """
    if text == "true":
        return True
    if text == "false":
        return False
    try:
        return float(text)
    except ValueError:
        return text


def _assert_inputs(engine: DatalogReasoner, params: Sequence[str], facts: Mapping[str, Any]) -> list[str]:
    """把事实库里存在的输入参数断言为 ``input(p)`` 事实, 返回缺失的参数。"""
    missing: list[str] = []
    for param in params:
        if param in facts:
            engine.add_fact(f"input({param})")
        else:
            missing.append(param)
    return missing


# --------------------------------------------------------------------------- #
# 引擎装配与规则求值
# --------------------------------------------------------------------------- #


def build_reasoner(rules: Sequence[DomainRule] | None = None) -> DatalogReasoner:
    """装配全部规则, 返回 semantica ``DatalogReasoner`` 引擎对象。

    每一条规则转换成一条 Horn 子句 (见 :mod:`matrag.domain.rules_loader`), 由
    引擎自己的 ``add_rule(str)`` 解析——因此子句文本必须符合 semantica 方言,
    转换器写错会在这里当场失败。
    """
    selected = list(load_rules()) if rules is None else list(rules)
    engine = DatalogReasoner()
    for clause in render_datalog(selected):
        engine.add_rule(clause)
    return engine


def _compare(actual: Any, expected: Any) -> bool:
    """比对实际值与期望值 (浮点用相对容差, 布尔严格相等)。"""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return math.isclose(float(actual), float(expected), rel_tol=1e-9, abs_tol=1e-12)
    return actual == expected


def _describe(rule: DomainRule, result: Mapping[str, Any]) -> str:
    """生成人类可读的失败说明 (verify_all_rules 的 detail)。"""
    parts: list[str] = []
    if result.get("error"):
        parts.append(str(result["error"]))
    if result.get("applicable") is False:
        missing = result.get("missing_inputs") or []
        parts.append(f"规则前提不满足, 缺失输入: {missing}")
    elif result.get("matched") is False:
        parts.append(
            f"expected[{result.get('key')}]={result.get('expected')!r} "
            f"got {result.get('actual')!r}"
        )
    if not parts:
        parts.append("未知失败")
    return "; ".join(parts)


def evaluate_rule(
    rule: DomainRule,
    facts: Mapping[str, Any],
    reasoner: DatalogReasoner | None = None,
) -> dict[str, Any]:
    """对单条规则按给定事实求值, 并与该规则自带的期望结果比对。

    Args:
        rule: 域规则。
        facts: 输入事实 (公式型规则取 ``derive.inputs`` 的键, 约束型规则取
            ``test.given`` 的键)。
        reasoner: 可复用的事先装配好的 semantica 引擎; 缺省时为该规则单独装配
            (事实互相隔离, 每条规则一个干净的事实库)。

    Returns:
        dict: ``rule_id/kind/key/actual/expected/matched/applicable/ok/detail``。

        - ``actual``: 引擎回查到的结论取值 (算术结果或布尔判定);
        - ``expected``/``matched``: 仅当 ``facts`` 覆盖该规则 ``test.given`` 的全部键
          时才做比对 (``matched=None`` 表示"未比对", 不是失败);
        - ``applicable``: 引擎是否激活了该规则 (事实库里输入参数齐备);
        - ``ok``: 求值成功且 (有比对时) 结果一致; 为 False 时 ``detail`` 说明原因,
          失败不静默吞掉。

    Raises:
        不主动抛出业务异常——失败以 ``ok=False`` + ``detail`` 呈现, 由
        :func:`verify_all_rules` 汇总成失败清单。
    """
    engine = reasoner if reasoner is not None else build_reasoner([rule])
    missing = _assert_inputs(engine, rule.input_params, facts)

    result: dict[str, Any] = {
        "rule_id": rule.rule_id,
        "kind": rule.kind,
        "key": rule.output_key,
        "actual": None,
        "expected": None,
        "matched": None,
        "applicable": False,
        "missing_inputs": missing,
        "error": None,
        "ok": False,
        "detail": "",
    }

    try:
        if rule.is_derive:
            derive = rule.derive or {}
            absent = [name for name in derive["inputs"] if name not in facts]
            if absent:
                raise RuleEvaluationError(f"公式规则缺少输入事实: {absent}")
            env = {name: facts[name] for name in derive["inputs"]}
            value: Any = SafeExpression(str(derive["expr"])).evaluate(env)
        else:
            value = evaluate_constraint(rule, facts)
    except Exception as exc:  # noqa: BLE001 - 失败信息进 detail, 由 verify 汇总, 不静默
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["detail"] = _describe(rule, result)
        return result

    engine.add_fact(f"evaluated({rule_slug(rule.rule_id)}, {rule.output_key}, {encode_value(value)})")
    rows = engine.query(f"holds({rule_slug(rule.rule_id)}, ?k, ?v)")

    if rows:
        result["applicable"] = True
        result["actual"] = decode_value(str(rows[0]["v"]))
    else:
        result["detail"] = _describe(rule, result)
        return result

    if set(rule.given) <= set(facts):
        expected = rule.expected_value
        result["expected"] = expected
        result["matched"] = _compare(result["actual"], expected)
    result["ok"] = result["matched"] is not False
    if not result["ok"]:
        result["detail"] = _describe(rule, result)
    return result


def verify_rule(
    rule: DomainRule, reasoner: DatalogReasoner | None = None
) -> dict[str, Any]:
    """用规则自带的 ``test.given`` → ``test.expect`` 校验单条规则。"""
    return evaluate_rule(rule, rule.given, reasoner=reasoner)


def verify_all_rules(
    rules: Sequence[DomainRule] | None = None,
    reasoner: DatalogReasoner | None = None,
) -> tuple[int, list[tuple[str, str]]]:
    """逐条用 ``test.given`` → ``test.expect`` 校验全部规则。

    Returns:
        ``(passed, failed)``: ``passed`` 是通过条数, ``failed`` 是
        ``[(rule_id, detail), ...]``; 全部通过时 ``failed`` 为空列表。
    """
    selected = list(load_rules()) if rules is None else list(rules)
    failed: list[tuple[str, str]] = []
    for rule in selected:
        result = verify_rule(rule, reasoner=reasoner)
        if not result["ok"]:
            failed.append((rule.rule_id, result["detail"]))
    return len(selected) - len(failed), failed


def generate_artifacts(
    out_dir: str | None = None,
    rules: Sequence[DomainRule] | None = None,
) -> dict[str, Any]:
    """生成本地规则产物 (rules.yaml 副本 + Datalog 子句集)。"""
    from .rules_loader import write_generated

    return write_generated(out_dir=out_dir, rules=rules)


__all__ = [
    "DomainRule",
    "RuleEvaluationError",
    "SafeExpression",
    "ShapePlan",
    "build_reasoner",
    "decode_value",
    "encode_value",
    "evaluate_constraint",
    "evaluate_rule",
    "generate_artifacts",
    "plan_shape",
    "verify_all_rules",
    "verify_rule",
]
