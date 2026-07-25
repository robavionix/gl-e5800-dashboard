#!/usr/bin/env python3
"""Watches the power/home button (pmic_pwrkey, KEY_POWER):
  - 1 press  -> toggle the screen's backlight on/off (sleep/wake)
  - 3 presses within the window -> toggle between the custom dashboard and
    the stock GL.iNet screen
  - 2 presses -> ignored (ambiguous)

A press isn't acted on immediately -- we wait for a short silence after the
last press before deciding, so a triple-tap doesn't also fire the
single-press action on its first press.

Runs as its own always-on service, independent of which screen UI is
currently active. Passively reads /dev/input/event1 (does not grab it
exclusively), so it never interferes with any other consumer of the power
key. A long hold is handled entirely separately by /etc/rc.button/power
(-> poweroff) through a different kernel pathway; this script only ever
sees short taps.
"""
import os
import struct
import subprocess
import time

DEV = "/dev/input/event1"
EVENT_FMT = "qqHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)
EV_KEY = 1
KEY_POWER = 116

SILENCE_WINDOW = 0.6  # seconds of silence after the last press before deciding

TOGGLE = "/root/dashboard/toggle.sh"
SLEEP_TOGGLE = "/root/dashboard/screen_sleep.sh"


def dashboard_running():
    try:
        out = subprocess.run(["pgrep", "-f", "dashboard/run.sh"], capture_output=True)
        return out.returncode == 0
    except Exception:
        return False


def do_ui_toggle():
    target = "off" if dashboard_running() else "on"
    subprocess.run([TOGGLE, target])
    subprocess.run(["logger", "-t", "homebutton", f"triple-tap -> toggle {target}"])


def do_sleep_toggle():
    subprocess.run([SLEEP_TOGGLE, "toggle"])
    subprocess.run(["logger", "-t", "homebutton", "single-tap -> sleep toggle"])


def main():
    presses = []
    with open(DEV, "rb") as f:
        fd = f.fileno()
        os.set_blocking(fd, False)
        while True:
            try:
                data = f.read(EVENT_SIZE)
            except (BlockingIOError, TypeError):
                data = None
            if data and len(data) == EVENT_SIZE:
                _, _, typ, code, val = struct.unpack(EVENT_FMT, data)
                if typ == EV_KEY and code == KEY_POWER and val == 1:
                    presses.append(time.time())
            else:
                time.sleep(0.02)

            if presses and time.time() - presses[-1] > SILENCE_WINDOW:
                count = len(presses)
                presses = []
                if count == 1:
                    do_sleep_toggle()
                elif count >= 3:
                    do_ui_toggle()
                # count == 2: no defined action


if __name__ == "__main__":
    main()
