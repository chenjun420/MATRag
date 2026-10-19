"""审计轨测试（``matrag.provenance``）: 后端选择 / 往返 / 校验和 / 篡改检出 / 落盘重开。

全程离线: 不用 LLM/网络，sqlite 一律落 ``tmp_path``; ``memory`` 后端断言"一个字节
都不落盘"。settings 只用 monkeypatch 改字段（``store_dir`` / ``provenance_backend``），
不碰仓库里的 ``data/``。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from matrag.answer import Citation
from matrag.config import settings
from matrag.provenance import (
    KIND_ANSWER,
    KIND_RETRIEVAL,
    AuditTrail,
    ProvenanceError,
    ProvenanceRecord,
    compute_record_checksum,
)

QUERY = "PA601-D54A 表11 输出特性的最大输出功率是多少"


# --------------------------------------------------------------------------- #
# 替身（与 RetrievalHit / QueryPlan / RuleHint 同形状的鸭子类型）
# --------------------------------------------------------------------------- #
class StubHit:
    """与 ``matrag.retrieve.retriever.RetrievalHit`` 同形状的命中。"""

    def __init__(
        self,
        chunk_id: str = "PA601-D54A:0007",
        doc_id: str = "PA601-D54A",
        *,
        page: int = 12,
        section_path: str = "5 > 输出特性 > 5.3 最大输出功率",
        table_id: str | None = "t12-1",
        rrf_score: float = 0.0322,
        text: str = "最大输出功率 120W（含原文，不应进审计库）",
    ) -> None:
        self.chunk_id = chunk_id
        self.doc_id = doc_id
        self.text = text
        self.meta = {"page": page, "section_path": section_path, "table_id": table_id}
        self.rrf_score = rrf_score
        self.legs_hit = ("vector", "keyword")


@dataclass
class StubPlan:
    """查询理解计划替身（QueryPlan 口径的 dataclass，走 ``asdict`` 归一路径）。"""

    raw: str = QUERY
    kind: str = "spec_value"
    intent: str = "查最大输出功率规格值"
    filters: dict[str, Any] = field(default_factory=lambda: {"product_code": "PA601-D54A"})
    needs_rules: bool = True
    degradations: list[dict[str, Any]] = field(default_factory=list)


class StubPlanWithToDict:
    """带 ``to_dict()`` 的计划替身（QueryPlan 真实契约定制路径）。"""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "spec_value", "filters": {"product_code": "PA601-D54A"},
                "needs_rules": False}


def _citations() -> list[Citation]:
    """应答层行级引用（真实 ``answer.Citation`` 对象，非 dict）。"""
    return [
        Citation(
            marker="[1]",
            doc_id="PA601-D54A",
            page=12,
            section_path="5 > 输出特性 > 5.3 最大输出功率",
            table_id="t12-1",
            chunk_id="PA601-D54A:0007",
            score=0.0322,
        ),
        Citation(
            marker="[2]",
            doc_id="PA601-D54A",
            page=13,
            section_path="5 > 输出特性 > 5.4 效率",
            table_id=None,
            chunk_id="PA601-D54A:0009",
            score=0.0188,
        ),
    ]


def _rule_hints() -> list[Any]:
    return [
        SimpleNamespace(rule_id="R-001", statement="效率不得低于标称值。", kind="formula",
                        score=0.42),
    ]


def _degradations() -> list[dict[str, Any]]:
    return [
        {"kind": "llm_unavailable", "detail": "未传入 LLM（llm=None）"},
        {"kind": "context_truncated", "reason": "max_chars", "dropped_items": 1},
    ]


def _record_answer_kwargs(**overrides: Any) -> dict[str, Any]:
    """一次完整应答记录的 kwargs（可覆盖单项）。"""
    kwargs: dict[str, Any] = dict(
        query=QUERY,
        plan_summary=StubPlan(),
        hits=[StubHit()],
        citations=_citations(),
        rule_outcomes=_rule_hints(),
        degradations=_degradations(),
        model="step-3",
        elapsed_ms=1234,
    )
    kwargs.update(overrides)
    return kwargs


def _tamper_metadata(path: Path, answer_id: str, mutate: Any) -> None:
    """直接改 sqlite 里的 metadata JSON（模拟"绕开 API 改库"的篡改）。"""
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT metadata FROM provenance WHERE entity_id = ?", (answer_id,)
        ).fetchone()
        assert row is not None, "审计记录应已落盘"
        payload = json.loads(row[0])
        mutate(payload)
        conn.execute(
            "UPDATE provenance SET metadata = ? WHERE entity_id = ?",
            (json.dumps(payload, ensure_ascii=False), answer_id),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 后端选择（settings.provenance_backend 真正接线）
# --------------------------------------------------------------------------- #
def test_backend_follows_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """不传参时按 ``settings.provenance_backend`` 选 sqlite 并落到 store_dir。"""
    store = tmp_path / "store"
    monkeypatch.setattr(settings, "provenance_backend", "sqlite")
    monkeypatch.setattr(settings, "store_dir", store)

    trail = AuditTrail()
    assert trail.backend == "sqlite"
    assert trail.db_path == store / "provenance.db"
    record = trail.record_answer(query=QUERY)

    assert trail.db_path is not None and trail.db_path.is_file()
    assert trail.verify(record.answer_id)
    trail.close()


def test_memory_backend_touches_no_disk(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """memory 后端不落盘: store_dir 下什么都不建。"""
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(settings, "store_dir", store)

    trail = AuditTrail("memory")
    assert trail.db_path is None
    record = trail.record_answer(query=QUERY)

    assert not (store / "provenance.db").exists()
    assert list(store.iterdir()) == []
    assert trail.get(record.answer_id) is not None
    trail.close()


def test_explicit_backend_wins_over_settings(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """显式参数 > settings（与仓库"显式参数 > 环境变量 > 默认"一致）。"""
    monkeypatch.setattr(settings, "provenance_backend", "memory")
    trail = AuditTrail("sqlite", db_path=tmp_path / "explicit.db")
    assert trail.backend == "sqlite"
    assert trail.db_path == tmp_path / "explicit.db"
    trail.close()


def test_unknown_backend_fails_fast(tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """未知 backend → ValueError，不静默回落到 memory。"""
    with pytest.raises(ValueError, match="provenance_backend"):
        AuditTrail("redis")

    monkeypatch.setattr(settings, "provenance_backend", "postgres")
    with pytest.raises(ValueError, match="postgres"):
        AuditTrail()


def test_backend_value_is_normalized_not_guessed(tmp_path: Path) -> None:
    """大小写/空白归一（" SQLite " == "sqlite"）；仍不猜未登记的后端。"""
    trail = AuditTrail("  SQLite  ", db_path=tmp_path / "norm.db")
    assert trail.backend == "sqlite"
    trail.close()


def test_sqlite_creates_missing_parent_dir(tmp_path: Path) -> None:
    """store_dir 不存在时建目录（与 config.ensure_dirs 同风格）。"""
    db_path = tmp_path / "deep" / "nested" / "provenance.db"
    trail = AuditTrail("sqlite", db_path=db_path)
    record = trail.record_answer(query=QUERY)
    assert db_path.is_file()
    assert trail.verify(record.answer_id)
    trail.close()


def test_unwritable_sqlite_path_raises_loudly(tmp_path: Path) -> None:
    """路径不可写 → ProvenanceError（带原始异常链），不出"无审计"假象。"""
    blocked = tmp_path / "blocked.db"
    blocked.mkdir()  # 用目录占住库路径 → sqlite 打不开

    with pytest.raises(ProvenanceError, match="sqlite 后端不可用") as excinfo:
        AuditTrail("sqlite", db_path=blocked)

    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)


# --------------------------------------------------------------------------- #
# 写入 → 读回 往返
# --------------------------------------------------------------------------- #
def test_answer_roundtrip_memory() -> None:
    """memory 后端: 写入 → 读回 → 校验和一致，载荷字段齐全。"""
    trail = AuditTrail("memory")
    record = trail.record_answer(**_record_answer_kwargs(answer_id="ans-fixed-1"))

    assert record.answer_id == "ans-fixed-1"
    assert record.kind == KIND_ANSWER
    assert record.backend == "memory"
    assert record.checksum and len(record.checksum) == 64

    back = trail.get("ans-fixed-1")
    assert back is not None
    assert back.to_dict() == record.to_dict()
    assert back.query == QUERY
    assert back.plan_summary["filters"] == {"product_code": "PA601-D54A"}
    assert back.model == "step-3"
    assert back.elapsed_ms == 1234
    assert [note["kind"] for note in back.degradations] == ["llm_unavailable",
                                                            "context_truncated"]
    assert [rule["rule_id"] for rule in back.rule_outcomes] == ["R-001"]

    # 行级引用: [n] → 锚点，两条都在
    assert [c["marker"] for c in back.citations] == ["[1]", "[2]"]
    assert back.citations[0]["chunk_id"] == "PA601-D54A:0007"
    assert back.citations[1]["table_id"] is None

    # 命中锚点: doc_id/chunk_id 是 SourceReference 形状
    hit = back.hits[0]
    assert hit["document"] == "PA601-D54A"
    assert hit["page"] == 12
    assert hit["section"] == "5 > 输出特性 > 5.3 最大输出功率"
    assert hit["metadata"]["chunk_id"] == "PA601-D54A:0007"
    assert hit["metadata"]["table_id"] == "t12-1"
    assert hit["metadata"]["score"] == pytest.approx(0.0322)
    assert hit["metadata"]["legs_hit"] == ["vector", "keyword"]

    assert trail.verify("ans-fixed-1")
    assert trail.verify_chain()["valid"]
    trail.close()


def test_answer_roundtrip_sqlite(tmp_path: Path) -> None:
    """sqlite 后端: 同一条记录的写入/读回/校验/哈希链。"""
    trail = AuditTrail("sqlite", db_path=tmp_path / "provenance.db")
    record = trail.record_answer(**_record_answer_kwargs())

    back = trail.get(record.answer_id)
    assert back is not None
    assert back.to_dict() == record.to_dict()
    assert trail.verify(record.answer_id)
    assert trail.verify_chain() == {"valid": True, "total_entries": 1, "broken_links": []}
    trail.close()


def test_hits_keep_anchors_only_not_source_text(tmp_path: Path) -> None:
    """审计库只存锚点，不复制语料原文（正文可按 chunk_id 回取）。"""
    trail = AuditTrail("memory")
    record = trail.record_answer(**_record_answer_kwargs())
    blob = json.dumps(record.to_dict(), ensure_ascii=False)
    assert "PA601-D54A:0007" in blob
    assert "含原文，不应进审计库" not in blob
    trail.close()


def test_duck_typed_inputs_are_normalized() -> None:
    """dict / Mapping / 带 to_dict 的对象都能当输入（不要求具体类型）。"""
    trail = AuditTrail("memory")
    record = trail.record_answer(
        query=QUERY,
        plan_summary={"kind": "spec_value", "filters": {"doc_id": "PA601-D54A"}},
        hits=[{"doc_id": "d1", "chunk_id": "d1:1", "meta": {"page": 3}, "rrf_score": 0.5}],
        citations=[{"marker": "[1]", "doc_id": "d1", "chunk_id": "d1:1", "score": 0.5}],
        rule_outcomes=[{"rule_id": "R-002", "statement": "s", "kind": "k", "score": 1.0}],
        degradations=[{"kind": "no_evidence"}],
    )
    back = trail.get(record.answer_id)
    assert back is not None
    assert back.plan_summary["filters"] == {"doc_id": "PA601-D54A"}
    assert back.hits[0]["document"] == "d1"
    assert back.hits[0]["metadata"]["chunk_id"] == "d1:1"
    assert back.citations[0]["marker"] == "[1]"
    assert back.rule_outcomes[0]["rule_id"] == "R-002"
    assert trail.verify(record.answer_id)
    trail.close()


def test_plan_summary_accepts_to_dict_objects() -> None:
    """plan_summary 收 QueryPlan（带 to_dict）与 ParsedQuery（dataclass）两类对象。"""
    trail = AuditTrail("memory")
    typed = trail.record_answer(query=QUERY, plan_summary=StubPlanWithToDict())
    structured = trail.record_answer(query=QUERY, plan_summary=StubPlan())

    back = trail.get(typed.answer_id)
    assert back is not None and back.plan_summary == StubPlanWithToDict().to_dict()

    back = trail.get(structured.answer_id)
    assert back is not None
    assert back.plan_summary["kind"] == "spec_value"
    assert back.plan_summary["needs_rules"] is True
    assert trail.verify(structured.answer_id)
    trail.close()


def test_extra_payload_is_persisted_and_covered() -> None:
    """extra 扩展位原样入档、参与校验和（改它也能被 verify 发现）。"""
    trail = AuditTrail("memory")
    record = trail.record_answer(query=QUERY, extra={"covered": True, "round": 1})
    assert record.extra == {"covered": True, "round": 1}

    entry = trail.storage.retrieve(record.answer_id)
    assert entry is not None
    entry.metadata["extra"]["round"] = 2
    assert not trail.verify(record.answer_id)
    trail.close()


# --------------------------------------------------------------------------- #
# 校验和（确定性 + 可对账）
# --------------------------------------------------------------------------- #
def test_record_checksum_is_deterministic() -> None:
    """同内容同校验和；改一个字即变（内容级指纹的语义）。"""
    record = ProvenanceRecord(
        answer_id="a", kind=KIND_ANSWER, recorded_at="2026-01-01T00:00:00+00:00",
        backend="memory", query=QUERY, model="step-3", elapsed_ms=10,
    )
    first = compute_record_checksum(record)
    assert first == compute_record_checksum(record)

    record.query = QUERY + "？"
    assert compute_record_checksum(record) != first


def test_record_checksum_ignores_checksum_field_itself() -> None:
    """校验和不自引用: 只覆盖内容字段（否则永远证伪自己）。"""
    record = ProvenanceRecord(
        answer_id="a", kind=KIND_ANSWER, recorded_at="t", backend="memory", query=QUERY,
    )
    without = compute_record_checksum(record)
    record.checksum = without
    assert compute_record_checksum(record) == without


# --------------------------------------------------------------------------- #
# verify 检出任一篡改
# --------------------------------------------------------------------------- #
def test_verify_detects_metadata_tampering_one_character() -> None:
    """memory 后端: metadata 里改一个字 → False（内容级校验层）。"""
    trail = AuditTrail("memory")
    record = trail.record_answer(**_record_answer_kwargs())
    assert trail.verify(record.answer_id)

    entry = trail.storage.retrieve(record.answer_id)
    assert entry is not None
    entry.metadata["query"] = QUERY + "？"
    trail.storage.store(entry)

    assert not trail.verify(record.answer_id)
    trail.close()


def test_verify_detects_metadata_tampering_in_sqlite(tmp_path: Path) -> None:
    """sqlite 后端: 绕开 API 直接改库文件里的一个字符 → False。"""
    db_path = tmp_path / "provenance.db"
    trail = AuditTrail("sqlite", db_path=db_path)
    record = trail.record_answer(**_record_answer_kwargs())
    trail.close()

    _tamper_metadata(db_path, record.answer_id, lambda payload: payload.update(
        {"query": QUERY + "（多一个字）"}
    ))

    reopened = AuditTrail("sqlite", db_path=db_path)
    assert not reopened.verify(record.answer_id)
    # 记录仍然读得出来（结构没坏），只是校验不过——审计可查、可证伪
    back = reopened.get(record.answer_id)
    assert back is not None and back.query == QUERY + "（多一个字）"
    reopened.close()


def test_verify_detects_degradation_tampering(tmp_path: Path) -> None:
    """降级留痕被改同样检出（"没留痕"不能靠改库洗白）。"""
    trail = AuditTrail("memory")
    record = trail.record_answer(**_record_answer_kwargs())

    entry = trail.storage.retrieve(record.answer_id)
    assert entry is not None
    entry.metadata["degradations"] = []
    trail.storage.store(entry)

    assert not trail.verify(record.answer_id)
    trail.close()


def test_verify_detects_provenance_frame_tampering() -> None:
    """PROV-O 框架字段（used_entities/source_document）被改 → 第一层就拦住。"""
    trail = AuditTrail("memory")
    record = trail.record_answer(**_record_answer_kwargs())

    entry = trail.storage.retrieve(record.answer_id)
    assert entry is not None
    entry.used_entities = ["PA601-D54A#PA601-D54A:9999"]
    trail.storage.store(entry)

    assert not trail.verify(record.answer_id)
    trail.close()


def test_verify_unknown_id_is_false_not_raise() -> None:
    """查无此id: get → None、verify → False（"不存在"不是异常）。"""
    trail = AuditTrail("memory")
    assert trail.get("no-such-id") is None
    assert not trail.verify("no-such-id")
    trail.close()


def test_verify_chain_detects_deleted_row(tmp_path: Path) -> None:
    """整行被删 → 哈希链断（靠 verify_chain 的全局视角补上单记录视角的盲区）。"""
    db_path = tmp_path / "provenance.db"
    trail = AuditTrail("sqlite", db_path=db_path)
    first = trail.record_answer(query="第一问", elapsed_ms=1)
    trail.record_answer(query="第二问", elapsed_ms=2)
    assert trail.verify_chain()["valid"]
    trail.close()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DELETE FROM provenance WHERE entity_id = ?", (first.answer_id,))
        conn.commit()
    finally:
        conn.close()

    reopened = AuditTrail("sqlite", db_path=db_path)
    assert not reopened.verify_chain()["valid"]
    reopened.close()


# --------------------------------------------------------------------------- #
# 分层记录 / 列表 / 生命周期
# --------------------------------------------------------------------------- #
def test_retrieval_records_are_layered() -> None:
    """检索层记录与应答层记录同轨分列，列表时间倒序。"""
    trail = AuditTrail("memory")
    retrieval = trail.record_retrieval(
        query=QUERY,
        plan_summary=StubPlan(),
        hits=[StubHit()],
        degradations=[{"kind": "understand_unavailable"}],
        elapsed_ms=42,
    )
    answer = trail.record_answer(**_record_answer_kwargs(elapsed_ms=99))

    assert retrieval.kind == KIND_RETRIEVAL
    assert retrieval.citations == [] and retrieval.rule_outcomes == []
    back = trail.get(retrieval.answer_id)
    assert back is not None and back.entity_type == "matrag_retrieval"
    assert back.degradations[0]["kind"] == "understand_unavailable"

    recent = trail.list_recent()
    assert [record.answer_id for record in recent] == [answer.answer_id,
                                                       retrieval.answer_id]
    assert [r.kind for r in recent] == [KIND_ANSWER, KIND_RETRIEVAL]
    assert trail.list_recent(limit=1) == [answer]
    trail.close()


def test_list_recent_validates_limit() -> None:
    """limit 必须 >= 1 的整数（不许悄悄返回全量）。"""
    trail = AuditTrail("memory")
    for bad in (0, -1, True, "5", 1.0):
        with pytest.raises(ValueError, match="limit"):
            trail.list_recent(bad)
    trail.close()


def test_repeated_id_keeps_version_history() -> None:
    """同一 id 重复记录走版本化归档: 取到最新版，历史行不进列表、链仍完整。"""
    trail = AuditTrail("memory")
    first = trail.record_answer(query=QUERY, elapsed_ms=1, answer_id="ans-x")
    second = trail.record_answer(query=QUERY + "（订正）", elapsed_ms=2, answer_id="ans-x")

    latest = trail.get("ans-x")
    assert latest is not None and latest.query == QUERY + "（订正）"
    assert latest.elapsed_ms == 2
    assert [record.answer_id for record in trail.list_recent()] == ["ans-x"]
    assert trail.verify("ans-x")
    assert trail.verify_chain()["valid"]
    assert first.answer_id == second.answer_id == "ans-x"
    trail.close()


def test_close_is_idempotent_and_blocks_writes_only() -> None:
    """close() 幂等; 关闭后写入显式失败，已记录内容仍可查可验。"""
    trail = AuditTrail("memory")
    record = trail.record_answer(query=QUERY)

    trail.close()
    trail.close()  # 幂等
    assert trail.closed

    with pytest.raises(ProvenanceError, match="已关闭"):
        trail.record_answer(query=QUERY)
    with pytest.raises(ProvenanceError, match="已关闭"):
        trail.record_retrieval(query=QUERY)

    # 关闭不销毁审计: 读和验仍然可用
    assert trail.verify(record.answer_id)
    assert trail.get(record.answer_id) is not None
    assert trail.list_recent() == [record]


def test_context_manager_records_and_closes() -> None:
    """with 语法: 退出即关，块内写、块外读。"""
    with AuditTrail("memory") as trail:
        record = trail.record_answer(query=QUERY)
    assert trail.closed
    assert trail.verify(record.answer_id)
    with pytest.raises(ProvenanceError, match="已关闭"):
        with trail:
            pass


def test_corrupted_payload_raises_on_read() -> None:
    """载荷结构坏（kind 非法/缺 answer_id）→ 读即 ProvenanceError，不假装没有。"""
    trail = AuditTrail("memory")
    record = trail.record_answer(query=QUERY)
    entry = trail.storage.retrieve(record.answer_id)
    assert entry is not None
    entry.metadata = {"kind": "bogus"}  # 模拟结构被改坏
    trail.storage.store(entry)

    with pytest.raises(ProvenanceError, match="载荷不可解析"):
        trail.get(record.answer_id)
    assert not trail.verify(record.answer_id)
    with pytest.raises(ProvenanceError, match="载荷不可解析"):
        trail.list_recent()
    trail.close()


# --------------------------------------------------------------------------- #
# 输入校验（fail-fast）
# --------------------------------------------------------------------------- #
def test_invalid_inputs_are_rejected() -> None:
    """空 query / 非法 elapsed_ms / 空 answer_id → ValueError，且什么都不落。"""
    trail = AuditTrail("memory")
    with pytest.raises(ValueError, match="query"):
        trail.record_answer(query="   ")
    with pytest.raises(ValueError, match="elapsed_ms"):
        trail.record_answer(query=QUERY, elapsed_ms=-1)
    with pytest.raises(ValueError, match="elapsed_ms"):
        trail.record_answer(query=QUERY, elapsed_ms=True)
    with pytest.raises(ValueError, match="answer_id"):
        trail.record_answer(query=QUERY, answer_id="  ")
    assert trail.list_recent() == []
    trail.close()


def test_unserializable_payload_is_rejected() -> None:
    """载荷不可 JSON 序列化 → TypeError（不把脏数据腌进审计库）。"""
    trail = AuditTrail("memory")
    with pytest.raises(TypeError, match="可 JSON 序列化"):
        trail.record_answer(query=QUERY, extra={"obj": object()})
    with pytest.raises(TypeError, match="plan_summary"):
        trail.record_answer(query=QUERY, plan_summary=42)
    with pytest.raises(TypeError, match="hits"):
        trail.record_answer(query=QUERY, hits="PA601-D54A:0007")
    assert trail.list_recent() == []
    trail.close()


def test_export_prov_turtle_ntriples_jsonld() -> None:
    """PROV-O 导出: 实体 id 保留、jsonld 可解析、格式大小写不敏感、非法格式 fail-fast。"""
    trail = AuditTrail("memory")
    trail.record_answer(query=QUERY, answer_id="answer:export-1")
    turtle = trail.export_prov()
    assert "answer:export-1" in turtle
    assert "http://www.w3.org/ns/prov#" in turtle
    ntriples = trail.export_prov(format="ntriples")
    assert "answer:export-1" in ntriples
    assert "record_answer" in ntriples
    parsed = json.loads(trail.export_prov(format="JSONLD"))  # 大小写不敏感
    assert parsed
    with pytest.raises(ValueError, match="导出格式"):
        trail.export_prov(format="xml")
    trail.close()
