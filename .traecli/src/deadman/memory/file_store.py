"""文件持久化记忆层 - 借鉴 Hermes Agent MIT 设计的 MEMORY.md/USER.md 格式。

当 Graphiti 与 LightRAG 都不可用时，作为纯文件降级后端：
    - USER.md：用户画像（YAML frontmatter + markdown body）
    - MEMORY.md：长期事实记忆（按章节组织）
    - EPISODES.md：情景记忆摘要（每条一行）

安全约束（与 rules/ 14 个规则文件协同）：
    - 调用 `deadman.memory.manager.sanitize_before_store` 做 PII 脱敏，
      覆盖 identifier/name/phone/address/account_number 字段（compliance-framework 数据安全底线）
    - 原子写入：先写 .tmp 再 os.replace，防止崩溃导致文件损坏
    - 文件不存在时返回空结构，绝不抛异常（韧性优先）

存储根目录：`~/.deadman/memory/`（与 SOUL.md 同级，user 级覆盖层）

P2.5 Memory Snapshot:
    - export_snapshot() 把 USER.md + MEMORY.md + EPISODES.md + REFLEXION.json
      打包为 JSON + gzip(可选 AES-256-GCM 加密)
    - import_snapshot(data) 解包并替换当前文件
    - feature flag DEADMAN_MEMORY_SNAPSHOT_ENABLED=0 默认关闭
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .manager import sanitize_before_store
from .semantic import UserProfile

logger = logging.getLogger(__name__)

# =====================================================================
# P2.5 feature flag - 默认关闭
# =====================================================================
MEMORY_SNAPSHOT_ENABLED: bool = os.environ.get(
    "DEADMAN_MEMORY_SNAPSHOT_ENABLED", "0"
).lower() in ("1", "true", "yes", "on")

# 可选 AES-256-GCM 加密依赖
try:  # pragma: no cover - 可选依赖
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
    _HAS_CRYPTO = True
except Exception:  # pragma: no cover
    AESGCM = None  # type: ignore
    _HAS_CRYPTO = False

# Snapshot 文件魔数 + 版本(用于 import 时校验)
_SNAPSHOT_MAGIC: bytes = b"DMSP"  # DeadMan Snapshot
_SNAPSHOT_VERSION: int = 1
# Snapshot 加密标识位(0=明文 gzip, 1=AES-256-GCM)
_SNAPSHOT_FLAG_PLAIN: int = 0
_SNAPSHOT_FLAG_AESGCM: int = 1

# 默认存储根目录：~/.deadman/memory/
# 与 SOUL.md（~/.deadman/SOUL.md）同级，属于用户级数据
DEFAULT_MEMORY_DIR = Path.home() / ".deadman" / "memory"

# MEMORY.md 章节固定顺序（与 deadman 4 层记忆结构对齐）
MEMORY_SECTIONS: tuple[str, ...] = (
    "用户事实",      # 用户基本事实（与 UserProfile 对齐）
    "逝者信息",      # deceased_info
    "流程进度",      # procedural 当前阶段与已完成阶段
    "待澄清矛盾",    # semantic.pending_contradictions
)


def _atomic_write(path: Path, content: str) -> None:
    """原子写入：先写 .tmp 再 os.replace。

    os.replace 在 POSIX 上是原子的；Windows 上也保证原子语义。
    若中途崩溃，原文件保持不变（.tmp 残留会被下次写入覆盖）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    # 写 .tmp 文件
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    # 原子替换
    os.replace(tmp_path, path)


def _profile_to_dict(profile: UserProfile) -> dict[str, Any]:
    """把 UserProfile dataclass 转为可序列化 dict（保留所有字段）"""
    return {
        "user_id": profile.user_id,
        "name": profile.name,
        "relationship_to_deceased": profile.relationship_to_deceased,
        "location": profile.location,
        "deceased_info": profile.deceased_info,
        "family_structure": profile.family_structure,
        "assets_summary": profile.assets_summary,
        "current_stage": profile.current_stage,
        "completed_stages": list(profile.completed_stages) if profile.completed_stages else [],
        "pending_tasks": list(profile.pending_tasks) if profile.pending_tasks else [],
    }


def _dict_to_profile(data: dict[str, Any]) -> UserProfile:
    """从 dict 反序列化为 UserProfile（容错：缺失字段走默认）"""
    return UserProfile(
        user_id=str(data.get("user_id", "")),
        name=data.get("name"),
        relationship_to_deceased=data.get("relationship_to_deceased"),
        location=data.get("location"),
        deceased_info=data.get("deceased_info"),
        family_structure=data.get("family_structure"),
        assets_summary=data.get("assets_summary"),
        current_stage=data.get("current_stage"),
        completed_stages=list(data.get("completed_stages") or []),
        pending_tasks=list(data.get("pending_tasks") or []),
    )


class FileMemoryStore:
    """纯文件持久化记忆层 - Graphiti/LightRAG 不可用时的降级后端。

    设计原则：
        - 无外部依赖（仅 stdlib + pyyaml）
        - 写入失败不抛异常（韧性优先），仅记录 warning
        - 文件不存在时返回空结构，不抛异常
        - 所有 PII 字段经 sanitize_before_store 脱敏后再写文件

    线程安全：单实例内的 read/write 各自原子，但跨实例并发写同一文件
    可能竞争（os.replace 仍是原子的，最坏情况是后写覆盖先写）。
    """

    def __init__(self, memory_dir: Path | None = None) -> None:
        """初始化文件记忆存储。

        Args:
            memory_dir: 存储目录，默认 ~/.deadman/memory/
        """
        self.memory_dir: Path = memory_dir if memory_dir is not None else DEFAULT_MEMORY_DIR
        self.user_file: Path = self.memory_dir / "USER.md"
        self.memory_file: Path = self.memory_dir / "MEMORY.md"
        self.episodes_file: Path = self.memory_dir / "EPISODES.md"
        # P0.3 Reflexion 跨会话持久化 - JSON 格式便于结构化读写 + TTL
        self.reflexion_file: Path = self.memory_dir / "REFLEXION.json"

    # ==================================================================
    # USER.md - 用户画像
    # ==================================================================

    def save_profile(self, user_id: str, profile: UserProfile) -> None:
        """把 UserProfile 序列化为 USER.md（YAML frontmatter + markdown body）

        写入前对所有 PII 字段（identifier/name/phone/address/account_number）
        做 sanitize_before_store 掩码处理，确保文件中不出现明文 PII。

        Args:
            user_id: 用户 ID（写入 frontmatter 便于反查）
            profile: UserProfile 实例
        """
        # 转为 dict
        raw = _profile_to_dict(profile)
        # 强制 user_id 一致
        raw["user_id"] = user_id
        # PII 脱敏（递归处理嵌套 dict）
        safe = sanitize_before_store(raw)

        # YAML frontmatter（用户画像字段）+ markdown body（人类可读视图）
        frontmatter = yaml.safe_dump(
            safe, allow_unicode=True, sort_keys=False, default_flow_style=False
        )
        body_lines: list[str] = [
            f"# 用户画像：{safe.get('user_id', user_id)}",
            "",
            f"- **姓名（脱敏）**：{safe.get('name') or '（未提供）'}",
            f"- **与逝者关系**：{safe.get('relationship_to_deceased') or '（未提供）'}",
            f"- **所在地**：{safe.get('location') or '（未提供）'}",
            f"- **逝者信息**：{safe.get('deceased_info') or '（未提供）'}",
            f"- **家庭结构**：{safe.get('family_structure') or '（未提供）'}",
            f"- **资产概况**：{safe.get('assets_summary') or '（未提供）'}",
            f"- **当前阶段**：第 {safe.get('current_stage') or '?'} 阶段",
            f"- **已完成阶段**：{safe.get('completed_stages') or []}",
            f"- **待办事项**：{safe.get('pending_tasks') or []}",
        ]
        content = f"---\n{frontmatter}---\n\n" + "\n".join(body_lines) + "\n"

        try:
            _atomic_write(self.user_file, content)
        except Exception as e:
            logger.warning("FileMemoryStore.save_profile 写入失败: %s", e)

    def load_profile(self, user_id: str) -> UserProfile | None:
        """从 USER.md 反序列化 UserProfile。

        文件不存在 / 解析失败 / user_id 不匹配时返回 None，绝不抛异常。

        Args:
            user_id: 期望的用户 ID（与 frontmatter 中的 user_id 比对）

        Returns:
            UserProfile 或 None
        """
        if not self.user_file.exists():
            return None
        try:
            text = self.user_file.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("FileMemoryStore.load_profile 读取失败: %s", e)
            return None

        # 解析 YAML frontmatter
        if not text.startswith("---"):
            return None
        parts = text.split("---", 2)
        if len(parts) < 3:
            return None
        try:
            data = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError as e:
            logger.warning("FileMemoryStore.load_profile YAML 解析失败: %s", e)
            return None
        if not isinstance(data, dict):
            return None

        # user_id 校验：文件中的 user_id 必须与请求的 user_id 匹配
        # （单用户场景，未来扩展多用户可按 user_id 拆分子目录）
        stored_uid = str(data.get("user_id", ""))
        if stored_uid and user_id and stored_uid != user_id:
            return None

        try:
            return _dict_to_profile(data)
        except Exception as e:
            logger.warning("FileMemoryStore.load_profile 反序列化失败: %s", e)
            return None

    # ==================================================================
    # EPISODES.md - 情景记忆摘要
    # ==================================================================

    def append_episode(
        self,
        episode_id: str,
        summary: str,
        timestamp: datetime | None = None,
        importance: float | None = None,
        pinned: bool = False,
    ) -> None:
        """追加一条情景记忆摘要到 EPISODES.md。

        每行格式（P0.5 扩展，向后兼容旧格式）：
            `[YYYY-MM-DD HH:MM] session=xxx importance=0.75 pinned=true summary=xxx`
            旧格式无 importance/pinned 字段,解析时 importance=None/pinned=False。

        P0.5 新增字段:
            importance: 0.0-1.0 重要性评分(LLM 评估),< 0.3 归档不召回,> 0.8 提升召回优先级
            pinned: True 表示"创伤"记忆(L0 安全触发 / 法律纠纷),永不压缩,永远保留原文

        不包含换行的 summary 会被强制单行化（换行替换为空格）。

        Args:
            episode_id: 片段 ID（作为 session 字段写入）
            summary: 片段摘要文本
            timestamp: 时间戳，默认 now()
            importance: 重要性评分 0.0-1.0(可选,向后兼容)
            pinned: 是否标记为"创伤"记忆永不压缩(默认 False)
        """
        if timestamp is None:
            timestamp = datetime.now()
        ts_str = timestamp.strftime("%Y-%m-%d %H:%M")
        # summary 强制单行，避免破坏 EPISODES.md 的行格式
        summary_oneline = (summary or "").replace("\n", " ").replace("\r", " ").strip()
        # 截断超长摘要，避免单行过长
        if len(summary_oneline) > 500:
            summary_oneline = summary_oneline[:500] + "..."

        # P0.5: 在 session= 与 summary= 之间插入可选元数据字段
        meta_parts: list[str] = []
        if importance is not None:
            # 归一化到 [0, 1]
            imp = max(0.0, min(1.0, float(importance)))
            meta_parts.append(f"importance={imp:.2f}")
        if pinned:
            meta_parts.append("pinned=true")

        meta_str = (" " + " ".join(meta_parts)) if meta_parts else ""
        line = f"[{ts_str}] session={episode_id}{meta_str} summary={summary_oneline}\n"

        try:
            self.episodes_file.parent.mkdir(parents=True, exist_ok=True)
            # 追加模式不需要原子写入（单次 append 是 POSIX 原子的，长度 < PIPE_BUF）
            with open(self.episodes_file, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            logger.warning("FileMemoryStore.append_episode 写入失败: %s", e)

    def load_episodes(self, limit: int = 20) -> list[dict[str, Any]]:
        """读取最近 N 条情景记忆。

        解析 EPISODES.md 每行为 dict：{timestamp, session, summary}
        文件不存在时返回空 list。

        Args:
            limit: 返回最近 N 条（按文件顺序倒序取）

        Returns:
            list[dict]，每条含 {timestamp, session, summary, raw}
        """
        if not self.episodes_file.exists():
            return []
        try:
            text = self.episodes_file.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("FileMemoryStore.load_episodes 读取失败: %s", e)
            return []

        results: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or not line.startswith("["):
                continue
            parsed = self._parse_episode_line(line)
            if parsed is not None:
                results.append(parsed)

        # 取最近 limit 条（倒序后取前 limit，再反转回正序）
        recent = results[-limit:] if limit > 0 else list(results)
        return recent

    @staticmethod
    def _parse_episode_line(line: str) -> dict[str, Any] | None:
        """解析单行 episode(向后兼容 P0.5 新字段)。

        支持格式:
            旧: `[ts] session=xxx summary=yyy`
            新: `[ts] session=xxx importance=0.75 pinned=true summary=yyy`

        新字段缺失时 importance=None, pinned=False(旧 episode 兼容)。
        """
        # 提取时间戳
        if "]" not in line:
            return None
        ts_end = line.index("]")
        ts_str = line[1:ts_end]  # 去掉首 [ 和末 ]
        rest = line[ts_end + 1 :].strip()

        # 解析 session= 与 summary=(中间可能有 importance=/pinned= 元数据)
        session = ""
        summary = ""
        importance: float | None = None
        pinned = False

        # session= 是第一个字段
        if rest.startswith("session="):
            rest = rest[len("session=") :]
            # summary= 是终止分隔符
            if " summary=" in rest:
                meta_part, _, sum_part = rest.partition(" summary=")
                summary = sum_part.strip()
                # P0.5: meta_part 形如 "sess-1 importance=0.75 pinned=true"
                # 第一个 token 是 session 值,其余是元数据
                tokens = meta_part.split()
                if tokens:
                    session = tokens[0]
                for token in tokens[1:]:
                    if token.startswith("importance="):
                        with contextlib.suppress(ValueError, IndexError):
                            importance = float(token.split("=", 1)[1])
                    elif token.startswith("pinned="):
                        pinned = token.split("=", 1)[1].strip().lower() in (
                            "true", "1", "yes",
                        )
            else:
                session = rest.strip()

        # 时间戳解析（失败保留原字符串）
        timestamp: datetime | None
        try:
            timestamp = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
        except ValueError:
            timestamp = None

        return {
            "timestamp": timestamp,
            "timestamp_str": ts_str,
            "session": session,
            "summary": summary,
            "importance": importance,
            "pinned": pinned,
            "raw": line,
        }

    # ==================================================================
    # REFLEXION.json - P0.3 Reflexion 跨会话反思记忆
    # ==================================================================

    # 默认 TTL:90 天(法规/政策可能已变,旧反思应失效)
    REFLEXION_TTL_DAYS: int = 90
    # 历史记录上限(每个 failure_type 最多保留 N 条历史,LRU 截断)
    REFLEXION_HISTORY_LIMIT: int = 20

    def load_reflexion(self) -> dict[str, Any]:
        """加载完整的 REFLEXION.json

        结构:
            {
                "agents": {
                    "death_aftercare": {
                        "failure_patterns": {failure_type: {count, first_seen, last_seen}},
                        "successful_adjustments": {
                            failure_type: {
                                strategy, success_count, total_count,
                                last_recorded, history: [{strategy, success, ts}]
                            }
                        }
                    }
                },
                "shared_patterns": {failure_type: {count, best_strategy}},
                "version": 1
            }

        文件不存在/解析失败时返回空结构,绝不抛异常。
        """
        if not self.reflexion_file.exists():
            return self._empty_reflexion_dict()
        try:
            text = self.reflexion_file.read_text(encoding="utf-8")
            data = json.loads(text) if text.strip() else {}
            if not isinstance(data, dict):
                return self._empty_reflexion_dict()
            # 兼容老格式:补全字段
            data.setdefault("agents", {})
            data.setdefault("shared_patterns", {})
            data.setdefault("version", 1)
            return data
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("FileMemoryStore.load_reflexion 解析失败: %s", e)
            return self._empty_reflexion_dict()

    def save_reflexion(self, data: dict[str, Any]) -> None:
        """保存 REFLEXION.json(原子写入)"""
        try:
            data.setdefault("version", 1)
            data["last_updated"] = datetime.now().isoformat()
            content = json.dumps(data, ensure_ascii=False, indent=2, default=str)
            _atomic_write(self.reflexion_file, content)
        except Exception as e:
            logger.warning("FileMemoryStore.save_reflexion 写入失败: %s", e)

    @staticmethod
    def _empty_reflexion_dict() -> dict[str, Any]:
        return {"agents": {}, "shared_patterns": {}, "version": 1}

    def get_agent_reflexion(self, agent_name: str) -> dict[str, Any]:
        """获取指定 agent 的反思记忆

        Returns:
            {
                "failure_patterns": {failure_type: {count, first_seen, last_seen}},
                "successful_adjustments": {failure_type: {strategy, success_rate, ...}},
                "shared_patterns": {...},  # 跨 agent 共享(只读)
            }
        """
        data = self.load_reflexion()
        agent_data = data.get("agents", {}).get(agent_name, {})
        # 合并跨 agent 共享模式(只读)
        shared = data.get("shared_patterns", {})

        # TTL 过滤:清除 90 天前的 failure_pattern
        agent_data = self._apply_ttl_filter(agent_data)
        return {
            "failure_patterns": agent_data.get("failure_patterns", {}),
            "successful_adjustments": agent_data.get("successful_adjustments", {}),
            "shared_patterns": shared,
        }

    def record_agent_adjustment(
        self,
        agent_name: str,
        failure_type: str,
        adjustment_strategy: str,
        success: bool,
    ) -> None:
        """记录单次调整结果(成功或失败)

        更新:
            - failure_patterns[failure_type].count += 1
            - successful_adjustments[failure_type].history 追加本次
            - 若 success 则 success_count += 1
            - shared_patterns[failure_type].count += 1(跨 agent 共享)
            - 自动更新 best_strategy(基于 success_rate)
        """
        try:
            data = self.load_reflexion()
            agents = data.setdefault("agents", {})
            agent_data = agents.setdefault(
                agent_name,
                {"failure_patterns": {}, "successful_adjustments": {}},
            )

            now_iso = datetime.now().isoformat()

            # 1. 更新 failure_patterns
            fp = agent_data["failure_patterns"]
            pattern = fp.get(
                failure_type,
                {"count": 0, "first_seen": now_iso, "last_seen": now_iso},
            )
            pattern["count"] = int(pattern.get("count", 0)) + 1
            pattern["last_seen"] = now_iso
            fp[failure_type] = pattern

            # 2. 更新 successful_adjustments
            sa = agent_data["successful_adjustments"]
            adj = sa.get(
                failure_type,
                {
                    "strategy": adjustment_strategy,
                    "success_count": 0,
                    "total_count": 0,
                    "last_recorded": now_iso,
                    "history": [],
                },
            )
            adj["total_count"] = int(adj.get("total_count", 0)) + 1
            if success:
                adj["success_count"] = int(adj.get("success_count", 0)) + 1
            adj["strategy"] = adjustment_strategy  # 用最新策略覆盖
            adj["last_recorded"] = now_iso

            # 历史记录追加 + LRU 截断
            history = adj.get("history", [])
            history.append(
                {
                    "strategy": adjustment_strategy,
                    "success": success,
                    "timestamp": now_iso,
                }
            )
            if len(history) > self.REFLEXION_HISTORY_LIMIT:
                history = history[-self.REFLEXION_HISTORY_LIMIT :]
            adj["history"] = history

            # 计算 success_rate
            adj["success_rate"] = (
                adj["success_count"] / adj["total_count"]
                if adj["total_count"] > 0
                else 0.0
            )
            sa[failure_type] = adj

            # 3. 更新 shared_patterns(跨 agent 共享统计)
            # 维护 per-agent 的最终 success_rate,选最高者作 best_strategy
            # (修复 v1.1 bug:旧逻辑用单次 success_rate 与存储的 best_success_rate 比较,
            #  在 agent_a 第 1 次 success=True 时就把 best 设为策略A,
            #  即使 agent_b 最终整体 5/5=1.0 也不会更新,因 1.0 不大于已存的 1.0)
            shared = data.setdefault("shared_patterns", {})
            shared_pattern = shared.get(
                failure_type,
                {
                    "count": 0,
                    "best_strategy": "",
                    "best_success_rate": 0.0,
                    "agent_rates": {},  # per-agent: {agent_name: {strategy, rate, total, success}}
                },
            )
            shared_pattern["count"] = int(shared_pattern.get("count", 0)) + 1

            # 记录每个 agent 的最新 success_rate(整体,非单次)
            agent_rates = shared_pattern.setdefault("agent_rates", {})
            agent_rates[agent_name] = {
                "strategy": adjustment_strategy,
                "rate": adj["success_rate"],
                "total": adj["total_count"],
                "success": adj["success_count"],
            }

            # 重算 best:遍历所有 agent,选 rate 最高者
            # 同分时按 total 降序(样本量大优先)、agent_name 字典序(稳定)排序
            candidates: list[tuple[float, int, str, dict[str, Any]]] = []
            for a_name, a_info in agent_rates.items():
                a_total = int(a_info.get("total", 0))
                if a_total <= 0:
                    continue
                a_rate = float(a_info.get("rate", 0.0))
                candidates.append((a_rate, a_total, a_name, a_info))

            # 排序:rate 降序 → total 降序 → agent_name 升序(稳定 tiebreak)
            candidates.sort(key=lambda x: (-x[0], -x[1], x[2]))

            if candidates:
                best_rate, _best_total, _best_agent, best_info = candidates[0]
                shared_pattern["best_strategy"] = best_info["strategy"]
                shared_pattern["best_success_rate"] = best_rate

            shared[failure_type] = shared_pattern

            self.save_reflexion(data)
        except Exception as e:
            logger.warning("FileMemoryStore.record_agent_adjustment 失败: %s", e)

    def _apply_ttl_filter(self, agent_data: dict[str, Any]) -> dict[str, Any]:
        """过滤掉 TTL 过期的反思记忆(默认 90 天)

        清除 last_seen 早于 TTL 天数的 failure_pattern 与 successful_adjustment。
        """
        try:
            ttl_seconds = self.REFLEXION_TTL_DAYS * 86400
            now = datetime.now()
            fp = agent_data.get("failure_patterns", {})
            sa = agent_data.get("successful_adjustments", {})

            # 过滤 failure_patterns
            for ftype in list(fp.keys()):
                last_seen_str = fp[ftype].get("last_seen", "")
                if self._is_expired(last_seen_str, now, ttl_seconds):
                    del fp[ftype]
                    # 同步删除 successful_adjustments
                    sa.pop(ftype, None)

            return agent_data
        except Exception:
            return agent_data

    @staticmethod
    def _is_expired(timestamp_str: str, now: datetime, ttl_seconds: int) -> bool:
        """判断时间戳是否超过 TTL"""
        if not timestamp_str:
            return False  # 无时间戳视为未过期(保守)
        try:
            ts = datetime.fromisoformat(timestamp_str)
            return (now - ts).total_seconds() > ttl_seconds
        except (ValueError, TypeError):
            return False

    def get_reflexion_summary(self) -> dict[str, Any]:
        """导出反思记忆摘要(供 CLI 查看)

        Returns:
            {
                "total_agents": int,
                "total_patterns": int,
                "total_adjustments": int,
                "agents": {agent_name: {patterns: int, adjustments: int, avg_success_rate: float}},
                "shared_patterns": {...},
            }
        """
        data = self.load_reflexion()
        agents = data.get("agents", {})
        shared = data.get("shared_patterns", {})

        agent_summaries: dict[str, Any] = {}
        total_patterns = 0
        total_adjustments = 0
        for agent_name, agent_data in agents.items():
            fp_count = len(agent_data.get("failure_patterns", {}))
            sa = agent_data.get("successful_adjustments", {})
            sa_count = len(sa)
            rates = [
                float(adj.get("success_rate", 0))
                for adj in sa.values()
                if adj.get("total_count", 0) > 0
            ]
            avg_rate = sum(rates) / len(rates) if rates else 0.0
            agent_summaries[agent_name] = {
                "patterns": fp_count,
                "adjustments": sa_count,
                "avg_success_rate": round(avg_rate, 3),
            }
            total_patterns += fp_count
            total_adjustments += sa_count

        return {
            "total_agents": len(agents),
            "total_patterns": total_patterns,
            "total_adjustments": total_adjustments,
            "agents": agent_summaries,
            "shared_patterns": shared,
        }

    # ==================================================================
    # MEMORY.md - 长期事实记忆
    # ==================================================================

    def append_fact(self, section: str, fact: str) -> None:
        """追加一条事实到 MEMORY.md 的指定章节。

        若章节不存在则创建。章节顺序遵循 MEMORY_SECTIONS 常量。
        事实文本强制单行化以保持 markdown 列表格式。

        Args:
            section: 章节名（如 "用户事实"/"逝者信息"/"流程进度"/"待澄清矛盾"）
            fact: 事实文本
        """
        # 读取现有内容
        existing = ""
        if self.memory_file.exists():
            try:
                existing = self.memory_file.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning("FileMemoryStore.append_fact 读取失败: %s", e)
                existing = ""

        # 解析为章节 -> 行列表
        sections = self._parse_memory_sections(existing)

        # 追加新事实
        fact_oneline = (fact or "").replace("\n", " ").replace("\r", " ").strip()
        if not fact_oneline:
            return  # 空事实不写
        sections.setdefault(section, []).append(fact_oneline)

        # 重新组装
        content = self._render_memory_sections(sections)

        try:
            _atomic_write(self.memory_file, content)
        except Exception as e:
            logger.warning("FileMemoryStore.append_fact 写入失败: %s", e)

    def load_facts(self) -> dict[str, list[str]]:
        """按章节加载 MEMORY.md 中的所有事实。

        Returns:
            dict[section_name, list[fact_str]]；文件不存在返回空 dict
        """
        if not self.memory_file.exists():
            return {}
        try:
            text = self.memory_file.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("FileMemoryStore.load_facts 读取失败: %s", e)
            return {}
        return self._parse_memory_sections(text)

    @staticmethod
    def _parse_memory_sections(text: str) -> dict[str, list[str]]:
        """解析 MEMORY.md 为 {section: [fact, ...]}。

        章节以 `## 章节名` 标识；事实以 `- ` 列表项标识。
        非列表项的文本（如章节描述）被忽略，只保留 `- ` 开头的事实。
        """
        sections: dict[str, list[str]] = {}
        current_section: str | None = None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                # 章节标题
                current_section = stripped[3:].strip()
                sections.setdefault(current_section, [])
            elif stripped.startswith("- ") and current_section is not None:
                # 列表项事实
                fact = stripped[2:].strip()
                if fact:
                    sections[current_section].append(fact)
            # 其他行忽略（如空行、章节描述）
        return sections

    @staticmethod
    def _render_memory_sections(sections: dict[str, list[str]]) -> str:
        """渲染 sections dict 为 MEMORY.md 文本。

        章节顺序：先按 MEMORY_SECTIONS 固定顺序，再按 dict 中剩余顺序。
        每个章节标题 `## 章节名`，每条事实 `- 事实`。
        """
        lines: list[str] = ["# MEMORY - 长期事实记忆", ""]

        # 先按固定顺序渲染
        rendered_sections: set[str] = set()
        for section in MEMORY_SECTIONS:
            if section in sections:
                lines.append(f"## {section}")
                lines.append("")
                for fact in sections[section]:
                    lines.append(f"- {fact}")
                lines.append("")
                rendered_sections.add(section)

        # 再渲染剩余章节（用户自定义章节）
        for section, facts in sections.items():
            if section in rendered_sections:
                continue
            lines.append(f"## {section}")
            lines.append("")
            for fact in facts:
                lines.append(f"- {fact}")
            lines.append("")

        return "\n".join(lines)

    # ==================================================================
    # 导出 - 供 CLI memory-export 子命令使用
    # ==================================================================

    def export_markdown(self) -> str:
        """把 USER.md + EPISODES.md + MEMORY.md 合并为单一 markdown 视图。

        供 `deadman memory-export` 子命令直接打印。
        文件不存在时对应章节为空。
        """
        parts: list[str] = []

        # USER.md
        parts.append("# 用户画像（USER.md）")
        parts.append("")
        if self.user_file.exists():
            try:
                user_text = self.user_file.read_text(encoding="utf-8")
                parts.append(user_text.rstrip())
            except OSError:
                parts.append("(读取失败)")
        else:
            parts.append("(无 USER.md，未保存用户画像)")
        parts.append("")
        parts.append("---")
        parts.append("")

        # MEMORY.md
        parts.append("# 长期事实记忆（MEMORY.md）")
        parts.append("")
        if self.memory_file.exists():
            try:
                mem_text = self.memory_file.read_text(encoding="utf-8")
                parts.append(mem_text.rstrip())
            except OSError:
                parts.append("(读取失败)")
        else:
            parts.append("(无 MEMORY.md，未保存事实)")
        parts.append("")
        parts.append("---")
        parts.append("")

        # EPISODES.md
        parts.append("# 情景记忆摘要（EPISODES.md）")
        parts.append("")
        if self.episodes_file.exists():
            try:
                ep_text = self.episodes_file.read_text(encoding="utf-8")
                parts.append(ep_text.rstrip())
            except OSError:
                parts.append("(读取失败)")
        else:
            parts.append("(无 EPISODES.md，未保存情景记忆)")
        parts.append("")

        return "\n".join(parts)

    # ==================================================================
    # P2.5 Memory Snapshot 导入/导出
    # ==================================================================
    #
    # 格式(网络字节序):
    #   [4 字节魔数 "DMSP"]
    #   [1 字节 version]
    #   [1 字节 flag]   0=明文 gzip, 1=AES-256-GCM
    #   [12 字节 nonce]  (仅 flag=1 时存在)
    #   [剩余 = payload]
    #
    # payload(flag=0): gzip(JSON {"files": {name: content}, "meta": {...}})
    # payload(flag=1): AES-256-GCM 密文(明文同上 gzip(JSON))
    #
    # feature flag DEADMAN_MEMORY_SNAPSHOT_ENABLED=0 时:
    #   - export_snapshot 返回空 bytes b""
    #   - import_snapshot 收到空 bytes 返回 False
    #
    # 降级:
    #   - cryptography 库不可用 → 即便指定 aes_key 也降级到明文 gzip
    #   - 解密/解压失败 → 返回 False

    def export_snapshot(
        self,
        aes_key: bytes | None = None,
    ) -> bytes:
        """把 USER.md + MEMORY.md + EPISODES.md + REFLEXION.json 打包导出。

        Args:
            aes_key: 可选 AES-256-GCM 密钥(32 字节)。提供且 cryptography
                     可用时加密;否则降级到明文 gzip。

        Returns:
            snapshot 二进制;DEADMAN_MEMORY_SNAPSHOT_ENABLED=0 时返回 b""。
        """
        if not MEMORY_SNAPSHOT_ENABLED:
            return b""

        # 收集文件内容(文件不存在则空字符串)
        files: dict[str, str] = {}
        for name, path in (
            ("USER.md", self.user_file),
            ("MEMORY.md", self.memory_file),
            ("EPISODES.md", self.episodes_file),
            ("REFLEXION.json", self.reflexion_file),
        ):
            if path.exists():
                try:
                    files[name] = path.read_text(encoding="utf-8")
                except OSError as exc:
                    logger.warning("snapshot 读取 %s 失败: %s", name, exc)
                    files[name] = ""
            else:
                files[name] = ""

        payload_obj = {
            "files": files,
            "meta": {
                "exported_at": datetime.now().isoformat(),
                "memory_dir": str(self.memory_dir),
                "version": _SNAPSHOT_VERSION,
            },
        }
        json_bytes = json.dumps(payload_obj, ensure_ascii=False, default=str).encode("utf-8")
        gz_bytes = gzip.compress(json_bytes)

        # 判断是否走加密
        use_aes = (
            aes_key is not None
            and _HAS_CRYPTO
            and len(aes_key) == 32
        )
        if use_aes:
            try:
                assert aes_key is not None  # narrowed by use_aes check
                nonce = os.urandom(12)
                aesgcm = AESGCM(aes_key)
                ciphertext = aesgcm.encrypt(nonce, gz_bytes, None)
                header = (
                    _SNAPSHOT_MAGIC
                    + bytes([_SNAPSHOT_VERSION])
                    + bytes([_SNAPSHOT_FLAG_AESGCM])
                    + nonce
                )
                return header + ciphertext
            except Exception as exc:
                logger.warning("AES-GCM 加密失败,降级明文 gzip: %s", exc)
                use_aes = False
        # 明文 gzip
        header = (
            _SNAPSHOT_MAGIC
            + bytes([_SNAPSHOT_VERSION])
            + bytes([_SNAPSHOT_FLAG_PLAIN])
        )
        return header + gz_bytes

    def import_snapshot(
        self,
        data: bytes,
        aes_key: bytes | None = None,
    ) -> bool:
        """从 snapshot 二进制恢复文件,替换当前 4 个文件。

        Args:
            data: export_snapshot 返回的 bytes
            aes_key: 可选 AES-256-GCM 密钥(若导出时加密)

        Returns:
            True 表示恢复成功;False 表示数据无效或解密失败。
            DEADMAN_MEMORY_SNAPSHOT_ENABLED=0 时直接返回 False。
        """
        if not MEMORY_SNAPSHOT_ENABLED:
            return False
        if not isinstance(data, (bytes, bytearray)) or len(data) < 6:
            return False
        data = bytes(data)
        # 校验魔数
        if data[:4] != _SNAPSHOT_MAGIC:
            return False
        version = data[4]
        if version != _SNAPSHOT_VERSION:
            logger.warning("snapshot 版本不匹配: got=%s expect=%s", version, _SNAPSHOT_VERSION)
            return False
        flag = data[5]
        body = data[6:]

        if flag == _SNAPSHOT_FLAG_AESGCM:
            # AES-256-GCM 解密
            if not _HAS_CRYPTO or len(body) < 12:
                return False
            if aes_key is None or len(aes_key) != 32:
                return False
            nonce = body[:12]
            ciphertext = body[12:]
            try:
                aesgcm = AESGCM(aes_key)
                gz_bytes = aesgcm.decrypt(nonce, ciphertext, None)
            except Exception as exc:
                logger.warning("snapshot AES-GCM 解密失败: %s", exc)
                return False
        elif flag == _SNAPSHOT_FLAG_PLAIN:
            gz_bytes = body
        else:
            logger.warning("snapshot 未知 flag: %s", flag)
            return False

        # 解压 + 解析 JSON
        try:
            json_bytes = gzip.decompress(gz_bytes)
            payload = json.loads(json_bytes.decode("utf-8"))
        except Exception as exc:
            logger.warning("snapshot 解压/解析失败: %s", exc)
            return False
        if not isinstance(payload, dict):
            return False
        files = payload.get("files", {})
        if not isinstance(files, dict):
            return False

        # 替换当前文件(原子写入)
        file_map = {
            "USER.md": self.user_file,
            "MEMORY.md": self.memory_file,
            "EPISODES.md": self.episodes_file,
            "REFLEXION.json": self.reflexion_file,
        }
        try:
            for name, path in file_map.items():
                content = files.get(name, "")
                # 空内容则跳过(保留原文件),非空则原子写入
                if content == "" and not path.exists():
                    continue
                _atomic_write(path, content)
            return True
        except Exception as exc:
            logger.warning("snapshot 写入文件失败: %s", exc)
            return False

    @staticmethod
    def is_snapshot_enabled() -> bool:
        """测试辅助:返回 snapshot feature flag 状态"""
        return MEMORY_SNAPSHOT_ENABLED
