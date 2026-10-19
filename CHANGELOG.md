# MATRag CHANGELOG

## v2026.10.18 — 架构升级主阶段全部完成（19+ lane）

> 主线 1326 → 1390 passed；spec/plan 已 FINAL；走查审计完成。
> 详见 `data/reports/architecture-upgrade-completion-2026-10-18.md`。

### 重大改动

- **数据链路贯通（7 跳 + R1 信封）**：bundle.cond_fingerprint → PlanStep → emit.ordered_limits → result_measured_value → executor → VariableSpace → forwarder → relay → audit_report
- **Pydantic AI 替换 langchain**（aterag_agent.py 单文件；其余 4 文件已 D12 收尾）
- **GAP-01 负轨 min/max 归一**：ascending_limits 守负轨区间 swap
- **GAP-09 outputs 写回 VariableSpace**：ThreadStepExecutor._record_step_outputs
- **D.3 cond_fingerprint 贯通**：flow_planner + emit + forwarder + executor 4 节点
- **OpenHTFStepExecutor variable_space 回写**：execute 收尾调 _record_step_outputs
- **unique_steps dedup 键加 cond_fingerprint**：防丢归因
- **PlanBootstrapper 单步路径接线**：`_collect_step_execution_specs` 在 `jetstream_worker.py:343`（ATEStudio 仓）
- **ATEStudio 解耦**：`ate_cloud/__init__.py` PEP 562 惰性导出
- **Agent 服务转外仓 matrag-agent/**：dev_agent / debug_agent / dev_scripts_agent（HT-A 归档）
- **跨仓契约同步门 check_vocab**：FAIL/WARN/INFO 三级；DRIVER_TABLE 死 action 升级 WARN
- **CI 工作流**：`.github/workflows/ci.yml` + `.github/workflows/contract-sync.yml`

### 优化与清理（本会话增量）

- semantica_adapter.py: `pass` 残留改 `logger.debug(..., exc_info=True)`
- planner.py / llm_planner.py: `except: return None` 加 `logger.warning` 留痕
- retrieve/{graph_index,routing,rule_hint,retriever}.py: 暴露公开别名（`contains_hit`、`TABLE_ID_SHAPE`、`resolve_tokenizer`）
- tools/contract_sync/check_vocab.py: #3 拆为 #3a FAIL（缺失阻断）+ #3b WARN（未引用警告）
- tests/test_cli.py: 新增 7 例离线单元测试（parse/doctor + main dispatcher）
- tests/test_neg_rail_extraction.py: 保留为定向 GAP-01 套件（docstring 强化；非冗余）
- pyproject.toml: `extend-exclude` + `[tool.ruff.lint.per-file-ignores]` for `scripts/demo_e2e_simulation.py`

### Backlog（领域知识 / 硬件 / 数据采集限制）

- namespace M-* ↔ en300132_* 实际映射（提供 data/domain/source/method_ref_aliases.yaml stub）
- 实机 .25 板 demo（PSU/DMM 物理 + service-account + D14 鉴权）
- B.4.5 live 评测（30 样本 / top-3 ≥70%）
- e2e NATS 测试默认 skip（依赖真实 NATS）

### 文档

- spec: 状态「已评审通过 v2」
- plan: 状态「FINAL」
- 完成报告: §10 走查审计与未决优化点（2026-10-18 合并三 lane）
- 决策矩阵 D1~D16 全部落地
