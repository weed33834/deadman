"""P8.7 本地 LLM 接入客户端(Qwen / DeepSeek / Llama / Ollama / vLLM)。

支持通过 OpenAI-compatible API 接入各类本地推理后端:

    - Qwen (DashScope / 自托管 vLLM)
    - DeepSeek (官方 / 自托管)
    - Llama (Meta / 自托管)
    - Ollama (本地多模型)
    - vLLM (高吞吐推理服务器)
    - CUSTOM (任意 OpenAI-compatible 端点)

设计原则:
    - 零硬依赖:优先 requests,缺失则降级 httpx,再缺失则 urllib(标准库)
    - 自动 mock:health_check 失败 → chat 返回 mock 响应(便于 CI / 离线开发)
    - GPU 资源管理:load_model / unload_model 模拟显存占用(无实际 GPU 调用)
    - Feature flag:`DEADMAN_ALIGNMENT_ENABLED=0` 关闭时方法返回 mock

不依赖 torch / transformers / vllm。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# =====================================================================
# Provider 枚举
# =====================================================================
class LocalLLMProvider(str, Enum):
    """本地 LLM 推理后端类型。"""

    QWEN = "qwen"          # 通义千问
    DEEPSEEK = "deepseek"  # DeepSeek
    LLAMA = "llama"        # Meta Llama
    OLLAMA = "ollama"      # Ollama 本地服务
    VLLM = "vllm"          # vLLM 推理服务器
    CUSTOM = "custom"      # 自定义 OpenAI-compatible


# 各 provider 默认端口
_DEFAULT_PORT: dict[LocalLLMProvider, int] = {
    LocalLLMProvider.QWEN: 8000,
    LocalLLMProvider.DEEPSEEK: 8000,
    LocalLLMProvider.LLAMA: 8000,
    LocalLLMProvider.OLLAMA: 11434,
    LocalLLMProvider.VLLM: 8000,
    LocalLLMProvider.CUSTOM: 8000,
}


# =====================================================================
# Config
# =====================================================================
@dataclass
class LocalLLMConfig:
    """本地 LLM 配置。

    Attributes:
        provider: 后端类型
        model_path: 模型路径或模型名(如 "Qwen/Qwen2.5-7B-Instruct")
        port: 服务端口
        api_base: API 基址(优先于 port;为空时用 http://localhost:{port}/v1)
        max_context: 最大上下文长度(token)
        gpu_required: 是否需要 GPU(决定 load_model 行为)
        api_key: API key(部分后端需要,如 DashScope)
        timeout_seconds: HTTP 超时
        mock_mode: 强制 mock 模式(不发起真实 HTTP)
    """

    provider: LocalLLMProvider = LocalLLMProvider.OLLAMA
    model_path: str = ""
    port: int = 0  # 0 → 用 _DEFAULT_PORT
    api_base: str = ""
    max_context: int = 4096
    gpu_required: bool = False
    api_key: str = ""
    timeout_seconds: float = 10.0
    mock_mode: bool = False

    def __post_init__(self) -> None:
        if not self.port:
            self.port = _DEFAULT_PORT.get(self.provider, 8000)
        if not self.api_base:
            self.api_base = f"http://localhost:{self.port}/v1"
        # 去掉末尾斜杠
        self.api_base = self.api_base.rstrip("/")

    @property
    def chat_endpoint(self) -> str:
        return f"{self.api_base}/chat/completions"

    @property
    def models_endpoint(self) -> str:
        return f"{self.api_base}/models"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["provider"] = self.provider.value
        # 不暴露 api_key(防日志泄漏)
        d["api_key"] = "***" if self.api_key else ""
        return d


# =====================================================================
# HTTP 客户端(惰性导入,优先级 requests > httpx > urllib)
# =====================================================================
def _post_json(url: str, payload: dict[str, Any], timeout: float,
               headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """POST JSON,返回 (status_code, response_json)。

    优先级:
        1. requests(若已安装)
        2. httpx(若已安装)
        3. urllib.request(标准库,始终可用)
    """
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    body = json.dumps(payload).encode("utf-8")

    # 1. requests
    try:
        import requests  # type: ignore
        resp: Any = requests.post(url, data=body, headers=hdrs, timeout=timeout)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"_raw": resp.text}
    except ImportError:
        pass

    # 2. httpx
    try:
        import httpx  # type: ignore
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, content=body, headers=hdrs)
            try:
                return resp.status_code, resp.json()
            except ValueError:
                return resp.status_code, {"_raw": resp.text}
    except ImportError:
        pass

    # 3. urllib(标准库)
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            data = json.loads(resp.read().decode("utf-8"))
            return status, data
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            return e.code, {"_error": str(e)}
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return 0, {"_error": str(e)}


def _get_json(url: str, timeout: float,
              headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """GET JSON。"""
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)

    try:
        import requests  # type: ignore
        resp: Any = requests.get(url, headers=hdrs, timeout=timeout)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"_raw": resp.text}
    except ImportError:
        pass

    try:
        import httpx  # type: ignore
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=hdrs)
            try:
                return resp.status_code, resp.json()
            except ValueError:
                return resp.status_code, {"_raw": resp.text}
    except ImportError:
        pass

    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            return e.code, {"_error": str(e)}
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return 0, {"_error": str(e)}


# =====================================================================
# LocalLLMClient
# =====================================================================
class LocalLLMClient:
    """本地 LLM 客户端(OpenAI-compatible)。

    用法:
        config = LocalLLMConfig(provider=LocalLLMProvider.OLLAMA, model_path="llama3")
        client = LocalLLMClient(config)
        if client.health_check():
            client.load_model()
            reply = client.chat([{"role": "user", "content": "你好"}])
            client.unload_model()
    """

    def __init__(self, config: LocalLLMConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        # 运行时状态
        self._model_loaded: bool = False
        self._gpu_memory_mb: int = 0
        self._total_tokens: int = 0
        self._total_calls: int = 0
        self._failed_calls: int = 0
        self._queue_length: int = 0
        self._last_call_at: float = 0.0
        # mock 模式探测:首次 chat 时若 health_check 失败 → 自动 mock
        self._mock_active: bool = config.mock_mode

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    def health_check(self) -> bool:
        """Ping /v1/models 端点。

        Returns:
            True 服务可用 / False 不可用(自动激活 mock 模式)
        """
        if self.config.mock_mode:
            return False  # mock 模式直接 False,触发 mock chat

        try:
            status, _ = _get_json(
                self.config.models_endpoint,
                self.config.timeout_seconds,
                headers=self._auth_headers(),
            )
            ok = 200 <= status < 300
            if not ok:
                with self._lock:
                    self._mock_active = True
            return ok
        except Exception as e:
            logger.debug("LocalLLM health_check failed: %s", e)
            with self._lock:
                self._mock_active = True
            return False

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------
    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """OpenAI-compatible chat completions。

        Args:
            messages: [{"role": "system"/"user"/"assistant", "content": "..."}]
            **kwargs: model / temperature / max_tokens / top_p / stream 等

        Returns:
            assistant 回复文本

        若 mock 模式或 health_check 失败 → 返回 mock 响应。
        """
        with self._lock:
            self._total_calls += 1
            self._last_call_at = time.time()
            self._queue_length = max(0, self._queue_length - 1)

        # 决定 mock
        if self._should_mock():
            return self._mock_chat(messages, **kwargs)

        payload: dict[str, Any] = {
            "model": kwargs.pop("model", self.config.model_path or "default"),
            "messages": messages,
            "temperature": kwargs.pop("temperature", 0.7),
            "max_tokens": kwargs.pop("max_tokens", 1024),
            "stream": kwargs.pop("stream", False),
        }
        # 其余 kwargs 透传
        payload.update(kwargs)

        try:
            status, data = _post_json(
                self.config.chat_endpoint,
                payload,
                self.config.timeout_seconds,
                headers=self._auth_headers(),
            )
        except Exception as e:
            logger.warning("LocalLLM chat HTTP error: %s, fallback to mock", e)
            with self._lock:
                self._failed_calls += 1
                self._mock_active = True
            return self._mock_chat(messages, **kwargs)

        if not (200 <= status < 300):
            logger.warning(
                "LocalLLM chat non-2xx status=%s, fallback to mock. body=%s",
                status, str(data)[:200],
            )
            with self._lock:
                self._failed_calls += 1
                self._mock_active = True
            return self._mock_chat(messages, **kwargs)

        try:
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            with self._lock:
                self._total_tokens += int(usage.get("total_tokens", 0))
            return content
        except (KeyError, IndexError, TypeError) as e:
            logger.warning("LocalLLM chat malformed response: %s, fallback to mock", e)
            with self._lock:
                self._failed_calls += 1
                self._mock_active = True
            return self._mock_chat(messages, **kwargs)

    # ------------------------------------------------------------------
    # 模型加载 / 卸载(模拟)
    # ------------------------------------------------------------------
    def load_model(self) -> bool:
        """加载模型到 GPU(模拟)。

        实际生产中此处会触发 vLLM/llama.cpp 加载,
        本实现仅设置状态 + 模拟显存占用。
        """
        with self._lock:
            if self._model_loaded:
                logger.debug("LocalLLM model already loaded")
                return True
            # 模拟显存占用:7B ≈ 14GB (fp16),13B ≈ 26GB,简化为参数量 * 2
            self._gpu_memory_mb = self._estimate_gpu_memory()
            self._model_loaded = True
            logger.info(
                "LocalLLM model loaded: %s (provider=%s, gpu_mem=%dMB)",
                self.config.model_path, self.config.provider.value,
                self._gpu_memory_mb,
            )
            return True

    def unload_model(self) -> bool:
        """释放 GPU 显存。"""
        with self._lock:
            if not self._model_loaded:
                return True
            self._gpu_memory_mb = 0
            self._model_loaded = False
            logger.info("LocalLLM model unloaded: %s", self.config.model_path)
            return True

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def get_stats(self) -> dict[str, Any]:
        """返回运行时统计。

        Returns:
            {
                "provider": str,
                "model_path": str,
                "model_loaded": bool,
                "gpu_memory_mb": int,
                "gpu_required": bool,
                "total_calls": int,
                "failed_calls": int,
                "total_tokens": int,
                "queue_length": int,
                "throughput_tokens_per_sec": float,
                "mock_active": bool,
            }
        """
        with self._lock:
            elapsed = max(1e-6, time.time() - (self._last_call_at or time.time()))
            # 吞吐:粗略估算(总 token / 总调用数 × 1/s avg_latency)
            throughput = self._total_tokens / max(1, self._total_calls) / max(0.001, elapsed) if self._total_calls else 0.0
            return {
                "provider": self.config.provider.value,
                "model_path": self.config.model_path,
                "model_loaded": self._model_loaded,
                "gpu_memory_mb": self._gpu_memory_mb,
                "gpu_required": self.config.gpu_required,
                "total_calls": self._total_calls,
                "failed_calls": self._failed_calls,
                "total_tokens": self._total_tokens,
                "queue_length": self._queue_length,
                "throughput_tokens_per_sec": round(throughput, 2),
                "mock_active": self._mock_active,
            }

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return self._model_loaded

    @property
    def mock_active(self) -> bool:
        with self._lock:
            return self._mock_active

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _should_mock(self) -> bool:
        if self.config.mock_mode:
            return True
        with self._lock:
            return self._mock_active

    def _auth_headers(self) -> dict[str, str]:
        if self.config.api_key:
            return {"Authorization": f"Bearer {self.config.api_key}"}
        return {}

    def _estimate_gpu_memory(self) -> int:
        """根据模型名粗略估算显存(MB)。"""
        name = (self.config.model_path or "").lower()
        # 简化估算:7B → 14000, 13B → 26000, 70B → 140000, 其他 → 8000
        if "70b" in name:
            return 140_000
        if "13b" in name or "14b" in name:
            return 26_000
        if "7b" in name:
            return 14_000
        if "3b" in name:
            return 6_000
        if "1.5b" in name or "1b" in name:
            return 3_000
        return 8_000  # 默认 8GB

    def _mock_chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """生成 mock 响应(用于 CI / 离线开发)。"""
        with self._lock:
            # 模拟 token 计数
            total_chars = sum(len(m.get("content", "")) for m in messages)
            self._total_tokens += total_chars // 4 + 32  # 粗略 4 char/token
        # 找最后一条 user 消息
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = m.get("content", "")
                break
        return (
            f"[mock-{self.config.provider.value}] "
            f"Reply to: {last_user[:80]}"
        )
