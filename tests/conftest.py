"""pytest 全局引导。

一次 LLM 连通探测（收集期，20s 超时，max_tokens=8）:
- 探测失败（凭据缺失 / 配额耗尽 / 网络不通）→ 清空 settings.llm_base，使
  ``settings.live`` 变 False，各任务的 ``skipif(not settings.live)`` 真实调用用例
  统一跳过。注意: 这是"外部服务不可用"而非代码回归，不允许它染红测试。
- 探测成功 → 不动配置，live 用例照常执行。
"""

from __future__ import annotations

from matrag.config import Settings, settings


def _probe_llm() -> str | None:
    if not settings.live:
        return "凭据未配置"
    try:
        from matrag.llm import LLMClient

        probe = LLMClient(
            Settings(
                llm_base=settings.llm_base,
                llm_api_key=settings.llm_api_key,
                llm_model=settings.llm_model,
                llm_timeout=20.0,
            )
        )
        resp = probe.complete("ping", "ok", max_tokens=8)
        if not resp.strip():
            return "空响应"
        return None
    except Exception as exc:  # noqa: BLE001 —— 探测失败原因原样记录
        return f"{type(exc).__name__}: {str(exc)[:120]}"


def _probe_embed() -> str | None:
    """embedding 探测: 失败则清空 embed_api_key, 使各任务的 live embed 用例
    （其 skipif 依赖 embed 三要素齐备）统一跳过——同样是环境状态而非回归。"""
    if not (settings.embed_base and settings.embed_api_key and settings.embed_model):
        return None  # 本来就没配, live embed 用例自然跳过
    try:
        from matrag.embeddings import MaaSEmbeddingClient

        vec = MaaSEmbeddingClient().embed("ping")
        if not vec:
            return "空向量"
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:120]}"


_reason = _probe_llm()
if _reason is not None:
    # live → False: 真实外部调用不可用视为环境状态，不是回归
    settings.llm_base = ""

_embed_reason = _probe_embed()
if _embed_reason is not None:
    settings.embed_api_key = ""
