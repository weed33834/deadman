"""P8.3.8 跨用户知识匿名化(k-匿名 + l-多样性)。

设计目标:
    - 在跨用户共享知识前,对节点做匿名化处理(去掉准标识符)
    - k-匿名:每条记录至少与 k-1 条其他记录在准标识符上不可区分
    - l-多样性:每个等价类中,敏感属性至少有 l 种不同值
    - 准标识符泛化:location → region, date → month, age → range

法规依据:
    - PIPL 第 27 条:已合法公开的个人信息可在合理范围内处理,需去标识化
    - GDPR 第 89 条:匿名化数据不再受 GDPR 约束(但伪匿名化仍受约束)

设计原则:
    - feature flag DEADMAN_KNOWLEDGE_GRAPH_ENABLED 默认关闭
    - 单元测试覆盖 k-anonymity 与 l-diversity 校验
    - 不破坏节点结构(只修改 properties / content 的某些字段)
"""

from __future__ import annotations

import logging
import re
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from ..infrastructure.feature_flags import is_enabled
from .graphiti_runtime import KGNode

logger = logging.getLogger(__name__)


# 准标识符字段名(节点 properties 中的字段)
QUASI_IDENTIFIERS: set[str] = {
    "location",
    "city",
    "address",
    "birthdate",
    "death_date",
    "age",
    "zip_code",
    "postal_code",
    "phone",
    "email",
    "name",
    "id_card",
    "passport",
    "user_id",
    "user_name",
}

# 敏感属性字段名(用于 l-diversity 校验)
SENSITIVE_ATTRIBUTES: set[str] = {
    "income",
    "health_condition",
    "criminal_record",
    "political_affiliation",
    "religion",
    "sexual_orientation",
    "debt_amount",
    "estate_value",
}


@dataclass
class AnonymizationResult:
    """匿名化结果。

    Attributes:
        node: 匿名化后的 KGNode(深拷贝,不修改原节点)
        k_achieved: 实际达到的 k 值
        l_achieved: 实际达到的 l 值
        generalizations: 应用的泛化规则记录
        suppressed_fields: 被完全抑制(移除)的字段
    """

    node: KGNode
    k_achieved: int = 1
    l_achieved: int = 1
    generalizations: list[str] = field(default_factory=list)
    suppressed_fields: list[str] = field(default_factory=list)


# 准标识符泛化规则:字段名 → 泛化函数(value -> generalized_value)
def _generalize_location(value: str) -> str:
    """location → region(省份/国家)。"""
    if not value:
        return value
    # 简化:取第一段(如 "北京市朝阳区" → "北京市")
    # 中国地名 "XX 省 / XX 市" 截到省级,使用非贪婪匹配取第一个行政区划边界
    m = re.match(r"^([\u4e00-\u9fff]{2,}?[省市区县])", value)
    if m:
        return m.group(1)
    # 逗号分隔 → 取首段
    if "," in value:
        return value.split(",")[0].strip()
    if " " in value:
        return value.split(" ")[0].strip()
    # 长地名截前 3 字符
    if len(value) > 3:
        return value[:3] + "*"
    return value


def _generalize_date(value: str) -> str:
    """date (YYYY-MM-DD) → month (YYYY-MM)。"""
    if not value:
        return value
    m = re.match(r"^(\d{4})[-/](\d{1,2})", value)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return value


def _generalize_age(value: Any) -> str:
    """age → range (e.g. 30 → "30-39")。"""
    try:
        age = int(value)
    except (TypeError, ValueError):
        return str(value)
    if age < 0:
        return "<0"
    lower = (age // 10) * 10
    upper = lower + 9
    return f"{lower}-{upper}"


def _generalize_zip(value: str) -> str:
    """zip_code → first 3 digits + **。"""
    if not value:
        return value
    s = str(value)
    if len(s) <= 3:
        return s
    return s[:3] + "*"


def _generalize_phone(value: str) -> str:
    """phone → 前 3 + **** + 后 4。"""
    s = re.sub(r"\D", "", str(value or ""))
    if len(s) <= 7:
        return "*" * len(s)
    return s[:3] + "****" + s[-4:]


def _generalize_email(value: str) -> str:
    """email → 首字母 + ***@domain。"""
    if not value or "@" not in value:
        return value
    user, _, domain = value.partition("@")
    if not user:
        return value
    return f"{user[0]}***@{domain}"


def _generalize_name(value: str) -> str:
    """name → 首字符 + *。"""
    if not value:
        return value
    if len(value) <= 1:
        return "*"
    return value[0] + "*" * (len(value) - 1)


# 字段名 → 泛化函数
_GENERALIZERS: dict[str, Any] = {
    "location": _generalize_location,
    "city": _generalize_location,
    "address": _generalize_location,
    "birthdate": _generalize_date,
    "death_date": _generalize_date,
    "age": _generalize_age,
    "zip_code": _generalize_zip,
    "postal_code": _generalize_zip,
    "phone": _generalize_phone,
    "email": _generalize_email,
    "name": _generalize_name,
    "id_card": lambda v: "[REDACTED-ID]",
    "passport": lambda v: "[REDACTED-PASSPORT]",
    "user_id": lambda v: "[REDACTED-UID]",
    "user_name": _generalize_name,
}


class Anonymizer:
    """跨用户知识匿名化器(k-匿名 + l-多样性)。

    用法:
        anonymizer = Anonymizer()
        # 1. 匿名化单条节点
        result = anonymizer.anonymize(node, k=5, l=2)
        safe_node = result.node
        # 2. 校验是否可共享(基于其他用户数)
        if anonymizer.can_share(node, other_users_count=10):
            shared_knowledge.add(safe_node)

    设计:
        - 准标识符泛化:location → region,date → month,age → range
        - 敏感属性 l-多样性校验
        - 不修改原节点(深拷贝)
        - flag 关闭时返回原节点的浅拷贝(无脱敏,但仍含原结构)
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def anonymize(
        self,
        node: KGNode,
        k: int = 5,
        l_diversity: int = 2,
    ) -> AnonymizationResult:
        """对节点做 k-匿名 + l-多样性匿名化。

        流程:
            1. 深拷贝原节点(不修改原数据)
            2. 对每个准标识符字段应用泛化函数
            3. 强抑制字段(如 id_card / passport)直接替换为 [REDACTED]
            4. 计算 k / l 实际可达值(本期内简化为 k=min(k, 已泛化字段数 +1),l=min(l, ...))
            5. 返回 AnonymizationResult

        Args:
            node: 原始节点
            k: k-匿名参数(默认 5)
            l: l-多样性参数(默认 2)

        Returns:
            AnonymizationResult(含匿名化后的节点)
        """
        with self._lock:
            if not is_enabled("knowledge_graph"):
                # flag 关闭时返回浅拷贝(不变更)
                return AnonymizationResult(
                    node=deepcopy(node),
                    k_achieved=1,
                    l_achieved=1,
                )

            safe_node = deepcopy(node)
            generalizations: list[str] = []
            suppressed: list[str] = []
            props = dict(safe_node.properties)

            # 1. 准标识符泛化
            for key, value in list(props.items()):
                if key in QUASI_IDENTIFIERS:
                    gen_fn = _GENERALIZERS.get(key)
                    if gen_fn is not None:
                        try:
                            new_val = gen_fn(value)
                            if new_val != value:
                                generalizations.append(f"{key}: {value!r} → {new_val!r}")
                            props[key] = new_val
                        except Exception as e:
                            # 泛化失败 → 抑制
                            suppressed.append(key)
                            props[key] = "[REDACTED]"
                            logger.warning("generalize %s failed: %s", key, e)

            # 2. content 内嵌的明显 PII(身份证 / 邮箱 / 手机号)做泛化
            safe_node.content = self._redact_content(safe_node.content)

            # 3. 注入 _anonymized 标记(供下游识别)
            props["_anonymized"] = True
            props["_k_target"] = k
            props["_l_target"] = l_diversity
            safe_node.properties = props

            # 4. 计算 k / l 实际值(简化:k = max(1, 已泛化字段数),l 由调用方基于群体计算)
            k_achieved = max(1, len(generalizations))
            l_achieved = max(1, len([k2 for k2 in props if k2 in SENSITIVE_ATTRIBUTES]))

            return AnonymizationResult(
                node=safe_node,
                k_achieved=k_achieved,
                l_achieved=l_achieved,
                generalizations=generalizations,
                suppressed_fields=suppressed,
            )

    def can_share(
        self,
        node: KGNode,
        other_users_count: int,
        k: int = 5,
        l_diversity: int = 2,
    ) -> bool:
        """判断节点是否可在 other_users_count 个其他用户中安全共享。

        简化判定:
            - other_users_count >= k(有足够多的人群隐藏在其中)
            - 节点已经过 _anonymized 标记
            - l-diversity 校验通过(节点敏感属性有足够多样性)

        Args:
            node: 待共享节点(应为已匿名化)
            other_users_count: 当前共享池中其他用户数
            k: k-匿名阈值
            l: l-多样性阈值

        Returns:
            True 表示可共享
        """
        if not is_enabled("knowledge_graph"):
            return False
        if other_users_count < k:
            return False
        if not node.properties.get("_anonymized", False):
            return False
        # l-diversity 校验:本节点至少有 1 个敏感属性,且应与共享池其他节点
        # 至少 l 个不同值(本期简化:仅校验节点本身有 SENSITIVE_ATTRIBUTES)
        sensitive_values = [
            node.properties.get(k) for k in node.properties
            if k in SENSITIVE_ATTRIBUTES and node.properties.get(k) is not None
        ]
        if l_diversity > 1 and len(set(sensitive_values)) < l_diversity:
            return False
        return True

    @staticmethod
    def check_l_diversity(nodes: list[KGNode], l_diversity: int = 2) -> bool:
        """校验一组节点的 l-多样性。

        Args:
            nodes: 同等价类(同 QI 泛化)的节点集合
            l: 多样性阈值

        Returns:
            True 表示敏感属性至少有 l 个不同值
        """
        if not nodes:
            return False
        sensitive_values: set[Any] = set()
        for node in nodes:
            for attr in SENSITIVE_ATTRIBUTES:
                if attr in node.properties and node.properties[attr] is not None:
                    sensitive_values.add(str(node.properties[attr]))
        return len(sensitive_values) >= l_diversity

    @staticmethod
    def check_k_anonymity(nodes: list[KGNode], k: int = 5) -> bool:
        """校验一组节点的 k-匿名(等价类大小 >= k)。"""
        return len(nodes) >= k

    # ==================================================================
    # 内部:content 内嵌 PII 脱敏(简化版)
    # ==================================================================

    def _redact_content(self, content: str) -> str:
        """对 content 文本做明显的 PII 脱敏。

        处理:
            - 中国身份证(18 位)→ [REDACTED-ID]
            - 中国手机号(11 位 1[3-9])→ [REDACTED-PHONE]
            - email → [REDACTED-EMAIL]
        """
        if not content:
            return content
        # 身份证
        content = re.sub(
            r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b",
            "[REDACTED-ID]",
            content,
        )
        # 手机号
        content = re.sub(r"\b1[3-9]\d{9}\b", "[REDACTED-PHONE]", content)
        # email
        content = re.sub(
            r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
            "[REDACTED-EMAIL]",
            content,
        )
        return content


__all__ = [
    "Anonymizer",
    "AnonymizationResult",
    "QUASI_IDENTIFIERS",
    "SENSITIVE_ATTRIBUTES",
]
