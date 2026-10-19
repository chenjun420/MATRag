"""Provider Gateway 的 embedding 半边: 向量生成的唯一入口。

- ``client.py``         —— ``EmbeddingClient`` 契约 + MaaS（OpenAI 兼容 /embeddings）实现
- ``cache.py``          —— SQLite 缓存（key = model|dim|text，float32 JSON）
- ``semantica_adapter.py`` —— 把上面的 client 注册成 semantica 自定义生成方法

用法::

    from matrag.embeddings import MaaSEmbeddingClient, CachedEmbeddingClient

    client = CachedEmbeddingClient(MaaSEmbeddingClient(), "data/store/emb.db")
    vecs = client.embed_batch(["…", "…"])           # 顺序与输入一致
    dim = client.dim                                # 惰性探测，探不到直接 RuntimeError

换实现（换厂商/本地模型）只需新写一个 ``EmbeddingClient`` 子类，业务代码不动。
"""

from matrag.embeddings.cache import CachedEmbeddingClient
from matrag.embeddings.client import (
    PROBE_TEXT,
    EmbeddingClient,
    MaaSEmbeddingClient,
    get_embedder,
)
from matrag.embeddings.semantica_adapter import (
    METHOD_NAME,
    register_with_semantica,
    registered_names,
    registration_failure_reason,
    unregister_from_semantica,
)

__all__ = [
    "EmbeddingClient",
    "MaaSEmbeddingClient",
    "CachedEmbeddingClient",
    "get_embedder",
    "PROBE_TEXT",
    "METHOD_NAME",
    "register_with_semantica",
    "unregister_from_semantica",
    "registered_names",
    "registration_failure_reason",
]
