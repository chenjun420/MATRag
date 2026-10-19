"""解析装配层（P0-6）: PDF/MD → CanonicalDocument + ParseReport。

职责（只做装配，不重造解析语义）:
1. 按扩展名分发到对应解析 provider（geometry 本地几何 / markdown 直读）。
2. 把 provider 产出的 TableDraft 喂给重建器（reconstruct.reconstruct_table）得到 TableModel。
3. 把重建结果填回 Block(kind=TABLE, text=table_id) 占位，维护 heading 栈填 section_path。
4. 对账（verify.build_parse_report），落盘 data/corpus/<doc_id>.md / .tables.json / .doc.json + 报告。
5. fail-closed: 报告不 ok 的文档不进索引，CLI 以非零退出码返回。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from matrag.config import settings
from matrag.errors import MatragError, ProviderNotConfiguredError
from matrag.llm import LLMClient, get_llm
from matrag.parse import geometry, reconstruct, verify
from matrag.types import (
    Block,
    BlockKind,
    CanonicalDocument,
    ParseReport,
    SourceKind,
    TableDraft,
    TableModel,
)

_EXT_TO_KIND = {".pdf": SourceKind.PDF, ".md": SourceKind.MARKDOWN, ".markdown": SourceKind.MARKDOWN}

#: markdown() 写在每个块前的锚点注释: <!-- page:N section:S -->
_ANCHOR_RE = re.compile(r"<!--\s*page:(\d+)\s+section:(.*?)\s*-->")


@dataclass
class ParsedDocument:
    """一份文档的解析产出（含对账报告）。"""

    doc: CanonicalDocument
    report: ParseReport

    @property
    def ok(self) -> bool:
        return self.report.ok

    def save(self, out_dir: Path | None = None) -> dict[str, Path]:
        """写 data/corpus/<doc_id>.md 与 .tables.json，报告写 data/reports/<doc_id>.parse_report.json。"""
        out_dir = out_dir or settings.corpus_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path = out_dir / f"{self.doc.doc_id}.md"
        tables_path = out_dir / f"{self.doc.doc_id}.tables.json"
        doc_path = out_dir / f"{self.doc.doc_id}.doc.json"
        report_path = settings.reports_dir / f"{self.doc.doc_id}.parse_report.json"
        md_path.write_text(self.doc.markdown(), encoding="utf-8")
        tables_path.write_text(
            json.dumps([t.to_dict() for t in self.doc.tables()], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        doc_path.write_text(json.dumps(self.doc.to_dict(), ensure_ascii=False), encoding="utf-8")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(self.report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"md": md_path, "tables": tables_path, "doc": doc_path, "report": report_path}


def load_canonical(doc_id: str, *, corpus_dir: Path | None = None) -> CanonicalDocument:
    """从 data/corpus/<doc_id>.doc.json 重建 CanonicalDocument（不重跑解析/LLM）。

    兼容回退: 无 doc.json 时用 <doc_id>.md（markdown 解析 + 锚点恢复 page/section）
    + <doc_id>.tables.json（按出现顺序挂接表格）重建。
    """
    corpus_dir = corpus_dir or settings.corpus_dir
    path = corpus_dir / f"{doc_id}.doc.json"
    if path.exists():
        return CanonicalDocument.from_dict(json.loads(path.read_text(encoding="utf-8")))
    return _load_canonical_from_markdown(doc_id, corpus_dir)


def _load_canonical_from_markdown(doc_id: str, corpus_dir: Path) -> CanonicalDocument:
    """md + tables.json 回退加载: 锚点注释恢复每块 page/section，表格按序挂接。"""
    md_path = corpus_dir / f"{doc_id}.md"
    tables_path = corpus_dir / f"{doc_id}.tables.json"
    if not (md_path.exists() and tables_path.exists()):
        raise FileNotFoundError(f"{md_path} 或 {tables_path} 缺失（先跑 parse）")
    from matrag.parse import geometry

    md_doc = geometry.extract_markdown(md_path)
    tables = json.loads(tables_path.read_text(encoding="utf-8"))
    by_order = list(tables)

    blocks: list[Block] = []
    pending_page, pending_section = 0, ""
    table_i = 0
    for b in md_doc.blocks:
        stripped = b.text.strip()
        if b.kind in (BlockKind.PARAGRAPH, BlockKind.NOTE) and stripped.startswith("<!--"):
            m = _ANCHOR_RE.search(stripped)
            if m:
                pending_page, pending_section = int(m.group(1)), m.group(2)
            continue  # 锚点是元信息，不产出块
        if b.kind == BlockKind.TABLE:
            if table_i >= len(by_order):
                continue  # md 表格多于 tables.json: 信任 tables.json
            td = by_order[table_i]
            table_i += 1
            b.table = TableModel(
                id=td.get("id") or b.text,
                caption=td.get("caption", ""),
                headers=list(td.get("headers", [])),
                rows=[list(r) for r in td.get("rows", [])],
                marks=list(td.get("marks", [])),
                confidence=float(td.get("confidence", 1.0)),
                source=dict(td.get("source", {})),
            )
            b.text = ""
        b.page = pending_page
        if pending_section:
            b.section_path = pending_section
        blocks.append(b)

    return CanonicalDocument(
        doc_id=doc_id,
        title=md_doc.title or doc_id,
        source_path=str(md_path),
        source_kind=SourceKind.MARKDOWN,
        meta={"reconstructed_from": "md+tables.json"},
        blocks=blocks,
    )


def _error_table(draft: TableDraft, flag: str) -> TableModel:
    """闸门失败标记表: 不可被引用为判据，只在报告里可见。"""
    return TableModel(
        id=draft.draft_id,
        caption=draft.caption,
        headers=[],
        rows=[],
        marks=[flag],
        confidence=0.0,
        source={"page": draft.page, "section_path": ""},
    )


@dataclass
class _SyntheticPage:
    """Markdown 无页概念, 投影为单页。"""

    page: int
    blocks: list[Block]
    table_drafts: list[TableDraft]


def parse_document(
    path: str | Path,
    *,
    llm: LLMClient | None = None,
    provider: str | None = None,
) -> ParsedDocument:
    """解析单份文档。llm=None 时取全局门面；未配置且文档含合并表格 → fail-closed 报错表。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    ext = path.suffix.lower()
    kind = _EXT_TO_KIND.get(ext)
    if kind is None:
        raise MatragError(f"不支持的扩展名 {ext!r}（支持: {sorted(_EXT_TO_KIND)}）: {path}")

    provider = provider or settings.parser_provider
    if provider != "local":
        raise ProviderNotConfiguredError(
            f"解析器 provider={provider!r} 尚未接入（当前仅 local；"
            "外部解析器须先过 verify 基准才能启用）"
        )

    pages, title = _run_provider(path, kind)
    llm = llm or get_llm()

    drafts: dict[str, TableDraft] = {}
    for page in pages:
        for d in page.table_drafts:
            drafts[d.draft_id] = d

    blocks: list[Block] = []
    models: list[tuple[TableModel, TableDraft]] = []
    heading_stack: list[tuple[int, str]] = []

    for page in pages:
        for b in page.blocks:
            nb = Block(
                kind=b.kind,
                text=b.text,
                level=b.level,
                page=getattr(page, "page", 0) or b.page,
                section_path=b.section_path,
                table=None,
            )
            if b.kind == BlockKind.HEADING and b.level:
                while heading_stack and heading_stack[-1][0] >= b.level:
                    heading_stack.pop()
                heading_stack.append((b.level, b.text))
            nb.section_path = (
                " > ".join(t for _, t in heading_stack) if heading_stack else b.section_path
            )
            blocks.append(nb)
            if b.kind != BlockKind.TABLE:
                continue
            draft = drafts.get(b.text)  # 表格占位块的 text 即 table_id
            if draft is None:
                nb.kind = BlockKind.NOTE
                nb.text = f"[missing-table-draft] {b.text}"
                continue
            model = _reconstruct_with_fallback(draft, llm, kind)
            nb.text = ""
            nb.table = model
            model.source = {
                "page": draft.page,
                "section_path": nb.section_path,
                "provider": provider,
                "marks": list(model.marks),
            }
            models.append((model, draft))

    doc = CanonicalDocument(
        doc_id=path.stem,
        title=title or path.stem,
        source_path=str(path),
        source_kind=kind,
        meta={"provider": provider, "ext": ext, "size_bytes": path.stat().st_size},
        blocks=blocks,
    )
    report = verify.build_parse_report(
        doc_id=doc.doc_id, source_path=doc.source_path, provider=provider, models=models
    )
    report.stats["n_tables"] = len(models)
    return ParsedDocument(doc=doc, report=report)


def _run_provider(path: Path, kind: SourceKind) -> tuple[list[Any], str]:
    """返回 (pages, title)。pages[i] 须含 page/blocks/table_drafts。"""
    if kind == SourceKind.PDF:
        doc = geometry.extract_pdf(path)
        return list(doc.pages), doc.title or ""
    if kind == SourceKind.MARKDOWN:
        md_doc = geometry.extract_markdown(path)
        page = _SyntheticPage(
            page=0, blocks=list(md_doc.blocks), table_drafts=list(md_doc.table_drafts)
        )
        return [page], getattr(md_doc, "title", "") or ""
    raise MatragError(f"未接管的 SourceKind: {kind}")


def _reconstruct_with_fallback(
    draft: TableDraft, llm: LLMClient, kind: SourceKind
) -> TableModel:
    """LLM 不可用 + 存在合并 → 明确标记，不静默降级。"""
    if draft.merged_cells > 0 and not llm.configured:
        return _error_table(draft, "ERROR_LLM_NOT_CONFIGURED")
    return reconstruct.reconstruct_table(draft, llm=llm, source_kind=kind)
