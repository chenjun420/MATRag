"""chunk 切分（P1-4 索引层）: ``CanonicalDocument`` → ``list[ChunkRecord]``。

切分策略（与检索/引用层的对接契约，签名固定）:

- 每个 heading 起新"节上下文"，chunk meta 带 ``section_path`` / ``page`` 锚点；
- 段落 / 列表按 ~``TARGET_CHARS`` 字切片，超长时按句读 / 换行边界滑窗，重叠

  ``OVERLAP_CHARS`` 字以保留上下文；
- 表格（``TableModel``）**保留表头逐行成 chunk**（``kind="table_row"``）:
  每行文本 = 表题 ``caption`` + 表头 ``headers`` + 该行 ``cells``，行内自含表头与
  表题，便于引用时单块自解释；
  表格上方紧邻的说明文字（前一段 / note，≤ ``PREAMBLE_CHARS`` 字）并入首块文本前缀；
- 页锚点: meta 里带 ``page``（取 ``Block.page``）；
- 章节标题锚点: meta 里带 ``section_titles``（``section_path`` 按 ``" > "`` 拆开的
  各级标题），供检索层按"章节名"过滤（见 :mod:`matrag.retrieve.lexicon`）；
- ``chunk_id = f"{doc_id}:{seq:04d}"``，doc 内唯一；全库唯一取决于 doc_id 唯一。

fail-fast: ``doc.blocks`` 为空 → ``ValueError``（不产出空索引）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from matrag.types import BlockKind, CanonicalDocument, SourceKind, TableModel

# 切片参数（契约里点名的默认值）
TARGET_CHARS = 600     # 段落目标长度（字）

# 文档家具（页眉/页脚/机密横幅）: 逐字重复、无信息量, 不入索引。
# 只匹配高置信模式（页码页脚 + 机密扩散声明 + 版权行），不做语义判断。
_FURNITURE_RES = (
    re.compile(r"第\s*\d+\s*页.{0,12}共\s*\d+\s*页"),
    re.compile("机密[，,]?未经许可不得扩散"),
    re.compile("All rights reserved"),
)


def _is_furniture(text: str) -> bool:
    """页脚/机密横幅等文档家具 → True（调用方跳过，不入索引）。"""
    return any(r.search(text) for r in _FURNITURE_RES)
OVERLAP_CHARS = 80     # 滑窗重叠（字）
PREAMBLE_CHARS = 200   # 表格说明文字并入上限（字）

# 句子边界: 中文句读 + 换行
_BOUNDARIES = frozenset("。！？；\n")


@dataclass
class ChunkRecord:
    """一个可索引 / 可引用的文本块。

    - ``chunk_id``: ``f"{doc_id}:{seq:04d}"``，doc 内唯一。
    - ``meta`` 至少含 ``kind``(section|table_row|paragraph|note)、``page``、
      ``section_path``、``section_titles``（各级章节标题）、``product_code``；
      表格块另带 ``table_id`` / ``caption``。
    """

    chunk_id: str
    doc_id: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


def chunk_document(doc: CanonicalDocument) -> list[ChunkRecord]:
    """把一份规范文档切成 ``ChunkRecord`` 序列。``doc.blocks`` 为空 → ``ValueError``。"""
    if not doc.blocks:
        raise ValueError(f"文档 {doc.doc_id!r} 无 blocks，拒绝产出空索引（fail-fast）")

    # 预扫描: 标记"被下一张表吸收"的说明文字块（紧邻表格上方的段落 / note）。
    # 该块不再单独成 chunk，而是并入表首块文本前缀，避免内容重复。
    absorbed: set[int] = {
        i - 1
        for i, b in enumerate(doc.blocks)
        if b.kind == BlockKind.TABLE
        and i > 0
        and doc.blocks[i - 1].kind in (BlockKind.PARAGRAPH, BlockKind.NOTE)
        and doc.blocks[i - 1].text.strip()
    }

    records: list[ChunkRecord] = []
    seq = 0
    section = ""       # 运行中的节上下文（heading 未逐块带 section_path 时兜底）
    preamble = ""      # 待并入下一张表首块的说明文字
    # 型号键: doc_id 的第一个空格分词（"PA601-D54A 定制电源技术规格书" -> "PA601-D54A"）。
    # 检索过滤按它等值匹配（doc_id 带后缀，不能直接等值）。
    #
    # **公共知识不绑型号**（用户要求）: 域知识卡片（SourceKind.DOMAIN）是全局通用知识，
    # 不属于任何型号——把它当型号会造成（a）目录被卡片名灌掉，
    # （b）查询时被型号过滤误排除，“通用经验”变成“某型号专属”。
    # 故它的 ``product_code`` 留空、``scope="public"``，型号过滤时仍可命中（见 index 两腿的
    # 公共知识处理）。
    is_public = doc.source_kind is SourceKind.DOMAIN
    scope = "public" if is_public else "product"
    product_code = "" if is_public else doc.doc_id.split(" ")[0]

    def emit(text: str, meta: dict[str, Any]) -> None:
        nonlocal seq
        stripped = text.strip()
        if not stripped or _is_furniture(stripped):
            return
        section_path = str(meta.get("section_path") or "")
        records.append(
            ChunkRecord(
                chunk_id=f"{doc.doc_id}:{seq:04d}",
                doc_id=doc.doc_id,
                text=stripped,
                meta={
                    **meta,
                    "product_code": product_code,
                    # 公共知识 vs 型号规格（型号过滤时公共知识仍可命中）
                    "scope": scope,
                    # 各级章节标题（section_path 按 " > " 拆开）: 按"章节名"过滤检索时用
                    "section_titles": [
                        t.strip() for t in section_path.split(" > ") if t.strip()
                    ],
                },
            )
        )
        seq += 1

    for i, b in enumerate(doc.blocks):
        if b.kind == BlockKind.PAGEBREAK:
            continue
        cur_section = b.section_path or section

        if b.kind == BlockKind.HEADING:
            section = cur_section
            emit(b.text, {"kind": "section", "page": b.page,
                          "section_path": cur_section})
            continue

        if b.kind in (BlockKind.PARAGRAPH, BlockKind.NOTE, BlockKind.LIST):
            if i in absorbed:
                # 表格上方说明文字: 暂存不单独成块
                preamble = b.text.strip()[:PREAMBLE_CHARS]
                continue
            kind = "note" if b.kind == BlockKind.NOTE else "paragraph"
            for piece in _split_paragraph(b.text):
                emit(piece, {"kind": kind, "page": b.page,
                             "section_path": cur_section})
            continue

        if b.kind == BlockKind.TABLE and b.table:
            _emit_table_rows(b.table, emit, {"page": b.page,
                                             "section_path": cur_section}, preamble)
            preamble = ""
            continue

        # 其它未知 BlockKind: 保留可见性，宁可多一块也不静默丢弃
        if b.text.strip():
            emit(b.text, {"kind": "note", "page": b.page,
                          "section_path": cur_section})

    return records


def _emit_table_rows(
    table: TableModel,
    emit: Callable[[str, dict[str, Any]], None],
    base_meta: dict[str, Any],
    preamble: str,
) -> None:
    """表格逐行成 chunk: 表题 + 表头 + 该行 cells；说明文字并入首块。"""
    def render(cells: list[str]) -> str:
        parts: list[str] = []
        if table.caption:
            parts.append(table.caption)
        parts.append(" | ".join(table.headers))
        parts.append(" | ".join(cells))
        return "\n".join(parts)

    # 无数据行时退化为一条"仅表头 / 表题"块，保留表的可检索性
    rows = table.rows if table.rows else [[]]
    for idx, row in enumerate(rows):
        body = render(row)
        if idx == 0 and preamble:
            body = f"{preamble}\n{body}"
        meta = {**base_meta, "kind": "table_row",
                "table_id": table.id, "caption": table.caption}
        emit(body, meta)


def _split_paragraph(text: str) -> list[str]:
    """按 ~600 字切段; 超长按句读 / 换行边界滑窗, 重叠 80 字。"""
    text = text.strip()
    if len(text) <= TARGET_CHARS:
        return [text] if text else []

    chunks: list[str] = []
    n = len(text)
    start = 0
    floor = int(TARGET_CHARS * 0.6)  # 回找句读边界的最小长度
    while start < n:
        if n - start <= TARGET_CHARS:
            chunks.append(text[start:])
            break
        hard = start + TARGET_CHARS
        cut = hard
        j = hard - 1
        # 从目标点往前找最近的句读 / 换行（含该字符），找不到就硬切到目标点
        while j >= start + floor and j < n:
            if text[j] in _BOUNDARIES:
                cut = j + 1
                break
            j -= 1
        if cut <= start:  # 兜底防死循环
            cut = hard
        chunks.append(text[start:cut])
        nxt = cut - OVERLAP_CHARS
        start = nxt if nxt > start else cut
    return [c for c in (chunk.strip() for chunk in chunks) if c]
