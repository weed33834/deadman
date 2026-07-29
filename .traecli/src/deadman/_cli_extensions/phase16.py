"""Phase 16 CLI 集成清单 - 把 Phase 16 新增能力暴露到 CLI

包含 13 个子命令，分布在 5 类能力：

Support Ticket（5 个）：
    ticket-create    --user-id --category --priority --subject --description
    ticket-list      --user-id [--status]
    ticket-get       --ticket-id --user-id
    ticket-reply     --ticket-id --user-id --content
    ticket-close    --ticket-id --user-id

Onboarding（3 个）：
    onboarding-show  --user-id
    onboarding-save  --user-id --relationship --location [--death-date]
                     [--current-stage] [--consent-disclaimer]
    onboarding-steps （无参数，列出 5 步问题）

Knowledge Freshness（2 个）：
    knowledge-freshness-scan    [--regions-dir]
    knowledge-freshness-check   --file-path

CN Search（2 个）：
    search-baidu     --query [--max-results]
    search-bing-cn   --query [--max-results]

WeChat Webhook 测试（1 个）：
    wechat-webhook-test  --token --timestamp --nonce --signature [--echostr]

合规关联：
    - integrity-framework.md：失败用 logger.warning + 友好提示，不抛异常退出
    - transparency-framework.md L5：边界告知
    - service-boundary-framework.md：搜索/工单结果仅供参考
    - PIPL：PII 不在 CLI 输出中暴露原值
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_DISCLAIMER = (
    "【边界告知】本工具为辅助信息整理，不替代官方渠道核实；"
    "涉及具体金额/时限/法条时，建议向官方机构电话确认。"
)


# =====================================================================
# 1. Support Ticket 子命令
# =====================================================================

def cmd_ticket_create(args: argparse.Namespace) -> None:
    """ticket-create：创建客服工单"""

    store = _make_ticket_store(args)
    try:
        ticket = store.create_ticket(
            user_id=args.user_id,
            category=args.category,
            priority=args.priority,
            subject=args.subject,
            description=args.description,
        )
    except ValueError as exc:
        logger.warning("ticket-create 参数校验失败: %s", exc)
        print(f"[错误] 工单创建失败：{exc}")
        return
    except Exception as exc:
        logger.warning("ticket-create 异常: %s: %s", type(exc).__name__, exc)
        print(f"[错误] 工单创建异常：{type(exc).__name__}: {exc}")
        return
    print(f"已创建工单：ticket_id={ticket.ticket_id}")
    print(f"  user_id:     {ticket.user_id}")
    print(f"  category:    {ticket.category}")
    print(f"  priority:    {ticket.priority}")
    print(f"  subject:     {ticket.subject}")
    print(f"  status:      {ticket.status}")
    print(f"  created_at:  {ticket.created_at}")


def cmd_ticket_list(args: argparse.Namespace) -> None:
    """ticket-list：列出当前用户的工单（可按状态过滤）"""

    store = _make_ticket_store(args)
    tickets = store.list_user_tickets(args.user_id)
    if args.status:
        tickets = [t for t in tickets if t.status == args.status]
    if not tickets:
        print(f"(用户 {args.user_id} 暂无工单")
        if args.status:
            print(f"  过滤状态：{args.status}")
        print(")")
        return
    print(f"=== 用户 {args.user_id} 的工单（共 {len(tickets)} 条）===")
    if args.status:
        print(f"  过滤状态：{args.status}")
    for t in tickets:
        print(
            f"  - [{t.ticket_id}] [{t.status}] [{t.priority}] "
            f"{t.subject}  (created: {t.created_at})"
        )


def cmd_ticket_get(args: argparse.Namespace) -> None:
    """ticket-get：查看工单详情（越权返回提示）"""

    store = _make_ticket_store(args)
    ticket = store.get_ticket(args.ticket_id, user_id=args.user_id)
    if ticket is None:
        logger.warning(
            "ticket-get: 工单 %s 不存在或越权访问（user_id=%s）",
            args.ticket_id, args.user_id,
        )
        print(
            f"[提示] 工单 {args.ticket_id} 不存在，"
            f"或您无权访问（user_id={args.user_id}）。"
        )
        return
    print(json.dumps(ticket.to_dict(), ensure_ascii=False, indent=2))


def cmd_ticket_reply(args: argparse.Namespace) -> None:
    """ticket-reply：给工单追加一条用户回复"""

    store = _make_ticket_store(args)
    reply = store.add_reply(
        ticket_id=args.ticket_id,
        author="user",
        content=args.content,
        user_id=args.user_id,
    )
    if reply is None:
        logger.warning(
            "ticket-reply: 工单 %s 不存在或越权（user_id=%s）",
            args.ticket_id, args.user_id,
        )
        print(
            f"[提示] 无法追加回复：工单 {args.ticket_id} 不存在，"
            f"或您无权操作（user_id={args.user_id}）。"
        )
        return
    print(f"已追加回复：reply_id={reply.reply_id}")
    print(f"  ticket_id:  {args.ticket_id}")
    print(f"  author:    {reply.author}")
    print(f"  created_at: {reply.created_at}")


def cmd_ticket_close(args: argparse.Namespace) -> None:
    """ticket-close：关闭工单（先校验 ownership）"""

    store = _make_ticket_store(args)
    # 先校验 ownership（update_status 内部已校验 user_id 越权）
    ok = store.update_status(
        ticket_id=args.ticket_id,
        status="closed",
        user_id=args.user_id,
    )
    if not ok:
        logger.warning(
            "ticket-close: 工单 %s 关闭失败（不存在/越权/状态流转非法）user_id=%s",
            args.ticket_id, args.user_id,
        )
        print(
            f"[提示] 工单 {args.ticket_id} 关闭失败：可能不存在、"
            f"您无权操作、或当前状态不允许直接关闭（user_id={args.user_id}）。"
        )
        return
    print(f"已关闭工单：ticket_id={args.ticket_id}")
    print(f"  user_id: {args.user_id}")


# =====================================================================
# 2. Onboarding 子命令
# =====================================================================

def cmd_onboarding_show(args: argparse.Namespace) -> None:
    """onboarding-show：查看当前用户的 onboarding profile"""

    store = _make_onboarding_store(args)
    profile = store.load(args.user_id)
    if profile is None:
        print(f"(用户 {args.user_id} 暂无 onboarding profile，可使用 onboarding-save 创建)")
        return
    print(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2))


def cmd_onboarding_save(args: argparse.Namespace) -> None:
    """onboarding-save：一次性保存 onboarding profile"""
    from deadman.onboarding.wizard import OnboardingWizard

    store = _make_onboarding_store(args)
    wizard = OnboardingWizard(store=store)

    # 解析 current_stage 逗号分隔
    current_stage: list[str] = []
    if args.current_stage:
        current_stage = [s.strip() for s in args.current_stage.split(",") if s.strip()]

    answers: dict[str, Any] = {
        "relationship": args.relationship,
        "location": args.location,
        "death_date": args.death_date or "",
        "current_stage": current_stage,
        "consent": bool(args.consent_disclaimer),
    }

    try:
        profile = wizard.save_profile(args.user_id, answers)
    except ValueError as exc:
        logger.warning("onboarding-save 校验失败: %s", exc)
        print(f"[错误] 保存失败：{exc}")
        return
    except Exception as exc:
        logger.warning("onboarding-save 异常: %s: %s", type(exc).__name__, exc)
        print(f"[错误] 保存异常：{type(exc).__name__}: {exc}")
        return
    print(f"已保存 onboarding profile：user_id={profile.user_id}")
    print(f"  relationship: {profile.relationship}")
    print(f"  location:     {profile.location}")
    print(f"  death_date:   {profile.death_date or '(空)'}")
    print(f"  current_stage: {profile.current_stage}")
    print(f"  consent_disclaimer: {profile.consent_disclaimer}")
    print(f"  updated_at:   {profile.updated_at}")


def cmd_onboarding_steps(args: argparse.Namespace) -> None:
    """onboarding-steps：列出 5 步引导问题（不保存）"""
    from deadman.onboarding.wizard import OnboardingWizard

    wizard = OnboardingWizard()
    print(f"=== Onboarding {wizard.TOTAL_STEPS} 步引导问题 ===")
    print()
    for i in range(wizard.TOTAL_STEPS):
        step = wizard.get_step(i)
        required_mark = "*" if step.get("required") else " "
        print(f"[步骤 {i + 1}/{wizard.TOTAL_STEPS}] {step['key']}{required_mark}")
        print(f"  问题：{step['question']}")
        print(f"  类型：{step['type']}")
        if step.get("options"):
            print(f"  选项：{', '.join(step['options'])}")
        if step.get("placeholder"):
            print(f"  占位：{step['placeholder']}")
        if step.get("skippable_when"):
            print(f"  可跳过条件：{step['skippable_when']}")
        print()


# =====================================================================
# 3. Knowledge Freshness 子命令
# =====================================================================

def cmd_knowledge_freshness_scan(args: argparse.Namespace) -> None:
    """knowledge-freshness-scan：扫描地域知识库时效"""
    from deadman.cron.tasks.knowledge_freshness import KnowledgeFreshnessChecker

    regions_dir = Path(args.regions_dir)
    checker = KnowledgeFreshnessChecker()
    try:
        reports = checker.scan_regions(regions_dir)
    except Exception as exc:
        logger.warning("knowledge-freshness-scan 异常: %s: %s", type(exc).__name__, exc)
        print(f"[错误] 扫描异常：{type(exc).__name__}: {exc}")
        return
    if not reports:
        print(f"(目录 {regions_dir} 下未扫描到任何 .md 文件)")
        return
    # 汇总统计
    counts = {"fresh": 0, "warning": 0, "stale": 0, "unknown": 0}
    for r in reports:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"=== 知识库时效扫描结果（共 {len(reports)} 个文件）===")
    print(
        f"  汇总：fresh={counts['fresh']} warning={counts['warning']} "
        f"stale={counts['stale']} unknown={counts['unknown']}"
    )
    print()
    for r in reports:
        last_updated_str = (
            r.last_updated.isoformat() if r.last_updated else "(无)"
        )
        days_str = f"{r.days_old} 天" if r.days_old is not None else "—"
        areas_str = ", ".join(r.policy_areas) if r.policy_areas else "—"
        print(f"- [{r.status:>7}] {r.region}")
        print(f"    file:         {r.file_path}")
        print(f"    last_updated: {last_updated_str}")
        print(f"    days_old:     {days_str}")
        print(f"    policy_areas: {areas_str}")
    print()
    print(_DISCLAIMER)


def cmd_knowledge_freshness_check(args: argparse.Namespace) -> None:
    """knowledge-freshness-check：检查指定文件的政策漂移"""
    from deadman.cron.tasks.knowledge_freshness import (
        FreshnessReport,
        KnowledgeFreshnessChecker,
    )

    file_path = Path(args.file_path)
    if not file_path.exists():
        logger.warning("knowledge-freshness-check: 文件不存在: %s", file_path)
        print(f"[提示] 文件不存在：{file_path}")
        return
    if not file_path.is_file():
        logger.warning("knowledge-freshness-check: 不是文件: %s", file_path)
        print(f"[提示] 路径不是文件：{file_path}")
        return

    checker = KnowledgeFreshnessChecker()
    # 读取文件内容，检测命中的高频政策领域
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("knowledge-freshness-check: 读取文件失败: %s: %s", file_path, exc)
        print(f"[错误] 读取文件失败：{type(exc).__name__}: {exc}")
        return
    # 复用 checker 的内部检测逻辑（@staticmethod，访问私有但仍属本模块公开 API 的等价物）
    policy_areas = checker._detect_policy_areas(text)
    last_updated = checker._parse_last_updated(text)

    # 构造一个强制 stale 的报告，触发 check_official_sources 提取关键政策点
    report = FreshnessReport(
        file_path=file_path,
        region=file_path.stem,
        last_updated=last_updated,
        days_old=None,
        status="stale",
        policy_areas=policy_areas,
    )
    try:
        drifts = checker.check_official_sources(report)
    except Exception as exc:
        logger.warning("knowledge-freshness-check 异常: %s: %s", type(exc).__name__, exc)
        print(f"[错误] 检查异常：{type(exc).__name__}: {exc}")
        return

    print(f"=== 政策漂移检查：{file_path} ===")
    print(f"  命中政策领域：{', '.join(policy_areas) if policy_areas else '(无)'}")
    print(f"  last_updated: {last_updated.isoformat() if last_updated else '(无)'}")
    print(f"  待审核项：{len(drifts)} 条")
    print()
    if not drifts:
        print("(未提取到含金额/时限/电话/法条号的政策点)")
    else:
        for i, d in enumerate(drifts, 1):
            print(f"{i}. [area={d.area}] [confidence={d.confidence}]")
            print(f"   current_text: {d.current_text}")
            if d.suggested_text:
                print(f"   suggested_text: {d.suggested_text}")
            if d.source_url:
                print(f"   source_url: {d.source_url}")
    print()
    print(_DISCLAIMER)


# =====================================================================
# 4. CN Search 子命令
# =====================================================================

def cmd_search_baidu(args: argparse.Namespace) -> None:
    """search-baidu：用百度搜索"""
    from deadman.tools.web_search import BaiduSearchProvider

    provider = BaiduSearchProvider()
    try:
        results = asyncio.run(provider.search(args.query, max_results=args.max_results))
    except Exception as exc:
        logger.warning("search-baidu 异常: %s: %s", type(exc).__name__, exc)
        print(f"[错误] 百度搜索异常：{type(exc).__name__}: {exc}")
        return
    _print_search_results("百度", args.query, results)
    try:
        asyncio.run(provider.close())
    except Exception as e:
        logger.debug("Baidu provider close 失败: %s", e)


def cmd_search_bing_cn(args: argparse.Namespace) -> None:
    """search-bing-cn：用必应中国搜索"""
    from deadman.tools.web_search import BingCNSearchProvider

    provider = BingCNSearchProvider()
    try:
        results = asyncio.run(provider.search(args.query, max_results=args.max_results))
    except Exception as exc:
        logger.warning("search-bing-cn 异常: %s: %s", type(exc).__name__, exc)
        print(f"[错误] 必应中国搜索异常：{type(exc).__name__}: {exc}")
        return
    _print_search_results("必应中国", args.query, results)
    try:
        asyncio.run(provider.close())
    except Exception as e:
        logger.debug("BingCN provider close 失败: %s", e)


def _print_search_results(provider_name: str, query: str, results: list) -> None:
    """打印搜索结果（共用辅助）"""
    print(f"=== {provider_name} 搜索结果 ===")
    print(f"query: {query}")
    if not results:
        print("(未找到结果，建议向官方热线核实，如 12345 政务服务热线)")
        print()
        print(_DISCLAIMER)
        return
    print(f"共 {len(results)} 条：")
    print()
    for i, r in enumerate(results, 1):
        # SearchResult 实例或 dict 都支持
        if hasattr(r, "to_dict"):
            d = r.to_dict()
        else:
            d = r
        print(f"{i}. [{d.get('source_type', 'unknown')}] "
              f"confidence={d.get('confidence', 0):.2f}")
        print(f"   title:   {d.get('title', '')}")
        print(f"   url:     {d.get('url', '')}")
        snippet = d.get("snippet", "") or ""
        if len(snippet) > 200:
            snippet = snippet[:200] + "..."
        print(f"   snippet: {snippet}")
    print()
    print(_DISCLAIMER)


# =====================================================================
# 5. WeChat Webhook 测试子命令
# =====================================================================

def cmd_wechat_webhook_test(args: argparse.Namespace) -> None:
    """wechat-webhook-test：测试微信公众号 webhook 签名校验"""
    from deadman.gateway.connectors.wechat import WeChatConnector

    # 仅用 verify_token 做签名校验，不接入 access_token / 消息处理
    # app_id/app_secret 留空，_verify_signature 仍可工作（不依赖它们）
    conn = WeChatConnector(
        app_id="",
        app_secret="",
        verify_token=args.token,
    )
    # _verify_signature 是 WeChatConnector 的实例方法，虽前缀 _ 仍属本模块公开 API 等价物
    try:
        ok = conn._verify_signature(args.signature, args.timestamp, args.nonce)
    except Exception as exc:
        logger.warning("wechat-webhook-test 异常: %s: %s", type(exc).__name__, exc)
        print(f"[错误] 签名校验异常：{type(exc).__name__}: {exc}")
        return

    print("=== 微信公众号 Webhook 签名校验 ===")
    print(f"  token:     {args.token}")
    print(f"  timestamp: {args.timestamp}")
    print(f"  nonce:     {args.nonce}")
    print(f"  signature: {args.signature}")
    print(f"  echostr:   {args.echostr or '(无)'}")
    print()
    if ok:
        print("  结果：✓ 校验通过")
        # 若提供了 echostr（GET 校验场景），按微信规则原样返回
        if args.echostr:
            print(f"  GET 验证响应（原样返回 echostr）：{args.echostr}")
    else:
        print("  结果：✗ 校验失败（签名不匹配）")
        print("  请检查 token 是否与微信公众号后台配置一致，"
              "以及 timestamp/nonce/signature 是否来自微信请求。")
    print()
    print(_DISCLAIMER)


# =====================================================================
# 辅助：构造 store（支持 --data-dir 用于测试隔离）
# =====================================================================

def _make_ticket_store(args: argparse.Namespace):
    """构造 TicketStore（支持 --data-dir 用于测试隔离）"""
    from deadman.support.store import TicketStore

    data_dir = getattr(args, "data_dir", None)
    if data_dir:
        return TicketStore(data_dir=Path(data_dir))
    return TicketStore()


def _make_onboarding_store(args: argparse.Namespace):
    """构造 OnboardingStore（支持 --data-dir 用于测试隔离）"""
    from deadman.onboarding.store import OnboardingStore

    data_dir = getattr(args, "data_dir", None)
    if data_dir:
        return OnboardingStore(data_dir=Path(data_dir))
    return OnboardingStore()


# =====================================================================
# subparser 注册
# =====================================================================

# Ticket 允许的 category/priority（与 deadman.support.models 一致）
_TICKET_CATEGORIES = ["咨询", "反馈", "投诉", "数据删除", "跨境合规"]
_TICKET_PRIORITIES = ["低", "普通", "紧急"]

# Onboarding 允许的 relationship（与 deadman.onboarding.wizard 一致）
_ONBOARDING_RELATIONSHIPS = ["亲属", "朋友", "本人", "其他"]


def register_subparsers(subparsers: Any) -> None:
    """注册 Phase 16 共 13 个子命令到 subparsers

    子命令清单（按类别）：
        Support Ticket（5）：
            ticket-create / ticket-list / ticket-get / ticket-reply / ticket-close
        Onboarding（3）：
            onboarding-show / onboarding-save / onboarding-steps
        Knowledge Freshness（2）：
            knowledge-freshness-scan / knowledge-freshness-check
        CN Search（2）：
            search-baidu / search-bing-cn
        WeChat Webhook（1）：
            wechat-webhook-test
    """
    # ============== 1. Support Ticket ==============
    tc_p = subparsers.add_parser(
        "ticket-create", help="创建客服工单（Phase 16）"
    )
    tc_p.add_argument("--user-id", required=True, help="用户 ID")
    tc_p.add_argument(
        "--category", required=True, choices=_TICKET_CATEGORIES,
        help="工单类别（咨询/反馈/投诉/数据删除/跨境合规）",
    )
    tc_p.add_argument(
        "--priority", default="普通", choices=_TICKET_PRIORITIES,
        help="优先级（低/普通/紧急，默认 普通）",
    )
    tc_p.add_argument("--subject", required=True, help="工单主题（≤200 字符）")
    tc_p.add_argument("--description", required=True, help="工单描述（≤5000 字符）")
    tc_p.add_argument("--data-dir", default=None, help="数据根目录（测试用）")
    tc_p.set_defaults(func=cmd_ticket_create)

    tl_p = subparsers.add_parser(
        "ticket-list", help="列出我的工单（Phase 16）"
    )
    tl_p.add_argument("--user-id", required=True, help="用户 ID")
    tl_p.add_argument(
        "--status", default=None,
        choices=["open", "in_progress", "resolved", "closed"],
        help="按状态过滤（可选）",
    )
    tl_p.add_argument("--data-dir", default=None, help="数据根目录（测试用）")
    tl_p.set_defaults(func=cmd_ticket_list)

    tg_p = subparsers.add_parser(
        "ticket-get", help="查看工单详情（Phase 16，越权返回提示）"
    )
    tg_p.add_argument("--ticket-id", required=True, help="工单 ID")
    tg_p.add_argument("--user-id", required=True, help="用户 ID（用于越权校验）")
    tg_p.add_argument("--data-dir", default=None, help="数据根目录（测试用）")
    tg_p.set_defaults(func=cmd_ticket_get)

    tr_p = subparsers.add_parser(
        "ticket-reply", help="给工单追加回复（Phase 16）"
    )
    tr_p.add_argument("--ticket-id", required=True, help="工单 ID")
    tr_p.add_argument("--user-id", required=True, help="用户 ID（用于越权校验）")
    tr_p.add_argument("--content", required=True, help="回复内容")
    tr_p.add_argument("--data-dir", default=None, help="数据根目录（测试用）")
    tr_p.set_defaults(func=cmd_ticket_reply)

    tcl_p = subparsers.add_parser(
        "ticket-close", help="关闭工单（Phase 16，先校验 ownership）"
    )
    tcl_p.add_argument("--ticket-id", required=True, help="工单 ID")
    tcl_p.add_argument("--user-id", required=True, help="用户 ID（用于越权校验）")
    tcl_p.add_argument("--data-dir", default=None, help="数据根目录（测试用）")
    tcl_p.set_defaults(func=cmd_ticket_close)

    # ============== 2. Onboarding ==============
    ob_show_p = subparsers.add_parser(
        "onboarding-show", help="查看当前用户的 onboarding profile（Phase 16）"
    )
    ob_show_p.add_argument("--user-id", required=True, help="用户 ID")
    ob_show_p.add_argument("--data-dir", default=None, help="数据根目录（测试用）")
    ob_show_p.set_defaults(func=cmd_onboarding_show)

    ob_save_p = subparsers.add_parser(
        "onboarding-save", help="保存 onboarding profile（Phase 16，一次性传入所有字段）"
    )
    ob_save_p.add_argument("--user-id", required=True, help="用户 ID")
    ob_save_p.add_argument(
        "--relationship", required=True, choices=_ONBOARDING_RELATIONSHIPS,
        help="与逝者的关系（亲属/朋友/本人/其他）",
    )
    ob_save_p.add_argument(
        "--location", required=True,
        help="所在省份（如 北京 / 上海 / 海外，与知识库 _PROVINCES 列表一致）",
    )
    ob_save_p.add_argument(
        "--death-date", default=None,
        help="逝者去世日期（YYYY-MM-DD，可选；本人场景可留空）",
    )
    ob_save_p.add_argument(
        "--current-stage", default=None,
        help="当前已办理阶段，逗号分隔（如 '死亡证明,户口注销'）",
    )
    ob_save_p.add_argument(
        "--consent-disclaimer", action="store_true",
        help="勾选表示已读并同意《用户协议》和《隐私政策》",
    )
    ob_save_p.add_argument("--data-dir", default=None, help="数据根目录（测试用）")
    ob_save_p.set_defaults(func=cmd_onboarding_save)

    ob_steps_p = subparsers.add_parser(
        "onboarding-steps", help="列出 5 步引导问题（Phase 16，不保存）"
    )
    ob_steps_p.set_defaults(func=cmd_onboarding_steps)

    # ============== 3. Knowledge Freshness ==============
    kf_scan_p = subparsers.add_parser(
        "knowledge-freshness-scan", help="扫描地域知识库文件时效（Phase 16）"
    )
    kf_scan_p.add_argument(
        "--regions-dir", default=".traecli/knowledge/regions",
        help="地域知识库目录（默认 .traecli/knowledge/regions）",
    )
    kf_scan_p.set_defaults(func=cmd_knowledge_freshness_scan)

    kf_check_p = subparsers.add_parser(
        "knowledge-freshness-check", help="检查指定文件的政策漂移（Phase 16）"
    )
    kf_check_p.add_argument(
        "--file-path", required=True, help="待检查的 .md 文件路径",
    )
    kf_check_p.set_defaults(func=cmd_knowledge_freshness_check)

    # ============== 4. CN Search ==============
    sb_p = subparsers.add_parser(
        "search-baidu", help="用百度搜索（Phase 16，中国境内备选）"
    )
    sb_p.add_argument("--query", required=True, help="搜索查询语句")
    sb_p.add_argument(
        "--max-results", type=int, default=5,
        help="最大结果数（默认 5）",
    )
    sb_p.set_defaults(func=cmd_search_baidu)

    sbc_p = subparsers.add_parser(
        "search-bing-cn", help="用必应中国搜索（Phase 16，中国境内备选）"
    )
    sbc_p.add_argument("--query", required=True, help="搜索查询语句")
    sbc_p.add_argument(
        "--max-results", type=int, default=5,
        help="最大结果数（默认 5）",
    )
    sbc_p.set_defaults(func=cmd_search_bing_cn)

    # ============== 5. WeChat Webhook Test ==============
    wx_p = subparsers.add_parser(
        "wechat-webhook-test", help="测试微信公众号 webhook 签名校验（Phase 16）"
    )
    wx_p.add_argument("--token", required=True, help="微信公众号后台配置的 Token")
    wx_p.add_argument("--timestamp", required=True, help="微信请求时间戳")
    wx_p.add_argument("--nonce", required=True, help="微信请求随机串")
    wx_p.add_argument("--signature", required=True, help="微信请求签名")
    wx_p.add_argument(
        "--echostr", default=None,
        help="GET 校验时的 echostr（提供则原样返回，模拟微信 URL 验证）",
    )
    wx_p.set_defaults(func=cmd_wechat_webhook_test)


# 命令清单（供 cli.py 引用）
COMMANDS = [
    "ticket-create",
    "ticket-list",
    "ticket-get",
    "ticket-reply",
    "ticket-close",
    "onboarding-show",
    "onboarding-save",
    "onboarding-steps",
    "knowledge-freshness-scan",
    "knowledge-freshness-check",
    "search-baidu",
    "search-bing-cn",
    "wechat-webhook-test",
]
