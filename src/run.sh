#!/bin/sh
# Runs the dashboard, retrying on crash. After repeated crashes, or on a
# clean stop (SIGTERM -> python exits 0), hands the screen back to the
# stock gl_screen UI so the physical display is never left blank/frozen.
#
# Backgrounds the python process and traps TERM/INT so procd's stop signal
# actually reaches the child instead of orphaning it (a plain foreground
# `python3 ...` here would swallow the signal at the shell and leave the
# python process running after `/etc/init.d/citydash stop`).

LOG_TAG="citydash"
child=""

term_handler() {
    [ -n "$child" ] && kill -TERM "$child" 2>/dev/null
    wait "$child" 2>/dev/null
    exit 0
}
trap term_handler TERM INT

fails=0

while true; do
    python3 /root/dashboard/dashboard.py &
    child=$!
    wait "$child"
    rc=$?
    if [ "$rc" -eq 0 ]; then
        logger -t "$LOG_TAG" "clean stop requested"
        exit 0
    fi
    fails=$((fails + 1))
    logger -t "$LOG_TAG" "dashboard exited rc=$rc (failure $fails)"
    if [ "$fails" -ge 3 ]; then
        logger -t "$LOG_TAG" "too many crashes, falling back to stock gl_screen"
        /etc/init.d/gl_screen start
        exit 1
    fi
    sleep 2
done
