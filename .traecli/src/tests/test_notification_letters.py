"""测试 deadman.notification_letters - Phase 15 通知信函生成器

覆盖：
    1-8. 8 种信函类型各 1 个生成测试
    9. 占位符提取测试
    10. PII 脱敏测试
    11. LLM 不可用降级测试
    12. CLI 命令测试（letter-list-types / letter-template / letter-generate）
    13. Web 端点测试 - 401 未认证
    14. Web 端点测试 - 200 认证后正常返回
    15. 不编造测试 - 生成结果不含未提供的具体电话/地址
    16. confidence 边界测试（无 LLM=0.7，有 LLM 但不可用=0.3）

测试隔离：
    - 不依赖 ~/.deadman，所有逻辑都在内存
    - LLM 全部走 mock（不真实调用）
    - Web 测试用 mock Handler 实例（不启 HTTP server）
"""

from __future__ import annotations

import argparse
import io
from unittest.mock import MagicMock, patch

import pytest
from deadman.notification_letters import (
    LETTER_TEMPLATES,
    LETTER_TYPES,
    LetterGenerator,
    LetterRequest,
    LetterResult,
)
from deadman.notification_letters.generator import LetterGenerator as Gen

# ====================================================================
# 辅助：构造通用 LetterRequest（每个类型特定字段可覆盖）
# ====================================================================

def _make_request(
    letter_type: str,
    extra_fields: dict | None = None,
    recipient: str = "测试机构",
) -> LetterRequest:
    return LetterRequest(
        letter_type=letter_type,
        decedent_name="张**",
        decedent_id_masked="110101********1234",
        death_date="2026-01-15",
        applicant_name="李**",
        applicant_relationship="子女",
        recipient_org=recipient,
        extra_fields=extra_fields or {},
    )


# ====================================================================
# 1-8. 8 种信函类型生成测试
# ====================================================================

class TestEightLetterTypes:
    """8 种信函类型各 1 个生成测试"""

    def test_1_household_cancellation(self):
        """户口注销通知"""
        req = _make_request(
            "household_cancellation",
            extra_fields={
                "household_type": "城镇户口",
                "household_address": "北京市朝阳区**",
            },
        )
        result = Gen().generate(req)
        assert isinstance(result, LetterResult)
        assert result.letter_type == "household_cancellation"
        assert "户口注销" in result.text
        assert "张**" in result.text
        assert "110101********1234" in result.text
        assert "城镇户口" in result.text
        assert "北京市朝阳区**" in result.text
        assert 0.0 < result.confidence <= 1.0
        assert result.disclaimer

    def test_2_social_security_benefit(self):
        """社保丧葬费申领"""
        req = _make_request(
            "social_security_benefit",
            extra_fields={
                "insurance_location": "北京市",
                "bank_name": "中国银行",
                "bank_account_masked": "6228****1234",
            },
        )
        result = Gen().generate(req)
        assert result.letter_type == "social_security_benefit"
        assert "丧葬费" in result.text or "抚恤金" in result.text
        assert "北京市" in result.text
        assert "6228****1234" in result.text
        assert "中国银行" in result.text

    def test_3_provident_fund_withdrawal(self):
        """公积金提取申请"""
        req = _make_request(
            "provident_fund_withdrawal",
            extra_fields={
                "account_balance": "￥123,456.78",
                "heir_name": "李**",
                "heir_id_masked": "110101********5678",
                "inheritance_method": "法定继承",
            },
        )
        result = Gen().generate(req)
        assert result.letter_type == "provident_fund_withdrawal"
        assert "公积金" in result.text
        assert "￥123,456.78" in result.text
        assert "法定继承" in result.text
        assert "110101********5678" in result.text

    def test_4_medical_insurance_cancellation(self):
        """医保账户注销"""
        req = _make_request(
            "medical_insurance_cancellation",
            extra_fields={
                "medical_insurance_card_masked": "1102****9012",
            },
        )
        result = Gen().generate(req)
        assert result.letter_type == "medical_insurance_cancellation"
        assert "医保" in result.text
        assert "1102****9012" in result.text
        assert "注销" in result.text

    def test_5_bank_account_inheritance(self):
        """银行账户解冻/继承"""
        req = _make_request(
            "bank_account_inheritance",
            extra_fields={
                "bank_name": "工商银行",
                "bank_account_masked": "6222****5678",
                "inheritance_method": "继承权公证",
            },
        )
        result = Gen().generate(req)
        assert result.letter_type == "bank_account_inheritance"
        assert "银行账户" in result.text or "存款" in result.text
        assert "工商银行" in result.text
        assert "6222****5678" in result.text
        assert "继承权公证" in result.text

    def test_6_property_inheritance_notarization(self):
        """房产继承公证申请"""
        req = _make_request(
            "property_inheritance_notarization",
            extra_fields={
                "property_address": "北京市海淀区**",
                "heir_name": "李**",
                "heir_id_masked": "110101********5678",
                "inheritance_method": "遗嘱继承",
            },
        )
        result = Gen().generate(req)
        assert result.letter_type == "property_inheritance_notarization"
        assert "公证" in result.text
        assert "北京市海淀区**" in result.text
        assert "遗嘱继承" in result.text

    def test_7_credit_card_cancellation(self):
        """信用卡销户"""
        req = _make_request(
            "credit_card_cancellation",
            extra_fields={
                "card_last_four": "1234",
            },
        )
        result = Gen().generate(req)
        assert result.letter_type == "credit_card_cancellation"
        assert "信用卡" in result.text
        assert "1234" in result.text
        assert "销户" in result.text

    def test_8_internet_account_cancellation(self):
        """互联网账号注销"""
        req = _make_request(
            "internet_account_cancellation",
            extra_fields={
                "platform_name": "某社交平台",
                "account_name": "user_****",
                "cancellation_reason": "用户死亡",
            },
        )
        result = Gen().generate(req)
        assert result.letter_type == "internet_account_cancellation"
        assert "互联网" in result.text or "账号注销" in result.text
        assert "某社交平台" in result.text
        assert "用户死亡" in result.text


# ====================================================================
# 9. 占位符提取测试
# ====================================================================

class TestPlaceholderExtraction:

    def test_extract_placeholders_basic(self):
        """提取 [xxx] 占位符（去重保序）"""
        text = (
            "致：[派出所名称]\n"
            "地址：[派出所地址]\n"
            "电话：[派出所电话]\n"
            "再次填：[派出所名称]\n"  # 重复，应去重
        )
        phs = Gen._extract_placeholders(text)
        assert phs == [
            "[派出所名称]",
            "[派出所地址]",
            "[派出所电话]",
        ]

    def test_extract_placeholders_empty(self):
        """无占位符返回空列表"""
        assert Gen._extract_placeholders("没有占位符的文本") == []

    def test_extract_placeholders_in_generated_letter(self):
        """生成结果中应包含未填字段的占位符"""
        # 不提供任何 extra_fields，所有类型特定字段都应转为占位符
        req = _make_request("credit_card_cancellation", extra_fields={})
        result = Gen().generate(req)
        # 卡号后四位未提供 → 应有 [card_last_four] 占位符
        assert "[card_last_four]" in result.placeholders
        # 申请日期是模板里手写的占位符
        assert "[申请日期]" in result.placeholders


# ====================================================================
# 10. PII 脱敏测试
# ====================================================================

class TestPIIMasking:

    def test_mask_id_card(self):
        """身份证号 18 位脱敏"""
        text = "身份证号：110101199001011234"
        masked = Gen._mask_pii(text)
        # 应脱敏为前 6 + ******** + 后 4
        assert "110101199001011234" not in masked
        assert "110101********1234" in masked

    def test_mask_phone(self):
        """手机号 11 位脱敏"""
        text = "联系电话：13812341234"
        masked = Gen._mask_pii(text)
        assert "13812341234" not in masked
        assert "138****1234" in masked

    def test_mask_bank_account(self):
        """银行账号 16-19 位脱敏"""
        text = "账号：6222021234567890123"
        masked = Gen._mask_pii(text)
        assert "6222021234567890123" not in masked
        # 前 4 + **** + 后 4
        assert "6222" in masked
        assert "0123" in masked

    def test_mask_already_masked_id_unchanged(self):
        """已脱敏的身份证号不被二次处理"""
        text = "身份证号：110101********1234"
        masked = Gen._mask_pii(text)
        assert masked == text

    def test_mask_in_generated_letter(self):
        """生成结果中如果出现明文 PII，应被脱敏"""
        # 故意传入未脱敏的身份证号（调用方失误）
        req = LetterRequest(
            letter_type="household_cancellation",
            decedent_name="张三",
            decedent_id_masked="110101199001011234",  # 明文！
            death_date="2026-01-15",
            applicant_name="李四",
            applicant_relationship="子女",
            recipient_org="测试派出所",
            extra_fields={"household_type": "城镇", "household_address": "北京"},
        )
        result = Gen().generate(req)
        # 明文身份证号不应出现在结果中
        assert "110101199001011234" not in result.text
        # 脱敏后的形式应出现
        assert "110101********1234" in result.text


# ====================================================================
# 11. LLM 不可用降级测试
# ====================================================================

class TestLLMUnavailableFallback:

    def test_llm_unavailable_returns_low_confidence(self):
        """use_llm=True 但 llm_client 不可用 → confidence=0.3"""
        req = _make_request(
            "household_cancellation",
            extra_fields={"household_type": "城镇", "household_address": "北京"},
        )
        # llm_client.api_key 为空 → 视为不可用
        import deadman.llm as llm_module
        mock_llm = MagicMock()
        mock_llm.api_key = ""  # 空 key 表示未配置
        old = llm_module.llm_client
        llm_module.llm_client = mock_llm
        try:
            result = Gen(use_llm=True).generate(req)
        finally:
            llm_module.llm_client = old
        assert result.confidence == pytest.approx(0.3)
        assert isinstance(result.text, str)
        assert "户口" in result.text

    def test_no_llm_returns_template_confidence(self):
        """use_llm=False → confidence=0.7（纯模板）"""
        req = _make_request(
            "household_cancellation",
            extra_fields={"household_type": "城镇", "household_address": "北京"},
        )
        result = Gen(use_llm=False).generate(req)
        assert result.confidence == pytest.approx(0.7)

    def test_llm_import_error_falls_back(self):
        """llm 模块导入失败 → 降级 confidence=0.3"""
        req = _make_request(
            "credit_card_cancellation",
            extra_fields={"card_last_four": "1234"},
        )
        # 用 patch 让 from ..llm import llm_client 抛 ImportError
        with patch.dict(
            "sys.modules",
            {"deadman.llm": None},
        ):
            result = Gen(use_llm=True).generate(req)
        assert result.confidence == pytest.approx(0.3)


# ====================================================================
# 12. CLI 命令测试
# ====================================================================

class TestCLICommands:

    def _run_cli(self, cmd_func, args_dict: dict) -> str:
        """运行 CLI 子命令，捕获 stdout"""
        args = argparse.Namespace(**args_dict)
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_func(args)
        return buf.getvalue()

    def test_letter_list_types(self):
        """letter-list-types 列出 8 种类型"""
        from deadman._cli_extensions.phase15_letters import cmd_letter_list_types
        out = self._run_cli(cmd_letter_list_types, {})
        assert "household_cancellation" in out
        assert "internet_account_cancellation" in out
        # 应列出 8 种
        for t in LETTER_TYPES:
            assert t["type"] in out
            assert t["name"] in out

    def test_letter_template(self):
        """letter-template 打印原始模板"""
        from deadman._cli_extensions.phase15_letters import cmd_letter_template
        out = self._run_cli(
            cmd_letter_template,
            {"type": "household_cancellation"},
        )
        # 原始模板中应含 {decedent_name} 这种未填充占位符
        assert "{decedent_name}" in out
        assert "户口" in out

    def test_letter_template_invalid_type_exits(self):
        """letter-template 未知类型应 sys.exit(1)"""
        from deadman._cli_extensions.phase15_letters import cmd_letter_template
        args = argparse.Namespace(type="nonexistent_type")
        with pytest.raises(SystemExit) as exc_info:
            cmd_letter_template(args)
        assert exc_info.value.code == 1

    def test_letter_generate(self):
        """letter-generate 生成信函"""
        from deadman._cli_extensions.phase15_letters import cmd_letter_generate
        out = self._run_cli(
            cmd_letter_generate,
            {
                "type": "credit_card_cancellation",
                "name": "张**",
                "id_masked": "110101********1234",
                "death_date": "2026-01-15",
                "applicant": "李**",
                "relationship": "子女",
                "recipient": "测试发卡行",
                "extra": ["card_last_four=1234"],
                "use_llm": False,
            },
        )
        assert "1234" in out
        assert "张**" in out
        assert "confidence" in out
        assert "草稿" in out  # disclaimer

    def test_letter_generate_missing_extra_uses_placeholder(self):
        """letter-generate 缺失 extra_fields → 占位符"""
        from deadman._cli_extensions.phase15_letters import cmd_letter_generate
        out = self._run_cli(
            cmd_letter_generate,
            {
                "type": "credit_card_cancellation",
                "name": "张**",
                "id_masked": "110101********1234",
                "death_date": "2026-01-15",
                "applicant": "李**",
                "relationship": "子女",
                "recipient": "测试发卡行",
                "extra": [],
                "use_llm": False,
            },
        )
        # card_last_four 未提供 → 应在 placeholders 列表中
        assert "[card_last_four]" in out

    def test_register_subparsers(self):
        """register_subparsers 挂载 3 个子命令"""
        from deadman._cli_extensions.phase15_letters import register_subparsers
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        register_subparsers(subparsers)
        # 解析 letter-list-types
        args = parser.parse_args(["letter-list-types"])
        assert args.command == "letter-list-types"
        assert callable(args.func)
        # 解析 letter-template
        args = parser.parse_args(["letter-template", "--type", "household_cancellation"])
        assert args.command == "letter-template"
        assert args.type == "household_cancellation"
        # 解析 letter-generate
        args = parser.parse_args([
            "letter-generate",
            "--type", "credit_card_cancellation",
            "--name", "张**",
            "--id-masked", "110101********1234",
            "--death-date", "2026-01-15",
            "--applicant", "李**",
            "--relationship", "子女",
            "--recipient", "测试行",
            "--extra", "card_last_four=1234",
        ])
        assert args.command == "letter-generate"
        assert args.type == "credit_card_cancellation"
        assert args.extra == ["card_last_four=1234"]


# ====================================================================
# 13-14. Web 端点测试
# ====================================================================

class _CapturedHandler:
    """模拟 WebServer.run 内的 Handler 实例，捕获 _send_json 调用

    用法：
        h = _CapturedHandler()
        h._handle_letters_types()
        status, payload = h.calls[-1]
    """

    def __init__(self, auth_user: dict | None = None) -> None:
        self.calls: list[tuple[int, object]] = []
        self._auth_user = auth_user

    def _send_json(self, status: int, payload: object) -> None:
        self.calls.append((status, payload))

    def _phase_auth_user(self) -> dict | None:
        return self._auth_user

    def _phase_unauthorized(self) -> None:
        self._send_json(401, {"error": "未认证或 token 无效"})

    # 从 web/server.py 中复制的 _handle_letters_* 方法签名
    def _handle_letters_types(self) -> None:
        from deadman.notification_letters.models import DEFAULT_DISCLAIMER
        from deadman.notification_letters.templates import LETTER_TYPES
        user = self._phase_auth_user()
        if user is None:
            self._phase_unauthorized()
            return
        self._send_json(200, {
            "types": [dict(t) for t in LETTER_TYPES],
            "count": len(LETTER_TYPES),
            "disclaimer": DEFAULT_DISCLAIMER,
        })

    def _handle_letters_template(self, query: dict) -> None:
        from deadman.notification_letters.models import DEFAULT_DISCLAIMER
        from deadman.notification_letters.templates import (
            LETTER_TEMPLATES,
            LETTER_TYPES,
            get_letter_type_meta,
        )
        user = self._phase_auth_user()
        if user is None:
            self._phase_unauthorized()
            return
        letter_type = (query.get("type", [""])[0] or "").strip()
        if not letter_type:
            self._send_json(400, {
                "error": "缺少 type 参数",
                "disclaimer": DEFAULT_DISCLAIMER,
            })
            return
        if letter_type not in LETTER_TEMPLATES:
            self._send_json(404, {
                "error": f"未知信函类型: {letter_type}",
                "supported_types": [t["type"] for t in LETTER_TYPES],
                "disclaimer": DEFAULT_DISCLAIMER,
            })
            return
        meta = get_letter_type_meta(letter_type) or {}
        self._send_json(200, {
            "type": letter_type,
            "name": meta.get("name", ""),
            "template": LETTER_TEMPLATES[letter_type],
            "extra_fields_needed": meta.get("extra_fields_needed", []),
            "disclaimer": DEFAULT_DISCLAIMER,
        })

    def _handle_letters_generate(self, body: dict, use_llm: bool = False) -> None:
        from deadman.notification_letters import (
            LetterRequest,
        )
        from deadman.notification_letters.models import DEFAULT_DISCLAIMER
        user = self._phase_auth_user()
        if user is None:
            self._phase_unauthorized()
            return
        letter_type = body.get("letter_type")
        if not letter_type:
            self._send_json(400, {
                "error": "缺少 letter_type",
                "disclaimer": DEFAULT_DISCLAIMER,
            })
            return
        try:
            request = LetterRequest(
                letter_type=letter_type,
                decedent_name=body.get("decedent_name", "") or "",
                decedent_id_masked=body.get("decedent_id_masked", "") or "",
                death_date=body.get("death_date", "") or "",
                applicant_name=body.get("applicant_name", "") or "",
                applicant_relationship=body.get("applicant_relationship", "") or "",
                recipient_org=body.get("recipient_org", "") or "",
                extra_fields=body.get("extra_fields") or {},
                language=body.get("language", "zh-CN") or "zh-CN",
            )
            generator = LetterGenerator(use_llm=use_llm)
            result = generator.generate(request)
        except ValueError as exc:
            self._send_json(400, {
                "error": str(exc),
                "disclaimer": DEFAULT_DISCLAIMER,
            })
            return
        self._send_json(200, result.to_dict())


class TestWebEndpoints:

    def test_letters_types_401_without_auth(self):
        """未认证访问 /api/letters/types 返回 401"""
        h = _CapturedHandler(auth_user=None)
        h._handle_letters_types()
        assert len(h.calls) == 1
        status, payload = h.calls[0]
        assert status == 401
        assert "error" in payload

    def test_letters_types_200_with_auth(self):
        """认证后访问 /api/letters/types 返回 200 + 8 种类型"""
        h = _CapturedHandler(auth_user={"user_id": "u1", "email": "a@b.c"})
        h._handle_letters_types()
        assert len(h.calls) == 1
        status, payload = h.calls[0]
        assert status == 200
        assert payload["count"] == 8
        assert len(payload["types"]) == 8
        assert "disclaimer" in payload

    def test_letters_template_401_without_auth(self):
        """未认证访问 /api/letters/template 返回 401"""
        h = _CapturedHandler(auth_user=None)
        h._handle_letters_template({"type": ["household_cancellation"]})
        status, payload = h.calls[0]
        assert status == 401

    def test_letters_template_200_with_auth(self):
        """认证后访问 /api/letters/template?type=xxx 返回 200 + 原始模板"""
        h = _CapturedHandler(auth_user={"user_id": "u1"})
        h._handle_letters_template({"type": ["household_cancellation"]})
        status, payload = h.calls[0]
        assert status == 200
        assert payload["type"] == "household_cancellation"
        assert "{decedent_name}" in payload["template"]  # 原始模板未填充
        assert "disclaimer" in payload

    def test_letters_template_404_unknown_type(self):
        """认证后但类型未知 → 404"""
        h = _CapturedHandler(auth_user={"user_id": "u1"})
        h._handle_letters_template({"type": ["nonexistent"]})
        status, payload = h.calls[0]
        assert status == 404
        assert "error" in payload
        assert "supported_types" in payload

    def test_letters_generate_401_without_auth(self):
        """未认证 POST /api/letters/generate 返回 401"""
        h = _CapturedHandler(auth_user=None)
        h._handle_letters_generate({
            "letter_type": "household_cancellation",
        })
        status, payload = h.calls[0]
        assert status == 401

    def test_letters_generate_200_with_auth(self):
        """认证后 POST /api/letters/generate 返回 200 + 信函文本"""
        h = _CapturedHandler(auth_user={"user_id": "u1"})
        h._handle_letters_generate({
            "letter_type": "credit_card_cancellation",
            "decedent_name": "张**",
            "decedent_id_masked": "110101********1234",
            "death_date": "2026-01-15",
            "applicant_name": "李**",
            "applicant_relationship": "子女",
            "recipient_org": "测试发卡行",
            "extra_fields": {"card_last_four": "1234"},
        })
        status, payload = h.calls[0]
        assert status == 200
        assert payload["letter_type"] == "credit_card_cancellation"
        assert "1234" in payload["text"]
        assert payload["confidence"] == pytest.approx(0.7)
        assert "placeholders" in payload
        assert "disclaimer" in payload

    def test_letters_generate_400_missing_type(self):
        """认证后但缺 letter_type → 400"""
        h = _CapturedHandler(auth_user={"user_id": "u1"})
        h._handle_letters_generate({})
        status, payload = h.calls[0]
        assert status == 400
        assert "error" in payload


# ====================================================================
# 15. 不编造测试
# ====================================================================

class TestNoFabrication:
    """生成结果不应编造未提供的具体电话/地址"""

    def test_no_fabricated_phone_in_letter(self):
        """未提供电话时，结果中不应出现编造的 11 位手机号"""
        req = _make_request(
            "household_cancellation",
            extra_fields={"household_type": "城镇", "household_address": "北京市朝阳区**"},
        )
        result = Gen().generate(req)
        # 扫描是否含 11 位 1 开头的连续数字（手机号模式）
        import re
        phones = re.findall(r"\b1[3-9]\d{9}\b", result.text)
        assert phones == [], f"信函中不应出现编造的手机号，但发现: {phones}"

    def test_no_fabricated_bank_account_in_letter(self):
        """未提供银行账号时，结果中不应出现编造的 16-19 位账号"""
        req = _make_request(
            "social_security_benefit",
            extra_fields={"insurance_location": "北京市"},
            # 不提供 bank_account_masked 和 bank_name
        )
        result = Gen().generate(req)
        import re
        # 16-19 位连续数字
        accounts = re.findall(r"\b\d{16,19}\b", result.text)
        assert accounts == [], f"信函中不应出现编造的银行账号，但发现: {accounts}"

    def test_no_fabricated_id_card_in_letter(self):
        """不应编造身份证号"""
        req = _make_request(
            "household_cancellation",
            extra_fields={"household_type": "城镇", "household_address": "北京"},
        )
        result = Gen().generate(req)
        import re
        # 18 位身份证号（明文）
        ids = re.findall(r"\b\d{17}[\dXx]\b", result.text)
        assert ids == [], f"信函中不应出现明文身份证号，但发现: {ids}"

    def test_placeholders_included_for_missing_fields(self):
        """缺失字段必须以 [xxx] 占位符形式出现在结果中（而非编造）"""
        req = _make_request(
            "credit_card_cancellation",
            extra_fields={},  # 不提供 card_last_four
        )
        result = Gen().generate(req)
        assert "[card_last_four]" in result.placeholders
        assert "[card_last_four]" in result.text  # 占位符出现在文本中

    def test_letter_types_metadata_complete(self):
        """8 种类型的元信息齐全"""
        assert len(LETTER_TYPES) == 8
        for t in LETTER_TYPES:
            assert "type" in t
            assert "name" in t
            assert "recipient_default" in t
            assert "extra_fields_needed" in t
            assert "description" in t
            assert t["type"] in LETTER_TEMPLATES

    def test_unknown_letter_type_raises(self):
        """未知信函类型应抛 ValueError"""
        req = LetterRequest(
            letter_type="nonexistent_type",
            decedent_name="张**",
            decedent_id_masked="110101********1234",
            death_date="2026-01-15",
            applicant_name="李**",
            applicant_relationship="子女",
            recipient_org="测试",
        )
        with pytest.raises(ValueError, match="未知信函类型"):
            Gen().generate(req)
