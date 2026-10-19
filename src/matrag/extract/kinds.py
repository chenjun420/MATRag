"""条件 kind 词表 —— 必须与 ATEStudio ``config/test_method_bindings.yaml`` 逐字一致。

为什么本模块存在
----------------
规划器只认绑定表里出现的 ``(kind, side)``: 多一个词, 那条条件永远排不出步骤
**且不报错** (规划器把无绑定的条件记进 ``unmapped``, 但不会让整体失败)。少一个
词则等于自断一类测试。因此生产侧在本层就做闸门: 词表外的 kind 直接拒绝
(:class:`matrag.extract.errors.KindViolationError`)。

两个方向的对齐
--------------
消费侧的 ``scripts/verify_bindings.py`` 会对真实 bundle 与绑定表做**双向差集**
(表里有而未出现 = WARN 空绑定)。本侧只保证"emitted ⊆ 表"这一方向 —— 空绑定是
消费侧的运维话题, 不是生产侧的错误。
"""

from __future__ import annotations

from matrag.extract.errors import KindViolationError

__all__ = [
    "KIND_SIDES",
    "KIND_UNITS",
    "KNOWN_KIND_SIDES",
    "kinds_for_side",
    "validate_clause_kind",
]

#: (kind, side) → 绑定表声明的量纲约定 (用于校验绑定是否配错; "" 表示无约定)。
#: 数据来源: ATEStudio ``config/test_method_bindings.yaml`` version 1 (25 条)。
KIND_UNITS: dict[tuple[str, str], str] = {
    # ── 输入侧: 设置外部激励 ──────────────────────────────────────────────
    ("input_voltage", "input"): "Vac",
    ("input_frequency", "input"): "Hz",
    ("input_type", "input"): "",
    ("load", "input"): "A",
    ("duty", "input"): "%",
    ("temperature", "input"): "degC",
    ("measurement_setup", "input"): "",
    ("command", "input"): "",
    ("test_mode", "input"): "",
    ("power_event", "input"): "",
    ("power_factor", "input"): "",
    ("fault_stimulus", "input"): "",
    ("output_current", "input"): "A",
    # ── 输出侧: 读取并判定 ────────────────────────────────────────────────
    ("cap_load", "output"): "uF",
    ("output_voltage", "output"): "V",
    ("output_current", "output"): "A",
    ("output_power", "output"): "W",
    ("ripple", "output"): "mVpp",
    ("efficiency", "output"): "%",
    ("timing", "output"): "ms",
    ("signal_state", "output"): "",
    ("telemetry_value", "output"): "",
    ("protection_action", "output"): "",
    ("presence", "output"): "",
    ("measurement_setup", "output"): "",
}

#: 冻结的 (kind, side) 集合 (25 条)。
KNOWN_KIND_SIDES: frozenset[tuple[str, str]] = frozenset(KIND_UNITS)

#: kind → 允许出现的 side 集合 (measurement_setup 等两侧均可)。
KIND_SIDES: dict[str, frozenset[str]] = {}
for _pair in KIND_UNITS:
    KIND_SIDES.setdefault(_pair[0], frozenset())
    KIND_SIDES[_pair[0]] = KIND_SIDES[_pair[0]] | {_pair[1]}
del _pair


def kinds_for_side(side: str) -> frozenset[str]:
    """返回某一侧全部合法 kind (供提示词/校验使用)。"""
    return frozenset(k for (k, s) in KNOWN_KIND_SIDES if s == side)


def validate_clause_kind(kind: str, side: str, *, origin: str = "") -> None:
    """kind/side 不在词表内即抛 :class:`KindViolationError` (fail-fast)。"""
    if (kind, side) not in KNOWN_KIND_SIDES:
        raise KindViolationError(kind, side, origin=origin)
