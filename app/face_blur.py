# 食堂混雑検知システム - 顔ぼかしモジュール
# OpenCV Haar Cascadeによる軽量な顔検出とぼかし処理

import logging
import cv2
import numpy as np
import os

logger = logging.getLogger(__name__)


class FaceBlur:
    """
    顔ぼかし処理クラス
    
    OpenCVのHaar Cascadeを使用して顔を検出し、ぼかしをかける
    軽量で確実に動作する
    """
    
    def __init__(self, blur_strength: int = 99, scale_factor: float = 1.1, min_neighbors: int = 5):
        """
        Args:
            blur_strength: ぼかしの強さ（奇数、大きいほど強い）
            scale_factor: 検出時のスケールファクター
            min_neighbors: 検出の最小近傍数
        """
        self.blur_strength = blur_strength if blur_strength % 2 == 1 else blur_strength + 1
        self.scale_factor = scale_factor
        self.min_neighbors = min_neighbors
        self.face_cascade = None
        self.profile_cascade = None
        
        self._initialize()
    
    def _initialize(self):
        """Haar Cascadeを初期化"""
        try:
            # OpenCVに含まれるHaar Cascadeファイルのパス
            cascade_path = cv2.data.haarcascades
            
            # 正面顔用
            frontal_path = os.path.join(cascade_path, 'haarcascade_frontalface_default.xml')
            if os.path.exists(frontal_path):
                self.face_cascade = cv2.CascadeClassifier(frontal_path)
                logger.info('正面顔検出器ロード完了')
            
            # 横顔用（オプション）
            profile_path = os.path.join(cascade_path, 'haarcascade_profileface.xml')
            if os.path.exists(profile_path):
                self.profile_cascade = cv2.CascadeClassifier(profile_path)
                logger.info('横顔検出器ロード完了')
            
            if self.face_cascade is None or self.face_cascade.empty():
                logger.error('顔検出器のロードに失敗しました')
                self.face_cascade = None
            else:
                logger.info(f'顔ぼかし初期化完了 (blur_strength={self.blur_strength})')
                
        except Exception as e:
            logger.error(f'顔検出初期化エラー: {e}')
            self.face_cascade = None
    
    def blur_faces(self, frame: np.ndarray) -> np.ndarray:
        """
        フレーム内の顔にぼかしをかける
        
        Args:
            frame: 入力画像 (BGR)
            
        Returns:
            顔がぼかされた画像
        """
        if self.face_cascade is None or frame is None:
            return frame
        
        try:
            # グレースケールに変換
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            
            # 正面顔を検出
            faces = self.face_cascade.detectMultiScale(
                gray,
                scaleFactor=self.scale_factor,
                minNeighbors=self.min_neighbors,
                minSize=(30, 30),
                flags=cv2.CASCADE_SCALE_IMAGE
            )
            
            # 横顔も検出（存在する場合）
            if self.profile_cascade is not None and not self.profile_cascade.empty():
                profiles = self.profile_cascade.detectMultiScale(
                    gray,
                    scaleFactor=self.scale_factor,
                    minNeighbors=self.min_neighbors,
                    minSize=(30, 30),
                    flags=cv2.CASCADE_SCALE_IMAGE
                )
                # 正面顔と横顔を結合
                if len(profiles) > 0:
                    if len(faces) > 0:
                        faces = np.vstack((faces, profiles))
                    else:
                        faces = profiles
            
            # 検出された顔にぼかしをかける
            for (x, y, w, h) in faces:
                # 顔領域を少し拡大（髪や耳も隠す）
                padding = int(w * 0.2)
                x1 = max(0, x - padding)
                y1 = max(0, y - padding)
                x2 = min(frame.shape[1], x + w + padding)
                y2 = min(frame.shape[0], y + h + padding)
                
                # 顔領域にぼかしをかける
                face_region = frame[y1:y2, x1:x2]
                if face_region.size > 0:
                    blurred = cv2.GaussianBlur(
                        face_region,
                        (self.blur_strength, self.blur_strength),
                        30
                    )
                    frame[y1:y2, x1:x2] = blurred
            
            return frame
            
        except Exception as e:
            logger.error(f'顔ぼかし処理エラー: {e}')
            return frame
    
    def detect_faces_count(self, frame: np.ndarray) -> int:
        """顔の数を返す（デバッグ用）"""
        if self.face_cascade is None or frame is None:
            return 0
        
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = self.face_cascade.detectMultiScale(
                gray,
                scaleFactor=self.scale_factor,
                minNeighbors=self.min_neighbors,
                minSize=(30, 30)
            )
            return len(faces)
        except:
            return 0
