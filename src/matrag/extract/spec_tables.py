"""确定性表 → requirements 投影 (P2 第二层)。

输入: :mod:`matrag.domain.spec_params` 对**一份规格书**抽出的全部行 (同 product_code)。
输出: requirements / excluded / review / stats —— 全部确定性, 无 LLM。

判定顺序 (设计冻结, 见 ``data/reports/extract-p2-design.md`` §2):
    1. grade 含"不要求" → excluded (matched_word="不要求")
    2. 非产测范围 (scope/category/环境词) → excluded (理由留痕, 不静默丢)
    3. match_kind 未命中 → review (可审计降级)
    4. 命中 → 变体分组后成 Requirement:
       - measure 类 kind 无数值 → review (绝不造数, 不排无判据步骤)
       - 多值保护行 → 拆为 limits (保护点/恢复点/回差), clause 无数值
       - 同编号多判据 → 后缀 ``-m{代表值}``, 仍冲突 → ``-c{2..}`` (附 review 留痕)
       - 输入侧条件: 原文数值 > 产品基线 (取自基线时 notes 留痕)
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from matrag.domain.spec_params import SpecParameter
from matrag.extract import fingerprints, patterns
from matrag.extract.bundle import (
    ClauseModel,
    ExcludedModel,
    LimitModel,
    RequirementModel,
    ReviewModel,
    ScenarioModel,
)
from matrag.extract.kinds import validate_clause_kind
from matrag.extract.patterns import KindMatch, format_number, match_kind, normalize_text, rep_value

__all__ = [
    "CATEGORY_LABEL",
    "ENV_NAME_KEYS",
    "EXCLUDED_CATEGORIES",
    "EXCLUDED_SCOPES",
    "SpecTableResult",
    "build_from_tables",
]

#: scope 属非产测范围的 (CY/PA601/GDC 实测)。
EXCLUDED_SCOPES: frozenset[str] = frozenset({"其他要求", "产品包装", "其它功能"})

#: category 属非产测范围的 (本体枚举见 domain/vocab.py)。
EXCLUDED_CATEGORIES: frozenset[str] = frozenset(
    {"environment", "emc", "safety", "reliability", "mechanical", "thermal", "standards"}
)

#: category=other 时按名称识别环境/可靠性条件 (GDC 绝对额定最大值等行)。
ENV_NAME_KEYS: tuple[str, ...] = ("温度", "湿度", "海拔", "凝露", "MTBF", "无故障", "寿命")

CATEGORY_LABEL: dict[str, str] = {
    "environment": "环境条件",
    "emc": "EMC",
    "safety": "安规",
    "reliability": "可靠性指标",
    "mechanical": "机械结构",
    "thermal": "热设计",
    "standards": "标准引用",
}


@dataclass
class SpecTableResult:
    """一份规格书的确定性投影结果 (narrative/pipeline 会在此之上叠加)。"""

    requirements: list[RequirementModel] = field(default_factory=list)
    excluded: list[ExcludedModel] = field(default_factory=list)
    review: list[ReviewModel] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class _RowPlan:
    """一行通过映射闸门后的待分组中间态。"""

    param: SpecParameter
    match: KindMatch
    value: dict[str, Any] | None
    split: list[tuple[str, dict[str, Any] | None]] | None = None


@dataclass
class _Variant:
    """一个需求变体 (SR 编号/合成编号 + 轨 + kind + 判据集)。"""

    code: str
    kind: str
    side: str
    rail: str
    plans: list[_RowPlan] = field(default_factory=list)
    split: list[tuple[str, dict[str, Any] | None]] | None = None
    synthesized: bool = False

    @property
    def value(self) -> dict[str, Any] | None:
        return self.plans[0].value if self.plans else None


def _fit(text: str | None, limit: int) -> str:
    """截断到字段上限 (超长加省略号, 不静默丢长短)。"""
    t = (text or "").strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _fit_section(path: str | None) -> str:
    """section_path ≤64: 超长时保留末级片段。"""
    p = (path or "").strip()
    if len(p) <= 64:
        return p
    segs = [s.strip() for s in re.split(r"\s*(?:>|›|/)\s*", p) if s.strip()]
    out = ""
    for s in reversed(segs):
        cand = f"{s} > {out}" if out else s
        if len(cand) > 64:
            break
        out = cand
    return out or p[:63] + "…"


def _synth_code(p: SpecParameter) -> str:
    """无编号文档的合成编号: SR-{product}-{table}-{row} (稳定可追溯)。"""
    return f"SR-{p.product_code}-{p.table_id}-{p.row_index}"


def _exclusion(p: SpecParameter) -> ExcludedModel | None:
    """第 1/2 步: 排除判定 (不要求 / 非产测范围)。理由必须留痕。"""

    def ex(reason: str, word: str = "") -> ExcludedModel:
        return ExcludedModel(
            req_id=_fit(p.code, 100),
            title=_fit(normalize_text(p.name), 255),
            section_path=_fit_section(p.section_path),
            reason=reason,
            matched_word=word,
            notes=_fit(p.note, 2000),
        )

    grade = p.grade or ""
    if "不要求" in grade or (grade.strip() in ("", "-") and "不要求" in (p.note or "")):
        return ex('规格标注"不要求"', "不要求")
    scope = normalize_text(p.scope)
    if scope in EXCLUDED_SCOPES:
        return ex(f"非产测范围: {scope}", scope)
    if p.category in EXCLUDED_CATEGORIES:
        return ex(f"非产测范围: {CATEGORY_LABEL.get(p.category, p.category)}", p.category)
    if p.category == "other" and any(k in normalize_text(p.name) for k in ENV_NAME_KEYS):
        return ex("非产测范围: 环境/可靠性条件")
    return None


def _classify(params: Sequence[SpecParameter]) -> tuple[list[_RowPlan], list[ExcludedModel], list[ReviewModel]]:
    """第 1~4 步的行级判定, 产出待分组计划 / 排除 / 复核。"""
    plans: list[_RowPlan] = []
    excluded: list[ExcludedModel] = []
    review: list[ReviewModel] = []
    for p in params:
        ex = _exclusion(p)
        if ex is not None:
            excluded.append(ex)
            continue
        m = match_kind(p)
        heading = _fit(normalize_text(p.name), 255)
        if m is None:
            review.append(
                ReviewModel(
                    kind="unmapped",
                    section_path=_fit_section(p.section_path),
                    heading=heading,
                    detail=f"未命中映射表 (cat={p.category or '-'}, scope={p.scope or '-'})",
                    hint="如需纳入产测: 先补 ATEStudio 绑定方法, 再在 patterns.py 扩规则后重抽",
                )
            )
            continue
        validate_clause_kind(m.kind, m.side, origin=f"{p.product_code}:{p.table_id}:{p.row_index}")
        value = patterns.build_value(p, m.kind)
        split = None
        if value is None and "multi_value" in p.flags:
            parts = patterns.split_multi_value(p, m.kind)
            if parts:
                split = parts
            else:
                review.append(
                    ReviewModel(
                        kind="multi_value_parse",
                        section_path=_fit_section(p.section_path),
                        heading=heading,
                        detail="多值行无法对齐拆分 (行数/单位不齐), 未物化为 limits",
                        hint="人工核对原文后修正表格结构或补充拆分规则",
                    )
                )
        if value is None and split is None and patterns.needs_numeric(m.kind):
            review.append(
                ReviewModel(
                    kind="measure_no_value",
                    section_path=_fit_section(p.section_path),
                    heading=heading,
                    detail=f"measure 类 kind={m.kind} 无数值判据 (原文数值字段为空或不可解析)",
                    hint="补判据后重抽; 不得为凑可执行性编造数值",
                )
            )
            continue
        plans.append(_RowPlan(param=p, match=m, value=value, split=split))
    return plans, excluded, review


def _value_key(value: dict[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, ensure_ascii=False)


def _variant_key(pl: _RowPlan, rail: str) -> tuple[str, str, str | None]:
    """变体键: (轨, kind, 判据集)。split 行以拆分结果做键。"""
    if pl.value is not None:
        return (rail, pl.match.kind, _value_key(pl.value))
    if pl.split is not None:
        key = json.dumps([(lab, val) for lab, val in pl.split], sort_keys=True, ensure_ascii=False)
        return (rail, pl.match.kind, f"SPLIT:{key}")
    return (rail, pl.match.kind, None)


def _group_variants(plans: Sequence[_RowPlan]) -> list[_Variant]:
    """按 (编号, 轨, kind, 判据集) 分组; 无值行并入同编号同轨的唯一数值变体。"""
    order: list[str] = []
    by_code: dict[str, list[_RowPlan]] = {}
    for pl in plans:
        code = pl.param.code or _synth_code(pl.param)
        if code not in by_code:
            by_code[code] = []
            order.append(code)
        by_code[code].append(pl)

    variants: list[_Variant] = []
    for code in order:
        rows = by_code[code]
        synth = not rows[0].param.code
        local: list[_Variant] = []
        index: dict[tuple[str, str, str | None], _Variant] = {}
        for pl in rows:
            rail = patterns.rail_of(pl.param)
            key = _variant_key(pl, rail)
            if key[2] is None:
                continue  # 无值行第二趟处理
            v = index.get(key)
            if v is None:
                v = _Variant(code=code, kind=pl.match.kind, side=pl.match.side, rail=rail, synthesized=synth)
                index[key] = v
                local.append(v)
            v.plans.append(pl)
            if pl.split is not None:
                v.split = pl.split
        for pl in rows:
            if _variant_key(pl, patterns.rail_of(pl.param))[2] is not None:
                continue
            rail = patterns.rail_of(pl.param)
            numeric = [v for v in local if v.rail == rail and v.kind == pl.match.kind]
            if len(numeric) == 1:
                numeric[0].plans.append(pl)
                continue
            v = _Variant(code=code, kind=pl.match.kind, side=pl.match.side, rail=rail, synthesized=synth)
            v.plans.append(pl)
            local.append(v)
        variants.extend(local)
    return variants


def _assign_codes(variants: list[_Variant]) -> list[ReviewModel]:
    """编号消歧: 多判据加 -m{代表值}, 仍冲突追加 -c{2..}; 冲突留痕入 review。"""
    review: list[ReviewModel] = []
    groups: dict[str, list[_Variant]] = {}
    order: list[str] = []
    for v in variants:
        if v.code not in groups:
            groups[v.code] = []
            order.append(v.code)
        groups[v.code].append(v)
    for base in order:
        group = groups[base]
        if len(group) == 1 or group[0].synthesized:
            continue
        used: set[str] = set()
        for v in group:
            rep = rep_value(v.value)
            cand = f"{base}-m{format_number(rep)}" if rep is not None else base
            final = cand
            k = 2
            while final in used:
                final = f"{cand}-c{k}"
                k += 1
            if final != cand:
                review.append(
                    ReviewModel(
                        kind="code_conflict",
                        heading=_fit(final, 255),
                        detail=f"同编号多判据消歧: {cand} → {final} (无法用代表值区分)",
                        hint="人工确认编号后缀后再评审签字",
                    )
                )
            v.code = final
            used.add(final)
    return review


def _clause(
    kind: str, side: str, text: str, value: dict[str, Any] | None, *, source: str, confidence: str = "rule"
) -> ClauseModel:
    """构造 clause (含契约词表校验 + 指纹)。"""
    validate_clause_kind(kind, side, origin=text[:60])
    fp = fingerprints.cond_fingerprint(kind=kind, side=side, value=value, method_ref="")
    return ClauseModel(
        kind=kind,
        text=_fit(text, 2000),
        role=side,  # type: ignore[arg-type]
        value=value,
        source=source,
        confidence=confidence,
        status="approved",
        method_ref="",
        cond_fingerprint=fp,
    )


def _own_clause_text(pl: _RowPlan) -> str:
    n = normalize_text(pl.param.name)
    sub = (pl.param.sub_name or "").split("\n")[0].strip()
    if sub and sub not in ("-", "") and sub not in n:
        return f"{n} [{sub}]"
    return n


def _scenario_name(p: SpecParameter, title: str) -> str:
    for cand in (normalize_text(p.note), normalize_text(p.condition)):
        if cand:
            return _fit(cand, 200)
    return _fit(title, 200)


def _build_requirement(v: _Variant, *, baseline: dict[str, Any] | None) -> RequirementModel:
    """把一个变体装配为 RequirementModel (条件/限值/场景/指纹)。"""
    first = v.plans[0].param
    title = _fit(normalize_text(first.name), 255)
    notes_parts: list[str] = []
    desc_parts: list[str] = []
    for pl in v.plans:
        if pl.param.note and pl.param.note not in notes_parts:
            notes_parts.append(pl.param.note)
        if pl.param.requirement and pl.param.requirement not in desc_parts:
            desc_parts.append(pl.param.requirement)

    input_conditions: list[ClauseModel] = []
    output_conditions: list[ClauseModel] = []
    limits: list[LimitModel] = []

    def add_limit(qualifier: str, value: dict[str, Any] | None) -> None:
        if not value:
            return
        nums = {k: value[k] for k in ("min", "typ", "max") if isinstance(value.get(k), (int, float))}
        if not nums:
            return
        limits.append(
            LimitModel(
                rail=_fit(v.rail, 32),
                qualifier=_fit(qualifier, 64),
                min=nums.get("min"),
                typ=nums.get("typ"),
                max=nums.get("max"),
                unit=str(value.get("unit", ""))[:32],
            )
        )

    sub = (first.sub_name or "").split("\n")[0].strip()
    own_qualifier = ""
    if sub and sub not in ("-", "") and sub != v.rail and sub not in normalize_text(first.name):
        own_qualifier = sub

    for pl in v.plans:
        text = _own_clause_text(pl)
        if pl is v.plans[0] and v.split:
            text = f"{text} (拆分: {'; '.join(lab for lab, _ in v.split)})"
        clause = _clause(pl.match.kind, pl.match.side, text, pl.value, source="spec")
        if pl.match.side == "input":
            input_conditions.append(clause)
        else:
            output_conditions.append(clause)

    own = v.plans[0]
    if own.split is not None:
        for lab, val in own.split:
            add_limit(lab, val)
    elif own.value is not None and own.match.side == "output":
        add_limit(own_qualifier, own.value)

    # 附加动作: 保护行含"短路" → fault_stimulus (destructive, 消费侧自动隔离)。
    extra = patterns.extra_clauses(first)
    for kind, side, value in extra:
        if kind in ("fault_stimulus",):
            text = "原文含\"短路\" → 触发短路事件"
            clause = _clause(kind, side, text, value, source="spec")
            input_conditions.append(clause)

    # 输入侧条件: 原文数值 > 产品基线 (缺失时, notes 留痕)。
    have = {c.kind for c in input_conditions}
    for kind, side, value, why in patterns.note_input_conditions(first, baseline):
        if kind in have:
            continue
        input_conditions.append(_clause(kind, side, why, value, source="notes"))
        have.add(kind)
    if "input_voltage" not in have and baseline is not None:
        input_conditions.append(
            _clause("input_voltage", "input", "产品基线(原文未标注明确输入条件)", dict(baseline), source="notes")
        )
        notes_parts.append("输入条件取自产品基线 (工作范围 + 最大标称档)。")

    scenarios = [
        ScenarioModel(
            scenario_id=_fit(f"{v.code}-S001", 200),
            seq=1,
            name=_scenario_name(first, title),
            rail=_fit(v.rail, 32),
            bindings=patterns.scenario_bindings(first),
            derived={},
            basis=_fit(
                f"{first.table_id}|{first.row_index}: {normalize_text(first.name)}; {normalize_text(first.note)}", 200
            ),
            source="spec",
        )
    ]

    flags: list[str] = []
    if v.synthesized:
        flags.append("synth-code")
    if v.split is not None:
        flags.append("multi-value")
    if v.value is None and v.split is None:
        flags.append("no-value")
    if len(v.plans) > 1:
        flags.append("merged-rows")

    input_fps = [c.cond_fingerprint for c in input_conditions]
    output_fps = [c.cond_fingerprint for c in output_conditions]
    req_fp = fingerprints.req_fingerprint(
        requirement_code=v.code,
        title=title,
        input_fps=input_fps,
        output_fps=output_fps,
        limits=[lim.model_dump() for lim in limits],
    )
    return RequirementModel(
        requirement_code=_fit(v.code, 200),
        spec_requirement_id=_fit(first.code, 100),
        title=title,
        section_path=_fit_section(first.section_path),
        role="",
        description=_fit(" | ".join(desc_parts), 8000),
        notes=_fit("\n".join(notes_parts), 2000),
        req_fingerprint=req_fp,
        flags=flags,
        input_conditions=input_conditions,
        output_conditions=output_conditions,
        limits=limits,
        scenarios=scenarios,
    )


def build_from_tables(params: Sequence[SpecParameter]) -> SpecTableResult:
    """把一份规格书的全部行投影为 requirements/excluded/review/stats (确定性)。"""
    if not params:
        return SpecTableResult()
    baseline = patterns.baseline_input_voltage(params)
    plans, excluded, review = _classify(params)
    variants = _group_variants(plans)
    review.extend(_assign_codes(variants))
    requirements = [_build_requirement(v, baseline=baseline) for v in variants]

    kinds: dict[str, int] = {}
    for req in requirements:
        for c in req.output_conditions:
            kinds[c.kind] = kinds.get(c.kind, 0) + 1
        for c in req.input_conditions:
            kinds[f"{c.kind}@{c.role}"] = kinds.get(f"{c.kind}@{c.role}", 0) + 1
    stats: dict[str, Any] = {
        "rows": len(params),
        "requirements": len(requirements),
        "excluded": len(excluded),
        "review": len(review),
        "kinds": dict(sorted(kinds.items())),
        "baseline_input_voltage": baseline,
    }
    return SpecTableResult(requirements=requirements, excluded=excluded, review=review, stats=stats)
