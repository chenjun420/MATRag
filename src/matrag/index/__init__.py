"""索引层（P1-4）: chunk 切分 + 向量腿 + 关键词腿。

- :mod:`~matrag.index.chunking` —— ``CanonicalDocument`` → ``ChunkRecord``
  （切分策略 / 表格逐行 / 页锚点）；
- :mod:`~matrag.index.store`    —— 向量腿（semantica ``SQLiteVecStore`` / vec0）
  与两腿共用的命中契约 ``ScoredChunk``；
- :mod:`~matrag.index.keyword`  —— 关键词腿（FTS5 + jieba + bm25）。

向量腿与关键词腿可共用同一 SQLite 文件（不同虚表名 ``chunks_vec`` / ``chunks_fts``）。
"""

from matrag.index.chunking import ChunkRecord, chunk_document
from matrag.index.keyword import KeywordIndex, bm25_scores, fts5_available
from matrag.index.store import ScoredChunk, VectorIndex

__all__ = [
    "ChunkRecord",
    "chunk_document",
    "ScoredChunk",
    "VectorIndex",
    "KeywordIndex",
    "bm25_scores",
    "fts5_available",
]
