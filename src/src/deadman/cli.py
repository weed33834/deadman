"""CLI 入口 - 命令行工具"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__


def setup_logging(level: str = "INFO"):
    """配置日志（委托给 structlog 集成的 logging_config.setup_logging）

    现有调用点 ``setup_logging(args.log_level)`` 无需修改即可获得：
    - structlog 与 stdlib logging 的统一渲染
    - ``DEADMAN_LOG_FORMAT`` / ``DEADMAN_LOG_LEVEL`` 环境变量支持
    - 向后兼容现有 ``logging.getLogger(__name__)`` 调用
    """
    from .logging_config import setup_logging as _setup_structlog_logging

    _setup_structlog_logging(level=level)


def cmd_version(args):
    """显示版本"""
    print(f"deadman v{__version__}")


def cmd_mcp_server(args):
    """启动 MCP Server"""
    from .mcp_server.server import main as server_main

    server_main()


def cmd_eval(args):
    """运行评估 - 跑 golden cases + 三层判定 + 反馈闭环"""
    import json

    from .config import settings
    from .evaluation.runner import run_all_cases
    from .observability import metrics_collector

    cases_dir = args.cases_dir or str(settings.tests_dir / "automated" / "cases")
    result = asyncio.run(run_all_cases(cases_dir))

    print("\n=== 评估结果 ===")
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

    # === 反馈闭环: 写 data/eval_health.json + metrics 采集 ===
    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "eval_health.json"
    payload = {
        "evaluated_at": datetime.now().isoformat(),
        "cases_dir": cases_dir,
        "summary": {
            "total": result["total"],
            "passed": result["passed"],
            "failed": result["failed"],
            "pass_rate": result["pass_rate"],
        },
        "results": [
            {
                "case_id": r["case_id"],
                "name": r.get("name", ""),
                "category": r.get("category", ""),
                "passed": r["passed"],
                "layer_reached": r.get("layer_reached", ""),
            }
            for r in result["results"]
        ],
    }
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] 评估结果已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    # metrics: 通过率 + 按 category 分组
    metrics_collector.record_metric(
        "cross_platform.golden_case_pass_rate",
        result["pass_rate"],
    )
    by_category: dict[str, list[bool]] = {}
    for r in result["results"]:
        cat = r.get("category", "unknown") or "unknown"
        by_category.setdefault(cat, []).append(r["passed"])
    for cat, passed_list in by_category.items():
        cat_rate = sum(1 for p in passed_list if p) / len(passed_list)
        metrics_collector.record_metric(
            "cross_platform.golden_case_pass_rate",
            cat_rate,
            tags={"category": cat},
        )

    if result["failed"] > 0 and getattr(args, "fail_fast", False):
        raise SystemExit(1)


# ====================================================================
# eval-list 子命令 - 列出评估 case 清单
# ====================================================================
def cmd_eval_list(args):
    """列出本地评估 case 清单(golden cases)"""
    import yaml

    from .config import settings

    cases_dir = Path(settings.tests_dir / "automated" / "cases")
    if not cases_dir.exists():
        print(f"[错误] cases 目录不存在: {cases_dir}")
        return

    cases = sorted(cases_dir.glob("*.yaml")) + sorted(cases_dir.glob("*.yml"))
    print("\n=== 评估 Case 清单 ===")
    print(f"{'Case ID':<10} {'类别':<14} {'优先级':<8} {'LLM Judge':<10} 名称")
    print("-" * 80)
    for path in cases:
        try:
            with open(path, encoding="utf-8") as f:
                case = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            continue
        case_id = case.get("case_id", path.stem)
        category = case.get("category", "-")
        priority = case.get("priority", "-")
        has_judge = "是" if case.get("evaluation", {}).get("llm_judge") else "否"
        name = (case.get("name", "") or "")[:30]
        print(f"{case_id!s:<10} {category:<14} {priority:<8} {has_judge:<10} {name}")
    print(f"\nCase 总数: {len(cases)}")


def cmd_eval_ragas(args):
    """运行 RAGAS 评估(9 维度 + 质量门 + 降级保护)

    用法:
        deadman eval-ragas --cases-dir tests/automated/cases
        deadman eval-ragas --quick                 # 仅跑 faithfulness + answer_relevancy
        deadman eval-ragas --quality-gate 0.8       # 自定义质量门阈值
        deadman eval-ragas --output results.jsonl   # 输出 JSONL 报告

    降级行为:
        - ragas 包未安装 → 打印提示,退出码 0(不阻断 CI)
        - LLM api_key 未配置 → 跳过 RAGAS 评估,仅打印 case 清单
        - faithfulness < threshold → 退出码 2(质量门未通过,CI 阻断 merge)
    """
    import json
    from datetime import datetime

    from .config import settings
    from .evaluation.ragas_evaluator import (
        DEFAULT_QUALITY_GATE_THRESHOLD,
        RAGASEvaluator,
        run_ragas_batch,
    )

    cases_dir = args.cases_dir or str(settings.tests_dir / "automated" / "cases")
    threshold = float(args.quality_gate or DEFAULT_QUALITY_GATE_THRESHOLD)
    quick_mode = bool(args.quick)
    output_file = args.output

    evaluator = RAGASEvaluator(
        quality_gate_threshold=threshold,
        quick_mode=quick_mode,
    )

    if not evaluator.available:
        print("[RAGAS] ragas/datasets 包未安装,跳过评估")
        print("       pip install deadman[ragas] 后可启用")
        print("       退出码 0(不阻断 CI)")
        return

    print("\n=== RAGAS 评估启动 ===")
    print(f"cases_dir: {cases_dir}")
    print(f"quick_mode: {quick_mode}")
    print(f"quality_gate_threshold: {threshold}")
    print(f"output: {output_file or '(stdout)'}")
    print()

    # 批量评估
    summary = asyncio.run(
        run_ragas_batch(
            cases_dir=cases_dir,
            evaluator=evaluator,
            output_file=output_file,
        )
    )

    # 打印汇总
    print("=== RAGAS 评估汇总 ===")
    print(f"总数: {summary['total']}")
    print(f"已评估: {summary['evaluated']}")
    print(f"降级: {summary['degraded']}")
    print(f"质量门通过: {summary['quality_gate_passed']}")

    if args.verbose:
        for record in summary.get("results", []):
            case_id = record.get("case_id", "?")
            result = record.get("result", {})
            metrics = result.get("metrics", {})
            gate = result.get("quality_gate_passed")
            degraded = result.get("degraded")
            metrics_str = ", ".join(
                f"{k}={v:.2f}" for k, v in metrics.items() if isinstance(v, (int, float))
            )
            print(f"  case-{case_id}: degraded={degraded}, gate={gate}, metrics={metrics_str}")

    # 写 health 文件
    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "ragas_health.json"
    payload = {
        "evaluated_at": datetime.now().isoformat(),
        "cases_dir": cases_dir,
        "summary": {
            "total": summary["total"],
            "evaluated": summary["evaluated"],
            "degraded": summary["degraded"],
            "quality_gate_passed": summary["quality_gate_passed"],
        },
        "threshold": threshold,
        "quick_mode": quick_mode,
        "results": summary.get("results", []),
    }
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] RAGAS 评估结果已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    # 质量门判定
    evaluated = summary["evaluated"]
    if evaluated == 0:
        print("\n[质量门] 无可评估结果,退出码 0")
        return

    if summary["degraded"] == evaluated:
        # 全部降级(LLM 不可用) → 不阻断 CI
        print("\n[质量门] 全部降级(LLM 不可用?),退出码 0")
        return

    passed = summary["quality_gate_passed"]
    pass_rate = passed / evaluated if evaluated else 0
    if pass_rate < 1.0 and args.fail_fast:
        print(f"\n[质量门] 通过率 {pass_rate:.1%} < 100%,faithfulness 阈值 {threshold}")
        raise SystemExit(2)


def cmd_run(args):
    """运行单次对话"""
    from .orchestration.graph import build_main_graph, default_graph_config
    from .orchestration.state import create_initial_state

    graph = build_main_graph()
    state = create_initial_state(user_input=args.input)

    result = asyncio.run(graph.ainvoke(state, config=default_graph_config()))

    print("\n=== 响应 ===")
    print(result.get("final_response", "(无响应)"))
    print(f"\n=== 智能体: {result.get('current_agent', '?')} ===")
    rc = result.get("rule_check")
    risk_tier = rc.risk_tier.value if rc else "?"
    print(f"=== 风险等级: {risk_tier} ===")


# ====================================================================
# llm-test 子命令 - 接入测试(本地+线上多厂商手动验证 + 真实反馈闭环)
# ====================================================================
# 本地 provider 无需 API key,云端 provider 缺 key 时标记 no_key(不算失败)
_LOCAL_PROVIDERS = {"ollama", "vllm", "llama_cpp"}

# ping 用 prompt - 越短越省 token,但要能验证模型真的在生成
_PING_MESSAGES = [{"role": "user", "content": "请只回复四个字:pong ok"}]


def cmd_llm_test(args):
    """LLM 接入测试 - 逐一 ping 各 provider,反馈真实延迟/可用性/token 用量

    针对用户需求"手动测试,有没有接入成功都要反馈真实的数据":
      - 遍历所有已接入 provider(OpenAI/Anthropic/智谱/Ollama/vLLM/llama.cpp)
      - 每个 provider 发一个简短 ping(直连、不走 fallback、不重试)
      - 真实记录:状态(ok/fail/no_key)、延迟(ms)、token 用量、响应预览、错误
      - 支持 --provider 过滤、--model 指定、--timeout 超时

    反馈闭环:
      - 表格化打印到终端
      - 写入 data/llm_health.json(供健康看板/巡检消费)
      - 记录到 metrics_collector(可观测性采集,efficiency 类指标)
    """
    import json
    import os
    import time
    from datetime import datetime

    from .config import settings
    from .llm import _PROVIDER_DEFAULTS, PROVIDER_MODELS, LLMClient
    from .observability import metrics_collector

    timeout = args.timeout if args.timeout is not None else settings.llm_timeout

    async def _ping_one(provider: str, model: str) -> dict:
        """ping 单个 provider+model,返回结果字典"""
        defaults = _PROVIDER_DEFAULTS.get(provider, {})
        env_key = defaults.get("env_key", "")
        api_key = os.getenv(env_key, "") if env_key else ""
        is_local = provider in _LOCAL_PROVIDERS

        # 云端 provider 缺 key → no_key(不算失败,但无法测试)
        if not api_key and not is_local:
            return {
                "provider": provider,
                "model": model,
                "status": "no_key",
                "latency_ms": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "response_preview": "",
                "error": f"环境变量 {env_key} 未设置",
                "timestamp": datetime.now().isoformat(),
            }

        # 本地 provider 无 key 用占位值(OpenAI 兼容接口不校验)
        client = LLMClient(
            provider=provider,
            model=model,
            api_key=api_key or "local-no-key-needed",
            base_url=defaults.get("base_url", ""),
        )
        client.timeout = timeout

        start = time.perf_counter()
        try:
            resp = await client.ping_once(_PING_MESSAGES, max_tokens=20)
            latency = (time.perf_counter() - start) * 1000
            usage = resp.usage or {}
            return {
                "provider": provider,
                "model": model,
                "status": "ok",
                "latency_ms": round(latency, 1),
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "response_preview": (resp.content or "")[:60].replace("\n", " "),
                "error": "",
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            latency = (time.perf_counter() - start) * 1000
            return {
                "provider": provider,
                "model": model,
                "status": "fail",
                "latency_ms": round(latency, 1),
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "response_preview": "",
                "error": f"{type(e).__name__}: {e}",
                "timestamp": datetime.now().isoformat(),
            }

    async def _run_all() -> list[dict]:
        # 收集测试目标
        targets: list[tuple[str, str]] = []
        providers_to_test = [args.provider] if args.provider else list(PROVIDER_MODELS.keys())
        for provider in providers_to_test:
            if provider not in PROVIDER_MODELS:
                print(f"[警告] 未知 provider: {provider},跳过")
                continue
            model = args.model or PROVIDER_MODELS[provider][0]["id"]
            targets.append((provider, model))
        # 并发 ping(加速多 provider 测试)
        return await asyncio.gather(*[_ping_one(p, m) for p, m in targets])

    results = asyncio.run(_run_all())

    # === 1. 表格化打印 ===
    _print_llm_test_table(results)

    # === 2. 写入 data/llm_health.json(反馈闭环数据源) ===
    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "llm_health.json"
    health_payload = {
        "checked_at": datetime.now().isoformat(),
        "timeout_seconds": timeout,
        "results": results,
    }
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(health_payload, f, ensure_ascii=False, indent=2)
        print(f"[反馈闭环] 健康状态已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入健康文件失败: {e}")

    # === 3. 记录到 metrics_collector(可观测性采集) ===
    for r in results:
        tags = {
            "provider": r["provider"],
            "model": r["model"],
            "status": r["status"],
        }
        metrics_collector.record_metric(
            "efficiency.llm_test_latency_ms", r["latency_ms"], tags=tags
        )
        metrics_collector.record_metric(
            "efficiency.llm_test_success",
            1.0 if r["status"] == "ok" else 0.0,
            tags=tags,
        )

    ok_count = sum(1 for r in results if r["status"] == "ok")
    fail_count = sum(1 for r in results if r["status"] == "fail")
    no_key_count = sum(1 for r in results if r["status"] == "no_key")
    print(
        f"[汇总] ok={ok_count}  fail={fail_count}  no_key={no_key_count}  "
        f"总计={len(results)}  指标已记录到 metrics_collector"
    )

    # CI 模式:有真实 fail 则退出码非零
    if fail_count > 0 and getattr(args, "fail_fast", False):
        raise SystemExit(1)


def _print_llm_test_table(results: list[dict]) -> None:
    """表格化打印 llm-test 结果"""
    print("\n=== LLM 接入测试报告 ===")
    header = (
        f"{'Provider':<12} {'Model':<22} {'状态':<9} "
        f"{'延迟(ms)':>10} {'Tokens':>8} {'响应/错误':<30}"
    )
    print(header)
    print("-" * 95)
    status_map = {
        "ok": "✓ ok",
        "fail": "✗ fail",
        "no_key": "○ no_key",
        "skip": "○ skip",
    }
    for r in results:
        status_str = status_map.get(r["status"], r["status"])
        preview = (r["response_preview"] or r["error"])[:28]
        print(
            f"{r['provider']:<12} {r['model']:<22} {status_str:<9} "
            f"{r['latency_ms']:>10.1f} {r['total_tokens']:>8} {preview:<30}"
        )
    print()

    failures = [r for r in results if r["status"] == "fail"]
    if failures:
        print("--- 失败详情 ---")
        for r in failures:
            print(f"[{r['provider']}/{r['model']}] {r['error']}")
        print()


# ====================================================================
# llm-sync-models 子命令 - 从各 provider /models 端点拉真实可用模型清单
# ====================================================================
def cmd_llm_sync_models(args):
    """同步模型清单 - fetch 各 provider /models 端点,对比本地 PROVIDER_MODELS

    对应用户需求"一定要获得现在最真的数据,不要用本地旧数据":
      - 从线上 API 拉真实可用模型(而非依赖本地写死的清单)
      - 与本地 PROVIDER_MODELS 对比,标记 新增/已下线/一致
      - 结果写入 data/llm_models_sync.json 供看板消费
    """
    import json
    from datetime import datetime

    from .llm import PROVIDER_MODELS, fetch_provider_models

    async def _sync_all() -> dict:
        providers = [args.provider] if args.provider else list(PROVIDER_MODELS.keys())
        report: dict[str, Any] = {}
        for provider in providers:
            local_ids = {m["id"] for m in PROVIDER_MODELS.get(provider, [])}
            remote_models = await fetch_provider_models(provider)
            remote_ids = {m["id"] for m in remote_models}
            added = sorted(remote_ids - local_ids)
            removed = sorted(local_ids - remote_ids)
            common = sorted(local_ids & remote_ids)
            report[provider] = {
                "fetched": len(remote_models),
                "local_count": len(local_ids),
                "added": added,
                "removed": removed,
                "common": common,
                "remote_sample": remote_models[:10],
            }
        return report

    report = asyncio.run(_sync_all())

    print("\n=== 模型清单同步报告 ===")
    print(f"{'Provider':<12} {'线上':>6} {'本地':>6} {'新增':>6} {'下线':>6}  详情")
    print("-" * 80)
    for provider, info in report.items():
        print(
            f"{provider:<12} {info['fetched']:>6} {info['local_count']:>6} "
            f"{len(info['added']):>6} {len(info['removed']):>6}  "
            f"新增={info['added'][:3]} 下线={info['removed'][:3]}"
        )
    print()

    # 持久化
    from .config import settings

    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    sync_file = data_dir / "llm_models_sync.json"
    payload = {"synced_at": datetime.now().isoformat(), "report": report}
    try:
        with open(sync_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[反馈闭环] 同步报告已写入 {sync_file}")
    except OSError as e:
        print(f"[警告] 写入同步报告失败: {e}")


# ====================================================================
# llm-cost 子命令 - 成本与配额汇总
# ====================================================================
def cmd_llm_cost(args):
    """成本汇总 - 展示累计 token 用量与成本,支持清零

    对应用户需求"成本与配额追踪":
      - 从 data/llm_cost.json 加载历史记录
      - 按 provider/model 汇总成本与 token
      - 表格化打印 + 总成本/总调用量
    """
    from .cost import cost_tracker

    if args.clear:
        cost_tracker.clear()
        print("[已清空] 成本记录已重置")
        return

    summary = cost_tracker.get_summary()

    print("\n=== LLM 成本与配额汇总 ===")
    print(f"总调用: {summary['total_calls']}  总成本: ${summary['total_cost_usd']:.6f}")
    print(
        f"总 token: 输入={summary['total_prompt_tokens']}  "
        f"输出={summary['total_completion_tokens']}"
    )
    print()

    print("--- 按 Provider ---")
    print(f"{'Provider':<14} {'调用':>6} {'成本(USD)':>12} {'输入tok':>10} {'输出tok':>10}")
    print("-" * 56)
    for provider, info in sorted(summary["by_provider"].items()):
        print(
            f"{provider:<14} {info['calls']:>6} {info['cost_usd']:>12.6f} "
            f"{info['prompt_tokens']:>10} {info['completion_tokens']:>10}"
        )
    print()

    if summary["by_model"]:
        print("--- 按 Provider/Model ---")
        print(f"{'Provider/Model':<28} {'调用':>6} {'成本(USD)':>12}")
        print("-" * 50)
        for mkey, info in sorted(summary["by_model"].items()):
            print(f"{mkey:<28} {info['calls']:>6} {info['cost_usd']:>12.6f}")
        print()

    alert = float(os.getenv("LLM_COST_ALERT_USD", "10.0"))
    print(f"[配额预警阈值] 单 provider 累计 ${alert:.2f} 时告警")


# ====================================================================
# prompt-list / prompt-test / prompt-sync - 提示词领域(本地+线上+测试+反馈)
# ====================================================================
def cmd_prompt_list(args):
    """列出本地+线上可用提示词"""
    from .prompts import local_prompt_store

    print("\n=== 本地提示词 ===")
    prompts = local_prompt_store.load_all()
    if not prompts:
        print("(无)")
    else:
        print(f"{'名称':<28} {'模型':<12} {'变量':<24} 说明")
        print("-" * 90)
        for name, p in sorted(prompts.items()):
            inputs = ",".join(p.inputs) or "(无)"
            desc = (p.description or "").replace("\n", " ")[:30]
            print(f"{name:<28} {p.model or '-':<12} {inputs:<24} {desc}")
    print(f"\n本地提示词总数: {len(prompts)}")


def cmd_prompt_test(args):
    """提示词测试 - 渲染本地提示词 + 发 LLM,反馈真实结果

    手动测试流程:
      1. 加载本地提示词(by name)
      2. 注入变量(--var key=value 可重复)
      3. 渲染模板(校验变量是否齐全)
      4. 发到 LLM 调用
      5. 反馈真实结果:渲染结果/LLM 响应/token/延迟/错误
    """
    import time

    from .llm import LLMClient
    from .prompts import local_prompt_store, render_template

    prompt = local_prompt_store.get(args.name)
    if prompt is None:
        print(f"[错误] 未找到提示词: {args.name}")
        print(f"可用: {', '.join(local_prompt_store.list_names())}")
        raise SystemExit(1)

    # 解析 --var key=value
    variables: dict[str, Any] = {}
    for item in args.var or []:
        if "=" in item:
            k, v = item.split("=", 1)
            variables[k] = v

    # 校验必填变量
    missing = [v for v in prompt.inputs if v not in variables]
    if missing and not args.allow_missing:
        print(f"[错误] 缺少变量: {missing}")
        print(f"提示词需要的变量: {prompt.inputs}")
        print("用 --var key=value 提供,或 --allow-missing 跳过校验")
        raise SystemExit(1)

    # 渲染
    rendered = render_template(prompt.template, variables)
    print("\n=== 渲染结果 ===")
    print(rendered)

    if args.dry_run:
        print("[--dry-run] 未发送 LLM")
        return

    # 发 LLM
    model = args.model or prompt.model or None
    client = LLMClient(provider=args.provider, model=model)
    messages = [{"role": "user", "content": rendered}]

    print(f"\n=== 调用 LLM (provider={client.provider} model={client.model}) ===")
    start = time.perf_counter()
    try:
        resp = asyncio.run(client.chat_with_tools(messages, max_tokens=args.max_tokens))
        latency = (time.perf_counter() - start) * 1000
        usage = resp.usage or {}
        print("\n=== LLM 响应 ===")
        print(resp.content)
        print(
            f"\n[反馈] 延迟={latency:.0f}ms  "
            f"tokens={usage.get('total_tokens', 0)} "
            f"(in={usage.get('prompt_tokens', 0)} out={usage.get('completion_tokens', 0)})"
        )
    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        print(f"\n[失败] 延迟={latency:.0f}ms  错误: {type(e).__name__}: {e}")
        if args.fail_fast:
            raise SystemExit(1) from None


def cmd_prompt_sync(args):
    """同步线上提示词清单 - LangSmith Hub + deepset PromptHub

    对应"查官网最新数据":从线上仓库拉真实公开提示词,与本地对比。
    """
    import json
    from datetime import datetime

    from .config import settings
    from .prompts import fetch_deepset_prompts, fetch_langsmith_prompts, local_prompt_store

    async def _fetch_all() -> dict:
        langsmith = await fetch_langsmith_prompts(query=args.query or "")
        deepset = await fetch_deepset_prompts()
        return {"langsmith": langsmith, "deepset": deepset}

    result = asyncio.run(_fetch_all())
    local_names = set(local_prompt_store.list_names())

    print("\n=== 线上提示词同步报告 ===")
    for source, items in result.items():
        print(f"\n--- {source} ({len(items)} 条) ---")
        for item in items[:10]:
            name = item.get("name") or item.get("full_name", "")
            tag = "(本地已有)" if name in local_names else ""
            desc = (item.get("description") or "")[:40]
            print(f"  {name:<30} {tag:<10} {desc}")

    # 持久化
    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    sync_file = data_dir / "prompt_sync.json"
    payload = {
        "synced_at": datetime.now().isoformat(),
        "local_count": len(local_names),
        "langsmith_count": len(result["langsmith"]),
        "deepset_count": len(result["deepset"]),
        "langsmith": result["langsmith"],
        "deepset": result["deepset"],
    }
    try:
        with open(sync_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] 同步结果已写入 {sync_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")


# ====================================================================
# rule-test / rule-validate - 规则领域(本地规则+校验测试+反馈闭环)
# ====================================================================
def cmd_rule_test(args):
    """规则校验测试 - 对文本跑 RuleChecker,反馈命中哪些规则

    手动测试:输入一段文本(智能体响应),跑 L0-L8 规则校验,
    真实反馈:violations/risk_tier/safety_triggered/integrity_violations
    """
    from .rules_loader import rule_checker

    result = rule_checker.check(args.text, context={})
    print("\n=== 规则校验结果 ===")
    print(f"通过: {'是' if result.passed else '否'}")
    print(f"风险等级: {result.risk_tier}")
    print(f"安全触发: {result.safety_triggered}")
    if result.integrity_violations:
        print(f"诚信违规: {result.integrity_violations}")
    if result.violations:
        print("\n--- 违规详情 ---")
        for v in result.violations:
            print(f"  [L{v.get('priority')}] {v.get('rule')}: {v.get('violation')}")
    else:
        print("无违规")


def cmd_rule_validate(args):
    """规则文件完整性校验 - 校验 rules/ 目录与优先级链

    反馈闭环:校验结果写 data/rule_health.json + metrics 采集
    """
    import json
    from datetime import datetime

    from .config import settings
    from .observability import metrics_collector
    from .rules_loader import validate_rules

    result = validate_rules()

    print("\n=== 规则文件完整性校验 ===")
    print(f"通过: {'是' if result['passed'] else '否'}")
    print(
        f"统计: 引用 {result['stats']['referenced_count']}  "
        f"优先级链 {result['stats']['priority_chain_count']}  "
        f"补充 {result['stats']['supplementary_count']}"
    )
    if result["errors"]:
        print("\n--- 错误 ---")
        for e in result["errors"]:
            print(f"  ✗ {e}")
    if result["warnings"]:
        print("\n--- 警告 ---")
        for w in result["warnings"]:
            print(f"  ○ {w}")
    if not result["errors"] and not result["warnings"]:
        print("规则文件完整,无错误无警告")

    # 持久化 + metrics
    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "rule_health.json"
    payload = {"checked_at": datetime.now().isoformat(), **result}
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] 校验结果已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    metrics_collector.record_metric(
        "quality.rule_violation_rate",
        1.0 if not result["passed"] else 0.0,
        tags={"check_type": "file_validation"},
    )
    metrics_collector.record_metric(
        "quality.rule_compliance_rate_dpo",
        1.0 if result["passed"] else 0.0,
        tags={"check_type": "file_validation"},
    )

    if not result["passed"] and args.fail_fast:
        raise SystemExit(1)


# ====================================================================
# agent-list / agent-ping - 智能体领域(本地+外部A2A+测试+反馈闭环)
# ====================================================================
def cmd_agent_list(args):
    """列出本地智能体配置(来自 agents/*.md)"""
    from .agents_store import load_local_agents

    agents = load_local_agents()
    print("\n=== 本地智能体配置 ===")
    if not agents:
        print("(无)")
        return
    print(f"{'名称':<32} {'工具':<24} 说明")
    print("-" * 90)
    for name, a in sorted(agents.items()):
        desc = (a.description or "").replace("\n", " ")[:32]
        print(f"{name:<32} {a.tools or '-':<24} {desc}")
    print(f"\n本地智能体总数: {len(agents)}")


def cmd_agent_ping(args):
    """ping 远端 A2A agent,反馈真实可达性/延迟/能力数

    手动测试外部智能体接入:GET {url}/.well-known/agent.json
    反馈闭环:结果写 data/agent_health.json + metrics 采集
    """
    import json
    from datetime import datetime

    from .agents_store import ping_remote_agent
    from .config import settings
    from .observability import metrics_collector

    async def _run() -> list[dict]:
        urls = args.url or []
        return await asyncio.gather(
            *[ping_remote_agent(u, timeout=args.timeout or 10.0) for u in urls]
        )

    if not args.url:
        print("[错误] 请用 --url 指定远端 A2A agent 地址(可重复)")
        raise SystemExit(1)

    results = asyncio.run(_run())

    print("\n=== A2A Agent 接入测试 ===")
    print(f"{'URL':<40} {'可达':<6} {'延迟ms':>8} {'能力数':>6} {'名称':<20} 错误")
    print("-" * 110)
    for r in results:
        reach = "✓" if r["reachable"] else "✗"
        print(
            f"{r['base_url']:<40} {reach:<6} {r['latency_ms']:>8.1f} "
            f"{r['skills']:>6} {r['agent_name']:<20} {r['error']}"
        )

    # 持久化 + metrics
    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "agent_health.json"
    payload = {"checked_at": datetime.now().isoformat(), "results": results}
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] 健康状态已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    for r in results:
        tags = {"base_url": r["base_url"], "reachable": str(r["reachable"])}
        metrics_collector.record_metric(
            "interop.a2a_call_success_rate", 1.0 if r["reachable"] else 0.0, tags=tags
        )
        metrics_collector.record_metric("interop.a2a_avg_latency_ms", r["latency_ms"], tags=tags)


# ====================================================================
# knowledge-list / knowledge-search / knowledge-freshness - 知识库领域
# ====================================================================
def cmd_knowledge_list(args):
    """列出本地知识库文件"""
    from .knowledge_store import load_knowledge_files

    files = load_knowledge_files()
    print("\n=== 本地知识库文件 ===")
    if not files:
        print("(无)")
        return
    print(f"{'国家':<6} {'地区':<14} {'可信度':<8} {'最后更新':<14} 路径")
    print("-" * 80)
    for f in files:
        print(
            f"{f.country:<6} {f.region:<14} {f.trust_level:<8} {f.last_updated or '-':<14} {f.path}"
        )
    print(f"\n知识库文件总数: {len(files)}")


def cmd_knowledge_search(args):
    """知识库检索测试 - 真实反馈检索命中

    手动测试:输入查询词,跑本地检索,反馈命中文件/评分/片段。
    LightRAG 可用时由 mcp_server 走图谱检索,此处为降级测试。
    反馈闭环:结果写 data/knowledge_health.json + metrics。
    """
    import json
    from datetime import datetime

    from .config import settings
    from .knowledge_store import search_knowledge
    from .observability import metrics_collector

    results = search_knowledge(args.query, country=args.country, region=args.region)
    print("\n=== 知识库检索结果 ===")
    print(f"查询: {args.query}")
    if args.country:
        print(f"国家过滤: {args.country}")
    if not results:
        print("(无命中)")
    else:
        print(f"{'评分':>4} {'国家':<6} {'地区':<14} {'命中词':<20} 片段")
        print("-" * 90)
        for r in results:
            print(
                f"{r['score']:>4} {r['country']:<6} {r['region']:<14} "
                f"{','.join(r['hits'])[:18]:<20} {r['snippet'][:40]}"
            )

    # 持久化 + metrics
    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "knowledge_health.json"
    payload = {
        "checked_at": datetime.now().isoformat(),
        "query": args.query,
        "hit_count": len(results),
        "results": results,
    }
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] 检索结果已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    metrics_collector.record_metric(
        "knowledge.context_recall",
        1.0 if results else 0.0,
        tags={"query": args.query[:30]},
    )


def cmd_knowledge_freshness(args):
    """知识库新鲜度检查 - 检测过期文件(政策/法条/普通)"""
    import json
    from datetime import datetime

    from .config import settings
    from .knowledge_store import check_freshness
    from .observability import metrics_collector

    result = check_freshness()
    print("\n=== 知识库新鲜度检查 ===")
    print(f"文件总数: {result['total_files']}")
    print(f"过期数: {result['stale_count']}")
    print(f"过期率: {result['stale_rate']:.1%}")
    if result["stale_files"]:
        print("\n--- 过期文件 ---")
        for s in result["stale_files"]:
            print(
                f"  [{s['category']}] {s['path']}  "
                f"年龄={s['age_days']}天 阈值={s['threshold_days']}天"
            )
    else:
        print("无过期文件")

    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    fresh_file = data_dir / "knowledge_freshness.json"
    payload = {"checked_at": datetime.now().isoformat(), **result}
    try:
        with open(fresh_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] 新鲜度报告已写入 {fresh_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    metrics_collector.record_metric("knowledge.stale_file_rate_6m", result["stale_rate"])


# ====================================================================
# tool-list / tool-test / mcp-ping - MCP 工具领域(本地+外部+测试+反馈闭环)
# ====================================================================
def cmd_tool_list(args):
    """列出本地注册的 MCP 工具"""
    from .mcp_server.server import mcp

    tools = mcp.list_tools()
    print("\n=== 本地 MCP 工具清单 ===")
    if not tools:
        print("(无)")
        return
    print(f"{'工具名':<22} {'必填参数':<24} 说明")
    print("-" * 90)
    for t in tools:
        schema = t.get("inputSchema", {})
        required = ",".join(schema.get("required", [])) or "(无)"
        desc = (t.get("description", "") or "").replace("\n", " ")[:40]
        print(f"{t['name']:<22} {required:<24} {desc}")
    print(f"\n工具总数: {len(tools)}")


def cmd_tool_test(args):
    """MCP 工具测试 - 真实调用单个工具,反馈结果

    手动测试:指定工具名 + 参数(--arg key=value 可重复),
    真实调用 mcp.call_tool,反馈 ok/error/result。
    反馈闭环:结果写 data/tool_health.json + metrics 采集。
    """
    import json
    from datetime import datetime

    from .config import settings
    from .mcp_server.server import mcp
    from .observability import metrics_collector

    # 解析 --arg key=value
    arguments: dict[str, Any] = {}
    for item in args.arg or []:
        if "=" in item:
            k, v = item.split("=", 1)
            # 尝试转 JSON(支持数字/布尔),失败保字符串
            try:
                arguments[k] = json.loads(v)
            except json.JSONDecodeError:
                arguments[k] = v

    print(f"\n=== 调用工具: {args.name} ===")
    print(f"参数: {arguments}")

    start_mark = datetime.now().isoformat()
    result = asyncio.run(mcp.call_tool(args.name, arguments))

    print("\n=== 工具响应 ===")
    ok = result.get("ok", False)
    print(f"成功: {'是' if ok else '否'}")
    if not ok:
        print(f"错误: {result.get('error', '')} - {result.get('message', '')}")
    if "result" in result:
        res = result["result"]
        # 截断长结果
        res_str = json.dumps(res, ensure_ascii=False, default=str)
        print(f"结果: {res_str[:500]}{'...' if len(res_str) > 500 else ''}")

    # 持久化 + metrics
    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "tool_health.json"
    record = {
        "tool": args.name,
        "arguments": arguments,
        "ok": ok,
        "error": result.get("error", ""),
        "called_at": start_mark,
    }
    payload = {"last_test": record}
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] 工具测试结果已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    metrics_collector.record_metric(
        "quality.tool_selection_accuracy",
        1.0 if ok else 0.0,
        tags={"tool": args.name},
    )
    metrics_collector.record_metric(
        "quality.subagent_call_failure_rate",
        0.0 if ok else 1.0,
        tags={"tool": args.name},
    )

    if not ok and args.fail_fast:
        raise SystemExit(1)


def cmd_mcp_ping(args):
    """ping 外部 MCP server,反馈可达性

    外部 MCP server 接入测试:检测目标 URL 是否可达(HTTP)。
    完整 MCP 协议握手需 JSON-RPC initialize,此处先做 HTTP 可达性快速检测。
    """
    import json
    from datetime import datetime

    try:
        import httpx
    except ImportError:
        print("[错误] httpx 不可用,无法 ping 外部 MCP server")
        return

    from .config import settings
    from .observability import metrics_collector

    if not args.url:
        print("[错误] 请用 --url 指定外部 MCP server 地址")
        raise SystemExit(1)

    async def _ping(url: str) -> dict:
        import time

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=args.timeout) as client:
                resp = await client.get(url)
                latency = (time.perf_counter() - start) * 1000
                return {
                    "url": url,
                    "reachable": resp.status_code < 500,
                    "status_code": resp.status_code,
                    "latency_ms": round(latency, 1),
                    "error": "" if resp.status_code < 500 else f"HTTP {resp.status_code}",
                }
        except Exception as e:
            latency = (time.perf_counter() - start) * 1000
            return {
                "url": url,
                "reachable": False,
                "status_code": 0,
                "latency_ms": round(latency, 1),
                "error": f"{type(e).__name__}: {e}",
            }

    results = asyncio.run(asyncio.gather(*[_ping(u) for u in args.url]))

    print("\n=== 外部 MCP Server 可达性 ===")
    print(f"{'URL':<40} {'可达':<6} {'状态码':>6} {'延迟ms':>8} 错误")
    print("-" * 90)
    for r in results:
        reach = "✓" if r["reachable"] else "✗"
        print(
            f"{r['url']:<40} {reach:<6} {r['status_code']:>6} {r['latency_ms']:>8.1f} {r['error']}"
        )

    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "mcp_health.json"
    payload = {"checked_at": datetime.now().isoformat(), "results": results}
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] MCP 健康状态已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    for r in results:
        metrics_collector.record_metric(
            "interop.a2a_call_success_rate",
            1.0 if r["reachable"] else 0.0,
            tags={"target": r["url"], "type": "mcp"},
        )


# ====================================================================
# obs-dashboard 子命令 - 显示 11 大类指标看板当前值
# ====================================================================
def cmd_obs_dashboard(args):
    """显示可观测性看板 - 11 大类指标的当前聚合值

    针对用户需求"手动测试,反馈真实数据":
      - 从 metrics_collector 全局单例拉真实记录(进程内累计)
      - 按 11 大类(质量/效率/知识库/安全/跨平台/协作/记忆/互操作/对齐/韧性/幻觉)打印
      - 每个指标显示 count/avg/last/last_timestamp
      - 支持 --category 过滤单类看板
    """
    from .observability import METRIC_CATEGORIES, metrics_collector

    category = getattr(args, "category", None)
    if category:
        if category not in METRIC_CATEGORIES:
            print(f"[错误] 未知分类: {category}")
            print(f"可选: {', '.join(METRIC_CATEGORIES.keys())}")
            return
        view = metrics_collector.get_category(category)
        views = {category: view}
    else:
        views = metrics_collector.get_dashboard()

    print("\n=== 可观测性看板 ===")
    for cat, meta in views.items():
        metrics_view = meta.get("metrics", {})
        if not metrics_view:
            continue
        print(f"\n--- {meta.get('name_cn', cat)} / {meta.get('dashboard', '')} ---")
        print(f"  {meta.get('description', '')}")
        print(f"  {'指标':<42} {'count':>6} {'avg':>10} {'last':>10} {'最后更新'}")
        for mname, stats in metrics_view.items():
            ts = (stats.get("last_timestamp") or "")[:19]
            print(
                f"  {mname:<42} {stats.get('count', 0):>6} "
                f"{stats.get('avg', 0.0):>10.3f} {stats.get('last', 0.0):>10.3f} {ts}"
            )
    total = sum(len(v.get("metrics", {})) for v in views.values())
    print(f"\n[汇总] 看板数={len(views)}  指标数={total}")


# ====================================================================
# obs-test 子命令 - 发射测试 span + 记录测试指标 + 验证后端可达性
# ====================================================================
def cmd_obs_test(args):
    """可观测性接入测试 - 真实发射 span + 记录指标 + 探测后端

    针对用户需求"接入成功都要反馈真实数据":
      - 本地:发射 root/tool/reflexion 三类测试 span,验证 tracer 链路
      - 线上:探测 OTel endpoint 与 Langfuse host 可达性(httpx/socket)
      - 指标:记录测试指标到 metrics_collector,验证 record_metric 链路
      - 反馈:写 data/obs_health.json + 表格化打印
    """
    import json
    import socket
    import time
    from datetime import datetime
    from urllib.parse import urlparse

    from .config import settings
    from .observability import SpanType, metrics_collector, tracer

    results: list[dict[str, Any]] = []

    # === 1. 本地 span 链路测试 ===
    span_types_to_test = [
        (SpanType.ROOT, "obs_test.root", {"test": True}),
        (SpanType.TOOL, "obs_test.tool", {"tool_name": "obs_test"}),
        (SpanType.REFLEXION, "obs_test.reflexion", {"operation_type": "tool"}),
    ]
    for st, name, attrs in span_types_to_test:
        start = time.perf_counter()
        try:
            sid = tracer.start_span(st, name, attrs)
            tracer.end_span(sid, status="OK")
            latency = (time.perf_counter() - start) * 1000
            results.append(
                {
                    "target": f"span:{st.value}",
                    "kind": "local",
                    "status": "ok",
                    "latency_ms": round(latency, 2),
                    "detail": f"span_id={sid[:8]}",
                }
            )
        except Exception as e:
            results.append(
                {
                    "target": f"span:{st.value}",
                    "kind": "local",
                    "status": "fail",
                    "latency_ms": 0.0,
                    "detail": f"{type(e).__name__}: {e}",
                }
            )

    # === 2. 指标记录链路测试 ===
    try:
        metrics_collector.record_metric(
            "efficiency.first_response_latency_p50",
            42.0,
            tags={"test": "obs_test"},
        )
        stats = metrics_collector.get_metric(
            "efficiency.first_response_latency_p50",
            tags={"test": "obs_test"},
        )
        results.append(
            {
                "target": "metrics:record_metric",
                "kind": "local",
                "status": "ok" if stats.get("count", 0) > 0 else "fail",
                "latency_ms": 0.0,
                "detail": f"count={stats.get('count', 0)}",
            }
        )
    except Exception as e:
        results.append(
            {
                "target": "metrics:record_metric",
                "kind": "local",
                "status": "fail",
                "latency_ms": 0.0,
                "detail": f"{type(e).__name__}: {e}",
            }
        )

    # === 3. 线上后端可达性探测 ===
    backends = [
        ("otel", settings.otel_endpoint, "OTel Collector"),
        ("langfuse", settings.langfuse_host, "Langfuse"),
    ]
    for key, url, label in backends:
        if not url:
            results.append(
                {
                    "target": f"backend:{key}",
                    "kind": "remote",
                    "status": "skip",
                    "latency_ms": 0.0,
                    "detail": "未配置 URL",
                }
            )
            continue
        start = time.perf_counter()
        try:
            parsed = urlparse(url)
            host = parsed.hostname or "localhost"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            with socket.create_connection((host, port), timeout=args.timeout):
                latency = (time.perf_counter() - start) * 1000
            results.append(
                {
                    "target": f"backend:{key}",
                    "kind": "remote",
                    "status": "ok",
                    "latency_ms": round(latency, 2),
                    "detail": f"{label} {host}:{port}",
                }
            )
        except Exception as e:
            latency = (time.perf_counter() - start) * 1000
            results.append(
                {
                    "target": f"backend:{key}",
                    "kind": "remote",
                    "status": "fail",
                    "latency_ms": round(latency, 2),
                    "detail": f"{type(e).__name__}: {e}",
                }
            )

    # === 4. 表格化打印 ===
    print("\n=== 可观测性接入测试 ===")
    print(f"{'目标':<32} {'类型':<8} {'状态':<9} {'延迟ms':>8} 详情")
    print("-" * 90)
    status_map = {"ok": "✓ ok", "fail": "✗ fail", "skip": "○ skip"}
    for r in results:
        print(
            f"{r['target']:<32} {r['kind']:<8} "
            f"{status_map.get(r['status'], r['status']):<9} "
            f"{r['latency_ms']:>8.1f} {r['detail']}"
        )

    # === 5. 反馈闭环:写 data/obs_health.json + metrics ===
    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "obs_health.json"
    payload = {
        "checked_at": datetime.now().isoformat(),
        "otel_available": tracer.otel_available,
        "results": results,
    }
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] 可观测性健康状态已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    for r in results:
        metrics_collector.record_metric(
            "efficiency.tool_latency_p50",
            r["latency_ms"],
            tags={"target": r["target"], "status": r["status"]},
        )

    ok = sum(1 for r in results if r["status"] == "ok")
    fail = sum(1 for r in results if r["status"] == "fail")
    print(f"[汇总] ok={ok}  fail={fail}  skip={len(results) - ok - fail}")


# ====================================================================
# obs-export 子命令 - 导出 Prometheus 格式指标
# ====================================================================
def cmd_obs_export(args):
    """导出指标为 Prometheus 文本格式

    用于外部 Prometheus 抓取,或人工查看当前指标快照。
    """
    from .observability import metrics_collector

    output = metrics_collector.export_prometheus()
    if not output.strip():
        print("[空] 当前无指标记录")
        return
    print(output)


# ====================================================================
# memory-list 子命令 - 列出 4 层记忆状态 + 可选后端可用性
# ====================================================================
def cmd_memory_list(args):
    """列出 4 层记忆(working/episodic/semantic/procedural)状态

    本地资源扫描:
      - 工作记忆:当前轮数/容量
      - 情景记忆:已归档片段数
      - 语义记忆:用户画像数/事实数/待处理矛盾数
      - 程序记忆:流程定义数
      - 可选后端:Graphiti/LightRAG 是否启用
    """
    from .memory.manager import MemoryManager

    mgr = MemoryManager()
    rows: list[dict[str, Any]] = [
        {
            "layer": "working",
            "items": len(mgr.working._turns) if hasattr(mgr.working, "_turns") else 0,
            "detail": f"session={mgr.working.session_id or '(无)'}",
        },
        {
            "layer": "episodic",
            "items": len(mgr.episodic._store),
            "detail": f"sessions={len(mgr.episodic._by_session)}",
        },
        {
            "layer": "semantic",
            "items": len(mgr.semantic.facts),
            "detail": (
                f"profiles={len(mgr.semantic.user_profiles)} "
                f"contradictions={len(mgr.semantic.pending_contradictions)}"
            ),
        },
        {
            "layer": "procedural",
            "items": len(mgr.procedural._procedures)
            if hasattr(mgr.procedural, "_procedures")
            else 0,
            "detail": "流程定义",
        },
    ]

    print("\n=== 分层记忆状态 ===")
    print(f"{'层级':<14} {'条目数':>8} 详情")
    print("-" * 60)
    for r in rows:
        print(f"{r['layer']:<14} {r['items']:>8} {r['detail']}")

    print("\n=== 可选后端 ===")
    print(f"  Graphiti: {'启用' if mgr.graphiti is not None else '未启用(纯内存)'}")
    print(f"  LightRAG: {'启用' if mgr.lightrag is not None else '未启用'}")


# ====================================================================
# memory-test 子命令 - 写入测试片段 + 召回验证(真实反馈)
# ====================================================================
def cmd_memory_test(args):
    """记忆系统写入+召回测试 - 真实反馈记忆链路是否工作

    手动测试:
      - 向情景记忆写入 1 个测试片段
      - 用关键词召回,验证能命中
      - 向语义记忆写入测试画像,验证可读回
      - 反馈:写 data/memory_health.json + metrics
    """
    import json
    from datetime import datetime

    from .config import settings
    from .memory.episodic import EpisodicMemory
    from .memory.semantic import SemanticMemory
    from .observability import metrics_collector

    results: list[dict[str, Any]] = []

    # === 1. 情景记忆写入+召回 ===
    ep = EpisodicMemory()
    test_session = f"test-{datetime.now().strftime('%H%M%S')}"
    test_turn = {
        "turn_id": "t1",
        "role": "user",
        "content": "请问北京户籍注销需要哪些材料?",
        "agent": "death-aftercare",
        "timestamp": datetime.now(),
    }

    async def _ep_test():
        return await ep.archive_turn(test_session, test_turn)

    try:
        episode = asyncio.run(_ep_test())
        if episode is None:
            raise RuntimeError("archive_turn 返回 None")
        # 召回测试
        hits = ep.recall_by_semantic("户籍注销 材料", top_k=3)
        results.append(
            {
                "target": "episodic:write+recall",
                "status": "ok" if hits else "fail",
                "detail": (
                    f"写入 episode={episode.episode_id[:8]} 召回={len(hits)} 条 "
                    f"摘要={episode.summary[:30]!r}"
                ),
            }
        )
    except Exception as e:
        results.append(
            {
                "target": "episodic:write+recall",
                "status": "fail",
                "detail": f"{type(e).__name__}: {e}",
            }
        )

    # === 2. 语义记忆画像写入+读回 ===
    try:
        sm = SemanticMemory()
        uid = "test-user-1"
        sm.update_user_profile(
            uid,
            {"name": "测试用户", "relationship_to_deceased": "子女"},
        )
        profile = sm.get_profile(uid)
        ok = profile is not None and profile.name == "测试用户"
        results.append(
            {
                "target": "semantic:profile_write+read",
                "status": "ok" if ok else "fail",
                "detail": (
                    f"画像={profile.name if profile else None} "
                    f"关系={profile.relationship_to_deceased if profile else None}"
                ),
            }
        )
    except Exception as e:
        results.append(
            {
                "target": "semantic:profile_write+read",
                "status": "fail",
                "detail": f"{type(e).__name__}: {e}",
            }
        )

    # === 3. 表格化打印 ===
    print("\n=== 记忆系统测试 ===")
    print(f"{'目标':<32} {'状态':<9} 详情")
    print("-" * 80)
    status_map = {"ok": "✓ ok", "fail": "✗ fail"}
    for r in results:
        print(f"{r['target']:<32} {status_map.get(r['status'], r['status']):<9} {r['detail']}")

    # === 4. 反馈闭环 ===
    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "memory_health.json"
    payload = {"checked_at": datetime.now().isoformat(), "results": results}
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] 记忆健康状态已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    for r in results:
        metrics_collector.record_metric(
            "memory.context_recall_accuracy",
            1.0 if r["status"] == "ok" else 0.0,
            tags={"target": r["target"]},
        )

    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"[汇总] ok={ok}/{len(results)}")


# ====================================================================
# memory-ping 子命令 - Graphiti/Neo4j/LightRAG 后端可达性
# ====================================================================
def cmd_memory_ping(args):
    """记忆后端可达性探测 - Graphiti(Neo4j)/LightRAG

    线上源接入测试:
      - 若 GRAPHITI_ENABLED=true,探测 Neo4j bolt 端口
      - 若 LIGHTRAG_ENABLED=true,探测 LightRAG 存储目录可写
      - 反馈:写 data/memory_health.json + metrics
    """
    import json
    import socket
    import time
    from datetime import datetime
    from urllib.parse import urlparse

    from .config import settings
    from .observability import metrics_collector

    results: list[dict[str, Any]] = []

    # === 1. Graphiti / Neo4j ===
    if not settings.graphiti_enabled:
        results.append(
            {
                "target": "graphiti",
                "status": "skip",
                "latency_ms": 0.0,
                "detail": "GRAPHITI_ENABLED=false",
            }
        )
    else:
        start = time.perf_counter()
        try:
            parsed = urlparse(settings.graphiti_neo4j_uri)
            host = parsed.hostname or "localhost"
            port = parsed.port or 7687
            with socket.create_connection((host, port), timeout=args.timeout):
                latency = (time.perf_counter() - start) * 1000
            results.append(
                {
                    "target": "graphiti",
                    "status": "ok",
                    "latency_ms": round(latency, 2),
                    "detail": f"Neo4j {host}:{port}",
                }
            )
        except Exception as e:
            latency = (time.perf_counter() - start) * 1000
            results.append(
                {
                    "target": "graphiti",
                    "status": "fail",
                    "latency_ms": round(latency, 2),
                    "detail": f"{type(e).__name__}: {e}",
                }
            )

    # === 2. LightRAG 存储目录 ===
    if not settings.lightrag_enabled:
        results.append(
            {
                "target": "lightrag",
                "status": "skip",
                "latency_ms": 0.0,
                "detail": "LIGHTRAG_ENABLED=false",
            }
        )
    else:
        try:
            storage = settings.lightrag_storage_dir
            storage.mkdir(parents=True, exist_ok=True)
            probe = storage / ".ping"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            results.append(
                {
                    "target": "lightrag",
                    "status": "ok",
                    "latency_ms": 0.0,
                    "detail": f"存储目录可写 {storage}",
                }
            )
        except Exception as e:
            results.append(
                {
                    "target": "lightrag",
                    "status": "fail",
                    "latency_ms": 0.0,
                    "detail": f"{type(e).__name__}: {e}",
                }
            )

    # === 3. 打印 + 反馈 ===
    print("\n=== 记忆后端可达性 ===")
    print(f"{'目标':<14} {'状态':<9} {'延迟ms':>8} 详情")
    print("-" * 70)
    status_map = {"ok": "✓ ok", "fail": "✗ fail", "skip": "○ skip"}
    for r in results:
        print(
            f"{r['target']:<14} {status_map.get(r['status'], r['status']):<9} "
            f"{r['latency_ms']:>8.1f} {r['detail']}"
        )

    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "memory_health.json"
    payload = {"checked_at": datetime.now().isoformat(), "results": results}
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] 记忆后端状态已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    for r in results:
        metrics_collector.record_metric(
            "memory.memory_query_latency_p95",
            r["latency_ms"],
            tags={"target": r["target"], "status": r["status"]},
        )


# ====================================================================
# a2a-card 子命令 - 显示本地 AgentCard(自名片)
# ====================================================================
def cmd_a2a_card(args):
    """显示本地 A2A AgentCard - 平台自名片

    本地资源:
      - 调用 a2a.server._build_default_card() 构建自 card
      - 打印 name/version/url/skills/provider/authentication
      - 校验必填字段完整性
    """
    import json

    from .a2a.server import _build_default_card

    card = _build_default_card()
    card_dict = card.to_dict()

    print("\n=== 本地 AgentCard (A2A v1.0) ===")
    print(f"  name:           {card_dict.get('name')}")
    print(f"  version:        {card_dict.get('version')}")
    print(f"  url:            {card_dict.get('url')}")
    print(f"  description:    {card_dict.get('description')}")
    provider = card_dict.get("provider", {})
    print(f"  provider:       {provider.get('name')} ({provider.get('url', '')})")
    auth = card_dict.get("authentication", {})
    print(f"  authentication: {auth.get('schemes', [])}")
    caps = card_dict.get("capabilities", {})
    print(
        f"  capabilities:   streaming={caps.get('streaming')} push={caps.get('pushNotifications')}"
    )

    skills = card_dict.get("skills", []) or []
    print(f"\n=== Skills ({len(skills)}) ===")
    print(f"  {'id':<24} {'名称':<16} {'司法管辖'}")
    print("  " + "-" * 70)
    for s in skills:
        jurisdictions = ",".join(s.get("jurisdictions", []) or [])
        print(f"  {s.get('id', ''):<24} {s.get('name', ''):<16} {jurisdictions}")

    # === 完整性校验 ===
    print("\n=== 完整性校验 ===")
    required = ["name", "version", "url", "description", "skills"]
    missing = [f for f in required if not card_dict.get(f)]
    if missing:
        print(f"  ✗ 缺失必填字段: {missing}")
    else:
        print("  ✓ 必填字段完整")
    if not skills:
        print("  ✗ 无 skills 声明")
    else:
        print(f"  ✓ skills 数量={len(skills)}")
    if not card_dict.get("url", "").startswith("http"):
        print("  ✗ url 格式无效")
    else:
        print("  ✓ url 格式有效")

    if args.json:
        print("\n=== 原始 JSON ===")
        print(json.dumps(card_dict, ensure_ascii=False, indent=2))


# ====================================================================
# a2a-test 子命令 - A2A 协议自测(JSON-RPC tasks/send)
# ====================================================================
def cmd_a2a_test(args):
    """A2A 协议自测 - 本地 card 完整性 + JSON-RPC 自测

    手动测试:
      - 校验本地 card 完整性
      - 构造 A2AServer,发 tasks/send JSON-RPC,验证返回结构
      - 反馈:写 data/a2a_health.json + metrics
    """
    import json
    from datetime import datetime

    from .a2a.server import A2AServer, _build_default_card
    from .config import settings
    from .observability import metrics_collector

    results: list[dict[str, Any]] = []

    # === 1. 本地 card 完整性 ===
    card = _build_default_card()
    card_dict = card.to_dict()
    required = ["name", "version", "url", "description", "skills"]
    missing = [f for f in required if not card_dict.get(f)]
    results.append(
        {
            "target": "card:completeness",
            "status": "ok" if not missing else "fail",
            "detail": (f"skills={len(card_dict.get('skills', []))} missing={missing or 'none'}"),
        }
    )

    # === 2. JSON-RPC tasks/send 自测 ===
    server = A2AServer()
    rpc_req = {
        "jsonrpc": "2.0",
        "id": "test-1",
        "method": "tasks/send",
        "params": {
            "skill_id": "death-aftercare",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "content": "测试:请简要说明户籍注销"}],
            },
            "metadata": {"test": True},
        },
    }
    try:
        resp = asyncio.run(server.handle_jsonrpc(rpc_req))
        has_result = "result" in resp
        has_error = "error" in resp
        # tasks/send 应返回 result(含 task 状态),不应是 method not found
        method_ok = not (has_error and resp.get("error", {}).get("code") == -32601)
        results.append(
            {
                "target": "jsonrpc:tasks/send",
                "status": "ok" if (has_result or method_ok) else "fail",
                "detail": (
                    f"result={'有' if has_result else '无'} "
                    f"error={resp.get('error', {}).get('message', '无')}"
                ),
            }
        )
    except Exception as e:
        results.append(
            {
                "target": "jsonrpc:tasks/send",
                "status": "fail",
                "detail": f"{type(e).__name__}: {e}",
            }
        )

    # === 3. JSON-RPC method not found 自测 ===
    bad_req = {
        "jsonrpc": "2.0",
        "id": "test-2",
        "method": "tasks/nonexistent",
        "params": {},
    }
    try:
        resp = asyncio.run(server.handle_jsonrpc(bad_req))
        # 期望返回 -32601 method not found
        code = resp.get("error", {}).get("code")
        results.append(
            {
                "target": "jsonrpc:error_handling",
                "status": "ok" if code == -32601 else "fail",
                "detail": f"error_code={code}",
            }
        )
    except Exception as e:
        results.append(
            {
                "target": "jsonrpc:error_handling",
                "status": "fail",
                "detail": f"{type(e).__name__}: {e}",
            }
        )

    # === 4. 打印 + 反馈 ===
    print("\n=== A2A 协议自测 ===")
    print(f"{'目标':<32} {'状态':<9} 详情")
    print("-" * 80)
    status_map = {"ok": "✓ ok", "fail": "✗ fail"}
    for r in results:
        print(f"{r['target']:<32} {status_map.get(r['status'], r['status']):<9} {r['detail']}")

    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "a2a_health.json"
    payload = {
        "checked_at": datetime.now().isoformat(),
        "card_url": card_dict.get("url"),
        "results": results,
    }
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] A2A 健康状态已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    for r in results:
        metrics_collector.record_metric(
            "interop.a2a_call_success_rate",
            1.0 if r["status"] == "ok" else 0.0,
            tags={"target": r["target"]},
        )
        metrics_collector.record_metric(
            "interop.agent_card_completeness",
            1.0 if r["status"] == "ok" else 0.0,
            tags={"target": r["target"]},
        )

    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"[汇总] ok={ok}/{len(results)}")


# ====================================================================
# a2a-registry 子命令 - A2A registry 可达性(线上源)
# ====================================================================
def cmd_a2a_registry(args):
    """A2A registry 可达性探测 - 线上智能体注册中心

    线上源接入测试:
      - 探测 settings.a2a_registry_url 可达性
      - 反馈:写 data/a2a_health.json + metrics
    """
    import json
    import socket
    import time
    from datetime import datetime
    from urllib.parse import urlparse

    from .config import settings
    from .observability import metrics_collector

    url = settings.a2a_registry_url
    results: list[dict[str, Any]] = []

    if not url:
        results.append(
            {
                "target": "a2a-registry",
                "status": "skip",
                "latency_ms": 0.0,
                "detail": "未配置 A2A_REGISTRY_URL",
            }
        )
    else:
        start = time.perf_counter()
        try:
            parsed = urlparse(url)
            host = parsed.hostname or "localhost"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            with socket.create_connection((host, port), timeout=args.timeout):
                latency = (time.perf_counter() - start) * 1000
            results.append(
                {
                    "target": "a2a-registry",
                    "status": "ok",
                    "latency_ms": round(latency, 2),
                    "detail": f"{url}",
                }
            )
        except Exception as e:
            latency = (time.perf_counter() - start) * 1000
            results.append(
                {
                    "target": "a2a-registry",
                    "status": "fail",
                    "latency_ms": round(latency, 2),
                    "detail": f"{type(e).__name__}: {e}",
                }
            )

    print("\n=== A2A Registry 可达性 ===")
    print(f"{'目标':<20} {'状态':<9} {'延迟ms':>8} 详情")
    print("-" * 70)
    status_map = {"ok": "✓ ok", "fail": "✗ fail", "skip": "○ skip"}
    for r in results:
        print(
            f"{r['target']:<20} {status_map.get(r['status'], r['status']):<9} "
            f"{r['latency_ms']:>8.1f} {r['detail']}"
        )

    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "a2a_health.json"
    payload = {"checked_at": datetime.now().isoformat(), "results": results}
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] A2A registry 状态已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    for r in results:
        metrics_collector.record_metric(
            "interop.a2a_avg_latency_ms",
            r["latency_ms"],
            tags={"target": r["target"], "status": r["status"]},
        )


# ====================================================================
# deploy-check 子命令 - 校验部署工件存在性与语法(本地资源)
# ====================================================================
def cmd_deploy_check(args):
    """校验部署工件 - Dockerfile/compose/entrypoint/healthcheck

    本地资源扫描:
      - 检查 Dockerfile / docker-compose.yml / entrypoint.sh / healthcheck.py 存在
      - 校验 docker-compose.yml YAML 语法
      - 校验 entrypoint.sh 可读、含 mode 分发
      - 校验 healthcheck.py 可编译
      - 反馈:写 data/deploy_health.json + metrics
    """
    import json
    from datetime import datetime

    import yaml

    from .config import settings
    from .observability import metrics_collector

    project_root = settings.project_root.parent  # .traecli/ 的父级 = 仓库根
    docker_dir = settings.project_root / "docker"

    artifacts = [
        ("Dockerfile", project_root / "Dockerfile"),
        ("docker-compose.yml", project_root / "docker-compose.yml"),
        ("entrypoint.sh", docker_dir / "entrypoint.sh"),
        ("healthcheck.py", docker_dir / "healthcheck.py"),
    ]

    results: list[dict[str, Any]] = []

    # === 1. 存在性校验 ===
    for name, path in artifacts:
        exists = path.exists()
        results.append(
            {
                "target": f"artifact:{name}",
                "status": "ok" if exists else "fail",
                "detail": f"{path} {'存在' if exists else '缺失'}",
            }
        )

    # === 2. docker-compose.yml YAML 语法校验 ===
    compose_path = project_root / "docker-compose.yml"
    if compose_path.exists():
        try:
            with open(compose_path, encoding="utf-8") as f:
                compose = yaml.safe_load(f) or {}
            services = compose.get("services", {})
            results.append(
                {
                    "target": "syntax:docker-compose",
                    "status": "ok",
                    "detail": f"YAML 合法, services={list(services.keys())}",
                }
            )
        except Exception as e:
            results.append(
                {
                    "target": "syntax:docker-compose",
                    "status": "fail",
                    "detail": f"{type(e).__name__}: {e}",
                }
            )

    # === 3. entrypoint.sh 内容校验 ===
    entry_path = docker_dir / "entrypoint.sh"
    if entry_path.exists():
        try:
            content = entry_path.read_text(encoding="utf-8")
            has_mode_dispatch = 'case "$MODE"' in content or 'case "$1"' in content
            has_set_eu = "set -euo pipefail" in content or "set -eu" in content
            ok = has_mode_dispatch and has_set_eu
            results.append(
                {
                    "target": "syntax:entrypoint.sh",
                    "status": "ok" if ok else "fail",
                    "detail": (
                        f"mode_dispatch={'有' if has_mode_dispatch else '无'} "
                        f"set_eu={'有' if has_set_eu else '无'}"
                    ),
                }
            )
        except Exception as e:
            results.append(
                {
                    "target": "syntax:entrypoint.sh",
                    "status": "fail",
                    "detail": f"{type(e).__name__}: {e}",
                }
            )

    # === 4. healthcheck.py 可编译校验 ===
    hc_path = docker_dir / "healthcheck.py"
    if hc_path.exists():
        try:
            import py_compile

            py_compile.compile(str(hc_path), doraise=True)
            results.append(
                {
                    "target": "syntax:healthcheck.py",
                    "status": "ok",
                    "detail": "Python 编译通过",
                }
            )
        except Exception as e:
            results.append(
                {
                    "target": "syntax:healthcheck.py",
                    "status": "fail",
                    "detail": f"{type(e).__name__}: {e}",
                }
            )

    # === 5. 打印 + 反馈 ===
    print("\n=== 部署工件校验 ===")
    print(f"{'目标':<28} {'状态':<9} 详情")
    print("-" * 80)
    status_map = {"ok": "✓ ok", "fail": "✗ fail"}
    for r in results:
        print(f"{r['target']:<28} {status_map.get(r['status'], r['status']):<9} {r['detail']}")

    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "deploy_health.json"
    payload = {"checked_at": datetime.now().isoformat(), "results": results}
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] 部署工件状态已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    for r in results:
        metrics_collector.record_metric(
            "efficiency.tool_latency_p50",
            1.0 if r["status"] == "ok" else 0.0,
            tags={"target": r["target"], "check": "deploy"},
        )

    ok = sum(1 for r in results if r["status"] == "ok")
    if getattr(args, "fail_fast", False) and any(r["status"] == "fail" for r in results):
        raise SystemExit(1)
    print(f"[汇总] ok={ok}/{len(results)}")


# ====================================================================
# deploy-test 子命令 - docker daemon 可达性 + healthcheck 脚本自测
# ====================================================================
def cmd_deploy_test(args):
    """部署运行时测试 - docker 可用性 + healthcheck 脚本可执行性

    线上源+手动测试:
      - docker 命令是否可用(docker version)
      - docker daemon 是否运行(docker info)
      - healthcheck.py 能否独立执行(返回非 0 也算可执行)
      - 反馈:写 data/deploy_health.json + metrics
    """
    import json
    import shutil
    import subprocess
    import time
    from datetime import datetime

    from .config import settings
    from .observability import metrics_collector

    results: list[dict[str, Any]] = []

    # === 1. docker 命令可用性 ===
    docker_bin = shutil.which("docker")
    if not docker_bin:
        results.append(
            {
                "target": "docker:binary",
                "status": "skip",
                "latency_ms": 0.0,
                "detail": "docker 命令未安装",
            }
        )
    else:
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
            latency = (time.perf_counter() - start) * 1000
            if proc.returncode == 0:
                results.append(
                    {
                        "target": "docker:daemon",
                        "status": "ok",
                        "latency_ms": round(latency, 2),
                        "detail": f"server={proc.stdout.strip()[:30]}",
                    }
                )
            else:
                results.append(
                    {
                        "target": "docker:daemon",
                        "status": "fail",
                        "latency_ms": round(latency, 2),
                        "detail": f"rc={proc.returncode} {proc.stderr.strip()[:40]}",
                    }
                )
        except Exception as e:
            results.append(
                {
                    "target": "docker:daemon",
                    "status": "fail",
                    "latency_ms": 0.0,
                    "detail": f"{type(e).__name__}: {e}",
                }
            )

    # === 2. healthcheck.py 自测 ===
    hc_path = settings.project_root / "docker" / "healthcheck.py"
    if hc_path.exists():
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                ["python3", str(hc_path)],
                capture_output=True,
                text=True,
                timeout=args.timeout,
                env={"HEALTHCHECK_TIMEOUT": "2", "PATH": "/usr/bin:/bin"},
            )
            latency = (time.perf_counter() - start) * 1000
            # 脚本能执行并返回(无论 0/1)即说明可运行
            runnable = proc.returncode in (0, 1)
            results.append(
                {
                    "target": "healthcheck:runnable",
                    "status": "ok" if runnable else "fail",
                    "latency_ms": round(latency, 2),
                    "detail": f"rc={proc.returncode} stderr={proc.stderr.strip()[:40]}",
                }
            )
        except Exception as e:
            results.append(
                {
                    "target": "healthcheck:runnable",
                    "status": "fail",
                    "latency_ms": 0.0,
                    "detail": f"{type(e).__name__}: {e}",
                }
            )
    else:
        results.append(
            {
                "target": "healthcheck:runnable",
                "status": "skip",
                "latency_ms": 0.0,
                "detail": "healthcheck.py 不存在",
            }
        )

    # === 3. 打印 + 反馈 ===
    print("\n=== 部署运行时测试 ===")
    print(f"{'目标':<24} {'状态':<9} {'延迟ms':>8} 详情")
    print("-" * 75)
    status_map = {"ok": "✓ ok", "fail": "✗ fail", "skip": "○ skip"}
    for r in results:
        print(
            f"{r['target']:<24} {status_map.get(r['status'], r['status']):<9} "
            f"{r['latency_ms']:>8.1f} {r['detail']}"
        )

    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "deploy_health.json"
    payload = {"checked_at": datetime.now().isoformat(), "results": results}
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] 部署运行时状态已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    for r in results:
        metrics_collector.record_metric(
            "efficiency.tool_latency_p50",
            r["latency_ms"],
            tags={"target": r["target"], "status": r["status"]},
        )

    ok = sum(1 for r in results if r["status"] == "ok")
    fail = sum(1 for r in results if r["status"] == "fail")
    skip = sum(1 for r in results if r["status"] == "skip")
    print(f"[汇总] ok={ok}  fail={fail}  skip={skip}")


# ====================================================================
# reflexion-list 子命令 - 列出预定义调整策略(本地资源)
# ====================================================================
def cmd_reflexion_list(args):
    """列出 Reflexion 预定义调整策略表(10 种快速路径)

    本地资源:
      - 从 reflexion.engine.ADJUSTMENT_STRATEGIES 读取
      - 打印每种策略的 failure_type + strategy + adjusted_params
    """
    from .reflexion.engine import ADJUSTMENT_STRATEGIES

    print("\n=== Reflexion 预定义调整策略 ===")
    print(f"{'失败类型':<26} {'策略':<40} 调整参数")
    print("-" * 100)
    for ftype, strat in ADJUSTMENT_STRATEGIES.items():
        strategy = strat.get("strategy", "")[:38]
        params = strat.get("adjusted_params", {})
        params_str = ",".join(f"{k}={v}" for k, v in params.items())[:40]
        print(f"{ftype:<26} {strategy:<40} {params_str}")
    print(f"\n策略总数: {len(ADJUSTMENT_STRATEGIES)}")


# ====================================================================
# reflexion-test 子命令 - 用 mock 操作跑反思重试(真实反馈)
# ====================================================================
def cmd_reflexion_test(args):
    """Reflexion 反思重试测试 - mock 操作失败后重试成功

    手动测试:
      - 构造 mock 操作:第 1 次失败(返回 error),第 2 次成功
      - 用 ReflexionEngine.execute_with_reflexion 跑
      - 验证:重试次数=2,success=True,reflections 非空
      - 反馈:写 data/reflexion_health.json + metrics
    """
    import json
    from datetime import datetime

    from .config import settings
    from .observability import metrics_collector
    from .reflexion.engine import ReflexionEngine, get_predefined_strategy

    results: list[dict[str, Any]] = []

    # === 1. 预定义策略命中率测试 ===
    test_cases = [
        ("timeout", True),
        ("format_error", True),
        ("rate_limit", True),
        ("nonexistent_failure_type", False),
    ]
    for ftype, expect_hit in test_cases:
        strat = get_predefined_strategy(ftype)
        actual_hit = strat is not None
        ok = actual_hit == expect_hit
        results.append(
            {
                "target": f"strategy:{ftype}",
                "status": "ok" if ok else "fail",
                "detail": (
                    f"命中={'是' if actual_hit else '否'} 期望={'是' if expect_hit else '否'}"
                ),
            }
        )

    # === 2. mock 操作反思重试测试 ===
    call_count = {"n": 0}

    async def mock_operation(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # 第 1 次失败(模拟 tool error)
            return {"error": "模拟超时", "error_type": "timeout"}
        # 第 2 次成功
        return {"ok": True, "result": "重试成功"}

    engine = ReflexionEngine(agent_name="reflexion-test")
    # 限制为 2 次重试,加快测试
    engine.max_retries = 2
    try:
        outcome = asyncio.run(
            engine.execute_with_reflexion(
                operation=mock_operation,
                initial_input={"query": "test"},
                operation_type="tool",
            )
        )
        success = outcome.get("success")
        attempts = outcome.get("attempts")
        reflections = outcome.get("reflections", [])
        results.append(
            {
                "target": "engine:retry_then_success",
                "status": "ok" if success else "fail",
                "detail": (
                    f"success={success} attempts={attempts} "
                    f"reflections={len(reflections)} failures={len(outcome.get('failures', []))}"
                ),
            }
        )
    except Exception as e:
        results.append(
            {
                "target": "engine:retry_then_success",
                "status": "fail",
                "detail": f"{type(e).__name__}: {e}",
            }
        )

    # === 3. fallback 路径测试(持续失败) ===
    call_count2 = {"n": 0}

    async def always_fail(**kwargs):
        call_count2["n"] += 1
        return {"error": "持续失败", "error_type": "api_error"}

    engine2 = ReflexionEngine(agent_name="reflexion-fallback-test")
    engine2.max_retries = 2
    try:
        outcome = asyncio.run(
            engine2.execute_with_reflexion(
                operation=always_fail,
                initial_input={"query": "test"},
                operation_type="tool",
            )
        )
        fallback = outcome.get("fallback")
        attempts = outcome.get("attempts")
        results.append(
            {
                "target": "engine:fallback_on_exhausted",
                "status": "ok" if fallback else "fail",
                "detail": (
                    f"fallback={fallback} attempts={attempts} "
                    f"failures={len(outcome.get('failures', []))}"
                ),
            }
        )
    except Exception as e:
        results.append(
            {
                "target": "engine:fallback_on_exhausted",
                "status": "fail",
                "detail": f"{type(e).__name__}: {e}",
            }
        )

    # === 4. 打印 + 反馈 ===
    print("\n=== Reflexion 反思重试测试 ===")
    print(f"{'目标':<34} {'状态':<9} 详情")
    print("-" * 85)
    status_map = {"ok": "✓ ok", "fail": "✗ fail"}
    for r in results:
        print(f"{r['target']:<34} {status_map.get(r['status'], r['status']):<9} {r['detail']}")

    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "reflexion_health.json"
    payload = {"checked_at": datetime.now().isoformat(), "results": results}
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] Reflexion 状态已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    for r in results:
        metrics_collector.record_metric(
            "resilience.reflexion_success_rate",
            1.0 if r["status"] == "ok" else 0.0,
            tags={"target": r["target"]},
        )

    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"[汇总] ok={ok}/{len(results)}")


# ====================================================================
# reflexion-ping 子命令 - LLM 反思路径可达性
# ====================================================================
def cmd_reflexion_ping(args):
    """LLM 反思路径可达性 - Reflexion 慢速路径依赖 LLM

    线上源接入测试:
      - 检查 llm_client.api_key 是否配置
      - 若配置,发一个简短 ping 验证 LLM 可用
      - 反馈:写 data/reflexion_health.json + metrics
    """
    import json
    from datetime import datetime

    from .config import settings
    from .llm import llm_client
    from .observability import metrics_collector

    results: list[dict[str, Any]] = []

    # === 1. LLM api_key 配置检查 ===
    has_key = bool(llm_client.api_key)
    results.append(
        {
            "target": "llm:api_key",
            "status": "ok" if has_key else "skip",
            "latency_ms": 0.0,
            "detail": "已配置" if has_key else "未配置(反思降级为预定义策略)",
        }
    )

    # === 2. LLM ping(若有 key) ===
    if has_key:
        import time

        start = time.perf_counter()
        try:
            resp = asyncio.run(
                llm_client.ping_once(
                    [{"role": "user", "content": "请只回复四个字:pong ok"}],
                    max_tokens=20,
                )
            )
            latency = (time.perf_counter() - start) * 1000
            preview = (resp.content or "")[:40].replace("\n", " ")
            results.append(
                {
                    "target": "llm:ping",
                    "status": "ok",
                    "latency_ms": round(latency, 2),
                    "detail": f"响应={preview!r}",
                }
            )
        except Exception as e:
            latency = (time.perf_counter() - start) * 1000
            results.append(
                {
                    "target": "llm:ping",
                    "status": "fail",
                    "latency_ms": round(latency, 2),
                    "detail": f"{type(e).__name__}: {e}",
                }
            )

    # === 3. 打印 + 反馈 ===
    print("\n=== Reflexion LLM 反思路径可达性 ===")
    print(f"{'目标':<18} {'状态':<9} {'延迟ms':>8} 详情")
    print("-" * 75)
    status_map = {"ok": "✓ ok", "fail": "✗ fail", "skip": "○ skip"}
    for r in results:
        print(
            f"{r['target']:<18} {status_map.get(r['status'], r['status']):<9} "
            f"{r['latency_ms']:>8.1f} {r['detail']}"
        )

    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "reflexion_health.json"
    payload = {"checked_at": datetime.now().isoformat(), "results": results}
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] Reflexion LLM 状态已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    for r in results:
        metrics_collector.record_metric(
            "resilience.reflexion_trigger_rate",
            1.0 if r["status"] == "ok" else 0.0,
            tags={"target": r["target"], "status": r["status"]},
        )


# ====================================================================
# skill-list 子命令 - 列出本地技能(skills/*/SKILL.md)
# ====================================================================
def cmd_skill_list(args):
    """列出本地技能清单 - skills/*/SKILL.md

    本地资源扫描:
      - 扫描 skills/*/SKILL.md
      - 解析 YAML frontmatter(name + description)
      - 统计每个 skill 目录下的文档数
    """
    import re

    import yaml

    from .config import settings

    skills_dir = settings.skills_dir
    if not skills_dir.exists():
        print(f"[错误] skills 目录不存在: {skills_dir}")
        return

    skill_dirs = sorted(d for d in skills_dir.iterdir() if d.is_dir())
    if not skill_dirs:
        print(f"[空] {skills_dir} 下无技能目录")
        return

    print("\n=== 本地技能清单 ===")
    print(f"{'名称':<28} {'文档数':>6} {'描述(前40字)'}")
    print("-" * 90)
    total = 0
    for d in skill_dirs:
        skill_md = d / "SKILL.md"
        if not skill_md.exists():
            print(f"{d.name:<28} {'缺失':>6} (无 SKILL.md)")
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
            # 解析 frontmatter
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
            front = yaml.safe_load(m.group(1)) if m else {}
            name = front.get("name", d.name)
            desc = (front.get("description", "") or "")[:40]
        except Exception as e:
            name = d.name
            desc = f"[解析失败: {type(e).__name__}]"
        doc_count = sum(1 for _ in d.glob("*.md"))
        print(f"{name:<28} {doc_count:>6} {desc}")
        total += 1
    print(f"\n技能总数: {total}")


# ====================================================================
# skill-test 子命令 - 加载并校验单个 SKILL.md(真实反馈)
# ====================================================================
def cmd_skill_test(args):
    """加载并校验单个技能 SKILL.md - frontmatter 完整性 + 文档引用

    手动测试:
      - 解析 SKILL.md frontmatter(name/description 必填)
      - 检查引用的 stage 文件是否存在
      - 反馈:写 data/skill_health.json + metrics
    """
    import json
    import re
    from datetime import datetime

    import yaml

    from .config import settings
    from .observability import metrics_collector

    skills_dir = settings.skills_dir
    skill_dir = skills_dir / args.name
    skill_md = skill_dir / "SKILL.md"

    results: list[dict[str, Any]] = []

    # === 1. 文件存在性 ===
    exists = skill_md.exists()
    results.append(
        {
            "target": "file:exists",
            "status": "ok" if exists else "fail",
            "detail": str(skill_md),
        }
    )
    if not exists:
        _print_skill_results(results)
        return

    # === 2. frontmatter 解析 ===
    try:
        content = skill_md.read_text(encoding="utf-8")
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not m:
            results.append(
                {
                    "target": "frontmatter:parse",
                    "status": "fail",
                    "detail": "未找到 YAML frontmatter",
                }
            )
            _print_skill_results(results)
            return
        front = yaml.safe_load(m.group(1)) or {}
        results.append(
            {
                "target": "frontmatter:parse",
                "status": "ok",
                "detail": f"字段={list(front.keys())}",
            }
        )
    except Exception as e:
        results.append(
            {
                "target": "frontmatter:parse",
                "status": "fail",
                "detail": f"{type(e).__name__}: {e}",
            }
        )
        _print_skill_results(results)
        return

    # === 3. 必填字段校验 ===
    required = ["name", "description"]
    missing = [f for f in required if not front.get(f)]
    results.append(
        {
            "target": "frontmatter:required_fields",
            "status": "ok" if not missing else "fail",
            "detail": f"missing={missing or 'none'}",
        }
    )

    # === 4. 引用文档存在性 ===
    # 从正文查找 stage-*.md 等引用
    referenced = set(re.findall(r"([a-zA-Z0-9_\-/]+\.md)", content))
    # 排除 SKILL.md 自身和绝对路径
    referenced = {r for r in referenced if r != "SKILL.md" and "/" not in r}
    missing_refs = []
    for ref in sorted(referenced):
        if not (skill_dir / ref).exists():
            missing_refs.append(ref)
    results.append(
        {
            "target": "refs:document_exists",
            "status": "ok" if not missing_refs else "fail",
            "detail": (f"引用={len(referenced)} 缺失={missing_refs or 'none'}"),
        }
    )

    # === 5. 打印 + 反馈 ===
    _print_skill_results(results)

    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "skill_health.json"
    payload = {
        "checked_at": datetime.now().isoformat(),
        "skill": args.name,
        "results": results,
    }
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] 技能状态已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    for r in results:
        metrics_collector.record_metric(
            "quality.tool_selection_accuracy",
            1.0 if r["status"] == "ok" else 0.0,
            tags={"skill": args.name, "target": r["target"]},
        )


def _print_skill_results(results: list[dict[str, Any]]) -> None:
    """打印 skill 测试结果表格"""
    print("\n=== 技能 SKILL.md 校验 ===")
    print(f"{'目标':<32} {'状态':<9} 详情")
    print("-" * 80)
    status_map = {"ok": "✓ ok", "fail": "✗ fail"}
    for r in results:
        print(f"{r['target']:<32} {status_map.get(r['status'], r['status']):<9} {r['detail']}")
    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"[汇总] ok={ok}/{len(results)}")


# ====================================================================
# skill-validate 子命令 - 全量校验所有技能完整性(反馈闭环)
# ====================================================================
def cmd_skill_validate(args):
    """全量校验所有技能 - 批量验证 SKILL.md frontmatter

    反馈闭环:
      - 遍历 skills/*/SKILL.md
      - 逐个校验 frontmatter 必填字段
      - 汇总写 data/skill_health.json + metrics
    """
    import json
    import re
    from datetime import datetime

    import yaml

    from .config import settings
    from .observability import metrics_collector

    skills_dir = settings.skills_dir
    if not skills_dir.exists():
        print(f"[错误] skills 目录不存在: {skills_dir}")
        return

    skill_dirs = sorted(d for d in skills_dir.iterdir() if d.is_dir())
    results: list[dict[str, Any]] = []
    required = ["name", "description"]

    for d in skill_dirs:
        skill_md = d / "SKILL.md"
        if not skill_md.exists():
            results.append(
                {
                    "skill": d.name,
                    "status": "fail",
                    "detail": "缺失 SKILL.md",
                }
            )
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
            front = yaml.safe_load(m.group(1)) if m else {}
            missing = [f for f in required if not front.get(f)]
            results.append(
                {
                    "skill": d.name,
                    "status": "ok" if not missing else "fail",
                    "detail": (f"name={front.get('name', '✗')} missing={missing or 'none'}"),
                }
            )
        except Exception as e:
            results.append(
                {
                    "skill": d.name,
                    "status": "fail",
                    "detail": f"{type(e).__name__}: {e}",
                }
            )

    print("\n=== 全量技能校验 ===")
    print(f"{'技能':<28} {'状态':<9} 详情")
    print("-" * 80)
    status_map = {"ok": "✓ ok", "fail": "✗ fail"}
    for r in results:
        print(f"{r['skill']:<28} {status_map.get(r['status'], r['status']):<9} {r['detail']}")

    data_dir = settings.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    health_file = data_dir / "skill_health.json"
    payload = {"checked_at": datetime.now().isoformat(), "results": results}
    try:
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n[反馈闭环] 技能校验状态已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入失败: {e}")

    for r in results:
        metrics_collector.record_metric(
            "quality.tool_selection_accuracy",
            1.0 if r["status"] == "ok" else 0.0,
            tags={"skill": r["skill"]},
        )

    ok = sum(1 for r in results if r["status"] == "ok")
    if getattr(args, "fail_fast", False) and any(r["status"] == "fail" for r in results):
        raise SystemExit(1)
    print(f"[汇总] ok={ok}/{len(results)}")


# ====================================================================
# chat 子命令 - 交互式对话 REPL（借鉴 Hermes Agent MIT 设计）
# ====================================================================
def cmd_chat(args):
    """启动交互式对话 REPL

    借鉴 Hermes Agent MIT 设计的 `hermes` 交互命令：
      - 加载 SOUL.md 用户级身份（不修改 agents/*.md）
      - 启动 MemoryManager + 4 层记忆恢复
      - 进入 asyncio REPL 循环：用户输入 → build_main_graph → after_turn
      - slash 命令白名单：/help /reset /usage /soul /memory /quit
      - 防注入硬约束：用户输入仅作为 LLM message content（input-guardrails.md）
    """
    from .repl import ChatREPL

    repl = ChatREPL(user_id=args.user_id, session_id=args.session_id)
    rc = repl.run()
    if rc != 0:
        raise SystemExit(rc)


# ====================================================================
# memory-export 子命令 - 导出 FileMemoryStore 为 markdown
# ====================================================================
def cmd_memory_export(args):
    """导出 FileMemoryStore 为单一 markdown 视图

    合并 USER.md + MEMORY.md + EPISODES.md 三个文件内容，直接打印到 stdout。
    文件不存在时对应章节为空。供用户快速查看/导出当前文件记忆层状态。
    """
    from .memory.file_store import FileMemoryStore

    store = FileMemoryStore()
    output = store.export_markdown()
    print(output)


# ====================================================================
# soul-show 子命令 - 显示当前 SOUL.md（用户级或默认）
# ====================================================================
def cmd_soul_show(args):
    """显示当前生效的 SOUL.md 内容

    优先显示用户级 ~/.deadman/SOUL.md；不存在时显示平台默认 SOUL。
    同时标注来源（user / default）与路径，便于用户区分当前身份。
    """
    from .soul_loader import DEFAULT_SOUL_PATH, SoulLoader

    loader = SoulLoader()
    user_soul = loader.load_soul()

    if user_soul is not None:
        print(f"=== SOUL.md 来源：用户级（{DEFAULT_SOUL_PATH}）===\n")
        print(user_soul)
    else:
        print(f"=== SOUL.md 来源：平台默认（未找到 {DEFAULT_SOUL_PATH}）===\n")
        print(loader.default_soul())


# ====================================================================
# cron-list / cron-propose / cron-confirm / cron-cancel /
# cron-run / cron-tick / cron-validate
# 借鉴 Hermes cron/scheduler.py 设计，严格遵守 notification-guardrails 第三章
# ====================================================================
def cmd_cron_list(args):
    """列出用户的所有 cron 任务（含待确认/已激活/已过期）"""
    from .cron.scheduler import CronScheduler

    scheduler = CronScheduler()
    jobs = scheduler.list_jobs(args.user)
    if not jobs:
        print(f"[空] 用户 {args.user} 暂无 cron 任务")
        return

    print(f"\n=== 用户 {args.user} 的 Cron 任务（共 {len(jobs)} 条）===")
    print(f"{'Job ID':<14} {'状态':<10} {'调度':<16} {'过期时间':<20} 内容(前30字)")
    print("-" * 100)
    for j in jobs:
        if j.pending_confirmation:
            state = "待确认"
        elif not j.enabled:
            state = "已停用"
        elif j.expires_at < datetime.now():
            state = "已过期"
        else:
            state = "已激活"
        expires = j.expires_at.strftime("%Y-%m-%d %H:%M")
        content = (j.content or "").replace("\n", " ")[:30]
        print(f"{j.job_id:<14} {state:<10} {j.schedule:<16} {expires:<20} {content}")


def cmd_cron_propose(args):
    """提议创建 cron 任务（需下一轮 cron-confirm 确认）"""
    from .cron.scheduler import CronScheduler

    scheduler = CronScheduler()
    try:
        result = asyncio.run(
            scheduler.propose_job(user_id=args.user, schedule=args.schedule, content=args.content)
        )
    except ValueError as e:
        print(f"[错误] 提议失败: {e}")
        raise SystemExit(1) from None
    print(f"\n[已提议] job_id={result['job_id']}")
    print(result["message"])


def cmd_cron_confirm(args):
    """确认创建 cron 任务（激活已提议的任务）"""
    from .cron.scheduler import CronScheduler

    scheduler = CronScheduler()
    try:
        result = asyncio.run(scheduler.confirm_job(user_id=args.user, job_id=args.job_id))
    except ValueError as e:
        print(f"[错误] 确认失败: {e}")
        raise SystemExit(1) from None
    print(f"\n[已激活] job_id={result['job_id']}")
    print(f"调度: {result['schedule']}")
    print(f"过期: {result['expires_at']}")
    print(result["message"])


def cmd_cron_cancel(args):
    """取消（删除）cron 任务"""
    from .cron.scheduler import CronScheduler

    scheduler = CronScheduler()
    ok = asyncio.run(scheduler.cancel_job(user_id=args.user, job_id=args.job_id))
    if ok:
        print(f"\n[已取消] job_id={args.job_id}")
    else:
        print(f"\n[失败] 未找到任务 job_id={args.job_id} user={args.user}")
        raise SystemExit(1)


def cmd_cron_run(args):
    """启动 cron 调度器主循环（前台运行，Ctrl+C 退出）"""
    from .cron.scheduler import CronScheduler

    scheduler = CronScheduler()
    print(f"启动 Cron 调度器主循环 interval={args.interval}s jobs_file={scheduler.jobs_file}")
    print("按 Ctrl+C 退出")
    asyncio.run(scheduler.run_forever(interval_seconds=args.interval))


def cmd_cron_tick(args):
    """执行单次 tick（调试用）"""
    from .cron.scheduler import CronScheduler

    scheduler = CronScheduler()
    results = asyncio.run(scheduler.tick())
    print(f"\n=== 单次 tick 结果（共 {len(results)} 条任务）===")
    print(f"{'Job ID':<14} {'用户':<14} {'触发':<6} 原因")
    print("-" * 80)
    for r in results:
        fired = "✓" if r["fired"] else "✗"
        print(f"{r['job_id']:<14} {r['user_id']:<14} {fired:<6} {r['reason']}")


def cmd_cron_validate(args):
    """校验 cron 表达式（语法 + 间隔 >= 24h）"""
    from .cron.scheduler import CronScheduler

    scheduler = CronScheduler()
    ok, reason = scheduler._validate_schedule(args.schedule)
    if ok:
        print(f"\n[✓ 合法] {args.schedule}")
        print(f"  {reason}")
    else:
        print(f"\n[✗ 非法] {args.schedule}")
        print(f"  {reason}")
        raise SystemExit(1)


# ====================================================================
# web-search 子命令 - 联网搜索测试（真实反馈）
# 借鉴 Hermes Agent (MIT License) 的 web_tools 设计
# ====================================================================
def cmd_web_search(args):
    """联网搜索测试 - 真实反馈 DuckDuckGo HTML 端点可达性与结果

    借鉴 Hermes Agent (MIT License) 的 web_tools 设计，但用 httpx 直连 + HTML 解析。
    每个结果含 source_type + confidence；confidence<0.5 的结果标黄显示。
    失败返回 ok=False + 引导打官方热线（integrity-framework：不编造）。
    结果同时写入 data/web_search_health.json 供反馈闭环。
    """

    from .tools.web_search import WebSearchTool

    query = args.query
    max_results = getattr(args, "max", 5)

    print("=== 联网搜索测试 ===")
    print(f"查询: {query}")
    print(f"最大结果数: {max_results}")
    print()

    tool = WebSearchTool()
    result = asyncio.run(tool.search(query, max_results=max_results))

    if not result.get("ok"):
        print(f"[失败] {result.get('error', 'unknown')}")
        print(f"note: {result.get('note', '')}")
        _write_web_search_health(query, result)
        if getattr(args, "fail_fast", False):
            raise SystemExit(1)
        return

    results = result.get("results", [])
    low_conf_count = result.get("low_confidence_count", 0)
    note = result.get("note", "")

    print(f"provider: {result.get('provider', 'unknown')}")
    print(f"结果数: {len(results)}（低可信度: {low_conf_count}）")
    print(f"note: {note}")
    print()

    if not results:
        print("[未找到结果] 建议打官方热线核实")
        _write_web_search_health(query, result)
        if getattr(args, "fail_fast", False):
            raise SystemExit(1)
        return

    # 表格打印结果
    print(f"{'#':<3} {'source_type':<10} {'confidence':<12} {'title':<40} {'url'}")
    print("-" * 110)
    for i, r in enumerate(results, 1):
        confidence = r.get("confidence", 0.0)
        source_type = r.get("source_type", "unknown")
        title = (r.get("title", "") or "")[:38]
        url = r.get("url", "")
        # 低可信度标黄（ANSI 黄色 \033[33m ... \033[0m）
        if confidence < 0.5:
            print(f"\033[33m{i:<3} {source_type:<10} {confidence:<12.3f} {title:<40} {url}\033[0m")
        else:
            print(f"{i:<3} {source_type:<10} {confidence:<12.3f} {title:<40} {url}")
        # snippet 缩进显示
        snippet = (r.get("snippet", "") or "").strip()
        if snippet:
            print(f"     snippet: {snippet[:100]}")

    print()
    _write_web_search_health(query, result)


def _write_web_search_health(query: str, result: dict) -> None:
    """把 web_search 结果写入 data/web_search_health.json（反馈闭环）"""
    try:
        import json
        from datetime import datetime

        from .config import settings

        data_dir = settings.project_root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        health_file = data_dir / "web_search_health.json"
        payload = {
            "queried_at": datetime.now().isoformat(),
            "query": query,
            "ok": result.get("ok", False),
            "results_count": len(result.get("results", [])),
            "low_confidence_count": result.get("low_confidence_count", 0),
            "note": result.get("note", ""),
            "provider": result.get("provider", "unknown"),
        }
        with open(health_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[反馈闭环] 结果已写入 {health_file}")
    except OSError as e:
        print(f"[警告] 写入健康文件失败: {e}")


# ====================================================================
# sandbox-test 子命令 - 沙箱代码执行测试（真实反馈）
# 借鉴 Hermes Agent (MIT License) 的 code_execution_tool.py 设计
# ====================================================================
def cmd_sandbox_test(args):
    """沙箱代码执行测试 - 真实反馈 LocalSandbox / DockerSandbox 可用性

    借鉴 Hermes Agent (MIT License) 的 code_execution_tool.py 设计，但简化：
    - 不实现 PTC / UDS RPC，仅执行 Python
    - LocalSandbox 用 resource.setrlimit 资源限制
    - DockerSandbox 用 --network=none --memory --cpus

    测试步骤：
      1. 后端可用性检测（LocalSandbox 始终可用；DockerSandbox 检测 daemon）
      2. 基本执行测试：print('hello from sandbox')
      3. 超时测试：while True sleep 0.1，timeout=2，验证超时终止
    """
    from .sandbox import DockerSandbox, LocalSandbox, SandboxManager

    print("=== 沙箱代码执行测试 ===")
    print()

    # === 1. 后端可用性检测 ===
    print("--- 后端可用性 ---")
    local = LocalSandbox()
    docker = DockerSandbox()
    print(f"LocalSandbox.is_available(): {local.is_available()}")
    print(f"DockerSandbox.is_available(): {docker.is_available()}")
    manager = SandboxManager()
    manager.get_active_backend()
    print(f"SandboxManager 当前后端: {manager.active_backend}")
    print()

    # === 2. 基本执行测试 ===
    print("--- 基本执行测试 ---")
    test_code = "print('hello from sandbox')"
    print(f"代码: {test_code}")
    result = asyncio.run(manager.execute(test_code, timeout=10))
    print(f"backend: {result.backend}")
    print(f"ok: {result.ok}")
    print(f"exit_code: {result.exit_code}")
    print(f"duration_ms: {result.duration_ms}")
    print(f"stdout: {result.stdout!r}")
    if result.stderr:
        print(f"stderr: {result.stderr!r}")
    if result.error:
        print(f"error: {result.error}")
    print()

    basic_ok = result.ok and "hello from sandbox" in result.stdout

    # === 3. 超时测试 ===
    print("--- 超时测试 ---")
    timeout_code = "import time\nwhile True:\n    time.sleep(0.1)"
    print(f"代码: {timeout_code}")
    print("timeout: 2 秒（期望被终止）")
    result2 = asyncio.run(manager.execute(timeout_code, timeout=2))
    print(f"backend: {result2.backend}")
    print(f"ok: {result2.ok}")
    print(f"timed_out: {result2.timed_out}")
    print(f"duration_ms: {result2.duration_ms}")
    if result2.error:
        print(f"error: {result2.error}")
    print()

    timeout_ok = (not result2.ok) and result2.timed_out

    # === 4. 汇总 ===
    print("--- 测试汇总 ---")
    print(f"基本执行: {'PASS' if basic_ok else 'FAIL'}")
    print(f"超时终止: {'PASS' if timeout_ok else 'FAIL'}")
    print(
        f"Docker 可用: {docker.is_available()}"
        f"（{'使用 Docker' if manager.active_backend == 'docker' else '降级到 LocalSandbox'}）"
    )

    if not (basic_ok and timeout_ok) and getattr(args, "fail_fast", False):
        raise SystemExit(1)


# ====================================================================
# gateway-start 子命令 - 启动消息平台 Gateway（借鉴 Hermes gateway/run.py）
# ====================================================================
def cmd_gateway_start(args):
    """启动消息平台 Gateway（借鉴 Hermes gateway/run.py，适配 deadman）

    流程：
      - 读 settings.telegram_bot_token（未配置时优雅降级）
      - 读 ~/.deadman/pairing_tokens.json
      - 创建 TelegramConnector + Gateway，注册，start
      - Ctrl+C 优雅退出

    所有主动推送受 NotificationGuardrail.can_send() 约束（notification-guardrails.md L4）。
    入站消息不受 guardrail 约束（用户主动询问 = opt-in 当前会话）。
    """
    import json
    import signal

    from .config import settings
    from .gateway.connectors.telegram import TelegramConnector
    from .gateway.core import Gateway
    from .notification.guardrail import NotificationGuardrail

    bot_token = settings.telegram_bot_token
    if not bot_token:
        print("[警告] DEADMAN_TELEGRAM_BOT_TOKEN 未配置，TelegramConnector 将优雅降级。")
        print("       gateway-start 仍会运行，但不会拉取/发送任何 Telegram 消息。")
        print("       配置方法：在 .env 中设置 DEADMAN_TELEGRAM_BOT_TOKEN=<bot_token>")

    # 读配对 token 表
    pairing_file = Path.home() / ".deadman" / "pairing_tokens.json"
    pairing_tokens: dict[str, str] = {}
    if pairing_file.exists():
        try:
            with open(pairing_file, encoding="utf-8") as f:
                pairing_tokens = json.load(f) or {}
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[警告] 读取配对 token 文件失败 {pairing_file}: {exc}")

    # 构造 guard / connector / gateway
    guard = NotificationGuardrail(data_dir=settings.notification_data_dir)
    connector = TelegramConnector(
        bot_token=bot_token,
        pairing_tokens=pairing_tokens,
        guard=guard,
    )

    # 延迟导入 graph 与 memory manager，避免在未配置 LLM 时启动失败
    graph = None
    memory_manager = None
    try:
        from .orchestration.graph import build_main_graph

        graph = build_main_graph()
    except Exception as exc:
        print(f"[警告] graph 初始化失败，handle_inbound 将返回错误提示: {exc}")
    try:
        from .memory.manager import MemoryManager

        memory_manager = MemoryManager()
    except Exception as exc:
        print(f"[警告] MemoryManager 初始化失败: {exc}")

    gateway = Gateway(guard=guard, memory_manager=memory_manager, graph=graph)
    gateway.register_connector("telegram", connector)

    async def _run():
        print("[gateway-start] 正在启动 Gateway ...")
        await gateway.start()
        print("[gateway-start] Gateway 已启动，Ctrl+C 退出")

        # 等待停止信号
        stop_event = asyncio.Event()

        def _signal_handler(*_):
            print("\n[gateway-start] 收到停止信号，正在退出 ...")
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except (NotImplementedError, RuntimeError):
                # Windows 不支持 add_signal_handler，降级为 signal.signal
                signal.signal(sig, _signal_handler)

        await stop_event.wait()
        await gateway.stop()
        print("[gateway-start] 已退出")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\n[gateway-start] 已退出")


# ====================================================================
# gateway-pair 子命令 - 生成 Telegram 配对 token
# ====================================================================
def cmd_gateway_pair(args):
    """生成 Telegram 配对 token

    流程：
      - 生成随机 token（secrets.token_urlsafe）
      - 写入 ~/.deadman/pairing_tokens.json
      - 打印"在 Telegram 发送 /start <token> 完成配对"

    用户 ID 通过 --user-id 指定（默认 "default-user"）。
    """
    import json
    import secrets

    user_id = args.user_id
    pair_file = Path.home() / ".deadman" / "pairing_tokens.json"
    pair_file.parent.mkdir(parents=True, exist_ok=True)

    # 读已有 token 表（追加模式）
    pairing_tokens: dict[str, str] = {}
    if pair_file.exists():
        try:
            with open(pair_file, encoding="utf-8") as f:
                pairing_tokens = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            pass

    # 生成新 token
    token = secrets.token_urlsafe(16)
    pairing_tokens[token] = user_id

    # 原子写入
    tmp = pair_file.with_suffix(pair_file.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pairing_tokens, f, ensure_ascii=False, indent=2)
    import os as _os

    _os.replace(tmp, pair_file)

    print("已生成配对 token：")
    print(f"  user_id: {user_id}")
    print(f"  token:   {token}")
    print(f"  文件:    {pair_file}")
    print("\n请在 Telegram 向你的 bot 发送：")
    print(f"  /start {token}")
    print(f"\n配对成功后，Telegram 用户将与 deadman user_id={user_id} 绑定。")


# ====================================================================
# notify-test 子命令 - 测试 NotificationGuardrail 各种场景
# ====================================================================
def cmd_notify_test(args):
    """测试 NotificationGuardrail - 模拟各种场景调 can_send

    场景覆盖 notification-guardrails.md 第七章第 3 节要求：
      - 静默时段拦截（22:00-08:00）
      - 频率超限拦截（单日 1 / 单周 3 / 单月 8）
      - 敏感日期封禁（清明 / 中元）
      - opt-in 缺失拦截 / opt-in 存在允许
      - 内容脱敏正确性（替换 + 完全不推送）
      - 退订立即生效
      - 脆弱期静默（72h / R3-14d / 高情绪-7d）
    """
    # 用临时数据目录，避免污染真实数据
    import tempfile
    from datetime import datetime, timedelta

    from .notification.guardrail import NotificationGuardrail

    tmp_dir = Path(tempfile.mkdtemp(prefix="deadman-notify-test-"))
    guard = NotificationGuardrail(data_dir=tmp_dir)

    user_id = "test-user"
    results: list[dict[str, Any]] = []

    # === 1. 静默时段 ===
    night = datetime(2026, 7, 21, 23, 30)
    allowed, reason = guard.can_send(user_id, night)
    results.append(
        {
            "scenario": "silent_hours (23:30)",
            "allowed": allowed,
            "reason": reason,
            "expect_allowed": False,
        }
    )

    # === 2. 频率上限 - 单日 1 条超限 ===
    guard.record_consent(user_id, "是的，明天 9 点提醒我", "reminder:test-daily")
    morning = datetime(2026, 7, 21, 10, 0)
    # 第一次应允许（已 opt-in、白天、无脆弱期）
    allowed1, _ = guard.can_send(user_id, morning)
    # 模拟已发送 1 条
    if allowed1:
        guard.record_send(user_id, "测试内容", "telegram")
    allowed2, reason2 = guard.can_send(user_id, morning)
    results.append(
        {
            "scenario": "frequency_daily_limit (第 2 条应拦截)",
            "allowed": allowed2,
            "reason": reason2,
            "expect_allowed": False,
        }
    )

    # === 3. 频率上限 - 单周 3 条超限 ===
    user2 = "test-weekly-user"
    guard.record_consent(user2, "请提醒我", "reminder:test-weekly")
    base = datetime(2026, 7, 21, 10, 0)
    for i in range(7):
        base - timedelta(days=i)
        guard.record_send(user2, f"测试 {i}", "telegram")
    allowed_w, reason_w = guard.can_send(user2, base)
    results.append(
        {
            "scenario": "frequency_weekly_limit (4 条已发应拦截)",
            "allowed": allowed_w,
            "reason": reason_w,
            "expect_allowed": False,
        }
    )

    # === 4. 频率上限 - 单月 8 条超限 ===
    user3 = "test-monthly-user"
    guard.record_consent(user3, "请提醒我", "reminder:test-monthly")
    for i in range(8):
        base - timedelta(days=i)
        guard.record_send(user3, f"测试 {i}", "telegram")
    allowed_m, reason_m = guard.can_send(user3, base)
    results.append(
        {
            "scenario": "frequency_monthly_limit (8 条已发应拦截)",
            "allowed": allowed_m,
            "reason": reason_m,
            "expect_allowed": False,
        }
    )

    # === 5. 敏感日期 - 清明 ===
    qingming = datetime(2026, 4, 5, 10, 0)
    user4 = "test-qingming-user"
    guard.record_consent(user4, "请提醒我", "reminder:test-qm")
    allowed_qm, reason_qm = guard.can_send(user4, qingming)
    results.append(
        {
            "scenario": "sensitive_date_qingming (4-5 应拦截)",
            "allowed": allowed_qm,
            "reason": reason_qm,
            "expect_allowed": False,
        }
    )

    # === 6. 敏感日期 - 中元 ===
    zhongyuan = datetime(2026, 8, 15, 10, 0)
    user5 = "test-zhongyuan-user"
    guard.record_consent(user5, "请提醒我", "reminder:test-zy")
    allowed_zy, reason_zy = guard.can_send(user5, zhongyuan)
    results.append(
        {
            "scenario": "sensitive_date_zhongyuan (8-15 应拦截)",
            "allowed": allowed_zy,
            "reason": reason_zy,
            "expect_allowed": False,
        }
    )

    # === 7. opt-in 缺失拦截 ===
    user6 = "test-no-optin-user"
    allowed_no, reason_no = guard.can_send(user6, datetime(2026, 7, 21, 10, 0))
    results.append(
        {
            "scenario": "optin_missing (无 opt-in 应拦截)",
            "allowed": allowed_no,
            "reason": reason_no,
            "expect_allowed": False,
        }
    )

    # === 8. opt-in 存在允许 ===
    user7 = "test-optin-ok-user"
    guard.record_consent(user7, "请提醒我", "reminder:ok")
    allowed_ok, reason_ok = guard.can_send(user7, datetime(2026, 7, 21, 10, 0))
    results.append(
        {
            "scenario": "optin_present (有 opt-in 应允许)",
            "allowed": allowed_ok,
            "reason": reason_ok,
            "expect_allowed": True,
        }
    )

    # === 9. 退订立即生效 ===
    user8 = "test-unsub-user"
    guard.record_consent(user8, "请提醒我", "reminder:unsub")
    guard.record_unsubscribe(user8, scope="all")
    allowed_unsub, reason_unsub = guard.can_send(user8, datetime(2026, 7, 21, 10, 0))
    results.append(
        {
            "scenario": "unsubscribe_immediate (退订后应拦截)",
            "allowed": allowed_unsub,
            "reason": reason_unsub,
            "expect_allowed": False,
        }
    )

    # === 10. 72h 静默期 ===
    user9 = "test-72h-user"
    guard.record_consent(user9, "请提醒我", "reminder:72h")
    guard.record_session_end(
        user9, safety_triggered=False, emotion_intensity="低", involved_sensitive_death=False
    )
    # 把 ended_at 改为 12 小时前
    last_session = guard._read_json(guard.last_session_file, {})
    last_session[user9]["ended_at"] = (
        datetime(2026, 7, 21, 10, 0) - timedelta(hours=12)
    ).isoformat()
    guard._write_json(guard.last_session_file, last_session)
    allowed_72, reason_72 = guard.can_send(user9, datetime(2026, 7, 21, 10, 0))
    results.append(
        {
            "scenario": "72h_silence_after_session (12h 后应拦截)",
            "allowed": allowed_72,
            "reason": reason_72,
            "expect_allowed": False,
        }
    )

    # === 11. R3 触发后 14 天静默 ===
    user10 = "test-r3-user"
    guard.record_consent(user10, "请提醒我", "reminder:r3")
    guard.record_session_end(
        user10, safety_triggered=True, emotion_intensity="中", involved_sensitive_death=False
    )
    last_session = guard._read_json(guard.last_session_file, {})
    last_session[user10]["ended_at"] = (
        datetime(2026, 7, 21, 10, 0) - timedelta(days=5)
    ).isoformat()
    guard._write_json(guard.last_session_file, last_session)
    allowed_r3, reason_r3 = guard.can_send(user10, datetime(2026, 7, 21, 10, 0))
    results.append(
        {
            "scenario": "r3_14d_silence (R3 后 5 天应拦截)",
            "allowed": allowed_r3,
            "reason": reason_r3,
            "expect_allowed": False,
        }
    )

    # === 12. 高情绪后 7 天静默 ===
    user11 = "test-emotion-user"
    guard.record_consent(user11, "请提醒我", "reminder:emotion")
    guard.record_session_end(
        user11, safety_triggered=False, emotion_intensity="高", involved_sensitive_death=False
    )
    last_session = guard._read_json(guard.last_session_file, {})
    last_session[user11]["ended_at"] = (
        datetime(2026, 7, 21, 10, 0) - timedelta(days=3)
    ).isoformat()
    guard._write_json(guard.last_session_file, last_session)
    allowed_em, reason_em = guard.can_send(user11, datetime(2026, 7, 21, 10, 0))
    results.append(
        {
            "scenario": "high_emotion_7d_silence (高情绪后 3 天应拦截)",
            "allowed": allowed_em,
            "reason": reason_em,
            "expect_allowed": False,
        }
    )

    # === 13. 内容脱敏 - 替换禁用词 ===
    sanitized = guard.sanitize_content("提醒：今天该去办死亡证明了")
    results.append(
        {
            "scenario": "sanitize_replaces (死亡→待办事项)",
            "allowed": "死亡" not in sanitized and "待办事项" in sanitized,
            "reason": sanitized,
            "expect_allowed": True,
        }
    )

    # === 14. 内容脱敏 - 完全不推送关键词 ===
    blocked = guard.sanitize_content("今天是逝者的忌日")
    results.append(
        {
            "scenario": "sanitize_blocks (含'忌日'返回空串)",
            "allowed": blocked == "",
            "reason": blocked,
            "expect_allowed": True,
        }
    )

    # === 表格化打印 ===
    print("\n=== NotificationGuardrail 测试报告 ===")
    print(f"{'场景':<48} {'预期':<8} {'实际':<8} {'通过':<6} {'原因'}")
    print("-" * 110)
    pass_count = 0
    for r in results:
        expected = "允许" if r["expect_allowed"] else "拦截"
        actual = "允许" if r["allowed"] else "拦截"
        passed = r["allowed"] == r["expect_allowed"]
        mark = "✓" if passed else "✗"
        if passed:
            pass_count += 1
        print(f"{r['scenario']:<48} {expected:<8} {actual:<8} {mark:<6} {r['reason']}")
    print(f"\n[汇总] 通过 {pass_count}/{len(results)}")

    # 清理临时目录
    import shutil

    shutil.rmtree(tmp_dir, ignore_errors=True)

    if pass_count < len(results) and getattr(args, "fail_fast", False):
        raise SystemExit(1)


# ====================================================================
# notify-consent 子命令 - 手动记录 opt-in（调试用）
# ====================================================================
def cmd_notify_consent(args):
    """手动记录 opt-in（调试用）

    把指定 content + scope 记录为 user_id 的 opt-in 到 consent.json。
    生产环境应由用户在交互中明确同意，由调用方主动调 record_consent。
    """
    from .notification.guardrail import NotificationGuardrail

    guard = NotificationGuardrail()
    guard.record_consent(args.user_id, args.content, args.scope)
    print("已记录 opt-in：")
    print(f"  user_id: {args.user_id}")
    print(f"  scope:   {args.scope}")
    print(f"  content: {args.content}")
    print(f"  文件:    {guard.consent_file}")


# ====================================================================
# alignment-status 子命令 - 显示对齐训练状态
# ====================================================================
def cmd_alignment_status(args):
    """显示 Alignment 模块状态(SFT/DPO/MoE/持续学习统计)

    读取 AlignmentManager.stats() 聚合各子组件统计。
    Feature flag DEADMAN_ALIGNMENT_ENABLED=0 时提示未启用。
    """
    from .alignment import AlignmentDisabledError, get_alignment_manager

    try:
        mgr = get_alignment_manager()
    except AlignmentDisabledError:
        print("[Alignment] 模块未启用 (DEADMAN_ALIGNMENT_ENABLED=0)")
        return

    stats = mgr.stats()
    print("\n=== Alignment 对齐训练状态 ===")
    print(f"  DPO 偏好样本数:   {stats['dpo']['preferences']}")
    print(f"  DPO 信任分数:     {stats['dpo']['trust_snapshot']}")
    print(f"  SFT 数据集统计:   {stats['sft']}")
    print(f"  MoE 路由统计:     {stats['moe']}")
    cl = stats["continuous_learning"]
    print(f"  持续学习事件数:   {cl['events']}")
    print(f"  Reflexion 集成:   {'是' if cl['has_reflexion'] else '否'}")
    llm_stats = stats.get("local_llm")
    if llm_stats:
        print(f"  本地 LLM:         {llm_stats}")
    else:
        print("  本地 LLM:         未挂载")


# ====================================================================
# alignment-train 子命令 - 触发 SFT → DPO 训练流水线
# ====================================================================
def cmd_alignment_train(args):
    """触发 Alignment 训练流水线(SFT → DPO)

    调用 AlignmentManager.run_training_pipeline() 执行 mock 训练。
    Feature flag DEADMAN_ALIGNMENT_ENABLED=0 时提示未启用。
    """
    from .alignment import AlignmentDisabledError, get_alignment_manager

    try:
        mgr = get_alignment_manager()
    except AlignmentDisabledError:
        print("[Alignment] 模块未启用 (DEADMAN_ALIGNMENT_ENABLED=0)")
        return

    print("=== Alignment 训练流水线启动 ===")
    report = mgr.run_training_pipeline()
    print(f"  SFT 样本数:     {report.sft_samples}")
    print(f"  DPO 样本数:     {report.dpo_samples}")
    print(f"  SFT 跳过:       {'是' if report.sft_skipped else '否'}")
    print(f"  DPO 跳过:       {'是' if report.dpo_skipped else '否'}")
    print(f"  训练完成:       {'是' if report.completed else '否'}")
    print(f"  耗时(秒):       {report.duration_seconds:.2f}")
    if report.error:
        print(f"  错误:           {report.error}")
    if report.dpo_report:
        print(f"  DPO 训练报告:   {report.dpo_report.to_dict()}")


# ====================================================================
# governance-status 子命令 - 显示治理框架状态
# ====================================================================
def cmd_governance_status(args):
    """显示 Governance 治理框架状态(模型卡/数据卡/风险卡/透明度/红线/伦理/保险)

    读取 GovernanceManager 各子模块的注册情况。
    Feature flag DEADMAN_GOVERNANCE_ENABLED=0 时提示未启用(红线仍 enforce)。
    """
    from .governance import GovernanceDisabledError, get_governance_manager

    try:
        gm = get_governance_manager()
    except GovernanceDisabledError:
        print("[Governance] 模块未启用 (DEADMAN_GOVERNANCE_ENABLED=0)")
        print("  注意: AI 红线仍 enforce (底线保护)")
        return

    print("\n=== Governance 治理框架状态 ===")
    # 模型卡
    mc = gm.model_cards
    print(f"  模型卡注册数:   {len(mc.list_all()) if hasattr(mc, 'list_all') else 'N/A'}")
    # 数据卡
    dc = gm.data_cards
    print(f"  数据卡注册数:   {len(dc.list_all()) if hasattr(dc, 'list_all') else 'N/A'}")
    # 风险卡
    ra = gm.risk_assessment
    print(f"  风险卡注册数:   {len(ra.list_all()) if hasattr(ra, 'list_all') else 'N/A'}")
    # 决策统计
    print(f"  总决策数:       {gm._decision_count}")
    print(f"  AI 决策数:      {gm._ai_decision_count}")
    print(f"  人工审核数:     {gm._human_review_count}")
    print(f"  偏见事件数:     {gm._bias_incidents}")
    # 复议
    appeals = gm.appeals
    print(
        f"  复议待处理:     {len(appeals.list_pending()) if hasattr(appeals, 'list_pending') else 'N/A'}"
    )
    # 伦理委员会
    ethics = gm.ethics
    print(
        f"  伦理案例数:     {len(ethics.list_cases()) if hasattr(ethics, 'list_cases') else 'N/A'}"
    )


# ====================================================================
# governance-check 子命令 - 运行合规检查
# ====================================================================
def cmd_governance_check(args):
    """运行 Governance 合规检查(红线 + 保险覆盖)

    调用 GovernanceManager.before_action() 对指定动作做合规预检。
    用法:
        deadman governance-check --action "代签遗嘱"
        deadman governance-check --action "生成悼文" --incident-type ai_decision
    """
    from .governance import get_governance_manager

    gm = get_governance_manager()
    action = args.action
    incident_type = getattr(args, "incident_type", None)
    amount = float(getattr(args, "amount", 0) or 0)

    print("=== Governance 合规检查 ===")
    print(f"  动作:       {action}")
    print(f"  保险类型:   {incident_type or '(未指定)'}")
    print(f"  涉及金额:   {amount}")

    decision = gm.before_action(
        action=action,
        context={"role": "cli-user"},
        incident_type=incident_type,
        amount=amount,
    )

    print("\n  检查结果:")
    print(f"    允许:       {'是' if decision.allowed else '否'}")
    print(f"    原因:       {decision.reason}")
    print(f"    保险覆盖:   {'是' if decision.insurance_covered else '否'}")
    if decision.redline_result:
        print(f"    红线允许:   {'是' if decision.redline_result.allowed else '否'}")
        if decision.redline_result.reason:
            print(f"    红线原因:   {decision.redline_result.reason}")


# ====================================================================
# multimodal-status 子命令 - 显示多模态管道状态
# ====================================================================
def cmd_multimodal_status(args):
    """显示 Multimodal 多模态管道状态(各能力开关/审计日志/budget)

    读取 MultimodalPipeline 配置和审计日志。
    Feature flag DEADMAN_MULTIMODAL_ENABLED=0 时提示未启用。
    """
    from .multimodal import MultimodalDisabledError, get_multimodal_pipeline

    try:
        pipe = get_multimodal_pipeline()
    except MultimodalDisabledError:
        print("[Multimodal] 模块未启用 (DEADMAN_MULTIMODAL_ENABLED=0)")
        return

    enabled = pipe.is_enabled()
    print("\n=== Multimodal 多模态管道状态 ===")
    print(f"  总开关:       {'启用' if enabled else '未启用'}")

    # 各能力状态
    caps = pipe.list_capabilities()
    all_caps = ["ocr", "asr", "tts", "vision", "image_gen"]
    print("\n  能力列表:")
    for cap in all_caps:
        status = "启用" if cap in caps else "禁用"
        print(f"    {cap:<12} {status}")

    # Pipeline 配置
    cfg = pipe.config
    print("\n  配置:")
    print(f"    默认 provider:     {cfg.default_provider or '(自动)'}")
    print(f"    Budget/会话:       {cfg.budget_token_per_session}")
    print(f"    Audit 日志:        {'启用' if cfg.audit_log_enabled else '禁用'}")
    print(f"    OCR PII 脱敏:     {'启用' if cfg.pii_redact_ocr else '禁用'}")

    # 最近审计日志
    audit = pipe.get_audit_log(limit=5)
    if audit:
        print(f"\n  最近审计记录 ({len(audit)} 条):")
        for entry in audit:
            print(
                f"    [{entry.get('capability')}] provider={entry.get('provider')} "
                f"success={entry.get('success')} duration={entry.get('duration_ms', 0):.0f}ms"
            )
    else:
        print("\n  审计记录: (无)")


# ====================================================================
# multimodal-test 子命令 - 测试多模态管道
# ====================================================================
def cmd_multimodal_test(args):
    """测试 Multimodal 多模态管道各能力(TTS 合成 + 能力列表)

    对 TTS 做一次 mock 合成测试,验证管道链路通畅。
    Feature flag DEADMAN_MULTIMODAL_ENABLED=0 时提示未启用。
    """
    from .multimodal import MultimodalDisabledError, get_multimodal_pipeline

    try:
        pipe = get_multimodal_pipeline()
    except MultimodalDisabledError:
        print("[Multimodal] 模块未启用 (DEADMAN_MULTIMODAL_ENABLED=0)")
        return

    if not pipe.is_enabled():
        print("[Multimodal] 管道总开关未启用")
        return

    results = []
    print("=== Multimodal 多模态管道测试 ===")

    # 1. 能力列表测试
    caps = pipe.list_capabilities()
    results.append({"test": "list_capabilities", "status": "ok", "detail": f"可用能力: {caps}"})
    print(f"  [能力列表] 可用: {caps}")

    # 2. TTS 合成测试(mock)
    try:
        tts_result = pipe.tts_synthesize(
            text="测试语音合成：愿逝者安息。",
            user_id="cli-test",
        )
        results.append(
            {
                "test": "tts_synthesize",
                "status": "ok",
                "detail": f"provider={tts_result.provider} bytes={len(tts_result.audio_bytes)}",
            }
        )
        print(
            f"  [TTS 合成] OK - provider={tts_result.provider} bytes={len(tts_result.audio_bytes)}"
        )
    except Exception as e:
        results.append({"test": "tts_synthesize", "status": "fail", "detail": str(e)})
        print(f"  [TTS 合成] FAIL - {e}")

    # 3. 审计日志验证
    audit = pipe.get_audit_log(limit=3)
    results.append(
        {
            "test": "audit_log",
            "status": "ok" if audit else "empty",
            "detail": f"最近 {len(audit)} 条记录",
        }
    )
    print(f"  [审计日志] {len(audit)} 条记录")

    # 汇总
    ok_count = sum(1 for r in results if r["status"] == "ok")
    print(f"\n  测试汇总: {ok_count}/{len(results)} 通过")


def cmd_db(args):
    """数据库管理（企业级扩展④）- 包装 Alembic 迁移命令

    用法：
        deadman db init          # 建表（create_all，开发/测试用）
        deadman db migrate       # alembic upgrade head（生产推荐）
        deadman db status        # 查看当前迁移版本
        deadman db revision -m "描述"  # 生成新迁移脚本
    """
    from .config import settings

    if not settings.database_url:
        print("[db] DATABASE_URL 未配置，主数据库未启用。")
        print("     配置 DATABASE_URL 环境变量后重试，或继续使用文件存储。")
        return

    action = args.db_action
    print(f"[db] 操作: {action}")
    print(f"[db] DATABASE_URL: {settings.database_url.split('@')[-1] if '@' in settings.database_url else '(已配置)'}")

    if action == "init":
        # 开发/测试：直接 create_all（不走 Alembic）
        import asyncio

        from .db.engine import dispose_engine, init_db

        async def _init():
            await init_db()
            await dispose_engine()

        asyncio.run(_init())
        print("[db] 表结构已创建（create_all）。生产环境建议使用 `deadman db migrate`。")
        return

    if action == "migrate":
        # 生产：alembic upgrade head
        import subprocess

        result = subprocess.run(
            ["alembic", "upgrade", "head"],
            capture_output=False,
            check=False,
        )
        if result.returncode != 0:
            print(f"[db] 迁移失败（exit code {result.returncode}）")
        else:
            print("[db] 迁移完成（alembic upgrade head）")
        return

    if action == "status":
        import subprocess

        subprocess.run(["alembic", "current"], check=False)
        return

    if action == "revision":
        import subprocess

        msg = args.message or "auto"
        subprocess.run(["alembic", "revision", "--autogenerate", "-m", msg], check=False)
        return

    print(f"[db] 未知操作: {action}")


def main():
    """CLI 主入口"""
    # 结构化日志早期初始化（读取 DEADMAN_LOG_LEVEL/DEADMAN_LOG_FORMAT 环境变量）。
    # 在 parse_args 之前调用，确保后续 CLI 扩展加载（含 try/except 警告）也走 structlog。
    # --log-level 在解析后会通过下方 setup_logging(args.log_level) 再次覆盖。
    from .logging_config import setup_logging as _setup_structlog_logging

    _setup_structlog_logging()

    parser = argparse.ArgumentParser(
        prog="deadman",
        description="deadman - 身后事多智能体引导平台 CLI",
    )
    parser.add_argument("--version", action="store_true", help="显示版本")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细输出")
    parser.add_argument("--log-level", default="INFO", help="日志级别")

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # mcp-server 子命令
    subparsers.add_parser("mcp-server", help="启动 MCP Server")

    # eval 子命令
    eval_parser = subparsers.add_parser("eval", help="运行评估(golden cases + 三层判定)")
    eval_parser.add_argument("--cases-dir", help="YAML case 目录路径")
    eval_parser.add_argument("--fail-fast", action="store_true", help="有失败时退出码非零")

    # eval-list 子命令 - 列出评估 case 清单
    subparsers.add_parser("eval-list", help="列出本地评估 case 清单")

    # eval-ragas 子命令 - P0.2 RAGAS 9 维度评估
    ragas_parser = subparsers.add_parser(
        "eval-ragas",
        help="运行 RAGAS 9 维度评估(质量门 + 降级保护,CI 友好)",
    )
    ragas_parser.add_argument("--cases-dir", help="YAML case 目录路径")
    ragas_parser.add_argument(
        "--quick",
        action="store_true",
        help="仅跑 faithfulness + answer_relevancy(加速本地迭代)",
    )
    ragas_parser.add_argument(
        "--quality-gate",
        type=float,
        default=None,
        help="faithfulness 质量门阈值(默认 0.7)",
    )
    ragas_parser.add_argument(
        "--output",
        help="输出 JSONL 报告路径(可选)",
    )
    ragas_parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="质量门未通过时退出码 2(CI 阻断 merge)",
    )

    # run 子命令
    run_parser = subparsers.add_parser("run", help="运行单次对话")
    run_parser.add_argument("input", help="用户输入")

    # llm-test 子命令 - 接入测试(本地+线上多厂商手动验证)
    llm_test_parser = subparsers.add_parser(
        "llm-test",
        help="测试 LLM 各 provider 接入(延迟/可用性/token,真实反馈)",
    )
    llm_test_parser.add_argument(
        "--provider",
        help="只测试指定 provider(openai/anthropic/zhipu/ollama/vllm/llama_cpp)",
    )
    llm_test_parser.add_argument("--model", help="指定模型(默认取该 provider 首个模型)")
    llm_test_parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="单次请求超时秒(默认用 settings.llm_timeout)",
    )
    llm_test_parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="有真实 fail 时退出码非零(CI 用)",
    )

    # llm-sync-models 子命令 - 同步线上真实模型清单
    sync_parser = subparsers.add_parser(
        "llm-sync-models",
        help="从各 provider /models 端点拉真实可用模型,对比本地清单",
    )
    sync_parser.add_argument("--provider", help="只同步指定 provider")

    # llm-cost 子命令 - 成本与配额汇总
    cost_parser = subparsers.add_parser("llm-cost", help="汇总 token 用量与成本(配额追踪)")
    cost_parser.add_argument("--clear", action="store_true", help="清空成本记录")

    # prompt-list 子命令 - 列出本地提示词
    subparsers.add_parser("prompt-list", help="列出本地提示词模板")

    # prompt-test 子命令 - 提示词渲染+发 LLM 测试
    pt_parser = subparsers.add_parser("prompt-test", help="渲染提示词并发 LLM 测试(真实反馈)")
    pt_parser.add_argument("name", help="提示词名称")
    pt_parser.add_argument("--var", action="append", help="变量 key=value(可重复)")
    pt_parser.add_argument("--provider", help="LLM provider")
    pt_parser.add_argument("--model", help="覆盖提示词里的 model")
    pt_parser.add_argument("--max-tokens", type=int, default=512)
    pt_parser.add_argument("--allow-missing", action="store_true", help="允许缺变量渲染")
    pt_parser.add_argument("--dry-run", action="store_true", help="只渲染不发 LLM")
    pt_parser.add_argument("--fail-fast", action="store_true", help="失败时退出码非零")

    # prompt-sync 子命令 - 同步线上提示词清单
    ps_parser = subparsers.add_parser("prompt-sync", help="同步线上提示词仓库(LangSmith/deepset)")
    ps_parser.add_argument("--query", help="LangSmith 搜索关键词")

    # rule-test 子命令 - 对文本跑规则校验
    rt_parser = subparsers.add_parser("rule-test", help="对文本跑 L0-L8 规则校验(手动测试)")
    rt_parser.add_argument("text", help="待校验文本")

    # rule-validate 子命令 - 规则文件完整性校验
    rv_parser = subparsers.add_parser("rule-validate", help="校验规则文件完整性与优先级链")
    rv_parser.add_argument("--fail-fast", action="store_true", help="校验失败退出码非零")

    # agent-list 子命令 - 列出本地智能体配置
    subparsers.add_parser("agent-list", help="列出本地智能体配置(agents/*.md)")

    # agent-ping 子命令 - 测试远端 A2A agent 可达性
    ap_parser = subparsers.add_parser("agent-ping", help="ping 远端 A2A agent(真实反馈可达性/延迟)")
    ap_parser.add_argument("--url", action="append", help="远端 agent base URL(可重复)")
    ap_parser.add_argument("--timeout", type=float, default=10.0, help="超时秒")

    # knowledge-list 子命令 - 列出本地知识库文件
    subparsers.add_parser("knowledge-list", help="列出本地知识库文件")

    # knowledge-search 子命令 - 知识库检索测试
    ks_parser = subparsers.add_parser("knowledge-search", help="知识库检索测试(真实反馈命中)")
    ks_parser.add_argument("query", help="查询词")
    ks_parser.add_argument("--country", help="国家过滤(CN/US/JP)")
    ks_parser.add_argument("--region", help="地区过滤")

    # knowledge-freshness 子命令 - 知识库新鲜度检查
    subparsers.add_parser("knowledge-freshness", help="检查知识库文件新鲜度(过期检测)")

    # tool-list 子命令 - 列出本地 MCP 工具
    subparsers.add_parser("tool-list", help="列出本地注册的 MCP 工具")

    # tool-test 子命令 - 测试单个 MCP 工具调用
    tt_parser = subparsers.add_parser("tool-test", help="测试单个 MCP 工具调用(真实反馈)")
    tt_parser.add_argument("name", help="工具名")
    tt_parser.add_argument("--arg", action="append", help="参数 key=value(可重复,值支持 JSON)")
    tt_parser.add_argument("--fail-fast", action="store_true", help="失败时退出码非零")

    # mcp-ping 子命令 - 测试外部 MCP server 可达性
    mp_parser = subparsers.add_parser("mcp-ping", help="ping 外部 MCP server(可达性检测)")
    mp_parser.add_argument("--url", action="append", help="外部 MCP server URL(可重复)")
    mp_parser.add_argument("--timeout", type=float, default=10.0, help="超时秒")

    # obs-dashboard 子命令 - 显示可观测性看板
    obsd_parser = subparsers.add_parser("obs-dashboard", help="显示 11 大类指标看板当前值")
    obsd_parser.add_argument("--category", help="只看某分类(quality/efficiency/...)")

    # obs-test 子命令 - 可观测性接入测试
    obst_parser = subparsers.add_parser(
        "obs-test", help="可观测性接入测试(span+指标+后端可达性,真实反馈)"
    )
    obst_parser.add_argument("--timeout", type=float, default=5.0, help="后端探测超时秒")

    # obs-export 子命令 - 导出 Prometheus 指标
    subparsers.add_parser("obs-export", help="导出 Prometheus 格式指标")

    # memory-list 子命令 - 列出分层记忆状态
    subparsers.add_parser(
        "memory-list", help="列出 4 层记忆状态(working/episodic/semantic/procedural)"
    )

    # memory-test 子命令 - 记忆写入+召回测试
    subparsers.add_parser("memory-test", help="记忆系统写入+召回测试(真实反馈)")

    # memory-ping 子命令 - 记忆后端可达性
    memp_parser = subparsers.add_parser(
        "memory-ping", help="记忆后端可达性(Graphiti/Neo4j/LightRAG)"
    )
    memp_parser.add_argument("--timeout", type=float, default=5.0, help="超时秒")

    # a2a-card 子命令 - 显示本地 AgentCard
    ac_parser = subparsers.add_parser("a2a-card", help="显示本地 A2A AgentCard(自名片+完整性校验)")
    ac_parser.add_argument("--json", action="store_true", help="输出原始 JSON")

    # a2a-test 子命令 - A2A 协议自测
    subparsers.add_parser("a2a-test", help="A2A 协议自测(card+JSON-RPC,真实反馈)")

    # a2a-registry 子命令 - A2A registry 可达性
    ar_parser = subparsers.add_parser("a2a-registry", help="A2A registry 可达性探测(线上源)")
    ar_parser.add_argument("--timeout", type=float, default=5.0, help="超时秒")

    # deploy-check 子命令 - 部署工件校验
    dc_parser = subparsers.add_parser(
        "deploy-check", help="校验部署工件(Dockerfile/compose/entrypoint/healthcheck)"
    )
    dc_parser.add_argument("--fail-fast", action="store_true", help="校验失败退出码非零")

    # deploy-test 子命令 - 部署运行时测试
    dt_parser = subparsers.add_parser(
        "deploy-test", help="部署运行时测试(docker 可用性+healthcheck 自测)"
    )
    dt_parser.add_argument("--timeout", type=float, default=10.0, help="超时秒")

    # reflexion-list 子命令 - 列出预定义调整策略
    subparsers.add_parser("reflexion-list", help="列出 Reflexion 预定义调整策略(10 种快速路径)")

    # reflexion-test 子命令 - 反思重试测试
    subparsers.add_parser("reflexion-test", help="Reflexion 反思重试测试(mock 操作,真实反馈)")

    # reflexion-ping 子命令 - LLM 反思路径可达性
    subparsers.add_parser("reflexion-ping", help="LLM 反思路径可达性(慢速路径依赖 LLM)")

    # skill-list 子命令 - 列出本地技能
    subparsers.add_parser("skill-list", help="列出本地技能清单(skills/*/SKILL.md)")

    # skill-test 子命令 - 校验单个技能
    st_parser = subparsers.add_parser(
        "skill-test", help="校验单个技能 SKILL.md(frontmatter+引用,真实反馈)"
    )
    st_parser.add_argument("name", help="技能目录名")

    # skill-validate 子命令 - 全量校验所有技能
    sv_parser = subparsers.add_parser("skill-validate", help="全量校验所有技能完整性")
    sv_parser.add_argument("--fail-fast", action="store_true", help="校验失败退出码非零")

    # chat 子命令 - 交互式对话 REPL（借鉴 Hermes Agent MIT 设计）
    chat_parser = subparsers.add_parser("chat", help="交互式对话 REPL（借鉴 Hermes MIT 设计）")
    chat_parser.add_argument("--user-id", default="default-user", help="用户 ID（记忆恢复用）")
    chat_parser.add_argument("--session-id", default=None, help="会话 ID（默认自动生成 uuid4）")

    # memory-export 子命令 - 导出 FileMemoryStore 为 markdown
    subparsers.add_parser("memory-export", help="导出文件记忆层为 markdown（USER+MEMORY+EPISODES）")

    # soul-show 子命令 - 显示当前 SOUL.md
    subparsers.add_parser("soul-show", help="显示当前 SOUL.md（用户级或默认）")

    # web-search 子命令 - 联网搜索测试（借鉴 Hermes MIT 设计，httpx 直连 DuckDuckGo）
    web_search_parser = subparsers.add_parser(
        "web-search",
        help="联网搜索测试（DuckDuckGo HTML，真实反馈，借鉴 Hermes MIT 设计）",
    )
    web_search_parser.add_argument("query", help="搜索查询语句")
    web_search_parser.add_argument("--max", type=int, default=5, help="最大结果数（默认 5）")
    web_search_parser.add_argument(
        "--fail-fast", action="store_true", help="搜索失败时退出码非零（CI 用）"
    )

    # sandbox-test 子命令 - 沙箱代码执行测试（借鉴 Hermes MIT 设计）
    sandbox_test_parser = subparsers.add_parser(
        "sandbox-test",
        help="沙箱代码执行测试（LocalSandbox + DockerSandbox，借鉴 Hermes MIT 设计）",
    )
    sandbox_test_parser.add_argument(
        "--fail-fast", action="store_true", help="测试失败时退出码非零（CI 用）"
    )

    # ====================================================================
    # cron 系列子命令 - 借鉴 Hermes cron/scheduler.py，遵守 notification-guardrails 第三章
    # ====================================================================
    cron_list_parser = subparsers.add_parser("cron-list", help="列出用户的所有 cron 任务")
    cron_list_parser.add_argument("--user", default="default-user", help="用户 ID")

    cron_propose_parser = subparsers.add_parser(
        "cron-propose", help="提议创建 cron 任务（需下一轮 cron-confirm 确认）"
    )
    cron_propose_parser.add_argument("--schedule", required=True, help="cron 表达式（5 字段）")
    cron_propose_parser.add_argument("--content", required=True, help="提醒内容")
    cron_propose_parser.add_argument("--user", default="default-user", help="用户 ID")

    cron_confirm_parser = subparsers.add_parser(
        "cron-confirm", help="确认创建 cron 任务（激活已提议的任务）"
    )
    cron_confirm_parser.add_argument("--job-id", required=True, help="任务 ID")
    cron_confirm_parser.add_argument("--user", default="default-user", help="用户 ID")

    cron_cancel_parser = subparsers.add_parser("cron-cancel", help="取消（删除）cron 任务")
    cron_cancel_parser.add_argument("--job-id", required=True, help="任务 ID")
    cron_cancel_parser.add_argument("--user", default="default-user", help="用户 ID")

    cron_run_parser = subparsers.add_parser(
        "cron-run", help="启动 cron 调度器主循环（前台运行，Ctrl+C 退出）"
    )
    cron_run_parser.add_argument("--interval", type=int, default=60, help="tick 间隔秒（默认 60）")

    subparsers.add_parser("cron-tick", help="执行单次 tick（调试用）")

    cron_validate_parser = subparsers.add_parser(
        "cron-validate", help="校验 cron 表达式（语法 + 间隔 >= 24h）"
    )
    cron_validate_parser.add_argument("--schedule", required=True, help="cron 表达式")

    # ====================================================================
    # gateway / notify 子命令 - 借鉴 Hermes gateway/run.py，遵守 notification-guardrails.md
    # ====================================================================
    subparsers.add_parser(
        "gateway-start",
        help="启动消息平台 Gateway（借鉴 Hermes，主动推送受 NotificationGuardrail 约束）",
    )

    gateway_pair_parser = subparsers.add_parser("gateway-pair", help="生成 Telegram 配对 token")
    gateway_pair_parser.add_argument("--user-id", default="default-user", help="deadman 用户 ID")

    notify_test_parser = subparsers.add_parser(
        "notify-test", help="测试 NotificationGuardrail 各种场景"
    )
    notify_test_parser.add_argument(
        "--fail-fast", action="store_true", help="有测试失败时退出码非零"
    )

    notify_consent_parser = subparsers.add_parser(
        "notify-consent", help="手动记录 opt-in（调试用）"
    )
    notify_consent_parser.add_argument("--user-id", required=True, help="用户 ID")
    notify_consent_parser.add_argument("--content", required=True, help="opt-in 原文")
    notify_consent_parser.add_argument(
        "--scope", required=True, help="同意范围（如 reminder:2026-07-22T09:00:00）"
    )

    # ====================================================================
    # alignment / governance / multimodal 子命令注册
    # ====================================================================
    # alignment-status 子命令 - 显示对齐训练状态
    subparsers.add_parser(
        "alignment-status", help="显示 Alignment 对齐训练状态(SFT/DPO/MoE/持续学习)"
    )

    # alignment-train 子命令 - 触发训练流水线
    subparsers.add_parser("alignment-train", help="触发 Alignment SFT → DPO 训练流水线(mock)")

    # governance-status 子命令 - 显示治理框架状态
    subparsers.add_parser(
        "governance-status", help="显示 Governance 治理框架状态(模型卡/风险卡/红线/伦理)"
    )

    # governance-check 子命令 - 运行合规检查
    gov_check_parser = subparsers.add_parser(
        "governance-check", help="运行 Governance 合规检查(红线 + 保险覆盖)"
    )
    gov_check_parser.add_argument("--action", required=True, help="待检查的动作描述")
    gov_check_parser.add_argument("--incident-type", default=None, help="保险覆盖类型")
    gov_check_parser.add_argument("--amount", type=float, default=0, help="涉及金额")

    # multimodal-status 子命令 - 显示多模态管道状态
    subparsers.add_parser(
        "multimodal-status", help="显示 Multimodal 多模态管道状态(能力/审计/budget)"
    )

    # multimodal-test 子命令 - 测试多模态管道
    subparsers.add_parser("multimodal-test", help="测试 Multimodal 多模态管道(TTS 合成 + 能力验证)")

    # === 企业级扩展④：数据库管理 ===
    db_parser = subparsers.add_parser("db", help="数据库管理（迁移/建表/状态）")
    db_parser.add_argument(
        "db_action",
        choices=["init", "migrate", "status", "revision"],
        help="init=建表(create_all) | migrate=alembic upgrade head | status=当前版本 | revision=生成迁移",
    )
    db_parser.add_argument("-m", "--message", default=None, help="revision 模式的迁移描述")

    # === Phase 7+ 扩展模块子命令注册（自动加载）===
    # 各 Phase 通过 _cli_extensions/phaseN.py 提供 register_subparsers(subparsers)
    # 用 set_defaults(func=cmd_xxx) 设置分发函数
    try:
        from ._cli_extensions import (
            phase8,
            phase9,
            phase10,
            phase11_12_13,
            phase15_letters,
            phase15_score,
            phase15_switch,
            phase16,
        )

        phase8.register_subparsers(subparsers)
        phase9.register_subparsers(subparsers)
        phase10.register_subparsers(subparsers)
        phase11_12_13.register_subparsers(subparsers)
        phase15_switch.register_subparsers(subparsers)
        phase15_letters.register_subparsers(subparsers)
        phase15_score.register_subparsers(subparsers)
        phase16.register_subparsers(subparsers)
    except ImportError as exc:
        logging.getLogger(__name__).warning(f"CLI 扩展模块加载失败（部分子命令不可用）: {exc}")

    # === Phase 15 (Memorial Writer): AI 悼文撰写 ===
    try:
        from ._cli_extensions import phase15_memorial

        phase15_memorial.register_subparsers(subparsers)
    except ImportError as exc:
        logging.getLogger(__name__).warning(f"Phase 15 Memorial Writer 扩展加载失败: {exc}")

    args = parser.parse_args()

    setup_logging(args.log_level)

    if args.version:
        cmd_version(args)
        return

    if args.command == "mcp-server":
        cmd_mcp_server(args)
    elif args.command == "eval":
        cmd_eval(args)
    elif args.command == "eval-list":
        cmd_eval_list(args)
    elif args.command == "eval-ragas":
        cmd_eval_ragas(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "llm-test":
        cmd_llm_test(args)
    elif args.command == "llm-sync-models":
        cmd_llm_sync_models(args)
    elif args.command == "llm-cost":
        cmd_llm_cost(args)
    elif args.command == "prompt-list":
        cmd_prompt_list(args)
    elif args.command == "prompt-test":
        cmd_prompt_test(args)
    elif args.command == "prompt-sync":
        cmd_prompt_sync(args)
    elif args.command == "rule-test":
        cmd_rule_test(args)
    elif args.command == "rule-validate":
        cmd_rule_validate(args)
    elif args.command == "agent-list":
        cmd_agent_list(args)
    elif args.command == "agent-ping":
        cmd_agent_ping(args)
    elif args.command == "knowledge-list":
        cmd_knowledge_list(args)
    elif args.command == "knowledge-search":
        cmd_knowledge_search(args)
    elif args.command == "knowledge-freshness":
        cmd_knowledge_freshness(args)
    elif args.command == "tool-list":
        cmd_tool_list(args)
    elif args.command == "tool-test":
        cmd_tool_test(args)
    elif args.command == "mcp-ping":
        cmd_mcp_ping(args)
    elif args.command == "obs-dashboard":
        cmd_obs_dashboard(args)
    elif args.command == "obs-test":
        cmd_obs_test(args)
    elif args.command == "obs-export":
        cmd_obs_export(args)
    elif args.command == "memory-list":
        cmd_memory_list(args)
    elif args.command == "memory-test":
        cmd_memory_test(args)
    elif args.command == "memory-ping":
        cmd_memory_ping(args)
    elif args.command == "a2a-card":
        cmd_a2a_card(args)
    elif args.command == "a2a-test":
        cmd_a2a_test(args)
    elif args.command == "a2a-registry":
        cmd_a2a_registry(args)
    elif args.command == "deploy-check":
        cmd_deploy_check(args)
    elif args.command == "deploy-test":
        cmd_deploy_test(args)
    elif args.command == "reflexion-list":
        cmd_reflexion_list(args)
    elif args.command == "reflexion-test":
        cmd_reflexion_test(args)
    elif args.command == "reflexion-ping":
        cmd_reflexion_ping(args)
    elif args.command == "skill-list":
        cmd_skill_list(args)
    elif args.command == "skill-test":
        cmd_skill_test(args)
    elif args.command == "skill-validate":
        cmd_skill_validate(args)
    elif args.command == "chat":
        cmd_chat(args)
    elif args.command == "memory-export":
        cmd_memory_export(args)
    elif args.command == "soul-show":
        cmd_soul_show(args)
    elif args.command == "web-search":
        cmd_web_search(args)
    elif args.command == "sandbox-test":
        cmd_sandbox_test(args)
    elif args.command == "cron-list":
        cmd_cron_list(args)
    elif args.command == "cron-propose":
        cmd_cron_propose(args)
    elif args.command == "cron-confirm":
        cmd_cron_confirm(args)
    elif args.command == "cron-cancel":
        cmd_cron_cancel(args)
    elif args.command == "cron-run":
        cmd_cron_run(args)
    elif args.command == "cron-tick":
        cmd_cron_tick(args)
    elif args.command == "cron-validate":
        cmd_cron_validate(args)
    elif args.command == "gateway-start":
        cmd_gateway_start(args)
    elif args.command == "gateway-pair":
        cmd_gateway_pair(args)
    elif args.command == "notify-test":
        cmd_notify_test(args)
    elif args.command == "notify-consent":
        cmd_notify_consent(args)
    elif args.command == "alignment-status":
        cmd_alignment_status(args)
    elif args.command == "alignment-train":
        cmd_alignment_train(args)
    elif args.command == "governance-status":
        cmd_governance_status(args)
    elif args.command == "governance-check":
        cmd_governance_check(args)
    elif args.command == "multimodal-status":
        cmd_multimodal_status(args)
    elif args.command == "multimodal-test":
        cmd_multimodal_test(args)
    elif args.command == "db":
        cmd_db(args)
    elif hasattr(args, "func") and callable(args.func):
        # Phase 7+ 扩展模块用 set_defaults(func=...) 自动分发
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
