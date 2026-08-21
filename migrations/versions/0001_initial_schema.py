"""initial schema: users / cron_jobs / notification_* / password_reset_tokens

Revision ID: 0001_initial
Revises:
Create Date: 2025-01-01 00:00:00

企业级扩展④：主数据库初始 schema
    - users: 用户表（外键根，替代 auth/users.json）
    - cron_jobs: Cron 调度任务（替代 cron/jobs.json）
    - notification_consents / unsubscribes / sent_logs / last_sessions
      （替代 notification/guardrail.py 的 4 个 JSON 文件）
    - password_reset_tokens（替代 auth/password_reset_tokens.json）
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # users — 用户表（外键根）
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("email_hmac", sa.String(128), nullable=False),
        sa.Column("password_hash", sa.String(128), nullable=False),
        sa.Column("salt", sa.String(64), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="user"),
        sa.Column("family_id", sa.String(36), nullable=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("password_updated_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("email_hmac", name="uq_users_email_hmac"),
    )
    op.create_index("ix_users_email_hmac", "users", ["email_hmac"])
    op.create_index("ix_users_family_id", "users", ["family_id"])

    # cron_jobs — Cron 调度任务
    op.create_table(
        "cron_jobs",
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("schedule", sa.String(64), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("scope", sa.String(32), nullable=False, server_default="cron"),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("last_fired", sa.DateTime, nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "pending_confirmation",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_cron_jobs_user_id", "cron_jobs", ["user_id"])
    op.create_index("ix_cron_jobs_enabled_expires", "cron_jobs", ["enabled", "expires_at"])

    # notification_consents — 通知同意记录
    op.create_table(
        "notification_consents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_notification_consents_user_id", "notification_consents", ["user_id"])

    # notification_unsubscribes — 退订记录
    op.create_table(
        "notification_unsubscribes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index(
        "ix_notification_unsubscribes_user_id", "notification_unsubscribes", ["user_id"]
    )

    # notification_sent_logs — 发送日志
    op.create_table(
        "notification_sent_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("sent_at", sa.DateTime, nullable=False),
    )
    op.create_index(
        "ix_notification_sent_logs_user_sent", "notification_sent_logs", ["user_id", "sent_at"]
    )

    # notification_last_sessions — 最近会话状态
    op.create_table(
        "notification_last_sessions",
        sa.Column("user_id", sa.String(36), primary_key=True),
        sa.Column("ended_at", sa.DateTime, nullable=True),
        sa.Column("safety_triggered", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("emotion_intensity", sa.Float, nullable=False, server_default="0.0"),
        sa.Column(
            "involved_sensitive_death",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("user_birthday", sa.String(10), nullable=True),
        sa.Column("deceased_birthday", sa.String(10), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    # password_reset_tokens — 密码重置令牌
    op.create_table(
        "password_reset_tokens",
        sa.Column("token", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
    )
    op.create_index(
        "ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"]
    )
    op.create_index(
        "ix_password_reset_tokens_expires_at", "password_reset_tokens", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_table("password_reset_tokens")
    op.drop_table("notification_last_sessions")
    op.drop_index("ix_notification_sent_logs_user_sent", table_name="notification_sent_logs")
    op.drop_table("notification_sent_logs")
    op.drop_index(
        "ix_notification_unsubscribes_user_id", table_name="notification_unsubscribes"
    )
    op.drop_table("notification_unsubscribes")
    op.drop_index("ix_notification_consents_user_id", table_name="notification_consents")
    op.drop_table("notification_consents")
    op.drop_index("ix_cron_jobs_enabled_expires", table_name="cron_jobs")
    op.drop_index("ix_cron_jobs_user_id", table_name="cron_jobs")
    op.drop_table("cron_jobs")
    op.drop_index("ix_users_family_id", table_name="users")
    op.drop_index("ix_users_email_hmac", table_name="users")
    op.drop_table("users")
