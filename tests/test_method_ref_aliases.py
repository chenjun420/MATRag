"""``matrag.domain.test_methods`` method_ref_aliases 单测（离线）。

覆盖
----
- :func:`load_method_ref_aliases` 缺源文件 → 返回空 dict + logger.warning (不阻断);
- :func:`load_method_ref_aliases` 解析正常 yaml → 返回 {method_id: (external_ref, ...)};
- :func:`build_reverse_aliases` 正向 M-* -> external_refs 自动推导反向 external_ref -> M-*;
- :func:`resolve_external_refs_for` / :func:`resolve_methods_by_external_ref` 双向解析;
- 双向解析的不变量: ``resolve_external_refs_for`` 结果 × ``resolve_methods_by_external_ref`` 倒查一致;
- 仓库真实源文件 (v4): 新方法 5 条 + 补齐条目存在, 且每个别名键都是真实 method_id。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from matrag.domain.test_methods import (
    build_reverse_aliases,
    load_method_ref_aliases,
    resolve_external_refs_for,
    resolve_method_ref_aliases_path,
    resolve_methods_by_external_ref,
)

# ── 构造器 ───────────────────────────────────────────────────────────────────

_ALIASES_YAML = """\
version: 1

aliases:
  M-RIPPLE-NOISE:
    external_refs:
      - iec62368_ripple
      - en300132_ripple
  M-EFFICIENCY:
    external_refs:
      - iec62368_efficiency
  M-INRUSH:
    external_refs:
      - iec62368_inrush
      - en300132_inrush
      - gb4943_inrush
"""


@pytest.fixture()
def aliases_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """在 tmp_path 写一份标准别名 yaml, 注入环境变量 + 默认路径 (后者覆盖)。"""
    p = tmp_path / "method_ref_aliases.yaml"
    p.write_text(_ALIASES_YAML, encoding="utf-8")
    # 通过环境变量覆盖默认路径, 优先于 DEFAULT_METHOD_REF_ALIASES_PATH
    monkeypatch.setenv("MATRAG_DOMAIN_METHOD_REF_ALIASES_PATH", str(p))
    return p


# ── load_method_ref_aliases ─────────────────────────────────────────────────


def test_load_method_ref_aliases_parses_yaml(aliases_yaml: Path) -> None:
    """正常 yaml 解析: 返回 {M-*: (external_ref, ...)} 映射。"""
    aliases = load_method_ref_aliases()
    assert set(aliases.keys()) == {"M-RIPPLE-NOISE", "M-EFFICIENCY", "M-INRUSH"}
    assert aliases["M-RIPPLE-NOISE"] == ("iec62368_ripple", "en300132_ripple")
    assert aliases["M-EFFICIENCY"] == ("iec62368_efficiency",)
    assert aliases["M-INRUSH"] == ("iec62368_inrush", "en300132_inrush", "gb4943_inrush")


def test_load_method_ref_aliases_missing_file_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源文件缺失: 降级为空 dict + logger.warning (不阻断, 留痕)。"""
    monkeypatch.setenv("MATRAG_DOMAIN_METHOD_REF_ALIASES_PATH", "/nonexistent/aliases.yaml")
    assert load_method_ref_aliases() == {}


def test_resolve_path_priority(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """显式参数 > 环境变量 > 默认。"""
    env_p = tmp_path / "env.yaml"
    env_p.write_text("aliases: {}", encoding="utf-8")
    monkeypatch.setenv("MATRAG_DOMAIN_METHOD_REF_ALIASES_PATH", str(env_p))

    # 显式参数优先于环境变量
    explicit_p = tmp_path / "explicit.yaml"
    explicit_p.write_text("aliases: {}", encoding="utf-8")
    resolved = resolve_method_ref_aliases_path(path=explicit_p)
    assert resolved == explicit_p

    # 缺显式参数时退回环境变量
    resolved_env = resolve_method_ref_aliases_path()
    assert resolved_env == env_p


# ── build_reverse_aliases ────────────────────────────────────────────────────


def test_build_reverse_aliases_correctness(aliases_yaml: Path) -> None:
    """反向推导 external_ref -> M-*。"""
    forward = load_method_ref_aliases()
    reverse = build_reverse_aliases(forward)
    assert reverse["iec62368_ripple"] == ("M-RIPPLE-NOISE",)
    assert reverse["en300132_ripple"] == ("M-RIPPLE-NOISE",)
    # 单一外部 ref -> 单一 M-*
    assert reverse["iec62368_efficiency"] == ("M-EFFICIENCY",)
    # 多个 M-* 不存在此例 (yaml 是 1:多 外部), 反向是多:1 内部


def test_build_reverse_aliases_with_empty() -> None:
    """空 forward: 反向也是空 (边界)。"""
    assert build_reverse_aliases({}) == {}


# ── resolve_* ───────────────────────────────────────────────────────────────


def test_resolve_external_refs_for(aliases_yaml: Path) -> None:
    """M-RIPPLE-NOISE -> 该方法响应的所有外部标准。"""
    refs = resolve_external_refs_for("M-RIPPLE-NOISE")
    assert refs == ("iec62368_ripple", "en300132_ripple")


def test_resolve_external_refs_for_unknown(aliases_yaml: Path) -> None:
    """未知 method_id: 返回空 tuple (降级, 不抛错)。"""
    assert resolve_external_refs_for("M-UNKNOWN-METHOD") == ()


def test_resolve_methods_by_external_ref(aliases_yaml: Path) -> None:
    """反向解析: iec62368_inrush -> M-INRUSH (单匹配)。"""
    methods = resolve_methods_by_external_ref("iec62368_inrush")
    assert methods == ("M-INRUSH",)


def test_resolve_methods_by_external_ref_unknown(aliases_yaml: Path) -> None:
    """未知 external_ref: 空 tuple。"""
    assert resolve_methods_by_external_ref("iec99999_unknown") == ()


# ── 不变量 ──────────────────────────────────────────────────────────────────


def test_bidirectional_invariant(aliases_yaml: Path) -> None:
    """双向不变量: ``forward[M] ⊇ {r | M ∈ reverse[r]}``。

    即: 任意 M-* 在正向映射里的 ref, 通过反向映射再查回去, 该 M 必在结果中。
    """
    forward = load_method_ref_aliases()
    reverse = build_reverse_aliases(forward)
    for method_id, refs in forward.items():
        for ref in refs:
            assert method_id in reverse.get(ref, ()), (
                f"{method_id} 通过 {ref} 反查失败"
            )


# ── 仓库真实源文件 (默认路径, 无 env 覆盖) ──────────────────────────────────

#: v4 必须新增的 5 条 M-* (来自 power_test_methods.yaml 新增方法)。
_V4_NEW_METHODS = {
    "M-STARTUP-TIME",
    "M-OVERSHOOT",
    "M-CROSS-REG",
    "M-OTP",
    "M-BURN-IN",
}

#: v4 补齐的其余"standards 非空"方法 (此前缺条目)。
_V4_GAPFILL_METHODS = {
    "M-SCP",
    "M-GROUND-BOND",
    "M-TOUCH-CURRENT",
    "M-INPUT-SLOW",
    "M-OPP",
    "M-TELEMETRY",
}


def test_repo_aliases_exists_and_includes_v4_new_methods() -> None:
    """仓库默认别名表可解析 (schema: version + aliases dict), 且含 5 条新方法。"""
    forward = load_method_ref_aliases()
    assert forward, "默认 method_ref_aliases.yaml 解析为空 (schema 是否漂移?)"
    assert _V4_NEW_METHODS <= set(forward)


def test_repo_aliases_includes_gapfill_methods() -> None:
    """standards 非空的其余方法均已补齐条目。"""
    forward = load_method_ref_aliases()
    assert _V4_GAPFILL_METHODS <= set(forward)


def test_repo_aliases_keys_are_real_methods() -> None:
    """每个别名键都必须是 power_test_methods.yaml 里真实存在的 method_id。"""
    from matrag.domain.test_methods import load_test_methods

    method_ids = {m.method_id for m in load_test_methods()}
    assert set(load_method_ref_aliases()) <= method_ids


def test_repo_aliases_reverse_lookup_v4_new_refs() -> None:
    """v4 新引入 external_ref id 可被反向解析回对应方法。"""
    reverse = build_reverse_aliases(load_method_ref_aliases())
    assert "M-STARTUP-TIME" in reverse["rohde_schwarz_startup_app_note"]
    assert "M-BURN-IN" in reverse["burn_in_aging_method"]
    assert "M-TELEMETRY" in reverse["pmbus_1_3_spec"]
    assert "M-CROSS-REG" in reverse["iec_61204_performance"]


def test_repo_aliases_unknown_ref_returns_empty() -> None:
    """未知 external_ref 反查 → 空 tuple (降级, 不抛错)。"""
    assert resolve_methods_by_external_ref("iec_99999_nonexistent") == ()

