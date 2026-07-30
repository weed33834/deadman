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

    # === P7: 多模型分工 configuration（借鉴 OpenDeepResearch configuration.py）===
    # 不同任务用不同模型以平衡成本与质量：
    #   - router: 意图分类，调一次 LLM 返回 JSON，用便宜模型即可（默认 gpt-4o-mini）
    #   - summarizer: 摘要/记忆压缩/上下文压缩，用便宜模型
    #   - respond: 主响应生成（agent_node），需要质量，用强模型（默认 = LLM_MODEL）
    # 未配置时全部回退到 LLM_MODEL（向后兼容）
    # 格式："provider:model"，例如 "openai:gpt-4o-mini"
    # 各 provider 的 api_key 从对应环境变量读取（OPENAI_API_KEY/ANTHROPIC_API_KEY/ZHIPU_API_KEY）
    llm_model_router: str = os.getenv("LLM_MODEL_ROUTER", "")  # 空 = 用 LLM_MODEL
    llm_model_summarizer: str = os.getenv("LLM_MODEL_SUMMARIZER", "")  # 空 = 用 LLM_MODEL
    llm_model_respond: str = os.getenv("LLM_MODEL_RESPOND", "")  # 空 = 用 LLM_MODEL

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
        default_factory=lambda: os.getenv("JUDGE_MODELS", "gpt-4o,claude-3-5-sonnet,glm-4.6").split(
            ","
        )
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
    selfcheck_temperatures: list[float] = field(default_factory=lambda: [0.3, 0.5, 0.7, 0.4, 0.6])
    selfcheck_consistency_threshold: float = float(
        os.getenv("SELFCHECK_CONSISTENCY_THRESHOLD", "0.5")
    )

    # === A2A ===
    a2a_registry_url: str = os.getenv("A2A_REGISTRY_URL", "https://a2a-registry.example.com")
    a2a_self_agent_id: str = os.getenv("A2A_SELF_AGENT_ID", "deadman")

    # === 工具 ===
    # 联网搜索 provider（duckduckgo 等；详见 tools/web_search.py）
    web_search_provider: str = os.getenv("WEB_SEARCH_PROVIDER", "duckduckgo")

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

    # === Sentry 错误监控（P1-2：企业级可观测性）===
    # DSN 留空时 sentry_sdk.init 不执行，零开销降级（生产必配）
    # 用 default_factory 让每次 Settings() 都读最新 env var（便于测试隔离）
    sentry_dsn: str = field(default_factory=lambda: os.getenv("SENTRY_DSN", ""))
    sentry_environment: str = field(
        default_factory=lambda: os.getenv("SENTRY_ENVIRONMENT", "production")
    )
    # 事务采样率（0=关闭性能监控，1=全量；生产建议 0.1~0.3 平衡配额与覆盖）
    sentry_traces_sample_rate: float = field(
        default_factory=lambda: float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1"))
    )
    # release 版本（默认从 git 推断；CI 可显式注入语义版本）
    sentry_release: str = field(default_factory=lambda: os.getenv("SENTRY_RELEASE", ""))

    # === 消息平台 Gateway / 主动通知护栏（notification-guardrails.md L4）===
    # Telegram Bot API token，未配置时 TelegramConnector 优雅降级
    telegram_bot_token: str = os.getenv("DEADMAN_TELEGRAM_BOT_TOKEN", "")
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

    # === 各业务模块数据目录（单一真相源，与 .env.example 对齐）===
    # Dead Man Switch 状态机数据（注意：实际目录名 deadman_switch，非 switch）
    switch_data_dir: Path = Path(
        os.getenv("DEADMAN_SWITCH_DATA_DIR", str(Path.home() / ".deadman" / "deadman_switch"))
    )
    # 客服工单数据
    support_data_dir: Path = Path(
        os.getenv("DEADMAN_SUPPORT_DATA_DIR", str(Path.home() / ".deadman" / "support"))
    )
    # Onboarding 向导数据
    onboarding_data_dir: Path = Path(
        os.getenv("DEADMAN_ONBOARDING_DATA_DIR", str(Path.home() / ".deadman" / "onboarding"))
    )

    # === 主数据库（企业级扩展④：PostgreSQL + SQLAlchemy async）===
    # 留空时所有现有文件存储原样工作（零侵入优雅降级）；
    # 配置后 DB 层激活，支持 DB↔文件双写迁移过渡。
    # 推荐格式：postgresql+asyncpg://user:pass@host:5432/deadman
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
    # 连接池大小（生产建议 10-20；单机开发 5 足够）
    db_pool_size: int = int(os.getenv("DATABASE_POOL_SIZE", "5"))
    # 连接池最大溢出
    db_max_overflow: int = int(os.getenv("DATABASE_MAX_OVERFLOW", "10"))
    # 连接池回收秒数（避免长连接被数据库侧关闭）
    db_pool_recycle: int = int(os.getenv("DATABASE_POOL_RECYCLE", "1800"))

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
