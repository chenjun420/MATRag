"""装配层: 三份语料 → ATEStudio bundle v1.0 (contract 59a852c2…)。

职责
----
- 逐产品加载 ``*.doc.json`` (blocks) 与全量 spec_params (来自 ``*.tables.json``);
- 确定性投影 :func:`matrag.extract.spec_tables.build_from_tables`;
- 叙述提案 :func:`matrag.extract.narrative.extract_narrative` (draft 红线; LLM 未配置时
  自动跳过并留痕 ``narrative_skipped``);
- 汇总 requirements/excluded/review 与 stats, 计算 product_doc_fingerprint /
  bundle_fingerprint, 填契约 hash 并 ``assert_contract`` fail-fast;
- 出口: :class:`matrag.extract.bundle.BundleModel` 列表 (逐产品一个 bundle)。

doc_version: 取自"更改记录"表 (表头同时含 文件编号+版本号 的表, 取最后一个非空版本格);
无此表的产品留空字符串 (契约允许, max_length=32)。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from matrag.domain.spec_params import DEFAULT_CORPUS_DIR, SpecParameter, extract_spec_parameters
from matrag.extract.bundle import (
    DEFAULT_GENERATOR,
    BundleModel,
    assert_contract,
    contract_hash,
    now_utc_iso,
)
from matrag.extract.errors import ExtractIncompleteError
from matrag.extract.fingerprints import bundle_fingerprint, file_fingerprint
from matrag.extract.narrative import NarrativeResult, extract_narrative
from matrag.extract.patterns import normalize_text
from matrag.extract.spec_tables import build_from_tables
from matrag.llm import LLMClient

__all__ = ["ProductDoc", "build_all", "build_bundle", "iter_products"]

#: 契约主版本 (消费侧 aterag_bundle.py 冻结)。
_BUNDLE_VERSION = "1.0"

#: 版本格取值白名单 (宽松): 字母数字与 . _ / - 空格。
_VERSION_CELL = re.compile(r"[A-Za-z0-9._/\- ]{1,32}")


@dataclass(frozen=True)
class ProductDoc:
    """一个产品的语料文件对。"""

    name: str  # 文件 stem (含中文标题)
    product_code: str  # doc_id 首段, 如 PA601-D54A
    doc_id: str
    doc_json: Path
    tables_json: Path


def iter_products(corpus_dir: Path | str = DEFAULT_CORPUS_DIR) -> list[ProductDoc]:
    """遍历语料目录, 配对 ``*.doc.json`` 与同名 ``*.tables.json`` (缺失即 fail-fast)。"""
    root = Path(corpus_dir)
    docs = sorted(root.glob("*.doc.json"))
    if not docs:
        raise ExtractIncompleteError(f"语料目录无 doc.json: {root}")
    out: list[ProductDoc] = []
    for doc_path in docs:
        stem = doc_path.name[: -len(".doc.json")]
        tables_path = doc_path.with_name(stem + ".tables.json")
        if not tables_path.is_file():
            raise ExtractIncompleteError(f"缺少配对表文件: {tables_path}")
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        doc_id = str(doc.get("doc_id") or stem).strip() or stem
        product_code = doc_id.split(" ")[0].strip() or stem
        out.append(
            ProductDoc(
                name=stem,
                product_code=product_code,
                doc_id=doc_id,
                doc_json=doc_path,
                tables_json=tables_path,
            )
        )
    return out


def _load_blocks(doc_json: Path) -> list[dict[str, Any]]:
    doc = json.loads(doc_json.read_text(encoding="utf-8"))
    blocks = doc.get("blocks")
    if not isinstance(blocks, list):
        raise ExtractIncompleteError(f"doc.json 缺少 blocks 数组: {doc_json}")
    return blocks


def _doc_version(blocks: Sequence[dict[str, Any]]) -> str:
    """更改记录表 → 版本号 (最后一个可解析的版本格); 无表或无值返回 ""。"""
    for b in blocks:
        if not isinstance(b, dict) or b.get("kind") != "table":
            continue
        table = b.get("table") or {}
        headers = [normalize_text(str(h)) for h in (table.get("headers") or [])]
        rev_idx = next((i for i, h in enumerate(headers) if "版本号" in h), None)
        if rev_idx is None or not any("文件编号" in h for h in headers):
            continue
        for row in reversed(table.get("rows") or []):
            cell = normalize_text(str(row[rev_idx]).split("<br>")[0]) if rev_idx < len(row) else ""
            if not cell or cell in {"-", "—", "–"}:
                continue
            if _VERSION_CELL.fullmatch(cell):
                return cell
    return ""


def _cond_fingerprints(result_requirements: Sequence[Any]) -> frozenset[str]:
    """需求集里全部条件指纹 (供叙事侧去重: 规格书已写死的条件不再出提案)。"""
    return frozenset(
        clause.cond_fingerprint
        for req in result_requirements
        for clause in (*req.input_conditions, *req.output_conditions)
    )


def build_bundle(
    product: ProductDoc,
    *,
    params: Sequence[SpecParameter],
    llm: LLMClient | None = None,
    use_narrative: bool = True,
    generated_at: str | None = None,
) -> BundleModel:
    """装配单个产品的 bundle (fail-fast: 契约校验不过直接抛)。"""
    spec = build_from_tables(params)
    blocks = _load_blocks(product.doc_json)
    if use_narrative:
        narrative = extract_narrative(
            blocks,
            product_code=product.product_code,
            llm=llm,
            exclude_fingerprints=_cond_fingerprints(spec.requirements),
        )
    else:
        narrative = NarrativeResult(skipped_reason="narrative_disabled")

    requirements = [*spec.requirements, *narrative.requirements]
    review = [*spec.review, *narrative.review]
    stats: dict[str, Any] = {
        "spec": dict(spec.stats),
        "narrative": dict(narrative.stats),
        "requirements_total": len(requirements),
        "excluded_total": len(spec.excluded),
        "review_total": len(review),
    }
    if narrative.skipped_reason:
        stats["narrative_skipped"] = narrative.skipped_reason

    draft = BundleModel(
        bundle_version=_BUNDLE_VERSION,
        contract_hash=contract_hash(),
        product_code=product.product_code,
        doc_version=_doc_version(blocks),
        product_doc_fingerprint=file_fingerprint(product.doc_json),
        generated_at=generated_at or now_utc_iso(),
        generator=DEFAULT_GENERATOR,
        bundle_fingerprint="",
        requirements=requirements,
        excluded=spec.excluded,
        review=review,
        excluded_scenarios=[],
        stats=stats,
        removed_requirement_codes=[],
    )
    bundle = draft.model_copy(update={"bundle_fingerprint": bundle_fingerprint(draft.model_dump())})
    assert_contract(bundle, strict=True)
    return bundle


def build_all(
    corpus_dir: Path | str = DEFAULT_CORPUS_DIR,
    *,
    llm: LLMClient | None = None,
    use_narrative: bool = True,
    generated_at: str | None = None,
) -> list[BundleModel]:
    """对整个语料目录出 bundle 列表 (按文件名排序; 同一 generated_at 便于幂等比对)。"""
    timestamp = generated_at or now_utc_iso()
    all_params = extract_spec_parameters(corpus_dir)
    bundles: list[BundleModel] = []
    for product in iter_products(corpus_dir):
        params = [p for p in all_params if p.product_code == product.product_code]
        bundles.append(
            build_bundle(
                product,
                params=params,
                llm=llm,
                use_narrative=use_narrative,
                generated_at=timestamp,
            )
        )
    return bundles
