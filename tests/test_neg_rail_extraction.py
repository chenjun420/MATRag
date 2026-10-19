"""GAP-01 负轨 min/max 归一 的抽取侧单元测试（全离线）。

背景
----
规格书作者对负轨常按"幅值"书写区间 (如 -54V 轨 `最小值 -53.2 / 最大值 -54.8`),
解析后 ``min > max``。消费侧 ``_judge`` 假定 ``下限 ≤ 上限``, 反向会让合格读数被
误判超差。抽取侧 :func:`matrag.extract.patterns.ascending_limits` 把负轨区间归一为
数学升序; 这里锁住其行为 (含"非负区间不动"的守卫)。

覆盖
----
- ``ascending_limits``: 负轨反向 → 交换; 负轨已升序 → 不动; 含非负端 → 不动;
  typ 与 unit 保持; None/空 → 原样;
- ``build_value``: 负轨行 → 升序 (数学意义 min<max); 正常正区间 → 行为不变;
- ``split_multi_value``: 负值拆分点同样归一;
- 真实语料 PA601-D54A: 1201 行负轨判据在最终 requirement 里为升序。
"""

from __future__ import annotations

from typing import Any

from matrag.domain.spec_params import SpecParameter, extract_spec_parameters
from matrag.extract.patterns import ascending_limits, build_value, split_multi_value
from matrag.extract.spec_tables import build_from_tables


def _param(
    *,
    name: str = "",
    category: str = "output",
    unit: str = "",
    minimum=None,
    typical=None,
    maximum=None,
    minimum_raw: str | None = None,
    typical_raw: str | None = None,
    maximum_raw: str | None = None,
) -> SpecParameter:
    """构造 SpecParameter, 只填本测试关心的字段。"""
    return SpecParameter(
        param_id="test-id",
        product_code="TEST",
        doc_id="TEST-doc",
        section_path="",
        table_id="t0",
        row_index=0,
        name=name,
        sub_name="",
        branch="",
        code="SR-TEST-0",
        grade="B",
        unit=unit,
        minimum=minimum,
        typical=typical,
        maximum=maximum,
        minimum_raw=minimum_raw if minimum_raw is not None else (str(minimum) if minimum is not None else ""),
        typical_raw=typical_raw if typical_raw is not None else (str(typical) if typical is not None else ""),
        maximum_raw=maximum_raw if maximum_raw is not None else (str(maximum) if maximum is not None else ""),
        note="",
        condition="",
        requirement="",
        scope="",
        category=category,
        flags=(),
    )


# ────────────── ascending_limits ──────────────


def test_ascending_limits_swaps_reversed_negative() -> None:
    """负轨反向 (-53.2 > -54.8) → 交换为 min=-54.8, max=-53.2。"""
    out = ascending_limits({"min": -53.2, "typ": -54.0, "max": -54.8, "unit": "V"})
    assert out is not None
    assert out["min"] == -54.8 and out["max"] == -53.2
    # typ 与 unit 原样保留 (typ 不参与区间判定, 不重排)
    assert out["typ"] == -54.0 and out["unit"] == "V"


def test_ascending_limits_already_ascending_negative_untouched() -> None:
    """负轨已升序 (min<max) → 原样返回同一对象。"""
    value = {"min": -54.8, "typ": -54.0, "max": -53.2, "unit": "V"}
    assert ascending_limits(value) is value


def test_ascending_limits_positive_reversed_not_touched() -> None:
    """非负区间 (含解析占位 0.0 的可疑反向) 不在"负轨"范畴 → 不动, 避免误改。"""
    value = {"min": 5.0, "max": 0.0, "unit": "A"}
    assert ascending_limits(value) is value


def test_ascending_limits_none_and_empty() -> None:
    assert ascending_limits(None) is None
    assert ascending_limits({}) == {}


# ────────────── build_value ──────────────


def test_build_value_negative_rail_normalized_ascending() -> None:
    """负轨行 (-53.2 ~ -54.8) → 数学升序 min=-54.8 < max=-53.2。"""
    p = _param(name="输出电压整定值", unit="V", minimum=-53.2, typical=-54.0, maximum=-54.8)
    v = build_value(p, "output_voltage")
    assert v is not None
    assert v["min"] == -54.8 and v["typ"] == -54.0 and v["max"] == -53.2
    assert v["min"] < v["max"], "必须满足消费侧 _judge 的 min ≤ max 前提"


def test_build_value_positive_range_unchanged() -> None:
    """正常正区间 → 行为不变 (min/typ/max 原样)。"""
    p = _param(name="输出电压", unit="V", minimum=53.2, typical=54.0, maximum=54.8)
    v = build_value(p, "output_voltage")
    assert v is not None
    assert v["min"] == 53.2 and v["typ"] == 54.0 and v["max"] == 54.8


def test_build_value_one_sided_negative_unchanged() -> None:
    """单边负值 (仅 min) → 无 max, 不交换。"""
    p = _param(name="额定输出电压", unit="V", minimum=-54.0)
    v = build_value(p, "output_voltage")
    assert v is not None
    assert v == {"min": -54.0, "unit": "V"}


# ────────────── split_multi_value ──────────────


def test_split_multi_value_negative_points_ascending() -> None:
    """多值保护行的负值拆分点同样归一为升序 (保护点/恢复点)。"""
    p = _param(
        name="保护功能\n保护点\n恢复点",
        category="protection",
        unit="V<br>V",
        minimum_raw="-53.2\n-52.0",
        maximum_raw="-54.8\n-53.6",
    )
    out = split_multi_value(p, "protection_action")
    assert out is not None and len(out) == 2
    _, v0 = out[0]
    assert v0 is not None and v0["min"] == -54.8 and v0["max"] == -53.2


# ────────────── 真实语料端到端 ──────────────


def _pa601_output_voltage_value(code: str) -> dict[str, Any]:
    params = [p for p in extract_spec_parameters() if p.product_code == "PA601-D54A"]
    result = build_from_tables(params)
    for req in result.requirements:
        if req.requirement_code == code:
            clause = req.output_conditions[0]
            assert clause.value is not None
            return dict(clause.value)
    raise AssertionError(f"未找到 {code}")


def test_real_corpus_pa601_negative_rail_ascending() -> None:
    """真实语料: PA601 1201 负轨判据在最终 requirement 里为数学升序。"""
    value = _pa601_output_voltage_value("SR-PA601-D54A-1201-m-53.2")
    assert value["min"] == -54.8 and value["typ"] == -54.0 and value["max"] == -53.2
    assert value["min"] < value["max"]


def test_real_corpus_no_negative_rail_limit_is_reversed() -> None:
    """全语料: 任何负轨限值都不得出现 min > max (GAP-01 归一的不变量)。

    只锁负轨 (两侧同为负); 正区间的可疑反向行 (如保护表 `5.0 ~ 0.0`, 多为解析
    占位) 不在本修复范畴, 故意不动。
    """
    for product in ("PA601-D54A", "GDC800T5054-UC", "CY0177-75V-20A"):
        params = [p for p in extract_spec_parameters() if p.product_code == product]
        for req in build_from_tables(params).requirements:
            for lim in req.limits:
                if lim.min is not None and lim.max is not None and lim.min < 0 and lim.max < 0:
                    assert lim.min <= lim.max, f"{product}/{req.requirement_code} 负轨限值反向: {lim}"
