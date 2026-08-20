# deadman

<p align="center"><img src="assets/logo.png" alt="deadman Logo" width="360"></p>

<p align="center">
  <a href="README.md"><img alt="English" src="https://img.shields.io/badge/English-README-blue"></a>
  <a href="README.zh-CN.md"><img alt="中文" src="https://img.shields.io/badge/%E4%B8%AD%E6%96%87-README.zh--CN-red"></a>
  <a href="#"><img alt="日本語" src="https://img.shields.io/badge/%E6%97%A5%E6%9C%AC%E8%AA%9E-%E6%9C%AC%E3%83%9A%E3%83%BC%E3%82%B8-green"></a>
</p>

> **deadman** — **生前準備と身後手続きの AI 案内アシスタント**。**会話最優先 / かんたん操作**：話すか入力するだけで、AI エージェントが手順を案内します。**民俗・習慣カスタマイズ** と **家族系譜グラフ可視化** に対応。特定ベンダーに依存せず、あらゆるエージェント基盤で動作します。

> **法人向け（To B）**：deadman は**マルチテナント機構プラットフォーム**として提供されます——葬儀 / 保険 / 法務 / 事後手続きサービス機関は**機構ワークベンチ**（`/org`）で顧客・案件・監査ログ・ナレッジベース・チームロールを管理できます。**ライセンス認証**（30 日試用、失効後は読み取り専用）と**データエクスポート**（CSV/JSON/zip）に対応。テナントデータは `resolve_tenant_path()` により完全に分離されます。

## ✨ 主な機能

- 🗣️ **会話最優先**：25+ コマンド・音声・自然言語で全機能を完結（`/help`）
- 🪦 **終活手続き**：死亡診断書から遺産整理まで 9 段階（中国 34 省 + 米・日）
- 🏥 **医療ナビゲーション**：医療保険・難病給付・終末期ケア
- 📜 **民俗ルールエンジン**：葬儀・婚礼の慣習、**頭七〜七七** の法要
- 👨‍👩‍👧 **家族系譜グラフ**：家族関係を SVG で可視化
- 🔐 **デジタル遺産**：暗号化アセット登録・受益者指定
- 🕯 **AI 弔文**：弔辞・訃報・御礼状・墓誌銘
- 🛠 **汎用エージェント能力**：10 層アーキテクチャ、RAG、MCP、サンドボックス描画、音声、ファイル解析、画像生成、IAM、i18n
- 🌐 **多言語**：English / 中文 / 日本語

## 🖥 スクリーンショット

| | |
|---|---|
| ![Chat](docs/screenshots/chat-home.png) | ![Commands](docs/screenshots/chat-command.png) |
| 会話メイン | /help とコマンド |
| ![Customs](docs/screenshots/customs.png) | ![Kinship](docs/screenshots/kinship-graph.png) |
| 民俗ルール | 家族系譜グラフ |
| ![Admin](docs/screenshots/admin-overview.png) | ![Mobile](docs/screenshots/mobile.png) |
| 管理コンソール | モバイル /m |

## 🚀 クイックスタート

```bash
git clone https://github.com/weed33834/deadman.git
cd deadman && pip install -e .[all]
cp .env.example .env   # LLM_PROVIDER / LLM_MODEL / LLM_API_KEY を設定
uvicorn deadman.web.app:app --host 0.0.0.0 --port 8002
# http://localhost:8002（会話がメイン、管理は /admin）
```

## 🗨 会話コマンド（モバイル /m でも可）

設定 `/prompt` `/expert` `/skill` · 検索 `/hotline` `/institution` `/custom` `/family`
業務 `/vault` `/note` `/docs` `/switch` `/task` `/cases` `/letters` `/score` `/support`
作成 `/memorial` `/plot` `/image` `/browse` `/canvas` · ヘルプ `/help` `/manual`

> 自然言語でも可：例「北京の葬儀費用は？」「読書好きだった父への弔辞を書いて」。

## 📚 ドキュメント

[管理コンソール](docs/ADMIN.md) · [コマンドマニュアル](docs/CHAT_COMMANDS.md) · [デプロイ](docs/DEPLOYMENT.md) · [ブランド/Logo](BRAND.md) · [変更履歴](CHANGELOG.md)

詳細は [README.md](README.md) を参照。
