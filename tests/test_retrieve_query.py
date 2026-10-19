"""查询理解测试（P1-5 ``matrag.retrieve.query``，全离线）。

覆盖: 验收用例（型号/表号/章节抽取）、型号形状规则的防误判、别名表两种形状与
fail-fast、分词确定性、LLM 补位"只填空不改写 + 失败留痕"、``llm=None`` 绝不动 LLM。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from matrag.config import settings
from matrag.retrieve.query import (
    DEFAULT_ALIAS_PATH,
    FILTER_PRODUCT_CODE,
    FILTER_SECTION,
    FILTER_SECTION_CONTAINS,
    FILTER_TABLE,
    LEXICON_PATH_ENV,
    ModelAlias,
    ParsedQuery,
    _builtin_tokenize,
    _extract_filters,
    lexicon_path,
    load_model_alias_records,
    load_model_aliases,
    parse_query,
    reset_lexicon_cache,
    reset_tokenizer_cache,
    resolve_alias_path,
    section_title_hits,
    tokenizer_provenance,
)
from matrag.retrieve.routing import QueryKind, build_where, route

ACCEPTANCE_QUERY = "PA601-D54A 表11 输出特性的最大输出功率"


class ExplodingLLM:
    """任何被调用的方法都引爆——用来证明 ``llm=None`` 路径绝不碰 LLM。"""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"llm=None 时不允许访问 LLM 方法 {name!r}")


class FakeLLM:
    """鸭子类型 LLM 替身: 记录调用, 按脚本返回。"""

    def __init__(self, payload: Any = None, *, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, system: str, user: str, schema_hint: str = "") -> Any:
        self.calls.append({"system": system, "user": user, "schema_hint": schema_hint})
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.fixture(autouse=True)
def _no_alias_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认指向不存在的路径, 保证测试不依赖仓库真实数据。

    章节词典同样中性化: 主会话重建索引后 ``data/store/section_lexicon.json``
    会真实存在，"输出特性"等章节名会给 filters 添上 ``section_contains``，
    让未涉及章节名过滤的用例断言失真（按章节号/型号抽取的用例不受影响）。
    """
    monkeypatch.setenv("MATRAG_MODEL_ALIASES_PATH", str(Path(__file__).parent / "definitely-absent.yaml"))
    monkeypatch.setenv(
        "MATRAG_SECTION_LEXICON_PATH",
        str(Path(__file__).parent / "definitely-absent-lexicon.json"),
    )
    reset_lexicon_cache()
    yield
    reset_lexicon_cache()
    reset_tokenizer_cache()


# --------------------------------------------------------------------------- 验收


def test_parse_query_acceptance_model_table() -> None:
    """验收: 从"PA601-D54A 表11 输出特性的最大输出功率"抽出 product_code/表号。"""
    parsed = parse_query(ACCEPTANCE_QUERY)
    assert parsed.raw == ACCEPTANCE_QUERY
    assert parsed.product_code == "PA601-D54A"
    assert parsed.filters == {"product_code": "PA601-D54A", "table": 11}
    assert "输出特性" in parsed.keywords
    assert "输出功率" in parsed.keywords
    assert "的" not in parsed.keywords


def test_parse_query_acceptance_section_and_catalog() -> None:
    """验收: 章节号抽取 + 与路由联动的 "…是多少" → SPEC_VALUE。"""
    parsed = parse_query("PA601-D54A 4.3 功能/性能要求 的指标是多少")
    assert parsed.filters[FILTER_PRODUCT_CODE] == "PA601-D54A"
    assert parsed.filters[FILTER_SECTION] == "4.3"
    assert parsed.product_code == "PA601-D54A"
    assert route(parsed.raw).kind is QueryKind.SPEC_VALUE


def test_parse_query_catalog_question_routes_to_catalog() -> None:
    """"…有哪些指标" 是清单型问法 → CATALOG（与 SPEC_VALUE 区分）。"""
    routed = route("PA601-D54A 4.3 功能/性能要求 有哪些指标")
    assert routed.kind is QueryKind.CATALOG


def test_parse_query_all_three_filters_together() -> None:
    """型号 + 多级章节 + 表号同时出现, 三项都抽得出来。"""
    parsed = parse_query("CY0177-75V-20A 4.3.4 表9 的输出电压是多少")
    assert parsed.product_code == "CY0177-75V-20A"
    assert parsed.filters[FILTER_SECTION] == "4.3.4"
    assert parsed.filters[FILTER_TABLE] == 9


def test_parse_query_plain_question_has_no_filters() -> None:
    """普通问句: 无 filters、product_code 为 None, 但关键词分词照常。"""
    parsed = parse_query("最大输出功率是多少")
    assert parsed.filters == {}
    assert parsed.product_code is None
    assert parsed.keywords
    assert parsed.provenance["alias_hit"] is False


# --------------------------------------------------------------------------- 型号形状


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("GDC800T5054-UC 的输出特性", "GDC800T5054-UC"),
        ("CY0177-75V-20A 表3 保护功能", "CY0177-75V-20A"),
        ("PA601-D54A", "PA601-D54A"),
        ("安全要求按 GB4943.1-2011 执行", None),   # 标准编号不得误判为型号
        ("GB/T 731-2000 的测试方法", None),        # 单字母前缀不得误判
        ("效率和功率有什么关系", None),           # 纯中文无型号
    ],
)
def test_model_shape_extraction(query: str, expected: str | None) -> None:
    """型号只按形状识别: 真型号命中, 标准编号/口语不得误判。"""
    filters, product_code = _extract_filters(query)
    assert product_code == expected
    if expected is None:
        assert FILTER_PRODUCT_CODE not in filters
    else:
        assert filters[FILTER_PRODUCT_CODE] == expected


def test_section_ignores_standard_numbers() -> None:
    """标准编号 "GB/T 17626.2" / "GB4943.1" 不得被抠成章节号。"""
    assert FILTER_SECTION not in _extract_filters("GB/T 17626.2 静电放电测试")[0]
    assert FILTER_SECTION not in _extract_filters("GB4943.1 的安全要求")[0]


@pytest.mark.parametrize(
    ("query", "section"),
    [
        ("4.3 功能/性能要求", "4.3"),
        ("第4.3节 监控功能", "4.3"),
        ("4.3.4 指示灯", "4.3.4"),
        ("11.1 外形尺寸", "11.1"),
    ],
)
def test_section_extraction(query: str, section: str) -> None:
    """常见章节写法: 4.3 / 第4.3节 / 4.3.4 / 11.1。"""
    filters, _ = _extract_filters(query)
    assert filters[FILTER_SECTION] == section


@pytest.mark.parametrize(("query", "table"), [("表11 输出特性", 11), ("表 0 修改记录", 0), ("表 9", 9)])
def test_table_extraction(query: str, table: int) -> None:
    """表号: "表11" / "表 0" / "表 9" → 整数。"""
    filters, _ = _extract_filters(query)
    assert filters[FILTER_TABLE] == table


# --------------------------------------------------------------------------- 章节名（词典）


@pytest.fixture
def lexicon_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把章节词典指到 tmp 文件（含"输入特性"标题），并清掉模块级缓存。"""
    file = tmp_path / "section_lexicon.json"
    file.write_text(
        json.dumps({"titles": {"输入特性": ["4 > 4.3 > 4.3.1 输入特性"]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv(LEXICON_PATH_ENV, str(file))
    reset_lexicon_cache()
    yield file
    reset_lexicon_cache()


def test_section_name_filter_from_lexicon(lexicon_file: Path) -> None:
    """验收: 「输入特性 下的参数」命中章节词典 → filters 含 section_contains。"""
    parsed = parse_query("输入特性 下的参数")
    assert parsed.filters[FILTER_SECTION_CONTAINS] == "输入特性"
    assert FILTER_SECTION_CONTAINS == "section_contains"
    # 章节名是查询侧新键, 不与原有型号/表号/章节号抽取冲突
    assert parsed.filters == {"section_contains": "输入特性"}


def test_section_name_and_section_number_together(lexicon_file: Path) -> None:
    """验收: 「4.3.2 输出特性」的章节号经 build_where 变成 section_contains="4.3.2"。"""
    parsed = parse_query("4.3.2 输出特性")
    assert parsed.filters[FILTER_SECTION] == "4.3.2"
    assert build_where(parsed) == {"section_contains": "4.3.2"}


def test_section_name_missing_lexicon_adds_no_filter() -> None:
    """词典文件不存在 → 不加 section_contains（"没建词典"是正常状态, 不是错误）。"""
    assert parse_query("输入特性 下的参数").filters == {}
    assert section_title_hits("输入特性") == []


def test_section_name_provenance_records_path_and_hits(lexicon_file: Path) -> None:
    """溯源: 词典路径 + 本次命中的全部章节名（最长在前）都进 provenance。"""
    parsed = parse_query("输入特性 下的参数")
    assert parsed.provenance["lexicon_path"] == str(lexicon_file)
    assert parsed.provenance["section_title_hits"] == ["输入特性"]


def test_section_title_hits_uses_cache_until_reset(
    lexicon_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模块级缓存按路径隔离: 改同文件内容不重读; ``reset_lexicon_cache`` 后立即生效。"""
    assert section_title_hits("输入特性 是多少") == ["输入特性"]

    # 同一路径: 文件内容改了也不重读（缓存生效, 生产上词典与索引同生命周期）
    lexicon_file.write_text(json.dumps({"titles": {}}, ensure_ascii=False), encoding="utf-8")
    assert section_title_hits("输入特性 是多少") == ["输入特性"]

    # reset 后重读 → 新内容生效
    reset_lexicon_cache()
    assert section_title_hits("输入特性 是多少") == []


def test_section_title_hits_cache_is_per_path(tmp_path: Path) -> None:
    """不同路径各持一份缓存, 互不污染（测试可并行指向多个 tmp 词典）。"""
    first = tmp_path / "first.json"
    first.write_text(
        json.dumps({"titles": {"输入特性": ["4 > 4.3 > 4.3.1 输入特性"]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    other = tmp_path / "other.json"
    other.write_text(
        json.dumps({"titles": {"输出电压": ["5 > 5.1 输出电压"]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert section_title_hits("输入特性 是多少", path=first) == ["输入特性"]
    assert section_title_hits("输出电压 是多少", path=other) == ["输出电压"]
    # 两个路径的缓存互不影响
    assert section_title_hits("输出电压 是多少", path=first) == []
    assert section_title_hits("输入特性 是多少", path=other) == []


def test_lexicon_path_resolution_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """路径解析: 显式参数 > 环境变量 > settings.store_dir 默认。"""
    explicit = tmp_path / "explicit.json"
    assert lexicon_path(explicit) == explicit
    monkeypatch.setenv(LEXICON_PATH_ENV, str(tmp_path / "from-env.json"))
    assert lexicon_path() == tmp_path / "from-env.json"
    monkeypatch.delenv(LEXICON_PATH_ENV)
    default = lexicon_path()
    assert default.name == "section_lexicon.json"
    assert default.parent == settings.store_dir


def test_llm_fill_does_not_clobber_section_name(lexicon_file: Path) -> None:
    """LLM 补位不得覆盖词典确定的 section_contains（只填空字段）。"""
    parsed = parse_query("输入特性 下的参数", llm=FakeLLM({"section": "4.3"}))
    assert parsed.filters[FILTER_SECTION_CONTAINS] == "输入特性"
    assert parsed.filters[FILTER_SECTION] == "4.3"  # 章节号是空字段, 被补上
    assert parsed.provenance["llm_status"]["used"] is True


# --------------------------------------------------------------------------- 别名表


def test_load_model_aliases_missing_file_returns_empty(tmp_path: Path) -> None:
    """文件缺失 → 空表（未配置别名的正常状态，不是错误）。"""
    assert load_model_aliases(tmp_path / "absent.yaml") == {}


def test_load_model_aliases_mapping_form(tmp_path: Path) -> None:
    """顶层映射形状: aliases: {别名: product_code}。"""
    file = tmp_path / "aliases.yaml"
    file.write_text(yaml.safe_dump({"aliases": {"PA601": "PA601-D54A", "GDC": "GDC800T5054-UC"}}), encoding="utf-8")
    assert load_model_aliases(file) == {"PA601": "PA601-D54A", "GDC": "GDC800T5054-UC"}


def test_load_model_aliases_list_form(tmp_path: Path) -> None:
    """记录列表形状: [{alias: ..., product_code: ..., note: ...}]。"""
    file = tmp_path / "aliases.yaml"
    file.write_text(
        yaml.safe_dump([{"alias": "CY0177", "product_code": "CY0177-75V-20A", "note": "简称"}]),
        encoding="utf-8",
    )
    records = load_model_alias_records(file)
    assert records == [ModelAlias(alias="CY0177", product_code="CY0177-75V-20A", note="")]
    assert load_model_aliases(file) == {"CY0177": "CY0177-75V-20A"}


def test_load_model_aliases_empty_file(tmp_path: Path) -> None:
    """空文件（None 内容）→ 空表。"""
    file = tmp_path / "aliases.yaml"
    file.write_text("", encoding="utf-8")
    assert load_model_aliases(file) == {}


@pytest.mark.parametrize(
    "content",
    [
        "just-a-string",                                  # 既非 list 也非 aliases 映射
        "- PA601-D54A\n",                                  # 记录不是映射
        "- {alias: PA601}\n",                              # 缺 product_code
        "- {alias: '', product_code: PA601-D54A}\n",       # 别名为空
    ],
)
def test_load_model_aliases_malformed_fails_fast(tmp_path: Path, content: str) -> None:
    """别名表结构非法 → ValueError fail-fast（错误数据比没数据更危险）。"""
    file = tmp_path / "aliases.yaml"
    file.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_model_aliases(file)


def test_load_model_aliases_conflicting_alias_fails_fast(tmp_path: Path) -> None:
    """同一别名映射到两个编号 → ValueError。"""
    file = tmp_path / "aliases.yaml"
    file.write_text(
        "- {alias: PA601, product_code: PA601-D54A}\n"
        "- {alias: PA601, product_code: PA601-XYZ}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="冲突"):
        load_model_aliases(file)


def test_resolve_alias_path_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """路径解析: 显式参数 > 环境变量 > 仓库默认。"""
    explicit = tmp_path / "explicit.yaml"
    assert resolve_alias_path(explicit) == explicit
    monkeypatch.setenv("MATRAG_MODEL_ALIASES_PATH", str(tmp_path / "from-env.yaml"))
    assert resolve_alias_path() == tmp_path / "from-env.yaml"
    monkeypatch.delenv("MATRAG_MODEL_ALIASES_PATH")
    assert resolve_alias_path() == DEFAULT_ALIAS_PATH


def test_alias_canonicalizes_product_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """别名表命中 → product_code 填规范化编号（简称被改写）。"""
    file = tmp_path / "aliases.yaml"
    file.write_text(yaml.safe_dump({"aliases": {"PA601": "PA601-D54A"}}), encoding="utf-8")
    monkeypatch.setenv("MATRAG_MODEL_ALIASES_PATH", str(file))
    parsed = parse_query("PA601 表11 输出特性")
    assert parsed.product_code == "PA601-D54A"
    assert parsed.filters[FILTER_PRODUCT_CODE] == "PA601-D54A"
    assert parsed.provenance["alias_path"] == str(file)


def test_parse_query_uses_repo_default_alias_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """仓库默认路径在 :data:`DEFAULT_ALIAS_PATH`，且指向 generated 目录。"""
    monkeypatch.delenv("MATRAG_MODEL_ALIASES_PATH", raising=False)
    assert resolve_alias_path() == DEFAULT_ALIAS_PATH
    assert DEFAULT_ALIAS_PATH.name == "model_aliases.yaml"


# --------------------------------------------------------------------------- 分词


def test_tokenization_is_offline_and_deterministic() -> None:
    """分词确定性: 同输入同输出（无论走 P1-4 契约分词器还是内置 jieba）。"""
    query = "最大输出功率是多少？的输出特性的最大输出功率"
    first = parse_query(query).keywords
    second = parse_query(query).keywords
    assert first == second
    assert "输出特性" in first and "输出功率" in first
    assert all(any(ch.isalnum() for ch in tok) for tok in first)  # 无纯标点词项


def test_builtin_tokenize_drops_stopwords_and_dedupes() -> None:
    """内置退化分词: 停用词/纯标点剔除 + 去重保序（P1-4 契约缺失时的兜底）。"""
    assert _builtin_tokenize("最大输出功率是多少？的输出特性的最大输出功率") == [
        "最大", "输出功率", "输出特性",
    ]
    assert _builtin_tokenize("我 -的- + 是") == []


def test_tokenizer_falls_back_with_trace_when_contract_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-4 契约模块缺失 → 退化到内置 jieba, 并留下可查询的原因。"""
    reset_tokenizer_cache()
    monkeypatch.setitem(sys.modules, "matrag.index.keyword", None)  # import 即 ImportError
    try:
        info = tokenizer_provenance()
        assert info["source"] == "builtin_jieba"
        assert "matrag.index.keyword" in info["fallback_reason"]
        assert parse_query("输出电压是多少").keywords
    finally:
        reset_tokenizer_cache()


def test_tokenizer_provenance_records_source() -> None:
    """分词器留痕: 来源只能是 P1-4 契约或内置 jieba, 退化必须有原因。"""
    info = tokenizer_provenance()
    assert info["source"] in {"keyword_index", "builtin_jieba"}
    if info["source"] == "builtin_jieba":
        assert info["fallback_reason"]
    else:
        assert info["fallback_reason"] is None
    reset_tokenizer_cache()
    assert parse_query("输出电压").keywords  # 清缓存后仍可解析


def test_parsed_query_defaults_are_independent() -> None:
    """dataclass 默认值按实例隔离（mutable default 反证）。"""
    left, right = ParsedQuery(raw="a"), ParsedQuery(raw="b")
    left.filters["doc_id"] = "X"
    assert right.filters == {}


def test_model_alias_to_dict() -> None:
    """ModelAlias 导出 dict（CLI/报告序列化）。"""
    alias = ModelAlias(alias="PA601", product_code="PA601-D54A", note="简称")
    assert alias.to_dict() == {"alias": "PA601", "product_code": "PA601-D54A", "note": "简称"}


# --------------------------------------------------------------------------- LLM 补位


def test_llm_none_never_touches_llm() -> None:
    """llm=None → provenance 里连 llm_status 都没有，证明全程未触达 LLM。"""
    parsed = parse_query(ACCEPTANCE_QUERY)
    assert "llm_status" not in parsed.provenance
    # 反向证明 ExplodingLLM 不是哑巴（确保上面的断言有意义）
    with pytest.raises(AssertionError):
        ExplodingLLM().complete_json("x", "y")


def test_llm_fills_only_missing_fields() -> None:
    """LLM 补位: 只填确定性路径空着的字段，不改写已确定的结果。"""
    llm = FakeLLM({
        "keywords": ["效率", "转换损耗"],
        "product_code": "PA601-D54A",
        "section": "4.3",
        "table": 11,
    })
    parsed = parse_query(ACCEPTANCE_QUERY, llm=llm)
    assert llm.calls and llm.calls[0]["system"]
    # 已有确定性结果不许被 LLM 覆盖
    assert parsed.filters[FILTER_PRODUCT_CODE] == "PA601-D54A"
    assert parsed.filters[FILTER_TABLE] == 11
    # 空缺的章节被补上, 关键词只追加
    assert parsed.filters[FILTER_SECTION] == "4.3"
    assert "效率" in parsed.keywords and "转换损耗" in parsed.keywords
    status = parsed.provenance["llm_status"]
    assert status["used"] is True
    assert status["filled"]["section"] == "4.3"
    assert status["filled"]["keywords"] == ["效率", "转换损耗"]


def test_llm_fill_on_query_without_filters() -> None:
    """确定性一无所获时, LLM 补出 product_code/filters。"""
    llm = FakeLLM({"product_code": "GDC800T5054-UC", "table": 12})
    parsed = parse_query("输出特性那一块的内容", llm=llm)
    assert parsed.product_code == "GDC800T5054-UC"
    assert parsed.filters == {"product_code": "GDC800T5054-UC", "table": 12}


def test_llm_failure_leaves_trace_and_keeps_deterministic_result() -> None:
    """LLM 挂了: 留痕（used=False + error），检索仍拿到确定性结果。"""
    llm = FakeLLM(error=RuntimeError("MaaS 配额耗尽"))
    parsed = parse_query(ACCEPTANCE_QUERY, llm=llm)
    status = parsed.provenance["llm_status"]
    assert status["used"] is False
    assert "RuntimeError" in status["error"]
    assert parsed.product_code == "PA601-D54A"  # 确定性结果不受影响


def test_llm_non_dict_response_is_traced() -> None:
    """LLM 返回非 JSON 对象 → 留痕, 不抛。"""
    parsed = parse_query("输出电压", llm=FakeLLM(["not", "a", "dict"]))
    assert parsed.provenance["llm_status"]["used"] is False
    assert parsed.provenance["llm_status"]["error"]


def test_llm_non_numeric_table_is_dropped_with_trace() -> None:
    """LLM 给的 table 不是整数 → 丢弃并留痕。"""
    parsed = parse_query("特性表", llm=FakeLLM({"table": "表十一"}))
    assert FILTER_TABLE not in parsed.filters
    assert parsed.provenance["llm_dropped"]


def test_llm_narrow_signature_is_supported() -> None:
    """鸭子类型: 只收 (system, user) 两个位置参数的替身也能用。"""
    class NarrowLLM:
        def complete_json(self, system: str, user: str) -> dict[str, Any]:
            return {"keywords": ["上电时序"]}

    parsed = parse_query("上电顺序", llm=NarrowLLM())
    assert "上电时序" in parsed.keywords
