"""线程安全单例工厂装饰器，替代全项目约 70 处手写 ``_instance + get_x() + reset_x()`` 样板。

用法：
    @singleton
    def get_store() -> SomeStore:
        return SomeStore(...)

    store = get_store()          # 同一实例
    get_store.reset()            # 清空缓存（测试隔离用）

线程安全（双重检查加锁），语义与原手写样板完全一致。
"""

from __future__ import annotations

import functools
import threading
from typing import Any, Callable, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])


def singleton(factory: F) -> F:
    """把工厂函数包装为单例 getter：首次调用构造，之后复用；附带 ``reset()``。"""
    _instance: Any = None
    _lock = threading.Lock()

    @functools.wraps(factory)
    def get(*args: Any, **kwargs: Any) -> Any:
        nonlocal _instance
        if _instance is None:
            with _lock:
                if _instance is None:
                    _instance = factory(*args, **kwargs)
        return _instance

    def reset() -> None:  # 测试隔离：清空缓存，下次调用重新构造
        nonlocal _instance
        with _lock:
            _instance = None

    setattr(get, "reset", reset)  # type: ignore[attr-defined]
    return cast(F, get)
