"""插件协议：核心与插件之间的唯一契约。

设计原则：
* 鸭子类型优先——插件无需继承任何基类，只需实现 :class:`Plugin` 协议
  要求的属性/方法即可被识别；避免核心反向 import 插件类型。
* 元信息与实现分离——:class:`PluginMeta` 描述插件的静态信息（name /
  version / description / dependencies），由 entry point 加载时读取，
  可离线展示（如 ``deadman plugin list``），不触发插件代码 import。
* 生命周期最小化——``setup(registry)`` 在启用时调用一次，``teardown()``
  在卸载/关闭时调用；其余能力通过注册表暴露的钩子接入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# 统一入口点组名：插件包在 pyproject.toml 声明
#   [project.entry-points."deadman.plugins"]
#   deadman-mcp = "deadman_plugin_mcp:plugin"
ENTRY_POINT_GROUP = "deadman.plugins"


@dataclass(frozen=True)
class PluginMeta:
    """插件静态元信息（加载前即可读取，不执行插件代码）。"""

    name: str
    version: str = "0.0.0"
    description: str = ""
    #: 运行时依赖的插件/核心能力名（可选，用于启动顺序与冲突检测）
    requires: tuple[str, ...] = ()
    #: 是否默认启用；False 表示需显式 ``deadman plugin enable <name>``
    enabled_by_default: bool = True
    #: 附加键值（作者、主页、许可等），透传不校验
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Plugin(Protocol):
    """插件运行时协议。

    一个合格的插件模块导出 ``plugin`` 对象（或 entry point 指向工厂函数
    返回该对象），满足：:

        @dataclass
        class MyPlugin:
            meta: PluginMeta

            def setup(self, registry: PluginRegistry) -> None: ...
            def teardown(self) -> None: ...
    """

    meta: PluginMeta

    def setup(self, registry: Any) -> None:
        """启用钩子：注册工具/端点/钩子函数。实现方应幂等。"""

    def teardown(self) -> None:
        """卸载钩子：释放资源、注销注册项。实现方应幂等。"""
