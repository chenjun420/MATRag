"""Embedding 缓存层测试（离线打桩，零网络；SQLite 用 tmp_path）。

覆盖: 命中不再发请求、model/dim 入 key 不互串、embed_batch 混命中保序 + 去重、
跨实例持久化、float32 存储口径、损坏行 fail-fast、非法表名 fail-fast。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from pathlib import Path

import pytest

from matrag.embeddings import CachedEmbeddingClient, EmbeddingClient
from matrag.embeddings.cache import _f32, _key, _normalize  # 与实现同源的键/精度口径

STUB_MODEL = "stub-model"
STUB_DIM = 4


class _Stub(EmbeddingClient):
    """确定性假客户端: 向量首维同时编码文本与模型名（可分辨串味），并记录调用。"""

    def __init__(self, model: str = STUB_MODEL, dim: int = STUB_DIM, *,
                 configured: bool = True, drop_last: bool = False) -> None:
        self._model = model
        self._dim = dim
        self._configured = configured
        self.drop_last = drop_last
        self.calls: list[list[str]] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def configured(self) -> bool:
        return self._configured

    def set_dim(self, dim: int) -> None:
        self._dim = dim

    def _vec(self, text: str) -> list[float]:
        head = float(ord(text[0])) if text else 0.0
        return [head + float(len(self._model))] + [0.1] * (self._dim - 1)

    def embed(self, text: str) -> list[float]:
        self.calls.append([text])
        return self._vec(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.drop_last:
            return [self._vec(t) for t in texts[:-1]]  # 故意少返回一条
        return [self._vec(t) for t in texts]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "emb.db"


def _cached(stub: EmbeddingClient, db_path: Path, **kwargs: object) -> CachedEmbeddingClient:
    return CachedEmbeddingClient(stub, db_path, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- 命中/未命中


def test_hit_skips_second_request(db: Path) -> None:
    """同一文本第二次不发请求；命中值 == 首次写入值（float32 口径一致）。"""
    stub = _Stub()
    client = _cached(stub, db)

    first = client.embed("abc")
    second = client.embed("abc")

    assert stub.calls == [["abc"]]
    assert first == second
    assert client.stats() == {"hits": 1, "misses": 1}
    assert client.count() == 1


def test_hit_miss_values_identical_across_process(db: Path) -> None:
    """换实例（模拟新进程/新 worker）读同一库仍命中，且值与本地重算一致。"""
    _cached(_Stub(), db).embed("abc")

    client2 = _cached(_Stub(), db)
    value = client2.embed("abc")

    assert client2.stats() == {"hits": 1, "misses": 0}
    assert value == _normalize(_Stub().embed("abc"))


def test_parent_dir_created(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "emb.db"
    client = _cached(_Stub(), nested)

    assert client.embed("x") == [float(ord("x") + len(STUB_MODEL))] + [_f32(0.1)] * 3
    assert nested.exists()


# --------------------------------------------------------------------------- key 隔离


def test_different_models_do_not_cross_talk(db: Path) -> None:
    """同一文本、不同 model 名 → 不同 key，各自请求一次。"""
    a = _cached(_Stub(model="m-alpha"), db)
    b = _cached(_Stub(model="m-beta"), db)

    va, vb = a.embed("abc"), b.embed("abc")

    assert va != vb
    assert a.count() == 2
    assert a.hits == 0 and b.hits == 0


def test_dim_change_creates_new_key(db: Path) -> None:
    """dim 也进 key：同模型改维度不会读到旧维度的向量。"""
    stub = _Stub()
    client = _cached(stub, db)
    v4 = client.embed("abc")
    stub.set_dim(8)
    v8 = client.embed("abc")

    assert stub.calls == [["abc"], ["abc"]]
    assert len(v4) == 4 and len(v8) == 8
    assert client.count() == 2


def test_key_formula(db: Path) -> None:
    """key == sha256(f"{model}|{dim}|{text}")，并与库内行对得上。"""
    _cached(_Stub(model="m", dim=STUB_DIM), db).embed("abc")

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT key FROM emb_cache").fetchall()

    assert rows == [(hashlib.sha256(b"m|4|abc").hexdigest(),)]
    assert _key("m", 4, "abc") == rows[0][0]


# --------------------------------------------------------------------------- 批量合并


def test_batch_mixed_hits_keep_order(db: Path) -> None:
    """命中/未命中混杂: 只对未命中调 inner，输出按输入顺序还原。"""
    stub = _Stub()
    client = _cached(stub, db)
    client.embed("a")

    out = client.embed_batch(["a", "b", "c"])

    assert stub.calls[-1] == ["b", "c"]
    assert [v[0] for v in out] == [
        ord("a") + len(STUB_MODEL),
        ord("b") + len(STUB_MODEL),
        ord("c") + len(STUB_MODEL),
    ]
    assert all(len(v) == STUB_DIM for v in out)
    # 统计按"处理过的文本条数"计: 1 次 embed 未命中 + 本次 1 命中/2 未命中。
    assert client.stats() == {"hits": 1, "misses": 3}


def test_batch_all_hits_no_request(db: Path) -> None:
    stub = _Stub()
    client = _cached(stub, db)
    client.embed_batch(["a", "b"])

    client.embed_batch(["b", "a"])

    assert stub.calls == [["a", "b"]]
    assert client.stats() == {"hits": 2, "misses": 2}


def test_batch_dedupes_repeated_texts(db: Path) -> None:
    """同一批内重复文本只请求一次，但结果按原位置原样返回。"""
    stub = _Stub()
    client = _cached(stub, db)

    out = client.embed_batch(["a", "a", "b"])

    assert stub.calls == [["a", "b"]]
    assert out[0] == out[1] != out[2]
    assert client.count() == 2


def test_batch_empty(db: Path) -> None:
    stub = _Stub()
    assert _cached(stub, db).embed_batch([]) == []
    assert stub.calls == []


def test_inner_batch_count_mismatch_fails_loud(db: Path) -> None:
    stub = _Stub(drop_last=True)
    client = _cached(stub, db)

    with pytest.raises(RuntimeError, match="不一致"):
        client.embed_batch(["a", "b", "c"])


# --------------------------------------------------------------------------- 存储口径/健壮性


def test_float32_storage(db: Path) -> None:
    """库里存的是 float32 精度的 JSON 数组（4 字节/值）。"""
    _cached(_Stub(model="m"), db).embed("abc")

    with sqlite3.connect(db) as conn:
        raw = conn.execute("SELECT vec FROM emb_cache").fetchone()[0]

    values = json.loads(raw)
    assert len(values) == STUB_DIM
    for v in values:
        assert struct.pack("<f", v) == struct.pack("<f", _f32(v))
    assert values[1] == _f32(0.1)
    assert values[1] != 0.1  # double 0.1 与 float32 0.1 不同，口径确实生效


def test_corrupt_row_fails_fast(db: Path) -> None:
    """缓存损坏直接报错，不返回可疑向量。"""
    _cached(_Stub(model="m"), db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO emb_cache (key, vec, model, dim, created_at) VALUES (?,?,?,?,?)",
            (_key("m", STUB_DIM, "abc"), "not-json", "m", STUB_DIM, "now"),
        )

    with pytest.raises(RuntimeError, match="缓存损坏"):
        _cached(_Stub(model="m"), db).embed("abc")


def test_bad_table_name_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="非法缓存表名"):
        CachedEmbeddingClient(_Stub(), tmp_path / "e.db", table="emb cache; DROP TABLE x")


def test_clear_empties_cache(db: Path) -> None:
    stub = _Stub()
    client = _cached(stub, db)
    client.embed("a")

    assert client.count() == 1
    assert client.clear() == 1
    assert client.count() == 0

    client.embed("a")  # 已清 → 重新请求
    assert stub.calls == [["a"], ["a"]]


# --------------------------------------------------------------------------- 契约透传


def test_delegates_model_dim_configured(db: Path) -> None:
    stub = _Stub(model="m-x", dim=6, configured=False)
    client = _cached(stub, db)

    assert client.model == "m-x"
    assert client.dim == 6
    assert client.configured is False
    assert "CachedEmbeddingClient" in repr(client)


# --------------------------------------------------------------------------- 组合


def test_cached_over_maas_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端: 缓存包住真实 MaaS 客户端。

    首次 embed = 维度探测 1 次 + 正文 1 次（探测是惰性的，只在需要维度时发生）；
    同一文本第二次直接命中，零请求。
    """
    from types import SimpleNamespace

    import openai

    from matrag.embeddings import PROBE_TEXT, MaaSEmbeddingClient

    wires: list[list[str]] = []

    class _FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.embeddings = self

        def create(self, *, model: str, input: list[str]) -> SimpleNamespace:
            wires.append(list(input))
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=i, embedding=[1.0, 2.0, 3.0, 4.0])
                    for i, _ in enumerate(input)
                ]
            )

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    inner = MaaSEmbeddingClient(
        base_url="http://maas.test/v1", api_key="sk-test", model="m", batch_size=3
    )
    client = _cached(inner, tmp_path / "emb.db")

    first = client.embed("abc")

    assert first == [1.0, 2.0, 3.0, 4.0]
    assert wires == [[PROBE_TEXT], ["abc"]]  # 探测 + 正文
    assert client.dim == 4

    assert client.embed("abc") == first
    assert len(wires) == 2  # 命中，零请求
    assert client.stats() == {"hits": 1, "misses": 1}
