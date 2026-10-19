"""explorer 启动器测试（全离线）: KG→ContextGraph 映射 / 环境与命令构造 / 鉴权 fail-fast。"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import explorer_serve as es

KG_SAMPLE = {
    "nodes": [
        {"node_id": "term:T-A", "kind": "term", "label": "术语 A", "attrs": {"aliases": ["TA"]}},
        {"node_id": "axiom:A-1", "kind": "axiom", "label": "公理 1", "attrs": {}},
    ],
    "edges": [
        {"source": "axiom:A-1", "target": "term:T-A", "relation": "denotes", "attrs": {"note": "n"}},
    ],
}


def test_kg_to_contextgraph_maps_kind_and_attrs() -> None:
    """节点 kind→type、label→content、attrs→properties；边 relation→type。"""
    out = es.kg_to_contextgraph(KG_SAMPLE)
    assert [n["id"] for n in out["nodes"]] == ["term:T-A", "axiom:A-1"]
    assert [n["type"] for n in out["nodes"]] == ["term", "axiom"]
    assert out["nodes"][0]["content"] == "术语 A"
    assert out["nodes"][0]["properties"] == {"aliases": ["TA"]}
    assert "properties" not in out["nodes"][1]
    assert out["edges"][0] == {
        "source": "axiom:A-1",
        "target": "term:T-A",
        "type": "denotes",
        "properties": {"note": "n"},
    }


def test_kg_to_contextgraph_skips_broken_and_defaults() -> None:
    """缺 id 的节点/缺端点的边跳过；缺 kind 默认 entity。"""
    out = es.kg_to_contextgraph(
        {
            "nodes": [{"node_id": "n1"}, {"label": "无 id"}, "not-a-dict"],
            "edges": [
                {"source": "n1", "target": "n2", "relation": "x", "attrs": {}},
                {"source": "n1"},
            ],
        }
    )
    assert out["nodes"] == [{"id": "n1", "type": "entity", "content": "n1"}]
    assert out["edges"] == [{"source": "n1", "target": "n2", "type": "x"}]


def test_converted_graph_roundtrip_preserves_node_type() -> None:
    """转换产物经 semantica ContextGraph 加载: node_type / 属性 / 边数保留。"""
    from semantica.context.context_graph import ContextGraph

    out = es.kg_to_contextgraph(KG_SAMPLE)
    loaded = ContextGraph()
    assert loaded.add_nodes(out["nodes"]) == 2
    node = loaded.nodes["term:T-A"]
    assert node.node_type == "term"
    assert node.properties.get("aliases") == ["TA"]
    assert loaded.add_edges(out["edges"]) == 1


def test_write_contextgraph(tmp_path: Path) -> None:
    """落盘路径默认 *.explorer.json，内容为转换结果。"""
    src = tmp_path / "kg.json"
    src.write_text(json.dumps(KG_SAMPLE, ensure_ascii=False), encoding="utf-8")
    out = es.write_contextgraph(src)
    assert out == tmp_path / "kg.explorer.json"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["nodes"]) == 2
    assert len(data["edges"]) == 1


def test_build_child_env(tmp_path: Path, monkeypatch) -> None:
    """UTF-8 必备项 + 隔离依赖注入 PYTHONPATH + 匿名开关设置/清除。"""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("SEMANTICA_ALLOW_ANONYMOUS", raising=False)
    env = es.build_child_env(tmp_path, allow_anonymous=True)
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONPATH"].startswith(str(tmp_path))
    assert env["SEMANTICA_ALLOW_ANONYMOUS"] == "true"

    monkeypatch.setenv("PYTHONPATH", "orig")
    monkeypatch.setenv("SEMANTICA_ALLOW_ANONYMOUS", "true")
    env2 = es.build_child_env(None, allow_anonymous=False)
    assert env2["PYTHONPATH"] == "orig"
    assert "SEMANTICA_ALLOW_ANONYMOUS" not in env2


def test_build_command_headless(tmp_path: Path) -> None:
    """命令走 python -m semantica.explorer，默认带 --no-browser。"""
    cmd = es.build_command("py", tmp_path / "g.json", "127.0.0.1", 8899, open_browser=False)
    assert cmd[:3] == ["py", "-m", "semantica.explorer"]
    assert "--graph" in cmd and "--no-browser" in cmd
    assert cmd[cmd.index("--port") + 1] == "8899"

    cmd2 = es.build_command("py", tmp_path / "g.json", "127.0.0.1", 8899, open_browser=True)
    assert "--no-browser" not in cmd2


def test_main_fail_fast_when_exposed_without_key(monkeypatch, capsys) -> None:
    """非回环绑定 + 无 API Key + 未显式匿名 → 退出码 2，不拉起任何进程。"""
    monkeypatch.delenv("SEMANTICA_API_KEY", raising=False)
    rc = es.main(["--host", "0.0.0.0", "--port", "8899"])
    assert rc == 2
    assert "SEMANTICA_API_KEY" in capsys.readouterr().err


class _FakeProc:
    """subprocess.run 桩（install 分支只关心被调到）。"""

    returncode = 0


def test_ensure_deps_venv_fastapi_skips_without_gensim(tmp_path: Path, monkeypatch) -> None:
    """venv 自带 fastapi 且隔离目录无 gensim → 不注入 PYTHONPATH。"""
    monkeypatch.setattr(es, "_fastapi_importable", lambda: True)
    assert es.ensure_deps(tmp_path) is None


def test_ensure_deps_injects_gensim_dir_when_present(tmp_path: Path, monkeypatch) -> None:
    """venv 自带 fastapi 但隔离目录已装 gensim → 仍注入（Node2Vec 解锁不静默失效）。"""
    monkeypatch.setattr(es, "_fastapi_importable", lambda: True)
    (tmp_path / "gensim").mkdir()
    assert es.ensure_deps(tmp_path) == tmp_path


def test_ensure_deps_installs_fastapi_when_missing(tmp_path: Path, monkeypatch) -> None:
    """无 fastapi 且隔离目录为空 → 走 pip --target 安装后返回隔离目录。"""
    monkeypatch.setattr(es, "_fastapi_importable", lambda: False)
    calls: list[list[str]] = []

    def fake_run(cmd, check):  # noqa: ANN001
        calls.append(list(cmd))
        return _FakeProc()

    monkeypatch.setattr(es.subprocess, "run", fake_run)
    assert es.ensure_deps(tmp_path) == tmp_path
    assert calls and "fastapi" in calls[0] and str(tmp_path) in calls[0]
