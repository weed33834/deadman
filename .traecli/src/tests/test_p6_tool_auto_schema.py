"""P6: @tool 装饰器自动 schema 生成测试（借鉴 smolagents @tool 模式）

覆盖：
  - _python_type_to_json_schema: 基本类型 / Optional / list[T] / dict / Literal / Union
  - _extract_docstring_description: docstring 首段提取
  - _parse_docstring_args: Google-style Args 段解析（含多行延续）
  - _build_schema_from_signature: 完整 schema 生成（类型 + 描述 + 默认值 + required）
  - McpServer.tool_auto 装饰器: 注册工具 + list_tools 返回自动 schema
  - 与现有手写 schema 的兼容性（report_incident 重构后行为一致）
"""

from __future__ import annotations

from typing import Any, Literal, Optional

# =====================================================================
# _python_type_to_json_schema
# =====================================================================


class TestPythonTypeToJsonSchema:
    """P6: Python type hint → JSON Schema 类型转换"""

    def test_basic_types(self):
        from deadman.mcp_server.server import _python_type_to_json_schema

        assert _python_type_to_json_schema(str) == {"type": "string"}
        assert _python_type_to_json_schema(int) == {"type": "integer"}
        assert _python_type_to_json_schema(float) == {"type": "number"}
        assert _python_type_to_json_schema(bool) == {"type": "boolean"}
        assert _python_type_to_json_schema(list) == {"type": "array"}
        assert _python_type_to_json_schema(dict) == {"type": "object"}

    def test_list_with_type_param(self):
        """list[str] 应生成 items schema"""
        from deadman.mcp_server.server import _python_type_to_json_schema

        result = _python_type_to_json_schema(list[str])
        assert result == {"type": "array", "items": {"type": "string"}}

    def test_list_of_int(self):
        from deadman.mcp_server.server import _python_type_to_json_schema

        result = _python_type_to_json_schema(list[int])
        assert result == {"type": "array", "items": {"type": "integer"}}

    def test_dict_type(self):
        """dict[str, Any] → object（不细分 K/V）"""
        from deadman.mcp_server.server import _python_type_to_json_schema

        result = _python_type_to_json_schema(dict[str, Any])
        assert result == {"type": "object"}

    def test_optional_unwraps_none(self):
        """Optional[str] / str | None → string"""
        from deadman.mcp_server.server import _python_type_to_json_schema

        # Optional[str] (typing.Optional) —— 故意用旧形式测试输入兼容性，与下方 str|None 对照
        result = _python_type_to_json_schema(Optional[str])  # noqa: UP045
        assert result == {"type": "string"}

        # str | None (Python 3.10+ 语法)
        result = _python_type_to_json_schema(str | None)
        assert result == {"type": "string"}

    def test_literal_string_to_enum(self):
        """Literal['a', 'b'] → {type: string, enum: [a, b]}"""
        from deadman.mcp_server.server import _python_type_to_json_schema

        result = _python_type_to_json_schema(Literal["a", "b", "c"])
        assert result == {"type": "string", "enum": ["a", "b", "c"]}

    def test_literal_int_to_enum(self):
        from deadman.mcp_server.server import _python_type_to_json_schema

        result = _python_type_to_json_schema(Literal[1, 2, 3])
        assert result == {"type": "integer", "enum": [1, 2, 3]}

    def test_none_type(self):
        from deadman.mcp_server.server import _python_type_to_json_schema

        assert _python_type_to_json_schema(None) == {"type": "null"}
        assert _python_type_to_json_schema(type(None)) == {"type": "null"}

    def test_unknown_type_falls_back_to_string(self):
        from deadman.mcp_server.server import _python_type_to_json_schema

        class CustomType:
            pass

        result = _python_type_to_json_schema(CustomType)
        assert result == {"type": "string"}


# =====================================================================
# _extract_docstring_description
# =====================================================================


class TestExtractDocstringDescription:
    """P6: docstring 首段提取"""

    def test_simple_docstring(self):
        from deadman.mcp_server.server import _extract_docstring_description

        def fn():
            """这是简短描述。"""

        assert _extract_docstring_description(fn) == "这是简短描述。"

    def test_multiline_first_paragraph(self):
        from deadman.mcp_server.server import _extract_docstring_description

        def fn():
            """首行描述。
            第二行还在首段。

            这是第二段，不应被提取。
            """

        desc = _extract_docstring_description(fn)
        assert "首行描述" in desc
        assert "第二行还在首段" in desc
        assert "第二段" not in desc

    def test_no_docstring(self):
        from deadman.mcp_server.server import _extract_docstring_description

        def fn():
            pass

        assert _extract_docstring_description(fn) == ""


# =====================================================================
# _parse_docstring_args
# =====================================================================


class TestParseDocstringArgs:
    """P6: Google-style Args 段解析"""

    def test_simple_args(self):
        from deadman.mcp_server.server import _parse_docstring_args

        def fn(query: str, max_results: int = 5):
            """简短描述。

            Args:
                query: 搜索查询语句
                max_results: 最大结果数
            """

        result = _parse_docstring_args(fn)
        assert result == {"query": "搜索查询语句", "max_results": "最大结果数"}

    def test_multiline_description(self):
        from deadman.mcp_server.server import _parse_docstring_args

        def fn(query: str):
            """描述。

            Args:
                query: 第一行描述
                    第二行延续
                    第三行延续
            """

        result = _parse_docstring_args(fn)
        assert "第一行描述" in result["query"]
        assert "第二行延续" in result["query"]
        assert "第三行延续" in result["query"]

    def test_no_args_section(self):
        from deadman.mcp_server.server import _parse_docstring_args

        def fn():
            """只有描述，没有 Args 段。"""

        assert _parse_docstring_args(fn) == {}

    def test_no_docstring(self):
        from deadman.mcp_server.server import _parse_docstring_args

        def fn():
            pass

        assert _parse_docstring_args(fn) == {}


# =====================================================================
# _build_schema_from_signature
# =====================================================================


class TestBuildSchemaFromSignature:
    """P6: 完整 schema 生成（类型 + 描述 + 默认值 + required）"""

    def test_basic_schema_generation(self):
        from deadman.mcp_server.server import _build_schema_from_signature

        def fn(query: str, max_results: int = 5):
            """搜索工具。

            Args:
                query: 搜索查询语句
                max_results: 最大结果数
            """

        schema = _build_schema_from_signature(fn)
        assert schema["type"] == "object"
        assert "query" in schema["properties"]
        assert "max_results" in schema["properties"]
        # query 无默认值 → required
        assert "query" in schema["required"]
        # max_results 有默认值 → 不在 required
        assert "max_results" not in schema["required"]
        # 默认值填入
        assert schema["properties"]["max_results"]["default"] == 5
        # 描述来自 docstring
        assert schema["properties"]["query"]["description"] == "搜索查询语句"

    def test_types_correctly_mapped(self):
        from deadman.mcp_server.server import _build_schema_from_signature

        def fn(
            text: str,
            count: int,
            ratio: float,
            enabled: bool,
            tags: list[str],
            metadata: dict[str, Any],
        ):
            """test"""

        schema = _build_schema_from_signature(fn)
        props = schema["properties"]
        assert props["text"]["type"] == "string"
        assert props["count"]["type"] == "integer"
        assert props["ratio"]["type"] == "number"
        assert props["enabled"]["type"] == "boolean"
        assert props["tags"]["type"] == "array"
        assert props["tags"]["items"] == {"type": "string"}
        assert props["metadata"]["type"] == "object"

    def test_optional_params_not_required(self):
        from deadman.mcp_server.server import _build_schema_from_signature

        def fn(required_param: str, optional_param: str | None = None):
            """test"""

        schema = _build_schema_from_signature(fn)
        assert "required_param" in schema["required"]
        assert "optional_param" not in schema["required"]
        # Optional[str] → string 类型
        assert schema["properties"]["optional_param"]["type"] == "string"

    def test_literal_generates_enum(self):
        from deadman.mcp_server.server import _build_schema_from_signature

        def fn(mode: Literal["fast", "slow"] = "fast"):
            """test"""

        schema = _build_schema_from_signature(fn)
        prop = schema["properties"]["mode"]
        assert prop["type"] == "string"
        assert prop["enum"] == ["fast", "slow"]
        assert prop["default"] == "fast"

    def test_self_param_skipped(self):
        from deadman.mcp_server.server import _build_schema_from_signature

        class MyClass:
            def method(self, query: str):
                """test"""

        schema = _build_schema_from_signature(MyClass.method)
        assert "self" not in schema["properties"]
        assert "query" in schema["properties"]


# =====================================================================
# McpServer.tool_auto 装饰器
# =====================================================================


class TestMcpServerToolAuto:
    """P6: McpServer.tool_auto 装饰器端到端"""

    def test_tool_auto_registers_with_auto_schema(self):
        from deadman.mcp_server.server import McpServer

        server = McpServer(name="test-server")

        @server.tool_auto(name="test_search", description="测试搜索工具")
        async def test_search(query: str, max_results: int = 5) -> dict[str, Any]:
            """测试搜索工具。

            Args:
                query: 搜索查询语句
                max_results: 最大结果数，默认 5
            """
            return {"results": []}

        # 工具已注册
        assert "test_search" in server._tools
        tool = server._tools["test_search"]
        assert tool.name == "test_search"
        assert tool.description == "测试搜索工具"

        # input_schema 自动生成
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert "query" in schema["properties"]
        assert "max_results" in schema["properties"]
        assert "query" in schema["required"]
        assert "max_results" not in schema["required"]
        assert schema["properties"]["max_results"]["default"] == 5
        assert schema["properties"]["query"]["description"] == "搜索查询语句"

    def test_tool_auto_defaults_name_from_function(self):
        from deadman.mcp_server.server import McpServer

        server = McpServer(name="test-server")

        @server.tool_auto()
        async def my_custom_tool(query: str) -> dict[str, Any]:
            """自定义工具描述。"""

        assert "my_custom_tool" in server._tools
        assert server._tools["my_custom_tool"].description == "自定义工具描述。"

    def test_tool_auto_list_tools_returns_schema(self):
        from deadman.mcp_server.server import McpServer

        server = McpServer(name="test-server")

        @server.tool_auto(name="auto_tool", description="auto")
        async def auto_tool(x: str, y: int = 10) -> dict[str, Any]:
            """test

            Args:
                x: x 参数
                y: y 参数
            """
            return {}

        tools = server.list_tools()
        target = next(t for t in tools if t["name"] == "auto_tool")
        assert target["description"] == "auto"
        assert target["inputSchema"]["properties"]["x"]["description"] == "x 参数"
        assert target["inputSchema"]["properties"]["y"]["default"] == 10
        assert "x" in target["inputSchema"]["required"]
        assert "y" not in target["inputSchema"]["required"]

    def test_tool_auto_call_tool_works(self):
        """tool_auto 注册的工具能被 call_tool 调用"""
        from deadman.mcp_server.server import McpServer

        server = McpServer(name="test-server")

        @server.tool_auto(name="echo")
        async def echo(message: str) -> dict[str, Any]:
            """echo 工具。

            Args:
                message: 要回显的消息
            """
            return {"echoed": message}

        import asyncio
        result = asyncio.run(server.call_tool("echo", {"message": "hello"}))
        assert result["echoed"] == "hello"


# =====================================================================
# 现有 report_incident 工具重构后兼容性
# =====================================================================


class TestReportIncidentAutoSchema:
    """P6: report_incident 重构为 tool_auto 后行为不变"""

    def test_report_incident_still_registered(self):
        """report_incident 应仍注册在全局 mcp server 中"""
        from deadman.mcp_server.server import mcp

        assert "report_incident" in mcp._tools

    def test_report_incident_schema_has_enum(self):
        """Literal 类型应生成 enum 约束"""
        from deadman.mcp_server.server import mcp

        tool = mcp._tools["report_incident"]
        schema = tool.input_schema
        # incident_type 是 Literal → enum
        assert "enum" in schema["properties"]["incident_type"]
        assert "injection_attempt" in schema["properties"]["incident_type"]["enum"]
        # severity 也是 Literal → enum
        assert "enum" in schema["properties"]["severity"]
        assert "critical" in schema["properties"]["severity"]["enum"]
        # severity 默认值 medium
        assert schema["properties"]["severity"]["default"] == "medium"

    def test_report_incident_required_correct(self):
        from deadman.mcp_server.server import mcp

        tool = mcp._tools["report_incident"]
        schema = tool.input_schema
        # incident_type + description 必填
        assert "incident_type" in schema["required"]
        assert "description" in schema["required"]
        # severity / user_input / agent_name 有默认值 → 不必填
        assert "severity" not in schema["required"]
        assert "user_input" not in schema["required"]
        assert "agent_name" not in schema["required"]

    def test_report_incident_call_still_works(self):
        """report_incident 调用应正常返回"""
        import asyncio

        from deadman.mcp_server.server import mcp
        result = asyncio.run(mcp.call_tool("report_incident", {
            "incident_type": "injection_attempt",
            "description": "测试注入",
        }))
        assert result["logged"] is True
        assert result["incident_type"] == "injection_attempt"
        assert "incident_id" in result
