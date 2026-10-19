"""混合检索层（P1-5）: 查询理解 → 问题路由 → 多腿 RRF 融合检索。

对外只暴露三个稳定入口::

    from matrag.retrieve import parse_query, route, HybridRetriever

- :mod:`matrag.retrieve.query`     —— 查询理解（分词/filters/型号别名表）
- :mod:`matrag.retrieve.routing`   —— 问题类型路由（决定用哪几条腿）
- :mod:`matrag.retrieve.retriever` —— 向量+关键词+图谱 RRF 融合检索与引用溯源
- :mod:`matrag.retrieve.graph_index` —— 图谱检索腿（实体链接 → 源知识卡 chunk）
- :mod:`matrag.retrieve.lexicon`   —— 章节词典（按"章节名"过滤检索的数据源）
- :mod:`matrag.retrieve.rule_hint` —— 确定性规则提示（纯数据表打分，解释性附加）

``graph`` 腿已落地（P1-5b）: :class:`GraphIndex` 把查询链接到域知识图谱里的
术语/公理实体并展开来源知识卡 chunk; ``graph_index=None``（未接线）时检索层
照旧跳过该腿并留痕（``plan["degradations"]`` / ``retriever.degradations``）。

W3-A 并行任务新增的 ``matrag.retrieve.understand``（``QueryPlan``）**不在这里
硬导入**: 检索层只对 plan 做鸭子类型读字段（``retrieve_with_plan``），
需要构造 plan 时才经 ``retriever.build_query_plan`` 惰性导入，缺模块只留痕
（``understand_unavailable``）不崩。
"""

from matrag.retrieve.catalog import (
    ProductCatalog,
    load_catalog,
    save_catalog,
)
from matrag.retrieve.graph_index import (
    EntityLink,
    GraphIndex,
    build_graph_index,
)
from matrag.retrieve.lexicon import (
    SectionLexicon,
    build_lexicon,
    load_lexicon,
    match_sections,
    normalize_title,
    save_lexicon,
)
from matrag.retrieve.query import (
    DEFAULT_ALIAS_PATH,
    FILTER_DOC_ID,
    FILTER_SECTION,
    FILTER_SECTION_CONTAINS,
    FILTER_TABLE,
    LEXICON_FILENAME,
    LEXICON_PATH_ENV,
    STOPWORDS,
    ModelAlias,
    ParsedQuery,
    lexicon_path,
    load_model_alias_records,
    load_model_aliases,
    parse_query,
    reset_lexicon_cache,
    reset_tokenizer_cache,
    resolve_alias_path,
    section_title_hits,
    tokenizer_provenance,
)
from matrag.retrieve.retriever import (
    FUSION_K,
    KNOWN_LEGS,
    OVERFETCH,
    RULE_HINT_TOP_N,
    HybridRetriever,
    RetrievalHit,
    build_query_plan,
    merge_legs,
)
from matrag.retrieve.routing import (
    CATALOG_KEYWORDS,
    DEFAULT_LEGS,
    FAULT_KEYWORDS,
    FORMULA_KEYWORDS,
    GRAPH_LEGS,
    PROCEDURE_KEYWORDS,
    SPEC_VALUE_KEYWORDS,
    QueryKind,
    RoutedQuery,
    build_where,
    kind_legs,
    route,
    routing_rules,
)
from matrag.retrieve.rule_hint import (
    RULE_KEYWORDS,
    RULE_KINDS,
    RuleHint,
    rule_hint_summary,
    score_rule,
    select_rule_hints,
)
from matrag.retrieve.understand import (
    QueryPlan,
    merge_regex,
    regex_only_plan,
    understand,
)

__all__ = [
    "ProductCatalog",
    "load_catalog",
    "save_catalog",
    "EntityLink",
    "GraphIndex",
    "build_graph_index",
    "QueryPlan",
    "understand",
    "merge_regex",
    "regex_only_plan",
    # query
    "ModelAlias",
    "ParsedQuery",
    "parse_query",
    "load_model_aliases",
    "load_model_alias_records",
    "resolve_alias_path",
    "tokenizer_provenance",
    "reset_tokenizer_cache",
    "DEFAULT_ALIAS_PATH",
    "FILTER_DOC_ID",
    "FILTER_SECTION",
    "FILTER_SECTION_CONTAINS",
    "FILTER_TABLE",
    "LEXICON_FILENAME",
    "LEXICON_PATH_ENV",
    "STOPWORDS",
    "lexicon_path",
    "reset_lexicon_cache",
    "section_title_hits",
    # lexicon（章节词典）
    "SectionLexicon",
    "build_lexicon",
    "load_lexicon",
    "match_sections",
    "normalize_title",
    "save_lexicon",
    # routing
    "QueryKind",
    "RoutedQuery",
    "route",
    "build_where",
    "kind_legs",
    "routing_rules",
    "DEFAULT_LEGS",
    "GRAPH_LEGS",
    "SPEC_VALUE_KEYWORDS",
    "PROCEDURE_KEYWORDS",
    "FORMULA_KEYWORDS",
    "FAULT_KEYWORDS",
    "CATALOG_KEYWORDS",
    # retriever
    "HybridRetriever",
    "RetrievalHit",
    "merge_legs",
    "build_query_plan",
    "FUSION_K",
    "OVERFETCH",
    "KNOWN_LEGS",
    "RULE_HINT_TOP_N",
    # rule_hint（确定性规则提示）
    "RuleHint",
    "select_rule_hints",
    "score_rule",
    "rule_hint_summary",
    "RULE_KEYWORDS",
    "RULE_KINDS",
]
