"""Phase 15 CLI 集成清单 - Dead Man Switch（多因子死亡推定）

包含 8 个子命令：
    switch-init            [--user-id STR] [--frequency DAYS] [--missed N]
                           [--window DAYS] [--cooldown DAYS]
                           [--emergency-contact ID（可重复）]
                           [--lawyer-id ID]
                           [--heir-id ID（可重复）]
                           [--email STR] [--phone STR]
    switch-checkin         [--user-id STR] [--method web|cli|sms|email|telegram]
    switch-status          [--user-id STR]
    switch-tick            [--user-id STR]  （Cron 调用，推进状态机）
    switch-verify-contact  --user-id STR --contact-id STR --confirm bool
    switch-verify-heir     --user-id STR --heir-id STR --confirm bool
    switch-cancel          [--user-id STR] --reason STR
    switch-list-actions    [--user-id STR]

合规关联：
    - notification-guardrails.md：所有主动通知在 actions.py 内过 NotificationGuardrail
    - safety-protocol.md：触发死亡推定后等待期至少 7 天，期间可撤销
    - legal-compliance-framework.md 第五章：email / phone 脱敏存储
    - integrity-framework.md：不编造联系人 / 继承人确认结果
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


# =====================================================================
# 子命令实现
# =====================================================================
def cmd_switch_init(args: argparse.Namespace) -> None:
    """switch-init：初始化 dead man switch 配置"""
    from deadman.deadman_switch.models import SwitchConfig

    store = _make_store(args)
    config = SwitchConfig(
        check_in_frequency_days=args.frequency,
        missed_threshold=args.missed,
        verification_window_days=args.window,
        cooldown_days=args.cooldown,
        emergency_contacts=list(args.emergency_contact or []),
        lawyer_user_id=args.lawyer_id,
        heir_user_ids=list(args.heir_id or []),
    )
    # 脱敏存储 PII（不在 CLI 输出中暴露原值）
    if args.email:
        config.set_email(args.email)
    if args.phone:
        config.set_phone(args.phone)
    record = store.init_switch(args.user_id, config)
    print(f"已初始化 dead man switch：user_id={record.user_id}")
    print(f"  state:                       {record.state.value}")
    print(f"  check_in_frequency_days:     {record.config.check_in_frequency_days}")
    print(f"  missed_threshold:            {record.config.missed_threshold}")
    print(f"  verification_window_days:    {record.config.verification_window_days}")
    print(f"  cooldown_days:               {record.config.cooldown_days}")
    print(f"  emergency_contacts:          {record.config.emergency_contacts}")
    print(f"  lawyer_user_id:              {record.config.lawyer_user_id}")
    print(f"  heir_user_ids:               {record.config.heir_user_ids}")
    print(f"  email_masked:                {record.config.email_masked}")
    print(f"  phone_masked:                {record.config.phone_masked}")
    print(
        "\n【边界告知】Dead Man Switch 是失联检测机制，"
        "不替代医疗预嘱 / 法律宣告死亡程序；触发后仍需人工核实。"
    )


def cmd_switch_checkin(args: argparse.Namespace) -> None:
    """switch-checkin：用户主动 check-in，状态机立即重置 ACTIVE"""

    store = _make_store(args)
    record = store.record_check_in(args.user_id, method=args.method)
    if record is None:
        print(f"[错误] 用户 {args.user_id} 尚未初始化 switch，请先 switch-init")
        sys.exit(1)
    print(f"已记录 check-in：user_id={record.user_id}, method={args.method}")
    print(f"  state:          {record.state.value}")
    print(f"  last_check_in:  {record.last_check_in.isoformat() if record.last_check_in else None}")


def cmd_switch_status(args: argparse.Namespace) -> None:
    """switch-status：查看当前状态"""

    store = _make_store(args)
    record = store.load(args.user_id)
    if record is None:
        print(f"(用户 {args.user_id} 尚未初始化 switch)")
        return
    print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
    print(
        "\n【边界告知】Dead Man Switch 是失联检测机制，"
        "不替代医疗预嘱 / 法律宣告死亡程序；触发后仍需人工核实。"
    )


def cmd_switch_tick(args: argparse.Namespace) -> None:
    """switch-tick：手动触发状态机检查（Cron 调用）"""

    store = _make_store(args)
    record = store.tick(args.user_id)
    if record is None:
        print(f"(用户 {args.user_id} 尚未初始化 switch)")
        return
    print(f"tick 完成：user_id={record.user_id}, state={record.state.value}")
    cooldown = store.cooldown_remaining_days(args.user_id)
    if record.state.value == "CONFIRMED":
        print(f"  cooldown_remaining_days: {cooldown}")
        if store.is_cooldown_passed(args.user_id):
            print("  冷静期已过，可调 switch-execute 执行动作")
        else:
            print("  冷静期未过，不可执行动作")


def cmd_switch_verify_contact(args: argparse.Namespace) -> None:
    """switch-verify-contact：紧急联系人确认 / 否认失联"""

    store = _make_store(args)
    record, msg = store.verify_emergency_contact(
        args.user_id, args.contact_id, args.confirm
    )
    if record is None:
        print(f"[错误] {msg}")
        sys.exit(1)
    print(f"verify_emergency_contact: user_id={args.user_id}, "
          f"contact_id={args.contact_id}, confirm={args.confirm}")
    print(f"  message: {msg}")
    print(f"  state:   {record.state.value}")


def cmd_switch_verify_heir(args: argparse.Namespace) -> None:
    """switch-verify-heir：继承人确认 / 否认失联"""

    store = _make_store(args)
    record, msg = store.verify_heir(
        args.user_id, args.heir_id, args.confirm
    )
    if record is None:
        print(f"[错误] {msg}")
        sys.exit(1)
    print(f"verify_heir: user_id={args.user_id}, "
          f"heir_id={args.heir_id}, confirm={args.confirm}")
    print(f"  message: {msg}")
    print(f"  state:   {record.state.value}")


def cmd_switch_cancel(args: argparse.Namespace) -> None:
    """switch-cancel：用户主动取消"""

    store = _make_store(args)
    record = store.cancel(args.user_id, reason=args.reason)
    if record is None:
        print(f"[错误] 用户 {args.user_id} 尚未初始化 switch")
        sys.exit(1)
    print(f"已取消 switch：user_id={record.user_id}")
    print(f"  state:  {record.state.value}")
    print(f"  reason: {args.reason}")


def cmd_switch_list_actions(args: argparse.Namespace) -> None:
    """switch-list-actions：列出待执行动作"""

    store = _make_store(args)
    record = store.load(args.user_id)
    if record is None:
        print(f"(用户 {args.user_id} 尚未初始化 switch)")
        return
    print(f"user_id: {record.user_id}, state: {record.state.value}")
    if not record.pending_actions:
        print("(无待执行动作)")
    else:
        print(f"待执行动作 ({len(record.pending_actions)} 项)：")
        for action in record.pending_actions:
            print(f"  - {action}")
    if record.executed_actions:
        print(f"\n已执行动作 ({len(record.executed_actions)} 项)：")
        for ea in record.executed_actions:
            print(f"  - {ea.get('action')} @ {ea.get('executed_at')}")


def cmd_switch_execute(args: argparse.Namespace) -> None:
    """switch-execute：CONFIRMED → EXECUTED 执行预设动作

    safety-protocol.md：必须先过冷静期（cooldown_days）
    """
    from deadman.deadman_switch.actions import SwitchActionExecutor

    store = _make_store(args)
    executor = SwitchActionExecutor(store=store)
    try:
        result = executor.execute_confirmed(args.user_id)
    except RuntimeError as exc:
        print(f"[错误] 执行被拒绝：{exc}")
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))


# =====================================================================
# 辅助
# =====================================================================
def _make_store(args: argparse.Namespace):
    """构造 SwitchStore（支持 --data-dir 用于测试隔离）"""
    from deadman.deadman_switch.store import SwitchStore
    data_dir = getattr(args, "data_dir", None)
    if data_dir:
        from pathlib import Path
        return SwitchStore(data_dir=Path(data_dir))
    return SwitchStore()


# =====================================================================
# subparser 注册
# =====================================================================
def register_subparsers(subparsers: Any) -> None:
    """注册 Phase 15 共 9 个子命令

    子命令清单：
        switch-init / switch-checkin / switch-status / switch-tick /
        switch-verify-contact / switch-verify-heir / switch-cancel /
        switch-list-actions / switch-execute
    """
    # switch-init
    init_p = subparsers.add_parser(
        "switch-init", help="初始化 dead man switch 配置"
    )
    init_p.add_argument("--user-id", default="default-user", help="用户 ID")
    init_p.add_argument("--frequency", type=int, default=30,
                        help="check-in 频率（天/次，默认 30）")
    init_p.add_argument("--missed", type=int, default=3,
                        help="连续失联多少次触发 SUSPECTED（默认 3）")
    init_p.add_argument("--window", type=int, default=7,
                        help="多因子验证窗口（天，默认 7）")
    init_p.add_argument("--cooldown", type=int, default=7,
                        help="CONFIRMED 后冷静期天数（默认 7，最小 7）")
    init_p.add_argument("--emergency-contact", action="append", default=[],
                        help="紧急联系人 user_id（可重复）")
    init_p.add_argument("--lawyer-id", default=None,
                        help="律师 user_id（可选）")
    init_p.add_argument("--heir-id", action="append", default=[],
                        help="法定继承人 user_id（可重复）")
    init_p.add_argument("--email", default=None,
                        help="邮箱（脱敏后存储）")
    init_p.add_argument("--phone", default=None,
                        help="手机号（脱敏后存储）")
    init_p.add_argument("--data-dir", default=None,
                        help="数据根目录（测试用）")
    init_p.set_defaults(func=cmd_switch_init)

    # switch-checkin
    checkin_p = subparsers.add_parser(
        "switch-checkin", help="用户主动 check-in（重置状态机到 ACTIVE）"
    )
    checkin_p.add_argument("--user-id", default="default-user", help="用户 ID")
    checkin_p.add_argument("--method", default="cli",
                            choices=["web", "cli", "sms", "email", "telegram"],
                            help="check-in 渠道")
    checkin_p.add_argument("--data-dir", default=None, help="数据根目录（测试用）")
    checkin_p.set_defaults(func=cmd_switch_checkin)

    # switch-status
    status_p = subparsers.add_parser(
        "switch-status", help="查看 switch 当前状态"
    )
    status_p.add_argument("--user-id", default="default-user", help="用户 ID")
    status_p.add_argument("--data-dir", default=None, help="数据根目录（测试用）")
    status_p.set_defaults(func=cmd_switch_status)

    # switch-tick
    tick_p = subparsers.add_parser(
        "switch-tick", help="手动触发状态机检查（Cron 调用）"
    )
    tick_p.add_argument("--user-id", default="default-user", help="用户 ID")
    tick_p.add_argument("--data-dir", default=None, help="数据根目录（测试用）")
    tick_p.set_defaults(func=cmd_switch_tick)

    # switch-verify-contact
    vc_p = subparsers.add_parser(
        "switch-verify-contact", help="紧急联系人确认 / 否认失联"
    )
    vc_p.add_argument("--user-id", required=True, help="用户 ID")
    vc_p.add_argument("--contact-id", required=True, help="紧急联系人 user_id")
    vc_p.add_argument("--confirm", required=True, type=_str2bool,
                        help="True=确认失联 / False=表示当事人安好")
    vc_p.add_argument("--data-dir", default=None, help="数据根目录（测试用）")
    vc_p.set_defaults(func=cmd_switch_verify_contact)

    # switch-verify-heir
    vh_p = subparsers.add_parser(
        "switch-verify-heir", help="法定继承人确认 / 否认失联"
    )
    vh_p.add_argument("--user-id", required=True, help="用户 ID")
    vh_p.add_argument("--heir-id", required=True, help="继承人 user_id")
    vh_p.add_argument("--confirm", required=True, type=_str2bool,
                        help="True=确认失联 / False=表示当事人安好")
    vh_p.add_argument("--data-dir", default=None, help="数据根目录（测试用）")
    vh_p.set_defaults(func=cmd_switch_verify_heir)

    # switch-cancel
    cancel_p = subparsers.add_parser(
        "switch-cancel", help="用户主动取消 switch"
    )
    cancel_p.add_argument("--user-id", default="default-user", help="用户 ID")
    cancel_p.add_argument("--reason", default="user_cancelled", help="取消原因")
    cancel_p.add_argument("--data-dir", default=None, help="数据根目录（测试用）")
    cancel_p.set_defaults(func=cmd_switch_cancel)

    # switch-list-actions
    la_p = subparsers.add_parser(
        "switch-list-actions", help="列出待执行动作"
    )
    la_p.add_argument("--user-id", default="default-user", help="用户 ID")
    la_p.add_argument("--data-dir", default=None, help="数据根目录（测试用）")
    la_p.set_defaults(func=cmd_switch_list_actions)

    # switch-execute（手动触发 CONFIRMED → EXECUTED）
    exec_p = subparsers.add_parser(
        "switch-execute", help="执行 CONFIRMED 状态的预设动作（需冷静期已过）"
    )
    exec_p.add_argument("--user-id", default="default-user", help="用户 ID")
    exec_p.add_argument("--data-dir", default=None, help="数据根目录（测试用）")
    exec_p.set_defaults(func=cmd_switch_execute)


def _str2bool(v: str) -> bool:
    """argparse type helper：把字符串转为 bool"""
    if isinstance(v, bool):
        return v
    if v.lower() in ("yes", "true", "t", "y", "1"):
        return True
    if v.lower() in ("no", "false", "f", "n", "0"):
        return False
    raise argparse.ArgumentTypeError(f"Boolean value expected, got {v}")
