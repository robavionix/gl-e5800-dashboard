#!/bin/sh
# Switch the built-in screen between the custom UK/China dashboard and the
# stock GL.iNet UI. The choice persists across reboots (enable/disable).
#
# Usage: toggle.sh on|off|status

wait_gone() {
    # wait briefly for a process name to fully exit; force-kill any
    # survivor immediately rather than waiting out procd's own ~5s
    # SIGTERM-then-SIGKILL grace period (gl_screen ignores SIGTERM, so
    # letting procd's stop run to completion made every "on" switch
    # take 5+ seconds)
    n=0
    while pgrep -f "$1" >/dev/null 2>&1 && [ "$n" -lt 6 ]; do
        sleep 0.2
        n=$((n + 1))
    done
    # no pkill on this busybox -- kill any survivors by PID instead
    pids=$(pgrep -f "$1" 2>/dev/null)
    [ -n "$pids" ] && kill -9 $pids 2>/dev/null
}

case "$1" in
    on)
        /etc/init.d/gl_screen stop
        wait_gone "/usr/bin/gl_screen"
        /etc/init.d/gl_screen disable
        /etc/init.d/citydash enable
        /etc/init.d/citydash start
        echo "dashboard ON (stock screen disabled)"
        ;;
    off)
        /etc/init.d/citydash stop
        wait_gone "dashboard/run.sh"
        /etc/init.d/citydash disable
        /etc/init.d/gl_screen enable
        /etc/init.d/gl_screen start
        echo "dashboard OFF (stock screen restored)"
        ;;
    status)
        if /etc/init.d/citydash running 2>/dev/null; then
            echo "dashboard: running"
        else
            echo "dashboard: stopped"
        fi
        if /etc/init.d/gl_screen running 2>/dev/null; then
            echo "stock gl_screen: running"
        else
            echo "stock gl_screen: stopped"
        fi
        ;;
    *)
        echo "usage: $0 on|off|status"
        exit 1
        ;;
esac
