"""Embedding 客户端测试（离线打桩，零网络）。

- fake OpenAI SDK：monkeypatch ``openai.OpenAI``，确定性返回，可注入失败/乱序/条数不符；
- 覆盖: 批量分批（3/3/1）、输入顺序保持、维度探测、未配置 fail-closed、settings 优先级；
- 文件末尾是 semantica 适配的注册/派发测试（按本任务声明的测试文件范围集中放在这里，
  镜像命名文件 ``tests/test_embeddings_semantica_adapter.py`` 留给主会话按需拆分）。
"""

from __future__ import annotations

from typing import Any

import openai
import pytest

from matrag.config import Settings, settings
from matrag.embeddings import (
    METHOD_NAME,
    PROBE_TEXT,
    EmbeddingClient,
    MaaSEmbeddingClient,
    register_with_semantica,
    registered_names,
    registration_failure_reason,
    unregister_from_semantica,
)

DIM = 8


# --------------------------------------------------------------------------- fake SDK


def _vec(text: str, dim: int = DIM) -> list[float]:
    """确定性伪向量: 第 0 维编码首字符码（顺序断言用），其余恒定 0.1。"""
    head = float(ord(text[0])) if text else 0.0
    return [head] + [0.1] * (dim - 1)


class _Item:
    """假 embedding 条目（对齐 openai SDK 的 ``index``/``embedding``）。"""

    def __init__(self, index: int, embedding: list[float]) -> None:
        self.index = index
        self.embedding = embedding


class _Resp:
    def __init__(self, data: list[_Item]) -> None:
        self.data = data


class _Harness:
    """拦截 ``OpenAI(...)`` 构造与 ``embeddings.create``，记录调用序列。"""

    def __init__(self, *, dim: int = DIM, fail: bool = False,
                 shuffle: bool = False, drop: int = 0) -> None:
        self.dim = dim
        self.fail = fail
        self.shuffle = shuffle
        self.drop = drop
        self.calls: list[tuple[str, list[str]]] = []
        self.init_kwargs: list[dict[str, Any]] = []

    def make_client(self, **kwargs: Any) -> Any:
        self.init_kwargs.append(dict(kwargs))
        return self

    @property
    def embeddings(self) -> _Harness:
        return self

    def create(self, *, model: str, input: list[str]) -> _Resp:
        self.calls.append((model, list(input)))
        if self.fail:
            raise RuntimeError("endpoint down")
        items = [_Item(i, _vec(t, self.dim)) for i, t in enumerate(input)]
        if self.shuffle:
            items.reverse()
        if self.drop:
            items = items[: max(0, len(items) - self.drop)]
        return _Resp(items)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    """把 ``openai.OpenAI`` 换成假实现（本客户端惰性 import，故只需替换模块属性）。"""
    h = _Harness()
    monkeypatch.setattr(openai, "OpenAI", h.make_client)
    return h


def _maas(harness: _Harness, **kwargs: Any) -> MaaSEmbeddingClient:
    """构造一个"配置齐备"的 MaaS 客户端（dim 显式给，避免触发探测噪声）。"""
    kwargs.setdefault("base_url", "http://maas.test/v1")
    kwargs.setdefault("api_key", "sk-test")
    kwargs.setdefault("model", "doubao-embedding-vision")
    kwargs.setdefault("dim", DIM)
    return MaaSEmbeddingClient(**kwargs)


def _sizes(harness: _Harness) -> list[int]:
    return [len(texts) for _, texts in harness.calls]


# --------------------------------------------------------------------------- 批量/顺序


def test_batch_split_and_order(harness: _Harness) -> None:
    """batch_size=3、7 条 → 调用序列 3/3/1，且顺序与输入一致。"""
    client = _maas(harness, batch_size=3)
    texts = [chr(ord("a") + i) for i in range(7)]

    vecs = client.embed_batch(texts)

    assert _sizes(harness) == [3, 3, 1]
    assert [v[0] for v in vecs] == [float(ord(t)) for t in texts]
    assert all(len(v) == DIM for v in vecs)


def test_batch_default_batch_size(harness: _Harness) -> None:
    """未显式给 batch_size 时用 settings.embed_batch_size。"""
    st = Settings(embed_base="http://s.test/v1", embed_api_key="sk-s",
                  embed_model="m-s", embed_batch_size=4, embed_dim=DIM)
    client = MaaSEmbeddingClient(settings=st)
    assert client.batch_size == 4

    client.embed_batch([chr(ord("a") + i) for i in range(10)])

    assert _sizes(harness) == [4, 4, 2]


def test_embed_batch_empty(harness: _Harness) -> None:
    assert _maas(harness).embed_batch([]) == []
    assert harness.calls == []


def test_response_out_of_order_is_restored(harness: _Harness) -> None:
    """服务端乱序返回时按 index 还原，不错配文本。"""
    harness.shuffle = True
    texts = ["a", "b", "c"]

    vecs = _maas(harness).embed_batch(texts)

    assert [v[0] for v in vecs] == [float(ord(t)) for t in texts]


def test_response_count_mismatch_fails_loud(harness: _Harness) -> None:
    """返回条数与输入不符 → RuntimeError（错配向量比报错更糟）。"""
    harness.drop = 1

    with pytest.raises(RuntimeError, match="不一致"):
        _maas(harness).embed_batch(["a", "b", "c"])


def test_type_errors(harness: _Harness) -> None:
    client = _maas(harness)
    with pytest.raises(TypeError):
        client.embed(123)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        client.embed_batch(["a", 2])  # type: ignore[list-item]


def test_dim_mismatch_fails_loud(harness: _Harness) -> None:
    """模型换了维度（升级）→ 直接报错，绝不把错维向量喂给索引。"""
    client = _maas(harness, dim=4)

    with pytest.raises(RuntimeError, match="维度"):
        client.embed("a")


# --------------------------------------------------------------------------- 维度探测


def test_dim_probe_uses_returned_length(harness: _Harness) -> None:
    """dim=None → 探针返回 8 维 → dim==8。"""
    client = _maas(harness, dim=None)

    assert client.dim == DIM
    assert harness.calls[0][1] == [PROBE_TEXT]
    assert harness.calls[0][0] == "doubao-embedding-vision"


def test_probe_dim_method_is_public(harness: _Harness) -> None:
    assert _maas(harness, dim=None).probe_dim() == DIM


def test_dim_probe_failure_raises(harness: _Harness) -> None:
    """探测失败 fail-closed：RuntimeError，不猜维度。"""
    harness.fail = True
    client = _maas(harness, dim=None)

    with pytest.raises(RuntimeError, match="维度探测失败"):
        assert client.dim > 0

    with pytest.raises(RuntimeError):
        assert client.embed("a")


def test_settings_embed_dim_honored_only_when_explicit(harness: _Harness) -> None:
    """settings 显式给过 embed_dim 才采信；否则探测（默认 1024 是兜底不是真相）。"""
    st_unset = Settings(embed_base="http://s.test/v1", embed_api_key="sk-s",
                        embed_model="m-s")
    client = MaaSEmbeddingClient(settings=st_unset)
    client.embed("a")
    assert harness.calls[0][1] == [PROBE_TEXT]
    assert client.dim == DIM

    st_set = Settings(embed_base="http://s.test/v1", embed_api_key="sk-s",
                      embed_model="m-s", embed_dim=DIM)
    client2 = MaaSEmbeddingClient(settings=st_set)
    client2.embed("a")
    assert all(texts != [PROBE_TEXT] for _, texts in harness.calls[1:])
    assert client2.dim == DIM


# --------------------------------------------------------------------------- 配置/fail-closed


def test_unconfigured_raises_with_missing_names(harness: _Harness) -> None:
    """三要素不齐 → configured False + 报错消息带缺失项名。"""
    st = Settings(embed_base="", embed_api_key="", embed_model="")
    client = MaaSEmbeddingClient(settings=st)

    assert client.configured is False
    assert client.model == ""
    with pytest.raises(RuntimeError) as exc:
        client.embed("a")
    message = str(exc.value)
    assert "embed_base" in message
    assert "embed_api_key" in message
    assert "embed_model" in message
    assert harness.calls == []


def test_partial_config_reports_only_missing(harness: _Harness) -> None:
    st = Settings(embed_base="http://s.test/v1", embed_api_key="", embed_model="m")
    client = MaaSEmbeddingClient(settings=st)

    with pytest.raises(RuntimeError) as exc:
        client.embed("a")
    assert "embed_api_key" in str(exc.value)
    assert "embed_base" not in str(exc.value)


def test_settings_fallback_and_priority(harness: _Harness) -> None:
    """显式参数 > settings；settings 三要素透传给 SDK 构造。"""
    st = Settings(embed_base="http://s.test/v1", embed_api_key="sk-s",
                  embed_model="m-s", embed_dim=DIM)
    client = MaaSEmbeddingClient(settings=st)

    assert (client.model, client.configured) == ("m-s", True)
    client.embed("a")
    assert harness.init_kwargs == [
        {"base_url": "http://s.test/v1", "api_key": "sk-s", "timeout": 60.0}
    ]
    assert harness.calls[0][0] == "m-s"

    explicit = MaaSEmbeddingClient(model="m-explicit", settings=st)
    assert explicit.model == "m-explicit"


def test_input_is_list_and_model_forwarded(harness: _Harness) -> None:
    """/embeddings 的 input 统一按列表提交（批量形状一致）。"""
    _maas(harness).embed("hello")

    assert harness.calls == [("doubao-embedding-vision", ["hello"])]


# --------------------------------------------------------------------------- 基类契约


class _Stub(EmbeddingClient):
    """最小实现: 不覆写 embed_batch，走基类默认循环。"""

    def __init__(self) -> None:
        self.seen: list[str] = []

    @property
    def model(self) -> str:
        return "stub"

    @property
    def dim(self) -> int:
        return 3

    def embed(self, text: str) -> list[float]:
        self.seen.append(text)
        return [0.1, 0.2, 0.3]


def test_base_embed_batch_defaults_to_loop() -> None:
    stub = _Stub()

    assert stub.embed_batch(["a", "b"]) == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]
    assert stub.seen == ["a", "b"]
    assert stub.configured is True
    assert "stub" in repr(stub)


def test_abstract_client_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        EmbeddingClient()  # type: ignore[abstract]


# --------------------------------------------------------------------------- semantica 适配


@pytest.fixture
def _stub_client() -> _Stub:
    return _Stub()


@pytest.fixture
def _cleanup_registry():
    """semantica 注册表是全局状态，测完注销，避免污染同会话其他用例。"""
    yield
    unregister_from_semantica(METHOD_NAME)


def test_register_with_semantica_roundtrip(_stub_client: _Stub,
                                           _cleanup_registry: None) -> None:
    """注册后回查通过，且 registered_names() 含 "matrag-maas"。"""
    assert register_with_semantica(_stub_client) is True

    assert METHOD_NAME in registered_names("generation")
    assert METHOD_NAME in registered_names("text")
    assert registration_failure_reason() is None


def test_registered_method_dispatches_to_client(_stub_client: _Stub,
                                                _cleanup_registry: None) -> None:
    """端到端: semantica 的 generate_embeddings/embed_text 派发到 matrag 客户端。"""
    from semantica.embeddings import embed_text, generate_embeddings

    assert register_with_semantica(_stub_client) is True

    matrix = generate_embeddings(["a", "b"], method=METHOD_NAME, data_type="text")
    assert matrix.shape == (2, 3)
    assert matrix.dtype.name == "float32"
    assert matrix[:, 0].tolist() == pytest.approx([0.1, 0.1])

    single = generate_embeddings("a", method=METHOD_NAME)
    assert single.shape == (3,)
    assert single.tolist() == pytest.approx([0.1, 0.2, 0.3])

    assert embed_text(["a", "b"], method=METHOD_NAME).shape == (2, 3)
    assert embed_text("a", method=METHOD_NAME).shape == (3,)


def test_registered_method_rejects_unsupported_input(
    _stub_client: _Stub, _cleanup_registry: None
) -> None:
    """非文本输入 fail-fast（TypeError 透传，不静默走 semantica 默认实现）。"""
    from semantica.embeddings import generate_embeddings

    assert register_with_semantica(_stub_client) is True

    with pytest.raises(TypeError):
        generate_embeddings("a", method=METHOD_NAME, data_type="image")


def test_register_returns_false_when_api_shape_differs(
    _stub_client: _Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """semantica 注册 API 形状不符 → False + 可查询原因（不伪造成功）。"""

    class _BrokenRegistry:
        def register(self, task: str, name: str, func: Any) -> None:  # 缺 list_all/get
            return None

    monkeypatch.setattr("semantica.embeddings.registry.method_registry",
                        _BrokenRegistry())

    assert register_with_semantica(_stub_client) is False
    assert registration_failure_reason() is not None
    assert "list_all" in (registration_failure_reason() or "")


def test_register_returns_false_when_roundtrip_mismatch(
    _stub_client: _Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """注册后回查拿不到同一对象 → False 并回滚。"""

    class _LyingRegistry:
        def __init__(self) -> None:
            self.store: dict[str, Any] = {}

        def register(self, task: str, name: str, func: Any) -> None:
            self.store[task] = lambda *a, **k: None  # 存了个别的对象

        def get(self, task: str, name: str) -> Any:
            return self.store.get(task)

        def list_all(self, task: str | None = None) -> dict[str, list[str]]:
            return {task: [METHOD_NAME]}

        def unregister(self, task: str, name: str) -> None:
            self.store.pop(task, None)

    fake = _LyingRegistry()
    monkeypatch.setattr("semantica.embeddings.registry.method_registry", fake)

    assert register_with_semantica(_stub_client) is False
    assert fake.store == {}  # 已回滚
    assert "回查不符" in (registration_failure_reason() or "")


def test_unregister_removes_method(_stub_client: _Stub) -> None:
    assert register_with_semantica(_stub_client) is True
    unregister_from_semantica()

    assert METHOD_NAME not in registered_names("generation")
    assert METHOD_NAME not in registered_names("text")


# --------------------------------------------------------------------------- 真实调用


def _live_embedding_ready() -> bool:
    """settings.live 且 embedding 三要素齐备才真连。"""
    return bool(settings.live and settings.embed_base and settings.embed_api_key
                and settings.embed_model)


@pytest.mark.skipif(not _live_embedding_ready(),
                    reason="无 MaaS embedding 凭据，跳过真实调用")
def test_live_maas_embeddings() -> None:
    """真实 MaaS 冒烟: 维度自洽 + 批量有序（无凭据自动跳过）。"""
    client = MaaSEmbeddingClient()

    vec = client.embed("matrag 冒烟测试")
    assert len(vec) == client.dim
    assert all(isinstance(x, float) for x in vec)

    vecs = client.embed_batch(["甲", "乙", "丙"])
    assert len(vecs) == 3
    assert all(len(v) == client.dim for v in vecs)


def test_get_embedder_constructs_with_settings_keyword() -> None:
    """回归: ``get_embedder`` 曾把 Settings 按位置传给 ``base_url``。

    症状: ``(base_url or ...).strip()`` 对 Settings 调 ``.strip()`` →
    ``AttributeError: 'Settings' object has no attribute 'strip'``，
    网关检索面整条链 fail-closed（retriever_unavailable）。构造期不触网
    （dim 探测惰性），只断言修复后能正常构造。
    """
    from matrag.embeddings.client import get_embedder

    get_embedder.cache_clear()
    try:
        client = get_embedder()
        # 修复前这里是 Settings 实例（位置参数错位），修复后必须是 str
        assert isinstance(client._base, str)  # noqa: SLF001 —— 回归断言私有字段类型
        assert isinstance(client.model, str)
    finally:
        get_embedder.cache_clear()
