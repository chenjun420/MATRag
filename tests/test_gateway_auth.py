"""P1-6 auth 测试: X-API-Key 比对 + 匿名开关矩阵（fail-closed 语义）。"""

from __future__ import annotations

import pytest

from matrag.gateway import auth

KEY = "sk-test-1234"


# --------------------------------------------------------------------------- #
# check_api_key
# --------------------------------------------------------------------------- #
def test_check_api_key_match() -> None:
    """key 一致 → True。"""
    assert auth.check_api_key(KEY, KEY) is True


def test_check_api_key_mismatch() -> None:
    """key 不一致 → False（含前缀相同、仅尾部不同的近似值）。"""
    assert auth.check_api_key("sk-test-1235", KEY) is False
    assert auth.check_api_key("sk-test", KEY) is False


def test_check_api_key_empty_or_none() -> None:
    """任一侧为空/None → False（fail-closed: 空 key 不得命中空配置）。"""
    assert auth.check_api_key("", KEY) is False
    assert auth.check_api_key(KEY, "") is False
    assert auth.check_api_key(None, KEY) is False
    assert auth.check_api_key(KEY, None) is False
    assert auth.check_api_key(None, None) is False


def test_check_api_key_non_ascii_roundtrip() -> None:
    """非 ASCII key 也能正确比对（内部走 sha256 摘要，不触发 TypeError）。"""
    cn = "密钥-测试值"
    assert auth.check_api_key(cn, cn) is True
    assert auth.check_api_key(cn + "x", cn) is False


# --------------------------------------------------------------------------- #
# is_anonymous_allowed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", ["true", "TRUE", "True", "1", "yes", "on", " on "])
def test_anonymous_true_values(raw: str) -> None:
    """真值集合（大小写/空白不敏感）才放行匿名。"""
    assert auth.is_anonymous_allowed({"MATRAG_GATEWAY_ALLOW_ANONYMOUS": raw}) is True


@pytest.mark.parametrize("raw", ["", "false", "0", "no", "off", "allow", "maybe"])
def test_anonymous_false_values(raw: str) -> None:
    """真值集合以外的任何取值一律 fail-closed（默认禁止匿名）。"""
    assert auth.is_anonymous_allowed({"MATRAG_GATEWAY_ALLOW_ANONYMOUS": raw}) is False


def test_anonymous_missing_env() -> None:
    """环境变量缺失 / 空环境 → False。"""
    assert auth.is_anonymous_allowed({}) is False
    assert auth.is_anonymous_allowed({"OTHER": "true"}) is False


def test_anonymous_env_none_reads_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """env=None 时读 os.environ（集成行为: 设置/删除都生效）。"""
    monkeypatch.setenv("MATRAG_GATEWAY_ALLOW_ANONYMOUS", "true")
    assert auth.is_anonymous_allowed() is True
    monkeypatch.delenv("MATRAG_GATEWAY_ALLOW_ANONYMOUS")
    assert auth.is_anonymous_allowed() is False


# --------------------------------------------------------------------------- #
# resolve_api_key
# --------------------------------------------------------------------------- #
def test_resolve_api_key_explicit_wins() -> None:
    """显式参数优先于环境变量。"""
    env = {"MATRAG_GATEWAY_API_KEY": "from-env"}
    assert auth.resolve_api_key("from-arg", env=env) == "from-arg"


def test_resolve_api_key_env_fallback() -> None:
    """未显式给 → 回落环境变量（strip 去空白）。"""
    env = {"MATRAG_GATEWAY_API_KEY": "  from-env  "}
    assert auth.resolve_api_key(env=env) == "from-env"
    assert auth.resolve_api_key(None, env=env) == "from-env"


def test_resolve_api_key_empty_is_unset() -> None:
    """空串/空白显式值等同未配置（空 key 永不生效）。"""
    env = {"MATRAG_GATEWAY_API_KEY": "from-env"}
    assert auth.resolve_api_key("   ", env=env) == "from-env"
    assert auth.resolve_api_key("", env=env) == "from-env"
    assert auth.resolve_api_key(env={}) == ""
