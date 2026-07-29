"""P3.1 MCP 6 层网关 - 在工具实际执行前依次过 6 道防线

6 层依次执行，任一层拦截即返回（短路语义）：
  1. schema_validate  : 参数类型 / 必填校验（基于工具 input_schema）
  2. trust_score      : 信任评分（按工具名 + caller），< 0.3 拦截
  3. rate_limit       : 令牌桶限流（按 tool + user），默认 100 QPM
  4. adversarial_prefilter: 检测 prompt injection 痕迹
  5. semantic_gate    : 可选 LLM 判断调用意图（feature flag 子开关）
  6. policy_match     : 规则匹配（如 write_file 禁止写 rules/）

任一层拦截 → GatewayDecision(allowed=False, reason=..., layer=..., score=...)
全部通过 → GatewayDecision(allowed=True, layer="all_passed", score=1.0)

Feature flag:DEADMAN_MCP_GATEWAY_ENABLED=0（默认关闭）
关闭时 evaluate 直接返回 allowed=True，不执行任何层，保证旧行为不变。

降级路径：
  - 工具未注册 schema → schema_validate 跳过（不强制）
  - LLM 不可用（semantic_gate）→ 跳过该层
  - 任何层抛异常 → 视为该层通过（fail-open，避免阻断业务）
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# =====================================================================
# 配置（feature flag，默认关闭）
# =====================================================================

GATEWAY_ENABLED: bool = os.environ.get(
    "DEADMAN_MCP_GATEWAY_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")

# 语义门子开关（默认关闭，避免无 LLM 时误拦截）
GATEWAY_SEMANTIC_ENABLED: bool = os.environ.get(
    "DEADMAN_MCP_GATEWAY_SEMANTIC_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")

# 信任评分阈值（低于此值拦截）
GATEWAY_TRUST_THRESHOLD: float = float(
    os.environ.get("DEADMAN_MCP_GATEWAY_TRUST_THRESHOLD", "0.3")
)

# 限流：默认 100 QPM（每分钟 100 次）
GATEWAY_RATE_LIMIT_QPM: int = int(
    os.environ.get("DEADMAN_MCP_GATEWAY_RATE_LIMIT_QPM", "100")
)

# 令牌桶容量（默认 = QPM，允许 1 分钟的突发）
GATEWAY_RATE_LIMIT_BURST: int = int(
    os.environ.get("DEADMAN_MCP_GATEWAY_RATE_LIMIT_BURST", str(GATEWAY_RATE_LIMIT_QPM))
)


# =====================================================================
# GatewayDecision
# =====================================================================


@dataclass
class GatewayDecision:
    """网关判定结果"""

    allowed: bool
    reason: str = ""
    layer: str = ""  # 拦截层名（如 "schema_validate"）；allowed=True 时为 "all_passed"
    score: float = 1.0  # 信任评分（0.0-1.0）；拦截时为该层给出的分数

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "layer": self.layer,
            "score": self.score,
        }


# =====================================================================
# Prompt injection 检测关键词（中英双语）
# =====================================================================

# 这些正则模式用于检测 args 中可能存在的 prompt injection 痕迹。
# 命中任一即视为对抗输入，由 adversarial_prefilter 拦截。
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"忽略(?:以上|前面|之前).*?(?:指令|规则|提示)", re.IGNORECASE),
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|above|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"无视(?:以上|前面|之前).*?(?:指令|规则|提示)", re.IGNORECASE),
    re.compile(r"你(?:现在|其实)?(?:是|扮演)(?:DAN|越狱|jailbreak)", re.IGNORECASE),
    re.compile(r"system\s*prompt|系统\s*提示", re.IGNORECASE),
    re.compile(r"developer\s*mode|开发者模式", re.IGNORECASE),
    re.compile(r"</?\s*(?:system|assistant|developer)\s*>", re.IGNORECASE),
    re.compile(r"不要再(?:遵守|遵循).*?(?:规则|指令|提示)"),
    re.compile(r"假装(?:你是|没有)(?:限制|规则|约束)"),
]


# =====================================================================
# TokenBucket 令牌桶
# =====================================================================


class _TokenBucket:
    """简单令牌桶：容量 burst，每秒补充 rate/60 个令牌"""

    __slots__ = ("capacity", "last_refill", "lock", "rate_per_sec", "tokens")

    def __init__(self, rate_per_minute: int, burst: int) -> None:
        self.capacity = max(1, burst)
        self.rate_per_sec = max(0.0, rate_per_minute / 60.0)
        self.tokens = float(self.capacity)
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def consume(self, tokens: float = 1.0) -> bool:
        """尝试消耗 tokens 个令牌，成功返回 True"""
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.last_refill = now
            # 补充令牌（不超过容量）
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_sec)
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False


# =====================================================================
# ToolGateway
# =====================================================================


class ToolGateway:
    """MCP 工具 6 层网关

    用法：
        gw = ToolGateway()
        # 注册工具 schema（供 schema_validate 用）
        gw.register_schema("query_knowledge", {...input_schema...})
        # 评估
        decision = gw.evaluate("query_knowledge", {"country": "CN"}, caller="user-1")
        if not decision.allowed:
            return {"ok": False, "error": "blocked by gateway", ...}
    """

    def __init__(
        self,
        rate_limit_qpm: int = GATEWAY_RATE_LIMIT_QPM,
        rate_limit_burst: int = GATEWAY_RATE_LIMIT_BURST,
        trust_threshold: float = GATEWAY_TRUST_THRESHOLD,
    ) -> None:
        self._schemas: dict[str, dict[str, Any]] = {}
        self._trust_scores: dict[tuple[str, str], float] = {}
        self._buckets: dict[tuple[str, str], _TokenBucket] = {}
        self._rate_limit_qpm = rate_limit_qpm
        self._rate_limit_burst = rate_limit_burst
        self._trust_threshold = trust_threshold
        self._lock = threading.RLock()
        # semantic_gate 可注入的 LLM 判定 callable（避免硬依赖 llm_client）
        # 签名：async def (tool_name, args) -> tuple[bool, str]
        self._semantic_judge: Callable[..., Any] | None = None
        # 自定义 policy 规则（list[(check_fn, layer_reason)]）
        # check_fn 签名：(tool_name, args) -> (allowed: bool, reason: str)
        self._policy_rules: list[
            tuple[Callable[[str, dict[str, Any]], tuple[bool, str]], str]
        ] = []
        # 注册默认 policy 规则
        self._register_default_policies()

    # ---------- 配置接口 ----------

    def register_schema(self, tool_name: str, schema: dict[str, Any]) -> None:
        """注册工具的 input_schema（供 schema_validate 使用）"""
        self._schemas[tool_name] = schema

    def set_trust_score(self, tool_name: str, caller: str, score: float) -> None:
        """显式设置某 (tool, caller) 的信任评分（0.0-1.0）"""
        score = max(0.0, min(1.0, float(score)))
        with self._lock:
            self._trust_scores[(tool_name, caller)] = score

    def set_semantic_judge(self, judge: Callable[..., Any] | None) -> None:
        """注入语义判定 callable（async，返回 (allowed, reason)）

        未注入时 semantic_gate 跳过（fail-open）。
        """
        self._semantic_judge = judge

    def add_policy_rule(
        self,
        check_fn: Callable[[str, dict[str, Any]], tuple[bool, str]],
        layer_reason: str = "policy_match",
    ) -> None:
        """添加自定义 policy 规则"""
        self._policy_rules.append((check_fn, layer_reason))

    def _register_default_policies(self) -> None:
        """注册默认 policy 规则（如 write_file 禁止写 rules/*.md）"""

        def _no_rules_write(tool_name: str, args: dict[str, Any]) -> tuple[bool, str]:
            if tool_name != "write_file":
                return True, ""
            path = str(args.get("path", "")).lower()
            if path.startswith("rules/") and path.endswith(".md"):
                return False, "禁止通过 write_file 写 rules/*.md（人工维护）"
            return True, ""

        self._policy_rules.append((_no_rules_write, "policy_match"))

    # ---------- 6 层实现 ----------

    def schema_validate(
        self, tool_name: str, args: dict[str, Any]
    ) -> tuple[bool, str, float]:
        """第 1 层：参数类型 / 必填校验"""
        schema = self._schemas.get(tool_name)
        if not schema:
            # 未注册 schema：跳过（fail-open，避免阻断未注册工具）
            return True, "", 1.0
        try:
            properties = schema.get("properties", {}) or {}
            required = schema.get("required", []) or []
            # 必填校验
            for req in required:
                if req not in args:
                    return False, f"缺少必填参数: {req}", 0.2
            # 类型校验（粗粒度：仅校验 JSON Schema type）
            _TYPE_MAP = {
                "string": str,
                "integer": int,
                "number": (int, float),
                "boolean": bool,
                "array": list,
                "object": dict,
            }
            for key, value in args.items():
                if key not in properties:
                    continue  # 未声明的额外参数：放行
                expected_type = properties[key].get("type")
                if not expected_type:
                    continue
                py_type = _TYPE_MAP.get(expected_type)
                if py_type is None:
                    continue
                # bool 是 int 的子类，需特殊处理（避免 True 被当 integer 通过）
                if expected_type == "integer" and isinstance(value, bool):
                    return False, f"参数 {key} 期望 integer 但收到 boolean", 0.2
                if expected_type == "number" and isinstance(value, bool):
                    return False, f"参数 {key} 期望 number 但收到 boolean", 0.2
                if not isinstance(value, py_type):
                    return (
                        False,
                        f"参数 {key} 期望 {expected_type} 但收到 {type(value).__name__}",
                        0.2,
                    )
            return True, "", 1.0
        except Exception as exc:
            # 校验逻辑自身出错：fail-open
            return True, f"schema_validate 内部异常: {exc}", 1.0

    def trust_score(
        self, tool_name: str, caller: str
    ) -> tuple[bool, str, float]:
        """第 2 层：信任评分"""
        with self._lock:
            score = self._trust_scores.get((tool_name, caller), 1.0)
        if score < self._trust_threshold:
            return (
                False,
                f"信任评分 {score:.2f} < 阈值 {self._trust_threshold:.2f}",
                score,
            )
        return True, "", score

    def rate_limit(self, tool_name: str, user_id: str) -> tuple[bool, str, float]:
        """第 3 层：令牌桶限流"""
        key = (tool_name, user_id)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _TokenBucket(self._rate_limit_qpm, self._rate_limit_burst)
                self._buckets[key] = bucket
        if not bucket.consume(1.0):
            return False, f"触发限流（{self._rate_limit_qpm} QPM）", 0.1
        return True, "", 1.0

    def adversarial_prefilter(self, args: dict[str, Any]) -> tuple[bool, str, float]:
        """第 4 层：检测 prompt injection 痕迹"""
        try:
            text = json.dumps(args, ensure_ascii=False, default=str)
        except Exception:
            text = str(args)
        for pattern in _INJECTION_PATTERNS:
            m = pattern.search(text)
            if m:
                return (
                    False,
                    f"检测到 prompt injection 痕迹: 匹配 {pattern.pattern!r}",
                    0.0,
                )
        return True, "", 1.0

    async def semantic_gate(
        self, tool_name: str, args: dict[str, Any]
    ) -> tuple[bool, str, float]:
        """第 5 层：可选 LLM 判断调用意图"""
        if not GATEWAY_SEMANTIC_ENABLED:
            return True, "", 1.0
        if self._semantic_judge is None:
            # 未注入判定器：跳过（fail-open）
            return True, "", 1.0
        try:
            result = await self._semantic_judge(tool_name, args)
            # 兼容 (allowed, reason) 与 dict 两种返回
            if isinstance(result, tuple):
                allowed, reason = result
            elif isinstance(result, dict):
                allowed = bool(result.get("allowed", True))
                reason = result.get("reason", "")
            else:
                allowed = bool(result)
                reason = ""
            return bool(allowed), reason, 0.5 if not allowed else 1.0
        except Exception as exc:
            # LLM 调用失败：fail-open
            return True, f"semantic_gate 跳过: {exc}", 1.0

    def policy_match(
        self, tool_name: str, args: dict[str, Any]
    ) -> tuple[bool, str, float]:
        """第 6 层：规则匹配"""
        for check_fn, layer_reason in self._policy_rules:
            try:
                allowed, reason = check_fn(tool_name, args)
                if not allowed:
                    return False, reason or layer_reason, 0.1
            except Exception:
                # policy 规则自身出错：fail-open
                continue
        return True, "", 1.0

    # ---------- 总入口 ----------

    async def evaluate(
        self,
        tool_name: str,
        args: dict[str, Any],
        caller: str = "default",
        user_id: str | None = None,
    ) -> GatewayDecision:
        """依次执行 6 层，任一拦截即返回

        Args:
            tool_name: 工具名
            args: 工具参数
            caller: 调用方身份（用于信任评分）
            user_id: 用户 ID（用于限流）；为 None 时用 caller
        """
        if not GATEWAY_ENABLED:
            return GatewayDecision(allowed=True, layer="gateway_disabled", score=1.0)

        args = args or {}
        uid = user_id if user_id is not None else caller

        # 第 1 层
        ok, reason, score = self.schema_validate(tool_name, args)
        if not ok:
            return GatewayDecision(False, reason, "schema_validate", score)

        # 第 2 层
        ok, reason, score = self.trust_score(tool_name, caller)
        if not ok:
            return GatewayDecision(False, reason, "trust_score", score)

        # 第 3 层
        ok, reason, score = self.rate_limit(tool_name, uid)
        if not ok:
            return GatewayDecision(False, reason, "rate_limit", score)

        # 第 4 层
        ok, reason, score = self.adversarial_prefilter(args)
        if not ok:
            return GatewayDecision(False, reason, "adversarial_prefilter", score)

        # 第 5 层（async，可调 LLM）
        ok, reason, score = await self.semantic_gate(tool_name, args)
        if not ok:
            return GatewayDecision(False, reason, "semantic_gate", score)

        # 第 6 层
        ok, reason, score = self.policy_match(tool_name, args)
        if not ok:
            return GatewayDecision(False, reason, "policy_match", score)

        return GatewayDecision(True, layer="all_passed", score=1.0)

    # ---------- 维护接口 ----------

    def reset_rate_limits(self) -> None:
        """清空限流桶（测试用）"""
        with self._lock:
            self._buckets.clear()

    def clear_trust_scores(self) -> None:
        """清空信任评分（测试用）"""
        with self._lock:
            self._trust_scores.clear()


# =====================================================================
# 全局单例
# =====================================================================

_global_gateway: ToolGateway | None = None


def get_global_gateway() -> ToolGateway:
    """返回进程级 ToolGateway 单例"""
    global _global_gateway
    if _global_gateway is None:
        _global_gateway = ToolGateway()
    return _global_gateway
