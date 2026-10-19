"""检索评测脚本（P1-7）: 跑 ``data/eval/qa.jsonl`` → hit@k / MRR / section_hit / must_contain。

用法::

    python -m scripts.run_eval                       # 全量评测（understand 默认开）
    python -m scripts.run_eval --no-understand       # 关掉语义查询理解，纯 parse_query
    python -m scripts.run_eval --top-k 8 --limit 10
    python -m scripts.run_eval --tier spec --tier fault

口径（全部写进 ``data/eval/out/run-<ts>.json``，逐题可追溯）:

==================  ==========================================================
指标                 定义
==================  ==========================================================
``hit@{1,3,8}``      ``expected_doc_id`` 出现在 top-k 的命中 ``doc_id`` 里
``mrr``             ``expected_doc_id`` **首次**出现名次的倒数（未出现记 0）
``section_hit@k``   top-k 里任一命中的 ``section_titles`` 文本包含任一
                    ``expected_sections``
``must_contain@k``  top-k 命中文本拼接后包含任一 ``must_contain`` 词条
==================  ==========================================================

**失败一律显式，不静默降级**:

- embedder 未配置 / 维度探测失败 / 索引库缺失 → 打印原因并 ``exit 2``;
- ``understand``（W3-A 的 ``matrag.retrieve.understand``）取不到 → 回落纯
  ``retrieve``，并在输出里标 ``understand=unavailable`` + 具体原因;
- 单题异常 → 该题记 ``error`` 行继续跑，不中断整轮;
- ``where`` 过滤条件不被索引腿接受 → 记降级并**退回无条件检索**（而不是让整题报错）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from matrag.config import settings

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 默认评测集文件名（相对 ``settings.eval_dir``）
DEFAULT_QA_NAME = "qa.jsonl"

#: 默认 top_k
DEFAULT_TOP_K = 8

#: 参与统计的 hit@k 切点
HIT_KS: tuple[int, ...] = (1, 3, 8)

#: must_contain 只看前 N 条命中
MUST_CONTAIN_TOP_N = 3

#: 判定"语义查询理解大面积降级"的比例阈值。
#:
#: LLM 不可用时 :mod:`matrag.retrieve.understand` 会留痕 ``llm_failed`` /
#: ``llm_unavailable`` 并退回正则基线计划——**每道题都仍然跑完并出指标**。
#: 于是配额耗尽(403 insufficient_quota)这类"LLM 全程挂掉"的情况，会打印一张
#: 和健康轮次毫无区别的指标表并 exit 0。这种假成功比直接失败危险得多：它会被
#: 当成 "understand 开启" 的实测结果记录下来。超过该比例即判定测量失效。
LLM_FAILURE_ABORT_RATIO = 0.5

#: 表示"语义理解这条路整体不可用"的降级 kind
LLM_UNAVAILABLE_KINDS: frozenset[str] = frozenset({"llm_failed", "llm_unavailable"})

#: 进程退出码（CLI 契约，勿随意改）
EXIT_OK = 0            # 全部题目跑完且无降级失效
EXIT_ITEM_ERROR = 1    # 有题目抛异常（逐题留痕，不中断整轮）
EXIT_SETUP = 2         # 环境/评测集不满足（索引缺失、embedder 未配等）
EXIT_MEASUREMENT = 3   # 跑完了，但语义理解大面积降级 → 本轮指标不可用

#: 默认索引库文件名（与 ``scripts/build_index.py`` 落盘一致）
INDEX_DB_NAME = "index.db"

#: 索引腿 where 支持的键（超出的键会被两腿 ``ValueError`` 拒收）
SUPPORTED_WHERE_KEYS: frozenset[str] = frozenset(
    {"doc_id", "product_code", "table_id", "section_contains", "section_path", "kind", "caption", "page"}
)

#: 评测集 tier 枚举（与 ``scripts/mine_eval_qa.py`` 共用口径）
TIERS: tuple[str, ...] = ("spec", "formula", "fault", "cross_doc")

#: 评测集必填字段
REQUIRED_FIELDS: tuple[str, ...] = (
    "qid",
    "query",
    "expected_doc_id",
    "expected_sections",
    "must_contain",
    "answer_hint",
    "tier",
)

_WHITESPACE_RE = re.compile(r"\s+")
_BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)


class EvalSetupError(RuntimeError):
    """评测环境不满足（embedder 未配 / 索引库缺失等）→ 显式退出码 2。"""


def normalize(text: str) -> str:
    """文本归一口径: NFKC + 去 ``<br>`` + 去全部空白（与挖掘脚本同口径）。"""
    folded = unicodedata.normalize("NFKC", str(text or ""))
    return _WHITESPACE_RE.sub("", _BR_RE.sub("", folded))


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class EvalItem:
    """一条评测题目（``qa.jsonl`` 的一行）。"""

    qid: str
    query: str
    expected_doc_id: str
    expected_sections: list[str]
    must_contain: list[str]
    answer_hint: str = ""
    tier: str = "spec"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class HitView:
    """一条检索命中的评测视图（落盘用，字段从 chunk meta 抽取）。"""

    rank: int
    chunk_id: str
    doc_id: str
    page: Any
    section_path: Any
    section_titles: list[str]
    table_id: Any
    caption: Any
    rrf_score: float
    legs_hit: list[str]
    ranks: dict[str, int]
    text_excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "page": self.page,
            "section_path": self.section_path,
            "section_titles": list(self.section_titles),
            "table_id": self.table_id,
            "caption": self.caption,
            "rrf_score": round(float(self.rrf_score), 6),
            "legs_hit": list(self.legs_hit),
            "leg_ranks": dict(self.ranks),
            "text_excerpt": self.text_excerpt,
        }


@dataclass
class ItemResult:
    """一题的评测结果（含逐条命中的可追溯信息）。"""

    qid: str
    tier: str
    query: str
    expected_doc_id: str
    expected_sections: list[str]
    must_contain: list[str]
    answer_hint: str
    first_rank: int | None
    reciprocal_rank: float
    hit: dict[int, bool]
    section_hit: dict[int, bool]
    must_contain_hit: bool | None
    must_contain_matched: list[str]
    matched_by_rank: int | None
    hits: list[HitView]
    plan: dict[str, Any]
    where: dict[str, Any] | None
    route: dict[str, Any]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "qid": self.qid,
            "tier": self.tier,
            "query": self.query,
            "expected_doc_id": self.expected_doc_id,
            "expected_sections": list(self.expected_sections),
            "must_contain": list(self.must_contain),
            "answer_hint": self.answer_hint,
            "first_rank": self.first_rank,
            "reciprocal_rank": self.reciprocal_rank,
            "hit": {str(k): v for k, v in sorted(self.hit.items())},
            "section_hit": {str(k): v for k, v in sorted(self.section_hit.items())},
            "must_contain_hit": self.must_contain_hit,
            "must_contain_matched": list(self.must_contain_matched),
            "must_contain_rank": self.matched_by_rank,
            "plan": self.plan,
            "where": self.where,
            "route": self.route,
            "error": self.error,
            "hits": [h.to_dict() for h in self.hits],
        }


# ---------------------------------------------------------------------------
# 评测集加载
# ---------------------------------------------------------------------------


def load_dataset(path: Path) -> list[EvalItem]:
    """读 ``qa.jsonl`` → :class:`EvalItem` 列表。

    字段缺失/类型不对 → ``ValueError`` fail-fast（评测集坏了必须报错，不能
    悄悄少跑几题还报"指标正常"）。
    """
    if not path.is_file():
        raise EvalSetupError(f"评测集不存在: {path}（先跑 python -m scripts.mine_eval_qa）")
    items: list[EvalItem] = []
    seen: set[str] = set()
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no} 不是合法 JSON: {exc}") from exc
        missing = [f for f in REQUIRED_FIELDS if f not in payload]
        if missing:
            raise ValueError(f"{path}:{line_no} 缺少必填字段 {missing}")
        qid = str(payload["qid"])
        if qid in seen:
            raise ValueError(f"{path}:{line_no} qid 重复: {qid}")
        seen.add(qid)
        items.append(
            EvalItem(
                qid=qid,
                query=str(payload["query"]),
                expected_doc_id=str(payload["expected_doc_id"]),
                expected_sections=[str(s) for s in payload["expected_sections"] or []],
                must_contain=[str(s) for s in payload["must_contain"] or []],
                answer_hint=str(payload.get("answer_hint", "")),
                tier=str(payload["tier"]),
                raw=payload,
            )
        )
    if not items:
        raise ValueError(f"评测集为空: {path}")
    return items


# ---------------------------------------------------------------------------
# 指标计算（纯函数，可离线单测）
# ---------------------------------------------------------------------------


def section_titles_of(meta: dict[str, Any]) -> list[str]:
    """chunk meta 里的章节标题列表（缺 ``section_titles`` 时回退 ``section_path``）。"""
    titles = meta.get("section_titles")
    if isinstance(titles, list) and titles:
        return [str(t) for t in titles]
    path = str(meta.get("section_path") or "")
    return [s.strip() for s in path.split(">") if s.strip()]


def first_doc_rank(hits: Sequence[Any], doc_id: str) -> int | None:
    """``doc_id`` 在命中序列里首次出现的**名次**（1 起）；没出现返回 ``None``。"""
    for index, hit in enumerate(hits, start=1):
        if str(getattr(hit, "doc_id", "")) == doc_id:
            return index
    return None


def hit_at_k(hits: Sequence[Any], doc_id: str, k: int) -> bool:
    """``doc_id`` 是否出现在前 ``k`` 条命中里。"""
    rank = first_doc_rank(hits, doc_id)
    return rank is not None and rank <= k


def section_hit_at_k(
    hits: Sequence[Any], expected_sections: Sequence[str], k: int
) -> bool:
    """前 ``k`` 条命中里，任一条的章节标题串包含任一 ``expected_sections``。"""
    wanted = [normalize(s) for s in expected_sections if normalize(s)]
    if not wanted:
        return False
    for hit in hits[:k]:
        meta = getattr(hit, "meta", {}) or {}
        haystack = normalize(" > ".join(section_titles_of(meta)))
        if any(term in haystack for term in wanted):
            return True
    return False


def must_contain_match(
    hits: Sequence[Any], terms: Sequence[str], *, top_n: int = MUST_CONTAIN_TOP_N
) -> tuple[bool, list[str], int | None]:
    """``must_contain`` 命中判定。

    Returns:
        ``(是否命中, 命中的词条, 首次命中的名次)``; 逐条命中而不是整段拼接，
        这样才能在报告里指出"是哪个词、第几条命中带来的"。
    """
    wanted = [str(t) for t in terms if normalize(t)]
    if not wanted:
        return False, [], None
    matched: list[str] = []
    first_rank: int | None = None
    for index, hit in enumerate(hits[:top_n], start=1):
        body = normalize(getattr(hit, "text", ""))
        for term in wanted:
            needle = normalize(term)
            if needle and needle in body:
                if term not in matched:
                    matched.append(term)
                if first_rank is None:
                    first_rank = index
    return bool(matched), matched, first_rank


def score_item(
    item: EvalItem,
    hits: Sequence[Any],
    *,
    hit_ks: Sequence[int] = HIT_KS,
    must_contain_top_n: int = MUST_CONTAIN_TOP_N,
) -> dict[str, Any]:
    """对一题的命中序列算全部指标（纯函数，无 I/O）。

    Returns:
        指标 dict（``first_rank`` / ``reciprocal_rank`` / ``hit`` / ``section_hit``
        / ``must_contain_*``）; ``must_contain`` 为空时该指标为 ``None``
        （"不适用"，不混进分母）。
    """
    rank = first_doc_rank(hits, item.expected_doc_id)
    mc_hit, mc_terms, mc_rank = must_contain_match(
        hits, item.must_contain, top_n=must_contain_top_n
    )
    return {
        "first_rank": rank,
        "reciprocal_rank": (1.0 / rank) if rank else 0.0,
        "hit": {int(k): hit_at_k(hits, item.expected_doc_id, int(k)) for k in hit_ks},
        "section_hit": {
            int(k): section_hit_at_k(hits, item.expected_sections, int(k)) for k in hit_ks
        },
        "must_contain_hit": (mc_hit if item.must_contain else None),
        "must_contain_matched": mc_terms,
        "must_contain_rank": mc_rank,
    }


def _rate(numerator: int, denominator: int) -> float | None:
    """比率；分母为 0 → ``None``（不适用），不是 0。"""
    return (numerator / denominator) if denominator else None


def llm_unavailable_items(results: Sequence[ItemResult]) -> list[str]:
    """因 LLM 不可用而降级到正则基线计划的题目 qid。

    ``understand`` 在 LLM 挂掉时不抛异常，而是留痕 ``llm_failed`` /
    ``llm_unavailable`` 后退回纯正则计划——**题目照跑、指标照出**。因此
    "LLM 全程不可用"和"LLM 正常工作"在指标表上几乎看不出区别，必须靠降级
    留痕识别，否则一轮全降级的评测会被当成有效实测记下来。
    """
    degraded: list[str] = []
    for item in results:
        if item.error is not None:
            continue
        plan = item.plan or {}
        notes = plan.get("degradations") or []
        if any(
            isinstance(note, dict) and note.get("kind") in LLM_UNAVAILABLE_KINDS
            for note in notes
        ):
            degraded.append(item.qid)
    return degraded


def aggregate(
    results: Sequence[ItemResult], *, hit_ks: Sequence[int] = HIT_KS
) -> dict[str, Any]:
    """汇总整体与分 tier 指标。

    ``errors`` 单独计数并从分母里剔除——一道题报错不该稀释其它题，但也不能
    当成"未命中"混进指标里（那样指标会骗人），所以两边都报出来。

    同样地，``llm_unavailable`` 报出"语义理解整体降级"的题数与比例：比例超
    :data:`LLM_FAILURE_ABORT_RATIO` 时 ``measurement_valid=False``，本轮指标
    衡量的是降级后的正则检索，不能代表"开启 understand"。
    """
    scored = [r for r in results if r.error is None]
    errors = [r for r in results if r.error is not None]
    degraded = llm_unavailable_items(scored)
    llm_ratio = len(degraded) / len(scored) if scored else 0.0

    def _block(rows: Sequence[ItemResult]) -> dict[str, Any]:
        n = len(rows)
        block: dict[str, Any] = {
            "n": n,
            "hit": {str(k): _rate(sum(1 for r in rows if r.hit.get(k)), n) for k in hit_ks},
            "section_hit": {
                str(k): _rate(sum(1 for r in rows if r.section_hit.get(k)), n) for k in hit_ks
            },
            "mrr": _rate(sum(r.reciprocal_rank for r in rows), n),
            "must_contain": _rate(
                sum(1 for r in rows if r.must_contain_hit is True),
                sum(1 for r in rows if r.must_contain_hit is not None),
            ),
            "mean_first_rank": (
                sum(r.first_rank for r in rows if r.first_rank) / n if n else None
            ),
        }
        return block

    by_tier: dict[str, Any] = {}
    for tier in TIERS:
        rows = [r for r in scored if r.tier == tier]
        if rows:
            by_tier[tier] = _block(rows)
    return {
        "n": len(results),
        "scored": len(scored),
        "errors": len(errors),
        "error_qids": [r.qid for r in errors],
        "llm_unavailable": {
            "n": len(degraded),
            "ratio": llm_ratio,
            "qids": degraded,
            "measurement_valid": llm_ratio < LLM_FAILURE_ABORT_RATIO,
        },
        "overall": _block(scored),
        "by_tier": by_tier,
    }


# ---------------------------------------------------------------------------
# 装配（索引 + embedder + 检索器）
# ---------------------------------------------------------------------------


def load_understand() -> tuple[Any | None, str]:
    """惰性取 W3-A 的 ``matrag.retrieve.understand.understand``。

    Returns:
        ``(callable | None, 状态说明)``; 说明串会原样写进评测结果，
        取不到时是显式的 ``unavailable: <原因>``，绝不静默。
    """
    try:
        from matrag.retrieve.understand import understand  # noqa: PLC0415 —— 惰性导入
    except Exception as exc:  # noqa: BLE001 —— 并行任务未落地属正常状态, 原因必须留痕
        return None, f"unavailable: {type(exc).__name__}: {exc}"
    if not callable(understand):
        return None, "unavailable: matrag.retrieve.understand.understand 不是可调用对象"
    return understand, "available"


def build_stack(
    db_path: Path, *, use_embed_cache: bool = True, graph: bool = True
) -> tuple[Any, dict[str, Any], list[Any]]:
    """装配 ``HybridRetriever``（向量腿 + 关键词腿 + 可选图谱腿）。

    Args:
        db_path: 索引库路径。
        use_embed_cache: 是否用 embedding 缓存。
        graph: 是否装配图谱腿（``--no-graph`` 时 False, 跑两腿对照）。

    Returns:
        ``(retriever, 栈信息, 需要关闭的资源)``; 关闭资源由调用方在评测结束后释放。

    Raises:
        EvalSetupError: 索引库缺失 / embedder 未配置 / 维度探测失败 /
            图谱腿装配失败（评测 fail-closed, 见到显式 ``--no-graph`` 才允许两腿）。
    """
    if not db_path.is_file():
        raise EvalSetupError(
            f"索引库不存在: {db_path}（先跑 python -m scripts.build_index）"
        )
    from matrag.embeddings import CachedEmbeddingClient, MaaSEmbeddingClient
    from matrag.index import KeywordIndex, VectorIndex
    from matrag.retrieve import HybridRetriever

    inner = MaaSEmbeddingClient()
    if not inner.configured:
        raise EvalSetupError(
            "embedder 未配置（MATRAG_EMBED_BASE / MATRAG_EMBED_API_KEY / MATRAG_EMBED_MODEL "
            "三者需齐备）；评测需要向量腿，不接受只跑关键词腿的静默降级"
        )
    embedder: Any = inner
    if use_embed_cache:
        embedder = CachedEmbeddingClient(inner, str(db_path) + ".embcache")
    try:
        dimension = embedder.dim  # fail-closed: 探测失败直接抛
    except Exception as exc:  # noqa: BLE001 —— 装配失败必须显式, 不能带病开跑
        raise EvalSetupError(
            f"embedding 维度探测失败: {type(exc).__name__}: {exc}"
        ) from exc

    vector_index = VectorIndex(db_path, dimension=dimension, embed_model=embedder.model)
    keyword_index = KeywordIndex(db_path)
    graph_index: Any = None
    if graph:
        from matrag.retrieve.graph_index import build_graph_index

        try:
            graph_index = build_graph_index(keyword_index.chunks_by_doc)
        except Exception as exc:  # noqa: BLE001 —— 评测对装配失败 fail-closed
            raise EvalSetupError(
                f"图谱腿装配失败: {type(exc).__name__}: {exc}"
                "（确认域知识数据完整; 两腿对照请显式加 --no-graph）"
            ) from exc
    retriever = HybridRetriever(
        vector_index=vector_index,
        keyword_index=keyword_index,
        embedder=embedder,
        graph_index=graph_index,
    )
    info = {
        "db_path": str(db_path),
        "embed_model": embedder.model,
        "dimension": dimension,
        "embed_cache": bool(use_embed_cache),
        "vector_count": vector_index.count(),
        "keyword_count": keyword_index.count(),
        "graph_leg": bool(graph_index),
    }
    return retriever, info, [vector_index, keyword_index]


def _where_from_plan(
    plan: Any, *, query: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """把 ``QueryPlan.filters`` 转成两条索引腿都认的 ``where``。

    走 :func:`matrag.retrieve.routing.build_where`（已测的既有契约）:
    章节号/章节名统一转 ``section_contains``，表号只在索引口径下转 ``table_id``，
    表达不了的键写进 notes（= 降级留痕）而不是悄悄丢掉。
    """
    if plan is None:
        return None, []
    filters = getattr(plan, "filters", None)
    if not isinstance(filters, dict) or not filters:
        return None, []
    from matrag.retrieve.query import ParsedQuery
    from matrag.retrieve.routing import build_where

    notes: list[dict[str, Any]] = []
    try:
        where = build_where(ParsedQuery(raw=query, filters=dict(filters)), notes=notes)
    except Exception as exc:  # noqa: BLE001 —— 计划里的过滤条件坏了不能毁掉整题
        return None, [{"kind": "plan_filters_invalid", "detail": f"{type(exc).__name__}: {exc}"}]
    if where:
        unsupported = sorted(set(where) - SUPPORTED_WHERE_KEYS)
        if unsupported:
            notes.append(
                {"kind": "where_key_unsupported", "keys": unsupported,
                 "detail": "索引腿不接受这些键，已剔除（不静默）"}
            )
            where = {k: v for k, v in where.items() if k in SUPPORTED_WHERE_KEYS} or None
    return where, notes


def _plan_summary(plan: Any) -> dict[str, Any]:
    """``QueryPlan`` 的可追溯摘要（鸭子类型，取得到什么记什么）。"""
    if plan is None:
        return {"available": False}
    summary: dict[str, Any] = {"available": True, "type": type(plan).__name__}
    for field_name in (
        "filters",
        "retrieval_queries",
        "sub_queries",
        "needs_rules",
        "degradations",
        "kinds",
    ):
        if hasattr(plan, field_name):
            summary[field_name] = _jsonable(getattr(plan, field_name))
    return summary


def _jsonable(value: Any, *, depth: int = 0) -> Any:
    """把任意对象转成可 JSON 序列化、深度受限的结构。"""
    if depth > 4:
        return repr(value)[:200]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth=depth + 1) for k, v in list(value.items())[:32]}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, depth=depth + 1) for v in list(value)[:32]]
    if hasattr(value, "to_dict"):
        try:
            return _jsonable(value.to_dict(), depth=depth + 1)
        except Exception:  # noqa: BLE001 —— 摘要失败退 repr, 不影响主流程
            return repr(value)[:200]
    return str(value)[:200]


def _invoke_retrieve(
    retriever: Any, query: str, *, plan: Any, top_k: int, where: dict[str, Any] | None
) -> tuple[list[Any], str]:
    """执行检索：优先 ``retrieve_with_plan``（若存在），否则 ``retrieve``。

    用 ``getattr`` 探测 + 签名适配探测；调用不兼容时**回落** ``retrieve``
    并把回落原因带进逐题记录（W3-A 的接口尚未定型，这里不能写死）。
    """
    fn = getattr(retriever, "retrieve_with_plan", None)
    if plan is None or not callable(fn):
        return list(retriever.retrieve(query, top_k=top_k, where=where)), "retrieve"
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    kwargs: dict[str, Any] = {}
    accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    if "top_k" in params or accepts_kwargs:
        kwargs["top_k"] = top_k
    if "where" in params or accepts_kwargs:
        kwargs["where"] = where
    for name in ("plan", "query_plan"):
        if name in params:
            kwargs[name] = plan
            break
    try:
        return list(fn(query, **kwargs)), "retrieve_with_plan"
    except TypeError as exc:
        return (
            list(retriever.retrieve(query, top_k=top_k, where=where)),
            f"retrieve(retrieve_with_plan 签名不兼容: {exc})",
        )


def _hit_views(hits: Sequence[Any], explained: dict[str, Any]) -> list[HitView]:
    """命中列表 → 落盘视图（页/章节/表号/分数/各腿名次全留）。"""
    views: list[HitView] = []
    for index, hit in enumerate(hits, start=1):
        meta = getattr(hit, "meta", {}) or {}
        detail = explained.get(getattr(hit, "chunk_id", ""), {}) if explained else {}
        views.append(
            HitView(
                rank=index,
                chunk_id=str(getattr(hit, "chunk_id", "")),
                doc_id=str(getattr(hit, "doc_id", "")),
                page=meta.get("page"),
                section_path=meta.get("section_path"),
                section_titles=section_titles_of(meta),
                table_id=meta.get("table_id"),
                caption=meta.get("caption"),
                rrf_score=float(getattr(hit, "rrf_score", 0.0)),
                legs_hit=[str(leg) for leg in getattr(hit, "legs_hit", ())],
                ranks={str(k): int(v) for k, v in (detail.get("ranks") or {}).items()},
                text_excerpt=str(getattr(hit, "text", ""))[:200],
            )
        )
    return views


def run_dataset(
    items: Sequence[EvalItem],
    retriever: Any,
    *,
    top_k: int = DEFAULT_TOP_K,
    use_understand: bool = True,
    understand_fn: Any | None = None,
    understand_state: str = "disabled",
    llm: Any | None = None,
    hit_ks: Sequence[int] = HIT_KS,
    progress: Any | None = None,
) -> list[ItemResult]:
    """跑完整评测集，返回逐题结果。

    单题异常被捕获成 ``ItemResult.error``，不影响其余题目（"失败要显式，
    但不因为一题崩掉整轮"）。
    """
    results: list[ItemResult] = []
    for index, item in enumerate(items, start=1):
        if progress:
            progress(index, len(items), item)
        try:
            results.append(
                _run_one(
                    item,
                    retriever,
                    top_k=top_k,
                    use_understand=use_understand,
                    understand_fn=understand_fn,
                    llm=llm,
                    hit_ks=hit_ks,
                )
            )
        except Exception as exc:  # noqa: BLE001 —— 单题失败留痕, 不中断整轮
            results.append(
                ItemResult(
                    qid=item.qid,
                    tier=item.tier,
                    query=item.query,
                    expected_doc_id=item.expected_doc_id,
                    expected_sections=list(item.expected_sections),
                    must_contain=list(item.must_contain),
                    answer_hint=item.answer_hint,
                    first_rank=None,
                    reciprocal_rank=0.0,
                    hit={int(k): False for k in hit_ks},
                    section_hit={int(k): False for k in hit_ks},
                    must_contain_hit=None,
                    must_contain_matched=[],
                    matched_by_rank=None,
                    hits=[],
                    plan={"available": False},
                    where=None,
                    route={},
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return results


def _run_one(
    item: EvalItem,
    retriever: Any,
    *,
    top_k: int,
    use_understand: bool,
    understand_fn: Any | None,
    llm: Any | None,
    hit_ks: Sequence[int],
) -> ItemResult:
    """跑一道题: 计划 → 检索 → 打分 → 组装可追溯记录。"""
    plan = None
    plan_error: str | None = None
    if use_understand and understand_fn is not None:
        try:
            plan = understand_fn(item.query, llm=llm)
        except Exception as exc:  # noqa: BLE001 —— 计划失败留痕并退回纯检索
            plan_error = f"{type(exc).__name__}: {exc}"
    where, where_notes = _where_from_plan(plan, query=item.query)

    hits, mode = _invoke_retrieve(retriever, item.query, plan=plan, top_k=top_k, where=where)
    try:
        explained = retriever.explain(hits)
    except Exception as exc:  # noqa: BLE001 —— explain 只用于溯源, 失败不毁题
        explained = {}
        where_notes = where_notes + [
            {"kind": "explain_failed", "detail": f"{type(exc).__name__}: {exc}"}
        ]

    metrics = score_item(item, hits, hit_ks=hit_ks)
    plan_summary = _plan_summary(plan)
    if plan_error:
        plan_summary["error"] = plan_error
    plan_summary["retrieve_mode"] = mode
    plan_summary["where_notes"] = where_notes
    route = _route_summary(retriever, item.query)
    return ItemResult(
        qid=item.qid,
        tier=item.tier,
        query=item.query,
        expected_doc_id=item.expected_doc_id,
        expected_sections=list(item.expected_sections),
        must_contain=list(item.must_contain),
        answer_hint=item.answer_hint,
        first_rank=metrics["first_rank"],
        reciprocal_rank=metrics["reciprocal_rank"],
        hit=metrics["hit"],
        section_hit=metrics["section_hit"],
        must_contain_hit=metrics["must_contain_hit"],
        must_contain_matched=metrics["must_contain_matched"],
        matched_by_rank=metrics["must_contain_rank"],
        hits=_hit_views(hits, explained),
        plan=plan_summary,
        where=where,
        route=route,
    )


def _route_summary(retriever: Any, query: str) -> dict[str, Any]:
    """路由层结论（问题类型/腿/置信度），拿不到就留空 dict。"""
    planner = getattr(retriever, "plan", None)
    if not callable(planner):
        return {}
    try:
        plan = planner(query)
    except Exception:  # noqa: BLE001 —— 纯诊断信息, 失败不毁题
        return {}
    routed = plan.get("routed") if isinstance(plan, dict) else None
    if routed is None:
        return {}
    return {
        "kind": str(getattr(getattr(routed, "kind", None), "value", "")),
        "legs": [str(leg) for leg in getattr(routed, "legs", ())],
        "confidence": getattr(routed, "confidence", None),
        "reason": str(getattr(routed, "reason", "")),
        "active_legs": [str(leg) for leg in plan.get("active_legs", ())],
        "degradations": plan.get("degradations", []),
    }


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _fmt_rate(value: float | None) -> str:
    return "  n/a " if value is None else f"{value * 100:6.2f}%"


def render_console(
    results: Sequence[ItemResult], summary: dict[str, Any]
) -> str:
    """控制台表格: 逐题一行 + 汇总。"""
    lines = [
        "=" * 108,
        f"{'qid':<44} {'tier':<10} {'hit@1':>6} {'hit@3':>6} {'hit@8':>6} "
        f"{'RR':>6} {'sec@8':>6} {'must':>6}  top-1",
        "-" * 108,
    ]
    for result in results:
        if result.error:
            lines.append(
                f"{result.qid:<44} {result.tier:<10} {'ERROR':>6} {'':>6} {'':>6} "
                f"{'':>6} {'':>6} {'':>6}  {result.error[:70]}"
            )
            continue
        first = result.hits[0].doc_id if result.hits else "-"
        lines.append(
            f"{result.qid:<44} {result.tier:<10} "
            + "".join(f"{('Y' if result.hit.get(k) else '.'):>6} " for k in HIT_KS)
            + f"{result.reciprocal_rank:6.3f} "
            + f"{('Y' if result.section_hit.get(max(HIT_KS)) else '.'):>6} "
            + f"{('Y' if result.must_contain_hit else ('.' if result.must_contain_hit is False else '-')):>6}"
            + f"  {first[:30]}"
        )
    overall = summary["overall"]
    lines.append("-" * 108)
    lines.append(
        f"总体 (n={summary['scored']}, errors={summary['errors']}): "
        + "  ".join(f"hit@{k}={_fmt_rate(overall['hit'][str(k)])}" for k in HIT_KS)
        + f"  MRR={_fmt_rate(overall['mrr'])}"
        + f"  section_hit@{max(HIT_KS)}={_fmt_rate(overall['section_hit'][str(max(HIT_KS))])}"
        + f"  must_contain={_fmt_rate(overall['must_contain'])}"
    )
    for tier, block in summary["by_tier"].items():
        lines.append(
            f"  [{tier:<10}] n={block['n']:<3} "
            + "  ".join(f"hit@{k}={_fmt_rate(block['hit'][str(k)])}" for k in HIT_KS)
            + f"  MRR={_fmt_rate(block['mrr'])}"
            + f"  section_hit@{max(HIT_KS)}={_fmt_rate(block['section_hit'][str(max(HIT_KS))])}"
            + f"  must={_fmt_rate(block['must_contain'])}"
        )
    if summary["errors"]:
        lines.append(f"  失败题目 qids: {summary['error_qids']}")
    return "\n".join(lines)


def write_report(path: Path, payload: dict[str, Any]) -> Path:
    """写评测结果 JSON（UTF-8、不转义中文、缩进 2）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_llm() -> tuple[Any | None, str]:
    """可选的 LLM 门面（查询理解要用）。未配置就是 ``None`` + 说明。"""
    try:
        from matrag.llm import get_llm
    except Exception as exc:  # noqa: BLE001 —— 未装/未配都按"不可用"处理并留痕
        return None, f"unavailable: {type(exc).__name__}: {exc}"
    if not settings.live:
        return None, "unavailable: LLM 凭据未配置（MATRAG_LLM_*）"
    try:
        client = get_llm()
    except Exception as exc:  # noqa: BLE001 —— 装配失败显式留痕, 退回"无 LLM"
        return None, f"unavailable: {type(exc).__name__}: {exc}"
    if not getattr(client, "configured", False):
        return None, "unavailable: LLMClient.configured 为 False"
    return client, "available"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_eval", description="检索评测（P1-7）：hit@k / MRR / section_hit / must_contain"
    )
    parser.add_argument("--dataset", default=None, help="评测集路径（默认 data/eval/qa.jsonl）")
    parser.add_argument("--db", default=None, help="索引库路径（默认 data/store/index.db）")
    parser.add_argument("--out", default=None, help="结果 JSON 目录（默认 data/eval/out）")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="每题取多少条命中")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0=全部）")
    parser.add_argument(
        "--tier", action="append", default=None, help="只跑指定 tier（可多次）"
    )
    parser.add_argument(
        "--no-understand",
        action="store_true",
        help="关闭 W3-A 语义查询理解（默认开启；不可用时自动回落并标注）",
    )
    parser.add_argument(
        "--no-graph", action="store_true", help="不装配图谱腿（两腿对照; 默认装配）"
    )
    parser.add_argument(
        "--no-embed-cache", action="store_true", help="不用 embedding 缓存（直接打远端）"
    )
    parser.add_argument("--quiet", action="store_true", help="不打印逐题进度")
    args = parser.parse_args(argv)

    dataset_path = Path(args.dataset) if args.dataset else Path(settings.eval_dir) / DEFAULT_QA_NAME
    db_path = Path(args.db) if args.db else Path(settings.store_dir) / INDEX_DB_NAME
    out_dir = Path(args.out) if args.out else Path(settings.eval_dir) / "out"

    started = time.time()
    timestamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")

    try:
        items = load_dataset(dataset_path)
    except Exception as exc:  # noqa: BLE001 —— 评测集坏了直接退出, 不产出"零分报告"
        print(f"[FAIL] 评测集加载失败: {type(exc).__name__}: {exc}")
        return 2
    if args.tier:
        wanted = set(args.tier)
        unknown = sorted(wanted - set(TIERS))
        if unknown:
            print(f"[FAIL] 未知 tier {unknown}（合法值: {list(TIERS)}）")
            return 2
        items = [i for i in items if i.tier in wanted]
    if args.limit and args.limit > 0:
        items = items[: args.limit]
    if not items:
        print("[FAIL] 过滤后没有题目可跑")
        return 2

    try:
        retriever, stack_info, closers = build_stack(
            db_path,
            use_embed_cache=not args.no_embed_cache,
            graph=not args.no_graph,
        )
    except EvalSetupError as exc:
        print(f"[FAIL] 评测环境未就绪: {exc}")
        return 2

    llm, llm_state = _build_llm()
    use_understand = not args.no_understand
    if use_understand:
        understand_fn, understand_state = load_understand()
    else:
        understand_fn, understand_state = None, "disabled(--no-understand)"

    def _progress(index: int, total: int, item: EvalItem) -> None:
        if not args.quiet:
            print(f"  [{index}/{total}] {item.qid} …", flush=True)

    print(f"[eval] dataset={dataset_path}  题目={len(items)}  top_k={args.top_k}")
    print(
        f"[eval] understand={understand_state}  llm={llm_state}"
        f"  graph={'on' if stack_info.get('graph_leg') else 'off'}"
    )

    results = run_dataset(
        items,
        retriever,
        top_k=args.top_k,
        use_understand=use_understand,
        understand_fn=understand_fn,
        llm=llm,
        progress=_progress,
    )
    summary = aggregate(results)
    payload = {
        "run": {
            "started_at": datetime.fromtimestamp(started, tz=UTC).astimezone().isoformat(),
            "finished_at": datetime.now(UTC).astimezone().isoformat(),
            "elapsed_s": round(time.time() - started, 2),
            "dataset": str(dataset_path),
            "top_k": args.top_k,
            "understand": understand_state,
            "llm": llm_state,
            "hit_ks": list(HIT_KS),
            "must_contain_top_n": MUST_CONTAIN_TOP_N,
        },
        "stack": stack_info,
        "summary": summary,
        "items": [r.to_dict() for r in results],
    }
    out_path = write_report(out_dir / f"run-{timestamp}.json", payload)

    print()
    print(render_console(results, summary))
    print()
    print(f"[OK] 报告: {out_path}")
    for resource in closers:
        try:
            resource.close()
        except Exception:  # noqa: BLE001 —— 关闭失败不影响已落盘的结果
            pass

    if not summary["llm_unavailable"]["measurement_valid"]:
        block = summary["llm_unavailable"]
        print()
        print("!" * 78)
        print(
            f"[FAIL] 测量失效: {block['n']}/{summary['scored']} 题"
            f"（{block['ratio'] * 100:.0f}%）的语义查询理解因 LLM 不可用而降级，"
            f"超过阈值 {LLM_FAILURE_ABORT_RATIO * 100:.0f}%"
        )
        print(
            "       本轮指标衡量的是降级后的『纯正则检索』，不能解释为 "
            "\"understand 开启\" 的结果。典型原因：MaaS 配额耗尽"
            "（403 insufficient_quota）"
        )
        print(f"       涉及题目: {block['qids'][:5]}{' …' if len(block['qids']) > 5 else ''}")
        print("!" * 78)
        return EXIT_MEASUREMENT
    if summary["errors"]:
        return EXIT_ITEM_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
