#!/usr/bin/env bash
# ============================================================
# Legacy 平台容器入口脚本
#
# 支持三种运行模式（通过 CMD/参数切换）：
#   mcp-server  (默认) 启动 MCP Server 的 HTTP 传输模式
#   eval               运行自动化评估套件
#   run "<输入>"        运行单次对话
#
# 用法：
#   docker run legacy                          # 默认 mcp-server
#   docker run legacy mcp-server               # 显式指定
#   docker run legacy eval                     # 评估
#   docker run legacy run "老人去世后如何办理死亡证明"
# ============================================================
set -euo pipefail

# 日志输出到 stderr（保留 stdout 给 MCP 的 JSON-RPC 流）
log() {
    printf '[entrypoint] %s\n' "$*" >&2
}

# 启动前检查必要环境变量
# 设计原则：缺失关键变量时告警但不阻塞（代码层已做优雅降级），
#           仅在 STRICT_ENV_CHECK=true 时强制校验并退出。
check_env() {
    local mode="$1"
    local warnings=()
    local errors=()

    # LLM_API_KEY：MCP/run/eval 模式下强烈建议配置（缺失则 LLM 工具走 fallback）
    if [[ -z "${LLM_API_KEY:-}" ]]; then
        warnings+=("LLM_API_KEY 未设置，LLM 相关工具将降级为 fallback 模式")
    fi

    # LLM_MODEL：缺失时使用 config.py 默认值（gpt-4o）
    if [[ -z "${LLM_MODEL:-}" ]]; then
        warnings+=("LLM_MODEL 未设置，将使用默认模型")
    fi

    # run 模式必须有输入参数
    if [[ "$mode" == "run" ]]; then
        if [[ $# -lt 2 || -z "${2:-}" ]]; then
            errors+=("run 模式需要提供输入文本作为参数")
        fi
    fi

    # 严格模式：可选启用，缺失关键变量即退出
    if [[ "${STRICT_ENV_CHECK:-false}" == "true" ]]; then
        if [[ -z "${LLM_API_KEY:-}" ]]; then
            errors+=("STRICT_ENV_CHECK=true 时 LLM_API_KEY 必须设置")
        fi
    fi

    # 输出告警
    for w in "${warnings[@]}"; do
        log "警告: $w"
    done

    # 输出错误并退出
    if (( ${#errors[@]} > 0 )); then
        for e in "${errors[@]}"; do
            log "错误: $e"
        done
        exit 1
    fi
}

# ====================================================================
# 主逻辑
# ====================================================================
MODE="${1:-mcp-server}"
shift || true  # 移除 mode 参数，剩余参数透传给子命令

log "运行模式: ${MODE}"

check_env "$MODE" "$@"

case "$MODE" in
    # ---- MCP Server 模式（默认）----
    mcp-server)
        log "启动 MCP Server (transport=http, host=${MCP_SERVER_HOST:-0.0.0.0}, port=${MCP_SERVER_PORT:-8000})"
        exec legacy-mcp-server \
            --transport http \
            --host "${MCP_SERVER_HOST:-0.0.0.0}" \
            --port "${MCP_SERVER_PORT:-8000}" \
            --log-level "${LOG_LEVEL:-INFO}"
        ;;

    # ---- 评估模式 ----
    eval)
        log "运行自动化评估套件"
        # 透传剩余参数（如 --cases-dir, -v）
        exec legacy eval "$@"
        ;;

    # ---- 单次对话模式 ----
    run)
        log "运行单次对话"
        exec legacy run "$@"
        ;;

    # ---- 未知模式 ----
    *)
        log "未知模式: ${MODE}"
        log "支持的模式: mcp-server (默认) | eval | run"
        log "示例:"
        log "  docker run legacy mcp-server"
        log "  docker run legacy eval"
        log "  docker run legacy run \"你的问题\""
        exit 1
        ;;
esac
