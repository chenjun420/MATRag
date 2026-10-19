# ATERag/AteStudio 产测链路 `cond_fingerprint` 数据缺口分析与应对方案

日期：2026-10-18
范围：MATRag `bundle.json` → ATEStudio 产测执行 → `audit_report.measured_conditions`
结论先行：**推荐方案 A**（改脚本发射模板，让 `.py` 脚本输出 `result_cond_fingerprint`），前提是先在 `flow_planner` 把指纹写进 `PlanStep.params`。指纹的最早丢失点不在 `execution_status_relay`，而在 `flow_planner._plan_scenario`。

---

## 1. 数据流追踪（每一跳标注 fingerprint 是否传递）

| # | 环节 | 文件 / 位置 | fingerprint |
|---|------|------------|-------------|
| 0 | MATRag 导出 bundle | `MATRag/src/matrag/extract/bundle.py:61` `ClauseModel.cond_fingerprint: str` | ✓ 存在（契约字段，`max_length=64`） |
| 1 | bundle.json 实际导出 | `MATRag/data/exports/PA601-D54A.bundle.json:36` 起 | ✓ 有值（如 `078f88ab6a2b5e17`） |
| 2 | 契约镜像（消费侧） | `ATEStudio/src/ate_cloud/schemas/aterag_bundle.py:62` | ✓ 字段对齐（`contract_hash` 冻结同基线 `59a852...`） |
| 3 | 导入落库 | `ATEStudio/src/ate_cloud/services/aterag_importer.py:366-394` | ✓ 写入 `TestCondition.cond_fingerprint`（唯一键 `(owner_type, owner_id, cond_fingerprint)`，`models/test_conditions.py:84,100`） |
| 4 | **流程规划 → PlanStep** | `ATEStudio/src/ate_cloud/services/flow_planner.py:677-685`（input）、`:722-730`（output） | ✗ **丢失点①**：`PlanStep.params` 只含 `condition_kind / condition_text / value / unit / method_ref / scenario_bindings / rail`，**没有 `cond_fingerprint`**，尽管此处 `cl.cond_fingerprint` 就在手边 |
| 5 | 计划 YAML | `flow_planner.py:148-166` `to_yaml_dict`（`params` 原样序列化） | ✗ 继承丢失（params 无此键） |
| 6 | **脚本发射** | `ATEStudio/src/ate_cloud/services/aterag_script_emit.py:224-311` `_render` | ✗ **丢失点②**：模板只产出 `result_measured / result_measured_value / result_passed`（measure）`/ result_state / result_present`（verify）`/ result_applied`（set），**不产出 `result_cond_fingerprint`**（即便 params 有，模板也没读） |
| 7 | 执行收集 | `ATEStudio/src/ate_platform/executor/step_executor.py:474-480`（`result_*` → `outputs`，去掉 `result_` 前缀）；`process_executor.py:582-600` 同协议 | ✗ outputs 无 `cond_fingerprint`（本跳是**通用透传**，`result_cond_fingerprint` 若存在会被自动收集为 `outputs["cond_fingerprint"]`） |
| 8 | NATS 状态事件 | `ATEStudio/src/ate_platform/scheduler/jetstream_worker.py:663-681` `_setup_status_forwarding` → `ate.status.{execution_id}` | ✗（且见 §4 风险 R1：事件形状存疑） |
| 9 | 落库 step_results | `ATEStudio/src/ate_cloud/services/execution_status_relay.py:246-254` | ✗ 只存 `outputs`（原样，不含 fingerprint） |
| 10 | 审计聚合 | `ATEStudio/src/ate_cloud/services/failure_indexer.py:522-586` | ✗ `_iter_condition_payloads` 读 `outputs["cond_fingerprint"]` 或 `{fp: {...}}`，都为空 → **`measured_conditions` 返回空列表** |

关键事实：**指纹在跳 4（flow_planner）第一次被丢弃**，随后跳 6（脚本发射）再次丢失。下游 `execution_status_relay`（跳 9）与 `failure_indexer`（跳 10）其实**已经具备**消费指纹的能力——`failure_indexer._iter_condition_payloads`（`failure_indexer.py:534-554`）已实现扁平单条件 `{"cond_fingerprint": fp, "measured_value": v, "pass": b}` 与多条件映射两种形态，且有单测覆盖（`tests/unit/test_failure_indexer_audit_report.py:156-170`）。

也就是说：**只要脚本的 `outputs` 里出现 `cond_fingerprint`，下游零改动即可贯通。** 缺口纯粹在"上游没把指纹送进脚本"。

---

## 2. 三个修复方案对比

### 方案 A：改脚本发射模板，输出 `result_cond_fingerprint`（推荐）

改动点：
- `flow_planner.py` `_plan_scenario`：在 input/output 两个 `params={...}` 里各加一行 `"cond_fingerprint": cl.cond_fingerprint`。
- `aterag_script_emit.py` `_render`：在生成脚本里加一条模块级 `result_cond_fingerprint = "..."`（与 `result_measured_value` 并列）。

优点：
- **与"数据来源是 MATRag"的决策一致**：指纹从 bundle 原样抄到脚本、随数据一路走，不重算、不反查、无第二事实源。
- **下游零改动**：executor 的 `result_*` 通用收集（跳 7）自动把它变成 `outputs["cond_fingerprint"]`；relay（跳 9）原样存；`failure_indexer._iter_condition_payloads`（跳 10）已认这个扁平形态。
- **无 DB 迁移**、无新写路径。
- 改动面最小、最可测（字符串断言 + 真实 exec 协议单测）。

缺点/成本：
- 脚本是生成产物，模板改后需**重新生成全部 `.py` 脚本**（部署注意点）。
- 指纹以字面量烧进脚本，脚本重生成后指纹才会更新（这符合"脚本与绑定表保持同步、手改会被覆盖"的既有约定）。

### 方案 B：改 `execution_status_relay`，从 plan_yaml 反查 step→condition

改动点：`execution_status_relay.py` 在 `_update_db` 里按 `Execution.sequence_id → Sequence.yaml_content` 反查 `step_id → cond_fingerprint` 映射，回填进 `step_results[step_id]`。

优点：
- 不改脚本、不改 executor 协议。

缺点（致命）：
- **前提同样要先改 `flow_planner`**：当前 plan YAML 的 params 里根本没有 fingerprint（跳 4 已丢），反查无从查起。等于 A 的 flow_planner 改动照做，再叠一层 relay 复杂度。
- **把热路径（每个状态事件）变成 YAML 解析 + 按 step_id 关联**，引入延迟与失败面；且 step_id 必须与 plan YAML 里的 id 精确一致（`flow_planner` 的 id 形如 `{code}_s001_g000_m_output_voltage`），对不上即静默丢失。
- **第二事实源风险**：plan_yaml 是"草稿"，测试工程师上机前可手改；手改后与 DB 里的 `TestCondition` 脱钩，反查出来的映射会漂移。违背"指纹从 MATRag 原样来"的单源原则。

### 方案 C：DB 加 `TestCondition.measured_value`，execution 完成后回写

改动点：`models/test_conditions.py` 加列 + alembic 迁移 + 执行完成后的回写钩子 + `audit_report` 改从 `TestCondition` 读。

缺点（否决）：
- **语义错误**：`TestCondition` 是"条件**定义**"（approved 判据，唯一键幂等），不是"本次**测量结果**"。把测量值写回定义行，混淆"该测什么"与"这次测到了什么"。
- **无逐次历史**：多次执行互相覆盖，audit 需要的是 per-execution 结果，而 `step_results` 本来就有 per-execution 存储。
- **代码量最大**：新列 + 迁移 + 新写路径 + 改 audit 读路径，且回写需在 execution 完成后触发（新增生命周期钩子）。
- 与 A 相比没有任何一项更优，只有更多风险。

**对比小结**：A 是唯一"把指纹从 MATRag 单源一路带到审计"且下游零改动的方案；B 与 C 要么引入第二事实源/热路径复杂度，要么语义错位 + 迁移成本。**推荐 A。**

---

## 3. 推荐方案 A 实施路径

### 3.1 文件改动清单

| 文件 | 改动 | 行数估算 |
|------|------|---------|
| `ATEStudio/src/ate_cloud/services/flow_planner.py` | `_plan_scenario` 两处 `params={...}`（input ~`:677-685`、output ~`:722-730`）各加 `"cond_fingerprint": cl.cond_fingerprint,` | 2 行 |
| `ATEStudio/src/ate_cloud/services/aterag_script_emit.py` | `_render`：`body_call`（measure/verify/set）之外，加模块级 `result_cond_fingerprint = {fp!r}`（从 `step.params.get("cond_fingerprint", "")` 取） | ~4 行 |
| `ATEStudio/tests/unit/test_flow_planner.py` | `_clause` 辅助函数补 `cond_fingerprint` 参数；新增断言 params 携带指纹 | ~6 行 |
| `ATEStudio/tests/unit/test_aterag_script_emit.py` | `_step` 补 `params["cond_fingerprint"]`；新增断言生成脚本含 `result_cond_fingerprint`，并经 `_exec_generated` 验证 outputs 含 `cond_fingerprint` | ~10 行 |
| （可选）`tests/unit/test_failure_indexer_audit_report.py` | 已覆盖扁平形态，无需改；可加一条"emit 产物 outputs → audit"的贯通用例 | 0~15 行 |

> 说明：`failure_indexer._iter_condition_payloads` 的扁平形态已实现（`failure_indexer.py:534-541`），实测 `outputs = {"measured": v, "measured_value": v, "passed": True, "cond_fingerprint": fp}` 会命中 `fingerprint = outputs.get("cond_fingerprint")` → `measured_value` 取自 `outputs["measured_value"]` → 产出 `{fp: {measured_value, pass}}`，`audit_report` 直接输出。下游无需改动。

### 3.2 实施顺序

1. 改 `flow_planner._plan_scenario`（两处 params 加指纹）→ 跑 `test_flow_planner`。
2. 改 `aterag_script_emit._render`（发射 `result_cond_fingerprint`）→ 跑 `test_aterag_script_emit`（含真实 exec 协议用例）。
3. 重新生成计划与 `.py` 脚本（`push_to_studio.py` 链路），上传前 `plan_gaps` 门照常。
4. 端到端验证：跑一批 → `Execution.step_results[step_id].outputs` 出现 `cond_fingerprint` → `audit_report` 的 `measured_conditions` 非空。

### 3.3 工期估算

- 代码 + 单测：**0.5 ~ 1 天**。
- 回归（`pytest` 全绿 + `ruff`）：随改动完成，无新增依赖。
- 联调（重新生成脚本 + 真实/仿真执行一次）：**0.5 天**。
- 合计：**1 ~ 1.5 人日**。

---

## 4. 风险评估

- **R1（需主会话确认）事件形状与 relay 的对齐**：`jetstream_worker._setup_status_forwarding`（`jetstream_worker.py:670-678`）把事件包成 `{"type": ..., "data": {...}}` 发到 `ate.status.{execution_id}`；而 `execution_status_relay._update_db`（`execution_status_relay.py:246-253`）读的是**扁平** `event["run_id"/"step_id"/"outputs"]`。e2e 与 relay 单测都用扁平事件（`tests/e2e/test_execution_e2e.py:229-253`、`tests/cloud/test_status_relay.py:140-146`）。若产线实际走 forwarder 的嵌套包，`run_id` 取不到、DB 更新会被跳过——这是**独立于本缺口**的既有隐患，方案 A 生效的前提是 `outputs` 确实落到 `step_results`。建议主会话核对真实事件形状后再上线。
- **R2 去重与指纹的一致性**：`aterag_script_emit.unique_steps`（`:132-144`）按 `(action, condition_kind, value)` 去重生成脚本，而 `cond_fingerprint` 由 `(kind, side, value, method_ref)` 决定。若两条不同需求/场景的条款在 `(action, kind, value)` 上撞车、但 `method_ref` 不同（→ 指纹不同），烧进单脚本的只有一个指纹，另一个条件失去归因。实际 `method_ref` 来自绑定表（同 kind 同值），撞车概率低，但需在 `unique_steps` 去重键或脚本生成处**显式校验/记录**，避免静默丢归因。
- **R3 失败步骤无 outputs**：`step_executor._run_script`（`:482-484`）在 `AssertionError` 分支返回的 `StepResult` **不带 outputs**，且 `_judge` 在超差时先抛错、不写 `result_measured_value`（`aterag_script_emit.py:337-344`）。因此"超差/失败"的测量值只出现在错误文本（`failed_steps.reason`），不会进 `measured_conditions`。这是既有设计（失败归因走 `step_id + reason`），方案 A 不改其语义，但需与产品对齐：`measured_conditions` 只含**通过且在界**的测量值。
- **R4 脚本再生成窗口**：指纹烧进脚本后，模板/绑定表变更需重新生成脚本，否则线上跑的是旧指纹脚本。已由既有"手改副本会被覆盖、与绑定表同步"约定约束，但需在发布流程里显式挂钩"重新生成"步骤。
- **R5 空指纹**：`ClauseModel.cond_fingerprint` 默认 `""`（`aterag_bundle.py:62`）。空串会被 `_iter_condition_payloads` 的 `isinstance(fp, str) and fp` 判为假而跳过归因。应确认导入侧对空指纹已有 fail-fast（`aterag_importer.py:372` 已记 `缺 cond_fingerprint` unmapped），不会让空指纹进入产测判据。

---

## 附：关键证据索引

- 指纹契约字段：`MATRag/src/matrag/extract/bundle.py:61` / `ATEStudio/src/ate_cloud/schemas/aterag_bundle.py:62`
- 指纹丢失点①：`ATEStudio/src/ate_cloud/services/flow_planner.py:677-685`、`:722-730`（params 无 `cond_fingerprint`）
- 指纹丢失点②：`ATEStudio/src/ate_cloud/services/aterag_script_emit.py:233-251`（`body_call`）、`:325-345`（`_judge_fn` 只设 `result_measured_value/result_passed`）
- executor 通用收集：`ATEStudio/src/ate_platform/executor/step_executor.py:474-480`
- relay 落库：`ATEStudio/src/ate_cloud/services/execution_status_relay.py:246-254`
- 审计消费（已支持扁平形态）：`ATEStudio/src/ate_cloud/services/failure_indexer.py:522-586`
- 下游单测（扁平/映射两形态）：`ATEStudio/tests/unit/test_failure_indexer_audit_report.py:133-170`
