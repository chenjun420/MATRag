"""解析装配层测试（P0-6 装配逻辑）。

用桩模块替换 matrag.parse.geometry / reconstruct / verify，使本测试与并行任务的
真实实现解耦: 只测 parse_pipeline 自己的职责——分发、占位回填、heading 栈、
LLM 未配置 fail-closed、落盘、报告汇总。
"""

from __future__ import annotations

import sys
import types as pytypes

import pytest

from matrag.errors import MatragError, ProviderNotConfiguredError
from matrag.types import (
    Block,
    BlockKind,
    Cell,
    ParseReport,
    SourceKind,
    TableDraft,
    TableModel,
)


def _merge_draft() -> TableDraft:
    """一张含纵向合并的草稿: 第 0 列 (0,0) 跨 3 行。"""

    def c(text: str, rs: int = 1, cs: int = 1) -> Cell:
        return Cell(rect=(0.0, 0.0, 1.0, 1.0), text=text, row_span=rs, col_span=cs)

    grid = [
        [c("输入欠压保护", rs=3), c("项目")],
        [None, c("欠压点")],
        [None, c("恢复点")],
    ]
    return TableDraft(draft_id="t1", page=9, table_index=0, caption="表3 保护功能",
                      n_rows=3, n_cols=2, grid=grid, reading_text="输入欠压保护")


def _flat_draft() -> TableDraft:
    def c(text: str) -> Cell:
        return Cell(rect=(0.0, 0.0, 1.0, 1.0), text=text)

    return TableDraft(draft_id="t2", page=10, table_index=1, caption="表4 输出特性",
                      n_rows=2, n_cols=2,
                      grid=[[c("项目"), c("单位")], [c("最大电流"), c("A")]])


class _FakePage:
    def __init__(self, page: int, blocks, drafts):
        self.page = page
        self.blocks = blocks
        self.table_drafts = drafts


class _FakePdfDoc:
    def __init__(self):
        self.title = "测试文档"
        self.pages = [
            _FakePage(
                9,
                [
                    Block(kind=BlockKind.HEADING, text="4 技术要求", level=2, page=9),
                    Block(kind=BlockKind.PARAGRAPH, text="保护如下表。", page=9),
                    Block(kind=BlockKind.TABLE, text="t1", page=9),
                    Block(kind=BlockKind.HEADING, text="4.3 功能/性能要求", level=3, page=9),
                    Block(kind=BlockKind.TABLE, text="t2", page=9),
                ],
                [_merge_draft(), _flat_draft()],
            )
        ]


class _GeometryStub(pytypes.ModuleType):
    def extract_pdf(self, path):
        return _FakePdfDoc()

    def extract_markdown(self, path):
        return _FakePdfDoc()


class _ReconstructStub(pytypes.ModuleType):
    def reconstruct_table(self, draft, *, llm=None, source_kind=None):
        first_row = [c.text if c else "" for c in draft.grid[0]]
        rows = [[c.text if c else "<merged>" for c in row] for row in draft.grid[1:]]
        return TableModel(
            id=draft.draft_id,
            caption=draft.caption,
            headers=first_row,
            rows=rows,
            marks=["stub"],
            confidence=0.99,
            source={"page": draft.page},
        )


class _VerifyStub(pytypes.ModuleType):
    def build_parse_report(self, doc_id, source_path, provider, models):
        from matrag.types import TableReport

        report = ParseReport(doc_id=doc_id, source_path=source_path, provider=provider)
        for model, draft in models:
            flags = [f"ERROR_{m.upper()}" for m in model.marks if m.startswith("ERROR")]
            report.tables.append(
                TableReport(
                    table_id=model.id,
                    page=draft.page,
                    n_rows=len(model.rows),
                    n_cols=len(model.headers),
                    merged_cells=draft.merged_cells,
                    flags=flags,
                )
            )
        report.stats["n_tables"] = len(models)
        return report


class _FakeLLM:
    """仅用于绕开 llm.configured 检查（不调真实端点）。"""

    def __init__(self, configured: bool = True) -> None:
        self.configured = configured


@pytest.fixture()
def pipeline(monkeypatch: pytest.MonkeyPatch):
    """安装桩模块后再导入 parse_pipeline（此时 import 才能成功）。

    当 ``matrag.cli`` 先被 collect（看 :mod:`test_cli.py`），它会触发
    ``matrag.parse_pipeline`` 的早期导入，把 ``from matrag.parse import geometry``
    这种局部绑定锁死到 *真实* 模块（pymupdf 真文档解析，对测试 fixture 创建的 ``%PDF-1.4 stub``
    文件抛 ``FileDataError``)。这里的隔离策略：

    1. ``sys.modules`` 注入 stub → 旧的 matrag.parse.{geometry,reconstruct,verify} 引用被替换；
    2. **重新 import matrag.parse_pipeline**, 触发再次 import matrag.parse, 这次 sys.modules
       返回的就是 stub, 函数体内的 local ``geometry`` / ``reconstruct`` / ``verify``
       绑定到 stub, 通过 ``parse_pipeline.geometry`` 调用走 stub 而不是 pymupdf 真文档解析。
    """
    # 注入 stub (必须在 re-import 之前, 否则 stub 不会占位)
    monkeypatch.setitem(sys.modules, "matrag.parse.geometry", _GeometryStub("matrag.parse.geometry"))
    monkeypatch.setitem(
        sys.modules, "matrag.parse.reconstruct", _ReconstructStub("matrag.parse.reconstruct")
    )
    monkeypatch.setitem(sys.modules, "matrag.parse.verify", _VerifyStub("matrag.parse.verify"))

    # 强制重新导入 parse_pipeline: 卸载 + import
    for mod_name in ("matrag.parse_pipeline", "matrag.parse"):
        sys.modules.pop(mod_name, None)
    import matrag.parse  # 让 stub 重新被引用到包属性
    import matrag.parse_pipeline  # 触发函数体里的 from ... import 重新解析, 这次走 stub
    monkeypatch.setattr(matrag.parse, "geometry", sys.modules["matrag.parse.geometry"], raising=False)
    monkeypatch.setattr(matrag.parse, "reconstruct", sys.modules["matrag.parse.reconstruct"], raising=False)
    monkeypatch.setattr(matrag.parse, "verify", sys.modules["matrag.parse.verify"], raising=False)
    return matrag.parse_pipeline


@pytest.fixture()
def env(tmp_path, pipeline, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pipeline.settings, "corpus_dir", tmp_path / "corpus")
    monkeypatch.setattr(pipeline.settings, "reports_dir", tmp_path / "reports")
    monkeypatch.setattr(pipeline.settings, "parser_provider", "local")
    src = tmp_path / "sample.pdf"
    src.write_bytes(b"%PDF-1.4 stub")
    return src


def test_assembles_blocks_and_tables(env, pipeline):
    parsed = pipeline.parse_document(env, llm=_FakeLLM())

    assert parsed.ok
    doc = parsed.doc
    assert doc.source_kind == SourceKind.PDF
    assert doc.title == "测试文档"
    tables = doc.tables()
    assert [t.id for t in tables] == ["t1", "t2"]
    # heading 栈: t1 继承 "4 技术要求"；t2 继承两级
    assert tables[0].source["section_path"] == "4 技术要求"
    assert tables[1].source["section_path"] == "4 技术要求 > 4.3 功能/性能要求"
    # 桩重建按 grid 生成
    assert tables[0].headers == ["输入欠压保护", "项目"]
    assert tables[0].rows == [["<merged>", "欠压点"], ["<merged>", "恢复点"]]
    md = doc.markdown()
    assert "| 输入欠压保护 | 项目 |" in md
    assert "<!-- page:9" in md


def test_save_writes_three_files(env, pipeline, tmp_path):
    parsed = pipeline.parse_document(env, llm=_FakeLLM())
    out = parsed.save()
    assert out["md"].exists() and out["tables"].exists() and out["report"].exists()
    assert "输入欠压保护" in out["md"].read_text(encoding="utf-8")


def test_llm_not_configured_with_merged_tables_is_gate_failure(env, pipeline):
    parsed = pipeline.parse_document(env, llm=_FakeLLM(configured=False))
    gate_model = next(t for t in parsed.doc.tables() if t.id == "t1")
    assert gate_model.marks == ["ERROR_LLM_NOT_CONFIGURED"]
    assert gate_model.headers == []  # fail-closed: 空表，不可被引用
    assert not parsed.ok


def test_unsupported_extension_and_missing_file(tmp_path, pipeline):
    bad = tmp_path / "x.docx"
    bad.write_bytes(b"x")
    with pytest.raises(MatragError):
        pipeline.parse_document(bad)
    with pytest.raises(FileNotFoundError):
        pipeline.parse_document(tmp_path / "nope.pdf")


def test_non_local_provider_refused(env, pipeline, monkeypatch):
    monkeypatch.setattr(pipeline.settings, "parser_provider", "docling-pro")
    with pytest.raises(ProviderNotConfiguredError):
        pipeline.parse_document(env)
