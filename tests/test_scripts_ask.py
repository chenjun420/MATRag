"""scripts.ask 审计接线测试（全离线）: record_audit 成功落档 / 失败显式留痕。"""

from __future__ import annotations

from typing import Any

from matrag.answer import AnswerResult, Citation
from matrag.provenance import AuditTrail, ProvenanceError
from scripts import ask as ask_script


def _result() -> AnswerResult:
    result = AnswerResult(
        answer="测试答案 [1]",
        citations=[
            Citation(
                marker="[1]",
                doc_id="D",
                page=2,
                section_path="4.1",
                table_id="T1",
                chunk_id="D:0000",
                score=0.9,
            )
        ],
        rule_hints=[{"rule_id": "K-ELEC-001", "kind": "derive"}],
        covered=True,
    )
    result.plan = {"kind": "spec_value"}
    return result


def test_record_audit_memory_roundtrip() -> None:
    """成功路径: memory 轨落档, answer_id 可回查、校验通过、无降级。"""
    trail = AuditTrail(backend="memory")
    result = _result()
    out = ask_script.record_audit("问题", result, elapsed_ms=12, model="stub", trail=trail)
    assert out["ok"] is True
    assert out["backend"] == "memory"
    assert out["answer_id"]
    record = trail.get(out["answer_id"])
    assert record is not None and record.answer_id == out["answer_id"]
    assert trail.verify(out["answer_id"]) is True
    assert result.degradations == []


def test_record_audit_provenance_error_leaves_trace() -> None:
    class _Broken:
        backend = "sqlite"
        db_path = None

        def record_answer(self, **kwargs: Any) -> Any:
            raise ProvenanceError("写入被拒")

    result = _result()
    out = ask_script.record_audit("问题", result, elapsed_ms=1, model="", trail=_Broken())
    assert out["ok"] is False and "写入被拒" in out["error"]
    assert [note["kind"] for note in result.degradations] == ["provenance_unavailable"]


def test_record_audit_unexpected_error_marks_failed() -> None:
    class _Broken:
        backend = "memory"
        db_path = None

        def record_answer(self, **kwargs: Any) -> Any:
            raise TypeError("坏载荷")

    result = _result()
    out = ask_script.record_audit("问题", result, elapsed_ms=1, model="", trail=_Broken())
    assert out["ok"] is False
    assert result.degradations[-1]["kind"] == "provenance_failed"
