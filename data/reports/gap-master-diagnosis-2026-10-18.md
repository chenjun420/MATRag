# 数据链路缺口统一诊断 + 分级修复路线图

日期：2026-10-18
范围：ATEStudio + MATRag + mat-device-control + matrag-agent（bundle → 产测执行 → 审计/检索闭环）
方法：逐条核实散落缺口（grep + read 源码定位文件:行），并发现 2 处**报告原话与代码现状不一致**的更正。

---

## 结论先行

1. **最严重缺口不在已知清单顶部，而在"输出上抛"一跳（GAP-09）**：生产路径里 ATERag 脚本的 `measured_value`/`cond_fingerprint` **根本到不了** `Execution.step_results[].outputs`。A.4 demo 之所以能看到 `measured_value`，是因为它**手工把 `outputs` 塞进扁平事件**喂给 relay（`demo_e2e_simulation.py:762-772`），完全没走 `jetstream_worker._setup_status_forwarding` 的 forwarder。真实 NATS → worker → forwarder → relay 路径从未被 e2e 覆盖（GAP-08）。
2. **GAP-01（负轨 min/max 反向）根因不在 emit 模板**：`aterag_script_emit` 是忠实拷贝；反转发生在**上游 MATRag 抽取**（bundle 数据本身已反向）。任务前提"问题在 emit 模板"需更正。
3. **D3 方案 A（指纹进 plan params + emit 发射 `result_cond_fingerprint`）已实现**：`flow_planner.py:685,731` + `aterag_script_emit.py:231,257,352` 均已落码。但 GAP-09 使下游仍拿不到它。
4. **GAP-12（cloudbreaker）是误报**：不存在该模块；真正的 `CircuitBreaker`（`ate_platform/common/circuit_breaker.py`）被 6+ 服务正常引用。
5. **GAP-07（gateway ask payload）前提不准确**：gateway `ask` 参数名是 `query`（不是 `text`），matrag-agent 也发 `query`，静态一致，无错位。

---

## 1. 逐缺口核实结果

### GAP-01 负轨判据 min/max 反向 —— 根因在上游 MATRag 抽取（高）

- **证据**：`MATRag/data/exports/PA601-D54A.bundle.json:731-733` 已写死
  `"min": -53.2, "typ": -54.0, "max": -54.8`（min > max）。
- `aterag_script_emit.py:147-152` `limits_of()` 原样拷贝；`:334-348` `_judge` 用
  `lo=min, hi=max`，`out_of_range = (measured < lo) or (measured > hi)`。
  对正确读数 `-54.0014`：`-54.0014 < -53.2` 为真 → **误判 FAILED**。
- **更正**：任务写"问题在 emit 模板"——**不成立**。emit 忠实透传；反转来自
  MATRag 抽取层（`patterns.py:229-238 build_value` 照搬 `minimum/maximum`，
  而 `SpecParameter.minimum/maximum` 对负轨已反向）。修复点应在
  `MATRag/src/matrag/extract/`（或 emit/flow_planner 加"min≤max 归一"防御）。
- demo 用 `MATRag/scripts/demo_e2e_simulation.py:306-319 _sort_reversed_limits`
  在 **commission 时**绕过，生产路径未修。
- **严重度：高**（真机负轨测量会被误判，阻塞 .25 板 demo）。

### GAP-02 unique_steps 去重键不含 fingerprint —— 已被绑定唯一性兜底（低/P2）

- `aterag_script_emit.py:132-144` 去重键 `action|condition_kind|value`，无 fingerprint。
- fingerprint = `f(kind, side, value, method_ref)`（`MATRag/src/matrag/extract/fingerprints.py:37-49`），
  `method_ref` 由 `(kind, side)` 绑定表决定；`load_bindings` 对重复 `(kind, side)` 已
  `ValueError` 拦截（`flow_planner.py:107-112`）。
- ⇒ 相同 `(action, kind, value)` 必然同 fingerprint，**当前结构下碰撞不可发生**。
  仅当绑定表出现第二行同 `(kind, side)` 才会炸，而那条已被守卫挡住。
- **严重度：低**；建议加一句显式校验/注释即可（防未来改动）。

### GAP-03 设置步不写 fingerprint 的边界 —— 潜在 landmine（低/P2）

- emit 中 `set`（`:246-247`）只写 `result_applied`；`verify`（`:241-245`）只写
  `result_state/result_present`；只有 `measure`（`_judge_fn:352`）发射 `result_cond_fingerprint`。
- `failure_indexer._iter_condition_payloads:545-552`：一旦某步 outputs 出现**扁平非空**
  `cond_fingerprint` 而无 `measured_value`，`_require_measured_value` 会 **fail-fast 抛 ValueError**。
- 当前 set/verify 不发射指纹 → 不炸。但边界未被文档化/防御。
- **严重度：低**；建议在 `_iter_condition_payloads` 加注释或在 emit 加断言，明确"只有 measure 步带指纹"。

### GAP-04 Execution.status=RUNNING 与 started_at 无人写 —— 生产可见性缺口（中/P1）

- relay `_EXECUTION_LEVEL_EVENT_TYPES = {EXECUTION_COMPLETED, EXECUTION_FAILED}`
  （`execution_status_relay.py:79-81`），只有它们推进 `Execution.status`（`:347-356`）。
  无任何事件把执行置 RUNNING，也无 `STEP_STARTED→RUNNING` 投射。
- `Execution` 创建即 `PENDING`（`executions.py:516`）；`started_at` **全程无人写**
  （relay 只写 `completed_at`，`:356`）。`started_at` 被 `dashboard.py:382`、
  `test_trace_service.py:81`、`atml_exporter.py:177` 消费 → 恒为 NULL。
- **结论**：推荐 relay 增加"首个 STEP_STARTED → 置 RUNNING + 写 `started_at`"。
  与 R1.5"两域分离"不冲突（步骤级事件不再推进执行**终态**，但执行**起态**仍需一个投射源）。

### GAP-05 relay `_split_envelope` 扁平回退是否收敛 —— 决策项（低/P2）

- `execution_status_relay.py:90-115`：nested `{type, data}` 优先，`data` 非 dict 才回退扁平。
- 生产 forwarder 发 nested（`jetstream_worker.py:850-852`）；扁平回退仅服务旧生产者/e2e。
- 现状无害，但两套解析口径并存有漂移风险。建议：定稿后移除扁平回退，e2e 同步改 nested。

### GAP-06 breakpoint_registry 读扁平、failure_indexer 读嵌套 —— 口径不统一（低/P2）

- relay 调 `handle_status_event(..., {"type": event_type, **data})`（`:263-266`）重建扁平视图；
  breakpoint 读扁平 `event.get("run_id")/("type")`（`breakpoint_registry.py:297-300`）。
- 而 `failure_indexer.handle_status_event` 读嵌套 `raw_event.get("data")`（`failure_indexer.py:230`）。
- 两个消费方解析口径不同，靠 relay 中间做适配。建议统一为嵌套入参。

### GAP-07 gateway `ask` payload 与 matrag-agent rag_search —— 前提需更正（P1 验证）

- **更正**：gateway `ask` 注册参数是 `query`（`MATRag/src/matrag/gateway/tools.py:275`、`:154`），
  **不是 `text`**。任务前提"已加 text"与代码不符。
- matrag-agent `rag_search` 发 `call_tool("ask", query=query, top_k=top_k)`
  （`rag_search.py:109`）→ 与 gateway `query` 一致；`_normalize_chunks` 读 `citations`
  （ask 返回 citations 带 `text` 字段，`answer.py:113`）→ 一致。
- **静态无错位**，但 gateway 与 matrag-agent 均只跑了 stub 测试，无真机 spot 验证。
  建议：起真 gateway + 真 matrag-agent 打一次 `ask` 确认真实 JSON-RPC 参数名与返回结构。

### GAP-08 e2e 依赖真 NATS 被 skip —— 集成覆盖缺口（中/P1）

- `ATEStudio/tests/e2e/test_execution_e2e.py:49-60`：`_check_nats()` 探测 localhost:4222，
  `@_skip` 无服务即整类跳过；`tests/cloud/test_status_relay.py` 用 MagicMock。
- 真实 NATS → cloud relay → DB 闭环无自动化覆盖，这也是 GAP-09 能潜伏至今的根因。

### GAP-09 STEP_COMPLETED outputs 生产路径丢失 —— 最高优先（高/P0）

- **事实链**：
  1. 生产唯一步骤级事件 = `STEP_STATUS_CHANGED`，载荷 `{step_id, old_status, new_status, run_id}`
     （`step_registry.py:162-167`、`process_executor.py:511-517`），**无 outputs**。
  2. forwarder 从 `STEP_STATUS_CHANGED` 合成 `STEP_COMPLETED`（`jetstream_worker.py:899-906`），
     复制 `data`（仍无 outputs），再 `_collect_step_outputs`（`:894`）回退到
     `variable_space.get_step_vars(step_id)`（`:1007` → `variable_space.py:275-285`）。
  3. `get_step_vars` 读的是 `steps.<step_id>.*` 作用域，由 `ContextProxy`（@measure DSL 路径）写入；
     **ATERag 生成脚本走的是 `result_*` 模块命名空间**（`step_executor.py:474-480`），
     结果进 `StepResult.outputs`，loop_executor 只把它写进 **loop 作用域**（`loop_executor.py:483`），
     **从不写 `steps.<step_id>.*`**。
  4. ⇒ `_collect_step_outputs` 返回 `{}`；relay 落库 `step_results[step_id].outputs = {}`；
     `audit_report.measured_conditions` 恒为空（`failure_indexer.py:586-588` 忽略空 outputs）。
- **demo 假象**：A.4 demo 的 `_step10_relay` 手工构造扁平 `STEP_COMPLETED` 且
  `event["outputs"] = {"measured_value": measured, "pass": True}`（`demo_e2e_simulation.py:762-772`），
  直接喂 relay，**绕过 forwarder**；step 9 用的是 `V32PlanDispatcher` 仿真器，不是
  scanner_scheduler/loop_executor/ProcessStepExecutor 生产链。整条生产路径从未跑通。
- **结论**：这是 D3 指纹链路与"生产数据可见性"的**真正断点**。修复方向二选一：
  - (A) 让 executor 把 `StepResult.outputs` 写回 `VariableSpace.steps.<step_id>.*`；或
  - (B) 让 forwarder 从 `StepResult`/事件本身取 outputs（而非 VariableSpace）。
  推荐 (A)（与 `get_step_vars` 既有契约对齐，改动集中在执行收尾一处）。

### GAP-10 PowerShell 中文乱码 —— 非代码缺口（关闭）

- 纯终端显示问题；只影响 ruff/pytest 报告 PDF 化。建议报告导出用 UTF-8 重定向，不改代码。

### GAP-11 MATRag 主仓 live 测试未跑 —— LLM 路径回归缺口（低/P1）

- `MATRag/tests/conftest.py:16` 强制 `settings.live=False`；真实 LLM 用例
  `skipif(not settings.live)`（`test_parse_reconstruct.py:217`、`test_embeddings_client.py:426`）。
- 1326→1379 期间所有 LLM 路径走 StubLLM，无真 MaaS 凭据冒烟。建议 CI 加 scheduled live smoke。

### GAP-12 cloudbreaker / cb_* 长期未审计 —— 误报（关闭）

- 全仓 grep `cloudbreaker|Cloudbreaker` 与 `cb_` 均无业务命中；
  唯一断路器实现是 `ate_platform/common/circuit_breaker.py`，被
  `traceback_analyzer / diagnosis_service / hybrid_retriever / embedding_service /
  query_rewrite / script_generator` 正常引用。不存在"长期未审计的 cloudbreaker"。

---

## 2a. 分级矩阵

| 缺口 ID | 文件:行 | 类型 | 严重 | 修复预估 | 阻塞谁 |
|---|---|---|---|---|---|
| GAP-09 | jetstream_worker.py:894,899-906 + variable_space.py:275 + step_executor.py:474 | 输出上抛断链 | **高** | 1~1.5 人日 | 生产数据可见性 / .25 板 demo |
| GAP-01 | MATRag bundle.json:731-733（根因在 extract）→ aterag_script_emit.py:334 | 负轨 min/max 反向 | **高** | 0.5 人日 | .25 板 demo / 真机判定 |
| GAP-04 | execution_status_relay.py:79-81,347-356 | RUNNING/started_at 无人写 | 中 | 0.5 人日 | dashboard/trace 可见性 |
| GAP-08 | tests/e2e/test_execution_e2e.py:49-60 | e2e 依赖真 NATS 跳过 | 中 | 1 人日 | 集成测试覆盖 |
| GAP-07 | gateway tools.py:275 ↔ rag_search.py:109 | ask 参数名 spot 验证 | 中（验证） | 0.25 人日 | debug_agent RAG 路径 |
| GAP-11 | MATRag tests/conftest.py:16 | live 测试未跑 | 低 | 0.5 人日 | LLM 路径回归 |
| GAP-02 | aterag_script_emit.py:142 | 去重键不含指纹 | 低 | 0.25 人日 | （已被守卫兜底） |
| GAP-03 | aterag_script_emit.py:246-247 + failure_indexer.py:545 | 设置步指纹边界未文档化 | 低 | 0.25 人日 | （潜在） |
| GAP-05 | execution_status_relay.py:90-115 | _split_envelope 双 shape | 低 | 0.5 人日 | 收敛决策 |
| GAP-06 | execution_status_relay.py:263-266 + breakpoint_registry.py:297 | 解析口径不统一 | 低 | 0.25 人日 | 统一 |
| GAP-10 | — | 中文乱码 | 无 | 0 | 仅报告 PDF |
| GAP-12 | —（无此模块） | 误报 | 无 | 0 | 已关闭 |

---

## 2b. 三档分类

### P0（立即派发：阻塞 demo / 阻塞生产数据可见性）

- **GAP-09**：STEP_COMPLETED outputs 生产路径丢失。让 `measured_value`/`cond_fingerprint`
  真正落进 `Execution.step_results`，否则 D3 指纹链路和审计 `measured_conditions` 全部空转。
- **GAP-01**：负轨 min/max 反向。真机负轨读数会被 `_judge` 误判，.25 板 demo 直接挂。

### P1（生产化前补：demo 能跑但生产有风险）

- **GAP-04**：RUNNING / `started_at` 投射（dashboard/trace 可见性）。
- **GAP-08**：真实 NATS 闭环 e2e（否则 GAP-09 这类断点继续潜伏）。
- **GAP-07**：gateway `ask` ↔ matrag-agent `rag_search` 真机 spot 验证。
- **GAP-11**：MATRag live LLM 冒烟。

### P2（backlog）

- **GAP-02**：unique_steps 去重键显式校验/注释。
- **GAP-03**：设置步指纹边界文档化 + 防御守卫。
- **GAP-05**：`_split_envelope` 扁平回退收敛。
- **GAP-06**：breakpoint/failure 两消费方解析口径统一。
- **GAP-10 / GAP-12**：关闭（非代码缺口 / 误报）。

---

## 2c. 修复路线图

### P0 派发计划

| 派发 | 文件 | 工期 | 备注 |
|---|---|---|---|
| P0-1（GAP-09） | `ATEStudio/src/ate_platform/executor/step_executor.py`（或 loop_executor）+ `jetstream_worker.py` 收尾 | 1~1.5 人日 | 让 `StepResult.outputs` 写回 `VariableSpace.steps.<step_id>.*` 或改 forwarder 取数源；配套真实 exec 单测 + 真 NATS e2e |
| P0-2（GAP-01） | `MATRag/src/matrag/extract/`（定位负轨 min/max 反号点）+ 可选 `aterag_script_emit` 归一防御 | 0.5 人日 | 修根因；emit/flow_planner 加 `min≤max` 断言防御 |

### P1 列表

1. GAP-04：relay 加 `STEP_STARTED → RUNNING + started_at`。
2. GAP-08：NATS-in-docker CI lane 或文档化手工 e2e。
3. GAP-07：真机 spot 验证 gateway ask ↔ rag_search。
4. GAP-11：MATRag scheduled live smoke。

### P2 列表

GAP-02 / GAP-03 / GAP-05 / GAP-06（见 2b，逐条 0.25~0.5 人日）。

---

## 2d. 与现 lane 状态对照

### 已派发 / 已完成 lane 已覆盖的

| lane | 状态 | 覆盖了哪些 |
|---|---|---|
| R1.5（relay 两域分离 + nested 信封 + STEP_COMPLETED 合成） | 完成 | 步骤级/执行级状态分离、`{type,data}` 嵌套信封、执行级终态事件。**但暴露 GAP-04（RUNNING/started_at 未做）与 GAP-09（outputs 未上抛）** |
| D3（fingerprint 链路） | 完成（方案 A） | `flow_planner.py:685,731` 写 `cond_fingerprint`；`aterag_script_emit.py:231,257,352` 发射 `result_cond_fingerprint`。**下游被 GAP-09 掐断** |
| G2（gateway ask） | 完成 | ask 应答合成（参数名 `query`，非 `text`——GAP-07 澄清） |
| A.4（demo 仿真） | 完成 | 11 步仿真闭环。**但用 `_sort_reversed_limits` 绕过 GAP-01、用手造事件绕过 GAP-09**，两真实缺口未修 |

### 未覆盖、需新派发（仅这部分才需新派发）

- **P0**：GAP-09、GAP-01（均未被任何已派发 lane 真正修到根因）。
- **P1**：GAP-04、GAP-08、GAP-07、GAP-11。
- **P2**：GAP-02、GAP-03、GAP-05、GAP-06。
- **关闭**：GAP-10、GAP-12。

---

## 附：关键证据索引

- 负轨反号证据：`MATRag/data/exports/PA601-D54A.bundle.json:731-733`、`MATRag/src/matrag/extract/patterns.py:229-238`、`ATERag emit _judge` `aterag_script_emit.py:334-348`
- 输出断链证据：`step_registry.py:162-167`、`process_executor.py:511-517`、`jetstream_worker.py:894,899-906,1007`、`variable_space.py:275-285`、`step_executor.py:474-480`、`loop_executor.py:483`
- demo 绕过证据：`MATRag/scripts/demo_e2e_simulation.py:306-319（_sort_reversed_limits）、762-772（手造 outputs）`
- 指纹契约：`MATRag/src/matrag/extract/fingerprints.py:37-49`
- RUNNING/started_at：`execution_status_relay.py:79-81,347-356`、`executions.py:516`、`models/execution.py:27,45`
