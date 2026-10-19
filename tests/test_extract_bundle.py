"""bundle 契约镜像的测试 (全离线, 不依赖 ATEStudio 侧)。

要点
----
- contract_hash 冻结在 ``KNOWN_CONTRACT_HASH`` (``59a852c2…``), 改 schema 立刻红。
- 所有 ``*Model`` 类的 ``extra="forbid"`` 必须真的生效 (拒收额外字段)。
- ``assert_contract`` 在 hash 不一致时抛 ``ContractMismatchError``, strict=False 时静默。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from matrag.extract.bundle import (
    BUNDLE_VERSION,
    CONTRACT_MAJOR,
    DEFAULT_GENERATOR,
    KNOWN_CONTRACT_HASH,
    BundleModel,
    ClauseModel,
    ContractMismatchError,
    ExcludedModel,
    ExcludedScenarioModel,
    LimitModel,
    RequirementModel,
    ReviewModel,
    ScenarioModel,
    assert_contract,
    contract_hash,
    now_utc_iso,
)


def test_contract_hash_matches_frozen_baseline() -> None:
    """contract_hash 与冻结基线一致 —— 任何 schema 字段漂移都会立刻红。"""
    assert KNOWN_CONTRACT_HASH == "59a852c26ca277e4141c6a60ae723b20"
    assert contract_hash() == KNOWN_CONTRACT_HASH
    assert len(contract_hash()) == 32


def test_bundle_version_and_generator_constants() -> None:
    """契约版本号与生成器标识冻结。"""
    assert BUNDLE_VERSION == "1.0"
    assert CONTRACT_MAJOR == 1
    assert DEFAULT_GENERATOR == "matrag.extract/1.0"


def test_clause_model_extra_forbid() -> None:
    """ClauseModel 必须拒绝未声明字段 (防消费侧悄悄多塞字段)。"""
    c = ClauseModel(kind="output_voltage", role="output")
    with pytest.raises(ValidationError):
        ClauseModel(kind="output_voltage", role="output", surprise=1)  # type: ignore[call-arg]
    assert c.kind == "output_voltage"


def test_clause_model_value_shape() -> None:
    """value 允许 None 或 dict, 缺 key 省略 (event 类走 {"event": ...})。"""
    c1 = ClauseModel(kind="protection_action", role="output", value={"event": "短路"})
    assert c1.value == {"event": "短路"}
    c2 = ClauseModel(kind="output_voltage", role="output", value=None)
    assert c2.value is None


def test_limit_model_min_typ_max_partial() -> None:
    """LimitModel 允许单边 (单边是常态, min/typ/max 可为 None)。"""
    only_min = LimitModel(rail="54V", qualifier="", min=53.5, unit="V")
    assert only_min.max is None
    only_typ = LimitModel(rail="54V", typ=54.0, unit="V")
    assert only_typ.min is None and only_typ.max is None


def test_scenario_model_required_fields() -> None:
    """ScenarioModel: scenario_id 与 seq 必填, seq ≥ 0。"""
    ScenarioModel(scenario_id="X-S001", seq=0, name="", rail="")
    with pytest.raises(ValidationError):
        ScenarioModel(scenario_id="", seq=0)  # type: ignore[call-arg]


def test_requirement_model_seq_unique_validator() -> None:
    """同一需求内场景 seq 与 scenario_id 必须唯一。"""
    sc = ScenarioModel(scenario_id="X-S001", seq=1)
    sc2 = ScenarioModel(scenario_id="X-S002", seq=2)
    dup_seq = ScenarioModel(scenario_id="X-S003", seq=1)
    with pytest.raises(ValidationError):
        RequirementModel(
            requirement_code="SR-X-1", title="T",
            scenarios=[sc, dup_seq],
        )
    dup_id = ScenarioModel(scenario_id="X-S001", seq=2)
    with pytest.raises(ValidationError):
        RequirementModel(
            requirement_code="SR-X-1", title="T",
            scenarios=[sc, dup_id],
        )
    # 合法用例: 不抛
    ok = RequirementModel(
        requirement_code="SR-X-1", title="T",
        scenarios=[sc, sc2],
    )
    assert len(ok.scenarios) == 2


def test_requirement_model_requirement_code_required() -> None:
    """requirement_code 与 title 必填; 空串拒绝。"""
    with pytest.raises(ValidationError):
        RequirementModel(requirement_code="", title="T")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        RequirementModel(requirement_code="X", title="")  # type: ignore[call-arg]


def test_excluded_and_review_and_excluded_scenario_extra_forbid() -> None:
    """三个辅助模型也必须 extra=forbid。"""
    e = ExcludedModel(reason="非产测范围")
    with pytest.raises(ValidationError):
        ExcludedModel(reason="X", whoops=1)  # type: ignore[call-arg]
    ReviewModel(kind="narrative_value_dropped")
    with pytest.raises(ValidationError):
        ReviewModel(kind="X", whoops=1)  # type: ignore[call-arg]
    ExcludedScenarioModel(reason="")
    with pytest.raises(ValidationError):
        ExcludedScenarioModel(reason="", whoops=1)  # type: ignore[call-arg]
    assert e.reason == "非产测范围"


def test_bundle_model_extra_forbid_and_version_major() -> None:
    """BundleModel: extra=forbid; bundle_version 主版本不一致即拒。"""
    BundleModel(bundle_version="1.0", contract_hash=contract_hash(), product_code="X")
    with pytest.raises(ValidationError):
        BundleModel(
            bundle_version="1.0", contract_hash=contract_hash(), product_code="X",
            surprise=1,  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        BundleModel(bundle_version="2.0", contract_hash=contract_hash(), product_code="X")
    # 版本字符串非法 → ValueError
    with pytest.raises(ValidationError):
        BundleModel(bundle_version="abc", contract_hash=contract_hash(), product_code="X")


def test_bundle_model_unique_requirement_codes() -> None:
    """BundleModel 内 requirement_code 必须唯一。"""
    req = RequirementModel(requirement_code="SR-X-1", title="T")
    req2 = RequirementModel(requirement_code="SR-X-1", title="T2")
    with pytest.raises(ValidationError):
        BundleModel(
            bundle_version="1.0", contract_hash=contract_hash(), product_code="X",
            requirements=[req, req2],
        )


def test_assert_contract_strict_raises_on_mismatch() -> None:
    """assert_contract 在 contract_hash 不一致时 strict=True 直接抛。"""
    bad = BundleModel(
        bundle_version="1.0",
        contract_hash="0" * 32,
        product_code="X",
    )
    with pytest.raises(ContractMismatchError) as excinfo:
        assert_contract(bad, strict=True)
    assert excinfo.value.expected == contract_hash()
    assert excinfo.value.received == "0" * 32


def test_assert_contract_strict_false_silent_on_mismatch() -> None:
    """strict=False 时不抛 (但消费侧默认走 strict=True, 仅用于运维通道)。"""
    bad = BundleModel(
        bundle_version="1.0",
        contract_hash="0" * 32,
        product_code="X",
    )
    result = assert_contract(bad, strict=False)
    assert result is None


def test_assert_contract_matches_returns_none() -> None:
    """contract_hash 一致时 assert_contract 直接返回 None。"""
    good = BundleModel(
        bundle_version="1.0",
        contract_hash=contract_hash(),
        product_code="X",
    )
    assert assert_contract(good, strict=True) is None


def test_now_utc_iso_format() -> None:
    """now_utc_iso 必须形如 YYYY-MM-DDTHH:MM:SSZ。"""
    ts = now_utc_iso()
    assert len(ts) == 20
    assert ts.endswith("Z")
    assert ts[4] == "-" and ts[7] == "-" and ts[10] == "T" and ts[13] == ":" and ts[16] == ":"


def test_contract_hash_deterministic() -> None:
    """contract_hash 两次调用逐位一致 (不依赖时间/环境)。"""
    assert contract_hash() == contract_hash()
