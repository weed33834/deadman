"""MCP 插件适配：把内置 ``deadman.mcp_server`` 暴露为可插拔插件。

过渡策略（Phase B 逻辑拆分）：
* 物理上 mcp_server 仍是 deadman 发行版的一部分（20+ 模块反向引用，
  直接搬包会破坏 import 面）；先通过 entry point + Plugin 协议让插件系统
  能发现/启用它，为 Phase C 物理拆包（``deadman-plugin-mcp``）铺路。
* ``setup`` 幂等：重复启用不会重复登记。
"""

from __future__ import annotations

import logging

from .._version import __version__ as _core_version
from ..plugins.protocol import Plugin, PluginMeta
from ..plugins.registry import PluginRegistry

logger = logging.getLogger(__name__)

#: 插件元信息；版本跟随核心，避免维护第二套版本号
_PLUGIN_META = PluginMeta(
    name="deadman-mcp",
    version=_core_version,
    description="Model Context Protocol server（15 个工具：知识查询/联网搜索/文件读写/子智能体/沙箱等）",
    extra={"home": "src/deadman/mcp_server"},
)


class McpPlugin:
    """mcp_server 的插件外观（实现 :class:`Plugin` 协议）。"""

    meta: PluginMeta = _PLUGIN_META

    def setup(self, registry: PluginRegistry) -> None:
        # 自登记元信息（loader 路径也会调用，幂等覆盖）
        registry.register_plugin(self.meta)
        from .server import mcp  # 延迟导入：避免 import 插件时拉起整个 server

        registry.register("mcp.servers", "default", mcp, plugin_name=self.meta.name)
        # 工具清单快照登记到 "mcp.tools"（供 UI/审计查询，不复制 handler 引用面）
        for tname, tdef in getattr(mcp, "_tools", {}).items():
            registry.register(
                "mcp.tools",
                tname,
                {"name": tdef.name, "description": tdef.description},
                plugin_name=self.meta.name,
            )
        logger.info("deadman-mcp 插件已启用：%d 个工具", len(getattr(mcp, "_tools", {})))

    def teardown(self, registry: PluginRegistry) -> None:
        registry.drop_plugin(self.meta.name)


#: 模块级单例，entry point 指向它：
#:   [project.entry-points."deadman.plugins"]
#:   deadman-mcp = "deadman.mcp_server:plugin"
plugin: Plugin = McpPlugin()
