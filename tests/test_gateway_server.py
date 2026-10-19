"""P1-6 server 测试: 启动鉴权闸门（fail-closed）+ HTTP 401/200 行为。

不起真实端口: HTTP 路径用 ``starlette.testclient.TestClient`` 直打 ASGI app
（lifespan 由 TestClient 管理），MCP 会话手工按 streamable-http 协议驱动。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from matrag.config import settings
from matrag.gateway import server as gw_server

KEY = "sk-gateway-test"
ALLOW_ENV = "MATRAG_GATEWAY_ALLOW_ANONYMOUS"
KEY_ENV = "MATRAG_GATEWAY_API_KEY"

#: mcp 1.30 streamable-http 握手所需请求头（DNS rebinding 保护要求 Host 落在
#: FastMCP 自动放行的 127.0.0.1:* 内，否则 421）。
HOST_HEADER = {"Host": "127.0.0.1:8321"}
JSON_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    **HOST_HEADER,
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉真实环境里的网关变量，避免 .env / 外部环境渗入测试。"""
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.setattr(settings, "gateway_api_key", "")
    monkeypatch.setattr(settings, "gateway_host", "127.0.0.1")
    monkeypatch.setattr(settings, "gateway_port", 8321)


def _sse_messages(text: str) -> list[dict[str, Any]]:
    """解析 streamable-http 的 SSE 响应体 → JSON-RPC 消息列表。"""
    out: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                out.append(json.loads(line[len("data:"):].strip()))
    return out


def _establish_session(client: TestClient, key: str | None = None) -> dict[str, str]:
    """initialize + notifications/initialized，返回带 session id 的请求头。"""
    headers = {**JSON_HEADERS}
    if key:
        headers["X-API-Key"] = key
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "gateway-test", "version": "1"},
            },
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    session_id = resp.headers.get("mcp-session-id")
    assert session_id
    headers["mcp-session-id"] = session_id
    init_notice = client.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers
    )
    assert init_notice.status_code == 202
    return headers


def _call_tool(client: TestClient, headers: dict[str, str], name: str,
               arguments: dict[str, Any]) -> dict[str, Any]:
    """tools/call → structuredContent（无 structured 时回退解析 text）。"""
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    messages = _sse_messages(resp.text)
    result = messages[-1]["result"]
    if result.get("structuredContent") is not None:
        return dict(result["structuredContent"])
    return json.loads(result["content"][0]["text"])


# --------------------------------------------------------------------------- #
# 启动闸门
# --------------------------------------------------------------------------- #
def test_build_app_rejects_missing_key(_clean_env: None) -> None:
    """未配 key 且未允许匿名 → RuntimeError（fail-closed 拒绝启动）。"""
    with pytest.raises(RuntimeError, match="fail-closed"):
        gw_server.build_app()


def test_build_app_rejects_with_unrelated_env(_clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量只有无关真值 → 仍拒绝（真值集合严格匹配）。"""
    monkeypatch.setenv(ALLOW_ENV, "maybe")
    monkeypatch.setenv(KEY_ENV, "")
    with pytest.raises(RuntimeError):
        gw_server.build_app()


def test_build_app_anonymous_via_env(_clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """MATRAG_GATEWAY_ALLOW_ANONYMOUS=true + 无 key → 允许启动（无生效 key）。"""
    monkeypatch.setenv(ALLOW_ENV, "true")
    app = gw_server.build_app()
    assert getattr(app, gw_server.API_KEY_ATTR) is None


def test_build_app_anonymous_via_flag(_clean_env: None) -> None:
    """allow_anonymous=True 显式放行（无需环境变量）。"""
    app = gw_server.build_app(allow_anonymous=True)
    assert getattr(app, gw_server.API_KEY_ATTR) is None


def test_build_app_with_explicit_key(_clean_env: None) -> None:
    """显式 key → 挂到实例（serve_http 据此决定 401 中间件）。"""
    app = gw_server.build_app(api_key=KEY)
    assert getattr(app, gw_server.API_KEY_ATTR) == KEY


def test_build_app_key_from_env(_clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量提供 key → 同样生效（不要求显式参数）。"""
    monkeypatch.setenv(KEY_ENV, KEY)
    app = gw_server.build_app()
    assert getattr(app, gw_server.API_KEY_ATTR) == KEY


def test_create_gateway_reads_settings(_clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """create_gateway 从 settings 取 key/路径; settings 无 key 且未放匿名 → 拒绝。"""
    monkeypatch.setattr(settings, "gateway_api_key", KEY)
    app = gw_server.create_gateway()
    assert app.name == "matrag"
    assert [t.name for t in app._tool_manager.list_tools()] == [  # noqa: SLF001 —— 断言注册面
        "ping", "list_documents", "search_context", "ask", "get_table", "doc_outline", "parse_report",
    ]
    monkeypatch.setattr(settings, "gateway_api_key", "")
    with pytest.raises(RuntimeError):
        gw_server.create_gateway()


# --------------------------------------------------------------------------- #
# default_retriever_factory（未接线形态）
# --------------------------------------------------------------------------- #
def test_default_factory_missing_db(_clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """索引库不存在 → None（未接线; search 会报 retriever_unconfigured）。"""
    monkeypatch.setattr(settings, "store_dir", tmp_path / "store")
    assert gw_server.default_retriever_factory() is None


# --------------------------------------------------------------------------- #
# HTTP 行为（TestClient 直打 ASGI，不起端口）
# --------------------------------------------------------------------------- #
def test_http_401_without_key(_clean_env: None) -> None:
    """无 key 请求 → 401（不到工具层）。"""
    app = gw_server.build_app(api_key=KEY)
    with TestClient(gw_server.build_http_app(app)) as client:
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                         "params": {}}, headers=JSON_HEADERS)
        assert resp.status_code == 401
        assert resp.json()["error"] == "unauthorized"


def test_http_401_with_wrong_key(_clean_env: None) -> None:
    """key 不匹配 → 401（近似值也不放行）。"""
    app = gw_server.build_app(api_key=KEY)
    with TestClient(gw_server.build_http_app(app)) as client:
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                         "params": {}}, headers={**JSON_HEADERS, "X-API-Key": "sk-wrong"})
        assert resp.status_code == 401


def test_http_200_with_correct_key(_clean_env: None, tmp_path: Path) -> None:
    """key 匹配 → 完整 MCP 会话: initialize → tools/call ping 返回服务自省。"""
    app = gw_server.build_app(api_key=KEY, corpus_dir=tmp_path)
    with TestClient(gw_server.build_http_app(app)) as client:
        headers = _establish_session(client, key=KEY)
        assert headers["X-API-Key"] == KEY
        ping = _call_tool(client, headers, "ping", {})
        assert ping["ok"] is True
        assert ping["service"] == "matrag"
        assert ping["version"]


def test_http_anonymous_mode_no_key_needed(_clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """匿名模式（显式放行 + 无 key）: 不带头也能建会话调工具。"""
    monkeypatch.setenv(ALLOW_ENV, "true")
    app = gw_server.build_app(corpus_dir=tmp_path)
    with TestClient(gw_server.build_http_app(app)) as client:
        headers = _establish_session(client)
        ping = _call_tool(client, headers, "ping", {})
        assert ping["service"] == "matrag"


def test_http_search_reports_unconfigured_retriever(_clean_env: None, tmp_path: Path) -> None:
    """search_context 在检索器未接线时: HTTP 层 200 + 结构化 retriever_unconfigured。"""
    app = gw_server.build_app(
        api_key=KEY, corpus_dir=tmp_path, retriever_factory=lambda: None
    )
    with TestClient(gw_server.build_http_app(app)) as client:
        headers = _establish_session(client, key=KEY)
        payload = _call_tool(client, headers, "search_context", {"query": "过压保护"})
        assert payload == {"error": "retriever_unconfigured"}


def test_serve_http_uses_wrapped_app(_clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """serve_http 不真起端口: 只验证它把（带鉴权的）http app 交给 uvicorn.run。"""
    captured: dict[str, Any] = {}

    def fake_run(app: Any, *, host: str, port: int) -> None:
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)
    app = gw_server.build_app(api_key=KEY)
    gw_server.serve_http(app)
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8321
    assert isinstance(captured["app"], gw_server._ApiKeyMiddleware)  # noqa: SLF001
