# MATRag 知识面缺口复审计报告（2026-10-17）

- **生成日期**: 2026-10-17
- **评估对象**: MATRag 知识层（知识卡片 / 术语 / 公理 / 本体 / 知识图谱 / 产测方法）+ 3 份产品规格书语料
- **数据面**: `data/corpus`（CY0177-75V-20A、GDC800T5054-UC、PA601-D54A 定制电源技术规格书）、`data/domain/source`（术语/公理/方法/规则 4 份 YAML）、`data/domain/generated`（本体 TTL/JSON + 知识图谱 JSON）
- **评估方式**: 确定性只读审计（知识图谱 API + 本机脚本）；不含 LLM 判断；全部结论可复现（见 d 节）
- **对照**: 取代旧报告 `data/reports/knowledge-gaps-20261016.md`（旧报告保留对照）
- **核心口径**: 命中 = 参数名/子项名/表级 scope 归一后与术语名/别名匹配（exact 优先，contains 次之，ASCII 词边界保护，单字符键仅 exact）；未匹配 = 无任何 denoted 术语

## 0. 本波修复摘要（2026-10-16 ~ 10-17）

1. **要求型表入参数层**: `spec_params` 扩展支持"编号/项目/要求/等级"型要求表 —— 要求列多列按「；」拼接、信号名称→`sub_name`、表级 `scope` 兜底锚定；CY 表4/表5、PA601 表13/14/15/22/24–26/27–29、GDC 表50 等 +153 行，参数 219→372。
2. **匹配层**: `_match_param` 新增 scope 兜底（name→sub_name→scope；遥信/遥控/遥测功能等表级概念）；单字符键（A/R）仅允许精确匹配。
3. **术语补录 163→168（全部带公开标准出处）**: T-VDIP（GB/T 17626.11）、T-SURGE（GB/T 17626.5）、T-ENVTEST（GB/T 2423 系列）、T-IP（GB/T 4208）、T-ROHS（GB/T 26572）。
4. **别名补录与规则挂接 8 处**: T-HIPOT[抗电强度/介电强度]、T-EFT[电快速脉冲群] 等；T-SHARE+K-CTL-104、T-EFFICIENCY+K-PWR-119/K-PWR-121、T-TELEMETRY+K-PROT-112、T-TELESIGNAL+K-CTL-102/K-PROT-110、T-INRUSH+K-SAF-106、T-POWER+K-PWR-125。
5. **语料修复**: CY0177 表2 两条 PDF 合并格丢失行名（`-`→`效率`；改前备份在本机 Temp 目录，不随仓库分发）。

## a) 覆盖基线

### a.1 对照第 6 波（10-16 报告）

| 指标 | 第 6 波 | 本报告 |
|---|---|---|
| 规格书参数 | 219 | **372** |
| 命中术语 | 204（93.2%） | **361（97.0%）** |
| 未匹配参数 | 15 | **11** |
| 术语 | 154 | **168** |
| 本体属性 | 267 | 267 |
| 有规格证据属性 | 82 | **104** |
| 无证据属性 | 185 | **163** |
| 规则 | 132 | 132 |
| 规则可达（产品参数可达术语反引） | 50 | **73** |
| 公理 | 76 | 76 |
| 公理不可达 | 44 | **38** |
| 产测方法 | 31 | **32** |
| 知识图谱 | 1055 节点 / 3337 边 | **1222 节点 / 4059 边** |
| 全仓测试 | 1156 passed | **1206 passed + ruff 全绿** |

### a.2 按产品（本报告实测）

| 产品 | 参数 | 命中术语 | 有属性证据 |
|---|---|---|---|
| CY0177-75V-20A | 50 | 50 | 38 |
| GDC800T5054-UC | 106 | 105 | 78 |
| PA601-D54A | 216 | 206 | 150 |
| 合计 | 372 | 361 | 266 |

### a.3 产物新鲜度与质量门

- 知识图谱 JSON 与内存重建一致核对通过（1222 节点 / 4059 边）；`validate()` 0 issue。
- 本体：70 类 / 267 属性，构建 issue 0；132/132 规则验证通过（Datalog + SHACL + 表达式求值）。
- 规则边 related_rule 289、instance_of 623、denotes 361。

## b) 缺口清单

### b.0 根因总览

| 编号 | 缺口 | 数量 | 性质 |
|---|---|---|---|
| R1 | 要求陈述句参数无术语锚定（未匹配） | 11 | 无法绑定定量值/概念的整机要求 |
| R2 | 规则不可达 = no-term 15 + 术语无实例 44 | 59 | 设计性豁免 + 等待产品锚点 |
| R3 | 无实例术语（无任何参数 denotes） | 85/168 | 计量/仿真/通用方法论概念 |
| R4 | 无规格证据属性 | 163/267 | 最大簇为工装/计量（fixture 44） |
| R5 | 公理不可达 38（另 4 条无任何规则关联） | 38 | 随规则可达收敛，本波 44→38 |

### b.1 未匹配参数 11 条（附录 A 明细）

- `GDC800T5054-UC`：`-`（输出特性表首行，PDF 合并格丢名，语料不可恢复）。
- `PA601-D54A` 10 条：MOS短路不起火要求 / 三防要求 / 其他底线安全性要求 / 器件要求 / 失效隔离 / 安规其他测试项目 / 混插要求 / 电源保护要求 / 直通电源过载不起火要求 / 进水氢爆防护要求 —— 均为整机级要求陈述句，无可绑定定量参数，**按现状结案**（不伪装匹配）。

### b.2 规则不可达 59/132

**no-term 规则 15 条（无任何术语反引；设计性豁免）**：K-CMN-001（SI 词头换算，通用单位知识非产品域）；K-FIX-001/005/006/007/008/011/012/014/018/019/020/021/022/023（夹具设计规则，无对应术语对象；文件内注释已标注豁免）。

**术语无实例规则 44 条（术语存在但无产品参数锚点）**：

| 类别 | 规则 |
|---|---|
| ac (1) | K-AC-003 |
| ac-dc (1) | K-PWR-117 |
| accuracy (2) | K-MSR-001, K-MSR-002 |
| calibration (2) | K-CAL-101, K-CAL-102 |
| control (3) | K-CTL-103, K-LOOP-002, K-LOOP-003 |
| coverage (1) | K-COV-001 |
| derating (3) | K-CAL-103, K-PWR-122, K-PWR-123 |
| electrical (4) | K-ELEC-002, K-ELEC-003, K-ELEC-004, K-ELEC-005 |
| fixture (9) | K-FIX-002, K-FIX-003, K-FIX-004, K-FIX-009, K-FIX-010, K-FIX-013, K-FIX-015, K-FIX-016, K-FIX-017 |
| measurement (4) | K-MSR-101, K-MSR-102, K-MSR-103, K-MSR-104 |
| power (5) | K-PWR-002, K-PWR-004, K-PWR-005, K-PWR-124, K-PWR-125 |
| protection (4) | K-PROT-113, K-PROT-114, K-PROT-118, K-PROT-119 |
| safety (2) | K-SAF-104, K-SAF-105 |
| telemetry (1) | K-TLM-104 |
| transformer (1) | K-PWR-105 |
| transient (1) | K-PWR-108 |

### b.3 产测方法断点（32 方法中 25 个存在断点；7 个全落地）

| 方法 | 未落地 measures | 不可达 rules |
|---|---|---|
| M-BURN-IN 老化测试 | 1/3 | 0/1 |
| M-COMM 通信接口测试 | 1/2 | 1/2 |
| M-EFFICIENCY 效率测试 | 1/4 | 0/5 |
| M-HIPOT 耐压测试 | 2/3 | 0/3 |
| M-HOLDUP 掉电保持时间测试 | 2/3 | 0/2 |
| M-INRUSH 输入浪涌电流测试 | 1/3 | 0/2 |
| M-KELVIN 四线开尔文接触电阻测试 | 3/3 | 4/4 |
| M-OCP 过流保护测试 | 2/3 | 1/4 |
| M-OPP 过功率保护测试 | 4/4 | 0/4 |
| M-OTP 过温保护测试 | 2/4 | 0/3 |
| M-PF 功率因数与输入电流测试 | 4/5 | 1/5 |
| M-POWERCYCLE 开关机循环测试 | 1/3 | 0/1 |
| M-REGULATION 调整率测试 | 1/4 | 0/3 |
| M-RIPPLE-NOISE 输出纹波与噪声测试 | 3/6 | 0/4 |
| M-SCP 短路保护测试 | 2/3 | 0/2 |
| M-SHARE 并联均流测试 | 2/3 | 1/5 |
| M-STARTUP 启动特性测试 | 1/5 | 0/1 |
| M-TELECONTROL 遥控开关机与设定测试 | 3/4 | 1/5 |
| M-TELEMETRY 遥测精度测试 | 1/3 | 2/6 |
| M-TELESIGNAL 遥信与告警上报测试 | 2/5 | 1/8 |
| M-TEMP-RISE 温升测试 | 3/4 | 0/2 |
| M-TOUCH-CURRENT 接触电流测试 | 1/2 | 1/2 |
| M-TRANSIENT 动态响应测试 | 1/3 | 2/4 |
| M-UVLO 欠压保护测试 | 1/4 | 0/5 |
| M-UVP 输出欠压保护测试 | 1/2 | 0/2 |

（分母 = 该方法的 measures/规则总数；未落地/不可达原因多为对应术语无规格书实例。）

### b.4 公理不可达 38（附关联规则）

`A-APERTURE-JITTER`(K-MSR-103)、`A-BOOST-DUTY`(K-PWR-004)、`A-BRIDGEPEAK`(K-PWR-117)、`A-BUCK-DUTY`(K-PWR-004)、`A-CAL-RESIDUAL`(K-CAL-101)、`A-CM`(K-MSR-001)、`A-DCPOWER-I2R`(K-ELEC-004)、`A-DCPOWER-V2R`(K-ELEC-005)、`A-DERATE-BY-V`(K-CAL-103)、`A-ENOB`(K-MSR-104)、`A-ESRLOSS`(K-PWR-005)、`A-GRR`(K-FIX-016)、`A-INSTRUMENT-3X`(K-FIX-001)、`A-JOULE`(K-ELEC-004)、`A-KELVIN`(K-FIX-009/010)、`A-LOOP-GM`(K-LOOP-003)、`A-LOOP-PM`(K-LOOP-002)、`A-LOSS`(K-PWR-002)、`A-LOWLINE-RATED`(K-PWR-122/123)、`A-LSB`(K-MSR-101)、`A-NTC`(K-TLM-104)、`A-OHM`(K-ELEC-002/003)、`A-POWERTRIANGLE`(K-AC-003)、`A-PROBE-CHANNELS`(K-FIX-006)、`A-PROBE-LIFE`(K-FIX-007/014)、`A-QUANTNOISE`(K-MSR-102)、`A-REACTIVE`(K-AC-003)、`A-RESOLUTION-10TO1`(K-FIX-015)、`A-RSS`(K-FIX-002/017)、`A-SNR-IDEAL`(K-MSR-104)、`A-TUR`(K-MSR-001)、`A-UNCERTAINTY`(K-MSR-002)、`A-WC`(K-FIX-017)、`A-YCAP`(K-SAF-104)。
**另 4 条无任何规则关联（背景知识，维持）**：`A-MOSCOND`、`A-PARALLEL`、`A-SERIES`、`A-TEMPCOEFF`。

### b.5 属性无规格证据 163/267（按类别）

fixture 44｜control 15｜protection 14｜measurement 10｜derating 9｜safety 8｜power 8｜telemetry 7｜accuracy 7｜transient 7｜current-sharing 5｜ripple 4｜efficiency 4｜calibration 3｜ac 3｜units 3｜limits 3｜transformer 3｜regulation 3｜reliability 2｜coverage 2｜ac-dc 1｜electrical 1。
完整属性名清单见知识图谱 JSON 的 `coverage.properties_without_spec_evidence`（163 条，可按 d 节命令重新生成）。

### b.6 术语无实例 85/168（有实例 83）

- communication (6): `T-DRYCONTACT` 干接点、`T-ENABLE` 使能信号、`T-PGOOD` 电源正常信号、`T-REMOTESENSE` 远端补偿、`T-TELEADJUST` 遥调、`T-WDT` 看门狗
- dynamics (4): `T-FC` 交越频率、`T-GM` 增益裕度、`T-LINETRANSIENT` 输入瞬态、`T-PM` 相位裕度
- efficiency (1): `T-LOSS` 损耗
- emc (5): `T-CS` 传导抗扰度、`T-EMC` 电磁兼容、`T-EN61000-3-2` 谐波电流限值、`T-RS` 辐射抗扰度、`T-YCAP` Y 电容
- environment (1): `T-SHORTTERM` 短期工作
- general (39): `T-APERTUREDELAY` 孔径延迟、`T-APERTUREJITTER` 孔径抖动、`T-APPARENTPOWER` 视在功率、`T-BOOST` 升压变换器、`T-BUCK` 降压变换器、`T-CALIB` 校准、`T-CM` 测量能力指数、`T-DUTY` 占空比、`T-ENOB` 有效位数、`T-ESR` 等效串联电阻、`T-FLYBACK` 反激变换器、`T-FORWARD` 正激变换器、`T-FSR` 量程、`T-GRR` 量具重复性与再现性、`T-KELVIN4W` 开尔文四线法、`T-LSB` 最低有效位、`T-MANDATORY` 强制需求、`T-POGOPIN` 弹簧探针、`T-POWER` 功率、`T-QUANTNOISE` 量化噪声、`T-REACTIVEPOWER` 无功功率、`T-REALPOWER` 有功功率、`T-RECOMMENDED` 推荐需求、`T-RESISTANCE` 电阻、`T-RESOLUTION` 分辨率、`T-RIPPLERATING` 纹波电流额定、`T-RMS` 有效值、`T-RSS` 均方根公差合成、`T-SAMPLERATE` 采样率、`T-SATCURRENT` 饱和电流、`T-SFDR` 无杂散动态范围、`T-SNR` 信纳比、`T-TUR` 测试不确定度比、`T-TURNRATIO` 匝比、`T-UNCERT` 测量不确定度、`T-VPEAK` 峰值电压、`T-VPP` 峰峰值电压、`T-WC` 最坏情况公差合成、`T-WCSTACK` 最坏情况公差链
- input (3): `T-ACDC` 交流-直流变换、`T-PFC` 功率因数校正、`T-RECTBUS` 整流母线电压
- output (6): `T-CROSSREG` 交叉调整率、`T-DROOP` 下垂法、`T-DROOP-GEN` 下垂、`T-HOLDUP` 保持时间、`T-LOWLINEDERATE` 低线输入降额、`T-RIPPLEVOLT` 纹波电压
- protection (7): `T-FAULTLATCH` 故障锁存、`T-FOLDBACK` 折返保护、`T-HICCUP` 打嗝保护、`T-LATCHOFF` 锁存关断、`T-NPLUS1` N+1 冗余、`T-OPP` 过功率保护、`T-UVP` 欠压保护
- reliability (5): `T-AVAIL` 可用度、`T-BURNIN` 老化、`T-DERATE` 降额、`T-DERATECURVE` 降额曲线、`T-MTTR` 平均修复时间
- safety (3): `T-CLEARANCE` 电气间隙、`T-SAFSEQ` 安规测试顺序、`T-Y1Y2` Y 电容耐压分级
- standards (1): `T-IEC62368` 音视频/信息技术设备安全标准
- thermal (4): `T-JTEMP` 结温、`T-NTC` 负温度系数热敏电阻、`T-RTH` 热阻、`T-THERMALCHAMBER` 温度箱

## c) 优先级（后续动作）

1. **fixture 属性簇（44/163）**：最大缺口，属产测工装/计量侧；需引入"工装规格/计量台账"数据面才可收敛。
2. **术语无实例规则 44 条**：计量/夹具/测试基础概念，产品规格书天然不出现；建议接受为方法论规则，或由工装域规格补锚。
3. **无实例术语 85 条**：同上（general 39 条为主），新规格书/工装数据接入后自然收敛。
4. **公理不可达 38 条**：随规则可达继续收敛（本波 44→38）；无规则 4 条维持。
5. **未匹配 11 条**：GDC `-` 结案（语料不可恢复）；PA601 10 条整机要求陈述句保持现状。
6. **回归机制**：每个波次语料扩展后按 d 节命令复跑本报告。

## d) 复现命令

审计脚本位于本机 `C:\Users\chenp\AppData\Local\Temp\opencode\matrag_gaps\`（不污染仓库）；工作目录 `F:\Workspace\MATRag`。

| 用途 | 命令 |
|---|---|
| 缺口重扫（原始 JSON） | `.venv\Scripts\python.exe -X utf8 <Temp>\matrag_gaps\scan_gaps.py` |
| 当前缺口清单（本报告 b 节） | `.venv\Scripts\python.exe -X utf8 <Temp>\matrag_gaps\report_gaps.py` |
| KG 数字（stats/coverage/validate） | `.venv\Scripts\python.exe -X utf8 <Temp>\matrag_gaps\kg_numbers.py` |
| 产物新鲜度 | `.venv\Scripts\python.exe -X utf8 <Temp>\matrag_gaps\check_generated.py` |
| 交叉核对 / 被跳过表 / 行级上下文 | `cross_checks.py` / `skipped_tables.py` / `row_context.py`（同目录） |
| 知识层重建 | `.venv\Scripts\python.exe scripts\import_domain.py` |
| 全仓测试 | `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`（当前 1206 passed） |
| 静态检查 | `.venv\Scripts\python.exe -m ruff check src tests` |

## 附录 A：未匹配 11 条明细

| # | 产品 | 参数名 | 类别 | 备注 |
|---|---|---|---|---|
| 1 | GDC800T5054-UC | `-` | output | PDF 合并格丢名；源 PDF 不在库，无法恢复 |
| 2 | PA601-D54A | MOS短路不起火要求 | reliability | 整机级要求陈述句 |
| 3 | PA601-D54A | 三防要求 | other | 同上（工艺要求） |
| 4 | PA601-D54A | 其他底线安全性要求 | reliability | 同上 |
| 5 | PA601-D54A | 器件要求 | other | 同上 |
| 6 | PA601-D54A | 失效隔离 | other | 同上 |
| 7 | PA601-D54A | 安规其他测试项目 | safety | 同上 |
| 8 | PA601-D54A | 混插要求 | other | 同上 |
| 9 | PA601-D54A | 电源保护要求 | reliability | 同上 |
| 10 | PA601-D54A | 直通电源过载不起火要求 | reliability | 同上 |
| 11 | PA601-D54A | 进水氢爆防护要求 | reliability | 同上 |

## 未复核项

1. 旧报告提及的历史探针 `coverage_check.py` 不在库中，未逐项核对历史口径差异；本报告以知识图谱 `coverage()` API 与本机脚本复核。
2. 未纳入 LLM 语义判断（确定性只读审计）。
3. 语料仅 1 处修复（CY t7-0 两行名，改前有备份），其余只读；`index.db` 与检索侧未改动。
4. "要求陈述句"是否应引入"要求型术语"锚（b.1 的 PA601 10 条）未决策，交产品主线定夺。
