"""LLM 合并单元格表格重建（P0-4）。

两种路径：
- **确定性直通**：``source_kind == MARKDOWN`` 或 ``draft.merged_cells == 0``
  时，表格已是干净结构，不调 LLM，直接取首行作表头、其余作数据行。
- **LLM 重建**：存在合并单元格时，用 ``prompts.py`` 模板调用 LLM 完成去规范化
  （rowspan 向下补、colspan 向右补、多级表头拼唯一叶表头、换行转 ``<br>``）。

无论哪条路径都会做同一套清洗与自检；LLM 失败/维度不符不抛异常，
而是返回带 ``ERROR_*`` 痕迹、``confidence=0`` 的 ``TableModel``，交给 verify 判失败
（fail-visible，绝不用空表/半表糊弄）。
"""

from __future__ import annotations

from typing import Any

from matrag.errors import JSONParseError
from matrag.llm import LLMClient, get_llm
from matrag.types import SourceKind, TableDraft, TableModel

#: LLM 调用可能抛出的异常类型（解析失败/网络/未配置/结构异常）——捕获后留痕。
_LLM_ERRORS = (JSONParseError, RuntimeError, ValueError, KeyError, TypeError)


def _clean_cell(text: str) -> str:
    """单元格清洗：去首尾空白，内部真实换行转 ``<br>``。

    ``|`` 的转义交给 ``TableModel.to_markdown``，这里不做。
    """
    t = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return t.replace("\n", "<br>")


def _dedup(seq: list[str]) -> list[str]:
    """保序去重。"""
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _reference_grid(draft: TableDraft) -> list[list[str]]:
    """草稿去规范化后的参考网格（rowspan 向下补、colspan 向右补）。

    每个网格槽被填成覆盖它的 Cell 的原文；若某槽既非 Cell 也无任何合并覆盖
    （畸形草稿），填空串。用于判断某个输出位置在源里是否本就没有文本。
    """
    ref: list[list[str]] = [["" for _ in range(draft.n_cols)] for _ in range(draft.n_rows)]
    for r in range(draft.n_rows):
        row = draft.grid[r] if r < len(draft.grid) else []
        for c in range(draft.n_cols):
            cell = row[c] if c < len(row) else None
            if cell is None:
                continue
            for i in range(max(1, cell.row_span)):
                for j in range(max(1, cell.col_span)):
                    rr, cc = r + i, c + j
                    if rr < draft.n_rows and cc < draft.n_cols:
                        ref[rr][cc] = cell.text
    return ref


def _passthrough(draft: TableDraft) -> TableModel:
    """确定性直通：首行作表头，其余作数据行；None 槽填 "-"。"""
    rows_out: list[list[str]] = []
    for r in range(draft.n_rows):
        row = draft.grid[r] if r < len(draft.grid) else []
        cells: list[str] = []
        for c in range(draft.n_cols):
            cell = row[c] if c < len(row) else None
            cells.append(_clean_cell(cell.text) if cell is not None else "-")
        rows_out.append(cells)

    headers = rows_out[0] if rows_out else []
    data = rows_out[1:]
    model = TableModel(
        id=draft.draft_id,
        caption=draft.caption,
        headers=headers,
        rows=data,
        marks=["passthrough"],
        confidence=1.0,
        source=_source_meta(draft),
    )
    _apply_header_hygiene(model)
    return model


def _source_meta(draft: TableDraft) -> dict[str, Any]:
    return {
        "page": draft.page,
        "table_index": draft.table_index,
        "draft_id": draft.draft_id,
        "n_rows": draft.n_rows,
        "n_cols": draft.n_cols,
        "merged_cells": draft.merged_cells,
    }


def _apply_header_hygiene(model: TableModel) -> None:
    """表头自检：重复表头记 DUP_HEADER（不阻断，交由 verify 记 WARN）。"""
    if len(model.headers) != len(set(model.headers)):
        model.marks.append("DUP_HEADER")


def _error_model(draft: TableDraft, mark: str, err: str = "") -> TableModel:
    """LLM 失败/维度不符时的 fail-visible 模型：空表 + ERROR 痕迹 + confidence=0。"""
    src = _source_meta(draft)
    if err:
        src["reconstruct_error"] = err
    return TableModel(
        id=draft.draft_id,
        caption=draft.caption,
        headers=[],
        rows=[],
        marks=[mark],
        confidence=0.0,
        source=src,
    )


def _coerce_cell(value: Any, *, source_has_text: bool, marks: list[str]) -> str:
    """把 LLM 输出的单元格强制成字符串；None/缺失按源文本有无决定填 "-" 或记错误。"""
    if isinstance(value, str):
        return _clean_cell(value)
    if value is None:
        if source_has_text:
            marks.append("ERROR_EMPTY_CELL")
            return ""
        marks.append("FILL_DASH")
        return "-"
    if isinstance(value, bool):  # bool 是 int 子类，JSON 布尔不应出现在单元格
        marks.append("ERROR_EMPTY_CELL")
        return ""
    if isinstance(value, (int, float)):
        # LLM 偶发把数字输出成 JSON number；整数 float 去掉多余的 .0 保持洁净。
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    marks.append("ERROR_EMPTY_CELL")
    return ""


def _llm_reconstruct(draft: TableDraft, client: LLMClient) -> TableModel:
    """LLM 重建路径：构造 prompt → complete_json → 自检。"""
    from matrag.parse.prompts import (
        JSON_SCHEMA_HINT,
        build_system_prompt,
        build_user_prompt,
    )

    system = build_system_prompt()
    user = build_user_prompt(draft)
    try:
        data = client.complete_json(system, user, schema_hint=JSON_SCHEMA_HINT)
    except _LLM_ERRORS as exc:
        return _error_model(draft, "ERROR_LLM_JSON", err=str(exc))
    except Exception as exc:  # SDK 传输/HTTP 错误（超时/403 额度等）: 按表隔离不扩散
        return _error_model(draft, "ERROR_LLM_CALL", err=f"{type(exc).__name__}: {exc}")

    if not isinstance(data, dict):
        return _error_model(draft, "ERROR_LLM_JSON", err="complete_json 未返回对象")

    raw_headers = data.get("headers")
    raw_rows = data.get("rows")
    if not isinstance(raw_headers, list) or not isinstance(raw_rows, list):
        return _error_model(draft, "ERROR_LLM_JSON", err="headers/rows 缺失或类型错误")

    marks: list[str] = [str(m) for m in data.get("marks", []) if isinstance(m, (str,))]
    confidence = data.get("confidence", 0.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0

    headers = [_clean_cell(h) if isinstance(h, str) else _coerce_cell(
        h, source_has_text=True, marks=marks) for h in raw_headers]

    # 维度自检：列数以草稿 n_cols 为准。
    dim_error = False
    if not headers or len(headers) != draft.n_cols:
        dim_error = True

    ref = _reference_grid(draft)
    header_offset = max(0, draft.n_rows - len(raw_rows))  # 表头占用的网格行数
    rows: list[list[str]] = []
    for r, raw_row in enumerate(raw_rows):
        if not isinstance(raw_row, list):
            dim_error = True
            continue
        if len(raw_row) != draft.n_cols:  # 每行必须矩形且等于 n_cols
            dim_error = True
        out_row: list[str] = []
        grid_row = r + header_offset
        for c, value in enumerate(raw_row):
            source_has_text = bool(ref[grid_row][c].strip()) if (
                grid_row < len(ref) and c < draft.n_cols) else False
            out_row.append(
                _coerce_cell(value, source_has_text=source_has_text, marks=marks)
            )
        rows.append(out_row)

    model = TableModel(
        id=draft.draft_id,
        caption=draft.caption,
        headers=headers,
        rows=rows,
        marks=_dedup(marks),
        confidence=max(0.0, min(1.0, confidence)),
        source=_source_meta(draft),
    )
    if dim_error:
        model.marks.append("ERROR_DIM")
        model.confidence = 0.0
        return model

    _apply_header_hygiene(model)
    model.marks = _dedup(model.marks)
    return model


def reconstruct_table(
    draft: TableDraft,
    *,
    llm: LLMClient | None = None,
    source_kind: SourceKind,
) -> TableModel:
    """重建单张表格。

    - ``source_kind == MARKDOWN`` 或 ``draft.merged_cells == 0`` → 确定性直通。
    - 否则走 LLM（``llm`` 注入优先，缺省用 ``get_llm()``）。

    返回的 ``TableModel`` 一定矩形且带痕迹；LLM/维度失败以 ``ERROR_*`` 痕迹 +
    ``confidence=0`` 表达，不抛异常、不用半表糊弄。
    """
    if source_kind == SourceKind.MARKDOWN or draft.merged_cells == 0:
        return _passthrough(draft)
    client = llm or get_llm()
    return _llm_reconstruct(draft, client)
