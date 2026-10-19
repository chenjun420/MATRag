"""graph_index（图谱检索腿）离线测试: 实体链接口径 + 源卡 chunk 展开。

- 手工构造小图谱（不读盘）: 精确/包含/最长键/边界/单字符键/排序/去重/截断;
- 真实 KG（``build_knowledge_graph``）做端到端链接抽查（module 级构建 1 次）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from matrag.domain.knowledge_graph import (
    KIND_AXIOM,
    KIND_METHOD,
    KIND_TERM,
    KgNode,
    KnowledgeGraph,
    build_knowledge_graph,
)
from matrag.index.store import ScoredChunk
from matrag.retrieve.graph_index import EntityLink, GraphIndex, build_graph_index


def _node(
    node_id: str,
    kind: str,
    label: str,
    *,
    zh: str = "",
    aliases: tuple[str, ...] = (),
    abbr: str = "",
    card: str | None = None,
) -> KgNode:
    """构造带 source 的图谱节点（只放链接用得到的字段）。"""
    source: dict[str, Any] = {}
    if card is not None:
        source = {"kind": "card", "name": f"{card} 标题", "card_id": card}
    return KgNode(
        node_id=node_id,
        kind=kind,
        label=label,
        attrs={
            "zh": zh,
            "en": "",
            "abbr": abbr,
            "aliases": list(aliases),
            "source": source,
        },
    )


def _graph(*nodes: KgNode) -> KnowledgeGraph:
    return KnowledgeGraph(nodes_by_id={node.node_id: node for node in nodes}, edges=())


class _Source:
    """可编程 ``chunk_source`` 替身: ``{doc_id: [(chunk_id, text), ...]}``。"""

    def __init__(self, mapping: dict[str, list[tuple[str, str]]]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, doc_id: str) -> list[ScoredChunk]:
        self.calls.append(doc_id)
        return [
            ScoredChunk(
                chunk_id=chunk_id,
                doc_id=doc_id,
                text=text,
                meta={"kind": "paragraph"},
                score=0.0,
                leg="keyword",
            )
            for chunk_id, text in self.mapping.get(doc_id, [])
        ]


# --------------------------------------------------------------------------- #
# 实体链接


def test_link_exact_prefers_exact_method() -> None:
    graph = _graph(
        _node("axiom:A-OHM", KIND_AXIOM, "欧姆定律", aliases=("欧姆定律",), card="power-knowledge-1"),
    )
    index = GraphIndex(graph, _Source({}))
    links = index.link("欧姆定律")
    assert [(link.entity_id, link.method, link.key) for link in links] == [
        ("axiom:A-OHM", "exact", "欧姆定律")
    ]


def test_contains_uses_longest_key() -> None:
    graph = _graph(
        _node(
            "term:T-RIPPLE",
            KIND_TERM,
            "输出纹波",
            zh="输出纹波",
            aliases=("纹波",),
            card="power-knowledge-8",
        ),
    )
    index = GraphIndex(graph, _Source({}))
    links = index.link("输出纹波测试为什么要限制 20MHz 带宽")
    assert [(link.method, link.key) for link in links] == [("contains", "输出纹波")]


def test_link_order_exact_then_longest() -> None:
    graph = _graph(
        _node("term:T-SHORT", KIND_TERM, "开尔文", zh="开尔文", card="card-s"),
        _node("term:T-LONG", KIND_TERM, "四线开尔文接法", zh="四线开尔文接法", card="card-l"),
    )
    index = GraphIndex(graph, _Source({}))
    links = index.link("四线开尔文接法怎么接")
    assert [link.entity_id for link in links] == ["term:T-LONG", "term:T-SHORT"]
    assert [link.key for link in links] == ["四线开尔文接法", "开尔文"]


def test_exact_outranks_longer_contains() -> None:
    graph = _graph(
        _node("term:T-X", KIND_TERM, "输出纹波测试", zh="输出纹波测试", card="card-x"),
        _node("term:T-Y", KIND_TERM, "输出纹波", zh="输出纹波", card="card-y"),
    )
    index = GraphIndex(graph, _Source({}))
    links = index.link("输出纹波测试")
    assert [(link.entity_id, link.method) for link in links] == [
        ("term:T-X", "exact"),
        ("term:T-Y", "contains"),
    ]


def test_single_cjk_key_not_used_in_contains() -> None:
    graph = _graph(
        _node("term:T-EFFICIENCY", KIND_TERM, "效率", zh="效率", abbr="电", card="power-knowledge-3"),
    )
    index = GraphIndex(graph, _Source({}))
    assert index.link("电压是多少") == []          # "电" 单字不参与包含匹配
    links = index.link("电")                       # 全串精确仍可命中
    assert [link.method for link in links] == ["exact"]


def test_single_ascii_key_requires_word_boundary() -> None:
    graph = _graph(
        _node("term:T-DUTY", KIND_TERM, "占空比", zh="占空比", abbr="d", card="power-knowledge-8"),
    )
    index = GraphIndex(graph, _Source({}))
    assert [link.entity_id for link in index.link("d=0.5 时怎么算")] == ["term:T-DUTY"]
    assert index.link("model-x 说明") == []        # 词内 "d" 不算命中


def test_link_empty_query_and_no_match() -> None:
    graph = _graph(_node("term:T-A", KIND_TERM, "纹波", zh="纹波", card="card-1"))
    index = GraphIndex(graph, _Source({}))
    assert index.link("") == []
    assert index.link("完全无关的文本") == []


def test_entity_link_to_dict() -> None:
    link = EntityLink(
        entity_id="term:T-A", kind=KIND_TERM, method="exact", key="纹波", card_id="power-knowledge-8"
    )
    assert link.to_dict() == {
        "entity_id": "term:T-A",
        "kind": "term",
        "method": "exact",
        "key": "纹波",
        "card_id": "power-knowledge-8",
    }


def test_link_method_node_expands_source_card() -> None:
    """产测方法节点与术语/公理走同一条链接与展开管线。"""
    graph = _graph(
        _node(
            "method:M-RIPPLE-NOISE",
            KIND_METHOD,
            "输出纹波与噪声测试",
            zh="输出纹波与噪声测试",
            aliases=("纹波测试",),
            card="power-method-ripple-noise",
        ),
    )
    source = _Source({"power-method-ripple-noise": [("mc:0", "方法正文")]})
    index = GraphIndex(graph, source)
    links = index.link("纹波测试怎么做")
    assert [(link.entity_id, link.kind, link.method) for link in links] == [
        ("method:M-RIPPLE-NOISE", KIND_METHOD, "contains")
    ]
    chunks = index.search_graph("纹波测试怎么做", top_k=5)
    assert [chunk.chunk_id for chunk in chunks] == ["mc:0"]


# --------------------------------------------------------------------------- #
# 源卡 chunk 展开


def test_search_expands_cards_with_dedupe() -> None:
    graph = _graph(
        _node("term:T-A", KIND_TERM, "纹波电压", zh="纹波电压", card="card-1"),
        _node("term:T-B", KIND_TERM, "纹波电流", zh="纹波电流", card="card-1"),
        _node("term:T-C", KIND_TERM, "输出噪声", zh="输出噪声", card="card-2"),
    )
    source = _Source(
        {
            "card-1": [("card-1:0", "甲"), ("card-1:1", "乙")],
            "card-2": [("card-2:0", "丙")],
        }
    )
    index = GraphIndex(graph, source)
    chunks = index.search_graph("纹波电压与纹波电流和输出噪声", top_k=10)
    assert [chunk.chunk_id for chunk in chunks] == ["card-1:0", "card-1:1", "card-2:0"]
    assert all(chunk.leg == "graph" for chunk in chunks)
    assert [chunk.score for chunk in chunks] == [1.0, 0.5, pytest.approx(1 / 3)]
    assert set(source.calls) == {"card-1", "card-2"}


def test_search_honors_top_k_and_records_links() -> None:
    graph = _graph(_node("term:T-A", KIND_TERM, "纹波", zh="纹波", card="card-1"))
    source = _Source({"card-1": [("c1", "a"), ("c2", "b"), ("c3", "c")]})
    index = GraphIndex(graph, source)
    chunks = index.search_graph("纹波", top_k=2)
    assert [chunk.chunk_id for chunk in chunks] == ["c1", "c2"]
    assert [chunk.score for chunk in chunks] == [1.0, 0.5]
    assert [link.entity_id for link in index.last_links] == ["term:T-A"]
    assert index.search_graph("纹波", top_k=0) == []


def test_search_ignores_where() -> None:
    graph = _graph(_node("term:T-A", KIND_TERM, "纹波", zh="纹波", card="card-1"))
    index = GraphIndex(graph, _Source({"card-1": [("c1", "a")]}))
    chunks = index.search_graph(
        "纹波", top_k=5, where={"product_code": "PA601-D54A", "section_contains": "4"}
    )
    assert [chunk.chunk_id for chunk in chunks] == ["c1"]


def test_entities_without_card_source_yield_nothing() -> None:
    graph = _graph(_node("term:T-A", KIND_TERM, "纹波", zh="纹波", card=None))
    index = GraphIndex(graph, _Source({}))
    links = index.link("纹波")
    assert links and links[0].card_id is None
    assert index.search_graph("纹波", top_k=5) == []


def test_missing_chunk_id_fails_fast() -> None:
    graph = _graph(_node("term:T-A", KIND_TERM, "纹波", zh="纹波", card="card-1"))
    index = GraphIndex(
        graph,
        lambda doc_id: [SimpleNamespace(chunk_id="", doc_id=doc_id, text="", meta={})],
    )
    with pytest.raises(ValueError, match="chunk_id"):
        index.search_graph("纹波", top_k=5)


def test_chunk_source_must_be_callable() -> None:
    with pytest.raises(ValueError, match="chunk_source"):
        GraphIndex(_graph(), "not-callable")  # type: ignore[arg-type]


def test_build_graph_index_factory_accepts_graph() -> None:
    graph = _graph(_node("term:T-A", KIND_TERM, "纹波", zh="纹波", card="card-1"))
    index = build_graph_index(_Source({"card-1": [("c1", "x")]}), graph=graph)
    assert [chunk.chunk_id for chunk in index.search_graph("纹波", top_k=5)] == ["c1"]


# --------------------------------------------------------------------------- #
# 真实 KG 抽查（构建 1 次, 给全部用例复用）


@pytest.fixture(scope="module")
def real_index() -> GraphIndex:
    return GraphIndex(build_knowledge_graph(), _Source({}))


def test_real_kg_links_holdup_card(real_index: GraphIndex) -> None:
    links = real_index.link("保持时间怎么算母线电容")
    holdup = [link for link in links if link.entity_id == "term:T-HOLDUP"]
    assert holdup and holdup[0].card_id == "power-knowledge-8"


def test_real_kg_links_ripple_entity(real_index: GraphIndex) -> None:
    ids = {link.entity_id for link in real_index.link("输出纹波超标可能是什么原因")}
    assert "term:T-RIPPLE" in ids


def test_real_kg_links_ripple_method(real_index: GraphIndex) -> None:
    """产测方法节点参与真实 KG 链接: M-RIPPLE-NOISE → power-method-ripple-noise。"""
    links = real_index.link("输出纹波与噪声测试")
    method = [link for link in links if link.entity_id == "method:M-RIPPLE-NOISE"]
    assert method and method[0].kind == KIND_METHOD
    assert method[0].card_id == "power-method-ripple-noise"


def test_real_kg_single_cjk_alias_not_matched(real_index: GraphIndex) -> None:
    # "电" 是 T-EFFICIENCY 的别名（单字 CJK 键不参与包含匹配）
    ids = {link.entity_id for link in real_index.link("电流是多少")}
    assert "term:T-EFFICIENCY" not in ids
