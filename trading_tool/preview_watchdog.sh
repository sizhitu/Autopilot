#!/usr/bin/env bash
# preview-8000 独立看门狗（第二层守护，由 supervisord 直接托管）
#
# 设计要点（针对此前发现的 supervisord desync 根因）：
#   * 本看门狗【不】后台任何长驻子进程，自身是简单 while+sleep 循环，
#     因此 supervisord 能可靠地 autorestart 它（已验证）。
#   * preview-8000 现已改为由 supervisord 直接托管真实的 web_app.py 进程
#     （不再是"带长驻子进程的包装脚本"），故 supervisord 的 autorestart /
#     restart 都能正确作用于真实服务进程，避免 desync。
#   * 本看门狗只负责"补位"：周期性探测 HTTP 健康 + 进程一致性，发现异常
#     时调用 `supervisord ctl restart preview-8000` 拉起。
#
# 监测：
#   1) HTTP 连续 2 次失败 -> restart；
#   2) 受管 pid 必须真实存活（kill -0）；
#   3) 真实 web_app.py 的 pid 必须等于受管 pid（无孤儿/错配）。
#   任一异常 -> restart。冷却 60s 防风暴。
set -u

SUPERVISORD_BIN="/.PlnPyKFp4CRfFtgC1/bin/supervisord"
SUPERVISOR_CONF="/.PlnPyKFp4CRfFtgC1/supervisord-conf/supervisord.conf"
PORT=8000
PROGRAM=preview-8000
COOLDOWN=60
POLL=30

http_fail_count=0
last_restart=0

# 返回 "STATE:PID"；注意 supervisord status 带 ANSI 颜色码（行首 ESC），
# 故用 awk 全行匹配，不能用 ^ 锚定 grep。
prog_state() {
    ${SUPERVISORD_BIN} ctl -c ${SUPERVISOR_CONF} status 2>/dev/null \
        | awk -v p="${PROGRAM}" '$0 ~ p {st=$2; pid=""; for(i=1;i<=NF;i++) if($i=="pid"){gsub(/,/,"",$(i+1)); pid=$(i+1)}; print st":"pid; exit}'
}
real_web() { pgrep -f "web_app.py" | head -1; }
alive()    { local p="$1"; [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }
http_ok()  { curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; }

do_restart() {
    local now; now=$(date +%s)
    if [ $((now - last_restart)) -lt $COOLDOWN ]; then
        echo "[watchdog] $(date) 冷却期内，跳过 restart"
        return
    fi
    last_restart=$now
    echo "[watchdog] $(date) 触发 restart ${PROGRAM} ..."
    ${SUPERVISORD_BIN} ctl -c ${SUPERVISOR_CONF} restart "${PROGRAM}" 2>&1 | sed 's/^/[watchdog] /'
}

echo "[watchdog] started pid $$ at $(date)"
while true; do
    sleep $POLL

    if http_ok; then
        http_fail_count=0
    else
        http_fail_count=$((http_fail_count + 1))
        echo "[watchdog] $(date) HTTP 探测失败 (连续 ${http_fail_count} 次)"
        if [ "$http_fail_count" -ge 2 ]; then
            do_restart
            http_fail_count=0
            continue
        fi
    fi

    INFO=$(prog_state)
    ST=${INFO%%:*}
    MGR=${INFO##*:}
    WEB=$(real_web)

    # 过渡态（Starting/Backoff/Stopping）不触发重启，避免与 supervisord 自愈竞态
    if [ "$ST" = "STARTING" ] || [ "$ST" = "BACKOFF" ] || [ "$ST" = "STOPPING" ]; then
        echo "[watchdog] $(date) 过渡态($ST)，跳过一致性检查"
        continue
    fi

    if [ "$ST" = "RUNNING" ]; then
        if ! alive "$MGR"; then
            echo "[watchdog] $(date) 受管 pid=$MGR 已失效 -> restart"
            do_restart
            continue
        fi
        if [ -n "$WEB" ] && [ "$WEB" != "$MGR" ]; then
            echo "[watchdog] $(date) web_app($WEB) 与受管($MGR) 不一致 -> restart"
            do_restart
            continue
        fi
    elif [ "$ST" = "STOPPED" ] || [ "$ST" = "FATAL" ] || [ "$ST" = "EXITED" ]; then
        # 稳定态但非运行：拉起
        echo "[watchdog] $(date) 程序状态=$ST -> restart"
        do_restart
        continue
    fi

    echo "[watchdog] $(date) 状态一致 ok (web_app=$WEB)"
done
