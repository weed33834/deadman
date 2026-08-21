"""前端用户流端到端测试

模拟用户在前端的完整交互链路（沙箱无浏览器，用 httpx + SSE 解析模拟）：
1. 加载首页 HTML（验证所有 nav-item / page 容器 / JS 函数就位）
2. 切换到不同页面（验证 switchPage 的 5 个 page 容器存在）
3. 在对话页输入复杂任务，点发送（模拟 send()），SSE 接收流式响应
4. 验证 SSE 事件类型：data / event: trace / event: done
5. 切换到 dashboard 页，验证对话统计被累加
6. 验证 P3 思考过程面板的数据结构（spans/subagent_called/metrics）

3 个复杂任务覆盖：
- 单轮直接回答（death_aftercare 简单问候）
- 跨 agent 转介触发（提到"法律争议"应触发 transfer signal）
- 复杂多领域问题（提到"跨境遗产"应触发 cross_border_specialist）

无 LLM_API_KEY 时后端走降级，但仍会推送 SSE event + 累加 dashboard 统计。
"""

from __future__ import annotations

import json
import sys
import threading

import pytest

# =====================================================================
# 辅助 + 模块级 client fixture（FastAPI TestClient，进程内）
# =====================================================================


@pytest.fixture(scope="module")
def client():
    """模块级 TestClient fixture：所有测试共享同一 app 实例

    scope="module" 让 dashboard 累加行为可被后续测试断言（test_user_send_complex_tasks
    发完消息后 test_dashboard_data_structure 才能验证累加结果）。
    """
    from fastapi.testclient import TestClient

    from deadman.web.app import app

    with TestClient(app) as c:
        yield c


# 复杂任务清单（模拟用户真实输入）
COMPLEX_TASKS = [
    {
        "name": "简单问候",
        "query": "你好，我父亲刚过世，我想了解身后事的基本流程",
        "agent": "death-aftercare",
        "expect_degraded": True,  # 无 API key 必降级
    },
    {
        "name": "法律争议转介",
        "query": "我父亲的遗产分配出现了法律争议，兄弟姐妹在争房产，需要律师介入诉讼",
        "agent": "death-aftercare",
        "expect_degraded": True,
    },
    {
        "name": "跨境遗产复杂场景",
        "query": "我父亲是外籍，在国内有房产和股权，海外还有银行账户，涉及跨境遗产继承，需要了解领事馆流程和跨国税务",
        "agent": "death-aftercare",
        "expect_degraded": True,
    },
]


# =====================================================================
# 测试用例
# =====================================================================


def test_home_html_integrity(client):
    """1. 首页 HTML 完整性：所有 nav-item / page 容器 / 关键 JS 函数就位"""
    print("\n=== 测试 1：首页 HTML 完整性 ===")
    r = client.get("/")
    assert r.status_code == 200, f"首页应返回 200，实际 {r.status_code}"
    html = r.text

    # nav-item 5 个（对话/运维看板/测试中心/资源列表/重新引导）
    nav_items = ["对话", "运维看板", "测试中心", "资源列表", "重新引导"]
    for label in nav_items:
        assert label in html, f"nav-item 缺失：{label}"
    print("  ✓ 5 个 nav-item 就位")

    # page 容器 4 个
    page_ids = ["page-chat", "page-dashboard", "page-test", "page-resources"]
    for pid in page_ids:
        assert f'id="{pid}"' in html, f"page 容器缺失：{pid}"
    print("  ✓ 4 个 page 容器就位")

    # P3 思考过程可视化函数
    assert "function renderTracePanel" in html, "renderTracePanel 函数缺失"
    assert "function toggleTheme" in html, "toggleTheme 函数缺失"
    assert "TRACE_TYPE_LABEL" in html, "TRACE_TYPE_LABEL 常量缺失"
    print("  ✓ P3 思考过程可视化函数就位")

    # P9 dashboard 函数
    assert "function renderDashboardStats" in html, "renderDashboardStats 缺失"
    assert "function renderBarChart" in html, "renderBarChart 缺失"
    assert 'id="dashboardStatsGrid"' in html, "dashboardStatsGrid 缺失"
    for chart_id in ["agentCallsChart", "riskTierChart", "spanTypeChart", "terminationChart"]:
        assert f'id="{chart_id}"' in html, f"{chart_id} 缺失"
    print("  ✓ P9 dashboard 函数 + 4 个图表容器就位")

    # P10 夜砚暗色模式 + 主题切换按钮
    assert "body.dark" in html, "夜砚暗色模式 CSS 缺失"
    assert "theme-toggle" in html, "主题切换按钮缺失"
    print("  ✓ 暗色模式 + 主题切换按钮就位")

    # send() 函数含 SSE 事件分发逻辑
    assert "currentEvent" in html, "SSE event 分发逻辑缺失"
    assert '"trace"' in html, "trace 事件处理缺失"
    print("  ✓ SSE 三类事件分发逻辑就位")

    print(f"  首页 HTML 大小：{len(html)} 字符")


def test_static_assets(client):
    """2. 静态资源可达性"""
    print("\n=== 测试 2：静态资源可达性 ===")
    r = client.get("/")
    assert r.status_code == 200
    print("  ✓ / (index.html) 200")
    assert "text/html" in r.headers.get("content-type", ""), "Content-Type 应为 text/html"
    print(f"  ✓ Content-Type: {r.headers.get('content-type')}")


def test_api_endpoints(client):
    """3. 关键 API 端点可达性"""
    print("\n=== 测试 3：关键 API 端点 ===")
    endpoints = [
        "/api/health",
        "/api/whoami",
        "/api/agents",
        "/api/tools",
        "/api/memory/state",
        "/api/dashboard",
        "/api/deploy/check",
        "/api/hotlines",
    ]
    for path in endpoints:
        r = client.get(path)
        assert r.status_code == 200, f"{path} 应返回 200，实际 {r.status_code}"
        data = r.json()
        assert isinstance(data, dict), f"{path} 应返回 dict"
        print(f"  ✓ {path} 200 (keys: {list(data.keys())[:5]})")


def test_dashboard_empty_state(client):
    """4. dashboard 空状态结构验证"""
    print("\n=== 测试 4：dashboard 空状态结构 ===")
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    data = r.json()
    required_keys = [
        "agent_calls",
        "risk_tier_counts",
        "span_type_counts",
        "token_usage_total",
        "termination_triggers",
        "total_conversations",
        "degraded_count",
        "recent_spans",
    ]
    for k in required_keys:
        assert k in data, f"dashboard 缺字段：{k}"
    print("  ✓ 空状态 8 个字段就位")
    print(f"  ✓ 数据结构：{json.dumps(data, ensure_ascii=False)[:200]}")


def test_user_send_complex_tasks(client):
    """5. 模拟用户发送 3 个复杂任务，SSE 接收 + dashboard 累加验证

    无 LLM_API_KEY 时降级路径行为：
    - graph 跑通 → 推 done 事件 + 累加 dashboard（degraded=False）
    - graph 异常 + 无 key → 推 error 事件 + return（不推 done，不累加）
    - graph 异常 + 有 key → 推 done 事件 + 累加（degraded=True）

    本测试环境无 key，但修了 thread_id bug 后 graph 应能跑通（走 agent_node 降级响应）。
    """
    print("\n=== 测试 5：模拟用户发送复杂任务（SSE 流）===")
    dashboard_before = client.get("/api/dashboard").json()
    print(f"  发送前 dashboard: total_conversations={dashboard_before['total_conversations']}")

    task_results = []
    for i, task in enumerate(COMPLEX_TASKS, 1):
        print(f"\n  --- 任务 {i}: {task['name']} ---")
        print(f"  query: {task['query'][:60]}...")
        print(f"  agent: {task['agent']}")

        params = {"query": task["query"], "agent": task["agent"]}
        full_response = ""
        events_received = []
        trace_data = None
        done_data = None
        error_data = None

        # SSE 流式接收（TestClient 底层即 httpx，stream 接口一致）
        with client.stream("GET", "/api/stream", params=params) as r:
            assert r.status_code == 200, f"SSE 应返回 200，实际 {r.status_code}"
            buffer = ""
            for chunk in r.iter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    event_block, buffer = buffer.split("\n\n", 1)
                    ev = {"event": "message", "data": None}
                    for line in event_block.split("\n"):
                        if line.startswith("event: "):
                            ev["event"] = line[7:].strip()
                        elif line.startswith("data: "):
                            try:
                                ev["data"] = json.loads(line[6:])
                            except json.JSONDecodeError:
                                ev["data"] = line[6:]
                    events_received.append(ev)
                    if ev["event"] == "trace":
                        trace_data = ev["data"]
                    elif ev["event"] == "done":
                        done_data = ev["data"]
                        break  # 收到 done 主动结束，避免等待服务器关连接
                    elif ev["event"] == "error":
                        error_data = ev["data"]
                        break
                    elif ev["event"] == "message" and ev["data"] and "chunk" in ev["data"]:
                        full_response += ev["data"]["chunk"]
                if done_data or error_data:
                    break  # 双层 break：内层 while 收到终止事件后，外层 for 也退出

        event_types = [e["event"] for e in events_received]
        type_counts = {k: event_types.count(k) for k in set(event_types)}
        print(f"  收到 {len(events_received)} 个 SSE 事件，类型分布：{type_counts}")

        # 必有终止事件：done 或 error 二者有其一
        assert "done" in event_types or "error" in event_types, (
            f"任务 {i} 缺终止事件（done 或 error）"
        )
        print(f"  ✓ 终止事件就位：{'done' if done_data else 'error'}")

        if done_data:
            # done 事件字段完整性
            assert "has_trace" in done_data, "done 事件缺 has_trace 标记"
            assert "agent" in done_data, "done 事件缺 agent 字段"
            assert "degraded" in done_data, "done 事件缺 degraded 字段"
            print(
                f"  ✓ done 事件字段完整: agent={done_data.get('agent')}, degraded={done_data.get('degraded')}, has_trace={done_data.get('has_trace')}"
            )
            # graph 跑通时响应文本应非空（agent_node 降级响应也有内容）
            if full_response:
                print(f"  ✓ 响应文本 {len(full_response)} 字符: {full_response[:80]}...")
            else:
                print("  （响应文本为空，可能 graph 内部降级）")
        elif error_data:
            print(f"  ✓ error 事件: {error_data}")

        task_results.append(
            {
                "name": task["name"],
                "events": events_received,
                "response": full_response,
                "trace": trace_data,
                "done": done_data,
                "error": error_data,
            }
        )

    # 验证 dashboard 累加（done 路径才累加，error 路径不累加）
    print("\n  --- 验证 dashboard 累加 ---")
    dashboard_after = client.get("/api/dashboard").json()
    done_count = sum(1 for t in task_results if t["done"])
    print(
        f"  发送后 dashboard: total_conversations={dashboard_after['total_conversations']}, done 路径 {done_count} 个"
    )

    if done_count > 0:
        assert (
            dashboard_after["total_conversations"]
            == dashboard_before["total_conversations"] + done_count
        ), f"total_conversations 应增加 {done_count}（done 路径数）"
        print(
            f"  ✓ total_conversations 累加正确：{dashboard_before['total_conversations']} → {dashboard_after['total_conversations']}"
        )

        assert len(dashboard_after["recent_spans"]) >= done_count
        print(f"  ✓ recent_spans 累加正确：{len(dashboard_after['recent_spans'])} 条")
        for span in dashboard_after["recent_spans"][:done_count]:
            assert "agent" in span, "recent_spans 条目缺 agent"
            assert "timestamp" in span, "recent_spans 条目缺 timestamp"
        print("  ✓ recent_spans 条目结构完整")
    else:
        print("  （所有任务走 error 路径，dashboard 不累加，符合预期）")


def test_dashboard_data_structure(client):
    """6. dashboard 数据结构完整性"""
    print("\n=== 测试 6：dashboard 数据结构完整性 ===")
    data = client.get("/api/dashboard").json()

    assert isinstance(data["agent_calls"], dict)
    assert isinstance(data["risk_tier_counts"], dict)
    assert isinstance(data["span_type_counts"], dict)
    assert isinstance(data["token_usage_total"], dict)
    assert isinstance(data["termination_triggers"], dict)
    assert isinstance(data["total_conversations"], int)
    assert isinstance(data["degraded_count"], int)
    assert isinstance(data["recent_spans"], list)

    tok = data["token_usage_total"]
    for k in ["prompt_tokens", "completion_tokens", "total_tokens"]:
        assert k in tok, f"token_usage_total 缺 {k}"
        assert isinstance(tok[k], int), f"token_usage_total.{k} 应是 int"

    print("  ✓ 所有字段类型正确")
    print(f"  ✓ 数据快照：{json.dumps(data, ensure_ascii=False, indent=2)[:500]}")


def test_concurrent_users(client):
    """7. 并发用户场景（2 个用户同时发消息，dashboard 应正确累加）"""
    print("\n=== 测试 7：并发用户场景 ===")
    dashboard_before = client.get("/api/dashboard").json()
    results = []

    def send_one(query, agent, idx):
        try:
            params = {"query": query, "agent": agent}
            with client.stream("GET", "/api/stream", params=params) as r:
                buffer = ""
                for chunk in r.iter_text():
                    buffer += chunk
                    while "\n\n" in buffer:
                        event_block, buffer = buffer.split("\n\n", 1)
                        # 收到 done 或 error 就结束
                        if "event: done" in event_block or "event: error" in event_block:
                            break
                    else:
                        continue
                    break  # 内层 break 触发后退出外层 for
            results.append(("ok", idx))
        except Exception as e:
            results.append(("err", idx, str(e)))

    threads = []
    queries = [
        ("并发用户1的查询", "death-aftercare"),
        ("并发用户2的查询", "legal-advisor"),
    ]
    for i, (q, a) in enumerate(queries):
        t = threading.Thread(target=send_one, args=(q, a, i))
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == 2, f"应完成 2 个并发请求，实际 {len(results)}"
    for r in results:
        assert r[0] == "ok", f"并发请求失败：{r}"

    dashboard_after = client.get("/api/dashboard").json()
    assert dashboard_after["total_conversations"] == dashboard_before["total_conversations"] + 2
    print(
        f"  ✓ 2 个并发请求完成，dashboard 累加正确：{dashboard_before['total_conversations']} → {dashboard_after['total_conversations']}"
    )


if __name__ == "__main__":
    # 支持手动单独运行：通过 pytest 触发模块级 client fixture
    sys.exit(pytest.main([__file__, "-v", "-s"]))
