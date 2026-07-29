"""P7.4 Feature Flag 系统 - 统一管理所有 *_enabled 配置。

支持三种来源(优先级高→低):
    1. 动态运行时配置(data/feature_flags.json) - 可热更新,无需重启
    2. 环境变量(DEADMAN_*_ENABLED) - 部署期固定
    3. 内置默认值(本文件 _DEFAULTS) - 安全兜底

支持四种评估模式:
    - boolean: 开/关(True/False)
    - percentage: 按 user_id hash 百分比分流(灰度)
    - variant: A/B/C 多变体(对应不同 prompt/模型配置)
    - user_list: 白名单(指定 user_id 列表开启)

设计原则:
    - 静默降级:flag 解析失败一律返回默认值,绝不抛异常
    - 可观测:每次 evaluate 记录 reason,便于审计
    - 原子写:动态配置写文件用 .tmp + os.replace 原子替换
    - 并发安全:读写各自加锁,热更新不阻塞读

集成方式(向后兼容):
    - 老 env var 仍可工作:is_enabled("MEMORY_COMPRESS") 先查 env DEADMAN_MEMORY_COMPRESS_ENABLED
    - 新代码统一用 FeatureFlagManager.is_enabled("memory_compress")

feature flag:`DEADMAN_FEATURE_FLAG_SYSTEM_ENABLED=1`(默认启用,无副作用)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# =====================================================================
# 全局开关 - 关闭时所有 flag 直接走 env var / 默认值,跳过动态配置
# =====================================================================
FEATURE_FLAG_SYSTEM_ENABLED: bool = os.environ.get(
    "DEADMAN_FEATURE_FLAG_SYSTEM_ENABLED", "1"
).lower() in ("1", "true", "yes", "on")

# 动态配置文件位置(与 data/ 同级)
DEFAULT_FLAGS_FILE = Path(os.environ.get("DEADMAN_FLAGS_FILE", "data/feature_flags.json"))

# 缓存 TTL:动态配置热更新后,最多 N 秒内所有线程看到旧值(避免每次读盘)
_CACHE_TTL_SECONDS = 5


# =====================================================================
# 内置默认值 - 安全兜底
# 与各模块 env var 命名对齐:DEADMAN_<NAME>_ENABLED
# =====================================================================
_DEFAULTS: dict[str, bool] = {
    # P0 - 代码级断点(默认开启)
    "debate": True,
    "ragas_eval": False,  # CI 标记 slow,默认 skip
    "reflexion_persist": True,
    "react_loop": True,
    "memory_compress": True,
    # P1 - 规划推理层
    "plan_execute": False,
    "tot": False,
    "evaluator_optimizer": False,
    "self_consistency": False,
    "react_reflexion": False,
    "cot_templates": True,
    # P2 - 记忆系统增强
    "vector_store": False,
    "episodic_ttl": False,
    "graphiti_deep": False,
    "shared_knowledge": False,
    "memory_snapshot": False,
    "forgetting_curve": False,
    # P3 - 工具扩展
    "mcp_gateway": False,
    "dry_run": False,
    "tool_permissions": False,
    "tool_cache": False,
    "dynamic_tool_registration": False,
    "tool_signing": False,
    # P4 - 多智能体协作
    "handoff": False,
    "scratchpad": False,
    "agent_registry": False,
    "a2a_v12": False,
    "handoff_audit": False,
    # P5 - 安全护栏
    "audit_chain": False,
    "jit_permission": False,
    "guid_sandbox": False,
    "content_sandbox": False,
    "redteam": False,
    "honeypot": False,
    # P6 - 可观测性
    "root_cause": False,
    "slo_dashboard": False,
    "trace_to_eval": False,
    "drift_detection": False,
    "replay": False,
    # P7 - 工程基建
    "web_middleware": False,
    "circuit_breaker": False,
    "multi_tenant": False,
    "prompt_versioning": False,
    "durable_execution": False,
    "quota": False,
    "credential_vault": False,
    # P7 自身:feature flag 系统默认启用
    "feature_flag_system": True,
    # P7.8 防御性工程(默认启用,无副作用)
    "defense": True,
    # P8 - 战略级
    "billing": False,
    "multimodal": False,
    "knowledge_graph": False,
    "marketplace": False,
    "i18n": False,
    "compliance": False,
    "alignment": False,
    "governance": False,
}


@dataclass
class FlagRule:
    """单个 flag 的运行时配置(可动态更新)。

    Attributes:
        name: flag 名(小写下划线,如 memory_compress)
        enabled: 总开关(False → 直接返回 False,不走 percentage/variant/user_list)
        percentage: 0-100,按 user_id hash 分流
        variant: 多变体配置(如 {"model": "gpt-4o-mini"} 用于 AB)
        user_whitelist: 显式开启的 user_id 列表(优先于 percentage)
        user_blacklist: 显式关闭的 user_id 列表(优先于 whitelist)
        description: 人类可读说明
        updated_at: 最后更新时间戳(epoch)
        updated_by: 最后更新者(admin user_id)
    """

    name: str
    enabled: bool
    percentage: int = 100
    variant: dict[str, Any] = field(default_factory=dict)
    user_whitelist: list[str] = field(default_factory=list)
    user_blacklist: list[str] = field(default_factory=list)
    description: str = ""
    updated_at: float = 0.0
    updated_by: str = "system"


@dataclass
class EvaluationResult:
    """flag 评估结果 - 含 reason 便于审计/调试。"""

    name: str
    value: bool
    variant: dict[str, Any] | None = None
    reason: str = ""  # "default" / "env_var" / "dynamic" / "whitelist" / "blacklist" / "percentage"


class FeatureFlagManager:
    """统一 Feature Flag 管理器。

    用法:
        from deadman.infrastructure.feature_flags import flags
        if flags.is_enabled("memory_compress", user_id="u123"):
            ...

    线程安全:读写各自加锁。热更新不阻塞读(读用缓存)。
    """

    def __init__(
        self,
        flags_file: Path | None = None,
        cache_ttl: int = _CACHE_TTL_SECONDS,
    ) -> None:
        self.flags_file = flags_file or DEFAULT_FLAGS_FILE
        self.cache_ttl = cache_ttl
        self._lock = threading.RLock()
        # 缓存:{name: FlagRule} + last_loaded_at
        self._cache: dict[str, FlagRule] = {}
        self._cache_loaded_at: float = 0.0

    # ==================================================================
    # 读取入口
    # ==================================================================

    def is_enabled(
        self,
        flag_name: str,
        user_id: str | None = None,
    ) -> bool:
        """评估 flag 是否启用。

        评估顺序:
            1. 系统总开关关闭 → 直接走 env var(向后兼容)
            2. user_blacklist 命中 → False
            3. user_whitelist 命中 → True
            4. percentage + user_id hash → 按百分比
            5. percentage=100 + 无 user_id → enabled
            6. 动态配置 enabled
            7. env var DEADMAN_<NAME>_ENABLED(向后兼容)
            8. 内置 _DEFAULTS

        Args:
            flag_name: flag 名(如 "memory_compress")
            user_id: 用户 ID(用于灰度/白名单),可选

        Returns:
            True/False
        """
        result = self.evaluate(flag_name, user_id)
        return result.value

    def evaluate(
        self,
        flag_name: str,
        user_id: str | None = None,
    ) -> EvaluationResult:
        """详细评估(含 reason + variant),便于审计。"""
        # 1. 系统总开关关闭 → 完全走 env var(向后兼容老配置)
        if not FEATURE_FLAG_SYSTEM_ENABLED:
            env_val = self._read_env_var(flag_name)
            return EvaluationResult(
                name=flag_name,
                value=env_val if env_val is not None else _DEFAULTS.get(flag_name, False),
                reason="env_var_fallback",
            )

        # 2. 加载动态配置(惰性 + 缓存 TTL)
        rules = self._load_rules()

        # 3. user_blacklist 优先级最高
        rule = rules.get(flag_name)
        if rule and user_id and user_id in rule.user_blacklist:
            return EvaluationResult(
                name=flag_name,
                value=False,
                variant=rule.variant if rule.enabled else None,
                reason="blacklist",
            )

        # 4. user_whitelist 次高
        if rule and user_id and user_id in rule.user_whitelist:
            return EvaluationResult(
                name=flag_name,
                value=True,
                variant=rule.variant or None,
                reason="whitelist",
            )

        # 5. percentage + user_id hash(灰度)
        if rule and rule.enabled and user_id and rule.percentage < 100:
            bucket = self._hash_user_to_bucket(flag_name, user_id)
            if bucket < rule.percentage:
                return EvaluationResult(
                    name=flag_name,
                    value=True,
                    variant=rule.variant or None,
                    reason=f"percentage_hit({bucket}<{rule.percentage})",
                )
            else:
                return EvaluationResult(
                    name=flag_name,
                    value=False,
                    reason=f"percentage_miss({bucket}>={rule.percentage})",
                )

        # 6. 动态配置存在 → 用 enabled
        if rule is not None:
            return EvaluationResult(
                name=flag_name,
                value=rule.enabled,
                variant=rule.variant or None,
                reason="dynamic",
            )

        # 7. env var(向后兼容)
        env_val = self._read_env_var(flag_name)
        if env_val is not None:
            # 环境变量优先于默认值
            return EvaluationResult(
                name=flag_name,
                value=env_val,
                reason="env_var",
            )

        # 8. 内置默认值
        default_val = _DEFAULTS.get(flag_name, False)
        return EvaluationResult(
            name=flag_name,
            value=default_val,
            reason="default",
        )

    def get_variant(
        self,
        flag_name: str,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        """获取 flag 的 variant 配置(用于 AB 测试切换 prompt/模型)。

        仅当 flag enabled 且 percentage 命中时返回 variant。
        """
        result = self.evaluate(flag_name, user_id)
        if result.value:
            return result.variant
        return None

    # ==================================================================
    # 动态配置管理(admin API 用)
    # ==================================================================

    def set_flag(
        self,
        flag_name: str,
        enabled: bool | None = None,
        percentage: int | None = None,
        variant: dict[str, Any] | None = None,
        user_whitelist: list[str] | None = None,
        user_blacklist: list[str] | None = None,
        description: str | None = None,
        updated_by: str = "admin",
    ) -> FlagRule:
        """动态更新单个 flag(热更新,无需重启)。

        只更新显式传入的字段,其他字段保留原值。
        持久化到 flags_file。
        """
        with self._lock:
            rules = self._load_rules(force_reload=True)
            existing = rules.get(flag_name)

            if existing is None:
                # 新建 flag,默认值取 _DEFAULTS
                existing = FlagRule(
                    name=flag_name,
                    enabled=_DEFAULTS.get(flag_name, False),
                    percentage=100,
                )

            # 字段更新
            if enabled is not None:
                existing.enabled = enabled
            if percentage is not None:
                existing.percentage = max(0, min(100, int(percentage)))
            if variant is not None:
                existing.variant = variant
            if user_whitelist is not None:
                existing.user_whitelist = list(user_whitelist)
            if user_blacklist is not None:
                existing.user_blacklist = list(user_blacklist)
            if description is not None:
                existing.description = description
            existing.updated_at = time.time()
            existing.updated_by = updated_by

            rules[flag_name] = existing
            self._save_rules(rules)
            # 立即刷新缓存
            self._cache[flag_name] = existing
            self._cache_loaded_at = time.time()

            logger.info(
                "Feature flag %s updated: enabled=%s percentage=%s by=%s",
                flag_name,
                existing.enabled,
                existing.percentage,
                updated_by,
            )
            return existing

    def delete_flag(self, flag_name: str) -> bool:
        """删除动态 flag(回退到 env var / 默认值)。"""
        with self._lock:
            rules = self._load_rules(force_reload=True)
            if flag_name in rules:
                del rules[flag_name]
                self._save_rules(rules)
                self._cache.pop(flag_name, None)
                logger.info("Feature flag %s deleted, falls back to env/default", flag_name)
                return True
            return False

    def list_flags(self) -> list[dict[str, Any]]:
        """列出所有 flag(动态 + 默认),便于 admin 看板展示。"""
        rules = self._load_rules()
        all_names = sorted(set(list(_DEFAULTS.keys()) + list(rules.keys())))
        result: list[dict[str, Any]] = []
        for name in all_names:
            rule = rules.get(name)
            if rule:
                result.append({
                    "name": name,
                    "source": "dynamic",
                    "enabled": rule.enabled,
                    "percentage": rule.percentage,
                    "variant": rule.variant,
                    "user_whitelist": rule.user_whitelist,
                    "user_blacklist": rule.user_blacklist,
                    "description": rule.description,
                    "updated_at": rule.updated_at,
                    "updated_by": rule.updated_by,
                })
            else:
                env_val = self._read_env_var(name)
                result.append({
                    "name": name,
                    "source": "env" if env_val is not None else "default",
                    "enabled": env_val if env_val is not None else _DEFAULTS.get(name, False),
                    "percentage": 100,
                    "variant": {},
                    "user_whitelist": [],
                    "user_blacklist": [],
                    "description": "",
                    "updated_at": 0,
                    "updated_by": "",
                })
        return result

    # ==================================================================
    # 内部:文件 IO + env var + hash
    # ==================================================================

    def _load_rules(self, force_reload: bool = False) -> dict[str, FlagRule]:
        """加载动态 flag 配置(带缓存 TTL,避免每次读盘)。"""
        if not FEATURE_FLAG_SYSTEM_ENABLED:
            return {}

        now = time.time()
        with self._lock:
            if not force_reload and self._cache and (now - self._cache_loaded_at) < self.cache_ttl:
                return dict(self._cache)

            rules: dict[str, FlagRule] = {}
            try:
                if self.flags_file.exists():
                    text = self.flags_file.read_text(encoding="utf-8")
                    data = json.loads(text) if text.strip() else {}
                    for name, rule_data in data.get("flags", {}).items():
                        rules[name] = FlagRule(
                            name=name,
                            enabled=bool(rule_data.get("enabled", False)),
                            percentage=int(rule_data.get("percentage", 100)),
                            variant=rule_data.get("variant", {}) or {},
                            user_whitelist=list(rule_data.get("user_whitelist", []) or []),
                            user_blacklist=list(rule_data.get("user_blacklist", []) or []),
                            description=rule_data.get("description", ""),
                            updated_at=float(rule_data.get("updated_at", 0.0)),
                            updated_by=rule_data.get("updated_by", "system"),
                        )
            except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
                # 静默降级:配置损坏不抛异常,只用默认值
                logger.warning("Feature flags config load failed: %s, using cache/defaults", e)

            # 更新缓存
            self._cache = dict(rules)
            self._cache_loaded_at = now
            return rules

    def _save_rules(self, rules: dict[str, FlagRule]) -> None:
        """原子写入动态 flag 配置(.tmp + os.replace)。"""
        try:
            self.flags_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "updated_at": time.time(),
                "flags": {name: asdict(rule) for name, rule in rules.items()},
            }
            tmp_path = self.flags_file.with_suffix(self.flags_file.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.flags_file)
        except OSError as e:
            logger.error("Feature flags save failed: %s", e)
            raise

    def _read_env_var(self, flag_name: str) -> bool | None:
        """读环境变量 DEADMAN_<NAME>_ENABLED(向后兼容)。

        Returns:
            True/False/None(未设置时返回 None)
        """
        env_key = f"DEADMAN_{flag_name.upper()}_ENABLED"
        val = os.environ.get(env_key)
        if val is None:
            return None
        return val.lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _hash_user_to_bucket(flag_name: str, user_id: str) -> int:
        """稳定哈希 user_id 到 0-99 桶(同一 user+flag 永远命中同一桶)。

        用 sha256 取前 8 字节 mod 100,确保分布均匀。
        """
        key = f"{flag_name}:{user_id}"
        hash_bytes = hashlib.sha256(key.encode("utf-8")).digest()
        # 取前 8 字节转 int,mod 100
        bucket = int.from_bytes(hash_bytes[:8], "big") % 100
        return bucket


# =====================================================================
# 全局单例(惰性初始化,避免 import 时 IO)
# =====================================================================
_flags_instance: FeatureFlagManager | None = None
_flags_lock = threading.Lock()


def get_flags() -> FeatureFlagManager:
    """获取全局 FeatureFlagManager 单例。"""
    global _flags_instance
    if _flags_instance is None:
        with _flags_lock:
            if _flags_instance is None:
                _flags_instance = FeatureFlagManager()
    return _flags_instance


# 便捷模块级 API(向后兼容老代码 if FeatureFlagManager.is_enabled(...) 写法)
def is_enabled(flag_name: str, user_id: str | None = None) -> bool:
    """模块级便捷函数,代理到全局单例。"""
    return get_flags().is_enabled(flag_name, user_id)


def evaluate(flag_name: str, user_id: str | None = None) -> EvaluationResult:
    """模块级便捷函数。"""
    return get_flags().evaluate(flag_name, user_id)


def get_variant(flag_name: str, user_id: str | None = None) -> dict[str, Any] | None:
    """模块级便捷函数。"""
    return get_flags().get_variant(flag_name, user_id)
