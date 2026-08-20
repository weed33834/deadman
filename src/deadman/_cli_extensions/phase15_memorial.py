"""Phase 15 CLI 集成清单 - AI 悼文/讣告/答谢词/墓志铭/追思会致辞生成

参考竞品 Toast + Empathy（Toast 已用此功能服务 70000+ 家庭）。

子命令清单（2 个）：
    memorial-generate --type STR --name STR [--relationship STR]
                      [--traits a,b,c] [--memories "x|y"] [--values "x|y"]
                      [--tone solemn|warm|humorous]
                      [--faith none|buddhist|taoist|christian]
                      [--language zh-CN|en-US|zh-Classical]
                      [--limit INT]
    memorial-list-types    列出 5 种文档类型说明

合规关联：
    - service-boundary-framework.md：所有输出末尾附"AI 生成仅供参考"边界告知
    - integrity-framework.md：用户未提供的特质/回忆不编造（由 generator 保证）
    - PIPL 第五章：decedent_name 不落盘（仅本次生成使用）
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from ..memorial_writer.generator import MemorialGenerator
from ..memorial_writer.models import (
    DOC_TYPES,
    VALID_FAITHS,
    VALID_LANGUAGES,
    VALID_TONES,
    MemorialRequest,
)

_DISCLAIMER = (
    "【边界告知】AI 生成的悼文仅供参考，建议家属审阅修改后使用。"
    "本平台不代办殡葬服务，不与殡葬机构分成。"
)


# ====================================================================
# 子命令实现
# ====================================================================


def _split_csv(s: str | None) -> list[str]:
    """逗号分隔 → list；空 → []"""
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def _split_pipe(s: str | None) -> list[str]:
    """竖线分隔 → list；空 → []

    用 | 而非 , 是因为 memories/values 内部可能含逗号。
    """
    if not s:
        return []
    return [x.strip() for x in s.split("|") if x.strip()]


def cmd_memorial_generate(args: argparse.Namespace) -> None:
    """memorial-generate - 生成悼文/讣告/答谢词/墓志铭/追思会致辞

    把 CLI 参数组装为 MemorialRequest，调 MemorialGenerator.generate()，
    打印主稿 + confidence + safety_flags。
    """
    req = MemorialRequest(
        doc_type=args.type,
        decedent_name=args.name,
        relationship=args.relationship,
        personality_traits=_split_csv(args.traits),
        memories=_split_pipe(args.memories),
        values_or_sayings=_split_pipe(args.values),
        tone=args.tone,
        faith=args.faith,
        language=args.language,
        word_limit=args.limit,
    )

    errors = req.validate()
    if errors:
        print("[错误] 参数校验失败：")
        for e in errors:
            print(f"  - {e}")
        raise SystemExit(1)

    gen = MemorialGenerator()
    try:
        result = asyncio.run(gen.generate(req))
    except Exception as exc:
        print(f"[错误] 生成失败：{exc}")
        raise SystemExit(1) from None

    doc_meta = DOC_TYPES.get(req.doc_type, {})
    print(f"=== {doc_meta.get('name', req.doc_type)} ===")
    print(f"doc_type:   {result.doc_type}")
    print(f"confidence: {result.confidence:.2f}")
    flags = result.safety_flags or {}
    if any(flags.values()):
        print(f"safety_flags: ⚠ {flags}")
    else:
        print(f"safety_flags: ✓ {flags}")
    print()
    print(result.text)
    print()
    print(_DISCLAIMER)


def cmd_memorial_list_types(args: argparse.Namespace) -> None:
    """memorial-list-types - 列出 5 种文档类型说明"""
    print("=== 悼文撰写支持的文档类型 ===\n")
    for key, meta in DOC_TYPES.items():
        word_lo, word_hi = meta["word_range"]
        print(f"[{key}] {meta['name']}（{meta['name_en']}）")
        print(f"  说明：{meta['description']}")
        print(f"  字数范围：{word_lo}-{word_hi} 字")
        print()
    print("支持的语气：", ", ".join(VALID_TONES))
    print("支持的信仰：", ", ".join(VALID_FAITHS))
    print("支持的语言：", ", ".join(VALID_LANGUAGES))
    print()
    print(_DISCLAIMER)


# ====================================================================
# subparser 注册
# ====================================================================


def register_subparsers(subparsers: Any) -> None:
    """注册 Phase 15 的 2 个子命令到 subparsers。

    用法：
        from deadman._cli_extensions import phase15_memorial
        phase15_memorial.register_subparsers(subparsers)
    """
    return register_subparser(subparsers)


def register_subparser(subparsers: Any) -> None:
    """注册子命令（单数形式，向后兼容）"""
    # memorial-generate
    gen_parser = subparsers.add_parser(
        "memorial-generate",
        help="AI 生成悼文/讣告/答谢词/墓志铭/追思会致辞",
        description="基于用户提供的信息生成悼文类文本",
    )
    gen_parser.add_argument(
        "--type",
        required=True,
        choices=list(DOC_TYPES.keys()),
        help="文档类型",
    )
    gen_parser.add_argument("--name", required=True, help="逝者姓名或称呼（可化名）")
    gen_parser.add_argument(
        "--relationship", default="家属", help="与逝者的关系（如 儿子/配偶/孙女）"
    )
    gen_parser.add_argument(
        "--traits",
        default=None,
        help="性格特质，逗号分隔（如 宽厚,爱读书,勤俭）",
    )
    gen_parser.add_argument(
        "--memories",
        default=None,
        help='共同回忆，竖线分隔（如 "每天早晨浇花|教我骑自行车"）',
    )
    gen_parser.add_argument(
        "--values",
        default=None,
        help='价值观/口头禅，竖线分隔（如 "做人要厚道|吃亏是福"）',
    )
    gen_parser.add_argument(
        "--tone",
        default="solemn",
        choices=list(VALID_TONES),
        help="语气（solemn 庄重 / warm 温暖 / humorous 幽默但得体）",
    )
    gen_parser.add_argument(
        "--faith",
        default="none",
        choices=list(VALID_FAITHS),
        help="信仰背景",
    )
    gen_parser.add_argument(
        "--language",
        default="zh-CN",
        choices=list(VALID_LANGUAGES),
        help="语言（zh-CN 现代文 / zh-Classical 古文 / en-US 英文）",
    )
    gen_parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="字数上限（0 = 用文档类型默认范围）",
    )
    gen_parser.set_defaults(func=cmd_memorial_generate)

    # memorial-list-types
    list_parser = subparsers.add_parser(
        "memorial-list-types",
        help="列出悼文撰写支持的文档类型",
        description="列出 5 种文档类型说明",
    )
    list_parser.set_defaults(func=cmd_memorial_list_types)


# ====================================================================
# 命令清单（供 cli.py 引用）
# ====================================================================
COMMANDS = [
    "memorial-generate",
    "memorial-list-types",
]
