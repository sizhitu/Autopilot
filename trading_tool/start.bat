@echo off
REM 本地一键启动（Windows）
REM 用法：双击 start.bat 即可，默认 http://localhost:8000
cd /d "%~dp0"

echo ==^> 安装依赖
pip install -r requirements.txt

echo ==^> 启动 Web 服务 (http://localhost:8000)
python web_app.py
pause
