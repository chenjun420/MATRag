"""确定性规则提示: 把用户问题映射到 power 域规则（130 条之一）作为**解释性附加**。

定位: 检索的"旁挂说明"，不是检索结果——本模块只挑规则、不改排序、不调 LLM。
推理层 (:mod:`matrag.domain.rules`) 负责求值, 这里只负责**选哪几条**。

设计约束:

- **纯确定性打分**: jieba 分词 → 与规则文本做词重叠 + 关键词短语命中,
  同一输入两次运行分数逐位一致; 同分按 ``rule_id`` 稳定排序（可复现）。
- **打分公式**（四项加权, 权重和为 1, 值域 ``[0, 1]``）::

      score = 0.55 * overlap_stmt      # 查询词命中规则 statement 的比例
           + 0.15 * overlap_aux       # 查询词命中 category/expr/inputs 的比例
           + 0.15 * phrase_ratio      # RULE_KEYWORDS 短语直接出现在 statement 里的比例
           + 0.15 * category_match    # 规则 category 落在 RULE_KEYWORDS 给出的提示类里

      overlap_stmt = |Q ∩ T(statement ∪ scope)| / |Q|
      overlap_aux  = |Q ∩ T(category ∪ expr ∪ inputs ∪ source)| / |Q|
      phrase_ratio = min(1, statement 内命中的提示短语数 / PHRASE_SATURATION)
      category_match = 1 if rule.category ∈ 提示类集合 else 0

  其中 ``Q`` 是查询分词去停用词后的词集合, ``T(·)`` 同法分词。分数的每一项
  都会写进 :attr:`RuleHint.score_parts`, 便于对账"为什么是这条规则"。
- **数据驱动**: :data:`RULE_KEYWORDS` 是纯"查询词 → 规则 category"数据表,
  **不含任何 rule_id 分支**; 新增规则/新增问法都只改数据不改代码。
- **fail-fast**: ``rules=None`` 时走 :func:`matrag.domain.rules_loader.load_rules`,
  加载失败直接 ``RuntimeError``（不静默退化成"没有规则"）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from matrag.retrieve.query import STOPWORDS, resolve_tokenizer

if TYPE_CHECKING:  # pragma: no cover —— 仅类型标注, 避免运行期拖入域规则依赖
    from matrag.domain.rules_loader import DomainRule

#: 公式型规则的对外类型标签（``DomainRule.kind`` 为 ``derive``/``constraint``）
KIND_FORMULA = "formula"
KIND_CONSTRAINT = "constraint"
#: 全部合法类型标签
RULE_KINDS: tuple[str, ...] = (KIND_FORMULA, KIND_CONSTRAINT)
#: :data:`DomainRule.kind` → 本模块对外标签的映射（数据表，不按规则分支）
_KIND_BY_DOMAIN: dict[str, str] = {"derive": KIND_FORMULA, "constraint": KIND_CONSTRAINT}

# ---- 打分权重（和为 1，保证 score ∈ [0, 1]）----
_W_OVERLAP_STMT = 0.55
_W_OVERLAP_AUX = 0.15
_W_PHRASE = 0.15
_W_CATEGORY = 0.15
#: statement 内命中多少个提示短语即视为"短语饱和"（phrase_ratio 封顶 1.0）
PHRASE_SATURATION = 2
#: constraint 摘要（expr 字段）的最大长度，超出截断
_CONSTRAINT_SUMMARY_LIMIT = 200

#: 查询词 → 规则 category 提示表。**纯数据表, 不含 rule_id 分支**:
#: 值必须是 rules.yaml 里真实出现的 category, 新增规则只需改这张表。
RULE_KEYWORDS: dict[str, str] = {
    # --- 效率 ---
    "效率": "efficiency",
    "转换效率": "efficiency",
    "空载损耗": "efficiency",
    "损耗": "power",
    # --- 纹波 ---
    "纹波": "ripple",
    "电流纹波": "ripple",
    "纹波电流": "ripple",
    "峰峰值": "ripple",
    "纹波频率": "ripple",
    # --- 保持时间 / 瞬态 ---
    "保持时间": "transient",
    "掉电保持": "transient",
    "瞬态": "transient",
    "瞬态响应": "transient",
    "恢复时间": "transient",
    "启动过冲": "startup",
    "软启动": "startup",
    # --- 浪涌 ---
    "浪涌": "inrush",
    "浪涌电流": "inrush",
    "inrush": "inrush",
    # --- 回差 / 滞回 ---
    "回差": "protection",
    "滞回": "protection",
    "告警阈值": "protection",
    "保护点": "protection",
    "过压保护": "protection",
    "过流保护": "protection",
    "短路保护": "protection",
    "hiccup": "protection",
    "打嗝": "protection",
    # --- 降额 ---
    "降额": "derating",
    "降额曲线": "derating",
    "低线": "derating",
    "高温降额": "derating",
    # --- 精度 ---
    "精度": "accuracy",
    "测量精度": "accuracy",
    "遥测精度": "telemetry",
    "公差": "fixture",
    # --- 温度系数 / 温度 ---
    "温度系数": "thermal",
    "温度": "thermal",
    "高温": "thermal",
    "寿命": "thermal",
    "散热": "thermal",
    # --- 电压调整率 ---
    "调整率": "regulation",
    "稳压": "regulation",
    "线电压": "regulation",
    # --- 均流 ---
    "均流": "current-sharing",
    "并联": "current-sharing",
    "下垂": "current-sharing",
    # --- 功率 / 基础电学 ---
    "功率": "power",
    "功率因数": "ac",
    "视在功率": "ac",
    "有功功率": "ac",
    "无功": "ac",
    "欧姆定律": "electrical",
    "电阻": "electrical",
    "电流": "electrical",
    "压降": "electrical",
    # --- 测量链 ---
    "adc": "measurement",
    "量化": "measurement",
    "采样": "measurement",
    "有效位数": "measurement",
    "enob": "measurement",
    "不确定度": "accuracy",
    "校准": "calibration",
    "标定": "calibration",
    # --- 遥测 / 控制 ---
    "遥测": "telemetry",
    "遥信": "telemetry",
    "ntc": "telemetry",
    "告警": "control",
    "遥控": "control",
    "看门狗": "control",
    "环路": "control",
    "相位裕度": "control",
    "增益裕度": "control",
    # --- 安规 ---
    "绝缘": "safety",
    "耐压": "safety",
    "安规": "safety",
    "接地": "safety",
    "hipot": "safety",
    "漏电流": "safety",
    # --- 变压器 / 整流 ---
    "变压器": "transformer",
    "变比": "transformer",
    "匝比": "transformer",
    "整流": "ac-dc",
    "峰值电压": "ac-dc",
    # --- 限值 / 工装 ---
    "超限": "limits",
    "量程": "limits",
    "规格限值": "limits",
    "公差链": "fixture",
    "探针": "fixture",
    "夹具": "fixture",
    "kelvin": "fixture",
    "四线": "fixture",
    "工装": "fixture",
    "故障注入": "fixture",
    "测试覆盖": "coverage",
    "mtbf": "reliability",
    "可用度": "reliability",
    # --- 词头换算 ---
    "单位换算": "units",
    "词头": "units",
    "千伏": "units",
}

#: SHACL 形状里的消息模板（constraint 型规则的 expr 摘要来源）
_SH_MESSAGE_RE = re.compile(r"sh:message\s+\"((?:[^\"\\]|\\.)*)\"")
_SH_TARGET_RE = re.compile(r"sh:targetClass\s+([^\s;]+)")

#: 纯标点/无内容 token 的剔除（与 :mod:`matrag.retrieve.query` 同口径）
_HAS_CONTENT_RE = re.compile(r"[\w一-鿿]")


@dataclass(frozen=True)
class RuleHint:
    """一条确定性规则提示（解释性附加，不是检索命中）。

    - ``kind``: ``formula``（公式型） / ``constraint``（约束型）;
    - ``expr``: 公式型取 ``derive.expr``，约束型取 SHACL 形状的 ``sh:message`` 摘要;
    - ``inputs``: 规则的输入参数（公式型 ``derive.inputs``，约束型 ``test.given`` 的键）;
    - ``score``: ``[0, 1]`` 的确定性相似度（同分按 ``rule_id`` 稳定排序）;
    - ``matched_terms`` / ``score_parts``: 打分明细，用于对账"为什么是这条规则"。
    """

    rule_id: str
    statement: str
    category: str
    kind: str
    expr: str
    inputs: list[str]
    source: str
    score: float
    matched_terms: tuple[str, ...] = ()
    score_parts: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化 dict（报告/CLI 用）。"""
        return {
            "rule_id": self.rule_id,
            "statement": self.statement,
            "category": self.category,
            "kind": self.kind,
            "expr": self.expr,
            "inputs": list(self.inputs),
            "source": self.source,
            "score": self.score,
            "matched_terms": list(self.matched_terms),
            "score_parts": dict(self.score_parts),
        }


def _tokens(text: str) -> set[str]:
    """分词去停用词的小写词集合（纯函数，确定性）。"""
    if not text:
        return set()
    out: set[str] = set()
    for raw in resolve_tokenizer()(text):
        tok = raw.strip().lower()
        if not tok or not _HAS_CONTENT_RE.search(tok):
            continue
        if tok in STOPWORDS:
            continue
        out.add(tok)
    return out


def _constraint_summary(shape_text: str) -> str:
    """约束型规则的 expr 摘要: 取 ``sh:message`` 模板（无则取 targetClass）。"""
    collapsed = " ".join(shape_text.split())
    match = _SH_MESSAGE_RE.search(collapsed)
    if match:
        summary = f"约束: {match.group(1)}"
    else:
        target = _SH_TARGET_RE.search(collapsed)
        summary = f"约束: SHACL NodeShape (targetClass={target.group(1) if target else '未知'})"
    if len(summary) > _CONSTRAINT_SUMMARY_LIMIT:
        summary = summary[: _CONSTRAINT_SUMMARY_LIMIT - 1] + "…"
    return summary


def _rule_kind(rule: DomainRule) -> str:
    """``DomainRule.kind``（``derive``/``constraint``）→ 对外标签。"""
    return _KIND_BY_DOMAIN.get(rule.kind, rule.kind)


def _rule_expr(rule: DomainRule) -> str:
    """规则的 expr 字段: 公式型取表达式，约束型取 SHACL 摘要。"""
    if rule.is_derive:
        return str((rule.derive or {}).get("expr", ""))
    return _constraint_summary(str((rule.constraint or {}).get("shape", "")))


def _rule_source(rule: DomainRule) -> str:
    """规则出处（标准名/来源文档名），缺省时回退 rule_id。"""
    name = str((rule.source or {}).get("name") or "").strip()
    return name or rule.rule_id


def _rule_source_text(rule: DomainRule) -> str:
    """参与打分的出处文本。

    **只用真实来源名**（标准名/来源文档名）；缺省时返回空串——rule_id 是规则
    编号（``K-PWR-101``），分词后是 ``k``/``pwr``/``101`` 这类噪声，进打分只会
    制造假命中（实测会把 "zzz" 里的片段撞上规则号）。
    """
    return str((rule.source or {}).get("name") or "").strip()


def score_rule(
    query: str,
    rule: DomainRule,
    *,
    keywords: dict[str, str] | None = None,
    query_tokens: set[str] | None = None,
) -> tuple[float, dict[str, float], tuple[str, ...]]:
    """单条规则对查询的打分（纯确定性，可单测对账）。

    Returns:
        ``(score, score_parts, matched_terms)``；``score_parts`` 给出四项
        加权明细, ``matched_terms`` 是命中规则文本的查询词（已排序）。
    """
    table = RULE_KEYWORDS if keywords is None else keywords
    text = (query or "").strip().lower()
    tokens = _tokens(text) if query_tokens is None else query_tokens
    if not tokens:
        return 0.0, {"overlap_stmt": 0.0, "overlap_aux": 0.0, "phrase": 0.0, "category": 0.0}, ()

    statement = f"{rule.statement} {rule.scope}".strip()
    expr = _rule_expr(rule)
    inputs = list(rule.input_params)
    aux = " ".join([rule.category, expr, " ".join(inputs), _rule_source_text(rule)])

    stmt_tokens = _tokens(statement)
    aux_tokens = _tokens(aux)

    matched = tuple(sorted(tokens & (stmt_tokens | aux_tokens)))
    overlap_stmt = len(tokens & stmt_tokens) / len(tokens)
    overlap_aux = len(tokens & aux_tokens) / len(tokens)

    # 关键词短语命中: 用子串匹配（中文分词会把"电流纹波"拆开, 子串更稳）
    hints = [kw for kw in table if kw in text]
    hinted_categories = {table[kw] for kw in hints}
    phrase_hits = [kw for kw in hints if kw in statement.lower()]
    phrase_ratio = min(1.0, len(phrase_hits) / PHRASE_SATURATION)
    category_match = 1.0 if rule.category in hinted_categories else 0.0

    score = (
        _W_OVERLAP_STMT * overlap_stmt
        + _W_OVERLAP_AUX * overlap_aux
        + _W_PHRASE * phrase_ratio
        + _W_CATEGORY * category_match
    )
    parts = {
        "overlap_stmt": round(overlap_stmt, 6),
        "overlap_aux": round(overlap_aux, 6),
        "phrase": round(phrase_ratio, 6),
        "category": category_match,
    }
    return min(1.0, score), parts, matched


def select_rule_hints(
    query: str,
    *,
    top_n: int = 5,
    rules: Sequence[DomainRule] | None = None,
    kinds: tuple[str, ...] = (KIND_FORMULA, KIND_CONSTRAINT),
) -> list[RuleHint]:
    """按查询挑出确定性规则提示（不调 LLM、不改检索排序）。

    Args:
        query: 用户原始问题。
        top_n: 最多返回几条（``>= 1`` 的整数）。
        rules: 规则列表; ``None`` 时用 :func:`matrag.domain.rules_loader.load_rules`
            现加载（失败 → ``RuntimeError``, 不静默空）。
        kinds: 参与选择的类型标签（``formula`` / ``constraint``）。

    Returns:
        按 ``(-score, rule_id)`` 稳定排序的 :class:`RuleHint`; 无任何词重叠且
        无关键词类别命中的规则不入选, 因此"未命中"时返回空列表（不抛）。
    """
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 1:
        raise ValueError(f"top_n 必须 >= 1 的整数: {top_n!r}")
    wanted = tuple(kinds)
    unknown = [kind for kind in wanted if kind not in RULE_KINDS]
    if unknown:
        raise ValueError(f"未知规则类型标签: {unknown}（合法值: {list(RULE_KINDS)}）")

    if rules is None:
        from matrag.domain.rules_loader import load_rules

        try:
            selected = load_rules()
        except Exception as exc:  # noqa: BLE001 —— 规则源不可用必须显式失败, 不静默返回空
            raise RuntimeError(
                f"加载域规则失败, 规则提示不可用: {type(exc).__name__}: {exc}"
            ) from exc
    else:
        selected = list(rules)
    if not wanted:
        return []

    text = (query or "").strip()
    tokens = _tokens(text.lower())
    hints: list[RuleHint] = []
    for rule in selected:
        kind = _rule_kind(rule)
        if kind not in wanted:
            continue
        score, parts, matched = score_rule(text, rule, query_tokens=tokens)
        if score <= 0.0:
            continue  # 零分规则不作为提示返回（"没命中"是正常状态）
        hints.append(
            RuleHint(
                rule_id=rule.rule_id,
                statement=rule.statement,
                category=rule.category,
                kind=kind,
                expr=_rule_expr(rule),
                inputs=list(rule.input_params),
                source=_rule_source(rule),
                score=score,
                matched_terms=matched,
                score_parts=parts,
            )
        )
    hints.sort(key=lambda hint: (-hint.score, hint.rule_id))
    return hints[:top_n]


def rule_hint_summary(query: str, *, top_n: int = 5) -> dict[str, Any]:
    """规则提示摘要（报告/CLI 用）; 规则源不可用时显式标注原因, 不吞错。"""
    try:
        hints = select_rule_hints(query, top_n=top_n)
    except RuntimeError as exc:
        return {"query": query, "available": False, "error": str(exc), "hints": []}
    return {
        "query": query,
        "available": True,
        "count": len(hints),
        "hints": [hint.to_dict() for hint in hints],
    }
