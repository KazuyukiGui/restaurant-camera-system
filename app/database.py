# 食堂混雑検知システム - データベースモジュール
# SQLite接続時に check_same_thread=False を設定（仕様書要件）

import os
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from sqlalchemy import create_engine, Column, Integer, Float, DateTime, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# JSTタイムゾーン（UTC+9）
JST = timezone(timedelta(hours=9))

def jst_now():
    """JST（日本標準時）の現在時刻を取得（naive datetimeとして返す）"""
    # SQLiteはタイムゾーン情報を保持しないため、naive datetimeとして返す
    return datetime.now(JST).replace(tzinfo=None)

# データベースパス
DATABASE_PATH = os.getenv('DATABASE_PATH', '/app/data/cafeteria.db')
DATABASE_URL = f'sqlite:///{DATABASE_PATH}'

# SQLite接続時に check_same_thread=False を必須設定
engine = create_engine(
    DATABASE_URL,
    connect_args={'check_same_thread': False},  # 仕様書要件
    echo=False
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class CrowdingRecord(Base):
    """混雑度記録テーブル"""
    __tablename__ = 'crowding_records'
    
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=jst_now, index=True)
    person_count = Column(Integer, nullable=False)
    crowding_level = Column(String(20), nullable=False)  # 'low', 'medium', 'high'
    confidence = Column(Float, nullable=True)
    
    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'person_count': self.person_count,
            'crowding_level': self.crowding_level,
            'confidence': self.confidence,
        }


class SystemLog(Base):
    """システムログテーブル"""
    __tablename__ = 'system_logs'
    
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=jst_now, index=True)
    level = Column(String(20), nullable=False)  # 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
    message = Column(String(500), nullable=False)
    component = Column(String(50), nullable=True)  # 'rtsp', 'detector', 'api', etc.


def init_db():
    """データベース初期化"""
    # データディレクトリの作成
    db_dir = os.path.dirname(DATABASE_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    
    # テーブル作成
    Base.metadata.create_all(bind=engine)


def get_db():
    """データベースセッションを取得（FastAPI依存性注入用）"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def get_db_session():
    """
    データベースセッションを取得（スレッド/バックグラウンド処理用）

    with文で使用することでセッションの確実なクローズを保証する。
    FastAPIの依存性注入が使えないスレッド内での使用を想定。

    Usage:
        with get_db_session() as db:
            save_crowding_record(db, ...)
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def save_crowding_record(db, person_count: int, crowding_level: str, confidence: float = None):
    """混雑度記録を保存"""
    record = CrowdingRecord(
        person_count=person_count,
        crowding_level=crowding_level,
        confidence=confidence
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def get_recent_records(db, limit: int = 100):
    """最近の記録を取得"""
    return db.query(CrowdingRecord).order_by(
        CrowdingRecord.timestamp.desc()
    ).limit(limit).all()


def save_system_log(db, level: str, message: str, component: str = None):
    """システムログを保存"""
    log = SystemLog(
        level=level,
        message=message[:500],  # 最大500文字に制限
        component=component
    )
    db.add(log)
    db.commit()
    return log


