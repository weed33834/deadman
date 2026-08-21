"""Phase 7 CLI 集成清单 - 由主智能体统一集成

Phase 7 范围：修复 deadman Web UI 的 P0-1 缺陷
（/api/chat 端点绕过 orchestration/graph.py 规则链）

# 新增子命令（无）
Phase 7 不新增 CLI 子命令，只修改 web/server.py

# 修改文件清单
- web/server.py
    - _handle_chat：从 (req: dict) 改为 (agent, query, history, user_id=None)
    - _handle_chat：走 build_main_graph().ainvoke(state) 而非 llm_client.chat()
    - _handle_chat：graph 失败时降级到 llm_client 但用 SoulLoader.default_soul() 而非硬编码 prompt
    - _handle_chat：调 MemoryManager.after_turn 更新 4 层记忆
    - _handle_whoami（新增）：返回平台身份（is_ai=True + disclaimer）
    - do_GET / do_POST：新增 /api/whoami 路由
    - do_POST /api/chat：从请求体读 user_id（user_id 或 userId），传给 _handle_chat
    - DeadmanWebServer = WebServer（别名，验证脚本用）
- web/static/index.html
    - 新增免责声明横幅（transparency-framework L5 + service-boundary 四项禁止）
    - 横幅可关闭（localStorage 持久化）
    - 页面初始化调 /api/whoami（透明度告知）
- tests/test_web_chat_graph.py（新增）：8 个测试用例覆盖 graph 集成
- _cli_extensions/__init__.py（新增）：CLI 扩展清单目录
- _cli_extensions/phase7.py（本文件）：Phase 7 CLI 集成清单

# cli.py 改动
无（web/server.py 自包含，不依赖 cli.py 改动）

# CHANGELOG.md 改动
无（由主智能体最后统一写入）
"""

# 无可执行代码 - 本文件仅作为 CLI 集成清单
