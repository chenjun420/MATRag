"""运维巡检: 索引 / 审计链 / semantica 组件 / 服务连通（P4 运维）。

设计约束:

- 本机检查只读（索引库与审计库都以 sqlite 只读模式 / 只读 API 打开）;
- ``semantica doctor`` 子进程体检: **核心运行时检查**（Python / semantica /
  rich / 日志目录）必须 ok; 部署相关的组件缺失（Neo4j / faiss /
  sentence-transformers / 云端 LLM key 等）记为非核心——本项目走 SQLite +
  远程 embedding，这些缺失是常态而非故障，默认不判失败（``--strict`` 时判）;
- 关键检查失败 → 退出码 1（供 cron / systemd timer 巡检; 失败即告警）;
- HTTP 探针: 任何 HTTP 响应（含 401/403/404）都算"服务在"——连接被拒 /
  超时 / 5xx 才算故障。

用法::

    python -m scripts.ops_doctor                                    # 本机巡检
    python -m scripts.ops_doctor --json
    python -m scripts.ops_doctor --strict
    python -m scripts.ops_doctor --gateway-url http://127.0.0.1:8321/mcp \\
                                 --explorer-url http://127.0.0.1:8899
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from matrag.config import settings
from matrag.provenance import DB_FILENAME, AuditTrail, ProvenanceError

#: semantica doctor 的"核心运行时"检查名: 这些必须 ok，其余组件缺失只记 warn
DOCTOR_CORE_CHECKS: frozenset[str] = frozenset(
    {"Python", "semantica", "rich", "Log directory"}
)
#: doctor 子进程超时（秒）
DOCTOR_TIMEOUT_S = 180
#: HTTP 探针超时（秒）
PROBE_TIMEOUT_S = 10.0


def _check(name: str, status: str, note: str, detail: Any = None) -> dict[str, Any]:
    """构造一条检查结果（``status ∈ {ok, warn, fail}``）。"""
    out: dict[str, Any] = {"check": name, "status": status, "note": note}
    if detail is not None:
        out["detail"] = detail
    return out


def check_index(db_path: Path) -> dict[str, Any]:
    """索引库体检: 存在性 + 双表条数（chunks_fts / chunks_vec，只读打开）。"""
    if not db_path.exists():
        return _check("index", "fail", f"索引库不存在: {db_path}")
    size_mb = db_path.stat().st_size / 1e6
    counts: dict[str, int | None] = {}
    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return _check("index", "fail", f"打开失败: {db_path}（{type(exc).__name__}: {exc}）")
    try:
        for table in ("chunks_fts", "chunks_vec"):
            try:
                row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
                counts[table] = int(row[0])
            except sqlite3.Error:
                counts[table] = None
    finally:
        conn.close()

    keyword, vector = counts.get("chunks_fts"), counts.get("chunks_vec")
    detail = {
        "db": str(db_path),
        "size_mb": round(size_mb, 1),
        "chunks_fts": keyword,
        "chunks_vec": vector,
    }
    if not keyword:
        return _check(
            "index", "fail",
            f"关键词腿为空/不可读: chunks_fts={keyword}（{size_mb:.1f} MB）", detail,
        )
    if vector is None:
        return _check(
            "index", "warn",
            f"向量腿不可数（vec0 虚表计数不支持?）; 关键词腿 {keyword} chunks"
            f"（{size_mb:.1f} MB）",
            detail,
        )
    if vector != keyword:
        return _check(
            "index", "warn",
            f"双腿条数不一致: fts={keyword} vec={vector}（{size_mb:.1f} MB）",
            detail,
        )
    return _check("index", "ok", f"{keyword} chunks（双腿一致），{size_mb:.1f} MB", detail)


def check_provenance(db_path: Path) -> dict[str, Any]:
    """审计链体检: 库存在 + 全库哈希链校验（valid / 总条数 / 断链数）。"""
    if not db_path.exists():
        return _check("provenance", "fail", f"审计库不存在: {db_path}")
    try:
        chain = AuditTrail("sqlite", db_path=db_path).verify_chain()
    except (ProvenanceError, ValueError) as exc:
        return _check("provenance", "fail", f"审计链校验失败: {type(exc).__name__}: {exc}")
    valid = bool(chain.get("valid"))
    total = int(chain.get("total_entries") or 0)
    broken = chain.get("broken_links") or []
    detail = {"db": str(db_path), "valid": valid, "total_entries": total,
              "broken_links": len(broken)}
    if valid:
        return _check("provenance", "ok", f"链 valid，{total} 条记录，0 断链", detail)
    return _check(
        "provenance", "fail", f"链损坏: {len(broken)} 处断链（{total} 条记录）", detail
    )


def check_semantica_doctor(*, strict: bool = False) -> dict[str, Any]:
    """``semantica doctor`` 子进程体检（核心运行时 + 组件清单摘要）。

    走 ``python -m semantica.cli``（semantica 包无 ``__main__``，不能 ``-m semantica``）。
    核心检查必须全 ok; 非核心的 ok 之外状态默认记 ``warn``，``strict=True`` 记 ``fail``。
    """
    cmd = [sys.executable, "-m", "semantica.cli", "--json", "doctor"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DOCTOR_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _check(
            "semantica-doctor", "fail",
            f"doctor 调用失败: {type(exc).__name__}: {exc}",
        )

    raw = (proc.stdout or "").strip()
    checks: Any = None
    for candidate in (raw, raw.splitlines()[-1] if raw else ""):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, IndexError):
            continue
        if isinstance(parsed, list):
            checks = parsed
            break
    if not isinstance(checks, list):
        err_tail = (proc.stderr or "").strip()[-200:]
        return _check(
            "semantica-doctor", "fail",
            f"doctor 输出不可解析（rc={proc.returncode}）: {raw[-200:] or err_tail}",
        )

    statuses = {
        str(item.get("check")): str(item.get("status"))
        for item in checks
        if isinstance(item, dict)
    }
    core_bad = sorted(name for name in DOCTOR_CORE_CHECKS if statuses.get(name) != "ok")
    non_ok = sorted(
        f"{name}:{status}" for name, status in statuses.items() if status != "ok"
    )
    if core_bad:
        return _check(
            "semantica-doctor", "fail",
            f"核心检查未过: {core_bad}; 非 ok: {non_ok}", detail=checks,
        )
    if non_ok:
        status = "fail" if strict else "warn"
        return _check(
            "semantica-doctor", status,
            f"核心 ok（{len(DOCTOR_CORE_CHECKS)}/{len(DOCTOR_CORE_CHECKS)}）；"
            f"部署相关非 ok: {non_ok}",
            detail=checks,
        )
    return _check(
        "semantica-doctor", "ok", f"{len(checks)} 项全部 ok", detail=checks
    )


def probe_url(name: str, url: str, *, timeout: float = PROBE_TIMEOUT_S) -> dict[str, Any]:
    """HTTP 可达性探针: 任何 HTTP 响应都算"服务在"（401/403 = 鉴权拦截，正常）。

    连接被拒 / DNS 失败 / 超时 / 5xx → ``fail``。
    """
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "matrag-ops-doctor"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code = int(response.status)
    except urllib.error.HTTPError as exc:  # 有响应 = 服务在（含 4xx）
        code = int(exc.code)
    except (urllib.error.URLError, OSError) as exc:
        return _check(name, "fail", f"不可达: {url}（{type(exc).__name__}: {exc}）")
    if code >= 500:
        return _check(name, "fail", f"{url} -> HTTP {code}")
    auth_note = "（鉴权拦截，正常）" if code in (401, 403) else ""
    return _check(name, "ok", f"{url} -> HTTP {code}{auth_note}")


def run_checks(
    store_dir: Path,
    *,
    gateway_url: str | None = None,
    explorer_url: str | None = None,
    strict: bool = False,
    with_doctor: bool = True,
) -> dict[str, Any]:
    """跑一组巡检并汇总（``{"ok", "checks", "summary"}``）。"""
    checks: list[dict[str, Any]] = [
        check_index(store_dir / "index.db"),
        check_provenance(store_dir / DB_FILENAME),
    ]
    if with_doctor:
        checks.append(check_semantica_doctor(strict=strict))
    if gateway_url:
        checks.append(probe_url("gateway", gateway_url))
    if explorer_url:
        checks.append(probe_url("explorer", explorer_url))

    summary = {
        "total": len(checks),
        "ok": sum(1 for item in checks if item["status"] == "ok"),
        "warn": sum(1 for item in checks if item["status"] == "warn"),
        "fail": sum(1 for item in checks if item["status"] == "fail"),
    }
    return {"ok": summary["fail"] == 0, "checks": checks, "summary": summary}


def main(argv: list[str] | None = None) -> int:
    """CLI 入口: 解析参数 → 巡检 → 报告; 有 fail 则退出码 1。"""
    parser = argparse.ArgumentParser(
        prog="matrag-ops-doctor",
        description="MATRag 运维巡检（索引 / 审计链 / semantica 组件 / 服务连通）",
    )
    parser.add_argument(
        "--store-dir", type=Path, default=None,
        help="数据目录（默认 settings.store_dir; 内含 index.db 与 provenance.db）",
    )
    parser.add_argument("--gateway-url", default=None, help="网关探针地址（如 http://127.0.0.1:8321/mcp）")
    parser.add_argument("--explorer-url", default=None, help="explorer 探针地址（如 http://127.0.0.1:8899）")
    parser.add_argument("--strict", action="store_true", help="doctor 非核心项非 ok 也判失败")
    parser.add_argument("--no-doctor", action="store_true", help="跳过 semantica doctor 子进程")
    parser.add_argument("--json", action="store_true", help="输出 JSON（而非行文本）")
    args = parser.parse_args(argv)

    store_dir = args.store_dir if args.store_dir is not None else settings.store_dir
    report = run_checks(
        store_dir,
        gateway_url=args.gateway_url,
        explorer_url=args.explorer_url,
        strict=args.strict,
        with_doctor=not args.no_doctor,
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report["checks"]:
            print(f"[{item['status']}] {item['check']}: {item['note']}")
        summary = report["summary"]
        verdict = "PASS" if report["ok"] else "FAIL"
        print(
            f"[{verdict}] {summary['total']} 项检查: "
            f"ok={summary['ok']} warn={summary['warn']} fail={summary['fail']}"
        )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
