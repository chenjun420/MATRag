# matrag

电子产品**产测 RAG** + 产测软件开发/调测 Agent 服务组。核心引擎 [semantica](https://github.com/semantica-agi/semantica)。

## 定位

```
用户侧: ATEStudio / Agent 前端 / MATStudio          运维: CLI（CLI 不做用户面）
              │ REST / MCP(HTTP, X-API-Key)
gateway ── matrag.answer（带引用应答 + 规则求值 + 审计留痕）
              │ retrieve: understand(LLM 语义计划+接地) → 混合检索(vector+BM25+KG, RRF) → 规则提示(确定性)
              │ parse:   PDF/MD → 几何草稿 → LLM 合并单元格重建 → 对账闸门(fail-closed)
              │ domain:  领域知识(卡片/术语/公理/本体/知识图谱/产测方法) + 132 条规则(Datalog/SHACL/安全表达式)
              │ audit:   provenance(sqlite) —— 问题→计划→命中→引用→规则→降级 全链留痕
              │ Provider Gateway（LLM/Embedding 可替换）  Storage（sqlite-vec dev / PG prod）
```

* 用户交互在 ATEStudio/Agent 前端；MATRag 是后端服务（**只用 MCP over HTTP，无 stdio 部署形态**）。
* 本地 `sqlite-vec + FTS5`（profile `dev`）；正式部署 PG（profile `prod`，`semantica store migrate` 官方直通）。
* 每个能力都是"接口 + 适配器"：解析器 provider、LLM/Embedding provider、存储后端、关键词腿均可替换。

## 当前实测事实（本机实测，非声称）

| 项 | 实测值 |
|---|---|
| 文档解析 | 3/3 文档通过对账闸门（PA601 33 表 / CY0177 14 表含 76 处合并单元格 / GDC 39 表） |
| 索引 | 1711 chunks（3 份规格书 1520 + 知识/方法卡片 191），向量腿 doubao **2048 维**（探针实测，非写死），关键词腿 jieba+FTS5+BM25 |
| 检索 | 三腿 RRF（vector+keyword+graph）；公共知识 `scope=public` 不绑型号；章节名/章节号过滤（词典 324 标题，数据驱动）；页脚家具不入索引 |
| 知识层 | 术语 168（规格书参数补全 28 + 通信/信号接口补录 5 + 要求型表补录 5）+ 公理 76 + 产测方法 32（参数/仪表/接口/指标四字段）+ 本体 267 属性/70 类（TTL+JSON 落盘，issue 0）+ 知识图谱 1222 节点/4062 边（validate 0 issue；规格书参数命中术语 362/372，要求型表已入参数层） |
| 确定性推理 | 132/132 规则验证通过（DatalogReasoner + SHACL + 白名单表达式求值）；规则求值接入应答层；反解 = SymPy `sympy.solve` + 数值回代验证（惰性导入，保守守卫） |
| 审计 | provenance sqlite 真接：每次应答落档（问题→计划→命中→引用→规则→降级），answer_id 可回查/校验 |
| 评测 | 39 题 4 tier（understand on，三腿）：hit@1 82.05 / hit@3 84.62 / hit@8 94.87 / MRR 85.45（方法卡入池后 run-20261013-184904；对照与波动见「设计权衡」） |
| 推理评估 | 推导基准 16 题（规则内 + 规则外/边界/脚本）：det / llm / agent 三模式均 16/16；agent 12 题给值、4 题如实 None（含 2 次安全网拒答） |
| 全仓测试 | **1390 passed** + ruff 全绿（含第 10 波架构升级新增：契约同步 / gateway text 字段 / 负轨归一） |

### 设计权衡：公共知识（`scope=public`）

30 张域知识卡片与 32 张产测方法卡片是**通用经验**而非产品资产，统一放 `scope=public`（不伪造型号）：

- **收益**：型号过滤不误杀通用经验（问 PA601 也能同时引用规格与 `power-knowledge-8` 的通用判据）；
  该动作落地时的对照实测：hit@8 持平（97.44）、section_hit@8 74.36→76.92、must_contain 71.79→76.92。
- **代价**：hit@1 / hit@3 略降（87.18→84.62 / 97.44→94.87）——故障与跨文档类问题里公共知识卡
  有时排在型号规格之前；spec 类 hit@1 保持 100%，正确答案仍全部在 top3。
- **方法卡入池后**（`run-20261013-184904` / `run-20261013-190920` 两轮）：hit@1 89.74→82.05 / 76.92、hit@3 92.31→84.62、
  hit@8 97.44→94.87 / 97.44、MRR 91.56→85.45 / 83.12——方法卡在 15/39 题进入 top-8（入池前 0）、约 6~8 题占 top-1
  （公式题 holdup / kelvin / uvlo 等：方法卡同时承载公式与判据，属相关新知识），期望卡退至 #2~#4（个别跨文档题单轮跌出、次轮回归）；
  两轮间 LLM 理解随机性 ±5pp，不作单次归因。同属「通用知识一等公民」的预期代价。
- **决定**：不加开关（保持架构简洁），在此记录权衡。

## RAG 闭环用法

```bash
# 0) 域知识产物（知识卡片 + 产测方法卡片 + 规则副本 + Datalog + 本体 TTL/JSON + 知识图谱 JSON）
.venv\Scripts\python.exe -m scripts.import_domain

# 1) 解析（PDF/MD → 规范化语料 + 对账报告；不过闸门不进索引）
.venv\Scripts\python.exe -m matrag.cli parse docs\CY0177-75V-20A.pdf
.venv\Scripts\python.exe -m scripts.verify_parse CY0177-75V-20A --from-corpus   # 语料复核

# 2) 建索引（切分 → embedding → 双腿；写章节词典与产品目录）
.venv\Scripts\python.exe -m scripts.build_index

# 3) 检索/应答（语义理解 → 混合检索(含图谱腿) → 规则/求值 → 带引用答案 + 审计落档）
.venv\Scripts\python.exe -m scripts.ask "PA601-D54A 输入特性 下的参数"
.venv\Scripts\python.exe -m scripts.ask "输出纹波超标可能是什么原因" --no-llm
# Agent 工具循环（原生 tool calling：规则工具 + Monty 沙箱脚本；须模型支持 tools）
.venv\Scripts\python.exe -m scripts.ask "输入功率540W、效率90%、输出电压54V，输出电流是多少" --derive agent

# 4) 评测（默认三腿 + 语义理解；--no-graph / --no-understand 做消融对照）
.venv\Scripts\python.exe -m scripts.run_eval
```

## 开发

```bash
uv sync --extra dev            # 首次（Python 3.13；并行开发期禁用 uv run）
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.venv\Scripts\python.exe -m ruff check src tests scripts
```

* 任务清单与验收: [TODO.md](TODO.md)
* 板卡部署（已执行）: [deploy/RECORDS-20261016.md](deploy/RECORDS-20261016.md)（旧栈清理 + MATRag 上板 + 双路 E2E）；流程清单: [deploy/CLEANUP-RUNBOOK.md](deploy/CLEANUP-RUNBOOK.md)
* 代码约定: [AGENTS.md](AGENTS.md)

## 已知外部约束（实测）

* MaaS `qwen3.8-max`：表格重建类重提示词 ~120s/次 → `MATRAG_LLM_TIMEOUT=300` + `MATRAG_LLM_ENABLE_THINKING=false`（121s→10s；`thinking_level=low` 反而 250s，勿用）。
* MaaS qwen（Agent 路径）：**thinking 模式拒绝 `tool_choice=required/object`**（结构化输出必须）→ Agent 模型固定 `extra_body={"enable_thinking": false}` + `temperature=0`（见 `tool_agent._build_default_model`；实机验证）。
* Ark embedding 有严格限流 → 客户端带最小间隔 + 指数退避（429 不再中断建索引）。