"""P8.4 Agent Marketplace 测试 - registry / reviewer / rating / revenue / sandbox。

覆盖:
    - Registry: submit/approve/reject/suspend/list/search/update_version
    - Reviewer: 每项 check 单独 + 综合 review + auto-approve/reject 阈值
    - Rating: rate/get/average/helpful_vote/flag/dedup
    - Revenue: record_usage/calculate_revenue/payout
    - Sandbox: 资源限制 + 工具白名单 + PII 双向脱敏
    - Disabled 状态抛 MarketplaceError
    - 多租户隔离

feature flag: DEADMAN_MARKETPLACE_ENABLED=1(测试启用)
              DEADMAN_DEFENSE_ENABLED=1(PII 脱敏生效)
              DEADMAN_MULTI_TENANT_ENABLED=1(tenant 隔离测试)
"""

from __future__ import annotations

import datetime as dt
import time

import pytest

from deadman.marketplace import MarketplaceError

# =====================================================================
# 公共 fixture
# =====================================================================


def _current_period() -> str:
    """返回当前 UTC 月份的 "YYYY-MM" 周期标识。

    payout 按周期窗口过滤 usage：RevenueShare._parse_period() 把 "YYYY-MM"
    解析为该月的 UTC 起止区间，而 record_usage() 以 time.time()(UTC epoch)
    落账。测试若硬编码固定月份，则只在那个月内通过，跨月后 usage 落在窗口
    之外，分账恒为 0.0。此处与生产实现保持同一时间口径(UTC)。
    """
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")


def _reset_marketplace_singletons() -> None:
    """清空 marketplace 模块所有单例(避免测试间状态污染)。"""
    import deadman.marketplace as mp

    mp._registry_instance = None
    mp._reviewer_instance = None  # type: ignore[attr-defined]
    mp._rating_instance = None  # type: ignore[attr-defined]
    mp._revenue_instance = None  # type: ignore[attr-defined]
    mp._sandbox_instance = None  # type: ignore[attr-defined]
    # 各子模块单例
    from deadman.marketplace import (
        rating as rat_mod,
    )
    from deadman.marketplace import (
        registry as reg_mod,
    )
    from deadman.marketplace import (
        revenue as ren_mod,
    )
    from deadman.marketplace import (
        reviewer as rev_mod,
    )
    from deadman.marketplace import (
        sandbox as sbx_mod,
    )

    reg_mod._registry_instance = None
    rev_mod._reviewer_instance = None
    rat_mod._rating_instance = None
    ren_mod._revenue_instance = None
    sbx_mod._sandbox_instance = None


def _reset_flags_cache() -> None:
    """清空 feature_flags 缓存,强制重新读 env var。"""
    from deadman.infrastructure.feature_flags import get_flags

    get_flags()._cache.clear()
    get_flags()._cache_loaded_at = 0.0


@pytest.fixture(autouse=True)
def enable_marketplace(monkeypatch, tmp_path):
    """每个测试都启用 marketplace + defense,并重置所有单例。

    把所有持久化路径指向 tmp_path 避免污染。
    """
    monkeypatch.setenv("DEADMAN_MARKETPLACE_ENABLED", "1")
    monkeypatch.setenv("DEADMAN_DEFENSE_ENABLED", "1")
    monkeypatch.setenv("DEADMAN_FEATURE_FLAG_SYSTEM_ENABLED", "1")
    # multi_tenant 默认关闭(只有 tenant 隔离测试启用)
    monkeypatch.setenv("DEADMAN_MULTI_TENANT_ENABLED", "0")
    _reset_flags_cache()
    _reset_marketplace_singletons()
    yield
    # 测试后清理(防污染其他测试)
    _reset_flags_cache()
    _reset_marketplace_singletons()


@pytest.fixture
def registry(tmp_path):
    """构造一个用 tmp_path 持久化的 MarketplaceRegistry。"""
    from deadman.marketplace.registry import MarketplaceRegistry

    return MarketplaceRegistry(store_path=tmp_path / "registry.json")


@pytest.fixture
def reviewer():
    from deadman.marketplace.reviewer import AgentReviewer

    return AgentReviewer()


@pytest.fixture
def rating_system(tmp_path):
    from deadman.marketplace.rating import RatingSystem

    return RatingSystem(store_path=tmp_path / "ratings.json")


@pytest.fixture
def revenue(tmp_path, registry):
    from deadman.marketplace.revenue import RevenueShare

    return RevenueShare(store_path=tmp_path / "revenue.json", registry=registry)


@pytest.fixture
def sandbox():
    from deadman.marketplace.sandbox import MarketplaceSandbox

    return MarketplaceSandbox()


# =====================================================================
# 公共辅助
# =====================================================================


def _good_card(agent_id: str = "agent_x") -> dict:
    """构造一个通过审核的 agent_card(A2A 兼容 + 无危险模式)。"""
    return {
        "name": "Test Agent",
        "description": "A test agent for marketplace integration tests.",
        "version": "1.0.0",
        "url": f"http://example.com/{agent_id}",
        "skills": [
            {
                "id": "skill_1",
                "name": "Greeting",
                "description": "Says hello to the user",
                "tags": ["greeting", "demo"],
            }
        ],
        "capabilities": {"streaming": False},
        "tools": ["search"],
        "examples": [{"input": "hi", "response": "hello"}],
        "tests": [{"name": "greets", "passes": True}],
    }


def _make_listing(
    agent_id: str = "agent_x",
    name: str = "Test Agent",
    author: str = "author_a",
    version: str = "1.0.0",
    description: str = "A test agent for marketplace integration tests with sufficient length.",
    category: str = "productivity",
    tags: list[str] | None = None,
    price_per_call: float = 0.0,
    agent_card: dict | None = None,
):
    from deadman.marketplace.registry import AgentListing

    return AgentListing(
        agent_id=agent_id,
        name=name,
        author=author,
        version=version,
        description=description,
        category=category,
        tags=tags if tags is not None else ["demo", "test"],
        price_per_call=price_per_call,
        agent_card=agent_card if agent_card is not None else _good_card(agent_id),
    )


# =====================================================================
# Registry 测试
# =====================================================================


class TestRegistry:
    def test_submit_returns_listing_id(self, registry):
        listing = _make_listing(agent_id="a1")
        listing_id = registry.submit(listing)
        assert listing_id == "a1"

    def test_submit_initial_status_pending(self, registry):
        registry.submit(_make_listing(agent_id="a1"))
        got = registry.get("a1")
        assert got is not None
        assert got.status == "pending"

    def test_submit_duplicate_raises(self, registry):
        registry.submit(_make_listing(agent_id="a1"))
        with pytest.raises(MarketplaceError):
            registry.submit(_make_listing(agent_id="a1"))

    def test_approve_changes_status(self, registry):
        registry.submit(_make_listing(agent_id="a1"))
        assert registry.approve("a1") is True
        assert registry.get("a1").status == "approved"

    def test_approve_non_pending_raises(self, registry):
        registry.submit(_make_listing(agent_id="a1"))
        registry.approve("a1")
        with pytest.raises(MarketplaceError):
            registry.approve("a1")  # 已 approved,不能重复 approve

    def test_reject_records_reason(self, registry):
        registry.submit(_make_listing(agent_id="a1"))
        registry.reject("a1", reason="bad quality")
        got = registry.get("a1")
        assert got.status == "rejected"
        assert got.review_reason == "bad quality"

    def test_suspend_changes_approved_to_suspended(self, registry):
        registry.submit(_make_listing(agent_id="a1"))
        registry.approve("a1")
        registry.suspend("a1", reason="policy violation")
        got = registry.get("a1")
        assert got.status == "suspended"
        assert got.review_reason == "policy violation"

    def test_suspend_non_approved_raises(self, registry):
        registry.submit(_make_listing(agent_id="a1"))
        with pytest.raises(MarketplaceError):
            registry.suspend("a1")  # pending 状态不能 suspend

    def test_list_only_returns_approved(self, registry):
        registry.submit(_make_listing(agent_id="a1", name="A"))
        registry.submit(_make_listing(agent_id="a2", name="B"))
        registry.submit(_make_listing(agent_id="a3", name="C"))
        registry.approve("a2")
        results = registry.list()
        assert len(results) == 1
        assert results[0].agent_id == "a2"

    def test_list_filter_by_category(self, registry):
        registry.submit(_make_listing(agent_id="a1", category="legal"))
        registry.submit(_make_listing(agent_id="a2", category="finance"))
        registry.approve("a1")
        registry.approve("a2")
        results = registry.list(category="legal")
        assert len(results) == 1
        assert results[0].category == "legal"

    def test_list_sort_by_price(self, registry):
        registry.submit(_make_listing(agent_id="a1", price_per_call=0.5))
        registry.submit(_make_listing(agent_id="a2", price_per_call=0.1))
        registry.submit(_make_listing(agent_id="a3", price_per_call=0.9))
        registry.approve("a1")
        registry.approve("a2")
        registry.approve("a3")
        asc = registry.list(sort_by="price_asc")
        assert [listing.agent_id for listing in asc] == ["a2", "a1", "a3"]

    def test_search_matches_name_description_tags(self, registry):
        registry.submit(
            _make_listing(
                agent_id="a1",
                name="Legal Helper",
                description="helps with legal documents",
                tags=["legal", "law"],
            )
        )
        registry.approve("a1")
        # name match
        assert len(registry.search("legal helper")) == 1
        # description match
        assert len(registry.search("documents")) == 1
        # tag match
        assert len(registry.search("law")) == 1
        # no match
        assert len(registry.search("nonexistent")) == 0

    def test_search_excludes_non_approved(self, registry):
        registry.submit(_make_listing(agent_id="a1", name="Findable"))
        # 未 approve,search 不应返回
        assert len(registry.search("findable")) == 0

    def test_update_version_on_approved(self, registry):
        registry.submit(_make_listing(agent_id="a1", version="1.0.0"))
        registry.approve("a1")
        new_card = _good_card()
        new_card["version"] = "2.0.0"
        assert registry.update_version("a1", "2.0.0", new_card) is True
        got = registry.get("a1")
        assert got.version == "2.0.0"
        assert got.agent_card["version"] == "2.0.0"

    def test_update_version_non_approved_raises(self, registry):
        registry.submit(_make_listing(agent_id="a1"))
        with pytest.raises(MarketplaceError):
            registry.update_version("a1", "2.0.0")

    def test_persistence_round_trip(self, registry, tmp_path):
        """submit 后重新构造实例(同一 store)能加载到数据。"""
        from deadman.marketplace.registry import MarketplaceRegistry

        registry.submit(_make_listing(agent_id="a1"))
        registry.approve("a1")
        # 新实例,同一 store_path
        new_reg = MarketplaceRegistry(store_path=tmp_path / "registry.json")
        got = new_reg.get("a1")
        assert got is not None
        assert got.status == "approved"

    def test_get_nonexistent_returns_none(self, registry):
        assert registry.get("nonexistent") is None


# =====================================================================
# Reviewer 测试
# =====================================================================


class TestReviewer:
    def test_security_scan_clean_card(self, reviewer):
        listing = _make_listing()
        issues, score = reviewer.security_scan(listing)
        # 干净的 card 不应有 critical issue
        assert not any(i.severity == "critical" for i in issues)
        assert score > 0

    def test_security_scan_detects_eval(self, reviewer):
        listing = _make_listing()
        listing.agent_card["skills"][0]["description"] = "uses eval( to compute"
        issues, score = reviewer.security_scan(listing)
        assert any(i.severity == "critical" for i in issues)
        assert score == 0

    def test_security_scan_detects_path_traversal(self, reviewer):
        listing = _make_listing()
        listing.agent_card["description"] = "reads ../../etc/passwd"
        issues, _score = reviewer.security_scan(listing)
        assert any("Path traversal" in i.message for i in issues)

    def test_security_scan_detects_shadow_tool(self, reviewer):
        listing = _make_listing()
        listing.agent_card["skills"][0]["id"] = "exec"
        issues, score = reviewer.security_scan(listing)
        assert any("Shadow tool" in i.message for i in issues)
        assert score == 0

    def test_schema_validation_valid_card(self, reviewer):
        listing = _make_listing()
        issues, score = reviewer.schema_validation(listing)
        assert not any(i.severity == "critical" for i in issues)
        assert score == 20

    def test_schema_validation_missing_required_field(self, reviewer):
        listing = _make_listing()
        del listing.agent_card["name"]
        issues, score = reviewer.schema_validation(listing)
        assert any("Missing required field: name" in i.message for i in issues)
        assert score == 0

    def test_schema_validation_empty_skills(self, reviewer):
        listing = _make_listing()
        listing.agent_card["skills"] = []
        issues, _score = reviewer.schema_validation(listing)
        assert any("skills" in i.message for i in issues)

    def test_pii_leak_clean_text(self, reviewer):
        listing = _make_listing(description="A perfectly safe description with no PII.")
        issues, _score = reviewer.pii_leak_check(listing)
        # 不应有 PII issue
        assert not any(i.check == "pii_leak_check" for i in issues)

    def test_pii_leak_detects_id_card(self, reviewer):
        listing = _make_listing(description="测试身份证 110101199003073847 应该被检测到")
        issues, score = reviewer.pii_leak_check(listing)
        # 应检测到 china_id_card(critical)
        critical = [i for i in issues if i.severity == "critical"]
        assert len(critical) >= 1
        assert score == 0

    def test_pii_leak_detects_email(self, reviewer):
        listing = _make_listing(description="Contact me at user@example.com for details.")
        issues, _score = reviewer.pii_leak_check(listing)
        # email 是 warning(非 critical)
        pii_issues = [i for i in issues if i.check == "pii_leak_check"]
        assert len(pii_issues) >= 1

    def test_safety_check_clean_card(self, reviewer):
        listing = _make_listing()
        issues, _score = reviewer.safety_check(listing)
        assert not any(i.severity == "critical" for i in issues)

    def test_safety_check_detects_exec(self, reviewer):
        listing = _make_listing()
        listing.agent_card["examples"] = [{"input": "x", "response": "exec('code')"}]
        issues, score = reviewer.safety_check(listing)
        critical = [i for i in issues if i.severity == "critical"]
        assert len(critical) >= 1
        assert score == 0

    def test_quality_score_full_score(self, reviewer):
        listing = _make_listing()
        _issues, score = reviewer.quality_score(listing)
        # 齐全(description/skills/tags/examples/tests) → 满分 20
        assert score == 20

    def test_quality_score_missing_examples(self, reviewer):
        listing = _make_listing()
        listing.agent_card.pop("examples")
        issues, score = reviewer.quality_score(listing)
        assert score < 20
        assert any("examples" in i.message for i in issues)

    def test_review_auto_approve_clean_listing(self, reviewer):
        """齐全 + 干净 → auto_decision='approve'。"""
        listing = _make_listing()
        listing_id = listing.agent_id
        # 直接 review(不经 registry,无需 submit)
        result = reviewer.review(listing)
        assert result.listing_id == listing_id
        assert result.auto_decision == "approve"
        assert result.passed is True
        assert result.score >= 80
        assert not result.has_critical

    def test_review_auto_reject_on_critical(self, reviewer):
        """含 critical issue → auto_decision='reject'。"""
        listing = _make_listing()
        # 注入 eval( 触发 security critical
        listing.agent_card["skills"][0]["description"] = "uses eval( internally"
        result = reviewer.review(listing)
        assert result.auto_decision == "reject"
        assert result.passed is False
        assert result.has_critical

    def test_review_manual_when_warning_only(self, reviewer):
        """仅 warning(无 critical)+ score < 80 → manual。"""
        listing = _make_listing()
        # 去掉 examples + tests → quality warning, score 降低
        listing.agent_card.pop("examples")
        listing.agent_card.pop("tests")
        result = reviewer.review(listing)
        # 应进入 manual(有 warning 但无 critical)
        assert result.auto_decision == "manual"
        assert result.passed is False
        assert not result.has_critical


# =====================================================================
# Rating 测试
# =====================================================================


class TestRating:
    def test_rate_returns_rating(self, rating_system):
        r = rating_system.rate("a1", "u1", 5, "great")
        assert r.agent_id == "a1"
        assert r.user_id == "u1"
        assert r.score == 5
        assert r.review_text == "great"

    def test_rate_invalid_score_raises(self, rating_system):
        with pytest.raises(MarketplaceError):
            rating_system.rate("a1", "u1", 0)
        with pytest.raises(MarketplaceError):
            rating_system.rate("a1", "u1", 6)

    def test_rate_dedup_overwrites(self, rating_system):
        rating_system.rate("a1", "u1", 3, "first")
        rating_system.rate("a1", "u1", 5, "second")
        ratings = rating_system.get_ratings("a1")
        assert len(ratings) == 1
        assert ratings[0].score == 5
        assert ratings[0].review_text == "second"

    def test_get_ratings_multiple_users(self, rating_system):
        rating_system.rate("a1", "u1", 4)
        rating_system.rate("a1", "u2", 5)
        rating_system.rate("a1", "u3", 2)
        ratings = rating_system.get_ratings("a1")
        assert len(ratings) == 3

    def test_average_score(self, rating_system):
        rating_system.rate("a1", "u1", 4)
        rating_system.rate("a1", "u2", 5)
        rating_system.rate("a1", "u3", 3)
        avg = rating_system.average_score("a1")
        assert avg == pytest.approx(4.0)

    def test_average_score_no_ratings(self, rating_system):
        assert rating_system.average_score("nonexistent") == 0.0

    def test_helpful_vote_increments(self, rating_system):
        rating_system.rate("a1", "u1", 5)
        rating_id = "a1:u1"
        assert rating_system.helpful_vote(rating_id, "u2") is True
        r = rating_system.get_ratings("a1")[0]
        assert r.helpful_votes == 1
        assert "u2" in r.voted_by

    def test_helpful_vote_dedup(self, rating_system):
        rating_system.rate("a1", "u1", 5)
        rating_id = "a1:u1"
        assert rating_system.helpful_vote(rating_id, "u2") is True
        # 同一 voter 再投 → False
        assert rating_system.helpful_vote(rating_id, "u2") is False
        r = rating_system.get_ratings("a1")[0]
        assert r.helpful_votes == 1

    def test_helpful_vote_nonexistent_rating(self, rating_system):
        assert rating_system.helpful_vote("nonexistent", "u2") is False

    def test_flag_records(self, rating_system):
        assert rating_system.flag("a1", "u1", "inappropriate") is True
        flags = rating_system.get_flags("a1")
        assert len(flags) == 1
        assert flags[0].reason == "inappropriate"

    def test_rating_persistence_round_trip(self, rating_system, tmp_path):
        from deadman.marketplace.rating import RatingSystem

        rating_system.rate("a1", "u1", 5, "great")
        rating_system.helpful_vote("a1:u1", "u2")
        # 新实例
        new_rs = RatingSystem(store_path=tmp_path / "ratings.json")
        ratings = new_rs.get_ratings("a1")
        assert len(ratings) == 1
        assert ratings[0].score == 5
        assert ratings[0].helpful_votes == 1


# =====================================================================
# Revenue 测试
# =====================================================================


class TestRevenue:
    def test_record_usage_returns_record(self, revenue):
        # 先 submit + approve 让 registry 能查到 price
        listing = _make_listing(agent_id="a1", price_per_call=0.5)
        revenue._registry.submit(listing)
        revenue._registry.approve("a1")
        rec = revenue.record_usage("a1", "u1", call_count=10, tokens=500)
        assert rec.agent_id == "a1"
        assert rec.call_count == 10
        assert rec.tokens == 500
        assert rec.cost == pytest.approx(5.0)  # 0.5 × 10

    def test_record_usage_accumulates_same_user(self, revenue):
        listing = _make_listing(agent_id="a1", price_per_call=0.1)
        revenue._registry.submit(listing)
        revenue._registry.approve("a1")
        revenue.record_usage("a1", "u1", call_count=5, tokens=100)
        revenue.record_usage("a1", "u1", call_count=3, tokens=50)
        records = revenue.get_usage("a1")
        assert len(records) == 1
        assert records[0].call_count == 8
        assert records[0].tokens == 150
        assert records[0].cost == pytest.approx(0.8)  # 0.1 × 8

    def test_record_usage_separate_users(self, revenue):
        listing = _make_listing(agent_id="a1", price_per_call=0.1)
        revenue._registry.submit(listing)
        revenue._registry.approve("a1")
        revenue.record_usage("a1", "u1", call_count=5, tokens=100)
        revenue.record_usage("a1", "u2", call_count=3, tokens=50)
        records = revenue.get_usage("a1")
        assert len(records) == 2

    def test_calculate_revenue_default_split(self, revenue):
        listing = _make_listing(agent_id="a1", price_per_call=1.0, author="author_a")
        revenue._registry.submit(listing)
        revenue._registry.approve("a1")
        revenue.record_usage("a1", "u1", call_count=100, tokens=1000)
        now = time.time()
        split = revenue.calculate_revenue(
            "a1",
            period_start=0,
            period_end=now + 1,
        )
        # total = 1.0 × 100 = 100
        assert split.total_revenue == pytest.approx(100.0)
        # default platform 30%, author 70%
        assert split.platform_share == pytest.approx(30.0)
        assert split.author_share == pytest.approx(70.0)
        assert split.author_id == "author_a"
        assert split.call_count == 100

    def test_calculate_revenue_pro_plan(self, revenue):
        listing = _make_listing(agent_id="a1", price_per_call=1.0, author="author_a")
        revenue._registry.submit(listing)
        revenue._registry.approve("a1")
        revenue.record_usage("a1", "u1", call_count=100, tokens=1000)
        now = time.time()
        split = revenue.calculate_revenue(
            "a1",
            period_start=0,
            period_end=now + 1,
            plan="pro",
        )
        # pro: platform 20%, author 80%
        assert split.platform_share == pytest.approx(20.0)
        assert split.author_share == pytest.approx(80.0)

    def test_calculate_revenue_no_usage(self, revenue):
        listing = _make_listing(agent_id="a1", price_per_call=1.0)
        revenue._registry.submit(listing)
        revenue._registry.approve("a1")
        split = revenue.calculate_revenue("a1", 0, time.time() + 1)
        assert split.total_revenue == 0.0
        assert split.call_count == 0

    def test_payout_aggregates_author_share(self, revenue):
        # author_a 名下 2 个 agent
        for aid in ("a1", "a2"):
            listing = _make_listing(agent_id=aid, price_per_call=1.0, author="author_a")
            revenue._registry.submit(listing)
            revenue._registry.approve(aid)
            revenue.record_usage(aid, "u1", call_count=100, tokens=1000)
        period = _current_period()
        payout = revenue.payout("author_a", period=period, plan="free")
        # 每个 agent author_share = 70,total = 140
        assert payout.author_id == "author_a"
        assert payout.amount == pytest.approx(140.0)
        assert payout.period == period
        assert set(payout.agent_ids) == {"a1", "a2"}

    def test_payout_idempotent_overwrites(self, revenue):
        listing = _make_listing(agent_id="a1", price_per_call=1.0, author="author_a")
        revenue._registry.submit(listing)
        revenue._registry.approve("a1")
        revenue.record_usage("a1", "u1", call_count=100, tokens=1000)
        period = _current_period()
        p1 = revenue.payout("author_a", period=period)
        p2 = revenue.payout("author_a", period=period)
        # 同一 period 覆盖
        assert p1.payout_id == p2.payout_id
        payouts = revenue.get_payouts("author_a")
        assert len(payouts) == 1


# =====================================================================
# Sandbox 测试
# =====================================================================


class TestSandbox:
    def test_execute_no_handler(self, sandbox):
        from deadman.marketplace.sandbox import SandboxConfig

        result = sandbox.execute("no_such_agent", {"x": 1}, SandboxConfig())
        assert result.success is False
        assert "No handler" in result.error

    def test_execute_success(self, sandbox):
        from deadman.marketplace.sandbox import SandboxConfig

        def handler(input_data, env):
            return {"echo": input_data}

        sandbox.register_handler("a1", handler)
        result = sandbox.execute("a1", {"msg": "hello"}, SandboxConfig())
        assert result.success is True
        assert result.output == {"echo": {"msg": "hello"}}

    def test_tool_whitelist_allows(self, sandbox):
        from deadman.marketplace.sandbox import SandboxConfig

        def handler(input_data, env):
            r = env.call_tool("search", query="x")
            return r

        config = SandboxConfig(allowed_tools=["search"], max_tool_calls=5)
        sandbox.register_handler("a1", handler)
        result = sandbox.execute("a1", {}, config)
        assert result.success is True
        assert result.resource_usage.tool_calls == 1

    def test_tool_whitelist_blocks(self, sandbox):
        from deadman.marketplace.sandbox import SandboxConfig

        def handler(input_data, env):
            return env.call_tool("exec", code="x")  # 不在白名单

        config = SandboxConfig(allowed_tools=["search"], max_tool_calls=5)
        sandbox.register_handler("a1", handler)
        result = sandbox.execute("a1", {}, config)
        assert result.success is False
        assert "blocked" in result.error.lower() or "not in whitelist" in result.error

    def test_tool_call_limit(self, sandbox):
        from deadman.marketplace.sandbox import SandboxConfig

        def handler(input_data, env):
            for _ in range(5):
                env.call_tool("search")
            return "done"

        config = SandboxConfig(allowed_tools=["search"], max_tool_calls=2)
        sandbox.register_handler("a1", handler)
        result = sandbox.execute("a1", {}, config)
        assert result.success is False
        assert "limit reached" in result.error.lower() or "blocked" in result.error.lower()

    def test_network_whitelist(self, sandbox):
        from deadman.marketplace.sandbox import SandboxConfig

        def handler(input_data, env):
            return env.http_get("https://api.example.com/x")

        config = SandboxConfig(
            allowed_urls=["https://api.example.com/"],
            max_network_calls=5,
        )
        sandbox.register_handler("a1", handler)
        result = sandbox.execute("a1", {}, config)
        assert result.success is True
        assert result.resource_usage.network_calls == 1

    def test_network_whitelist_blocks(self, sandbox):
        from deadman.marketplace.sandbox import SandboxConfig

        def handler(input_data, env):
            return env.http_get("https://evil.com/x")

        config = SandboxConfig(
            allowed_urls=["https://api.example.com/"],
            max_network_calls=5,
        )
        sandbox.register_handler("a1", handler)
        result = sandbox.execute("a1", {}, config)
        assert result.success is False
        assert "not in whitelist" in result.error

    def test_pii_redact_on_input(self, sandbox):
        from deadman.marketplace.sandbox import SandboxConfig

        received: dict = {}

        def handler(input_data, env):
            received["input"] = input_data
            return "ok"

        sandbox.register_handler("a1", handler)
        # input 含中国手机号(PII)
        result = sandbox.execute(
            "a1",
            {"phone": "13912345678"},
            SandboxConfig(),
        )
        assert result.success is True
        assert result.pii_redacted_input is True
        # handler 收到的是已脱敏的 input
        assert "13912345678" not in str(received["input"])

    def test_pii_redact_on_output(self, sandbox):
        from deadman.marketplace.sandbox import SandboxConfig

        def handler(input_data, env):
            # output 含 email(PII)
            return {"contact": "user@example.com"}

        sandbox.register_handler("a1", handler)
        result = sandbox.execute("a1", {}, SandboxConfig())
        assert result.success is True
        assert result.pii_redacted_output is True
        # output 已脱敏
        assert "user@example.com" not in str(result.output)

    def test_handler_exception_caught(self, sandbox):
        from deadman.marketplace.sandbox import SandboxConfig

        def handler(input_data, env):
            raise ValueError("boom")

        sandbox.register_handler("a1", handler)
        result = sandbox.execute("a1", {}, SandboxConfig())
        assert result.success is False
        assert "Handler error" in result.error
        assert "boom" in result.error

    def test_input_too_large(self, sandbox):
        from deadman.marketplace.sandbox import SandboxConfig

        def handler(input_data, env):
            return "ok"

        sandbox.register_handler("a1", handler)
        # max_input_chars=10,但 input 长 100
        config = SandboxConfig(max_input_chars=10)
        big_input = "x" * 100
        result = sandbox.execute("a1", big_input, config)
        assert result.success is False
        assert "Input too large" in result.error

    def test_resource_usage_recorded(self, sandbox):
        from deadman.marketplace.sandbox import SandboxConfig

        def handler(input_data, env):
            # 简单计算消耗 CPU
            total = sum(range(1000))
            return total

        sandbox.register_handler("a1", handler)
        result = sandbox.execute("a1", {}, SandboxConfig())
        assert result.success is True
        assert result.resource_usage.cpu_time >= 0
        assert result.resource_usage.memory_peak >= 0


# =====================================================================
# Disabled state 测试
# =====================================================================


class TestDisabledState:
    """marketplace flag 关闭时所有 API 抛 MarketplaceError。"""

    def test_registry_submit_raises_when_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_MARKETPLACE_ENABLED", "0")
        _reset_flags_cache()
        from deadman.marketplace.registry import MarketplaceError, MarketplaceRegistry

        reg = MarketplaceRegistry(store_path=tmp_path / "r.json")
        with pytest.raises(MarketplaceError):
            reg.submit(_make_listing())

    def test_registry_list_raises_when_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_MARKETPLACE_ENABLED", "0")
        _reset_flags_cache()
        from deadman.marketplace.registry import MarketplaceError, MarketplaceRegistry

        reg = MarketplaceRegistry(store_path=tmp_path / "r.json")
        with pytest.raises(MarketplaceError):
            reg.list()

    def test_reviewer_raises_when_disabled(self, monkeypatch):
        monkeypatch.setenv("DEADMAN_MARKETPLACE_ENABLED", "0")
        _reset_flags_cache()
        from deadman.marketplace.reviewer import AgentReviewer, MarketplaceError

        reviewer = AgentReviewer()
        with pytest.raises(MarketplaceError):
            reviewer.review(_make_listing())

    def test_rating_raises_when_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_MARKETPLACE_ENABLED", "0")
        _reset_flags_cache()
        from deadman.marketplace.rating import MarketplaceError, RatingSystem

        rs = RatingSystem(store_path=tmp_path / "r.json")
        with pytest.raises(MarketplaceError):
            rs.rate("a1", "u1", 5)

    def test_revenue_raises_when_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEADMAN_MARKETPLACE_ENABLED", "0")
        _reset_flags_cache()
        from deadman.marketplace.revenue import MarketplaceError, RevenueShare

        rev = RevenueShare(store_path=tmp_path / "r.json")
        with pytest.raises(MarketplaceError):
            rev.record_usage("a1", "u1", 1, 100)

    def test_sandbox_raises_when_disabled(self, monkeypatch):
        monkeypatch.setenv("DEADMAN_MARKETPLACE_ENABLED", "0")
        _reset_flags_cache()
        from deadman.marketplace.sandbox import MarketplaceError, MarketplaceSandbox

        sbx = MarketplaceSandbox()
        with pytest.raises(MarketplaceError):
            sbx.execute("a1", {}, None)


# =====================================================================
# 多租户隔离测试
# =====================================================================


class TestTenantIsolation:
    """multi_tenant 启用时,不同租户的 registry 数据相互隔离。"""

    @pytest.fixture
    def enable_multi_tenant(self, monkeypatch):
        monkeypatch.setenv("DEADMAN_MULTI_TENANT_ENABLED", "1")
        _reset_flags_cache()
        yield
        monkeypatch.setenv("DEADMAN_MULTI_TENANT_ENABLED", "0")
        _reset_flags_cache()

    def test_tenant_isolation_registry(self, enable_multi_tenant, tmp_path):
        # 把 TENANTS_ROOT 指向 tmp_path 避免污染真实文件系统
        import deadman.infrastructure.multi_tenant as mt
        from deadman.infrastructure.multi_tenant import (
            TenantContext,
            TenantInfo,
        )
        from deadman.marketplace.registry import MarketplaceRegistry

        original_root = mt.TENANTS_ROOT
        mt.TENANTS_ROOT = tmp_path / "tenants"

        try:
            tenant_a = TenantInfo(tenant_id="tenant_a", name="A")
            tenant_b = TenantInfo(tenant_id="tenant_b", name="B")

            reg = MarketplaceRegistry()  # 不显式传 store_path,走 tenant 路径解析

            # tenant_a 提交 a1
            with TenantContext(tenant_a):
                reg.submit(_make_listing(agent_id="a1", name="AgentA1"))
                assert reg.get("a1") is not None

            # tenant_b 提交 b1
            with TenantContext(tenant_b):
                reg.submit(_make_listing(agent_id="b1", name="AgentB1"))
                assert reg.get("b1") is not None
                # tenant_b 看不到 tenant_a 的 a1
                assert reg.get("a1") is None

            # 切回 tenant_a,看不到 tenant_b 的 b1
            with TenantContext(tenant_a):
                assert reg.get("b1") is None
                assert reg.get("a1") is not None
        finally:
            mt.TENANTS_ROOT = original_root

    def test_tenant_isolation_rating(self, enable_multi_tenant, tmp_path):
        import deadman.infrastructure.multi_tenant as mt
        from deadman.infrastructure.multi_tenant import (
            TenantContext,
            TenantInfo,
        )
        from deadman.marketplace.rating import RatingSystem

        original_root = mt.TENANTS_ROOT
        mt.TENANTS_ROOT = tmp_path / "tenants"

        try:
            tenant_a = TenantInfo(tenant_id="tenant_a")
            tenant_b = TenantInfo(tenant_id="tenant_b")

            rs = RatingSystem()

            with TenantContext(tenant_a):
                rs.rate("a1", "u1", 5, "great")
                assert len(rs.get_ratings("a1")) == 1

            with TenantContext(tenant_b):
                # tenant_b 看不到 tenant_a 的评分
                assert len(rs.get_ratings("a1")) == 0
                rs.rate("a1", "u1", 3, "ok")
                assert len(rs.get_ratings("a1")) == 1

            with TenantContext(tenant_a):
                # tenant_a 的评分仍是 5(隔离)
                ratings = rs.get_ratings("a1")
                assert len(ratings) == 1
                assert ratings[0].score == 5
        finally:
            mt.TENANTS_ROOT = original_root


# =====================================================================
# get_marketplace 单例入口测试
# =====================================================================


class TestSingleton:
    def test_get_marketplace_returns_registry(self):
        from deadman.marketplace import MarketplaceRegistry, get_marketplace

        mp = get_marketplace()
        assert isinstance(mp, MarketplaceRegistry)

    def test_get_marketplace_singleton(self):
        from deadman.marketplace import get_marketplace

        a = get_marketplace()
        b = get_marketplace()
        assert a is b
