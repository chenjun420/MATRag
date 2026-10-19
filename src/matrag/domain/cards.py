"""域知识卡片：把 power 域知识文档（本仓数据资产）切成卡片语料。

知识源为 ``data/domain/source/power_knowledge.md`` —— 原 ATERag 域知识文档的
内联副本（本仓自包含, 运行时不再依赖 ATERag 目录）。

设计要点:
- 只做"内容搬运": 按 ``## `` 小节切分, 不做任何语义改写;
- 知识源路径可被环境变量 ``MATRAG_DOMAIN_KNOWLEDGE_PATH`` 覆盖
  (``MATRAG_`` 前缀与 :mod:`matrag.config` 的约定一致; 因 ``config.py``
  属公共契约文件, 本模块不往 ``Settings`` 里加字段, 直接读同名环境变量);
- 文件缺失一律 ``FileNotFoundError`` (fail-fast, 不静默返回空列表)。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# 知识源默认取本仓内联副本 (``data/domain/source/``), 不再指向 ATERag 目录;
# 需要替换知识源时仍可由环境变量覆盖。
DEFAULT_KNOWLEDGE_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "domain"
    / "source"
    / "power_knowledge.md"
)
DEFAULT_RULES_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "domain"
    / "source"
    / "power_rules.yaml"
)

KNOWLEDGE_PATH_ENV = "MATRAG_DOMAIN_KNOWLEDGE_PATH"
RULES_PATH_ENV = "MATRAG_DOMAIN_RULES_PATH"

KNOWLEDGE_SOURCE_LABEL = "MATRag power_knowledge.md"
RULES_SOURCE_LABEL = "MATRag power_rules.yaml"
DOMAIN_NAME = "power"

_HEADING_RE = re.compile(r"^##\s+(.*\S)\s*$")


@dataclass(frozen=True)
class DomainCard:
    """一张域知识卡片 = 知识文档里的一个 ``## `` 小节。

    - ``card_id``: 稳定 id, 形如 ``power-knowledge-3`` (按小节顺序 1 起编号);
    - ``title``: 源文里的原始标题行 (含 ``## `` 前缀);
    - ``section_path``: 归一化小节名 (去掉 ``## ``), 供引用回溯;
    - ``body``: 该小节的正文 (``### `` 子节随正文保留, 不再切分)。
    """

    card_id: str
    title: str
    section_path: str
    body: str
    source: str = KNOWLEDGE_SOURCE_LABEL
    domain: str = DOMAIN_NAME

    @property
    def char_count(self) -> int:
        """正文字符数 (不含标题)。"""
        return len(self.body)

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict (jsonl 一行一个)。"""
        return {
            "card_id": self.card_id,
            "title": self.title,
            "section_path": self.section_path,
            "body": self.body,
            "source": self.source,
            "domain": self.domain,
            "char_count": self.char_count,
        }


def resolve_knowledge_path(path: Path | str | None = None) -> Path:
    """解析知识文档路径: 显式参数 > 环境变量 > 仓内默认路径。"""
    if path is not None:
        return Path(path)
    override = os.environ.get(KNOWLEDGE_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return DEFAULT_KNOWLEDGE_PATH


def resolve_rules_path(path: Path | str | None = None) -> Path:
    """解析规则集路径: 显式参数 > 环境变量 > 仓内默认路径。"""
    if path is not None:
        return Path(path)
    override = os.environ.get(RULES_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return DEFAULT_RULES_PATH


def _split_sections(text: str) -> list[tuple[str, str]]:
    """按 ``## `` 切节, 返回 [(原始标题行, 正文)]。

    - 文档首个 ``# `` 一级标题及其后到第一个 ``## `` 之间的导入文字不计入卡片;
    - 正文为空的小节直接跳过 (不算卡片, 也不报错——空节没有语料价值)。
    """
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            if current is not None:
                sections.append((current[0], current[1]))
            current = [line, []]
            continue
        if current is None:
            continue  # 文档主标题/导入段
        current[1].append(line)
    if current is not None:
        sections.append((current[0], current[1]))

    out: list[tuple[str, str]] = []
    for title, lines in sections:
        body = "\n".join(lines).strip()
        if body:
            out.append((title, body))
    return out


def load_knowledge_cards(path: Path | str | None = None) -> list[DomainCard]:
    """读取知识文档并切成卡片列表。

    Args:
        path: 知识文档路径; 缺省按 :func:`resolve_knowledge_path` 解析。

    Returns:
        按文档顺序排列的 :class:`DomainCard` 列表 (至少一张)。

    Raises:
        FileNotFoundError: 知识文档不存在。
    """
    file = resolve_knowledge_path(path)
    if not file.is_file():
        raise FileNotFoundError(
            f"域知识文档不存在: {file} "
            f"(可用环境变量 {KNOWLEDGE_PATH_ENV} 或参数指定)"
        )
    text = file.read_text(encoding="utf-8")
    cards: list[DomainCard] = []
    for index, (title, body) in enumerate(_split_sections(text), start=1):
        section = _HEADING_RE.match(title)
        section_path = section.group(1).strip() if section else title.strip()
        cards.append(
            DomainCard(
                card_id=f"{DOMAIN_NAME}-knowledge-{index}",
                title=title,
                section_path=section_path,
                body=body,
            )
        )
    return cards


def load_domain_rules_raw(path: Path | str | None = None) -> list[dict[str, Any]]:
    """读取 rules.yaml 的原始规则列表 (dict, 不做任何加工)。

    Args:
        path: rules.yaml 路径; 缺省按 :func:`resolve_rules_path` 解析。

    Returns:
        顶层 ``rules`` 键下的 dict 列表 (当前 130 条)。

    Raises:
        FileNotFoundError: 规则文件不存在。
        ValueError: 文件结构不是 ``{"rules": [ {...}, ... ]}``。
    """
    file = resolve_rules_path(path)
    if not file.is_file():
        raise FileNotFoundError(
            f"域规则文件不存在: {file} "
            f"(可用环境变量 {RULES_PATH_ENV} 或参数指定)"
        )
    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "rules" not in data:
        raise ValueError(f"规则文件缺少顶层 rules 键: {file}")
    rules = data["rules"]
    if not isinstance(rules, list) or not rules:
        raise ValueError(f"规则文件 rules 为空或不是列表: {file}")
    for item in rules:
        if not isinstance(item, dict) or "id" not in item:
            raise ValueError(f"规则条目缺少 id 字段: {item!r}")
    return rules


def write_knowledge_cards(cards: list[DomainCard], out_path: Path | str) -> Path:
    """把卡片写成 jsonl (每行一个 dict, UTF-8, 不转义中文)。"""
    import json

    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as fh:
        for card in cards:
            fh.write(json.dumps(card.to_dict(), ensure_ascii=False) + "\n")
    return target
