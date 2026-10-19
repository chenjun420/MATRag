"""跨"检索→应答"的审计轨: 把一次应答的完整决策链写成可校验的审计记录。

**为什么存在**: ``settings.provenance_backend``（``sqlite | memory``）此前在全仓
零引用——声明了却没有任何代码读它，是骗人的死配置。本模块把它真正接上线：审计
记录的存储后端由 settings 决定，每次应答的决策链（问题 → 查询理解 → 检索命中 →
行级引用 → 规则 → 降级留痕 → 模型/耗时）落成一条 :class:`ProvenanceRecord`，
可回查、可对账。

**分层**（同一 :class:`AuditTrail` 上记两种记录，``kind`` 区分，不混列）:

- ``retrieval``: 检索层决策（查询理解 + 命中锚点 + 降级）——
  :meth:`AuditTrail.record_retrieval`;
- ``answer``: 应答层决策（检索之上再加行级引用/规则/模型/耗时）——
  :meth:`AuditTrail.record_answer`。

**存储映射**（semantica PROV-O 口径，``ProvenanceManager.track_entity`` 写入）::

    entity_id       = answer_id                # 审计记录 id（可显式指定）
    entity_type     = "matrag_answer" / "matrag_retrieval"
    activity_id     = "record_answer" / "record_retrieval"
    agent_id        = model（无模型时 "matrag"）
    source_document / used_entities = 命中锚点 "doc_id#chunk_id"
    source_quote    = query                    # 便于人读（原文不入库，见下）
    metadata        = ProvenanceRecord.to_dict()   # 完整决策链

命中只存**锚点**（``doc_id``/``chunk_id``/page/section/table_id/score，每条一个
semantica :class:`~semantica.provenance.SourceReference`）而不复制原文：正文可按
``chunk_id`` 回语料取，审计库不背取样体积。

**两层校验**（:meth:`AuditTrail.verify` 两层都过才返回 ``True``）:

1. entry 级: ``verify_checksum(entry)``——覆盖 PROV-O 框架字段与哈希链，改
   ``source_document``/``used_entities``/``agent``/``timestamp`` 即被发现;
2. 内容级: :func:`compute_record_checksum`——semantica 的 ``compute_checksum``
   **明确不覆盖 metadata**（其 docstring: 只对 PROV-O 框架字段取哈希），而完整
   决策链恰恰在 metadata 里。因此把整条记录的规范化 JSON 投影到
   ``compute_checksum`` 覆盖的字段上再取哈希，结果回写
   ``metadata["checksum"]``，读回时重算比对——改一个字即 ``False``。

**失败约定（不得静默降级吞错）**:

- 未知 backend 取值 → ``ValueError``（不许静默回落到 memory）;
- 后端不可用 / 路径不可写 / 写入未被后端确认 → :class:`ProvenanceError`
  （保留原始异常链）;
- 载荷不可 JSON 序列化或结构非法 → ``TypeError`` / ``ValueError``。

调用方若持"审计不可用不阻断应答"的策略，必须**显式**捕获
:class:`ProvenanceError` 并记入自己的 ``degradations``——本模块不替调用方做这个
决定，不出"无审计假象"。
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from semantica.provenance import (
    InMemoryStorage,
    ProvenanceEntry,
    ProvenanceManager,
    ProvenanceStorage,
    SourceReference,
    compute_checksum,
    verify_checksum,
)
from semantica.provenance.storage import SQLiteStorage

from matrag.config import settings
from matrag.errors import MatragError

#: 支持的存储后端（``settings.provenance_backend`` 的合法取值）
BACKEND_SQLITE = "sqlite"
BACKEND_MEMORY = "memory"
SUPPORTED_BACKENDS: tuple[str, ...] = (BACKEND_SQLITE, BACKEND_MEMORY)
#: sqlite 后端的库文件名（落在 ``settings.store_dir``，与 index.db 同目录）
DB_FILENAME = "provenance.db"
#: ``AuditTrail.export_prov`` 支持的 RDF 序列化格式（rdflib 口径）
PROV_EXPORT_FORMATS: tuple[str, ...] = ("turtle", "ntriples", "jsonld")
#: 记录种类（分层审计: 检索层 / 应答层）
KIND_ANSWER = "answer"
KIND_RETRIEVAL = "retrieval"
RECORD_KINDS: frozenset[str] = frozenset({KIND_ANSWER, KIND_RETRIEVAL})
#: 审计记录在 PROV-O 里的实体类型前缀（``entity_type = f"matrag_{kind}"``）
ENTITY_TYPE_PREFIX = "matrag_"
#: 内容级校验和投影用的固定标识（只参与哈希，不落盘）
DIGEST_ENTITY_TYPE = "matrag_audit_digest"
DIGEST_ACTIVITY_ID = "content_digest"
DIGEST_AGENT_ID = "matrag"
#: 无模型（确定性路径）时的 agent 标识
DEFAULT_AGENT_ID = "matrag"
#: ``list_recent`` 默认条数
DEFAULT_LIST_LIMIT = 20
#: 记录里承载校验和的字段名（``ProvenanceRecord.checksum`` 的落盘点）
CHECKSUM_KEY = "checksum"


class ProvenanceError(MatragError):
    """审计轨失败: 后端不可用 / 写入未确认 / 记录载荷不可解析。

    显式抛出而非静默降级——"没有审计"必须是个可以被看见的事件。
    """


@dataclass
class ProvenanceRecord:
    """一条审计记录（一次检索或一次应答的完整决策链）。

    - ``answer_id``: 审计记录 id（同一 id 重复记录时 semantica 按版本化归档旧值，
      不覆盖历史）;
    - ``kind``: ``"answer"`` / ``"retrieval"``（分层，见模块 docstring）;
    - ``recorded_at``: matrag 侧记录时间（ISO 8601，UTC）; 注意
      ``ProvenanceEntry.timestamp`` 是存储侧落戳，两者可能差几毫秒;
    - ``backend``: 写入时所用的存储后端;
    - ``plan_summary``: 查询理解摘要（``QueryPlan.to_dict()`` 的口径，或任意
      可 JSON 序列化映射）;
    - ``hits``: 检索命中锚点，每条一个 :class:`SourceReference` 形状的 dict
      （``document=doc_id``、``page``、``section=section_path``、
      ``metadata.chunk_id/table_id/score/legs_hit``）;
    - ``citations``: 应答层行级引用（``marker``/``doc_id``/``chunk_id``/``page``/
      ``section_path``/``table_id``/``score``），与正文里的 ``[n]`` 一一对应;
    - ``rule_outcomes``: 参与的确定性规则（``rule_id``/``statement``/``kind``/
      ``score``）;
    - ``degradations``: 本次的全部降级留痕，原样收存（不静默）;
    - ``extra``: 扩展位（接线方自带的附注，如覆盖度/答复摘要），参与校验和。

    可 JSON 序列化（:meth:`to_dict`），是 sqlite metadata 的落盘形状。
    """

    answer_id: str
    kind: str
    recorded_at: str
    backend: str
    query: str
    plan_summary: dict[str, Any] = field(default_factory=dict)
    hits: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    rule_outcomes: list[dict[str, Any]] = field(default_factory=list)
    degradations: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    elapsed_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
    checksum: str = ""

    @property
    def entity_type(self) -> str:
        """该记录在 PROV-O 里的实体类型（``matrag_answer`` / ``matrag_retrieval``）。"""
        return f"{ENTITY_TYPE_PREFIX}{self.kind}"

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化 dict（sqlite metadata 的落盘形状）。"""
        return {
            "answer_id": self.answer_id,
            "kind": self.kind,
            "recorded_at": self.recorded_at,
            "backend": self.backend,
            "query": self.query,
            "plan_summary": dict(self.plan_summary),
            "hits": [dict(hit) for hit in self.hits],
            "citations": [dict(item) for item in self.citations],
            "rule_outcomes": [dict(item) for item in self.rule_outcomes],
            "degradations": [dict(item) for item in self.degradations],
            "model": self.model,
            "elapsed_ms": self.elapsed_ms,
            "extra": dict(self.extra),
            CHECKSUM_KEY: self.checksum,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProvenanceRecord:
        """从落盘 dict 还原记录（深拷贝，防止与存储共享可变对象）。

        Raises:
            ValueError: 载荷不是 Mapping、``kind``/``answer_id`` 缺失或非法、
                列表字段形状不对（载荷被改坏时 fail-fast，由调用方决定如何处理）。
        """
        if not isinstance(data, Mapping):
            raise ValueError(f"审计记录载荷须为 Mapping: {type(data).__name__}")
        kind = str(data.get("kind") or "")
        if kind not in RECORD_KINDS:
            raise ValueError(f"未知审计记录 kind: {kind!r}（可选: {sorted(RECORD_KINDS)}）")
        answer_id = str(data.get("answer_id") or "")
        if not answer_id:
            raise ValueError("审计记录载荷缺少 answer_id")
        elapsed_ms = data.get("elapsed_ms") or 0
        if isinstance(elapsed_ms, bool) or not isinstance(elapsed_ms, int):
            raise ValueError(f"elapsed_ms 须为整数: {elapsed_ms!r}")
        return cls(
            answer_id=answer_id,
            kind=kind,
            recorded_at=str(data.get("recorded_at") or ""),
            backend=str(data.get("backend") or ""),
            query=str(data.get("query") or ""),
            plan_summary=_mapping_field(data, "plan_summary"),
            hits=_dict_list(data.get("hits"), "hits"),
            citations=_dict_list(data.get("citations"), "citations"),
            rule_outcomes=_dict_list(data.get("rule_outcomes"), "rule_outcomes"),
            degradations=_dict_list(data.get("degradations"), "degradations"),
            model=str(data.get("model") or ""),
            elapsed_ms=elapsed_ms,
            extra=_mapping_field(data, "extra"),
            checksum=str(data.get(CHECKSUM_KEY) or ""),
        )


def compute_record_checksum(record: ProvenanceRecord) -> str:
    """对一条审计记录的**内容**取校验和（semantica ``compute_checksum`` 口径）。

    整条记录（含 metadata 里的 query/hits/citations/degradations…）规范化为
    JSON 后投影到 ``compute_checksum`` 覆盖的字段上取 SHA-256。记录自身的
    ``checksum`` 字段不参与输入，因此它是"内容的指纹"而不是自引用。

    Returns:
        十六进制 SHA-256 字符串; 输入相同则结果必然相同（确定性，可对账）。
    """
    return _content_digest(record.to_dict())


class AuditTrail:
    """审计轨: 封装 semantica provenance，写/查/校验跨"检索→应答"的决策链。

    构造时读 ``settings.provenance_backend``::

        trail = AuditTrail()                       # 按 settings 选后端
        trail = AuditTrail("memory")               # 显式覆盖（参数 > settings）
        trail = AuditTrail("sqlite", db_path=...)  # 显式库路径

    - ``"sqlite"`` → ``SQLiteStorage(settings.store_dir / "provenance.db")``
      （或显式 ``db_path``），父目录不存在则创建，路径不可用即抛异常;
    - ``"memory"`` → ``InMemoryStorage()``，**不落盘**;
    - 其它任何取值 → ``ValueError``，不静默回落。

    线程安全由后端自负（sqlite 后端自带 busy_timeout）; 本类不做额外加锁。
    """

    def __init__(
        self,
        backend: str | None = None,
        *,
        db_path: Path | str | None = None,
    ) -> None:
        chosen = settings.provenance_backend if backend is None else backend
        normalized = str(chosen).strip().lower()
        if normalized not in SUPPORTED_BACKENDS:
            raise ValueError(
                f"未知 provenance_backend: {chosen!r}"
                f"（可选: {list(SUPPORTED_BACKENDS)}，见 settings.provenance_backend）"
            )

        path: Path | None = None
        storage: ProvenanceStorage
        if normalized == BACKEND_SQLITE:
            path = Path(db_path) if db_path is not None else settings.store_dir / DB_FILENAME
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                storage = SQLiteStorage(str(path))
            except Exception as exc:  # noqa: BLE001 —— 保留原始异常链，转成显式失败
                raise ProvenanceError(
                    f"provenance sqlite 后端不可用: {path}（{type(exc).__name__}: {exc}）"
                ) from exc
        else:
            storage = InMemoryStorage()

        self.backend = normalized
        self.db_path = path
        self.storage = storage
        self.manager = ProvenanceManager(storage=storage)
        self._closed = False

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def record_answer(
        self,
        *,
        query: str,
        plan_summary: Any = None,
        hits: Sequence[Any] = (),
        citations: Sequence[Any] = (),
        rule_outcomes: Sequence[Any] = (),
        degradations: Sequence[Any] = (),
        model: str = "",
        elapsed_ms: int = 0,
        extra: Mapping[str, Any] | None = None,
        answer_id: str | None = None,
    ) -> ProvenanceRecord:
        """把一次**应答**的完整决策链写成一条审计记录。

        Args:
            query: 用户原始问题（必填非空）。
            plan_summary: 查询理解摘要（``QueryPlan`` / ``ParsedQuery`` / dict，
                鸭子类型，见 :func:`_as_mapping`）; ``None`` 记空。
            hits: 检索命中（``RetrievalHit`` / dict），取 ``doc_id``/``chunk_id``
                /page/section/table_id/score 作锚点（原文不入库）。
            citations: 应答层行级引用（``answer.Citation`` / dict），``[n]`` →
                锚点，与正文引用一一对应。
            rule_outcomes: 参与的确定性规则（``RuleHint`` / dict）。
            degradations: 本次全部降级留痕（answer 层/检索层的 ``degradations``）。
            model: 合成所用模型名; 空串表示确定性模板（无 LLM）。
            elapsed_ms: 本次应答耗时（毫秒，>= 0 的整数）。
            extra: 扩展位附注（如 ``{"covered": true}``），原样入档并参与校验和。
            answer_id: 审计记录 id; 不传则自动生成（``answer:<uuid>``）。

        Returns:
            落档的 :class:`ProvenanceRecord`（含 ``answer_id`` 与 ``checksum``）。

        Raises:
            ValueError: ``query`` 为空、``elapsed_ms`` 非法、``answer_id`` 为空串;
            TypeError: 载荷不可 JSON 序列化或序列字段形状不对;
            ProvenanceError: 审计轨已关闭、后端不可用或写入未被确认。
        """
        self._ensure_open()
        return self._record(
            kind=KIND_ANSWER,
            answer_id=answer_id,
            query=query,
            plan_summary=plan_summary,
            hits=hits,
            citations=citations,
            rule_outcomes=rule_outcomes,
            degradations=degradations,
            model=model,
            elapsed_ms=elapsed_ms,
            extra=extra,
        )

    def record_retrieval(
        self,
        *,
        query: str,
        plan_summary: Any = None,
        hits: Sequence[Any] = (),
        degradations: Sequence[Any] = (),
        model: str = "",
        elapsed_ms: int = 0,
        extra: Mapping[str, Any] | None = None,
        answer_id: str | None = None,
    ) -> ProvenanceRecord:
        """把一次**检索**的决策链写成一条审计记录（分层记录，与应答记录分列）。

        与 :meth:`record_answer` 的差别只在语义层: 行级引用/规则属于应答层，检索
        层不记（``citations``/``rule_outcomes`` 恒为空）。其余参数、返回与失败
        约定完全相同。
        """
        self._ensure_open()
        return self._record(
            kind=KIND_RETRIEVAL,
            answer_id=answer_id,
            query=query,
            plan_summary=plan_summary,
            hits=hits,
            citations=(),
            rule_outcomes=(),
            degradations=degradations,
            model=model,
            elapsed_ms=elapsed_ms,
            extra=extra,
        )

    # ------------------------------------------------------------------ #
    # 查询 / 校验
    # ------------------------------------------------------------------ #
    def get(self, answer_id: str) -> ProvenanceRecord | None:
        """按 id 读回一条审计记录; 不存在返回 ``None``。

        Raises:
            ProvenanceError: 记录存在但载荷不可解析（被改坏/结构非法）——
                读不出来必须显式失败，不能假装"没有这条记录"。
        """
        entry = self.storage.retrieve(answer_id)
        if entry is None:
            return None
        try:
            return _record_from_entry(entry)
        except (ValueError, TypeError) as exc:
            raise ProvenanceError(
                f"审计记录载荷不可解析: {answer_id}（{type(exc).__name__}: {exc}）"
            ) from exc

    def verify(self, answer_id: str) -> bool:
        """校验一条审计记录是否未被篡改（entry 级 + 内容级，两层都过才 ``True``）。

        返回 ``False`` 的情形: 记录不存在、PROV-O 框架字段/哈希链被改
        （``verify_checksum`` 不过）、载荷结构坏、``answer_id`` 与实体不符、
        内容级校验和不匹配（含 metadata 里改一个字）。
        """
        entry = self.storage.retrieve(answer_id)
        if entry is None:
            return False
        if not verify_checksum(entry):
            return False
        try:
            record = _record_from_entry(entry)
        except (ValueError, TypeError):
            return False
        if record.answer_id != answer_id or record.entity_type != entry.entity_type:
            return False
        return bool(record.checksum) and compute_record_checksum(record) == record.checksum

    def list_recent(self, limit: int = DEFAULT_LIST_LIMIT) -> list[ProvenanceRecord]:
        """最近的审计记录（时间倒序，同刻按 sequence_id/id 稳定排序）。

        Args:
            limit: 最多返回几条（>= 1 的整数）。

        Raises:
            ValueError: ``limit`` 非法;
            ProvenanceError: 某条记录载荷不可解析。
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"limit 须为 >= 1 的整数: {limit!r}")
        entries = [
            entry
            for entry in self.storage.retrieve_all()
            if entry.entity_type.startswith(ENTITY_TYPE_PREFIX)
        ]
        entries.sort(
            key=lambda entry: (
                entry.timestamp,
                entry.sequence_id if entry.sequence_id is not None else 0,
                entry.entity_id,
            ),
            reverse=True,
        )
        records: list[ProvenanceRecord] = []
        for entry in entries[:limit]:
            try:
                record = _record_from_entry(entry)
            except (ValueError, TypeError) as exc:
                raise ProvenanceError(
                    f"审计记录载荷不可解析: {entry.entity_id}"
                    f"（{type(exc).__name__}: {exc}）"
                ) from exc
            if record.answer_id != entry.entity_id:
                continue  # semantica 版本化归档的历史行（answer_id 与归档键不同）
            records.append(record)
        return records

    def verify_chain(self) -> dict[str, Any]:
        """全库哈希链校验（semantica ``ProvenanceManager.verify_chain`` 直通）。

        逐条目检查"自身校验和 + 前驱校验和 + sequence_id 连续"，可发现整行被删/
        被换。``{"valid": bool, "total_entries": int, "broken_links": list}``。
        """
        return self.manager.verify_chain()

    def export_prov(self, format: str = "turtle", base_uri: str | None = None) -> str:
        """把审计轨导出为 W3C PROV-O RDF 文本（semantica ``export_prov`` 直通）。

        审计交付物: 整库序列化为 PROV-O RDF——每条审计记录是 ``prov:Entity``
        （entity_type = ``matrag_answer``/``matrag_retrieval``），写入活动是
        ``prov:Activity``（``record_answer``/``record_retrieval``），模型/工具是
        ``prov:Agent``; 命中锚点/问题文本按 semantica 的标准映射（``prov:used`` /
        ``source_quote``）输出。版本化归档的历史行也会导出（完整对账）。

        Args:
            format: ``"turtle"``（默认）/ ``"ntriples"`` / ``"jsonld"``;
            base_uri: 实体 URI 的命名空间; 默认 semantica ``DEFAULT_BASE_URI``
                （与 KG 导出的 URI 同口径，实体可共解析）。

        Returns:
            序列化后的 RDF 字符串（UTF-8）。

        Raises:
            ValueError: ``format`` 不在 :data:`PROV_EXPORT_FORMATS`;
            ProvenanceError: 序列化失败（含 ``rdflib`` 未安装——导出能力缺失
                必须显式可见，不静默降级）。

        Note:
            导出只读存储、与 :meth:`close` 无关（关闭只挡写）。
        """
        fmt = str(format).strip().lower()
        if fmt not in PROV_EXPORT_FORMATS:
            raise ValueError(
                f"不支持 PROV-O 导出格式: {format!r}（可选: {list(PROV_EXPORT_FORMATS)}）"
            )
        try:
            return self.manager.export_prov(format=fmt, base_uri=base_uri)
        except Exception as exc:  # noqa: BLE001 —— 依赖缺失/序列化失败统一转显式失败
            raise ProvenanceError(
                f"PROV-O 导出失败（format={fmt}）: {type(exc).__name__}: {exc}"
            ) from exc

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    @property
    def closed(self) -> bool:
        """本轨是否已 :meth:`close`。"""
        return self._closed

    def close(self) -> None:
        """关闭审计轨（幂等）。

        semantica 的两种后端都不持有长连接（``SQLiteStorage`` 每次调用自行开闭），
        没有需要释放的系统资源; ``close()`` 的语义是"本轨不再接受新记录"——关闭后
        写入抛 :class:`ProvenanceError`，而 :meth:`get` / :meth:`verify` /
        :meth:`list_recent` / :meth:`verify_chain` 仍可查询已记录的审计
        （sqlite 后端另开一个 :class:`AuditTrail` 指向同一文件同样可查）。
        重复调用是幂等空操作。
        """
        self._closed = True

    def __enter__(self) -> AuditTrail:
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _record(
        self,
        *,
        kind: str,
        answer_id: str | None,
        query: str,
        plan_summary: Any,
        hits: Sequence[Any],
        citations: Sequence[Any],
        rule_outcomes: Sequence[Any],
        degradations: Sequence[Any],
        model: str,
        elapsed_ms: int,
        extra: Mapping[str, Any] | None,
    ) -> ProvenanceRecord:
        """两种记录的公共写入路径: 校验 → 归一 → 校验和 → 落库 → 返回。"""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 不能为空（审计记录必须回答'问的是什么'）")
        if isinstance(elapsed_ms, bool) or not isinstance(elapsed_ms, int) or elapsed_ms < 0:
            raise ValueError(f"elapsed_ms 须为 >= 0 的整数: {elapsed_ms!r}")
        record_id = answer_id if answer_id is not None else f"{kind}:{uuid.uuid4().hex}"
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValueError(f"answer_id 不能为空: {answer_id!r}")

        record = ProvenanceRecord(
            answer_id=record_id,
            kind=kind,
            recorded_at=datetime.now(UTC).isoformat(),
            backend=self.backend,
            query=query,
            plan_summary=_as_mapping(plan_summary, "plan_summary"),
            hits=[_hit_source(hit) for hit in _as_sequence(hits, "hits")],
            citations=[_citation_dict(item) for item in _as_sequence(citations, "citations")],
            rule_outcomes=[
                _rule_dict(item) for item in _as_sequence(rule_outcomes, "rule_outcomes")
            ],
            degradations=[
                _as_mapping(item, "degradations 条目")
                for item in _as_sequence(degradations, "degradations")
            ],
            model=str(model or "").strip(),
            elapsed_ms=elapsed_ms,
            extra=_as_mapping(extra, "extra"),
        )
        record.checksum = compute_record_checksum(record)

        entry = self.manager.track_entity(
            record.answer_id,
            source=_anchor_summary(record.hits),
            metadata=record.to_dict(),
            entity_type=record.entity_type,
            activity_id=f"record_{record.kind}",
            # 无模型（确定性模板路径）时 agent 记 "matrag"，不留空
            agent_id=record.model or DEFAULT_AGENT_ID,
            agent_type="software_agent",
            source_quote=record.query,
            used_entities=_anchor_list(record.hits),
            confidence=1.0,
        )
        # track_entity 存储失败时只记日志并返回 None——"没写进去却当成写了"
        # 正是本模块要消灭的静默降级，因此这里显式失败。
        if entry is None or entry.checksum is None:
            raise ProvenanceError(
                f"审计记录写入未被后端确认: {record.answer_id}（backend={self.backend}）"
            )
        return record

    def _ensure_open(self) -> None:
        if self._closed:
            raise ProvenanceError("AuditTrail 已关闭，不再接受新记录")


# --------------------------------------------------------------------------- #
# 记录 ↔ 存储实体
# --------------------------------------------------------------------------- #
def _record_from_entry(entry: ProvenanceEntry) -> ProvenanceRecord:
    """从存储实体还原审计记录，并核对实体类型与记录自述一致。"""
    if not isinstance(entry.metadata, Mapping):
        raise ValueError(f"审计记录载荷不是 Mapping: {type(entry.metadata).__name__}")
    record = ProvenanceRecord.from_dict(entry.metadata)
    if record.entity_type != entry.entity_type:
        raise ValueError(
            f"实体类型与载荷不一致: entry={entry.entity_type!r} payload={record.entity_type!r}"
        )
    return record


def _content_digest(record_dict: Mapping[str, Any]) -> str:
    """把整条记录投影到 ``compute_checksum`` 覆盖的字段上取内容校验和。

    semantica 的 ``compute_checksum`` 只哈希 PROV-O 框架字段（entity_type/
    activity_id/agent_id/agent_type/source_document/timestamp/confidence/…），
    **不含 metadata**; 而我们的决策链全部在 metadata 里。因此这里把规范化后的
    完整记录 JSON 放进被覆盖的 ``source_document`` 字段——投影仅用于取哈希，
    不落盘（落盘的 ``source_document`` 是命中锚点摘要，见 :meth:`AuditTrail._record`）。
    """
    body = {key: value for key, value in record_dict.items() if key != CHECKSUM_KEY}
    projection = {
        "entity_type": DIGEST_ENTITY_TYPE,
        "activity_id": DIGEST_ACTIVITY_ID,
        "agent_id": DIGEST_AGENT_ID,
        "agent_type": "software_agent",
        "source_document": _canonical_json(body),
        "confidence": 1.0,
    }
    return compute_checksum(projection)


def _canonical_json(payload: Mapping[str, Any]) -> str:
    """规范化 JSON: 键排序 + 紧凑分隔 + 中文不转义（保证校验和确定可复现）。"""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _anchor_list(hits: Sequence[Mapping[str, Any]]) -> list[str]:
    """命中锚点列表（``doc_id#chunk_id``，写进 PROV-O 的 ``prov:used``）。"""
    anchors: list[str] = []
    for hit in hits:
        chunk_id = str(hit.get("chunk_id") or "")
        if not chunk_id:
            continue
        anchors.append(f"{hit.get('document') or ''}#{chunk_id}")
    return anchors


def _anchor_summary(hits: Sequence[Mapping[str, Any]]) -> str:
    """命中锚点摘要（写进 PROV-O 的 ``source_document``）。"""
    return ",".join(_anchor_list(hits))


# --------------------------------------------------------------------------- #
# 输入归一（鸭子类型: 对象属性 / dict 都收; 不可序列化即显式失败）
# --------------------------------------------------------------------------- #
def _as_sequence(value: Any, name: str) -> list[Any]:
    """把序列参数归一为 list（``None`` → 空列表; 字符串/映射/不可迭代 → 显式失败）。"""
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        raise TypeError(f"{name} 须为序列，收到 {type(value).__name__}")
    try:
        return list(value)
    except TypeError as exc:
        raise TypeError(f"{name} 不可迭代: {type(value).__name__}") from exc


def _as_mapping(value: Any, name: str) -> dict[str, Any]:
    """把任意输入归一为可 JSON 序列化的 dict。

    支持: ``None`` → 空 dict、Mapping、带 ``to_dict()`` 的对象（``QueryPlan`` /
    ``AgentRecord`` …）、dataclass（``ParsedQuery`` …）; 其余显式 ``TypeError``。
    """
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return _jsonable(dict(value))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(to_dict())
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    raise TypeError(f"{name} 须为 Mapping / 带 to_dict() / dataclass，收到 {type(value).__name__}")


def _hit_source(hit: Any) -> dict[str, Any]:
    """一条检索命中 → semantica ``SourceReference`` 形状的 dict（锚点，不存原文）。

    ``document=doc_id``、``section=section_path``、``page`` 是 PROV-O 的源定位;
    ``chunk_id``/``table_id``/``score``/``legs_hit`` 放 ``metadata``——源引用本身
    没有 chunk 概念，硬塞进 ``line`` 会丢语义。``confidence`` 保持 1.0:
    检索融合分不是置信度，塞进去是对字段语义的滥用。
    """
    meta = _field(hit, "meta") or {}
    if not isinstance(meta, Mapping):
        meta = {}
    table_id = meta.get("table_id")
    return SourceReference(
        document=str(_field(hit, "doc_id") or ""),
        page=_as_int(meta.get("page")) or None,
        section=str(meta.get("section_path") or "") or None,
        confidence=1.0,
        metadata={
            "chunk_id": str(_field(hit, "chunk_id") or ""),
            "table_id": str(table_id) if table_id else None,
            "score": _as_float(_field(hit, "rrf_score", _field(hit, "score"))),
            "legs_hit": [str(leg) for leg in (_field(hit, "legs_hit") or ())],
        },
    ).to_dict()


def _citation_dict(citation: Any) -> dict[str, Any]:
    """一条行级引用（``answer.Citation`` / dict）→ 归一 dict（``[n]`` → 锚点）。"""
    meta = _field(citation, "meta") or {}
    if not isinstance(meta, Mapping):
        meta = {}
    table_id = _field(citation, "table_id", meta.get("table_id"))
    return {
        "marker": str(_field(citation, "marker") or ""),
        "doc_id": str(_field(citation, "doc_id") or ""),
        "chunk_id": str(_field(citation, "chunk_id") or ""),
        "page": _as_int(_field(citation, "page", meta.get("page"))),
        "section_path": str(_field(citation, "section_path", meta.get("section_path")) or ""),
        "table_id": str(table_id) if table_id else None,
        "score": _as_float(_field(citation, "score")),
    }


def _rule_dict(rule: Any) -> dict[str, Any]:
    """一条规则结果（``RuleHint`` / dict）→ 归一 dict（与 answer 层口径一致）。"""
    return {
        "rule_id": str(_field(rule, "rule_id", _field(rule, "id")) or ""),
        "statement": str(_field(rule, "statement") or ""),
        "kind": str(_field(rule, "kind") or ""),
        "score": _as_float(_field(rule, "score")),
    }


def _jsonable(value: Any) -> Any:
    """递归归一为可 JSON 序列化值; 认不出来即 ``TypeError``（fail-fast）。

    怀着"先存进去再说"的猜测做隐式转换，只会把脏数据腌在审计库里。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=str)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(to_dict())
    raise TypeError(f"审计载荷不是可 JSON 序列化的类型: {type(value).__name__}")


def _dict_list(value: Any, name: str) -> list[dict[str, Any]]:
    """从落盘载荷里取字典列表（深拷贝隔离）; 形状不对即 ``ValueError``。"""
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        raise ValueError(f"{name} 须为列表: {type(value).__name__}")
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"{name} 条目须为 Mapping: {type(item).__name__}")
        items.append(copy.deepcopy(dict(item)))
    return items


def _mapping_field(data: Mapping[str, Any], name: str) -> dict[str, Any]:
    """从落盘载荷里取字典字段（深拷贝隔离）; 形状不对即 ``ValueError``。"""
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} 须为 Mapping: {type(value).__name__}")
    return copy.deepcopy(dict(value))


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """读字段（鸭子类型: Mapping 直接取，否则 getattr，缺失不抛）。"""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_float(value: Any, *, default: float = 0.0) -> float:
    """宽松取浮点（非数值退化为 ``default``，得分/耗时不致命）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _as_int(value: Any, *, default: int = 0) -> int:
    """宽松取整数（非数值退化为 ``default``）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


__all__ = [
    "BACKEND_MEMORY",
    "BACKEND_SQLITE",
    "DB_FILENAME",
    "DEFAULT_LIST_LIMIT",
    "ENTITY_TYPE_PREFIX",
    "KIND_ANSWER",
    "KIND_RETRIEVAL",
    "PROV_EXPORT_FORMATS",
    "RECORD_KINDS",
    "SUPPORTED_BACKENDS",
    "AuditTrail",
    "ProvenanceError",
    "ProvenanceRecord",
    "compute_record_checksum",
]
