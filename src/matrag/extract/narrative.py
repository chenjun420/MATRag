"""叙述文本 → LLM 提案 (draft 红线 + 事实护栏)。

输入: doc.json 的 blocks (heading/paragraph/table) + 方法目录 + LLM 客户端;
输出: 叙述提案组装成的 draft requirements + review + stats。

红线 (设计冻结, 不得放宽)
------------------------
- LLM 提案一律 ``status="draft"`` / ``source="narrative"`` —— 生产侧绝不代签,
  消费侧规划器会把非 approved 条件排除在执行序列外并列入 pending。
- 事实护栏: 提案 value 的每个数值必须能按字面在原文块中出现, 否则弃该数值并落
  review; kind=measure 的条目弃值后直接把整条弃掉 (不得无判据测量)。
- ``method_ref`` 必须存在于方法目录 (power_test_methods.yaml), 否则清空并落 review。
- kind/side 必须 ∈ :data:`matrag.extract.kinds.KNOWN_KIND_SIDES`, 否则弃条目并落 review。

确定性: 相同输入与相同 LLM 输出 → 相同提案 (编号按块出现顺序 ``NAR-{product}-{seq:03d}``)。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from matrag.domain.spec_params import classify_section
from matrag.domain.test_methods import TestMethod
from matrag.errors import JSONParseError
from matrag.extract.bundle import ClauseModel, RequirementModel, ReviewModel, ScenarioModel
from matrag.extract.fingerprints import cond_fingerprint, req_fingerprint
from matrag.extract.kinds import KIND_UNITS, KNOWN_KIND_SIDES
from matrag.extract.patterns import MEASURE_KINDS, ascending_limits, normalize_text
from matrag.llm import LLMClient

__all__ = ["NarrativeResult", "build_requirements", "extract_narrative", "propose_from_blocks"]

#: 允许出提案的类别 —— 与 spec_tables 的排除面互补 (非产测类别不出提案)。
IN_SCOPE_CATEGORIES: frozenset[str] = frozenset(
    {"input", "output", "protection", "efficiency", "dynamics", "communication"}
)

_MIN_BLOCK_CHARS = 6
_BLOCK_CHUNK = 8
_MAX_METHODS_IN_PROMPT = 120

_SYSTEM_PROMPT = (
    "你是电源规格书产测抽取助手。输入给出 kind 词表与方法目录, 以及若干原文段落。"
    "任务: 只从原文抽取可用于产测评审的要求条目, 每条映射到一个 (kind, side), 输出严格 JSON。"
    "铁律: ①只能使用原文中逐字出现的数值, 不得换算、推断或补全; "
    "②kinds 列表中 measure=true 的条目必须带 value 数值; "
    "③method_ref 只能填方法目录中的 id, 不确定就省略; "
    "④把握不足就不要输出该条; ⑤text 用原文措辞, 不要改写加入新信息。"
)

_SCHEMA_HINT = (
    '{"proposals":[{"block":0,"kind":"output_voltage","side":"output",'
    '"text":"原文要求陈述","value":{"min":0,"typ":0,"max":0,"unit":"V"},"method_ref":""}]}'
)


@dataclass(frozen=True)
class NarrativeBlock:
    """被选入提案的原文块 (index 为 doc.json blocks 下标, 供 LLM 引用)。"""

    index: int
    section_path: str
    page: int
    text: str


@dataclass
class NarrativeResult:
    """叙述抽取的组装结果。"""

    requirements: list[RequirementModel] = field(default_factory=list)
    review: list[ReviewModel] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    skipped_reason: str = ""


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _section_leaf(section_path: str) -> str:
    return (section_path or "").split(">")[-1].strip()


def _kinds_catalog() -> list[dict[str, Any]]:
    return [
        {"kind": k, "side": s, "unit": KIND_UNITS.get((k, s), ""), "measure": k in MEASURE_KINDS}
        for (k, s) in sorted(KNOWN_KIND_SIDES)
    ]


def select_blocks(blocks: Sequence[dict[str, Any]], *, max_blocks: int) -> tuple[list[NarrativeBlock], dict[str, int]]:
    """挑出可提案段落: paragraph + 长度门槛 + 在产测类别内 + 文本去重, 截断留痕。"""
    stats = {"blocks_total": 0, "blocks_in_scope": 0, "blocks_used": 0, "blocks_truncated": 0}
    seen: set[str] = set()
    out: list[NarrativeBlock] = []
    for i, b in enumerate(blocks):
        stats["blocks_total"] += 1
        if not isinstance(b, dict) or b.get("kind") != "paragraph":
            continue
        text = normalize_text(b.get("text"))
        if len(text) < _MIN_BLOCK_CHARS:
            continue
        section = str(b.get("section_path") or "").strip()
        if classify_section(section) not in IN_SCOPE_CATEGORIES:
            continue
        if text in seen:
            continue
        seen.add(text)
        stats["blocks_in_scope"] += 1
        out.append(NarrativeBlock(index=i, section_path=section, page=int(b.get("page") or 0), text=text))
    if len(out) > max_blocks:
        stats["blocks_truncated"] = len(out) - max_blocks
        out = out[:max_blocks]
    stats["blocks_used"] = len(out)
    return out, stats


def _seen_number(num: float, text: str) -> bool:
    """数值是否按字面出现在原文 (fact guard; 接受 220/220.0/+220 等写法)。"""
    forms = {f"{num:g}", str(num)}
    if num > 0:
        forms.add(f"+{num:g}")
    return any(form in text for form in forms)


def _clause_from_item(
    item: dict[str, Any],
    block: NarrativeBlock,
    method_ids: set[str],
    stats: dict[str, int],
    review: list[ReviewModel],
) -> ClauseModel | None:
    """单条 LLM 提案 → ClauseModel (draft); 触发护栏时返回 None (已落 review)。"""
    kind = str(item.get("kind") or "").strip()
    side = str(item.get("side") or "").strip()
    if (kind, side) not in KNOWN_KIND_SIDES:
        stats["kind_dropped"] = stats.get("kind_dropped", 0) + 1
        review.append(
            ReviewModel(
                kind="narrative_kind_invalid",
                section_path=_fit(block.section_path, 64),
                heading=_fit(f"{kind}/{side}", 255),
                detail=_fit(f"提案 kind/side 不在词表, 已弃: {kind}/{side}", 1000),
                hint="如该概念确需产测, 先在提取侧扩展词表与绑定表。",
                method_ref="",
            )
        )
        return None
    text = normalize_text(item.get("text"))
    if not text or len(text) > 2000:
        stats["text_dropped"] = stats.get("text_dropped", 0) + 1
        return None

    value: dict[str, Any] | None = None
    raw_value = item.get("value")
    if isinstance(raw_value, dict):
        kept: dict[str, Any] = {}
        for key in ("min", "typ", "max"):
            num = raw_value.get(key)
            if isinstance(num, bool) or not isinstance(num, (int, float)):
                continue
            if _seen_number(float(num), block.text):
                kept[key] = round(float(num), 6)
            else:
                stats["values_dropped"] = stats.get("values_dropped", 0) + 1
                review.append(
                    ReviewModel(
                        kind="narrative_value_dropped",
                        section_path=_fit(block.section_path, 64),
                        heading=_fit(text, 255),
                        detail=_fit(f"提案数值 {num} 未在原文块中出现, 已弃用。", 1000),
                        hint="数值须逐字见于原文, 不得换算或推断; 如确为该行判据, 请人工补录。",
                        method_ref="",
                    )
                )
        unit = str(raw_value.get("unit") or "").strip()
        if kept and unit and len(unit) <= 32:
            kept["unit"] = unit
        if kept:
            # 负轨 min/max 幅值序 → 数学升序 (GAP-01); 数字集合不变, 事实护栏不受影响。
            value = ascending_limits(kept)
    if kind in MEASURE_KINDS and value is None:
        stats["measure_dropped"] = stats.get("measure_dropped", 0) + 1
        review.append(
            ReviewModel(
                kind="narrative_measure_no_value",
                section_path=_fit(block.section_path, 64),
                heading=_fit(text, 255),
                detail="measure 类提案无原文可核数值, 已弃 (不得无判据测量)。",
                hint="如需纳入, 请人工补充判据数值并复核。",
                method_ref="",
            )
        )
        return None

    method_ref = str(item.get("method_ref") or "").strip()
    if method_ref and method_ref not in method_ids:
        stats["methods_cleared"] = stats.get("methods_cleared", 0) + 1
        review.append(
            ReviewModel(
                kind="narrative_method_ref_invalid",
                section_path=_fit(block.section_path, 64),
                heading=_fit(method_ref, 255),
                detail=_fit(f"提案方法 {method_ref} 不在方法目录, 已清空。", 1000),
                hint="method_ref 必须是 power_test_methods.yaml 中的 id。",
                method_ref="",
            )
        )
        method_ref = ""

    return ClauseModel(
        kind=kind,
        text=text,
        role=side,  # type: ignore[arg-type]
        value=value,
        source="narrative",
        confidence="llm",
        status="draft",
        method_ref=_fit(method_ref, 64),
        cond_fingerprint=cond_fingerprint(kind=kind, side=side, value=value, method_ref=method_ref),
    )


def propose_from_blocks(
    blocks: Sequence[dict[str, Any]],
    *,
    methods: Sequence[TestMethod] | None = None,
    llm: LLMClient | None = None,
    max_blocks: int = 24,
    max_clauses: int = 48,
) -> tuple[list[tuple[NarrativeBlock, ClauseModel]], list[ReviewModel], dict[str, int], str]:
    """blocks → [(块, draft clause)] + review + stats + skipped_reason。

    LLM 未配置时整体跳过 (skipped_reason="llm_not_configured", 记 stats 留痕);
    单个分片调用失败不中断全局, 落 review(kind=narrative_llm_error)。
    """
    selected, stats = select_blocks(blocks, max_blocks=max_blocks)
    review: list[ReviewModel] = []
    proposals: list[tuple[NarrativeBlock, ClauseModel]] = []
    if not selected:
        stats["clauses_draft"] = 0
        return proposals, review, stats, "no_candidates"
    if llm is None or not getattr(llm, "configured", False):
        stats["clauses_draft"] = 0
        stats["skipped_llm_off"] = 1
        return proposals, review, stats, "llm_not_configured"

    method_ids = {m.method_id for m in (methods or [])}
    method_list = sorted(method_ids)[:_MAX_METHODS_IN_PROMPT]
    catalog = _kinds_catalog()
    by_index = {b.index: b for b in selected}
    seen_fp: set[str] = set()
    for start in range(0, len(selected), _BLOCK_CHUNK):
        chunk = selected[start : start + _BLOCK_CHUNK]
        payload = {
            "kinds": catalog,
            "methods": method_list,
            "blocks": [{"index": b.index, "section": b.section_path, "text": b.text} for b in chunk],
        }
        stats["calls"] = stats.get("calls", 0) + 1
        try:
            data = llm.complete_json(
                _SYSTEM_PROMPT,
                json.dumps(payload, ensure_ascii=False),
                schema_hint=_SCHEMA_HINT,
                max_tokens=2000,
            )
        except (RuntimeError, JSONParseError) as exc:
            stats["llm_errors"] = stats.get("llm_errors", 0) + 1
            review.append(
                ReviewModel(
                    kind="narrative_llm_error",
                    section_path=_fit(chunk[0].section_path, 64),
                    heading="LLM 提案调用失败",
                    detail=_fit(str(exc), 1000),
                    hint="确定性部分不受影响; 修复 LLM 配置/网络后重跑即可补齐。",
                    method_ref="",
                )
            )
            continue
        items = data.get("proposals") if isinstance(data, dict) else None
        if not isinstance(items, list):
            stats["llm_errors"] = stats.get("llm_errors", 0) + 1
            review.append(
                ReviewModel(
                    kind="narrative_llm_bad_response",
                    section_path=_fit(chunk[0].section_path, 64),
                    heading="LLM 返回缺少 proposals 列表",
                    detail=_fit(str(data)[:400], 1000),
                    hint="",  # type: ignore[arg-type]
                    method_ref="",
                )
            )
            continue
        for item in items:
            stats["proposals_raw"] = stats.get("proposals_raw", 0) + 1
            if not isinstance(item, dict):
                continue
            try:
                block = by_index[int(item.get("block"))]
            except (KeyError, TypeError, ValueError):
                stats["bad_block_ref"] = stats.get("bad_block_ref", 0) + 1
                continue
            clause = _clause_from_item(item, block, method_ids, stats, review)
            if clause is None:
                continue
            if clause.cond_fingerprint in seen_fp:
                stats["duplicates"] = stats.get("duplicates", 0) + 1
                continue
            seen_fp.add(clause.cond_fingerprint)
            if len(proposals) >= max_clauses:
                stats["clauses_truncated"] = stats.get("clauses_truncated", 0) + 1
                continue
            proposals.append((block, clause))
    stats["clauses_draft"] = len(proposals)
    return proposals, review, stats, ""


def build_requirements(
    proposals: Sequence[tuple[NarrativeBlock, ClauseModel]],
    *,
    product_code: str,
    exclude_fingerprints: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[RequirementModel], list[ReviewModel], dict[str, int]]:
    """按源块分组, 每块一个 draft requirement (NAR-{product}-{seq:03d})。"""
    stats = {"deduped_against_spec": 0, "requirements": 0, "clauses_bound": 0}
    groups: dict[int, list[tuple[NarrativeBlock, ClauseModel]]] = {}
    order: list[int] = []
    for block, clause in proposals:
        if clause.cond_fingerprint in exclude_fingerprints:
            stats["deduped_against_spec"] += 1
            continue
        if block.index not in groups:
            groups[block.index] = []
            order.append(block.index)
        groups[block.index].append((block, clause))
    requirements: list[RequirementModel] = []
    for seq, idx in enumerate(order, start=1):
        items = groups[idx]
        block = items[0][0]
        code = f"NAR-{product_code}-{seq:03d}"
        title = _section_leaf(block.section_path) or f"叙述提案 {seq:03d}"
        input_conditions = [c for _, c in items if c.role == "input"]
        output_conditions = [c for _, c in items if c.role == "output"]
        scenario = ScenarioModel(
            scenario_id=_fit(f"{code}-S001", 200),
            seq=1,
            name=_fit(title, 200),
            rail="",
            bindings={},
            derived={},
            basis=_fit(f"叙述块: {block.text}", 200),
            source="narrative",
        )
        req_fp = req_fingerprint(
            requirement_code=code,
            title=title,
            input_fps=[c.cond_fingerprint for c in input_conditions],
            output_fps=[c.cond_fingerprint for c in output_conditions],
            limits=[],
        )
        requirements.append(
            RequirementModel(
                requirement_code=_fit(code, 200),
                spec_requirement_id="",
                title=_fit(title, 255),
                section_path=_fit(block.section_path, 64),
                role="",
                description="LLM 叙述提案（未签字; 评审签字前不得作为产测判据）。",
                notes=_fit(block.text, 2000),
                req_fingerprint=req_fp,
                flags=["narrative"],
                input_conditions=input_conditions,
                output_conditions=output_conditions,
                limits=[],
                scenarios=[scenario],
            )
        )
        stats["clauses_bound"] += len(items)
    stats["requirements"] = len(requirements)
    return requirements, [], stats


def extract_narrative(
    blocks: Sequence[dict[str, Any]],
    *,
    product_code: str,
    methods: Sequence[TestMethod] | None = None,
    llm: LLMClient | None = None,
    exclude_fingerprints: frozenset[str] | set[str] = frozenset(),
    max_blocks: int = 24,
    max_clauses: int = 48,
) -> NarrativeResult:
    """一步到位: blocks → draft requirements (propose + build 组合)。"""
    proposals, review, stats, skipped = propose_from_blocks(
        blocks, methods=methods, llm=llm, max_blocks=max_blocks, max_clauses=max_clauses
    )
    requirements, build_review, build_stats = build_requirements(
        proposals, product_code=product_code, exclude_fingerprints=exclude_fingerprints
    )
    stats.update(build_stats)
    review.extend(build_review)
    return NarrativeResult(requirements=requirements, review=review, stats=stats, skipped_reason=skipped)
