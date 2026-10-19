"""产品目录（型号接地）: LLM/查询侧提出型号时的唯一事实来源。

为什么需要: 词典接地只管章节名，管不住型号。实测 LLM 会把 ``CY0177`` 当型号
（真实 doc_id 是 ``CY0177-75V-20A``），也会把 **别的型号的章节**（如 PA601 的
"4.3.3 保护功能"）安到当前查询上。错误的 where 过滤 = 零召回，且"看起来正常"。

本模块只做**确定性解析**（精确 → 前缀 → 忽略大小写），不调 LLM；解析不出就
让调用方拒绝该过滤（见 ``understand``），绝不"猜一个像型号的"。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from matrag.config import settings

logger = logging.getLogger("matrag.retrieve.catalog")

CATALOG_FILENAME = "product_catalog.json"


@dataclass
class ProductCatalog:
    """型号 → 该型号下的 doc_id 列表（一个型号可对应多份文档）。"""

    products: dict[str, list[str]] = field(default_factory=dict)
    #: 公共知识文档（域知识卡片等）的 doc_id：属于**所有**型号，不属于某个型号
    public_doc_ids: list[str] = field(default_factory=list)

    # ---- 构建 ----
    @classmethod
    def from_records(cls, records: Iterable[Any]) -> ProductCatalog:
        """从 chunk 序列聚合（鸭子类型: 有 ``.doc_id`` 与 ``.meta`` 的记录）。

        **公共知识不进型号表**：``meta["scope"] == "public"``（域知识卡片）的 chunk 归入
        ``public_doc_ids``。把卡片名当型号会让目录被卡片名灌掉，也会让接地校验
        误以为"这个章节属于那个型号"——通用知识属于**所有**型号。
        """
        products: dict[str, list[str]] = {}
        public_doc_ids: list[str] = []
        for record in records:
            meta = getattr(record, "meta", None)
            doc_id = str(getattr(record, "doc_id", "") or "")
            if not isinstance(meta, dict):
                continue
            if str(meta.get("scope") or "") == "public":
                if doc_id and doc_id not in public_doc_ids:
                    public_doc_ids.append(doc_id)
                continue
            code = str(meta.get("product_code") or "").strip() or doc_id.split(" ")[0]
            if not code:
                continue
            bucket = products.setdefault(code, [])
            if doc_id and doc_id not in bucket:
                bucket.append(doc_id)
        return cls(products=products, public_doc_ids=public_doc_ids)

    # ---- 查询 ----
    def has(self, code: str) -> bool:
        return self.resolve(code) is not None

    def resolve(self, code: str) -> str | None:
        """型号解析: 精确 → 前缀(唯一命中) → 忽略大小写。解析不出返回 None。"""
        key = (code or "").strip()
        if not key:
            return None
        if key in self.products:
            return key
        prefix_hits = [k for k in self.products if k.startswith(key)]
        if len(prefix_hits) == 1:
            return prefix_hits[0]
        if len(prefix_hits) > 1:
            # 多命中: 取最短（最具体），仍歧义则不解析
            return min(prefix_hits, key=len)
        lowered = key.lower()
        for k in self.products:
            if k.lower() == lowered:
                return k
        return None

    def doc_ids(self, product_code: str) -> list[str]:
        """该型号的规格文档 + 公共知识文档（公共知识对所有型号成立）。"""
        return list(self.products.get(product_code, [])) + list(self.public_doc_ids)

    def __len__(self) -> int:
        return len(self.products)

    def to_dict(self) -> dict[str, Any]:
        return {
            "products": {k: sorted(v) for k, v in sorted(self.products.items())},
            "public_doc_ids": sorted(self.public_doc_ids),
        }


def save_catalog(catalog: ProductCatalog, path: Path) -> Path:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        json.dumps(catalog.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return file


def load_catalog(path: Path | str | None = None) -> ProductCatalog:
    """读产品目录; 文件缺失/损坏 → 空目录（warning 留痕，不抛错）。

    空目录语义: **无法验证**。调用方据此跳过型号接地（并留痕），而不是
    一律拒绝——否则没建目录的开发环境会丢掉全部 LLM 过滤条件。
    """
    file = Path(path) if path is not None else (settings.store_dir / CATALOG_FILENAME)
    if not file.is_file():
        logger.warning("产品目录不存在，跳过型号接地: %s", file)
        return ProductCatalog()
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("产品目录损坏，按空目录处理: %s error=%s: %s", file, type(exc).__name__, exc)
        return ProductCatalog()
    raw = data.get("products") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        logger.warning("产品目录缺少 products 映射，按空目录处理: %s", file)
        return ProductCatalog()
    products: dict[str, list[str]] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            products[str(key)] = [value]
        elif isinstance(value, list):
            products[str(key)] = [str(item) for item in value]
    raw_public = data.get("public_doc_ids")
    public = [str(x) for x in raw_public] if isinstance(raw_public, list) else []
    return ProductCatalog(products=products, public_doc_ids=public)
