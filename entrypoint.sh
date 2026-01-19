#!/bin/bash
set -e

echo "=========================================="
echo "Flow2API Token Updater v3.0"
echo "轻量版 - Cookie 导入模式"
echo "=========================================="
echo ""
echo "管理界面: http://localhost:8002"
echo ""
echo "=========================================="

# 确保目录存在
mkdir -p /app/logs /app/profiles /app/data

# 直接启动应用
exec python -m token_updater.main
