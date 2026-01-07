# 食堂混雑検知システム v3.3

RTSPカメラを使用した食堂の混雑状況をリアルタイムで検知・表示するシステムです。

## 対象環境

- **ハードウェア**: Core i3-10105T / 8GB RAM / GPU無し
- **カメラ**: 640×480 @ 30fps (RTSP)

## 主な機能

- YOLOv8による人物検知（CPU最適化）
- リアルタイム混雑レベル表示（低/中/高）
- Web UI によるモニタリング
- 三段防波堤設計によるRTSP接続の堅牢性確保

## クイックスタート

### 1. 環境変数の設定

```bash
cp .env.example .env
# .env を編集してRTSP_URLを設定
```

### 2. Docker Composeで起動

```bash
docker compose up -d
```

### 3. アクセス

- **Web UI**: http://localhost:8000
- **ヘルスチェック**: http://localhost:8000/api/health
- **混雑状況API**: http://localhost:8000/api/crowding

## API エンドポイント

| エンドポイント | 説明 |
|---------------|------|
| `GET /` | Web UI |
| `GET /api/health` | ヘルスチェック（Docker healthcheck用） |
| `GET /api/crowding` | 現在の混雑状況 |
| `GET /api/crowding/history` | 混雑履歴 |
| `GET /api/frame` | 現在のフレーム（JPEG） |
| `GET /api/frame/annotated` | 検知結果描画済みフレーム |

## 環境変数

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `RTSP_URL` | - | RTSPカメラURL（必須） |
| `IMGSZ` | 416 | YOLO推論画像サイズ |
| `PROCESS_FPS` | 3 | 処理FPS |
| `CONFIDENCE_THRESHOLD` | 0.5 | 検知信頼度閾値 |

## アーキテクチャ

### 三段防波堤設計

RTSPストリームの信頼性を確保するため、以下の防波堤を実装：

1. **第1防波堤**: バッファ滞留対策（常時grabで最新フレーム上書き）
2. **第2防波堤**: read間隔異常検知（前回read成功から5秒超で再接続）
3. **第3防波堤**: read()ブロック検知（Watchdog: 10秒更新なしでスレッド再起動）
4. **第4防波堤**: ゾンビスレッド対策（Watchdog再起動3回/時超でコンテナ再起動誘発）

### ヘルスチェック連携

`system_halted=True` になると `/api/health` が503を返し、Docker healthcheckが失敗→コンテナ自動再起動でゾンビスレッドを浄化。

## 開発

### ローカル実行

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### ビルド

```bash
docker compose build
```

## ライセンス

内部利用限定

---

**デジタルコミュニケーション部** | 2026年1月5日


