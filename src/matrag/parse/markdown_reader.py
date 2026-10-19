"""Markdown 解析 provider：GFM 文稿 → 规范文档模型草稿（契约见 ``matrag.types``）。

与 PDF provider 的差异: Markdown 已是干净文本，表格是标准 GFM pipe 表，
因此不需要几何; 合并 span 恒为 1，标题/引用/表格由语法直接判定。

产出约定:
- 返回 :class:`MarkdownDoc`: 单页（``page=0``）的 ``blocks`` + ``table_drafts``。
- 表格以 ``Block(kind=BlockKind.TABLE, text=<table_id>)`` 占位，
  ``table_drafts`` 中 ``grid`` 第 0 行即 GFM 表头行、其余为数据行，
  重建层据此直接平铺（无需合并判定）。
- 失败 fail-fast: 文件不存在抛 ``FileNotFoundError``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from matrag.types import Block, BlockKind, Cell, TableDraft

__all__ = ["MarkdownDoc", "extract_markdown"]


# ATX 标题: 行首 1~6 个 # 后跟空白（GFM 要求 # 后有空格或行尾）。
_ATX_RE = re.compile(r"^(#{1,6})(?:[ \t]+(.*))?$")
# 章节号前缀，如 "4.3 功能/性能要求" / "4 技术要求"。
_NUM_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,3}){0,4})[ \t、]+(.+)$")
_NUM_PREFIX_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,3}){0,4})[ \t、]")
# 表格粗体表题: "**表4-1 ...**" / "**Table 3 ...**"。
_MD_CAPTION_RE = re.compile(r"^\*\*\s*(表\s?\d+(?:-\d+)?|Table\s+\d+)[^*]*\*\*\s*$")


@dataclass
class MarkdownDoc:
    """一份 Markdown 的解析结果（单页，page=0）。"""

    doc_id: str
    title: str
    source_path: str
    blocks: list[Block]
    table_drafts: list[TableDraft]
    meta: dict[str, int] = field(default_factory=dict)


@dataclass
class _TableSpan:
    """一段 GFM pipe 表的中间表示（不含 source_meta）。"""

    header_i: int
    end_i: int
    caption: str
    caption_i: int | None
    headers: list[str]
    data_rows: list[list[str]]


# --------------------------------------------------------------------------- #
# 单元格切分 / 行类型判定
# --------------------------------------------------------------------------- #
def _split_row(line: str) -> list[str]:
    """按 ``|`` 切分一行，尊重反斜杠转义 ``\\|``（还原为 ``|``），去首尾空白。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf).strip())
    return cells


def _is_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and len(s) > 1


def _is_delimiter(line: str) -> bool:
    s = line.strip()
    if "|" not in s:
        return False
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    parts = s.split("|")
    if not parts:
        return False
    return all(re.match(r"^\s*:?-+:?\s*$", p) for p in parts)


# --------------------------------------------------------------------------- #
# section_path 层级栈（按章节号深度，兼容 Markdown 层级不规范的情况）
# --------------------------------------------------------------------------- #
def _numbered_depth(text: str) -> int | None:
    m = _NUM_PREFIX_RE.match(text.strip())
    if not m:
        return None
    return m.group(1).count(".") + 1


def _segments(text: str) -> list[str]:
    """把一个祖先标题拆成 section_path 片段: 有章节号则号与题分开。"""
    m = _NUM_RE.match(text.strip())
    if m:
        return [m.group(1), m.group(2).strip()]
    return [text.strip()]


class _SectionTracker:
    """按「章节号深度优先、无章节号用 ATX 层级」维护标题栈并累积路径。"""

    def __init__(self) -> None:
        self._stack: list[tuple[int, str]] = []
        self.current = ""

    def heading(self, atx_level: int, text: str) -> str:
        """压入一个标题，返回它的完整 section_path（含自身为末段）。"""
        depth = _numbered_depth(text)
        d = depth if depth is not None else atx_level
        while self._stack and self._stack[-1][0] >= d:
            self._stack.pop()
        self._stack.append((d, text))
        parts: list[str] = []
        for _d, t in self._stack[:-1]:
            parts.extend(_segments(t))
        parts.append(text)
        self.current = " > ".join(parts)
        return self.current


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def _find_tables(lines: list[str]) -> list[_TableSpan]:
    """扫描出所有 GFM pipe 表及其表题。"""
    spans: list[_TableSpan] = []
    i = 0
    n = len(lines)
    while i < n - 1:
        if _is_row(lines[i]) and _is_delimiter(lines[i + 1]):
            headers = _split_row(lines[i])
            if headers:
                j = i + 2
                data: list[list[str]] = []
                while j < n and _is_row(lines[j]):
                    data.append(_split_row(lines[j]))
                    j += 1
                caption, caption_i = _find_md_caption(lines, i)
                spans.append(
                    _TableSpan(
                        header_i=i,
                        end_i=j - 1,
                        caption=caption,
                        caption_i=caption_i,
                        headers=headers,
                        data_rows=data,
                    )
                )
                i = j
                continue
        i += 1
    return spans


def _find_md_caption(lines: list[str], header_i: int) -> tuple[str, int | None]:
    """表格上方最近的粗体表题; 找不到返回 ("表{index}", None)，index 由调用方补齐。"""
    k = header_i - 1
    while k >= 0 and not lines[k].strip():
        k -= 1
    if k >= 0:
        m = _MD_CAPTION_RE.match(lines[k].strip())
        if m:
            inner = lines[k].strip().strip("*").strip()
            return inner, k
    return "", None


def _atx_content(line: str) -> tuple[int, str] | None:
    m = _ATX_RE.match(line)
    if not m:
        return None
    level = len(m.group(1))
    content = (m.group(2) or "").strip()
    # 去掉 ATX 行尾的闭包 #（形如 "## 标题 ##"）。
    content = re.sub(r"\s+#+\s*$", "", content).strip()
    return level, content


def extract_markdown(path: str | Path) -> MarkdownDoc:
    """解析 Markdown 为 :class:`MarkdownDoc`（单页 blocks + 表格草稿）。

    fail-fast: 文件不存在抛 ``FileNotFoundError``。
    """
    if isinstance(path, Path):
        p = path
    else:
        if not str(path):
            raise FileNotFoundError("路径为空")
        p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Markdown 不存在: {p}")

    text = p.read_text(encoding="utf-8")
    lines = text.split("\n")

    doc_id = p.stem
    spans = _find_tables(lines)
    by_header = {sp.header_i: sp for sp in spans}

    # 需要跳过的行: 表格体 + 被表格引用的表题行。
    skip: set[int] = set()
    for sp in spans:
        skip.update(range(sp.header_i + 1, sp.end_i + 1))
        if sp.caption_i is not None:
            skip.add(sp.caption_i)

    tracker = _SectionTracker()
    blocks: list[Block] = []
    drafts: list[TableDraft] = []
    title = ""
    para: list[str] = []
    note: list[str] = []

    def flush_para() -> None:
        if para:
            blocks.append(
                Block(kind=BlockKind.PARAGRAPH, text="\n".join(para),
                      page=0, section_path=tracker.current)
            )
            para.clear()

    def flush_note() -> None:
        if note:
            blocks.append(
                Block(kind=BlockKind.NOTE, text="\n".join(note),
                      page=0, section_path=tracker.current)
            )
            note.clear()

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if i in by_header:
            flush_para()
            flush_note()
            sp = by_header[i]
            table_idx = len(drafts)
            caption = sp.caption or f"表{table_idx}"
            table_id = f"t0-{table_idx}"
            blocks.append(
                Block(kind=BlockKind.TABLE, text=table_id, page=0,
                      section_path=tracker.current)
            )
            drafts.append(_md_draft(table_id, table_idx, caption, sp))
            i += 1
            continue

        if i in skip:
            flush_para()
            flush_note()
            i += 1
            continue

        stripped = line.strip()

        atx = _atx_content(line) if line.lstrip().startswith("#") else None
        if atx is not None:
            flush_para()
            flush_note()
            level, content = atx
            if content and not title:
                title = content
            section = tracker.heading(level, content)
            blocks.append(
                Block(kind=BlockKind.HEADING, text=content, level=level,
                      page=0, section_path=section)
            )
            i += 1
            continue

        if stripped.startswith(">"):
            flush_para()
            note.append(re.sub(r"^>\s?", "", line.rstrip()))
            i += 1
            continue
        if note:
            flush_note()

        if not stripped:
            flush_para()
            i += 1
            continue

        para.append(line.rstrip())
        i += 1

    flush_para()
    flush_note()

    return MarkdownDoc(
        doc_id=doc_id,
        title=title or doc_id,
        source_path=str(p),
        blocks=blocks,
        table_drafts=drafts,
        meta={"n_tables": len(drafts), "n_blocks": len(blocks)},
    )


def _collapse_dup_headers(headers: list[str]) -> list[str]:
    """折叠**相邻重复**的表头格（保留首次出现的文本）。

    规格书 markdown 常把合并表头展开成重复文本（如"编号|项目"上层的"项目"
    被写成"项目|项目"）。相邻重复是展开合并的表现，非内容错误。
    """
    out: list[str] = []
    for cell in headers:
        text = str(cell).strip()
        if out and out[-1] == text:
            continue
        out.append(text)
    return out


def _md_draft(
    table_id: str, table_idx: int, caption: str, sp: _TableSpan
) -> TableDraft:
    """把 :class:`_TableSpan` 转成 dense ``TableDraft``: 表头为第 0 行。

    源 markdown 若表头列数多于数据行（作者把合并表头展开、或手误多写了一格），
    补空会让**整表数据语义错位**（"单位"列里躺的是数值）——这是判据级风险。
    这里做一次**确定性修复**：仅当"折叠相邻重复表头"后表头宽度恰好等于数据行
    宽度时，才折叠并按该宽度补/裁数据行（裁剪只允许裁尾部空单元格）。
    修复不成立或仍有宽度差 → 保留 ``RAGGED_ROW``，由 verify 标 ``SOURCE_RAGGED``
    进人工复核（fail-closed，不猜测）。
    """
    headers = [str(h) for h in sp.headers]
    data_widths = [len(row) for row in sp.data_rows]
    flags: list[str] = []
    modal_width = max(data_widths) if data_widths else len(headers)

    collapsed = _collapse_dup_headers(headers)
    if len(collapsed) < len(headers) and modal_width and len(collapsed) == modal_width:
        # 宽度对上了 → 判定为合并表头展开，确定性修复
        headers = collapsed
        flags.append("HEADER_DUP_COLLAPSED")

    n_cols = len(headers)
    grid: list[list[Cell | None]] = [
        [Cell(rect=(0.0, 0.0, 0.0, 0.0), text=h) for h in headers]
    ]
    for row in sp.data_rows:
        if len(row) != n_cols and "RAGGED_ROW" not in flags:
            flags.append("RAGGED_ROW")
        cells = [str(c) for c in row]
        if len(cells) > n_cols:
            # 只裁尾部空单元格；裁不动就保留（verify 会判 SOURCE_RAGGED）
            trimmed = cells[:n_cols]
            if any(str(c).strip() for c in cells[n_cols:]):
                flags.append("RAGGED_ROW")
            cells = trimmed
        if len(cells) < n_cols:
            cells.extend([""] * (n_cols - len(cells)))
        grid.append([Cell(rect=(0.0, 0.0, 0.0, 0.0), text=c) for c in cells])
    return TableDraft(
        draft_id=table_id,
        page=0,
        table_index=table_idx,
        caption=caption,
        n_rows=len(grid),
        n_cols=n_cols,
        grid=grid,
        source_meta={"y0": 0, "flags": flags, "data_row_widths": data_widths},
    )
