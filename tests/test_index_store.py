"""store（向量索引腿 VectorIndex）离线测试（stub 向量，零网络 / 零 LLM）。

覆盖: add → search_vector top_k、命中结构（leg/text/doc_id/meta）、where 等值过滤、
维度不符 fail-fast（add 与 search 两个方向）、条数不一致 fail-fast、重开 db count 保持、
stats 字段、embed_model 记录、空查询。向量按文本 sha256 生成，确定性 4 维。
"""

from __future__ import annotations

import hashlib

import pytest

from matrag.index.chunking import ChunkRecord
from matrag.index.store import ScoredChunk, VectorIndex

DIM = 4


def _vec(text: str, dim: int = DIM) -> list[float]:
    """确定性伪向量: sha256(text) → [0,1]^dim（同文本恒同，异文本高概率不同）。"""
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [h[i % len(h)] / 255.0 for i in range(dim)]


def _records(n: int = 5) -> list[ChunkRecord]:
    return [
        ChunkRecord(
            chunk_id=f"D{i % 2}:{i:04d}",
            doc_id=f"D{i % 2}",
            text=f"内容 {i} 欠压保护 测试样本",
            meta={"kind": "paragraph", "page": i, "section_path": f"4.{i}"},
        )
        for i in range(n)
    ]


@pytest.fixture
def idx(tmp_path):
    vi = VectorIndex(tmp_path / "vec.db", dimension=DIM, embed_model="stub-embed")
    recs = _records(5)
    vi.add_chunks(recs, vectors=[_vec(r.text) for r in recs])
    yield vi, recs
    vi.close()


# --------------------------------------------------------------------------- #


def test_add_and_topk(idx) -> None:
    vi, recs = idx
    assert vi.count() == 5
    hits = vi.search_vector(_vec(recs[2].text), top_k=1)
    assert isinstance(hits[0], ScoredChunk)
    assert hits[0].chunk_id == recs[2].chunk_id   # 用自身向量查，恒为 top-1
    assert hits[0].leg == "vector"
    assert hits[0].text == recs[2].text
    assert hits[0].doc_id == recs[2].doc_id
    assert hits[0].score == pytest.approx(1.0, abs=1e-6)  # 余弦距离 0
    # top_k 截断
    assert len(vi.search_vector(_vec(recs[0].text), top_k=3)) == 3


def test_where_filter(idx) -> None:
    vi, recs = idx
    all_hits = vi.search_vector(_vec(recs[0].text), top_k=10)
    assert {h.doc_id for h in all_hits} == {"D0", "D1"}
    filtered = vi.search_vector(_vec(recs[0].text), top_k=10, where={"doc_id": "D0"})
    assert filtered and all(h.doc_id == "D0" for h in filtered)


def test_dim_mismatch_add_fails_loud(tmp_path) -> None:
    vi = VectorIndex(tmp_path / "v.db", dimension=DIM, embed_model="stub")
    recs = _records(1)
    with pytest.raises(RuntimeError, match="维度"):
        vi.add_chunks(recs, vectors=[[0.1, 0.2, 0.3]])  # 3 维 ≠ 4
    assert vi.count() == 0  # fail-fast，未写入
    vi.close()


def test_dim_mismatch_search_fails_loud(idx) -> None:
    vi, _ = idx
    with pytest.raises(RuntimeError, match="维度"):
        vi.search_vector([0.1, 0.2, 0.3], top_k=1)


def test_count_mismatch_fails_loud(tmp_path) -> None:
    vi = VectorIndex(tmp_path / "v.db", dimension=DIM, embed_model="stub")
    recs = _records(2)
    with pytest.raises(RuntimeError, match="不一致"):
        vi.add_chunks(recs, vectors=[_vec(recs[0].text)])  # 2 记录 1 向量
    assert vi.count() == 0
    vi.close()


def test_reopen_preserves_count(tmp_path) -> None:
    db = tmp_path / "vec.db"
    vi = VectorIndex(db, dimension=DIM, embed_model="stub")
    recs = _records(4)
    vi.add_chunks(recs, vectors=[_vec(r.text) for r in recs])
    vi.close()
    # 重开同一文件仍可搜、count 保持、向量可命中
    vi2 = VectorIndex(db, dimension=DIM, embed_model="stub")
    assert vi2.count() == 4
    hits = vi2.search_vector(_vec(recs[1].text), top_k=1)
    assert hits[0].chunk_id == recs[1].chunk_id
    vi2.close()


def test_stats_fields(tmp_path) -> None:
    db = tmp_path / "vec.db"
    vi = VectorIndex(db, dimension=DIM, embed_model="doubao-embedding-vision")
    recs = _records(3)
    vi.add_chunks(recs, vectors=[_vec(r.text) for r in recs])
    s = vi.stats()
    assert s["db_path"] == str(db)
    assert s["table"] == "chunks_vec"
    assert s["count"] == 3
    assert s["dimension"] == DIM
    assert s["embed_model"] == "doubao-embedding-vision"
    vi.close()


def test_empty_add_is_noop(tmp_path) -> None:
    vi = VectorIndex(tmp_path / "v.db", dimension=DIM, embed_model="stub")
    vi.add_chunks([], vectors=[])
    assert vi.count() == 0
    vi.close()


def test_top_k_zero_returns_empty(tmp_path) -> None:
    vi = VectorIndex(tmp_path / "v.db", dimension=DIM, embed_model="stub")
    vi.add_chunks(_records(2), vectors=[_vec(r.text) for r in _records(2)])
    assert vi.search_vector(_vec("x"), top_k=0) == []
    vi.close()


# --------------------------------------------------------------------------- #
# section_contains（向量腿 Python 侧后过滤: semantica 只做等值）


def _section_records() -> list[ChunkRecord]:
    """三章各一条: 前两条带 section_titles, 第三条故意缺省（回退 section_path）。"""
    return [
        ChunkRecord("D0:0000", "D0", "输入特性 内容",
                    {"kind": "paragraph", "page": 1,
                     "section_path": "4 > 4.3 > 4.3.1 输入特性",
                     "section_titles": ["4", "4.3", "4.3.1 输入特性"]}),
        ChunkRecord("D1:0000", "D1", "输出特性 内容",
                    {"kind": "paragraph", "page": 2,
                     "section_path": "4 > 4.3 > 4.3.2 输出特性",
                     "section_titles": ["4", "4.3", "4.3.2 输出特性"]}),
        ChunkRecord("D2:0000", "D2", "附注 内容",
                    {"kind": "paragraph", "page": 3,
                     "section_path": "4 > 4.3"}),
    ]


@pytest.fixture
def sect_idx(tmp_path):
    vi = VectorIndex(tmp_path / "vec.db", dimension=DIM, embed_model="stub-embed")
    recs = _section_records()
    vi.add_chunks(recs, vectors=[_vec(r.text) for r in recs])
    yield vi, recs
    vi.close()


def test_section_contains_filters_by_section_titles(sect_idx) -> None:
    """按 meta.section_titles 做包含过滤: 只返回目标章节（含子节）下的 chunk。"""
    vi, recs = sect_idx
    query = _vec(recs[0].text)
    hits = vi.search_vector(query, top_k=10, where={"section_contains": "输入特性"})
    assert [h.chunk_id for h in hits] == ["D0:0000"]
    hits = vi.search_vector(query, top_k=10, where={"section_contains": "4.3.2"})
    assert [h.chunk_id for h in hits] == ["D1:0000"]
    # 无命中 → 空
    assert vi.search_vector(query, top_k=10, where={"section_contains": "第9章"}) == []


def test_section_contains_falls_back_to_section_path(sect_idx) -> None:
    """meta 缺 section_titles → 回退 section_path 包含判定（老索引不炸）。"""
    vi, recs = sect_idx
    hits = vi.search_vector(_vec(recs[0].text), top_k=10, where={"section_contains": "4 > 4.3"})
    assert {h.chunk_id for h in hits} == {"D0:0000", "D1:0000", "D2:0000"}


def test_section_contains_list_requires_all(sect_idx) -> None:
    """list → 全部包含才算命中。"""
    vi, recs = sect_idx
    query = _vec(recs[0].text)
    hits = vi.search_vector(query, top_k=10, where={"section_contains": ["4.3", "输入特性"]})
    assert [h.chunk_id for h in hits] == ["D0:0000"]
    assert vi.search_vector(query, top_k=10, where={"section_contains": ["4.3.2", "输入特性"]}) == []


def test_section_contains_composes_with_equality(sect_idx) -> None:
    """section_contains 与等值过滤组合（先等值 SQL 过滤, 再 Python 侧包含过滤）。"""
    vi, recs = sect_idx
    query = _vec(recs[0].text)
    hits = vi.search_vector(query, top_k=10, where={"section_contains": "输入特性", "doc_id": "D0"})
    assert [h.chunk_id for h in hits] == ["D0:0000"]
    assert vi.search_vector(query, top_k=10, where={"section_contains": "输入特性", "doc_id": "OTHER"}) == []


def test_section_contains_truncates_to_top_k(sect_idx) -> None:
    """后过滤后仍截到 top_k（预取放大不外溢）。"""
    vi, recs = sect_idx
    hits = vi.search_vector(_vec(recs[0].text), top_k=1, where={"section_contains": "4"})
    assert len(hits) == 1


def test_section_contains_empty_value_means_no_filter(sect_idx) -> None:
    """空串/空 list → 不加包含过滤（等价于不过滤）。"""
    vi, recs = sect_idx
    for value in ("", []):
        assert len(vi.search_vector(_vec(recs[0].text), top_k=10, where={"section_contains": value})) == 3


def test_section_contains_rejects_bad_types(sect_idx) -> None:
    """值类型不合法 → fail-fast（过滤条件写错比不过滤更危险）。"""
    vi, recs = sect_idx
    for bad in (11, 0, {"a": 1}, ["输入特性", 3]):
        with pytest.raises(ValueError, match="section_contains"):
            vi.search_vector(_vec(recs[0].text), top_k=5, where={"section_contains": bad})


def _mixed_scope_records() -> list[ChunkRecord]:
    """一个型号 + 一个公共知识 + 一个其它型号。"""
    return [
        ChunkRecord(chunk_id="A:0000", doc_id="A 规格书", text="输出纹波 规格",
                    meta={"product_code": "A", "scope": "product", "section_path": "4.3"}),
        ChunkRecord(chunk_id="K:0000", doc_id="power-knowledge-8", text="降压占空比 公共知识",
                    meta={"product_code": "", "scope": "public", "section_path": "电压调整"}),
        ChunkRecord(chunk_id="B:0000", doc_id="B 规格书", text="输出纹波 规格",
                    meta={"product_code": "B", "scope": "product", "section_path": "4.3"}),
    ]


@pytest.fixture
def scope_idx(tmp_path):
    vi = VectorIndex(tmp_path / "scope.db", dimension=DIM, embed_model="stub-embed")
    recs = _mixed_scope_records()
    vi.add_chunks(recs, vectors=[_vec(r.text) for r in recs])
    yield vi, recs
    vi.close()


def test_product_filter_keeps_public_knowledge(scope_idx) -> None:
    """型号过滤不得误杀公共知识（scope=public 对所有型号成立）。"""
    vi, _ = scope_idx
    hits = vi.search_vector(_vec("输出纹波 规格"), top_k=10,
                            where={"product_code": "A"})
    doc_ids = {c.doc_id for c in hits}
    assert "A 规格书" in doc_ids
    assert "power-knowledge-8" in doc_ids   # 公共知识不属于型号 -> 不能被排除
    assert "B 规格书" not in doc_ids  # 其它型号必须排除


def test_product_filter_does_not_mutate_caller_dict(scope_idx) -> None:
    """两腿共享 where dict: 向量腿拆键后不得把关键词腿的条件也拆掉。"""
    vi, _ = scope_idx
    where = {"product_code": "A"}
    vi.search_vector(_vec("输出纹波 规格"), top_k=10, where=where)
    assert where == {"product_code": "A"}


def test_section_contains_does_not_mutate_caller_dict(sect_idx) -> None:
    """不得原地改调用方的 where（两条腿共享同一个 dict 对象）。"""
    vi, recs = sect_idx
    where = {"section_contains": "输入特性", "doc_id": "D0"}
    vi.search_vector(_vec(recs[0].text), top_k=10, where=where)
    assert where == {"section_contains": "输入特性", "doc_id": "D0"}
