# ============================================================
# deadman 平台 Dockerfile - 多阶段构建
#
# 阶段划分：
#   builder  - 安装 Python 依赖到独立虚拟环境
#   runtime  - 最小化运行时镜像，仅复制代码与已装依赖
#
# 构建：  docker build -t deadman:latest .
# 运行：  docker run -p 8000:8000 -e LLM_API_KEY=sk-xxx deadman:latest
# 模式：  docker run deadman:latest mcp-server   # 默认
#         docker run -p 8002:8002 deadman:latest web-server
#         docker run deadman:latest eval
#         docker run deadman:latest run "你的问题"
# ============================================================

# ============================================================
# Builder 阶段：安装依赖
# ============================================================
FROM python:3.12-slim AS builder

# 关闭 Python 字节码写入与缓冲，加速构建
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装编译工具链（部分 Python 依赖如 pydantic-core 需要编译）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
    && rm -rf /var/lib/apt/lists/*

# 创建独立虚拟环境（runtime 阶段仅复制此目录）
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 升级打包工具
RUN pip install --upgrade pip setuptools wheel

# 复制 pyproject.toml + 源码，安装 deadman 包及其依赖
# 利用 .dockerignore 排除 tests/ 与缓存，缩小构建上下文
WORKDIR /build
COPY pyproject.toml ./
COPY .traecli/src/ ./.traecli/src/

# 安装当前包 + 企业级扩展④ [db] extras（SQLAlchemy/asyncpg/alembic）
# [db] extras 使镜像内置数据库迁移能力，DATABASE_URL 空时零开销降级
RUN pip install --no-cache-dir ".[db]"

# ============================================================
# Runtime 阶段：最小化运行时镜像
# ============================================================
FROM python:3.12-slim AS runtime

# 构建参数（仅用于镜像元数据，不进入运行时环境）
ARG DEADMAN_VERSION=5.1
ARG BUILD_DATE=unknown
ARG VCS_REF=unknown

# 运行时环境变量
#   - PYTHONPATH 指向源码目录，使 import deadman 优先加载源码树
#     （保证 config.py 中 project_root = Path(__file__).parent.parent.parent
#      解析为 /app/.traecli/，从而正确定位 rules/agents/knowledge/skills）
#   - MCP_SERVER_HOST=0.0.0.0 使服务在容器内对外可达
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app/.traecli/src" \
    DEADMAN_VERSION=${DEADMAN_VERSION} \
    # === MCP Server ===
    MCP_SERVER_HOST=0.0.0.0 \
    MCP_SERVER_PORT=8000 \
    # === LLM 默认配置（运行时通过 -e 或 compose 覆盖）===
    LLM_PROVIDER=openai \
    LLM_MODEL=gpt-4o \
    LLM_API_KEY="" \
    LLM_BASE_URL="" \
    LLM_TIMEOUT=30 \
    # === 可观测性默认端点（对应 docker-compose 中服务名）===
    OTEL_ENDPOINT=http://otel-collector:4317 \
    LANGFUSE_HOST=http://langfuse:3000 \
    LANGFUSE_SECRET_KEY="" \
    LANGFUSE_PUBLIC_KEY="" \
    # === Graphiti / Neo4j ===
    GRAPHITI_ENABLED=false \
    GRAPHITI_NEO4J_URI=bolt://neo4j:7687 \
    GRAPHITI_NEO4J_USER=neo4j \
    GRAPHITI_NEO4J_PASSWORD="" \
    # === LightRAG ===
    LIGHTRAG_ENABLED=false \
    # === 日志 ===
    LOG_LEVEL=INFO

# 安装运行时系统依赖：
#   curl            - HEALTHCHECK 用
#   ca-certificates - HTTPS 证书校验（LLM/OTel 出站）
#   tini             - PID 1 init，正确转发 SIGTERM
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

# 从 builder 复制已装好的虚拟环境（含依赖与 deadman 包）
COPY --from=builder /opt/venv /opt/venv

# 创建非 root 用户（安全最佳实践）
RUN groupadd --system deadman \
    && useradd --system --gid deadman --create-home \
       --home-dir /home/deadman --shell /bin/bash deadman

# 创建项目目录与数据持久化目录
RUN mkdir -p /app/.traecli /app/data /app/docker \
    && chown -R deadman:deadman /app

WORKDIR /app

# 复制项目运行时数据（rules/agents/knowledge/skills 等被 MCP 工具读取）
# 注意：构建上下文为仓库根目录，仅复制 .traecli/ 子目录以保持容器内路径结构
# src/ 中的 deadman 包通过 PYTHONPATH 优先加载，保证 project_root 解析正确
COPY --chown=deadman:deadman .traecli/ /app/.traecli/

# 复制入口脚本与健康检查脚本
COPY --chown=deadman:deadman .traecli/docker/entrypoint.sh /app/docker/entrypoint.sh
COPY --chown=deadman:deadman .traecli/docker/healthcheck.py /app/docker/healthcheck.py
# 企业级扩展④：复制 Alembic 迁移配置与脚本（支持容器内 alembic upgrade head）
COPY --chown=deadman:deadman alembic.ini /app/alembic.ini
COPY --chown=deadman:deadman migrations/ /app/migrations/
RUN chmod +x /app/docker/entrypoint.sh /app/docker/healthcheck.py

# 切换到非 root 用户
USER deadman

# 暴露端口：MCP Server(8000) / A2A Server(8001) / Web UI(8002)
EXPOSE 8000 8001 8002

# 数据持久化卷（记忆存储 / LightRAG / 运行时数据）
VOLUME ["/app/data"]

# 健康检查：curl 调用 /health 端点
#   --interval  两次检查间隔
#   --timeout   单次检查超时
#   --start-period 启动宽限期（MCP Server 初始化时间）
#   --retries   连续失败次数后标记 unhealthy
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# 镜像元数据（OCI 标准）
LABEL org.opencontainers.image.title="deadman" \
      org.opencontainers.image.description="身后事多智能体引导平台 MCP Server" \
      org.opencontainers.image.version="${DEADMAN_VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/weed33834/deadman" \
      org.opencontainers.image.licenses="MIT"

# 入口点：tini 作为 PID 1，entrypoint.sh 处理模式切换
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]

# 默认启动 MCP Server（HTTP 模式）
# 可通过 docker run ... <mode> 覆盖：mcp-server | eval | run
CMD ["mcp-server"]
