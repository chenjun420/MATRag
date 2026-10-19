"""产测方法层 (``matrag.domain.test_methods``) 的离线测试。

覆盖: 默认路径/环境变量/缺失与重复 fail-fast、35 条方法的结构完整性
（四个新字段 parameters/instruments/interfaces/metrics）、质量门
（真实引用 + 注入式检查）、统计、公共知识卡片渲染与 jsonl 落盘。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from matrag.domain.cards import write_knowledge_cards
from matrag.domain.rules_loader import load_rules
from matrag.domain.terms import load_terms
from matrag.domain.test_methods import (
    DEFAULT_TEST_METHODS_PATH,
    ISSUE_CATEGORY_UNKNOWN,
    ISSUE_DEFINITION_TOO_SHORT,
    ISSUE_MEASURE_UNKNOWN,
    ISSUE_PROCEDURE_EMPTY,
    ISSUE_RULE_UNKNOWN,
    TEST_METHODS_PATH_ENV,
    TestMethod,
    build_method_cards,
    load_test_methods,
    resolve_test_methods_path,
    validate_test_methods,
)
from matrag.domain.test_methods import (
    test_method_stats as method_stats,
)

METHODS = load_test_methods()

_NEW_METHODS = {
    "M-PF",
    "M-INPUT-SLOW",
    "M-OPP",
    "M-REVERSE-IN",
    "M-ORING",
    "M-SHARE",
    "M-TELEMETRY",
    "M-TELESIGNAL",
    "M-TELECONTROL",
    "M-COMM",
    "M-POWERCYCLE",
    "M-UVP",
}

_MINIMAL_YAML = """\
- method_id: M-TEST-1
  zh: 测试方法
  en: Test Method
  category: general
  definition: 一条用于覆盖加载路径测试的方法定义, 满足最短字数要求。
  parameters: [常温]
  instruments: [示波器]
  interfaces: [输出端子]
  metrics: [峰峰值]
  procedure: [第一步]
  criteria: [不大于限值]
  measures: [T-RIPPLE]
  references: [https://example.com]
  retrieved: "2026-10-12"
"""


def test_default_path_and_ids() -> None:
    """默认源文件存在, 35 条方法 id 唯一且格式合法。"""
    assert resolve_test_methods_path() == DEFAULT_TEST_METHODS_PATH
    assert DEFAULT_TEST_METHODS_PATH.is_file()
    assert len(METHODS) == 35
    ids = [method.method_id for method in METHODS]
    assert len(set(ids)) == len(ids)
    assert all(method_id.startswith("M-") for method_id in ids)


def test_new_methods_are_present() -> None:
    """扩域的 12 条新方法全部在列。"""
    ids = {method.method_id for method in METHODS}
    assert _NEW_METHODS <= ids


def test_four_new_fields_are_populated() -> None:
    """每条方法的 测试条件/仪表/接口/指标 四字段均非空。"""
    for method in METHODS:
        assert method.parameters, method.method_id
        assert method.instruments, method.method_id
        assert method.interfaces, method.method_id
        assert method.metrics, method.method_id
        assert method.procedure and method.criteria, method.method_id
        assert method.references and method.retrieved, method.method_id


def test_validate_is_clean() -> None:
    """35 条方法对术语表/规则集零断引用。"""
    assert validate_test_methods() == []


def test_validate_references_against_loaded_tables() -> None:
    """measures/rules 均落在真实术语表与规则集里 (双保险, 不经 validate 内部)。"""
    term_ids = {term.term_id for term in load_terms()}
    rule_ids = {rule.rule_id for rule in load_rules()}
    for method in METHODS:
        assert set(method.measures) <= term_ids, method.method_id
        assert set(method.rules) <= rule_ids, method.method_id


def test_stats() -> None:
    """统计: 总数/类别分布/去重连接数 (与真实源文件一致)。"""
    stats = method_stats()
    assert stats["total"] == 35
    assert stats["by_category"] == {
        "communication": 4,
        "dynamics": 4,
        "efficiency": 1,
        "general": 2,
        "input": 3,
        "output": 5,
        "protection": 9,
        "reliability": 2,
        "safety": 4,
        "thermal": 1,
    }
    assert stats["measures_count"] == 77
    assert stats["rules_count"] == 78


def test_cards_render_sections() -> None:
    """卡片: 数量/id 前缀/标题与六个正文分区。"""
    cards = build_method_cards()
    assert len(cards) == len(METHODS)
    assert len({card.card_id for card in cards}) == len(cards)
    by_id = {card.card_id: card for card in cards}
    card = by_id["power-method-ripple-noise"]
    assert card.title == "## 输出纹波与噪声测试"
    assert card.section_path == "输出纹波与噪声测试"
    assert card.domain == "power"
    sections = (
        "测试条件与参数:",
        "所需仪表:",
        "接口与接线:",
        "测试指标:",
        "测试步骤:",
        "判据:",
        "参考来源:",
    )
    for section in sections:
        assert section in card.body, section


def test_cards_jsonl_roundtrip(tmp_path: Path) -> None:
    """jsonl 每行一张方法卡片, 可原样读回。"""
    cards = build_method_cards()
    out = write_knowledge_cards(cards, tmp_path / "power_methods.jsonl")
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(cards) == 35
    first = json.loads(lines[0])
    assert first["card_id"] == cards[0].card_id
    assert first["body"] == cards[0].body


def test_missing_file_raises(tmp_path: Path) -> None:
    """源文件缺失必须 FileNotFoundError, 不静默返回空列表。"""
    with pytest.raises(FileNotFoundError):
        load_test_methods(tmp_path / "nope.yaml")


def test_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量 MATRAG_DOMAIN_TEST_METHODS_PATH 可覆盖默认路径。"""
    doc = tmp_path / "methods.yaml"
    doc.write_text(_MINIMAL_YAML, encoding="utf-8")
    monkeypatch.setenv(TEST_METHODS_PATH_ENV, str(doc))
    methods = load_test_methods()
    assert [method.method_id for method in methods] == ["M-TEST-1"]
    assert methods[0].card_id == "power-method-test-1"


def test_loader_fails_fast_on_missing_metrics(tmp_path: Path) -> None:
    """缺 metrics 字段 → ValueError (fail-fast, 不带病加载)。"""
    doc = tmp_path / "methods.yaml"
    doc.write_text(_MINIMAL_YAML.replace("  metrics: [峰峰值]\n", ""), encoding="utf-8")
    with pytest.raises(ValueError, match="metrics"):
        load_test_methods(doc)


def test_loader_rejects_duplicate_ids(tmp_path: Path) -> None:
    """两条记录同一 method_id → ValueError, 不静默覆盖。"""
    doc = tmp_path / "methods.yaml"
    doc.write_text(_MINIMAL_YAML + _MINIMAL_YAML, encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_test_methods(doc)


def test_validate_detects_injected_issues() -> None:
    """注入式检查: 类别/定义/步骤/断引用问题全部报出。"""
    bad = TestMethod(
        method_id="M-BAD-1",
        zh="坏方法",
        en="Bad Method",
        category="not-a-category",
        definition="短",
        measures=("T-NOPE",),
        rules=("K-NOPE",),
    )
    kinds = {issue.kind for issue in validate_test_methods([bad], terms=(), rules=())}
    assert ISSUE_CATEGORY_UNKNOWN in kinds
    assert ISSUE_DEFINITION_TOO_SHORT in kinds
    assert ISSUE_PROCEDURE_EMPTY in kinds
    assert ISSUE_MEASURE_UNKNOWN in kinds
    assert ISSUE_RULE_UNKNOWN in kinds
