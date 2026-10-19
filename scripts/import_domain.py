#!/usr/bin/env python
"""域知识导入冒烟脚本: power 域知识 + 规则（本仓 data/domain/source）→ data/domain/generated 产物。

用法::

    python scripts/import_domain.py                       # 全部用默认路径
    python scripts/import_domain.py --source <knowledge.md> --out <dir>
    python scripts/import_domain.py --rules <rules.yaml> --out <dir>

产物:
- ``knowledge_cards.jsonl``: 每行一张域知识卡片 (dict);
- ``power_methods.jsonl``: 每行一张产测方法卡片 (dict, 公共知识);
- ``power_rules.yaml``: 规则集副本 (与源文件一致);
- ``power_rules_datalog.dl``: 132 条规则转出的 Datalog Horn 子句集;
- ``power_ontology.ttl`` / ``power_ontology.json``: SHACL → 电力域本体 (OWL);
- ``power_knowledge_graph.json``: 五层连图
  (产品-参数-术语-规则-方法-本体) + 覆盖率报告。

``--skip-rules`` 时同时跳过本体与知识图谱产物 (二者由规则集派生)。

退出码: 0 = 成功; 1 = 失败 (找不到源文件/结构非法), 失败即非零退出。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from matrag.domain.cards import (  # noqa: E402
    DEFAULT_KNOWLEDGE_PATH,
    DEFAULT_RULES_PATH,
    load_knowledge_cards,
    resolve_knowledge_path,
    resolve_rules_path,
    write_knowledge_cards,
)
from matrag.domain.knowledge_graph import write_knowledge_graph  # noqa: E402
from matrag.domain.ontology import build_ontology, write_ontology  # noqa: E402
from matrag.domain.rules_loader import load_rules, write_generated  # noqa: E402
from matrag.domain.test_methods import (  # noqa: E402
    build_method_cards,
    load_test_methods,
    test_method_stats,
)


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI 参数。"""
    parser = argparse.ArgumentParser(
        prog="import_domain",
        description="导入 power 域知识与规则（内联数据源）, 生成本地语料/规则产物",
    )
    parser.add_argument(
        "--source",
        default=None,
        help=f"知识文档路径 (默认: 环境变量或 {DEFAULT_KNOWLEDGE_PATH})",
    )
    parser.add_argument(
        "--rules",
        default=None,
        help=f"规则集 rules.yaml 路径 (默认: 环境变量或 {DEFAULT_RULES_PATH})",
    )
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "data" / "domain" / "generated"),
        help="产物输出目录 (默认: data/domain/generated)",
    )
    parser.add_argument(
        "--skip-rules",
        action="store_true",
        help="只导入知识卡片, 不生成规则产物",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """脚本入口。"""
    args = build_parser().parse_args(argv)
    out_dir = Path(args.out)

    knowledge_path = resolve_knowledge_path(args.source)
    cards = load_knowledge_cards(knowledge_path)
    if not cards:
        print(f"[FAIL] 知识文档没有可切分的小节: {knowledge_path}", file=sys.stderr)
        return 1
    jsonl = write_knowledge_cards(cards, out_dir / "knowledge_cards.jsonl")

    total_chars = sum(card.char_count for card in cards)
    print(f"[OK] 知识卡片: {len(cards)} 张, 正文总字数 {total_chars}")
    print(f"     源: {knowledge_path}")
    print(f"     产物: {jsonl}")
    for card in cards[:3]:
        print(f"     - {card.card_id}: {card.section_path} ({card.char_count} 字)")
    if len(cards) > 3:
        print(f"     - ... 其余 {len(cards) - 3} 张")

    methods = load_test_methods()
    method_cards = build_method_cards(methods)
    methods_jsonl = write_knowledge_cards(
        method_cards, out_dir / "power_methods.jsonl"
    )
    mst = test_method_stats(methods)
    print(
        f"[OK] 产测方法: {mst['total']} 条 (类别 {len(mst['by_category'])}"
        f" / 关联术语 {mst['measures_count']} / 关联规则 {mst['rules_count']})"
    )
    print(f"     产物: {methods_jsonl}")

    if not args.skip_rules:
        rules_path = resolve_rules_path(args.rules)
        rules = load_rules(rules_path)
        stats = write_generated(out_dir=out_dir, rules=rules, source=rules_path)
        print(
            f"[OK] 规则: {stats['rule_count']} 条 "
            f"(公式 {stats['derive_count']} / 约束 {stats['constraint_count']}), "
            f"Datalog 子句 {stats['clause_count']} 条"
        )
        print(f"     源: {stats['source']}")
        print(f"     产物: {stats['rules_copy']}")
        print(f"     产物: {stats['datalog']}")

        onto = write_ontology(out_dir=out_dir, rules=rules)
        ost = onto["stats"]
        print(
            f"[OK] 本体: 规则 {ost['rule_count']} / 类 {ost['class_count']}"
            f" / 属性 {ost['property_count']} (issue {ost['issue_count']})"
        )
        print(f"     产物: {onto['turtle']}")
        print(f"     产物: {onto['json']}")

        ontology = build_ontology(rules)
        kg = write_knowledge_graph(out_dir=out_dir, ontology=ontology, methods=methods)
        totals = kg["stats"]["totals"]
        cov = kg["coverage"]
        print(
            f"[OK] 知识图谱: 节点 {totals['nodes']} / 边 {totals['edges']}"
            f" / 参数 {cov['params_total']} (命中术语 {cov['params_with_term']})"
            f" / 方法 {cov['methods_total']}"
        )
        print(f"     产物: {kg['json']}")

    print("[DONE] 域知识导入完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
