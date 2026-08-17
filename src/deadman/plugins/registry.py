"""插件注册表：核心与插件之间的运行时登记处。

* 插件通过 :meth:`PluginRegistry.register` 登记能力（工具、端点、钩子、
  路由前缀等），核心统一在此查询，避免核心反向 import 插件。
* 生命周期：``setup()`` 时注册，``teardown()`` 时注销；支持按命名空间
  分组（如 ``"mcp.tools"`` / ``"web.routes"``），互不污染。
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from .protocol import PluginMeta

logger = logging.getLogger(__name__)


class PluginRegistry:
    """线程安全的插件能力注册表。

    用法::

        registry = PluginRegistry()
        registry.register("mcp.tools", name="my_tool", value=my_tool)
        for v in registry.lookup("mcp.tools"):
            ...

    插件卸载时调用 :meth:`unregister` 按 ``plugin_name`` 批量清理，避免
    残留失效引用。
    """

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = defaultdict(dict)
        # plugin_name -> [(namespace, key)] 便于按插件回滚
        self._owned: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self._lock = threading.RLock()
        self._plugins: dict[str, PluginMeta] = {}

    # ------------------------------------------------------------------
    # 能力登记
    # ------------------------------------------------------------------
    def register(self, namespace: str, key: str, value: Any, plugin_name: str = "") -> None:
        """在 ``namespace`` 下登记一项能力 ``key -> value``。"""
        with self._lock:
            self._items[namespace][key] = value
            if plugin_name:
                self._owned[plugin_name].append((namespace, key))

    def unregister(self, namespace: str, key: str) -> None:
        """移除单项能力；不存在时静默忽略。"""
        with self._lock:
            self._items.get(namespace, {}).pop(key, None)

    def lookup(self, namespace: str) -> Iterable[Any]:
        """列出某命名空间下全部能力值（插入序）。"""
        with self._lock:
            return list(self._items.get(namespace, {}).values())

    def get(self, namespace: str, key: str, default: Any = None) -> Any:
        """按 key 取单项能力。"""
        with self._lock:
            return self._items.get(namespace, {}).get(key, default)

    # ------------------------------------------------------------------
    # 插件生命周期
    # ------------------------------------------------------------------
    def register_plugin(self, meta: PluginMeta) -> None:
        """登记插件元信息（幂等：同名覆盖）。"""
        with self._lock:
            self._plugins[meta.name] = meta

    def drop_plugin(self, plugin_name: str) -> None:
        """按插件名批量注销其登记的能力与元信息。"""
        with self._lock:
            for namespace, key in self._owned.pop(plugin_name, []):
                self._items.get(namespace, {}).pop(key, None)
            self._plugins.pop(plugin_name, None)

    def plugins(self) -> dict[str, PluginMeta]:
        """返回已登记插件的元信息副本。"""
        with self._lock:
            return dict(self._plugins)

    def has(self, plugin_name: str) -> bool:
        with self._lock:
            return plugin_name in self._plugins

    # ------------------------------------------------------------------
    # 批量清理
    # ------------------------------------------------------------------
    def clear(self) -> None:
        """清空全部登记（测试/热卸载用）。"""
        with self._lock:
            self._items.clear()
            self._owned.clear()
            self._plugins.clear()


# 全局默认注册表：核心模块与插件共用同一实例，避免重复创建。
# 可通过 ``set_default_registry`` 替换（测试隔离）。
_default_registry: PluginRegistry | None = None
_default_registry_lock = threading.Lock()


def get_default_registry() -> PluginRegistry:
    """返回进程级默认注册表（懒创建）。"""
    global _default_registry
    with _default_registry_lock:
        if _default_registry is None:
            _default_registry = PluginRegistry()
        return _default_registry


def set_default_registry(registry: PluginRegistry | None) -> None:
    """替换/重置默认注册表（主要用于测试隔离）。"""
    global _default_registry
    with _default_registry_lock:
        _default_registry = registry
