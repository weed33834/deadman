# 品牌名

本项目是一个**通用智能体平台**，不绑定任何特定厂商（不止 TRAE，也适用 OpenAI/Anthropic/阿里/腾讯/智谱等所有支持 agent 的平台）。因此品牌名独立于任何平台。

## 三语品牌名

| 语言 | 品牌名 | 含义 | 适用场景 |
|------|--------|------|---------|
| 中文 | **死者为大** | 取自中国传统观念"死者为大"，体现对逝者的尊重与庄重。用户一听即懂主题。 | 中文用户、CN 地域 |
| 英文 | **Legacy** | 意为"遗产/传承"，国际通用，涵盖身后事（财产/账号/记忆的传承）。简洁易记。 | 国际用户、EN 地域 |
| 日文 | **終活**（しゅうかつ） | 日本现成词汇，意为"为临终做准备"，本土化最强，日本用户秒懂。 | 日本用户、JP 地域 |

## 智能体根据用户语言自动选择品牌名展示

```python
BRAND_NAMES = {
    "zh": "死者为大",
    "en": "Legacy",
    "ja": "終活",
}

DEFAULT_BRAND = "Legacy"  # 国际默认

def get_brand_name(user_language: str) -> str:
    """根据用户语言返回对应品牌名"""
    lang_prefix = user_language.split("-")[0].lower()
    return BRAND_NAMES.get(lang_prefix, DEFAULT_BRAND)
```

## 命名规范

### 包名/标识符（统一用英文）

- Python 包名：`legacy`
- PyPI 包名：`legacy-aftercare`
- CLI 命令：`legacy`
- MCP Server 命令：`legacy-mcp-server`
- A2A agent_id 前缀：`legacy-*`（如 `legacy-death-aftercare`）

### 用户可见名（按语言切换）

- 中文场景：死者为大
- 英文场景：Legacy
- 日文场景：終活

### 不使用的前缀（已废弃）

- ~~trae-aftercare~~（绑定特定平台，已废弃）
- ~~trae_aftercare~~（同上）
