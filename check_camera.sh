#!/bin/bash
# USBカメラ診断スクリプト

echo "=== USBカメラ診断レポート ==="
echo ""

echo "1. カメラデバイスの存在確認"
ls -la /dev/video* 2>&1
echo ""

echo "2. Dockerコンテナの状態"
docker ps -a | grep -E "(usb-camera|rtsp-server|cafeteria-counter)"
echo ""

echo "3. USBカメラコンテナの最終エラー（最後の10行）"
docker logs usb-camera-stream --tail 10 2>&1 | tail -5
echo ""

echo "4. RTSPサーバーの状態"
docker logs rtsp-server --tail 5 2>&1 | tail -5
echo ""

echo "5. カメラデバイスの権限確認"
stat /dev/video0 2>&1 | grep -E "(Access|Uid|Gid)"
echo ""

echo "6. カメラが使用中かどうか"
lsof /dev/video0 2>&1 || echo "カメラは使用されていません"
echo ""

echo "7. ネットワーク接続確認（RTSPサーバー）"
timeout 2 bash -c "</dev/tcp/localhost/8554" 2>&1 && echo "RTSPサーバーは応答しています" || echo "RTSPサーバーに接続できません"
echo ""

echo "=== 診断完了 ==="



