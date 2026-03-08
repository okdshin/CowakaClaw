# cowaka-claw

MCP対応のAIエージェントフレームワーク。セッション管理・cronスケジューリング・複数UIに対応。

## 特徴

- **マルチUI**: CLI / Slack / OpenAI互換HTTP API
- **セッション永続化**: 会話履歴をJSONLファイルに保存。プロセス再起動後も継続できる
- **cronスケジューリング**: 一度限り・定期実行・固定間隔の3種類のスケジュールをサポート
- **MCP統合**: [Model Context Protocol](https://modelcontextprotocol.io/) 経由でツールを追加できる
- **ストリーミング**: テキストをリアルタイムに逐次出力

## インストール

```bash
pip install -e .

# Slack UI を使う場合
pip install -e ".[slack]"

# HTTP API UI を使う場合
pip install -e ".[api]"

# すべて
pip install -e ".[all]"
```

## クイックスタート

```bash
export OPENAI_API_KEY=sk-...

# CLIで起動（モデルを対話的に選択）
cowaka-claw

# モデルを指定して起動
cowaka-claw --model gpt-4o
```

起動すると `> ` プロンプトが表示され、チャットできる。`/new` と入力するとセッションをリセットする。

## 設定

### エージェントの人格・記憶

| ファイル | 内容 |
|---------|------|
| `workspace/SOUL.md` | エージェントの人格・基本指示 |
| `workspace/MEMORY.md` | セッション間で共有する記憶（エージェントが自ら更新） |

`--workspace` オプションでディレクトリを変更できる。

### MCP

`mcp_config.json` にMCPサーバーを設定する。

```json
{
  "mcpServers": {
    "サーバー名": {
      "command": "コマンド",
      "args": ["引数"]
    }
  }
}
```

`--mcp-config` オプションで設定ファイルのパスを変更できる。

## UI

### CLI

```bash
cowaka-claw --ui cli
```

標準入力から1行ずつ読み込む。ストリーミング出力に対応。

### Slack

```bash
export COWAKA_CLAW_SLACK_BOT_TOKEN=xoxb-...
export COWAKA_CLAW_SLACK_APP_TOKEN=xapp-...

cowaka-claw --ui slack --slack-channel C1234567890
```

Socket Mode で接続する。メンション（`@エージェント名`）とDMに対応。スレッドは独立したセッションとして管理される。

### HTTP API（Chat Completions 互換）

```bash
cowaka-claw --ui openai_api_chat_completions --api-port 8000
```

OpenAI Chat Completions API と互換性のあるエンドポイントを提供する。

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "こんにちは"}]
  }'
```

リクエストごとに独立したセッションキーを生成する（ステートレス）。ストリーミング（`"stream": true`）に対応。

### HTTP API（Responses API 互換）

```bash
cowaka-claw --ui openai_api_responses --api-port 8000
```

OpenAI Responses API と互換性のあるエンドポイントを提供する。`previous_response_id` によるサーバーサイドのセッション継続に対応。

```bash
# 新規セッション
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "input": "こんにちは"}'

# 続きのセッション
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "input": "続けて", "previous_response_id": "resp_xxx"}'
```

## cronスケジューリング

エージェントに以下のツールが組み込まれており、LLMがユーザーの指示に応じてスケジュールを登録・削除する。

| ツール | 説明 | スケジュール指定例 |
|--------|------|------------------|
| `cron_job_add_at` | 一度限りの実行 | `"2026-03-01T09:00:00"` / `"30m"` / `"2h"` |
| `cron_job_add_cron` | cron式による定期実行 | `"0 9 * * 1-5"`（平日9時） |
| `cron_job_add_every` | 固定間隔の定期実行 | `3600`（秒） |
| `cron_job_delete` | ジョブのキャンセル | job_id を指定 |

ジョブの定義は `base_dir/cron/jobs.json` に保存され、プロセス再起動後も引き継がれる。

## コマンドラインオプション

```
--ui                  UI種別 (cli / slack / openai_api_chat_completions / openai_api_responses)
--model               使用するモデル名（未指定時は対話的に選択）
--base-dir            セッション・cronジョブの保存ディレクトリ（デフォルト: ./base_dir）
--workspace           SOUL.md / MEMORY.md のディレクトリ（デフォルト: ./workspace）
--mcp-config          MCP設定ファイルのパス（デフォルト: ./mcp_config.json）
--max-tool-iterations ツール呼び出しの最大反復回数（デフォルト: 無制限）
--llm-timeout-sec     LLM API呼び出しのタイムアウト秒数（デフォルト: なし）
--log-level           ログレベル (DEBUG / INFO / WARNING / ERROR、デフォルト: INFO)
--api-host            APIサーバーのホスト（デフォルト: 0.0.0.0）
--api-port            APIサーバーのポート（デフォルト: 8000）
--slack-channel       SlackのデフォルトチャンネルID（--ui slack 時に必須）
```

環境変数 `COWAKA_CLAW_OPENAI_MODEL` でデフォルトモデルを設定できる。

## 開発

```bash
pip install -e ".[all,dev]"

# テスト
pytest

# リント
ruff check src tests
```

## データ構造

```
base_dir/
├── agents/main/sessions/
│   ├── sessions.json       # session_key → session_id のマッピング
│   └── {session_id}.jsonl  # 会話履歴（1行1メッセージ）
└── cron/
    └── jobs.json           # cronジョブ定義

workspace/
├── SOUL.md    # エージェントの人格・基本指示
└── MEMORY.md  # エージェントの記憶
```
