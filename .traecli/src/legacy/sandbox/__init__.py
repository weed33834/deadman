"""Docker 沙箱执行器 - 在隔离容器内执行文件写入等操作

启用后 write_file 工具的文件操作在 Docker 容器内执行，
避免智能体写入的文件污染主环境。
Docker 不可用时自动降级为本地执行。

用法：
    from .sandbox import sandbox_write_file
    result = await sandbox_write_file(path, content)
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)


def _docker_available() -> bool:
    """检查 Docker daemon 是否可用"""
    if not settings.sandbox_enabled:
        return False
    if shutil.which("docker") is None:
        return False
    return True


async def _docker_exec(command: list[str]) -> dict[str, Any]:
    """在 Docker 容器内执行命令

    返回 {"exit_code": int, "stdout": str, "stderr": str}
    """
    docker_cmd = [
        "docker", "run", "--rm",
        "-m", "256m",  # 内存限制
        "--cpus", "0.5",  # CPU 限制
        "--network", "none",  # 禁止网络
        "--read-only",  # 只读根文件系统
        "--tmpfs", "/tmp:size=64m",  # 临时目录
        "-w", settings.sandbox_work_dir,
        settings.sandbox_image,
        *command,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *docker_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=settings.sandbox_timeout
        )
        return {
            "exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }
    except asyncio.TimeoutError:
        return {"exit_code": -1, "stdout": "", "stderr": "timeout"}
    except Exception as exc:
        return {"exit_code": -1, "stdout": "", "stderr": str(exc)}


async def sandbox_write_file(
    path: str, content: str, encoding: str = "utf-8"
) -> dict[str, Any]:
    """沙箱化文件写入

    Docker 可用时在容器内写入；否则降级为本地写入。
    """
    # 安全：仅在沙箱模式启用 + Docker 可用时走容器
    if not _docker_available():
        return _local_write(path, content, encoding)

    # 把内容通过 stdin 传入容器，避免命令行参数过长
    # 容器内用 python -c 写文件
    work_dir = settings.sandbox_work_dir
    escaped_path = path.replace("'", "\\'")
    script = (
        f"import sys; data=sys.stdin.buffer.read(); "
        f"open('{work_dir}/{escaped_path}', 'wb').write(data); "
        f"print(len(data))"
    )
    docker_cmd = [
        "docker", "run", "--rm", "-i",
        "-m", "256m", "--cpus", "0.5",
        "--network", "none",
        "--tmpfs", f"{work_dir}:size=64m",
        "-w", work_dir,
        settings.sandbox_image,
        "python", "-c", script,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *docker_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        data = content.encode(encoding)
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=data), timeout=settings.sandbox_timeout
        )
        if proc.returncode == 0:
            return {
                "ok": True,
                "path": path,
                "bytes_written": int(stdout.decode().strip() or len(data)),
                "sandbox": True,
            }
        return {
            "ok": False,
            "path": path,
            "error": stderr.decode("utf-8", errors="replace"),
            "sandbox": True,
        }
    except Exception as exc:
        logger.warning("Docker 沙箱写入失败，降级为本地: %s", exc)
        return _local_write(path, content, encoding)


def _local_write(path: str, content: str, encoding: str) -> dict[str, Any]:
    """本地文件写入（降级模式）"""
    try:
        from ..mcp_server.server import _safe_resolve

        target = _safe_resolve(path)
        if target is None:
            return {"ok": False, "path": path, "error": "路径越界"}
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode(encoding)
        target.write_bytes(data)
        return {
            "ok": True,
            "path": str(target.relative_to(settings.project_root)),
            "bytes_written": len(data),
            "sandbox": False,
        }
    except Exception as exc:
        return {"ok": False, "path": path, "error": str(exc), "sandbox": False}


async def sandbox_read_file(
    path: str, encoding: str = "utf-8", max_bytes: int = 1048576
) -> dict[str, Any]:
    """沙箱化文件读取

    Docker 可用时在容器内读取；否则降级为本地读取。
    读取操作通常不需要沙箱（无副作用），但为完整性提供。
    """
    # 读取无副作用，直接本地读
    from ..mcp_server.server import _safe_resolve

    target = _safe_resolve(path)
    if target is None:
        return {"ok": False, "path": path, "error": "路径越界"}
    if not target.exists():
        return {"ok": False, "path": path, "error": "文件不存在"}
    if target.stat().st_size > max_bytes:
        return {"ok": False, "path": path, "error": "文件过大"}
    try:
        content = target.read_text(encoding=encoding)
        return {
            "ok": True,
            "path": str(target.relative_to(settings.project_root)),
            "content": content,
            "size": target.stat().st_size,
            "sandbox": False,
        }
    except Exception as exc:
        return {"ok": False, "path": path, "error": str(exc)}


def get_sandbox_status() -> dict[str, Any]:
    """返回沙箱状态信息"""
    return {
        "enabled": settings.sandbox_enabled,
        "docker_available": _docker_available(),
        "image": settings.sandbox_image,
        "timeout": settings.sandbox_timeout,
        "work_dir": settings.sandbox_work_dir,
    }
