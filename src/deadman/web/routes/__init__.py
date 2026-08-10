"""web.routes —— FastAPI 路由包

存放补齐 agent-builder-skill 完整版能力而新增的 API 路由：
  * admin.py      —— G1 管理台只读/监控端点
  * resources.py  —— G1 管理台资源服务（prompts/agents/voices/settings/backup/测试台）
  * voice.py      —— G2 语音输入输出（transcribe + speak）
  * mcp.py        —— G3 MCP 客户端管理
  * text.py       —— 底层文本处理与检索
"""

from __future__ import annotations
