"""测试 deadman._cli_extensions.phase16 - Phase 16 CLI 子命令

覆盖点（>= 15 个）：
  - register_subparsers 注册全部 13 个子命令
  - 每个子命令的参数解析（required / choices / 默认值）
  - ticket-create 创建工单（真实 TicketStore + tmp_path 隔离）
  - ticket-list 列出工单（含状态过滤）
  - ticket-get 越权防护
  - ticket-reply / ticket-close
  - onboarding-show / onboarding-save / onboarding-steps
  - knowledge-freshness-scan / knowledge-freshness-check
  - search-baidu / search-bing-cn（mock provider.search）
  - wechat-webhook-test 签名校验（合法 + 非法 + echostr）
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from deadman._cli_extensions import phase16


# =====================================================================
# 辅助
# =====================================================================


def make_parser() -> argparse.ArgumentParser:
    """构造挂载了 phase16 子命令的 ArgumentParser"""
    parser = argparse.ArgumentParser(prog="deadman-test")
    subparsers = parser.add_subparsers(dest="command")
    phase16.register_subparsers(subparsers)
    return parser


def parse(argv: list[str]) -> argparse.Namespace:
    return make_parser().parse_args(argv)


# =====================================================================
# 1. register_subparsers 注册全部 13 个子命令
# =====================================================================


class TestRegisterSubparsers:
    def test_registers_all_13_commands(self):
        """注册的 13 个子命令都在 choices 中"""
        parser = make_parser()
        # 通过 --help 解析时各 subparser 注册到 choices
        subparsers_action = next(
            a for a in parser._actions
            if isinstance(a, argparse._SubParsersAction)
        )
        registered = set(subparsers_action.choices.keys())
        for cmd in phase16.COMMANDS:
            assert cmd in registered, f"子命令 {cmd} 未注册"
        assert len(phase16.COMMANDS) == 13


# =====================================================================
# 2. ticket-create 参数解析 + 行为
# =====================================================================


class TestTicketCreate:
    def test_required_args_missing_user_id_exits(self):
        with pytest.raises(SystemExit):
            parse([
                "ticket-create",
                "--category", "咨询",
                "--subject", "测试",
                "--description", "测试描述",
            ])

    def test_required_args_missing_category_exits(self):
        with pytest.raises(SystemExit):
            parse([
                "ticket-create",
                "--user-id", "u1",
                "--subject", "测试",
                "--description", "测试描述",
            ])

    def test_invalid_category_rejected_by_choices(self):
        """英文 category 名应被 argparse choices 拒绝（与底层 Chinese 校验对齐）"""
        with pytest.raises(SystemExit):
            parse([
                "ticket-create",
                "--user-id", "u1",
                "--category", "complaint",  # 应为「投诉」
                "--subject", "测试",
                "--description", "测试描述",
            ])

    def test_priority_default_is_normal(self):
        """默认 priority 应为「普通」"""
        args = parse([
            "ticket-create",
            "--user-id", "u1",
            "--category", "咨询",
            "--subject", "测试",
            "--description", "测试描述",
        ])
        assert args.priority == "普通"

    def test_create_success_prints_ticket_id(self, tmp_path: Path, capsys):
        args = parse([
            "ticket-create",
            "--user-id", "u1",
            "--category", "咨询",
            "--priority", "紧急",
            "--subject", "如何办理户口注销？",
            "--description", "请问需要哪些材料？",
            "--data-dir", str(tmp_path),
        ])
        assert callable(args.func)
        args.func(args)
        out = capsys.readouterr().out
        assert "已创建工单" in out
        assert "ticket_id=tkt-" in out
        assert "如何办理户口注销？" in out

    def test_create_invalid_priority_friendly_error(self, tmp_path: Path, capsys):
        """绕过 argparse choices，传 invalid priority 给底层 -> 友好错误"""
        # 手动构造 args 绕过 choices 校验
        ns = argparse.Namespace(
            user_id="u1",
            category="咨询",
            priority="INVALID",  # 不在 _ALLOWED_PRIORITIES
            subject="测试",
            description="测试描述",
            data_dir=str(tmp_path),
        )
        phase16.cmd_ticket_create(ns)
        out = capsys.readouterr().out
        assert "[错误]" in out
        assert "INVALID" in out or "工单创建失败" in out


# =====================================================================
# 3. ticket-list 参数解析 + 行为
# =====================================================================


class TestTicketList:
    def test_list_empty_prints_hint(self, tmp_path: Path, capsys):
        args = parse([
            "ticket-list", "--user-id", "u1", "--data-dir", str(tmp_path),
        ])
        args.func(args)
        out = capsys.readouterr().out
        assert "暂无工单" in out

    def test_list_with_status_filter(self, tmp_path: Path, capsys):
        # 先创建一个工单
        from deadman.support.store import TicketStore
        store = TicketStore(data_dir=tmp_path)
        store.create_ticket("u1", "咨询", "普通", "主题1", "描述1")
        args = parse([
            "ticket-list", "--user-id", "u1",
            "--status", "open",
            "--data-dir", str(tmp_path),
        ])
        assert args.status == "open"
        args.func(args)
        out = capsys.readouterr().out
        assert "主题1" in out
        assert "共 1 条" in out


# =====================================================================
# 4. ticket-get 越权防护
# =====================================================================


class TestTicketGet:
    def test_get_success_prints_json(self, tmp_path: Path, capsys):
        from deadman.support.store import TicketStore
        store = TicketStore(data_dir=tmp_path)
        t = store.create_ticket("alice", "咨询", "普通", "主题A", "描述A")
        args = parse([
            "ticket-get",
            "--ticket-id", t.ticket_id,
            "--user-id", "alice",
            "--data-dir", str(tmp_path),
        ])
        args.func(args)
        out = capsys.readouterr().out
        # JSON 输出包含 ticket_id
        assert t.ticket_id in out
        assert "主题A" in out

    def test_get_other_user_denied_prints_hint(self, tmp_path: Path, capsys):
        """越权访问应返回 None 并打印友好提示"""
        from deadman.support.store import TicketStore
        store = TicketStore(data_dir=tmp_path)
        t = store.create_ticket("alice", "咨询", "普通", "私密主题", "私密描述")
        args = parse([
            "ticket-get",
            "--ticket-id", t.ticket_id,
            "--user-id", "bob",  # bob 不是 owner
            "--data-dir", str(tmp_path),
        ])
        args.func(args)
        out = capsys.readouterr().out
        assert "无权访问" in out or "不存在" in out
        # 关键：不能泄露私密内容
        assert "私密主题" not in out
        assert "私密描述" not in out

    def test_get_nonexistent_prints_hint(self, tmp_path: Path, capsys):
        args = parse([
            "ticket-get",
            "--ticket-id", "tkt-nonexistent",
            "--user-id", "u1",
            "--data-dir", str(tmp_path),
        ])
        args.func(args)
        out = capsys.readouterr().out
        assert "不存在" in out or "无权访问" in out


# =====================================================================
# 5. ticket-reply / ticket-close
# =====================================================================


class TestTicketReplyClose:
    def test_reply_success_prints_reply_id(self, tmp_path: Path, capsys):
        from deadman.support.store import TicketStore
        store = TicketStore(data_dir=tmp_path)
        t = store.create_ticket("u1", "咨询", "普通", "主题", "描述")
        args = parse([
            "ticket-reply",
            "--ticket-id", t.ticket_id,
            "--user-id", "u1",
            "--content", "这是我的回复",
            "--data-dir", str(tmp_path),
        ])
        args.func(args)
        out = capsys.readouterr().out
        assert "已追加回复" in out
        assert "rep-" in out

    def test_reply_other_user_denied(self, tmp_path: Path, capsys):
        from deadman.support.store import TicketStore
        store = TicketStore(data_dir=tmp_path)
        t = store.create_ticket("alice", "咨询", "普通", "主题", "描述")
        args = parse([
            "ticket-reply",
            "--ticket-id", t.ticket_id,
            "--user-id", "bob",
            "--content", "我试图追加",
            "--data-dir", str(tmp_path),
        ])
        args.func(args)
        out = capsys.readouterr().out
        assert "无法追加回复" in out

    def test_close_success(self, tmp_path: Path, capsys):
        from deadman.support.store import TicketStore
        store = TicketStore(data_dir=tmp_path)
        t = store.create_ticket("u1", "咨询", "普通", "主题", "描述")
        args = parse([
            "ticket-close",
            "--ticket-id", t.ticket_id,
            "--user-id", "u1",
            "--data-dir", str(tmp_path),
        ])
        args.func(args)
        out = capsys.readouterr().out
        assert "已关闭工单" in out
        # 验证状态确实变成 closed
        loaded = store.get_ticket(t.ticket_id, user_id="u1")
        assert loaded is not None
        assert loaded.status == "closed"

    def test_close_other_user_denied(self, tmp_path: Path, capsys):
        from deadman.support.store import TicketStore
        store = TicketStore(data_dir=tmp_path)
        t = store.create_ticket("alice", "咨询", "普通", "主题", "描述")
        args = parse([
            "ticket-close",
            "--ticket-id", t.ticket_id,
            "--user-id", "bob",
            "--data-dir", str(tmp_path),
        ])
        args.func(args)
        out = capsys.readouterr().out
        assert "关闭失败" in out
        # 工单状态应仍是 open
        loaded = store.get_ticket(t.ticket_id, user_id="alice")
        assert loaded is not None
        assert loaded.status == "open"


# =====================================================================
# 6. onboarding-show / onboarding-save / onboarding-steps
# =====================================================================


class TestOnboarding:
    def test_show_no_profile_prints_hint(self, tmp_path: Path, capsys):
        args = parse([
            "onboarding-show", "--user-id", "u1", "--data-dir", str(tmp_path),
        ])
        args.func(args)
        out = capsys.readouterr().out
        assert "暂无 onboarding profile" in out

    def test_save_success_prints_summary(self, tmp_path: Path, capsys):
        args = parse([
            "onboarding-save",
            "--user-id", "u1",
            "--relationship", "亲属",
            "--location", "北京",
            "--death-date", "2024-01-01",
            "--current-stage", "死亡证明,户口注销",
            "--consent-disclaimer",
            "--data-dir", str(tmp_path),
        ])
        args.func(args)
        out = capsys.readouterr().out
        assert "已保存 onboarding profile" in out
        assert "亲属" in out
        assert "北京" in out
        # 验证确实落盘
        from deadman.onboarding.store import OnboardingStore
        loaded = OnboardingStore(data_dir=tmp_path).load("u1")
        assert loaded is not None
        assert loaded.relationship == "亲属"
        assert loaded.location == "北京"
        assert loaded.death_date == "2024-01-01"
        assert loaded.current_stage == ["死亡证明", "户口注销"]
        assert loaded.consent_disclaimer is True

    def test_save_missing_consent_fails_friendly(self, tmp_path: Path, capsys):
        """未勾选 --consent-disclaimer 应被 wizard 校验拒绝"""
        args = parse([
            "onboarding-save",
            "--user-id", "u1",
            "--relationship", "亲属",
            "--location", "北京",
            "--data-dir", str(tmp_path),
        ])
        args.func(args)
        out = capsys.readouterr().out
        assert "[错误]" in out
        assert "consent" in out or "同意" in out

    def test_save_invalid_relationship_rejected(self):
        with pytest.raises(SystemExit):
            parse([
                "onboarding-save",
                "--user-id", "u1",
                "--relationship", "cousin",  # 应为中文
                "--location", "北京",
            ])

    def test_steps_lists_5_steps(self, capsys):
        args = parse(["onboarding-steps"])
        args.func(args)
        out = capsys.readouterr().out
        assert "5 步引导问题" in out
        # 5 步的关键字都应出现
        for key in ["relationship", "location", "death_date", "current_stage", "consent"]:
            assert key in out, f"步骤 {key} 未出现"
        # 每步的"问题"行
        assert out.count("问题：") == 5


# =====================================================================
# 7. knowledge-freshness-scan / knowledge-freshness-check
# =====================================================================


class TestKnowledgeFreshness:
    def test_scan_nonexistent_dir_prints_empty(self, tmp_path: Path, capsys):
        nonexistent = tmp_path / "no-such-dir"
        args = parse([
            "knowledge-freshness-scan",
            "--regions-dir", str(nonexistent),
        ])
        args.func(args)
        out = capsys.readouterr().out
        assert "未扫描到" in out

    def test_scan_with_files_prints_reports(self, tmp_path: Path, capsys):
        regions = tmp_path / "regions" / "CN" / "beijing"
        regions.mkdir(parents=True)
        # 创建一个 fresh 文件 + 一个 stale 文件 + 一个 unknown 文件
        (regions / "fresh.md").write_text(
            "# 北京指南\n## 元信息\n最后更新: 2026-07-15\n", encoding="utf-8"
        )
        (regions / "stale.md").write_text(
            "# 北京指南\n## 元信息\n最后更新: 2020-01-01\n\n包含社保信息\n",
            encoding="utf-8",
        )
        (regions / "unknown.md").write_text(
            "# 北京指南（无日期）\n", encoding="utf-8"
        )
        args = parse([
            "knowledge-freshness-scan",
            "--regions-dir", str(tmp_path / "regions"),
        ])
        args.func(args)
        out = capsys.readouterr().out
        assert "共 3 个文件" in out
        assert "stale" in out
        assert "fresh" in out
        assert "unknown" in out

    def test_check_nonexistent_file_prints_hint(self, tmp_path: Path, capsys):
        nonexistent = tmp_path / "no-such.md"
        args = parse([
            "knowledge-freshness-check",
            "--file-path", str(nonexistent),
        ])
        args.func(args)
        out = capsys.readouterr().out
        assert "文件不存在" in out

    def test_check_with_policy_points_extracts_drifts(self, tmp_path: Path, capsys):
        """文件含金额/时限且命中政策领域，应提取 drift items"""
        md = tmp_path / "policy.md"
        md.write_text(
            "# 政策文件\n"
            "## 元信息\n最后更新: 2020-01-01\n\n"
            "## 阶段8：社保\n"
            "- 养老金发放期限为 60 天内到账\n"
            "- 抚恤金标准约 50000 元\n",
            encoding="utf-8",
        )
        args = parse([
            "knowledge-freshness-check",
            "--file-path", str(md),
        ])
        args.func(args)
        out = capsys.readouterr().out
        assert "命中的政策领域" in out or "命中政策领域" in out
        assert "社保" in out
        # 至少提取出 1 条 drift
        assert "待审核项" in out


# =====================================================================
# 8. search-baidu / search-bing-cn（mock provider.search）
# =====================================================================


class TestCNSearch:
    @patch("deadman.tools.web_search.BaiduSearchProvider")
    def test_search_baidu_calls_provider_and_prints(
        self, MockProvider, capsys
    ):
        from deadman.tools.web_search import SearchResult

        mock_instance = MockProvider.return_value
        mock_instance.search = AsyncMock(
            return_value=[
                SearchResult(
                    title="北京市公安局",
                    url="https://www.beijing.gov.cn/gaj/",
                    snippet="户籍办理指南",
                    source_type="official",
                    confidence=0.9,
                ),
            ]
        )
        mock_instance.close = AsyncMock(return_value=None)

        args = parse(["search-baidu", "--query", "北京户籍办理", "--max-results", "3"])
        args.func(args)
        out = capsys.readouterr().out
        assert "百度" in out and "搜索结果" in out
        assert "共 1 条" in out
        assert "北京市公安局" in out
        assert "official" in out
        # 验证 provider.search 被调用且参数正确
        mock_instance.search.assert_awaited_once_with(
            "北京户籍办理", max_results=3
        )

    @patch("deadman.tools.web_search.BaiduSearchProvider")
    def test_search_baidu_empty_results_prints_hint(self, MockProvider, capsys):
        mock_instance = MockProvider.return_value
        mock_instance.search = AsyncMock(return_value=[])
        mock_instance.close = AsyncMock(return_value=None)

        args = parse(["search-baidu", "--query", "无结果查询"])
        args.func(args)
        out = capsys.readouterr().out
        assert "未找到结果" in out
        assert "12345" in out  # 提示打官方热线

    @patch("deadman.tools.web_search.BingCNSearchProvider")
    def test_search_bing_cn_calls_provider(self, MockProvider, capsys):
        from deadman.tools.web_search import SearchResult

        mock_instance = MockProvider.return_value
        mock_instance.search = AsyncMock(
            return_value=[
                SearchResult(
                    title="示例结果",
                    url="https://example.gov.cn/",
                    snippet="政策摘要",
                    source_type="official",
                    confidence=0.85,
                ),
            ]
        )
        mock_instance.close = AsyncMock(return_value=None)

        args = parse(["search-bing-cn", "--query", "测试", "--max-results", "2"])
        args.func(args)
        out = capsys.readouterr().out
        assert "必应中国" in out
        assert "示例结果" in out
        mock_instance.search.assert_awaited_once_with("测试", max_results=2)


# =====================================================================
# 9. wechat-webhook-test 签名校验
# =====================================================================


def _compute_wechat_signature(token: str, timestamp: str, nonce: str) -> str:
    """复用 WeChatConnector._verify_signature 的算法计算合法签名"""
    parts = sorted([token, timestamp, nonce])
    raw = "".join(parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


class TestWeChatWebhookTest:
    def test_valid_signature_prints_pass(self, capsys):
        token = "my-wechat-token"
        timestamp = "1700000000"
        nonce = "abc123nonce"
        signature = _compute_wechat_signature(token, timestamp, nonce)
        args = parse([
            "wechat-webhook-test",
            "--token", token,
            "--timestamp", timestamp,
            "--nonce", nonce,
            "--signature", signature,
        ])
        args.func(args)
        out = capsys.readouterr().out
        assert "校验通过" in out

    def test_invalid_signature_prints_fail(self, capsys):
        args = parse([
            "wechat-webhook-test",
            "--token", "my-token",
            "--timestamp", "1700000000",
            "--nonce", "abc123",
            "--signature", "deadbeef-not-matching",
        ])
        args.func(args)
        out = capsys.readouterr().out
        assert "校验失败" in out

    def test_valid_signature_with_echostr_prints_echostr(self, capsys):
        """提供 --echostr 时按微信 GET URL 验证规则原样返回"""
        token = "test-token"
        timestamp = "1700000001"
        nonce = "nonce-xyz"
        signature = _compute_wechat_signature(token, timestamp, nonce)
        args = parse([
            "wechat-webhook-test",
            "--token", token,
            "--timestamp", timestamp,
            "--nonce", nonce,
            "--signature", signature,
            "--echostr", "random-echo-string-12345",
        ])
        args.func(args)
        out = capsys.readouterr().out
        assert "校验通过" in out
        assert "random-echo-string-12345" in out
        assert "GET 验证响应" in out


# =====================================================================
# 10. 子命令分发：set_defaults(func=...) 正确绑定
# =====================================================================


class TestCommandDispatch:
    def test_each_command_has_callable_func(self):
        """每个子命令解析后 args.func 应是 phase16 中的 cmd_xxx 函数"""
        cases = [
            (["ticket-create", "--user-id", "u1", "--category", "咨询",
              "--subject", "s", "--description", "d"],
             phase16.cmd_ticket_create),
            (["ticket-list", "--user-id", "u1"], phase16.cmd_ticket_list),
            (["ticket-get", "--ticket-id", "t1", "--user-id", "u1"],
             phase16.cmd_ticket_get),
            (["ticket-reply", "--ticket-id", "t1", "--user-id", "u1",
              "--content", "c"], phase16.cmd_ticket_reply),
            (["ticket-close", "--ticket-id", "t1", "--user-id", "u1"],
             phase16.cmd_ticket_close),
            (["onboarding-show", "--user-id", "u1"], phase16.cmd_onboarding_show),
            (["onboarding-save", "--user-id", "u1", "--relationship", "亲属",
              "--location", "北京", "--consent-disclaimer"],
             phase16.cmd_onboarding_save),
            (["onboarding-steps"], phase16.cmd_onboarding_steps),
            (["knowledge-freshness-scan"], phase16.cmd_knowledge_freshness_scan),
            (["knowledge-freshness-check", "--file-path", "/tmp/x.md"],
             phase16.cmd_knowledge_freshness_check),
            (["search-baidu", "--query", "q"], phase16.cmd_search_baidu),
            (["search-bing-cn", "--query", "q"], phase16.cmd_search_bing_cn),
            (["wechat-webhook-test", "--token", "t", "--timestamp", "1",
              "--nonce", "n", "--signature", "s"],
             phase16.cmd_wechat_webhook_test),
        ]
        for argv, expected_func in cases:
            args = parse(argv)
            assert args.func is expected_func, (
                f"命令 {argv[0]} 的 func 应为 {expected_func.__name__}, "
                f"实际 {args.func.__name__}"
            )
