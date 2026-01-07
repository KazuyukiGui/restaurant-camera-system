import os
import sys
from datetime import datetime, timedelta

# アプリのパスを通す
sys.path.append(os.getcwd())

from app.database import init_db, get_db, CrowdingRecord

def check_db():
    init_db()
    db = next(get_db())
    
    print("=== DB Data Check ===")
    
    # 全件数
    count = db.query(CrowdingRecord).count()
    print(f"Total records: {count}")
    
    # 最新10件
    print("\n--- Latest 10 records ---")
    latest = db.query(CrowdingRecord).order_by(CrowdingRecord.timestamp.desc()).limit(10).all()
    for r in latest:
        print(f"ID: {r.id}, Time: {r.timestamp}, Count: {r.person_count}")
        
    # 最古10件（あるいは昨日のデータがありそうなあたり）
    print("\n--- Oldest 10 records ---")
    oldest = db.query(CrowdingRecord).order_by(CrowdingRecord.timestamp.asc()).limit(10).all()
    for r in oldest:
        print(f"ID: {r.id}, Time: {r.timestamp}, Count: {r.person_count}")

if __name__ == "__main__":
    check_db()


