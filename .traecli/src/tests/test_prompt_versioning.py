"""P7.5 Prompt 版本化 + AB 测试测试。"""

from __future__ import annotations

import pytest

from deadman.infrastructure.prompt_versioning import (
    PromptVersionManager,
)


@pytest.fixture(autouse=True)
def enable_prompt_versioning(monkeypatch):
    monkeypatch.setenv("DEADMAN_PROMPT_VERSIONING_ENABLED", "1")
    from deadman.infrastructure.feature_flags import get_flags
    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    yield


class TestPublish:
    def test_publish_creates_version(self, tmp_path):
        pm = PromptVersionManager(repo_root=tmp_path / "prompts")
        pv = pm.publish(
            "death_aftercare",
            "1.0.0",
            template="Hello {{ user_input }}",
            variables=["user_input"],
            description="初始版本",
        )
        assert pv.name == "death_aftercare"
        assert pv.version == "1.0.0"
        assert pv.is_active is True

    def test_publish_persists_to_yaml(self, tmp_path):
        repo = tmp_path / "prompts"
        pm = PromptVersionManager(repo_root=repo)
        pm.publish(
            "death_aftercare",
            "1.0.0",
            template="Hello",
            description="初始",
        )
        # 应该有 yaml 文件
        assert (repo / "death_aftercare" / "1.0.0.yaml").exists()

    def test_publish_multiple_versions(self, tmp_path):
        pm = PromptVersionManager(repo_root=tmp_path / "prompts")
        pm.publish("death_aftercare", "1.0.0", template="v1")
        pm.publish("death_aftercare", "1.1.0", template="v2")
        versions = pm.list_versions("death_aftercare")
        assert len(versions) == 2
        # 1.1.0 应该是 active
        active = pm.get_active_version("death_aftercare")
        assert active == "1.1.0"

    def test_publish_without_active(self, tmp_path):
        """set_active=False 时不替换当前生效版本(灰度发布)。"""
        pm = PromptVersionManager(repo_root=tmp_path / "prompts")
        pm.publish("death_aftercare", "1.0.0", template="v1", set_active=True)
        pm.publish("death_aftercare", "1.1.0", template="v2", set_active=False)
        # active 仍是 1.0.0
        assert pm.get_active_version("death_aftercare") == "1.0.0"


class TestResolve:
    def test_resolve_active_version(self, tmp_path):
        pm = PromptVersionManager(repo_root=tmp_path / "prompts")
        pm.publish("death_aftercare", "1.0.0", template="Hello {{ user_input }}")
        result = pm.resolve("death_aftercare", user_id="u1")
        assert result.version == "1.0.0"
        assert "Hello" in result.template
        assert result.reason == "active"

    def test_resolve_pinned_version(self, tmp_path):
        """显式 version 参数优先。"""
        pm = PromptVersionManager(repo_root=tmp_path / "prompts")
        pm.publish("death_aftercare", "1.0.0", template="v1")
        pm.publish("death_aftercare", "1.1.0", template="v2")  # active
        # 显式要 1.0.0
        result = pm.resolve("death_aftercare", version="1.0.0")
        assert result.version == "1.0.0"
        assert result.reason == "pinned"

    def test_resolve_unknown_returns_builtin(self, tmp_path):
        pm = PromptVersionManager(repo_root=tmp_path / "prompts")
        result = pm.resolve("nonexistent_prompt")
        assert result.version == "builtin"
        assert result.template == ""

    def test_reload_from_disk(self, tmp_path):
        """新实例从 yaml 加载已发布的版本。"""
        repo = tmp_path / "prompts"
        pm1 = PromptVersionManager(repo_root=repo)
        pm1.publish("death_aftercare", "1.0.0", template="Hello")

        pm2 = PromptVersionManager(repo_root=repo)
        result = pm2.resolve("death_aftercare")
        assert result.version == "1.0.0"
        assert "Hello" in result.template


class TestRender:
    def test_render_replaces_variables(self, tmp_path):
        pm = PromptVersionManager(repo_root=tmp_path / "prompts")
        pm.publish("test", "1.0.0", template="Hello {{ name }}, you are {{ age }}")
        result = pm.resolve("test")
        rendered = pm.render(result, name="Alice", age="30")
        assert rendered == "Hello Alice, you are 30"

    def test_render_with_no_variables(self, tmp_path):
        pm = PromptVersionManager(repo_root=tmp_path / "prompts")
        pm.publish("test", "1.0.0", template="Hello World")
        result = pm.resolve("test")
        rendered = pm.render(result)
        assert rendered == "Hello World"


class TestRollback:
    def test_rollback_to_previous_version(self, tmp_path):
        pm = PromptVersionManager(repo_root=tmp_path / "prompts")
        pm.publish("death_aftercare", "1.0.0", template="v1")
        pm.publish("death_aftercare", "1.1.0", template="v2")
        # active 是 1.1.0
        assert pm.get_active_version("death_aftercare") == "1.1.0"
        # 回滚到 1.0.0
        assert pm.rollback("death_aftercare", "1.0.0") is True
        assert pm.get_active_version("death_aftercare") == "1.0.0"

    def test_rollback_to_unknown_returns_false(self, tmp_path):
        pm = PromptVersionManager(repo_root=tmp_path / "prompts")
        pm.publish("death_aftercare", "1.0.0", template="v1")
        assert pm.rollback("death_aftercare", "999.0.0") is False


class TestABExperiment:
    def test_create_experiment(self, tmp_path):
        pm = PromptVersionManager(repo_root=tmp_path / "prompts")
        pm.publish("death_aftercare", "1.0.0", template="control")
        pm.publish("death_aftercare", "1.1.0", template="variant_a")
        exp = pm.create_experiment(
            "exp1",
            prompt_name="death_aftercare",
            variants={"control": "1.0.0", "variant_a": "1.1.0"},
            traffic_split={"control": 50, "variant_a": 50},
            description="测试新 prompt",
        )
        assert exp.name == "exp1"
        assert exp.status == "running"

    def test_invalid_traffic_split_raises(self, tmp_path):
        pm = PromptVersionManager(repo_root=tmp_path / "prompts")
        with pytest.raises(ValueError):
            pm.create_experiment(
                "exp1",
                prompt_name="death_aftercare",
                variants={"control": "1.0.0"},
                traffic_split={"control": 50},  # 总和 50 != 100
            )

    def test_experiment_splits_users(self, tmp_path):
        """50/50 分流,约一半命中 variant_a。"""
        pm = PromptVersionManager(repo_root=tmp_path / "prompts")
        pm.publish("death_aftercare", "1.0.0", template="control")
        pm.publish("death_aftercare", "1.1.0", template="variant_a")
        pm.create_experiment(
            "exp1",
            prompt_name="death_aftercare",
            variants={"control": "1.0.0", "variant_a": "1.1.0"},
            traffic_split={"control": 50, "variant_a": 50},
        )
        variant_a_hits = sum(
            1 for i in range(1000)
            if pm.resolve("death_aftercare", user_id=f"u{i}").variant_id == "variant_a"
        )
        # 期望 ~500,允许 ±100
        assert 400 <= variant_a_hits <= 600

    def test_same_user_same_variant(self, tmp_path):
        """同一 user 永远命中同一 variant(稳定性)。"""
        pm = PromptVersionManager(repo_root=tmp_path / "prompts")
        pm.publish("death_aftercare", "1.0.0", template="control")
        pm.publish("death_aftercare", "1.1.0", template="variant_a")
        pm.create_experiment(
            "exp1",
            prompt_name="death_aftercare",
            variants={"control": "1.0.0", "variant_a": "1.1.0"},
            traffic_split={"control": 50, "variant_a": 50},
        )
        variant_ids = {pm.resolve("death_aftercare", user_id="u_test").variant_id for _ in range(10)}
        assert len(variant_ids) == 1

    def test_stop_experiment(self, tmp_path):
        pm = PromptVersionManager(repo_root=tmp_path / "prompts")
        pm.publish("death_aftercare", "1.0.0", template="control")
        pm.publish("death_aftercare", "1.1.0", template="variant_a")
        pm.create_experiment(
            "exp1",
            prompt_name="death_aftercare",
            variants={"control": "1.0.0", "variant_a": "1.1.0"},
            traffic_split={"control": 50, "variant_a": 50},
        )
        assert pm.stop_experiment("exp1") is True
        # 停止后不再分流,走 active
        result = pm.resolve("death_aftercare", user_id="u1")
        assert result.variant_id == "control"

    def test_auto_complete_on_target_reached(self, tmp_path):
        """达到 target_sample_size 自动 stop。"""
        pm = PromptVersionManager(repo_root=tmp_path / "prompts")
        pm.publish("death_aftercare", "1.0.0", template="control")
        pm.publish("death_aftercare", "1.1.0", template="variant_a")
        exp = pm.create_experiment(
            "exp1",
            prompt_name="death_aftercare",
            variants={"control": "1.0.0", "variant_a": "1.1.0"},
            traffic_split={"control": 50, "variant_a": 50},
            target_sample_size=10,
        )
        # 触发 10 次采样
        for i in range(10):
            pm.resolve("death_aftercare", user_id=f"u{i}")
        # 应该自动 completed
        updated = next(e for e in pm.list_experiments() if e.name == "exp1")
        assert updated.status == "completed"


class TestFeatureFlagDisabled:
    """feature flag 关闭时的行为。"""

    def test_disabled_returns_builtin(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_PROMPT_VERSIONING_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags
        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0

        pm = PromptVersionManager(repo_root=tmp_path / "prompts")
        result = pm.resolve("any_prompt")
        assert result.version == "builtin"
        assert result.reason == "disabled"
