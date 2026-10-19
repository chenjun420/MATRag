"""RAG 闭环 CLI（用户侧语义入口）: 理解 → 检索 → 规则推理 → 带引用应答。

用法:
    python -m scripts.ask "PA601-D54A 输入特性 下的参数"
    python -m scripts.ask "CY0177 输入欠压保护点是多少" --top-k 6 --json
    python -m scripts.ask "输出纹波超标可能是什么原因" --no-llm     # 确定性路径（无 LLM 往返）
    python -m scripts.ask "输入功率100W、效率90%、输出电压12V，输出电流是多少" --derive agent

链路:
    understand(query)            语义查询理解（LLM 计划 + 型号/章节接地校验）
      → retrieve_with_plan       多查询变体 × 向量腿/关键词腿/图谱腿 × RRF
      → select_rule_hints        确定性规则提示（公式/约束，不调 LLM）
      → answer_question          证据合成 + 行级引用 [n] + 推理链
      → record_audit             审计留痕（provenance_backend, 默认 sqlite）

图谱腿由 KG（域知识图谱）实体链接到源知识卡; KG 构建失败时打 stderr 告警并按
"未接线"继续（两腿照常, 检索层对公式类查询留 ``leg_not_wired`` 降级记录）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from matrag.answer import AnswerResult, answer_question
from matrag.config import settings
from matrag.embeddings import MaaSEmbeddingClient
from matrag.index.keyword import KeywordIndex
from matrag.index.store import VectorIndex
from matrag.llm import get_llm
from matrag.provenance import AuditTrail, ProvenanceError
from matrag.retrieve import GraphIndex, HybridRetriever, build_graph_index


def build_retriever() -> HybridRetriever:
    db: Path = settings.store_dir / "index.db"
    if not db.exists():
        raise FileNotFoundError(f"索引不存在: {db}（先跑 scripts.build_index）")
    probe = VectorIndex(db, dimension=2048, embed_model=settings.embed_model or "unknown")
    dim = probe.stats().get("dimension")
    vec = VectorIndex(db, dimension=int(dim or 2048), embed_model=settings.embed_model or "unknown")
    keyword = KeywordIndex(db)
    return HybridRetriever(
        vector_index=vec,
        keyword_index=keyword,
        embedder=MaaSEmbeddingClient(),
        llm=get_llm(),
        graph_index=build_graph_index_or_none(keyword),
    )


def build_graph_index_or_none(keyword: KeywordIndex) -> GraphIndex | None:
    """装配图谱腿（KG 接线）; 失败 → 打警告并返回 ``None``（不阻断两腿检索）。

    返回 ``None`` 时，检索层对 FORMULA 类查询会留下 ``leg_not_wired`` 降级记录
    （可查询）; 这里的 stderr 告警给出**原因**（KG 数据缺失/损坏等）。
    """
    try:
        return build_graph_index(keyword.chunks_by_doc)
    except Exception as exc:  # noqa: BLE001 —— 图谱腿是增强项, 缺 KG 数据不应阻断应答
        print(f"[WARN] 图谱腿未接线: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def record_audit(
    query: str,
    result: AnswerResult,
    *,
    elapsed_ms: int,
    model: str,
    trail: AuditTrail | None = None,
) -> dict[str, Any]:
    """把一次应答写入审计轨（``provenance_backend`` 真接）; 失败不阻断但必须留痕。

    Args:
        query: 用户原始问题。
        result: ``AnswerResult``（读 ``plan`` / ``hits`` / ``citations`` / ``rule_hints``
            / ``degradations``）。
        elapsed_ms: 应答耗时（毫秒）。
        model: 合成所用模型名; 确定性路径（``--no-llm``）传空串。
        trail: 审计轨; 默认按 ``settings.provenance_backend`` 新建（测试可注入 memory）。

    Returns:
        成功 ``{"ok": True, "answer_id": ..., "backend": ..., "db_path": ...}``;
        失败 ``{"ok": False, "error": "ExcType: ..."}`` 且已向 ``result.degradations``
        追加 ``provenance_unavailable`` / ``provenance_failed`` 留痕（可查询）。
    """
    own = trail is None
    try:
        if trail is None:
            trail = AuditTrail()
        record = trail.record_answer(
            query=query,
            plan_summary=getattr(result, "plan", None),
            hits=getattr(result, "hits", ()) or (),
            citations=result.citations,
            rule_outcomes=result.rule_hints,
            degradations=result.degradations,
            model=model,
            elapsed_ms=int(elapsed_ms),
            extra={"covered": bool(result.covered)},
        )
        return {
            "ok": True,
            "answer_id": record.answer_id,
            "backend": trail.backend,
            "db_path": str(trail.db_path) if trail.db_path else None,
        }
    except ProvenanceError as exc:
        _note_audit_failure(result, "provenance_unavailable", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:  # noqa: BLE001 —— 审计是旁路, 任何失败都不阻断已完成的应答
        _note_audit_failure(result, "provenance_failed", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if own and trail is not None:
            trail.close()


def _note_audit_failure(result: Any, kind: str, exc: BaseException) -> None:
    """向 ``result.degradations`` 追加审计失败留痕（列表不存在时忽略）。"""
    degradations = getattr(result, "degradations", None)
    if isinstance(degradations, list):
        degradations.append({
            "kind": kind,
            "detail": f"审计写入失败: {type(exc).__name__}: {exc}",
        })


def _configure_stdio() -> None:
    """把标准输出/错误的编码错误策略改为 ``replace``。

    Windows 下 stdout 被重定向/管道捕获时按本地码页（GBK）编码, 答案里偶发的
    特殊符号（U+2212 减号、上标等）会让 ``print`` 抛 ``UnicodeEncodeError``
    导致 CLI 中途崩掉; 改为替换输出（显示 ``?``）保证结果完整打印。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="replace")


def _print_json(text: str) -> None:
    """输出 JSON: 交互终端走控制台编码; 管道/重定向输出 UTF-8 字节（跨平台可读）。"""
    if sys.stdout.isatty():
        print(text)
        return
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        print(text)
        return
    buffer.write(text.encode("utf-8") + b"\n")
    buffer.flush()


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    ap = argparse.ArgumentParser(prog="ask")
    ap.add_argument("query", help="自然语言查询（可含型号/章节/口语表达）")
    ap.add_argument("--top-k", type=int, default=settings.answer_max_items // 2)
    ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    ap.add_argument("--no-llm", action="store_true", help="确定性路径（不调 LLM）")
    ap.add_argument(
        "--derive",
        choices=("deterministic", "llm", "agent"),
        default="deterministic",
        help=(
            "多跳推导编排: deterministic=规划器(默认); llm=LLM 选计划+引擎执行(失败回退规划器); "
            "agent=原生工具循环(规则工具+Monty 沙箱脚本; 须模型支持 tools, 不支持即报错)"
        ),
    )
    args = ap.parse_args(argv)

    if not args.query.strip():
        print("[FAIL] 空查询")
        return 2

    llm = None if args.no_llm else get_llm()
    try:
        retriever = build_retriever()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        return 2

    started = time.perf_counter()
    try:
        result = answer_question(
            args.query,
            retriever=retriever,
            llm=llm,
            top_k=args.top_k,
            derive_mode=args.derive,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 应答失败: {type(exc).__name__}: {exc}")
        return 2
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    audit = record_audit(
        args.query,
        result,
        elapsed_ms=elapsed_ms,
        model=(settings.llm_model if llm is not None else ""),
    )

    if args.json:
        payload = result.to_dict()
        payload["audit"] = audit
        _print_json(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("=" * 72)
    print(f"问: {args.query}")
    print(f"覆盖(规格书是否给出): {'是' if result.covered else '否/证据不足'}")
    print("-" * 72)
    print(result.answer.strip() or "(空答案)")
    if result.citations:
        print("\n引用:")
        for c in result.citations:
            loc = f"{c.doc_id} p{c.page}"
            if c.section_path:
                loc += f" {c.section_path}"
            if c.table_id:
                loc += f" [{c.table_id}]"
            print(f"  {c.marker} {loc} score={c.score:.4f}")
    if result.rule_hints:
        print("\n确定性规则参考:")
        for r in result.rule_hints[:5]:
            print(f"  {r.get('rule_id')} [{r.get('kind')}] {str(r.get('statement', ''))[:60]}")
    print("\n推理步骤:")
    for s in result.steps:
        print(f"  - {s}")
    if result.degradations:
        print("\n降级留痕:")
        for d in result.degradations:
            print(f"  ! {d.get('kind')}")
    if audit.get("ok"):
        print(f"\n审计记录: {audit['answer_id']}（backend={audit['backend']}）")
    else:
        print(f"\n[WARN] 审计写入失败: {audit.get('error')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
