#!/usr/bin/env bash
# 本地一键启动（Linux / macOS）
# 用法：
#   ./start.sh            # 默认 http://localhost:8000
#   PORT=8080 ./start.sh  # 自定义端口
set -e
cd "$(dirname "$0")"

echo "==> 安装依赖 (pip install -r requirements.txt)"
pip install -r requirements.txt

echo "==> 启动 Web 服务"
echo "    访问地址: http://localhost:${PORT:-8000}"
exec python3 web_app.py
