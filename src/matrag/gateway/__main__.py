"""``python -m matrag.gateway`` 入口: 起 streamable-http 服务（部署形态，见 TODO P4）。

行为与 :mod:`matrag.gateway` 文档一致: ``serve_http(create_gateway())``。
未配 API key 且未显式允许匿名 → 启动即 fail-closed 拒绝（server.build_app 闸门）。
"""

from __future__ import annotations

from matrag.gateway import create_gateway, serve_http


def main() -> None:
    """起服务（阻塞; host/port 取 settings.gateway_host/gateway_port）。"""
    serve_http(create_gateway())


if __name__ == "__main__":
    main()
