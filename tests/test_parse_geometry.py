"""geometry（PDF 几何 provider）离线测试。

只读仓库 ``docs/`` 下的样例，不触网、不调 LLM。
"""

from __future__ import annotations

import pytest

from matrag.config import REPO_ROOT
from matrag.parse.geometry import extract_pdf
from matrag.types import BlockKind

DOCS = REPO_ROOT / "docs"
CY0177 = DOCS / "CY0177-75V-20A.pdf"
GDC = DOCS / "GDC800T5054-UC.pdf"


@pytest.fixture(scope="module")
def cy0177():
    return extract_pdf(CY0177)


@pytest.fixture(scope="module")
def gdc():
    return extract_pdf(GDC)


def _total_tables(doc) -> int:
    return sum(len(pg.table_drafts) for pg in doc.pages)


def _grid_text(draft) -> str:
    return "\n".join(c.text for row in draft.grid for c in row if c is not None)


# --------------------------------------------------------------------------- #
# CY0177-75V-20A.pdf
# --------------------------------------------------------------------------- #
def test_cy0177_page_count(cy0177):
    assert len(cy0177.pages) == 16
    assert [pg.page for pg in cy0177.pages] == list(range(1, 17))


def test_cy0177_total_tables(cy0177):
    assert _total_tables(cy0177) >= 6


def test_cy0177_title(cy0177):
    assert cy0177.title == "CY0177-75V-20A 电源设计规格书"


def test_cy0177_page9_merged_and_terms(cy0177):
    pg = cy0177.pages[8]  # 第 9 页
    assert len(pg.table_drafts) >= 1
    draft = pg.table_drafts[0]
    assert draft.merged_cells > 0
    text = _grid_text(draft)
    assert "欠压点" in text
    assert "回差" in text


def test_cy0177_page9_caption(cy0177):
    draft = cy0177.pages[8].table_drafts[0]
    assert "表3" in draft.caption
    assert "保护功能" in draft.caption


def test_merged_covered_slot_none_and_span(cy0177):
    draft = cy0177.pages[8].table_drafts[0]
    # 存在 span>1 的合并格
    spanned = [
        c for row in draft.grid for c in row if c is not None and (c.row_span > 1 or c.col_span > 1)
    ]
    assert spanned
    # 存在被合并覆盖的 None 槽
    covered = [c for row in draft.grid for c in row if c is None]
    assert covered
    # 具体到左上角的横向合并: grid[0][0] 跨 2 列，grid[0][1] 为被覆盖槽
    head = draft.grid[0][0]
    assert head is not None and head.col_span > 1
    assert draft.grid[0][1] is None
    # 密集网格: 非 None + None == n_rows * n_cols
    non_null = sum(1 for row in draft.grid for c in row if c is not None)
    assert non_null + len(covered) == draft.n_rows * draft.n_cols
    # 契约属性 merged_cells 与手工统计一致
    manual = sum(
        1
        for row in draft.grid
        for c in row
        if c is not None and (c.row_span > 1 or c.col_span > 1)
    )
    assert draft.merged_cells == manual


def test_page9_placeholder_no_content_duplication(cy0177):
    pg = cy0177.pages[8]
    # 表格内容只存在于 table_drafts，不出现在正文段落里
    para_text = "\n".join(b.text for b in pg.blocks if b.kind != BlockKind.TABLE)
    assert "欠压点" not in para_text
    # 恰好一个 TABLE 占位，且 text 与草稿 draft_id 对齐
    placeholders = [b for b in pg.blocks if b.kind == BlockKind.TABLE]
    assert len(placeholders) == 1
    assert placeholders[0].text == pg.table_drafts[0].draft_id


def test_source_meta_geometry(cy0177):
    for pg in cy0177.pages:
        for draft in pg.table_drafts:
            meta = draft.source_meta
            assert "y0" in meta and "rect" in meta
            x0, y0, x1, y1 = meta["rect"]
            assert x1 > x0 and y1 > y0
            assert meta["y0"] == pytest.approx(y0)


def test_pdf_blocks_section_path_empty(cy0177):
    # PDF 侧 section_path 由主会话统一回填，解析层填空串
    for pg in cy0177.pages[:3]:
        for b in pg.blocks:
            assert b.section_path == ""


def test_blocks_carry_page_number(cy0177):
    pg = cy0177.pages[4]
    assert pg.blocks
    assert all(b.page == 5 for b in pg.blocks)


# --------------------------------------------------------------------------- #
# GDC800T5054-UC.pdf
# --------------------------------------------------------------------------- #
def test_gdc_page_count(gdc):
    assert len(gdc.pages) == 52


def test_gdc_total_tables_exact(gdc):
    assert _total_tables(gdc) == 39


# --------------------------------------------------------------------------- #
# fail-fast
# --------------------------------------------------------------------------- #
def test_pdf_missing_file():
    with pytest.raises(FileNotFoundError):
        extract_pdf(DOCS / "no_such_file.pdf")


def test_pdf_bad_file(tmp_path):
    bad = tmp_path / "garbage.pdf"
    bad.write_bytes(b"this is not a valid PDF at all")
    with pytest.raises(RuntimeError):
        extract_pdf(bad)
