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

    # === LLM ===
    llm_provider: str = os.getenv("LLM_PROVIDER", "openai")  # openai/anthropic/zhipu/...
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4o")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "")
    llm_timeout: int = int(os.getenv("LLM_TIMEOUT", "30"))

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
    a2a_self_agent_id: str = os.getenv("A2A_SELF_AGENT_ID", "legacy")

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
