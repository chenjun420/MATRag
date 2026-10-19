"""audit_export 测试（全离线）: PROV-O 导出（turtle/ntriples/jsonld）+ CLI 失败路径。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from matrag.provenance import AuditTrail
from scripts import audit_export as ae


def _write_one_record(db: Path, *, answer_id: str = "answer:test-1") -> None:
    """写一条最小审计记录（turtle 导出里有实体可断言）。"""
    with AuditTrail("sqlite", db_path=db) as trail:
        trail.record_answer(
            query="纹波测试怎么测？",
            plan_summary={"intent": "procedure"},
            hits=[
                {
                    "doc_id": "PA601",
                    "chunk_id": "c1",
                    "rrf_score": 0.5,
                    "meta": {"page": 3},
                }
            ],
            model="qwen3.7-flash",
            elapsed_ms=12,
            answer_id=answer_id,
        )


def test_export_turtle_contains_prov_and_entity(tmp_path: Path) -> None:
    """turtle 导出: 记录条数正确、PROV 前缀与实体 id 都在。"""
    db = tmp_path / "provenance.db"
    _write_one_record(db)
    rdf, count = ae.export(db, fmt="turtle")
    assert count == 1
    assert "http://www.w3.org/ns/prov#" in rdf
    assert "answer:test-1" in rdf


def test_export_jsonld_is_valid_json(tmp_path: Path) -> None:
    """jsonld 导出产物可被 json.loads 解析（交付给外部工具时必要）。"""
    db = tmp_path / "provenance.db"
    _write_one_record(db)
    rdf, count = ae.export(db, fmt="jsonld")
    assert count == 1
    parsed = json.loads(rdf)
    assert parsed


def test_export_ntriples_contains_entity_and_activity(tmp_path: Path) -> None:
    """ntriples 导出: 实体 URI 与写入活动 URI 出现在三元组里。"""
    db = tmp_path / "provenance.db"
    _write_one_record(db)
    rdf, _ = ae.export(db, fmt="ntriples")
    assert "answer:test-1" in rdf
    assert "record_answer" in rdf


def test_export_missing_db_fails_loudly(tmp_path: Path) -> None:
    """审计库不存在 → FileNotFoundError（不凭空造空库）。"""
    with pytest.raises(FileNotFoundError):
        ae.export(tmp_path / "nope.db", fmt="turtle")


def test_export_unknown_format_rejected(tmp_path: Path) -> None:
    """格式不在支持列表 → ValueError。"""
    db = tmp_path / "provenance.db"
    _write_one_record(db)
    with pytest.raises(ValueError):
        ae.export(db, fmt="xml")


def test_main_writes_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI --out: 落盘成功、stderr 摘要含条数。"""
    db = tmp_path / "provenance.db"
    _write_one_record(db)
    out = tmp_path / "reports" / "audit.ttl"
    rc = ae.main(["--db", str(db), "--format", "turtle", "--out", str(out)])
    assert rc == 0
    assert out.exists()
    assert "answer:test-1" in out.read_text(encoding="utf-8")
    assert "1 条审计记录" in capsys.readouterr().err


def test_main_missing_db_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI 失败路径: 退出码 2 + stderr 明确说明。"""
    rc = ae.main(["--db", str(tmp_path / "nope.db")])
    assert rc == 2
    assert "导出失败" in capsys.readouterr().err
