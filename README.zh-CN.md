# deadman

<p align="center"><img src="assets/logo.svg" alt="deadman Logo" width="360"></p>

<p align="center">
  <a href="README.md"><img alt="English" src="https://img.shields.io/badge/English-README-blue"></a>
  <a href="#"><img alt="中文" src="https://img.shields.io/badge/%E4%B8%AD%E6%96%87-%E6%9C%AC%E9%A1%B5-red"></a>
  <a href="README.ja-JP.md"><img alt="日本語" src="https://img.shields.io/badge/%E6%97%A5%E6%9C%AC%E8%AA%9E-README.ja--JP-green"></a>
</p>

> **deadman** —— 身后事 + 医疗导航多智能体引导平台。**对话优先 / 傻瓜式操作**：直接说话或输入，Agent 即可为你办完一切，无需点页面。支持**民俗/规则定制**与**亲属图谱可视化**。不绑定厂商，适用于任意 agent 平台。

## ✨ 核心特性

- 🗣️ **对话优先**：25+ 命令 + 语音 + 自然语言，全部功能可在对话中完成（`/help` 查看）
- 🪦 **身后事流程**：死亡证明 → 遗产清偿 9 阶段，覆盖中国 34 省 + 美/日
- 🏥 **医疗导航**：医保 / 大病 / 临终关怀
- 📜 **民俗规则引擎**：导入/自定义丧葬、婚嫁民俗，**头七~七七** 祭奠
- 👨‍👩‍👧 **亲属图谱**：家族成员 + 关系 → **SVG 图谱**可视化
- 🔐 **数字遗产与保险库**：加密资产登记、受益人指派
- 🕯 **AI 悼文**：悼文 / 讣告 / 答谢词 / 墓志铭
- 🛠 **通用智能体能力**：10 层架构、RAG 知识库、MCP 客户端、沙箱画图、文件解析、导出、图像生成、语音、定时任务、IAM、i18n、Trace、告警、管理台
- 🌐 **多语言**：English / 中文 / 日本語

## 🖥 界面预览

| | |
|---|---|
| ![对话](docs/screenshots/chat-home.png) | ![命令](docs/screenshots/chat-command.png) |
| 对话主界面 | /help 与命令 |
| ![民俗](docs/screenshots/customs.png) | ![亲属](docs/screenshots/kinship-graph.png) |
| 民俗规则 | 亲属图谱 |
| ![管理台](docs/screenshots/admin-overview.png) | ![移动端](docs/screenshots/mobile.png) |
| 管理台 | 移动端 /m |

## 🚀 快速开始

```bash
git clone https://github.com/weed33834/deadman.git
cd deadman && pip install -e .[all]
cp .env.example .env   # 配置 LLM_PROVIDER/LLM_MODEL/LLM_API_KEY
uvicorn deadman.web.app:app --host 0.0.0.0 --port 8002
# 打开 http://localhost:8002（对话为主界面，管理台在 /admin）
```

## 🗨 对话命令（可在手机 /m 使用）

配置 `/prompt` `/expert` `/skill` · 查询 `/hotline` `/institution` `/custom` `/family`
业务 `/vault` `/note` `/docs` `/switch` `/task` `/cases` `/letters` `/score` `/support`
创作 `/memorial` `/plot` `/image` `/browse` `/canvas` · 帮助 `/help` `/manual`

> 也可直接中文提问，如"北京丧葬费怎么领？""给爱读书的父亲写悼文"。

## 📚 文档

[管理台与功能](docs/ADMIN.md) · [对话命令手册](docs/CHAT_COMMANDS.md) · [部署](docs/DEPLOYMENT.md) · [品牌/Logo](BRAND.md) · [变更日志](CHANGELOG.md)

完整英文说明见 [README.md](README.md)。
