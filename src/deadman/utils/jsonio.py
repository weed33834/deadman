"""JSON 读写与序列化的统一约定，消除各 Store 重复的原子写样板。

原先约 50 处 Store/Manager 各自实现了 ``tmp + os.replace + json.dumps`` 的
原子写 JSON，以及 ``json.dumps(ensure_ascii=False, default=str)`` 的序列化约定。
这里收敛为单一事实源：

- ``atomic_write_bytes`` / ``atomic_write_text``：先写 ``.tmp`` 再 ``os.replace``
  的原子落盘（POSIX/Windows 均原子），中途崩溃原文件保持不变。
- ``atomic_write_json``：``dumps()`` 后原子落盘。
- ``read_json``：容错读取，文件缺失或解析失败返回默认值。
- ``dumps`` / ``dumps_pretty``：统一 ``ensure_ascii=False`` + ``default=str``，
  避免各模块对 datetime/dataclass 兜底序列化的写法漂移。
- ``stable_args_hash``：参数 → sha256 稳定哈希（缓存键复用）。

使用约定：子类 Store 需要写盘时直接调用，勿再自行实现 tmp/replace 逻辑。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写入字节：写 .tmp → fsync → os.replace；失败时清理残留 .tmp。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写入字节数据。"""
    _atomic_write_bytes(path, data)


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """原子写入文本。"""
    _atomic_write_bytes(path, content.encode(encoding))


def dumps(obj: Any, *, default: Any = str) -> str:
    """统一 JSON 序列化：ensure_ascii=False，datetime/dataclass 用 str 兜底。"""
    return json.dumps(obj, ensure_ascii=False, default=default)


def dumps_pretty(obj: Any, *, indent: int = 2, default: Any = str) -> str:
    """统一 JSON 序列化（带缩进，用于落盘）。"""
    return json.dumps(obj, ensure_ascii=False, indent=indent, default=default)


def atomic_write_json(
    path: Path,
    obj: Any,
    *,
    indent: int = 2,
    default: Any = str,
) -> None:
    """原子写入 JSON。"""
    _atomic_write_bytes(path, dumps_pretty(obj, indent=indent, default=default).encode("utf-8"))


def read_json(path: Path, default: Any = None, logger: Any = None) -> Any:
    """容错读取 JSON；文件缺失或解析失败返回 ``default``。"""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        if logger is not None:
            logger.warning("读取 JSON 失败 %s: %s", path, e)
        return default


def stable_args_hash(args: dict[str, Any] | None) -> str:
    """计算参数 dict 的稳定哈希（排序后 sha256），用作缓存键。

    - sort_keys=True 保证字段顺序无关
    - default=str 兜底不可序列化对象（dataclass / datetime）
    """
    if not args:
        return "0" * 64
    try:
        payload = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        payload = repr(args)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
