"""建索引脚本（P1-4 集成）: data/corpus + 域知识卡片 → 切分 → embedding → 双腿索引。

用法:
    python -m scripts.build_index                 # 全量重建（默认清库重灌）
    python -m scripts.build_index --doc CY0177-75V-20A
    python -m scripts.build_index --keep-existing  # 增量（同 chunk_id upsert）

纪律:
- 索引期与查询期 embedding 模型必须一致（dimension 记录进 store stats，模型名随向量入索引）。
- 域知识卡片（power_knowledge/power_rules）与产测方法卡片（power_test_methods）同属
  公共知识, 与产品规格书走同一条管线，kind 区分。
- 全量重建时先清两个腿（关键词腿 delete-by-doc_id；向量腿因无按 doc 删的 API,
  直接删 db 文件重建——语料小, 全量重建 ~分钟级）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from matrag.config import settings
from matrag.domain.cards import load_knowledge_cards
from matrag.domain.test_methods import build_method_cards
from matrag.embeddings import CachedEmbeddingClient, MaaSEmbeddingClient
from matrag.index.chunking import ChunkRecord, chunk_document
from matrag.index.keyword import KeywordIndex
from matrag.index.store import VectorIndex
from matrag.parse_pipeline import load_canonical
from matrag.retrieve.catalog import ProductCatalog, save_catalog
from matrag.retrieve.lexicon import build_lexicon, save_lexicon
from matrag.retrieve.query import LEXICON_FILENAME
from matrag.types import Block, BlockKind, CanonicalDocument, SourceKind


def _domain_card_doc(card) -> CanonicalDocument:
    """域知识卡片 → 伪 CanonicalDocument（kind=domain），走同一 chunk 管线。"""
    blocks = [
        Block(kind=BlockKind.HEADING, text=card.title, level=1,
              section_path=card.section_path, page=0),
        Block(kind=BlockKind.PARAGRAPH, text=card.body,
              section_path=card.section_path, page=0),
    ]
    return CanonicalDocument(
        doc_id=card.card_id,
        title=card.title,
        source_path=str(getattr(card, "source", "") or ""),
        source_kind=SourceKind.DOMAIN,
        meta={"domain": card.domain, "kind": "domain_card"},
        blocks=blocks,
    )


def collect_records(doc_ids: list[str] | None, *, include_domain: bool) -> list[ChunkRecord]:
    records: list[ChunkRecord] = []
    corpus = settings.corpus_dir
    doc_ids: list[str] = []
    for p in sorted(corpus.glob("*.doc.json")):
        doc_ids.append(p.name[: -len(".doc.json")])
    for p in sorted(corpus.glob("*.md")):
        doc_id = p.name[: -len(".md")]
        if doc_id not in doc_ids:
            doc_ids.append(doc_id)
    if doc_ids:
        wanted = set(doc_ids)
        doc_ids = [d for d in doc_ids if d in wanted]
    for doc_id in doc_ids:
        doc = load_canonical(doc_id, corpus_dir=corpus)
        recs = chunk_document(doc)
        records.extend(recs)
        print(f"  [corpus] {doc_id}: {len(recs)} chunks")
    if include_domain:
        cards = [*load_knowledge_cards(), *build_method_cards()]
        for card in cards:
            recs = chunk_document(_domain_card_doc(card))
            records.extend(recs)
        print(f"  [domain] knowledge+method cards ({len(cards)})")
        print(f"  [domain] -> {len(records)} 累计")
    return records


def build(records: list[ChunkRecord], *, db_path: Path, reset: bool) -> dict:
    embedder = CachedEmbeddingClient(
        MaaSEmbeddingClient(), db_path=str(db_path) + ".embcache"
    )
    dim = embedder.dim  # 探测（fail-closed: 探测失败抛 RuntimeError）
    print(f"embedder: model={embedder.model} dim={dim} batch={settings.embed_batch_size}")

    if reset and db_path.exists():
        db_path.unlink()
    vec = VectorIndex(db_path, dimension=dim, embed_model=embedder.model)
    kw = KeywordIndex(db_path)

    t0 = time.time()
    texts = [r.text for r in records]
    vectors: list[list[float]] = []
    bs = max(1, settings.embed_batch_size)
    for i in range(0, len(texts), bs):
        batch = texts[i : i + bs]
        vectors.extend(embedder.embed_batch(batch))
        done = min(i + bs, len(texts))
        print(f"  embedded {done}/{len(texts)}  ({time.time() - t0:.0f}s)", flush=True)

    vec.add_chunks(records, vectors=vectors)
    kw.add_chunks(records)
    catalog = ProductCatalog.from_records(records)
    save_catalog(catalog, db_path.parent / "product_catalog.json")
    print(f"  catalog: products={len(catalog)} -> {db_path.parent / 'product_catalog.json'}")
    # 章节词典（按章节名过滤检索的数据源）: 与索引同目录落盘, 查询期只读
    lexicon = build_lexicon(records)
    lexicon_file = settings.store_dir / LEXICON_FILENAME
    save_lexicon(lexicon, lexicon_file)
    print(f"lexicon: titles={len(lexicon.titles)} -> {lexicon_file}", flush=True)
    stats = {
        **vec.stats(),
        "keyword_count": kw.count(),
        "lexicon_titles": len(lexicon.titles),
        "lexicon_path": str(lexicon_file),
        "elapsed_s": round(time.time() - t0, 1),
    }
    vec.close()
    kw.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="build_index")
    ap.add_argument("--doc", action="append", default=None, help="只建指定文档（可多次）")
    ap.add_argument("--no-domain", action="store_true", help="不含域知识卡片")
    ap.add_argument("--keep-existing", action="store_true", help="增量（不删库）")
    ap.add_argument("--db", default=None, help="索引库路径（默认 data/store/index.db）")
    args = ap.parse_args(argv)

    settings.ensure_dirs()
    db_path = Path(args.db) if args.db else settings.store_dir / "index.db"
    reset = not args.keep_existing

    records = collect_records(args.doc, include_domain=not args.no_domain)
    if not records:
        print("[FAIL] 无 chunk 可索引（先跑 parse / 确认 corpus 存在）")
        return 2

    print(f"collect: {len(records)} chunks -> {db_path} (reset={reset})")
    try:
        stats = build(records, db_path=db_path, reset=reset)
    except Exception as exc:  # noqa: BLE001 —— 建索引失败要显式，不带病交付
        print(f"[FAIL] build_index: {type(exc).__name__}: {exc}")
        return 2
    print(f"[OK] {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
