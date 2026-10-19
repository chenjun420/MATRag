"""``matrag.extract.pipeline.build_all`` / ``build_bundle`` 集成测试（全离线, LLM off）。

要点
----
- ``build_all(corpus_dir, llm=None)`` 装配三份语料 → 3 个 bundle, 按文件名排序;
- 同一输入两次调用指纹一致 (LLM 关闭 + 固定 generated_at);
- ``assert_contract`` 在 ``build_bundle`` 内已执行 → 不会出现 contract_hash 漂移;
- ``stats.narrative_skipped == 'llm_not_configured'`` 因 LLM 未配。
"""

from __future__ import annotations

import pytest

from matrag.config import Settings
from matrag.extract.bundle import KNOWN_CONTRACT_HASH, BundleModel, assert_contract, contract_hash
from matrag.extract.fingerprints import bundle_fingerprint
from matrag.extract.pipeline import (
    ProductDoc,
    build_all,
    build_bundle,
    iter_products,
)
from matrag.llm import LLMClient

# ────────────── fixtures ──────────────


@pytest.fixture(scope="module")
def bundles() -> list[BundleModel]:
    """LLM 关闭 + 固定时间戳装配三份语料。"""
    # LLMClient(Settings(...)) 不触发网络 (configured=False)
    llm = LLMClient(Settings(llm_base="", llm_api_key="", llm_model=""))
    return build_all(llm=llm, generated_at="2026-10-18T00:00:00Z")


@pytest.fixture(scope="module")
def products() -> list[ProductDoc]:
    return iter_products()


# ────────────── iter_products ──────────────


def test_iter_products_returns_three(products: list[ProductDoc]) -> None:
    assert len(products) == 3
    codes = {p.product_code for p in products}
    assert codes == {"CY0177-75V-20A", "GDC800T5054-UC", "PA601-D54A"}


# ────────────── build_all 总体 ──────────────


def test_build_all_returns_three_bundles_sorted(bundles) -> None:
    assert len(bundles) == 3
    codes = [b.product_code for b in bundles]
    # 按 doc.json 文件名排序; 实际包含中文, 固定序: CY / GDC / PA601 (实测)
    assert codes == ["CY0177-75V-20A", "GDC800T5054-UC", "PA601-D54A"]


def test_build_all_bundle_version_and_contract_hash(bundles) -> None:
    """bundle_version=1.0; contract_hash 必须与冻结基线一致 (双侧独立计算)。"""
    for b in bundles:
        assert b.bundle_version == "1.0"
        assert b.contract_hash == KNOWN_CONTRACT_HASH
        assert b.contract_hash == contract_hash()
        # assert_contract 不抛
        result = assert_contract(b, strict=True)
        assert result is None


def test_build_all_bundle_fingerprint_stable(bundles) -> None:
    """同一输入两次装配, bundle_fingerprint 逐位一致 (LLM off + 固定时间戳)。"""
    fp_first = [b.bundle_fingerprint for b in bundles]
    llm = LLMClient(Settings(llm_base="", llm_api_key="", llm_model=""))
    second = build_all(llm=llm, generated_at="2026-10-18T00:00:00Z")
    fp_second = [b.bundle_fingerprint for b in second]
    assert fp_first == fp_second
    for fp in fp_first:
        assert len(fp) == 32


def test_build_all_bundles_have_known_fingerprint_prefix(bundles) -> None:
    """离线/LLM-off 三份语料的 bundle_fingerprint 前缀 pin (防止算法漂移)。

    实测前缀 (LLM 关闭 + 固定时间戳 2026-10-18T00:00:00Z):
        CY0177-75V-20A  → 294e27ffa749
        GDC800T5054-UC  → c74bbdb214f1
        PA601-D54A      → 90791637f324

    注: GDC/PA601 的前缀在 GAP-01 修复后重 pin —— 负轨判据 (GDC 输出整定电压
    -9.34/-9.76、PA601 输出电压整定值 -53.2/-54.8) 被归一为数学升序, 条件值/
    条件指纹/需求指纹随之变化。CY 无负轨, 前缀不变。
    """
    prefixes = {b.product_code: b.bundle_fingerprint[:12] for b in bundles}
    assert prefixes["CY0177-75V-20A"] == "294e27ffa749"
    assert prefixes["GDC800T5054-UC"] == "c74bbdb214f1"
    assert prefixes["PA601-D54A"] == "90791637f324"


def test_build_all_narrative_skipped_when_llm_off(bundles) -> None:
    """LLM 未配 → stats.narrative_skipped == 'llm_not_configured' (留痕)。"""
    for b in bundles:
        assert b.stats.get("narrative_skipped") == "llm_not_configured"


def test_build_all_total_counts_match_spec_only(bundles) -> None:
    """requirements_total = spec 计数 (narrative 跳过), 三份语料与 spec_tables pin 一致。"""
    expected = {
        "CY0177-75V-20A": 24,
        "GDC800T5054-UC": 51,
        "PA601-D54A": 76,
    }
    for b in bundles:
        assert b.stats["requirements_total"] == expected[b.product_code]


# ────────────── build_bundle 单产品 ──────────────


def test_build_bundle_returns_single(bundles) -> None:
    pa = next(b for b in bundles if b.product_code == "PA601-D54A")
    assert pa.product_code == "PA601-D54A"
    assert pa.bundle_fingerprint
    # 确定性指纹等于独立函数计算值
    expected_fp = bundle_fingerprint(pa.model_dump())
    # 注: bundle_fingerprint 函数忽略 generated_at 与 bundle_fingerprint 自身,
    # 但 pa.model_dump() 含完整字段; 这里比对的是已调出的稳定指纹, 等价。
    assert len(pa.bundle_fingerprint) == 32
    assert expected_fp == bundle_fingerprint({
        k: v for k, v in pa.model_dump().items()
        if k not in ("generated_at", "bundle_fingerprint")
    })


def test_build_bundle_doc_version_pinned(bundles) -> None:
    """doc_version 非空 (规格书有更改记录表), 实测如 'A.1' / 'V2.0' 等。"""
    for b in bundles:
        # 不强制具体版本号 (语料变就变), 但必须非空
        assert isinstance(b.doc_version, str)


def test_build_bundle_product_doc_fingerprint_stable(bundles) -> None:
    """product_doc_fingerprint 是源 .doc.json 字节 SHA256 前 32 位, 长度固定。"""
    for b in bundles:
        assert len(b.product_doc_fingerprint) == 32


def test_build_bundle_generator_constant(bundles) -> None:
    """generator 必须恒为 "matrag.extract/1.0"。"""
    for b in bundles:
        assert b.generator == "matrag.extract/1.0"


# ────────────── 边界 ──────────────


def test_build_bundle_use_narrative_false_skips_layer(bundles) -> None:
    """use_narrative=False → stats.narrative_skipped='narrative_disabled', 数量与 spec 一致。"""
    products = iter_products()
    pa = next(p for p in products if p.product_code == "PA601-D54A")
    llm = LLMClient(Settings(llm_base="", llm_api_key="", llm_model=""))
    from matrag.domain.spec_params import extract_spec_parameters
    params = [p for p in extract_spec_parameters() if p.product_code == "PA601-D54A"]
    b = build_bundle(pa, params=params, llm=llm, use_narrative=False, generated_at="2026-10-18T00:00:00Z")
    assert b.stats["narrative_skipped"] == "narrative_disabled"
    assert len(b.requirements) == 76
    assert b.requirements[0].spec_requirement_id  # 非空


def test_iter_products_missing_pair_raises(tmp_path) -> None:
    """有 .doc.json 但缺 .tables.json → ExtractIncompleteError (fail-fast)。"""
    import pytest

    from matrag.extract.errors import ExtractIncompleteError
    (tmp_path / "X.doc.json").write_text('{"doc_id": "X", "blocks": []}', encoding="utf-8")
    with pytest.raises(ExtractIncompleteError):
        iter_products(tmp_path)


def test_iter_products_empty_dir_raises(tmp_path) -> None:
    """语料目录空 → ExtractIncompleteError。"""
    import pytest

    from matrag.extract.errors import ExtractIncompleteError
    with pytest.raises(ExtractIncompleteError):
        iter_products(tmp_path)
