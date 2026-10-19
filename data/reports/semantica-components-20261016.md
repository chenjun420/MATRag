# semantica 0.7.0 组件评审与引入建议（2026-10-16）

> 范围：semantica 全部功能组件（CLI 命令组 + Python 模块）。
> 环境：本机 dev 与板卡（192.168.5.25）均为 semantica 0.7.0；pyproject extras：`vectorstore-sqlite` / `shacl` / `parse-pdf`。
> 集成形态：**库调用为主**（检索/嵌入/推理/本体/审计），**独立进程**部署 explorer 可视化（`matrag-explorer.service`）。

## 一、已采用（现状）

| # | 组件 | MATRag 落点 | 说明 |
|---|---|---|---|
| 1 | `vector_store.SearchRanker` | `src/matrag/retrieve/retriever.py` | vector+keyword 双路 RRF 融合 |
| 2 | `embeddings.registry`（method_registry） | `src/matrag/embeddings/semantica_adapter.py` | 注册 `matrag-maas` 嵌入方法（doubao 2048 维） |
| 3 | `reasoning.datalog_reasoner`（BodyAtom/DatalogRule） | `src/matrag/domain/rules_loader.py` | 确定性前推；规则求值入应答与审计 |
| 4 | `ontology` + SHACL | `src/matrag/domain/ontology.py` | 本体 TTL/JSON 落盘 + issue 校验（0 issue） |
| 5 | `provenance`（Manager/SQLiteStorage/compute_checksum） | `src/matrag/provenance.py` | 审计轨：CLI + gateway 双路落档；verify / verify_chain |
| 6 | `context.ContextGraph` | `scripts/explorer_serve.py` | explorer 装载（kind→node_type 映射适配） |
| 7 | `explorer` | `scripts/explorer_serve.py` + 板卡 `matrag-explorer.service` | `0.0.0.0:8899`，X-API-Key，隔离依赖 `.tools/explorer-deps` |

用法速记：

```
# 本机（自动备好隔离依赖 .tools/explorer-deps，不动 venv）
.venv\Scripts\python.exe -m scripts.explorer_serve                # 127.0.0.1:8899
.venv\Scripts\python.exe -m scripts.explorer_serve --allow-anonymous
# 板卡
systemctl status matrag-explorer
curl -H "X-API-Key: <见 /opt/matrag/.env SEMANTICA_API_KEY>" http://192.168.5.25:8899/api/health
```

验证记录：本机与板卡 explorer 均实测通过（health/nodes 200，节点 type 映射生效）；
gateway `ask` 审计 E2E 通过（板卡 `answer:3d4954db…`，provenance 链 17 条 valid、0 断链）。

## 二、全组件清单与结论

### Data Ingestion（数据摄取）

| 组件 | 功能 | 结论 |
|---|---|---|
| `ingest` | 文件/URL/库/流入图 | 不采用——语料固定（3 份规格书），自研 parse→corpus 已验证 |
| `watch` | 目录监听自动摄取 | 不采用——无实时摄取场景 |
| `parse` | 文档→结构化输出 | 不采用——自研 parse 层（md/tables/recon 对账）贴合抽检口径 |
| `split` | 可配置切块 | 不采用——自研章节+表格感知切块已入索引 |
| `normalize` | 确定性文本/日期归一 | 观望——未来跨规格书参数比对可借力 |

### Intelligence（智能）

| 组件 | 功能 | 结论 |
|---|---|---|
| `extract` | NER/关系/三元组/事件抽取 | 不采用——自研 `spec_params` 面向规格书表格；通用抽取替换成本高 |
| `deduplicate` | 实体去重/消解 | 观望——术语规模扩大后可用其算法产出"疑似重复"提示（本轮合并为人工规则） |
| `reason` | 推理引擎 + 结论解释 | 已用（库路径）；CLI 未用（图库在 MATRag 侧） |
| `decision` | 决策记录/因果追踪 | 观望——产测 Agent（P3）阶段评估；当前审计由 provenance 承担 |
| `temporal` | 点时刻查询/区间代数 | 观望——KG 已支持 valid_from/valid_until；产品版本演化需求出现时引入 |

### Knowledge Graph（图谱）

| 组件 | 功能 | 结论 |
|---|---|---|
| `kg` | 图构建/中心度/社区/路径/校验 | 部分采用（explorer 内部用其分析器）；建议低成本脚本产出枢纽/孤点报告辅助缺口闭环（可选） |

### Analytics（分析）

| 组件 | 功能 | 结论 |
|---|---|---|
| `embed` | 嵌入生成/索引/检索 | 不采用——自研 index（sqlite-vec + jieba/BM25 + MaaS 嵌入）已满足；避免双轨 |
| `validate` | SHACL/冲突检测 | 已用（本体 issue 校验） |
| `ontology` | OWL/SHACL/SKOS | 已用（TTL/JSON 导出 + 校验） |
| `provenance` | 血缘/审计/PROV-O 导出 | 已用（写入+校验）；**可选优化**：PROV-O 导出做审计交付物 |

### Export & Viz（导出与可视化）

| 组件 | 功能 | 结论 |
|---|---|---|
| `export` | 14 种格式导出 | 观望——TTL 已有；需要 JSON-LD/GraphML 等交付时启用 |
| `visualize` | kg/ontology 渲染（HTML/SVG/PNG/PDF） | 不采用——渲染源为 semantica 自身存储；我们的 KG 以 explorer 展现（已上板） |

### Infrastructure（基础设施）

| 组件 | 功能 | 结论 |
|---|---|---|
| `store` | 存储后端管理（graph/vector/triplet） | 观望——dev=sqlite；P1-8 上 PG 时评估其 postgres/pgvector 后端 |
| `backup` | 全量备份/恢复 | 不采用——数据面自管（sqlite+jsonl），tar 备份更直接（运维脚本 P4 排期） |
| `pipeline` | 管线 | 不采用（官方标注 foundation placeholder） |
| `config` | 配置管理 | 不采用——配置统一走 matrag settings/.env |

### Services（服务）

| 组件 | 功能 | 结论 |
|---|---|---|
| `server` | REST API（:8000） | 不采用——对外只有 MCP gateway（含鉴权/工具面） |
| `explorer` | 知识探索 UI | **已采用**（本波部署：独立进程 + X-API-Key） |
| `mcp` | semantica 自带 MCP server | 观望——若 Agent 需要图谱原语（neighborhood/path/distance-matrix）可对比暴露 |

### Tools（工具）

| 组件 | 功能 | 结论 |
|---|---|---|
| `doctor` | 组件健康检查 | **建议纳入**运维巡检（低成本） |
| `init` / `shell` / `completion` / `changelog` / `info` | 向导/REPL/补全/变更日志/信息 | 不采用——开发体验类 |

## 三、优化建议（按优先级）

1. **doctor 巡检**：部署/巡检脚本追加 `semantica doctor` 输出归档（零改动成本）。
2. **kg 洞察（可选）**：用 `semantica.kg`（CentralityCalculator / CommunityDetector / PathFinder）产出一份图谱洞察报告（枢纽节点、孤点、桥接），辅助知识缺口闭环。
3. **PROV-O 审计导出（可选）**：把审计链导出为 W3C PROV-O 交付物。
4. **graph-embeddings（按需）**：explorer 的 Node2Vec/语义邻域分析当前不可用（缺 gensim，仅警告，非致命）；如需引入 `semantica[graph-embeddings]`（新增依赖，按约定先上报统一加）。
5. **PG 预留**：P1-8 Postgres 迁移时优先评估 semantica `store` 的后端管理面。

## 四、不引入原则

- 不引入与自研 index / parse / spec_params 重复的双轨组件（embed / extract / parse / split）；
- 对外服务面只保留 MCP gateway + explorer（只读可视化），不叠加 server / pipeline / mcp 第二套 API。
