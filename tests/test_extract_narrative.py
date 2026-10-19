"""``matrag.extract.narrative`` LLM 提案 + 事实护栏的离线测试（用 stub LLM）。

红线
----
- 提案一律 ``status="draft"`` / ``source="narrative"`` / ``confidence="llm"`` —— 生产侧绝不代签。
- 数值必须按字面见于原文 → 否则弃值 (kind=measure 整条弃, 非 measure 仅弃值)。
- ``method_ref`` 必须存在于方法目录 → 否则清空并落 review。
- kind/side 必须在 :data:`matrag.extract.kinds.KNOWN_KIND_SIDES` 内 → 否则弃并落 review。
- LLM 未配置 → 整体跳过 (``skipped_reason="llm_not_configured"``), 留痕不静默丢。
- 需求编号 = ``NAR-{product}-{seq:03d}``。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from matrag.domain.test_methods import load_test_methods
from matrag.extract.kinds import KIND_UNITS, KNOWN_KIND_SIDES
from matrag.extract.narrative import (
    IN_SCOPE_CATEGORIES,
    NarrativeResult,
    build_requirements,
    extract_narrative,
    propose_from_blocks,
    select_blocks,
)

# ────────────── stub LLM ──────────────


class _StubLLM:
    """最小 LLM 替身: configured=True, complete_json 返回 ``next_payload`` 列表里的下一项。"""

    def __init__(self, payloads: Sequence[dict[str, Any]], *, fail: bool = False) -> None:
        self._payloads = list(payloads)
        self._calls = 0
        self._fail = fail
        self.last_user: str | None = None
        self.last_system: str | None = None

    @property
    def configured(self) -> bool:
        return True

    def complete_json(
        self, system: str, user: str, *, schema_hint: str = "", max_tokens: int | None = None
    ) -> dict[str, Any]:
        self.last_system = system
        self.last_user = user
        if self._fail:
            raise RuntimeError("stub llm network down")
        if self._calls >= len(self._payloads):
            return {"proposals": []}
        self._calls += 1
        return self._payloads[self._calls - 1]


# ────────────── 测试用段落 ──────────────


# 落到 IN_SCOPE_CATEGORIES 的章节头 (output)
_OUT_SECTION = "输出特性 > 输出电压"

# 段落文本里出现的数值 (用于测试 fact guard)
_TEXT_WITH_220 = "额定 220Vac 输入下, 输出电压范围 53.5~54.5 V, 典型值 54.0 V。"
_TEXT_WITH_300 = "最大输出电压 300 V 是绝对额定上限, 严禁触碰。"


def _para(text: str, section: str = _OUT_SECTION, page: int = 1) -> dict[str, Any]:
    return {"kind": "paragraph", "text": text, "section_path": section, "page": page, "level": 0}


# ────────────── select_blocks ──────────────


def test_select_blocks_picks_in_scope_paragraphs() -> None:
    blocks = [
        _para(_TEXT_WITH_220),
        _para("环境温度: -10~50 °C", section="环境条件 > 温度"),  # 不在产测类别
        {"kind": "table", "section_path": _OUT_SECTION},  # 非 paragraph
        _para("短"),  # 长度不足 _MIN_BLOCK_CHARS
    ]
    picked, stats = select_blocks(blocks, max_blocks=10)
    assert stats["blocks_total"] == 4
    assert stats["blocks_in_scope"] == 1
    assert stats["blocks_used"] == 1
    assert len(picked) == 1
    assert picked[0].text == _TEXT_WITH_220


def test_select_blocks_dedup_text() -> None:
    blocks = [_para(_TEXT_WITH_220), _para(_TEXT_WITH_220), _para(_TEXT_WITH_220 + " 增量")]
    picked, stats = select_blocks(blocks, max_blocks=10)
    assert len(picked) == 2
    assert stats["blocks_in_scope"] == 2


def test_select_blocks_truncates_with_stats() -> None:
    """每条 ≥ _MIN_BLOCK_CHARS (6) 才能进选; 20 条按 max_blocks=5 截断。"""
    blocks = [_para(f"输出段落条目 {i}", section=_OUT_SECTION) for i in range(20)]
    picked, stats = select_blocks(blocks, max_blocks=5)
    assert len(picked) == 5
    assert stats["blocks_truncated"] == 15
    assert stats["blocks_used"] == 5


def test_select_blocks_other_category_filtered_out() -> None:
    """"段落 N" 不在 IN_SCOPE_CATEGORIES (classify_section→other) → 被滤掉。"""
    blocks = [_para(f"环境段落 {i} 文字", section="环境段落 X") for i in range(3)]
    picked, stats = select_blocks(blocks, max_blocks=10)
    assert picked == []
    assert stats["blocks_in_scope"] == 0


def test_select_blocks_no_candidates_returns_empty() -> None:
    blocks = [_para("环境温度 25°C", section="环境条件 > 温度")]
    picked, stats = select_blocks(blocks, max_blocks=10)
    assert picked == []
    assert stats["blocks_used"] == 0


# ────────────── propose_from_blocks: LLM 未配置 ──────────────


def test_propose_skipped_when_llm_not_configured() -> None:
    """LLMClient.configured=False → 整体跳过, skipped_reason="llm_not_configured" + 留痕。"""
    blocks = [_para(_TEXT_WITH_220)]
    proposals, review, stats, reason = propose_from_blocks(blocks, llm=None)
    assert proposals == []
    assert reason == "llm_not_configured"
    assert stats["skipped_llm_off"] == 1
    assert stats["clauses_draft"] == 0


class _ConfiguredButMissingLLM:
    configured = False  # 显式 False


def test_propose_skipped_when_configured_false() -> None:
    """显式 configured=False → 同上。"""
    blocks = [_para(_TEXT_WITH_220)]
    proposals, review, stats, reason = propose_from_blocks(blocks, llm=_ConfiguredButMissingLLM())
    assert proposals == []
    assert reason == "llm_not_configured"


# ────────────── propose_from_blocks: 护栏 ──────────────


def test_propose_value_not_in_text_is_dropped_for_measure() -> None:
    """measure 类 kind (output_voltage) 的数值未见于原文 → 整条弃 + review。"""
    blocks = [_para(_TEXT_WITH_220)]
    # 提案: output_voltage 数值=100.0 (原文没出现)
    llm = _StubLLM([{"proposals": [
        {"block": 0, "kind": "output_voltage", "side": "output",
         "text": "输出电压", "value": {"min": 100.0, "unit": "V"}, "method_ref": ""}
    ]}])
    proposals, review, stats, reason = propose_from_blocks(blocks, llm=llm)
    assert reason == ""
    assert proposals == []  # measure 无值整条弃
    assert any(r.kind == "narrative_measure_no_value" for r in review)


def test_propose_value_not_in_text_dropped_for_non_measure_kind() -> None:
    """非 measure (input_voltage) 数值未见于原文 → 弃值, clause 保留 + review。"""
    blocks = [_para(_TEXT_WITH_220)]
    llm = _StubLLM([{"proposals": [
        {"block": 0, "kind": "input_voltage", "side": "input",
         "text": "输入电压", "value": {"min": 999.0, "unit": "V"}, "method_ref": ""}
    ]}])
    proposals, review, stats, _ = propose_from_blocks(blocks, llm=llm)
    assert len(proposals) == 1
    block, clause = proposals[0]
    assert clause.value is None or "min" not in clause.value  # 值被弃
    assert any(r.kind == "narrative_value_dropped" for r in review)


def test_propose_kind_not_in_vocabulary_dropped() -> None:
    """kind/side 不在 (KNOWN_KIND_SIDES) → 弃 + review kind=narrative_kind_invalid。"""
    blocks = [_para(_TEXT_WITH_220)]
    llm = _StubLLM([{"proposals": [
        {"block": 0, "kind": "not_a_kind", "side": "output",
         "text": "假 kind", "value": None, "method_ref": ""}
    ]}])
    proposals, review, stats, _ = propose_from_blocks(blocks, llm=llm)
    assert proposals == []
    assert any(r.kind == "narrative_kind_invalid" for r in review)
    assert stats["kind_dropped"] == 1


def test_propose_method_ref_invalid_cleared() -> None:
    """method_ref 不在 load_test_methods() → 清空 + review。"""
    blocks = [_para(_TEXT_WITH_220)]
    methods = load_test_methods()  # 真实方法目录
    method_ids = {m.method_id for m in methods}
    fake_method = "totally-not-in-catalog-xyz"
    assert fake_method not in method_ids
    llm = _StubLLM([{"proposals": [
        {"block": 0, "kind": "output_voltage", "side": "output",
         "text": "输出电压", "value": {"min": 53.5, "max": 54.5, "unit": "V"},
         "method_ref": fake_method}
    ]}])
    proposals, review, stats, _ = propose_from_blocks(blocks, llm=llm, methods=methods)
    assert len(proposals) == 1
    _, clause = proposals[0]
    assert clause.method_ref == ""
    assert any(r.kind == "narrative_method_ref_invalid" for r in review)


def test_propose_dedupes_by_cond_fingerprint() -> None:
    """相同 (kind, side, value, method_ref) 提案两次 → 只收一条, stats.duplicates += 1。"""
    blocks = [_para(_TEXT_WITH_220)]
    llm = _StubLLM([{"proposals": [
        {"block": 0, "kind": "output_voltage", "side": "output",
         "text": "输出电压", "value": {"min": 53.5, "max": 54.5, "unit": "V"},
         "method_ref": ""},
        {"block": 0, "kind": "output_voltage", "side": "output",
         "text": "输出电压 重复", "value": {"min": 53.5, "max": 54.5, "unit": "V"},
         "method_ref": ""},
    ]}])
    proposals, review, stats, _ = propose_from_blocks(blocks, llm=llm)
    assert len(proposals) == 1
    assert stats["duplicates"] == 1


def test_propose_accepts_value_in_text_for_measure() -> None:
    """measure 类数值在原文里 → 保留, clause.value 含 min。"""
    blocks = [_para(_TEXT_WITH_220)]  # 含 53.5 / 54.5 / 54.0
    llm = _StubLLM([{"proposals": [
        {"block": 0, "kind": "output_voltage", "side": "output",
         "text": "输出电压", "value": {"min": 53.5, "max": 54.5, "typ": 54.0, "unit": "V"},
         "method_ref": ""}
    ]}])
    proposals, _, stats, _ = propose_from_blocks(blocks, llm=llm)
    assert len(proposals) == 1
    _, clause = proposals[0]
    assert clause.status == "draft"
    assert clause.source == "narrative"
    assert clause.confidence == "llm"
    assert clause.role == "output"
    assert clause.value and clause.value.get("min") == 53.5


def test_propose_negative_rail_value_normalized_ascending() -> None:
    """负轨提案 (LLM 给幅值序 -53.2/-54.8) → clause.value 归一为数学升序 (GAP-01)。"""
    text = "负轨输出整定值: 最小值 -53.2 V, 典型值 -54.0 V, 最大值 -54.8 V。"
    blocks = [_para(text, section=_OUT_SECTION)]
    llm = _StubLLM([{"proposals": [
        {"block": 0, "kind": "output_voltage", "side": "output",
         "text": "输出电压整定值", "value": {"min": -53.2, "typ": -54.0, "max": -54.8, "unit": "V"},
         "method_ref": ""}
    ]}])
    proposals, _, _, _ = propose_from_blocks(blocks, llm=llm)
    assert len(proposals) == 1
    _, clause = proposals[0]
    assert clause.value is not None
    assert clause.value["min"] == -54.8 and clause.value["max"] == -53.2
    assert clause.value["min"] < clause.value["max"]


def test_propose_bad_block_ref_skipped() -> None:
    """block 索引越界 → 跳过, stats.bad_block_ref += 1。"""
    blocks = [_para(_TEXT_WITH_220)]
    llm = _StubLLM([{"proposals": [
        {"block": 99, "kind": "output_voltage", "side": "output",
         "text": "x", "value": {"min": 53.5, "unit": "V"}, "method_ref": ""}
    ]}])
    proposals, _, stats, _ = propose_from_blocks(blocks, llm=llm)
    assert proposals == []
    assert stats["bad_block_ref"] == 1


def test_propose_llm_error_falls_through_with_review() -> None:
    """单 chunk LLM 调用失败 → 不中断全局, 落 review(narrative_llm_error)。"""
    blocks = [_para(_TEXT_WITH_220), _para(_TEXT_WITH_300)]
    llm = _StubLLM(payloads=[], fail=True)
    proposals, review, stats, _ = propose_from_blocks(blocks, llm=llm, max_blocks=2)
    assert proposals == []
    assert any(r.kind == "narrative_llm_error" for r in review)
    assert stats["llm_errors"] == 1


def test_propose_bad_response_shape_records_review() -> None:
    """LLM 返回非 proposals 列表 → review(narrative_llm_bad_response)。"""
    blocks = [_para(_TEXT_WITH_220)]
    llm = _StubLLM([{"not_proposals": 1}])
    proposals, review, stats, _ = propose_from_blocks(blocks, llm=llm)
    assert proposals == []
    assert any(r.kind == "narrative_llm_bad_response" for r in review)


# ────────────── build_requirements ──────────────


def test_build_requirements_groups_by_block_and_codes() -> None:
    """每个源块一个 NAR-{product}-{seq:03d} 需求, 状态=approved 但带 flags=[narrative]。

    注意: clause 是 draft, 但外层 Requirement.status 默认 approved (设计冻结口径,
    评审在 review 层做, 不让规划器排进 plan_gaps)。
    """
    from matrag.extract.bundle import ClauseModel
    from matrag.extract.narrative import NarrativeBlock

    block = NarrativeBlock(index=0, section_path="输出特性 > 输出电压", page=1, text=_TEXT_WITH_220)
    clauses = [
        ClauseModel(
            kind="output_voltage", text="输出电压", role="output",
            value={"min": 53.5, "max": 54.5, "unit": "V"}, source="narrative",
            confidence="llm", status="draft", method_ref="",
            cond_fingerprint="abc",
        ),
        ClauseModel(
            kind="ripple", text="纹波", role="output",
            value={"max": 100.0, "unit": "mVpp"}, source="narrative",
            confidence="llm", status="draft", method_ref="",
            cond_fingerprint="def",
        ),
    ]
    proposals = [(block, c) for c in clauses]
    reqs, review, stats = build_requirements(proposals, product_code="PA601-D54A")
    assert len(reqs) == 1
    assert reqs[0].requirement_code == "NAR-PA601-D54A-001"
    assert reqs[0].flags == ["narrative"]
    assert len(reqs[0].output_conditions) == 2
    assert reqs[0].req_fingerprint  # 非空
    assert stats["requirements"] == 1
    assert stats["clauses_bound"] == 2


def test_build_requirements_dedupes_against_spec_fingerprints() -> None:
    """spec 已写过的 cond_fingerprint 不再出 draft (避免冗余评审)。"""
    from matrag.extract.bundle import ClauseModel
    from matrag.extract.narrative import NarrativeBlock

    block = NarrativeBlock(index=0, section_path="输出特性 > 输出电压", page=1, text=_TEXT_WITH_220)
    dup = ClauseModel(
        kind="output_voltage", text="x", role="output",
        value={"min": 53.5, "unit": "V"}, source="narrative",
        confidence="llm", status="draft", method_ref="", cond_fingerprint="dup-fp",
    )
    reqs, _, stats = build_requirements([(block, dup)], product_code="X", exclude_fingerprints={"dup-fp"})
    assert reqs == []
    assert stats["deduped_against_spec"] == 1


# ────────────── extract_narrative 一步式 ──────────────


def test_extract_narrative_returns_narrative_result_with_draft_clauses() -> None:
    """完整流程: blocks + stub LLM → NarrativeResult, 各 clause.status=draft/source=narrative/confidence=llm。"""
    blocks = [_para(_TEXT_WITH_220)]
    llm = _StubLLM([{"proposals": [
        {"block": 0, "kind": "output_voltage", "side": "output",
         "text": "输出电压", "value": {"min": 53.5, "max": 54.5, "unit": "V"},
         "method_ref": ""}
    ]}])
    res = extract_narrative(blocks, product_code="PA601-D54A", llm=llm)
    assert isinstance(res, NarrativeResult)
    assert res.skipped_reason == ""
    assert len(res.requirements) == 1
    req = res.requirements[0]
    assert req.requirement_code.startswith("NAR-PA601-D54A-")
    assert req.flags == ["narrative"]
    for c in req.output_conditions:
        assert c.status == "draft"
        assert c.source == "narrative"
        assert c.confidence == "llm"


def test_extract_narrative_skipped_returns_empty_with_reason() -> None:
    """llm=None → skipped_reason='llm_not_configured', requirements=[], stats 留痕。"""
    blocks = [_para(_TEXT_WITH_220)]
    res = extract_narrative(blocks, product_code="PA601-D54A", llm=None)
    assert res.requirements == []
    assert res.review == []
    assert res.skipped_reason == "llm_not_configured"
    assert res.stats["skipped_llm_off"] == 1


# ────────────── 词表一致性 ──────────────


def test_in_scope_categories_complement_excluded() -> None:
    """IN_SCOPE_CATEGORIES 与 spec_tables.EXCLUDED_CATEGORIES 不重叠 (设计纪律)。"""
    from matrag.extract.spec_tables import EXCLUDED_CATEGORIES
    assert IN_SCOPE_CATEGORIES.isdisjoint(EXCLUDED_CATEGORIES)


def test_kinds_for_side_covers_known_pairs() -> None:
    """KIND_UNITS 的所有 (kind, side) 对都互相一致 (防回归)。"""
    sides = {"input", "output"}
    for (k, s), _unit in KIND_UNITS.items():
        assert s in sides
        assert (k, s) in KNOWN_KIND_SIDES
