"""semantica Knowledge Explorer 启动器（P4-3: 独立进程只读可视化）。

把 MATRag 域知识图谱 JSON（``node_id/kind/label/attrs`` 口径）转成
semantica ContextGraph 可加载的 JSON（``id/type/content/properties``：
``kind`` 映射为节点类型、``attrs`` 映射为属性），再拉起 ``semantica.explorer``
（FastAPI, 只读浏览）。

用法::

    python -m scripts.explorer_serve                          # 127.0.0.1:8899（本地免鉴权）
    python -m scripts.explorer_serve --open-browser           # 启动后自动开浏览器
    python -m scripts.explorer_serve --host 0.0.0.0           # 对外暴露: 需 SEMANTICA_API_KEY

链路口径::

    KG JSON（data/domain/generated/power_knowledge_graph.json）
      → kg_to_contextgraph() 转换（落盘 *.explorer.json）
      → semantica.explorer（需要 fastapi; 见下）
      → http://host:port 浏览（/api/graph/* 只读查询; /docs 为 OpenAPI）

依赖: venv 未预装 fastapi 时，自动用 ``pip install --target`` 装到仓库内
隔离目录 ``.tools/explorer-deps``（不污染 venv），子进程经 PYTHONPATH 注入。
可选能力（gensim → Node2Vec 图嵌入）装进同一隔离目录即随子进程解锁:
``pip install --target .tools/explorer-deps gensim``。

鉴权: 非回环绑定必须给 ``SEMANTICA_API_KEY``（explorer 原生校验），除非显式
``--allow-anonymous``（仅限可信网络）；回环绑定默认匿名（本机开发）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
KG_PATH = REPO_ROOT / "data" / "domain" / "generated" / "power_knowledge_graph.json"
DEPS_DIR = REPO_ROOT / ".tools" / "explorer-deps"

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def kg_to_contextgraph(kg: Mapping[str, Any]) -> dict[str, Any]:
    """把 MATRag 域知识图谱 JSON 转成 ContextGraph 可加载的 JSON。

    节点: ``node_id→id`` / ``kind→type`` / ``label→content`` / ``attrs→properties``;
    边: ``source/target`` 直传, ``relation→type``, ``attrs→properties``。
    缺 id 的节点与缺端点的边跳过（不静默造数据）。
    """
    nodes: list[dict[str, Any]] = []
    for raw in kg.get("nodes", ()):
        if not isinstance(raw, Mapping):
            continue
        node_id = raw.get("node_id")
        if not node_id:
            continue
        node: dict[str, Any] = {
            "id": str(node_id),
            "type": str(raw.get("kind") or "entity"),
            "content": str(raw.get("label") or node_id),
        }
        attrs = raw.get("attrs")
        if attrs:
            node["properties"] = dict(attrs)
        nodes.append(node)

    edges: list[dict[str, Any]] = []
    for raw in kg.get("edges", ()):
        if not isinstance(raw, Mapping):
            continue
        source, target = raw.get("source"), raw.get("target")
        if not source or not target:
            continue
        edge: dict[str, Any] = {
            "source": str(source),
            "target": str(target),
            "type": str(raw.get("relation") or "related"),
        }
        attrs = raw.get("attrs")
        if attrs:
            edge["properties"] = dict(attrs)
        edges.append(edge)

    return {"nodes": nodes, "edges": edges}


def write_contextgraph(kg_path: Path = KG_PATH, out_path: Path | None = None) -> Path:
    """读取 KG JSON、转换并落盘（默认同目录 ``*.explorer.json``），返回产物路径。"""
    out_path = out_path or kg_path.with_suffix(".explorer.json")
    kg = json.loads(kg_path.read_text(encoding="utf-8"))
    converted = kg_to_contextgraph(kg)
    out_path.write_text(
        json.dumps(converted, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return out_path


def _fastapi_importable() -> bool:
    """当前解释器是否可直接导入 fastapi（venv 自带时不需要隔离目录）。"""
    try:
        import fastapi  # noqa: F401

        return True
    except ImportError:
        return False


def ensure_deps(deps_dir: Path = DEPS_DIR) -> Path | None:
    """保证 fastapi 可用；返回需注入 PYTHONPATH 的目录（None = 无需注入）。

    优先用 venv 自带 fastapi；否则用仓库内隔离目录 ``.tools/explorer-deps``
    （不存在则 ``pip install --target`` 落盘，仅 fastapi 及其依赖）。

    另一情形: venv 自带 fastapi，但隔离目录里已装了**可选能力**（如 gensim →
    Node2Vec 图嵌入），此时仍返回隔离目录注入 PYTHONPATH——否则"装好了却不生效"
    就是静默失效。
    """
    if not _fastapi_importable():
        if not (deps_dir / "fastapi").is_dir():
            print(f"[explorer] 安装隔离依赖 fastapi -> {deps_dir}", file=sys.stderr)
            subprocess.run(
                [
                    sys.executable, "-m", "pip", "install", "--quiet",
                    "--disable-pip-version-check", "--target", str(deps_dir), "fastapi",
                ],
                check=True,
            )
        return deps_dir
    return deps_dir if (deps_dir / "gensim").is_dir() else None


def build_child_env(deps_dir: Path | None, *, allow_anonymous: bool) -> dict[str, str]:
    """构造 explorer 子进程环境: UTF-8 + （可选）隔离依赖 PYTHONPATH + 匿名开关。

    ``allow_anonymous=False`` 时主动移除 ``SEMANTICA_ALLOW_ANONYMOUS``，
    确保非回环暴露走 explorer 原生 API Key 校验（不继承外部环境残留）。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if deps_dir is not None:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(deps_dir) + (os.pathsep + existing if existing else "")
    if allow_anonymous:
        env["SEMANTICA_ALLOW_ANONYMOUS"] = "true"
    else:
        env.pop("SEMANTICA_ALLOW_ANONYMOUS", None)
    return env


def build_command(
    python: str,
    graph: Path,
    host: str,
    port: int,
    *,
    open_browser: bool,
) -> list[str]:
    """拼装 ``semantica.explorer`` 启动命令（默认不自动开浏览器）。"""
    cmd = [
        python, "-m", "semantica.explorer",
        "--graph", str(graph),
        "--host", host,
        "--port", str(port),
    ]
    if not open_browser:
        cmd.append("--no-browser")
    return cmd


def main(argv: list[str] | None = None) -> int:
    """CLI 入口: 解析参数 → 鉴权预检 → 转换 KG → 依赖就绪 → 拉起 explorer。"""
    parser = argparse.ArgumentParser(
        prog="matrag-explorer",
        description="MATRag KG 的 semantica Explorer 启动器（只读可视化）",
    )
    parser.add_argument(
        "--graph", type=Path, default=KG_PATH,
        help="MATRag KG JSON（自动转 ContextGraph 格式）",
    )
    parser.add_argument("--port", type=int, default=8899, help="监听端口（默认 8899）")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1）")
    parser.add_argument("--open-browser", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument(
        "--allow-anonymous", action="store_true",
        help="显式允许匿名访问（含非回环绑定；仅限可信网络）",
    )
    args = parser.parse_args(argv)

    loopback = args.host in _LOOPBACK_HOSTS
    if not loopback and not os.environ.get("SEMANTICA_API_KEY") and not args.allow_anonymous:
        print(
            "[explorer] 非回环绑定需要 SEMANTICA_API_KEY（或 --allow-anonymous，仅限可信网络）",
            file=sys.stderr,
        )
        return 2

    graph = write_contextgraph(args.graph)
    print(f"[explorer] ContextGraph -> {graph}", file=sys.stderr)
    deps = ensure_deps()
    env = build_child_env(deps, allow_anonymous=loopback or args.allow_anonymous)
    cmd = build_command(
        sys.executable, graph, args.host, args.port, open_browser=args.open_browser
    )
    print(f"[explorer] http://{args.host}:{args.port}  ({' '.join(cmd)})", file=sys.stderr)
    try:
        return subprocess.call(cmd, env=env)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
