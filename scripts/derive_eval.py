#!/usr/bin/env python
"""推导引擎三级对比评估: L1 确定性规划器 / L2 LLM 编排 / L3 Agent 工具循环。

用法::

    python scripts/derive_eval.py                                   # 默认只跑 L1 (离线)
    python scripts/derive_eval.py --modes deterministic,llm,agent   # 需已配置 LLM (settings)
    python scripts/derive_eval.py --questions data/eval/derive_questions.yaml --out data/eval/out

评分口径 (评估集每题按模式给期望, 缺省 = 不评分仅记录):

- ``expect.<mode>: <数值>`` —— 返回值须落在相对容差 ``tol`` (默认 1e-6) 内;
- ``expect.<mode>: none`` —— 期望"不给数值" (缺值/不可达/除零/非计算问题/脚本类
  对规则引擎不可算, 或 L3 拒答); 编造出数值即判失败。

产物: ``data/eval/out/derive-<YYYYmmdd-HHMMSS>.json`` (逐题记录 + 分模式汇总)。
退出码: 0 = 被评分项全部通过且无错误; 1 = 有失败/错误 (含 LLM 未配置)。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from matrag.domain.llm_planner import orchestrate_from_text  # noqa: E402
from matrag.domain.query_derive import derive_from_text  # noqa: E402
from matrag.domain.tool_agent import (  # noqa: E402
    ToolAgentError,
    ToolSupportError,
    compute_with_agent,
)
from matrag.llm import get_llm  # noqa: E402

#: 全部模式 (``--modes`` 取值); 顺序与人读口径一致。
MODES: tuple[str, ...] = ("deterministic", "llm", "agent")

#: 期望字面量: "不给数值"。
EXPECT_NONE = "none"

#: 评估集默认路径 (``questions`` 列表 + 每模式期望)。
DEFAULT_QUESTIONS = REPO_ROOT / "data" / "eval" / "derive_questions.yaml"

#: 评估产物默认目录 (与检索评估 ``run-*.json`` 同目录)。
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "eval" / "out"


# --------------------------------------------------------------------------- #
# 评估集
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvalQuestion:
    """评估集里的一道题 (含分模式期望)。

    Attributes:
        id: 题号 (唯一; 回归测试与报告按它定位)。
        kind: ``rules`` / ``script`` / ``boundary``。
        question: 问题原文。
        expect: 模式 → 期望 (float 或 :data:`EXPECT_NONE`); 缺键 = 不评分。
        tol: 相对容差 (判据 ``|got-expect| <= tol * max(1, |expect|)``)。
    """

    id: str
    kind: str
    question: str
    expect: dict[str, Any]
    tol: float


def load_questions(path: Path | str | None = None) -> list[EvalQuestion]:
    """加载评估集 YAML (缺文件 ``FileNotFoundError``; 结构非法即 ``ValueError``)。"""
    file = Path(path) if path is not None else DEFAULT_QUESTIONS
    if not file.is_file():
        raise FileNotFoundError(f"评估集文件不存在: {file}")
    data = yaml.safe_load(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise ValueError(f"评估集缺少 questions 列表: {file}")
    out: list[EvalQuestion] = []
    seen: set[str] = set()
    for index, raw in enumerate(data["questions"], start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"评估集第 {index} 题不是映射: {raw!r}")
        qid = str(raw.get("id", "")).strip()
        question = str(raw.get("question", "")).strip()
        kind = str(raw.get("kind", "")).strip()
        if not qid or not question or kind not in ("rules", "script", "boundary"):
            raise ValueError(f"评估集第 {index} 题字段非法: {raw!r}")
        if qid in seen:
            raise ValueError(f"评估集题号重复: {qid}")
        seen.add(qid)
        raw_expect = raw.get("expect") or {}
        if not isinstance(raw_expect, dict):
            raise ValueError(f"评估集 {qid} 的 expect 应为映射: {raw_expect!r}")
        expect: dict[str, Any] = {}
        for mode, value in raw_expect.items():
            if mode not in MODES:
                raise ValueError(f"评估集 {qid} 出现未知模式键: {mode!r}")
            if isinstance(value, str):
                if value != EXPECT_NONE:
                    raise ValueError(f"评估集 {qid} 的期望字面量非法: {value!r}")
                expect[mode] = EXPECT_NONE
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                expect[mode] = float(value)
            else:
                raise ValueError(f"评估集 {qid} 的期望值非法: {value!r}")
        tol_raw = raw.get("tol", 1e-6)
        if isinstance(tol_raw, bool) or not isinstance(tol_raw, (int, float)) or tol_raw <= 0:
            raise ValueError(f"评估集 {qid} 的 tol 非法: {tol_raw!r}")
        out.append(
            EvalQuestion(
                id=qid,
                kind=kind,
                question=question,
                expect=expect,
                tol=float(tol_raw),
            )
        )
    if not out:
        raise ValueError(f"评估集 questions 为空: {file}")
    return out


# --------------------------------------------------------------------------- #
# 三种模式的单题运行
# --------------------------------------------------------------------------- #


@dataclass
class ModeOutcome:
    """单题单模式的运行结果。

    Attributes:
        value: 推导/计算出的数值; ``None`` 表示未给出数值。
        target: 目标参数/术语 (无则 ``None``)。
        detail: 路径摘要 (计划/策略/方法) 或未出数值的原因。
        llm_calls: 本模式的 LLM 调用次数。
        elapsed: 秒。
        error: 运行异常摘要 (有则本题判错误)。
    """

    value: float | None
    target: str | None
    detail: str
    llm_calls: int
    elapsed: float
    error: str | None = None


def run_deterministic(question: str) -> ModeOutcome:
    """L1: 确定性规划器 (离线, 不调 LLM)。"""
    started = time.perf_counter()
    found = derive_from_text(question)
    elapsed = time.perf_counter() - started
    if found is None:
        return ModeOutcome(None, None, "no_result", 0, elapsed)
    hops = "+".join(step.rule_id for step in found.derivation.plan)
    return ModeOutcome(found.derivation.value, found.target, f"plan:{hops}", 0, elapsed)


def run_llm(question: str, llm: Any) -> ModeOutcome:
    """L2: LLM 编排 (失败自动回退规划器; ``detail`` 注明是否回退)。"""
    started = time.perf_counter()
    result = orchestrate_from_text(question, llm=llm)
    elapsed = time.perf_counter() - started
    if result is None:
        return ModeOutcome(None, None, "no_result", 0, elapsed)
    hops = "+".join(step.rule_id for step in result.derivation.plan)
    return ModeOutcome(
        result.derivation.value,
        result.target,
        f"strategy:{result.strategy}|plan:{hops}",
        result.llm_calls,
        elapsed,
    )


def run_agent(question: str, llm: Any) -> ModeOutcome:
    """L3: Agent 工具循环 (数值只来自工具; 拒答记为 ``refused:*``)。"""
    started = time.perf_counter()
    try:
        computed = compute_with_agent(question, llm=llm)
    except ToolAgentError as exc:
        elapsed = time.perf_counter() - started
        text = " ".join(str(exc).split())[:60]
        return ModeOutcome(None, None, f"refused:{type(exc).__name__}:{text}", 0, elapsed)
    elapsed = time.perf_counter() - started
    if computed is None:
        return ModeOutcome(None, None, "no_result", 0, elapsed)
    return ModeOutcome(
        computed.value,
        computed.target,
        f"method:{computed.method}",
        computed.llm_calls,
        elapsed,
    )


# --------------------------------------------------------------------------- #
# 评分与汇总
# --------------------------------------------------------------------------- #


def score(expect: Any, outcome: ModeOutcome, tol: float) -> bool | None:
    """按期望评分: ``None`` = 不评分 (题未给该模式期望), 否则 True/False。"""
    if expect is None:
        return None
    if expect == EXPECT_NONE:
        return outcome.value is None
    if outcome.value is None:
        return False
    return abs(outcome.value - expect) <= tol * max(1.0, abs(expect))


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """按模式汇总逐题记录 (通过率/耗时/调用数/回退与拒答)。"""
    scored = [row for row in rows if row["ok"] is not None]
    passed = [row for row in scored if row["ok"]]
    elapsed = sum(row["elapsed"] for row in rows)
    return {
        "questions": len(rows),
        "scored": len(scored),
        "passed": len(passed),
        "failed": len(scored) - len(passed),
        "errors": sum(1 for row in rows if row["error"]),
        "value_count": sum(1 for row in rows if row["value"] is not None),
        "none_count": sum(1 for row in rows if row["value"] is None),
        "refused_count": sum(1 for row in rows if row["detail"].startswith("refused:")),
        "fallback_count": sum(1 for row in rows if "strategy:planner" in row["detail"]),
        "llm_calls_total": sum(row["llm_calls"] for row in rows),
        "elapsed_total": round(elapsed, 3),
        "elapsed_mean": round(elapsed / len(rows), 3) if rows else 0.0,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI 参数。"""
    parser = argparse.ArgumentParser(
        prog="derive_eval",
        description="推导引擎三级对比评估 (确定性规划器 / LLM 编排 / Agent 工具循环)",
    )
    parser.add_argument(
        "--questions",
        default=None,
        help=f"评估集 YAML (默认: {DEFAULT_QUESTIONS})",
    )
    parser.add_argument(
        "--modes",
        default="deterministic",
        help="逗号分隔的模式, 可选 deterministic,llm,agent (默认: deterministic)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help=f"产物目录 (默认: {DEFAULT_OUT_DIR})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """脚本入口。"""
    args = build_parser().parse_args(argv)
    questions_path = Path(args.questions) if args.questions else DEFAULT_QUESTIONS
    questions = load_questions(questions_path)
    modes = [item.strip() for item in args.modes.split(",") if item.strip()]
    bad = [mode for mode in modes if mode not in MODES]
    if bad or not modes:
        print(f"[FAIL] 未知/空模式: {bad or args.modes!r} (可选: {', '.join(MODES)})", file=sys.stderr)
        return 1
    llm = None
    if any(mode in ("llm", "agent") for mode in modes):
        llm = get_llm()
        if not getattr(llm, "configured", False):
            print("[FAIL] llm/agent 模式需要已配置的 LLM (settings)", file=sys.stderr)
            return 1

    print(f"评估集: {questions_path} ({len(questions)} 题) | 模式: {', '.join(modes)}")
    rows: list[dict[str, Any]] = []
    for question in questions:
        print(f"\n[{question.id}] ({question.kind}) {question.question}")
        for mode in modes:
            try:
                if mode == "deterministic":
                    outcome = run_deterministic(question.question)
                else:
                    outcome = run_llm(question.question, llm) if mode == "llm" else run_agent(
                        question.question, llm
                    )
            except ToolSupportError as exc:
                print(f"[FAIL] 模型 tools 能力预检失败, 评估终止: {exc}", file=sys.stderr)
                return 1
            except Exception as exc:  # noqa: BLE001 — 单题失败不拖垮全轮, 记录后继续
                outcome = ModeOutcome(
                    None, None, "exception", 0, 0.0, error=f"{type(exc).__name__}: {exc}"
                )
            ok = score(question.expect.get(mode), outcome, question.tol)
            rows.append(
                {
                    "id": question.id,
                    "kind": question.kind,
                    "mode": mode,
                    "ok": ok,
                    "expect": question.expect.get(mode),
                    "value": outcome.value,
                    "target": outcome.target,
                    "detail": outcome.detail,
                    "llm_calls": outcome.llm_calls,
                    "elapsed": round(outcome.elapsed, 4),
                    "error": outcome.error,
                }
            )
            mark = "-" if ok is None else ("PASS" if ok else "FAIL")
            value = "none" if outcome.value is None else f"{outcome.value:.6g}"
            extra = f" error={outcome.error}" if outcome.error else ""
            print(
                f"    {mode:>13} {mark:>4} value={value} calls={outcome.llm_calls} "
                f"{outcome.elapsed:.2f}s {outcome.detail}{extra}"
            )

    out_dir = Path(args.out) if args.out else DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    artifact = out_dir / f"derive-{stamp}.json"
    summary = {
        mode: summarize([row for row in rows if row["mode"] == mode]) for mode in modes
    }
    payload = {
        "timestamp": stamp,
        "questions_file": str(questions_path),
        "modes": modes,
        "results": rows,
        "summary": summary,
    }
    artifact.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n汇总:")
    failed_total = 0
    for mode in modes:
        item = summary[mode]
        failed_total += item["failed"] + item["errors"]
        print(
            f"  {mode:>13}: 评分 {item['scored']}/{item['questions']} | 通过 {item['passed']} "
            f"| 失败 {item['failed']} | 错误 {item['errors']} | 均值 {item['elapsed_mean']}s "
            f"| 调用 {item['llm_calls_total']} | 回退 {item['fallback_count']} "
            f"| 拒答 {item['refused_count']}"
        )
    print(f"产物: {artifact}")
    return 1 if failed_total else 0


if __name__ == "__main__":
    sys.exit(main())
