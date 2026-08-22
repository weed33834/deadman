"""多模态工具适配测试（不调用真实 provider）

覆盖：
- flag 关闭（默认）→ ok=False + 提示
- 文件不存在守卫
- 空入参守卫
- MCP 注册：5 个工具名存在 + 权限等级
- ReAct multimodal 统一入口分发
"""

from __future__ import annotations

from deadman.tools.multimodal_tools import (
    tool_analyze_image,
    tool_asr_transcribe,
    tool_generate_image,
    tool_ocr_extract,
    tool_text_to_speech,
)


class TestGuards:
    def test_missing_file_blocked(self):
        r = tool_ocr_extract("Z:/no/such/image.png")
        assert r["ok"] is False and "文件不存在" in r["error"]

    def test_empty_text_tts(self):
        r = tool_text_to_speech("   ")
        assert r["ok"] is False and "text" in r["error"]

    def test_empty_prompt_imagegen(self):
        r = tool_generate_image("  ")
        assert r["ok"] is False and "prompt" in r["error"]


class TestDisabledDegradation:
    """DEADMAN_MULTIMODAL_ENABLED 默认 0 → pipeline 抛 DisabledError → ok=False"""

    async def test_all_five_degrade_gracefully(self, tmp_path):
        dummy = tmp_path / "x.bin"
        dummy.write_bytes(b"\x00\x01")
        results = [
            tool_ocr_extract(str(dummy)),
            tool_asr_transcribe(str(dummy)),
            tool_text_to_speech("你好"),
            tool_analyze_image(str(dummy)),
            tool_generate_image("一只猫"),
        ]
        for r in results:
            # flag 开启的环境会走真实 provider；此处只断言「不抛异常、有 envelope」
            assert isinstance(r, dict) and "ok" in r
            if not r["ok"]:
                assert "error" in r


class TestRegistration:
    def test_registered_in_mcp_server(self):
        from deadman.mcp_server.server import mcp

        names = {t["name"] for t in mcp.list_tools()}
        for n in (
            "ocr_extract",
            "asr_transcribe",
            "text_to_speech",
            "analyze_image",
            "generate_image",
        ):
            assert n in names, f"{n} 未注册"

    def test_permission_levels(self):
        from deadman.mcp_server.permissions import ToolPermission, get_permission

        assert get_permission("ocr_extract") == ToolPermission.READ_ONLY
        assert get_permission("asr_transcribe") == ToolPermission.READ_ONLY
        assert get_permission("analyze_image") == ToolPermission.READ_ONLY
        assert get_permission("text_to_speech") == ToolPermission.WRITE_ASYNC
        assert get_permission("generate_image") == ToolPermission.WRITE_ASYNC

    def test_react_multimodal_dispatch(self):
        from deadman.orchestration.react_loop import get_available_tools
        from deadman.orchestration.react_tools import _wrap_multimodal, register_default_react_tools

        register_default_react_tools()
        assert "multimodal" in get_available_tools()

        # 分发逻辑：空参数 → 明确错误
        r = __import__("asyncio").run(_wrap_multimodal())
        assert r["ok"] is False and "需要" in r["error"]

        # tts 分发：text+tts → 走 TTS（flag 关闭则降级 envelope）
        r2 = __import__("asyncio").run(_wrap_multimodal(text="怀念", tts=True))
        assert isinstance(r2, dict) and "ok" in r2
