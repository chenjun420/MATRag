"""keyword（关键词索引腿 KeywordIndex，FTS5+jieba+bm25）离线测试。

覆盖: jieba 中文命中、bm25 排序方向（负分转正 / tf 高更靠前）、where SQL 层过滤、
空查询、无结果、tokenize 静态方法、count / 重开保持 / 重入库幂等、FTS5 可用性、
与 VectorIndex 同库共存。全部离线。
"""

from __future__ import annotations

import hashlib
from typing import Any

import jieba
import pytest

from matrag.index.chunking import ChunkRecord
from matrag.index.keyword import KeywordIndex, bm25_scores, fts5_available
from matrag.index.store import VectorIndex

jieba.setLogLevel(60)


def _records() -> list[ChunkRecord]:
    return [
        ChunkRecord("D:0000", "D", "该设备提供输出欠压保护功能，欠压点 68V",
                    {"kind": "paragraph", "page": 1, "section_path": "4"}),
        ChunkRecord("D:0001", "D", "过流保护阈值 5.2A",
                    {"kind": "table_row", "page": 2, "section_path": "4",
                     "table_id": "T1", "caption": "表1"}),
        ChunkRecord("E:0000", "E", "欠压 欠压 欠压 提及频繁",
                    {"kind": "paragraph", "page": 1, "section_path": "7"}),
    ]


@pytest.fixture
def ki(tmp_path) -> KeywordIndex:
    idx = KeywordIndex(tmp_path / "kw.db")
    idx.add_chunks(_records())
    yield idx
    idx.close()


# --------------------------------------------------------------------------- #
# chunks_by_doc（图谱腿的源卡展开）


def test_chunks_by_doc_all_in_insert_order(ki: KeywordIndex) -> None:
    chunks = ki.chunks_by_doc("D")
    assert [c.chunk_id for c in chunks] == ["D:0000", "D:0001"]
    assert chunks[0].text == _records()[0].text
    assert chunks[1].meta.get("table_id") == "T1"


def test_chunks_by_doc_unknown_doc_empty(ki: KeywordIndex) -> None:
    assert ki.chunks_by_doc("NOPE") == []


def test_chunks_by_doc_blank_id_and_bad_limit_fail(ki: KeywordIndex) -> None:
    with pytest.raises(ValueError):
        ki.chunks_by_doc("   ")
    with pytest.raises(ValueError):
        ki.chunks_by_doc("D", limit=0)


def test_chunks_by_doc_limit(ki: KeywordIndex) -> None:
    assert [c.chunk_id for c in ki.chunks_by_doc("D", limit=1)] == ["D:0000"]


# --------------------------------------------------------------------------- #


def test_jieba_chinese_hit(ki: KeywordIndex) -> None:
    # "欠压保护" → jieba [欠压, 保护]；AND 优先、不足回退 OR（bm25 排序）。
    # 同时含两词的 D:0000 排首位；只含其一的块作为召回补充出现（检索腿召回契约）。
    hits = ki.search_keyword("欠压保护", top_k=5)
    ids = [h.chunk_id for h in hits]
    assert ids and ids[0] == "D:0000"
    assert hits[0].leg == "keyword"
    assert hits[0].text == _records()[0].text


def test_bm25_order_direction(ki: KeywordIndex) -> None:
    # "欠压" → D:0000(tf=1) 与 E:0000(tf=3 且更短)。tf 高者更相关，排最前；分数越大越相关
    hits = ki.search_keyword("欠压", top_k=5)
    ids = [h.chunk_id for h in hits]
    assert set(ids) == {"D:0000", "E:0000"}
    assert hits[0].chunk_id == "E:0000"                         # tf 更高居首
    assert all(h.score > 0 for h in hits)                       # 正分数约定
    assert hits[0].score >= hits[1].score                       # 越大越相关（降序）
    # SQL 层即按 bm25 升序，转换后严格非增
    assert all(hits[i].score >= hits[i + 1].score for i in range(len(hits) - 1))


def test_bm25_scores_sign_convention() -> None:
    assert bm25_scores([-1.0, -2.0, 0.0]) == [1.0, 2.0, 0.0]


def test_where_filter_doc(ki: KeywordIndex) -> None:
    hits = ki.search_keyword("欠压", top_k=5, where={"doc_id": "E"})
    assert [h.chunk_id for h in hits] == ["E:0000"]


def test_where_filter_kind(ki: KeywordIndex) -> None:
    hits = ki.search_keyword("保护", top_k=5, where={"kind": "paragraph"})
    assert hits and all(h.meta.get("kind") == "paragraph" for h in hits)
    assert "D:0001" not in [h.chunk_id for h in hits]  # table_row 被过滤掉


def test_where_unknown_key_fails_fast(ki: KeywordIndex) -> None:
    with pytest.raises(ValueError, match="过滤键"):
        ki.search_keyword("欠压", top_k=5, where={"not_a_column": 1})


# --------------------------------------------------------------------------- #
# section_contains（章节名/章节号 的"包含"过滤）


def _section_records() -> list[ChunkRecord]:
    """三章各一条 chunk: section_path 含章节号 + 章节名 + 字面通配符, 与真实语料同形。"""
    return [
        ChunkRecord("D:0000", "D", "本章 输入特性 说明 电压范围",
                    {"kind": "paragraph", "page": 1,
                     "section_path": "4 > 4.3 > 4.3.1 输入特性"}),
        ChunkRecord("D:0001", "D", "本章 输出特性 说明 功率范围",
                    {"kind": "paragraph", "page": 2,
                     "section_path": "4 > 4.3 > 4.3.2 输出特性"}),
        ChunkRecord("D:0002", "D", "本章 混合 标题 说明 裕度",
                    {"kind": "paragraph", "page": 3,
                     "section_path": "4 > 10% 裕度_测试 > 附注"}),
    ]


@pytest.fixture
def sect_ki(tmp_path) -> KeywordIndex:
    idx = KeywordIndex(tmp_path / "sect.db")
    idx.add_chunks(_section_records())
    yield idx
    idx.close()


def _ids_with(idx: KeywordIndex, value: Any) -> list[str]:
    """以"本章"查询 + 给定 section_contains 值, 返回命中 chunk_id 列表。"""
    hits = idx.search_keyword("本章", top_k=10, where={"section_contains": value})
    return [h.chunk_id for h in hits]


def test_where_section_contains_filters_by_substring(sect_ki: KeywordIndex) -> None:
    """where={"section_contains": "输入特性"} 只返回该章节下的 chunk。"""
    hits = sect_ki.search_keyword("本章", top_k=10, where={"section_contains": "输入特性"})
    assert [h.chunk_id for h in hits] == ["D:0000"]
    # 章节号同样走"包含"过滤（section_path 里含 "4.3.2"）
    hits = sect_ki.search_keyword("本章", top_k=10, where={"section_contains": "4.3.2"})
    assert [h.chunk_id for h in hits] == ["D:0001"]
    # 不命中任何章节 → 空
    assert sect_ki.search_keyword("本章", top_k=10, where={"section_contains": "第9章"}) == []


def test_where_section_contains_unknown_key_error_still_fires(sect_ki: KeywordIndex) -> None:
    """section_contains 之外的未知键仍然 fail-fast（它不进白名单）。"""
    with pytest.raises(ValueError, match="过滤键"):
        sect_ki.search_keyword("本章", top_k=5, where={"section_contains": "输入特性", "bogus": 1})


def test_where_section_contains_composes_with_equality(sect_ki: KeywordIndex) -> None:
    """section_contains 与等值键可组合（AND LIKE + AND 等值）。"""
    hits = sect_ki.search_keyword(
        "本章", top_k=10,
        where={"section_contains": "输入特性", "doc_id": "D"},
    )
    assert [h.chunk_id for h in hits] == ["D:0000"]
    # 等值键不匹配时为空（章节过滤没有绕过等值约束）
    assert sect_ki.search_keyword(
        "本章", top_k=10, where={"section_contains": "输入特性", "doc_id": "OTHER"}
    ) == []


def test_where_section_contains_escapes_like_wildcards(sect_ki: KeywordIndex) -> None:
    """LIKE 通配符 % / _ 转义: 按字面匹配, 不会"匹配全部"或炸掉。

    ``%`` 若未转义会匹配任意长度字符串 → 三条全返回（错误）; 转义后只有路径里
    真有字面 ``%`` 的那条（"4 > 10% 裕度_测试 > 附注"）命中。
    """
    assert _ids_with(sect_ki, "%") == ["D:0002"]
    # "_" 未转义会匹配任意单字符 → 三条全返回; 转义后只有字面含 "_" 的命中
    assert _ids_with(sect_ki, "_") == ["D:0002"]
    # 字面值精确匹配
    assert _ids_with(sect_ki, "10%") == ["D:0002"]
    assert _ids_with(sect_ki, "裕度_测试") == ["D:0002"]
    # 路径里没有这个字面值 → 无命中（不是"匹配任意"）
    assert _ids_with(sect_ki, "9% 裕度") == []
    # 转义符本身也能按字面出现（不炸）
    assert _ids_with(sect_ki, "\\") == []


def test_where_section_contains_list_requires_all(sect_ki: KeywordIndex) -> None:
    """list → 多个 AND LIKE: 全部包含才算命中。"""
    hits = sect_ki.search_keyword(
        "本章", top_k=10, where={"section_contains": ["4.3", "输入特性"]}
    )
    assert [h.chunk_id for h in hits] == ["D:0000"]
    # "4.3.2" 与 "输入特性" 不同章 → 无命中
    assert sect_ki.search_keyword(
        "本章", top_k=10, where={"section_contains": ["4.3.2", "输入特性"]}
    ) == []


def test_where_section_contains_rejects_bad_types(sect_ki: KeywordIndex) -> None:
    """section_contains 的值类型不合法 → fail-fast（过滤条件写错比不过滤更危险）。"""
    for bad in (11, 0, {"a": 1}, ["输入特性", 3]):
        with pytest.raises(ValueError, match="section_contains"):
            sect_ki.search_keyword("本章", top_k=5, where={"section_contains": bad})


def test_where_section_contains_does_not_mutate_caller_dict(sect_ki: KeywordIndex) -> None:
    """不得原地改调用方的 where（两条腿共享同一个 dict 对象）。"""
    where = {"section_contains": "输入特性", "doc_id": "D"}
    sect_ki.search_keyword("本章", top_k=10, where=where)
    assert where == {"section_contains": "输入特性", "doc_id": "D"}


def _scope_records() -> list[ChunkRecord]:
    """型号 A / 型号 B / 公共知识（scope=public, product_code 为空）。"""
    return [
        ChunkRecord(chunk_id="A:0000", doc_id="A 规格书", text="输出纹波测量带宽",
                    meta={"product_code": "A", "scope": "product"}),
        ChunkRecord(chunk_id="B:0000", doc_id="B 规格书", text="输出纹波测量带宽",
                    meta={"product_code": "B", "scope": "product"}),
        ChunkRecord(chunk_id="K:0000", doc_id="power-knowledge-8", text="输出纹波测量带宽的通用经验",
                    meta={"product_code": "", "scope": "public"}),
    ]


@pytest.fixture
def scope_ki(tmp_path) -> KeywordIndex:
    ki = KeywordIndex(tmp_path / "scope.db")
    ki.add_chunks(_scope_records())
    yield ki
    ki.close()


def test_product_filter_keeps_public_knowledge(scope_ki: KeywordIndex) -> None:
    """关键词腿的型号过滤同样保留公共知识（两腿口径必须一致）。"""
    hits = scope_ki.search_keyword("输出纹波测量带宽", top_k=10,
                                   where={"product_code": "A"})
    doc_ids = {c.doc_id for c in hits}
    assert "A 规格书" in doc_ids
    assert "power-knowledge-8" in doc_ids
    assert "B 规格书" not in doc_ids


def test_empty_and_no_hit(ki: KeywordIndex) -> None:
    assert ki.search_keyword("") == []
    assert ki.search_keyword("   ") == []
    assert ki.search_keyword("完全不存在词xyzqw") == []


def test_tokenize_static() -> None:
    toks = KeywordIndex.tokenize("输出的欠压保护")
    assert "欠压" in toks and "保护" in toks
    assert "的" not in toks                       # 停用词剔除
    assert KeywordIndex.tokenize("，，，。") == []  # 纯标点剔除
    assert KeywordIndex.tokenize("the a of") == []  # 英文停用词剔除


def test_count_and_reopen(tmp_path) -> None:
    db = tmp_path / "kw.db"
    ki = KeywordIndex(db)
    ki.add_chunks(_records())
    n = ki.count()
    ki.close()
    # 重开仍能搜
    ki2 = KeywordIndex(db)
    assert ki2.count() == n
    # AND 优先、不足回退 OR（bm25 排序）: 同时含"欠压+保护"的 D:0000 排首位,
    # 只含其一的块作为召回补充出现——这是检索腿的召回契约, 不是失败降级。
    ids = [h.chunk_id for h in ki2.search_keyword("欠压保护", top_k=5)]
    assert ids and ids[0] == "D:0000"
    ki2.close()


def test_readd_is_idempotent(tmp_path) -> None:
    ki = KeywordIndex(tmp_path / "kw.db")
    ki.add_chunks(_records())
    ki.add_chunks(_records())  # 同 doc_id 先删后插
    assert ki.count() == len(_records())
    ki.close()


def test_fts5_available() -> None:
    assert fts5_available() is True


def test_coexist_with_vector_index(tmp_path) -> None:
    # 同一 db 文件: 向量腿(vec0) 与关键词腿(fts5) 不同虚表共存，互不干扰
    db = tmp_path / "both.db"
    recs = _records()
    vi = VectorIndex(db, dimension=3, embed_model="stub")

    def vec(text: str) -> list[float]:
        h = hashlib.sha256(text.encode()).digest()
        return [h[i % len(h)] / 255.0 for i in range(3)]

    vi.add_chunks(recs, vectors=[vec(r.text) for r in recs])
    ki = KeywordIndex(db)
    ki.add_chunks(recs)

    assert vi.count() == len(recs)
    assert ki.count() == len(recs)
    # 两腿各自 where 过滤
    assert all(h.doc_id == "D" for h in
               vi.search_vector(vec(recs[0].text), top_k=10, where={"doc_id": "D"}))
    assert all(h.doc_id == "E" for h in
               ki.search_keyword("欠压", top_k=10, where={"doc_id": "E"}))

    vi.close()
    ki.close()
