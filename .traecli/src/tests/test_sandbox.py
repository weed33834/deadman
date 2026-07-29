"""测试 deadman.sandbox.base - 代码执行沙箱后端

覆盖点（7 个）：
  1. test_local_sandbox_execute_ok: 基本执行 print('hello') 成功
  2. test_local_sandbox_timeout: while True 在 timeout=1 时被终止
  3. test_local_sandbox_stderr: 1/0 触发 ZeroDivisionError，stderr 含错误信息
  4. test_local_sandbox_cleanup: 临时文件执行后被清理
  5. test_docker_sandbox_unavailable_graceful: Docker 不可用时返回 ok=False, error="docker_unavailable"
  6. test_sandbox_manager_fallback: Docker 不可用时降级到 LocalSandbox
  7. test_sandbox_manager_prefers_docker: Docker 可用时优先使用 Docker

不依赖 pytest-asyncio：async 方法用 asyncio.run() 在 sync 测试函数内调用。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock


from deadman.sandbox.base import (
    DockerSandbox,
    LocalSandbox,
    SandboxManager,
    SandboxResult,
)


# =====================================================================
# 1. LocalSandbox 基本执行
# =====================================================================


class TestLocalSandboxExecute:
    """测试 LocalSandbox 基本执行能力"""

    def test_local_sandbox_execute_ok(self):
        # 执行 print('hello from sandbox')，应成功
        sandbox = LocalSandbox()
        result = asyncio.run(sandbox.execute("print('hello from sandbox')", timeout=10))

        assert isinstance(result, SandboxResult)
        assert result.ok is True, f"应成功，stderr={result.stderr!r}"
        assert result.exit_code == 0
        assert "hello from sandbox" in result.stdout
        assert result.backend == "local"
        assert result.timed_out is False
        assert result.duration_ms > 0


# =====================================================================
# 2. LocalSandbox 超时终止
# =====================================================================


class TestLocalSandboxTimeout:
    """测试 LocalSandbox 超时终止能力"""

    def test_local_sandbox_timeout(self):
        # while True 循环，timeout=1，应被终止
        sandbox = LocalSandbox()
        code = "import time\nwhile True:\n    time.sleep(0.1)"
        result = asyncio.run(sandbox.execute(code, timeout=1))

        assert result.ok is False
        assert result.timed_out is True
        assert result.error == "timeout"
        assert result.exit_code == -1
        # duration 应在 1 秒附近（允许 0.5-3 秒的浮动）
        assert 800 <= result.duration_ms <= 3000, (
            f"duration_ms 应在 1s 附近，实际: {result.duration_ms}"
        )


# =====================================================================
# 3. LocalSandbox stderr 捕获
# =====================================================================


class TestLocalSandboxStderr:
    """测试 LocalSandbox 捕获 stderr（如 ZeroDivisionError）"""

    def test_local_sandbox_stderr(self):
        # 1/0 触发 ZeroDivisionError
        sandbox = LocalSandbox()
        result = asyncio.run(sandbox.execute("print(1/0)", timeout=10))

        assert result.ok is False
        assert result.exit_code != 0
        # stderr 应包含 ZeroDivisionError
        assert "ZeroDivisionError" in result.stderr, (
            f"stderr 应含 ZeroDivisionError，实际: {result.stderr!r}"
        )


# =====================================================================
# 4. LocalSandbox 临时文件清理
# =====================================================================


class TestLocalSandboxCleanup:
    """测试 LocalSandbox 执行后清理临时文件"""

    def test_local_sandbox_cleanup(self):
        # 监控临时目录，执行后不应残留 deadman_sandbox_*.py 文件
        tmp_dir = Path(tempfile.gettempdir())

        # 执行前快照
        before = set(tmp_dir.glob("deadman_sandbox_*.py"))

        sandbox = LocalSandbox()
        asyncio.run(sandbox.execute("print('cleanup test')", timeout=10))

        # 执行后快照
        after = set(tmp_dir.glob("deadman_sandbox_*.py"))
        # 不应残留新的临时文件
        new_files = after - before
        assert len(new_files) == 0, (
            f"临时文件未清理，残留: {[str(f) for f in new_files]}"
        )


# =====================================================================
# 5. DockerSandbox 不可用时优雅降级
# =====================================================================


class TestDockerSandboxUnavailable:
    """测试 DockerSandbox 在 Docker 不可用时优雅降级"""

    def test_docker_sandbox_unavailable_graceful(self):
        # mock DockerSandbox.is_available 返回 False
        sandbox = DockerSandbox()
        # 强制缓存为 False（模拟无 docker）
        sandbox._availability_cached = False

        result = asyncio.run(sandbox.execute("print('test')", timeout=5))

        # 应返回 ok=False, error="docker_unavailable"（graceful degradation）
        assert result.ok is False
        assert result.error == "docker_unavailable"
        assert result.backend == "docker"
        # 不应抛异常，不应用 local 后端执行
        assert result.stdout == ""


# =====================================================================
# 6. SandboxManager Docker 不可用时降级到 LocalSandbox
# =====================================================================


class TestSandboxManagerFallback:
    """测试 SandboxManager 在 Docker 不可用时降级到 LocalSandbox"""

    def test_sandbox_manager_fallback(self):
        # 构造一个 Docker 不可用的 manager
        mock_docker = MagicMock()
        mock_docker.is_available.return_value = False
        mock_docker.name = "docker"

        local = LocalSandbox()
        manager = SandboxManager(local_sandbox=local, docker_sandbox=mock_docker, prefer_docker=True)

        # get_active_backend 应返回 local（Docker 不可用）
        backend = manager.get_active_backend()
        assert backend is local
        assert manager.active_backend == "local"

        # 执行应使用 local 后端
        result = asyncio.run(manager.execute("print('fallback test')", timeout=10))

        assert result.ok is True
        assert result.backend == "local"
        assert "fallback test" in result.stdout
        # 验证 docker.is_available 被调用过
        mock_docker.is_available.assert_called()


# =====================================================================
# 7. SandboxManager Docker 可用时优先使用 Docker
# =====================================================================


class TestSandboxManagerPrefersDocker:
    """测试 SandboxManager 在 Docker 可用时优先使用 Docker"""

    def test_sandbox_manager_prefers_docker(self):
        # 构造一个 Docker 可用的 manager
        from unittest.mock import AsyncMock

        mock_docker = MagicMock()
        mock_docker.is_available.return_value = True
        mock_docker.name = "docker"
        # mock execute 返回成功结果（用 AsyncMock 因为 manager.execute 会 await）
        mock_docker.execute = AsyncMock(
            return_value=SandboxResult(
                ok=True,
                exit_code=0,
                stdout="via docker\n",
                backend="docker",
            )
        )

        local = LocalSandbox()
        manager = SandboxManager(local_sandbox=local, docker_sandbox=mock_docker, prefer_docker=True)

        # get_active_backend 应返回 docker（Docker 可用）
        backend = manager.get_active_backend()
        assert backend is mock_docker
        assert manager.active_backend == "docker"

        # 执行应使用 docker 后端（mock）
        result = asyncio.run(manager.execute("print('docker test')", timeout=10))

        assert result.ok is True
        assert result.backend == "docker"
        assert "via docker" in result.stdout
        # 验证 docker.execute 被调用过（local 没被调用）
        mock_docker.execute.assert_called_once()
