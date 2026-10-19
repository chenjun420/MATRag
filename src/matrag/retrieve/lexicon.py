"""章节词典（P1-5 查询理解 → 索引过滤）: 从已切分 chunk 聚合"规范化章节标题"。

让"按章节名过滤检索"**数据驱动**: 章节清单来自索引里的 ``section_path``，
代码不写死任何具体章节名。

生命周期::

    建索引期一次:  build_lexicon(records) → save_lexicon(lex, path)
    查询期每查询:  load_lexicon(path) → match_sections(query, lex)

词典形状（JSON，见 :func:`save_lexicon` / :func:`load_lexicon`）::

    {"titles": {"输入特性": ["4 > 4.3 > 4.3.1 输入特性"], "输出电压": [...]}}

设计约束:

- **规范化不去语义**: :func:`normalize_title` 只做 NFKC（全角→半角）、压缩空白、
  转小写; 另产出"去首部章节号"的附加键（"4.3.1 输入特性" → "输入特性"），
  让查询无论带不带章节号都能命中;
- **最长优先去重叠**: :func:`match_sections` 多个标题同时命中时按长度降序，
  去掉被更长命中包含的短命中（"输入" 被 "输入特性" 包含）；
- **不带病抛错**: 词典文件缺失/损坏 → 空词典（没建词典是正常状态，不是错误），
  "为什么不生效"的退化留痕由调用方（查询理解的 provenance）负责。
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: ``section_path`` 的层级分隔符（与 matrag.parse.markdown_reader 的路径拼接一致）
#: 纯章节号形态（不入会名过滤词典，见 _is_pure_number）
_PURE_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)*$")
SEGMENT_SEPARATOR = " > "

#: 首部章节号: "4.3.1 输入特性" 的 "4.3.1" / "4 技术要求" 的 "4"
_LEADING_NUMBER_RE = re.compile(r"^\s*[0-9]+(?:\.[0-9]+)*\s*")


@dataclass
class SectionLexicon:
    """章节词典: 规范化标题 → 出现该标题的 ``section_path`` 列表。

    - ``titles``: 键 = 规范化标题（含"去章节号"附加键，见 :func:`build_lexicon`），
      值 = 出现该标题的完整 ``section_path``（保序去重）。
    """

    titles: dict[str, list[str]] = field(default_factory=dict)


def normalize_title(title: str) -> str:
    """规范化章节标题: NFKC（全角数字/标点→半角）+ 压缩空白 + 转小写。

    只做规范化、**不去语义**——"4.3.1 输入特性" 与 "４．３．１ 输入特性"
    都归一到 "4.3.1 输入特性"，是否真是章节由数据（词典来源）决定。
    """
    text = unicodedata.normalize("NFKC", str(title or ""))
    return re.sub(r"\s+", " ", text).strip().lower()


def _is_pure_number(title: str) -> bool:
    """纯章节号标题（如 "4" / "11.1" / "4.3.2"）。

    不入"会名过滤词典": 章节号由 query.py 的数字正则路径抽取（filter 键为
    section, 精确到点号层级）, 而会名过滤是子串匹配 —— 让 LLM 提议
    section_contains="4" 会退化成 ``LIKE '%4%'`` 的过宽召回。
    """
    return bool(_PURE_NUMBER_RE.match(title))


def _without_leading_number(normalized: str) -> str | None:
    """去掉规范化标题的首部章节号: "4.3.1 输入特性" → "输入特性"。

    无首部章节号、或去掉后为空（纯编号标题如 "4.3"）时返回 ``None``。
    """
    stripped = _LEADING_NUMBER_RE.sub("", normalized, count=1).strip()
    return stripped or None


def build_lexicon(records: Iterable[Any]) -> SectionLexicon:
    """从 chunk 序列聚合章节词典（读每条记录的 ``meta["section_path"]``）。

    每个层级标题登记两个键: 规范化原形 + "去章节号"附加键（两者不同才有意义）。
    于是查询 "输入特性" 命中键 "输入特性"，查询 "4.3.1 输入特性" 命中键
    "4.3.1 输入特性"——两种写法都能过滤到同一批 chunk。

    ``records`` 只要带 ``meta`` dict 即可（鸭子类型: :class:`ChunkRecord`
    或任何相似结构），无 ``section_path`` 的记录跳过。
    """
    titles: dict[str, list[str]] = {}

    def _register(key: str, section_path: str) -> None:
        bucket = titles.setdefault(key, [])
        if section_path not in bucket:  # 保序去重，保证 save 输出确定
            bucket.append(section_path)

    for record in records:
        meta = getattr(record, "meta", None)
        if not isinstance(meta, dict):
            continue
        section_path = str(meta.get("section_path") or "")
        if not section_path:
            continue
        for segment in section_path.split(SEGMENT_SEPARATOR):
            normalized = normalize_title(segment)
            if not normalized or _is_pure_number(normalized):
                continue  # 纯编号标题不入会名过滤词典（见 _is_pure_number 说明）
            _register(normalized, section_path)
            alias = _without_leading_number(normalized)
            if alias and alias != normalized:
                _register(alias, section_path)
    return SectionLexicon(titles=titles)


def save_lexicon(lex: SectionLexicon, path: Path) -> None:
    """词典写盘（JSON ``{"titles": {...}}``，键排序保证输出确定）。"""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        json.dumps({"titles": lex.titles}, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_lexicon(path: Path) -> SectionLexicon:
    """读取词典; 文件缺失 / 损坏 → 空词典（不带病抛错）。

    损坏（读取失败 / JSON 非法 / ``titles`` 不是映射）走 warning 留痕后按空词典
    继续——没词典只会让"按章节名过滤"不生效，不该让整个检索崩掉。
    """
    file = Path(path)
    if not file.is_file():
        return SectionLexicon()
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning(
            "章节词典损坏，按空词典处理（按章节名过滤不生效）: path=%s error=%s: %s",
            file, type(exc).__name__, exc,
        )
        return SectionLexicon()
    raw = data.get("titles") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        logger.warning("章节词典缺少 titles 映射，按空词典处理: path=%s", file)
        return SectionLexicon()

    titles: dict[str, list[str]] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            titles[str(key)] = [value]
        elif isinstance(value, list):
            titles[str(key)] = [str(item) for item in value]
    return SectionLexicon(titles=titles)


def match_sections(query: str, lex: SectionLexicon) -> list[str]:
    """查询命中的规范化标题（按长度降序、最长优先、去掉被包含的短命中）。

    命中判定 = 规范化标题是规范化查询的子串（query="输入特性 下的参数" 含
    "输入特性"）。无词典 / 无命中 → 空列表; 调用方据此决定是否加过滤条件。
    """
    normalized_query = normalize_title(query)
    if not normalized_query or not lex.titles:
        return []
    hits = sorted(
        (title for title in lex.titles if title and title in normalized_query),
        key=len,
        reverse=True,
    )
    kept: list[str] = []
    for hit in hits:
        # "输入" 已被更长的 "输入特性" 包含 → 只保留最长者，避免过滤条件互相稀释
        if any(hit in longer for longer in kept):
            continue
        kept.append(hit)
    return kept
