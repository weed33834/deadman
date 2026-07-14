"""测试 legacy.mcp_server - MCP Server 工具注册与调用

覆盖点：
  - list_tools 返回 13 个工具
  - call_tool query_knowledge 知识库查询
  - call_tool check_rules 规则校验
"""

from __future__ import annotations



from legacy.mcp_server.server import McpServer, mcp


# =====================================================================
# McpServer - 工具注册
# =====================================================================


class TestMcpServerRegistration:
    """测试 McpServer 工具注册"""

    def test_list_tools_returns_13_tools(self):
        # 全局 mcp 单例应注册了 13 个工具
        tools = mcp.list_tools()
        assert len(tools) == 13

    def test_list_tools_format(self):
        # 每个工具含 name/description/inputSchema
        tools = mcp.list_tools()
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool

    def test_expected_tool_names(self):
        # 13 个工具名齐全
        tools = mcp.list_tools()
        names = {t["name"] for t in tools}
        expected = {
            "query_knowledge", "web_search", "read_file", "write_file",
            "invoke_subagent", "check_integrity", "check_rules",
            "query_memory", "initiate_debate", "call_external_agent",
            "execute_reflexion",
            "init_transfer", "report_incident",
        }
        assert names == expected

    def test_register_tool_custom(self):
        # 自定义工具注册
        server = McpServer("test-server")

        async def handler(**kwargs):
            return {"ok": True}

        server.register_tool(
            name="custom_tool",
            description="自定义工具",
            input_schema={"type": "object"},
            handler=handler,
        )
        tools = server.list_tools()
        assert any(t["name"] == "custom_tool" for t in tools)

    def test_tool_decorator(self):
        # @tool 装饰器注册
        server = McpServer("decorator-test")

        @server.tool(name="decorated", description="装饰器注册", input_schema={})
        async def decorated(**kwargs):
            return {"ok": True}

        tools = server.list_tools()
        assert any(t["name"] == "decorated" for t in tools)


# =====================================================================
# call_tool - query_knowledge
# =====================================================================


class TestCallToolQueryKnowledge:
    """测试 call_tool query_knowledge 知识库查询"""

    async def test_query_knowledge_country_not_found(self):
        # 国家目录不存在 → found=False, needs_research=True
        result = await mcp.call_tool("query_knowledge", {
            "country": "XX",  # 不存在的国家代码
            "topic": "death_certificate",
        })
        assert result["found"] is False
        assert result.get("needs_research") in (True, None)

    async def test_query_knowledge_returns_dict(self):
        # 返回应是 dict
        result = await mcp.call_tool("query_knowledge", {
            "country": "CN",
            "topic": "test",
        })
        assert isinstance(result, dict)

    async def test_query_knowledge_with_region(self):
        # 带 region 参数
        result = await mcp.call_tool("query_knowledge", {
            "country": "CN",
            "topic": "test",
            "region": "beijing",
        })
        assert isinstance(result, dict)

    async def test_query_knowledge_fallback_to_search_false(self):
        # fallback_to_search=False → needs_research=False
        result = await mcp.call_tool("query_knowledge", {
            "country": "XX",
            "topic": "x",
            "fallback_to_search": False,
        })
        assert result["found"] is False
        # fallback_to_search=False 时 needs_research 应为 False
        assert result.get("needs_research") is False

    async def test_query_knowledge_query_mode(self):
        # query_mode 参数透传
        result = await mcp.call_tool("query_knowledge", {
            "country": "CN",
            "topic": "x",
            "query_mode": "hybrid",
        })
        # hybrid 模式但 LightRAG 未启用 → degraded=True
        assert isinstance(result, dict)


# =====================================================================
# call_tool - check_rules
# =====================================================================


class TestCallToolCheckRules:
    """测试 call_tool check_rules 规则校验"""

    async def test_check_rules_clean_text_passes(self):
        # 干净文本 → passed=True
        result = await mcp.call_tool("check_rules", {
            "agent_name": "death_aftercare",
            "output_text": "建议咨询当地医保部门。",
        })
        assert result["passed"] is True
        assert result["violations"] == []
        assert result["safety_triggered"] is False
        assert result["risk_tier"] == "R0"
        assert result["agent_name"] == "death_aftercare"

    async def test_check_rules_detects_fabrication(self):
        # 编造检测 → passed=False
        result = await mcp.call_tool("check_rules", {
            "agent_name": "medical-guide",
            "output_text": "大概7天就能办下来。",
        })
        assert result["passed"] is False
        assert len(result["integrity_violations"]) >= 1

    async def test_check_rules_detects_crisis(self):
        # 心理危机检测 → safety_triggered=True, risk_tier=R3
        result = await mcp.call_tool("check_rules", {
            "agent_name": "death_aftercare",
            "output_text": "我最近不想活了。",
        })
        assert result["safety_triggered"] is True
        assert result["risk_tier"] == "R3"
        assert result["passed"] is False

    async def test_check_rules_detects_r2_signal(self):
        # R2 信号检测 → risk_tier=R2
        result = await mcp.call_tool("check_rules", {
            "agent_name": "legal_advisor",
            "output_text": "这涉及继承争议。",
        })
        assert result["risk_tier"] == "R2"
        assert result["passed"] is False

    async def test_check_rules_with_context(self):
        # 传 context 参数
        result = await mcp.call_tool("check_rules", {
            "agent_name": "death_aftercare",
            "output_text": "普通文本。",
            "context": {"user_input": "用户问题", "risk_tier": "R0"},
        })
        assert isinstance(result, dict)
        assert result["passed"] is True

    async def test_check_rules_returns_required_fields(self):
        # 返回应含 passed/violations/risk_tier/safety_triggered
        result = await mcp.call_tool("check_rules", {
            "agent_name": "x",
            "output_text": "x",
        })
        for key in ["passed", "violations", "risk_tier", "safety_triggered"]:
            assert key in result, f"缺少字段 {key}"


# =====================================================================
# call_tool - 其他工具基础调用
# =====================================================================


class TestCallToolOthers:
    """测试其他工具调用"""

    async def test_call_unknown_tool_returns_error(self):
        # 调用不存在的工具 → 返回错误结构
        result = await mcp.call_tool("non_existent_tool", {})
        assert result["ok"] is False
        assert result["error"] == "ToolNotFound"

    async def test_call_web_search_returns_mock(self):
        # web_search 是 mock 实现
        result = await mcp.call_tool("web_search", {"query": "test"})
        assert isinstance(result, dict)
        assert result.get("mock") is True
        assert result.get("needs_research") is True

    async def test_call_query_memory_available(self):
        # query_memory 调用应返回 dict（memory 模块可用）
        result = await mcp.call_tool("query_memory", {
            "action": "recall",
            "user_id": "u1",
        })
        assert isinstance(result, dict)
        # action 字段应被回显
        assert result.get("action") == "recall"

    async def test_call_execute_reflexion(self):
        # execute_reflexion 调用应返回 dict
        result = await mcp.call_tool("execute_reflexion", {
            "operation_type": "tool",
            "operation_name": "test_op",
            "failure_reason": "test failure",
            "original_input": {},
        })
        assert isinstance(result, dict)


# =====================================================================
# call_tool - 异常处理
# =====================================================================


class TestCallToolErrorHandling:
    """测试 call_tool 异常处理"""

    async def test_tool_exception_caught(self):
        # 工具抛异常应被捕获，返回错误结构
        server = McpServer("error-test")

        @server.tool(name="error_tool", description="会抛错的工具", input_schema={})
        async def error_tool(**kwargs):
            raise ValueError("工具内部错误")

        result = await server.call_tool("error_tool", {})
        assert result["ok"] is False
        assert "error" in result
        assert "ValueError" in result["error"] or "error" in result["error"]

    async def test_call_tool_with_none_arguments(self):
        # arguments=None 应被当作空 dict
        result = await mcp.call_tool("query_memory", None)
        # 缺少必填参数 user_id → 应返回错误或空结果
        assert isinstance(result, dict)


# =====================================================================
# McpServer - 基础行为
# =====================================================================


class TestMcpServerBasics:
    """测试 McpServer 基础行为"""

    def test_init_with_name(self):
        server = McpServer("my-server")
        assert server.name == "my-server"

    def test_init_default_name(self):
        # 全局 mcp 单例名为 legacy-platform
        assert mcp.name == "legacy-platform"

    def test_trace_id_is_string(self):
        # trace_id 应是字符串（uuid）
        assert isinstance(mcp.trace_id, str)
        assert len(mcp.trace_id) > 0

    def test_list_tools_returns_list(self):
        tools = mcp.list_tools()
        assert isinstance(tools, list)

    async def test_call_tool_returns_dict(self):
        # call_tool 始终返回 dict
        result = await mcp.call_tool("web_search", {"query": "x"})
        assert isinstance(result, dict)
