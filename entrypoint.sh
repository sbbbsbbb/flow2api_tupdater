#!/bin/bash
set -e

echo "=========================================="
echo "Flow2API Token Updater v2.0"
echo "多 Profile 管理版"
echo "=========================================="
echo ""
echo "管理界面: http://localhost:8080"
echo "noVNC:    http://localhost:${NOVNC_PORT:-6080}/vnc.html"
if [ "${VNC_PASSWORD:-}" = "flow2api" ]; then
  echo "VNC 密码: 使用默认值，请尽快修改"
else
  echo "VNC 密码: 已设置"
fi
echo ""
echo "=========================================="

# 确保目录存在
mkdir -p /app/logs /app/profiles /app/data

# 启动 supervisor
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf