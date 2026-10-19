"""混合检索器测试（P1-5 ``matrag.retrieve.retriever``，全离线）。

覆盖: 手算 RRF 对账、SearchRanker 真实被调用（收到带 "id" 键的 dict）、
单腿缺失、同 chunk 双腿命中只出现一次、top_k 截断、where 透传与合并优先级、
explain 结构、腿加权公式、LLM 改写并行融合、graph 腿预留留痕、参数 fail-fast。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from semantica.vector_store import SearchRanker

from matrag.retrieve import retriever as retriever_module
from matrag.retrieve.query import (
    FILTER_SECTION_CONTAINS,
    parse_query,
    reset_lexicon_cache,
)
from matrag.retrieve.retriever import (
    FUSION_K,
    KNOWN_LEGS,
    OVERFETCH,
    PRIMARY_VARIANT_WEIGHT,
    HybridRetriever,
    RetrievalHit,
    merge_legs,
)

# --------------------------------------------------------------------------- fakes

#: 每个 chunk 的 (doc_id, text, meta 锚点)
CHUNKS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "c1": ("PA601-D54A", "输出电压 12V", {"page": 5, "section_path": "4 > 4.3", "table_id": "表3"}),
    "c2": ("PA601-D54A", "最大输出功率 600W", {"page": 6, "section_path": "4 > 4.3", "table_id": "表11"}),
    "c3": ("CY0177-75V-20A", "效率 93%", {"page": 8, "section_path": "3 > 3.2", "table_id": "表9"}),
    "c4": ("GDC800T5054-UC", "保持时间 20ms", {"page": 9, "section_path": "5", "table_id": ""}),
    # 原查询变体加权用例的专用 chunk：c9 只在原查询命中，g1~g4 只在改写变体命中
    "c9": ("PA601-D54A", "输入电压范围 85~264Vac", {"page": 12, "section_path": "4.3.1", "table_id": ""}),
    "g1": ("PA601-D54A", "通用条目一", {"page": 13, "section_path": "4.3", "table_id": ""}),
    "g2": ("PA601-D54A", "通用条目二", {"page": 14, "section_path": "4.3", "table_id": ""}),
    "g3": ("PA601-D54A", "通用条目三", {"page": 15, "section_path": "4.3", "table_id": ""}),
    "g4": ("PA601-D54A", "通用条目四", {"page": 16, "section_path": "4.3", "table_id": ""}),
}


#: 原查询变体加权用例用的两条改写（长度互不相同 → 假向量索引可按长度区分名次表）
REWRITE_1 = "输出特性同义改写"
REWRITE_2 = "输出特性的另一种问法"


def _chunk(chunk_id: str, leg: str) -> SimpleNamespace:
    """鸭子类型 ScoredChunk 替身（属性形态，与 P1-4 契约同形）。"""
    doc_id, text, meta = CHUNKS[chunk_id]
    return SimpleNamespace(
        chunk_id=chunk_id, doc_id=doc_id, text=text, meta=dict(meta), score=0.5, leg=leg
    )


class FakeEmbedder:
    """确定性 embedding: 向量即查询文本的标记（供向量腿按查询区分）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [float(len(text)), 0.0]


class FakeVectorIndex:
    """按查询长度选名次表的假向量索引。"""

    def __init__(self, orders: dict[int, list[str]] | None = None) -> None:
        self.orders = orders if orders is not None else {}
        self.calls: list[dict[str, Any]] = []

    def search_vector(
        self, vector: list[float], *, top_k: int = 10, where: dict[str, Any] | None = None
    ) -> list[SimpleNamespace]:
        self.calls.append({"vector": list(vector), "top_k": top_k, "where": where})
        order = self.orders.get(int(vector[0]), [])
        return [_chunk(chunk_id, "vector") for chunk_id in order[:top_k]]


class FakeKeywordIndex:
    """按查询文本选名次表的假关键词索引。"""

    def __init__(self, orders: dict[str, list[str]] | None = None) -> None:
        self.orders = orders if orders is not None else {}
        self.calls: list[dict[str, Any]] = []

    def search_keyword(
        self, query: str, *, top_k: int = 10, where: dict[str, Any] | None = None
    ) -> list[SimpleNamespace]:
        self.calls.append({"query": query, "top_k": top_k, "where": where})
        order = self.orders.get(query, [])
        return [_chunk(chunk_id, "keyword") for chunk_id in order[:top_k]]


class RecordingRanker(SearchRanker):
    """真实 SearchRanker 子类: 记录收到的输入，行为完全委托父类。"""

    def __init__(self, strategy: str = "reciprocal_rank_fusion") -> None:
        super().__init__(strategy=strategy)
        self.calls: list[dict[str, Any]] = []

    def reciprocal_rank_fusion(
        self, results: list[list[dict[str, Any]]], k: int = 60
    ) -> list[dict[str, Any]]:
        self.calls.append({"results": [[dict(item) for item in leg] for leg in results], "k": k})
        return super().reciprocal_rank_fusion(results, k)


class FakeLLM:
    """鸭子类型 LLM 替身。"""

    def __init__(self, payload: Any = None, *, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, system: str, user: str, schema_hint: str = "") -> Any:
        self.calls.append({"system": system, "user": user, "schema_hint": schema_hint})
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.fixture(autouse=True)
def _no_alias_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """指向不存在的别名表与章节词典，保证用例不依赖仓库真实数据。

    章节词典不中性化的话, 主会话重建索引后 ``data/store/section_lexicon.json``
    会真实存在，"输出特性"等章节名就会给 where 添上 ``section_contains``，
    让未涉及章节过滤的用例断言失真。
    """
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


def _retriever(
    *,
    vector: Any = None,
    keyword: Any = None,
    embedder: Any = None,
    **kwargs: Any,
) -> HybridRetriever:
    return HybridRetriever(
        vector_index=vector, keyword_index=keyword, embedder=embedder, **kwargs
    )


def kw_key(text: str) -> str:
    """关键词腿实际收到的查询文本（fakes 的键与实现同源）。

    规范化本身（剥 where token + 去停用词）由专门的单测用硬编码期望值覆盖;
    这里只保证 fake 的键与实现一致，融合/排序另有手算断言。
    """
    probe = HybridRetriever(vector_index=None, keyword_index=None, embedder=None)
    return probe._keyword_query(parse_query(text), text)

def _standard() -> tuple[HybridRetriever, FakeVectorIndex, FakeKeywordIndex, FakeEmbedder]:
    """确定性双腿检索器: vector [c1,c2,c3] / keyword [c2,c3,c4]。"""
    query = parse_query("输出特性查询").raw
    embedder = FakeEmbedder()
    vector = FakeVectorIndex({len(query): ["c1", "c2", "c3"]})
    keyword = FakeKeywordIndex({kw_key(query): ["c2", "c3", "c4"]})
    return _retriever(vector=vector, keyword=keyword, embedder=embedder), vector, keyword, embedder

# --------------------------------------------------------------------------- 融合对账


def test_rrf_matches_hand_calculation() -> None:
    """手算 RRF 对账: vector[1,2,3] / keyword[2,3,4], k=60。"""
    retriever, _, _, _ = _standard()
    hits = retriever.retrieve("输出特性查询", top_k=10)

    expected = {
        "c2": 1 / 62 + 1 / 61,   # vector rank2 + keyword rank1
        "c3": 1 / 63 + 1 / 62,   # vector rank3 + keyword rank2
        "c1": 1 / 61,             # vector rank1
        "c4": 1 / 63,             # keyword rank3
    }
    assert [h.chunk_id for h in hits] == ["c2", "c3", "c1", "c4"]
    for hit in hits:
        assert hit.rrf_score == pytest.approx(expected[hit.chunk_id])
        assert hit.rrf_score == pytest.approx(sum(v for v in hit.per_leg.values() if v is not None))


def test_per_leg_normalized_rank_scores() -> None:
    """per_leg = w_leg / (fusion_k + rank); 缺失腿为 None, legs_hit 与之一致。"""
    retriever, _, _, _ = _standard()
    hits = {h.chunk_id: h for h in retriever.retrieve("输出特性查询")}

    assert hits["c1"].per_leg == {"vector": pytest.approx(1 / 61), "keyword": None}
    assert hits["c1"].legs_hit == ("vector",)
    assert hits["c2"].per_leg == {"vector": pytest.approx(1 / 62), "keyword": pytest.approx(1 / 61)}
    assert hits["c2"].legs_hit == ("vector", "keyword")
    assert hits["c4"].per_leg == {"vector": None, "keyword": pytest.approx(1 / 63)}


def test_same_chunk_in_both_legs_appears_once() -> None:
    """同 chunk 两腿命中 → 只出现一次, per_leg 双填, legs_hit 双腿。"""
    retriever, _, _, _ = _standard()
    hits = retriever.retrieve("输出特性查询")
    assert len({h.chunk_id for h in hits}) == len(hits)
    both = [h for h in hits if h.legs_hit == ("vector", "keyword")]
    assert {h.chunk_id for h in both} == {"c2", "c3"}


def test_top_k_truncates_after_fusion() -> None:
    """top_k 截断发生在融合之后（不是各腿先截断）。"""
    retriever, vector, keyword, _ = _standard()
    hits = retriever.retrieve("输出特性查询", top_k=2)
    assert [h.chunk_id for h in hits] == ["c2", "c3"]
    assert vector.calls[0]["top_k"] == 2 * OVERFETCH  # 各腿 overfetch 3x
    assert keyword.calls[0]["top_k"] == 2 * OVERFETCH


def test_missing_leg_index_runs_single_leg() -> None:
    """keyword index=None → 只走 vector, per_leg keyword=None, 缺腿留痕。"""
    query = parse_query("输出特性查询").raw
    embedder = FakeEmbedder()
    vector = FakeVectorIndex({len(query): ["c1", "c2"]})
    retriever = _retriever(vector=vector, keyword=None, embedder=embedder)

    hits = retriever.retrieve("输出特性查询")
    assert [h.chunk_id for h in hits] == ["c1", "c2"]
    assert all(h.per_leg == {"vector": pytest.approx(1 / 61 if i == 0 else 1 / 62), "keyword": None}
               for i, h in enumerate(hits))
    assert all(h.legs_hit == ("vector",) for h in hits)

    kinds = [d["kind"] for d in retriever.degradations]
    assert "leg_missing_index" in kinds
    missing = [d for d in retriever.degradations if d["kind"] == "leg_missing_index"]
    assert missing[0]["leg"] == "keyword"


def test_no_results_returns_empty_list() -> None:
    """两腿都空 → 空列表，不抛。"""
    retriever = _retriever(
        vector=FakeVectorIndex({}), keyword=FakeKeywordIndex({}), embedder=FakeEmbedder()
    )
    assert retriever.retrieve("输出特性查询") == []


# --------------------------------------------------------------------------- where


def test_where_doc_id_passed_through() -> None:
    """型号过滤（doc_id）原样透传给两腿（P1-4 两腿都支持等值过滤）。"""
    retriever, vector, keyword, _ = _standard()
    retriever.retrieve("PA601-D54A 表11 输出特性查询", top_k=3)
    assert vector.calls[0]["where"] == {"product_code": "PA601-D54A"}
    assert keyword.calls[0]["where"] == {"product_code": "PA601-D54A"}


def test_caller_where_wins_over_parsed() -> None:
    """调用方 where 优先（同键覆盖，异键合并）。"""
    retriever, vector, keyword, _ = _standard()
    retriever.retrieve("PA601-D54A 表11 输出特性查询", where={"doc_id": "OTHER", "page": 4})
    merged = {"doc_id": "OTHER", "page": 4, "product_code": "PA601-D54A"}
    assert vector.calls[0]["where"] == merged
    assert keyword.calls[0]["where"] == merged


def test_unexpressible_filters_dropped_with_trace() -> None:
    """口语表号 → 不进 where 但留痕; 裸章节号 → 转 section_contains 包含过滤。"""
    query = parse_query("PA601-D54A 表11 4.3 输出特性查询").raw
    vector = FakeVectorIndex({len(query): ["c1"]})
    keyword = FakeKeywordIndex({kw_key(query): ["c1"]})
    retriever = _retriever(vector=vector, keyword=keyword, embedder=FakeEmbedder())

    retriever.retrieve(query)
    expected = {"product_code": "PA601-D54A", "section_contains": "4.3"}  # 不含 table
    assert vector.calls[0]["where"] == expected
    assert keyword.calls[0]["where"] == expected
    dropped = [d for d in retriever.degradations if d["kind"] == "where_key_dropped"]
    assert {d["key"] for d in dropped} == {"table"}


def test_section_name_filter_reaches_both_legs(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """端到端: 查询「输入特性 查询」→ 两腿 where 都带 section_contains="输入特性"。

    章节词典指向 tmp 文件（含"输入特性"标题），两条腿为 fake 并记录收到的 where。
    """
    lexicon = tmp_path / "section_lexicon.json"
    lexicon.write_text(
        json.dumps({"titles": {"输入特性": ["4 > 4.3 > 4.3.1 输入特性"]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("MATRAG_SECTION_LEXICON_PATH", str(lexicon))
    reset_lexicon_cache()
    try:
        query = parse_query("输入特性 查询").raw  # 词典命中 "输入特性" → 过滤条件
        vector = FakeVectorIndex({len(query): ["c1"]})
        keyword = FakeKeywordIndex({kw_key(query): ["c1"]})
        retriever = _retriever(vector=vector, keyword=keyword, embedder=FakeEmbedder())

        retriever.retrieve(query)
        for leg in (vector, keyword):
            assert leg.calls[0]["where"] == {"section_contains": "输入特性"}
        assert FILTER_SECTION_CONTAINS == "section_contains"
    finally:
        reset_lexicon_cache()


def test_section_number_filter_reaches_both_legs() -> None:
    """端到端: 章节号 "4.3.1" 也转成 section_contains 下达到两腿。"""
    query = parse_query("PA601-D54A 4.3.1 输入特性 查询").raw
    vector = FakeVectorIndex({len(query): ["c1"]})
    keyword = FakeKeywordIndex({kw_key(query): ["c1"]})
    retriever = _retriever(vector=vector, keyword=keyword, embedder=FakeEmbedder())

    retriever.retrieve(query)
    # 章节号比章节名更精确 → where 里是章节号
    expected = {"product_code": "PA601-D54A", "section_contains": "4.3.1"}
    assert vector.calls[0]["where"] == expected
    assert keyword.calls[0]["where"] == expected


def test_index_form_table_id_is_passed_through() -> None:
    """用户直接引用索引口径表号（表t0-6）→ 以 table_id 透传，两腿均可用。"""
    query = parse_query("PA601-D54A 表t0-6 输出特性查询").raw
    vector = FakeVectorIndex({len(query): ["c1"]})
    keyword = FakeKeywordIndex({kw_key(query): ["c1"]})
    retriever = _retriever(vector=vector, keyword=keyword, embedder=FakeEmbedder())

    retriever.retrieve(query)
    assert vector.calls[0]["where"] == {"product_code": "PA601-D54A", "table_id": "t0-6"}
    assert keyword.calls[0]["where"] == {"product_code": "PA601-D54A", "table_id": "t0-6"}


def test_caller_where_only() -> None:
    """查询无 filters 时只透传调用方 where。"""
    retriever, vector, keyword, _ = _standard()
    retriever.retrieve("输出特性查询", where={"doc_id": "X"})
    assert vector.calls[0]["where"] == {"doc_id": "X"}
    assert keyword.calls[0]["where"] == {"doc_id": "X"}


# --------------------------------------------------------------------------- SearchRanker


def test_search_ranker_is_really_called_with_id_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """SearchRanker 被真实调用, 且每个 result dict 都带 "id" 键。"""
    retriever, _, _, _ = _standard()
    ranker = RecordingRanker()
    monkeypatch.setattr(retriever, "_ranker", ranker)

    hits = retriever.retrieve("输出特性查询")
    assert ranker.calls, "SearchRanker.reciprocal_rank_fusion 未被调用"
    call = ranker.calls[0]
    assert call["k"] == FUSION_K
    assert len(call["results"]) == 2  # 两条腿各一个名次表
    for leg_results in call["results"]:
        assert leg_results
        for item in leg_results:
            assert item["id"] in {h.chunk_id for h in hits}
    # 真实融合结果与替身父类实现一致 → 排序没被绕过
    assert [h.chunk_id for h in hits] == ["c2", "c3", "c1", "c4"]


def test_search_ranker_constructed_from_module_symbol() -> None:
    """构造函数用的是模块级 SearchRanker（monkeypatch 模块属性即可替换）。"""
    assert retriever_module.SearchRanker is SearchRanker
    retriever, _, _, _ = _standard()
    assert type(retriever._ranker) is SearchRanker
    assert retriever._ranker.strategy == "reciprocal_rank_fusion"

# --------------------------------------------------------------------------- explain


def test_explain_structure() -> None:
    """explain: {chunk_id: {doc_id, page, section_path, table_id, legs_hit, per_leg, rrf_score, ...}}。"""
    retriever, _, _, _ = _standard()
    hits = retriever.retrieve("输出特性查询")
    explained = retriever.explain(hits)

    assert set(explained) == {h.chunk_id for h in hits}
    entry = explained["c2"]
    assert entry["doc_id"] == "PA601-D54A"
    assert entry["page"] == 6
    assert entry["section_path"] == "4 > 4.3"
    assert entry["table_id"] == "表11"
    assert entry["legs_hit"] == ["vector", "keyword"]
    assert entry["per_leg"]["vector"] == pytest.approx(1 / 62)
    assert entry["rrf_score"] == pytest.approx(1 / 62 + 1 / 61)
    assert entry["ranks"] == {"vector": 2, "keyword": 1}
    assert entry["fused_rank"] == 1


def test_explain_survives_missing_anchors() -> None:
    """meta 缺锚点字段 → explain 里为 None，不抛。"""
    query = parse_query("输出特性查询").raw
    vector = FakeVectorIndex({len(query): ["c1"]})
    keyword = FakeKeywordIndex({kw_key(query): ["c4"]})
    retriever = _retriever(vector=vector, keyword=keyword, embedder=FakeEmbedder())
    entry = retriever.explain(retriever.retrieve("输出特性查询"))["c4"]
    assert entry["table_id"] == ""
    assert entry["section_path"] == "5"
    assert entry["page"] == 9


# --------------------------------------------------------------------------- 腿加权


def test_weighted_fusion_formula() -> None:
    """腿加权: rrf = Σ w_leg / (fusion_k + rank), 排序与手算一致。"""
    query = parse_query("输出特性查询").raw
    embedder = FakeEmbedder()
    vector = FakeVectorIndex({len(query): ["c1", "c2", "c3"]})
    keyword = FakeKeywordIndex({kw_key(query): ["c2", "c3", "c4"]})
    retriever = _retriever(
        vector=vector, keyword=keyword, embedder=embedder,
        vector_weight=2.0, keyword_weight=1.0,
    )

    assert retriever.weighted is True
    hits = {h.chunk_id: h for h in retriever.retrieve("输出特性查询")}
    assert hits["c2"].rrf_score == pytest.approx(2 / 62 + 1 / 61)
    assert hits["c3"].rrf_score == pytest.approx(2 / 63 + 1 / 62)
    assert hits["c1"].rrf_score == pytest.approx(2 / 61)
    assert hits["c4"].rrf_score == pytest.approx(1 / 63)
    # per_leg 归一化名次分同样带权重
    assert hits["c1"].per_leg["vector"] == pytest.approx(2 / 61)
    assert hits["c2"].per_leg["keyword"] == pytest.approx(1 / 61)


def test_equal_weights_use_plain_rrf_path() -> None:
    """等权时走 semantica RRF 原路（不分腿调用）。"""
    retriever, _, _, _ = _standard()
    assert retriever.weighted is False
    retriever.retrieve("输出特性查询")


# ------------------------------------------------------- 原查询变体加权（P1-7 修复）


def test_single_variant_is_identical_under_any_primary_weight() -> None:
    """单变体零回归: primary_variant_weight 取任何值, 融合分与排序逐位不变。

    单变体时没有可比对象, 加权只会整体平移全部分数, 破坏 rrf_score 与
    sum(per_leg) 的手算口径。_variant_weights 对 n=1 恒返回 [1.0] 兜住这点。
    """
    query = parse_query("输出特性查询").raw

    def _scores(weight: float) -> list[tuple[str, float]]:
        retriever = _retriever(
            vector=FakeVectorIndex({len(query): ["c1", "c2", "c3"]}),
            keyword=FakeKeywordIndex({kw_key(query): ["c2", "c3", "c4"]}),
            embedder=FakeEmbedder(),
            primary_variant_weight=weight,
        )
        return [(h.chunk_id, h.rrf_score) for h in retriever.retrieve(query)]

    expected = [(h.chunk_id, h.rrf_score) for h in _standard()[0].retrieve(query)]
    assert _scores(1.0) == pytest.approx(expected, rel=1e-12)
    assert _scores(999.0) == pytest.approx(expected, rel=1e-12)
    # 与手算经典 RRF 口径一致（未被整体平移）
    assert expected[0][1] == pytest.approx(1 / 62 + 1 / 61)


def _consensus_scenario(weight: float) -> HybridRetriever:
    """原查询只命中 c9；两条改写都命中同一组"共识 chunk" g1~g4。

    3 变体 × 2 腿 = 6 张名次表：c9 只出现在原查询那 2 张里（双腿 rank1），
    g1 出现在改写的 4 张里（每张 rank1）。RRF 按"出现在越多表里"累加，
    所以等权时 g1 得分是 c9 的两倍。
    """
    query = parse_query("输出特性查询").raw
    consensus = ["g1", "g2", "g3", "g4"]
    return _retriever(
        vector=FakeVectorIndex(
            {len(query): ["c9"], len(REWRITE_1): consensus, len(REWRITE_2): consensus}
        ),
        keyword=FakeKeywordIndex(
            {
                kw_key(query): ["c9"],
                kw_key(REWRITE_1): consensus,
                kw_key(REWRITE_2): consensus,
            }
        ),
        embedder=FakeEmbedder(),
        primary_variant_weight=weight,
    )


def test_primary_query_beats_rewrite_consensus_chunk() -> None:
    """原查询的精准命中不被"多条改写都命中"的共识 chunk 挤下去。

    复现 P1-7 实测缺陷：多查询等权 RRF 下，一个在多条改写名次表里都排头名的
    泛化 chunk 分数是原查询头名 chunk 的数倍，精确查表类问题的正确章节被挤出
    top-8（spec 层 section_hit@8 95.8% → 70.8%）。原查询加权后 c9 反超。
    """
    query = parse_query("输出特性查询").raw
    hits = _consensus_scenario(PRIMARY_VARIANT_WEIGHT).retrieve_many(
        [query, REWRITE_1, REWRITE_2], top_k=5, legs=("vector", "keyword")
    )
    # c9 = 原查询 2 张表 × w3 → 6/61；g1 = 改写 4 张表 × w1 → 4/61
    assert hits[0].chunk_id == "c9"
    assert hits[0].rrf_score == pytest.approx(6 / (FUSION_K + 1))
    assert hits[1].chunk_id == "g1"
    assert hits[1].rrf_score == pytest.approx(4 / (FUSION_K + 1))


def test_primary_variant_weight_one_restores_equal_weight_fusion() -> None:
    """把 primary_variant_weight 调成 1.0 → 逐位回到等权融合（可回退开关）。"""
    query = parse_query("输出特性查询").raw
    hits = _consensus_scenario(1.0).retrieve_many(
        [query, REWRITE_1, REWRITE_2], top_k=5, legs=("vector", "keyword")
    )
    # 等权：c9 只 2/61，g1 有 4/61 → 共识 chunk 压过原查询（修复前的行为）
    assert hits[0].chunk_id == "g1"
    assert hits[0].rrf_score == pytest.approx(4 / (FUSION_K + 1))
    c9 = next(h for h in hits if h.chunk_id == "c9")
    assert c9.rrf_score == pytest.approx(2 / (FUSION_K + 1))


def test_invalid_primary_variant_weight_fails_fast() -> None:
    for bad in (0, -1.0, "3"):
        with pytest.raises(ValueError, match="primary_variant_weight"):
            _retriever(vector=None, keyword=None, embedder=None, primary_variant_weight=bad)


def test_fusion_k_is_configurable() -> None:
    """fusion_k 可配并贯穿腿与融合。"""
    query = parse_query("输出特性查询").raw
    vector = FakeVectorIndex({len(query): ["c1", "c2"]})
    keyword = FakeKeywordIndex({kw_key(query): ["c2", "c1"]})
    retriever = _retriever(vector=vector, keyword=keyword, embedder=FakeEmbedder(), fusion_k=10)
    hits = {h.chunk_id: h for h in retriever.retrieve("输出特性查询")}
    assert hits["c2"].rrf_score == pytest.approx(1 / 12 + 1 / 11)
    assert hits["c1"].rrf_score == pytest.approx(1 / 11 + 1 / 12)
    assert retriever.fusion_k == 10


# --------------------------------------------------------------------------- 路由联动 / graph 腿


def test_formula_query_skips_graph_leg_with_trace() -> None:
    """FORMULA → 三腿路由, graph 未接线 → 跳过并留痕，另两腿照常。"""
    query = parse_query("效率计算公式").raw
    vector = FakeVectorIndex({len(query): ["c1", "c3"]})
    keyword = FakeKeywordIndex({kw_key(query): ["c3", "c1"]})
    retriever = _retriever(vector=vector, keyword=keyword, embedder=FakeEmbedder())

    hits = retriever.retrieve("效率计算公式")
    # c1/c3 融合分持平（各 1/61 + 1/62），按 chunk_id 稳定 tiebreak
    assert [h.chunk_id for h in hits] == ["c1", "c3"]
    assert hits[0].rrf_score == pytest.approx(hits[1].rrf_score)
    assert all(h.per_leg["graph"] is None for h in hits)
    assert all(h.legs_hit == ("vector", "keyword") for h in hits)
    notes = [d for d in retriever.degradations if d["kind"] == "leg_not_wired"]
    assert notes and notes[0]["leg"] == "graph"
    assert "P1-5b" in notes[0]["detail"]
    assert retriever.last_plan["routed"].kind.value == "formula"


def test_explicit_legs_override_routing() -> None:
    """显式 legs 覆盖路由结果。"""
    query = parse_query("输出特性查询").raw
    vector = FakeVectorIndex({len(query): ["c1"]})
    keyword = FakeKeywordIndex({kw_key(query): ["c1"]})
    retriever = _retriever(vector=vector, keyword=keyword, embedder=FakeEmbedder())
    retriever.retrieve("效率计算公式", legs=("vector",))
    assert keyword.calls == []
    assert vector.calls


# --------------------------------------------------------------------------- LLM 改写


def test_llm_rewrite_parallel_search_then_fuse() -> None:
    """LLM 改写: 原查询 + 2 个同义查询并行检索后一起融合（4 个名次表）。"""
    query = parse_query("输出特性查询").raw
    rewrite_1, rewrite_2 = "输出特性同义改写", "输出特性的同义问法"  # 长度互不相同（8/9 字符）
    embedder = FakeEmbedder()
    vector = FakeVectorIndex({len(query): ["c1"], len(rewrite_1): ["c2"], len(rewrite_2): ["c3"]})
    keyword = FakeKeywordIndex({
        kw_key(query): ["c2"],
        kw_key(rewrite_1): ["c3"],
        kw_key(rewrite_2): ["c4"],
    })
    llm = FakeLLM({"queries": [rewrite_1, rewrite_2]})
    retriever = _retriever(vector=vector, keyword=keyword, embedder=embedder, llm=llm)

    hits = retriever.retrieve("输出特性查询")
    assert len(embedder.calls) == 3  # 原查询 + 两个改写
    assert len(keyword.calls) == 3
    assert len(vector.calls) == 3
    assert {h.chunk_id for h in hits} == {"c1", "c2", "c3", "c4"}
    # 变体权重: 原查询 w=3, 改写 w=1（RRF 对"出现在越多名次表"的 chunk 有偏好,
    # 而改写彼此高度相关, 等权会让共识噪声压过原查询的精准命中——见 PRIMARY_VARIANT_WEIGHT）
    # c2 = keyword(原查询 r1)*3 + vector(改写一 r1)*1 = 4/61
    # c1 = vector(原查询 r1)*3              = 3/61
    # c3 = vector(改写二 r1)*1 + keyword(改写一 r1)*1 = 2/61
    # c4 = keyword(改写二 r1)*1             = 1/61
    assert [h.chunk_id for h in hits] == ["c2", "c1", "c3", "c4"]
    assert hits[0].rrf_score == pytest.approx(4 / 61)
    assert hits[1].rrf_score == pytest.approx(3 / 61)
    assert hits[2].rrf_score == pytest.approx(2 / 61)
    assert hits[3].rrf_score == pytest.approx(1 / 61)
    assert retriever.last_plan["queries"] == [query, rewrite_1, rewrite_2]


def test_llm_none_never_rewrites() -> None:
    """llm=None → 只检索原始查询一次，绝不多发请求。"""
    retriever, vector, keyword, embedder = _standard()
    retriever.retrieve("输出特性查询")
    assert embedder.calls == ["输出特性查询"]
    assert len(keyword.calls) == 1


def test_llm_rewrite_failure_leaves_trace() -> None:
    """改写失败 → 留痕并用原始查询（检索不崩）。"""
    retriever, _, _, embedder = _standard()
    retriever._llm = FakeLLM(error=RuntimeError("MaaS 配额耗尽"))
    hits = retriever.retrieve("输出特性查询")
    assert [h.chunk_id for h in hits] == ["c2", "c3", "c1", "c4"]
    assert embedder.calls == ["输出特性查询"]
    notes = [d for d in retriever.degradations if d["kind"] == "llm_rewrite_failed"]
    assert notes and "RuntimeError" in notes[0]["detail"]


def test_llm_rewrite_empty_or_invalid_response() -> None:
    """改写返回空/非对象 → 留痕，仍用原查询。"""
    retriever, _, _, embedder = _standard()
    retriever._llm = FakeLLM({"queries": []})
    assert retriever._llm_rewrite("输出特性查询") == []
    retriever._llm = FakeLLM(["oops"])
    assert retriever._llm_rewrite("输出特性查询") == []
    assert any(d["kind"].startswith("llm_rewrite") for d in retriever.degradations)


# --------------------------------------------------------------------------- 稳健性


def test_duplicate_chunk_in_one_leg_counted_once() -> None:
    """同一腿内重复 chunk → 只出现一次并留痕。"""
    query = parse_query("输出特性查询").raw
    vector = FakeVectorIndex({len(query): ["c1", "c1", "c2"]})
    keyword = FakeKeywordIndex({kw_key(query): ["c1"]})
    retriever = _retriever(vector=vector, keyword=keyword, embedder=FakeEmbedder())
    hits = retriever.retrieve("输出特性查询")
    assert [h.chunk_id for h in hits] == ["c1", "c2"]
    assert hits[0].rrf_score == pytest.approx(1 / 61 + 1 / 61)
    assert [d for d in retriever.degradations if d["kind"] == "duplicate_chunk_in_leg"]


def test_dict_shaped_chunks_are_supported() -> None:
    """鸭子类型: index 返回 dict 形态的 chunk 也能取到锚点。"""

    class DictIndex:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def search_vector(self, vector, *, top_k=10, where=None):
            self.calls.append({"where": where})
            return [{
                "chunk_id": "c1",
                "doc_id": "PA601-D54A",
                "text": "输出电压 12V",
                "meta": {"page": 5, "section_path": "4 > 4.3", "table_id": "表3"},
                "score": 0.9,
                "leg": "vector",
            }]

    index = DictIndex()
    retriever = _retriever(vector=index, keyword=None, embedder=FakeEmbedder())
    hits = retriever.retrieve("输出特性查询")
    assert hits[0].doc_id == "PA601-D54A"
    assert retriever.explain(hits)["c1"]["page"] == 5


def test_chunk_without_id_fails_fast() -> None:
    """chunk 缺 chunk_id → ValueError（不许静默丢弃）。"""

    class BadIndex:
        def search_vector(self, vector, *, top_k=10, where=None):
            return [{"doc_id": "x", "text": "y", "meta": {}}]

    retriever = _retriever(vector=BadIndex(), keyword=None, embedder=FakeEmbedder())
    with pytest.raises(ValueError, match="chunk_id"):
        retriever.retrieve("输出特性查询")


def test_vector_leg_without_embedder_fails_fast() -> None:
    """vector 腿需要 embedder；缺 → ValueError。"""
    vector = FakeVectorIndex({0: ["c1"]})
    retriever = _retriever(vector=vector, keyword=None, embedder=None)
    with pytest.raises(ValueError, match="embedder"):
        retriever.retrieve("输出特性查询")


# --------------------------------------------------------------------------- 参数校验


@pytest.mark.parametrize(
    "kwargs",
    [{"fusion_k": 0}, {"fusion_k": -1}, {"vector_weight": 0}, {"vector_weight": -1.0},
     {"keyword_weight": 0}, {"keyword_weight": -0.5}],
)
def test_invalid_constructor_params_fail_fast(kwargs: dict[str, Any]) -> None:
    """非法 fusion_k / 权重 → ValueError。"""
    with pytest.raises(ValueError):
        _retriever(vector=None, keyword=None, embedder=None, **kwargs)


@pytest.mark.parametrize("kwargs", [{"top_k": 0}, {"top_k": -3}])
def test_invalid_top_k_fails_fast(kwargs: dict[str, Any]) -> None:
    """非法 top_k → ValueError。"""
    retriever = _retriever(vector=None, keyword=None, embedder=None)
    with pytest.raises(ValueError, match="top_k"):
        retriever.retrieve("输出特性查询", **kwargs)


@pytest.mark.parametrize("legs", [("bogus",), ("vector", "nope"), ()])
def test_unknown_legs_fail_fast(legs: tuple[str, ...]) -> None:
    """未知/空腿名 → ValueError。"""
    retriever = _retriever(vector=FakeVectorIndex({}), keyword=None, embedder=FakeEmbedder())
    with pytest.raises(ValueError, match="腿"):
        retriever.retrieve("输出特性查询", legs=legs)


def test_known_legs_constant() -> None:
    """KNOWN_LEGS 与路由层的腿名一致。"""
    assert set(KNOWN_LEGS) == {"vector", "keyword", "graph"}


# --------------------------------------------------------------------------- 计划/合并辅助


def test_plan_reports_everything_without_searching() -> None:
    """plan 只算不查: 解析/路由/where/查询变体/腿/fetch_k 全在里面。"""
    query = parse_query("PA601-D54A 表11 输出特性查询").raw
    vector = FakeVectorIndex({len(query): ["c1"]})
    keyword = FakeKeywordIndex({kw_key(query): ["c1"]})
    retriever = _retriever(vector=vector, keyword=keyword, embedder=FakeEmbedder())

    plan = retriever.plan("PA601-D54A 表11 输出特性查询", top_k=5)
    assert plan["routed"].kind.value == "unknown"
    assert plan["requested_legs"] == ("vector", "keyword")
    assert plan["active_legs"] == ("vector", "keyword")
    assert plan["where"] == {"product_code": "PA601-D54A"}  # table 不可等值表达，已留痕
    assert [d["key"] for d in plan["degradations"] if d["kind"] == "where_key_dropped"] == ["table"]
    assert plan["queries"] == ["PA601-D54A 表11 输出特性查询"]
    assert plan["fetch_k"] == 15
    assert [d["kind"] for d in plan["degradations"]] == ["where_key_dropped"]
    assert plan["degradations"][0]["key"] == "table"
    assert vector.calls == [] and keyword.calls == []


def test_retrieve_parsed_reuses_parse_result() -> None:
    """retrieve_parsed 复用 parse_query 结果（不再解析一次）。"""
    parsed = parse_query("PA601-D54A 表11 输出特性查询")
    keyword_query = "输出特性"  # 型号/表号已由 where 承载，停用词"查询"亦被剔除
    vector = FakeVectorIndex({len(parsed.raw): ["c1"]})
    keyword = FakeKeywordIndex({keyword_query: ["c1"]})
    retriever = _retriever(vector=vector, keyword=keyword, embedder=FakeEmbedder())

    hits = retriever.retrieve_parsed(parsed)
    assert [h.chunk_id for h in hits] == ["c1"]
    assert vector.calls[0]["where"] == {"product_code": "PA601-D54A"}
    assert keyword.calls[0]["query"] == keyword_query
    assert keyword.calls[0]["where"] == {"product_code": "PA601-D54A"}
    # 两腿各拿第 1 名 → 同分融合
    assert hits[0].rrf_score == pytest.approx(2 / 61)


def test_keyword_query_strips_where_backed_tokens() -> None:
    """关键词腿查询剥离型号/表号（P1-4 FTS5 AND 匹配下否则整条判空）。"""
    parsed = parse_query("PA601-D54A 表11 最大输出功率是多少")
    retriever = _retriever(vector=FakeVectorIndex({}), keyword=None, embedder=FakeEmbedder())
    assert retriever._keyword_query(parsed, parsed.raw) == "最大 输出功率"


def test_keyword_query_keeps_query_when_all_stripped() -> None:
    """剥完为空 → 回退原查询，不留空查询给索引。"""
    parsed = parse_query("PA601-D54A 表11")
    retriever = _retriever(vector=FakeVectorIndex({}), keyword=None, embedder=FakeEmbedder())
    assert retriever._keyword_query(parsed, parsed.raw) == parsed.raw


def test_retrieval_hit_dataclass_shape() -> None:
    """RetrievalHit 字段齐备。"""
    hit = RetrievalHit(
        chunk_id="c1", doc_id="d", text="t", meta={"page": 1},
        rrf_score=0.5, per_leg={"vector": 0.5, "keyword": None}, legs_hit=("vector",),
    )
    assert hit.chunk_id == "c1" and hit.legs_hit == ("vector",)
    assert hit.per_leg["keyword"] is None


def test_merge_legs_dedupes_and_orders() -> None:
    """merge_legs 去重保序，未知腿名忽略，空输入回默认腿。"""
    assert merge_legs(("vector", "keyword"), ("vector", "graph")) == ("vector", "keyword", "graph")
    assert merge_legs(("bogus",)) == ("vector", "keyword")
    assert merge_legs() == ("vector", "keyword")


def test_degradations_bounded() -> None:
    """降级记录有界，长跑不无限增长。"""
    retriever = _retriever(vector=None, keyword=None, embedder=None)
    for _ in range(retriever_module._MAX_DEGRADATIONS + 50):
        retriever._note({"kind": "test"})
    assert len(retriever.degradations) == retriever_module._MAX_DEGRADATIONS
