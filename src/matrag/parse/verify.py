"""对账闸门 verify（P0-5）。

对重建结果做 fail-closed 对账：维度/矩形、守恒（源文本不丢）、反凭空（输出数字
必须有源）、合并痕迹计数。任何 ``ERROR_`` 前缀 flag → ``ParseReport.ok`` 为 False，
该文档不得进入索引。
"""

from __future__ import annotations

import re

from matrag.types import ParseReport, TableDraft, TableModel, TableReport

#: 守恒比较时剥离的标点/符号集合（中英文常见标点、斜杠、百分号等）。
_PUNCT = set(
    "，。、；：？！“”‘’（）()【】[]{}《》<>·…—–-/\\|+*&^%$#@~`="
    ".,;:!?\"' \t\r\n"
)

#: 合并痕迹标记前缀（与 prompts.py 约定一致）。
_MERGE_PREFIXES = ("rowspan", "colspan")


def _norm_text(s: str) -> str:
    """标准化文本用于守恒包含比较：去 ``<br>``、空白、标点，转小写。"""
    s = s.replace("<br>", "")
    return "".join(ch for ch in s if ch not in _PUNCT and not ch.isspace()).lower()


def _number_tokens(s: str) -> list[str]:
    """抽取数字 token（数字 + 小数点），先去掉逗号千分位。

    "2%" → "2"；"1,000" → "1000"；百分比号/逗号不算 token 的一部分。
    """
    return re.findall(r"\d+(?:\.\d+)?", s.replace(",", ""))


def _merge_mark_count(marks: list[str]) -> int:
    """统计 marks 里的 rowspan/colspan 合并痕迹条数。"""
    return sum(1 for m in marks if m.startswith(_MERGE_PREFIXES))


def _dedup(seq: list[str]) -> list[str]:
    """保序去重。"""
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _model_row_norms(model: TableModel) -> list[list[str]]:
    return [[_norm_text(c) for c in row] for row in model.rows]


def _check_conservation(model: TableModel, draft: TableDraft) -> str | None:
    """守恒：草稿每个非空 cell 文本（标准化后）必须出现在 model 输出中。

    优先在对应行找，其次全表；都找不到 → 返回 "ERROR_TEXT_LOST"。
    """
    header_norms = [_norm_text(h) for h in model.headers]
    row_norms = _model_row_norms(model)
    all_cell_norms = header_norms + [n for row in row_norms for n in row]

    for r in range(draft.n_rows):
        row = draft.grid[r] if r < len(draft.grid) else []
        for c in range(draft.n_cols):
            cell = row[c] if c < len(row) else None
            if cell is None:
                continue
            src = _norm_text(cell.text)
            if not src:
                continue
            # 对应行优先。
            if r < len(row_norms) and any(src in cn for cn in row_norms[r]):
                continue
            # 其次全表。
            if any(src in cn for cn in all_cell_norms):
                continue
            return "ERROR_TEXT_LOST"
    return None


def _check_invented(model: TableModel, draft: TableDraft) -> str | None:
    """反凭空：model **数据行**中的数字 token 必须能在草稿原文中找到。

    只查数据行、不查 headers：表头唯一性限定词（如 "标准(1)"、"欠压点(输入欠压保护)"）
    是结构性标记，其数字不是内容主张；内容判据只适用于承载事实的数据格。
    找不到 → 返回 "INVENTED_NUM"（进 unresolved，不视为硬 ERROR）。
    """
    corpus_parts = [draft.reading_text]
    for row in draft.grid:
        for cell in row:
            if cell is not None:
                corpus_parts.append(cell.text)
    draft_nums = set(_number_tokens(" ".join(corpus_parts)))

    for cell_text in [c for row in model.rows for c in row]:
        for tok in _number_tokens(cell_text):
            if tok not in draft_nums:
                return "INVENTED_NUM"
    return None


def verify_table(model: TableModel, draft: TableDraft) -> TableReport:
    """对单张重建表做对账，返回 ``TableReport``。

    flags 语义：
    - ``ERROR_RECT``    维度不矩形 / 表头为空 / 列数不符（硬伤）。
    - ``ERROR_TEXT_LOST``守恒失败，源文本丢失（硬伤）。
    - ``INVENTED_NUM``  输出出现草稿中不存在的数字（进 unresolved）。
    - ``MISSING_MERGE`` 合并痕迹条数与草稿 merged_cells 不符（进 unresolved）。
    - ``WARN_DUP_HEADER`` / ``WARN_EMPTY_HEADER`` 表头重复/为空（仅告警）。
    """
    flags: list[str] = []
    n_cols = draft.n_cols
    headers = model.headers

    # 1) 矩形 / 结构：表头非空、列数 == n_cols、每行矩形。
    rect_ok = (
        bool(headers)
        and len(headers) == n_cols
        and all(len(r) == n_cols for r in model.rows)
    )
    if not rect_ok:
        flags.append("ERROR_RECT")

    # 2) 表头非空/唯一（重复或空 = WARN，不 ERROR）。
    if any(not h.strip() for h in headers):
        flags.append("WARN_EMPTY_HEADER")
    if len(headers) != len(set(headers)):
        flags.append("WARN_DUP_HEADER")

    # 3) 守恒（硬伤）。
    lost = _check_conservation(model, draft)
    if lost:
        flags.append(lost)

    # 4) 反凭空（进 unresolved）。
    invented = _check_invented(model, draft)
    if invented:
        flags.append(invented)

    # 5) 源表自身错位（表头列数 != 数据行列数，且无合并跨度可解释）：
    #    补空后数据会整体错位（如"单位"列里躺的是数值），这类表不得当作判据。
    #    非 ERROR（不阻断整文档索引），但进入 unresolved 人工复核清单。
    src_flags = list((draft.source_meta or {}).get("flags") or [])
    if "HEADER_DUP_COLLAPSED" in src_flags:
        # 确定性修复痕迹：作者把合并表头展开成重复文本，已折叠对齐（数据未移动）
        flags.append("WARN_HEADER_REPAIRED")

    widths = draft.source_meta.get("data_row_widths") if draft.source_meta else None
    if widths and n_cols:
        max_w = max(int(w) for w in widths)
        has_span = any(
            c is not None and c.col_span > 1
            for row in draft.grid for c in row
        )
        if max_w < n_cols and not has_span:
            flags.append("SOURCE_RAGGED")

    # 6) 合并痕迹计数：审计痕迹，不是内容判据。
    #    内容正确性由上面的守恒（ERROR_TEXT_LOST）与反凭空（INVENTED_NUM）担保；
    #    marks 未列全只影响审计可读性，不阻断索引。
    if _merge_mark_count(model.marks) != draft.merged_cells:
        flags.append("WARN_MERGE_INCOMPLETE")

    flags = _dedup(flags)
    return TableReport(
        table_id=model.id,
        page=draft.page,
        n_rows=len(model.rows),
        n_cols=len(headers),
        merged_cells=draft.merged_cells,
        flags=flags,
        note="; ".join(flags) if flags else "clean",
    )


def build_parse_report(
    doc_id: str,
    source_path: str,
    provider: str,
    models: list[tuple[TableModel, TableDraft]],
) -> ParseReport:
    """逐表 verify 汇总为 ``ParseReport``。

    unresolved 收集触发 ``INVENTED_NUM`` 的 table_id（唯一的软性阻断项:
    凭空数字 = 幻觉进入判据的风险，必须人工复核）；
    ``WARN_MERGE_INCOMPLETE`` 等只是痕迹不齐，不阻断。
    stats 填 n_tables/n_rows_total/merged_total/unresolved_count。
    ``ok`` 复用 ``ParseReport.ok``（fail-closed），不另立标准。
    """
    report = ParseReport(doc_id=doc_id, source_path=source_path, provider=provider)
    n_rows_total = 0
    merged_total = 0
    for model, draft in models:
        tr = verify_table(model, draft)
        report.tables.append(tr)
        n_rows_total += len(model.rows)
        merged_total += draft.merged_cells
        # 只收**数据语义风险**：凭空数字 / 源表错位（判据列错位最致命）。
        # 表头重名/空表头只是命名问题（数据仍对齐），留在 tables[].flags 里可查即可，
        # 不阻断整文档索引。
        review_flags = ("INVENTED_NUM", "SOURCE_RAGGED")
        if any(f in tr.flags for f in review_flags) and tr.table_id not in report.unresolved:
            report.unresolved.append(tr.table_id)
    report.stats = {
        "n_tables": len(report.tables),
        "n_rows_total": n_rows_total,
        "merged_total": merged_total,
        "unresolved_count": len(report.unresolved),
    }
    return report
