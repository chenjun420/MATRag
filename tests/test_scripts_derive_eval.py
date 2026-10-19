"""tests for :mod:`scripts.derive_eval`（推导评估集契约, 全离线: 只跑 L1）。

评估集本身是回归契约: 每题按 ``expect.deterministic`` 评分必须全部通过
(缺值/不可达题期望 ``none``; 规则链/单位混算题期望具体数值)。
"""

from __future__ import annotations

import pytest

from scripts import derive_eval as eval_script


def test_questions_loaded_and_wellformed() -> None:
    """评估集 16 题、题号唯一、类别闭集、每题都有 deterministic 期望。"""
    questions = eval_script.load_questions()
    assert len(questions) == 16
    assert len({q.id for q in questions}) == 16
    assert {q.kind for q in questions} <= {"rules", "script", "boundary"}
    assert all(q.expect for q in questions)
    assert all("deterministic" in q.expect for q in questions)


def test_deterministic_expectations_all_hold() -> None:
    """L1 回归契约: 逐题评分必须全部通过 (评分结果不得为 False)。"""
    failures: list[tuple[str, object, object]] = []
    for question in eval_script.load_questions():
        outcome = eval_script.run_deterministic(question.question)
        verdict = eval_script.score(
            question.expect.get("deterministic"), outcome, question.tol
        )
        assert verdict is not None  # 上面已断言每题都有 deterministic 期望
        if verdict is False:
            failures.append((question.id, question.expect["deterministic"], outcome.value))
    assert failures == []


def test_score_none_expectation_rejects_fabricated_value() -> None:
    """``none`` 期望: 出数值即失败, 未出数值 (含拒答) 通过。"""
    fake = eval_script.ModeOutcome(1.0, "x", "plan:K", 0, 0.0)
    assert eval_script.score(eval_script.EXPECT_NONE, fake, 1e-6) is False
    no_result = eval_script.ModeOutcome(None, None, "no_result", 0, 0.0)
    assert eval_script.score(eval_script.EXPECT_NONE, no_result, 1e-6) is True


def test_loader_rejects_bad_structure(tmp_path) -> None:
    """结构门: 未知类别 / 未知模式键 / 题号重复 / 非法期望值都要报 ValueError。"""
    bad_kind = tmp_path / "bad_kind.yaml"
    bad_kind.write_text(
        "questions:\n  - id: X\n    kind: nope\n    question: q\n", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        eval_script.load_questions(bad_kind)

    bad_mode = tmp_path / "bad_mode.yaml"
    bad_mode.write_text(
        "questions:\n  - id: X\n    kind: rules\n    question: q\n    expect: {moonshot: 1}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        eval_script.load_questions(bad_mode)

    duplicated = tmp_path / "dup.yaml"
    duplicated.write_text(
        "questions:\n"
        "  - id: X\n    kind: rules\n    question: a\n    expect: {deterministic: 1.0}\n"
        "  - id: X\n    kind: rules\n    question: b\n    expect: {deterministic: 2.0}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        eval_script.load_questions(duplicated)

    bad_value = tmp_path / "bad_value.yaml"
    bad_value.write_text(
        "questions:\n  - id: X\n    kind: rules\n    question: q\n    expect: {deterministic: maybe}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        eval_script.load_questions(bad_value)
