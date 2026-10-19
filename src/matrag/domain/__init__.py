"""域知识层: 知识卡片 + 术语/公理 + 本体 + 知识图谱 + 确定性推理规则。

- :mod:`matrag.domain.cards` —— power 知识文档 → 卡片语料;
- :mod:`matrag.domain.rules_loader` —— rules.yaml → 规则对象 / Datalog Horn 子句;
- :mod:`matrag.domain.rules` —— semantica 推理引擎装配 + 130 条规则自测;
- :mod:`matrag.domain.terms` / :mod:`matrag.domain.axioms` —— 开关电源术语表 / 公理表;
- :mod:`matrag.domain.ontology` —— SHACL → 电力域本体 (``power_ontology.ttl/.json``);
- :mod:`matrag.domain.spec_params` —— 规格书参数 / 标准引用抽取;
- :mod:`matrag.domain.test_methods` —— 产测方法表 (``power_test_methods.yaml``)
  + 公共知识卡片 (``power_methods.jsonl``);
- :mod:`matrag.domain.knowledge_graph` —— 五层连图
  (产品-参数-术语-规则-方法-本体, ``power_knowledge_graph.json``)。
"""

from __future__ import annotations

from .axioms import (
    Axiom,
    AxiomIssue,
    axiom_stats,
    axioms_for_category,
    load_axioms,
    validate_axioms,
)
from .cards import (
    DOMAIN_NAME,
    KNOWLEDGE_PATH_ENV,
    KNOWLEDGE_SOURCE_LABEL,
    RULES_PATH_ENV,
    RULES_SOURCE_LABEL,
    DomainCard,
    load_domain_rules_raw,
    load_knowledge_cards,
    resolve_knowledge_path,
    resolve_rules_path,
    write_knowledge_cards,
)
from .knowledge_graph import (
    KgEdge,
    KgIssue,
    KgNode,
    KnowledgeGraph,
    build_knowledge_graph,
    write_knowledge_graph,
)
from .ontology import (
    OntologyBuildError,
    OntologyClass,
    OntologyIssue,
    OntologyProperty,
    OntologyRuleLink,
    PowerOntology,
    build_ontology,
    write_ontology,
)
from .rules import (
    RuleEvaluationError,
    SafeExpression,
    build_reasoner,
    decode_value,
    encode_value,
    evaluate_constraint,
    evaluate_rule,
    plan_shape,
    verify_all_rules,
    verify_rule,
)
from .rules_loader import (
    DomainRule,
    load_rules,
    render_datalog,
    rule_clause,
    rule_slug,
    to_datalog,
    write_generated,
)
from .spec_params import (
    SpecParameter,
    SpecStandard,
    classify_section,
    classify_table,
    extract_spec_parameters,
    extract_standards,
    spec_param_stats,
)
from .terms import (
    Term,
    TermIssue,
    build_term_index,
    find_term,
    load_terms,
    term_stats,
    validate_terms,
)
from .test_methods import (
    TestMethod,
    TestMethodIssue,
    build_method_cards,
    load_test_methods,
    resolve_test_methods_path,
    test_method_stats,
    validate_test_methods,
)

__all__ = [
    "DOMAIN_NAME",
    "KNOWLEDGE_PATH_ENV",
    "KNOWLEDGE_SOURCE_LABEL",
    "RULES_PATH_ENV",
    "RULES_SOURCE_LABEL",
    "Axiom",
    "AxiomIssue",
    "DomainCard",
    "DomainRule",
    "KgEdge",
    "KgIssue",
    "KgNode",
    "KnowledgeGraph",
    "OntologyBuildError",
    "OntologyClass",
    "OntologyIssue",
    "OntologyProperty",
    "OntologyRuleLink",
    "PowerOntology",
    "RuleEvaluationError",
    "SafeExpression",
    "SpecParameter",
    "SpecStandard",
    "Term",
    "TermIssue",
    "TestMethod",
    "TestMethodIssue",
    "axiom_stats",
    "axioms_for_category",
    "build_knowledge_graph",
    "build_method_cards",
    "build_ontology",
    "build_reasoner",
    "build_term_index",
    "classify_section",
    "classify_table",
    "decode_value",
    "encode_value",
    "evaluate_constraint",
    "evaluate_rule",
    "extract_spec_parameters",
    "extract_standards",
    "find_term",
    "load_axioms",
    "load_domain_rules_raw",
    "load_knowledge_cards",
    "load_rules",
    "load_terms",
    "load_test_methods",
    "plan_shape",
    "render_datalog",
    "resolve_knowledge_path",
    "resolve_rules_path",
    "resolve_test_methods_path",
    "rule_clause",
    "rule_slug",
    "spec_param_stats",
    "term_stats",
    "test_method_stats",
    "to_datalog",
    "validate_axioms",
    "validate_terms",
    "validate_test_methods",
    "verify_all_rules",
    "verify_rule",
    "write_generated",
    "write_knowledge_cards",
    "write_knowledge_graph",
    "write_ontology",
]
