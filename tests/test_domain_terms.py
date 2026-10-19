"""``matrag.domain.terms`` 的离线测试。

覆盖三块:
1. 真实数据 (``data/domain/source/power_terms.yaml``) 的加载与覆盖度断言;
2. YAML → Term → dict 的往返与 schema 完整性;
3. 索引/查询/质量门在**小规模构造 fixture** 上的逐类行为 (不依赖真实数据
   碰巧触发问题, fixture 一律写进 ``tmp_path``)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from matrag.domain.terms import (
    DEFAULT_TERMS_PATH,
    ISSUE_ABBR_DUPLICATE,
    ISSUE_ALIAS_CONFLICT,
    ISSUE_CATEGORY_UNKNOWN,
    ISSUE_DEFINITION_TOO_SHORT,
    ISSUE_SOURCE_MISSING,
    MIN_DEFINITION_CHARS,
    SOURCE_KINDS,
    Term,
    build_term_index,
    find_term,
    load_terms,
    term_stats,
    validate_terms,
)
from matrag.domain.vocab import CATEGORIES, normalize_name

#: 定义一个合法术语的模板 (fixture 基线)。
TERM_TEMPLATE: dict[str, Any] = {
    "term_id": "T-AAA",
    "zh": "示例术语",
    "en": "Example Term",
    "abbr": "EX",
    "category": "general",
    "definition": "这是一个用于测试的示例术语定义, 长度满足质量门要求。",
    "aliases": ["示例别名"],
    "properties": ["voltage"],
    "related_rules": ["K-ELEC-001"],
    "source": {
        "kind": "card",
        "name": "power-knowledge-1 基本电学关系",
        "card_id": "power-knowledge-1",
        "url": "",
        "retrieved": "2026-10-11",
    },
}


def make_term(**overrides: Any) -> dict[str, Any]:
    """构造一条术语原始 dict (浅拷贝模板, 允许逐字段覆盖)。"""
    item = {key: value for key, value in TERM_TEMPLATE.items()}
    item.update(overrides)
    return item


def write_terms(path: Path, items: list[dict[str, Any]]) -> Path:
    """把术语条目写成临时 YAML 文件。"""
    path.write_text(
        yaml.safe_dump({"terms": items}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# 真实数据: 加载与覆盖度
# --------------------------------------------------------------------------- #


def test_default_path_points_into_repo_source_dir() -> None:
    assert DEFAULT_TERMS_PATH.name == "power_terms.yaml"
    assert DEFAULT_TERMS_PATH.parent.name == "source"
    assert DEFAULT_TERMS_PATH.is_file()


def test_load_real_terms_succeeds() -> None:
    terms = load_terms()
    assert len(terms) >= 60
    assert all(isinstance(term, Term) for term in terms)
    # 确定性: 同输入两次加载顺序与内容逐位一致。
    assert [t.to_dict() for t in load_terms()] == [t.to_dict() for t in terms]


def test_real_terms_ids_unique_and_categories_closed() -> None:
    terms = load_terms()
    ids = [term.term_id for term in terms]
    assert len(set(ids)) == len(ids)
    assert {term.category for term in terms} <= set(CATEGORIES)


def test_real_terms_all_have_source() -> None:
    for term in load_terms():
        assert isinstance(term.source, dict)
        assert term.source.get("name"), term.term_id
        assert term.source.get("kind") in SOURCE_KINDS, term.term_id


def test_real_terms_pass_validation_and_index_builds() -> None:
    terms = load_terms()
    assert validate_terms(terms) == []
    index = build_term_index(terms)
    assert len(index) >= len(terms)


def test_real_terms_required_concepts_are_findable() -> None:
    terms = load_terms()
    index = build_term_index(terms)
    probes = {
        "EMC": "T-EMC",
        "纹波": "T-RIPPLE",
        "纹波电流": "T-RIPPLE-CURRENT",
        "保持时间": "T-HOLDUP",
        "均流": "T-SHARE",
        "功率因数": "T-PF",
        "开尔文四线": "T-KELVIN4W",
        "pogo pin": "T-POGOPIN",
        "ENOB": "T-ENOB",
        "PMBus": "T-PMBUS",
    }
    for text, expected in probes.items():
        hit = find_term(text, index=index, terms=terms)
        assert hit is not None, text
        assert hit.term_id == expected, text


def test_real_terms_stats_are_consistent() -> None:
    terms = load_terms()
    stats = term_stats(terms)
    assert stats["count"] == len(terms)
    assert sum(stats["by_category"].values()) == len(terms)
    assert sum(stats["sources_by_kind"].values()) == len(terms)
    assert stats["with_properties"] >= 1
    assert stats["aliases_count"] >= len(terms)
    assert term_stats(terms) == stats


# --------------------------------------------------------------------------- #
# schema 往返
# --------------------------------------------------------------------------- #


def test_load_terms_roundtrip_schema(tmp_path: Path) -> None:
    path = write_terms(tmp_path / "terms.yaml", [make_term()])
    (term,) = load_terms(path)
    payload = term.to_dict()
    assert set(payload) == {
        "term_id",
        "zh",
        "en",
        "abbr",
        "category",
        "definition",
        "aliases",
        "properties",
        "related_rules",
        "source",
    }
    assert payload["aliases"] == ["示例别名"]
    assert payload["source"]["card_id"] == "power-knowledge-1"
    # 往返回来应完全等价。
    (reloaded,) = load_terms(write_terms(tmp_path / "again.yaml", [payload]))
    assert reloaded == term


def test_optional_fields_default_to_empty(tmp_path: Path) -> None:
    raw = make_term()
    for key in ("en", "abbr", "aliases", "properties", "related_rules"):
        raw.pop(key)
    path = write_terms(tmp_path / "terms.yaml", [raw])
    (term,) = load_terms(path)
    assert term.en == "" and term.abbr == ""
    assert term.aliases == () and term.properties == () and term.related_rules == ()


def test_all_names_drops_blanks_and_keeps_order() -> None:
    term = Term(
        term_id="T-X",
        zh="中文名",
        en="",
        abbr="AB",
        category="general",
        definition="长度足够的示例定义用于测试。",
        aliases=("别一", "", "中文名"),
    )
    assert term.all_names() == ("中文名", "AB", "别一")


# --------------------------------------------------------------------------- #
# 加载失败路径 (fail-fast)
# --------------------------------------------------------------------------- #


def test_load_terms_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="术语表文件不存在"):
        load_terms(tmp_path / "nope.yaml")


def test_load_terms_missing_key_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"items": []}, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="缺少顶层 terms 键"):
        load_terms(path)


def test_load_terms_empty_list_raises(tmp_path: Path) -> None:
    path = write_terms(tmp_path / "empty.yaml", [])
    with pytest.raises(ValueError, match="terms 为空"):
        load_terms(path)


def test_load_terms_invalid_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("terms: [ {id: 1}\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        load_terms(path)


def test_load_terms_duplicate_id_raises(tmp_path: Path) -> None:
    path = write_terms(tmp_path / "dup.yaml", [make_term(), make_term()])
    with pytest.raises(ValueError, match="术语 id 重复: T-AAA"):
        load_terms(path)


def test_load_terms_missing_required_field_raises(tmp_path: Path) -> None:
    raw = make_term()
    raw.pop("definition")
    path = write_terms(tmp_path / "bad.yaml", [raw])
    with pytest.raises(ValueError, match="缺少合法字段 definition"):
        load_terms(path)


# --------------------------------------------------------------------------- #
# 索引与查询
# --------------------------------------------------------------------------- #


def test_build_term_index_maps_normalized_names(tmp_path: Path) -> None:
    path = write_terms(tmp_path / "terms.yaml", [make_term()])
    (term,) = load_terms(path)
    index = build_term_index([term])
    assert index[normalize_name("示例术语")] == "T-AAA"
    assert index[normalize_name("Example Term")] == "T-AAA"
    assert index[normalize_name("ex")] == "T-AAA"
    assert index[normalize_name("示例别名")] == "T-AAA"


def test_build_term_index_alias_conflict_raises(tmp_path: Path) -> None:
    first = make_term()
    second = make_term(term_id="T-BBB", zh="另一术语", abbr="BB", aliases=["撞名"])
    first["aliases"] = ["撞名"]
    path = write_terms(tmp_path / "conflict.yaml", [first, second])
    terms = load_terms(path)
    with pytest.raises(ValueError, match="归一后冲突"):
        build_term_index(terms)


def test_find_term_hits_zh_alias_en_abbr(tmp_path: Path) -> None:
    path = write_terms(tmp_path / "terms.yaml", [make_term()])
    terms = load_terms(path)
    for probe in ("示例术语", "示例别名", "Example Term", "EX"):
        hit = find_term(probe, terms=terms)
        assert hit is not None and hit.term_id == "T-AAA", probe


def test_find_term_is_case_and_whitespace_insensitive(tmp_path: Path) -> None:
    path = write_terms(tmp_path / "terms.yaml", [make_term()])
    terms = load_terms(path)
    for probe in ("example term", "  EXAMPLE   TERM ", "ＥＸ"):
        hit = find_term(probe, terms=terms)
        assert hit is not None and hit.term_id == "T-AAA", probe


def test_find_term_returns_none_when_missing(tmp_path: Path) -> None:
    path = write_terms(tmp_path / "terms.yaml", [make_term()])
    terms = load_terms(path)
    assert find_term("不存在的术语", terms=terms) is None
    assert find_term("", terms=terms) is None


def test_find_term_with_prebuilt_index_does_not_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_terms(tmp_path / "terms.yaml", [make_term()])
    terms = load_terms(path)
    index = build_term_index(terms)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("不应再读默认文件")

    monkeypatch.setattr("matrag.domain.terms.load_terms", boom)
    hit = find_term("示例术语", index=index, terms=terms)
    assert hit is not None and hit.term_id == "T-AAA"


# --------------------------------------------------------------------------- #
# 质量门: fixture 逐类检出
# --------------------------------------------------------------------------- #


def kinds_of(terms: list[Term]) -> set[str]:
    return {issue.kind for issue in validate_terms(terms)}


def test_validate_terms_clean_fixture_passes(tmp_path: Path) -> None:
    path = write_terms(tmp_path / "terms.yaml", [make_term()])
    assert validate_terms(load_terms(path)) == []


def test_validate_terms_detects_unknown_category(tmp_path: Path) -> None:
    raw = make_term(category="no-such-category")
    path = write_terms(tmp_path / "terms.yaml", [raw])
    issues = validate_terms(load_terms(path))
    assert [i.kind for i in issues] == [ISSUE_CATEGORY_UNKNOWN]
    assert issues[0].involved == ("T-AAA",)


def test_validate_terms_detects_missing_source(tmp_path: Path) -> None:
    raw = make_term()
    raw.pop("source")
    path = write_terms(tmp_path / "terms.yaml", [raw])
    assert kinds_of(load_terms(path)) == {ISSUE_SOURCE_MISSING}


def test_validate_terms_detects_source_without_name(tmp_path: Path) -> None:
    raw = make_term(source={"kind": "card", "card_id": "power-knowledge-1"})
    path = write_terms(tmp_path / "terms.yaml", [raw])
    assert kinds_of(load_terms(path)) == {ISSUE_SOURCE_MISSING}


def test_validate_terms_detects_short_definition(tmp_path: Path) -> None:
    raw = make_term(definition="太短")
    path = write_terms(tmp_path / "terms.yaml", [raw])
    issues = validate_terms(load_terms(path))
    assert [i.kind for i in issues] == [ISSUE_DEFINITION_TOO_SHORT]
    assert str(MIN_DEFINITION_CHARS) in issues[0].detail


def test_validate_terms_detects_duplicate_abbr(tmp_path: Path) -> None:
    # 只保留 term_id/zh/category/definition/abbr, 使本 fixture 只触发"缩写重复";
    # 否则第二个术语的缩写会与第一个的英文名/别名撞车, 顺带报别名冲突。
    first = make_term(
        term_id="T-AAA",
        zh="张三术语",
        en="",
        abbr="EX",
        aliases=[],
        source={"kind": "card", "name": "power-knowledge-1 基本电学关系"},
    )
    second = make_term(term_id="T-BBB", zh="李四术语", en="", abbr="ex", aliases=[])
    path = write_terms(tmp_path / "terms.yaml", [first, second])
    issues = validate_terms(load_terms(path))
    assert [i.kind for i in issues] == [ISSUE_ABBR_DUPLICATE]
    assert issues[0].involved == ("T-AAA", "T-BBB")


def test_validate_terms_detects_alias_conflict(tmp_path: Path) -> None:
    first = make_term(aliases=["共享别名"])
    second = make_term(
        term_id="T-BBB", zh="另一术语", en="Another Term", abbr="BB", aliases=["共享别名"]
    )
    path = write_terms(tmp_path / "terms.yaml", [first, second])
    kinds = kinds_of(load_terms(path))
    assert ISSUE_ALIAS_CONFLICT in kinds


def test_validate_terms_issues_are_sorted_and_deterministic(
    tmp_path: Path,
) -> None:
    raw = make_term(category="bogus", definition="短")
    raw.pop("source")
    path = write_terms(tmp_path / "terms.yaml", [raw])
    terms = load_terms(path)
    first = validate_terms(terms)
    assert first == validate_terms(terms)
    assert [(i.kind, i.detail) for i in first] == sorted(
        (i.kind, i.detail) for i in first
    )
    assert {i.kind for i in first} == {
        ISSUE_CATEGORY_UNKNOWN,
        ISSUE_SOURCE_MISSING,
        ISSUE_DEFINITION_TOO_SHORT,
    }


def test_term_stats_on_fixture(tmp_path: Path) -> None:
    first = make_term()
    second = make_term(
        term_id="T-BBB",
        zh="另一术语",
        en="Another Term",
        abbr="BB",
        aliases=[],
        properties=[],
        related_rules=[],
        source={"kind": "spec_table", "name": "PA601-D54A 规格书 §3 术语和约定"},
    )
    path = write_terms(tmp_path / "terms.yaml", [first, second])
    stats = term_stats(load_terms(path))
    assert stats["count"] == 2
    assert stats["by_category"] == {"general": 2}
    assert stats["with_properties"] == 1
    assert stats["with_rules"] == 1
    assert stats["sources_by_kind"] == {"card": 1, "spec_table": 1}
    assert stats["aliases_count"] == 1
