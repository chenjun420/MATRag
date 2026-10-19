"""域知识卡片测试 (P1-2)。

知识源走 :func:`matrag.domain.cards.load_knowledge_cards` 的默认路径解析
(环境变量覆盖), 测试里不硬编码知识源绝对路径。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from matrag.domain.cards import (
    DEFAULT_KNOWLEDGE_PATH,
    load_domain_rules_raw,
    load_knowledge_cards,
    resolve_knowledge_path,
    resolve_rules_path,
    write_knowledge_cards,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

CARDS = load_knowledge_cards()


def test_source_path_defaults_to_materialized_doc() -> None:
    """默认路径应指向仓内内联的知识文档。"""
    assert resolve_knowledge_path() == DEFAULT_KNOWLEDGE_PATH
    assert DEFAULT_KNOWLEDGE_PATH.is_file()


def test_cards_are_split_and_non_empty() -> None:
    """卡片切分非空: 数量 > 8, 每张卡片正文非空、id 唯一。"""
    assert len(CARDS) > 8
    assert len({c.card_id for c in CARDS}) == len(CARDS)
    for card in CARDS:
        assert card.card_id.startswith("power-knowledge-")
        assert card.body.strip(), card.card_id
        assert card.section_path.strip(), card.card_id
        assert card.title.startswith("## "), card.card_id
        assert card.section_path in card.title
        assert card.char_count > 0
        assert card.source == "MATRag power_knowledge.md"
        assert card.domain == "power"


def test_card_ids_are_sequential() -> None:
    """card_id 按小节顺序编号。"""
    assert [c.card_id for c in CARDS] == [
        f"power-knowledge-{i}" for i in range(1, len(CARDS) + 1)
    ]


def test_cards_keep_expected_sections() -> None:
    """关键小节都应在卡片里 (切分没有漏节)。"""
    paths = [c.section_path for c in CARDS]
    for expected in ("基本电学关系", "保护功能矩阵", "产测校准与降额"):
        assert expected in paths


def test_sub_sections_stay_in_body() -> None:
    """``### `` 子节不另起卡片, 随正文保留。"""
    card = next(c for c in CARDS if c.section_path == "并联均流与下垂法")
    assert "### 产测执行方法" in card.body


def test_missing_source_raises(tmp_path: Path) -> None:
    """源文件缺失必须 FileNotFoundError, 不返回空列表。"""
    with pytest.raises(FileNotFoundError):
        load_knowledge_cards(tmp_path / "nope.md")


def test_env_var_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """环境变量 MATRAG_DOMAIN_KNOWLEDGE_PATH 可覆盖默认路径。"""
    doc = tmp_path / "knowledge.md"
    doc.write_text("# 标题\n\n## 甲\n\n内容甲。\n\n## 乙\n\n内容乙。\n", encoding="utf-8")
    monkeypatch.setenv("MATRAG_DOMAIN_KNOWLEDGE_PATH", str(doc))
    cards = load_knowledge_cards()
    assert [c.section_path for c in cards] == ["甲", "乙"]
    assert cards[0].body == "内容甲。"


def test_rules_raw_and_default_path() -> None:
    """rules.yaml 默认路径可读, 且是 132 条 dict。"""
    assert resolve_rules_path().is_file()
    raw = load_domain_rules_raw()
    assert len(raw) == 132
    assert all(isinstance(item, dict) and item["id"] for item in raw)


def test_write_knowledge_cards_jsonl(tmp_path: Path) -> None:
    """jsonl 每行一个 dict, 可原样读回。"""
    out = write_knowledge_cards(CARDS[:2], tmp_path / "knowledge_cards.jsonl")
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["card_id"] == CARDS[0].card_id
    assert first["body"] == CARDS[0].body
    assert first["char_count"] == len(CARDS[0].body)


def test_import_script_generates_artifacts(tmp_path: Path) -> None:
    """冒烟脚本可离线跑通, 产物齐全 (卡片 jsonl + 规则副本 + Datalog + 本体 + 知识图谱)。"""
    out_dir = tmp_path / "generated"
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "import_domain.py"),
            "--out",
            str(out_dir),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    jsonl = out_dir / "knowledge_cards.jsonl"
    assert jsonl.is_file()
    cards = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert len(cards) == len(CARDS)
    methods_jsonl = out_dir / "power_methods.jsonl"
    assert methods_jsonl.is_file()
    methods = [json.loads(line) for line in methods_jsonl.read_text(encoding="utf-8").splitlines()]
    assert len(methods) == 35
    assert all(item["card_id"].startswith("power-method-") for item in methods)
    rules_copy = out_dir / "power_rules.yaml"
    assert rules_copy.is_file()
    assert rules_copy.read_bytes() == resolve_rules_path().read_bytes()
    clauses = [
        line
        for line in (out_dir / "power_rules_datalog.dl").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert len(clauses) == 132
    assert clauses[0].startswith("holds(k_elec_001, power, V) :- input(voltage), input(current),")
    ontology_json = out_dir / "power_ontology.json"
    assert (out_dir / "power_ontology.ttl").is_file()
    ontology = json.loads(ontology_json.read_text(encoding="utf-8"))
    assert ontology["stats"]["rule_count"] == 132
    kg = json.loads((out_dir / "power_knowledge_graph.json").read_text(encoding="utf-8"))
    assert kg["stats"]["totals"]["nodes"] > 0
    assert kg["coverage"]["params_total"] > 0
    assert kg["stats"]["nodes"]["method"] == 35
    assert kg["coverage"]["methods_total"] == 35
    assert os.environ.get("MATRAG_DOMAIN_KNOWLEDGE_PATH") is None


def test_import_script_main_returns_zero(tmp_path: Path) -> None:
    """直接调 main() 冒烟: 返回 0 并落地产物。"""
    import importlib.util

    script = REPO_ROOT / "scripts" / "import_domain.py"
    spec = importlib.util.spec_from_file_location("import_domain_under_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out_dir = tmp_path / "generated"
    code = module.main(["--out", str(out_dir)])
    assert code == 0
    assert (out_dir / "knowledge_cards.jsonl").is_file()
    assert (out_dir / "power_rules_datalog.dl").is_file()
    assert (out_dir / "power_ontology.ttl").is_file()
    assert (out_dir / "power_methods.jsonl").is_file()
    assert (out_dir / "power_knowledge_graph.json").is_file()
