"""域规则 → 电力域本体 (power domain ontology)。

本模块补上仓库里长期空白的一层: **显式的类 / 属性 / 量纲本体**。
在此之前, 全仓只用到 semantica 的一个函数
(``run_shacl_validation``, 供 :mod:`matrag.domain.rules` 求值 SHACL 约束),
本体语义只隐含在 ``rules.yaml`` 的 130 条规则里, 没有任何可查询的本体文档产物。

三条建模轴 (全部数据驱动, **没有任何按 rule_id 的分支**)::

    轴        来源                                  语义
    --------  -----------------------------------  ------------------------------
    subject   SHACL shape 的 ``sh:targetClass``    被测对象类型 (DUT 的类)
    category  规则自身的 ``category`` 字段         知识主题 (规则讲哪块知识)
    property  ``sh:path`` / ``derive.inputs`` /   量纲参数 (可计算、可带单位)
              ``derive.output`` / ``input_params``

为什么 subject 轴与 category 轴必须分开:

- ``sh:targetClass ps:Protection`` 说的是"**被测对象**是保护装置", 它是
  *实例的类* —— ``urn:dut`` 就是它的实例, 所以它是可实例化的;
- ``category: protection`` 说的是"这条**规则**讲的是保护这个知识主题",
  它是*规则的分类维度*, 不是被测对象的类型。混在一个命名空间里,
  ``rules_for_class("protection")`` 就没法区分"找测保护装置的规则"
  还是"找保护主题的全部规则", 上层路由必然出错;
- 工程上二者生命周期不同: category 是**闭集** (规则字段, 26 个, 全库可控),
  subject 是**开放集** (shape 作者自由命名, 44 个, 随时可能新增/改名);
- 因此本模块用两个**独立命名空间**物理隔离: 被测对象类型 ``power:``,
  知识主题 ``power/category:``, 即使将来出现同名也不会互相覆盖, 且
  :meth:`PowerOntology.classes` 返回的每一项都带 ``axis`` 字段标明归属。

量纲 (unit) 建模: 规则文件的 ``variables`` 字段是唯一的单位来源。
**未声明单位的参数不会消失** —— 它们的 ``unit`` 是空串 ``""``, 在
``properties()`` / ``properties_without_unit()`` / ``stats()`` / ``to_dict()``
里都可见。单位缺失本身是一条**质量门 issue** (:data:`ISSUE_UNIT_MISSING`),
而不是被静默丢弃。

参数名归一: SHACL ``sh:path`` 写的是 camelCase (``contactResistance``),
而 ``test.given`` 的事实键写的是 snake_case (``contact_resistance``)。
所有来源 (path / inputs / output / given 键) 一律经
:func:`normalize_param_name` 归一成同一套命名, 否则同一物理量会在本体里
被拆成两个属性, 和约束求值层对不上。归并前的原始写法全部保留在
``OntologyProperty.declared_names`` 里, 可回溯。

什么**不是**量纲属性: 约束型规则的输出 ``violation`` (``rules_loader`` 的
``VIOLATION_KEY``) 是"这条约束被违反"的布尔判定, 不是被测对象的物理量,
因此不建成属性 —— 否则单位质量门会对着一个 ``xsd:boolean`` 报警。它仍然
保留在 ``OntologyRuleLink.produces`` 与图的 ``power:verdict`` 上。

质量门 :meth:`PowerOntology.validate` 是本模块的核心价值: 它把"规则集自相
矛盾"的地方显式列出来 (见 ``ISSUE_*`` 常量), **空清单才代表通过**。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD
from semantica.ontology import OWLGenerator

from matrag.domain.rules_loader import DomainRule, load_rules, rule_slug
from matrag.errors import MatragError

__all__ = [
    "AXIS_CATEGORY",
    "AXIS_SUBJECT",
    "CATEGORY_NS",
    "DEFAULT_GENERATED_DIR",
    "ISSUE_RULE_INCONSISTENT",
    "ISSUE_SHAPE_UNPARSEABLE",
    "ISSUE_UNCLASSIFIED_CONSTRAINT",
    "ISSUE_UNIT_CONFLICT",
    "ISSUE_UNIT_MISSING",
    "ONTOLOGY_NAME",
    "ONTOLOGY_VERSION",
    "ORIGIN_DERIVE_INPUT",
    "ORIGIN_DERIVE_OUTPUT",
    "ORIGIN_GIVEN_KEY",
    "ORIGIN_SHACL_PATH",
    "POWER_NS",
    "OntologyBuildError",
    "OntologyClass",
    "OntologyIssue",
    "OntologyProperty",
    "OntologyRuleLink",
    "PowerOntology",
    "build_ontology",
    "normalize_param_name",
    "write_ontology",
]


class OntologyBuildError(MatragError):
    """本体构建失败 (依赖缺失 / 图结构不合法 / 空规则集 / 序列化失败)。"""


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 被测对象类型 + 量纲参数 + 规则个体所在命名空间 (与规则文件 SHACL 的 ``ps:``
#: 前缀一致, 因此 shape 里的 targetClass / path 可以直接对上)。
POWER_NS = "https://matrag.example.org/power#"

#: 知识主题分类轴的独立命名空间 (与 :data:`POWER_NS` 物理隔离, 杜绝同名覆盖)。
CATEGORY_NS = "https://matrag.example.org/power/category#"

ONTOLOGY_NAME = "PowerOntology"
ONTOLOGY_VERSION = "1.0"

#: 产物默认输出目录 (与 ``rules_loader.DEFAULT_GENERATED_DIR`` 同址同风格)。
DEFAULT_GENERATED_DIR = (
    Path(__file__).resolve().parents[3] / "data" / "domain" / "generated"
)
TTL_FILENAME = "power_ontology.ttl"
JSON_FILENAME = "power_ontology.json"

# --- class 轴标签 ---------------------------------------------------------- #
AXIS_SUBJECT = "subject"  # 被测对象类型 (sh:targetClass)
AXIS_CATEGORY = "category"  # 知识主题 (rule.category)

# --- property 来源标签 ----------------------------------------------------- #
ORIGIN_SHACL_PATH = "shacl_path"  # SHACL shape 的 sh:path
ORIGIN_DERIVE_INPUT = "derive_input"  # derive.inputs
ORIGIN_DERIVE_OUTPUT = "derive_output"  # derive.output
ORIGIN_GIVEN_KEY = "given_key"  # 约束型规则的 test.given 键 (输入)

# --- 质量门 issue 类型 ----------------------------------------------------- #
#: 同一参数在不同规则里声明了不同单位, 且无显式换算声明。
ISSUE_UNIT_CONFLICT = "unit_conflict"
#: 被多条规则引用的参数, 其 ``variables`` 未声明单位 (或声明为空串)。
ISSUE_UNIT_MISSING = "unit_missing"
#: 约束型规则缺 ``sh:targetClass``, 挂不到任何被测对象类型上。
ISSUE_UNCLASSIFIED_CONSTRAINT = "unclassified_constraint"
#: 规则不自洽: ``derive.inputs`` 与 ``test.given`` 的键集不一致。
ISSUE_RULE_INCONSISTENT = "rule_inconsistent"
#: 约束型规则的 shape 不是合法 Turtle, 无法抽取 targetClass / sh:path。
ISSUE_SHAPE_UNPARSEABLE = "shape_unparseable"

#: 单位缺失质量门只针对"被多条规则引用"的参数 (单条规则自用不报, 噪声太大)。
UNIT_MISSING_MIN_RULES = 2

_SH_TARGET_CLASS = URIRef("http://www.w3.org/ns/shacl#targetClass")
_SH_PATH = URIRef("http://www.w3.org/ns/shacl#path")

# 本体自定义词汇 (全部落在 POWER_NS 下, 由 rdflib 补写 —— semantica 的通用
# OWL 模型表达不了"单位""规则个体""规则↔参数关联"这些电力域语义)。
_HAS_UNIT = URIRef(POWER_NS + "hasUnit")
_DECLARED_UNITS = URIRef(POWER_NS + "declaredUnits")
_HAS_RULE = URIRef(POWER_NS + "hasRule")
_IN_CATEGORY = URIRef(POWER_NS + "inCategory")
_CONSTRAINS = URIRef(POWER_NS + "constrainsClass")
_DECLARED_NAMES = URIRef(POWER_NS + "declaredNames")
_DECLARED_FROM = URIRef(POWER_NS + "declaredFrom")
_RULE_ID = URIRef(POWER_NS + "ruleId")
_STATEMENT = URIRef(POWER_NS + "statement")
_SCOPE = URIRef(POWER_NS + "scope")
_KIND = URIRef(POWER_NS + "ruleKind")
_CONSUMES = URIRef(POWER_NS + "consumes")
_PRODUCES = URIRef(POWER_NS + "produces")
_VERDICT = URIRef(POWER_NS + "verdict")


# --------------------------------------------------------------------------- #
# 参数名归一化
# --------------------------------------------------------------------------- #

_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_ACRONYM_RE = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")


def normalize_param_name(name: str) -> str:
    """把形状里的 camelCase 属性名归一成 snake_case 参数名。

    ``percentGaugeRR`` → ``percent_gauge_rr``; ``hysteresis`` → ``hysteresis``。

    这是 :func:`matrag.domain.rules._camel_to_snake` 的同款算法 (连续大写串按
    一个词处理)。本模块**不跨模块 import 私有符号** (那会把两个模块焊死,
    私有符号也没有兼容性承诺), 而是独立复刻, 并在
    ``tests/test_domain_ontology.py`` 里断言两者对真实 shape 的全部属性名
    结果完全一致, 防止算法漂移。

    归一化的意义: SHACL ``sh:path`` 写的是 ``contactResistance``, 而规则
    ``test.given`` 的事实键写的是 ``contact_resistance`` —— 不归一就会在本体
    里凭空多出一个属性, 与约束求值层对不上, 同一物理量被拆成两个本体属性。
    """
    spaced = _CAMEL_BOUNDARY_RE.sub("_", name)
    return _CAMEL_ACRONYM_RE.sub("_", spaced).lower()


def _local_name(uri: str) -> str:
    """取 IRI 的 local name (``https://…#Protection`` → ``Protection``)。"""
    return uri.rsplit("#", 1)[-1] if "#" in uri else uri.rsplit("/", 1)[-1]


# --------------------------------------------------------------------------- #
# 值对象
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OntologyIssue:
    """一条本体质量门问题 (类型见模块级 ``ISSUE_*`` 常量)。

    Attributes:
        kind: 问题类型, 例如 :data:`ISSUE_UNIT_CONFLICT`。
        detail: 人类可读的中文说明, 已含具体参数名与单位取值。
        involved: 涉及的规则 id / 参数名 (已排序, 便于比对与断言)。
    """

    kind: str
    detail: str
    involved: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict。"""
        return {
            "kind": self.kind,
            "detail": self.detail,
            "involved": list(self.involved),
        }


@dataclass(frozen=True)
class OntologyClass:
    """一个本体类 —— 被测对象类型 (subject 轴) 或知识主题 (category 轴)。

    Attributes:
        name: 短名 (``Protection`` / ``protection``), 也是查询用的键。
        uri: 完整 IRI (两轴分别在 :data:`POWER_NS` / :data:`CATEGORY_NS` 下)。
        axis: :data:`AXIS_SUBJECT` 或 :data:`AXIS_CATEGORY`。
        source_uri: 该类在规则文件里出现时的原始标识 (subject 轴为 shape
            里的 IRI; category 轴就是规则字段的字面值)。
        comment: 中文说明。
        rule_ids: 挂在这个类上的规则 id (已排序)。
    """

    name: str
    uri: str
    axis: str
    source_uri: str
    comment: str
    rule_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict。"""
        return {
            "name": self.name,
            "uri": self.uri,
            "axis": self.axis,
            "source_uri": self.source_uri,
            "comment": self.comment,
            "rule_ids": list(self.rule_ids),
        }


@dataclass(frozen=True)
class OntologyProperty:
    """一个量纲参数 (本体属性)。

    Attributes:
        name: 归一后的参数名 (snake_case), 查询用的键。所有来源
            (``sh:path`` / ``derive.inputs`` / ``derive.output`` /
            ``test.given`` 键) 一律经 :func:`normalize_param_name` 归一,
            同一物理量只对应一个属性。
        uri: 完整 IRI。
        origins: 来源标签集合 (:data:`ORIGIN_*`)。多个来源说明同一物理量在
            多处被独立声明过, 是"同义归并"留下的证据。
        declared_names: 原始写法 (camelCase 的 shape path、snake 的事实键),
            归并前的原始形态全部保留, 便于回溯。
        unit: 规范单位; 未声明时为 ``""`` (**可见, 不丢弃**)。存在冲突时取
            字典序最小者作为规范值, 完整集合见 ``units``。
        units: 所有规则声明过的单位字符串 (已排序, 无声明时为空元组)。
        range_iri: 由样例取值推断的 XSD 值域。
        domain_classes: 该参数出现于哪些被测对象类型下 (写进 ``rdfs:domain``)。
        categories: 该参数出现于哪些知识主题下。
        rule_ids: 引用该参数的规则 id (输入或产出), 已排序。
    """

    name: str
    uri: str
    origins: tuple[str, ...] = ()
    declared_names: tuple[str, ...] = ()
    unit: str = ""
    units: tuple[str, ...] = ()
    range_iri: str = ""
    domain_classes: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()

    @property
    def has_unit(self) -> bool:
        """是否声明了单位。"""
        return bool(self.unit)

    @property
    def unit_conflict(self) -> bool:
        """是否存在单位冲突 (声明了多于一个不同单位)。"""
        return len(self.units) > 1

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict。"""
        return {
            "name": self.name,
            "uri": self.uri,
            "origins": list(self.origins),
            "declared_names": list(self.declared_names),
            "unit": self.unit,
            "units": list(self.units),
            "range_iri": self.range_iri,
            "domain_classes": list(self.domain_classes),
            "categories": list(self.categories),
            "rule_ids": list(self.rule_ids),
        }


@dataclass(frozen=True)
class OntologyRuleLink:
    """一条规则与本体的关联 (规则 ↔ 类 / 分类 / 参数)。

    Attributes:
        rule_id: 规则 id。
        uri: 规则个体 IRI。
        kind: ``derive`` / ``constraint``。
        category: 知识主题 (空串表示规则未声明 category)。
        scope: 规则适用范围 (原样保留)。
        statement: 规则陈述原文。
        class_names: 该规则引用的被测对象类型短名 (来自 ``sh:targetClass``)。
        consumes: 消费 (输入) 的参数 (已归一为 snake_case)。
        produces: 产出名 (已归一)。约束型为 ``violation`` —— 那是"是否被
            违反"的布尔判定, **不建成量纲属性**, 只在这里与图的
            ``power:verdict`` 上体现。
        shacl_paths: shape 中显式声明的 ``sh:path`` 原始写法。
    """

    rule_id: str
    uri: str
    kind: str
    category: str
    scope: str
    statement: str
    class_names: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    shacl_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的 dict。"""
        return {
            "rule_id": self.rule_id,
            "uri": self.uri,
            "kind": self.kind,
            "category": self.category,
            "scope": self.scope,
            "statement": self.statement,
            "class_names": list(self.class_names),
            "consumes": list(self.consumes),
            "produces": list(self.produces),
            "shacl_paths": list(self.shacl_paths),
        }


# --------------------------------------------------------------------------- #
# 本体
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, eq=False)
class PowerOntology:
    """电力域本体 —— 可查询、可序列化、可做质量门。

    由 :func:`build_ontology` 构造, 构造后不可变。所有查询都返回新 list/dict,
    调用方无法反过来污染本体。

    ``eq=False`` 的原因: 本体内部是 dict/set, 哈希无意义, 且"确定性"应由
    :meth:`to_dict` / :meth:`to_turtle` 的逐位输出来判断, 而不是对象相等。
    """

    # --- 公共数据 (按名称索引的只读视图) --- #
    classes_by_name: dict[str, OntologyClass]
    properties_by_name: dict[str, OntologyProperty]
    links_by_rule: dict[str, OntologyRuleLink]
    subject_class_names: tuple[str, ...]
    category_names: tuple[str, ...]

    # --- 构建期证据 (供 validate / 单规则单位查询, 不参与对外主视图) --- #
    parse_failures: tuple[tuple[str, str], ...] = ()
    unit_by_rule_param: dict[tuple[str, str], str] = field(
        default_factory=dict, repr=False, compare=False
    )
    rules_declaring_unit: dict[str, tuple[str, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )
    empty_unit_params: tuple[str, ...] = ()
    derive_inputs: dict[str, tuple[str, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )
    given_keys: dict[str, tuple[str, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )

    _turtle_cache: str | None = field(
        default=None, repr=False, compare=False
    )

    # ------------------------------------------------------------ 查询 -- #

    def classes(self) -> dict[str, OntologyClass]:
        """全部类 (被测对象类型 + 知识主题), 键为短名。"""
        return dict(self.classes_by_name)

    def properties(self) -> dict[str, OntologyProperty]:
        """全部量纲参数, 键为归一后的参数名。"""
        return dict(self.properties_by_name)

    def rules(self) -> dict[str, OntologyRuleLink]:
        """全部规则的本体关联, 键为 ``rule_id``。"""
        return dict(self.links_by_rule)

    def subject_classes(self) -> dict[str, OntologyClass]:
        """仅被测对象类型轴 (来自 ``sh:targetClass``)。"""
        return {
            name: cls
            for name, cls in self.classes_by_name.items()
            if cls.axis == AXIS_SUBJECT
        }

    def category_classes(self) -> dict[str, OntologyClass]:
        """仅知识主题轴 (来自 ``rule.category``)。"""
        return {
            name: cls
            for name, cls in self.classes_by_name.items()
            if cls.axis == AXIS_CATEGORY
        }

    def categories(self) -> dict[str, OntologyClass]:
        """知识主题轴 → 类定义 (等价于 :meth:`category_classes`)。"""
        return self.category_classes()

    def rules_for_class(self, class_name: str) -> list[str]:
        """挂在某个类上的规则 id (已排序)。

        ``class_name`` 可以是被测对象类型短名 (``Protection``), 也可以是
        知识主题短名 (``protection``) —— 两轴共用一张查询表, 因为短名在
        构建期就被强制不允许跨轴重名 (重名直接 :class:`OntologyBuildError`),
        且可用 :meth:`classes()[name].axis` 区分。

        未知短名返回空列表 —— 这是"查询无结果"的正常语义, 与"构建降级"
        无关 (构建期空本体会直接抛 :class:`OntologyBuildError`)。
        """
        cls = self.classes_by_name.get(class_name)
        if cls is None:
            return []
        return list(cls.rule_ids)

    def rules_for_category(self, category: str) -> list[str]:
        """某个知识主题下的规则 id (已排序); 未知主题返回空列表。"""
        return self.rules_for_class(category)

    def params_of(self, rule_id: str) -> list[str]:
        """规则触及的全部参数 (消费 ∪ 产出, 已排序去重)。

        Raises:
            KeyError: ``rule_id`` 不存在。
        """
        link = self.links_by_rule[rule_id]
        return sorted(set(link.consumes) | set(link.produces))

    def consumes_of(self, rule_id: str) -> list[str]:
        """规则消费的 (输入) 参数, 已排序。

        Raises:
            KeyError: ``rule_id`` 不存在。
        """
        return sorted(self.links_by_rule[rule_id].consumes)

    def produces_of(self, rule_id: str) -> list[str]:
        """规则产出的 (输出) 参数, 已排序。

        Raises:
            KeyError: ``rule_id`` 不存在。
        """
        return sorted(self.links_by_rule[rule_id].produces)

    def link_of(self, rule_id: str) -> OntologyRuleLink:
        """取单条规则的本体关联。

        Raises:
            KeyError: ``rule_id`` 不存在。
        """
        return self.links_by_rule[rule_id]

    def unit_of(self, param: str) -> str:
        """取参数的规范单位; 未声明时返回空串 ``""`` (**不丢弃该参数**)。

        查找是**归一化容错**的: ``unit_of("contactResistance")`` 与
        ``unit_of("contact_resistance")`` 指向同一个属性 (属性表的键一律是
        :func:`normalize_param_name` 之后的 snake_case 名)。

        存在单位冲突时返回字典序最小者作为规范值。冲突不会被吞掉: 完整单位
        集合见 :meth:`units_of`, 并由 :meth:`validate` 报成
        :data:`ISSUE_UNIT_CONFLICT`。未知参数同样返回 ``""``。
        """
        prop = self._property(param)
        return "" if prop is None else prop.unit

    def units_of(self, param: str) -> list[str]:
        """取参数被声明过的**全部**单位 (已排序, 可能多于一个)。"""
        prop = self._property(param)
        return [] if prop is None else list(prop.units)

    def unit_of_rule(self, rule_id: str, param: str) -> str:
        """取**该条规则自己**为某参数声明的单位; 未声明则返回空串。

        这是单位冲突的原始证据来源: 冲突参数在不同规则上会返回不同的单位。
        未知参数或该规则没声明时返回空串。

        Raises:
            KeyError: ``rule_id`` 不存在。
        """
        if rule_id not in self.links_by_rule:
            raise KeyError(rule_id)
        return self.unit_by_rule_param.get(
            (rule_id, normalize_param_name(param)), ""
        )

    def _property(self, param: str) -> OntologyProperty | None:
        """按归一化名查属性 (先精确后归一, 允许调用方传 camelCase)。"""
        prop = self.properties_by_name.get(param)
        if prop is not None:
            return prop
        return self.properties_by_name.get(normalize_param_name(param))

    def properties_without_unit(self) -> list[str]:
        """所有未声明单位的参数名 (已排序) —— 单位缺失的完整名单。"""
        return sorted(
            name
            for name, prop in self.properties_by_name.items()
            if not prop.unit
        )

    def properties_with_unit_conflict(self) -> list[str]:
        """所有存在单位冲突的参数名 (已排序)。"""
        return sorted(
            name
            for name, prop in self.properties_by_name.items()
            if prop.unit_conflict
        )

    # ---------------------------------------------------------- 质量门 -- #

    def validate(self) -> list[OntologyIssue]:
        """本体质量门: 检出规则集自相矛盾之处。

        Returns:
            issue 列表, 按 ``(kind, detail, involved)`` 排序保证确定性;
            **空列表 = 通过**。

        检出的类型 (常量见模块顶部):

        - :data:`ISSUE_UNIT_CONFLICT` —— 同一参数在不同规则里单位不同;
        - :data:`ISSUE_UNIT_MISSING` —— 被 ≥2 条规则引用的参数没声明单位;
        - :data:`ISSUE_UNCLASSIFIED_CONSTRAINT` —— 约束型规则没有 targetClass;
        - :data:`ISSUE_RULE_INCONSISTENT` —— ``derive.inputs`` 与
          ``test.given`` 键集不一致;
        - :data:`ISSUE_SHAPE_UNPARSEABLE` —— shape 不是合法 Turtle。
        """
        issues: list[OntologyIssue] = []
        issues.extend(self._validate_shapes())
        issues.extend(self._validate_units())
        issues.extend(self._validate_consistency())
        issues.sort(key=lambda i: (i.kind, i.detail, i.involved))
        return issues

    def issues_by_kind(self) -> dict[str, list[OntologyIssue]]:
        """按 ``kind`` 分组的质量门结果 (组内顺序与 :meth:`validate` 一致)。"""
        grouped: dict[str, list[OntologyIssue]] = {}
        for issue in self.validate():
            grouped.setdefault(issue.kind, []).append(issue)
        return grouped

    def _validate_shapes(self) -> list[OntologyIssue]:
        """检出 shape 解析失败 / 约束型规则无法归类。"""
        issues = [
            OntologyIssue(
                kind=ISSUE_SHAPE_UNPARSEABLE,
                detail=(
                    f"规则 {rule_id} 的 SHACL shape 无法解析为 Turtle: {error}"
                ),
                involved=(rule_id,),
            )
            for rule_id, error in self.parse_failures
        ]
        for link in self.links_by_rule.values():
            if link.kind == "constraint" and not link.class_names:
                issues.append(
                    OntologyIssue(
                        kind=ISSUE_UNCLASSIFIED_CONSTRAINT,
                        detail=(
                            f"约束型规则 {link.rule_id} 缺少 sh:targetClass, "
                            f"挂不到任何被测对象类型上 "
                            f"(category={link.category!r})"
                        ),
                        involved=(link.rule_id,),
                    )
                )
        return issues

    def _validate_units(self) -> list[OntologyIssue]:
        """检出单位冲突 / 单位缺失。"""
        issues: list[OntologyIssue] = []
        for name in sorted(self.properties_by_name):
            prop = self.properties_by_name[name]
            owners = self.rules_declaring_unit.get(name, ())
            if prop.unit_conflict:
                issues.append(
                    OntologyIssue(
                        kind=ISSUE_UNIT_CONFLICT,
                        detail=(
                            f"参数 {name} 在不同规则里声明了不同单位 "
                            f"{list(prop.units)}, 且无显式换算声明"
                        ),
                        involved=(name, *owners),
                    )
                )
            elif not prop.unit and len(prop.rule_ids) >= UNIT_MISSING_MIN_RULES:
                declared_empty = name in self.empty_unit_params
                how = (
                    "variables 声明为空串"
                    if declared_empty
                    else "variables 未声明该参数"
                )
                issues.append(
                    OntologyIssue(
                        kind=ISSUE_UNIT_MISSING,
                        detail=(
                            f"参数 {name} 被 {len(prop.rule_ids)} 条规则引用, "
                            f"但 {how} (unit=\"\")"
                        ),
                        involved=(name, *prop.rule_ids),
                    )
                )
        return issues

    def _validate_consistency(self) -> list[OntologyIssue]:
        """检出 ``derive.inputs`` 与 ``test.given`` 不自洽的规则。"""
        issues: list[OntologyIssue] = []
        for rule_id in sorted(self.derive_inputs):
            inputs = set(self.derive_inputs[rule_id])
            given = set(self.given_keys.get(rule_id, ()))
            if inputs == given:
                continue
            missing = sorted(inputs - given)
            extra = sorted(given - inputs)
            parts = []
            if missing:
                parts.append(f"inputs 中有而 given 中无: {missing}")
            if extra:
                parts.append(f"given 中有而 inputs 中无: {extra}")
            issues.append(
                OntologyIssue(
                    kind=ISSUE_RULE_INCONSISTENT,
                    detail=(
                        f"规则 {rule_id} 的 derive.inputs 与 test.given "
                        f"键集不一致; " + "; ".join(parts)
                    ),
                    involved=(rule_id, *missing, *extra),
                )
            )
        return issues

    # ------------------------------------------------------------ 导出 -- #

    def stats(self) -> dict[str, Any]:
        """本体统计摘要 (纯数据, 无时间戳, 可 JSON 序列化, 确定性)。

        ``issue_counts`` 来自 :meth:`validate`, 所以 **stats 自带真实的质量门
        结果**, CLI / CI 可以直接拿它判断本体是否健康, 不必再单独调 validate。
        """
        return self._stats_with(self.validate())

    def _stats_with(self, issues: Sequence[OntologyIssue]) -> dict[str, Any]:
        """给定质量门结果时的统计 (避免 to_dict 里重复跑一遍 validate)。"""
        props = self.properties_by_name
        links = self.links_by_rule
        return {
            "ontology_name": ONTOLOGY_NAME,
            "namespace": POWER_NS,
            "category_namespace": CATEGORY_NS,
            "rule_count": len(links),
            "derive_rule_count": sum(
                1 for x in links.values() if x.kind == "derive"
            ),
            "constraint_rule_count": sum(
                1 for x in links.values() if x.kind == "constraint"
            ),
            "class_count": len(self.classes_by_name),
            "subject_class_count": len(self.subject_class_names),
            "category_count": len(self.category_names),
            "property_count": len(props),
            "property_with_unit": sum(1 for p in props.values() if p.unit),
            "property_without_unit": sum(1 for p in props.values() if not p.unit),
            "property_with_unit_conflict": sum(
                1 for p in props.values() if p.unit_conflict
            ),
            "property_from_shacl_path": sum(
                1 for p in props.values() if ORIGIN_SHACL_PATH in p.origins
            ),
            "property_without_rule": sum(1 for p in props.values() if not p.rule_ids),
            "rule_with_class": sum(1 for x in links.values() if x.class_names),
            "rule_without_category": sum(
                1 for x in links.values() if not x.category
            ),
            "shacl_path_declaration": sum(len(x.shacl_paths) for x in links.values()),
            "shape_parse_failure": len(self.parse_failures),
            "issue_count": len(issues),
            "issue_counts": {
                kind: sum(1 for i in issues if i.kind == kind)
                for kind in sorted({i.kind for i in issues})
            },
        }

    def to_dict(self) -> dict[str, Any]:
        """导出完整本体为可 JSON 序列化的 dict (确定性, 无时间戳)。"""
        issues = self.validate()
        return {
            "ontology_name": ONTOLOGY_NAME,
            "version": ONTOLOGY_VERSION,
            "namespace": POWER_NS,
            "category_namespace": CATEGORY_NS,
            "stats": self._stats_with(issues),
            "classes": [
                self.classes_by_name[name].to_dict()
                for name in sorted(self.classes_by_name)
            ],
            "properties": [
                self.properties_by_name[name].to_dict()
                for name in sorted(self.properties_by_name)
            ],
            "rules": [
                self.links_by_rule[rule_id].to_dict()
                for rule_id in sorted(self.links_by_rule)
            ],
            "issues": [issue.to_dict() for issue in issues],
            "shape_parse_failures": [
                {"rule_id": rule_id, "error": error}
                for rule_id, error in self.parse_failures
            ],
        }

    def to_json(self, indent: int = 2) -> str:
        """导出为确定性的 JSON 文本 (``sort_keys=True``, UTF-8 原样输出)。"""
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
        )

    def to_turtle(self) -> str:
        """序列化为 OWL/RDF Turtle (确定性: 同输入逐位同输出)。

        产出路径:

        1. **semantica 优先** —— 类 / 属性交给 ``semantica.ontology.OWLGenerator``
           生成 OWL 骨架 (``owl:Class`` / ``owl:DatatypeProperty`` /
           ``rdfs:label`` / ``rdfs:comment`` / ``rdfs:domain`` / ``rdfs:range``);
        2. rdflib 补上 semantica 通用 OWL 模型**表达不了**的电力域词汇
           (单位、规则个体、规则↔类/分类/参数的关联);
        3. 统一由 rdflib 序列化, 并做"必须能被 rdflib 重新解析"的自检。

        Raises:
            OntologyBuildError: 骨架生成失败、图自检失败或序列化为空 (fail-fast,
                绝不返回空本体)。
        """
        if self._turtle_cache is None:
            object.__setattr__(self, "_turtle_cache", self._render_turtle())
        return self._turtle_cache

    def _render_turtle(self) -> str:
        """执行 :meth:`to_turtle` 的第 1/2/3 步。"""
        graph = _build_owl_skeleton(self)
        _annotate_graph(graph, self)
        text = graph.serialize(format="turtle")
        if not text.strip():
            raise OntologyBuildError("本体图序列化结果为空 (fail-fast)")
        try:
            Graph().parse(data=text, format="turtle")
        except Exception as exc:  # pragma: no cover - 防御性
            raise OntologyBuildError(
                f"生成的 Turtle 无法被 rdflib 重新解析: {exc}"
            ) from exc
        return text


# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #


def _extract_shape(shape_text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """从 SHACL Turtle 中抽出 ``(targetClass 短名, sh:path 原始 IRI)``。

    用 ``triples((None, sh:path, None))`` 而不是只找顶层, 因为 ``sh:property``
    的值通常是嵌套 blank node, 只查直接 subject 会漏掉。

    Raises:
        OntologyBuildError: shape 不是合法 Turtle (调用方转成 issue 留痕)。
    """
    graph = Graph()
    try:
        graph.parse(data=shape_text, format="turtle")
    except Exception as exc:
        raise OntologyBuildError(f"SHACL shape 不是合法 Turtle: {exc}") from exc
    targets = sorted(
        {_local_name(str(o)) for o in graph.objects(None, _SH_TARGET_CLASS)}
    )
    paths = sorted(
        {str(o) for _s, _p, o in graph.triples((None, _SH_PATH, None))}
    )
    return tuple(targets), tuple(paths)


def _infer_range(values: Iterable[Any]) -> str:
    """从样例取值推断 XSD 值域; 多类型混用时退回 ``xsd:string``。"""
    kinds: set[str] = set()
    for value in values:
        if isinstance(value, bool):
            kinds.add("boolean")
        elif isinstance(value, (int, float)):
            kinds.add("decimal")
        else:
            kinds.add("string")
    if len(kinds) == 1:
        return str(XSD[next(iter(kinds))])
    return str(XSD.string)


def build_ontology(
    rules: Sequence[DomainRule] | None = None,
) -> PowerOntology:
    """从规则集构建电力域本体 (纯数据驱动, 无任何按 rule_id 的分支)。

    Args:
        rules: 规则列表; ``None`` 表示调用
            :func:`matrag.domain.rules_loader.load_rules` 重新加载。

    Returns:
        构造完成的 :class:`PowerOntology`。

    Raises:
        OntologyBuildError: ``rules`` 为空 (严禁静默返回空本体), 或某个类名
            同时落在 subject 轴与 category 轴 (无法消歧, 属构建期硬错误)。
    """
    if rules is None:
        rules = load_rules()
    rule_list = list(rules)
    if not rule_list:
        raise OntologyBuildError(
            "规则集为空, 拒绝构建空本体 (fail-fast); 请检查 rules.yaml 路径与内容"
        )

    classes: dict[str, OntologyClass] = {}
    links: dict[str, OntologyRuleLink] = {}
    origins: dict[str, set[str]] = {}
    declared_names: dict[str, set[str]] = {}
    unit_by_param: dict[str, dict[str, str]] = {}
    unit_by_rule_param: dict[tuple[str, str], str] = {}
    empty_unit: dict[str, set[str]] = {}
    samples: dict[str, list[Any]] = {}
    param_rules: dict[str, set[str]] = {}
    param_domains: dict[str, set[str]] = {}
    param_categories: dict[str, set[str]] = {}
    class_rules: dict[str, set[str]] = {}
    subject_names: set[str] = set()
    category_names: set[str] = set()
    derive_inputs: dict[str, tuple[str, ...]] = {}
    given_keys: dict[str, tuple[str, ...]] = {}
    parse_failures: list[tuple[str, str]] = []

    def record(
        name: str, rule_id: str, origin: str, raw: str, sample: Any = None
    ) -> None:
        """把一个参数的来源证据记进累加器 (内部闭包, 避免到处传六个字典)。"""
        origins.setdefault(name, set()).add(origin)
        declared_names.setdefault(name, set()).add(raw)
        param_rules.setdefault(name, set()).add(rule_id)
        if sample is not None:
            samples.setdefault(name, []).append(sample)

    for rule in rule_list:
        rule_id = rule.rule_id
        targets: tuple[str, ...] = ()
        shacl_paths: tuple[str, ...] = ()

        if rule.is_constraint:
            try:
                targets, shacl_paths = _extract_shape(
                    str(rule.constraint.get("shape", ""))
                )
            except OntologyBuildError as exc:
                # 留痕不吞错: 该规则进不了 subject 轴, 但 issue 会报出来。
                parse_failures.append((rule_id, str(exc)))
            for target in targets:
                subject_names.add(target)
                class_rules.setdefault(target, set()).add(rule_id)
            for raw_path in shacl_paths:
                local = _local_name(raw_path)
                # 键取归一化后的名字 (与 test.given 的事实键同一套命名),
                # declared_names 里保留原始 camelCase 写法以便回溯。
                record(
                    normalize_param_name(local),
                    rule_id,
                    ORIGIN_SHACL_PATH,
                    local,
                )

        if rule.category:
            category_names.add(rule.category)
            class_rules.setdefault(rule.category, set()).add(rule_id)
        consumes = tuple(sorted({normalize_param_name(p) for p in rule.input_params}))
        produces = (normalize_param_name(rule.output_key),)
        given_map = {normalize_param_name(k): v for k, v in rule.given.items()}
        raw_given = {normalize_param_name(k): k for k in rule.given}
        given = tuple(sorted(given_map))
        given_keys[rule_id] = given
        if rule.is_derive:
            derive_inputs[rule_id] = consumes
        # 只给真正的量纲参数挂 domain / category; 约束型的 violation 判定
        # 不是属性, 不进这两个轴。
        dimensional = consumes + (
            produces if rule.is_derive else ()
        )
        for param in dimensional:
            if targets:
                param_domains.setdefault(param, set()).update(targets)
            if rule.category:
                param_categories.setdefault(param, set()).add(rule.category)

        for param in consumes:
            origin = ORIGIN_DERIVE_INPUT if rule.is_derive else ORIGIN_GIVEN_KEY
            record(
                param,
                rule_id,
                origin,
                raw_given.get(param, param),
                sample=given_map.get(param),
            )
        if rule.is_derive:
            # 只有公式型规则的输出才是**量纲参数**。约束型规则的
            # ``violation`` 是"这条约束被违反"的布尔判定 (``rules_loader``
            # 里的 VIOLATION_KEY), 不是被测对象的物理量, 建成属性反而会让
            # 单位质量门对着一个 xsd:boolean 报警。它仍然保留在
            # ``OntologyRuleLink.produces`` 与图上的 ``power:verdict`` 里。
            record(
                produces[0],
                rule_id,
                ORIGIN_DERIVE_OUTPUT,
                produces[0],
                sample=rule.expected_value,
            )

        # variables 是唯一的单位来源; 空串视为"声明了但没写单位"。
        for raw_param, raw_unit in rule.variables.items():
            param = normalize_param_name(raw_param)
            unit = str(raw_unit).strip()
            if not unit:
                empty_unit.setdefault(param, set()).add(rule_id)
                continue
            unit_by_param.setdefault(param, {})[rule_id] = unit
            unit_by_rule_param[(rule_id, param)] = unit

        links[rule_id] = OntologyRuleLink(
            rule_id=rule_id,
            uri=f"{POWER_NS}Rule_{rule_slug(rule_id)}",
            kind=rule.kind,
            category=rule.category,
            scope=rule.scope,
            statement=rule.statement,
            class_names=targets,
            consumes=consumes,
            produces=produces,
            shacl_paths=shacl_paths,
        )

    overlap = sorted(subject_names & category_names)
    if overlap:  # pragma: no cover - 真实数据无重叠, 属防御性硬错误
        raise OntologyBuildError(
            f"类名 {overlap} 同时出现在 subject 轴 (sh:targetClass) 与 "
            f"category 轴 (rule.category), 无法消歧; 请改名或拆分命名空间"
        )

    for name in sorted(subject_names | category_names):
        axis = AXIS_SUBJECT if name in subject_names else AXIS_CATEGORY
        namespace = POWER_NS if axis == AXIS_SUBJECT else CATEGORY_NS
        classes[name] = OntologyClass(
            name=name,
            uri=f"{namespace}{name}",
            axis=axis,
            source_uri=f"{POWER_NS}{name}" if axis == AXIS_SUBJECT else name,
            comment=(
                "被测对象类型 (DUT 的类, 可实例化): 由约束型规则 SHACL shape "
                "的 sh:targetClass 抽取"
                if axis == AXIS_SUBJECT
                else "知识主题 (规则的分类维度): 来自 rule.category 字段, "
                "不可实例化, 只用于检索路由"
            ),
            rule_ids=tuple(sorted(class_rules.get(name, ()))),
        )

    props: dict[str, OntologyProperty] = {}
    for name in sorted(origins):
        uniq = sorted(set(unit_by_param.get(name, {}).values()))
        props[name] = OntologyProperty(
            name=name,
            uri=f"{POWER_NS}{name}",
            origins=tuple(sorted(origins[name])),
            declared_names=tuple(sorted(declared_names[name])),
            unit=uniq[0] if uniq else "",
            units=tuple(uniq),
            range_iri=_infer_range(samples.get(name, ())),
            domain_classes=tuple(sorted(param_domains.get(name, ()))),
            categories=tuple(sorted(param_categories.get(name, ()))),
            rule_ids=tuple(sorted(param_rules[name])),
        )

    return PowerOntology(
        classes_by_name=classes,
        properties_by_name=props,
        links_by_rule=links,
        subject_class_names=tuple(sorted(subject_names)),
        category_names=tuple(sorted(category_names)),
        parse_failures=tuple(parse_failures),
        unit_by_rule_param=unit_by_rule_param,
        rules_declaring_unit={
            param: tuple(sorted(by_rule))
            for param, by_rule in unit_by_param.items()
        },
        empty_unit_params=tuple(sorted(empty_unit)),
        derive_inputs=derive_inputs,
        given_keys=given_keys,
    )


# --------------------------------------------------------------------------- #
# 序列化 (semantica 优先 + rdflib 补齐电力域词汇)
# --------------------------------------------------------------------------- #


def _build_owl_skeleton(ontology: PowerOntology) -> Graph:
    """第 1 步: 交给 semantica ``OWLGenerator`` 生成 OWL 骨架。

    ``OWLGenerator`` 负责 "类/属性 → RDF 三元组" 的映射。这里显式给出每个
    实体 IRI, 避免它按默认 base 自行改名 (``NamespaceManager`` 默认会把
    ``power#Protection`` 改成 ``semantica.dev/ontology/Protection`` 之类)。

    Raises:
        OntologyBuildError: semantica 生成失败, 或产物无法被 rdflib 解析。
    """
    spec: dict[str, Any] = {
        "uri": POWER_NS,
        "name": ONTOLOGY_NAME,
        "version": ONTOLOGY_VERSION,
        "classes": [
            {
                "uri": cls.uri,
                "name": cls.name,
                "label": cls.name,
                "comment": cls.comment,
            }
            for cls in ontology.classes_by_name.values()
        ],
        "properties": [
            {
                "uri": prop.uri,
                "name": prop.name,
                "label": prop.name,
                "type": "datatype",
                "range": prop.range_iri,
                "domain": [
                    ontology.classes_by_name[name].uri
                    for name in prop.domain_classes
                    if name in ontology.classes_by_name
                ],
            }
            for prop in ontology.properties_by_name.values()
        ],
    }
    try:
        turtle = OWLGenerator().generate_owl(spec, format="turtle")
    except Exception as exc:
        raise OntologyBuildError(
            f"semantica OWLGenerator 生成 OWL 骨架失败: {exc}"
        ) from exc
    graph = Graph()
    try:
        graph.parse(data=turtle, format="turtle")
    except Exception as exc:  # pragma: no cover - 防御性
        raise OntologyBuildError(
            f"semantica 生成的 OWL 骨架无法被 rdflib 解析: {exc}"
        ) from exc
    graph.bind("power", Namespace(POWER_NS))
    graph.bind("cat", Namespace(CATEGORY_NS))
    return graph


def _annotate_graph(graph: Graph, ontology: PowerOntology) -> None:
    """第 2 步: 补上 semantica 通用 OWL 模型表达不了的电力域词汇。

    - 参数的来源 / 原始写法 / 单位 / 所属知识主题;
    - 规则个体 (``ruleId`` / ``statement`` / ``scope`` / ``ruleKind``);
    - 规则 ↔ 被测对象类型 / 知识主题 / 参数 的双向关联。

    未声明单位的参数**照样进图** (作为 ``owl:DatatypeProperty`` + label),
    只是不写 ``hasUnit`` 三元组 —— 缺失由 ``validate()`` 报成
    :data:`ISSUE_UNIT_MISSING`, 不是从本体里抹掉。
    """
    for prop in ontology.properties_by_name.values():
        subject = URIRef(prop.uri)
        graph.add((subject, RDF.type, OWL.DatatypeProperty))
        graph.add((subject, RDFS.label, Literal(prop.name)))
        for origin in prop.origins:
            graph.add((subject, _DECLARED_FROM, Literal(origin)))
        for raw in prop.declared_names:
            graph.add((subject, _DECLARED_NAMES, Literal(raw)))
        if prop.unit:
            graph.add((subject, _HAS_UNIT, Literal(prop.unit)))
        if prop.unit_conflict:
            for unit in prop.units:
                graph.add((subject, _DECLARED_UNITS, Literal(unit)))
        for category in prop.categories:
            graph.add((subject, _IN_CATEGORY, URIRef(CATEGORY_NS + category)))

    for link in ontology.links_by_rule.values():
        subject = URIRef(link.uri)
        graph.add((subject, RDF.type, OWL.NamedIndividual))
        graph.add((subject, _RULE_ID, Literal(link.rule_id)))
        graph.add((subject, RDFS.label, Literal(link.rule_id)))
        graph.add((subject, _KIND, Literal(link.kind)))
        graph.add((subject, _STATEMENT, Literal(link.statement)))
        if link.scope:
            graph.add((subject, _SCOPE, Literal(link.scope)))
        if link.category:
            graph.add(
                (subject, _IN_CATEGORY, URIRef(CATEGORY_NS + link.category))
            )
        for cls in link.class_names:
            graph.add((subject, _CONSTRAINS, URIRef(POWER_NS + cls)))
        for param in link.consumes:
            param_uri = URIRef(POWER_NS + param)
            graph.add((param_uri, _HAS_RULE, subject))
            graph.add((subject, _CONSUMES, param_uri))
        if link.kind == "derive":
            for param in link.produces:
                param_uri = URIRef(POWER_NS + param)
                graph.add((param_uri, _HAS_RULE, subject))
                graph.add((subject, _PRODUCES, param_uri))
        else:
            # 约束型的产出是"是否被违反"的布尔判定, 不是量纲参数, 因此写成
            # 字面量, 而不是指向一个根本没建出来的属性 (避免悬空引用)。
            graph.add((subject, _VERDICT, Literal(link.produces[0])))


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #


def write_ontology(
    out_dir: Path | str | None = None,
    rules: Sequence[DomainRule] | None = None,
) -> dict[str, Any]:
    """把本体写成 ``power_ontology.ttl`` + ``power_ontology.json``。

    Args:
        out_dir: 输出目录; 默认 :data:`DEFAULT_GENERATED_DIR`
            (``data/domain/generated``)。
        rules: 规则列表; ``None`` 表示重新加载。

    Returns:
        统计 dict: ``turtle`` / ``json`` / ``out_dir`` 三个路径, 外加完整的
        ``stats`` 与 ``issue_counts``, 供 CLI 打印与测试断言。

    Raises:
        OntologyBuildError: 规则集为空或序列化失败 (fail-fast)。
    """
    ontology = build_ontology(rules)
    target_dir = Path(out_dir) if out_dir is not None else DEFAULT_GENERATED_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    ttl_path = target_dir / TTL_FILENAME
    json_path = target_dir / JSON_FILENAME
    ttl_path.write_text(ontology.to_turtle(), encoding="utf-8")
    json_path.write_text(ontology.to_json(), encoding="utf-8")

    stats = ontology.stats()
    return {
        "turtle": str(ttl_path),
        "json": str(json_path),
        "out_dir": str(target_dir),
        "stats": stats,
        "issue_counts": stats["issue_counts"],
    }
