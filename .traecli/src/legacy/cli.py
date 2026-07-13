"""CLI 入口 - 命令行工具"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from . import __version__


def setup_logging(level: str = "INFO"):
    """配置日志"""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_version(args):
    """显示版本"""
    print(f"Legacy v{__version__} (死者为大 / 終活)")


def cmd_mcp_server(args):
    """启动 MCP Server"""
    from .mcp_server.server import main as server_main
    server_main()


def cmd_eval(args):
    """运行评估"""
    from .evaluation.runner import run_all_cases
    from .config import settings

    cases_dir = args.cases_dir or str(settings.tests_dir / "automated" / "cases")
    result = asyncio.run(run_all_cases(cases_dir))

    print(f"\n=== 评估结果 ===")
    print(f"总数: {result['total']}")
    print(f"通过: {result['passed']}")
    print(f"失败: {result['failed']}")
    print(f"通过率: {result['pass_rate']:.1%}")

    if args.verbose:
        for r in result["results"]:
            status = "✓" if r["passed"] else "✗"
            print(f"  {status} case-{r['case_id']}: {r.get('name', '')}")
            if not r["passed"]:
                for fail in r.get("failures", []):
                    print(f"      - {fail}")


def cmd_run(args):
    """运行单次对话"""
    from .orchestration.graph import build_main_graph, create_initial_state

    graph = build_main_graph()
    state = create_initial_state(user_input=args.input)

    result = asyncio.run(graph.ainvoke(state))

    print(f"\n=== 响应 ===")
    print(result.get("final_response", "(无响应)"))
    print(f"\n=== 智能体: {result.get('current_agent', '?')} ===")
    print(f"=== 风险等级: {result.get('risk_tier', '?')} ===")


def main():
    """CLI 主入口"""
    parser = argparse.ArgumentParser(
        prog="legacy",
        description="Legacy / 死者为大 / 終活 - 身后事多智能体平台 CLI",
    )
    parser.add_argument("--version", action="store_true", help="显示版本")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细输出")
    parser.add_argument("--log-level", default="INFO", help="日志级别")

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # mcp-server 子命令
    subparsers.add_parser("mcp-server", help="启动 MCP Server")

    # eval 子命令
    eval_parser = subparsers.add_parser("eval", help="运行评估")
    eval_parser.add_argument("--cases-dir", help="YAML case 目录路径")

    # run 子命令
    run_parser = subparsers.add_parser("run", help="运行单次对话")
    run_parser.add_argument("input", help="用户输入")

    args = parser.parse_args()

    setup_logging(args.log_level)

    if args.version:
        cmd_version(args)
        return

    if args.command == "mcp-server":
        cmd_mcp_server(args)
    elif args.command == "eval":
        cmd_eval(args)
    elif args.command == "run":
        cmd_run(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
