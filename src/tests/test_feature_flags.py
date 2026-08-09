"""P7.4 Feature Flag 系统测试。"""

from __future__ import annotations

from deadman.infrastructure.feature_flags import (
    FeatureFlagManager,
)


class TestFeatureFlagBasics:
    """基础读取功能。"""

    def test_unknown_flag_returns_false_by_default(self, tmp_path):
        """未声明的 flag → False。"""
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        assert fm.is_enabled("nonexistent_flag_xyz") is False

    def test_known_flag_returns_default(self, tmp_path):
        """内置默认值生效。"""
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        # memory_compress 默认 True
        assert fm.is_enabled("memory_compress") is True
        # plan_execute 默认 False
        assert fm.is_enabled("plan_execute") is False

    def test_env_var_overrides_default(self, tmp_path, monkeypatch):
        """env var DEADMAN_<NAME>_ENABLED 优先于默认值。"""
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        monkeypatch.setenv("DEADMAN_PLAN_EXECUTE_ENABLED", "1")
        assert fm.is_enabled("plan_execute") is True

        monkeypatch.setenv("DEADMAN_PLAN_EXECUTE_ENABLED", "0")
        assert fm.is_enabled("plan_execute") is False

    def test_evaluate_returns_reason(self, tmp_path):
        """evaluate 返回详细 reason。"""
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        result = fm.evaluate("memory_compress")
        assert result.value is True
        assert result.reason in ("default", "env_var", "env_var_fallback")


class TestDynamicConfig:
    """动态配置(set_flag)。"""

    def test_set_flag_persists_to_file(self, tmp_path):
        """set_flag 后落盘,新实例能读到。"""
        flags_file = tmp_path / "flags.json"
        fm1 = FeatureFlagManager(flags_file=flags_file)
        fm1.set_flag("plan_execute", enabled=True)

        # 新实例从同一文件加载
        fm2 = FeatureFlagManager(flags_file=flags_file)
        assert fm2.is_enabled("plan_execute") is True

    def test_set_flag_partial_update(self, tmp_path):
        """只更新指定字段,其他字段保留。"""
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        fm.set_flag("memory_compress", percentage=50)
        rule = fm.list_flags()
        plan_rule = next(r for r in rule if r["name"] == "memory_compress")
        assert plan_rule["percentage"] == 50
        # enabled 字段保留默认 True
        assert plan_rule["enabled"] is True

    def test_delete_flag_falls_back_to_default(self, tmp_path):
        """删除动态 flag → 回退到默认值。"""
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        # 默认 plan_execute=False
        assert fm.is_enabled("plan_execute") is False
        fm.set_flag("plan_execute", enabled=True)
        assert fm.is_enabled("plan_execute") is True
        fm.delete_flag("plan_execute")
        # 删除后回退默认值
        assert fm.is_enabled("plan_execute") is False

    def test_invalid_json_falls_back_gracefully(self, tmp_path):
        """损坏的 JSON → 静默降级到默认值,不抛异常。"""
        flags_file = tmp_path / "flags.json"
        flags_file.parent.mkdir(parents=True, exist_ok=True)
        flags_file.write_text("invalid json {{{", encoding="utf-8")
        fm = FeatureFlagManager(flags_file=flags_file)
        # 应返回默认值,不抛异常
        assert fm.is_enabled("memory_compress") is True

    def test_atomic_write_uses_tmp_file(self, tmp_path):
        """保存使用 .tmp + os.replace 原子模式(防止崩溃损坏)。"""
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        fm.set_flag("memory_compress", enabled=True)
        # .tmp 文件不应残留
        assert not (tmp_path / "flags.json.tmp").exists()
        assert (tmp_path / "flags.json").exists()


class TestPercentageSplit:
    """百分比分流(灰度发布)。"""

    def test_percentage_100_always_enabled(self, tmp_path):
        """percentage=100 + 无 user_id → enabled。"""
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        fm.set_flag("plan_execute", enabled=True, percentage=100)
        assert fm.is_enabled("plan_execute") is True

    def test_percentage_0_never_enabled(self, tmp_path):
        """percentage=0 → 任何 user 都不命中。"""
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        fm.set_flag("plan_execute", enabled=True, percentage=0)
        for i in range(100):
            assert fm.is_enabled("plan_execute", user_id=f"u{i}") is False

    def test_percentage_50_splits_users(self, tmp_path):
        """percentage=50 → 约一半 user 命中。"""
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        fm.set_flag("plan_execute", enabled=True, percentage=50)
        hits = sum(1 for i in range(1000) if fm.is_enabled("plan_execute", user_id=f"u{i}"))
        # 期望 ~500,允许 ±100(分布波动)
        assert 400 <= hits <= 600, f"Expected ~500 hits, got {hits}"

    def test_same_user_always_same_bucket(self, tmp_path):
        """同一 user+flag 永远命中同一桶(稳定性)。"""
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        fm.set_flag("plan_execute", enabled=True, percentage=50)
        # 同一 user 多次查询应一致
        results = {fm.is_enabled("plan_execute", user_id="u_test") for _ in range(10)}
        assert len(results) == 1


class TestUserLists:
    """白名单/黑名单。"""

    def test_whitelist_overrides_percentage(self, tmp_path):
        """白名单 user 即使 percentage=0 也启用。"""
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        fm.set_flag(
            "plan_execute",
            enabled=True,
            percentage=0,
            user_whitelist=["vip_user"],
        )
        assert fm.is_enabled("plan_execute", user_id="vip_user") is True
        assert fm.is_enabled("plan_execute", user_id="normal_user") is False

    def test_blacklist_overrides_everything(self, tmp_path):
        """黑名单优先级最高(即使 whitelist 也无效)。"""
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        fm.set_flag(
            "plan_execute",
            enabled=True,
            percentage=100,
            user_whitelist=["user1"],
            user_blacklist=["user1"],
        )
        # user1 同时在白/黑名单 → 黑名单优先
        assert fm.is_enabled("plan_execute", user_id="user1") is False

    def test_blacklisted_user_disables_regardless_of_default(self, tmp_path):
        """黑名单关闭默认开启的 flag。"""
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        fm.set_flag(
            "memory_compress",  # 默认 True
            user_blacklist=["banned_user"],
        )
        assert fm.is_enabled("memory_compress", user_id="banned_user") is False
        assert fm.is_enabled("memory_compress", user_id="normal_user") is True


class TestVariant:
    """variant(AB 测试配置)。"""

    def test_get_variant_returns_dict(self, tmp_path):
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        fm.set_flag(
            "plan_execute",
            enabled=True,
            percentage=100,
            variant={"model": "gpt-4o-mini", "max_tokens": 1000},
        )
        variant = fm.get_variant("plan_execute")
        assert variant == {"model": "gpt-4o-mini", "max_tokens": 1000}

    def test_variant_none_when_disabled(self, tmp_path):
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        fm.set_flag("plan_execute", enabled=False, variant={"model": "x"})
        assert fm.get_variant("plan_execute") is None


class TestListFlags:
    """list_flags(看板)。"""

    def test_list_includes_dynamic_and_defaults(self, tmp_path):
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        fm.set_flag("plan_execute", enabled=True)
        flags = fm.list_flags()
        names = [f["name"] for f in flags]
        # 应包含动态设置的 + 内置默认的
        assert "plan_execute" in names
        assert "memory_compress" in names

    def test_list_includes_source(self, tmp_path):
        fm = FeatureFlagManager(flags_file=tmp_path / "flags.json")
        fm.set_flag("plan_execute", enabled=True)
        flags = fm.list_flags()
        plan_flag = next(f for f in flags if f["name"] == "plan_execute")
        assert plan_flag["source"] == "dynamic"
        memory_flag = next(f for f in flags if f["name"] == "memory_compress")
        assert memory_flag["source"] in ("default", "env")
