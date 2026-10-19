"""semantica 引擎的 Embedding 适配: 把 matrag 的 ``EmbeddingClient`` 注册成 semantica 的自定义生成方法。

semantica 0.7.0 的注册机制（本环境实测，引 ``.venv/Lib/site-packages/semantica/embeddings/registry.py``）:

.. code-block:: python

    class MethodRegistry:
        _methods: Dict[str, Dict[str, Callable]] = {
            "generation": {}, "text": {}, "image": {}, ...}

        @classmethod
        def register(cls, task: str, name: str, method_func: Callable): ...
        @classmethod
        def get(cls, task: str, name: str) -> Optional[Callable]: ...
        @classmethod
        def list_all(cls, task: Optional[str] = None) -> Dict[str, List[str]]: ...

派发路径（``semantica/embeddings/methods.py``）:

.. code-block:: python

    def generate_embeddings(data, data_type=None, method="default", **kwargs) -> np.ndarray:
        custom_method = method_registry.get("generation", method)
        if custom_method and custom_method is not generate_embeddings:
            result = call_custom_method(
                logger, method, custom_method, data, data_type=data_type, **kwargs)
            ...

即: 注册在 task ``"generation"`` 下的方法会以 ``(data, data_type=..., **kwargs)`` 被调用，
``data`` 是单串或字符串列表；``embed_text`` 只按位置传 ``(text, **kwargs)``（无 ``data_type``）。
本适配因此把 ``client.embed_batch`` 包成对两种调用形状都兼容的可调用对象，分别注册到
``"generation"`` 与 ``"text"`` 两个 task 下。

返回形状对齐 semantica 约定: 单串 → 1D ``np.ndarray``，列表 → 2D ``np.ndarray``（float32）。

**不伪造成功**: 若 semantica 的注册 API 形状与上文不符（import 失败、``method_registry``
缺失/无 ``register``、注册后回查拿不到同一可调用对象），注册函数返回 ``False`` 并把原因
写进 :func:`registration_failure_reason`（可查询），绝不返回 True 假装注册上了。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注用
    from collections.abc import Callable

    import numpy as np

from matrag.embeddings.client import EmbeddingClient

logger = logging.getLogger(__name__)

#: 注册进 semantica 的方法名。模型级差异（doubao/qwen/…）由 matrag 侧 settings 控制，
#: 因此只用一个稳定方法名，避免"方法名 ↔ 模型名"双轨漂移。
METHOD_NAME = "matrag-maas"

#: 注册到哪些 task。``generation`` 覆盖 ``generate_embeddings``，``text`` 覆盖 ``embed_text``。
_TASKS = ("generation", "text")

#: 最近一次注册失败的原因（模块级可查询，避免"静默降级"）。
_LAST_FAILURE: str | None = None


def _build_method(client: EmbeddingClient) -> Callable[..., np.ndarray]:
    """把 ``client.embed_batch`` 包成 semantica 自定义生成方法的可调用形状。"""

    def matrag_embed(
        data: Any, data_type: str | None = None, **kwargs: Any
    ) -> np.ndarray:
        import numpy as np  # 延迟导入: 本模块不该因 semantica/numpy 缺失而不可 import

        if data_type not in (None, "", "text"):
            raise TypeError(
                f"方法 {METHOD_NAME!r} 只做文本向量，收到 data_type={data_type!r}"
            )
        if isinstance(data, (list, tuple)):
            return np.asarray(client.embed_batch([str(t) for t in data]), dtype=np.float32)
        if isinstance(data, str):
            return np.asarray(client.embed(data), dtype=np.float32)
        raise TypeError(
            f"方法 {METHOD_NAME!r} 只接受 str 或 list[str]，收到 {type(data).__name__}"
        )

    matrag_embed.__name__ = METHOD_NAME
    return matrag_embed


def _load_registry() -> tuple[Any, str | None]:
    """惰性导入 semantica 注册表；形状不符时返回 ``(None, 原因)``。"""
    global _LAST_FAILURE  # noqa: PLW0603 —— 失败原因需要模块级可查询

    try:
        from semantica.embeddings.registry import method_registry  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 —— 未装/损坏都要转成"未注册 + 原因"
        _LAST_FAILURE = f"无法导入 semantica.embeddings.registry: {exc}"
        return None, _LAST_FAILURE
    if not hasattr(method_registry, "register") or not hasattr(
        method_registry, "list_all"
    ):
        _LAST_FAILURE = (
            "semantica.embeddings.registry.method_registry 缺少 register/list_all，"
            "API 形状与 semantica 0.7.x 不符"
        )
        return None, _LAST_FAILURE
    _LAST_FAILURE = None
    return method_registry, None


def register_with_semantica(
    client: EmbeddingClient, name: str = METHOD_NAME
) -> bool:
    """把 ``client.embed_batch`` 注册为 semantica 自定义生成方法（名如 ``"matrag-maas"``）。

    注册后即可::

        from semantica.embeddings import generate_embeddings
        vecs = generate_embeddings(["a", "b"], method="matrag-maas")   # 走 matrag 客户端

    Args:
        client: 任意 ``EmbeddingClient`` 实现（MaaS / 缓存包装 / 测试桩均可）。
        name: 注册方法名，默认 ``"matrag-maas"``。

    Returns:
        True: 已注册且回查通过（``method_registry.get(task, name)`` 拿回同一可调用对象）。
        False: 未注册。原因见 :func:`registration_failure_reason`。

    Note:
        - 注册是**幂等覆盖**：重复注册同一名字只是替换可调用对象。
        - 本函数不发起任何 embedding 请求（不探测、不联网），只有真实生成时才碰端点。
        - semantica API 形状不符时返回 False 而不是伪造成功；此时调用方可自行走
          ``client.embed_batch``，不必依赖 semantica 派发。
    """
    global _LAST_FAILURE  # noqa: PLW0603

    registry, _reason = _load_registry()
    if registry is None:
        return False

    method = _build_method(client)
    done: list[str] = []
    try:
        for task in _TASKS:
            registry.register(task, name, method)
            done.append(task)
    except Exception as exc:  # noqa: BLE001 —— 注册本身失败也要留痕
        _LAST_FAILURE = f"method_registry.register 抛错: {exc}"
        _rollback(registry, done, name)
        return False

    # 回查验证: get 必须拿回同一对象且 list_all 含该名字，否则视为未生效。
    for task in _TASKS:
        try:
            back = registry.get(task, name)
            listed = name in registry.list_all(task).get(task, [])
        except Exception as exc:  # noqa: BLE001
            _LAST_FAILURE = f"注册后回查 task={task!r} 失败: {exc}"
            _rollback(registry, _TASKS, name)
            return False
        if back is not method or not listed:
            _LAST_FAILURE = (
                f"注册后回查不符: task={task!r} get->{back!r}, "
                f"list_all 含 {name}={listed}"
            )
            _rollback(registry, _TASKS, name)
            return False

    _LAST_FAILURE = None
    return True


def unregister_from_semantica(name: str = METHOD_NAME) -> None:
    """从 semantica 注销本适配注册的方法（测试清理 / 换模型实现时用）。"""
    registry, _reason = _load_registry()
    if registry is None:
        return
    _rollback(registry, list(_TASKS), name)


def registered_names(task: str = "generation") -> list[str]:
    """semantica 指定 task 下当前已注册的方法名（断言 / 观测用）。"""
    registry, _reason = _load_registry()
    if registry is None:
        return []
    try:
        return list(registry.list_all(task).get(task, []))
    except Exception as exc:  # noqa: BLE001 —— 探不到就当没有, 但留痕 (fail-fast 纪律)
        logger.debug("semantica.list_all_skipped: task=%s err=%s", task, exc)
        return []


def registration_failure_reason() -> str | None:
    """最近一次注册失败的原因；从未失败或最近一次成功时返回 ``None``。"""
    return _LAST_FAILURE


def _rollback(registry: Any, tasks: list[str], name: str) -> None:
    """部分失败时回滚已注册的 task，避免留下半成品方法名。"""
    for task in tasks:
        try:
            if hasattr(registry, "unregister"):
                registry.unregister(task, name)
        except Exception as exc:  # noqa: BLE001 —— 回滚尽力而为, 但留痕
            logger.debug("semantica.rollback_skipped: task=%s err=%s", task, exc)
