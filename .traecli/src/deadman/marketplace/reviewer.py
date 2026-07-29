"""P8.4.2 Agent 自动审核系统 - 安全扫描 + schema 校验 + PII 检测 + 评分。

设计:
    - ReviewResult: 审核结果(score 0-100,issues, recommendations)
    - ReviewIssue: 单条问题(severity + check + message)
    - AgentReviewer: 跑 5 项 check,聚合结果

5 项 check:
    1. SecurityScan: agent_card 静态扫描(无 shadow tools / 系统调用 / 路径穿越)
    2. SchemaValidation: agent_card 符合 A2A spec(skills / capabilities / tools)
    3. PIILeakCheck: agent 描述 / 响应样本是否泄漏 PII(借 PIIRedactor)
    4. SafetyCheck: 危险模式扫描(文件系统 / 网络 / exec)
    5. QualityScore: 质量分(描述长度 / 示例 / 测试 / 标签)

决策:
    - 任一 critical issue → auto-reject(passed=False)
    - 全部 green 且 score >= 80 → auto-approve(passed=True)
    - 中间状态 → 人工审核(passed=False,但无 critical issue)

feature flag: `DEADMAN_MARKETPLACE_ENABLED=0`(默认关闭)
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..infrastructure.feature_flags import is_enabled
from .registry import AgentListing, MarketplaceError

logger = logging.getLogger(__name__)


# =====================================================================
# 异常 + 枚举
# =====================================================================
class ReviewSeverity(str, Enum):
    """issue 严重度。"""

    INFO = "info"            # 信息性
    WARNING = "warning"      # 警告(扣分但不阻塞)
    CRITICAL = "critical"    # 严重(强制 reject)


class ReviewCheck(str, Enum):
    """检查项标识。"""

    SECURITY = "security_scan"
    SCHEMA = "schema_validation"
    PII = "pii_leak_check"
    SAFETY = "safety_check"
    QUALITY = "quality_score"


# =====================================================================
# 数据模型
# =====================================================================
@dataclass
class ReviewIssue:
    """单条审核 issue。"""

    check: str             # ReviewCheck.value
    severity: str          # ReviewSeverity.value
    message: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class ReviewResult:
    """审核结果。

    Attributes:
        listing_id: 被审核的 listing ID
        passed: 是否通过(auto-approve 阈值)
        score: 0-100(综合分)
        issues: 所有 issue 列表(含 warning)
        recommendations: 给 author 的改进建议
        auto_decision: "approve" / "reject" / "manual"(无 critical 且 score < 80)
    """

    listing_id: str
    passed: bool
    score: int
    issues: list[ReviewIssue] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    auto_decision: str = "manual"

    @property
    def has_critical(self) -> bool:
        return any(
            i.severity == ReviewSeverity.CRITICAL.value
            for i in self.issues
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "listing_id": self.listing_id,
            "passed": self.passed,
            "score": self.score,
            "issues": [i.to_dict() for i in self.issues],
            "recommendations": list(self.recommendations),
            "auto_decision": self.auto_decision,
        }


# =====================================================================
# 危险模式库
# =====================================================================
# 系统调用 / 危险 API 模式
SHADOW_SYSCALL_PATTERNS = [
    re.compile(r"\bos\.system\s*\("),
    re.compile(r"\bsubprocess\.[A-Za-z_]+\s*\("),
    re.compile(r"\bos\.popen\s*\("),
    re.compile(r"\bos\.exec[lv][a-z]*\s*\("),
    re.compile(r"\b__import__\s*\("),
    re.compile(r"\beval\s*\("),
    re.compile(r"\bexec\s*\("),
    re.compile(r"\bcompile\s*\("),
]

# 路径穿越模式
PATH_TRAVERSAL_PATTERNS = [
    re.compile(r"\.\./"),
    re.compile(r"\.\.\\"),
    re.compile(r"/etc/passwd"),
    re.compile(r"/etc/shadow"),
    re.compile(r"~/\.ssh"),
    re.compile(r"\bC:\\Windows\\System32\b", re.IGNORECASE),
]

# 文件系统访问
FS_ACCESS_PATTERNS = [
    re.compile(r"\bopen\s*\("),
    re.compile(r"\bos\.(?:remove|unlink|rmdir|mkdir|makedirs|rename|chmod|chown)\s*\("),
    re.compile(r"\bshutil\.[A-Za-z_]+\s*\("),
    re.compile(r"\bpathlib\.Path\s*\("),
]

# 网络访问(注:不扫描裸 URL,因为 agent_card.url 是合法 metadata;
# 仅扫描代码层 API 调用)
NETWORK_ACCESS_PATTERNS = [
    re.compile(r"\bsocket\s*\("),
    re.compile(r"\burllib\.[A-Za-z_.]+\s*\("),
    re.compile(r"\brequests\.[A-Za-z_]+\s*\("),
    re.compile(r"\bhttpx\.[A-Za-z_]+\s*\("),
    re.compile(r"\baiohttp\.[A-Za-z_]+\s*\("),
    re.compile(r"\bfetch\s*\("),
]

# shadow tools(不应该被 marketplace agent 重新定义)
SHADOW_TOOL_NAMES = {
    "exec", "eval", "system", "shell", "subprocess",
    "import", "compile", "exit", "quit", "__import__",
}


# =====================================================================
# AgentReviewer
# =====================================================================
class AgentReviewer:
    """自动审核器。

    用法:
        reviewer = get_agent_reviewer()
        result = reviewer.review(listing)
        if result.auto_decision == "approve":
            registry.approve(listing.listing_id)
        elif result.auto_decision == "reject":
            registry.reject(listing.listing_id, reason="auto-reject")
    """

    AUTO_APPROVE_THRESHOLD = 80

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------
    def review(self, listing: AgentListing) -> ReviewResult:
        """对单个 listing 跑全部 5 项 check,聚合结果。

        Args:
            listing: 待审核的 listing(状态应为 pending)

        Returns:
            ReviewResult(passed + score + issues + auto_decision)
        """
        self._require_enabled()
        with self._lock:
            issues: list[ReviewIssue] = []
            recommendations: list[str] = []
            # 跑每项 check
            sec_issues, sec_score = self.security_scan(listing)
            sch_issues, sch_score = self.schema_validation(listing)
            pii_issues, pii_score = self.pii_leak_check(listing)
            saf_issues, saf_score = self.safety_check(listing)
            qual_issues, qual_score = self.quality_score(listing)

            issues.extend(sec_issues)
            issues.extend(sch_issues)
            issues.extend(pii_issues)
            issues.extend(saf_issues)
            issues.extend(qual_issues)

            # 总分 0-100(每项 0-20,5 项相加)
            score = int(sec_score + sch_score + pii_score + saf_score + qual_score)
            score = max(0, min(100, score))

            # 改进建议(基于 warning issue)
            for issue in issues:
                if issue.severity == ReviewSeverity.WARNING.value:
                    recommendations.append(
                        f"[{issue.check}] {issue.message}"
                    )

            has_critical = any(
                i.severity == ReviewSeverity.CRITICAL.value for i in issues
            )

            # 决策
            if has_critical:
                passed = False
                auto_decision = "reject"
            elif not issues and score >= self.AUTO_APPROVE_THRESHOLD:
                passed = True
                auto_decision = "approve"
            else:
                # 有 warning 或 score < 80 → 人工审核
                passed = False
                auto_decision = "manual"

            return ReviewResult(
                listing_id=listing.listing_id,
                passed=passed,
                score=score,
                issues=issues,
                recommendations=recommendations,
                auto_decision=auto_decision,
            )

    # ------------------------------------------------------------------
    # Check 1: SecurityScan
    # ------------------------------------------------------------------
    def security_scan(self, listing: AgentListing) -> tuple[list[ReviewIssue], int]:
        """静态扫描 agent_card: 无 shadow tools / 系统调用 / 路径穿越。

        Returns:
            (issues, score 0-20)
        """
        issues: list[ReviewIssue] = []
        score = 20  # 满分
        card = listing.agent_card or {}

        # 序列化 card 供正则扫描(包括 skills 的 description)
        text = self._stringify_card(card)

        # shadow tools
        for skill in card.get("skills", []) or []:
            if not isinstance(skill, dict):
                continue
            tool_name = str(skill.get("id", "")) or str(skill.get("name", ""))
            if tool_name.lower() in SHADOW_TOOL_NAMES:
                issues.append(ReviewIssue(
                    check=ReviewCheck.SECURITY.value,
                    severity=ReviewSeverity.CRITICAL.value,
                    message=f"Shadow tool declared: {tool_name}",
                    detail=f"Skill '{tool_name}' shadows a system built-in",
                ))
                score = 0

        # 系统调用
        for pattern in SHADOW_SYSCALL_PATTERNS:
            for m in pattern.finditer(text):
                issues.append(ReviewIssue(
                    check=ReviewCheck.SECURITY.value,
                    severity=ReviewSeverity.CRITICAL.value,
                    message=f"Shadow system call detected: {m.group()}",
                    detail=f"Pattern {pattern.pattern} matched in agent_card",
                ))
                score = 0

        # 路径穿越
        for pattern in PATH_TRAVERSAL_PATTERNS:
            for m in pattern.finditer(text):
                issues.append(ReviewIssue(
                    check=ReviewCheck.SECURITY.value,
                    severity=ReviewSeverity.CRITICAL.value,
                    message=f"Path traversal pattern: {m.group()}",
                    detail=f"Pattern {pattern.pattern} matched in agent_card",
                ))
                score = 0

        return issues, score

    # ------------------------------------------------------------------
    # Check 2: SchemaValidation
    # ------------------------------------------------------------------
    def schema_validation(self, listing: AgentListing) -> tuple[list[ReviewIssue], int]:
        """agent_card schema 符合 A2A spec(name / description / version / skills / capabilities)。

        Returns:
            (issues, score 0-20)
        """
        issues: list[ReviewIssue] = []
        score = 20
        card = listing.agent_card or {}

        # 必填字段
        required = ["name", "description", "version"]
        for field_name in required:
            val = card.get(field_name)
            if not val or not isinstance(val, str):
                issues.append(ReviewIssue(
                    check=ReviewCheck.SCHEMA.value,
                    severity=ReviewSeverity.CRITICAL.value,
                    message=f"Missing required field: {field_name}",
                    detail=f"agent_card.{field_name} must be non-empty string",
                ))
                score = 0

        # skills 必须是 list 且至少 1 个
        skills = card.get("skills")
        if not isinstance(skills, list) or len(skills) == 0:
            issues.append(ReviewIssue(
                check=ReviewCheck.SCHEMA.value,
                severity=ReviewSeverity.CRITICAL.value,
                message="skills must be a non-empty list",
                detail="agent_card.skills required by A2A spec",
            ))
            score = 0
        else:
            # 每个 skill 必须有 id + name + description
            for i, s in enumerate(skills):
                if not isinstance(s, dict):
                    issues.append(ReviewIssue(
                        check=ReviewCheck.SCHEMA.value,
                        severity=ReviewSeverity.CRITICAL.value,
                        message=f"skill[{i}] must be an object",
                    ))
                    score = 0
                    continue
                for kf in ("id", "name", "description"):
                    if not s.get(kf):
                        issues.append(ReviewIssue(
                            check=ReviewCheck.SCHEMA.value,
                            severity=ReviewSeverity.WARNING.value,
                            message=f"skill[{i}].{kf} missing",
                        ))
                        score = min(score, 10)

        # capabilities 必须是 dict
        caps = card.get("capabilities")
        if not isinstance(caps, dict):
            issues.append(ReviewIssue(
                check=ReviewCheck.SCHEMA.value,
                severity=ReviewSeverity.WARNING.value,
                message="capabilities should be an object",
            ))
            score = min(score, 10)

        # tools 可选(若存在必须是 list)
        tools = card.get("tools")
        if tools is not None and not isinstance(tools, list):
            issues.append(ReviewIssue(
                check=ReviewCheck.SCHEMA.value,
                severity=ReviewSeverity.WARNING.value,
                message="tools must be a list if present",
            ))
            score = min(score, 10)

        return issues, score

    # ------------------------------------------------------------------
    # Check 3: PIILeakCheck
    # ------------------------------------------------------------------
    def pii_leak_check(self, listing: AgentListing) -> tuple[list[ReviewIssue], int]:
        """扫描 agent_card / description / skills 是否泄漏 PII。

        借 `defense.pii_guard.PIIRedactor`(防御性工程 D4)。
        defense 关闭时 PIIRedactor.detect 直接返回空结果(透传)。

        Returns:
            (issues, score 0-20)
        """
        issues: list[ReviewIssue] = []
        score = 20

        try:
            from ..infrastructure.defense.pii_guard import get_pii_redactor
            redactor = get_pii_redactor()
        except Exception as e:
            logger.warning("PIIRedactor unavailable, skip PII check: %s", e)
            return issues, score

        # 扫描 description + agent_card 全部字符串字段 + sample responses
        scan_texts = [listing.description or "", listing.name or ""]
        scan_texts.extend(self._extract_card_strings(listing.agent_card or {}))
        # 也扫描 agent_card 中的 sample_responses / examples
        card = listing.agent_card or {}
        for sr in (card.get("sample_responses") or []):
            if isinstance(sr, str):
                scan_texts.append(sr)
        for ex in (card.get("examples") or []):
            if isinstance(ex, dict):
                resp = ex.get("response") or ex.get("output")
                if isinstance(resp, str):
                    scan_texts.append(resp)
            elif isinstance(ex, str):
                scan_texts.append(ex)

        total_pii = 0
        for text in scan_texts:
            if not text:
                continue
            result = redactor.detect(text)
            if result.has_pii:
                total_pii += len(result.matches)
                # 区分敏感度:身份证 / 银行卡 = critical,其他 = warning
                for m in result.matches:
                    pii_type = m.pii_type.value
                    if pii_type in (
                        "china_id_card", "china_bank_card",
                        "china_passport", "credit_card",
                    ):
                        issues.append(ReviewIssue(
                            check=ReviewCheck.PII.value,
                            severity=ReviewSeverity.CRITICAL.value,
                            message=f"High-sensitivity PII leaked: {pii_type}",
                            detail=f"Original text in listing exposes {pii_type}",
                        ))
                        score = 0
                    else:
                        issues.append(ReviewIssue(
                            check=ReviewCheck.PII.value,
                            severity=ReviewSeverity.WARNING.value,
                            message=f"PII detected: {pii_type}",
                            detail=f"Listing contains {pii_type} pattern",
                        ))
                        score = min(score, 10)

        # 即使只有 warning 也再扣一些分
        if total_pii > 0 and score > 0:
            score = max(0, score - 5 * min(total_pii, 3))

        return issues, score

    # ------------------------------------------------------------------
    # Check 4: SafetyCheck
    # ------------------------------------------------------------------
    def safety_check(self, listing: AgentListing) -> tuple[list[ReviewIssue], int]:
        """扫描危险模式: 文件系统访问 / 网络访问 / exec。

        Returns:
            (issues, score 0-20)
        """
        issues: list[ReviewIssue] = []
        score = 20
        card = listing.agent_card or {}
        text = self._stringify_card(card)

        # 文件系统访问
        fs_hits = 0
        for pattern in FS_ACCESS_PATTERNS:
            for m in pattern.finditer(text):
                fs_hits += 1
                issues.append(ReviewIssue(
                    check=ReviewCheck.SAFETY.value,
                    severity=ReviewSeverity.WARNING.value,
                    message=f"File system access pattern: {m.group()}",
                ))
        if fs_hits:
            score = max(0, score - 5 * min(fs_hits, 3))

        # 网络访问
        net_hits = 0
        for pattern in NETWORK_ACCESS_PATTERNS:
            for m in pattern.finditer(text):
                net_hits += 1
                issues.append(ReviewIssue(
                    check=ReviewCheck.SAFETY.value,
                    severity=ReviewSeverity.WARNING.value,
                    message=f"Network access pattern: {m.group()}",
                ))
        if net_hits:
            score = max(0, score - 5 * min(net_hits, 3))

        # exec 调用(security 已 cover 一部分,这里给独立 warning)
        exec_hits = 0
        for pattern in (re.compile(r"\bexec\s*\("), re.compile(r"\beval\s*\(")):
            for m in pattern.finditer(text):
                exec_hits += 1
                issues.append(ReviewIssue(
                    check=ReviewCheck.SAFETY.value,
                    severity=ReviewSeverity.CRITICAL.value,
                    message=f"Dangerous exec/eval pattern: {m.group()}",
                ))
        if exec_hits:
            score = 0

        return issues, score

    # ------------------------------------------------------------------
    # Check 5: QualityScore
    # ------------------------------------------------------------------
    def quality_score(self, listing: AgentListing) -> tuple[list[ReviewIssue], int]:
        """质量分: 描述长度 / 示例 / 测试 / 标签 / skills 数量。

        Returns:
            (issues, score 0-20)
        """
        issues: list[ReviewIssue] = []
        score = 0
        card = listing.agent_card or {}

        # 描述长度(>= 50 字符 = +4)
        desc_len = len(listing.description or "")
        if desc_len >= 50:
            score += 4
        elif desc_len >= 20:
            score += 2
        else:
            issues.append(ReviewIssue(
                check=ReviewCheck.QUALITY.value,
                severity=ReviewSeverity.WARNING.value,
                message="Description too short (<50 chars)",
            ))

        # 至少 1 个 skill(+4)
        skills = card.get("skills") or []
        if isinstance(skills, list) and len(skills) >= 1:
            score += 4
        else:
            issues.append(ReviewIssue(
                check=ReviewCheck.QUALITY.value,
                severity=ReviewSeverity.WARNING.value,
                message="No skills declared",
            ))

        # 至少 1 个 tag(+4)
        if len(listing.tags) >= 1:
            score += 4
        else:
            issues.append(ReviewIssue(
                check=ReviewCheck.QUALITY.value,
                severity=ReviewSeverity.WARNING.value,
                message="No tags provided",
            ))

        # examples(+4)
        examples = card.get("examples") or []
        if isinstance(examples, list) and len(examples) >= 1:
            score += 4
        else:
            issues.append(ReviewIssue(
                check=ReviewCheck.QUALITY.value,
                severity=ReviewSeverity.WARNING.value,
                message="No examples provided",
            ))

        # tests(+4)
        tests = card.get("tests") or []
        if isinstance(tests, list) and len(tests) >= 1:
            score += 4
        else:
            issues.append(ReviewIssue(
                check=ReviewCheck.QUALITY.value,
                severity=ReviewSeverity.INFO.value,
                message="No tests provided",
            ))

        return issues, min(score, 20)

    # ==================================================================
    # 内部
    # ==================================================================
    def _stringify_card(self, card: dict[str, Any]) -> str:
        """把 agent_card 拍平成纯文本(供正则扫描)。

        简化: 用 repr + json,避免漏掉嵌套字段。
        """
        parts: list[str] = []
        try:
            import json as _json
            parts.append(_json.dumps(card, ensure_ascii=False, default=str))
        except Exception:
            parts.append(repr(card))
        return "\n".join(parts)

    def _extract_card_strings(self, obj: Any) -> list[str]:
        """递归提取 card 内所有字符串字段。"""
        out: list[str] = []
        if isinstance(obj, str):
            out.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                out.extend(self._extract_card_strings(v))
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                out.extend(self._extract_card_strings(v))
        return out

    def _require_enabled(self) -> None:
        if not is_enabled("marketplace"):
            raise MarketplaceError(
                "Marketplace feature is disabled (set DEADMAN_MARKETPLACE_ENABLED=1)"
            )


# =====================================================================
# 全局单例
# =====================================================================
_reviewer_instance: AgentReviewer | None = None
_reviewer_lock = threading.Lock()


def get_agent_reviewer() -> AgentReviewer:
    """获取全局 AgentReviewer 单例。"""
    global _reviewer_instance
    if _reviewer_instance is None:
        with _reviewer_lock:
            if _reviewer_instance is None:
                _reviewer_instance = AgentReviewer()
    return _reviewer_instance
