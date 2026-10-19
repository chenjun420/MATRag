"""P1-5b 应答层测试: 上下文打包 / 引用完整性 / 推理链 / 规则提示 / 降级留痕。

全程离线: ``StubLLM`` 注入假 LLMClient, 检索用鸭子类型替身, 不打网络也不依赖
索引库。W3-A (``understand``) / W3-B (``rule_hint``) 用 monkeypatch 替换本模块的
惰性导入函数, 因此**不依赖它们是否已落盘**。
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

from matrag import answer as answer_mod
from matrag.answer import (
    EVAL_SECTION_HEADER,
    INSUFFICIENT_HINT,
    MARKER_PATTERN,
    RULE_SECTION_HEADER,
    AnswerResult,
    Citation,
    answer_question,
    build_context,
    synthesize,
)


class StubLLM:
    """最小 LLMClient 替身: 预置返回值或异常, 记录每次调用的 (system, user, hint)。"""

    def __init__(
        self,
        result: dict[str, Any] | None = None,
        exc: Exception | None = None,
        *,
        configured: bool = True,
    ) -> None:
        self._result = result
        self._exc = exc
        self._configured = configured
        self.calls: list[tuple[str, str, str]] = []

    @property
    def configured(self) -> bool:
        return self._configured

    def complete_json(self, system: str, user: str, *, schema_hint: str = "",
                      max_tokens: int | None = None) -> dict[str, Any]:
        self.calls.append((system, user, schema_hint))
        if self._exc is not None:
            raise self._exc
        assert self._result is not None, "StubLLM 既无 result 也无 exc"
        return self._result


class FakeRetriever:
    """检索器替身: 契约与 ``HybridRetriever`` 对齐（retrieve/last_plan/last_rule_hints）。"""

    def __init__(
        self,
        hits: list[Any] | None = None,
        *,
        where: dict[str, Any] | None = None,
        last_rule_hints: list[Any] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._hits = hits if hits is not None else [_hit("spec-a:0001", 0.0322)]
        self._where = where
        self._last_rule_hints = last_rule_hints or []
        self._raises = raises
        self.calls: list[tuple[str, int, Any]] = []
        self.last_plan: dict[str, Any] = {"queries": [], "legs": ["vector", "keyword"]}
        self.degradations: list[dict[str, Any]] = []

    @property
    def last_rule_hints(self) -> list[Any]:
        return list(self._last_rule_hints)

    def retrieve(self, query: str, *, top_k: int = 10,
                 where: dict[str, Any] | None = None) -> list[Any]:
        self.calls.append((query, top_k, where))
        self.last_plan = {
            "queries": [query],
            "legs": ["vector", "keyword"],
            "where": self._where or where,
        }
        if self._raises is not None:
            raise self._raises
        return self._hits[:top_k]


class PlanRetriever(FakeRetriever):
    """支持 ``retrieve_with_plan`` 的检索器替身（W3-B 签名: plan 为第二个位置参数）。"""

    def __init__(self, hits: list[Any] | None = None, **kwargs: Any) -> None:
        super().__init__(hits, **kwargs)
        self.plan_calls: list[tuple[str, Any, int]] = []

    def retrieve_with_plan(self, query: str, plan: Any = None, *, top_k: int = 10,
                           where: dict[str, Any] | None = None) -> list[Any]:
        self.plan_calls.append((query, plan, top_k))
        self.calls.append((query, top_k, where))
        self.last_plan = {"queries": [query], "legs": ["vector", "keyword"], "where": where}
        return self._hits[:top_k]


class NarrowRetriever(FakeRetriever):
    """``retrieve`` 不接受 ``where`` 的窄签名替身（考验调用形态自适应）。"""

    def retrieve(self, query: str, *, top_k: int = 10) -> list[Any]:  # type: ignore[override]
        self.calls.append((query, top_k, None))
        self.last_plan = {"queries": [query], "legs": ["vector"], "where": None}
        return self._hits[:top_k]


TABLE_ROW = "| 输出电压 | 5V±2% | DC |"


def _hit(
    chunk_id: str,
    score: float,
    *,
    doc_id: str = "spec-a",
    page: int = 3,
    section_path: str = "4 > 技术要求 > 4.3 功能/性能要求",
    table_id: str | None = None,
    text: str = "输出电压范围为 5V±2%。",
) -> Any:
    """构造一条与 ``RetrievalHit`` 同形状的命中。"""
    meta: dict[str, Any] = {"page": page, "section_path": section_path}
    if table_id:
        meta["table_id"] = table_id
    return SimpleNamespace(
        chunk_id=chunk_id,
        doc_id=doc_id,
        text=text,
        meta=meta,
        rrf_score=score,
        per_leg={"vector": score / 2, "keyword": score / 2},
        legs_hit=("vector", "keyword"),
    )


def _plan(**kwargs: Any) -> Any:
    """构造查询理解计划替身（W3-A QueryPlan 的最小形状）。"""
    base: dict[str, Any] = {
        "kind": "spec_value",
        "intent": "查规格值",
        "entities": {"doc_id": "spec-a"},
        "sub_queries": ["输出电压 限值"],
        "retrieval_queries": ["输出电压 限值", "输出电压 范围"],
        "needs_rules": False,
        "degradations": [],
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def _rule(id_: str = "R-001") -> dict[str, Any]:
    return {"rule_id": id_, "statement": "输出电压应满足 5V±2%。", "kind": "formula", "score": 0.42}


def _eval_payload(rule_id: str = "K-ELEC-001") -> dict[str, Any]:
    """构造一条求值结果的 dict 形状（与 ``EvaluationResult.to_dict()`` 同字段）。"""
    return {
        "rule_id": rule_id,
        "statement": "直流功率 = 电压 × 电流 (P = U × I)",
        "kind": "formula",
        "output_key": "power",
        "unit": "W",
        "applicable": True,
        "ok": True,
        "actual": 599.4,
        "missing_inputs": [],
        "facts_used": {"voltage": 54.0, "current": 11.1},
        "detail": "",
        "error": "",
        "display": f"{rule_id}（formula）: 代入 voltage=54、current=11.1 → power = 599.4 W",
    }


def _markers(text: str) -> set[str]:
    return {f"[{n}]" for n in MARKER_PATTERN.findall(text)}


# --------------------------------------------------------------------------- #
# 上下文打包
# --------------------------------------------------------------------------- #
def test_build_context_marks_anchors_and_keeps_source_text() -> None:
    """每条证据带 [n] + 锚点行; 原文（含表格管道符）一字不改。"""
    hits = [
        _hit("spec-a:0002", 0.01, text=TABLE_ROW, table_id="t3-1"),
        _hit("spec-a:0001", 0.03),
    ]
    context, citations = build_context(hits)

    assert [c.marker for c in citations] == ["[1]", "[2]"]  # 按 rrf_score 降序
    assert citations[0].chunk_id == "spec-a:0001"
    assert citations[0].score == pytest.approx(0.03)
    assert citations[1].table_id == "t3-1"
    assert TABLE_ROW in context  # 表格管道原样
    assert "| 表: t3-1" in context
    assert "[2] 文档: spec-a p3 4 > 技术要求 > 4.3 功能/性能要求" in context


def test_build_context_dedupes_and_caps_items() -> None:
    """同锚点重复证据只留一条（留痕）, 条数上限截断也留痕。"""
    notes: list[dict[str, Any]] = []
    hits = [
        _hit("spec-a:0001", 0.04),
        _hit("spec-a:0001", 0.03),  # 同 chunk 再出现（分数更低）→ 去重
        _hit("spec-a:0002", 0.02),
        _hit("spec-a:0003", 0.01),
    ]
    context, citations = build_context(hits, max_items=2, notes=notes)
    assert [c.chunk_id for c in citations] == ["spec-a:0001", "spec-a:0002"]
    kinds = [note["kind"] for note in notes]
    assert "duplicate_evidence_merged" in kinds
    assert "context_truncated" in kinds
    truncated = next(n for n in notes if n["kind"] == "context_truncated")
    assert truncated["reason"] == "max_items" and truncated["dropped_items"] == 1
    assert "[2] 文档: spec-a" in context and "[3]" not in context


def test_build_context_truncates_by_chars_and_records() -> None:
    """累计字符超上限即停止追加; 留痕且不打乱已有条目。"""
    notes: list[dict[str, Any]] = []
    hits = [_hit(f"spec-a:000{i}", 0.01 * (i + 1), text="x" * 200) for i in range(4)]
    context, citations = build_context(hits, max_chars=260, notes=notes)

    assert len(citations) < len(hits)
    truncated = next(n for n in notes if n["kind"] == "context_truncated")
    assert truncated["reason"] == "max_chars"
    assert len(context) <= 260
    assert [c.marker for c in citations] == [f"[{i + 1}]" for i in range(len(citations))]


def test_build_context_single_oversized_item_stays_empty() -> None:
    """单条证据就超上限时不截断原文, 直接不装入并留痕 context_empty。"""
    notes: list[dict[str, Any]] = []
    context, citations = build_context([_hit("spec-a:0001", 0.5, text="y" * 500)], max_chars=100,
                                       notes=notes)
    assert context == "" and citations == []
    assert any(note["kind"] == "context_empty" for note in notes)


@pytest.mark.parametrize("bad", [0, -1, True, "3"])
def test_build_context_rejects_bad_limits(bad: Any) -> None:
    """非法上限 → ValueError（fail-fast, 不静默截断）。"""
    with pytest.raises(ValueError):
        build_context([], max_items=bad)
    with pytest.raises(ValueError):
        build_context([], max_chars=bad)


# --------------------------------------------------------------------------- #
# 引用完整性
# --------------------------------------------------------------------------- #
def test_llm_markers_are_subset_of_citations() -> None:
    """答案里出现的每个 [n] 都能在 citations 里找到。"""
    llm = StubLLM({"answer": "输出电压限值为 5V±2% [1]，详见表 [2]。", "covered": True,
                   "steps": ["限值直接来自表 2-1 的原文行。"]})
    result = answer_question("输出电压限值是多少", retriever=FakeRetriever(
        [_hit("spec-a:0001", 0.03), _hit("spec-a:0002", 0.02, table_id="t3-1", text=TABLE_ROW)]
    ), plan=_plan(), llm=llm)

    assert result.covered is True
    assert _markers(result.answer) <= {c.marker for c in result.citations}
    assert "解读：" in " ".join(result.steps)


def test_llm_hallucinated_marker_is_stripped_and_traced() -> None:
    """模型编造的 [9] 被剔除并留痕（引用完整性不被破坏）。"""
    llm = StubLLM({"answer": "限值 5V±2% [1]，另一处写 6V [9]。", "covered": True})
    result = answer_question("输出电压限值是多少", retriever=FakeRetriever(), plan=_plan(), llm=llm)

    assert "[9]" not in result.answer
    assert _markers(result.answer) <= {c.marker for c in result.citations}
    note = next(n for n in result.degradations if n["kind"] == "llm_citation_out_of_range")
    assert note["markers"] == ["[9]"]


def test_citations_keep_unreferenced_and_steps_say_so() -> None:
    """未被引用的证据保留在 citations, 步骤里注明条数。"""
    llm = StubLLM({"answer": "只有 [1] 能回答。", "covered": True})
    hits = [_hit(f"spec-a:000{i}", 0.01 * (i + 1)) for i in range(1, 4)]
    result = answer_question("q", retriever=FakeRetriever(hits), plan=_plan(), llm=llm)

    assert len(result.citations) == 3
    usage = [step for step in result.steps if "未被答案引用的证据" in step]
    assert usage and "2 条" in usage[0]


def test_llm_claims_covered_without_citation_is_forced_false() -> None:
    """模型说"证据充分"却没给任何引用 → 强制 covered=false 并留痕。"""
    llm = StubLLM({"answer": "输出电压限值是 5V±2%。", "covered": True})
    result = answer_question("q", retriever=FakeRetriever(), plan=_plan(), llm=llm)

    assert result.covered is False
    assert any(n["kind"] == "covered_without_citation" for n in result.degradations)


# --------------------------------------------------------------------------- #
# 覆盖判定 / 证据不足
# --------------------------------------------------------------------------- #
def test_llm_says_insufficient_is_kept() -> None:
    """模型判 covered=false 时原样保留, 并进入推理链。"""
    answer = f"{INSUFFICIENT_HINT}该型号的纹波指标, 证据不足, 无法给出数值。"
    llm = StubLLM({"answer": answer, "covered": False})
    result = answer_question("纹波指标是多少", retriever=FakeRetriever(), plan=_plan(), llm=llm)

    assert result.covered is False
    assert result.answer == answer
    assert any(INSUFFICIENT_HINT in step for step in result.steps)


def test_no_hits_means_no_llm_call_and_insufficient_answer() -> None:
    """零命中: 不调 LLM, 明说证据不足, 并留痕。"""
    llm = StubLLM({"answer": "不该被调用", "covered": True})
    result = answer_question("不存在的指标", retriever=FakeRetriever([]), plan=_plan(), llm=llm)

    assert llm.calls == []
    assert result.covered is False
    assert INSUFFICIENT_HINT in result.answer
    assert any(n["kind"] == "no_evidence" for n in result.degradations)


def test_synthesize_covered_hint_false_overrides_llm() -> None:
    """covered_hint=False 时即便模型说够也强制判不足并留痕。"""
    llm = StubLLM({"answer": "限值 5V±2% [1]。", "covered": True})
    context, citations = build_context([_hit("spec-a:0001", 0.03)])
    result = synthesize("q", context, citations, llm=llm, covered_hint=False)

    assert result.covered is False
    assert any(n["kind"] == "covered_forced_false" for n in result.degradations)


# --------------------------------------------------------------------------- #
# LLM 不可用 / 失败
# --------------------------------------------------------------------------- #
def test_llm_none_falls_back_to_deterministic_template() -> None:
    """LLM 不可用 → 确定性模板逐条引用原文 + llm_unavailable 留痕, 答案不空。"""
    hits = [_hit("spec-a:0001", 0.03, text=TABLE_ROW, table_id="t3-1")]
    result = answer_question("输出电压限值是多少", retriever=FakeRetriever(hits), plan=_plan())

    assert result.covered is False
    note = next(n for n in result.degradations if n["kind"] == "llm_unavailable")
    assert "llm=None" in note["detail"]
    assert "检索到以下相关条文：" in result.answer
    assert TABLE_ROW in result.answer  # 原文照抄
    assert "[1]" in result.answer
    # 步骤与引用不依赖 LLM 成功
    assert any("检索到 1 条证据" in step for step in result.steps)
    assert any("未被答案引用的证据" in step for step in result.steps)


def test_llm_not_configured_is_traced() -> None:
    """llm.configured=False 也走确定性模板（不是当成功处理）。"""
    llm = StubLLM({"answer": "不该被调用", "covered": True}, configured=False)
    result = answer_question("q", retriever=FakeRetriever(), plan=_plan(), llm=llm)

    assert llm.calls == []
    assert result.covered is False
    assert any(n["kind"] == "llm_unavailable" for n in result.degradations)


@pytest.mark.parametrize(
    ("stub", "kind"),
    [
        (StubLLM(exc=RuntimeError("boom")), "llm_failed"),
        (StubLLM(result=["不是对象"]), "llm_invalid_output"),
        (StubLLM(result={"covered": True}), "llm_answer_invalid"),
    ],
)
def test_llm_failure_modes_fall_back_with_trace(stub: StubLLM, kind: str) -> None:
    """调用炸/形状不对/答案缺失都退回确定性模板并留痕（对应 kind）。"""
    result = answer_question("q", retriever=FakeRetriever(), plan=_plan(), llm=stub)

    assert result.covered is False
    assert result.answer.strip()
    assert any(note["kind"] == kind for note in result.degradations)
    assert any("检索到" in step for step in result.steps)


# --------------------------------------------------------------------------- #
# 推理链（确定性）
# --------------------------------------------------------------------------- #
def test_steps_are_deterministic_and_do_not_need_llm() -> None:
    """LLM 全程炸掉时推理链仍完整（查询条件/过滤/检索/上下文/引用统计）。"""
    llm = StubLLM(exc=RuntimeError("down"))
    result = answer_question(
        "PA601-D54A 输入特性里的欠压点是多少",
        retriever=FakeRetriever(where={"section_contains": "输入特性"}),
        plan=_plan(sub_queries=["欠压点 限值"]),
        llm=llm,
        where={"section_contains": "输入特性"},
    )
    joined = " ".join(result.steps)

    assert "抽取查询条件" in joined and "spec_value" in joined
    assert "按章节「输入特性」过滤" in joined
    assert "检索到 1 条证据" in joined
    assert "装入上下文 1 条证据" in joined
    assert "未被答案引用的证据" in joined
    assert result.degradations  # 降级留痕也在


def test_steps_note_understand_degradation(monkeypatch: pytest.MonkeyPatch) -> None:
    """understand 不可导入时步骤明说未做语义理解, 且 degradations 有痕迹（不崩）。"""
    monkeypatch.setattr(
        answer_mod, "_load_understand", lambda: (None, "ImportError: no understand")
    )
    result = answer_question("输出电压限值", retriever=FakeRetriever())
    joined = " ".join(result.steps)

    assert "未做语义查询理解" in joined
    assert any(n["kind"] == "understand_unavailable" for n in result.degradations)
    assert result.citations  # 降级路径照常工作（纯检索）


# --------------------------------------------------------------------------- #
# 查询理解 / 规则提示的惰性依赖
# --------------------------------------------------------------------------- #
def test_understand_used_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """understand 可导入时被调用（传同一个 llm）, 其降级并入结果。"""
    seen: dict[str, Any] = {}

    def fake_understand(query: str, *, llm: Any = None) -> Any:
        seen["query"] = query
        seen["llm"] = llm
        return _plan(degradations=[{"kind": "llm_rewrite_failed", "detail": "网络抖动"}])

    monkeypatch.setattr(answer_mod, "_load_understand", lambda: (fake_understand, ""))
    llm = StubLLM({"answer": "限值 5V [1]。", "covered": True})
    result = answer_question("输出电压限值", retriever=FakeRetriever(), llm=llm)

    assert seen == {"query": "输出电压限值", "llm": llm}
    assert any(n.get("source") == "understand" for n in result.degradations)


def test_understand_failure_falls_back_to_plain_retrieve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """understand 抛错 → 留痕 + 照常检索（不崩）。"""

    def boom(query: str, *, llm: Any = None) -> Any:
        raise RuntimeError("understand 炸了")

    monkeypatch.setattr(answer_mod, "_load_understand", lambda: (boom, ""))
    retriever = FakeRetriever()
    result = answer_question("q", retriever=retriever)

    assert retriever.calls  # 照常检索
    assert result.citations
    assert any(n["kind"] == "understand_failed" for n in result.degradations)


def test_rule_hints_are_selected_and_marked_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """plan.needs_rules → 确定性规则选取; 上下文里规则与规格分区, 规则写规则 id。"""
    calls: list[tuple[str, int]] = []

    def fake_select(query: str, *, top_n: int = 5) -> list[dict[str, Any]]:
        calls.append((query, top_n))
        return [_rule("R-001"), _rule("R-007")]

    monkeypatch.setattr(answer_mod, "_load_select_rule_hints", lambda: (fake_select, ""))
    llm = StubLLM({"answer": "输出电压限值为 5V±2% [1]（规则 R-001 亦要求该范围）。", "covered": True})
    result = answer_question("输出电压限值是多少", retriever=FakeRetriever(), plan=_plan(needs_rules=True),
                             llm=llm)

    assert calls == [("输出电压限值是多少", answer_mod.DEFAULT_RULE_TOP_N)]
    assert [h["rule_id"] for h in result.rule_hints] == ["R-001", "R-007"]
    prompt = llm.calls[0][1]
    assert RULE_SECTION_HEADER in prompt
    assert "- R-001（formula）: 输出电压应满足 5V±2%。" in prompt
    assert "规则 R-001" in result.answer  # 规则用规则 id 引用
    assert _markers(result.answer) <= {c.marker for c in result.citations}
    assert any("应用确定性规则 2 条" in step for step in result.steps)


def test_rule_hint_unavailable_is_traced(monkeypatch: pytest.MonkeyPatch) -> None:
    """rule_hint 模块缺失 → 留痕, 答案照常产出（不带规则）。"""
    monkeypatch.setattr(
        answer_mod, "_load_select_rule_hints", lambda: (None, "ImportError: no rule_hint")
    )
    result = answer_question("q", retriever=FakeRetriever(), plan=_plan(needs_rules=True))

    assert result.rule_hints == []
    assert any(n["kind"] == "rule_hint_unavailable" for n in result.degradations)


def test_rule_hint_selector_failure_is_traced(monkeypatch: pytest.MonkeyPatch) -> None:
    """规则源不可用（select 抛错）→ 留痕 rule_hint_failed, 不阻断应答。"""

    def boom(query: str, *, top_n: int = 5) -> list[Any]:
        raise RuntimeError("rules.yaml 缺失")

    monkeypatch.setattr(answer_mod, "_load_select_rule_hints", lambda: (boom, ""))
    result = answer_question("q", retriever=FakeRetriever(), plan=_plan(needs_rules=True))

    assert result.citations
    assert any(n["kind"] == "rule_hint_failed" for n in result.degradations)


def test_retriever_rule_hints_reused_without_reselect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """检索器已挂好 last_rule_hints 时直接复用, 不重复选取。"""

    def fake_select(query: str, *, top_n: int = 5) -> list[dict[str, Any]]:
        raise AssertionError("不该重复选取")

    monkeypatch.setattr(answer_mod, "_load_select_rule_hints", lambda: (fake_select, ""))
    retriever = FakeRetriever(last_rule_hints=[_rule("R-042")])
    result = answer_question("q", retriever=retriever, plan=_plan(needs_rules=True))

    assert [h["rule_id"] for h in result.rule_hints] == ["R-042"]


# --------------------------------------------------------------------------- #
# 规则求值（代值求算）
# --------------------------------------------------------------------------- #
def test_evaluations_rendered_in_prompt_answer_and_steps() -> None:
    """求值结果进提示词求值分区 / 推理链 / AnswerResult; 规则与求值分区各只出现一次。"""
    context, citations = build_context([_hit("spec-a:0001", 0.03)])
    llm = StubLLM({"answer": "按公式计算功率为 599.4 W（非规格书数据）[1]。", "covered": True})
    result = synthesize(
        "已知电压 54V、电流 11.1A，求功率",
        context,
        citations,
        rule_hints=[_rule("K-ELEC-001")],
        evaluations=[_eval_payload()],
        llm=llm,
    )

    prompt = llm.calls[0][1]
    assert prompt.count(RULE_SECTION_HEADER) == 1
    assert prompt.count(EVAL_SECTION_HEADER) == 1
    assert "power = 599.4 W" in prompt
    assert [e["rule_id"] for e in result.evaluations] == ["K-ELEC-001"]
    assert any("规则求值 1 条" in step for step in result.steps)
    assert result.to_dict()["evaluations"][0]["rule_id"] == "K-ELEC-001"


def test_evaluations_rendered_in_deterministic_answer() -> None:
    """LLM 不可用时求值结果照常呈现（确定性模板带“规则求值”分区）。"""
    context, citations = build_context([_hit("spec-a:0001", 0.03)])
    result = synthesize(
        "已知电压 54V、电流 11.1A，求功率",
        context,
        citations,
        evaluations=[_eval_payload()],
    )

    assert "规则求值（按问题中数值代入公式计算，非规格书数据）：" in result.answer
    assert "power = 599.4 W" in result.answer
    assert any("规则求值 1 条" in step for step in result.steps)


def test_user_prompt_has_no_eval_section_without_evaluations() -> None:
    """没有求值结果时提示词不带求值分区（不留空标题）。"""
    context, citations = build_context([_hit("spec-a:0001", 0.03)])
    prompt = answer_mod.build_user_prompt("q", context=context, citations=citations)

    assert EVAL_SECTION_HEADER not in prompt


def test_answer_question_forwards_facts_to_evaluator(monkeypatch: pytest.MonkeyPatch) -> None:
    """answer_question: 提示规则 + 显式 facts → 原样转给求值服务。"""
    calls: list[dict[str, Any]] = []

    def fake_evaluate(hints: Any, *, query: str | None = None,
                      facts: Any = None) -> list[dict[str, Any]]:
        calls.append({"ids": [h["rule_id"] for h in hints], "query": query, "facts": facts})
        return [_eval_payload()]

    monkeypatch.setattr(answer_mod, "_load_evaluate", lambda: (fake_evaluate, ""))
    retriever = FakeRetriever(last_rule_hints=[_rule("K-ELEC-001")])
    result = answer_question(
        "已知电压 54V、电流 11.1A，求功率",
        retriever=retriever,
        plan=_plan(needs_rules=True),
        facts={"voltage": 54.0, "current": 11.1},
    )

    assert calls == [{
        "ids": ["K-ELEC-001"],
        "query": "已知电压 54V、电流 11.1A，求功率",
        "facts": {"voltage": 54.0, "current": 11.1},
    }]
    assert [e["rule_id"] for e in result.evaluations] == ["K-ELEC-001"]


def test_answer_question_includes_multihop_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """计算类问题: 多跳推导 (query_derive) 自动补入求值条目, 逐跳可审计。

    单跳求值器用空替身 (不产出条目), 验证推导链是独立接线的:
    输入功率100W、效率90%、输出电压12V → 90W → 7.5A。
    """
    monkeypatch.setattr(
        answer_mod, "_load_evaluate", lambda: (lambda *a, **k: [], "")
    )
    retriever = FakeRetriever(last_rule_hints=[_rule("K-PWR-126")])
    result = answer_question(
        "输入功率100W、整机效率90%、输出电压12V，输出电流是多少？",
        retriever=retriever,
        plan=_plan(needs_rules=True),
    )

    assert [e["rule_id"] for e in result.evaluations] == ["K-PWR-126", "K-PWR-127"]
    displays = " ".join(str(e["display"]) for e in result.evaluations)
    assert "output_current = 7.5" in displays
    assert any("规则求值 2 条" in step for step in result.steps)


def test_answer_question_rejects_bad_derive_mode() -> None:
    with pytest.raises(ValueError, match="derive_mode"):
        answer_question("q", retriever=FakeRetriever(), derive_mode="wat")


def test_answer_question_llm_derive_mode_without_llm_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """derive_mode="llm" 且 llm=None: 编排器失败 → 内部回退规划器, 条目仍带留痕。"""
    monkeypatch.setattr(
        answer_mod, "_load_evaluate", lambda: (lambda *a, **k: [], "")
    )
    retriever = FakeRetriever(last_rule_hints=[_rule("K-PWR-126")])
    result = answer_question(
        "输入功率100W、整机效率90%、输出电压12V，输出电流是多少？",
        retriever=retriever,
        plan=_plan(needs_rules=True),
        derive_mode="llm",
    )
    assert [e["rule_id"] for e in result.evaluations] == ["K-PWR-126", "K-PWR-127"]
    assert all(e.get("source") == "derivation-llm" for e in result.evaluations)
    assert all(e.get("strategy") for e in result.evaluations)


def test_answer_question_llm_derive_mode_uses_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """derive_mode="llm": 编排器结果 (strategy=llm) 原样进入求值条目。"""
    from matrag.domain.llm_planner import OrchestrationResult
    from matrag.domain.planner import Derivation, PlanStep

    fake_result = OrchestrationResult(
        strategy="llm",
        target="output_current",
        derivation=Derivation(
            target="output_current",
            value=7.5,
            plan=(
                PlanStep("K-PWR-126", "forward", "output_power"),
                PlanStep("K-PWR-127", "forward", "output_current"),
            ),
            steps=(
                "K-PWR-126 [正向]: 代入 input_power=100、efficiency=90 → output_power = 90.0",
                "K-PWR-127 [正向]: 代入 output_power=90.0、output_voltage=12.0 → output_current = 7.5",
            ),
        ),
        facts={},
        llm_calls=1,
        reason="r",
    )
    monkeypatch.setattr(
        answer_mod,
        "_load_llm_orchestrator",
        lambda: (lambda q, *, llm=None, facts=None, **kw: fake_result, ""),
    )
    monkeypatch.setattr(
        answer_mod, "_load_evaluate", lambda: (lambda *a, **k: [], "")
    )
    retriever = FakeRetriever(last_rule_hints=[_rule("K-PWR-126")])
    result = answer_question(
        "输入功率100W、整机效率90%、输出电压12V，输出电流是多少？",
        retriever=retriever,
        plan=_plan(needs_rules=True),
        derive_mode="llm",
    )
    assert [e["rule_id"] for e in result.evaluations] == ["K-PWR-126", "K-PWR-127"]
    assert all(e.get("strategy") == "llm" for e in result.evaluations)


def test_answer_question_agent_derive_mode_merges_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """derive_mode="agent": 工具循环结果 (strategy=agent) 原样进入求值条目。"""
    from matrag.domain.tool_agent import AgentComputation

    fake = AgentComputation(
        strategy="agent",
        target="output_current",
        value=7.5,
        method="rules",
        entries=[
            {
                "rule_id": "K-PWR-126",
                "display": "K-PWR-126（formula·代入）: → 90.0",
                "source": "derivation-agent",
                "target": "output_power",
            },
            {
                "rule_id": "K-PWR-127",
                "display": "K-PWR-127（formula·代入）: → 7.5",
                "source": "derivation-agent",
                "target": "output_current",
            },
        ],
        steps=["derive_with_rules(target='输出电流') ⇒ output_current = 7.5"],
        llm_calls=2,
        reason="链式推导",
    )
    monkeypatch.setattr(
        answer_mod,
        "_load_tool_agent",
        lambda: (lambda q, *, llm=None, facts=None, **kw: fake, ""),
    )
    monkeypatch.setattr(answer_mod, "_load_evaluate", lambda: (lambda *a, **k: [], ""))
    retriever = FakeRetriever(last_rule_hints=[_rule("K-PWR-126")])
    result = answer_question(
        "输入功率100W、整机效率90%、输出电压12V，输出电流是多少？",
        retriever=retriever,
        plan=_plan(needs_rules=True),
        derive_mode="agent",
    )

    assert [e["rule_id"] for e in result.evaluations] == ["K-PWR-126", "K-PWR-127"]
    assert all(e.get("strategy") == "agent" for e in result.evaluations)
    assert all(e.get("source") == "derivation-agent" for e in result.evaluations)


def test_answer_question_agent_tool_support_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型不支持 tools 能力 → 上抛 ToolSupportError（无降级路径）。"""
    from matrag.domain.tool_agent import ToolSupportError

    def boom(q: str, *, llm: Any = None, facts: Any = None, **kw: Any) -> Any:
        raise ToolSupportError("模型不支持 tools")

    monkeypatch.setattr(answer_mod, "_load_tool_agent", lambda: (boom, ""))
    monkeypatch.setattr(answer_mod, "_load_evaluate", lambda: (lambda *a, **k: [], ""))
    retriever = FakeRetriever(last_rule_hints=[_rule("K-PWR-126")])

    with pytest.raises(ToolSupportError):
        answer_question(
            "输入功率100W、整机效率90%、输出电压12V，输出电流是多少？",
            retriever=retriever,
            plan=_plan(needs_rules=True),
            derive_mode="agent",
        )


def test_answer_question_agent_unavailable_degrades_traced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent 模块未落盘 → 留痕并退化为确定性规划器（能力缺失有记录）。"""
    monkeypatch.setattr(
        answer_mod, "_load_tool_agent", lambda: (None, "ImportError: no tool_agent")
    )
    monkeypatch.setattr(answer_mod, "_load_evaluate", lambda: (lambda *a, **k: [], ""))
    retriever = FakeRetriever(last_rule_hints=[_rule("K-PWR-126")])
    result = answer_question(
        "输入功率100W、整机效率90%、输出电压12V，输出电流是多少？",
        retriever=retriever,
        plan=_plan(needs_rules=True),
        derive_mode="agent",
    )

    assert any(n["kind"] == "tool_agent_unavailable" for n in result.degradations)
    assert [e["rule_id"] for e in result.evaluations] == ["K-PWR-126", "K-PWR-127"]


def test_rule_eval_unavailable_is_traced(monkeypatch: pytest.MonkeyPatch) -> None:
    """求值服务缺失 → 留痕 rule_eval_unavailable, 答案照常（只少计算值）。"""
    monkeypatch.setattr(
        answer_mod, "_load_evaluate", lambda: (None, "ImportError: no evaluate")
    )
    retriever = FakeRetriever(last_rule_hints=[_rule("R-001")])
    result = answer_question("q", retriever=retriever, plan=_plan(needs_rules=True))

    assert result.evaluations == []
    assert any(n["kind"] == "rule_eval_unavailable" for n in result.degradations)


def test_rule_eval_failure_is_traced(monkeypatch: pytest.MonkeyPatch) -> None:
    """求值执行异常 → 留痕 rule_eval_failed, 不阻断应答。"""

    def boom(hints: Any, *, query: str | None = None, facts: Any = None) -> list[Any]:
        raise RuntimeError("rules.yaml 缺失")

    monkeypatch.setattr(answer_mod, "_load_evaluate", lambda: (boom, ""))
    retriever = FakeRetriever(last_rule_hints=[_rule("R-001")])
    result = answer_question("q", retriever=retriever, plan=_plan(needs_rules=True))

    assert result.citations
    assert any(n["kind"] == "rule_eval_failed" for n in result.degradations)


def test_empty_facts_disable_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    """facts={} → 显式关闭求值（连求值服务都不加载）。"""

    def should_not_load() -> Any:
        raise AssertionError("facts={} 时不该加载求值服务")

    monkeypatch.setattr(answer_mod, "_load_evaluate", should_not_load)
    retriever = FakeRetriever(last_rule_hints=[_rule("R-001")])
    result = answer_question("q", retriever=retriever, plan=_plan(needs_rules=True), facts={})

    assert result.evaluations == []


def test_no_hints_skip_evaluation_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有提示规则且未给事实 → 不求值也不加载服务（常见快路径）。"""

    def should_not_load() -> Any:
        raise AssertionError("无提示规则时不该加载求值服务")

    monkeypatch.setattr(answer_mod, "_load_evaluate", should_not_load)
    result = answer_question("q", retriever=FakeRetriever(), plan=_plan())

    assert result.evaluations == []


# --------------------------------------------------------------------------- #
# 检索调用形态
# --------------------------------------------------------------------------- #
def test_plan_is_forwarded_to_retrieve_with_plan() -> None:
    """有 plan 且检索器支持 retrieve_with_plan → plan 透传。"""
    plan = _plan()
    retriever = PlanRetriever()
    answer_question("q", retriever=retriever, plan=plan)

    assert retriever.plan_calls == [("q", plan, answer_mod.DEFAULT_TOP_K)]


def test_narrow_retriever_signature_still_works() -> None:
    """检索器不接受 where 时自动降级调用形态, 不抛 TypeError。"""
    retriever = NarrowRetriever()
    result = answer_question("q", retriever=retriever, plan=_plan(), where={"doc_id": "spec-a"})

    assert retriever.calls == [("q", answer_mod.DEFAULT_TOP_K, None)]
    assert result.citations


def test_plan_only_retriever_without_plan_falls_back_to_plan_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只有 retrieve_with_plan 的检索器在 plan=None 时也照常检索（等价 retrieve）。"""
    monkeypatch.setattr(answer_mod, "_load_understand", lambda: (None, "ImportError: no understand"))

    class PlanOnlyRetriever:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any, int]] = []
            self.last_plan: dict[str, Any] = {}
            self.last_rule_hints: list[Any] = []

        def retrieve_with_plan(self, query: str, plan: Any = None, *,
                               top_k: int = 10) -> list[Any]:
            self.calls.append((query, plan, top_k))
            return [_hit("spec-a:0001", 0.03)]

    retriever = PlanOnlyRetriever()
    result = answer_question("q", retriever=retriever)

    assert retriever.calls == [("q", None, answer_mod.DEFAULT_TOP_K)]
    assert result.citations


def test_retriever_failure_propagates() -> None:
    """检索执行异常原样上抛（fail-fast）, 由 gateway 转结构化错误。"""
    with pytest.raises(OSError):
        answer_question("q", retriever=FakeRetriever(raises=OSError("db locked")))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"query": "  "}, "query"),
        ({"top_k": 0}, "top_k"),
        ({"retriever": None}, "retriever"),
        ({"retriever": object()}, "retrieve"),
        ({"facts": ["voltage=54"]}, "facts"),
    ],
)
def test_answer_question_validates_inputs(kwargs: dict[str, Any], message: str) -> None:
    """非法入参 → ValueError（不静默给假答案）。"""
    params: dict[str, Any] = {"query": "q", "retriever": FakeRetriever(), "plan": _plan()}
    params.update(kwargs)
    with pytest.raises(ValueError, match=message):
        answer_question(**params)


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def test_answer_result_to_dict_shape() -> None:
    """to_dict 七个键齐备, citations 为 dict 列表。"""
    context, citations = build_context([_hit("spec-a:0001", 0.03)])
    payload = AnswerResult(
        answer="限值 5V±2% [1]。",
        citations=citations,
        steps=["s"],
        rule_hints=[_rule()],
        evaluations=[_eval_payload()],
        covered=True,
        degradations=[{"kind": "x"}],
    ).to_dict()
    assert payload["answer"] == "限值 5V±2% [1]。"
    assert set(payload) == {
        "answer", "citations", "steps", "rule_hints", "evaluations", "covered", "degradations",
    }
    assert payload["evaluations"][0]["rule_id"] == "K-ELEC-001"
    assert payload["citations"][0]["marker"] == "[1]"
    assert payload["citations"][0]["chunk_id"] == "spec-a:0001"
    assert payload["covered"] is True
    assert context  # 上下文非空（打包本身无异常）


def test_user_prompt_marks_citation_range_and_rules() -> None:
    """提示词声明 [n] 范围并附规则分区（供离线对账提示词结构）。"""
    context, citations = build_context([_hit("spec-a:0001", 0.03)])
    prompt = answer_mod.build_user_prompt(
        "输出电压限值", context=context, citations=citations, plan=_plan(), rule_hints=[_rule()],
    )
    assert "# 规格书证据（共 1 条" in prompt
    assert RULE_SECTION_HEADER in prompt
    assert "R-001" in prompt
    assert re.search(r"\[n\]", prompt)


def test_citation_defaults_when_meta_missing() -> None:
    """meta 缺字段不猜: 页码 0、章节空串、table_id 为 None; text 原样带上命中原文。"""
    bare = SimpleNamespace(chunk_id="c1", doc_id="d1", text="t", meta=None, rrf_score=None,
                           legs_hit=())
    _, citations = build_context([bare])
    assert citations[0] == Citation(marker="[1]", doc_id="d1", page=0, section_path="",
                                     table_id=None, chunk_id="c1", score=0.0, text="t")


# --------------------------------------------------------------------------- #
# AnswerResult.plan / hits（审计接线用原始引用, 不进 to_dict）


def test_answer_question_carries_plan_and_hits() -> None:
    """``answer_question`` 把 plan 与检索命中原样挂在结果上（审计用）。"""
    plan = _plan()
    retriever = FakeRetriever(hits=[_hit("spec-a:0001", 0.5)])
    result = answer_question("输出电压是多少", retriever=retriever, plan=plan, llm=None)
    assert result.plan is plan
    assert [hit.chunk_id for hit in result.hits] == ["spec-a:0001"]
    payload = result.to_dict()
    assert "plan" not in payload and "hits" not in payload
    assert set(payload) == {
        "answer", "citations", "steps", "rule_hints", "evaluations", "covered", "degradations",
    }


def test_answer_result_plan_hits_default_empty() -> None:
    result = AnswerResult(answer="x")
    assert result.plan is None
    assert result.hits == []
