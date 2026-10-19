"""多查询变体融合检索 + 查询计划驱动检索测试（``matrag.retrieve.retriever``）。

覆盖: 两变体结果跨腿/跨变体融合去重、per_leg 跨变体累加、单变体空 → variant_empty
留痕不退化为异常、plan_meta 挂载、plan=None 走旧路径（零回归）、plan 的
retrieval_queries 缺失 → plan_fallback、plan.filters 合并 where（调用方优先）、
plan.degradations 带 source="plan" 追加、needs_rules → last_rule_hints 非空
（用 fake 规则集）、规则提示不可用只留痕、understand 惰性导入失败 →
understand_unavailable 且降级为单变体检索、变体去重保序、参数 fail-fast。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from matrag.domain.rules_loader import DomainRule
from matrag.retrieve import retriever as retriever_module
from matrag.retrieve.query import reset_lexicon_cache
from matrag.retrieve.retriever import (
    FUSION_K,
    OVERFETCH,
    HybridRetriever,
    RetrievalHit,
)

# --------------------------------------------------------------------------- fakes

CHUNKS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "c1": ("PA601-D54A", "输出电压 12V", {"page": 5}),
    "c2": ("PA601-D54A", "最大输出功率 600W", {"page": 6}),
    "c3": ("CY0177-75V-20A", "效率 93%", {"page": 8}),
    "c4": ("GDC800T5054-UC", "保持时间 20ms", {"page": 9}),
}


def _chunk(chunk_id: str, leg: str) -> SimpleNamespace:
    doc_id, text, meta = CHUNKS[chunk_id]
    return SimpleNamespace(
        chunk_id=chunk_id, doc_id=doc_id, text=text, meta=dict(meta), score=0.5, leg=leg
    )


class RecordingVector:
    """记录每次调用的向量腿索引（向量即查询文本长度, 便于按变体区分）。"""

    def __init__(self, orders: dict[int, list[str]] | None = None) -> None:
        self.orders = orders or {}
        self.calls: list[dict[str, Any]] = []

    def search_vector(
        self, vector: list[float], *, top_k: int = 10, where: dict[str, Any] | None = None
    ) -> list[SimpleNamespace]:
        self.calls.append({"vector": list(vector), "top_k": top_k, "where": where})
        order = self.orders.get(int(vector[0]), [])
        return [_chunk(cid, "vector") for cid in order[:top_k]]


class RecordingKeyword:
    """记录每次调用的关键词腿索引。"""

    def __init__(self, orders: dict[str, list[str]] | None = None) -> None:
        self.orders = orders or {}
        self.calls: list[dict[str, Any]] = []

    def search_keyword(
        self, query: str, *, top_k: int = 10, where: dict[str, Any] | None = None
    ) -> list[SimpleNamespace]:
        self.calls.append({"query": query, "top_k": top_k, "where": where})
        order = self.orders.get(query, [])
        return [_chunk(cid, "keyword") for cid in order[:top_k]]


class CountingEmbedder:
    """确定性 embedding: 向量分量 = 查询文本长度。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [float(len(text)), 0.0]


@pytest.fixture(autouse=True)
def _no_alias_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """别名表/章节词典中性化: 用例不依赖仓库真实数据, 避免 where 被污染。"""
    monkeypatch.setenv(
        "MATRAG_MODEL_ALIASES_PATH", str(Path(__file__).parent / "definitely-absent.yaml")
    )
    monkeypatch.setenv(
        "MATRAG_SECTION_LEXICON_PATH",
        str(Path(__file__).parent / "definitely-absent-lexicon.json"),
    )
    reset_lexicon_cache()
    yield
    reset_lexicon_cache()


def _retriever(**kwargs: Any) -> HybridRetriever:
    return HybridRetriever(**kwargs)


def _two_variant_retriever() -> tuple[HybridRetriever, RecordingVector, RecordingKeyword, CountingEmbedder]:
    """变体 "效率甲"(3 字) / "效率乙甲"(4 字): 双腿各自给出不同结果集。"""
    vector = RecordingVector({3: ["c1", "c2"], 4: ["c2", "c3"]})
    keyword = RecordingKeyword({})
    embedder = CountingEmbedder()
    return (
        _retriever(vector_index=vector, keyword_index=keyword, embedder=embedder),
        vector,
        keyword,
        embedder,
    )


def _plan(**kwargs: Any) -> SimpleNamespace:
    """QueryPlan 鸭子类型替身（只给用到的字段, 缺字段即验证 fallback）。"""
    defaults: dict[str, Any] = {
        "retrieval_queries": ["效率甲", "效率乙甲"],
        "sub_queries": [],
        "needs_rules": False,
        "degradations": [],
        "filters": {},
        "kind": "spec_value",
        "intent": "查效率",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _fake_rules() -> list[DomainRule]:
    """fake 规则集: 两条 efficiency 规则, 供 needs_rules 用例使用。"""
    return [
        DomainRule(
            rule_id="K-FAKE-001",
            statement="电源转换效率 = 输出功率 / 输入功率",
            category="efficiency",
            scope="dc",
            derive={"output": "efficiency", "expr": "p_out / p_in * 100", "inputs": ["p_out", "p_in"]},
            test={"given": {}, "expect": {}},
            source={"name": "fake 标准"},
        ),
        DomainRule(
            rule_id="K-FAKE-002",
            statement="效率测试方法判据",
            category="efficiency",
            scope="dc",
            constraint={"shape": 'sh:message "效率读数"'},
            test={"given": {}, "expect": {}},
            source={"name": "fake 标准"},
        ),
    ]


# --------------------------------------------------------------------------- retrieve_many


def test_two_variants_fuse_and_dedupe() -> None:
    """两变体结果融合去重: 同 chunk 只出现一次, rrf_score 跨变体累加。"""
    retriever, vector, keyword, _ = _two_variant_retriever()
    hits = retriever.retrieve_many(["效率甲", "效率乙甲"], top_k=10, legs=("vector",))

    by_id = {h.chunk_id: h for h in hits}
    assert set(by_id) == {"c1", "c2", "c3"}
    assert len(hits) == len(by_id)  # 去重: 无重复 chunk_id
    # c2 两个变体都命中 (变体一 rank2 / 变体二 rank1) → w0/(k+2) + w1/(k+1) 累加
    assert by_id["c2"].rrf_score == pytest.approx(
        3 / (FUSION_K + 2) + 1 / (FUSION_K + 1)
    )
    assert by_id["c2"].per_leg["vector"] == pytest.approx(
        3 / (FUSION_K + 2) + 1 / (FUSION_K + 1)
    )
    assert by_id["c1"].rrf_score == pytest.approx(3 / (FUSION_K + 1))
    assert by_id["c3"].rrf_score == pytest.approx(1 / (FUSION_K + 2))
    # rrf_score == Σ per_leg（跨变体累加后依然可手算对账）
    assert by_id["c2"].rrf_score == pytest.approx(
        sum(v for v in by_id["c2"].per_leg.values() if v is not None)
    )
    assert len(vector.calls) == 2  # 每变体跑一次


def test_per_leg_and_legs_hit_merge_across_variants() -> None:
    """per_leg 跨变体合并: 同一腿被两变体命中则累加, legs_hit 取并集。"""
    vector = RecordingVector({3: ["c1"], 4: ["c1", "c2"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    hits = retriever.retrieve_many(["效率甲", "效率乙甲"], top_k=10, legs=("vector", "keyword"))
    c1 = next(h for h in hits if h.chunk_id == "c1")
    # c1 在 vector 两个变体都命中 (rank1) → w0/(k+1) + w1/(k+1) 累加; keyword 未命中 → None
    assert c1.per_leg["vector"] == pytest.approx(4 * (1 / (FUSION_K + 1)))
    assert c1.per_leg["keyword"] is None
    assert c1.legs_hit == ("vector",)


def test_single_variant_empty_leaves_trace_not_exception() -> None:
    """单变体空结果不退化为异常: 记 variant_empty, 其余变体照常融合。"""
    vector = RecordingVector({3: ["c1"], 4: []})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    hits = retriever.retrieve_many(["效率甲", "效率乙甲"], top_k=10, legs=("vector",))
    assert [h.chunk_id for h in hits] == ["c1"]
    traces = [d for d in retriever.degradations if d["kind"] == "variant_empty"]
    assert traces and traces[0]["query"] == "效率乙甲"


def test_all_variants_empty_returns_empty_list() -> None:
    """全部变体空 → 空列表 + 每变体都留痕, 不抛。"""
    vector = RecordingVector({})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    assert retriever.retrieve_many(["效率甲", "效率乙甲"], legs=("vector",)) == []
    traces = [d for d in retriever.degradations if d["kind"] == "variant_empty"]
    assert {t["query"] for t in traces} == {"效率甲", "效率乙甲"}


def test_variant_dedupe_keeps_order_and_strips_blanks() -> None:
    """变体去重保序: 重复项只跑一次, 空串/空白项被丢弃。"""
    vector = RecordingVector({4: ["c1"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    retriever.retrieve_many(["效率甲", "  ", "效率乙甲", "效率甲"], legs=("vector",))
    assert [int(call["vector"][0]) for call in vector.calls] == [3, 4]  # 两变体各一次


def test_overfetch_applies_per_variant_and_leg() -> None:
    """每个 (腿 × 变体) 各自 overfetch 3x。"""
    retriever, vector, keyword, _ = _two_variant_retriever()
    retriever.retrieve_many(["效率甲", "效率乙甲"], top_k=2, legs=("vector",))
    assert {call["top_k"] for call in vector.calls} == {2 * OVERFETCH}


def test_plan_none_uses_legacy_retrieve() -> None:
    """plan=None → 逐位等价 retrieve（零回归: 路由/改写/where 语义全同）。"""
    vector = RecordingVector({4: ["c1"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    with_plan = retriever.retrieve_with_plan("效率甲", None, top_k=5)
    plain = retriever.retrieve("效率甲", top_k=5)

    assert [(h.chunk_id, h.rrf_score) for h in with_plan] == [(h.chunk_id, h.rrf_score) for h in plain]
    assert all(h.plan_meta is None for h in with_plan)  # 无计划 → 无 plan_meta


# --------------------------------------------------------------------------- retrieve_with_plan


def test_plan_meta_attached_to_every_hit() -> None:
    """plan 摘要挂到每条 hit 的 plan_meta（kind/intent/sub_queries/needs_rules/变体）。"""
    vector = RecordingVector({4: ["c1", "c2"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    plan = _plan(sub_queries=["子问题1", "子问题2"], kind="formula", intent="查效率公式")
    hits = retriever.retrieve_with_plan("效率甲", plan, top_k=5, legs=("vector",))

    assert hits
    for hit in hits:
        meta = hit.plan_meta
        assert meta is not None
        assert meta["kind"] == "formula"
        assert meta["intent"] == "查效率公式"
        assert meta["sub_queries"] == ["子问题1", "子问题2"]
        assert meta["needs_rules"] is False
        assert meta["retrieval_queries"] == ["效率甲", "效率乙甲"]


def test_plan_retrieval_queries_drive_variants() -> None:
    """plan.retrieval_queries 决定实际检索的变体（两个变体都跑腿）。"""
    vector = RecordingVector({3: ["c1"], 4: ["c2"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    hits = retriever.retrieve_with_plan("效率甲", _plan(), top_k=5, legs=("vector",))
    assert {h.chunk_id for h in hits} == {"c1", "c2"}
    assert len(vector.calls) == 2  # 两变体


def test_plan_missing_retrieval_queries_falls_back_to_query() -> None:
    """plan 缺 retrieval_queries → 退回 [query] 并记 plan_fallback, 不抛。"""
    vector = RecordingVector({3: ["c1"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    plan = SimpleNamespace(kind="spec_value")  # 无 retrieval_queries 字段
    hits = retriever.retrieve_with_plan("效率甲", plan, top_k=5, legs=("vector",))

    assert [h.chunk_id for h in hits] == ["c1"]
    assert len(vector.calls) == 1
    traces = [d for d in retriever.degradations if d["kind"] == "plan_fallback"]
    assert traces and traces[0]["plan_type"] == "SimpleNamespace"


def test_plan_empty_retrieval_queries_falls_back() -> None:
    """plan.retrieval_queries 为空列表 → 同样退回原查询 + plan_fallback。"""
    vector = RecordingVector({3: ["c1"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    hits = retriever.retrieve_with_plan("效率甲", _plan(retrieval_queries=[]), legs=("vector",))
    assert [h.chunk_id for h in hits] == ["c1"]
    assert any(d["kind"] == "plan_fallback" for d in retriever.degradations)


def test_plan_filters_merge_into_where_but_caller_wins() -> None:
    """plan.filters 合并进 where; 调用方 where 仍优先（同 retrieve 语义）。"""
    vector = RecordingVector({3: ["c1"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    plan = _plan(filters={"section_contains": "安规要求", "product_code": "PA601-D54A"})
    retriever.retrieve_with_plan("效率甲", plan, legs=("vector",), where={"section_contains": "调用方章节"})

    merged = vector.calls[0]["where"]
    # 调用方 section_contains 胜出, plan 的 product_code 补入
    assert merged["section_contains"] == "调用方章节"
    assert merged["product_code"] == "PA601-D54A"


def test_plan_filter_conflict_with_caller_where_is_traced() -> None:
    """plan.filters 与调用方 where 冲突 → 调用方胜出 + where_key_conflict 留痕。"""
    vector = RecordingVector({3: ["c1"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    plan = _plan(filters={"section_contains": "计划章节"})
    retriever.retrieve_with_plan("效率甲", plan, legs=("vector",), where={"section_contains": "调用方章节"})

    conflicts = [d for d in retriever.degradations if d["kind"] == "where_key_conflict"]
    assert conflicts and conflicts[0]["key"] == "section_contains"
    assert conflicts[0]["kept"] == "调用方章节"
    assert conflicts[0]["source"] == "plan"


def test_plan_degradations_appended_with_source_marker() -> None:
    """plan.degradations 追加到 retriever.degradations, 带 source='plan' 标记。"""
    vector = RecordingVector({3: ["c1"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    plan = _plan(degradations=[{"kind": "llm_failed", "detail": "配额耗尽"}])
    retriever.retrieve_with_plan("效率甲", plan, legs=("vector",))

    traced = [d for d in retriever.degradations if d["kind"] == "llm_failed"]
    assert traced and traced[0]["source"] == "plan"
    assert traced[0]["detail"] == "配额耗尽"


def test_plan_as_dict_is_supported() -> None:
    """plan 用 dict 形态传入同样生效（鸭子类型读字段）。"""
    vector = RecordingVector({3: ["c1"], 4: ["c2"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    plan = {"retrieval_queries": ["效率甲", "效率乙甲"], "needs_rules": False}
    hits = retriever.retrieve_with_plan("效率甲", plan, top_k=5, legs=("vector",))
    assert {h.chunk_id for h in hits} == {"c1", "c2"}
    assert hits[0].plan_meta["retrieval_queries"] == ["效率甲", "效率乙甲"]


# --------------------------------------------------------------------------- 规则提示


def test_needs_rules_attaches_rule_hints_without_changing_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """needs_rules=True → last_rule_hints 非空, 且不影响检索排序。"""
    vector = RecordingVector({3: ["c1"], 4: ["c2"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    monkeypatch.setattr(
        "matrag.retrieve.rule_hint.select_rule_hints",
        lambda query, top_n=5: iter(_fake_rules()),  # type: ignore[return-value]
    )
    plan = _plan(needs_rules=True)
    hits = retriever.retrieve_with_plan("效率甲", plan, legs=("vector",))

    # 排序不受规则提示影响: 仍按 RRF（c1 rank1 + c2 rank1 各变体）
    assert [h.chunk_id for h in hits] == ["c1", "c2"]
    assert len(retriever.last_rule_hints) == 2
    assert retriever.last_rule_hints[0].rule_id == "K-FAKE-001"


def test_needs_rules_false_clears_last_rule_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """needs_rules=False → last_rule_hints 复位为空。"""
    vector = RecordingVector({3: ["c1"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    monkeypatch.setattr(
        "matrag.retrieve.rule_hint.select_rule_hints",
        lambda query, top_n=5: iter(_fake_rules()),  # type: ignore[return-value]
    )
    retriever.retrieve_with_plan("效率甲", _plan(needs_rules=True), legs=("vector",))
    assert retriever.last_rule_hints

    retriever.retrieve_with_plan("效率甲", _plan(needs_rules=False), legs=("vector",))
    assert retriever.last_rule_hints == []


def test_rule_hint_failure_is_traced_and_does_not_break_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """规则提示不可用 → 记 rule_hint_unavailable, 检索照常返回。"""
    vector = RecordingVector({3: ["c1"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    def _boom(query: str, top_n: int = 5) -> list[Any]:
        raise RuntimeError("规则源不可用")

    monkeypatch.setattr("matrag.retrieve.rule_hint.select_rule_hints", _boom)
    hits = retriever.retrieve_with_plan("效率甲", _plan(needs_rules=True), legs=("vector",))

    assert [h.chunk_id for h in hits] == ["c1"]
    assert retriever.last_rule_hints == []
    traced = [d for d in retriever.degradations if d["kind"] == "rule_hint_unavailable"]
    assert traced and "规则源不可用" in traced[0]["detail"]


def test_plan_none_resets_last_rule_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """plan=None 走旧路径时也复位 last_rule_hints（不留脏状态）。"""
    vector = RecordingVector({3: ["c1"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    monkeypatch.setattr(
        "matrag.retrieve.rule_hint.select_rule_hints",
        lambda query, top_n=5: iter(_fake_rules()),  # type: ignore[return-value]
    )
    retriever.retrieve_with_plan("效率甲", _plan(needs_rules=True), legs=("vector",))
    assert retriever.last_rule_hints
    retriever.retrieve_with_plan("效率甲", None, legs=("vector",))
    assert retriever.last_rule_hints == []


# --------------------------------------------------------------------------- understand 惰性依赖


def test_retrieve_understood_degrades_when_plan_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """查询计划不可用 → understand_unavailable 留痕, 降级为单变体检索。"""
    vector = RecordingVector({3: ["c1"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    monkeypatch.setattr(retriever_module, "build_query_plan", _unavailable_plan_builder())

    hits = retriever.retrieve_understood("效率甲", legs=("vector",))
    assert [h.chunk_id for h in hits] == ["c1"]
    assert len(vector.calls) == 1  # 单变体
    traced = [d for d in retriever.degradations if d["kind"] == "understand_unavailable"]
    assert traced and "No module named" in traced[0]["detail"]


def _unavailable_plan_builder() -> Any:
    """模拟 understand 不可用: 返回 ``(None, [understand_unavailable])``。"""

    def _builder(query: str, llm: Any = None) -> tuple[None, list[dict[str, Any]]]:
        return None, [
            {"kind": "understand_unavailable", "detail": "No module named 'understand'",
             "fallback": "retrieve_many([query])"},
        ]

    return _builder


def test_retrieve_understood_uses_plan_when_available() -> None:
    """查询计划可用时走 retrieve_with_plan（变体/plan_meta 均生效）。"""
    vector = RecordingVector({3: ["c1"], 4: ["c2"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    hits = retriever.retrieve_understood("效率甲", top_k=5, legs=("vector",))
    # 真实 understand 的 retrieval_queries[0] 恒为原查询; 不依赖其变体内容
    assert hits
    assert hits[0].plan_meta is not None
    assert hits[0].plan_meta["retrieval_queries"][0] == "效率甲"
    assert not any(d["kind"] == "understand_unavailable" for d in retriever.degradations)


def test_build_query_plan_import_error_becomes_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """understand 缺模块 → ``(None, [understand_unavailable])``, 不抛异常。"""
    import sys

    import matrag.retrieve as retrieve_pkg

    # 模拟 "W3-A 的 understand 尚未落盘": 从包属性与 sys.modules 同时摘掉,
    # 让 ``from matrag.retrieve import understand`` 真的走 ImportError 分支。
    monkeypatch.setitem(sys.modules, "matrag.retrieve.understand", None)
    monkeypatch.delattr(retrieve_pkg, "understand", raising=False)

    plan, notes = retriever_module.build_query_plan("效率甲")

    assert plan is None
    assert notes[0]["kind"] == "understand_unavailable"
    assert notes[0]["fallback"] == "retrieve_many([query])"
    assert "ModuleNotFoundError" in notes[0]["detail"] or "ImportError" in notes[0]["detail"]


def test_build_query_plan_uses_real_understand_when_present() -> None:
    """understand 已落盘时 build_query_plan 真的产出计划（不留降级）。"""
    plan, notes = retriever_module.build_query_plan("效率甲")
    assert notes == []
    assert plan is not None
    assert plan.retrieval_queries[0] == "效率甲"


# --------------------------------------------------------------------------- 参数 fail-fast


@pytest.mark.parametrize("top_k", [0, -1])
def test_invalid_top_k_fails_fast(top_k: int) -> None:
    """retrieve_many / retrieve_with_plan 的非法 top_k → ValueError。"""
    retriever = _retriever(vector_index=None, keyword_index=None, embedder=None)
    with pytest.raises(ValueError, match="top_k"):
        retriever.retrieve_many(["效率甲"], top_k=top_k)
    with pytest.raises(ValueError, match="top_k"):
        retriever.retrieve_with_plan("效率甲", None, top_k=top_k)


def test_retrieve_many_empty_queries_fails_fast() -> None:
    """retrieve_many 空/全空白变体 → ValueError。"""
    retriever = _retriever(vector_index=None, keyword_index=None, embedder=None)
    with pytest.raises(ValueError, match="queries"):
        retriever.retrieve_many([])
    with pytest.raises(ValueError, match="queries"):
        retriever.retrieve_many(["  ", ""])


def test_retrieval_hit_plan_meta_defaults_to_none() -> None:
    """RetrievalHit.plan_meta 默认 None（向后兼容旧构造）。"""
    hit = RetrievalHit(
        chunk_id="c1", doc_id="d", text="t", meta={},
        rrf_score=0.5, per_leg={"vector": 0.5}, legs_hit=("vector",),
    )
    assert hit.plan_meta is None


def test_multi_query_keeps_single_query_invariant() -> None:
    """单变体时 retrieve_many 与 retrieve 结果完全一致（恒等式回归为零）。"""
    vector = RecordingVector({3: ["c1", "c2"]})
    keyword = RecordingKeyword({})
    retriever = _retriever(vector_index=vector, keyword_index=keyword, embedder=CountingEmbedder())

    multi = retriever.retrieve_many(["效率甲"], legs=("vector",))
    single = retriever.retrieve("效率甲", legs=("vector",))

    assert [(h.chunk_id, h.rrf_score) for h in multi] == [(h.chunk_id, h.rrf_score) for h in single]
    # 多查询变体入口不挂 plan_meta
    assert all(h.plan_meta is None for h in multi)
