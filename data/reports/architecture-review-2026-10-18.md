# 架构升级方案 Spec 深度评审报告（2026-10-18）

> **评审对象**：`docs/superpowers/specs/2026-10-18-architecture-upgrade-design.md`
> **评审方式**：主会话自评 + oracle 独立评审（三仓源码文件级核查）
> **结论**：方案方向正确（D1~D11 决策合理），但**存在 4 个硬阻断项，不可直接进入 writing-plans**；修复后可行。

---

## 1. 事实性错误（spec 与代码不符）

### F1【严重】「langchain 只在 aterag_agent.py 一处」是错的 —— 实为 6 处

langchain 家族分布（已 grep 证实）：

| 文件 | 用法 |
|---|---|
| `services/aterag_agent.py` | `langchain_mcp_adapters` + `langchain.agents` + `langchain.chat_models` |
| `services/script_generator.py:252-253` | `langchain_core.prompts` + `langchain_openai.ChatOpenAI` |
| `services/diagnosis_service.py:97-98` | 同上 |
| `services/traceback_analyzer.py:130-131` | 同上 |
| `services/query_rewrite.py:157-158` | 同上 |
| `services/embedding_service.py:60` | `langchain_openai.OpenAIEmbeddings` |

**影响**：D8 的语义必须显式化。若目标是「技术栈收敛」，只改 aterag_agent.py 达不到。二选一：
- **D8a**：只替换 agent 编排框架（langchain-mcp-adapters → Pydantic AI MCP adapter），B.2 工期 1~2 周成立
- **D8b**：替换全部 6 处 langchain 依赖（5 处 ChatPromptTemplate/ChatOpenAI 可统一走轻量 LLM 门面），工期 +2~3 周，需另立 backlog 项

**建议**：D8 改为 D8a + 新增 backlog 项「langchain_openai → 轻量 LLM 门面迁移（5 文件）」；spec 措辞从 "langchain" 改为 "langchain-mcp-adapters"。

### F2【严重】A.3 exec_observer 订阅的 subject 不存在

spec 写「订阅 NATS `ate.executions.*`」。实际边缘发布的是 **`ate.status.{execution_id}`**（`scheduler/jetstream_worker.py:667`），云侧 `execution_status_relay.py` 以 pull_subscribe 消费同一流。`ate.executions.*` 全仓无定义。

### F3【严重】A.3 exec_observer 与 execution_status_relay 功能 100% 重叠

`execution_status_relay.py`（已在 lifespan 启动）已经做：订阅 status → 更新 Execution DB `step_results`/`status` → push SSE → 转交 failure_indexer。A.3 计划新建 observer 做「写 DB」= 重复造轮子，且两个 pull consumer 写同一 Execution 行有 ack 竞争。

**建议**：删除独立 exec_observer；`audit_report(execution_id)` 作为纯函数挂进 failure_indexer（复用已落库的 step_results）。省 2 周。

### F4【严重】demo 路径第 6 步 `POST /api/v1/scripts` 契约错误

`api/v1/scripts.py` 的 POST 是纯 CRUD（`name`/`script_path`/`params_schema`/`tags`），**不接收 plan_yaml**。真正的「plan → 脚本」是 `aterag_script_emit.generate_all(plan)`，但**没有任何 HTTP 端点**暴露它（全仓无路由调用）。`POST /scripts/generate` 是从自然语言 spec_text 生成，也不是从 plan_yaml。

**结论**：demo 路径「plan_yaml → 脚本落库」环节在代码里是断的，需新建端点或改走序列化路径。

### F5【严重】demo 路径缺 sequence 创建环节

`ExecutionCreate` 要求 `sequence_id` 必填（`schemas/execution.py:24`）；sequence 需先经 `POST /api/v1/sequences` 写入 `yaml_content`。demo 路径无此步。且 `plan_aterag_bundle` 返回的 plan_yaml 是**草稿**，含 `fixture_id: FILL_IN_AT_COMMISSION`（`flow_planner.py:293`）——FILL_IN 桩无法被 step_executor 执行。

### F6【严重】demo 路径缺「签字」环节，0 gaps 验收无法达成

`plan_flow` 默认 `include_draft=False` 是**红线**（`flow_planner.py:432-441`）：draft 条件整体进 `plan.pending`，不排入序列。MATRag narrative 提案全为 draft；spec 表解析为 approved。`_apply_review_state` 把 DB 里已签字 approved 的条件按 fingerprint overlay 上去。

demo 路径 10 步**没有任何签字步骤**。若 PA601 有 narrative draft 条件，plan 会整体排除 → `0 gaps` 验收失败。**前提必须显式声明**：demo 用 spec-only bundle（76 reqs 全 approved），或 demo 路径补「签字」步骤。

### F7【轻微】§4.3 模块清单遗漏 `modules/api/routes/config.py`（现有文件，main.py:7-8 挂载）

---

## 2. 遗漏风险（R1~R12 之外）

### R13【高，硬阻断】matrag-agent → ATEStudio 鉴权缺口

ATEStudio **无 API key / 服务账号机制**，只有 JWT + RBAC scope；`/imports/aterag/plan` 要求 `aterag:import` scope 且 imports_router 在 `_PROTECTED_ROUTERS` 中。matrag-agent 作为独立服务**没有用户凭据、没有长期 token 获取路径**。这是 B.3 dev_agent 的硬阻断。

**建议**：Phase 0 决策：①新增 service-account 用户 + 长期 access token（JWT 无此机制，需新增）；②matrag-agent 配 `ATe_DEV_MODE=true` 的私有实例；③引入 API-key 机制。

### R14【高，硬阻断】B.3 dev_agent 流程与 plan_flow 红线冲突

dev_agent 提案的是**全新 bundle**，DB 里没有对应 approved 条件 → `_apply_review_state` 无法 overlay → plan_flow 把全部 draft 排除进 `pending`（不是 gaps）。spec 把「未签字（pending）」与「无判据（gap）」混为一谈，B.3 验收「解读 plan_gaps 给建议」语义不成立。

**建议**：B.3 流程重定义：区分两种产出——pending（需走评审闭环）与 gap（需补 binding 表）；dev_agent 报告分别处理。

### R15【高】真机 driver 实例化桩未纳入范围

`aterag_script_emit.py` 的 `_driver()` 是 `raise NotImplementedError(FILL_IN)`（L295-299）。demo 第 9 步执行时 edge 如何拿到真机句柄（mat-device-control HTTP）完全未定义。A.2 的 bridge 在 cloud 侧，无法拦截 edge 侧的驱动调用。

### R16【高】mat-device-control 新 HTTP 端点无鉴权

现有代码零鉴权；新增 `POST /devices/{id}/call` = 反射式任意方法调用暴露到网络 = 任何能到达端口的调用者都能 set_voltage / 短路注入。

**建议**：最小安全 = 内网限定 + 可选 API key + method 白名单（禁 `__` 开头、禁非设备方法）。

### R17【高】两套 driver 体系映射未定义

真机（`DeviceController.call(method, kwargs)` 反射）与仿真（`ate_platform/drivers/` DriverRegistry）是两套完全不同的驱动接口。spec A.2 的双轨路由只设计到 cloud 侧，**edge 执行侧**的驱动适配（脚本 `measure_dc_voltage` → mat-device-control `call("dmm001", ...)`）无映射表定义。这是「真机链路未跑通」原始问题的核心。

---

## 3. 架构缺陷

### A1 双轨接入点错误（边界放错层）

A.2 把 bridge 放 cloud 侧，但执行在 edge 侧。数据流：`cloud dispatch → NATS → edge jetstream_worker → step_executor → _driver()`。真机适配的正确位置是 **edge 的 driver 层**——新增实现 ate_platform driver 接口的 `MatDeviceHttpDriver`（内部调 `POST /devices/{id}/call`），仿真/真机双轨变成"driver 层二选一"，而非 cloud 侧独立桥。demo 第 9 步的 step_executor 无需改动即可接真机。

### A2 D2「事件不回 MATRag」与 C.1「知识回链」存在张力

C.1 的 failure_indexer 数据源正是 NATS 事件（经 relay 转交）。数据实际还是跨仓回流了（NATS→MCP 批处理）。需显式声明：**D2 禁的是事件级实时回流；允许的是人工审核后的知识级回流**。

### A3 matrag-agent「不写 DB」与「bundle draft 进 ATEStudio」边界模糊

B.3 输出的 bundle draft 要进评审/导入流程必然落库。当前 `/aterag/plan` 只读、`/imports/aterag` 落库需 scope。dev_agent 草案由谁、经哪个端点落库供人工签字？依赖矩阵 §4.4 需补「dev_agent 草案提交」交互。

### A4 contract_sync 跨仓可达性未定义

MATRag CI 如何读到 ATEStudio 的 `config/test_method_bindings.yaml`？跨仓文件路径/获取方式（submodule / 快照 / 镜像）未定义。`tools/contract_sync/` 在 MATRag 仓但要读 ATEStudio 文件，需明确数据源路径（建议 env var `ATE_STUDIO_ROOT` + 默认兄弟目录路径）。

---

## 4. 主会话自评补充（oracle 之外）

### S1 matrag-agent 缺 provenance 审计设计

MATRag 主仓有 provenance sqlite 审计（answer_id 可回查）。matrag-agent/ 独立仓的「不代签」红线同样需要审计痕迹（谁、何时、产出什么 draft、谁签字）。spec 未提及。**建议**：matrag-agent 落本地 sqlite 审计（job → 输入 → 产物指纹 → 签字状态），与主仓 provenance 同构。

### S2 dev_scripts_agent 产物消费者不明

HT-A 原产 pytest 脚本 + SSH 部署到边缘。合并后 dev_scripts_agent 产出 pytest 脚本，**谁运行它们**？若执行归 ATEStudio（DSL 而非 pytest），pytest 脚本的消费者只有本地开发/CI。**建议**：B.5 立项前明确消费者；若仅是开发期辅助，降级为 optional skill；若要与 ATEStudio script_generator.py 竞争，需先做边界裁决（后者有 circuit breaker + 版本管理）。

### S3 验收标准 pytest 计数跨仓歧义

§8「pytest 1326 → ≥1400」未指明是哪个仓。新增测试分散在 MATRag 主仓 / matrag-agent / ATEStudio / mat-device-control 四仓。**建议**：验收标准按仓拆开（MATRag 主仓 ≥1400；matrag-agent 全新基线；ATEStudio 现有 + bridge 测试；mat-device-control 现有 + devices API 测试）。

### S4 debug_agent 命中率指标定义弱

「失败样本 10 个，命中率 ≥60%」：样本量太小（10 个 60% = 6/10，置信区间宽）；「命中」定义不明（top-1 还是 top-3？）。**建议**：样本 ≥30、指标 = top-3 含真实根因、阈值 ≥70%。

### S5 A.4 硬件环境依赖未声明

demo 需要物理仪器（PSU/DMM）连到 .25 板。spec 未声明硬件就绪前提。**建议**：A.4 前置条件清单（仪器清单、连线、驱动状态）。

### S6 多仓协调 overhead 未计入

Phase A 跨 4 仓并行改动（MATRag tools/ + ATEStudio services/ + ATETest modules/ + matrag-agent 新建）。spec 12 周未含跨仓协调 buffer。**建议**：+20~30% buffer（14~16 周）。

### S7 RAG drift

规格书变更 → MATRag 索引需重建 → bundle 需重生成。链路中无触发机制。**建议**：`extract_bundle.py` 输出 `product_doc_fingerprint` 已存在；新增「指纹变化 → 提示重建索引 + 重导出」的 CI 检查（低成本）。

---

## 5. 评审结论

### 硬阻断项（必须先修，否则不可进入 writing-plans）

| # | 阻断项 | 修复动作 |
|---|---|---|
| 1 | F4/F5/F6：demo 路径契约错误（scripts 端点 / 缺 sequence / 缺签字） | §5 A.4 重写为可执行序列（见下） |
| 2 | F2/F3：exec_observer subject 不存在 + 与 relay 重叠 | A.3 删除，改 failure_indexer 纯函数 |
| 3 | R13：matrag-agent → ATEStudio 鉴权缺口 | Phase 0 决策鉴权方案 |
| 4 | R14：dev_agent 草案被红线排除 | B.3 流程重定义（pending vs gap） |

### A.4 demo 路径修正版（可执行）

```
1. extract_bundle --product PA601-D54A（spec-only，声明无 narrative draft）
2. POST /imports/aterag?dry_run=true → preview
3. 签字环节：draft 条件走评审 → DB status=approved（若 demo 用 spec-only 可跳过）
4. POST /imports/aterag?dry_run=false&confirm_overwrite=true → 落库
5. POST /imports/aterag/plan → plan_yaml（_apply_review_state overlay 后 0 pending）
6. 人工填 FILL_IN 桩：fixture_id + _driver() 实例化（MatDeviceHttpDriver）
7. POST /api/v1/sequences（yaml_content=plan_yaml）→ sequence_id
8. POST /api/v1/executions（sequence_id + uut_sn）→ execution_id
9. execution_dispatch → NATS ate.tasks.{id} → edge jetstream_worker → step_executor → MatDeviceHttpDriver → POST /devices/{id}/call → 读数
10. edge 发布 ate.status.{id} → relay 落 DB + SSE → 前端
```

### 次要修复（不阻塞 writing-plans，Phase 内明确）

| # | 项 | Phase |
|---|---|---|
| F1 | D8 范围显式化（D8a + backlog 项） | Phase 0 |
| R15/R17/A1 | 真机适配点移到 edge driver 层（MatDeviceHttpDriver） | A.2 重设计 |
| R16 | mat-device-control 最小安全（内网 + API key + method 白名单） | A.2 |
| A2 | D2/C.1 语义显式化（事件级 vs 知识级） | Phase 0 |
| A3 | dev_agent 草案提交交互补入 §4.4 | B.3 |
| A4 | contract_sync 数据源路径定义 | A.1 |
| S1~S7 | provenance / 消费者 / 验收拆仓 / 指标 / 硬件前置 / buffer / drift | 各 Phase |

### 方案整体判断

决策矩阵 D1~D11 方向正确；方案 B 渐进重构是正确选择；single source of truth（D10）与跨仓依赖方向大体成立。**demo 路径契约描述与代码脱节**（作者未核对 scripts.py/sequences.py/executions.py 真实端点）+ **真机双轨 driver 适配点放错层** 是两大根本问题。修完后可进入 writing-plans。

---

## 6. 评审待决策清单（供用户）

1. **F1**：D8 选 D8a（只换 agent 框架）还是 D8b（全 6 处）？
2. **F2/F3**：接受「删除 exec_observer，audit_report 挂 failure_indexer」？
3. **R13**：鉴权方案选 ①service-account + 长期 token ②dev_mode 私有实例 ③API-key？
4. **R14**：接受 B.3 重定义（pending vs gap 分离）？
5. **R15/R17/A1**：接受真机适配点移到 edge driver 层（MatDeviceHttpDriver）？
6. **S2**：dev_scripts_agent 消费者明确（开发期辅助 vs 与 script_generator 竞争）？
7. **S5**：A.4 硬件环境（.25 板 + 仪器）何时就绪？
8. **S6**：工期 12 周 vs 14~16 周（含跨仓 buffer）？