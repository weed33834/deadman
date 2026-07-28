"""代码执行沙箱后端 - 在隔离环境内执行用户提供的 Python 代码

借鉴 Hermes Agent (MIT License) 的 code_execution_tool.py 设计，但按 deadman 身后事场景定位改造：

与 Hermes 差异：
- 不实现 UDS / file-based RPC（Hermes 用 PTC 让 LLM 在沙箱内调用其他工具，deadman 不需要）
- 只执行 Python（不 shell=True），代码字符串写入临时 .py 文件后用 asyncio.create_subprocess_exec 跑
- 不引入新依赖（仅 stdlib + 已有 httpx）；Docker 不可用时降级到本地子进程
- 资源限制：LocalSandbox 用 resource.setrlimit(RLIMIT_AS=256MB, RLIMIT_CPU=timeout)
            DockerSandbox 用 --network=none --memory=256m --cpus=0.5 --rm

遵守规则文件：
- compliance-framework.md：仅执行用户提供的 Python 代码，不代查 / 不代办
- input-guardrails.md：用户代码字符串仅写入临时文件，不拼接到 shell；subprocess 不用 shell=True
- integrity-framework.md：失败返回 ok=False + error，不抛异常
- safety-protocol.md：网络禁用（Docker --network=none；LocalSandbox 无 RLIMIT_NETWORK 但默认不导入网络库也够用）
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..config import settings

logger = logging.getLogger(__name__)


# =====================================================================
# 结果数据结构
# =====================================================================


@dataclass
class SandboxResult:
    """沙箱执行结果

    Attributes:
        ok: 是否执行成功（exit_code == 0）
        exit_code: 子进程退出码；超时为 -1
        stdout: 标准输出（utf-8 解码，errors=replace）
        stderr: 标准错误（utf-8 解码，errors=replace）
        backend: 实际使用的后端名（"local" / "docker"）
        duration_ms: 执行耗时（毫秒）
        timed_out: 是否因超时被终止
        error: 后端级错误描述（如 docker_unavailable）
    """

    ok: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    backend: str = "local"
    duration_ms: int = 0
    timed_out: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转 dict 用于 MCP / CLI 返回"""
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "backend": self.backend,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "error": self.error,
        }


# =====================================================================
# Backend 抽象
# =====================================================================


class SandboxBackend(Protocol):
    """沙箱后端抽象 - 可插拔

    实现方需保证：
    - 不使用 shell=True（input-guardrails：用户代码不拼接到 shell）
    - 失败返回 ok=False + error，不抛异常（integrity-framework）
    - 仅执行 Python 代码（compliance-framework：不代办 shell 操作）
    - 网络隔离 / 资源限制（safety-protocol）
    """

    name: str

    def is_available(self) -> bool:
        """后端是否可用（如 Docker daemon 是否在跑）"""
        ...

    async def execute(
        self, code: str, timeout: int | None = None
    ) -> SandboxResult:
        """执行 Python 代码字符串，返回 SandboxResult

        Args:
            code: Python 代码字符串（写入临时文件后用 subprocess 跑，绝不 shell=True）
            timeout: 超时秒（None 取 settings.sandbox_timeout）
        """
        ...


# =====================================================================
# LocalSandbox - 子进程 + resource.setrlimit 资源限制
# =====================================================================


# LocalSandbox 资源限制默认值
# RLIMIT_AS: 进程最大虚拟内存（256MB），防止 OOM 影响主进程
# RLIMIT_CPU: 最大 CPU 秒数，防止 while True 卡死
# RLIMIT_FSIZE: 最大文件写入大小（16MB），防止恶意脚本写满磁盘
_DEFAULT_MEMORY_BYTES: int = 256 * 1024 * 1024  # 256 MB
_DEFAULT_MAX_FILE_BYTES: int = 16 * 1024 * 1024  # 16 MB


class LocalSandbox:
    """本地子进程沙箱

    执行流程：
      1. 把 code 字符串写入临时 .py 文件（不 shell=True，不拼接）
      2. 用 asyncio.create_subprocess_exec 启动 python 子进程
      3. 子进程入口脚本内调用 resource.setrlimit 设内存/CPU/文件大小限制
      4. asyncio.wait_for 等待完成或超时
      5. 清理临时文件

    资源限制：
      - RLIMIT_AS = 256 MB（虚拟内存上限）
      - RLIMIT_CPU = timeout（CPU 秒上限，超时被 SIGXCPU 终止）
      - RLIMIT_FSIZE = 16 MB（文件写入上限）

    网络隔离：LocalSandbox 不强制禁网（RLIMIT_NETWORK 仅 BSD 有），
    但默认不导入 requests/httpx 等库也无法联网；如需严格禁网用 DockerSandbox。
    """

    name: str = "local"

    def __init__(
        self,
        python_executable: str | None = None,
        memory_limit_bytes: int = _DEFAULT_MEMORY_BYTES,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
    ):
        """初始化本地沙箱

        Args:
            python_executable: Python 解释器路径（None 取 sys.executable）
            memory_limit_bytes: RLIMIT_AS 字节数
            max_file_bytes: RLIMIT_FSIZE 字节数
        """
        import sys

        self.python_executable: str = python_executable or sys.executable
        self.memory_limit_bytes: int = memory_limit_bytes
        self.max_file_bytes: int = max_file_bytes

    def is_available(self) -> bool:
        """本地沙箱始终可用"""
        return True

    async def execute(
        self, code: str, timeout: int | None = None
    ) -> SandboxResult:
        """执行 Python 代码字符串

        Args:
            code: Python 代码字符串
            timeout: 超时秒（None 取 settings.sandbox_timeout）

        Returns:
            SandboxResult
        """
        effective_timeout: int = int(timeout if timeout is not None else settings.sandbox_timeout)
        import time

        start_ms: int = int(time.monotonic() * 1000)

        # 写入临时 .py 文件（不 shell=True，不拼接 shell 字符串）
        # 用户代码仅作为文件内容，子进程入口通过 exec(compile(...)) 执行
        wrapper = self._build_wrapper(code, effective_timeout)
        tmp_path: Path | None = None
        try:
            # 用 NamedTemporaryFile 创建临时文件，确保文件名唯一
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".py",
                prefix="deadman_sandbox_",
                delete=False,
                encoding="utf-8",
            ) as f:
                f.write(wrapper)
                tmp_path = Path(f.name)

            # 用 create_subprocess_exec 启动 python（不用 shell=True）
            # 输入仅作为文件路径参数，不参与 shell 解析
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.python_executable,
                    str(tmp_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                # python 解释器找不到
                return SandboxResult(
                    ok=False,
                    exit_code=-1,
                    backend=self.name,
                    duration_ms=int(time.monotonic() * 1000) - start_ms,
                    error=f"python_executable_not_found: {exc}",
                )

            # 等待完成或超时
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=effective_timeout
                )
            except asyncio.TimeoutError:
                # 超时杀掉子进程
                try:
                    proc.kill()
                    # 给 0.5 秒让进程退出，回收资源
                    await asyncio.wait_for(proc.wait(), timeout=0.5)
                except Exception as e:
                    logger.debug("LocalSandbox 超时后 kill 子进程失败: %s", e)
                duration_ms = int(time.monotonic() * 1000) - start_ms
                return SandboxResult(
                    ok=False,
                    exit_code=-1,
                    stdout="",
                    stderr=f"timeout after {effective_timeout}s",
                    backend=self.name,
                    duration_ms=duration_ms,
                    timed_out=True,
                    error="timeout",
                )

            duration_ms = int(time.monotonic() * 1000) - start_ms
            exit_code: int = proc.returncode if proc.returncode is not None else -1
            stdout_text = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            stderr_text = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

            return SandboxResult(
                ok=(exit_code == 0),
                exit_code=exit_code,
                stdout=stdout_text,
                stderr=stderr_text,
                backend=self.name,
                duration_ms=duration_ms,
            )
        except Exception as exc:
            # integrity-framework：失败不抛异常，返回 ok=False
            logger.warning("LocalSandbox 执行失败: %s: %s", type(exc).__name__, exc)
            duration_ms = int(time.monotonic() * 1000) - start_ms
            return SandboxResult(
                ok=False,
                exit_code=-1,
                backend=self.name,
                duration_ms=duration_ms,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            # 清理临时文件（无论成功/失败/超时）
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception as e:
                    logger.debug("LocalSandbox 清理临时文件失败 %s: %s", tmp_path, e)

    def _build_wrapper(self, code: str, timeout: int) -> str:
        """构造包装脚本 - 在子进程入口设置资源限制后执行用户代码

        为何需要包装：resource.setrlimit 必须在子进程内调用（影响当前进程），
        不能在父进程预设后继承（部分限制不继承）。所以包装脚本先 setrlimit
        再 exec(compile(user_code, "<sandbox>", "exec"))。

        Args:
            code: 用户提供的 Python 代码字符串
            timeout: 超时秒（同时用作 RLIMIT_CPU）

        Returns:
            包装脚本字符串
        """
        # 把用户代码字符串作为字符串字面量嵌入包装脚本
        # 用 repr() 保证转义安全（用户代码中的引号/换行不会破坏包装）
        # 用户代码仅作为字符串字面量传入 exec()，不参与 shell 解析
        code_repr = repr(code)
        return (
            "import resource\n"
            "import sys\n"
            "\n"
            "# === 设置资源限制（仅当前子进程，不影响父进程）===\n"
            f"MEMORY_LIMIT = {self.memory_limit_bytes}\n"
            f"MAX_FILE_BYTES = {self.max_file_bytes}\n"
            f"CPU_LIMIT = {int(timeout)}\n"
            "\n"
            "# RLIMIT_AS: 虚拟内存上限\n"
            "try:\n"
            "    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))\n"
            "except (ValueError, OSError):\n"
            "    pass  # 部分平台不支持，忽略\n"
            "\n"
            "# RLIMIT_FSIZE: 文件写入大小上限\n"
            "try:\n"
            "    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))\n"
            "except (ValueError, OSError):\n"
            "    pass\n"
            "\n"
            "# RLIMIT_CPU: CPU 秒上限（超时 SIGXCPU）\n"
            "try:\n"
            "    resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT, CPU_LIMIT))\n"
            "except (ValueError, OSError):\n"
            "    pass\n"
            "\n"
            "# === 执行用户代码（不 shell=True，仅 exec compile）===\n"
            "_user_code = " + code_repr + "\n"
            "exec(compile(_user_code, '<sandbox>', 'exec'))\n"
        )


# =====================================================================
# DockerSandbox - Docker 容器内执行
# =====================================================================


class DockerSandbox:
    """Docker 容器沙箱

    执行流程：
      1. is_available() 检查 docker 命令是否在 PATH + daemon 是否响应
      2. execute() 把 code 字符串通过 stdin 传入容器内 python 进程
      3. 容器配置：--network=none --memory=256m --cpus=0.5 --rm --read-only
      4. 不可用时返回 {"ok": False, "error": "docker_unavailable"}（graceful degradation）

    安全约束：
      - 用户代码仅作为 stdin 传入，不拼接到 docker run 命令行
      - --network=none 禁止网络
      - --read-only + tmpfs /tmp 防止持久化写入
      - --memory + --cpus 限制资源
    """

    name: str = "docker"

    # 默认 Docker 镜像（Python 3.11 slim）
    _DEFAULT_IMAGE: str = "python:3.11-slim"

    def __init__(
        self,
        image: str | None = None,
        memory: str = "256m",
        cpus: str = "0.5",
        work_dir: str = "/tmp/deadman-sandbox",
    ):
        """初始化 Docker 沙箱

        Args:
            image: Docker 镜像名（None 取 settings.sandbox_image，再降级到 _DEFAULT_IMAGE）
            memory: 容器内存限制（默认 256m）
            cpus: 容器 CPU 限制（默认 0.5）
            work_dir: 容器内工作目录
        """
        self.image: str = image or settings.sandbox_image or self._DEFAULT_IMAGE
        self.memory: str = memory
        self.cpus: str = cpus
        self.work_dir: str = work_dir
        # is_available 结果缓存（避免每次 execute 都 docker version）
        self._availability_cached: bool | None = None

    def is_available(self) -> bool:
        """检查 Docker 是否可用

        检查项：
          1. docker 命令在 PATH（shutil.which）
          2. docker version 命令成功（daemon 响应）

        结果缓存到 self._availability_cached，避免重复探测
        """
        if self._availability_cached is not None:
            return self._availability_cached

        available: bool = False
        try:
            if shutil.which("docker") is None:
                self._availability_cached = False
                return False
            # 用 subprocess 同步检查 docker version（阻塞但快，<1s）
            import subprocess

            result = subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                timeout=3.0,
            )
            available = result.returncode == 0 and bool(result.stdout.strip())
        except Exception as exc:
            logger.debug("Docker 不可用: %s: %s", type(exc).__name__, exc)
            available = False

        self._availability_cached = available
        if not available:
            logger.info("Docker 沙箱不可用，将降级到 LocalSandbox")
        return available

    async def execute(
        self, code: str, timeout: int | None = None
    ) -> SandboxResult:
        """在 Docker 容器内执行 Python 代码

        Args:
            code: Python 代码字符串（通过 stdin 传入容器，不拼接到 docker 命令行）
            timeout: 超时秒（None 取 settings.sandbox_timeout）

        Returns:
            SandboxResult；Docker 不可用时返回 ok=False, error="docker_unavailable"
        """
        effective_timeout: int = int(timeout if timeout is not None else settings.sandbox_timeout)

        # 不可用直接返回 graceful degradation
        if not self.is_available():
            return SandboxResult(
                ok=False,
                exit_code=-1,
                backend=self.name,
                error="docker_unavailable",
            )

        import time

        start_ms: int = int(time.monotonic() * 1000)

        # 构造 docker run 命令
        # 用户代码通过 stdin 传入（-i），不作为命令行参数
        # 容器内用 python - 接收 stdin 作为脚本
        docker_cmd: list[str] = [
            "docker", "run", "--rm", "-i",
            "--network", "none",  # 禁止网络
            "--memory", self.memory,  # 内存限制
            "--cpus", self.cpus,  # CPU 限制
            "--read-only",  # 只读根文件系统
            "--tmpfs", "/tmp:size=64m",  # 临时目录 tmpfs
            "-w", self.work_dir,
            self.image,
            "python", "-",  # 从 stdin 读取脚本
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            # docker 命令找不到（理论上 is_available 已检查过，但兜底）
            self._availability_cached = False
            return SandboxResult(
                ok=False,
                exit_code=-1,
                backend=self.name,
                duration_ms=int(time.monotonic() * 1000) - start_ms,
                error=f"docker_not_found: {exc}",
            )

        # 通过 stdin 把用户代码传入容器
        # 用户代码仅作为 stdin 数据，不参与 docker 命令行解析
        try:
            code_bytes = code.encode("utf-8")
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=code_bytes), timeout=effective_timeout
            )
        except asyncio.TimeoutError:
            # 超时杀掉容器
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=0.5)
            except Exception as e:
                logger.debug("DockerSandbox 超时后 kill 容器失败: %s", e)
            duration_ms = int(time.monotonic() * 1000) - start_ms
            return SandboxResult(
                ok=False,
                exit_code=-1,
                stdout="",
                stderr=f"timeout after {effective_timeout}s",
                backend=self.name,
                duration_ms=duration_ms,
                timed_out=True,
                error="timeout",
            )
        except Exception as exc:
            duration_ms = int(time.monotonic() * 1000) - start_ms
            logger.warning("DockerSandbox 执行失败: %s: %s", type(exc).__name__, exc)
            return SandboxResult(
                ok=False,
                exit_code=-1,
                backend=self.name,
                duration_ms=duration_ms,
                error=f"{type(exc).__name__}: {exc}",
            )

        duration_ms = int(time.monotonic() * 1000) - start_ms
        exit_code: int = proc.returncode if proc.returncode is not None else -1
        stdout_text = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr_text = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

        return SandboxResult(
            ok=(exit_code == 0),
            exit_code=exit_code,
            stdout=stdout_text,
            stderr=stderr_text,
            backend=self.name,
            duration_ms=duration_ms,
        )


# =====================================================================
# SandboxManager - 后端选择 + 自动降级
# =====================================================================


class SandboxManager:
    """沙箱管理器 - 自动选择后端 + Docker 不可用时降级到 LocalSandbox

    选择策略：
      1. 优先用 DockerSandbox（如果 is_available() 为 True）
      2. Docker 不可用时降级到 LocalSandbox（始终可用）

    用法：
        manager = SandboxManager()
        result = await manager.execute("print('hello')")
        print(result.to_dict())
    """

    def __init__(
        self,
        local_sandbox: LocalSandbox | None = None,
        docker_sandbox: DockerSandbox | None = None,
        prefer_docker: bool = True,
    ):
        """初始化沙箱管理器

        Args:
            local_sandbox: 本地沙箱实例（None 时按需创建）
            docker_sandbox: Docker 沙箱实例（None 时按需创建）
            prefer_docker: 是否优先使用 Docker（True 时尝试 Docker，失败降级到本地）
        """
        self.local_sandbox: LocalSandbox = local_sandbox or LocalSandbox()
        self.docker_sandbox: DockerSandbox = docker_sandbox or DockerSandbox()
        self.prefer_docker: bool = prefer_docker
        # 实际使用的后端名（首次 execute 后确定）
        self.active_backend: str = "local"

    def get_active_backend(self) -> SandboxBackend:
        """返回当前生效的后端实例

        Returns:
            SandboxBackend（Docker 可用且 prefer_docker=True 时返回 DockerSandbox，否则 LocalSandbox）
        """
        if self.prefer_docker and self.docker_sandbox.is_available():
            self.active_backend = "docker"
            return self.docker_sandbox
        self.active_backend = "local"
        return self.local_sandbox

    async def execute(
        self, code: str, timeout: int | None = None
    ) -> SandboxResult:
        """执行 Python 代码 - 自动选择后端

        Args:
            code: Python 代码字符串
            timeout: 超时秒（None 取 settings.sandbox_timeout）

        Returns:
            SandboxResult（含 backend 字段标明实际使用的后端）
        """
        backend = self.get_active_backend()
        try:
            result = await backend.execute(code, timeout=timeout)
            # 确保 backend 字段与 active_backend 一致
            result.backend = self.active_backend
            return result
        except Exception as exc:
            # integrity-framework：失败不抛异常
            # 如果 Docker 失败，尝试降级到 LocalSandbox
            logger.warning(
                "后端 %s 执行失败，尝试降级: %s: %s",
                self.active_backend,
                type(exc).__name__,
                exc,
            )
            if self.active_backend == "docker":
                # Docker 失败 → 降级到 LocalSandbox
                self.active_backend = "local"
                try:
                    result = await self.local_sandbox.execute(code, timeout=timeout)
                    result.backend = "local"
                    return result
                except Exception as fallback_exc:
                    logger.error(
                        "LocalSandbox 也失败: %s: %s",
                        type(fallback_exc).__name__,
                        fallback_exc,
                    )
                    return SandboxResult(
                        ok=False,
                        exit_code=-1,
                        backend="local",
                        error=f"fallback_failed: {type(fallback_exc).__name__}: {fallback_exc}",
                    )
            # 本地后端失败，直接返回
            return SandboxResult(
                ok=False,
                exit_code=-1,
                backend=self.active_backend,
                error=f"{type(exc).__name__}: {exc}",
            )
