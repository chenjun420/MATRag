# MATRag × ATEStudio × ATETest 架构升级方案（评审稿 v2）

> ⚠️ **本文件为方案设计稿（v0 评审稿）。最终落地状态、72 任务完成度、测试基线与决策矩阵走查请见 `architecture-upgrade-completion-2026-10-18.md`（完成报告 v1）。本文件保留作为历史方案 baseline 备查。**

> **范围**：基于已完成的全链路侦察（MATRag P0→P2→P3-0 + ATEStudio 完整架构 + ATETest 三套独立子项目）给出的渐进升级路径与实现方案。
> **作者立场**：MATRag 主线已全 done；本方案聚焦「让 plan → 真机执行跑通」与「Agent 闭环」两条主线。
> **战略冻结前提**：ATEStudio/ATETest 暂缓重构 — 本方案在「**不重写**」前提下给出渐进路径；重构建议作为附录。
> **日期**：2026-10-18

---

## 0. 关键修正（与 v1 的差异）

侦察后修正了三条关键假设：

| v1 假设 | 真实情况 | 影响 |
|---|---|---|
| ATETest 是统一的产测执行层 | ATETest 是 **3 个独立子项目**，无共享 schema、无 MATRag/ATEStudio 引用 | 不再假设"ATETest 提供 step_executor" |
| step_executor 归属未定 | ATEStudio 自带 `src/ate_platform/executor/step_executor.py` + `scheduler/jetstream_worker.py`；云↔边靠 NATS subject `ate.tasks.{exec_id}` 通信 | 重新定义"链路层" = ATEStudio 云↔边的 dispatch + 观测/存档层 |
| MATRag/ATEStudio 边界已清晰 | ATEStudio 含**完整 edge 引擎**（drivers/scheduler/simulation/recorder），与 MATRag 之间只有 bundle 一份 JSON 接触面 | 不需要"跨仓解耦"；需要"实机验证 + 设备池接 mat-device-control"决策 |

---

## 1. 当前状态盘点（v2 校准）

### 1.1 MATRag（生产侧 + 知识 + 计算 — done）
| 模块 | 状态 | 关键产物 |
|---|---|---|
| P0 解析服务 | ✅ done | 三格式 PDF/MD → data/corpus |
| P1 RAG 核心 | ✅ done | 1711 chunks 索引 (sqlite-vec + FTS5 + KG) |
| P1 知识层 | ✅ done | 术语 168 + 公理 76 + 方法 32 + 本体 267 属性 + KG 1222/4062 |
| P2 抽取投影 | ✅ done | 三产品 bundle 6/6 PASS dry-run |
| P3-0 计算路径 | ✅ done | Monty 沙箱 + tool calling + SymPy 反解 |
| **P3-1/2/3 Agent 服务** | ⏳ backlog | dev_agent / debug_agent / 服务骨架（**本方案主线之一**） |
| 测试 | ✅ done | 1326 pytest passed + ruff 全绿 |

### 1.2 ATEStudio（消费侧 + 编排 + 边缘执行 — 大体完工）
**关键结构**：
- `src/ate_cloud/`（云侧 FastAPI，~50 services / ~40 routers / 28 表）
  - `schemas/aterag_bundle.py`（契约镜像 + contract_hash 闸门）
  - `services/flow_planner.py:plan_flow()`（BundleModel → FlowPlan）
  - `services/aterag_importer.py`（bundle → DB 幂等落档）
  - `services/aterag_script_emit.py`（plan → DSL v3.2 脚本模板）
  - `services/aterag_agent.py`（**只读** LangChain over MATRag MCP + ALLOWED_TOOLS 白名单）
  - `services/aterag_catalog.py`（经 MCP `list_models` 取型号）
  - `services/execution_dispatch.py`（publish YamlPlan → NATS `ate.tasks.{exec_id}`）
  - `services/plan_materializer.py`（Execution → Sequence → YamlPlan）
  - `api/v1/imports.py`（`POST /api/v1/imports/aterag/plan` 评审 / `POST /api/v1/imports/aterag` 落库）
  - `scripts/verify_bindings.py`（词表双向差集）/ `verify_e2e*.py` / `push_to_studio.py`
- `src/ate_platform/`（**边缘执行引擎，自带**）
  - `executor/step_executor.py`（执行 step 的 Python 模板 + result_* 协议：return=PASSED / AssertionError=FAILED / 其它=ERROR，无 SKIPPED）
  - `scheduler/{jetstream_worker,station_orchestrator,resource_manager,condition_evaluator,...}.py`（订阅 `ate.tasks.*`、调度、fault_predictor、CPSAT 排产）
  - `drivers/{base,base_hal,base_mal,capabilities,tcp_instrument}.py` + `pymeasure_wrappers/` + `grpc/` + `mock/`
  - `simulation/{headless_runner,full_chain_simulator,fault_injector,...}.py`（仿真执行）
  - `recorder/{execution_recorder,replay_executor}.py`（录制 + 回放）
  - `proxy/proxy_manager.py`（直连/代理设备）
- `src/shared/`（跨端契约）
  - `dsl.py`（YamlPlan/YamlStep/ExecutionMode/LoopType）
  - `types.py` / `events.py` / `measurement.py` / `config_schema.py` / `multi_station.py` / `product_config.py` / `fixture_topology.py` / `operator_checkpoint.py` / `test_limits.py`
- **云↔边通信**：NATS JetStream subject `ate.tasks.{execution_id}`；`scripts/push_to_studio.py` 负责草稿 gate

### 1.3 ATETest（**3 个独立子项目，无统一执行层**）

| 子项目 | 内容 | 与 MATRag/ATEStudio 关系 |
|---|---|---|
| `backend/` | PageIndex RAG 服务（FastAPI + SQLite + MCP；与产测**无关**） | 零（业务事实零命中） |
| `mat-device-control/` | 设备控制独立仓（FastAPI + MQTT 桥；DeviceController 反射 `device_id.method()`；**无设备操作 REST 端点**；SCPI TCPIP 真实 + USB/GPIB/Modbus 占位） | 零，但**可作为 ATEStudio edge 的真机设备池替代** |
| `agent/hardware-test-agent/` | DeerFlow 2.0 多 Agent（Parser→Writer→Coder→Deployer→Debugger；LLM 生成 pytest 脚本） | 零，独立工作流 |

**结论**：ATETest 在主流程上是**外部旁观者**。`mat-device-control` 是有潜在价值的真机设备池候选；其余两块与本方案无关。

---

## 2. 端到端链路现状（v2 校准）

```
MATRag ────[bundle.json]──> ATEStudio (cloud) ────[YamlPlan + NATS]──> ATEStudio (edge/ate_platform)
  P2                            P2.5/aterag_importer + flow_planner + script_emit + execution_dispatch
                                                                           │
                                                                           ├──> ate_platform/executor/step_executor.py
                                                                           │     （drivers/ 中的 resource: PSU_1/ELOAD_1/... 抽象）
                                                                           │
                                                                           └──> ?真实设备?（当前可能走 simulation/ 路径）

ATETest ────────── 旁观，无连接 ───────────────
  mat-device-control  ────────── 候选真机设备池 ──────────────（待决策，见 §5.1）
```

**链路 1~4 完整，链路 5 缺决策**：是接 mat-device-control，还是用 ate_platform 内置的 `drivers/` + pymeasure？

---

## 3. 架构洞察（v2 校准）

### 3.1 双侧独立契约 → 已无消除空间
- bundle schema 在两侧完全独立声明，靠 contract_hash 守约；
- 任何字段新增需两侧 PR 联动（minor +1）。

### 3.2 词表三侧同步 → 现有纪律已强
| 侧 | 文件 | 现状 |
|---|---|---|
| MATRag | `matrag/extract/kinds.py:KIND_UNITS`（25 (kind,side)） | `validate_clause_kind` 闸门 |
| ATEStudio | `config/test_method_bindings.yaml`（25 + action/settle/destructive 等） | `scripts/verify_bindings.py` 双向差集 |
| ATEStudio | `services/aterag_script_emit.py:DRIVER_TABLE`（action → driver） | 当前无跨文件校验 |
| MATRag | `data/domain/source/power_test_methods.yaml`（32 方法） | method_ref 引用基 |

**缺**：跨仓一次性四端联检脚本（MATRag ↔ ATEStudio CI gate）。

### 3.3 ATEStudio `__init__.py` 启动耦合债
- `ate_cloud/__init__.py:1` 直接 `from .main import app` → `main.py` 顶 `import nats; from nats.aio.client import Client` → `lifespan` 强制 NATS/JetStream 连通性
- `verify_bundle_ate.py` 已用 shim 绕开 → 是症状处理

### 3.4 sign-off 状态有"事实层"但缺"链路层"
- `_apply_review_state(db, bundle)` 通过 DB 比对 `cond_fingerprint` 把已签字的 draft 升 approved
- `plan_flow()` 红线：include_draft=False（`flow_planner.py:424`），仅 approved 条件入 plan
- **风险**：DB ↔ bundle 状态分两层；plan_gaps 的"无判据"判定只看 plan 阶段不查 DB

### 3.5 ATEStudio 内置 edge 引擎 vs MATRag 计算能力 = 边界清晰
- ATEStudio edge：`ate_platform/executor/step_executor.py` + `drivers/` — 设备级执行
- MATRag 计算：`src/matrag/domain/{tool_agent.py,script_sandbox.py,planner.py}` — 离线推理/反解
- **不重叠**：MATRag 不应接管设备控制；ATEStudio 不应做 LLM 反解

### 3.6 增量导入 + 失败回溯 = 缺失
- MATRag 重导整个 bundle（CY 24 / GDC 51 / PA601 76 reqs 全量）
- ATEStudio 导入端点 `POST /api/v1/imports/aterag` 全量 upsert
- **缺**：spec 修订只改了 5 行 → 应只 diff 这 5 行

### 3.7 Agent 服务层缺口 = 本方案主线
- ATEStudio 已有 **只读** agent（`aterag_agent.py` over MATRag MCP）
- MATRag 已有 **计算路径**（tool_agent.py + Monty + SymPy）
- **缺**：
  - MATRag 侧的产测 Agent 服务骨架（P3-1）
  - 产测软件开发 Agent（P3-2）
  - 调测 Agent（P3-3）
  - ATEStudio 侧的诊断 Agent（用 MATRag agent 模式分析 failure_indexer 落档）

---

## 4. 升级方案：分层架构（**不重写**前提）

### 4.1 总体分层

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Layer 4: Agent 服务（P3-1/2/3 — 本方案主线）— Pydantic AI + MCP-HTTP    │
│  - dev_agent: 自然语言 → bundle 提案 → plan 评审                            │
│  - debug_agent: execution failures → 诊断 → 修复建议 → 重新规划              │
│  - review_agent: bundle 评审 (plan_gaps 解释)                              │
├──────────────────────────────────────────────────────────────────────────┤
│  Layer 3: 链路层（Phase A 主线）                                            │
│  - PUSH: bundle → plan_yaml → push_to_studio.py（草稿 gate 已就绪）       │
│  - DISPATCH: plan → Execution → plan_materializer → NATS ate.tasks.*       │
│  - EXEC: ATEStudio edge (ate_platform) 消费 → step_executor → 设备          │
│  - OBSERVE: execution_status_relay + SSE bridge → 前端                    │
│  - 缺位: 真机设备池接 mat-device-control（决策点 §5.1）                      │
├──────────────────────────────────────────────────────────────────────────┤
│  Layer 2: 三仓按各自强项演化                                                 │
│  - MATRag (RAG + 抽取 + 计算)                                              │
│  - ATEStudio (cloud + edge + recorder + simulation 自闭环)                  │
│  - mat-device-control (可选: 真机设备池替代 ate_platform/drivers)            │
├──────────────────────────────────────────────────────────────────────────┤
│  Layer 1: 数据契约 (ATERag bundle)                                          │
│  - contract_hash = 59a852c26ca277e4141c6a60ae723b20                       │
│  - 双侧独立声明, hash 守约                                                  │
├──────────────────────────────────────────────────────────────────────────┤
│  Layer 0: 规格书 / 知识 / 数据                                              │
│  - MATRag data/corpus, data/domain                                          │
│  - ATEStudio DB (28 表), shared/dsl.py (YamlPlan)                          │
│  - mat-device-control devices.toml (若集成)                                │
└──────────────────────────────────────────────────────────────────────────┘
```

### 4.2 关键设计原则（v2 校准）

1. **不重写**：
   - ATEStudio `ate_cloud/` + `ate_platform/` 全部不动；只补**链路层**观测 + Agent 集成
   - mat-device-control 维持原状（独立仓）；只在 Layer 3 做**集成适配**
   - MATRag 已 done 部分不动；Agent 服务是新增

3. **数据面守约**：
   - 三仓之间**只通过 JSON / 文件 / HTTP / NATS** 通信
   - bundle 落盘 + contract_hash 是唯一信任根

4. **Agent 只读 + 强制红线**：
   - 沿用 ATEStudio `aterag_agent.py` 的 ALLOWED_TOOLS 白名单 + DENIED 黑名单双轨
   - 不让任何 agent 触"代签"（MATRag LLM draft 红线已冻结）
   - agent 写操作走确定性服务（plan_importer / failure_indexer / calibration_manager）

5. **测试纪律**：
   - 所有新增模块必须 offline-runnable（stub LLM / stub NATS / 仿真设备）
   - 真机 / 真 LLM 用例按 `pytest.mark.skipif(not settings.live)` 跳过
   - 仿真优先于真机：`ate_platform/simulation/headless_runner.py` + `full_chain_simulator.py` 已就位

---

## 5. 分阶段实施路线（v2）

### Phase A: 链路层补齐 + 真机 demo（5~7 周，最高优先级）

**目标**：让"bundle → 真机执行 → 结果回传"全链路跑通一台 demo 板。

#### A.0 决策点（**评审必答**）

**A.0.1 真机设备池策略**（决定 Phase A.2 路线）

| 选项 | 描述 | 优势 | 劣势 |
|---|---|---|---|
| **α 沿用 ATEStudio edge drivers** | 用 `ate_platform/drivers/` + pymeasure + grpc | 单仓闭环；ATEStudio 已有完整抽象 | 需要逐设备驱动开发；pymeasure 库覆盖度有限 |
| **β 接 mat-device-control** | ATEStudio edge 通过 HTTP/MQTT 调 mat-device-control | mat-device-control 已有 TCPIP SCPI 真实现；MQTT 桥已就位 | 跨仓；mat-device-control 无设备 REST，需加适配层 |
| **γ 双轨** | 仿真端用 α；真机端用 β | 取长补短 | 维护两套；测试矩阵翻倍 |

**推荐**：先 β 探针 → 视结果决定后续（避免一次性押注）。

#### A.1 跨仓契约同步门（1 周，**评审必先做**）
- **新增 `tools/contract_sync/`**（MATRag 侧 — 不污染 matrag 包）：
  - `tools/contract_sync/check_vocab.py`:
    - 读 `matrag/extract/kinds.py:KIND_UNITS`
    - 读 `data/domain/source/power_test_methods.yaml`（method ids）
    - 读 `ATEStudio/config/test_method_bindings.yaml`
    - 读 `ATEStudio/src/ate_cloud/services/aterag_script_emit.py:DRIVER_TABLE`
    - 校验：25 (kind, side) ⊆ 3 侧；method_ref ⊆ yaml；action ⊆ DRIVER_TABLE
    - 输出差异报告 + 退出码 0/1（CI gate）
  - `tools/contract_sync/check_contract_hash.py`:
    - 两侧独立算 `contract_hash`，diff
- **测试**：`tests/test_contract_sync.py`（离线三产品 mock）
- **CI 接入**：MATRag 与 ATEStudio 各跑一次

#### A.2 真机设备池适配（β 路线，3 周）
**新建 `services/mat_device_adapter/`**（挂在 MATRag — 因 adapter 与 RAG 无关，独立包或挂在 gateway 均可；建议挂在 ATEStudio edge 内的 `ate_platform/drivers/mat_device_adapter/`，避免跨仓）：
- `mat_device_adapter.py`:
  - 类 `MatDevicePool` 包装 `mat_device_control.DeviceController` 反射调用
  - 实现 ATEStudio edge 期望的接口（`base.py:BaseDriver` 子类）：
    - `_connect_impl(self, target) -> None`
    - `_write_impl(self, command, payload) -> None`
    - `_read_impl(self, query) -> dict[str, Any]`
    - `_query_impl(self, query) -> dict[str, Any]`
  - 设备名映射表（mat-device-control `device_id` ↔ ATEStudio `resource: PSU_1`）：
    - `psu001` ↔ `PSU_1`
    - `scope001` ↔ `SCOPE_1`
    - `eload001` ↔ `ELOAD_1`
- `tcp_dmm_via_mat.py`、`tcp_psu_via_mat.py`（具体仪器驱动，按需）
- **关键工程取舍**：
  - 选项 b1：**HTTP 适配**（mat-device-control 加 `POST /devices/{id}/call`）→ 干净但需改 mat-device-control
  - 选项 b2：**MQTT 适配**（ATEStudio edge 通过 MQTT 桥调 mat-device-control）→ 复用现有桥
  - 选项 b3：**进程内 import**（ATEStudio edge 直接 import mat-device-control 模块）→ 紧耦合，不推荐
  - **推荐**：b2（MQTT 桥已通，零 mat-device-control 改动；ATEStudio edge 加 MQTT 客户端即可）
- **测试**：
  - `tests/test_mat_device_adapter.py`: stub DeviceController，验证接口对齐
  - `tests/integration/test_mat_device_mqtt.py`: 用 mat-device-control 的 mock 设备验证 MQTT 协议

#### A.3 链路层观测补齐（2 周）
**新建 `services/exec_observer/`**（挂在 MATRag — 与 RAG 无关；建议挂在 ATEStudio cloud 内，因为数据归属）：
- `events.py`:
  - 监听 NATS `ate.executions.{exec_id}.events` subject
  - 转发到 MATRag RAG 入库（**仅当评审同意**；默认接受"事件不回 MATRag，由 ATEStudio 落 DB"）
  - 触发 MATRag 知识回链（Phase C.1）
- `audit_report.py`:
  - 给定 execution_id，输出"用了哪些 req / 哪些条件 / 哪些 failed"
  - 用于 dev_agent / debug_agent 检索上下文
- **测试**：mock NATS + mock DB

#### A.4 端到端 demo（1~2 周）
- **目标**：PA601-D54A 真机走 1 个 requirement（输出电压 54V 范围 53.5~54.5V）+ 1 个 condition → 设备读数 → DB 落档 → SSE 前端可见
- **执行路径**：
  1. MATRag `scripts/extract_bundle.py --product PA601-D54A --out /tmp/p2.json`
  2. ATEStudio `POST /api/v1/imports/aterag?dry_run=true` → 评审
  3. ATEStudio `POST /api/v1/imports/aterag?dry_run=false&confirm_overwrite=true` → 落库
  4. ATEStudio `POST /api/v1/imports/aterag/plan` → plan_yaml + 0 gaps
  5. ATEStudio `POST /api/v1/scripts` body=plan_yaml → script_id
  6. ATEStudio `POST /api/v1/executions` body=script_id+uut_sn → execution_id
  7. ATEStudio cloud `execution_dispatch.py` publish → NATS `ate.tasks.{exec_id}`
  8. ATEStudio edge `scheduler/jetstream_worker.py` 消费 → `step_executor.py` → 设备读数
  9. ATEStudio edge 回写 `test_conditions.measured_value` + `pass/fail`
  10. SSE 推送 `condition.measured` 事件 → 前端可见
- **闸门**：
  - bundle contract_hash 三向一致
  - plan_yaml 经 `YamlParser.parse()` 校验
  - plan_gaps 为空
  - 设备读数进入 DB 后 SSE 推送

### Phase B: Agent 服务层（4~6 周，与 Phase A 并行）

#### B.1 MATRag Agent 服务骨架（P3-1，2 周）
- **新建 `src/matrag/agent/`**：
  - `service.py`（FastAPI，端口 8322）:
    - `POST /agent/dev` — 产测软件开发入口
    - `POST /agent/debug` — 调测诊断入口
    - `POST /agent/review` — bundle 评审入口
    - `GET /agent/{job_id}/events` — SSE 流式返回
  - `clients/gateway.py` — 调 `gateway.ask` MCP（已有 6 工具）
  - `clients/atestudio.py` — 调 ATEStudio `POST /imports/aterag/plan`（HTTP）
  - `clients/nats.py` — 订阅 `ate.executions.*`（仅当 Phase A.3 同意事件回 MATRag）
  - `tools/`:
    - `tools/read_bundle.py`
    - `tools/plan.py`：HTTP 调 ATEStudio `/aterag/plan`
    - `tools/propose_test.py`：Monty 沙箱跑测试条件（复用 P3-0a）
    - `tools/derive.py`：复用 P3-0d 的 derive
    - `tools/query_exec.py`（**若 Phase A.3 同意**）：从 NATS 拉历史 execution
- **Skills 面**（Pydantic AI）：
  - `skills/bundle-review.skill.md`
  - `skills/test-dev.skill.md`
  - `skills/debug-cycle.skill.md`
- **Pydantic AI 配置**：
  - `model = "qwen3.7-flash"`（沿用项目 id）
  - `enable_thinking=False`
  - `tools=[...]` 显式列表
- **测试**：`tests/test_agent_service.py` + `tests/test_agent_tools.py` + `tests/test_agent_skills.py`

#### B.2 dev_agent — 产测软件开发（P3-2，2 周）
- 流程：自然语言 → RAG 检索 → LLM 提案 bundle（draft 状态） → 调 ATEStudio `/aterag/plan` → 解读 plan_gaps
- 不触碰：代签（人工 review 升级 draft）；写 DB（仅产出方案）
- 与 `agent/hardware-test-agent/`（DeerFlow）的关系：**互不替代**。DeerFlow 生成 pytest 脚本（开发期）；dev_agent 生成 bundle+plan（执行期）。可后续在 BundleModel 加 `test_scripts_ref` 字段做对接。

#### B.3 debug_agent — 调测诊断（P3-3，2 周）
- 流程：execution_id + 失败 step_id + 设备读数 + plan_yaml → RAG 检索故障模式 → LLM 综合 Top-N 根因 + 建议
- 不触碰：改 plan（仅建议）；改 DB（仅落 FMEA）
- 数据来源：ATEStudio `failure_indexer` + `diagnosis_service`（已存在）

### Phase C: 知识层与增量导入（持续）

#### C.1 跨仓知识回链
- 现状：MATRag KG 1222/4062；ATEStudio `failure_indexer` 落档 FMEA
- 建议：周期性把 `failure_indexer` 高频故障模式 → 通过 MATRag MCP `add_knowledge` 入公共知识（**需新增工具**）
- 公共知识增删必须走人类审核

#### C.2 增量导入
- `bundle_diff.py`（MATRag 侧）：
  - 旧 bundle vs 新 bundle → 增/改/迁 diff JSON
- ATEStudio 增量端点：`POST /api/v1/imports/aterag/incremental`
- 价值：规格书修订 ~95% 导入时间节省

### Phase D: 部署硬化（持续，与 P1-8 / P4-3 并行）

#### D.1 ATEStudio `__init__.py` 解耦
- `ate_cloud/__init__.py` 改只导出公开类型
- `create_app()` 工厂：仅调用时连 NATS
- `verify_bundle_ate.py` 移除 shim（一次性债务消除）
- 测试：`tests/test_app_factory.py`

#### D.2 PG prod profile（P1-8）
- MATRag：sqlite-vec dev + pgvector prod
- ATEStudio：已有 SQLite/PG 双适配
- ATETest：sqlite（保持；非主路径）
- 迁移演练：`scripts/migrate_store.py`（dry_run → 真实）

#### D.3 Explorer prod（P4-3）
- `explorer_serve.py` prod profile：独立进程 + 反向代理 + X-API-Key + 监控
- `ops_doctor.py` 已就位；加 backup/restore
- 文档 `deploy/explorer-prod.md`

---

## 6. 风险与权衡（v2 校准）

### 6.1 主要风险

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| 词表漂移（kind/method）未捕获 | 高 | 漏测类目 | Phase A.1 自动化契约同步门 |
| Bundle 字段升级时三侧 PR 漏一 | 中 | 静默丢字段 | contract_hash 三向一致 + CI gate |
| 真机设备池策略选错（α/β/γ） | 中 | 投入浪费 | Phase A.2 先做 β 探针 |
| mat-device-control 无设备 REST | 高 | β 路线需改 | b2（MQTT 桥）零改动 |
| Agent 代签红线被破坏 | 中 | 产线错判 | 沿用 ALLOWED + DENIED 双轨；CI 测试断言 |
| 端到端真机回路未跑通就 ship | 高 | 上线即崩 | Phase A.4 demo 闸门 |
| MATRag/ATEStudio 启动耦合债延续 | 中 | 跨仓协作摩擦 | Phase D.1 解耦 |
| 公共契约文件被 Agent 改动 | 低 | 跨仓大震荡 | 公共契约一律由主会话改；副任务回报 |
| MATRag agent 与 hardware-test-agent 边界模糊 | 低 | 重复实现 | 明确 MATRag agent 产出 bundle/plan；hardware-test-agent 产出 pytest 脚本；可后续对接 |
| NATS 事件回 MATRag 的权限边界 | 中 | 越权写 | 默认拒绝；评审单独决策 |

### 6.2 战略冻结边界

- **ATEStudio `ate_cloud/` + `ate_platform/` 全部不动**（35+ schemas / 40 services / 35+ routers / 28 表）
- **ATETest `backend/` 不动**（PageIndex RAG 与产测无关）
- **ATETest `agent/hardware-test-agent/` 不动**（独立工作流）
- **ATETest `mat-device-control/` 不动**（除非选项 β 决策需加 MQTT 客户端）
- **MATRag 已 done 部分不动**（P0/P1/P2/P3-0）
- **新增仅在**：tools/contract_sync/、src/matrag/agent/、ATEStudio `tests/`（链路层测试）

### 6.3 不在本次方案范围

- ATEStudio 大型重构（服务拆分 / 数据面重构）
- ATETest 三项目合并
- 多厂商仪器适配（仅就现有 SCPI/Modbus）
- 实时诊断 AI（故障 100ms 内在线判读）
- 安全/合规审计

---

## 7. 验收标准（Phase A 端到端 demo）

### 7.1 通过条件

- [ ] 词表同步门：CI 红蓝绿三向一致
- [ ] bundle → plan_yaml：dry-run 0 gap（PA601 76 reqs 全部有判据）
- [ ] plan → script → execution：DB 落档 + SSE 推送
- [ ] 设备读数 → DB → SSE：单步验证
- [ ] pytest 1326 → ≥1400（新增 80+ bridge/contract tests）

### 7.2 demo 演示路径（PA601-D54A 输出电压）

1. `python -m scripts.extract_bundle --product PA601-D54A` → `/tmp/p2.json`
2. `curl -X POST /api/v1/imports/aterag?dry_run=true` body=p2.json → preview
3. `curl -X POST /api/v1/imports/aterag?dry_run=false&confirm_overwrite=true` body=p2.json → 落库
4. `curl -X POST /api/v1/imports/aterag/plan` body=p2.json → plan_yaml + 0 gaps
5. `curl -X POST /api/v1/scripts` body=plan_yaml → script_id
6. `curl -X POST /api/v1/executions` body=script_id+uut_sn → execution_id + SSE 流
7. ATEStudio edge 消费 NATS → step_executor → 真机/仿真设备读 54.0V
8. DB `TestCondition.measured_value=54.0` → SSE `condition.measured`
9. SSE → 前端 visible

---

## 8. 时间表（评审估算）

| 阶段 | 周 | 关键产出 | 闸门 |
|---|---|---|---|
| **A.0** 决策点 | 0 | 真机设备池策略决议（α/β/γ） | 评审签字 |
| **A.1** 契约同步门 | 1 | tools/contract_sync/ + CI | 词表三向一致 |
| **A.2** 真机设备池适配（β） | 3 | services/mat_device_adapter/ + 至少 1 仪器端到端 | mat_device MQTT 协议单元测试 |
| **A.3** 链路层观测 | 2 | services/exec_observer/ | NATS 订阅 mock 测试 |
| **A.4** 端到端 demo | 1~2 | PA601 单步真机回路 | demo 录像 + pytest |
| **B.1** Agent 服务骨架 | 2 | src/matrag/agent/ + Pydantic AI | service+tools+skills 测试全绿 |
| **B.2** dev_agent | 2 | 自然语言 → bundle+plan 提案 | LLM off stub 测试；live 用例 skipif |
| **B.3** debug_agent | 2 | 执行失败 → 诊断报告 | 失败样本 10 个，命中率 ≥60% |
| **C.1** 跨仓知识回链 | 持续 | services/knowledge_back_link.py | 知识增删审计 |
| **C.2** 增量导入 | 持续 | bundle_diff.py + /imports/aterag/incremental | diff 准确率 100% |
| **D.1** ATEStudio 解耦 | 1 | app_factory + 移除 shim | verify_bundle_ate.py 不再 shim |
| **D.2** PG prod | 持续 | prod profile + 迁移演练 | 压测通过 |
| **D.3** Explorer prod | 持续 | deploy/explorer-prod.md + 监控 | 7×24 监控 |

合计：**8~12 周**完成 Phase A + B（与 Phase A.0 决策并行）；Phase C/D 与 backlog 并行。

---

## 9. 评审需要决策的点（v2 校准）

1. **真机设备池策略**（α/β/γ）— 决定 Phase A.2 路线
2. **NATS 事件是否回 MATRag**（exec_observer）— 默认否；如需是，权限边界？
3. **Agent 服务端口/部署**（MATRag 8321 + Agent 8322 同仓同域 vs 独立部署）
4. **知识回链权限**（failure_indexer 自动写入 vs 仅建议）
5. **bundle 升级纪律**（minor +1 必须两侧 PR 联动 — 由谁维护 changelog？）
6. **公共契约改动流程**（当前由主会话改；Agent 上线后是否引入"契约变更 CR"流程？）

---

## 10. 附录：项目纪律摘要（不可绕过）

来自 `AGENTS.md` / `TODO.md`：
- Python 3.13，`src/matrag/` 布局；导入一律 `from matrag.xxx import yyy`
- **公共契约文件非本人任务不得修改**：`src/matrag/types.py`、`config.py`、`llm.py`、`pyproject.toml`、`TODO.md`
- 测试在 `tests/`，与被测模块同名镜像
- 依赖已预装，禁止 `uv run`/`uv sync`/`uv add`；命令统一用 venv 直调
- 风格：docstring/注释中文；类型标注完整；异常用 `matrag.types` 里定义的或标准库；失败 fail-fast
- 收尾必做：pytest 全绿 + ruff 无 E/F

**本方案严格遵守**：所有新增模块在 `src/matrag/agent/`、`tools/contract_sync/` 等新目录；不触碰公共契约；新模块必须含中文 docstring + 完整类型 + fail-fast。

---

## 11. 评审 checklist

- [ ] Phase A.0 真机设备池策略决议
- [ ] Phase A.1~A.4 优先级与时间表
- [ ] Phase B.1~B.3 Agent 服务层与 P3-1/2/3 对应
- [ ] NATS 事件回 MATRag 决策
- [ ] 知识回链权限边界
- [ ] bundle 升级纪律责任主体
- [ ] 公共契约改动流程
- [ ] 风险清单是否完备
- [ ] 演示路径是否可接受作为 Phase A 通过项
- [ ] 与 hardware-test-agent（DeerFlow）的边界与对接时机