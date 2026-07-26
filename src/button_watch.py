#!/usr/bin/env python3
"""Watches the power/home button (pmic_pwrkey, KEY_POWER):
  - quick tap            -> toggle the screen's backlight on/off (sleep/wake)
  - press-and-hold ~1-2s -> toggle between the custom dashboard and the
                            stock GL.iNet screen

Fires the moment you release the button -- no waiting to see whether more
presses follow. Runs as its own always-on service, independent of which
screen UI is currently active. Passively reads /dev/input/event1 (does not
grab it exclusively), so it never interferes with any other consumer of
the power key. A genuine long hold well past HOLD_MAX is handled entirely
separately, at the kernel/procd level, by /etc/rc.button/power ->
poweroff; this script only acts within its own short hold-time window and
doesn't intersect with that path.

Rewritten 2026-07-26 from an earlier tap-counting design (1 press = sleep
toggle, 3 presses within a silence window = UI switch, decided ~0.6s after
the last press). Measured on the real device: a single physical tap can
produce two raw press/release cycles several hundred ms apart (contact
bounce, and/or the user pressing again since the old design gave zero
feedback for ~0.6s). That fed a bogus tap count -- silently ignored, or
occasionally miscounted as 3 (unwanted UI switch) -- matching the exact
symptom reported: "press power, nothing happens" alternating with "screen
flashes on then immediately off". Duration-based detection sidesteps this
entirely: each press/release pair is handled on its own the instant it
completes, so it doesn't matter how many raw edges bounce in between, and
there's no artificial delay for the common quick-tap case.
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

DEBOUNCE = 0.08   # ignore edges within this long of the last one (contact bounce)
HOLD_MIN = 1.0    # held at least this long on release -> UI switch, not a tap
HOLD_MAX = 2.5    # held longer than this -> assume it's headed for the hardware's
                  # own long-press poweroff; don't act on release at all

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
    subprocess.run(["logger", "-t", "homebutton", f"hold -> toggle {target}"])


def do_sleep_toggle():
    subprocess.run([SLEEP_TOGGLE, "toggle"])
    subprocess.run(["logger", "-t", "homebutton", "tap -> sleep toggle"])


def main():
    down_at = None
    last_edge = 0.0
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
                if typ == EV_KEY and code == KEY_POWER and val in (0, 1):
                    now = time.time()
                    if now - last_edge < DEBOUNCE:
                        continue
                    last_edge = now
                    if val == 1 and down_at is None:
                        down_at = now
                    elif val == 0 and down_at is not None:
                        held = now - down_at
                        down_at = None
                        if held < HOLD_MIN:
                            do_sleep_toggle()
                        elif held <= HOLD_MAX:
                            do_ui_toggle()
                        # held > HOLD_MAX: leave it to the hardware's own
                        # long-press path, don't act here.
            else:
                time.sleep(0.01)


if __name__ == "__main__":
    main()
