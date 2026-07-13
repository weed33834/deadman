"""规则加载器 - 加载 rules/*.md 并提供优先级链校验"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import settings
from .types import RuleCheckResult, RiskTier

logger = logging.getLogger(__name__)

# 规则优先级链 - conflict-resolution.md
RULE_PRIORITY = {
    0: "safety-protocol",
    1: "integrity-framework",
    2: "input-guardrails",
    3: "compliance-framework",
    4: "risk-tier-framework",
    5: "transparency-framework",
    6: "accountability-framework",
    7: "retrieval-guardrails",
    8: "tone-framework",
}

# 补充规则（无优先级，作为约束补充）
SUPPLEMENTARY_RULES = [
    "legal-compliance-framework",
    "multilingual-framework",
    "service-boundary-framework",
    "special-populations-framework",
]


class RuleLoader:
    """规则加载器"""

    def __init__(self, rules_dir: Path | None = None):
        self.rules_dir = rules_dir or settings.rules_dir
        self._cache: dict[str, str] = {}

    def load_rule(self, name: str) -> str:
        """加载单个规则文件"""
        if name in self._cache:
            return self._cache[name]
        path = self.rules_dir / f"{name}.md"
        if not path.exists():
            logger.warning(f"Rule file not found: {path}")
            return ""
        content = path.read_text(encoding="utf-8")
        self._cache[name] = content
        return content

    def load_all_rules(self) -> dict[int, str]:
        """按优先级加载所有规则"""
        rules = {}
        for priority, name in RULE_PRIORITY.items():
            content = self.load_rule(name)
            if content:
                rules[priority] = content
        return rules

    def load_supplementary(self) -> dict[str, str]:
        """加载补充规则"""
        return {name: self.load_rule(name) for name in SUPPLEMENTARY_RULES}

    def get_system_prompt_rules(self) -> str:
        """生成注入 system prompt 的规则文本"""
        rules = self.load_all_rules()
        supplementary = self.load_supplementary()

        lines = ["# 必须遵守的规则（按优先级从高到低）\n"]
        for priority in sorted(rules.keys()):
            name = RULE_PRIORITY[priority]
            lines.append(f"## L{priority} {name}")
            lines.append(rules[priority][:2000])  # 截断防止 token 过多
            lines.append("")

        lines.append("# 补充规则\n")
        for name, content in supplementary.items():
            lines.append(f"## {name}")
            lines.append(content[:2000])
            lines.append("")

        return "\n".join(lines)


class RuleChecker:
    """规则校验器 - 对输出做 L0-L8 校验"""

    # L1 诚信 - 编造检测正则
    FABRICATION_PATTERNS = [
        r"大概\s*\d+",  # "大概 7 天"
        r"应该\s*\d+",  # "应该 15 个工作日"
        r"差不多\s*\d+",
        r"估计\s*\d+",
        r"我记得是",
    ]

    # L0 安全 - 心理危机关键词
    CRISIS_KEYWORDS = ["不想活", "想死", "跟着去", "活不下去", "结束自己"]

    # L4 风险等级信号
    R2_SIGNALS = ["继承争议", "多继承人", "无遗嘱", "跨境", "跨国", "诉讼"]
    R3_SIGNALS = CRISIS_KEYWORDS + ["自残", "轻生"]

    def check(self, output_text: str, context: dict | None = None) -> RuleCheckResult:
        """执行 L0-L8 规则校验"""
        violations: list[dict] = []
        risk_tier = RiskTier.R0
        safety_triggered = False
        integrity_violations: list[str] = []

        # L0 safety - 心理危机检测
        for keyword in self.CRISIS_KEYWORDS:
            if keyword in output_text:
                safety_triggered = True
                risk_tier = RiskTier.R3
                break

        # L1 integrity - 编造检测
        for pattern in self.FABRICATION_PATTERNS:
            matches = re.findall(pattern, output_text)
            if matches:
                integrity_violations.append(f"疑似编造: {matches}")
                violations.append({
                    "rule": "integrity-framework",
                    "priority": 1,
                    "violation": f"匹配到编造模式: {pattern}",
                    "matches": matches,
                })

        # L4 risk-tier - 风险信号检测
        if not safety_triggered:
            for signal in self.R3_SIGNALS:
                if signal in output_text:
                    risk_tier = RiskTier.R3
                    safety_triggered = True
                    break
            if risk_tier == RiskTier.R0:
                for signal in self.R2_SIGNALS:
                    if signal in output_text:
                        risk_tier = RiskTier.R2
                        violations.append({
                            "rule": "risk-tier-framework",
                            "priority": 4,
                            "violation": f"检测到 R2 信号: {signal}",
                        })
                        break

        return RuleCheckResult(
            passed=len(violations) == 0 and not safety_triggered,
            violations=violations,
            risk_tier=risk_tier,
            safety_triggered=safety_triggered,
            integrity_violations=integrity_violations,
        )


# 全局单例
rule_loader = RuleLoader()
rule_checker = RuleChecker()
