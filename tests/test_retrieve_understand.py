"""语义查询理解测试（P1-5 ``matrag.retrieve.understand``，全离线 StubLLM）。

覆盖: 正则+LLM 合并、LLM 不可用/未配置降级留痕、LLM 输出不可解析降级、
精确引用不可被覆盖、检索查询去重保序与原查询置顶、needs_rules 规则词表、
提示词接地（章节词表 + 正则线索）、章节名防编造、脏字段留痕。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from matrag.errors import JSONParseError
from matrag.retrieve.lexicon import SectionLexicon, build_lexicon, save_lexicon
from matrag.retrieve.query import (
    FILTER_PRODUCT_CODE,
    FILTER_SECTION_CONTAINS,
    FILTER_TABLE,
    ParsedQuery,
    reset_lexicon_cache,
)
from matrag.retrieve.routing import QueryKind
from matrag.retrieve.understand import (
    MAX_LEXICON_TITLES,
    MAX_REWRITES,
    PROV_LLM,
    PROV_MERGED,
    PROV_REGEX,
    RULE_HINT_KEYWORDS,
    QueryPlan,
    _lexicon_titles,
    iter_degradation_kinds,
    merge_regex,
    regex_only_plan,
    rule_hint_hits,
    understand,
)

# ---- 测试语料（中性化: 别名表/章节词典都指向 tmp，不依赖仓库真实数据）----
ALIAS_CODE = "PA601-D54A"
QUERY = f"{ALIAS_CODE} 表11 输出特性的最大输出功率"
LEXICON_TITLES = ["输入特性", "输出特性", "输出电压", "效率", "安规要求"]


class _Section:
    """``build_lexicon`` 只要 meta["section_path"]，这里给个最小替身。"""

    def __init__(self, path: str) -> None:
        self.meta = {"section_path": path}


def _write_lexicon(tmp_path, *sections) -> Path:
    """写一份章节词典（tmp）并返回路径。"""
    lex = build_lexicon(sections)
    path = tmp_path / "section_lexicon.json"
    save_lexicon(lex, path)
    reset_lexicon_cache()
    return path


def _write_catalog(tmp_path) -> Path:
    """写一份产品目录（型号接地用）并返回路径。"""
    from matrag.retrieve.catalog import ProductCatalog, save_catalog

    path = tmp_path / "product_catalog.json"
    save_catalog(
        ProductCatalog(products={
            ALIAS_CODE: [f"{ALIAS_CODE} X"],
            "CY0177-75V-20A": ["CY0177-75V-20A"],
        }),
        path,
    )
    return path


class StubLLM:
    """鸭子类型 LLM 替身: 记录 prompt, 按脚本返回或抛错。"""

    def __init__(
        self,
        payload: Any = None,
        *,
        error: Exception | None = None,
        configured: bool = True,
    ) -> None:
        self.payload = payload
        self.error = error
        self.configured = configured
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, system: str, user: str, schema_hint: str = "") -> Any:
        self.calls.append({"system": system, "user": user, "schema_hint": schema_hint})
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """别名表与章节词典都中性化到 tmp_path，并清词典缓存。"""
    alias_file = tmp_path / "aliases.yaml"
    alias_file.write_text(
        f"aliases:\n  PA601: {ALIAS_CODE}\n", encoding="utf-8"
    )
    lexicon_file = tmp_path / "section_lexicon.json"
    # 用纯文字父路径，避免 build_lexicon 把 "4" / "4.3" 这类纯编号段也登记成标题
    # （match_sections 是子串匹配，纯编号标题会误命中 "PA601-D54A" 里的数字）。
    save_lexicon(
        build_lexicon([_Section(f"技术要求 > {title}") for title in LEXICON_TITLES]),
        lexicon_file,
    )
    monkeypatch.setenv("MATRAG_MODEL_ALIASES_PATH", str(alias_file))
    monkeypatch.setenv("MATRAG_SECTION_LEXICON_PATH", str(lexicon_file))
    reset_lexicon_cache()
    yield
    reset_lexicon_cache()


def _payload(**overrides: Any) -> dict[str, Any]:
    """一份"正常"的 LLM 输出（各用例只覆盖关心的字段）。"""
    base: dict[str, Any] = {
        "intent": "查该型号输出特性的最大输出功率",
        "kind": "spec_value",
        "entities": {"parameter": ["最大输出功率"], "component": [], "standard": [], "phenomenon": []},
        "keywords": ["输出功率", "输出特性"],
        "rewrite_queries": ["PA601-D54A 输出特性 最大输出功率", "输出功率 上限"],
        "sub_queries": [],
        "needs_rules": False,
        "reasoning": "用户问的是限值类规格，先按章节与表号定位再取最大值",
        "confidence": 0.86,
    }
    base.update(overrides)
    return base


def _degradation(plan: QueryPlan, kind: str) -> dict[str, Any]:
    return next(item for item in plan.degradations if item["kind"] == kind)


# ---------------------------------------------------------------- 合并


def test_llm_plan_merges_with_regex_plan() -> None:
    """正则线索 + LLM 语义字段合并成完整计划。"""
    stub = StubLLM(_payload())
    plan = understand(QUERY, llm=stub)

    # 语义字段来自 LLM
    assert plan.intent == "查该型号输出特性的最大输出功率"
    assert plan.entities["parameter"] == ["最大输出功率"]
    assert plan.needs_rules is False
    assert plan.reasoning == "用户问的是限值类规格，先按章节与表号定位再取最大值"
    assert plan.confidence == 0.86

    # 正则精确引用原样保留
    assert plan.filters[FILTER_PRODUCT_CODE] == ALIAS_CODE
    assert plan.filters[FILTER_TABLE] == 11
    assert plan.filters[FILTER_SECTION_CONTAINS] == "输出特性"

    # 关键词/检索查询: 正则在前、LLM 补后
    assert "输出功率" in plan.keywords
    assert plan.retrieval_queries[0] == QUERY
    assert "PA601-D54A 输出特性 最大输出功率" in plan.retrieval_queries[1:]

    # provenance 字段级可见: 精确引用 regex, 语义字段 llm, 一致处 merged
    assert plan.provenance["filters.product_code"] == PROV_REGEX
    assert plan.provenance["intent"] == PROV_LLM
    assert plan.provenance["kind"] == PROV_MERGED  # LLM 与正则路由判定一致
    assert plan.degradations == []


def test_malformed_llm_fields_are_traced_not_swallowed() -> None:
    """脏字段（filters 非对象/kind 非法/confidence 非数）全部留痕, 且不回退整条结果。"""
    stub = StubLLM(
        {
            "intent": "",
            "kind": "编造的意图类型",
            "confidence": "很高",
            "needs_rules": "是",
            "entities": "不是对象",
            "filters": "不是对象",
            "rewrite_queries": "也不是数组",
        }
    )
    plan = understand("这个电源的可靠性指标是多少", llm=stub)

    dropped = [item["field"] for item in plan.degradations if item["kind"] == "llm_field_dropped"]
    assert set(dropped) >= {"intent", "kind", "confidence", "needs_rules", "entities", "filters"}
    # 脏字段只丢自己: kind/置信度退回正则基线, 其余计划照常可用
    assert plan.kind == regex_only_plan("这个电源的可靠性指标是多少").kind
    assert plan.confidence <= 0.6          # 正则基线封顶, 没被 "很高" 污染
    assert plan.retrieval_queries == ["这个电源的可靠性指标是多少"]


def test_llm_can_add_new_sub_queries_and_needs_rules() -> None:
    """多跳拆解 + 规则域问题: sub_queries 与 needs_rules 都应落进计划。"""
    stub = StubLLM(
        _payload(
            kind="formula",
            sub_queries=["先查输出功率限值", "再按效率曲线换算实际输出"],
            needs_rules=True,
        )
    )
    plan = understand(QUERY, llm=stub)

    assert plan.kind == QueryKind.FORMULA.value
    assert plan.sub_queries == ["先查输出功率限值", "再按效率曲线换算实际输出"]
    assert plan.needs_rules is True
    assert plan.provenance["sub_queries"] == PROV_LLM


def test_rule_keyword_hit_beats_llm_false() -> None:
    """规则域关键词命中即置 needs_rules（确定性优先于 LLM 判断）。"""
    stub = StubLLM(_payload(needs_rules=False))
    plan = understand(f"{ALIAS_CODE} 的输出纹波是多少", llm=stub)

    assert plan.needs_rules is True
    assert plan.provenance["needs_rules"] == PROV_REGEX


# ---------------------------------------------------------------- LLM 不可用


def test_llm_none_returns_regex_plan_with_degradation() -> None:
    """llm=None: 纯正则计划 + llm_unavailable 留痕, 且绝不能碰 LLM。"""
    plan = understand(QUERY)

    assert plan.filters[FILTER_PRODUCT_CODE] == ALIAS_CODE
    assert plan.filters[FILTER_TABLE] == 11
    assert plan.retrieval_queries == [QUERY]
    assert plan.sub_queries == []
    assert plan.entities == {}
    assert "未做语义推理" in plan.reasoning
    assert _degradation(plan, "llm_unavailable")["kind"] == "llm_unavailable"
    assert set(plan.provenance.values()) == {PROV_REGEX}
    assert plan.provenance["filters.product_code"] == PROV_REGEX


def test_unconfigured_llm_is_degraded_not_called() -> None:
    """configured=False: 与 llm=None 同路径, 且一次都不调用。"""
    stub = StubLLM(_payload(), configured=False)
    plan = understand(QUERY, llm=stub)

    assert stub.calls == []
    assert list(iter_degradation_kinds(plan)) == ["llm_unavailable"]
    assert "未做语义推理" in plan.reasoning


def test_regex_only_plan_is_deterministic() -> None:
    """regex_only_plan: 两次调用结果一致, provenance 全 regex, 无降级。"""
    first, second = regex_only_plan(QUERY), regex_only_plan(QUERY)
    assert first == second
    assert set(first.provenance.values()) == {PROV_REGEX}
    assert first.degradations == []
    assert first.retrieval_queries == [QUERY]
    assert first.kind == QueryKind.SPEC_VALUE.value


# ---------------------------------------------------------------- LLM 失败


def test_json_parse_error_degrades_but_keeps_regex_filters() -> None:
    """LLM 输出不可解析: 退回正则结果 + llm_failed 留痕, 检索仍可用。"""
    stub = StubLLM(error=JSONParseError("no JSON object in response", "抱歉我无法回答"))
    plan = understand(QUERY, llm=stub)

    assert plan.filters[FILTER_PRODUCT_CODE] == ALIAS_CODE
    assert plan.filters[FILTER_TABLE] == 11
    assert plan.retrieval_queries == [QUERY]          # 仍可直接拿去检索
    note = _degradation(plan, "llm_failed")
    assert "JSONParseError" in note["detail"]
    assert note["raw"] == "抱歉我无法回答"
    assert "未做语义推理" in plan.reasoning


def test_timeout_degrades_with_truncated_detail() -> None:
    """超时等其它异常同样只留痕不抛, detail 原样截断。"""
    stub = StubLLM(error=TimeoutError("x" * 500))
    plan = understand(QUERY, llm=stub)

    note = _degradation(plan, "llm_failed")
    assert note["detail"].startswith("TimeoutError:")
    assert len(note["detail"]) <= 320
    assert list(iter_degradation_kinds(plan)) == ["llm_failed"]


def test_non_dict_output_degrades() -> None:
    """LLM 返回非对象（list/字符串）→ llm_failed, 不崩溃。"""
    plan = understand(QUERY, llm=StubLLM(["不是对象"]))

    assert _degradation(plan, "llm_failed")["detail"].startswith("LLM 未返回 JSON 对象")
    assert plan.filters[FILTER_PRODUCT_CODE] == ALIAS_CODE


# ---------------------------------------------------------------- 精确引用优先


def test_llm_cannot_override_exact_references() -> None:
    """LLM 试图改写型号/章节名/表号 → 全部保留正则值并留痕。"""
    stub = StubLLM(
        _payload(
            product_code="PA602-ZZ99",                 # 顶层同形键
            filters={"product_code": "PA603-BB00", "section_contains": "安规要求", "table": 99},
        )
    )
    plan = understand(QUERY, llm=stub)

    assert plan.filters[FILTER_PRODUCT_CODE] == ALIAS_CODE   # 正则值胜出
    assert plan.filters[FILTER_TABLE] == 11
    assert plan.filters[FILTER_SECTION_CONTAINS] == "输出特性"
    assert plan.provenance["filters.product_code"] == PROV_REGEX
    assert plan.provenance["filters.table"] == PROV_REGEX
    ignored = [item for item in plan.degradations if item["kind"] == "llm_filter_ignored"]
    assert {item["key"] for item in ignored} >= {
        FILTER_PRODUCT_CODE,
        FILTER_SECTION_CONTAINS,
        FILTER_TABLE,
    }
    assert all(item["kept"] != item["value"] for item in ignored)


def test_merge_regex_restores_precision_keys() -> None:
    """merge_regex 是最后一道闸门: 直接喂被改脏的计划也要被拉回正则值。"""
    parsed = ParsedQuery(
        raw=QUERY,
        keywords=["输出特性", "最大输出功率"],
        filters={FILTER_PRODUCT_CODE: ALIAS_CODE, FILTER_TABLE: 11},
        product_code=ALIAS_CODE,
    )
    dirty = QueryPlan(
        raw=QUERY,
        kind=QueryKind.SPEC_VALUE.value,
        intent="LLM 编的意图",
        filters={FILTER_PRODUCT_CODE: "PA602-ZZ99", FILTER_TABLE: 99, "caption": "输出特性表"},
        keywords=["功率"],
        retrieval_queries=[QUERY],
        provenance={"filters.product_code": PROV_LLM, "filters.table": PROV_LLM},
    )
    merged = merge_regex(dirty, parsed)

    assert merged.filters[FILTER_PRODUCT_CODE] == ALIAS_CODE
    assert merged.filters[FILTER_TABLE] == 11
    assert merged.filters["caption"] == "输出特性表"   # LLM 新增键保留
    assert merged.provenance["filters.product_code"] == PROV_REGEX
    assert merged.provenance["filters.table"] == PROV_REGEX
    assert merged.intent == "LLM 编的意图"            # 语义字段不被正则覆盖
    assert merged.keywords == ["输出特性", "最大输出功率", "功率"]
    # 入参不被改（merge_regex 必须是纯函数语义）
    assert dirty.filters[FILTER_PRODUCT_CODE] == "PA602-ZZ99"


def test_llm_section_title_outside_lexicon_is_rejected() -> None:
    """编造的章节名（不在词表内）必须拒绝, 否则 LIKE 过滤直接零召回。"""
    stub = StubLLM(
        _payload(
            kind="unknown",
            filters={"section_contains": "根本不存在的一章"},
        )
    )
    plan = understand("这个电源的可靠性指标是多少", llm=stub)

    assert "section_contains" not in plan.filters
    rejected = [item for item in plan.degradations if item["kind"] == "llm_filter_ignored"]
    assert rejected and rejected[0]["value"] == "根本不存在的一章"


def test_llm_section_title_inside_lexicon_is_accepted(tmp_path) -> None:
    """词表内的章节名 + 型号根路径对得上 → 接受, 记 merged。"""
    lex_path = _write_lexicon(
        tmp_path, _Section(f"{ALIAS_CODE} X > 4 技术要求 > 4.3 功能性能要求 > 4.3.2 输出特性")
    )
    cat_path = _write_catalog(tmp_path)
    stub = StubLLM(_payload(kind="unknown", filters={"section_contains": "输出特性"}))
    plan = understand(
        f"{ALIAS_CODE} 的全要求有哪些", llm=stub,
        lexicon_path=lex_path, catalog_path=cat_path,
    )

    assert plan.filters[FILTER_SECTION_CONTAINS] == "输出特性"
    assert plan.provenance["filters.section_contains"] == PROV_LLM
    assert plan.provenance["filters"] == PROV_MERGED


def test_llm_section_of_other_product_is_rejected(tmp_path) -> None:
    """章节名在词表内但属于其他型号 → 拒绝（错过滤 = 零召回）。"""
    lex_path = _write_lexicon(tmp_path, _Section("CY0177-75V-20A 规格书 > 4 保护功能"))
    cat_path = _write_catalog(tmp_path)
    stub = StubLLM(_payload(kind="unknown", filters={"section_contains": "保护功能"}))
    plan = understand(
        f"{ALIAS_CODE} 的保护在哪里", llm=stub,
        lexicon_path=lex_path, catalog_path=cat_path,
    )

    assert FILTER_SECTION_CONTAINS not in plan.filters
    out = [d for d in plan.degradations if d["kind"] == "llm_section_out_of_product"]
    assert out and out[0]["value"] == "保护功能"


def test_llm_section_without_ground_in_query_is_dropped(tmp_path) -> None:
    """
    无型号 + 查询文本里没该章节名 → 拒绝虚构过滤。

    实测：公式类问题（无型号）被 LLM 悬上别的文档章节名，
    过滤会归零。
    """
    lex_path = _write_lexicon(tmp_path, _Section("CY0177-75V-20A 规格书 > 4.3 输出特性"))
    cat_path = _write_catalog(tmp_path)
    stub = StubLLM(_payload(kind="formula", filters={"section_contains": "输出特性"}))
    plan = understand("纹波怎么算", llm=stub, lexicon_path=lex_path, catalog_path=cat_path)

    assert FILTER_SECTION_CONTAINS not in plan.filters
    out = [d for d in plan.degradations if d["kind"] == "llm_section_ungrounded"]
    assert out and out[0]["value"] == "输出特性"
    # 不能因为丢了过滤而进行加场：检索式仍含原查询
    assert plan.retrieval_queries[0] == "纹波怎么算"


def test_llm_section_kept_when_query_text_mentions_it(tmp_path) -> None:
    """查询文本里本就写了章节名 → 接受（有依据）。"""
    lex_path = _write_lexicon(tmp_path, _Section("CY0177-75V-20A 规格书 > 4.3 输出特性"))
    cat_path = _write_catalog(tmp_path)
    stub = StubLLM(_payload(kind="spec_value", filters={"section_contains": "输出特性"}))
    plan = understand("输出特性的最大输出功率", llm=stub,
                      lexicon_path=lex_path, catalog_path=cat_path)
    assert plan.filters.get(FILTER_SECTION_CONTAINS) == "输出特性"
    assert not [d for d in plan.degradations if d["kind"] == "llm_section_ungrounded"]


def test_llm_product_code_is_grounded_in_catalog(tmp_path) -> None:
    """LLM 提的型号：不在目录内拒绝；可前缀解析则规范化。"""
    lex_path = _write_lexicon(tmp_path)
    cat_path = _write_catalog(tmp_path)
    bogus = StubLLM(_payload(kind="unknown", filters={FILTER_PRODUCT_CODE: "XXX-9999"}))
    plan_bad = understand(
        "这个型号的输出特性", llm=bogus,
        lexicon_path=lex_path, catalog_path=cat_path,
    )
    assert FILTER_PRODUCT_CODE not in plan_bad.filters
    assert any(d["kind"] == "llm_filter_ignored" for d in plan_bad.degradations)

    prefix = StubLLM(_payload(kind="unknown", filters={FILTER_PRODUCT_CODE: "CY0177"}))
    plan_ok = understand(
        "这个型号的输出特性", llm=prefix,
        lexicon_path=lex_path, catalog_path=cat_path,
    )
    assert plan_ok.filters[FILTER_PRODUCT_CODE] == "CY0177-75V-20A"
    assert any(d["kind"] == "llm_product_resolved" for d in plan_ok.degradations)


def test_rewrite_dedup_order_and_raw_first() -> None:
    """改写去重保序 + 原查询恒在 index 0 + 条数上限。"""
    stub = StubLLM(
        _payload(
            rewrite_queries=[QUERY, "  输出功率 上限  ", "输出功率 上限", "最大输出功率是多少", "第三种问法"]
        )
    )
    plan = understand(QUERY, llm=stub)

    assert plan.retrieval_queries[0] == QUERY
    assert plan.retrieval_queries.count(QUERY) == 1
    assert plan.retrieval_queries == [QUERY, "输出功率 上限", "最大输出功率是多少", "第三种问法"]
    assert len(plan.retrieval_queries) - 1 <= MAX_REWRITES


def test_sub_queries_dedup_against_retrieval_queries() -> None:
    """子问题与原查询/改写重复时去重。"""
    stub = StubLLM(_payload(sub_queries=[QUERY, "查输出特性表", "查输出特性表", "核对效率"]))
    plan = understand(QUERY, llm=stub)

    assert plan.sub_queries == ["查输出特性表", "核对效率"]


def test_empty_query_is_handled() -> None:
    """空查询不炸, 且不产生空的检索串。"""
    plan = understand("   ", llm=StubLLM(_payload(rewrite_queries=["任意问法"])))

    assert plan.raw == ""
    assert plan.retrieval_queries == ["任意问法"]


# ---------------------------------------------------------------- needs_rules 词表


@pytest.mark.parametrize("question", ["效率是多少", "输出纹波多大", "掉电保持时间", "浪涌耐受", "回差", "门限值"])
def test_needs_rules_true_by_keyword_table(question: str) -> None:
    """规则域关键词表命中 → needs_rules=True。"""
    assert rule_hint_hits(question), f"{question!r} 应当命中规则词表"
    assert regex_only_plan(question).needs_rules is True


@pytest.mark.parametrize("question", ["有哪些型号", "怎么接线", "这个表格里写了什么"])
def test_needs_rules_false_when_no_rule_keyword(question: str) -> None:
    """未命中 → needs_rules=False。"""
    assert rule_hint_hits(question) == []
    assert regex_only_plan(question).needs_rules is False


def test_rule_hint_keywords_have_no_rule_ids() -> None:
    """规则词表只是"领域词", 不含具体规则 id（数据驱动, 不写死规则分支）。"""
    assert "效率" in RULE_HINT_KEYWORDS and "纹波" in RULE_HINT_KEYWORDS
    assert not any("-" in word or "/" in word for word in RULE_HINT_KEYWORDS)


# ---------------------------------------------------------------- 提示词接地


def test_prompt_is_grounded_with_lexicon_and_regex_clues() -> None:
    """提示词必须含章节词表标题与正则层线索（接地, 不让模型凭记忆编章节名）。"""
    stub = StubLLM(_payload())
    understand(QUERY, llm=stub)

    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert "章节词表" in call["system"] and "章节词表" in call["user"]
    for title in LEXICON_TITLES:
        assert title in call["user"], f"章节词表标题 {title!r} 未进入提示词"
    assert f"{FILTER_PRODUCT_CODE}: {ALIAS_CODE}" in call["user"]
    assert f"{FILTER_TABLE}: 11" in call["user"]
    assert f"{FILTER_SECTION_CONTAINS}: 输出特性" in call["user"]
    assert QUERY in call["user"]
    assert "rewrite_queries" in call["schema_hint"]


def test_prompt_without_lexicon_says_no_titles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """没有词典时提示词明说"不要输出章节名"（而不是静默给空表）。"""
    monkeypatch.setenv("MATRAG_SECTION_LEXICON_PATH", str(tmp_path / "absent-lexicon.json"))
    reset_lexicon_cache()
    stub = StubLLM(_payload())

    understand("随便问一句", llm=stub)

    assert "没有章节词表" in stub.calls[0]["user"]


def test_prompt_titles_are_capped_and_hit_first() -> None:
    """词表条数有上限, 且正则命中的标题优先（接地闭环 + prompt 可控体积）。"""
    lex = SectionLexicon(titles={f"章节{i:03d}": ["p"] for i in range(200)} | {"输出特性": ["p"]})
    titles = _lexicon_titles("输出特性的限值是多少", lex)

    assert titles[0] == "输出特性"
    assert len(titles) == MAX_LEXICON_TITLES


def test_llm_plan_is_json_serializable() -> None:
    """to_dict 可 JSON 序列化（网关/CLI 直接透出）。"""
    plan = understand(QUERY, llm=StubLLM(_payload()))
    dumped = json.dumps(plan.to_dict(), ensure_ascii=False)
    assert json.loads(dumped)["raw"] == QUERY
    assert json.loads(dumped)["filters"][FILTER_PRODUCT_CODE] == ALIAS_CODE
