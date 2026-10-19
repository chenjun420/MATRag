# 设备本体 catalog 数据采集说明 (2026-10-18)

## 版本 / 状态

- **v1 (2026-10-18 早)**: 13 条 instruments (ac_source / eload / dmm / scope / power_meter /
  dc_source / safety_tester), 训练知识为主
- **v2 (2026-10-18 中)**: 同 13 条, 通过 TAVILY/EXA/FIRECRAWL 三路径 websearch 校验 + distributor/PDF URL 替换训练知识
- **v3 (2026-10-18 晚)**: 在 v2 基础上新增 15 条 (yokogawa-wt310hc/wt332/wt5000, keysight-n6700,
  itech-it-m3900, gwinstek-psw, rigol-dl3031/dg1000, kikusui-pmx/plz, hioki-pw3335/rm3544,
  chroma-11210/19032/61505); 数据来源仍为多路 websearch

## 背景

PA601 端到端验证（spec §5 A.4 + e2e_pa601_virtual.py）暴露了第 4 层知识缺口：
**测试设备本体**（PSU/DMM/Safety Tester 等具体型号的 spec）当前是写死常量字符串，
无法跨产品泛化（PA601 54V vs GDC 5V vs CY0177 75V）。

须新建 `data/domain/source/instrument_catalog.yaml` 给接线可视化 + 虚拟仿真一个
**结构化数据源**。

## 工具资源

4 个 web search API 服务 + 2 个直链 fetch, 全部经沙箱实测可达：

| 服务 | URL | 实测结果 |
|---|---|---|
| **TINYFISH** | api.tinyfish.ai | ❌ DNS resolution failure (沙箱拦截) |
| **TAVILY** | api.tavily.com/search | ✅ 200 OK, 返回 PDF/HTML snippet + URL + title |
| **EXA** | api.exa.ai/search | ✅ 200 OK, 返回 url + title (snippet 较长) |
| **FIRECRAWL** | api.firecrawl.dev/v1/search | ✅ 200 OK, 返回 web 抓取结果 (含 description) |
| **webfetch** | (直链) | ⚠️ 例外 site 反爬 (keysight.com 403, chromausa.com 403); distributor 镜像稳定 |

## 12 型号 × 3 路径 = 36 queries

每型号 3 路查询, 选 top-3 URL, 优先:
1. 厂商主站 PDF (vendor_official_pdf)
2. distributor (mouser/testequity/tequipment/datatec/axiomtest/finaltest)
3. KEYSIGHT/Tek/Chroma 官方 download.tek.com 等路径

## 知识获取

| 维度 | 来源 |
|---|---|
| voltage_max / current_max / power_max | TAVILY snippet 直接抽 (chromaate 系列 datasheet 镜像) |
| accuracy_basic | EXA 搜 PDF 内容 / TAVILY snippet |
| protocols / terminals | TAVILY snippet 含 "SCPI/GPIB/USB/LAN" / "4-wire Kelvin" 等 |
| compatible_methods | 反向推: 看 M-XXX::instruments 字符串 vs catalog 字段是否对齐 |
| data_provenance_note | 厂商主站可达 → vendor_official_site / vendor_official_pdf; 仅 distributor / training 知识可达 → 标对应源 |

## 数据校验

每个 entry 必填字段:
- id (lowercase + hyphen)
- vendor / model
- category ∈ {ac_source, eload, dmm, scope, power_meter, dc_source, safety_tester}
- key_specs (类别相关子集, 非空)
- protocols / terminals (列表)
- data_source (4 类枚举)
- references (URL 列表, sandbox 内 webfetch 不强求 200 但应 manual verify)

校验由 `matrag.domain.instruments._parse_one` 完成。

## 双向 cross-validation

`cross_validate_with_methods(catalog)` 找出 power_test_methods.yaml 中存在但
catalog 中**无任何仪器显式适配**的 M-* 方法 (gap 报告, 不阻断, e2e 仿真继续)。

## 数据局限 (与缺口)

1. **Yokogawa WT310 vs WT310HC vs WT310EH**: 同系列不同功率, 录入基本款 WT310 (20A/1 element);
   完整系列应单独条目 (WT310HC, WT310EH, WT332, WT333)
2. **Tek MSO46 带宽选项**: 200/350/500/1000/1500 MHz 五个选项只录基本 spec; 实际产品
   按带宽升级级差计价
3. **Chroma 61705 是 3-phase**: 与 61605 单相不同; 数据已用 `phases: 3` 标注
4. **DMM 精度细分**: 多种 range 多 speed (NPLC), 这里只录 1-yr 24h 1V range typical;
   完整精度表见各厂商 datasheet

## 后续补齐 (manual)

- Chroma 11020/11050 系列绝缘耐压测试仪
- Gwinstek 系列产品 (PSW / PEL / GDS 系列)
- 横河 WT310HC/WT310EH/WT332/WT333
- ITECH IT-M 系列 (实际产测常见, 200W~30kW)
- Sorensen / TDK-Lambda (DC source)

## 已知 sandbox 限制

- chromausa.com 直接 webfetch 返回 403 (Cloudflare challenge); 走 distributor / PDF 镜像
- keysight.com 部分页 Cloudflare browser challenge; 走 kc-usercontent.com 直链 PDF
- websearch (opencode 内建) HTTP 402 计费限制
- TINYFISH (api.tinyfish.ai) DNS resolution failure
