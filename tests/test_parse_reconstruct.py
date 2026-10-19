"""P0-4 reconstruct 测试：直通路径、LLM 合并重建、错误留痕。

全程离线：用 StubLLM 注入假 LLMClient，不打网络。真实 LLM 用例 skipif 非 live。
"""

from __future__ import annotations

import pytest

from matrag.config import settings
from matrag.errors import JSONParseError
from matrag.parse.reconstruct import reconstruct_table
from matrag.types import Cell, SourceKind, TableDraft


class StubLLM:
    """最小 LLMClient 替身：预置返回值或异常，记录调用。"""

    def __init__(self, result: dict | None = None, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc
        self.calls: list[tuple[str, str, str]] = []

    @property
    def configured(self) -> bool:
        return True

    def complete_json(self, system: str, user: str, *, schema_hint: str = "",
                      max_tokens: int | None = None) -> dict:
        self.calls.append((system, user, schema_hint))
        if self._exc is not None:
            raise self._exc
        assert self._result is not None, "StubLLM 既无 result 也无 exc"
        return self._result


def _cell(text: str, *, row_span: int = 1, col_span: int = 1) -> Cell:
    return Cell(rect=(0.0, 0.0, 1.0, 1.0), text=text, row_span=row_span, col_span=col_span)


def _no_merge_draft() -> TableDraft:
    """无合并草稿（2x2），用于直通路径。"""
    grid = [
        [_cell("Name"), _cell("Value")],
        [_cell("Alpha"), _cell("10")],
        [_cell("Beta"), _cell("20")],
    ]
    return TableDraft(
        draft_id="pt0", page=1, table_index=0, caption="直通表",
        n_rows=3, n_cols=2, grid=grid,
    )


def _rowspan_draft() -> TableDraft:
    """带 rowspan=3 纵向合并的草稿（4x3，merged_cells=1）。"""
    grid = [
        [_cell("项目"), _cell("参数"), _cell("备注")],
        [_cell("输入欠压保护", row_span=3), _cell("欠压点"), _cell("-")],
        [None, _cell("恢复点"), _cell("-")],
        [None, _cell("回差"), _cell("-")],
    ]
    return TableDraft(
        draft_id="mt0", page=2, table_index=1, caption="合并表",
        n_rows=4, n_cols=3, grid=grid,
    )


_CORRECT_JSON = {
    "headers": ["项目", "参数", "备注"],
    "rows": [
        ["输入欠压保护", "欠压点", "-"],
        ["输入欠压保护", "恢复点", "-"],
        ["输入欠压保护", "回差", "-"],
    ],
    "marks": ["rowspan:3@col0"],
    "confidence": 0.9,
}


# ---------------------------------------------------------------- 直通路径
def test_passthrough_no_merge() -> None:
    """无合并草稿 → 矩形表，marks==['passthrough']，不调 LLM。"""
    draft = _no_merge_draft()
    model = reconstruct_table(draft, source_kind=SourceKind.PDF)

    assert model.marks == ["passthrough"]
    assert model.headers == ["Name", "Value"]
    assert model.rows == [["Alpha", "10"], ["Beta", "20"]]
    # 矩形：每行长度 == 表头长度
    assert all(len(r) == len(model.headers) for r in model.rows)
    assert model.confidence == 1.0


def test_passthrough_newline_to_br() -> None:
    """直通也要清洗：cell 内换行转 <br>，首尾 trim。"""
    grid = [[_cell("标题\n多行"), _cell("V")], [_cell("  a\nb  "), _cell("1")]]
    draft = TableDraft(
        draft_id="nl", page=1, table_index=0, caption="",
        n_rows=2, n_cols=2, grid=grid,
    )
    model = reconstruct_table(draft, source_kind=SourceKind.MARKDOWN)
    assert model.headers == ["标题<br>多行", "V"]
    assert model.rows == [["a<br>b", "1"]]


def test_markdown_source_skips_llm_even_with_merge() -> None:
    """source_kind==MARKDOWN 时即使有合并也走直通，不调 LLM。"""
    draft = _rowspan_draft()  # merged_cells=1
    stub = StubLLM(result=_CORRECT_JSON)
    model = reconstruct_table(draft, llm=stub, source_kind=SourceKind.MARKDOWN)
    assert model.marks == ["passthrough"]
    assert stub.calls == []  # 未调用 LLM


# ---------------------------------------------------------------- LLM 合并路径
def test_llm_rowspan_denormalized() -> None:
    """rowspan=3 正确去规范化：'输入欠压保护' 向下重复 3 行，无 ERROR。"""
    draft = _rowspan_draft()
    stub = StubLLM(result=_CORRECT_JSON)
    model = reconstruct_table(draft, llm=stub, source_kind=SourceKind.PDF)

    assert stub.calls, "应调用一次 LLM"
    assert "ERROR_DIM" not in model.marks
    assert "ERROR_LLM_JSON" not in model.marks
    # 纵向合并值向下重复到每一被覆盖行
    assert [row[0] for row in model.rows] == ["输入欠压保护"] * 3
    assert model.headers == ["项目", "参数", "备注"]
    assert all(len(r) == model.source.get("n_cols") for r in model.rows)
    assert "rowspan:3@col0" in model.marks
    assert model.confidence == pytest.approx(0.9)


def test_llm_dim_short_row() -> None:
    """LLM 少填一格（行宽不足 n_cols）→ ERROR_DIM, confidence=0，不崩。"""
    bad = {
        "headers": ["项目", "参数", "备注"],
        "rows": [["输入欠压保护", "欠压点"]],  # 少一列
        "marks": [],
        "confidence": 0.8,
    }
    model = reconstruct_table(_rowspan_draft(), llm=StubLLM(result=bad),
                              source_kind=SourceKind.PDF)
    assert "ERROR_DIM" in model.marks
    assert model.confidence == 0.0


def test_llm_dim_over_column() -> None:
    """LLM 多填一列（行宽超过 n_cols）→ ERROR_DIM。"""
    bad = {
        "headers": ["项目", "参数", "备注"],
        "rows": [["输入欠压保护", "欠压点", "-", "多"]],  # 多一列
        "marks": [],
        "confidence": 0.8,
    }
    model = reconstruct_table(_rowspan_draft(), llm=StubLLM(result=bad),
                              source_kind=SourceKind.PDF)
    assert "ERROR_DIM" in model.marks
    assert model.confidence == 0.0


def test_llm_bad_json_no_crash() -> None:
    """LLM 返回非 JSON（complete_json 抛 JSONParseError）→ ERROR_LLM_JSON，不抛异常。"""
    model = reconstruct_table(
        _rowspan_draft(),
        llm=StubLLM(exc=JSONParseError("not json", "raw")),
        source_kind=SourceKind.PDF,
    )
    assert "ERROR_LLM_JSON" in model.marks
    assert model.confidence == 0.0
    assert model.source.get("reconstruct_error")


def test_llm_none_on_text_cell_is_error_empty() -> None:
    """LLM 对源有文本的槽返回 None → 记 ERROR_EMPTY_CELL（非 FILL_DASH）。"""
    result = {
        "headers": ["项目", "参数", "备注"],
        "rows": [
            ["输入欠压保护", "欠压点", "-"],
            ["输入欠压保护", "恢复点", None],  # 源对应槽文本非空
            ["输入欠压保护", "回差", "-"],
        ],
        "marks": ["rowspan:3@col0"],
        "confidence": 0.7,
    }
    model = reconstruct_table(_rowspan_draft(), llm=StubLLM(result=result),
                              source_kind=SourceKind.PDF)
    assert "ERROR_EMPTY_CELL" in model.marks
    assert "FILL_DASH" not in model.marks
    assert model.rows[1][2] == ""


def test_llm_fill_dash_for_empty_covered_slot() -> None:
    """源覆盖槽本就无文本，LLM 返回 None → 填 '-' 并记 FILL_DASH。"""
    grid = [
        [_cell("项目"), _cell("参数")],
        [_cell("", row_span=2), _cell("a")],   # 合并格文本为空，覆盖 row1/row2 的 col0
        [None, _cell("b")],
    ]
    draft = TableDraft(
        draft_id="ed", page=1, table_index=0, caption="",
        n_rows=3, n_cols=2, grid=grid,
    )
    result = {
        "headers": ["项目", "参数"],
        "rows": [["", "a"], [None, "b"]],
        "marks": ["rowspan:2@col0"],
        "confidence": 0.5,
    }
    model = reconstruct_table(draft, llm=StubLLM(result=result),
                              source_kind=SourceKind.PDF)
    assert "FILL_DASH" in model.marks
    assert "ERROR_EMPTY_CELL" not in model.marks
    assert model.rows[1][0] == "-"


# ---------------------------------------------------------------- 真实 LLM（默认跳过）
@pytest.mark.skipif(not settings.live, reason="no MaaS creds")
def test_llm_live_rowspan() -> None:
    """真实 LLM 重建 rowspan 草稿（需 MaaS 凭据，离线跳过）。"""
    model = reconstruct_table(_rowspan_draft(), source_kind=SourceKind.PDF)
    assert [row[0] for row in model.rows] == ["输入欠压保护"] * 3
