# 端到端数据缺口评估（2026-10-19）

**源基准**：业界电源产品产测用例（tek / APTPCB / Seaward / 80 PLUS / OpenCompute 48V /
AT&T Practices / Chrome Systems Solutions），与 MATRag 当前 catalog/aliases/bundle/pa601 dry-run
覆盖率对照。

## 1. 业界产测用例 23 项基准（业界共识）

| 序号 | 类别 | 用例名称 | 主要测量 | 业界引用 |
|---|---|---|---|---|
| TC-01 | 安全 | **HIPOT (Dielectric Withstand)** | primary/secondary 4-5kVac, 漏电流 ≤ mA | IEC 62368-1; UL |
| TC-02 | 安全 | **Insulation Resistance** | 500Vdc, ≥10MΩ (typ ≥ 100MΩ) | IEC 62368-1 |
| TC-03 | 安全 | **Ground Bond / Continuity** | 25A/30A, ≤0.1Ω | IEC 62368-1 |
| TC-04 | 安全 | **Touch Current / Leakage** | ≤3.5mA AC | IEC 60990 |
| TC-05 | 输入 | **AC Source Power-Up / Drop Sequence** | turn-on/off timing | 80 PLUS; Tek |
| TC-06 | 输入 | **Inrush Current** | peak A within 50ms | ICE-3 5/8; UL |
| TC-07 | 输入 | **Hold-up Time** | ≥16-20ms @ 80% nom. V | IEC 61000-4-11 |
| TC-08 | 输入 | **UVLO / OVLO threshold** | trip / recovery hysteresis | IEC 61204 |
| TC-09 | 输入 | **Power Factor / Harmonics** | PF ≥0.9; THD per Class A/B/D | IEC 61000-3-2 |
| TC-10 | 输出 | **Output Voltage Tolerance** | ±5% min/typ/max | 通用 |
| TC-11 | 输出 | **Line / Load Regulation** | ≤1% | 通用 |
| TC-12 | 输出 | **Cross Regulation** | multi-rail ± 2% | 通用 |
| TC-13 | 输出 | **Ripple & Noise** | ≤1% Vout (often ≤50-100mVpp) | IEC 61204-3; JEITA-RC9131A |
| TC-14 | 输出 | **Transient Response** | step load 25/50/75%, recovery ≤500µs | IEC 61204 |
| TC-15 | 输出 | **Turn-On Time / Rise Time** | <100ms sometimes | 通用 |
| TC-16 | 输出 | **Overshoot on Turn-On** | ≤5% | 通用 |
| TC-17 | 输出 | **Efficiency / Power Loss** | η curve 25/50/75/100% load | 80 PLUS; ETSI EN 303470 |
| TC-18 | 保护 | **OCP (Over Current)** | 110-150% rated I, cycle or foldback | IEC 60950-1; IEC 62368-1 |
| TC-19 | 保护 | **OVP (Over Voltage)** | 110-130% rated V, latch | IEC 61204-3 |
| TC-20 | 保护 | **UVP (Under Voltage)** | 80-90% rated V, latch or auto-recovery | IEC 61204 |
| TC-21 | 保护 | **SCP (Short Circuit)** | continuous vs hiccup recovery | IEC 61204 |
| TC-22 | 保护 | **OTP (Over Temperature)** | thermistor trip, latch or auto | 通用 |
| TC-23 | 环境 | **Burn-In** | 48hr or 168hr, 50°C, full load | Tek; ActionPower; OCP 48V |

## 2. MATRag 当前覆盖矩阵

| TC  | M-* (catalog) | external_ref | catalog 仪器 | comm sim | 真机 demo | 覆盖率 |
|-----|---|---|---|---|---|---|
| 01  | M-HIPOT ✅ | iec62368/60335/61010/GB4943 ✅ | chroma-19020/19032, keysight-e4980a ✅ | — | step1 *IDN? ✅ | **高** |
| 02  | M-INSULATION-RES ✅ | iec62368/60950/4-wire ✅ | chroma-19020/19032/keysight-e4980a ✅ | — | — | **高** |
| 03  | M-GROUND-BOND ✅ | (none) | chroma-19020/19032 ✅ | — | — | **中** |
| 04  | M-TOUCH-CURRENT ✅ | — | chroma-19032 ✅ | — | — | **中** |
| 05  | M-STARTUP-TIME ✅ | iec_62368-1, rohde_schwarz_startup_app_note ✅ | tek-mso46, keysight-dsox3024t ✅ | — | — | **高** |
| 06  | M-INRUSH ✅ | iec61204, IEEE ✅ | chroma-61605, 61505, rigol-dg1000 ✅ | — | — | **高** |
| 07  | M-HOLDUP ✅ | iec61000-4-11, iec61000-4-34 ✅ | chroma-61605, tek-mso46 ✅ | — | **partial** (demo 仅 set 54V 不做掉电) | **高** |
| 08  | M-UVLO ✅ M-UVP ✅ | iec61204 ✅ | (none 专属) | — | — | **中** |
| 09  | M-PF ✅ | iec61000-3-2, chroma61505 ✅ | yokogawa-wt310/332/5000, hioki-pw3335 ✅ | — | — | **高** |
| 10  | (output_voltage kind) | — | — | ✅ psu_measure_54v=54.0V | **9/9 PASS** | **高** |
| 11  | M-REGULATION ✅ | iec61204 ✅ | chroma-63206, keysight-n6700 ✅ | — | — | **高** |
| 12  | M-CROSS-REG ✅ | iec_61204/en_61204 ✅ | chroma-63206, hp-34461a ✅ | — | e2e 虚拟 ✅ 0.593% @54V | **高** |
| 13  | M-RIPPLE-NOISE ✅ | iec61204-3, jeita ✅ | tek-mso46, hp-34461a ✅ | — | — | **高** |
| 14  | M-TRANSIENT ✅ | iec61204 ✅ | chroma-63206, tek-mso46 ✅ | — | — | **高** |
| 15  | M-STARTUP ✅ | iec61204-3 ✅ | tek-mso46, gwinstek-psw ✅ | — | — | **高** |
| 16  | M-OVERSHOOT ✅ | iec_61204, iec_62368-1 ✅ | tek-mso46, keysight-dsox3024t ✅ | — | e2e 虚拟 ✅ 2.222% @54V | **高** |
| 17  | M-EFFICIENCY ✅ | 80 PLUS, ETSI EN303470, IEC61204 ✅ | yokogawa-wt310/5000, hioki-pw3335 ✅ | — | — | **高** |
| 18  | M-OCP ✅ | iec60950, iec60601, ul60950 ✅ | chroma-63206, keysight-n6700 ✅ | — | — | **高** |
| 19  | M-OVP ✅ | iec61204, iec61000-4-5 ✅ | chroma-62000h, keysight-n6700 ✅ | — | — | **高** |
| 20  | M-UVP ✅ | iec61204 ✅ | (none) | — | — | **中** |
| 21  | M-SCP ✅ | iec_61204/en_61204 ✅ | (none 专属，走 chroma-63206 OCP 通道) | — | — | **中** |
| 22  | M-OTP ✅（方法在册；旧「无 M-OTP」结论更正） | iec_61204/en_61204 ✅ | (thermo/DAQ 缺) | — | — | **中** |
| 23  | M-BURN-IN ✅（方法在册；旧「无 M-BURN-IN」结论更正） | burn_in_aging_method ✅ | (thermo/DAQ 缺) | — | — | **中** |

## 3. 缺口分级

### 3.1 严 (生产**必须**) — 状态更新 2026-10-19（第 11 波）

| 缺口 | TC 引用 | 业务影响 | 修复路径 | 当前状态 |
|---|---|---|---|---|
| **M-OTP**（旧「无 M-OTP」结论更正） | TC-22 | PSU 过温保护总测项目 — 行业惯例 + UL/IEC 62368-1 clause 9 | 温度箱 + 热电偶 + DAQ；hioki-rm3544 或专 thermo logger | **方法本体在册 + alias ✅**（iec_61204）；残留：catalog 无温箱/DAQ 条目 + 真机路径 |
| **M-BURN-IN**（旧「无 M-BURN-IN」结论更正） | TC-23 | 业界 48-168hr 老化筛选 — burn-in tester (ActionPower 等) | 温箱 + 循环负载 + 老化时长 ≥ 48hr | **方法本体在册 + alias ✅**（burn_in_aging_method）；残留：catalog 温箱/DAQ + 真机（温箱）路径 |
| ~~无 M-STARTUP-TIME~~ | TC-05/16 | 上电时序（sequencing） & overshoot — tek mso46 8-ch sequencing 典范 | 加 method `M-STARTUP-TIME`：ton/tr/ts 测量 | **✅ 已闭环**（方法+alias+catalog：tek-mso46 / keysight-dsox3024t） |
| **M-CROSS-REG** | TC-12 | 多轨电源轨间耦合 | 加 method `M-CROSS-REG`：N rail cross-coupling 测法 | **✅ 已闭环**（chroma-63206 / hp-34461a）+ e2e 虚拟 TC-12 0.593% @54V |
| **M-OVERSHOOT** | TC-16 | 输出开通过冲 ≤5% | 示波器捕获 turn-on 峰值 | **✅ 已闭环**（方法+alias+catalog）+ e2e 虚拟 TC-16 2.222% @54V |

### 3.2 中 (业务用例)

| 缺口 | 引用 | 影响 | 修复 |
|---|---|---|---|
| **TRI sync 输入与 Tek sequencing** | TC-15 | hold-up 同步缺 instrument-specific path | 在 chuck-corpus.yaml 加 keithley-source-meter 类模板 |
| **48V-specific OCP trace** | TC-18 | OpenCompute 48V 衰减模型不一样 | 加 alias reference |
| **output overshoot 单独测量** | TC-16 | 集成在 startup 内不细化 | ✅ 已闭环：M-OVERSHOOT（v4 新增） |

### 3.3 轻 (边缘扩展)

| 项 | 建议 |
|---|---|
| **insulation-resistance 数据细则** | 现 alias YAML 是空 stub；M-INSULATION-RES 已加，但 62368-1 表格实际还需要分 1min/2min/10min 值 |
| **多 instrument interleaving** (burn-in+OT) | 需多 instrument scheduler — dep 链 + python async |
| **48V OCP-reverse recovery** | OpenCompute spec 6.2.3 — AR特性 |

## 4. cross-check vs PA601 dry-run 9 步

| Step | TC 覆盖 | 备注 |
|---|---|---|
| 1 board 连通 | n/a | TCP/IP |
| 2 VISA/TCPIP PSU *IDN? | TC-25 (供电身份) | 非测试项 |
| 3 mat-device-control /health | n/a | 健康 |
| 4 PSU psu001 *IDN? | TC-01 (hall probe) | 安全身份 |
| 5 DMM dmm001 *IDN? | TC-25 | 仪器身份 |
| 6 FILL_IN 桩 | n/a | 真链实例化 |
| **7 mini plan (54V)** | **TC-10 (output voltage tolerance)** | **54V ± 2V** |
| 8 step_results 落 DB | TC-26 (data capture) | 持久化 |
| 9 audit_report 1 条 | TC-17/13 (efficiency/ripple) | 归因链 |

→ PA601 dry-run 只覆盖 23 项中的 **2 项**（TC-10/TC-17/13 通过 cond_fingerprint 间接）。距离生
产线全套落地**还差 21 个 TC**。

**2026-10-19 更新（第 11 波）**：方法本体缺口已闭环——TC-05/16（M-STARTUP-TIME/M-OVERSHOOT）、
TC-12（M-CROSS-REG）新增方法 + alias + catalog 接线；TC-22/23（M-OTP/M-BURN-IN）方法本体在册
（旧「无 M-OTP/M-BURN-IN」结论更正），残留硬件（温箱/DAQ）条目与真机路径。虚拟 e2e
（`e2e-pa601-virtual-2026-10-19.md`，33 步 31 PASS/2 FAIL）已覆盖 TC-16/TC-12 链路。剩余差距收敛为：
ATEStudio 23 步上板计划（`pa601_full_commission.yaml`，22 个脚本 TBD）+ 真板执行。

## 5. 优先级建议（状态更新 2026-10-19）

| 序 | 项 | 估计复杂度 | 建议窗口 | 状态 |
|---|---|---|---|---|
| 1 | 加 `M-STARTUP-TIME` + `M-OVERSHOOT` (tek-mso46 已具备) | 1-2h | 本周 | **✅ 完成**（+ keysight-dsox3024t 接线） |
| 2 | 加 `M-CROSS-REG` (multi-rail) | 2h | 本周 | **✅ 完成**（chroma-63206 / hp-34461a） |
| 3 | 加恒温 / DAQ 仪器 spec 进 catalog（M-OTP/M-BURN-IN 真机前置） | 4-6h | 下周 | 🟡 虚拟基线已就位（**第 12 波 23/23 PASS** 含 TC-22/23）；真机 catalog（温箱/DAQ）条目仍待补 |
| 4 | 加 alias（OTP/BURN-IN/SCP/GROUND-BOND/TOUCH-CURRENT 等） | 1h | 下周 | **✅ 完成**（aliases v4 = 25 条） |
| 5 | PA601 真板真机填完所有 23 TC（一步扩 mini plan 到 23 步） | 1 工作日 | 用户上板后 | 脚本就绪（**第 12 波补齐 23 脚本、通过 py_compile & `--check`**）；virtual runner 23/23 PASS；真板执行仍待用户上板 |

**结论（2026-10-19 更新）**：PA601 dry-run 证明**链路通** + 最基本 TC-10 output voltage tolerance 通过；
虚拟 e2e 再覆盖 TC-16/TC-12 链路（33 步 31 PASS，2 FAIL 为既有 spec 边界用例 SR-PA601-D54A-1200/1201）。
23 项产测基线中，方法/alias/catalog 可闭环的项已全部闭环（M-STARTUP-TIME / M-OVERSHOOT / M-CROSS-REG 新增；
M-OTP / M-BURN-IN 在册）。遗留缺口的唯一形式是**硬件**（温箱/DAQ catalog 条目）与**真板上板执行**。

**结论（2026-10-19 第 12 波再更新）**：
- **2 个 e2e 历史 FAIL 已修复**（SR-PA601-D54A-1200-m-54 / -1201-m-54.8，根因为仿真/判据负轨符号处理缺陷，
  判据区间归一化 + 负轨实测值方向修正；33 步 33/0，报告 `e2e-pa601-virtual-2026-10-19.md`）。
- **PA601 23-TC 全计划在虚拟设备上跑通**：ATEStudio → MatDeviceHttpDriver → mat-device-control →
  MAT.Virtual*（9 台确定性虚拟仪器）→ 判据 → audit 收束，**23/23 PASS**；故障注入证明
  fail-fast / 判据活性（`--inject tc06=100.0`、`tc21=99.0` 触发 FAIL 并中止）。报告
  `pa601-23tc-virtual-2026-10-19.md`（端口每次随机分配，含 `[inject]` 提示一行）。
- **虚拟设备驱动已就位**：MDC `modules/device/drivers/virtual/` 9 型
  (PSU/DMM/ELoad/Scope/ACSource/PowerMeter/Safety/Thermo/Aging)，注册 `MAT.Virtual*`
  到 factory，含 `*IDN?` 与冻结方法；tests/test_virtual_drivers.py + enums 增 `VIRTUAL`；MDC 全仓
  303 passed。
- **遗留形态**：唯一硬缺口仍为**硬件**（温箱 / DAQ 真机目录条目未补、`mat-device-control`
  HTTP API 方法白名单需按 deploy 路径接管时按计划阈值扩展）与**真板上板执行**；
  虚拟基线已为上板前的工具链与判据活性提供闭环验证，差真板/规格书的最后 1 km。

## 6. 闭环记录（2026-10-19 第 11 波）

| 动作 | 产物 | 证据 |
|---|---|---|
| 方法本体：新增 M-STARTUP-TIME / M-OVERSHOOT / M-CROSS-REG | `data/domain/source/power_test_methods.yaml`（35 条） | validate 0 issue；全量 pytest 1439 passed / 0 failed |
| alias v4：25 条（新增 3 条 + 补 SCP/OTP/BURN-IN/GROUND-BOND/TOUCH-CURRENT/INPUT-SLOW/OPP/TELEMETRY） | `data/domain/source/method_ref_aliases.yaml` | check_aliases 冒烟：25 mappings / 38 reverse keys |
| catalog 接线（28 条）：tek-mso46、keysight-dsox3024t（STARTUP-TIME/OVERSHOOT）；chroma-63206、hp-34461a（CROSS-REG） | `data/domain/source/instrument_catalog.yaml` | 重新生成 jsonl 35 行；KG 1225 节点 / 4074 边 |
| e2e 虚拟场景：TC-16 / TC-12 | `scripts/e2e_pa601_virtual.py`；报告 `data/reports/e2e-pa601-virtual-2026-10-19.md` | 33 步 31 PASS / 2 FAIL（FAIL 为既有 spec 边界） |
| 上板计划：PA601 23 步 | `ATEStudio/data/commission/pa601_full_commission.yaml`、`docs/commissioning/pa601-full-plan.md` | 7 例结构测试通过；22 个脚本 TBD |

## 7. 闭环记录（2026-10-19 第 12 波 · 虚拟驱动 + e2e 修复 + 23-TC 虚拟执行）

| 动作 | 产物 | 证据 |
|---|---|---|
| mat-device-control 虚拟仪器驱动包（9 型 · MAT.Virtual*） | `mat-device-control/modules/device/drivers/virtual/{virtual_devices.py,__init__.py}`；factory 注册 + `ConnectionType.VIRTUAL`；tests/test_virtual_drivers.py | MDC 全仓 `303 passed`；`allowed_methods` 既在 devices.toml 每设备已声明、也支持顶层 `[api]` 与 env `MAT_DEVICE_METHOD_ALLOWLIST` 追加 |
| e2e 负轨判据/模型修复（SR-PA601-D54A-1200/1201） | `MATRag/scripts/e2e_pa601_virtual.py`：`_judge` 区间归一化 + `simulate_step` 负轨实测方向修正 + 报告硬编码计数行更新 | 报告 `data/reports/e2e-pa601-virtual-2026-10-19.md` 重生成，**33 PASS / 0 FAIL**；MATRag 全量 pytest **1444 passed**（+5）；ruff `src tests scripts` 无 E/F |
| ATEStudio 23 脚本补齐（22 TBD → 脚本齐） + overlay 外置 | `data/commission/scripts/*.py`（23 文件）+ `data/commission/pa601_virtual_limits.yaml`（覆盖 tc05/06/08/21/22 占位）+ 文档同步 | `--check` 全过；23 脚本均 py_compile 通过；脚本名与 plan 一一对应；含自含 `_judge`（含归一化） |
| 23-TC 虚拟执行 runner（live / --inject / --check 三模式） | `ATEStudio/scripts/commission_full_virtual.py` + 配套 pytest 25 例 | live **23/23 PASS**（端口每次随机分配），`--inject tc06=100.0` 触发 FAIL + fail-fast（退出码 1，证明判据/中止链路活性）；ATEStudio 全量 `ruff` 无 E/F |
| 文档更新 | 本报告 §5 / §6 / §7；`TODO.md` 第 12 波；`docs/commissioning/pa601-full-plan.md` 末尾追加虚拟执行章节 | 与报告本表一一对应 |

**残留缺口（按性质，2026-10-19 再更新）**：
1. **硬件目录**：温箱 / DAQ 真机 catalog 条目仍缺（虚拟基线 TC-22/23 已通过；真机上板前置）。
2. **真板执行**：23 步计划全部脚本齐 + virtual runner 23/23 PASS + 故障注入证判据活性；差 `.25` 板 +
   PSU/DMM 就位 + 替换 overlay 限值为 PA601 规格书后的端到端真机执行。
3. **MDC API 白名单扩展**：基础集 + devices.toml `allowed_methods` + env `MAT_DEVICE_METHOD_ALLOWLIST` 已能覆盖
   本波冻结方法族；上板时若新增仪器方法，按同样三处追加即可，不需改路由核心（`modules/api/routes/devices.py`
   公共契约未碰）。
4. **数据源负轨 min/max 反排**（bundle 层，已识记）：`PA601-D54A.bundle.json` 中 1201-m-54.8 仍存 `min=-53.2/max=-54.8`；
   本轮在判据/仿真层归一化；建议后续在 `extract_bundle` 端对负轨做符号归一化校验（非本波范围）。
