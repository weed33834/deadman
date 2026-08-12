# deadman

<p align="center"><img src="assets/logo.svg" alt="deadman Logo" width="360"></p>

<p align="center">
  <strong>An end-of-life & medical-navigation multi-agent guidance platform</strong><br>
  Framework-agnostic · runs on any agent platform (OpenAI / Anthropic / DeepSeek / local models)
</p>

<p align="center">
  <a href="#"><img alt="Language" src="https://img.shields.io/badge/lang-English-blue"></a>
  <a href="README.zh-CN.md"><img alt="中文" src="https://img.shields.io/badge/%E4%B8%AD%E6%96%87-README.zh--CN-red"></a>
  <a href="README.ja-JP.md"><img alt="日本語" src="https://img.shields.io/badge/%E6%97%A5%E6%9C%AC%E8%AA%9E-README.ja--JP-green"></a>
</p>

<p align="center">
  [![tests](https://github.com/weed33834/deadman/actions/workflows/tests.yml/badge.svg)](https://github.com/weed33834/deadman/actions/workflows/tests.yml)
  [![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
  [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
  [![Version](https://img.shields.io/badge/version-5.4.0-6b5d4f.svg)](CHANGELOG.md)
  [![Tests](https://img.shields.io/badge/pytest-2926%20passed-brightgreen)]()
  [![MCP](https://img.shields.io/badge/MCP-Server%2BClient-blueviolet)]()
</p>

> **deadman** is a conversation-first AI platform that guides families through **end-of-life procedures, medical navigation, digital legacy, grief companionship**, and more. **Type or speak your need and the agent does it** — no need to click through pages. It also lets you **customize folk-custom rules** (funeral / wedding / memorial rituals) and visualize your **family tree (kinship graph)**.

---

## ✨ Key Features

- 🗣️ **Conversation-first, foolproof UX** — every feature is invocable by chatting or voice (`/help` lists 25+ commands). First-run guide included.
- 🪦 **After-death procedures** — 9-step guidance, death certificate → estate settlement, across China's 34 provinces + US + JP.
- 🏥 **Medical navigation** — medical insurance, critical-illness benefits, hospice care.
- 📜 **Folk-custom & rules engine** — import/customize regional funeral & wedding customs, **head-seven-to-seven (头七~七七)** memorial rituals.
- 👨‍👩‍👧 **Kinship graph** — build a family tree and render it as a visual **SVG kinship graph**.
- 🔐 **Digital legacy & vault** — encrypted asset register, beneficiary assignment, handover plans.
- 🕯 **Memorial writer** — AI-generated eulogy / obituary / thank-you note / epitaph.
- 🧠 **Awareness & crisis guard** — intent recognition + safety intervention (L0).
- 🛠 **Universal agent capabilities** — 10-layer architecture: LLM adapters (OpenAI/Anthropic/DeepSeek/Qwen/Zhipu/Ollama…), RAG + knowledge base, MCP server & client, sandboxed code execution + charts, voice (ASR/TTS), file parsing (PDF/Word/Image), export (MD/Word/PDF), image generation, web browsing, scheduled tasks, IAM, i18n, trace viewer, alerts, and a full **admin console**.
- 🌐 **Multi-language** — English / 中文 / 日本語 UI (i18n).

## 🖥 Screenshots

AI-driven live captures of the real interface. Full demo video: `docs/screenshots/demo.webm`.

| | |
|---|---|
| ![Chat](docs/screenshots/chat-home.png) | ![Commands](docs/screenshots/chat-command.png) |
| **Conversation-first chat** | **/help & 25+ commands** |
| ![Customs](docs/screenshots/customs.png) | ![Kinship](docs/screenshots/kinship-graph.png) |
| **Folk-custom rules** | **Kinship graph visualization** |
| ![Admin](docs/screenshots/admin-overview.png) | ![Mobile](docs/screenshots/mobile.png) |
| **Admin console** | **Mobile /m** |

## 🚀 Quick Start

```bash
git clone https://github.com/weed33834/deadman.git
cd deadman
pip install -e .[all]            # or minimal: pip install -e .

# configure LLM
cp .env.example .env
# edit .env: LLM_PROVIDER=openai  LLM_MODEL=gpt-4o  LLM_API_KEY=sk-...

# start the web app (conversation-first UI + admin console)
uvicorn deadman.web.app:app --host 0.0.0.0 --port 8002
```

Open `http://localhost:8002` — the chat is the primary interface. The admin console is at `/admin`.

### Run tests

```bash
python -m pytest            # 2926 passed
```

## 🗨 Conversation Commands

Type or speak any of these in the chat (also available on mobile `/m`):

| Category | Commands |
|---|---|
| Config | `/prompt` `/expert` `/skill` |
| Info | `/hotline` `/institution` `/custom` `/family` |
| Business | `/vault` `/note` `/docs` `/switch` `/task` `/cases` `/letters` `/score` `/support` |
| Create | `/memorial` `/plot` `/image` `/browse` `/canvas` |
| Help | `/help` `/manual` |

> Or just ask in natural language — e.g. "How to claim funeral allowance in Beijing?" or "Write a eulogy for my father who loved reading."

## 🏗 Architecture

10-layer architecture (see `docs/`):

| Layer | Stack |
|---|---|
| L1 LLM | OpenAI / Anthropic / DeepSeek / Qwen / Zhipu / Ollama / vLLM |
| L2 Interface | retry · fallback · streaming · token tracking |
| L3 Prompt | role templates · rules (L0–L8) · output parsing |
| L4 Agent | LangGraph · ReAct · reflexion · termination conditions |
| L5 Tools | MCP server & client · sandbox code execution · tool registry |
| L6 Memory | working/episodic/semantic/procedural · vector store · knowledge graph |
| L7 Orchestration | multi-agent · handoff · planner · debate |
| L8 API | FastAPI · SSE streaming · auth · rate limit |
| L9 Frontend | chat-first SPA · mobile `/m` · admin console |
| L10 Infra | Docker · monitoring · tracing · IAM · i18n · alerts |

## 📚 Documentation

- [CHANGELOG](CHANGELOG.md) · [SECURITY](SECURITY.md) · [CONTRIBUTING](CONTRIBUTING.md)
- [Admin & features](docs/ADMIN.md) · [Conversation commands](docs/CHAT_COMMANDS.md) · [Deployment](docs/DEPLOYMENT.md) · [Quick Start](docs/QUICKSTART.md)
- [Brand / Logo](BRAND.md) · [Platforms](PLATFORMS.md)

## 🔍 Discoverability

**Topics / keywords**: `end-of-life` · `funeral` · `aftercare` · `digital legacy` · `grief support` · `medical navigation` · `folk customs` · `kinship graph` · `multi-agent` · `LLM` · `RAG` · `MCP` · `LangGraph` · `FastAPI` · `conversational AI` · `voice AI`

To make this repo easy to find, add these as **repository topics/tags** on GitHub/GitCode/Gitee.

## 📦 Architecture Highlights (v5.4)

- **Conversation-first**: everything invocable from chat (25+ commands + voice + natural language).
- **Folk-custom & kinship**: `/custom` rules engine + `/family` SVG kinship graph.
- **Universal agent stack**: MCP client, sandbox charts, file parsing, export, image gen, scheduled tasks, IAM, i18n, traces, alerts, error codes.
- **10-layer clean architecture**, `2926` passing tests, zero shells.

## License

MIT — see [LICENSE](LICENSE).

---

*Made with care for families navigating life's hardest moments.*
