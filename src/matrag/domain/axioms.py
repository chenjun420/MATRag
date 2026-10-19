"""电力域公理/定理/公式表 (axiom / theorem / formula) 的加载与质量门。

定位: 本模块是**通用知识层**的公式侧, 与 :mod:`matrag.domain.terms`
(术语侧) 平行。它把「基本定律 / 导出关系 / 工程公式」变成可加载、可过滤、
可校验的数据结构, 供知识图谱组装层把公式挂到本体属性与域规则上。

设计要点:

1. **三档 kind**: ``axiom`` (基本定律) / ``theorem`` (导出关系) /
   ``formula`` (工程公式), 与源文件约定一致, 非法值由质量门报出;
2. **类别闭集复用**: ``category`` 一律取自
   :data:`matrag.domain.vocab.CATEGORIES`;
3. **公式不执行**: 本模块只保存 ``expr`` 文本与变量表, **不做符号求值**
   (求值属于 :mod:`matrag.domain.rules` 的职责), 避免把两套语义焊死;
4. **fail-fast**: 文件缺失 ``FileNotFoundError``, 结构非法 ``ValueError``,
   重复 ``axiom_id`` 一律拒绝, 绝不静默丢条目。

公理源文件: ``data/domain/source/power_axioms.yaml``。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from matrag.domain.vocab import CATEGORIES

__all__ = [
    "AXIOM_KINDS",
    "DEFAULT_AXIOMS_PATH",
    "ISSUE_CATEGORY_UNKNOWN",
    "ISSUE_EXPR_EMPTY",
    "ISSUE_KIND_UNKNOWN",
    "ISSUE_SOURCE_MISSING",
    "ISSUE_VARIABLES_EMPTY",
    "Axiom",
    "AxiomIssue",
    "axiom_stats",
    "axioms_for_category",
    "load_axioms",
    "validate_axioms",
]

#: 公理源文件默认路径 (``data/domain/source/power_axioms.yaml``)。
DEFAULT_AXIOMS_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "domain"
    / "source"
    / "power_axioms.yaml"
)

#: 允许的 ``kind`` 取值: 基本定律 / 导出关系 / 工程公式。
AXIOM_KINDS: tuple[str, ...] = ("axiom", "theorem", "formula")

#: 质量门 issue 类型。
ISSUE_KIND_UNKNOWN = "unknown_kind"
ISSUE_EXPR_EMPTY = "expr_empty"
ISSUE_VARIABLES_EMPTY = "variables_empty"
ISSUE_CATEGORY_UNKNOWN = "unknown_category"
ISSUE_SOURCE_MISSING = "source_missing"


# --------------------------------------------------------------------------- #
# 值对象
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Axiom:
    """一条公理/定理/工程公式。

    Attributes:
        axiom_id: 稳定 id (``A-XXX``, 全局唯一)。
        kind: :data:`AXIOM_KINDS` 之一 (``axiom`` / ``theorem`` / ``formula``)。
        statement: 中文陈述 (公式的文字表述, 含适用条件)。
        expr: 公式文本 (如 ``"V = I * R"``); 本模块只保存不执行。
        variables: 变量名 → 含义+单位 的中文说明。
        category: 类别, 必须 ∈ :data:`matrag.domain.vocab.CATEGORIES`。
        aliases: 其他常见叫法。
        properties: 关联的本体属性名 (不确定时为空元组)。
        related_rules: 关联的域规则 id (不确定时为空元组)。
        source: 出处 dict (含 ``kind`` / ``name`` 等, 原样保留)。
    """

    axiom_id: str
    kind: str
    statement: str
    expr: str
    variables: dict[str, str] = field(default_factory=dict)
    category: str = ""
    aliases: tuple[str, ...] = ()
    properties: tuple[str, ...] = ()
    related_rules: tuple[str, ...] = ()
    source: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict (字段顺序与源文件一致)。"""
        return {
            "axiom_id": self.axiom_id,
            "kind": self.kind,
            "statement": self.statement,
            "expr": self.expr,
            "variables": dict(self.variables),
            "category": self.category,
            "aliases": list(self.aliases),
            "properties": list(self.properties),
            "related_rules": list(self.related_rules),
            "source": dict(self.source),
        }


@dataclass(frozen=True)
class AxiomIssue:
    """公理质量门的一条问题 (类型见模块级 ``ISSUE_*`` 常量)。"""

    kind: str
    detail: str
    involved: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict。"""
        return {
            "kind": self.kind,
            "detail": self.detail,
            "involved": list(self.involved),
        }


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #


def _as_str_tuple(raw: Any, *, where: str) -> tuple[str, ...]:
    """把 YAML 里的列表字段转成去空的字符串元组; 结构非法即 ValueError。"""
    if raw is None:
        return ()
    if isinstance(raw, str):
        raise ValueError(f"{where} 应为列表, 实际是字符串: {raw!r}")
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{where} 应为列表, 实际是 {type(raw).__name__}: {raw!r}")
    out: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _as_variables(raw: Any, *, where: str) -> dict[str, str]:
    """把 ``variables`` 字段转成 ``{变量名: 说明}``; 结构非法即 ValueError。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 应为映射, 实际是 {type(raw).__name__}: {raw!r}")
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if name:
            out[name] = str(value).strip()
    return out


def _build_axiom(raw: dict[str, Any], *, index: int) -> Axiom:
    """把原始 dict 组装成 :class:`Axiom`; 结构不合法即 ValueError。"""
    where = f"公理列表第 {index} 项"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 不是映射: {raw!r}")
    axiom_id = raw.get("axiom_id")
    if not isinstance(axiom_id, str) or not axiom_id.strip():
        raise ValueError(f"{where} 缺少合法的 axiom_id: {axiom_id!r}")
    axiom_id = axiom_id.strip()
    for key in ("kind", "statement", "expr", "category"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"公理 {axiom_id} 缺少合法字段 {key}: {value!r}")
    source = raw.get("source")
    if source is None:
        source_dict: dict[str, Any] = {}
    elif isinstance(source, dict):
        source_dict = dict(source)
    else:
        raise ValueError(
            f"公理 {axiom_id} 的 source 应为映射, 实际是 "
            f"{type(source).__name__}: {source!r}"
        )
    return Axiom(
        axiom_id=axiom_id,
        kind=str(raw["kind"]).strip(),
        statement=str(raw["statement"]).strip(),
        expr=str(raw["expr"]).strip(),
        variables=_as_variables(
            raw.get("variables"), where=f"公理 {axiom_id} 的 variables"
        ),
        category=str(raw["category"]).strip(),
        aliases=_as_str_tuple(
            raw.get("aliases"), where=f"公理 {axiom_id} 的 aliases"
        ),
        properties=_as_str_tuple(
            raw.get("properties"), where=f"公理 {axiom_id} 的 properties"
        ),
        related_rules=_as_str_tuple(
            raw.get("related_rules"), where=f"公理 {axiom_id} 的 related_rules"
        ),
        source=source_dict,
    )


def load_axioms(path: Path | str | None = None) -> list[Axiom]:
    """加载公理/定理/公式表。

    Args:
        path: YAML 路径; ``None`` 表示 :data:`DEFAULT_AXIOMS_PATH`。

    Returns:
        :class:`Axiom` 列表, **顺序与文件一致** (确定性, 测试可固化)。

    Raises:
        FileNotFoundError: 文件不存在 (fail-fast, 不返回空列表)。
        ValueError: 顶层不是 ``{axioms: [...]}``、条目结构非法或 ``axiom_id`` 重复。
    """
    file = Path(path) if path is not None else DEFAULT_AXIOMS_PATH
    if not file.is_file():
        raise FileNotFoundError(f"公理表文件不存在: {file}")
    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "axioms" not in data:
        raise ValueError(f"公理表缺少顶层 axioms 键: {file}")
    raw_axioms = data["axioms"]
    if not isinstance(raw_axioms, list) or not raw_axioms:
        raise ValueError(f"公理表 axioms 为空或不是列表: {file}")

    axioms = [
        _build_axiom(item, index=i) for i, item in enumerate(raw_axioms, start=1)
    ]
    seen: dict[str, int] = {}
    for position, axiom in enumerate(axioms, start=1):
        if axiom.axiom_id in seen:
            raise ValueError(
                f"公理 id 重复: {axiom.axiom_id} "
                f"(第 {seen[axiom.axiom_id]} 项与第 {position} 项)"
            )
        seen[axiom.axiom_id] = position
    return axioms


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #


def axioms_for_category(
    category: str, *, axioms: Sequence[Axiom] | None = None
) -> list[Axiom]:
    """取某类别下的全部公理 (保持文件顺序); 未知类别返回空列表。

    未知类别返回空列表是"查询无结果"的正常语义 (与加载失败不同,
    加载失败会抛 ``FileNotFoundError``/``ValueError``)。
    """
    items = list(axioms) if axioms is not None else load_axioms()
    return [axiom for axiom in items if axiom.category == category]


# --------------------------------------------------------------------------- #
# 质量门
# --------------------------------------------------------------------------- #


def validate_axioms(axioms: Sequence[Axiom] | None = None) -> list[AxiomIssue]:
    """公理表质量门: **空列表 = 通过**。

    检出的类型 (常量见模块顶部):

    - :data:`ISSUE_KIND_UNKNOWN` —— ``kind`` 不在 :data:`AXIOM_KINDS` 中;
    - :data:`ISSUE_EXPR_EMPTY` —— ``expr`` 为空 (只有文字没有公式);
    - :data:`ISSUE_VARIABLES_EMPTY` —— ``variables`` 为空 (公式变量无说明);
    - :data:`ISSUE_CATEGORY_UNKNOWN` —— ``category`` 不在
      :data:`matrag.domain.vocab.CATEGORIES` 中;
    - :data:`ISSUE_SOURCE_MISSING` —— 缺 ``source`` 或缺 ``source.name``。

    Returns:
        issue 列表, 按 ``(kind, detail, involved)`` 排序保证确定性。
    """
    items = list(axioms) if axioms is not None else load_axioms()
    issues: list[AxiomIssue] = []

    for axiom in items:
        if axiom.kind not in AXIOM_KINDS:
            issues.append(
                AxiomIssue(
                    kind=ISSUE_KIND_UNKNOWN,
                    detail=(
                        f"公理 {axiom.axiom_id} 的 kind={axiom.kind!r} "
                        f"不在 {list(AXIOM_KINDS)} 内"
                    ),
                    involved=(axiom.axiom_id,),
                )
            )
        if not axiom.expr.strip():
            issues.append(
                AxiomIssue(
                    kind=ISSUE_EXPR_EMPTY,
                    detail=f"公理 {axiom.axiom_id} 的 expr 为空",
                    involved=(axiom.axiom_id,),
                )
            )
        if not axiom.variables:
            issues.append(
                AxiomIssue(
                    kind=ISSUE_VARIABLES_EMPTY,
                    detail=f"公理 {axiom.axiom_id} 的 variables 为空 (公式变量无说明)",
                    involved=(axiom.axiom_id,),
                )
            )
        if axiom.category not in CATEGORIES:
            issues.append(
                AxiomIssue(
                    kind=ISSUE_CATEGORY_UNKNOWN,
                    detail=(
                        f"公理 {axiom.axiom_id} 的 category={axiom.category!r} "
                        f"不在 vocab.CATEGORIES 闭集内"
                    ),
                    involved=(axiom.axiom_id,),
                )
            )
        source = axiom.source
        if not isinstance(source, dict) or not str(source.get("name", "")).strip():
            issues.append(
                AxiomIssue(
                    kind=ISSUE_SOURCE_MISSING,
                    detail=(
                        f"公理 {axiom.axiom_id} 缺少 source 或 source.name: "
                        f"{source!r}"
                    ),
                    involved=(axiom.axiom_id,),
                )
            )

    issues.sort(key=lambda issue: (issue.kind, issue.detail, issue.involved))
    return issues


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #


def axiom_stats(axioms: Sequence[Axiom] | None = None) -> dict[str, Any]:
    """公理表统计摘要 (纯数据, 无时间戳, 可 JSON 序列化, 确定性)。

    Returns:
        ``{"count", "by_kind", "by_category", "with_properties", "with_rules",
        "sources_by_kind", "variables_count", "issue_count"}``:

        - ``by_kind`` / ``by_category``: 分布 (键已排序);
        - ``with_properties`` / ``with_rules``: 填了 ``properties`` /
          ``related_rules`` 的条数;
        - ``sources_by_kind``: ``source.kind`` → 条数 (缺失 kind 记为 ``""``);
        - ``variables_count``: 全部变量说明条目数;
        - ``issue_count``: :func:`validate_axioms` 的问题数 (自带质量门结果)。
    """
    items = list(axioms) if axioms is not None else load_axioms()
    by_kind: dict[str, int] = {}
    by_category: dict[str, int] = {}
    sources_by_kind: dict[str, int] = {}
    for axiom in items:
        by_kind[axiom.kind] = by_kind.get(axiom.kind, 0) + 1
        by_category[axiom.category] = by_category.get(axiom.category, 0) + 1
        kind = axiom.source.get("kind") if isinstance(axiom.source, dict) else None
        key = str(kind).strip() if kind is not None else ""
        sources_by_kind[key] = sources_by_kind.get(key, 0) + 1
    return {
        "count": len(items),
        "by_kind": {k: by_kind[k] for k in sorted(by_kind)},
        "by_category": {k: by_category[k] for k in sorted(by_category)},
        "with_properties": sum(1 for a in items if a.properties),
        "with_rules": sum(1 for a in items if a.related_rules),
        "sources_by_kind": {k: sources_by_kind[k] for k in sorted(sources_by_kind)},
        "variables_count": sum(len(a.variables) for a in items),
        "issue_count": len(validate_axioms(items)),
    }
