"""测试 deadman.deadman_switch - Phase 15 Dead Man Switch 多因子死亡推定

覆盖点（24 个）：
    - test_init_creates_switch_with_default_config     初始化配置
    - test_init_with_pii_masks_email_and_phone         PII 脱敏存储
    - test_init_does_not_store_raw_pii                 文件中不出现原始 PII
    - test_checkin_resets_state_to_active              check-in 重置状态
    - test_checkin_appends_to_checkins_log             check-in 日志追加
    - test_tick_active_to_suspected_after_threshold   ACTIVE→SUSPECTED（N 次失联）
    - test_tick_active_stays_active_before_threshold  阈值未到保持 ACTIVE
    - test_tick_suspected_to_verifying                 SUSPECTED→VERIFYING
    - test_tick_does_not_advance_without_contact_conf  多因子验证缺失不推进
    - test_tick_advances_to_confirmed_with_all_confirm VERIFYING→CONFIRMED
    - test_emergency_contact_confirms_missing          紧急联系人确认失联
    - test_emergency_contact_reports_alive_resets      紧急联系人回复"安好"
    - test_heir_confirms_missing                       继承人确认失联
    - test_heir_reports_alive_resets                   继承人回复"安好"
    - test_engage_lawyer_marks_engaged                 律师介入
    - test_cooldown_not_passed_blocks_execution        冷静期内不可执行
    - test_cooldown_passed_allows_execution            冷静期过后可执行
    - test_cancel_moves_to_cancelled_state             取消测试
    - test_checkin_during_cooldown_resets              冷静期内 check-in 回到 ACTIVE
    - test_encrypted_at_rest_no_plaintext              加密存储测试
    - test_cli_switch_init_command                     CLI 命令测试
    - test_cli_switch_checkin_command                  CLI 命令测试
    - test_web_switch_init_without_token_returns_401   Web 端点 401
    - test_web_switch_init_with_token_returns_201      Web 端点 201

测试隔离：每个测试用 tmp_path fixture 独立数据目录，互不污染。
加密口令：测试期通过 monkeypatch DEADMAN_ENDING_NOTE_PASSPHRASE 环境变量固定。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import http.client
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from deadman.deadman_switch.models import (
    SwitchConfig,
    SwitchRecord,
    SwitchState,
    mask_email,
    mask_phone,
)
from deadman.deadman_switch.store import SwitchStore
from deadman.deadman_switch.actions import (
    SwitchActionExecutor,
)


# ====================================================================
# Fixtures
# ====================================================================
@pytest.fixture(autouse=True)
def _fixed_passphrase(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定加密口令，避免依赖环境变量"""
    monkeypatch.setenv("DEADMAN_ENDING_NOTE_PASSPHRASE", "test-switch-passphrase-fixed")


@pytest.fixture
def store(tmp_path: Path) -> SwitchStore:
    """每个测试独立的 SwitchStore"""
    return SwitchStore(data_dir=tmp_path / "deadman_switch")


def _default_config() -> SwitchConfig:
    """默认测试配置：30 天 check-in / 3 次失联 / 7 天窗口 / 7 天冷静期"""
    return SwitchConfig(
        check_in_frequency_days=30,
        missed_threshold=3,
        verification_window_days=7,
        cooldown_days=7,
        emergency_contacts=["contact-A"],
        lawyer_user_id="lawyer-X",
        heir_user_ids=["heir-1"],
    )


def _advance_to_verifying(store: SwitchStore, user_id: str = "u-1") -> SwitchRecord:
    """辅助：把状态机推到 VERIFYING（用于多个测试）"""
    store.init_switch(user_id, _default_config())
    # 90 天后 tick → ACTIVE → SUSPECTED → VERIFYING
    record = store.load(user_id)
    assert record is not None
    future = record.last_check_in + timedelta(days=100)
    store.tick(user_id, now=future)
    final = store.load(user_id)
    assert final is not None
    assert final.state == SwitchState.VERIFYING
    return final


# ====================================================================
# 1. 初始化测试
# ====================================================================
class TestInit:
    def test_init_creates_switch_with_default_config(self, store: SwitchStore):
        record = store.init_switch("u-init", SwitchConfig())
        assert record.user_id == "u-init"
        assert record.state == SwitchState.ACTIVE
        assert record.config.check_in_frequency_days == 30
        assert record.config.missed_threshold == 3
        assert record.config.cooldown_days == 7
        # state_history 记录了初始化事件
        assert len(record.state_history) >= 1
        assert record.state_history[0]["state"] == "ACTIVE"

    def test_init_with_pii_masks_email_and_phone(self, store: SwitchStore):
        cfg = SwitchConfig()
        cfg.set_email("alice@example.com")
        cfg.set_phone("13812345678")
        record = store.init_switch("u-pii", cfg)
        # 脱敏后存储
        assert record.config.email_masked == "a***@example.com"
        assert record.config.phone_masked == "138****5678"
        # 不应出现明文
        assert "alice" not in (record.config.email_masked or "")
        assert "13812345678" not in (record.config.phone_masked or "")

    def test_init_does_not_store_raw_pii(self, store: SwitchStore, tmp_path: Path):
        cfg = SwitchConfig()
        cfg.set_email("bob@example.com")
        cfg.set_phone("13900001111")
        store.init_switch("u-pii-2", cfg)
        # 读取加密文件内容
        switch_path = store._switch_path("u-pii-2")
        assert switch_path.exists()
        raw_bytes = switch_path.read_bytes()
        # 加密后不应出现明文 PII
        assert b"bob@example.com" not in raw_bytes
        assert b"13900001111" not in raw_bytes

    def test_mask_helpers(self):
        assert mask_email("alice@example.com") == "a***@example.com"
        assert mask_phone("13812345678") == "138****5678"
        # 短号脱敏为 ***
        assert mask_phone("12345") == "***"
        # 空值
        assert mask_email("") == ""
        assert mask_phone("") == ""


# ====================================================================
# 2. Check-in 测试
# ====================================================================
class TestCheckIn:
    def test_checkin_resets_state_to_active(self, store: SwitchStore):
        # 先推进到 VERIFYING
        record = _advance_to_verifying(store)
        user_id = record.user_id
        # 用户主动 check-in → 重置 ACTIVE
        new_record = store.record_check_in(user_id, method="web")
        assert new_record is not None
        assert new_record.state == SwitchState.ACTIVE
        # 多因子验证状态被清空
        assert new_record.contact_confirmations == {}
        assert new_record.heir_confirmations == {}
        assert new_record.lawyer_engaged is False
        # last_check_in 更新
        assert new_record.last_check_in is not None
        # state_history 追加了 reset 记录
        last_history = new_record.state_history[-1]
        assert last_history["state"] == "ACTIVE"
        assert "check_in_reset" in last_history["reason"]

    def test_checkin_appends_to_checkins_log(self, store: SwitchStore):
        store.init_switch("u-checkin-log", _default_config())
        store.record_check_in("u-checkin-log", method="web")
        store.record_check_in("u-checkin-log", method="cli")
        logs = store.list_check_ins("u-checkin-log")
        assert len(logs) == 2
        # 倒序：最新在前
        assert logs[0].method == "cli"
        assert logs[1].method == "web"

    def test_checkin_unknown_user_returns_none(self, store: SwitchStore):
        result = store.record_check_in("u-never-init", method="web")
        assert result is None


# ====================================================================
# 3. 状态机转换测试
# ====================================================================
class TestStateMachineTransitions:
    def test_tick_active_to_suspected_after_threshold(self, store: SwitchStore):
        store.init_switch("u-active", _default_config())
        record = store.load("u-active")
        assert record is not None
        # 30 天 * 3 次 = 90 天阈值；100 天后应触发 SUSPECTED
        future = record.last_check_in + timedelta(days=100)
        store.tick("u-active", now=future)
        # tick 会一次推进 ACTIVE → SUSPECTED → VERIFYING
        final = store.load("u-active")
        assert final is not None
        # 验证 SUSPECTED 被记录在 state_history
        states_in_history = [h["state"] for h in final.state_history]
        assert "SUSPECTED" in states_in_history
        assert "VERIFYING" in states_in_history

    def test_tick_active_stays_active_before_threshold(self, store: SwitchStore):
        store.init_switch("u-stay", _default_config())
        record = store.load("u-stay")
        assert record is not None
        # 50 天 < 90 天阈值，应保持 ACTIVE
        future = record.last_check_in + timedelta(days=50)
        store.tick("u-stay", now=future)
        final = store.load("u-stay")
        assert final is not None
        assert final.state == SwitchState.ACTIVE

    def test_tick_suspected_to_verifying(self, store: SwitchStore):
        # 手动构造：直接 transition_to SUSPECTED，再 tick
        store.init_switch("u-sus", _default_config())
        store.transition_to("u-sus", SwitchState.SUSPECTED, reason="manual_test")
        # tick 应推进到 VERIFYING
        store.tick("u-sus")
        final = store.load("u-sus")
        assert final is not None
        assert final.state == SwitchState.VERIFYING

    def test_tick_does_not_advance_without_contact_conf(self, store: SwitchStore):
        # 进入 VERIFYING 后未确认紧急联系人，tick 不应推进
        _advance_to_verifying(store, "u-no-conf")
        store.tick("u-no-conf")
        final = store.load("u-no-conf")
        assert final is not None
        # 缺少 contact 确认 → 停留 VERIFYING
        assert final.state == SwitchState.VERIFYING

    def test_tick_advances_to_confirmed_with_all_confirm(self, store: SwitchStore):
        record = _advance_to_verifying(store, "u-all-conf")
        user_id = record.user_id
        # 紧急联系人确认失联
        store.verify_emergency_contact(user_id, "contact-A", True)
        # 律师介入
        store.engage_lawyer(user_id)
        # 继承人确认失联
        store.verify_heir(user_id, "heir-1", True)
        # tick 应推进到 CONFIRMED
        store.tick(user_id)
        final = store.load(user_id)
        assert final is not None
        assert final.state == SwitchState.CONFIRMED
        assert final.confirmed_at is not None


# ====================================================================
# 4. 多因子验证测试
# ====================================================================
class TestMultiFactorVerification:
    def test_emergency_contact_confirms_missing(self, store: SwitchStore):
        record = _advance_to_verifying(store, "u-emg-conf")
        result, msg = store.verify_emergency_contact(
            record.user_id, "contact-A", True
        )
        assert result is not None
        assert result.contact_confirmations.get("contact-A") is True
        assert "contact-A" in result.contact_confirmed_at
        assert msg == "contact_confirmed_missing"

    def test_emergency_contact_reports_alive_resets(self, store: SwitchStore):
        record = _advance_to_verifying(store, "u-emg-alive")
        # confirm=False 表示联系人表示当事人安好
        result, msg = store.verify_emergency_contact(
            record.user_id, "contact-A", False
        )
        assert result is not None
        assert result.state == SwitchState.ACTIVE
        assert "alive_report" in msg

    def test_emergency_contact_unknown_id_rejected(self, store: SwitchStore):
        record = _advance_to_verifying(store, "u-emg-unknown")
        result, msg = store.verify_emergency_contact(
            record.user_id, "not-in-list", True
        )
        assert result is not None
        assert "not_in_emergency_list" in msg

    def test_heir_confirms_missing(self, store: SwitchStore):
        record = _advance_to_verifying(store, "u-heir-conf")
        result, msg = store.verify_heir(record.user_id, "heir-1", True)
        assert result is not None
        assert result.heir_confirmations.get("heir-1") is True
        assert "heir-1" in result.heir_confirmed_at

    def test_heir_reports_alive_resets(self, store: SwitchStore):
        record = _advance_to_verifying(store, "u-heir-alive")
        result, msg = store.verify_heir(record.user_id, "heir-1", False)
        assert result is not None
        assert result.state == SwitchState.ACTIVE
        assert "alive_report" in msg

    def test_engage_lawyer_marks_engaged(self, store: SwitchStore):
        record = _advance_to_verifying(store, "u-lawyer")
        result, msg = store.engage_lawyer(record.user_id)
        assert result is not None
        assert result.lawyer_engaged is True
        assert result.lawyer_engaged_at is not None
        assert msg == "lawyer_engaged"

    def test_engage_lawyer_without_config_rejected(self, store: SwitchStore):
        # 不配置 lawyer_user_id；直接推进状态机（不用 _advance_to_verifying 助手，
        # 因为助手会用 _default_config() 覆盖自定义配置）
        cfg = SwitchConfig(
            emergency_contacts=["c-1"],
            heir_user_ids=["h-1"],
            lawyer_user_id=None,
        )
        store.init_switch("u-no-lawyer", cfg)
        record = store.load("u-no-lawyer")
        assert record is not None
        # 推进到 VERIFYING（手动 tick，跳过助手）
        future = record.last_check_in + timedelta(days=100)
        store.tick("u-no-lawyer", now=future)
        final = store.load("u-no-lawyer")
        assert final is not None
        assert final.state == SwitchState.VERIFYING
        # 调 engage_lawyer 应被拒绝（无 lawyer 配置）
        result, msg = store.engage_lawyer("u-no-lawyer")
        assert result is not None
        assert "no_lawyer_configured" in msg


# ====================================================================
# 5. 冷静期 / 执行测试
# ====================================================================
class TestCooldownAndExecution:
    def _advance_to_confirmed(self, store: SwitchStore, user_id: str) -> SwitchRecord:
        """辅助：推进到 CONFIRMED 状态"""
        _advance_to_verifying(store, user_id)
        store.verify_emergency_contact(user_id, "contact-A", True)
        store.engage_lawyer(user_id)
        store.verify_heir(user_id, "heir-1", True)
        store.tick(user_id)
        final = store.load(user_id)
        assert final is not None
        assert final.state == SwitchState.CONFIRMED
        return final

    def test_cooldown_not_passed_blocks_execution(self, store: SwitchStore):
        record = self._advance_to_confirmed(store, "u-cool-block")
        # 冷静期未过（confirmed_at 刚刚）
        assert not store.is_cooldown_passed(record.user_id)
        remaining = store.cooldown_remaining_days(record.user_id)
        assert remaining >= 1
        # 执行应被拒绝
        executor = SwitchActionExecutor(store=store)
        with pytest.raises(RuntimeError, match="cooldown"):
            executor.execute_confirmed(record.user_id)

    def test_cooldown_passed_allows_execution(self, store: SwitchStore, tmp_path: Path, monkeypatch):
        # 用一个独立的 NotificationGuardrail 数据目录避免污染全局
        from deadman.notification.guardrail import NotificationGuardrail
        # 本测试关注冷静期机制，提高频率上限避免被 DAILY_LIMIT=1 阻塞
        # （deliver_ending_note + notify_heirs 会对同一收件人发送多次）
        monkeypatch.setattr(NotificationGuardrail, "DAILY_LIMIT", 10)
        monkeypatch.setattr(NotificationGuardrail, "WEEKLY_LIMIT", 30)
        monkeypatch.setattr(NotificationGuardrail, "MONTHLY_LIMIT", 80)
        # 关闭静默时段检查避免 flaky（22:00-08:00 UTC 跑测试会被拒绝）
        # 本测试关注冷静期机制，不验证 silent_hours 规则（该规则由其他测试覆盖）
        monkeypatch.setattr(NotificationGuardrail, "_in_silent_hours", lambda self, dt: False)
        # 同理关闭敏感日期封禁（避免清明/中元等公历近似日误命中）
        monkeypatch.setattr(NotificationGuardrail, "is_sensitive_date", lambda self, dt, user_id: False)
        record = self._advance_to_confirmed(store, "u-cool-pass")
        # 把 confirmed_at 回退到 8 天前（> 7 天冷静期）
        record.confirmed_at = datetime.utcnow() - timedelta(days=8)
        store.save(record)
        assert store.is_cooldown_passed(record.user_id)
        # 需要先 record_consent 让 NotificationGuardrail 通过
        guard_data = tmp_path / "notifications"
        guardrail = NotificationGuardrail(data_dir=guard_data)
        # 为每个收件人记录 opt-in consent（emergency_contacts + heirs）
        for rid in ["contact-A", "heir-1"]:
            guardrail.record_consent(rid, "同意 deadman_switch 通知", "deadman_switch")
        # 执行
        executor = SwitchActionExecutor(store=store, guardrail=guardrail)
        result = executor.execute_confirmed(record.user_id)
        assert "executed" in result
        # 至少一个动作执行成功
        assert len(result["executed"]) >= 1
        # 状态应转为 EXECUTED（所有动作成功）
        final = store.load(record.user_id)
        assert final is not None
        assert final.state == SwitchState.EXECUTED

    def test_checkin_during_cooldown_resets_active(self, store: SwitchStore):
        record = self._advance_to_confirmed(store, "u-cool-checkin")
        # 用户在冷静期内主动 check-in
        new_record = store.record_check_in(record.user_id, method="web")
        assert new_record is not None
        assert new_record.state == SwitchState.ACTIVE
        # 冷静期信息被清空
        assert new_record.confirmed_at is None

    def test_state_non_confirmed_rejects_execution(self, store: SwitchStore):
        # VERIFYING 状态调 execute_confirmed 应抛 RuntimeError
        _advance_to_verifying(store, "u-not-conf")
        executor = SwitchActionExecutor(store=store)
        with pytest.raises(RuntimeError, match="must be CONFIRMED"):
            executor.execute_confirmed("u-not-conf")


# ====================================================================
# 6. 取消测试
# ====================================================================
class TestCancel:
    def test_cancel_moves_to_cancelled_state(self, store: SwitchStore):
        _advance_to_verifying(store, "u-cancel")
        record = store.cancel("u-cancel", reason="user_changed_mind")
        assert record is not None
        assert record.state == SwitchState.CANCELLED
        # state_history 记录取消事件
        last = record.state_history[-1]
        assert last["state"] == "CANCELLED"
        assert last["reason"] == "user_changed_mind"

    def test_cancel_unknown_user_returns_none(self, store: SwitchStore):
        result = store.cancel("u-never-init")
        assert result is None


# ====================================================================
# 7. 加密存储测试
# ====================================================================
class TestEncryption:
    def test_encrypted_at_rest_no_plaintext(self, store: SwitchStore):
        cfg = _default_config()
        cfg.set_email("secret@example.com")
        cfg.set_phone("13900001111")
        store.init_switch("u-encrypt", cfg)
        # 读取加密文件
        switch_path = store._switch_path("u-encrypt")
        raw = switch_path.read_bytes()
        # 不应出现明文 PII
        assert b"secret@example.com" not in raw
        assert b"13900001111" not in raw
        # 也不应出现配置中的明文 user_id（实际 user_id 在 envelope 外，
        # 但 envelope 内是密文 ct）
        # 验证 envelope 是 v3 加密（AES-256-GCM，ct 含 GCM tag）
        envelope = json.loads(raw.decode("utf-8"))
        assert envelope.get("version") == 3
        assert "ct" in envelope
        assert "salt" in envelope
        assert envelope.get("alg") == "aes-256-gcm"

    def test_load_returns_record_after_save(self, store: SwitchStore):
        cfg = _default_config()
        original = store.init_switch("u-loadsave", cfg)
        # 重新加载
        loaded = store.load("u-loadsave")
        assert loaded is not None
        assert loaded.user_id == original.user_id
        assert loaded.state == original.state
        assert loaded.config.check_in_frequency_days == original.config.check_in_frequency_days
        assert loaded.config.emergency_contacts == original.config.emergency_contacts

    def test_delete_removes_files(self, store: SwitchStore):
        store.init_switch("u-delete", _default_config())
        switch_path = store._switch_path("u-delete")
        assert switch_path.exists()
        ok = store.delete("u-delete")
        assert ok
        assert not switch_path.exists()
        # 再删一次返回 False
        assert store.delete("u-delete") is False


# ====================================================================
# 8. CLI 命令测试
# ====================================================================
class TestCLICommands:
    def test_cli_switch_init_command(self, tmp_path: Path, capsys):
        from deadman._cli_extensions import phase15_switch
        from argparse import Namespace
        args = Namespace(
            user_id="u-cli-init",
            frequency=14,
            missed=2,
            window=5,
            cooldown=7,
            emergency_contact=["c-1", "c-2"],
            lawyer_id="lawyer-1",
            heir_id=["h-1"],
            email="cli@example.com",
            phone="13800001111",
            data_dir=str(tmp_path / "switch_cli"),
        )
        phase15_switch.cmd_switch_init(args)
        out = capsys.readouterr().out
        assert "已初始化" in out
        assert "u-cli-init" in out
        # 验证文件已生成
        from deadman.deadman_switch.store import SwitchStore as S
        s = S(data_dir=tmp_path / "switch_cli")
        record = s.load("u-cli-init")
        assert record is not None
        assert record.config.check_in_frequency_days == 14
        assert record.config.missed_threshold == 2
        assert "c-1" in record.config.emergency_contacts
        assert "lawyer-1" == record.config.lawyer_user_id
        # PII 已脱敏
        assert record.config.email_masked == "c***@example.com"
        assert record.config.phone_masked == "138****1111"

    def test_cli_switch_checkin_command(self, tmp_path: Path, capsys):
        from deadman._cli_extensions import phase15_switch
        from argparse import Namespace
        # 先 init
        init_args = Namespace(
            user_id="u-cli-checkin",
            frequency=30,
            missed=3,
            window=7,
            cooldown=7,
            emergency_contact=["c-1"],
            lawyer_id="lawyer-1",
            heir_id=["h-1"],
            email=None,
            phone=None,
            data_dir=str(tmp_path / "switch_cli2"),
        )
        phase15_switch.cmd_switch_init(init_args)
        capsys.readouterr()  # 清空
        # checkin
        checkin_args = Namespace(
            user_id="u-cli-checkin",
            method="cli",
            data_dir=str(tmp_path / "switch_cli2"),
        )
        phase15_switch.cmd_switch_checkin(checkin_args)
        out = capsys.readouterr().out
        assert "已记录 check-in" in out
        assert "ACTIVE" in out

    def test_cli_switch_status_command(self, tmp_path: Path, capsys):
        from deadman._cli_extensions import phase15_switch
        from argparse import Namespace
        # 先 init
        init_args = Namespace(
            user_id="u-cli-status",
            frequency=30, missed=3, window=7, cooldown=7,
            emergency_contact=["c-1"], lawyer_id=None, heir_id=["h-1"],
            email=None, phone=None,
            data_dir=str(tmp_path / "switch_cli3"),
        )
        phase15_switch.cmd_switch_init(init_args)
        capsys.readouterr()
        # status
        status_args = Namespace(
            user_id="u-cli-status",
            data_dir=str(tmp_path / "switch_cli3"),
        )
        phase15_switch.cmd_switch_status(status_args)
        out = capsys.readouterr().out
        assert "u-cli-status" in out
        assert "ACTIVE" in out


# ====================================================================
# 9. Web 端点测试
# ====================================================================
def _get_free_port() -> int:
    """获取一个可用端口"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_server(port: int, timeout: float = 5.0) -> bool:
    """等待服务器就绪（轮询 /api/health）"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/api/health")
            resp = conn.getresponse()
            conn.close()
            if resp.status == 200:
                return True
        except (ConnectionError, OSError):
            pass
        time.sleep(0.1)
    return False


class TestWebEndpoints:
    """验证 /api/switch/* 端点的认证保护"""

    def test_web_switch_init_without_token_returns_401(
        self, tmp_path: Path, monkeypatch
    ):
        # 把 SwitchStore 默认数据目录指向 tmp_path
        from deadman.deadman_switch import store as switch_store_mod
        monkeypatch.setattr(
            switch_store_mod.SwitchStore, "__init__",
            lambda self, data_dir=None: _orig_init(self, tmp_path / "switch_web"),
        )
        # 启服务器
        port = _get_free_port()
        from deadman.web.server import WebServer
        server = WebServer()
        thread = threading.Thread(
            target=server.run, args=("127.0.0.1", port), daemon=True
        )
        thread.start()
        try:
            assert _wait_for_server(port), "服务器未在超时内启动"
            # 无 token 调 /api/switch/init
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(
                "POST", "/api/switch/init",
                body=json.dumps({"frequency": 30}),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            assert resp.status == 401
            body = json.loads(resp.read().decode("utf-8"))
            assert "error" in body
            conn.close()
        finally:
            pass  # daemon 线程随进程退出

    def test_web_switch_init_with_token_returns_201(
        self, tmp_path: Path, monkeypatch
    ):
        # 让 SwitchStore 默认数据目录指向 tmp_path
        from deadman.deadman_switch import store as switch_store_mod
        monkeypatch.setattr(
            switch_store_mod.SwitchStore, "__init__",
            lambda self, data_dir=None: _orig_init(self, tmp_path / "switch_web2"),
        )
        # 让 UserStore 数据目录也指向 tmp_path
        from deadman.config import settings
        monkeypatch.setattr(settings, "auth_data_dir", tmp_path / "auth")
        monkeypatch.setattr(settings, "jwt_secret", "")
        monkeypatch.setattr(settings, "jwt_expiry_days", 7)
        monkeypatch.setattr(settings, "password_min_length", 8)

        port = _get_free_port()
        from deadman.web.server import WebServer
        server = WebServer()
        thread = threading.Thread(
            target=server.run, args=("127.0.0.1", port), daemon=True
        )
        thread.start()
        try:
            assert _wait_for_server(port)
            # 注册用户拿 token
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(
                "POST", "/api/auth/register",
                body=json.dumps({
                    "email": "switch-test@example.com",
                    "password": "password123",
                    "display_name": "SwitchTest",
                }),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            token = data["token"]
            conn.close()
            # 带 token 调 /api/switch/init
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(
                "POST", "/api/switch/init",
                body=json.dumps({
                    "frequency": 14,
                    "missed": 2,
                    "emergency_contacts": ["c-1"],
                    "heir_ids": ["h-1"],
                }),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )
            resp = conn.getresponse()
            assert resp.status == 201
            data = json.loads(resp.read().decode("utf-8"))
            assert data["state"] == "ACTIVE"
            assert data["config"]["check_in_frequency_days"] == 14
            conn.close()
            # 带 token 调 /api/switch/status
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(
                "GET", "/api/switch/status",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp = conn.getresponse()
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["state"] == "ACTIVE"
            conn.close()
        finally:
            pass


# 保留对原始 __init__ 的引用，用于 monkeypatch 中恢复
_orig_init = SwitchStore.__init__
