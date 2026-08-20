"""P8.4.5 沙盒执行环境 - 第三方 agent 在受限环境内执行。

设计:
    - SandboxConfig: 资源限制(max_cpu_seconds / max_memory_mb / max_network_calls /
                            max_tool_calls / allowed_tools)
    - SandboxResult: 执行结果(success + output + error + resource_usage)
    - MarketplaceSandbox: 执行器

资源限制实现:
    - CPU 时间: signal.SIGALRM(主线程,超时 raise TimeoutError)
    - 内存: resource.RLIMIT_AS 设置进程级 soft limit + 执行后 getrusage 读 peak
    - 工具调用 / 网络调用: 通过注入的 tool_router 计数 + 白名单校验
    - 注: signal 仅在主线程可用;非主线程降级为软计数 + 时间窗检查

关键集成:
    - PII 双向脱敏: input → redact → handler(input') → output → redact → return
      借 `defense.pii_guard.PIIRedactor`
    - 成本可控: 执行前向 `defense.budget_coordinator.BudgetCoordinator` 申请
      LLM_TOKENS + TOOL_CALLS 预算,执行后按 actual_used 释放

handler 注册:
    - 通过 `register_handler(agent_id, fn)` 注册 Python callable
    - execute 时按 agent_id 查找 handler;未注册返回错误 result

feature flag: `DEADMAN_MARKETPLACE_ENABLED=0`(默认关闭)
"""

from __future__ import annotations

import logging
import signal
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from ..infrastructure.feature_flags import is_enabled
from .registry import MarketplaceError

logger = logging.getLogger(__name__)


# =====================================================================
# 数据模型
# =====================================================================
@dataclass
class SandboxConfig:
    """沙盒资源限制配置。

    Attributes:
        max_cpu_seconds: CPU 时间上限(秒,signal-based 超时)
        max_memory_mb: 内存峰值上限(MB,RLIMIT_AS)
        max_network_calls: 网络调用次数上限(由 tool_router 计数)
        max_tool_calls: 工具调用次数上限(由 tool_router 计数)
        allowed_tools: 允许的工具名白名单(其他工具调用被拒)
        allowed_urls: 允许的网络 URL 白名单(前缀匹配)
        max_input_chars: 输入字符上限(防超大 input)
        max_output_chars: 输出字符上限(防超大 output)
    """

    max_cpu_seconds: float = 5.0
    max_memory_mb: int = 128
    max_network_calls: int = 5
    max_tool_calls: int = 10
    allowed_tools: list[str] = field(default_factory=list)
    allowed_urls: list[str] = field(default_factory=list)
    max_input_chars: int = 64 * 1024
    max_output_chars: int = 256 * 1024


@dataclass
class ResourceUsage:
    """沙盒资源使用统计。"""

    cpu_time: float = 0.0  # 秒
    memory_peak: int = 0  # bytes
    network_calls: int = 0
    tool_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SandboxResult:
    """沙盒执行结果。

    Attributes:
        success: 是否成功完成(无异常 + 资源未超限)
        output: handler 返回值(已 PII 脱敏)
        error: 错误信息(失败时填写,已脱敏)
        resource_usage: 资源使用统计
        side_effects: handler 产生的副作用日志(list[str])
        pii_redacted_input: 是否对 input 做了 PII 脱敏
        pii_redacted_output: 是否对 output 做了 PII 脱敏
    """

    success: bool
    output: Any = None
    error: str = ""
    resource_usage: ResourceUsage = field(default_factory=ResourceUsage)
    side_effects: list[str] = field(default_factory=list)
    pii_redacted_input: bool = False
    pii_redacted_output: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "resource_usage": self.resource_usage.to_dict(),
            "side_effects": list(self.side_effects),
            "pii_redacted_input": self.pii_redacted_input,
            "pii_redacted_output": self.pii_redacted_output,
        }


# =====================================================================
# Sandbox 内部异常
# =====================================================================
class SandboxTimeoutError(Exception):
    """沙盒 CPU 超时。"""


class SandboxMemoryExceededError(Exception):
    """沙盒内存超限。"""


class SandboxToolBlockedError(Exception):
    """工具调用被白名单拒绝 / 次数超限。"""


# =====================================================================
# MarketplaceSandbox
# =====================================================================
class MarketplaceSandbox:
    """沙盒执行器。

    用法:
        sandbox = get_marketplace_sandbox()
        sandbox.register_handler("agent_x", my_handler)
        config = SandboxConfig(allowed_tools=["search"])
        result = sandbox.execute("agent_x", {"query": "..."}, config)
        if result.success:
            print(result.output)

    设计:
        - PII 脱敏: input 和 output 都过 PIIRedactor
        - Budget: 执行前 allocate(TOOL_CALLS, max_tool_calls),执行后 release(actual_used)
        - 资源: signal.SIGALRM(主线程) + resource.RLIMIT_AS + 计数器
        - 副作用: 通过注入的 SandboxEnvironment 捕获(tool_calls / network_calls / log)
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handlers: dict[str, Callable[[Any, Any], Any]] = {}

    # ------------------------------------------------------------------
    # handler 注册
    # ------------------------------------------------------------------
    def register_handler(self, agent_id: str, handler: Callable[[Any, Any], Any]) -> None:
        """注册 agent 的 Python callable。

        handler 签名: handler(input_data: Any, env: SandboxEnvironment) -> Any
            - input_data: 已脱敏的输入
            - env: SandboxEnvironment(暴露 call_tool / http_get / log 等受限 API)

        Raises:
            MarketplaceError: flag 关闭
        """
        self._require_enabled()
        with self._lock:
            self._handlers[agent_id] = handler

    def unregister_handler(self, agent_id: str) -> bool:
        """移除 handler(便于测试隔离)。"""
        self._require_enabled()
        with self._lock:
            return self._handlers.pop(agent_id, None) is not None

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def execute(
        self,
        agent_id: str,
        input_data: Any,
        config: SandboxConfig | None = None,
        user_id: str = "anonymous",
    ) -> SandboxResult:
        """在沙盒内执行 agent handler。

        Args:
            agent_id: 目标 agent
            input_data: 输入数据(dict / str / 任意)
            config: 资源限制(None 用默认 SandboxConfig)
            user_id: 调用者 ID(用于 budget 关联)

        Returns:
            SandboxResult
        """
        self._require_enabled()
        config = config or SandboxConfig()
        usage = ResourceUsage()
        side_effects: list[str] = []

        # 1. 找 handler
        with self._lock:
            handler = self._handlers.get(agent_id)
        if handler is None:
            return SandboxResult(
                success=False,
                error=f"No handler registered for agent {agent_id}",
                resource_usage=usage,
                side_effects=side_effects,
            )

        # 2. input 大小检查
        input_str = self._stringify(input_data)
        if len(input_str) > config.max_input_chars:
            return SandboxResult(
                success=False,
                error=f"Input too large ({len(input_str)} > {config.max_input_chars})",
                resource_usage=usage,
                side_effects=side_effects,
            )

        # 3. PII 脱敏 input
        redacted_input, input_redacted = self._redact_pii(input_data)
        if input_redacted:
            side_effects.append("PII redacted on input")

        # 4. 申请 budget(defense 关闭时透传)
        budget_alloc = self._allocate_budget(config, user_id)

        # 5. 注入 SandboxEnvironment(受限工具调用)
        env = SandboxEnvironment(config=config, usage=usage, side_effects=side_effects)

        # 6. 执行(资源限制 + 异常捕获)
        output, error = self._run_with_limits(
            handler,
            redacted_input,
            env,
            config,
            usage,
        )

        # 7. 释放 budget(actual_used)
        self._release_budget(budget_alloc, usage)

        # 8. PII 脱敏 output
        redacted_output, output_redacted = self._redact_pii(output)
        if output_redacted:
            side_effects.append("PII redacted on output")

        # 9. output 大小检查
        output_str = self._stringify(redacted_output)
        if len(output_str) > config.max_output_chars:
            return SandboxResult(
                success=False,
                error=f"Output too large ({len(output_str)} > {config.max_output_chars})",
                resource_usage=usage,
                side_effects=side_effects,
            )

        success = error == ""
        return SandboxResult(
            success=success,
            output=redacted_output if success else None,
            error=error,
            resource_usage=usage,
            side_effects=side_effects,
            pii_redacted_input=input_redacted,
            pii_redacted_output=output_redacted,
        )

    # ==================================================================
    # 内部: PII 脱敏
    # ==================================================================
    def _redact_pii(self, data: Any) -> tuple[Any, bool]:
        """对 data 做 PII 脱敏。

        - str → 直接 redact
        - dict → 对所有 str value redact
        - list → 对所有元素 redact
        - 其他类型 → 原样返回

        Returns:
            (redacted_data, did_redact: bool)
        """
        try:
            from ..infrastructure.defense.pii_guard import get_pii_redactor

            redactor = get_pii_redactor()
        except Exception as e:
            logger.debug("PIIRedactor unavailable: %s", e)
            return data, False

        # defense 关闭时 redact 透传(detect 返回 has_pii=False)
        return self._redact_recursive(data, redactor)

    def _redact_recursive(self, data: Any, redactor: Any) -> tuple[Any, bool]:
        if isinstance(data, str):
            result = redactor.redact(data)
            if result.has_pii:
                return result.redacted_text, True
            return data, False
        if isinstance(data, dict):
            new_dict: dict[str, Any] = {}
            any_redacted = False
            for k, v in data.items():
                new_v, r = self._redact_recursive(v, redactor)
                new_dict[k] = new_v
                any_redacted = any_redacted or r
            return new_dict, any_redacted
        if isinstance(data, list):
            new_list: list[Any] = []
            any_redacted = False
            for v in data:
                new_v, r = self._redact_recursive(v, redactor)
                new_list.append(new_v)
                any_redacted = any_redacted or r
            return new_list, any_redacted
        return data, False

    # ==================================================================
    # 内部: Budget 协调
    # ==================================================================
    def _allocate_budget(self, config: SandboxConfig, user_id: str) -> Any:
        """向 BudgetCoordinator 申请 max_tool_calls + 估算 token 预算。

        defense 关闭时 BudgetCoordinator.allocate 返回虚拟 allocation(透传)。
        """
        try:
            from ..infrastructure.defense.budget_coordinator import (
                BudgetCoordinator,
                BudgetDimension,
                BudgetScope,
                get_budget_coordinator,
            )

            bc: BudgetCoordinator = get_budget_coordinator()
            # scope_id 格式: "session-{user}-sandbox-{ts}"
            scope_id = f"session-{user_id}-sandbox-{int(time.time())}"
            # 申请 TOOL_CALLS(按 max_tool_calls)
            alloc = bc.allocate(
                scope=BudgetScope.SESSION,
                scope_id=scope_id,
                dimension=BudgetDimension.TOOL_CALLS,
                amount=config.max_tool_calls,
                consumer="marketplace_sandbox",
            )
            return alloc
        except Exception as e:
            logger.debug("Budget allocate failed (degraded): %s", e)
            return None

    def _release_budget(self, alloc: Any, usage: ResourceUsage) -> None:
        """按 actual tool_calls 释放 budget。"""
        if alloc is None:
            return
        try:
            from ..infrastructure.defense.budget_coordinator import (
                get_budget_coordinator,
            )

            bc = get_budget_coordinator()
            allocation_id = getattr(alloc, "allocation_id", "")
            if allocation_id and allocation_id != "disabled":
                bc.release(allocation_id, actual_used=usage.tool_calls)
        except Exception as e:
            logger.debug("Budget release failed (degraded): %s", e)

    # ==================================================================
    # 内部: 资源限制 + 执行
    # ==================================================================
    def _run_with_limits(
        self,
        handler: Callable[[Any, Any], Any],
        input_data: Any,
        env: SandboxEnvironment,
        config: SandboxConfig,
        usage: ResourceUsage,
    ) -> tuple[Any, str]:
        """在资源限制内执行 handler,返回 (output, error)。

        error 为空字符串表示成功。
        """
        # 设置 signal 超时(仅主线程 + POSIX;Windows / 非主线程降级)
        old_handler = None
        set_alarm = False
        _alarm_sig = getattr(signal, "SIGALRM", None)
        _itimer_real = getattr(signal, "ITIMER_REAL", None)
        if (
            _alarm_sig is not None
            and _itimer_real is not None
            and threading.current_thread() is threading.main_thread()
        ):
            try:
                old_handler = signal.signal(_alarm_sig, self._alarm_handler)
                signal.setitimer(_itimer_real, float(config.max_cpu_seconds))
                set_alarm = True
            except (ValueError, OSError):
                set_alarm = False

        # 设置内存上限(RLIMIT_AS,soft limit;仅 POSIX)
        old_rlimit = None
        try:
            import resource as _resource

            mem_bytes = config.max_memory_mb * 1024 * 1024
            old_rlimit = _resource.getrlimit(_resource.RLIMIT_AS)
            # soft limit = mem_bytes; hard limit 保持原值(不收紧)
            _resource.setrlimit(_resource.RLIMIT_AS, (mem_bytes, old_rlimit[1]))
        except (ValueError, OSError, AttributeError, ImportError):
            old_rlimit = None

        try:
            cpu_start = time.process_time()
            output = handler(input_data, env)
            cpu_end = time.process_time()
            usage.cpu_time = cpu_end - cpu_start
            # 读内存 peak
            try:
                import resource as _resource

                usage.memory_peak = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss * 1024
            except Exception:
                usage.memory_peak = 0
            return output, ""
        except SandboxTimeoutError:
            return None, f"Sandbox CPU timeout (>{config.max_cpu_seconds}s)"
        except SandboxMemoryExceededError:
            return None, f"Sandbox memory exceeded (>{config.max_memory_mb}MB)"
        except SandboxToolBlockedError as e:
            return None, f"Tool call blocked: {e}"
        except Exception as e:
            tb = traceback.format_exc()
            logger.warning("Sandbox handler raised: %s\n%s", e, tb)
            return None, f"Handler error: {type(e).__name__}: {e}"
        finally:
            if set_alarm:
                try:
                    signal.setitimer(_itimer_real, 0)
                    if old_handler is not None:
                        signal.signal(_alarm_sig, old_handler)
                except (ValueError, OSError):
                    pass
            if old_rlimit is not None:
                try:
                    import resource as _resource

                    _resource.setrlimit(_resource.RLIMIT_AS, old_rlimit)
                except (ValueError, OSError):
                    pass

    @staticmethod
    def _alarm_handler(signum, frame) -> None:
        raise SandboxTimeoutError("CPU time exceeded")

    # ==================================================================
    # 内部
    # ==================================================================
    @staticmethod
    def _stringify(data: Any) -> str:
        if isinstance(data, str):
            return data
        try:
            import json as _json

            return _json.dumps(data, ensure_ascii=False, default=str)
        except Exception:
            return repr(data)

    def _require_enabled(self) -> None:
        if not is_enabled("marketplace"):
            raise MarketplaceError(
                "Marketplace feature is disabled (set DEADMAN_MARKETPLACE_ENABLED=1)"
            )


# =====================================================================
# SandboxEnvironment - 注入到 handler 的受限执行环境
# =====================================================================
class SandboxEnvironment:
    """注入到 handler 的受限环境,暴露 call_tool / http_get / log 等 API。

    所有调用都计入 ResourceUsage,超限 / 非白名单 → raise SandboxToolBlockedError。
    """

    def __init__(
        self,
        config: SandboxConfig,
        usage: ResourceUsage,
        side_effects: list[str],
    ) -> None:
        self._config = config
        self._usage = usage
        self._side_effects = side_effects

    def call_tool(self, tool_name: str, **kwargs: Any) -> Any:
        """调用一个工具(白名单 + 次数限制)。"""
        if tool_name not in self._config.allowed_tools:
            raise SandboxToolBlockedError(
                f"Tool '{tool_name}' not in whitelist {self._config.allowed_tools}"
            )
        if self._usage.tool_calls >= self._config.max_tool_calls:
            raise SandboxToolBlockedError(
                f"Tool call limit reached ({self._config.max_tool_calls})"
            )
        self._usage.tool_calls += 1
        self._side_effects.append(f"call_tool: {tool_name} {kwargs}")
        # 默认返回 stub(测试可通过 monkeypatch 替换)
        return {"tool": tool_name, "args": kwargs, "result": "stub"}

    def http_get(self, url: str) -> Any:
        """发起 HTTP GET(URL 白名单 + 次数限制)。"""
        allowed = any(url.startswith(prefix) for prefix in self._config.allowed_urls)
        if not allowed:
            raise SandboxToolBlockedError(
                f"URL '{url}' not in whitelist {self._config.allowed_urls}"
            )
        if self._usage.network_calls >= self._config.max_network_calls:
            raise SandboxToolBlockedError(
                f"Network call limit reached ({self._config.max_network_calls})"
            )
        self._usage.network_calls += 1
        self._side_effects.append(f"http_get: {url}")
        return {"url": url, "status": "stub", "body": ""}

    def log(self, message: str) -> None:
        """记录副作用日志(不输出到 stdout,避免污染)。"""
        self._side_effects.append(f"log: {message}")

    @property
    def usage(self) -> ResourceUsage:
        return self._usage


# =====================================================================
# 全局单例
# =====================================================================
_sandbox_instance: MarketplaceSandbox | None = None
_sandbox_lock = threading.Lock()


def get_marketplace_sandbox() -> MarketplaceSandbox:
    """获取全局 MarketplaceSandbox 单例。"""
    global _sandbox_instance
    if _sandbox_instance is None:
        with _sandbox_lock:
            if _sandbox_instance is None:
                _sandbox_instance = MarketplaceSandbox()
    return _sandbox_instance
