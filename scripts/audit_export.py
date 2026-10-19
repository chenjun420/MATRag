"""审计轨 PROV-O 导出（审计交付物）: 把 provenance 库序列化为 W3C PROV-O RDF。

用法::

    python -m scripts.audit_export                             # turtle -> stdout
    python -m scripts.audit_export --format jsonld --out data/reports/audit.jsonld
    python -m scripts.audit_export --db data/index/provenance.db --format ntriples

口径:

- 数据源 = ``settings.store_dir/provenance.db``（或 ``--db`` 显式指定）;
- 导出内容 = semantica ``ProvenanceManager.export_prov`` 直通: 每条审计记录是一个
  ``prov:Entity``（``matrag_answer``/``matrag_retrieval``），写入活动是
  ``prov:Activity``，模型/工具是 ``prov:Agent``，命中锚点/问题按 PROV-O 标准映射;
- stdout 只输出 RDF 本体（摘要走 stderr），便于 ``> file.ttl`` 管道;
- 审计库不存在即显式失败（不在导出路径上凭空造一个空库）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from matrag.config import settings
from matrag.provenance import (
    DB_FILENAME,
    ENTITY_TYPE_PREFIX,
    PROV_EXPORT_FORMATS,
    AuditTrail,
    ProvenanceError,
)


def default_db_path() -> Path:
    """settings 口径的审计库路径（与 :class:`AuditTrail` 默认落点一致）。"""
    return settings.store_dir / DB_FILENAME


def export(
    db_path: Path | str,
    *,
    fmt: str = "turtle",
    base_uri: str | None = None,
) -> tuple[str, int]:
    """导出审计库为 PROV-O RDF; 返回 ``(RDF 文本, 审计记录条数)``。

    Raises:
        FileNotFoundError: 审计库不存在（没有任何审计可导出）;
        ValueError: ``fmt`` 不在 :data:`~matrag.provenance.PROV_EXPORT_FORMATS`;
        ProvenanceError: 读取 / 序列化失败（含 rdflib 缺失）。
    """
    db = Path(db_path)
    if not db.exists():
        raise FileNotFoundError(
            f"审计库不存在: {db}（先产生审计记录，或检查 settings.store_dir）"
        )
    with AuditTrail("sqlite", db_path=db) as trail:
        entries = trail.storage.retrieve_all()
        count = sum(
            1 for entry in entries if entry.entity_type.startswith(ENTITY_TYPE_PREFIX)
        )
        rdf = trail.export_prov(format=fmt, base_uri=base_uri)
    return rdf, count


def main(argv: list[str] | None = None) -> int:
    """CLI 入口: 解析参数 → 导出 → 写 stdout 或文件; 失败显式返回 2。"""
    parser = argparse.ArgumentParser(
        prog="matrag-audit-export",
        description="审计轨 PROV-O 导出（turtle/ntriples/jsonld）",
    )
    parser.add_argument(
        "--db", type=Path, default=None,
        help=f"审计库路径（默认 {DB_FILENAME}，即 settings.store_dir 下）",
    )
    parser.add_argument(
        "--format", default="turtle", choices=PROV_EXPORT_FORMATS,
        help="RDF 序列化格式（默认 turtle）",
    )
    parser.add_argument(
        "--base-uri", default=None,
        help="实体 URI 命名空间（默认 semantica DEFAULT_BASE_URI，与 KG 导出同口径）",
    )
    parser.add_argument("--out", type=Path, default=None, help="输出文件（默认 stdout）")
    args = parser.parse_args(argv)

    db = args.db if args.db is not None else default_db_path()
    try:
        rdf, count = export(db, fmt=args.format, base_uri=args.base_uri)
    except (FileNotFoundError, ValueError, ProvenanceError) as exc:
        print(f"[audit-export] 导出失败: {exc}", file=sys.stderr)
        return 2

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rdf, encoding="utf-8")
        print(f"[audit-export] {count} 条审计记录 -> {args.out}", file=sys.stderr)
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # 非文本流 / 不支持重配（重定向时）
            pass
        sys.stdout.write(rdf)
        if not rdf.endswith("\n"):
            sys.stdout.write("\n")
        print(f"[audit-export] {count} 条审计记录", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
