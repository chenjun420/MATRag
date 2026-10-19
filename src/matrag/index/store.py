"""向量索引腿（P1-4 索引层）: semantica ``SQLiteVecStore`` 封装 + 命中契约。

- 向量腿走 semantica 的 ``SQLiteVecStore``（vec0 虚表，cosine 距离），持久化到
  同一 ``db_path``，重开可搜（实测）；
- 每条 chunk 的完整 meta + ``text`` / ``doc_id`` / ``chunk_id`` 一并写入 vec0 的
  ``metadata`` JSON 列，供 ``where`` 过滤与命中还原；
- ``search_vector`` 的 ``where`` 透传给 ``store.search(filter=...)``——semantica 实测:
  仅支持 ``dict`` 的**等值**过滤（``json_extract(metadata,'$.key') = ?``），键须为
  安全标识符；``None`` / 空 dict 表示不过滤。``section_contains``（章节名/章节号
  **包含**过滤）不在 semantica 能力内，由本类在 Python 侧后过滤实现；
- ``dimension`` 与 ``embed_model`` 记录在 ``stats``；向量维度与构造不一致（add vs
  构造）→ ``RuntimeError``（fail-fast，绝不把错维向量喂进索引）。

与 :class:`~matrag.index.keyword.KeywordIndex` 可共用同一 ``db_path`` SQLite 文件
（不同虚表名，见该模块 docstring）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from semantica.utils.exceptions import SemanticaError
from semantica.vector_store import SQLiteVecStore

from matrag.index.chunking import ChunkRecord

_VEC_TABLE = "chunks_vec"
_META_STRIP_KEYS = frozenset({"text", "doc_id", "chunk_id"})
#: ``section_contains`` 过滤时的过取倍数: 包含过滤在 Python 侧做, 先多取再截断,
#: 避免"等值过滤后剩下的 chunk 恰好都在目标章节外"导致不足 top_k。
SECTION_PREFETCH = 5
#: 型号过滤（保留公共知识）的取回倍数：公共知识占一部分空间，取回太少会衰减能力
SCOPE_PREFETCH = 8
#: 公共知识（域知识卡片）的 meta.scope 值——不绑型号，型号过滤时仍可命中
PUBLIC_SCOPE = "public"

#: 型号过滤的 where 键名（与 matrag.retrieve.query.FILTER_PRODUCT_CODE 一致）
PRODUCT_CODE_KEY = "product_code"

#: ``section_contains`` 的 where 键名（与 matrag.retrieve.query.FILTER_SECTION_CONTAINS 一致）
SECTION_CONTAINS_KEY = "section_contains"


def _section_terms(value: Any) -> list[str]:
    """把 ``section_contains`` 的值规范成字符串列表（空值 → 空列表 = 不过滤）。

    支持 ``str`` 与 ``list[str]``; 其它类型 fail-fast。
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        if not all(isinstance(item, str) for item in value):
            raise ValueError(
                f"{SECTION_CONTAINS_KEY} 只接受 str 或 list[str]，收到: {value!r}"
            )
        return [item for item in value if item]
    raise ValueError(
        f"{SECTION_CONTAINS_KEY} 只接受 str 或 list[str]，收到 {type(value).__name__}"
    )


def _fold(text: str) -> str:
    """轻量规范化: NFKC + 压缩空白 + 小写（与查询侧章节名口径一致）。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text or ""))).strip().lower()


def _section_haystack(meta: dict[str, Any]) -> str:
    """chunk meta 的章节标题串: 优先 ``section_titles``（缺失回退 ``section_path``）。"""
    titles = meta.get("section_titles")
    if isinstance(titles, list) and titles:
        return " > ".join(str(t) for t in titles)
    return str(meta.get("section_path") or "")


def _section_matches(meta: dict[str, Any], terms: list[str]) -> bool:
    """chunk 的章节标题是否包含全部 ``terms``（大小写不敏感，与 SQLite LIKE 同口径）。

    查询侧的章节名已由 :func:`matrag.retrieve.lexicon.normalize_title` 规范化，
    这里对两侧同样做轻量规范化（NFKC + 压缩空白 + 小写）后按子串包含判定。
    """
    if not terms:
        return True
    haystack = _fold(_section_haystack(meta))
    return all(_fold(term) in haystack for term in terms)


@dataclass
class ScoredChunk:
    """一次命中（向量腿 / 关键词腿通用结构，检索层对接契约）。

    ``score`` 统一约定为**越大越相关**：向量腿 = semantica 相似度
    ``1/(1+distance)``，关键词腿 = ``-bm25()``；``leg ∈ {"vector","keyword"}``。
    RRF 融合（检索层）只取排名，两腿分数无需同量纲。
    """

    chunk_id: str
    doc_id: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    leg: str = ""


def _scope_matches(meta: dict[str, Any], product_code: str) -> bool:
    """型号过滤: 本型号规格文档 **或** 公共知识（scope=public）。

    公共知识（域知识卡片/通用工艺经验）不属于任何型号，但对所有型号均成立；
    若跟着型号过滤一起排除，就会让“为什么要用 20MHz 带宽测纹波”这类
    通用经验在带型号的查询中消失——严格的语义错误。
    """
    if str(meta.get("scope") or "") == PUBLIC_SCOPE:
        return True
    return str(meta.get(PRODUCT_CODE_KEY) or "") == product_code


def stored_meta(record: ChunkRecord) -> dict[str, Any]:
    """序列化进 vec0 ``metadata`` 列的完整字典（含 text/doc_id/chunk_id，便于过滤与还原）。"""
    out = dict(record.meta)
    out["doc_id"] = record.doc_id
    out["chunk_id"] = record.chunk_id
    out["text"] = record.text
    return out


class VectorIndex:
    """SQLite（vec0）向量索引腿。构造参数按对接契约固定。"""

    def __init__(self, db_path: str | Path, *, dimension: int, embed_model: str) -> None:
        """打开 / 新建向量库。

        Args:
            db_path: SQLite 文件路径（或 ``":memory:"``）。
            dimension: 向量维度（必须为正整数）。
            embed_model: 生成这些向量所用的 embedding 模型标识（记录进 stats）。
        """
        if dimension <= 0:
            raise RuntimeError(f"dimension 必须为正整数，收到 {dimension}")
        self.db_path = str(db_path)
        self.dimension = int(dimension)
        self.embed_model = embed_model
        self._table = _VEC_TABLE
        self._store = SQLiteVecStore(
            db_path=self.db_path,
            table_name=self._table,
            dimension=self.dimension,
            distance_metric="cosine",
        )

    def add_chunks(self, records: list[ChunkRecord], *, vectors: list[list[float]]) -> None:
        """写入 chunk 及其向量（按 chunk_id upsert）。

        维度不符（与构造的 dimension 不一致）→ ``RuntimeError``（fail-fast）。
        """
        if len(records) != len(vectors):
            raise RuntimeError(
                f"记录数({len(records)})与向量数({len(vectors)})不一致"
            )
        for i, vec in enumerate(vectors):
            if len(vec) != self.dimension:
                raise RuntimeError(
                    f"第 {i} 条向量维度 {len(vec)} 与索引 dimension {self.dimension} 不一致"
                )
        if not records:
            return
        try:
            self._store.add(
                vectors=vectors,
                metadata=[stored_meta(r) for r in records],
                ids=[r.chunk_id for r in records],
            )
        except SemanticaError as exc:
            raise RuntimeError(f"写入向量索引失败: {exc}") from exc

    def search_vector(
        self, vector: list[float], *, top_k: int = 10, where: dict | None = None
    ) -> list[ScoredChunk]:
        """向量相似度检索。

        ``where`` 为 metadata 等值过滤（见模块 docstring）; 另支持
        ``section_contains`` 的**包含**过滤——semantica 只做等值，这里先在 SQL
        层用**其余键**等值过滤、按 ``SECTION_PREFETCH`` 倍放大 ``top_k`` 取回，
        再在 Python 侧按 ``meta["section_titles"]``（缺失回退 ``section_path``）
        做包含过滤并截到 ``top_k``。值为 ``str`` 或 ``list[str]``（list → 全部包含）。
        """
        if len(vector) != self.dimension:
            raise RuntimeError(
                f"查询向量维度 {len(vector)} 与索引 dimension {self.dimension} 不一致"
            )
        if top_k <= 0:
            return []
        terms: list[str] = []
        product_code: str | None = None
        # 复制一份: 下面要从过滤条件里抽键，不能原地改调用方的 dict
        # （否则同一 where 被两腿共享时，向量腿拆键会把关键词腿的条件也拆掉）。
        equality_where: dict[str, Any] | None = dict(where) if where else None
        if where and SECTION_CONTAINS_KEY in where:
            # 先校验再决定是否过滤: 空值 = 不过滤, 非法类型 fail-fast
            terms = _section_terms(where[SECTION_CONTAINS_KEY])
            equality_where = (
                {k: v for k, v in where.items() if k != SECTION_CONTAINS_KEY} or None
            )
        if equality_where and PRODUCT_CODE_KEY in equality_where:
            # 型号等值过滤会误杀公共知识（scope=public 不属于任何型号），
            # 而 semantica 只能做等值。故拆出该键，放大取回后在 Python 侧过滤:
            # “本型号或公共知识”。
            value = equality_where.pop(PRODUCT_CODE_KEY)
            product_code = str(value) if value else None
            equality_where = equality_where or None
        prefetch = SECTION_PREFETCH if terms else SCOPE_PREFETCH
        fetch_k = top_k * prefetch if (terms or product_code) else top_k
        try:
            hits = self._store.search(query_vector=vector, top_k=fetch_k, filter=equality_where)
        except SemanticaError as exc:
            raise RuntimeError(f"向量检索失败: {exc}") from exc
        chunks = [self._to_chunk(hit) for hit in hits]
        if terms:
            chunks = [c for c in chunks if _section_matches(c.meta, terms)]
        if product_code:
            chunks = [c for c in chunks if _scope_matches(c.meta, product_code)]
        return chunks[:top_k]

    @staticmethod
    def _to_chunk(hit: dict[str, Any]) -> ScoredChunk:
        meta = dict(hit.get("metadata") or {})
        return ScoredChunk(
            chunk_id=str(hit.get("id", "")),
            doc_id=str(meta.get("doc_id", "")),
            text=str(meta.get("text", "")),
            meta={k: v for k, v in meta.items() if k not in _META_STRIP_KEYS},
            score=float(hit.get("score", 0.0)),
            leg="vector",
        )

    def count(self) -> int:
        """当前向量条数。"""
        return self._store.count()

    def stats(self) -> dict[str, Any]:
        """索引统计: db_path / table / count / dimension / embed_model / distance_metric。"""
        return {
            "db_path": self.db_path,
            "table": self._table,
            "count": self.count(),
            "dimension": self.dimension,
            "embed_model": self.embed_model,
            "distance_metric": self._store.distance_metric,
        }

    def close(self) -> None:
        """关闭底层连接。"""
        self._store.close()

    def __enter__(self) -> VectorIndex:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
