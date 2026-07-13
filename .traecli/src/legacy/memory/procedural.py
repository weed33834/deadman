"""程序记忆 - 流程知识 + 用户进度。

类比人的技能记忆："办死亡证明要先去医院，带身份证"。
Graphiti 集成为可选项。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class Procedure:
    """程序记忆 - 任务流程"""

    procedure_id: str
    procedure_name: str
    jurisdiction: dict = field(default_factory=dict)  # 适用地域
    steps: list[dict] = field(default_factory=list)
    # [{step_id, action, required_documents, responsible_authority,
    #   time_estimate, common_issues, next_step}, ...]
    required_documents_total: list[str] = field(default_factory=list)
    estimated_total_time: Optional[str] = None
    source: str = "knowledge_base"  # knowledge_base / web_search / user_taught
    verified: bool = False
    last_updated: Optional[datetime] = None
    # 对应 9 阶段中的第几阶段（供 start_session 恢复进度用）
    stage: Optional[int] = None


@dataclass
class UserProgress:
    """用户在某流程上的进度"""

    user_id: str
    procedure_id: str
    current_step: int = 1
    completed_steps: list[int] = field(default_factory=list)
    skipped_steps: list[int] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.utcnow)
    last_active_at: datetime = field(default_factory=datetime.utcnow)
    notes: dict = field(default_factory=dict)


class ProceduralMemory:
    """程序记忆 - 流程知识 + 用户进度。

    - procedures: procedure_id -> Procedure
    - user_progress: (user_id, procedure_id) -> UserProgress
    """

    def __init__(self, graphiti_client: Any = None):
        self.procedures: dict[str, Procedure] = {}
        self.user_progress: dict[tuple[str, str], UserProgress] = {}
        self.graphiti = graphiti_client

    def get_procedure(
        self,
        procedure_name: str,
        jurisdiction: dict | None = None,
    ) -> Optional[Procedure]:
        """获取流程（按名称 + 地域匹配）。

        jurisdiction 为空时返回第一个同名流程。
        """
        for proc in self.procedures.values():
            if proc.procedure_name != procedure_name:
                continue
            if jurisdiction is None or self._jurisdiction_matches(
                proc.jurisdiction, jurisdiction
            ):
                return proc
        return None

    def get_procedures_by_stage(self, stage: int) -> list[Procedure]:
        """获取某阶段的所有流程（供 start_session 恢复进度用）"""
        return [p for p in self.procedures.values() if p.stage == stage]

    def get_user_progress(
        self, user_id: str, procedure_id: str
    ) -> Optional[UserProgress]:
        """获取用户进度"""
        return self.user_progress.get((user_id, procedure_id))

    def update_user_progress(
        self,
        user_id: str,
        procedure_id: str,
        step_completed: int,
    ) -> UserProgress:
        """更新用户进度（记录已完成步骤，推进当前步骤）"""
        key = (user_id, procedure_id)
        if key not in self.user_progress:
            self.user_progress[key] = UserProgress(
                user_id=user_id,
                procedure_id=procedure_id,
            )

        progress = self.user_progress[key]
        if step_completed not in progress.completed_steps:
            progress.completed_steps.append(step_completed)
        progress.current_step = step_completed + 1
        progress.last_active_at = datetime.utcnow()

        # 可选：同步到 Graphiti（跨会话续接）
        if self.graphiti is not None:
            try:
                self.graphiti.add_event({
                    "event_type": "UserProgressEvent",
                    "user_id": user_id,
                    "procedure_id": procedure_id,
                    "completed_steps": progress.completed_steps,
                    "current_step": progress.current_step,
                    "timestamp": datetime.utcnow(),
                })
            except Exception as e:
                logger.warning(f"Graphiti 同步失败: {e}")

        return progress

    def learn_from_user(
        self,
        user_id: str,
        procedure_name: str,
        jurisdiction: dict,
        user_correction: dict,
    ) -> Optional[Procedure]:
        """从用户反馈中学习 - 用户纠正了流程的某一步（Reflexion 机制）。

        user_correction 形如：{"step_id": 1, "correction": "..."}
        """
        proc = self.get_procedure(procedure_name, jurisdiction)
        if not proc:
            logger.warning(f"未找到流程 {procedure_name}，无法应用用户纠正")
            return None

        step_id = user_correction.get("step_id")
        for step in proc.steps:
            if step.get("step_id") == step_id:
                step["user_correction"] = user_correction.get("correction")
                step["corrected_by_user"] = user_id
                step["corrected_at"] = datetime.utcnow()
                break

        proc.last_updated = datetime.utcnow()
        proc.verified = False  # 需要重新验证

        # 可选：记录到 Graphiti 作为 KnowledgeVersion
        if self.graphiti is not None:
            try:
                self.graphiti.add_event({
                    "event_type": "KnowledgeVersion",
                    "procedure_id": proc.procedure_id,
                    "change": "user_correction",
                    "user_correction": user_correction,
                    "transaction_time": datetime.utcnow(),
                })
            except Exception as e:
                logger.warning(f"Graphiti 同步失败: {e}")

        return proc

    @staticmethod
    def _jurisdiction_matches(proc_juris: dict, query_juris: dict) -> bool:
        """地域匹配：query 的每个字段需与 proc 一致（proc 字段为空视为通配）"""
        for key, value in query_juris.items():
            proc_value = proc_juris.get(key)
            if proc_value and proc_value != value:
                return False
        return True
