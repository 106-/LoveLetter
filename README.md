# LoveLetter

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
