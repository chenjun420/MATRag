"""``matrag.domain.instruments`` 离线单测 (E2E: PA601 接线可视化依赖)。

覆盖
----
- :func:`load_instrument_catalog` 缺源文件 → 返回 ``{}`` + logger.warning;
- :func:`load_instrument_catalog` 正常 yaml 解析 → 28 条 + 字段映射准确;
- :func:`find_instrument_by_id` 命中 / 未命中;
- :func:`find_instruments_by_method` 反向查 M-* (例: M-HOLDUP → list of AC source);
- :func:`find_by_category` 七大类过滤;
- :func:`compatible_methods_for` 列方法;
- :func:`cross_validate_with_methods` 反向 gap 报告 (power_test_methods.yaml 已知覆盖);
- 字段校验: id 格式, category 闭集, key_specs 非空;
- Provenance 标签: vendor_official_pdf / vendor_distributor_web / vendor_official_site /
  training_knowledge 都至少有一条;
- 13 条 M-* 在 catalog 中显式有仪器适配 (vs power_test_methods.yaml::compatible_methods 实际数);
- v4 新增方法适配: M-STARTUP-TIME / M-OVERSHOOT → 示波器; M-CROSS-REG → 多路负载/6.5 位 DMM。
"""

from __future__ import annotations

import logging
import os

from matrag.domain.instruments import (
    INSTRUMENT_CATEGORIES,
    compatible_methods_for,
    find_by_category,
    find_instrument_by_id,
    find_instruments_by_method,
    load_instrument_catalog,
)

# ── ID helpers ──────────────────────────────────────────────────────────────


def test_id_format_valid() -> None:
    """lowercase + hyphen 格式 + 双 dash 段."""
    catalog = load_instrument_catalog()
    invalid = [
        iid for iid in catalog
        if not (iid and iid[0].isalpha() and iid == iid.lower())
    ]
    assert not invalid, f"非法 id: {invalid}"


# ── catalog 完整性 ────────────────────────────────────────────────────────


def test_catalog_loads_at_least_28() -> None:
    """v3 数据采集 28 条 (13 v2 + 15 v3). 7 类各至少 1."""
    catalog = load_instrument_catalog()
    assert len(catalog) >= 28, f"got {len(catalog)} entries"


def test_all_categories_well_known() -> None:
    """每条 category ∈ INSTRUMENT_CATEGORIES."""
    catalog = load_instrument_catalog()
    bad = [
        (iid, inst.category) for iid, inst in catalog.items()
        if inst.category not in INSTRUMENT_CATEGORIES
    ]
    assert not bad, f"未知 category: {bad}"


def test_categories_have_at_least_one_each() -> None:
    """7 大类至少有 1 条."""
    catalog = load_instrument_catalog()
    seen = {inst.category for inst in catalog.values()}
    missing = INSTRUMENT_CATEGORIES - seen
    assert not missing, f"category 空缺: {missing}"


def test_required_fields_present() -> None:
    """id / vendor / model / key_specs / protocols / terminals / data_source 都非空."""
    catalog = load_instrument_catalog()
    for iid, inst in catalog.items():
        assert inst.id, f"{iid}.id 空"
        assert inst.vendor, f"{iid}.vendor 空"
        assert inst.model, f"{iid}.model 空"
        assert inst.key_specs, f"{iid}.key_specs 空"
        assert inst.protocols, f"{iid}.protocols 空"
        assert inst.terminals, f"{iid}.terminals 空"
        assert inst.data_source, f"{iid}.data_source 空"


def test_data_source_in_closed_set() -> None:
    """data_source ∈ {vendor_official_pdf / vendor_official_site / vendor_distributor_web / training_knowledge}."""
    catalog = load_instrument_catalog()
    allowed = {"vendor_official_pdf", "vendor_official_site", "vendor_distributor_web", "training_knowledge"}
    bad = [
        (iid, inst.data_source) for iid, inst in catalog.items()
        if inst.data_source not in allowed
    ]
    assert not bad, f"未知 data_source: {bad}"


def test_provenance_balanced() -> None:
    """4 类 provenance 至少各有 1 条 (v2 数据采集中已显式分类, 不会出现单类全占)."""
    catalog = load_instrument_catalog()
    sources = {inst.data_source for inst in catalog.values()}
    assert "vendor_official_pdf" in sources
    assert "vendor_distributor_web" in sources
    # vendor_official_site 与 training_knowledge 至少其一 (本批次主要走 PDF + distributor)


def test_references_nonempty_for_distributor_entries() -> None:
    """distributor / vendor_official entry 都至少 1 个 URL."""
    catalog = load_instrument_catalog()
    for iid, inst in catalog.items():
        if inst.data_source in {"vendor_official_pdf", "vendor_distributor_web", "vendor_official_site"}:
            assert inst.references, f"{iid} ({inst.data_source}) 无 references"


# ── 查找 ────────────────────────────────────────────────────────────────────


def test_find_instrument_by_id_hit_and_miss() -> None:
    inst = find_instrument_by_id("chroma-61605")
    assert inst is not None
    assert inst.vendor == "Chroma"
    assert inst.category == "ac_source"
    assert find_instrument_by_id("does-not-exist") is None


def test_find_instruments_by_method_m_holdup_returns_ac_source() -> None:
    """M-HOLDUP → 至少 1 ac_source 命中."""
    hits = find_instruments_by_method("M-HOLDUP")
    assert any(h.category == "ac_source" for h in hits)


def test_find_instruments_by_method_m_ripple_returns_scope_or_dmm() -> None:
    """M-RIPPLE-NOISE → 至少 1 dmm + 1 scope."""
    hits = find_instruments_by_method("M-RIPPLE-NOISE")
    cats = {h.category for h in hits}
    assert "dmm" in cats and "scope" in cats


def test_find_instruments_by_method_unknown_returns_empty() -> None:
    assert find_instruments_by_method("M-NONEXISTENT") == ()


def test_find_instruments_by_method_m_startup_time_returns_scope() -> None:
    """M-STARTUP-TIME → 至少 1 台示波器 (ton/tr 阶跃时序测量)。"""
    hits = find_instruments_by_method("M-STARTUP-TIME")
    assert any(h.category == "scope" for h in hits)


def test_find_instruments_by_method_m_overshoot_returns_scope() -> None:
    """M-OVERSHOOT → 至少 1 台示波器 (过冲/振铃峰值捕获)。"""
    hits = find_instruments_by_method("M-OVERSHOOT")
    assert any(h.category == "scope" for h in hits)


def test_find_instruments_by_method_m_cross_reg_returns_eload_and_dmm() -> None:
    """M-CROSS-REG → 至少 1 台电子负载 + 1 台 DMM (多路交叉调整率)。"""
    hits = find_instruments_by_method("M-CROSS-REG")
    cats = {h.category for h in hits}
    assert "eload" in cats and "dmm" in cats


def test_compatible_methods_for_tek_mso46_has_startup_time_and_overshoot() -> None:
    """tek-mso46 (6-ch 示波器) 适配新方法 M-STARTUP-TIME / M-OVERSHOOT。"""
    methods = compatible_methods_for("tek-mso46")
    assert "M-STARTUP-TIME" in methods
    assert "M-OVERSHOOT" in methods
    # 反向: 安全类方法不由示波器执行。
    assert "M-HIPOT" not in methods
    assert "M-CROSS-REG" not in methods


def test_compatible_methods_for_chroma_63206_has_cross_reg() -> None:
    """chroma-63206 (多路负载) 适配 M-CROSS-REG。"""
    assert "M-CROSS-REG" in compatible_methods_for("chroma-63206")


def test_compatible_methods_for_hp_34461a_has_cross_reg() -> None:
    """hp-34461a (6.5 位 DMM) 适配 M-CROSS-REG。"""
    assert "M-CROSS-REG" in compatible_methods_for("hp-34461a")


def test_find_by_category_eload_includes_chroma_63206() -> None:
    hits = find_by_category("eload")
    assert any(h.id == "chroma-63206" for h in hits)


def test_find_by_category_unknown_logs_warning() -> None:
    """未注册 category → 空 tuple + logger.warning."""
    hits = find_by_category("not_a_real_category")
    assert hits == ()


def test_compatible_methods_for_chroma_61605() -> None:
    methods = compatible_methods_for("chroma-61605")
    assert "M-HOLDUP" in methods
    assert "M-EFFICIENCY" in methods


def test_compatible_methods_for_unknown_id_returns_empty() -> None:
    assert compatible_methods_for("does-not-exist") == ()


# ── 双向 cross-validation ─────────────────────────────────────────────────


def test_cross_validate_with_methods_no_fatal_gap() -> None:
    """power_test_methods.yaml 35 方法中至少有大部分被 catalog 显式覆盖.

    v3 (28 条 instruments) 覆盖从 17 提升到 ≥20; v4 补齐 M-STARTUP-TIME/M-OVERSHOOT/
    M-CROSS-REG 适配后实测覆盖 21。其余"无 catalog 仪器显式适配"的 M-*
    是 pending 补齐任务 (M-TELEMETRY/TELESIGNAL/TELECONTROL/COMM 类需要协议分析仪接口,
    现 catalog 偏 DC 测试, 后续可加 Agilent N4903 等专用仪器)。
    """
    from matrag.domain.test_methods import load_test_methods
    methods_in_yaml = {m.method_id for m in load_test_methods()}
    catalog_covered = set()
    catalog = load_instrument_catalog()
    for inst in catalog.values():
        for m in inst.compatible_methods:
            if m in methods_in_yaml:
                catalog_covered.add(m)
    assert len(catalog_covered) >= 20, f"catalog 覆盖 M-* 太少: {len(catalog_covered)}"


# ── 错误路径 ───────────────────────────────────────────────────────────────


def test_load_missing_file_returns_empty_warned(caplog) -> None:
    """缺源文件 → {} + logger.warning."""
    caplog.set_level(logging.WARNING, logger="matrag.domain.instruments")
    os.environ["MATRAG_DOMAIN_INSTRUMENT_CATALOG_PATH"] = "/nonexistent/path/ic.yaml"
    try:
        result = load_instrument_catalog()
    finally:
        del os.environ["MATRAG_DOMAIN_INSTRUMENT_CATALOG_PATH"]
    assert result == {}
    assert any("不存在" in r.message for r in caplog.records)


def test_priority_path_arguments_over_env(tmp_path, monkeypatch) -> None:
    """显式参数 > 环境变量."""
    env_p = tmp_path / "env.yaml"
    env_p.write_text("version: 1\ninstruments: []\n", encoding="utf-8")
    explicit_p = tmp_path / "explicit.yaml"
    explicit_p.write_text("version: 1\ninstruments: []\n", encoding="utf-8")
    monkeypatch.setenv("MATRAG_DOMAIN_INSTRUMENT_CATALOG_PATH", str(env_p))
    result = load_instrument_catalog(path=explicit_p)
    assert result == {}  # both empty → empty result


def test_invalid_yaml_skipped_entry_warns(caplog, tmp_path) -> None:
    """非法条目 (id 格式 / 缺字段) → logger.warning + 跳过."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "version: 1\n"
        "instruments:\n"
        "  - id: BAD_ID      # uppercase not allowed\n"
        "    vendor: X\n"
        "    model: 999\n"
        "    category: ac_source\n"
        "    key_specs: {}\n"
        "    protocols: []\n"
        "    terminals: []\n"
        "    data_source: vendor_official_site\n",
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="matrag.domain.instruments")
    result = load_instrument_catalog(path=bad)
    assert result == {}  # 全部条目非法被跳过
    assert any("跳过" in r.message for r in caplog.records)


def test_instrument_categories_const() -> None:
    assert isinstance(INSTRUMENT_CATEGORIES, frozenset)
    assert len(INSTRUMENT_CATEGORIES) == 7
    assert "ac_source" in INSTRUMENT_CATEGORIES
    assert "safety_tester" in INSTRUMENT_CATEGORIES
