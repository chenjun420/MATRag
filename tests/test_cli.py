"""matrag.cli 离线单元测试（capsys + monkeypatch，不发起网络/LLM/真实文件系统）。

覆盖
----
- _cmd_parse 子命令: 解析成功 vs 失败 -> 退出码 0 / 2 + 期望输出 (含 ERROR 行);
- _cmd_doctor 子命令: 健康检查路径（llm/embedding 模块配置 + 关键依赖 importable）;
  通过 monkeypatch 替换 parse_document 与模块 __import__, 验证 fail-fast 与退出码;
- main argv 入口: parse 子命令必填 files; 缺子命令 -> SystemExit (argparse required=True)。
"""

from __future__ import annotations

import argparse
import builtins
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from matrag import cli as cli_mod


class _FakeParsed:
    """模拟 parse_document 产出 (含 save / report / ok / doc.tables)。"""

    def __init__(self, *, ok: bool, n_tables: int = 0) -> None:
        self.ok = ok
        # report 仅在 cli 失败路径需 .tables / .unresolved; 用 SimpleNamespace 兜底
        self.report = SimpleNamespace(tables=(), unresolved=())
        self.doc = SimpleNamespace(tables=lambda: tuple(object() for _ in range(n_tables)))
        # save() 返回 dict[str, Path]; 用真实 Path 模拟
        self.saved = {"md": Path("out.md"), "json": Path("out.json")}

    def save(self) -> dict[str, Path]:
        return self.saved


# ── _cmd_parse ──────────────────────────────────────────────────────────────


def test_cmd_parse_succeeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """所有文件都解析成功 -> 退出码 0 + 打印 'OK'。"""
    src = tmp_path / "doc.pdf"
    src.write_text("dummy", encoding="utf-8")
    monkeypatch.setattr(cli_mod, "parse_document", lambda p: _FakeParsed(ok=True))

    rc = cli_mod._cmd_parse(argparse.Namespace(files=[str(src)]))
    out = capsys.readouterr().out

    assert rc == 0
    assert "OK" in out
    assert "全部通过对账闸门" in out


def test_cmd_parse_fails_when_parse_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """单文件抛异常 -> 退出码 2 + 打印 FAIL 行 (不静默吞错)。"""
    src = tmp_path / "bad.pdf"
    src.write_text("dummy", encoding="utf-8")

    def _boom(p: Path) -> None:
        raise RuntimeError("hypothetical parse failure")

    monkeypatch.setattr(cli_mod, "parse_document", _boom)

    rc = cli_mod._cmd_parse(argparse.Namespace(files=[str(src)]))
    out = capsys.readouterr().out

    assert rc == 2
    assert "FAIL" in out
    assert "RuntimeError" in out
    assert "未通过对账闸门" in out


def test_cmd_parse_fails_when_gate_unmet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """单文件解析 OK 但 parsed.ok=False -> 退出码 2 + 打印 GATE 行 (非静默)。"""
    src = tmp_path / "ok-but-gate-fails.pdf"
    src.write_text("dummy", encoding="utf-8")
    monkeypatch.setattr(cli_mod, "parse_document", lambda p: _FakeParsed(ok=False))

    rc = cli_mod._cmd_parse(argparse.Namespace(files=[str(src)]))
    out = capsys.readouterr().out

    assert rc == 2
    assert "GATE" in out
    assert "未通过对账闸门" in out


# ── _cmd_doctor ─────────────────────────────────────────────────────────────


def test_cmd_doctor_with_deps_importable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """health check: llm/embedding 全配 + 关键依赖可导入 -> 退出码 0。"""
    monkeypatch.setattr(cli_mod.settings, "llm_base", "http://x", raising=False)
    monkeypatch.setattr(cli_mod.settings, "llm_api_key", "k", raising=False)
    monkeypatch.setattr(cli_mod.settings, "llm_model", "m", raising=False)
    monkeypatch.setattr(cli_mod.settings, "embed_base", "http://y", raising=False)
    monkeypatch.setattr(cli_mod.settings, "embed_api_key", "k", raising=False)
    monkeypatch.setattr(cli_mod.settings, "embed_model", "m", raising=False)

    orig_import = builtins.__import__

    def _safe_import(name: str, *args: object, **kwargs: object) -> object:
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _safe_import)

    rc = cli_mod._cmd_doctor(argparse.Namespace())
    out = capsys.readouterr().out

    assert rc == 0
    assert "configured" in out
    assert "importable" in out


def test_cmd_doctor_with_missing_dep_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """health check: 关键依赖不可导入 -> 退出码 2 (fail-fast)。"""
    orig_import = builtins.__import__

    def _broken(name: str, *args: object, **kwargs: object) -> object:
        if name == "fitz":
            raise ImportError("simulated missing dep")
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _broken)

    rc = cli_mod._cmd_doctor(argparse.Namespace())
    assert rc == 2


# ── main 入口 ──────────────────────────────────────────────────────────────


def test_main_missing_subcommand_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """argparse required=True 时缺子命令 -> SystemExit + stderr (argparse 自身行为)。"""
    with redirect_stderr(StringIO()):
        with pytest.raises(SystemExit) as exc_info:
            cli_mod.main([])
    assert exc_info.value.code == 2


def test_main_dispatch_to_doctor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """main(['doctor']) 真的派发到 _cmd_doctor, 验证 dispatcher 工作。"""
    calls: list[argparse.Namespace] = []

    def _stub_cmd(args: argparse.Namespace) -> int:
        calls.append(args)
        return 7

    monkeypatch.setattr(cli_mod, "_cmd_doctor", _stub_cmd)

    rc = cli_mod.main(["doctor"])

    assert rc == 7
    assert len(calls) == 1
