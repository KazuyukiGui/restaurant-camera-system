import os
import sys
from datetime import datetime, timedelta

sys.path.append(os.getcwd())
from app.database import init_db, get_db, CrowdingRecord

def check_db():
    init_db()
    db = next(get_db())
    
    print("=== DB Check 2 ===")
    
    # 昨日の日付
    yesterday = datetime(2026, 1, 7).strftime('%Y-%m-%d')
    print(f"Searching for data on {yesterday} between 11:00 and 22:00...")
    
    # 昨日の11:00〜22:00のデータをカウント
    start = datetime(2026, 1, 7, 11, 0, 0)
    end = datetime(2026, 1, 7, 22, 0, 0)
    
    count = db.query(CrowdingRecord).filter(
        CrowdingRecord.timestamp >= start,
        CrowdingRecord.timestamp <= end
    ).count()
    
    print(f"Records found: {count}")
    
    if count > 0:
        sample = db.query(CrowdingRecord).filter(
            CrowdingRecord.timestamp >= start,
            CrowdingRecord.timestamp <= end
        ).first()
        print(f"Sample: {sample.timestamp} (Count: {sample.person_count})")

if __name__ == "__main__":
    check_db()


