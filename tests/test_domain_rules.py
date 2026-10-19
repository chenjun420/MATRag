"""确定性推理规则引擎测试 (P1-3)。

覆盖 132 条规则 (55 公式 + 77 约束) 的逐条自测, 以及引擎/转换器的通用性
(转换器由 rules.yaml 数据驱动, 不得有按 rule_id 的分支——用"凭空构造的规则"
反证这一点)。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from semantica.reasoning import DatalogReasoner

from matrag.domain.cards import resolve_rules_path
from matrag.domain.rules import (
    RuleEvaluationError,
    SafeExpression,
    build_reasoner,
    decode_value,
    encode_value,
    evaluate_rule,
    verify_all_rules,
)
from matrag.domain.rules_loader import DomainRule, load_rules, render_datalog, to_datalog

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATED_DIR = REPO_ROOT / "data" / "domain" / "generated"

RULES = load_rules()
DERIVE = [r for r in RULES if r.is_derive]
CONSTRAINT = [r for r in RULES if r.is_constraint]


def test_rule_set_shape() -> None:
    """132 条规则: 55 公式 + 77 约束, 每条都有 test.given/expect。"""
    assert len(RULES) == 132
    assert len(DERIVE) == 55
    assert len(CONSTRAINT) == 77
    assert len({r.rule_id for r in RULES}) == 132
    for rule in RULES:
        assert rule.given, rule.rule_id
        assert rule.rule_id
        assert rule.statement
        assert rule.output_key


@pytest.mark.parametrize("rule", RULES, ids=[r.rule_id for r in RULES])
def test_rule_self_check(rule: DomainRule) -> None:
    """逐条用 test.given 求值, 与 test.expect 比对, 必须 100% 通过。"""
    result = evaluate_rule(rule, rule.given)
    assert result["ok"], result["detail"]
    assert result["applicable"] is True
    assert result["error"] is None
    assert result["matched"] is True


def test_verify_all_rules_reports_all_passed() -> None:
    """verify_all_rules 汇总: 132 通过, 失败列表为空。"""
    passed, failed = verify_all_rules()
    assert passed == 132
    assert failed == []


def test_verify_all_rules_on_fully_assembled_engine() -> None:
    """整台引擎 (132 条子句一次装配) 同样逐条通过。"""
    engine = build_reasoner()
    passed, failed = verify_all_rules(reasoner=engine)
    assert passed == 132
    assert failed == []


def test_build_reasoner_engine_performs_the_join() -> None:
    """引擎不是装饰: 前提齐备时前推得出结论, 缺前提则查不到。"""
    engine = build_reasoner()
    engine.add_fact("input(voltage)")
    engine.add_fact("input(current)")
    engine.add_fact("evaluated(k_elec_001, power, 599.4)")
    assert engine.query("holds(k_elec_001, ?k, ?v)") == [{"k": "power", "v": "599.4"}]
    # 同一台引擎里另一条规则没有事实, 不应被激活
    assert engine.query("holds(k_elec_002, ?k, ?v)") == []


def test_engine_only_activates_when_inputs_present() -> None:
    """缺少输入参数时规则不激活 (适用性由引擎判定)。"""
    rule = next(r for r in DERIVE if r.rule_id == "K-ELEC-001")
    result = evaluate_rule(rule, {"voltage": 54})
    assert result["ok"] is False
    assert result["applicable"] is False
    assert "current" in result["detail"]


def test_to_datalog_matches_rendered_clauses() -> None:
    """结构化转换与文本转换一致, 且每条子句可被引擎重新解析。"""
    clauses = to_datalog(RULES)
    texts = render_datalog(RULES)
    assert len(clauses) == len(texts) == 132
    for clause, text in zip(clauses, texts, strict=True):
        assert clause.head_predicate == "holds"
        assert clause.head_args[2] == "V"
        assert text.startswith(f"holds({clause.head_args[0]}, {clause.head_args[1]}, V) :- ")
        assert text.endswith(f"evaluated({clause.head_args[0]}, {clause.head_args[1]}, V).")
        inputs = [atom for atom in clause.body if atom.predicate == "input"]
        for atom in inputs:
            assert f"input({atom.args[0]})" in text


def test_all_clauses_parse_in_semantica_dialect() -> None:
    """132 条 Horn 子句必须能被 semantica 的 add_rule 全部吃下。"""
    engine = DatalogReasoner()
    for text in render_datalog(RULES):
        engine.add_rule(text)
    assert len(engine._rules) == 132  # noqa: SLF001 - 引擎内部清单, 用于确认全部装载


def test_generated_artifacts_exist() -> None:
    """本地产物: power_rules.yaml 副本 + power_rules_datalog.dl。"""
    rules_copy = GENERATED_DIR / "power_rules.yaml"
    dl = GENERATED_DIR / "power_rules_datalog.dl"
    assert rules_copy.is_file(), "先跑 scripts/import_domain.py 生成本地产物"
    assert rules_copy.read_bytes() == resolve_rules_path().read_bytes()
    clauses = [
        line
        for line in dl.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert len(clauses) == 132


def test_converter_is_data_driven_not_rule_id_keyed() -> None:
    """转换器/求值器是通用的: 现造一条规则 (不在 rules.yaml 里) 也能跑通。"""
    rule = DomainRule(
        rule_id="X-TEST-009",
        statement="测试用假规则",
        category="test",
        scope="test",
        derive={"output": "area", "expr": "base * height", "inputs": ["base", "height"]},
        test={"given": {"base": 3, "height": 4}, "expect": {"area": 12}},
    )
    result = evaluate_rule(rule, rule.given)
    assert result["ok"], result["detail"]
    assert result["actual"] == 12


def test_constraint_path_is_data_driven() -> None:
    """约束型规则求值同样是通用的: 现造一条 SHACL 形状也能判定。"""
    shape = (
        "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
        "@prefix ps: <https://example.org/test#> .\n"
        "ps:DemoShape a sh:NodeShape ;\n"
        "    sh:targetClass ps:Demo ;\n"
        "    sh:property [\n"
        "        sh:path ps:value ;\n"
        "        sh:maxInclusive 10 ;\n"
        "        sh:message \"值必须 <= 10\" ;\n"
        "    ] .\n"
    )
    rule = DomainRule(
        rule_id="X-TEST-010",
        statement="测试用假约束",
        category="test",
        scope="test",
        constraint={"shape": shape},
        test={"given": {"value": 12}, "expect": {"violation": True}},
    )
    assert evaluate_rule(rule, {"value": 12})["ok"]
    assert evaluate_rule(rule, {"value": 8})["matched"] is False


def test_expression_evaluator_supports_rule_syntax() -> None:
    """白名单求值器覆盖 rules.yaml 里出现的全部语法形态。"""
    cases = [
        ("voltage * current", {"voltage": 54, "current": 11.1}, 599.4),
        ("current * current * resistance", {"current": 3, "resistance": 0.03}, 0.27),
        (
            "(apparent_power ** 2 - active_power ** 2) ** 0.5",
            {"apparent_power": 600, "active_power": 500},
            331.66247903554,
        ),
        ("sum(t * t for t in components) ** 0.5", {"components": [0.3, 0.4]}, 0.5),
        ("0.5 if current <= 1 else 2.5", {"current": 2}, 2.5),
        (
            "-(-throughput * test_time // available_time)",
            {"throughput": 100, "test_time": 60, "available_time": 3600},
            2,
        ),
        ("min(spec_tolerance, process_variation) / 10", {"spec_tolerance": 0.3, "process_variation": 5}, 0.03),
        ("lsb / sqrt(12)", {"lsb": 0.0006103515625}, 0.00017619331945484186),
        (
            "2 * pi * input_frequency * aperture_jitter * input_amplitude",
            {"input_frequency": 1e6, "aperture_jitter": 1e-12, "input_amplitude": 1},
            6.283185307179586e-06,
        ),
        ("1 if default_state == 'normally_closed' else 0", {"default_state": "normally_open"}, 0),
    ]
    for expression, env, expected in cases:
        got = SafeExpression(expression).evaluate(env)
        assert math.isclose(got, expected, rel_tol=1e-9, abs_tol=1e-12), expression


def test_expression_evaluator_rejects_unsafe_syntax() -> None:
    """属性访问/导入/任意调用/lambda 一律拒绝 (fail-fast)。"""
    for bad in (
        "__import__('os').system('echo hi')",
        "().__class__.__bases__[0]",
        "open('x')",
        "voltage.__class__",
        "(lambda x: x)(1)",
        "{'a': 1}",
        "voltage @ 2",
    ):
        with pytest.raises(RuleEvaluationError):
            SafeExpression(bad)


def test_expression_evaluator_requires_variables() -> None:
    """表达式引用了未提供的变量时报错, 不静默当成 0。"""
    with pytest.raises(RuleEvaluationError):
        SafeExpression("voltage * current").evaluate({"voltage": 1})


def test_value_encoding_round_trip() -> None:
    """结论取值能在 Datalog 常量与 Python 值之间往返。"""
    for value in (599.4, 0, -3, True, False, "auto"):
        assert decode_value(encode_value(value)) == value
    with pytest.raises(RuleEvaluationError):
        encode_value({"not": "supported"})
