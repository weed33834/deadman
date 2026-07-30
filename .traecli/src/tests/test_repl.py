"""测试 deadman.repl - 交互式对话 REPL

覆盖点（3 个）：
  - test_slash_help: /help 命令打印帮助文本
  - test_slash_quit: /quit 命令使 REPL 退出（_read_input 返回 None）
  - test_normal_input_calls_graph: 普通输入调用 graph.ainvoke 并提取响应

防注入验证（input-guardrails.md）：
  - test_normal_input_calls_graph 校验用户输入仅作为 ConversationState.user_input 字段
  - 不拼接到 shell/exec/eval（由 build_main_graph 的 input_guard 节点做规则校验）

测试隔离：MemoryManager / graph / SoulLoader 全部用 mock，不触达真实 LLM 或文件系统。
不依赖 pytest-asyncio：async 方法用 asyncio.run() 在 sync 测试函数内调用。
"""

from __future__ import annotations

import asyncio
import builtins
import io
from unittest.mock import AsyncMock, MagicMock

from deadman.repl import _SLASH_COMMANDS, ChatREPL

# =====================================================================
# 辅助：构造带 mock 依赖的 ChatREPL
# =====================================================================


def _make_repl_with_mocks(
    stdout: io.StringIO | None = None,
    graph: MagicMock | None = None,
    memory_manager: MagicMock | None = None,
) -> ChatREPL:
    """构造一个用 mock 依赖的 ChatREPL，避免触达真实 LLM / 文件系统。

    - soul_loader: mock，get_soul 返回固定文本
    - memory_manager: mock，所有方法无副作用
    - graph: AsyncMock，ainvoke 返回固定响应
    - stdout: io.StringIO，便于断言输出
    """
    if stdout is None:
        stdout = io.StringIO()

    soul_loader = MagicMock()
    soul_loader.get_soul.return_value = "测试 SOUL 身份"

    if memory_manager is None:
        memory_manager = MagicMock()
        memory_manager.working = MagicMock()
        memory_manager.working.session_id = "test-sess"
        memory_manager.working.temp_vars = {}
        memory_manager.working.recent_turns = []
        memory_manager.start_session = MagicMock()
        memory_manager.after_turn = AsyncMock()
        memory_manager.episodic = MagicMock()
        memory_manager.episodic._store = {}
        memory_manager.semantic = MagicMock()
        memory_manager.semantic.user_profiles = {}
        memory_manager.semantic.facts = {}
        memory_manager.semantic.pending_contradictions = []
        memory_manager.procedural = MagicMock()
        memory_manager.procedural._procedures = {}
        memory_manager.graphiti = None
        memory_manager.lightrag = None
        memory_manager.file_store = None

    if graph is None:
        graph = MagicMock()
        graph.ainvoke = AsyncMock(
            return_value={
                "final_response": "测试响应内容",
                "current_agent": "death-aftercare",
                "rule_check": None,
                "pending_transfer": None,
                "subagent_results": [],
            }
        )

    repl = ChatREPL(
        user_id="test-user",
        session_id="test-sess",
        soul_loader=soul_loader,
        memory_manager=memory_manager,
        graph=graph,
        stdout=stdout,
    )
    return repl


# =====================================================================
# 1. /help 命令打印帮助文本
# =====================================================================


class TestSlashHelp:
    """测试 /help slash 命令"""

    def test_slash_help(self):
        # /help 应打印所有 slash 命令的说明
        stdout = io.StringIO()
        repl = _make_repl_with_mocks(stdout=stdout)

        # _handle_slash 是 async 方法，用 asyncio.run 调用
        asyncio.run(repl._handle_slash("/help"))

        output = stdout.getvalue()
        # 应包含"帮助"标题
        assert "帮助" in output, "/help 输出应包含帮助标题"
        # 应包含所有 slash 命令
        for cmd in _SLASH_COMMANDS:
            assert cmd in output, f"/help 输出应包含命令 {cmd}"
        # 应包含至少一条命令描述
        assert "退出" in output or "显示" in output, "应包含命令描述"

    def test_slash_help_does_not_call_graph(self):
        # /help 不应调用 LLM graph（防注入：slash 命令是本地指令）
        graph = MagicMock()
        graph.ainvoke = AsyncMock(return_value={"final_response": ""})
        repl = _make_repl_with_mocks(graph=graph)

        asyncio.run(repl._handle_slash("/help"))

        graph.ainvoke.assert_not_called()


# =====================================================================
# 2. /quit 命令使 REPL 退出
# =====================================================================


class TestSlashQuit:
    """测试 /quit 退出命令"""

    def test_slash_quit(self):
        # /quit 应使 _read_input 返回 None（_async_run 据此退出循环）
        repl = _make_repl_with_mocks()

        # mock builtins.input 返回 "/quit"
        original_input = builtins.input
        builtins.input = lambda prompt="": "/quit"
        try:
            result = repl._read_input()
        finally:
            builtins.input = original_input

        assert result is None, "/quit 应使 _read_input 返回 None（触发退出）"

    def test_slash_quit_exits_run_cleanly(self):
        # 完整 run() 流程：banner → start_session → /quit → 退出码 0
        repl = _make_repl_with_mocks()

        original_input = builtins.input
        builtins.input = lambda prompt="": "/quit"
        try:
            rc = repl.run()
        finally:
            builtins.input = original_input

        assert rc == 0, "/quit 退出码应为 0（正常退出）"
        # memory_manager.start_session 应被调用过
        repl.memory_manager.start_session.assert_called_once_with("test-user", "test-sess")


# =====================================================================
# 3. 普通输入调用 graph.ainvoke（防注入：用户输入仅作为 message content）
# =====================================================================


class TestNormalInputCallsGraph:
    """测试普通输入调用 graph.ainvoke"""

    def test_normal_input_calls_graph(self):
        # 普通输入应调用 graph.ainvoke，且 user_input 仅作为 state 字段
        graph = MagicMock()
        graph.ainvoke = AsyncMock(
            return_value={
                "final_response": "这是助手响应",
                "current_agent": "death-aftercare",
                "rule_check": None,
                "pending_transfer": None,
                "subagent_results": [],
            }
        )
        repl = _make_repl_with_mocks(graph=graph)

        user_text = "请问户籍注销需要哪些材料？"
        # _handle_normal_input 是 async 方法，用 asyncio.run 调用
        asyncio.run(repl._handle_normal_input(user_text))

        # graph.ainvoke 应被调用一次
        graph.ainvoke.assert_called_once()

        # 校验传入的 state：user_input 字段等于用户输入（防注入硬约束）
        call_args = graph.ainvoke.call_args
        state = call_args.args[0] if call_args.args else call_args.kwargs.get("state")
        assert state is not None, "ainvoke 应收到 state 参数"
        assert state["user_input"] == user_text, (
            "用户输入应仅作为 ConversationState.user_input 字段（防注入）"
        )
        assert state["session_id"] == "test-sess"

        # after_turn 应被调用（更新记忆）
        repl.memory_manager.after_turn.assert_called_once()
        after_turn_kwargs = repl.memory_manager.after_turn.call_args.kwargs
        assert after_turn_kwargs.get("user_input") == user_text
        assert after_turn_kwargs.get("assistant_response") == "这是助手响应"

        # 统计指标应更新
        assert repl.turn_count == 1
        assert repl.total_input_chars == len(user_text)
        assert repl.total_output_chars == len("这是助手响应")
        assert repl.last_agent == "death-aftercare"

    def test_normal_input_user_input_not_injected_to_shell(self):
        # 防注入回归：含 shell 元字符的用户输入应原样作为 state.user_input，
        # 不被解释为本地指令（不调用 subprocess/os.system/eval/exec）
        graph = MagicMock()
        graph.ainvoke = AsyncMock(
            return_value={
                "final_response": "响应",
                "current_agent": "death-aftercare",
                "rule_check": None,
                "pending_transfer": None,
                "subagent_results": [],
            }
        )
        repl = _make_repl_with_mocks(graph=graph)

        # 含 shell 注入尝试的用户输入
        malicious = "rm -rf /; eval('os.system(\"ls\")'); __import__('os')"
        asyncio.run(repl._handle_normal_input(malicious))

        graph.ainvoke.assert_called_once()
        state = graph.ainvoke.call_args.args[0]
        # 用户输入应原样作为 state.user_input 字段，不被执行
        assert state["user_input"] == malicious
