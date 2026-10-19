"""markdown_reader（Markdown 解析 provider）离线测试。

样例读仓库 ``docs/PA601-D54A 定制电源技术规格书.md``；另用临时合成文稿覆盖
``<br>``、反斜杠转义 ``\\|``、RAGGED_ROW、引用块等分支。全部离线。
"""

from __future__ import annotations

import pytest

from matrag.config import REPO_ROOT
from matrag.parse.geometry import extract_markdown as geom_extract_markdown
from matrag.parse.markdown_reader import extract_markdown
from matrag.types import BlockKind

DOCS = REPO_ROOT / "docs"
PA601 = DOCS / "PA601-D54A 定制电源技术规格书.md"


@pytest.fixture(scope="module")
def md():
    return extract_markdown(PA601)


def _headings(doc):
    return [b for b in doc.blocks if b.kind == BlockKind.HEADING]


# --------------------------------------------------------------------------- #
# docs/PA601-D54A 定制电源技术规格书.md
# --------------------------------------------------------------------------- #
def test_md_tables_count(md):
    assert len(md.table_drafts) >= 8


def test_md_br_cells_present(md):
    # 「表9-2 指示灯定义」「修改记录」等单元格含 <br>
    found = [
        d.caption
        for d in md.table_drafts
        for row in d.grid
        for c in row
        if c is not None and "<br>" in c.text
    ]
    assert found


def test_md_headings_count(md):
    assert len(_headings(md)) >= 30


def test_md_section_path_shape(md):
    paths = {b.section_path for b in md.blocks}
    assert any(sp.startswith("4 > 技术要求") for sp in paths)
    # 精确命中: 4.3 标题（ATX 层级不规范，靠章节号归位）
    assert "4 > 技术要求 > 4.3 功能/性能要求" in paths


def test_md_placeholder_ids_align(md):
    placeholders = [b for b in md.blocks if b.kind == BlockKind.TABLE]
    assert [b.text for b in placeholders] == [d.draft_id for d in md.table_drafts]


def test_md_header_is_grid_row0(md):
    # 编写依据表: 表头 | 标准编号 | 标准名称 |
    match = [
        d
        for d in md.table_drafts
        if d.grid and [c.text for c in d.grid[0]] == ["标准编号", "标准名称"]
    ]
    assert match
    draft = match[0]
    assert draft.n_cols == 2
    assert all(len(row) == draft.n_cols for row in draft.grid)


def test_md_flags_ragged_present(md):
    ragged = [d for d in md.table_drafts if "RAGGED_ROW" in d.source_meta.get("flags", [])]
    assert ragged
    for draft in ragged:
        # 补齐后每行列数一致
        assert all(len(row) == draft.n_cols for row in draft.grid)


def test_md_dup_header_collapsed_to_data_width(tmp_path):
    """源表头把合并格展开成重复文本时, 确定性折叠对齐（数据不移动）。

    真实案例: docs/PA601-D54A 表10 表头 9 格、数据行 8 格。折叠相邻重复的“项目”
    后宽度正好等于数据行宽 -> 修复; 若不修, 补空会让“单位”列里躺的是数值。
    """
    md_text = (
        "# T\n\n"
        "**表X 输入特性表**\n\n"
        "| 编号 | 项目 | 项目 | 单位 |"
        " 最小值 | 最大值 | 备注 |\n"
        "|---|---|---|---|---|---|---|\n"
        "| SR-1 | 标称输入电压 | Vac | 100 | 127 | 铭牌 |\n"
    )
    f = tmp_path / "dup_header.md"
    f.write_text(md_text, encoding="utf-8")
    draft = extract_markdown(f).table_drafts[0]
    assert [c.text for c in draft.grid[0]] == [
        "编号", "项目", "单位", "最小值", "最大值", "备注",
    ]
    assert "HEADER_DUP_COLLAPSED" in draft.source_meta.get("flags", [])
    # 数据未被移动: 单位列仍是 Vac
    assert draft.grid[1][2].text == "Vac"
    assert all(len(row) == draft.n_cols for row in draft.grid)


def test_md_ragged_not_repairable_keeps_flag(tmp_path):
    """补空到补不上的错位表: 不猜、保留 RAGGED_ROW。

    交验证层判 ``SOURCE_RAGGED`` 并进复核清单。
    """
    md_text = (
        "# T\n\n"
        "**表Y 宽表**\n\n"
        "| A | B | C | D |\n"
        "|---|---|---|---|\n"
        "| 1 | 2 |\n"
    )
    f = tmp_path / "ragged.md"
    f.write_text(md_text, encoding="utf-8")
    draft = extract_markdown(f).table_drafts[0]
    assert "RAGGED_ROW" in draft.source_meta.get("flags", [])
    assert "HEADER_DUP_COLLAPSED" not in draft.source_meta.get("flags", [])


def test_md_note_and_paragraph(md):
    notes = [b for b in md.blocks if b.kind == BlockKind.NOTE]
    assert any("技术文件名称" in b.text for b in notes)
    assert any(b.kind == BlockKind.PARAGRAPH for b in md.blocks)


def test_md_title(md):
    assert md.title == "PA601-D54A 定制电源技术规格书 B"


def test_md_missing_file():
    with pytest.raises(FileNotFoundError):
        extract_markdown(DOCS / "no_such_file.md")


def test_reexport_from_geometry():
    # geometry.py re-export 的是同一个函数
    assert geom_extract_markdown is extract_markdown


# --------------------------------------------------------------------------- #
# 合成文稿: 覆盖 <br> / \| 转义 / RAGGED / 引用 / 编号标题
# --------------------------------------------------------------------------- #
_SYNTHETIC = """\
# 规格书 Title A

> note line one
> note line two

## 4 技术要求

**表1 测试表**

| A | B | C |
|---|---|---|
| 1 | 2 | 3 |
| 4 | 5 |
| x \\| y | <br>z | keep |

正文段落 line。
"""


@pytest.fixture(scope="module")
def synth(tmp_path_factory):
    p = tmp_path_factory.mktemp("synth") / "syn.md"
    p.write_text(_SYNTHETIC, encoding="utf-8")
    return extract_markdown(p)


def test_synth_headings(synth):
    heads = {b.text for b in _headings(synth)}
    assert "规格书 Title A" in heads
    assert "4 技术要求" in heads


def test_synth_note(synth):
    notes = [b for b in synth.blocks if b.kind == BlockKind.NOTE]
    assert notes
    assert "note line one" in notes[0].text
    assert "note line two" in notes[0].text


def test_synth_table(synth):
    assert len(synth.table_drafts) == 1
    draft = synth.table_drafts[0]
    assert draft.caption == "表1 测试表"
    assert [c.text for c in draft.grid[0]] == ["A", "B", "C"]
    assert draft.n_cols == 3
    # RAGGED: "| 4 | 5 |" 只有 2 列
    assert "RAGGED_ROW" in draft.source_meta["flags"]
    # 补齐: 第 2 行数据应为 ["4","5",""]
    assert [c.text for c in draft.grid[2]] == ["4", "5", ""]
    # "\|" 还原为 "|"，未误切分
    cells = [c.text for row in draft.grid for c in row]
    assert "x | y" in cells
    # <br> 原样保留
    assert "<br>z" in cells
    # dense grid: data 行数 = 3
    assert draft.n_rows == 4


def test_synth_section_path(synth):
    # "## 4 技术要求"（章节号 4）把 H1 标题弹出栈，路径从编号起
    tbl = next(b for b in synth.blocks if b.kind == BlockKind.TABLE)
    assert tbl.section_path == "4 技术要求"
    tbl_section = next(d for d in synth.table_drafts)
    assert tbl_section.source_meta["flags"] is not None
