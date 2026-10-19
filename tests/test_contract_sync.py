"""``tools/contract_sync`` 的离线单元测试。

全部用例离线: 四个数据源用临时文件或 monkeypatch 构造, 不发起网络/LLM/subprocess
真实调用 (subprocess 仅在被测代码路径内被 monkeypatch 替换)。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.contract_sync import check_contract_hash as cch  # noqa: E402
from tools.contract_sync import check_vocab as cv  # noqa: E402

# ── 构造器 ──────────────────────────────────────────────────────────────────


def _binding(
    kind: str,
    side: str,
    action: str = "act",
    method_ref: str = "",
    unit: str = "",
) -> cv.Binding:
    return cv.Binding(kind=kind, side=side, action=action, method_ref=method_ref, unit=unit)


def _write_matrag_tree(root: Path, methods_yaml: str) -> None:
    path = root / cv.METHODS_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(methods_yaml, encoding="utf-8")


def _write_ate_tree(root: Path, *, bindings_yaml: str, driver_py: str) -> None:
    bindings_path = root / cv.BINDINGS_RELPATH
    bindings_path.parent.mkdir(parents=True, exist_ok=True)
    bindings_path.write_text(bindings_yaml, encoding="utf-8")
    driver_path = root / cv.DRIVER_TABLE_RELPATH
    driver_path.parent.mkdir(parents=True, exist_ok=True)
    driver_path.write_text(driver_py, encoding="utf-8")


_METHODS_YAML = """\
- method_id: M-A
  zh: 甲
- method_id: M-B
  zh: 乙
"""

_BINDINGS_YAML = """\
version: 1
bindings:
  - kind: input_voltage
    side: input
    action: set_ac_input
    method_ref: M-A
    unit: Vac
  - kind: output_voltage
    side: output
    action: measure_dc_voltage
    method_ref: M-B
    unit: V
"""

_DRIVER_PY = """\
DRIVER_TABLE: dict[str, tuple[str, str, str]] = {
    "set_ac_input": ("ac_source", "set_voltage", "set"),
    "measure_dc_voltage": ("dmm", "measure_dc_voltage", "measure"),
}
"""

_ALIGNED_KIND_UNITS = {
    ("input_voltage", "input"): "Vac",
    ("output_voltage", "output"): "V",
}


# ── run_checks: 成功路径 ─────────────────────────────────────────────────────


def test_run_checks_all_aligned() -> None:
    results = cv.run_checks(
        kind_units=_ALIGNED_KIND_UNITS,
        bindings=[
            _binding("input_voltage", "input", "set_ac_input", "M-A", "Vac"),
            _binding("output_voltage", "output", "measure_dc_voltage", "M-B", "V"),
        ],
        method_ids={"M-A", "M-B"},
        driver_actions={"set_ac_input", "measure_dc_voltage"},
    )
    assert len(results) == 5  # check1/check2/check3a/check3b/check4
    # 硬门禁 (#1/#3a) 全部通过且无差集
    hard = [r for r in results if r.severity == cv.SEVERITY_FAIL]
    assert len(hard) == 2
    assert all(r.ok for r in hard)
    assert all(not r.missing for r in hard)
    # check3a 已无 extra 字段 (extra 已剥离到 check3b WARN)
    assert all(not r.extra for r in hard)
    # 软警告: #2 命名空间无差异 → 通过; #3b driver 完全对得上 → 通过
    warns = [r for r in results if r.severity == cv.SEVERITY_WARN]
    assert all(r.ok for r in warns)
    assert results[1].severity == cv.SEVERITY_WARN
    assert results[1].ok
    # 信息项 (#4) 存在 (M-* 会命中可疑命名)
    assert results[4].severity == cv.SEVERITY_INFO


# ── run_checks: 失败路径 (逐项) ─────────────────────────────────────────────


def test_run_checks_kind_missing_in_bindings() -> None:
    results = cv.run_checks(
        kind_units={**_ALIGNED_KIND_UNITS, ("ripple", "output"): "mVpp"},
        bindings=[
            _binding("input_voltage", "input", "set_ac_input", "M-A", "Vac"),
            _binding("output_voltage", "output", "measure_dc_voltage", "M-B", "V"),
        ],
        method_ids={"M-A", "M-B"},
        driver_actions={"set_ac_input", "measure_dc_voltage"},
    )
    check1 = results[0]
    assert not check1.ok
    assert "ripple:output" in check1.missing
    assert not check1.extra


def test_run_checks_kind_extra_in_bindings() -> None:
    results = cv.run_checks(
        kind_units=_ALIGNED_KIND_UNITS,
        bindings=[
            _binding("input_voltage", "input", "set_ac_input", "M-A", "Vac"),
            _binding("output_voltage", "output", "measure_dc_voltage", "M-B", "V"),
            _binding("timing", "output", "measure_transient_response", "M-C", "ms"),
        ],
        method_ids={"M-A", "M-B", "M-C"},
        driver_actions={"set_ac_input", "measure_dc_voltage", "measure_transient_response"},
    )
    check1 = results[0]
    assert not check1.ok
    assert "timing:output" in check1.extra
    assert not check1.missing


def test_run_checks_namespace_overlap_is_warn() -> None:
    """#2 降级为软警告: 内部 M-* 与外部 method_ref 命名空间不相交不阻断。"""
    results = cv.run_checks(
        kind_units=_ALIGNED_KIND_UNITS,
        bindings=[
            _binding("input_voltage", "input", "set_ac_input", "M-A", "Vac"),
            _binding("output_voltage", "output", "measure_dc_voltage", "M-B", "V"),
        ],
        method_ids={"M-A", "M-B", "M-ORPHAN"},
        driver_actions={"set_ac_input", "measure_dc_voltage"},
    )
    check2 = results[1]
    assert check2.severity == cv.SEVERITY_WARN
    assert not check2.ok
    assert check2.missing == ("M-ORPHAN",)
    assert check2.notes  # 命名空间设计说明
    # 软警告不计入退出码: 无 FAIL 级别失败
    assert not any(r.severity == cv.SEVERITY_FAIL and not r.ok for r in results)


def test_run_checks_driver_table_unused_entries_are_warn() -> None:
    """#3a 硬门禁: 绑定表引用的 action 都在 DRIVER_TABLE → 通过 (无 missing);
    #3b 软警告: DRIVER_TABLE 中多出的 action 在 check3b 出现 (WARN, 不阻断)。"""
    results = cv.run_checks(
        kind_units=_ALIGNED_KIND_UNITS,
        bindings=[
            _binding("input_voltage", "input", "set_ac_input", "M-A", "Vac"),
            _binding("output_voltage", "output", "measure_dc_voltage", "M-B", "V"),
        ],
        method_ids={"M-A", "M-B"},
        driver_actions={"set_ac_input", "measure_dc_voltage", "power_cycle", "set_capacitive_load"},
    )
    check3a = results[2]
    assert check3a.severity == cv.SEVERITY_FAIL
    assert check3a.ok
    assert check3a.missing == ()  # 绑定表引用的 action 都齐全
    check3b = results[3]
    assert check3b.severity == cv.SEVERITY_WARN
    assert not check3b.ok  # 多出来的 action 触发警告
    assert check3b.extra == ("power_cycle", "set_capacitive_load")


def test_run_checks_method_ref_naming_convention_flags_suspicious() -> None:
    """#4 信息项: 不符合外部标准命名约定的 method_ref 被列为可疑。"""
    results = cv.run_checks(
        kind_units=_ALIGNED_KIND_UNITS,
        bindings=[
            _binding("input_voltage", "input", "set_ac_input", "iec62368_protection", "Vac"),
            _binding("output_voltage", "output", "measure_dc_voltage", "weirdRef", "V"),
        ],
        method_ids={"M-A", "M-B"},
        driver_actions={"set_ac_input", "measure_dc_voltage"},
    )
    check4 = results[4]  # results: check1=0, check2=1, check3a=2, check3b=3, check4=4
    assert check4.severity == cv.SEVERITY_INFO
    assert not check4.ok
    assert check4.missing == ("weirdRef",)


def test_run_checks_method_ref_convention_clean() -> None:
    """#4 信息项: 全部符合外部标准命名约定时无可疑项。"""
    results = cv.run_checks(
        kind_units=_ALIGNED_KIND_UNITS,
        bindings=[
            _binding("input_voltage", "input", "set_ac_input", "en300132_ripple", "Vac"),
            _binding("output_voltage", "output", "measure_dc_voltage", "iec61000_4_11_surge", "V"),
        ],
        method_ids={"M-A", "M-B"},
        driver_actions={"set_ac_input", "measure_dc_voltage"},
    )
    check4 = results[3]
    assert check4.ok
    assert not check4.missing


def test_run_checks_binding_action_not_in_driver_table() -> None:
    results = cv.run_checks(
        kind_units=_ALIGNED_KIND_UNITS,
        bindings=[
            _binding("input_voltage", "input", "set_ac_input", "M-A", "Vac"),
            _binding("output_voltage", "output", "no_such_action", "M-B", "V"),
        ],
        method_ids={"M-A", "M-B"},
        driver_actions={"set_ac_input"},
    )
    check3 = results[2]
    assert not check3.ok
    assert check3.missing == ("no_such_action",)


# ── 读取器: 成功与失败路径 ──────────────────────────────────────────────────


def test_load_bindings_ok(tmp_path: Path) -> None:
    path = tmp_path / "bindings.yaml"
    path.write_text(_BINDINGS_YAML, encoding="utf-8")
    bindings = cv.load_bindings(path)
    assert len(bindings) == 2
    assert bindings[0] == _binding("input_voltage", "input", "set_ac_input", "M-A", "Vac")
    assert bindings[1].method_ref == "M-B"


def test_load_bindings_missing_bindings_key_raises(tmp_path: Path) -> None:
    path = tmp_path / "bindings.yaml"
    path.write_text("version: 1\n", encoding="utf-8")
    with pytest.raises(cv.ContractSyncError, match="bindings"):
        cv.load_bindings(path)


def test_load_bindings_missing_kind_raises(tmp_path: Path) -> None:
    path = tmp_path / "bindings.yaml"
    path.write_text("bindings:\n  - side: input\n    action: x\n", encoding="utf-8")
    with pytest.raises(cv.ContractSyncError, match="kind"):
        cv.load_bindings(path)


def test_load_bindings_not_a_list_raises(tmp_path: Path) -> None:
    path = tmp_path / "bindings.yaml"
    path.write_text("bindings:\n  kind: input_voltage\n", encoding="utf-8")
    with pytest.raises(cv.ContractSyncError, match="列表"):
        cv.load_bindings(path)


def test_load_bindings_bad_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "bindings.yaml"
    path.write_text("bindings: [unclosed\n", encoding="utf-8")
    with pytest.raises(cv.ContractSyncError, match="YAML"):
        cv.load_bindings(path)


def test_load_method_ids_ok(tmp_path: Path) -> None:
    path = tmp_path / "methods.yaml"
    path.write_text(_METHODS_YAML, encoding="utf-8")
    assert cv.load_method_ids(path) == {"M-A", "M-B"}


def test_load_method_ids_missing_id_raises(tmp_path: Path) -> None:
    path = tmp_path / "methods.yaml"
    path.write_text("- zh: 甲\n", encoding="utf-8")
    with pytest.raises(cv.ContractSyncError, match="method_id"):
        cv.load_method_ids(path)


def test_load_driver_table_ast_ok(tmp_path: Path) -> None:
    path = tmp_path / "emit.py"
    path.write_text(_DRIVER_PY, encoding="utf-8")
    assert cv.load_driver_table(path) == {"set_ac_input", "measure_dc_voltage"}


def test_load_driver_table_no_assignment_raises(tmp_path: Path) -> None:
    path = tmp_path / "emit.py"
    path.write_text("OTHER = {}\n", encoding="utf-8")
    with pytest.raises(cv.ContractSyncError, match="DRIVER_TABLE"):
        cv.load_driver_table(path)


def test_load_driver_table_syntax_error_raises(tmp_path: Path) -> None:
    path = tmp_path / "emit.py"
    path.write_text("DRIVER_TABLE = {\n", encoding="utf-8")
    with pytest.raises(cv.ContractSyncError, match="语法错误"):
        cv.load_driver_table(path)


# ── build_inputs: 成功/失败 (mock 全部四个数据源) ────────────────────────────


def test_build_inputs_offline_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    matrag_root = tmp_path / "MATRag"
    ate_root = tmp_path / "ATEStudio"
    _write_matrag_tree(matrag_root, _METHODS_YAML)
    _write_ate_tree(ate_root, bindings_yaml=_BINDINGS_YAML, driver_py=_DRIVER_PY)
    monkeypatch.setattr(cv, "load_kind_units", lambda: dict(_ALIGNED_KIND_UNITS))

    inputs = cv.build_inputs(matrag_root, ate_root)
    assert inputs.kind_units == _ALIGNED_KIND_UNITS
    assert len(inputs.bindings) == 2
    assert inputs.method_ids == frozenset({"M-A", "M-B"})
    assert inputs.driver_actions == frozenset({"set_ac_input", "measure_dc_voltage"})


def test_build_inputs_missing_methods_file_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cv, "load_kind_units", lambda: dict(_ALIGNED_KIND_UNITS))
    with pytest.raises(cv.ContractSyncError, match="方法目录"):
        cv.build_inputs(tmp_path / "MATRag", tmp_path / "ATEStudio")


def test_build_inputs_missing_ate_root_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    matrag_root = tmp_path / "MATRag"
    _write_matrag_tree(matrag_root, _METHODS_YAML)
    monkeypatch.setattr(cv, "load_kind_units", lambda: dict(_ALIGNED_KIND_UNITS))
    with pytest.raises(cv.ContractSyncError, match="ATEStudio 仓库根目录"):
        cv.build_inputs(matrag_root, tmp_path / "NOPE")


# ── resolve_ate_root ────────────────────────────────────────────────────────


def test_resolve_ate_root_default_is_sibling(tmp_path: Path) -> None:
    assert cv.resolve_ate_root(tmp_path) == (tmp_path / ".." / "ATEStudio").resolve()


def test_resolve_ate_root_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "ate_studio"
    monkeypatch.setenv(cv.ATE_STUDIO_ROOT_ENV, str(target))
    assert cv.resolve_ate_root(tmp_path) == target


def test_resolve_ate_root_explicit_beats_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cv.ATE_STUDIO_ROOT_ENV, str(tmp_path / "env"))
    assert cv.resolve_ate_root(tmp_path, override=str(tmp_path / "arg")) == tmp_path / "arg"


# ── check_vocab.main: 退出码 ────────────────────────────────────────────────


def _aligned_inputs(matrag_root: Path, ate_root: Path) -> cv.Inputs:
    return cv.Inputs(
        matrag_root=matrag_root,
        ate_root=ate_root,
        kind_units=dict(_ALIGNED_KIND_UNITS),
        bindings=(
            _binding("input_voltage", "input", "set_ac_input", "M-A", "Vac"),
            _binding("output_voltage", "output", "measure_dc_voltage", "M-B", "V"),
        ),
        method_ids=frozenset({"M-A", "M-B"}),
        driver_actions=frozenset({"set_ac_input", "measure_dc_voltage"}),
    )


def test_vocab_main_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cv, "build_inputs", lambda *a, **k: _aligned_inputs(tmp_path, tmp_path))
    assert cv.main([]) == cv.EXIT_OK


def test_vocab_main_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    drifted = cv.Inputs(
        matrag_root=tmp_path,
        ate_root=tmp_path,
        kind_units={**_ALIGNED_KIND_UNITS, ("ripple", "output"): "mVpp"},
        bindings=_aligned_inputs(tmp_path, tmp_path).bindings,
        method_ids=frozenset({"M-A", "M-B"}),
        driver_actions=frozenset({"set_ac_input", "measure_dc_voltage"}),
    )
    monkeypatch.setattr(cv, "build_inputs", lambda *a, **k: drifted)
    assert cv.main([]) == cv.EXIT_DRIFT


def test_vocab_main_namespace_drift_is_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """真实命名空间漂移 (M-* 内部 vs 外部标准) 只出 WARN, 退出码仍为 0。"""
    drifted = cv.Inputs(
        matrag_root=tmp_path,
        ate_root=tmp_path,
        kind_units=dict(_ALIGNED_KIND_UNITS),
        bindings=(
            _binding("input_voltage", "input", "set_ac_input", "en300132_ripple", "Vac"),
            _binding("output_voltage", "output", "measure_dc_voltage", "iec62368_protection", "V"),
        ),
        method_ids=frozenset({"M-A", "M-B"}),
        driver_actions=frozenset({"set_ac_input", "measure_dc_voltage"}),
    )
    monkeypatch.setattr(cv, "build_inputs", lambda *a, **k: drifted)
    assert cv.main([]) == cv.EXIT_OK
    out = capsys.readouterr().out
    assert "[WARN]" in out
    assert "exit 0" in out


def test_vocab_main_action_drift_is_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#3 硬门禁: 绑定表 action 不在 DRIVER_TABLE → 退出码 1。"""
    drifted = cv.Inputs(
        matrag_root=tmp_path,
        ate_root=tmp_path,
        kind_units=dict(_ALIGNED_KIND_UNITS),
        bindings=(
            _binding("input_voltage", "input", "set_ac_input", "M-A", "Vac"),
            _binding("output_voltage", "output", "no_such_action", "M-B", "V"),
        ),
        method_ids=frozenset({"M-A", "M-B"}),
        driver_actions=frozenset({"set_ac_input"}),
    )
    monkeypatch.setattr(cv, "build_inputs", lambda *a, **k: drifted)
    assert cv.main([]) == cv.EXIT_DRIFT


def test_vocab_main_config_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def _boom(*_a: object, **_k: object) -> cv.Inputs:
        raise cv.ContractSyncError("缺少 ATEStudio")

    monkeypatch.setattr(cv, "build_inputs", _boom)
    assert cv.main([]) == cv.EXIT_CONFIG
    assert "ERROR: 缺少 ATEStudio" in capsys.readouterr().err


# ── check_contract_hash: 纯函数校验 ─────────────────────────────────────────


def test_run_hash_checks_all_equal() -> None:
    snap = cch.HashSnapshot(
        matrag_known="deadbeef",
        matrag_computed="deadbeef",
        ate_known="deadbeef",
        ate_computed="deadbeef",
    )
    results = cch.run_hash_checks(snap)
    assert all(r.ok for r in results)


def test_run_hash_checks_cross_repo_mismatch() -> None:
    snap = cch.HashSnapshot("a", "a", "b", "b")
    results = cch.run_hash_checks(snap)
    assert results[0].ok is False  # 两侧实际指纹不一致
    assert results[1].ok is True  # 各侧 known 与自身计算值一致
    assert results[2].ok is True


def test_run_hash_checks_stale_baseline() -> None:
    snap = cch.HashSnapshot("old", "new", "new", "new")
    results = cch.run_hash_checks(snap)
    assert results[0].ok is True
    assert results[1].ok is False  # MATRag 冻结基线过期


# ── check_contract_hash: 读取器 / 解释器探测 ────────────────────────────────


_ATE_BUNDLE_PY = """\
CONTRACT_MAJOR = 1
BUNDLE_VERSION = "1.0"
KNOWN_CONTRACT_HASH = "0123abcd0123abcd0123abcd0123abcd"
"""


def test_load_ate_known_hash_parses(tmp_path: Path) -> None:
    path = tmp_path / cch.ATE_BUNDLE_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_ATE_BUNDLE_PY, encoding="utf-8")
    assert cch.load_ate_known_hash(tmp_path) == "0123abcd0123abcd0123abcd0123abcd"


def test_load_ate_known_hash_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(cch.ContractSyncError, match="bundle 契约文件不存在"):
        cch.load_ate_known_hash(tmp_path)


def test_load_ate_known_hash_no_constant_raises(tmp_path: Path) -> None:
    path = tmp_path / cch.ATE_BUNDLE_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("CONTRACT_MAJOR = 1\n", encoding="utf-8")
    with pytest.raises(cch.ContractSyncError, match="KNOWN_CONTRACT_HASH"):
        cch.load_ate_known_hash(tmp_path)


def test_find_ate_python_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cch.ATE_PYTHON_ENV, raising=False)
    with pytest.raises(cch.ContractSyncError, match="venv 解释器"):
        cch.find_ate_python(tmp_path)


def test_find_ate_python_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "python.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv(cch.ATE_PYTHON_ENV, str(fake))
    assert cch.find_ate_python(tmp_path) == fake


def test_find_ate_python_env_override_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cch.ATE_PYTHON_ENV, str(tmp_path / "ghost.exe"))
    with pytest.raises(cch.ContractSyncError, match="不存在"):
        cch.find_ate_python(tmp_path)


def test_find_ate_python_detects_venv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cch.ATE_PYTHON_ENV, raising=False)
    exe = tmp_path / ".venv" / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("", encoding="utf-8")
    assert cch.find_ate_python(tmp_path) == exe


# ── compute_ate_contract_hash: subprocess (mock) ────────────────────────────


def test_compute_ate_hash_subprocess_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="noise\nCONTRACT_HASH=abc123\n", stderr="")

    monkeypatch.setattr(cch.subprocess, "run", _fake_run)
    got = cch.compute_ate_contract_hash(tmp_path, python_exe=Path(sys.executable))
    assert got == "abc123"


def test_compute_ate_hash_subprocess_nonzero_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(cch.subprocess, "run", _fake_run)
    with pytest.raises(cch.ContractSyncError, match="退出码 1"):
        cch.compute_ate_contract_hash(tmp_path, python_exe=Path(sys.executable))


def test_compute_ate_hash_subprocess_no_marker_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="only noise\n", stderr="")

    monkeypatch.setattr(cch.subprocess, "run", _fake_run)
    with pytest.raises(cch.ContractSyncError, match="CONTRACT_HASH 标记"):
        cch.compute_ate_contract_hash(tmp_path, python_exe=Path(sys.executable))


# ── build_snapshot / main ───────────────────────────────────────────────────


def test_build_snapshot_missing_ate_root_raises(tmp_path: Path) -> None:
    with pytest.raises(cch.ContractSyncError, match="ATEStudio 仓库根目录不存在"):
        cch.build_snapshot(tmp_path, tmp_path / "NOPE")


def _snapshot_ok() -> cch.HashSnapshot:
    return cch.HashSnapshot("h", "h", "h", "h")


def test_hash_main_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cch, "build_snapshot", lambda *a, **k: _snapshot_ok())
    assert cch.main([]) == cch.EXIT_OK


def test_hash_main_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cch, "build_snapshot", lambda *a, **k: cch.HashSnapshot("a", "a", "b", "b"))
    assert cch.main([]) == cch.EXIT_DRIFT


def test_hash_main_config_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def _boom(*_a: object, **_k: object) -> cch.HashSnapshot:
        raise cch.ContractSyncError("无 ATEStudio venv")

    monkeypatch.setattr(cch, "build_snapshot", _boom)
    assert cch.main([]) == cch.EXIT_CONFIG
    assert "ERROR: 无 ATEStudio venv" in capsys.readouterr().err
