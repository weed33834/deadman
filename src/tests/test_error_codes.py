"""统一错误码体系测试 —— deep-spec 21（DM-模块-序号 三段式 + 结构化错误返回）"""

from __future__ import annotations

from deadman.errors import DeadmanError, DeadmanHTTPException, ErrorRegistry


class TestErrorRegistry:
    def test_has_defaults(self):
        codes = ErrorRegistry.all()
        assert len(codes) >= 20
        assert "DM-PROMPT-4040" in {c["code"] for c in codes}

    def test_get_registered(self):
        ec = ErrorRegistry.get("DM-TOOL-4040")
        assert ec is not None and ec.http_status == 404

    def test_get_unknown(self):
        assert ErrorRegistry.get("DM-NOPE-9999") is None


class TestDeadmanError:
    def test_structured_dict(self):
        e = DeadmanError("DM-PROMPT-4040")
        d = e.to_dict("rid-1")
        assert d["code"] == "DM-PROMPT-4040"
        assert d["error"] == "DM-PROMPT-4040"
        assert d["message"] == "提示词不存在"
        assert d["severity"] == "error"
        assert d["request_id"] == "rid-1"

    def test_http_status_from_code(self):
        assert DeadmanError("DM-AUTH-4010").http_status == 401
        assert DeadmanError("DM-INTERNAL-5000").http_status == 500

    def test_unknown_code_falls_back_500(self):
        e = DeadmanError("DM-NOPE-9999", message="自定义")
        assert e.http_status == 500
        assert e.message == "自定义"

    def test_http_exception_subclass(self):
        e = DeadmanHTTPException("DM-VOICE-4150")
        assert isinstance(e, DeadmanError)
        assert e.http_status == 415
