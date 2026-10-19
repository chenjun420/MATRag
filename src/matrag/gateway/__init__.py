"""MATRag gateway（P1-6）: 对外唯一服务面（MCP over HTTP；REST 面留待后续版本）。

- :mod:`~matrag.gateway.server`        应用装配 + 启动鉴权闸门 + ``serve_http``
- :mod:`~matrag.gateway.tools`         MCP 检索面工具（ping/list_documents/search_context/ask/...）
- :mod:`~matrag.gateway.auth`          ``X-API-Key`` 校验（fail-closed）
- :mod:`~matrag.gateway.corpus_reader` ``data/corpus`` 装配层产物读层

入口::

    from matrag.gateway import create_gateway, serve_http
    serve_http(create_gateway())   # streamable-http（键未配且未允许匿名则拒绝启动）

应答面 ``answer_question``（:mod:`matrag.answer`）在此转出，方便调用方一次 import 拿全
"检索 + 应答"能力；它在服务面里经 ``ask`` 工具暴露（``tools.ask``）。
"""

from matrag.answer import answer_question
from matrag.gateway.auth import (
    ALLOW_ANONYMOUS_ENV,
    API_KEY_HEADER,
    check_api_key,
    is_anonymous_allowed,
    resolve_api_key,
)
from matrag.gateway.corpus_reader import (
    CorpusDoc,
    get_doc_outline,
    get_table,
    list_documents,
    read_report,
)
from matrag.gateway.server import (
    build_app,
    build_http_app,
    create_gateway,
    default_retriever_factory,
    serve_http,
)
from matrag.gateway.tools import ask, register_tools, search_context

__all__ = [
    "ALLOW_ANONYMOUS_ENV",
    "API_KEY_HEADER",
    "CorpusDoc",
    "answer_question",
    "ask",
    "build_app",
    "build_http_app",
    "check_api_key",
    "create_gateway",
    "default_retriever_factory",
    "get_doc_outline",
    "get_table",
    "is_anonymous_allowed",
    "list_documents",
    "read_report",
    "register_tools",
    "resolve_api_key",
    "search_context",
    "serve_http",
]
