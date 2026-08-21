"""plugins 骨架单元测试：注册表 + 入口点加载器 + 协议鸭子类型。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from deadman.plugins import PluginMeta, PluginRegistry, load_entry_point_plugins
from deadman.plugins.protocol import Plugin
from deadman.plugins.registry import get_default_registry, set_default_registry


# ----------------------------------------------------------------------
# 测试插件（鸭子类型实现协议）
# ----------------------------------------------------------------------
@dataclass
class DemoPlugin:
    meta: PluginMeta
    setup_called: int = 0
    teardown_called: int = 0
    registered: list[tuple[str, str]] = field(default_factory=list)

    def setup(self, registry: PluginRegistry) -> None:
        self.setup_called += 1
        registry.register("mcp.tools", "demo_tool", lambda: "ok", plugin_name=self.meta.name)
        self.registered.append(("mcp.tools", "demo_tool"))

    def teardown(self) -> None:
        self.teardown_called += 1


class FakeEntryPoint:
    """迷你 entry point 替身（仅测试用，不依赖真实安装）。"""

    def __init__(self, name: str, factory) -> None:
        self.name = name
        self._factory = factory

    def load(self):
        return self._factory()


@pytest.fixture(autouse=True)
def _isolated_registry():
    """每个测试独立注册表，避免全局状态污染。"""
    fresh = PluginRegistry()
    set_default_registry(fresh)
    yield fresh
    set_default_registry(None)


# ----------------------------------------------------------------------
# 注册表
# ----------------------------------------------------------------------
class TestRegistry:
    def test_register_lookup_get(self, _isolated_registry: PluginRegistry):
        _isolated_registry.register("web.routes", "/healthz", "handler")
        assert _isolated_registry.lookup("web.routes") == ["handler"]
        assert _isolated_registry.get("web.routes", "/healthz") == "handler"
        assert _isolated_registry.get("web.routes", "missing", "default") == "default"

    def test_unregister(self, _isolated_registry: PluginRegistry):
        _isolated_registry.register("ns", "k", 1)
        _isolated_registry.unregister("ns", "k")
        assert _isolated_registry.lookup("ns") == []

    def test_drop_plugin_rolls_back_owned(self, _isolated_registry: PluginRegistry):
        meta = PluginMeta(name="demo", version="1.0.0")
        _isolated_registry.register_plugin(meta)
        _isolated_registry.register("ns", "a", 1, plugin_name="demo")
        _isolated_registry.register("ns", "b", 2, plugin_name="demo")
        _isolated_registry.register("ns", "c", 3, plugin_name="other")

        _isolated_registry.drop_plugin("demo")

        assert not _isolated_registry.has("demo")
        assert _isolated_registry.lookup("ns") == [3]  # 仅保留其他插件的登记
        assert _isolated_registry.plugins() == {}

    def test_clear(self, _isolated_registry: PluginRegistry):
        _isolated_registry.register("ns", "k", 1)
        _isolated_registry.register_plugin(PluginMeta(name="p"))
        _isolated_registry.clear()
        assert _isolated_registry.plugins() == {}
        assert _isolated_registry.lookup("ns") == []


# ----------------------------------------------------------------------
# 入口点加载器
# ----------------------------------------------------------------------
class TestLoader:
    def test_load_entry_point_plugins_enables_and_registers(self, monkeypatch, _isolated_registry):
        plugin = DemoPlugin(meta=PluginMeta(name="demo", version="1.0.0"))
        monkeypatch.setattr(
            "deadman.plugins.loader.discover_entry_points",
            lambda: [FakeEntryPoint("demo", lambda: plugin)],
        )

        loaded = load_entry_point_plugins()

        assert loaded == [("demo", plugin)]
        assert plugin.setup_called == 1
        assert _isolated_registry.has("demo")
        tool = _isolated_registry.lookup("mcp.tools")
        assert len(tool) == 1 and tool[0]() == "ok"

    def test_failure_isolation_default(self, monkeypatch, _isolated_registry):
        """默认 fail_fast=False：坏插件被跳过，好插件照常加载。"""
        good = DemoPlugin(meta=PluginMeta(name="good"))
        monkeypatch.setattr(
            "deadman.plugins.loader.discover_entry_points",
            lambda: [
                FakeEntryPoint("bad", lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
                FakeEntryPoint("good", lambda: good),
            ],
        )

        loaded = load_entry_point_plugins()

        assert [name for name, _ in loaded] == ["good"]
        assert _isolated_registry.has("good")
        assert not _isolated_registry.has("bad")

    def test_fail_fast_raises(self, monkeypatch, _isolated_registry):
        monkeypatch.setattr(
            "deadman.plugins.loader.discover_entry_points",
            lambda: [FakeEntryPoint("bad", lambda: (_ for _ in ()).throw(RuntimeError("boom")))],
        )
        with pytest.raises(RuntimeError, match="boom"):
            load_entry_point_plugins(fail_fast=True)

    def test_only_filter(self, monkeypatch, _isolated_registry):
        good = DemoPlugin(meta=PluginMeta(name="good"))
        other = DemoPlugin(meta=PluginMeta(name="other"))
        monkeypatch.setattr(
            "deadman.plugins.loader.discover_entry_points",
            lambda: [
                FakeEntryPoint("good", lambda: good),
                FakeEntryPoint("other", lambda: other),
            ],
        )

        loaded = load_entry_point_plugins(only=["good"])

        assert [name for name, _ in loaded] == ["good"]

    def test_plugin_without_meta_gets_default(self, monkeypatch, _isolated_registry):
        class BarePlugin:
            def setup(self, registry):
                pass

            def teardown(self):
                pass

        monkeypatch.setattr(
            "deadman.plugins.loader.discover_entry_points",
            lambda: [FakeEntryPoint("bare", lambda: BarePlugin())],
        )
        loaded = load_entry_point_plugins()
        assert loaded[0][0] == "bare"
        assert _isolated_registry.has("bare")


# ----------------------------------------------------------------------
# 协议鸭子类型
# ----------------------------------------------------------------------
class TestProtocol:
    def test_demo_plugin_satisfies_protocol(self):
        assert isinstance(DemoPlugin(meta=PluginMeta(name="x")), Plugin)

    def test_get_default_registry_singleton(self):
        assert get_default_registry() is get_default_registry()
