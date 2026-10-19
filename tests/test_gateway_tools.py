"""P1-6 tools 测试: 模块级直调 + FastMCP in-process client（全程离线）。

- fake retriever 返回带 anchor 所需的 meta 的假 hits（不依赖 P1-4/P1-5 接线）;
- MCP 层用 ``mcp.shared.memory.create_connected_server_and_client_session``
  起 in-process client（不起端口、不走网络）。
- 失败形态: retriever_unconfigured / retriever_unavailable / retrieve_failed /
  not_found / empty_query / bad_top_k 全部断言结构化 error。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from matrag import answer as answer_mod
from matrag.answer import AnswerResult, Citation
from matrag.gateway import tools as gw_tools
from matrag.provenance import AuditTrail, ProvenanceError

MD_DOC = """# 规格书 A

## 1 范围

描述过压保护。

## 2 电特性

**表2-1 输出特性**

| 项目 | 限值 |
| --- | --- |
| 输出电压 | 5V |
"""

TABLES_DOC = [
    {
        "id": "t0-0",
        "caption": "表2-1 输出特性",
        "headers": ["项目", "限值"],
        "rows": [["输出电压", "5V"]],
        "marks": [],
        "confidence": 1.0,
        "source": {"page": 2, "section_path": "2 电特性", "provider": "local"},
    }
]

REPORT_DOC = {
    "doc_id": "spec-a",
    "source_path": "docs/spec-a.pdf",
    "provider": "local",
    "ok": True,
    "tables": [{"table_id": "t0-0", "page": 2, "n_rows": 1, "n_cols": 2,
                "merged_cells": 0, "flags": [], "note": "clean"}],
    "unresolved": [],
    "stats": {"n_tables": 1},
}


class FakeRetriever:
    """替身检索器: 契约与 ``HybridRetriever`` 对齐（retrieve/indexed_doc_ids/last_plan/degradations）。"""

    def __init__(
        self,
        *,
        hits: list[Any] | None = None,
        indexed: list[str] | None = None,
        raise_on_retrieve: Exception | None = None,
        expose_indexed: bool = True,
    ) -> None:
        self._hits = hits if hits is not None else [self._hit("spec-a:0001")]
        self._indexed = set(indexed) if indexed is not None else {"spec-a"}
        self._raise = raise_on_retrieve
        self._expose_indexed = expose_indexed
        self.calls: list[tuple[str, int]] = []
        self.last_plan: dict[str, Any] = {"queries": [], "legs": ["vector", "keyword"]}
        self.degradations: list[dict[str, Any]] = []

    @staticmethod
    def _hit(chunk_id: str, *, table_id: str | None = None) -> Any:
        from types import SimpleNamespace

        meta: dict[str, Any] = {"kind": "table_row", "page": 2, "section_path": "2 电特性"}
        if table_id:
            meta["table_id"] = table_id
        return SimpleNamespace(
            chunk_id=chunk_id,
            doc_id="spec-a",
            text="表2-1 输出特性 | 项目 | 限值",
            meta=meta,
            rrf_score=0.0322,
            per_leg={"vector": 0.0164, "keyword": 0.0158},
            legs_hit=("vector", "keyword"),
        )

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 10,
        where: dict[str, Any] | None = None,
        legs: Any = None,
    ) -> list[Any]:
        self.calls.append((query, top_k))
        self.last_plan = {"queries": [query], "legs": ["vector", "keyword"]}
        if self._raise is not None:
            raise self._raise
        return self._hits[:top_k]

    def indexed_doc_ids(self) -> set[str]:
        if not self._expose_indexed:
            raise AttributeError("不应被调用: 该替身不暴露 indexed_doc_ids")
        return set(self._indexed)


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """假语料: spec-a 一份完整文档（md + tables.json + 报告）。

    目录同时通过 monkeypatch 挂到 settings，让默认参数（corpus_dir=None）的
    直调路径也打到假语料; monkeypatch 负责还原，不泄漏给其它测试。
    """
    base = tmp_path / "corpus"
    base.mkdir()
    (base / "spec-a.md").write_text(MD_DOC, encoding="utf-8")
    (base / "spec-a.tables.json").write_text(
        json.dumps(TABLES_DOC, ensure_ascii=False), encoding="utf-8"
    )
    (base / "spec-b.tables.json").write_text("[]", encoding="utf-8")  # 未入索引的半成品
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "spec-a.parse_report.json").write_text(
        json.dumps(REPORT_DOC, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(gw_tools.settings, "corpus_dir", base)
    monkeypatch.setattr(gw_tools.settings, "reports_dir", reports)
    return base


# --------------------------------------------------------------------------- #
# 模块级直调
# --------------------------------------------------------------------------- #
def test_ping_payload() -> None:
    """ping: ok/service/version 三键齐备。"""
    payload = gw_tools.ping()
    assert payload["ok"] is True
    assert payload["service"] == "matrag"
    assert payload["version"]


def test_list_documents_with_index_state(corpus: Path) -> None:
    """docs 列出全部语料文档; unindexed = 有语料无索引（spec-b）。"""
    payload = gw_tools.list_documents(
        retriever_factory=lambda: FakeRetriever(indexed=["spec-a"]), corpus_dir=corpus
    )
    assert [d["doc_id"] for d in payload["docs"]] == ["spec-a", "spec-b"]
    assert payload["docs"][0]["md_present"] is True
    assert payload["docs"][1]["md_present"] is False
    assert payload["unindexed"] == ["spec-b"]
    assert payload["index_state"] == gw_tools.INDEX_OK
    assert "retriever" not in payload


def test_list_documents_index_unknown(corpus: Path) -> None:
    """检索器不暴露 doc 清单钩子 → index_state=unknown，unindexed 恒空（不猜）。"""

    class BareRetriever:
        """只有 retrieve 的最小检索器（无 indexed_doc_ids 钩子）。"""

        def retrieve(self, query: str, *, top_k: int = 10, **_: Any) -> list[Any]:
            return []

    payload = gw_tools.list_documents(retriever_factory=BareRetriever, corpus_dir=corpus)
    assert payload["index_state"] == gw_tools.INDEX_UNKNOWN
    assert payload["unindexed"] == []


def test_list_documents_retriever_unavailable(corpus: Path) -> None:
    """factory 炸裂 → 索引状态 unavailable + detail 留痕; 文档列举不阻塞。"""

    def boom() -> Any:
        raise RuntimeError("维度探测失败")

    payload = gw_tools.list_documents(retriever_factory=boom, corpus_dir=corpus)
    assert payload["index_state"] == gw_tools.INDEX_UNAVAILABLE
    assert payload["retriever"] == {
        "error": "retriever_unavailable",
        "detail": "RuntimeError: 维度探测失败",
    }
    assert [d["doc_id"] for d in payload["docs"]] == ["spec-a", "spec-b"]


def test_search_hits_with_anchors(corpus: Path) -> None:
    """search: hit 带引用锚点 + explain 带 plan/degradations。"""
    payload = gw_tools.search_context(
        "过压保护限值",
        top_k=3,
        retriever_factory=lambda: FakeRetriever(hits=[FakeRetriever._hit("spec-a:0002", table_id="t0-0")]),
    )
    assert payload["query"] == "过压保护限值"
    assert payload["top_k"] == 3
    hit = payload["hits"][0]
    assert hit["chunk_id"] == "spec-a:0002"
    assert hit["doc_id"] == "spec-a"
    assert hit["score"] == pytest.approx(0.0322)
    assert hit["legs_hit"] == ["vector", "keyword"]
    assert hit["anchor"] == {
        "doc_id": "spec-a",
        "page": 2,
        "section_path": "2 电特性",
        "table_id": "t0-0",
        "chunk_id": "spec-a:0002",
    }
    assert payload["explain"]["last_plan"]["queries"] == ["过压保护限值"]
    assert payload["explain"]["degradations"] == []


def test_search_top_k_forwarded(corpus: Path) -> None:
    """top_k 透传给 retriever.retrieve（且上限外的值在工具层即被拒）。"""
    retriever = FakeRetriever()
    gw_tools.search_context("q", top_k=5, retriever_factory=lambda: retriever)
    assert retriever.calls == [("q", 5)]


def test_search_unconfigured(corpus: Path) -> None:
    """factory 返回 None（未接线）→ retriever_unconfigured，不静默给空结果。"""
    payload = gw_tools.search_context("q", retriever_factory=lambda: None)
    assert payload == {"error": "retriever_unconfigured"}


def test_search_unavailable(corpus: Path) -> None:
    """factory 抛错 → retriever_unavailable + detail。"""

    def boom() -> Any:
        raise ValueError("索引库损坏")

    payload = gw_tools.search_context("q", retriever_factory=boom)
    assert payload["error"] == "retriever_unavailable"
    assert payload["detail"] == "ValueError: 索引库损坏"


def test_search_retrieve_failed(corpus: Path) -> None:
    """检索执行异常 → retrieve_failed + detail（不把异常抛给传输层）。"""
    payload = gw_tools.search_context(
        "q", retriever_factory=lambda: FakeRetriever(raise_on_retrieve=OSError("db locked"))
    )
    assert payload["error"] == "retrieve_failed"
    assert payload["detail"] == "OSError: db locked"


@pytest.mark.parametrize("top_k", [0, -1, True, "5", 10**6])
def test_search_bad_top_k(corpus: Path, top_k: Any) -> None:
    """非法 top_k → bad_top_k（不去打检索器）。"""
    payload = gw_tools.search_context("q", top_k=top_k, retriever_factory=lambda: FakeRetriever())
    assert payload["error"] == "bad_top_k"
    assert "detail" in payload


def test_search_empty_query(corpus: Path) -> None:
    """空查询 → empty_query（不去打检索器）。"""
    payload = gw_tools.search_context("   ", retriever_factory=lambda: FakeRetriever())
    assert payload == {"error": "empty_query"}


# --------------------------------------------------------------------------- #
# ask（应答面）: 模块级直调
# --------------------------------------------------------------------------- #
@pytest.fixture
def no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """打桩 LLM 解析: ``ask`` 走确定性模板, 全程不触网（也不依赖 live 配置）。"""
    monkeypatch.setattr(gw_tools, "_resolve_llm", lambda: None)


def _fake_result() -> AnswerResult:
    return AnswerResult(
        answer="输出电压限值为 5V [1]。",
        citations=[
            Citation(marker="[1]", doc_id="spec-a", page=2, section_path="2 电特性",
                     table_id="t2-0", chunk_id="spec-a:0001", score=0.0322)
        ],
        steps=["装配上下文证据 1 条"],
        rule_hints=[{"rule_id": "R-001", "statement": "输出电压应满足 5V。",
                     "kind": "formula", "score": 0.5}],
        covered=True,
        degradations=[],
    )


def test_ask_with_fake_answer_dependency(monkeypatch: pytest.MonkeyPatch, no_llm: None) -> None:
    """ask: 替身 answer 依赖被正确调用, 返回七个数据键 + audit 审计摘要。"""
    seen: dict[str, Any] = {}

    def fake_answer_question(query: str, **kwargs: Any) -> AnswerResult:
        seen.update({"query": query, **kwargs})
        return _fake_result()

    monkeypatch.setattr(answer_mod, "answer_question", fake_answer_question)
    retriever = FakeRetriever()
    payload = gw_tools.ask(
        "输出电压限值", 4,
        retriever_factory=lambda: retriever,
        audit_factory=lambda: AuditTrail("memory"),
    )

    assert set(payload) == {
        "answer", "citations", "steps", "rule_hints", "evaluations", "covered", "degradations",
        "audit",
    }
    assert payload["answer"].startswith("输出电压限值")
    assert payload["citations"][0]["chunk_id"] == "spec-a:0001"
    assert payload["rule_hints"][0]["rule_id"] == "R-001"
    assert payload["covered"] is True
    assert payload["audit"]["ok"] is True
    assert str(payload["audit"]["answer_id"]).startswith("answer:")
    assert seen["query"] == "输出电压限值"
    assert seen["top_k"] == 4
    assert seen["retriever"] is retriever
    assert seen["llm"] is None


def test_ask_without_llm_reports_insufficient(corpus: Path, no_llm: None) -> None:
    """ask: 无 LLM 时不返空答案, 走确定性模板并留痕 llm_unavailable（审计照落档）。"""
    trail = AuditTrail("memory")
    payload = gw_tools.ask(
        "输出电压限值", 2, retriever_factory=lambda: FakeRetriever(
            hits=[FakeRetriever._hit("spec-a:0001", table_id="t2-0")]
        ),
        audit_factory=lambda: trail,
    )
    assert payload["answer"].strip()
    assert payload["covered"] is False
    assert any(n["kind"] == "llm_unavailable" for n in payload["degradations"])
    assert payload["citations"][0]["marker"] == "[1]"
    assert any("检索到" in step for step in payload["steps"])
    assert payload["audit"]["ok"] is True
    record = trail.list_recent(1)[0].to_dict()
    assert record["query"] == "输出电压限值"
    assert any(n["kind"] == "llm_unavailable" for n in record["degradations"])


def test_ask_unconfigured(corpus: Path) -> None:
    """factory 返回 None（未接线）→ retriever_unconfigured（与 search_context 同形态）。"""
    assert gw_tools.ask("q", retriever_factory=lambda: None) == {"error": "retriever_unconfigured"}


def test_ask_unavailable(corpus: Path) -> None:
    """factory 抛错 → retriever_unavailable + detail。"""

    def boom() -> Any:
        raise ValueError("索引库损坏")

    payload = gw_tools.ask("q", retriever_factory=boom)
    assert payload["error"] == "retriever_unavailable"
    assert payload["detail"] == "ValueError: 索引库损坏"


def test_ask_empty_query_and_bad_top_k(corpus: Path) -> None:
    """空查询 / 非法 top_k → 结构化 error（不去打检索器）。"""
    assert gw_tools.ask("  ", retriever_factory=lambda: FakeRetriever()) == {"error": "empty_query"}
    payload = gw_tools.ask("q", top_k=0, retriever_factory=lambda: FakeRetriever())
    assert payload["error"] == "bad_top_k"


def test_ask_failed(monkeypatch: pytest.MonkeyPatch, no_llm: None) -> None:
    """应答执行异常 → answer_failed + detail（不把异常抛给传输层）。"""

    def boom(query: str, **kwargs: Any) -> AnswerResult:
        raise RuntimeError("检索器炸了")

    monkeypatch.setattr(answer_mod, "answer_question", boom)
    payload = gw_tools.ask("q", retriever_factory=lambda: FakeRetriever())
    assert payload["error"] == "answer_failed"
    assert payload["detail"] == "RuntimeError: 检索器炸了"


def test_ask_audit_records_reasoning_chain(monkeypatch: pytest.MonkeyPatch, no_llm: None) -> None:
    """ask: 审计落档含规则结果/引用/校验和, verify(answer_id) 通过（确定性推理留痕）。"""
    monkeypatch.setattr(answer_mod, "answer_question", lambda q, **kw: _fake_result())
    trail = AuditTrail("memory")
    payload = gw_tools.ask(
        "输出电压限值", 3,
        retriever_factory=lambda: FakeRetriever(),
        audit_factory=lambda: trail,
    )
    assert payload["audit"]["ok"] is True
    assert payload["audit"]["backend"] == "memory"
    record = trail.list_recent(1)[0]
    data = record.to_dict()
    assert data["rule_outcomes"][0]["rule_id"] == "R-001"
    assert data["citations"][0]["marker"] == "[1]"
    assert data["extra"]["covered"] is True
    assert trail.verify(data["answer_id"]) is True


def test_ask_audit_unavailable_visible(monkeypatch: pytest.MonkeyPatch, no_llm: None) -> None:
    """审计轨不可用（ProvenanceError）→ audit.ok=false + provenance_unavailable 留痕, 应答不受阻。"""
    monkeypatch.setattr(answer_mod, "answer_question", lambda q, **kw: _fake_result())

    def boom() -> Any:
        raise ProvenanceError("后端不可用")

    payload = gw_tools.ask(
        "输出电压限值", 2,
        retriever_factory=lambda: FakeRetriever(),
        audit_factory=boom,
    )
    assert payload["answer"].strip()
    assert payload["audit"]["ok"] is False
    assert "ProvenanceError" in payload["audit"]["error"]
    assert any(n["kind"] == "provenance_unavailable" for n in payload["degradations"])


def test_ask_audit_write_failed_visible(monkeypatch: pytest.MonkeyPatch, no_llm: None) -> None:
    """审计写入炸裂（非 ProvenanceError）→ provenance_failed 留痕, 应答不受阻。"""
    monkeypatch.setattr(answer_mod, "answer_question", lambda q, **kw: _fake_result())

    def boom() -> Any:
        raise RuntimeError("磁盘满")

    payload = gw_tools.ask(
        "输出电压限值", 2,
        retriever_factory=lambda: FakeRetriever(),
        audit_factory=boom,
    )
    assert payload["answer"].strip()
    assert payload["audit"]["ok"] is False
    assert any(n["kind"] == "provenance_failed" for n in payload["degradations"])


def test_ask_audit_layer_missing_visible(monkeypatch: pytest.MonkeyPatch, no_llm: None) -> None:
    """审计层导入失败 → provenance_unavailable 留痕, 应答照常（旁路不阻断）。"""
    monkeypatch.setattr(answer_mod, "answer_question", lambda q, **kw: _fake_result())
    monkeypatch.setitem(sys.modules, "matrag.provenance", None)
    payload = gw_tools.ask("输出电压限值", 2, retriever_factory=lambda: FakeRetriever())
    assert payload["answer"].strip()
    assert payload["audit"]["ok"] is False
    assert any(n["kind"] == "provenance_unavailable" for n in payload["degradations"])


def test_get_table_and_outline_and_report(corpus: Path) -> None:
    """get_table/doc_outline/parse_report 三件套直调。"""
    table = gw_tools.get_table("spec-a", "t0-0", corpus_dir=corpus)
    assert table["table"]["caption"] == "表2-1 输出特性"
    assert gw_tools.get_table("spec-a", "nope", corpus_dir=corpus)["error"] == "not_found"

    outline = gw_tools.doc_outline("spec-a", corpus_dir=corpus)
    assert outline["title"] == "规格书 A"
    assert outline["tables"][0]["page"] == 2
    assert gw_tools.doc_outline("spec-b", corpus_dir=corpus)["error"] == "md_not_found"

    report = gw_tools.parse_report("spec-a", reports_dir=corpus.parent / "reports")
    assert report["report"]["ok"] is True
    assert gw_tools.parse_report("spec-b", reports_dir=corpus.parent / "reports")["error"] == "not_found"


# --------------------------------------------------------------------------- #
# in-process MCP client
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_mcp_in_process_tools(corpus: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """经 in-process client 调全部 7 个工具，断言工具名与返回结构。"""
    monkeypatch.setattr(gw_tools, "_resolve_llm", lambda: None)
    app = FastMCP(name="matrag-test")
    gw_tools.register_tools(
        app,
        retriever_factory=lambda: FakeRetriever(
            hits=[FakeRetriever._hit("spec-a:0003", table_id="t0-0")], indexed=["spec-a"]
        ),
        corpus_dir=corpus,
        reports_dir=corpus.parent / "reports",
        audit_factory=lambda: AuditTrail("memory"),
    )
    async with create_connected_server_and_client_session(app) as session:
        listed = sorted(t.name for t in (await session.list_tools()).tools)
        assert listed == [
            "ask",
            "doc_outline",
            "get_table",
            "list_documents",
            "parse_report",
            "ping",
            "search_context",
        ]

        ping = await session.call_tool("ping", {})
        assert ping.structuredContent["ok"] is True

        docs = await session.call_tool("list_documents", {})
        assert [d["doc_id"] for d in docs.structuredContent["docs"]] == ["spec-a", "spec-b"]
        assert docs.structuredContent["unindexed"] == ["spec-b"]

        search = await session.call_tool("search_context", {"query": "过压保护", "top_k": 2})
        assert search.structuredContent["hits"][0]["anchor"]["table_id"] == "t0-0"
        assert search.isError is False

        asked = await session.call_tool("ask", {"query": "输出电压限值", "top_k": 2})
        assert asked.structuredContent["citations"][0]["marker"] == "[1]"
        assert asked.structuredContent["covered"] is False  # 无 LLM → 证据不足且不留空
        assert asked.structuredContent["answer"].strip()
        assert asked.structuredContent["audit"]["ok"] is True
        assert asked.isError is False

        table = await session.call_tool("get_table", {"doc_id": "spec-a", "table_id": "t0-0"})
        assert table.structuredContent["table"]["headers"] == ["项目", "限值"]

        outline = await session.call_tool("doc_outline", {"doc_id": "spec-a"})
        assert outline.structuredContent["title"] == "规格书 A"

        report = await session.call_tool("parse_report", {"doc_id": "spec-a"})
        assert report.structuredContent["report"]["stats"] == {"n_tables": 1}


@pytest.mark.asyncio
async def test_mcp_in_process_search_unconfigured(corpus: Path) -> None:
    """retriever 未接线: MCP 调用也返回结构化 retriever_unconfigured（非异常）。"""
    app = FastMCP(name="matrag-test")
    gw_tools.register_tools(app, retriever_factory=lambda: None, corpus_dir=corpus)
    async with create_connected_server_and_client_session(app) as session:
        res = await session.call_tool("search_context", {"query": "过压保护"})
        assert res.structuredContent == {"error": "retriever_unconfigured"}
        assert res.isError is False


@pytest.mark.asyncio
async def test_mcp_in_process_ask_unconfigured(corpus: Path) -> None:
    """retriever 未接线: MCP 调 ask 也返回结构化 retriever_unconfigured（非异常）。"""
    app = FastMCP(name="matrag-test")
    gw_tools.register_tools(app, retriever_factory=lambda: None, corpus_dir=corpus)
    async with create_connected_server_and_client_session(app) as session:
        res = await session.call_tool("ask", {"query": "过压保护"})
        assert res.structuredContent == {"error": "retriever_unconfigured"}
        assert res.isError is False


@pytest.mark.asyncio
async def test_mcp_in_process_not_found(corpus: Path) -> None:
    """不存在的 doc/table → 结构化 not_found（非异常）。"""
    app = FastMCP(name="matrag-test")
    gw_tools.register_tools(app, retriever_factory=lambda: None, corpus_dir=corpus)
    async with create_connected_server_and_client_session(app) as session:
        res = await session.call_tool("get_table", {"doc_id": "spec-a", "table_id": "nope"})
        assert res.structuredContent["error"] == "not_found"
        res2 = await session.call_tool("doc_outline", {"doc_id": "spec-b"})
        assert res2.structuredContent["error"] == "md_not_found"
