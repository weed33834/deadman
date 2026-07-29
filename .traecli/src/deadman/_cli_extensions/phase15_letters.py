"""Phase 15 CLI 集成清单 - 通知信函生成器（notification_letters）

本模块定义 Phase 15 的 3 个 CLI 子命令实现 + register_subparsers 函数。
不修改 `deadman.cli.main()`；cli.py 用 `from ._cli_extensions import phase15_letters`
+ `phase15_letters.register_subparsers(subparsers)` 自动挂载。

子命令清单：
    letter-generate     生成通知信函（必填：--type/--name/--id-masked/
                        --death-date/--applicant/--relationship/--recipient；
                        可选：--extra key=val（可重复）、--use-llm）
    letter-list-types   列出 8 种信函类型 + 每种需要的字段
    letter-template     打印原始模板（不填充）

合规关联：
    - 所有输出末尾附"信函仅为草稿"边界告知
      （service-boundary-framework.md 第三章）
    - 信函中真实姓名/身份证号/账号默认由调用方脱敏传入
      （legal-compliance-framework.md 第五章 PIPL）
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

_DISCLAIMER = (
    "【边界告知】信函仅为草稿，具体格式请以办理机构要求为准；"
    "占位符 [xxx] 需手动填写。"
)


# ====================================================================
# 子命令实现
# ====================================================================


def cmd_letter_generate(args: argparse.Namespace) -> None:
    """letter-generate 生成通知信函

    选项：
        --type STR          信函类型（8 类之一）
        --name STR          逝者姓名（建议脱敏，如"张**"）
        --id-masked STR     已脱敏的逝者身份证号（如 110101********1234）
        --death-date STR    死亡日期（YYYY-MM-DD）
        --applicant STR     申请人姓名
        --relationship STR  申请人与逝者关系
        --recipient STR     收件机构
        --extra key=val     可重复，类型特定字段
        --use-llm           可选，启用 LLM 语气优化
    """
    from deadman.notification_letters import (
        LetterGenerator,
        LetterRequest,
    )

    # 解析 --extra key=val
    extra_fields: dict[str, str] = {}
    for pair in args.extra or []:
        if "=" not in pair:
            print(f"[错误] --extra 必须是 key=val 格式: {pair}")
            sys.exit(1)
        key, _, val = pair.partition("=")
        extra_fields[key.strip()] = val

    request = LetterRequest(
        letter_type=args.type,
        decedent_name=args.name,
        decedent_id_masked=args.id_masked,
        death_date=args.death_date,
        applicant_name=args.applicant,
        applicant_relationship=args.relationship,
        recipient_org=args.recipient,
        extra_fields=extra_fields,
    )

    generator = LetterGenerator(use_llm=bool(args.use_llm))
    try:
        result = generator.generate(request)
    except ValueError as exc:
        print(f"[错误] {exc}")
        sys.exit(1)

    print(f"=== {result.letter_type} 信函 ===")
    print(f"confidence: {result.confidence}")
    print()
    print(result.text)
    print()
    if result.placeholders:
        print(f"## 待手动填写的占位符（{len(result.placeholders)} 个）")
        for p in result.placeholders:
            print(f"  - {p}")
        print()
    print(_DISCLAIMER)


def cmd_letter_list_types(args: argparse.Namespace) -> None:
    """letter-list-types 列出 8 种信函类型 + 每种需要的字段"""
    from deadman.notification_letters.templates import LETTER_TYPES

    print(f"=== 支持的信函类型（共 {len(LETTER_TYPES)} 种）===")
    print()
    for i, item in enumerate(LETTER_TYPES, 1):
        print(f"{i}. {item['name']} ({item['type']})")
        print(f"   收件机构默认：{item['recipient_default']}")
        print(f"   说明：{item['description']}")
        if item.get("extra_fields_needed"):
            print("   类型特定字段（--extra 传入）：")
            for f in item["extra_fields_needed"]:
                print(f"     - {f}")
        print()


def cmd_letter_template(args: argparse.Namespace) -> None:
    """letter-template --type STR 打印原始模板（不填充）"""
    from deadman.notification_letters.templates import (
        LETTER_TEMPLATES,
        LETTER_TYPES,
    )

    letter_type = args.type
    if letter_type not in LETTER_TEMPLATES:
        print(f"[错误] 未知信函类型: {letter_type}")
        names = [t["type"] for t in LETTER_TYPES]
        print(f"支持的类型: {', '.join(names)}")
        sys.exit(1)

    meta = next(
        (t for t in LETTER_TYPES if t["type"] == letter_type), None
    )
    if meta:
        print(f"=== {meta['name']} 原始模板 ===")
        print(f"type: {letter_type}")
        print(f"默认收件机构: {meta['recipient_default']}")
        print(f"类型特定字段: {meta.get('extra_fields_needed', [])}")
        print()
    print(LETTER_TEMPLATES[letter_type])
    print(_DISCLAIMER)


# ====================================================================
# subparser 注册
# ====================================================================


def register_subparsers(subparsers: Any) -> None:
    """把 Phase 15 的 3 个子命令挂载到 subparsers。"""
    # letter-generate
    gen_parser = subparsers.add_parser(
        "letter-generate",
        help="生成通知信函（8 类之一）",
    )
    gen_parser.add_argument(
        "--type", required=True,
        help="信函类型（如 household_cancellation）",
    )
    gen_parser.add_argument(
        "--name", required=True,
        help="逝者姓名（建议脱敏，如 '张**'）",
    )
    gen_parser.add_argument(
        "--id-masked", required=True,
        help="已脱敏的逝者身份证号（如 110101********1234）",
    )
    gen_parser.add_argument(
        "--death-date", required=True,
        help="死亡日期（YYYY-MM-DD）",
    )
    gen_parser.add_argument(
        "--applicant", required=True,
        help="申请人姓名",
    )
    gen_parser.add_argument(
        "--relationship", required=True,
        help="申请人与逝者关系（配偶/子女/父母/兄弟姐妹/其他）",
    )
    gen_parser.add_argument(
        "--recipient", required=True,
        help="收件机构（如 '户籍所在地派出所'）",
    )
    gen_parser.add_argument(
        "--extra", action="append", default=[],
        help="类型特定字段，格式 key=val（可重复）",
    )
    gen_parser.add_argument(
        "--use-llm", action="store_true",
        help="启用 LLM 语气优化（默认禁用，纯模板填充）",
    )
    gen_parser.set_defaults(func=cmd_letter_generate)

    # letter-list-types
    list_parser = subparsers.add_parser(
        "letter-list-types",
        help="列出 8 种通知信函类型 + 每种需要的字段",
    )
    list_parser.set_defaults(func=cmd_letter_list_types)

    # letter-template
    tpl_parser = subparsers.add_parser(
        "letter-template",
        help="打印原始信函模板（不填充）",
    )
    tpl_parser.add_argument(
        "--type", required=True,
        help="信函类型（如 household_cancellation）",
    )
    tpl_parser.set_defaults(func=cmd_letter_template)
