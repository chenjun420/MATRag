"""知识图谱检索腿（P1-5b 落地）: 查询 → 术语/公理/产测方法实体链接 → 源知识卡 chunk。

本模块实现 ``HybridRetriever`` 的第三条腿（:data:`matrag.retrieve.routing.LEG_GRAPH`）:
把查询文本链接到域知识图谱（:mod:`matrag.domain.knowledge_graph`）里的术语
（``term``）/ 公理（``axiom``）/ 产测方法（``method``）节点, 再沿节点的 ``source``
展开其来源知识卡（``power-knowledge-N`` / ``power-method-*``）的 chunk,
按链接质量排序去重后返回。

口径与约束:

- 匹配规则与 :mod:`matrag.domain.knowledge_graph` **完全一致**: 复用其
  ``_contains_hit`` 边界检查（纯 ASCII 字母数字键要求词边界, 中文/含连接符键
  直接包含）, 保证"参数实例 → 术语"与"查询 → 术语"两处口径不漂移;
- 精确匹配（查询全串 == 某个名称）优先于包含匹配; 包含匹配对每个实体取**最长**
  命中键; 链接按（精确 > 包含, 键长降序, kind, node_id）稳定排序;
- 单字符**非 ASCII** 键不参与包含匹配（如术语表里的 "电" → T-EFFICIENCY:
  长查询里几乎必为伪命中）; 单字符 ASCII 字母数字键（``d``/``P``/``Q`` 等）
  保留 —— 边界规则已能挡住 "reset" 之类的词内命中;
- 只展开 ``source.kind == "card"`` 的实体（源卡为公共知识）; 其它来源
  （``spec_table`` / ``standard`` / ``websearch``）不展开;
- ``where`` 有意忽略: 源卡是公共知识（``scope=public``）, 不绑型号也不属于
  规格书章节, 与索引层"型号过滤保留 public"的语义一致;
- 无实体命中 / 实体无卡片源 → 空列表（不抛）; ``score`` 记 ``1/名次``
  （仅作观测 —— RRF 融合只用名次）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from matrag.domain.knowledge_graph import (
    KIND_AXIOM,
    KIND_METHOD,
    KIND_TERM,
    KnowledgeGraph,
    build_knowledge_graph,
    contains_hit,
)
from matrag.domain.vocab import normalize_name
from matrag.index.store import ScoredChunk
from matrag.retrieve.routing import LEG_GRAPH

__all__ = ["EntityLink", "GraphIndex", "build_graph_index"]

#: 匹配方式取值（与 knowledge_graph 的 denotes 边口径一致）。
_MATCH_EXACT = "exact"
_MATCH_CONTAINS = "contains"

#: 可展开来源的 ``source.kind`` 值（与 terms.SOURCE_KINDS 约定一致）。
_SOURCE_KIND_CARD = "card"


@dataclass(frozen=True)
class EntityLink:
    """一次"查询 → 实体"链接（审计/解释用）。"""

    entity_id: str
    """图谱 node_id（``term:T-RIPPLE`` / ``axiom:A-OHM`` / ``method:M-RIPPLE-NOISE``）。"""

    kind: str
    """实体类型（``KIND_TERM`` / ``KIND_AXIOM`` / ``KIND_METHOD``）。"""

    method: str
    """``"exact"`` 或 ``"contains"``。"""

    key: str
    """命中的归一化名称（exact 时 = 归一化查询全串）。"""

    card_id: str | None
    """源知识卡 doc_id（``power-knowledge-N``）; 非卡来源为 ``None``。"""

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict。"""
        return {
            "entity_id": self.entity_id,
            "kind": self.kind,
            "method": self.method,
            "key": self.key,
            "card_id": self.card_id,
        }


@dataclass(frozen=True)
class _Entity:
    """一条可参与链接的实体（从图谱节点抽出, 构建期一次性结算）。"""

    node_id: str
    kind: str
    names: tuple[str, ...]
    card_id: str | None


def _card_id_of(source: Any) -> str | None:
    """从节点的 ``source`` 字段取知识卡 doc_id; 非卡来源返回 ``None``。"""
    if not isinstance(source, Mapping):
        return None
    if str(source.get("kind", "")).strip().lower() != _SOURCE_KIND_CARD:
        return None
    card_id = str(source.get("card_id", "") or "").strip()
    return card_id or None


def _collect_entities(graph: KnowledgeGraph) -> list[_Entity]:
    """从图谱取全部术语/公理/产测方法节点（含可检索名称与源卡 id, 按 node_id 稳定序）。"""
    entities: list[_Entity] = []
    for node in (
        *graph.nodes(KIND_TERM),
        *graph.nodes(KIND_AXIOM),
        *graph.nodes(KIND_METHOD),
    ):
        attrs = node.attrs if isinstance(node.attrs, Mapping) else {}
        names: list[str] = []
        for name in (attrs.get("zh"), attrs.get("en"), attrs.get("abbr")):
            if isinstance(name, str) and name.strip():
                names.append(name)
        aliases = attrs.get("aliases")
        if isinstance(aliases, (list, tuple)):
            names.extend(a for a in aliases if isinstance(a, str) and a.strip())
        entities.append(
            _Entity(
                node_id=node.node_id,
                kind=node.kind,
                names=tuple(names),
                card_id=_card_id_of(attrs.get("source")),
            )
        )
    return entities


def _linkable_key(key: str) -> bool:
    """包含匹配可用的键: 长度 >= 2, 或单字符 ASCII 字母数字（有边界规则兜底）。"""
    if len(key) >= 2:
        return True
    return len(key) == 1 and key.isascii() and key.isalnum()


class GraphIndex:
    """图谱检索腿: 实体链接 → 源卡 chunk 展开（``HybridRetriever`` 契约实现）。"""

    def __init__(
        self,
        graph: KnowledgeGraph,
        chunk_source: Callable[[str], Sequence[ScoredChunk]],
    ) -> None:
        """构造图谱腿。

        Args:
            graph: 域知识图谱（:func:`build_knowledge_graph` 或手动构造）。
            chunk_source: ``doc_id → chunk 列表`` 的取数函数（典型为
                :meth:`matrag.index.keyword.KeywordIndex.chunks_by_doc`）;
                返回序即文档序。

        Raises:
            ValueError: ``chunk_source`` 不可调用。
        """
        if not callable(chunk_source):
            raise ValueError("chunk_source 必须可调用（doc_id → chunk 列表）")
        self._graph = graph
        self._chunk_source = chunk_source
        self._entities: tuple[_Entity, ...] = tuple(_collect_entities(graph))

        exact: dict[str, list[tuple[str, str]]] = {}
        keys: set[tuple[str, str, str]] = set()
        cards: dict[str, str | None] = {}
        for entity in self._entities:
            cards[entity.node_id] = entity.card_id
            for name in entity.names:
                normalized = normalize_name(name)
                if not normalized:
                    continue
                entry = (entity.kind, entity.node_id)
                bucket = exact.setdefault(normalized, [])
                if entry not in bucket:
                    bucket.append(entry)
                if _linkable_key(normalized):
                    keys.add((normalized, entity.kind, entity.node_id))
        self._exact: dict[str, tuple[tuple[str, str], ...]] = {
            key: tuple(value) for key, value in exact.items()
        }
        # (键长降序, 键, kind, node_id): 遍历时每个实体第一次命中即为最长键
        self._keys: tuple[tuple[str, str, str], ...] = tuple(
            sorted(keys, key=lambda item: (-len(item[0]), item[0], item[1], item[2]))
        )
        self._cards = cards
        self._last_links: tuple[EntityLink, ...] = ()

    @property
    def graph(self) -> KnowledgeGraph:
        """底层知识图谱（只读观测用）。"""
        return self._graph

    @property
    def last_links(self) -> tuple[EntityLink, ...]:
        """最近一次 :meth:`search_graph` 的实体链接（观测/审计用）。"""
        return self._last_links

    # ------------------------------------------------------------------ #
    # 实体链接
    # ------------------------------------------------------------------ #

    def link(self, query: str) -> list[EntityLink]:
        """查询文本 → 实体链接列表（精确优先; 每实体最长键; 稳定排序）。"""
        normalized = normalize_name(query)
        if not normalized:
            return []
        best: dict[str, EntityLink] = {}
        for key, kind, node_id in self._keys:
            if node_id in best:
                continue  # 已拿到该实体的更长键（_keys 按键长降序）
            if contains_hit(normalized, key):
                best[node_id] = EntityLink(
                    entity_id=node_id,
                    kind=kind,
                    method=_MATCH_CONTAINS,
                    key=key,
                    card_id=self._cards.get(node_id),
                )
        for kind, node_id in self._exact.get(normalized, ()):
            best[node_id] = EntityLink(
                entity_id=node_id,
                kind=kind,
                method=_MATCH_EXACT,
                key=normalized,
                card_id=self._cards.get(node_id),
            )
        return sorted(
            best.values(),
            key=lambda link: (
                0 if link.method == _MATCH_EXACT else 1,
                -len(link.key),
                link.kind,
                link.entity_id,
            ),
        )

    # ------------------------------------------------------------------ #
    # 检索腿入口
    # ------------------------------------------------------------------ #

    def search_graph(
        self,
        query: str,
        *,
        top_k: int = 10,
        where: Mapping[str, Any] | None = None,
    ) -> list[ScoredChunk]:
        """实体链接 → 源卡 chunk 展开（去重, 截断 ``top_k``）。

        ``where`` 有意忽略（源卡为公共知识, 见模块 docstring）。
        """
        if int(top_k) <= 0:
            return []
        links = self.link(query)
        self._last_links = tuple(links)
        chunks: list[ScoredChunk] = []
        seen: set[str] = set()
        for link in links:
            if not link.card_id:
                continue
            for raw in self._chunk_source(link.card_id):
                chunk_id = str(getattr(raw, "chunk_id", "") or "")
                if not chunk_id:
                    raise ValueError(f"chunk_source 返回的 chunk 缺少 chunk_id: {raw!r}")
                if chunk_id in seen:
                    continue
                seen.add(chunk_id)
                rank = len(chunks) + 1
                chunks.append(
                    ScoredChunk(
                        chunk_id=chunk_id,
                        doc_id=str(getattr(raw, "doc_id", "") or ""),
                        text=str(getattr(raw, "text", "") or ""),
                        meta=dict(getattr(raw, "meta", {}) or {}),
                        score=1.0 / rank,
                        leg=LEG_GRAPH,
                    )
                )
                if len(chunks) >= int(top_k):
                    return chunks
        return chunks


def build_graph_index(
    chunk_source: Callable[[str], Sequence[ScoredChunk]],
    *,
    graph: KnowledgeGraph | None = None,
) -> GraphIndex:
    """便捷工厂: ``graph=None`` 时现建知识图谱（默认数据源, ≈1 秒）。

    构建中的任何异常都**原样上抛** —— 由调用方决定留痕策略:
    ``scripts.ask`` / 网关记 ``leg_not_wired`` 继续两腿工作; 评测脚本 fail-closed
    （见到显式 ``--no-graph`` 才跑两腿对照）。
    """
    kg = build_knowledge_graph() if graph is None else graph
    return GraphIndex(kg, chunk_source)
