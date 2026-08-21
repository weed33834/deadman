"""入口点插件加载器：基于 ``importlib.metadata`` 自动发现已安装插件。

插件包在自己的 ``pyproject.toml`` 声明：:

    [project.entry-points."deadman.plugins"]
    deadman-mcp = "deadman_plugin_mcp:plugin"

``load_entry_point_plugins`` 会：

1. 扫描 ``entry_points(group="deadman.plugins")``；
2. 对每个入口点调用工厂/导入对象，校验 :class:`Plugin` 协议；
3. 调用 ``setup(registry)`` 完成启用；
4. 单个插件加载失败只记日志、不拖垮核心（默认
   ``fail_fast=False``）。

按需下载策略（Phase B/C 落地）：核心只加载"已安装"的插件；
``deadman plugin install <name>`` 通过内部 marketplace 安装 wheel 后，
新 entry point 下次启动即自动可见。
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Iterable
from typing import Any

from .protocol import ENTRY_POINT_GROUP, Plugin, PluginMeta
from .registry import PluginRegistry, get_default_registry

logger = logging.getLogger(__name__)


def discover_entry_points(
    group: str = ENTRY_POINT_GROUP,
) -> list[importlib.metadata.EntryPoint]:
    """列出组内全部 entry point（兼容 Python 3.10+ 的两种 API 形态）。"""
    try:
        # Python 3.12+: entry_points() 支持 group 参数
        eps = importlib.metadata.entry_points(group=group)
        return sorted(eps, key=lambda e: e.name)
    except TypeError:  # pragma: no cover - Python 3.10/3.11 旧 API
        all_eps = importlib.metadata.entry_points()
        return sorted(all_eps.get(group, []), key=lambda e: e.name)


def _coerce_plugin(obj: Any) -> Plugin | None:
    """把 entry point 解析出的对象规范化为插件实例。

    支持四种形态：
    1. 已实现 :class:`Plugin` 协议的对象（``plugin`` 模块级单例）；
    2. 工厂函数/可调用，返回插件对象；
    3. 模块对象（含 ``plugin`` 属性）；
    4. 未声明 ``meta`` 但具备 ``setup`` 能力的对象——自动补默认元信息
       （名字取对象 ``name`` 或 ``"unnamed"``），保持鸭子类型友好。
    """
    from types import ModuleType

    if isinstance(obj, ModuleType):
        obj = getattr(obj, "plugin", None)
        if obj is None:
            return None
    if isinstance(obj, Plugin):
        return obj
    if callable(obj) and not hasattr(obj, "setup"):
        try:
            obj = obj()
        except Exception:
            logger.exception("插件工厂调用失败")
            return None
        if isinstance(obj, Plugin):
            return obj
    if hasattr(obj, "setup") and callable(getattr(obj, "setup", None)):
        if not getattr(obj, "meta", None):
            obj.meta = PluginMeta(name=getattr(obj, "name", "unnamed"))
        return obj
    return None


def load_entry_point_plugins(
    registry: PluginRegistry | None = None,
    fail_fast: bool = False,
    only: Iterable[str] | None = None,
) -> list[tuple[str, Plugin]]:
    """加载并启用已安装的 entry point 插件。

    :param registry: 目标注册表；默认使用进程级默认注册表。
    :param fail_fast: True 时首个插件失败即抛异常；默认跳过并继续。
    :param only: 仅加载指定插件名（如 ``["deadman-mcp"]``）。
    :return: ``[(entry_point_name, plugin_instance)]`` 成功启用的列表。
    """
    registry = registry or get_default_registry()
    loaded: list[tuple[str, Plugin]] = []
    for ep in discover_entry_points():
        if only is not None and ep.name not in set(only):
            continue
        try:
            obj = ep.load()
            plugin = _coerce_plugin(obj)
            if plugin is None:
                logger.warning("插件 %r 未实现 Plugin 协议，跳过", ep.name)
                continue
            meta = getattr(plugin, "meta", None)
            # _coerce_plugin 可能已补占位名 "unnamed"，此处统一用入口点名覆盖
            if meta is None or meta.name == "unnamed":
                meta = PluginMeta(name=ep.name)
                plugin.meta = meta  # type: ignore[attr-defined]
            registry.register_plugin(meta)
            plugin.setup(registry)
            loaded.append((ep.name, plugin))
            logger.info("插件已启用: %s %s", meta.name, meta.version)
        except Exception:
            logger.exception("插件 %r 加载失败", ep.name)
            if fail_fast:
                raise
    return loaded
