"""G2: gateway ``ask`` 响应 citations 携带 ``text`` 原文片段（全离线）。

背景: ``ask`` 旧契约的 citation 只有 ``section_path`` 等锚点, 没有原文片段,
下游（matrag-agent ``rag_search``）只能退回小节路径。本模块验证修复后每条引用
自带真实原文窗口:

- 每个 citation 含非空 ``text``;
- ``text`` 取自命中 chunk 原文, 与 ``section_path`` 章节一致（不是路径回退）;
- ``text`` 长度有限（≤ :data:`CITATION_TEXT_MAX_CHARS`）;
- 多条 citation 各自的 ``text`` 不同;
- 关键词在窗口里居中（定位到 query token）;
- 全程离线（不触网、不调真实 LLM）;
- 向后兼容: 七个旧键仍在, 旧消费者用 ``.get()`` 缺省值不破坏。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from matrag.answer import CITATION_TEXT_MAX_CHARS, build_context
from matrag.gateway import tools as gw_tools
from matrag.provenance import AuditTrail


def _hit(
    chunk_id: str,
    text: str,
    *,
    doc_id: str = "spec-a",
    section: str = "4.3 输出特性",
    page: int = 2,
    score: float = 0.0322,
) -> SimpleNamespace:
    """构造一条同 ``RetrievalHit`` 形状的命中（鸭子类型, 无需真实索引）。"""
    return SimpleNamespace(
        chunk_id=chunk_id,
        doc_id=doc_id,
        text=text,
        meta={"page": page, "section_path": section},
        rrf_score=score,
        per_leg={"vector": score / 2, "keyword": score / 2},
        legs_hit=("vector", "keyword"),
    )


class FakeRetriever:
    """替身检索器: 只实现 ``ask`` 需要的 ``retrieve`` 契约（离线）。"""

    def __init__(self, hits: list[Any]) -> None:
        self._hits = hits
        self.last_plan: dict[str, Any] = {"queries": [], "legs": ["vector", "keyword"]}
        self.degradations: list[dict[str, Any]] = []

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 10,
        where: Any = None,
        legs: Any = None,
    ) -> list[Any]:
        return self._hits[:top_k]


@pytest.fixture(autouse=True)
def _offline_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制离线: ``ask`` 的 LLM 解析恒为 ``None``（走确定性模板, 不触网）。"""
    monkeypatch.setattr(gw_tools, "_resolve_llm", lambda: None)


def _ask(
    hits: list[Any],
    query: str = "输出电压是多少",
    top_k: int = 5,
) -> dict[str, Any]:
    """经 gateway ``ask`` 跑一次应答（审计轨用内存后端, 不落盘）。"""
    return gw_tools.ask(
        query,
        top_k,
        retriever_factory=lambda: FakeRetriever(hits),
        audit_factory=lambda: AuditTrail("memory"),
    )


def test_ask_citations_carry_text() -> None:
    """每个 citation 都带非空 ``text`` 字段。"""
    payload = _ask([_hit("spec-a:0001", "输出电压限值 5V，过压保护点 6V。")])
    assert "error" not in payload
    citations = payload["citations"]
    assert citations, "至少应装入一条引用"
    for citation in citations:
        assert "text" in citation
        assert citation["text"], "text 不应为空"


def test_citation_text_reflects_chunk_original() -> None:
    """``text`` 是命中 chunk 原文, 与 ``section_path`` 章节一致（不是路径回退）。"""
    payload = _ask([_hit("spec-a:0001", "输出电压限值 5V。", section="4.3 输出特性")])
    citation = payload["citations"][0]
    assert citation["text"] == "输出电压限值 5V。"
    assert citation["section_path"] == "4.3 输出特性"


def test_citation_text_length_bounded() -> None:
    """超长原文被裁成 ≤ ``CITATION_TEXT_MAX_CHARS`` 的窗口。"""
    long_text = "过压" + "字" * 2000
    payload = _ask([_hit("spec-a:0001", long_text)], query="过压")
    citation = payload["citations"][0]
    assert 0 < len(citation["text"]) <= CITATION_TEXT_MAX_CHARS


def test_multiple_citations_have_distinct_text() -> None:
    """多条 citation 各自的 ``text`` 不同（逐条原文, 非共用）。"""
    payload = _ask(
        [
            _hit("spec-a:0001", "第一条证据原文 ABC。", section="4.3 输出特性"),
            _hit("spec-a:0002", "第二条证据原文 XYZ。", section="4.4 输入特性", score=0.02),
        ]
    )
    citations = payload["citations"]
    assert len(citations) == 2
    assert citations[0]["text"] != citations[1]["text"]


def test_citation_text_window_centers_keyword() -> None:
    """长原文里关键词被居中保留（窗口定位到 query token）。"""
    filler = "无关内容" * 200  # 前后各 800 字填充, 远超窗口
    text = filler + "过压保护点 6V" + filler
    _, citations = build_context([_hit("spec-a:0001", text)], query="过压保护点")
    cited = citations[0].text
    assert "过压保护点 6V" in cited
    assert len(cited) <= CITATION_TEXT_MAX_CHARS


def test_ask_text_path_is_offline_deterministic() -> None:
    """无 LLM 时不触网: 走确定性模板 + ``llm_unavailable`` 留痕, 仍带 text。"""
    payload = _ask([_hit("spec-a:0001", "输出电压限值 5V。")])
    assert payload["covered"] is False
    assert any(note["kind"] == "llm_unavailable" for note in payload["degradations"])
    assert payload["citations"][0]["text"] == "输出电压限值 5V。"


def test_citation_payload_backward_compatible() -> None:
    """向后兼容: 七个旧键仍在; 旧消费者用 ``.get()`` 缺省值不破坏。"""
    payload = _ask([_hit("spec-a:0001", "输出电压限值 5V。", section="4.3 输出特性")])
    citation = payload["citations"][0]
    for key in ("marker", "doc_id", "page", "section_path", "table_id", "chunk_id", "score"):
        assert key in citation
    # 模拟旧消费者的取值: 有 text 就用, 没有才退回 section_path
    assert citation.get("text", "") == "输出电压限值 5V。"
    legacy = citation.get("raw_text", "") or citation.get("section_path", "")
    assert legacy == "4.3 输出特性"
