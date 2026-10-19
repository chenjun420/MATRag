"""P0-5 verify 测试：对账闸门（守恒/反凭空/合并痕迹/维度）+ report 汇总。

全程离线：自造 TableModel / TableDraft，不 import geometry。
"""

from __future__ import annotations

from matrag.parse.verify import build_parse_report, verify_table
from matrag.types import Cell, TableDraft, TableModel


def _cell(text: str, *, row_span: int = 1, col_span: int = 1) -> Cell:
    return Cell(rect=(0.0, 0.0, 1.0, 1.0), text=text, row_span=row_span, col_span=col_span)


def _merged_pair() -> tuple[TableModel, TableDraft]:
    """正确重建的合并表对：rowspan=3，merged_cells=1，verify 应为 clean。"""
    draft = TableDraft(
        draft_id="mt0", page=2, table_index=1, caption="合并表",
        n_rows=4, n_cols=3,
        grid=[
            [_cell("项目"), _cell("参数"), _cell("备注")],
            [_cell("输入欠压保护", row_span=3), _cell("欠压点"), _cell("-")],
            [None, _cell("恢复点"), _cell("-")],
            [None, _cell("回差"), _cell("-")],
        ],
    )
    model = TableModel(
        id="mt0", caption="合并表",
        headers=["项目", "参数", "备注"],
        rows=[
            ["输入欠压保护", "欠压点", "-"],
            ["输入欠压保护", "恢复点", "-"],
            ["输入欠压保护", "回差", "-"],
        ],
        marks=["rowspan:3@col0"],
        confidence=0.9,
        source={"page": 2},
    )
    return model, draft


def test_verify_clean() -> None:
    """正确合并表 → flags 为空（守恒/反凭空/合并痕迹全过）。"""
    model, draft = _merged_pair()
    tr = verify_table(model, draft)
    assert tr.flags == []
    assert tr.table_id == "mt0"
    assert tr.page == 2
    assert tr.n_rows == 3
    assert tr.n_cols == 3
    assert tr.merged_cells == 1


def test_verify_conservation_text_lost() -> None:
    """删掉一个源文本单元格（把 '回差' 改成别的）→ ERROR_TEXT_LOST。"""
    model, draft = _merged_pair()
    model.rows[2][1] = "坏值"  # 覆盖了源 [3][1] 的 '回差'
    tr = verify_table(model, draft)
    assert "ERROR_TEXT_LOST" in tr.flags


def test_verify_invented_number() -> None:
    """把草稿里的 '-' 改成 '95'（草稿无此数字）→ INVENTED_NUM。"""
    model, draft = _merged_pair()
    model.rows[0][2] = "95"  # 源对应槽是 '-'，全草稿无 95
    tr = verify_table(model, draft)
    assert "INVENTED_NUM" in tr.flags
    assert "ERROR_TEXT_LOST" not in tr.flags


def test_verify_missing_merge_extra_mark() -> None:
    """多写一个合并痕迹 → merge 计数 != merged_cells → WARN_MERGE_INCOMPLETE。"""
    model, draft = _merged_pair()
    model.marks = ["rowspan:3@col0", "colspan:2@row5"]  # 多了一条
    tr = verify_table(model, draft)
    assert "WARN_MERGE_INCOMPLETE" in tr.flags


def test_verify_missing_merge_absent() -> None:
    """合并表但无合并痕迹 → WARN_MERGE_INCOMPLETE。"""
    model, draft = _merged_pair()
    model.marks = []
    tr = verify_table(model, draft)
    assert "WARN_MERGE_INCOMPLETE" in tr.flags


def test_verify_rect_and_dup_header() -> None:
    """行宽不符 → ERROR_RECT；表头重复 → WARN_DUP_HEADER（不 fail-closed）。"""
    # 行宽不符（少一列）
    draft_bad = TableDraft(
        draft_id="bad", page=1, table_index=0, caption="",
        n_rows=2, n_cols=3,
        grid=[[_cell("A"), _cell("B"), _cell("C")], [_cell("1"), _cell("2"), _cell("3")]],
    )
    model_bad = TableModel(
        id="bad", caption="", headers=["A", "B", "C"],
        rows=[["1", "2"]],  # 只 2 列，应为 3
    )
    tr = verify_table(model_bad, draft_bad)
    assert "ERROR_RECT" in tr.flags

    # 表头重复
    draft_dup = TableDraft(
        draft_id="dup", page=1, table_index=0, caption="",
        n_rows=2, n_cols=3,
        grid=[[_cell("A"), _cell("A"), _cell("备注")], [_cell("a"), _cell("b"), _cell("c")]],
    )
    model_dup = TableModel(
        id="dup", caption="", headers=["A", "A", "备注"], rows=[["a", "b", "c"]],
    )
    tr2 = verify_table(model_dup, draft_dup)
    assert "WARN_DUP_HEADER" in tr2.flags
    assert not any(f.startswith("ERROR") for f in tr2.flags)


def _ragged_pair() -> tuple[TableModel, TableDraft]:
    """源表自身错位（表头 3 格 / 数据行 2 格）, 补空后数据整体错位。"""
    draft = TableDraft(
        draft_id="rt0", page=1, table_index=0, caption="错位表",
        n_rows=3, n_cols=3,
        grid=[
            [_cell("项目"), _cell("单位"), _cell("最小值")],
            [_cell("标称输入电压"), _cell("Vac"), _cell("100")],
            [_cell("输入频率"), _cell("Hz"), _cell("45")],
        ],
        source_meta={"y0": 0, "flags": ["RAGGED_ROW"], "data_row_widths": [2, 2]},
    )
    model = TableModel(
        id="rt0", caption="错位表",
        headers=["项目", "单位", "最小值"],
        rows=[["标称输入电压", "Vac", "100"],
              ["输入频率", "Hz", "45"]],
        marks=["passthrough"], confidence=1.0, source={"page": 1},
    )
    return model, draft


def test_verify_source_ragged_goes_to_review() -> None:
    """
    补空导致的数据错位 → ``SOURCE_RAGGED`` 并进 unresolved。

    unresolved 非空 = fail-closed，整文档不进索引。
    """
    model, draft = _ragged_pair()
    tr = verify_table(model, draft)
    assert "SOURCE_RAGGED" in tr.flags

    report = build_parse_report("doc", "src.pdf", "test", [(model, draft)])
    assert report.unresolved == ["rt0"]
    assert report.ok is False  # 有未决项 = 不过闸门


def test_verify_header_repair_trace_visible() -> None:
    """确定性修复合并表头的痕迹可查（不阻断索引）。"""
    model, draft = _ragged_pair()
    draft.source_meta = {"y0": 0, "flags": ["HEADER_DUP_COLLAPSED"], "data_row_widths": [3, 3]}
    tr = verify_table(model, draft)
    assert "WARN_HEADER_REPAIRED" in tr.flags
    assert "SOURCE_RAGGED" not in tr.flags
    report = build_parse_report("doc", "src.md", "markdown", [(model, draft)])
    assert report.ok is True


def test_dup_header_alone_does_not_block_document() -> None:
    """
    表头重名只是命名问题（数据仍对齐）。

    数据对齐就没有判据风险，不进 unresolved（不阻断整文档）。
    """
    model, draft = _ragged_pair()
    model.headers = ["项目", "项目", "最小值"]
    draft.grid[0] = [_cell("项目"), _cell("项目"), _cell("最小值")]
    draft.source_meta = {"y0": 0, "flags": [], "data_row_widths": [3, 3]}
    tr = verify_table(model, draft)
    assert "WARN_DUP_HEADER" in tr.flags
    assert "SOURCE_RAGGED" not in tr.flags
    report = build_parse_report("doc", "src.md", "markdown", [(model, draft)])
    assert report.unresolved == []
    assert report.ok is True


def test_number_percent_matches() -> None:
    """'2%' 与 '2' 能对上：草稿 '2%'，输出 '2' 不算凭空。"""
    from matrag.parse.verify import _number_tokens

    draft = TableDraft(
        draft_id="p", page=1, table_index=0, caption="",
        n_rows=2, n_cols=2,
        grid=[[_cell("k"), _cell("v")], [_cell("容差"), _cell("2%")]],
    )
    model = TableModel(
        id="p", caption="", headers=["k", "v"], rows=[["容差", "2"]],
    )
    tr = verify_table(model, draft)
    assert "INVENTED_NUM" not in tr.flags
    assert _number_tokens("2%") == ["2"]


def test_build_parse_report_ok() -> None:
    """干净表 → report.ok True，stats 正确，to_dict 冒烟。"""
    model, draft = _merged_pair()
    rep = build_parse_report("doc1", "/x/y.pdf", "local", [(model, draft)])
    assert rep.ok is True
    assert rep.stats == {
        "n_tables": 1, "n_rows_total": 3, "merged_total": 1, "unresolved_count": 0,
    }
    assert rep.unresolved == []
    d = rep.to_dict()
    assert d["ok"] is True
    assert d["stats"]["n_tables"] == 1
    # to_markdown / to_dict 冒烟
    md = model.to_markdown()
    assert md.startswith("| 项目 |")
    assert "输入欠压保护" in md
    md_row = [ln for ln in md.splitlines() if "欠压点" in ln]
    assert md_row, "输出应含数据行"


def test_build_parse_report_unresolved() -> None:
    """触发 INVENTED_NUM → 进 unresolved，ok False。"""
    model, draft = _merged_pair()
    model.rows[0][2] = "95"
    rep = build_parse_report("doc2", "/x/z.pdf", "local", [(model, draft)])
    assert rep.ok is False
    assert "mt0" in rep.unresolved
    assert rep.stats["unresolved_count"] == 1


def test_build_parse_report_error_fails_closed() -> None:
    """空错误表（headers 空）→ ERROR_* → ok False（fail-closed）。"""
    draft = TableDraft(
        draft_id="e0", page=1, table_index=0, caption="",
        n_rows=2, n_cols=2,
        grid=[[_cell("项目"), _cell("输入欠压保护", row_span=2)],
              [None, _cell("a")]],
    )
    model = TableModel(id="e0", caption="", headers=[], rows=[],
                       marks=["ERROR_LLM_JSON"], confidence=0.0, source={"page": 1})
    rep = build_parse_report("doc3", "/x/e.pdf", "local", [(model, draft)])
    assert rep.ok is False
    flags = [f for t in rep.tables for f in t.flags]
    assert any(f.startswith("ERROR") for f in flags)
