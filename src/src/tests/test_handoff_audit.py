"""P4.5 Handoff 状态血缘 - 测试矩阵

覆盖点：
1. test_log_handoff_basic: 基础写入 + 字段完整
2. test_chain_hash_linkage: 链式 hash 衔接（prev_hash → curr_hash）
3. test_verify_chain_intact: 完整链校验通过
4. test_verify_chain_detects_tamper: 篡改检测（中间行被改后校验失败）
5. test_persist_to_jsonl: append-only 持久化（重启后能加载）
6. test_lineage_query: 血缘查询（按 transfer_id / agent_name）
7. test_disabled_no_change: feature flag 关闭行为不变
8. test_context_variables_only_hash_persisted: PII 不落盘（仅存 hash）
9. test_corrupt_line_skipped: 损坏行容错
10. test_persistence_failure_degrades_gracefully: 持久化失败降级
"""

from __future__ import annotations

import json
from pathlib import Path

import deadman.orchestration.handoff_audit as audit_module
import pytest
from deadman.orchestration.handoff_audit import (
    HandoffAuditEntry,
    HandoffAuditLogger,
    _compute_context_hash,
    _compute_curr_hash,
    get_handoff_audit_logger,
    reset_handoff_audit_logger,
)

# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture(autouse=True)
def _enable_audit(monkeypatch):
    """每个测试默认开启 handoff audit feature flag"""
    monkeypatch.setattr(audit_module, "HANDOFF_AUDIT_ENABLED", True)
    # 重置全局单例，避免跨测试污染
    reset_handoff_audit_logger()
    yield
    reset_handoff_audit_logger()


@pytest.fixture
def tmp_audit_path(tmp_path) -> Path:
    """临时审计文件路径（每个测试独立）"""
    return tmp_path / "handoff_audit.jsonl"


@pytest.fixture
def logger(tmp_audit_path) -> HandoffAuditLogger:
    """构造一个用临时路径的 logger"""
    return HandoffAuditLogger(persist_path=tmp_audit_path)


# =====================================================================
# 1. 基础写入
# =====================================================================


class TestLogHandoffBasic:
    def test_log_handoff_basic(self, logger, tmp_audit_path):
        """log_handoff 返回 HandoffAuditEntry，字段完整"""
        entry = logger.log_handoff(
            transfer_id="tx-001",
            from_agent="death_aftercare",
            to_agent="legal_advisor",
            reason="检测到法律信号",
            compressed_message="用户咨询遗产继承",
            context_variables={"location": "北京"},
        )
        assert entry is not None
        assert entry.transfer_id == "tx-001"
        assert entry.from_agent == "death_aftercare"
        assert entry.to_agent == "legal_advisor"
        assert entry.reason == "检测到法律信号"
        assert entry.compressed_message == "用户咨询遗产继承"
        # created_at 自动填充
        assert entry.created_at
        # 首条记录的 prev_hash 为 genesis（64 个 0）
        assert entry.prev_hash == "0" * 64
        # curr_hash 已计算
        assert entry.curr_hash
        assert len(entry.curr_hash) == 64
        # 文件已写入
        assert tmp_audit_path.exists()

    def test_log_handoff_auto_generates_transfer_id(self, logger):
        """transfer_id 为空时自动生成 uuid"""
        entry = logger.log_handoff(
            transfer_id="",
            from_agent="a",
            to_agent="b",
            reason="r",
            compressed_message="",
        )
        assert entry is not None
        assert entry.transfer_id  # 自动生成的非空 ID

    def test_log_handoff_empty_context_variables(self, logger):
        """context_variables 为空时 context_variables_hash 为空字符串"""
        entry = logger.log_handoff(
            transfer_id="tx",
            from_agent="a",
            to_agent="b",
            reason="r",
            compressed_message="",
            context_variables=None,
        )
        assert entry is not None
        assert entry.context_variables_hash == ""


# =====================================================================
# 2. 链式 hash 衔接
# =====================================================================


class TestChainHashLinkage:
    def test_chain_hash_linkage(self, logger):
        """多条记录的 prev_hash 链接到前一条的 curr_hash"""
        e1 = logger.log_handoff("tx-1", "a", "b", "r1", "m1", {"k": "v1"})
        e2 = logger.log_handoff("tx-2", "b", "c", "r2", "m2", {"k": "v2"})
        e3 = logger.log_handoff("tx-3", "c", "d", "r3", "m3", {"k": "v3"})

        # 第一条 prev_hash 是 genesis
        assert e1.prev_hash == "0" * 64
        # 第二条 prev_hash == 第一条 curr_hash
        assert e2.prev_hash == e1.curr_hash
        # 第三条 prev_hash == 第二条 curr_hash
        assert e3.prev_hash == e2.curr_hash
        # 三条 curr_hash 互不相同
        assert len({e1.curr_hash, e2.curr_hash, e3.curr_hash}) == 3

    def test_curr_hash_is_deterministic(self):
        """相同字段值产生相同 curr_hash"""
        entry1 = HandoffAuditEntry(
            transfer_id="tx",
            from_agent="a",
            to_agent="b",
            reason="r",
            compressed_message="m",
            context_variables_hash="abc",
            created_at="2026-01-01T00:00:00",
            prev_hash="0" * 64,
        )
        entry2 = HandoffAuditEntry(
            transfer_id="tx",
            from_agent="a",
            to_agent="b",
            reason="r",
            compressed_message="m",
            context_variables_hash="abc",
            created_at="2026-01-01T00:00:00",
            prev_hash="0" * 64,
        )
        assert _compute_curr_hash(entry1) == _compute_curr_hash(entry2)


# =====================================================================
# 3. 完整链校验
# =====================================================================


class TestVerifyChainIntact:
    def test_verify_chain_intact(self, logger):
        """完整链 verify_chain 返回 True"""
        logger.log_handoff("tx-1", "a", "b", "r1", "m1")
        logger.log_handoff("tx-2", "b", "c", "r2", "m2")
        logger.log_handoff("tx-3", "c", "d", "r3", "m3")
        assert logger.verify_chain() is True

    def test_verify_chain_empty_returns_false(self, logger):
        """空链 verify_chain 返回 False"""
        assert logger.verify_chain() is False

    def test_verify_chain_single_entry(self, logger):
        """单条记录链 verify_chain 通过"""
        logger.log_handoff("tx-1", "a", "b", "r1", "m1")
        assert logger.verify_chain() is True


# =====================================================================
# 4. 篡改检测
# =====================================================================


class TestVerifyChainDetectsTamper:
    def test_verify_chain_detects_tampered_curr_hash(self, logger, tmp_audit_path):
        """篡改某行 curr_hash → 校验失败"""
        logger.log_handoff("tx-1", "a", "b", "r1", "m1")
        logger.log_handoff("tx-2", "b", "c", "r2", "m2")
        logger.log_handoff("tx-3", "c", "d", "r3", "m3")

        # 读取并篡改第二行的 curr_hash
        lines = tmp_audit_path.read_text(encoding="utf-8").strip().split("\n")
        tampered = json.loads(lines[1])
        tampered["curr_hash"] = "0" * 64  # 篡改
        lines[1] = json.dumps(tampered, ensure_ascii=False)
        tmp_audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # 重新构造 logger 加载篡改后的文件
        logger2 = HandoffAuditLogger(persist_path=tmp_audit_path)
        assert logger2.verify_chain() is False

    def test_verify_chain_detects_tampered_field(self, logger, tmp_audit_path):
        """篡改某行 from_agent 字段 → curr_hash 不匹配 → 校验失败"""
        logger.log_handoff("tx-1", "a", "b", "r1", "m1")
        logger.log_handoff("tx-2", "b", "c", "r2", "m2")

        # 篡改第一行的 from_agent（不改 curr_hash，让重算不匹配）
        lines = tmp_audit_path.read_text(encoding="utf-8").strip().split("\n")
        tampered = json.loads(lines[0])
        tampered["from_agent"] = "tampered-agent"
        lines[0] = json.dumps(tampered, ensure_ascii=False)
        tmp_audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        logger2 = HandoffAuditLogger(persist_path=tmp_audit_path)
        assert logger2.verify_chain() is False

    def test_verify_chain_detects_broken_prev_hash_link(self, logger, tmp_audit_path):
        """篡改某行 prev_hash → 链接断开 → 校验失败"""
        logger.log_handoff("tx-1", "a", "b", "r1", "m1")
        logger.log_handoff("tx-2", "b", "c", "r2", "m2")

        # 篡改第二行的 prev_hash（不链接到第一条 curr_hash）
        lines = tmp_audit_path.read_text(encoding="utf-8").strip().split("\n")
        tampered = json.loads(lines[1])
        tampered["prev_hash"] = "deadbeef" + "0" * 56  # 错误的 prev_hash
        lines[1] = json.dumps(tampered, ensure_ascii=False)
        tmp_audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        logger2 = HandoffAuditLogger(persist_path=tmp_audit_path)
        assert logger2.verify_chain() is False


# =====================================================================
# 5. 持久化（append-only）
# =====================================================================


class TestPersistToJsonl:
    def test_persist_to_jsonl(self, logger, tmp_audit_path):
        """register 后写入 jsonl 文件，重启后能加载"""
        logger.log_handoff("tx-1", "a", "b", "r1", "m1", {"k": "v"})
        logger.log_handoff("tx-2", "b", "c", "r2", "m2")

        # 文件存在且有 2 行
        assert tmp_audit_path.exists()
        content = tmp_audit_path.read_text(encoding="utf-8")
        lines = [line for line in content.strip().split("\n") if line]
        assert len(lines) == 2

        # 重新构造 logger（模拟重启）能加载链
        logger2 = HandoffAuditLogger(persist_path=tmp_audit_path)
        chain = logger2.get_chain()
        assert len(chain) == 2
        assert chain[0].transfer_id == "tx-1"
        assert chain[1].transfer_id == "tx-2"
        # 重启后能继续追加，链式 hash 衔接正确
        e3 = logger2.log_handoff("tx-3", "c", "d", "r3", "m3")
        assert e3.prev_hash == chain[1].curr_hash
        # 链仍然完整
        assert logger2.verify_chain() is True

    def test_append_only_does_not_overwrite(self, logger, tmp_audit_path):
        """追加新记录不覆盖已有记录（append-only）"""
        logger.log_handoff("tx-1", "a", "b", "r1", "m1")
        first_size = tmp_audit_path.stat().st_size
        logger.log_handoff("tx-2", "b", "c", "r2", "m2")
        second_size = tmp_audit_path.stat().st_size
        # 第二次写入后文件变大（追加而非覆盖）
        assert second_size > first_size

    def test_count_returns_correct_number(self, logger):
        """count 返回正确的记录数"""
        assert logger.count() == 0
        logger.log_handoff("tx-1", "a", "b", "r1", "m1")
        assert logger.count() == 1
        logger.log_handoff("tx-2", "b", "c", "r2", "m2")
        logger.log_handoff("tx-3", "c", "d", "r3", "m3")
        assert logger.count() == 3


# =====================================================================
# 6. 血缘查询
# =====================================================================


class TestLineageQuery:
    def test_get_lineage_by_transfer_id(self, logger):
        """按 transfer_id 过滤"""
        logger.log_handoff("tx-1", "a", "b", "r1", "m1")
        logger.log_handoff("tx-2", "b", "c", "r2", "m2")
        logger.log_handoff("tx-3", "c", "d", "r3", "m3")

        results = logger.get_lineage(transfer_id="tx-2")
        assert len(results) == 1
        assert results[0].transfer_id == "tx-2"

    def test_get_lineage_by_agent_name(self, logger):
        """按 agent_name 过滤（from 或 to）"""
        logger.log_handoff("tx-1", "a", "b", "r1", "m1")
        logger.log_handoff("tx-2", "b", "c", "r2", "m2")
        logger.log_handoff("tx-3", "c", "d", "r3", "m3")

        # agent "b" 参与 tx-1 (to) 和 tx-2 (from)
        results = logger.get_lineage(agent_name="b")
        assert len(results) == 2
        transfer_ids = {r.transfer_id for r in results}
        assert transfer_ids == {"tx-1", "tx-2"}

    def test_get_lineage_combined_filter(self, logger):
        """同时按 transfer_id + agent_name 过滤（交集）"""
        logger.log_handoff("tx-1", "a", "b", "r1", "m1")
        logger.log_handoff("tx-2", "b", "c", "r2", "m2")

        # 交集：tx-1 + agent b → 1 条（tx-1 中 b 是 to）
        results = logger.get_lineage(transfer_id="tx-1", agent_name="b")
        assert len(results) == 1
        # 交集：tx-1 + agent c → 0 条（tx-1 没有 c）
        results = logger.get_lineage(transfer_id="tx-1", agent_name="c")
        assert len(results) == 0

    def test_get_lineage_no_filter_returns_all(self, logger):
        """都不给定返回完整链"""
        logger.log_handoff("tx-1", "a", "b", "r1", "m1")
        logger.log_handoff("tx-2", "b", "c", "r2", "m2")
        results = logger.get_lineage()
        assert len(results) == 2

    def test_get_lineage_chain_for_agent(self, logger):
        """get_lineage_chain_for_agent 返回该 agent 的所有 in/out 记录"""
        logger.log_handoff("tx-1", "a", "b", "r1", "m1")
        logger.log_handoff("tx-2", "b", "c", "r2", "m2")
        logger.log_handoff("tx-3", "c", "d", "r3", "m3")

        # b 的转交链：tx-1 (b 入向) + tx-2 (b 出向)
        chain = logger.get_lineage_chain_for_agent("b")
        assert len(chain) == 2
        assert chain[0].transfer_id == "tx-1"
        assert chain[1].transfer_id == "tx-2"

    def test_get_chain_returns_in_order(self, logger):
        """get_chain 按文件顺序（时间顺序）返回"""
        logger.log_handoff("tx-1", "a", "b", "r1", "m1")
        logger.log_handoff("tx-2", "b", "c", "r2", "m2")
        logger.log_handoff("tx-3", "c", "d", "r3", "m3")
        chain = logger.get_chain()
        assert [e.transfer_id for e in chain] == ["tx-1", "tx-2", "tx-3"]


# =====================================================================
# 7. feature flag 关闭
# =====================================================================


class TestDisabledNoChange:
    def test_disabled_log_handoff_returns_none(self, monkeypatch, tmp_audit_path):
        """feature flag 关闭：log_handoff 返回 None"""
        monkeypatch.setattr(audit_module, "HANDOFF_AUDIT_ENABLED", False)
        logr = HandoffAuditLogger(persist_path=tmp_audit_path)
        entry = logr.log_handoff("tx", "a", "b", "r", "m")
        assert entry is None
        # 文件不创建
        assert not tmp_audit_path.exists()

    def test_disabled_read_operations_return_empty(self, monkeypatch, tmp_audit_path):
        """feature flag 关闭：读操作返回空"""
        monkeypatch.setattr(audit_module, "HANDOFF_AUDIT_ENABLED", False)
        logr = HandoffAuditLogger(persist_path=tmp_audit_path)
        assert logr.get_chain() == []
        assert logr.get_lineage() == []
        assert logr.get_lineage(transfer_id="any") == []
        assert logr.get_lineage(agent_name="any") == []
        assert logr.get_lineage_chain_for_agent("any") == []
        assert logr.verify_chain() is False
        assert logr.count() == 0

    def test_disabled_does_not_load_existing_file(self, monkeypatch, tmp_audit_path):
        """feature flag 关闭时不加载已有文件"""
        # 先开启 flag 写入数据
        monkeypatch.setattr(audit_module, "HANDOFF_AUDIT_ENABLED", True)
        logr1 = HandoffAuditLogger(persist_path=tmp_audit_path)
        logr1.log_handoff("tx", "a", "b", "r", "m")
        assert tmp_audit_path.exists()
        # 关闭 flag 重新构造
        monkeypatch.setattr(audit_module, "HANDOFF_AUDIT_ENABLED", False)
        logr2 = HandoffAuditLogger(persist_path=tmp_audit_path)
        # 即使文件存在，get_chain 仍返回 []
        assert logr2.get_chain() == []
        assert logr2.count() == 0


# =====================================================================
# 8. PII 不落盘（仅存 hash）
# =====================================================================


class TestContextVariablesOnlyHashPersisted:
    def test_context_variables_only_hash_persisted(self, logger, tmp_audit_path):
        """context_variables 原始值不落盘，仅 hash 落盘（PII 保护）"""
        ctx = {"user_name": "张三", "phone": "13800138000", "location": "北京"}
        logger.log_handoff("tx", "a", "b", "r", "m", context_variables=ctx)

        # 读取文件内容，校验 context_variables 原始值不存在
        content = tmp_audit_path.read_text(encoding="utf-8")
        assert "张三" not in content
        assert "13800138000" not in content
        # context_variables_hash 存在
        line = json.loads(content.strip())
        assert line["context_variables_hash"]
        assert len(line["context_variables_hash"]) == 64

    def test_compute_context_hash_stable(self):
        """相同 dict 产生相同 hash（sort_keys 保证稳定）"""
        ctx1 = {"b": "2", "a": "1"}
        ctx2 = {"a": "1", "b": "2"}  # 顺序不同，内容相同
        h1 = _compute_context_hash(ctx1)
        h2 = _compute_context_hash(ctx2)
        assert h1 == h2
        assert len(h1) == 64

    def test_compute_context_hash_empty_returns_empty(self):
        """空 dict / None 返回空字符串"""
        assert _compute_context_hash(None) == ""
        assert _compute_context_hash({}) == ""

    def test_compute_context_hash_unserializable_falls_back(self):
        """不可序列化对象退化为空字符串（不抛异常）"""

        # set 不可 JSON 序列化，但 default=str 兜底
        # 真正不可序列化的对象需要构造特殊场景
        class Unserializable:
            def __repr__(self):
                return "<unserializable>"

        # default=str 会把对象转成字符串，所以这里应该能产生 hash
        ctx = {"obj": Unserializable()}
        h = _compute_context_hash(ctx)
        # default=str 让 JSON 能序列化，hash 仍能计算
        assert h
        assert len(h) == 64


# =====================================================================
# 9. 损坏行容错
# =====================================================================


class TestCorruptLineSkipped:
    def test_corrupt_line_skipped_in_get_chain(self, tmp_audit_path):
        """get_chain 跳过损坏行"""
        # 手工写一个文件，第二行是损坏 JSON
        tmp_audit_path.parent.mkdir(parents=True, exist_ok=True)
        valid_line = json.dumps(
            {
                "transfer_id": "tx-1",
                "from_agent": "a",
                "to_agent": "b",
                "reason": "r",
                "compressed_message": "m",
                "context_variables_hash": "",
                "created_at": "2026-01-01T00:00:00",
                "prev_hash": "0" * 64,
                "curr_hash": "deadbeef" + "0" * 56,
            },
            ensure_ascii=False,
        )
        corrupt_line = "not a valid json {"
        tmp_audit_path.write_text(valid_line + "\n" + corrupt_line + "\n", encoding="utf-8")

        logr = HandoffAuditLogger(persist_path=tmp_audit_path)
        chain = logr.get_chain()
        # 只加载了第一行（合法），跳过第二行（损坏）
        assert len(chain) == 1
        assert chain[0].transfer_id == "tx-1"

    def test_empty_lines_skipped(self, tmp_audit_path):
        """空行被跳过"""
        tmp_audit_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_audit_path.write_text("\n\n  \n", encoding="utf-8")
        logr = HandoffAuditLogger(persist_path=tmp_audit_path)
        assert logr.get_chain() == []
        assert logr.count() == 0

    def test_load_corrupt_file_does_not_crash(self, tmp_audit_path):
        """加载完全损坏的文件不抛异常"""
        tmp_audit_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_audit_path.write_text("totally not json at all", encoding="utf-8")
        # 不抛异常
        logr = HandoffAuditLogger(persist_path=tmp_audit_path)
        assert logr.get_chain() == []


# =====================================================================
# 10. 持久化失败降级
# =====================================================================


class TestPersistenceFailureDegradation:
    def test_persistence_failure_degrades_gracefully(self, monkeypatch, tmp_audit_path):
        """文件写入失败时降级：仅内存更新 last_hash，不抛异常"""
        logr = HandoffAuditLogger(persist_path=tmp_audit_path)

        # mock _append 返回 False（模拟写入失败）
        monkeypatch.setattr(logr, "_append", lambda entry: False)

        # log_handoff 仍返回 entry（内存链推进），不抛异常
        entry = logr.log_handoff("tx", "a", "b", "r", "m")
        assert entry is not None
        assert entry.curr_hash

        # 再次写入，prev_hash 链接到上一条的 curr_hash（内存链正确）
        entry2 = logr.log_handoff("tx-2", "b", "c", "r2", "m2")
        assert entry2.prev_hash == entry.curr_hash

    def test_persistence_to_unwritable_path_does_not_raise(self, monkeypatch, tmp_path):
        """持久化到不可写路径不抛异常"""
        # 用一个不存在的嵌套路径（mkdir 失败时降级）
        # 由于 _append 内部捕获 OSError，这里直接构造一个会触发 OSError 的场景
        logr = HandoffAuditLogger(persist_path=tmp_path / "nonexistent_subdir" / "audit.jsonl")

        # mock mkdir 抛 OSError

        def _fail_mkdir(self, *args, **kwargs):
            raise OSError("mock: permission denied")

        monkeypatch.setattr(Path, "mkdir", _fail_mkdir)
        # log_handoff 不抛异常
        entry = logr.log_handoff("tx", "a", "b", "r", "m")
        assert entry is not None


# =====================================================================
# 11. 全局单例
# =====================================================================


class TestGlobalSingleton:
    def test_get_handoff_audit_logger_singleton(self):
        """get_handoff_audit_logger 返回同一实例"""
        logr1 = get_handoff_audit_logger()
        logr2 = get_handoff_audit_logger()
        assert logr1 is logr2

    def test_reset_handoff_audit_logger(self):
        """reset 后下次 get 返回新实例"""
        logr1 = get_handoff_audit_logger()
        reset_handoff_audit_logger()
        logr2 = get_handoff_audit_logger()
        assert logr1 is not logr2

    def test_clear_resets_chain(self, logger, tmp_audit_path):
        """clear 后链被清空"""
        logger.log_handoff("tx-1", "a", "b", "r1", "m1")
        logger.log_handoff("tx-2", "b", "c", "r2", "m2")
        assert logger.count() == 2

        logger.clear()
        assert logger.count() == 0
        assert logger.get_chain() == []
        assert not tmp_audit_path.exists()
        # 清空后写入新记录，prev_hash 是 genesis（链重置）
        entry = logger.log_handoff("tx-3", "c", "d", "r3", "m3")
        assert entry.prev_hash == "0" * 64


# =====================================================================
# 12. HandoffAuditEntry 序列化
# =====================================================================


class TestHandoffAuditEntrySerialization:
    def test_to_dict_from_dict_roundtrip(self):
        """to_dict / from_dict 往返"""
        entry = HandoffAuditEntry(
            transfer_id="tx-1",
            from_agent="a",
            to_agent="b",
            reason="r",
            compressed_message="m",
            context_variables_hash="abc123",
            created_at="2026-01-01T00:00:00",
            prev_hash="0" * 64,
            curr_hash="deadbeef" + "0" * 56,
        )
        d = entry.to_dict()
        entry2 = HandoffAuditEntry.from_dict(d)
        assert entry2.transfer_id == entry.transfer_id
        assert entry2.from_agent == entry.from_agent
        assert entry2.to_agent == entry.to_agent
        assert entry2.reason == entry.reason
        assert entry2.compressed_message == entry.compressed_message
        assert entry2.context_variables_hash == entry.context_variables_hash
        assert entry2.created_at == entry.created_at
        assert entry2.prev_hash == entry.prev_hash
        assert entry2.curr_hash == entry.curr_hash

    def test_from_dict_missing_fields_uses_defaults(self):
        """from_dict 缺失字段填默认"""
        entry = HandoffAuditEntry.from_dict({"transfer_id": "tx"})
        assert entry.transfer_id == "tx"
        assert entry.from_agent == ""
        assert entry.to_agent == ""
        assert entry.prev_hash == "0" * 64
        assert entry.curr_hash == ""
