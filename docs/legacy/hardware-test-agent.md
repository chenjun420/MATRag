# 归档：hardware-test-agent（DeerFlow 多 Agent 试验）

> **状态**：已归档（Phase B.5）。原目录 `ATETest/agent/hardware-test-agent/` 已删除；
> 其**脚本生成职责已并入** matrag-agent 的 `dev_scripts_agent`
> （见 `matrag-agent/src/matrag_agent/agents/dev_scripts_agent.py` 与
> `matrag-agent/src/matrag_agent/skills/dev-scripts.skill.md`）。
>
> 本文保留其 **DeerFlow / LangGraph 多 Agent 编排经验**，供后续 agent 工作流设计参考。
> 原工程基于 **DeerFlow 2.0** 框架，是一条"规格书 → 测试 → 部署 → 自愈"的完整试验链路，
> 因与 MATRag（RAG 抽取 / bundle）＋ ATEStudio（DSL 执行 / 仪器）正式链路重复、且技术栈
> 不一致（LangGraph + LangChain，Python 3.12），故整体退役、经验归档。

## 1. 原系统定位

基于 DeerFlow 2.0 的硬件测试自动化多 Agent 系统：从产品规格书自动提取测试项目、生成测试
脚本，并部署到边缘网关节点执行，失败时自动分析修复并迭代重试。

**核心能力**

- 文档解析：解析 PDF / Word 格式产品硬件规格书。
- 测试提取：识别中试转产测试与产线批量测试项目。
- PRD 生成：生成完整产测 PRD 文档。
- 代码生成：自动生成 Python 测试脚本（设备控制 + 测试逻辑，pytest 形式）。
- 自动部署：通过 SSH 将脚本部署到边缘网关。
- 错误修复：分析执行错误并自动修复代码，支持多次迭代重试。

> 注：其脚本产物为 **pytest 脚本 + pyvisa/pyserial 直连**，与 MATRag 体系产物
> （ATEStudio DSL）不同；B.5 起脚本生成改由 `dev_scripts_agent` 产出 **ATEStudio DSL v3.2**。

## 2. 多 Agent 架构（5 Agent 流水线）

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ Parser Agent │→ │ Writer Agent │→ │ Coder Agent  │
│ (文档分析)    │  │ (PRD 生成)    │  │ (代码生成)    │
└──────────────┘  └──────────────┘  └───────┬──────┘
                                            │
┌──────────────┐  ┌──────────────┐         ↓
│ Debugger     │← │ Deployer     │←────────┘
│ (错误修复)    │  │ (部署执行)    │
└──────────────┘  └──────────────┘
```

| Agent | 职责 | 输入 | 输出 |
|---|---|---|---|
| Parser Agent | 从规格书提取测试项目 | PDF/Word 规格书 | 结构化测试项目列表 |
| Writer Agent | 生成产测 PRD | 测试项目列表 | Markdown PRD |
| Coder Agent | 生成 Python 测试脚本 | PRD + 测试项目 | 可执行 Python 脚本 |
| Deployer Agent | 部署脚本到边缘网关并运行 | Python 脚本 | 执行结果 |
| Debugger Agent | 分析错误并修复代码 | 错误日志 + 源码 | 修复后的代码 |

## 3. 工作流（LangGraph 状态机）

使用 `StateGraph` 编排，条件边实现"失败 → 修复 → 重部署"自愈环：

```
1. 解析规格书（parse_spec）      → 提取测试项目
2. 生成 PRD（generate_prd）
3. 生成测试脚本（generate_scripts）
4. 部署并运行（deploy_run）
5. 判定：should_retry
     ├─ success → END
     ├─ retry   → debug_fix →（回边）deploy_run
     └─ fail    → END（重试达上限）
```

- 条件边 `should_retry` 依据执行结果与重试计数（`max_retry_count` / `current_retry`）分流。
- `MemorySaver` 检查点做状态持久化，便于中断续跑。
- 状态对象 `HardwareTestState` 汇集输入（`spec_file_path` / `product_name`）、中间结果
  （`test_items` / `prd_content` / `generated_scripts`）、执行与修复结果、控制参数与
  `messages` / `errors`。

### 状态数据结构（节选）

```python
class TestItem(TypedDict, total=False):
    name: str; category: str; description: str
    parameters: Dict[str, Any]; pass_criteria: str; equipment: str; test_time: float

class GeneratedScript(TypedDict, total=False):
    script_name: str; code: str; dependencies: List[str]; config: Dict[str, Any]

class ExecutionResult(TypedDict):
    success: bool; output: str; error: str; exit_code: int; script_path: str

class FixResult(TypedDict):
    analysis: str; root_cause: str; fix_description: str; fixed_code: str
```

## 4. 多 LLM 后端抽象（Coder / Debugger）

Coder 与 Debugger 各支持 5 种可热切换后端，通过 `CodeBackend` 枚举 + `from_string` 统一：

| 后端 | 说明 | 适用场景 |
|---|---|---|
| LangChain LLM（默认） | 复用 `get_llm()`，任意 LangChain 兼容模型 | 默认方案 |
| OpenAI API | gpt-4-turbo-preview / gpt-4o，支持兼容 `OPENAI_BASE_URL` | 生产 |
| Anthropic Claude | claude-3 系列 | 高质量要求 |
| Z.AI GLM | glm-4（国内访问快） | 国内生产 |
| CodeLlama（本地 Ollama） | codellama:7b/13b/34b | 开发 / 成本敏感 / 隐私 |

**可迁移经验**：后端以枚举 + 字符串解析封装，两个 Agent 可混合使用不同后端（如 Coder 用
商业 API 保质量、Debugger 用本地模型降成本），失败时支持运行时切换后端重试。

## 5. 与 OpenCode 的集成经验

- OpenCode 定位为**开源 AI 编码助手平台**（类 Cursor 开源版），提供 HTTP Server 与 CLI
  两种调用面，并以 `SKILL.md` 定义可复用专业技能（代码规范、设备驱动模板、pytest 夹具模式）。
- 集成实现：`opencode_http_client.py`（HTTP API）、`opencode_cli_client.py`（CLI 流式）、
  `coder_agent_opencode.py`（Agent 侧封装）；`.opencode/skills/hardware-test/SKILL.md` 承载
  领域技能。
- **可迁移经验**：把"领域代码规范"沉淀为 `SKILL.md` 供生成侧复用——这正是 MATRag 体系
  `matrag-agent/src/matrag_agent/skills/*.skill.md` 的前身（如 `dev-scripts.skill.md`）。

## 6. 目录结构（原工程）

```
hardware-test-agent/
├── src/
│   ├── agents/      # parser / writer / coder / deployer / debugger（+ coder_agent_opencode）
│   ├── tools/       # ssh_executor / doc_parser / opencode_* / backend / <llm>_client
│   ├── workflow/    # state.py / builder.py / nodes.py（LangGraph）
│   ├── prompts/     # parser/writer/coder/deployer/debugger .md
│   └── config/      # settings.py / llm_config.py
├── specs/           # 产品规格书输入
├── output/          # prd/ 与 scripts/ 产物
├── tests/           # test_parser / test_coder / test_workflow
├── main.py          # CLI 入口
└── requirements.txt
```

原技术栈：DeerFlow 2.0 / LangGraph / LangChain、PyMuPDF / python-docx、asyncssh、aiohttp、
pydantic-settings、loguru、pytest。

## 7. 退役原因与经验沉淀

**退役原因**

- 与 MATRag（RAG 抽取 → bundle）+ ATEStudio（DSL 执行 / 仪器资源）正式链路职责重叠。
- 技术栈不一致（LangGraph/LangChain + Python 3.12），与 matrag-agent（Pydantic AI + 3.13）
  无法共用工具 / 契约。
- 脚本产物形态不一致（pytest + 直连 VISA vs ATEStudio DSL v3.2）。
- 部署面（SSH 直推边缘网关）不在正式产测链路内。

**保留的多 Agent 经验**

1. **流水线 + 自愈环**：顺序流水线（解析 → 生成 → 执行）叠加"失败 → 修复 → 重试"条件边，
   是脚本生成类任务的有效编排模式；`dev_scripts_agent` 沿用其"确定性流水线 + 失败留痕"
   思路，但工具层由 Pydantic AI Agent + 白名单闸门收敛。
2. **状态化编排**：显式 `State`（TypedDict）+ 检查点是长流程可观测 / 可续跑的基础。
3. **多后端抽象**：模型后端以枚举统一、可运行时切换，是避免供应商锁定的通用做法。
4. **领域技能外置**：把领域规范沉淀为可复用技能文件（`SKILL.md`），供生成侧共享。
5. **产物落盘约定**：PRD 与脚本分目录落盘、依赖清单随产物返回（`GeneratedScript.dependencies`），
   成为 B.5 `DevScriptsAgentResult.dependencies` 设计的先例。

## 附录：原顶层文档（本次一并归档）

`ATETest/agent/` 目录下另存有 HT-A 的顶层文档，本次归档一并移除，仅在此登记：

| 原文件 | 内容摘要 |
|---|---|
| `README_HARDWARE_TEST_AGENT.md` | 项目 README：目录结构、环境依赖、`.env` 配置模板、5 Agent 与工具（`SSHExecutor` / `OpenCodeClient` / `doc_parser` / `script_validator`）源码样例、LangGraph 编排（`builder.py` / `nodes.py` / `state.py`）、`main.py`、`requirements.txt` 与 `langgraph.json`。 |
| `PROJECT_SUMMARY.md` | 项目总结：核心功能、5 Agent 职责、工作流状态管理、扩展能力（新增 Agent / 自定义提示词 / 新设备驱动）、安全考虑（SSH 密钥 / API Key / 沙盒 / 最小权限）、性能优化与未来规划。 |
| `README.TXT` | 空文件（0 字节），无内容。 |

> 上述文档内容均属**实现细节与运行说明**；其**设计要点**（5 Agent 流水线、LangGraph 状态机、
> 多后端抽象、OpenCode 技能集成）已收录于本文正文 §2–§7，故随 HT-A 退役一并移除、不再单独保留。

---

*归档时间：Phase B.5（dev_scripts_agent 落地后）。原始详细文档（README / QUICKSTART /
EXAMPLES / DEPLOYMENT / MULTI_BACKEND_GUIDE / OPENCODE_* 等，含 `ATETest/agent/` 下顶层
`README_HARDWARE_TEST_AGENT.md` / `PROJECT_SUMMARY.md` / `README.TXT`）随原目录一并移除，
仅保留本文。*
