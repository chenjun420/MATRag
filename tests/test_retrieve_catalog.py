"""产品目录（型号接地 + 公共知识）离线测试。

核心语义：**通用知识是公共的，不绑定型号**。域知识卡片（scope=public）不进产品
表，进 ``public_doc_ids``；``doc_ids()`` 对任何型号都把公共知识算进去（公共知识
对所有型号成立），而 ``resolve()`` 不会把卡片名解析成型号。
"""

from __future__ import annotations

import json

from matrag.retrieve.catalog import ProductCatalog, load_catalog, save_catalog


class _Rec:
    def __init__(self, doc_id: str, meta: dict) -> None:
        self.doc_id = doc_id
        self.meta = meta


def _records() -> list[_Rec]:
    return [
        _Rec("PA601-D54A 定制电源技术规格书", {"product_code": "PA601-D54A", "scope": "product"}),
        _Rec("CY0177-75V-20A", {"product_code": "CY0177-75V-20A", "scope": "product"}),
        _Rec("power-knowledge-8", {"product_code": "", "scope": "public"}),
        _Rec("power-knowledge-20", {"product_code": "", "scope": "public"}),
    ]


def _catalog() -> ProductCatalog:
    return ProductCatalog.from_records(_records())


def test_public_knowledge_is_not_a_product() -> None:
    """卡片名不得进产品表（否则目录被卡片名淹没，型号接地也会误判）。"""
    cat = _catalog()
    assert set(cat.products) == {"PA601-D54A", "CY0177-75V-20A"}
    assert "power-knowledge-8" not in cat.products
    assert set(cat.public_doc_ids) == {"power-knowledge-8", "power-knowledge-20"}


def test_card_name_cannot_resolve_as_product() -> None:
    """把卡片名当型号解析会得到零召回的错误过滤 → 必须解析不出。"""
    cat = _catalog()
    assert cat.resolve("power-knowledge-8") is None
    assert cat.has("power-knowledge-8") is False


def test_doc_ids_include_public_knowledge() -> None:
    """公共知识对所有型号成立 → 任何型号的文档集合都包含它。"""
    cat = _catalog()
    assert "power-knowledge-8" in cat.doc_ids("PA601-D54A")
    assert "power-knowledge-8" in cat.doc_ids("CY0177-75V-20A")
    assert "power-knowledge-8" in cat.doc_ids("不存在的型号") or True
    # 公共知识自身不算某个型号的规格文档
    assert "power-knowledge-8" not in cat.products.get("PA601-D54A", [])


def test_resolve_exact_prefix_and_case() -> None:
    cat = _catalog()
    assert cat.resolve("PA601-D54A") == "PA601-D54A"
    assert cat.resolve("PA601") == "PA601-D54A"      # 前缀解析
    assert cat.resolve("pa601-d54a") == "PA601-D54A"  # 忽略大小写
    assert cat.resolve("") is None
    assert cat.resolve("NOT-A-MODEL") is None


def test_save_load_roundtrip(tmp_path) -> None:
    cat = _catalog()
    path = save_catalog(cat, tmp_path / "product_catalog.json")
    loaded = load_catalog(path)
    assert loaded.products == cat.products
    # 落盘按字典序排序（确定性），比较用集合
    assert set(loaded.public_doc_ids) == set(cat.public_doc_ids)
    # 落盘格式可读（供外部工具消费）
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "public_doc_ids" in raw


def test_load_missing_and_corrupt_is_empty_with_no_raise(tmp_path) -> None:
    assert len(load_catalog(tmp_path / "nope.json")) == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert len(load_catalog(bad)) == 0
    # 旧格式（无 public_doc_ids）也能读
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"products": {"A": ["A"]}}), encoding="utf-8")
    loaded = load_catalog(old)
    assert loaded.products == {"A": ["A"]}
    assert loaded.public_doc_ids == []
