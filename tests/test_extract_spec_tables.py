"""``matrag.extract.spec_tables.build_from_tables`` 的真实语料 pin 测试（全离线）。

口径
----
- 三份语料 (CY0177-75V-20A / GDC800T5054-UC / PA601-D54A) 在 ``data/corpus/``;
- 当前计数 pin (规格 / 算法冻结时记录的快照):
    - CY  req 24 / excl 17 / review 9
    - GDC req 51 / excl 12 / review 43
    - PA601 req 76 / excl 128 / review 16
- 输入电压基线 pin (产品级输入工作范围, 用于原文未给输入条件时的兜底):
    - CY   85~300 Vac, typ 220
    - GDC  36~60 V (typ 48)
    - PA601 88~290 Vac, typ 220
"""

from __future__ import annotations

import pytest

from matrag.domain.spec_params import extract_spec_parameters
from matrag.extract.spec_tables import build_from_tables

# ────────────── fixtures ──────────────


@pytest.fixture(scope="module")
def corpus_by_code() -> dict[str, list]:
    """加载全量 spec 参数并按 product_code 分组 (模块级, 一次 IO)。"""
    by: dict[str, list] = {}
    for p in extract_spec_parameters():
        by.setdefault(p.product_code, []).append(p)
    return by


@pytest.fixture(scope="module")
def pa601_result(corpus_by_code):
    return build_from_tables(corpus_by_code["PA601-D54A"])


@pytest.fixture(scope="module")
def cy_result(corpus_by_code):
    return build_from_tables(corpus_by_code["CY0177-75V-20A"])


@pytest.fixture(scope="module")
def gdc_result(corpus_by_code):
    return build_from_tables(corpus_by_code["GDC800T5054-UC"])


# ────────────── 总数 pin ──────────────


def test_pa601_counts_pinned(pa601_result) -> None:
    """PA601: req 76 / excl 128 / review 16。"""
    r = pa601_result
    assert len(r.requirements) == 76
    assert len(r.excluded) == 128
    assert len(r.review) == 16


def test_cy_counts_pinned(cy_result) -> None:
    """CY: req 24 / excl 17 / review 9。"""
    r = cy_result
    assert len(r.requirements) == 24
    assert len(r.excluded) == 17
    assert len(r.review) == 9


def test_gdc_counts_pinned(gdc_result) -> None:
    """GDC: req 51 / excl 12 / review 43。"""
    r = gdc_result
    assert len(r.requirements) == 51
    assert len(r.excluded) == 12
    assert len(r.review) == 43


# ────────────── 编号唯一性 / 后缀消歧 ──────────────


def test_requirement_codes_unique_within_product(pa601_result) -> None:
    """同一产品内 requirement_code 不重。"""
    codes = [r.requirement_code for r in pa601_result.requirements]
    assert len(codes) == len(set(codes))


def test_pa601_m53_2_variant_exists(pa601_result) -> None:
    """PA601-D54A 1201 行的负轨判据经 GAP-01 归一后, 代表值取数学上界 -53.2。

    原文幅值序 ``最小值 -53.2 / 最大值 -54.8`` 在抽取侧被归一为升序
    ``min=-54.8, max=-53.2``, 故 ``rep_value``(max 优先) 取 -53.2。
    """
    codes = {r.requirement_code for r in pa601_result.requirements}
    assert "SR-PA601-D54A-1201-m-53.2" in codes


def test_pa601_1308_c2_suffix(pa601_result) -> None:
    """PA601-D54A 1308 出现两次判据冲突, 第二个加 -c2 后缀。"""
    codes = {r.requirement_code for r in pa601_result.requirements}
    assert "SR-PA601-D54A-1308" in codes
    assert "SR-PA601-D54A-1308-c2" in codes


# ────────────── measure 类条件必须带值 ──────────────


@pytest.mark.parametrize("kind", ["output_voltage", "output_current", "output_power", "ripple", "efficiency", "timing"])
def test_measure_kinds_always_have_value(pa601_result, kind: str) -> None:
    """measure 类 kind 的 clause.value 必须非空 (有 min/typ/max 至少其一)。

    不允许"无判据测量"被物化为 requirement (那会让规划器排进 plan_gaps)。
    """
    for req in pa601_result.requirements:
        for clause in (*req.input_conditions, *req.output_conditions):
            if clause.kind == kind:
                assert clause.value is not None, f"{kind} no value: {req.requirement_code}"
                assert any(
                    isinstance(clause.value.get(k), (int, float))
                    for k in ("min", "typ", "max")
                ), f"{kind} value without numeric: {req.requirement_code}"


# ────────────── 基线输入电压 pin (兜底) ──────────────


def test_pa601_baseline_input_voltage(pa601_result) -> None:
    """PA601: 88~290 Vac, typ 220。"""
    bl = pa601_result.stats["baseline_input_voltage"]
    assert bl == {"min": 88.0, "typ": 220.0, "max": 290.0, "unit": "Vac"}


def test_cy_baseline_input_voltage(cy_result) -> None:
    """CY: 85~300 Vac, typ 220。"""
    bl = cy_result.stats["baseline_input_voltage"]
    assert bl == {"min": 85.0, "typ": 220.0, "max": 300.0, "unit": "Vac"}


def test_gdc_baseline_input_voltage(gdc_result) -> None:
    """GDC: 36~60 V (typ 48, 默认 V 单位)。"""
    bl = gdc_result.stats["baseline_input_voltage"]
    assert bl == {"min": 36.0, "typ": 48.0, "max": 60.0, "unit": "V"}


# ────────────── 排除判定 (第一/二步) ──────────────


def test_pa601_fangfan_excluded_for_not_required(pa601_result) -> None:
    """"输入防反功能" → excluded, matched_word="不要求" (规格明示不做)。"""
    for e in pa601_result.excluded:
        if "防反" in e.title:
            assert e.matched_word == "不要求"
            break
    else:
        pytest.fail("未找到『输入防反功能』排除行")


# ────────────── 复核 (第三步) ──────────────


def test_pa601_measure_no_value_for_kaiji_yanshi(pa601_result) -> None:
    """"开机输出延迟" 缺判据 → review kind=measure_no_value (绝不凑可执行性)。"""
    found_measure = False
    for r in pa601_result.review:
        if "开机输出延迟" in r.heading and r.kind == "measure_no_value":
            found_measure = True
            break
    assert found_measure, "『开机输出延迟』应落 measure_no_value"


def test_review_kinds_present(pa601_result) -> None:
    """review 至少覆盖 measure_no_value / unmapped 两类。"""
    kinds = {r.kind for r in pa601_result.review}
    assert "measure_no_value" in kinds
    assert "unmapped" in kinds


# ────────────── 边界 ──────────────


def test_empty_params_returns_empty_result() -> None:
    """空参数列表 → 空结果, 不抛。"""
    res = build_from_tables([])
    assert res.requirements == []
    assert res.excluded == []
    assert res.review == []
    # 空输入 stats 是空 dict (baseline 与 kinds 都不算)


def test_stats_keys(corpus_by_code) -> None:
    """stats 必填键: rows / requirements / excluded / review / kinds / baseline_input_voltage。"""
    res = build_from_tables(corpus_by_code["PA601-D54A"])
    for k in ("rows", "requirements", "excluded", "review", "kinds", "baseline_input_voltage"):
        assert k in res.stats


def test_requirement_scenarios_have_unique_seq(pa601_result) -> None:
    """每个 requirement 内的 scenarios seq 都从 1 开始且唯一。"""
    for req in pa601_result.requirements:
        seqs = [s.seq for s in req.scenarios]
        assert len(seqs) == len(set(seqs))
