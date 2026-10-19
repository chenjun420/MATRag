"""关键词索引腿（P1-4 索引层）: SQLite FTS5 + jieba 分词 + ``bm25()`` 排序。

- 同一 SQLite 文件内建一张 FTS5 虚表（``chunks_fts``），与向量腿
  :class:`~matrag.index.store.VectorIndex`（vec0 虚表 ``chunks_vec``）可**同库共存**
  —— 只是同一库里的两张虚表，实测读写互不干扰；
- 索引 / 查询都走 jieba 精确模式分词，词间以空格连接；FTS5 默认 ``unicode61``
  分词器把每个 jieba 词当作一个 term（实测 CJK 词**不会**被拆成单字）；
- 查询串以 ``AND`` 连接各词（各词均需出现、允许不相邻，实测语义明确），空查询 /
  无结果 → 空列表，不抛；
- 排序约定: FTS5 ``bm25()`` 返回**负分、越相关越负**（实测）。本腿
  ``score = -bm25()``，即**越大越相关**，与向量腿同向；SQL 层
  ``ORDER BY bm25() ASC``（等价按 score 降序）取最相关；
- 若本环境 sqlite 不支持 FTS5 → 构造时 ``RuntimeError``（fail-fast，绝不静默退化）；
- ``where`` 过滤（``doc_id`` / ``kind`` / ``table_id`` / ``section_path`` / ``caption``
  / ``page`` 等值）在 SQL 层做，仅支持这些已知列，未知键 → ``ValueError``;
  另支持 ``section_contains`` 的**包含**过滤（``section_path LIKE '%值%'``，
  见 :meth:`KeywordIndex.search_keyword`）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import jieba

from matrag.index.chunking import ChunkRecord
from matrag.index.store import ScoredChunk

jieba.setLogLevel(60)  # 关闭 jieba 字典加载噪声日志

_FTS_TABLE = "chunks_fts"

# where 可过滤键 → FTS5 UNINDEXED 列名
_WHERE_COLUMNS = {
    "doc_id": "doc_id",
    "kind": "kind",
    "table_id": "table_id",
    "section_path": "section_path",
    "caption": "caption",
    "page": "page",
    # 型号键: doc_id 的第一段（"PA601-D54A 定制电源技术规格书" -> "PA601-D54A"），
    # 检索层按型号过滤时用它等值匹配（doc_id 带后缀不能直接等值）。
    "product_code": "json_extract(meta_json, '$.product_code')",
}

# 极简停用词（中文常见虚词 + 英文功能词），命中即弃
_STOPWORDS = frozenset({
    "的", "了", "和", "是", "在", "与", "及", "等", "对", "为", "有", "并",
    "或", "而", "把", "被", "从", "到", "于", "以", "其", "该", "这", "那",
    "一个", "一般", "可以",
    "the", "a", "an", "of", "to", "and", "or", "is",
})

#: ``section_contains`` 的 where 键名（与 matrag.retrieve.query.FILTER_SECTION_CONTAINS 一致）
SECTION_CONTAINS_KEY = "section_contains"


def _like_escape(value: str) -> str:
    """转义 SQL ``LIKE`` 通配符（``%`` / ``_``）与转义符本身，使值按字面匹配。

    配合 ``ESCAPE '\\'`` 使用: 查询 "100% 裕度" 不会被当成"任意字符+裕度"。
    """
    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _as_str_list(value: Any) -> list[str]:
    """把 ``section_contains`` 的值规范成字符串列表（空值 → 空列表）。

    支持 ``str`` 与 ``list[str]``; 其它类型 fail-fast（过滤条件写错比不过滤更危险）。
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


def fts5_available() -> bool:
    """探测当前 sqlite 是否支持 FTS5（供上层在降级前显式判断）。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()
    return True


def bm25_scores(raw_ranks: Any) -> list[float]:
    """把 FTS5 ``bm25()`` 的原始负分转成“越大越相关”的正分数。

    约定（测试固定）: ``bm25()`` 返回负值、越相关越负；取负后高分 = 高相关，
    与向量腿 ``score`` 同向，便于同一套 ``top_k`` / RRF 逻辑复用两腿。
    """
    return [-float(r) for r in raw_ranks]


class KeywordIndex:
    """SQLite（FTS5）关键词索引腿。

    与 :class:`~matrag.index.store.VectorIndex` 可共用同一 ``db_path``: 两者只是
    同一库里的两张虚表（本类 = ``chunks_fts`` FTS5；向量腿 = ``chunks_vec`` vec0），
    互不干扰；各自持有独立连接，写入侧已设 ``busy_timeout`` 容忍并发。

    ``add_chunks`` 按 ``doc_id`` 先删旧行再插入，重入库幂等。
    """

    def __init__(self, db_path: str | Path) -> None:
        """打开 / 新建关键词库；本环境不支持 FTS5 → ``RuntimeError``（fail-fast）。"""
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA busy_timeout=5000")
        try:
            self._conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} USING fts5("
                "toks, chunk_id UNINDEXED, doc_id UNINDEXED, kind UNINDEXED, "
                "table_id UNINDEXED, caption UNINDEXED, section_path UNINDEXED, "
                "page UNINDEXED, raw UNINDEXED, meta_json UNINDEXED)"
            )
        except sqlite3.OperationalError as exc:
            self._conn.close()
            raise RuntimeError(
                f"本环境 SQLite 不支持 FTS5，关键词腿拒绝静默退化: {exc}"
            ) from exc
        self._conn.commit()

    @staticmethod
    def tokenize(text: str) -> list[str]:
        """jieba 精确模式分词 + 去停用词 / 纯标点 / 空（顺序保持，允许重复）。"""
        out: list[str] = []
        for tok in jieba.lcut(text):
            t = tok.strip()
            if not t or t in _STOPWORDS:
                continue
            if not any(ch.isalnum() for ch in t):
                continue  # 纯标点 / 符号
            out.append(t)
        return out

    def add_chunks(self, records: list[ChunkRecord]) -> None:
        """写入 chunk（按 doc_id 先删旧行，幂等）。FTS5 仅支持按 rowid 删，故先取 rowid。"""
        if not records:
            return
        for doc_id in sorted({r.doc_id for r in records}):
            rowids = [
                row[0]
                for row in self._conn.execute(
                    f"SELECT rowid FROM {_FTS_TABLE} WHERE doc_id = ?", (doc_id,)
                ).fetchall()
            ]
            for rid in rowids:
                self._conn.execute(f"DELETE FROM {_FTS_TABLE} WHERE rowid = ?", (rid,))
        self._conn.executemany(
            f"INSERT INTO {_FTS_TABLE}"
            "(toks, chunk_id, doc_id, kind, table_id, caption, section_path, "
            "page, raw, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [self._row(r) for r in records],
        )
        self._conn.commit()

    @staticmethod
    def _row(r: ChunkRecord) -> tuple[Any, ...]:
        meta = r.meta
        return (
            " ".join(KeywordIndex.tokenize(r.text)),
            r.chunk_id,
            r.doc_id,
            str(meta.get("kind", "")),
            str(meta.get("table_id", "") or ""),
            str(meta.get("caption", "") or ""),
            str(meta.get("section_path", "") or ""),
            int(meta.get("page", 0) or 0),
            r.text,
            json.dumps(meta, ensure_ascii=False),
        )

    def search_keyword(
        self, query: str, *, top_k: int = 10, where: dict | None = None
    ) -> list[ScoredChunk]:
        """jieba 分词 → FTS5 ``bm25()`` 排序（越大越相关）。空查询 / 无结果 → 空列表。

        ``where`` 支持两类键:

        - 白名单列的**等值**过滤（``_WHERE_COLUMNS``: doc_id/kind/table_id/
          section_path/caption/page，型号走 ``product_code`` 的 json_extract）;
        - ``section_contains``（章节名/章节号的**包含**过滤）: 在 SQL 层转成
          ``section_path LIKE '%值%' ESCAPE '\\'``，值为 ``str`` 或 ``list[str]``
          （list → 多个 AND LIKE，即全部包含才算命中）。该键不进白名单——
          ``section_path`` 是完整路径，等值匹配不上。
        """
        tokens = self.tokenize(query)
        if not tokens or top_k <= 0:
            return []
        # section_contains 提前解析并校验（空值 = 不加过滤, 非法类型 fail-fast）
        contain_terms = _as_str_list(where.get(SECTION_CONTAINS_KEY)) if where else []
        sql = (
            f"SELECT chunk_id, doc_id, raw, meta_json, bm25({_FTS_TABLE}) AS rank "
            f"FROM {_FTS_TABLE} WHERE {_FTS_TABLE} MATCH ?"
        )
        params: list[Any] = [self._build_match(tokens, operator="AND")]
        if where:
            for key, value in where.items():
                if key == SECTION_CONTAINS_KEY:
                    continue  # 包含过滤单独处理（不是白名单等值列）
                col = _WHERE_COLUMNS.get(key)
                if col is None:
                    raise ValueError(
                        f"关键词腿不支持的过滤键 {key!r}"
                        f"（支持: {sorted(_WHERE_COLUMNS)}）"
                    )
                if key == "product_code":
                    # 型号过滤保留公共知识（scope=public 不绑型号）:
                    # “本型号规格 或 公共知识”——否则通用经验会被误排除
                    sql += (
                        " AND (json_extract(meta_json, '$.product_code') = ?"
                        " OR json_extract(meta_json, '$.scope') = 'public')"
                    )
                    params.append(value)
                    continue
                sql += f" AND {col} = ?"
                params.append(value)
        for term in contain_terms:
            # LIKE 通配符已转义（_like_escape），ESCAPE '\' 声明转义符
            sql += " AND section_path LIKE ? ESCAPE '\\'"
            params.append(f"%{_like_escape(term)}%")
        sql += " ORDER BY rank ASC LIMIT ?"

        def _run() -> list[tuple[Any, ...]]:
            return self._conn.execute(sql, tuple(params) + (int(top_k),)).fetchall()

        rows = _run()
        # AND 召回不足（查询含型号 token 等不入正文的词）→ 切 OR 重试, 由 bm25 排序补召回。
        # 这是检索腿的召回策略, 不是失败降级: 两条路径都正常执行, 只是匹配算子不同。
        if len(rows) < top_k and len(tokens) > 1:
            params[0] = self._build_match(tokens, operator="OR")
            rows = _run()
        return [self._to_chunk(row) for row in rows]

    def chunks_by_doc(self, doc_id: str, *, limit: int = 200) -> list[ScoredChunk]:
        """直取指定文档的全部 chunk（按入库序，不分词不排序）。

        供图谱腿把"实体链接到的源知识卡"展开成 chunk 使用（卡内 chunk 本就不参与
        bm25 排序，返回序 = 文档序）。``score`` 无排序语义（占位），``leg`` 仍是
        ``"keyword"``，由调用方按需改标（图谱腿自行构造 ``ScoredChunk``）。

        Args:
            doc_id: 文档 id（如 ``power-knowledge-8``）; 空白 → ``ValueError``。
            limit: 最多返回条数（防御异常 doc），须 >= 1。

        Returns:
            ``ScoredChunk`` 列表; 文档不存在 → 空列表。
        """
        text = str(doc_id).strip()
        if not text:
            raise ValueError("chunks_by_doc 需要非空 doc_id")
        if int(limit) < 1:
            raise ValueError(f"chunks_by_doc limit 须 >= 1: {limit!r}")
        rows = self._conn.execute(
            f"SELECT chunk_id, doc_id, raw, meta_json, 0.0 AS rank "
            f"FROM {_FTS_TABLE} WHERE doc_id = ? ORDER BY rowid LIMIT ?",
            (text, int(limit)),
        ).fetchall()
        return [self._to_chunk(row) for row in rows]

    @staticmethod
    def _build_match(tokens: list[str], *, operator: str = "AND") -> str:
        """每个 jieba 词用双引号包成 FTS5 字符串常量，词间以 operator 连接
        （AND=同时命中全部词; OR=命中任意词, 由 bm25 排序）。"""
        joiner = f" {operator} "
        return joiner.join('"{}"'.format(t.replace('"', '""')) for t in tokens)

    @staticmethod
    def _to_chunk(row: tuple[Any, ...]) -> ScoredChunk:
        chunk_id, doc_id, raw, meta_json, rank = row
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except json.JSONDecodeError:
            meta = {}
        return ScoredChunk(
            chunk_id=str(chunk_id),
            doc_id=str(doc_id),
            text=str(raw),
            meta=meta,
            score=bm25_scores((rank,))[0],
            leg="keyword",
        )

    def count(self) -> int:
        """当前关键词条数。"""
        return int(self._conn.execute(f"SELECT COUNT(*) FROM {_FTS_TABLE}").fetchone()[0])

    def close(self) -> None:
        """关闭连接。"""
        self._conn.close()

    def __enter__(self) -> KeywordIndex:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
