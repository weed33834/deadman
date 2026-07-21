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
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

from .manager import sanitize_before_store
from .semantic import UserProfile

logger = logging.getLogger(__name__)

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

    def __init__(self, memory_dir: Optional[Path] = None) -> None:
        """初始化文件记忆存储。

        Args:
            memory_dir: 存储目录，默认 ~/.deadman/memory/
        """
        self.memory_dir: Path = memory_dir if memory_dir is not None else DEFAULT_MEMORY_DIR
        self.user_file: Path = self.memory_dir / "USER.md"
        self.memory_file: Path = self.memory_dir / "MEMORY.md"
        self.episodes_file: Path = self.memory_dir / "EPISODES.md"

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

    def load_profile(self, user_id: str) -> Optional[UserProfile]:
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
        self, episode_id: str, summary: str, timestamp: Optional[datetime] = None
    ) -> None:
        """追加一条情景记忆摘要到 EPISODES.md。

        每行格式：`[YYYY-MM-DD HH:MM] session=xxx summary=xxx`
        不包含换行的 summary 会被强制单行化（换行替换为空格）。

        Args:
            episode_id: 片段 ID（作为 session 字段写入）
            summary: 片段摘要文本
            timestamp: 时间戳，默认 now()
        """
        if timestamp is None:
            timestamp = datetime.now()
        ts_str = timestamp.strftime("%Y-%m-%d %H:%M")
        # summary 强制单行，避免破坏 EPISODES.md 的行格式
        summary_oneline = (summary or "").replace("\n", " ").replace("\r", " ").strip()
        # 截断超长摘要，避免单行过长
        if len(summary_oneline) > 500:
            summary_oneline = summary_oneline[:500] + "..."

        line = f"[{ts_str}] session={episode_id} summary={summary_oneline}\n"

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
        if limit > 0:
            recent = results[-limit:]
        else:
            recent = list(results)
        return recent

    @staticmethod
    def _parse_episode_line(line: str) -> Optional[dict[str, Any]]:
        """解析单行 episode：[YYYY-MM-DD HH:MM] session=xxx summary=xxx"""
        # 提取时间戳
        if "]" not in line:
            return None
        ts_end = line.index("]")
        ts_str = line[1:ts_end]  # 去掉首 [ 和末 ]
        rest = line[ts_end + 1 :].strip()

        # 解析 session= 与 summary=
        session = ""
        summary = ""
        # session= 是第一个字段
        if rest.startswith("session="):
            rest = rest[len("session=") :]
            # summary= 是下一个分隔符
            if " summary=" in rest:
                sess_part, _, sum_part = rest.partition(" summary=")
                session = sess_part.strip()
                summary = sum_part.strip()
            else:
                session = rest.strip()

        # 时间戳解析（失败保留原字符串）
        timestamp: Optional[datetime]
        try:
            timestamp = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
        except ValueError:
            timestamp = None

        return {
            "timestamp": timestamp,
            "timestamp_str": ts_str,
            "session": session,
            "summary": summary,
            "raw": line,
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
        current_section: Optional[str] = None
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
