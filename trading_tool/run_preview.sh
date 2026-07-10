#!/usr/bin/env bash
# preview-8000 自愈启动器（第一层守护）
# 职责：
#   1) 启动前清理残留 web_app 进程，释放 8000 端口，防止休眠恢复后僵尸占用；
#   2) web_app 异常退出后自动重启（外层 supervisord 再兜一层）；
#   3) 退出时（被 supervisord stop / 收到信号）清理 web_app，避免孤儿进程。
#
# 注意：本脚本【不再】后台启动看门狗。看门狗已独立为 supervisord 程序
#       preview-watchdog，由 supervisord 直接托管，避免"后台子进程被 reparent
#       到 PID1 后干扰 supervisord 对本脚本死亡的检测"这一根因问题。
set -u

APP=/workspace/trading_tool/web_app.py

cleanup() {
    # 仅清理 web_app，不再触碰看门狗（看门狗由 supervisord 独立管理）
    pkill -f "[w]eb_app.py" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[run_preview] guard started at $(date)"

while true; do
    # 启动前清理上一轮可能的残留，释放端口（含孤儿进程）
    pkill -f "[w]eb_app.py" 2>/dev/null || true
    sleep 1

    echo "[run_preview] launching web_app.py at $(date)"
    python3.11 "$APP"
    CODE=$?
    echo "[run_preview] web_app.py exited (code=$CODE) at $(date)"

    # 退避，避免重启风暴
    sleep 3
done
