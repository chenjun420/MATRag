"""``matrag.domain.spec_params`` 的离线测试。

覆盖三部分:

1. **fixture 小表**: 三种真实表头布局、单位列位置、占位符、``±``/``+``/en-dash 负号、
   ``<br>`` 多值、范围、行长不齐、全空行、重复"项目"列 (第二个作 ``sub_name``);
2. **分类函数**: :func:`classify_section` 各分支与兜底、:func:`classify_table` 正反例;
3. **真实语料**: 对 ``data/corpus`` 的 3 份规格书核对实测值 (总量/按产品/关键参数/
   多值标记/标准引用) 与确定性。

全部离线、确定性; 真数据断言均按实测结果写死 (见各断言注释)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from matrag.domain.spec_params import (
    CATEGORY_VALUES,
    DEFAULT_CORPUS_DIR,
    SPEC_PARAM_FLAGS,
    SpecParameter,
    SpecStandard,
    classify_section,
    classify_table,
    extract_spec_parameters,
    extract_standards,
    spec_param_stats,
)
from matrag.domain.vocab import CATEGORIES

# --------------------------------------------------------------------------- #
# fixture 工具
# --------------------------------------------------------------------------- #


def _write_corpus(
    root: Path,
    doc_id: str,
    tables: list[dict],
    *,
    table_section: str = "",
    table_id: str = "t0-0",
    headers: list[str] | None = None,
    rows: list[list[str]] | None = None,
) -> Path:
    """在 ``tmp_path`` 下造一份最小语料 (doc.json + tables.json)。"""
    (root / f"{doc_id}.doc.json").write_text(
        json.dumps({"doc_id": doc_id, "title": doc_id}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / f"{doc_id}.tables.json").write_text(
        json.dumps(tables, ensure_ascii=False), encoding="utf-8"
    )
    return root


def _table(
    headers: list[str],
    rows: list[list[str]],
    *,
    table_id: str = "t0-0",
    section_path: str = "",
) -> dict:
    """构造与真实产物同构的一张表。"""
    return {
        "id": table_id,
        "caption": "",
        "headers": headers,
        "rows": rows,
        "marks": [],
        "confidence": 1.0,
        "source": {
            "page": 1,
            "section_path": section_path,
            "provider": "local",
            "marks": [],
        },
    }


def _extract_one(root: Path, doc_id: str = "TEST-001") -> list[SpecParameter]:
    """抽取 fixture 语料 (单文档) 并断言非空。"""
    params = extract_spec_parameters(root)
    assert all(param.doc_id == doc_id for param in params)
    return params


def _only(root: Path, name: str) -> SpecParameter:
    """按名称取唯一一条参数实例。"""
    hits = [param for param in _extract_one(root) if param.name == name]
    assert len(hits) == 1, f"{name!r} 命中 {len(hits)} 条"
    return hits[0]


# --------------------------------------------------------------------------- #
# 1. 三种真实表头布局
# --------------------------------------------------------------------------- #


def test_extract_cy_layout_unit_before_minimum(tmp_path: Path) -> None:
    """CY0177 布局 ``项目|单位|最小值|典型值|最大值|备注``: 四列各就各位。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["项目", "单位", "最小值", "典型值", "最大值", "备注"],
                [["交流输入电压范围", "Vac", "85", "220", "300", "输入低于180V线性降额"]],
            )
        ],
    )
    param = _only(tmp_path, "交流输入电压范围")
    assert param.unit == "Vac"
    assert (param.minimum, param.typical, param.maximum) == (85.0, 220.0, 300.0)
    raws = (param.minimum_raw, param.typical_raw, param.maximum_raw)
    assert raws == ("85", "220", "300")
    assert param.note == "输入低于180V线性降额"
    assert param.condition == ""  # 备注列的别名是 note, 不串到 condition
    assert param.flags == ()
    assert param.param_id == "TEST-001|t0-0|0"


def test_extract_gdc_layout_unit_after_numbers(tmp_path: Path) -> None:
    """GDC 布局 ``项目|最小值|典型值|最大值|单位|说明``: 单位在数值之后也不串位。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["项目", "最小值", "典型值", "最大值", "单位", "说明"],
                [["工作电压", "36", "48", "60", "V", "Vin=48V"]],
            )
        ],
    )
    param = _only(tmp_path, "工作电压")
    assert param.unit == "V"
    assert (param.minimum, param.typical, param.maximum) == (36.0, 48.0, 60.0)
    assert param.note == "Vin=48V"
    assert param.branch == ""


def test_extract_pa601_layout_code_and_grade(tmp_path: Path) -> None:
    """PA601 布局 ``编号|项目|单位|最小值|典型值|最大值|备注|等级``: 编号/等级各归其位。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["编号", "项目", "单位", "最小值", "典型值", "最大值", "备注", "等级"],
                [["SR-1100", "标称输入电压范围", "Vac", "100", "110", "127", "铭牌标称", "强制"]],
            )
        ],
    )
    param = _only(tmp_path, "标称输入电压范围")
    assert param.code == "SR-1100"
    assert param.grade == "强制"
    assert param.unit == "Vac"
    assert (param.minimum, param.typical, param.maximum) == (100.0, 110.0, 127.0)


def test_extract_extra_unknown_column_does_not_shift(tmp_path: Path) -> None:
    """多余列 (如保护表的"有/无") 必须被忽略而非错位。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                [
                    "编号", "项目", "项目", "单位", "有/无",
                    "最小值", "典型值", "最大值", "备注", "等级",
                ],
                [
                    [
                        "SR-1300", "输入过压保护点", "输入过压保护点", "Vac/Vdc", "有",
                        "305", "-", "-", "额定输入", "强制",
                    ]
                ],
            )
        ],
    )
    param = _only(tmp_path, "输入过压保护点")
    assert param.minimum == 305.0  # "有" 不会被当成最小值
    assert param.minimum_raw == "305"
    assert param.maximum is None
    assert param.sub_name == "输入过压保护点"
    assert param.unit == "Vac/Vdc"
    assert param.flags == ()


def test_extract_duplicate_name_column_second_is_sub_name(tmp_path: Path) -> None:
    """重复"项目"列: 首列取 name, 第二个归 sub_name, 第三个及以后忽略。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["项目", "项目", "项目", "单位", "最小值", "最大值"],
                [["额定输出电压", "-54V", "多余子项", "V", "1", "3"]],
            )
        ],
    )
    param = _only(tmp_path, "额定输出电压")
    assert param.sub_name == "-54V"  # 第二列, 不是第三列
    assert param.minimum == 1.0
    assert param.maximum == 3.0
    assert param.minimum_raw == "1"  # 未被 sub_name 串位


def test_extract_sub_item_header_column(tmp_path: Path) -> None:
    """``项目/名称`` + ``项目/子项`` 布局: 子项进入 sub_name。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["项目/名称", "项目/子项", "单位", "最小值", "典型值", "最大值", "备注"],
                [["输入欠压保护", "欠压点", "Vac", "85", "/", "/", "上位机发指令后才开机"]],
            )
        ],
    )
    param = _only(tmp_path, "输入欠压保护")
    assert param.sub_name == "欠压点"
    assert param.minimum == 85.0
    assert param.typical is None
    assert param.maximum is None


def test_extract_requirement_table_layout(tmp_path: Path) -> None:
    """要求型布局 ``编号|项目|信号名称|信号要求|备注|等级``: 声明式列全部入参。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["编号", "项目", "信号名称", "信号要求", "备注", "等级"],
                [["SR-2001", "输入欠压告警", "VIN_UN_ALM", "OC门输出,与系统GND共地", "低电平有效", "强制"]],
                section_path="4.3 功能/性能要求 > 4.3.4 监控功能 > 4.3.4.1 遥信功能",
            )
        ],
    )
    param = _only(tmp_path, "输入欠压告警")
    assert param.sub_name == "VIN_UN_ALM"
    assert param.requirement == "OC门输出,与系统GND共地"
    assert param.note == "低电平有效"
    assert param.grade == "强制"
    assert param.scope == "遥信功能"  # 无数值列 → 表级作用域取章节叶名
    assert param.minimum is None and param.flags == ()


def test_extract_multi_requirement_columns_joined(tmp_path: Path) -> None:
    """``试验等级``/``持续时间`` 同入 requirement: 按列序「；」拼接, 数值不猜。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["试验项目", "试验等级(%Ut)", "持续时间(s)", "性能判据"],
                [["电压暂降", "70", "0.01", "见表注1"]],
                section_path="4.4.2 EMC指标要求",
            )
        ],
    )
    param = _only(tmp_path, "电压暂降")
    assert param.requirement == "70；0.01"
    assert param.grade == "见表注1"
    assert param.minimum is None and param.typical is None and param.maximum is None
    assert param.flags == ()
    assert param.category == "emc"


# --------------------------------------------------------------------------- #
# 2. 占位符 / 数值解析
# --------------------------------------------------------------------------- #


def test_placeholders_keep_raw_and_are_not_unparsed(tmp_path: Path) -> None:
    """占位符 ``/`` 与 ``-``: raw 保留、数值 None、**不打** unparsed。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["项目", "单位", "最小值", "典型值", "最大值"],
                [["待机功耗", "W", "/", "-", "/"]],
            )
        ],
    )
    param = _only(tmp_path, "待机功耗")
    assert (param.minimum, param.typical, param.maximum) == (None, None, None)
    assert (param.minimum_raw, param.typical_raw, param.maximum_raw) == ("/", "-", "/")
    assert "unparsed" not in param.flags


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("–40", -40.0),   # U+2013 en-dash 负号
        ("—25", -25.0),   # U+2014 em-dash 负号
        ("-25", -25.0),
        ("+3.45", 3.45),  # 前导加号
        ("-0.02", -0.02),
    ],
)
def test_signed_numbers_parse(tmp_path: Path, raw: str, expected: float) -> None:
    """en-dash/em-dash/``-``/``+`` 前导符号都要按符号解析, 且不许打 unparsed。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["项目", "单位", "最小值", "典型值", "最大值"],
                [["电压", "V", raw, "0", "10"]],
            )
        ],
    )
    param = _only(tmp_path, "电压")
    assert param.minimum == expected
    assert param.minimum_raw == raw.replace("–", "-").replace("—", "-")
    assert param.flags == ()


@pytest.mark.parametrize("raw", ["±1.5", "±0.5"])
def test_plus_minus_tolerance_marks_unparsed(tmp_path: Path, raw: str) -> None:
    """``±`` 是双向容差: 不猜数值, raw 保留并打 unparsed。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["项目", "单位", "最小值", "典型值", "最大值"],
                [["精度", "%", raw, "/", "/"]],
            )
        ],
    )
    param = _only(tmp_path, "精度")
    assert param.minimum is None
    assert param.minimum_raw == raw
    assert param.flags == ("unparsed",)


def test_br_multi_value_keeps_raw_and_flags(tmp_path: Path) -> None:
    """``<br>`` 多值: raw 完整保留 (换行可读), 数值 None, 打 multi_value。"""
    raw = "29.5<br>31.5<br>2"
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["项目", "单位", "最小值", "典型值", "最大值"],
                [["欠压保护", "V", raw, "31.5<br>33.5<br>3", "33.5<br>35.5<br>4"]],
            )
        ],
    )
    param = _only(tmp_path, "欠压保护")
    assert param.minimum is None
    assert param.maximum is None
    assert param.minimum_raw == "29.5\n31.5\n2"
    assert param.maximum_raw == "33.5\n35.5\n4"
    assert param.flags == ("multi_value",)


@pytest.mark.parametrize("raw", ["45~65", "110/220", "40%/70%"])
def test_range_multi_value_marks_flags(tmp_path: Path, raw: str) -> None:
    """范围 (``~`` / ``/`` / 百分号范围) 不拆不猜, raw 保留并打 multi_value。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["项目", "单位", "最小值", "典型值", "最大值"],
                [["频率", "Hz", "0", raw, "100"]],
            )
        ],
    )
    param = _only(tmp_path, "频率")
    assert param.typical is None
    assert param.typical_raw == raw
    assert param.flags == ("multi_value",)


def test_single_number_still_parses_without_multi_flag(tmp_path: Path) -> None:
    """单个数值不得被误判为多值。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["项目", "单位", "最小值", "典型值", "最大值"],
                [["输入电流", "A", "/", "/", "6"]],
            )
        ],
    )
    param = _only(tmp_path, "输入电流")
    assert param.maximum == 6.0
    assert param.flags == ()


def test_formula_like_value_marks_unparsed(tmp_path: Path) -> None:
    """``2%Vout`` 这类公式式取值: 不猜, raw 保留并打 unparsed。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["项目", "单位", "最小值", "典型值", "最大值"],
                [["噪声+纹波", "V", "/", "/", "2%Vout"]],
            )
        ],
    )
    param = _only(tmp_path, "噪声+纹波")
    assert param.maximum is None
    assert param.maximum_raw == "2%Vout"
    assert param.flags == ("unparsed",)


# --------------------------------------------------------------------------- #
# 3. 行长不齐 / 空行 / 缺列
# --------------------------------------------------------------------------- #


def test_short_row_pads_missing_cells(tmp_path: Path) -> None:
    """行长不足: 缺的列补空, 前面的列不受影响。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["项目", "单位", "最小值", "典型值", "最大值"],
                [["短路保护", "V", "/", "/"]],
            )
        ],
    )
    param = _only(tmp_path, "短路保护")
    assert param.unit == "V"
    assert (param.minimum_raw, param.typical_raw, param.maximum_raw) == ("/", "/", "")
    assert (param.minimum, param.typical, param.maximum) == (None, None, None)


def test_long_row_ignores_extra_tail_cells(tmp_path: Path) -> None:
    """行长超长: 多余尾列忽略, 不污染已映射列。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["项目", "单位", "最小值", "典型值", "最大值"],
                [["输入电流", "A", "/", "/", "6", "多余1", "多余2"]],
            )
        ],
    )
    param = _only(tmp_path, "输入电流")
    assert param.maximum == 6.0
    assert param.note == ""
    assert param.grade == ""


def test_all_empty_row_is_skipped(tmp_path: Path) -> None:
    """name 与全部数值列都空的行不产生实例。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["项目", "单位", "最小值", "典型值", "最大值"],
                [["", "/", "/", "/", "/"], ["有效参数", "V", "1", "2", "3"]],
            )
        ],
    )
    params = _extract_one(tmp_path)
    assert [param.name for param in params] == ["有效参数"]
    assert params[0].row_index == 1


def test_section_path_used_as_category(tmp_path: Path) -> None:
    """类别取自表的 section_path, 而不是参数名。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["项目", "单位", "最小值", "最大值"],
                [["额定输出电流", "A", "0", "11.1"]],
                section_path="4 电气特性 > 4.3 输出特性",
            )
        ],
    )
    assert _only(tmp_path, "额定输出电流").category == "output"


# --------------------------------------------------------------------------- #
# 4. classify_section / classify_table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("section_path", "expected"),
    [
        ("3 技术要求 > 3.1 电源输入规格", "input"),
        ("4.3 功能/性能要求 > 4.3.2 输出特性", "output"),
        ("4.4.6.1 安规测试要求", "safety"),
        ("4.3.3 保护功能", "protection"),
        ("4.5 效率特性", "efficiency"),
        ("4.6 动态特性", "dynamics"),
        ("4.1 环境条件", "environment"),
        ("7 EMC 要求", "emc"),
        ("4.4 可靠性要求", "reliability"),
        ("9 通信 > 9.2 信号特性", "communication"),
        ("11 机械结构及标签", "mechanical"),
        ("10 电源可靠性要求", "reliability"),
        ("1.2 参考标准", "standards"),
        ("4.2 结构/工艺要求", "mechanical"),
        ("推荐电路", "other"),
        ("", "other"),
    ],
)
def test_classify_section_branches(section_path: str, expected: str) -> None:
    """章节关键词覆盖全部分支, 未命中回落到 other。"""
    assert classify_section(section_path) == expected
    assert classify_section(section_path) in CATEGORIES


def test_classify_table_true_for_parameter_layouts() -> None:
    """含 name 列且有数值列 ≥2 个 (或 requirement/condition 列) 的表头算参数表。"""
    assert classify_table(["项目", "单位", "最小值", "典型值", "最大值", "备注"])
    assert classify_table(["项目", "最小值", "典型值", "最大值", "单位", "说明"])
    assert classify_table(["编号", "项目", "项目", "单位", "最小值", "典型值", "最大值", "等级"])
    assert classify_table(["项目/名称", "项目/子项", "单位", "最小", "最大"])
    assert classify_table(["编号", "项目", "信号名称", "信号要求", "备注", "等级"])
    assert classify_table(["试验项目", "试验等级(%Ut)", "持续时间(s)", "性能判据"])


def test_classify_table_false_for_non_parameter_tables() -> None:
    """改动记录表、标准清单表、引脚表、信号表都不是参数表。"""
    assert not classify_table(["日期", "修订版本", "描述", "核 准"])
    assert not classify_table(["标准编号", "标准名称"])
    assert not classify_table(["引脚编号", "引脚名称", "功能描述", "类型"])
    assert not classify_table(["信号名称", "属性", "电平", "源/宿", "说明"])
    assert not classify_table(["项目", "单位", "备注"])  # 数值列只命中 0 个
    assert not classify_table(["项目", "最小值", "备注"])  # 只命中 1 个
    assert not classify_table([])


# --------------------------------------------------------------------------- #
# 5. 真实语料核对
# --------------------------------------------------------------------------- #

_REAL_PRODUCTS = {"CY0177-75V-20A", "GDC800T5054-UC", "PA601-D54A"}


@pytest.fixture(scope="module")
def real_params() -> list[SpecParameter]:
    """真实语料抽取结果 (模块级复用)。"""
    return extract_spec_parameters(DEFAULT_CORPUS_DIR)


@pytest.fixture(scope="module")
def real_standards() -> list[SpecStandard]:
    """真实语料标准引用结果 (模块级复用)。"""
    return extract_standards(DEFAULT_CORPUS_DIR)


def test_real_corpus_dir_exists() -> None:
    """三份规格书产物齐备。"""
    names = sorted(path.name for path in DEFAULT_CORPUS_DIR.glob("*.tables.json"))
    assert names == [
        "CY0177-75V-20A.tables.json",
        "GDC800T5054-UC.tables.json",
        "PA601-D54A 定制电源技术规格书.tables.json",
    ]


def test_real_three_products_all_have_params(real_params: list[SpecParameter]) -> None:
    """三个产品都有抽取结果 (实测总量 372)。"""
    counts = spec_param_stats(real_params)["by_product"]
    assert set(counts) == _REAL_PRODUCTS
    assert counts == {"CY0177-75V-20A": 50, "GDC800T5054-UC": 106, "PA601-D54A": 216}
    assert len(real_params) == 372


def test_real_stats_total_matches(real_params: list[SpecParameter]) -> None:
    """``spec_param_stats()['total']`` 与序列长度一致, 分项自洽。"""
    stats = spec_param_stats(real_params)
    assert stats["total"] == len(real_params)
    assert sum(stats["by_product"].values()) == stats["total"]
    assert sum(stats["by_category"].values()) == stats["total"]
    assert stats["multi_value"] == 6
    assert stats["unparsed"] == 4
    assert stats["flagged"] == stats["multi_value"] + stats["unparsed"] == 10


def test_real_categories_and_products_in_closed_sets(
    real_params: list[SpecParameter],
) -> None:
    """全部 category ∈ CATEGORIES, product_code ∈ 三个真实型号。"""
    for param in real_params:
        assert param.category in CATEGORIES, param
        assert param.product_code in _REAL_PRODUCTS, param
        assert set(param.flags) <= set(SPEC_PARAM_FLAGS), param


def test_real_pa601_nominal_input_voltage_range(
    real_params: list[SpecParameter],
) -> None:
    """PA601 ``标称输入电压范围``: Vac, 100/110/127 (首行)。"""
    hits = [
        param
        for param in real_params
        if param.product_code == "PA601-D54A" and param.name == "标称输入电压范围"
    ]
    assert hits, "PA601 缺少 标称输入电压范围"
    head = hits[0]
    assert head.unit == "Vac"
    assert (head.minimum, head.typical, head.maximum) == (100.0, 110.0, 127.0)
    raws = (head.minimum_raw, head.typical_raw, head.maximum_raw)
    assert raws == ("100", "110", "127")
    assert head.flags == ()
    assert head.table_id == "t0-13"


def test_real_cy0177_ac_input_voltage_range(real_params: list[SpecParameter]) -> None:
    """CY0177 ``交流输入电压范围``: Vac, 85/220/300。"""
    hits = [
        param
        for param in real_params
        if param.product_code == "CY0177-75V-20A" and param.name == "交流输入电压范围"
    ]
    assert len(hits) == 1
    param = hits[0]
    assert param.unit == "Vac"
    assert (param.minimum, param.typical, param.maximum) == (85.0, 220.0, 300.0)
    assert param.table_id == "t7-0"
    assert param.category == "input"


def test_real_gdc_working_voltage(real_params: list[SpecParameter]) -> None:
    """GDC ``工作电压``: V, 36/48/60。"""
    hits = [
        param
        for param in real_params
        if param.product_code == "GDC800T5054-UC" and param.name == "工作电压"
    ]
    assert len(hits) == 1
    param = hits[0]
    assert param.unit == "V"
    assert (param.minimum, param.typical, param.maximum) == (36.0, 48.0, 60.0)
    assert param.table_id == "t11-1"


def test_real_pa601_input_ovp_threshold_parses_305(
    real_params: list[SpecParameter],
) -> None:
    """PA601 ``t0-16`` 输入过压保护点: minimum=305 (不被多值 flag 破坏)。"""
    hits = [
        param
        for param in real_params
        if param.table_id == "t0-16" and param.row_index == 0
    ]
    assert len(hits) == 1
    param = hits[0]
    assert param.name == "输入过压保护点"
    assert param.minimum == 305.0
    assert param.minimum_raw == "305"
    assert "multi_value" not in param.flags
    assert param.category == "protection"


def test_real_gdc_multi_value_row(real_params: list[SpecParameter]) -> None:
    """GDC ``t14-1`` 保护点/恢复点/回差用 ``<br>`` 挤列: 多值 flag + raw 完整。"""
    hits = [param for param in real_params if param.table_id == "t14-1"]
    assert len(hits) == 2
    param = hits[0]
    assert param.category == "protection"
    assert "multi_value" in param.flags
    assert param.minimum is None and param.maximum is None
    assert param.minimum_raw == "29.5\n31.5\n2"
    assert param.maximum_raw == "33.5\n35.5\n4"
    assert "保护点" in param.name and "回差" in param.name


def test_real_products_with_multi_value_flags(real_params: list[SpecParameter]) -> None:
    """带 multi_value 标记的参数实例覆盖 GDC 与 PA601。"""
    flagged = [param for param in real_params if "multi_value" in param.flags]
    assert {param.product_code for param in flagged} == {"GDC800T5054-UC", "PA601-D54A"}
    assert len(flagged) == 6


def test_real_requirement_tables_and_scope(real_params: list[SpecParameter]) -> None:
    """要求型表入参: 远程信号表行数与表级作用域; 试验等级/持续时间拼入 requirement。"""
    by_table: dict[str, list[SpecParameter]] = {}
    for param in real_params:
        by_table.setdefault(param.table_id, []).append(param)
    assert len(by_table["t0-17"]) == 31
    assert len(by_table["t0-18"]) == 19
    assert len(by_table["t0-19"]) == 18
    assert {param.scope for param in by_table["t0-17"]} == {"遥信功能"}
    assert {param.scope for param in by_table["t0-18"]} == {"遥控功能"}
    assert {param.scope for param in by_table["t0-19"]} == {"遥测功能"}
    dips = [param for param in by_table["t0-24"] if param.name == "电压暂降"]
    assert dips, "t0-24 缺少 电压暂降"
    assert all(param.category == "emc" for param in dips)
    assert any("；" in param.requirement for param in dips)
    eff = [param for param in by_table["t7-0"] if param.name == "效率"]
    assert len(eff) == 2  # 语料修复: 原名占位 '-', 现为 效率
    assert {param.product_code for param in eff} == {"CY0177-75V-20A"}
    delay = [param for param in by_table["t14-0"] if param.name == "输出电压延迟时间"]
    assert len(delay) == 1  # 语料修复: 原 name 占位 '-', 现为 输出电压延迟时间 (PDF 跨页续行)
    assert delay[0].product_code == "GDC800T5054-UC"
    assert delay[0].branch == "N10V"
    assert delay[0].maximum_raw == "200" and delay[0].unit == "ms"


def test_real_every_product_has_standard(real_standards: list[SpecStandard]) -> None:
    """每个产品至少 1 条标准引用 (实测共 92 条)。"""
    by_product: dict[str, int] = {}
    for standard in real_standards:
        by_product[standard.product_code] = by_product.get(standard.product_code, 0) + 1
    assert set(by_product) == _REAL_PRODUCTS
    assert by_product == {"CY0177-75V-20A": 7, "GDC800T5054-UC": 13, "PA601-D54A": 72}
    assert len(real_standards) == 92


def test_real_standard_sources_and_shape(real_standards: list[SpecStandard]) -> None:
    """标准来源表命中实测清单; 每条标准号/名至少一个非空, standard_id 唯一。"""
    tables = {standard.table_id for standard in real_standards}
    assert {"t0-1", "t0-23", "t0-31", "t6-0", "t20-1", "t51-0"} == tables
    ids = [standard.standard_id for standard in real_standards]
    assert len(ids) == len(set(ids))
    for standard in real_standards:
        assert standard.code or standard.name
        assert standard.product_code in _REAL_PRODUCTS
        assert "标准号" != standard.code  # 表头行不落库


def test_real_pa601_standards_keep_names(real_standards: list[SpecStandard]) -> None:
    """PA601 ``t0-1`` 标准编号/标准名称成对保留。"""
    pairs = {
        standard.code: standard.name
        for standard in real_standards
        if standard.table_id == "t0-1"
    }
    assert pairs["GB 3873"] == "通信设备产品包装通用技术条件"
    assert pairs["GB/T 17626.2"] == "电磁兼容 试验和测量技术 静电放电抗扰度试验"
    assert pairs["IEC 62368-1"].startswith("Audio/video")


def test_real_cy0177_standard_keeps_full_text(
    real_standards: list[SpecStandard],
) -> None:
    """CY0177 ``t6-0`` 标准号列被展平成一列时, 全文保留 (含 <br> 续行)。"""
    cy = [
        standard
        for standard in real_standards
        if standard.product_code == "CY0177-75V-20A"
    ]
    codes = [standard.code for standard in cy]
    assert any(code.startswith("GB 50689-2011 通信局站防雷") for code in codes)
    assert any("EN 55022" in code and "EN 50116" in code for code in codes)
    assert all("标准号" != code for code in codes)


def test_spec_parameter_to_dict_round_trip(tmp_path: Path) -> None:
    """``to_dict`` 覆盖全部字段且 flags 为 list, 可直接 JSON 序列化。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [_table(["项目", "单位", "最小值", "典型值", "最大值"], [["电压", "V", "1", "2", "3"]])],
    )
    param = _only(tmp_path, "电压")
    data = param.to_dict()
    assert set(data) == {field for field in param.__dataclass_fields__}
    assert data["flags"] == []
    assert data["minimum"] == 1.0
    json.dumps(data, ensure_ascii=False)


def test_spec_standard_to_dict_round_trip() -> None:
    """``SpecStandard.to_dict`` 输出八个契约字段。"""
    standard = SpecStandard(
        standard_id="D|t|0|0",
        product_code="P",
        doc_id="D",
        section_path="1.2 参考标准",
        table_id="t",
        row_index=0,
        code="GB 4943",
        name="安全要求",
    )
    data = standard.to_dict()
    assert set(data) == {
        "standard_id", "product_code", "doc_id", "section_path",
        "table_id", "row_index", "code", "name",
    }
    assert data["code"] == "GB 4943"


# --------------------------------------------------------------------------- #
# 6. 确定性与 fail-fast
# --------------------------------------------------------------------------- #


def test_extraction_is_deterministic(real_params: list[SpecParameter]) -> None:
    """同一输入两次抽取结果逐位一致 (含 flags 与顺序)。"""
    again = extract_spec_parameters(DEFAULT_CORPUS_DIR)
    assert again == real_params
    again_dicts = [param.to_dict() for param in again]
    original = [param.to_dict() for param in real_params]
    assert again_dicts == original


def test_standards_extraction_is_deterministic(
    real_standards: list[SpecStandard],
) -> None:
    """标准引用同样可重复复现。"""
    assert extract_standards(DEFAULT_CORPUS_DIR) == real_standards


def test_fixture_extraction_is_deterministic(tmp_path: Path) -> None:
    """fixture 语料同样两次逐位一致 (含 flags 与顺序)。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["项目", "单位", "最小值", "典型值", "最大值"],
                [
                    ["电压", "V", "1", "2", "3"],
                    ["电流", "A", "/", "29.5<br>2", "40%/70%"],
                ],
            )
        ],
    )
    first = extract_spec_parameters(tmp_path)
    second = extract_spec_parameters(tmp_path)
    assert first == second
    assert [param.flags for param in first] == [(), ("multi_value",)]


def test_real_params_sorted_and_ids_unique(real_params: list[SpecParameter]) -> None:
    """排序稳定 (doc_id, table_id, row_index) 且 param_id 唯一。"""
    keys = [(param.doc_id, param.table_id, param.row_index) for param in real_params]
    assert keys == sorted(keys)
    ids = [param.param_id for param in real_params]
    assert len(ids) == len(set(ids))
    for param in real_params:
        assert param.param_id == f"{param.doc_id}|{param.table_id}|{param.row_index}"


def test_missing_corpus_dir_raises(tmp_path: Path) -> None:
    """语料目录不存在 → FileNotFoundError (不静默返回空)。"""
    with pytest.raises(FileNotFoundError):
        extract_spec_parameters(tmp_path / "not-here")
    with pytest.raises(FileNotFoundError):
        extract_standards(tmp_path / "not-here")


def test_empty_corpus_dir_raises(tmp_path: Path) -> None:
    """目录里没有 *.doc.json → FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        extract_spec_parameters(tmp_path)


def test_missing_tables_file_raises(tmp_path: Path) -> None:
    """doc.json 存在但 tables.json 缺失 → FileNotFoundError。"""
    (tmp_path / "TEST-001.doc.json").write_text(
        json.dumps({"doc_id": "TEST-001"}, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(FileNotFoundError):
        extract_spec_parameters(tmp_path)


def test_bad_json_raises_value_error(tmp_path: Path) -> None:
    """tables.json 不是 list → ValueError (不跳过整份文档)。"""
    _write_corpus(tmp_path, "TEST-001", [])
    (tmp_path / "TEST-001.tables.json").write_text("{not a list}", encoding="utf-8")
    with pytest.raises(ValueError):
        extract_spec_parameters(tmp_path)


def test_table_missing_headers_raises_value_error(tmp_path: Path) -> None:
    """表缺 headers 列表 → ValueError。"""
    _write_corpus(tmp_path, "TEST-001", [{"id": "t0-0", "rows": []}])
    with pytest.raises(ValueError):
        extract_spec_parameters(tmp_path)


def test_table_non_string_headers_raises_value_error(tmp_path: Path) -> None:
    """headers 含非字符串 → ValueError。"""
    _write_corpus(tmp_path, "TEST-001", [{"id": "t0-0", "headers": [1, 2], "rows": []}])
    with pytest.raises(ValueError):
        extract_spec_parameters(tmp_path)


def test_doc_missing_doc_id_raises_value_error(tmp_path: Path) -> None:
    """doc.json 缺 doc_id → ValueError。"""
    (tmp_path / "TEST-001.doc.json").write_text(json.dumps({}), encoding="utf-8")
    (tmp_path / "TEST-001.tables.json").write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(ValueError):
        extract_spec_parameters(tmp_path)


def test_standards_table_extraction_in_fixture(tmp_path: Path) -> None:
    """fixture 标准清单表: 标准号/名称成对抽取, 空行跳过, 表头行不落库。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["标准编号", "标准名称"],
                [
                    ["GB 4943.1", "安全要求"],
                    ["", ""],
                    ["IEC 62368-1", "Safety requirements"],
                ],
                table_id="t0-1",
                section_path="2 编写依据",
            )
        ],
    )
    standards = extract_standards(tmp_path)
    assert [(item.code, item.name) for item in standards] == [
        ("GB 4943.1", "安全要求"),
        ("IEC 62368-1", "Safety requirements"),
    ]
    assert all(item.section_path == "2 编写依据" for item in standards)


def test_parameter_table_not_mistaken_for_standards(tmp_path: Path) -> None:
    """``标准（或测试条件）`` 列的参数表不能被当成标准清单表。"""
    _write_corpus(
        tmp_path,
        "TEST-001",
        [
            _table(
                ["编号", "项目", "最小值", "最大值", "标准（或测试条件）", "等级"],
                [["SR-2500", "绝缘电压（输入对输出）", "4000", "4000", "承受1分钟", "强制"]],
            )
        ],
    )
    assert extract_standards(tmp_path) == []
    params = _extract_one(tmp_path)
    assert [param.name for param in params] == ["绝缘电压(输入对输出)"]
    assert params[0].minimum == 4000.0


def test_spec_param_stats_defaults_to_corpus(real_params: list[SpecParameter]) -> None:
    """``spec_param_stats()`` 缺省按 :data:`DEFAULT_CORPUS_DIR` 现抽。"""
    assert spec_param_stats()["total"] == len(real_params)


def test_module_constants_are_consistent() -> None:
    """模块常量与共享契约对齐。"""
    assert CATEGORY_VALUES == CATEGORIES
    assert "other" in CATEGORIES
    assert DEFAULT_CORPUS_DIR.name == "corpus"
