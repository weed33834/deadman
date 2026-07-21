"""deadman.auth - 用户认证与会话系统

Phase 8：实现注册/登录/JWT 会话，纯文件存储无数据库依赖。

模块：
  - store.UserStore：用户存储（PBKDF2-HMAC-SHA256 + HMAC 邮箱索引）
  - jwt.JWTManager：JWT 签发/验证/刷新（HS256 自实现）

遵守：
  - legal-compliance-framework：PIPL 数据最小化、加密、不存敏感 PII
  - safety-protocol：不泄露"邮箱不存在" vs "密码错"
  - integrity-framework：不编造、不弱化安全
  - input-guardrails：密码不入对话历史
"""

from .store import UserStore
from .jwt import JWTManager

__all__ = ["UserStore", "JWTManager"]
