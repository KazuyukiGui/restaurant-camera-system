---
theme: default
title: 食堂混雑検知システム v3.5
info: |
  RTSPカメラとYOLO11sを使用した
  リアルタイム混雑検知システム
class: text-center
highlighter: shiki
drawings:
  persist: false
transition: slide-left
mdc: true
---

# 食堂混雑検知システム

## v3.5 - CPU環境対応 AIモニタリング

<div class="pt-12">
  <span class="px-2 py-1">
    RTSPカメラ × YOLO11s × FastAPI
  </span>
</div>

---

# システム概要

<div class="grid grid-cols-2 gap-4">
<div>

## 目的

RTSPカメラを使用して、**食堂の混雑状況**をリアルタイムで検知・表示するシステム

## ターゲット環境

- **CPU**: Intel Core i3-10105T
- **RAM**: 8GB
- **GPU**: なし（CPU推論のみ）

</div>
<div>

## 主な機能

- YOLO11sによる人物検知
- リアルタイム混雑レベル表示
- Web UIダッシュボード
- 混雑履歴の記録・分析
- RESTful API
- 三段防波堤設計による堅牢性

</div>
</div>

---

# 技術スタック

<div class="grid grid-cols-3 gap-4">
<div>

### バックエンド

- **FastAPI** 0.109.0
- **Uvicorn** 0.27.0
- **Gunicorn** 21.2.0
- **Python** 3.11

</div>
<div>

### AI / 画像処理

- **YOLO11s** (Ultralytics)
- **OpenVINO** 2024.4.0
- **OpenCV** 4.9.0

</div>
<div>

### インフラ

- **Docker Compose**
- **SQLite** + SQLAlchemy
- **FFmpeg**
- **mediamtx** (RTSP)

</div>
</div>

---
layout: center
---

# アーキテクチャ

---

# システム構成図

```
┌─────────────────────────────────────────────────────────────┐
│                  Docker Compose (3コンテナ)                   │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────┐     ┌───────────────────┐                 │
│  │ RTSPサーバー  │     │ USBカメラ → RTSP  │                 │
│  │ (mediamtx)   │     │    (FFmpeg)       │                 │
│  └──────┬───────┘     └─────────┬─────────┘                 │
│         └───────────────────────┼──────────┐                 │
│                                 ▼          ▼                 │
│  ┌───────────────────────────────────────────────────────┐  │
│  │        食堂混雑検知アプリ (Python/FastAPI)             │  │
│  ├───────────────────────────────────────────────────────┤  │
│  │  RTSPCapture → PersonDetector → DB記録                │  │
│  └───────────────────────────────────────────────────────┘  │
│                          │                                    │
│                          ▼                                    │
│              ┌─────────────────────────┐                     │
│              │   SQLite Database       │                     │
│              │   /app/data/cafeteria.db│                     │
│              └─────────────────────────┘                     │
└─────────────────────────────────────────────────────────────┘
```

---

# YOLO11s 人物検知

<div class="grid grid-cols-2 gap-8">
<div>

## モデル仕様

| 項目 | 値 |
|------|-----|
| モデル | YOLO11s |
| パラメータ数 | 9.4M |
| COCO mAP | 47.0% |
| 推論形式 | OpenVINO |

</div>
<div>

## 混雑レベル判定

| レベル | 人数 | 表示 |
|--------|------|------|
| 低 | 0-4人 | 空き |
| 中 | 5-7人 | やや混雑 |
| 高 | 8人以上 | 混雑 |

</div>
</div>

---

# 三段防波堤設計

RTSP接続の堅牢性を確保する多層防御

| 防波堤 | 検知対象 | トリガー条件 | 対応 |
|--------|---------|-------------|------|
| **第1** | バッファ滞留 | 常時grab | 最新フレーム上書き |
| **第2** | read()遅延 | 5秒超 | 再接続（指数バックオフ） |
| **第3** | read()ブロック | 10秒更新なし | スレッド再起動 |
| **第4** | ゾンビスレッド | 3回/時超 | コンテナ再起動誘発 |

---

# API エンドポイント

<div class="grid grid-cols-2 gap-4">
<div>

### 基本API

| エンドポイント | 説明 |
|---------------|------|
| `GET /` | Web UI |
| `GET /api/health` | ヘルスチェック |
| `GET /api/crowding` | 現在の混雑状況 |
| `GET /api/frame` | 現在のフレーム |

</div>
<div>

### 分析API

| エンドポイント | 説明 |
|---------------|------|
| `GET /api/crowding/history` | 混雑履歴 |
| `GET /api/crowding/timeline` | 時間帯別サマリー |
| `GET /api/crowding/weekly` | 週間データ |
| `GET /api/crowding/export` | CSVエクスポート |

</div>
</div>

---

# プロジェクト構成

```
restaurant-camera-system/
├── README.md                 # プロジェクト説明
├── spec.md                   # 詳細仕様書
├── requirements.txt          # Python依存パッケージ
├── Dockerfile                # コンテナビルド設定
├── docker-compose.yml        # マルチコンテナ管理
├── .env.example              # 環境変数テンプレート
└── app/
    ├── main.py               # FastAPIメインアプリ
    ├── detector.py           # YOLO11人物検知
    ├── rtsp_capture.py       # RTSPキャプチャ
    └── database.py           # SQLAlchemy ORM
```

---

# 主要モジュール

<div class="grid grid-cols-2 gap-6">
<div>

### main.py
- FastAPIアプリケーション
- ライフサイクル管理
- 常時監視ループ
- REST APIエンドポイント

### detector.py
- YOLO11sモデル管理
- OpenVINO形式変換
- 人物検知・混雑判定

</div>
<div>

### rtsp_capture.py
- スレッドベースフレーム取得
- 三段防波堤実装
- 指数バックオフ再接続

### database.py
- SQLAlchemy ORM
- CrowdingRecord テーブル
- SystemLog テーブル

</div>
</div>

---

# Web UI ダッシュボード

<div class="grid grid-cols-2 gap-8">
<div>

## 特徴

- モバイルファースト設計
- 直感的な混雑レベル表示
- 週間トレンドグラフ
- ライブカメラ映像
- 2秒ごとのリアルタイム更新
- フルスクリーン対応

</div>
<div>

## 混雑表示

```
┌──────────────────┐
│                  │
│       空き       │
│                  │
│   現在 3 人検知   │
│                  │
└──────────────────┘
```

</div>
</div>

---

# 環境変数設定

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `RTSP_URL` | - | RTSPカメラURL（必須） |
| `IMGSZ` | 416 | YOLO推論画像サイズ |
| `PROCESS_FPS` | 3 | 処理FPS |
| `CONFIDENCE_THRESHOLD` | 0.5 | 検知信頼度閾値 |
| `YOLO_MODEL` | yolo11s.pt | モデルファイル |
| `DATABASE_PATH` | /app/data/cafeteria.db | DB格納先 |
| `RECORD_INTERVAL` | 60 | DB記録間隔（秒） |

---

# Docker設定

<div class="grid grid-cols-2 gap-8">
<div>

## リソース制限

- **ベースイメージ**: python:3.11-slim
- **メモリ制限**: 4GB
- **メモリ予約**: 2GB

</div>
<div>

## ヘルスチェック

- **間隔**: 30秒
- **タイムアウト**: 10秒
- **開始待機**: 60秒
- **ログ**: JSON形式、10MB × 3

</div>
</div>

---

# 最近の更新履歴

### v3.5 (2026-01-09)
- DBセッションリーク修正
- 長期稼働時の安定性向上

### v3.4 (2026-01-09)
- YOLO11s採用（9.4M params, mAP 47.0%）
- 顔ぼかし機能を削除

### v3.3 (2026-01-05)
- 第2防波堤の論理バグ修正
- ゾンビスレッド対策実装

---
layout: center
class: text-center
---

# まとめ

<div class="grid grid-cols-2 gap-12 pt-8">
<div class="text-left">

### 堅牢性
三段防波堤設計でRTSP接続信頼性を確保

### スケーラビリティ
RESTful APIで複数アプリ連携可能

### 可視化
Web UIで直感的に混雑状況を把握

</div>
<div class="text-left">

### 分析機能
履歴データから時系列分析・CSVエクスポート

### 運用性
Dockerコンテナで簡単デプロイ

### CPU最適化
GPU不要でIntel CPU環境に最適化

</div>
</div>

---
layout: center
class: text-center
---

# Thank You

食堂混雑検知システム v3.5

<div class="pt-12">
  <span class="text-sm opacity-50">
    Powered by YOLO11s + FastAPI + Docker
  </span>
</div>
