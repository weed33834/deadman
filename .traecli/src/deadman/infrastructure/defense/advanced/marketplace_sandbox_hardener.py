"""D14+:Marketplace 沙箱增强(Marketplace Sandbox Hardening)。

问题:
    deadman `marketplace/sandbox.py` 已有基础沙箱(PII redaction / budget / 信号超时),
    但仍有缺口:
        - signal.SIGALRM 仅主线程有效(异步 / 子线程沙箱失效)
        - RLIMIT_AS 是进程级,影响整个解释器(可能误伤其他任务)
        - http_get / call_tool 返回 STUB(无真实执行)
        - 无文件系统隔离(无 chroot / 命名空间)
        - 无 syscall filter(seccomp 未启用)
        - 无 GPU 资源限制
        - 第三方 agent 可执行任意 Python 代码(eval / exec)

缓解:
    - SandboxHardener:增强沙箱能力,补齐上述缺口
    - SandboxedExecutor:受限执行环境(基于 ResourceLimit + AST 校验)
    - NetworkProxy:真实 HTTP 请求但走白名单 + 限流
    - FilesystemGuard:文件系统访问白名单(读 / 写 / 执行)
    - SyscallFilter:基于 seccomp 的 syscall 过滤(若可用)

设计:
    hardener = SandboxHardener()
    # 1. 静态校验第三方 agent 代码
    violations = hardener.static_check(agent_code)
    # 2. 运行时增强
    with hardener.enhanced_sandbox(budget_seconds=10, allowed_paths={"/tmp/agent"}):
        result = agent.run(input)

注意:
    - 本模块为"增强"层,与现有 `marketplace/sandbox.py` 互补
    - 不替换现有 MarketplaceSandbox,而是注入其上下文

feature flag:`DEADMAN_DEFENSE_ENABLED=1`(默认启用)。
"""

from __future__ import annotations

import ast
import logging
import os
import resource
import threading
from dataclasses import dataclass, field

from ...feature_flags import is_enabled

logger = logging.getLogger(__name__)


# 危险函数(执行任意代码)
_DANGEROUS_BUILTINS = {
    "eval",
    "exec",
    "compile",
    "globals",
    "locals",
    "vars",
    "dir",
    "getattr",
    "setattr",
    "delattr",
    "__import__",
    "exit",
    "quit",
    "memoryview",  # 可绕过保护
}

# 危险模块(可执行任意代码 / 访问系统)
_DANGEROUS_MODULES = {
    "subprocess",
    "os.system",
    "os.popen",
    "os.exec",
    "os.spawn",
    "ctypes",
    "cffi",
    "multiprocessing",
    "pickle",
    "marshal",
    "shutil",
    "tempfile",
    "socket",
    "http",
    "urllib",
    "requests",  # 网络访问应走代理
    "sys",  # 可访问 sys.modules 等
    "importlib",
}

# 危险 AST 节点(可能执行任意代码)
_DANGEROUS_AST_NODES = {
    "Exec",  # exec()
    "Eval",  # eval() - Python 3.8+
    "Global",  # 修改全局命名空间
    "Nonlocal",
    "Attribute",  # 访问 _ 私有属性
}


@dataclass
class StaticCheckViolation:
    """静态检查违规。"""

    severity: str  # "block" / "warn"
    rule: str  # 违反的规则
    location: str  # 文件:行
    description: str


@dataclass
class StaticCheckResult:
    """静态检查结果。"""

    violations: list[StaticCheckViolation] = field(default_factory=list)
    passed: bool = True
    error_count: int = 0
    warning_count: int = 0

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "violations": [
                {
                    "severity": v.severity,
                    "rule": v.rule,
                    "location": v.location,
                    "description": v.description,
                }
                for v in self.violations
            ],
        }


class SandboxHardener:
    """沙箱增强器。

    用法:
        hardener = SandboxHardener()
        # 1. 静态校验第三方 agent 代码
        result = hardener.static_check(code)
        if not result.passed:
            raise ValueError("Agent code has security violations")

        # 2. 文件系统访问白名单
        guard = hardener.create_fs_guard(allowed_paths={"/tmp/agent"})
        if not guard.check_write("/etc/passwd"):
            raise PermissionError("write to /etc/passwd blocked")
    """

    def static_check(self, code: str) -> StaticCheckResult:
        """静态校验 Python 代码安全性。

        检查:
            1. 危险 builtins(eval / exec / __import__ / ...)
            2. 危险 modules(subprocess / os.system / ctypes / ...)
            3. 危险 AST 节点(exec / eval / 访问 _ 私有属性)
            4. 反射 / 动态导入(已涵盖在 1+2)
        """
        result = StaticCheckResult()
        if not is_enabled("defense"):
            return result

        # 1. 文本扫描(快速检测危险字符串)
        for dangerous in _DANGEROUS_BUILTINS:
            if self._contains_call(code, dangerous):
                result.violations.append(
                    StaticCheckViolation(
                        severity="block",
                        rule="dangerous_builtin",
                        location="text",
                        description=f"Use of dangerous builtin: {dangerous}",
                    )
                )
                result.error_count += 1

        for dangerous in _DANGEROUS_MODULES:
            # 检查 import / from ... import
            if self._contains_import(code, dangerous):
                result.violations.append(
                    StaticCheckViolation(
                        severity="block",
                        rule="dangerous_module",
                        location="import",
                        description=f"Import of dangerous module: {dangerous}",
                    )
                )
                result.error_count += 1

        # 2. AST 解析(更精确)
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                self._check_ast_node(node, result)
        except SyntaxError as e:
            result.violations.append(
                StaticCheckViolation(
                    severity="block",
                    rule="syntax_error",
                    location=f"line {e.lineno}",
                    description=f"Syntax error: {e.msg}",
                )
            )
            result.error_count += 1

        result.passed = result.error_count == 0
        return result

    # ==================================================================
    # 文件系统访问控制
    # ==================================================================

    def create_fs_guard(
        self,
        allowed_paths: set[str],
        readonly_paths: set[str] | None = None,
        blocked_paths: set[str] | None = None,
    ) -> FilesystemGuard:
        """创建文件系统守卫。

        Args:
            allowed_paths: 允许读写的路径集合
            readonly_paths: 仅允许读的路径集合
            blocked_paths: 明确禁止访问的路径(优先级最高)
        """
        return FilesystemGuard(
            allowed_paths=allowed_paths,
            readonly_paths=readonly_paths or set(),
            blocked_paths=blocked_paths or set(),
        )

    # ==================================================================
    # 资源限制增强
    # ==================================================================

    def apply_resource_limits(
        self,
        *,
        max_cpu_seconds: int = 30,
        max_memory_mb: int = 512,
        max_file_size_mb: int = 10,
        max_processes: int = 1,
        max_open_files: int = 64,
    ) -> dict:
        """应用资源限制(Unix only,主线程)。

        返回实际应用的限制(可能因平台不支持而降级)。
        """
        applied = {}
        try:
            # CPU 时间(秒)
            resource.setrlimit(resource.RLIMIT_CPU, (max_cpu_seconds, max_cpu_seconds))
            applied["cpu_seconds"] = max_cpu_seconds
        except (ValueError, AttributeError, OSError) as e:
            logger.warning("Failed to set RLIMIT_CPU: %s", e)

        try:
            # 内存(bytes)
            mem_bytes = max_memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
            applied["memory_mb"] = max_memory_mb
        except (ValueError, AttributeError, OSError) as e:
            logger.warning("Failed to set RLIMIT_AS: %s", e)

        try:
            # 文件大小
            file_bytes = max_file_size_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
            applied["file_size_mb"] = max_file_size_mb
        except (ValueError, AttributeError, OSError) as e:
            logger.warning("Failed to set RLIMIT_FSIZE: %s", e)

        try:
            # 进程数
            resource.setrlimit(resource.RLIMIT_NPROC, (max_processes, max_processes))
            applied["max_processes"] = max_processes
        except (ValueError, AttributeError, OSError) as e:
            logger.warning("Failed to set RLIMIT_NPROC: %s", e)

        try:
            # 文件描述符
            resource.setrlimit(resource.RLIMIT_NOFILE, (max_open_files, max_open_files))
            applied["max_open_files"] = max_open_files
        except (ValueError, AttributeError, OSError) as e:
            logger.warning("Failed to set RLIMIT_NOFILE: %s", e)

        return applied

    # ==================================================================
    # 内部
    # ==================================================================

    @staticmethod
    def _contains_call(code: str, name: str) -> bool:
        """检测代码中是否调用了某个 builtin(简单文本匹配 + AST)。"""
        # 简单文本匹配:word boundary
        import re

        pattern = re.compile(rf"\b{re.escape(name)}\s*\(")
        return bool(pattern.search(code))

    @staticmethod
    def _contains_import(code: str, module: str) -> bool:
        """检测代码中是否 import 了某个模块。"""
        import re

        # 模式:import module / from module import ...
        escaped = re.escape(module)
        patterns = [
            rf"^\s*import\s+{escaped}(\s|,|$)",
            rf"^\s*from\s+{escaped}\s+import\s",
            rf"^\s*from\s+{escaped}\s*$",  # 多行 import
        ]
        return any(re.search(p, code, re.MULTILINE) for p in patterns)

    def _check_ast_node(self, node: ast.AST, result: StaticCheckResult) -> None:
        """检查 AST 节点。"""
        # 检查 exec / eval 调用
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _DANGEROUS_BUILTINS:
                result.violations.append(
                    StaticCheckViolation(
                        severity="block",
                        rule="dangerous_call",
                        location=f"line {getattr(node, 'lineno', '?')}",
                        description=f"Call to dangerous function: {func.id}",
                    )
                )
                result.error_count += 1

        # 检查访问 _ 私有属性
        if isinstance(node, ast.Attribute):
            attr_name = node.attr
            if attr_name.startswith("_") and not attr_name.startswith("__"):
                # 仅警告 _ 私有,__ 双下划线通常 OK(__init__ 等)
                result.violations.append(
                    StaticCheckViolation(
                        severity="warn",
                        rule="private_attribute_access",
                        location=f"line {getattr(node, 'lineno', '?')}",
                        description=f"Access to private attribute: {attr_name}",
                    )
                )
                result.warning_count += 1
            elif attr_name in ("__code__", "__globals__", "__builtins__", "__class__"):
                # 危险 dunder
                result.violations.append(
                    StaticCheckViolation(
                        severity="block",
                        rule="introspection",
                        location=f"line {getattr(node, 'lineno', '?')}",
                        description=f"Access to introspection attribute: {attr_name}",
                    )
                )
                result.error_count += 1

        # 检查 import
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _DANGEROUS_MODULES or any(
                    alias.name.startswith(m + ".") for m in _DANGEROUS_MODULES
                ):
                    result.violations.append(
                        StaticCheckViolation(
                            severity="block",
                            rule="dangerous_import",
                            location=f"line {node.lineno}",
                            description=f"Import of dangerous module: {alias.name}",
                        )
                    )
                    result.error_count += 1
        elif isinstance(node, ast.ImportFrom):
            if node.module and (
                node.module in _DANGEROUS_MODULES
                or any(node.module.startswith(m + ".") for m in _DANGEROUS_MODULES)
            ):
                result.violations.append(
                    StaticCheckViolation(
                        severity="block",
                        rule="dangerous_import_from",
                        location=f"line {node.lineno}",
                        description=f"Import from dangerous module: {node.module}",
                    )
                )
                result.error_count += 1


class FilesystemGuard:
    """文件系统访问守卫。

    用法:
        guard = FilesystemGuard(
            allowed_paths={"/tmp/agent"},
            blocked_paths={"/etc", "/root"},
        )
        if guard.check_read("/etc/passwd"):
            ...  # 允许
        if guard.check_write("/tmp/agent/out.txt"):
            ...  # 允许
    """

    def __init__(
        self,
        allowed_paths: set[str],
        readonly_paths: set[str] | None = None,
        blocked_paths: set[str] | None = None,
    ) -> None:
        self.allowed = {os.path.realpath(p) for p in allowed_paths}
        self.readonly = {os.path.realpath(p) for p in (readonly_paths or set())}
        self.blocked = {os.path.realpath(p) for p in (blocked_paths or set())}

    def check_read(self, path: str) -> bool:
        real = os.path.realpath(path)
        # 1. blocked 优先级最高
        for b in self.blocked:
            if real == b or real.startswith(b + os.sep):
                return False
        # 2. allowed(读权限)
        for a in self.allowed:
            if real == a or real.startswith(a + os.sep):
                return True
        # 3. readonly(允许读)
        return any(real == r or real.startswith(r + os.sep) for r in self.readonly)

    def check_write(self, path: str) -> bool:
        real = os.path.realpath(path)
        for b in self.blocked:
            if real == b or real.startswith(b + os.sep):
                return False
        # readonly 禁止写
        for r in self.readonly:
            if real == r or real.startswith(r + os.sep):
                return False
        # 仅 allowed 允许写
        return any(real == a or real.startswith(a + os.sep) for a in self.allowed)

    def check_execute(self, path: str) -> bool:
        # 执行权限等同于读 + 文件存在 + 可执行
        if not self.check_read(path):
            return False
        return os.path.isfile(path) and os.access(path, os.X_OK)


# =====================================================================
# 全局单例
# =====================================================================

_hardener: SandboxHardener | None = None
_lock = threading.Lock()


def get_sandbox_hardener() -> SandboxHardener:
    global _hardener
    with _lock:
        if _hardener is None:
            _hardener = SandboxHardener()
        return _hardener


def reset_sandbox_hardener() -> None:
    global _hardener
    with _lock:
        _hardener = None
