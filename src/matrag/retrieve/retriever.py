"""混合检索: vector + keyword（graph 预留）多腿检索 → RRF 融合 → 引用溯源。

流程（每次 ``retrieve``）::

    parse_query ──► route ──► where 合并(调用方优先) ──► 每腿各取 top_k*3
        ──► SearchRanker.reciprocal_rank_fusion(k=fusion_k) ──► 归一化 per_leg ──► 截断 top_k

多查询变体（``retrieve_many`` / ``retrieve_with_plan``）在此之上再叠一层::

    主查询 parse_query ──► 变体去重保序 ──► 每 (腿 × 变体) 各取 top_k*3
        ──► 同一 RRF 对全部名次表融合（同 chunk 只出现一次，分数跨变体累加）

约定与取舍:

- **语义层优先复用 semantica**: 等权融合直接调
  ``semantica.vector_store.SearchRanker.reciprocal_rank_fusion``（每个 result dict
  必须带 ``"id"`` 键，融合后 ``result["score"]`` 即 RRF 总分）; 只有当
  ``vector_weight``/``keyword_weight`` 偏离 1.0 时才按腿加权自算（公式见
  :meth:`HybridRetriever._fuse`），此时仍按腿调用同一个 ``SearchRanker``，
  只是把加权挪到"腿级名次分"上;
- **per_leg 归一化名次分**: ``per_leg[leg] = Σ_变体 w_leg / (fusion_k + rank)``,
  单查询 + 等权时恒有 ``rrf_score == sum(per_leg.values())``（可手算对账）;
- **LLM 改写是可选增强、默认关**: ``llm=None`` 时绝不触碰 LLM; 传入后先用一次
  查询改写（生成 1~2 个同义查询并行检索再融合），改写失败留痕不改写;
- **查询计划（W3-A ``understand.QueryPlan``）是可选的**: ``retrieve_with_plan``
  只对传入的 plan 做**鸭子类型**读字段（``retrieval_queries``/``filters``/
  ``needs_rules``/``degradations``…），``plan=None`` 时行为与 :meth:`retrieve`
  逐位一致; :func:`build_query_plan` 惰性导入 understand，缺模块只留痕不崩;
- **规则提示是解释性附加、不是检索结果**: plan 声明 ``needs_rules`` 时把
  :func:`matrag.retrieve.rule_hint.select_rule_hints` 的结果挂到
  :attr:`HybridRetriever.last_rule_hints`，**绝不参与排序**;
- **降级必留痕**: 单腿缺索引、graph 腿未接线、LLM 失败、空变体、plan 字段缺失、
  规则源不可用、腿内重复 id 等一律记入 ``plan["degradations"]`` /
  :attr:`HybridRetriever.degradations`。

P1-4 索引契约（duck-typed，本模块不 import 真实 index）::

    vector_index.search_vector(vector, *, top_k=10, where=None) -> list[ScoredChunk]
    keyword_index.search_keyword(query, *, top_k=10, where=None) -> list[ScoredChunk]
    embedder.embed(text) -> list[float]
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from semantica.vector_store import SearchRanker

from matrag.retrieve.query import STOPWORDS, ParsedQuery, parse_query, resolve_tokenizer
from matrag.retrieve.routing import (
    DEFAULT_LEGS,
    LEG_GRAPH,
    LEG_KEYWORD,
    LEG_VECTOR,
    TABLE_ID_SHAPE,
    build_where,
    route,
)

#: RRF 常数（semantica ``SearchRanker`` 同款默认值）
FUSION_K = 60
#: overfetch 倍数: 两腿各取 top_k*3，融合后再截断
OVERFETCH = 3
#: 已知腿名（未知腿名 fail-fast）
KNOWN_LEGS: frozenset[str] = frozenset({LEG_VECTOR, LEG_KEYWORD, LEG_GRAPH})
#: LLM 一次改写最多产出几个同义查询
MAX_REWRITES = 2
#: 多查询入口默认挂几条规则提示
RULE_HINT_TOP_N = 5
#: 原查询在多查询融合中的权重（改写变体固定为 1.0）。
#: RRF 系统性偏好"出现在越多名次表里"的 chunk，而 LLM 改写彼此高度相关
#: （同一 query 的同义句）——共识不等于独立证据。原查询是用户唯一真实意图的
#: 载体，给它更高权重以抵消改写引入的广度噪声。
#: 实测依据见 TODO.md P1-7：等权融合把 spec 层 section_hit@8 从 95.8% 打到 70.8%。
PRIMARY_VARIANT_WEIGHT = 3.0
#: 退化记录的保留条数（防止长跑进程无限增长）
_MAX_DEGRADATIONS = 200


@dataclass
class RetrievalHit:
    """一条融合后的检索命中。

    - ``rrf_score``: 融合分（RRF，保留原始口径）;
    - ``per_leg``: 每条请求腿的归一化名次分（多查询时按变体累加），未命中为 ``None``;
    - ``legs_hit``: 实际命中该 chunk 的腿（``per_leg`` 非 ``None`` 的那些）;
    - ``plan_meta``: 本次检索所用查询计划的摘要（kind/intent/sub_queries/
      needs_rules/retrieval_queries）; 无计划检索为 ``None``。
    """

    chunk_id: str
    doc_id: str
    text: str
    meta: dict[str, Any]
    rrf_score: float
    per_leg: dict[str, float | None]
    legs_hit: tuple[str, ...]
    plan_meta: dict[str, Any] | None = None


@dataclass
class _Payload:
    """chunk 载荷（腿间共享，先见先得，重复出现需一致否则留痕）。"""

    chunk_id: str
    doc_id: str
    text: str
    meta: dict[str, Any]


def _chunk_field(chunk: Any, name: str, default: Any = None) -> Any:
    """读 ``ScoredChunk`` 字段（鸭子类型: 兼容属性对象与 dict）。"""
    if isinstance(chunk, dict):
        return chunk.get(name, default)
    return getattr(chunk, name, default)


def _payload_of(chunk: Any) -> _Payload:
    """从索引返回的 chunk 抽出引用锚点载荷; 缺 ``chunk_id`` 直接 fail-fast。"""
    chunk_id = _chunk_field(chunk, "chunk_id")
    if not isinstance(chunk_id, str) or not chunk_id:
        raise ValueError(f"索引返回的 chunk 缺少 chunk_id: {chunk!r}")
    meta = _chunk_field(chunk, "meta", {}) or {}
    if not isinstance(meta, dict):
        raise ValueError(f"chunk {chunk_id} 的 meta 不是 dict: {meta!r}")
    doc_id = _chunk_field(chunk, "doc_id", "") or ""
    text = _chunk_field(chunk, "text", "") or ""
    return _Payload(chunk_id=chunk_id, doc_id=str(doc_id), text=str(text), meta=dict(meta))


def _dedupe_variants(queries: Sequence[str] | None) -> list[str]:
    """查询变体去重保序（去首尾空白，丢弃空串）。"""
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in queries or ():
        text = str(raw).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return ordered


def _plan_get(plan: Any, name: str, default: Any = None) -> Any:
    """读查询计划字段（鸭子类型: 兼容 ``QueryPlan`` 属性对象与 dict 形态计划）。"""
    if isinstance(plan, Mapping):
        return plan.get(name, default)
    return getattr(plan, name, default)


def build_query_plan(query: str, *, llm: Any = None) -> tuple[Any | None, list[dict[str, Any]]]:
    """经 :mod:`matrag.retrieve.understand` 惰性构造查询计划（W3-A 提供）。

    本模块**不硬依赖** understand: 导入失败 / 缺少计划构造入口都只返回降级记录,
    由调用方决定回退（:meth:`HybridRetriever.retrieve_understood` 退化为
    ``retrieve_many([query])``），绝不静默假装拿到了计划。

    构造入口按候选名依次探测（鸭子类型，不锁死单一名字）:
    ``build_query_plan`` → ``plan_query`` → ``understand``。

    Returns:
        ``(plan, notes)``: ``plan`` 为 ``None`` 时 ``notes`` 里必有
        ``kind="understand_unavailable"`` 的降级记录（含原因）。
    """
    try:
        # 用 importlib 按模块取: 包名 ``understand`` 已被函数导出遮蔽,
        # ``from matrag.retrieve import understand`` 拿到的是函数而非模块。
        import importlib

        understand_module = importlib.import_module("matrag.retrieve.understand")
    except ImportError as exc:
        return None, [{
            "kind": "understand_unavailable",
            "detail": f"{type(exc).__name__}: {exc}",
            "fallback": "retrieve_many([query])",
        }]
    factory = None
    for name in ("build_query_plan", "plan_query", "understand"):
        candidate = getattr(understand_module, name, None)
        if callable(candidate):
            factory = candidate
            break
    if factory is None:
        return None, [{
            "kind": "understand_unavailable",
            "detail": "matrag.retrieve.understand 已就绪但未提供计划构造入口"
                      "（build_query_plan / plan_query / understand）",
            "fallback": "retrieve_many([query])",
        }]
    try:
        return factory(query, llm=llm), []
    except TypeError:  # 替身签名更窄（只收 query）
        return factory(query), []


class HybridRetriever:
    """混合检索器（vector + keyword，graph 腿预留）。

    Args:
        vector_index: P1-4 向量索引（``search_vector``）; 可为 ``None`` 表示
            该腿缺位（其余腿照常检索，缺腿会留痕）。
        keyword_index: P1-4 关键词索引（``search_keyword``）; 可为 ``None``。
        embedder: embedding 门面（``matrag.embeddings.get_embedder`` 或替身）,
            仅 vector 腿触达时才被调用。
        fusion_k: RRF 常数（默认 60）。
        vector_weight / keyword_weight: 腿权重，默认 1.0（等权走 semantica
            RRF 原路）; 任一偏离 1.0 时切换为腿加权名次分融合。
        llm: 可选 LLM 门面；``None`` 时全程不碰 LLM。
        graph_index: 预留（P1-5b 知识图谱腿）; 接线后 ``legs`` 里的 ``graph``
            才会真正执行。
    """

    def __init__(
        self,
        *,
        vector_index: Any,
        keyword_index: Any,
        embedder: Any,
        fusion_k: int = FUSION_K,
        vector_weight: float = 1.0,
        keyword_weight: float = 1.0,
        primary_variant_weight: float = PRIMARY_VARIANT_WEIGHT,
        llm: Any = None,
        graph_index: Any = None,
    ) -> None:
        if not isinstance(fusion_k, int) or fusion_k < 1:
            raise ValueError(f"fusion_k 必须 >= 1 的整数: {fusion_k!r}")
        for name, weight in (
            ("vector_weight", vector_weight),
            ("keyword_weight", keyword_weight),
            ("primary_variant_weight", primary_variant_weight),
        ):
            if not isinstance(weight, (int, float)) or weight <= 0:
                raise ValueError(f"{name} 必须 > 0: {weight!r}")
        self._vector_index = vector_index
        self._keyword_index = keyword_index
        self._embedder = embedder
        self._fusion_k = int(fusion_k)
        self._vector_weight = float(vector_weight)
        self._keyword_weight = float(keyword_weight)
        self._primary_variant_weight = float(primary_variant_weight)
        self._llm = llm
        self._graph_index = graph_index
        self._ranker = SearchRanker(strategy="reciprocal_rank_fusion")
        self._degradations: list[dict[str, Any]] = []
        self._last_plan: dict[str, Any] = {}
        self._leg_ranks: dict[str, dict[str, int]] = {}
        self._active_parsed: ParsedQuery | None = None
        self._last_rule_hints: list[Any] = []

    # ------------------------------------------------------------------ 公开 API

    @property
    def fusion_k(self) -> int:
        """当前 RRF 常数（report/CLI 用）。"""
        return self._fusion_k

    @property
    def weights(self) -> dict[str, float]:
        """当前腿权重。"""
        return {LEG_VECTOR: self._vector_weight, LEG_KEYWORD: self._keyword_weight, LEG_GRAPH: 1.0}

    @property
    def degradations(self) -> list[dict[str, Any]]:
        """历次检索的降级记录（缺腿/图腿未接线/LLM 失败/重复 id 等）。"""
        return list(self._degradations)

    @property
    def last_plan(self) -> dict[str, Any]:
        """最近一次检索的执行计划（解析/路由/where/查询变体/腿/降级）。"""
        return dict(self._last_plan)

    @property
    def last_rule_hints(self) -> list[Any]:
        """最近一次带查询计划的检索附带的确定性规则提示（解释性附加，非检索结果）。"""
        return list(self._last_rule_hints)

    @property
    def weighted(self) -> bool:
        """是否处于腿加权模式（``vector_weight``/``keyword_weight`` 有任一偏离 1.0）。"""
        return self._vector_weight != 1.0 or self._keyword_weight != 1.0

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 10,
        where: dict[str, Any] | None = None,
        legs: Sequence[str] | None = None,
    ) -> list[RetrievalHit]:
        """检索入口: 解析查询 → 路由 → 融合。

        Args:
            query: 用户原始问题。
            top_k: 最终返回条数（两腿各 overfetch ``top_k*3``）。
            where: 调用方附加过滤条件，**优先于**查询解析出的 filters。
            legs: 显式指定检索腿; ``None`` 时用路由给出的腿（默认两腿）。

        Returns:
            按融合分降序的 :class:`RetrievalHit`（长度 ≤ ``top_k``）。
        """
        return self.retrieve_parsed(
            parse_query(query, llm=self._llm), top_k=top_k, where=where, legs=legs
        )

    def retrieve_parsed(
        self,
        parsed: ParsedQuery,
        *,
        top_k: int = 10,
        where: dict[str, Any] | None = None,
        legs: Sequence[str] | None = None,
    ) -> list[RetrievalHit]:
        """给定已解析查询的检索（复用 :func:`matrag.retrieve.query.parse_query` 结果）。"""
        if not isinstance(top_k, int) or top_k < 1:
            raise ValueError(f"top_k 必须 >= 1 的整数: {top_k!r}")
        plan = self._build_plan(parsed, where=where, legs=legs, top_k=top_k)
        return self._execute(plan, top_k=top_k)

    def retrieve_many(
        self,
        queries: list[str],
        *,
        top_k: int = 10,
        where: dict[str, Any] | None = None,
        legs: Sequence[str] | None = None,
    ) -> list[RetrievalHit]:
        """多查询变体融合检索: 每个变体各自跑双腿（overfetch 3x），变体之间再用同一 RRF 融合。

        变体先去重保序（_dedupe_variants）；同一个 chunk 被多个变体命中时**只出现
        一次**，``per_leg`` 按腿跨变体累加、``legs_hit`` 取并集、``rrf_score`` 为
        各变体名次分之和（公式见 :meth:`HybridRetriever._fuse`）。

        单个变体空结果**不退化为异常**：记 ``variant_empty`` 降级后继续；所有变体
        都空则返回空列表。

        **各变体权重不等**：index 0 按 ``primary_variant_weight`` 计，其余为 1.0
        （理由见 :data:`PRIMARY_VARIANT_WEIGHT`）。本入口把**调用方给的第一个变体**
        视为主查询——原查询放首位即可拿到该加权；要回到等权融合请显式传
        ``primary_variant_weight=1.0``。

        Args:
            queries: 查询变体列表（至少一个非空；顺序即融合输入顺序）。
            top_k: 最终返回条数（每腿每变体各 overfetch ``top_k*3``）。
            where: 调用方附加过滤条件，与主查询解析出的 filters 合并（调用方优先）。
            legs: 显式指定检索腿；``None`` 时按**主查询**（首个变体）路由。

        Returns:
            按融合分降序的 :class:`RetrievalHit`（长度 ≤ ``top_k``，
            ``plan_meta`` 为 ``None``——本入口不接查询计划）。
        """
        if not isinstance(top_k, int) or top_k < 1:
            raise ValueError(f"top_k 必须 >= 1 的整数: {top_k!r}")
        variants = _dedupe_variants(queries)
        if not variants:
            raise ValueError("queries 不能为空（至少要有一个非空查询变体）")

        # 解析/路由/where 只按主查询做一次；变体只参与"检索什么"，不改过滤条件。
        parsed = parse_query(variants[0], llm=self._llm)
        plan = self._build_plan(parsed, where=where, legs=legs, top_k=top_k)
        plan["queries"] = variants  # 覆盖 LLM 改写：多查询入口的变体由调用方给全
        plan["keyword_queries"] = [self._keyword_query(parsed, q) for q in variants]
        return self._execute(plan, top_k=top_k)

    def retrieve_with_plan(
        self,
        query: str,
        plan: Any = None,
        *,
        top_k: int = 10,
        where: dict[str, Any] | None = None,
        legs: Sequence[str] | None = None,
    ) -> list[RetrievalHit]:
        """带查询计划（W3-A ``understand.QueryPlan``）的检索。

        - ``plan=None`` → 逐位等价 :meth:`retrieve`（零回归，保证旧调用方不受影响）；
        - ``plan`` 存在 → 取 ``plan.retrieval_queries`` 作为变体（字段缺失则退回
          ``[query]`` 并记 ``plan_fallback``），把 ``plan.filters`` 合并进 where
          （**调用方 where 仍优先**，同 :meth:`retrieve` 语义），plan 摘要挂到
          每条 hit 的 ``plan_meta``，``plan.degradations`` 追加到
          :attr:`HybridRetriever.degradations`（带 ``source="plan"`` 标记）；
        - ``plan.needs_rules=True`` → 自动选规则提示挂到 :attr:`last_rule_hints`
          （解释性附加，**不参与排序**）；规则源不可用只留痕不崩。

        Args:
            query: 用户原始问题。
            plan: 查询计划对象（鸭子类型）或 ``None``。
            top_k / where / legs: 与 :meth:`retrieve` 同语义。

        Returns:
            按融合分降序的 :class:`RetrievalHit`（长度 ≤ ``top_k``）。
        """
        if not isinstance(top_k, int) or top_k < 1:
            raise ValueError(f"top_k 必须 >= 1 的整数: {top_k!r}")

        self._last_rule_hints = []
        if plan is None:
            # 零回归：无计划等价旧路径（含路由决定腿、LLM 改写等全部语义）
            return self.retrieve(query, top_k=top_k, where=where, legs=legs)

        raw_queries = _plan_get(plan, "retrieval_queries", None)
        usable = isinstance(raw_queries, Sequence) and not isinstance(raw_queries, str)
        variants = _dedupe_variants(raw_queries) if usable else []
        if not variants:
            variants = [query]
            self._note({
                "kind": "plan_fallback",
                "detail": "查询计划未给出可用 retrieval_queries（缺字段/为空/非序列），"
                          "退回原查询单变体",
                "plan_type": type(plan).__name__,
            })

        parsed = parse_query(query, llm=self._llm)
        # where 优先级: 查询解析 < 计划 filters < 调用方（_build_plan 内先铺解析结果）
        plan_where = self._plan_filters_to_where(parsed, plan)
        caller_where = dict(where) if where else None
        if plan_where and caller_where:
            for key in set(plan_where) & set(caller_where):
                if plan_where[key] != caller_where[key]:
                    self._note({
                        "kind": "where_key_conflict",
                        "key": key,
                        "kept": caller_where[key],
                        "dropped": plan_where[key],
                        "source": "plan",
                        "detail": "调用方 where 与查询计划条件冲突，调用方优先",
                    })
        exec_plan = self._build_plan(
            parsed, where=self._merge_where(plan_where, caller_where), legs=legs, top_k=top_k
        )
        exec_plan["queries"] = variants
        exec_plan["keyword_queries"] = [self._keyword_query(parsed, q) for q in variants]
        plan_meta = self._plan_summary(plan, variants)

        for note in _plan_get(plan, "degradations", None) or []:
            entry = dict(note) if isinstance(note, Mapping) else {"detail": str(note)}
            entry["source"] = "plan"
            self._note(entry)

        if bool(_plan_get(plan, "needs_rules", False)):
            self._last_rule_hints = self._rule_hints(query)

        hits = self._execute(exec_plan, top_k=top_k, plan_meta=plan_meta)
        return hits

    def retrieve_understood(
        self,
        query: str,
        *,
        top_k: int = 10,
        where: dict[str, Any] | None = None,
        legs: Sequence[str] | None = None,
    ) -> list[RetrievalHit]:
        """先让 understand（W3-A）出查询计划再检索；不可用则降级为单变体检索。

        understand 惰性导入失败（``ImportError``）或缺计划构造入口时，
        ``degradations`` 记 ``understand_unavailable``，功能降级为
        :meth:`retrieve_many` 的 ``[query]`` 单变体（行为与 :meth:`retrieve` 一致）。

        Returns:
            与 :meth:`retrieve_with_plan` 同形的 :class:`RetrievalHit` 列表。
        """
        plan, notes = build_query_plan(query, llm=self._llm)
        for note in notes:
            self._note(note)
        if plan is None:
            return self.retrieve_many([query], top_k=top_k, where=where, legs=legs)
        return self.retrieve_with_plan(query, plan, top_k=top_k, where=where, legs=legs)

    # ------------------------------------------------------------------ 内部

    def _variant_weights(self, variant_count: int) -> list[float]:
        """各查询变体在 RRF 里的权重（index 0 必为原查询）。

        **单变体时恒为 ``[1.0]``**：没有可比对象时给原查询加权只会平移全部
        分数、不改变相对次序，却会让 ``rrf_score`` 与 ``sum(per_leg)`` 的
        绝对值偏离手算口径。单变体保持零回归是多查询入口的既有契约。

        Args:
            variant_count: 去重后的变体条数（≥ 1）。

        Returns:
            长度与 ``variant_count`` 相同、首项为 ``primary_variant_weight``
            的权重列表。
        """
        if variant_count <= 0:
            return []
        if variant_count == 1:
            return [1.0]
        return [self._primary_variant_weight] + [1.0] * (variant_count - 1)

    def _execute(
        self,
        plan: dict[str, Any],
        *,
        top_k: int,
        plan_meta: dict[str, Any] | None = None,
    ) -> list[RetrievalHit]:
        """按计划执行检索: 每 (腿 × 变体) 各取 ``fetch_k`` 条 → 同一 RRF 融合。

        变体与腿是**同构的名次表**: 每个 (腿, 变体) 组合各产出一个名次表交给
        :meth:`_fuse`，因此多查询路径的融合分公式与单查询完全一致，只是求和项
        从"腿"扩展为"腿 × 变体"。单变体时退化为原有语义（回归为零）。

        Args:
            plan: :meth:`_build_plan` 产出的执行计划（``queries`` 已定稿）。
            top_k: 融合后截断条数。
            plan_meta: 挂到每条 hit 的查询计划摘要（无计划为 ``None``）。

        Returns:
            按融合分降序的 :class:`RetrievalHit`（长度 ≤ ``top_k``）。
        """
        self._active_parsed = plan["parsed"]  # 关键词腿据此剥离 where 已承载的 token
        requested: tuple[str, ...] = plan["requested_legs"]
        active: tuple[str, ...] = plan["active_legs"]
        where_effective: dict[str, Any] | None = plan["where"]
        queries: list[str] = plan["queries"]
        fetch_k: int = plan["fetch_k"]
        multi_variant = len(queries) > 1

        payload: dict[str, _Payload] = {}
        leg_ranks: dict[str, dict[str, int]] = {}
        leg_scores: dict[str, dict[str, float]] = {}
        self._leg_ranks = leg_ranks  # explain 读的是本次调用的名次表
        variant_weights = self._variant_weights(len(queries))
        # (腿, 该名次表权重, 名次条目) —— 喂给 SearchRanker 的 RRF 输入
        rrf_input: list[tuple[str, float, list[dict[str, str]]]] = []
        variant_hits: dict[str, int] = {}

        for leg in requested:
            if leg not in active:
                continue  # 缺索引/未接线的腿: 已在 plan["degradations"] 留痕
            for v_index, variant in enumerate(queries):
                # 腿权重 × 变体权重 = 该名次表在 RRF 里的贡献系数
                list_weight = self.weights[leg] * variant_weights[v_index]
                results = self._search_leg(leg, variant, fetch_k, where_effective)
                entries: list[dict[str, str]] = []
                seen_in_list: set[str] = set()
                for rank, chunk in enumerate(results, start=1):
                    item = _payload_of(chunk)
                    previous = payload.get(item.chunk_id)
                    if previous is None:
                        payload[item.chunk_id] = item
                    elif (previous.doc_id, previous.text) != (item.doc_id, item.text):
                        self._note({
                            "kind": "payload_mismatch",
                            "leg": leg,
                            "chunk_id": item.chunk_id,
                            "detail": "同 chunk 在两腿载荷不一致，取先见者",
                        })
                    if item.chunk_id in seen_in_list:
                        self._note({
                            "kind": "duplicate_chunk_in_leg",
                            "leg": leg,
                            "chunk_id": item.chunk_id,
                            "detail": "同一腿同一变体内 chunk 重复，保留最优先次",
                        })
                        continue
                    seen_in_list.add(item.chunk_id)
                    ranks = leg_ranks.setdefault(leg, {})
                    previous_rank = ranks.get(item.chunk_id)
                    # explain 报告"该腿最佳名次"（跨变体取最小 rank）
                    ranks[item.chunk_id] = rank if previous_rank is None else min(previous_rank, rank)
                    # 同一腿跨变体累加名次分；多查询时按变体权重计入。
                    # 单变体时 _variant_weights 恒为 [1.0] → 与旧公式逐位相同。
                    scores = leg_scores.setdefault(leg, {})
                    scores[item.chunk_id] = scores.get(item.chunk_id, 0.0) + (
                        list_weight / (self._fusion_k + rank)
                    )
                    entries.append({"id": item.chunk_id, "leg": leg})
                if entries:
                    rrf_input.append((leg, list_weight, entries))
                    variant_hits[variant] = variant_hits.get(variant, 0) + len(entries)

        if multi_variant and active:
            # 单变体空结果不退化为异常：只留痕（全部变体皆空时返回空列表）
            for variant in queries:
                if not variant_hits.get(variant):
                    self._note({
                        "kind": "variant_empty",
                        "query": variant,
                        "detail": "该查询变体在所有可用检索腿下均无命中（其余变体照常融合）",
                    })

        fused = self._fuse(rrf_input) if rrf_input else []
        plan["result_count"] = len(fused)
        self._last_plan = plan

        hits: list[RetrievalHit] = []
        for chunk_id, score in fused[:top_k]:
            # leg_scores 按 (腿 × 变体权重) 累加, 其和恒等于 rrf_score:
            # 单变体时权重为 w_leg, 与经典 RRF 口径一致。
            per_leg = {leg: leg_scores.get(leg, {}).get(chunk_id) for leg in requested}
            legs_hit = tuple(leg for leg, value in per_leg.items() if value is not None)
            item = payload[chunk_id]
            hits.append(
                RetrievalHit(
                    chunk_id=chunk_id,
                    doc_id=item.doc_id,
                    text=item.text,
                    meta=item.meta,
                    rrf_score=score,
                    per_leg=per_leg,
                    legs_hit=legs_hit,
                    plan_meta=dict(plan_meta) if plan_meta is not None else None,
                )
            )
        return hits

    def _plan_filters_to_where(self, parsed: ParsedQuery, plan: Any) -> dict[str, Any] | None:
        """把查询计划的 filters 转成 where 键（复用 :func:`build_where` 的键名口径）。

        计划侧的 filters 与 :class:`matrag.retrieve.query.ParsedQuery` 同为**查询侧**
        键名（``section``/``table``/``product_code``…），因此走同一条转换路径，
        不可表达的条件同样以 ``where_key_dropped`` 留痕。
        """
        filters = _plan_get(plan, "filters", None)
        if not isinstance(filters, Mapping) or not filters:
            return None
        notes: list[dict[str, Any]] = []
        merged = ParsedQuery(
            raw=parsed.raw,
            keywords=list(parsed.keywords),
            filters=dict(filters),
            product_code=parsed.product_code,
            provenance=dict(parsed.provenance),
        )
        where = build_where(merged, notes=notes)
        for note in notes:
            self._note(note)
        return where

    def _merge_where(
        self,
        plan_where: dict[str, Any] | None,
        caller_where: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """合并"计划 filters + 调用方 where"（调用方优先）。

        查询解析出的 filters 由 :meth:`_build_plan` 先行铺底（优先级最低），
        因此本函数只需在"计划 < 调用方"之间定序。
        """
        merged: dict[str, Any] = {}
        if plan_where:
            merged.update(plan_where)
        if caller_where:
            merged.update(caller_where)
        return merged or None

    def _plan_summary(self, plan: Any, variants: list[str]) -> dict[str, Any]:
        """抽取查询计划摘要（kind/intent/sub_queries/needs_rules/retrieval_queries）。

        字段缺失只是"计划没写"而非错误——不在 degradations 里留痕（计划本身
        是可选增强，缺字段走 :meth:`retrieve_with_plan` 的 ``plan_fallback`` 路径）。
        """
        sub_queries = _plan_get(plan, "sub_queries", None) or []
        if isinstance(sub_queries, str):
            sub_queries = [sub_queries]
        return {
            "kind": str(_plan_get(plan, "kind", "") or ""),
            "intent": str(_plan_get(plan, "intent", "") or ""),
            "sub_queries": [str(item) for item in sub_queries] if isinstance(sub_queries, Sequence) else [],
            "needs_rules": bool(_plan_get(plan, "needs_rules", False)),
            "retrieval_queries": list(variants),
        }

    def _rule_hints(self, query: str) -> list[Any]:
        """按查询选确定性规则提示（仅解释性附加；规则源不可用只留痕）。"""
        try:
            from matrag.retrieve.rule_hint import select_rule_hints

            return list(select_rule_hints(query, top_n=RULE_HINT_TOP_N))
        except Exception as exc:  # noqa: BLE001 —— 规则提示是解释性附加，失败留痕不影响检索
            self._note({
                "kind": "rule_hint_unavailable",
                "detail": f"{type(exc).__name__}: {exc}",
                "fallback": "不附规则提示，检索结果不变",
            })
            return []

    def explain(self, hits: list[RetrievalHit]) -> dict[str, dict[str, Any]]:
        """给引用方看的结构化解释: 每 hit 的腿来源/名次/锚点摘要。

        Args:
            hits: 同一次检索返回的命中列表。

        Returns:
            ``{chunk_id: {"doc_id", "page", "section_path", "table_id",
            "legs_hit", "per_leg", "rrf_score", "ranks", "fused_rank"}}``。
            锚点（page/section_path/table_id）取自 chunk 的 ``meta``;
            ``ranks`` 取自本实例最近一次检索记录的名次（手工构造的 hit 会是空表）;
            多查询路径下 ``ranks`` 是该腿**跨变体的最佳名次**（``per_leg`` 则是
            各变体名次分之和，两者口径不同，见 :meth:`HybridRetriever._execute`）。
        """
        explained: dict[str, dict[str, Any]] = {}
        for index, hit in enumerate(hits, start=1):
            ranks = {
                leg: chunk_ranks[hit.chunk_id]
                for leg, chunk_ranks in self._leg_ranks.items()
                if hit.chunk_id in chunk_ranks
            }
            explained[hit.chunk_id] = {
                "doc_id": hit.doc_id,
                "page": hit.meta.get("page"),
                "section_path": hit.meta.get("section_path"),
                "table_id": hit.meta.get("table_id"),
                "legs_hit": list(hit.legs_hit),
                "per_leg": dict(hit.per_leg),
                "rrf_score": hit.rrf_score,
                "ranks": ranks,
                "fused_rank": index,
            }
        return explained

    def plan(
        self,
        query: str,
        *,
        top_k: int | None = None,
        where: dict[str, Any] | None = None,
        legs: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """只算执行计划、不真正检索（CLI 排障/报告用）。"""
        return self._build_plan(
            parse_query(query, llm=self._llm), where=where, legs=legs, top_k=top_k
        )

    # ------------------------------------------------------------------ 内部

    def _build_plan(
        self,
        parsed: ParsedQuery,
        *,
        where: dict[str, Any] | None,
        legs: Sequence[str] | None,
        top_k: int | None = None,
    ) -> dict[str, Any]:
        """组装执行计划: 路由 → 腿解析 → where 合并（调用方优先）→ 查询变体。"""
        routed = route(parsed.raw, llm=self._llm)
        requested: tuple[str, ...] = tuple(legs) if legs is not None else tuple(routed.legs)
        unknown = [leg for leg in requested if leg not in KNOWN_LEGS]
        if unknown:
            raise ValueError(f"未知检索腿: {unknown}（合法值: {sorted(KNOWN_LEGS)}）")
        if not requested:
            raise ValueError("legs 不能为空（至少一条检索腿）")

        where_notes: list[dict[str, Any]] = []
        where_parsed = build_where(parsed, notes=where_notes)
        where_merged: dict[str, Any] = {}
        if where_parsed:
            where_merged.update(where_parsed)
        if where:
            where_merged.update(where)  # 调用方 where 优先

        skipped, leg_notes = self._resolve_legs(requested)
        notes = where_notes + leg_notes
        for note in notes:
            self._note(note)  # plan 与实例两条查询路径都能看到降级记录
        queries = self._query_variants(parsed)
        fetch_k = max(top_k, 1) * OVERFETCH if top_k is not None else None

        return {
            "parsed": parsed,
            "routed": routed,
            "requested_legs": requested,
            "active_legs": tuple(leg for leg in requested if leg not in skipped),
            "where": where_merged or None,
            "queries": queries,
            "keyword_queries": [self._keyword_query(parsed, q) for q in queries],
            "top_k": top_k,
            "fetch_k": fetch_k,
            "degradations": notes,
        }

    def _resolve_legs(self, requested: Sequence[str]) -> tuple[list[str], list[dict[str, Any]]]:
        """检查每条腿的索引可用性; 缺位的腿跳过并留痕（不静默）。

        Returns:
            ``(被跳过的腿, 降级说明)``; 说明同时写入
            :attr:`HybridRetriever.degradations` 供查询。
        """
        skipped: list[str] = []
        notes: list[dict[str, Any]] = []
        for leg in requested:
            if leg == LEG_VECTOR and self._vector_index is None:
                detail = "vector_index 未提供"
            elif leg == LEG_KEYWORD and self._keyword_index is None:
                detail = "keyword_index 未提供"
            elif leg == LEG_GRAPH and self._graph_index is None:
                detail = "graph 腿为 P1-5b（知识图谱检索）预留，graph_index 未接线"
            else:
                continue
            skipped.append(leg)
            note = {"kind": "leg_missing_index" if "未提供" in detail else "leg_not_wired",
                    "leg": leg, "detail": detail}
            notes.append(note)  # 由 _build_plan 统一落到 self._note，避免双记
        return skipped, notes

    def _query_variants(self, parsed: ParsedQuery) -> list[str]:
        """检索用查询变体: 原查询 + （可选）LLM 同义改写。

        ``llm=None`` → 只有 ``[parsed.raw]``，绝不触碰 LLM。
        """
        variants: list[str] = [parsed.raw]
        if self._llm is not None and parsed.raw:
            for rewrite in self._llm_rewrite(parsed.raw):
                if rewrite not in variants:
                    variants.append(rewrite)
        return variants

    def _llm_rewrite(self, query: str) -> list[str]:
        """LLM 查询改写: 生成 1~2 个同义查询。失败留痕返回空（改写只是可选增强）。"""
        try:
            data = _llm_json(
                self._llm,
                system=(
                    "你是检索系统的查询改写器。把用户问题改写成 1~2 个同义检索查询"
                    "（保留型号/章节/表号等专有信息），只输出 JSON，不要解释。"
                ),
                user=f"问题: {query}",
                schema_hint='{"queries": ["同义查询"]}',
            )
        except Exception as exc:  # noqa: BLE001 —— 可选增强，失败留痕不改写
            self._note({
                "kind": "llm_rewrite_failed",
                "detail": f"{type(exc).__name__}: {exc}",
                "fallback": "使用原始查询",
            })
            return []

        if not isinstance(data, dict):
            self._note({"kind": "llm_rewrite_invalid", "detail": "LLM 未返回 JSON 对象"})
            return []
        rewrites: list[str] = []
        for item in (data.get("queries") or [])[:MAX_REWRITES]:
            text = str(item).strip()
            if text:
                rewrites.append(text)
        if not rewrites:
            self._note({"kind": "llm_rewrite_empty", "detail": "LLM 未给出可用同义查询"})
        return rewrites

    def _search_leg(
        self, leg: str, query: str, fetch_k: int, where: dict[str, Any] | None
    ) -> list[Any]:
        """单腿检索（各自 overfetch ``fetch_k`` 条）。"""
        if leg == LEG_VECTOR:
            if self._embedder is None:
                raise ValueError("vector 腿需要 embedder（matrag.embeddings.get_embedder）")
            vector = self._embedder.embed(query)
            return list(self._vector_index.search_vector(vector, top_k=fetch_k, where=where))
        if leg == LEG_KEYWORD:
            return list(
                self._keyword_index.search_keyword(
                    self._keyword_query(self._active_parsed, query), top_k=fetch_k, where=where
                )
            )
        if leg == LEG_GRAPH:
            return list(self._graph_index.search_graph(query, top_k=fetch_k, where=where))
        raise ValueError(f"未知检索腿: {leg!r}")

    def _keyword_query(self, parsed: ParsedQuery, query: str) -> str:
        """关键词腿的实际查询文本: 剥掉 where 已承载的 token + 剔除停用词。

        P1-4 的 ``search_keyword`` 对分词结果做 FTS5 **AND** 匹配（每个词都要在
        chunk 正文出现才算命中）。这带来两个实测坑:

        1. 产品编号/表号这类 token 通常不在 chunk 正文里，而型号过滤已由
           ``where=doc_id`` 承载 → 剥掉，去掉重复约束;
        2. 疑问词/虚词（"是多少"/"多少"/"怎么"…）不在规格书正文里 → 按
           :data:`matrag.retrieve.query.STOPWORDS` 剔除。

        两步都只作用于关键词腿: 向量腿仍用完整原查询，语义不损失。
        规范化结果记入 ``plan["keyword_queries"]`` 可查询; 剥完为空则回退原查询。
        """
        text = query
        values: list[str | int] = [parsed.product_code or "", *parsed.filters.values()]
        for value in values:
            if isinstance(value, str) and value:
                text = text.replace(value, " ")
                if TABLE_ID_SHAPE.match(value):
                    text = re.sub(r"表(?=\s|$)", " ", text)
            elif isinstance(value, int):
                # 表号连同前面的"表"字一起剥（"表11" 不是 chunk 正文里的 token）
                text = re.sub(rf"表\s*{value}(?![0-9])", " ", text)
                text = re.sub(rf"(?<![0-9]){value}(?![0-9])", " ", text)
        tokens = [tok for tok in resolve_tokenizer()(text) if tok not in STOPWORDS]
        return " ".join(tokens) or query

    def _fuse(
        self, rrf_input: Sequence[tuple[str, float, Sequence[dict[str, str]]]]
    ) -> list[tuple[str, float]]:
        """RRF 融合。

        入参每项是 ``(腿, 该名次表权重, 名次条目)``; 权重已由调用方算成
        ``w_leg × w_variant``（见 :meth:`_execute`）。全部为 1.0 时一次性喂给
        同一个 semantica ``SearchRanker.reciprocal_rank_fusion``::

            rrf_score(id) = Σ_表 1 / (fusion_k + rank_in(表))

        存在非 1.0 权重时按名次表分别过同一个 ``SearchRanker`` 再乘该表权重求和
        （等权路径的严格推广，别处无第二套实现）::

            rrf_score(id) = Σ_表 w_表 · 1 / (fusion_k + rank_in(表))

        腿加权与多查询变体加权走的是同一条推广路径。单变体时表权重恒为
        ``w_leg × 1.0``，退化为经典 RRF。两种路径的输出都按
        ``(-分数, chunk_id)`` 稳定排序，保证确定性。
        """
        if all(weight == 1.0 for _, weight, _ in rrf_input):
            fused = self._ranker.reciprocal_rank_fusion(
                [list(entries) for _, _, entries in rrf_input], k=self._fusion_k
            )
            ranked = [(str(item["id"]), float(item["score"])) for item in fused]
        else:
            scores: dict[str, float] = {}
            for _leg, weight, entries in rrf_input:
                for item in self._ranker.reciprocal_rank_fusion([list(entries)], k=self._fusion_k):
                    chunk_id = str(item["id"])
                    scores[chunk_id] = scores.get(chunk_id, 0.0) + weight * float(item["score"])
            ranked = list(scores.items())
        ranked.sort(key=lambda pair: (-pair[1], pair[0]))
        return ranked

    def _note(self, entry: dict[str, Any]) -> None:
        """记录一次降级（有界，防止长跑无限增长）。"""
        self._degradations.append(dict(entry))
        if len(self._degradations) > _MAX_DEGRADATIONS:
            del self._degradations[: len(self._degradations) - _MAX_DEGRADATIONS]


def _llm_json(llm: Any, *, system: str, user: str, schema_hint: str) -> Any:
    """LLM JSON 调用（鸭子类型: 兼容带 ``schema_hint`` 与只收两参的替身）。"""
    try:
        return llm.complete_json(system, user, schema_hint=schema_hint)
    except TypeError:  # 替身签名更窄
        return llm.complete_json(system, user)


def merge_legs(*legs: Iterable[str]) -> tuple[str, ...]:
    """合并多组腿名并去重保序（CLI/多路融合辅助）。"""
    ordered: list[str] = []
    for group in legs:
        for leg in group:
            if leg in KNOWN_LEGS and leg not in ordered:
                ordered.append(leg)
    return tuple(ordered) or DEFAULT_LEGS


__all__ = [
    "FUSION_K",
    "KNOWN_LEGS",
    "OVERFETCH",
    "RULE_HINT_TOP_N",
    "HybridRetriever",
    "RetrievalHit",
    "build_query_plan",
    "merge_legs",
]
