"""gateway 鉴权（P1-6）: ``X-API-Key`` 常量时间比对 + 匿名开关（fail-closed）。

- 未配 key 且未显式允许匿名 → 拒绝服务（:func:`matrag.gateway.server.build_app`
  启动即 ``RuntimeError``，绝不起无鉴权服务）。
- 请求 key 不匹配 → 401（由 server.py 的 ASGI 中间件在 HTTP 层执行）。
- ``MATRAG_GATEWAY_ALLOW_ANONYMOUS=true`` 才允许匿名（默认 ``False``）。
"""

from __future__ import annotations

import hashlib
import hmac
import os

#: 网关约定的 API key 请求头（ATEStudio/Agent 侧按此头传 key）。
API_KEY_HEADER = "X-API-Key"

#: 允许匿名的环境变量（只有真值才放行，缺省/任意其它值一律 fail-closed）。
ALLOW_ANONYMOUS_ENV = "MATRAG_GATEWAY_ALLOW_ANONYMOUS"

#: 视为"真"的环境变量取值（大小写不敏感）。
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def check_api_key(candidate: str | None, expected: str | None) -> bool:
    """常量时间比对请求 key 与期望 key。

    任一侧为空/非字符串 → ``False``（fail-closed: 不允许空 key 命中空配置）。
    """
    if not expected or not candidate:
        return False
    if not isinstance(expected, str) or not isinstance(candidate, str):
        return False
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def is_anonymous_allowed(env: dict | None = None) -> bool:
    """环境是否显式允许匿名访问（默认 ``False``，fail-closed）。

    Args:
        env: 环境字典；``None`` 时取 ``os.environ``（测试可注入替身）。
    """
    source = os.environ if env is None else env
    raw = source.get(ALLOW_ANONYMOUS_ENV)
    if raw is None:
        return False
    return str(raw).strip().lower() in _TRUE_VALUES


def resolve_api_key(explicit: str | None = None, env: dict | None = None) -> str:
    """Gateway 生效 key: 显式参数优先，缺省回落 ``MATRAG_GATEWAY_API_KEY``。

    统一 strip 去空白; 空串等同未配置（空 key 永不生效）。
    """
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    source = os.environ if env is None else env
    raw = source.get("MATRAG_GATEWAY_API_KEY")
    if raw is None:
        return ""
    return str(raw).strip()


__all__ = [
    "ALLOW_ANONYMOUS_ENV",
    "API_KEY_HEADER",
    "check_api_key",
    "is_anonymous_allowed",
    "resolve_api_key",
]
