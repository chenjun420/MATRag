"""确定性规则提示测试（``matrag.retrieve.rule_hint``，全离线）。

覆盖: 同分稳定排序、分数可复现、kinds 过滤、空查询/未命中返回空列表、
参数 fail-fast、规则源不可用 → RuntimeError（不静默空）、真实 rules.yaml
（132 条）加载后"效率"类查询能命中 efficiency 类规则、打分可解释性。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from matrag.domain.rules_loader import DomainRule, load_rules
from matrag.retrieve.rule_hint import (
    KIND_CONSTRAINT,
    KIND_FORMULA,
    RULE_KEYWORDS,
    RULE_KINDS,
    RuleHint,
    rule_hint_summary,
    score_rule,
    select_rule_hints,
)

# --------------------------------------------------------------------------- fakes

REAL_RULES_ENV = "MATRAG_DOMAIN_RULES_PATH"


def _rule(
    rule_id: str,
    *,
    statement: str = "",
    category: str = "",
    kind: str = KIND_FORMULA,
    expr: str = "",
    inputs: tuple[str, ...] = (),
    given: dict[str, Any] | None = None,
    source_name: str = "",
) -> DomainRule:
    """构造最小可用的 :class:`DomainRule` 替身（公式型或约束型二选一）。"""
    derive = {"output": "out", "expr": expr, "inputs": list(inputs)} if kind == KIND_FORMULA else None
    constraint = {"shape": "x"} if kind == KIND_CONSTRAINT else None
    return DomainRule(
        rule_id=rule_id,
        statement=statement,
        category=category,
        scope="dc",
        derive=derive,
        constraint=constraint,
        test={"given": dict(given or {}), "expect": {}},
        source={"name": source_name} if source_name else {},
    )


def _same_score_rules() -> list[DomainRule]:
    """三条分数必然相同的规则（陈述完全一致, 只有 rule_id 不同）。"""
    return [
        _rule("K-ZZZ-003", statement="纹波限值判据", category="ripple", kind=KIND_CONSTRAINT),
        _rule("K-AAA-001", statement="纹波限值判据", category="ripple", kind=KIND_CONSTRAINT),
        _rule("K-MMM-002", statement="纹波限值判据", category="ripple", kind=KIND_CONSTRAINT),
    ]


# --------------------------------------------------------------------------- 排序/复现


def test_tie_scores_are_sorted_stably_by_rule_id() -> None:
    """同分时按 rule_id 升序稳定排序（不是插入顺序, 可复现）。"""
    rules = _same_score_rules()
    hints = select_rule_hints("纹波限值", rules=rules, top_n=3)

    assert [h.rule_id for h in hints] == ["K-AAA-001", "K-MMM-002", "K-ZZZ-003"]
    assert len({h.score for h in hints}) == 1  # 确实同分


def test_repeated_calls_give_identical_scores() -> None:
    """同一输入两次运行分数逐位一致（纯确定性, 可复现）。"""
    rules = _same_score_rules()
    first = select_rule_hints("纹波限值判据", rules=rules, top_n=3)
    second = select_rule_hints("纹波限值判据", rules=rules, top_n=3)

    assert [h.rule_id for h in first] == [h.rule_id for h in second]
    assert [h.score for h in first] == [h.score for h in second]
    assert [h.score_parts for h in first] == [h.score_parts for h in second]


def test_score_is_normalized_to_unit_interval() -> None:
    """score ∈ [0, 1]（四项权重和为 1）。"""
    rules = _same_score_rules()
    for hint in select_rule_hints("纹波限值判据", rules=rules, top_n=3):
        assert 0.0 <= hint.score <= 1.0


# --------------------------------------------------------------------------- kinds


def test_kinds_filter_selects_only_requested_kind() -> None:
    """kinds 过滤：只要 formula 就不会返回 constraint 型规则。"""
    rules = [
        _rule("K-F-001", statement="纹波限值判据", category="ripple", kind=KIND_FORMULA, expr="a * b"),
        _rule("K-C-001", statement="纹波限值判据", category="ripple", kind=KIND_CONSTRAINT),
    ]

    formulas = select_rule_hints("纹波限值判据", rules=rules, kinds=(KIND_FORMULA,))
    constraints = select_rule_hints("纹波限值判据", rules=rules, kinds=(KIND_CONSTRAINT,))

    assert [h.rule_id for h in formulas] == ["K-F-001"]
    assert [h.rule_id for h in constraints] == ["K-C-001"]
    assert formulas[0].kind == "formula"
    assert constraints[0].kind == "constraint"


def test_default_kinds_cover_formula_and_constraint() -> None:
    """默认 kinds = (formula, constraint)。"""
    assert RULE_KINDS == (KIND_FORMULA, KIND_CONSTRAINT)
    rules = [
        _rule("K-F-001", statement="纹波判据", category="ripple", kind=KIND_FORMULA, expr="a * b"),
        _rule("K-C-001", statement="纹波判据", category="ripple", kind=KIND_CONSTRAINT),
    ]
    assert len(select_rule_hints("纹波判据", rules=rules, top_n=5)) == 2


def test_unknown_kind_label_fails_fast() -> None:
    """未知 kinds 标签 → ValueError（不静默忽略）。"""
    with pytest.raises(ValueError, match="规则类型标签"):
        select_rule_hints("纹波", rules=_same_score_rules(), kinds=("derive",))


# --------------------------------------------------------------------------- 空/未命中


@pytest.mark.parametrize("query", ["", "   ", "zzz qqq www"])
def test_no_signal_returns_empty_list(query: str) -> None:
    """空查询 / 与规则毫无词重叠的查询 → 空列表, 不抛异常。"""
    assert select_rule_hints(query, rules=_same_score_rules(), top_n=5) == []


def test_empty_query_with_real_rules_returns_empty_list() -> None:
    """真实规则集下空查询同样返回空列表（不是异常）。"""
    assert select_rule_hints("", top_n=5) == []


def test_zero_score_rules_are_filtered_out() -> None:
    """完全不相干的规则（score=0）不入选, 不占 top_n 名额。"""
    rules = _same_score_rules() + [
        _rule("K-OTHER-001", statement="变压器变比判据", category="transformer", kind=KIND_CONSTRAINT)
    ]
    hints = select_rule_hints("纹波限值", rules=rules, top_n=5)
    assert "K-OTHER-001" not in {h.rule_id for h in hints}


# --------------------------------------------------------------------------- 字段完整性


def test_rule_hint_fields_are_populated() -> None:
    """RuleHint 字段齐备: expr/inputs/source/score_parts/matched_terms。"""
    rules = [
        _rule(
            "K-DER-001",
            statement="输出纹波判据",
            category="ripple",
            kind=KIND_FORMULA,
            expr="ripple / (8 * f)",
            inputs=("ripple", "f"),
            source_name="某标准 §4.3",
        )
    ]
    hint = select_rule_hints("纹波", rules=rules, top_n=1)[0]

    assert hint.rule_id == "K-DER-001"
    assert hint.category == "ripple"
    assert hint.kind == "formula"
    assert hint.expr == "ripple / (8 * f)"
    assert hint.inputs == ["ripple", "f"]
    assert hint.source == "某标准 §4.3"
    assert "纹波" in hint.matched_terms
    assert set(hint.score_parts) == {"overlap_stmt", "overlap_aux", "phrase", "category"}


def test_constraint_expr_is_shacl_message_summary() -> None:
    """约束型规则的 expr 是 SHACL ``sh:message`` 摘要（不是 Turtle 原文）。"""
    shape = (
        '@prefix sh: <http://www.w3.org/ns/shacl#> .\n'
        "sh:MyShape a sh:NodeShape ; sh:message \"纹波实测超出规格\" ;"
    )
    rule = DomainRule(
        rule_id="K-SHACL-001",
        statement="纹波判据",
        category="ripple",
        scope="dc",
        constraint={"shape": shape},
        test={"given": {}, "expect": {}},
    )
    hint = select_rule_hints("纹波", rules=[rule], top_n=1)[0]
    assert hint.kind == "constraint"
    assert hint.expr == "约束: 纹波实测超出规格"


def test_rule_hint_to_dict_is_json_friendly() -> None:
    """to_dict 可 JSON 序列化（inputs 转 list, score_parts 转 dict）。"""
    rules = _same_score_rules()
    payload = select_rule_hints("纹波限值", rules=rules, top_n=1)[0].to_dict()

    assert isinstance(payload["inputs"], list)
    assert isinstance(payload["score_parts"], dict)
    assert isinstance(payload["matched_terms"], list)
    assert payload["kind"] == "constraint"


def test_empty_rule_list_returns_empty_hints() -> None:
    """规则集为空（显式传入）→ 空列表, 不抛。"""
    assert select_rule_hints("纹波", rules=[], top_n=3) == []


def test_empty_kinds_tuple_returns_empty_hints() -> None:
    """kinds=() → 没有候选类型, 返回空列表（不放宽过滤）。"""
    assert select_rule_hints("纹波", rules=_same_score_rules(), kinds=()) == []


# --------------------------------------------------------------------------- fail-fast


@pytest.mark.parametrize("top_n", [0, -1, 1.5, "3", True])
def test_invalid_top_n_fails_fast(top_n: Any) -> None:
    """非法 top_n → ValueError。"""
    with pytest.raises(ValueError, match="top_n"):
        select_rule_hints("纹波", rules=_same_score_rules(), top_n=top_n)


def test_missing_rules_source_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """rules=None 且规则源不可用 → RuntimeError（不静默返回空规则集）。"""
    monkeypatch.setenv(REAL_RULES_ENV, str(Path(__file__).parent / "definitely-absent.yaml"))
    with pytest.raises(RuntimeError, match="加载域规则失败"):
        select_rule_hints("纹波", top_n=3)


def test_rule_hint_summary_marks_unavailable_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """summary 在规则源不可用时显式标注 available=False + 原因, 不吞错。"""
    monkeypatch.setenv(REAL_RULES_ENV, str(Path(__file__).parent / "definitely-absent.yaml"))
    summary = rule_hint_summary("纹波", top_n=3)

    assert summary["available"] is False
    assert summary["hints"] == []
    assert "加载域规则失败" in summary["error"]


# --------------------------------------------------------------------------- 关键词表


def test_rule_keywords_table_is_pure_data() -> None:
    """RULE_KEYWORDS 是纯数据表: 值必须是真实 category, 不含 rule_id 字样。"""
    assert "效率" in RULE_KEYWORDS and RULE_KEYWORDS["效率"] == "efficiency"
    assert "纹波" in RULE_KEYWORDS and RULE_KEYWORDS["纹波"] == "ripple"
    assert "保持时间" in RULE_KEYWORDS and RULE_KEYWORDS["保持时间"] == "transient"
    assert "浪涌" in RULE_KEYWORDS and RULE_KEYWORDS["浪涌"] == "inrush"
    assert "回差" in RULE_KEYWORDS and RULE_KEYWORDS["回差"] == "protection"
    assert "降额" in RULE_KEYWORDS and RULE_KEYWORDS["降额"] == "derating"
    assert "精度" in RULE_KEYWORDS and RULE_KEYWORDS["精度"] == "accuracy"
    assert "温度系数" in RULE_KEYWORDS and RULE_KEYWORDS["温度系数"] == "thermal"
    assert "电流纹波" in RULE_KEYWORDS and RULE_KEYWORDS["电流纹波"] == "ripple"
    # 数据表不含规则 id 分支: 键/值都不是 rule_id 形态
    for key, value in RULE_KEYWORDS.items():
        assert not key.startswith("K-") and not value.startswith("K-")


# --------------------------------------------------------------------------- 打分可解释


def test_score_parts_explain_the_score() -> None:
    """分数可解释: score == 四项加权和, score_parts 给出每项明细。"""
    rule = _rule(
        "K-EXP-001",
        statement="电源转换效率 = 输出功率 / 输入功率",
        category="efficiency",
        kind=KIND_FORMULA,
        expr="output_power / input_power * 100",
        inputs=("output_power", "input_power"),
    )
    score, parts, matched = score_rule("转换效率怎么算", rule)

    weights = {"overlap_stmt": 0.55, "overlap_aux": 0.15, "phrase": 0.15, "category": 0.15}
    expected = sum(weights[key] * parts[key] for key in weights)
    assert score == pytest.approx(min(1.0, expected))
    assert score == pytest.approx(expected)  # 未触顶
    assert parts["category"] == 1.0        # 命中效率类提示
    assert parts["overlap_stmt"] > 0.0
    assert matched  # 至少命中一个词


def test_score_rule_with_empty_query_is_zero() -> None:
    """空查询打分恒 0（四项全零）。"""
    rule = _same_score_rules()[0]
    score, parts, matched = score_rule("", rule)
    assert score == 0.0
    assert set(parts.values()) == {0.0}
    assert matched == ()


def test_custom_keyword_table_overrides_categories() -> None:
    """keywords 参数可换表（便于测试与按域定制, 不依赖全局状态）。"""
    rule = _rule("K-X-001", statement="无关内容", category="ripple", kind=KIND_CONSTRAINT)
    _, default_parts, _ = score_rule("纹波", rule)
    _, custom_parts, _ = score_rule("纹波", rule, keywords={"别的东西": "ripple"})
    assert default_parts["category"] == 1.0
    assert custom_parts["category"] == 0.0


# --------------------------------------------------------------------------- 真实规则集


def load_all_rules() -> list[DomainRule]:
    """加载真实规则集（真实 rules.yaml, 缺文件即测试失败, 不跳过）。"""
    return load_rules()


def test_real_rules_load_and_efficiency_query_hits_efficiency_rules() -> None:
    """真实 rules.yaml（132 条）可加载, "效率"类查询命中 efficiency 类规则。"""
    rules = load_all_rules()
    assert len(rules) == 132

    hints = select_rule_hints("电源转换效率是多少", top_n=5, rules=rules)
    assert hints, "效率类查询应至少命中一条规则"
    assert any(h.category == "efficiency" for h in hints)
    assert all(0.0 <= h.score <= 1.0 for h in hints)
    # 规则出处（标准名）非空: 解释性附加必须能说清"依据哪条标准"
    assert all(h.source for h in hints)


def test_bare_efficiency_query_ranks_efficiency_rules_first() -> None:
    """裸"效率"查询: efficiency 类规则排在最前（关键词表命中即加分）。"""
    hints = select_rule_hints("效率是多少", top_n=3, rules=load_all_rules())
    assert [h.category for h in hints[:2]] == ["efficiency", "efficiency"]
    assert hints[0].kind in {"formula", "constraint"}


def test_real_rules_ripple_query_hits_ripple_rules() -> None:
    """真实规则集: 纹波类查询命中 ripple 类规则。"""
    hints = select_rule_hints("输出纹波限值是多少", top_n=5, rules=load_all_rules())
    assert hints and hints[0].category == "ripple"


def test_real_rules_holdup_query_hits_transient_rules() -> None:
    """真实规则集: 保持时间类查询命中 transient 类规则。"""
    hints = select_rule_hints("保持时间要求是多少", top_n=5, rules=load_all_rules())
    assert hints and any(h.category == "transient" for h in hints)


def test_real_rules_repeated_selection_is_identical() -> None:
    """真实规则集上重复选取结果逐位一致（132 条全跑仍确定性）。"""
    rules = load_all_rules()
    first = select_rule_hints("浪涌电流怎么限", top_n=5, rules=rules)
    second = select_rule_hints("浪涌电流怎么限", top_n=5, rules=rules)
    assert [(h.rule_id, h.score) for h in first] == [(h.rule_id, h.score) for h in second]


def test_real_rules_top_n_is_respected() -> None:
    """top_n 截断生效（返回条数 ≤ top_n）。"""
    hints = select_rule_hints("效率 纹波 精度", top_n=3, rules=load_all_rules())
    assert 0 < len(hints) <= 3


def test_real_rules_summary_is_available() -> None:
    """summary 在规则源可用时返回 hints 明细。"""
    summary = rule_hint_summary("保持时间", top_n=3)
    assert summary["available"] is True
    assert summary["count"] == len(summary["hints"])
    assert summary["hints"][0]["rule_id"].startswith("K-")


def test_rule_hint_dataclass_defaults() -> None:
    """RuleHint 额外字段（matched_terms/score_parts）有默认值, 构造只需 8 个必填项。"""
    hint = RuleHint(
        rule_id="K-1", statement="s", category="c", kind="formula",
        expr="e", inputs=["i"], source="src", score=0.5,
    )
    assert hint.matched_terms == ()
    assert hint.score_parts == {}
