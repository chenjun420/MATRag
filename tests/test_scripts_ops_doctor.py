"""ops_doctor 测试（全离线）: 索引/审计链直查 + doctor 子进程与 HTTP 探针的桩替身。"""

from __future__ import annotations

import json
import sqlite3
import types
import urllib.error
from pathlib import Path

import pytest

from matrag.provenance import AuditTrail
from scripts import ops_doctor as od


def _make_index(db: Path, *, fts: int = 2, vec: int = 2) -> None:
    """伪造索引库（普通表占位——check_index 只做 count，不依赖 vec0/FTS5 扩展）。"""
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE chunks_fts (x TEXT)")
    conn.execute("CREATE TABLE chunks_vec (x TEXT)")
    conn.executemany("INSERT INTO chunks_fts VALUES (?)", [("a",)] * fts)
    conn.executemany("INSERT INTO chunks_vec VALUES (?)", [("b",)] * vec)
    conn.commit()
    conn.close()


def _make_provenance(db: Path, *, records: int = 1) -> None:
    """写 records 条最小审计记录（哈希链自洽）。"""
    with AuditTrail("sqlite", db_path=db) as trail:
        for index in range(records):
            trail.record_answer(query=f"问题 {index}", answer_id=f"answer:t{index}")


ALL_OK_DOCTOR = [
    {"check": "Python", "status": "ok", "note": "3.13", "hint": None},
    {"check": "semantica", "status": "ok", "note": "0.7.0", "hint": None},
    {"check": "rich", "status": "ok", "note": "15", "hint": None},
    {"check": "Log directory", "status": "ok", "note": "writable", "hint": None},
    {"check": "Graph store", "status": "fail", "note": "neo4j missing", "hint": None},
    {"check": "OpenAI", "status": "warn", "note": "no key", "hint": None},
]


def _fake_run(stdout: str, rc: int = 0):
    """替代 subprocess.run 的桩（doctor 输出可注入）。"""

    def run(cmd, **kwargs):  # noqa: ANN001, ANN003
        return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr="")

    return run


# --------------------------------------------------------------------------- #
# 索引 / 审计链
# --------------------------------------------------------------------------- #
def test_check_index_ok(tmp_path: Path) -> None:
    """双腿一致 → ok，细节含两表条数。"""
    db = tmp_path / "index.db"
    _make_index(db)
    result = od.check_index(db)
    assert result["status"] == "ok"
    assert "2 chunks" in result["note"]
    assert result["detail"]["chunks_fts"] == 2
    assert result["detail"]["chunks_vec"] == 2


def test_check_index_missing_and_empty_fail(tmp_path: Path) -> None:
    """库不存在 / 关键词腿为空 → fail。"""
    assert od.check_index(tmp_path / "nope.db")["status"] == "fail"
    db = tmp_path / "index.db"
    _make_index(db, fts=0, vec=0)
    assert od.check_index(db)["status"] == "fail"


def test_check_index_mismatch_warns(tmp_path: Path) -> None:
    """双腿条数不一致 → warn（不判失败，但必须可见）。"""
    db = tmp_path / "index.db"
    _make_index(db, fts=3, vec=2)
    assert od.check_index(db)["status"] == "warn"


def test_check_provenance_ok_and_missing(tmp_path: Path) -> None:
    """审计链自洽 → ok; 库不存在 → fail。"""
    db = tmp_path / "provenance.db"
    _make_provenance(db, records=2)
    result = od.check_provenance(db)
    assert result["status"] == "ok"
    assert result["detail"]["valid"] is True
    assert result["detail"]["total_entries"] == 2
    assert od.check_provenance(tmp_path / "nope.db")["status"] == "fail"


# --------------------------------------------------------------------------- #
# semantica doctor 子进程
# --------------------------------------------------------------------------- #
def test_doctor_core_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """核心检查 fail → doctor 整体 fail。"""
    checks = [{"check": "Python", "status": "fail", "note": "-", "hint": None}]
    monkeypatch.setattr(od.subprocess, "run", _fake_run(json.dumps(checks)))
    result = od.check_semantica_doctor()
    assert result["status"] == "fail"
    assert "核心检查未过" in result["note"]


def test_doctor_non_core_warn_vs_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    """核心 ok + 部署相关非 ok: 默认 warn，strict 判 fail。"""
    monkeypatch.setattr(od.subprocess, "run", _fake_run(json.dumps(ALL_OK_DOCTOR)))
    assert od.check_semantica_doctor()["status"] == "warn"
    assert od.check_semantica_doctor(strict=True)["status"] == "fail"


def test_doctor_parses_json_after_log_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON 前面混入日志行时，取最后一行仍能解析。"""
    stdout = "2026-10-16 WARNING noise\n" + json.dumps(ALL_OK_DOCTOR) + "\n"
    monkeypatch.setattr(od.subprocess, "run", _fake_run(stdout))
    assert od.check_semantica_doctor()["status"] == "warn"


def test_doctor_unparsable_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """输出完全不可解析 → fail。"""
    monkeypatch.setattr(od.subprocess, "run", _fake_run("not json at all", rc=1))
    result = od.check_semantica_doctor()
    assert result["status"] == "fail"
    assert "不可解析" in result["note"]


def test_doctor_missing_semantica(monkeypatch: pytest.MonkeyPatch) -> None:
    """解释器跑不起 semantica.cli（OSError）→ fail，不炸巡检。"""

    def boom(cmd, **kwargs):  # noqa: ANN001, ANN003
        raise FileNotFoundError("python not found")

    monkeypatch.setattr(od.subprocess, "run", boom)
    assert od.check_semantica_doctor()["status"] == "fail"


# --------------------------------------------------------------------------- #
# HTTP 探针
# --------------------------------------------------------------------------- #
def test_probe_url_http_error_is_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """401 = 服务在 + 鉴权拦截（正常），不是故障。"""

    def raise_401(request, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(od.urllib.request, "urlopen", raise_401)
    result = od.probe_url("explorer", "http://127.0.0.1:8899")
    assert result["status"] == "ok"
    assert "鉴权拦截" in result["note"]


def test_probe_url_unreachable_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接失败 → fail。"""

    def refuse(request, timeout=None):  # noqa: ANN001
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(od.urllib.request, "urlopen", refuse)
    assert od.probe_url("gateway", "http://127.0.0.1:8321/mcp")["status"] == "fail"


def test_probe_url_5xx_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """5xx → fail（服务在但内部故障）。"""

    class _Resp:
        status = 503

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *args):  # noqa: ANN002
            return False

    monkeypatch.setattr(od.urllib.request, "urlopen", lambda request, timeout=None: _Resp())
    assert od.probe_url("gateway", "http://127.0.0.1:8321/mcp")["status"] == "fail"


# --------------------------------------------------------------------------- #
# 汇总 / CLI
# --------------------------------------------------------------------------- #
def test_main_json_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """全绿 → 0（warn 不拦）; 索引缺失 → 1; --json 输出可解析。"""
    store = tmp_path / "store"
    store.mkdir()
    _make_index(store / "index.db")
    _make_provenance(store / "provenance.db")
    monkeypatch.setattr(od.subprocess, "run", _fake_run(json.dumps(ALL_OK_DOCTOR)))

    rc = od.main(["--store-dir", str(store), "--json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["summary"]["fail"] == 0
    assert report["summary"]["warn"] == 1  # doctor 非核心的 Graph store fail + OpenAI warn

    empty = tmp_path / "empty"
    empty.mkdir()
    _make_provenance(empty / "provenance.db")
    rc2 = od.main(["--store-dir", str(empty), "--json", "--no-doctor"])
    assert rc2 == 1
    report2 = json.loads(capsys.readouterr().out)
    assert report2["ok"] is False
    assert report2["summary"]["fail"] == 1  # 索引库缺失


def test_main_text_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """行文本模式含 [ok]/[PASS] 与摘要。"""
    store = tmp_path / "store"
    store.mkdir()
    _make_index(store / "index.db")
    _make_provenance(store / "provenance.db")
    rc = od.main(["--store-dir", str(store), "--no-doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[ok] index:" in out
    assert "[PASS]" in out
