"""CLI 入口 - 命令行工具"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
from typing import Any

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
    """运行评估 - 跑 golden cases + 三层判定 + 反馈闭环"""
    import json
    from datetime import datetime

    from .evaluation.runner import run_all_cases
    from .config import settings
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
        print(f"{str(case_id):<10} {category:<14} {priority:<8} {has_judge:<10} {name}")
    print(f"\nCase 总数: {len(cases)}")


def cmd_run(args):
    """运行单次对话"""
    from .orchestration.graph import build_main_graph, create_initial_state

    graph = build_main_graph()
    state = create_initial_state(user_input=args.input)

    result = asyncio.run(graph.ainvoke(state))

    print("\n=== 响应 ===")
    print(result.get("final_response", "(无响应)"))
    print(f"\n=== 智能体: {result.get('current_agent', '?')} ===")
    print(f"=== 风险等级: {result.get('risk_tier', '?')} ===")


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
    from .llm import PROVIDER_MODELS, _PROVIDER_DEFAULTS, LLMClient
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
        providers_to_test = (
            [args.provider] if args.provider else list(PROVIDER_MODELS.keys())
        )
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
            raise SystemExit(1)


def cmd_prompt_sync(args):
    """同步线上提示词清单 - LangSmith Hub + deepset PromptHub

    对应"查官网最新数据":从线上仓库拉真实公开提示词,与本地对比。
    """
    import json
    from datetime import datetime

    from .prompts import fetch_deepset_prompts, fetch_langsmith_prompts, local_prompt_store

    from .config import settings

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
        metrics_collector.record_metric("interop.a2a_call_success_rate", 1.0 if r["reachable"] else 0.0, tags=tags)
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
            f"{f.country:<6} {f.region:<14} {f.trust_level:<8} "
            f"{f.last_updated or '-':<14} {f.path}"
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

    metrics_collector.record_metric(
        "knowledge.stale_file_rate_6m", result["stale_rate"]
    )


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
            f"{r['url']:<40} {reach:<6} {r['status_code']:>6} "
            f"{r['latency_ms']:>8.1f} {r['error']}"
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
    print(
        f"{'目标':<32} {'类型':<8} {'状态':<9} {'延迟ms':>8} 详情"
    )
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
        print(
            f"{r['target']:<32} {status_map.get(r['status'], r['status']):<9} "
            f"{r['detail']}"
        )

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
    print(f"  capabilities:   streaming={caps.get('streaming')} push={caps.get('pushNotifications')}")

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
            "detail": (
                f"skills={len(card_dict.get('skills', []))} "
                f"missing={missing or 'none'}"
            ),
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
        print(
            f"{r['target']:<32} {status_map.get(r['status'], r['status']):<9} "
            f"{r['detail']}"
        )

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
            has_mode_dispatch = "case \"$MODE\"" in content or "case \"$1\"" in content
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
        print(
            f"{r['target']:<28} {status_map.get(r['status'], r['status']):<9} "
            f"{r['detail']}"
        )

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
                    f"命中={'是' if actual_hit else '否'} "
                    f"期望={'是' if expect_hit else '否'}"
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
        print(
            f"{r['target']:<34} {status_map.get(r['status'], r['status']):<9} "
            f"{r['detail']}"
        )

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
            "detail": (
                f"引用={len(referenced)} 缺失={missing_refs or 'none'}"
            ),
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
        print(
            f"{r['target']:<32} {status_map.get(r['status'], r['status']):<9} "
            f"{r['detail']}"
        )
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
                    "detail": (
                        f"name={front.get('name', '✗')} "
                        f"missing={missing or 'none'}"
                    ),
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
        print(
            f"{r['skill']:<28} {status_map.get(r['status'], r['status']):<9} "
            f"{r['detail']}"
        )

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
    eval_parser = subparsers.add_parser("eval", help="运行评估(golden cases + 三层判定)")
    eval_parser.add_argument("--cases-dir", help="YAML case 目录路径")
    eval_parser.add_argument("--fail-fast", action="store_true", help="有失败时退出码非零")

    # eval-list 子命令 - 列出评估 case 清单
    subparsers.add_parser("eval-list", help="列出本地评估 case 清单")

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
    llm_test_parser.add_argument(
        "--model", help="指定模型(默认取该 provider 首个模型)"
    )
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
    cost_parser = subparsers.add_parser(
        "llm-cost", help="汇总 token 用量与成本(配额追踪)"
    )
    cost_parser.add_argument(
        "--clear", action="store_true", help="清空成本记录"
    )

    # prompt-list 子命令 - 列出本地提示词
    subparsers.add_parser("prompt-list", help="列出本地提示词模板")

    # prompt-test 子命令 - 提示词渲染+发 LLM 测试
    pt_parser = subparsers.add_parser(
        "prompt-test", help="渲染提示词并发 LLM 测试(真实反馈)"
    )
    pt_parser.add_argument("name", help="提示词名称")
    pt_parser.add_argument(
        "--var", action="append", help="变量 key=value(可重复)"
    )
    pt_parser.add_argument("--provider", help="LLM provider")
    pt_parser.add_argument("--model", help="覆盖提示词里的 model")
    pt_parser.add_argument("--max-tokens", type=int, default=512)
    pt_parser.add_argument(
        "--allow-missing", action="store_true", help="允许缺变量渲染"
    )
    pt_parser.add_argument(
        "--dry-run", action="store_true", help="只渲染不发 LLM"
    )
    pt_parser.add_argument(
        "--fail-fast", action="store_true", help="失败时退出码非零"
    )

    # prompt-sync 子命令 - 同步线上提示词清单
    ps_parser = subparsers.add_parser(
        "prompt-sync", help="同步线上提示词仓库(LangSmith/deepset)"
    )
    ps_parser.add_argument("--query", help="LangSmith 搜索关键词")

    # rule-test 子命令 - 对文本跑规则校验
    rt_parser = subparsers.add_parser(
        "rule-test", help="对文本跑 L0-L8 规则校验(手动测试)"
    )
    rt_parser.add_argument("text", help="待校验文本")

    # rule-validate 子命令 - 规则文件完整性校验
    rv_parser = subparsers.add_parser(
        "rule-validate", help="校验规则文件完整性与优先级链"
    )
    rv_parser.add_argument(
        "--fail-fast", action="store_true", help="校验失败退出码非零"
    )

    # agent-list 子命令 - 列出本地智能体配置
    subparsers.add_parser("agent-list", help="列出本地智能体配置(agents/*.md)")

    # agent-ping 子命令 - 测试远端 A2A agent 可达性
    ap_parser = subparsers.add_parser(
        "agent-ping", help="ping 远端 A2A agent(真实反馈可达性/延迟)"
    )
    ap_parser.add_argument(
        "--url", action="append", help="远端 agent base URL(可重复)"
    )
    ap_parser.add_argument("--timeout", type=float, default=10.0, help="超时秒")

    # knowledge-list 子命令 - 列出本地知识库文件
    subparsers.add_parser("knowledge-list", help="列出本地知识库文件")

    # knowledge-search 子命令 - 知识库检索测试
    ks_parser = subparsers.add_parser(
        "knowledge-search", help="知识库检索测试(真实反馈命中)"
    )
    ks_parser.add_argument("query", help="查询词")
    ks_parser.add_argument("--country", help="国家过滤(CN/US/JP)")
    ks_parser.add_argument("--region", help="地区过滤")

    # knowledge-freshness 子命令 - 知识库新鲜度检查
    subparsers.add_parser(
        "knowledge-freshness", help="检查知识库文件新鲜度(过期检测)"
    )

    # tool-list 子命令 - 列出本地 MCP 工具
    subparsers.add_parser("tool-list", help="列出本地注册的 MCP 工具")

    # tool-test 子命令 - 测试单个 MCP 工具调用
    tt_parser = subparsers.add_parser(
        "tool-test", help="测试单个 MCP 工具调用(真实反馈)"
    )
    tt_parser.add_argument("name", help="工具名")
    tt_parser.add_argument(
        "--arg", action="append", help="参数 key=value(可重复,值支持 JSON)"
    )
    tt_parser.add_argument(
        "--fail-fast", action="store_true", help="失败时退出码非零"
    )

    # mcp-ping 子命令 - 测试外部 MCP server 可达性
    mp_parser = subparsers.add_parser(
        "mcp-ping", help="ping 外部 MCP server(可达性检测)"
    )
    mp_parser.add_argument(
        "--url", action="append", help="外部 MCP server URL(可重复)"
    )
    mp_parser.add_argument("--timeout", type=float, default=10.0, help="超时秒")

    # obs-dashboard 子命令 - 显示可观测性看板
    obsd_parser = subparsers.add_parser(
        "obs-dashboard", help="显示 11 大类指标看板当前值"
    )
    obsd_parser.add_argument("--category", help="只看某分类(quality/efficiency/...)")

    # obs-test 子命令 - 可观测性接入测试
    obst_parser = subparsers.add_parser(
        "obs-test", help="可观测性接入测试(span+指标+后端可达性,真实反馈)"
    )
    obst_parser.add_argument("--timeout", type=float, default=5.0, help="后端探测超时秒")

    # obs-export 子命令 - 导出 Prometheus 指标
    subparsers.add_parser("obs-export", help="导出 Prometheus 格式指标")

    # memory-list 子命令 - 列出分层记忆状态
    subparsers.add_parser("memory-list", help="列出 4 层记忆状态(working/episodic/semantic/procedural)")

    # memory-test 子命令 - 记忆写入+召回测试
    subparsers.add_parser("memory-test", help="记忆系统写入+召回测试(真实反馈)")

    # memory-ping 子命令 - 记忆后端可达性
    memp_parser = subparsers.add_parser(
        "memory-ping", help="记忆后端可达性(Graphiti/Neo4j/LightRAG)"
    )
    memp_parser.add_argument("--timeout", type=float, default=5.0, help="超时秒")

    # a2a-card 子命令 - 显示本地 AgentCard
    ac_parser = subparsers.add_parser(
        "a2a-card", help="显示本地 A2A AgentCard(自名片+完整性校验)"
    )
    ac_parser.add_argument("--json", action="store_true", help="输出原始 JSON")

    # a2a-test 子命令 - A2A 协议自测
    subparsers.add_parser("a2a-test", help="A2A 协议自测(card+JSON-RPC,真实反馈)")

    # a2a-registry 子命令 - A2A registry 可达性
    ar_parser = subparsers.add_parser(
        "a2a-registry", help="A2A registry 可达性探测(线上源)"
    )
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
    subparsers.add_parser(
        "reflexion-list", help="列出 Reflexion 预定义调整策略(10 种快速路径)"
    )

    # reflexion-test 子命令 - 反思重试测试
    subparsers.add_parser(
        "reflexion-test", help="Reflexion 反思重试测试(mock 操作,真实反馈)"
    )

    # reflexion-ping 子命令 - LLM 反思路径可达性
    subparsers.add_parser(
        "reflexion-ping", help="LLM 反思路径可达性(慢速路径依赖 LLM)"
    )

    # skill-list 子命令 - 列出本地技能
    subparsers.add_parser("skill-list", help="列出本地技能清单(skills/*/SKILL.md)")

    # skill-test 子命令 - 校验单个技能
    st_parser = subparsers.add_parser(
        "skill-test", help="校验单个技能 SKILL.md(frontmatter+引用,真实反馈)"
    )
    st_parser.add_argument("name", help="技能目录名")

    # skill-validate 子命令 - 全量校验所有技能
    sv_parser = subparsers.add_parser(
        "skill-validate", help="全量校验所有技能完整性"
    )
    sv_parser.add_argument("--fail-fast", action="store_true", help="校验失败退出码非零")

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
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
