# 安全策略

## 报告漏洞

deadman 严肃对待安全问题。如发现漏洞，请勿在公开 Issue 中提交，
按以下流程私下报告：

1. 通过 GitHub/GitCode 仓库的 **Security Advisory** 功能提交（优先）
2. 或邮件至项目维护者（地址见仓库 Profile）
3. 或在私有 Issue 中标记 `security` 标签

报告中请包含：
- 漏洞类型与影响范围
- 复现步骤（最小化 PoC）
- 受影响版本
- 建议的修复方向（可选）

## 响应时效

- 收到报告后 **72 小时内**确认收到
- **7 个工作日内**给出初步评估（是否接受、严重程度、修复计划）
- 修复完成后**公开致谢**（如报告者同意）

## 适用范围

下列问题视为安全漏洞：

- 越权访问他人终活笔记 / 保险库 / 工单 / onboarding profile
- 加密 envelope 可被无密钥解密
- JWT 伪造或绕过
- PII 脱敏失效导致明文落盘
- Prompt Injection 绕过 input-guardrails 导致规则链失效
- 自杀风险信号被 L0 拦截后仍输出代办/法律意见

下列问题**不视为**安全漏洞：

- 自身账户内数据被合法管理员查看
- 自托管部署未配置 HTTPS / 未设置环境变量
- 已知第三方依赖漏洞（请直接报告至上游）

## 加密方案现状

截至 v5.1.0：

| 模块 | 算法 | 状态 |
|------|------|------|
| `auth/store.py` | PBKDF2-HMAC-SHA256 (100k iter) + 16B salt | ✅ 达 NIST/OWASP 2023 推荐 |
| `auth/jwt.py` | PyJWT HS256 签发/验证/刷新 | ✅ 标准库实现，过期与签名校验由 SDK 处理 |
| `ending_note/store.py` | PBKDF2-HMAC-SHA256 派生密钥 + AES-256-GCM AEAD + per-user passphrase（v3） | ✅ 已升级到认证加密（utils/crypto.py 共享模块） |
| `vault/store.py` | 同 ending_note | ✅ 同上 |

加密 envelope v1/v2（旧流密码）数据通过自动迁移机制在读取时解密、写入时升级到 v3（AES-256-GCM）。

## 安全最佳实践（自托管者必读）

1. **必须设置环境变量**：
   ```bash
   export DEADMAN_ENDING_NOTE_PASSPHRASE="<强随机串>"
   export DEADMAN_VAULT_PASSWORD="<强随机串>"
   export JWT_SECRET="<强随机串>"
   ```
2. **生产部署**：
   - 启用 HTTPS（Nginx 反代 + Let's Encrypt）
   - 设置 CSP 头
   - 限制 `/api/cli/*` 端点的访问来源
3. **数据备份**：`~/.deadman/` 目录需定期加密备份
4. **日志脱敏**：生产环境 `LOG_LEVEL=WARNING`，避免 PII 进日志

## 致谢

感谢所有报告漏洞的安全研究者。已修复的安全问题将在 CHANGELOG 中记录。
