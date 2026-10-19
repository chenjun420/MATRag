"""领域共享词汇与归一化 (知识图谱各模块之间的最小契约)。

本模块只做三件事, 全部是纯常量/纯函数, 不做 IO、不依赖第三方库:

1. :data:`CATEGORIES` / :data:`CATEGORY_LABELS` —— 参数与术语的**类别闭集**
   (对齐业界开关电源规格书的章节习惯: 输入特性/输出特性/保护特性/效率特性/
   动态特性/环境/EMC/安规/可靠性/通信/机械/热设计/标准符合性/通用/其他)。
   规格书章节归类、术语归类、公式归类一律从这里取值, 避免各模块各造一套字符串。
2. :func:`normalize_name` —— 参数名/术语名的最小归一 (NFKC 全角转半角、
   连接符统一、去全部空白、小写), 供"规格书实例名 ↔ 术语别名"匹配使用。
   注意: 它故意**不**做任何语义改写 (如去掉"范围"两字), 避免过度归并。
3. :data:`EMPTY_TOKENS` / :func:`is_empty_value` —— 规格书表格中表示
   "未给出/不适用" 的占位符 (``/``、``-``、``–``、``—``、``N/A``、``无`` 等)。
   抽取层必须把它们与真实值区分开, **不得当成 0 或静默丢弃**。

:func:`normalize_unit` 只做单位串的最小归一 (``℃``→``°C``、``µ/μ``→``u``、
空白压缩), **不改大小写** (``mΩ`` 与 ``MΩ`` 语义完全不同)。
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "CATEGORIES",
    "CATEGORY_LABELS",
    "CATEGORY_COMMUNICATION",
    "CATEGORY_DYNAMICS",
    "CATEGORY_EFFICIENCY",
    "CATEGORY_EMC",
    "CATEGORY_ENVIRONMENT",
    "CATEGORY_GENERAL",
    "CATEGORY_INPUT",
    "CATEGORY_MECHANICAL",
    "CATEGORY_OTHER",
    "CATEGORY_OUTPUT",
    "CATEGORY_PROTECTION",
    "CATEGORY_RELIABILITY",
    "CATEGORY_SAFETY",
    "CATEGORY_STANDARDS",
    "CATEGORY_THERMAL",
    "EMPTY_TOKENS",
    "is_empty_value",
    "normalize_name",
    "normalize_unit",
]

# --------------------------------------------------------------------------- #
# 类别闭集
# --------------------------------------------------------------------------- #

CATEGORY_INPUT = "input"                    # 输入特性 (输入电压/电流/频率/浪涌)
CATEGORY_OUTPUT = "output"                  # 输出特性 (电压/电流/功率/纹波/调整率)
CATEGORY_PROTECTION = "protection"          # 保护特性 (OVP/OCP/OTP/SCP/UVLO/回差)
CATEGORY_EFFICIENCY = "efficiency"          # 效率特性 (满载/半载/空载)
CATEGORY_DYNAMICS = "dynamics"              # 动态特性 (瞬态响应/启动/保持时间)
CATEGORY_ENVIRONMENT = "environment"        # 环境条件 (温度/湿度/海拔/存储)
CATEGORY_EMC = "emc"                        # 电磁兼容 (CE/RE/ESD/EFT/Surge)
CATEGORY_SAFETY = "safety"                  # 安规 (耐压/绝缘/接地/接触电流)
CATEGORY_RELIABILITY = "reliability"        # 可靠性 (MTBF/MTTR/寿命/可用度)
CATEGORY_COMMUNICATION = "communication"    # 通信与监控 (PMBus/遥测遥信遥控)
CATEGORY_MECHANICAL = "mechanical"          # 机械结构 (尺寸/端子/引脚/重量)
CATEGORY_THERMAL = "thermal"                # 热设计 (热阻/温升/降额曲线)
CATEGORY_STANDARDS = "standards"            # 标准符合性 (引用标准/认证)
CATEGORY_GENERAL = "general"                # 通用基础 (电学/单位/基础公式)
CATEGORY_OTHER = "other"                    # 其他 (未能归类的兜底, 保留可见)

#: 类别闭集 (顺序固定, 供稳定遍历/导出)。
CATEGORIES: tuple[str, ...] = (
    CATEGORY_INPUT,
    CATEGORY_OUTPUT,
    CATEGORY_PROTECTION,
    CATEGORY_EFFICIENCY,
    CATEGORY_DYNAMICS,
    CATEGORY_ENVIRONMENT,
    CATEGORY_EMC,
    CATEGORY_SAFETY,
    CATEGORY_RELIABILITY,
    CATEGORY_COMMUNICATION,
    CATEGORY_MECHANICAL,
    CATEGORY_THERMAL,
    CATEGORY_STANDARDS,
    CATEGORY_GENERAL,
    CATEGORY_OTHER,
)

#: 类别的中文标签 (报告/导出用)。
CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_INPUT: "输入特性",
    CATEGORY_OUTPUT: "输出特性",
    CATEGORY_PROTECTION: "保护特性",
    CATEGORY_EFFICIENCY: "效率特性",
    CATEGORY_DYNAMICS: "动态特性",
    CATEGORY_ENVIRONMENT: "环境条件",
    CATEGORY_EMC: "电磁兼容",
    CATEGORY_SAFETY: "安规",
    CATEGORY_RELIABILITY: "可靠性",
    CATEGORY_COMMUNICATION: "通信与监控",
    CATEGORY_MECHANICAL: "机械结构",
    CATEGORY_THERMAL: "热设计",
    CATEGORY_STANDARDS: "标准符合性",
    CATEGORY_GENERAL: "通用基础",
    CATEGORY_OTHER: "其他",
}

#: 归一化后仍表示"未给出"的占位符集合 (与 :func:`normalize_name` 同口径)。
EMPTY_TOKENS: frozenset[str] = frozenset(
    {"", "/", "-", "--", "n/a", "na", "null", "none", "无", "不适用", "待定"}
)

_DASHES = {"\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2212"}
_DASH_RE = re.compile("[" + "".join(_DASHES) + "]")
_WS_RE = re.compile(r"\s+")


def normalize_name(text: object) -> str:
    """参数名/术语名的最小归一: NFKC → 连接符统一 → 去空白 → 小写。

    只做形变归一, 不做语义改写:
    ``"  输出 电压 "`` 与 ``"输出电压"`` 归一后相同;
    ``"Hold-Up Time"`` 与 ``"hold up time"`` 归一后相同;
    但 ``"输出电压范围"`` 与 ``"输出电压"`` 仍然不同 (交由匹配层按别名处理)。
    """
    if text is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(text))
    normalized = _DASH_RE.sub("-", normalized)
    normalized = _WS_RE.sub("", normalized)
    return normalized.strip().lower()


def normalize_unit(unit: object) -> str:
    """单位串的最小归一: NFKC、``℃``→``°C``、``µ/μ``→``u``、空白压缩。

    **不改大小写** —— ``mΩ`` 与 ``MΩ``、``ms`` 与 ``Ms`` 语义不同, 原样保留。
    """
    if unit is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(unit))
    normalized = normalized.replace("℃", "°C")
    normalized = normalized.replace("µ", "u").replace("μ", "u")
    normalized = _WS_RE.sub(" ", normalized)
    return normalized.strip()


def is_empty_value(raw: object) -> bool:
    """判断单元格是否表示"未给出" (占位符/空白/None)。

    ``"/"``、``"-"``、``"–"``、``"—"``、``"N/A"``、``"无"``、``""`` 一律为 True;
    ``"0"``、``"≥5"``、``"abc"`` 为 False (它们是真实内容, 由抽取层继续解析)。
    """
    if raw is None:
        return True
    normalized = unicodedata.normalize("NFKC", str(raw))
    normalized = _DASH_RE.sub("-", normalized)
    normalized = _WS_RE.sub("", normalized).strip().lower()
    return normalized in EMPTY_TOKENS
