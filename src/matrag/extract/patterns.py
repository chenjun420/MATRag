"""名称/语境 → (kind, side) 映射与值解析辅助 —— P2 确定性抽取的第一层。

规则怎么来的
------------
不是凭空写的: 对三份真实语料 (PA601-D54A / CY0177-75V-20A / GDC800T5054-UC)
的全部 372 行参数逐行对照消费侧绑定表 (ATEStudio ``config/test_method_bindings.yaml``,
25 个 (kind,side) 对见 :mod:`matrag.extract.kinds`) 后冻结。抽取判定顺序:

    1. grade 含"不要求" → excluded          (spec_tables 负责)
    2. 非产测范围 → excluded                (spec_tables 负责)
    3. 本模块 match_kind 命中 → 进 requirements
    4. 未命中 → review (可审计降级, 不静默丢)

映射原则
--------
- **宁缺毋滥**: 命中必须有消费侧绑定支撑; 存疑一律 ``None`` 落 review。例如 GDC 的
  VIL/VIH/开关频率/调整率/检测精度 等接口电平与派生指标没有绑定方法, 不硬凑。
- **顺序敏感**: "输出电压延迟时间"是 timing 不是 output_voltage, "纹波电流"不是
  ripple, "输出电流检测精度"不是 output_current —— 规则按定义顺序短路。
- **绝不造数**: 数值只从 spec_params 已解析出的 min/typ/max 取; 解析失败即 None。
  多值行 (multi_value 保护表) 由 :func:`split_multi_value` 拆 保护点/恢复点/回差,
  拆不出退回普通值。
- **单位换算**只做口径对齐绑定表 (timing→ms, ripple→mVpp, efficiency→%,
  cap_load→uF, temperature→degC); 量纲不匹配时保持原文 (如 GDC 过冲幅度 mV 在
  timing kind 下不换)。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from matrag.domain.spec_params import SpecParameter

__all__ = [
    "MEASURE_KINDS",
    "TIMING_KEYS",
    "KindMatch",
    "ascending_limits",
    "baseline_input_voltage",
    "build_value",
    "extra_clauses",
    "format_number",
    "match_kind",
    "needs_numeric",
    "normalize_text",
    "normalize_unit_symbol",
    "note_input_conditions",
    "rail_of",
    "rep_value",
    "scenario_bindings",
    "split_multi_value",
]

#: 需要数值判据的 kind (消费侧 DRIVER_TABLE nature=measure 的集合)。
#: 这些 kind 的条件若 value 无 min/typ/max, verify_e2e 会报"无判据的测量步骤"。
MEASURE_KINDS: frozenset[str] = frozenset(
    {"output_voltage", "output_current", "output_power", "ripple", "efficiency", "timing"}
)

#: timing 判定关键词 (顺序敏感: 必须先于输出电压/电流通配)。
TIMING_KEYS: tuple[str, ...] = (
    "过冲",
    "恢复时间",
    "上升时间",
    "延迟",
    "开机时间",
    "启动时间",
    "掉电延",
    "动态响应",
)

#: 单位符号换算表: (kind, 归一后单位) → (数值系数, 目标单位)。
_UNIT_SCALES: dict[tuple[str, str], tuple[float, str]] = {
    ("timing", "us"): (0.001, "ms"),
    ("timing", "s"): (1000.0, "ms"),
    ("timing", "ms"): (1.0, "ms"),
    ("ripple", "mV"): (1.0, "mVpp"),
    ("ripple", "mVpp"): (1.0, "mVpp"),
    ("ripple", "V"): (1000.0, "mVpp"),
    ("efficiency", ""): (1.0, "%"),
    ("efficiency", "%"): (1.0, "%"),
    ("cap_load", "uF"): (1.0, "uF"),
    ("temperature", "degC"): (1.0, "degC"),
}

_FULLWIDTH = str.maketrans({"（": "(", "）": ")", "：": ":", "／": "/", "％": "%", "\u3000": " "})
_RAIL_TOKEN = re.compile(r"^[Nn]?[+-]?\d+(?:\.\d+)?[Vv]\d*$")
_RAIL_NOTE = re.compile(r"(\d+(?:\.\d+)?)\s*[Vv]\s*输出")
_TIER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*[Vv](ac|dc|AC|DC)?(?![a-zA-Z])")
_LOAD_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%[^\n。;；,，]{0,8}负载")
_HZ = re.compile(r"(?<![A-Za-z])(\d+(?:\.\d+)?)\s*Hz")
_TA_TB = re.compile(r"(?:TA|TB)\s*=\s*(-?\d+(?:\.\d+)?)\s*°C")


@dataclass(frozen=True)
class KindMatch:
    """一次成功的名称→kind 命中 (rule 记录命中依据, 供留痕/调试)。"""

    kind: str
    side: str
    rule: str


def normalize_text(text: str | None) -> str:
    """全角转半角、空白折叠 —— 仅用于名称/语境匹配。"""
    return re.sub(r"\s+", " ", (text or "").translate(_FULLWIDTH)).strip()


def normalize_unit_symbol(unit: str | None) -> str:
    """单位符号归一 (μ→u, ℃/°C→degC, mS→ms, S→s)。"""
    u = (unit or "").strip().replace("μ", "u").replace("µ", "u")
    u = u.replace("℃", "degC").replace("°C", "degC")
    u = u.replace("mS", "ms")
    if u == "S":
        u = "s"
    return u


def needs_numeric(kind: str) -> bool:
    """该 kind 的条件是否必须携带数值判据。"""
    return kind in MEASURE_KINDS


def match_kind(param: SpecParameter) -> KindMatch | None:
    """把一行规格参数映射到 (kind, side); 未命中返回 None (调用方落 review)。

    :param param: spec_params 抽取出的一行 (name/scope/category 参与判定)。
    """
    n = normalize_text(param.name)
    scope = normalize_text(param.scope)
    cat = param.category or ""

    def hit(kind: str, side: str, rule: str) -> KindMatch:
        return KindMatch(kind, side, rule)

    # 0) 先验否决: 名称相近但词表无对应概念的, 直接落 review, 防止下游通配误伤。
    if "纹波电流" in n or "相关性" in n:
        return None
    # 1) 监控三表按 scope 短路 (PA601 4.3.4.1~3)。
    if "遥信" in scope:
        return hit("signal_state", "output", "scope=遥信功能")
    if "遥控" in scope:
        return hit("command", "input", "scope=遥控功能")
    if "遥测" in scope:
        return hit("telemetry_value", "output", "scope=遥测功能")
    # 2) timing 家族 —— 必须先于输出电压/电流通配 ("输出电压延迟时间"含"输出电压")。
    if any(k in n for k in TIMING_KEYS):
        return hit("timing", "output", "timing关键词")
    # 3) 输出电压家族 (排除 检测精度/纹波噪声 误伤)。
    if ("输出电压" in n or "整定电压" in n) and not any(k in n for k in ("检测", "纹波", "杂音")):
        return hit("output_voltage", "output", "输出电压family")
    # 4) 输入电流 = 输入侧读数 (output_current 的 input side)。
    if "输入电流" in n and "检测" not in n:
        return hit("output_current", "input", "输入电流读数")
    # 5) 输出电流家族 (纹波电流已在先验否决)。
    if ("输出电流" in n and "检测" not in n) or (cat == "output" and "电流" in n):
        return hit("output_current", "output", "输出电流family")
    # 6) 输出功率家族 (功耗≠功率)。
    if "输出功率" in n or (cat == "output" and "功率" in n and "功耗" not in n):
        return hit("output_power", "output", "输出功率family")
    # 7) 效率 (CY 的效率行落在 cat=input, 故名称优先)。
    if "效率" in n or cat == "efficiency":
        return hit("efficiency", "output", "效率")
    # 8) 纹波/杂音 (含"噪声+纹波")。
    if "杂音" in n or "纹波" in n:
        return hit("ripple", "output", "纹波杂音")
    # 9) 容性负载 (GDC 输入侧的容性负载无绑定, 排除)。
    if "容性负载" in n and cat != "input":
        return hit("cap_load", "output", "容性负载")
    # 10) ORING/热插拔能力核验。
    if "ORING" in n.upper() or "热插拔" in n:
        return hit("presence", "output", "ORING/热插拔")
    # 11) 功率因数 (输入侧 kind)。
    if "功率因数" in n:
        return hit("power_factor", "input", "功率因数")
    # 12) 输入电压家族 (排除 频率, 它归 rule 13)。
    if "输入" in n and "电压" in n and not any(k in n for k in ("保护", "检测", "遥测", "告警", "精度", "频率")):
        return hit("input_voltage", "input", "输入电压family")
    if n == "工作电压":
        return hit("input_voltage", "input", "工作电压")
    # 13) 输入频率 (开关频率不含 交流/输入, 自然落 review)。
    if "频率" in n and ("交流" in n or "输入" in n):
        return hit("input_frequency", "input", "输入频率")
    # 14) 输入冲击电流。
    if "冲击电流" in n:
        return hit("power_event", "input", "冲击电流")
    # 15) 保护表整体承载判据 (过压/欠压/过流/过温/短路/反接...)。
    if cat == "protection":
        return hit("protection_action", "output", "保护表")
    return None


def _to_float(text: str | None) -> float | None:
    """原文数值 → float; '-', '/', '2%Vout' 等非纯数值返回 None (不造数)。"""
    t = (text or "").strip().replace("%", "")
    if not t or t in {"-", "/", "—", "–"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _scale_for(kind: str, unit: str | None) -> tuple[float, str]:
    """返回 (数值系数, 目标单位)。未登记的组合按归一后原样返回。"""
    u = normalize_unit_symbol(unit)
    return _UNIT_SCALES.get((kind, u), (1.0, u))


def _round(x: float) -> float:
    return round(x, 6)


def _has_newline(param: SpecParameter) -> bool:
    return "\n" in (param.minimum_raw or "") or "\n" in (param.typical_raw or "") or "\n" in (param.maximum_raw or "")


def ascending_limits(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """把负轨条件的 min/max 归一为数学升序 (min ≤ max); typ/unit 原样保留。

    GAP-01 修复: 规格书作者对**负轨**常按"幅值"书写区间 —— 如 -54V 轨写
    `最小值 -53.2 / 最大值 -54.8`, 解析后 ``min(-53.2) > max(-54.8)``, 有符号
    语义下区间被判反。消费侧 (ATEStudio ``_judge``) 假定 ``下限 ≤ 上限``, 反向
    会让 -54V 合格读数 (-54.0014) 被判超差。

    归一口径: 仅当 min/max 均为数值、**两侧同为负**(整个区间落在负半轴)、且
    ``min > max`` 时交换二者。正区间的可疑反向行 (如保护表的 `5.0 ~ 0.0`, 多为
    解析占位) 不在"负轨"范畴内, 一律不动, 避免误改。中间值 typ 不参与区间判定,
    保持原位不重排。

    原始幅值序仍可从 :class:`~matrag.domain.spec_params.SpecParameter` 的
    ``minimum_raw``/``maximum_raw`` 反查 (留痕, 不静默改写来源)。

    :param value: 条件数值 dict (可含 min/typ/max/unit); None 或非负区间原样返回。
    :return: 归一后的 dict (无改动时返回同一对象)。
    """
    if not value:
        return value
    lo = value.get("min")
    hi = value.get("max")
    if (
        isinstance(lo, (int, float))
        and not isinstance(lo, bool)
        and isinstance(hi, (int, float))
        and not isinstance(hi, bool)
        and lo < 0
        and hi < 0
        and lo > hi
    ):
        fixed = dict(value)
        fixed["min"], fixed["max"] = hi, lo
        return fixed
    return value


def build_value(param: SpecParameter, kind: str) -> dict[str, Any] | None:
    """从已解析的 min/typ/max 构造 clause.value; 无数值返回 None。

    多值行 (原始值含换行) 返回 None, 交由 :func:`split_multi_value` 拆分。
    负轨区间经 :func:`ascending_limits` 归一为数学升序 (GAP-01)。
    """
    if _has_newline(param):
        return None
    factor, unit = _scale_for(kind, param.unit)
    out: dict[str, Any] = {}
    for key, field in (("min", "minimum"), ("typ", "typical"), ("max", "maximum")):
        num = getattr(param, field)
        if num is None:
            continue
        out[key] = _round(float(num) * factor)
    if not out:
        return None
    if unit:
        out["unit"] = unit
    return ascending_limits(out)


def split_multi_value(param: SpecParameter, kind: str) -> list[tuple[str, dict[str, Any] | None]] | None:
    """拆分 multi_value 行 (如保护表的 保护点/恢复点/回差)。

    返回 [(标签, value|None), ...]; 无法对齐 (行数不一致/单行) 返回 None。
    标签取名称第 2 行起; 单位按 ``<br>`` 逐段对齐。
    """
    raws = {
        "min": param.minimum_raw or "",
        "typ": param.typical_raw or "",
        "max": param.maximum_raw or "",
    }
    cols = {k: [ln.strip() for ln in v.split("\n")] for k, v in raws.items() if v.strip()}
    n = max((len(c) for c in cols.values()), default=0)
    if n < 2:
        return None
    labels = [ln.strip() for ln in (param.name or "").split("\n") if ln.strip()][1:]
    units = [u.strip() for u in (param.unit or "").split("<br>")] if param.unit else []
    if len(labels) != n:
        labels = [f"第{i + 1}点" for i in range(n)]
    out: list[tuple[str, dict[str, Any] | None]] = []
    for i in range(n):
        value: dict[str, Any] = {}
        for key in ("min", "typ", "max"):
            col = cols.get(key, [])
            if i < len(col):
                num = _to_float(col[i])
                if num is not None:
                    value[key] = _round(num)
        u = normalize_unit_symbol(units[i]) if i < len(units) else ""
        if value and u:
            value["unit"] = u
        out.append((labels[i], ascending_limits(value) if value else None))
    return out


def rep_value(value: dict[str, Any] | None) -> float | None:
    """变体代表数值: max 优先, 否则 min (设计冻结口径, 用于 -m{数值} 后缀)。"""
    if not value:
        return None
    for key in ("max", "min"):
        num = value.get(key)
        if isinstance(num, (int, float)):
            return float(num)
    return None


def format_number(x: float) -> str:
    """数值格式化: 去尾零 (%g 语义)。"""
    return f"{x:g}"


def rail_of(param: SpecParameter) -> str:
    """识别支路名: sub → branch → 原文"XXV 输出" (CY 无支路列的场景)。"""
    for raw in (param.sub_name, param.branch):
        token = (raw or "").split("(")[0].split("\n")[0].strip().replace(" ", "")
        if _RAIL_TOKEN.match(token):
            return token
    m = _RAIL_NOTE.search(param.note or "")
    if m:
        return f"{m.group(1)}V"
    return ""


def _find_tier(text: str) -> tuple[float, str, str] | None:
    """在文本中找第一个输入电压档位; 排除 "48V 输出/电池" 等输出侧指代。"""
    for m in _TIER.finditer(text):
        tail = text[m.end() : m.end() + 8]
        if "输出" in tail or "电池" in tail:
            continue
        suffix = (m.group(2) or "").lower()
        unit = {"ac": "Vac", "dc": "Vdc"}.get(suffix, "V")
        num = _to_float(m.group(1))
        if num is None:
            continue
        return num, unit, m.group(0).strip()
    return None


def scenario_bindings(param: SpecParameter) -> dict[str, str]:
    """从名称/备注/条件中提取工况档位词 (input_tier / load), 供 scenario.bindings。"""
    text = " ".join(x for x in (param.name, param.note, param.condition) if x)
    out: dict[str, str] = {}
    tier = _find_tier(text)
    if tier is not None:
        out["input_tier"] = f"{format_number(tier[0])}{tier[1]}"
    m = _LOAD_PCT.search(text)
    if m:
        out["load"] = f"{m.group(1)}%"
    else:
        for kw in ("满载", "半载", "空载"):
            if kw in text:
                out["load"] = kw
                break
    return out


def note_input_conditions(
    param: SpecParameter, baseline: dict[str, Any] | None
) -> list[tuple[str, str, dict[str, Any] | None, str]]:
    """从备注/测试条件派生输入侧条件提案: [(kind, side, value, 留痕文本), ...]。

    - 电压档位: 原文明确数值 (如 "额定220Vac输入" → typ 220Vac);
    - "额定输入"无数字 → 产品基线 (baseline, 由 pipeline 传入);
    - 频率/温度: 仅接受明确的数字写法 (50Hz / TA=25°C)。
    """
    text = " ".join(x for x in (param.note, param.condition) if x)
    out: list[tuple[str, str, dict[str, Any] | None, str]] = []
    tier = _find_tier(text)
    if tier is not None:
        out.append(("input_voltage", "input", {"typ": _round(tier[0]), "unit": tier[1]}, f"原文标注 {tier[2]}"))
    elif baseline is not None and ("额定输入" in text or "标称输入" in text):
        out.append(("input_voltage", "input", dict(baseline), "产品基线(额定输入无数字)"))
    m = _HZ.search(text)
    if m:
        num = _to_float(m.group(1))
        if num is not None:
            out.append(("input_frequency", "input", {"typ": num, "unit": "Hz"}, f"原文标注 {m.group(0).strip()}"))
    m = _TA_TB.search(text)
    if m:
        num = _to_float(m.group(1))
        if num is not None:
            out.append(("temperature", "input", {"typ": num, "unit": "degC"}, f"原文标注 {m.group(0).strip()}"))
    return out


def baseline_input_voltage(params: Sequence[SpecParameter]) -> dict[str, Any] | None:
    """产品输入电压基线: min/max 取工作范围, typ 取最大标称档 (设计冻结口径)。"""
    work: SpecParameter | None = None
    for p in params:
        n = normalize_text(p.name)
        is_work_row = "输入工作电压范围" in n or "交流输入电压范围" in n or n == "工作电压"
        if p.category == "input" and is_work_row and p.minimum is not None:
            work = p
            break
    if work is None:
        return None
    tiers: list[float] = []
    for p in params:
        n = normalize_text(p.name)
        if p.category == "input" and ("标称输入电压范围" in n or "额定输入电压范围" in n) and p.typical is not None:
            tiers.append(float(p.typical))
    value: dict[str, Any] = {"min": float(work.minimum)}
    typ = max(tiers) if tiers else (float(work.typical) if work.typical is not None else None)
    if typ is not None:
        value["typ"] = typ
    if work.maximum is not None:
        value["max"] = float(work.maximum)
    value["unit"] = normalize_unit_symbol(work.unit) or "Vac"
    return ascending_limits(value)


def extra_clauses(param: SpecParameter) -> list[tuple[str, str, dict[str, Any] | None]]:
    """保护行的附加动作: 行文含"短路" → fault_stimulus(input, 消费侧 destructive 段自动隔离)。

    仅当 kind 命中 protection_action 时由调用方追加。
    """
    text = normalize_text(param.name) + " " + (param.note or "")
    if "短路" in text:
        return [("fault_stimulus", "input", {"event": "短路"})]
    return []
