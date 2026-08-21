"""organization tenant: customers / cases / case_events

Revision ID: 0002_organization_tenant
Revises: 0001_initial
Create Date: 2025-01-01 00:00:00

B2B-IMPLEMENTATION Step 5：机构客户档案与案件（DB 版）
    - customers: 客户档案（org_id + id 双键隔离）
    - cases: 案件（状态机见 org/case_flow.py）
    - case_events: 事件/审计（只增不改，状态变更强制落库）

org_id 为硬隔离键，所有查询必须同时带 org_id + 主键（防跨租户越权）。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_organization_tenant"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # customers — 机构客户档案
    op.create_table(
        "customers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("province", sa.String(32), nullable=False, server_default=""),
        sa.Column("stage", sa.String(32), nullable=False, server_default="planning"),
        sa.Column("owner_user_id", sa.String(36), nullable=True),
        sa.Column("relationships", sa.JSON, nullable=False),
        sa.Column("tags", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_customers_org_id", "customers", ["org_id"])
    op.create_index("ix_customers_org_owner", "customers", ["org_id", "owner_user_id"])

    # cases — 机构案件
    op.create_table(
        "cases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("case_type", sa.String(32), nullable=False, server_default="funeral"),
        sa.Column("status", sa.String(32), nullable=False, server_default="created"),
        sa.Column("stage", sa.String(32), nullable=False, server_default=""),
        sa.Column("assignee_user_id", sa.String(36), nullable=True),
        sa.Column("priority", sa.String(16), nullable=False, server_default="normal"),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("closed_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_cases_org_id", "cases", ["org_id"])
    op.create_index("ix_cases_customer_id", "cases", ["customer_id"])
    op.create_index("ix_cases_org_customer", "cases", ["org_id", "customer_id"])
    op.create_index("ix_cases_org_assignee", "cases", ["org_id", "assignee_user_id", "status"])

    # case_events — 案件事件/审计（只增不改）
    op.create_table(
        "case_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("case_id", sa.String(36), nullable=False),
        sa.Column("actor_user_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("detail", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_case_events_org_id", "case_events", ["org_id"])
    op.create_index("ix_case_events_case_id", "case_events", ["case_id"])
    op.create_index("ix_case_events_case_time", "case_events", ["case_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_case_events_case_time", table_name="case_events")
    op.drop_index("ix_case_events_case_id", table_name="case_events")
    op.drop_index("ix_case_events_org_id", table_name="case_events")
    op.drop_table("case_events")
    op.drop_index("ix_cases_org_assignee", table_name="cases")
    op.drop_index("ix_cases_org_customer", table_name="cases")
    op.drop_index("ix_cases_customer_id", table_name="cases")
    op.drop_index("ix_cases_org_id", table_name="cases")
    op.drop_table("cases")
    op.drop_index("ix_customers_org_owner", table_name="customers")
    op.drop_index("ix_customers_org_id", table_name="customers")
    op.drop_table("customers")
