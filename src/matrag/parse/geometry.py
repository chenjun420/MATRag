"""local 几何解析 provider：PyMuPDF 单元格几何 + 合并 span（契约见 ``matrag.types``）。

职责边界（与其它 provider 对齐）:
- 只负责「几何草稿」: 产出 ``PDFDoc`` / ``MarkdownDoc``，正文与表格草稿分离。
- 表格以 ``Block(kind=BlockKind.TABLE, text=<table_id>)`` 占位，重建后的
  ``TableModel`` 由主会话回填到该 ``Block.table``；
  ``table_drafts`` 与占位 block 通过 ``table_id == draft_id`` 关联。
- Markdown 草稿由 ``markdown_reader`` 提供，此处 re-export，方便上层单点导入。

设计取舍:
- 不使用任何 LLM；判定全部基于几何/语法，失败 fail-fast，绝不静默降级。
- PDF 阅读顺序文本若落在表格矩形内则并入表格草稿，不作为正文段落，避免内容重复。
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from matrag.parse.markdown_reader import MarkdownDoc, extract_markdown
from matrag.types import Block, BlockKind, Cell, TableDraft

__all__ = [
    "PDFDoc",
    "PageDoc",
    "MarkdownDoc",
    "extract_pdf",
    "extract_markdown",
]

# 表格列/行边界聚类的容差（pt）。经验值: 覆盖整数坐标抖动/抗锯齿误差，
# 又远小于真实栏宽（本项目两本样例最小栏距 5.3pt）。
_EDGE_TOL = 2.5

# PDF 标题识别: 独立成行（压缩后为单行）且以章节号开头，如 "4.3 功能/性能要求"。
_HEADING_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,3}){0,4})[ \t、]+\S")
_HEADING_MAX_LEN = 45

# 表题识别: "表3 保护功能" / "Table 3 ..."。
_CAPTION_RE = re.compile(r"^\s*(表\s?\d+|Table\s+\d+)[^\n]{0,80}$")
# 表题到表格上沿的最大垂直距离（pt），超过则认为不是本表表题。
_CAPTION_WINDOW = 150.0
# 文档标题强关键词: 命中且单行则优先作为首页标题。
_TITLE_STRONG = (
    "规格书",
    "技术规格",
    "技术条件",
    "规格",
    "说明书",
    "specification",
    "datasheet",
)


# --------------------------------------------------------------------------- #
# 文档级数据结构（本 provider 私有，不属于公共契约）
# --------------------------------------------------------------------------- #
@dataclass
class PageDoc:
    """单页解析结果: 阅读顺序 blocks + 本页表格草稿。``page`` 从 1 起。"""

    page: int
    blocks: list[Block]
    table_drafts: list[TableDraft]


@dataclass
class PDFDoc:
    """一份 PDF 的解析结果。``title`` 尽量取自首页文本，取不到回退 ``doc_id``。"""

    doc_id: str
    title: str
    pages: list[PageDoc]


# --------------------------------------------------------------------------- #
# 几何工具
# --------------------------------------------------------------------------- #
def _cluster(values: list[float], tol: float) -> list[float]:
    """把已排序边界值按容差 ``tol`` 单链聚类，返回每簇代表值（均值）。"""
    if not values:
        return []
    vs = sorted(values)
    reps: list[float] = []
    group: list[float] = [vs[0]]
    for v in vs[1:]:
        if v - group[-1] <= tol:
            group.append(v)
        else:
            reps.append(sum(group) / len(group))
            group = [v]
    reps.append(sum(group) / len(group))
    return reps


def _nearest_index(reps: list[float], value: float) -> int:
    """返回 ``value`` 在升序 ``reps`` 中最近代表值的下标。"""
    if not reps:
        raise ValueError("empty representative list")
    i = bisect.bisect_left(reps, value)
    best, best_d = i, float("inf")
    for j in (i - 1, i):
        if 0 <= j < len(reps):
            d = abs(reps[j] - value)
            if d < best_d:
                best, best_d = j, d
    return best


def _compress_text(raw: str) -> str:
    """压掉块内空行: 逐行去尾空白、丢弃空行，其余以单换行连接。"""
    lines = [ln.rstrip() for ln in raw.split("\n")]
    kept = [ln for ln in lines if ln.strip()]
    return "\n".join(kept)


def _rect_area(r: tuple[float, float, float, float]) -> float:
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def _intersect_area(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _inside_any(
    rect: tuple[float, float, float, float],
    regions: list[tuple[float, float, float, float]],
    ratio: float = 0.5,
) -> bool:
    """``rect`` 面积有超过 ``ratio`` 落入任一 ``regions`` 内则返回 True。"""
    area = _rect_area(rect)
    if area <= 0:
        return False
    return any(_intersect_area(rect, r) / area > ratio for r in regions)


# --------------------------------------------------------------------------- #
# 标题/正文判定
# --------------------------------------------------------------------------- #
def _classify_text_block(text: str) -> tuple[BlockKind, int]:
    """判定压缩后的块是标题还是正文; 标题返回 ``(HEADING, level)``，否则 PARAGRAPH。

    宁缺毋滥: 仅当压成一行、整体较短且以章节号开头才算标题。
    """
    joined = re.sub(r"\s+", " ", text.strip())
    if joined and len(joined) <= _HEADING_MAX_LEN and "\n" not in text.strip():
        m = _HEADING_RE.match(joined)
        if m:
            return BlockKind.HEADING, m.group(1).count(".") + 1
    return BlockKind.PARAGRAPH, 0


# --------------------------------------------------------------------------- #
# 表题
# --------------------------------------------------------------------------- #
def _find_caption(
    blocks: list[tuple[float, float, float, float, str]],
    table_y0: float,
    table_index: int,
) -> tuple[str, str]:
    """在本表上沿之前找最近的表题文本块。

    返回 ``(caption, reading_text)``: ``caption`` 命中正则则取其文本，否则
    ``表{table_index}``；``reading_text`` 命中的原文（补充 LLM 判读）。
    """
    best_text = ""
    best_y1 = -1.0
    for blk in blocks:
        y1 = blk[3]
        first = blk[4].split("\n", 1)[0]
        if y1 <= table_y0 and (table_y0 - y1) <= _CAPTION_WINDOW:
            if _CAPTION_RE.match(first) and y1 > best_y1:
                best_y1, best_text = y1, first.strip()
    if best_text:
        return best_text, best_text
    return f"表{table_index}", ""


# --------------------------------------------------------------------------- #
# 表格草稿
# --------------------------------------------------------------------------- #
def _cell_text(page: pymupdf.Page, rect: tuple[float, float, float, float]) -> str:
    """按单元格矩形裁切取文（sort=True 保阅读顺序），多行以 ``\\n`` 连接。"""
    raw = page.get_text(clip=pymupdf.Rect(*rect), sort=True)
    return "\n".join(ln.rstrip() for ln in raw.split("\n") if ln.strip())


def _build_table_draft(
    page: pymupdf.Page,
    table: object,
    page_no: int,
    table_index: int,
    caption_blocks: list[tuple[float, float, float, float, str]],
) -> TableDraft:
    """把一个 ``find_tables`` 结果重建为密集网格 ``TableDraft``。

    全局列/行边界: 收集所有非 None cell 的 x0/x1、y0/y1 去重后按容差聚类。
    每个非 None cell 投影到网格得到 ``(row0, col0, row_span, col_span)``，
    合并格覆盖的后续槽位保持 None。
    """
    rows = table.rows  # type: ignore[attr-defined]

    xs: list[float] = []
    ys: list[float] = []
    cells: list[tuple[float, float, float, float]] = []
    for r in rows:
        for c in r.cells:
            if c is None:
                continue
            x0, y0, x1, y1 = c
            cells.append((x0, y0, x1, y1))
            xs.extend([x0, x1])
            ys.extend([y0, y1])
    if not cells:
        raise RuntimeError(f"page {page_no} table {table_index} has no non-None cell")

    x_reps = _cluster(xs, _EDGE_TOL)
    y_reps = _cluster(ys, _EDGE_TOL)
    n_cols = len(x_reps) - 1
    n_rows = len(y_reps) - 1
    if n_cols < 1 or n_rows < 1:
        raise RuntimeError(
            f"page {page_no} table {table_index} degenerate grid "
            f"({n_rows}x{n_cols}) from {len(cells)} cells"
        )

    grid: list[list[Cell | None]] = [[None] * n_cols for _ in range(n_rows)]
    for x0, y0, x1, y1 in cells:
        col0 = _nearest_index(x_reps, x0)
        col1 = _nearest_index(x_reps, x1)
        row0 = _nearest_index(y_reps, y0)
        row1 = _nearest_index(y_reps, y1)
        col_span = max(1, col1 - col0)
        row_span = max(1, row1 - row0)
        if grid[row0][col0] is not None:
            raise RuntimeError(
                f"page {page_no} table {table_index} projected two cells "
                f"onto grid slot ({row0},{col0})"
            )
        grid[row0][col0] = Cell(
            rect=(x0, y0, x1, y1),
            text=_cell_text(page, (x0, y0, x1, y1)),
            row_span=row_span,
            col_span=col_span,
        )

    t_y0 = float(getattr(table, "bbox", cells[0])[1])
    t_rect = tuple(float(v) for v in getattr(table, "bbox", cells[0]))
    caption, reading_text = _find_caption(caption_blocks, t_y0, table_index)
    return TableDraft(
        draft_id=f"t{page_no}-{table_index}",
        page=page_no,
        table_index=table_index,
        caption=caption,
        n_rows=n_rows,
        n_cols=n_cols,
        grid=grid,
        reading_text=reading_text,
        source_meta={"y0": t_y0, "rect": t_rect},
    )


# --------------------------------------------------------------------------- #
# 标题（文档级）
# --------------------------------------------------------------------------- #
def _first_page_title(doc: pymupdf.Document) -> str:
    """尽量从首页文本取标题; 取不到返回空串由调用方回退 doc_id。

    启发式（宁缺毋滥，只作草稿提示，正式标题由主会话定）:
    1) 优先取「单行」且含强标题关键词（规格书/技术规格/技术条件/说明书…）的块；
    2) 否则退回首个「单行」且有实质内容（4~60 字）的块。
    """
    blocks = [b for b in doc[0].get_text("blocks") if b[6] == 0 and b[4].strip()]
    blocks.sort(key=lambda b: (round(b[1], 1), round(b[0], 1)))
    cands = [c for b in blocks if (c := _compress_text(b[4]))]
    for full in cands:
        line = full.split("\n", 1)[0].strip()
        if "\n" not in full and 4 <= len(line) <= 60 and any(
            k in line for k in _TITLE_STRONG
        ):
            return line
    for full in cands:
        line = full.split("\n", 1)[0].strip()
        if "\n" not in full and 4 <= len(line) <= 60:
            return line
    return ""


# --------------------------------------------------------------------------- #
# 主入口: PDF
# --------------------------------------------------------------------------- #
def extract_pdf(path: str | Path) -> PDFDoc:
    """解析 PDF 为 :class:`PDFDoc`（每页阅读顺序 blocks + 表格草稿）。

    fail-fast: 文件不存在抛 ``FileNotFoundError``；打不开（非 PDF/损坏）抛
    ``RuntimeError``。
    """
    p = _as_path(path)
    if not p.is_file():
        raise FileNotFoundError(f"PDF 不存在: {p}")
    try:
        doc = pymupdf.open(str(p))
    except Exception as exc:  # pymupdf 打开失败/非 PDF
        raise RuntimeError(f"无法打开 PDF: {p} ({exc})") from exc

    with doc:
        doc_id = p.stem
        title = _first_page_title(doc) or doc_id
        pages: list[PageDoc] = []
        for pno in range(len(doc)):
            pages.append(_parse_page(doc[pno], pno + 1))
        return PDFDoc(doc_id=doc_id, title=title, pages=pages)


def _parse_page(page: pymupdf.Page, page_no: int) -> PageDoc:
    try:
        found = page.find_tables()
    except Exception as exc:  # find_tables 在异常页面上可能抛错
        raise RuntimeError(f"page {page_no} find_tables 失败 ({exc})") from exc

    tables = list(found.tables)
    table_rects = [
        tuple(float(v) for v in getattr(t, "bbox", (0, 0, 0, 0))) for t in tables
    ]

    # 阅读顺序文本块: 仅文字块；落入表格矩形内的归并到表格草稿，避免正文重复。
    kept: list[tuple[float, float, float, float, str]] = []
    for b in page.get_text("blocks"):
        x0, y0, x1, y1, text, _no, btype = b
        if btype != 0 or not text.strip():
            continue
        rect = (float(x0), float(y0), float(x1), float(y1))
        if _inside_any(rect, table_rects):
            continue
        kept.append(rect + (_compress_text(text),))

    # 表格草稿: 表题只在「正文侧」文本块（kept）里找，
    # 而单元格取文直接用 ``page`` 按矩形裁切，二者互不依赖。
    table_drafts: list[TableDraft] = [
        _build_table_draft(page, t, page_no, idx, kept)
        for idx, t in enumerate(tables)
    ]

    # 合并阅读顺序: 正文块 + 表格占位，按 (y0, x0) 排序。
    items: list[tuple[float, float, Block]] = []
    for x0, y0, _x1, _y1, text in kept:
        kind, level = _classify_text_block(text)
        items.append((y0, x0, Block(kind=kind, text=text, level=level, page=page_no)))  # type: ignore[arg-type]
    for idx, t in enumerate(tables):
        tb = getattr(t, "bbox", (0, 0))
        ty0 = float(tb[1]) if len(tb) >= 2 else 0.0  # type: ignore[index]
        tx0 = float(tb[0]) if len(tb) >= 1 else 0.0  # type: ignore[index]
        items.append(
            (
                ty0,
                tx0,
                Block(
                    kind=BlockKind.TABLE,
                    text=f"t{page_no}-{idx}",
                    page=page_no,
                ),
            )
        )
    items.sort(key=lambda it: (it[0], it[1]))
    blocks = [b for _y, _x, b in items]

    return PageDoc(page=page_no, blocks=blocks, table_drafts=table_drafts)


def _as_path(path: str | Path) -> Path:
    """把 ``str | Path`` 归一为 :class:`~pathlib.Path`；空串按不存在处理。"""
    if isinstance(path, Path):
        return path
    if not str(path):
        raise FileNotFoundError("路径为空")
    return Path(path)
