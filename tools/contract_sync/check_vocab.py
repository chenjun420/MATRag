"""跨仓词表契约同步门 —— MATRag 侧 (Phase A.1)。

本工具在 MATRag CI 中运行, 读取生产侧与消费侧的数据源, 做子集/双向差集校验,
输出人类可读的差异报告, 并以退出码驱动 CI 门禁:

数据源
------
1. ``src/matrag/extract/kinds.py:KIND_UNITS`` —— 生产侧冻结的 25 个
   ``(kind, side)`` 量纲约定 (本进程内 import, 不触网);
2. ``data/domain/source/power_test_methods.yaml`` —— 生产侧测试方法目录;
3. ``$ATE_STUDIO_ROOT/config/test_method_bindings.yaml`` —— 消费侧
   ``(kind, side) → 仪器动作`` 绑定表 (含 action / method_ref / unit);
4. ``$ATE_STUDIO_ROOT/src/ate_cloud/services/aterag_script_emit.py:DRIVER_TABLE``
   —— 消费侧 ``action → (driver, method, nature)`` 驱动派发表 (AST 静态解析,
   不 import 消费侧包, 避免触发其重型初始化)。

校验
----
- (#1, 硬门禁) ``KIND_UNITS`` 与绑定表的 ``(kind, side)`` 集合双向差集 (两方向都要为空);
- (#2, 软警告) ``power_test_methods.yaml`` 的 ``method_id`` (MATRag 内部方法库,
  ``M-*`` 命名空间) 与绑定表 ``method_ref`` (外部标准 id, 如 ``iec62368_*``) 的
  **命名空间重叠报告** —— 二者**设计上就是两个不相交的命名空间** (内部方法目录 vs
  外部标准溯源), 故仅报告差异, **不计入退出码**;
- (#3, 硬门禁) 绑定表的 ``action`` ⊆ ``DRIVER_TABLE`` (``DRIVER_TABLE`` 中未被引用的
  action 仅作信息提示, 不阻断);
- (#4, 信息) ``method_ref`` 值是否符合外部标准命名约定 (启发式正则)。

严重级别
--------
每条校验带一个严重级别: ``FAIL`` (硬门禁, 漂移即退出码 1) / ``WARN`` (软警告) /
``INFO`` (纯信息)。报告分别标注三者; **只有 ``FAIL`` 级别失败才阻断 CI**。

为什么必须对齐
--------------
漏一个 kind / action, 对应那类测试会**静默缺失**: 计划照常生成、报告照样变绿,
流出的却是不合格品。因此 #1/#3 为硬门禁, fail-fast —— 任一数据源缺失或解析失败
即明确报错 (退出码 2), 绝不静默降级。方法命名空间差异 (#2) 属预期的设计差异,
降级为 WARN, 不再阻断 CI。

退出码
------
``0`` = 硬门禁全部通过 (仅有 WARN/INFO); ``1`` = 硬门禁检出漂移 (kind 或 action);
``2`` = 环境/数据源错误 (路径不存在、解析失败、结构非法等)。
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "ATE_STUDIO_ROOT_DEFAULT",
    "ATE_STUDIO_ROOT_ENV",
    "BINDINGS_RELPATH",
    "DRIVER_TABLE_RELPATH",
    "EXIT_CONFIG",
    "EXIT_DRIFT",
    "EXIT_OK",
    "EXTERNAL_REF_RE",
    "METHODS_RELPATH",
    "SEVERITY_FAIL",
    "SEVERITY_INFO",
    "SEVERITY_WARN",
    "Binding",
    "CheckResult",
    "ContractSyncError",
    "Inputs",
    "build_inputs",
    "force_utf8_stdio",
    "load_bindings",
    "load_driver_table",
    "load_kind_units",
    "load_method_ids",
    "main",
    "render_report",
    "resolve_ate_root",
    "resolve_matrag_root",
    "run_checks",
]

#: 退出码语义。
EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_CONFIG = 2

#: 校验严重级别: ``FAIL``=硬门禁 (阻断 CI) / ``WARN``=软警告 / ``INFO``=纯信息。
SEVERITY_FAIL = "FAIL"
SEVERITY_WARN = "WARN"
SEVERITY_INFO = "INFO"

#: 外部标准 id 命名约定 (启发式): 小写字母前缀 + 数字标准号 + 下划线分隔,
#: 如 ``iec62368_protection`` / ``en300132_ripple`` / ``iec61000_4_11_surge``。
EXTERNAL_REF_RE = re.compile(r"^[a-z]+\d+_*[a-z0-9_]+$")

#: ATEStudio 仓库根目录的环境变量名与默认值 (相对 MATRag 根目录的兄弟目录)。
ATE_STUDIO_ROOT_ENV = "ATE_STUDIO_ROOT"
ATE_STUDIO_ROOT_DEFAULT = "../ATEStudio"

#: 各数据源相对其仓库根目录的路径。
METHODS_RELPATH = Path("data") / "domain" / "source" / "power_test_methods.yaml"
BINDINGS_RELPATH = Path("config") / "test_method_bindings.yaml"
DRIVER_TABLE_RELPATH = Path("src") / "ate_cloud" / "services" / "aterag_script_emit.py"

#: 报告里每个差集列表最多逐条列出的条目数 (超出仅报计数)。
_MAX_LISTED = 40


class ContractSyncError(RuntimeError):
    """数据源缺失、解析失败或结构非法 —— fail-fast, 绝不静默降级。"""


@dataclass(frozen=True, slots=True)
class Binding:
    """绑定表 ``test_method_bindings.yaml`` 中的一条记录。"""

    kind: str
    side: str
    action: str
    method_ref: str
    unit: str


@dataclass(frozen=True, slots=True)
class CheckResult:
    """一条校验的结果。

    ``severity`` = 本项校验的严重级别 (``FAIL`` / ``WARN`` / ``INFO``);
    只有 ``FAIL`` 级别且 ``ok=False`` 才计入退出码 1。

    ``missing`` = 本应有而对方缺失的条目; ``extra`` = 对方有而多余/本侧没有的
    条目 (双向差集时双侧均为失败条件, 单向子集时仅 ``missing`` 为失败条件)。
    ``missing_label`` / ``extra_label`` 用于按校验语义自定义两列表的标题;
    ``notes`` 为随报告附注的说明行。
    """

    name: str
    ok: bool
    detail: str
    severity: str = SEVERITY_FAIL
    missing: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    missing_label: str = "缺失"
    extra_label: str = "多余/未引用"
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Inputs:
    """四个数据源的解析结果 (供 :func:`run_checks` 消费)。"""

    matrag_root: Path
    ate_root: Path
    kind_units: Mapping[tuple[str, str], str]
    bindings: tuple[Binding, ...] = ()
    method_ids: frozenset[str] = field(default_factory=frozenset)
    driver_actions: frozenset[str] = field(default_factory=frozenset)


# ── 路径解析 ────────────────────────────────────────────────────────────────


def resolve_matrag_root() -> Path:
    """由脚本位置推断 MATRag 仓库根目录 (``tools/contract_sync/x.py`` → 上溯两级)。"""
    return Path(__file__).resolve().parents[2]


def resolve_ate_root(matrag_root: Path | None = None, override: str | None = None) -> Path:
    """解析 ATEStudio 仓库根目录。

    优先级: 显式 ``override`` > 环境变量 ``ATE_STUDIO_ROOT`` > 默认 ``../ATEStudio``。
    相对路径一律相对 ``matrag_root`` (缺省时由脚本位置推断)。
    """
    raw = override or os.environ.get(ATE_STUDIO_ROOT_ENV) or ATE_STUDIO_ROOT_DEFAULT
    path = Path(raw).expanduser()
    if not path.is_absolute():
        base = matrag_root if matrag_root is not None else resolve_matrag_root()
        path = (base / path).resolve()
    return path


def _require_file(path: Path, *, what: str) -> Path:
    """文件不存在即 fail-fast, 并给出环境变量提示。"""
    if not path.is_file():
        raise ContractSyncError(
            f"{what} 不存在: {path}\n"
            f"  提示: 用环境变量 {ATE_STUDIO_ROOT_ENV} 指定 ATEStudio 仓库根目录 "
            f"(默认 {ATE_STUDIO_ROOT_DEFAULT})。"
        )
    return path


def _require_dir(path: Path, *, what: str) -> Path:
    """目录不存在即 fail-fast。"""
    if not path.is_dir():
        raise ContractSyncError(
            f"{what} 不存在: {path}\n"
            f"  提示: 用环境变量 {ATE_STUDIO_ROOT_ENV} 指定 ATEStudio 仓库根目录 "
            f"(默认 {ATE_STUDIO_ROOT_DEFAULT})。"
        )
    return path


def _read_text(path: Path, *, what: str) -> str:
    """读取 UTF-8 文本, 失败即 fail-fast 并保留原始错误。"""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ContractSyncError(f"{what} 非 UTF-8 文本: {path}: {exc}") from exc
    except OSError as exc:
        raise ContractSyncError(f"{what} 读取失败: {path}: {exc}") from exc


def _load_yaml(path: Path, *, what: str) -> Any:
    """读取并解析 YAML, 失败即 fail-fast。"""
    text = _read_text(path, what=what)
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ContractSyncError(f"{what} 解析失败 (YAML): {path}: {exc}") from exc


# ── 数据源读取器 ────────────────────────────────────────────────────────────


def load_kind_units() -> dict[tuple[str, str], str]:
    """读取生产侧 ``KIND_UNITS`` (本进程内 import; 可被测试 monkeypatch)。"""
    try:
        from matrag.extract.kinds import KIND_UNITS
    except Exception as exc:  # noqa: BLE001 —— 导入失败原因原样上抛
        raise ContractSyncError(f"无法读取 matrag.extract.kinds.KIND_UNITS: {exc}") from exc
    return dict(KIND_UNITS)


def load_bindings(path: Path) -> list[Binding]:
    """解析绑定表 ``test_method_bindings.yaml``。"""
    doc = _load_yaml(path, what="绑定表 test_method_bindings.yaml")
    if not isinstance(doc, Mapping) or "bindings" not in doc:
        raise ContractSyncError(f"绑定表结构非法 (缺少 bindings 列表): {path}")
    raw = doc["bindings"]
    if not isinstance(raw, list):
        raise ContractSyncError(f"绑定表 bindings 必须是列表: {path}")
    out: list[Binding] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ContractSyncError(f"绑定表第 {index} 条不是映射: {path}")
        for required in ("kind", "side"):
            if not item.get(required):
                raise ContractSyncError(f"绑定表第 {index} 条缺少非空字段 {required!r}: {path}")
        out.append(
            Binding(
                kind=str(item["kind"]).strip(),
                side=str(item["side"]).strip(),
                action=str(item.get("action") or "").strip(),
                method_ref=str(item.get("method_ref") or "").strip(),
                unit=str(item.get("unit") or "").strip(),
            )
        )
    return out


def load_method_ids(path: Path) -> set[str]:
    """解析方法目录 ``power_test_methods.yaml`` 的 ``method_id`` 集合。"""
    doc = _load_yaml(path, what="方法目录 power_test_methods.yaml")
    if not isinstance(doc, list):
        raise ContractSyncError(f"方法目录必须是条目列表: {path}")
    ids: set[str] = set()
    for index, item in enumerate(doc):
        if not isinstance(item, Mapping) or not item.get("method_id"):
            raise ContractSyncError(f"方法目录第 {index} 条缺少 method_id: {path}")
        ids.add(str(item["method_id"]).strip())
    return ids


def _find_driver_table_node(tree: ast.AST) -> ast.expr | None:
    """在 AST 中定位 ``DRIVER_TABLE`` 的赋值右值。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "DRIVER_TABLE":
                    return node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "DRIVER_TABLE":
                return node.value
    return None


def load_driver_table(path: Path) -> set[str]:
    """静态解析消费侧 ``DRIVER_TABLE`` 的 action 键集合 (AST, 不 import)。"""
    source = _read_text(path, what="DRIVER_TABLE 源文件 aterag_script_emit.py")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise ContractSyncError(f"DRIVER_TABLE 源文件语法错误: {path}: {exc}") from exc
    node = _find_driver_table_node(tree)
    if node is None:
        raise ContractSyncError(f"未在 {path} 中找到 DRIVER_TABLE 赋值")
    if not isinstance(node, ast.Dict):
        raise ContractSyncError(f"DRIVER_TABLE 不是字典字面量 (无法静态解析): {path}")
    actions: set[str] = set()
    for key in node.keys:
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            raise ContractSyncError(f"DRIVER_TABLE 含非字符串键 (无法静态解析): {path}")
        actions.add(key.value)
    return actions


def build_inputs(matrag_root: Path, ate_root: Path) -> Inputs:
    """按解析出的两个仓库根目录读取全部数据源 (任一失败即 fail-fast)。"""
    kind_units = load_kind_units()

    methods_path = _require_file(matrag_root / METHODS_RELPATH, what="方法目录 power_test_methods.yaml")
    method_ids = load_method_ids(methods_path)

    _require_dir(ate_root, what="ATEStudio 仓库根目录")
    bindings_path = _require_file(ate_root / BINDINGS_RELPATH, what="绑定表 test_method_bindings.yaml (ATEStudio)")
    bindings = load_bindings(bindings_path)

    driver_path = _require_file(ate_root / DRIVER_TABLE_RELPATH, what="DRIVER_TABLE 源文件 (ATEStudio)")
    driver_actions = load_driver_table(driver_path)

    return Inputs(
        matrag_root=matrag_root,
        ate_root=ate_root,
        kind_units=kind_units,
        bindings=tuple(bindings),
        method_ids=frozenset(method_ids),
        driver_actions=frozenset(driver_actions),
    )


# ── 校验 (纯函数, 便于离线测试) ─────────────────────────────────────────────


def _fmt_pair(pair: tuple[str, str]) -> str:
    return f"{pair[0]}:{pair[1]}"


def run_checks(
    *,
    kind_units: Mapping[tuple[str, str], str],
    bindings: Sequence[Binding],
    method_ids: set[str] | frozenset[str],
    driver_actions: set[str] | frozenset[str],
) -> list[CheckResult]:
    """执行四项校验, 返回结果列表 (纯函数, 不触碰文件系统)。

    返回顺序固定为 #1 (硬门禁) / #2 (软警告) / #3 (硬门禁) / #4 (信息),
    便于调用方与测试按索引稳定取值。
    """
    # ── #1 (硬门禁): KIND_UNITS ↔ 绑定表 (kind, side) 双向差集 ──
    kind_pairs = set(kind_units)
    binding_pairs = {(b.kind, b.side) for b in bindings}
    ku_missing = kind_pairs - binding_pairs
    ku_extra = binding_pairs - kind_pairs
    check1 = CheckResult(
        name="KIND_UNITS ↔ test_method_bindings.yaml (kind,side) 双向一致",
        ok=not ku_missing and not ku_extra,
        detail=(
            f"KIND_UNITS={len(kind_pairs)} == 绑定表={len(binding_pairs)} "
            f"(缺失={len(ku_missing)} 多余={len(ku_extra)})"
        ),
        missing=tuple(sorted(_fmt_pair(p) for p in ku_missing)),
        extra=tuple(sorted(_fmt_pair(p) for p in ku_extra)),
    )

    # ── #2 (软警告): 内部方法命名空间 M-* vs 外部标准命名空间 ──
    # 二者设计上就是两个不相交的命名空间 (内部测试方法库 vs 外部标准溯源),
    # 故仅出差异报告, 不计入退出码。
    refs = {b.method_ref for b in bindings if b.method_ref}
    method_ids_set = set(method_ids)
    mr_missing = method_ids_set - refs
    mr_extra = refs - method_ids_set
    check2 = CheckResult(
        name="namespace overlap (MATRag M-* method_id vs bindings.method_ref 外部标准)",
        ok=not mr_missing and not mr_extra,
        severity=SEVERITY_WARN,
        detail=(
            f"MATRag method_id={len(method_ids_set)} bindings.method_ref={len(refs)} "
            f"交集={len(method_ids_set & refs)}"
        ),
        missing=tuple(sorted(mr_missing)),
        extra=tuple(sorted(mr_extra)),
        missing_label="MATRag method_id 不在 bindings.method_ref",
        extra_label="bindings.method_ref 不在 MATRag method_id",
        notes=(
            "M-* 是 MATRag 内部测试方法库; method_ref 是外部标准溯源 (哪条 IEC/EN 标准要求该项测试)。",
            "二者是两个设计上不相交的命名空间, 本项仅报告差异, 不阻断 CI。",
        ),
    )

    # ── #3a (硬门禁): 绑定表 action ⊆ DRIVER_TABLE（缺则阻断 CI）──
    binding_actions = {b.action for b in bindings if b.action}
    driver_set = set(driver_actions)
    da_missing = binding_actions - driver_set
    da_extra = driver_set - binding_actions
    check3a = CheckResult(
        name="test_method_bindings.yaml.action ⊆ DRIVER_TABLE",
        severity=SEVERITY_FAIL,
        ok=not da_missing,
        detail=(
            f"绑定表 action={len(binding_actions)} ⊆ DRIVER_TABLE={len(driver_set)} "
            f"缺失={len(da_missing)}"
        ),
        missing=tuple(sorted(da_missing)),
        notes=(
            "硬门禁: 绑定表引用了 DRIVER_TABLE 中不存在的 action，"
            "会导致 ATEStudio 派发阶段 KeyError。",
        ),
    )
    # ── #3b (软警告): DRIVER_TABLE 中未被引用的 action ──
    # DRIVER_TABLE 加了新 action 但绑定表未跟进 → 不会阻断 CI, 但积累死代码。
    check3b = CheckResult(
        name="DRIVER_TABLE 未被绑定表引用的 action",
        severity=SEVERITY_WARN,
        ok=not da_extra,
        detail=f"未被引用={len(da_extra)}（不阻断, 但会堆积死 action）",
        extra=tuple(sorted(da_extra)),
        notes=(
            "建议: 周期扫描 N 周未用 action, 加入 data/reports/action_lifecycle.md 待办。",
        ),
    )

    # ── #4 (信息): method_ref 外部标准命名约定 ──
    suspicious = tuple(sorted(r for r in refs if not EXTERNAL_REF_RE.match(r)))
    check4 = CheckResult(
        name="method_ref naming convention (外部标准 id 命名约定)",
        ok=not suspicious,
        severity=SEVERITY_INFO,
        detail=f"{len(refs)} 个 method_ref, {len(suspicious)} 个可疑",
        missing=suspicious,
        missing_label="不符合命名约定 (可能是 typo 或自定义命名)",
    )

    return [check1, check2, check3a, check3b, check4]


# ── 报告渲染 ────────────────────────────────────────────────────────────────


def _format_list(items: Sequence[str]) -> list[str]:
    """把差集列表渲染为缩进行 (超出上限仅列计数)。"""
    lines: list[str] = []
    shown = items[:_MAX_LISTED]
    for chunk_start in range(0, len(shown), 6):
        lines.append("          " + ", ".join(shown[chunk_start : chunk_start + 6]))
    if len(items) > _MAX_LISTED:
        lines.append(f"          ... 其余 {len(items) - _MAX_LISTED} 条省略")
    return lines


def _status_marker(result: CheckResult) -> str:
    """按严重级别给出结果标记 (硬门禁 ✓/✗; 软警告 ✓/⚠; 信息 ℹ)。"""
    if result.severity == SEVERITY_FAIL:
        return "✓" if result.ok else "✗"
    if result.severity == SEVERITY_WARN:
        return "✓" if result.ok else "⚠"
    return "ℹ"


def render_report(inputs: Inputs, results: Sequence[CheckResult]) -> str:
    """把数据源概况与校验结果渲染为人类可读报告 (区分 FAIL/WARN/INFO)。"""
    lines: list[str] = []
    lines.append("=== MATRag 跨仓契约同步门 (Phase A.1) ===")
    lines.append(f"MATRag   根目录: {inputs.matrag_root}")
    lines.append(f"ATEStudio 根目录: {inputs.ate_root}")
    lines.append("")
    lines.append("数据源:")
    lines.append(f"  KIND_UNITS (MATRag)          : {len(inputs.kind_units)} 个 (kind, side)")
    lines.append(f"  test_method_bindings (ATE)   : {len(inputs.bindings)} 条绑定")
    lines.append(f"  power_test_methods (MATRag)  : {len(inputs.method_ids)} 个方法")
    lines.append(f"  DRIVER_TABLE (ATE)           : {len(inputs.driver_actions)} 个 action")
    lines.append("")
    lines.append("校验:")
    for index, result in enumerate(results, start=1):
        lines.append(f"[{result.severity}] check #{index} {result.name}")
        lines.append(f"          {_status_marker(result)} {result.detail}")
        if result.missing:
            lines.append(f"          {result.missing_label} ({len(result.missing)}):")
            lines.extend(_format_list(result.missing))
        if result.extra:
            lines.append(f"          {result.extra_label} ({len(result.extra)}):")
            lines.extend(_format_list(result.extra))
        for note in result.notes:
            lines.append(f"          Note: {note}")
    lines.append("")

    n_fail = sum(1 for r in results if r.severity == SEVERITY_FAIL and not r.ok)
    n_pass = sum(1 for r in results if r.severity == SEVERITY_FAIL and r.ok)
    n_warn = sum(1 for r in results if r.severity == SEVERITY_WARN and not r.ok)
    n_info = sum(1 for r in results if r.severity == SEVERITY_INFO and not r.ok)
    exit_code = EXIT_DRIFT if n_fail else EXIT_OK
    lines.append("=== Summary ===")
    lines.append(f"PASS={n_pass}  WARN={n_warn}  FAIL={n_fail}  (INFO={n_info})  → exit {exit_code}")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────────


def force_utf8_stdio() -> None:
    """尽力把标准输出/错误切到 UTF-8, 避免 Windows GBK 控制台输出中文/符号时崩溃。

    对无 ``reconfigure`` 的对象 (如 pytest 捕获流) 静默跳过 —— 这是显示层适配,
    不是业务降级。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):  # pragma: no cover —— 个别流不支持
            continue


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口: 返回退出码 (0 一致 / 1 漂移 / 2 环境错误)。"""
    force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="contract_sync.check_vocab",
        description="跨仓词表契约同步门 (MATRag 侧): 校验 KIND_UNITS / 绑定表 / 方法目录 / DRIVER_TABLE。",
    )
    parser.add_argument("--matrag-root", default=None, help="MATRag 仓库根目录 (默认由脚本位置推断)")
    parser.add_argument(
        "--ate-root",
        default=None,
        help=f"ATEStudio 仓库根目录 (默认 ${ATE_STUDIO_ROOT_ENV} 或 {ATE_STUDIO_ROOT_DEFAULT})",
    )
    args = parser.parse_args(argv)

    matrag_root = Path(args.matrag_root).resolve() if args.matrag_root else resolve_matrag_root()
    try:
        ate_root = Path(args.ate_root).resolve() if args.ate_root else resolve_ate_root(matrag_root)
        inputs = build_inputs(matrag_root, ate_root)
    except ContractSyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    results = run_checks(
        kind_units=inputs.kind_units,
        bindings=inputs.bindings,
        method_ids=inputs.method_ids,
        driver_actions=inputs.driver_actions,
    )
    print(render_report(inputs, results))
    blocked = any(r.severity == SEVERITY_FAIL and not r.ok for r in results)
    return EXIT_DRIFT if blocked else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
