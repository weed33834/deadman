"""deadman 插件化核心骨架。

Phase B 目标：把单体 16.3 万行拆成 ``deadman-core`` + 17 个可插拔插件包。
本包提供核心契约：

* :class:`deadman.plugins.protocol.Plugin` —— 插件协议（鸭子类型，无需继承）；
* :class:`deadman.plugins.registry.PluginRegistry` —— 插件注册表（生命周期管理）；
* :func:`deadman.plugins.loader.load_entry_point_plugins` —— 基于
  ``importlib.metadata.entry_points(group="deadman.plugins")`` 的自动发现。

依赖方向单向：``deadman-core`` 只依赖本包协议，插件包通过 entry points
挂载，核心不反向依赖任何插件实现。
"""

from .loader import load_entry_point_plugins
from .protocol import Plugin, PluginMeta
from .registry import PluginRegistry

__all__ = [
    "Plugin",
    "PluginMeta",
    "PluginRegistry",
    "load_entry_point_plugins",
]
