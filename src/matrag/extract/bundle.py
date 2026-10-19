"""ATERag bundle v1.0 契约镜像 (生产侧) —— 与 ATEStudio 冻结契约逐字对齐。

背景与纪律
----------
ATEStudio 的 ``src/ate_cloud/schemas/aterag_bundle.py`` 是消费侧对同一契约的
**独立再声明**: 两仓独立发布, 任何一方 import 另一方都会把每次契约变更变成
两仓协同发版。耦合靠 ``contract_hash`` 保持诚实 —— 两侧各自从自己的 Pydantic
schema 推导指纹, 不一致时在**第一次导入**就带着两侧指纹报错, 而不是等字段
静默丢失或线上 4xx。

因此本模块的模型定义 (字段名/类型/默认值/长度与数值约束/模型名) 必须与消费侧
逐字一致; 只允许 docstring/注释不同 —— 那些不影响指纹 (见
:data:`_VOLATILE_SCHEMA_KEYS`)。改动任何一个字节的约束 = 契约漂移, 指纹立刻
变化, 测试 ``test_extract_bundle.py::test_contract_hash_matches_frozen_baseline``
会红。

版本规则 (与消费侧一致):

* 增加可选字段 → minor +1, major 不变;
* 增加必填字段 / 删除字段 / 语义变更 → major +1。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 必须等于消费侧 ``CONTRACT_MAJOR``。
CONTRACT_MAJOR = 1

#: 必须等于消费侧 ``BUNDLE_VERSION``。
BUNDLE_VERSION = "1.0"

#: 契约冻结时记录的基线指纹 (消费侧 header 中同名常量)。
KNOWN_CONTRACT_HASH = "59a852c26ca277e4141c6a60ae723b20"

#: 导出文件的默认 generator 标识 (写进 bundle.generator)。
DEFAULT_GENERATOR = "matrag.extract/1.0"

ContractStatus = Literal["approved", "draft"]


class ClauseModel(BaseModel):
    """一条条件条款 (输入激励或输出测量)。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(..., min_length=1, max_length=64)
    text: str = Field("", max_length=2000)
    role: Literal["input", "output"]
    value: dict[str, Any] | None = None
    source: str = Field("notes", max_length=32)
    confidence: str = Field("rule", max_length=32)
    status: ContractStatus = "approved"
    method_ref: str = Field("", max_length=64)
    cond_fingerprint: str = Field("", max_length=64)


class LimitModel(BaseModel):
    """一条限值。min/typ/max 允许缺失 —— 单边是常态。"""

    model_config = ConfigDict(extra="forbid")

    rail: str = Field("", max_length=32)
    qualifier: str = Field("", max_length=64)
    min: float | None = None
    typ: float | None = None
    max: float | None = None
    unit: str = Field("", max_length=32)


class ScenarioModel(BaseModel):
    """一个可执行的工况组合。"""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(..., min_length=1, max_length=200)
    seq: int = Field(..., ge=0)
    name: str = Field("", max_length=200)
    rail: str = Field("", max_length=32)
    bindings: dict[str, str] = Field(default_factory=dict)
    derived: dict[str, float] = Field(default_factory=dict)
    basis: str = Field("", max_length=200)
    source: str = Field("spec", max_length=32)


class RequirementModel(BaseModel):
    """一条需求及其条件、工况与判定。"""

    model_config = ConfigDict(extra="forbid")

    requirement_code: str = Field(..., min_length=1, max_length=200)
    spec_requirement_id: str = Field("", max_length=100)
    title: str = Field(..., min_length=1, max_length=255)
    section_path: str = Field("", max_length=64)
    role: str = Field("", max_length=32)
    description: str = Field("", max_length=8000)
    notes: str = Field("", max_length=2000)
    req_fingerprint: str = Field("", max_length=64)
    flags: list[str] = Field(default_factory=list)
    input_conditions: list[ClauseModel] = Field(default_factory=list)
    output_conditions: list[ClauseModel] = Field(default_factory=list)
    limits: list[LimitModel] = Field(default_factory=list)
    scenarios: list[ScenarioModel] = Field(default_factory=list)
    one_sided: dict[str, Any] = Field(default_factory=dict)
    assessment: dict[str, str] = Field(default_factory=dict)

    @field_validator("scenarios")
    @classmethod
    def _seq_unique(cls, v: list[ScenarioModel]) -> list[ScenarioModel]:
        """同一需求内场景 seq 唯一 (case_code = ``{code}-S{seq:03d}``)。"""
        seqs = [s.seq for s in v]
        if len(seqs) != len(set(seqs)):
            raise ValueError(f"场景序号重复: {sorted({s for s in seqs if seqs.count(s) > 1})}")
        ids = [s.scenario_id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("场景标识重复")
        return v


class ExcludedModel(BaseModel):
    """被排除的行 —— reason 必填 (排除必须可审计)。"""

    model_config = ConfigDict(extra="forbid")

    req_id: str = Field("", max_length=100)
    title: str = Field("", max_length=255)
    section_path: str = Field("", max_length=64)
    reason: str = Field("", max_length=500)
    matched_word: str = Field("", max_length=64)
    notes: str = Field("", max_length=2000)


class ReviewModel(BaseModel):
    """待复核项。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(..., max_length=64)
    section_path: str = Field("", max_length=64)
    heading: str = Field("", max_length=255)
    detail: str = Field("", max_length=1000)
    hint: str = Field("", max_length=1000)
    method_ref: str = Field("", max_length=64)


class ExcludedScenarioModel(BaseModel):
    """被排除 (不可执行) 的场景组合。"""

    model_config = ConfigDict(extra="forbid")

    req_id: str = Field("", max_length=100)
    title: str = Field("", max_length=255)
    bindings: dict[str, str] = Field(default_factory=dict)
    reason: str = Field("", max_length=200)
    rule_id: str = Field("", max_length=64)


class BundleModel(BaseModel):
    """整个 bundle —— 两仓之间唯一的数据面产物。"""

    model_config = ConfigDict(extra="forbid")

    bundle_version: str = Field(...)
    contract_hash: str = Field(..., max_length=64)
    product_code: str = Field(..., min_length=1, max_length=100)
    doc_version: str = Field("", max_length=32)
    product_doc_fingerprint: str = Field("", max_length=64)
    generated_at: str = Field("", max_length=64)
    generator: str = Field("", max_length=64)
    bundle_fingerprint: str = Field("", max_length=64)
    requirements: list[RequirementModel] = Field(default_factory=list)
    excluded: list[ExcludedModel] = Field(default_factory=list)
    review: list[ReviewModel] = Field(default_factory=list)
    excluded_scenarios: list[ExcludedScenarioModel] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    removed_requirement_codes: list[str] = Field(default_factory=list)

    @field_validator("bundle_version")
    @classmethod
    def _version_ok(cls, v: str) -> str:
        try:
            major = int(v.split(".")[0])
        except (ValueError, IndexError) as e:
            raise ValueError(f"bundle_version 格式非法: {v!r}") from e
        if major != CONTRACT_MAJOR:
            raise ValueError(f"bundle 主版本不兼容: 导入方为 {CONTRACT_MAJOR}.x, 收到 {v}")
        return v

    @field_validator("requirements")
    @classmethod
    def _req_codes_unique(cls, v: list[RequirementModel]) -> list[RequirementModel]:
        codes = [r.requirement_code for r in v]
        if len(codes) != len(set(codes)):
            raise ValueError(f"requirement_code 重复: {sorted({c for c in codes if codes.count(c) > 1})}")
        return v


#: 指纹计算时忽略的 schema 键: 纯描述性文本。
_VOLATILE_SCHEMA_KEYS = frozenset({"title", "description", "examples", "$comment"})


def _stable_repr(node: Any) -> str:
    """去掉易变描述后的确定性序列化 (两侧必须逐字节一致)。

    映射按键排序、结构化渲染 (而不是对任意 dict 调 ``json.dumps``), 保证
    两侧产生的输入字节相同。
    """
    if isinstance(node, Mapping):
        items = {k: v for k, v in node.items() if k not in _VOLATILE_SCHEMA_KEYS}
        return "{" + ",".join(f"{k}:{_stable_repr(items[k])}" for k in sorted(items)) + "}"
    if isinstance(node, list | tuple):
        return "[" + ",".join(_stable_repr(x) for x in node) + "]"
    return json.dumps(node, sort_keys=True, ensure_ascii=False)


def contract_hash() -> str:
    """契约定义的指纹 —— 在本侧独立计算。

    与生产侧 (消费方的说法里, 本模块即生产侧) 的哈希一致, 当且仅当两侧
    schema 声明一致。
    """
    schema = BundleModel.model_json_schema()
    return hashlib.sha256(_stable_repr(schema).encode("utf-8")).hexdigest()[:32]


class ContractMismatchError(RuntimeError):
    """生产/消费契约漂移 —— 拒绝导出/导入而不是猜。"""

    def __init__(self, received: str, expected: str) -> None:
        super().__init__(
            f"契约不匹配: bundle contract_hash={received}, 本侧={expected}。"
            " 两侧 schema 已漂移, 请先对齐 extract/bundle.py 与 schemas/aterag_bundle.py"
            " (勿用关闭校验的方式绕过 —— 那会让缺失的字段静默丢失)"
        )
        self.received = received
        self.expected = expected


def assert_contract(bundle: BundleModel, *, strict: bool = True) -> None:
    """导出/加载前的契约自校验闸门。"""
    mine = contract_hash()
    if bundle.contract_hash == mine:
        return
    if strict:
        raise ContractMismatchError(bundle.contract_hash, mine)


def now_utc_iso() -> str:
    """生成时间戳 (秒精度, UTC, ISO 8601)。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "BUNDLE_VERSION",
    "CONTRACT_MAJOR",
    "DEFAULT_GENERATOR",
    "KNOWN_CONTRACT_HASH",
    "BundleModel",
    "ClauseModel",
    "ContractMismatchError",
    "ExcludedModel",
    "ExcludedScenarioModel",
    "LimitModel",
    "RequirementModel",
    "ReviewModel",
    "ScenarioModel",
    "assert_contract",
    "contract_hash",
    "now_utc_iso",
]
