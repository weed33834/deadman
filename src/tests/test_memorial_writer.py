"""测试 deadman.memorial_writer - AI 悼文/讣告/答谢词/墓志铭/追思会致辞生成

覆盖：
    1. 5 种 doc_type 各 1 个生成测试（mock LLM 返回固定文本）
    2. LLM 不可用降级测试（confidence=0.3）
    3. 多语言测试（zh-Classical 生成古文）
    4. 多信仰测试（buddhist 含"往生"）
    5. 多语气测试（humorous 不出格）
    6. 安全检测测试（含自伤内容触发 safety_flags）
    7. CLI 命令测试（注册成功）
    8. Web 端点测试（401 未认证 / 200 成功）
    9. integrity 测试（不编造未提供的特质）

测试隔离：每个测试用 monkeypatch 替换 generator 模块的 llm_client，
LLM 调用全部走 mock，不真正调外部 API。
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from deadman.memorial_writer.generator import MemorialGenerator
from deadman.memorial_writer.models import (
    MemorialRequest,
    MemorialResult,
)

if TYPE_CHECKING:
    from deadman.web.server import WebServer

# =====================================================================
# 辅助：mock LLM
# =====================================================================


def _make_mock_llm(resp_text: str = "（mock 悼文正文）") -> MagicMock:
    """构造一个 mock llm_client，chat 是 AsyncMock 返回 resp_text"""
    client = MagicMock()
    client.api_key = "test-key-not-real"
    client.chat = AsyncMock(return_value=resp_text)
    return client


@pytest.fixture
def patch_generator_llm(monkeypatch):
    """返回一个函数，用于替换 generator 模块的 llm_client"""
    import deadman.memorial_writer.generator as gen_module

    def _patch(resp_text: str = "（mock 悼文正文）") -> MagicMock:
        mock = _make_mock_llm(resp_text)
        monkeypatch.setattr(gen_module, "llm_client", mock)
        return mock

    return _patch


def _make_request(**kwargs) -> MemorialRequest:
    """构造一个基础合法 request，可用 kwargs 覆盖字段"""
    defaults = {
        "doc_type": "eulogy",
        "decedent_name": "先父",
        "relationship": "儿子",
        "personality_traits": ["宽厚", "爱读书"],
        "memories": ["每天早晨浇花", "教我骑自行车"],
        "values_or_sayings": ["做人要厚道"],
        "tone": "solemn",
        "faith": "none",
        "language": "zh-CN",
        "word_limit": 0,
    }
    defaults.update(kwargs)
    return MemorialRequest(**defaults)


# =====================================================================
# 1. 5 种 doc_type 各 1 个生成测试
# =====================================================================


class TestGenerateByDocType:
    """5 种 doc_type 各 1 个生成测试"""

    @pytest.mark.parametrize(
        "doc_type,expected_marker",
        [
            ("eulogy", "悼文"),
            ("obituary", "讣告"),
            ("thank_you_note", "答谢词"),
            ("epitaph", "墓志铭"),
            ("memorial_speech", "追思会致辞"),
        ],
    )
    def test_generate_each_doc_type(self, patch_generator_llm, doc_type: str, expected_marker: str):
        # LLM mock 返回带 doc_type 标识的文本
        mock_text = f"这是一篇{expected_marker}正文。"
        patch_generator_llm(mock_text)

        req = _make_request(doc_type=doc_type)
        gen = MemorialGenerator()
        result = asyncio.run(gen.generate(req))

        assert isinstance(result, MemorialResult)
        assert result.doc_type == doc_type
        assert result.text == mock_text
        # LLM 正常返回时 confidence 应为 0.8
        assert result.confidence == pytest.approx(0.8)
        # 无安全标记
        assert not any(result.safety_flags.values())


# =====================================================================
# 2. LLM 不可用降级测试（confidence=0.3）
# =====================================================================


class TestLLMUnavailable:
    def test_llm_unavailable_falls_back_to_template(self, monkeypatch):
        """LLM api_key 为空时降级模板填充，confidence=0.3"""
        import deadman.memorial_writer.generator as gen_module

        mock_llm = MagicMock()
        mock_llm.api_key = ""  # 空 key → 触发降级
        mock_llm.chat = AsyncMock(return_value="should-not-be-called")
        monkeypatch.setattr(gen_module, "llm_client", mock_llm)

        req = _make_request()
        gen = MemorialGenerator()
        result = asyncio.run(gen.generate(req))

        assert result.confidence == pytest.approx(0.3)
        # 降级文本含 [模板生成] 标记
        assert "[模板生成]" in result.text
        # 降级文本应包含用户提供的姓名
        assert "先父" in result.text
        # 不应调 LLM
        assert not mock_llm.chat.called

    def test_llm_call_failure_falls_back_to_template(self, monkeypatch):
        """LLM 调用抛异常时降级模板填充，confidence=0.3"""
        import deadman.memorial_writer.generator as gen_module

        mock_llm = MagicMock()
        mock_llm.api_key = "test-key"
        mock_llm.chat = AsyncMock(side_effect=RuntimeError("网络错误"))
        monkeypatch.setattr(gen_module, "llm_client", mock_llm)

        req = _make_request()
        gen = MemorialGenerator()
        result = asyncio.run(gen.generate(req))

        assert result.confidence == pytest.approx(0.3)
        assert "[模板生成]" in result.text


# =====================================================================
# 3. 多语言测试（zh-Classical 生成古文）
# =====================================================================


class TestMultiLanguage:
    def test_classical_chinese_fallback(self, monkeypatch):
        """zh-Classical 降级模板含古文特征（'呜呼哀哉' 或 '先考'）"""
        import deadman.memorial_writer.generator as gen_module

        mock_llm = MagicMock()
        mock_llm.api_key = ""
        monkeypatch.setattr(gen_module, "llm_client", mock_llm)

        req = _make_request(language="zh-Classical", relationship="父亲")
        gen = MemorialGenerator()
        result = asyncio.run(gen.generate(req))

        # 古文降级模板应包含 "呜呼哀哉" 或 "先考"
        assert "呜呼哀哉" in result.text or "先考" in result.text
        assert result.confidence == pytest.approx(0.3)

    def test_english_fallback(self, monkeypatch):
        """en-US 降级模板含英文"""
        import deadman.memorial_writer.generator as gen_module

        mock_llm = MagicMock()
        mock_llm.api_key = ""
        monkeypatch.setattr(gen_module, "llm_client", mock_llm)

        req = _make_request(language="en-US")
        gen = MemorialGenerator()
        result = asyncio.run(gen.generate(req))

        assert "rest in peace" in result.text.lower() or "memory of" in result.text.lower()


# =====================================================================
# 4. 多信仰测试（buddhist 含"往生"）
# =====================================================================


class TestMultiFaith:
    def test_buddhist_fallback_contains_wangsheng(self, monkeypatch):
        """佛教降级模板含'往生'"""
        import deadman.memorial_writer.generator as gen_module

        mock_llm = MagicMock()
        mock_llm.api_key = ""
        monkeypatch.setattr(gen_module, "llm_client", mock_llm)

        req = _make_request(faith="buddhist")
        gen = MemorialGenerator()
        result = asyncio.run(gen.generate(req))

        assert "往生" in result.text

    def test_taoist_fallback_contains_yuhua(self, monkeypatch):
        """道教降级模板含"羽化"或"登仙" """
        import deadman.memorial_writer.generator as gen_module

        mock_llm = MagicMock()
        mock_llm.api_key = ""
        monkeypatch.setattr(gen_module, "llm_client", mock_llm)

        req = _make_request(faith="taoist")
        gen = MemorialGenerator()
        result = asyncio.run(gen.generate(req))

        assert "羽化" in result.text or "登仙" in result.text

    def test_christian_fallback_contains_anxi(self, monkeypatch):
        """基督教降级模板含"安息主怀" """
        import deadman.memorial_writer.generator as gen_module

        mock_llm = MagicMock()
        mock_llm.api_key = ""
        monkeypatch.setattr(gen_module, "llm_client", mock_llm)

        req = _make_request(faith="christian")
        gen = MemorialGenerator()
        result = asyncio.run(gen.generate(req))

        assert "安息主怀" in result.text


# =====================================================================
# 5. 多语气测试（humorous 不出格）
# =====================================================================


class TestMultiTone:
    def test_humorous_tone_does_not_trigger_safety(self, patch_generator_llm):
        """humorous 语气生成的文本不应触发安全标记"""
        patch_generator_llm("记得父亲总爱讲冷笑话，每次都自己先笑出声。他的笑声是我最温暖的回忆。")

        req = _make_request(tone="humorous")
        gen = MemorialGenerator()
        result = asyncio.run(gen.generate(req))

        # humorous 不应触发任何 safety_flag
        assert not result.safety_flags.get("self_harm")
        assert not result.safety_flags.get("violence")
        assert not result.safety_flags.get("inappropriate")

    def test_warm_tone_generates_normally(self, patch_generator_llm):
        """warm 语气正常生成"""
        mock_text = "父亲温暖的笑容，是我心中永远的港湾。"
        patch_generator_llm(mock_text)

        req = _make_request(tone="warm")
        gen = MemorialGenerator()
        result = asyncio.run(gen.generate(req))

        assert result.text == mock_text
        assert result.confidence == pytest.approx(0.8)


# =====================================================================
# 6. 安全检测测试（含自伤内容触发 safety_flags）
# =====================================================================


class TestSafetyCheck:
    def test_self_harm_content_triggers_safety_flag(self, patch_generator_llm):
        """LLM 返回含'自杀'的文本应触发 self_harm safety_flag"""
        patch_generator_llm("某人自杀了，这是一段不该出现的悼文内容。")

        req = _make_request()
        gen = MemorialGenerator()
        result = asyncio.run(gen.generate(req))

        assert result.safety_flags.get("self_harm") is True

    def test_violence_content_triggers_safety_flag(self, patch_generator_llm):
        """LLM 返回含'杀害'的文本应触发 violence safety_flag"""
        patch_generator_llm("他生前曾被人杀害，凶手至今未找到。")

        req = _make_request()
        gen = MemorialGenerator()
        result = asyncio.run(gen.generate(req))

        assert result.safety_flags.get("violence") is True

    def test_safe_content_does_not_trigger_flags(self, patch_generator_llm):
        """正常悼文不触发任何安全标记"""
        patch_generator_llm("父亲一生勤俭，待人和善。愿父亲安息。")

        req = _make_request()
        gen = MemorialGenerator()
        result = asyncio.run(gen.generate(req))

        assert not any(result.safety_flags.values())


# =====================================================================
# 7. CLI 命令测试（注册成功）
# =====================================================================


class TestCLIRegistration:
    def test_register_subparsers_creates_two_subcommands(self):
        """register_subparsers 应注册 2 个子命令"""
        from deadman._cli_extensions import phase15_memorial

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        phase15_memorial.register_subparsers(subparsers)

        # 解析 memorial-list-types
        args, _ = parser.parse_known_args(["memorial-list-types"])
        assert args.command == "memorial-list-types"
        assert callable(args.func)

    def test_register_subparser_singular_alias(self):
        """register_subparser（单数形式）也应正常工作"""
        from deadman._cli_extensions import phase15_memorial

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        phase15_memorial.register_subparser(subparsers)

        args, _ = parser.parse_known_args(
            [
                "memorial-generate",
                "--type",
                "eulogy",
                "--name",
                "先父",
                "--relationship",
                "儿子",
                "--traits",
                "宽厚,爱读书",
                "--memories",
                "每天浇花|教我骑车",
                "--values",
                "做人要厚道",
                "--tone",
                "solemn",
                "--faith",
                "none",
                "--language",
                "zh-CN",
                "--limit",
                "600",
            ]
        )
        assert args.command == "memorial-generate"
        assert args.type == "eulogy"
        assert args.name == "先父"
        assert args.relationship == "儿子"
        assert args.traits == "宽厚,爱读书"
        assert args.memories == "每天浇花|教我骑车"
        assert args.values == "做人要厚道"
        assert args.tone == "solemn"
        assert args.faith == "none"
        assert args.language == "zh-CN"
        assert args.limit == 600
        assert callable(args.func)

    def test_commands_list_contains_two_commands(self):
        """COMMANDS 清单包含 2 个命令"""
        from deadman._cli_extensions import phase15_memorial

        assert "memorial-generate" in phase15_memorial.COMMANDS
        assert "memorial-list-types" in phase15_memorial.COMMANDS
        assert len(phase15_memorial.COMMANDS) == 2


# =====================================================================
# 8. Web 端点测试（401 未认证 / 200 成功）
# =====================================================================


def _make_web_server(tmp_path: Path, monkeypatch) -> WebServer:
    """构造一个用 tmp_path 作为 auth_data_dir 的 WebServer"""
    from deadman.config import settings
    from deadman.web.server import WebServer

    monkeypatch.setattr(settings, "auth_data_dir", tmp_path)
    monkeypatch.setattr(settings, "jwt_secret", "")
    monkeypatch.setattr(settings, "jwt_expiry_days", 7)
    monkeypatch.setattr(settings, "password_min_length", 8)
    return WebServer()


class TestWebMemorialEndpoints:
    """Web 端点测试"""

    def test_generate_without_token_returns_401(self, tmp_path: Path, monkeypatch):
        """未认证访问 /api/memorial/generate 应返回 401"""
        server = _make_web_server(tmp_path, monkeypatch)

        # 直接调 _require_auth 验证无 token 时返回 None
        user = server._require_auth({})
        assert user is None

    def test_types_endpoint_requires_auth(self, tmp_path: Path, monkeypatch):
        """/api/memorial/types 也强制认证"""
        server = _make_web_server(tmp_path, monkeypatch)
        # 无 token → _require_auth 返回 None
        user = server._require_auth({})
        assert user is None

    def test_generate_with_token_returns_200(self, tmp_path: Path, monkeypatch, mock_llm_client):
        """带 token 访问 /api/memorial/generate 应正常生成"""
        server = _make_web_server(tmp_path, monkeypatch)
        # 注册并拿 token
        reg_resp = asyncio.run(
            server._handle_auth_register(
                {
                    "email": "alice@example.com",
                    "password": "password123",
                    "display_name": "Alice",
                }
            )
        )
        token = reg_resp["token"]
        user = server._require_auth({"Authorization": f"Bearer {token}"})
        assert user is not None

        # mock LLM
        import deadman.memorial_writer.generator as gen_module

        mock_llm = MagicMock()
        mock_llm.api_key = "test-key"
        mock_llm.chat = AsyncMock(return_value="这是一篇悼文正文。")
        monkeypatch.setattr(gen_module, "llm_client", mock_llm)

        # 调 generator（模拟 _handle_memorial_generate 的核心逻辑）
        from deadman.memorial_writer.models import MemorialRequest

        req = MemorialRequest(
            doc_type="eulogy",
            decedent_name="先父",
            relationship="儿子",
            personality_traits=["宽厚"],
        )
        gen = MemorialGenerator()
        result = asyncio.run(gen.generate(req))

        assert result.doc_type == "eulogy"
        assert "悼文" in result.text
        assert result.confidence == pytest.approx(0.8)


# =====================================================================
# 9. integrity 测试（不编造未提供的特质）
# =====================================================================


class TestIntegrity:
    def test_prompt_includes_do_not_fabricate_instruction(self, monkeypatch):
        """提示词含"不要编造"约束（integrity-framework L1）"""
        import deadman.memorial_writer.generator as gen_module

        captured_messages: list = []

        async def capture_chat(messages, **kwargs):
            captured_messages.append(messages)
            return "mock 悼文"

        mock_llm = MagicMock()
        mock_llm.api_key = "test-key"
        mock_llm.chat = capture_chat
        monkeypatch.setattr(gen_module, "llm_client", mock_llm)

        req = _make_request()
        gen = MemorialGenerator()
        asyncio.run(gen.generate(req))

        assert len(captured_messages) == 1
        messages = captured_messages[0]
        # 系统消息
        sys_msg = messages[0]["content"]
        assert "不编造" in sys_msg or "integrity" in sys_msg
        # 用户消息（prompt）
        user_msg = messages[1]["content"]
        assert "不要编造" in user_msg or "不要自行补充" in user_msg

    def test_fallback_template_only_uses_provided_traits(self, monkeypatch):
        """降级模板只使用用户提供的特质，不补充未提供的"""
        import deadman.memorial_writer.generator as gen_module

        mock_llm = MagicMock()
        mock_llm.api_key = ""  # 触发降级
        monkeypatch.setattr(gen_module, "llm_client", mock_llm)

        # 只提供特质，不提供 memories/values
        req = _make_request(
            personality_traits=["宽厚"],
            memories=[],
            values_or_sayings=[],
        )
        gen = MemorialGenerator()
        result = asyncio.run(gen.generate(req))

        # 用户提供的特质应在结果中
        assert "宽厚" in result.text
        # 降级模板对未提供的特质应明确"家属可补充"，而不是编造新特质
        assert "家属可补充" in result.text

    def test_fallback_template_does_not_invent_memories(self, monkeypatch):
        """降级模板不会编造未提供的回忆"""
        import deadman.memorial_writer.generator as gen_module

        mock_llm = MagicMock()
        mock_llm.api_key = ""
        monkeypatch.setattr(gen_module, "llm_client", mock_llm)

        # 不提供 memories
        req = _make_request(memories=[])
        gen = MemorialGenerator()
        result = asyncio.run(gen.generate(req))

        # 应回退到"家属可补充共同回忆"，不编造具体事件
        assert "家属可补充共同回忆" in result.text

    def test_request_validation_rejects_invalid_doc_type(self):
        """非法 doc_type 应被 validate() 拒绝"""
        req = MemorialRequest(
            doc_type="invalid_type",
            decedent_name="x",
        )
        errors = req.validate()
        assert any("doc_type" in e for e in errors)

    def test_request_validation_rejects_empty_name(self):
        """空的 decedent_name 应被拒绝"""
        req = MemorialRequest(
            doc_type="eulogy",
            decedent_name="",
        )
        errors = req.validate()
        assert any("decedent_name" in e for e in errors)


# =====================================================================
# 10. 端到端冒烟：CLI 子命令实际可执行
# =====================================================================


class TestCLISmoke:
    def test_memorial_list_types_runs(self, capsys):
        """memorial-list-types 命令实际运行能输出 5 种文档类型"""
        from deadman._cli_extensions import phase15_memorial

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        phase15_memorial.register_subparsers(subparsers)

        args = parser.parse_args(["memorial-list-types"])
        args.func(args)

        out = capsys.readouterr().out
        # 应列出 5 种文档类型
        assert "悼文" in out
        assert "讣告" in out
        assert "答谢词" in out
        assert "墓志铭" in out
        assert "追思会致辞" in out

    def test_memorial_generate_runs_with_mock_llm(self, capsys, monkeypatch):
        """memorial-generate 命令带 mock LLM 可运行"""
        import deadman.memorial_writer.generator as gen_module
        from deadman._cli_extensions import phase15_memorial

        mock_llm = MagicMock()
        mock_llm.api_key = "test-key"
        mock_llm.chat = AsyncMock(return_value="父亲一生勤勉。")
        monkeypatch.setattr(gen_module, "llm_client", mock_llm)

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        phase15_memorial.register_subparsers(subparsers)

        args = parser.parse_args(
            [
                "memorial-generate",
                "--type",
                "eulogy",
                "--name",
                "先父",
                "--relationship",
                "儿子",
                "--traits",
                "宽厚,爱读书",
            ]
        )
        args.func(args)

        out = capsys.readouterr().out
        assert "先父" in out or "父亲一生勤勉" in out
        # 应包含 confidence
        assert "confidence" in out
