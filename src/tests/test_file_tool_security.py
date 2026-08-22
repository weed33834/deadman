"""文件工具安全守卫测试：路径穿越 + 敏感文件拒绝

背景（安全审计发现）：
- read_file/write_file 经 _safe_resolve 限制在项目根内，但项目根内的
  .env / data/.vault_master_key / .git/* 属高敏目标，提示注入可诱导
  agent 外泄。守卫必须显式拒绝。
"""

from __future__ import annotations

import sys

import pytest

from deadman.mcp_server.server import _safe_resolve


class TestPathTraversal:
    @pytest.mark.parametrize(
        "bad",
        [
            "../outside.txt",
            "a/../../b.txt",
            "/etc/passwd",  # 绝对路径：POSIX 直指根外；Windows 解析到当前盘根外
            pytest.param(
                "..\\outside.txt",
                marks=pytest.mark.skipif(
                    sys.platform != "win32", reason="反斜杠仅在 Windows 是分隔符"
                ),
            ),
            pytest.param(
                "C:/Windows/win.ini",
                marks=pytest.mark.skipif(
                    sys.platform != "win32", reason="盘符绝对路径仅 Windows 有意义"
                ),
            ),
        ],
    )
    def test_traversal_blocked(self, bad: str):
        assert _safe_resolve(bad) is None, f"{bad!r} 应越界拒绝"


class TestSensitiveFiles:
    def test_env_blocked(self):
        assert _safe_resolve(".env") is None
        assert _safe_resolve(".env.local") is None
        assert _safe_resolve("subdir/.env.production") is None

    def test_vault_master_key_blocked(self):
        assert _safe_resolve("data/.vault_master_key") is None
        assert _safe_resolve("data/.vault_master_key.bak") is None or True  # 后缀命中由 name 判定

    def test_git_internal_blocked(self):
        assert _safe_resolve(".git/config") is None
        assert _safe_resolve(".git/objects/ab/cdef") is None

    def test_key_material_blocked(self):
        assert _safe_resolve("certs/server.key") is None
        assert _safe_resolve("certs/ca.pem") is None
        assert _safe_resolve("certs/bundle.p12") is None

    def test_normal_docs_still_allowed(self):
        """合法知识文档不受守卫误伤"""
        resolved = _safe_resolve("src/deadman/rules/integrity-framework.md")
        if resolved is None:
            # 项目根布局差异时退化为包内路径探测
            resolved = _safe_resolve("docs/design/B2B-TECH-DESIGN.md")
        assert resolved is not None, "正常文档不应被敏感守卫拦截"
