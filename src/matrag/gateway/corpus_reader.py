"""gateway 语料读层（P1-6）: 读 data/corpus 装配层产物，支撑 MCP 工具直答。

装配层（P0-6 parse CLI）产物契约（本层只读，绝不改写）:
- ``<corpus_dir>/<doc_id>.md``                  CanonicalDocument.markdown() 规范化文本
- ``<corpus_dir>/<doc_id>.tables.json``         ``[TableModel.to_dict()]`` 表格清单
- ``<reports_dir>/<doc_id>.parse_report.json``  ``ParseReport.to_dict()`` 对账报告

设计约束:
- **不向传输层抛异常**: 文件不存在 → ``None``; 损坏/不可读 → 带 ``error`` 字段的
  dict 并写 ``logging`` 留痕（fail-closed 且可查询，绝不静默吞错）。
- **doc_id 安全**: 拒绝含路径分隔符/``..`` 的 doc_id（防目录穿越），视为未找到。
- 标题树复用解析层 :func:`matrag.parse.markdown_reader.extract_markdown`，
  不重造 Markdown 语义; 页码优先取 tables.json 的 ``source.page``（md 单页投影兜底 0）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from matrag.parse.markdown_reader import extract_markdown
from matrag.types import BlockKind

logger = logging.getLogger(__name__)

#: 装配层产物文件名后缀（doc_id 即文件名去掉后缀的部分）。
TABLES_SUFFIX = ".tables.json"
REPORT_SUFFIX = ".parse_report.json"

#: md 首个 ATX 标题（作为文档标题兜底）。
_ATX_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
#: 合法 doc_id: 不含路径分隔符与冒号等，防目录穿越/异常输入。
_SAFE_DOC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-一-龥 ]*$")


@dataclass(frozen=True)
class CorpusDoc:
    """语料里的一份文档: md 与 tables.json 成对出现（md 缺失时 ``md_path`` 仍给出）。"""

    doc_id: str
    title: str
    md_path: Path
    tables_path: Path


def list_documents(corpus_dir: Path) -> list[CorpusDoc]:
    """扫描 ``*.tables.json`` 配对同名 ``*.md``，按 doc_id 排序返回。

    只有 tables.json 没有 md 的条目也返回（装配层半成品，调用方可见），
    标题退化为 doc_id。
    """
    base = Path(corpus_dir)
    if not base.is_dir():
        logger.warning("corpus 目录不存在: %s", base)
        return []
    docs: list[CorpusDoc] = []
    for tables_path in sorted(base.glob(f"*{TABLES_SUFFIX}")):
        doc_id = tables_path.name[: -len(TABLES_SUFFIX)]
        if not doc_id:
            continue
        md_path = base / f"{doc_id}.md"
        docs.append(
            CorpusDoc(
                doc_id=doc_id,
                title=_md_title(md_path) or doc_id,
                md_path=md_path,
                tables_path=tables_path,
            )
        )
    return docs


def get_table(doc_id: str, table_id: str, *, corpus_dir: Path) -> dict | None:
    """读 ``<doc_id>.tables.json`` 里 ``id == table_id`` 的表（``TableModel.to_dict()`` 形状）。

    文档/表不存在、doc_id 非法、JSON 损坏 → ``None``（损坏会写日志留痕）。
    """
    if not _safe_doc_id(doc_id):
        logger.warning("拒绝非法 doc_id=%r", doc_id)
        return None
    tables, error = _read_json(_tables_path(doc_id, corpus_dir))
    if error is not None:
        logger.warning("读取表格清单失败 doc_id=%r: %s", doc_id, error)
        return None
    if not isinstance(tables, list):
        logger.warning("表格清单不是 list doc_id=%r", doc_id)
        return None
    for item in tables:
        if isinstance(item, dict) and str(item.get("id", "")) == table_id:
            return item
    return None


def get_doc_outline(doc_id: str, *, corpus_dir: Path) -> dict:
    """从 md 抽标题树（# 层级）+ 表格清单（table_id/caption/section_path/page）。

    返回 ``{"doc_id", "title", "headings": [...], "tables": [...], "error"?: str}``。
    md 不存在 → ``error="md_not_found"``; md 损坏 → ``error="md_unreadable: <原因>"``。
    """
    if not _safe_doc_id(doc_id):
        logger.warning("拒绝非法 doc_id=%r", doc_id)
        return _outline(doc_id, error="invalid_doc_id")
    md_path = _md_path(doc_id, corpus_dir)
    if not md_path.is_file():
        return _outline(doc_id, error="md_not_found")
    try:
        md_doc = extract_markdown(md_path)
    except Exception as exc:  # noqa: BLE001 —— 解析层失败转为结构化 error 并留痕
        logger.warning("md 解析失败 doc_id=%r: %s: %s", doc_id, type(exc).__name__, exc)
        return _outline(doc_id, error=f"md_unreadable: {type(exc).__name__}")

    headings: list[dict[str, Any]] = []
    md_tables: list[dict[str, Any]] = []
    for block in md_doc.blocks:
        if block.kind is BlockKind.HEADING and block.text:
            headings.append(
                {
                    "level": int(block.level),
                    "text": block.text,
                    "section_path": block.section_path,
                }
            )
        elif block.kind is BlockKind.TABLE and block.text:
            md_tables.append({"table_id": block.text, "section_path": block.section_path})

    pages = _table_page_index(_tables_path(doc_id, corpus_dir))
    for entry in md_tables:
        info = pages.get(entry["table_id"], {})
        entry["caption"] = info.get("caption", "")
        entry["page"] = info.get("page", 0)
    return _outline(doc_id, title=md_doc.title or doc_id, headings=headings, tables=md_tables)


def read_report(doc_id: str, *, reports_dir: Path) -> dict | None:
    """读 ``<doc_id>.parse_report.json``（``ParseReport.to_dict()`` 形状）。

    报告不存在/损坏/非法 doc_id → ``None``（损坏写日志留痕）。
    """
    if not _safe_doc_id(doc_id):
        logger.warning("拒绝非法 doc_id=%r", doc_id)
        return None
    report, error = _read_json(_report_path(doc_id, reports_dir))
    if error is not None:
        logger.warning("读取解析报告失败 doc_id=%r: %s", doc_id, error)
        return None
    if not isinstance(report, dict):
        logger.warning("解析报告不是 object doc_id=%r", doc_id)
        return None
    return report


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _outline(
    doc_id: str,
    *,
    title: str = "",
    headings: list[dict[str, Any]] | None = None,
    tables: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> dict:
    """组装 outline 返回结构（error 仅在异常分支出现，见 :func:`get_doc_outline`）。"""
    out: dict[str, Any] = {
        "doc_id": doc_id,
        "title": title or doc_id,
        "headings": list(headings or []),
        "tables": list(tables or []),
    }
    if error is not None:
        out["error"] = error
    return out


def _read_json(path: Path) -> tuple[Any | None, str | None]:
    """读 JSON 文件。返回 ``(data, None)`` 或 ``(None, 错误描述)``。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "not_found"
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"JSONDecodeError: {exc}"


def _md_title(md_path: Path) -> str:
    """md 首个 ATX 一级标题；读不到（不存在/损坏）返回空串。"""
    try:
        text = md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _ATX_H1_RE.match(stripped)
        return match.group(1) if match else ""
    return ""


def _table_page_index(tables_path: Path) -> dict[str, dict[str, Any]]:
    """tables.json → ``{table_id: {"caption", "page"}}``；读失败返回 {}（页码退化为 0）。"""
    tables, error = _read_json(tables_path)
    if error is not None or not isinstance(tables, list):
        return {}
    index: dict[str, dict[str, Any]] = {}
    for item in tables:
        if not isinstance(item, dict):
            continue
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        index[str(item.get("id", ""))] = {
            "caption": str(item.get("caption", "")),
            "page": _as_int(source.get("page")),
        }
    return index


def _as_int(value: Any) -> int:
    """宽松取整: 非整数值（None/字符串/越界）一律退化为 0，不抛给传输层。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def _safe_doc_id(doc_id: str) -> bool:
    """doc_id 合法性: 非空、无路径分隔符/``..``、限定字符集（防目录穿越）。"""
    if not isinstance(doc_id, str) or not doc_id.strip():
        return False
    if "/" in doc_id or "\\" in doc_id or ".." in doc_id:
        return False
    return bool(_SAFE_DOC_ID_RE.match(doc_id))


def _doc_paths(doc_id: str, corpus_dir: Path) -> tuple[Path, Path]:
    """``(md_path, tables_path)`` 对（doc_id 已由调用方验证过安全性）。"""
    base = Path(corpus_dir)
    return base / f"{doc_id}.md", base / f"{doc_id}{TABLES_SUFFIX}"


def _md_path(doc_id: str, corpus_dir: Path) -> Path:
    return _doc_paths(doc_id, corpus_dir)[0]


def _tables_path(doc_id: str, corpus_dir: Path) -> Path:
    return _doc_paths(doc_id, corpus_dir)[1]


def _report_path(doc_id: str, reports_dir: Path) -> Path:
    return Path(reports_dir) / f"{doc_id}{REPORT_SUFFIX}"


__all__ = [
    "CorpusDoc",
    "REPORT_SUFFIX",
    "TABLES_SUFFIX",
    "get_doc_outline",
    "get_table",
    "list_documents",
    "read_report",
]
