# A.4 端到端 Demo 报告（仿真模式）— 2026-10-18

> Phase A.4 决策 D2：物理仪器无连接，改用 ATEStudio 内置仿真器
> （`ate_platform.simulation`）。本报告由 `scripts/demo_e2e_simulation.py` 自动生成。

## 结论

- 总体：**全部通过**
- 产品：`PA601-D54A`，bundle contract_hash=`59a852c26ca277e4141c6a60ae723b20`
- run_id：`0a69d192-77e1-40fc-924b-ed5186564422`
- 目标需求：`SR-PA601-D54A-1201`（-54V 输出电压整定值）
- **measured_value = -54.00144090329578**（`SR-PA601-D54A-1201-m-54.8_s001_g024_m_output_voltage`）

## 11 步执行结果

| 步骤 | 说明 | 结果 | 详情 |
| --- | --- | --- | --- |
| 0 | 仿真器可用性验证 | PASS | v32/full 跑通；fixture_clamp/release 已注入；InstrumentSimulator.query/simulate_measurement 可用；PSUAbstraction.set_voltage=True, DMMAbstraction.measure_voltage=True |
| 1 | MATRag extract_bundle → bundle JSON | PASS | bundle PA601-D54A.bundle.json; req=76; fp=b3000312e300 |
| 2 | POST /imports/aterag?dry_run=true → 预览 | PASS | HTTP 200; dry_run=True changed=True |
| 3 | POST /imports/aterag?dry_run=false → 落库 | PASS | HTTP 200; requirements created=76 conditions created=151 |
| 4 | POST /imports/aterag/plan → plan_yaml | PASS | HTTP 200; segments=29 steps=100 pending=0 gaps=0 |
| 5 | FILL_IN 桩替换（fixture_id + _driver 仿真实例化） | PASS | fixture 替换 2 处→test_fixture_001; 测量脚本仿真实例化; measured_value=-54.0014V |
| 6 | POST /sequences（yaml_content=commissioned plan） | PASS | HTTP 201; sequence_id=13cf9b2d-9c38-4999-a488-57e4fd8de70b |
| 7 | POST /executions（sequence_id + uut_sn） | PASS | HTTP 201; run_id=0a69d192-77e1-40fc-924b-ed5186564422 status=PENDING→RUNNING; dut_serial=PA601DEMO-0001 |
| 8 | execution_dispatch → ate.tasks.{exec_id} | PASS | published ate.tasks.0a69d192-77e1-40fc-924b-ed5186564422; payload=76048B; header.execution_id=0a69d192-77e1-40fc-924b-ed5186564422 |
| 9 | edge 仿真执行（V32PlanDispatcher + 读数） | PASS | edge 仿真 104 步/104 pass; fixture_clamp→fixture_release; 破坏性栅栏 power_cycle x2; 目标读数=-54.00144090329578V |
| 10 | execution_status_relay → DB + SSE | PASS | relay 处理 104 条 STEP_COMPLETED + 1 条 EXECUTION_COMPLETED → DB step_results + SSE |
| 11 | SSE → 前端 visible | PASS | SSE 收到 106 事件：EXECUTION_STARTED, STEP_COMPLETED, STEP_COMPLETED, STEP_COMPLETED, STEP_COMPLETED … |
| V | 验收：Execution.step_results 含 measured_value | PASS | status=COMPLETED; step_results=104 步; SR-PA601-D54A-1201-m-54.8_s001_g024_m_output_voltage.outputs.measured_value=-54.0014V |

## measured_value 验证

- Execution.status = `COMPLETED`
- Execution.step_results 步数 = 104
- `SR-PA601-D54A-1201-m-54.8_s001_g024_m_output_voltage.outputs.measured_value` = `-54.00144090329578`
- 测量脚本 outputs = `{'RESOURCES': ['DMM_1'], 'LIMITS': {'min': -54.8, 'typ': -54.0, 'max': -53.2}, 'ORIGIN': 'SR-PA601-D54A-1201 / 场景 24 / -54V 额定输出（半载）', 'result_measured': -54.00144090329578, 'result_measured_value': -54.00144090329578, 'result_passed': True, 'result_cond_fingerprint': '', 'measured_value': -54.00144090329578}`

## 证据（分步 evidence）

### 0 仿真器可用性验证

```json
{
  "v32_summary": "[headless] v32: 104 passed, 0 failed, 0 skipped, 104 steps in 0.046s",
  "full_summary": "[headless] full: 104 passed, 0 failed, 0 skipped, 104 steps in 0.000s",
  "fixture_clamped": true,
  "fixture_released": true,
  "sim_has_query": true,
  "sim_has_simulate_measurement": true,
  "psu_has_set_voltage": true,
  "dmm_has_measure_voltage": true
}
```

### 1 MATRag extract_bundle → bundle JSON

```json
{
  "bundle_path": "F:\\Workspace\\MATRag\\data\\exports\\PA601-D54A.bundle.json",
  "contract_hash": "59a852c26ca277e4141c6a60ae723b20",
  "stdout_tail": "  �� data\\exports\\PA601-D54A.bundle.json  fp=b3000312e300��"
}
```

### 2 POST /imports/aterag?dry_run=true → 预览

```json
{
  "http": 200,
  "body_keys": [
    "cases",
    "cases_reset_to_draft",
    "changed",
    "conditions",
    "conflicts",
    "contract_hash",
    "dry_run",
    "limits",
    "product_code",
    "requirements",
    "requirements_staled",
    "unmapped"
  ],
  "changed": true
}
```

### 3 POST /imports/aterag?dry_run=false → 落库

```json
{
  "http": 200,
  "requirements": {
    "created": 76,
    "updated": 0
  },
  "conditions": {
    "created": 151,
    "updated": 0
  },
  "contract_hash": "59a852c26ca277e4141c6a60ae723b20"
}
```

### 4 POST /imports/aterag/plan → plan_yaml

```json
{
  "http": 200,
  "segments": 29,
  "steps": 100,
  "pending": 0,
  "gaps": 0,
  "plan_yaml": "<45000 chars, 见 F:\\Workspace\\MATRag\\data\\reports\\plans\\PA601-D54A.plan.commissioned.yaml>",
  "plan_yaml_length": 45000
}
```

### 5 FILL_IN 桩替换（fixture_id + _driver 仿真实例化）

```json
{
  "fixture_id": "test_fixture_001",
  "fixture_replacements": 2,
  "commissioned_yaml": "<44990 chars, 见 F:\\Workspace\\MATRag\\data\\reports\\plans\\PA601-D54A.plan.commissioned.yaml>",
  "measure_script": "F:\\Workspace\\MATRag\\data\\reports\\plans\\PA601-D54A.measure_step.py",
  "measure_outputs": {
    "RESOURCES": [
      "DMM_1"
    ],
    "LIMITS": {
      "min": -54.8,
      "typ": -54.0,
      "max": -53.2
    },
    "ORIGIN": "SR-PA601-D54A-1201 / 场景 24 / -54V 额定输出（半载）",
    "result_measured": -54.00144090329578,
    "result_measured_value": -54.00144090329578,
    "result_passed": true,
    "result_cond_fingerprint": "",
    "measured_value": -54.00144090329578
  },
  "measure_status": "PASSED"
}
```

### 6 POST /sequences（yaml_content=commissioned plan）

```json
{
  "http": 201,
  "sequence_id": "13cf9b2d-9c38-4999-a488-57e4fd8de70b"
}
```

### 7 POST /executions（sequence_id + uut_sn）

```json
{
  "http": 201,
  "run_id": "0a69d192-77e1-40fc-924b-ed5186564422",
  "uut_sn": "PA601DEMO-0001"
}
```

### 8 execution_dispatch → ate.tasks.{exec_id}

```json
{
  "subject": "ate.tasks.0a69d192-77e1-40fc-924b-ed5186564422",
  "payload_bytes": 76048,
  "headers": {
    "execution_id": "0a69d192-77e1-40fc-924b-ed5186564422"
  }
}
```

### 9 edge 仿真执行（V32PlanDispatcher + 读数）

```json
{
  "outcomes": [
    "StepOutcome(step_id='fixture_clamp', step_type='fixture_control', status='PASS', detail=\"clamp on fixture 'test_fixture_001'\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1103_s001_g000_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1103_s001_g000_input_frequency', step_type='script', status='PASS', detail=\"script 'set_line_frequency.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1204_s001_g001_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1204_s001_g001_m_output_power', step_type='script', status='PASS', detail=\"script 'measure_output_power.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1600_s001_g002_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1600_s001_g002_m_telemetry_value', step_type='script', status='PASS', detail=\"script 'read_telemetry.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1413_s001_g003_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1413_s001_g003_m_signal_state', step_type='script', status='PASS', detail=\"script 'read_signal_state.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1300_s001_g004_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1300_s001_g004_m_protection_action', step_type='script', status='PASS', detail=\"script 'verify_protection_action.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1419_s001_g005_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1419_s001_g005_m_signal_state', step_type='script', status='PASS', detail=\"script 'read_signal_state.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1414_s001_g006_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1414_s001_g006_m_signal_state', step_type='script', status='PASS', detail=\"script 'read_signal_state.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1421_s001_g006_m_signal_state', step_type='script', status='PASS', detail=\"script 'read_signal_state.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1302_s001_g007_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1302_s001_g007_m_protection_action', step_type='script', status='PASS', detail=\"script 'verify_protection_action.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1100-m127_s001_g008_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1100-m240_s001_g009_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1210-m93_s001_g010_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1210-m93_s001_g010_m_efficiency', step_type='script', status='PASS', detail=\"script 'measure_efficiency.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1223_s001_g010_m_presence', step_type='script', status='PASS', detail=\"script 'verify_capability_present.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1301_s001_g010_m_protection_action', step_type='script', status='PASS', detail=\"script 'verify_protection_action.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1304_s001_g010_m_protection_action', step_type='script', status='PASS', detail=\"script 'verify_protection_action.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1305_s001_g010_m_protection_action', step_type='script', status='PASS', detail=\"script 'verify_protection_action.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1306_s001_g010_m_protection_action', step_type='script', status='PASS', detail=\"script 'verify_protection_action.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1307_s001_g010_m_protection_action', step_type='script', status='PASS', detail=\"script 'verify_protection_action.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1402_s001_g010_m_signal_state', step_type='script', status='PASS', detail=\"script 'read_signal_state.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1405_s001_g010_m_signal_state', step_type='script', status='PASS', detail=\"script 'read_signal_state.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1415_s001_g010_m_signal_state', step_type='script', status='PASS', detail=\"script 'read_signal_state.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1416_s001_g010_m_signal_state', step_type='script', status='PASS', detail=\"script 'read_signal_state.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1417_s001_g010_m_signal_state', step_type='script', status='PASS', detail=\"script 'read_signal_state.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1418_s001_g010_m_signal_state', step_type='script', status='PASS', detail=\"script 'read_signal_state.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1422_s001_g010_m_signal_state', step_type='script', status='PASS', detail=\"script 'read_signal_state.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1428_s001_g010_m_signal_state', step_type='script', status='PASS', detail=\"script 'read_signal_state.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1500_s001_g010_command', step_type='script', status='PASS', detail=\"script 'issue_command.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1601_s001_g010_m_telemetry_value', step_type='script', status='PASS', detail=\"script 'read_telemetry.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1603_s001_g010_m_telemetry_value', step_type='script', status='PASS', detail=\"script 'read_telemetry.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1604_s001_g010_m_telemetry_value', step_type='script', status='PASS', detail=\"script 'read_telemetry.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1606_s001_g010_m_telemetry_value', step_type='script', status='PASS', detail=\"script 'read_telemetry.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1608_s001_g010_m_telemetry_value', step_type='script', status='PASS', detail=\"script 'read_telemetry.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1613_s001_g010_m_telemetry_value', step_type='script', status='PASS', detail=\"script 'read_telemetry.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1101_s001_g011_input_voltage_1', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1101_s001_g011_input_voltage_2', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1102_s001_g012_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1210-m86_s001_g013_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1210-m86_s001_g013_m_efficiency', step_type='script', status='PASS', detail=\"script 'measure_efficiency.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1104-m85_s001_g014_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1104-m85_s001_g014_power_factor', step_type='script', status='PASS', detail=\"script 'configure_pf_measurement.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1210-m91_s001_g015_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1210-m91_s001_g015_m_efficiency', step_type='script', status='PASS', detail=\"script 'measure_efficiency.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1104-m95_s001_g016_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1104-m95_s001_g016_power_factor', step_type='script', status='PASS', detail=\"script 'configure_pf_measurement.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1303_s001_g017_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1303_s001_g017_m_protection_action', step_type='script', status='PASS', detail=\"script 'verify_protection_action.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1427_s001_g017_m_signal_state', step_type='script', status='PASS', detail=\"script 'read_signal_state.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1106_s001_g018_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1106_s001_g018_output_current', step_type='script', status='PASS', detail=\"script 'measure_input_current.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1105_s001_g019_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1105_s001_g019_power_event', step_type='script', status='PASS', detail=\"script 'trigger_power_event.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1104-m98_s001_g020_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1104-m98_s001_g020_power_factor', step_type='script', status='PASS', detail=\"script 'configure_pf_measurement.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1309-m18_s001_g021_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1309-m18_s001_g021_m_protection_action', step_type='script', status='PASS', detail=\"script 'verify_protection_action.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1200-m-54_s001_g022_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1200-m-54_s001_g022_m_output_voltage', step_type='script', status='PASS', detail=\"script 'measure_dc_voltage.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1203-m1_s001_g022_m_output_current', step_type='script', status='PASS', detail=\"script 'measure_dc_current.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1205-m62_s001_g022_m_output_voltage', step_type='script', status='PASS', detail=\"script 'measure_dc_voltage.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1206-m500_s001_g022_m_ripple', step_type='script', status='PASS', detail=\"script 'measure_ripple_20mhz.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1212-m5_s001_g022_m_timing', step_type='script', status='PASS', detail=\"script 'measure_transient_response.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1214-m5_s001_g022_m_timing', step_type='script', status='PASS', detail=\"script 'measure_transient_response.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1215-m300_s001_g022_m_timing', step_type='script', status='PASS', detail=\"script 'measure_transient_response.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1216-m2000_s001_g022_m_cap_load', step_type='script', status='PASS', detail=\"script 'verify_cap_load_capability.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1218_s001_g022_m_timing', step_type='script', status='PASS', detail=\"script 'measure_transient_response.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1310_s001_g022_m_protection_action', step_type='script', status='PASS', detail=\"script 'verify_protection_action.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1311_s001_g022_m_protection_action', step_type='script', status='PASS', detail=\"script 'verify_protection_action.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1211-m0.2_s001_g023_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1211-m0.2_s001_g023_m_timing', step_type='script', status='PASS', detail=\"script 'measure_transient_response.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1201-m-54.8_s001_g024_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1201-m-54.8_s001_g024_m_output_voltage', step_type='script', status='PASS', detail='measured_value=-54.00144090329578', attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1309-m5_s001_g025_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1309-m5_s001_g025_m_protection_action', step_type='script', status='PASS', detail=\"script 'verify_protection_action.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1200-m3.45_s001_g026_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1200-m3.45_s001_g026_m_output_voltage', step_type='script', status='PASS', detail=\"script 'measure_dc_voltage.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1201-m6_s001_g026_m_output_voltage', step_type='script', status='PASS', detail=\"script 'measure_dc_voltage.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1203-m1-c2_s001_g026_m_output_current', step_type='script', status='PASS', detail=\"script 'measure_dc_current.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1205-m8_s001_g026_m_output_voltage', step_type='script', status='PASS', detail=\"script 'measure_dc_voltage.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1206-m50_s001_g026_m_ripple', step_type='script', status='PASS', detail=\"script 'measure_ripple_20mhz.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1211-m0.005_s001_g026_m_timing', step_type='script', status='PASS', detail=\"script 'measure_transient_response.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1212-m5-c2_s001_g026_m_timing', step_type='script', status='PASS', detail=\"script 'measure_transient_response.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1214-m5-c2_s001_g026_m_timing', step_type='script', status='PASS', detail=\"script 'measure_transient_response.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1215-m20_s001_g026_m_timing', step_type='script', status='PASS', detail=\"script 'measure_transient_response.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1216-m100_s001_g026_m_cap_load', step_type='script', status='PASS', detail=\"script 'verify_cap_load_capability.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1222_s001_g026_m_presence', step_type='script', status='PASS', detail=\"script 'verify_capability_present.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='recover_before_SR-PA601-D54A-1308_s001_g027_input_voltage', step_type='script', status='PASS', detail=\"script 'power_cycle.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1308_s001_g027_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1308_s001_g027_fault_stimulus', step_type='script', status='PASS', detail=\"script 'inject_fault.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1308_s001_g027_m_protection_action', step_type='script', status='PASS', detail=\"script 'verify_protection_action.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='recover_before_SR-PA601-D54A-1308-c2_s001_g028_input_voltage', step_type='script', status='PASS', detail=\"script 'power_cycle.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1308-c2_s001_g028_input_voltage', step_type='script', status='PASS', detail=\"script 'set_ac_input.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1308-c2_s001_g028_fault_stimulus', step_type='script', status='PASS', detail=\"script 'inject_fault.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='SR-PA601-D54A-1308-c2_s001_g028_m_protection_action', step_type='script', status='PASS', detail=\"script 'verify_protection_action.py' (attempt 1)\", attempts=1)",
    "StepOutcome(step_id='fixture_release', step_type='fixture_control', status='PASS', detail=\"release on fixture 'test_fixture_001'\", attempts=1)"
  ],
  "total_steps": 104,
  "passed": 104,
  "power_cycle_fences": [
    "recover_before_SR-PA601-D54A-1308_s001_g027_input_voltage",
    "recover_before_SR-PA601-D54A-1308-c2_s001_g028_input_voltage"
  ],
  "target_step": "SR-PA601-D54A-1201-m-54.8_s001_g024_m_output_voltage",
  "measured_value": -54.00144090329578
}
```

### 10 execution_status_relay → DB + SSE

```json
{
  "events": 105
}
```

### 11 SSE → 前端 visible

```json
{
  "event_count": 106,
  "types": [
    "EXECUTION_STARTED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "STEP_COMPLETED",
    "EXECUTION_COMPLETED"
  ]
}
```

### V 验收：Execution.step_results 含 measured_value

```json
{
  "status": "COMPLETED",
  "step_results_count": 104,
  "target_step": "SR-PA601-D54A-1201-m-54.8_s001_g024_m_output_voltage",
  "measured_value": -54.00144090329578
}
```
