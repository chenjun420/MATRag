"""评测集与评测脚本测试（P1-7 ``scripts/mine_eval_qa.py`` / ``scripts/run_eval.py``，全离线）。

覆盖三块:

1. **评测集 schema**（``data/eval/qa.jsonl``）: 必填字段齐全、tier 枚举、qid 唯一、
   题量 ≥30、覆盖 3 份文档与 ≥4 个 tier、``must_contain`` 非空、``expected_doc_id``
   落在真实语料的 doc_id 集合里。
2. **grounding**（"禁止编造"）: 每个 ``must_contain`` 词条与 ``expected_sections``
   都必须能在它自己声明的来源表/卡片原文里找到——直接对着语料文件校验，
   这样题目文件被手改过也会立刻红。
3. **指标计算**: 用假检索器跑 ``score_item`` / ``aggregate`` / ``run_dataset``，
   对已知排序手算断言 hit@k / MRR / section_hit / must_contain。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from matrag.config import settings
from scripts import mine_eval_qa as mine
from scripts import run_eval as ev

QA_PATH = Path(settings.eval_dir) / ev.DEFAULT_QA_NAME
CORPUS_DIR = Path(settings.corpus_dir)
CARDS_PATH = Path(settings.domain_dir) / "generated" / "knowledge_cards.jsonl"

MIN_ITEMS = 30
MIN_TIERS = 4
MIN_DOCS = 3


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def dataset() -> list[dict[str, Any]]:
    if not QA_PATH.is_file():
        pytest.fail(f"评测集缺失: {QA_PATH}（跑 python -m scripts.mine_eval_qa 生成）")
    return [
        json.loads(line)
        for line in QA_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture(scope="module")
def corpus_texts() -> dict[str, str]:
    """``{doc_id: 全文}``（语料渲染文本 + 章节路径），用于 grounding 校验。

    章节路径也拼进来: ``expected_sections`` 的来源是表/块的 ``section_path``，
    而不是表格正文。
    """
    tables = mine.load_corpus_tables(CORPUS_DIR)
    texts: dict[str, list[str]] = {}
    for table in tables:
        parts = [table.rendered_text()]
        parts.extend(seg.strip() for seg in table.section_path.split(">"))
        texts.setdefault(table.doc_id, []).append("\n".join(parts))
    for card in mine.load_knowledge_cards(CARDS_PATH).values():
        texts.setdefault(card.card_id, []).append(card.rendered_text)
    return {doc_id: "\n".join(chunks) for doc_id, chunks in texts.items()}


@pytest.fixture(scope="module")
def corpus_doc_ids() -> set[str]:
    if not CORPUS_DIR.is_dir():
        pytest.skip(f"语料目录不存在: {CORPUS_DIR}（评测集数据源缺失，无法校验 doc_id）")
    return {p.name[: -len(".tables.json")] for p in CORPUS_DIR.glob("*.tables.json")}


# --------------------------------------------------------------------------- 1. schema


def test_dataset_schema(dataset: list[dict[str, Any]]) -> None:
    required = set(ev.REQUIRED_FIELDS)
    for record in dataset:
        missing = required - set(record)
        assert not missing, f"{record.get('qid')} 缺字段 {sorted(missing)}"
        assert isinstance(record["query"], str) and record["query"].strip()
        assert isinstance(record["expected_doc_id"], str) and record["expected_doc_id"]
        assert isinstance(record["expected_sections"], list)
        assert all(isinstance(s, str) and s for s in record["expected_sections"])
        assert isinstance(record["must_contain"], list)
        assert isinstance(record["answer_hint"], str)
        assert record["tier"] in ev.TIERS, f"{record['qid']} tier 非法: {record['tier']}"


def test_dataset_size_and_coverage(dataset: list[dict[str, Any]]) -> None:
    assert len(dataset) >= MIN_ITEMS, f"题目数 {len(dataset)} < {MIN_ITEMS}"
    qids = [r["qid"] for r in dataset]
    assert len(set(qids)) == len(qids), "qid 有重复"
    tiers = {r["tier"] for r in dataset}
    assert len(tiers) >= MIN_TIERS, f"tier 覆盖不足: {sorted(tiers)}"
    corpus_docs = {
        r["expected_doc_id"] for r in dataset if not r["expected_doc_id"].startswith("power-knowledge-")
    }
    assert len(corpus_docs) >= MIN_DOCS, f"规格书覆盖不足: {sorted(corpus_docs)}"


def test_must_contain_non_empty(dataset: list[dict[str, Any]]) -> None:
    for record in dataset:
        terms = record["must_contain"]
        assert isinstance(terms, list) and terms, f"{record['qid']} must_contain 为空"
        assert all(isinstance(t, str) and t.strip() for t in terms)


def test_queries_look_like_questions(dataset: list[dict[str, Any]]) -> None:
    """问句必须像人问的：不能是「检索 xxx」这类检索指令，也不能退化成纯表头拼接。"""
    for record in dataset:
        query = record["query"]
        assert not query.startswith("检索"), record["qid"]
        assert "检索" not in query, f"{record['qid']} 问句里出现检索指令: {query}"
        assert len(query) >= 8, f"{record['qid']} 问句过短: {query}"


def test_expected_doc_id_in_corpus(dataset: list[dict[str, Any]], corpus_doc_ids: set[str]) -> None:
    card_ids = set(mine.load_knowledge_cards(CARDS_PATH))
    for record in dataset:
        doc_id = record["expected_doc_id"]
        assert doc_id in corpus_doc_ids or doc_id in card_ids, (
            f"{record['qid']} expected_doc_id {doc_id!r} 不在语料/卡片集合里"
        )


# --------------------------------------------------------------------------- 2. grounding


def test_must_contain_grounded_in_source(dataset: list[dict[str, Any]], corpus_texts: dict[str, str]) -> None:
    """每个 must_contain 词条都能在它声明的来源文档里找到（防"编造"）。"""
    for record in dataset:
        doc_id = record["expected_doc_id"]
        if doc_id not in corpus_texts:
            pytest.fail(f"{record['qid']} 的来源 {doc_id!r} 不在语料渲染文本里")
        mine._assert_grounded(
            record["must_contain"], corpus_texts[doc_id], where=f"qa:{record['qid']}"
        )


def test_expected_sections_grounded_in_source(
    dataset: list[dict[str, Any]], corpus_texts: dict[str, str]
) -> None:
    """expected_sections 必须真实出现在来源文档里（cross_doc 跨两份文档校验）。"""
    for record in dataset:
        if not record["expected_sections"]:
            continue
        doc_ids = [record["expected_doc_id"]]
        related = record.get("source", {}).get("related_doc_id")
        if related:
            doc_ids.append(related)
        haystack = "\n".join(corpus_texts.get(doc_id, f"<missing:{doc_id}>") for doc_id in doc_ids)
        mine._assert_grounded(record["expected_sections"], haystack, where=f"qa:{record['qid']}")


def test_source_anchor_matches_declared_document(dataset: list[dict[str, Any]]) -> None:
    """``source`` 溯源字段必须自洽: 声明的 doc_id 与表格/卡片 id 真实存在。"""
    tables = mine.load_corpus_tables(CORPUS_DIR)
    table_index = {(t.doc_id, t.table_id): t for t in tables}
    cards = mine.load_knowledge_cards(CARDS_PATH)
    for record in dataset:
        source = record.get("source")
        assert isinstance(source, dict), f"{record['qid']} 缺 source 溯源字段"
        if "card_id" in source:
            assert source["card_id"] in cards, record["qid"]
            continue
        key = (source.get("doc_id"), source.get("table_id"))
        assert key in table_index, f"{record['qid']} source 指向不存在的表 {key}"
        table = table_index[key]
        assert table.doc_id == record["expected_doc_id"], record["qid"]


def test_cross_doc_items_declare_two_documents(dataset: list[dict[str, Any]]) -> None:
    cross = [r for r in dataset if r["tier"] == "cross_doc"]
    assert cross, "cross_doc 题为空"
    for record in cross:
        assert record["source"].get("related_doc_id"), f"{record['qid']} 未声明关联文档"


# --------------------------------------------------------------------------- 3. 指标


@dataclass
class _FakeHit:
    """鸭子类型检索命中替身（字段与 RetrievalHit 同形）。"""

    chunk_id: str
    doc_id: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)
    rrf_score: float = 0.0
    per_leg: dict[str, float | None] = field(default_factory=dict)
    legs_hit: tuple[str, ...] = ()


def _hit(chunk_id: str, doc_id: str, text: str, *, section: str = "", page: int = 1) -> _FakeHit:
    titles = [t.strip() for t in section.split(">") if t.strip()]
    return _FakeHit(
        chunk_id=chunk_id,
        doc_id=doc_id,
        text=text,
        meta={"page": page, "section_path": section, "section_titles": titles},
        rrf_score=1.0 / len(chunk_id),
        legs_hit=("vector", "keyword"),
    )


def _item(
    *,
    qid: str = "q1",
    expected_doc_id: str = "DOC-A",
    expected_sections: tuple[str, ...] = ("4.3.1 输入特性",),
    must_contain: tuple[str, ...] = ("输入电压",),
    tier: str = "spec",
) -> ev.EvalItem:
    return ev.EvalItem(
        qid=qid,
        query="DOC-A 的输入电压是多少",
        expected_doc_id=expected_doc_id,
        expected_sections=list(expected_sections),
        must_contain=list(must_contain),
        answer_hint="",
        tier=tier,
    )


def test_score_item_hit_and_mrr_known_ordering() -> None:
    """构造已知排序，断言 hit@k / MRR / 首次名次逐个对得上。"""
    hits = [
        _hit("c1", "DOC-B", "无关文档"),
        _hit("c2", "DOC-C", "无关文档"),
        _hit("c3", "DOC-A", "输入电压 0~320Vac", section="4.3.4.3 遥测功能"),
    ]
    result = ev.score_item(_item(), hits)
    assert result["first_rank"] == 3
    assert result["reciprocal_rank"] == pytest.approx(1 / 3)
    assert result["hit"] == {1: False, 3: True, 8: True}
    # section_hit：DOC-A 那条在 4.3.4.3，不在期望的 4.3.1
    assert result["section_hit"][8] is False


def test_score_item_hit_at_1_and_section_and_must_contain() -> None:
    hits = [
        _hit("c1", "DOC-A", "输入电压 0~320Vac", section="PA601 B > 4 技术要求 > 4.3.1 输入特性"),
        _hit("c2", "DOC-B", "输出电压", section="4.3.2 输出特性"),
        _hit("c3", "DOC-C", "输出电流", section="4.3.2 输出特性"),
        _hit("c4", "DOC-D", "杂项", section="9"),
    ]
    result = ev.score_item(_item(), hits)
    assert result["first_rank"] == 1
    assert result["reciprocal_rank"] == pytest.approx(1.0)
    assert result["hit"][1] is True and result["hit"][3] is True and result["hit"][8] is True
    assert result["section_hit"][1] is True
    assert result["must_contain_hit"] is True
    assert result["must_contain_matched"] == ["输入电压"]
    assert result["must_contain_rank"] == 1


def test_score_item_miss_is_zero_not_dropped() -> None:
    hits = [_hit(f"c{i}", "DOC-X", "无关") for i in range(8)]
    result = ev.score_item(_item(expected_doc_id="DOC-A"), hits)
    assert result["first_rank"] is None
    assert result["reciprocal_rank"] == 0.0
    assert all(value is False for value in result["hit"].values())


def test_section_hit_requires_matching_title() -> None:
    hits = [_hit("c1", "DOC-A", "输入电压", section="4.3.2 输出特性")]
    result = ev.score_item(_item(expected_sections=("4.3.1 输入特性",)), hits)
    assert result["section_hit"][1] is False
    result_ok = ev.score_item(_item(expected_sections=("输出特性",)), hits)
    assert result_ok["section_hit"][1] is True


def test_must_contain_uses_normalized_text() -> None:
    """命中判定用统一归一口径: 语料里的 ``<br>`` / 空格差异不该判成未命中。"""
    hits = [_hit("c1", "DOC-A", "表11 输出特性<br>输入 电压 0~320 Vac")]
    result = ev.score_item(_item(must_contain=("输入电压",)), hits)
    assert result["must_contain_hit"] is True
    assert result["must_contain_rank"] == 1


def test_must_contain_only_top3() -> None:
    hits = [
        _hit("c1", "DOC-B", "无关"),
        _hit("c2", "DOC-B", "无关"),
        _hit("c3", "DOC-B", "无关"),
        _hit("c4", "DOC-A", "输入电压"),
    ]
    result = ev.score_item(_item(), hits)
    assert result["must_contain_hit"] is False
    assert result["hit"][8] is True


def test_must_contain_empty_is_not_applicable() -> None:
    hits = [_hit("c1", "DOC-A", "输入电压", section="4.3.1 输入特性")]
    result = ev.score_item(_item(must_contain=()), hits)
    assert result["must_contain_hit"] is None


# --------------------------------------------------------------------------- 4. run_dataset / aggregate


class _FakeRetriever:
    """假检索器: 每次 ``retrieve`` 返回固定排序，并记录收到的 query/where。"""

    def __init__(self, order: list[_FakeHit]) -> None:
        self.order = order
        self.calls: list[tuple[str, int, dict[str, Any] | None]] = []
        self.plans: list[str] = []

    def retrieve(self, query: str, *, top_k: int = 10, where: dict[str, Any] | None = None) -> list[_FakeHit]:
        self.calls.append((query, top_k, where))
        return self.order[:top_k]

    def plan(self, query: str) -> dict[str, Any]:
        return {
            "routed": SimpleNamespace(
                kind=SimpleNamespace(value="spec_value"),
                legs=("vector", "keyword"),
                confidence=0.7,
                reason="命中关键词",
            ),
            "active_legs": ("vector", "keyword"),
            "degradations": [],
        }

    def explain(self, hits: list[_FakeHit]) -> dict[str, Any]:
        return {h.chunk_id: {"ranks": {"vector": i}} for i, h in enumerate(hits, start=1)}


def test_run_dataset_and_aggregate() -> None:
    order = [
        _hit("c1", "DOC-B", "别的文档", section="9 通信"),
        _hit("c2", "DOC-A", "输入电压 0~320Vac", section="4.3.1 输入特性"),
        _hit("c3", "DOC-A", "输出电压", section="4.3.1 输入特性"),
    ]
    retriever = _FakeRetriever(order)
    items = [
        _item(qid="q1"),
        _item(qid="q2", expected_doc_id="DOC-A", expected_sections=("4.3.1 输入特性",)),
        _item(qid="q3", expected_doc_id="DOC-ZZZ"),
    ]
    results = ev.run_dataset(items, retriever, top_k=8)
    assert len(results) == 3
    assert all(r.error is None for r in results)
    assert results[0].first_rank == 2
    assert results[0].reciprocal_rank == pytest.approx(0.5)
    assert results[1].first_rank == 2
    assert results[2].first_rank is None

    summary = ev.aggregate(results)
    assert summary["n"] == 3 and summary["scored"] == 3 and summary["errors"] == 0
    overall = summary["overall"]
    assert overall["hit"]["1"] == pytest.approx(0.0)
    assert overall["hit"]["3"] == pytest.approx(2 / 3)
    assert overall["hit"]["8"] == pytest.approx(2 / 3)
    assert overall["mrr"] == pytest.approx((0.5 + 0.5 + 0.0) / 3)
    # q3 的期望文档没命中，但 must_contain 词条仍出现在 top-3 文本里 → 该项照记命中
    assert overall["must_contain"] == pytest.approx(1.0)
    assert "spec" in summary["by_tier"]
    assert retriever.calls[0][1] == 8


def test_run_dataset_records_error_per_item() -> None:
    class _Boom(_FakeRetriever):
        def retrieve(self, query: str, *, top_k: int = 10, where: Any = None) -> list[_FakeHit]:
            raise RuntimeError("索引炸了")

    retriever = _Boom([])
    results = ev.run_dataset([_item(qid="q1"), _item(qid="q2")], retriever, top_k=8)
    assert [r.error for r in results] == ["RuntimeError: 索引炸了"] * 2
    summary = ev.aggregate(results)
    assert summary["errors"] == 2 and summary["scored"] == 0
    assert summary["error_qids"] == ["q1", "q2"]


def test_run_dataset_uses_understand_plan_filters() -> None:
    """understand 计划里的 filters 必须真的进 where，并落到产物里可追溯。"""

    @dataclass
    class _Plan:
        filters: dict[str, Any]
        retrieval_queries: list[str]
        sub_queries: list[str]
        needs_rules: bool
        degradations: list[str]

    def _understand(query: str, *, llm: Any = None) -> _Plan:
        return _Plan(
            filters={"product_code": "DOC-A", "section": "4.3.1"},
            retrieval_queries=[query],
            sub_queries=[],
            needs_rules=False,
            degradations=[],
        )

    retriever = _FakeRetriever([_hit("c1", "DOC-A", "输入电压", section="4.3.1 输入特性")])
    results = ev.run_dataset(
        [_item(qid="q1")],
        retriever,
        top_k=8,
        use_understand=True,
        understand_fn=_understand,
    )
    assert results[0].error is None
    assert results[0].where == {"product_code": "DOC-A", "section_contains": "4.3.1"}
    assert retriever.calls[0][2] == {"product_code": "DOC-A", "section_contains": "4.3.1"}
    assert results[0].plan["available"] is True
    assert results[0].plan["retrieve_mode"] == "retrieve"
    assert results[0].route["kind"] == "spec_value"


def test_retrieve_with_plan_is_used_when_available() -> None:
    class _PlanRetriever(_FakeRetriever):
        def retrieve_with_plan(
            self, query: str, *, plan: Any, top_k: int = 10, where: Any = None
        ) -> list[_FakeHit]:
            self.plans.append(type(plan).__name__)
            return self.order[:top_k]

    retriever = _PlanRetriever([_hit("c1", "DOC-A", "输入电压", section="4.3.1 输入特性")])
    results = ev.run_dataset(
        [_item(qid="q1")],
        retriever,
        top_k=8,
        use_understand=True,
        understand_fn=lambda query, *, llm=None: SimpleNamespace(filters={}, retrieval_queries=[]),
    )
    assert results[0].error is None
    assert results[0].plan["retrieve_mode"] == "retrieve_with_plan"
    assert retriever.plans == ["SimpleNamespace"]


def test_load_dataset_rejects_broken_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"qid": "x", "query": "y"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="缺少必填字段"):
        ev.load_dataset(bad)

    dup = tmp_path / "dup.jsonl"
    record = {
        "qid": "x",
        "query": "y",
        "expected_doc_id": "d",
        "expected_sections": ["s"],
        "must_contain": ["m"],
        "answer_hint": "",
        "tier": "spec",
    }
    dup.write_text(json.dumps(record, ensure_ascii=False) + "\n" + json.dumps(record) + "\n",
                   encoding="utf-8")
    with pytest.raises(ValueError, match="qid 重复"):
        ev.load_dataset(dup)


def test_load_dataset_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ev.EvalSetupError):
        ev.load_dataset(tmp_path / "nope.jsonl")


# --------------------------------------------------------------------------- 5. 挖掘脚本


def test_normalize_and_display() -> None:
    assert mine.normalize(" 输入 电压<br>0~320 ") == "输入电压0~320"
    assert mine.normalize("（A）") == "(A)"  # NFKC 全角→半角
    assert mine._display("过冲幅度<br>恢复时间") == "过冲幅度恢复时间"
    assert mine._display_keeping_breaks("29.5<br>31.5") == "29.5/31.5"


def test_alignment_issue_detects_shifted_header() -> None:
    """单位列里躺纯数字 = 表头/数据错位 → 整表跳过出题（否则答案必错）。"""
    ok = mine.CorpusTable(
        doc_id="D",
        doc_title="D",
        raw={
            "id": "t0",
            "caption": "表0",
            "headers": ["编号", "项目", "单位", "最小值"],
            "rows": [["A-1", "工作温度", "℃", "-40"]],
            "source": {"page": 1, "section_path": "1 概述"},
        },
    )
    assert mine.alignment_issue(ok) is None
    bad = mine.CorpusTable(
        doc_id="D",
        doc_title="D",
        raw={
            "id": "t1",
            "caption": "表1",
            "headers": ["编号", "项目", "项目", "单位", "最小值"],
            "rows": [["A-1", "标称输入电压范围", "Vac", "100", "110"]],
            "source": {"page": 1, "section_path": "1 概述"},
        },
    )
    assert mine.alignment_issue(bad) is not None
    assert mine._questions_from_table(bad, max_per_table=3) == []


def test_assert_grounded_rejects_invented_term() -> None:
    mine._assert_grounded(["输入电压"], "表1 输入规格 输入电压 85Vac", where="t")
    with pytest.raises(ValueError, match="在来源文本中不存在"):
        mine._assert_grounded(["输出纹波"], "表1 输入规格 输入电压 85Vac", where="t")


def test_build_dataset_is_deterministic() -> None:
    tables = mine.load_corpus_tables(CORPUS_DIR)
    cards = mine.load_knowledge_cards(CARDS_PATH)
    first = [i.qid for i in mine.build_dataset(tables, cards, min_per_doc=4)]
    second = [i.qid for i in mine.build_dataset(tables, cards, min_per_doc=4)]
    assert first == second
    assert len(set(first)) == len(first)


def test_where_from_plan_translates_and_drops() -> None:
    where, notes = ev._where_from_plan(
        SimpleNamespace(filters={"product_code": "X", "section": "4.3.1", "table": 11}),
        query="q",
    )
    assert where is not None
    assert where["product_code"] == "X"
    assert where["section_contains"] == "4.3.1"
    assert any(note.get("kind") == "where_key_dropped" for note in notes)

    assert ev._where_from_plan(None, query="q") == (None, [])
    assert ev._where_from_plan(SimpleNamespace(filters={}), query="q") == (None, [])
