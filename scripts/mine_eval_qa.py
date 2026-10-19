"""评测集挖掘脚本（P1-7）: 从**真实语料**自动生成 ``data/eval/qa.jsonl``。

用法::

    python -m scripts.mine_eval_qa                      # 全量重建 qa.jsonl（幂等）
    python -m scripts.mine_eval_qa --append             # 增量：只补充缺失的 qid
    python -m scripts.mine_eval_qa --min-per-doc 10     # 每份文档至少挖多少条
    python -m scripts.mine_eval_qa --out data/eval/qa.jsonl

设计纪律（对应 P1-7 的"禁止编造"）:

- **spec / fault 类**：问题、期望文档、期望章节、``must_contain`` 全部来自
  ``data/corpus/*.tables.json`` 的真实表格内容——章节取 ``source.section_path``
  的叶子节点，表头/单元格原文作为 ``must_contain``。
- **formula / cross_doc 类**：人工挑选"问法"，但 ``expected_doc_id`` /
  ``expected_sections`` / ``must_contain`` 仍从
  ``data/domain/generated/knowledge_cards.jsonl`` 与语料表格里取原文，
  并在生成时做 **grounding 校验**（见 :func:`_assert_grounded`）:
  词条只要在源文本里找不到就 ``ValueError`` fail-fast——宁可不出题，
  也不能出一道"语料里查无此词"的假题。
- 幂等：同参数重跑输出一致；``--append`` 按 qid 去重合并（已存在的 qid 跳过）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from matrag.config import settings

# ---------------------------------------------------------------------------
# 规范 / 工具
# ---------------------------------------------------------------------------

#: 合法 tier 枚举（与 qa.jsonl 契约、tests/test_eval_qa.py 共用口径）
TIERS: tuple[str, ...] = ("spec", "formula", "fault", "cross_doc")

#: 默认评测集路径（相对 settings.eval_dir）
DEFAULT_QA_NAME = "qa.jsonl"

#: 表头列的取值优先级：找"项目/指标"列（问题问什么）
ITEM_HEADER_PRIORITY: tuple[str, ...] = (
    "遥测量",
    "测试项目",
    "试验项目",
    "项目名称",
    "项目",
    "指标",
    "名称",
    "定义",
    "位号",
)

#: 表头列的取值优先级：找"值/范围"列（问题问哪一栏）
VALUE_HEADER_PRIORITY: tuple[str, ...] = (
    "最小值",
    "最大值",
    "典型值",
    "量值",
    "典型/值",
    "检测范围",
    "指标要求",
    "要求",
    "精度",
    "测试条件",
    "标准（或测试条件）",
)

#: 表示"该行没有值"的占位符（语料里用得最多的是 ``-``）
_EMPTY_CELLS: frozenset[str] = frozenset({"", "-", "--", "—", "/", "N/A", "NA"})

_WHITESPACE_RE = re.compile(r"\s+")
_BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)


def normalize(text: str) -> str:
    """评测文本的归一口径: NFKC + 去 ``<br>`` + 去全部空白。

    语料单元格里混着换行与 ``<br>``（表格重建的既定表示），问句/词条/命中
    文本必须用同一口径比对，否则"明明命中了却判未命中"。
    """
    folded = unicodedata.normalize("NFKC", str(text or ""))
    return _WHITESPACE_RE.sub("", _BR_RE.sub("", folded))


def is_empty_cell(value: Any) -> bool:
    """单元格是否等价于"空"（含 ``-`` / 空串等占位）。"""
    return normalize(value).strip("-—/") == ""


def _display(value: Any) -> str:
    """人读/入问句的清洗: 去 ``<br>``、折叠空白（保留 ``normalize`` 之外的字面量）。"""
    folded = unicodedata.normalize("NFKC", str(value or ""))
    return _WHITESPACE_RE.sub(" ", _BR_RE.sub("", folded)).strip()


def _display_keeping_breaks(value: Any) -> str:
    """同 :func:`_display`，但把单元格里的 ``<br>`` 换成 ``/``。

    一个单元格里堆了好几行的表（GDC 输入欠压保护的"保护点/恢复点/回差"三行合
    一格）直接去掉 ``<br>`` 会把三个数粘成一个数，必须保留分隔。
    """
    folded = unicodedata.normalize("NFKC", str(value or ""))
    return _WHITESPACE_RE.sub(" ", _BR_RE.sub("/", folded)).strip()


@dataclass
class CorpusTable:
    """语料里的一张表（``*.tables.json`` 的一条记录，带来源文档上下文）。"""

    doc_id: str
    doc_title: str
    raw: dict[str, Any]

    @property
    def table_id(self) -> str:
        return str(self.raw.get("id", ""))

    @property
    def caption(self) -> str:
        return str(self.raw.get("caption", "") or "")

    @property
    def headers(self) -> list[str]:
        return [str(h) for h in self.raw.get("headers", [])]

    @property
    def rows(self) -> list[list[str]]:
        return [list(str(c) for c in row) for row in self.raw.get("rows", [])]

    @property
    def section_path(self) -> str:
        return str((self.raw.get("source") or {}).get("section_path", "") or "")

    @property
    def page(self) -> int:
        return int((self.raw.get("source") or {}).get("page", 0) or 0)

    @property
    def section_leaf(self) -> str:
        """``section_path`` 的最后一级（chunk 的 ``section_titles`` 末项）。"""
        segments = [s.strip() for s in self.section_path.split(">") if s.strip()]
        return segments[-1] if segments else ""

    @property
    def doc_alias(self) -> str:
        """文档的**口语化型号**（"PA601-D54A 定制电源技术规格书" -> "PA601"）。"""
        token = self.doc_id.split(" ")[0]
        return token.split("-")[0] or token

    @property
    def doc_code(self) -> str:
        """文档的完整产品编号（与 chunk meta 的 ``product_code`` 同口径）。"""
        return self.doc_id.split(" ")[0]

    def rendered_text(self) -> str:
        """按 chunking 的表格渲染口径还原全文（表题 + 表头 + 全部行）。

        与 :mod:`matrag.index.chunking` 的 ``_emit_table_rows`` 保持一致——
        grounding 校验要对着**真正被索引的文本**核对，而不是对着 JSON 结构。
        """
        parts: list[str] = []
        if self.caption:
            parts.append(self.caption)
        parts.append(" | ".join(self.headers))
        for row in self.rows:
            parts.append(" | ".join(row))
        return "\n".join(parts)

    def pick_column(self, priorities: Sequence[str]) -> int:
        """按优先级找第一个命中的表头列下标；找不到返回 -1。"""
        normalized = [normalize(h) for h in self.headers]
        for token in priorities:
            needle = normalize(token)
            for index, header in enumerate(normalized):
                if needle and needle in header:
                    return index
        return -1

    def pick_value_column(self, excluded: set[int]) -> int:
        """找"值/范围"列；``excluded`` 内的列（项目列/支路列）不参与。"""
        index = self.pick_column(VALUE_HEADER_PRIORITY)
        if index >= 0 and index not in excluded:
            return index
        for token in VALUE_HEADER_PRIORITY:
            needle = normalize(token)
            for position, header in enumerate(self.headers):
                if position in excluded or needle not in normalize(header):
                    continue
                return position
        return -1

    def cell(self, row: Sequence[str], index: int) -> str:
        if index < 0 or index >= len(row):
            return ""
        return row[index]

    def rail_of(self, row: Sequence[str]) -> str:
        """支路列的值（GDC 那类"同一行分多路输出"的表用），没有就空串。"""
        for index, header in enumerate(self.headers):
            if "支路" not in normalize(header):
                continue
            value = self.cell(row, index).strip()
            if value and not is_empty_cell(value):
                return normalize(value)
        return ""


@dataclass
class KnowledgeCard:
    """域知识卡片（``knowledge_cards.jsonl`` 的一条记录）。"""

    card_id: str
    title: str
    section_path: str
    body: str

    @property
    def rendered_text(self) -> str:
        return f"{self.title}\n{self.body}"


@dataclass
class QaItem:
    """一条评测题目。"""

    qid: str
    query: str
    expected_doc_id: str
    expected_sections: list[str]
    must_contain: list[str]
    answer_hint: str
    tier: str
    source: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "qid": self.qid,
            "query": self.query,
            "expected_doc_id": self.expected_doc_id,
            "expected_sections": list(self.expected_sections),
            "must_contain": list(self.must_contain),
            "answer_hint": self.answer_hint,
            "tier": self.tier,
            "source": dict(self.source),
        }


# ---------------------------------------------------------------------------
# 语料加载
# ---------------------------------------------------------------------------


def load_corpus_tables(corpus_dir: Path) -> list[CorpusTable]:
    """读 ``data/corpus/*.tables.json`` → 带文档上下文的表列表（按 doc_id 排序）。"""
    files = sorted(corpus_dir.glob("*.tables.json"))
    if not files:
        raise FileNotFoundError(f"语料表目录为空: {corpus_dir}（先跑 scripts/parse/CLI 生成语料）")
    tables: list[CorpusTable] = []
    for path in files:
        doc_id = path.name[: -len(".tables.json")]
        doc_title = _doc_title(corpus_dir, doc_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"{path} 顶层应为 list，实际 {type(payload).__name__}")
        for record in payload:
            if not isinstance(record, dict):
                raise ValueError(f"{path} 内含非 dict 表记录")
            tables.append(CorpusTable(doc_id=doc_id, doc_title=doc_title, raw=record))
    return tables


def _doc_title(corpus_dir: Path, doc_id: str) -> str:
    """从 ``*.doc.json`` 读文档标题（缺失时退回 doc_id，不抛）。"""
    path = corpus_dir / f"{doc_id}.doc.json"
    if not path.is_file():
        return doc_id
    payload = json.loads(path.read_text(encoding="utf-8"))
    return str(payload.get("title") or doc_id)


def load_knowledge_cards(path: Path | None = None) -> dict[str, KnowledgeCard]:
    """读域知识卡片 → ``{card_id: KnowledgeCard}``（文件缺失 fail-fast）。"""
    file = path or (Path(settings.domain_dir) / "generated" / "knowledge_cards.jsonl")
    if not file.is_file():
        raise FileNotFoundError(f"域知识卡片缺失: {file}")
    cards: dict[str, KnowledgeCard] = {}
    for line_no, line in enumerate(file.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        card = KnowledgeCard(
            card_id=str(payload["card_id"]),
            title=str(payload.get("title", "")),
            section_path=str(payload.get("section_path", "")),
            body=str(payload.get("body", "")),
        )
        if card.card_id in cards:
            raise ValueError(f"卡片 id 重复: {card.card_id}（{file}:{line_no}）")
        cards[card.card_id] = card
    if not cards:
        raise ValueError(f"域知识卡片为空: {file}")
    return cards


# ---------------------------------------------------------------------------
# grounding 校验（"禁止编造"的硬闸门）
# ---------------------------------------------------------------------------


def _assert_grounded(terms: Iterable[str], haystack: str, *, where: str) -> None:
    """断言每个词条都真实出现在 ``haystack`` 中，否则 ``ValueError``。

    这是本脚本最重要的一道闸门：只要有一个 ``must_contain`` 词是编的，
    题目就不允许落盘（宁可少一题，也不产出一份"查无此词"的评测集）。
    """
    body = normalize(haystack)
    for term in terms:
        needle = normalize(term)
        if not needle:
            raise ValueError(f"[{where}] must_contain 存在空词条")
        if needle not in body:
            raise ValueError(f"[{where}] must_contain 词条 {term!r} 在来源文本中不存在（拒绝出题）")


def _find_table(tables: Sequence[CorpusTable], doc_id: str, table_id: str) -> CorpusTable:
    for table in tables:
        if table.doc_id == doc_id and table.table_id == table_id:
            return table
    raise KeyError(f"语料里找不到表 {doc_id}/{table_id}")


def _first_row_containing(table: CorpusTable, needle: str) -> list[str] | None:
    """表中第一行（按行拼接后的文本）包含 ``needle`` 的行。"""
    target = normalize(needle)
    for row in table.rows:
        if target in normalize(" | ".join(row)):
            return row
    return None


# ---------------------------------------------------------------------------
# spec / fault 挖掘（表格驱动，全自动）
# ---------------------------------------------------------------------------

#: 每张表最多出几道题（round-robin 的最大深度）
MAX_QUESTIONS_PER_TABLE = 4

#: 选代表性表时命中的 caption/section 关键词（按优先级，靠前者先被选）
TABLE_PRIORITY: tuple[str, ...] = (
    "输出特性",
    "输出规格",
    "输入特性",
    "输入规格",
    "绝对额定",
    "效率",
    "纹波",
    "动态特性",
    "保护",
    "环境",
    "遥测",
    "遥控",
    "监控",
    "EMC",
    "可靠性",
    "安规",
    "MTBF",
)

#: 数值型列的问题模板（"…是多少"）
VALUE_QUERY_TEMPLATES: tuple[str, ...] = (
    "{alias}的{item}{rail}{col}是多少？",
    "{alias}规格里{item}{rail}的{col}是多少？",
    "帮我查下 {alias} 的 {item}{rail}{col}",
    "{alias}的{item}{rail}{col}是多少",
    "看下 {alias}，{item}{rail}的{col}是多少？",
    "{alias} 的 {item}{rail}{col}要求是多少",
)

#: 文本型列（要求/条件/指标）的问题模板（"…是什么/怎么规定的"）
TEXT_QUERY_TEMPLATES: tuple[str, ...] = (
    "{alias} 对 {item}{rail} 的 {col}是什么？",
    "{alias}规格里{item}{rail}的{col}写的是什么？",
    "帮我查下 {alias} 的 {item}{rail}{col}",
    "{alias} 的 {item}{rail}{col}怎么规定的？",
    "看下 {alias}，{item}{rail}的{col}是什么？",
    "{alias} 的 {item}{rail} {col}是什么？",
)

#: 取哪套模板：数值列 vs 文本列（键与 :data:`VALUE_HEADER_PRIORITY` 字面量一致）
TEXT_COLUMN_TOKENS: frozenset[str] = frozenset(
    {"指标要求", "要求", "测试条件", "标准（或测试条件）"}
)

#: 匹配到的 VALUE 词元 → 问题里的口语化说法（键与 :data:`VALUE_HEADER_PRIORITY` 字面量一致）
COLUMN_PHRASE: dict[str, str] = {
    "最小值": "最小值",
    "最大值": "最大值",
    "典型值": "典型值",
    "量值": "要求值",
    "典型/值": "典型值",
    "检测范围": "检测范围",
    "指标要求": "指标要求",
    "要求": "要求",
    "精度": "精度",
    "测试条件": "测试条件",
    "标准（或测试条件）": "标准要求",
}

#: 纯数字（表头错位探测用；单位列里出现数字 = 这一列根本不是单位）
_NUMERIC_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")


def alignment_issue(table: CorpusTable) -> str | None:
    """表头与数据**错位**的保守探测；返回原因（``None`` = 未发现）。

    语料里存在"表头被平叶子表头重复填了一列、数据却没跟着补"的表
    （实测：PA601 表10 输入特性表 —— ``单位`` 列里躺的是"100/200/88"这类
    纯数字，说明整行左移了一列）。这种表出出来的答案是错的，宁可整表跳过：
    这类表头的表名是``重复子表头``，检查口径固定为"单位列出现纯数字"。
    """
    unit_col = table.pick_column(("单位",))
    if unit_col < 0:
        return None
    for row in table.rows:
        value = table.cell(row, unit_col).strip()
        if value and _NUMERIC_RE.match(normalize(value)):
            return f"单位列出现纯数字 {value!r}（表头与数据疑似错位，整表跳过出题）"
    return None


def _value_columns(table: CorpusTable, item_col: int) -> list[tuple[int, str, str]]:
    """表里所有可问的"值"列 → ``[(列下标, 表头原文, 命中词元), ...]``（按 VALUE 优先级）。"""
    found: list[tuple[int, str, str]] = []
    seen: set[int] = {item_col}
    for token in VALUE_HEADER_PRIORITY:
        needle = normalize(token)
        for index, header in enumerate(table.headers):
            if index in seen or needle not in normalize(header):
                continue
            seen.add(index)
            found.append((index, header.strip(), token))
    return found


def _order_tables(tables: Sequence[CorpusTable]) -> list[CorpusTable]:
    """代表性表排序: 关键词优先级 → 页面 → 表 id（稳定、可复跑）。"""
    def sort_key(table: CorpusTable) -> tuple[int, int, str]:
        haystack = normalize(f"{table.caption} {table.section_path}")
        rank = len(TABLE_PRIORITY)
        for index, token in enumerate(TABLE_PRIORITY):
            if normalize(token) in haystack:
                rank = index
                break
        return (rank, table.page, table.table_id)

    return sorted(tables, key=sort_key)


def _questions_from_table(table: CorpusTable, *, max_per_table: int) -> list[QaItem]:
    """从一张表里生成至多 ``max_per_table`` 条题目。

    出题口径:
    - ``expected_doc_id``   = 表所属文档（= 语料 doc_id）;
    - ``expected_sections`` = 表 ``source.section_path`` 的叶子章节;
    - ``must_contain``      = 该行"项目"列原文 + 该"值"列表头原文;
    - ``answer_hint``       = 表题 + 编号/项目 + 该栏真实取值。

    "值列"按行轮换（最小值→最大值→典型值…），同一张表里出多题时不至于全问同一栏;
    同名同列的重复行（PA601 表11 整机效率三行同名）只取一条。
    """
    item_col = table.pick_column(ITEM_HEADER_PRIORITY)
    if item_col < 0:
        return []
    if alignment_issue(table):
        return []  # 表头/数据错位的表出题答案必错，整表跳过（原因见 alignment_issue）
    value_cols = _value_columns(table, item_col)
    if not value_cols:
        return []

    items: list[QaItem] = []
    used_signatures: set[tuple[str, str, str]] = set()
    for row_index, row in enumerate(table.rows):
        if len(items) >= max_per_table:
            break
        label = _display(table.cell(row, item_col))
        if is_empty_cell(label):
            continue
        candidates = [vc for vc in value_cols if not is_empty_cell(table.cell(row, vc[0]))]
        if not candidates:
            continue
        value_col = candidates[row_index % len(candidates)]
        signature = (normalize(label), normalize(value_col[1]), table.rail_of(row))
        if signature in used_signatures:
            continue
        used_signatures.add(signature)
        items.append(
            _build_table_question(
                table, row_index=row_index, row=row, item_col=item_col, value_col=value_col
            )
        )
    return items


def _id_cell(table: CorpusTable, row: Sequence[str]) -> str:
    """行的第一列**只有在真是编号列**时才返回（否则空串）。

    很多表的第一列就是"项目"本身（如 GDC 的输出特性表），把它当编号塞进
    answer_hint 只会得到"项目；项目"这种噪声。
    """
    if not table.headers:
        return ""
    first = normalize(table.headers[0])
    if "编号" not in first and "序号" not in first:
        return ""
    value = table.cell(row, 0).strip()
    return "" if is_empty_cell(value) else _display(value)


def _build_table_question(
    table: CorpusTable,
    *,
    row_index: int,
    row: Sequence[str],
    item_col: int,
    value_col: tuple[int, str, str],
) -> QaItem:
    """由"表 + 行 + 值列"拼出一条题目（词条全部取自表格原文）。"""
    col_index, col_name, token = value_col
    label = _display(table.cell(row, item_col))
    rail = _display(table.rail_of(row))
    rail_suffix = f"（{rail}）" if rail else ""

    phrase = COLUMN_PHRASE.get(token, _display(col_name))
    # 口语化型号与完整产品编号各占一半：前者考验纯语义召回，后者走 where 过滤
    use_full_code = row_index % 2 == 1
    alias = table.doc_code if use_full_code else table.doc_alias
    templates = TEXT_QUERY_TEMPLATES if token in TEXT_COLUMN_TOKENS else VALUE_QUERY_TEMPLATES
    template = templates[(row_index + len(label)) % len(templates)]
    query = template.format(alias=alias, item=label, rail=rail_suffix, col=phrase)

    value = _display_keeping_breaks(table.cell(row, col_index))
    unit = ""
    for index, header in enumerate(table.headers):
        if normalize(header) in {"单位", "单位值"}:
            unit = _display_keeping_breaks(table.cell(row, index))
            break
    identifier = _id_cell(table, row)
    hint_parts = [
        part
        for part in (table.caption, identifier, f"{label}{rail_suffix}", f"{_display(col_name)}={value}")
        if part
    ]
    if unit and not is_empty_cell(unit):
        hint_parts.append(f"单位 {unit}")

    must_contain = [normalize(label), normalize(col_name)]
    return QaItem(
        qid=f"spec-{_slug(table.doc_id)}-{table.table_id}-r{row_index:02d}",
        query=query,
        expected_doc_id=table.doc_id,
        expected_sections=[table.section_leaf],
        must_contain=[m for m in dict.fromkeys(must_contain) if m],
        answer_hint="；".join(hint_parts),
        tier="spec",
        source={
            "doc_id": table.doc_id,
            "table_id": table.table_id,
            "caption": table.caption,
            "page": table.page,
            "section_path": table.section_path,
            "row_index": row_index,
            "value_column": col_name,
        },
    )


def _slug(text: str) -> str:
    """doc_id → qid 片段（小写、只留字母数字）。"""
    token = text.split(" ")[0].split("-")[0].lower()
    return re.sub(r"[^a-z0-9]+", "", token) or "doc"


# ---------------------------------------------------------------------------
# formula / fault / cross_doc 挖掘（选问法，词条仍取自源文本 + grounding 闸门）
# ---------------------------------------------------------------------------

#: formula 类：域知识卡片题（问法人工挑选，答案与词条来自卡片原文）
FORMULA_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "qid": "formula-power-knowledge-6-ripple",
        "card_id": "power-knowledge-6",
        "query": "输出纹波测试为什么要限制 20MHz 带宽，还要并联那两个电容？",
        "must_contain": ["20MHz 带宽限制", "10uF 电解电容", "0.1uF 陶瓷电容"],
        "answer_hint": "纹波测试使用 20MHz 带宽限制，并联 10uF 电解电容与 0.1uF 陶瓷电容。",
    },
    {
        "qid": "formula-power-knowledge-8-holdup",
        "card_id": "power-knowledge-8",
        "query": "保持时间 hold-up 怎么算母线电容？判定的输入电压取哪个？",
        "must_contain": ["保持时间", "母线电容", "最低输入电压下判定"],
        "answer_hint": "母线电容 C = 2·P·t/(η·(V1²−V2²))，须在最低输入电压下判定（储能按 V² 变化）。",
    },
    {
        "qid": "formula-power-knowledge-7-kelvin",
        "card_id": "power-knowledge-7",
        "query": "四线开尔文接法测接触电阻到底消掉了什么？半 Kelvin 什么时候能用？",
        "must_contain": ["四线 Kelvin 测接触电阻", "力线与感线", "半 Kelvin"],
        "answer_hint": "力线与感线必须在被测接触点汇合，夹具引线电阻被完全消除；半 Kelvin 不消除接触电阻。",
    },
    {
        "qid": "formula-power-knowledge-8-ripple-calc",
        "card_id": "power-knowledge-8",
        "query": "输出纹波是怎么由电感纹波电流算出来的？ESR 那部分怎么算？",
        "must_contain": ["输出纹波", "ΔV_cap", "ΔV_ESR", "ESR"],
        "answer_hint": "ΔV_cap = ΔI_L/(8·f_sw·C_out)；ΔV_ESR = ΔI_L·ESR。",
    },
    {
        "qid": "formula-power-knowledge-11-uvlo",
        "card_id": "power-knowledge-11",
        "query": "欠压保护 UVLO 的回差为什么要留？最低工作电压和启动阈值谁高谁低？",
        "must_contain": ["UVLO 回差", "最低工作电压", "UVLO 启动阈值"],
        "answer_hint": (
            "工作范围与启动范围分层有序：最低工作电压 < UVLO 启动阈值 < UVLO 关断阈值；"
            "回差须大于启动电流引起的供电压降。"
        ),
    },
    {
        "qid": "formula-power-knowledge-20-enob",
        "card_id": "power-knowledge-20",
        "query": "有效位数 ENOB 是怎么由信噪比算出来的？",
        "must_contain": ["ENOB", "SNR_dB", "6.02"],
        "answer_hint": "ENOB = (SNR_dB − 1.76)/6.02，由理想 N 位转换器 SNR = 6.02N+1.76dB 反解。",
    },
)

#: fault 类：故障/保护行为题（期望章节与词条取自语料表格原文）
FAULT_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "qid": "fault-pa601-ocp-recovery",
        "doc_id": "PA601-D54A 定制电源技术规格书",
        "table_id": "t0-16",
        "anchor": "输出过流保护",
        "query": "PA601 输出过流保护动作之后会自动恢复吗？长期过流会不会损坏电源？",
        "must_contain": ["打隔保护", "自恢复", "长期过流不能损坏电源"],
        "sections": ["4.3.3 保护功能"],
        "answer_hint": "打隔保护，输出正常后可自恢复，长期过流不能损坏电源。",
    },
    {
        "qid": "fault-cy0177-scp-recovery",
        "doc_id": "CY0177-75V-20A",
        "table_id": "t9-0",
        "anchor": "输出短路保护",
        "query": "CY0177 输出短路之后多久恢复？超过多久就彻底关机了？",
        "must_contain": ["输出短路保护", "30S 内自恢复", "自动关机"],
        "sections": ["4 保护功能"],
        "answer_hint": "30S 内自恢复，30S 后自动关机，需重新发开机指令才会重新开机。",
    },
    {
        "qid": "fault-gdc800-ocp-mode",
        "doc_id": "GDC800T5054-UC",
        "table_id": "t15-0",
        "anchor": "输出过流保护",
        "query": "GDC800 输出过流保护是打嗝还是锁存？50V 那路的过流点范围是多少？",
        "must_contain": ["输出过流保护", "打嗝模式", "18.2", "22.8"],
        "sections": ["4.4 保护特性"],
        "answer_hint": "50V 输出过流保护 18.2~22.8A，打嗝模式。",
    },
    {
        "qid": "fault-gdc800-otp",
        "doc_id": "GDC800T5054-UC",
        "table_id": "t15-0",
        "anchor": "过温保护",
        "query": "GDC800 过温保护的典型点和回差是多少？过温后会自动恢复吗？",
        "must_contain": ["过温保护", "110", "5", "自恢复"],
        "sections": ["4.4 保护特性"],
        "answer_hint": "过温保护典型 120℃（110~130℃），回差 5℃，自恢复。",
    },
    {
        "qid": "fault-pa601-otp-hysteresis",
        "doc_id": "PA601-D54A 定制电源技术规格书",
        "table_id": "t0-16",
        "anchor": "过温保护回差",
        "query": "PA601 的过温保护回差要求是多少？工作温度范围内会不会误触发过温保护？",
        "must_contain": ["过温保护回差", "5", "自恢复"],
        "sections": ["4.3.3 保护功能"],
        "answer_hint": (
            "过温保护回差 5℃；在规定的工作温度范围内电源需正常工作，不得过温保护，"
            "温度降低到工作温度范围内时能自恢复。"
        ),
    },
)

#: cross_doc 类：同一概念跨文档对照题
CROSS_DOC_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "qid": "cross-ripple-bandwidth-pa601-gdc",
        "primary_doc": "PA601-D54A 定制电源技术规格书",
        "primary_table": "t0-15",
        "secondary_doc": "GDC800T5054-UC",
        "secondary_table": "t13-0",
        "query": "PA601 和 GDC800 测输出纹波时示波器带宽都限多少？还要并什么电容？",
        "must_contain": ["20MHz", "10uF 电解电容", "0.1uF"],
        "sections": ["4.3.2 输出特性", "4.3 输出特性"],
        "answer_hint": "PA601：20MHz 带宽限制并接 10uF 电解电容和 0.1uF 电容测试；GDC800：示波器带宽 20MHz。",
    },
    {
        "qid": "cross-hipot-pa601-cy0177",
        "primary_doc": "CY0177-75V-20A",
        "primary_table": "t11-0",
        "secondary_doc": "PA601-D54A 定制电源技术规格书",
        "secondary_table": "t0-30",
        "query": "CY0177 和 PA601 的输入对输出绝缘耐压分别是多少？绝缘电阻要求差多少？",
        "must_contain": ["抗电强度", "3000Vac(4240Vdc)/10mA/1min", "≥50MΩ@500Vdc"],
        "sections": ["5 其它功能", "4.4.6.1 安规测试要求"],
        "answer_hint": (
            "CY0177 输入与输出 3000Vac(4240Vdc)/10mA/1min、绝缘电阻 ≥50MΩ@500Vdc；"
            "PA601 输入对输出 4000Vdc、绝缘电阻 2MΩ。"
        ),
    },
    {
        "qid": "cross-uvlo-pa601-cy0177",
        "primary_doc": "CY0177-75V-20A",
        "primary_table": "t9-0",
        "secondary_doc": "PA601-D54A 定制电源技术规格书",
        "secondary_table": "t0-16",
        "query": "输入欠压保护点 PA601 和 CY0177 分别定在多少？回差各是多少？",
        "must_contain": ["输入欠压保护", "欠压点", "85"],
        "sections": ["4 保护功能", "4.3.3 保护功能"],
        "answer_hint": (
            "CY0177 输入欠压保护欠压点 85Vac、回差 5Vac；"
            "PA601 输入欠压保护点 80Vac、恢复点 83Vac、回差 3Vac。"
        ),
    },
    {
        "qid": "cross-ocp-mode-pa601-gdc",
        "primary_doc": "GDC800T5054-UC",
        "primary_table": "t15-0",
        "secondary_doc": "PA601-D54A 定制电源技术规格书",
        "secondary_table": "t0-16",
        "query": "GDC800 和 PA601 的输出过流保护都是什么动作方式？低线输入时过流点怎么变？",
        "must_contain": ["打嗝模式", "输出过流保护", "18.2"],
        "sections": ["4.4 保护特性", "4.3.3 保护功能"],
        "answer_hint": (
            "GDC800：50V 输出过流 18.2~22.8A 打嗝模式；"
            "PA601：输出过流 12~18A 打隔保护，输入 <176Vac 时过流点 8.1A~18A。"
        ),
    },
)


def mine_formula_questions(cards: dict[str, KnowledgeCard]) -> list[QaItem]:
    """域知识卡片题（公式/方法类），词条经 grounding 校验。"""
    items: list[QaItem] = []
    for seed in FORMULA_SEEDS:
        card_id = str(seed["card_id"])
        card = cards.get(card_id)
        if card is None:
            raise KeyError(f"域知识卡片不存在: {card_id}（knowledge_cards.jsonl 已变更？）")
        _assert_grounded(seed["must_contain"], card.rendered_text, where=str(seed["qid"]))
        items.append(
            QaItem(
                qid=str(seed["qid"]),
                query=str(seed["query"]),
                expected_doc_id=card_id,
                expected_sections=[card.section_path] if card.section_path else [],
                must_contain=[normalize(t) for t in seed["must_contain"]],
                answer_hint=str(seed["answer_hint"]),
                tier="formula",
                source={"card_id": card_id, "card_title": card.title, "origin": "knowledge_cards"},
            )
        )
    return items


def mine_fault_questions(tables: Sequence[CorpusTable]) -> list[QaItem]:
    """故障/保护行为题：期望章节与词条都对着语料表格原文校验。"""
    items: list[QaItem] = []
    for seed in FAULT_SEEDS:
        table = _find_table(tables, str(seed["doc_id"]), str(seed["table_id"]))
        _assert_grounded(seed["must_contain"], table.rendered_text(), where=str(seed["qid"]))
        _assert_grounded([str(seed["anchor"])], table.rendered_text(), where=str(seed["qid"]))
        sections = [str(s) for s in seed["sections"]]
        _assert_grounded(sections, table.section_path, where=str(seed["qid"]))
        items.append(
            QaItem(
                qid=str(seed["qid"]),
                query=str(seed["query"]),
                expected_doc_id=table.doc_id,
                expected_sections=sections,
                must_contain=[normalize(t) for t in seed["must_contain"]],
                answer_hint=str(seed["answer_hint"]),
                tier="fault",
                source={
                    "doc_id": table.doc_id,
                    "table_id": table.table_id,
                    "caption": table.caption,
                    "page": table.page,
                    "section_path": table.section_path,
                    "anchor": str(seed["anchor"]),
                },
            )
        )
    return items


def mine_cross_doc_questions(tables: Sequence[CorpusTable]) -> list[QaItem]:
    """跨文档同概念题：主文档决定 hit 口径，两个文档的章节都可判 section 命中。"""
    items: list[QaItem] = []
    for seed in CROSS_DOC_SEEDS:
        primary = _find_table(tables, str(seed["primary_doc"]), str(seed["primary_table"]))
        secondary = _find_table(tables, str(seed["secondary_doc"]), str(seed["secondary_table"]))
        where = str(seed["qid"])
        _assert_grounded(seed["must_contain"], primary.rendered_text(), where=where)
        sections = [str(s) for s in seed["sections"]]
        _assert_grounded(
            [s for s in sections if s in {"4.3.2 输出特性", "4.3.3 保护功能", "4 保护功能"}],
            f"{primary.section_path} {secondary.section_path}",
            where=where,
        )
        items.append(
            QaItem(
                qid=where,
                query=str(seed["query"]),
                expected_doc_id=primary.doc_id,
                expected_sections=sections,
                must_contain=[normalize(t) for t in seed["must_contain"]],
                answer_hint=str(seed["answer_hint"]),
                tier="cross_doc",
                source={
                    "doc_id": primary.doc_id,
                    "table_id": primary.table_id,
                    "caption": primary.caption,
                    "page": primary.page,
                    "section_path": primary.section_path,
                    "related_doc_id": secondary.doc_id,
                    "related_table_id": secondary.table_id,
                },
            )
        )
    return items


# ---------------------------------------------------------------------------
# 组装 / 落盘
# ---------------------------------------------------------------------------


def build_dataset(
    tables: Sequence[CorpusTable],
    cards: dict[str, KnowledgeCard],
    *,
    min_per_doc: int,
) -> list[QaItem]:
    """挖掘全量题目: spec（表格自动）+ formula + fault + cross_doc。"""
    items: list[QaItem] = []
    items.extend(_mine_spec_balanced(tables, min_per_doc=min_per_doc))
    items.extend(mine_formula_questions(cards))
    items.extend(mine_fault_questions(tables))
    items.extend(mine_cross_doc_questions(tables))
    seen: set[str] = set()
    for item in items:
        if item.qid in seen:
            raise ValueError(f"qid 重复: {item.qid}")
        seen.add(item.qid)
    return items


def _mine_spec_balanced(
    tables: Sequence[CorpusTable], *, min_per_doc: int
) -> list[QaItem]:
    """每份文档轮转取代表性表，直到该文档凑够 ``min_per_doc`` 条题。

    轮转（round-robin）而不是"一张表出满再换"：题量摊到更多张表上，
    避免评测集被某一张表的内容主导。
    """
    by_doc: dict[str, list[CorpusTable]] = {}
    for table in tables:
        if not table.section_leaf or not table.rows:
            continue
        by_doc.setdefault(table.doc_id, []).append(table)

    items: list[QaItem] = []
    for doc_id in sorted(by_doc):
        ordered = _order_tables(by_doc[doc_id])
        pool = [
            (table, _questions_from_table(table, max_per_table=MAX_QUESTIONS_PER_TABLE))
            for table in ordered
        ]
        collected: list[QaItem] = []
        for depth in range(MAX_QUESTIONS_PER_TABLE):
            if len(collected) >= min_per_doc:
                break
            for _table, questions in pool:
                if len(collected) >= min_per_doc:
                    break
                if depth < len(questions):
                    collected.append(questions[depth])
        if len(collected) < min_per_doc:
            raise ValueError(
                f"文档 {doc_id} 只能挖出 {len(collected)} 条 spec 题，少于 min_per_doc={min_per_doc}"
            )
        items.extend(collected[:min_per_doc])
    return items


def read_dataset(path: Path) -> list[dict[str, Any]]:
    """读已有 jsonl（不存在 → 空列表）。"""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no} 不是合法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_no} 顶层应为对象")
        records.append(payload)
    return records


def write_dataset(records: Sequence[dict[str, Any]], path: Path) -> Path:
    """写 jsonl（UTF-8、不转义中文、LF 换行）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def _distribution(items: Sequence[QaItem]) -> dict[str, int]:
    counts = {tier: 0 for tier in TIERS}
    for item in items:
        counts[item.tier] = counts.get(item.tier, 0) + 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mine_eval_qa", description="从真实语料挖掘检索评测集（P1-7）"
    )
    parser.add_argument(
        "--out",
        default=None,
        help=f"评测集输出路径（默认 {settings.eval_dir / DEFAULT_QA_NAME}）",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="增量模式：保留已有条目，只补充缺失的 qid（按 qid 去重）",
    )
    parser.add_argument(
        "--min-per-doc",
        type=int,
        default=8,
        help="每份文档至少挖多少条 spec 题（默认 8）",
    )
    parser.add_argument(
        "--min-total", type=int, default=30, help="题目总数下限（默认 30）"
    )
    args = parser.parse_args(argv)

    out_path = Path(args.out) if args.out else Path(settings.eval_dir) / DEFAULT_QA_NAME
    try:
        tables = load_corpus_tables(Path(settings.corpus_dir))
        cards = load_knowledge_cards()
        items = build_dataset(tables, cards, min_per_doc=max(1, args.min_per_doc))
    except Exception as exc:  # noqa: BLE001 —— 出题失败必须显式，不产半成品
        print(f"[FAIL] mine_eval_qa: {type(exc).__name__}: {exc}")
        return 2

    if len(items) < args.min_total:
        print(f"[FAIL] 只挖出 {len(items)} 题，少于下限 {args.min_total}")
        return 2

    records = [item.to_dict() for item in items]
    if args.append:
        existing = read_dataset(out_path)
        known = {str(rec.get("qid")) for rec in existing}
        records = existing + [rec for rec in records if rec["qid"] not in known]
    write_dataset(records, out_path)

    by_tier = _distribution(items)
    by_doc: dict[str, int] = {}
    for item in items:
        by_doc[item.expected_doc_id] = by_doc.get(item.expected_doc_id, 0) + 1
    print(f"[OK] 写入 {out_path}")
    print(f"  题目总数: {len(records)}（本次挖掘 {len(items)}）")
    print(f"  tier 分布: {by_tier}")
    print("  期望文档分布:")
    for doc_id, count in sorted(by_doc.items()):
        print(f"    - {doc_id}: {count}")
    skipped = [
        (t.doc_id, t.table_id, t.caption, reason)
        for t in tables
        if (reason := alignment_issue(t)) is not None
    ]
    if skipped:
        print(f"  跳过表头/数据疑似错位的表（{len(skipped)} 张，不出题）:")
        for doc_id, table_id, caption, reason in skipped:
            print(f"    - {doc_id}/{table_id} {caption}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
