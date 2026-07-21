"""Phase 11/12/13 CLI 集成清单

包含 18 个子命令：

Phase 11 (vault) - 7 个:
    vault-add / vault-list / vault-get / vault-delete /
    vault-beneficiaries / vault-inherited / vault-trigger

Phase 12 (doc_extract) - 4 个:
    doc-extract / doc-list / doc-get / doc-delete

Phase 13 (decedent_id) - 6 个:
    case-create / case-list / case-get / case-event-add /
    case-archive / case-timeline

用法（在 cli.py main() 中）:
    from ._cli_extensions.phase11_12_13 import register_subparsers, dispatch
    register_subparsers(subparsers)
    if dispatch(args.command, args):
        return
    # ... 原 cli.py 的 if/elif 分支
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any


# =====================================================================
# Phase 11: Vault 命令实现
# =====================================================================
def cmd_vault_add(args: argparse.Namespace) -> None:
    """vault-add: 添加条目

    选项：--user USER_ID, --type TYPE, --title TITLE, --content TEXT,
         --beneficiary ID（可重复）, --trigger immediate|on_death|on_date|manual,
         --delivery-date ISO, --metadata JSON
    """
    from deadman.vault.store import VaultStore
    store = VaultStore()
    content = args.content
    if args.content_file:
        with open(args.content_file, "rb") as f:
            content = f.read()
    metadata = {}
    if args.metadata:
        try:
            metadata = json.loads(args.metadata)
        except json.JSONDecodeError as exc:
            print(f"[错误] --metadata 不是合法 JSON: {exc}")
            sys.exit(1)
    from datetime import datetime
    delivery_date = None
    if args.delivery_date:
        try:
            delivery_date = datetime.fromisoformat(args.delivery_date)
        except ValueError as exc:
            print(f"[错误] --delivery-date 解析失败: {exc}")
            sys.exit(1)
    item = store.add_item(
        owner_user_id=args.user,
        type=args.type,
        title=args.title,
        content=content,
        beneficiary_user_ids=args.beneficiary,
        delivery_trigger=args.trigger,
        delivery_date=delivery_date,
        metadata=metadata,
    )
    print(f"已添加条目：{item.item_id}")
    print(f"  title: {item.title}")
    print(f"  type: {item.type}")
    print(f"  beneficiaries: {item.beneficiary_user_ids}")
    print(f"  trigger: {item.delivery_trigger}")


def cmd_vault_list(args: argparse.Namespace) -> None:
    """vault-list: 列出我的条目（仅元数据）"""
    from deadman.vault.store import VaultStore
    store = VaultStore()
    items = store.list_items(args.user, args.user)
    if not items:
        print("(无条目)")
        return
    print(f"共 {len(items)} 条：")
    for it in items:
        print(f"  [{it.get('item_id', '')[:16]}] {it.get('title', '')} "
              f"(type={it.get('type', '')}, trigger={it.get('delivery_trigger', '')}, "
              f"beneficiaries={it.get('beneficiary_user_ids', [])})")


def cmd_vault_get(args: argparse.Namespace) -> None:
    """vault-get: 获取条目详情（仅元数据，不解密 content）"""
    from deadman.vault.store import VaultStore
    store = VaultStore()
    item = store.get_item(args.item_id, args.user)
    if item is None:
        print("[错误] 条目不存在或无权限")
        sys.exit(1)
    print(json.dumps(item.to_index_dict(), ensure_ascii=False, indent=2))


def cmd_vault_delete(args: argparse.Namespace) -> None:
    """vault-delete: 删除条目"""
    from deadman.vault.store import VaultStore
    store = VaultStore()
    ok = store.delete_item(args.item_id, args.user)
    if ok:
        print(f"已删除：{args.item_id}")
    else:
        print(f"[错误] 条目不存在或无权限：{args.item_id}")
        sys.exit(1)


def cmd_vault_beneficiaries(args: argparse.Namespace) -> None:
    """vault-beneficiaries: 列出我指定的受益人"""
    from deadman.vault.store import VaultStore
    store = VaultStore()
    beneficiaries = store.list_beneficiaries(args.user)
    if not beneficiaries:
        print("(未指定任何受益人)")
        return
    print(f"共 {len(beneficiaries)} 位受益人：")
    for b in beneficiaries:
        print(f"  {b['beneficiary_user_id']} - 涉及 {b['item_count']} 条")


def cmd_vault_inherited(args: argparse.Namespace) -> None:
    """vault-inherited: 列出我能继承的条目"""
    from deadman.vault.store import VaultStore
    store = VaultStore()
    inherited = store.list_inherited(args.user)
    if not inherited:
        print("(无可继承条目)")
        return
    print(f"共 {len(inherited)} 条可继承：")
    for e in inherited:
        print(f"  [{e['item_id'][:16]}] from {e['owner_user_id']} - "
              f"{e['title']} (status={e['status']}, trigger={e['delivery_trigger']})")


def cmd_vault_trigger(args: argparse.Namespace) -> None:
    """vault-trigger: 触发投递"""
    from deadman.vault.store import VaultStore
    store = VaultStore()
    result = store.trigger_delivery(args.item_id, args.trigger_type, args.user)
    print(json.dumps({
        "delivered": result["delivered"],
        "pending_days": result["pending_days"],
        "reason": result["reason"],
        "content_bytes": len(result["content"]) if result.get("content") else 0,
    }, ensure_ascii=False, indent=2))
    if result.get("content"):
        # 默认不打印解密内容，避免泄露；用 --show-content 显式开启
        if getattr(args, "show_content", False):
            try:
                print("--- content ---")
                print(result["content"].decode("utf-8"))
            except UnicodeDecodeError:
                print(f"(二进制内容 {len(result['content'])} 字节)")


# =====================================================================
# Phase 12: Document Extract 命令实现
# =====================================================================
def cmd_doc_extract(args: argparse.Namespace) -> None:
    """doc-extract: 上传文档并提取

    选项：--file PATH, --type will|trust|insurance|property|bank_statement|id_card|other,
         --user USER_ID
    """
    from deadman.doc_extract.extractor import DocumentExtractor
    extractor = DocumentExtractor()
    with open(args.file, "rb") as f:
        content = f.read()
    import os
    filename = os.path.basename(args.file)
    doc = asyncio.run(
        extractor.extract(
            owner_user_id=args.user,
            filename=filename,
            content=content,
            doc_type_hint=args.type,
        )
    )
    print(f"已提取文档：{doc.doc_id}")
    print(f"  filename: {doc.filename}")
    print(f"  file_type: {doc.file_type}")
    print(f"  doc_type: {doc.doc_type}")
    print(f"  confidence: {doc.confidence:.2f}")
    print(f"  summary: {doc.summary}")
    if doc.key_fields:
        print("  key_fields:")
        for k, v in doc.key_fields.items():
            print(f"    {k}: {v}")


def cmd_doc_list(args: argparse.Namespace) -> None:
    """doc-list: 列出我的文档"""
    from deadman.doc_extract.extractor import DocumentExtractor
    extractor = DocumentExtractor()
    docs = extractor.list_my_documents(args.user)
    if not docs:
        print("(无文档)")
        return
    print(f"共 {len(docs)} 篇文档：")
    for d in docs:
        print(f"  [{d.doc_id[:16]}] {d.filename} "
              f"(type={d.doc_type}, confidence={d.confidence:.2f})")


def cmd_doc_get(args: argparse.Namespace) -> None:
    """doc-get: 文档详情"""
    from deadman.doc_extract.extractor import DocumentExtractor
    extractor = DocumentExtractor()
    doc = extractor.get_document(args.doc_id, args.user)
    if doc is None:
        print("[错误] 文档不存在或无权限")
        sys.exit(1)
    print(json.dumps(doc.to_dict(), ensure_ascii=False, indent=2))


def cmd_doc_delete(args: argparse.Namespace) -> None:
    """doc-delete: 删除文档"""
    from deadman.doc_extract.extractor import DocumentExtractor
    extractor = DocumentExtractor()
    ok = extractor.delete_document(args.doc_id, args.user)
    if ok:
        print(f"已删除：{args.doc_id}")
    else:
        print(f"[错误] 文档不存在或无权限：{args.doc_id}")
        sys.exit(1)


# =====================================================================
# Phase 13: Decedent ID 命令实现
# =====================================================================
def cmd_case_create(args: argparse.Namespace) -> None:
    """case-create: 创建案例"""
    from deadman.decedent_id.registry import DecedentRegistry
    reg = DecedentRegistry()
    case = reg.create_case(
        owner_user_id=args.user,
        decedent_alias=args.alias,
        relationship=args.relationship,
    )
    print(f"已创建案例：{case.case_id}")
    print(f"  alias: {case.decedent_alias}")
    print(f"  relationship: {case.relationship}")
    print(f"  status: {case.status}")


def cmd_case_list(args: argparse.Namespace) -> None:
    """case-list: 列出我的案例"""
    from deadman.decedent_id.registry import DecedentRegistry
    reg = DecedentRegistry()
    cases = reg.list_cases(args.user)
    if not cases:
        print("(无案例)")
        return
    print(f"共 {len(cases)} 个案例：")
    for c in cases:
        print(f"  [{c.case_id[:16]}] {c.decedent_alias} ({c.relationship}) "
              f"status={c.status}, events={len(c.events)}")


def cmd_case_get(args: argparse.Namespace) -> None:
    """case-get: 案例详情"""
    from deadman.decedent_id.registry import DecedentRegistry
    reg = DecedentRegistry()
    case = reg.get_case(args.case_id, args.user)
    if case is None:
        print("[错误] 案例不存在或无权限")
        sys.exit(1)
    print(json.dumps(case.to_dict(), ensure_ascii=False, indent=2))


def cmd_case_event_add(args: argparse.Namespace) -> None:
    """case-event-add: 添加事件"""
    from deadman.decedent_id.registry import DecedentRegistry
    reg = DecedentRegistry()
    case = reg.add_event(
        case_id=args.case_id,
        owner_user_id=args.user,
        event=args.event,
        agent=args.agent,
        notes=args.notes or "",
    )
    if case is None:
        print("[错误] 案例不存在或无权限")
        sys.exit(1)
    print(f"已添加事件到案例 {case.case_id}，当前共 {len(case.events)} 个事件")


def cmd_case_archive(args: argparse.Namespace) -> None:
    """case-archive: 归档案例"""
    from deadman.decedent_id.registry import DecedentRegistry
    reg = DecedentRegistry()
    ok = reg.archive_case(args.case_id, args.user)
    if ok:
        print(f"已归档案例：{args.case_id}")
    else:
        print(f"[错误] 案例不存在或无权限：{args.case_id}")
        sys.exit(1)


def cmd_case_timeline(args: argparse.Namespace) -> None:
    """case-timeline: 获取时间线"""
    from deadman.decedent_id.registry import DecedentRegistry
    reg = DecedentRegistry()
    timeline = reg.get_timeline(args.case_id, args.user)
    if not timeline:
        print("(无事件)")
        return
    print(f"共 {len(timeline)} 个事件：")
    for e in timeline:
        print(f"  [{e.get('timestamp', '')[:19]}] "
              f"{e.get('event', '')} (by {e.get('agent', '')})")
        if e.get("notes"):
            print(f"      notes: {e['notes']}")


# =====================================================================
# subparser 注册
# =====================================================================
# 命令名 → 处理函数 映射（dispatch 用）
_COMMAND_MAP: dict[str, Any] = {}


def register_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """注册 Phase 11/12/13 共 17 个子命令（vault 7 + doc 4 + case 6 = 17）

    18 个 subparser 写法上为 17（vault-trigger 算 1 个，doc 4 个，case 6 个 = 17）；
    本注释留出空间以备后续 phase 扩展。
    """
    # ---------- Phase 11: Vault ----------
    va = subparsers.add_parser("vault-add", help="添加保险库条目")
    va.add_argument("--user", required=True, help="owner 用户 ID")
    va.add_argument("--type", required=True,
                    help="类型（password/document/photo/video/audio/note/account/crypto）")
    va.add_argument("--title", required=True, help="条目标题")
    va.add_argument("--content", help="内容（文本）。与 --content-file 二选一")
    va.add_argument("--content-file", help="从文件读取内容（二进制安全）")
    va.add_argument("--beneficiary", action="append", default=[],
                    help="受益人 user_id（可重复）")
    va.add_argument("--trigger", default="manual",
                    choices=["immediate", "on_death", "on_date", "manual"],
                    help="投递触发方式")
    va.add_argument("--delivery-date", help="on_date 触发时的目标时间 ISO 格式")
    va.add_argument("--metadata", help="附加元数据 JSON 字符串")
    _COMMAND_MAP["vault-add"] = cmd_vault_add

    vl = subparsers.add_parser("vault-list", help="列出我的保险库条目")
    vl.add_argument("--user", required=True, help="用户 ID")
    _COMMAND_MAP["vault-list"] = cmd_vault_list

    vg = subparsers.add_parser("vault-get", help="获取保险库条目详情")
    vg.add_argument("--user", required=True, help="用户 ID")
    vg.add_argument("--item-id", required=True, help="条目 ID")
    _COMMAND_MAP["vault-get"] = cmd_vault_get

    vd = subparsers.add_parser("vault-delete", help="删除保险库条目")
    vd.add_argument("--user", required=True, help="用户 ID")
    vd.add_argument("--item-id", required=True, help="条目 ID")
    _COMMAND_MAP["vault-delete"] = cmd_vault_delete

    vb = subparsers.add_parser("vault-beneficiaries", help="列出我指定的受益人")
    vb.add_argument("--user", required=True, help="用户 ID")
    _COMMAND_MAP["vault-beneficiaries"] = cmd_vault_beneficiaries

    vi = subparsers.add_parser("vault-inherited", help="列出我能继承的条目")
    vi.add_argument("--user", required=True, help="用户 ID")
    _COMMAND_MAP["vault-inherited"] = cmd_vault_inherited

    vt = subparsers.add_parser("vault-trigger", help="触发保险库条目投递")
    vt.add_argument("--user", required=True, help="用户 ID")
    vt.add_argument("--item-id", required=True, help="条目 ID")
    vt.add_argument("--trigger-type", required=True,
                    choices=["on_death", "on_date", "manual"],
                    help="触发类型")
    vt.add_argument("--show-content", action="store_true",
                    help="显示解密后的内容（默认隐藏）")
    _COMMAND_MAP["vault-trigger"] = cmd_vault_trigger

    # ---------- Phase 12: Document Extract ----------
    de = subparsers.add_parser("doc-extract", help="上传文档并 AI 提取摘要")
    de.add_argument("--user", required=True, help="用户 ID")
    de.add_argument("--file", required=True, help="文件路径")
    de.add_argument("--type", default=None,
                    choices=["will", "trust", "insurance", "property",
                             "bank_statement", "id_card", "other"],
                    help="文档类型提示")
    _COMMAND_MAP["doc-extract"] = cmd_doc_extract

    dl = subparsers.add_parser("doc-list", help="列出我的文档")
    dl.add_argument("--user", required=True, help="用户 ID")
    _COMMAND_MAP["doc-list"] = cmd_doc_list

    dg = subparsers.add_parser("doc-get", help="获取文档详情")
    dg.add_argument("--user", required=True, help="用户 ID")
    dg.add_argument("--doc-id", required=True, help="文档 ID")
    _COMMAND_MAP["doc-get"] = cmd_doc_get

    dd = subparsers.add_parser("doc-delete", help="删除文档")
    dd.add_argument("--user", required=True, help="用户 ID")
    dd.add_argument("--doc-id", required=True, help="文档 ID")
    _COMMAND_MAP["doc-delete"] = cmd_doc_delete

    # ---------- Phase 13: Decedent ID ----------
    cc = subparsers.add_parser("case-create", help="创建逝者案例（遗码通）")
    cc.add_argument("--user", required=True, help="用户 ID")
    cc.add_argument("--alias", required=True, help="逝者化名（如我父亲，不存真实姓名）")
    cc.add_argument("--relationship", required=True,
                    choices=["配偶", "子女", "父母", "兄弟姐妹", "祖父母", "孙辈", "其他"],
                    help="与逝者的关系")
    _COMMAND_MAP["case-create"] = cmd_case_create

    cl = subparsers.add_parser("case-list", help="列出我的逝者案例")
    cl.add_argument("--user", required=True, help="用户 ID")
    _COMMAND_MAP["case-list"] = cmd_case_list

    cg = subparsers.add_parser("case-get", help="获取案例详情")
    cg.add_argument("--user", required=True, help="用户 ID")
    cg.add_argument("--case-id", required=True, help="案例 ID")
    _COMMAND_MAP["case-get"] = cmd_case_get

    ce = subparsers.add_parser("case-event-add", help="向案例添加时间线事件")
    ce.add_argument("--user", required=True, help="用户 ID")
    ce.add_argument("--case-id", required=True, help="案例 ID")
    ce.add_argument("--event", required=True, help="事件描述")
    ce.add_argument("--agent", required=True, help="触发 agent 名称")
    ce.add_argument("--notes", default="", help="备注（可选）")
    _COMMAND_MAP["case-event-add"] = cmd_case_event_add

    ca = subparsers.add_parser("case-archive", help="归档案例")
    ca.add_argument("--user", required=True, help="用户 ID")
    ca.add_argument("--case-id", required=True, help="案例 ID")
    _COMMAND_MAP["case-archive"] = cmd_case_archive

    ct = subparsers.add_parser("case-timeline", help="获取案例时间线")
    ct.add_argument("--user", required=True, help="用户 ID")
    ct.add_argument("--case-id", required=True, help="案例 ID")
    _COMMAND_MAP["case-timeline"] = cmd_case_timeline


def dispatch(command: str | None, args: argparse.Namespace) -> bool:
    """分发命令。若 command 属于 Phase 11/12/13 之一则执行并返回 True，
    否则返回 False（让 cli.py 继续走原 if/elif 分支）。
    """
    handler = _COMMAND_MAP.get(command or "")
    if handler is None:
        return False
    handler(args)
    return True
