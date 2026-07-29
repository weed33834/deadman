"""Phase 10 CLI 集成清单 - 终活笔记（エンディングノート）+ 家庭共享

本模块定义 Phase 10 的 4 个 CLI 子命令实现 + subparser 注册函数。
不修改 `deadman.cli.main()`；调用方按需 import 后挂载到自己的 subparsers。

subparser 清单：
    ending-note-show        [--user-id STR]
    ending-note-guide       [--user-id STR] [--section STR]
    ending-note-share       --user-id STR --target-user-id STR [--sections STR]
    ending-note-completion  --user-id STR

合规关联：
    - 所有输出末尾附"终活笔记不是法律文件"边界告知
      （service-boundary-framework.md 第三章）
    - ending-note-guide 检测自杀风险信号后停止流程引导，输出 safety-protocol L0 话术
      （safety-protocol.md 第一章）
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from ..ending_note.guide import EndingNoteGuide
from ..ending_note.models import SECTION_KEYS, EndingNote
from ..ending_note.store import EndingNoteStore

_DISCLAIMER = (
    "【边界告知】终活笔记不是法律文件，不替代遗嘱/信托/医疗预嘱；"
    "如需法律效力，请咨询律师/公证处办理正式文件。"
)


# ====================================================================
# 子命令实现
# ====================================================================


def cmd_ending_note_show(args: argparse.Namespace) -> None:
    """ending-note-show [--user-id STR]

    显示我的终活笔记内容（解密后输出 JSON）。
    """
    user_id = args.user_id
    store = EndingNoteStore()
    note = store.load(user_id)
    if note is None:
        print(f"[{user_id}] 尚无终活笔记。请用 ending-note-guide 开始填写。")
        print(_DISCLAIMER)
        return
    print("=== 终活笔记 ===")
    print(f"user_id:     {note.user_id}")
    print(f"note_id:     {note.note_id}")
    print(f"created_at:  {note.created_at.isoformat()}")
    print(f"updated_at:  {note.updated_at.isoformat()}")
    print()
    for key in SECTION_KEYS:
        title = _section_title(key)
        value = getattr(note, key, None)
        print(f"## {title} ({key})")
        if value is None:
            print("  (未填写)")
        else:
            print(json.dumps(value, ensure_ascii=False, indent=2))
        print()
    # 共享与触发
    print("## 共享设置")
    print(f"  shared_with: {note.shared_with or []}")
    print(f"  delivery_triggers: {note.delivery_triggers or []}")
    print()
    print("## 安全标记")
    print(json.dumps(note.safety_flags or {}, ensure_ascii=False, indent=2))
    print()
    # 安全告警
    flags = note.safety_flags or {}
    if flags.get("contains_suicidal_ideation"):
        print("【安全告警】本笔记检测到自杀风险信号。")
        print(
            "  如果你或笔记所有者正处于心理危机中，请立即联系"
            "当地心理危机干预热线或急救电话——你的安全比这份笔记更重要。"
        )
    print()
    print(_DISCLAIMER)


def cmd_ending_note_guide(args: argparse.Namespace) -> None:
    """ending-note-guide [--user-id STR] [--section STR]

    无 --section：获取下一章引导问题（跳过已填章节）。
    有 --section：输出指定章节的引导问题（不跳过；用于回看）。

    注：本命令不写入笔记；用户回答通过 Web API / 后续交互保存。
    """
    user_id = args.user_id
    section_arg = getattr(args, "section", None)

    store = EndingNoteStore()
    note = store.load(user_id) or EndingNote.new(user_id)
    guide = EndingNoteGuide(store=store)

    if section_arg:
        # 指定章节：找到对应 (key, title, question)
        for key, title, question in guide.SECTIONS:
            if key == section_arg:
                print(f"=== {title} ===")
                print(question)
                print()
                print(_DISCLAIMER)
                return
        print(f"[错误] 未知章节: {section_arg}")
        print(f"支持的章节: {', '.join(SECTION_KEYS)}")
        return

    # 不指定章节：调 next_question
    section_key, title, question = guide.next_question(note)
    print(f"=== {title} ===")
    print(f"(章节: {section_key})")
    print()
    print(question)
    print()

    # 完整度
    rate = guide.completion_rate(note)
    print(f"当前完整度: {rate['overall']:.0%}")
    filled = [k for k, v in rate["sections"].items() if v == 1.0]
    print(f"已填写章节: {len(filled)}/{len(SECTION_KEYS)} ({', '.join(filled) or '无'})")
    print()

    # 安全分支提示
    if section_key == "__safety__":
        print(
            "【安全优先】检测到自杀风险信号，已停止流程引导。"
            "请考虑联系当地心理危机干预热线或急救电话。"
        )
    elif section_key == "__done__":
        print("所有章节已填写完成。可以用 ending-note-share 共享给家人。")

    print()
    print(_DISCLAIMER)


def cmd_ending_note_share(args: argparse.Namespace) -> None:
    """ending-note-share --user-id STR --target-user-id STR [--sections STR]

    共享笔记给家庭成员。
    --sections：逗号分隔的章节 key 列表，缺省 = 全部章节
    """
    user_id = args.user_id
    target_user_id = args.target_user_id
    sections_str = getattr(args, "sections", None)

    sections: list[str] | None
    if sections_str:
        sections = [s.strip() for s in sections_str.split(",") if s.strip()]
        # 校验章节 key
        invalid = [s for s in sections if s not in SECTION_KEYS]
        if invalid:
            print(f"[错误] 未知章节: {invalid}")
            print(f"支持的章节: {', '.join(SECTION_KEYS)}")
            return
    else:
        sections = None  # 全部章节

    store = EndingNoteStore()
    try:
        store.share_with(user_id, target_user_id, sections)
    except ValueError as exc:
        print(f"[错误] {exc}")
        return

    print(f"已共享 {user_id} 的终活笔记给 {target_user_id}")
    if sections is None:
        print("  共享范围: 全部章节")
    else:
        print(f"  共享范围: {', '.join(sections)}")
    print()
    print(_DISCLAIMER)


def cmd_ending_note_completion(args: argparse.Namespace) -> None:
    """ending-note-completion --user-id STR

    计算并显示终活笔记的填写完整度。
    """
    user_id = args.user_id
    store = EndingNoteStore()
    note = store.load(user_id) or EndingNote.new(user_id)
    guide = EndingNoteGuide(store=store)
    rate = guide.completion_rate(note)

    print("=== 终活笔记完整度 ===")
    print(f"user_id: {user_id}")
    print(f"总完整度: {rate['overall']:.0%}")
    print()
    print(f"{'章节':<24} {'key':<22} {'已填写'}")
    print("-" * 60)
    for key, title in [(s[0], s[1]) for s in guide.SECTIONS]:
        score = rate["sections"].get(key, 0.0)
        mark = "✓" if score == 1.0 else " "
        print(f"{title:<24} {key:<22} [{mark}]")
    print()
    filled = sum(1 for v in rate["sections"].values() if v == 1.0)
    print(f"已填写章节: {filled}/{len(SECTION_KEYS)}")
    print()
    print(_DISCLAIMER)


# ====================================================================
# subparser 注册（供调用方按需挂载）
# ====================================================================


def register_subparsers(subparsers: Any) -> None:
    """把 Phase 10 的 4 个子命令挂载到 subparsers。

    用法：
        from deadman._cli_extensions import phase10
        phase10.register_subparsers(subparsers)
    """
    # ending-note-show
    show_parser = subparsers.add_parser(
        "ending-note-show",
        help="显示我的终活笔记内容（解密后输出）",
    )
    show_parser.add_argument(
        "--user-id", default="default-user", help="用户 ID"
    )
    show_parser.set_defaults(func=cmd_ending_note_show)

    # ending-note-guide
    guide_parser = subparsers.add_parser(
        "ending-note-guide",
        help="获取终活笔记引导问题（AI 引导填写）",
    )
    guide_parser.add_argument(
        "--user-id", default="default-user", help="用户 ID"
    )
    guide_parser.add_argument(
        "--section",
        help="指定章节 key（如 personal_info）；缺省 = 下一未填章节",
    )
    guide_parser.set_defaults(func=cmd_ending_note_guide)

    # ending-note-share
    share_parser = subparsers.add_parser(
        "ending-note-share",
        help="共享终活笔记给家庭成员",
    )
    share_parser.add_argument(
        "--user-id", required=True, help="笔记所有者用户 ID"
    )
    share_parser.add_argument(
        "--target-user-id", required=True, help="接收方用户 ID"
    )
    share_parser.add_argument(
        "--sections",
        help="共享章节（逗号分隔，如 personal_info,family_relations）；缺省 = 全部",
    )
    share_parser.set_defaults(func=cmd_ending_note_share)

    # ending-note-completion
    completion_parser = subparsers.add_parser(
        "ending-note-completion",
        help="显示终活笔记填写完整度",
    )
    completion_parser.add_argument(
        "--user-id", default="default-user", help="用户 ID"
    )
    completion_parser.set_defaults(func=cmd_ending_note_completion)


# ====================================================================
# 辅助
# ====================================================================


def _section_title(key: str) -> str:
    """章节 key → 中文标题"""
    titles = {
        "personal_info": "第一章：个人信息",
        "family_relations": "第二章：家庭关系",
        "assets": "第三章：资产清单",
        "funeral_wishes": "第四章：葬礼意愿",
        "medical_wishes": "第五章：医疗意愿",
        "digital_legacy": "第六章：数字遗产",
        "messages": "第七章：给家人的留言",
        "emergency_contacts": "第八章：重要联系人",
        "will_intent": "第九章：立遗嘱意向",
    }
    return titles.get(key, key)
