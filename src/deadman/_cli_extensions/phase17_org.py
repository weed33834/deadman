"""Phase 17（To B 机构域）CLI 子命令：deadman org migrate-crypto。

本模块定义 `org` 子命令组的 `migrate-crypto` 实现，通过
`register_subparsers(subparsers)` 挂载到 argparse subparsers。

迁移语义（Step 4.3）：
    - 多租户（multi）模式下，把旧 per-user 派生加密的 envelope 重加密为
      per-tenant 派生（ending_note / vault / deadman_switch）。
    - single 模式下派生本就 per-user，无迁移需求，直接提示跳过。
    - 迁移在目标租户的 TenantContext 内执行：store 的默认 data_dir 自动
      路由到该租户目录；load 用 per-tenant 密钥（失败自动 fallback 旧
      per-user），save 以当前 per-tenant 密钥重写。
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _iter_tenant_dirs(root) -> list[str]:
    """列出 multi 模式下的租户 id（tenants/ 下的一级子目录）。"""
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and p.name != "registry.json")


def cmd_migrate_crypto(args: argparse.Namespace) -> None:
    """把指定租户（或全部）的旧 per-user 加密数据重加密为 per-tenant。"""
    from deadman.infrastructure.multi_tenant import (
        TENANTS_ROOT,
        TenantContext,
        TenantInfo,
        is_multi_tenant_enabled,
    )

    if not is_multi_tenant_enabled():
        print("当前为 single 模式（per-user 派生），无需迁移。")
        return

    tenants = [args.tenant] if args.tenant else _iter_tenant_dirs(TENANTS_ROOT)

    if not tenants:
        print("未发现任何租户数据目录（tenants/ 为空）。")
        return

    total = 0
    for tid in tenants:
        with TenantContext(TenantInfo(tenant_id=tid, name="migrate-crypto")):
            count = _migrate_single_tenant(args)
        print(f"tenant={tid}: 重加密 {count} 个文件")
        total += count
    print(f"全部完成，共重加密 {total} 个文件")


def _migrate_single_tenant(args: argparse.Namespace) -> int:
    """在租户上下文内迁移单个租户的 ending_note / vault / switch 数据。

    返回重加密的文件数量。
    """
    count = 0

    if args.ending_note:
        count += _migrate_ending_notes(args)

    if args.switch:
        count += _migrate_switch(args)

    # vault：仅投递时才解密，索引不落明文，无独立迁移入口；如无 per-user
    # 旧数据（multi 新建）则天然为 per-tenant，跳过。保留占位便于后续扩展。
    if args.vault:
        count += _migrate_vault(args)

    return count


def _migrate_ending_notes(args: argparse.Namespace) -> int:
    """ending_note：对每个用户 load（per-tenant 优先、per-user 兜底）+ save 重加密。"""
    from deadman.ending_note.store import EndingNoteStore

    store = EndingNoteStore(data_dir=args.data_dir)
    count = 0
    for user_dir in store.data_dir.iterdir():
        if not user_dir.is_dir() or not (user_dir / "note.json").exists():
            continue
        user_id = user_dir.name
        note = store.load(user_id)
        if note is None:
            continue
        store.save(note)  # 以当前 per-tenant 密钥重写
        count += 1
    return count


def _migrate_switch(args: argparse.Namespace) -> int:
    """deadman_switch：load（兼容旧 per-user）+ save 重加密。"""
    from deadman.deadman_switch.store import SwitchStore

    store = SwitchStore(data_dir=args.data_dir)
    count = 0
    for user_dir in store.data_dir.iterdir():
        if not user_dir.is_dir() or not (user_dir / "switch.json").exists():
            continue
        user_id = user_dir.name
        rec = store.load(user_id)
        if rec is None:
            continue
        store.save(rec)
        count += 1
    return count


def _migrate_vault(args: argparse.Namespace) -> int:
    """vault：当前索引不含 content 明文，迁移以「读到即重写」兜底。

    由于 vault 内容仅在投递时解密，这里不做全量重加密，避免误改投递状态。
    返回 0（无独立迁移动作），并提示用户以 vault 正常读写路径逐步迁移。
    """
    if getattr(args, "verbose", False):
        print("vault 内容仅投递时解密，无独立迁移入口；新写入自动使用 per-tenant 密钥。")
    return 0


def register_subparsers(subparsers: Any) -> None:
    """把 org 子命令挂载到 subparsers。"""
    org = subparsers.add_parser("org", help="机构域管理（migrate-crypto 等）")
    sub = org.add_subparsers(dest="org_command", required=True)

    mc = sub.add_parser("migrate-crypto", help="把旧 per-user 加密数据重加密为 per-tenant")
    mc.add_argument(
        "--tenant",
        default=None,
        help="指定迁移的租户 id（缺省迁移所有租户）",
    )
    mc.add_argument(
        "--ending-note",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否迁移 ending_note（默认开）",
    )
    mc.add_argument(
        "--vault",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否迁移 vault（默认开）",
    )
    mc.add_argument(
        "--switch",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否迁移 deadman_switch（默认开）",
    )
    mc.add_argument("--data-dir", default=None, help="显式指定数据目录（调试用）")
    mc.add_argument("--verbose", action="store_true", help="输出详细迁移日志")
    mc.set_defaults(func=cmd_migrate_crypto)


def register_subparser(subparsers: Any) -> None:
    """旧函数名（保留向后兼容）。"""
    return register_subparsers(subparsers)


# === 命令清单（供 cli.py 引用）===
COMMANDS = ["org"]
