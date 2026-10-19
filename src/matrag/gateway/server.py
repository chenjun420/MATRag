"""gateway 服务装配（P1-6）: MCP over streamable-http + 启动鉴权闸门。

- **transport 只有 http（streamable-http）**: 用户侧经 ATEStudio/Agent 访问;
  stdio 仅开发调试，本服务不提供（部署形态见 TODO P4-3）。
- **启动 fail-closed**: 未提供/未配 API key 且未显式允许匿名 → ``RuntimeError``，
  绝不起无鉴权服务（:func:`build_app` 首行即闸门）。
- **请求级 401**: key 不匹配由 :func:`build_http_app` 挂的 ASGI 中间件在 HTTP 层
  拦截，不到工具层; 匿名模式（显式开启）不挂中间件。

FastMCP HTTP 挂载方式（mcp 1.30 API 原文）::

    def streamable_http_app(self) -> Starlette:
        \"\"\"Return an instance of the StreamableHTTP server app.\"\"\"

即 ``app.streamable_http_app()`` 返回自带 lifespan（``session_manager.run()``）的
Starlette ASGI app；``serve_http`` 在其外套 ``X-API-Key`` 中间件后交给 uvicorn。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.types import ASGIApp, Receive, Scope, Send

from matrag.config import settings
from matrag.gateway import auth, tools

logger = logging.getLogger(__name__)

#: build_app 把生效密钥挂到 FastMCP 实例的属性名（serve_http/build_http_app 取用）。
API_KEY_ATTR = "matrag_api_key"

#: 默认检索装配用的索引库文件名: 与 ``scripts/build_index.py`` 默认落盘一致
#: （``settings.store_dir/index.db``，关键词/向量双腿同库；embedding 缓存同名
#: ``.embcache``）。库文件不存在 → 检索器未接线（search 报 retriever_unconfigured）。
INDEX_DB_NAME = "index.db"


def build_app(
    *,
    retriever_factory: Any | None = None,
    corpus_dir: Path | None = None,
    api_key: str | None = None,
    allow_anonymous: bool = False,
) -> FastMCP:
    """装配 gateway 应用（鉴权闸门 + 工具注册）。

    Args:
        retriever_factory: 返回 ``HybridRetriever`` 的工厂; ``None`` 时不注册检索
            能力（search 报 ``retriever_unconfigured``）。
        corpus_dir: 语料目录；缺省 ``settings.corpus_dir``。
        api_key: 显式 API key; 缺省回落 ``MATRAG_GATEWAY_API_KEY``。
        allow_anonymous: ``False``（默认）= 未显式指定，回落环境变量
            ``MATRAG_GATEWAY_ALLOW_ANONYMOUS``; ``True`` = 显式放行匿名。

    Raises:
        RuntimeError: 无生效 key 且未允许匿名（fail-closed 拒绝启动）。
    """
    expected_key = (auth.resolve_api_key(api_key) or str(settings.gateway_api_key or "")).strip()
    anonymous = bool(allow_anonymous) or auth.is_anonymous_allowed(os.environ)
    if not expected_key and not anonymous:
        raise RuntimeError(
            "gateway 拒绝启动（fail-closed）: 未配置 API key "
            "(MATRAG_GATEWAY_API_KEY 或 api_key 参数) 且未允许匿名。"
            "请配置 key，或显式设置 MATRAG_GATEWAY_ALLOW_ANONYMOUS=true。"
        )
    app = FastMCP(
        name=tools.SERVICE_NAME,
        instructions=(
            "MATRag 检索面: search_context 混合检索（带引用锚点）+ list_documents/"
            "doc_outline/get_table/parse_report 语料查询。只读，不做写入。"
        ),
        host=settings.gateway_host,
        port=settings.gateway_port,
    )
    tools.register_tools(app, retriever_factory=retriever_factory, corpus_dir=corpus_dir)
    # 生效密钥挂到实例: serve_http/build_http_app 据此决定是否挂 401 中间件
    setattr(app, API_KEY_ATTR, expected_key or None)
    return app


def create_gateway(
    *,
    api_key: str | None = None,
    allow_anonymous: bool | None = None,
) -> FastMCP:
    """从 settings 装配默认 gateway（检索面: 语料直读 + 默认检索器工厂）。

    ``allow_anonymous=None``（默认）= 未指定，回落 ``MATRAG_GATEWAY_ALLOW_ANONYMOUS``。
    """
    return build_app(
        retriever_factory=default_retriever_factory,
        corpus_dir=settings.corpus_dir,
        api_key=api_key,
        allow_anonymous=bool(allow_anonymous),
    )


def default_retriever_factory() -> Any | None:
    """默认检索器工厂: 索引库存在才装配 ``HybridRetriever``，否则 ``None``（未接线）。

    索引库缺失 = CLI 尚未 ingest（未接线），属预期形态而非错误; 已存在但装配失败
    （维度探测失败/库损坏等）不静默吞——异常上抛，由 search 工具转成
    ``retriever_unavailable`` 结构化错误（带 detail 留痕）。**图谱腿例外**:
    装配失败只打 warning 并按"未接线"继续（两腿照常, 检索层对公式类查询留
    ``leg_not_wired`` note）。
    """
    db_path = _index_db_path()
    if not db_path.is_file():
        logger.info("检索器未接线: 索引库不存在 %s", db_path)
        return None
    # 延迟导入: 离线/未接线态不需要把索引层与检索层拖进 import 期
    from matrag.embeddings import get_embedder
    from matrag.index import KeywordIndex, VectorIndex
    from matrag.retrieve import HybridRetriever

    embedder = get_embedder()
    dimension = embedder.dim  # 未配置/探测失败 → fail-closed RuntimeError
    vector_index = VectorIndex(db_path, dimension=dimension, embed_model=embedder.model)
    keyword_index = KeywordIndex(db_path)
    graph_index = default_graph_index(keyword_index)
    return HybridRetriever(
        vector_index=vector_index,
        keyword_index=keyword_index,
        embedder=embedder,
        graph_index=graph_index,
    )


def default_graph_index(keyword_index: Any) -> Any | None:
    """装配图谱腿（KG 接线）; 失败不阻断（两腿照常）, 但必须显式告警。

    未接线时 FORMULA 类查询会带 ``leg_not_wired`` 降级记录（检索层留痕）。
    """
    try:
        from matrag.retrieve.graph_index import build_graph_index

        return build_graph_index(keyword_index.chunks_by_doc)
    except Exception as exc:  # noqa: BLE001 —— 图谱腿是增强项, 缺失不阻断检索
        logger.warning(
            "图谱腿装配失败（按未接线处理）: %s: %s", type(exc).__name__, exc
        )
        return None


def build_http_app(app: FastMCP, *, api_key: str | None = None) -> Any:
    """FastMCP → 带鉴权的 streamable-http ASGI app（供 serve_http 与 HTTP 测试用）。

    ``api_key=None`` 时取实例上挂的生效密钥（:func:`build_app` 写入）; 仍为空
    返回裸 app（匿名模式，授权判断已由 build_app 闸门完成）。
    """
    key = api_key if api_key is not None else getattr(app, API_KEY_ATTR, None)
    http_app = app.streamable_http_app()
    if not key:
        return http_app
    return _ApiKeyMiddleware(http_app, expected_key=str(key))


def serve_http(app: FastMCP, *, host: str | None = None, port: int | None = None) -> None:
    """以 streamable-http 起服务（阻塞; 只支持 http transport）。

    host/port 缺省取 ``settings.gateway_host`` / ``settings.gateway_port``。
    """
    import uvicorn  # 延迟导入: import 期不依赖服务端依赖（测试/纯工具调用无感）

    http_app = build_http_app(app)
    uvicorn.run(
        http_app,
        host=host or settings.gateway_host,
        port=port or settings.gateway_port,
    )


def _index_db_path() -> Path:
    """索引库路径: ``settings.store_dir/<INDEX_DB_NAME>``。"""
    return Path(settings.store_dir) / INDEX_DB_NAME


class _ApiKeyMiddleware:
    """X-API-Key 校验 ASGI 中间件: 不匹配 → 401 JSON（不等同静默放行）。

    以**整个 app** 为粒度拦截（含 ``/mcp`` 端点），挂在 mcp 自带中间件之外，
    任一层漏掉也不影响 fail-closed 语义。非 HTTP scope（lifespan/websocket）原样放行。
    """

    def __init__(self, app: ASGIApp, *, expected_key: str) -> None:
        self._app = app
        self._expected_key = expected_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        candidate = _header(scope, auth.API_KEY_HEADER)
        if not auth.check_api_key(candidate, self._expected_key):
            response = _unauthorized()
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def _header(scope: Scope, name: str) -> str | None:
    """从 ASGI scope 取请求头（大小写不敏感; 取不到返回 None）。"""
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers") or []:
        if key == wanted:
            return value.decode("latin-1")
    return None


def _unauthorized() -> Any:
    """401 JSON 响应（延迟 import starlette，保持模块 import 轻量）。"""
    from starlette.responses import JSONResponse

    return JSONResponse(
        {"error": "unauthorized", "detail": f"missing or invalid {auth.API_KEY_HEADER} header"},
        status_code=401,
        headers={"WWW-Authenticate": auth.API_KEY_HEADER},
    )


__all__ = [
    "API_KEY_ATTR",
    "INDEX_DB_NAME",
    "build_app",
    "build_http_app",
    "create_gateway",
    "default_graph_index",
    "default_retriever_factory",
    "serve_http",
]
