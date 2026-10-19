"""解析验收脚本（P0-6/P0-7）: 人眼复核入口 + 合并单元格样本集导出。

用法:
    python -m scripts.verify_parse <doc.pdf|doc.md> [--show N]
    python -m scripts.verify_parse CY0177-75V-20A --show 2      # 只打印前 2 张合并表

行为:
1. 复用 matrag.parse_pipeline.parse_document（不重跑则直接读 data/corpus 产物）。
2. 打印对账报告摘要 + 每张表的 flags。
3. 对含合并单元格的表打印重建后的 markdown（人眼复核: 去规范化是否重复、叶表头是否平铺）。
4. --dump-samples: 把"含合并的表 + 重建结果 + 草稿摘要"导出到 data/reports/<doc>.samples.json，
   作为解析器评测基准（P0-7）的样本集——将来换外部解析器时, 同一份 samples 重跑对比。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from matrag.config import settings
from matrag.parse_pipeline import ParsedDocument, parse_document
from matrag.types import TableModel


def _summarize(parsed: ParsedDocument) -> str:
    r = parsed.report
    lines = [
        f"文档: {parsed.doc.doc_id} ({parsed.doc.source_kind.value})",
        f"provider: {r.provider}  通过闸门: {r.ok}",
        f"表数量: {len(r.tables)}  未决复核: {len(r.unresolved)}  stats: {r.stats}",
    ]
    for t in r.tables:
        mark = "✓" if not t.flags else "✗"
        lines.append(
            f"  {mark} {t.table_id} p{t.page} {t.n_rows}x{t.n_cols} merged={t.merged_cells}"
            + (f" flags={t.flags}" if t.flags else "")
        )
    return "\n".join(lines)


def _show_merged(parsed: ParsedDocument, limit: int) -> None:
    shown = 0
    for tbl in parsed.doc.tables():
        has_merge = any(m.startswith(("rowspan", "colspan")) for m in tbl.marks) or "flattened_header" in tbl.marks
        if not has_merge:
            continue
        print(f"\n===== {tbl.id} (p{tbl.source.get('page')}) {tbl.caption} =====")
        print(f"marks: {tbl.marks}")
        print(tbl.to_markdown()[:2000])
        shown += 1
        if shown >= limit:
            break
    if shown == 0:
        print("\n(无重建合并表——可能全部直通或文档无合并)")


def _dump_samples(parsed: ParsedDocument, out: Path) -> Path:
    """导出评测基准样本: 每张含合并/重建痕迹的表, 带草稿原文摘要与重建结果。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    samples = []
    for tbl in parsed.doc.tables():
        src = tbl.source or {}
        samples.append(
            {
                "table_id": tbl.id,
                "page": src.get("page"),
                "section_path": src.get("section_path"),
                "caption": tbl.caption,
                "marks": tbl.marks,
                "confidence": tbl.confidence,
                "headers": tbl.headers,
                "rows": tbl.rows,
                "reconstruct_error": src.get("reconstruct_error"),
            }
        )
    out.write_text(
        json.dumps({"doc_id": parsed.doc.doc_id, "tables": samples}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out


def _verify_from_corpus(doc_id: str, *, show: int) -> int:
    """从 data/corpus 已产物复核（不重跑 LLM）: 读 tables.json + parse_report.json。"""
    tables_path = settings.corpus_dir / f"{doc_id}.tables.json"
    report_path = settings.reports_dir / f"{doc_id}.parse_report.json"
    if not tables_path.exists():
        print(f"[FAIL] 无语料产物: {tables_path}（先跑 parse）")
        return 2
    tables = json.loads(tables_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    print(f"文档: {doc_id}（语料复核）  闸门: {report.get('ok')}  表: {len(tables)}")
    for t in tables:
        flags = [f for f in (t.get("source", {}).get("marks") or []) if f.startswith("ERROR")]
        print(f"  {'x' if flags else '-'} {t['id']} p{t.get('source', {}).get('page')} "
              f"{len(t['rows'])}x{len(t['headers'])} merged_marks={len(t.get('marks', []))}")
    shown = 0
    for t in tables:
        if not t.get("marks"):
            continue
        print(f"\n===== {t['id']} {t.get('caption', '')} =====")
        m = TableModel(id=t["id"], caption=t.get("caption", ""), headers=t["headers"],
                       rows=t["rows"], marks=t.get("marks", []),
                       source=t.get("source", {}))
        print(m.to_markdown()[:2000])
        shown += 1
        if shown >= show:
            break
    return 0 if report.get("ok", True) else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="verify_parse")
    ap.add_argument("doc", help="文档名（docs 下文件名去扩展名）或完整路径")
    ap.add_argument("--show", type=int, default=2, help="打印前 N 张重建合并表")
    ap.add_argument("--dump-samples", action="store_true", help="导出评测样本集")
    ap.add_argument(
        "--from-corpus",
        action="store_true",
        help="直接读 data/corpus 已产物（不重跑解析/LLM）",
    )
    args = ap.parse_args(argv)

    path = Path(args.doc)
    if not path.exists():
        for ext in (".pdf", ".md"):
            cand = settings.data_dir.parent / "docs" / f"{args.doc}{ext}"
            if cand.exists():
                path = cand
                break
    if not path.exists():
        print(f"[FAIL] 找不到文档: {args.doc}")
        return 2

    if args.from_corpus:
        return _verify_from_corpus(args.doc, show=args.show)

    parsed = parse_document(path)
    print(_summarize(parsed))
    _show_merged(parsed, args.show)

    if args.dump_samples:
        out = settings.reports_dir / f"{parsed.doc.doc_id}.samples.json"
        print(f"[samples] {out}")

    return 0 if parsed.ok else 2


if __name__ == "__main__":
    sys.exit(main())
