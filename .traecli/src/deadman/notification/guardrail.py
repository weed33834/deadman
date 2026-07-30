"""NotificationGuardrail - 主动通知护栏（L4 硬边界）

实现 `.traecli/rules/notification-guardrails.md` 第七章要求：
    - can_send(user_id, scheduled_time) -> (allowed, reason)
    - record_consent / record_unsubscribe / record_send / record_session_end
    - sanitize_content / is_sensitive_date

数据存储在 `~/.deadman/notifications/` 下：
    - consent.json        : opt-in 记录（用户 ID + 内容 + 时间戳 + scope）
    - unsubscribes.json   : 退订记录（用户 ID + 时间 + scope）
    - sent_log.json       : 已发送记录（用户 ID + 内容 + 时间 + 渠道）
    - last_session.json   : 每用户最后会话快照（用于脆弱期判定）

设计原则：
    - 默认静默：任何检查失败均返回 (False, reason)，宁可错杀不主动打扰
    - 单点入口：所有主动推送代码路径必须先调 can_send()
    - 韧性优先：JSON 读写失败不抛异常，降级为"拒绝推送"
    - 不依赖外部库：仅用 stdlib（json/datetime/pathlib）
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# 敏感日期封禁表（公历近似，每年按月-日匹配）
# 清明 4-5、中元 8-15、寒衣 11-1、重阳 10-（农历九月初九，公历约 10 月）
# 此处用公历近似，前后 ±3 天封禁；用户生日 / 逝者生日也从 last_session 读
SENSITIVE_FIXED_DATES: list[tuple[int, int, str]] = [
    (4, 5, "清明"),
    (8, 15, "中元"),
    (11, 1, "寒衣"),
    (10, 1, "重阳"),  # 公历近似（实为农历九月初九，简化处理）
]


class NotificationGuardrail:
    """主动通知护栏 - 实现 notification-guardrails.md L4 规则。

    所有主动推送代码路径必须先调 can_send()，返回 (False, reason) 时禁止推送。
    """

    # 频率上限（notification-guardrails.md 第二章约束 4）
    DAILY_LIMIT = 1
    WEEKLY_LIMIT = 3
    MONTHLY_LIMIT = 8

    # 静默时段（22:00-08:00 当地时区，简化用 scheduled_time 时区）
    SILENT_HOURS = (22, 8)

    # 脆弱期静默时长（第一章约束 7-9）
    POST_SESSION_SILENCE_HOURS = 72  # 最后会话后 72 小时
    R3_SILENCE_DAYS = 14  # R3 触发后 14 天
    HIGH_EMOTION_SILENCE_DAYS = 7  # 高情绪强度后 7 天
    SENSITIVE_DEATH_SILENCE_DAYS = 30  # 自杀/他杀/非正常死亡会话后 30 天

    # 禁用词替换表（第一章约束 5）
    # "忌日 / 周年 / 自杀 / 他杀 / 非正常死亡" 完全不推送（sanitize 返回空串）
    FORBIDDEN_WORDS: dict[str, str] = {
        "死亡": "待办事项",
        "死亡证明": "资料准备",
        "死者": "当事人",
        "丧事": "仪式安排",
        "丧礼": "仪式安排",
        "殡仪": "仪式安排",
        "遗体": "后续事宜",
        "火化": "后续事宜",
        "安葬": "后续事宜",
        "遗产": "财产事务",
        "遗嘱": "财产事务",
        "继承": "财产事务",
        "销户": "户籍事务",
        "注销户口": "户籍事务",
        "逝者": "当事人",
    }

    # 完全不推送的关键词（含此类词返回空串标记不推送）
    BLOCK_KEYWORDS: tuple[str, ...] = (
        "忌日",
        "周年",
        "自杀",
        "他杀",
        "非正常死亡",
    )

    def __init__(self, data_dir: Path | None = None) -> None:
        """初始化护栏。

        Args:
            data_dir: 数据目录，默认 ~/.deadman/notifications/
        """
        if data_dir is None:
            data_dir = Path.home() / ".deadman" / "notifications"
        self.data_dir: Path = data_dir
        self.consent_file: Path = data_dir / "consent.json"
        self.unsubscribes_file: Path = data_dir / "unsubscribes.json"
        self.sent_log_file: Path = data_dir / "sent_log.json"
        self.last_session_file: Path = data_dir / "last_session.json"

        # 确保目录存在（韧性：失败仅 warning，不抛异常）
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("NotificationGuardrail 创建数据目录失败 %s: %s", self.data_dir, exc)

    # ==================================================================
    # JSON 文件读写（原子 + 韧性）
    # ==================================================================

    def _read_json(self, path: Path, default: Any) -> Any:
        """读取 JSON 文件，失败返回默认值（不抛异常）"""
        try:
            if not path.exists():
                return default
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取 %s 失败: %s", path, exc)
            return default

    def _write_json(self, path: Path, data: Any) -> None:
        """原子写入 JSON 文件，失败仅 warning（不抛异常）"""
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("写入 %s 失败: %s", path, exc)

    # ==================================================================
    # can_send - 推送前置检查
    # ==================================================================

    def can_send(self, user_id: str, scheduled_time: datetime) -> tuple[bool, str]:
        """推送前置检查，返回 (是否允许, 拒绝原因)。

        拒绝原因用于日志，不告知用户。

        检查顺序（任一失败即拒绝）：
          1. 用户是否退订
          2. 静默时段（22:00-08:00）
          3. 敏感日期封禁
          4. 最后会话后 72 小时静默期
          5. R3 触发后 14 天静默期
          6. 高情绪强度后 7 天静默期
          7. 自杀/他杀/非正常死亡会话后 30 天静默期
          8. 频率上限（单日 1 / 单周 3 / 单月 8）
          9. opt-in 是否存在且有效
        """
        # 1. 退订检查
        if self._is_unsubscribed(user_id):
            return False, "user_unsubscribed"

        # 2. 静默时段
        if self._in_silent_hours(scheduled_time):
            return False, "silent_hours"

        # 3. 敏感日期封禁
        if self.is_sensitive_date(scheduled_time, user_id):
            return False, "sensitive_date"

        # 4-7. 脆弱期检查
        fragility_reason = self._check_fragility(user_id, scheduled_time)
        if fragility_reason:
            return False, fragility_reason

        # 8. 频率上限
        freq_reason = self._check_frequency(user_id, scheduled_time)
        if freq_reason:
            return False, freq_reason

        # 9. opt-in 检查
        if not self._has_valid_consent(user_id):
            return False, "optin_missing"

        return True, ""

    # ==================================================================
    # 检查项 1: 退订
    # ==================================================================

    def _is_unsubscribed(self, user_id: str) -> bool:
        """检查用户是否已退订（任何 scope=all 视为全部退订）"""
        unsubscribes = self._read_json(self.unsubscribes_file, {})
        user_records = unsubscribes.get(user_id, [])
        if not user_records:
            return False
        # 任意一条 scope=all 的退订即视为全退订
        return any(record.get("scope", "all") == "all" for record in user_records)

    # ==================================================================
    # 检查项 2: 静默时段
    # ==================================================================

    def _in_silent_hours(self, dt: datetime) -> bool:
        """检查是否在 22:00-08:00 静默时段"""
        start_hour, end_hour = self.SILENT_HOURS
        hour = dt.hour
        # 22:00-23:59 + 00:00-07:59
        return bool(hour >= start_hour or hour < end_hour)

    # ==================================================================
    # 检查项 3: 敏感日期
    # ==================================================================

    def is_sensitive_date(self, dt: datetime, user_id: str) -> bool:
        """检查是否敏感日期（清明/中元/寒衣/重阳 ±3 天，用户生日 ±3 天）"""
        month = dt.month
        day = dt.day

        # 固定敏感日期 ±3 天
        for sm, sd, _name in SENSITIVE_FIXED_DATES:
            for delta in range(-3, 4):
                try:
                    candidate = dt.replace(month=sm, day=sd) + timedelta(days=delta)
                except ValueError:
                    continue
                if candidate.month == month and candidate.day == day:
                    return True

        # 用户生日 / 逝者生日 ±3 天（从 last_session.json 读）
        last_session = self._read_json(self.last_session_file, {})
        user_session = last_session.get(user_id, {}) if isinstance(last_session, dict) else {}
        for key in ("user_birthday", "deceased_birthday"):
            birthday_str = user_session.get(key)
            if not birthday_str:
                continue
            try:
                # 期望 "MM-DD" 或 "YYYY-MM-DD" 格式
                if "-" in birthday_str and len(birthday_str.split("-")) >= 2:
                    parts = birthday_str.split("-")
                    bm = int(parts[-2])
                    bd = int(parts[-1])
                    for delta in range(-3, 4):
                        try:
                            candidate = dt.replace(month=bm, day=bd) + timedelta(days=delta)
                        except ValueError:
                            continue
                        if candidate.month == month and candidate.day == day:
                            return True
            except (ValueError, TypeError):
                continue

        return False

    # ==================================================================
    # 检查项 4-7: 脆弱期
    # ==================================================================

    def _check_fragility(self, user_id: str, now: datetime) -> str:
        """检查脆弱期静默状态。返回非空字符串表示拒绝原因。"""
        last_session = self._read_json(self.last_session_file, {})
        if not isinstance(last_session, dict):
            return ""
        user_session = last_session.get(user_id, {})
        if not user_session:
            return ""

        last_end_str = user_session.get("ended_at")
        if not last_end_str:
            return ""

        try:
            last_end = datetime.fromisoformat(last_end_str)
        except ValueError:
            return ""

        # 4. 最后会话后 72 小时静默期
        if now - last_end < timedelta(hours=self.POST_SESSION_SILENCE_HOURS):
            return "within_72h_after_session"

        # 7. 自杀/他杀/非正常死亡会话后 30 天
        if user_session.get("involved_sensitive_death"):
            if now - last_end < timedelta(days=self.SENSITIVE_DEATH_SILENCE_DAYS):
                return "within_30d_after_sensitive_death"

        # 5. R3 触发后 14 天
        if user_session.get("safety_triggered"):
            if now - last_end < timedelta(days=self.R3_SILENCE_DAYS):
                return "within_14d_after_r3"

        # 6. 高情绪强度后 7 天
        emotion = user_session.get("emotion_intensity", "")
        if emotion == "高":
            if now - last_end < timedelta(days=self.HIGH_EMOTION_SILENCE_DAYS):
                return "within_7d_after_high_emotion"

        return ""

    # ==================================================================
    # 检查项 8: 频率
    # ==================================================================

    def _check_frequency(self, user_id: str, now: datetime) -> str:
        """检查频率上限。返回非空字符串表示拒绝原因。"""
        sent_log = self._read_json(self.sent_log_file, {})
        if not isinstance(sent_log, dict):
            return ""
        user_records = sent_log.get(user_id, [])
        if not user_records:
            return ""

        # 按时间窗口统计
        daily_count = 0
        weekly_count = 0
        monthly_count = 0
        for record in user_records:
            ts_str = record.get("sent_at", "")
            try:
                ts = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                continue
            if (now - ts).days < 1 and (now - ts).total_seconds() >= 0:
                # 当天 24 小时内
                daily_count += 1
            if (now - ts).days < 7:
                weekly_count += 1
            if (now - ts).days < 30:
                monthly_count += 1

        if daily_count >= self.DAILY_LIMIT:
            return "daily_limit_exceeded"
        if weekly_count >= self.WEEKLY_LIMIT:
            return "weekly_limit_exceeded"
        if monthly_count >= self.MONTHLY_LIMIT:
            return "monthly_limit_exceeded"
        return ""

    # ==================================================================
    # 检查项 9: opt-in
    # ==================================================================

    def _has_valid_consent(self, user_id: str) -> bool:
        """检查用户是否有有效 opt-in 记录"""
        consents = self._read_json(self.consent_file, {})
        if not isinstance(consents, dict):
            return False
        user_records = consents.get(user_id, [])
        return bool(user_records)

    # ==================================================================
    # record_* 方法
    # ==================================================================

    def record_consent(self, user_id: str, content: str, scope: str) -> None:
        """记录 opt-in 到 consent.json，含时间戳与原文。

        Args:
            user_id: 用户 ID
            content: 用户同意的原文（必须真实，不得伪造）
            scope: 同意范围（如 "reminder:2026-07-22T09:00:00"）
        """
        consents = self._read_json(self.consent_file, {})
        if not isinstance(consents, dict):
            consents = {}
        user_records = consents.setdefault(user_id, [])
        user_records.append(
            {
                "content": content,
                "scope": scope,
                "recorded_at": datetime.now().isoformat(),
            }
        )
        self._write_json(self.consent_file, consents)

    def record_unsubscribe(self, user_id: str, scope: str = "all") -> None:
        """记录退订，立即生效。

        Args:
            user_id: 用户 ID
            scope: 退订范围，"all" 表示全部退订
        """
        unsubscribes = self._read_json(self.unsubscribes_file, {})
        if not isinstance(unsubscribes, dict):
            unsubscribes = {}
        user_records = unsubscribes.setdefault(user_id, [])
        user_records.append(
            {
                "scope": scope,
                "recorded_at": datetime.now().isoformat(),
            }
        )
        self._write_json(self.unsubscribes_file, unsubscribes)

    def record_send(
        self,
        user_id: str,
        content: str,
        channel: str,
        sent_at: datetime | None = None,
    ) -> None:
        """记录已发送，用于频率统计。

        Args:
            user_id: 用户 ID
            content: 已发送内容（脱敏后）
            channel: 渠道（telegram/email/webhook）
            sent_at: 发送时间（默认 datetime.now()）；测试可显式注入，
                     避免 record_send/can_send 时序错位导致的 flaky
        """
        sent_log = self._read_json(self.sent_log_file, {})
        if not isinstance(sent_log, dict):
            sent_log = {}
        user_records = sent_log.setdefault(user_id, [])
        user_records.append(
            {
                "content": content,
                "channel": channel,
                "sent_at": (sent_at or datetime.now()).isoformat(),
            }
        )
        self._write_json(self.sent_log_file, sent_log)

    def record_session_end(
        self,
        user_id: str,
        safety_triggered: bool,
        emotion_intensity: str,
        involved_sensitive_death: bool,
    ) -> None:
        """会话结束时记录，供下次 can_send 判定脆弱期。

        Args:
            user_id: 用户 ID
            safety_triggered: 是否触发 safety-protocol.md L0/R3
            emotion_intensity: 情绪强度（"高"/"中"/"低"）
            involved_sensitive_death: 是否涉及自杀/他杀/非正常死亡
        """
        last_session = self._read_json(self.last_session_file, {})
        if not isinstance(last_session, dict):
            last_session = {}
        last_session[user_id] = {
            "ended_at": datetime.now().isoformat(),
            "safety_triggered": bool(safety_triggered),
            "emotion_intensity": emotion_intensity,
            "involved_sensitive_death": bool(involved_sensitive_death),
        }
        self._write_json(self.last_session_file, last_session)

    # ==================================================================
    # sanitize_content - 内容脱敏
    # ==================================================================

    def sanitize_content(self, content: str) -> str:
        """内容脱敏，替换禁用词。

        含 '忌日/周年/自杀/他杀/非正常死亡' 关键词时返回空串标记不推送。

        Args:
            content: 原始推送内容

        Returns:
            脱敏后内容，或空串（表示完全不推送）
        """
        if not content:
            return ""

        # 命中"完全不推送"关键词 → 返回空串
        for keyword in self.BLOCK_KEYWORDS:
            if keyword in content:
                return ""

        # 按字典 key 长度降序替换（先替换长词，避免"死亡证明"被"死亡"先匹配）
        for word in sorted(self.FORBIDDEN_WORDS.keys(), key=len, reverse=True):
            if word in content:
                content = content.replace(word, self.FORBIDDEN_WORDS[word])
        return content
