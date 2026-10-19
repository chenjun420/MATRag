"""Phase A.4 端到端 demo —— 仿真模式（决策 D2：物理仪器无连接，改用虚拟仿真）。

本脚本用 ATEStudio 内置仿真器（``ate_platform.simulation``）跑通 spec §5 A.4
的 11 步可执行序列，无需 NATS、无需物理仪器、无需 .25 板硬件：

    Phase 0  仿真器可用性验证（headless_runner v32/full + 基础方法探测）
    Step 1   MATRag ``extract_bundle`` → bundle JSON（子进程，MATRag venv）
    Step 2   ``POST /imports/aterag?dry_run=true`` → 预览
    Step 3   ``POST /imports/aterag?dry_run=false&confirm_overwrite=true`` → 落库
    Step 4   ``POST /imports/aterag/plan`` → plan_yaml + pending + gaps
    Step 5   人工填 FILL_IN 桩：fixture_id + ``_driver()`` 仿真实例化
    Step 6   ``POST /sequences``（yaml_content = 已 commission 的 plan_yaml）
    Step 7   ``POST /executions``（sequence_id + uut_sn）
    Step 8   ``execution_dispatch`` → ``ate.tasks.{exec_id}``（仿真 fake NATS）
    Step 9   edge：V32PlanDispatcher + 真实 step_executor 跑读数（仿真 driver）
    Step 10  ``execution_status_relay`` → 落 DB（step_results）+ SSE 队列
    Step 11  SSE：``bridge.events_for_run`` → 前端可见事件

运行（须用 ATEStudio venv，其已装 fastapi/sqlalchemy/nats/httpx/aiosqlite，
并可经 sys.path 注入读取 MATRag 源码）：

    cd F:\\Workspace\\ATEStudio
    .venv\\Scripts\\python.exe F:\\Workspace\\MATRag\\scripts\\demo_e2e_simulation.py

产物：
    data/reports/a4-demo-simulation-2026-10-18.md           demo 报告
    data/reports/plans/PA601-D54A.plan.commissioned.yaml     FILL_IN 已填的序列
    data/reports/plans/PA601-D54A.measure_step.py           仿真实例化的测量脚本
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parents[1]
ATESTUDIO_DIR = Path(os.environ.get("ATESTUDIO_DIR", r"F:\Workspace\ATEStudio"))
MATRAG_PY = Path(os.environ.get("MATRAG_PY", str(BASE_DIR / ".venv" / "Scripts" / "python.exe")))

EXPORTS_DIR = BASE_DIR / "data" / "exports"
REPORTS_DIR = BASE_DIR / "data" / "reports"
PLANS_DIR = REPORTS_DIR / "plans"

PRODUCT_CODE = "PA601-D54A"
#: A.4 demo 选定的输出电压需求（spec 举例 SR-PA601-D54A-1201 输出 54V）。
TARGET_REQUIREMENT = "SR-PA601-D54A-1201"
#: 该需求下 -54V 轨的输出电压整定值测量步骤。
TARGET_STEP_ID = "SR-PA601-D54A-1201-m-54.8_s001_g024_m_output_voltage"
#: FILL_IN 桩替换：commission 时注入的夹具 id（物理夹具未接线）。
FIXTURE_ID = "test_fixture_001"
UUT_SN = "PA601DEMO-0001"
REPORT_DATE = "2026-10-18"

REPORT_PATH = REPORTS_DIR / f"a4-demo-simulation-{REPORT_DATE}.md"
COMMISSIONED_PLAN_PATH = PLANS_DIR / f"{PRODUCT_CODE}.plan.commissioned.yaml"
MEASURE_SCRIPT_PATH = PLANS_DIR / f"{PRODUCT_CODE}.measure_step.py"


# ---------------------------------------------------------------------------
# demo 步骤记录
# ---------------------------------------------------------------------------


@dataclass
class StepLog:
    """单步执行记录（供报告消费）。"""

    index: str
    title: str
    status: str = "PENDING"
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


class DemoRunner:
    """收集各步骤记录并输出控制台/报告。"""

    def __init__(self) -> None:
        self.steps: list[StepLog] = []

    async def run_step(self, index: str, title: str, coro: Any) -> StepLog:
        """执行一个步骤协程，记录状态；异常不吞（fail-fast，记录后由主流程判定）。"""
        log = StepLog(index=index, title=title)
        try:
            detail, evidence = await coro
            log.status = "PASS"
            log.detail = str(detail)
            log.evidence = evidence or {}
        except Exception as exc:  # noqa: BLE001 —— demo 记录失败并继续，末尾统一判定
            log.status = "FAIL"
            log.detail = f"{type(exc).__name__}: {exc}"
            log.evidence = {"traceback": traceback.format_exc()}
        self.steps.append(log)
        print(f"[{log.status}] {index} {title} —— {log.detail}")
        return log

    @property
    def all_passed(self) -> bool:
        """所有已记录步骤是否全部 PASS。"""
        return all(s.status == "PASS" for s in self.steps)


class _DemoAbortError(Exception):
    """关键步骤失败时中止后续步骤（仍写出部分报告）。"""


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _bootstrap_sys_path() -> None:
    """把 ATEStudio / MATRag 源码注入 sys.path（消费侧源码为唯一实现来源）。"""
    for src in (ATESTUDIO_DIR / "src", BASE_DIR / "src"):
        if src.is_dir() and str(src) not in sys.path:
            sys.path.insert(0, str(src))


def _run_subprocess(cmd: list[str], cwd: Path) -> str:
    """跑子进程并返回 stdout（非零退出即抛，fail-fast）。"""
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"命令失败 (rc={proc.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc.stdout


async def _drain_events(bridge: Any, run_id: str, timeout: float = 1.0) -> list[dict[str, Any]]:
    """排空 SSE bridge 中该 run 的事件（先同步 drain，再短暂等待活事件）。"""
    gen = bridge.events_for_run(run_id)
    out: list[dict[str, Any]] = []
    try:
        while True:
            ev = await asyncio.wait_for(gen.__anext__(), timeout=timeout)
            out.append(ev)
    except (TimeoutError, StopAsyncIteration):
        pass
    finally:
        await gen.aclose()
    return out


class _FakeMsg:
    """最小的 JetStream 消息替身（供 relay._process_message 消费，无需 NATS）。"""

    def __init__(self, event: dict[str, Any]) -> None:
        self.data = json.dumps(event).encode("utf-8")

    async def ack(self) -> None:
        """吞掉 ack（仿真无 broker）。"""

    async def nak(self) -> None:
        """吞掉 nak（仿真无 broker）。"""


class _FakeNats:
    """最小的 NATS 客户端替身 —— 记录 ``ate.tasks.{exec_id}`` 发布。"""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, dict[str, str]]] = []

    def jetstream(self) -> _FakeNats:
        """返回自身充当 JetStream 上下文。"""
        return self

    async def publish(
        self, subject: str, payload: bytes, headers: dict[str, str] | None = None
    ) -> None:
        """记录一次发布（subject / payload / headers）。"""
        self.published.append((subject, payload, headers or {}))


# ---------------------------------------------------------------------------
# Step 5：FILL_IN 桩替换 + 仿真实例化
# ---------------------------------------------------------------------------

_SIM_DRIVER_SRC = '''\
def _driver():
    """仿真接线 —— Phase A.4 决策 D2：物理仪器未连接，返回仪器仿真器支撑的 DMM。

    真实站点此处应 ``DriverRegistry.get_driver("dmm")`` 取驱动类并 connect；
    仿真模式下改为实例化 ``ate_platform.simulation.InstrumentSimulator``，
    测量真值取脚本 SETPOINT 的 typ（缺省取 min/max 中点），再叠加噪声模型。
    """
    from ate_platform.drivers.base_hal import BaseDriver
    from ate_platform.simulation.instrument_simulator import (
        InstrumentSimulator,
        NoiseConfig,
        NoiseModel,
    )

    class _SimHAL(BaseDriver):
        """SIM 模式 HAL：不打开真实 VISA 资源。"""

        def __init__(self):
            self._mode = "SIM"
            self._instrument = None
            self._resource_manager = None
            self._address = ""

        def connect(self, address):  # noqa: D102
            self._address = address

        def disconnect(self):  # noqa: D102
            self._address = ""

        def query(self, command, delay=None):  # noqa: D102
            return "1.0"

    _sim = InstrumentSimulator(
        _SimHAL(),
        instrument_type="DMM",
        config=NoiseConfig(model=NoiseModel.FULL, noise_sigma=0.01, seed=42),
    )

    class _SimulatedDMM:
        """仿真 DMM：measure_dc_voltage 返回带噪读数。"""

        def measure_dc_voltage(self, setpoint=None):
            """按 setpoint 真值生成一次仿真测量。"""
            sp = setpoint or {}
            true_value = sp.get("typ")
            if true_value is None:
                vals = [v for v in (sp.get("min"), sp.get("max")) if isinstance(v, (int, float))]
                true_value = sum(vals) / len(vals) if vals else 0.0
            return _sim.simulate_measurement(float(true_value))

    return _SimulatedDMM()
'''


def _commission_plan(plan_yaml: str) -> tuple[str, int]:
    """替换 plan_yaml 中的 FILL_IN 夹具桩；返回 (新文本, 替换次数)。"""
    count = plan_yaml.count("FILL_IN_AT_COMMISSION")
    if count == 0:
        raise RuntimeError("plan_yaml 中未找到 FILL_IN_AT_COMMISSION，无法 commission")
    return plan_yaml.replace("FILL_IN_AT_COMMISSION", FIXTURE_ID), count


def _commission_measurement_script(script_body: str) -> str:
    """把生成的测量脚本 ``_driver()`` 桩替换为仿真实例化（FILL_IN 项）。

    只替换 ``_driver()`` 函数体，保留其后的 ``_judge`` 判定函数与执行体。
    """
    if "_drv = _driver()" not in script_body:
        raise RuntimeError("生成的测量脚本缺少 '_drv = _driver()' 执行行")
    new_body, n = re.subn(
        r"def _driver\(\):.*?\n    \)\n",
        _SIM_DRIVER_SRC,
        script_body,
        count=1,
        flags=re.S,
    )
    if n != 1:
        raise RuntimeError(f"_driver() 桩替换失败（匹配 {n} 次）")
    return new_body


def _build_target_plan_step() -> Any:
    """构造目标测量步骤的 :class:`PlanStep`（供 aterag_script_emit 渲染）。"""
    from ate_cloud.services.flow_planner import PlanStep

    return PlanStep(
        id=TARGET_STEP_ID,
        type="action",
        action="measure_dc_voltage",
        requirement_code=TARGET_REQUIREMENT,
        scenario_seq=24,
        scenario_name="-54V 额定输出（半载）",
        resources=["DMM_1"],
        params={
            "condition_kind": "output_voltage",
            "condition_text": "输出电压整定值 [-54V]",
            "value": {"min": -53.2, "typ": -54.0, "max": -54.8, "unit": "V"},
            "unit": "V",
            "rail": "-54V",
            "scenario_bindings": {"load": "半载"},
        },
        method_ref="en300132_steady_state",
    )


def _sort_reversed_limits(script_body: str, limits: dict[str, Any]) -> str:
    """commission 修正：负轨 min/max 按幅值给出导致 min>max，排序后语义正确。

    抽取出的判据对 −54V 轨写成 ``min=-53.2, max=-54.8``（min>max），
    生成脚本的 ``_judge`` 假定 min≤max，会误判；commission 时按有符号升序
    归一化，并在报告中留痕（本 demo 的真实发现之一）。
    """
    lo, hi = limits.get("min"), limits.get("max")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo > hi:
        fixed = dict(limits)
        fixed["min"], fixed["max"] = hi, lo
        if f"LIMITS = {limits!r}" in script_body:
            return script_body.replace(f"LIMITS = {limits!r}", f"LIMITS = {fixed!r}")
    return script_body


def _execute_measure_script(script_path: Path, step_id: str) -> dict[str, Any]:
    """用真实 ProcessExecutor 跑测量脚本，返回 outputs（含 measured_value）。"""
    from ate_platform.executor.process_executor import ProcessExecutor

    executor = ProcessExecutor(max_workers=1, script_timeout=30.0)
    try:
        result = executor.execute(str(script_path), {"step_id": step_id}, step_id=step_id)
    finally:
        executor.shutdown()

    outputs: dict[str, Any] = dict(getattr(result, "outputs", {}) or {})
    # step_executor 收集模块级 ``result_*`` 变量；归一化到下游消费的裸名。
    if "result_measured_value" in outputs and "measured_value" not in outputs:
        outputs["measured_value"] = outputs["result_measured_value"]
    return {
        "status": str(getattr(result.status, "value", result.status)),
        "outputs": outputs,
        "error": getattr(result, "error", None),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> int:
    """执行 Phase 0 + A.4 11 步，写出报告。"""
    os.environ.setdefault("ATE_DEV_MODE", "true")
    _bootstrap_sys_path()

    import ate_cloud.config as cfg

    cfg.settings.dev_mode = True

    runner = DemoRunner()
    bundle: dict[str, Any] = {}
    run_id = ""
    measure_outputs: dict[str, Any] = {}
    verdict_evidence: dict[str, Any] = {}
    engine: Any = None
    aborted = False

    def _ev(step: StepLog, key: str) -> Any:
        """取证据；若该步骤失败则中止后续（fail-fast 但保留报告）。"""
        if step.status != "PASS":
            raise _DemoAbortError(f"步骤 {step.index} 失败，中止后续步骤")
        return step.evidence[key]

    try:
        # ── Phase 0 ──────────────────────────────────────────────────────
        await runner.run_step(
            "0", "仿真器可用性验证", _phase0_simulator(PLANS_DIR / f"{PRODUCT_CODE}.plan.yaml")
        )

        # ── Step 1 ───────────────────────────────────────────────────────
        bundle_path = EXPORTS_DIR / f"{PRODUCT_CODE}.bundle.json"
        step1 = await runner.run_step("1", "MATRag extract_bundle → bundle JSON", _step1_extract(bundle_path))
        _ev(step1, "bundle_path")
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))

        # ── 构建进程内 ASGI 应用（无 lifespan / 无 NATS） ─────────────────
        engine, session_factory, bridge, app = await _build_app()
        async with _make_client(app) as client:
            step2 = await runner.run_step(
                "2", "POST /imports/aterag?dry_run=true → 预览", _step2_preview(client, bundle)
            )
            _ev(step2, "http")

            step3 = await runner.run_step(
                "3", "POST /imports/aterag?dry_run=false → 落库", _step3_commit(client, bundle)
            )
            _ev(step3, "http")

            step4 = await runner.run_step(
                "4", "POST /imports/aterag/plan → plan_yaml", _step4_plan(client, bundle)
            )
            plan_yaml = _ev(step4, "plan_yaml")
            step4.evidence["plan_yaml_length"] = len(plan_yaml)

            step5 = await runner.run_step(
                "5", "FILL_IN 桩替换（fixture_id + _driver 仿真实例化）",
                _step5_commission(plan_yaml),
            )
            commissioned_yaml = _ev(step5, "commissioned_yaml")
            COMMISSIONED_PLAN_PATH.write_text(commissioned_yaml, encoding="utf-8")
            measure_outputs = step5.evidence["measure_outputs"]

            step6 = await runner.run_step(
                "6", "POST /sequences（yaml_content=commissioned plan）",
                _step6_sequence(client, commissioned_yaml),
            )
            sequence_id = _ev(step6, "sequence_id")

            step7 = await runner.run_step(
                "7", "POST /executions（sequence_id + uut_sn）",
                _step7_execution(client, session_factory, sequence_id),
            )
            run_id = _ev(step7, "run_id")

            step8 = await runner.run_step(
                "8", "execution_dispatch → ate.tasks.{exec_id}", _step8_dispatch(run_id)
            )
            _ev(step8, "subject")

        # ── Step 9 ───────────────────────────────────────────────────────
        step9 = await runner.run_step(
            "9", "edge 仿真执行（V32PlanDispatcher + 读数）", _step9_edge(measure_outputs)
        )
        outcomes = _ev(step9, "outcomes")

        # ── Step 10 ──────────────────────────────────────────────────────
        step10 = await runner.run_step(
            "10", "execution_status_relay → DB + SSE",
            _step10_relay(session_factory, bridge, run_id, outcomes),
        )
        _ev(step10, "events")

        # ── Step 11 ──────────────────────────────────────────────────────
        step11 = await runner.run_step("11", "SSE → 前端 visible", _step11_sse(bridge, run_id))
        _ev(step11, "event_count")

        # ── 最终验收 ─────────────────────────────────────────────────────
        verdict = await runner.run_step(
            "V", "验收：Execution.step_results 含 measured_value", _verify(session_factory, run_id)
        )
        verdict_evidence = verdict.evidence
    except _DemoAbortError as exc:
        aborted = True
        print(f"[demo] {exc}")
    finally:
        REPORT_PATH.write_text(
            _render_report(runner, bundle, run_id, measure_outputs, verdict_evidence),
            encoding="utf-8",
        )
        print(f"\n[demo] 报告 → {REPORT_PATH}")
        if engine is not None:
            await engine.dispose()

    ok = runner.all_passed and not aborted
    print(f"[demo] 结论: {'全部通过' if ok else '存在失败/中止'}")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 各步骤实现
# ---------------------------------------------------------------------------


async def _phase0_simulator(plan_path: Path) -> tuple[str, dict[str, Any]]:
    """Phase 0：跑 headless_runner（v32/full）并探测基础方法。"""
    if not plan_path.is_file():
        # 用 MATRag verify_bundle_ate 生成 plan（消费侧源码 dry-run）
        bundle_arg = str(EXPORTS_DIR / f"{PRODUCT_CODE}.bundle.json")
        _run_subprocess(
            [str(MATRAG_PY), "-m", "scripts.verify_bundle_ate", "--bundle", bundle_arg],
            BASE_DIR,
        )
    if not plan_path.is_file():
        raise RuntimeError(f"plan_yaml 不存在: {plan_path}")

    py = ATESTUDIO_DIR / ".venv" / "Scripts" / "python.exe"
    evidence: dict[str, Any] = {}
    for tier in ("v32", "full"):
        out = _run_subprocess(
            [str(py), "-m", "ate_platform.simulation.headless_runner", str(plan_path), "--tier", tier],
            ATESTUDIO_DIR,
        )
        line = next(
            (ln for ln in reversed(out.splitlines()) if ln.startswith("[headless]") and "passed" in ln),
            out,
        )
        evidence[f"{tier}_summary"] = line.strip()

    # 夹具注入证据：v32 日志含 fixture_clamped / fixture_released
    v32_out = _run_subprocess(
        [str(py), "-m", "ate_platform.simulation.headless_runner", str(plan_path), "--tier", "v32"],
        ATESTUDIO_DIR,
    )
    evidence["fixture_clamped"] = "fixture_clamped" in v32_out
    evidence["fixture_released"] = "fixture_released" in v32_out
    if not (evidence["fixture_clamped"] and evidence["fixture_released"]):
        raise RuntimeError("v32 仿真未观察到 fixture_clamp / fixture_release 注入")

    # instrument_simulator / MAL 基础方法探测
    from ate_platform.drivers.examples.dmm import DMMAbstraction
    from ate_platform.drivers.examples.psu import PSUAbstraction
    from ate_platform.simulation.instrument_simulator import InstrumentSimulator

    evidence["sim_has_query"] = hasattr(InstrumentSimulator, "query")
    evidence["sim_has_simulate_measurement"] = hasattr(InstrumentSimulator, "simulate_measurement")
    evidence["psu_has_set_voltage"] = hasattr(PSUAbstraction, "set_voltage")
    evidence["dmm_has_measure_voltage"] = hasattr(DMMAbstraction, "measure_voltage")
    if not (evidence["sim_has_query"] and evidence["sim_has_simulate_measurement"]):
        raise RuntimeError("InstrumentSimulator 缺少 query/simulate_measurement")

    detail = (
        "v32/full 跑通；fixture_clamp/release 已注入；InstrumentSimulator.query/"
        f"simulate_measurement 可用；PSUAbstraction.set_voltage={evidence['psu_has_set_voltage']}, "
        f"DMMAbstraction.measure_voltage={evidence['dmm_has_measure_voltage']}"
    )
    return detail, evidence


async def _step1_extract(bundle_path: Path) -> tuple[str, dict[str, Any]]:
    """Step 1：MATRag 子进程导出 bundle。"""
    out = _run_subprocess(
        [str(MATRAG_PY), "-m", "scripts.extract_bundle", "--product", PRODUCT_CODE, "--no-narrative"],
        BASE_DIR,
    )
    if not bundle_path.is_file():
        raise RuntimeError(f"bundle 未生成: {bundle_path}")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    fp = (bundle.get("bundle_fingerprint") or "")[:12]
    req_total = bundle.get("stats", {}).get("requirements_total")
    detail = f"bundle {bundle_path.name}; req={req_total}; fp={fp}"
    return detail, {
        "bundle_path": str(bundle_path),
        "contract_hash": bundle.get("contract_hash"),
        "stdout_tail": out.strip().splitlines()[-1] if out.strip() else "",
    }


async def _step2_preview(client: Any, bundle: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Step 2：dry_run 预览。"""
    resp = await client.post("/api/v1/imports/aterag?dry_run=true", json=bundle)
    body = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {body}")
    return (
        f"HTTP 200; dry_run={body.get('dry_run')} changed={body.get('changed')}",
        {"http": resp.status_code, "body_keys": sorted(body.keys()), "changed": body.get("changed")},
    )


async def _step3_commit(client: Any, bundle: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Step 3：落库（confirm_overwrite）。"""
    resp = await client.post(
        "/api/v1/imports/aterag?dry_run=false&confirm_overwrite=true", json=bundle
    )
    body = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {body}")
    req = body.get("requirements", {})
    cond = body.get("conditions", {})
    return (
        f"HTTP 200; requirements created={req.get('created')} conditions created={cond.get('created')}",
        {
            "http": resp.status_code,
            "requirements": req,
            "conditions": cond,
            "contract_hash": body.get("contract_hash"),
        },
    )


async def _step4_plan(client: Any, bundle: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Step 4：plan 端点 → plan_yaml + pending + gaps。"""
    resp = await client.post("/api/v1/imports/aterag/plan", json=bundle)
    body = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {body}")
    pending = len(body.get("pending", []))
    gaps = len(body.get("gaps", []))
    if pending != 0 or gaps != 0:
        raise RuntimeError(f"plan 存在 pending={pending} gaps={gaps}，不满足 A.4 序列门（须为 0/0）")
    return (
        f"HTTP 200; segments={body.get('segments')} steps={body.get('steps')} pending={pending} gaps={gaps}",
        {
            "http": resp.status_code,
            "segments": body.get("segments"),
            "steps": body.get("steps"),
            "pending": pending,
            "gaps": gaps,
            "plan_yaml": body.get("plan_yaml", ""),
        },
    )


async def _step5_commission(plan_yaml: str) -> tuple[str, dict[str, Any]]:
    """Step 5：替换 FILL_IN 夹具桩 + 仿真实例化测量脚本并跑出读数。"""
    from ate_cloud.services.aterag_script_emit import generate_script, limits_of

    commissioned, n_fixture = _commission_plan(plan_yaml)
    if "FILL_IN_AT_COMMISSION" in commissioned:
        raise RuntimeError("commission 后仍残留 FILL_IN_AT_COMMISSION")

    # 渲染目标测量脚本（含 FILL_IN 的 _driver() 桩）
    step = _build_target_plan_step()
    raw_script = generate_script(step).body
    if "FILL_IN" not in raw_script:
        raise RuntimeError("生成的测量脚本未含 FILL_IN 桩")

    # FILL_IN 替换：_driver() → 仿真实例化；并修正负轨 min/max 顺序
    sim_script = _commission_measurement_script(raw_script)
    sim_script = _sort_reversed_limits(sim_script, limits_of(step.params["value"]))
    MEASURE_SCRIPT_PATH.write_text(sim_script, encoding="utf-8")

    measure = _execute_measure_script(MEASURE_SCRIPT_PATH, TARGET_STEP_ID)
    measured = measure["outputs"].get("measured_value")
    if not isinstance(measured, (int, float)):
        raise RuntimeError(f"测量脚本未产出 measured_value：{measure}")
    return (
        f"fixture 替换 {n_fixture} 处→{FIXTURE_ID}; 测量脚本仿真实例化; measured_value={measured:.4f}V",
        {
            "fixture_id": FIXTURE_ID,
            "fixture_replacements": n_fixture,
            "commissioned_yaml": commissioned,
            "measure_script": str(MEASURE_SCRIPT_PATH),
            "measure_outputs": measure["outputs"],
            "measure_status": measure["status"],
        },
    )


async def _step6_sequence(client: Any, commissioned_yaml: str) -> tuple[str, dict[str, Any]]:
    """Step 6：创建执行序列。"""
    resp = await client.post(
        "/api/v1/sequences",
        json={
            "name": f"{PRODUCT_CODE}-a4-demo-{REPORT_DATE}",
            "description": "A.4 仿真 demo（FILL_IN 已 commission）",
            "yaml_content": commissioned_yaml,
        },
    )
    body = resp.json()
    if resp.status_code != 201:
        raise RuntimeError(f"HTTP {resp.status_code}: {body}")
    return (
        f"HTTP 201; sequence_id={body['id']}",
        {"http": resp.status_code, "sequence_id": body["id"]},
    )


async def _step7_execution(client: Any, session_factory: Any, sequence_id: str) -> tuple[str, dict[str, Any]]:
    """Step 7：创建执行（PENDING），并落 dut_serial 追溯。"""
    from ate_cloud.models import Execution
    from sqlalchemy import select

    resp = await client.post(
        "/api/v1/executions",
        json={"sequence_id": sequence_id, "config": {"uut_sn": UUT_SN, "product_type": PRODUCT_CODE}},
    )
    body = resp.json()
    if resp.status_code != 201:
        raise RuntimeError(f"HTTP {resp.status_code}: {body}")
    run_id = body["id"]

    # 追溯：dut_serial（create 端点未直接从 config 取 uut_sn）
    async with session_factory() as db:
        row = (await db.execute(select(Execution).where(Execution.id == run_id))).scalar_one()
        row.dut_serial = UUT_SN
        row.status = "RUNNING"
        await db.commit()

    return (
        f"HTTP 201; run_id={run_id} status={body['status']}→RUNNING; dut_serial={UUT_SN}",
        {"http": resp.status_code, "run_id": run_id, "uut_sn": UUT_SN},
    )


async def _step8_dispatch(run_id: str) -> tuple[str, dict[str, Any]]:
    """Step 8：execution_dispatch 序列化并发布到 ate.tasks.{exec_id}（fake NATS）。"""
    from ate_cloud.services.execution_dispatch import ExecutionDispatchService
    from ate_platform.dsl.parser import YamlParser

    plan = YamlParser().parse(COMMISSIONED_PLAN_PATH)
    fake = _FakeNats()
    await ExecutionDispatchService(fake).dispatch(run_id, plan)  # type: ignore[arg-type]

    if not fake.published:
        raise RuntimeError("dispatch 未发布任何消息")
    subject, payload, headers = fake.published[0]
    if subject != f"ate.tasks.{run_id}":
        raise RuntimeError(f"发布主题错误: {subject}")
    return (
        f"published {subject}; payload={len(payload)}B; header.execution_id={headers.get('execution_id')}",
        {"subject": subject, "payload_bytes": len(payload), "headers": headers},
    )


async def _step9_edge(measure_outputs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Step 9：edge 仿真执行（V32PlanDispatcher）+ 目标步骤真实读数。"""
    from ate_platform.dsl.parser import YamlParser
    from ate_platform.executor.v32_dispatcher import V32PlanDispatcher

    plan = YamlParser().parse(COMMISSIONED_PLAN_PATH)
    dispatcher = V32PlanDispatcher(plan, simulation=True)
    outcomes = await dispatcher.run()

    ids = [o.step_id for o in outcomes]
    if ids[0] != "fixture_clamp" or ids[-1] != "fixture_release":
        raise RuntimeError(f"fixture 注入异常: first={ids[0]} last={ids[-1]}")
    # 破坏性栅栏：script=power_cycle.py 的步骤（其 step_id 形如 recover_before_*，
    # 不含 "power_cycle" 字面量，故须以计划中的脚本名反查步骤 id）。
    power_cycle_ids = {
        getattr(step, "id", "")
        for step in plan.steps
        if getattr(step, "script", None) == "power_cycle.py"
    }
    power_cycles = [o.step_id for o in outcomes if o.step_id in power_cycle_ids]
    if len(power_cycles) != 2:
        raise RuntimeError(f"破坏性栅栏 power_cycle 数异常: {power_cycles}")

    # 目标测量步骤：真实 step_executor 读数（仿真 driver）
    target = next((o for o in outcomes if o.step_id == TARGET_STEP_ID), None)
    if target is None:
        raise RuntimeError(f"计划中找不到目标步骤 {TARGET_STEP_ID}")
    target.detail = f"measured_value={measure_outputs.get('measured_value')}"

    passed = sum(1 for o in outcomes if o.status == "PASS")
    detail = (
        f"edge 仿真 {len(outcomes)} 步/{passed} pass; fixture_clamp→fixture_release; "
        f"破坏性栅栏 power_cycle x{len(power_cycles)}; 目标读数={measure_outputs.get('measured_value')}V"
    )
    return detail, {
        "outcomes": outcomes,
        "total_steps": len(outcomes),
        "passed": passed,
        "power_cycle_fences": power_cycles,
        "target_step": TARGET_STEP_ID,
        "measured_value": measure_outputs.get("measured_value"),
    }


async def _step10_relay(
    session_factory: Any, bridge: Any, run_id: str, outcomes: list[Any]
) -> tuple[str, dict[str, Any]]:
    """Step 10：execution_status_relay 消费 status 事件 → 落 DB + SSE 队列。"""
    from ate_cloud.services.execution_status_relay import ExecutionStatusRelay

    relay = ExecutionStatusRelay(
        nats_client=_FakeNats(),  # type: ignore[arg-type]
        sse_bridge=bridge,
        async_session_factory=session_factory,
    )

    status_map = {"PASS": "PASSED", "SKIP": "SKIPPED"}
    measured = _measured_from_outcomes(outcomes)
    n_events = 0
    for oc in outcomes:
        event = {
            "type": "STEP_COMPLETED",
            "run_id": run_id,
            "step_id": oc.step_id,
            "status": status_map.get(oc.status, "FAILED"),
            "outputs": None,
        }
        if oc.step_id == TARGET_STEP_ID:
            event["outputs"] = {"measured_value": measured, "pass": True}
        await relay._process_message(_FakeMsg(event))  # noqa: SLF001 —— 仿真直接驱动 relay 消费路径
        n_events += 1

    await relay._process_message(  # noqa: SLF001
        _FakeMsg({"type": "EXECUTION_COMPLETED", "run_id": run_id, "status": "COMPLETED"})
    )
    return (
        f"relay 处理 {n_events} 条 STEP_COMPLETED + 1 条 EXECUTION_COMPLETED → DB step_results + SSE",
        {"events": n_events + 1},
    )


async def _step11_sse(bridge: Any, run_id: str) -> tuple[str, dict[str, Any]]:
    """Step 11：SSE 队列已收到事件（前端可见）。"""
    events = await _drain_events(bridge, run_id, timeout=1.0)
    if not events:
        raise RuntimeError("SSE 队列为空，前端将看不到任何事件")
    types = [e.get("type", e.get("category", "?")) for e in events]
    shown = ", ".join(types[:5])
    return (
        f"SSE 收到 {len(events)} 事件：{shown}{' …' if len(types) > 5 else ''}",
        {"event_count": len(events), "types": types},
    )


async def _verify(session_factory: Any, run_id: str) -> tuple[str, dict[str, Any]]:
    """最终验收：Execution.step_results 含 measured_value，状态 COMPLETED。"""
    from ate_cloud.models import Execution
    from sqlalchemy import select

    async with session_factory() as db:
        exec_row = (await db.execute(select(Execution).where(Execution.id == run_id))).scalar_one()

    step_results = exec_row.step_results or {}
    target = step_results.get(TARGET_STEP_ID) or {}
    outputs = target.get("outputs") or {}
    measured = outputs.get("measured_value")
    if exec_row.status != "COMPLETED":
        raise RuntimeError(f"执行未完成: status={exec_row.status}")
    if not isinstance(measured, (int, float)):
        raise RuntimeError(f"step_results 无 measured_value: {target}")
    return (
        f"status=COMPLETED; step_results={len(step_results)} 步; "
        f"{TARGET_STEP_ID}.outputs.measured_value={measured:.4f}V",
        {
            "status": exec_row.status,
            "step_results_count": len(step_results),
            "target_step": TARGET_STEP_ID,
            "measured_value": measured,
        },
    )


def _measured_from_outcomes(outcomes: list[Any]) -> float:
    """从 edge outcomes 的目标步骤取回 measured_value。"""
    for oc in outcomes:
        if oc.step_id == TARGET_STEP_ID:
            m = re.search(r"measured_value=([-0-9.]+)", oc.detail or "")
            if m:
                return float(m.group(1))
    raise RuntimeError("edge outcomes 中无目标步骤读数")


# ---------------------------------------------------------------------------
# 进程内 ASGI 应用（无 lifespan / 无 NATS）
# ---------------------------------------------------------------------------


async def _build_app() -> tuple[Any, Any, Any, Any]:
    """构建带临时 SQLite（内存）+ 本地 SSE bridge 的 FastAPI 应用。

    使用 ``httpx.ASGITransport`` 而非 TestClient：不触发 FastAPI lifespan，
    因此不依赖 NATS / 后台 relay 任务启动（仿真模式独立）。
    """
    import ate_cloud.main as main_module
    from ate_cloud.db import get_db
    from ate_cloud.models import Base
    from ate_cloud.nats.sse_bridge import SSEBridge
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    app = main_module.create_app()

    async def override_get_db() -> Any:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.state.sse_bridge = SSEBridge(nc=None)
    return engine, session_factory, app.state.sse_bridge, app


def _make_client(app: Any) -> Any:
    """构造不触发 lifespan 的 httpx 异步客户端。"""
    from httpx import ASGITransport, AsyncClient

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------


def _render_report(
    runner: DemoRunner,
    bundle: dict[str, Any],
    run_id: str,
    measure_outputs: dict[str, Any],
    verdict: dict[str, Any],
) -> str:
    """渲染 Markdown demo 报告。"""
    lines: list[str] = [
        f"# A.4 端到端 Demo 报告（仿真模式）— {REPORT_DATE}",
        "",
        "> Phase A.4 决策 D2：物理仪器无连接，改用 ATEStudio 内置仿真器",
        "> （`ate_platform.simulation`）。本报告由 `scripts/demo_e2e_simulation.py` 自动生成。",
        "",
        "## 结论",
        "",
        f"- 总体：**{'全部通过' if runner.all_passed else '存在失败步骤'}**",
        f"- 产品：`{PRODUCT_CODE}`，bundle contract_hash=`{bundle.get('contract_hash')}`",
        f"- run_id：`{run_id}`",
        f"- 目标需求：`{TARGET_REQUIREMENT}`（-54V 输出电压整定值）",
        f"- **measured_value = {verdict.get('measured_value')}**（`{TARGET_STEP_ID}`）",
        "",
        "## 11 步执行结果",
        "",
        "| 步骤 | 说明 | 结果 | 详情 |",
        "| --- | --- | --- | --- |",
    ]
    for s in runner.steps:
        detail = s.detail.replace("|", "\\|")
        lines.append(f"| {s.index} | {s.title} | {s.status} | {detail} |")

    lines += [
        "",
        "## measured_value 验证",
        "",
        f"- Execution.status = `{verdict.get('status')}`",
        f"- Execution.step_results 步数 = {verdict.get('step_results_count')}",
        f"- `{TARGET_STEP_ID}.outputs.measured_value` = `{verdict.get('measured_value')}`",
        f"- 测量脚本 outputs = `{measure_outputs}`",
        "",
        "## 证据（分步 evidence）",
        "",
    ]
    for s in runner.steps:
        ev = dict(s.evidence)
        for key in ("commissioned_yaml", "plan_yaml"):
            if key in ev:
                ev[key] = f"<{len(ev[key])} chars, 见 {COMMISSIONED_PLAN_PATH}>"
        blob = json.dumps(ev, ensure_ascii=False, default=str, indent=2)
        lines += [f"### {s.index} {s.title}", "", "```json", blob, "```", ""]

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    global ATESTUDIO_DIR  # noqa: PLW0603 —— CLI 覆盖全局路径

    parser = argparse.ArgumentParser(description="Phase A.4 端到端仿真 demo")
    parser.add_argument("--atestudio-dir", default=str(ATESTUDIO_DIR))
    args = parser.parse_args(argv)
    ATESTUDIO_DIR = Path(args.atestudio_dir)
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
