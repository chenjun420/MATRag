"""测试设备本体 catalog (Phase A.4 增强: PA601 E2E 接线可视化依赖)。

设计目标
--------
- 给虚拟仿真 (e2e_pa601_virtual.py) 与接线可视化一个**结构化**数据源 (替代写死的常量字符串);
- 与 ``power_test_methods.yaml::M-XXX::instruments`` (文字描述) 双向交叉: 反向解析
  ``compatible_methods`` 字段, 给 query "M-* 测法需要哪些仪器" / "Chroma 61605 适配哪些
  测试方法" 直接数据化;
- 数据缺失**不**阻断 (P1 backend)- 缺文件 → 返回 ``{}`` + ``logger.warning`` 留痕
  (项目纪律: 失败 fail-fast 但降级可查)。

公开接口
--------
- :func:`load_instrument_catalog`     加载默认 / 自定义 yaml
- :func:`find_instrument_by_id`       按 id 查
- :func:`find_instruments_by_method`  按 M-* 查所有兼容仪器 (反向查找)
- :func:`find_by_category`            按 category 查
- :func:`compatible_methods_for`      列出该仪器所有 M-* 适配
- :func:`cross_validate_with_methods` 与 power_test_methods.yaml::M-* 反向校验 (WARN)

数据源
------
``data/domain/source/instrument_catalog.yaml``, 字段约定见该文件顶部 docstring。
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from matrag.domain.test_methods import load_test_methods

__all__ = [
    "DEFAULT_INSTRUMENT_CATALOG_PATH",
    "INSTRUMENT_CATEGORIES",
    "INSTRUMENT_CATALOG_PATH_ENV",
    "Instrument",
    "InstrumentCatalogError",
    "compatible_methods_for",
    "cross_validate_with_methods",
    "find_by_category",
    "find_instrument_by_id",
    "find_instruments_by_method",
    "load_instrument_catalog",
    "resolve_instrument_catalog_path",
]

#: 默认路径 (与 power_test_methods.yaml 同目录)
DEFAULT_INSTRUMENT_CATALOG_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "domain"
    / "source"
    / "instrument_catalog.yaml"
)

#: 环境变量覆盖
INSTRUMENT_CATALOG_PATH_ENV = "MATRAG_DOMAIN_INSTRUMENT_CATALOG_PATH"

#: 合法 category 集合 (硬约束, 与 yaml 字段对照)
INSTRUMENT_CATEGORIES: frozenset[str] = frozenset(
    {
        "ac_source",      # 可编程交流电源
        "eload",          # 直流电子负载
        "dmm",            # 数字多用表
        "scope",          # 示波器
        "power_meter",    # 功率计
        "dc_source",      # 可编程直流电源
        "safety_tester",  # 安规测试仪 (hipot/IR/GB)
    }
)

#: id 格式约束: 小写 + hyphen + 数字 (与 yaml 顶部 docstring 一致)
_ID_RE = re.compile(r"^[a-z][a-z0-9-]+$")


# ── 错误 ────────────────────────────────────────────────────────────────


class InstrumentCatalogError(ValueError):
    """catalog yaml 解析失败时使用 (与 ``InstrumentCatalogError`` 区别)"""


# ── 数据类 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Instrument:
    """一条测试设备本体记录。

    Attributes:
        id: 唯一 id (lowercase hyphen-separated, 例 ``chroma-61605``)
        vendor: 厂商名
        model: 型号
        category: 七大类之一
        key_specs: 关键规格 (类别不同字段集不同)
        protocols: 支持接口列表 (例 [SCPI, USB, GPIB])
        terminals: 物理接口类型 (例 [4-wire_kelvin, banana])
        compatible_methods: 适配的产测方法 id (反向索引, 留空也允许)
        data_source: 数据源标签 (vendor_official_site / vendor_official_pdf / training_knowledge)
        data_provenance_note: 当 data_source=training_knowledge 时填真实依据
        references: URLs (manual verify 提示, sandbox 无 live verify)
    """

    id: str
    vendor: str
    model: str
    category: str
    key_specs: Mapping[str, Any]
    protocols: tuple[str, ...] = ()
    terminals: tuple[str, ...] = ()
    compatible_methods: tuple[str, ...] = ()
    data_source: str = ""
    references: tuple[str, ...] = ()
    data_provenance_note: str = ""

    def is_training_knowledge(self) -> bool:
        return self.data_source == "training_knowledge"


# ── 路径解析 / loader ──────────────────────────────────────────────────


def resolve_instrument_catalog_path(path: Path | str | None = None) -> Path:
    """显式参数 > 环境变量 > 仓库默认路径。"""
    if path is not None:
        return Path(path)
    override = os.environ.get(INSTRUMENT_CATALOG_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return DEFAULT_INSTRUMENT_CATALOG_PATH


def _require_str(d: Mapping[str, Any], key: str, *, where: str) -> str:
    v = d.get(key)
    if isinstance(v, (str, int, float)):
        s = str(v).strip()
        if s:
            return s
    raise InstrumentCatalogError(f"{where} 的 {key!r} 缺失/非字符串/为空")


def _parse_one(raw: Mapping[str, Any], *, index: int) -> Instrument:
    """把一条 yaml dict 解析为 :class:`Instrument`, 含基础 fail-fast 校验。"""
    prefix = f"instrument_catalog[{index}]"
    iid = _require_str(raw, "id", where=prefix)
    if not _ID_RE.match(iid):
        raise InstrumentCatalogError(
            f"{prefix} 的 id 格式不合规 (lowercase, hyphen only): {iid!r}"
        )
    vendor = _require_str(raw, "vendor", where=prefix)
    model = _require_str(raw, "model", where=prefix)
    category = _require_str(raw, "category", where=prefix)
    if category not in INSTRUMENT_CATEGORIES:
        raise InstrumentCatalogError(
            f"{prefix}.category={category!r} 不在闭集 "
            f"(allowed: {sorted(INSTRUMENT_CATEGORIES)})"
        )
    key_specs_raw = raw.get("key_specs", {})
    if not isinstance(key_specs_raw, Mapping) or not key_specs_raw:
        raise InstrumentCatalogError(f"{prefix}.key_specs 必须是非空 dict")
    protocols = tuple(str(p) for p in raw.get("protocols", []) if p)
    terminals = tuple(str(t) for t in raw.get("terminals", []) if t)
    compatible = tuple(str(m) for m in raw.get("compatible_methods", []) if m)
    references = tuple(str(u) for u in raw.get("references", []) if u)
    return Instrument(
        id=iid,
        vendor=vendor,
        model=model,
        category=category,
        key_specs=dict(key_specs_raw),
        protocols=protocols,
        terminals=terminals,
        compatible_methods=compatible,
        data_source=str(raw.get("data_source", "")),
        references=references,
        data_provenance_note=str(raw.get("data_provenance_note", "")),
    )


def load_instrument_catalog(
    path: Path | str | None = None,
) -> dict[str, Instrument]:
    """加载 catalog yaml → {id: Instrument}。

    缺文件或解析失败: 返回 ``{}`` + ``logger.warning`` (降级可查, 不阻断 CI)。
    """
    logger = logging.getLogger(__name__)
    resolved = resolve_instrument_catalog_path(path)
    if not resolved.exists():
        logger.warning(
            "instrument_catalog 源文件不存在: %s (返回空 catalog, e2e 仿真退化为硬编码)",
            resolved,
        )
        return {}
    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.warning("instrument_catalog 解析失败: %s: %s", resolved, exc)
        return {}
    raw_list = data.get("instruments") if isinstance(data, Mapping) else None
    if not isinstance(raw_list, list):
        # Fallback: 顶层就是 list
        raw_list = data if isinstance(data, list) else []
    out: dict[str, Instrument] = {}
    for idx, raw in enumerate(raw_list):
        if not isinstance(raw, Mapping):
            logger.warning("跳过 index=%d 非 dict 条目", idx)
            continue
        try:
            inst = _parse_one(raw, index=idx)
        except InstrumentCatalogError as exc:
            logger.warning("跳过非法条目: %s", exc)
            continue
        if inst.id in out:
            logger.warning("id 重复, 跳过: %s", inst.id)
            continue
        out[inst.id] = inst
    if not out:
        logger.warning("instrument_catalog 已加载但 0 条, 留痕待补")
    return out


# ── 解析入口 ──────────────────────────────────────────────────────────


def find_instrument_by_id(
    instrument_id: str,
    catalog: Mapping[str, Instrument] | None = None,
) -> Instrument | None:
    """按 id 查; 未找到返回 ``None`` (不抛)。"""
    if catalog is None:
        catalog = load_instrument_catalog()
    return catalog.get(instrument_id)


def find_instruments_by_method(
    method_id: str,
    catalog: Mapping[str, Instrument] | None = None,
) -> tuple[Instrument, ...]:
    """按 M-* 方法 id 反查所有适配设备 (compatible_methods 字段索引)。"""
    if catalog is None:
        catalog = load_instrument_catalog()
    return tuple(
        sorted(
            (inst for inst in catalog.values() if method_id in inst.compatible_methods),
            key=lambda i: i.id,
        )
    )


def find_by_category(
    category: str,
    catalog: Mapping[str, Instrument] | None = None,
) -> tuple[Instrument, ...]:
    """按 category 查; category 不在闭集 → 返回空 tuple + logger.warning。"""
    if catalog is None:
        catalog = load_instrument_catalog()
    if category not in INSTRUMENT_CATEGORIES:
        logging.getLogger(__name__).warning(
            "find_by_category 收到未注册 category=%r (allowed: %s)",
            category, sorted(INSTRUMENT_CATEGORIES),
        )
        return ()
    return tuple(
        sorted(
            (inst for inst in catalog.values() if inst.category == category),
            key=lambda i: i.id,
        )
    )


def compatible_methods_for(
    instrument_id: str,
    catalog: Mapping[str, Instrument] | None = None,
) -> tuple[str, ...]:
    """给定设备 id, 返回所有 M-* 适配方法 (顺序与 yaml 一致)。"""
    if catalog is None:
        catalog = load_instrument_catalog()
    inst = catalog.get(instrument_id)
    return inst.compatible_methods if inst else ()


def cross_validate_with_methods(
    catalog: Mapping[str, Instrument] | None = None,
    methods_yaml_path: Path | str | None = None,
) -> dict[str, list[str]]:
    """双向校验: 给定 power_test_methods.yaml, 找出 methods.yaml 中存在的 M-* 但
    catalog 中**没有任何**仪器适配它的列表。

    Returns:
        ``{method_id: [被缺漏的仪器描述]}``; 空 dict 表示全覆盖。
        不阻断, 只作 WARN 报告 (e2e 仿真可继续, 但 catalog 未覆盖的方法是欠知识)。

    Note:
        双向性: 该函数**不**校验 catalog.compatible_methods 引用的 M-* 是否在
        methods.yaml 中定义 (该方向校验由 :func:`validate_test_methods` 处理)。
    """
    if catalog is None:
        catalog = load_instrument_catalog()
    methods = {m.method_id for m in load_test_methods(methods_yaml_path)}
    catalog_covered: set[str] = set()
    for inst in catalog.values():
        for mid in inst.compatible_methods:
            if mid in methods:
                catalog_covered.add(mid)
    gap: dict[str, list[str]] = {}
    for mid in methods - catalog_covered:
        gap.setdefault(mid, ["无 catalog 仪器显式适配; 仅文字描述"])
    return gap
