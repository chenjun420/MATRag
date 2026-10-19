"""抽取投影层专用异常 (fail-fast 语义, 不允许静默降级)。"""

from __future__ import annotations

from matrag.errors import MatragError


class ExtractError(MatragError):
    """抽取投影层错误的基类。"""


class KindViolationError(ExtractError):
    """条款 kind 不在 ATEStudio 绑定表的词表内。

    词表是消费方 (ATEStudio 规划器) 的绑定键, 多一个词 = 那条条件永远排不出
    步骤且不报错 —— 所以在本层直接拒绝, 而不是把问题留到产线。
    """

    def __init__(self, kind: str, side: str, *, origin: str = "") -> None:
        where = f" (来源: {origin})" if origin else ""
        super().__init__(
            f"kind 不在绑定表词表内: side={side} kind={kind!r}{where}; "
            "请先与 ATEStudio config/test_method_bindings.yaml 对齐"
        )
        self.kind = kind
        self.side = side


class ExtractIncompleteError(ExtractError):
    """抽取结果不满足导出前置条件 (缺编号 / 编号重复 / 必填为空等)。"""


class SemanticGuardError(ExtractError):
    """LLM 产出违反事实护栏 (数值不在原文 / 方法 id 不存在 / 非法 kind 等)。"""
