"""matrag CLI（运维面，用户交互在 ATEStudio/Agent 前端，不在此）。

子命令:
    parse    解析规格书 → data/corpus（带对账报告；有 ERROR 则非零退出）
    doctor   配置与依赖健康检查（不写任何文件）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from matrag.config import settings
from matrag.parse_pipeline import parse_document


def _cmd_parse(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.files]
    settings.ensure_dirs()
    failed: list[str] = []
    for p in paths:
        try:
            parsed = parse_document(p)
        except Exception as exc:  # 解析级失败: 报告而非静默跳过
            print(f"[FAIL] {p}: {type(exc).__name__}: {exc}")
            failed.append(str(p))
            continue
        paths_out = parsed.save()
        n_tab = len(parsed.doc.tables())
        print(
            f"[{ 'OK' if parsed.ok else 'GATE'} ] {p.name}: tables={n_tab} "
            f"unresolved={len(parsed.report.unresolved)} -> {paths_out['md'].name}"
        )
        if not parsed.ok:
            failed.append(str(p))
            for t in parsed.report.tables:
                for f in t.flags:
                    if f.startswith("ERROR"):
                        print(f"    - {t.table_id} (p{t.page}): {f}")
            for u in parsed.report.unresolved:
                print(f"    ? 待人工复核: {u}")
    if failed:
        print(f"\n{len(failed)} 个文档未通过对账闸门（fail-closed，不进索引）")
        return 2
    print("全部通过对账闸门")
    return 0


def _cmd_doctor(_: argparse.Namespace) -> int:
    print(f"profile={settings.profile}  vector={settings.vector_backend}  "
          f"keyword={settings.keyword_backend}  graph={settings.graph_backend}")
    checks = {
        "llm": settings.llm_base and settings.llm_api_key and settings.llm_model,
        "embedding": settings.embed_base and settings.embed_api_key and settings.embed_model,
    }
    for name, ok in checks.items():
        print(f"  {name:10s}: {'configured' if ok else 'MISSING (live 功能将被 fail-closed 拒绝)'}")
    for mod in ("fitz", "pymupdf", "semantica", "jieba", "pytest"):
        try:
            __import__(mod)
            print(f"  {mod:10s}: importable")
        except Exception as exc:
            print(f"  {mod:10s}: IMPORT FAILED {exc}")
            return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="matrag")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse", help="解析规格书 → 规范化语料")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=_cmd_parse)

    d = sub.add_parser("doctor", help="健康检查")
    d.set_defaults(func=_cmd_doctor)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
