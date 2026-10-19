"""MCP 工具注册（P1-6 检索面，FastMCP 1.x 装饰器定义）。

工具清单（ATEStudio/Agent 经 streamable-http 调用）:

===============  ==========================================
工具名            语义
===============  ==========================================
``ping``         服务自省（ok / service / version）
``list_documents`` 语料目录 + 未入索引文档清单
``search_context`` 混合检索（走 ``HybridRetriever.retrieve``）
``ask``          应答合成（走 ``matrag.answer.answer_question``）
``get_table``    按 table_id 取表格全文（锚点可回溯）
``doc_outline``  文档标题树 + 表格清单
``parse_report`` 解析对账报告（闸门结果）
===============  ==========================================

命名约定: 检索入口取 ``search_context``（调用方兼容清单，见 TODO P4）;
本期只实现通用检索语义，不加域特化。工具函数与注册解耦——每个工具都有同名
模块级实现，可在不启服务的情况下直接调用（测试/CLI 复用）。

失败约定（不得静默降级）:
- retriever 未接线（factory 返回 ``None``）→ ``{"error": "retriever_unconfigured"}``;
- factory 构建炸裂 → ``{"error": "retriever_unavailable", "detail": ...}``（留痕）;
- 检索执行异常 → ``{"error": "retrieve_failed", "detail": ...}``（留痕）;
- 应答执行异常 → ``{"error": "answer_failed", "detail": ...}``（留痕）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from matrag.config import settings
from matrag.gateway import corpus_reader

logger = logging.getLogger(__name__)

#: 服务名（ping/对外标识）。
SERVICE_NAME = "matrag"

#: 检索入口工具名（调用方兼容清单见 TODO P4）。
TOOL_SEARCH = "search_context"
#: 应答入口工具名（检索 + 证据合成 + 行级引用 + 推理链）。
TOOL_ASK = "ask"

#: top_k 上限: 防止调用方一次拉全库把检索腿打爆。
MAX_TOP_K = 50

#: index_state 取值: ok（拿到过索引 doc 清单）/ unknown（检索器无 doc 清单能力）/
# unavailable（检索器未接线或不可用）。
INDEX_OK = "ok"
INDEX_UNKNOWN = "unknown"
INDEX_UNAVAILABLE = "unavailable"


def service_version() -> str:
    """安装元数据里的版本；开发态（未安装分发）返回 ``"dev"``。"""
    try:
        return _dist_version(SERVICE_NAME)
    except PackageNotFoundError:  # pragma: no cover - 源码直跑场景
        return "dev"


def ping() -> dict[str, Any]:
    """服务自省: ``{"ok": True, "service": "matrag", "version": ...}``。"""
    return {"ok": True, "service": SERVICE_NAME, "version": service_version()}


def list_documents(
    *,
    retriever_factory: Callable[[], Any] | None = None,
    corpus_dir: Path | None = None,
) -> dict[str, Any]:
    """语料文档清单 + 未入索引清单。

    ``index_state`` 说明 unindexed 的可信度: 检索器不可用 → ``unavailable``
    （unindexed 恒为空，不猜）; 检索器可用但不暴露 doc 清单 → ``unknown``。
    检索器问题不阻塞目录列举（语料是落盘文件，离线可读）。
    """
    base = corpus_dir or settings.corpus_dir
    docs = corpus_reader.list_documents(base)
    payload: dict[str, Any] = {
        "docs": [
            {
                "doc_id": doc.doc_id,
                "title": doc.title,
                "md_present": doc.md_path.is_file(),
                "md_path": str(doc.md_path),
                "tables_path": str(doc.tables_path),
            }
            for doc in docs
        ],
        "unindexed": [],
        "index_state": INDEX_UNAVAILABLE,
    }
    if retriever_factory is None:
        return payload
    retriever, error = _resolve_retriever(retriever_factory)
    if error is not None:
        payload["retriever"] = error
        return payload
    indexed = _indexed_doc_ids(retriever)
    if indexed is None:
        payload["index_state"] = INDEX_UNKNOWN
        return payload
    payload["index_state"] = INDEX_OK
    payload["unindexed"] = [doc.doc_id for doc in docs if doc.doc_id not in indexed]
    return payload


def search_context(
    query: str,
    top_k: int = 10,
    *,
    retriever_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """混合检索: 走 ``HybridRetriever.retrieve(query, top_k=...)``。

    每个 hit 带引用锚点（``doc_id/page/section_path/table_id/chunk_id``），
    供 ATEStudio 端原文回溯; ``explain`` 尽量带执行计划与降级留痕。
    """
    if not isinstance(query, str) or not query.strip():
        return {"error": "empty_query"}
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_TOP_K:
        return {"error": "bad_top_k", "detail": f"top_k 须为 1~{MAX_TOP_K} 的整数: {top_k!r}"}
    if retriever_factory is None:
        return {"error": "retriever_unconfigured"}
    retriever, error = _resolve_retriever(retriever_factory)
    if error is not None:
        return error
    try:
        raw_hits = retriever.retrieve(query, top_k=top_k)
    except Exception as exc:  # noqa: BLE001 —— 汇总为结构化错误并留痕，不静默
        return {
            "error": "retrieve_failed",
            "detail": f"{type(exc).__name__}: {exc}",
        }
    hits = [_hit_payload(hit) for hit in raw_hits or []]
    return {
        "query": query,
        "top_k": top_k,
        "hits": hits,
        "explain": _explain(retriever, query=query, top_k=top_k, n_hits=len(hits)),
    }


def ask(
    query: str,
    top_k: int = 8,
    *,
    retriever_factory: Callable[[], Any] | None = None,
    audit_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """应答合成: 混合检索 → 证据打包 → 带行级引用的中文答案 + 推理链 + 审计留痕。

    走 :func:`matrag.answer.answer_question`（应答层惰性导入，未接线也不影响检索面）。
    返回 ``answer/citations/steps/rule_hints/evaluations/covered/degradations`` 七个键，
    外加 ``audit`` 审计摘要（成功 ``{"ok": True, "answer_id", "backend", "db_path"}`` /
    失败 ``{"ok": False, "error"}``）; 证据不足时 ``covered=false`` 且 ``degradations``
    留痕，**不返回空答案**。

    审计是旁路: 写入失败不阻断已完成的应答，但必须留痕（``degradations`` 追加
    ``provenance_unavailable`` / ``provenance_failed``，可查询）。

    失败形态与 :func:`search_context` 一致: ``empty_query`` / ``bad_top_k`` /
    ``retriever_unconfigured`` / ``retriever_unavailable`` / ``answer_failed``。

    Args:
        audit_factory: 审计轨工厂; 默认按 ``settings.provenance_backend`` 新建
            （sqlite 落盘）; 测试注入 ``AuditTrail("memory")`` 保持离线。
    """
    if not isinstance(query, str) or not query.strip():
        return {"error": "empty_query"}
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_TOP_K:
        return {"error": "bad_top_k", "detail": f"top_k 须为 1~{MAX_TOP_K} 的整数: {top_k!r}"}
    if retriever_factory is None:
        return {"error": "retriever_unconfigured"}
    retriever, error = _resolve_retriever(retriever_factory)
    if error is not None:
        return error
    llm = _resolve_llm()
    started = time.perf_counter()
    try:
        from matrag.answer import answer_question  # 惰性导入: 应答层缺失不影响检索面

        result = answer_question(query, retriever=retriever, llm=llm, top_k=top_k)
    except Exception as exc:  # noqa: BLE001 —— 汇总为结构化错误并留痕，不静默
        logger.warning("answer_question 失败: %s: %s", type(exc).__name__, exc)
        return {"error": "answer_failed", "detail": f"{type(exc).__name__}: {exc}"}
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    # 先写审计再取快照: 审计失败会向 result.degradations 追加留痕,
    # 必须在 to_dict() 拷贝前追加，否则失败留痕会丢失（曾挂 3 个测试）。
    audit = _record_audit(
        query,
        result,
        elapsed_ms=elapsed_ms,
        model=settings.llm_model if llm is not None else "",
        factory=audit_factory,
    )
    payload = result.to_dict()
    payload["audit"] = audit
    return payload


def get_table(
    doc_id: str,
    table_id: str,
    *,
    corpus_dir: Path | None = None,
) -> dict[str, Any]:
    """按 table_id 取表格全文（``TableModel.to_dict()`` 形状），未找到 → ``error``。"""
    table = corpus_reader.get_table(doc_id, table_id, corpus_dir=corpus_dir or settings.corpus_dir)
    if table is None:
        return {"error": "not_found", "doc_id": doc_id, "table_id": table_id}
    return {"table": table}


def doc_outline(
    doc_id: str,
    *,
    corpus_dir: Path | None = None,
) -> dict[str, Any]:
    """文档标题树 + 表格清单（md 缺失/损坏时结果里带 ``error`` 字段）。"""
    return corpus_reader.get_doc_outline(doc_id, corpus_dir=corpus_dir or settings.corpus_dir)


def parse_report(
    doc_id: str,
    *,
    reports_dir: Path | None = None,
) -> dict[str, Any]:
    """解析对账报告（闸门结果）; 报告不存在 → ``error``。"""
    report = corpus_reader.read_report(doc_id, reports_dir=reports_dir or settings.reports_dir)
    if report is None:
        return {"error": "not_found", "doc_id": doc_id}
    return {"report": report}


def register_tools(
    mcp: FastMCP,
    *,
    retriever_factory: Callable[[], Any] | None = None,
    corpus_dir: Path | None = None,
    reports_dir: Path | None = None,
    audit_factory: Callable[[], Any] | None = None,
) -> None:
    """把检索面工具注册到 FastMCP 实例（函数体与模块级实现一一对应）。"""

    @mcp.tool(name="ping", description="服务自省: 返回 ok/service/version")
    def _ping() -> dict[str, Any]:
        return ping()

    @mcp.tool(name="list_documents", description="语料文档清单 + 未入索引文档")
    def _list_documents() -> dict[str, Any]:
        return list_documents(retriever_factory=retriever_factory, corpus_dir=corpus_dir)

    @mcp.tool(
        name=TOOL_SEARCH,
        description="混合检索（vector+keyword 融合），命中带引用锚点",
    )
    def _search_context(query: str, top_k: int = 10) -> dict[str, Any]:
        return search_context(query, top_k, retriever_factory=retriever_factory)

    @mcp.tool(
        name=TOOL_ASK,
        description="检索问答：带行级引用([n])的中文答案 + 推理链步骤 + 证据不足说明 + 审计留痕",
    )
    def _ask(query: str, top_k: int = 8) -> dict[str, Any]:
        return ask(
            query,
            top_k,
            retriever_factory=retriever_factory,
            audit_factory=audit_factory,
        )

    @mcp.tool(name="get_table", description="按 table_id 取表格全文（未找到返回 error）")
    def _get_table(doc_id: str, table_id: str) -> dict[str, Any]:
        return get_table(doc_id, table_id, corpus_dir=corpus_dir)

    @mcp.tool(name="doc_outline", description="文档标题树 + 表格清单")
    def _doc_outline(doc_id: str) -> dict[str, Any]:
        return doc_outline(doc_id, corpus_dir=corpus_dir)

    @mcp.tool(name="parse_report", description="解析对账报告（闸门结果）")
    def _parse_report(doc_id: str) -> dict[str, Any]:
        return parse_report(doc_id, reports_dir=reports_dir)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _resolve_llm() -> Any | None:
    """应答层用的 LLM 门面; 未配置 → ``None``（应答层据此留痕 ``llm_unavailable``）。

    独立成函数是为了测试可替换（离线用例打桩这里，永不触网）。
    """
    try:
        from matrag.llm import get_llm
    except ImportError:  # pragma: no cover - 依赖缺失时按"无 LLM"处理
        return None
    llm = get_llm()
    return llm if getattr(llm, "configured", False) else None


def _record_audit(
    query: str,
    result: Any,
    *,
    elapsed_ms: int,
    model: str,
    factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """把一次应答写入审计轨（旁路；失败只留痕不阻断）。

    与 ``scripts.ask.record_audit`` 同语义: 成功返回
    ``{"ok": True, "answer_id", "backend", "db_path"}``; 失败返回
    ``{"ok": False, "error": ...}`` 且向 ``result.degradations`` 追加
    ``provenance_unavailable`` / ``provenance_failed`` 留痕（可查询）。

    Args:
        query: 用户原始问题。
        result: ``AnswerResult``（读 ``plan`` / ``hits`` / ``citations`` /
            ``rule_hints`` / ``degradations`` / ``covered``）。
        elapsed_ms: 应答耗时（毫秒）。
        model: 合成所用模型名; 确定性路径（无 LLM）传空串。
        factory: 审计轨工厂; ``None`` → 按 ``settings.provenance_backend``
            新建并在写完后关闭; 注入的轨（测试/调用方持有）不代为关闭。
    """
    try:
        from matrag.provenance import AuditTrail, ProvenanceError  # 惰性导入
    except ImportError as exc:  # pragma: no cover - 审计层缺失时留痕不阻断
        _note_audit_failure(result, "provenance_unavailable", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    own = factory is None
    trail = None
    try:
        trail = AuditTrail() if own else factory()
        record = trail.record_answer(
            query=query,
            plan_summary=getattr(result, "plan", None),
            hits=getattr(result, "hits", ()) or (),
            citations=getattr(result, "citations", ()) or (),
            rule_outcomes=getattr(result, "rule_hints", ()) or (),
            degradations=getattr(result, "degradations", ()) or (),
            model=model,
            elapsed_ms=int(elapsed_ms),
            extra={"covered": bool(getattr(result, "covered", False))},
        )
        return {
            "ok": True,
            "answer_id": record.answer_id,
            "backend": trail.backend,
            "db_path": str(trail.db_path) if trail.db_path else None,
        }
    except ProvenanceError as exc:
        _note_audit_failure(result, "provenance_unavailable", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:  # noqa: BLE001 —— 审计失败必须留痕（可查询）
        _note_audit_failure(result, "provenance_failed", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if own and trail is not None:
            trail.close()


def _note_audit_failure(result: Any, kind: str, exc: BaseException) -> None:
    """向 ``result.degradations`` 追加审计失败留痕（列表不存在时忽略）。"""
    degradations = getattr(result, "degradations", None)
    if isinstance(degradations, list):
        degradations.append({
            "kind": kind,
            "detail": f"审计写入失败: {type(exc).__name__}: {exc}",
        })


def _resolve_retriever(factory: Callable[[], Any]) -> tuple[Any | None, dict[str, Any] | None]:
    """调 factory 取检索器。返回 ``(retriever, None)`` 或 ``(None, error_dict)``。"""
    try:
        retriever = factory()
    except Exception as exc:  # noqa: BLE001 —— 构建失败必须留痕（fail-closed 拒绝假检索）
        return None, {"error": "retriever_unavailable", "detail": f"{type(exc).__name__}: {exc}"}
    if retriever is None:
        return None, {"error": "retriever_unconfigured"}
    return retriever, None


def _indexed_doc_ids(retriever: Any) -> set[str] | None:
    """检索器已索引 doc 清单（duck-typed 可选钩子 ``indexed_doc_ids()``）。

    检索器不暴露该钩子 → ``None``（未知，调用方不得反过来断言 unindexed）。
    """
    hook = getattr(retriever, "indexed_doc_ids", None)
    if not callable(hook):
        return None
    try:
        raw = hook()
    except Exception as exc:  # noqa: BLE001 —— 钩子炸裂等同未知，留痕
        logger.warning("indexed_doc_ids() 失败: %s: %s", type(exc).__name__, exc)
        return None
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return {str(k) for k in raw}
    if isinstance(raw, (str, bytes)):
        return {str(raw)}
    try:
        return {str(v) for v in raw}
    except TypeError:
        return None


def _hit_payload(hit: Any) -> dict[str, Any]:
    """把一次命中转成带引用锚点的 JSON 友好 dict（兼容 dataclass/dict 两种命中）。"""
    meta_raw = _field(hit, "meta")
    meta: Mapping[str, Any] = meta_raw if isinstance(meta_raw, Mapping) else {}
    chunk_id = str(_field(hit, "chunk_id") or "")
    doc_id = str(_field(hit, "doc_id") or "")
    return {
        "chunk_id": chunk_id,
        "doc_id": doc_id,
        "score": _as_float(_field(hit, "rrf_score")),
        "legs_hit": [str(leg) for leg in (_field(hit, "legs_hit") or ())],
        "text": str(_field(hit, "text") or ""),
        "anchor": {
            "doc_id": doc_id,
            "page": meta.get("page"),
            "section_path": str(meta.get("section_path") or ""),
            "table_id": meta.get("table_id"),
            "chunk_id": chunk_id,
        },
    }


def _explain(retriever: Any, *, query: str, top_k: int, n_hits: int) -> dict[str, Any]:
    """执行说明: 查询变体/路由/where/腿/降级（检索器不暴露即缺省，不猜）。

    ``last_plan`` 为 dict、``degradations`` 为 list——两种形态都收（检索器属性
    是 property 或普通属性均可，钩子方法亦可）。
    """
    explain: dict[str, Any] = {"query": query, "top_k": top_k, "n_hits": n_hits}
    for name in ("last_plan", "degradations"):
        attr = getattr(retriever, name, None)
        if callable(attr):
            try:
                attr = attr()
            except Exception as exc:  # noqa: BLE001 —— 说明字段缺失不致命，留痕
                logger.warning("explain %s 读取失败: %s: %s", name, type(exc).__name__, exc)
                continue
        if isinstance(attr, Mapping):
            explain[name] = dict(attr)
        elif isinstance(attr, list):
            explain[name] = list(attr)
    return explain


def _field(obj: Any, name: str) -> Any:
    """从命中对象取字段: dict 直接取，否则 getattr（不抛 KeyError/AttributeError）。"""
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _as_float(value: Any) -> float:
    """宽松取浮点: 非数值退化为 0.0（得分字段缺失不致命）。"""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


__all__ = [
    "INDEX_OK",
    "INDEX_UNAVAILABLE",
    "INDEX_UNKNOWN",
    "MAX_TOP_K",
    "SERVICE_NAME",
    "TOOL_ASK",
    "TOOL_SEARCH",
    "ask",
    "doc_outline",
    "get_table",
    "list_documents",
    "parse_report",
    "ping",
    "register_tools",
    "search_context",
    "service_version",
]
