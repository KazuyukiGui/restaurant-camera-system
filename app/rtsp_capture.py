# v3.3: Risk #1 + Risk #2 修正版
# 食堂混雑検知システム - RTSPキャプチャモジュール

import cv2
import threading
import time
import logging

logger = logging.getLogger(__name__)


class RTSPCapture:
    """
    三段防波堤設計のRTSPキャプチャ（v3.3修正版）
    
    防波堤設計:
    - 第1: バッファ滞留対策（常時grabで最新フレーム上書き）
    - 第2: read間隔異常検知（前回read成功から5秒超で再接続）
    - 第3: read()ブロック検知（Watchdog: 10秒更新なしでスレッド再起動）
    - 第4: ゾンビスレッド対策（Watchdog再起動3回/時超でコンテナ再起動誘発）
    """
    
    MAX_RECONNECT_PER_HOUR = 5
    MAX_WATCHDOG_RESTART_PER_HOUR = 3  # v3.3: Risk #2対策
    READ_INTERVAL_THRESHOLD = 5.0      # v3.3: Risk #1対策（第2防波堤）
    WATCHDOG_TIMEOUT = 10.0
    LOOP_SLEEP = 0.005  # CPU負荷対策
    
    def __init__(self, rtsp_url: str):
        self.rtsp_url = rtsp_url
        self.frame = None
        self.frame_time = None
        self.last_successful_read_time = None  # v3.3: read成功時刻
        self.lock = threading.Lock()
        self.running = False
        self.delay_seconds = 0.0
        self.reconnect_count = 0
        self.reconnect_reset_time = time.time()
        self.watchdog_restart_count = 0        # v3.3: Risk #2対策
        self.watchdog_restart_reset_time = time.time()
        self.system_halted = False
        self.thread = None
    
    def start(self):
        """キャプチャスレッドを開始"""
        self.running = True
        self.system_halted = False
        self.last_successful_read_time = time.time()  # v3.3
        self.thread = threading.Thread(target=self._capture_loop)
        self.thread.daemon = True
        self.thread.start()
        logger.info(f'RTSPキャプチャ開始: {self.rtsp_url}')
    
    def stop(self):
        """キャプチャスレッドを停止"""
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        logger.info('RTSPキャプチャ停止')
    
    def restart(self):
        """
        v3.3: Watchdog再起動（回数カウント付き）
        
        ゾンビスレッド対策として、再起動回数をカウントし、
        閾値を超えた場合はコンテナ再起動を誘発する
        """
        # 1時間経過でカウンタリセット
        if time.time() - self.watchdog_restart_reset_time > 3600:
            self.watchdog_restart_count = 0
            self.watchdog_restart_reset_time = time.time()
        
        self.watchdog_restart_count += 1
        
        # v3.3 Risk #2: 閾値超過でシステム停止→コンテナ再起動誘発
        if self.watchdog_restart_count > self.MAX_WATCHDOG_RESTART_PER_HOUR:
            logger.critical('Watchdog再起動上限超過 - コンテナ再起動が必要')
            self.system_halted = True
            return
        
        logger.warning(f'Watchdog: 再起動 {self.watchdog_restart_count}/{self.MAX_WATCHDOG_RESTART_PER_HOUR}')
        self.stop()
        time.sleep(1.0)
        self.start()
    
    def is_healthy(self) -> bool:
        """ヘルスチェック用の状態確認"""
        if self.system_halted:
            return False  # v3.3: 停止中は常にunhealthy
        if self.frame_time is None:
            return True  # まだフレーム取得前
        if time.time() - self.frame_time > self.WATCHDOG_TIMEOUT:
            return False
        return True
    
    def _connect(self) -> cv2.VideoCapture:
        """RTSPストリームへの接続"""
        cap = cv2.VideoCapture(self.rtsp_url)
        # バッファサイズを最小に設定（環境によっては効かない場合あり）
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        logger.info(f'RTSP接続完了: {self.rtsp_url}')
        return cap
    
    def _handle_reconnect(self, cap: cv2.VideoCapture) -> cv2.VideoCapture | None:
        """再接続処理（指数バックオフ付き）"""
        # 1時間経過でカウンタリセット
        if time.time() - self.reconnect_reset_time > 3600:
            self.reconnect_count = 0
            self.reconnect_reset_time = time.time()
        
        self.reconnect_count += 1
        
        # 再接続上限チェック
        if self.reconnect_count > self.MAX_RECONNECT_PER_HOUR:
            logger.critical('再接続上限超過 - システム停止')
            self.system_halted = True
            self.running = False
            return None
        
        # 指数バックオフで待機
        wait = min(30, 2 ** (self.reconnect_count - 1))
        logger.warning(f'再接続 {self.reconnect_count}/{self.MAX_RECONNECT_PER_HOUR} ({wait}秒後)')
        cap.release()
        time.sleep(wait)
        new_cap = self._connect()
        self.last_successful_read_time = time.time()  # v3.3: リセット
        return new_cap
    
    def _capture_loop(self):
        """メインキャプチャループ（三段防波堤実装）"""
        cap = self._connect()
        
        while self.running:
            ret, frame = cap.read()
            current_time = time.time()
            
            if ret:
                # フレーム取得成功
                with self.lock:
                    self.frame = frame
                    self.frame_time = current_time
                
                # v3.3 Risk #1修正: read成功時刻を更新
                self.last_successful_read_time = current_time
            else:
                # read失敗時の処理
                logger.warning('フレーム取得失敗 - 再接続を試行')
                cap = self._handle_reconnect(cap)
                if cap is None:
                    break
            
            # v3.3 Risk #1修正: 第2防波堤 - read間隔監視（get_frame依存を排除）
            if self.last_successful_read_time:
                read_interval = current_time - self.last_successful_read_time
                if read_interval > self.READ_INTERVAL_THRESHOLD:
                    logger.warning(f'read間隔異常: {read_interval:.1f}秒')
                    cap = self._handle_reconnect(cap)
                    if cap is None:
                        break
            
            # CPU負荷対策
            time.sleep(self.LOOP_SLEEP)
        
        # クリーンアップ
        if cap is not None:
            cap.release()
    
    def get_frame(self) -> tuple:
        """
        現在のフレームを取得
        
        Returns:
            tuple: (frame, delay_seconds, system_halted)
        """
        with self.lock:
            if self.frame_time:
                self.delay_seconds = time.time() - self.frame_time
            return self.frame, self.delay_seconds, self.system_halted
    
    def get_health_stats(self) -> dict:
        """
        v3.3: ヘルスチェック用統計を返す
        
        Returns:
            dict: 監視用の詳細統計
        """
        return {
            'is_healthy': self.is_healthy(),
            'system_halted': self.system_halted,
            'delay_seconds': round(self.delay_seconds, 2),
            'reconnect_count': self.reconnect_count,
            'watchdog_restart_count': self.watchdog_restart_count,
        }


