# 电源产品产测知识缺口评估（matrag）

- 生成日期：2026-10-16
- 评估对象：`F:\Workspace\MATRag`（规格书驱动 RAG "matrag"，无 git 仓库）
- 数据面：`data/domain/source/`（术语 155 / 公理 76 / 规则 132 / 产测方法 31）+ `data/corpus/`（CY0177-75V-20A、GDC800T5054-UC、PA601-D54A 三份真实规格书解析产物）+ `data/domain/generated/`（知识卡片 / 本体 / 知识图谱 / Datalog）
- 评估方式：全部为**只读**分析；用仓库 `.venv` 直接调用 `matrag.domain.*` 现算，重放官方导入流水线到临时目录核对；未修改 `src/`、`tests/`、`data/store/index.db`
- 核心口径：术语/公理/规则为域知识层，参数/标准为产品层；覆盖率由 `KnowledgeGraph.coverage()` 计算

## 结论摘要

1. 上轮基线**无漂移**：参数 219 / 命中术语 204 / 未匹配 15 / 占位符 3 / 有本体属性 176；本体属性 267 / 有规格书证据 82（185 条无证据）；方法 31（术语、规则均 31/31）；`rules_reachable_from_products = 50`。逐项与上轮已知数字一致，且 `data/domain/generated/` 产物与当前代码重放结果逐字段一致（无陈旧）。
2. 最大的系统性缺口是**规格书"要求型表格"没有进入参数层**（`classify_table` 只认"名称 + 最小/典型/最大"数值表）：安规（耐压/绝缘电阻/接触电流/接地阻抗）、EMC 判据、遥信/遥控/遥测功能、可靠性/环境/包装、机械尺寸等表格被整表跳过。这直接造成术语、属性、规则、方法四层在 safety/emc/telemetry 方向上"集体失证据"。
3. 规则可达率低（50/132）不是数据坏，而是链路断：82 条不可达 = **16 条无任何术语关联** + **66 条术语有挂接但该术语在 3 份规格书里没有任何实例**；其中 32 条仍被产测方法引用（`guided_by`），属于"方法需要、产品侧落不了地"。
4. 4 个安规类方法（M-HIPOT / M-INSULATION-RES / M-TOUCH-CURRENT / M-KELVIN）的规则**全部**不可达、measures 术语也**全部**无规格书实例；另有 4 个方法（M-GROUND-BOND / M-TEMP-RISE / M-OPP / M-TELECONTROL）measures 全无实例。这是产测问答最直接的检索风险点。
5. 公理层：76 条中 44 条无法从产品侧经规则链抵达（40 条关联规则全不可达 + 4 条无规则关联），方法引用的 78 条规则中 39 条没有任何公理背书，7 个方法经规则反查不到任何公理。

---

## a) 覆盖基线（本次实测）

### a.1 数据规模与质量门（全部 0 issue）

| 层 | 规模 | 质量门 |
|----|------|--------|
| 术语 `power_terms.yaml` | 155 条（general 44 / output 23 / protection 16 / input 12 / communication 10 / safety 9 / emc 8 / dynamics 8 / environment 7 / reliability 6 / thermal 6 / efficiency 3 / standards 2 / mechanical 1），有属性 133、有规则 129，别名 300 | `validate_terms` 0 issue |
| 公理 `power_axioms.yaml` | 76 条（axiom 12 / theorem 25 / formula 39），变量 242，全部有属性、72 条关联规则 | `validate_axioms` 0 issue |
| 规则 `power_rules.yaml` | 132 条（公式 55 / 约束 77），Datalog 子句 132 | 本体构建 0 issue |
| 产测方法 `power_test_methods.yaml` | 31 条（10 类），measures 边 72、rules 边 78 | `validate_test_methods` 0 issue |
| 规格书参数（3 产品） | 219 条（CY0177 39 / GDC 99 / PA601 81），flags 10（multi_value 6 / unparsed 4） | 抽取 fail-fast，无静默跳表 |
| 标准引用 | 92 条 | — |
| 知识图谱 | 节点 1055 / 边 3337 | `KnowledgeGraph.validate()` 0 issue（无悬空边/缺属性/缺规则/缺类） |
| 知识卡片 | `knowledge_cards.jsonl` 22 张 + `power_methods.jsonl` 31 张 | — |

自动测试佐证：`tests/test_domain_knowledge_graph.py` 等 5 个域测试文件共 177 项全部通过（含 `test_real_coverage_numbers` 对上述数字的硬断言）。

### a.2 覆盖率实测（与已知基线对照）

| 指标 | 本次实测 | 已知基线（上轮） | 差异 |
|------|----------|------------------|------|
| `params_total` | 219 | 219 | 一致 |
| `params_with_term` | 204 | 204 | 一致 |
| `params_with_property` | 176 | 176 | 一致 |
| `params_placeholder`（name 为空/占位符） | 3 | 3 | 一致 |
| `params_unmatched` | 15 | 15 | 一致 |
| `unmatched_by_category` | input 2 / other 12 / output 1 | 同 | 一致 |
| 未匹配产品分布 | CY0177 2 / GDC 13 / PA601 0 | （未给） | 新增：PA601 零缺口 |
| `properties_total` | 267 | 267 | 一致 |
| `properties_with_spec_evidence` | 82 | 82 | 一致 |
| `properties_without_spec_evidence` | 185 | 185 | 一致 |
| `rules_reachable_from_products` | 50 | 50 | 一致 |
| `methods_total / with_term / with_rule` | 31 / 31 / 31 | 31 / 31 / 31 | 一致 |
| 方法 measures 全落地的方法数 | 4 | （未给） | 新增：27 个方法至少 1 个 measures 无实例 |
| 方法规则全可达的方法数 | 14 | （未给） | 新增：4 个方法规则全不可达 |
| 术语有规格书实例数 | 59 / 155（96 条无实例） | （未给） | 新增 |
| 公理产品侧可达数 | 32 / 76（44 条不可达） | （未给） | 新增 |

> 注：历史探针 `scripts/coverage_check.py`（REQUIRED_TERMS ~90 项 + REQUIRED_AXIOMS 40 个 id）**在本仓库及现有检索范围内已不存在**，本轮无法复核该清单，详见文末"未复核项"。本文以 `KnowledgeGraph.coverage()` + 现算交叉统计替代，口径与产物逐字段对齐。

### a.3 产物新鲜度

- 用当前代码重放 `scripts/import_domain.py --out <临时目录>`，得到的 coverage 与 `data/domain/generated/power_knowledge_graph.json` 中的 coverage **逐字段一致**（`stale_fields` 为空），节点 1055 / 边 3337 一致 → 产物未陈旧，可放心作为分析依据。

---

## b) 缺口清单

### b.0 根因总览

| 根因 | 说明 | 波及 |
|------|------|------|
| R1 参数抽取只认"数值表" | `spec_params.classify_table` 要求表头同时有"名称"列 + 至少 2 个 min/typ/max 列；"编号/项目/要求/等级"型要求表与"试验项目/等级/判据"型判据表被整表跳过 | 安规、EMC、可靠性/环境、遥信遥测遥控、机械尺寸等章节不进参数层 → 术语/属性/规则/方法在 safety/emc/telemetry 上失去规格书证据 |
| R2 表格合并单元格/续行导致 name 丢失 | 解析产物中 name 列为 `-`，实际含义已并入相邻行 | 3 条占位符未匹配参数（疑为效率行、上升时间行） |
| R3 术语表缺"信号级"词汇 | GDC 4.7 其他特性描述 N10V_FAULT / PWR_I2C_RESET / REMOTE_POWER_OFF 的电压电流高低电平，术语表无任何对应信号术语 | 12 条未匹配参数；M-TELESIGNAL/M-TELECONTROL/M-COMM 相关规则（K-PROT-110/113/114、K-CTL-102、K-TLM-*）不可达 |
| R4 规则与术语/方法图缺表 | 16 条规则既无术语也无方法关联（多为 fixture 工装内部规则）；39/78 方法规则无公理背书 | 规则不可达 82 条中的一部分属"设计如此"，需要在数据中显式豁免而不是当作缺陷 |
| R5 工程内部量被混入"属性缺证据" | 185 条无规格书证据的属性里，37 条是规则派生量、71 条是公式/工装内部变量；它们本就不应要求规格书行 | 影响"真缺口"识别，需要豁免清单 |

### b.1 术语未匹配参数（15 条，全部为真实缺口）

产品分布：**CY0177 2 条、GDC800T5054-UC 13 条、PA601-D54A 0 条**；章节分布：3.1 电源输入规格 2 条、4.3 输出特性 1 条、4.7 其他特性 12 条。

- 3 条 name 为 `-` 的占位符行（= `coverage.params_placeholder` 的 3 条，全部未匹配）：
  - `param:CY0177-75V-20A|t7-0|7`（值 91，备注"220Vac/50Hz 输入，48V 输出，满载"）→ 疑为 48V 支路效率行；
  - `param:CY0177-75V-20A|t7-0|8`（值 92，备注"60V 输出，满载"）→ 疑为 60V 支路效率行；
  - `param:GDC800T5054-UC|t14-0|0`（支路 N10V，最大 200ms）→ 疑为"输出电压上升时间"行。
  - 影响：表1 输入规格 8 行中 2 行、输出特性首行无法检索/推理；效率相关规则（K-PWR-121 等）与 M-EFFICIENCY 在 CY0177 上没有数值锚点。
- 12 条 GDC 4.7 其他特性信号电平行（完整明细见附录 A）：`N10V_FAULT电压/电流 ×（低电平/高电平）`、`PWR_I2C_RESET电压/电流 ×（低/高电平）`、`REMOTE_POWER_OFF电压/电流 ×（低/高电平）`。
  - 影响：这是"产测判据"的核心数据（如 PWR_I2C_RESET 高电平有效、持续时间 >700ms、接地电阻 10kΩ；REMOTE_POWER_OFF 低电平 <0.8V、重启前后时序 150ms/200ms），当前既无术语挂接、也无规则直接消费；M-TELESIGNAL（8 条规则、6 条不可达）与 M-TELECONTROL（5 条规则、3 条不可达）只能靠卡片文本兜底。
  - 附带数据质量注：解析结果中出现 `高电平 min=8.0 max=6.0`（min>max）之类的行列错位，建议在补术语时一并复核该表解析。

另外与"值为 '-'"相关的对照数据（非缺口）：全参数中 **32 条三个取值列全为占位符/空**（CY0177 7 / PA601 25），但其中 **32/32 都靠参数名命中了术语**（是"功能/要求"行，如"输入防反功能、接地方式、散热方式"），属正常形态；只有上述 3 条 name 占位符是真问题。

15 条完整明细（含参数 id、值、章节、判定说明）见**附录 A**。

### b.2 属性无规格书证据（185 条，按"是否真缺口"分簇）

口径：`properties_without_spec_evidence` = 本体 267 属性中没有任何 `param → instance_of` 落地的属性。按关联画像分为三簇（完整清单见附录 B）：

| 簇 | 条数 | 判定 | 说明 |
|----|------|------|------|
| A. 规则派生量（有 `produces` 边） | 37 | **可豁免（计算量）** | 由公式规则算出而非规格书直读：`input_power`、`loss`、`duty_cycle`、`derated_output_current`、`rectified_bus_voltage`、`reactive_power`、`required_*`、`rss_tolerance` 等；任务是"从已有量推导"，不应要求出现于规格书行 |
| B. 术语已挂但术语无实例 | 104 | **混合**：其中 safety/telemetry/derating/power 类是真缺口；fixture/measurement/accuracy 类多为工装量 | 如 `insulation_resistance`、`ground_resistance`、`test_voltage_v`、`hold_up_time`、`p_in`、`p_out`、`i_leak`、`sample_rate_hz`、`esr`、`sat_current` |
| C. 无术语关联且非派生 | 71 | **多为内部量/公式变量（部分可豁免）** | 如 `p_in`、`v_out`、`v_nominal`、`v_low_line`、`freq`、`exponent`、`total_resistance`、`used_count`、`margin`；另一部分是工装/制造量（`can_inject_*`、`fixture_resistance`、`pitch`、`diameter`、`throughput`） |

任务点名的内部量实测：

| 属性 | 簇 | terms / rules / axioms | 判定 |
|------|----|------------------------|------|
| `input_power` | A | T-INPWR；被 K-PWR-003 派生、被 K-PWR-001/002/126 消费 | 可豁免（派生量） |
| `p_in` | C | 无术语；被 K-PWR-118 消费；被 A-ACINPUTPOWER / A-EFFICIENCY / A-LOSS / A-PWRBALANCE 作为公式变量 | 可豁免（公式变量），建议补 `alias_of: input_power` 之类的等价标注 |
| `p_out` | B（术语 T-OPP） | 被 K-PWR-118 消费；被 A-EFFICIENCY / A-LOSS / A-PWRBALANCE 使用 | 可部分豁免，但 T-OPP（输出过功率保护）挂到 `p_out` 语义偏弱，建议复核链接 |

按本体类别分布（185 条，多类别重复计）：fixture 48、control 19、safety 17、protection 14、measurement 10、telemetry 10、power 9、derating 9、accuracy 7、transient 7、current-sharing 5、ripple 4、efficiency 4、calibration 3、ac 3、units 3、limits 3、transformer 3、regulation 3、reliability 2、coverage 2、thermal 1、ac-dc 1、electrical 1。

"规格书可观测口径"（类别落在规格书域且非纯工程域）的候选真缺口共 **89 条**，其中优先级最高的代表：
- **safety**：`insulation_resistance`（T-INSULRES）、`ground_resistance`（T-GNDCONT）、`measured_touch_current_ma` / `touch_current_limit_ma`（T-TOUCHCURR）、`test_voltage_v` / `insulation_type`（T-HIPOT）、`i_leak` / `max_y_capacitance`（T-YCAP）→ 规格书**有**对应要求（PA601 表22 安规测试要求：绝缘电压 4000Vdc/2500Vdc/500Vdc、绝缘电阻 2MΩ、接触电流 <2.5mA/<0.5mA、接地阻抗 <0.1Ω；CY0177 表4 耐压与绝缘电阻：3000/1500/500Vac、≥50MΩ@500Vdc），但被 R1 整表跳过 → **真缺口，可修**。
- **transient**：`hold_up_time`（T-HOLDUP）。
- **derating**：`derated_capacity_a`、`line_derated_power_w`、`p_line_derated`。
- **telemetry/communication**：`sample_rate_hz`、`measured_telemetry_error_pct`、`alarm_latency_limit_ms`、`debounce_ms`（PA601 遥测表有"输入电压精度 ±3%、输出电压精度 ±3%"等要求，被 R1 跳过）。
- **efficiency/power**：`esr`、`sat_current`、`peak_current`、`noise_pp`。

### b.3 规则不可达（82 / 132）

可达定义：`product → has_parameter → param → denotes → term → related_rule → rule`，即"规则关联的术语至少被一份规格书参数命中"。当前可达 50 条。82 条不可达按原因分层：

| 子类 | 条数 | 说明 |
|------|------|------|
| 无任何术语关联 | 16 | 规则在"术语-规则"图上完全孤立；其中 8 条被方法引用（`K-CTL-102`、`K-FIX-005`、`K-FIX-018`、`K-FIX-019`、`K-PROT-110`、`K-PWR-119` 等） |
| 有术语但术语无规格书实例 | 66 | fixture 13、safety 11、control 6、power 5、protection 5、telemetry 5、electrical 4、measurement 4、derating 3、accuracy 2、calibration 2、其余 6 |
| （其中）仍被产测方法 `guided_by` 引用 | 32 | "方法需要、产品侧落不了地"——补证据优先级最高 |

按类型：derive 38 / constraint 44。按类别前几名：fixture 23、safety 11、control 7、protection 7、power 6、telemetry 5。

影响说明：
- safety 11 条不可达直接对应 M-HIPOT / M-INSULATION-RES / M-TOUCH-CURRENT / M-GROUND-BOND 的规则链断裂（见 b.4）；
- fixture 23 条 + 无术语的 fixture 规则多为工装/产线工程规则，建议在规则数据中加 `scope: fixture`/豁免标注，避免与产品规则混在一起拉低可达率；
- telemetry/control 5+7 条对应 GDC PMBus 命令表与 PA601 遥信/遥控表未入参数层（R1+R3）。

### 不可达规则 82 条 = 16 条无术语关联 + 66 条术语无规格书实例

**16 条无任何术语关联（规则图侧完全孤立）：**

- `rule:K-CMN-001`（derive/units）方法: -; consumes=['exponent', 'value']; produces=['scaled']
- `rule:K-CTL-102`（constraint/control）方法: M-TELESIGNAL; consumes=['isolation_level', 'main_isolation_level']; produces=['violation']
- `rule:K-FIX-005`（constraint/fixture）方法: M-KELVIN; consumes=['contact_resistance']; produces=['violation']
- `rule:K-FIX-008`（constraint/fixture）方法: -; consumes=['diameter', 'spacing']; produces=['violation']
- `rule:K-FIX-011`（constraint/fixture）方法: -; consumes=['current_rating', 'min_centers']; produces=['violation']
- `rule:K-FIX-012`（derive/fixture）方法: -; consumes=['diameter', 'pitch', 'stagger']; produces=['effective_clearance']
- `rule:K-FIX-018`（constraint/fixture）方法: M-KELVIN; consumes=['contact_resistance', 'contact_resistance_limit', 'contact_resistance_margin']; produces=['violation']
- `rule:K-FIX-019`（derive/fixture）方法: M-TRANSIENT; consumes=['dut_response_time']; produces=['max_load_risetime']
- `rule:K-FIX-020`（derive/fixture）方法: -; consumes=['dut_rated_current', 'load_current_rating']; produces=['load_current_margin']
- `rule:K-FIX-021`（derive/fixture）方法: -; consumes=['can_inject_open', 'can_inject_short_inter_channel', 'can_inject_short_to_rail']; produces=['fault_coverage_complete']
- `rule:K-FIX-022`（derive/fixture）方法: -; consumes=['default_state']; produces=['default_path_continuous']
- `rule:K-FIX-023`（derive/fixture）方法: -; consumes=['injected_fault_current', 'switch_current_rating']; produces=['switch_current_margin']
- `rule:K-PROT-110`（constraint/protection）方法: M-TELESIGNAL, M-UVLO; consumes=['alarm', 'turn_off_threshold']; produces=['violation']
- `rule:K-PROT-112`（constraint/protection）方法: -; consumes=['fail_safe_clamp', 'open_circuit_detected']; produces=['violation']
- `rule:K-PWR-119`（constraint/efficiency）方法: M-EFFICIENCY, M-PF; consumes=['judgement_margin_pct', 'meter_accuracy_pct']; produces=['violation']
- `rule:K-PWR-125`（constraint/power）方法: -; consumes=['rail_power_budget_tolerance_w', 'rail_power_deviation_w']; produces=['violation']

**66 条有术语但术语无规格书实例（按类别分组，规则 id）：**

- **fixture**（13）: `K-FIX-001`, `K-FIX-002`, `K-FIX-003`, `K-FIX-004`, `K-FIX-006`, `K-FIX-007`, `K-FIX-009`, `K-FIX-010`, `K-FIX-013`, `K-FIX-014`, `K-FIX-015`, `K-FIX-016`, `K-FIX-017`
- **safety**（11）: `K-SAF-001`, `K-SAF-003`, `K-SAF-101`, `K-SAF-102`, `K-SAF-103`, `K-SAF-104`, `K-SAF-105`, `K-SAF-106`, `K-SAF-107`, `K-SAF-108`, `K-SAF-109`
- **control**（6）: `K-CTL-103`, `K-CTL-104`, `K-LOOP-002`, `K-LOOP-003`, `K-TLM-107`, `K-TLM-109`
- **power**（5）: `K-PWR-002`, `K-PWR-004`, `K-PWR-005`, `K-PWR-124`, `K-PWR-126`
- **protection**（5）: `K-PROT-105`, `K-PROT-113`, `K-PROT-114`, `K-PROT-118`, `K-PROT-119`
- **telemetry**（5）: `K-TLM-102`, `K-TLM-103`, `K-TLM-104`, `K-TLM-110`, `K-TLM-111`
- **electrical**（4）: `K-ELEC-002`, `K-ELEC-003`, `K-ELEC-004`, `K-ELEC-005`
- **measurement**（4）: `K-MSR-101`, `K-MSR-102`, `K-MSR-103`, `K-MSR-104`
- **derating**（3）: `K-CAL-103`, `K-PWR-122`, `K-PWR-123`
- **accuracy**（2）: `K-MSR-001`, `K-MSR-002`
- **calibration**（2）: `K-CAL-101`, `K-CAL-102`
- **ac**（1）: `K-AC-003`
- **ac-dc**（1）: `K-PWR-117`
- **coverage**（1）: `K-COV-001`
- **efficiency**（1）: `K-PWR-121`
- **transformer**（1）: `K-PWR-105`
- **transient**（1）: `K-PWR-108`

### b.4 方法闭环（31 个逐条）

闭环口径：`method → measures → term ← denotes ← param`（术语落地）+ `method → guided_by → rule ← related_rule ← term`（规则可达）。31 个方法**全部**有 measures 与 rules 挂接（31/31/31），但只有 **4 个**完全闭环（M-OVP、M-INPUT-SLOW、M-REVERSE-IN、M-ORING），27 个存在至少一处断点：

| 缺陷模式 | 方法数 | 清单 |
|----------|--------|------|
| measures 术语全部无规格书实例 | 8 | M-HIPOT、M-INSULATION-RES、M-GROUND-BOND、M-TOUCH-CURRENT、M-KELVIN、M-TEMP-RISE、M-OPP、M-TELECONTROL |
| rules 全部不可达 | 4 | M-HIPOT、M-INSULATION-RES、M-TOUCH-CURRENT、M-KELVIN |
| rules 全部可达（=14） | 14 | 其余方法中规则侧无断点 |
| 经规则反查不到任何公理 | 7 | M-GROUND-BOND、M-TRANSIENT、M-STARTUP、M-REVERSE-IN、M-ORING、M-TELESIGNAL、M-COMM |

最严重的 4 个（安规/接触电阻方向）：M-HIPOT（3/3 measures 无实例、3/3 规则不可达）、M-INSULATION-RES（1/1、2/2）、M-TOUCH-CURRENT（2/2、2/2）、M-KELVIN（3/3、4/4）。这 4 个方法的检索结果目前只能依赖方法卡片正文与规则文本，无法通过"产品参数实例"落点，问答时"某产品实际要求是多少"必然答不上来（规格书本身有要求，见 b.2）。

逐条明细（`measures` 总数 / 其中无实例数；`rules` 总数 / 其中不可达数）：

| 方法 | 名称 | measures | 其中术语无规格书实例 | rules | 其中规则不可达 | 间接术语→规则 |
|------|------|----------|----------------------|-------|----------------|----------------|
| `M-RIPPLE-NOISE` ⚠ | 输出纹波与噪声测试 | 6 | 3 | 4 | 0 | 7 |
| `M-HOLDUP` ⚠ | 掉电保持时间测试 | 3 | 2 | 2 | 0 | 3 |
| `M-UVLO` ⚠ | 欠压保护(UVLO)测试 | 4 | 1 | 5 | 1 | 5 |
| `M-OCP` ⚠ | 过流保护测试 | 3 | 2 | 4 | 2 | 5 |
| `M-OTP` ⚠ | 过温保护测试 | 4 | 2 | 3 | 0 | 5 |
| `M-SCP` ⚠ | 短路保护测试 | 3 | 2 | 2 | 0 | 3 |
| `M-OVP` | 过压保护测试 | 1 | 0 | 2 | 0 | 1 |
| `M-HIPOT` ⚠ | 耐压(电介质强度)测试 | 3 | 3 | 3 | 3 | 4 |
| `M-INSULATION-RES` ⚠ | 绝缘电阻测试 | 1 | 1 | 2 | 2 | 2 |
| `M-GROUND-BOND` ⚠ | 接地连续性(接地阻抗)测试 | 1 | 1 | 2 | 1 | 2 |
| `M-TOUCH-CURRENT` ⚠ | 接触电流(漏电流)测试 | 2 | 2 | 2 | 2 | 3 |
| `M-EFFICIENCY` ⚠ | 效率测试 | 4 | 1 | 5 | 2 | 5 |
| `M-REGULATION` ⚠ | 调整率测试(负载/线性) | 4 | 1 | 3 | 0 | 4 |
| `M-TRANSIENT` ⚠ | 动态响应测试 | 3 | 1 | 4 | 2 | 4 |
| `M-STARTUP` ⚠ | 启动特性测试(延迟/上升时间/过冲) | 4 | 1 | 1 | 0 | 2 |
| `M-INRUSH` ⚠ | 输入浪涌电流测试 | 3 | 1 | 2 | 1 | 5 |
| `M-KELVIN` ⚠ | 四线开尔文接触电阻测试 | 3 | 3 | 4 | 4 | 6 |
| `M-TEMP-RISE` ⚠ | 温升测试 | 4 | 4 | 2 | 0 | 2 |
| `M-BURN-IN` ⚠ | 老化测试(Burn-in) | 3 | 1 | 1 | 0 | 2 |
| `M-PF` ⚠ | 功率因数与输入电流测试 | 5 | 4 | 5 | 2 | 3 |
| `M-INPUT-SLOW` | 输入缓慢变动测试 | 3 | 0 | 3 | 0 | 4 |
| `M-OPP` ⚠ | 过功率保护测试 | 4 | 4 | 4 | 1 | 6 |
| `M-REVERSE-IN` | 输入反接保护测试 | 1 | 0 | 1 | 0 | 1 |
| `M-ORING` | 输出防倒灌(反灌)测试 | 2 | 0 | 1 | 0 | 2 |
| `M-SHARE` ⚠ | 并联均流测试 | 3 | 2 | 5 | 2 | 4 |
| `M-TELEMETRY` ⚠ | 遥测精度测试 | 3 | 2 | 6 | 3 | 6 |
| `M-TELESIGNAL` ⚠ | 遥信与告警上报测试 | 4 | 2 | 8 | 6 | 10 |
| `M-TELECONTROL` ⚠ | 遥控开关机与设定测试 | 2 | 2 | 5 | 3 | 4 |
| `M-COMM` ⚠ | 通信接口测试 | 2 | 1 | 2 | 1 | 3 |
| `M-POWERCYCLE` ⚠ | 开关机循环测试 | 3 | 1 | 1 | 0 | 2 |
| `M-UVP` ⚠ | 输出欠压保护测试 | 2 | 1 | 2 | 0 | 4 |

### b.5 公理与占位符

#### b.5.1 公理覆盖（76 条）

- 产品侧可达 32 条；**44 条不可达**：40 条"关联规则全部不可达" + 4 条"无任何规则关联"（`A-SERIES`、`A-PARALLEL`、`A-TEMPCOEFF`、`A-MOSCOND`，后四条是纯电学基础公式，无域规则连接）。
- 方法引用规则（78 条）中 **39 条没有任何公理背书**；7 个方法（M-GROUND-BOND、M-TRANSIENT、M-STARTUP、M-REVERSE-IN、M-ORING、M-TELESIGNAL、M-COMM）经规则反查不到任何公理。
- 历史探针的 `REQUIRED_AXIOMS`（40 个 id）清单已丢失，**该清单未复核**；上表"40 条关联规则全不可达"仅是口径巧合，不可当作该清单的替代结论。

不可达公理明细（44 条）：

**不可从产品侧抵达的公理 44 条**（规则关联链 `axiom → related_rules → 规则` 全部不可达；4 条无任何规则关联）

- `A-OHM`（axiom/general）rules=['K-ELEC-002', 'K-ELEC-003']
- `A-JOULE`（axiom/general）rules=['K-ELEC-004']
- `A-DCPOWER-I2R`（theorem/general）rules=['K-ELEC-004']
- `A-DCPOWER-V2R`（theorem/general）rules=['K-ELEC-005']
- `A-POWERTRIANGLE`（theorem/general）rules=['K-AC-003']
- `A-REACTIVE`（theorem/general）rules=['K-AC-003']
- `A-BRIDGEPEAK`（theorem/input）rules=['K-PWR-117']
- `A-LOSS`（theorem/efficiency）rules=['K-PWR-002']
- `A-ESRLOSS`（theorem/efficiency）rules=['K-PWR-005']
- `A-PWRBALANCE`（theorem/efficiency）rules=['K-PWR-121']
- `A-BUCK-DUTY`（theorem/output）rules=['K-PWR-004']
- `A-BOOST-DUTY`（theorem/output）rules=['K-PWR-004']
- `A-CAL-RESIDUAL`（formula/output）rules=['K-CAL-101']
- `A-LOWLINE-RATED`（formula/reliability）rules=['K-PWR-122', 'K-PWR-123']
- `A-DERATE-BY-V`（theorem/reliability）rules=['K-CAL-103']
- `A-NTC`（formula/thermal）rules=['K-TLM-104']
- `A-YCAP`（formula/safety）rules=['K-SAF-104']
- `A-CLEARANCE`（theorem/safety）rules=['K-SAF-103']
- `A-HIPOT-LIMIT`（formula/safety）rules=['K-SAF-101', 'K-SAF-102']
- `A-INSULRES`（formula/safety）rules=['K-SAF-001']
- `A-LOOP-PM`（formula/dynamics）rules=['K-LOOP-002']
- `A-LOOP-GM`（formula/dynamics）rules=['K-LOOP-003']
- `A-UNCERTAINTY`（axiom/general）rules=['K-MSR-002']
- `A-CM`（formula/general）rules=['K-MSR-001']
- `A-TUR`（formula/general）rules=['K-MSR-001']
- `A-WC`（formula/general）rules=['K-FIX-017']
- `A-RSS`（formula/general）rules=['K-FIX-002', 'K-FIX-017']
- `A-RESOLUTION-10TO1`（formula/general）rules=['K-FIX-015']
- `A-GRR`（formula/general）rules=['K-FIX-016']
- `A-INSTRUMENT-3X`（formula/general）rules=['K-FIX-001']
- `A-PROBE-LIFE`（formula/general）rules=['K-FIX-007', 'K-FIX-014']
- `A-PROBE-CHANNELS`（formula/general）rules=['K-FIX-006']
- `A-KELVIN`（axiom/general）rules=['K-FIX-009', 'K-FIX-010']
- `A-LSB`（axiom/general）rules=['K-MSR-101']
- `A-QUANTNOISE`（theorem/general）rules=['K-MSR-102']
- `A-APERTURE-JITTER`（theorem/general）rules=['K-MSR-103']
- `A-ENOB`（theorem/general）rules=['K-MSR-104']
- `A-SNR-IDEAL`（theorem/general）rules=['K-MSR-104']
- `A-NYQUIST`（theorem/general）rules=['K-TLM-102']
- `A-SHARE-LIMIT`（formula/output）rules=['K-CTL-104', 'K-PROT-118']

**无任何规则关联的 4 条：**

- `A-SERIES`（axiom/general）expr=`R = R1 + R2 + ...` props=['resistance', 'total_resistance']
- `A-PARALLEL`（axiom/general）expr=`1 / R = 1 / R1 + 1 / R2 + ...` props=['resistance', 'total_resistance']
- `A-TEMPCOEFF`（theorem/general）expr=`R(T) = R0 * (1 + alpha * (T - T0))` props=['resistance', 'ambient_temp_c']
- `A-MOSCOND`（theorem/efficiency）expr=`P_cond = I_avg^2 * R_DS(on) * D` props=['duty_cycle', 'current', 'resistance']

#### b.5.2 占位符参数与 "-" 值

| 项 | 数量 | 明细/影响 |
|----|------|-----------|
| name 为空或占位符（`params_placeholder`） | 3 | 全部未匹配；即 b.1 的 3 条 `-` 行；属真实缺口（合并且丢失的名称需回填） |
| 三个取值列全占位符/空 | 32 | 32/32 已按名称命中术语（CY0177 7 / PA601 25）；不构成匹配缺口，但这些行没有数值证据，定量问答不可用（如"散热方式、接地方式、输入防反功能"等描述行） |
| 参数 flags | 10 | multi_value 6（GDC 保护点/回差类多值行、PA601 输入范围等）、unparsed 4（CY0177 噪声+纹波、过冲电压/电流、输出过压保护）→ 数值解析未完成，影响定量推导 |

### b.6 术语侧缺口（96 / 155 无规格书实例）

155 条术语中只有 59 条被某条规格书参数命中；96 条无实例。按类别：general 39、emc 8、safety 8、output 7、protection 7、thermal 5、reliability 5、communication 5、input 4、dynamics 4、standards 2、efficiency 1、environment 1。

其中"被方法/规则依赖但产品侧无实例"的重点：T-HIPOT、T-INSULRES、T-TOUCHCURR、T-GNDCONT、T-YCAP、T-ESD、T-EFT、T-EMC、T-CE、T-EN61000-3-2（安规/EMC 章）；T-TELECONTROL、T-TELESIGNAL、T-TELEMETRY、T-SAMPLERATE（通信章）；T-HOLDUP、T-LINETRANSIENT（动态章）；T-OPP、T-FOLDBACK、T-HICCUP、T-LATCHOFF、T-FAULTLATCH（保护章）；T-MTTR、T-AVAIL（可靠性章）。这些都是"域知识有、产品证据无"的同一根因（R1/R3）的不同侧面。

---

## c) 优先级建议（Top 10 待补清单）

排序依据：是否阻断"产品参数 → 术语 → 规则 → 方法"主链路，以及是否影响产测问答的正确答案。

| # | 缺口 | 具体 id/范围 | 为什么优先 | 建议动作 |
|---|------|--------------|------------|----------|
| 1 | GDC 4.7 信号电平行无术语（12 条） | `N10V_FAULT/PWR_I2C_RESET/REMOTE_POWER_OFF` 电压/电流 × 高低电平（param:GDC…t16-1\|0..1、t17-0\|0..9） | 产测判据核心；关联 M-TELESIGNAL/M-TELECONTROL/M-COMM 与 6+3+1 条不可达规则 | 术语表新增信号级术语（如 T-FAULT-SIGNAL、T-REMOTE-CTRL、T-I2C-RESET），补 source=spec_table；或在 KG 增加"信号量"参数类型 |
| 2 | 安规要求表未入参数层 | PA601 t0-30（绝缘电压 4000/2500/500Vdc、绝缘电阻 2MΩ、接触电流 <2.5mA/<0.5mA、接地阻抗 <0.1Ω）、CY0177 t11-0 表4（3000/1500/500Vac、≥50MΩ@500Vdc） | 直接解锁 safety 11 条规则（K-SAF-*）与 4 个安规方法闭环；回答"该产品耐压/漏电要求" | 扩展 `spec_params` 支持"编号/项目/要求/等级"型要求表（R1），把定量要求抽为参数实例 |
| 3 | 3 条 `-` name 行 | CY0177 t7-0\|7/8（91/92 疑效率）、GDC t14-0\|0（200ms 疑上升时间） | 效率/输出动态特性是高频问题；两产品各失一个关键量 | 修解析端合并单元格名称回填（或先做别名手工补丁），命中 T-EFFICIENCY/T-STARTUP 相关术语 |
| 4 | 16 条无术语规则 | K-FIX-008/011/012/020~023、K-CMN-001、K-PROT-110/112、K-PWR-119/125、K-CTL-102、K-FIX-005/018/019 | 规则图侧完全孤立；其中 8 条被方法引用 | fixture/units 类加豁免标注；K-PROT-110、K-PWR-119 等产品相关规则补术语挂接 |
| 5 | 安规/EMC 术语补规格书证据 | T-INSULRES、T-HIPOT、T-TOUCHCURR、T-GNDCONT、T-YCAP、T-ESD、T-EFT、T-EMC、T-CE | 方法+规则双断链的源头；EMC 判据表（CY0177 表5、PA601 表23~25）也整表未入参 | 依赖第 2 项；EMC 判据表按"试验项目/等级/判据"结构单独建"判据型参数" |
| 6 | 通信/遥测/遥控条款未入参 | PA601 t0-17 遥信（31 行）、t0-18 遥控（19 行）、t0-19 遥测（含 ±3% 精度）、GDC t38/t39 PMBus 命令与状态位 | M-TELEMETRY（6 规则 3 不可达）、M-TELESIGNAL（8 规则 6 不可达）、M-TELECONTROL；遥测精度是问答高频 | 扩展要求表抽取 + 术语补 T-TELESIGNAL/T-TELECONTROL/T-TELEMETRY 的规格书锚点 |
| 7 | 8 个方法 measures 零落地 | M-HIPOT、M-INSULATION-RES、M-GROUND-BOND、M-TOUCH-CURRENT、M-KELVIN、M-TEMP-RISE、M-OPP、M-TELECONTROL | 方法问答无产品证据可引用 | 依赖第 2/5 项；M-KELVIN、M-TEMP-RISE 的工装类术语可改指可落地量（接触电阻、温升限值）或明确"工装要求，无规格书来源" |
| 8 | 公理不可达 | 44 条（40 关联规则不可达 + A-SERIES/A-PARALLEL/A-TEMPCOEFF/A-MOSCOND 无规则） | 公式检索与推导解释引用不到规则；A-OHM/A-LOSS/A-PWRBALANCE 等基础公式不可达影响推导链展示 | 给孤立公理补 related_rules（可指向 K-ELEC-* 等基础规则）或允许"无规则"公理仅作为检索锚；随第 2~6 项自然修复 40 条中的大部分 |
| 9 | 方法规则无公理背书 | 39/78 条方法规则无公理；7 个方法零公理（M-GROUND-BOND、M-TRANSIENT、M-STARTUP、M-REVERSE-IN、M-ORING、M-TELESIGNAL、M-COMM） | 判据解释缺公式支撑（如接地阻抗、动态响应斜率、开机时序） | 为这些规则补 axiom 关联（新增 A-* 或挂到已有公式） |
| 10 | 属性豁免清单缺失 | 185 条无证据：A 簇 37 派生 + C 簇 71 内部量中的工装/公式变量 | 现在无法区分"真缺口"与"设计豁免"，报告/看板会把 185 全当缺陷 | 在数据侧显式输出豁免清单（建议字段：`internal: true` 或 ontology 属性注记），让覆盖率指标聚焦真缺口 89 条 |

次优先（不建议本轮处理）：fixture 类 48 条属性/23 条规则中的产线工装细节、general 类基础电学术语（T-ACDC、T-RE、T-REALPOWER 等）——它们对"产品产测问答"价值低，但需在豁免清单中显式化。

---

## d) 复现命令

以下命令均在 `F:\Workspace\MATRag` 下执行；临时脚本位于 `C:\Users\chenp\AppData\Local\Temp\opencode\matrag_gaps\`（不污染仓库）。Python 一律用仓库 venv：

| 统计 | 命令 |
|------|------|
| 全量缺口扫描（基线 + 15 未匹配 + 185 属性 + 82 规则 + 31 方法 + 76 公理 + 占位符 + 产物新鲜度） | `.venv\Scripts\python.exe C:\Users\chenp\AppData\Local\Temp\opencode\matrag_gaps\scan_gaps.py` → 输出 `gaps_scan.json` |
| 官方流水线重放（校核产物新鲜度/节点边数） | `.venv\Scripts\python.exe scripts\import_domain.py --out C:\Users\chenp\AppData\Local\Temp\opencode\matrag_gaps\generated` |
| 交叉统计（术语落地 59/155、16 条无术语规则、8 个零落地方法、公理↔方法反查、属性类别直方图） | `.venv\Scripts\python.exe C:\Users\chenp\AppData\Local\Temp\opencode\matrag_gaps\cross_checks.py` |
| 抽取盲区（被跳过的表及原因） | `.venv\Scripts\python.exe C:\Users\chenp\AppData\Local\Temp\opencode\matrag_gaps\skipped_tables.py` |
| 3 条占位符行上下文 | `.venv\Scripts\python.exe C:\Users\chenp\AppData\Local\Temp\opencode\matrag_gaps\row_context.py` |
| 报告片段生成（附录表格） | `.venv\Scripts\python.exe C:\Users\chenp\AppData\Local\Temp\opencode\matrag_gaps\make_fragments.py` |
| 基线硬断言复跑 | `.venv\Scripts\python.exe -m pytest tests/test_domain_knowledge_graph.py tests/test_domain_terms.py tests/test_domain_axioms.py tests/test_domain_test_methods.py tests/test_domain_spec_params.py -q -p no:cacheprovider` |
| 快查 coverage（只读） | `.venv\Scripts\python.exe -X utf8 -c "import json,pathlib; c=json.loads(pathlib.Path('data/domain/generated/power_knowledge_graph.json').read_text(encoding='utf-8'))['coverage']; print({k:c[k] for k in ('params_total','params_with_term','params_unmatched','properties_total','properties_with_spec_evidence','rules_reachable_from_products','methods_total')})"` |
| coverage_check 历史探针 | **未复核**：`scripts/coverage_check.py` 在仓库中不存在（见文末） |

---

## 附录

### 附录 A —— 15 条未匹配参数明细

| # | 参数 id | 产品 | 类别 | 名称 | 子项 | 值(min/typ/max) | 单位 | 章节 | 说明 |
|---|---------|------|------|------|------|-----------------|------|------|------|
| 1 | `param:CY0177-75V-20A\|t7-0\|7` | CY0177-75V-20A | input | `-` | `` | 91.0/None/None | - | 3 技术要求 > 3.1 电源输入规格 | 表1 输入规格; 值 91 + 备注「220Vac/50Hz 输入,48V 输出,满载」→ 疑为 48V 支路效率行, name 单元格合并丢失 |
| 2 | `param:CY0177-75V-20A\|t7-0\|8` | CY0177-75V-20A | input | `-` | `` | 92.0/None/None | - | 3 技术要求 > 3.1 电源输入规格 | 同上, 值 92 + 备注「60V 输出,满载」→ 疑为 60V 支路效率行 |
| 3 | `param:GDC800T5054-UC\|t14-0\|0` | GDC800T5054-UC | output | `-` | `` | None/None/200.0 | ms | 4 电气特性 > 4.3 输出特性 | 4.3 输出特性表首行, 支路 N10V, 最大 200ms → 疑为「输出电压上升时间」行, name 合并丢失 |
| 4 | `param:GDC800T5054-UC\|t16-1\|0` | GDC800T5054-UC | other | `N10V_FAULT电压<br>(低电平)` | `` | 0.0/None/8.0 | V | 4 电气特性 > 4.7 其他特性 | 4.7 其他特性: 信号电平行, 术语表无对应信号术语 |
| 5 | `param:GDC800T5054-UC\|t16-1\|1` | GDC800T5054-UC | other | `N10V_FAULT电压<br>(高电平)` | `` | 8.0/None/6.0 | V | 4 电气特性 > 4.7 其他特性 | 4.7 其他特性: 信号电平行, 术语表无对应信号术语 |
| 6 | `param:GDC800T5054-UC\|t17-0\|0` | GDC800T5054-UC | other | `N10V_FAULT电流<br>(低电平)` | `` | -4.0/None/None | mA | 4 电气特性 > 4.7 其他特性 | 4.7 其他特性: 信号电平行, 术语表无对应信号术语 |
| 7 | `param:GDC800T5054-UC\|t17-0\|1` | GDC800T5054-UC | other | `N10V_FAULT电流<br>(高电平)` | `` | None/None/4.0 | mA | 4 电气特性 > 4.7 其他特性 | 4.7 其他特性: 信号电平行, 术语表无对应信号术语 |
| 8 | `param:GDC800T5054-UC\|t17-0\|2` | GDC800T5054-UC | other | `PWR_I2C_RESET电压<br>(低电平)` | `` | 0.0/None/8.0 | V | 4 电气特性 > 4.7 其他特性 | 4.7 其他特性: 信号电平行, 术语表无对应信号术语 |
| 9 | `param:GDC800T5054-UC\|t17-0\|3` | GDC800T5054-UC | other | `PWR_I2C_RESET电压<br>(高电平)` | `` | 8.0/None/6.0 | V | 4 电气特性 > 4.7 其他特性 | 4.7 其他特性: 信号电平行, 术语表无对应信号术语 |
| 10 | `param:GDC800T5054-UC\|t17-0\|4` | GDC800T5054-UC | other | `PWR_I2C_RESET电流<br>(低电平)` | `` | -4.0/None/None | mA | 4 电气特性 > 4.7 其他特性 | 4.7 其他特性: 信号电平行, 术语表无对应信号术语 |
| 11 | `param:GDC800T5054-UC\|t17-0\|5` | GDC800T5054-UC | other | `PWR_I2C_RESET电流<br>(高电平)` | `` | None/None/4.0 | mA | 4 电气特性 > 4.7 其他特性 | 4.7 其他特性: 信号电平行, 术语表无对应信号术语 |
| 12 | `param:GDC800T5054-UC\|t17-0\|6` | GDC800T5054-UC | other | `REMOTE_POWER_OFF<br>电压<br>(低电平)` | `` | 0.0/None/8.0 | V | 4 电气特性 > 4.7 其他特性 | 4.7 其他特性: 信号电平行, 术语表无对应信号术语 |
| 13 | `param:GDC800T5054-UC\|t17-0\|7` | GDC800T5054-UC | other | `REMOTE_POWER_OFF<br>电压<br>(高电平)` | `` | 8.0/None/6.0 | V | 4 电气特性 > 4.7 其他特性 | 4.7 其他特性: 信号电平行, 术语表无对应信号术语 |
| 14 | `param:GDC800T5054-UC\|t17-0\|8` | GDC800T5054-UC | other | `REMOTE_POWER_OFF<br>电流<br>(低电平)` | `` | -4.0/None/None | mA | 4 电气特性 > 4.7 其他特性 | 4.7 其他特性: 信号电平行, 术语表无对应信号术语 |
| 15 | `param:GDC800T5054-UC\|t17-0\|9` | GDC800T5054-UC | other | `REMOTE_POWER_OFF<br>电流<br>(高电平)` | `` | None/None/4.0 | mA | 4 电气特性 > 4.7 其他特性 | 4.7 其他特性: 信号电平行, 术语表无对应信号术语 |

### 附录 B —— 185 条无规格书证据属性（三分簇全清单）

三个功能簇（共 185）：派生量 37 + 术语已挂未落地 77 + 无术语关联非派生 71

**A. 规则派生量（37，计算可得，建议豁免规格书证据）**

- `adc_lsb`（measurement）terms=['T-LSB']
- `aperture_jitter_error`（measurement）terms=['T-APERTUREJITTER']
- `availability`（reliability）terms=['T-AVAIL']
- `current_share_error`（current-sharing）terms=['T-DROOP', 'T-DROOP-GEN']
- `derated_output_current`（derating）terms=['T-DERATE', 'T-LOWLINEDERATE']
- `duty_cycle`（power）terms=['T-BOOST', 'T-BUCK', 'T-DUTY', 'T-FORWARD']
- `effective_clearance`（fixture）terms=['T-CLEARANCE', 'T-CREEPAGE']
- `enob_from_snr`（measurement）terms=['T-ENOB', 'T-SNR']
- `esr_loss`（power）terms=['T-ESR']
- `expanded_uncertainty`（accuracy）terms=['T-UNCERT']
- `fault_coverage_complete`（fixture）terms=[]
- `fixture_precision`（fixture）terms=[]
- `gain_margin_headroom_db`（control）terms=['T-GM']
- `input_power`（power）terms=['T-INPWR']
- `kelvin_measured_resistance`（fixture）terms=['T-KELVIN4W']
- `life_factor`（thermal）terms=['T-ARRHENIUS']
- `load_current_margin`（fixture）terms=[]
- `loss`（power）terms=['T-JTEMP', 'T-LOSS', 'T-RTH']
- `max_y_capacitance`（safety）terms=['T-YCAP']
- `measurement_capability_index`（accuracy）terms=['T-CM']
- `min_pin_diameter`（fixture）terms=['T-POGOPIN']
- `ntc_resistance`（telemetry）terms=['T-NTC']
- `phase_margin_headroom_deg`（control）terms=['T-PM']
- `quantization_noise_rms`（measurement）terms=['T-QUANTNOISE']
- `rail_power_deviation_w`（power）terms=[]
- `reactive_power`（ac）terms=['T-REACTIVEPOWER']
- `rectified_bus_voltage`（ac-dc）terms=['T-PFC', 'T-RECTBUS']
- `remaining_life`（fixture）terms=[]
- `required_bulk_capacitance`（transient）terms=[]
- `required_channels`（fixture）terms=[]
- `required_cycle_life`（fixture）terms=['T-MANDATORY']
- `required_resolution`（fixture）terms=['T-RESOLUTION']
- `required_setpoint_accuracy`（current-sharing）terms=[]
- `rss_tolerance`（fixture）terms=['T-RSS', 'T-WC', 'T-WCSTACK']
- `scaled`（units）terms=[]
- `switch_current_margin`（fixture）terms=[]
- `total_tolerance`（fixture）terms=['T-RSS', 'T-WC', 'T-WCSTACK']

**B. 术语已挂但该术语无规格书实例（77）**

- `actual_clearance_mm`（safety）terms=['T-CLEARANCE', 'T-CREEPAGE']
- `alarm_latency_limit_ms`（telemetry）terms=['T-TELESIGNAL']
- `aperture_jitter`（measurement）terms=['T-APERTUREJITTER']
- `b_coeff`（telemetry）terms=['T-NTC']
- `bits`（measurement）terms=['T-ENOB', 'T-FSR', 'T-LSB', 'T-QUANTNOISE', 'T-RESOLUTION']
- `cal_storage`（calibration）terms=['T-CALIB']
- `calibration_limit_pct`（calibration）terms=['T-CALIB']
- `calibration_residual_pct`（calibration）terms=['T-CALIB']
- `cap_class`（safety）terms=['T-Y1Y2', 'T-YCAP']
- `combined_standard_uncertainty`（accuracy）terms=['T-UNCERT']
- `command_latency_limit_ms`（control）terms=['T-TELECONTROL']
- `contact_resistance`（fixture）terms=['T-GNDCONT', 'T-KELVIN4W', 'T-POGOPIN']
- `coverage_factor`（accuracy）terms=['T-MANDATORY']
- `crossover_ratio_divisor`（control）terms=['T-FC']
- `current_rms`（ac）terms=['T-APPARENTPOWER', 'T-RMS']
- `current_setpoint_max`（control）terms=['T-SOFTSTART']
- `current_share_error_limit`（current-sharing）terms=['T-DROOP', 'T-DROOP-GEN']
- `debounce_ms`（telemetry）terms=['T-TELESIGNAL']
- `default_state`（fixture）terms=['T-LATCHOFF', 'T-WDT']
- `derated_capacity_a`（derating）terms=['T-DERATE']
- `droop_slope`（current-sharing）terms=['T-DROOP', 'T-DROOP-GEN']
- `esr`（power/ripple）terms=['T-ESR']
- `fail_safe_state`（telemetry）terms=['T-WDT']
- `fault_active`（control）terms=['T-FAULTLATCH', 'T-NPLUS1', 'T-WDT']
- `gain_margin_db`（control）terms=['T-GM']
- `gain_margin_min_db`（control）terms=['T-GM']
- `ground_resistance`（safety）terms=['T-GNDCONT']
- `has_esd_protection`（safety）terms=['T-ESD']
- `hold_up_time`（transient）terms=['T-HOLDUP']
- `i_leak`（safety）terms=['T-TOUCHCURR', 'T-YCAP']
- `insulation_resistance`（safety）terms=['T-INSULRES']
- `insulation_type`（safety）terms=['T-HIPOT', 'T-INSULRES']
- `isolation_level`（control）terms=['T-HIPOT', 'T-Y1Y2']
- `line_derated_power_w`（derating）terms=['T-DERATECURVE', 'T-LOWLINEDERATE']
- `lsb`（measurement）terms=['T-LSB', 'T-RESOLUTION']
- `measured_command_latency_ms`（control）terms=['T-TELECONTROL']
- `measured_telemetry_error_pct`（telemetry）terms=['T-TELEMETRY']
- `measured_touch_current_ma`（safety）terms=['T-TOUCHCURR']
- `measured_turns_ratio`（transformer）terms=['T-TUR', 'T-TURNRATIO']
- `measurement_type`（safety）terms=['T-KELVIN4W']
- `min_centers`（fixture）terms=['T-POGOPIN']
- `mttr`（reliability）terms=['T-AVAIL', 'T-MTTR']
- `need`（fixture）terms=['T-RECOMMENDED']
- `noise_pp`（protection）terms=['T-QUANTNOISE', 'T-SFDR', 'T-VPEAK', 'T-VPP']
- `nominal_turns_ratio`（transformer）terms=['T-FLYBACK', 'T-FORWARD', 'T-TURNRATIO']
- `p_line_derated`（derating）terms=['T-LOWLINEDERATE']
- `p_out`（efficiency）terms=['T-OPP']
- `peak_current`（ripple）terms=['T-SATCURRENT']
- `percent_gauge_rr`（fixture）terms=['T-GRR']
- `phase_margin_deg`（control）terms=['T-FC', 'T-PM']
- `phase_margin_min_deg`（control）terms=['T-PM']
- `priority`（coverage）terms=['T-RECOMMENDED']
- `process_variation`（fixture）terms=['T-GRR', 'T-SAFSEQ']
- `r25`（telemetry）terms=['T-NTC']
- `rail_power_budget_tolerance_w`（power）terms=['T-RSS']
- `rated_life`（fixture）terms=['T-ARRHENIUS']
- `recovery_mode`（protection）terms=['T-HICCUP']
- `requires_manual_reset`（protection）terms=['T-FAULTLATCH', 'T-LATCHOFF']
- `reset_clears_active_fault`（control/protection）terms=['T-FAULTLATCH', 'T-LATCHOFF']
- `resistance`（electrical）terms=['T-RESISTANCE']
- `ripple_current_actual`（ripple）terms=['T-RIPPLERATING']
- `sample_rate_hz`（telemetry）terms=['T-SAMPLERATE']
- `sat_current`（ripple）terms=['T-SATCURRENT']
- `setpoint_mismatch`（current-sharing）terms=['T-REMOTESENSE']
- `severity_sequence`（control）terms=['T-SAFSEQ']
- `snr_db`（measurement）terms=['T-ENOB', 'T-SFDR', 'T-SNR']
- `spacing`（fixture）terms=['T-CREEPAGE']
- `spec_tolerance`（fixture）terms=['T-CM', 'T-FSR', 'T-TUR']
- `standard_uncertainty`（accuracy）terms=['T-UNCERT']
- `test_time`（fixture）terms=['T-BURNIN', 'T-SHORTTERM', 'T-THERMALCHAMBER']
- `test_voltage_v`（safety）terms=['T-HIPOT', 'T-Y1Y2']
- `tolerance`（accuracy）terms=['T-FSR', 'T-WC']
- `touch_current_limit_ma`（safety）terms=['T-TOUCHCURR']
- `turns_ratio_tolerance`（transformer）terms=['T-FLYBACK', 'T-FORWARD', 'T-TURNRATIO']
- `uncertainty_multiple`（accuracy）terms=['T-CM']
- `v_high_line`（regulation）terms=['T-VPEAK']
- `voltage_rms`（ac）terms=['T-APPARENTPOWER', 'T-RMS']

**C. 无术语关联且非派生（71，多为公式变量/工装量纲）**

- `can_inject_open`（fixture）consumed_by=1 axioms=0 classes=[]
- `can_inject_short_inter_channel`（fixture）consumed_by=1 axioms=0 classes=[]
- `can_inject_short_to_rail`（fixture）consumed_by=1 axioms=0 classes=[]
- `comm_error_count`（telemetry）consumed_by=1 axioms=0 classes=['TelemetrySpec']
- `components`（fixture）consumed_by=2 axioms=0 classes=[]
- `connection_type`（fixture）consumed_by=1 axioms=0 classes=['Measurement']
- `connects_to`（safety）consumed_by=1 axioms=0 classes=['YCapacitor']
- `contact_resistance_limit`（fixture）consumed_by=1 axioms=0 classes=['Probe']
- `contact_resistance_margin`（fixture）consumed_by=1 axioms=0 classes=['Probe']
- `contact_type`（telemetry）consumed_by=1 axioms=0 classes=['DigitalOutput']
- `control_type`（protection）consumed_by=1 axioms=0 classes=['Product']
- `current_rating`（fixture）consumed_by=1 axioms=0 classes=['Probe']
- `declared_total_output_power_w`（power）consumed_by=1 axioms=0 classes=[]
- `device_i2t`（safety）consumed_by=1 axioms=1 classes=['FuseSpec']
- `diameter`（fixture）consumed_by=2 axioms=0 classes=['Probe']
- `dut_rated_current`（fixture）consumed_by=1 axioms=1 classes=[]
- `dut_resistance`（fixture）consumed_by=1 axioms=0 classes=['Measurement']
- `effective_cycle_life`（fixture）consumed_by=1 axioms=0 classes=['Probe']
- `exponent`（units）consumed_by=1 axioms=0 classes=[]
- `fail_safe_clamp`（protection）consumed_by=1 axioms=0 classes=['Sensor']
- `first_step`（safety）consumed_by=1 axioms=0 classes=['TestProcedure']
- `fixture_resistance`（fixture）consumed_by=1 axioms=0 classes=[]
- `freq`（safety）consumed_by=1 axioms=0 classes=[]
- `full_load_a`（protection）consumed_by=1 axioms=0 classes=['RedundancySpec']
- `full_scale`（measurement）consumed_by=1 axioms=2 classes=[]
- `has_over_current_protection`（safety）consumed_by=1 axioms=0 classes=['TestEquipment']
- `has_test_case`（coverage）consumed_by=1 axioms=0 classes=['Requirement']
- `injected_fault_current`（fixture）consumed_by=1 axioms=0 classes=[]
- `input_amplitude`（measurement）consumed_by=1 axioms=0 classes=[]
- `input_type`（protection）consumed_by=1 axioms=0 classes=['Product']
- `judgement_margin_pct`（efficiency）consumed_by=1 axioms=0 classes=['TestEquipment']
- `line_deviation_limit_pct`（transient）consumed_by=1 axioms=0 classes=['TransientSpec']
- `line_deviation_pct`（transient）consumed_by=1 axioms=0 classes=['TransientSpec']
- `load_current_rating`（fixture）consumed_by=1 axioms=0 classes=[]
- `main_isolation_level`（control）consumed_by=1 axioms=0 classes=['DigitalOutput']
- `margin`（protection）consumed_by=1 axioms=0 classes=['Alarm']
- `max`（limits）consumed_by=1 axioms=0 classes=['Parameter']
- `measured_deviation_pct`（transient）consumed_by=1 axioms=0 classes=['TransientSpec']
- `meter_accuracy_pct`（efficiency）consumed_by=1 axioms=0 classes=['TestEquipment']
- `min`（limits）consumed_by=1 axioms=0 classes=['Parameter']
- `open_circuit_detected`（protection）consumed_by=1 axioms=0 classes=['Sensor']
- `p_in`（efficiency）consumed_by=1 axioms=4 classes=[]
- `param_tolerance`（fixture）consumed_by=1 axioms=0 classes=[]
- `pitch`（fixture）consumed_by=1 axioms=0 classes=[]
- `production_cycles`（fixture）consumed_by=1 axioms=0 classes=[]
- `rail_power_sum_w`（power）consumed_by=1 axioms=0 classes=[]
- `rated_capacity_a`（derating）consumed_by=1 axioms=0 classes=['ThermalTest']
- `rated_power_w`（derating）consumed_by=1 axioms=0 classes=['PowerTest']
- `rated_voltage_v`（derating）consumed_by=1 axioms=1 classes=['Capacitor']
- `rating`（fixture）consumed_by=1 axioms=0 classes=['Probe']
- `readback_limit_pct`（control）consumed_by=1 axioms=1 classes=['ControlSpec']
- `remaining_capacity_a`（protection）consumed_by=1 axioms=0 classes=['RedundancySpec']
- `safety_factor`（fixture）consumed_by=1 axioms=1 classes=[]
- `setpoint_margin`（control）consumed_by=1 axioms=0 classes=['ControlSpec']
- `setpoint_readback_delta_pct`（control）consumed_by=1 axioms=1 classes=['ControlSpec']
- `sharing_fault_threshold_pct`（control）consumed_by=1 axioms=2 classes=['CurrentSharingSpec']
- `stagger`（fixture）consumed_by=1 axioms=0 classes=[]
- `switch_current_rating`（fixture）consumed_by=1 axioms=0 classes=[]
- `throughput`（fixture）consumed_by=1 axioms=1 classes=[]
- `total_resistance`（fixture）consumed_by=1 axioms=2 classes=[]
- `track_ratio`（protection）consumed_by=1 axioms=0 classes=[]
- `trip_measured`（protection）consumed_by=1 axioms=1 classes=['ProtectionPoint']
- `trip_nominal`（protection）consumed_by=1 axioms=1 classes=['ProtectionPoint']
- `used_count`（fixture）consumed_by=1 axioms=1 classes=[]
- `v_end`（transient）consumed_by=1 axioms=1 classes=[]
- `v_low_line`（regulation）consumed_by=1 axioms=1 classes=[]
- `v_nominal`（regulation）consumed_by=1 axioms=2 classes=[]
- `v_out`（derating）consumed_by=1 axioms=0 classes=[]
- `v_start`（transient）consumed_by=1 axioms=1 classes=[]
- `value`（limits/units）consumed_by=2 axioms=0 classes=['Parameter']
- `working_voltage_v`（derating）consumed_by=1 axioms=1 classes=['Capacitor']

---

## 未复核项与限制

1. **历史探针 `scripts/coverage_check.py` 未复核（文件不存在）**。查找范围：`scripts/` 全目录、仓库内 `**/coverage*.py`、`grep REQUIRED_TERMS/REQUIRED_AXIOMS`、`F:\Workspace` 各仓库（深度 3）、`data/backups/oldstack-20261016-200939/aterag-board.aterag.tgz` 全量列表（只有旧栈 ATERag 的 `verify_requirement_coverage.py`，无本仓库探针）。因此 `REQUIRED_TERMS ~90` 与 `REQUIRED_AXIOMS 40` 两份清单**无法核对**；本文以现算代理口径替代（术语以"方法 measures ∪ 规则 related_rules"依赖面覆盖，公理以"产品侧可达 + 方法反查"覆盖）。
2. 需要 LLM 的分析（如语义判断某术语是否确实不该出现在规格书）一律跳过；文中"可豁免/真缺口"的判定基于图结构与类别规则，是启发式而非人工逐条确认。
3. `data/corpus/*.doc.json|*.tables.json|*.md` 为解析产物，本次只读；GDC 4.7 表出现的 min>max 现象按原样记录，未回改。
4. `data/store/index.db` 未触碰；检索索引重建需网络/嵌入模型，不在本次范围。
5. 本报告只读分析，未修改任何 `src/`、`tests/` 代码与公共契约文件。
