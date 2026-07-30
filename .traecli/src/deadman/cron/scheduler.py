"""Cron 调度器 - 借鉴 Hermes cron/scheduler.py 设计，严格遵守 notification-guardrails 第三章

设计要点（与 Hermes 的差异）：

1. **默认关闭**：CronScheduler 不自动启动；任务创建后 enabled=False，
   需用户在下一轮显式 confirm 后才置 enabled=True。
2. **双重确认**：propose_job 只入暂存（pending_confirmation=True），
   confirm_job 才真正激活。避免误操作 / 用户被动同意。
3. **任务粒度硬约束**（notification-guardrails.md §三.2）：
   - 单用户 ≤ 5 条
   - 最小触发间隔 ≥ 24 小时
   - 最长持续 30 天，到期自动失效
4. **失败不重试**（§三.4）：tick 中触发失败的 job 仅记日志、更新 last_fired，
   下次用户主动对话时由对话层报告"昨天的提醒发送失败"。
5. **不监控逝者数据源 / 不自动关怀 / 不自动转介**（§三.3）：本调度器仅
   触发"用户主动 opt-in 的提醒类任务"，不承载监控/转介能力。

依赖：NotificationGuardrail.can_send / sanitize_content / record_consent /
record_send。Phase 3 同步实现，本模块按"已存在"导入；若运行环境未就绪，
回落到内置 stub（can_send 永远返回 False，确保不会绕过护栏误推）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .expr import CronExpr

logger = logging.getLogger(__name__)


# ============================================================
# NotificationGuardrail 依赖（Phase 3 同步实现，此处防御性导入）
# ============================================================

try:
    # Phase 3 已就绪时直接用真实 guardrail
    from deadman.notification.guardrail import NotificationGuardrail  # type: ignore

    _GUARD_AVAILABLE = True
except ImportError:  # pragma: no cover - Phase 3 未落地时的降级路径
    logger.warning(
        "deadman.notification.guardrail 未就绪（Phase 3 未完成？）；"
        "CronScheduler 将使用降级 stub，can_send 永远拒绝推送。"
        "生产环境请确保 Phase 3 已上线。"
    )

    class NotificationGuardrail:  # type: ignore[no-redef]
        """降级 stub - Phase 3 未就绪时的占位实现

        保守策略：can_send 永远返回 False（不推送），
        确保未就绪环境下 cron 不会绕过护栏误推。测试应注入 mock guard。
        """

        def can_send(self, user_id: str, scheduled_time: datetime) -> tuple[bool, str]:
            return False, "NotificationGuardrail 未就绪（Phase 3 未完成）"

        def record_consent(self, user_id: str, content: str, scope: str) -> None:
            pass

        def sanitize_content(self, content: str) -> str:
            return content

        def record_send(self, user_id: str, content: str, channel: str) -> None:
            pass

        def record_unsubscribe(self, user_id: str, scope: str) -> None:
            pass

    _GUARD_AVAILABLE = False


# ============================================================
# 数据模型
# ============================================================


@dataclass
class CronJob:
    """Cron 任务定义 - 遵守 notification-guardrails.md 第三章"""

    job_id: str
    user_id: str
    schedule: str  # cron 表达式（5 字段：min hour dom mon dow）
    content: str  # 提醒内容（触发前会过 sanitize_content 脱敏）
    scope: str  # opt-in 范围标识（如 "cron"）
    created_at: datetime
    expires_at: datetime  # 最长 30 天后；过期自动失效
    last_fired: datetime | None = None
    enabled: bool = True
    pending_confirmation: bool = True  # 创建后需下一轮用户确认

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的 dict"""
        d = asdict(self)
        # datetime → ISO 字符串
        for k in ("created_at", "expires_at", "last_fired"):
            v = d.get(k)
            d[k] = v.isoformat() if isinstance(v, datetime) else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CronJob:
        """从 dict 反序列化"""

        def _parse_dt(v: Any) -> datetime | None:
            if v is None or v == "":
                return None
            if isinstance(v, datetime):
                return v
            try:
                return datetime.fromisoformat(str(v))
            except (ValueError, TypeError):
                return None

        return cls(
            job_id=str(d["job_id"]),
            user_id=str(d["user_id"]),
            schedule=str(d["schedule"]),
            content=str(d.get("content", "")),
            scope=str(d.get("scope", "cron")),
            created_at=_parse_dt(d["created_at"]) or datetime.now(),
            expires_at=_parse_dt(d["expires_at"]) or datetime.now(),
            last_fired=_parse_dt(d.get("last_fired")),
            enabled=bool(d.get("enabled", True)),
            pending_confirmation=bool(d.get("pending_confirmation", True)),
        )


# 触发处理器签名：接收 job 与脱敏后 content，返回 awaitable
FireHandler = Callable[[CronJob, str], Awaitable[None]]


# ============================================================
# CronScheduler
# ============================================================


class CronScheduler:
    """Cron 调度器 - 借鉴 Hermes cron/scheduler.py 但严格遵守 notification-guardrails

    与 Hermes 差异（见模块 docstring）：
    - 默认 enabled=false（Hermes 默认开启 heartbeat）
    - 任务创建需双重确认（用户提议 → 下一轮再次确认）
    - 任务上限 5 条/用户（Hermes 无上限）
    - 最小间隔 24h（Hermes 无限制）
    - 最长持续 30 天（Hermes 无限制）
    - 失败不重试（Hermes 有 retry）
    - 不支持 heartbeat / scale_to_zero（deadman 是轻量部署）
    """

    MAX_JOBS_PER_USER = 5
    MIN_INTERVAL_HOURS = 24
    MAX_DURATION_DAYS = 30

    def __init__(
        self,
        data_dir: Path | None = None,
        guard: NotificationGuardrail | None = None,
        fire_handler: FireHandler | None = None,
    ):
        """构造调度器

        Args:
            data_dir: 数据目录，默认 ~/.deadman/cron/。jobs.json 存于此
            guard: NotificationGuardrail 实例。None 时用默认实例
            fire_handler: 触发处理器，签名 (job, sanitized_content) -> awaitable。
                None 时用默认 no-op 处理器（仅记日志）。
                生产环境注入"调 orchestration.graph 或 send_proactive"的回调。
        """
        # 默认 ~/.deadman/cron/
        if data_dir is None:
            data_dir = Path.home() / ".deadman" / "cron"
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_file = self.data_dir / "jobs.json"

        self.guard = guard if guard is not None else NotificationGuardrail()
        self._fire_handler: FireHandler = fire_handler or _default_fire_handler

    # ============================================================
    # 任务生命周期：propose → confirm → tick → cancel
    # ============================================================

    async def propose_job(self, user_id: str, schedule: str, content: str) -> dict[str, Any]:
        """提议创建任务（pending_confirmation=True，enabled=False）

        第一阶段：仅校验 cron 表达式合法 + 内容非空，入暂存。
        不校验上限/间隔/持续时长（那些在 confirm 阶段才硬约束，
        避免用户改主意时已被占名额）。

        Returns:
            {"job_id":..., "needs_confirmation": True, "message": "请在下一轮确认..."}
        """
        if not user_id or not user_id.strip():
            raise ValueError("user_id 不能为空")
        if not content or not content.strip():
            raise ValueError("content 不能为空")

        # 校验 cron 表达式合法（不强校验间隔，间隔在 confirm 时再硬约束）
        ok, reason = self._validate_schedule_syntax(schedule)
        if not ok:
            raise ValueError(f"cron 表达式非法: {reason}")

        now = datetime.now()
        # expires_at 默认按最大值（30 天）设置；confirm 时再硬校验
        expires_at = now + timedelta(days=self.MAX_DURATION_DAYS)

        job = CronJob(
            job_id=uuid.uuid4().hex[:12],
            user_id=user_id,
            schedule=schedule.strip(),
            content=content,
            scope="cron",
            created_at=now,
            expires_at=expires_at,
            last_fired=None,
            enabled=False,  # propose 阶段未激活
            pending_confirmation=True,
        )

        jobs = self._load_jobs()
        jobs.append(job)
        self._save_jobs(jobs)

        logger.info(
            "Cron 任务已提议 user=%s job=%s schedule=%s（待确认）",
            user_id,
            job.job_id,
            job.schedule,
        )
        return {
            "job_id": job.job_id,
            "needs_confirmation": True,
            "message": (
                "已记录提议。按身后事场景的隐私约束，"
                "请在下一轮再次确认创建此提醒（deadman cron-confirm --job-id "
                f"{job.job_id}）。未确认前不会触发。"
            ),
        }

    async def confirm_job(self, user_id: str, job_id: str) -> dict[str, Any]:
        """用户在下一轮确认创建任务

        校验链：
        1. job 存在 + 属于该 user_id + pending_confirmation=True
        2. 当前用户 enabled 任务数 < MAX_JOBS_PER_USER
        3. expires_at <= now + MAX_DURATION_DAYS
        4. schedule 间隔 >= MIN_INTERVAL_HOURS
        5. 调 guard.record_consent 记录 opt-in
        6. 置 pending_confirmation=False, enabled=True
        7. 持久化
        """
        jobs = self._load_jobs()
        target: CronJob | None = None
        for j in jobs:
            if j.job_id == job_id and j.user_id == user_id:
                target = j
                break

        if target is None:
            raise ValueError(f"未找到待确认任务 job_id={job_id} user_id={user_id}")

        if not target.pending_confirmation:
            raise ValueError(f"任务 {job_id} 已确认或非待确认状态")

        # 上限校验：已激活 + 待确认都算占名额，但只统计 enabled=True 的活跃任务
        # （notification-guardrails.md §三.2 "单用户 Cron 任务数上限：5 条"）
        active_count = sum(
            1 for j in jobs if j.user_id == user_id and j.enabled and not j.pending_confirmation
        )
        if active_count >= self.MAX_JOBS_PER_USER:
            raise ValueError(
                f"用户 {user_id} 已有 {active_count} 条活跃任务，超过上限 {self.MAX_JOBS_PER_USER}"
            )

        # 持续时长校验：expires_at 不能晚于 now + MAX_DURATION_DAYS
        now = datetime.now()
        max_expires = now + timedelta(days=self.MAX_DURATION_DAYS)
        if target.expires_at > max_expires:
            # 自动收敛到最大值（容错：propose 后过了几天才 confirm）
            target.expires_at = max_expires
            logger.info("任务 %s expires_at 收敛到最大值 %s", job_id, max_expires)

        # 间隔校验
        ok, reason = self._validate_schedule(target.schedule)
        if not ok:
            raise ValueError(f"cron 表达式间隔不足: {reason}")

        # 记录 opt-in（NotificationGuardrail 落盘到 consent.json）
        try:
            self.guard.record_consent(user_id, target.content, target.scope)
        except Exception as e:
            # record_consent 失败不阻断确认（避免护栏未就绪时无法创建），
            # 但记日志
            logger.warning("record_consent 失败（不阻断）: %s", e)

        # 激活
        target.pending_confirmation = False
        target.enabled = True
        self._save_jobs(jobs)

        logger.info(
            "Cron 任务已确认激活 user=%s job=%s schedule=%s expires=%s",
            user_id,
            job_id,
            target.schedule,
            target.expires_at,
        )
        return {
            "job_id": job_id,
            "confirmed": True,
            "schedule": target.schedule,
            "expires_at": target.expires_at.isoformat(),
            "message": "任务已激活，将在调度器主循环中按时触发。",
        }

    async def cancel_job(self, user_id: str, job_id: str) -> bool:
        """取消任务（删除）"""
        jobs = self._load_jobs()
        before = len(jobs)
        jobs = [j for j in jobs if not (j.job_id == job_id and j.user_id == user_id)]
        if len(jobs) == before:
            logger.info("取消失败：未找到任务 job=%s user=%s", job_id, user_id)
            return False
        self._save_jobs(jobs)
        logger.info("Cron 任务已取消 user=%s job=%s", user_id, job_id)
        return True

    def list_jobs(self, user_id: str) -> list[CronJob]:
        """列出用户的所有任务（含待确认、已激活、已过期）"""
        return [j for j in self._load_jobs() if j.user_id == user_id]

    # ============================================================
    # 调度核心：tick + run_forever
    # ============================================================

    async def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """调度器一次 tick - 检查所有到期任务

        对每个任务依次检查：
        1. pending_confirmation=True → 跳过（未确认）
        2. enabled=False → 跳过
        3. expires_at < now → 跳过（自动失效）
        4. guard.can_send(user_id, now) 拒绝 → 跳过
        5. cron.matches(now) 不匹配 → 跳过（未到点）
        6. last_fired 与 now 同分钟 → 跳过（本分钟已触发，防重复）
        7. guard.sanitize_content(content) 为空 → 跳过（含禁用词完全不推送）
        8. 调 fire_handler 触发
        9. 失败不重试，记日志
        10. attempted=True 时（成功或失败都算）更新 last_fired，避免本分钟重试

        Returns:
            [{"job_id":..., "user_id":..., "fired": bool, "reason": str}]
        """
        if now is None:
            now = datetime.now()

        jobs = self._load_jobs()
        results: list[dict[str, Any]] = []
        dirty = False  # 是否需要回写 jobs.json

        for job in jobs:
            try:
                fired, attempted, reason = await self._try_fire(job, now)
            except Exception as e:
                # 单个任务异常不影响其他任务
                logger.exception("tick 处理任务异常 job=%s: %s", job.job_id, e)
                fired, attempted, reason = False, False, f"tick 异常: {e}"

            # 成功或失败都更新 last_fired（失败不重试，但本分钟内不再触发）；
            # 未实际触发（待确认/未到点/被护栏拦截等）则不动 last_fired
            if attempted:
                job.last_fired = now
                dirty = True

            results.append(
                {
                    "job_id": job.job_id,
                    "user_id": job.user_id,
                    "fired": fired,
                    "reason": reason,
                }
            )

        if dirty:
            self._save_jobs(jobs)

        return results

    async def _try_fire(self, job: CronJob, now: datetime) -> tuple[bool, bool, str]:
        """单任务触发判定 + 执行

        Returns:
            (fired, attempted, reason)
            - fired: 是否触发成功
            - attempted: 是否实际调用了 fire_handler（成功/失败都 True，
              用于 tick 决定是否更新 last_fired 防本分钟重试）
            - reason: 跳过/触发原因（用于日志与返回结果）
        """
        # 1. 待确认 → 跳过
        if job.pending_confirmation:
            return False, False, "pending_confirmation"
        # 2. 未启用 → 跳过
        if not job.enabled:
            return False, False, "disabled"
        # 3. 已过期 → 跳过
        if job.expires_at < now:
            return False, False, "expired"
        # 4. 护栏拦截 → 跳过（含静默时段/频率上限/敏感日期等）
        try:
            allowed, reason = self.guard.can_send(job.user_id, now)
        except Exception as e:
            logger.warning("guard.can_send 异常 job=%s: %s", job.job_id, e)
            return False, False, f"guard_error: {e}"
        if not allowed:
            return False, False, f"guard_blocked: {reason}"

        # 5. cron 是否匹配当前分钟
        try:
            expr = CronExpr(job.schedule)
        except ValueError as e:
            logger.error("任务 %s cron 表达式非法: %s", job.job_id, e)
            return False, False, f"invalid_schedule: {e}"

        if not expr.matches(now):
            return False, False, "not_matched"

        # 6. 本分钟内已触发 → 跳过（防重复）
        if job.last_fired is not None:
            last = job.last_fired.replace(second=0, microsecond=0)
            cur = now.replace(second=0, microsecond=0)
            if last == cur:
                return False, False, "already_fired_this_minute"

        # 7. 内容脱敏（含禁用词 → 空串则跳过）
        try:
            sanitized = self.guard.sanitize_content(job.content)
        except Exception as e:
            logger.warning("guard.sanitize_content 异常 job=%s: %s", job.job_id, e)
            return False, False, f"sanitize_error: {e}"

        if not sanitized or not sanitized.strip():
            return False, False, "sanitized_empty"

        # 8. 触发（失败不重试）
        try:
            await self._fire_handler(job, sanitized)
            # 9. 记录已发送（频率统计）
            try:
                self.guard.record_send(job.user_id, sanitized, channel="cron")
            except Exception as e:
                logger.warning("guard.record_send 异常 job=%s: %s", job.job_id, e)
            logger.info("Cron 任务触发成功 user=%s job=%s", job.user_id, job.job_id)
            return True, True, "fired"
        except Exception as e:
            # 失败不重试，仅记日志；attempted=True 让 tick 更新 last_fired
            # 避免本分钟内重试（notification-guardrails.md §三.4）
            logger.error(
                "Cron 任务触发失败 user=%s job=%s error=%s",
                job.user_id,
                job.job_id,
                e,
                exc_info=True,
            )
            return False, True, f"fire_failed: {e}"

    async def run_forever(self, interval_seconds: int = 60) -> None:
        """主循环 - 每 interval 秒 tick 一次

        Ctrl+C 优雅退出（KeyboardInterrupt 捕获后返回）。
        """
        logger.info(
            "CronScheduler 主循环启动 interval=%ss jobs_file=%s",
            interval_seconds,
            self.jobs_file,
        )
        try:
            while True:
                try:
                    await self.tick()
                except Exception as e:
                    # tick 本身不应抛（_try_fire 已兜底），但兜一层防止主循环挂掉
                    logger.exception("tick 异常（主循环继续）: %s", e)
                await asyncio.sleep(interval_seconds)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("CronScheduler 主循环收到退出信号，正在停止...")

    # ============================================================
    # 校验工具
    # ============================================================

    def _validate_schedule(self, schedule: str) -> tuple[bool, str]:
        """校验 cron 表达式 + 间隔 >= MIN_INTERVAL_HOURS

        Returns:
            (是否合法, 原因/错误信息)
        """
        ok, reason = self._validate_schedule_syntax(schedule)
        if not ok:
            return False, reason

        try:
            expr = CronExpr(schedule)
        except ValueError as e:
            return False, str(e)

        interval = expr.min_interval_hours()
        if interval < self.MIN_INTERVAL_HOURS:
            return False, (
                f"最小触发间隔 {interval:.2f}h < {self.MIN_INTERVAL_HOURS}h "
                "（身后事场景不得高频推送）"
            )
        return True, f"ok (min_interval={interval:.2f}h)"

    @staticmethod
    def _validate_schedule_syntax(schedule: str) -> tuple[bool, str]:
        """仅校验 cron 表达式语法（不校验间隔）"""
        if not schedule or not schedule.strip():
            return False, "schedule 为空"
        try:
            CronExpr(schedule)
            return True, "ok"
        except ValueError as e:
            return False, str(e)

    # ============================================================
    # 持久化
    # ============================================================

    def _load_jobs(self) -> list[CronJob]:
        """从 jobs.json 加载任务列表

        文件不存在/损坏时返回空列表（韧性优先，不抛异常打断主循环）。
        """
        if not self.jobs_file.exists():
            return []
        try:
            with open(self.jobs_file, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.error(
                "jobs.json 读取失败 (%s)：将视为空列表。请检查文件 %s",
                e,
                self.jobs_file,
            )
            return []

        if not isinstance(data, dict):
            logger.error("jobs.json 结构非法（非 dict），视为空列表")
            return []

        raw_jobs = data.get("jobs", [])
        if not isinstance(raw_jobs, list):
            logger.error("jobs.json 'jobs' 字段非 list，视为空列表")
            return []

        jobs: list[CronJob] = []
        for r in raw_jobs:
            if not isinstance(r, dict):
                continue
            try:
                jobs.append(CronJob.from_dict(r))
            except (KeyError, ValueError, TypeError) as e:
                logger.warning("跳过损坏的 job 记录: %s", e)
        return jobs

    def _save_jobs(self, jobs: list[CronJob]) -> None:
        """原子写入 jobs.json

        先写临时文件 → fsync → os.replace，确保写入原子性。
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "jobs": [j.to_dict() for j in jobs],
            "updated_at": datetime.now().isoformat(),
        }

        # 原子写入：临时文件 + os.replace
        fd, tmp_path = tempfile.mkstemp(dir=str(self.data_dir), suffix=".tmp", prefix=".jobs_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.jobs_file)
            # 仅 owner 可读写（敏感：含 user_id / 提醒内容）
            with contextlib.suppress(OSError):
                os.chmod(self.jobs_file, 0o600)
        except BaseException:
            # 写入失败时清理临时文件
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise


# ============================================================
# 默认触发处理器（生产环境应注入真实回调）
# ============================================================


async def _default_fire_handler(job: CronJob, sanitized_content: str) -> None:
    """默认触发处理器 - 仅记日志

    生产环境应在构造 CronScheduler 时注入 fire_handler，例如：

        async def handler(job, content):
            from deadman.orchestration.graph import build_main_graph, create_initial_state
            graph = build_main_graph()
            state = create_initial_state(user_input=content)
            await graph.ainvoke(state)

        scheduler = CronScheduler(fire_handler=handler)

    本默认实现不调用任何外部模块，避免在 Phase 3 / 编排层未就绪时崩。
    """
    logger.info(
        "[默认 fire_handler] job=%s user=%s content=%r（未注入真实处理器）",
        job.job_id,
        job.user_id,
        sanitized_content[:80],
    )
