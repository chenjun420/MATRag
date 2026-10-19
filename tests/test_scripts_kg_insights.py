"""kg_insights 测试（全离线）: 孤点/微小分量识别 + 枢纽排名 + 渲染与 CLI。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import kg_insights as ki

KG_SMALL = {
    "nodes": [
        {"node_id": "hub", "kind": "term", "label": "枢纽术语"},
        {"node_id": "a", "kind": "term", "label": "术语 A"},
        {"node_id": "b", "kind": "axiom", "label": "公理 B"},
        {"node_id": "orphan", "kind": "card", "label": "孤点卡片"},
        {"node_id": "pair1", "kind": "card", "label": "对 1"},
        {"node_id": "pair2", "kind": "card", "label": "对 2"},
    ],
    "edges": [
        {"source": "hub", "target": "a", "relation": "denotes"},
        {"source": "hub", "target": "b", "relation": "constrains"},
        {"source": "pair1", "target": "pair2", "relation": "related"},
    ],
}


def test_analyze_finds_orphans_and_hubs() -> None:
    """孤点、微小分量、连通分量与枢纽排名一次到位。"""
    report = ki.analyze(KG_SMALL, top_k=3, source="mem")
    assert report["totals"]["nodes"] == 6
    assert report["totals"]["edges"] == 3
    assert report["totals"]["components"] == 3
    assert report["totals"]["largest_component"] == 3
    assert report["totals"]["isolated"] == 1
    assert [o["node_id"] for o in report["orphans"]] == ["orphan"]
    assert report["orphans"][0]["label"] == "孤点卡片"
    assert report["top"]["degree"][0]["node_id"] == "hub"
    assert [c["size"] for c in report["small_components"]] == [2, 3]
    assert [m["node_id"] for m in report["small_components"][0]["members"]] == ["pair1", "pair2"]
    assert [m["node_id"] for m in report["small_components"][1]["members"]] == ["a", "b", "hub"]
    assert set(report["top"]) == {"degree", "pagerank", "betweenness", "closeness"}


def test_analyze_skips_optional_measures() -> None:
    """关掉 genkins 贵项后只保留 degree/pagerank。"""
    report = ki.analyze(KG_SMALL, top_k=2, with_betweenness=False, with_closeness=False)
    assert set(report["top"]) == {"degree", "pagerank"}


def test_analyze_rejects_bad_top_k() -> None:
    """top_k 非法 fail-fast（布尔也是 int 的坑要挡住）。"""
    with pytest.raises(ValueError):
        ki.analyze(KG_SMALL, top_k=0)
    with pytest.raises(ValueError):
        ki.analyze(KG_SMALL, top_k=True)  # type: ignore[arg-type]


def test_render_markdown_contains_sections() -> None:
    """Markdown 渲染: 总量 / 孤点 / 微小分量 / 枢纽分段都在。"""
    report = ki.analyze(KG_SMALL, top_k=3, source="mem")
    md = ki.render_markdown(report)
    assert "# KG 洞察报告" in md
    assert "孤点" in md and "孤点卡片" in md
    assert "微小分量" in md and "pair1" in md
    assert "枢纽 Top-3（Degree）" in md
    assert "枢纽 Top-3（PageRank）" in md


def test_main_out_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI --out: 落盘 Markdown + stderr 摘要。"""
    src = tmp_path / "kg.json"
    src.write_text(json.dumps(KG_SMALL, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "kg-insights.md"
    rc = ki.main(["--graph", str(src), "--out", str(out), "--top-k", "2"])
    assert rc == 0
    assert "孤点" in out.read_text(encoding="utf-8")
    assert "孤点=1" in capsys.readouterr().err


def test_main_json_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI --json: stdout 是可解析 JSON。"""
    src = tmp_path / "kg.json"
    src.write_text(json.dumps(KG_SMALL, ensure_ascii=False), encoding="utf-8")
    rc = ki.main(
        ["--graph", str(src), "--json", "--skip-betweenness", "--skip-closeness"]
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["totals"]["nodes"] == 6


def test_main_missing_graph_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """KG 文件不存在 → 退出码 2。"""
    rc = ki.main(["--graph", str(tmp_path / "nope.json")])
    assert rc == 2
    assert "分析失败" in capsys.readouterr().err
