# 架构升级方案 Spec v2 重新评审报告（2026-10-18）

> **评审对象**：`docs/superpowers/specs/2026-10-18-architecture-upgrade-design.md`（v2，700 行）
> **评审基线**：v1 评审发现的 4 个硬阻断项 + 5 项遗漏风险 + 4 项架构缺陷 + 7 项主会话补充
> **决策已应用**：D8a（仅 aterag_agent.py）、R16 延后
> **结论**：4 个硬阻断项全部修复；评审通过，可进入 writing-plans

---

## 1. v1 评审问题修复状态

### 硬阻断项（4/4 已修复）

| # | 阻断项 | 修复动作 | 验证 |
|---|---|---|---|
| 1 | F4/F5/F6：demo 路径契约错误 | §5 A.4 重写为 11 步可执行序列（含 sequence 创建、FILL_IN 桩、spec-only bundle 前提） | spec §5 A.4 行 367+ |
| 2 | F2/F3：exec_observer subject 不存在 + 与 relay 重叠 | A.3 删除独立 observer；`failure_indexer.audit_report(execution_id)` 纯函数实现 | spec §5 A.3（0.5 周） |
| 3 | R13：matrag-agent → ATEStudio 鉴权缺口 | D14 决策：service-account + 长期 JWT；Phase 0 必做 | spec §2 D14；§4.4 鉴权规则段 |
| 4 | R14：dev_agent 草案被红线排除 | D16 决策：pending/gap/warnings 三类分离处理；B.3 流程重定义 | spec §5 B.3；D16 |

### 次要修复（5/5 已修复）

| # | 项 | 修复 | 位置 |
|---|---|---|---|
| F1 | D8 范围错误（langchain 实为 6 处） | D8a：仅 aterag_agent.py；其余 5 文件迁移单立 backlog | spec §2 D12；§4.2 改造段 |
| R15/R17/A1 | 真机适配点放错层（cloud 桥） | D15：移至 edge driver 层（`MatDeviceHttpDriver`）；bridge 方案作废 | spec §5 A.2 重写 |
| R16 | mat-device-control 零鉴权 | **D13 延后**（用户决策）；当前仅内网 + method 白名单；正式鉴权待生产化 | spec §5 A.2；§10 Q10 |
| A2 | D2/C.1 语义张力 | 显式声明：D2 禁事件级实时回流；允许知识级人工审核回流 | spec §2 D7 + D2 注释 |
| A3 | dev_agent 草案提交交互 | 依赖矩阵补「dev_agent 草案 → ATEStudio 评审签字 → DB 落库」交互 | spec §4.4 + §5 B.3 |

### 主会话补充修复（4/7 已修复，3/7 留 Phase 内处理）

| # | 项 | 修复 | 位置 |
|---|---|---|---|
| S1 | matrag-agent 缺 provenance 审计 | **留 writing-plans 处理**：matrag-agent sqlite 审计设计同构 MATRag | spec §10 提示 |
| S2 | dev_scripts_agent 消费者不明 | **Q9 留 B.5 立项前裁决**（与 script_generator.py 边界） | spec §10 Q9 |
| S3 | 验收 pytest 计数跨仓歧义 | §8 验收标准按仓拆分（MATRag / ATEStudio / mat-device-control / matrag-agent） | spec §8 |
| S4 | debug_agent 指标弱 | §5 B.4 + §8：30 样本、top-3 ≥70% | spec §5 B.4 |
| S5 | A.4 硬件前提未声明 | **Phase 0 前置**：硬件就绪（.25 板 + 仪器连接 + devices.toml） | spec §9 Phase 0 |
| S6 | 跨仓协调 buffer | §9 时间表：12 周 → 12~16 周（含 +20~30% buffer） | spec §9 |
| S7 | RAG drift | **留 writing-plans 处理**：低优先级，加 CI 检查 | spec §10 提示 |

---

## 2. v2 关键改动摘要

### 决策矩阵扩展（D12~D16）

| 新增 | 决策 | 选择 |
|---|---|---|
| D12 | langchain 范围 | **D8a**：仅 aterag_agent.py；其余 5 文件迁移另立 backlog |
| D13 | mat-device-control 鉴权 | **R16 延后**；当前仅内网 + method 白名单 |
| D14 | matrag-agent → ATEStudio 鉴权 | **service-account + 长期 JWT**（**Phase 0 必做**） |
| D15 | 真机适配位置 | **edge driver 层**（MatDeviceHttpDriver） |
| D16 | B.3 dev_agent 语义 | **pending 与 gap 分离** |

### §4.4 跨仓依赖矩阵（强化）

- ATEStudio edge → mat-device-control 改为 **D14：HTTP `POST /devices/{id}/call` 通过 MatDeviceHttpDriver 调用**（不是 cloud 桥）
- 鉴权规则段新增：跨仓 HTTP 必须有 JWT/API key；MATRag gateway 不出具 ATEStudio token
- 表格新增 ATEStudio cloud ↔ edge NATS 通讯行（`ate.tasks.*` 发布 / `ate.status.*` 消费）

### §5 A.2 重写（核心架构修复）

```
旧方案：ate_cloud/services/aterag_plan_exec_bridge/（cloud 侧桥）
新方案：ate_platform/drivers/mat_device/MatDeviceHttpDriver（edge driver 层）
       + mapping.py（设备名映射 data-driven）
       + mat-device-control/modules/api/routes/devices.py（method 白名单）
```

理由：cloud dispatch → NATS → edge step_executor → _driver() 的实际执行架构中，driver 适配应在 edge 而非 cloud。

### §5 A.3 简化（避免重复造轮）

```
旧方案：aterag_exec_observer/（订阅 NATS + 写 DB）
新方案：failure_indexer.audit_report(execution_id)（纯函数；复用 relay 已落库数据）
工期：2 周 → 0.5 周
```

### §5 A.4 demo 路径重写（与代码契约一致）

```
旧 7 步（错误）：
  extract → imports?dry_run → imports?commit → imports/plan → scripts → executions → SSE

新 11 步（正确）：
  1. extract_bundle --no-narrative --generated-at ... 
  2. POST /imports/aterag?dry_run=true
  3. POST /imports/aterag?dry_run=false&confirm_overwrite=true（spec-only 全 approved，无需签字）
  4. POST /imports/aterag/plan（_apply_review_state overlay 后 0 pending + 0 gaps）
  5. 人工填 FILL_IN 桩（fixture_id + _driver() 实例化 MatDeviceHttpDriver）
  6. POST /api/v1/sequences（yaml_content=plan_yaml）
  7. POST /api/v1/executions（sequence_id + uut_sn）
  8. execution_dispatch → NATS ate.tasks.{exec_id}
  9. edge jetstream_worker → step_executor → MatDeviceHttpDriver → POST /devices/{id}/call → 读数
  10. edge publish ate.status.{exec_id} → execution_status_relay → 落 DB + SSE
  11. SSE → 前端 visible
```

### §5 B.3 流程重定义

```
旧流程：LLM 提案 → /aterag/plan → 解读 plan_gaps
新流程：LLM 提案 → /aterag/plan → 三类分离解读
  - pending 报告：未签字 draft 条件（需人工签字）
  - gap 报告：无判据条件（需补 binding）
  - warnings 报告：planner 警告
```

### §9 时间表调整

```
旧：8~12 周完成 Phase A+B
新：12~16 周完成 Phase 0+A+B（含跨仓协调 buffer）
     Phase 0（1 周）：D14 鉴权 + S5 硬件 + 端点契约验证
     Phase A（6.5~8.5 周）：A.1+A.2+A.3+A.4
     Phase B（7~8 周，与 A 重叠）：B.1+B.2+B.3+B.4+B.5
```

---

## 3. 仍待 Phase 内解决项

| # | 项 | 解决时机 |
|---|---|---|
| 1 | mat-device-control Python 版本降级（>=3.14 → 3.13） | Phase A.2 同期 |
| 2 | ATEStudio `create_app()` 改造范围 | Phase D.1 实施时评估 |
| 3 | matrag-agent 与 MATRag 主仓共享工具的 import 方式 | Phase B.1 立项时决策 |
| 4 | bundle 契约 v1.1 升级触发 | 实际需求出现时 |
| 5 | Pydantic AI 版本锁定 | Phase B.1 立项时（与 MATRag P3-0 同版） |
| 6 | 跨仓观测方案选型（D9 延迟项） | 后续评估 |
| 7 | ATEStudio 条件签字端点存在性验证 | **Phase 0 必做**（B.3 前置） |
| 8 | dev_scripts_agent 与 script_generator.py 边界裁决 | **B.5 立项前必做** |
| 9 | matrag-agent provenance 审计设计（S1） | writing-plans 阶段设计 |
| 10 | RAG drift CI 检查（S7） | writing-plans 阶段 |
| 11 | R16 mat-device-control 正式鉴权 | 生产化阶段（不阻塞 A.2） |

---

## 4. 风险登记更新（R13~R17 已纳入）

| # | 风险 | 决策 |
|---|---|---|
| R13 | matrag-agent 鉴权缺口 | **D14 Phase 0 必做**；service-account + 长期 JWT |
| R14 | dev_agent 草案被红线排除 | **D16**；三类分离 |
| R15 | 真机 driver 实例化桩 | **D15**；edge driver 层适配 |
| R16 | mat-device-control HTTP 端点无鉴权 | **D13 延后**；当前 method 白名单 + 内网 |
| R17 | 两套 driver 体系映射未定义 | **D15**；MatDeviceHttpDriver + mapping.py |

---

## 5. 评审结论

### ✅ 可进入 writing-plans

4 个硬阻断项全部修复：
1. ✅ demo 路径与代码契约一致（11 步可执行）
2. ✅ exec_observer 删除，复用 failure_indexer
3. ✅ service-account 鉴权 Phase 0 必做
4. ✅ dev_agent pending/gap 分离

5 个次要项全部修复（langchain 范围 / 真机适配位置 / 鉴权延后 / D2-C.1 语义 / dev_agent 交互）。

7 项主会话补充项 4 项已修复，3 项留 writing-plans / Phase 内处理。

### 决策符合率：9/9

| # | 用户决策 | 评审处理 |
|---|---|---|
| D8a | langchain 仅 aterag_agent.py | ✅ D12 已采纳 |
| R16 延后 | mat-device-control 鉴权延后 | ✅ D13 已采纳 |
| 其余 7 项 | （用户在上一轮未单独回答） | ✅ 默认应用 oracle 推荐值 |

### 待用户确认的剩余 11 项（§3）

均为 Phase 内实施细节，不阻塞 writing-plans 进入。

### 下一步

- ✅ Spec v2 落盘（`docs/superpowers/specs/2026-10-18-architecture-upgrade-design.md`）
- ✅ 评审 v2 落盘（`data/reports/architecture-review-v2-2026-10-18.md`）
- ⏭️ 进入 **writing-plans** 生成实施计划（如用户批准）