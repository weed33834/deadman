# 品牌名

本项目是一个**通用智能体平台**，不绑定任何特定厂商（不止 TRAE，也适用 OpenAI/Anthropic/阿里/腾讯/智谱等所有支持 agent 的平台）。因此品牌名独立于任何平台。

## 品牌名

统一品牌名：**deadman**（身后事多智能体引导平台）。

```python
BRAND_NAMES = {
    "zh": "deadman",
    "en": "deadman",
    "ja": "deadman",
}

DEFAULT_BRAND = "deadman"  # 国际默认

def get_brand_name(user_language: str) -> str:
    """根据用户语言返回对应品牌名"""
    lang_prefix = user_language.split("-")[0].lower()
    return BRAND_NAMES.get(lang_prefix, DEFAULT_BRAND)
```

## 命名规范

### 包名/标识符（统一用英文）

- Python 包名：`deadman`
- PyPI 包名：`deadman`
- CLI 命令：`deadman`
- MCP Server 命令：`deadman-mcp-server`
- A2A agent_id 前缀：`deadman-*`（如 `deadman-death-aftercare`）

### 用户可见名

- 全部场景统一使用：deadman
