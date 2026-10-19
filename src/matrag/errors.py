"""matrag 专用异常。业务代码不得裸吞 Exception 后静默降级。"""

from __future__ import annotations


class MatragError(RuntimeError):
    """所有 matrag 运行时错误的基类。"""


class JSONParseError(MatragError):
    """LLM 输出不是合法 JSON。"""

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


class ParseGateError(MatragError):
    """解析对账闸门未通过（文档不得进入索引）。"""


class ProviderNotConfiguredError(MatragError):
    """Provider（解析器/LLM/Embedding）未配置。"""
