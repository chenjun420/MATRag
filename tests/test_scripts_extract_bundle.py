"""``scripts/extract_bundle.py`` CLI 的离线 smoke 测试（不动 data/exports 真盘）。

只测最关键的写盘路径: ``--corpus`` 指定真实语料目录, ``--out`` 指定 tmp_path,
跑完断言:
- exit code == 0;
- 写出三份 bundle JSON, 每个能用 :class:`BundleModel` 重验;
- 每份含 ``bundle_fingerprint`` 与 ``product_code``。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from matrag.extract.bundle import BundleModel

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = REPO_ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / "python.exe"


def _run(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """用仓库 venv 直接跑脚本; stdout/stderr 用文本, 失败时抛。"""
    return subprocess.run(
        [str(PYTHON), "-m", "scripts.extract_bundle", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )


@pytest.mark.skipif(not PYTHON.exists(), reason="repo venv 缺失")
def test_cli_writes_three_bundles_and_validates(tmp_path: Path) -> None:
    """--corpus 真实语料 + --out tmp_path → 三份 bundle JSON 可被 BundleModel 重验。"""
    proc = _run([
        "--corpus", "data/corpus",
        "--out", str(tmp_path),
        "--no-narrative",  # LLM off, 离线可跑
        "--generated-at", "2026-10-18T00:00:00Z",
    ])
    assert proc.returncode == 0

    written = sorted(tmp_path.glob("*.bundle.json"))
    codes = {p.stem.removesuffix(".bundle") for p in written}
    assert codes == {"CY0177-75V-20A", "GDC800T5054-UC", "PA601-D54A"}

    for path in written:
        raw = json.loads(path.read_text(encoding="utf-8"))
        # 必填字段
        assert raw["bundle_version"] == "1.0"
        assert raw["product_code"]
        assert raw["contract_hash"]
        assert raw["bundle_fingerprint"]
        # BundleModel 重验 (pydantic strict, extra=forbid)
        b = BundleModel.model_validate(raw)
        assert b.bundle_fingerprint == raw["bundle_fingerprint"]


@pytest.mark.skipif(not PYTHON.exists(), reason="repo venv 缺失")
def test_cli_single_product_filters(tmp_path: Path) -> None:
    """--product PA601-D54A → 仅写一份。"""
    proc = _run([
        "--corpus", "data/corpus",
        "--out", str(tmp_path),
        "--no-narrative",
        "--product", "PA601-D54A",
        "--generated-at", "2026-10-18T00:00:00Z",
    ])
    assert proc.returncode == 0
    written = list(tmp_path.glob("*.bundle.json"))
    assert len(written) == 1
    assert written[0].stem == "PA601-D54A.bundle"


@pytest.mark.skipif(not PYTHON.exists(), reason="repo venv 缺失")
def test_cli_unknown_product_exits_nonzero(tmp_path: Path) -> None:
    """--product XYZ → SystemExit (非 0), stderr 报找不到。"""
    import subprocess
    proc = subprocess.run(
        [str(PYTHON), "-m", "scripts.extract_bundle",
         "--corpus", "data/corpus",
         "--out", str(tmp_path),
         "--product", "NOPE-XYZ"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "找不到产品" in proc.stderr or "找不到产品" in proc.stdout


@pytest.mark.skipif(not PYTHON.exists(), reason="repo venv 缺失")
def test_cli_json_summary(tmp_path: Path) -> None:
    """--json → stdout 是合法 JSON, 含 products 数组。"""
    proc = _run([
        "--corpus", "data/corpus",
        "--out", str(tmp_path),
        "--no-narrative",
        "--json",
        "--generated-at", "2026-10-18T00:00:00Z",
    ])
    assert proc.returncode == 0
    summary = json.loads(proc.stdout)
    assert summary["out_dir"] == str(tmp_path)
    assert len(summary["products"]) == 3
    codes = {row["product_code"] for row in summary["products"]}
    assert codes == {"CY0177-75V-20A", "GDC800T5054-UC", "PA601-D54A"}
    # 每行含 requirements/excluded/review 与 bundle_fingerprint
    for row in summary["products"]:
        assert isinstance(row["requirements"], int) and row["requirements"] > 0
        assert "bundle_fingerprint" in row and len(row["bundle_fingerprint"]) == 32
