# 对话命令使用手册

> deadman 平台为**对话优先 / 傻瓜式操作**设计：多数功能无需点页面，直接聊天或输入命令即可完成。
> 在对话中输入 `/help` 看总览、`/manual` 看本手册。

## 快速上手
- **直接中文提问**：如「北京殡葬流程」「医保怎么报销」「帮我写悼文」——Agent 会自动调用工具。
- **或输入斜杠命令**：见下。

## 一、资源 / 配置
| 命令 | 作用 |
|---|---|
| `/prompt list` | 查看提示词列表 |
| `/prompt get <名>` | 查看某提示词 |
| `/prompt set <名> <内容>` | 修改 / 新建提示词（人设/规则） |
| `/expert list` | 查看自定义专家 |
| `/expert new <id> <名> <人设>` | 新增专家 |
| `/expert delete <id>` | 删除专家 |
| `/skill list` · `/skill enable <名>` · `/skill disable <名>` | 技能管理 |

## 二、查询 / 信息
| 命令 | 作用 |
|---|---|
| `/hotline [省份] [功能]` | 查官方热线 |
| `/institution [省] [城市]` | 查机构 |
| `/custom list` · `/custom get <地区>` · `/custom presets` | 民俗规则（丧葬/婚嫁/烧七） |
| `/family list` · `/family add <姓名>` | 亲属图谱 |

## 三、业务数据
| 命令 | 作用 |
|---|---|
| `/vault list` · `/vault add <名称> <类别>` | 数字遗产保险库 |
| `/note list` · `/note set <章节> <内容>` | 终活笔记 |
| `/docs list` | 已管理文档 |
| `/switch status` | Dead Man Switch 状态 |
| `/task list` · `/task add <cron> <内容>` | 定时任务 |

## 四、创作 / 工具
| 命令 | 作用 |
|---|---|
| `/memorial <姓名> <关系> <回忆>` | 悼文生成 |
| `/plot <python代码>` | 沙箱画图（matplotlib） |
| `/image <描述>` | AI 图像生成 |
| `/browse <网址>` | 浏览网页并总结 |

## 五、其他
- `/help`：命令总览
- `/manual`：本手册
- 界面快捷按钮：身后事流程 / 医疗导航 / 数字遗产 / 悼文 / 热线 / 民俗 / 亲属 / 画图 / 命令

## 提示
- 移动端 `/m` 同样支持上述命令、语音输入与朗读。
- 管理台（`/admin`）提供更细粒度的可视化操作（民俗/亲属/知识库/IAM/定时任务等）。
