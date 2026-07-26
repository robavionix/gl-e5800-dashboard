#!/usr/bin/env python3
"""Watches the power/home button (pmic_pwrkey, KEY_POWER):
  - quick tap            -> toggle the screen's backlight on/off (sleep/wake)
  - press-and-hold ~1-2s -> toggle between the custom dashboard and the
                            stock GL.iNet screen

Runs as its own always-on service, independent of which screen UI is
currently active. Passively reads /dev/input/event1 (does not grab it
exclusively), so it never interferes with any other consumer of the power
key. A genuine long hold well past HOLD_MAX is handled entirely
separately, at the kernel/procd level, by /etc/rc.button/power ->
poweroff; this script only acts within its own short hold-time window and
doesn't intersect with that path.

Rewritten 2026-07-26, twice. First pass replaced an earlier tap-counting
design (1 press = sleep toggle, 3 presses within a silence window = UI
switch) with duration-based classification (measure how long the button
was held on release), because a single physical tap could produce two raw
press/release cycles and confuse the counter. That was an improvement but
still wrong: it used a blanket "ignore any edge within 80ms of the last
one" debounce filter, and a live 60s capture of raw KEY_POWER events
against this script's own action log proved several clean, well-formed
presses (including a full 2.19s hold) produced *no action at all*. Root
cause: a real release edge can land within 80ms of the preceding press
edge -- measured natural tap durations here run 68-200ms, i.e. comparable
to or shorter than the debounce window -- so the debounce filter would
sometimes discard the *genuine* release. That left the internal "are we
currently down" state stuck, silently merging the next real press
(however clean) into one bogus multi-second span, which then landed past
HOLD_MAX and got thrown away as "probably headed for the hardware's own
long-press path." Matches the reported symptom exactly: the same gesture
working sometimes and doing nothing other times, unpredictably.

Second pass (this one) replaces the "ignore nearby edges" debounce with a
confirm-window on release instead: a release is only treated as final
after CONFIRM_WINDOW seconds of no further press edges. Any bounce
(however many extra press/release edges arrive in between) just keeps
re-confirming the same episode without ever losing track of when it
truly started, so the state can never get stuck -- every episode
eventually resolves once the button is genuinely left alone. This adds
CONFIRM_WINDOW of latency to every action, tap or hold, which is a
deliberate trade: a small, constant, honest delay beats a gesture that
silently does nothing some fraction of the time.
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

# Longest observed bounce burst (first edge to last edge of one physical
# tap) during diagnosis was ~195ms; this gives comfortable margin above that.
CONFIRM_WINDOW = 0.25

HOLD_MIN = 1.0    # confirmed-held at least this long -> UI switch, not a tap
HOLD_MAX = 2.5    # confirmed-held longer than this -> assume it's headed for
                  # the hardware's own long-press poweroff; don't act here.

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
    down_at = None          # when the current press episode first started
    pending_release = None  # timestamp of the most recent release edge,
                             # tentative until CONFIRM_WINDOW passes with no
                             # further press
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
                    if val == 1:
                        if down_at is None:
                            down_at = now
                        pending_release = None  # any new press cancels a
                                                 # tentative release -- it
                                                 # was bounce, still down
                    elif val == 0 and down_at is not None:
                        pending_release = now
            else:
                time.sleep(0.005)

            if pending_release is not None and time.time() - pending_release >= CONFIRM_WINDOW:
                held = pending_release - down_at
                down_at = None
                pending_release = None
                if held < HOLD_MIN:
                    do_sleep_toggle()
                elif held <= HOLD_MAX:
                    do_ui_toggle()
                # held > HOLD_MAX: leave it to the hardware's own long-press
                # path, don't act here.


if __name__ == "__main__":
    main()
