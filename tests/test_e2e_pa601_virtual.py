"""``scripts/e2e_pa601_virtual.py`` 负轨判据/实测值回归测试。

覆盖 2026-10-19 修复的两处缺陷（均为仿真/判据的负轨符号处理问题，非产品违规）:

1. :func:`_judge` —— 负轨规格表 min/max 反排 (如 ``{min:-53.2, max:-54.8}``) 时,
   按归一化区间 ``[lo, hi]`` 判定, 消除恒 FAIL 的假失败。
2. :func:`simulate_step` (``output_voltage``) —— 负压单边/反排边界下 nominal 的
   方向修正, 使实测值落在归一化区间内。

脚本位于 ``scripts/`` 下无包名, 故用 ``importlib.util.spec_from_file_location``
按文件路径加载 (不改动全局 sys.path 假设); 测试不调用 ``main()``, 不写 ``data/reports``。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "e2e_pa601_virtual.py"


def _load_module():
    """按文件路径加载 e2e 虚拟脚本模块 (scripts/ 无包名)。"""
    spec = importlib.util.spec_from_file_location("e2e_pa601_virtual", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses 依赖 cls.__module__ 已在 sys.modules 中解析, 故先注册再 exec
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def e2e():
    return _load_module()


# ---------------------------------------------------------------------------
# _judge: 负轨反排 / 单边 / 边界
# ---------------------------------------------------------------------------


def test_judge_negated_bounds_reversed_is_ok(e2e) -> None:
    """负轨 min/max 反排 {min:-53.2, max:-54.8} → 归一化 [-54.8, -53.2], -53.46 判 OK。"""
    status, detail = e2e._judge(-53.46, {"min": -53.2, "max": -54.8})
    assert status == "OK"
    # detail 用归一化区间, 便于报告可读
    assert "-54.8" in detail and "-53.2" in detail


def test_judge_negated_min_only_out_of_limit_is_fail(e2e) -> None:
    """真实越限仍可判: -56.983 < min=-54.0 → FAIL。"""
    status, detail = e2e._judge(-56.983, {"min": -54.0})
    assert status == "FAIL"
    assert "<" in detail


def test_judge_negated_min_only_on_boundary_is_ok(e2e) -> None:
    """边界值 -54.0 == min → OK。"""
    status, _ = e2e._judge(-54.0, {"min": -54.0})
    assert status == "OK"


# ---------------------------------------------------------------------------
# simulate_step: output_voltage 负轨两案例
# ---------------------------------------------------------------------------

# bundle data/exports/PA601-D54A.bundle.json 中的现成 cond_fingerprint
FP_1200_M54 = "ec0c398e16ff7567"      # SR-PA601-D54A-1200-m-54: {min:-54.0}
FP_1201_M548 = "35febfbdf561e2c4"     # SR-PA601-D54A-1201-m-54.8: {min:-53.2, typ:-54.0, max:-54.8}


def test_simulate_step_1200_min_only(e2e) -> None:
    """单边负压 {min:-54.0}: nominal=-51.3±噪声 → 实测 ≥ min, status OK。"""
    bus = e2e.VirtualDeviceBus()
    cond = {
        "kind": "output_voltage",
        "role": "output",
        "cond_fingerprint": FP_1200_M54,
        "value": {"min": -54.0, "unit": "V"},
    }
    r = e2e.simulate_step(step_id="SR-PA601-D54A-1200-m-54", condition=cond, bus=bus)
    assert r.status == "OK", r.detail
    # 实测落在期望下界 (归一化后 single-side) 内, 且约 -51.3±0.26
    assert r.expected_min == -54.0
    assert r.measured >= r.expected_min
    assert abs(r.measured - (-51.3)) <= 0.27


def test_simulate_step_1201_reversed_bounds(e2e) -> None:
    """双边界反排 {min:-53.2, max:-54.8}: 归一化 [-54.8, -53.2], 实测落在其中。"""
    bus = e2e.VirtualDeviceBus()
    cond = {
        "kind": "output_voltage",
        "role": "output",
        "cond_fingerprint": FP_1201_M548,
        "value": {"min": -53.2, "typ": -54.0, "max": -54.8, "unit": "V"},
    }
    r = e2e.simulate_step(step_id="SR-PA601-D54A-1201-m-54.8", condition=cond, bus=bus)
    assert r.status == "OK", r.detail
    # 双边界时 expected_min/max 存归一化值
    assert r.expected_min == -54.8
    assert r.expected_max == -53.2
    assert r.expected_min <= r.measured <= r.expected_max
