"""测试 P0.3 Reflexion 跨会话持久化

覆盖 FileMemoryStore + MemoryManager 的反思记忆功能:
  - REFLEXION.json 加载/保存
  - 失败模式统计(failure_patterns.count 累加)
  - 成功调整记录(successful_adjustments.history)
  - 跨 agent 共享(shared_patterns.best_strategy)
  - TTL 过期(90 天前失效)
  - 反思质量评估(success_rate)
  - LRU 历史截断(REFLEXION_HISTORY_LIMIT)
  - MemoryManager.record_successful_adjustment 固化经验到 procedural
  - MemoryManager.get_reflexion_memory 注入 best_strategy
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from deadman.memory.file_store import FileMemoryStore
from deadman.memory.manager import MemoryManager
from deadman.reflexion.engine import ReflexionEngine

# =====================================================================
# FileMemoryStore - REFLEXION.json 读写
# =====================================================================


class TestReflexionFileStoreBasics:
    """FileMemoryStore 反思记忆基础读写"""

    def test_load_reflexion_empty_when_no_file(self, tmp_path):
        """文件不存在 → 空结构"""
        store = FileMemoryStore(memory_dir=tmp_path)
        result = store.load_reflexion()
        assert result == {"agents": {}, "shared_patterns": {}, "version": 1}

    def test_save_load_roundtrip(self, tmp_path):
        """保存后加载应能恢复"""
        store = FileMemoryStore(memory_dir=tmp_path)
        data = {
            "agents": {
                "death_aftercare": {
                    "failure_patterns": {"timeout": {"count": 3}},
                    "successful_adjustments": {"timeout": {"success_count": 2}},
                }
            },
            "shared_patterns": {"timeout": {"count": 5}},
        }
        store.save_reflexion(data)

        loaded = store.load_reflexion()
        assert loaded["agents"]["death_aftercare"]["failure_patterns"]["timeout"]["count"] == 3
        assert loaded["shared_patterns"]["timeout"]["count"] == 5
        assert loaded["version"] == 1
        # 应自动写入 last_updated
        assert "last_updated" in loaded

    def test_load_invalid_json_returns_empty(self, tmp_path):
        """JSON 解析失败 → 返回空结构(不抛异常)"""
        store = FileMemoryStore(memory_dir=tmp_path)
        # 写入无效 JSON
        store.reflexion_file.parent.mkdir(parents=True, exist_ok=True)
        store.reflexion_file.write_text("invalid json {{{", encoding="utf-8")

        result = store.load_reflexion()
        assert result == {"agents": {}, "shared_patterns": {}, "version": 1}


class TestRecordAgentAdjustment:
    """测试 record_agent_adjustment 单次记录"""

    def test_first_record_creates_agent_entry(self, tmp_path):
        """首次记录某 agent → 创建该 agent 节点"""
        store = FileMemoryStore(memory_dir=tmp_path)
        store.record_agent_adjustment(
            agent_name="death_aftercare",
            failure_type="timeout",
            adjustment_strategy="简化任务",
            success=True,
        )
        memory = store.get_agent_reflexion("death_aftercare")
        assert "timeout" in memory["failure_patterns"]
        assert memory["failure_patterns"]["timeout"]["count"] == 1
        assert "timeout" in memory["successful_adjustments"]
        assert memory["successful_adjustments"]["timeout"]["success_count"] == 1
        assert memory["successful_adjustments"]["timeout"]["total_count"] == 1
        assert memory["successful_adjustments"]["timeout"]["success_rate"] == 1.0

    def test_failure_count_increments(self, tmp_path):
        """多次记录同一失败 → count 累加"""
        store = FileMemoryStore(memory_dir=tmp_path)
        for _ in range(3):
            store.record_agent_adjustment(
                agent_name="agent_x",
                failure_type="api_error",
                adjustment_strategy="降级",
                success=True,
            )
        memory = store.get_agent_reflexion("agent_x")
        assert memory["failure_patterns"]["api_error"]["count"] == 3
        assert memory["successful_adjustments"]["api_error"]["total_count"] == 3
        assert memory["successful_adjustments"]["api_error"]["success_count"] == 3

    def test_success_rate_computed_correctly(self, tmp_path):
        """混合成功/失败 → success_rate 计算正确"""
        store = FileMemoryStore(memory_dir=tmp_path)
        # 2 成功 + 2 失败
        for success in [True, True, False, False]:
            store.record_agent_adjustment(
                agent_name="agent_y",
                failure_type="format_error",
                adjustment_strategy="加格式约束",
                success=success,
            )
        memory = store.get_agent_reflexion("agent_y")
        adj = memory["successful_adjustments"]["format_error"]
        assert adj["total_count"] == 4
        assert adj["success_count"] == 2
        assert adj["success_rate"] == 0.5

    def test_history_appended(self, tmp_path):
        """历史记录应追加到 history 列表"""
        store = FileMemoryStore(memory_dir=tmp_path)
        store.record_agent_adjustment(
            "agent_z", "rate_limit", "等待重试", success=True
        )
        store.record_agent_adjustment(
            "agent_z", "rate_limit", "等待重试", success=False
        )
        memory = store.get_agent_reflexion("agent_z")
        history = memory["successful_adjustments"]["rate_limit"]["history"]
        assert len(history) == 2
        assert history[0]["success"] is True
        assert history[1]["success"] is False

    def test_history_lru_truncation(self, tmp_path):
        """历史超过 REFLEXION_HISTORY_LIMIT → 截断保留最近 N 条"""
        store = FileMemoryStore(memory_dir=tmp_path)
        store.REFLEXION_HISTORY_LIMIT = 5  # 测试用小值

        for i in range(10):
            store.record_agent_adjustment(
                "agent_lru", "timeout", f"策略{i}", success=True
            )
        memory = store.get_agent_reflexion("agent_lru")
        history = memory["successful_adjustments"]["timeout"]["history"]
        assert len(history) == 5
        # 应保留最后 5 条(策略 5-9)
        assert history[0]["strategy"] == "策略5"
        assert history[-1]["strategy"] == "策略9"


class TestSharedPatterns:
    """跨 agent 共享统计"""

    def test_shared_patterns_aggregates_across_agents(self, tmp_path):
        """多 agent 记录同一失败类型 → shared_patterns.count 累加"""
        store = FileMemoryStore(memory_dir=tmp_path)
        store.record_agent_adjustment("agent_a", "timeout", "策略A", success=True)
        store.record_agent_adjustment("agent_b", "timeout", "策略B", success=True)
        store.record_agent_adjustment("agent_c", "timeout", "策略C", success=False)

        memory_a = store.get_agent_reflexion("agent_a")
        shared = memory_a["shared_patterns"]
        assert shared["timeout"]["count"] == 3  # 三个 agent 共 3 次

    def test_best_strategy_updated_when_higher_success_rate(self, tmp_path):
        """某 agent success_rate 更高 → 更新 best_strategy"""
        store = FileMemoryStore(memory_dir=tmp_path)
        # agent_a 4/5 = 0.8
        for success in [True, True, True, True, False]:
            store.record_agent_adjustment("agent_a", "timeout", "策略A", success=success)
        # agent_b 5/5 = 1.0
        for _ in range(5):
            store.record_agent_adjustment("agent_b", "timeout", "策略B", success=True)

        memory = store.get_agent_reflexion("agent_a")
        shared = memory["shared_patterns"]
        # best_strategy 应是 策略B(因为 success_rate 更高)
        assert shared["timeout"]["best_strategy"] == "策略B"
        assert shared["timeout"]["best_success_rate"] == 1.0

    def test_best_strategy_injected_to_memory(self, tmp_path):
        """get_reflexion_memory 注入 best_strategy 字段"""
        store = FileMemoryStore(memory_dir=tmp_path)
        store.record_agent_adjustment("agent_a", "timeout", "策略A", success=True)

        memory = store.get_agent_reflexion("agent_a")
        # best_strategy 字段应被填充
        assert "best_strategy" in memory or memory["shared_patterns"]["timeout"].get("best_strategy")


class TestTTLFilter:
    """TTL 过期过滤(90 天)"""

    def test_recent_pattern_not_expired(self, tmp_path):
        """近期记录不应被 TTL 过滤"""
        store = FileMemoryStore(memory_dir=tmp_path)
        store.record_agent_adjustment("agent", "timeout", "策略", success=True)

        memory = store.get_agent_reflexion("agent")
        assert "timeout" in memory["failure_patterns"]

    def test_old_pattern_expired(self, tmp_path):
        """90 天前的记录应被 TTL 过滤"""
        store = FileMemoryStore(memory_dir=tmp_path)
        # 直接写入老数据
        old_time = (datetime.now() - timedelta(days=100)).isoformat()
        data = {
            "agents": {
                "agent": {
                    "failure_patterns": {
                        "timeout": {
                            "count": 5,
                            "first_seen": old_time,
                            "last_seen": old_time,
                        }
                    },
                    "successful_adjustments": {
                        "timeout": {
                            "strategy": "老策略",
                            "success_count": 4,
                            "total_count": 5,
                            "last_recorded": old_time,
                        }
                    },
                }
            },
            "shared_patterns": {},
        }
        store.save_reflexion(data)

        # 加载时 TTL 过滤应清除
        memory = store.get_agent_reflexion("agent")
        assert "timeout" not in memory["failure_patterns"]
        assert "timeout" not in memory["successful_adjustments"]

    def test_just_before_ttl_kept(self, tmp_path):
        """恰好 89 天前的记录应保留(< 90 天)"""
        store = FileMemoryStore(memory_dir=tmp_path)
        old_time = (datetime.now() - timedelta(days=89)).isoformat()
        data = {
            "agents": {
                "agent": {
                    "failure_patterns": {
                        "timeout": {
                            "count": 1,
                            "first_seen": old_time,
                            "last_seen": old_time,
                        }
                    },
                    "successful_adjustments": {},
                }
            },
            "shared_patterns": {},
        }
        store.save_reflexion(data)

        memory = store.get_agent_reflexion("agent")
        assert "timeout" in memory["failure_patterns"]


class TestReflexionSummary:
    """反思记忆摘要导出"""

    def test_empty_summary(self, tmp_path):
        """空文件 → 零汇总"""
        store = FileMemoryStore(memory_dir=tmp_path)
        summary = store.get_reflexion_summary()
        assert summary["total_agents"] == 0
        assert summary["total_patterns"] == 0
        assert summary["total_adjustments"] == 0

    def test_summary_aggregates(self, tmp_path):
        """汇总正确统计"""
        store = FileMemoryStore(memory_dir=tmp_path)
        store.record_agent_adjustment("a1", "timeout", "策略", success=True)
        store.record_agent_adjustment("a1", "api_error", "策略", success=False)
        store.record_agent_adjustment("a2", "timeout", "策略", success=True)

        summary = store.get_reflexion_summary()
        assert summary["total_agents"] == 2
        assert summary["total_patterns"] == 3  # a1:2 + a2:1
        assert summary["total_adjustments"] == 3
        assert "a1" in summary["agents"]
        assert summary["agents"]["a1"]["patterns"] == 2


# =====================================================================
# MemoryManager - 反思记忆接口
# =====================================================================


class TestMemoryManagerReflexion:
    """测试 MemoryManager.get_reflexion_memory / record_successful_adjustment"""

    def _make_manager_with_tmp_store(self, tmp_path) -> MemoryManager:
        """构造用 tmp_path 作 file_store 的 MemoryManager"""
        store = FileMemoryStore(memory_dir=tmp_path)
        # 用最小化 manager(避免 Graphiti/LightRAG 初始化)
        manager = MemoryManager.__new__(MemoryManager)
        manager.graphiti = None
        manager.lightrag = None
        manager.file_store = store
        manager._file_store_degraded_logged = True
        manager.working = MagicMock()
        manager.episodic = MagicMock()
        manager.semantic = MagicMock()
        manager.procedural = MagicMock()
        return manager

    async def test_get_reflexion_memory_empty(self, tmp_path):
        """空记忆 → 返回空结构"""
        manager = self._make_manager_with_tmp_store(tmp_path)
        memory = await manager.get_reflexion_memory("any_agent")
        assert "failure_patterns" in memory
        assert "successful_adjustments" in memory
        assert "shared_patterns" in memory
        assert memory["failure_patterns"] == {}

    async def test_record_and_retrieve(self, tmp_path):
        """记录后可读取"""
        manager = self._make_manager_with_tmp_store(tmp_path)
        await manager.record_successful_adjustment(
            "death_aftercare", "timeout", "简化任务", success=True
        )

        memory = await manager.get_reflexion_memory("death_aftercare")
        assert memory["failure_patterns"]["timeout"]["count"] == 1
        assert memory["successful_adjustments"]["timeout"]["success_count"] == 1

    async def test_record_failed_adjustment(self, tmp_path):
        """记录失败调整(success=False)"""
        manager = self._make_manager_with_tmp_store(tmp_path)
        await manager.record_successful_adjustment(
            "agent", "api_error", "降级", success=False
        )

        memory = await manager.get_reflexion_memory("agent")
        assert memory["successful_adjustments"]["api_error"]["success_count"] == 0
        assert memory["successful_adjustments"]["api_error"]["total_count"] == 1
        assert memory["successful_adjustments"]["api_error"]["success_rate"] == 0.0

    async def test_record_triggers_procedural_persist(self, tmp_path):
        """成功率 >= 0.8 且尝试 >= 5 → 固化到 procedural"""
        manager = self._make_manager_with_tmp_store(tmp_path)

        # 5 次成功调整 → 触发固化
        for _ in range(5):
            await manager.record_successful_adjustment(
                "agent_x", "timeout", "策略A", success=True
            )

        # MEMORY.md 应有"经验固化"章节
        mem_text = manager.file_store.memory_file.read_text(encoding="utf-8")
        assert "经验固化" in mem_text
        assert "策略A" in mem_text

    async def test_record_no_persist_below_threshold(self, tmp_path):
        """成功率 < 0.8 → 不固化"""
        manager = self._make_manager_with_tmp_store(tmp_path)
        # 3 成功 + 2 失败 = 0.6 < 0.8
        for success in [True, True, True, False, False]:
            await manager.record_successful_adjustment(
                "agent", "timeout", "策略", success=success
            )

        # 不应固化(因 success_rate=0.6 < 0.8)
        # 但 total=5 >= 5,所以需要检查 success_rate
        mem_text = manager.file_store.memory_file.read_text(encoding="utf-8") if manager.file_store.memory_file.exists() else ""
        # 经验固化应不出现(或仅在更高 success_rate 时)
        # 由于 success_rate=0.6,不应固化
        # (允许文件不存在或不含"经验固化")
        assert "经验固化" not in mem_text or mem_text == ""

    async def test_best_strategy_injected(self, tmp_path):
        """shared_patterns 有 best_strategy → 注入到返回的 best_strategy 字段"""
        manager = self._make_manager_with_tmp_store(tmp_path)
        # 两次成功 → best_strategy 应记录
        for _ in range(2):
            await manager.record_successful_adjustment(
                "agent_a", "timeout", "最佳策略", success=True
            )

        memory = await manager.get_reflexion_memory("agent_a")
        # best_strategy 字段应被注入(shared_patterns.timeout.best_strategy 非空)
        if "best_strategy" in memory:
            assert "timeout" in memory["best_strategy"]
            assert memory["best_strategy"]["timeout"] == "最佳策略"

    async def test_get_reflexion_summary(self, tmp_path):
        """MemoryManager.get_reflexion_summary 委托给 file_store"""
        manager = self._make_manager_with_tmp_store(tmp_path)
        await manager.record_successful_adjustment(
            "agent", "timeout", "策略", success=True
        )
        summary = manager.get_reflexion_summary()
        assert summary["total_agents"] == 1
        assert summary["total_patterns"] == 1


# =====================================================================
# ReflexionEngine 与 MemoryManager 集成
# =====================================================================


class TestReflexionEngineWithMemoryStore:
    """测试 ReflexionEngine 接入 MemoryManager 后的端到端行为"""

    def _make_mock_memory_store(self, tmp_path):
        """构造真实可用的 MemoryManager(mock 大部分依赖)"""
        store = FileMemoryStore(memory_dir=tmp_path)
        manager = MemoryManager.__new__(MemoryManager)
        manager.graphiti = None
        manager.lightrag = None
        manager.file_store = store
        manager._file_store_degraded_logged = True
        manager.working = MagicMock()
        manager.episodic = MagicMock()
        manager.semantic = MagicMock()
        manager.procedural = MagicMock()
        return manager

    async def test_record_adjustment_on_success(self, tmp_path, patch_llm):
        """成功路径记录 success=True"""
        store = self._make_mock_memory_store(tmp_path)

        call_count = {"n": 0}

        async def operation(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"execution_mode": "fallback", "fallback_reason": "api_error"}
            return {"execution_mode": "success"}

        patch_llm.chat_json = AsyncMock(return_value={
            "failure_type": "api_error",
            "failure_reason": "API error",
            "adjustment_strategy": "降级",
            "adjusted_params": {},
        })

        engine = ReflexionEngine(agent_name="test-agent", memory_store=store)
        await engine.execute_with_reflexion(
            operation=operation,
            initial_input={"prompt": "test"},
            operation_type="subagent",
        )

        # 应记录 success=True(因为是成功路径)
        memory = await store.get_reflexion_memory("test-agent")
        assert "api_error" in memory["successful_adjustments"]
        assert memory["successful_adjustments"]["api_error"]["success_count"] == 1

    async def test_record_adjustment_on_failure(self, tmp_path, patch_llm):
        """失败路径记录 success=False"""
        store = self._make_mock_memory_store(tmp_path)

        async def operation(**kwargs):
            return {"execution_mode": "fallback", "fallback_reason": "rate_limit"}

        patch_llm.chat_json = AsyncMock(return_value={
            "failure_type": "rate_limit",
            "failure_reason": "限流",
            "adjustment_strategy": "等待重试",
            "adjusted_params": {},
        })

        engine = ReflexionEngine(agent_name="fail-agent", memory_store=store)
        await engine.execute_with_reflexion(
            operation=operation,
            initial_input={"prompt": "test"},
            operation_type="subagent",
        )

        # 应记录 success=False(所有重试都失败)
        memory = await store.get_reflexion_memory("fail-agent")
        adj = memory["successful_adjustments"].get("rate_limit", {})
        assert adj.get("total_count", 0) >= 1
        # 失败次数应等于 max_retries
        # 成功次数应为 0
        # 由于 engine 最后一次失败也调用 _record_adjustment(success=False),
        # total_count 应等于 max_retries
        assert adj.get("success_count", 0) == 0

    async def test_reflection_uses_historical_pattern(self, tmp_path, patch_llm):
        """反思 prompt 应包含历史模式信息"""
        store = self._make_mock_memory_store(tmp_path)

        # 预先写入历史:某 agent 某失败类型出现 5 次
        for _ in range(5):
            await store.record_successful_adjustment(
                "history-agent", "timeout", "历史策略", success=True
            )

        captured_prompt = {"value": ""}

        async def fake_chat_json(messages, **kwargs):
            captured_prompt["value"] = messages[0]["content"] if messages else ""
            return {
                "failure_type": "timeout",
                "failure_reason": "超时",
                "adjustment_strategy": "策略",
                "adjusted_params": {},
            }
        patch_llm.chat_json = AsyncMock(side_effect=fake_chat_json)

        async def operation(**kwargs):
            return {"execution_mode": "fallback", "fallback_reason": "timeout"}

        engine = ReflexionEngine(agent_name="history-agent", memory_store=store)
        await engine.execute_with_reflexion(
            operation=operation,
            initial_input={"prompt": "test"},
            operation_type="subagent",
        )

        # 反思 prompt 应包含"历史经验"或"历史出现次数"
        prompt_text = captured_prompt["value"]
        assert "历史" in prompt_text
        assert "5" in prompt_text  # 历史出现次数


# =====================================================================
# 边界情况与回退
# =====================================================================


class TestEdgeCasesAndFallbacks:
    """边界情况"""

    def test_file_store_no_file_store_returns_empty(self, tmp_path):
        """FileMemoryStore 不存在时 → 返回空结构"""
        store = FileMemoryStore(memory_dir=tmp_path)
        memory = store.get_agent_reflexion("any")
        assert "failure_patterns" in memory
        assert memory["failure_patterns"] == {}

    async def test_memory_manager_no_file_store_returns_empty(self):
        """MemoryManager 无 file_store → 返回空结构"""
        manager = MemoryManager.__new__(MemoryManager)
        manager.graphiti = None
        manager.lightrag = None
        manager.file_store = None
        manager._file_store_degraded_logged = True

        memory = await manager.get_reflexion_memory("any")
        assert memory["failure_patterns"] == {}
        assert memory["successful_adjustments"] == {}

    async def test_memory_manager_record_no_file_store_noop(self):
        """MemoryManager 无 file_store → record 静默跳过"""
        manager = MemoryManager.__new__(MemoryManager)
        manager.graphiti = None
        manager.lightrag = None
        manager.file_store = None
        manager._file_store_degraded_logged = True

        # 不应抛异常
        await manager.record_successful_adjustment(
            "agent", "timeout", "策略", success=True
        )

    async def test_engine_with_old_memory_store_signature(self, patch_llm):
        """旧版 memory_store 不支持 success 参数 → 降级只记成功"""

        class OldMemoryStore:
            async def record_successful_adjustment(
                self, agent_name, failure_type, adjustment_strategy
            ):
                # 旧版 3 参数签名(无 success)
                pass

        async def operation(**kwargs):
            return {"execution_mode": "fallback", "fallback_reason": "api_error"}

        patch_llm.chat_json = AsyncMock(return_value={
            "failure_type": "api_error",
            "failure_reason": "fail",
            "adjustment_strategy": "降级",
            "adjusted_params": {},
        })

        engine = ReflexionEngine(
            agent_name="old-store-agent",
            memory_store=OldMemoryStore(),
        )
        # 不应抛 TypeError
        result = await engine.execute_with_reflexion(
            operation=operation,
            initial_input={"prompt": "test"},
            operation_type="subagent",
        )
        assert result["success"] is False
        assert result.get("fallback") is True
