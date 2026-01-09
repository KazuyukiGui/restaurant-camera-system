# 食堂混雑検知システム - 人物検知モジュール
# YOLO11を使用した人物検知（CPU最適化）

import os
import logging
from typing import Tuple, List
import numpy as np

logger = logging.getLogger(__name__)

# YOLO importはtry-exceptで囲む（インストールされていない環境でのエラー回避）
try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False
    logger.warning('ultralytics not installed - detection disabled')


class PersonDetector:
    """
    YOLO11ベースの人物検知クラス

    CPU環境（Core i3-10105T）向けに最適化:
    - 推論サイズ: 416x416 (デフォルト)
    - 人物クラスのみ検知
    - YOLO11n: YOLOv8nより+2.2 mAP向上、30%高速化
    """
    
    # 人物クラスID (COCO dataset)
    PERSON_CLASS_ID = 0
    
    # 混雑レベル閾値
    CROWDING_THRESHOLDS = {
        'low': 6,       # 0-6人: 空いている
        'medium': 10,   # 7-10人: やや混雑
        'high': float('inf')  # 11人以上: 混雑
    }
    
    def __init__(self, model_path: str = None, imgsz: int = None):
        """
        Args:
            model_path: YOLOモデルのパス (デフォルト: yolo11n.pt)
            imgsz: 推論画像サイズ (デフォルト: 環境変数 IMGSZ または 416)
        """
        self.imgsz = imgsz or int(os.getenv('IMGSZ', '416'))
        self.model_path = model_path or os.getenv('YOLO_MODEL', 'yolo11n.pt')
        self.model = None
        self.confidence_threshold = float(os.getenv('CONFIDENCE_THRESHOLD', '0.5'))
        
        if YOLO_AVAILABLE:
            self._load_model()
        else:
            logger.error('YOLO not available - detection will return empty results')
    
    def _load_model(self):
        """モデルをロード（OpenVINO対応）"""
        try:
            target_model = self.model_path
            
            # .ptファイルが指定された場合、OpenVINO形式への変換を試みる
            if self.model_path.endswith('.pt'):
                # パスのみ取得（拡張子除去）
                base_name = os.path.splitext(self.model_path)[0]
                ov_model_dir = f'{base_name}_openvino_model'
                
                # OpenVINOモデルが未作成ならエクスポート
                if not os.path.exists(ov_model_dir):
                    logger.info(f'OpenVINOモデルへ変換中: {self.model_path} -> {ov_model_dir}')
                    try:
                        # 一度PyTorchモデルとしてロード
                        pt_model = YOLO(self.model_path)
                        # エクスポート実行 (imgszを合わせて最適化)
                        pt_model.export(format='openvino', imgsz=self.imgsz)
                        logger.info('OpenVINO変換完了')
                        target_model = ov_model_dir
                    except Exception as e:
                        logger.error(f'OpenVINO変換失敗: {e} - PyTorchモデルを使用します')
                        target_model = self.model_path
                else:
                    logger.info(f'既存のOpenVINOモデルを使用: {ov_model_dir}')
                    target_model = ov_model_dir
            
            self.model = YOLO(target_model)
            logger.info(f'YOLOモデルロード完了: {target_model} (imgsz={self.imgsz})')
        except Exception as e:
            logger.error(f'モデルロード失敗: {e}')
            self.model = None
    
    def detect_persons(self, frame: np.ndarray) -> Tuple[int, List[dict], float]:
        """
        フレームから人物を検知
        
        Args:
            frame: 入力画像 (BGR形式)
            
        Returns:
            Tuple[int, List[dict], float]: (人数, 検知結果リスト, 平均信頼度)
        """
        if self.model is None or frame is None:
            return 0, [], 0.0
        
        try:
            # YOLO推論
            results = self.model(
                frame,
                imgsz=self.imgsz,
                conf=self.confidence_threshold,
                classes=[self.PERSON_CLASS_ID],  # 人物のみ
                verbose=False
            )
            
            detections = []
            confidences = []
            
            for result in results:
                boxes = result.boxes
                if boxes is not None:
                    for box in boxes:
                        conf = float(box.conf[0])
                        xyxy = box.xyxy[0].tolist()
                        
                        detections.append({
                            'bbox': xyxy,
                            'confidence': conf
                        })
                        confidences.append(conf)
            
            person_count = len(detections)
            avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0
            
            return person_count, detections, avg_confidence
            
        except Exception as e:
            logger.error(f'検知エラー: {e}')
            return 0, [], 0.0
    
    def get_crowding_level(self, person_count: int) -> str:
        """
        人数から混雑レベルを判定
        
        Args:
            person_count: 検知された人数
            
        Returns:
            str: 'low', 'medium', 'high'
        """
        if person_count <= self.CROWDING_THRESHOLDS['low']:
            return 'low'
        elif person_count <= self.CROWDING_THRESHOLDS['medium']:
            return 'medium'
        else:
            return 'high'
    
    def process_frame(self, frame: np.ndarray) -> dict:
        """
        フレームを処理して混雑情報を返す
        
        Args:
            frame: 入力画像
            
        Returns:
            dict: 混雑情報
        """
        person_count, detections, avg_confidence = self.detect_persons(frame)
        crowding_level = self.get_crowding_level(person_count)
        
        return {
            'person_count': person_count,
            'crowding_level': crowding_level,
            'confidence': round(avg_confidence, 3),
            'detections': detections
        }
    
    def draw_detections(self, frame: np.ndarray, detections: List[dict]) -> np.ndarray:
        """
        検知結果を画像に描画
        
        Args:
            frame: 入力画像
            detections: 検知結果リスト
            
        Returns:
            np.ndarray: 描画済み画像
        """
        import cv2
        
        output = frame.copy()
        
        for det in detections:
            bbox = det['bbox']
            conf = det['confidence']
            
            x1, y1, x2, y2 = map(int, bbox)
            
            # バウンディングボックス
            cv2.rectangle(output, (x1, y1), (x2, y2), (0, 255, 0), 2)
            
            # 信頼度ラベル
            label = f'{conf:.2f}'
            cv2.putText(output, label, (x1, y1 - 10),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
        
        return output

