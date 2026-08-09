"""P5 安全与护栏层 - 审计链 / JIT 权限 / 内容沙箱 / 红队 / Honeypot

所有模块均由独立的 feature flag 控制，默认关闭：
- DEADMAN_AUDIT_CHAIN_ENABLED=0      审计链 append-only
- DEADMAN_JIT_PERMISSION_ENABLED=0   JIT 短时工具权限
- DEADMAN_GUID_SANDBOX_ENABLED=0     GUID 分隔符防御（在 orchestration/nodes.py）
- DEADMAN_CONTENT_SANDBOX_ENABLED=0  外部内容沙箱
- DEADMAN_REDTEAM_ENABLED=0          红队自动化
- DEADMAN_HONEYPOT_ENABLED=0         Honeypot 假工具

降级原则：所有模块在 feature flag 关闭时静默 no-op，不抛异常，行为完全不变。
"""

from __future__ import annotations
