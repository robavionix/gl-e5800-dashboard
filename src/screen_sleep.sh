#!/bin/sh
# Real hardware backlight on/off (not a fake black frame) for the built-in
# screen. Saves/restores the previous brightness level.

BL=/sys/class/backlight/soc:backlight
STATE_FILE=/tmp/dashboard_backlight_saved

case "$1" in
    off)
        cur=$(cat "$BL/brightness" 2>/dev/null)
        [ -n "$cur" ] && [ "$cur" -gt 0 ] && echo "$cur" > "$STATE_FILE"
        echo 0 > "$BL/brightness"
        ;;
    on)
        if [ -f "$STATE_FILE" ]; then
            cat "$STATE_FILE" > "$BL/brightness"
        else
            cat "$BL/max_brightness" > "$BL/brightness"
        fi
        ;;
    toggle)
        cur=$(cat "$BL/brightness" 2>/dev/null)
        if [ "$cur" = "0" ]; then
            "$0" on
        else
            "$0" off
        fi
        ;;
    status)
        cur=$(cat "$BL/brightness" 2>/dev/null)
        [ "$cur" = "0" ] && echo "asleep" || echo "awake"
        ;;
    *)
        echo "usage: $0 on|off|toggle|status"
        exit 1
        ;;
esac
