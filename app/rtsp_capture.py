# v3.4: バッファ滞留対策修正版
# 食堂混雑検知システム - RTSPキャプチャモジュール

import cv2
import threading
import time
import logging

logger = logging.getLogger(__name__)


class RTSPCapture:
    """
    RTSPキャプチャ（v3.4 バッファ滞留対策版）

    防波堤設計:
    - 第1: バッファ滞留対策（grab専用スレッドで常時バッファ消費）
    - 第2: read間隔異常検知（前回read成功から5秒超で再接続）
    - 第3: read()ブロック検知（Watchdog: 10秒更新なしでスレッド再起動）
    - 第4: ゾンビスレッド対策（Watchdog再起動3回/時超でコンテナ再起動誘発）
    """

    MAX_RECONNECT_PER_HOUR = 5
    MAX_WATCHDOG_RESTART_PER_HOUR = 3
    READ_INTERVAL_THRESHOLD = 5.0
    WATCHDOG_TIMEOUT = 10.0
    GRAB_INTERVAL = 0.01  # 10ms間隔でgrab（100fps相当でバッファ消費）

    def __init__(self, rtsp_url: str):
        self.rtsp_url = rtsp_url
        self.frame = None
        self.frame_time = None
        self.last_successful_read_time = None
        self.lock = threading.Lock()
        self.cap_lock = threading.Lock()  # VideoCapture用ロック
        self.running = False
        self.delay_seconds = 0.0
        self.reconnect_count = 0
        self.reconnect_reset_time = time.time()
        self.watchdog_restart_count = 0
        self.watchdog_restart_reset_time = time.time()
        self.system_halted = False
        self.grab_thread = None
        self.retrieve_thread = None
        self.cap = None
        self.new_frame_available = threading.Event()

    def start(self):
        """キャプチャスレッドを開始"""
        self.running = True
        self.system_halted = False
        self.last_successful_read_time = time.time()

        # 接続
        self.cap = self._connect()

        # grab専用スレッド（バッファ消費用）
        self.grab_thread = threading.Thread(target=self._grab_loop, name="RTSP-Grab")
        self.grab_thread.daemon = True
        self.grab_thread.start()

        # retrieve専用スレッド（フレーム取得用）
        self.retrieve_thread = threading.Thread(target=self._retrieve_loop, name="RTSP-Retrieve")
        self.retrieve_thread.daemon = True
        self.retrieve_thread.start()

        logger.info(f'RTSPキャプチャ開始（2スレッド方式）: {self.rtsp_url}')

    def stop(self):
        """キャプチャスレッドを停止"""
        self.running = False
        self.new_frame_available.set()  # スレッドを起こす

        if self.grab_thread and self.grab_thread.is_alive():
            self.grab_thread.join(timeout=2.0)
        if self.retrieve_thread and self.retrieve_thread.is_alive():
            self.retrieve_thread.join(timeout=2.0)

        with self.cap_lock:
            if self.cap is not None:
                self.cap.release()
                self.cap = None

        logger.info('RTSPキャプチャ停止')

    def restart(self):
        """
        Watchdog再起動（回数カウント付き）
        """
        # 1時間経過でカウンタリセット
        if time.time() - self.watchdog_restart_reset_time > 3600:
            self.watchdog_restart_count = 0
            self.watchdog_restart_reset_time = time.time()

        self.watchdog_restart_count += 1

        # 閾値超過でシステム停止→コンテナ再起動誘発
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
            return False
        if self.frame_time is None:
            return True  # まだフレーム取得前
        if time.time() - self.frame_time > self.WATCHDOG_TIMEOUT:
            return False
        return True

    def _connect(self) -> cv2.VideoCapture:
        """RTSPストリームへの接続"""
        cap = cv2.VideoCapture(self.rtsp_url)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        logger.info(f'RTSP接続完了: {self.rtsp_url}')
        return cap

    def _handle_reconnect(self) -> bool:
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
            return False

        # 指数バックオフで待機
        wait = min(30, 2 ** (self.reconnect_count - 1))
        logger.warning(f'再接続 {self.reconnect_count}/{self.MAX_RECONNECT_PER_HOUR} ({wait}秒後)')

        with self.cap_lock:
            if self.cap is not None:
                self.cap.release()
            time.sleep(wait)
            self.cap = self._connect()

        self.last_successful_read_time = time.time()
        return True

    def _grab_loop(self):
        """
        第1防波堤: grab専用ループ

        常時grabを呼んでバッファを消費し続ける。
        これにより古いフレームが滞留しない。
        """
        consecutive_failures = 0

        while self.running:
            with self.cap_lock:
                if self.cap is None:
                    time.sleep(0.1)
                    continue

                ret = self.cap.grab()

            if ret:
                consecutive_failures = 0
                self.new_frame_available.set()  # 新しいフレームがあることを通知
            else:
                consecutive_failures += 1
                if consecutive_failures > 30:  # 約300ms連続失敗
                    logger.warning('grab連続失敗 - 再接続を試行')
                    if not self._handle_reconnect():
                        break
                    consecutive_failures = 0

            time.sleep(self.GRAB_INTERVAL)

    def _retrieve_loop(self):
        """
        フレーム取得ループ

        grabが成功したら最新フレームをretrieveで取得。
        """
        while self.running:
            # 新しいフレームを待つ（最大100ms）
            self.new_frame_available.wait(timeout=0.1)
            self.new_frame_available.clear()

            if not self.running:
                break

            current_time = time.time()

            with self.cap_lock:
                if self.cap is None:
                    continue

                ret, frame = self.cap.retrieve()

            if ret and frame is not None:
                with self.lock:
                    self.frame = frame
                    self.frame_time = current_time

                self.last_successful_read_time = current_time

            # 第2防波堤: read間隔監視
            if self.last_successful_read_time:
                read_interval = current_time - self.last_successful_read_time
                if read_interval > self.READ_INTERVAL_THRESHOLD:
                    logger.warning(f'read間隔異常: {read_interval:.1f}秒')
                    if not self._handle_reconnect():
                        break

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
        ヘルスチェック用統計を返す
        """
        return {
            'is_healthy': self.is_healthy(),
            'system_halted': self.system_halted,
            'delay_seconds': round(self.delay_seconds, 2),
            'reconnect_count': self.reconnect_count,
            'watchdog_restart_count': self.watchdog_restart_count,
        }
