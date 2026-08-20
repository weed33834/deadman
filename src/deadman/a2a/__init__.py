"""A2A (Agent-to-Agent) Protocol v1.0 实现

让本平台智能体能被外部 agent 发现和调用，也能调用外部 agent。
核心概念：
- AgentCard：智能体名片，声明能力
- Task Lifecycle：submitted → working → completed/failed/input-required
- JSON-RPC 2.0：tasks/send, tasks/get, tasks/sendSubscribe
"""
