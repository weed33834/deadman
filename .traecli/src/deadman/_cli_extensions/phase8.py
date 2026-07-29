"""Phase 8 CLI 集成清单 - 用户认证与会话系统

本文件不直接修改 cli.py，提供：
1. cmd_xxx 函数 - 子命令处理函数（可直接被 cli.py import）
2. register_subparser(subparsers) - 注册子命令到 argparse subparsers
3. COMMANDS 清单 - 供 cli.py 主入口引用

主智能体集成步骤：
    from deadman._cli_extensions.phase8 import register_subparser
    register_subparser(subparsers)  # 在 cli.py 的 main() 中调用

并在 if-elif 分发链中加：
    elif args.command == "auth-register": cmd_auth_register(args)
    elif args.command == "auth-login": cmd_auth_login(args)
    elif args.command == "auth-me": cmd_auth_me(args)
    elif args.command == "auth-user-list": cmd_auth_user_list(args)

子命令清单：
    auth-register --email STR --password STR [--display-name STR]
    auth-login --email STR --password STR
    auth-me --token STR
    auth-user-list
"""

from __future__ import annotations

import json
import sys
from typing import Any


# === 子命令处理函数 ===


def cmd_auth_register(args) -> None:
    """注册新用户并签发 token

    --email / --password / --display-name
    输出：user_id + token
    """
    from deadman.auth.store import UserStore
    from deadman.auth.jwt import JWTManager
    from deadman.config import settings

    store = UserStore(data_dir=settings.auth_data_dir)
    store.password_min_length = settings.password_min_length
    try:
        user = store.register(args.email, args.password, args.display_name)
    except ValueError as exc:
        print(f"注册失败：{exc}")
        raise SystemExit(1) from None

    token = JWTManager(
        secret=settings.jwt_secret or None,
        expiry_days=settings.jwt_expiry_days,
    ).issue(user)
    print(f"注册成功。user_id={user['user_id']}")
    print(f"token={token}")


def cmd_auth_login(args) -> None:
    """登录获取 token

    --email / --password
    输出：token + display_name
    失败：exit 1（防枚举：不区分"邮箱不存在" vs "密码错"）
    """
    from deadman.auth.store import UserStore
    from deadman.auth.jwt import JWTManager
    from deadman.config import settings

    store = UserStore(data_dir=settings.auth_data_dir)
    user = store.verify(args.email, args.password)
    if not user:
        print("登录失败")
        sys.exit(1)

    token = JWTManager(
        secret=settings.jwt_secret or None,
        expiry_days=settings.jwt_expiry_days,
    ).issue(user)
    print(f"token={token}")
    print(f"display_name={user.get('display_name', '')}")


def cmd_auth_me(args) -> None:
    """显示当前用户（根据 token）

    --token STR
    输出：用户信息 JSON
    """
    from deadman.auth.jwt import JWTManager
    from deadman.auth.store import UserStore
    from deadman.config import settings

    payload = JWTManager(
        secret=settings.jwt_secret or None,
        expiry_days=settings.jwt_expiry_days,
    ).verify(args.token)
    if not payload:
        print("token 无效或过期")
        sys.exit(1)

    user = UserStore(data_dir=settings.auth_data_dir).get_user(payload["user_id"])
    if not user:
        print("用户不存在")
        sys.exit(1)
    print(json.dumps(user, indent=2, ensure_ascii=False))


def cmd_auth_user_list(args) -> None:
    """列出所有用户（admin only）

    注意：调用方需自行确保当前操作者是 admin（本命令不做角色校验，集成方负责）
    输出：每行一个用户 user_id + display_name + email_hmac 前 16 字符
    """
    from deadman.auth.store import UserStore
    from deadman.config import settings

    users = UserStore(data_dir=settings.auth_data_dir).list_users()
    if not users:
        print("（无用户）")
        return
    for u in users:
        hmac_preview = u.get("email_hmac", "...")[:16]
        print(f"{u['user_id']}  {u.get('display_name', '')}  {hmac_preview}...")
    print(f"\n共 {len(users)} 个用户")


# === 子命令注册 ===


def register_subparsers(subparsers: Any) -> None:
    """注册 Phase 8 子命令到 argparse subparsers

    在 cli.py 的 main() 中调用：
        from deadman._cli_extensions.phase8 import register_subparsers
        register_subparsers(subparsers)
    """
    # 兼容旧调用名 register_subparser（单数）
    return register_subparser(subparsers)


def register_subparser(subparsers: Any) -> None:
    """旧函数名（保留向后兼容）"""
    # auth-register
    p = subparsers.add_parser(
        "auth-register",
        help="注册新用户（Phase 8 用户认证）",
        description="注册新用户，返回 user_id 和 JWT token",
    )
    p.add_argument("--email", required=True, help="邮箱")
    p.add_argument("--password", required=True, help="密码（至少 8 位）")
    p.add_argument("--display-name", default=None, help="显示名（可选）")
    p.set_defaults(func=cmd_auth_register)

    # auth-login
    p = subparsers.add_parser(
        "auth-login",
        help="登录获取 token（Phase 8 用户认证）",
        description="用邮箱密码登录，返回 JWT token",
    )
    p.add_argument("--email", required=True, help="邮箱")
    p.add_argument("--password", required=True, help="密码")
    p.set_defaults(func=cmd_auth_login)

    # auth-me
    p = subparsers.add_parser(
        "auth-me",
        help="显示当前用户（Phase 8 用户认证）",
        description="根据 JWT token 显示当前用户信息",
    )
    p.add_argument("--token", required=True, help="JWT token")
    p.set_defaults(func=cmd_auth_me)

    # auth-user-list（admin only）
    p = subparsers.add_parser(
        "auth-user-list",
        help="列出所有用户（admin only，Phase 8 用户认证）",
        description="列出所有用户（仅 admin 可调用，调用方需自行校验角色）",
    )
    p.set_defaults(func=cmd_auth_user_list)


# === 命令清单（供 cli.py 引用）===
COMMANDS = [
    "auth-register",
    "auth-login",
    "auth-me",
    "auth-user-list",
]
