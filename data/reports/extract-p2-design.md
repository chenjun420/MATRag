# P2 抽取投影设计要点（波 9 工作笔记）

> 来源: 对 ATEStudio 消费侧的逐文件反读 (schemas/aterag_bundle.py、api/v1/imports.py、
> services/flow_planner.py、services/aterag_importer.py、services/aterag_script_emit.py、
> scripts/verify_e2e.py、scripts/verify_bindings.py、config/test_method_bindings.yaml、
> docs/操作手册-ATERag规格评审.md) + MATRag 语料实测 (PA601/CY0177/GDC800 三份 doc.json)。
> 本文件是生产侧实现的唯一设计依据；与消费侧冲突处, 以消费侧源码为准。

## 1. 消费侧硬约束（生产侧必须满足）

- **契约**: bundle_version="1.0"; contract_hash 必须 = 59a852c26ca277e4141c6a60ae723b20
  (已在 matrag pydantic 2.14 下验证镜像 SHA 一致); `extra=forbid` 处处生效, 不得多字段。
- **requirement_code** 唯一; **scenario.seq** 每需求唯一 (case_code=`{code}-S{seq:03d}`);
  scenario_id 唯一。
- **cond_fingerprint 必填**: 缺失 → 导入器记 unmapped 并**跳过该条件**(静默丢测试)。
  且它是"签字身份": 重导时指纹变 → 用例回退 draft。故只覆盖语义 (kind/side/value/method_ref), 不覆盖 text。
- **限值物化**: `limits[]` 中 min/typ/max 至少一项非 None 才生成 TestLimit;
  limit_id=`aterag-{req_code}-{rail|main}-{qualifier|na}`。rail≤32, qualifier≤64。
- **kind 证据链 (DRIVER_TABLE)**: 动作 nature=measure 的 kind 才需要数值判据, 且
  判据从 `clause.value` 的 min/typ/max 取 (`limits_of(value)`) —— measure 类 kind:
  output_voltage, output_current(两侧), output_power, ripple, efficiency, timing。
  **这些 kind 的 clause.value 必须带数值**, 否则 verify_e2e 报"无判据的测量步骤"。
  verify 类 (signal_state/telemetry_value/protection_action/presence/cap_load) 无数值要求。
- **规划器读法**: 每个 scenario 会遍历需求的**全部** input_conditions(作 setup)与
  output_conditions(作测量); SETUP_ORDER 按输入 kind 排序; 段内按 (action,value,bindings,rail) 去重;
  setup 签名 = rail + scen.bindings + input 条件 value(telemetry/sequence 组除外) → **同签名需求同段**。
  结论: 不同判据值(不同轨/不同负载)必须拆成不同需求, 不能塞进同一需求的多个场景。
- **红线**: status=draft 的条件不进执行序列, 走 pending 上报; 人工在 ATEStudio 签字后才纳入
  (端点用 cond_fingerprint 把库内 approved 单向覆盖回 bundle)。行业方法提案 (规格书未写、由方法层补的)
  → draft; 规格书明确写的 → approved。生产侧**绝不**代替人签字。
- **验收脚本口径** (verify_e2e.py 10 项): 计划非空 / 红线无泄漏 / draft 全上报 /
  measure 步骤判据齐 / 步 id 全局唯一 / 平台 DSL parser 接受 / 逐台稳定等待≤30min / 无未映射场景。
- **verify_bindings.py**: 对真实 bundle 与绑定表 (kind,side) 双向差集; 未绑定=FAIL(排不出步骤), 空绑定=WARN。
- **导入幂等**: requirements 键 (product_code, requirement_code); conditions 键 (owner, cond_fingerprint);
  重导相同 bundle 必须 0 变更 (字段逐一比较, 空串→None 归一)。

## 2. 词表与口径

- clause.kind ∈ 25 个 (kind,side) 对, 已固化在 `src/matrag/extract/kinds.py`
  (含 measure 类集合的消费侧证据见上)。measure 类欠数值 → 记 review, 不造数。
- grade 列语义: 含"不要求" → 排除 (进 bundle.excluded, reason+matched_word="不要求");
  强制/推荐/"-"(空) → 正常抽取。**排除也必须可审计**。
- 非产测章节 (结构/工艺/外观/标识/文档/材料/包装运输/可靠性指标/EMC/安规实验室项/环境试验)
  → 本轮不进 requirements, 进 excluded (reason="非产测范围: …")。行业方法提案扩展留后续波。
- 源文: `data/corpus/*.doc.json` (blocks 内嵌 table; 212~…块)。
  product_code = doc_id.split(" ")[0] (PA601-D54A / CY0177-75V-20A / GDC800T5054-UC, 与 spec_params 同步)。
  doc_version: 从修订记录表 (表头含"版本号/修订版本") 取最后一行值 (PA601→B; 无表则 "")。
- 需求粒度: **变体 = (SR 编号, 轨/支路, 判据数值集)**; 每变体一个 requirement, 默认 1 个 scenario。
  同一编号多判据 → requirement_code 加后缀 `-m{数值}` (legacy 风格, 如 -m0.85/-m11.1; 数值取该变体
  min 或 max 的代表值, 格式化去尾零); 仍冲突则追加 `-c{2..}`。后缀规则确定、可测。
- 输入侧条件 (input_voltage 等) 只从原文明确数值派生; "额定输入"无数字 → 用产品基线
  (从该产品"输入工作电压范围/标称"行派生: min/max 取工作范围, typ 取最大标称档) —— 数据驱动、留痕 notes。
- 遥信/告警 → signal_state(output); 遥控 → command(input); 遥测 → telemetry_value(output)。
- 保护表 → protection_action(output) 承载判据; 行文含"短路"再加 fault_stimulus(input, destructive 段自动隔离);
  输入过/欠压 → protection_action; 输出过流/过压/过温 → protection_action。
- clause.value 形状: 数值类 {"min":f,"typ":f,"max":f,"unit":s} (缺项省略); 事件/命令类 {"event":s} 或 {"value":s};
  纯定性 → None。单位口径随绑定表 (mVpp/uF/degC/%)。
- scenario: name=工况短名 (≤200), rail=支路名 ("-54V"/"3.45V"/""), bindings 记档位/负载词 (如
  {"input_tier":"220Vac","load":"50%"}), basis=证据 (限定到 200 字符: 行文本片段), source="spec"。
- review 项: LLM 跳过/映射存疑/measure 缺数值/编号重复消歧, 全部落 bundle.review (可审计降级)。

## 3. 模块与验收

- `extract/bundle.py` 契约镜像 ✅ hash 已验证一致; `extract/kinds.py` ✅; `extract/fingerprints.py` ✅。
- 待写: `patterns.py`(名称/语境→kind 映射表+值解析辅助), `spec_tables.py`(确定性表→requirements),
  `narrative.py`(LLM 提案, draft 红线+事实护栏: 数值须现于原文、method_ref 须存在于方法目录),
  `pipeline.py`(装配+指纹+统计+fail-fast), `__init__.py`。
- 脚本: `scripts/extract_bundle.py` (doc→bundle.json), `scripts/verify_bundle_ate.py`
  (镜像校验 + 消费侧源码内 plan_flow dry-run + plan_yaml 落盘; HTTP 模式留给可达的 ATEStudio)。
- 测试全离线: 真实语料 (data/corpus) 上 pin 数量; stub LLM; 契约 hash pin;
  指纹稳定性; 幂等 (两次导出 bundle_fingerprint 相同); kind 闸门; draft 红线。
- LLM 冒烟: 真实 MaaS 一轮 (narrative 章节), 结果与降级如实记录。
