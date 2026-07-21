"""测试 deadman.notification.guardrail - 主动通知护栏

覆盖 notification-guardrails.md 第七章第 3 节要求的 14 个测试场景：
    - 静默时段拦截（22:00-08:00）
    - 频率超限拦截（单日 1 / 单周 3 / 单月 8）
    - 敏感日期封禁（清明 / 中元）
    - opt-in 缺失拦截 / opt-in 存在允许
    - 内容脱敏正确性（替换 + 完全不推送）
    - 退订立即生效
    - 脆弱期静默（72h / R3-14d / 高情绪-7d）

测试隔离：每个测试用 tmp_path fixture 独立数据目录，互不污染。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from deadman.notification.guardrail import NotificationGuardrail


# =====================================================================
# 辅助：构造带 opt-in 的护栏
# =====================================================================


def _make_guard_with_consent(tmp_path: Path, user_id: str = "u1") -> NotificationGuardrail:
    """构造一个带 opt-in 记录的 guard"""
    g = NotificationGuardrail(data_dir=tmp_path)
    g.record_consent(user_id, "是的，请提醒我", "reminder:test")
    return g


# =====================================================================
# 1. 静默时段拦截
# =====================================================================


class TestSilentHours:
    """22:00-08:00 静默时段拦截"""

    def test_silent_hours_blocked(self, tmp_path: Path):
        # 23:30 应被静默时段拦截
        guard = _make_guard_with_consent(tmp_path)
        night = datetime(2026, 7, 21, 23, 30)
        allowed, reason = guard.can_send("u1", night)
        assert allowed is False
        assert reason == "silent_hours"

    def test_silent_hours_morning_blocked(self, tmp_path: Path):
        # 06:00 也应被拦截
        guard = _make_guard_with_consent(tmp_path)
        early_morning = datetime(2026, 7, 21, 6, 0)
        allowed, reason = guard.can_send("u1", early_morning)
        assert allowed is False
        assert reason == "silent_hours"

    def test_work_hours_allowed(self, tmp_path: Path):
        # 10:00 在工作时段，应允许（其他条件都满足）
        guard = _make_guard_with_consent(tmp_path)
        noon = datetime(2026, 7, 21, 10, 0)
        allowed, reason = guard.can_send("u1", noon)
        assert allowed is True, f"应允许推送，但被拦截: {reason}"


# =====================================================================
# 2. 频率上限 - 单日 1 条
# =====================================================================


class TestFrequencyDailyLimit:
    """单日 1 条超限拦截"""

    def test_frequency_daily_limit(self, tmp_path: Path):
        guard = _make_guard_with_consent(tmp_path)
        now = datetime(2026, 7, 21, 10, 0)
        # 已发送 1 条（显式注入 sent_at，避免 datetime.now() 与 now 时序错位 flaky）
        guard.record_send("u1", "已发1", "telegram", sent_at=now - timedelta(hours=1))
        allowed, reason = guard.can_send("u1", now)
        assert allowed is False
        assert reason == "daily_limit_exceeded"


# =====================================================================
# 3. 频率上限 - 单周 3 条
# =====================================================================


class TestFrequencyWeeklyLimit:
    """单周 3 条超限拦截"""

    def test_frequency_weekly_limit(self, tmp_path: Path):
        guard = _make_guard_with_consent(tmp_path)
        now = datetime(2026, 7, 21, 10, 0)
        # 过去 6 天每天发一条（共 6 条，超 3 条上限）
        for i in range(6):
            ts = now - timedelta(days=i + 1)
            # 直接构造 sent_log
            sent_log = guard._read_json(guard.sent_log_file, {})
            sent_log.setdefault("u1", []).append(
                {"content": f"历史 {i}", "channel": "telegram", "sent_at": ts.isoformat()}
            )
            guard._write_json(guard.sent_log_file, sent_log)
        allowed, reason = guard.can_send("u1", now)
        assert allowed is False
        assert reason == "weekly_limit_exceeded"


# =====================================================================
# 4. 频率上限 - 单月 8 条
# =====================================================================


class TestFrequencyMonthlyLimit:
    """单月 8 条超限拦截"""

    def test_frequency_monthly_limit(self, tmp_path: Path):
        guard = _make_guard_with_consent(tmp_path)
        now = datetime(2026, 7, 21, 10, 0)
        # 过去 8 天每天发一条（共 8 条，超月上限 8）
        # 但注意：8 条也触发了 weekly（>=3），但 weekly 先于 monthly 检查
        # 所以需要把发送分散在 30 天窗口内但不超过 7 天窗口 3 条
        # 这里改用：4 条在 6 天内（触发 weekly），1 条在 8 天前，1 条在 15 天前，
        # 1 条在 22 天前，1 条在 29 天前 = 共 8 条
        sent_log = {"u1": []}
        for delta_days in [1, 2, 3, 8, 15, 22, 29, 30]:
            ts = now - timedelta(days=delta_days)
            sent_log["u1"].append(
                {"content": f"历史 {delta_days}d", "channel": "telegram", "sent_at": ts.isoformat()}
            )
        guard._write_json(guard.sent_log_file, sent_log)
        # weekly 已发 3 条（1d, 2d, 3d 内），monthly 已发 8 条
        # weekly 检查在 monthly 之前，所以这里期望 weekly 拦截
        # 但我们的目标测试 monthly，故改用 7 天内只发 2 条，30 天内发 8 条
        sent_log = {"u1": []}
        for delta_days in [1, 2, 8, 9, 15, 22, 26, 29]:
            ts = now - timedelta(days=delta_days)
            sent_log["u1"].append(
                {"content": f"历史 {delta_days}d", "channel": "telegram", "sent_at": ts.isoformat()}
            )
        guard._write_json(guard.sent_log_file, sent_log)
        allowed, reason = guard.can_send("u1", now)
        # 7 天内 2 条（未超 weekly），30 天内 8 条（超 monthly=8）
        assert allowed is False
        assert reason == "monthly_limit_exceeded"


# =====================================================================
# 5. 敏感日期 - 清明
# =====================================================================


class TestSensitiveDateQingming:
    """清明 4-5 ±3 天封禁"""

    def test_sensitive_date_qingming(self, tmp_path: Path):
        guard = _make_guard_with_consent(tmp_path)
        qingming = datetime(2026, 4, 5, 10, 0)
        allowed, reason = guard.can_send("u1", qingming)
        assert allowed is False
        assert reason == "sensitive_date"

    def test_sensitive_date_qingming_plus_2d(self, tmp_path: Path):
        # 清明 +2 天仍在 ±3 天窗口内
        guard = _make_guard_with_consent(tmp_path)
        qingming_plus = datetime(2026, 4, 7, 10, 0)
        allowed, reason = guard.can_send("u1", qingming_plus)
        assert allowed is False
        assert reason == "sensitive_date"


# =====================================================================
# 6. 敏感日期 - 中元
# =====================================================================


class TestSensitiveDateZhongyuan:
    """中元 8-15 ±3 天封禁"""

    def test_sensitive_date_zhongyuan(self, tmp_path: Path):
        guard = _make_guard_with_consent(tmp_path)
        zhongyuan = datetime(2026, 8, 15, 10, 0)
        allowed, reason = guard.can_send("u1", zhongyuan)
        assert allowed is False
        assert reason == "sensitive_date"


# =====================================================================
# 7. opt-in 缺失拦截
# =====================================================================


class TestOptinMissing:
    """无 opt-in 应拦截"""

    def test_optin_missing_blocked(self, tmp_path: Path):
        guard = NotificationGuardrail(data_dir=tmp_path)
        # 不记录 opt-in
        allowed, reason = guard.can_send("u-no-optin", datetime(2026, 7, 21, 10, 0))
        assert allowed is False
        assert reason == "optin_missing"


# =====================================================================
# 8. opt-in 存在允许
# =====================================================================


class TestOptinPresent:
    """有 opt-in 且其他条件满足时允许"""

    def test_optin_present_allowed(self, tmp_path: Path):
        guard = _make_guard_with_consent(tmp_path, user_id="u-ok")
        allowed, reason = guard.can_send("u-ok", datetime(2026, 7, 21, 10, 0))
        assert allowed is True
        assert reason == ""


# =====================================================================
# 9. 内容脱敏 - 替换禁用词
# =====================================================================


class TestSanitizeReplaces:
    """禁用词应被替换为中性词"""

    def test_sanitize_replaces_forbidden_words(self, tmp_path: Path):
        guard = NotificationGuardrail(data_dir=tmp_path)
        # 输入含 "死亡"（不含更长词），应替换为 "待办事项"
        result1 = guard.sanitize_content("关于死亡这件事")
        assert "死亡" not in result1
        assert "待办事项" in result1

        # 输入含 "死亡证明"（长词优先），应替换为 "资料准备"
        result2 = guard.sanitize_content("提醒：今天该去办死亡证明了")
        assert "死亡" not in result2, "脱敏后不应出现'死亡'"
        assert "死亡证明" not in result2, "脱敏后不应出现'死亡证明'"
        assert "资料准备" in result2

    def test_sanitize_replaces_multiple(self, tmp_path: Path):
        guard = NotificationGuardrail(data_dir=tmp_path)
        # 多个禁用词同时替换
        result = guard.sanitize_content("遗体火化安葬")
        assert "遗体" not in result
        assert "火化" not in result
        assert "安葬" not in result
        assert "后续事宜" in result

    def test_sanitize_long_word_priority(self, tmp_path: Path):
        # "死亡证明" 应被替换为 "资料准备"，而不是 "死亡" 先替换为 "待办事项"
        guard = NotificationGuardrail(data_dir=tmp_path)
        result = guard.sanitize_content("死亡证明")
        assert result == "资料准备"


# =====================================================================
# 10. 内容脱敏 - 完全不推送关键词
# =====================================================================


class TestSanitizeBlocks:
    """含'忌日/自杀'等关键词返回空串"""

    def test_sanitize_blocks_anniversary_keyword(self, tmp_path: Path):
        guard = NotificationGuardrail(data_dir=tmp_path)
        assert guard.sanitize_content("今天是逝者的忌日") == ""
        assert guard.sanitize_content("自杀相关内容") == ""
        assert guard.sanitize_content("他杀相关") == ""
        assert guard.sanitize_content("非正常死亡") == ""
        assert guard.sanitize_content("周年纪念") == ""


# =====================================================================
# 11. 退订立即生效
# =====================================================================


class TestUnsubscribeImmediate:
    """退订后立即拦截"""

    def test_unsubscribe_immediate(self, tmp_path: Path):
        guard = _make_guard_with_consent(tmp_path)
        # 退订
        guard.record_unsubscribe("u1", scope="all")
        # 立即检查应被拦截
        allowed, reason = guard.can_send("u1", datetime(2026, 7, 21, 10, 0))
        assert allowed is False
        assert reason == "user_unsubscribed"


# =====================================================================
# 12. 72 小时静默期
# =====================================================================


class Test72hSilenceAfterSession:
    """最后会话后 72 小时内拦截"""

    def test_72h_silence_after_session(self, tmp_path: Path):
        guard = _make_guard_with_consent(tmp_path)
        # 记录会话结束（默认 ended_at = now，立即触发 72h 检查）
        guard.record_session_end(
            "u1",
            safety_triggered=False,
            emotion_intensity="低",
            involved_sensitive_death=False,
        )
        # 立即推送应被拦截（在 72h 静默期内）
        # 注意：record_session_end 用 datetime.now()，can_send 也用 datetime.now()
        # 但测试需要可控时间，故直接构造 last_session 时间戳
        now = datetime(2026, 7, 21, 10, 0)
        last_session = guard._read_json(guard.last_session_file, {})
        last_session["u1"]["ended_at"] = (now - timedelta(hours=12)).isoformat()
        guard._write_json(guard.last_session_file, last_session)

        allowed, reason = guard.can_send("u1", now)
        assert allowed is False
        assert reason == "within_72h_after_session"

    def test_72h_silence_expired(self, tmp_path: Path):
        # 72 小时之后应允许（其他条件都满足）
        guard = _make_guard_with_consent(tmp_path)
        now = datetime(2026, 7, 21, 10, 0)
        guard.record_session_end(
            "u1",
            safety_triggered=False,
            emotion_intensity="低",
            involved_sensitive_death=False,
        )
        last_session = guard._read_json(guard.last_session_file, {})
        # 73 小时前
        last_session["u1"]["ended_at"] = (now - timedelta(hours=73)).isoformat()
        guard._write_json(guard.last_session_file, last_session)

        allowed, reason = guard.can_send("u1", now)
        assert allowed is True, f"72h 后应允许，但被拦截: {reason}"


# =====================================================================
# 13. R3 触发后 14 天静默
# =====================================================================


class TestR3_14dSilence:
    """R3 触发后 14 天内拦截"""

    def test_r3_14d_silence(self, tmp_path: Path):
        guard = _make_guard_with_consent(tmp_path)
        now = datetime(2026, 7, 21, 10, 0)
        # R3 触发的会话，结束时间设在 73 小时前（绕过 72h 静默）
        guard.record_session_end(
            "u1",
            safety_triggered=True,
            emotion_intensity="中",
            involved_sensitive_death=False,
        )
        last_session = guard._read_json(guard.last_session_file, {})
        last_session["u1"]["ended_at"] = (now - timedelta(days=5)).isoformat()
        guard._write_json(guard.last_session_file, last_session)

        allowed, reason = guard.can_send("u1", now)
        assert allowed is False
        assert reason == "within_14d_after_r3"


# =====================================================================
# 14. 高情绪强度后 7 天静默
# =====================================================================


class TestHighEmotion7dSilence:
    """高情绪强度后 7 天内拦截"""

    def test_high_emotion_7d_silence(self, tmp_path: Path):
        guard = _make_guard_with_consent(tmp_path)
        now = datetime(2026, 7, 21, 10, 0)
        # 高情绪会话，结束时间设在 73 小时前（绕过 72h 静默）
        guard.record_session_end(
            "u1",
            safety_triggered=False,
            emotion_intensity="高",
            involved_sensitive_death=False,
        )
        last_session = guard._read_json(guard.last_session_file, {})
        last_session["u1"]["ended_at"] = (now - timedelta(days=3)).isoformat()
        guard._write_json(guard.last_session_file, last_session)

        allowed, reason = guard.can_send("u1", now)
        assert allowed is False
        assert reason == "within_7d_after_high_emotion"
