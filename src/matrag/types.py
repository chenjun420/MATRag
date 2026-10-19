"""规范文档模型（解析层契约，所有解析器 provider 必须产出本模型）。

这是整个系统真正拥有的资产: 实现可换（local 几何+LLM、docling、MinerU、Mathpix…），
契约不动。下游（抽取/索引/检索/Agent）只认这里定义的结构。

- ``CanonicalDocument``: 一份规范化文档（块序列 + 出处锚点）。
- ``TableModel``: 已重建完毕的表格 —— 行长方等、合并值已去规范化（rowspan 向下补、
  colspan 向右补）、换行以 ``<br>`` 表示、竖线转义。``marks`` 记录重建痕迹。
- ``TableDraft``: 重建前的草稿（几何 + 原文），由解析 provider 产出，喂给重建器。
- ``ParseReport``: 对账报告。``ok`` 为 False 时该文档不得进入索引（fail-closed）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SourceKind(StrEnum):
    PDF = "pdf"
    MARKDOWN = "markdown"
    HTML = "html"
    DOMAIN = "domain"  # 域知识卡片（非产品规格书）


class BlockKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    NOTE = "note"
    PAGEBREAK = "pagebreak"


@dataclass
class Cell:
    """草稿单元格: 几何矩形 + 原文 + 合并跨度。``text`` 允许为空字符串。"""

    rect: tuple[float, float, float, float]  # x0, y0, x1, y1 (pt)
    text: str
    row_span: int = 1
    col_span: int = 1


@dataclass
class TableDraft:
    """重建前草稿。``grid`` 为密集网格 (n_rows x n_cols)，None 表示被合并覆盖的占位。

    约定: 网格左上角为第一格; 合并格的 ``row_span/col_span`` >= 1;
    被其覆盖的后续槽位为 None（覆盖它的 Cell 才是非 None）。
    """

    draft_id: str
    page: int
    table_index: int
    caption: str
    n_rows: int
    n_cols: int
    grid: list[list[Cell | None]] = field(default_factory=list)
    reading_text: str = ""  # 页面/表格周边阅读顺序原文（补充 LLM 判读）
    source_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def merged_cells(self) -> int:
        return sum(
            1
            for row in self.grid
            for c in row
            if c is not None and (c.row_span > 1 or c.col_span > 1)
        )


@dataclass
class TableModel:
    """重建后的规范表格。

    - ``headers``: 平铺后的唯一叶表头（合并表头逐级拼接，如"输入欠压保护/欠压点"）。
    - ``rows``: 每行 len(rows[i]) == len(headers)；合并值已补全到每个被覆盖格。
    - ``marks``: 重建痕迹，如 ``rowspan:2@col0``、``flattened_header``。
    """

    id: str
    caption: str
    headers: list[str]
    rows: list[list[str]]
    marks: list[str] = field(default_factory=list)
    confidence: float = 1.0
    source: dict[str, Any] = field(default_factory=dict)  # page/section_path/provider 等

    def to_markdown(self) -> str:
        """RFC 兼容 GFM 表格: ``<br>`` 折行、``|`` 转义。"""
        def esc(t: str) -> str:
            return t.replace("|", "\\|").replace("\n", "<br>")
        lines = ["| " + " | ".join(esc(h) for h in self.headers) + " |"]
        lines.append("|" + "---|" * len(self.headers))
        for r in self.rows:
            lines.append("| " + " | ".join(esc(c) for c in r) + " |")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "caption": self.caption,
            "headers": list(self.headers),
            "rows": [list(r) for r in self.rows],
            "marks": list(self.marks),
            "confidence": self.confidence,
            "source": dict(self.source),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TableModel:
        return cls(
            id=d["id"],
            caption=d.get("caption", ""),
            headers=list(d.get("headers", [])),
            rows=[list(r) for r in d.get("rows", [])],
            marks=list(d.get("marks", [])),
            confidence=float(d.get("confidence", 1.0)),
            source=dict(d.get("source", {})),
        )


@dataclass
class Block:
    kind: BlockKind
    text: str = ""
    level: int = 0  # heading 层级
    section_path: str = ""  # 如 "4 > 技术要求 > 4.3 功能/性能要求"
    page: int = 0
    table: TableModel | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "text": self.text,
            "level": self.level,
            "section_path": self.section_path,
            "page": self.page,
            "table": self.table.to_dict() if self.table else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Block:
        return cls(
            kind=BlockKind(d["kind"]),
            text=d.get("text", ""),
            level=int(d.get("level", 0)),
            section_path=d.get("section_path", ""),
            page=int(d.get("page", 0)),
            table=TableModel.from_dict(d["table"]) if d.get("table") else None,
        )


@dataclass
class CanonicalDocument:
    doc_id: str  # 稳定 id，如文件名 stem
    title: str
    source_path: str
    source_kind: SourceKind
    meta: dict[str, Any] = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)

    def tables(self) -> list[TableModel]:
        return [b.table for b in self.blocks if b.kind == BlockKind.TABLE and b.table]

    def markdown(self) -> str:
        """渲染为规范 markdown: 每个块前带 page/section 锚点注释，供回退加载恢复。"""
        parts: list[str] = []
        for b in self.blocks:
            anchor = f"<!-- page:{b.page} section:{b.section_path} -->"
            if b.kind == BlockKind.HEADING:
                parts.append(anchor)
                parts.append(f"\n{'#' * max(1, b.level)} {b.text}\n")
            elif b.kind == BlockKind.TABLE and b.table:
                parts.append(anchor)
                if b.table.caption:
                    parts.append(f"**{b.table.caption}**\n")
                parts.append(b.table.to_markdown() + "\n")
            elif b.kind == BlockKind.PAGEBREAK:
                parts.append(anchor)
            elif b.text:
                parts.append(anchor)
                parts.append(b.text + "\n")
        return "\n".join(parts).strip() + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "source_path": self.source_path,
            "source_kind": self.source_kind.value,
            "meta": dict(self.meta),
            "blocks": [b.to_dict() for b in self.blocks],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CanonicalDocument:
        return cls(
            doc_id=d["doc_id"],
            title=d.get("title", ""),
            source_path=d.get("source_path", ""),
            source_kind=SourceKind(d.get("source_kind", "markdown")),
            meta=dict(d.get("meta", {})),
            blocks=[Block.from_dict(b) for b in d.get("blocks", [])],
        )


@dataclass
class TableReport:
    table_id: str
    page: int
    n_rows: int
    n_cols: int
    merged_cells: int
    flags: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class ParseReport:
    doc_id: str
    source_path: str
    provider: str
    tables: list[TableReport] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)  # 必须人工复核的 table_id
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """fail-closed: 有未决项或硬伤 flags 即不算通过。"""
        return not self.unresolved and not any(
            f.startswith("ERROR") for t in self.tables for f in t.flags
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "source_path": self.source_path,
            "provider": self.provider,
            "ok": self.ok,
            "tables": [t.__dict__ for t in self.tables],
            "unresolved": list(self.unresolved),
            "stats": dict(self.stats),
        }
