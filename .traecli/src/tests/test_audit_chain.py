"""P5.1 审计链 - 测试矩阵

覆盖点：
1. test_audit_append_creates_entry: 基础写入 + 字段完整
2. test_audit_chain_hash_linking: 链式 hash 衔接（prev_hash → curr_hash）
3. test_audit_verify_chain_intact: 完整链校验通过
4. test_audit_tamper_detected: 篡改检测（中间行被改后校验失败）
5. test_audit_query_by_event_type: 按事件类型查询
6. test_audit_query_by_actor: 按 actor 查询
7. test_audit_disabled_noop: feature flag 关闭行为不变
8. test_audit_query_by_target: 按 target 查询
9. test_audit_query_by_since: 按 since 时间过滤
10. test_audit_compute_hash_deterministic: 相同字段产生相同 hash
11. test_audit_persistence_roundtrip: 持久化往返（重启后能加载）
12. test_audit_corrupt_line_skipped: 损坏行容错
13. test_audit_global_singleton: 全局单例
"""

from __future__ import annotations

import json
from pathlib import Path

import deadman.security.audit as audit_module
import pytest
from deadman.security.audit import (
    GENESIS_HASH,
    AuditChain,
    AuditEvent,
    compute_hash,
    get_audit_chain,
    reset_audit_chain,
)

# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture(autouse=True)
def _enable_audit(monkeypatch):
    """每个测试默认开启 audit chain feature flag"""
    monkeypatch.setattr(audit_module, "AUDIT_CHAIN_ENABLED", True)
    # 重置全局单例，避免跨测试污染
    reset_audit_chain()
    yield
    reset_audit_chain()


@pytest.fixture
def tmp_audit_path(tmp_path) -> Path:
    """临时审计文件路径（每个测试独立）"""
    return tmp_path / "audit.jsonl"


@pytest.fixture
def chain(tmp_audit_path) -> AuditChain:
    """构造一个用临时路径的 AuditChain"""
    return AuditChain(persist_path=tmp_audit_path)


# =====================================================================
# 1. 基础写入
# =====================================================================


class TestAuditAppendCreatesEntry:
    def test_audit_append_creates_entry(self, chain, tmp_audit_path):
        """append 返回 AuditEvent，字段完整"""
        event = chain.append(
            event_type="tool_call",
            actor="agent.legal_advisor",
            action="call_tool",
            target="web_search",
            metadata={"query": "遗产继承法"},
        )
        assert event is not None
        assert event.event_type == "tool_call"
        assert event.actor == "agent.legal_advisor"
        assert event.action == "call_tool"
        assert event.target == "web_search"
        assert event.metadata == {"query": "遗产继承法"}
        assert event.event_id  # 自动生成非空
        assert event.timestamp  # 自动生成非空
        # 首条记录的 prev_hash 为 genesis（64 个 0）
        assert event.prev_hash == GENESIS_HASH
        # curr_hash 已计算
        assert event.curr_hash
        assert len(event.curr_hash) == 64
        # 文件已写入
        assert tmp_audit_path.exists()


# =====================================================================
# 2. 链式 hash 衔接
# =====================================================================


class TestAuditChainHashLinking:
    def test_audit_chain_hash_linking(self, chain):
        """多条记录的 prev_hash 链接到前一条的 curr_hash"""
        e1 = chain.append("tool_call", "a", "call_tool", "t1", {"k": "v1"})
        e2 = chain.append("rule_triggered", "b", "trigger_rule", "t2", {"k": "v2"})
        e3 = chain.append("handoff", "c", "handoff", "t3", {"k": "v3"})

        # 第一条 prev_hash 是 genesis
        assert e1.prev_hash == GENESIS_HASH
        # 第二条 prev_hash == 第一条 curr_hash
        assert e2.prev_hash == e1.curr_hash
        # 第三条 prev_hash == 第二条 curr_hash
        assert e3.prev_hash == e2.curr_hash
        # 三条 curr_hash 互不相同
        assert len({e1.curr_hash, e2.curr_hash, e3.curr_hash}) == 3


# =====================================================================
# 3. 完整链校验
# =====================================================================


class TestAuditVerifyChainIntact:
    def test_audit_verify_chain_intact(self, chain):
        """完整链 verify_chain 返回 True"""
        chain.append("tool_call", "a", "call_tool", "t1")
        chain.append("rule_triggered", "b", "trigger_rule", "t2")
        chain.append("handoff", "c", "handoff", "t3")
        assert chain.verify_chain() is True

    def test_audit_verify_chain_empty_returns_false(self, chain):
        """空链 verify_chain 返回 False"""
        assert chain.verify_chain() is False

    def test_audit_verify_chain_single_entry(self, chain):
        """单条记录链 verify_chain 通过"""
        chain.append("tool_call", "a", "call_tool", "t1")
        assert chain.verify_chain() is True


# =====================================================================
# 4. 篡改检测
# =====================================================================


class TestAuditTamperDetected:
    def test_audit_tamper_detected_curr_hash(self, chain, tmp_audit_path):
        """篡改某行 curr_hash → 校验失败"""
        chain.append("tool_call", "a", "call_tool", "t1")
        chain.append("rule_triggered", "b", "trigger_rule", "t2")
        chain.append("handoff", "c", "handoff", "t3")

        # 读取并篡改第二行的 curr_hash
        lines = tmp_audit_path.read_text(encoding="utf-8").strip().split("\n")
        tampered = json.loads(lines[1])
        tampered["curr_hash"] = "0" * 64  # 篡改
        lines[1] = json.dumps(tampered, ensure_ascii=False)
        tmp_audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # 重新构造 chain 加载篡改后的文件
        chain2 = AuditChain(persist_path=tmp_audit_path)
        assert chain2.verify_chain() is False

    def test_audit_tamper_detected_field(self, chain, tmp_audit_path):
        """篡改某行 actor 字段 → curr_hash 不匹配 → 校验失败"""
        chain.append("tool_call", "a", "call_tool", "t1")
        chain.append("rule_triggered", "b", "trigger_rule", "t2")

        # 篡改第一行的 actor（不改 curr_hash，让重算不匹配）
        lines = tmp_audit_path.read_text(encoding="utf-8").strip().split("\n")
        tampered = json.loads(lines[0])
        tampered["actor"] = "tampered-actor"
        lines[0] = json.dumps(tampered, ensure_ascii=False)
        tmp_audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        chain2 = AuditChain(persist_path=tmp_audit_path)
        assert chain2.verify_chain() is False

    def test_audit_tamper_detected_broken_prev_hash(self, chain, tmp_audit_path):
        """篡改某行 prev_hash → 链接断开 → 校验失败"""
        chain.append("tool_call", "a", "call_tool", "t1")
        chain.append("rule_triggered", "b", "trigger_rule", "t2")

        # 篡改第二行的 prev_hash（不链接到第一条 curr_hash）
        lines = tmp_audit_path.read_text(encoding="utf-8").strip().split("\n")
        tampered = json.loads(lines[1])
        tampered["prev_hash"] = "deadbeef" + "0" * 56  # 错误的 prev_hash
        lines[1] = json.dumps(tampered, ensure_ascii=False)
        tmp_audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        chain2 = AuditChain(persist_path=tmp_audit_path)
        assert chain2.verify_chain() is False


# =====================================================================
# 5. 按事件类型查询
# =====================================================================


class TestAuditQueryByEventType:
    def test_audit_query_by_event_type(self, chain):
        """按 event_type 过滤"""
        chain.append("tool_call", "a", "call_tool", "t1")
        chain.append("rule_triggered", "b", "trigger_rule", "t2")
        chain.append("tool_call", "c", "call_tool", "t3")

        results = chain.query(event_type="tool_call")
        assert len(results) == 2
        for r in results:
            assert r.event_type == "tool_call"

    def test_audit_query_event_type_no_match(self, chain):
        """查询不存在的事件类型返回空"""
        chain.append("tool_call", "a", "call_tool", "t1")
        results = chain.query(event_type="nonexistent")
        assert results == []


# =====================================================================
# 6. 按 actor 查询
# =====================================================================


class TestAuditQueryByActor:
    def test_audit_query_by_actor(self, chain):
        """按 actor 过滤"""
        chain.append("tool_call", "alice", "call_tool", "t1")
        chain.append("rule_triggered", "bob", "trigger_rule", "t2")
        chain.append("handoff", "alice", "handoff", "t3")

        results = chain.query(actor="alice")
        assert len(results) == 2
        for r in results:
            assert r.actor == "alice"


# =====================================================================
# 7. feature flag 关闭
# =====================================================================


class TestAuditDisabledNoop:
    def test_audit_disabled_append_returns_none(self, monkeypatch, tmp_audit_path):
        """feature flag 关闭：append 返回 None"""
        monkeypatch.setattr(audit_module, "AUDIT_CHAIN_ENABLED", False)
        c = AuditChain(persist_path=tmp_audit_path)
        event = c.append("tool_call", "a", "call_tool", "t1")
        assert event is None
        # 文件不创建
        assert not tmp_audit_path.exists()

    def test_audit_disabled_read_operations_return_empty(self, monkeypatch, tmp_audit_path):
        """feature flag 关闭：读操作返回空"""
        monkeypatch.setattr(audit_module, "AUDIT_CHAIN_ENABLED", False)
        c = AuditChain(persist_path=tmp_audit_path)
        assert c.query() == []
        assert c.query(event_type="any") == []
        assert c.query(actor="any") == []
        assert c.get_chain() == []
        assert c.verify_chain() is False
        assert c.count() == 0

    def test_audit_disabled_does_not_load_existing_file(self, monkeypatch, tmp_audit_path):
        """feature flag 关闭时不加载已有文件"""
        # 先开启 flag 写入数据
        monkeypatch.setattr(audit_module, "AUDIT_CHAIN_ENABLED", True)
        c1 = AuditChain(persist_path=tmp_audit_path)
        c1.append("tool_call", "a", "call_tool", "t1")
        assert tmp_audit_path.exists()
        # 关闭 flag 重新构造
        monkeypatch.setattr(audit_module, "AUDIT_CHAIN_ENABLED", False)
        c2 = AuditChain(persist_path=tmp_audit_path)
        # 即使文件存在，get_chain 仍返回 []
        assert c2.get_chain() == []
        assert c2.count() == 0


# =====================================================================
# 8. 按 target 查询 + since 时间过滤
# =====================================================================


class TestAuditQueryByTargetAndSince:
    def test_audit_query_by_target(self, chain):
        """按 target 过滤"""
        chain.append("tool_call", "a", "call_tool", "web_search")
        chain.append("tool_call", "b", "call_tool", "file_read")
        chain.append("tool_call", "c", "call_tool", "web_search")

        results = chain.query(target="web_search")
        assert len(results) == 2
        for r in results:
            assert r.target == "web_search"

    def test_audit_query_by_since(self, chain):
        """按 since 时间过滤（ISO8601 字典序）"""
        # 手工指定 timestamp 以测试 since 过滤
        chain.append("tool_call", "a", "call_tool", "t1", timestamp="2026-01-01T00:00:00")
        chain.append("tool_call", "b", "call_tool", "t2", timestamp="2026-06-01T00:00:00")
        chain.append("tool_call", "c", "call_tool", "t3", timestamp="2026-12-01T00:00:00")

        # since 2026-05-01 → 应返回后两条
        results = chain.query(since="2026-05-01T00:00:00")
        assert len(results) == 2
        assert results[0].target == "t2"
        assert results[1].target == "t3"

    def test_audit_query_combined_filter(self, chain):
        """多条件组合过滤（交集）"""
        chain.append(
            "tool_call", "alice", "call_tool", "web_search", timestamp="2026-01-01T00:00:00"
        )
        chain.append("tool_call", "bob", "call_tool", "web_search", timestamp="2026-06-01T00:00:00")
        chain.append(
            "rule_triggered", "alice", "trigger_rule", "L0", timestamp="2026-07-01T00:00:00"
        )

        # event_type=tool_call + actor=alice → 1 条
        results = chain.query(event_type="tool_call", actor="alice")
        assert len(results) == 1
        assert results[0].target == "web_search"


# =====================================================================
# 9. compute_hash 确定性
# =====================================================================


class TestAuditComputeHash:
    def test_audit_compute_hash_deterministic(self):
        """相同字段产生相同 hash"""
        e1 = AuditEvent(
            event_id="evt-1",
            event_type="tool_call",
            actor="a",
            action="call_tool",
            target="t",
            timestamp="2026-01-01T00:00:00",
            metadata={"k": "v"},
            prev_hash=GENESIS_HASH,
        )
        e2 = AuditEvent(
            event_id="evt-1",
            event_type="tool_call",
            actor="a",
            action="call_tool",
            target="t",
            timestamp="2026-01-01T00:00:00",
            metadata={"k": "v"},
            prev_hash=GENESIS_HASH,
        )
        assert compute_hash(e1) == compute_hash(e2)
        assert len(compute_hash(e1)) == 64

    def test_audit_compute_hash_metadata_order_stable(self):
        """metadata dict 顺序不同但内容相同 → 相同 hash（sort_keys）"""
        e1 = AuditEvent(
            event_id="evt-1",
            event_type="t",
            actor="a",
            action="act",
            target="t",
            timestamp="ts",
            metadata={"b": "2", "a": "1"},
            prev_hash=GENESIS_HASH,
        )
        e2 = AuditEvent(
            event_id="evt-1",
            event_type="t",
            actor="a",
            action="act",
            target="t",
            timestamp="ts",
            metadata={"a": "1", "b": "2"},
            prev_hash=GENESIS_HASH,
        )
        assert compute_hash(e1) == compute_hash(e2)

    def test_audit_compute_hash_unserializable_metadata_falls_back(self):
        """不可序列化 metadata 退化为 default=str，不抛异常"""

        class Obj:
            def __str__(self):
                return "<obj>"

        e = AuditEvent(
            event_id="evt",
            event_type="t",
            actor="a",
            action="act",
            target="t",
            timestamp="ts",
            metadata={"obj": Obj()},
            prev_hash=GENESIS_HASH,
        )
        h = compute_hash(e)
        assert h
        assert len(h) == 64


# =====================================================================
# 10. 持久化往返 + 损坏行容错
# =====================================================================


class TestAuditPersistence:
    def test_audit_persistence_roundtrip(self, chain, tmp_audit_path):
        """写入后重启能加载，且能继续追加，链式 hash 衔接正确"""
        chain.append("tool_call", "a", "call_tool", "t1", {"k": "v"})
        chain.append("rule_triggered", "b", "trigger_rule", "t2")

        # 文件存在且有 2 行
        assert tmp_audit_path.exists()
        content = tmp_audit_path.read_text(encoding="utf-8")
        lines = [line for line in content.strip().split("\n") if line]
        assert len(lines) == 2

        # 重新构造 chain（模拟重启）能加载链
        chain2 = AuditChain(persist_path=tmp_audit_path)
        events = chain2.get_chain()
        assert len(events) == 2
        # 重启后能继续追加，链式 hash 衔接正确
        e3 = chain2.append("handoff", "c", "handoff", "t3")
        assert e3.prev_hash == events[1].curr_hash
        # 链仍然完整
        assert chain2.verify_chain() is True

    def test_audit_corrupt_line_skipped(self, tmp_audit_path):
        """get_chain 跳过损坏行"""
        tmp_audit_path.parent.mkdir(parents=True, exist_ok=True)
        valid_line = json.dumps(
            {
                "event_id": "evt-1",
                "event_type": "tool_call",
                "actor": "a",
                "action": "call_tool",
                "target": "t",
                "timestamp": "2026-01-01T00:00:00",
                "metadata": {},
                "prev_hash": GENESIS_HASH,
                "curr_hash": "deadbeef" + "0" * 56,
            },
            ensure_ascii=False,
        )
        corrupt_line = "not a valid json {"
        tmp_audit_path.write_text(valid_line + "\n" + corrupt_line + "\n", encoding="utf-8")

        c = AuditChain(persist_path=tmp_audit_path)
        events = c.get_chain()
        # 只加载了第一行（合法），跳过第二行（损坏）
        assert len(events) == 1
        assert events[0].event_id == "evt-1"

    def test_audit_append_only_does_not_overwrite(self, chain, tmp_audit_path):
        """追加新记录不覆盖已有记录（append-only）"""
        chain.append("tool_call", "a", "call_tool", "t1")
        first_size = tmp_audit_path.stat().st_size
        chain.append("rule_triggered", "b", "trigger_rule", "t2")
        second_size = tmp_audit_path.stat().st_size
        # 第二次写入后文件变大（追加而非覆盖）
        assert second_size > first_size


# =====================================================================
# 11. 全局单例
# =====================================================================


class TestAuditGlobalSingleton:
    def test_get_audit_chain_singleton(self):
        """get_audit_chain 返回同一实例"""
        c1 = get_audit_chain()
        c2 = get_audit_chain()
        assert c1 is c2

    def test_reset_audit_chain(self):
        """reset 后下次 get 返回新实例"""
        c1 = get_audit_chain()
        reset_audit_chain()
        c2 = get_audit_chain()
        assert c1 is not c2


# =====================================================================
# 12. AuditEvent 序列化
# =====================================================================


class TestAuditEventSerialization:
    def test_to_dict_from_dict_roundtrip(self):
        """to_dict / from_dict 往返"""
        event = AuditEvent(
            event_id="evt-1",
            event_type="tool_call",
            actor="a",
            action="call_tool",
            target="t",
            timestamp="2026-01-01T00:00:00",
            metadata={"k": "v"},
            prev_hash=GENESIS_HASH,
            curr_hash="deadbeef" + "0" * 56,
        )
        d = event.to_dict()
        event2 = AuditEvent.from_dict(d)
        assert event2.event_id == event.event_id
        assert event2.event_type == event.event_type
        assert event2.actor == event.actor
        assert event2.action == event.action
        assert event2.target == event.target
        assert event2.timestamp == event.timestamp
        assert event2.metadata == event.metadata
        assert event2.prev_hash == event.prev_hash
        assert event2.curr_hash == event.curr_hash

    def test_from_dict_missing_fields_uses_defaults(self):
        """from_dict 缺失字段填默认"""
        event = AuditEvent.from_dict({"event_id": "evt"})
        assert event.event_id == "evt"
        assert event.actor == ""
        assert event.prev_hash == GENESIS_HASH
        assert event.curr_hash == ""
        assert event.metadata == {}
