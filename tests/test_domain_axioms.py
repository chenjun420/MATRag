"""``matrag.domain.axioms`` 的离线测试。

覆盖三块:
1. 真实数据 (``data/domain/source/power_axioms.yaml``) 的加载与覆盖度断言;
2. YAML → Axiom → dict 的往返与 schema 完整性;
3. 加载失败路径、类别过滤与质量门在**小规模构造 fixture** 上的逐类行为。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from matrag.domain.axioms import (
    AXIOM_KINDS,
    DEFAULT_AXIOMS_PATH,
    ISSUE_CATEGORY_UNKNOWN,
    ISSUE_EXPR_EMPTY,
    ISSUE_KIND_UNKNOWN,
    ISSUE_SOURCE_MISSING,
    ISSUE_VARIABLES_EMPTY,
    Axiom,
    axiom_stats,
    axioms_for_category,
    load_axioms,
    validate_axioms,
)
from matrag.domain.vocab import CATEGORIES

#: 定义一条合法公理的模板 (fixture 基线)。
AXIOM_TEMPLATE: dict[str, Any] = {
    "axiom_id": "A-TEST",
    "kind": "formula",
    "statement": "测试用公式: 输出功率等于输出电压乘以输出电流。",
    "expr": "P = V * I",
    "variables": {"P": "功率(W)", "V": "电压(V)", "I": "电流(A)"},
    "category": "general",
    "aliases": ["测试公式"],
    "properties": ["power", "voltage", "current"],
    "related_rules": ["K-ELEC-001"],
    "source": {
        "kind": "card",
        "name": "power-knowledge-1 基本电学关系",
        "card_id": "power-knowledge-1",
        "url": "",
        "retrieved": "2026-10-11",
    },
}


def make_axiom(**overrides: Any) -> dict[str, Any]:
    """构造一条公理原始 dict (浅拷贝模板, 允许逐字段覆盖)。"""
    item = {key: value for key, value in AXIOM_TEMPLATE.items()}
    item.update(overrides)
    return item


def write_axioms(path: Path, items: list[dict[str, Any]]) -> Path:
    """把公理条目写成临时 YAML 文件。"""
    path.write_text(
        yaml.safe_dump({"axioms": items}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# 真实数据: 加载与覆盖度
# --------------------------------------------------------------------------- #


def test_default_path_points_into_repo_source_dir() -> None:
    assert DEFAULT_AXIOMS_PATH.name == "power_axioms.yaml"
    assert DEFAULT_AXIOMS_PATH.parent.name == "source"
    assert DEFAULT_AXIOMS_PATH.is_file()


def test_load_real_axioms_succeeds() -> None:
    axioms = load_axioms()
    assert len(axioms) >= 40
    assert all(isinstance(axiom, Axiom) for axiom in axioms)
    # 确定性: 同输入两次加载顺序与内容逐位一致。
    assert [a.to_dict() for a in load_axioms()] == [a.to_dict() for a in axioms]


def test_real_axioms_kinds_expr_and_ids_are_valid() -> None:
    axioms = load_axioms()
    ids = [axiom.axiom_id for axiom in axioms]
    assert len(set(ids)) == len(ids)
    for axiom in axioms:
        assert axiom.kind in AXIOM_KINDS, axiom.axiom_id
        assert axiom.expr.strip(), axiom.axiom_id
        assert axiom.statement.strip(), axiom.axiom_id


def test_real_axioms_categories_and_source_are_valid() -> None:
    for axiom in load_axioms():
        assert axiom.category in CATEGORIES, axiom.axiom_id
        assert isinstance(axiom.source, dict)
        assert axiom.source.get("name"), axiom.axiom_id


def test_real_axioms_pass_validation() -> None:
    axioms = load_axioms()
    assert validate_axioms(axioms) == []
    stats = axiom_stats(axioms)
    assert stats["issue_count"] == 0


def test_real_axioms_cover_key_relations() -> None:
    axioms = load_axioms()
    by_id = {axiom.axiom_id: axiom for axiom in axioms}
    for required in (
        "A-OHM",
        "A-DCPOWER",
        "A-PF",
        "A-EFFICIENCY",
        "A-BUCK-DUTY",
        "A-BOOST-DUTY",
        "A-RIPPLE-CURRENT",
        "A-OUTPUT-RIPPLE",
        "A-HOLDUP-CAP",
        "A-DELTA-T",
        "A-RTH-CHAIN",
        "A-NTC",
        "A-YCAP",
        "A-LINEREG",
        "A-LOADREG",
        "A-SHARE-ERROR",
        "A-ENOB",
        "A-LSB",
        "A-QUANTNOISE",
        "A-APERTURE-JITTER",
        "A-RSS",
        "A-WC",
        "A-UNCERTAINTY",
        "A-CM",
        "A-TUR",
        "A-AVAIL",
        "A-HYSTERESIS",
    ):
        assert required in by_id, required
        assert by_id[required].expr.strip()


def test_real_axioms_stats_are_consistent() -> None:
    axioms = load_axioms()
    stats = axiom_stats(axioms)
    assert stats["count"] == len(axioms)
    assert sum(stats["by_kind"].values()) == len(axioms)
    assert sum(stats["by_category"].values()) == len(axioms)
    assert set(stats["by_kind"]) <= set(AXIOM_KINDS)
    assert stats["variables_count"] >= len(axioms)
    assert axiom_stats(axioms) == stats


def test_axioms_for_category_filters_and_keeps_order() -> None:
    axioms = load_axioms()
    protection = axioms_for_category("protection", axioms=axioms)
    assert protection, "protection 类别应有公理"
    assert all(axiom.category == "protection" for axiom in protection)
    expected = [a.axiom_id for a in axioms if a.category == "protection"]
    assert [a.axiom_id for a in protection] == expected


def test_axioms_for_category_unknown_returns_empty() -> None:
    axioms = load_axioms()
    assert axioms_for_category("no-such-category", axioms=axioms) == []


# --------------------------------------------------------------------------- #
# schema 往返
# --------------------------------------------------------------------------- #


def test_load_axioms_roundtrip_schema(tmp_path: Path) -> None:
    path = write_axioms(tmp_path / "axioms.yaml", [make_axiom()])
    (axiom,) = load_axioms(path)
    payload = axiom.to_dict()
    assert set(payload) == {
        "axiom_id",
        "kind",
        "statement",
        "expr",
        "variables",
        "category",
        "aliases",
        "properties",
        "related_rules",
        "source",
    }
    assert payload["variables"] == {
        "P": "功率(W)",
        "V": "电压(V)",
        "I": "电流(A)",
    }
    assert payload["source"]["card_id"] == "power-knowledge-1"
    # 往返回来应完全等价。
    (reloaded,) = load_axioms(write_axioms(tmp_path / "again.yaml", [payload]))
    assert reloaded == axiom


# --------------------------------------------------------------------------- #
# 加载失败路径 (fail-fast)
# --------------------------------------------------------------------------- #


def test_load_axioms_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="公理表文件不存在"):
        load_axioms(tmp_path / "nope.yaml")


def test_load_axioms_missing_key_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"items": []}, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="缺少顶层 axioms 键"):
        load_axioms(path)


def test_load_axioms_empty_list_raises(tmp_path: Path) -> None:
    path = write_axioms(tmp_path / "empty.yaml", [])
    with pytest.raises(ValueError, match="axioms 为空"):
        load_axioms(path)


def test_load_axioms_duplicate_id_raises(tmp_path: Path) -> None:
    path = write_axioms(tmp_path / "dup.yaml", [make_axiom(), make_axiom()])
    with pytest.raises(ValueError, match="公理 id 重复: A-TEST"):
        load_axioms(path)


def test_load_axioms_missing_required_field_raises(tmp_path: Path) -> None:
    raw = make_axiom()
    raw.pop("expr")
    path = write_axioms(tmp_path / "bad.yaml", [raw])
    with pytest.raises(ValueError, match="缺少合法字段 expr"):
        load_axioms(path)


def test_load_axioms_error_includes_position(tmp_path: Path) -> None:
    path = write_axioms(tmp_path / "bad.yaml", [make_axiom(), make_axiom(axiom_id="")])
    with pytest.raises(ValueError, match="第 2 项"):
        load_axioms(path)


# --------------------------------------------------------------------------- #
# 质量门: fixture 逐类检出
# --------------------------------------------------------------------------- #


def kinds_of(axioms: list[Axiom]) -> set[str]:
    return {issue.kind for issue in validate_axioms(axioms)}


def test_validate_axioms_clean_fixture_passes(tmp_path: Path) -> None:
    path = write_axioms(tmp_path / "axioms.yaml", [make_axiom()])
    assert validate_axioms(load_axioms(path)) == []


def test_validate_axioms_detects_unknown_kind(tmp_path: Path) -> None:
    raw = make_axiom(kind="law")
    path = write_axioms(tmp_path / "axioms.yaml", [raw])
    issues = validate_axioms(load_axioms(path))
    assert [i.kind for i in issues] == [ISSUE_KIND_UNKNOWN]
    assert issues[0].involved == ("A-TEST",)


def test_validate_axioms_detects_empty_expr() -> None:
    # load 阶段要求 expr 非空, 因此空 expr 只能由构造对象触发质量门。
    axiom = Axiom(
        axiom_id="A-EMPTY",
        kind="formula",
        statement="只有文字没有公式的条目, 用于验证质量门。",
        expr="",
        variables={"x": "变量"},
        category="general",
        source={"kind": "card", "name": "power-knowledge-1 基本电学关系"},
    )
    issues = validate_axioms([axiom])
    assert [i.kind for i in issues] == [ISSUE_EXPR_EMPTY]
    assert issues[0].involved == ("A-EMPTY",)


def test_validate_axioms_detects_empty_variables(tmp_path: Path) -> None:
    raw = make_axiom(variables={})
    path = write_axioms(tmp_path / "axioms.yaml", [raw])
    assert kinds_of(load_axioms(path)) == {ISSUE_VARIABLES_EMPTY}


def test_validate_axioms_detects_unknown_category(tmp_path: Path) -> None:
    raw = make_axiom(category="no-such-category")
    path = write_axioms(tmp_path / "axioms.yaml", [raw])
    issues = validate_axioms(load_axioms(path))
    assert [i.kind for i in issues] == [ISSUE_CATEGORY_UNKNOWN]


def test_validate_axioms_detects_missing_source(tmp_path: Path) -> None:
    raw = make_axiom()
    raw.pop("source")
    path = write_axioms(tmp_path / "axioms.yaml", [raw])
    assert kinds_of(load_axioms(path)) == {ISSUE_SOURCE_MISSING}


def test_validate_axioms_issues_are_sorted_and_deterministic(tmp_path: Path) -> None:
    raw = make_axiom(kind="bogus", category="bogus", variables={})
    raw.pop("source")
    path = write_axioms(tmp_path / "axioms.yaml", [raw])
    axioms = load_axioms(path)
    first = validate_axioms(axioms)
    assert first == validate_axioms(axioms)
    assert [(i.kind, i.detail) for i in first] == sorted(
        (i.kind, i.detail) for i in first
    )
    assert {i.kind for i in first} == {
        ISSUE_KIND_UNKNOWN,
        ISSUE_VARIABLES_EMPTY,
        ISSUE_CATEGORY_UNKNOWN,
        ISSUE_SOURCE_MISSING,
    }


def test_axiom_stats_on_fixture(tmp_path: Path) -> None:
    first = make_axiom()
    second = make_axiom(
        axiom_id="A-SECOND",
        kind="axiom",
        category="protection",
        aliases=[],
        properties=[],
        related_rules=[],
        source={"kind": "card", "name": "power-knowledge-5 保护逻辑常识"},
    )
    path = write_axioms(tmp_path / "axioms.yaml", [first, second])
    stats = axiom_stats(load_axioms(path))
    assert stats["count"] == 2
    assert stats["by_kind"] == {"axiom": 1, "formula": 1}
    assert stats["by_category"] == {"general": 1, "protection": 1}
    assert stats["with_properties"] == 1
    assert stats["with_rules"] == 1
    assert stats["sources_by_kind"] == {"card": 2}
    assert stats["variables_count"] == 6
    assert stats["issue_count"] == 0
