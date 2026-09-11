#!/usr/bin/env python3
"""Watches the power/home button (pmic_pwrkey, KEY_POWER):
  - quick tap            -> toggle the screen's backlight on/off (sleep/wake)
                            -- only while the dashboard is the active UI;
                            the stock gl_screen UI handles taps itself
  - press-and-hold ~1-2s -> switch between the custom dashboard and the
                            stock GL.iNet screen

Runs as its own always-on service, independent of which screen UI is
currently active (the hold has to work from the stock UI too). Passively reads /dev/input/event1 (does not grab it
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

Second pass replaced the "ignore nearby edges" debounce with a
confirm-window on release instead: a release is only treated as final
after CONFIRM_WINDOW seconds of no further press edges. Any bounce
(however many extra press/release edges arrive in between) just keeps
re-confirming the same episode without ever losing track of when it truly
started, so the state can never get stuck.

Third pass (this one) attacks the latency that design costs, but only
where it can be attacked safely:

  * The TAP still waits out CONFIRM_WINDOW, and that is not a fixable
    shortcoming -- the measured data forces it. One physical press can
    spread bounce edges over ~195ms while genuine taps last 68-200ms; the
    two ranges overlap, so *any* rule that acts on the first release edge
    will sometimes fire a tap in the middle of a hold. The window is
    trimmed 0.25 -> 0.22 (still clear of the 195ms worst case) and
    otherwise left alone.

  * The HOLD no longer waits for release at all when the dashboard is the
    active UI: the moment the press crosses HOLD_MIN it cannot be a tap
    any more, so the switch request fires right then, under the user's
    finger, instead of ~1.2s later. That is safe *because* the dashboard
    now only receives a request and asks for confirmation on screen (see
    SWITCH_REQUEST_FILE in dashboard.py) -- if the hold was actually
    headed for the hardware's poweroff, the router powers off with an
    unanswered dialog on screen and nothing has changed.

  * Going the other way (stock UI -> dashboard) there is no dashboard
    running to confirm with, so that direction still switches directly,
    and therefore still waits for release and respects HOLD_MAX. Firing it
    early would mean every poweroff hold also flipped the UI and the
    router would come back up in the wrong one.
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
# tap) during diagnosis was ~195ms; this keeps comfortable margin above it.
CONFIRM_WINDOW = 0.22

HOLD_MIN = 1.0    # held at least this long -> UI switch, not a tap
HOLD_MAX = 2.5    # confirmed-held longer than this -> assume it's headed for
                  # the hardware's own long-press poweroff; don't act here.

TOGGLE = "/root/dashboard/toggle.sh"
SLEEP_TOGGLE = "/root/dashboard/screen_sleep.sh"
SWITCH_REQUEST_FILE = "/tmp/dashboard_ui_switch_request"


def log(msg):
    try:
        subprocess.run(["logger", "-t", "homebutton", msg])
    except Exception:
        pass


def dashboard_running():
    try:
        out = subprocess.run(["pgrep", "-f", "dashboard/run.sh"], capture_output=True)
        return out.returncode == 0
    except Exception:
        return False


def request_switch_to_stock():
    """Ask the running dashboard to confirm on screen before handing the
    display back. A dropped file is enough IPC here -- the dashboard polls
    for it a few times a second -- and it keeps this watcher from needing
    to know anything about the UI's state."""
    try:
        with open(SWITCH_REQUEST_FILE, "w") as f:
            f.write(str(time.time()))
        log("hold -> asked dashboard to confirm switch to stock UI")
        return True
    except Exception as e:
        log(f"hold -> could not write switch request ({e})")
        return False


def do_ui_toggle():
    target = "off" if dashboard_running() else "on"
    subprocess.run([TOGGLE, target])
    log(f"hold -> toggle {target}")


def do_sleep_toggle():
    subprocess.run([SLEEP_TOGGLE, "toggle"])
    log("tap -> sleep toggle")


def main():
    down_at = None          # when the current press episode first started
    pending_release = None  # timestamp of the most recent release edge,
                            # tentative until CONFIRM_WINDOW passes with no
                            # further press
    acted = False           # this episode already produced an action

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
                    if val == 1:
                        if down_at is None:
                            down_at = time.time()
                            acted = False
                        pending_release = None  # any new press cancels a
                                                # tentative release -- it
                                                # was bounce, still down
                    elif val == 0 and down_at is not None:
                        pending_release = time.time()
            else:
                time.sleep(0.005)

            now = time.time()

            # Still held and already past HOLD_MIN: it cannot be a tap any
            # more, so give feedback now rather than after release. Only
            # valid in the ask-first direction -- see the module docstring.
            if (down_at is not None and not acted and pending_release is None
                    and now - down_at >= HOLD_MIN and dashboard_running()):
                if request_switch_to_stock():
                    acted = True

            if pending_release is not None and now - pending_release >= CONFIRM_WINDOW:
                held = pending_release - down_at
                down_at = None
                pending_release = None
                if acted:
                    acted = False           # already handled mid-hold
                elif held < HOLD_MIN:
                    # Taps are only ours while the dashboard is on screen.
                    # The stock gl_screen UI handles the power-key tap
                    # itself (back on a sub-page, sleep/wake on its home
                    # page); toggling the backlight here as well made one
                    # tap do both -- back AND blank, or wake then
                    # immediately blank again -- so on the stock UI a tap
                    # is left entirely to gl_screen.
                    if dashboard_running():
                        do_sleep_toggle()
                    else:
                        log("tap -> stock UI active, left to gl_screen")
                elif held <= HOLD_MAX:
                    do_ui_toggle()
                # held > HOLD_MAX: leave it to the hardware's own long-press
                # path, don't act here.


if __name__ == "__main__":
    main()
