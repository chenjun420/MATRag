# TODO 任务清单

> 规则: 每个任务 = 独立文件边界 + 明确验收; 并行任务不得改公共文件(types/config/llm), 需要改先回报。
> 纪律: "能力声称可用前必须有端到端实测"; CLI 失败要 fail-fast 拒绝假成功。

## P0 解析服务（PDF/MD → 规范化语料）

| ID | 任务 | 文件范围 | 状态 |
|---|---|---|---|
| P0-1 | 规范文档模型契约 types.py（CanonicalDocument/Block/TableModel/TableDraft/ParseReport） | `src/matrag/types.py` | done(main) |
| P0-2 | 解析服务骨架 + provider 注册表 + PDF 几何草稿（PyMuPDF 单元格矩形、合并 span、阅读顺序文本） | `src/matrag/parse/geometry.py`, `provider_local.py` | done(S1) |
| P0-3 | Markdown 读取器（表格/标题/锚点 → 契约） | `src/matrag/parse/markdown_reader.py` | done(S1) |
| P0-4 | LLM 合并单元格重建（去规范化、平铺叶表头、`<br>`、锚点） | `src/matrag/parse/reconstruct.py`, `prompts.py` | done(S2) |
| P0-5 | 对账闸门 verify（维度一致/无 Col2/无凭空字符/merge 统计/report） | `src/matrag/parse/verify.py` | done(S2) |
| P0-6 | parse CLI + 端到端（3 文档 → data/corpus + parse_report.json） | `src/matrag/cli/*`, `scripts/verify_parse.py` | done(main)；3/3 文档过闸门 |
| P0-7 | 解析器评测基准（合并单元格样本集，为后续换外部解析器留裁判） | `tests/fixtures/tables/*`, `src/matrag/parse/bench.py` | done(main)：`scripts/verify_parse --from-corpus` 语料复核 + samples 导出；换外部解析器时用同 corpus 对账 |

## P1 RAG 核心（semantica 主体）

| ID | 任务 | 文件范围 | 状态 |
|---|---|---|---|
| P1-1 | Provider Gateway：LLM 门面 + Embedding 适配（MaaS base_url、维度探针、缓存、模型版本入索引） | `src/matrag/llm.py`(done), `embeddings/*` | done(S4)；补限流退避+最小间隔 |
| P1-2 | 域知识导入（knowledge.md/rules.yaml 已内联 `data/domain/source` → 卡片语料；规则/术语可用 132 条规则/154 术语） | `src/matrag/domain/*`, `data/domain/*` | done(S3)：22 卡片 |
| P1-3 | 确定性推理：rules.yaml(55 公式+77 约束) → semantica 引擎 + 132 自测 | `src/matrag/domain/rules.py`, `data/domain/power/rules/*` | done(S3)：132/132 通过（Datalog+SHACL+安全表达式适配层） |
| P1-4 | 索引层：sqlite-vec 向量 + FTS5(jieba)关键词 + KG 文件 + provenance | `src/matrag/index/*`, `scripts/build_index.py` | done(S4-A)+main：真实索引 1581 chunks/2048d；product_code 过滤（`scope=public` 保通用知识）、AND→OR 召回、文档家具清洗；provenance sqlite 审计真接 |
| P1-5 | 混合检索：vector+keyword+graph 三腿 + RRF + 引用溯源 | `src/matrag/retrieve/*` | done+main：三腿全接线（graph 腿 = KG 实体链接→源卡 chunk）；章节过滤（词典 262 标题）真实生效 |
| P1-6 | gateway 服务：MCP(HTTP) + REST 域工具 | `src/matrag/gateway/*` | done(S4-C)：6 工具+真实 HTTP E2E+鉴权矩阵 |
| P1-7 | 评测集 30~50 题 + 检索质量指标 | `data/eval/*`, `scripts/run_eval.py`, `scripts/mine_eval_qa.py` | done(W3-C)+main：39 题 4 tier；understand/graph 消融（`--no-understand` / `--no-graph`）；结果 JSON 落盘 `data/eval/out` |
| P1-5b | **语义查询理解（LLM）+ 确定性规则推理 + 应答合成 + 审计** | `retrieve/understand.py`, `rule_hint.py`, `retriever.py`, `catalog.py`, `answer.py`, `provenance.py`, `scripts/ask.py` | done(W3-A/B/D)+main：真实闭环实测通过；规则求值→应答接线；`scripts.ask` 审计落档（sqlite） |
| P1-8 | prod profile：PG(pgvector+pg_textsearch) 配置 + sqlite→pgvector 迁移演练 | `config/profiles/*`, `scripts/migrate_store.py` | backlog |
| P1-9 | 知识层接线：术语/公理/本体（SHACL→OWL）/知识图谱产物 + 图谱检索腿 | `src/matrag/domain/*`, `retrieve/graph_index.py`, `scripts/import_domain.py` | done(main)：见「第 4 波实测」 |
| P1-10 | 产测方法层扩域：19→31 方法 × 四字段（参数/仪表/接口/指标）→ 公共知识卡片 + KG `method` 节点（`measures`/`guided_by` 边）+ 图谱腿实体 | `data/domain/source/power_test_methods.yaml`, `src/matrag/domain/test_methods.py`, `knowledge_graph.py`, `retrieve/graph_index.py` | done(main)：见「第 5 波实测」 |

## P2 抽取投影

| ID | 任务 | 文件范围 | 状态 |
|---|---|---|---|
| P2-1 | LLM 结构化抽取 → 本体实例（requirement/condition/limit/scenario，ATEStudio bundle v1.0 形状） | `src/matrag/extract/*` | done(main)：spec 投影 + narrative draft 红线 + 事实护栏 + 多值保护行拆分 + 变体消歧；离线 1326 passed |
| P2-2 | bundle 导出 + contract_hash 自校验（draft 红线） | `src/matrag/extract/bundle.py`, `scripts/extract_bundle.py` | done(main)：契约 hash `59a852c2…` 三向一致（生产侧+消费侧+独立再算），`assert_contract` strict=True fail-fast，`scripts/extract_bundle` CLI 写盘三产品可被 `BundleModel.model_validate_json` 重验 |
| P2-3 | ATEStudio dry-run（`POST /aterag/plan`）验证 | `scripts/verify_bundle_ate.py` | done(main)：三产品 6/6 PASS（contract_hash 三向一致 / plan_nonempty / red_line / no_gaps / step_ids_unique / yaml_parses）；plan YAML 落盘 `data/reports/plans/{code}.plan.yaml`；用 shim 注册 `ate_cloud/.schemas/.services/.config` 包路径绕开消费侧 `__init__`（生产侧源码仍为唯一实现） |

## P3 Agent 服务（Pydantic AI 2.x + MCP-HTTP；原 LangChain 计划已替换）

| ID | 任务 | 文件范围 | 状态 |
|---|---|---|---|
| P3-0a | **Agent 计算路径 v3**：Monty 沙箱执行层（Rust 解释器；契约校验：脚本拒绝/执行失败分类；可插拔 ScriptRunner） | `src/matrag/domain/script_sandbox.py` | done(main)：12 测试；实机冒烟 4 路径（正常/除零/超时/未定义名） |
| P3-0b | Agent 工具循环：原生 tool calling（`derive_with_rules` + `run_python`）；输出数值可追溯校验（不可追溯→纠错重跑→拒答）；启动预检 fail-fast（不支持 tools 即报错，无降级） | `src/matrag/domain/tool_agent.py`, `answer.py`(derive_mode=agent), `scripts/ask.py`(--derive agent) | done(main)：13 测试 + 应答接线 3 测试；真实 LLM 实测（9.0A / 1198.8Wh） |
| P3-0c | SymPy 替换自研表达式反解（保持 planner 函数契约；求解核心 `sympy.solve` + 数值回代验证） | `src/matrag/domain/planner.py` | done(main)：守卫契约不变；新增 5 边界测试；实机复验 9.0A / 1198.8Wh |
| P3-0d | L1/L2/L3 对比评估（基准扩 16 题：规则库外 3 + 边界 4；z3 spike 另行评估） | `scripts/*`, `data/eval/*` | done(main)：三模式 16/16（det 0.47s / llm 1.94s / agent 15.0s 均值）；信任边界 8 项修复（tool_agent 23 例）；z3 spike 结论=不替代 planner（见「第 6 波实测」） |
| P3-1 | agent 服务骨架（HTTP 调用 gateway、决策留痕 semantica provenance；Pydantic AI + MCP-HTTP + Skills 面） | `src/matrag/agent/*` | backlog |
| P3-2 | 产测软件开发 Agent（需求 → ATEStudio YAML v3.2 + Python 脚本） | `src/matrag/agent/dev_agent/*` | backlog |
| P3-3 | 调测 Agent（仿真/真机执行 → 诊断 → 修复循环） | `src/matrag/agent/debug_agent/*` | backlog |

## P4 对接与运维

| ID | 任务 | 文件范围 | 状态 |
|---|---|---|---|
| P4-0 | **板卡部署前置清理闸门**（已执行 2026-10-16，用户裁定不备份直接删）：.25 清 ATERag 5 单元/3 库/13 目录/3 用户 → MATRag dev profile 上板（/opt/matrag，8321，fail-closed）→ 双路 E2E 通过 | `deploy/CLEANUP-RUNBOOK.md`, `deploy/RECORDS-20261016.md`, `scripts/deploy_inventory.sh`, `scripts/deploy_cleanup.sh` | done |
| P4-1 | ATEStudio 端到端（导入向导等价路径） | `scripts/e2e_atestudio.py` | backlog |
| P4-2 | ATETest 接线（mat-device-control / PageIndex MCP） | `src/matrag/adapters/atetest.py` | backlog |
| P4-3 | Explorer 部署（prod：独立进程 + X-API-Key）+ 运维脚本（doctor/backup）；gateway 已上板（dev profile，2026-10-16，见 RECORDS） | `deploy/*` | backlog |

## 波次

- **第 1 波（并行）**: P0-2/P0-3 ✅S1｜P0-4/P0-5 ✅S2｜P1-2/P1-3 ✅S3｜P1-1 ✅S4 → 全完成
- **第 2 波（并行）**: P1-4 索引 ✅｜P1-5 检索 ✅｜P1-6 gateway ✅ → 真实索引 1581 chunks、章节过滤真实生效、gateway HTTP E2E 通过
- **第 3 波（并行）**: W3-A 语义查询理解 ✅｜W3-B 多查询 RRF + 规则提示 ✅｜W3-C 评测集 ✅｜W3-D 带引用答案合成 + gateway `ask` ✅ → **真实闭环实测通过**（见下）
- **第 4 波（知识层接线）**: 本体/知识图谱产物落盘 ✅｜图谱检索腿（实体链接→源卡 chunk）✅｜provenance 审计真接 ✅｜P1-7 评测消融 ✅
- **第 5 波（产测方法层）**: YAML 扩域（19→31 方法 × 四字段）✅｜KG method 节点 + measures/guided_by 边 ✅｜方法卡入公共知识索引 ✅｜评测对比 ✅ → 见「第 5 波实测」
- **第 6 波（知识补全 + 推理链 + Agent 计算路径 v3）**: 单位/术语补全（术语→154，命中 204/219）✅｜多跳推理（K-PWR-126/127、链式求值、planner、query_derive、llm_planner）✅｜Agent 工具循环（Monty 沙箱 + 原生 tools，`--derive agent`）✅｜SymPy 替换自研反解 ✅｜评估扩展（推导 16 题三模式 16/16；z3 spike 结论：不替代 planner）✅ → 见「第 6 波实测」
- **第 7 波（知识面修复, 2026-10-17）**: 要求型表入参数层（参数 219→372，命中 361/372）✅｜术语 163→168（GB 标准源 ×5）✅｜规则可达 50→73、no-term 剥离至设计性豁免 15 ✅｜复审计报告 `data/reports/knowledge-gaps-20261017.md` ✅｜全仓 pytest 1206 passed ✅ → 见「第 7 波实测」
- **第 8 波（遗留缺口关闭, 2026-10-18）**: GDC 跨页截断行名恢复（全库 name 占位符 1→0，命中 361→362/372）✅｜CY t7-0 三件套一致性回填（doc.json/md）✅｜索引重建（1711 chunks、词典 324）+ explorer 产物刷新 ✅｜未匹配 10 条（要求陈述句）最终处置与剩余缺口分类收口 ✅｜复审计报告 `data/reports/knowledge-gaps-20261018.md` ✅ → 见「第 8 波实测」
- **第 9 波（P2 抽取投影完整 + 测试, 2026-10-18）**: spec 投影（CY 24 / GDC 51 / PA601 76 requirements；输入基线 CY {85,220,300 Vac} / GDC {36,48,60 V} / PA601 {88,220,290 Vac}）+ narrative draft 红线 + 事实护栏 ✅｜bundle 导出 contract_hash `59a852c2…` 三向一致 + `assert_contract` strict=True ✅｜ATEStudio dry-run 三产品 6/6 PASS ✅｜离线测试 `test_extract_bundle.py` / `test_extract_fingerprints.py` / `test_extract_patterns.py` / `test_extract_spec_tables.py` / `test_extract_narrative.py` / `test_extract_pipeline.py` / `test_scripts_extract_bundle.py` 共 120 例全绿 ✅｜1326 passed + ruff 全绿 ✅ → 见「第 9 波实测」

## 第 3 波实测（RAG 闭环，真实 LLM + 真实索引）

`python -m scripts.ask "PA601-D54A 输入特性 下的参数"`
- 语义计划: spec_value + 3 条改写检索式; 过滤 product_code + section「输入特性」(正则/词典定, LLM 越权值被拒并留痕)
- 证据: 6 条全部落在 4.3.1 输入特性 表 t0-13; 双腿命中; RRF 融合
- 答案: 数值准确(315Vac / PF 0.85·0.95·0.98 / 45·50-60·66Hz), 源表为"-"处**显式声明"规格书未给出"**, 逐条 [n] 引用到 doc/章节/表

`python -m scripts.ask "输出纹波超标可能是什么原因"`
- 语义计划: fault + 3 个子问题 + 4 条改写; 章节过滤「4.3 输出特性」(LLM 提议, 经产品目录接地校验通过)
- 证据: 5 条 GDC 输出特性(滤波器 300~500~2000µF / 17.6µF 陶瓷 / TDD 500mV vs FDD 50mV / 20MHz 带宽)
- 规则: 5 条确定性规则(K-PWR-101 公式、102/103/104 约束、K-PROT-119) 以"常识参考"身份参与, 与规格判据严格区分
- 判定: **证据不足**明示 + 有引用的候选原因; 未引用证据保留可回溯

## 第 4 波实测（知识层接线，真实数据 + 真实索引）

- **知识层产物**（`python -m scripts.import_domain` → `data/domain/generated/`，确定性可重跑）：
  术语 129 + 公理 76；本体 266 属性 / 70 类 / 130 规则链接（`power_ontology.ttl/.json`，质量门 issue 16 条，非阻断）；
  知识图谱 994 节点 / 2865 边（`power_knowledge_graph.json`，validate 0 issue；参数 219、命中术语 104）。
- **图谱腿**（`retrieve/graph_index.py`）：FORMULA 路由查询的第三腿 = KG 实体链接 → 源知识卡 chunk；
  与 KG 同一套边界规则（ASCII 键词边界、单字 CJK 键剔除，防"电"式伪命中）。
  冒烟：T-RIPPLE→`power-knowledge-8`、T-HOLDUP→8、T-UVLO→11、T-HYSTERESIS→5；spec 类查询无链接（按设计）。
- **审计**（`provenance` sqlite 真接）：`scripts.ask` 每次应答落档（问题→计划→命中→引用→规则→降级）；
  写失败显式留痕（`provenance_unavailable` / `provenance_failed`），不阻断应答。
- **评测消融**（39 题，understand on；on `run-20261012-212623` / off `run-20261011-194047`）：
  hit@1 89.74 vs 84.62、hit@3 92.31 vs 89.74、hit@8 97.44 持平、MRR 91.56 vs 88.13；
  公式题 5/6 持平（未覆盖题 = 源卡映射缺口：期望 card6、T-RIPPLE 源 card8，修数据另立）；
  图谱腿实际仅触发 2/6 公式题（routing kind 未走 FORMULA："保持时间"→spec_value、"欠压"→fault、"纹波"→spec_value）；
  本轮其它层的变化都发生在图谱腿未参与的题目上 → 归因 LLM 理解随机性（同栈历史 must_contain 波动 69~95%），不作单次归因。
  结论：图谱腿接线后**无回退、无单次可测增益**；价值在确定性源卡召回与可解释链接；放大触发面属后续单独立项。

## 第 5 波实测（产测方法层接入 KG + 扩域，2026-10-13）

- **方法扩域**（`data/domain/source/power_test_methods.yaml`）：19→31 方法（新增 12：M-PF/M-INPUT-SLOW/M-OPP/M-REVERSE-IN/M-ORING/M-SHARE/
  M-TELEMETRY/M-TELESIGNAL/M-TELECONTROL/M-COMM/M-POWERCYCLE/M-UVP）；每方法新增四字段 `parameters`（测试条件与参数）/
  `instruments`（所需仪表）/`interfaces`（接口与接线）/`metrics`（测试指标）；质量门 0 issue（10 类别；去重关联术语 72 / 规则 78）。
- **KG 接线**（`knowledge_graph.py`）：`method` 节点 31 + `measures`（method→term）93 边 + `guided_by`（method→rule）96 边 →
  **1025 节点 / 3054 边**（validate 0 issue）；coverage methods 31/31/31；图谱腿实体链接收入 method
  （`M-RIPPLE-NOISE` → `power-method-ripple-noise`）。
- **卡片与索引**：`power_methods.jsonl` 31 张公共知识卡片（`power-method-<slug>`）；重建索引 **1681 chunks**
  （1520 规格书 + 161 卡片）；章节词典 315 标题。
- **真实闭环**：`scripts.ask "输出纹波与噪声测试需要哪些仪器"` → 4 条方法卡证据（条件/仪器/接线/指标）+ 5 条规则判据
  （K-PWR-101/102/103/104/119）；审计落档。
- **评测对比**（39 题，understand on，三腿）：入池前 `run-20261012-212623` hit@1 89.74 / hit@3 92.31 / hit@8 97.44 / MRR 91.56；
  入池后两轮 `run-20261013-184904` / `run-20261013-190920`：hit@1 82.05 / 76.92、hit@3 84.62、hit@8 94.87 / 97.44、
  MRR 85.45 / 83.12、must_contain 74.36。
  归因：方法卡 15/39 题进入 top-8（入池前 0）、约 6~8 题占 top-1（公式题 holdup/kelvin/uvlo、跨文档 ripple/ocp、spec 个别），
  期望卡退至 #2~#4（个别跨文档题单轮跌出 top-8、次轮回归）；两轮间 LLM 理解随机性 ±5pp，不作单次归因。
  属「新公共知识参与竞争」的预期权衡（与「公共知识入池」同类）；不加开关。
- **测试**：1041 passed（新增 `tests/test_domain_test_methods.py` 13 例 + KG/graph_index/卡片测试更新）+ ruff 全绿。

## 第 6 波实测（知识补全 + 推理链 + Agent 计算路径 v3，2026-10-14 ~ 10-15）

- **知识补全**：单位冲突修复（16→0）；术语 129→154（新增 23 + 别名修订 8，10 路 websearch 取证）；
  规格书参数命中术语 104→204/219；本体质量门 issue 0；KG 1054 节点/3331 边（validate 0）。
  「补全不了」清单已交付（12 条产品专有信号电平 + 3 个占位符参数；112 个单规则参数无单位属结构特征）。
- **多跳推理链**：新增 K-PWR-126/127（55 公式+77 约束=132 规则）；evaluate 链式求值（max_depth=3，
  中文紧邻数字扫描修复）；planner 通用反解 + 等价视图；query_derive 问题→推导接线；
  llm_planner（LLM 选计划 + 引擎执行，失败回退，`--derive llm`）；answer 逐跳条目（source/strategy 留痕）。
- **Agent 计算路径 v3**（`--derive agent`，P3-0a/P3-0b）：
  - 沙箱 = pydantic-monty（Rust 解释器进程池；无文件/网络/环境变量；时间/内存限额强制；stdout 收集不污染进程）；
  - 工具循环 = Pydantic AI 原生 tool calling（`derive_with_rules` + `run_python`；数值必须原样来自工具结果，
    不可追溯 → 附纠错反馈重跑，仍失败 → 拒答；模型不支持 tools → 预检拒启动，**无降级路径**）；
  - 真实 LLM 实测（qwen3.8-flash，温度 0）：规则内题 **9.0A**（K-PWR-126→127，2 次调用 3.5s）；
    规则库外题 **1198.8Wh**（K-PWR-127 反解 599.4W + 沙箱脚本 ×2h，3~4 次调用 5.5~7.9s）；两次运行一致；
  - 平台特性（README 已记录）：thinking 模式拒绝 `tool_choice=required/object`（结构化输出必须）→
    Agent 模型固定 `enable_thinking=false`；真实模型会把 dict 参数双重编码成 JSON 字符串 → 工具层容忍并归一化。
- **求解核心（P3-0c）**：自研 AST 反解已替换为 **SymPy `sympy.solve`**（惰性导入）+ **数值回代验证**
  （相对容差 1e-6）；保守契约不变（目标出现一次、不得在幂指数、白名单结构、± 对称对取正根、
  其余多解拒绝；奇次幂负结果按实根求解）；新增 5 个边界测试；真实验证：9.0A / 1198.8Wh。
- **推理评估（P3-0d）**：推导基准 16 题（规则内 7 + 边界 6 + 脚本 3），三模式 **全部 16/16**：
  deterministic 均值 0.47s/题；llm 1.94s/题、8 次调用；agent 15.0s/题、35 次调用（12 题给值、4 题如实 None，
  其中 2 次经 UsageLimitExceeded 安全网拒答）；产物 `data/eval/out/derive-20261015-231745.json`。
  信任边界 8 项修复：facts 仅复述 grounded 值；run_python 对拍门（规则链不可达/数值无定义/与规则不一致 → 拒采信）；
  inputs 规则参数须与 grounded 一致；提示词硬规则（禁造值、禁特判无定义）；归属门（改名估算拦截）；纠错反馈回路扩展。
  新增 `tests/test_domain_tool_agent.py` 9 例（共 23）、`tests/test_scripts_derive_eval.py` 4 例。
- **z3 spike（结论：不替代 planner）**：公式规则覆盖 47/55 可编码；全库同时断言路线不可行——
  ①共享别名过绑定（`vac_rms` 与 `rectified_bus_voltage` 同绑，与 K-PWR-117 √2 规则冲突 → Q04/Q07 unsat）；
  ②等价别名未归一 → 效率题多解；③除零在 z3 为全函数、无「无定义」语义。与 planner 一致仅 5/8 数值题。
  脚本/产物：`scripts/z3_spike.py`、`data/eval/out/z3-spike-20261015-232741.txt`（z3-solver 仅 spike 使用）。
- **测试**：1156 passed + ruff 全绿（P3-0d 新增信任边界/评估集契约/G1-G2 回归等用例）。

## P0-6 端到端实测记录（2026-10-11）

- `python -m matrag.cli parse` 三文档全跑通：
  - PA601-D54A（md）：33 张表，**全部通过对账闸门**，exit 0
  - CY0177-75V-20A（pdf）：14 张表，5 clean，8 张闸门失败
  - GDC800T5054-UC（pdf）：39 张表，26 clean，13 张闸门失败
- 失败根因核验：21/21 全部为 `ERROR_LLM_NOT_CONFIGURED`（未配 MaaS 凭据的预期 fail-closed），无其他根因
- **切换 qwen3.8-max 后实测**：LLM 可达（ping 2-3s、中等 JSON 3-5s），**大表重建提示词 121s+/次**（模型生成慢，非不可达）→ `MATRAG_LLM_TIMEOUT=300`，后台跑全量；embedding（ark doubao）**实测 2048 维**（探针价值兑现，禁写死维度）
- **下一步**：CY0177/GDC 后台重建完成后，`python -m scripts.verify_parse <doc> --show 2` 人眼复核合并单元格质量（CY0177 第 9 页保护表、GDC 第 12 页输出特性表）

## P0 最终验收（2026-10-11，全部通过）

- **CY0177-75V-20A.pdf：14/14 表过闸门**（76 处合并单元格）
- **GDC800T5054-UC.pdf：39/39 表过闸门**
- 关键调优：
  1. `enable_thinking=false`（qwen3.8-max 实测 121s→10s；`thinking_level=low` 反而 250s，勿用）
  2. 提示词增补：无表头表格（标签-值型）禁 Col1/2 占位、按列语义自拟唯一表头；表头唯一性硬性；整行横向合并只填首列其余 `-`
  3. 对账口径修正：`MISSING_MERGE` → `WARN_MERGE_INCOMPLETE`（审计痕迹不进 unresolved）；`INVENTED_NUM` 只查数据行（表头唯一性限定词的数字不是内容主张）
- 语料产物：`data/corpus/*.md` + `*.tables.json`；复核入口 `python -m scripts.verify_parse <doc> --from-corpus`

## 部署闸门（不可绕过）

- 板卡部署（P4 任何 prod 动作）前必须完成 `deploy/CLEANUP-RUNBOOK.md`：停 ATERag 服务（.25）→ 数据去留决策（默认 dump 备份）→ 删库删目录 → 验证端口/进程/库全释放。未完成不放行。
- **2026-10-16 已执行完成**（.25 全量清理；用户裁定不备份直接删）→ 已部署 MATRag gateway（dev profile）。执行记录: `deploy/RECORDS-20261016.md`。

## 第 7 波实测（知识面修复，2026-10-17）

- **要求型表入参数层**：`spec_params` 扩展支持"编号/项目/要求/等级"型要求表（`requirement` 列（同字段多列按「；」拼接）、信号名称→`sub_name`、表级 `scope` 兜底锚定）；CY 表4/表5、PA601 表13/14/15/22/24–26/27–29、GDC 表50 等 153 行新入参数层，参数总量 219→372。
- **匹配层**：`_match_param` 新增 scope 兜底（遥信/遥控/遥测功能等表级概念；name→sub_name→scope 顺序）；单字符键（A/R）只允许精确匹配。
- **术语补录 163→168**：T-VDIP（GB/T 17626.11）、T-SURGE（GB/T 17626.5）、T-ENVTEST（GB/T 2423 系列）、T-IP（GB/T 4208）、T-ROHS（GB/T 26572）；另有 T-HIPOT/T-EFT/T-EFFICIENCY/T-CREEPAGE/T-AVAIL/T-ARRHENIUS 等别名补齐；terms.yaml 规则挂接 8 处。
- **数据修复**：CY0177 t7-0 两条 PDF 合并格丢失行名（`-`→`效率`），修改前已备份语料。
- **复审计**（对照第 6 波基线）：规格书参数命中术语 204/219→361/372（97.0%）；未匹配 15→11（余项均为无术语锚定的要求陈述句）；本体属性有规格证据 82→104；规则可达 50→73；no-term 规则 18→15（全为设计性豁免：K-CMN-001 SI 词头换算 + K-FIX 夹具规则 ×14）；公理不可达 44→38；KG 1055 节点/3337 边 →1222/4059；validate 0 issue；产物新鲜度核验一致。
- **测试**：1206 passed + ruff 全绿；缺口复审计报告 `data/reports/knowledge-gaps-20261017.md`（旧报告 `knowledge-gaps-20261016.md` 保留）。

## 第 8 波实测（遗留缺口关闭，2026-10-18）

- **GDC 跨页截断行名恢复**：全库唯一 name 占位符（GDC t14-0 行 0，`'-'`）经 PDF 溯源（page13→14「输出电压延迟时间」组的 N10V 续行）回填为「输出电压延迟时间」→ 精确命中 `T-STARTUPTIME`；参数命中 361→362/372（97.3%），name 占位符 1→0，全占位值行 184 为要求型表设计特征（保留）。
- **CY t7-0 三件套一致性**：第 7 波仅修 `tables.json`；本波回填 `.doc.json`（行 7/8 首格）与 `.md` 两行 → 「效率」，检索文本与抽取层一致。
- **未匹配 10 条最终处置**：全部为 PA601 整机「要求陈述句」（t0-21×4 / t0-30×1 / t0-32×5）；决定不新增「要求型术语」锚（公共知识不绑型号 + 无参数形态 + 文本已在 requirement 字段承载），列明细于报告附录。
- **产物刷新**：`import_domain.py` → KG 1222/4062（validate 0）；`build_index.py` → 1711 chunks、2048 维、词典 324 标题；explorer 产物 1222/4062。
- **剩余缺口分类收口**（非缺陷，随数据面演进）：属性无证据 163（孤儿 0；fixture 簇 44 为工装数据面、规则产生 34 被消费 130）；不可达规则 59（11 无术语设计性豁免 + 48 术语未落地、13 有方法引导）；无实例术语 85/168（接口通信类为主）；公理 4 条无规则（A-SERIES/A-PARALLEL/A-TEMPCOEFF/A-MOSCOND）+ 34 条无可达规则（级联）；方法断点 3（M-KELVIN/M-OPP/M-INDICATOR）。
- **测试**：断言/数字更新（含新增 pin：CY「效率」×2→T-EFFICIENCY、GDC「输出电压延迟时间」→T-STARTUPTIME、placeholder==0）；1206 passed + ruff 全绿；报告 `data/reports/knowledge-gaps-20261018.md`（`knowledge-gaps-20261017.md` 保留对照）。

## 第 10 波架构升级（落地状态，2026-10-18）

执行架构升级方案（spec v2 + 实施计划），共 **≥21 条并行 lane** 全部落地。详见 `data/reports/architecture-upgrade-completion-2026-10-18.md`。

### 已完成 lane（按阶段）

| 阶段 | 内容 |
|---|---|
| Phase A | A.1 契约同步 / A.2 真链 driver 双向（ATEStudio + mat-device-control）/ A.3 audit_report / A.4 真链 + 仿真 demo |
| Phase B | B.1 matrag-agent 骨架 / B.2 Pydantic AI 替换 / B.3 dev_agent / B.4 debug_agent / B.5 dev_scripts_agent + HT-A 归档 |
| Phase D | D.1 ATEStudio 解耦 / D.3 cond_fingerprint 贯通 / D.12 langchain 收尾 |
| 修复链路 | R1 信封 / R2 forwarder / R1.5 execution-level 分级 / G2 gateway text |
| 数据缺口 | GAP-01 负轨 / GAP-09 outputs 写回 VariableSpace |

### 5 仓测试基线

| 仓 | 起 | 当前 | delta |
|---|---|---|---|
| MATRag | 1326 | **1390** | +64 |
| ATEStudio | 3467 | **3605** | +138（含单步路径集成测试 +9） |
| mat-device-control | 234 | **269** | +35 |
| matrag-agent | 0 | **128+1 skip** | 全基线 |
| **合计** | 5027 | **5392** | **+365** |

### 决策矩阵 D1~D16（落地状态）

详见 spec §0 + final report。

### 已知 backlog

| 项 | 严重 | 状态 |
|---|---|---|
| OpenHTF step_executor variable_space 回写 | 中 | ✅ 完成（OpenHTFStepExecutor.__init__ 增 variable_space + execute 收尾调 _record_step_outputs） |
| 单步执行路径 (ScannerScheduler → ProcessStepExecutor) | 高 | ✅ 完成（PlanBootstrapper._collect_step_execution_specs 在 jetstream_worker.py:343；tests/integration/test_plan_bootstrap_step_specs.py 9 例） |
| Agent 服务（P3-1/P3-2/P3-3 → 已转外仓 matrag-agent/） | 高 | ✅ 完成于外仓 matrag-agent/（B.1 骨架 + B.2 Pydantic AI + B.3 dev_agent + B.4 debug_agent + B.5 dev_scripts_agent + HT-A 归档）|
| namespace M-* ↔ en300132_* 映射表 | 低 | A.1 已 WARN 化（#2 检查）；领域知识 backlog |
| 真机 demo (.25 板 + PSU + DMM + service-account) | 中 | 🟡 用户/硬件未就位 |
| B.4.5 live 评测（30 样本 / top-3 ≥70%）| 低 | 已延后 |



P2 主线完整落地：spec 投影 → narrative 提案 → bundle 导出 → 消费侧 dry-run → 离线测试全覆盖。

**spec 投影（`build_from_tables`）**：三产品实测
| 产品 | requirements | excluded | review | baseline_input_voltage |
|------|-------------:|---------:|-------:|------------------------|
| CY0177-75V-20A | 24 | 17 | 9 | 85~300 Vac, typ 220 |
| GDC800T5054-UC | 51 | 12 | 43 | 36~60 V (typ 48) |
| PA601-D54A | 76 | 128 | 16 | 88~290 Vac, typ 220 |

代表性 pin：PA601 `SR-PA601-D54A-1201-m-54.8`、`SR-PA601-D54A-1308-c2`（变体消歧后缀）；「输入防反功能」→ excluded matched_word=「不要求」；「开机输出延迟」→ review kind=measure_no_value。measure 类 kind（output_voltage/output_current/output_power/ripple/efficiency/timing）一律带值（min/typ/max 至少其一）才物化为 requirement。

**narrative 提案（`extract_narrative`）**：LLM off 走整体跳过（`stats.narrative_skipped='llm_not_configured'`），LLM on 时强制红线：
- clause.status='draft' / source='narrative' / confidence='llm'（生产侧绝不代签）；
- 数值未按字面见于原文 → 弃值（非 measure）/ 弃整条（measure），落 review；
- method_ref 不在 `load_test_methods()` → 清空 + review；
- kind/side ∉ KNOWN_KIND_SIDES → 弃 + review；
- 同 (kind,side,value,method_ref) → stats.duplicates++；
- 需求编号 NAR-{product}-{seq:03d}，按源块分组。

**bundle 导出 + 契约**：
- `contract_hash = 59a852c26ca277e4141c6a60ae723b20`（生产侧 pydantic 2.14.0/py3.13.6 与 ATEStudio 侧 pydantic 2.13.4/py3.12.11 独立计算，逐位一致）；
- `assert_contract(bundle, strict=True)` 在 build_bundle 内 fail-fast；
- `bundle_fingerprint` 忽略 generated_at / bundle_fingerprint 自身 → 幂等；
- LLM off + 固定 generated_at 三产品指纹前缀 pin：CY `294e27ffa749` / GDC `60446cf26cae` / PA601 `78155d7063c1`；
- `scripts/extract_bundle.py` CLI：`--corpus`/`--out`/`--product`/`--no-narrative`/`--generated-at`/`--json`；产物可被 `BundleModel.model_validate_json` 重验（extra=forbid + 版本号 1.0 + 字段名/类型逐字一致）。

**消费侧 dry-run（`scripts/verify_bundle_ate.py`）**：三产品 6/6 PASS
- contract_hash 三向一致（生产侧写盘 / 消费侧加载 / 独立再算）；
- plan_nonempty / no_gaps（measure 类条件都有判据）/ red_line（draft 不入执行 + pending 数一致）/ step_ids_unique；
- yaml_parses 用 `YamlParser.parse(out_path)` 镜像 `verify_e2e.py:174-200` 验收口径：`plan.to_yaml()`→写盘→`parse(out)`→`len(parsed.steps) == plan.emitted_step_count`；
- 用 shim 注册 `ate_cloud/.schemas/.services/.config` 包路径绕开消费侧 `__init__` 拉起 nats/fastapi 等（生产侧源码仍为唯一实现来源）。

**离线测试**（共 120 例，全绿）：
- `tests/test_extract_bundle.py`（17 例）：contract_hash pin / extra=forbid / 版本号校验 / 场景 seq 唯一 / assert_contract 行为（strict True/False）；
- `tests/test_extract_fingerprints.py`（7 例）：cond_fingerprint 覆盖 kind/side/value/method_ref 不含 text；None vs {} 归一；bundle_fingerprint 忽略 generated_at/bundle_fingerprint；file_fingerprint 字节级稳定；
- `tests/test_extract_patterns.py`（30 例）：match_kind 顺序敏感（输出电压延迟→timing / 输出电流检测精度→output_current 当前行为 / 派生指标 VIL/VIH/调整率 → None）；rail_of / scenario_bindings / note_input_conditions / baseline_input_voltage 关键分支；build_value 单位换算（timing us→ms / ripple mV→mVpp）；split_multi_value 三点拆分；MEASURE_KINDS / TIMING_KEYS 冻结；
- `tests/test_extract_spec_tables.py`（22 例）：三产品计数 pin；PA601 m54.8/c2 变体；measure 类 kind 必带值参数化校验；三产品 baseline_input_voltage pin；输入防反功能 excluded；开机输出延迟 review measure_no_value；
- `tests/test_extract_narrative.py`（22 例）：select_blocks（in_scope/length/dedupe/truncate）；stub LLM（kind 词表外 / 数值不在原文 / measure 无值整条弃 / method_ref 目录外清空 / cond_fingerprint 去重 / LLM 调用失败 review / 坏响应 review）；build_requirements 分组+编号；extract_narrative 一步式；词表一致性；
- `tests/test_extract_pipeline.py`（14 例）：iter_products 配对校验；三产品 build_all 排序；contract_hash 三向一致 + assert_contract 不抛；指纹幂等 + 长度 32 + LLM off 前缀 pin；narrative_skipped 留痕；spec_only 计数匹配；use_narrative=False；空目录/缺 .tables.json → ExtractIncompleteError；
- `tests/test_scripts_extract_bundle.py`（4 例）：CLI 写盘三份 bundle 可重验；--product 单产品；--product NOPE → SystemExit 非 0；--json stdout 合法 JSON 含 products 数组。

**遗留风险**（非阻塞）：
1. `tests/test_extract_pipeline.py::test_build_bundle_use_narrative_false_skips_layer` 中 `b.requirements[0].spec_requirement_id` 仅断言非空，不校验具体格式（`_fit(first.code, 100)`），如 spec_params 出现空 code 字段需另立测试；
2. `scripts/extract_bundle.py` 缺 `--no-write` dry-run 开关（仅 `--json` 可行），产测 Agent 接线时如需纯 dry-run 校验可加；
3. ATEStudio `__init__.py` 拉起 nats/fastapi 的问题在 verify_bundle_ate 中靠包路径 shim 绕开，但仍是耦合债 — ATEStudio 拆分 `__init__.py` 为「接口聚合 + 服务工厂」后可根除；
4. `.24` 板卡已被 `.25` 替代，部署面收敛到 `.25` 一块板。

## 第 11 波（端到端缺口闭环 + PA601 23-TC 全计划，2026-10-19）

- **方法层闭环**：`power_test_methods.yaml` 35 条（新增 M-STARTUP-TIME / M-OVERSHOOT / M-CROSS-REG；rules/measures 全引用既有词表 id：K-PWR-109/110/111、K-PROT-118、T-CROSSREG；`validate_test_methods()` 0 issue）。
- **alias v4**：`method_ref_aliases.yaml` schema 修复（`version` + `aliases` dict）+ 25 条 mappings / 38 reverse keys；新 ref id：`rohde_schwarz_startup_app_note` / `pmbus_1_3_spec` / `burn_in_aging_method`。
- **catalog 接线**：tek-mso46、keysight-dsox3024t（STARTUP-TIME/OVERSHOOT）、chroma-63206、hp-34461a（CROSS-REG）；`import_domain.py` 重生成 jsonl 35 行 / KG 1225 节点 / 4074 边。
- **e2e 虚拟**：`scripts/e2e_pa601_virtual.py` TC-16 @54V 2.222%≤5%、TC-12 @54V 0.593%≤5%；33 步 31 PASS/2 FAIL（既有 SR-PA601-D54A-1200/1201 spec 边界，未动）；报告 `data/reports/e2e-pa601-virtual-2026-10-19.md`。
- **上板计划**：ATEStudio `data/commission/pa601_full_commission.yaml` 23 步（22 脚本 TBD）+ `docs/commissioning/pa601-full-plan.md` + 7 例结构测试（本波新增）。
- **缺口评估**：`data/reports/end-to-end-gap-assessment-2026-10-19.md` 矩阵行 / §6 闭环记录更新；「无 M-OTP/M-BURN-IN」旧结论更正（方法本体在册，缺温箱/DAQ catalog 条目 + 真机路径）。
- **核验**：MATRag 全量 1439 passed / 0 failed（run3 + run4 两次 junitxml）；ATEStudio 全量 3612 passed / 113 skipped（含本波新增 7 例结构测试）；ruff（src tests scripts）无 E/F；早前 flaky `test_cli_unknown_product_exits_nonzero` 单跑/全量均不复现。
- **基线**：methods=35 / aliases=25（reverse 38）/ catalog=28 / terms=168 / rules=132。
- **未决**：真机 demo（.25 板 + PSU/DMM）待用户/硬件；OTP/BURN-IN 真机路径需温箱/DAQ；23 步计划 22 个脚本 TBD。

## 第 12 波（虚拟设备 + e2e 修复 + 23-TC 虚拟执行，2026-10-19）

- **mat-device-control 虚拟仪器驱动包（9 型 · MAT.Virtual\*）**：`modules/device/drivers/virtual/{virtual_devices.py,__init__.py}` 注册 PSU/DMM/ELoad/Scope/ACSource/PowerMeter/Safety/Thermo/Aging 到 `DriverFactory`，`ConnectionType` 增 `VIRTUAL = "virtual"`；公开方法 `set_voltage/measure_voltage`、`set_cc`、各 `measure_*`/`capture_*`/`run_*` 冻结签名；`*IDN?` 返回 `MAT,Virtual<X>,SN-V001,1.0`。`tests/test_virtual_drivers.py` 9 型参数化 + DeviceController 分发路径 + 未注册型号 `ValueError` + 未知方法 `AttributeError`。MDC 全仓 **303 passed**；`connection_type="virtual"` + `[[devices]] allowed_methods=[…]` 已贯通 MDC HTTP 路由的 method 校验（基础集 ∪ 配置 ∪ env `MAT_DEVICE_METHOD_ALLOWLIST`）。
- **MATRag e2e 负轨判据/模型修复**：`scripts/e2e_pa601_virtual.py` `_judge` 当 `min`/`max` 皆非空且 `min>max` 时归一化交换 lo/hi；`simulate_step.output_voltage` 负轨方向修正（max-only `*1.05 if mx<0`、min-only `*0.95 if mn<0`、droop 阈值用 `abs(nominal)`）；报告 §3b 计数行改 "33 PASS / 0 FAIL"。`SR-PA601-D54A-1200-m-54` 现 `-51.249 ≥ -54.0` ✅，`SR-PA601-D54A-1201-m-54.8` 现 `-53.892 ∈ [-54.8, -53.2]` ✅。新增 `tests/test_e2e_pa601_virtual.py` 5 例（归一化、边界、负轨实测）。MATRag 全量 pytest **1444 passed**（基线 1439 + 5）；ruff `src tests scripts` 无 E/F。
- **ATEStudio 23 脚本补齐（22 TBD → 脚本齐）+ overlay 外置**：`data/commission/scripts/commission_*.py` 23 个脚本，每脚本自含 `_driver()`（MatDeviceHttpDriver，`device_id`/`base_url` 来自 params/env）、`_judge(measured, value)`（包含归一化 + `params["force_measured"]` 故障注入）、成功后置 `result_measured_value / result_passed / result_cond_fingerprint / result_detail`，越限 `raise AssertionError`；执行体放 `if __name__ == "__main__":`（importlib 加载不触发真实设备调用）。`data/commission/pa601_virtual_limits.yaml` 覆盖 tc05(max 500ms)/tc06(max 60A)/tc08(70..90V)/tc21(max 5s)/tc22(max 125C) 占位步，余步沿用 plan 自带限值；不改 `pa601_full_commission.yaml` 本体。
- **23-TC 虚拟执行 runner**：`scripts/commission_full_virtual.py`（live 默认 / `--inject TC=VALUE` 注入证明判据+中止链路 / `--check` 静态自检 三模式）。temp workdir 生成 `config.toml` + `devices.toml`（9 台 MAT.Virtual*，`allowed_methods` 与冻结方法表一致），启动 MDC uvicorn + 等待 `/health`，通过 `YamlParser → V32PlanDispatcher(simulation=False, script_executor=ProcessExecutor.execute_async)` 执行 23 步，断言 23/23 PASS + 每步含 `result_measured_value`，写报告 `data/reports/pa601-23tc-virtual-<date>.md`。**独立复跑 live 23/23 PASS**（随机端口）、`--inject tc06=100.0` 触发 FAIL + fail-fast 中止、退出码 1。`tests/unit/commission/test_commission_virtual_scripts.py` 25 例（脚本齐/可编译/与 plan 一一对应、overlay 覆盖、importlib 加载脚本断言 `_judge` 归一化）。ATEStudio `ruff src tests scripts` 无 E/F。
- **三处文档同步**：`data/reports/end-to-end-gap-assessment-2026-10-19.md` §5 状态列更新、§7 新增本波闭环记录 + 残留缺口 4 条；本文 TODO.md（本节）；`ATEStudio/docs/commissioning/pa601-full-plan.md` 补「虚拟执行」章节 + 解除「22 脚本 TBD」宣称。
- **核验**（独立复跑，避免依赖子代理报告）：
  - `python scripts/e2e_pa601_virtual.py` → **33 PASS / 0 FAIL**，退出码 0（`audit_report` 不再含 `first_failure`）；
  - `python scripts/commission_full_virtual.py` → **23/23 PASS**，退出码 0（基线 URL `http://127.0.0.1:<port>`）；
  - MATRag 全量 `pytest -q` → **1444 passed**；ATEStudio 涉及目录 `pytest` → **≥1841 passed / 61 skipped**（无回归）；MDC 全量 `pytest -q` → **303 passed**；三仓 ruff 均无 E/F。
- **残留**：唯一硬缺口仍为**硬件**（温箱 / DAQ 真机 catalog 条目）与**真板上板执行**。bundle 源数据 `1201-m-54.8 value.min/value.max` 反排属数据质量问题，本轮已在判据/仿真层归一化，建议后续在 `extract_bundle` 端加符号校验。MDC API 方法白名单三处（基础集 / devices.toml `allowed_methods` / env）已覆盖本波冻结方法族；上板新增方法按同三处追加即可，路由核心未动。
