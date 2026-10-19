"""matrag.extract —— 规格书 → ATEStudio bundle v1.0 抽取投影 (P2 主线)。

对外主线入口: :func:`matrag.extract.pipeline.build_all`。
契约镜像: :mod:`matrag.extract.bundle` (contract 59a852c2…, 与 ATEStudio 侧独立核对过)。
"""

from matrag.extract.bundle import (
    DEFAULT_GENERATOR,
    BundleModel,
    ClauseModel,
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
from matrag.extract.errors import (
    ExtractError,
    ExtractIncompleteError,
    KindViolationError,
    SemanticGuardError,
)
from matrag.extract.kinds import KIND_UNITS, KNOWN_KIND_SIDES, kinds_for_side, validate_clause_kind
from matrag.extract.pipeline import ProductDoc, build_all, build_bundle, iter_products

__all__ = [
    "DEFAULT_GENERATOR",
    "KIND_UNITS",
    "KNOWN_KIND_SIDES",
    "BundleModel",
    "ClauseModel",
    "ExcludedModel",
    "ExcludedScenarioModel",
    "ExtractError",
    "ExtractIncompleteError",
    "KindViolationError",
    "LimitModel",
    "ProductDoc",
    "RequirementModel",
    "ReviewModel",
    "ScenarioModel",
    "SemanticGuardError",
    "assert_contract",
    "build_all",
    "build_bundle",
    "contract_hash",
    "iter_products",
    "kinds_for_side",
    "now_utc_iso",
    "validate_clause_kind",
]
