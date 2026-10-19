"""规则集加载与 Datalog 转换（power_rules.yaml → semantica 推理引擎）。

本模块只负责"把规则读出来 + 变成推理引擎能装的语言", 不做求值
(求值在 :mod:`matrag.domain.rules`)。

Datalog 子句约定 (全部由 rules.yaml 数据驱动生成, 没有任何按 rule_id 的分支)::

    holds(<rule_slug>, <output_key>, V) :-
        input(<param_1>), ..., input(<param_n>),   # 规则的适用性前提
        evaluated(<rule_slug>, <output_key>, V).   # 适配层给出的结论值

- ``input(p)``: 事实库中存在该规则的输入参数 (公式型规则取 ``derive.inputs``,
  约束型规则取 ``test.given`` 的键——规则文件没有为约束声明输入签名,
  ``test.given`` 是唯一显式的参数清单);
- ``evaluated(rid, key, V)``: 由 :mod:`matrag.domain.rules` 的适配层求值后写入
  事实库 (公式型规则的值是算术结果, 约束型规则的值是布尔判定);
- ``holds(...)`` 由 semantica ``DatalogReasoner`` 做连接/前推得出, 表示
  "该规则的前提在当前事实库中成立且结论可用"。

semantica 的 Datalog 方言: 变量以大写字母开头, 常量小写开头; 事实形如
``predicate(arg1, arg2)``。因此规则 id 统一 slug 化为小写。
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from semantica.reasoning.datalog_reasoner import BodyAtom, DatalogRule

from .cards import (
    RULES_SOURCE_LABEL,
    load_domain_rules_raw,
    resolve_rules_path,
)

VIOLATION_KEY = "violation"
DEFAULT_GENERATED_DIR = Path(__file__).resolve().parents[3] / "data" / "domain" / "generated"


@dataclass(frozen=True)
class DomainRule:
    """一条域规则 (130 条之一)。

    - ``derive``: 公式型规则的推导定义 (``expr``/``inputs``/``output``/``variables``);
    - ``constraint``: 约束型规则的 SHACL 形状 (``shape`` 字段是 Turtle 文本);
    - ``test``: 规则自带的校验用例 (``given`` → ``expect``);
    - ``output_key``: 输出名 (公式型取 ``derive.output``, 约束型统一为 ``violation``)。
    """

    rule_id: str
    statement: str
    category: str
    scope: str
    derive: dict[str, Any] | None = None
    constraint: dict[str, Any] | None = None
    test: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    variables: dict[str, str] = field(default_factory=dict)
    note: str = ""

    @property
    def is_derive(self) -> bool:
        """是否公式型规则。"""
        return self.derive is not None

    @property
    def is_constraint(self) -> bool:
        """是否约束型规则。"""
        return self.constraint is not None

    @property
    def kind(self) -> str:
        """规则类型标签: ``derive`` / ``constraint``。"""
        return "derive" if self.is_derive else "constraint"

    @property
    def given(self) -> dict[str, Any]:
        """自测用例的输入事实。"""
        return dict(self.test.get("given", {}))

    @property
    def expected(self) -> dict[str, Any]:
        """自测用例的期望结果。"""
        return dict(self.test.get("expect", {}))

    @property
    def output_key(self) -> str:
        """规则的输出名。"""
        if self.is_derive:
            return str(self.derive["output"])
        return VIOLATION_KEY

    @property
    def expected_value(self) -> Any | None:
        """自测用例中本规则输出名对应的期望值 (无则 None)。"""
        return self.expected.get(self.output_key)

    @property
    def input_params(self) -> list[str]:
        """规则的输入参数名 (适用性前提)。

        公式型规则取 ``derive.inputs`` (YAML 显式声明);
        约束型规则取 ``test.given`` 的键 (规则文件未声明输入签名,
        ``test.given`` 是唯一显式给出的事实清单)。
        """
        if self.is_derive:
            return [str(p) for p in self.derive.get("inputs", [])]
        return sorted(self.given)

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict。"""
        return {
            "rule_id": self.rule_id,
            "statement": self.statement,
            "category": self.category,
            "scope": self.scope,
            "kind": self.kind,
            "derive": self.derive,
            "constraint": self.constraint,
            "test": self.test,
            "source": self.source,
            "confidence": self.confidence,
            "variables": self.variables,
            "note": self.note,
        }


def rule_slug(rule_id: str) -> str:
    """把规则 id 变成 Datalog 常量 (小写开头, 仅含 ``[a-z0-9_]``)。"""
    slug = re.sub(r"[^a-z0-9]+", "_", rule_id.lower()).strip("_")
    if not slug:
        raise ValueError(f"规则 id 无法 slug 化: {rule_id!r}")
    return slug


def _build_rule(raw: dict[str, Any]) -> DomainRule:
    """把原始 dict 组装成 :class:`DomainRule`, 结构不合法即报错。"""
    rule_id = raw["id"]
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ValueError(f"规则 id 非法: {rule_id!r}")
    for key in ("statement", "test"):
        if key not in raw:
            raise ValueError(f"规则 {rule_id} 缺少字段 {key}")
    derive = raw.get("derive")
    constraint = raw.get("constraint")
    if derive is None and constraint is None:
        raise ValueError(f"规则 {rule_id} 既无 derive 也无 constraint")
    if derive is not None and constraint is not None:
        raise ValueError(f"规则 {rule_id} 同时有 derive 与 constraint")
    if derive is not None:
        for key in ("expr", "inputs", "output"):
            if key not in derive:
                raise ValueError(f"规则 {rule_id} 的 derive 缺少字段 {key}")
        if not isinstance(derive["inputs"], list):
            raise ValueError(f"规则 {rule_id} 的 derive.inputs 不是列表")
    if constraint is not None and "shape" not in constraint:
        raise ValueError(f"规则 {rule_id} 的 constraint 缺少字段 shape")
    if "given" not in raw["test"] or "expect" not in raw["test"]:
        raise ValueError(f"规则 {rule_id} 的 test 缺少 given/expect")
    return DomainRule(
        rule_id=rule_id,
        statement=str(raw["statement"]),
        category=str(raw.get("category", "")),
        scope=str(raw.get("scope", "")),
        derive=dict(derive) if derive is not None else None,
        constraint=dict(constraint) if constraint is not None else None,
        test={"given": dict(raw["test"]["given"]), "expect": dict(raw["test"]["expect"])},
        source=dict(raw.get("source", {})),
        confidence=float(raw.get("confidence", 1.0)),
        variables=dict(raw.get("variables", {})),
        note=str(raw.get("note", "")),
    )


def load_rules(path: Path | str | None = None) -> list[DomainRule]:
    """加载全部域规则。

    Args:
        path: rules.yaml 路径; 缺省按 :func:`matrag.domain.cards.resolve_rules_path`
            解析 (环境变量 ``MATRAG_DOMAIN_RULES_PATH`` > 仓内默认路径)。

    Returns:
        :class:`DomainRule` 列表 (当前 130 条, 顺序与文件一致)。

    Raises:
        FileNotFoundError: 规则文件不存在。
        ValueError: 规则结构不合法。
    """
    file = resolve_rules_path(path)
    raw_rules = load_domain_rules_raw(file)
    rules = [_build_rule(item) for item in raw_rules]
    slugs = [rule_slug(r.rule_id) for r in rules]
    if len(set(slugs)) != len(slugs):
        raise ValueError("规则 id slug 化后出现重复")
    return rules


def rule_clause(rule: DomainRule) -> str:
    """生成单条规则的 Horn 子句文本 (可直接喂给 ``DatalogReasoner.add_rule``)。"""
    return render_datalog([rule])[0]


def render_clause(clause: DatalogRule) -> str:
    """把结构化 ``DatalogRule`` 渲染成 Horn 子句文本。

    ``holds(id, key, V) :- input(p1), ..., evaluated(id, key, V).``
    (变量 ``V`` 大写开头, 常量小写开头, 符合 semantica 的 Datalog 方言)。
    """
    head = f"{clause.head_predicate}({', '.join(clause.head_args)})"
    body = ", ".join(f"{atom.predicate}({', '.join(atom.args)})" for atom in clause.body)
    return f"{head} :- {body}."


def to_datalog(rules: Sequence[DomainRule]) -> list[DatalogRule]:
    """把规则列表转换为 semantica 的 ``DatalogRule`` (结构化 Horn 子句)。

    转换是纯数据驱动的: 规则 id → slug, 输入参数与输出名取自规则自身
    (公式型取 ``derive.inputs``/``derive.output``, 约束型取 ``test.given`` 的键);
    没有任何按 rule_id 的分支。
    """
    clauses: list[DatalogRule] = []
    for rule in rules:
        rid = rule_slug(rule.rule_id)
        key = rule.output_key
        clauses.append(
            DatalogRule(
                head_predicate="holds",
                head_args=(rid, key, "V"),
                body=[BodyAtom("input", (p,)) for p in rule.input_params]
                + [BodyAtom("evaluated", (rid, key, "V"))],
            )
        )
    return clauses


def render_datalog(rules: Sequence[DomainRule]) -> list[str]:
    """渲染 Horn 子句文本集 (``.dl`` 产物, 也用于 ``DatalogReasoner.add_rule``)。"""
    return [render_clause(clause) for clause in to_datalog(rules)]


def write_generated(
    out_dir: Path | str | None = None,
    rules: Sequence[DomainRule] | None = None,
    source: Path | str | None = None,
) -> dict[str, Any]:
    """生成本地产物: 规则集副本 + Datalog Horn 子句集。

    Args:
        out_dir: 输出目录 (默认 ``data/domain/generated``)。
        rules: 规则列表 (默认重新加载)。
        source: 被复制的 rules.yaml 路径 (默认按解析规则解析)。

    Returns:
        统计 dict (产物路径与数量), 供 CLI 打印。
    """
    if rules is None:
        rules = load_rules()
    rules = list(rules)
    target_dir = Path(out_dir) if out_dir is not None else DEFAULT_GENERATED_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    src = resolve_rules_path(source)
    if not src.is_file():
        raise FileNotFoundError(f"规则源文件不存在: {src}")
    rules_copy = target_dir / "power_rules.yaml"
    shutil.copyfile(src, rules_copy)

    dl_path = target_dir / "power_rules_datalog.dl"
    clauses = render_datalog(rules)
    header = [
        "# power 域规则 → Datalog Horn 子句 (由 scripts/import_domain.py 生成, 请勿手改)",
        f"# 源: {RULES_SOURCE_LABEL}; 规则数: {len(rules)}",
        "# 方言: semantica.reasoning.DatalogReasoner (变量大写开头, 常量小写开头)",
        "# 事实层: input(<param>) / evaluated(<rule_slug>, <output_key>, <value>)",
        "# 结论层: holds(<rule_slug>, <output_key>, <value>)",
        "",
    ]
    dl_path.write_text("\n".join(header + clauses) + "\n", encoding="utf-8")

    return {
        "rules_copy": str(rules_copy),
        "datalog": str(dl_path),
        "rule_count": len(rules),
        "derive_count": sum(1 for r in rules if r.is_derive),
        "constraint_count": sum(1 for r in rules if r.is_constraint),
        "clause_count": len(clauses),
        "source": str(src),
    }


def load_rules_source_label() -> str:
    """规则来源标签 (产物/报告用)。"""
    return RULES_SOURCE_LABEL


def dump_rules_yaml(rules: Sequence[DomainRule]) -> str:
    """把规则列表序列化回 YAML (调试/校验用, 与源文件字段一致)。"""
    return yaml.safe_dump(
        {"rules": [rule.to_dict() for rule in rules]},
        allow_unicode=True,
        sort_keys=False,
    )
