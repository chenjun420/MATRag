"""产品参数抽取层: 规格书参数表 → 结构化参数实例 / 标准引用。

本模块把真实规格书解析出的表格产物 (``data/corpus/*.tables.json`` 与 ``*.doc.json``)
抽成"产品层"结构化实例, 供知识图谱组装层与本体抽取优化使用:

- :class:`SpecParameter` —— 一条参数实例 (名称/支路/单位/最小-典型-最大/要求/备注/条件);
- :class:`SpecStandard` —— 一条标准引用 (标准号 + 标准名);
- :func:`classify_section` —— 章节路径 → 类别 (取值来自 :data:`matrag.domain.vocab.CATEGORIES`);
- :func:`classify_table` —— 表头是否参数表;
- :func:`extract_spec_parameters` / :func:`extract_standards` —— 全语料批量抽取;
- :func:`spec_param_stats` —— 抽取结果统计。

设计要点 (fail-fast, 不静默降级):

1. **表头驱动, 不按表 id 分支**: 表头经 :func:`normalize_name` 归一后匹配别名表,
   多余列忽略但**不位移**; 行长不足补空, 超长忽略多余尾列。
2. **不猜数值**: 单元格含 ``<br>`` 多值或范围 (``45~65``、``110/220``) 时只保留 raw
   文本, 数值置 ``None`` 并打 ``multi_value`` 标记; 解析失败打 ``unparsed``。
   占位符 (``/``、``-``) 由 :func:`is_empty_value` 判定, 保留 raw、数值 ``None``,
   **不算** unparsed。
3. **要求型表同样入参数层**: 表头含 ``name`` 且带 ``requirement``/``condition`` 列
   (信号要求/试验等级/持续时间/检测范围/精度/标准(或测试条件)…) 的表按参数抽取;
   同名字段的多列文本按「；」拼接 (试验等级+持续时间不丢信息), 不拆数值。
4. **确定性**: 结果按 ``(doc_id, table_id, row_index)`` 稳定排序, 同一输入两次运行
   逐位一致。
5. **契约错误直接抛**: ``*.tables.json`` 缺失 → ``FileNotFoundError``;
   JSON 结构非法 (不是 list / 表缺 headers / headers 非字符串列表) → ``ValueError``。
"""

from __future__ import annotations

import html
import json
import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from matrag.domain.vocab import (
    CATEGORIES,
    is_empty_value,
    normalize_name,
    normalize_unit,
)

__all__ = [
    "CATEGORY_VALUES",
    "DEFAULT_CORPUS_DIR",
    "SPEC_PARAM_FLAGS",
    "SpecParameter",
    "SpecStandard",
    "classify_section",
    "classify_table",
    "extract_spec_parameters",
    "extract_standards",
    "spec_param_stats",
]

#: 默认语料目录 (项目根 ``data/corpus``)。
DEFAULT_CORPUS_DIR = Path(__file__).resolve().parents[3] / "data" / "corpus"

#: 参数实例上可能出现的标记闭集 (顺序固定)。
SPEC_PARAM_FLAGS: tuple[str, ...] = ("multi_value", "unparsed", "no_name")

#: 类别闭集 (来自共享契约; 抽取层不另造类别字符串)。
CATEGORY_VALUES: tuple[str, ...] = CATEGORIES

#: 兜底类别 (章节路径未命中关键词时使用)。
_CATEGORY_FALLBACK = "other"

# --------------------------------------------------------------------------- #
# 表头别名词典 (归一后匹配; 数据表驱动)
# --------------------------------------------------------------------------- #

#: 归一表头 → 参数实例字段名 (顺序敏感)。
_PARAM_HEADER_ALIASES: tuple[tuple[str, str], ...] = (
    ("项目/子项", "sub_name"),
    ("信号名称", "sub_name"),
    ("项目(子项)", "branch"),
    ("项目(大类)", "sub_name"),
    ("项目/项目分类", "sub_name"),
    ("子项", "sub_name"),
    ("项目/名称", "name"),
    ("项目/测试项目", "name"),
    ("项目名称", "name"),
    ("参数名称", "name"),
    ("名称", "name"),
    ("项目", "name"),
    ("试验项目", "name"),
    ("遥测量", "name"),
    ("指标", "name"),
    ("输出支路", "branch"),
    ("支路", "branch"),
    ("单位", "unit"),
    ("最小值", "minimum"),
    ("最小", "minimum"),
    ("典型值", "typical"),
    ("典型", "typical"),
    ("标准值", "typical"),
    ("量值", "typical"),
    ("最大值", "maximum"),
    ("最大", "maximum"),
    ("备注", "note"),
    ("说明", "note"),
    ("测试条件", "condition"),
    ("标准(或测试条件)", "condition"),
    ("试验条件", "condition"),
    ("评估条件", "condition"),
    ("条件", "condition"),
    ("要求", "requirement"),
    ("信号要求", "requirement"),
    ("指标要求", "requirement"),
    ("参考标准/试验参数", "requirement"),
    ("参数", "requirement"),
    ("检测范围", "requirement"),
    ("精度", "requirement"),
    ("试验等级(%ut)", "requirement"),
    ("持续时间(s)", "requirement"),
    ("编号", "code"),
    ("等级", "grade"),
    ("性能判据", "grade"),
    ("性能判/据", "grade"),
)

#: 标准引用行的组织/前缀白名单 (内容启发式, 用于无标准名称列的清单表)。
#: 只收"真实规格书里出现过的标准组织/前缀", 避免把普通编号 (如 ``SR-PA601-...``) 当标准。
_STANDARD_PREFIXES: frozenset[str] = frozenset(
    {
        "gb", "gb/t", "gb/z", "iec", "en", "ul", "iso", "yd", "yd/t",
        "sj/t", "gjb", "q", "q/zx", "zx", "ipc", "jb", "jis", "csa",
        "ansi", "itu", "etsi", "cispr",
    }
)

#: 标准清单表内部重复表头行 (被解析成数据行时) 的识别词。
_STANDARD_HEADER_TERMS: frozenset[str] = frozenset(
    {"标准号", "标准编号", "标准名称", "名称", "标准", "安规标准", "安规标准内容"}
)

#: 标准清单表的"标准代码列"表头闭集 (归一后精确匹配)。
_STANDARD_KEY_COLUMNS: frozenset[str] = frozenset(
    {"标准号", "标准编号", "安规标准", "标准代码", "标准"}
)

#: 标准清单表的"标准名称列"表头闭集 (归一后精确匹配)。
_STANDARD_NAME_COLUMNS: frozenset[str] = frozenset(
    {"标准名称", "标准内容", "安规标准内容", "名称"}
)

#: 标准清单表识别用的列名并集。
_STANDARD_COLUMN_LABELS: frozenset[str] = (
    _STANDARD_KEY_COLUMNS | _STANDARD_NAME_COLUMNS
)

#: 内容启发式: 判定列属性所需的最少行数。
_MIN_CODE_SAMPLES = 2

#: 内容启发式: 判定列属性时的抽样行数上限。
_CODE_SAMPLE_LIMIT = 5

#: 内容启发式: 列内"像标准代码的行"占比下限。
_CODE_LIKE_MIN_RATIO = 0.5

#: 续行 (被 ``<br>`` 折断的标准号文本) 的最短长度, 更短的孤立行按噪声丢弃。
_MIN_CONTINUATION_LEN = 8

#: 章节路径关键词 → 类别 (顺序固定: 更具体/更靠前的关键词优先)。
_SECTION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("input", ("输入",)),
    ("output", ("输出",)),
    ("protection", ("保护",)),
    ("efficiency", ("效率",)),
    ("dynamics", ("动态", "瞬态", "启动", "保持")),
    ("environment", ("环境", "温度", "湿度", "海拔", "储存", "存储")),
    ("emc", ("EMC", "电磁")),
    ("safety", ("安规", "绝缘", "耐压", "接地", "接触电流")),
    ("reliability", ("可靠", "MTBF", "寿命", "返修")),
    ("communication", ("通信", "遥测", "遥信", "遥控", "PMBus")),
    ("mechanical", ("机械", "结构", "引脚", "尺寸", "标签")),
    ("thermal", ("热", "散热", "温升")),
    ("standards", ("标准", "认证")),
)

#: 参数表判定: 数值列命中数下限。
_NUMERIC_MIN_HITS = 2
_NUMERIC_KEYS: tuple[str, ...] = ("minimum", "typical", "maximum")

# --------------------------------------------------------------------------- #
# 正则
# --------------------------------------------------------------------------- #

#: HTML 标签 (``<br>`` / ``<br/>`` / ``<b>`` 等)。
_TAG_RE = re.compile(r"<[^<>]{0,64}>")

#: 换行标签 (多值单元格的典型标志)。
_BR_RE = re.compile(r"<\s*br\s*/?\s*>", flags=re.IGNORECASE)

#: 数值首尾的近似/量级装饰符 (``~5``、``5~``), 解析时剥离。
_EDGE_DECOR_RE = re.compile("^[~\u223c\u301c\uff5e]+|[~\u223c\u301c\uff5e]+$")

#: 纯数值 (整数/小数/科学计数, 允许前导 ``+``/``-``), **全串**必须匹配。
_PURE_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")

#: 多值范围: 两个数字 (允许中间夹单字符量级/百分号) 被 ``/ ~ ～ -`` 分隔,
#: 例如 ``45~65``、``110/220``、``40%/70%``、``16/40μs``。
_MULTI_RANGE_RE = re.compile(
    r"\d\s*[%/kKmM]?\s*[/~\u223c\u301c\uff5e-]\s*[%/kKmM]?\s*\d"
)

#: 行内注释性前缀 (如 ``1. ``、``2）``), 数值单元格清洗时剥离。
_LIST_PREFIX_RE = re.compile(r"^\s*\d+\s*[.、)）]\s*")

#: 章节编号前缀 (如 ``4.3.4.1 ``), 要求型表作用域取叶名时剥离。
_SECTION_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)*[\s.、]*")

#: 空白 (含全角空格) 压缩。
_WS_RE = re.compile(r"[\s\u3000]+")

#: 行首 ASCII 代码片段 (含数字/点/斜线/连字符)。
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9./-]*")

#: CJK 字符 (用于识别"纯中文短句")。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# --------------------------------------------------------------------------- #
# 文本归一 / 数值解析 工具
# --------------------------------------------------------------------------- #


def _clean_text(raw: object) -> str:
    """单元格/表头清洗: NFKC → 破折号统一 → HTML 实体与标签 → 空白压缩。

    ``<br>`` 转成 ``\\n`` 后再压缩空白, 多值单元格的每行在文本列里保持可读;
    数值列由 :func:`_is_multi_value` 见过原始 ``<br>`` 后按"多值"处理。

    **不做**任何语义改写; 文本列与数值列共用同一清洗口径。
    """
    if raw is None:
        return ""
    text = unicodedata.normalize("NFKC", str(raw))
    text = re.sub("[\u2010\u2011\u2012\u2013\u2014\u2212]", "-", text)
    text = _BR_RE.sub("\n", text)
    text = html.unescape(text)
    text = _TAG_RE.sub(" ", text)
    lines = [_WS_RE.sub(" ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _clean_label(raw: object) -> str:
    """整串归一 (与 :func:`matrag.domain.vocab.normalize_name` 同口径)。"""
    return normalize_name(_clean_text(raw))


def _clean_cell(raw: object) -> str:
    """文本列取值 (名称/支路/编号/等级/备注/条件), 只清洗不改写。"""
    return _clean_text(raw)


def _to_placeholder(text: str) -> str:
    """把"纯破折号/波浪线"文本还原成统一占位符 (``-``/``–``/``—`` 都表示未给出)。"""
    stripped = text.strip()
    if stripped and all(char in "-~_." for char in stripped):
        return "-"
    return stripped


def _expand_lines(text: str) -> list[str]:
    """把含换行的单元格拆成非空行 (``<br>`` 已在清洗时转成 ``\\n``)。"""
    return [line.strip() for line in text.split("\n") if line.strip()]


def _is_multi_value(text: str, raw: object) -> bool:
    """判断单元格是否承载多值 (不猜数值, 只标记)。

    触发条件: 原始文本含 ``<br>`` 换行, 或两个数字被 ``/ - ~`` 范围符分隔。
    """
    if not text:
        return False
    if raw is not None and _BR_RE.search(str(raw)):
        return True
    return bool(_MULTI_RANGE_RE.search(text))


def _to_float(text: str) -> float | None:
    """把清洗后文本解析为 float; 不可解析返回 ``None`` (调用方决定如何留痕)。"""
    if not text:
        return None
    candidate = _LIST_PREFIX_RE.sub("", text).strip()
    for value in (candidate, _EDGE_DECOR_RE.sub("", candidate).strip()):
        if _PURE_NUMBER_RE.match(value):
            number = float(value)
            if math.isfinite(number):
                return number
    return None


def _resolve_cell(raw: object, flags: list[str]) -> tuple[str, float | None]:
    """解析一个数值单元格: 返回 ``(raw, 数值)``; 必要时向 ``flags`` 追加标记。

    - 空/占位符: raw 保留原样 (清洗后), 数值 ``None``, **不打**标记;
    - 多值: raw 保留, 数值 ``None``, 打 ``multi_value``;
    - 解析失败: raw 保留, 数值 ``None``, 打 ``unparsed``。
    """
    text = _clean_text(raw)
    if not text or is_empty_value(text):
        return text, None
    if _is_multi_value(text, raw):
        flags.append("multi_value")
        return text, None
    value = _to_float(text)
    if value is None:
        flags.append("unparsed")
        return text, None
    return text, value


def _dedupe_flags(flags: Sequence[str]) -> tuple[str, ...]:
    """按 :data:`SPEC_PARAM_FLAGS` 的固定顺序去重 (同一标记只出现一次)。"""
    present = set(flags)
    return tuple(flag for flag in SPEC_PARAM_FLAGS if flag in present)


# --------------------------------------------------------------------------- #
# 实例
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SpecParameter:
    """规格书中的一条产品参数实例。"""

    param_id: str
    product_code: str
    doc_id: str
    section_path: str
    table_id: str
    row_index: int
    name: str
    sub_name: str
    branch: str
    code: str
    grade: str
    unit: str
    minimum_raw: str
    typical_raw: str
    maximum_raw: str
    minimum: float | None
    typical: float | None
    maximum: float | None
    note: str
    condition: str
    requirement: str
    scope: str
    category: str
    flags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的 dict (``flags`` 转 list)。"""
        text = asdict(self)
        text["flags"] = list(self.flags)
        return text


@dataclass(frozen=True)
class SpecStandard:
    """规格书引用的一条标准 (标准号 + 标准名)。"""

    standard_id: str
    product_code: str
    doc_id: str
    section_path: str
    table_id: str
    row_index: int
    code: str
    name: str

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的 dict。"""
        return asdict(self)


# --------------------------------------------------------------------------- #
# 分类
# --------------------------------------------------------------------------- #


def classify_section(section_path: str) -> str:
    """章节路径 → 类别 (取值 ∈ :data:`matrag.domain.vocab.CATEGORIES`)。

    关键词按 :data:`_SECTION_RULES` 固定顺序匹配, 先命中者胜; 未命中返回 ``"other"``。
    """
    normalized = _clean_label(section_path)
    if not normalized:
        return _CATEGORY_FALLBACK
    for category, keywords in _SECTION_RULES:
        if any(_clean_label(keyword) in normalized for keyword in keywords):
            return category
    return _CATEGORY_FALLBACK


def _section_leaf(section_path: str) -> str:
    """章节路径 → 叶名 (去掉 ``>`` 层级与编号前缀), 作为要求型表的匹配作用域。"""
    if not section_path:
        return ""
    leaf = section_path.split(">")[-1].strip()
    return _SECTION_NUMBER_RE.sub("", leaf).strip()


def classify_table(headers: Sequence[str]) -> bool:
    """表头是否"参数表" — 归一后含 ``name`` 列, 且满足其一:

    1. 最小值/典型值/最大值至少命中 2 个 (数值型参数表);
    2. 命中 ``requirement``/``condition`` 列 (要求型表: 信号要求/试验等级/
       持续时间/检测范围/标准(或测试条件)… 同样入参数层)。
    """
    mapped = _map_param_headers(headers)
    if "name" not in mapped:
        return False
    numeric_hits = sum(1 for key in _NUMERIC_KEYS if key in mapped)
    if numeric_hits >= _NUMERIC_MIN_HITS:
        return True
    return "requirement" in mapped or "condition" in mapped


# --------------------------------------------------------------------------- #
# 参数表表头映射
# --------------------------------------------------------------------------- #

#: 字段被占用后的回退字段 (重复"项目"列 → ``sub_name``; 重复"等级"列 → ``requirement``)。
_PARAM_HEADER_FALLBACK: dict[str, str] = {"name": "sub_name", "grade": "requirement"}

#: 同名字段多列时**去重拼接**而不是回退的字段 (试验等级 + 持续时间 都进 requirement)。
_MULTI_TEXT_FIELDS: frozenset[str] = frozenset({"requirement", "condition"})


def _map_param_headers(headers: Sequence[str]) -> dict[str, tuple[int, ...]]:
    """归一表头 → 参数实例字段 → 列下标序列 (未识别列忽略, 不位移)。

    优先级规则:

    1. 显式别名优先: ``项目/子项``/``子项`` 先占 ``sub_name``, 避免被前面的
       ``项目`` 抢占;
    2. 同一字段先到先得: 第一个 ``项目`` 列取 ``name``;
    3. 别名树回退: 第二个 ``项目`` 列 (name 已被占) 落到 ``sub_name``,
       第三个及以后忽略; 第二个 ``等级`` 列 (如"强制/推荐") 落到 ``requirement``;
    4. 拼接字段 (:data:`_MULTI_TEXT_FIELDS`): 同字段列**追加**保留, 文本按列序
       用「；」拼接 (试验等级/持续时间/精度等多列不丢信息)。
    """
    mapped: dict[str, tuple[int, ...]] = {}
    for index, header in enumerate(headers):
        for alias, field in _PARAM_HEADER_ALIASES:
            if alias != _clean_label(header):
                continue
            if field in _MULTI_TEXT_FIELDS:
                mapped[field] = (*mapped.get(field, ()), index)
                break
            if field not in mapped:
                mapped[field] = (index,)
                break
            fallback = _PARAM_HEADER_FALLBACK.get(field)
            if fallback is not None and fallback not in mapped:
                mapped[fallback] = (index,)
                break
    return mapped


# --------------------------------------------------------------------------- #
# 标准清单识别 / 列挑选 / 行解析
# --------------------------------------------------------------------------- #


def _line_tokens(line: str) -> list[str]:
    """按空白切出 ASCII 代码片段 (中文正文被切开后丢弃, 空片段剔除)。"""
    tokens: list[str] = []
    for chunk in _WS_RE.split(line):
        matched = _ASCII_TOKEN_RE.match(chunk.strip("()[]{},;:"))
        if matched:
            tokens.append(matched.group(0).strip("./-"))
    return [token for token in tokens if token]


def _code_like_text(values: Sequence[str]) -> bool:
    """宽松版列判定: 非占位行里至少一条像标准代码 (用于内容稀少的清单表)。"""
    return any(_looks_like_standard_code(line) for line in _meaningful_lines(values))


def _is_standards_table(
    headers: Sequence[str], matrix: Sequence[Sequence[str]]
) -> bool:
    """是否标准清单表: 归一表头含有标准清单专用列, 且该列确有内容。

    专用列闭集见 :data:`_STANDARD_COLUMN_LABELS`。两个判别要点:

    1. 参数表里的 ``标准（或测试条件）`` 列**不算** —— 展开后虽然只剩 ``标准``,
       但它装的是"判据/试验条件"正文 (``判据B``、``29.5``), 内容启发式能识破;
    2. 即使是闭集里的列, 也必须真有内容 —— 26 行全是 ``-`` 的列不算。
    """
    labels = [_clean_label(header) for header in headers]
    for index, label in enumerate(labels):
        if label not in _STANDARD_COLUMN_LABELS:
            continue
        if label != "标准":
            if _column_has_content(headers, matrix, index):
                return True
            continue
        values = _column_values(matrix, index)
        if _column_is_code_like(values) or _code_like_text(values):
            return True
    return False


def _looks_like_standard_code(line: str) -> bool:
    """内容启发式: 该行是否"以标准代码开头"。

    典型正例: ``GB 4943-2011``、``IEC 61000-4-6（判据A）``、``IPC-9592B 5.2.3``;
    典型负例: ``29.5``、``判据B``、``通信设备产品包装通用技术条件``。

    判定方式: 首个空白分隔片段必须**以组织前缀开头**且后面还跟着代码片段
    (``GB 4943-2011``), 或整行就是一个前缀加数字后缀 (``EN 55022``)。
    """
    if not line:
        return False
    parts = [part for part in _WS_RE.split(line.strip()) if part]
    if not parts:
        return False
    head = parts[0].strip("()[]{},;:").lower()
    for prefix in sorted(_STANDARD_PREFIXES, key=len, reverse=True):
        if head == prefix:
            return len(parts) > 1
        if head.startswith(prefix + "-") or head.startswith(prefix + "/"):
            return True
    return False


def _column_values(matrix: Sequence[Sequence[str]], index: int) -> list[str]:
    """取列向量 (行不足时按缺失处理)。"""
    return [row[index] if index < len(row) else "" for row in matrix]


def _meaningful_lines(values: Sequence[str]) -> list[str]:
    """列向量 → 去掉空/占位符后的行清单 (供内容启发式判断列属性)。"""
    lines: list[str] = []
    for value in values:
        for line in _expand_lines(value):
            if not is_empty_value(_to_placeholder(line)):
                lines.append(line)
    return lines


def _column_has_content(
    headers: Sequence[str], matrix: Sequence[Sequence[str]], index: int
) -> bool:
    """该列是否存在表头之外的实质内容行 (占位符行不算)。"""
    if index >= len(headers):
        return False
    return bool(_meaningful_lines(_column_values(matrix, index)))


def _column_is_code_like(values: Sequence[str]) -> bool:
    """列属性启发: 该列的非占位行里, 多数行像标准代码。

    样本上限 :data:`_CODE_SAMPLE_LIMIT`; 非占位行少于 :data:`_MIN_CODE_SAMPLES`
    时按"非代码列"处理。
    """
    lines = _meaningful_lines(values)
    if len(lines) < _MIN_CODE_SAMPLES:
        return False
    sample = lines[:_CODE_SAMPLE_LIMIT]
    hits = sum(1 for line in sample if _looks_like_standard_code(line))
    return hits / len(sample) >= _CODE_LIKE_MIN_RATIO


def _pick_code_columns(
    headers: Sequence[str], matrix: Sequence[Sequence[str]]
) -> list[int]:
    """挑出"标准代码列"。

    表头命中闭集的列优先; 另外对"内容像标准代码"的列做补充 —— 现实语料里存在
    表头写"标准号"、数据却全挤进第 0 列的畸形表 (CY0177 ``t6-0``), 只有内容启发能救回。
    """
    labels = [_clean_label(header) for header in headers]
    picked = [
        index
        for index, label in enumerate(labels)
        if label in _STANDARD_KEY_COLUMNS
        and _column_has_content(headers, matrix, index)
    ]
    for index in range(len(headers)):
        if index not in picked and _column_is_code_like(_column_values(matrix, index)):
            picked.append(index)
    return sorted(picked)


def _pick_name_columns(
    headers: Sequence[str], matrix: Sequence[Sequence[str]], code_cols: Sequence[int]
) -> list[int]:
    """挑出"标准名称列"。

    优先取表头命中名称列闭集且确有内容的列; 否则取"代码列右侧首个有内容的正文列"
    (排除代码形态列与其它闭集列, 避免把 ``等级``/``备注`` 当名称)。
    """
    labels = [_clean_label(header) for header in headers]
    by_header = [
        index
        for index, label in enumerate(labels)
        if label in _STANDARD_NAME_COLUMNS
        and _column_has_content(headers, matrix, index)
    ]
    if by_header:
        return by_header
    floor = max(code_cols) if code_cols else -1
    for index in range(floor + 1, len(headers)):
        if index in code_cols or labels[index] in _STANDARD_COLUMN_LABELS:
            continue
        if _column_is_code_like(_column_values(matrix, index)):
            continue
        if _column_has_content(headers, matrix, index):
            return [index]
    return []


def _standard_line_is_code(line: str, previous_code: bool) -> bool:
    """内容启发式: 该行是否算一条"标准号文本" (含被 ``<br>`` 折断的续行)。

    保留: ``GB 4943-2011``、``IEC 61000-4-6（判据A）``、``IPC-9592B 5.2.3``,
    以及紧跟标准代码之后的英文续行 (``(Including Electrical Business Equipment)``);
    丢弃: 纯破折号占位、``29.5``、``判据B``、``通信设备产品包装通用技术条件``。
    """
    if not line or is_empty_value(_to_placeholder(line)):
        return False
    if _looks_like_standard_code(line):
        return True
    if not previous_code:
        return False
    tokens = _line_tokens(line)
    if tokens and all(_PURE_NUMBER_RE.match(token) for token in tokens):
        return False
    if _CJK_RE.search(line) and len(line) < _MIN_CONTINUATION_LEN:
        return False
    return len(line) >= _MIN_CONTINUATION_LEN


def _filter_code_cell(text: str) -> str:
    """对"内容启发式选中的代码列"单元格做行级过滤, 返回用「；」连接的行。"""
    kept: list[str] = []
    previous_code = False
    for line in _expand_lines(text):
        if _standard_line_is_code(line, previous_code):
            kept.append(line)
            previous_code = _looks_like_standard_code(line)
        else:
            previous_code = False
    return "；".join(kept)


def _standard_row_codes(
    headers: Sequence[str], row: Sequence[Any], code_cols: Sequence[int]
) -> list[str]:
    """一行的标准号列表: 同类列的非空单元格用「；」级联。

    表头明确写着"标准号"的列**全量保留** (信表头); 靠内容启发选中的列做行级过滤
    (这种列里可能混进 ``判据B``、``29.5`` 这类判据/参数值)。
    """
    values: list[str] = []
    for index in code_cols:
        text = _clean_cell(_cell_value(row, index))
        if not text or is_empty_value(_to_placeholder(text)):
            continue
        if _clean_label(headers[index]) not in _STANDARD_KEY_COLUMNS:
            text = _filter_code_cell(text)
            if not text:
                continue
        values.append(text)
    return ["；".join(values)] if values else []


def _standard_row_name(
    row: Sequence[Any], name_cols: Sequence[int], index: int, total: int
) -> str:
    """一行的标准名: 同名类列的非空单元格用「；」级联; 单名称复用给多标准号。"""
    values: list[str] = []
    for column in name_cols:
        text = _clean_cell(_cell_value(row, column))
        if text and not is_empty_value(text):
            values.append(text)
    if not values:
        return ""
    if total > 1 and len(values) == 1:
        return values[0]
    return values[index] if index < len(values) else values[0]


def _is_standard_header_row(code: str, name: str) -> bool:
    """该行是否是被解析成数据行的"重复表头" (如 ``标准号 | 名称``)。"""
    if code not in _STANDARD_HEADER_TERMS:
        return False
    return not name or name in _STANDARD_HEADER_TERMS


# --------------------------------------------------------------------------- #
# 文件读取 (fail-fast)
# --------------------------------------------------------------------------- #


def _resolve_corpus_dir(corpus_dir: Path | str | None) -> Path:
    """解析语料目录 (缺省 :data:`DEFAULT_CORPUS_DIR`)。"""
    return Path(corpus_dir) if corpus_dir is not None else DEFAULT_CORPUS_DIR


def _load_json(path: Path) -> Any:
    """读 JSON; 文件缺失 → ``FileNotFoundError``, 内容非法 → ``ValueError``。"""
    if not path.is_file():
        raise FileNotFoundError(f"语料文件缺失: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"语料 JSON 非法: {path}: {exc}") from exc


def _iter_corpus(corpus_dir: Path) -> list[Path]:
    """列出语料目录下的 ``*.doc.json`` (目录/文件缺失即报错, 不静默跳过)。"""
    if not corpus_dir.is_dir():
        raise FileNotFoundError(f"语料目录缺失: {corpus_dir}")
    doc_files = sorted(corpus_dir.glob("*.doc.json"))
    if not doc_files:
        raise FileNotFoundError(f"语料目录下无 *.doc.json: {corpus_dir}")
    return doc_files


def _iter_doc_tables(
    corpus_dir: Path, doc_json: Path
) -> tuple[str, str, list[dict[str, Any]]]:
    """读取一份文档的 ``doc_id``/``product_code`` 与表格列表 (结构非法即抛)。"""
    doc = _load_json(doc_json)
    if not isinstance(doc, dict):
        raise ValueError(f"doc 产物结构非法 (应为对象): {doc_json}")
    doc_id = doc.get("doc_id")
    if not isinstance(doc_id, str) or not doc_id.strip():
        raise ValueError(f"doc 产物缺少 doc_id: {doc_json}")
    tables_path = corpus_dir / doc_json.name.replace(".doc.json", ".tables.json")
    tables = _load_json(tables_path)
    if not isinstance(tables, list):
        raise ValueError(f"tables 产物结构非法 (应为 list): {tables_path}")
    for table in tables:
        if not isinstance(table, dict):
            raise ValueError(f"表结构非法 (应为对象): {tables_path}")
        headers = table.get("headers")
        if not isinstance(headers, list):
            raise ValueError(f"表缺少 headers 列表: {tables_path}")
        if any(not isinstance(header, str) for header in headers):
            raise ValueError(f"表 headers 含非字符串: {tables_path}")
        rows = table.get("rows")
        if not isinstance(rows, list):
            raise ValueError(f"表缺少 rows 列表: {tables_path}")
        if any(not isinstance(row, list) for row in rows):
            raise ValueError(f"表 rows 含非列表行: {tables_path}")
    return doc_id, doc_id.split(" ", 1)[0], tables


def _cell_value(row: Sequence[Any], index: int) -> object:
    """取行内单元格 (行长不足补空, 超长忽略多余尾列)。"""
    return row[index] if index < len(row) else ""


# --------------------------------------------------------------------------- #
# 抽取
# --------------------------------------------------------------------------- #


def extract_spec_parameters(
    corpus_dir: Path | str | None = None,
) -> list[SpecParameter]:
    """抽取全部产品参数实例 (按 ``(doc_id, table_id, row_index)`` 稳定排序)。

    :param corpus_dir: 语料目录; ``None`` 用 :data:`DEFAULT_CORPUS_DIR`。
    :raises FileNotFoundError: 目录或 ``*.tables.json`` 缺失。
    :raises ValueError: JSON 结构非法。
    """
    root = _resolve_corpus_dir(corpus_dir)
    results: list[SpecParameter] = []
    for doc_json in _iter_corpus(root):
        doc_id, product_code, tables = _iter_doc_tables(root, doc_json)
        for index in range(len(tables)):
            results.extend(_extract_table(doc_id, product_code, tables, index))
    results.sort(key=lambda item: (item.doc_id, item.table_id, item.row_index))
    return results


def extract_standards(corpus_dir: Path | str | None = None) -> list[SpecStandard]:
    """抽取全部标准引用 (按 ``(doc_id, table_id, row_index, standard_id)`` 稳定排序)。

    :param corpus_dir: 语料目录; ``None`` 用 :data:`DEFAULT_CORPUS_DIR`。
    :raises FileNotFoundError: 目录或 ``*.tables.json`` 缺失。
    :raises ValueError: JSON 结构非法。
    """
    root = _resolve_corpus_dir(corpus_dir)
    results: list[SpecStandard] = []
    for doc_json in _iter_corpus(root):
        doc_id, product_code, tables = _iter_doc_tables(root, doc_json)
        for table in tables:
            results.extend(_extract_standard_table(doc_id, product_code, table))
    results.sort(
        key=lambda item: (
            item.doc_id, item.table_id, item.row_index, item.standard_id,
        )
    )
    return results


def _extract_table(
    doc_id: str, product_code: str, tables: list[dict[str, Any]], index: int
) -> list[SpecParameter]:
    """抽取单张表的参数实例 (非参数表/标准清单表返回空)。"""
    table = tables[index]
    headers = [str(header) for header in table["headers"]]
    matrix = [[_clean_cell(cell) for cell in row] for row in table["rows"]]
    if _is_standards_table(headers, matrix) or not classify_table(headers):
        return []
    table_id = str(table.get("id") or f"t{index}")
    source = table.get("source") if isinstance(table.get("source"), dict) else {}
    section_path = str(source.get("section_path", ""))
    category = classify_section(section_path)
    mapped = _map_param_headers(headers)
    numeric_hits = sum(1 for key in _NUMERIC_KEYS if key in mapped)
    scope = _section_leaf(section_path) if numeric_hits < _NUMERIC_MIN_HITS else ""
    out: list[SpecParameter] = []
    for row_index, row in enumerate(table["rows"]):
        name = _clean_cell(_cell_value(row, mapped["name"][0]))
        sub_name = _text_cell(row, mapped, "sub_name")
        condition = _joined_text_cell(row, mapped, "condition")
        requirement = _joined_text_cell(row, mapped, "requirement")
        flags: list[str] = []
        raws: dict[str, str] = {}
        numbers: dict[str, float | None] = {}
        for key in _NUMERIC_KEYS:
            if key in mapped:
                cell = _cell_value(row, mapped[key][0])
                raws[key], numbers[key] = _resolve_cell(cell, flags)
            else:
                raws[key], numbers[key] = "", None
        numbers_empty = all(value is None for value in numbers.values())
        if not name and not sub_name and numbers_empty and not requirement and not condition:
            continue
        if not name:
            flags.append("no_name")
        out.append(
            SpecParameter(
                param_id=f"{doc_id}|{table_id}|{row_index}",
                product_code=product_code,
                doc_id=doc_id,
                section_path=section_path,
                table_id=table_id,
                row_index=row_index,
                name=name,
                sub_name=sub_name,
                branch=_text_cell(row, mapped, "branch"),
                code=_text_cell(row, mapped, "code"),
                grade=_text_cell(row, mapped, "grade"),
                unit=_unit_cell(row, mapped),
                minimum_raw=raws["minimum"],
                typical_raw=raws["typical"],
                maximum_raw=raws["maximum"],
                minimum=numbers["minimum"],
                typical=numbers["typical"],
                maximum=numbers["maximum"],
                note=_text_cell(row, mapped, "note"),
                condition=condition,
                requirement=requirement,
                scope=scope,
                category=category,
                flags=_dedupe_flags(flags),
            )
        )
    return out


def _text_cell(row: Sequence[Any], mapped: dict[str, tuple[int, ...]], key: str) -> str:
    """取文本列 (未映射或缺失时返回空串); 同字段多列时取第一列。"""
    columns = mapped.get(key)
    if not columns:
        return ""
    return _clean_cell(_cell_value(row, columns[0]))


def _joined_text_cell(
    row: Sequence[Any], mapped: dict[str, tuple[int, ...]], key: str
) -> str:
    """取同字段的多列文本并按列序「；」拼接 (相同文本只保留一次)。

    要求型表里 ``试验等级``/``持续时间``/``精度`` 同进 ``requirement``,
    ``条件``/``评估条件``/``试验条件`` 同进 ``condition`` —— 全部保留不丢信息。
    """
    columns = mapped.get(key)
    if not columns:
        return ""
    parts: list[str] = []
    for column in columns:
        text = _clean_cell(_cell_value(row, column))
        if not text or is_empty_value(text) or text in parts:
            continue
        parts.append(text)
    return "；".join(parts)


def _unit_cell(row: Sequence[Any], mapped: dict[str, tuple[int, ...]]) -> str:
    """取单位列并做最小归一 (``℃``→``°C``、``µ``→``u``; 不改大小写)。"""
    columns = mapped.get("unit")
    if not columns:
        return ""
    return normalize_unit(_cell_value(row, columns[0]))


def _extract_standard_table(
    doc_id: str, product_code: str, table: dict[str, Any]
) -> list[SpecStandard]:
    """抽取单张标准清单表的引用记录。"""
    headers = [str(header) for header in table["headers"]]
    matrix = [[_clean_cell(cell) for cell in row] for row in table["rows"]]
    if not _is_standards_table(headers, matrix):
        return []
    code_cols = _pick_code_columns(headers, matrix)
    if not code_cols:
        return []
    name_cols = _pick_name_columns(headers, matrix, code_cols)
    table_id = str(table.get("id") or "")
    source = table.get("source") if isinstance(table.get("source"), dict) else {}
    section_path = str(source.get("section_path", ""))
    out: list[SpecStandard] = []
    for row_index, row in enumerate(table["rows"]):
        codes = _standard_row_codes(headers, row, code_cols)
        if not codes:
            continue
        for seq, code in enumerate(codes):
            name = _standard_row_name(row, name_cols, seq, len(codes))
            if _is_standard_header_row(code, name):
                continue
            out.append(
                SpecStandard(
                    standard_id=f"{doc_id}|{table_id}|{row_index}|{seq}",
                    product_code=product_code,
                    doc_id=doc_id,
                    section_path=section_path,
                    table_id=table_id,
                    row_index=row_index,
                    code=code,
                    name=name,
                )
            )
    return out


def spec_param_stats(params: Sequence[SpecParameter] | None = None) -> dict[str, Any]:
    """参数实例统计。

    :param params: 已抽取的参数序列; ``None`` 时按 :data:`DEFAULT_CORPUS_DIR` 现抽。
    :return: ``{"total", "by_product", "by_category", "flagged", "multi_value",
        "unparsed"}`` (``by_*`` 为按 key 排序的计数字典)。
    """
    items = list(params) if params is not None else extract_spec_parameters()
    by_product: dict[str, int] = {}
    by_category: dict[str, int] = {}
    flagged = 0
    multi_value = 0
    unparsed = 0
    for param in items:
        by_product[param.product_code] = by_product.get(param.product_code, 0) + 1
        by_category[param.category] = by_category.get(param.category, 0) + 1
        if param.flags:
            flagged += 1
        if "multi_value" in param.flags:
            multi_value += 1
        if "unparsed" in param.flags:
            unparsed += 1
    return {
        "total": len(items),
        "by_product": dict(sorted(by_product.items())),
        "by_category": dict(sorted(by_category.items())),
        "flagged": flagged,
        "multi_value": multi_value,
        "unparsed": unparsed,
    }
