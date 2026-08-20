"""P7 工程基建层 - 生产级基础设施组件。

包含:
    - feature_flags: 统一 Feature Flag 系统(env var + 动态切换 + 百分比分流)
    - circuit_breaker: 三态熔断器(Closed/Open/Half-Open)
    - rate_limiter: 令牌桶限流器
    - web_middleware: 限流/CSP/安全头/CORS 中间件
    - multi_tenant: 多租户数据隔离
    - prompt_versioning: Prompt 版本化 + AB 测试
    - durable_execution: 幂等键 + 崩溃恢复
    - quota: 配额与计费(超限降级)
    - credential_vault: AES-256-GCM 凭证保险柜

所有模块遵循"feature flag 默认关闭 + 降级路径全覆盖 + 不破坏 1076 测试"三大原则。
"""
