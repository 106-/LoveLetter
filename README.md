# LoveLetter

## プレイ画面

![](https://i.imgur.com/0oSAizc.png)

## 必要環境
- `Python 3.11+`
- `uv`
- `Node.js 20+` と `npm`
- `make`

## 起動（ローカル）
1. 依存関係をインストールします。
```bash
uv sync --frozen
cd frontend && npm install && cd ..
```

2. サーバーを起動します。
```bash
make run
```

3. ブラウザで開きます。
```text
http://localhost:8000
```

## 補助コマンド
```bash
make format
make test
make ui-build
make ui-dev
```

## AIプレイヤー（LiteLLM）
- `.env` で設定管理できます。初回は次を実行してください。
```bash
cp .env.example .env
```
- ロビー画面でホストが `AIプレイヤーを追加` から `OpenAI / Anthropic / Gemini / xAI` を選んで追加できます。
- APIキーは利用プロバイダに応じて環境変数を設定してください。
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY` または `GOOGLE_API_KEY`
- `XAI_API_KEY`
- AIはサーバー側で保持する直近の行動ログを参照して合法手を選びます。LLM応答に失敗した場合は合法手からランダムでフォールバックします。
