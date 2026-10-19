"""章节词典测试（P1-5 ``matrag.retrieve.lexicon``，全离线）。

覆盖: build_lexicon 聚合（规范化原键 + 去章节号附加键）、normalize_title 规范化
（空白/全角/大小写）、match_sections 命中判定与最长优先去重叠、save/load 往返、
文件缺失/损坏 → 空词典（不带病抛错但留痕）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from matrag.retrieve.lexicon import (
    SEGMENT_SEPARATOR,
    SectionLexicon,
    build_lexicon,
    load_lexicon,
    match_sections,
    normalize_title,
    save_lexicon,
)


def _rec(section_path: str) -> SimpleNamespace:
    """鸭子类型 chunk 替身（只要带 meta dict，与 ChunkRecord 同形）。"""
    return SimpleNamespace(meta={"section_path": section_path})


# --------------------------------------------------------------------------- build_lexicon


def test_build_lexicon_aggregates_titles_and_paths() -> None:
    """聚合: 每个层级标题都登记键, 值 = 出现该标题的完整 section_path。"""
    lex = build_lexicon([_rec("4 > 4.3 > 4.3.1 输入特性")])
    # 纯编号标题（"4.3"）不入会名过滤词典: 章节号由数字正则路径处理,
    # 会名过滤是子串匹配（LIKE '%4%' 会过宽召回）
    assert "4.3" not in lex.titles
    assert lex.titles["4.3.1 输入特性"] == ["4 > 4.3 > 4.3.1 输入特性"]
    # 附加键: 去首部章节号形式, 让"输入特性"也能命中
    assert lex.titles["输入特性"] == ["4 > 4.3 > 4.3.1 输入特性"]
    assert "4.3.1" not in lex.titles  # 纯编号去掉前缀后为空, 不登记


def test_build_lexicon_dedupes_and_keeps_order() -> None:
    """同一标题出现在多条记录: 去重保序, 不同路径都保留。"""
    lex = build_lexicon([
        _rec("4 > 4.3.1 输入特性"),
        _rec("4 > 4.3.1 输入特性"),  # 完全重复
        _rec("5 > 5.1 输入特性"),     # 同名不同路径
    ])
    assert lex.titles["输入特性"] == ["4 > 4.3.1 输入特性", "5 > 5.1 输入特性"]


def test_build_lexicon_skips_records_without_section_path() -> None:
    """无 section_path / meta 不是 dict / 无 meta 属性的记录静默跳过。"""
    lex = build_lexicon([
        SimpleNamespace(meta={}),
        SimpleNamespace(meta={"section_path": ""}),
        SimpleNamespace(meta=None),
        SimpleNamespace(),  # type: ignore[arg-type]
    ])
    assert lex.titles == {}


def test_segment_separator_matches_parse_layer() -> None:
    """分隔符与解析层的路径拼接一致（契约不变量）。"""
    assert SEGMENT_SEPARATOR == " > "


# --------------------------------------------------------------------------- normalize_title


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  4.3.1  输入特性 ", "4.3.1 输入特性"),   # 去空白 + 压缩内部空白
        ("４．３．１ 输入特性", "4.3.1 输入特性"),  # 全角数字/句点 → 半角
        ("ABC（Test）", "abc(test)"),               # 大小写 + 全角括号
        ("", ""),
        (None, ""),                                # type: ignore[arg-type]
    ],
)
def test_normalize_title_is_pure_normalization(raw: str, expected: str) -> None:
    """规范化不做语义处理, 只统一空白/全半角/大小写。"""
    assert normalize_title(raw) == expected


# --------------------------------------------------------------------------- match_sections


def test_match_sections_hits_title_and_numbered_title() -> None:
    """"输入特性" 与 "4.3.1 输入特性" 两种写法都命中同一章节。"""
    lex = build_lexicon([
        _rec("4 > 4.3 > 4.3.1 输入特性"),
        _rec("4 > 4.3 > 4.3.2 输出特性"),
    ])
    assert match_sections("输入特性 下的参数", lex) == ["输入特性"]
    assert match_sections("4.3.1 输入特性是多少", lex) == ["4.3.1 输入特性"]
    assert match_sections("完全不相关的查询", lex) == []


def test_match_sections_normalizes_query() -> None:
    """查询也走同一套规范化: 全角/大小写不影响命中。"""
    lex = build_lexicon([_rec("4 > 4.3.1 输入特性")])
    assert match_sections("４.３.１ 输入特性的参数", lex) == ["4.3.1 输入特性"]


def test_match_sections_keeps_longest_and_drops_overlap() -> None:
    """最长优先: "输入"被"输入特性"包含 → 只保留最长者（避免过滤条件互相稀释）。"""
    lex = SectionLexicon(titles={
        "输入": ["p"],
        "输入特性": ["p"],
        "输入特性参数": ["p"],
    })
    assert match_sections("输入特性参数是多少", lex) == ["输入特性参数"]


def test_match_sections_empty_query_and_lexicon() -> None:
    """空查询 / 空词典 → 空列表（"没有词典"是正常状态，不是错误）。"""
    lex = build_lexicon([_rec("4 > 4.3.1 输入特性")])
    assert match_sections("", lex) == []
    assert match_sections("输入特性", SectionLexicon()) == []
    assert match_sections("输入特性", SectionLexicon(titles={})) == []


# --------------------------------------------------------------------------- save / load


def test_save_load_roundtrip(tmp_path: Path) -> None:
    """save → load 往返一致, 落盘是 JSON 且顶层只有 titles 键。"""
    lex = build_lexicon([
        _rec("4 > 4.3 > 4.3.1 输入特性"),
        _rec("5 > 5.1 输出电压"),
    ])
    path = tmp_path / "section_lexicon.json"
    save_lexicon(lex, path)

    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"titles"}
    loaded = load_lexicon(path)
    assert loaded.titles == lex.titles


def test_save_lexicon_creates_parent_dirs(tmp_path: Path) -> None:
    """父目录不存在时自动创建（store_dir 可能还没建）。"""
    path = tmp_path / "nested" / "dir" / "section_lexicon.json"
    save_lexicon(SectionLexicon(titles={"输入特性": ["p"]}), path)
    assert path.is_file()
    assert load_lexicon(path).titles == {"输入特性": ["p"]}


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    """文件不存在 → 空词典（未建索引时的正常状态）。"""
    assert load_lexicon(tmp_path / "absent.json").titles == {}


def test_load_corrupt_file_returns_empty_with_trace(tmp_path: Path, caplog) -> None:
    """文件损坏 → 空词典且 warning 留痕（不带病抛错, 但绝不静默）。"""
    path = tmp_path / "bad.json"
    path.write_text("{ 这不是合法 JSON", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert load_lexicon(path).titles == {}
    assert caplog.records and "空词典" in caplog.text


def test_load_file_without_titles_mapping_returns_empty(tmp_path: Path, caplog) -> None:
    """JSON 合法但没有 titles 映射 → 空词典并留痕。"""
    path = tmp_path / "no_titles.json"
    path.write_text(json.dumps({"other": 1}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert load_lexicon(path).titles == {}
    assert caplog.records
