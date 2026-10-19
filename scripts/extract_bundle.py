"""P2 导出脚本: 规格书语料 → ATEStudio bundle v1.0 JSON。

用法:
    python -m scripts.extract_bundle                        # 全部产品 → data/exports
    python -m scripts.extract_bundle --product PA601-D54A   # 单产品
    python -m scripts.extract_bundle --no-narrative         # 只出确定性部分 (离线可跑)
    python -m scripts.extract_bundle --json                 # 机器可读摘要

行为:
1. 遍历 data/corpus 的产品 (iter_products), 逐产品装配 bundle (pipeline.build_bundle)。
2. LLM 未配置时叙述层自动跳过并留痕 (stats.narrative_skipped), 不影响确定性投影。
3. 产物写 {out}/{product_code}.bundle.json (UTF-8, indent=2), 供消费侧导入与
   scripts.verify_bundle_ate 验收使用。
4. --generated-at 可固定时间戳便于幂等复跑; bundle_fingerprint 本身已忽略时间戳。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from matrag.domain.spec_params import DEFAULT_CORPUS_DIR, extract_spec_parameters
from matrag.extract.bundle import BundleModel
from matrag.extract.pipeline import build_all, build_bundle, iter_products

DEFAULT_OUT = Path("data/exports")


def _write_bundle(bundle: BundleModel, out_dir: Path) -> Path:
    """写出单个 bundle JSON (覆盖写, ensure_ascii=False 保持可读中文)。"""
    path = out_dir / f"{bundle.product_code}.bundle.json"
    path.write_text(
        json.dumps(bundle.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _build(args: argparse.Namespace) -> list[BundleModel]:
    """按参数装配 bundle 列表 (单产品过滤或全量)。"""
    corpus = Path(args.corpus)
    if not args.product:
        return build_all(corpus, use_narrative=not args.no_narrative, generated_at=args.generated_at)
    products = [p for p in iter_products(corpus) if p.product_code == args.product]
    if not products:
        raise SystemExit(f"语料目录中找不到产品: {args.product!r} (corpus={corpus})")
    all_params = extract_spec_parameters(corpus)
    bundles: list[BundleModel] = []
    for product in products:
        params = [p for p in all_params if p.product_code == product.product_code]
        bundles.append(
            build_bundle(
                product,
                params=params,
                use_narrative=not args.no_narrative,
                generated_at=args.generated_at,
            )
        )
    return bundles


def _summary_row(bundle: BundleModel, path: Path) -> dict[str, object]:
    stats = bundle.stats
    return {
        "product_code": bundle.product_code,
        "requirements": stats.get("requirements_total"),
        "excluded": stats.get("excluded_total"),
        "review": stats.get("review_total"),
        "doc_version": bundle.doc_version,
        "bundle_fingerprint": bundle.bundle_fingerprint,
        "narrative_skipped": stats.get("narrative_skipped"),
        "path": str(path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="规格书语料 → ATEStudio bundle v1.0 JSON")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS_DIR), help="语料目录 (默认 data/corpus)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="产物目录 (默认 data/exports)")
    parser.add_argument("--product", default="", help="只导出指定 product_code (如 PA601-D54A)")
    parser.add_argument("--no-narrative", action="store_true", help="跳过叙述层 LLM 提案 (纯确定性)")
    parser.add_argument("--generated-at", default=None, help="固定 generated_at (ISO 字符串, 幂等复跑用)")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON 摘要")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    bundles = _build(args)

    rows: list[dict[str, object]] = []
    for bundle in bundles:
        path = _write_bundle(bundle, out_dir)
        row = _summary_row(bundle, path)
        rows.append(row)
        if not args.json:
            skipped = f" narrative={row['narrative_skipped']}" if row["narrative_skipped"] else ""
            print(
                f"{bundle.product_code}: req={row['requirements']} excl={row['excluded']} "
                f"review={row['review']} doc_version={bundle.doc_version!r}{skipped}"
            )
            print(f"  → {path}  fp={(bundle.bundle_fingerprint or '')[:12]}…")

    if args.json:
        print(json.dumps({"out_dir": str(out_dir), "products": rows}, ensure_ascii=False, indent=2))
    elif not bundles:
        print("(无产品可导出)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
