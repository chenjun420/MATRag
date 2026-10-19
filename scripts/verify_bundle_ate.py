"""P2 验收脚本: bundle JSON 用消费侧 (ATEStudio) 源码实现做离线 dry-run。

用法:
    python -m scripts.verify_bundle_ate --bundle data/exports/PA601-D54A.bundle.json
    python -m scripts.verify_bundle_ate --all                    # data/exports 下全部 bundle
    python -m scripts.verify_bundle_ate --all --json

行为:
1. 把 {atestudio}/src (及同级 ATEPlatform/src, 若存在) 注入 sys.path, 直接复用消费侧实现
   (契约模型 / 规划器 / 脚本发射): BundleModel、contract_hash、load_bindings、plan_flow、
   plan_gaps —— 不复制其逻辑, 消费侧源码为准。
2. 检查项 (镜像 ATEStudio/scripts/verify_e2e.py):
   - contract_hash 三向一致 (冻结基准 / bundle / 消费侧现算);
   - 计划非空 (step_count>0);
   - draft 红线: draft 条件不得进入执行序列, pending 数一致;
   - plan_gaps 为空 (measure 类步骤必须有数值判据);
   - 步骤 id 全局唯一;
   - 计划 YAML 可被平台解析器 YamlParser 解析 (失败即 FAIL);
   - advisory (非致命): 单台节拍 total_settle_s≤1800s、unmapped 条件数。
3. 计划 YAML 落盘 {plan_dir}/{product_code}.plan.yaml 供人工复核。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:  # 在消费侧 venv 下运行时可能不可用: matrag 侧 hash 仅作三方核对, 缺失时跳过。
    from matrag.extract.bundle import contract_hash as matrag_contract_hash
except Exception:  # noqa: BLE001 - 可选依赖, 任何导入失败都降级为跳过
    matrag_contract_hash = None  # type: ignore[assignment]

#: 冻结基准 (与消费侧 aterag_bundle.py 双向核对过的契约 hash)。
FROZEN_CONTRACT_HASH = "59a852c26ca277e4141c6a60ae723b20"

DEFAULT_ATESTUDIO = Path(r"F:\Workspace\ATEStudio")
DEFAULT_BUNDLE_DIR = Path("data/exports")
DEFAULT_PLAN_DIR = Path("data/reports/plans")


def _load_consumer(atestudio_dir: Path) -> SimpleNamespace:
    """注入消费侧源码并返回其 API 集合 (fail-fast: 目录/导入错误直接抛)。

    Phase D.1 起 ATEStudio ``ate_cloud/__init__.py`` 已与 ``main`` 解耦:
    导入 ``ate_cloud`` / ``ate_cloud.schemas`` / ``ate_cloud.services`` 不再
    拉起 FastAPI/NATS 等重依赖 (前者惰性导出 ``create_app``), 因此不再需要
    用空壳 module 顶替包 ``__init__``。这里只注入 ``src`` 后按真实包导入即可,
    消费侧源码仍是唯一实现来源。
    """
    src = atestudio_dir / "src"
    if not src.is_dir():
        raise SystemExit(f"ATEStudio src 目录不存在: {src}")
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    sibling = atestudio_dir.parent / "ATEPlatform" / "src"
    if sibling.is_dir():
        sys.path.insert(0, str(sibling))
    from ate_cloud.schemas.aterag_bundle import BundleModel, contract_hash
    from ate_cloud.services.aterag_script_emit import plan_gaps
    from ate_cloud.services.flow_planner import load_bindings, plan_flow

    return SimpleNamespace(
        BundleModel=BundleModel,
        contract_hash=contract_hash,
        plan_gaps=plan_gaps,
        load_bindings=load_bindings,
        plan_flow=plan_flow,
    )


def _steps(plan: Any) -> list[Any]:
    return [s for seg in plan.segments for s in (getattr(seg, "steps", None) or [])]


def _check_bundle(api: SimpleNamespace, bundle_path: Path, plan_dir: Path) -> tuple[dict[str, Any], bool]:
    """对单个 bundle 跑全部检查; 返回 (报告 dict, 是否全部通过)。"""
    raw = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle = api.BundleModel.model_validate_json(json.dumps(raw, ensure_ascii=False))
    plan = api.plan_flow(bundle, api.load_bindings())

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    own_hash = api.contract_hash()
    add(
        "contract_hash",
        own_hash == bundle.contract_hash == matrag_contract_hash() == FROZEN_CONTRACT_HASH,
        f"consumer={own_hash} bundle={bundle.contract_hash} matrag={matrag_contract_hash()}",
    )

    steps = _steps(plan)
    add(
        "plan_nonempty",
        plan.step_count > 0 and bool(steps),
        f"{len(plan.segments)} 分段 / step_count={plan.step_count} / emitted={plan.emitted_step_count}",
    )

    draft_groups = {
        (r.requirement_code, c.kind)
        for r in bundle.requirements
        for c in (*r.input_conditions, *r.output_conditions)
        if c.status != "approved"
    }
    step_groups = {
        (getattr(s, "requirement_code", None), (s.params or {}).get("condition_kind")) for s in steps
    }
    pending = getattr(plan, "pending", []) or []
    intersect = draft_groups & step_groups
    add(
        "red_line",
        not intersect and len(pending) == len(draft_groups),
        f"draft 组={len(draft_groups)} pending={len(pending)} 入队交集={len(intersect)}",
    )

    gaps = api.plan_gaps(plan)
    first = "; ".join(
        f"{getattr(g, 'requirement_code', '?')}/{getattr(g, 'condition_kind', '?')}" for g in list(gaps)[:3]
    )
    add("no_gaps", not gaps, f"{len(gaps)} 条缺口" + (f": {first}" if gaps else ""))

    ids = [s.id for s in steps]
    add("step_ids_unique", len(ids) == len(set(ids)), f"{len(ids)} 步 / {len(set(ids))} 唯一 id")

    plan_yaml = plan.to_yaml()
    plan_dir.mkdir(parents=True, exist_ok=True)
    out = plan_dir / f"{bundle.product_code}.plan.yaml"
    out.write_text(plan_yaml, encoding="utf-8")
    try:
        from ate_platform.dsl.parser import YamlParser
    except Exception as exc:  # noqa: BLE001 - 平台解析器可能不在当前环境
        add("yaml_parses", False, f"平台解析器不可用: {exc}")
    else:
        try:
            # 平台解析器只接受磁盘文件（Path.exists()）; 步数对比口径与消费侧
            # verify_e2e 一致: len(parsed.steps) == plan.emitted_step_count。
            parsed = YamlParser().parse(out)
            n = len(parsed.steps)
            want = plan.emitted_step_count
            add("yaml_parses", n == want, f"解析步数={n} 期望={want}, version={getattr(parsed, 'version', '?')}")
        except Exception as exc:  # noqa: BLE001 - 解析失败即验收失败
            add("yaml_parses", False, f"解析异常: {exc}")

    advisory: dict[str, Any] = {
        "total_settle_s": getattr(plan, "total_settle_s", None),
        "unmapped": len(getattr(plan, "unmapped", None) or []),
    }
    settle = advisory["total_settle_s"]
    if isinstance(settle, (int, float)) and settle > 1800:
        advisory["warn"] = f"单台节拍 {settle}s 超过 1800s"
    if advisory["unmapped"]:
        advisory["warn_unmapped"] = f"{advisory['unmapped']} 条条件未映射"

    ok = all(c["ok"] for c in checks)
    report = {
        "bundle": str(bundle_path),
        "product_code": bundle.product_code,
        "ok": ok,
        "checks": checks,
        "advisory": advisory,
        "plan_yaml": str(out),
    }
    return report, ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="bundle JSON 经消费侧源码 dry-run 验收")
    parser.add_argument("--bundle", action="append", default=[], help="bundle JSON 路径 (可多次)")
    parser.add_argument("--all", action="store_true", help="扫描 --bundle-dir 下全部 *.bundle.json")
    parser.add_argument("--bundle-dir", default=str(DEFAULT_BUNDLE_DIR), help="--all 的扫描目录")
    parser.add_argument("--plan-dir", default=str(DEFAULT_PLAN_DIR), help="计划 YAML 落盘目录")
    parser.add_argument("--atestudio-dir", default=str(DEFAULT_ATESTUDIO), help="ATEStudio 仓库根")
    parser.add_argument("--json", action="store_true", help="输出机器可读报告")
    args = parser.parse_args(argv)

    paths = [Path(p) for p in args.bundle]
    if args.all:
        paths.extend(sorted(Path(args.bundle_dir).glob("*.bundle.json")))
    if not paths:
        raise SystemExit("必须给 --bundle 或 --all")
    for p in paths:
        if not p.is_file():
            raise SystemExit(f"bundle 文件不存在: {p}")

    api = _load_consumer(Path(args.atestudio_dir))
    results: list[dict[str, Any]] = []
    all_ok = True
    for path in paths:
        report, ok = _check_bundle(api, path, Path(args.plan_dir))
        results.append(report)
        all_ok = all_ok and ok
        if not args.json:
            n_ok = sum(1 for c in report["checks"] if c["ok"])
            print(f"[{'PASS' if ok else 'FAIL'}] {report['product_code']} ({n_ok}/{len(report['checks'])})")
            for c in report["checks"]:
                if not c["ok"]:
                    print(f"  ✗ {c['name']}: {c['detail']}")
            for key in ("warn", "warn_unmapped"):
                if key in report["advisory"]:
                    print(f"  ! {report['advisory'][key]}")
            print(f"  plan → {report['plan_yaml']}")

    if args.json:
        print(json.dumps({"results": results, "ok": all_ok}, ensure_ascii=False, indent=2))
    elif all_ok:
        print("ALL PASS")
    return 0 if all_ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
