"""RAG 应答层（证据合成 + 行级引用 + 推理链）。

把检索层的命中（``matrag.retrieve.retriever.RetrievalHit``）合成为**带行级引用**的
中文答案，并同时输出可机器消费的推理链与降级留痕::

    answer        正文，每个结论句末紧跟 ``[n]`` 行级引用标记
    citations     ``[n]`` → 锚点（doc_id/page/section_path/table_id/chunk_id/text）
    steps         推理链（查询条件 / 检索 / 过滤 / 上下文 / 规则 / 引用统计）
    rule_hints    参与的确定性规则 ``{rule_id, statement, kind, score}``
    evaluations   规则求值（按问题中数值代入公式的确定性计算，不是规格书数据）
    covered       证据是否足以回答
    degradations  全部降级留痕（LLM 不可用 / understand 不可用 / 规则提示不可用 /
                  上下文截断 / 无证据 / LLM 引用越界……）

三条硬约束:

1. **证据不足就明说不足**——无命中、上下文为空、LLM 判定 ``covered=false`` 时一律给出
   "规格书未给出 / 证据不足"的答案，**禁止编造数值**;
2. **规格 = 判据，规则 = 常识，求值 = 计算**——规格书证据引用写 ``[n]``，领域规则引用写规则
   id（如 ``R-001``），规则求值只呈现按问题数值算出的公式结果并标注"非规格书数据"，
   三者在上下文里分区呈现，提示词明确禁止用规则/求值替代规格数值;
3. **LLM 不可用不留空**——走确定性模板（逐条引用原文），``covered=False`` +
   ``degradations`` 记 ``llm_unavailable``; **步骤与引用不依赖 LLM 成功**。

对 W3-A / W3-B / 规则求值服务的**惰性依赖**（尚未落盘时只降级不崩）::

    matrag.retrieve.understand.understand(query, *, llm=None, ...) -> QueryPlan
    matrag.retrieve.rule_hint.select_rule_hints(query, *, top_n=5, ...) -> list[RuleHint]
    matrag.domain.evaluate.evaluate_for_answer(hints, *, query=None, facts=None)
    HybridRetriever.retrieve_with_plan(query, plan=None, ...) / retriever.last_rule_hints

导入失败一律 ``ImportError`` → 记 degradation → 退回"无 plan 的纯检索 + 无规则提示"。
本模块只 import 标准库: 不把检索层/规则层拖进 import 链，gateway 可直接导出
``answer_question``。
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 上下文字符上限（超限截断并留痕 ``context_truncated``）。
MAX_CONTEXT_CHARS = 12000
#: 上下文条数上限（超限截断并留痕 ``context_truncated``）。
MAX_CONTEXT_ITEMS = 12
#: 默认检索条数。
DEFAULT_TOP_K = 8
#: 规则提示默认条数（``select_rule_hints(top_n=...)``）。
DEFAULT_RULE_TOP_N = 5
#: citation 携带的原文片段窗口上限（字符）: gateway ``ask`` 的每条引用自带原文，
#: 调用方（如 matrag-agent ``rag_search``）可直接取用而无需再回查 chunk store;
#: 超限时按关键词居中的窗口截取，**绝不臆造内容**。
CITATION_TEXT_MAX_CHARS = 500

#: 正文里的行级引用标记（``[1]``）。
MARKER_PATTERN = re.compile(r"\[(\d{1,3})\]")
#: :func:`build_context` 产出的证据块首行（``[1] 文档: xxx | 页: 3 | 章节: ...``）。
_BLOCK_HEADER_PATTERN = re.compile(r"^\[(\d{1,3})\] 文档: ")
#: 证据不足时的标准措辞（LLM 与确定性模板共用，便于测试断言）。
INSUFFICIENT_HINT = "规格书未给出"
#: 上下文里领域规则分区的小标题（与规格证据明确区分）。
RULE_SECTION_HEADER = "# 领域规则参考（常识性判据，不是产品规格书内容，不可作为规格数值来源）"
#: 规则求值分区的小标题（按问题中数值代入公式的确定性计算，同样不是规格书数据）。
EVAL_SECTION_HEADER = (
    "# 规则求值（按问题中给出的数值代入领域公式的确定性计算，"
    "不是产品规格书内容，不可作为规格数值来源）"
)

#: where 键名（与 ``matrag.retrieve.routing`` 的 ``FILTER_SECTION_CONTAINS`` 同一索引
#: 契约；这里只抄字符串常量，避免应答层为一个键名把整个检索层拉进 import 链）。
_KEY_SECTION_CONTAINS = "section_contains"


# --------------------------------------------------------------------------- #
# 契约
# --------------------------------------------------------------------------- #
@dataclass
class Citation:
    """一条行级引用: 正文里的 ``[n]`` → 可回溯的原文锚点。

    ``table_id`` 为 ``None`` 表示非表格证据; ``score`` 是 RRF 融合分。
    ``text`` 是命中 chunk 的原文片段（≤ :data:`CITATION_TEXT_MAX_CHARS` 字符，
    尽量让关键词居中），供 gateway ``ask`` 的调用方直接展示/检索，无需再回查
    chunk store; 旧消费者可忽略该键（向后兼容）。
    """

    marker: str
    doc_id: str
    page: int
    section_path: str
    table_id: str | None
    chunk_id: str
    score: float
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict（gateway 工具返回用）。"""
        return {
            "marker": self.marker,
            "doc_id": self.doc_id,
            "page": self.page,
            "section_path": self.section_path,
            "table_id": self.table_id,
            "chunk_id": self.chunk_id,
            "score": self.score,
            "text": self.text,
        }


@dataclass
class AnswerResult:
    """一次应答的完整结果。

    - ``citations`` 含**全部**装入上下文的证据（未被答案引用的也保留，供检索兜底展示）;
    - ``evaluations`` 是规则提示的求值结果（按问题中数值代入公式计算，非规格书数据）;
    - ``usage`` 恒为 ``None``: ``LLMClient.complete_json`` 不返回 token 用量，
      本层不臆造统计（后续接入用量统计时在此字段补齐）;
    - ``plan`` / ``hits`` 是查询理解计划与检索命中的**原始引用**（审计 / 诊断用），
      不参与 :meth:`to_dict`（对外七键契约保持不变）。
    """

    answer: str
    citations: list[Citation] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    rule_hints: list[dict] = field(default_factory=list)
    evaluations: list[dict] = field(default_factory=list)
    covered: bool = False
    degradations: list[dict] = field(default_factory=list)
    usage: dict | None = None
    plan: Any = None
    hits: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict（gateway ``ask`` 工具返回用）。"""
        return {
            "answer": self.answer,
            "citations": [c.to_dict() for c in self.citations],
            "steps": list(self.steps),
            "rule_hints": [dict(hint) for hint in self.rule_hints],
            "evaluations": [dict(item) for item in self.evaluations],
            "covered": self.covered,
            "degradations": [dict(note) for note in self.degradations],
        }


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """\
你是电子产品产测领域的技术规格书检索问答专家。你只能依据"规格书证据"作答，
并显式区分"规格书原文"与"领域规则参考"。

# 硬性要求（逐条遵守）
1. 每个结论都必须带**行级引用标记** `[n]`（n 是证据编号，范围 1..N），紧跟在该结论句末；
   没有标记的句子视为没有依据，必须删掉。
2. 证据不足以回答时：把 `covered` 置为 false，并在 `answer` 里明确写出"规格书未给出"
   或"证据不足"，**禁止推测、补全、换算或编造任何数值/单位/条件**。
3. 表格数值**原样引用**（含单位、小数位、正负号、范围符号），不得四舍五入、不得省略单位、
   不得跨行拼接表格单元格的半截内容。
4. **不得引入上下文之外的事实**；上下文里没有的一律按第 2 条处理。
5. "领域规则参考"是常识性判据，**不是规格数值来源**：引用规则时写规则 id（如 `R-001`），
   不要给它加 `[n]` 标记，也不要用规则去顶替规格书的数值。
6. 用中文作答，简洁分点；不复述整段原文，只引用支撑结论的数值与条件。
"""

SCHEMA_HINT = """{
  "answer": "中文正文；每个结论句末带 [n] 行级引用；证据不足时明说“规格书未给出/证据不足”",
  "covered": true,
  "steps": ["自然语言解读：为什么这些证据能回答问题"]
}"""


def build_user_prompt(
    query: str,
    *,
    context: str,
    citations: Sequence[Citation],
    plan: Any = None,
    rule_hints: Sequence[Mapping[str, Any]] | None = None,
    evaluations: Sequence[Mapping[str, Any]] | None = None,
    covered_hint: bool = True,
) -> str:
    """组装喂给 LLM 的 user 消息（问题 + 查询理解 + 证据 + 规则/求值 + 输出约束）。

    Args:
        query: 用户原始问题。
        context: :func:`build_context` 产出的证据上下文（已带 ``[n]`` 标记）。
        citations: 与 ``context`` 一一对应的引用锚点。
        plan: 可选查询理解计划（只作为"系统怎么理解这个问题"的提示，**不是证据**）。
        rule_hints: 可选领域规则（常识参考）。
        evaluations: 可选规则求值结果（代入问题中数值的公式计算值，**不是规格书数据**）。
        covered_hint: 调用方对"证据是否足够"的先验; ``False`` 时要求模型直接判不足。

    Returns:
        完整的 user 消息文本。
    """
    plan_text = _plan_block(plan)
    rule_text = _rule_block(rule_hints or [])
    eval_text = _evaluation_block(evaluations or [])
    parts = [
        f"# 用户问题\n{query}",
        f"# 检索系统对问题的理解（仅供参考，不是规格书证据）\n{plan_text}",
        f"# 规格书证据（共 {len(citations)} 条，标记 [n] 与下面编号一一对应）\n{context}",
        rule_text,
        eval_text,
        (
            "# 输出要求\n"
            "- 只输出 JSON，键为 answer / covered / steps。\n"
            f"- 证据不足时 covered=false，并在 answer 里写明“{INSUFFICIENT_HINT}/证据不足”，"
            "严禁编造数值。\n"
            "- answer 里出现的每个 [n] 都必须在上方证据编号范围内。\n"
            + (
                "- 「规则求值」是代入问题中数值的公式计算结果（不是规格书数据）：可引用其数值，"
                "但要写明是公式计算值。\n"
                if eval_text
                else ""
            )
            + (
                "- 调用方已判定证据不足（covered_hint=False）：请直接说明缺什么，不要给确定性结论。\n"
                if not covered_hint
                else ""
            )
        ),
    ]
    return "\n\n".join(part for part in parts if part)


# --------------------------------------------------------------------------- #
# 上下文打包
# --------------------------------------------------------------------------- #
def build_context(
    hits: Sequence[Any],
    *,
    max_chars: int = MAX_CONTEXT_CHARS,
    max_items: int = MAX_CONTEXT_ITEMS,
    notes: list[dict[str, Any]] | None = None,
    query: str = "",
) -> tuple[str, list[Citation]]:
    """把检索命中打包成带 ``[n]`` 标记的证据上下文（**原文一字不改**）。

    规则:

    - 按 ``rrf_score`` 降序取（并列时按 ``chunk_id`` 稳定排序，保证可复现）;
    - 按 ``(doc_id, page, table_id, chunk_id)`` 去重，重复的只留分数最高的一条;
    - 每条形如 ``[1] 文档: X p3 A > B | 表: t0-6`` + 换行 + **原文**;
      表格行的管道符原样保留（不重排、不改写、不补单位）;
    - 条数超 ``max_items`` 或累计字符超 ``max_chars`` 即停止追加，**截断必须留痕**
      （``notes`` 非 ``None`` 时原址追加 ``context_truncated`` / ``context_empty`` /
      ``duplicate_evidence_merged``）; 单条证据就超 ``max_chars`` 时不装入任何上下文
      （宁可留空也不截断原文）。

    Args:
        hits: 命中列表（``RetrievalHit`` 或同形状对象/dict，鸭子类型）。
        max_chars: 上下文字符上限。
        max_items: 上下文条数上限。
        notes: 可选的降级记录列表（原址追加）。``None`` 表示调用方主动放弃留痕。
        query: 用户问题; 用于把每条引用的 ``text`` 片段窗口居中到关键词
            （空串则取中段窗口）, 不影响上下文正文与排序。

    Returns:
        ``(上下文文本, 引用列表)``; 引用列表顺序即 ``[n]`` 的编号顺序。
    """
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
        raise ValueError(f"max_items 须为 >= 1 的整数: {max_items!r}")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError(f"max_chars 须为 >= 1 的整数: {max_chars!r}")

    ordered = sorted(
        list(hits),
        key=lambda hit: (-_as_float(_field(hit, "rrf_score")), str(_field(hit, "chunk_id") or "")),
    )
    citations: list[Citation] = []
    blocks: list[str] = []
    seen: set[tuple[str, int, str | None, str]] = set()
    used_chars = 0
    dropped_duplicate = 0
    dropped_items = 0
    dropped_chars = 0
    stopped_at = len(ordered)

    for index, hit in enumerate(ordered):
        if len(citations) >= max_items:
            dropped_items += 1
            continue
        citation = _citation_of(hit, query=query)
        key = (citation.doc_id, citation.page, citation.table_id, citation.chunk_id)
        if key in seen:
            dropped_duplicate += 1
            continue
        citation.marker = f"[{len(citations) + 1}]"
        block = _render_block(citation, str(_field(hit, "text") or ""))
        extra = len(block) + (1 if blocks else 0)  # 块之间以空行分隔
        if used_chars + extra > max_chars:
            stopped_at = index
            break
        seen.add(key)
        citations.append(citation)
        blocks.append(block)
        used_chars += extra

    dropped_chars = len(ordered) - stopped_at
    if dropped_duplicate:
        _note(
            notes,
            "duplicate_evidence_merged",
            detail="同 (doc_id, page, table_id, chunk_id) 的重复证据只保留分数最高的一条",
            count=dropped_duplicate,
        )
    if dropped_items:
        _note(
            notes,
            "context_truncated",
            reason="max_items",
            detail=f"证据条数超过上限 {max_items}，超出部分未装入上下文",
            dropped_items=dropped_items,
            kept_items=len(citations),
        )
    if dropped_chars:
        _note(
            notes,
            "context_truncated",
            reason="max_chars",
            detail=f"上下文超过 {max_chars} 字符上限，超出部分未装入（原文不截断，宁缺毋滥）",
            dropped_items=dropped_chars,
            kept_items=len(citations),
        )
    if ordered and not citations:
        _note(
            notes,
            "context_empty",
            detail=f"单条证据即超过 {max_chars} 字符上限，未装入任何上下文（原文不截断）",
        )
    return "\n\n".join(blocks), citations


def _citation_of(hit: Any, *, query: str = "") -> Citation:
    """把一条命中转成引用锚点（缺失字段不猜：页码 0、章节空串）。

    ``text`` 取命中 chunk 的原文片段（≤ :data:`CITATION_TEXT_MAX_CHARS` 字符，
    尽量把 ``query`` 关键词居中）；缺失时为空串（旧消费者可忽略）。
    """
    meta = _field(hit, "meta") or {}
    if not isinstance(meta, Mapping):
        meta = {}
    table_raw = meta.get("table_id")
    return Citation(
        marker="",
        doc_id=str(_field(hit, "doc_id") or ""),
        page=_as_int(meta.get("page")),
        section_path=str(meta.get("section_path") or ""),
        table_id=str(table_raw) if table_raw else None,
        chunk_id=str(_field(hit, "chunk_id") or ""),
        score=_as_float(_field(hit, "rrf_score")),
        text=_clip_text(str(_field(hit, "text") or ""), query=query),
    )


def _clip_text(text: str, *, query: str = "", limit: int = CITATION_TEXT_MAX_CHARS) -> str:
    """把 chunk 原文裁成 ≤ ``limit`` 字符的窗口（尽量让关键词居中）。

    - 原文不超限 → 原样返回（一字不改）;
    - 超限 → 以 ``query`` token 在原文里的首次出现为中心取窗口; 定位不到关键词
      时取文本中段。截断端以省略号标记，正文**只截取真实原文**，绝不臆造内容。

    Args:
        text: chunk 原文（命中 ``RetrievalHit.text``）。
        query: 用户问题（用于把关键词居中; 空串则取中段）。
        limit: 窗口字符上限（含截断省略号）。

    Returns:
        长度 ≤ ``limit`` 的原文片段; ``text`` 为空时原样返回。
    """
    if not text or len(text) <= limit:
        return text
    window_len = max(1, limit - 2)  # 两端最多各一个省略号，确保总长 ≤ limit
    anchor = _anchor_index(text, query)
    start = max(0, min(anchor - window_len // 2, len(text) - window_len))
    window = text[start : start + window_len]
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + window_len < len(text) else ""
    return f"{prefix}{window}{suffix}"


def _anchor_index(text: str, query: str) -> int:
    """关键词在原文里的中心位置（定位不到返回文本中点）。"""
    for token in _query_tokens(query):
        index = text.find(token)
        if index >= 0:
            return index + len(token) // 2
    return len(text) // 2


def _query_tokens(query: str) -> list[str]:
    """从查询串里取用于定位的 token（按长度降序，长 token 优先命中）。"""
    tokens = [tok for tok in re.split(r"[\s,，。、；;:：/\\|（）()\[\]【】]+", query) if tok]
    return sorted(tokens, key=len, reverse=True)


def _render_block(citation: Citation, text: str) -> str:
    """渲染一条证据块: 锚点行（``doc_id p{page} {section_path}`` + 可选表号）+ **原文**。

    原文一字不改：表格行的管道符原样保留（不重排/不换算/不补单位）。
    """
    section = citation.section_path or "（无章节信息）"
    head = f"{citation.marker} 文档: {citation.doc_id} p{citation.page} {section}"
    if citation.table_id:
        head += f" | 表: {citation.table_id}"
    return f"{head}\n{text}"


def _context_bodies(context: str) -> dict[str, str]:
    """从上下文里按 ``[n]`` 取回每条证据的正文（确定性模板逐条引用原文用）。

    块的首行形如 ``[1] 文档: ...``，其后所有行是该块的正文（正文里的空行原样保留）。
    """
    bodies: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in (context or "").splitlines():
        match = _BLOCK_HEADER_PATTERN.match(line)
        if match:
            if current is not None:
                bodies[current] = "\n".join(buf).strip("\n")
            current = f"[{match.group(1)}]"
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        bodies[current] = "\n".join(buf).strip("\n")
    return bodies


# --------------------------------------------------------------------------- #
# 合成
# --------------------------------------------------------------------------- #
def synthesize(
    query: str,
    context: str,
    citations: list[Citation],
    *,
    plan: Any = None,
    rule_hints: Sequence[Any] | None = None,
    evaluations: Sequence[Any] | None = None,
    llm: Any = None,
    covered_hint: bool = True,
) -> AnswerResult:
    """把已打包的上下文合成为带引用的答案（不碰检索层）。

    Args:
        query: 用户原始问题。
        context: :func:`build_context` 产出的上下文。
        citations: 与 ``context`` 一一对应的引用锚点。
        plan: 可选查询理解计划（只用于生成步骤与提示，不作为证据）。
        rule_hints: 可选领域规则（``RuleHint`` / dict 均可，鸭子类型）。
        evaluations: 可选规则求值结果（``EvaluationResult`` / dict 均可，鸭子类型）;
            与规则一样只作常识分区，**不作为规格证据**。
        llm: 可选 LLM 门面; ``None`` 或未配置 → 确定性模板 + ``llm_unavailable`` 留痕。
        covered_hint: 调用方对"证据是否足够"的先验; ``False`` 时即使 LLM 说够也强制
            ``covered=False``（并留痕）。

    Returns:
        :class:`AnswerResult`。**引用完整性**在返回前已被强制成立: ``answer`` 里出现的
        每个 ``[n]`` 必然存在于 ``citations``（越界标记被剔除并留痕）。
    """
    hints = _normalize_rule_hints(rule_hints)
    evals = _normalize_evaluations(evaluations)
    cites = list(citations)
    degradations: list[dict[str, Any]] = []
    llm_steps: list[str] = []

    if not cites:
        _note(degradations, "no_evidence", detail="上下文无任何证据，跳过大模型合成（拒绝无据作答）")
        reason = f"检索无证据，{INSUFFICIENT_HINT}"
        answer = _deterministic_answer(
            query, cites, hints, evaluations=evals, reason=reason, context=context,
            degradations=degradations,
        )
        covered = False
    else:
        reason = _llm_unavailable_reason(llm)
        if reason is not None:
            _note(degradations, "llm_unavailable", detail=reason)
            answer = _deterministic_answer(
                query, cites, hints, evaluations=evals, reason=reason, context=context,
                degradations=degradations,
            )
            covered = False
        else:
            answer, covered, llm_steps = _llm_answer(
                llm,
                query=query,
                context=context,
                citations=cites,
                plan=plan,
                hints=hints,
                evaluations=evals,
                covered_hint=covered_hint,
                degradations=degradations,
            )

    steps: list[str] = [_evidence_step(cites, len(context))]
    if hints:
        steps.append(_rule_step(hints))
    if evals:
        steps.append(_evaluation_step(evals))
    steps.append(_usage_step(answer, cites))
    if not covered:
        steps.append(f"判定：{INSUFFICIENT_HINT}（证据不足，答案未给出确定性结论）")
    steps.extend(llm_steps)
    return AnswerResult(
        answer=answer,
        citations=cites,
        steps=steps,
        rule_hints=hints,
        evaluations=evals,
        covered=covered,
        degradations=degradations,
    )


def _llm_answer(
    llm: Any,
    *,
    query: str,
    context: str,
    citations: list[Citation],
    plan: Any,
    hints: list[dict[str, Any]],
    evaluations: list[dict[str, Any]],
    covered_hint: bool,
    degradations: list[dict[str, Any]],
) -> tuple[str, bool, list[str]]:
    """走 LLM 合成; 任一环节失败即退回确定性模板并留痕（绝不返回空答案）。"""
    user = build_user_prompt(
        query,
        context=context,
        citations=citations,
        plan=plan,
        rule_hints=hints,
        evaluations=evaluations,
        covered_hint=covered_hint,
    )
    reason = "大模型合成失败"
    try:
        data = _complete_json(llm, system=SYSTEM_PROMPT, user=user, schema_hint=SCHEMA_HINT)
    except Exception as exc:  # noqa: BLE001 —— 可选增强，失败留痕退回模板，不静默
        _note(degradations, "llm_failed", detail=f"{type(exc).__name__}: {exc}")
        return (
            _deterministic_answer(query, citations, hints, evaluations=evaluations,
                                  reason=reason, context=context, degradations=degradations),
            False,
            [],
        )

    if not isinstance(data, Mapping):
        _note(degradations, "llm_invalid_output", detail="大模型未返回 JSON 对象")
        return (
            _deterministic_answer(query, citations, hints, evaluations=evaluations,
                                  reason=reason, context=context, degradations=degradations),
            False,
            [],
        )

    raw_answer = data.get("answer")
    if not isinstance(raw_answer, str) or not raw_answer.strip():
        _note(degradations, "llm_answer_invalid", detail="大模型未给出非空 answer 字段")
        return (
            _deterministic_answer(query, citations, hints, evaluations=evaluations,
                                  reason="大模型未给出答案正文",
                                  context=context, degradations=degradations),
            False,
            [],
        )

    text, used = _enforce_citation_markers(raw_answer.strip(), citations, degradations)

    llm_covered = _as_bool(data.get("covered"))
    covered = bool(llm_covered and covered_hint)
    if llm_covered and not covered_hint:
        _note(degradations, "covered_forced_false", detail="调用方给定 covered_hint=False，强制判证据不足")
    if covered and not used:
        covered = False
        _note(degradations, "covered_without_citation",
              detail="大模型称证据充分，但正文没有任何 [n] 引用")

    return text, covered, _parse_llm_steps(data.get("steps"), degradations)


def _enforce_citation_markers(
    text: str, citations: Sequence[Citation], degradations: list[dict[str, Any]]
) -> tuple[str, list[str]]:
    """强制引用完整性: 剔除上下文之外的 ``[n]`` 标记（留痕），返回 (正文, 实际引用的标记)。"""
    known = {c.marker for c in citations}
    found = MARKER_PATTERN.findall(text)
    unknown = sorted(
        {f"[{num}]" for num in found if f"[{num}]" not in known},
        key=lambda marker: int(marker[1:-1]),
    )
    if unknown:
        _note(degradations, "llm_citation_out_of_range",
              detail="剔除上下文之外的引用标记（模型幻觉编号）", markers=unknown)
        text = MARKER_PATTERN.sub(lambda m: "" if m.group(0) not in known else m.group(0), text)
    used = sorted({f"[{num}]" for num in MARKER_PATTERN.findall(text)}, key=lambda m: int(m[1:-1]))
    return text.strip(), used


def _parse_llm_steps(raw: Any, degradations: list[dict[str, Any]]) -> list[str]:
    """LLM 的 steps 只作自然语言解读段收进推理链（形状不对则留痕丢弃）。"""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        _note(degradations, "llm_steps_invalid", detail="steps 不是字符串列表，已忽略")
        return []
    return [f"解读：{item.strip()}" for item in raw if isinstance(item, str) and item.strip()]


def _deterministic_answer(
    query: str,
    citations: Sequence[Citation],
    hints: Sequence[Mapping[str, Any]],
    *,
    evaluations: Sequence[Mapping[str, Any]] = (),
    reason: str,
    context: str = "",
    degradations: list[dict[str, Any]] | None = None,
) -> str:
    """LLM 不可用/失败/无证据时的确定性答案（逐条引用原文，绝不假装回答）。"""
    bodies = _context_bodies(context) if context else {}
    lines: list[str] = []
    if not citations:
        lines.append(f"关于「{query}」：{reason}。")
        lines.append(f"{INSUFFICIENT_HINT}：未检索到相关条文，证据不足，无法作答。")
    else:
        lines.append(f"关于「{query}」：{reason}，以下只逐条列出检索到的原文证据，不做推断。")
        lines.append("")
        lines.append("检索到以下相关条文：")
        for citation in citations:
            body = bodies.get(citation.marker)
            if body is None:
                if degradations is not None:
                    _note(degradations, "context_body_missing",
                          detail=f"上下文里找不到 {citation.marker} 的正文，只列锚点",
                          marker=citation.marker)
                body = "（上下文未提供该条正文）"
            lines.append(f"{citation.marker} {_anchor_text(citation)}：{body}")
    if hints:
        lines.append("")
        lines.append("可参考的领域规则（常识判据，不是规格数值来源）：")
        lines.extend(_rule_lines(hints))
    if evaluations:
        lines.append("")
        lines.append("规则求值（按问题中数值代入公式计算，非规格书数据）：")
        lines.extend(_evaluation_lines(evaluations))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 端到端
# --------------------------------------------------------------------------- #
def answer_question(
    query: str,
    *,
    retriever: Any = None,
    plan: Any = None,
    llm: Any = None,
    top_k: int = DEFAULT_TOP_K,
    where: Mapping[str, Any] | None = None,
    max_chars: int = MAX_CONTEXT_CHARS,
    facts: Mapping[str, Any] | None = None,
    derive_mode: str = "deterministic",
) -> AnswerResult:
    """端到端应答: 查询理解 → 混合检索 → 上下文打包 → 合成。

    Args:
        query: 用户问题（必须非空）。
        retriever: 检索器（``HybridRetriever`` 或同形状替身）。**必填**: 没有检索器
            就无法应答，直接 ``ValueError``（对外错误形态由 gateway ``ask`` 转成
            ``retriever_unconfigured``）。
        plan: 可选查询理解计划（``matrag.retrieve.understand.QueryPlan``）。``None`` 时
            本函数自行调用 ``understand``；模块不存在 → 记 ``understand_unavailable``
            并走"原查询直检索"的降级路径。
        llm: LLM 门面（查询理解与答案合成共用同一个）。
        top_k: 检索条数。
        where: 调用方附加过滤（与查询解析出的条件合并时以本参数优先）。
        max_chars: 上下文字符上限。
        facts: 实测事实（键 = 规则输入参数名，值 = 数值/布尔/字符串）。``None`` 时
            从问题文本自动抽取「数字 + 单位」代入提示规则求值; 显式给出时优先使用,
            空映射 ``{}`` 显式关闭求值。
        derive_mode: 多跳推导的编排策略: ``"deterministic"`` (目标导向规划器,
            默认) / ``"llm"`` (LLM 选计划 + 引擎执行, 失败回退规划器) /
            ``"agent"`` (原生工具循环: 规则工具 + Monty 沙箱脚本; 模型不支持
            tools 能力时报错, 不做降级)。

    Returns:
        :class:`AnswerResult`; ``steps`` 以前缀的检索推理链开头（查询条件 / 过滤 /
        检索 / 上下文），随后是合成层的推理链。

    Raises:
        ValueError: ``query`` 为空、``top_k`` 非法、``derive_mode`` 非法、
            ``retriever`` 未接线或无检索方法。
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query 不能为空")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ValueError(f"top_k 须为 >= 1 的整数: {top_k!r}")
    if derive_mode not in {"deterministic", "llm", "agent"}:
        raise ValueError(f"derive_mode 非法: {derive_mode!r} (deterministic/llm/agent)")
    if facts is not None and not isinstance(facts, Mapping):
        raise ValueError(f"facts 须为映射或 None: {facts!r}")
    if retriever is None:
        raise ValueError(
            "answer_question 需要 retriever；未接线时对外请走 gateway.ask（retriever_unconfigured）"
        )
    if not callable(getattr(retriever, "retrieve", None)) and not callable(
        getattr(retriever, "retrieve_with_plan", None)
    ):
        raise ValueError("retriever 既没有 retrieve() 也没有 retrieve_with_plan()")

    degradations: list[dict[str, Any]] = []
    if plan is None:
        plan = _understand(query, llm=llm, degradations=degradations)

    hits = _retrieve(retriever, query, plan=plan, top_k=top_k, where=where)
    context, citations = build_context(hits, max_chars=max_chars, notes=degradations, query=query)
    hints = _resolve_rule_hints(query, plan=plan, retriever=retriever, degradations=degradations)
    evaluations = _resolve_evaluations(
        query,
        hints=hints,
        facts=facts,
        degradations=degradations,
        derive_mode=derive_mode,
        llm=llm,
    )

    result = synthesize(
        query,
        context,
        citations,
        plan=plan,
        rule_hints=hints,
        evaluations=evaluations,
        llm=llm,
        covered_hint=bool(citations),  # 一条证据都没装进来 → 先验判不足
    )
    result.degradations = degradations + result.degradations
    result.plan = plan
    result.hits = list(hits)
    steps = _retrieval_steps(
        query, plan=plan, retriever=retriever, where=where, hits=hits,
        context=context, citations=citations, top_k=top_k, max_chars=max_chars,
    )
    if result.degradations:
        steps.append(f"本次应答有 {len(result.degradations)} 项降级留痕（见 degradations）")
    result.steps = steps + result.steps
    return result


def _understand(query: str, *, llm: Any, degradations: list[dict[str, Any]]) -> Any:
    """惰性调用 W3-A 的 ``understand``; 不可用/失败 → 留痕并返回 ``None``（直检索）。"""
    understand, error = _load_understand()
    if understand is None:
        _note(degradations, "understand_unavailable",
              detail=f"语义查询理解不可用: {error or 'matrag.retrieve.understand 未落盘'}",
              fallback="直接以原查询检索")
        return None
    try:
        plan = understand(query, llm=llm)
    except TypeError:
        plan = understand(query)  # 替身签名更窄（不带 llm）
    except Exception as exc:  # noqa: BLE001 —— 降级必须留痕，不静默
        _note(degradations, "understand_failed", detail=f"{type(exc).__name__}: {exc}",
              fallback="直接以原查询检索")
        return None
    for entry in _plan_field(plan, "degradations", []) or []:
        if isinstance(entry, Mapping):
            note = dict(entry)
            note.setdefault("source", "understand")
            degradations.append(note)
    return plan


def _load_understand() -> tuple[Any | None, str]:
    """导入 ``matrag.retrieve.understand.understand``（未落盘 → ``(None, 原因)``）。"""
    try:
        from matrag.retrieve.understand import understand
    except ImportError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return understand, ""


def _load_select_rule_hints() -> tuple[Any | None, str]:
    """导入 W3-B 的 ``select_rule_hints``（未落盘 → ``(None, 原因)``）。"""
    try:
        from matrag.retrieve.rule_hint import select_rule_hints
    except ImportError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return select_rule_hints, ""


def _load_evaluate() -> tuple[Any | None, str]:
    """导入规则求值服务 ``matrag.domain.evaluate.evaluate_for_answer``（未落盘 → ``(None, 原因)``）。"""
    try:
        from matrag.domain.evaluate import evaluate_for_answer
    except ImportError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return evaluate_for_answer, ""


def _resolve_rule_hints(
    query: str,
    *,
    plan: Any,
    retriever: Any,
    rule_hints: Sequence[Any] | None = None,
    degradations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """规则提示来源优先级: 调用方直传 > 检索器留痕（``last_rule_hints``）> 确定性选取。

    检索器已在带计划检索时挂好 ``last_rule_hints`` 就直接复用（不重复选取）;
    其它情形且 ``plan.needs_rules`` 为真时才调 ``select_rule_hints``（确定性、不调 LLM）。
    """
    if rule_hints:
        normalized = _normalize_rule_hints(rule_hints)
        if normalized:
            return normalized
    last = getattr(retriever, "last_rule_hints", None)
    if last:
        normalized = _normalize_rule_hints(last)
        if normalized:
            return normalized
    if _as_bool(_plan_field(plan, "needs_rules")):
        selector, error = _load_select_rule_hints()
        if selector is None:
            _note(degradations, "rule_hint_unavailable",
                  detail=f"规则提示不可用: {error or 'matrag.retrieve.rule_hint 未落盘'}",
                  fallback="答案不带领域规则参考")
        else:
            try:
                selected = selector(query, top_n=DEFAULT_RULE_TOP_N)
            except Exception as exc:  # noqa: BLE001 —— 规则是增强，失败留痕不阻断应答
                _note(degradations, "rule_hint_failed", detail=f"{type(exc).__name__}: {exc}")
            else:
                normalized = _normalize_rule_hints(selected)
                if normalized:
                    return normalized
                _note(degradations, "rule_hint_empty", detail="确定性规则选取没有命中任何规则")
    return []


def _resolve_evaluations(
    query: str,
    *,
    hints: Sequence[Mapping[str, Any]],
    facts: Mapping[str, Any] | None,
    degradations: list[dict[str, Any]],
    derive_mode: str = "deterministic",
    llm: Any = None,
) -> list[dict[str, Any]]:
    """对规则提示代入实测事实求值（显式 ``facts`` 优先，否则从问题文本自动抽取），
    并补充多跳推导条目（``derive_mode``: 确定性规划器 / LLM 编排）。

    增强件缺失/失败 → 留痕（``rule_eval_unavailable`` / ``rule_eval_failed`` /
    ``query_derive_unavailable`` / ``query_derive_failed`` /
    ``llm_orchestrator_unavailable`` / ``llm_orchestrator_failed``）,
    答案退回"只带规则提示"的形态; 空 ``facts={}`` 显式关闭求值。
    """
    if facts is not None and not facts:
        return []  # 显式关闭求值(含多跳推导)
    out: list[dict[str, Any]] = []
    if facts is not None or hints:
        evaluator, error = _load_evaluate()
        if evaluator is None:
            _note(degradations, "rule_eval_unavailable",
                  detail=f"规则求值不可用: {error or 'matrag.domain.evaluate 未落盘'}",
                  fallback="答案不含规则求值")
        else:
            try:
                results = evaluator(hints, query=query, facts=facts)
            except Exception as exc:  # noqa: BLE001 —— 求值是增强，失败留痕不阻断应答
                _note(degradations, "rule_eval_failed",
                      detail=f"{type(exc).__name__}: {exc}")
            else:
                out = _normalize_evaluations(results)
    out.extend(
        _resolve_derivation(
            query, out, facts, degradations, derive_mode=derive_mode, llm=llm
        )
    )
    return out


def _load_query_derive() -> tuple[Any | None, Any | None, str]:
    """导入多跳推导服务 ``matrag.domain.query_derive``（未落盘 → ``(None, None, 原因)``）。"""
    try:
        from matrag.domain.query_derive import derivation_entries, derive_from_text
    except ImportError as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    return derive_from_text, derivation_entries, ""


def _load_llm_orchestrator() -> tuple[Any | None, str]:
    """导入 LLM 编排服务 ``matrag.domain.llm_planner``（未落盘 → ``(None, 原因)``）。"""
    try:
        from matrag.domain.llm_planner import orchestrate_from_text
    except ImportError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return orchestrate_from_text, ""


def _load_tool_agent() -> tuple[Any | None, str]:
    """导入 Agent 工具循环 ``matrag.domain.tool_agent``（未落盘 → ``(None, 原因)``）。"""
    try:
        from matrag.domain.tool_agent import compute_with_agent
    except ImportError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return compute_with_agent, ""


def _is_tool_support_error(exc: BaseException) -> bool:
    """异常是否为 ``tool_agent.ToolSupportError``（模型不支持 tools, 须上抛不降级）。"""
    try:
        from matrag.domain.tool_agent import ToolSupportError
    except ImportError:
        return False
    return isinstance(exc, ToolSupportError)


def _resolve_derivation(
    query: str,
    existing: Sequence[Mapping[str, Any]],
    facts: Mapping[str, Any] | None,
    degradations: list[dict[str, Any]],
    *,
    derive_mode: str = "deterministic",
    llm: Any = None,
) -> list[dict[str, Any]]:
    """计算类问题: 多跳推导 → 与求值同形的逐跳条目。

    三种编排 (产出同形, 逐跳可审计):

    - ``derive_mode="deterministic"``: 目标导向规划器 (``query_derive``);
    - ``derive_mode="llm"``: LLM 选计划 + 引擎执行, 失败自动回退规划器
      (``llm_planner``, ``fallback=True``);
    - ``derive_mode="agent"``: 原生工具循环 + Monty 沙箱 (``tool_agent``,
      模型不支持 tools 能力抛 ``ToolSupportError``, **不降级**)。

    与既有单跳求值按 ``rule_id`` 去重 (补足缺失跳)。
    """
    if not query:
        return []
    derive_fn, entries_fn, error = _load_query_derive()
    if derive_fn is None or entries_fn is None:
        _note(degradations, "query_derive_unavailable",
              detail=f"多跳推导不可用: {error or 'matrag.domain.query_derive 未落盘'}",
              fallback="答案不含多跳推导")
        return []
    if derive_mode not in {"deterministic", "llm", "agent"}:
        raise ValueError(f"derive_mode 非法: {derive_mode!r} (deterministic/llm/agent)")
    entries: list[dict[str, Any]] = []
    strategy = "deterministic"
    if derive_mode == "agent":
        compute, agent_error = _load_tool_agent()
        if compute is None:
            _note(degradations, "tool_agent_unavailable",
                  detail=f"Agent 工具循环不可用: {agent_error or 'matrag.domain.tool_agent 未落盘'}",
                  fallback="多跳推导退化为确定性规划器")
        else:
            try:
                found = compute(query, llm=llm, facts=facts)
            except Exception as exc:  # noqa: BLE001 —— 运行失败留痕; 不支持 tools 则上抛
                if _is_tool_support_error(exc):
                    raise  # 模型不支持 tools 能力 → 拒绝（无降级路径）
                _note(degradations, "tool_agent_failed",
                      detail=f"{type(exc).__name__}: {exc}")
            else:
                if found is not None:
                    strategy = str(found.strategy)
                    entries = [dict(item) for item in found.entries]
                    if found.value is None:
                        _note(degradations, "tool_agent_no_result",
                              detail=f"Agent 未产出数值结果: {found.reason or '无说明'}")
                else:
                    _note(degradations, "tool_agent_no_result",
                          detail="Agent 未产出可用推导（LLM 未接线或无已知值）")
        if not entries:
            # Agent 路径未产出（不可用/失败/无目标）→ 确定性规划器兜底
            derive_mode = "deterministic"
    if derive_mode == "llm":
        orchestrator, orch_error = _load_llm_orchestrator()
        if orchestrator is None:
            _note(degradations, "llm_orchestrator_unavailable",
                  detail=f"LLM 编排不可用: {orch_error or 'matrag.domain.llm_planner 未落盘'}",
                  fallback="多跳推导退化为确定性规划器")
        else:
            try:
                found = orchestrator(query, llm=llm, facts=facts)
            except Exception as exc:  # noqa: BLE001 —— 编排增强, 失败留痕不阻断
                _note(degradations, "llm_orchestrator_failed",
                      detail=f"{type(exc).__name__}: {exc}")
                found = None
            if found is not None:
                strategy = str(found.strategy)
                entries = entries_fn(found.derivation, source="derivation-llm")
        if not entries:
            # LLM 路径未产出 (不可用/失败/无可行目标) → 确定性规划器兜底
            derive_mode = "deterministic"
    if not entries and derive_mode == "deterministic":
        try:
            found = derive_fn(query, facts=facts)
        except Exception as exc:  # noqa: BLE001 —— 推导是增强，失败留痕不阻断应答
            _note(degradations, "query_derive_failed",
                  detail=f"{type(exc).__name__}: {exc}")
            return []
        if found is None:
            return []
        entries = entries_fn(found.derivation)
    have = {str(item.get("rule_id") or "") for item in existing}
    out: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("rule_id") in have:
            continue
        entry = dict(entry)
        entry.setdefault("strategy", strategy)
        out.append(entry)
    return out


def _normalize_rule_hints(hints: Sequence[Any] | None) -> list[dict[str, Any]]:
    """把 ``RuleHint``/dict 统一成 ``{rule_id, statement, kind, score}``。"""
    normalized: list[dict[str, Any]] = []
    for hint in hints or []:
        rule_id = str(_field(hint, "rule_id") or _field(hint, "id") or "").strip()
        statement = str(_field(hint, "statement") or "").strip()
        if not rule_id or not statement:
            continue
        normalized.append(
            {
                "rule_id": rule_id,
                "statement": statement,
                "kind": str(_field(hint, "kind") or ""),
                "score": _as_float(_field(hint, "score"), default=0.0),
            }
        )
    return normalized


def _normalize_evaluations(evaluations: Sequence[Any] | None) -> list[dict[str, Any]]:
    """把 ``EvaluationResult``/dict 统一成 ``{rule_id, ..., display}``。"""
    normalized: list[dict[str, Any]] = []
    for item in evaluations or []:
        if isinstance(item, Mapping):
            data = dict(item)
        else:
            to_dict = getattr(item, "to_dict", None)
            if not callable(to_dict):
                continue
            data = dict(to_dict())
        if not str(data.get("rule_id") or "").strip():
            continue
        if not str(data.get("display") or "").strip():
            data["display"] = _evaluation_display(data)
        normalized.append(data)
    return normalized


def _evaluation_display(item: Mapping[str, Any]) -> str:
    """缺 ``display`` 时的兜底一行渲染（求值层正常会给全字段）。"""
    rule_id = str(item.get("rule_id", ""))
    detail = str(item.get("detail") or item.get("statement") or "").strip()
    return f"{rule_id}: {detail}" if detail else rule_id


def _retrieve(
    retriever: Any,
    query: str,
    *,
    plan: Any,
    top_k: int,
    where: Mapping[str, Any] | None,
) -> list[Any]:
    """检索（有 plan 且检索器支持 ``retrieve_with_plan`` 就走 plan 感知路径）。"""
    method = getattr(retriever, "retrieve_with_plan", None)
    plain = getattr(retriever, "retrieve", None)
    if callable(method) and (plan is not None or not callable(plain)):
        # plan=None 调 ``retrieve_with_plan`` 与 ``retrieve`` 等价（见 HybridRetriever 契约），
        # 这里只在"检索器没有 retrieve"时才这么兜底。
        variants = [
            ((query, plan), {"top_k": top_k, "where": where}),
            ((query,), {"plan": plan, "top_k": top_k, "where": where}),
            ((query, plan), {"top_k": top_k}),
            ((query,), {"plan": plan, "top_k": top_k}),
            ((query, plan), {}),
            ((query,), {"plan": plan}),
            ((query,), {"top_k": top_k, "where": where}),
            ((query,), {"top_k": top_k}),
            ((query,), {}),
        ]
        return list(_first_bindable(method, variants))
    return list(
        _first_bindable(
            plain,
            [((query,), {"top_k": top_k, "where": where}), ((query,), {"top_k": top_k}), ((query,), {})],
        )
    )


def _first_bindable(fn: Any, variants: list[tuple[tuple[Any, ...], dict[str, Any]]]) -> Any:
    """按签名挑第一个能绑定的调用形态再调用（兼容检索器替身/新签名的差异）。"""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover - C 扩展等无签名对象
        signature = None
    for args, kwargs in variants:
        if signature is not None:
            try:
                signature.bind(*args, **kwargs)
            except TypeError:
                continue
        return fn(*args, **kwargs)
    raise TypeError(f"{getattr(fn, '__qualname__', fn)!r} 不接受任何候选调用形态: {variants!r}")


# --------------------------------------------------------------------------- #
# 推理链（确定性）
# --------------------------------------------------------------------------- #
def _retrieval_steps(
    query: str,
    *,
    plan: Any,
    retriever: Any,
    where: Mapping[str, Any] | None,
    hits: Sequence[Any],
    context: str,
    citations: Sequence[Citation],
    top_k: int,
    max_chars: int,
) -> list[str]:
    """检索阶段推理链: 查询条件 → 过滤 → 检索 → 上下文打包（全部确定性）。"""
    legs: list[str] = []
    for hit in hits:
        for leg in _field(hit, "legs_hit", ()) or ():
            if str(leg) not in legs:
                legs.append(str(leg))
    return [
        _query_step(query, plan),
        _where_step(retriever, where),
        f"检索到 {len(hits)} 条证据（top_k={top_k}；命中腿：{'、'.join(legs) or '无'}；RRF 融合分降序）",
        f"装入上下文 {len(citations)} 条证据（{len(context)} 字符，上限 {max_chars} 字符 / "
        f"{MAX_CONTEXT_ITEMS} 条）",
    ]


def _query_step(query: str, plan: Any) -> str:
    """步骤 1: 抽取查询条件（无 plan 时如实说明未做语义理解）。"""
    if plan is None:
        return f"未做语义查询理解（无 QueryPlan），直接以原查询检索：{query}"
    parts = [f"抽取查询条件：{query}"]
    kind = _plan_text(_plan_field(plan, "kind") or _plan_field(plan, "intent"))
    if kind:
        parts.append(f"意图={kind}")
    entities = _plan_text(_plan_field(plan, "entities"))
    if entities:
        parts.append(f"关键条件={entities}")
    sub_queries = _plan_text(_plan_field(plan, "sub_queries"))
    if sub_queries:
        parts.append(f"子查询={sub_queries}")
    retrieval_queries = _plan_text(_plan_field(plan, "retrieval_queries"))
    if retrieval_queries:
        parts.append(f"检索式={retrieval_queries}")
    return "；".join(parts)


def _where_step(retriever: Any, where: Mapping[str, Any] | None) -> str:
    """步骤 2: 过滤条件（优先读检索器的 ``last_plan['where']``，否则用调用方传入值）。"""
    effective: Any = None
    last_plan = getattr(retriever, "last_plan", None)
    if isinstance(last_plan, Mapping):
        effective = last_plan.get("where")
    if not isinstance(effective, Mapping) or not effective:
        effective = where if isinstance(where, Mapping) else None
    if not effective:
        return "未附加过滤条件（全库检索）"
    parts = []
    for key, value in effective.items():
        if key == _KEY_SECTION_CONTAINS:
            parts.append(f"按章节「{value}」过滤")
        else:
            parts.append(f"按 {key}={value} 过滤")
    return "；".join(parts)


def _evidence_step(citations: Sequence[Citation], context_len: int) -> str:
    """合成阶段: 证据装配。"""
    if not citations:
        return "装配上下文证据 0 条"
    return (
        f"装配上下文证据 {len(citations)} 条（{context_len} 字符，"
        f"行级引用 [1]~[{len(citations)}]）"
    )


def _rule_step(hints: Sequence[Mapping[str, Any]]) -> str:
    """合成阶段: 规则应用（规则只是常识参考，不作为规格数值来源）。"""
    ids = "、".join(str(hint.get("rule_id", "")) for hint in hints)
    return f"应用确定性规则 {len(hints)} 条：{ids}（规则为常识参考，不作为规格数值来源）"


def _evaluation_step(evaluations: Sequence[Mapping[str, Any]]) -> str:
    """合成阶段: 规则求值（代入问题中数值的公式计算，非规格数值来源）。"""
    ids = "、".join(str(item.get("rule_id", "")) for item in evaluations)
    return f"规则求值 {len(evaluations)} 条：{ids}（代入问题中数值计算，非规格书数据）"


def _usage_step(answer: str, citations: Sequence[Citation]) -> str:
    """引用统计: 哪些证据被引用、哪些没有（未被引用的也保留，供检索兜底展示）。"""
    known = {c.marker for c in citations}
    used = sorted({f"[{num}]" for num in MARKER_PATTERN.findall(answer)} & known,
                  key=lambda marker: int(marker[1:-1]))
    unused = [c.marker for c in citations if c.marker not in set(used)]
    parts = [f"答案引用证据 {len(used)} 条：{'、'.join(used) or '无'}"]
    if unused:
        parts.append(f"未被答案引用的证据 {len(unused)} 条：{'、'.join(unused)}（已保留供回溯）")
    else:
        parts.append("未被答案引用的证据 0 条")
    return "；".join(parts)


def _anchor_text(citation: Citation) -> str:
    """锚点的一行文本（``doc p页 章节 表``）。"""
    parts = [citation.doc_id or "(未知文档)", f"p{citation.page}"]
    if citation.section_path:
        parts.append(citation.section_path)
    if citation.table_id:
        parts.append(f"表{citation.table_id}")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# 提示词/正文里的规则与求值分区
# --------------------------------------------------------------------------- #
def _rule_block(hints: Sequence[Mapping[str, Any]]) -> str:
    """提示词里的规则分区（statement 原样引用，不改写）。"""
    if not hints:
        return ""
    return f"{RULE_SECTION_HEADER}\n" + "\n".join(_rule_lines(hints))


def _evaluation_block(evaluations: Sequence[Mapping[str, Any]]) -> str:
    """提示词里的求值分区（计算值，与规格证据、规则常识三分区呈现）。"""
    if not evaluations:
        return ""
    return f"{EVAL_SECTION_HEADER}\n" + "\n".join(_evaluation_lines(evaluations))


def _rule_lines(hints: Sequence[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    for hint in hints:
        rule_id = str(hint.get("rule_id", ""))
        kind = str(hint.get("kind") or "")
        tail = f"（{kind}）" if kind else ""
        lines.append(f"- {rule_id}{tail}: {hint.get('statement', '')}")
    return lines


def _evaluation_lines(evaluations: Sequence[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in evaluations:
        display = str(item.get("display") or "").strip() or _evaluation_display(item)
        lines.append(f"- {display}")
    return lines


def _plan_block(plan: Any) -> str:
    """提示词里的查询理解摘要（明确标注"不是证据"）。"""
    if plan is None:
        return "（本次没有查询理解计划，按原查询检索）"
    lines = [f"意图={_plan_text(_plan_field(plan, 'kind') or _plan_field(plan, 'intent')) or '未标注'}"]
    entities = _plan_field(plan, "entities")
    if entities:
        lines.append(f"关键条件={_plan_text(entities)}")
    sub_queries = _plan_field(plan, "sub_queries")
    if sub_queries:
        lines.append(f"子查询={_plan_text(sub_queries)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def _field(obj: Any, name: str, default: Any = None) -> Any:
    """从命中/规则对象取字段: Mapping 直接取，否则 getattr（不抛异常）。"""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _plan_field(plan: Any, name: str, default: Any = None) -> Any:
    """从查询理解计划取字段（dataclass / Mapping 都支持；缺字段返回 ``default``）。"""
    if plan is None:
        return default
    return _field(plan, name, default)


def _as_float(value: Any, *, default: float = 0.0) -> float:
    """宽松取浮点（非数值退化为 ``default``，得分缺失不致命）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _as_int(value: Any, *, default: int = 0) -> int:
    """宽松取整数（非数值退化为 ``default``）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _as_bool(value: Any, *, default: bool = False) -> bool:
    """宽松取布尔（LLM 可能给 ``"true"``/``1``；无法判定时用 ``default``）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n", ""}:
            return False
    return default


def _plan_text(value: Any) -> str:
    """查询理解字段的人读文本（枚举取 ``.value``，序列用 ``、`` 连接）。"""
    if value is None:
        return ""
    inner = getattr(value, "value", None)
    if inner is not None and not isinstance(value, (str, bytes)):
        return str(inner)
    if isinstance(value, Mapping):
        return "、".join(f"{k}={v}" for k, v in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return "、".join(str(item) for item in value)
    return str(value)


def _note(notes: list[dict[str, Any]] | None, kind: str, **fields: Any) -> None:
    """记录一次降级（``notes`` 为 ``None`` 时调用方主动放弃留痕）。"""
    if notes is None:
        return
    entry: dict[str, Any] = {"kind": kind}
    entry.update(fields)
    notes.append(entry)


def _llm_unavailable_reason(llm: Any) -> str | None:
    """LLM 不可用原因（``None`` 表示可用）。"""
    if llm is None:
        return "未传入 LLM（llm=None）"
    if not _as_bool(getattr(llm, "configured", True), default=True):
        return "LLM 未配置（llm.configured=False）"
    return None


def _complete_json(llm: Any, *, system: str, user: str, schema_hint: str) -> Any:
    """LLM JSON 调用（鸭子类型: 兼容带 ``schema_hint`` 与只收两参的替身）。"""
    try:
        return llm.complete_json(system, user, schema_hint=schema_hint)
    except TypeError:  # 替身签名更窄
        return llm.complete_json(system, user)


__all__ = [
    "CITATION_TEXT_MAX_CHARS",
    "DEFAULT_RULE_TOP_N",
    "DEFAULT_TOP_K",
    "INSUFFICIENT_HINT",
    "MAX_CONTEXT_CHARS",
    "MAX_CONTEXT_ITEMS",
    "AnswerResult",
    "Citation",
    "SYSTEM_PROMPT",
    "SCHEMA_HINT",
    "answer_question",
    "build_context",
    "build_user_prompt",
    "synthesize",
]
