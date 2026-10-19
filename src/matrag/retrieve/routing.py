"""问题类型路由: 决定"这是什么问题、该用哪几条腿去检索"。

定位: 混合检索的第二棒，夹在查询理解与融合检索之间。**确定性规则优先**:
关键词命中表驱动 verdict，LLM 只在全部关键词落空（``UNKNOWN``）时补位，
且调用方不显式传 ``llm`` 时绝不动 LLM。

路由规则表（模块级常量，可测试可维护）::

    QueryKind      keywords 常量               legs
    ----------     ------------------------    ------------------------
    SPEC_VALUE     SPEC_VALUE_KEYWORDS         ("vector", "keyword")
    PROCEDURE      PROCEDURE_KEYWORDS          ("vector", "keyword")
    FORMULA        FORMULA_KEYWORDS            ("vector", "keyword", "graph")
    FAULT          FAULT_KEYWORDS              ("vector", "keyword")
    CATALOG        CATALOG_KEYWORDS            ("vector", "keyword")
    UNKNOWN        -                           ("vector", "keyword")

``graph`` 腿为 P1-5b（知识图谱检索）预留: 路由层会给出三层腿，检索层在
图索引未接线前会跳过该腿并留痕（见 ``matrag.retrieve.retriever``）。

不出现任何具体型号/规格值硬编码分支——规则只描述"问题的问法"。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from matrag.retrieve.query import (
    FILTER_DOC_ID,
    FILTER_PRODUCT_CODE,
    FILTER_SECTION,
    FILTER_SECTION_CONTAINS,
    FILTER_TABLE,
    TABLE_ID_SHAPE,
    ParsedQuery,
    load_model_aliases,
)

# ---- 腿名常量（与 retriever 共用，避免散落字符串）----
LEG_VECTOR = "vector"
LEG_KEYWORD = "keyword"
LEG_GRAPH = "graph"
DEFAULT_LEGS: tuple[str, ...] = (LEG_VECTOR, LEG_KEYWORD)
GRAPH_LEGS: tuple[str, ...] = (LEG_VECTOR, LEG_KEYWORD, LEG_GRAPH)


class QueryKind(StrEnum):
    """问题类型。取值即小写英文标签，可直接进报告/日志。"""

    SPEC_VALUE = "spec_value"  # 规格值/限值/参数是多少
    PROCEDURE = "procedure"    # 怎么测/怎么做/步骤/方法
    FORMULA = "formula"        # 公式/计算/推导/换算
    FAULT = "fault"            # 故障/不良/异常
    CATALOG = "catalog"        # 有哪些/清单/目录
    UNKNOWN = "unknown"        # 一个关键词都没命中


# ---- 关键词表（问法而非业务: 故意不含任何型号/规格值字面量）----
SPEC_VALUE_KEYWORDS: frozenset[str] = frozenset(
    """
    是多少 多大 多少 规格 参数 指标 限值 范围 额定 典型值 最小值 最大值
    电压 电流 功率 效率 温度 频率 纹波 精度 容差 负载率 保持时间 漏电流
    """.split()
)

PROCEDURE_KEYWORDS: frozenset[str] = frozenset(
    """
    怎么测 如何测 测试方法 测量方法 步骤 操作 流程 怎么做 如何做 怎样做
    试验 检验 检测 接线 上电 老化 校准 调试 执行 操作步骤 试验方法
    """.split()
)

FORMULA_KEYWORDS: frozenset[str] = frozenset(
    """
    公式 计算 推导 换算 求值 表达式 定律 怎么算 如何算 怎样算 算出 求出
    相乘 相除 代入 平方 根号 正比 反比 推导过程
    """.split()
)

FAULT_KEYWORDS: frozenset[str] = frozenset(
    """
    故障 不良 异常 失效 烧 冒烟 不工作 无输出 不开机 报警 重启 死机
    保护动作 过压 欠压 过流 短路 过热 排查 原因分析 失效模式 告警
    """.split()
)

CATALOG_KEYWORDS: frozenset[str] = frozenset(
    """
    有哪些 列表 目录 清单 都包括 都包含 分类 汇总 列举 有几种 有几个
    系列 全部型号 所有型号 一览 包括哪些 包含哪些
    """.split()
)

# 判定优先级: 等多类同时命中时取最靠前的（越靠前意图越具体）。
_KIND_PRIORITY: tuple[tuple[QueryKind, frozenset[str]], ...] = (
    (QueryKind.FAULT, FAULT_KEYWORDS),
    (QueryKind.PROCEDURE, PROCEDURE_KEYWORDS),
    (QueryKind.FORMULA, FORMULA_KEYWORDS),
    (QueryKind.CATALOG, CATALOG_KEYWORDS),
    (QueryKind.SPEC_VALUE, SPEC_VALUE_KEYWORDS),
)

_KIND_LEGS: dict[QueryKind, tuple[str, ...]] = {
    QueryKind.SPEC_VALUE: DEFAULT_LEGS,
    QueryKind.PROCEDURE: DEFAULT_LEGS,
    QueryKind.FORMULA: GRAPH_LEGS,
    QueryKind.FAULT: DEFAULT_LEGS,
    QueryKind.CATALOG: DEFAULT_LEGS,
    QueryKind.UNKNOWN: DEFAULT_LEGS,
}

# kind → 优先级序号（0 最高）; 等命中数时取序号小的（意图更具体）。
_PRIORITY_INDEX: dict[QueryKind, int] = {
    kind: index for index, (kind, _) in enumerate(_KIND_PRIORITY)
}

# LLM 补位可选的 kind 标签（与 QueryKind 值一致，防止 LLM 编造）。
_LLM_KINDS: frozenset[str] = frozenset(k.value for k in QueryKind if k is not QueryKind.UNKNOWN)


@dataclass
class RoutedQuery:
    """路由结果。

    - ``kind``: 问题类型;
    - ``legs``: 建议检索腿（调用方显式传 ``legs`` 时以调用方为准）;
    - ``confidence``: 置信度（0~1，命中数/歧义度驱动）;
    - ``reason``: 人读判定依据（命中哪些关键词、为何选这一类）。
    """

    kind: QueryKind
    legs: tuple[str, ...]
    confidence: float
    reason: str


def _match_kinds(text: str) -> dict[QueryKind, list[str]]:
    """统计每类关键词的命中项（按关键词原词返回，供 reason 展示）。"""
    lowered = text.lower()
    matched: dict[QueryKind, list[str]] = {}
    for kind, keywords in _KIND_PRIORITY:
        hits = sorted(kw for kw in keywords if kw in lowered)
        if hits:
            matched[kind] = hits
    return matched


def _llm_route(query: str, llm: Any) -> tuple[QueryKind | None, str]:
    """LLM 补位路由: 只在确定性规则全部落空时调用。失败返回 ``(None, 原因)``。"""
    try:
        try:
            data = llm.complete_json(
                "你是检索路由器。判断问题类型，只输出 JSON，不要解释。",
                f"问题: {query}",
                schema_hint='{"kind": "spec_value|procedure|formula|fault|catalog"}',
            )
        except TypeError:  # 替身签名更窄
            data = llm.complete_json("判断问题类型", f"问题: {query}")
    except Exception as exc:  # noqa: BLE001 —— 可选增强，失败留痕不抛
        return None, f"LLM 路由失败: {type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return None, "LLM 路由未返回 JSON 对象"
    kind_raw = str(data.get("kind", "")).strip().lower()
    if kind_raw not in _LLM_KINDS:
        return None, f"LLM 路由返回未知类型: {kind_raw!r}"
    return QueryKind(kind_raw), "LLM 补位命中"


def route(query: str, *, llm: Any = None) -> RoutedQuery:
    """判定问题类型并给出检索腿。

    Args:
        query: 查询文本（通常传 ``ParsedQuery.raw``）。
        llm: 可选 LLM 门面。**默认 ``None``**; 传入后仅在关键词全部落空
            （``UNKNOWN``）时补判一次。

    Returns:
        :class:`RoutedQuery`。``UNKNOWN`` 不抛错，退化为默认两腿并如实记录
        低置信度。
    """
    text = (query or "").strip()
    matched = _match_kinds(text)

    if not matched:
        if llm is not None:
            llm_kind, note = _llm_route(text, llm)
            if llm_kind is not None:
                return RoutedQuery(
                    kind=llm_kind,
                    legs=_KIND_LEGS[llm_kind],
                    confidence=0.6,
                    reason=f"关键词未命中（{note}）",
                )
            reason = f"关键词未命中，LLM 未补位（{note}），退回默认两腿"
        else:
            reason = "关键词未命中，退回默认两腿"
        return RoutedQuery(
            kind=QueryKind.UNKNOWN,
            legs=DEFAULT_LEGS,
            confidence=0.0,
            reason=reason,
        )

    best_kind, best_hits = max(
        matched.items(),
        key=lambda kv: (len(kv[1]), -_PRIORITY_INDEX[kv[0]]),
    )
    others = {kind: hits for kind, hits in matched.items() if kind is not best_kind}
    n_kinds = len(matched)

    confidence = min(1.0, 0.55 + 0.2 * (len(best_hits) - 1))
    if n_kinds == 1:
        confidence = min(1.0, confidence + 0.1)  # 单一意图，加分

    reason = f"命中 {best_kind.value} 关键词 {best_hits}"
    if others:
        reason += f"；另有 {sorted(k.value for k in others)} 意图（优先级较低）"
    return RoutedQuery(
        kind=best_kind,
        legs=_KIND_LEGS[best_kind],
        confidence=confidence,
        reason=reason,
    )


def build_where(parsed: ParsedQuery, *, notes: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """把解析出的过滤条件转成 index 的 ``where``（P1-4 口径）。

    两条腿的等值 / 过滤能力:

    - 向量腿: ``json_extract(metadata, '$.key') = ?``（``doc_id`` 在 metadata 里）;
    - 关键词腿: 白名单列 ``{doc_id, kind, table_id, section_path, caption, page}``,
      给了白名单外的键会直接 ``ValueError``。

    章节类条件（章节号 / 章节名）都无法**等值**表达（``section_path`` 是完整路径
    "4 > 4.3 > 4.3.1 输入特性"），两条腿都另支持 ``section_contains`` 的**包含**
    过滤（关键词腿 ``section_path LIKE '%值%'``; 向量腿按 ``meta.section_titles``
    后过滤）。因此这里把两者都转成 ``where["section_contains"]``:

    - ``FILTER_SECTION``（章节号，如 "4.3.2"）→ ``section_contains``（比章节名
      更精确，同时出现时优先）;
    - ``FILTER_SECTION_CONTAINS``（章节名，如 "输入特性"，查询侧键名与 where
      键名一致）→ 原样进 ``where``。

    其余条件:

    - ``doc_id`` / ``product_code``（型号）→ 直传;
    - ``table`` → 仅当值已是索引口径（``t<页>-<序号>``）时转成 ``table_id``。

    被丢弃 / 被合并的条件写进 ``notes``（供检索层计入 ``degradations``）——满足
    "降级必须留痕"，调用方不关心时传 ``None`` 即可安静返回。

    Args:
        parsed: 解析后的查询。
        notes: 可选的降级记录列表（原址追加 ``{"kind": "where_key_dropped", ...}``
            或 ``{"kind": "where_key_merged", ...}``）。

    Returns:
        where 条件 dict; 无可表达条件时 ``None``（让 index 走全库检索）。
    """
    where: dict[str, Any] = {}
    section_title: Any = None
    for key, value in parsed.filters.items():
        if key in (FILTER_DOC_ID, FILTER_PRODUCT_CODE) and value:
            where[key] = value
        elif key == FILTER_TABLE and isinstance(value, str) and TABLE_ID_SHAPE.match(value):
            where["table_id"] = value
        elif key == FILTER_SECTION and value:
            # 章节号: 等值匹配不上完整 section_path → 转成包含过滤（不再丢弃）
            where[FILTER_SECTION_CONTAINS] = value
        elif key == FILTER_SECTION_CONTAINS:
            section_title = value  # 章节号更精确，等循环结束后再决定是否采用
        else:
            note = {
                "kind": "where_key_dropped",
                "key": key,
                "value": value,
                "detail": "该查询侧条件无法表达为 where 键（已保留在查询文本中）",
            }
            if notes is not None:
                notes.append(note)
    if section_title is not None and FILTER_SECTION_CONTAINS not in where:
        where[FILTER_SECTION_CONTAINS] = section_title
    elif section_title is not None and notes is not None:
        # 同时给了章节号与章节名: 章节号更精确，章节名条件已被它承载
        notes.append({
            "kind": "where_key_merged",
            "key": FILTER_SECTION_CONTAINS,
            "value": section_title,
            "detail": "查询同时含章节号与章节名，章节号更精确；章节名条件由 where.section_contains（章节号）承载",
        })
    return where or None


def kind_legs(kind: QueryKind) -> tuple[str, ...]:
    """某类问题的检索腿（表驱动，供检索层复用 :data:`_KIND_LEGS`）。"""
    return _KIND_LEGS[kind]


def routing_rules() -> list[dict[str, Any]]:
    """导出路由规则表（CLI/报告/测试用，避免规则只能靠读代码还原）。"""
    return [
        {
            "kind": kind,
            "keywords": sorted(keywords),
            "legs": list(_KIND_LEGS[kind]),
            "priority": index,
        }
        for index, (kind, keywords) in enumerate(_KIND_PRIORITY)
    ]


def alias_summary() -> dict[str, str]:
    """当前生效的型号别名表（路由/检索排查用）——缺失即 ``{}``。"""
    return load_model_aliases()
