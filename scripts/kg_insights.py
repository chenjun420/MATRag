"""知识图谱洞察（枢纽 / 孤点 / 连通性）: 用 semantica.kg 分析器给 KG 做体检。

定位（知识缺口闭环的辅助工具，只读不改图）:

- ``CentralityCalculator``: degree / pagerank / betweenness / closeness 找枢纽
  ——图谱主干就是那些连接最多、最像"换乘站"的术语/公理/卡片;
- ``ConnectivityAnalyzer``: 连通分量 / 孤点 / 密度找"挂不上图"的节点——孤点
  （degree=0）和微小分量（2~3 个节点自成一体）是典型的**关系回填候选**
  （卡片/术语导入了但没建边）。

用法::

    python -m scripts.kg_insights                        # Markdown -> stdout
    python -m scripts.kg_insights --out data/reports/kg-insights-20261016.md
    python -m scripts.kg_insights --json                 # 机器可读
    python -m scripts.kg_insights --top-k 15 --skip-betweenness

分析器输入口径: MATRag KG JSON（``nodes``/``edges``）直接可用——semantica 的
图视图同时认 ``node_id``/``id`` 与 ``source``/``target``（见其 ``_graph_view``）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from semantica.kg import CentralityCalculator, ConnectivityAnalyzer

REPO_ROOT = Path(__file__).resolve().parents[1]
KG_PATH = REPO_ROOT / "data" / "domain" / "generated" / "power_knowledge_graph.json"

#: 微小分量上限（含）: 2~3 个节点自成一体 = 关系回填候选
SMALL_COMPONENT_MAX = 3
#: Markdown 表格里孤点/分量最多列多少条（超出给计数说明）
DISPLAY_LIMIT = 50


def load_kg(path: Path | str = KG_PATH) -> dict[str, Any]:
    """读 KG JSON（``nodes``/``edges`` 口径）; 文件缺失 / 非法 JSON 显式失败。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _node_maps(kg: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """KG → ``(node_id→label, node_id→kind)`` 映射（缺 label 回退 node_id）。"""
    labels: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for raw in kg.get("nodes", ()):  # type: ignore[union-attr]
        if not isinstance(raw, Mapping):
            continue
        node_id = str(raw.get("node_id") or "")
        if not node_id:
            continue
        labels[node_id] = str(raw.get("label") or node_id)
        kinds[node_id] = str(raw.get("kind") or "entity")
    return labels, kinds


def _rank_items(result: Any) -> list[tuple[str, float]]:
    """把 semantica 各类 center 返回的 rankings 归一为 ``(节点, 分数)`` 列表。

    degree/betweenness/closeness 的 rankings 是 ``{"node","score"}`` 字典、
    pagerank 的是 ``(node, score)`` 元组——两种形状都收。
    """
    rankings = result.get("rankings", []) if isinstance(result, Mapping) else result
    items: list[tuple[str, float]] = []
    for entry in rankings or ():
        if isinstance(entry, Mapping):
            node, score = entry.get("node"), entry.get("score")
        elif isinstance(entry, (tuple, list)) and len(entry) >= 2:
            node, score = entry[0], entry[1]
        else:
            continue
        if node is None:
            continue
        try:
            items.append((str(node), float(score)))
        except (TypeError, ValueError):
            continue
    return items


def _top_with_meta(
    items: list[tuple[str, float]],
    labels: Mapping[str, str],
    kinds: Mapping[str, str],
    top_k: int,
) -> list[dict[str, Any]]:
    """取前 top_k 个节点并附 label/kind（跨条目统一形状）。"""
    out: list[dict[str, Any]] = []
    for node_id, score in items[:top_k]:
        out.append(
            {
                "node_id": node_id,
                "label": labels.get(node_id, node_id),
                "kind": kinds.get(node_id, ""),
                "score": score,
            }
        )
    return out


def analyze(
    kg: Mapping[str, Any],
    *,
    top_k: int = 10,
    with_betweenness: bool = True,
    with_closeness: bool = True,
    source: str = "",
) -> dict[str, Any]:
    """对 KG 跑一组只读洞察: 总量 / 孤点 / 微小分量 / 枢纽 Top-K。

    Args:
        kg: KG JSON（``nodes``/``edges``）;
        top_k: 每种中心性的枢纽条数（>= 1）;
        with_betweenness / with_closeness: 大图提速开关（betweenness 最贵）;
        source: 报告里注明的数据来源（如文件路径）。

    Returns:
        可直接 ``json.dumps`` 的报告 dict（``generated_at`` / ``totals`` /
        ``orphans`` / ``small_components`` / ``top``）。
    """
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ValueError(f"top_k 须为 >= 1 的整数: {top_k!r}")
    labels, kinds = _node_maps(kg)

    calc = CentralityCalculator()
    degree = calc.calculate_degree_centrality(dict(kg))
    # 千点图 20 次迭代不收敛（实测），给足迭代次数让排名稳定
    pagerank = calc.calculate_pagerank(dict(kg), max_iterations=100)
    measures: dict[str, Any] = {"degree": degree, "pagerank": pagerank}
    if with_betweenness:
        measures["betweenness"] = calc.calculate_betweenness_centrality(dict(kg))
    if with_closeness:
        measures["closeness"] = calc.calculate_closeness_centrality(dict(kg))

    conn = ConnectivityAnalyzer().analyze_connectivity(dict(kg))

    degree_scores = degree.get("centrality", {})
    orphan_ids = sorted(
        str(node) for node, score in degree_scores.items() if float(score or 0.0) == 0.0
    )
    orphans = [
        {"node_id": node, "label": labels.get(node, node), "kind": kinds.get(node, "")}
        for node in orphan_ids
    ]

    small_components: list[dict[str, Any]] = []
    for component in conn.get("components", ()):
        size = len(component)
        if 1 < size <= SMALL_COMPONENT_MAX:
            small_components.append(
                {
                    "size": size,
                    "members": [
                        {
                            "node_id": str(node),
                            "label": labels.get(str(node), str(node)),
                            "kind": kinds.get(str(node), ""),
                        }
                        for node in sorted(str(n) for n in component)
                    ],
                }
            )
    small_components.sort(key=lambda item: (item["size"], item["members"]))

    top: dict[str, list[dict[str, Any]]] = {}
    for name, result in measures.items():
        top[name] = _top_with_meta(_rank_items(result), labels, kinds, top_k)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": source,
        "totals": {
            "nodes": int(conn.get("num_nodes") or 0),
            "edges": int(conn.get("num_edges") or 0),
            "components": int(conn.get("num_components") or 0),
            "largest_component": int(conn.get("largest_component_size") or 0),
            "isolated": len(orphans),
            "density": float(conn.get("density") or 0.0),
            "avg_degree": float(conn.get("avg_degree") or 0.0),
        },
        "orphans": orphans,
        "small_components": small_components,
        "top": top,
    }


def _table(rows: list[list[str]], header: list[str]) -> str:
    """渲染一个简单 Markdown 表。"""
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def render_markdown(report: Mapping[str, Any]) -> str:
    """把 :func:`analyze` 的报告渲染成 Markdown（可落盘为审计/巡检产物）。"""
    totals = report.get("totals", {})
    orphans: list[dict[str, Any]] = list(report.get("orphans", ()))
    small: list[dict[str, Any]] = list(report.get("small_components", ()))
    top: Mapping[str, list[dict[str, Any]]] = report.get("top", {})

    parts: list[str] = [
        "# KG 洞察报告（枢纽 / 孤点）",
        "",
        f"> 生成时间: {report.get('generated_at', '')}  来源: {report.get('source') or '(内存)'}",
        "",
        "## 总量",
        "",
        _table(
            [
                ["节点", str(totals.get("nodes", 0))],
                ["边", str(totals.get("edges", 0))],
                ["连通分量", str(totals.get("components", 0))],
                ["最大分量", str(totals.get("largest_component", 0))],
                ["孤点（degree=0）", str(totals.get("isolated", 0))],
                ["密度", f"{float(totals.get('density', 0.0)):.6f}"],
                ["平均度", f"{float(totals.get('avg_degree', 0.0)):.2f}"],
            ],
            ["指标", "值"],
        ),
        "",
        f"## 孤点（关系回填候选，共 {len(orphans)} 个）",
        "",
    ]
    if orphans:
        rows = [
            [item["node_id"], item["label"], item["kind"]]
            for item in orphans[:DISPLAY_LIMIT]
        ]
        parts.append(_table(rows, ["node_id", "label", "kind"]))
        if len(orphans) > DISPLAY_LIMIT:
            parts.append(f"\n（仅列前 {DISPLAY_LIMIT} 个，共 {len(orphans)} 个）")
    else:
        parts.append("无。")

    parts += ["", f"## 微小分量（2~{SMALL_COMPONENT_MAX} 节点，共 {len(small)} 组）", ""]
    if small:
        for index, item in enumerate(small[:DISPLAY_LIMIT], 1):
            members = "、".join(f"{m['node_id']}（{m['label']}）" for m in item["members"])
            parts.append(f"{index}. size={item['size']}: {members}")
        if len(small) > DISPLAY_LIMIT:
            parts.append(f"\n（仅列前 {DISPLAY_LIMIT} 组，共 {len(small)} 组）")
    else:
        parts.append("无。")

    titles = {
        "degree": "Degree",
        "pagerank": "PageRank",
        "betweenness": "Betweenness",
        "closeness": "Closeness",
    }
    for name, items in top.items():
        parts += ["", f"## 枢纽 Top-{len(items)}（{titles.get(name, name)}）", ""]
        if items:
            parts.append(
                _table(
                    [
                        [
                            str(index),
                            item["node_id"],
                            item["label"],
                            item["kind"],
                            f"{item['score']:.6g}",
                        ]
                        for index, item in enumerate(items, 1)
                    ],
                    ["#", "node_id", "label", "kind", "score"],
                )
            )
        else:
            parts.append("无。")
    parts.append("")
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口: 读 KG → 分析 → Markdown/stdout 或文件（失败显式返回 2）。"""
    parser = argparse.ArgumentParser(
        prog="matrag-kg-insights",
        description="知识图谱洞察（枢纽 / 孤点 / 连通性，只读）",
    )
    parser.add_argument("--graph", type=Path, default=KG_PATH, help="KG JSON 路径")
    parser.add_argument("--top-k", type=int, default=10, help="每种中心性的枢纽条数")
    parser.add_argument("--json", action="store_true", help="输出 JSON（而非 Markdown）")
    parser.add_argument("--out", type=Path, default=None, help="输出文件（默认 stdout）")
    parser.add_argument(
        "--skip-betweenness", action="store_true", help="跳过 betweenness（大图提速）"
    )
    parser.add_argument(
        "--skip-closeness", action="store_true", help="跳过 closeness（大图提速）"
    )
    args = parser.parse_args(argv)

    try:
        kg = load_kg(args.graph)
        report = analyze(
            kg,
            top_k=args.top_k,
            with_betweenness=not args.skip_betweenness,
            with_closeness=not args.skip_closeness,
            source=str(args.graph),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[kg-insights] 分析失败: {exc}", file=sys.stderr)
        return 2

    if args.json:
        text = json.dumps(report, ensure_ascii=False, indent=2)
    else:
        text = render_markdown(report)

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(
            f"[kg-insights] nodes={report['totals']['nodes']} "
            f"edges={report['totals']['edges']} 孤点={report['totals']['isolated']} "
            f"-> {args.out}",
            file=sys.stderr,
        )
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
