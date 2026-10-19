"""P1-6 corpus_reader 测试: 假 corpus（tmp 目录）+ 损坏/缺失/穿越 doc_id 不抛异常。

全程离线: 自造 md / tables.json / parse_report.json，不起任何服务。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from matrag.gateway import corpus_reader

MD_A = """# 电源模块规格书 A

## 1 概述

本文件描述电源模块的输出特性。

## 2 电特性

**表2-1 输出特性**

<!-- page:3 section:2 > 电特性 -->

| 项目 | 限值 |
| --- | --- |
| 输出电压 | 5V |
| 输出电流 | 20A |
"""

MD_CORRUPT = b"\xff\xfe\x00\x01 invalid utf-8 binary garbage"

TABLES_A = [
    {
        "id": "t0-0",
        "caption": "表2-1 输出特性",
        "headers": ["项目", "限值"],
        "rows": [["输出电压", "5V"], ["输出电流", "20A"]],
        "marks": ["clean"],
        "confidence": 1.0,
        "source": {"page": 3, "section_path": "2 > 电特性", "provider": "local"},
    }
]

REPORT_A = {
    "doc_id": "doc-a",
    "source_path": "docs/doc-a.pdf",
    "provider": "local",
    "ok": True,
    "tables": [{"table_id": "t0-0", "page": 3, "n_rows": 2, "n_cols": 2,
                "merged_cells": 0, "flags": [], "note": "clean"}],
    "unresolved": [],
    "stats": {"n_tables": 1},
}


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """假语料: doc-a（完整）/ doc-b（只有 tables.json）/ doc-c（JSON 损坏）。"""
    base = tmp_path / "corpus"
    base.mkdir()
    (base / "doc-a.md").write_text(MD_A, encoding="utf-8")
    (base / "doc-a.tables.json").write_text(
        json.dumps(TABLES_A, ensure_ascii=False), encoding="utf-8"
    )
    (base / "doc-b.tables.json").write_text("[]", encoding="utf-8")
    (base / "doc-c.tables.json").write_text("{ not json", encoding="utf-8")
    return base


@pytest.fixture
def reports(tmp_path: Path) -> Path:
    """假报告目录: doc-a 有、doc-b 没有、doc-c 损坏。"""
    base = tmp_path / "reports"
    base.mkdir()
    (base / "doc-a.parse_report.json").write_text(
        json.dumps(REPORT_A, ensure_ascii=False), encoding="utf-8"
    )
    (base / "doc-c.parse_report.json").write_text("{ broken", encoding="utf-8")
    return base


# --------------------------------------------------------------------------- #
# list_documents
# --------------------------------------------------------------------------- #
def test_list_documents_pairs_md_and_tables(corpus: Path) -> None:
    """doc-a: md+tables.json 配对，标题取 md 的 H1。"""
    docs = corpus_reader.list_documents(corpus)
    assert [d.doc_id for d in docs] == ["doc-a", "doc-b", "doc-c"]
    doc_a = docs[0]
    assert doc_a.title == "电源模块规格书 A"
    assert doc_a.md_path.name == "doc-a.md"
    assert doc_a.tables_path.name == "doc-a.tables.json"


def test_list_documents_tables_only_entry(corpus: Path) -> None:
    """doc-b 只有 tables.json: 仍列出（装配层半成品可见），标题退化为 doc_id。"""
    docs = {d.doc_id: d for d in corpus_reader.list_documents(corpus)}
    assert docs["doc-b"].title == "doc-b"
    assert not docs["doc-b"].md_path.is_file()


def test_list_documents_empty_dir(tmp_path: Path) -> None:
    """空目录 → 空列表（不抛）。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert corpus_reader.list_documents(empty) == []


def test_list_documents_missing_dir(tmp_path: Path) -> None:
    """目录不存在 → 空列表（不抛）。"""
    assert corpus_reader.list_documents(tmp_path / "nope") == []


# --------------------------------------------------------------------------- #
# get_table
# --------------------------------------------------------------------------- #
def test_get_table_found(corpus: Path) -> None:
    """按 table_id 命中: headers/rows/source.page 完整。"""
    table = corpus_reader.get_table("doc-a", "t0-0", corpus_dir=corpus)
    assert table is not None
    assert table["caption"] == "表2-1 输出特性"
    assert table["headers"] == ["项目", "限值"]
    assert table["rows"][1] == ["输出电流", "20A"]


def test_get_table_unknown_table(corpus: Path) -> None:
    """table_id 不存在 → None。"""
    assert corpus_reader.get_table("doc-a", "no-such", corpus_dir=corpus) is None


def test_get_table_unknown_doc(corpus: Path) -> None:
    """doc_id 不存在 → None（含损坏 JSON 的 doc-c，不抛）。"""
    assert corpus_reader.get_table("nope", "t0-0", corpus_dir=corpus) is None
    assert corpus_reader.get_table("doc-c", "t0-0", corpus_dir=corpus) is None


def test_get_table_rejects_traversal(corpus: Path) -> None:
    """目录穿越 doc_id → None（fail-closed，不读文件系统外内容）。"""
    assert corpus_reader.get_table("../secrets", "t0-0", corpus_dir=corpus) is None
    assert corpus_reader.get_table("doc-a", "t0-0", corpus_dir=corpus) is not None


# --------------------------------------------------------------------------- #
# get_doc_outline
# --------------------------------------------------------------------------- #
def test_outline_headings_and_tables(corpus: Path) -> None:
    """标题树（level/section_path）+ 表格清单（caption/page 来自 tables.json）。"""
    outline = corpus_reader.get_doc_outline("doc-a", corpus_dir=corpus)
    assert "error" not in outline
    assert outline["title"] == "电源模块规格书 A"
    assert [(h["level"], h["text"]) for h in outline["headings"]] == [
        (1, "电源模块规格书 A"),
        (2, "1 概述"),
        (2, "2 电特性"),
    ]
    assert [(t["table_id"], t["caption"], t["page"]) for t in outline["tables"]] == [
        ("t0-0", "表2-1 输出特性", 3)
    ]
    assert outline["tables"][0]["section_path"] == "2 电特性"


def test_outline_md_missing(corpus: Path) -> None:
    """md 不存在 → error=md_not_found，结构其余字段仍可用。"""
    outline = corpus_reader.get_doc_outline("doc-b", corpus_dir=corpus)
    assert outline["error"] == "md_not_found"
    assert outline["headings"] == []


def test_outline_corrupt_md(corpus: Path) -> None:
    """md 二进制损坏（非法 UTF-8）→ error 前缀 md_unreadable（不抛给传输层）。"""
    (corpus / "doc-c.md").write_bytes(MD_CORRUPT)
    outline = corpus_reader.get_doc_outline("doc-c", corpus_dir=corpus)
    assert str(outline["error"]).startswith("md_unreadable:")


def test_outline_invalid_doc_id(corpus: Path) -> None:
    """穿越/空 doc_id → error=invalid_doc_id。"""
    assert corpus_reader.get_doc_outline("../../etc", corpus_dir=corpus)["error"] == "invalid_doc_id"
    assert corpus_reader.get_doc_outline("", corpus_dir=corpus)["error"] == "invalid_doc_id"


# --------------------------------------------------------------------------- #
# read_report
# --------------------------------------------------------------------------- #
def test_read_report_found(reports: Path) -> None:
    """报告存在 → 原 dict（ok/tables/unresolved 齐备）。"""
    report = corpus_reader.read_report("doc-a", reports_dir=reports)
    assert report is not None
    assert report["ok"] is True
    assert report["tables"][0]["table_id"] == "t0-0"


def test_read_report_missing_or_corrupt(reports: Path) -> None:
    """缺失/损坏/穿越 → None（不抛）。"""
    assert corpus_reader.read_report("doc-b", reports_dir=reports) is None
    assert corpus_reader.read_report("doc-c", reports_dir=reports) is None
    assert corpus_reader.read_report("../x", reports_dir=reports) is None
