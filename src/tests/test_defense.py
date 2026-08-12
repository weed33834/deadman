"""防御性工程模块测试 - D1-D7 跨层联动风险缓解。

覆盖:
    - budget_coordinator (D1+D3):跨会话 budget + 多机制协调
    - tenant_circuit_breaker (D2):熔断器按租户隔离
    - pii_guard (D4):PII 检测 + 脱敏 + 摘要验证
    - cache_protection (D5):singleflight + 防穿透 + 防雪崩
    - degradation_guard (D6):降级风暴防护
    - cascading_guard (D7):级联故障防护
"""

from __future__ import annotations

import asyncio
import time

import pytest


@pytest.fixture(autouse=True)
def enable_defense(monkeypatch):
    """每个测试启用 defense(默认已启用,但显式确保)。

    注意:CircuitBreaker 自身有 circuit_breaker feature flag,
    所以也需要启用 circuit_breaker 才能让熔断器真正生效。
    """
    monkeypatch.setenv("DEADMAN_DEFENSE_ENABLED", "1")
    monkeypatch.setenv("DEADMAN_CIRCUIT_BREAKER_ENABLED", "1")
    monkeypatch.setenv("DEADMAN_FEATURE_FLAG_SYSTEM_ENABLED", "1")
    from deadman.infrastructure.feature_flags import get_flags

    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    # 重置全局单例
    import deadman.infrastructure.defense as defense_pkg

    defense_pkg._bc_instance = None
    defense_pkg._pr_instance = None
    defense_pkg._cp_instance = None
    defense_pkg._dg_instance = None
    defense_pkg._cg_instance = None
    # tenant_circuit_breaker 全局 registry 也重置
    from deadman.infrastructure.defense.tenant_circuit_breaker import _tcb_registry

    _tcb_registry._tenant_cbs.clear()
    # cb_registry 完全清空(避免上一个测试的 breaker 状态 / config 泄漏)
    from deadman.infrastructure.circuit_breaker import cb_registry

    cb_registry._breakers.clear()
    yield
    # 测试后重置
    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0
    cb_registry._breakers.clear()


# =====================================================================
# D1 + D3: BudgetCoordinator
# =====================================================================


class TestBudgetCoordinator:
    def test_allocate_and_check(self, tmp_path):
        from deadman.infrastructure.defense.budget_coordinator import (
            BudgetCoordinator,
            BudgetDimension,
            BudgetScope,
        )

        bc = BudgetCoordinator(store_path=tmp_path / "budget.json")
        alloc = bc.allocate(
            scope=BudgetScope.USER,
            scope_id="u1",
            dimension=BudgetDimension.LLM_TOKENS,
            amount=100,
            consumer="react_loop",
        )
        assert alloc is not None
        assert alloc.amount == 100

        # 检查
        status = bc.check(BudgetScope.USER, "u1", BudgetDimension.LLM_TOKENS)
        assert status["used"] == 100

    def test_release_refunds_unused(self, tmp_path):
        from deadman.infrastructure.defense.budget_coordinator import (
            BudgetCoordinator,
            BudgetDimension,
            BudgetScope,
        )

        bc = BudgetCoordinator(store_path=tmp_path / "budget.json")
        alloc = bc.allocate(
            scope=BudgetScope.USER,
            scope_id="u1",
            dimension=BudgetDimension.LLM_TOKENS,
            amount=100,
            consumer="test",
        )
        # 只用了 30,回退 70
        bc.release(alloc.allocation_id, actual_used=30)
        status = bc.check(BudgetScope.USER, "u1", BudgetDimension.LLM_TOKENS)
        assert status["used"] == 30

    def test_exceeding_budget_returns_none_non_strict(self, tmp_path):
        from deadman.infrastructure.defense.budget_coordinator import (
            BudgetCoordinator,
            BudgetDimension,
            BudgetScope,
        )

        bc = BudgetCoordinator(
            store_path=tmp_path / "budget.json",
            user_limits={BudgetDimension.LLM_TOKENS: 100},
        )
        # 第一次分配 80(成功)
        alloc1 = bc.allocate(
            BudgetScope.USER,
            "u1",
            BudgetDimension.LLM_TOKENS,
            80,
            "test",
        )
        assert alloc1 is not None
        # 第二次分配 30(80+30 > 100 → 返回 None 表示拒绝)
        alloc2 = bc.allocate(
            BudgetScope.USER,
            "u1",
            BudgetDimension.LLM_TOKENS,
            30,
            "test",
        )
        assert alloc2 is None

    def test_strict_raises_exception(self, tmp_path):
        from deadman.infrastructure.defense.budget_coordinator import (
            BudgetCoordinator,
            BudgetDimension,
            BudgetExceededError,
            BudgetScope,
        )

        bc = BudgetCoordinator(
            store_path=tmp_path / "budget.json",
            user_limits={BudgetDimension.LLM_TOKENS: 100},
        )
        bc.allocate(BudgetScope.USER, "u1", BudgetDimension.LLM_TOKENS, 80, "test")
        with pytest.raises(BudgetExceededError):
            bc.allocate(
                BudgetScope.USER,
                "u1",
                BudgetDimension.LLM_TOKENS,
                30,
                "test",
                strict=True,
            )

    def test_cross_scope_chain(self, tmp_path):
        """用户级 budget 应同时影响 tenant / global。"""
        from deadman.infrastructure.defense.budget_coordinator import (
            BudgetCoordinator,
            BudgetDimension,
            BudgetScope,
        )

        bc = BudgetCoordinator(
            store_path=tmp_path / "budget.json",
            global_limits={BudgetDimension.LLM_TOKENS: 1000},
            user_limits={BudgetDimension.LLM_TOKENS: 100},  # 远低于 global
        )
        # user 上限 100,远低于 global 1000
        # 分配 80 成功(user 和 global 都扣 80)
        alloc = bc.allocate(
            BudgetScope.USER,
            "u1",
            BudgetDimension.LLM_TOKENS,
            80,
            "test",
        )
        assert alloc is not None
        # 验证 global 也扣了 80
        g = bc.check(BudgetScope.GLOBAL, "global", BudgetDimension.LLM_TOKENS)
        assert g["used"] == 80

    def test_disabled_returns_virtual_alloc(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_DEFENSE_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags

        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        from deadman.infrastructure.defense.budget_coordinator import (
            BudgetCoordinator,
            BudgetDimension,
            BudgetScope,
        )

        bc = BudgetCoordinator(
            store_path=tmp_path / "budget.json",
            user_limits={BudgetDimension.LLM_TOKENS: 1},  # 极小
        )
        # 关闭后即使超限也透传
        alloc = bc.allocate(
            BudgetScope.USER,
            "u1",
            BudgetDimension.LLM_TOKENS,
            10000,
            "test",
        )
        assert alloc is not None
        assert alloc.allocation_id == "disabled"

    def test_persist_across_instances(self, tmp_path):
        """跨会话累积(用户级 budget 持久化)。"""
        from deadman.infrastructure.defense.budget_coordinator import (
            BudgetCoordinator,
            BudgetDimension,
            BudgetScope,
        )

        store = tmp_path / "budget.json"
        bc1 = BudgetCoordinator(store_path=store)
        bc1.allocate(BudgetScope.USER, "u1", BudgetDimension.LLM_TOKENS, 50, "test1")

        # 新实例(模拟新会话)
        bc2 = BudgetCoordinator(store_path=store)
        status = bc2.check(BudgetScope.USER, "u1", BudgetDimension.LLM_TOKENS)
        assert status["used"] == 50  # 跨会话累积


# =====================================================================
# D2: TenantCircuitBreaker
# =====================================================================


class TestTenantCircuitBreaker:
    def test_different_tenants_independent(self, tmp_path):
        """关键:租户 A 熔断不影响租户 B。"""
        from deadman.infrastructure.circuit_breaker import (
            CircuitBreakerOpenError,
            CircuitConfig,
            CircuitState,
        )
        from deadman.infrastructure.defense.tenant_circuit_breaker import (
            TenantCircuitBreaker,
        )

        # 配置:1 次失败就熔断(便于测试)
        cfg = CircuitConfig(
            failure_rate_threshold=1.0,
            minimum_number_of_calls=1,
            wait_duration_in_open_state_seconds=300,
        )
        tcb = TenantCircuitBreaker("llm_openai", config=cfg)

        # tenant_A 失败一次 → 熔断
        tcb.acquire(tenant_id="tA")
        tcb.release_failure(tenant_id="tA", error=RuntimeError("timeout"))
        # 再 acquire(tA) → 熔断器 Open
        with pytest.raises(CircuitBreakerOpenError):
            tcb.acquire(tenant_id="tA")
        assert tcb.get_state(tenant_id="tA") == CircuitState.OPEN

        # tenant_B 不受影响
        token = tcb.acquire(tenant_id="tB")  # 应成功
        assert token != "bypass"
        assert tcb.get_state(tenant_id="tB") == CircuitState.CLOSED

    def test_reset_tenant_only(self):
        from deadman.infrastructure.circuit_breaker import (
            CircuitConfig,
            CircuitState,
        )
        from deadman.infrastructure.defense.tenant_circuit_breaker import (
            TenantCircuitBreaker,
        )

        cfg = CircuitConfig(
            failure_rate_threshold=1.0,
            minimum_number_of_calls=1,
        )
        tcb = TenantCircuitBreaker("test", config=cfg)
        # tA 熔断
        tcb.acquire(tenant_id="tA")
        tcb.release_failure(tenant_id="tA")
        # tB 也熔断(独立)
        tcb.acquire(tenant_id="tB")
        tcb.release_failure(tenant_id="tB")
        # 仅重置 tA
        tcb.reset_tenant("tA")
        assert tcb.get_state(tenant_id="tA") == CircuitState.CLOSED
        # tB 仍是 Open
        from deadman.infrastructure.circuit_breaker import CircuitBreakerOpenError

        with pytest.raises(CircuitBreakerOpenError):
            tcb.acquire(tenant_id="tB")

    def test_disabled_uses_global(self, monkeypatch):
        """关闭 defense 后退回全局熔断器(向后兼容)。"""
        monkeypatch.setenv("DEADMAN_DEFENSE_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags

        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        from deadman.infrastructure.defense.tenant_circuit_breaker import (
            TenantCircuitBreaker,
        )

        tcb = TenantCircuitBreaker("test_global")
        # acquire 应直接走全局(不区分租户)
        token = tcb.acquire(tenant_id="tA")
        assert token is not None
        tcb.release_success(tenant_id="tA")

    def test_list_tenant_states(self):
        from deadman.infrastructure.defense.tenant_circuit_breaker import (
            TenantCircuitBreaker,
        )

        tcb = TenantCircuitBreaker("test_list")
        tcb.acquire(tenant_id="tA")
        tcb.release_success(tenant_id="tA")
        tcb.acquire(tenant_id="tB")
        tcb.release_success(tenant_id="tB")
        states = tcb.list_tenant_states()
        # key 是 tenant_id,value 是 metrics dict
        assert "tA" in states
        assert "tB" in states
        assert states["tA"]["name"].endswith(":tA")
        assert len(states) == 2


# =====================================================================
# D4: PIIGuard
# =====================================================================


class TestPIIGuard:
    def test_detect_china_id_card(self):
        from deadman.infrastructure.defense.pii_guard import (
            PIIRedactor,
            PIIType,
        )

        redactor = PIIRedactor()
        text = "我的身份证号是 110101199001011234"
        result = redactor.detect(text)
        assert result.has_pii
        assert PIIType.CHINA_ID_CARD.value in result.pii_count_by_type

    def test_detect_phone(self):
        from deadman.infrastructure.defense.pii_guard import (
            PIIRedactor,
            PIIType,
        )

        redactor = PIIRedactor()
        text = "联系电话 13812345678"
        result = redactor.detect(text)
        assert PIIType.CHINA_PHONE.value in result.pii_count_by_type

    def test_detect_email(self):
        from deadman.infrastructure.defense.pii_guard import (
            PIIRedactor,
            PIIType,
        )

        redactor = PIIRedactor()
        text = "邮箱 user@example.com"
        result = redactor.detect(text)
        assert PIIType.EMAIL.value in result.pii_count_by_type

    def test_redact_partial(self):
        from deadman.infrastructure.defense.pii_guard import (
            PIIRedactor,
            PIIType,
            RedactStrategy,
        )

        redactor = PIIRedactor(
            strategies={PIIType.CHINA_PHONE: RedactStrategy.PARTIAL},
        )
        text = "电话 13812345678 联系"
        result = redactor.redact(text)
        # 部分保留:前3 + 中间星号 + 后4
        assert "138" in result.redacted_text
        assert "5678" in result.redacted_text
        assert "*" in result.redacted_text
        # 原始不应出现完整手机号
        assert "13812345678" not in result.redacted_text

    def test_redact_full(self):
        from deadman.infrastructure.defense.pii_guard import (
            PIIRedactor,
            PIIType,
            RedactStrategy,
        )

        redactor = PIIRedactor(
            strategies={PIIType.CHINA_ID_CARD: RedactStrategy.REDACT},
        )
        text = "身份证 110101199001011234"
        result = redactor.redact(text)
        assert "110101199001011234" not in result.redacted_text
        assert "[REDACTED-PII:china_id_card]" in result.redacted_text

    def test_verify_summary_no_leak(self):
        from deadman.infrastructure.defense.pii_guard import PIIRedactor

        redactor = PIIRedactor()
        original = "电话 13812345678"
        # 摘要已脱敏 → 无泄漏
        redacted = redactor.redact(original)
        result = redactor.verify_summary(original, redacted.redacted_text)
        # 摘要 PII 数 ≤ 原文(部分被脱敏)
        assert not result["leaked"]

    def test_verify_summary_detects_drift(self):
        """摘要中 PII 比原文还多 → 检测到漂移。"""
        from deadman.infrastructure.defense.pii_guard import PIIRedactor

        redactor = PIIRedactor()
        original = "联系电话"
        # 摘要中凭空出现 PII
        summary = "电话号码是 13812345678"
        result = redactor.verify_summary(original, summary)
        assert result["leaked"]

    def test_whitelisted_pii_kept(self):
        from deadman.infrastructure.defense.pii_guard import (
            PIIRedactor,
            PIIType,
        )

        redactor = PIIRedactor(whitelisted_pii={PIIType.EMAIL})
        text = "邮箱 user@example.com"
        result = redactor.redact(text)
        # email 白名单 → 保留原值
        assert "user@example.com" in result.redacted_text

    def test_disabled_returns_text_unchanged(self, monkeypatch):
        monkeypatch.setenv("DEADMAN_DEFENSE_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags

        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        from deadman.infrastructure.defense.pii_guard import PIIRedactor

        redactor = PIIRedactor()
        text = "身份证 110101199001011234 电话 13812345678"
        result = redactor.redact(text)
        assert result.redacted_text == text  # 完全透传


# =====================================================================
# D5: CacheProtection
# =====================================================================


class TestCacheProtection:
    def test_get_or_load_basic(self):
        from deadman.infrastructure.defense.cache_protection import (
            CacheProtection,
        )

        cache = CacheProtection()
        call_count = [0]

        def loader():
            call_count[0] += 1
            return "value"

        # 第一次:miss → loader 执行
        r1 = cache.get_or_load_sync("k1", loader, ttl=10)
        assert r1 == "value"
        assert call_count[0] == 1
        # 第二次:hit → loader 不执行
        r2 = cache.get_or_load_sync("k1", loader, ttl=10)
        assert r2 == "value"
        assert call_count[0] == 1

    def test_singleflight_prevents_stampede(self):
        """同一 key 并发请求,loader 只执行一次。"""
        from deadman.infrastructure.defense.cache_protection import (
            CacheProtection,
        )

        cache = CacheProtection()
        call_count = [0]
        lock = __import__("threading").Lock()

        def loader():
            with lock:
                call_count[0] += 1
            # 模拟慢查询
            time.sleep(0.1)
            return "value"

        # 5 个线程同时请求同一 key
        import threading

        threads = []
        results = [None] * 5

        def worker(i):
            results[i] = cache.get_or_load_sync("k1", loader, ttl=10)

        for i in range(5):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # loader 只应执行 1 次(singleflight)
        assert call_count[0] == 1
        # 所有结果一致
        assert all(r == "value" for r in results)

    def test_null_cache_prevents_penetration(self):
        """空值缓存(防穿透)。"""
        from deadman.infrastructure.defense.cache_protection import (
            CacheProtection,
        )

        cache = CacheProtection()
        call_count = [0]

        def loader():
            call_count[0] += 1
            return None  # 查询不存在的 key

        # 第一次:miss → loader 执行 → 缓存空值
        r1 = cache.get_or_load_sync("nonexistent", loader, ttl=10, allow_null_cache=True)
        assert r1 is None
        assert call_count[0] == 1
        # 第二次:null 命中 → loader 不执行(防穿透)
        r2 = cache.get_or_load_sync("nonexistent", loader, ttl=10, allow_null_cache=True)
        assert r2 is None
        assert call_count[0] == 1

    def test_invalidate(self):
        from deadman.infrastructure.defense.cache_protection import CacheProtection

        cache = CacheProtection()
        cache.set("k1", "v1", ttl=10)
        assert cache.get("k1") == "v1"
        assert cache.invalidate("k1")
        assert cache.get("k1") is None

    def test_stats(self):
        from deadman.infrastructure.defense.cache_protection import CacheProtection

        cache = CacheProtection()
        cache.get_or_load_sync("k1", lambda: "v1", ttl=10)
        cache.get_or_load_sync("k1", lambda: "v1", ttl=10)  # hit
        cache.get_or_load_sync("k2", lambda: "v2", ttl=10)  # miss
        stats = cache.stats()
        assert stats.hits == 1
        assert stats.misses == 2
        assert stats.total_keys == 2

    def test_disabled_calls_loader_directly(self, monkeypatch):
        monkeypatch.setenv("DEADMAN_DEFENSE_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags

        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        from deadman.infrastructure.defense.cache_protection import CacheProtection

        cache = CacheProtection()
        call_count = [0]

        def loader():
            call_count[0] += 1
            return "v"

        # 关闭后每次都执行 loader(不缓存)
        cache.get_or_load_sync("k1", loader)
        cache.get_or_load_sync("k1", loader)
        assert call_count[0] == 2

    def test_async_singleflight(self):
        """异步 singleflight 测试。"""
        from deadman.infrastructure.defense.cache_protection import CacheProtection

        cache = CacheProtection()
        call_count = [0]

        async def loader():
            call_count[0] += 1
            await asyncio.sleep(0.05)
            return "value"

        async def main():
            # 并发 5 个相同 key 的请求
            tasks = [cache.get_or_load("k1", loader, ttl=10) for _ in range(5)]
            results = await asyncio.gather(*tasks)
            return results

        results = asyncio.run(main())
        assert all(r == "value" for r in results)
        # singleflight → 只执行 1 次
        assert call_count[0] == 1


# =====================================================================
# D6: DegradationGuard
# =====================================================================


class TestDegradationGuard:
    def test_can_degrade_soft_under_limit(self):
        from deadman.infrastructure.defense.degradation_guard import (
            DegradationEvent,
            DegradationGuard,
            DegradationLevel,
        )

        guard = DegradationGuard(max_soft_per_scope=3)
        # 第一次 soft 降级允许
        assert guard.can_degrade("quota.downgrade_model", scope="tenant:t1")
        guard.record(
            DegradationEvent(
                timestamp=time.time(),
                mechanism="quota.downgrade_model",
                level=DegradationLevel.SOFT,
                scope="tenant:t1",
            )
        )
        # 第二次允许
        assert guard.can_degrade("quota.downgrade_model", scope="tenant:t1")

    def test_blocks_when_too_many_hard(self):
        from deadman.infrastructure.defense.degradation_guard import (
            DegradationEvent,
            DegradationGuard,
            DegradationLevel,
        )

        guard = DegradationGuard(max_hard_per_scope=2)
        # 2 个 HARD(达到上限)
        for _ in range(2):
            guard.record(
                DegradationEvent(
                    timestamp=time.time(),
                    mechanism="circuit_breaker.open",
                    level=DegradationLevel.HARD,
                    scope="tenant:t1",
                    reason="test",
                )
            )
        # 第 3 个 HARD 应被拒绝
        assert not guard.can_degrade("circuit_breaker.open", scope="tenant:t1")

    def test_critical_blocks_all(self):
        from deadman.infrastructure.defense.degradation_guard import (
            DegradationEvent,
            DegradationGuard,
            DegradationLevel,
        )

        guard = DegradationGuard()
        # CRITICAL 降级
        guard.record(
            DegradationEvent(
                timestamp=time.time(),
                mechanism="quota.reject",
                level=DegradationLevel.CRITICAL,
                scope="tenant:t1",
            )
        )
        # 后续所有降级都拒绝
        assert not guard.can_degrade("quota.downgrade_model", scope="tenant:t1")
        assert not guard.can_degrade("react_loop.stuck", scope="tenant:t1")

    def test_recover_removes_event(self):
        from deadman.infrastructure.defense.degradation_guard import (
            DegradationEvent,
            DegradationGuard,
            DegradationLevel,
        )

        guard = DegradationGuard()
        guard.record(
            DegradationEvent(
                timestamp=time.time(),
                mechanism="quota.downgrade_model",
                level=DegradationLevel.SOFT,
                scope="tenant:t1",
            )
        )
        assert len(guard.get_active(scope="tenant:t1")) == 1
        # 恢复
        guard.recover("quota.downgrade_model", "tenant:t1")
        assert len(guard.get_active(scope="tenant:t1")) == 0

    def test_get_level(self):
        from deadman.infrastructure.defense.degradation_guard import (
            DegradationEvent,
            DegradationGuard,
            DegradationLevel,
        )

        guard = DegradationGuard()
        assert guard.get_level("tenant:t1") == DegradationLevel.NONE
        guard.record(
            DegradationEvent(
                timestamp=time.time(),
                mechanism="quota.downgrade_model",
                level=DegradationLevel.SOFT,
                scope="tenant:t1",
            )
        )
        assert guard.get_level("tenant:t1") == DegradationLevel.SOFT
        guard.record(
            DegradationEvent(
                timestamp=time.time(),
                mechanism="circuit_breaker.open",
                level=DegradationLevel.HARD,
                scope="tenant:t1",
            )
        )
        assert guard.get_level("tenant:t1") == DegradationLevel.HARD

    def test_stats(self):
        from deadman.infrastructure.defense.degradation_guard import (
            DegradationEvent,
            DegradationGuard,
            DegradationLevel,
        )

        guard = DegradationGuard()
        guard.record(
            DegradationEvent(
                timestamp=time.time(),
                mechanism="m1",
                level=DegradationLevel.SOFT,
                scope="t1",
            )
        )
        guard.record(
            DegradationEvent(
                timestamp=time.time(),
                mechanism="m2",
                level=DegradationLevel.HARD,
                scope="t2",
            )
        )
        stats = guard.stats()
        assert stats["total_active"] == 2
        assert stats["scopes_affected"] == 2
        assert stats["by_level"]["soft"] == 1
        assert stats["by_level"]["hard"] == 1

    def test_disabled_allows_all(self, monkeypatch):
        monkeypatch.setenv("DEADMAN_DEFENSE_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags

        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        from deadman.infrastructure.defense.degradation_guard import (
            DegradationGuard,
        )

        guard = DegradationGuard(max_hard_per_scope=1)
        # 关闭后即使已满也允许
        assert guard.can_degrade("any", scope="any")


# =====================================================================
# D8: ChainCircuitBreaker (降级链独立熔断)
# =====================================================================


class TestChainCircuitBreaker:
    """D8: 降级链独立熔断器测试。"""

    def setup_method(self):
        """每个测试前重置 chain registry。"""
        from deadman.infrastructure.defense.chain_circuit_breaker import (
            _chain_lock,
            _chain_registry,
        )

        with _chain_lock:
            _chain_registry.clear()
        # 重置 cb_registry(避免状态泄漏)
        from deadman.infrastructure.circuit_breaker import cb_registry

        cb_registry._breakers.clear()

    def test_degradation_chain_inits_with_rule_level(self):
        """链自动追加 rule 级(若未指定)。"""
        from deadman.infrastructure.defense.chain_circuit_breaker import DegradationChain

        chain = DegradationChain("test1", ["gpt-4o", "gpt-4o-mini"])
        assert "rule" in chain.levels
        # rule 在末尾
        assert chain.levels[-1] == "rule"

    def test_chain_call_uses_first_level_on_success(self):
        """正常调用走顶级。"""
        from deadman.infrastructure.defense.chain_circuit_breaker import (
            get_or_create_chain,
        )

        called_levels = []

        def func(level: str) -> str:
            called_levels.append(level)
            return f"result-from-{level}"

        chain = get_or_create_chain("test_chain_ok", ["gpt-4o", "gpt-4o-mini", "rule"])
        result = chain.call(func)
        assert result.success
        assert result.used_level == "gpt-4o"
        assert result.used_level_index == 0
        assert called_levels == ["gpt-4o"]

    def test_chain_fallback_on_failure(self):
        """顶级失败 → 自动 fallback 到下一级。"""
        from deadman.infrastructure.defense.chain_circuit_breaker import (
            get_or_create_chain,
        )

        call_count = [0]

        def func(level: str) -> str:
            call_count[0] += 1
            if level == "gpt-4o":
                raise RuntimeError("gpt-4o failed")
            return f"result-from-{level}"

        chain = get_or_create_chain("test_chain_fb", ["gpt-4o", "gpt-4o-mini", "rule"])
        result = chain.call(func)
        assert result.success
        assert result.used_level == "gpt-4o-mini"
        assert result.used_level_index == 1
        # 跳过的级别包含 gpt-4o
        assert "gpt-4o" in result.skipped_levels
        assert call_count[0] == 2  # 第一次失败 + 第二次成功

    def test_chain_fallback_to_rule_on_all_fail(self):
        """所有 LLM 级失败 → 走 rule 兜底。"""
        from deadman.infrastructure.defense.chain_circuit_breaker import (
            get_or_create_chain,
        )

        def func(level: str) -> str:
            if level == "rule":
                return "rule-based-fallback"
            raise RuntimeError(f"{level} failed")

        chain = get_or_create_chain("test_chain_rule", ["gpt-4o", "gpt-4o-mini", "rule"])
        result = chain.call(func)
        assert result.success
        assert result.used_level == "rule"

    def test_chain_full_failure_records_critical(self):
        """全链失败(含 rule)→ 记录 critical 状态。"""
        from deadman.infrastructure.defense.chain_circuit_breaker import (
            get_or_create_chain,
        )

        def func(level: str) -> str:
            raise RuntimeError(f"{level} failed")

        chain = get_or_create_chain(
            "test_chain_full_fail",
            ["gpt-4o", "gpt-4o-mini", "rule"],
        )
        result = chain.call(func)
        assert not result.success
        stats = chain.get_stats()
        assert stats["full_chain_failure"] == 1

    def test_chain_call_with_preferred_level(self):
        """指定从某级开始(用于 budget 不足时跳级)。"""
        from deadman.infrastructure.defense.chain_circuit_breaker import (
            get_or_create_chain,
        )

        called = []

        def func(level: str) -> str:
            called.append(level)
            return f"from-{level}"

        chain = get_or_create_chain("test_chain_pref", ["gpt-4o", "gpt-4o-mini", "rule"])
        result = chain.call(func, preferred_level="gpt-4o-mini")
        assert result.success
        assert result.used_level == "gpt-4o-mini"
        # gpt-4o 未被调用
        assert "gpt-4o" not in called

    def test_chain_timeout_triggers_fallback(self):
        """单级超时 → fallback。"""
        import time

        from deadman.infrastructure.defense.chain_circuit_breaker import (
            get_or_create_chain,
        )

        def func(level: str) -> str:
            if level == "gpt-4o":
                time.sleep(2)  # 超时
                return "slow"
            return f"fast-{level}"

        chain = get_or_create_chain("test_chain_to", ["gpt-4o", "gpt-4o-mini", "rule"])
        result = chain.call(func, timeout_seconds=0.5)
        assert result.success
        # 应 fallback 到 gpt-4o-mini
        assert result.used_level == "gpt-4o-mini"

    def test_chain_stats_per_level(self):
        """stats 包含每级熔断器状态。"""
        from deadman.infrastructure.defense.chain_circuit_breaker import (
            get_or_create_chain,
        )

        def func(level: str) -> str:
            return "ok"

        chain = get_or_create_chain("test_chain_stats", ["gpt-4o", "rule"])
        chain.call(func)
        stats = chain.get_stats()
        assert stats["total_calls"] == 1
        assert stats["successful_calls"] == 1
        assert "gpt-4o" in stats["levels"]
        assert "rule" in stats["levels"]

    def test_chain_disabled_passes_through(self, monkeypatch):
        """关闭 defense 时直接调顶级(透传)。"""
        monkeypatch.setenv("DEADMAN_DEFENSE_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags

        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        from deadman.infrastructure.defense.chain_circuit_breaker import (
            get_or_create_chain,
        )

        called = []

        def func(level: str) -> str:
            called.append(level)
            return "ok"

        chain = get_or_create_chain("test_chain_disabled", ["gpt-4o", "rule"])
        result = chain.call(func)
        assert result.success
        # 关闭后只调顶级
        assert called == ["gpt-4o"]

    def test_reset_all_chains(self):
        """reset_all_chains 重置所有链。"""
        from deadman.infrastructure.defense.chain_circuit_breaker import (
            get_or_create_chain,
            list_chains,
            reset_all_chains,
        )

        chain = get_or_create_chain("test_chain_reset", ["gpt-4o", "rule"])

        def func(level: str) -> str:
            return "ok"

        chain.call(func)
        assert chain.get_stats()["total_calls"] == 1

        reset_all_chains()
        # 重置后 stats 归零(若该链仍存在)
        chains = list_chains()
        if "test_chain_reset" in chains:
            assert chains["test_chain_reset"]["total_calls"] == 0


# =====================================================================
# D9: TraceAnonymizer (跨 session trace 脱敏)
# =====================================================================


class TestTraceAnonymizer:
    """D9: 跨 session trace 脱敏测试。"""

    def test_consent_grant_and_revoke(self, tmp_path):
        from deadman.infrastructure.defense.trace_anonymizer import (
            CrossSessionLinker,
            TraceLinkStrategy,
        )

        linker = CrossSessionLinker(store_path=str(tmp_path / "links.json"))
        # 未授予同意 → 不可关联
        assert not linker.can_link("user1")
        # 授予
        consent = linker.grant_consent("user1", strategy=TraceLinkStrategy.HASH, expires_in_days=30)
        assert consent.is_valid()
        assert linker.can_link("user1")
        # 撤回
        assert linker.revoke_consent("user1")
        assert not linker.can_link("user1")

    def test_consent_expires(self, tmp_path):
        import time

        from deadman.infrastructure.defense.trace_anonymizer import CrossSessionLinker

        linker = CrossSessionLinker(store_path=str(tmp_path / "links.json"))
        linker.grant_consent("user1", expires_in_days=1)
        assert linker.can_link("user1")
        # 模拟过期(直接改 expires_at)
        linker._consents["user1"].expires_at = time.time() - 1
        assert not linker.can_link("user1")

    def test_link_id_returns_none_without_consent(self, tmp_path):
        from deadman.infrastructure.defense.trace_anonymizer import CrossSessionLinker

        linker = CrossSessionLinker(store_path=str(tmp_path / "links.json"))
        # 无同意 → None
        assert linker.link_id("session1", "user1") is None

    def test_link_id_with_consent(self, tmp_path):
        from deadman.infrastructure.defense.trace_anonymizer import (
            CrossSessionLinker,
            TraceLinkStrategy,
        )

        linker = CrossSessionLinker(store_path=str(tmp_path / "links.json"))
        linker.grant_consent("user1", strategy=TraceLinkStrategy.HASH)
        link_id = linker.link_id("session1", "user1")
        assert link_id is not None
        assert len(link_id) == 32  # 截断到 32 字符
        # 同一 session+user → 相同 link_id(salt 不变时)
        link_id2 = linker.link_id("session1", "user1")
        assert link_id == link_id2

    def test_link_id_differs_for_different_sessions(self, tmp_path):
        from deadman.infrastructure.defense.trace_anonymizer import (
            CrossSessionLinker,
        )

        linker = CrossSessionLinker(store_path=str(tmp_path / "links.json"))
        linker.grant_consent("user1")
        link1 = linker.link_id("session1", "user1")
        link2 = linker.link_id("session2", "user1")
        # 不同 session → 不同 link_id
        assert link1 != link2

    def test_link_id_differs_for_different_users(self, tmp_path):
        from deadman.infrastructure.defense.trace_anonymizer import CrossSessionLinker

        linker = CrossSessionLinker(store_path=str(tmp_path / "links.json"))
        linker.grant_consent("user1")
        linker.grant_consent("user2")
        link1 = linker.link_id("session1", "user1")
        link2 = linker.link_id("session1", "user2")
        assert link1 != link2

    def test_anonymizer_redacts_sensitive_fields(self):
        from deadman.infrastructure.defense.trace_anonymizer import TraceAnonymizer

        anonymizer = TraceAnonymizer()
        record = {
            "span_type": "react.action",
            "user_id": "u123",
            "user_email": "test@example.com",
            "query": "如何写遗嘱",
            "tool_args": {"name": "张三"},
            "tool_result": {"phone": "13812345678"},
            "ip_address": "192.168.1.1",
            "non_sensitive": "kept",
        }
        sanitized = anonymizer.sanitize(record)
        # 敏感字段被替换
        assert sanitized["user_id"] != "u123"
        assert sanitized["user_email"] != "test@example.com"
        assert sanitized["query"] != "如何写遗嘱"
        assert sanitized["tool_args"] == "[REDACTED_COMPLEX]"
        assert sanitized["tool_result"] == "[REDACTED_COMPLEX]"
        # 非敏感字段保留
        assert sanitized["non_sensitive"] == "kept"

    def test_anonymizer_no_link_without_consent(self):
        from deadman.infrastructure.defense.trace_anonymizer import TraceAnonymizer

        anonymizer = TraceAnonymizer()
        record = {
            "span_type": "test",
            "user_id": "u1",
            "session_id": "s1",
        }
        sanitized = anonymizer.sanitize(
            record, user_id="u1", session_id="s1", link_cross_session=True
        )
        # 无同意 → link_id 为 None
        assert sanitized.get("link_id") is None

    def test_anonymizer_links_with_consent(self, tmp_path):
        from deadman.infrastructure.defense.trace_anonymizer import (
            CrossSessionLinker,
            TraceAnonymizer,
        )

        linker = CrossSessionLinker(store_path=str(tmp_path / "links.json"))
        linker.grant_consent("u1")
        anonymizer = TraceAnonymizer(linker=linker)
        record = {"span_type": "test", "user_id": "u1", "session_id": "s1"}
        sanitized = anonymizer.sanitize(record, user_id="u1", session_id="s1")
        assert sanitized.get("link_id") is not None

    def test_behavior_aggregator_ldp_noise(self):
        from deadman.infrastructure.defense.trace_anonymizer import BehaviorAggregator

        agg = BehaviorAggregator(epsilon=2.0)  # 较高 epsilon = 较少噪声
        # 多次添加同模式
        for _ in range(20):
            agg.add_pattern("pattern_hash_1")
        # 加噪后 count 应在合理范围(0-20)
        patterns = agg.get_patterns(min_count=0)
        assert any(p.pattern_hash == "pattern_hash_1" for p in patterns)
        # count 在 0-20 之间(LDP 噪声)
        p1 = next(p for p in patterns if p.pattern_hash == "pattern_hash_1")
        assert 0 <= p1.occurrence_count <= 20

    def test_behavior_aggregator_filters_low_count(self):
        from deadman.infrastructure.defense.trace_anonymizer import BehaviorAggregator

        agg = BehaviorAggregator(epsilon=10.0)  # 高 epsilon 几乎无噪声
        for _ in range(10):
            agg.add_pattern("pattern_high")
        agg.add_pattern("pattern_low")
        patterns = agg.get_patterns(min_count=5)
        hashes = [p.pattern_hash for p in patterns]
        assert "pattern_high" in hashes
        assert "pattern_low" not in hashes

    def test_disabled_anonymizer_passes_through(self, monkeypatch):
        monkeypatch.setenv("DEADMAN_DEFENSE_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags

        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        from deadman.infrastructure.defense.trace_anonymizer import TraceAnonymizer

        anonymizer = TraceAnonymizer()
        record = {"user_id": "u123", "query": "如何写遗嘱"}
        sanitized = anonymizer.sanitize(record)
        # 关闭后原值透传
        assert sanitized["user_id"] == "u123"
        assert sanitized["query"] == "如何写遗嘱"


# =====================================================================
# D10: MasterKeyBackup (主密钥 SSS 备份)
# =====================================================================


class TestShamirSecretSharing:
    """Shamir Secret Sharing 数学正确性测试。"""

    def test_split_and_reconstruct_with_minimum_shares(self):
        import os

        from deadman.infrastructure.defense.master_key_backup import ShamirSecretSharing

        secret = os.urandom(32)
        shares = ShamirSecretSharing.split(secret, n=5, k=3)
        assert len(shares) == 5
        # K=3 个分片可重建
        reconstructed = ShamirSecretSharing.reconstruct(shares[:3])
        assert reconstructed == secret

    def test_reconstruct_with_different_subset(self):
        import os

        from deadman.infrastructure.defense.master_key_backup import ShamirSecretSharing

        secret = os.urandom(64)
        shares = ShamirSecretSharing.split(secret, n=5, k=3)
        # 不同子集(任意 K 个)
        subset = [shares[0], shares[2], shares[4]]
        reconstructed = ShamirSecretSharing.reconstruct(subset)
        assert reconstructed == secret

    def test_reconstruct_with_more_than_k_shares(self):
        import os

        from deadman.infrastructure.defense.master_key_backup import ShamirSecretSharing

        secret = os.urandom(16)
        shares = ShamirSecretSharing.split(secret, n=5, k=3)
        # 用 4 个分片也能重建
        reconstructed = ShamirSecretSharing.reconstruct(shares[:4])
        assert reconstructed == secret

    def test_split_validates_k_le_n(self):
        import pytest

        from deadman.infrastructure.defense.master_key_backup import ShamirSecretSharing

        with pytest.raises(ValueError):
            ShamirSecretSharing.split(b"x" * 16, n=3, k=5)

    def test_split_validates_k_ge_2(self):
        import pytest

        from deadman.infrastructure.defense.master_key_backup import ShamirSecretSharing

        with pytest.raises(ValueError):
            ShamirSecretSharing.split(b"x" * 16, n=5, k=1)

    def test_split_validates_n_le_255(self):
        import pytest

        from deadman.infrastructure.defense.master_key_backup import ShamirSecretSharing

        with pytest.raises(ValueError):
            ShamirSecretSharing.split(b"x" * 16, n=300, k=3)

    def test_empty_secret(self):
        from deadman.infrastructure.defense.master_key_backup import ShamirSecretSharing

        secret = b""
        shares = ShamirSecretSharing.split(secret, n=3, k=2)
        reconstructed = ShamirSecretSharing.reconstruct(shares[:2])
        assert reconstructed == secret


class TestMasterKeyBackup:
    """D10: 主密钥备份管理器测试。"""

    def test_create_backup(self, tmp_path):
        import os

        from deadman.infrastructure.defense.master_key_backup import (
            BackupStatus,
            MasterKeyBackup,
        )

        backup = MasterKeyBackup(store_path=tmp_path / "backup")
        master_key = os.urandom(32)
        shares = backup.create_backup(master_key, n=5, k=3)
        assert len(shares) == 5
        assert backup.get_status() == BackupStatus.BACKED_UP
        assert backup.has_backup()

    def test_reconstruct_after_backup(self, tmp_path):
        import os

        from deadman.infrastructure.defense.master_key_backup import MasterKeyBackup

        backup = MasterKeyBackup(store_path=tmp_path / "backup")
        master_key = os.urandom(32)
        shares = backup.create_backup(master_key, n=5, k=3)
        # 用前 3 个分片重建
        reconstructed = backup.reconstruct(shares[:3])
        assert reconstructed == master_key
        # 状态变为 RECOVERED
        from deadman.infrastructure.defense.master_key_backup import BackupStatus

        assert backup.get_status() == BackupStatus.RECOVERED

    def test_reconstruct_with_wrong_shares_fails_fingerprint(self, tmp_path):
        import os

        import pytest

        from deadman.infrastructure.defense.master_key_backup import MasterKeyBackup

        backup = MasterKeyBackup(store_path=tmp_path / "backup")
        real_master_key = os.urandom(32)
        backup.create_backup(real_master_key, n=5, k=3)

        # 用其他分片重建(应该成功,因为指纹匹配)
        # 但用伪造的分片重建 → 指纹不匹配
        # 这里我们用错误的 share 值
        from deadman.infrastructure.defense.master_key_backup import KeyShare

        fake_shares = [
            KeyShare(
                share_id=f"fake-{i}",
                share_index=i + 1,
                share_value=("00" * 32),  # 32 字节全 0,肯定错误
                recipient="attacker",
            )
            for i in range(3)
        ]
        with pytest.raises(ValueError, match="fingerprint"):
            backup.reconstruct(fake_shares)

    def test_drill_success(self, tmp_path):
        import os

        from deadman.infrastructure.defense.master_key_backup import (
            BackupStatus,
            MasterKeyBackup,
        )

        backup = MasterKeyBackup(store_path=tmp_path / "backup")
        master_key = os.urandom(32)
        backup.create_backup(master_key, n=5, k=3)
        # 演练(用任意 3 个分片)
        drill = backup.drill(notes="quarterly drill")
        assert drill.success
        assert drill.reconstructed
        assert drill.shares_collected == 3
        # 状态变为 DRILL_VERIFIED
        assert backup.get_status() == BackupStatus.DRILL_VERIFIED

    def test_drill_no_shares(self, tmp_path):
        from deadman.infrastructure.defense.master_key_backup import MasterKeyBackup

        backup = MasterKeyBackup(store_path=tmp_path / "backup")
        # 未创建备份 → 演练失败
        drill = backup.drill()
        assert not drill.success

    def test_rotate_invalidates_old_shares(self, tmp_path):
        import os

        import pytest

        from deadman.infrastructure.defense.master_key_backup import (
            MasterKeyBackup,
        )

        backup = MasterKeyBackup(store_path=tmp_path / "backup")
        old_master_key = os.urandom(32)
        old_shares = backup.create_backup(old_master_key, n=5, k=3)
        # 轮换
        new_master_key = os.urandom(32)
        new_shares = backup.rotate(new_master_key, n=5, k=3)
        # 旧分片指纹不再匹配
        with pytest.raises(ValueError, match="fingerprint"):
            backup.reconstruct(old_shares[:3])
        # 新分片可重建新主密钥
        reconstructed = backup.reconstruct(new_shares[:3])
        assert reconstructed == new_master_key

    def test_list_shares_no_value_leak(self, tmp_path):
        import os

        from deadman.infrastructure.defense.master_key_backup import MasterKeyBackup

        backup = MasterKeyBackup(store_path=tmp_path / "backup")
        master_key = os.urandom(32)
        backup.create_backup(
            master_key, n=5, k=3, recipients=["alice", "bob", "carol", "dave", "eve"]
        )
        shares_info = backup.list_shares()
        assert len(shares_info) == 5
        for s in shares_info:
            # share_value 不应出现在 list_shares 输出(防泄漏)
            assert "share_value" not in s
            assert "recipient" in s

    def test_drill_records_audit(self, tmp_path):
        import os

        from deadman.infrastructure.defense.master_key_backup import MasterKeyBackup

        backup = MasterKeyBackup(store_path=tmp_path / "backup")
        master_key = os.urandom(32)
        backup.create_backup(master_key, n=5, k=3)
        # 多次演练
        backup.drill(notes="Q1 drill")
        backup.drill(notes="Q2 drill")
        drills = backup.list_drills()
        assert len(drills) == 2
        assert all("drill" in d["notes"] for d in drills)

    def test_persistence_across_instances(self, tmp_path):
        import os

        from deadman.infrastructure.defense.master_key_backup import (
            BackupStatus,
            MasterKeyBackup,
        )

        backup_path = tmp_path / "backup"
        # 第一个实例创建备份
        backup1 = MasterKeyBackup(store_path=backup_path)
        master_key = os.urandom(32)
        shares = backup1.create_backup(master_key, n=5, k=3)
        # 第二个实例加载(应能查到状态)
        backup2 = MasterKeyBackup(store_path=backup_path)
        assert backup2.has_backup()
        assert backup2.get_status() == BackupStatus.BACKED_UP
        # 第二个实例可重建
        reconstructed = backup2.reconstruct(shares[:3])
        assert reconstructed == master_key

    def test_disabled_skips_backup(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_DEFENSE_ENABLED", "0")
        from deadman.infrastructure.feature_flags import get_flags

        get_flags()._cache.clear()
        get_flags()._cache_loaded_at = 0.0
        import os

        from deadman.infrastructure.defense.master_key_backup import MasterKeyBackup

        backup = MasterKeyBackup(store_path=tmp_path / "backup")
        master_key = os.urandom(32)
        # 关闭后 create_backup 返回空列表
        shares = backup.create_backup(master_key, n=5, k=3)
        assert shares == []
        # 演练也跳过
        drill = backup.drill()
        assert "disabled" in drill.notes
