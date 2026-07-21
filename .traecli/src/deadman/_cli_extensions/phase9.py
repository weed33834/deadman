"""Phase 9 CLI 集成清单 - 法律免责显式告知 + 殡葬机构查询 + 官方热线转介

本文件不直接修改 cli.py，提供：
1. cmd_xxx 函数 - 子命令处理函数
2. register_subparser(subparsers) - 注册子命令到 argparse subparsers
3. COMMANDS 清单 - 供 cli.py 主入口引用

主智能体集成步骤：
    from deadman._cli_extensions.phase9 import register_subparser
    register_subparser(subparsers)  # 在 cli.py 的 main() 中调用

子命令清单：
    disclaimer-show [--scenario legal|agent|data|identity]
    hotline-lookup [--province STR] [--function STR]
    institution-search [--province STR] [--city STR] [--type STR] [--keyword STR]
    institution-import --file PATH
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# === 子命令处理函数 ===


def cmd_disclaimer_show(args) -> None:
    """显示免责告知

    不带 --scenario：打印完整开场告知（首次会话用）
    --scenario legal/agent/data/identity：打印对应场景简短提醒
    """
    from deadman.disclaimer.text import DisclaimerBuilder

    if getattr(args, "scenario", None):
        try:
            print(DisclaimerBuilder.short_reminder(args.scenario))
        except ValueError as exc:
            print(f"错误: {exc}")
            raise SystemExit(1)
    else:
        print(DisclaimerBuilder.full_opening())


def cmd_hotline_lookup(args) -> None:
    """查询热线

    --province 指定省份（不指定则只返回全国热线）
    --function 指定职能（殡葬服务/政策咨询/法律援助/心理援助/消费者投诉/社保咨询）
    """
    from deadman.hotlines.lookup import HotlineLookup

    results = HotlineLookup().lookup(args.province, args.function)
    if not results:
        print("未找到匹配的热线。建议拨打 12345 政务服务热线咨询。")
        return
    for r in results:
        scope_tag = f"[{r.get('scope', '?')}]"
        province_tag = f" {r.get('province', '')}" if r.get("province") else ""
        print(f"{scope_tag}{province_tag} {r['phone']}\t{r['note']}")
        print(f"  来源: {r.get('source', '未知')} (confidence={r.get('confidence', 0)})")
    print("\n提示: 电话/职能信息整理自公开资料，办理前请拨打官方热线核实。")


def cmd_institution_search(args) -> None:
    """搜索机构

    --province / --city / --type / --keyword 任一组合过滤
    """
    from deadman.institutions.store import InstitutionStore

    store = InstitutionStore()
    results = store.search(args.province, args.city, args.type, args.keyword)
    if not results:
        print("未找到机构。建议拨打 12345 政务热线咨询。")
        return
    print(f"共找到 {len(results)} 家机构：\n")
    for i in results:
        print(f"[{i.institution_id}] {i.name} ({i.type})")
        print(f"  地址: {i.address or '未公开'}")
        print(f"  电话: {i.phone or '未公开（建议拨打当地殡葬服务热线或 12345 核实）'}")
        print(f"  服务: {', '.join(i.services) if i.services else '未公开'}")
        print(f"  明码标价: {'是' if i.price_public else '未公开'}")
        print(f"  来源: {i.source} (confidence={i.confidence})")
        if i.needs_verification_warning():
            print("  ⚠ 此数据可信度较低，建议向官方核实")
        print()
    print("提示: 机构信息整理自公开资料，办理前请拨打官方热线核实。")


def cmd_institution_import(args) -> None:
    """从 JSON 文件批量导入机构

    --file PATH: JSON 文件路径，格式同 knowledge/institutions/seed.json
    每条记录的 source 字段会被 --source 参数覆盖（若提供）
    """
    from deadman.institutions.store import InstitutionStore

    file_path = Path(args.file)
    if not file_path.exists():
        print(f"错误: 文件不存在: {file_path}")
        raise SystemExit(1)
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"错误: JSON 解析失败: {exc}")
        raise SystemExit(1)
    records = data.get("institutions", []) if isinstance(data, dict) else data
    if not isinstance(records, list):
        print("错误: JSON 格式应为 {institutions: [...]} 或 [...]")
        raise SystemExit(1)

    source_name = getattr(args, "source", None) or file_path.stem
    store = InstitutionStore()
    added = store.import_from_official_source(source_name, records)
    print(f"导入完成：新增 {added} 条，当前共 {store.count()} 条机构。")


# === 子命令注册 ===


def register_subparsers(subparsers: Any) -> None:
    """注册 Phase 9 子命令到 argparse subparsers

    在 cli.py 的 main() 中调用：
        from deadman._cli_extensions.phase9 import register_subparsers
        register_subparsers(subparsers)
    """
    return register_subparser(subparsers)


def register_subparser(subparsers: Any) -> None:
    """旧函数名（保留向后兼容）"""
    # disclaimer-show
    p = subparsers.add_parser(
        "disclaimer-show",
        help="显示免责告知（首次会话开场或场景化提醒）",
        description="显示 deadman 平台的免责告知文本",
    )
    p.add_argument(
        "--scenario",
        choices=["legal", "agent", "data", "identity"],
        default=None,
        help="场景化简短提醒（不指定则打印完整开场告知）",
    )
    p.set_defaults(func=cmd_disclaimer_show)

    # hotline-lookup
    p = subparsers.add_parser(
        "hotline-lookup",
        help="查询官方热线（殡葬服务/政策咨询/法律援助等）",
        description="查询官方热线（全国 + 省级）",
    )
    p.add_argument("--province", default=None, help="省份（如 北京/上海/重庆/山东/安徽铜陵）")
    p.add_argument(
        "--function",
        default=None,
        help="职能（殡葬服务/政策咨询/法律援助/心理援助/消费者投诉/社保咨询）",
    )
    p.set_defaults(func=cmd_hotline_lookup)

    # institution-search
    p = subparsers.add_parser(
        "institution-search",
        help="搜索殡葬机构（殡仪馆/火化场/公墓/殡仪服务站）",
        description="搜索殡葬机构",
    )
    p.add_argument("--province", default=None, help="省份过滤")
    p.add_argument("--city", default=None, help="城市过滤")
    p.add_argument(
        "--type",
        default=None,
        choices=["funeral_home", "crematorium", "cemetery", "funeral_service_station"],
        help="机构类型过滤",
    )
    p.add_argument("--keyword", default=None, help="关键词（在名称/地址/服务中模糊匹配）")
    p.set_defaults(func=cmd_institution_search)

    # institution-import
    p = subparsers.add_parser(
        "institution-import",
        help="从 JSON 文件批量导入机构",
        description="从 JSON 文件批量导入机构（格式同 seed.json）",
    )
    p.add_argument("--file", required=True, help="JSON 文件路径")
    p.add_argument("--source", default=None, help="数据来源标注（默认使用文件名）")
    p.set_defaults(func=cmd_institution_import)


# === 命令清单（供 cli.py 引用）===
COMMANDS = [
    "disclaimer-show",
    "hotline-lookup",
    "institution-search",
    "institution-import",
]
