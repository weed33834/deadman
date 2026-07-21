#!/usr/bin/env python3
"""deadman 平台容器健康检查脚本

请求 MCP Server 的 /health 端点，判断服务是否健康：
  - 返回 0：健康（HTTP 2xx）
  - 返回 1：不健康（连接失败 / 非 2xx 响应）

可作为 Docker HEALTHCHECK 使用（替代 curl 方案），也可被外部
监控系统（Prometheus blackbox_exporter、K8s livenessProbe 等）复用。

仅依赖 Python 标准库（urllib），无需安装额外包。

环境变量（均可选）：
  HEALTHCHECK_HOST    目标主机，默认 127.0.0.1
  HEALTHCHECK_PORT    目标端口，默认取 MCP_SERVER_PORT 或 8000
  HEALTHCHECK_PATH    健康端点路径，默认 /health
  HEALTHCHECK_TIMEOUT 请求超时秒数，默认 5
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    """执行健康检查，返回 0（健康）或 1（不健康）"""
    host = os.getenv("HEALTHCHECK_HOST", "127.0.0.1")
    port = os.getenv("HEALTHCHECK_PORT") or os.getenv("MCP_SERVER_PORT", "8000")
    path = os.getenv("HEALTHCHECK_PATH", "/health")
    timeout = float(os.getenv("HEALTHCHECK_TIMEOUT", "5"))
    url = f"http://{host}:{port}{path}"

    try:
        # urllib 默认会跟随 3xx 重定向；此处不期望重定向，禁用之
        opener = urllib.request.build_opener(NoRedirectHandler)
        with opener.open(url, timeout=timeout) as response:
            status = response.status
            if 200 <= status < 300:
                return 0
            print(f"健康检查失败：HTTP {status}", file=sys.stderr)
            return 1
    except urllib.error.HTTPError as exc:
        # 非 2xx 响应（4xx/5xx）
        print(f"健康检查失败：HTTP {exc.code} {exc.reason}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        # 连接失败（服务未启动 / 端口未监听）
        print(f"健康检查失败：无法连接 {url} - {exc.reason}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 健康检查必须吞掉所有异常
        print(f"健康检查异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁止跟随 HTTP 重定向的健康检查 handler

    /health 端点应直接返回 200，若发生重定向视为异常。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


if __name__ == "__main__":
    sys.exit(main())
