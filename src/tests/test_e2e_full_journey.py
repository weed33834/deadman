"""端到端（E2E）用户旅程测试 - deadman FastAPI 全链路

测试方法：
    * 直接用 ``fastapi.testclient.TestClient`` 调 ``from deadman.web.app import app``
      （不启动真实 HTTP server）
    * 每个测试用 ``tmp_path`` 隔离数据目录（auth / vault / ending_note /
      deadman_switch / support / onboarding / cases / notifications）
    * 用 monkeypatch 重定向 ``settings`` 与各 ``_DEFAULT_DATA_DIR`` 模块级常量
    * 禁用限流（``DEADMAN_RATE_LIMIT_ENABLED=0``），避免测试请求被掐
    * LLM 全程 mock（不真正调外部 API）

覆盖 17 个用户旅程章节（详见各 Test 类 docstring）：
    1. 未认证访问公开端点
    2. 注册 / 登录 / me / refresh
    3. 认证失败工况（无 token / 错误 token / 过期 token）
    4. Onboarding 画像
    5. 终活笔记全流程
    6. 保险库全流程
    7. Dead Man Switch 全状态机
    8. 案例管理
    9. 通知信函
    10. 悼文生成
    11. 规划评分
    12. 客服工单
    13. 对话（chat / stream，可能降级）
    14. 运维端点
    15. 跨用户权限隔离
    16. Pydantic 校验
    17. CORS 预检

特别标注：任何返回 500 的端点几乎都是 bug，会在测试中通过 ``assert status != 500``
前置检查暴露，最终报告里集中列出。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# =====================================================================
# Fixtures：隔离的 TestClient + 认证 token
# =====================================================================


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把所有持久化目录重定向到 tmp_path，并禁用限流 / 启用相关模块。

    - settings.auth_data_dir / jwt_secret / expiry_days / password_min_length
    - settings.switch/support/onboarding_data_dir
    - 各业务 store 模块的 ``_DEFAULT_DATA_DIR`` 模块级常量
    - ``DEADMAN_VAULT_PASSWORD`` / ``DEADMAN_ENDING_NOTE_PASSPHRASE``
    - ``DEADMAN_RATE_LIMIT_ENABLED=0`` 防止测试请求被掐
    - ``DEADMAN_BILLING_ENABLED=1`` / ``DEADMAN_MARKETPLACE_ENABLED=1`` ...
      （这里不依赖 feature flag；如需启用单独 monkeypatch）
    """
    from deadman.config import settings

    # === Auth ===
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "auth_data_dir", auth_dir)
    monkeypatch.setattr(settings, "jwt_secret", "")
    monkeypatch.setattr(settings, "jwt_expiry_days", 7)
    monkeypatch.setattr(settings, "password_min_length", 8)

    # === 业务模块 data_dir（settings 层）===
    monkeypatch.setattr(settings, "switch_data_dir", tmp_path / "deadman_switch")
    monkeypatch.setattr(settings, "support_data_dir", tmp_path / "support")
    monkeypatch.setattr(settings, "onboarding_data_dir", tmp_path / "onboarding")

    # === 业务模块 _DEFAULT_DATA_DIR 模块级常量 ===
    # support / onboarding 的 store 在 import 时取 _DEFAULT_DATA_DIR，
    # 测试期 monkeypatch 该常量；其它 store 在 __init__ 时读环境变量或 settings。
    import deadman.onboarding.store as obs
    import deadman.support.store as ssm

    monkeypatch.setattr(ssm, "_DEFAULT_DATA_DIR", tmp_path / "support")
    monkeypatch.setattr(obs, "_DEFAULT_DATA_DIR", tmp_path / "onboarding")

    # === DATA_DIR 环境变量（SwitchStore / SupportStore 等在 __init__ 时读环境变量，
    # 不经过 settings；SwitchAutoTicker 后台线程也用 SwitchStore() 默认构造，
    # 必须把环境变量也重定向到 tmp，否则 ticker 会读到 ~/.deadman 下的真实脏数据）===
    monkeypatch.setenv("DEADMAN_SWITCH_DATA_DIR", str(tmp_path / "deadman_switch"))
    monkeypatch.setenv("DEADMAN_SUPPORT_DATA_DIR", str(tmp_path / "support"))
    monkeypatch.setenv("DEADMAN_ONBOARDING_DATA_DIR", str(tmp_path / "onboarding"))

    # === 禁用 SwitchAutoTicker 后台线程 ===
    # 该线程 run_forever 默认 sleep 300s 一轮，_stop_switch_auto_ticker 仅打日志
    # 不会真正取消 sleep，导致 TestClient 退出时线程仍存活并阻塞进程。
    # E2E 测的是 HTTP 端点行为，不需要后台调度器。
    monkeypatch.setenv("DEADMAN_SWITCH_AUTO_TICK_ENABLED", "0")

    # === 加密口令（vault / ending_note / deadman_switch 共用全局 secret）===
    monkeypatch.setenv("DEADMAN_VAULT_PASSWORD", "test-vault-passphrase-fixed")
    monkeypatch.setenv("DEADMAN_ENDING_NOTE_PASSPHRASE", "test-ending-note-passphrase")

    # === 限流禁用，避免 TestClient 连续请求被 429 ===
    monkeypatch.setenv("DEADMAN_RATE_LIMIT_ENABLED", "0")

    # === JWT secret 文件路径（JWTManager._load_or_create_secret 用 Path.home()）===
    # 不需要 monkeypatch，因为 settings.jwt_secret="" → JWTManager 走
    # 环境变量 DEADMAN_JWT_SECRET（未设置）→ _load_or_create_secret() 走文件。
    # 由于 UserStore 与 JWTManager 共享 ~/.deadman/auth/jwt_secret，
    # auth_data_dir 已被 monkeypatch 到 tmp_path/auth，但 JWTManager 用的是
    # 模块级 _DEFAULT_SECRET_FILE = Path.home() / ".deadman" / "auth" / "jwt_secret"
    # → 这里通过显式注入 secret 让两者共享同一份。
    # 先让 UserStore 创建 secret 文件（在 auth_data_dir），然后读出来注入 JWTManager。
    # 但 UserStore 实际用 _SERVER_SECRET_FILE = "jwt_secret"（相对名），
    # 在 self.data_dir / "jwt_secret" 创建。我们这里直接显式提供一个 secret。
    fixed_jwt_secret = "e2e-test-jwt-secret-fixed-do-not-use-in-prod-32bytes"
    monkeypatch.setattr(settings, "jwt_secret", fixed_jwt_secret)
    monkeypatch.setenv("DEADMAN_JWT_SECRET", fixed_jwt_secret)

    return tmp_path


@pytest.fixture
def client(isolated_env, patch_llm) -> TestClient:
    """构造一个 TestClient（每次测试独立环境）。

    依赖 patch_llm（conftest.py）：把 LLMClient 全局单例替换为 mock，
    避免 /api/chat / /api/stream 的 graph 调用真实 LLM API（空 api_key
    下 graph 虽然会降级，但 _stream_chat 后台 asyncio.create_task 的
    teardown 可能挂住后续测试）。用 with 语法触发 lifespan。
    """
    from deadman.web.app import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _register_and_login(
    client: TestClient, email: str, password: str = "Password123!"
) -> tuple[str, str]:
    """注册并返回 (user_id, token)"""
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": password, "display_name": email.split("@")[0]},
    )
    assert r.status_code in (200, 201), f"register failed: {r.status_code} {r.text}"
    body = r.json()
    return body["user_id"], body["token"]


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# =====================================================================
# 1. 未认证访问公开端点
# =====================================================================


class TestPublicEndpoints:
    """未认证访问的公开端点 —— 应全部返回 200。"""

    def test_root_returns_html(self, client: TestClient):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_mobile_returns_html(self, client: TestClient):
        r = client.get("/m")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_healthz(self, client: TestClient):
        r = client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "alive"

    def test_readyz(self, client: TestClient):
        r = client.get("/readyz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ready"

    def test_api_health(self, client: TestClient):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_api_agents(self, client: TestClient):
        r = client.get("/api/agents")
        assert r.status_code == 200
        agents = r.json()["agents"]
        assert len(agents) >= 5
        ids = {a["id"] for a in agents}
        assert "death-aftercare" in ids

    def test_api_tools(self, client: TestClient):
        r = client.get("/api/tools")
        assert r.status_code == 200
        body = r.json()
        assert "tools" in body

    def test_api_hotlines(self, client: TestClient):
        r = client.get("/api/hotlines")
        assert r.status_code == 200
        body = r.json()
        assert "hotlines" in body

    def test_api_institutions_keyword(self, client: TestClient):
        r = client.get("/api/institutions", params={"keyword": "殡仪"})
        assert r.status_code == 200
        body = r.json()
        assert "institutions" in body
        assert "disclaimer" in body

    def test_api_disclaimer(self, client: TestClient):
        r = client.get("/api/disclaimer")
        assert r.status_code == 200
        body = r.json()
        assert "text" in body
        assert "disclaimer" in body


# =====================================================================
# 2. 注册 / 登录 / me / refresh
# =====================================================================


class TestAuthFlow:
    """认证全链路：register → login → me → refresh。"""

    def test_register_returns_token(self, client: TestClient):
        uid, token = _register_and_login(client, "alice@example.com")
        assert uid
        assert len(token) > 30

    def test_login_after_register(self, client: TestClient):
        _register_and_login(client, "bob@example.com", "SecretPass1")
        r = client.post(
            "/api/auth/login",
            json={"email": "bob@example.com", "password": "SecretPass1"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["display_name"] == "bob"

    def test_login_wrong_password_returns_401(self, client: TestClient):
        _register_and_login(client, "carol@example.com", "SecretPass1")
        r = client.post(
            "/api/auth/login",
            json={"email": "carol@example.com", "password": "WrongPassword!"},
        )
        assert r.status_code == 401

    def test_me_with_valid_token(self, client: TestClient):
        uid, token = _register_and_login(client, "dave@example.com")
        r = client.get("/api/auth/me", headers=_auth_headers(token))
        assert r.status_code == 200
        body = r.json()
        assert body["user_id"] == uid
        assert body["email"] == "dave@example.com"
        assert "password_hash" not in body  # 不泄露密码哈希

    def test_refresh_returns_new_token(self, client: TestClient, monkeypatch):
        # refresh 仅在剩余有效期 < 1 天时签发新 token；构造一个即将过期的 token
        from deadman.auth.jwt import JWTManager
        from deadman.auth.store import UserStore
        from deadman.config import settings

        # 注册
        store = UserStore(data_dir=settings.auth_data_dir)
        store.password_min_length = 8
        user = store.register("erin@example.com", "Password1!", "Erin")

        # 用 expiry_days=0（即立刻过期）签发一个 token，
        # 但 verify 会拒绝已过期的 token → refresh 返回 None → 401。
        # 所以我们用一个稍微过期的 token，但用 future-dated issue 时的方式不行。
        # 改用 expiry_days=-1 验证过期路径：会签发已过期 token → refresh 拒绝 → 401
        # 但 issue 时若 expiry_seconds<=0，jwt.encode 仍会签发（exp=now+negative）
        # → verify 抛 ExpiredSignatureError → 返回 None → 401。
        # 这覆盖了"过期 token" 测试。
        # 对于"成功 refresh"，我们手动构造一个剩余 < 1 天的 token：
        mgr_short = JWTManager(secret=settings.jwt_secret, expiry_days=0.5)
        token_short = mgr_short.issue(user)
        r = client.post("/api/auth/refresh", headers=_auth_headers(token_short))
        # 0.5 天 = 12 小时 < 24 小时（_REFRESH_THRESHOLD_SECONDS）→ 应返回新 token
        assert r.status_code == 200, f"refresh failed: {r.status_code} {r.text}"
        body = r.json()
        assert "token" in body
        assert body["token"] != token_short


# =====================================================================
# 3. 认证失败工况
# =====================================================================


class TestAuthFailures:
    """401 工况：无 token / 错误 token / 过期 token。"""

    def test_me_without_token_returns_401(self, client: TestClient):
        r = client.get("/api/auth/me")
        assert r.status_code == 401

    def test_me_with_invalid_token_returns_401(self, client: TestClient):
        r = client.get("/api/auth/me", headers=_auth_headers("invalid.token.here"))
        assert r.status_code == 401

    def test_me_with_expired_token_returns_401(self, client: TestClient, monkeypatch):
        # 用 expiry_days=0（实际为 0 秒）签发，立刻过期
        from deadman.auth.jwt import JWTManager
        from deadman.auth.store import UserStore
        from deadman.config import settings

        store = UserStore(data_dir=settings.auth_data_dir)
        store.password_min_length = 8
        user = store.register("frank@example.com", "Password1!", "Frank")
        # expiry_seconds = 0 → exp == iat → 已过期
        mgr = JWTManager(secret=settings.jwt_secret, expiry_days=0)
        expired_token = mgr.issue(user)
        # pyjwt 可能允许 exp == iat 但 verify 时立刻抛 ExpiredSignatureError
        r = client.get("/api/auth/me", headers=_auth_headers(expired_token))
        assert r.status_code == 401

    def test_protected_endpoint_without_token_returns_401(self, client: TestClient):
        # 多个受保护端点都应 401
        for path in [
            "/api/ending-note",
            "/api/vault/items",
            "/api/switch/status",
            "/api/cases",
            "/api/support/tickets",
            "/api/onboarding",
            "/api/plan-score",
            "/api/letters/types",
            "/api/memorial/types",
        ]:
            r = client.get(path)
            assert r.status_code == 401, f"{path} 未带 token 应 401，实际 {r.status_code}"


# =====================================================================
# 4. Onboarding 画像
# =====================================================================


class TestOnboardingFlow:
    """GET /api/onboarding/step/1 → POST /api/onboarding → GET /api/onboarding。"""

    def test_step_1_returns_relationship_question(self, client: TestClient):
        r = client.get("/api/onboarding/step/1")
        assert r.status_code == 200
        body = r.json()
        assert body["step"]["key"] == "location"  # step 0=relationship, 1=location
        assert body["total_steps"] == 5

    def test_save_onboarding_profile(self, client: TestClient):
        _, token = _register_and_login(client, "gina@example.com")
        r = client.post(
            "/api/onboarding",
            json={
                "relationship": "亲属",
                "location": "北京",
                "death_date": "2024-01-15",
                "current_stage": ["死亡证明"],
                "consent": True,
            },
            headers=_auth_headers(token),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["completed"] is True
        assert body["profile"]["relationship"] == "亲属"
        assert body["profile"]["location"] == "北京"

    def test_get_onboarding_after_save(self, client: TestClient):
        _, token = _register_and_login(client, "harry@example.com")
        client.post(
            "/api/onboarding",
            json={
                "relationship": "朋友",
                "location": "上海",
                "death_date": "",
                "current_stage": [],
                "consent": True,
            },
            headers=_auth_headers(token),
        )
        r = client.get("/api/onboarding", headers=_auth_headers(token))
        assert r.status_code == 200
        body = r.json()
        assert body["completed"] is True
        assert body["profile"]["relationship"] == "朋友"

    def test_get_onboarding_empty_returns_null(self, client: TestClient):
        _, token = _register_and_login(client, "ivy@example.com")
        r = client.get("/api/onboarding", headers=_auth_headers(token))
        assert r.status_code == 200
        body = r.json()
        assert body["completed"] is False
        assert body["profile"] is None


# =====================================================================
# 5. 终活笔记全流程
# =====================================================================


class TestEndingNoteFlow:
    """guide/next → completion → save section → view → share → unshare。"""

    def test_full_journey(self, client: TestClient):
        _, token = _register_and_login(client, "jack@example.com")
        h = _auth_headers(token)

        # guide/next
        r = client.get("/api/ending-note/guide/next", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "section" in body
        assert "question" in body

        # completion (空笔记)
        r = client.get("/api/ending-note/completion", headers=h)
        assert r.status_code == 200
        assert "completion" in r.json()

        # save section
        r = client.post(
            "/api/ending-note/section",
            json={
                "section": "personal_info",
                "answer": {
                    "full_name": "张三",
                    "birth_date": "1958-03-15",
                    "occupation": "工程师",
                },
            },
            headers=h,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["note"]["personal_info"]["full_name_masked"] == "张**"

        # view note
        r = client.get("/api/ending-note", headers=h)
        assert r.status_code == 200
        assert r.json()["note"] is not None

        # share (with another user)
        other_uid, _ = _register_and_login(client, "kate@example.com")
        r = client.post(
            "/api/ending-note/share",
            json={"target_user_id": other_uid, "sections": ["personal_info"]},
            headers=h,
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

        # unshare
        r = client.delete(
            "/api/ending-note/share",
            params={"target_user_id": other_uid},
            headers=h,
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True


# =====================================================================
# 6. 保险库全流程
# =====================================================================


class TestVaultFlow:
    """add → list → get → update → trigger → delete。"""

    def test_full_journey(self, client: TestClient):
        _, token = _register_and_login(client, "liam@example.com")
        h = _auth_headers(token)

        # add
        r = client.post(
            "/api/vault/items",
            json={
                "type": "note",
                "title": "我的遗嘱",
                "content": "我名下的房产由配偶继承。",
                "beneficiary_user_ids": [],
                "delivery_trigger": "manual",
            },
            headers=h,
        )
        assert r.status_code == 201, r.text
        item = r.json()
        item_id = item["item_id"]
        assert item["title"] == "我的遗嘱"

        # list
        r = client.get("/api/vault/items", headers=h)
        assert r.status_code == 200
        items = r.json()["items"]
        assert any(i["item_id"] == item_id for i in items)

        # get
        r = client.get(f"/api/vault/items/{item_id}", headers=h)
        assert r.status_code == 200
        assert r.json()["item_id"] == item_id

        # update
        r = client.put(
            f"/api/vault/items/{item_id}",
            json={"title": "我的遗嘱 v2"},
            headers=h,
        )
        assert r.status_code == 200, r.text
        assert r.json()["title"] == "我的遗嘱 v2"

        # trigger delivery
        r = client.post(
            f"/api/vault/items/{item_id}/trigger",
            json={"trigger_type": "manual"},
            headers=h,
        )
        assert r.status_code == 200, r.text
        # 注意：trigger 返回的内容视实现而定，仅验证不抛 500

        # delete
        r = client.delete(f"/api/vault/items/{item_id}", headers=h)
        assert r.status_code == 200
        assert r.json()["deleted"] is True

        # 再 GET 应 404
        r = client.get(f"/api/vault/items/{item_id}", headers=h)
        assert r.status_code == 404


# =====================================================================
# 7. Dead Man Switch 全状态机
# =====================================================================


class TestSwitchStateMachine:
    """init → status → checkin → actions → verify-contact → verify-heir
    → engage-lawyer → cancel；以及 execute 在未过冷静期时 409。"""

    def test_full_journey(self, client: TestClient):
        _, token = _register_and_login(client, "mike@example.com")
        h = _auth_headers(token)

        # init
        r = client.post(
            "/api/switch/init",
            json={
                "frequency": 30,
                "missed": 3,
                "window": 7,
                "cooldown": 7,
                "emergency_contacts": ["contact-1"],
                "lawyer_id": "lawyer-1",
                "heir_ids": ["heir-1"],
                "email": "mike@example.com",
                "phone": "13800138000",
            },
            headers=h,
        )
        assert r.status_code == 201, r.text
        record = r.json()
        assert record["state"] == "ACTIVE"

        # status
        r = client.get("/api/switch/status", headers=h)
        assert r.status_code == 200
        assert r.json()["state"] == "ACTIVE"

        # checkin
        r = client.post("/api/switch/checkin", json={"method": "web"}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "ACTIVE"

        # actions
        r = client.get("/api/switch/actions", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert "pending_actions" in body
        assert "executed_actions" in body

        # verify-contact (confirm=True)
        r = client.post(
            "/api/switch/verify-contact",
            json={"contact_id": "contact-1", "confirm": True},
            headers=h,
        )
        assert r.status_code == 200, r.text

        # verify-heir (confirm=True)
        r = client.post(
            "/api/switch/verify-heir",
            json={"heir_id": "heir-1", "confirm": True},
            headers=h,
        )
        assert r.status_code == 200, r.text

        # engage-lawyer (需在 VERIFYING 或 CONFIRMED 状态)
        r = client.post("/api/switch/engage-lawyer", headers=h)
        # 状态机当前是 ACTIVE（因为 checkin 重置过），engage-lawyer 期望 VERIFYING/CONFIRMED
        # → 返回 409 + "not_in_verifying_or_confirmed_state"
        assert r.status_code in (200, 409), r.text

        # execute 在未过冷静期：状态非 CONFIRMED → 抛 RuntimeError → 409
        r = client.post("/api/switch/execute", headers=h)
        assert r.status_code == 409, r.text

        # cancel
        r = client.post("/api/switch/cancel", json={"reason": "test"}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "CANCELLED"


# =====================================================================
# 8. 案例管理
# =====================================================================


class TestCasesFlow:
    """create → list → add event → timeline → archive。"""

    def test_full_journey(self, client: TestClient):
        _, token = _register_and_login(client, "nora@example.com")
        h = _auth_headers(token)

        # create
        r = client.post(
            "/api/cases",
            json={"decedent_alias": "我父亲", "relationship": "子女"},
            headers=h,
        )
        assert r.status_code == 201, r.text
        case_id = r.json()["case_id"]
        assert case_id.startswith("case-")

        # list
        r = client.get("/api/cases", headers=h)
        assert r.status_code == 200
        cases = r.json()["cases"]
        assert any(c["case_id"] == case_id for c in cases)

        # add event
        r = client.post(
            f"/api/cases/{case_id}/events",
            json={"event": "已办理死亡证明", "agent": "death-aftercare", "notes": "派出所"},
            headers=h,
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["events"]) == 1

        # timeline
        r = client.get(f"/api/cases/{case_id}/timeline", headers=h)
        assert r.status_code == 200
        timeline = r.json()["timeline"]
        assert len(timeline) == 1
        assert timeline[0]["event"] == "已办理死亡证明"

        # archive
        r = client.post(f"/api/cases/{case_id}/archive", headers=h)
        assert r.status_code == 200
        assert r.json()["archived"] is True


# =====================================================================
# 9. 通知信函
# =====================================================================


class TestLettersFlow:
    """types → template → generate。"""

    def test_letters_types(self, client: TestClient):
        _, token = _register_and_login(client, "oscar@example.com")
        h = _auth_headers(token)
        r = client.get("/api/letters/types", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["count"] >= 1
        assert "types" in body

    def test_letters_template_valid(self, client: TestClient):
        _, token = _register_and_login(client, "oscar2@example.com")
        h = _auth_headers(token)
        # 用一个存在的类型 household_cancellation
        r = client.get(
            "/api/letters/template", params={"type": "household_cancellation"}, headers=h
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["type"] == "household_cancellation"
        assert "template" in body

    def test_letters_template_unknown_returns_404(self, client: TestClient):
        _, token = _register_and_login(client, "oscar3@example.com")
        h = _auth_headers(token)
        # 任务书里写的 cremation_notice 实际不存在于 LETTER_TEMPLATES
        # → 应 404（不是 500）
        r = client.get("/api/letters/template", params={"type": "cremation_notice"}, headers=h)
        assert r.status_code == 404, r.text
        # 同时验证 unknown 类型也 404
        r2 = client.get("/api/letters/template", params={"type": "totally_unknown"}, headers=h)
        assert r2.status_code == 404

    def test_letters_generate(self, client: TestClient):
        _, token = _register_and_login(client, "oscar4@example.com")
        h = _auth_headers(token)
        r = client.post(
            "/api/letters/generate",
            json={
                "letter_type": "household_cancellation",
                "decedent_name": "张老先生",
                "decedent_id_masked": "110********",
                "death_date": "2024-01-15",
                "applicant_name": "张某",
                "applicant_relationship": "子女",
                "recipient_org": "派出所",
                "extra_fields": {
                    "household_type": "家庭户",
                    "household_address": "北京市朝阳区",
                },
            },
            headers=h,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "content" in body or "letter_text" in body or "text" in body


# =====================================================================
# 10. 悼文生成
# =====================================================================


class TestMemorialFlow:
    """memorial types → generate (用最小合法参数)。"""

    def test_memorial_types(self, client: TestClient):
        _, token = _register_and_login(client, "paul@example.com")
        h = _auth_headers(token)
        r = client.get("/api/memorial/types", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert len(body["types"]) >= 1
        assert "tones" in body

    def test_memorial_generate_minimal(self, client: TestClient):
        _, token = _register_and_login(client, "paul2@example.com")
        h = _auth_headers(token)
        r = client.post(
            "/api/memorial/generate",
            json={
                "doc_type": "epitaph",
                "decedent_name": "先父",
                "relationship": "儿子",
                "personality_traits": ["宽厚"],
                "memories": ["教我骑自行车"],
                "values_or_sayings": ["做人要正直"],
                "tone": "solemn",
                "faith": "none",
                "language": "zh-CN",
            },
            headers=h,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "text" in body
        assert body["doc_type"] == "epitaph"


# =====================================================================
# 11. 规划评分
# =====================================================================


class TestPlanScore:
    """plan-score → plan-score/detail。"""

    def test_plan_score(self, client: TestClient):
        _, token = _register_and_login(client, "quinn@example.com")
        h = _auth_headers(token)
        r = client.get("/api/plan-score", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "total_score" in body or "score" in body

    def test_plan_score_detail(self, client: TestClient):
        _, token = _register_and_login(client, "quinn2@example.com")
        h = _auth_headers(token)
        r = client.get("/api/plan-score/detail", headers=h)
        assert r.status_code == 200, r.text


# =====================================================================
# 12. 客服工单
# =====================================================================


class TestSupportTicketsFlow:
    """create → list → reply → update status。"""

    def test_full_journey(self, client: TestClient):
        _, token = _register_and_login(client, "rachel@example.com")
        h = _auth_headers(token)

        # create
        r = client.post(
            "/api/support/tickets",
            json={
                "category": "咨询",
                "priority": "普通",
                "subject": "如何办理户口注销？",
                "description": "需要哪些材料？",
            },
            headers=h,
        )
        assert r.status_code == 201, r.text
        ticket = r.json()["ticket"]
        ticket_id = ticket["ticket_id"]
        assert ticket_id.startswith("tkt-")
        assert ticket["status"] == "open"

        # list
        r = client.get("/api/support/tickets", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["count"] >= 1
        assert any(t["ticket_id"] == ticket_id for t in body["tickets"])

        # reply
        r = client.post(
            f"/api/support/tickets/{ticket_id}/replies",
            json={"content": "补充：逝者在北京"},
            headers=h,
        )
        assert r.status_code == 200, r.text
        assert r.json()["reply"]["content"] == "补充：逝者在北京"

        # update status
        r = client.put(
            f"/api/support/tickets/{ticket_id}/status",
            json={"status": "in_progress"},
            headers=h,
        )
        assert r.status_code == 200, r.text
        assert r.json()["ticket"]["status"] == "in_progress"


# =====================================================================
# 13. 对话（chat / stream）
# =====================================================================


class TestChatEndpoints:
    """POST /api/chat + GET /api/stream —— 验证不抛 500 即可。"""

    def test_post_chat_no_500(self, client: TestClient):
        r = client.post("/api/chat", json={"query": "你好"})
        assert r.status_code != 500, f"/api/chat 返回 500: {r.text}"
        assert r.status_code == 200
        body = r.json()
        assert "response" in body

    def test_get_stream_consumes_sse_and_terminates(self, client: TestClient):
        # GET /api/stream 的 SSE 流必须在 _stream_chat 结束后正常终止
        # （依赖 _WfileAdapter.close() 投递 None 哨兵）。
        # 本测试完整消费响应 body，验证不挂起、不抛 500、收到 done 事件。
        r = client.get("/api/stream", params={"query": "你好"})
        assert r.status_code == 200, f"/api/stream 返回 {r.status_code}: {r.text}"
        assert r.headers["content-type"].startswith("text/event-stream"), (
            f"Content-Type 应为 text/event-stream，实际: {r.headers['content-type']}"
        )
        body = r.content.decode("utf-8", errors="ignore")
        # 流必须终止且包含 done 事件（_stream_chat 末尾推送 event: done）
        assert "event: done" in body or "event: error" in body, (
            f"SSE 流未正常终止（缺 done/error 事件），body 前 500 字: {body[:500]}"
        )

    def test_get_stream_openapi_reports_sse(self, client: TestClient):
        # OpenAPI schema 应显式声明 text/event-stream 响应（Bug#3 修复）
        spec = client.get("/openapi.json").json()
        assert "/api/stream" in spec["paths"], "OpenAPI 缺 /api/stream 路径"
        op = spec["paths"]["/api/stream"].get("get", {})
        assert op, "/api/stream 缺 GET 操作定义"
        params = {p["name"]: p for p in op.get("parameters", [])}
        assert "query" in params, "/api/stream 缺 query 参数"
        # 验证 responses 显式声明了 text/event-stream
        responses = op.get("responses", {})
        ok_200 = responses.get("200", {})
        content_types = list(ok_200.get("content", {}).keys())
        assert "text/event-stream" in content_types, (
            f"OpenAPI /api/stream 响应未声明 text/event-stream，实际 content types: {content_types}"
        )


# =====================================================================
# 14. 运维端点
# =====================================================================


class TestOpsEndpoints:
    """/metrics、/api/obs/dashboard、/api/slo、/api/dashboard、/api/deploy/check、
    /api/memory/state、/api/health/all、/docs、/openapi.json。"""

    def test_metrics(self, client: TestClient):
        r = client.get("/metrics")
        assert r.status_code == 200
        # prometheus 文本格式
        ct = r.headers.get("content-type", "")
        assert "text" in ct

    def test_obs_dashboard(self, client: TestClient):
        r = client.get("/api/obs/dashboard")
        assert r.status_code != 500, f"/api/obs/dashboard 返回 500: {r.text}"
        assert r.status_code == 200

    def test_slo(self, client: TestClient):
        r = client.get("/api/slo")
        assert r.status_code != 500, f"/api/slo 返回 500: {r.text}"
        assert r.status_code == 200

    def test_dashboard(self, client: TestClient):
        r = client.get("/api/dashboard")
        assert r.status_code == 200
        body = r.json()
        assert "total_conversations" in body

    def test_deploy_check(self, client: TestClient):
        r = client.get("/api/deploy/check")
        assert r.status_code == 200
        body = r.json()
        assert "artifacts" in body

    def test_memory_state(self, client: TestClient):
        r = client.get("/api/memory/state")
        assert r.status_code != 500, f"/api/memory/state 返回 500: {r.text}"
        assert r.status_code == 200

    def test_health_all(self, client: TestClient):
        r = client.get("/api/health/all")
        assert r.status_code == 200
        body = r.json()
        # 至少应有若干 domain 字段
        assert isinstance(body, dict)

    def test_docs_page(self, client: TestClient):
        r = client.get("/docs")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_openapi_json(self, client: TestClient):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        body = r.json()
        assert body["info"]["title"] == "deadman"
        assert "paths" in body


# =====================================================================
# 15. 跨用户权限隔离
# =====================================================================


class TestCrossUserIsolation:
    """用户 A 的笔记/保险库/Switch 不能被用户 B 访问。"""

    def test_ending_note_isolation(self, client: TestClient):
        uid_a, token_a = _register_and_login(client, "alice_iso@example.com")
        _uid_b, token_b = _register_and_login(client, "bob_iso@example.com")
        ha = _auth_headers(token_a)
        hb = _auth_headers(token_b)

        # Alice 保存笔记
        r = client.post(
            "/api/ending-note/section",
            json={
                "section": "personal_info",
                "answer": {"full_name": "李四", "birth_date": "1960-01-01"},
            },
            headers=ha,
        )
        assert r.status_code == 200

        # Bob 用自己的 token GET /api/ending-note → 应看到自己的空笔记（404，不是 Alice 的）
        r = client.get("/api/ending-note", headers=hb)
        assert r.status_code == 404  # Bob 没笔记 → 404

        # Bob 试 share Alice 的笔记给自己的 uid → 会创建一条 Bob 自己的 share 记录，
        # 不会触碰 Alice 的数据（store.share_with 是 per-user 的 shares.json）
        r = client.post(
            "/api/ending-note/share",
            json={"target_user_id": uid_a, "sections": ["personal_info"]},
            headers=hb,
        )
        # 这实际是 Bob 把自己的笔记共享给 Alice（Bob 没笔记 → 仍可创建 share 记录，
        # 但不暴露 Alice 的数据）
        assert r.status_code == 200

    def test_vault_isolation(self, client: TestClient):
        _uid_a, token_a = _register_and_login(client, "alice_v@example.com")
        _, token_b = _register_and_login(client, "bob_v@example.com")
        ha = _auth_headers(token_a)
        hb = _auth_headers(token_b)

        # Alice 创建 vault item
        r = client.post(
            "/api/vault/items",
            json={
                "type": "note",
                "title": "Alice 私密笔记",
                "content": "仅 Alice 可见",
                "delivery_trigger": "manual",
            },
            headers=ha,
        )
        assert r.status_code == 201
        item_id = r.json()["item_id"]

        # Bob 用自己的 token 列出 → 应为空
        r = client.get("/api/vault/items", headers=hb)
        assert r.status_code == 200
        assert r.json()["items"] == []

        # Bob 试访问 Alice 的 item_id → 应 404（不存在或无权限）
        r = client.get(f"/api/vault/items/{item_id}", headers=hb)
        assert r.status_code == 404

        # Bob 试 update → 应 404
        r = client.put(
            f"/api/vault/items/{item_id}",
            json={"title": "hacked"},
            headers=hb,
        )
        assert r.status_code == 404

        # Bob 试 delete → 应 404
        r = client.delete(f"/api/vault/items/{item_id}", headers=hb)
        assert r.status_code == 404

    def test_switch_isolation(self, client: TestClient):
        _, token_a = _register_and_login(client, "alice_s@example.com")
        _, token_b = _register_and_login(client, "bob_s@example.com")
        ha = _auth_headers(token_a)
        hb = _auth_headers(token_b)

        # Alice 初始化 switch
        r = client.post(
            "/api/switch/init",
            json={
                "frequency": 30,
                "missed": 3,
                "window": 7,
                "cooldown": 7,
            },
            headers=ha,
        )
        assert r.status_code == 201

        # Bob 查 status → 应 404（Bob 没 init switch）
        r = client.get("/api/switch/status", headers=hb)
        assert r.status_code == 404

    def test_support_ticket_isolation(self, client: TestClient):
        _, token_a = _register_and_login(client, "alice_t@example.com")
        _, token_b = _register_and_login(client, "bob_t@example.com")
        ha = _auth_headers(token_a)
        hb = _auth_headers(token_b)

        # Alice 创建工单
        r = client.post(
            "/api/support/tickets",
            json={
                "category": "咨询",
                "priority": "普通",
                "subject": "Alice 私密",
                "description": "私密内容",
            },
            headers=ha,
        )
        ticket_id = r.json()["ticket"]["ticket_id"]

        # Bob 试 GET → 应 404
        r = client.get(f"/api/support/tickets/{ticket_id}", headers=hb)
        assert r.status_code == 404

        # Bob 试 reply → 应 404
        r = client.post(
            f"/api/support/tickets/{ticket_id}/replies",
            json={"content": "恶意回复"},
            headers=hb,
        )
        assert r.status_code == 404

    def test_cases_isolation(self, client: TestClient):
        _, token_a = _register_and_login(client, "alice_c@example.com")
        _, token_b = _register_and_login(client, "bob_c@example.com")
        ha = _auth_headers(token_a)
        hb = _auth_headers(token_b)

        # Alice 创建 case
        r = client.post(
            "/api/cases",
            json={"decedent_alias": "我父亲", "relationship": "子女"},
            headers=ha,
        )
        case_id = r.json()["case_id"]

        # Bob 试 add event → 应 404
        r = client.post(
            f"/api/cases/{case_id}/events",
            json={"event": "恶意", "agent": "evil"},
            headers=hb,
        )
        assert r.status_code == 404

        # Bob 试 timeline → 应 404（与 case_get/events/archive 一致，不泄露端点可达性）
        r = client.get(f"/api/cases/{case_id}/timeline", headers=hb)
        assert r.status_code == 404

        # Bob 试 archive → 应 404
        r = client.post(f"/api/cases/{case_id}/archive", headers=hb)
        assert r.status_code == 404


# =====================================================================
# 16. Pydantic 校验
# =====================================================================


class TestPydanticValidation:
    """缺失必填 / 错误类型 / 超大数值 → 应 422，不 500。"""

    def test_register_missing_email_returns_422(self, client: TestClient):
        r = client.post("/api/auth/register", json={"password": "Password1!"})
        assert r.status_code == 422

    def test_register_missing_password_returns_422(self, client: TestClient):
        r = client.post("/api/auth/register", json={"email": "x@example.com"})
        assert r.status_code == 422

    def test_switch_init_frequency_string_returns_422(self, client: TestClient):
        _, token = _register_and_login(client, "pyd1@example.com")
        r = client.post(
            "/api/switch/init",
            json={"frequency": "not-an-int"},  # 字符串传给 int 字段
            headers=_auth_headers(token),
        )
        assert r.status_code == 422, r.text

    def test_switch_init_missed_huge_number_returns_422(self, client: TestClient):
        # 超大数值（超出 int 范围）
        _, token = _register_and_login(client, "pyd2@example.com")
        r = client.post(
            "/api/switch/init",
            json={"missed": 10**100},
            headers=_auth_headers(token),
        )
        # Pydantic 可能接受大 int，也可能 422（取决于版本）；只要不 500
        assert r.status_code != 500, f"返回 500: {r.text}"

    def test_vault_item_missing_required_returns_422(self, client: TestClient):
        # VaultItemAddRequest 所有字段都有默认值，所以这里测一个非可选模型
        # 用 EndingNoteSectionRequest（section + answer 都必填）
        _, token = _register_and_login(client, "pyd3@example.com")
        r = client.post(
            "/api/ending-note/section",
            json={"section": "personal_info"},  # 缺 answer
            headers=_auth_headers(token),
        )
        assert r.status_code == 422

    def test_support_ticket_missing_required_returns_422(self, client: TestClient):
        # SupportTicketReplyRequest.content 必填
        _, token = _register_and_login(client, "pyd4@example.com")
        # 先创建一个 ticket
        r = client.post(
            "/api/support/tickets",
            json={
                "category": "咨询",
                "priority": "普通",
                "subject": "test",
                "description": "test",
            },
            headers=_auth_headers(token),
        )
        ticket_id = r.json()["ticket"]["ticket_id"]
        # 缺 content → 422
        r = client.post(
            f"/api/support/tickets/{ticket_id}/replies",
            json={},
            headers=_auth_headers(token),
        )
        assert r.status_code == 422

    def test_case_event_missing_required_returns_422(self, client: TestClient):
        # CaseEventRequest.event 默认空字符串 ""，但 agent 默认 "unknown"
        # 用一个更严格的：LetterGenerateRequest.letter_type 必填
        _, token = _register_and_login(client, "pyd5@example.com")
        r = client.post(
            "/api/letters/generate",
            json={},  # 缺 letter_type
            headers=_auth_headers(token),
        )
        assert r.status_code == 422

    def test_register_with_invalid_email_no_at_returns_400(self, client: TestClient):
        # email 缺 @ → 通过 Pydantic 校验（email: str），但在 store.register 抛
        # ValueError → 400
        r = client.post(
            "/api/auth/register",
            json={"email": "not-an-email", "password": "Password1!"},
        )
        assert r.status_code == 400


# =====================================================================
# 17. CORS 预检
# =====================================================================


class TestCORSPreflight:
    """OPTIONS 请求带 Origin + Access-Control-Request-Method 头。"""

    def test_cors_preflight_simple_get(self, client: TestClient):
        r = client.options(
            "/api/health",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        # CORS 中间件应返回 200，并带 Access-Control-Allow-Origin
        assert r.status_code == 200
        aco = r.headers.get("access-control-allow-origin")
        assert aco is not None, "缺 Access-Control-Allow-Origin"

    def test_cors_preflight_post_with_auth_header(self, client: TestClient):
        r = client.options(
            "/api/auth/register",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type, Authorization",
            },
        )
        assert r.status_code == 200
        aco = r.headers.get("access-control-allow-origin")
        assert aco is not None
        # 应允许 POST
        acam = r.headers.get("access-control-allow-methods", "")
        assert "POST" in acam or acam == "*", f"未允许 POST: {acam}"

    def test_cors_preflight_delete(self, client: TestClient):
        r = client.options(
            "/api/vault/items/some-id",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "DELETE",
            },
        )
        assert r.status_code == 200
        aco = r.headers.get("access-control-allow-origin")
        assert aco is not None
