"""Alembic 迁移环境 - 企业级扩展④

从 deadman.config.settings.database_url 动态构建同步 URL
（Alembic 的 env.py 运行在同步上下文，asyncpg → psycopg2 自动转换）。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 确保 deadman 包可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".traecli" / "src"))

from deadman.config import settings  # noqa: E402
from deadman.db.base import Base  # noqa: E402
from deadman.db import models  # noqa: E402,F401 - 注册模型到 metadata

# Alembic 配置对象
config = context.config

# 日志配置
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 目标 metadata（autogenerate 的真相源）
target_metadata = Base.metadata

# 动态设置同步数据库 URL
# asyncpg → psycopg2 转换（Alembic env.py 用同步引擎）
_db_url = settings.database_url
if _db_url:
    _db_url = _db_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    _db_url = _db_url.replace("postgresql://", "postgresql+psycopg2://")
    config.set_main_option("sqlalchemy.url", _db_url)


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 脚本而不连接数据库。"""
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise SystemExit("DATABASE_URL 未配置，无法运行离线迁移")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库执行迁移。"""
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise SystemExit("DATABASE_URL 未配置，无法运行在线迁移")
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
