"""交互式 REPL - 借鉴 Hermes Agent MIT 设计的 `hermes` 交互命令。

实现 `deadman chat` 子命令：
    - 交互式 REPL，基于 Python 内置 input() + asyncio
    - 启动时打印 banner（deadman 版本 + 默认 SOUL 身份）
    - 主循环：
        1. 读取用户输入（EOF/Ctrl+D 退出，/quit 或 /exit 退出）
        2. 支持 slash 命令：/help /reset /usage /soul /memory
        3. 普通输入：调用 build_main_graph().ainvoke(state) 获取响应
        4. 每轮调用 MemoryManager.after_turn() 更新记忆
        5. 打印响应 + 当前 agent + risk_tier
    - 退出时打印 session 摘要（轮数、token 估算）

防注入硬约束（input-guardrails.md）：
    - 用户输入仅作为 LLM message content，绝不拼接到 shell/exec/eval
    - 所有 LLM 调用走 build_main_graph，由 input_guard 节点做规则校验
    - slash 命令白名单，未知 / 开头的输入当普通文本送 LLM（不作为本地指令）

不引入新依赖：仅用 stdlib（input/asyncio/sys）+ 已有 deadman 模块。
不用 readline/curses（保持依赖最小）。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Optional
from uuid import uuid4

from . import __version__
from .memory.manager import MemoryManager
from .orchestration.graph import build_main_graph
from .orchestration.state import create_initial_state
from .soul_loader import SoulLoader

logger = logging.getLogger(__name__)


# slash 命令白名单（与 input-guardrails 第七章协同：用户输入不能覆盖系统规则）
_SLASH_COMMANDS: dict[str, str] = {
    "/help": "显示帮助",
    "/reset": "清空当前会话工作记忆",
    "/usage": "显示当前 session token 用量（从 metrics_collector 拉）",
    "/soul": "显示当前 SOUL.md 内容（或默认）",
    "/memory": "显示当前 4 层记忆状态",
    "/quit": "退出 REPL",
    "/exit": "退出 REPL",
}


class ChatREPL:
    """交互式对话 REPL - `deadman chat` 子命令实现。

    设计原则：
        - 单进程单事件循环（asyncio.run 包裹整个 REPL，避免嵌套事件循环）
        - 用户输入仅作为 LLM message content（防注入硬约束）
        - 异常不退出 REPL，捕获并打印错误后继续（韧性优先）
        - 不用 readline/curses（依赖最小，纯 input()）

    用法：
        repl = ChatREPL()
        repl.run()  # 阻塞，直到用户 /quit 或 EOF
    """

    def __init__(
        self,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        soul_loader: Optional[SoulLoader] = None,
        memory_manager: Optional[MemoryManager] = None,
        graph: Any = None,
        stdin: Any = None,
        stdout: Any = None,
    ) -> None:
        """初始化 REPL。

        Args:
            user_id: 用户 ID，默认 "default-user"
            session_id: 会话 ID，默认自动生成 uuid4
            soul_loader: SoulLoader 实例，默认新建
            memory_manager: MemoryManager 实例，默认新建
            graph: 已构建的编排图，默认 build_main_graph()
            stdin: 输入流（测试用），默认 sys.stdin
            stdout: 输出流（测试用），默认 sys.stdout
        """
        self.user_id: str = user_id or "default-user"
        self.session_id: str = session_id or f"sess-{uuid4().hex[:8]}"
        self.soul_loader: SoulLoader = soul_loader or SoulLoader()
        self.memory_manager: MemoryManager = memory_manager or MemoryManager()
        self._graph: Any = graph  # 懒加载，避免构造时副作用
        self._stdin = stdin or sys.stdin
        self._stdout = stdout or sys.stdout

        # 统计指标
        self.turn_count: int = 0
        self.total_input_chars: int = 0
        self.total_output_chars: int = 0
        self.last_agent: str = ""
        self.last_risk_tier: str = ""

    # ==================================================================
    # 公开 API
    # ==================================================================

    def run(self) -> int:
        """启动 REPL 主循环（阻塞）。

        Returns:
            退出码：0 正常退出，1 异常退出
        """
        try:
            asyncio.run(self._async_run())
            return 0
        except KeyboardInterrupt:
            self._print("\n[已中断]")
            return 0
        except Exception as e:
            logger.error("REPL 异常退出: %s", e, exc_info=True)
            self._print(f"[错误] REPL 异常: {type(e).__name__}: {e}")
            return 1
        finally:
            self._print_session_summary()

    # ==================================================================
    # 异步主循环
    # ==================================================================

    async def _async_run(self) -> None:
        """异步主循环：打印 banner → start_session → 循环处理输入"""
        self._print_banner()
        # 启动会话：恢复历史记忆
        try:
            self.memory_manager.start_session(self.user_id, self.session_id)
        except Exception as e:
            self._print(f"[警告] 会话启动失败（继续）: {type(e).__name__}: {e}")

        while True:
            # 读取输入
            try:
                user_input = self._read_input()
            except EOFError:
                self._print("\n[EOF] 退出")
                return
            if user_input is None:
                # /quit 或 /exit 已处理
                return

            # 异常不退出 REPL：捕获并打印错误后继续
            try:
                await self._handle_input(user_input)
            except Exception as e:
                logger.error("处理输入异常: %s", e, exc_info=True)
                self._print(f"[错误] 处理输入时: {type(e).__name__}: {e}")

    def _read_input(self) -> Optional[str]:
        """读取一行输入。

        Returns:
            用户输入文本；/quit 或 /exit 返回 None（调用方应退出）

        Raises:
            EOFError: 用户按 Ctrl+D
        """
        try:
            self._print("", end="")  # 刷新
            line = input("deadman> ")
        except EOFError:
            raise
        except KeyboardInterrupt:
            # 重新抛出让上层处理
            raise

        text = line.strip()
        # 处理退出命令
        if text in ("/quit", "/exit"):
            self._print("[退出]")
            return None
        return text

    async def _handle_input(self, user_input: str) -> None:
        """处理单条用户输入：slash 命令或 LLM 对话"""
        if not user_input:
            return

        # slash 命令白名单（防注入：用户输入仅作为本地指令识别，不拼接到任何 shell/exec）
        if user_input.startswith("/"):
            await self._handle_slash(user_input)
            return

        # 普通输入：送 LLM graph（防注入硬约束：仅作为 message content）
        await self._handle_normal_input(user_input)

    # ==================================================================
    # slash 命令处理
    # ==================================================================

    async def _handle_slash(self, command: str) -> None:
        """处理 slash 命令（白名单内的本地指令，不调用 LLM）"""
        cmd_key = command.split()[0] if command.split() else command

        if cmd_key == "/help":
            self._print_help()
        elif cmd_key == "/reset":
            self._handle_reset()
        elif cmd_key == "/usage":
            self._handle_usage()
        elif cmd_key == "/soul":
            self._handle_soul()
        elif cmd_key == "/memory":
            await self._handle_memory()
        else:
            # 未知 slash 命令：当普通文本送 LLM（防注入：不作为本地指令执行）
            self._print(
                f"[未知命令] {cmd_key}（当普通文本送 LLM）。"
                f"可用命令：{', '.join(_SLASH_COMMANDS.keys())}"
            )
            await self._handle_normal_input(command)

    def _print_help(self) -> None:
        """打印帮助"""
        self._print("=== deadman chat 帮助 ===")
        for cmd, desc in _SLASH_COMMANDS.items():
            self._print(f"  {cmd:<10} {desc}")
        self._print("")
        self._print("直接输入文本即可与 deadman 对话；Ctrl+D 或 /quit 退出。")

    def _handle_reset(self) -> None:
        """清空当前会话工作记忆"""
        # 保留 session_id，只清 recent_turns 和 temp_vars
        self.memory_manager.working.recent_turns = []
        self.memory_manager.working.temp_vars = {}
        self.turn_count = 0
        self.total_input_chars = 0
        self.total_output_chars = 0
        self._print("[已清空] 当前会话工作记忆已重置")

    def _handle_usage(self) -> None:
        """显示当前 session token 用量估算（从 metrics_collector 拉）"""
        try:
            from .observability import metrics_collector
            # 拉取本轮 session 相关指标
            input_tokens = metrics_collector.get_metric(
                "efficiency.token_input_count",
                tags={"session_id": self.session_id},
            )
            output_tokens = metrics_collector.get_metric(
                "efficiency.token_output_count",
                tags={"session_id": self.session_id},
            )
            in_count = int(input_tokens.get("sum", 0))
            out_count = int(output_tokens.get("sum", 0))
            total = in_count + out_count
            self._print("=== 当前 Session 用量 ===")
            self._print(f"  轮数：{self.turn_count}")
            self._print(f"  输入字符数（估算）：{self.total_input_chars}")
            self._print(f"  输出字符数（估算）：{self.total_output_chars}")
            self._print(f"  metrics 输入 token：{in_count}")
            self._print(f"  metrics 输出 token：{out_count}")
            self._print(f"  合计 token（metrics）：{total}")
        except Exception as e:
            self._print(f"[用量] 无法拉取 metrics：{type(e).__name__}: {e}")
            # 兜底：用字符数估算
            self._print(
                f"  轮数：{self.turn_count}  输入字符：{self.total_input_chars}  "
                f"输出字符：{self.total_output_chars}"
            )

    def _handle_soul(self) -> None:
        """显示当前 SOUL.md 内容（或默认）"""
        soul = self.soul_loader.get_soul()
        self._print("=== 当前 SOUL ===")
        self._print(soul)

    async def _handle_memory(self) -> None:
        """显示当前 4 层记忆状态（调用 cmd_memory_list 逻辑）"""
        mgr = self.memory_manager
        self._print("=== 分层记忆状态 ===")
        # working
        wm_turns = len(mgr.working.recent_turns)
        self._print(
            f"  working    条目={wm_turns}  session={mgr.working.session_id or '(无)'}"
        )
        # episodic
        ep_count = len(mgr.episodic._store) if hasattr(mgr.episodic, "_store") else 0
        self._print(f"  episodic   条目={ep_count}")
        # semantic
        self._print(
            f"  semantic   profiles={len(mgr.semantic.user_profiles)}  "
            f"facts={len(mgr.semantic.facts)}  "
            f"contradictions={len(mgr.semantic.pending_contradictions)}"
        )
        # procedural
        proc_count = (
            len(mgr.procedural._procedures)
            if hasattr(mgr.procedural, "_procedures")
            else 0
        )
        self._print(f"  procedural 流程定义={proc_count}")
        # 后端
        self._print(
            f"  Graphiti: {'启用' if mgr.graphiti is not None else '未启用'}  "
            f"LightRAG: {'启用' if mgr.lightrag is not None else '未启用'}  "
            f"FileMemoryStore: {'启用' if mgr.file_store is not None else '未启用'}"
        )

    # ==================================================================
    # 普通输入处理（调用 LLM graph）
    # ==================================================================

    async def _handle_normal_input(self, user_input: str) -> None:
        """普通输入：调用 build_main_graph().ainvoke(state) 获取响应

        防注入硬约束：user_input 仅作为 ConversationState.user_input 字段，
        由 graph 的 input_guard 节点做规则校验，不拼接到任何 shell/exec/eval。
        """
        # 懒加载 graph（避免构造时副作用）
        graph = self._graph if self._graph is not None else build_main_graph()

        # 构造初始 state：把当前 working memory 的 user_profile 注入
        profile = self.memory_manager.working.temp_vars.get("user_profile")
        user_profile_dict: dict[str, Any] = {}
        if profile is not None:
            # UserProfile dataclass 转 dict（粗略，只取可序列化字段）
            try:
                user_profile_dict = {
                    "user_id": profile.user_id,
                    "name": profile.name,
                    "relationship_to_deceased": profile.relationship_to_deceased,
                    "location": profile.location,
                    "current_stage": profile.current_stage,
                }
            except Exception:
                user_profile_dict = {}

        state = create_initial_state(
            user_input=user_input,
            user_profile=user_profile_dict,
            session_id=self.session_id,
        )

        # 调用 graph（可能抛异常，由上层捕获）
        result = await graph.ainvoke(state)

        # 提取响应
        response = result.get("final_response", "") or "(无响应)"
        agent = result.get("current_agent", "?")
        risk_tier = "R0"
        # 从 rule_check 结果提取 risk_tier
        rule_check = result.get("rule_check")
        if rule_check is not None and hasattr(rule_check, "risk_tier"):
            risk_tier = rule_check.risk_tier or "R0"

        # 更新记忆（每轮调用 after_turn）
        try:
            await self.memory_manager.after_turn(
                user_id=self.user_id,
                user_input=user_input,
                assistant_response=response,
                agent=agent,
                risk_tier=risk_tier,
                rule_check_result=rule_check,
                transfer_triggered=bool(result.get("pending_transfer")),
                subagents_called=[r.get("agent", "") for r in result.get("subagent_results", [])] if isinstance(result.get("subagent_results"), list) else [],
            )
        except Exception as e:
            logger.warning("after_turn 更新记忆失败: %s", e)

        # 打印响应 + agent + risk_tier
        self._print("")
        self._print(response)
        self._print(f"\n[agent={agent} | risk={risk_tier}]")

        # 更新统计
        self.turn_count += 1
        self.total_input_chars += len(user_input)
        self.total_output_chars += len(response)
        self.last_agent = agent
        self.last_risk_tier = risk_tier

    # ==================================================================
    # 辅助
    # ==================================================================

    def _print_banner(self) -> None:
        """打印启动 banner"""
        soul = self.soul_loader.get_soul()
        # 取 SOUL 首行作为简短身份
        soul_first_line = soul.split("\n", 1)[0] if soul else "deadman"
        self._print("=" * 60)
        self._print(f"deadman v{__version__} - 交互式对话 REPL")
        self._print(f"身份：{soul_first_line}")
        self._print(f"session: {self.session_id}  user: {self.user_id}")
        self._print("输入 /help 查看命令；Ctrl+D 或 /quit 退出")
        self._print("=" * 60)
        self._print("")

    def _print_session_summary(self) -> None:
        """打印 session 摘要"""
        self._print("")
        self._print("=== Session 摘要 ===")
        self._print(f"  总轮数：{self.turn_count}")
        self._print(f"  输入字符数（估算）：{self.total_input_chars}")
        self._print(f"  输出字符数（估算）：{self.total_output_chars}")
        # 粗略 token 估算：中文 1 字 ≈ 1 token，英文 4 字符 ≈ 1 token
        est_tokens = (
            self.total_input_chars + self.total_output_chars
        )  # 保守上界
        self._print(f"  token 估算（粗略上界）：{est_tokens}")
        if self.last_agent:
            self._print(f"  最后 agent：{self.last_agent}  risk：{self.last_risk_tier}")

    def _print(self, text: str, end: str = "\n") -> None:
        """统一输出（便于测试时 mock）"""
        try:
            self._stdout.write(text + end)
            self._stdout.flush()
        except Exception:
            pass
