"""跨仓契约指纹同步门 —— MATRag 侧 (Phase A.1)。

生产侧 (MATRag) 与消费侧 (ATEStudio) 各自从自己的 Pydantic schema 独立推导
``contract_hash()``; 两侧相等当且仅当 schema 声明逐字一致。本门禁校验两件事:

1. 两侧**实际计算**的 ``contract_hash()`` 是否一致;
2. 两侧**冻结基线** ``KNOWN_CONTRACT_HASH`` 是否与各自实际值一致 (基线未过期)。

为什么用 subprocess 算消费侧指纹
--------------------------------
``ate_cloud`` 包 import 会触发应用侧初始化 (挂载 SPA 等); 在本进程内 import 有
把 MATRag CI 与消费侧运行时耦合的风险。故本工具用 ATEStudio 自己的 venv
解释器在**子进程**里计算 ``contract_hash()``, 只取回一行带标记的指纹字符串,
本进程绝不 import ``ate_cloud``。

退出码
------
``0`` = 全部一致; ``1`` = 检出漂移; ``2`` = 环境/数据源错误 (路径不存在、
子进程失败、常量缺失等)。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tools.contract_sync.check_vocab import (
    ATE_STUDIO_ROOT_ENV,
    EXIT_CONFIG,
    EXIT_DRIFT,
    EXIT_OK,
    CheckResult,
    ContractSyncError,
    force_utf8_stdio,
    resolve_ate_root,
    resolve_matrag_root,
)

__all__ = [
    "ATE_PYTHON_ENV",
    "HashSnapshot",
    "build_snapshot",
    "compute_ate_contract_hash",
    "compute_matrag_contract_hash",
    "find_ate_python",
    "load_ate_known_hash",
    "load_matrag_known_hash",
    "main",
    "render_report",
    "run_hash_checks",
]

#: ATEStudio venv 解释器的环境变量名 (缺省自动探测 ``<ate_root>/.venv``)。
ATE_PYTHON_ENV = "ATE_STUDIO_PYTHON"

#: 消费侧 bundle 契约文件 (相对 ATEStudio 根目录)。
ATE_BUNDLE_RELPATH = Path("src") / "ate_cloud" / "schemas" / "aterag_bundle.py"

#: 子进程探测脚本: 只回显一行带标记的指纹, 便于在混杂输出中稳定提取。
_ATE_HASH_PROBE = (
    "from ate_cloud.schemas.aterag_bundle import contract_hash;"
    "print('CONTRACT_HASH=' + contract_hash())"
)

#: 冻结基线常量 (文本解析, 不 import 消费侧包)。
_KNOWN_HASH_RE = re.compile(r"""^\s*KNOWN_CONTRACT_HASH\s*=\s*["']([0-9a-fA-F]+)["']""", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class HashSnapshot:
    """两侧契约指纹与冻结基线的快照。"""

    matrag_known: str
    matrag_computed: str
    ate_known: str
    ate_computed: str


# ── 生产侧 (MATRag): 本进程内 import ────────────────────────────────────────


def load_matrag_known_hash() -> str:
    """读取生产侧冻结基线 ``KNOWN_CONTRACT_HASH``。"""
    try:
        from matrag.extract.bundle import KNOWN_CONTRACT_HASH
    except Exception as exc:  # noqa: BLE001 —— 导入失败原因原样上抛
        raise ContractSyncError(f"无法读取 matrag.extract.bundle.KNOWN_CONTRACT_HASH: {exc}") from exc
    return KNOWN_CONTRACT_HASH


def compute_matrag_contract_hash() -> str:
    """在本进程内计算生产侧 ``contract_hash()`` (仅依赖 pydantic, 不触网)。"""
    try:
        from matrag.extract.bundle import contract_hash
    except Exception as exc:  # noqa: BLE001
        raise ContractSyncError(f"无法导入 matrag.extract.bundle.contract_hash: {exc}") from exc
    return contract_hash()


# ── 消费侧 (ATEStudio): 文本解析 + 子进程 ───────────────────────────────────


def load_ate_known_hash(ate_root: Path) -> str:
    """文本解析消费侧冻结基线 ``KNOWN_CONTRACT_HASH`` (不 import 消费侧包)。"""
    path = ate_root / ATE_BUNDLE_RELPATH
    if not path.is_file():
        raise ContractSyncError(
            f"ATEStudio bundle 契约文件不存在: {path}\n"
            f"  提示: 用环境变量 {ATE_STUDIO_ROOT_ENV} 指定 ATEStudio 仓库根目录。"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractSyncError(f"ATEStudio bundle 契约文件读取失败: {path}: {exc}") from exc
    match = _KNOWN_HASH_RE.search(text)
    if match is None:
        raise ContractSyncError(f"未在 {path} 中找到 KNOWN_CONTRACT_HASH 常量")
    return match.group(1)


def find_ate_python(ate_root: Path, override: str | None = None) -> Path:
    """定位消费侧解释器。

    优先级: 显式 ``override`` > 环境变量 ``ATE_STUDIO_PYTHON`` >
    ``<ate_root>/.venv`` (Windows ``Scripts/python.exe`` / POSIX ``bin/python``)。
    """
    raw = override or os.environ.get(ATE_PYTHON_ENV)
    if raw:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise ContractSyncError(f"{ATE_PYTHON_ENV} 指向的解释器不存在: {path}")
        return path
    candidates = (
        ate_root / ".venv" / "Scripts" / "python.exe",  # Windows
        ate_root / ".venv" / "bin" / "python",  # POSIX
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    attempted = ", ".join(str(c) for c in candidates)
    raise ContractSyncError(
        f"找不到 ATEStudio venv 解释器 (尝试: {attempted});\n"
        f"  提示: 用环境变量 {ATE_PYTHON_ENV} 指定一个可 import ate_cloud 的解释器。"
    )


def compute_ate_contract_hash(
    ate_root: Path,
    python_exe: Path | None = None,
    *,
    timeout: float = 120.0,
) -> str:
    """在子进程中用消费侧解释器计算 ``contract_hash()`` 并取回。"""
    exe = python_exe or find_ate_python(ate_root)
    try:
        proc = subprocess.run(
            [str(exe), "-c", _ATE_HASH_PROBE],
            cwd=str(ate_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ContractSyncError(f"无法执行消费侧解释器 {exe}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ContractSyncError(f"消费侧子进程超时 ({timeout}s): {exe}") from exc
    if proc.returncode != 0:
        raise ContractSyncError(
            f"消费侧子进程计算 contract_hash() 失败 (退出码 {proc.returncode}): {exe}\n"
            f"  stderr: {proc.stderr.strip()[:2000]}"
        )
    for line in proc.stdout.splitlines():
        if line.startswith("CONTRACT_HASH="):
            return line.split("=", 1)[1].strip()
    raise ContractSyncError(
        f"消费侧子进程未输出 CONTRACT_HASH 标记: {exe}\n  stdout: {proc.stdout.strip()[:2000]}"
    )


def build_snapshot(matrag_root: Path, ate_root: Path, *, python_exe: Path | None = None) -> HashSnapshot:
    """采集两侧契约指纹与冻结基线 (任一失败即 fail-fast)。"""
    if not ate_root.is_dir():
        raise ContractSyncError(
            f"ATEStudio 仓库根目录不存在: {ate_root}\n"
            f"  提示: 用环境变量 {ATE_STUDIO_ROOT_ENV} 指定 ATEStudio 仓库根目录。"
        )
    return HashSnapshot(
        matrag_known=load_matrag_known_hash(),
        matrag_computed=compute_matrag_contract_hash(),
        ate_known=load_ate_known_hash(ate_root),
        ate_computed=compute_ate_contract_hash(ate_root, python_exe),
    )


# ── 校验 (纯函数, 便于离线测试) ─────────────────────────────────────────────


def run_hash_checks(snapshot: HashSnapshot) -> list[CheckResult]:
    """基于指纹快照做三项校验 (纯函数, 不触碰文件系统)。"""
    check1 = CheckResult(
        name="两侧 contract_hash() 一致",
        ok=snapshot.matrag_computed == snapshot.ate_computed,
        detail=f"MATRag={snapshot.matrag_computed} ATEStudio={snapshot.ate_computed}",
    )
    check2 = CheckResult(
        name="MATRag KNOWN_CONTRACT_HASH 与计算值一致",
        ok=snapshot.matrag_known == snapshot.matrag_computed,
        detail=f"KNOWN={snapshot.matrag_known} 计算={snapshot.matrag_computed}",
    )
    check3 = CheckResult(
        name="ATEStudio KNOWN_CONTRACT_HASH 与计算值一致",
        ok=snapshot.ate_known == snapshot.ate_computed,
        detail=f"KNOWN={snapshot.ate_known} 计算={snapshot.ate_computed}",
    )
    return [check1, check2, check3]


def render_report(snapshot: HashSnapshot, results: Sequence[CheckResult]) -> str:
    """把指纹快照与校验结果渲染为人类可读报告。"""
    lines: list[str] = []
    lines.append("=== MATRag ↔ ATEStudio 契约指纹同步门 (Phase A.1) ===")
    lines.append(f"MATRag   contract_hash()      : {snapshot.matrag_computed}")
    lines.append(f"ATEStudio contract_hash()     : {snapshot.ate_computed}")
    lines.append(f"MATRag   KNOWN_CONTRACT_HASH  : {snapshot.matrag_known}")
    lines.append(f"ATEStudio KNOWN_CONTRACT_HASH : {snapshot.ate_known}")
    lines.append("")
    lines.append("校验:")
    for index, result in enumerate(results, start=1):
        status = "PASS" if result.ok else "FAIL"
        lines.append(f"[{status}] #{index} {result.name}")
        lines.append(f"          {result.detail}")
    lines.append("")
    failed = [r for r in results if not r.ok]
    if failed:
        lines.append(f"结论: 契约指纹漂移 {len(failed)} 项 (退出码 {EXIT_DRIFT})")
    else:
        lines.append(f"结论: 两侧契约指纹一致 (退出码 {EXIT_OK})")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口: 返回退出码 (0 一致 / 1 漂移 / 2 环境错误)。"""
    force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="contract_sync.check_contract_hash",
        description="跨仓契约指纹同步门: 校验 MATRag 与 ATEStudio 的 contract_hash() 及冻结基线。",
    )
    parser.add_argument("--matrag-root", default=None, help="MATRag 仓库根目录 (默认由脚本位置推断)")
    parser.add_argument(
        "--ate-root",
        default=None,
        help=f"ATEStudio 仓库根目录 (默认 ${ATE_STUDIO_ROOT_ENV} 或 ../ATEStudio)",
    )
    parser.add_argument(
        "--ate-python",
        default=None,
        help=f"ATEStudio venv 解释器路径 (默认 ${ATE_PYTHON_ENV} 或 <ate_root>/.venv)",
    )
    args = parser.parse_args(argv)

    matrag_root = Path(args.matrag_root).resolve() if args.matrag_root else resolve_matrag_root()
    ate_root = Path(args.ate_root).resolve() if args.ate_root else resolve_ate_root(matrag_root)
    python_exe = Path(args.ate_python).expanduser() if args.ate_python else None
    try:
        snapshot = build_snapshot(matrag_root, ate_root, python_exe=python_exe)
    except ContractSyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    results = run_hash_checks(snapshot)
    print(render_report(snapshot, results))
    return EXIT_OK if all(r.ok for r in results) else EXIT_DRIFT


if __name__ == "__main__":
    raise SystemExit(main())
