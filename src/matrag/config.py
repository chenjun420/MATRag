"""配置: 全部环境变量驱动（MATRAG_ 前缀），profile 决定存储/关键词腿后端。

演进约定: dev=sqlite-vec/FTS5/file, prod=PG(pgvector/pg-textsearch)/age。
任何"默认写死"的上游选择都不允许出现在代码里，必须经 settings 读取。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MATRAG_", env_file=REPO_ROOT / ".env", extra="ignore"
    )

    profile: str = "dev"  # dev | prod

    # ---- LLM (OpenAI 兼容 chat completions) ----
    llm_base: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.0
    llm_timeout: float = 60.0
    llm_max_tokens: int = 4096
    # None=不发送（用平台默认）; False=显式关闭思考（大表重建实测 121s→10s）
    llm_enable_thinking: bool | None = None

    # ---- Embedding (OpenAI 兼容 /embeddings; MaaS) ----
    embed_base: str = ""
    embed_api_key: str = ""
    embed_model: str = ""
    embed_dim: int = 1024
    embed_batch_size: int = 10
    embed_timeout: float = 60.0
    # Ark/DashScope 限流: 批量间最小间隔(s)与 429 重试次数
    embed_min_interval: float = 1.0
    embed_max_retries: int = 6

    # ---- 分层后端（profile 决定默认值，.env 显式覆盖优先）----
    parser_provider: str = "local"     # local | <外部解析器名，评测通过后启用>
    vector_backend: str = "sqlite"     # sqlite | pgvector
    keyword_backend: str = "fts5"      # fts5 | pg-textsearch
    graph_backend: str = "file"        # file | age
    provenance_backend: str = "sqlite" # semantica 提供: sqlite | memory

    # ---- 检索/应答策略 ----
    # True: HybridRetriever.retrieve 默认先做语义查询理解（understand→plan）再检索。
    # 关闭后退化为"正则抽取条件 + 原查询检索"（确定性路径, 无 LLM 往返）。
    retrieve_auto_understand: bool = True
    answer_max_items: int = 12
    answer_max_chars: int = 12000

    # ---- 路径 ----
    data_dir: Path = REPO_ROOT / "data"
    corpus_dir: Path = REPO_ROOT / "data" / "corpus"
    store_dir: Path = REPO_ROOT / "data" / "store"
    reports_dir: Path = REPO_ROOT / "data" / "reports"
    domain_dir: Path = REPO_ROOT / "data" / "domain"
    eval_dir: Path = REPO_ROOT / "data" / "eval"
    pg_dsn: str = ""

    # ---- gateway ----
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8321
    gateway_api_key: str = ""

    @property
    def live(self) -> bool:
        """真实外部调用是否可用（测试用）。LLM/Embedding 端点齐备才算 live。"""
        return bool(self.llm_base and self.llm_api_key and self.llm_model)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.corpus_dir, self.store_dir,
                  self.reports_dir, self.domain_dir, self.eval_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


# 便捷单例（测试可 monkeypatch settings 的字段）
settings = get_settings()
