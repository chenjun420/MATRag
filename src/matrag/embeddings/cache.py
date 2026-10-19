"""Embedding 缓存层（SQLite 落盘，模型+维度入 key）。

向量生成是纯函数式开销（同一文本 + 同一模型 + 同一维度 ⇒ 同一向量），因此缓存
既安全又划算: 文档切块后重复切块、评测重跑都能省掉大量 MaaS 往返。

缓存键: ``sha256(f"{model}|{dim}|{text}")`` —— 模型名与维度进 key，所以换模型/换维度
不会串味（老缓存自然失焦，需要时 ``clear()`` 清库）。

存储: SQLite 单表，值为 **float32** 精度向量的 JSON 数组（与 sqlite-vec 的 float32
语义一致，避免 double 精度假象）。命中和未命中返回的都是同一套 float32 值，
因此同一文本不受"是否命中"影响。连接按操作临时建立（用完即关），实例可跨线程使用。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import struct
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from matrag.embeddings.client import EmbeddingClient

#: 表名只允许安全标识符（用户传进来的名字要拼进 DDL，不能注入）。
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: ``key IN (...)`` 批量查询的分片大小，避开 SQLite 变量数上限。
_LOOKUP_CHUNK = 500


def _f32(value: float) -> float:
    """按 float32 舍入（结构体往返；不引 numpy，精度语义与 SQLite float32 列一致）。"""
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def _normalize(vec: Sequence[float]) -> list[float]:
    """float32 舍入 —— 缓存层的返回值口径，命中/未命中一致。"""
    return [_f32(x) for x in vec]


def _encode(vec: Sequence[float]) -> str:
    """float32 后的 JSON 数组（紧凑分隔）。"""
    return json.dumps(_normalize(vec), separators=(",", ":"))


def _decode(raw: str) -> list[float]:
    """解析缓存值；形状不符即抛 ``ValueError``（损坏缓存宁可炸，不返回可疑向量）。"""
    data = json.loads(raw)
    if not isinstance(data, list) or not data:
        raise ValueError("缓存值不是非空 JSON 数组")
    for x in data:
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            raise ValueError("缓存值含非数值元素")
    return [float(x) for x in data]


def _key(model: str, dim: int, text: str) -> str:
    """缓存键: 模型 + 维度 + 文本三元组 sha256。"""
    return hashlib.sha256(f"{model}|{dim}|{text}".encode()).hexdigest()


class CachedEmbeddingClient(EmbeddingClient):
    """给任意 ``EmbeddingClient`` 加 SQLite 缓存（命中零请求，未命中回填）。

    ``embed_batch`` 只对**未命中**的文本调 ``inner``（同一批内重复文本也只请求一次），
    最后按原始输入顺序还原；命中/未命中混杂时顺序依然正确。
    """

    def __init__(
        self,
        inner: EmbeddingClient,
        db_path: str | Path,
        table: str = "emb_cache",
    ) -> None:
        if not _TABLE_RE.match(table):
            raise ValueError(f"非法缓存表名 {table!r}（只允许数据库标识符）")
        self._inner = inner
        self._db_path = Path(db_path)
        self._table = table
        self._hits = 0
        self._misses = 0
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._execute(
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "key TEXT PRIMARY KEY, "
            "vec TEXT NOT NULL, "
            "model TEXT NOT NULL, "
            "dim INTEGER NOT NULL, "
            "created_at TEXT NOT NULL)"
        )

    # ---- 契约透传 ----

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def dim(self) -> int:
        return self._inner.dim

    @property
    def configured(self) -> bool:
        return self._inner.configured

    # ---- 观测 ----

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def stats(self) -> dict[str, int]:
        """命中/未命中计数（可查询，便于评估缓存收益与排障）。"""
        return {"hits": self._hits, "misses": self._misses}

    def count(self) -> int:
        """缓存行数（跨进程视角的真实存量）。"""
        rows = self._fetchall(f"SELECT COUNT(*) FROM {self._table}")
        return int(rows[0][0])

    def clear(self) -> int:
        """清空缓存（换模型 / 重建索引时用），返回删除行数。"""
        return self._execute(f"DELETE FROM {self._table}")

    # ---- 向量生成 ----

    def embed(self, text: str) -> list[float]:
        k = _key(self._inner.model, self._inner.dim, text)
        found = self._lookup([k])
        cached = found.get(k)
        if cached is not None:
            self._hits += 1
            return cached
        self._misses += 1
        vec = _normalize(self._inner.embed(text))
        self._store([(k, vec)])
        return vec

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """按缓存命中分批合并请求: 命中直接取，未命中凑成一次 ``inner.embed_batch``。"""
        if not texts:
            return []
        model, dim = self._inner.model, self._inner.dim
        keys = [_key(model, dim, t) for t in texts]
        found = self._lookup(keys)

        # 同一批里的重复文本只请求一次（inner 收到去重后的唯一未命中文本）。
        unique_missing: list[str] = []
        seen: set[str] = set()
        for text, k in zip(texts, keys, strict=True):
            if k in found or text in seen:
                continue
            seen.add(text)
            unique_missing.append(text)

        fresh: dict[str, list[float]] = {}
        if unique_missing:
            vecs = self._inner.embed_batch(unique_missing)
            if len(vecs) != len(unique_missing):
                raise RuntimeError(
                    f"inner.embed_batch 返回 {len(vecs)} 条，与请求 "
                    f"{len(unique_missing)} 条不一致"
                )
            fresh = {
                _key(model, dim, text): _normalize(vec)
                for text, vec in zip(unique_missing, vecs, strict=True)
            }
            self._store(list(fresh.items()))

        self._misses += len(unique_missing)
        self._hits += len(texts) - len(unique_missing)

        out: list[list[float]] = []
        for k in keys:
            vec = found.get(k) or fresh.get(k)
            if vec is None:
                raise RuntimeError(f"key={k[:12]}… 既没命中也没算出来，疑似并发写入损坏")
            out.append(vec)
        return out

    def __repr__(self) -> str:
        return f"CachedEmbeddingClient(inner={self._inner!r}, db={self._db_path.name})"

    # ---- 内部 ----

    def _connect(self) -> sqlite3.Connection:
        """按操作新建连接（用完即关），多线程/多实例复用同一文件不会串连接状态。"""
        return sqlite3.connect(self._db_path, timeout=30.0)

    def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with closing(self._connect()) as conn:
            return conn.execute(sql, tuple(params)).fetchall()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with closing(self._connect()) as conn:
            with conn:  # 事务: 失败回滚
                cur = conn.execute(sql, tuple(params))
                return cur.rowcount or 0

    def _executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
        with closing(self._connect()) as conn:
            with conn:  # 事务: 失败整体回滚
                cur = conn.executemany(sql, [tuple(r) for r in rows])
                return cur.rowcount or 0

    def _lookup(self, keys: list[str]) -> dict[str, list[float]]:
        """批量查缓存；发现损坏行直接抛错（fail-fast: 返回可疑向量比慢更糟）。"""
        out: dict[str, list[float]] = {}
        for start in range(0, len(keys), _LOOKUP_CHUNK):
            chunk = keys[start : start + _LOOKUP_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            rows = self._fetchall(
                f"SELECT key, vec FROM {self._table} WHERE key IN ({placeholders})",
                chunk,
            )
            for k, raw in rows:
                try:
                    out[k] = _decode(raw)
                except (ValueError, TypeError) as exc:
                    raise RuntimeError(f"缓存损坏 key={k[:12]}…: {exc}") from exc
        return out

    def _store(self, pairs: Sequence[tuple[str, Sequence[float]]]) -> None:
        """回填（幂等 upsert）。"""
        now = datetime.now(UTC).isoformat()
        rows = [
            (k, _encode(vec), self._inner.model, len(vec), now) for k, vec in pairs
        ]
        self._executemany(
            f"INSERT OR REPLACE INTO {self._table} "
            "(key, vec, model, dim, created_at) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
