# 架构升级实施计划（2026-10-18）

> **基于**：`docs/superpowers/specs/2026-10-18-architecture-upgrade-design.md`（v2，700 行）
> **状态**：FINAL — 73 任务已全部纳入实施；本计划为实施驱动文档（lane 落地见 `data/reports/architecture-upgrade-completion-2026-10-18.md`）
> **总工期**：12~16 周完成 Phase 0+A+B（含跨仓协调 buffer）
> **方法**：按 Phase 拆分 → 每 Phase 内部任务粒度 ≤ 2 天 → 并行 lane 标注 → 显式验收门
> **关键纪律**：
> - 离线优先（stub LLM/NATS/HTTP/device）
> - 公共契约（types/config/llm/pyproject/TODO）由主会话改，副任务回报
> - 失败 fail-fast，不得静默降级
> - 每 Phase 结束必做：pytest 全绿 + ruff 无 E/F + spec/报告更新

---

## 0. 全局前提（所有 Phase 共享）

### 0.1 仓库拓扑（4 仓）

| 仓库 | 路径 | 状态 |
|---|---|---|
| **MATRag** | `F:\Workspace\MATRag` | 主仓，公共契约所在 |
| **matrag-agent/**（新仓） | `F:\Workspace\matrag-agent` | Phase B.1 创建；Phase 0 准备 |
| **ATEStudio** | `F:\Workspace\ATEStudio` | 消费侧 + edge |
| **ATETest**（仅 mat-device-control） | `F:\Workspace\ATETest\mat-device-control` | 真机设备池 |

### 0.2 跨仓可达性

- **MATRag CI 读 ATEStudio**：env `ATE_STUDIO_ROOT` + 默认 `../ATEStudio`（兄弟目录）
- **ATEStudio CI 读 MATRag**：同上反向
- **matrag-agent 装 MATRag 工具**：`pip install "matrag @ git+file:///<MATRag_path>@main"`（git tag 而非 submodule）

### 0.3 Python 版本

- 三仓统一 **3.13**（mat-device-control `requires-python>=3.14` 是错配，Phase A.2 同期降级）

### 0.4 测试矩阵

- MATRag 主仓：`pytest ≥1400`（现有 1326 + 新增 ≥74）
- matrag-agent/：全新基线
- ATEStudio：现有 + bridge 测试新增
- mat-device-control：现有 + devices API 测试新增
- ruff 全绿（任一仓）

### 0.5 决策 ID 映射（spec §2）

D1=γ双轨 / D2=NATS不回MATRag / D3=独立仓库 / D4=producer主 / D5=主会话单点 / D6=方案B / D7=HTTP+SSE / D8a=仅agent框架 / D9=OTel延迟 / D10=规格书单一源 / D11=HT-A合并 / D12=D8a显式 / D13=R16延后 / D14=service-account / D15=edge driver / D16=pending/gap分离

---

## Phase 0：决策与硬件前置（1 周）

**目标**：B 阶段开工的所有前置条件就绪。

| ID | 任务 | 文件/位置 | 工期 | 依赖 | 验收 |
|---|---|---|---|---|---|
| P0.1 | **D14 鉴权落地**（ATEStudio）— 新增 service-account 机制：①admin 角色 + scope `aterag:import` 长期 access token；②`POST /api/v1/auth/service-token` 给 matrag-agent 用 | `ate_cloud/auth/{jwt,dependencies,password}.py`、`api/v1/auth.py` | 2 天 | 无 | matrag-agent 用长期 JWT 调 `/imports/aterag/plan` 成功 |
| P0.2 | **ATEStudio 条件签字端点已查实** — 端点是 `POST /api/v1/knowledge/conditions/approve`（payload `{requirement_id, by, condition_ids?}`），路由前缀 `/knowledge`；B.3 dev_agent 调用此端点签 draft 条件 | （无需修改代码，仅记录） | 0 天 | 无 | plan 已记录端点契约 |
| P0.3 | **S5 硬件就绪**（.25 板 + 仪器）— 检查 PSU/DMM/SCOPE/ELOAD 物理连接 + `devices.toml` 配置；编写 `docs/hardware-readiness-2026-10-18.md` 清单 | `mat-device-control/devices.toml`、`ATETest/mat-device-control/docs/` | 1 天 | 无 | 至少 PSU + DMM 实测可 SCPI 通讯 |
| P0.4 | **mat-device-control Python 版本降级** — `requires-python>=3.14` → `>=3.13,<3.14`；uv lock 重生成 | `mat-device-control/pyproject.toml`、`uv.lock` | 0.5 天 | 无 | `uv sync` 成功；pytest 全绿 |
| P0.5 | **Pydantic AI 版本锁定**（MATRag P3-0 沿用版本；记录到 matrag-agent/pyproject.toml） | `matrag-agent/pyproject.toml` | 0.5 天 | 无 | 锁 `pydantic-ai-slim[openai]>=2.56,<3`（P3-0 实测版，非早废的 `>=0.7.2`） |

**Phase 0 验收门**：
- ✅ matrag-agent 用长期 JWT 调 `/imports/aterag/plan` 成功
- ✅ 至少 1 个 PSU 实测可通讯
- ✅ pytest 全绿（三仓）
- ✅ 报告 `data/reports/phase0-2026-10-25.md`（Phase 0 完成报告）

---

## Phase A：链路层补齐 + 真机 demo（6.5~8.5 周）

### Phase A.1：跨仓契约同步门（1 周）

| ID | 任务 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| A.1.1 | **新建 tools/contract_sync/** — `check_vocab.py` 读三端词表（KIND_UNITS / power_test_methods.yaml / test_method_bindings.yaml / DRIVER_TABLE）做双向差集 | `tools/contract_sync/{__init__,check_vocab,check_contract_hash}.py` | 1 天 | 三端 25 (kind,side) 一致；method_ref ⊆ yaml；action ⊆ DRIVER_TABLE |
| A.1.2 | **MATRag CI 接入** — `.github/workflows/contract-sync.yml` 跑 `tools/contract_sync/`；env `ATE_STUDIO_ROOT` | `.github/workflows/contract-sync.yml` | 1 天 | CI 红蓝绿 |
| A.1.3 | **ATEStudio CI 接入** — 镜像触发；env `MATRAG_ROOT` | `ATEStudio/.github/workflows/contract-sync.yml` | 1 天 | ATEStudio CI 同步触发 |
| A.1.4 | **离线测试** — `tests/test_contract_sync.py`：mock 三端文件 → 验证差异报告生成 | `tests/test_contract_sync.py` | 2 天 | pytest ≥6 例 |

**A.1 验收**：三端词表 CI 一致；离线测试全绿。

### Phase A.2：真机设备池适配 — edge driver 层（3 周）

| ID | 任务 | 文件 | 工期 | 依赖 | 验收 |
|---|---|---|---|---|---|
| A.2.1 | **MatDeviceHttpDriver 实现 BaseDriver 接口** — 继承 `ate_platform/drivers/base.py:BaseDriver`，实现 5 个 `_xxx_impl`；内部 HTTP 调 `POST /devices/{id}/call` | `ATEStudio/src/ate_platform/drivers/mat_device/http_driver.py` | 3 天 | Phase 0 | 单元测试全绿 |
| A.2.2 | **mapping.py** — data-driven 设备名映射（psu001↔PSU_1 等），从 `devices.toml` 读 | `ATEStudio/src/ate_platform/drivers/mat_device/mapping.py` | 1 天 | 无 | mapping 加载测试 |
| A.2.3 | **protocol.py** — HTTP 协议封装（POST 请求、响应解析、错误码映射） | `ATEStudio/src/ate_platform/drivers/mat_device/protocol.py` | 1 天 | A.2.1 | 协议单元测试 |
| A.2.4 | **driver registry 注册** — `ate_platform/drivers/registry.py` 注册 `MatDeviceHttpDriver` | `ATEStudio/src/ate_platform/drivers/registry.py` | 0.5 天 | A.2.1 | registry 双轨可见 |
| A.2.5 | **mat-device-control devices.py** — `POST /devices/{id}/call` 反射式端点 + method 白名单 | `ATETest/mat-device-control/modules/api/routes/devices.py` | 2 天 | 无 | curl 调用成功；method 白名单生效 |
| A.2.6 | **mat-device-control devices.py 单元测试** — 反射调用 + 白名单 + 异常路径 | `ATETest/mat-device-control/tests/test_devices_api.py` | 1 天 | A.2.5 | pytest ≥8 例 |
| A.2.7 | **ATEStudio edge 端到端测试** — stub HTTP + MatDeviceHttpDriver，验证 set_voltage / measure_voltage 完整链 | `ATEStudio/tests/edge/test_mat_device_http_driver.py` | 2 天 | A.2.4 | pytest ≥6 例 |
| A.2.8 | **真机集成测试**（live，skipif）— 用真实 PSU 跑 set + measure；记录延迟 | `ATEStudio/tests/integration/test_mat_device_live.py` | 2 天 | A.2.7 + 硬件 | live 跑通一次 |

**A.2 验收**：仿真轨（ate_platform/drivers/examples）+ 真机轨（MatDeviceHttpDriver）二选一；至少 1 仪器端到端。

### Phase A.3：链路层审计报告（0.5 周）

| ID | 任务 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| A.3.1 | **failure_indexer.audit_report(execution_id)** — 纯函数；读 DB；输出 used_requirement_codes / measured_conditions / failed_steps / pending_signoffs | `ATEStudio/src/ate_cloud/services/failure_indexer.py` | 1 天 | 单元测试 ≥4 例 |
| A.3.2 | **单元测试** — fixture Execution + step_results → 输出结构正确 | `ATEStudio/tests/unit/test_failure_indexer_audit_report.py` | 0.5 天 | pytest 全绿 |

**A.3 验收**：audit_report 函数独立可测；不订阅 NATS；不写 DB。

### Phase A.4：端到端 demo（2~3 周）

**前置**：Phase 0 + A.1 + A.2 + A.3 全部就绪。

| ID | 任务 | 文件/位置 | 工期 | 依赖 | 验收 |
|---|---|---|---|---|---|
| A.4.1 | **bundle 导出脚本验证**（MATRag）— `scripts/extract_bundle.py --product PA601-D54A --no-narrative --generated-at 2026-10-18T00:00:00Z` → bundle.json 含 76 reqs 全 approved | `MATRag/scripts/extract_bundle.py` | 0.5 天 | 无 | 计数 76；status 全 approved |
| A.4.2 | **dry-run 导入 + 落库** — curl `POST /api/v1/imports/aterag?dry_run=true` + `?dry_run=false&confirm_overwrite=true`；检查 DB 行 | `ATEStudio/src/ate_cloud/api/v1/imports.py` | 1 天 | A.4.1 | DB 验证：76 test_requirements + 76×N test_conditions + limits |
| A.4.3 | **plan 生成（0 pending + 0 gaps）** — `POST /api/v1/imports/aterag/plan`；verify plan.pending == [] + plan_gaps == [] | 同上 | 0.5 天 | A.4.2 | 字段验证 |
| A.4.4 | **FILL_IN 桩替换** — 人工填 fixture_id + 脚本 `_driver()` 实例化为 `MatDeviceHttpDriver("psu001")` | `ATEStudio/src/ate_platform/scheduler/` 脚本模板 | 1 天 | A.2 + A.4.3 | grep 无 `FILL_IN_AT_COMMISSION` |
| A.4.5 | **sequence 创建 + execution 启动** — `POST /api/v1/sequences` 写入 yaml_content；`POST /api/v1/executions` 引用 sequence_id + uut_sn | `ATEStudio/src/ate_cloud/api/v1/{sequences,executions}.py` | 1 天 | A.4.4 | DB 验证：sequence + execution 行 |
| A.4.6 | **NATS dispatch + edge 执行** — execution_dispatch publish `ate.tasks.{id}`；edge jetstream_worker 消费；step_executor → MatDeviceHttpDriver → 真机读数 | `ATEStudio/src/ate_cloud/services/execution_dispatch.py`、`src/ate_platform/scheduler/jetstream_worker.py` | 2 天 | A.4.5 + A.2.8 | 真实 PSU 读到 54.0V |
| A.4.7 | **SSE + DB 落档** — execution_status_relay 消费 `ate.status.{id}`；落 Execution.step_results + push SSE | `ATEStudio/src/ate_cloud/services/execution_status_relay.py` | 1 天 | A.4.6 | DB 验证 measured_value=54.0；SSE 推送 |
| A.4.8 | **demo 录像 + 报告** — `data/reports/phase-a4-demo-2026-XX-XX.md`：含 11 步执行序列 + 截图 + pytest 输出 | `MATRag/data/reports/` | 0.5 天 | A.4.7 | 报告完成 |

**A.4 验收门**：
- ✅ bundle contract_hash 三向一致
- ✅ plan.pending == 0 + plan_gaps == 0
- ✅ FILL_IN 桩已替换
- ✅ 设备真实读数 54.0V 落 DB
- ✅ SSE 推送可见
- ✅ pytest 全绿（按仓）

**Phase A 总验收**：
- pytest ≥1400（MATRag 主仓）；ATE 新增 ≥20；mat-device-control 新增 ≥8
- ruff 全绿
- 报告 `data/reports/phase-a-complete-2026-XX-XX.md`

---

## Phase B：Agent 服务层（7~8 周，可与 A 部分重叠）

### Phase B.1：MATRag Agent 服务骨架（2 周）

| ID | 任务 | 文件 | 工期 | 依赖 | 验收 |
|---|---|---|---|---|---|
| B.1.1 | **新建 matrag-agent/ 仓库** — `pyproject.toml` + 目录结构 + `from __future__ import annotations` + 中文章档 | `matrag-agent/pyproject.toml` | 0.5 天 | Phase 0 | `pip install -e .` 成功 |
| B.1.2 | **service.py** — FastAPI（端口 8322）；`/agent/dev`、`/agent/debug`、`/agent/review`、`/{job_id}/events` SSE | `matrag-agent/src/matrag_agent/service.py` | 2 天 | B.1.1 | HTTP 启动 + 4 端点可见 |
| B.1.3 | **clients/llm.py** — Pydantic AI；model `qwen3.7-flash`，enable_thinking=False | `matrag-agent/src/matrag_agent/clients/llm.py` | 1 天 | B.1.2 | 简单 completion 测试 |
| B.1.4 | **clients/gateway.py** — HTTP → MATRag gateway MCP；ALLOWED_TOOLS 白名单 + DENIED 黑名单双轨 | `matrag-agent/src/matrag_agent/clients/gateway.py` | 1 天 | B.1.3 | stub gateway 单元测试 |
| B.1.5 | **clients/atestudio.py** — HTTP → ATEStudio（带 P0.1 长期 JWT）；ALLOWED endpoints 白名单 | `matrag-agent/src/matrag_agent/clients/atestudio.py` | 1 天 | B.1.4 + P0.1 | stub atestudio 单元测试 |
| B.1.6 | **tools/{read_bundle,plan,derive}.py** — 基础工具骨架（stub） | `matrag-agent/src/matrag_agent/tools/` | 1 天 | B.1.5 | 工具注册测试 |
| B.1.7 | **skills/{bundle-review,test-dev,debug-cycle}.skill.md** — 工作流定义（占位） | `matrag-agent/src/matrag_agent/skills/` | 0.5 天 | 无 | YAML/Markdown 语法 OK |
| B.1.8 | **离线测试** — `tests/test_agent_service.py` + `tests/test_agent_tools.py` + `tests/test_agent_clients.py` | `matrag-agent/tests/` | 1.5 天 | B.1.6 | pytest ≥15 例；live 用例 skipif |

**B.1 验收**：matrag-agent/ 独立启动；4 端点 + SSE 通；离线测试全绿。

### Phase B.2：D8a — ATEStudio langchain → Pydantic AI（1~2 周，与 B.1 并行）

| ID | 任务 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| B.2.1 | **aterag_agent.py 替换 langchain-mcp-adapters → Pydantic AI MCP adapter** | `ATEStudio/src/ate_cloud/services/aterag_agent.py` | 3 天 | 单元测试全绿 |
| B.2.2 | **ALLOWED_TOOLS / DENIED 机制** — 迁移到 Pydantic AI tool registry；CI pin 工具列表 | 同上 + 测试 | 1 天 | 工具列表 CI check |
| B.2.3 | **integration 测试** — 真实 MCP 通讯（live，skipif） | `ATEStudio/tests/unit/test_aterag_agent.py` | 1 天 | live 跑通 |

**B.2 验收**：aterag_agent.py 不再 import langchain-mcp-adapters；测试全绿；CI pin 生效。
**不在范围**（D12 backlog）：script_generator.py / diagnosis_service.py / traceback_analyzer.py / query_rewrite.py / embedding_service.py 的 langchain_openai 迁移。

### Phase B.3：dev_agent — 产测软件开发（2 周，D16 语义修正）

| ID | 任务 | 文件 | 工期 | 依赖 | 验收 |
|---|---|---|---|---|---|
| B.3.1 | **skills/test-dev.skill.md** — 工作流：自然语言 → RAG → LLM 提案 bundle draft → /aterag/plan → 三类分离解读 | `matrag-agent/src/matrag_agent/skills/test-dev.skill.md` | 0.5 天 | B.1.7 | skill 文档完成 |
| B.3.2 | **agents/dev_agent.py** — Pydantic AI Agent + tools=[read_bundle, plan, atestudio_review] | `matrag-agent/src/matrag_agent/agents/dev_agent.py` | 2 天 | B.3.1 | stub 测试 |
| B.3.3 | **tools/propose_test.py** — LLM 提案 bundle draft（status=draft，数值必见原文）；走 matrag P0 RAG 上下文 | `matrag-agent/src/matrag_agent/tools/propose_test.py` | 1 天 | B.1.6 | stub 提案测试 |
| B.3.4 | **三类分离解读** — pending / gap / warnings 分别返回结构化报告 | `matrag-agent/src/matrag_agent/agents/dev_agent.py` | 1 天 | B.3.2 | 三类场景单元测试 |
| B.3.5 | **离线测试** — `tests/test_dev_agent.py` 覆盖三类场景 | `matrag-agent/tests/test_dev_agent.py` | 1.5 天 | B.3.4 | pytest ≥12 例 |
| B.3.6 | **Live 用例**（skipif） — 3 个真实产品，至少 1 个产出完整分类报告 | `matrag-agent/tests/live/test_dev_agent_live.py` | 1 天 | B.3.5 | live 跑通 |

**B.3 验收**：dev_agent 输出含正确分类的解读报告（pending/gap/warnings 分别列表）；离线 + live 测试通过。

### Phase B.4：debug_agent — 调测诊断（2 周，S4 指标修正）

| ID | 任务 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| B.4.1 | **skills/debug-cycle.skill.md** — 工作流：execution_id + 失败 step → failure_indexer 历史 → RAG → LLM 综合 Top-N 根因 | skill 文件 | 0.5 天 | 文档完成 |
| B.4.2 | **agents/debug_agent.py** — Pydantic AI Agent + tools=[query_exec, failure_history, rag] | agents/debug_agent.py | 1.5 天 | stub 测试 |
| B.4.3 | **tools/query_exec.py** — 调 ATEStudio failure_indexer.audit_report(execution_id) | tools/ | 1 天 | 单元测试 |
| B.4.4 | **离线测试** — `tests/test_debug_agent.py`：stub failure_indexer + stub LLM | tests/ | 1 天 | pytest ≥8 例 |
| B.4.5 | **Live 评测**（skipif） — 30 失败样本，top-3 命中率 ≥70% | tests/live/ | 2 天 | 样本评测结果 |

**B.4 验收**：debug_agent 输出 Top-3 根因 + 建议；30 样本 top-3 ≥70%。

### Phase B.5：dev_scripts_agent（吸收 HT-A，1~2 周）

| ID | 任务 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| B.5.1 | **裁决 dev_scripts_agent 与 script_generator.py 边界**（Q9 必做） | spec 更新 + 决策记录 | 0.5 天 | 文档化边界 |
| B.5.2 | **agents/dev_scripts_agent.py** — Pydantic AI；输入 = MATRag RAG + power_test_methods.yaml + test_method_bindings.yaml；输出 = pytest 脚本（或 ATE platform 脚本，依赖 B.5.1 裁决） | `matrag-agent/src/matrag_agent/agents/dev_scripts_agent.py` | 2 天 | stub 测试 |
| B.5.3 | **skills/dev-scripts.skill.md + prd-gen.skill.md** — 工作流定义 | skills/ | 0.5 天 | 文档完成 |
| B.5.4 | **离线测试** — `tests/test_dev_scripts_agent.py` | tests/ | 1 天 | pytest ≥8 例 |
| B.5.5 | **HT-A 目录归档** — 删除 `ATETest/agent/hardware-test-agent/`；README + 设计文档 → `MATRag/docs/legacy/hardware-test-agent.md` | ATETest + MATRag | 0.5 天 | 归档文档完成 |

**B.5 验收**：dev_scripts_agent 测试全绿；HT-A 目录归档完成。

**Phase B 总验收**：
- matrag-agent/ 独立仓 + 5 agents（dev/debug/review/scripts + root）+ 3 clients + N tools
- ATEStudio `aterag_agent.py` langchain → Pydantic AI 完成（其余 5 文件 backlog）
- HT-A 下线归档
- pytest（matrag-agent 全基线 + ATEStudio B.2 测试）
- ruff 全绿
- 报告 `data/reports/phase-b-complete-2026-XX-XX.md`

---

## Phase C：知识层与增量导入（持续，与 A/B 并行）

### Phase C.1：跨仓知识回链（持续，1 周首次实现）

| ID | 任务 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| C.1.1 | **MATRag gateway 新增 `add_knowledge` MCP 工具** — 写 KG 节点/边；要求 caller 有 `knowledge:write` scope | `MATRag/src/matrag/gateway/tools.py` | 1 天 | stub 测试 |
| C.1.2 | **ATEStudio knowledge_back_link.py** — 周期调度：failure_indexer 高频故障模式 → MCP `add_knowledge`；人工审核闸门（写入前要求 reviewer 标记） | `ATEStudio/src/ate_cloud/services/knowledge_back_link.py` | 2 天 | 单元测试 |
| C.1.3 | **审计日志** — 每次写入记录 reviewer + 理由；写入失败显式留痕 | 同上 | 1 天 | 审计可查 |
| C.1.4 | **离线测试** | ATEStudio + MATRag 各测试 | 1 天 | pytest 全绿 |

### Phase C.2：增量导入（持续，1 周首次实现）

| ID | 任务 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| C.2.1 | **MATRag bundle_diff.py** — 旧 vs 新 bundle → 增/改/迁 diff JSON（基于 cond_fingerprint 与 requirement_code） | `MATRag/src/matrag/extract/bundle_diff.py` | 2 天 | 单元测试 |
| C.2.2 | **ATEStudio 增量端点** — `POST /api/v1/imports/aterag/incremental` body=diff | `ATEStudio/src/ate_cloud/api/v1/imports.py` | 1 天 | 单元测试 |
| C.2.3 | **准确率测试** — 三产品两版本比对 → diff 准确率 100% | tests/ | 1 天 | pytest 全绿 |

---

## Phase D：部署硬化（持续）

### Phase D.1：ATEStudio `__init__.py` 解耦（1 周，可与 Phase A 并行）

| ID | 任务 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| D.1.1 | **create_app() 工厂模式** — `ate_cloud/main.py` 改 `def create_app() -> FastAPI`；NATS/JetStream 初始化仅在 lifespan | `ate_cloud/main.py` | 2 天 | `app_factory` 单元测试 |
| D.1.2 | **`ate_cloud/__init__.py` 不再 `from .main import app`** — 仅导出公开类型 | `ate_cloud/__init__.py` | 0.5 天 | `from ate_cloud.schemas import BundleModel` 不触发 NATS |
| D.1.3 | **verify_bundle_ate.py 移除 shim** | `MATRag/scripts/verify_bundle_ate.py` | 0.5 天 | 不再依赖包路径 shim |
| D.1.4 | **回归测试** — 现有 ATEStudio 1326+ 测试不破坏 | tests/ | 1 天 | pytest 全绿 |

### Phase D.2：PG prod profile（持续）

| ID | 任务 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| D.2.1 | **MATRag `config/profiles/prod.yaml`** — pgvector + pg_textsearch 配置 | `MATRag/config/profiles/prod.yaml` | 1 天 | 文档 + 配置 |
| D.2.2 | **迁移演练脚本** — `scripts/migrate_store.py`（dry_run → 真实） | `MATRag/scripts/migrate_store.py` | 2 天 | sqlite → pg 迁移演练通过 |
| D.2.3 | **ATEStudio PG 配置**（已有） | 文档 | 0.5 天 | 文档完整 |
| D.2.4 | **压测**（locust 1000 req/min × 24h） | tests/load/ | 持续 | p99 < 1s |

### Phase D.3：Explorer prod（持续）

| ID | 任务 | 文件 | 工期 | 验收 |
|---|---|---|---|---|
| D.3.1 | **explorer_serve.py prod profile** — 独立进程 + 反向代理 + X-API-Key | `MATRag/scripts/explorer_serve.py` | 1 天 | 文档 |
| D.3.2 | **deploy/explorer-prod.md** | `MATRag/deploy/explorer-prod.md` | 0.5 天 | 文档完成 |
| D.3.3 | **ops_doctor.py backup/restore** | `MATRag/scripts/ops_doctor.py` | 1 天 | backup/restore 测试通过 |
| D.3.4 | **7×24 监控配置** | 部署 | 持续 | 监控告警 |

---

## 并行 lane 总结

```
Week 1-2:   Phase 0（鉴权+硬件） + Phase A.1（契约同步）
Week 3-5:   Phase A.2（edge driver） + Phase D.1（ATEStudio 解耦，可与 A 并行）
Week 6:     Phase A.3（audit_report） + Phase B.1 启动
Week 7-9:   Phase A.4（demo） + Phase B.1 + B.2 并行
Week 10-12: Phase B.3 + B.4
Week 13-14: Phase B.5（HT-A 合并）
持续:       Phase C + D
```

---

## 风险 + 缓解（在 spec §7.R 基础上补充执行层）

| # | 风险 | 缓解 |
|---|---|---|
| R13 | matrag-agent 鉴权缺口 | P0.1 必做；P0 准入门 |
| R16 | mat-device-control 零鉴权 | D13 延后；当前 method 白名单 + 内网；生产化阶段评估 |
| R18 | A.2 设备依赖风险 | P0.3 硬件就绪；A.2.7/8 含集成测试 fallback（用仿真 driver） |
| R19 | Phase B 工期超 8 周 | +20~30% buffer 已计入；每 Phase 末做 burn-down check |
| R20 | 跨仓 CI 同步失败 | A.1.3 env-based 路径 + 失败非阻断（仅警告） |

---

## 交付物清单

### 文档
- `MATRag/docs/superpowers/specs/2026-10-18-architecture-upgrade-design.md`（v2，700 行，已完成）
- `MATRag/docs/superpowers/plans/2026-10-18-architecture-upgrade-impl-plan.md`（本文件）
- `MATRag/data/reports/architecture-review-v2-2026-10-18.md`（已落盘）
- `MATRag/data/reports/phase0-2026-XX-XX.md`（Phase 0 完成报告）
- `MATRag/data/reports/phase-a-complete-2026-XX-XX.md`（Phase A 完成报告）
- `MATRag/data/reports/phase-b-complete-2026-XX-XX.md`（Phase B 完成报告）
- `MATRag/data/reports/phase-a4-demo-2026-XX-XX.md`（demo 报告，含录像）
- `MATRag/docs/legacy/hardware-test-agent.md`（HT-A 归档）
- `MATRag/deploy/explorer-prod.md`（D.3.2）

### 代码
- `MATRag/tools/contract_sync/{check_vocab,check_contract_hash}.py`
- `MATRag/scripts/verify_bundle_ate.py`（移除 shim）
- `MATRag/src/matrag/extract/bundle_diff.py`
- `MATRag/src/matrag/gateway/tools.py`（新增 `add_knowledge`）
- `ATEStudio/src/ate_platform/drivers/mat_device/{http_driver,mapping,protocol}.py`
- `ATEStudio/src/ate_cloud/services/failure_indexer.py`（新增 `audit_report`）
- `ATEStudio/src/ate_cloud/services/knowledge_back_link.py`
- `ATEStudio/src/ate_cloud/services/aterag_agent.py`（Pydantic AI 替换）
- `ATEStudio/src/ate_cloud/main.py`（`create_app()` 工厂）
- `ATETest/mat-device-control/modules/api/routes/devices.py`
- `matrag-agent/`（新仓）— service.py + clients/ + tools/ + skills/ + agents/

### CI / 配置
- `MATRag/.github/workflows/contract-sync.yml`
- `ATEStudio/.github/workflows/contract-sync.yml`
- `MATRag/config/profiles/prod.yaml`

---

## 验证总览（evidence path）

每 Phase 结束必查：
1. **代码层**：pytest 全绿 + ruff 无 E/F（按仓拆分）
2. **集成层**：A.4 真机 demo 录像 + B.3/B.4 live 样本评测
3. **文档层**：spec 与实际实现一致；过期项标注
4. **审计层**：matrag-agent provenance sqlite 审计（S1 在 B.1 落地）；失败留痕可查

---

## 12. 实施纪律

来自 `AGENTS.md`：
- Python 3.13（mat-device-control 降级同步）
- `src/matrag/` 布局；跨仓 import 严格
- **公共契约**（`src/matrag/types.py`、`config.py`、`llm.py`、`pyproject.toml`、`TODO.md`）由主会话改；副任务发现需改时回报
- 测试与被测模块同名（`tests/test_xxx.py`）
- 依赖已预装，禁 `uv run`/`uv sync`/`uv add`（mat-device-control 降级需 uv lock 重生成是例外）
- 中文 docstring + 完整类型 + fail-fast

跨仓改动建议：每 Phase 设一个主负责 + 跨仓 PR 链接备忘；不在主仓的改动回写到此 spec 的 changelog 段。