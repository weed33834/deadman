"""Dead Man Switch - Phase 15 多因子死亡推定模块

竞品对标：GoodTrust "Dead Man Switch"（邮件 + 连续 3 次失联触发）。

deadman 强化方向：
    - 多因子验证：邮件 + 短信 + 紧急联系人 + 律师介入 + 法定继承人二次确认
    - 不可逆操作必须真人二次确认
    - 遵守 notification-guardrails.md L4 硬边界（默认静默 / opt-in / 频率上限 / 退订入口）
    - 遵守 safety-protocol.md：触发死亡推定后等待期至少 7 天，期间可撤销

状态机：
    ACTIVE → SUSPECTED → VERIFYING → CONFIRMED → EXECUTED
                                       ↑
    任何阶段用户主动 check-in 或紧急联系人回复"安好" → 回到 ACTIVE
    用户主动取消 → CANCELLED
"""
