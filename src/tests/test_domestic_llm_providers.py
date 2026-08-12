"""国产大模型（通义千问 / DeepSeek / 文心一言）接入测试。

P2 修复点：cost_router 早已路由到 deepseek/qwen，但 llm.py 的
_PROVIDER_DEFAULTS 缺失这两家（及 ernie）的 base_url / env_key，
导致 LLMClient 无法真正实例化 —— 即「已引用但未定义」。

本测试锁定：
  - 三家国产 provider 已登记且 base_url / env_key / sdk 正确
  - 三家均进入 PROVIDER_MODELS 目录（llm-test 可见）
  - LLMClient 在 LLM_API_KEY 为空时，能回退到 provider 专属环境变量取 key
"""

from __future__ import annotations

from deadman.config import settings
from deadman.llm import _PROVIDER_DEFAULTS, PROVIDER_MODELS, LLMClient

EXPECTED = {
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env_key": "DASHSCOPE_API_KEY",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "env_key": "DEEPSEEK_API_KEY",
    },
    "ernie": {
        "base_url": "https://qianfan.baidubce.com/v2",
        "env_key": "QIANFAN_API_KEY",
    },
}


def test_domestic_providers_registered():
    for name, expect in EXPECTED.items():
        assert name in _PROVIDER_DEFAULTS, f"{name} 未登记到 _PROVIDER_DEFAULTS"
        cfg = _PROVIDER_DEFAULTS[name]
        assert cfg["base_url"] == expect["base_url"], f"{name} base_url 错误"
        assert cfg["env_key"] == expect["env_key"], f"{name} env_key 错误"
        # 三家均为 OpenAI 兼容接口
        assert cfg["sdk"] == "openai", f"{name} 应为 openai 兼容"


def test_domestic_providers_in_catalog():
    for name in EXPECTED:
        assert name in PROVIDER_MODELS, f"{name} 未进入 PROVIDER_MODELS"
        assert len(PROVIDER_MODELS[name]) > 0, f"{name} 模型目录为空"


def test_cost_router_deepseek_now_reachable():
    # cost_router 的 ModelChoice("deepseek", "deepseek-chat") 现在能拿到 base_url
    # （修复前此处为 ""，导致请求打到 OpenAI 默认地址而失败）
    assert _PROVIDER_DEFAULTS["deepseek"]["base_url"] == "https://api.deepseek.com"
    assert _PROVIDER_DEFAULTS["qwen"]["base_url"] != ""
    assert _PROVIDER_DEFAULTS["ernie"]["base_url"] != ""


def test_client_falls_back_to_provider_env_key(monkeypatch):
    """LLM_API_KEY 为空时，应按 provider 回退到专属环境变量取 key。"""
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-domestic-deepseek")
    client = LLMClient(provider="deepseek", model="deepseek-chat")
    assert client.api_key == "sk-domestic-deepseek"
    assert client.provider == "deepseek"


def test_client_uses_explicit_api_key_over_env(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-env-qwen")
    client = LLMClient(provider="qwen", model="qwen-max", api_key="sk-explicit")
    assert client.api_key == "sk-explicit"
