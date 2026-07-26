#!/bin/sh
# Real hardware screen sleep/wake for the built-in display (not a fake
# black frame). Saves/restores the previous brightness level.
#
# There are THREE independently-controlled pieces of hardware state here,
# found the hard way after "screen won't wake / flashes then goes dark"
# survived several rounds of fixing the power-button press detection:
#   - brightness (.../soc:backlight/brightness) -- backlight PWM duty
#     cycle. This is all any earlier version of this script ever touched.
#   - bl_power   (.../soc:backlight/bl_power) -- backlight power gate,
#     FB_BLANK semantics (0 = on, nonzero = off), managed independently by
#     GL.iNet's own /etc/gl_screen/platform.sh (get_backlight_state) --
#     not something this project ever wrote to.
#   - fb0/blank  (/sys/class/graphics/fb0/blank) -- the LCD controller's
#     own blank/sleep state. This panel is SPI-attached (fb0's device
#     symlink points at .../spi0.0), so it has hardware sleep/wake
#     commands entirely separate from backlight. THIS is the one that
#     actually determines whether the panel renders anything -- confirmed
#     live: with brightness=59 and bl_power=1 the screen was dark, writing
#     fb0/blank=0 lit it instantly with nothing else changed, and
#     fb0/blank=1 blanked it again. Something else on this device (not
#     part of this project) blanks fb0 independently of brightness, which
#     is the actual reason the screen could look "on" by brightness's
#     account while genuinely being dark, no matter how correctly the
#     button-press logic itself worked.
#
# fb0/blank reads back empty (appears write-only), so it can't be used to
# track current state -- brightness is used as that proxy for
# toggle/status instead, same as before. Since "on" always explicitly
# force-unblanks regardless of assumed prior state, this self-corrects
# within one on/off cycle even if something else blanked fb0 in between.

BL=/sys/class/backlight/soc:backlight
FB_BLANK=/sys/class/graphics/fb0/blank
STATE_FILE=/tmp/dashboard_backlight_saved

case "$1" in
    off)
        cur=$(cat "$BL/brightness" 2>/dev/null)
        [ -n "$cur" ] && [ "$cur" -gt 0 ] && echo "$cur" > "$STATE_FILE"
        echo 1 > "$FB_BLANK" 2>/dev/null
        echo 0 > "$BL/brightness"
        ;;
    on)
        if [ -f "$STATE_FILE" ]; then
            cat "$STATE_FILE" > "$BL/brightness"
        else
            cat "$BL/max_brightness" > "$BL/brightness"
        fi
        echo 0 > "$FB_BLANK" 2>/dev/null
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
