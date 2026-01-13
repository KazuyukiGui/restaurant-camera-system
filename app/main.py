# 食堂混雑検知システム v3.5
# Core i3-10105T / 8GB RAM / GPU無し 環境向け
# YOLO11s使用（9.4Mパラメータ、COCO mAP 47.0%）

import cv2
import os
import io
import time
import logging
import threading
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import secrets
from fastapi import FastAPI, Response, Depends, HTTPException, status
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

# モジュールインポート
from app.rtsp_capture import RTSPCapture
from app.detector import PersonDetector
from app.database import init_db, get_db, get_db_session, save_crowding_record, save_system_log, get_recent_records, CrowdingRecord

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 環境変数ロード
load_dotenv()

# グローバルインスタンス
rtsp_capture = None
detector = None
latest_result = {'person_count': 0, 'crowding_level': 'low', 'confidence': 0.0}
latest_result_lock = threading.Lock()

# ===============================
# 管理者認証設定
# ===============================
security = HTTPBasic()

# 環境変数から認証情報を取得（デフォルト: admin/admin）
ADMIN_USER = os.getenv('ADMIN_USER', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin')

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """管理者認証（カメラ映像へのアクセス制御）"""
    correct_username = secrets.compare_digest(credentials.username, ADMIN_USER)
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)

    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="認証に失敗しました",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 起動時処理
    global rtsp_capture, detector
    
    # DB初期化
    init_db()
    
    # モデルロード
    detector = PersonDetector()
    
    # RTSPキャプチャ開始
    rtsp_url = os.getenv('RTSP_URL')
    if rtsp_url:
        rtsp_capture = RTSPCapture(rtsp_url)
        rtsp_capture.start()
        
        # 監視・記録スレッド開始
        threading.Thread(target=monitoring_loop, daemon=True).start()
    else:
        logger.warning('RTSP_URL not set - capture disabled')
    
    yield
    
    # 終了時処理
    if rtsp_capture:
        rtsp_capture.stop()

app = FastAPI(lifespan=lifespan)

def monitoring_loop():
    """常時監視・記録ループ"""
    global latest_result
    
    logger.info("Monitoring loop started")
    
    # 記録間隔（秒）
    RECORD_INTERVAL = int(os.getenv('RECORD_INTERVAL', '10'))
    last_record_time = 0
    
    # 推論レート制御
    process_fps = int(os.getenv('PROCESS_FPS', '3'))
    process_interval = 1.0 / process_fps
    
    while True:
        start_time = time.time()
        
        if rtsp_capture and detector:
            # Watchdogチェック & 再起動処理
            if not rtsp_capture.is_healthy():
                logger.warning("RTSP Capture unhealthy - restarting")
                rtsp_capture.restart()
                time.sleep(5) # 再起動待機
                continue

            frame, delay, halted = rtsp_capture.get_frame()
            
            # システム停止中は何もしない
            if halted:
                time.sleep(5)
                continue
                
            if frame is not None:
                # 推論実行
                result = detector.process_frame(frame)
                
                # 結果更新
                with latest_result_lock:
                    latest_result = result
                    latest_result['delay_seconds'] = round(delay, 2)
                
                # DB記録（指定間隔ごと）
                current_time = time.time()
                if current_time - last_record_time >= RECORD_INTERVAL:
                    try:
                        # コンテキストマネージャでセッションを確実にクローズ
                        with get_db_session() as db:
                            save_crowding_record(
                                db,
                                result['person_count'],
                                result['crowding_level'],
                                result['confidence']
                            )
                        last_record_time = current_time
                    except Exception as e:
                        logger.error(f"DB recording failed: {e}")
        
        # FPS制御
        elapsed = time.time() - start_time
        wait = max(0, process_interval - elapsed)
        time.sleep(wait)


# ===============================
# API Endpoints
# ===============================

@app.get('/api/health')
def health_check():
    """ヘルスチェック"""
    if rtsp_capture is None:
        return {'status': 'unhealthy', 'reason': 'not_initialized'}
        
    stats = rtsp_capture.get_health_stats()
    
    # v3.3: system_haltedの場合は503を返す（Docker再起動誘発）
    if stats['system_halted']:
        return JSONResponse(
            status_code=503,
            content={
                'status': 'unhealthy',
                'reason': 'system_halted - container restart required',
                **stats
            }
        )
    
    # 正常時
    status = 'healthy' if stats['is_healthy'] else 'degraded'
    return {
        'status': status,
        **stats,
        'config': {
            'imgsz': int(os.getenv('IMGSZ', '416')),
            'process_fps': int(os.getenv('PROCESS_FPS', '3')),
        }
    }


@app.get('/api/crowding')
def get_crowding():
    """現在の混雑状況を取得"""
    with latest_result_lock:
        return {
            **latest_result,
            'system_halted': rtsp_capture.system_halted if rtsp_capture else False
        }


@app.get('/api/crowding/history')
def get_crowding_history(limit: int = 100, db: Session = Depends(get_db)):
    """混雑履歴を取得"""
    records = get_recent_records(db, limit=limit)
    return {'count': len(records), 'records': [r.to_dict() for r in records]}


@app.get('/api/crowding/timeline')
def get_crowding_timeline(hours: int = 6, db: Session = Depends(get_db)):
    """時間帯別の混雑状況サマリーを取得"""
    from datetime import datetime, timezone, timedelta
    from collections import defaultdict
    
    # JST設定
    JST = timezone(timedelta(hours=9))
    UTC = timezone.utc
    
    # 過去N時間のデータを取得
    records = get_recent_records(db, limit=1000)
    now = datetime.now(JST)
    cutoff = now - timedelta(hours=hours)
    
    # 時間帯ごとに集計
    hourly_data = defaultdict(list)
    migration_date_naive = datetime(2026, 1, 6, 0, 0, 0)
    
    for record in records:
        record_time = record.timestamp
        if record_time.tzinfo is None:
            if record_time < migration_date_naive:
                record_time = record_time.replace(tzinfo=UTC).astimezone(JST)
            else:
                record_time = record_time.replace(tzinfo=JST)
        else:
            record_time = record_time.astimezone(JST)
        
        if record_time >= cutoff:
            # 5分刻み
            minute = record_time.minute
            rounded_minute = (minute // 5) * 5
            time_key = f'{record_time.hour:02d}:{rounded_minute:02d}'
            hourly_data[time_key].append(record.person_count)
    
    # 各時間帯の平均と最大を計算（11:00-21:55）
    timeline = []
    for hour in range(11, 22):
        for minute in range(0, 60, 5):
            time_key = f'{hour:02d}:{minute:02d}'
            
            if time_key in hourly_data:
                counts = hourly_data[time_key]
                avg_count = sum(counts) / len(counts)
                max_count = max(counts)
                timeline.append({
                    'hour': time_key,
                    'avg_count': round(avg_count, 1),
                    'max_count': max_count,
                    'samples': len(counts)
                })
            else:
                timeline.append({
                    'hour': time_key,
                    'avg_count': 0,
                    'max_count': 0,
                    'samples': 0
                })
    
    current_minute = (now.minute // 5) * 5
    current_hour_jst = f'{now.hour:02d}:{current_minute:02d}'
    return {'timeline': timeline, 'current_hour': current_hour_jst}


@app.get('/api/crowding/weekly')
def get_crowding_weekly(days: int = 7, db: Session = Depends(get_db)):
    """過去N日間の週間混雑データを取得（11時-21時）"""
    from datetime import datetime, timezone, timedelta
    from collections import defaultdict
    
    JST = timezone(timedelta(hours=9))
    UTC = timezone.utc
    
    # 過去N日間のデータを取得
    records = get_recent_records(db, limit=10000)
    now = datetime.now(JST)
    cutoff = now - timedelta(days=days)
    
    # 日付ごと、時間帯ごとに集計
    daily_hourly_data = defaultdict(lambda: defaultdict(list))
    migration_date_naive = datetime(2026, 1, 6, 0, 0, 0)
    
    for record in records:
        record_time = record.timestamp
        if record_time.tzinfo is None:
            if record_time < migration_date_naive:
                record_time = record_time.replace(tzinfo=UTC).astimezone(JST)
            else:
                record_time = record_time.replace(tzinfo=JST)
        else:
            record_time = record_time.astimezone(JST)
        
        if record_time >= cutoff:
            date_key = record_time.strftime('%Y-%m-%d')
            hour = record_time.hour
            if 11 <= hour <= 21:
                minute = record_time.minute
                rounded_minute = (minute // 5) * 5
                time_key = f'{hour:02d}:{rounded_minute:02d}'
                daily_hourly_data[date_key][time_key].append(record.person_count)
    
    sorted_dates = sorted(daily_hourly_data.keys(), reverse=True)
    
    weekly_data = []
    for date_key in sorted_dates[:days]:
        date_obj = datetime.strptime(date_key, '%Y-%m-%d').replace(tzinfo=JST)
        weekday_num = date_obj.weekday()
        weekday_names = ['月', '火', '水', '木', '金', '土', '日']
        weekday_name = weekday_names[weekday_num]
        
        days_diff = (now.date() - date_obj.date()).days
        if days_diff == 0:
            date_label = '本日'
        elif days_diff == 1:
            date_label = '昨日'
        elif days_diff == 2:
            date_label = '一昨日'
        else:
            date_label = date_obj.strftime('%m/%d')
        
        hourly_data = []
        for hour in range(11, 22):
            for minute in range(0, 60, 5):
                time_key = f'{hour:02d}:{minute:02d}'
                
                if time_key in daily_hourly_data[date_key]:
                    counts = daily_hourly_data[date_key][time_key]
                    avg_count = sum(counts) / len(counts)
                    max_count = max(counts)
                    hourly_data.append({
                        'hour': time_key,
                        'avg_count': round(avg_count, 1),
                        'max_count': max_count,
                        'samples': len(counts)
                    })
                else:
                    hourly_data.append({
                        'hour': time_key,
                        'avg_count': 0,
                        'max_count': 0,
                        'samples': 0
                    })
        
        weekly_data.append({
            'date': date_key,
            'date_label': date_label,
            'weekday': weekday_name,
            'hourly_data': hourly_data
        })
    
    return {'weekly_data': weekly_data, 'current_date': now.strftime('%Y-%m-%d')}


@app.get('/api/crowding/export')
def export_crowding_csv(date: str = None, days: int = 7, db: Session = Depends(get_db)):
    """CSVエクスポート"""
    import csv
    from datetime import datetime, timezone, timedelta
    
    JST = timezone(timedelta(hours=9))
    
    query = db.query(CrowdingRecord).order_by(CrowdingRecord.timestamp.asc())
    
    if date:
        try:
            target_date = datetime.strptime(date, '%Y-%m-%d')
            next_date = target_date + timedelta(days=1)
            query = query.filter(CrowdingRecord.timestamp >= target_date, CrowdingRecord.timestamp < next_date)
        except ValueError:
            raise HTTPException(status_code=400, detail='Invalid date format')
    else:
        cutoff = datetime.now(JST).replace(tzinfo=None) - timedelta(days=days)
        query = query.filter(CrowdingRecord.timestamp >= cutoff)
    
    records = query.all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['timestamp', 'person_count', 'crowding_level', 'confidence'])
    
    for record in records:
        writer.writerow([
            record.timestamp.strftime('%Y-%m-%d %H:%M:%S') if record.timestamp else '',
            record.person_count,
            record.crowding_level,
            round(record.confidence, 3) if record.confidence else ''
        ])
    
    output.seek(0)
    filename = f'crowding_{date}.csv' if date else f'crowding_last_{days}days.csv'
    
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode('utf-8-sig')),
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@app.get('/api/crowding/stats')
def get_crowding_stats(days: int = 7, db: Session = Depends(get_db)):
    """統計情報"""
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import func
    
    JST = timezone(timedelta(hours=9))
    cutoff = datetime.now(JST).replace(tzinfo=None) - timedelta(days=days)
    
    stats = db.query(
        func.count(CrowdingRecord.id).label('total_records'),
        func.avg(CrowdingRecord.person_count).label('avg_count'),
        func.max(CrowdingRecord.person_count).label('max_count'),
        func.min(CrowdingRecord.person_count).label('min_count')
    ).filter(CrowdingRecord.timestamp >= cutoff).first()
    
    level_counts = db.query(
        CrowdingRecord.crowding_level,
        func.count(CrowdingRecord.id)
    ).filter(CrowdingRecord.timestamp >= cutoff).group_by(CrowdingRecord.crowding_level).all()
    
    return {
        'period_days': days,
        'total_records': stats.total_records or 0,
        'avg_person_count': round(stats.avg_count, 1) if stats.avg_count else 0,
        'max_person_count': stats.max_count or 0,
        'min_person_count': stats.min_count or 0,
        'level_distribution': {level: count for level, count in level_counts}
    }


@app.get('/api/frame')
def get_frame(username: str = Depends(verify_admin)):
    """現在のフレーム（認証必須）"""
    if rtsp_capture is None:
        raise HTTPException(status_code=503, detail='Camera not initialized')
    frame, delay, halted = rtsp_capture.get_frame()
    if frame is None:
        raise HTTPException(status_code=503, detail='No frame available')
    _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return StreamingResponse(
        io.BytesIO(buffer.tobytes()),
        media_type='image/jpeg',
        headers={'X-Delay-Seconds': str(round(delay, 2)), 'X-System-Halted': str(halted)}
    )

@app.get('/api/frame/annotated')
def get_annotated_frame(username: str = Depends(verify_admin)):
    """描画済みフレーム（認証必須）"""
    if rtsp_capture is None or detector is None:
        raise HTTPException(status_code=503, detail='System not initialized')
    frame, delay, halted = rtsp_capture.get_frame()
    if frame is None:
        raise HTTPException(status_code=503, detail='No frame available')
    
    person_count, detections, confidence = detector.detect_persons(frame)
    annotated = detector.draw_detections(frame, detections)
    crowding_level = detector.get_crowding_level(person_count)
    
    # タイムスタンプとデバッグ情報の描画
    import datetime
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    info_text = f'People: {person_count} | Level: {crowding_level}'
    
    # テキスト描画 (背景付きで見やすく)
    cv2.putText(annotated, info_text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
    cv2.putText(annotated, now_str, (10, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
    cv2.putText(annotated, f"Delay: {delay:.2f}s", (10, 100), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    
    _, buffer = cv2.imencode('.jpg', annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return StreamingResponse(
        io.BytesIO(buffer.tobytes()),
        media_type='image/jpeg'
    )

# ===============================
# Web UI (Modern Mobile-First)
# ===============================

@app.get('/', response_class=HTMLResponse)
def index(username: str = Depends(verify_admin)):
    """モダン・モバイルファーストなダッシュボードUI（認証必須）"""
    html = '''
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>食堂混雑情報</title>
    <style>
        /* Base / Reset */
        :root {
            --bg: #f1f5f9;
            --bg-card: #ffffff;
            --text-main: #0f172a;
            --text-sub: #64748b;
            --border: #e2e8f0;
            --primary: #3b82f6;
            --green: #10b981; --green-bg: #ecfdf5; --green-border: #a7f3d0;
            --yellow: #f59e0b; --yellow-bg: #fffbeb; --yellow-border: #fde68a;
            --red: #ef4444; --red-bg: #fef2f2; --red-border: #fecaca;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: var(--bg);
            color: var(--text-main);
            line-height: 1.5;
            padding-bottom: 40px;
        }

        /* Container */
        .container {
            max-width: 800px;
            margin: 0 auto;
            padding: 16px;
        }

        /* Header */
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        h1 {
            font-size: 1.25rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .clock {
            font-family: monospace;
            font-weight: 600;
            color: var(--text-sub);
            font-size: 1.1rem;
        }

        /* Grid Layout */
        .grid {
            display: grid;
            gap: 16px;
            grid-template-columns: 1fr;
        }
        
        @media (min-width: 768px) {
            .grid {
                grid-template-columns: 1fr 1fr;
                grid-template-areas: 
                    "status status"
                    "graph graph"
                    "camera camera"
                    "info info";
            }
            .card-status { grid-area: status; }
            .card-graph { grid-area: graph; }
            .card-camera { grid-area: camera; }
            .card-info { grid-area: info; }
        }

        /* Cards */
        .card {
            background: var(--bg-card);
            border-radius: 16px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -1px rgba(0, 0, 0, 0.03);
            overflow: hidden;
            border: 1px solid var(--border);
        }
        
        .card-header {
            padding: 16px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .card-title {
            font-weight: 600;
            font-size: 0.95rem;
            color: var(--text-sub);
        }

        /* Status Hero (Level 1 Info) */
        .status-hero {
            padding: 24px;
            text-align: center;
            transition: all 0.3s ease;
        }
        .status-hero.low { background: var(--green-bg); color: #065f46; }
        .status-hero.medium { background: var(--yellow-bg); color: #92400e; }
        .status-hero.high { background: var(--red-bg); color: #991b1b; }

        .status-icon { font-size: 4rem; margin-bottom: 8px; display: block; }
        .status-label { font-size: 2rem; font-weight: 800; letter-spacing: 0.05em; margin-bottom: 4px; }
        .status-detail { font-size: 1rem; opacity: 0.9; }
        .status-count { font-size: 1.5rem; font-weight: 700; }

        /* Camera (Level 3 Info) - Compact */
        .camera-container {
            position: relative;
            background: #000;
            aspect-ratio: 16/9;
            max-height: 240px; /* 高さ制限 */
            margin: 0 auto;
            cursor: pointer;
        }
        .camera-container:fullscreen {
            max-height: none;
            width: 100vw;
            height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            background: #000;
        }
        .camera-container:fullscreen .camera-img {
            width: 100%;
            height: 100%;
            object-fit: contain;
        }
        .camera-img { width: 100%; height: 100%; object-fit: contain; }
        .camera-overlay {
            position: absolute; bottom: 8px; right: 8px;
            background: rgba(0,0,0,0.6); color: #fff;
            padding: 2px 6px; border-radius: 4px; font-size: 0.7rem;
            pointer-events: none;
        }
        .fullscreen-btn {
            position: absolute; top: 8px; right: 8px;
            background: rgba(0,0,0,0.6); color: #fff;
            border: none; padding: 4px 8px; border-radius: 4px;
            cursor: pointer; font-size: 0.8rem;
            display: flex; align-items: center; gap: 4px;
        }
        .fullscreen-btn:hover { background: rgba(0,0,0,0.8); }

        /* Weekly Graph (Level 2 Info) - Scrollable */
        .timeline-scroll {
            overflow-x: auto;
            position: relative;
            scrollbar-width: thin;
            background: #fff;
        }
        .timeline-content {
            min-width: 800px; /* Ensure scroll on mobile */
            padding: 10px 0;
            position: relative;
        }
        .timeline-header, .timeline-footer {
            height: 20px;
            position: relative;
            margin-left: 70px; /* label width */
            margin-right: 16px;
        }
        .timeline-scale-label {
            position: absolute; transform: translateX(-50%);
            font-size: 0.7rem; color: var(--text-sub);
        }
        .timeline-grid {
            position: absolute; top: 20px; bottom: 20px;
            left: 70px; right: 16px; pointer-events: none;
        }
        .grid-line {
            position: absolute; top: 0; bottom: 0;
            border-left: 1px dashed #e2e8f0;
        }
        
        .day-row {
            display: flex; height: 44px; align-items: center; margin-bottom: 2px;
            position: relative; z-index: 1;
        }
        .day-label {
            position: sticky; left: 0; z-index: 10;
            width: 70px; min-width: 70px;
            background: rgba(255,255,255,0.95);
            font-size: 0.75rem; font-weight: 600;
            display: flex; flex-direction: column; justify-content: center; align-items: center;
            border-right: 1px solid var(--border);
            box-shadow: 2px 0 4px rgba(0,0,0,0.02);
            height: 100%;
        }
        .day-label.today { color: var(--primary); }
        
        .bars-container {
            flex: 1; display: flex; align-items: flex-end;
            height: 100%; padding: 4px 0; margin-right: 16px; gap: 1px;
        }
        .bar-slot {
            flex: 1; position: relative; min-width: 3px;
            background: rgba(226, 232, 240, 0.3);
            border-radius: 2px 2px 0 0;
            display: flex; align-items: flex-end;
            height: 100%;  /* 親の高さを継承してパーセント指定を有効化 */
        }
        .bar-avg { width: 100%; position: relative; z-index: 2; border-radius: 1px 1px 0 0; }
        .bar-max { position: absolute; bottom: 0; left: 0; width: 100%; z-index: 1; background: rgba(0,0,0,0.05); }
        
        .bar-slot.low .bar-avg { background: var(--green); }
        .bar-slot.medium .bar-avg { background: var(--yellow); }
        .bar-slot.high .bar-avg { background: var(--red); }
        
        .bar-slot.low .bar-max { background: rgba(16, 185, 129, 0.2); }
        .bar-slot.medium .bar-max { background: rgba(245, 158, 11, 0.2); }
        .bar-slot.high .bar-max { background: rgba(239, 68, 68, 0.2); }

        /* Logs & Info */
        .log-list { max-height: 200px; overflow-y: auto; padding: 0 16px; }
        .log-item {
            display: flex; align-items: center; gap: 12px;
            padding: 10px 0; border-bottom: 1px solid var(--border);
            font-size: 0.85rem;
        }
        .log-dot { width: 8px; height: 8px; border-radius: 50%; }
        .log-dot.low { background: var(--green); }
        .log-dot.medium { background: var(--yellow); }
        .log-dot.high { background: var(--red); }

        .info-grid {
            display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 16px;
        }
        .info-box { background: var(--bg); padding: 10px; border-radius: 8px; text-align: center; }
        .info-val { font-weight: 700; font-size: 1rem; display: block; }
        .info-key { font-size: 0.7rem; color: var(--text-sub); }

        .legend {
            display: flex; justify-content: center; gap: 16px; padding: 12px;
            font-size: 0.7rem; color: var(--text-sub); background: #fafafa;
        }
        .legend-item { display: flex; align-items: center; gap: 4px; }
        .legend-color { width: 10px; height: 10px; border-radius: 2px; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🍽️ 食堂混雑情報</h1>
            <div id="clock" class="clock">--:--</div>
        </header>

        <div class="grid">
            <!-- 1. 現在の状況 (最重要) -->
            <div class="card card-status">
                <div id="status-hero" class="status-hero low">
                    <span id="status-icon" class="status-icon">😊</span>
                    <div id="status-text" class="status-label">空き</div>
                    <div class="status-detail">
                        現在 <span id="person-count" class="status-count">0</span> 人
                    </div>
                    <div style="margin-top:8px; font-size:0.75rem; opacity:0.7;">
                        最終更新: <span id="last-updated">--:--</span>
                    </div>
                </div>
            </div>

            <!-- 2. 週間トレンド (判断材料) -->
            <div class="card card-graph">
                <div class="card-header">
                    <span class="card-title">📊 週間トレンド (平均/最大)</span>
                </div>
                <div class="timeline-scroll">
                    <div class="timeline-content">
                        <div id="timeline-header" class="timeline-header"></div>
                        <div id="timeline-grid" class="timeline-grid"></div>
                        <div id="timeline-rows">
                            <div style="padding:20px; text-align:center; color:#999;">Loading...</div>
                        </div>
                        <div id="timeline-footer" class="timeline-footer"></div>
                    </div>
                </div>
                <div class="legend">
                    <div class="legend-item"><span class="legend-color" style="background:var(--green)"></span>空</div>
                    <div class="legend-item"><span class="legend-color" style="background:var(--yellow)"></span>やや混</div>
                    <div class="legend-item"><span class="legend-color" style="background:var(--red)"></span>混雑</div>
                    <div class="legend-item"><span class="legend-color" style="background:rgba(0,0,0,0.1)"></span>薄色は最大値</div>
                </div>
            </div>

            <!-- 3. カメラ映像 (確認用・控えめ) -->
            <div class="card card-camera">
                <div class="card-header">
                    <span class="card-title">📷 ライブ映像</span>
                </div>
                <div class="camera-container" id="camera-container">
                    <img id="video-frame" src="/api/frame/annotated" class="camera-img" alt="Live">
                    <button class="fullscreen-btn" onclick="toggleFullscreen(event)">
                        <span>⛶</span> 最大化
                    </button>
                    <div class="camera-overlay" id="delay-display">Delay: 0.0s</div>
                </div>
            </div>

            <!-- 4. 詳細情報 -->
            <div class="card card-info">
                <div class="card-header">
                    <span class="card-title">ℹ️ システム詳細</span>
                </div>
                <div class="info-grid">
                    <div class="info-box">
                        <span class="info-val" id="health-val" style="color:var(--green)">正常</span>
                        <span class="info-key">システム状態</span>
                    </div>
                    <div class="info-box">
                        <span class="info-val" id="confidence-val">--%</span>
                        <span class="info-key">検知精度</span>
                    </div>
                </div>
                <div class="log-list" id="log-list">
                    <!-- Logs here -->
                </div>
            </div>
        </div>
    </div>

    <script>
        const CAPACITY = 15;  // 実データの最大値に合わせて調整
        const STATUS_CONFIG = {
            low: { text: '空き', icon: '😊', class: 'low' },
            medium: { text: 'やや混雑', icon: '😐', class: 'medium' },
            high: { text: '混雑', icon: '😰', class: 'high' }
        };
        const logHistory = [];

        async function loadHistory() {
            try {
                const res = await fetch('/api/crowding/history?limit=20');
                const data = await res.json();
                
                if (data.records) {
                    const list = document.getElementById('log-list');
                    list.innerHTML = data.records.map(r => {
                        const date = new Date(r.timestamp);
                        const timeStr = date.toLocaleTimeString('ja-JP', {hour:'2-digit', minute:'2-digit'});
                        const dateStr = date.toLocaleDateString('ja-JP', {month:'numeric', day:'numeric'});
                        const level = r.crowding_level;
                        const levelClass = level; // low, medium, high
                        
                        return `
                        <div class="log-item">
                            <span class="log-dot ${levelClass}"></span>
                            <span style="flex:1; font-size:0.8rem; color:#666;">${dateStr} ${timeStr}</span>
                            <strong>${r.person_count}人</strong>
                        </div>`;
                    }).join('');
                }
            } catch(e) { console.error(e); }
        }
        loadHistory();

        function updateClock() {
            const now = new Date();
            document.getElementById('clock').textContent = now.toLocaleTimeString('ja-JP', {hour:'2-digit', minute:'2-digit'});
        }
        setInterval(updateClock, 1000);

        function updateFrame() {
            document.getElementById('video-frame').src = '/api/frame/annotated?' + Date.now();
        }
        setInterval(updateFrame, 1000); // 1秒更新

        async function updateStatus() {
            try {
                const [crowdRes, healthRes] = await Promise.all([
                    fetch('/api/crowding'),
                    fetch('/api/health')
                ]);
                const crowd = await crowdRes.json();
                const health = await healthRes.json();

                // Status Hero
                const hero = document.getElementById('status-hero');
                const config = STATUS_CONFIG[crowd.crowding_level];
                hero.className = 'status-hero ' + config.class;
                document.getElementById('status-icon').textContent = config.icon;
                document.getElementById('status-text').textContent = config.text;
                document.getElementById('person-count').textContent = crowd.person_count;
                
                const now = new Date();
                const timeStr = now.toLocaleTimeString('ja-JP', {hour:'2-digit', minute:'2-digit'});
                document.getElementById('last-updated').textContent = timeStr;

                // Logs
                if (logHistory.length === 0 || logHistory[0].time !== timeStr) {
                    logHistory.unshift({time: timeStr, count: crowd.person_count, level: crowd.crowding_level});
                    if(logHistory.length > 10) logHistory.pop();
                    
                    document.getElementById('log-list').innerHTML = logHistory.map(l => `
                        <div class="log-item">
                            <span class="log-dot ${l.level}"></span>
                            <span style="flex:1">${l.time}</span>
                            <strong>${l.count}人</strong>
                        </div>
                    `).join('');
                }

                // System Info
                document.getElementById('delay-display').textContent = `Delay: ${crowd.delay_seconds}s`;
                document.getElementById('health-val').textContent = health.status === 'healthy' ? '正常' : '異常';
                document.getElementById('health-val').style.color = health.status === 'healthy' ? 'var(--green)' : 'var(--red)';
                document.getElementById('confidence-val').textContent = Math.round(crowd.confidence * 100) + '%';

            } catch(e) { console.error(e); }
        }
        setInterval(updateStatus, 2000);

        async function updateTimeline() {
            try {
                const res = await fetch('/api/crowding/weekly?days=7');
                const data = await res.json();
                if(!data.weekly_data) return;

                // Draw Headers & Grid (11:00 - 22:00)
                const startHour = 11, endHour = 22, total = endHour - startHour;
                let headHtml = '', gridHtml = '';
                
                for(let h=startHour; h<=endHour; h++){
                    const p = ((h-startHour)/total)*100;
                    if(h < endHour) {
                        headHtml += `<span class="timeline-scale-label" style="left:${p}%">${h}</span>`;
                        gridHtml += `<div class="grid-line" style="left:${p}%"></div>`;
                    }
                }
                gridHtml += `<div class="grid-line" style="left:100%; border:none; border-right:1px dashed #ddd"></div>`;
                
                document.getElementById('timeline-header').innerHTML = headHtml;
                document.getElementById('timeline-footer').innerHTML = headHtml;
                document.getElementById('timeline-grid').innerHTML = gridHtml;

                // Draw Rows
                const rows = data.weekly_data.map(day => {
                    const isToday = day.date === data.current_date;
                    const bars = day.hourly_data.map(item => {
                        if(item.samples === 0) return '<div class="bar-slot" style="background:transparent"></div>';
                        
                        const avgH = Math.min(100, Math.max(15, (item.avg_count/CAPACITY)*100));
                        const maxH = Math.min(100, Math.max(15, (item.max_count/CAPACITY)*100));
                        const level = item.avg_count <= 4 ? 'low' : (item.avg_count <= 7 ? 'medium' : 'high');
                        
                        return `<div class="bar-slot ${level}">
                                    <div class="bar-max" style="height:${maxH}%"></div>
                                    <div class="bar-avg" style="height:${avgH}%"></div>
                                </div>`;
                    }).join('');
                    
                    return `<div class="day-row">
                                <div class="day-label ${isToday?'today':''}">
                                    <span>${day.date_label}</span>
                                    <span style="font-size:0.65rem; color:#888">${day.weekday}</span>
                                </div>
                                <div class="bars-container">${bars}</div>
                            </div>`;
                }).join('');
                
                document.getElementById('timeline-rows').innerHTML = rows;

            } catch(e) { console.error(e); }
        }
        updateTimeline();
        setInterval(updateTimeline, 60000);
        updateStatus();

        function toggleFullscreen(e) {
            e.stopPropagation(); // コンテナのクリックイベントと干渉しないように
            const container = document.getElementById('camera-container');
            
            if (!document.fullscreenElement) {
                if (container.requestFullscreen) {
                    container.requestFullscreen();
                } else if (container.webkitRequestFullscreen) { /* Safari */
                    container.webkitRequestFullscreen();
                } else if (container.msRequestFullscreen) { /* IE11 */
                    container.msRequestFullscreen();
                }
            } else {
                if (document.exitFullscreen) {
                    document.exitFullscreen();
                } else if (document.webkitExitFullscreen) { /* Safari */
                    document.webkitExitFullscreen();
                } else if (document.msExitFullscreen) { /* IE11 */
                    document.msExitFullscreen();
                }
            }
        }
        
        // ダブルクリックでも最大化
        document.getElementById('camera-container').addEventListener('dblclick', toggleFullscreen);
    </script>
</body>
</html>
'''
    return HTMLResponse(content=html)


# ===============================
# 一般職員用UI（カメラ映像なし）
# ===============================

@app.get('/staff', response_class=HTMLResponse)
def staff_index():
    """一般職員用ダッシュボードUI（カメラ映像なし）"""
    html = '''
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>食堂混雑情報 - 職員用</title>
    <style>
        /* Base / Reset */
        :root {
            --bg: #f1f5f9;
            --bg-card: #ffffff;
            --text-main: #0f172a;
            --text-sub: #64748b;
            --border: #e2e8f0;
            --primary: #3b82f6;
            --green: #10b981; --green-bg: #ecfdf5; --green-border: #a7f3d0;
            --yellow: #f59e0b; --yellow-bg: #fffbeb; --yellow-border: #fde68a;
            --red: #ef4444; --red-bg: #fef2f2; --red-border: #fecaca;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: transparent; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: var(--bg);
            color: var(--text-main);
            line-height: 1.5;
            padding-bottom: 40px;
        }

        /* Container */
        .container {
            max-width: 800px;
            margin: 0 auto;
            padding: 16px;
        }

        /* Header */
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        h1 {
            font-size: 1.25rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .clock {
            font-family: monospace;
            font-weight: 600;
            color: var(--text-sub);
            font-size: 1.1rem;
        }

        /* Grid Layout */
        .grid {
            display: grid;
            gap: 16px;
            grid-template-columns: 1fr;
        }

        @media (min-width: 768px) {
            .grid {
                grid-template-columns: 1fr 1fr;
                grid-template-areas:
                    "status status"
                    "graph graph"
                    "info info";
            }
            .card-status { grid-area: status; }
            .card-graph { grid-area: graph; }
            .card-info { grid-area: info; }
        }

        /* Cards */
        .card {
            background: var(--bg-card);
            border-radius: 16px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -1px rgba(0, 0, 0, 0.03);
            overflow: hidden;
            border: 1px solid var(--border);
        }

        .card-header {
            padding: 16px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .card-title {
            font-weight: 600;
            font-size: 0.95rem;
            color: var(--text-sub);
        }

        /* Status Hero (Level 1 Info) */
        .status-hero {
            padding: 24px;
            text-align: center;
            transition: all 0.3s ease;
        }
        .status-hero.low { background: var(--green-bg); color: #065f46; }
        .status-hero.medium { background: var(--yellow-bg); color: #92400e; }
        .status-hero.high { background: var(--red-bg); color: #991b1b; }

        .status-icon { font-size: 4rem; margin-bottom: 8px; display: block; }
        .status-label { font-size: 2rem; font-weight: 800; letter-spacing: 0.05em; margin-bottom: 4px; }
        .status-detail { font-size: 1rem; opacity: 0.9; }
        .status-count { font-size: 1.5rem; font-weight: 700; }

        /* Weekly Graph (Level 2 Info) - Scrollable */
        .timeline-scroll {
            overflow-x: auto;
            position: relative;
            scrollbar-width: thin;
            background: #fff;
        }
        .timeline-content {
            min-width: 800px; /* Ensure scroll on mobile */
            padding: 10px 0;
            position: relative;
        }
        .timeline-header, .timeline-footer {
            height: 20px;
            position: relative;
            margin-left: 70px; /* label width */
            margin-right: 16px;
        }
        .timeline-scale-label {
            position: absolute; transform: translateX(-50%);
            font-size: 0.7rem; color: var(--text-sub);
        }
        .timeline-grid {
            position: absolute; top: 20px; bottom: 20px;
            left: 70px; right: 16px; pointer-events: none;
        }
        .grid-line {
            position: absolute; top: 0; bottom: 0;
            border-left: 1px dashed #e2e8f0;
        }

        .day-row {
            display: flex; height: 44px; align-items: center; margin-bottom: 2px;
            position: relative; z-index: 1;
        }
        .day-label {
            position: sticky; left: 0; z-index: 10;
            width: 70px; min-width: 70px;
            background: rgba(255,255,255,0.95);
            font-size: 0.75rem; font-weight: 600;
            display: flex; flex-direction: column; justify-content: center; align-items: center;
            border-right: 1px solid var(--border);
            box-shadow: 2px 0 4px rgba(0,0,0,0.02);
            height: 100%;
        }
        .day-label.today { color: var(--primary); }

        .bars-container {
            flex: 1; display: flex; align-items: flex-end;
            height: 100%; padding: 4px 0; margin-right: 16px; gap: 1px;
        }
        .bar-slot {
            flex: 1; position: relative; min-width: 3px;
            background: rgba(226, 232, 240, 0.3);
            border-radius: 2px 2px 0 0;
            display: flex; align-items: flex-end;
            height: 100%;  /* 親の高さを継承してパーセント指定を有効化 */
        }
        .bar-avg { width: 100%; position: relative; z-index: 2; border-radius: 1px 1px 0 0; }
        .bar-max { position: absolute; bottom: 0; left: 0; width: 100%; z-index: 1; background: rgba(0,0,0,0.05); }

        .bar-slot.low .bar-avg { background: var(--green); }
        .bar-slot.medium .bar-avg { background: var(--yellow); }
        .bar-slot.high .bar-avg { background: var(--red); }

        .bar-slot.low .bar-max { background: rgba(16, 185, 129, 0.2); }
        .bar-slot.medium .bar-max { background: rgba(245, 158, 11, 0.2); }
        .bar-slot.high .bar-max { background: rgba(239, 68, 68, 0.2); }

        /* Logs & Info */
        .log-list { max-height: 200px; overflow-y: auto; padding: 0 16px; }
        .log-item {
            display: flex; align-items: center; gap: 12px;
            padding: 10px 0; border-bottom: 1px solid var(--border);
            font-size: 0.85rem;
        }
        .log-dot { width: 8px; height: 8px; border-radius: 50%; }
        .log-dot.low { background: var(--green); }
        .log-dot.medium { background: var(--yellow); }
        .log-dot.high { background: var(--red); }

        .info-grid {
            display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 16px;
        }
        .info-box { background: var(--bg); padding: 10px; border-radius: 8px; text-align: center; }
        .info-val { font-weight: 700; font-size: 1rem; display: block; }
        .info-key { font-size: 0.7rem; color: var(--text-sub); }

        .legend {
            display: flex; justify-content: center; gap: 16px; padding: 12px;
            font-size: 0.7rem; color: var(--text-sub); background: #fafafa;
        }
        .legend-item { display: flex; align-items: center; gap: 4px; }
        .legend-color { width: 10px; height: 10px; border-radius: 2px; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🍽️ 食堂混雑情報</h1>
            <div id="clock" class="clock">--:--</div>
        </header>

        <div class="grid">
            <!-- 1. 現在の状況 (最重要) -->
            <div class="card card-status">
                <div id="status-hero" class="status-hero low">
                    <span id="status-icon" class="status-icon">😊</span>
                    <div id="status-text" class="status-label">空き</div>
                    <div class="status-detail">
                        現在 <span id="person-count" class="status-count">0</span> 人
                    </div>
                    <div style="margin-top:8px; font-size:0.75rem; opacity:0.7;">
                        最終更新: <span id="last-updated">--:--</span>
                    </div>
                </div>
            </div>

            <!-- 2. 週間トレンド (判断材料) -->
            <div class="card card-graph">
                <div class="card-header">
                    <span class="card-title">📊 週間トレンド (平均/最大)</span>
                </div>
                <div class="timeline-scroll">
                    <div class="timeline-content">
                        <div id="timeline-header" class="timeline-header"></div>
                        <div id="timeline-grid" class="timeline-grid"></div>
                        <div id="timeline-rows">
                            <div style="padding:20px; text-align:center; color:#999;">Loading...</div>
                        </div>
                        <div id="timeline-footer" class="timeline-footer"></div>
                    </div>
                </div>
                <div class="legend">
                    <div class="legend-item"><span class="legend-color" style="background:var(--green)"></span>空</div>
                    <div class="legend-item"><span class="legend-color" style="background:var(--yellow)"></span>やや混</div>
                    <div class="legend-item"><span class="legend-color" style="background:var(--red)"></span>混雑</div>
                    <div class="legend-item"><span class="legend-color" style="background:rgba(0,0,0,0.1)"></span>薄色は最大値</div>
                </div>
            </div>

            <!-- 3. 履歴情報 -->
            <div class="card card-info">
                <div class="card-header">
                    <span class="card-title">📋 最近の記録</span>
                </div>
                <div class="log-list" id="log-list">
                    <!-- Logs here -->
                </div>
            </div>
        </div>
    </div>

    <script>
        const CAPACITY = 15;  // 実データの最大値に合わせて調整
        const STATUS_CONFIG = {
            low: { text: '空き', icon: '😊', class: 'low' },
            medium: { text: 'やや混雑', icon: '😐', class: 'medium' },
            high: { text: '混雑', icon: '😰', class: 'high' }
        };
        const logHistory = [];

        async function loadHistory() {
            try {
                const res = await fetch('/api/crowding/history?limit=20');
                const data = await res.json();

                if (data.records) {
                    const list = document.getElementById('log-list');
                    list.innerHTML = data.records.map(r => {
                        const date = new Date(r.timestamp);
                        const timeStr = date.toLocaleTimeString('ja-JP', {hour:'2-digit', minute:'2-digit'});
                        const dateStr = date.toLocaleDateString('ja-JP', {month:'numeric', day:'numeric'});
                        const level = r.crowding_level;
                        const levelClass = level; // low, medium, high

                        return `
                        <div class="log-item">
                            <span class="log-dot ${levelClass}"></span>
                            <span style="flex:1; font-size:0.8rem; color:#666;">${dateStr} ${timeStr}</span>
                            <strong>${r.person_count}人</strong>
                        </div>`;
                    }).join('');
                }
            } catch(e) { console.error(e); }
        }
        loadHistory();

        function updateClock() {
            const now = new Date();
            document.getElementById('clock').textContent = now.toLocaleTimeString('ja-JP', {hour:'2-digit', minute:'2-digit'});
        }
        setInterval(updateClock, 1000);

        async function updateStatus() {
            try {
                const crowdRes = await fetch('/api/crowding');
                const crowd = await crowdRes.json();

                // Status Hero
                const hero = document.getElementById('status-hero');
                const config = STATUS_CONFIG[crowd.crowding_level];
                hero.className = 'status-hero ' + config.class;
                document.getElementById('status-icon').textContent = config.icon;
                document.getElementById('status-text').textContent = config.text;
                document.getElementById('person-count').textContent = crowd.person_count;

                const now = new Date();
                const timeStr = now.toLocaleTimeString('ja-JP', {hour:'2-digit', minute:'2-digit'});
                document.getElementById('last-updated').textContent = timeStr;

                // Logs
                if (logHistory.length === 0 || logHistory[0].time !== timeStr) {
                    logHistory.unshift({time: timeStr, count: crowd.person_count, level: crowd.crowding_level});
                    if(logHistory.length > 10) logHistory.pop();

                    document.getElementById('log-list').innerHTML = logHistory.map(l => `
                        <div class="log-item">
                            <span class="log-dot ${l.level}"></span>
                            <span style="flex:1">${l.time}</span>
                            <strong>${l.count}人</strong>
                        </div>
                    `).join('');
                }

            } catch(e) { console.error(e); }
        }
        setInterval(updateStatus, 2000);

        async function updateTimeline() {
            try {
                const res = await fetch('/api/crowding/weekly?days=7');
                const data = await res.json();
                if(!data.weekly_data) return;

                // Draw Headers & Grid (11:00 - 22:00)
                const startHour = 11, endHour = 22, total = endHour - startHour;
                let headHtml = '', gridHtml = '';

                for(let h=startHour; h<=endHour; h++){
                    const p = ((h-startHour)/total)*100;
                    if(h < endHour) {
                        headHtml += `<span class="timeline-scale-label" style="left:${p}%">${h}</span>`;
                        gridHtml += `<div class="grid-line" style="left:${p}%"></div>`;
                    }
                }
                gridHtml += `<div class="grid-line" style="left:100%; border:none; border-right:1px dashed #ddd"></div>`;

                document.getElementById('timeline-header').innerHTML = headHtml;
                document.getElementById('timeline-footer').innerHTML = headHtml;
                document.getElementById('timeline-grid').innerHTML = gridHtml;

                // Draw Rows
                const rows = data.weekly_data.map(day => {
                    const isToday = day.date === data.current_date;
                    const bars = day.hourly_data.map(item => {
                        if(item.samples === 0) return '<div class="bar-slot" style="background:transparent"></div>';

                        const avgH = Math.min(100, Math.max(15, (item.avg_count/CAPACITY)*100));
                        const maxH = Math.min(100, Math.max(15, (item.max_count/CAPACITY)*100));
                        const level = item.avg_count <= 4 ? 'low' : (item.avg_count <= 7 ? 'medium' : 'high');

                        return `<div class="bar-slot ${level}">
                                    <div class="bar-max" style="height:${maxH}%"></div>
                                    <div class="bar-avg" style="height:${avgH}%"></div>
                                </div>`;
                    }).join('');

                    return `<div class="day-row">
                                <div class="day-label ${isToday?'today':''}">
                                    <span>${day.date_label}</span>
                                    <span style="font-size:0.65rem; color:#888">${day.weekday}</span>
                                </div>
                                <div class="bars-container">${bars}</div>
                            </div>`;
                }).join('');

                document.getElementById('timeline-rows').innerHTML = rows;

            } catch(e) { console.error(e); }
        }
        updateTimeline();
        setInterval(updateTimeline, 60000);
        updateStatus();
    </script>
</body>
</html>
'''
    return HTMLResponse(content=html)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8000)
