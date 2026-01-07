# 食堂混雑検知システム v3.3
# Core i3-10105T / 8GB RAM / GPU無し 環境向け

FROM python:3.11-slim

# 環境変数
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# システム依存パッケージ
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 作業ディレクトリ
WORKDIR /app

# Python依存パッケージ
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Note: YOLOモデルは初回起動時に自動ダウンロードされます

# アプリケーションコード
COPY app/ ./app/

# データディレクトリ作成
RUN mkdir -p /app/data

# 非rootユーザー
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# ポート
EXPOSE 8000

# ヘルスチェック用にcurlを使用
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# 起動コマンド
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

