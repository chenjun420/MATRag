"""LLM 门面 (Provider Gateway 的一半)。

接口稳定、实现可换: 默认走 OpenAI 兼容端点（base_url 指向 MaaS）。
所有语义判断（表格重建/抽取/诊断/生成）只允许经这里，禁止业务代码直连具体 SDK。
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from matrag.config import Settings, get_settings


class LLMClient:
    """OpenAI 兼容 chat completions 客户端。

    ``complete_json`` 带一次修复重试: 首次解析失败时把错误回给模型重输出。
    LLM 不可用（未配置/网络失败）时抛 ``RuntimeError``，调用方决定 fail-closed。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(self._settings.llm_base and self._settings.llm_api_key and self._settings.llm_model)

    def _openai(self):
        if not self.configured:
            raise RuntimeError(
                "LLM 未配置 (MATRAG_LLM_BASE/API_KEY/MODEL) —— fail-closed, 拒绝假输出"
            )
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                base_url=self._settings.llm_base,
                api_key=self._settings.llm_api_key,
                timeout=self._settings.llm_timeout,
            )
        return self._client

    def complete(self, system: str, user: str, *, temperature: float | None = None,
                 max_tokens: int | None = None, response_format_json: bool = False) -> str:
        client = self._openai()
        kwargs: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self._settings.llm_temperature if temperature is None else temperature,
            "max_tokens": self._settings.llm_max_tokens if max_tokens is None else max_tokens,
        }
        if response_format_json:
            kwargs["response_format"] = {"type": "json_object"}
        extra_body: dict[str, Any] = {}
        if self._settings.llm_enable_thinking is not None:
            # 平台兼容参数: 显式关闭/开启思考（qwen3.8-max 实测 121s→10s）
            extra_body["enable_thinking"] = bool(self._settings.llm_enable_thinking)
        if extra_body:
            kwargs["extra_body"] = extra_body
        resp = client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    def complete_json(self, system: str, user: str, *, schema_hint: str = "",
                      max_tokens: int | None = None) -> dict[str, Any]:
        """要求模型输出 JSON 对象; 解析失败返回错误信息与原始输出，供修复重试。"""
        hint = f"\n\n输出 JSON，schema 如下（只输出 JSON，无解释）:\n{schema_hint}" if schema_hint else ""
        raw = self.complete(system, user + hint, max_tokens=max_tokens, response_format_json=True)
        try:
            return _loads_lenient(raw)
        except json.JSONDecodeError as exc:
            repair_user = (
                f"上一次输出不是合法 JSON: {exc}\n原始输出:\n{raw[:2000]}\n"
                "请只重输出修正后的 JSON。"
            )
            raw2 = self.complete(system, repair_user + hint, max_tokens=max_tokens,
                                 response_format_json=True)
            return _loads_lenient(raw2)


def _loads_lenient(raw: str) -> dict[str, Any]:
    """容错解析: 剥离 ```json 围栏 / 截取 { 到最后一个 }。"""
    from matrag.errors import JSONParseError

    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise JSONParseError("no JSON object in response", raw)
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise JSONParseError(str(exc), text[start : end + 1]) from exc


@lru_cache
def get_llm() -> LLMClient:
    return LLMClient(get_settings())
