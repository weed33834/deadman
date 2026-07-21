"""全局配置 - 通过环境变量或配置文件加载"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    """全局配置"""

    # === 项目根目录 ===
    project_root: Path = Path(__file__).parent.parent.parent  # .traecli/

    # === MCP Server ===
    mcp_server_port: int = int(os.getenv("MCP_SERVER_PORT", "8000"))
    mcp_server_host: str = os.getenv("MCP_SERVER_HOST", "127.0.0.1")

    # === LLM 主配置 ===
    llm_provider: str = os.getenv("LLM_PROVIDER", "openai")  # openai/anthropic/zhipu/...
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4o")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "")
    llm_timeout: int = int(os.getenv("LLM_TIMEOUT", "30"))

    # === LLM Fallback 链（主 LLM 失败时按序尝试）===
    # 格式："provider:model" 逗号分隔，例如 "openai:gpt-4o,anthropic:claude-3-5-sonnet,zhipu:glm-4.6"
    # 每个 provider 的 api_key 从 {PROVIDER}_API_KEY 环境变量读取（OPENAI_API_KEY/ANTHROPIC_API_KEY/ZHIPU_API_KEY）
    llm_fallback_chain: list[str] = field(
        default_factory=lambda: [
            x.strip() for x in os.getenv("LLM_FALLBACK_CHAIN", "").split(",") if x.strip()
        ]
    )

    # === 评审模型（LLM-as-Judge） ===
    judge_models: list[str] = field(
        default_factory=lambda: os.getenv(
            "JUDGE_MODELS", "gpt-4o,claude-3-5-sonnet,glm-4.6"
        ).split(",")
    )
    judge_consensus_threshold: float = float(os.getenv("JUDGE_CONSENSUS_THRESHOLD", "0.67"))

    # === 可观测性 ===
    otel_endpoint: str = os.getenv("OTEL_ENDPOINT", "http://localhost:4317")
    langfuse_host: str = os.getenv("LANGFUSE_HOST", "http://localhost:3000")
    langfuse_secret: str = os.getenv("LANGFUSE_SECRET_KEY", "")
    langfuse_public: str = os.getenv("LANGFUSE_PUBLIC_KEY", "")

    # === 记忆 ===
    memory_max_turns: int = int(os.getenv("MEMORY_MAX_TURNS", "10"))
    memory_retention_years: int = int(os.getenv("MEMORY_RETENTION_YEARS", "7"))

    # === Checkpointer（LangGraph 状态持久化）===
    # 开发期用 SQLite，生产可换 Postgres（langgraph-checkpoint-postgres）
    # 为空则降级为 MemorySaver（进程重启即丢）
    checkpoint_db_path: str = os.getenv("CHECKPOINT_DB_PATH", "data/checkpoints.db")

    # === Docker 沙箱（工具隔离执行）===
    # 启用后 write_file 等工具在 Docker 容器内执行，避免污染主环境
    # 需要 Docker daemon 可用；不可用时自动降级为本地执行
    sandbox_enabled: bool = os.getenv("SANDBOX_ENABLED", "false").lower() == "true"
    sandbox_image: str = os.getenv("SANDBOX_IMAGE", "python:3.12-slim")
    sandbox_timeout: int = int(os.getenv("SANDBOX_TIMEOUT", "30"))
    sandbox_work_dir: str = os.getenv("SANDBOX_WORK_DIR", "/tmp/deadman-sandbox")

    # === Reflexion ===
    reflexion_max_retries: int = int(os.getenv("REFLEXION_MAX_RETRIES", "3"))

    # === SelfCheckGPT ===
    selfcheck_sample_count: int = int(os.getenv("SELFCHECK_SAMPLE_COUNT", "5"))
    selfcheck_temperatures: list[float] = field(
        default_factory=lambda: [0.3, 0.5, 0.7, 0.4, 0.6]
    )
    selfcheck_consistency_threshold: float = float(
        os.getenv("SELFCHECK_CONSISTENCY_THRESHOLD", "0.5")
    )

    # === A2A ===
    a2a_registry_url: str = os.getenv("A2A_REGISTRY_URL", "https://a2a-registry.example.com")
    a2a_self_agent_id: str = os.getenv("A2A_SELF_AGENT_ID", "deadman")

    # === LightRAG ===
    lightrag_enabled: bool = os.getenv("LIGHTRAG_ENABLED", "false").lower() == "true"
    lightrag_storage_dir: Path = Path(
        os.getenv("LIGHTRAG_STORAGE_DIR", str(Path(__file__).parent.parent / "data" / "lightrag"))
    )

    # === Graphiti ===
    graphiti_enabled: bool = os.getenv("GRAPHITI_ENABLED", "false").lower() == "true"
    graphiti_neo4j_uri: str = os.getenv("GRAPHITI_NEO4J_URI", "bolt://localhost:7687")
    graphiti_neo4j_user: str = os.getenv("GRAPHITI_NEO4J_USER", "neo4j")
    graphiti_neo4j_password: str = os.getenv("GRAPHITI_NEO4J_PASSWORD", "")

    # === 消息平台 Gateway / 主动通知护栏（notification-guardrails.md L4）===
    # Telegram Bot API token，未配置时 TelegramConnector 优雅降级
    telegram_bot_token: str = os.getenv("DEADMAN_TELEGRAM_BOT_TOKEN", "")
    # Gateway 总开关，默认关闭；启用需显式 DEADMAN_GATEWAY_ENABLED=true 或调 gateway-start
    gateway_enabled: bool = os.getenv("DEADMAN_GATEWAY_ENABLED", "false").lower() == "true"
    # 主动通知护栏数据目录（consent / unsubscribes / sent_log / last_session）
    notification_data_dir: Path = Path(
        os.getenv("DEADMAN_NOTIFICATION_DATA_DIR", str(Path.home() / ".deadman" / "notifications"))
    )

    # === 用户认证与会话（Phase 8，遵守 legal-compliance-framework PIPL）===
    # 用户数据目录：~/.deadman/auth/users.json + jwt_secret
    auth_data_dir: Path = Path(
        os.getenv("DEADMAN_AUTH_DATA_DIR", str(Path.home() / ".deadman" / "auth"))
    )
    # JWT 签名密钥（留空则自动生成并持久化到 auth_data_dir/jwt_secret）
    # 生产环境建议通过环境变量显式注入，避免单机密钥漂移
    jwt_secret: str = os.getenv("DEADMAN_JWT_SECRET", "")
    # JWT 过期天数，默认 7 天
    jwt_expiry_days: int = int(os.getenv("DEADMAN_JWT_EXPIRY_DAYS", "7"))
    # 密码最小长度（PBKDF2-HMAC-SHA256，100000 iterations）
    password_min_length: int = int(os.getenv("DEADMAN_PASSWORD_MIN_LENGTH", "8"))

    @property
    def rules_dir(self) -> Path:
        return self.project_root / "rules"

    @property
    def agents_dir(self) -> Path:
        return self.project_root / "agents"

    @property
    def knowledge_dir(self) -> Path:
        return self.project_root / "knowledge"

    @property
    def tests_dir(self) -> Path:
        return self.project_root / "tests"

    @property
    def skills_dir(self) -> Path:
        return self.project_root / "skills"


# 全局单例
settings = Settings()
