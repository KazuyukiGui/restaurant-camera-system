

# **食堂混雑検知システム**

開発仕様書 v3.3 Final

論理バグ修正 \+ ゾンビスレッド対策

**対象ハードウェア: Core i3-10105T / 8GB RAM / GPU無し**  
**対象カメラ: 640×480 @ 30fps (RTSP)**

作成日: 2026年1月5日  
デジタルコミュニケーション部

# **1\. v3.3 クリティカル修正**

**v3.2に対する技術レビューで発見された2つの致命的な問題を修正した。**

| Risk | 問題 | 影響 | v3.3での修正 |
| ----- | ----- | ----- | ----- |
| \#1 | 第2防波堤の論理バグ | get\_frame()が呼ばれないと遅延検知が機能しない | ループ内で直接遅延計算 \+ 目的の明確化 |
| \#2 | ゾンビスレッド蓄積 | Watchdog再起動でメモリリーク | 再起動回数カウント→閾値超過でコンテナ再起動誘発 |

# **2\. Risk \#1: 第2防波堤の論理バグ【詳細分析】**

## **2.1 v3.2のバグ箇所**

*\# v3.2の問題コード（\_capture\_loop内）*

if ret:  
    with self.lock:  
        self.frame \= frame  
        self.frame\_time \= current\_time  
      
    \# 【バグ】self.delay\_seconds は get\_frame() が呼ばれないと更新されない！  
    if self.delay\_seconds \> self.DELAY\_THRESHOLD\_RECONNECT:  
        cap \= self.\_handle\_reconnect(cap)

## **2.2 障害シナリオ**

1. 夜間や休日、誰もWeb画面を見ていない（get\_frame()が呼ばれない）  
2. カメラのバッファが詰まり、cap.read()は成功するが映像は5分前の過去映像  
3. get\_frame()が呼ばれないため、self.delay\_secondsは0のまま  
4. 再接続が発動せず、翌朝職員が画面を開いた瞬間にフリーズに見える

## **2.3 技術的制約の理解**

**【重要】RTSPストリーム自体の遅延は受信側だけでは検知困難**

OpenCVのcap.read()が成功している限り、frame\_timeは更新され続ける。バッファから古いフレームが吐き出されている場合でも、受信側からは「正常に新しいフレームが来ている」ように見える。RTSPフレームの撮影時刻（PTS/DTS）を取得するにはGStreamerが必要だが、本システムではFFmpegバックエンドを使用しているため、この情報は取得できない。

## **2.4 修正方針：三段防波堤の役割分担を明確化**

| 防波堤 | 検知対象 | トリガー条件 |
| ----- | ----- | ----- |
| 第1 | バッファ滞留（古いフレームが溜まる問題） | 別スレッドで常時grabし、常に最新を上書き |
| 第2 | フレーム取得間隔の異常（read()が遅くなる問題） | 前回read成功からの経過時間 \> 5秒 |
| 第3 | read()無期限ブロック（スレッド停止） | Watchdog：最終フレーム取得から10秒更新なし |

**【設計変更】第2防波堤は「get\_frame()依存」から「read()間隔監視」に変更。これにより、誰もAPIを呼ばない状況でも検知が機能する。**

# **3\. RTSPキャプチャ実装【v3.3修正版】**

## **3.1 rtsp\_capture.py**

*\# v3.3: Risk \#1 \+ Risk \#2 修正版*

import cv2  
import threading  
import time  
import logging

class RTSPCapture:  
    """三段防波堤設計のRTSPキャプチャ（v3.3修正版）"""  
      
    MAX\_RECONNECT\_PER\_HOUR \= 5  
    MAX\_WATCHDOG\_RESTART\_PER\_HOUR \= 3  \# v3.3: Risk \#2対策  
    READ\_INTERVAL\_THRESHOLD \= 5.0      \# v3.3: Risk \#1対策（第2防波堤）  
    WATCHDOG\_TIMEOUT \= 10.0  
    LOOP\_SLEEP \= 0.005  
      
    def \_\_init\_\_(self, rtsp\_url):  
        self.rtsp\_url \= rtsp\_url  
        self.frame \= None  
        self.frame\_time \= None  
        self.last\_successful\_read\_time \= None  \# v3.3: read成功時刻  
        self.lock \= threading.Lock()  
        self.running \= False  
        self.delay\_seconds \= 0.0  
        self.reconnect\_count \= 0  
        self.reconnect\_reset\_time \= time.time()  
        self.watchdog\_restart\_count \= 0        \# v3.3: Risk \#2対策  
        self.watchdog\_restart\_reset\_time \= time.time()  
        self.system\_halted \= False  
        self.thread \= None  
      
    def start(self):  
        self.running \= True  
        self.system\_halted \= False  
        self.last\_successful\_read\_time \= time.time()  \# v3.3  
        self.thread \= threading.Thread(target=self.\_capture\_loop)  
        self.thread.daemon \= True  
        self.thread.start()  
      
    def stop(self):  
        self.running \= False  
        if self.thread and self.thread.is\_alive():  
            self.thread.join(timeout=2.0)  
      
    def restart(self):  
        """v3.3: Watchdog再起動（回数カウント付き）"""  
        \# 1時間経過でカウンタリセット  
        if time.time() \- self.watchdog\_restart\_reset\_time \> 3600:  
            self.watchdog\_restart\_count \= 0  
            self.watchdog\_restart\_reset\_time \= time.time()  
          
        self.watchdog\_restart\_count \+= 1  
          
        \# v3.3 Risk \#2: 閾値超過でシステム停止→コンテナ再起動誘発  
        if self.watchdog\_restart\_count \> self.MAX\_WATCHDOG\_RESTART\_PER\_HOUR:  
            logging.critical('Watchdog再起動上限超過 \- コンテナ再起動が必要')  
            self.system\_halted \= True  
            return  
          
        logging.warning(f'Watchdog: 再起動 {self.watchdog\_restart\_count}/3')  
        self.stop()  
        time.sleep(1.0)  
        self.start()  
      
    def is\_healthy(self):  
        if self.system\_halted:  
            return False  \# v3.3: 停止中は常にunhealthy  
        if self.frame\_time is None:  
            return True  
        if time.time() \- self.frame\_time \> self.WATCHDOG\_TIMEOUT:  
            return False  
        return True  
      
    def \_connect(self):  
        cap \= cv2.VideoCapture(self.rtsp\_url)  
        cap.set(cv2.CAP\_PROP\_BUFFERSIZE, 1\)  
        return cap  
      
    def \_handle\_reconnect(self, cap):  
        if time.time() \- self.reconnect\_reset\_time \> 3600:  
            self.reconnect\_count \= 0  
            self.reconnect\_reset\_time \= time.time()  
          
        self.reconnect\_count \+= 1  
          
        if self.reconnect\_count \> self.MAX\_RECONNECT\_PER\_HOUR:  
            logging.critical('再接続上限超過 \- システム停止')  
            self.system\_halted \= True  
            self.running \= False  
            return None  
          
        wait \= min(30, 2 \*\* (self.reconnect\_count \- 1))  
        logging.warning(f'再接続 {self.reconnect\_count}/5 ({wait}秒後)')  
        cap.release()  
        time.sleep(wait)  
        new\_cap \= self.\_connect()  
        self.last\_successful\_read\_time \= time.time()  \# v3.3: リセット  
        return new\_cap  
      
    def \_capture\_loop(self):  
        cap \= self.\_connect()  
        while self.running:  
            ret, frame \= cap.read()  
            current\_time \= time.time()  
              
            if ret:  
                with self.lock:  
                    self.frame \= frame  
                    self.frame\_time \= current\_time  
                  
                \# v3.3 Risk \#1修正: read成功時刻を更新  
                self.last\_successful\_read\_time \= current\_time  
            else:  
                \# read失敗時の処理  
                cap \= self.\_handle\_reconnect(cap)  
                if cap is None:  
                    break  
              
            \# v3.3 Risk \#1修正: 第2防波堤 \- read間隔監視（get\_frame依存を排除）  
            if self.last\_successful\_read\_time:  
                read\_interval \= current\_time \- self.last\_successful\_read\_time  
                if read\_interval \> self.READ\_INTERVAL\_THRESHOLD:  
                    logging.warning(f'read間隔異常: {read\_interval:.1f}秒')  
                    cap \= self.\_handle\_reconnect(cap)  
                    if cap is None:  
                        break  
              
            time.sleep(self.LOOP\_SLEEP)  
      
    def get\_frame(self):  
        with self.lock:  
            if self.frame\_time:  
                self.delay\_seconds \= time.time() \- self.frame\_time  
            return self.frame, self.delay\_seconds, self.system\_halted  
      
    def get\_health\_stats(self):  \# v3.3: ヘルスチェック用統計  
        """監視用の詳細統計を返す"""  
        return {  
            'is\_healthy': self.is\_healthy(),  
            'system\_halted': self.system\_halted,  
            'delay\_seconds': self.delay\_seconds,  
            'reconnect\_count': self.reconnect\_count,  
            'watchdog\_restart\_count': self.watchdog\_restart\_count,  
        }

# **4\. Risk \#2: ゾンビスレッド対策【詳細分析】**

## **4.1 問題の本質**

Pythonのthreadingモジュールはスレッドの強制終了（Kill）ができない。cap.read()でハングしているスレッドに対してstop()を呼んでも、スレッドはC++領域（FFmpeg内部）で止まったままループを抜けられない。

## **4.2 Watchdog restart()時の挙動**

5. stop()が呼ばれ、running=Falseになる  
6. しかしスレッドはcap.read()内部でブロック中のため、ループを抜けられない  
7. join(timeout=2.0)でタイムアウトする  
8. 古いスレッドはゾンビとして残ったまま、start()で新しいスレッドが起動  
9. 繰り返すとメモリリーク（スレッド1つ＋OpenCVインスタンス1つ分 ≒ 数十MB/回）

## **4.3 v3.3の解決策：二段構えの浄化**

| 段階 | 条件 | アクション |
| ----- | ----- | ----- |
| ソフト再起動 | Watchdog再起動 1-3回/時 | スレッド再起動（ゾンビ許容） |
| ハード再起動 | Watchdog再起動 \> 3回/時 | system\_halted=True → /api/health 503 → Docker再起動 |

**【設計思想】ゾンビスレッドが溜まる前にコンテナごと浄化する。これにより、メモリリークを根本的に解決。**

# **5\. ヘルスチェックAPI【v3.3更新】**

## **5.1 /api/health 実装**

from fastapi import FastAPI, Response  
from fastapi.responses import JSONResponse

@app.get('/api/health')  
def health\_check():  
    stats \= rtsp\_capture.get\_health\_stats()  
      
    \# v3.3: system\_haltedの場合は503を返す（Docker再起動誘発）  
    if stats\['system\_halted'\]:  
        return JSONResponse(  
            status\_code=503,  
            content={  
                'status': 'unhealthy',  
                'reason': 'system\_halted \- container restart required',  
                \*\*stats  
            }  
        )  
      
    \# 正常時  
    status \= 'healthy' if stats\['is\_healthy'\] else 'degraded'  
    return {  
        'status': status,  
        \*\*stats,  
        'config': {  
            'imgsz': int(os.getenv('IMGSZ', '416')),  
            'process\_fps': int(os.getenv('PROCESS\_FPS', '3')),  
        }  
    }

## **5.2 docker-compose.yml（healthcheck設定）**

version: '3.8'  
services:  
  cafeteria-counter:  
    build: .  
    ports:  
      \- '8000:8000'  
    env\_file:  
      \- .env  
    volumes:  
      \- ./data:/app/data  
    restart: unless-stopped  
    healthcheck:  
      test: \['CMD', 'curl', '-f', 'http://localhost:8000/api/health'\]  
      interval: 30s  
      timeout: 10s  
      retries: 3  
      start\_period: 60s  \# v3.3: 起動猶予

*【動作】system\_halted=Trueになると、/api/healthが503を返す → Dockerのhealthcheckが3回連続失敗 → コンテナ再起動 → ゾンビスレッドを含むプロセス全体が浄化される*

# **6\. 防波堤設計サマリー【v3.3最終版】**

| 段 | 検知対象 | トリガー | アクション | 検知可否 |
| ----- | ----- | ----- | ----- | ----- |
| 第1 | バッファ滞留 | 別スレッド常時grab | 最新フレーム上書き | ◎ |
| 第2 | read間隔異常 | 前回read成功から5秒超 | 再接続 | ◎ |
| 第3 | read()ブロック | 10秒更新なし | スレッド再起動 | ◎ |
| 第4 | ゾンビスレッド蓄積 | Watchdog再起動3回/時超 | コンテナ再起動誘発 | ◎ |

【制約事項】RTSPストリーム自体の遅延（カメラ側でバッファが溜まっている場合）は、受信側だけでは検知不可能。この問題は第1防波堤（常時grab）で「最新を上書きし続ける」ことで実質的に軽減している。

# **7\. AIエディタへの最終実装指示**

**【Cursor / Copilot への指示】**  
食堂混雑検知システムを実装してください。以下の必須要件を守ってください：

**1\. Risk \#1対策 (Capture Logic Fix):**  
   rtsp\_capture.py の \_capture\_loop 内で、  
   last\_successful\_read\_time を記録し、  
   「前回read成功からの経過時間 \> 5秒」で再接続を発動。  
   get\_frame()への依存を排除すること。

**2\. Risk \#2対策 (Zombie Thread Prevention):**  
   Watchdogによる restart() の呼び出し回数をカウントし、  
   1時間以内に3回以上restartした場合は  
   system\_halted=True にして /api/health で 503 を返し、  
   Docker Healthcheckによるコンテナ再起動を誘発させる。

**3\. Database:**  
   SQLite接続時に check\_same\_thread=False を必須設定

**4\. CPU負荷対策:**  
   \_capture\_loop 内に time.sleep(0.005) を入れる

**5\. Default Config:**  
   .env の IMGSZ デフォルト値を 416 に設定

# **8\. 付録**

## **8.1 改訂履歴**

| 日付 | Ver | 内容 |
| ----- | ----- | ----- |
| 2026-01-05 | 3.3 | Risk \#1修正（第2防波堤論理バグ）、Risk \#2修正（ゾンビスレッド対策） |
| 2026-01-05 | 3.2 | Watchdog監視追加、SQLite競合対策、CPU負荷最適化、IMGSZ=416 |
| 2026-01-05 | 3.1 | 再接続ロジック修正、二段防波堤設計、環境変数反映必須化 |
| 2026-01-05 | 3.0 | VGAカメラ対応、RTSP遅延対策追加 |
| 2026-01-05 | 2.0 | VLM調査結果追加、技術選定根拠明確化 |
| 2026-01-04 | 1.0 | 初版作成 |

## **8.2 技術的制約事項**

本システムには以下の技術的制約が存在する。これらは設計上の限界であり、追加投資なしには解決できない。

* RTSPストリーム自体の遅延（カメラ側バッファ）は受信側では検知不可（GStreamer必要）

* Pythonスレッドの強制終了は不可能（ゾンビスレッドはコンテナ再起動で浄化）

* CAP\_PROP\_BUFFERSIZEはFFmpegバックエンドで効かない環境あり（別スレッドで対応）

* VGA（640×480）解像度では、広角設置時に人が小さく写り検知精度が低下

— 以上 —