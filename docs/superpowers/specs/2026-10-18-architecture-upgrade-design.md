# 架构升级方案设计 Spec（2026-10-18）

> **方案 B（渐进重构）** — 不重写现有代码，技术栈收敛，工程化优先。
> **作者**：主会话 + 评审
> **状态**：已评审通过 v2（见 `architecture-review-v2-2026-10-18.md`；v2 已修复 v1 F2/F3/F4/F5/F6 + R13 + R14 七项硬阻断）

## 1. 背景与目标

### 1.1 现状

**MATRag**：P0 解析、P1 RAG 核心、P1 知识层、P2 抽取投影、P3-0 计算路径 全部 done（27/34）；7 项 backlog 按战略冻结属后续工作。

**ATEStudio**：自包含的产测平台。`ate_cloud/`（云：~50 services / ~40 routers / 28 表 / FastAPI + NATS + JWT + langchain-mcp）+ `ate_platform/`（边缘：scheduler + executor + step_executor + drivers + simulation + recorder + proxy）+ `shared/`（跨端：dsl.py YamlPlan / events / types / measurement）。云↔边通信用 NATS JetStream `ate.tasks.{exec_id}` subject。

**ATETest**：3 个独立子项目，零 MATRag/ATEStudio 引用：
- `backend/` = PageIndex RAG（与产测无关，保留）
- `mat-device-control/` = 设备控制独立仓（FastAPI + MQTT 桥；**无设备操作 REST**；SCPI TCPIP 真实现 + USB/GPIB/Modbus 占位）
- `agent/hardware-test-agent/` = DeerFlow 2.0 多 Agent（**已决策下线**；功能合并到 matrag-agent/，见 §4.3）

### 1.2 真实问题

1. **真机链路未跑通**：ATEStudio edge 自闭环（simulation 可跑），但真机设备池接谁未定
2. **Agent 服务层缺位**：MATRag P3-1/2/3 backlog；ATEStudio 仅有只读 agent
3. **ATEStudio 启动耦合债**：`ate_cloud/__init__.py` 顶 `from .main import app` → `import nats` → `lifespan` 强制 NATS 连通 → 任何 `import ate_cloud` 触发 NATS 连接
4. **跨仓词表同步缺自动化门**：kind/method 25+32 条需两侧同步；现有 `verify_bindings.py` 单侧
5. **ATEStudio LLM 框架分裂**：langchain-mcp-adapters 与 MATRag Pydantic AI 框架不一致
6. **增量导入缺位**：spec 修订全量重导，未做 diff
7. **跨仓知识回链缺位**：failure_indexer 落档 → MATRag 公共知识的人工路径

### 1.3 目标

- **Phase A**（4~6 周）：bundle → 真机执行 → 结果回传全链路 demo（PA601-D54A 单步）
- **Phase B**（6 周）：MATRag Agent 服务层（P3-1/2/3）
- **Phase C/D**（持续）：跨仓知识回链 + 增量导入 + 部署硬化
- **总工期**：8~12 周完成 Phase A+B；Phase C/D 并行

### 1.4 非目标

- ATEStudio 大型重构
- ATETest 三项目合并
- 多厂商仪器适配
- 实时诊断 AI（<100ms 在线判读）
- 安全/合规审计

### 1.5 架构根本原则：**MATRag 是规格书的 single source of truth**

所有规格书原文、解析后表格、抽取的测试条件、公共知识（术语/公理/方法/规则）**只在 MATRag 仓内落地**：
- `data/corpus/*.doc.json`（blocks 段落结构）
- `data/corpus/*.tables.json`（解析后的参数表）
- `data/domain/source/`（YAML 公共知识源）
- `data/index/`（chunk + 向量索引）

下游各仓的接入规则：
- **ATEStudio**：通过 `bundle.json` 接收 MATRag 的抽取投影（contract_hash 守约）；不再独立解析规格书
- **matrag-agent/**：通过 MCP-HTTP 调 MATRag gateway 检索；不重新解析规格书
- **ATEStudio `aterag_agent.py`**：通过 MATRag MCP 读公共知识；不重新解析规格书
- **mat-device-control**：纯设备驱动层；不接触规格书

**反例**（已纠正或待下线）：
- HT-A `tools/doc_parser.py` 试图二次解析 PDF/Word —— **重复造轮子**；决策 X4 下线
- 任何下游仓新建 `parse_spec` / `parse_pdf` 模块 —— **禁止**；规格书解析一律走 MATRag P0

---

## 2. 决策矩阵（评审通过）

| # | 决策项 | 选择 | 理由 |
|---|---|---|---|
| D1 | 真机设备池策略 | **γ 双轨**：仿真 edge drivers + 真机 mat-device-control | 取长补短；仿真可离线测试，真机可生产验证 |
| D2 | NATS 事件回 MATRag | **否** | ATEStudio 落 DB 即可；不让事件回流 MATRag |
| D3 | Agent 服务部署 | **独立仓库** `matrag-agent/` | 独立版本 / CI / 扩缩容；bundle 契约稳定后可行 |
| D4 | bundle 升级 changelog | **MATRag 主会话**（producer 主） | 与既有 "公共契约由主会话改" 纪律一致 |
| D5 | 公共契约改动 | **维持主会话单点** | 现状纪律；扩 Agent 后再评估 |
| D6 | 整体方案 | **B（渐进重构）** | 技术栈收敛 + 工期可控 |
| D7 | Agent IPC | **HTTP + SSE**；Redis Streams 延后 | YAGNI；零新依赖；HTTP 调试友好 |
| D8 | ATEStudio langchain→Pydantic AI | **做** | 与 MATRag 一致；Pydantic AI 已有验证（9.0A/1198.8Wh） |
| D9 | OpenTelemetry 统一观测 | **延迟** | 优先核心工程化；OTel 待后续评估 |
| D10 | 规格书 single source of truth | **MATRag**（`data/corpus/`） | 所有解析产物只在 MATRag；下游仓不重新解析 |
| D11 | hardware-test-agent 处置 | **X4 功能合并**：HT-A 下线，能力（Writer/Coder）迁入 matrag-agent/（Phase B.5） | Parser 已由 MATRag P0 覆盖；Deployer 属 ATEStudio；Debugger 属 debug_agent |
| D12 | langchain→Pydantic AI 范围 | **D8a**：仅 `aterag_agent.py`（agent 编排框架） | 评审 F1 修复；其余 5 文件 langchain_openai 迁移单独立 backlog |
| D13 | mat-device-control 鉴权 | **R16 延后**；当前仅内网 + method 白名单 | R16 不阻塞 A.2；后续评估（MAG 风险登记） |
| D14 | matrag-agent → ATEStudio 鉴权 | **service-account + 长期 JWT**（**Phase 0 必做**） | 评审 R13 修复；B.3 前置 |
| D15 | 真机适配位置 | **edge driver 层**（`MatDeviceHttpDriver`） | 评审 A1 修复；与 cloud 双轨桥方案作废 |
| D16 | B.3 dev_agent 语义 | **pending 与 gap 分离**：draft → pending 走签字；gap → 补 binding 表 | 评审 R14 修复；dev_agent 报告分别处理 |

---

## 3. 技术选型（最优选择）

| 层 | 现状 | 推荐 | 评估 |
|---|---|---|---|
| ATEStudio 消息总线 | NATS JetStream | **保留 NATS** | 已落地；迁移成本高；暂无替代驱动价值 |
| ATEStudio LLM 框架 | langchain-mcp-adapters | **替换 Pydantic AI** | 与 MATRag 一致；原生 tool calling 更可靠 |
| ATEStudio HTTP | FastAPI | **保留 FastAPI** | 生态成熟 |
| ATEStudio ORM | SQLAlchemy 2.0 async + alembic | **保留** | 跨 DB 适配合理 |
| ATEStudio edge drivers | pymeasure + 自研 + grpc | **保留 + 增量** | pymeasure 覆盖有限；按需补 driver |
| MATRag Agent LLM | (P3-0 Pydantic AI) | **沿用** | 已验证 |
| Agent IPC | (新) | **HTTP + SSE** | 见 D7 |
| Bundle schema | Pydantic | **保留** | 已双侧独立声明 + contract_hash；Protobuf 迁移收益边际递减 |
| 三仓构建 | uv + pyproject.toml | **保留 uv** | 已统一 |
| Python 版本 | 3.12 / 3.13 / **3.14 错配** | **统一 3.13** | mat-device-control `>=3.14` 是错配，需降级 |
| 跨仓观测 | 各自 NATS 日志 | **延迟**（D9） | OTel 后续评估 |

---

## 4. 模块责任边界

### 4.1 MATRag（含 Agent 独立仓库）

```
matrag/                       (生产侧 + 知识 + 计算)
├── matrag/                   (主仓，F:\Workspace\MATRag)
│   ├── extract/              ← P2 done：规格书 → ATEStudio bundle v1.0
│   ├── llm.py / embeddings/  ← LLM/Embedding 门面 (P1-1)
│   ├── domain/               ← 推理/反解 (P3-0d: Monty + SymPy)
│   ├── index/                ← sqlite-vec + FTS5 + KG (P1-4)
│   ├── retrieve/             ← 三腿 RRF (P1-5)
│   ├── answer/               ← 应答合成 + provenance (P1-5b)
│   └── gateway/              ← MCP-HTTP + REST (P1-6)
│
└── matrag-agent/             (新仓库 — P3-1/2/3 + 吸收 HT-A 功能)
    ├── src/matrag_agent/
    │   ├── service.py        ← FastAPI: /agent/dev|debug|review|scripts + SSE
    │   ├── clients/
    │   │   ├── gateway.py    ← HTTP → matrag.gateway (RAG)
    │   │   ├── atestudio.py  ← HTTP → ATEStudio /aterag/plan
    │   │   └── llm.py        ← Pydantic AI
    │   ├── tools/
    │   │   ├── read_bundle.py
    │   │   ├── plan.py
    │   │   ├── propose_test.py  ← 复用 P3-0a Monty
    │   │   ├── derive.py        ← 复用 P3-0d
    │   │   ├── query_exec.py
    │   │   └── gen_script.py    ← 🆕 Phase B.5 (吸收 HT-A Coder Agent)
    │   ├── skills/
    │   │   ├── bundle-review.skill.md
    │   │   ├── test-dev.skill.md
    │   │   ├── debug-cycle.skill.md
    │   │   ├── prd-gen.skill.md     ← 🆕 Phase B.5 (吸收 HT-A Writer Agent)
    │   │   └── dev-scripts.skill.md ← 🆕 Phase B.5 (吸收 HT-A Coder Agent)
    │   └── agents/
    │       ├── dev_agent.py       ← 自然语言 → bundle draft (P3-2)
    │       ├── debug_agent.py     ← 失败 → 诊断 (P3-3)
    │       ├── review_agent.py    ← bundle 评审 (P3-1)
    │       └── dev_scripts_agent.py ← 🆕 Phase B.5 (吸收 HT-A)
    ├── tests/                ← offline stub + skipif live
    └── pyproject.toml        ← Python 3.13；独立版本
```

**MATRag 主仓责任**：
- **规格书 single source of truth**：`data/corpus/*.doc.json` + `*.tables.json`（P0 解析产物）
- 公共知识源：`data/domain/source/`（术语/公理/方法/规则）
- RAG 索引与检索
- 抽取投影（bundle 导出）
- 离线推理/反解
- Gateway HTTP/MCP
- **不动**：已 done 的 P0/P1/P2/P3-0 代码

**matrag-agent 责任**：
- **数据消费方**：从 MATRag 主仓 RAG + index + `data/corpus` 读取，**不重新解析规格书**
- dev_agent：自然语言 → bundle draft + plan 评审（P3-2）
- debug_agent：execution failure → 根因 + 修复建议（P3-3）
- review_agent：bundle 评审 + plan_gaps 解释
- **dev_scripts_agent**（Phase B.5 吸收 HT-A）：开发期 pytest 脚本生成（输入：MATRag 已解析的规格书 + 方法库 + 绑定表）
- 走 HTTP + SSE；不直连 NATS / DB
- ALLOWED_TOOLS 白名单 + DENIED 黑名单双轨（沿用 ATEStudio `aterag_agent.py` 模式）
- 不代签；不写 DB

### 4.2 ATEStudio

```
ATEStudio/
├── src/ate_cloud/            (云侧 — 不动 + 局部改造)
│   ├── schemas/aterag_bundle.py  ← 契约镜像 (不变)
│   ├── services/flow_planner.py  ← bundle → FlowPlan (不变)
│   ├── services/aterag_importer.py  ← bundle → DB 幂等 (不变)
│   ├── services/aterag_script_emit.py  ← plan → 脚本 (不变)
│   ├── services/aterag_agent.py  ← 🔧 langchain → Pydantic AI (Phase B)
│   ├── services/aterag_catalog.py  ← MCP list_models (不变)
│   ├── services/execution_dispatch.py  ← NATS publish (不变)
│   ├── services/plan_materializer.py  ← Execution → YamlPlan (不变)
│   ├── services/aterag_script_emit.py  ← plan → 脚本 (不变)
│   │   ├── bridge.py  ← 真机设备池适配（双轨）
│   │   ├── sim.py    ← 仿真端 edge drivers 适配
│   │   └── mat_device_adapter.py  ← 真机端 mat-device-control MQTT 适配
│   ├── api/v1/imports.py  ← POST /imports/aterag/plan | /imports/aterag (不变)
│   ├── api/v1/executions.py  ← (不变)
│   └── main.py  ← 🔧 create_app 工厂解耦 (Phase D.1)
│
├── src/ate_platform/         (边缘执行 — 不动)
│   ├── executor/step_executor.py  ← Python 模板 + result_* 协议
│   ├── drivers/{base,base_hal,base_mal,capabilities,tcp_instrument}.py  ← 仿真端 driver (不变)
│   ├── scheduler/{jetstream_worker,station_orchestrator,...}.py  ← NATS 消费 (不变)
│   ├── simulation/{headless_runner,full_chain_simulator,fault_injector,...}.py  ← (不变)
│   ├── recorder/             ← (不变)
│   └── proxy/proxy_manager.py  ← (不变)
│
├── src/shared/               (跨端契约 — 不动)
│   └── dsl.py                ← YamlPlan / YamlStep / ExecutionMode / LoopType
│
└── config/test_method_bindings.yaml  ← 25 (kind,side) → action/settle/... (不变)
```

**ATEStudio 云侧责任**：
- bundle 契约守门（contract_hash）
- bundle → DB 幂等导入
- bundle → FlowPlan（plan_flow）
- FlowPlan → YamlPlan + 脚本（script_emit）
- Execution 调度（NATS publish）
- HTTP API + RBAC + JWT

**ATEStudio edge 责任**：
- 消费 NATS `ate.tasks.*`
- step_executor 执行 step
- drivers 调设备
- simulation 离线演练
- recorder 录制 + 回放

**Phase B 改造**（D8a — 仅替换 agent 编排框架）：
- `aterag_agent.py`：langchain-mcp-adapters → Pydantic AI MCP adapter
- ALLOWED_TOOLS / DENIED 机制不变
- 与 matrag-agent/ 同框架
- **不在 Phase B 范围**（新增 backlog 项）：`script_generator.py` / `diagnosis_service.py` / `traceback_analyzer.py` / `query_rewrite.py` / `embedding_service.py` 共 5 处的 langchain_openai 依赖，待 P3-1/2/3 完成后统一迁移到轻量 LLM 门面（不在本次 spec 范围）

**Phase D.1 改造**（ate_cloud/main.py）：
- `create_app()` 工厂模式
- `ate_cloud/__init__.py` 不再 `from .main import app`
- 任何 `from ate_cloud.schemas import BundleModel` 不触发 NATS

### 4.3 ATETest（mat-device-control 唯一相关）

```
ATETest/
├── backend/                  ← PageIndex RAG (与产测无关，保留)
├── agent/hardware-test-agent/  ← ❌ 下线 (功能合并到 matrag-agent/)
└── mat-device-control/       ← 🔧 加 HTTP/MQTT 适配层 (Phase A.2)
    ├── main.py               ← FastAPI lifespan 装载设备
    ├── modules/device/
    │   ├── controller.py     ← DeviceController 反射
    │   ├── base.py           ← BaseDriver 抽象
    │   ├── protocols/scpi.py ← SCPI TCPIP (真实现)
    │   ├── protocols/modbus.py  ← (placeholder)
    │   └── families/scpi_devices.py
    ├── modules/mqtt/         ← MQTT 桥 (复用)
    ├── modules/api/          ← 🔧 新增 device_ops API
    │   ├── routes/devices.py  ← POST /devices/{id}/call
    │   └── routes/health.py
    ├── tests/                ← 已有 (20 文件)
    ├── devices.toml          ← 设备池配置
    └── pyproject.toml        ← Python 3.13 (降级 from >=3.14)
```

**硬件测试 Agent 处置**（决策 X4：功能合并到 matrag-agent/）：
- 下线 `ATETest/agent/hardware-test-agent/` 目录
- **HT-A 的 `parse_spec` 步骤完全冗余——MATRag P0 已是规格书 single source of truth**
  - 规格书原文：`data/corpus/*.doc.json`（blocks）+ `*.tables.json`（解析后表格）
  - 公共知识：`data/domain/source/`（术语/公理/方法/规则）
  - HT-A 的 `tools/doc_parser.py` 试图二次解析 PDF/Word = 重复造轮子
- 功能映射到 `matrag-agent/`：
  | HT-A agent | 合并到 matrag-agent/ | 数据来源 |
  |---|---|---|
  | Parser Agent (文档分析) | **已在 MATRag P0** (`matrag/parse/`) — 无需合并 | `data/corpus/*.doc.json` |
  | Writer Agent (PRD 生成) | 新增 skill `prd-gen.skill.md`（Phase B.5） | MATRag RAG (`/ask`) + `data/domain/` |
  | Coder Agent (代码生成) | 新增 `dev_scripts_agent`（Phase B.5） | MATRag RAG + `power_test_methods.yaml` + `test_method_bindings.yaml` |
  | Deployer Agent (部署执行) | ATEStudio 职责（`execution_dispatch`）— 不动 | — |
  | Debugger Agent (错误修复) | 已在 matrag-agent/ 规划（debug_agent）— 不动 | ATEStudio failure_indexer |
- 归档 README + 设计文档到 `MATRag/docs/legacy/hardware-test-agent.md`（保留 DeerFlow 多 agent 编排经验）
- 释放 Python 3.12 venv + 4 个 LLM backend 适配的维护负担

**mat-device-control 责任**：
- 设备 TCPIP SCPI 真机控制
- MQTT 桥自动映射 API 路由
- 设备生命周期管理
- 仿真 / mock（测试用）

**Phase A.2 改造**（在 `modules/api/` 内新增）：
- 不修改现有 MQTT 桥
- 新增 `POST /devices/{device_id}/call` HTTP 端点
- 调 `DeviceController.call()` 反射
- 让 ATEStudio edge 通过 HTTP 或 MQTT 调用

### 4.4 跨仓依赖矩阵

| 调用方 \ 被调方 | MATRag gateway | matrag-agent/ | ATEStudio cloud | ATEStudio edge | mat-device-control |
|---|---|---|---|---|---|
| MATRag gateway | — | 不允许 | 不允许 | 不允许 | 不允许 |
| matrag-agent/ | HTTP MCP（**Phase 0 落地 service-account + 长期 JWT**） | — | HTTP `/aterag/plan`, `/imports/aterag`（同上鉴权） | 不允许 | 不允许（脚本运行由 ATEStudio 接管） |
| ATEStudio cloud | HTTP MCP（只读） | 不允许 | — | NATS `ate.tasks.*` | 不允许 |
| ATEStudio edge | 不允许 | 不允许 | NATS `ate.status.*` | — | HTTP `POST /devices/{id}/call`（**D14 edge driver 层**，MatDeviceHttpDriver 调用） |
| mat-device-control | 不允许 | 不允许 | 不允许 | HTTP 回调（结果回写） | — |

**HT-A 已下线**：原 `ATETest/agent/hardware-test-agent/` 目录删除；其功能（Parser/Writer/Coder/Debugger）合并入 matrag-agent/（P0 已含 Parser；Phase B.5 新增 Writer/Coder 能力；Phase B.3 已含 Debugger）。

**规格书 single source of truth 规则**（强化）：
- 规格书原文与解析产物**只在 MATRag 主仓 `data/corpus/` 落地**
- ATEStudio / matrag-agent/ / mat-device-control **不允许独立解析规格书**
- HT-A 的 `tools/doc_parser.py` 是反例；下线后该路径彻底关闭

**鉴权规则**（强化，R13 修复）：
- ATEStudio HTTP 端点全部需要 JWT（scope `aterag:import` 等）
- 跨仓调用方必须持长期 JWT 或 API key；MATRag gateway**不出具 ATEStudio token**（避免信任链路绕环）
- **Phase 0 必做**：ATEStudio 新增 service-account 机制（admin 用户 + 长期 access token），供 matrag-agent 使用
- ATEStudio `__init__.py` 解耦（Phase D.1）让 `import ate_cloud.schemas` 不触发 NATS；service-account 凭据通过环境变量注入

**规则**：
- 同仓内：直接 import
- 跨仓：仅 HTTP / 文件 / NATS（限 ATEStudio 云↔边）
- 跨仓 HTTP 必须有鉴权（JWT 或 API key）
- bundle 契约 = 跨仓唯一信任根

---

## 5. 实施方案（分阶段）

### Phase A：链路层补齐 + 真机 demo（5~7 周）

#### A.1 跨仓契约同步门（1 周）

**新建**（MATRag 主仓）：
```
tools/contract_sync/
├── __init__.py
├── check_vocab.py     # MATRag KIND_UNITS vs ATEStudio test_method_bindings.yaml
├── check_contract_hash.py  # 两侧 hash diff
└── README.md
tests/test_contract_sync.py  # 离线三产品 mock
```

**验收**：
- CI gate：MATRag CI + ATEStudio CI 各跑一次
- 三向一致：25 (kind,side) ⊆ MATRag + ATEStudio；method_ref ⊆ power_test_methods.yaml；action ⊆ DRIVER_TABLE

#### A.2 真机设备池适配 — edge driver 层（3 周）

**核心设计调整**（评审 A1 修复）：真机适配放在 **edge driver 层**，不是 cloud 侧桥。
原方案在 cloud 侧建桥的做法与实际执行架构错位（cloud dispatch → NATS → edge step_executor → _driver()）。
修正后的双轨=driver 层二选一。

**新建**（ATEStudio edge `ate_platform/drivers/mat_device/`）：
```
ate_platform/drivers/mat_device/
├── __init__.py
├── http_driver.py         # MatDeviceHttpDriver（实现 BaseDriver 接口）
├── mapping.py             # 设备名映射表（psu001 ↔ PSU_1 等）
└── protocol.py            # 与 mat-device-control HTTP 协议封装
```

**关键设计**：
- `MatDeviceHttpDriver` 继承 `ate_platform/drivers/base.py:BaseDriver`，实现 `_connect_impl/_write_impl/_query_impl/_read_impl`
- 内部调 mat-device-control `POST /devices/{id}/call` 反射式接口
- 设备名映射表（data-driven，配置在 `mapping.py`）：
  - `psu001` ↔ `PSU_1`
  - `scope001` ↔ `SCOPE_1`
  - `eload001` ↔ `ELOAD_1`
  - `dmm001` ↔ `DMM_1`
- 双轨切换：`ate_platform/drivers/registry.py` 注册两个实现，运行时由 fixture 配置选择
- 仿真轨：现有 `ate_platform/drivers/examples/{psu,dmm,chroma_eload}.py`（不变）

**新建**（mat-device-control `modules/api/`）：
```
modules/api/routes/
├── devices.py        # POST /devices/{device_id}/call（反射式）
├── config.py         # (已有)
└── health.py         # (已有)
```

**`devices.py` 设计要点**：
- `POST /devices/{device_id}/call` body = `{"method": "...", "kwargs": {...}}`
- 反射调用 `DeviceController.call(device_id, method, **kwargs)`
- **method 白名单**（防止 `__init__` 等敏感方法）：从 `devices.toml` 读取或硬编码基础白名单
- **不引入鉴权**（决策 D13：R16 延后；当前仅内网暴露 + method 白名单兜底）

**验收**：
- `ate_platform/tests/drivers/test_mat_device_http_driver.py`：stub HTTP，验证 BaseDriver 接口实现完整
- `mat-device-control/tests/test_devices_api.py`：调用反射式端点 + method 白名单
- 端到端：至少 1 仪器（如 Keysight E36312A）端到端：bundle → plan → 真机 → 读数

#### A.3 链路层审计报告（删除独立 observer；接入 failure_indexer，0.5 周）

**评审 F2/F3 修复**：原方案订阅不存在的 `ate.executions.*` + 与 execution_status_relay 重叠。
修正：在 `failure_indexer` 加纯函数 `audit_report(execution_id)`，复用 relay 已落库的 step_results。

**修改**（ATEStudio `services/failure_indexer.py`）：
- 新增 `async def audit_report(db, execution_id) -> dict` 纯函数
- 输出：`{used_requirement_codes: [...], measured_conditions: [...], failed_steps: [...], pending_signoffs: [...]}`
- 不订阅 NATS；只读 DB（避免与 execution_status_relay 的 pull consumer 冲突）

**验收**：
- `tests/test_failure_indexer.py::test_audit_report`：fixture Execution + step_results → 输出结构正确
- 不订阅 NATS；不写 DB

#### A.4 端到端 demo（2~3 周，含签字 + FILL_IN 桩）

**目标**：PA601-D54A 真机走 1 个 requirement（输出电压 54V 范围 53.5~54.5V）+ 1 个 condition → 设备读数 → DB 落档 → SSE 前端可见

**前置条件**（新增）：
- .25 板仪器已物理连接（PSU + DMM at minimum），`devices.toml` 配置就位
- demo bundle 用 **spec-only**（`extract_bundle.py --no-narrative`）保证 76 reqs 全 approved；narrative draft 条件不参与 demo

**执行路径**（与代码契约一致）：
1. MATRag `python -m scripts.extract_bundle --product PA601-D54A --no-narrative --generated-at 2026-10-18T00:00:00Z` → `/tmp/p2.json`
2. ATEStudio `POST /api/v1/imports/aterag?dry_run=true` body=p2.json → preview
3. ATEStudio `POST /api/v1/imports/aterag?dry_run=false&confirm_overwrite=true` body=p2.json → 落库（spec-only bundle 全 approved，无需签字）
4. ATEStudio `POST /api/v1/imports/aterag/plan` body=p2.json → plan_yaml + 0 pending + 0 gaps（_apply_review_state overlay 后 plan_flow 入队）
5. **人工填 FILL_IN 桩**：`fixture_id`（commission 时填写）+ 脚本 `_driver()` 实例化（`MatDeviceHttpDriver("psu001")`）
6. ATEStudio `POST /api/v1/sequences` body=`yaml_content=plan_yaml` → sequence_id
7. ATEStudio `POST /api/v1/executions` body=`{sequence_id, uut_sn}` → execution_id + SSE 流
8. ATEStudio cloud `execution_dispatch.py` publish NATS `ate.tasks.{exec_id}`
9. ATEStudio edge `scheduler/jetstream_worker.py` 消费 → `executor/step_executor.py` → `MatDeviceHttpDriver` → `POST /devices/psu001/call set_voltage` → 读数
10. edge publish `ate.status.{exec_id}` → `execution_status_relay` 消费 → 落 `Execution.step_results` DB + push SSE
11. SSE → 前端 visible

**闸门**：
- bundle contract_hash 三向一致
- plan_yaml 经 `YamlParser.parse()` 校验
- plan.pending == 0；plan_gaps == 0
- FILL_IN 桩已替换（grep 脚本无 `FILL_IN_AT_COMMISSION`）
- 设备读数进入 DB 后 SSE 推送

### Phase B：Agent 服务层（6 周）

#### B.1 MATRag Agent 服务骨架（2 周）

**新仓库** `matrag-agent/`（F:\Workspace\matrag-agent）：
```
matrag-agent/
├── src/matrag_agent/
│   ├── service.py           # FastAPI: /agent/dev|debug|review + SSE
│   ├── clients/
│   │   ├── gateway.py       # HTTP → MATRag gateway MCP
│   │   ├── atestudio.py     # HTTP → ATEStudio /aterag/plan
│   │   └── llm.py           # Pydantic AI
│   ├── tools/
│   │   ├── read_bundle.py
│   │   ├── plan.py
│   │   ├── propose_test.py  # 复用 P3-0a Monty (跨仓 import P3-0a 模块)
│   │   ├── derive.py        # 复用 P3-0d
│   │   └── query_exec.py
│   └── skills/
│       ├── bundle-review.skill.md
│       ├── test-dev.skill.md
│       └── debug-cycle.skill.md
├── tests/
├── pyproject.toml          # Python 3.13；model = "qwen3.7-flash"
└── README.md
```

**关键设计**：
- 端口 8322（独立于 MATRag 8321）
- LLM 模型 `qwen3.7-flash`，`enable_thinking=False`
- ALLOWED_TOOLS 白名单 + DENIED 黑名单
- 不代签；不写 DB
- Skills 工作流定义决策路径

**验收**：
- `tests/test_agent_service.py`: HTTP 端点 + SSE
- `tests/test_agent_tools.py`: stub LLM + stub HTTP
- `tests/test_agent_skills.py`: LLM off stub

#### B.2 ATEStudio langchain → Pydantic AI（与 B.1 并行，1~2 周）

**修改**（ATEStudio `src/ate_cloud/services/aterag_agent.py`）：
- 替换 `from langchain_core.tools.base import BaseTool` → Pydantic AI 工具基类
- 替换 `langchain_mcp_adapters.client.MultiServerMCPClient` → Pydantic AI MCP adapter
- ALLOWED_TOOLS / DENIED 机制不变
- 测试 `tests/unit/test_aterag_agent.py` 保持全绿

**验收**：
- `tests/unit/test_aterag_agent.py` 全绿（pin 工具列表）
- 真实 LLM 实测（live 测试，skipif）

#### B.3 dev_agent — 产测软件开发（2 周，D16 语义修正）

**实现**（matrag-agent `service.py` + `skills/test-dev.skill.md`）：
- 输入：自然语言需求 + product_code + MATRag RAG 上下文
- 流程：
  1. RAG 检索（`/ask`，spec 上下文 + 公共知识）
  2. LLM 提案 bundle JSON（status=draft，数值必见原文）
  3. 调 ATEStudio `/aterag/plan` → 返回 `pending`（未签字 draft 列表）+ `gaps`（无判据条件）+ `warnings`
  4. **D16 修复**：dev_agent 报告**分别处理**两种产出
     - pending 报告：列出哪些 draft 条件需要人工签字 → 走 `POST /api/v1/test_conditions/{id}/approve`（需 ATEStudio 验证该端点存在）
     - gap 报告：列出哪些条件缺 binding → 建议补 `test_method_bindings.yaml`
     - warnings 报告：planner 标注的资源冲突 / 占位限制
- 输出：bundle draft + 分类解读报告（pending/gap/warnings 分别列表）

**前置**：
- Phase 0：service-account 鉴权（D14）
- Phase 0：验证 ATEStudio 条件签字端点存在（`POST /api/v1/test_conditions/{id}/approve` 或类似）

**验收**：
- `tests/test_dev_agent.py`: stub LLM + stub ATEStudio plan（含 pending/gap/warnings 三种场景）
- Live 用例：3 个真实产品，dev_agent 输出含正确分类的解读报告

#### B.4 debug_agent — 调测诊断（2 周，S4 指标修正）

**实现**（matrag-agent `service.py` + `skills/debug-cycle.skill.md`）：
- 输入：execution_id + 失败 step_id + 设备读数 + plan_yaml
- 流程：
  1. 拉取 ATEStudio failure_indexer 历史（同型号/同方法）
  2. RAG 检索（已知 root cause patterns）
  3. LLM 综合：可能根因 Top-N + 推荐下一步
- 输出：诊断报告（root cause + 建议）

**S4 验收指标修正**：
- 样本量 ≥30（不是 10）
- 「命中」= top-3 含真实根因
- 阈值 ≥70%（不是 60%）

**验收**：
- `tests/test_debug_agent.py`: stub failure_indexer + stub LLM
- Live 用例：失败样本 10 个，命中率 ≥60%

### Phase C：知识层与增量导入（持续）

#### C.1 跨仓知识回链
- 现状：MATRag KG 1222/4062；ATEStudio failure_indexer 落档 FMEA
- 建议：周期性把 failure_indexer 高频故障模式 → 通过 MATRag MCP `add_knowledge` 入公共知识（**需新增工具**）
- **公共知识增删必须走人工**，不能让自动服务直接写

#### C.2 增量导入
- MATRag `bundle_diff.py`：旧 bundle vs 新 bundle → 增/改/迁 diff JSON
- ATEStudio 增量端点：`POST /api/v1/imports/aterag/incremental`
- 价值：规格书修订 ~95% 导入时间节省

### Phase D：部署硬化（持续）

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

## 6. 测试策略

### 6.1 离线优先
- 所有新增模块必须 offline-runnable
- stub LLM / stub NATS / stub HTTP / stub device
- 真机 / 真 LLM 用例按 `pytest.mark.skipif(not settings.live)` 跳过

### 6.2 仿真优先于真机
- ATEStudio `ate_platform/simulation/headless_runner.py` + `full_chain_simulator.py` 已就位
- 真机测试仅在仿真通过后跑

### 6.3 测试覆盖目标
- Phase A 完成后：pytest ≥1400（新增 80+）
- Phase B 完成后：pytest ≥1500（新增 100+）
- ruff 全绿

### 6.4 契约测试
- contract_hash 漂移立即红（已有 `test_contract_hash_matches_frozen_baseline`）
- 词表漂移立即红（新加 `test_contract_sync.py`）

---

## 7. 风险登记

| # | 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|---|
| R1 | 词表漂移未捕获 | 高 | 漏测类目 | A.1 自动化契约同步门 |
| R2 | bundle 字段升级时三侧 PR 漏一 | 中 | 静默丢字段 | contract_hash 三向一致 + CI gate |
| R3 | 真机设备池策略选错 | 中 | 投入浪费 | A.2 先做 HTTP+MQTT 双适配探针 |
| R4 | mat-device-control `>=3.14` 错配 | 中 | Python 版本冲突 | 降级到 3.13；与 ATEStudio/MATRag 一致 |
| R5 | ATEStudio langchain→Pydantic AI 改动过大 | 中 | ate_cloud 启动失败 | 局部改造（仅 `aterag_agent.py` 一文件）；分阶段 PR |
| R6 | matrag-agent/ 独立仓跨版本锁 | 低 | bundle 契约漂移 | contract_hash 闸门 + 端到端测试 |
| R7 | Agent 代签红线被破坏 | 中 | 产线错判 | ALLOWED + DENIED 双轨；CI 工具列表 pin |
| R8 | 端到端真机回路未跑通就 ship | 高 | 上线即崩 | A.4 demo 闸门（必须录像） |
| R9 | MATRag/ATEStudio 启动耦合债延续 | 中 | 跨仓协作摩擦 | D.1 解耦 |
| R10 | 公共契约文件被 Agent 改动 | 低 | 跨仓大震荡 | 维持主会话单点（D5） |
| R11 | NATS 事件回 MATRag 越权 | 低 | 写污染 | D2 默认否；评审单独决策 |
| R12 | hardware-test-agent（DeerFlow）功能重复 | 低 | 维护负担 | 已决策 X4：HT-A 功能合并到 matrag-agent/；HT-A 自身下线；新能力以 skill/agent 形式存在（Phase B.5） |
| R13 | matrag-agent → ATEStudio 鉴权缺口 | 高 | B.3 dev_agent 无法调用 ATEStudio | **Phase 0 必做**（D14）：新增 service-account 用户 + 长期 access token；matrag-agent 通过 env 注入 |
| R14 | dev_agent 草案被 plan_flow 红线排除 | 高 | B.3 语义不成立 | **D16 修复**：dev_agent 报告分 pending（需走评审）/ gap（需补 binding）/ warnings 三类分别处理 |
| R15 | 真机 driver 实例化桩未纳入 | 高 | edge 执行侧无驱动 | **D15 修复**：真机适配在 edge driver 层（`MatDeviceHttpDriver`），不走 cloud 侧桥 |
| R16 | mat-device-control HTTP 端点无鉴权 | 中 | 任意调用者可执行设备方法 | **D13 延后**：当前仅内网 + method 白名单；正式鉴权待后续评估（不阻塞 A.2 验收） |
| R17 | 两套 driver 体系映射未定义 | 高 | edge 侧无法接真机 | **D15 修复**：MatDeviceHttpDriver 实现 BaseDriver 接口，mapping.py data-driven；双轨由 fixture 配置选择 |

---

## 8. 验收标准

### Phase A（端到端 demo）

- [ ] 词表同步门：CI 红蓝绿三向一致
- [ ] bundle → plan_yaml：dry-run 0 gap（PA601 76 reqs 全部有判据）
- [ ] plan → script → execution：DB 落档 + SSE 推送
- [ ] 设备读数 → DB → SSE：单步验证（PA601 输出电压 54V 范围）
- [ ] pytest 1326 → ≥1400
- [ ] ruff 全绿

### Phase A（端到端 demo）

- [ ] Phase 0：service-account 鉴权就绪（D14）；ATEStudio 条件签字端点验证；硬件（.25 板 + 仪器）连接就绪
- [ ] 词表同步门：CI 红蓝绿三向一致
- [ ] `MatDeviceHttpDriver` 实现 BaseDriver 接口完整；method 白名单生效
- [ ] bundle → plan_yaml：dry-run 0 pending + 0 gap（spec-only PA601 bundle）
- [ ] plan → sequence → execution：DB 落档 + FILL_IN 桩已替换 + SSE 推送
- [ ] 设备读数 → DB → SSE：单步验证（PA601 输出电压 54V 范围）
- [ ] pytest（**按仓拆分**：MATRag 主仓 ≥1400；ATEStudio + MatDeviceHttpDriver 测试新增；mat-device-control + devices API 测试新增；ruff 全绿）

### Phase B（Agent 服务）

- [ ] matrag-agent/ 仓库可独立 `pip install -e .` + 启动
- [ ] `POST /agent/dev` 返回 bundle draft + **分类解读报告**（pending/gap/warnings 分别列表）
- [ ] `POST /agent/debug` 返回 root cause Top-N + 建议
- [ ] `POST /agent/review` 返回 plan 解读 + 修复建议
- [ ] ALLOWED_TOOLS / DENIED 工具列表被 CI pin 住
- [ ] ATEStudio `aterag_agent.py` langchain → Pydantic AI 完成；测试全绿（其余 5 文件 langchain_openai 迁移另立 backlog）
- [ ] debug_agent live 评测：30 样本，top-3 命中率 ≥70%
- [ ] pytest（按仓拆分：MATRag 主仓 + matrag-agent/）；ruff 全绿

### Phase C+D（持续）

- [ ] failure_indexer.audit_report(execution_id) 纯函数实现 + 单元测试
- [ ] knowledge_back_link → MATRag 公共知识回链；人工闸门
- [ ] bundle_diff 准确率 100%（旧 vs 新）
- [ ] ATEStudio `__init__.py` 解耦完成；verify_bundle_ate.py 移除 shim
- [ ] PG prod profile + 迁移演练通过
- [ ] R16 mat-device-control 正式鉴权（待生产化阶段评估）

---

## 9. 时间表（含跨仓协调 buffer）

| 阶段 | 周 | 关键产出 | 闸门 |
|---|---|---|---|
| **Phase 0** 决策与硬件前置 | 1 | D14 service-account 落地；S5 硬件就绪（.25 板 + 仪器连接）；验证 ATEStudio 签字端点存在 | Phase A 准入 |
| **A.1** 契约同步门 | 1 | tools/contract_sync/ + CI | 词表三向一致 |
| **A.2** 真机设备池适配（edge driver 层） | 3 | `ate_platform/drivers/mat_device/MatDeviceHttpDriver` + `modules/api/routes/devices.py`（method 白名单） | 至少 1 仪器端到端 |
| **A.3** 链路层审计 | 0.5 | `failure_indexer.audit_report(execution_id)` 纯函数 | 单元测试 |
| **A.4** 端到端 demo | 2~3 | PA601 真机单步（spec-only bundle + FILL_IN 桩 + sequence + execution） | demo 录像 + pytest + contract_hash 三向一致 |
| **B.1** Agent 服务骨架 | 2 | matrag-agent/ + Pydantic AI + ALLOWED_TOOLS | service+tools+skills 测试 |
| **B.2** ATEStudio langchain→Pydantic AI（D8a） | 1~2（与 B.1 并行） | 仅 aterag_agent.py 替换 | tests/unit 全绿 |
| **B.3** dev_agent（D16 语义修正） | 2 | 自然语言 → bundle draft + 分类解读报告（pending/gap/warnings） | LLM stub + live skipif |
| **B.4** debug_agent（S4 指标修正） | 2 | 失败 → 诊断报告（top-3，30 样本 ≥70%） | live 样本评测 |
| **B.5** dev_scripts_agent（吸收 HT-A） | 1~2 | pytest 脚本生成 + prd-gen skill；**输入全部来自 MATRag** | dev_scripts_agent 测试；HT-A 目录归档 |
| **C.1** 跨仓知识回链 | 持续 | knowledge_back_link.py | 人工闸门审计 |
| **C.2** 增量导入 | 持续 | bundle_diff.py | diff 准确率 100% |
| **D.1** ATEStudio 解耦 | 1 | create_app + 移除 shim | verify_bundle_ate.py 不再 shim |
| **D.2** PG prod | 持续 | prod profile + 迁移演练 | 压测通过 |
| **D.3** Explorer prod | 持续 | deploy/explorer-prod.md + 监控 | 7×24 监控 |

合计：**12~16 周**完成 Phase 0+A+B（含跨仓协调 +20~30% buffer）；Phase C/D 与 backlog 并行。

---

## 10. 开放问题（评审需决策或留待 Phase 内解决）

1. **mat-device-control Python 版本降级**：需降级到 3.13，是否在 Phase A.2 同期做？（建议同期）
2. **ATEStudio `create_app()` 改造范围**：仅 main.py vs 涉及 lifespan 拆分？（建议仅 main.py；lifespan 拆分留待 D.1 实施时评估）
3. **matrag-agent/ 与 MATRag 主仓共享工具**：跨仓 import 方式（pip + git tag vs submodule）？（建议 pip install from git tag，避免 submodule 复杂度）
4. **bundle 契约 v1.1 何时升级**：当前 1.0 够用；下次升级触发条件（新增字段需求，留待实际需求出现时评估）
5. **Pydantic AI 版本**：与 MATRag P3-0 锁同一版本（避免 API 漂移）
6. **跨仓观测方案选型**（D9 延迟项）：OpenTelemetry / Prometheus + Grafana / 三仓各自
7. ~~hardware-test-agent 归档时机~~ → **已决策 D11/X4**：Phase B.5 完成后立即执行（dev_scripts_agent 验证通过 = HT-A 可下线）
8. **ATEStudio 条件签字端点**：dev_agent 流程需要调签字端点（`POST /api/v1/test_conditions/{id}/approve` 或类似）；Phase 0 必查实端点契约（不在 spec 假设）
9. **dev_scripts_agent 与 script_generator.py 边界**：HT-A 合并后两者均产脚本但用途不同（开发期 vs 生产期）；是否在 B.5 同期明确裁决？（建议 B.5 立项前裁决）
10. **R16 mat-device-control 鉴权延后**：当前仅 method 白名单 + 内网；何时正式鉴权？（待生产化阶段评估）

---

## 11. 纪律摘要（不可绕过）

来自 `AGENTS.md` / `TODO.md`：
- Python 3.13；`src/matrag/` 布局；导入一律 `from matrag.xxx import yyy`
- **公共契约文件非本人任务不得修改**：`src/matrag/types.py`、`config.py`、`llm.py`、`pyproject.toml`、`TODO.md`
- 测试在 `tests/`，与被测模块同名镜像
- 依赖已预装，禁止 `uv run`/`uv sync`/`uv add`；命令统一用 venv 直调
- 风格：docstring/注释中文；类型标注完整；异常用 `matrag.types` 里定义的或标准库；失败 fail-fast
- 收尾必做：pytest 全绿 + ruff 无 E/F

**本 spec 严格遵守**：
- 所有新增模块在 `tools/contract_sync/`（MATRag）、`src/matrag_agent/`（新仓）、`ate_platform/drivers/mat_device/MatDeviceHttpDriver`（ATEStudio edge driver 层）、`mat-device-control/modules/api/routes/devices.py`（ATETest）等新目录
- 不触碰公共契约；新模块必须含中文 docstring + 完整类型 + fail-fast

---

## 12. 评审 checklist

- [ ] 决策矩阵（D1~D16）是否合理
- [ ] 方案 B 渐进重构的范围是否合适
- [ ] 模块责任边界（§4）是否清晰
- [ ] 跨仓依赖矩阵（§4.4）是否合理
- [ ] 实施阶段（§5）时间表是否符合资源约束
- [ ] 测试策略（§6）是否完备
- [ ] 风险登记（§7）是否完备
- [ ] 验收标准（§8）是否可执行
- [ ] 开放问题（§10）决策后是否纳入 Phase 0
- [ ] 与 hardware-test-agent 边界（§7 R12）是否清晰：已决策 X4（功能合并 matrag-agent/，HT-A 下线，Phase B.5 实施）