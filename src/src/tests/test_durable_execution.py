"""P7.6 Durable Execution 测试 - 幂等键 + 崩溃恢复 + 重放。"""

from __future__ import annotations

import pytest
from deadman.infrastructure.durable_execution import (
    DurableExecutionManager,
    ExecutionStatus,
)


@pytest.fixture(autouse=True)
def enable_durable_execution(monkeypatch):
    monkeypatch.setenv("DEADMAN_DURABLE_EXECUTION_ENABLED", "1")
    from deadman.infrastructure.feature_flags import get_flags

    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    yield


class TestIdempotencyKey:
    def test_same_args_same_key(self):
        key1 = DurableExecutionManager.generate_idempotency_key(
            "write_file", {"path": "/tmp/a", "content": "hello"}
        )
        key2 = DurableExecutionManager.generate_idempotency_key(
            "write_file", {"path": "/tmp/a", "content": "hello"}
        )
        assert key1 == key2

    def test_different_args_different_key(self):
        key1 = DurableExecutionManager.generate_idempotency_key(
            "write_file", {"path": "/tmp/a", "content": "hello"}
        )
        key2 = DurableExecutionManager.generate_idempotency_key(
            "write_file", {"path": "/tmp/a", "content": "world"}
        )
        assert key1 != key2

    def test_different_node_different_key(self):
        key1 = DurableExecutionManager.generate_idempotency_key("write_file", {"path": "/tmp/a"})
        key2 = DurableExecutionManager.generate_idempotency_key("init_transfer", {"path": "/tmp/a"})
        assert key1 != key2

    def test_args_order_independent(self):
        """dict 字段顺序不同应生成相同 key(json sort_keys=True)。"""
        key1 = DurableExecutionManager.generate_idempotency_key("write_file", {"a": 1, "b": 2})
        key2 = DurableExecutionManager.generate_idempotency_key("write_file", {"b": 2, "a": 1})
        assert key1 == key2

    def test_salt_changes_key(self):
        key1 = DurableExecutionManager.generate_idempotency_key(
            "write_file", {"path": "/tmp/a"}, salt="run1"
        )
        key2 = DurableExecutionManager.generate_idempotency_key(
            "write_file", {"path": "/tmp/a"}, salt="run2"
        )
        assert key1 != key2


class TestRecordStart:
    def test_record_start_creates_started_record(self, tmp_path):
        dm = DurableExecutionManager(log_path=tmp_path / "log.jsonl")
        key = "key1"
        record = dm.record_start(key, "trace1", "write_file", {"path": "/tmp/a"})
        assert record.status == ExecutionStatus.STARTED
        assert record.node_name == "write_file"

    def test_record_start_idempotent_on_completed(self, tmp_path):
        """已 COMPLETED 的 key 再次 record_start 直接返回(幂等)。"""
        dm = DurableExecutionManager(log_path=tmp_path / "log.jsonl")
        key = "key1"
        # 第一次:start
        dm.record_start(key, "trace1", "write_file", {"path": "/tmp/a"})
        # 完成
        dm.record_complete(key, result={"bytes_written": 100})
        # 第二次 start:应直接返回 completed(幂等)
        record = dm.record_start(key, "trace2", "write_file", {"path": "/tmp/a"})
        assert record.status == ExecutionStatus.COMPLETED
        assert record.result == {"bytes_written": 100}

    def test_record_start_marks_started_as_failed(self, tmp_path):
        """已 STARTED 但未完成的 key 再次 start 时,标记前一次为 FAILED。"""
        dm = DurableExecutionManager(log_path=tmp_path / "log.jsonl")
        key = "key1"
        dm.record_start(key, "trace1", "write_file", {"path": "/tmp/a"})
        # 模拟崩溃:不调 record_complete 就再次 start
        record = dm.record_start(key, "trace2", "write_file", {"path": "/tmp/a"})
        # 新 record 是 STARTED
        assert record.status == ExecutionStatus.STARTED


class TestRecordComplete:
    def test_record_complete_success(self, tmp_path):
        dm = DurableExecutionManager(log_path=tmp_path / "log.jsonl")
        key = "key1"
        dm.record_start(key, "trace1", "write_file", {"path": "/tmp/a"})
        record = dm.record_complete(key, result={"bytes": 100})
        assert record.status == ExecutionStatus.COMPLETED
        assert record.result == {"bytes": 100}
        assert record.duration_ms > 0

    def test_record_complete_failure(self, tmp_path):
        dm = DurableExecutionManager(log_path=tmp_path / "log.jsonl")
        key = "key1"
        dm.record_start(key, "trace1", "write_file", {"path": "/tmp/a"})
        record = dm.record_complete(key, error="disk_full")
        assert record.status == ExecutionStatus.FAILED
        assert record.error == "disk_full"


class TestCompensation:
    def test_record_compensation(self, tmp_path):
        dm = DurableExecutionManager(log_path=tmp_path / "log.jsonl")
        key = "key1"
        dm.record_start(key, "trace1", "init_transfer", {"amount": 100})
        dm.record_complete(key, error="bank_timeout")
        # 触发补偿(回滚)
        dm.record_compensation(key, "reverse_transfer")
        record = dm.lookup(key)
        assert record.status == ExecutionStatus.COMPENSATED
        assert record.saga_compensation == "reverse_transfer"


class TestLookup:
    def test_lookup_returns_none_when_not_found(self, tmp_path):
        dm = DurableExecutionManager(log_path=tmp_path / "log.jsonl")
        assert dm.lookup("nonexistent") is None

    def test_lookup_by_trace(self, tmp_path):
        dm = DurableExecutionManager(log_path=tmp_path / "log.jsonl")
        # 3 个不同 trace 的记录
        dm.record_start("k1", "trace1", "write_file", {"path": "/a"})
        dm.record_start("k2", "trace1", "write_file", {"path": "/b"})
        dm.record_start("k3", "trace2", "write_file", {"path": "/c"})
        records = dm.lookup_by_trace("trace1")
        assert len(records) == 2
        for r in records:
            assert r.trace_id == "trace1"


class TestExecutionScope:
    def test_scope_records_success(self, tmp_path):
        dm = DurableExecutionManager(log_path=tmp_path / "log.jsonl")
        key = "key1"
        with dm.execution_scope(key, "trace1", "write_file", {"path": "/a"}) as scope:
            # 模拟实际执行
            scope.set_result({"bytes": 100})
        # 应该有 completed 记录
        record = dm.lookup(key)
        assert record.status == ExecutionStatus.COMPLETED
        assert record.result == {"bytes": 100}

    def test_scope_records_failure(self, tmp_path):
        dm = DurableExecutionManager(log_path=tmp_path / "log.jsonl")
        key = "key1"
        with pytest.raises(RuntimeError):  # noqa: SIM117  pytest.raises 需独立 with 捕获内层 with 抛出的异常
            with dm.execution_scope(key, "trace1", "write_file", {"path": "/a"}):
                raise RuntimeError("disk full")
        record = dm.lookup(key)
        assert record.status == ExecutionStatus.FAILED
        assert "disk full" in record.error

    def test_scope_idempotent_cached(self, tmp_path):
        """已 completed 的 key 再次 scope 时 is_cached=True。"""
        dm = DurableExecutionManager(log_path=tmp_path / "log.jsonl")
        key = "key1"
        # 第一次执行
        with dm.execution_scope(key, "trace1", "write_file", {"path": "/a"}) as scope:
            scope.set_result({"bytes": 100})
        # 第二次相同 key
        with dm.execution_scope(key, "trace2", "write_file", {"path": "/a"}) as scope2:
            assert scope2.is_cached is True
            assert scope2.cached_result == {"bytes": 100}

    def test_scope_does_not_re_execute_when_cached(self, tmp_path):
        dm = DurableExecutionManager(log_path=tmp_path / "log.jsonl")
        key = "key1"
        execution_count = 0

        def do_work():
            nonlocal execution_count
            execution_count += 1
            return {"bytes": 100}

        with dm.execution_scope(key, "trace1", "write_file", {"path": "/a"}) as scope:
            if not scope.is_cached:
                scope.set_result(do_work())

        with dm.execution_scope(key, "trace2", "write_file", {"path": "/a"}) as scope:
            if not scope.is_cached:
                scope.set_result(do_work())

        # 应该只执行一次(第二次 cached)
        assert execution_count == 1


class TestReplay:
    def test_replay_returns_all_records(self, tmp_path):
        dm = DurableExecutionManager(log_path=tmp_path / "log.jsonl")
        # 模拟一个 trace 内的多个副作用
        dm.record_start("k1", "trace1", "write_file", {"path": "/a"})
        dm.record_complete("k1", result={"bytes": 100})
        dm.record_start("k2", "trace1", "init_transfer", {"amount": 100})
        dm.record_complete("k2", result={"tx_id": "tx1"})

        records = dm.replay("trace1")
        assert len(records) == 2

    def test_replay_callback_invoked(self, tmp_path):
        dm = DurableExecutionManager(log_path=tmp_path / "log.jsonl")
        dm.record_start("k1", "trace1", "write_file", {"path": "/a"})
        dm.record_complete("k1", result={"bytes": 100})

        called_nodes = []
        dm.replay("trace1", on_node=lambda r: called_nodes.append(r.node_name))
        assert called_nodes == ["write_file"]


class TestPersistence:
    def test_reload_loads_existing_records(self, tmp_path):
        """新实例从 log 文件加载已有记录。"""
        log_path = tmp_path / "log.jsonl"
        dm1 = DurableExecutionManager(log_path=log_path)
        dm1.record_start("k1", "trace1", "write_file", {"path": "/a"})
        dm1.record_complete("k1", result={"bytes": 100})

        dm2 = DurableExecutionManager(log_path=log_path)
        record = dm2.lookup("k1")
        assert record is not None
        assert record.status == ExecutionStatus.COMPLETED
        assert record.result == {"bytes": 100}


class TestFeatureFlagDisabled:
    """feature flag 关闭时不做任何记录。"""

    def test_disabled_lookup_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_DURABLE_EXECUTION_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags

        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0

        dm = DurableExecutionManager(log_path=tmp_path / "log.jsonl")
        assert dm.lookup("any_key") is None

    def test_disabled_scope_does_not_record(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_DURABLE_EXECUTION_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags

        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0

        dm = DurableExecutionManager(log_path=tmp_path / "log.jsonl")
        with dm.execution_scope("k1", "trace1", "write_file", {"path": "/a"}) as scope:
            assert scope.is_cached is False  # flag 关闭时不查缓存
            scope.set_result({"bytes": 100})
        # log 文件应该不存在(flag 关闭不写)
        assert not (tmp_path / "log.jsonl").exists()
