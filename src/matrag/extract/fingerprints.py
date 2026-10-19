"""抽取投影层的稳定指纹 (确定性, 跨次运行逐位一致)。

指纹的用途 (消费侧契约):
- ``cond_fingerprint``: 条件的身份。ATEStudio 用它在**重导**时判断"还是不是
  那条条件" —— 变了就意味着人工签过的字失效, 用例被打回 draft。因此它必须只
  覆盖**语义**: kind/side/value/method_ref; 措辞 (text) 变化不该清零签字。
- ``req_fingerprint``: 需求的身份, 覆盖需求级语义内容。
- ``product_doc_fingerprint``: 源文档字节指纹 (溯源)。
- ``bundle_fingerprint``: 整个导出文件的内容指纹, 排除时间戳/自身字段,
  用于"同一输入两次导出逐位一致"的幂等性验收。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "bundle_fingerprint",
    "cond_fingerprint",
    "file_fingerprint",
    "req_fingerprint",
]

#: bundle 内容指纹忽略的字段 (时间/自指/环境噪声)。
_VOLATILE_BUNDLE_KEYS = frozenset({"generated_at", "bundle_fingerprint"})


def _canon(obj: Any) -> str:
    """确定性 JSON 序列化 (键排序、紧凑分隔符、保留 UNICODE)。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def cond_fingerprint(*, kind: str, side: str, value: Mapping[str, Any] | None,
                     method_ref: str = "") -> str:
    """条件指纹: 覆盖 kind/side/value/method_ref, 不覆盖 text。

    value 为 None 与空 dict 归一 (两侧契约里 None 与 {} 都可出现, 语义相同)。
    """
    payload = {
        "kind": kind,
        "side": side,
        "value": dict(value) if value else {},
        "method_ref": method_ref or "",
    }
    return hashlib.sha256(_canon(payload).encode("utf-8")).hexdigest()[:16]


def req_fingerprint(*, requirement_code: str, title: str, input_fps: list[str],
                    output_fps: list[str], limits: list[Mapping[str, Any]]) -> str:
    """需求指纹: 需求级语义 (编号/标题/两侧条件指纹/限值集合)。"""
    payload = {
        "requirement_code": requirement_code,
        "title": title,
        "input_conditions": sorted(input_fps),
        "output_conditions": sorted(output_fps),
        "limits": sorted(_canon(lim) for lim in limits),
    }
    return hashlib.sha256(_canon(payload).encode("utf-8")).hexdigest()[:16]


def file_fingerprint(path: Path | str) -> str:
    """源文件字节指纹 (前 32 位十六进制)。"""
    data = Path(path).read_bytes()
    return hashlib.sha256(data).hexdigest()[:32]


def bundle_fingerprint(payload: Mapping[str, Any]) -> str:
    """整个 bundle 的内容指纹 (排除 generated_at / bundle_fingerprint)。"""
    stable = {k: v for k, v in payload.items() if k not in _VOLATILE_BUNDLE_KEYS}
    return hashlib.sha256(_canon(stable).encode("utf-8")).hexdigest()[:32]
