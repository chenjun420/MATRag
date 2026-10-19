"""chunking（CanonicalDocument → ChunkRecord）离线测试。

覆盖: chunk 数量 / kind / meta 锚点（page/section_path/table_id/caption）、
表头逐行 chunk 文本含表题与表头、表格上方说明文字并入首块、段落长文滑窗切片、
chunk_id 唯一、空 blocks fail-fast。全部离线，无 LLM/网络。
"""

from __future__ import annotations

import pytest

from matrag.index.chunking import (
    OVERLAP_CHARS,
    TARGET_CHARS,
    chunk_document,
)
from matrag.types import (
    Block,
    BlockKind,
    CanonicalDocument,
    SourceKind,
    TableModel,
)


def _doc(blocks: list[Block]) -> CanonicalDocument:
    return CanonicalDocument(
        doc_id="DOC1", title="标题", source_path="/x/DOC1.md",
        source_kind=SourceKind.MARKDOWN, blocks=blocks,
    )


def _sample_doc() -> CanonicalDocument:
    return _doc([
        Block(kind=BlockKind.HEADING, text="4 技术要求", level=1,
              section_path="4 技术要求", page=2),
        Block(kind=BlockKind.NOTE, text="本章说明若干功能指标。",
              section_path="4 技术要求", page=2),
        Block(kind=BlockKind.PARAGRAPH, text="下表列出保护参数。",
              section_path="4 技术要求", page=3),  # 表格上方说明文字（应被吸收）
        Block(kind=BlockKind.TABLE, section_path="4 技术要求", page=3,
              table=TableModel(
                  id="T1", caption="表1 保护参数",
                  headers=["项目", "欠压点", "单位"],
                  rows=[["欠压保护", "68", "V"], ["过流保护", "5.2", "A"]],
              )),
    ])


# --------------------------------------------------------------------------- #


def test_empty_blocks_fail_fast() -> None:
    with pytest.raises(ValueError, match="无 blocks"):
        chunk_document(_doc([]))


def _domain_card_doc() -> CanonicalDocument:
    """域知识卡片（SourceKind.DOMAIN）: 公共知识，不绑型号。"""
    return CanonicalDocument(
        doc_id="power-knowledge-8", title="降压占空比", source_path="power_knowledge.md",
        source_kind=SourceKind.DOMAIN,
        blocks=[Block(kind=BlockKind.HEADING, text="降压占空比", level=1,
                      section_path="电压调整", page=0),
                Block(kind=BlockKind.PARAGRAPH, text="降压占空比 D = Vout/Vin。",
                      section_path="电压调整", page=0)],
    )


def test_domain_card_is_public_scope_not_a_product() -> None:
    """域知识卡片是**公共知识**： scope=public, product_code 留空。

    若把卡片名当型号（power-knowledge-8），它会被型号过滤误排除——
    "通用经验"变成"某型号专属"，语义错误。
    """
    recs = chunk_document(_domain_card_doc())
    assert recs
    for rec in recs:
        assert rec.meta["scope"] == "public"
        assert rec.meta["product_code"] == ""


def test_spec_doc_scope_is_product() -> None:
    """规格书 chunk 仍是型号负责（scope=product）, 可按型号过滤。"""
    recs = chunk_document(_sample_doc())
    assert {r.meta["scope"] for r in recs} == {"product"}


def test_kinds_and_anchors() -> None:
    recs = chunk_document(_sample_doc())
    kinds = [r.meta["kind"] for r in recs]
    # heading→section, note→note, 表格 2 行→table_row × 2；说明段落被吸收不单独成块
    assert kinds == ["section", "note", "table_row", "table_row"]
    for r in recs:
        assert r.doc_id == "DOC1"
        assert r.meta["section_path"] == "4 技术要求"
    assert recs[0].meta["page"] == 2
    assert recs[2].meta["page"] == 3


def test_table_row_text_has_caption_and_headers() -> None:
    recs = [r for r in chunk_document(_sample_doc()) if r.meta["kind"] == "table_row"]
    assert len(recs) == 2
    for r in recs:
        assert "表1 保护参数" in r.text          # 表题
        assert "项目 | 欠压点 | 单位" in r.text    # 表头
        assert r.meta["table_id"] == "T1"
        assert r.meta["caption"] == "表1 保护参数"
    # 逐行: 第一行含 68/欠压保护，第二行含 5.2/过流保护，互不含对方的数据行
    assert "68" in recs[0].text and "5.2" not in recs[0].text
    assert "5.2" in recs[1].text and "68" not in recs[1].text


def test_preamble_merged_into_first_row_only() -> None:
    recs = chunk_document(_sample_doc())
    # 说明文字"下表列出保护参数。"被吸收进第一块 table_row，不再单独成段
    assert "下表列出保护参数。" not in [r.text for r in recs if r.meta["kind"] == "paragraph"]
    first_row = next(r for r in recs if r.meta["kind"] == "table_row")
    assert first_row.text.startswith("下表列出保护参数。")
    second_row = [r for r in recs if r.meta["kind"] == "table_row"][1]
    assert not second_row.text.startswith("下表列出保护参数。")


def test_preamble_truncated_to_limit() -> None:
    long_note = "说明" * 300  # 远超 200 字
    doc = _doc([
        Block(kind=BlockKind.PARAGRAPH, text=long_note, section_path="S", page=1),
        Block(kind=BlockKind.TABLE, section_path="S", page=1,
              table=TableModel(id="T", caption="cap", headers=["a"], rows=[["b"]])),
    ])
    recs = chunk_document(doc)
    row = next(r for r in recs if r.meta["kind"] == "table_row")
    preamble = row.text.split("\n", 1)[0]
    assert len(preamble) <= 200


def test_chunk_id_unique_and_formatted() -> None:
    recs = chunk_document(_sample_doc())
    ids = [r.chunk_id for r in recs]
    assert len(set(ids)) == len(ids)
    assert all(i.startswith("DOC1:") for i in ids)
    assert ids == [f"DOC1:{n:04d}" for n in range(len(ids))]


def test_section_titles_in_meta() -> None:
    """meta 带 section_titles: section_path 按 " > " 拆开的各级标题。"""
    doc = _doc([
        Block(kind=BlockKind.HEADING, text="4.3.1 输入特性", level=3,
              section_path="4 > 4.3 > 4.3.1 输入特性", page=1),
        Block(kind=BlockKind.PARAGRAPH, text="本节目录说明输入特性。",
              section_path="4 > 4.3 > 4.3.1 输入特性", page=1),
    ])
    recs = chunk_document(doc)
    for r in recs:
        assert r.meta["section_titles"] == ["4", "4.3", "4.3.1 输入特性"]
        assert r.meta["section_path"] == "4 > 4.3 > 4.3.1 输入特性"


def test_section_titles_empty_when_no_section_path() -> None:
    """无 section_path（标题之前的块）→ section_titles 为空列表，不报错。"""
    doc = _doc([
        Block(kind=BlockKind.PARAGRAPH, text="正文", section_path="", page=1),
    ])
    assert chunk_document(doc)[0].meta["section_titles"] == []


def test_section_titles_on_table_rows() -> None:
    """表格行块同样带 section_titles（emitter 统一注入，按章节名过滤一致生效）。"""
    doc = _doc([
        Block(kind=BlockKind.HEADING, text="4.3.1 输入特性", level=3,
              section_path="4 > 4.3 > 4.3.1 输入特性", page=1),
        Block(kind=BlockKind.TABLE, section_path="4 > 4.3 > 4.3.1 输入特性", page=1,
              table=TableModel(id="T", caption="cap", headers=["a"], rows=[["b"]])),
    ])
    recs = [r for r in chunk_document(doc) if r.meta["kind"] == "table_row"]
    assert recs and recs[0].meta["section_titles"] == ["4", "4.3", "4.3.1 输入特性"]


def test_standalone_paragraph_not_absorbed() -> None:
    doc = _doc([
        Block(kind=BlockKind.PARAGRAPH, text="普通段落，不在表格上方。",
              section_path="S", page=1),
        Block(kind=BlockKind.HEADING, text="5 下一节", level=1,
              section_path="5 下一节", page=2),
    ])
    recs = chunk_document(doc)
    paras = [r for r in recs if r.meta["kind"] == "paragraph"]
    assert len(paras) == 1
    assert paras[0].text == "普通段落，不在表格上方。"


# --------------------------------------------------------------------------- #
# 段落长文滑窗切片
# --------------------------------------------------------------------------- #
def test_paragraph_sliding_window() -> None:
    # 构造 > TARGET 的长文，每句以句号分隔，无超长单句
    sentence = "这是第%03d句测试内容。"  # 8 字
    para = "".join(sentence % i for i in range(200))  # 远大于 600
    assert len(para) > TARGET_CHARS
    doc = _doc([Block(kind=BlockKind.PARAGRAPH, text=para, section_path="S", page=1)])
    recs = chunk_document(doc)
    assert len(recs) > 1
    for r in recs:
        assert r.meta["kind"] == "paragraph"
        assert len(r.text) <= TARGET_CHARS + 2  # 允许含一个边界字符的微小溢出
    # 相邻块之间有重叠内容（衔接上一段尾部）
    overlap_ok = any(
        recs[i].text[-OVERLAP_CHARS:] and recs[i].text[-OVERLAP_CHARS:] in recs[i + 1].text
        for i in range(len(recs) - 1)
    )
    assert overlap_ok


def test_short_paragraph_single_chunk() -> None:
    doc = _doc([Block(kind=BlockKind.PARAGRAPH, text="短段落。",
                      section_path="S", page=1)])
    recs = chunk_document(doc)
    assert len(recs) == 1
    assert recs[0].text == "短段落。"


def test_pagebreak_and_list() -> None:
    doc = _doc([
        Block(kind=BlockKind.LIST, text="条目一；条目二。", section_path="S", page=1),
        Block(kind=BlockKind.PAGEBREAK, section_path="S", page=1),
    ])
    recs = chunk_document(doc)
    # PAGEBREAK 不成块；LIST 归为 paragraph
    assert [r.meta["kind"] for r in recs] == ["paragraph"]


def test_empty_table_no_crash() -> None:
    doc = _doc([
        Block(kind=BlockKind.TABLE, section_path="S", page=1,
              table=TableModel(id="T", caption="空表", headers=["a", "b"], rows=[])),
    ])
    recs = chunk_document(doc)
    # 无数据行 → 退化为一条仅表头/表题块
    assert len(recs) == 1
    assert "空表" in recs[0].text and "a | b" in recs[0].text


def test_section_fallback_without_block_path() -> None:
    # blocks 未逐块带 section_path 时，heading 之后的块沿用该节上下文
    doc = _doc([
        Block(kind=BlockKind.HEADING, text="9 附录", level=1,
              section_path="9 附录", page=1),
        Block(kind=BlockKind.PARAGRAPH, text="正文", page=1),
    ])
    recs = chunk_document(doc)
    assert recs[1].meta["section_path"] == "9 附录"
