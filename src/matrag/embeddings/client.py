"""Embedding 客户端 (Provider Gateway 的另一半)。

接口稳定、实现可换: 默认走 OpenAI 兼容 ``/embeddings`` 端点（``base_url`` 指向 MaaS）。
所有向量生成只允许经这里，禁止业务代码直连具体 SDK。

本模块提供:

- ``EmbeddingClient``      —— 抽象基类: ``model``/``dim`` 属性 + ``embed``/``embed_batch``。
- ``MaaSEmbeddingClient``  —— OpenAI 兼容实现（MaaS base_url、批量分批、维度探针）。
- ``get_embedder``         —— 进程内单例工厂（风格对齐 ``matrag.llm.get_llm``）。

维度约定 (fail-closed): 维度**不猜**。构造时给了 ``dim`` 直接用；没给则首次使用时
发一个探针串拿真实向量长度。探针失败直接抛 ``RuntimeError``，绝不用默认维度糊过去。
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Any

from matrag.config import Settings, get_settings
from matrag.errors import ProviderNotConfiguredError

logger = logging.getLogger("matrag.embeddings")

#: 维度探针串。内容无所谓，只要非空且稳定（便于复现与日志比对）。
PROBE_TEXT = "matrag-embedding-dimension-probe"


class EmbeddingClient(ABC):
    """向量生成契约: ``model``/``dim`` 标识来源与形状，``embed``/``embed_batch`` 取向量。

    子类必须给出 ``model``/``dim`` 与单条 ``embed``；``embed_batch`` 默认循环 ``embed``，
    可批量化（如 MaaS 支持多条一次请求）的子类自行覆写以省往返。
    """

    @property
    @abstractmethod
    def model(self) -> str:
        """模型标识（含版本）。模型名必须随向量一起入索引，换模型即换库。"""

    @property
    @abstractmethod
    def dim(self) -> int:
        """向量维度（int）。可能惰性探测（见 ``MaaSEmbeddingClient``）。"""

    @property
    def configured(self) -> bool:
        """三要素（base/api_key/model）齐备才为 True。默认 True，provider 子类按需覆写。"""
        return True

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """单条文本 → 向量（长度 == ``dim``）。"""

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量文本 → 向量矩阵，返回值与输入**同序等长**。

        默认逐条调 ``embed``；子类（如 MaaS 可一次提交多条）应覆写以减少往返。
        """
        return [self.embed(t) for t in texts]

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self.model!r})"


class MaaSEmbeddingClient(EmbeddingClient):
    """OpenAI 兼容 ``/embeddings`` 客户端（base_url 指向 MaaS）。

    配置优先级: 显式参数 > settings；``dim`` 例外——只有 settings **显式**给过
    ``embed_dim``（env/构造参数）才采信，否则保持 None 交给探测，不猜维度。

    分批: ``embed_batch`` 按 ``batch_size`` 切片顺序提交，并把结果按 ``index`` 还原成
    输入顺序（服务端若乱序返回也不会错配）。
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        dim: int | None = None,
        batch_size: int | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        st = get_settings() if settings is None else settings
        self._settings = st
        self._base = (base_url or st.embed_base or "").strip()
        self._key = (api_key or st.embed_api_key or "").strip()
        self._model = (model or st.embed_model or "").strip()
        # 显式参数 > settings；settings 里只有"被显式设置过"的 embed_dim 才可信
        # （model_fields_set 记录 env/kwargs 显式赋值的字段名，可作采信依据）。
        if dim is not None:
            self._dim: int | None = int(dim)
        else:
            settings_dim = st.embed_dim if "embed_dim" in st.model_fields_set else None
            self._dim = int(settings_dim) if settings_dim else None
        size = batch_size if batch_size is not None else st.embed_batch_size
        self._batch_size = max(1, int(size))
        self._openai_client: Any = None
        self._last_call_ts: float = -1.0

    # ---- 基本标识 ----

    @property
    def model(self) -> str:
        return self._model

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def configured(self) -> bool:
        """三要素（base/api_key/model）齐备才 True。"""
        return bool(self._base and self._key and self._model)

    @property
    def dim(self) -> int:
        """向量维度；未指定维度时惰性探测（探针失败 fail-closed 抛 RuntimeError）。"""
        if self._dim is None:
            self._dim = self.probe_dim()
        return self._dim

    # ---- 维度探测 ----

    def probe_dim(self) -> int:
        """用探针串的真实返回定维度并写回 ``self._dim``。

        探测失败（未配置/网络/空响应）一律 ``RuntimeError``，因为猜维度会让
        后续所有检索结果错配，宁可现在就炸。
        """
        self._require_configured()
        try:
            vectors = self._request([PROBE_TEXT])
        except Exception as exc:  # noqa: BLE001 —— 汇总成明确的 fail-closed 错误
            raise RuntimeError(
                f"维度探测失败: 模型 {self._model!r} 无法返回向量（{exc}）。"
                "拒绝猜测维度，请检查 MATRAG_EMBED_BASE/API_KEY/MODEL。"
            ) from exc
        dim = len(vectors[0])
        if dim <= 0:
            raise RuntimeError(f"维度探测失败: 模型 {self._model!r} 返回了空向量")
        self._dim = dim
        return dim

    # ---- 向量生成 ----

    def embed(self, text: str) -> list[float]:
        if not isinstance(text, str):
            raise TypeError(f"embed 只接受 str，收到 {type(text).__name__}")
        if self._dim is None:
            self._dim = self.probe_dim()
        return self._request([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """按 ``batch_size`` 分批调用，保持输入顺序（服务端乱序也会被 index 还原）。"""
        if not texts:
            return []
        for t in texts:
            if not isinstance(t, str):
                raise TypeError(f"embed_batch 只接受 list[str]，含 {type(t).__name__}")
        if self._dim is None:
            self._dim = self.probe_dim()
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            out.extend(self._request(texts[start : start + self._batch_size]))
        return out

    def __repr__(self) -> str:
        return f"MaaSEmbeddingClient(model={self._model!r}, base={self._base!r})"

    # ---- 内部 ----

    def _require_configured(self) -> None:
        missing = [
            name
            for name, value in (
                ("embed_base", self._base),
                ("embed_api_key", self._key),
                ("embed_model", self._model),
            )
            if not value
        ]
        if missing:
            raise ProviderNotConfiguredError(
                "Embedding 未配置，缺失 "
                + ", ".join(missing)
                + "（显式参数或 MATRAG_EMBED_BASE/EMBED_API_KEY/EMBED_MODEL）—— "
                "fail-closed，拒绝用未配置端点产出向量"
            )

    def _openai(self) -> Any:
        """惰性构造 SDK 客户端（import 也惰性：离线测试无需真装 openai 也能 import 本模块）。"""
        self._require_configured()
        if self._openai_client is None:
            from openai import OpenAI  # 延迟导入，风格对齐 matrag.llm

            # embed_timeout: settings 显式给过才用，否则 60s（SDK 默认 600s 过宽）
            timeout = (
                float(self._settings.embed_timeout)
                if "embed_timeout" in self._settings.model_fields_set
                else 60.0
            )
            self._openai_client = OpenAI(
                base_url=self._base, api_key=self._key, timeout=timeout
            )
        return self._openai_client


    # ---- 限流/退避 ----
    def _throttle(self) -> None:
        interval = float(getattr(self._settings, 'embed_min_interval', 1.0) or 0.0)
        if interval <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._last_call_ts
        if 0 <= elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_call_ts = time.monotonic()

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        name = type(exc).__name__
        if name in ('RateLimitError', 'APITimeoutError', 'APIConnectionError', 'InternalServerError'):
            return True
        status = getattr(exc, 'status_code', None)
        return isinstance(status, int) and (status == 429 or status >= 500)

    @staticmethod
    def _retry_after(exc: Exception, attempt: int) -> float:
        resp = getattr(exc, 'response', None)
        if resp is not None:
            ra = None
            try:
                ra = resp.headers.get('retry-after')
            except Exception:
                ra = None
            if ra:
                try:
                    return min(60.0, max(1.0, float(ra)))
                except ValueError:
                    pass
        return min(30.0, 2.0 * (2 ** attempt))

    def _request(self, texts: list[str]) -> list[list[float]]:
        """裸调 embeddings.create，带限流退避重试与批量间最小间隔。

        429/5xx/超时 → 指数退避重试（尊重 Retry-After），最多 embed_max_retries 次；
        仍失败则抛错（fail-closed，不静默丢批）。
        """
        client = self._openai()
        last_exc = None
        resp = None
        for attempt in range(max(1, int(self._settings.embed_max_retries))):
            self._throttle()
            try:
                resp = client.embeddings.create(model=self._model, input=list(texts))
                break
            except Exception as exc:  # noqa: BLE001 —— 下面判断是否可重试
                if not self._is_retryable(exc):
                    raise
                last_exc = exc
                wait = self._retry_after(exc, attempt)
                logger.warning(
                    "embeddings 限流/临时错误 attempt=%d/%d wait=%.1fs err=%s",
                    attempt + 1, int(self._settings.embed_max_retries), wait, exc,
                )
                time.sleep(wait)
        if resp is None:
            raise RuntimeError(f"embeddings 重试 {int(self._settings.embed_max_retries)} 次仍失败: {last_exc}")
        data = list(getattr(resp, "data", None) or [])
        if len(data) != len(texts):
            raise RuntimeError(
                f"/embeddings 返回 {len(data)} 条，与输入 {len(texts)} 条不一致"
            )
        vecs = self._ordered_vectors(data, len(texts))
        out: list[list[float]] = []
        for vec in vecs:
            if vec is None or len(vec) == 0:
                raise RuntimeError("/embeddings 返回了空向量")
            raw = [float(x) for x in vec]
            if self._dim is not None and len(raw) != self._dim:
                raise RuntimeError(
                    f"/embeddings 返回维度 {len(raw)} != 期望 {self._dim}"
                    f"（模型 {self._model!r} 可能已升级，向量库需重建）"
                )
            out.append(raw)
        return out

    @staticmethod
    def _ordered_vectors(data: list[Any], n: int) -> list[Any]:
        """按 ``index`` 还原顺序后取向量；index 缺失/不构成排列时信任服务端原序。"""
        embeddings = [list(getattr(d, "embedding", None) or []) for d in data]
        indices = [getattr(d, "index", None) for d in data]
        if all(isinstance(i, int) for i in indices) and sorted(indices) == list(range(n)):
            ordered: list[Any] = [None] * n
            for i, vec in zip(indices, embeddings, strict=True):
                ordered[i] = vec
            return ordered
        return embeddings


@lru_cache
def get_embedder() -> MaaSEmbeddingClient:
    """进程内单例 embedding 客户端（从 settings 读配置）。"""
    return MaaSEmbeddingClient(settings=get_settings())
