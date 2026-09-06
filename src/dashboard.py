#!/usr/bin/env python3
"""Interactive dual-city status dashboard for the GL-E5800's built-in screen.

Four main panels reached by swiping left/right: world clock, currency,
active SIM, and OpenClash. Tapping into a panel opens a sub-screen (city
picker, currency picker, data-cap picker, or the OpenClash on/off + mode
control) -- tap the header to go back, or swipe right. Writes RGB565
frames directly to /dev/fb0.

Usage:
  dashboard.py            run the live loop, drawing to /dev/fb0
  dashboard.py --preview  render every screen to PNG files in the given
                          directory (plus a contact sheet), for visual QA
  dashboard.py --calibrate  flash solid red/green/blue full-screen for
                          1s each, to verify the framebuffer colour
                          channel order on real hardware
"""
import json
import math
import os
import random
import signal
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 240, 320
FB_PATH = "/dev/fb0"
FONT_DIR = "/etc/gl_screen/language/ttf"
STATE_DIR = Path("/root/dashboard")
FX_CACHE = STATE_DIR / "fx_cache.json"
CONFIG_FILE = STATE_DIR / "config.json"
GAME_SCORES_FILE = STATE_DIR / "game_scores.json"

BG = (11, 18, 32)
FG = (230, 235, 245)
DIM = (120, 130, 150)
ACCENT = {
    "clock": (86, 182, 255),
    "fx": (255, 190, 90),
    "sim": (110, 220, 150),
    "openclash": (200, 140, 255),
    "weather": (90, 214, 200),
    "monitor": (235, 120, 160),
    "games": (255, 210, 90),
}

MCC_COUNTRY = {
    "234": "UK", "235": "UK",
    "460": "China", "461": "China",
    "454": "Hong Kong", "466": "Taiwan",
    "206": "Belgium", "208": "France", "204": "Netherlands",
    "262": "Germany", "222": "Italy", "214": "Spain", "268": "Portugal",
    "240": "Sweden", "238": "Denmark", "242": "Norway", "244": "Finland",
    "250": "Russia", "302": "Canada", "310": "USA", "311": "USA",
    "440": "Japan", "441": "Japan", "450": "South Korea", "505": "Australia",
    "228": "Switzerland", "226": "Romania", "231": "Slovakia",
}

CITIES = [
    ("Pacific/Auckland", "Auckland"),
    ("Europe/Berlin", "Berlin"),
    ("America/Chicago", "Chicago"),
    ("Asia/Dubai", "Dubai"),
    ("Asia/Hong_Kong", "Hong Kong"),
    ("Europe/London", "London"),
    ("America/Los_Angeles", "Los Angeles"),
    ("Europe/Moscow", "Moscow"),
    ("America/New_York", "New York"),
    ("Europe/Paris", "Paris"),
    ("Asia/Seoul", "Seoul"),
    ("Asia/Shanghai", "Shanghai"),
    ("Asia/Singapore", "Singapore"),
    ("Australia/Sydney", "Sydney"),
    ("Asia/Tokyo", "Tokyo"),
    ("America/Toronto", "Toronto"),
]

CURRENCIES = ["CNY", "JPY", "CAD", "AUD", "SGD", "NZD", "GBP", "EUR", "USD", "HKD"]
CURRENCY_NAMES = {
    "CNY": "Chinese Yuan", "JPY": "Japanese Yen", "CAD": "Canadian Dollar", "AUD": "Australian Dollar",
    "SGD": "Singapore Dollar", "NZD": "NZ Dollar", "GBP": "British Pound",
    "EUR": "Euro", "USD": "US Dollar", "HKD": "Hong Kong Dollar",
}

DATA_CAP_PRESETS = [
    None, 500, 1024, 2048, 3072, 5120, 10240, 15360, 20480, 30720,
    51200, 76800, 102400, 153600, 204800, 307200, 512000, 768000, 1024000,
]

# id -> (display name, lat, lon) -- same cities as the world clock, plus coords.
# Sorted alphabetically by name (the order this list is in is exactly the
# scroll order both this picker and the clock city picker show).
WEATHER_CITIES = [
    ("Aberdeen", 57.1497, -2.0943),
    ("Auckland", -36.8485, 174.7633),
    ("Bath", 51.3811, -2.3590),
    ("Belfast", 54.5973, -5.9301),
    ("Berlin", 52.5200, 13.4050),
    ("Birmingham", 52.4862, -1.8904),
    ("Bristol", 51.4545, -2.5879),
    ("Cambridge", 52.2053, 0.1218),
    ("Cardiff", 51.4816, -3.1791),
    ("Chicago", 41.8781, -87.6298),
    ("Coventry", 52.4068, -1.5197),
    ("Dubai", 25.2048, 55.2708),
    ("Edinburgh", 55.9533, -3.1883),
    ("Exeter", 50.7184, -3.5339),
    ("Glasgow", 55.8642, -4.2518),
    ("Hangzhou", 30.2741, 120.1551),
    ("Hong Kong", 22.3193, 114.1694),
    ("Leeds", 53.8008, -1.5491),
    ("Leicester", 52.6369, -1.1398),
    ("Liverpool", 53.4084, -2.9916),
    ("London", 51.5074, -0.1278),
    ("Los Angeles", 34.0522, -118.2437),
    ("Manchester", 53.4808, -2.2426),
    ("Moscow", 55.7558, 37.6173),
    ("Nanjing", 32.0603, 118.7969),
    ("New York", 40.7128, -74.0060),
    ("Newcastle", 54.9783, -1.6178),
    ("Ningbo", 29.8683, 121.5440),
    ("Norwich", 52.6309, 1.2974),
    ("Nottingham", 52.9548, -1.1581),
    ("Oxford", 51.7520, -1.2577),
    ("Paris", 48.8566, 2.3522),
    ("Plymouth", 50.3755, -4.1427),
    ("Portsmouth", 50.8198, -1.0880),
    ("Seoul", 37.5665, 126.9780),
    ("Shanghai", 31.2304, 121.4737),
    ("Sheffield", 53.3811, -1.4701),
    ("Singapore", 1.3521, 103.8198),
    ("Southampton", 50.9097, -1.4044),
    ("Suzhou", 31.2989, 120.5853),
    ("Sydney", -33.8688, 151.2093),
    ("Tokyo", 35.6762, 139.6503),
    ("Toronto", 43.6532, -79.3832),
    ("York", 53.9600, -1.0873),
]

DEFAULT_CONFIG = {
    "clock_top": "Europe/London",
    "clock_bottom": "Asia/Shanghai",
    "fx_top_from": "USD",
    "fx_top_to": "CNY",
    "fx_bottom_from": "GBP",
    "fx_bottom_to": "CNY",
    "data_cap_mb": None,
    "weather_city": "London",
    "clock_style": "digital",
}

_fonts = {}


def font(name, size):
    key = (name, size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(f"{FONT_DIR}/{name}.ttf", size)
    return _fonts[key]


def run_checked(cmd, timeout=8):
    """(ok, stdout). ok is False when the command could not run at all
    (binary missing, timeout, ...) or exited non-zero -- which is a
    different thing from a command that ran fine and printed nothing.
    run() collapses both to "" for the many callers that genuinely do not
    care; anything that would otherwise read a failure as a meaningful
    empty value must use this instead."""
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True)
        return out.returncode == 0, out.stdout.strip()
    except Exception:
        return False, ""


def run(cmd, timeout=8):
    return run_checked(cmd, timeout)[1]


def ubus_call(obj, method, params=None):
    args = ["ubus", "call", obj, method]
    if params:
        args.append(json.dumps(params))
    out = run(args)
    try:
        return json.loads(out)
    except Exception:
        return {}


def uci_get(key, default=""):
    """`uci -q get` exits non-zero both for an absent key and for uci
    itself failing, so those two stay indistinguishable here -- callers
    that must tell them apart (get_wifi_radio_state) probe the section as
    well. `default` exists so a caller can at least choose which way an
    unreadable value falls."""
    ok, out = run_checked(["uci", "-q", "get", key])
    return out if ok else default


def uci_set(key, val):
    run(["uci", "set", f"{key}={val}"])
    run(["uci", "commit", key.split(".")[0]])


BACKLIGHT_PATH = "/sys/class/backlight/soc:backlight/brightness"

# button_watch.py drops this file instead of switching the UI out from
# under you: a 1-2s hold is easy to trigger by accident (it sits between
# "tap" and the hardware's own poweroff hold), and losing the whole
# dashboard with no warning or undo is a harsh outcome for a misread
# gesture. When the dashboard is running it now gets to ask first. Going
# the other way -- stock UI back to dashboard -- still switches directly,
# since there is nothing of ours on screen to ask with.
SWITCH_REQUEST_FILE = "/tmp/dashboard_ui_switch_request"


_asleep_cache = {"ts": 0.0, "val": False}
_ASLEEP_TTL = 0.4


def is_screen_asleep():
    """Cached for _ASLEEP_TTL: mode_live polls this every loop iteration
    (~80x/s), and re-opening a sysfs file that often is pure overhead on
    this SoC. 0.4s is far below any human-perceptible wake latency."""
    now = time.time()
    if now - _asleep_cache["ts"] < _ASLEEP_TTL:
        return _asleep_cache["val"]
    try:
        with open(BACKLIGHT_PATH) as f:
            val = f.read().strip() == "0"
    except Exception:
        val = False
    _asleep_cache["ts"] = now
    _asleep_cache["val"] = val
    return val


# ---------- config ----------

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    """Atomic: write a sibling temp file, fsync, then rename over the real
    one. A plain write_text can leave a half-written file if the router
    loses power mid-write -- and load_config silently falls back to
    DEFAULT_CONFIG on a parse error, so that would wipe every setting with
    no indication why. There is a Shutdown button in this very UI, so this
    is not a theoretical window."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(cfg, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_FILE)


def load_game_scores():
    if GAME_SCORES_FILE.exists():
        try:
            return json.loads(GAME_SCORES_FILE.read_text())
        except Exception:
            pass
    return {}


def save_game_scores(scores):
    """Same atomic write pattern as save_config -- a half-written high
    score file would silently reset every game's best score to zero."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = GAME_SCORES_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(scores, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, GAME_SCORES_FILE)


def city_name(tz_id):
    for tz, name in CITIES:
        if tz == tz_id:
            return name
    return tz_id.split("/")[-1].replace("_", " ")


def cap_label(v):
    if v is None:
        return "No Limit"
    if v >= 1024:
        return f"{v / 1024:.0f} GB"
    return f"{v:.0f} MB"


# ---------- background refresh ----------

class Refresher:
    """Runs the periodic data pulls on one background thread and publishes
    the results, so the render loop only ever reads already-fetched
    values.

    Every one of these jobs used to be called straight from mode_live's
    idle branch. fetch_fx alone blocks for up to curl's --max-time (8s),
    and the weather tick does two of those back to back -- during which
    the loop drew nothing and read no touch events, i.e. the screen was
    genuinely frozen for up to ~16s at a time. The author had already
    diagnosed exactly this class of bug twice (see get_fx_history_cached
    and the repeater-scan thread) and moved those off the UI path; these
    were the ones left behind.

    Jobs are plain callables run in registration order; a job that raises
    keeps its previous value rather than taking the thread down."""

    def __init__(self):
        self.lock = threading.Lock()
        self._values = {}
        self._jobs = []          # [name, fn, interval, next_at]
        self._wake = threading.Event()

    def add(self, name, fn, interval, initial=None, run_now=True):
        with self.lock:
            self._values[name] = initial
        self._jobs.append([name, fn, interval, 0.0 if run_now else time.time() + interval])

    def get(self, name, default=None):
        with self.lock:
            val = self._values.get(name)
        return default if val is None else val

    def request(self, name):
        """Ask for one job to run as soon as the worker next wakes."""
        for job in self._jobs:
            if job[0] == name:
                job[3] = 0.0
        self._wake.set()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not _stop:
            now = time.time()
            sleep_for = 5.0
            for job in self._jobs:
                name, fn, interval, next_at = job
                if now >= next_at:
                    try:
                        value = fn()
                    except Exception:
                        value = None
                    job[3] = time.time() + interval
                    if value is not None:
                        with self.lock:
                            self._values[name] = value
                else:
                    sleep_for = min(sleep_for, next_at - now)
            self._wake.wait(max(0.05, sleep_for))
            self._wake.clear()


# ---------- data sources ----------

def fetch_fx(force=False):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cached = None
    if FX_CACHE.exists():
        try:
            cached = json.loads(FX_CACHE.read_text())
        except Exception:
            cached = None
    stale = force or cached is None or "rates" not in cached or (time.time() - cached.get("ts", 0)) > 6 * 3600
    if stale:
        raw = run(["curl", "-s", "--max-time", "6" if force else "8", "https://open.er-api.com/v6/latest/USD"])
        try:
            data = json.loads(raw)
            cached = {"ts": time.time(), "rates": data["rates"]}
            FX_CACHE.write_text(json.dumps(cached))
        except Exception:
            pass
    return cached


def _rate_vs_usd(code, rates):
    """fx["rates"] is base=USD. The API may or may not include "USD": 1.0
    itself in that dict -- treat USD as 1.0 regardless."""
    if code == "USD":
        return 1.0
    return rates.get(code)


def rate_between(from_code, to_code, fx):
    if not fx:
        return None
    if from_code == to_code:
        return 1.0
    rates = fx.get("rates", {})
    rf = _rate_vs_usd(from_code, rates)
    rt = _rate_vs_usd(to_code, rates)
    if rf is None or rt is None:
        return None
    return rt / rf


FX_RANGES = ["week", "month", "year"]
_FX_RANGE_DAYS = {"week": 7, "month": 30, "year": 365}


def fetch_fx_history(from_code, to_code, rng):
    """Daily from_code->to_code history for the last week/month/year, via
    Frankfurter (ECB reference rates, free, no key). Cached per
    (from,to,range) for 12h -- this is historical data, it doesn't need to
    be fresher than that."""
    if from_code == to_code:
        days = _FX_RANGE_DAYS[rng]
        end = datetime.utcnow().date()
        return [((end - timedelta(days=i)).isoformat(), 1.0) for i in range(days, -1, -1)]

    cache_file = STATE_DIR / f"fx_hist_{from_code}_{to_code}_{rng}.json"
    cached = None
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
        except Exception:
            cached = None
    if cached and time.time() - cached.get("ts", 0) < 12 * 3600:
        return cached["points"]

    days = _FX_RANGE_DAYS[rng]
    end = datetime.utcnow().date()
    start = end - timedelta(days=days)
    url = f"https://api.frankfurter.app/{start}..{end}?from={from_code}&to={to_code}"
    raw = run(["curl", "-sL", "--max-time", "6", url])
    try:
        data = json.loads(raw)
        rates = data.get("rates", {})
        points = [(d, v[to_code]) for d, v in sorted(rates.items()) if to_code in v]
        if points:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"ts": time.time(), "points": points}))
            return points
    except Exception:
        pass
    return cached["points"] if cached else []


_fx_hist_cache = {}  # (from, to, rng) -> {"points": [...] or None, "fetching": bool, "failed_at": float}
_FX_HIST_RETRY_AFTER = 120.0


def get_fx_history_cached(from_code, to_code, rng):
    """Non-blocking wrapper around fetch_fx_history. panel_fx() renders
    from the touch-handling loop (both for normal redraws and for the
    neighbor-panel pre-render at drag-start), so it can never block on
    fetch_fx_history's network call -- a cache miss there was measured to
    freeze the whole UI for up to 6s (curl's --max-time), which got much
    more likely once Currency stopped being fixed-target-CNY and gained a
    10x10 from/to combination space. Same reasoning as the repeater-scan
    background thread: don't call unmeasured/slow I/O synchronously from
    the touch path. Returns already-available points immediately (empty
    list if nothing cached yet), kicking off a background fetch instead of
    blocking."""
    if from_code == to_code:
        return fetch_fx_history(from_code, to_code, rng)  # synthetic flat line, no network

    key = (from_code, to_code, rng)
    entry = _fx_hist_cache.get(key)
    if entry is None:
        cache_file = STATE_DIR / f"fx_hist_{from_code}_{to_code}_{rng}.json"
        cached_points = None
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text())
                if time.time() - cached.get("ts", 0) < 12 * 3600:
                    cached_points = cached["points"]
            except Exception:
                pass
        entry = {"points": cached_points, "fetching": False, "failed_at": 0.0}
        _fx_hist_cache[key] = entry

    # A failed fetch used to store [] here, which is not None -- so the
    # "never fetched yet" guard below stopped firing and the chart stayed
    # permanently blank for the rest of the process's life after a single
    # offline moment. Keep points None on failure and retry on a backoff
    # instead, so the chart heals itself once the network is back.
    now = time.time()
    needs_fetch = (entry["points"] is None and not entry["fetching"]
                   and now - entry.get("failed_at", 0.0) >= _FX_HIST_RETRY_AFTER)
    if needs_fetch:
        entry["fetching"] = True

        def worker():
            try:
                points = fetch_fx_history(from_code, to_code, rng)
                if points:
                    entry["points"] = points
                    entry["failed_at"] = 0.0
                else:
                    entry["failed_at"] = time.time()
            finally:
                entry["fetching"] = False

        threading.Thread(target=worker, daemon=True).start()

    return entry["points"] or []


# WMO weather codes (Open-Meteo) -> (short label, icon key)
_WMO_MAP = {
    0: ("Clear", "sun"), 1: ("Mostly clear", "sun"), 2: ("Partly cloudy", "cloud_sun"),
    3: ("Overcast", "cloud"),
    45: ("Fog", "fog"), 48: ("Fog", "fog"),
    51: ("Light drizzle", "rain"), 53: ("Drizzle", "rain"), 55: ("Heavy drizzle", "rain"),
    56: ("Freezing drizzle", "rain"), 57: ("Freezing drizzle", "rain"),
    61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"), 67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Rain showers", "rain"), 81: ("Rain showers", "rain"), 82: ("Violent showers", "rain"),
    85: ("Snow showers", "snow"), 86: ("Snow showers", "snow"),
    95: ("Thunderstorm", "storm"), 96: ("Thunderstorm", "storm"), 99: ("Thunderstorm", "storm"),
}


def wmo_info(code):
    return _WMO_MAP.get(code, ("Unknown", "cloud"))


def fetch_weather(city_name, force=False):
    entry = next((c for c in WEATHER_CITIES if c[0] == city_name), WEATHER_CITIES[0])
    _, lat, lon = entry
    cache_file = STATE_DIR / f"weather_{city_name.replace(' ', '_')}.json"
    cached = None
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
        except Exception:
            cached = None
    cache_fresh = (cached and time.time() - cached.get("ts", 0) < 2 * 3600
                   and cached.get("days") and "uv_max" in cached["days"][0])
    if not force and cache_fresh:
        return cached["days"]

    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
           "apparent_temperature_max,apparent_temperature_min,wind_speed_10m_max,uv_index_max"
           "&timezone=auto&forecast_days=3")
    raw = run(["curl", "-s", "--max-time", "6" if force else "8", url])
    try:
        data = json.loads(raw)
        daily = data["daily"]
        n = len(daily["time"])
        days = []
        for i in range(n):
            days.append({
                "date": daily["time"][i],
                "code": daily["weather_code"][i],
                "tmax": daily["temperature_2m_max"][i],
                "tmin": daily["temperature_2m_min"][i],
                "precip": daily.get("precipitation_probability_max", [None] * n)[i],
                "feels_max": daily.get("apparent_temperature_max", [None] * n)[i],
                "feels_min": daily.get("apparent_temperature_min", [None] * n)[i],
                "wind_max": daily.get("wind_speed_10m_max", [None] * n)[i],
                "uv_max": daily.get("uv_index_max", [None] * n)[i],
            })
        if days:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"ts": time.time(), "days": days}))
            return days
    except Exception:
        pass
    return cached["days"] if cached else []


def fetch_air_quality(city_name, force=False):
    """Open-Meteo's air-quality API only offers hourly data (confirmed live
    -- a daily=us_aqi_max request errors out), so daily max PM2.5/PM10/US
    AQI are aggregated here from the hourly series ourselves. Returns []
    per day when a value is missing (e.g. sparse coverage in some regions)
    rather than guessing -- callers should render that as "no data yet"."""
    entry = next((c for c in WEATHER_CITIES if c[0] == city_name), WEATHER_CITIES[0])
    _, lat, lon = entry
    cache_file = STATE_DIR / f"aq_{city_name.replace(' ', '_')}.json"
    cached = None
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
        except Exception:
            cached = None
    if not force and cached and time.time() - cached.get("ts", 0) < 2 * 3600:
        return cached["days"]

    url = (f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}"
           "&hourly=us_aqi,pm2_5,pm10&timezone=auto&forecast_days=3")
    raw = run(["curl", "-s", "--max-time", "6" if force else "8", url])
    try:
        data = json.loads(raw)
        hourly = data["hourly"]
        times = hourly["time"]
        aqis = hourly.get("us_aqi", [None] * len(times))
        pm25s = hourly.get("pm2_5", [None] * len(times))
        pm10s = hourly.get("pm10", [None] * len(times))
        by_date = {}
        for i, ts in enumerate(times):
            date = ts.split("T")[0]
            slot = by_date.setdefault(date, {"date": date, "aqi": None, "pm2_5": None, "pm10": None})
            for key, series in (("aqi", aqis), ("pm2_5", pm25s), ("pm10", pm10s)):
                v = series[i]
                if v is not None and (slot[key] is None or v > slot[key]):
                    slot[key] = v
        days = [by_date[d] for d in sorted(by_date)]
        if days:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"ts": time.time(), "days": days}))
            return days
    except Exception:
        pass
    return cached["days"] if cached else []


def uv_category(uv):
    if uv is None:
        return None
    if uv < 3:
        return "Low"
    if uv < 6:
        return "Moderate"
    if uv < 8:
        return "High"
    if uv < 11:
        return "Very High"
    return "Extreme"


def aqi_category(aqi):
    if aqi is None:
        return None
    if aqi <= 50:
        return "Good"
    if aqi <= 100:
        return "Moderate"
    if aqi <= 150:
        return "Unhealthy (SG)"
    if aqi <= 200:
        return "Unhealthy"
    if aqi <= 300:
        return "Very Unhealthy"
    return "Hazardous"


# ---------- SMS ----------

# smstools3 (`smsd`, confirmed live and actually running against this
# modem via a local AT-over-TCP bridge on 127.0.0.1:44383) drops each
# received message as a plain-text file here -- standard "SMS Server
# Tools 3" spool format (`From:`/`Sent:` header lines, blank line, then
# the body). Read-only: this only lists messages, it never moves or
# deletes spool files, so it can't interfere with any other consumer
# (e.g. GL.iNet's own SMS forwarding) that might also be watching these
# directories.
SMS_SPOOL_DIRS = ["/etc/spool/sms/incoming", "/etc/spool/sms/checked"]


def _parse_sms_file(path):
    """smstools3's header is always plain ASCII, but the body's actual byte
    encoding depends on the `Alphabet` header -- confirmed live against a
    real received message: `Alphabet: UCS2` bodies are raw big-endian
    UTF-16 bytes (not UTF-8 text, not hex-encoded), while plain-ASCII
    messages come through as `Alphabet: UTF-8`. Blindly UTF-8-decoding a
    UCS2 body (the original approach) corrupts every non-ASCII message --
    confirmed by a real device test that showed Chinese text as mojibake."""
    try:
        raw = path.read_bytes()
    except Exception:
        return None
    # Accept CRLF as well as LF as the header/body separator -- a spool
    # file written with CRLF contains no "\n\n" at all, so the old check
    # put the whole file in the header and left every body empty.
    sep, sep_len = raw.find(b"\r\n\r\n"), 4
    if sep == -1:
        sep, sep_len = raw.find(b"\n\n"), 2
    header_bytes, body_bytes = (raw, b"") if sep == -1 else (raw[:sep], raw[sep + sep_len:])
    header = header_bytes.decode("ascii", errors="replace")
    fields = {}
    for line in header.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip().lower()] = v.strip()
    alphabet = fields.get("alphabet", "").upper()
    if alphabet in ("UCS2", "UCS-2", "UTF-16", "UTF16"):
        body = body_bytes.decode("utf-16-be", errors="replace")
    else:
        body = body_bytes.decode("utf-8", errors="replace")
    sent_raw = fields.get("sent", "")
    try:
        sent = datetime.strptime(sent_raw, "%y-%m-%d %H:%M:%S")
    except Exception:
        sent = None
    return {
        "from": fields.get("from", "Unknown"),
        "sent": sent,
        "sent_raw": sent_raw,
        "body": body.strip(),
    }


def get_sms_messages():
    messages = []
    for d in SMS_SPOOL_DIRS:
        p = Path(d)
        if not p.is_dir():
            continue
        for f in p.iterdir():
            if not f.is_file():
                continue
            msg = _parse_sms_file(f)
            if msg:
                messages.append(msg)
    messages.sort(key=lambda m: m["sent"] or datetime.min, reverse=True)
    return messages


# ---------- WireGuard ----------

# GL.iNet stores each added WireGuard peer as its own `wireguard.peer_NNNN`
# UCI section (name, keys, endpoint, allowed_ips -- all of it), separate
# from the actual `network` interface that carries traffic. Confirmed live
# by reading /lib/netifd/proto/wgclient.sh and the wireguard hotplug
# script: bringing a peer online just means creating a `network.wgclient`
# interface with `proto=wgclient` and `config=<peer section name>` --
# everything else (keys, endpoint, allowed_ips) is pulled live from that
# wireguard.<config> section by the proto script itself at ifup time, so
# nothing secret needs to be duplicated into `network`.
WG_IFACE = "wgclient"


def get_wireguard_peers():
    out = run(["uci", "show", "wireguard"])
    peers = []
    for line in out.splitlines():
        if line.startswith("wireguard.") and line.endswith("=peers"):
            section = line[len("wireguard."):-len("=peers")]
            name = uci_get(f"wireguard.{section}.name") or section
            peers.append({"id": section, "name": name})
    return peers


def get_wireguard_active():
    """None if the wgclient interface doesn't exist or isn't up, else the
    wireguard.peer_NNNN id it's currently configured to carry."""
    config = uci_get(f"network.{WG_IFACE}.config")
    if not config:
        return None
    status = ubus_call(f"network.interface.{WG_IFACE}", "status")
    return config if status.get("up") else None


def set_wireguard_enabled(peer_id, enabled):
    if enabled:
        uci_set(f"network.{WG_IFACE}", "interface")
        uci_set(f"network.{WG_IFACE}.proto", "wgclient")
        uci_set(f"network.{WG_IFACE}.config", peer_id)
        subprocess.Popen(["ifup", WG_IFACE])
    else:
        subprocess.Popen(["ifdown", WG_IFACE])


_DEMO_SMS_MESSAGES = [
    {"from": "+447700900123", "sent": datetime(2026, 7, 26, 14, 30, 0),
     "sent_raw": "26-07-26 14:30:00",
     "body": "Your verification code is 481920. It expires in 10 minutes."},
    {"from": "+8613800138000", "sent": datetime(2026, 7, 25, 9, 15, 22),
     "sent_raw": "26-07-25 09:15:22",
     "body": "您好，您的验证码是123456，10分钟内有效，请勿泄露给他人。"},
]


def get_sim_status(cfg):
    sims = ubus_call("cellular.sim", "info", {"bus": "cpu"}).get("sims", [])
    modem = ubus_call("cellular.modem", "status", {"bus": "cpu"})
    slot = str(modem.get("current_sim_slot", "1"))
    active = next((s for s in sims if str(s.get("slot")) == slot), None)
    traffic_mb = None
    if active:
        net = ubus_call("cellular.network", "status", {"bus": "cpu", "slot": int(slot)})
        for n in net.get("networks", []):
            if str(n.get("slot")) == slot:
                try:
                    traffic_mb = int(n["traffic_total"]) / 1024 / 1024
                except Exception:
                    pass
    country = None
    phone = None
    iccid = None
    roaming = False
    if active:
        country = MCC_COUNTRY.get(active.get("mcc", ""), f"MCC {active.get('mcc', '?')}")
        phone = active.get("phone_number") or ""
        iccid = active.get("iccid")
        if iccid:
            roaming = bool(ubus_call("cellular.sim", "get_config", {"iccid": iccid}).get("roaming", False))

    # "sim_choice" reflects the UI's 2-way pick. SIM2 was removed from the
    # picker: it and eSIM both live on slot 2 on this hardware and both just
    # reorder slot priority to prefer slot 2, so there was no way to
    # actually select one over the other -- SIM2 was dead weight in the UI.
    #
    # This MUST be derived from `slot` (current_sim_slot -- what the modem
    # is actually running right now), not from get_slot_priority_config's
    # slot_priority list. Confirmed live on this device: priority is only
    # a *requested* preference and can read [1, 2] while the modem is
    # already sitting on slot 2 (its own backhaul logic reordering things,
    # or a switch still settling) -- the two genuinely diverge, and every
    # OTHER field on this screen (country, phone, iccid) already reads off
    # `slot`. Reading the toggle off priority instead showed "SIM1"
    # selected while the flag, phone number and data cap right next to it
    # were all slot 2's (eSIM) -- a self-contradicting screen.
    sim_choice = "sim1" if slot == "1" else "esim"

    data_iface = ubus_call("network.interface.modem_cpu", "status")
    data_up = bool(data_iface.get("up"))
    # "attached" (network registration -- SMS/calls) is a different thing
    # from "data_up" (a live PDP data session on modem_cpu): the modem can
    # be registered with the network without the data interface being up
    # at all, which is exactly the "receives SMS but not using cellular
    # for data" mode this is meant to represent. No direct ubus getter for
    # registration state, so it's inferred from whether cell_info is
    # currently populated at all (see _get_active_cell_info).
    attached = bool(_get_active_cell_info().get("mode"))

    return {
        "slot": slot, "country": country, "phone": phone, "traffic_mb": traffic_mb,
        "cap_mb": cfg.get("data_cap_mb"), "sim_choice": sim_choice, "data_up": data_up,
        "iccid": iccid, "attached": attached, "roaming": roaming,
    }


def set_sim_choice(choice):
    """choice: 'sim1' | 'esim'. eSIM lives on slot 2 on this hardware --
    same slot the removed 'sim2' option used to target."""
    target_slot = 1 if choice == "sim1" else 2
    other = 2 if target_slot == 1 else 1
    run(["ubus", "call", "cellular.modem", "set_slot_priority_config",
         json.dumps({"bus": "cpu", "slot_priority": [target_slot, other]})])


def set_cellular_data_enabled(enabled):
    subprocess.Popen(["ifup" if enabled else "ifdown", "modem_cpu"])


def set_network_attach_enabled(enabled):
    """Toggle the modem's network registration (SMS/calls) independent of
    the cellular DATA session (data_up / set_cellular_data_enabled) --
    airplane-mode style: enabled=True registers with the network,
    enabled=False fully detaches (no signal, no SMS/calls). This is the
    "attach without necessarily using cellular for data" mode -- can stay
    registered while a WiFi repeater/ethernet handles actual internet
    traffic. NOTE: cellular.modem has no paired getter for this, so
    get_sim_status infers current "attached" state from whether cell_info
    is populated at all, rather than tracking a separate flag."""
    run(["ubus", "call", "cellular.modem", "set_airplane_mode",
         json.dumps({"enable": not enabled})])


def set_roaming_enabled(iccid, enabled):
    """Data roaming permission for a given SIM, via cellular.sim's
    get_config/set_config -- a per-iccid config table (auth/apn/roaming/
    etc), not a standalone flag, so this reads the current table and
    writes it back with only 'roaming' changed rather than clobbering the
    rest (apn, pincode, auth...) with a partial object."""
    if not iccid:
        return
    cur = ubus_call("cellular.sim", "get_config", {"iccid": iccid})
    if not cur:
        return
    cur["roaming"] = enabled
    run(["ubus", "call", "cellular.sim", "set_config",
         json.dumps({"iccid": iccid, "data": cur})])


# ---------- repeater (station/WiFi-extender mode) ----------

def get_repeater_status():
    st = ubus_call("repeater", "status")
    if not st or not st.get("running"):
        return {"connected": False, "ssid": None, "signal": None, "ip": None}
    connected = st.get("state_s") == "connected"
    ip = (st.get("ipv4") or {}).get("ip", "").split("/")[0] or None
    return {
        "connected": connected,
        "ssid": st.get("ssid"),
        "signal": st.get("signal"),
        "ip": ip,
        "band": st.get("band") if connected else None,
    }


def repeater_scan():
    result = ubus_call("repeater", "scan", {"cached": True})
    survey = result.get("survey", []) if result else []
    best = {}
    for ap in survey:
        ssid = ap.get("ssid")
        if not ssid:
            continue
        sig = ap.get("signal", -999)
        if ssid not in best or sig > best[ssid]["signal"]:
            best[ssid] = {
                "ssid": ssid,
                "signal": sig,
                "bssid": ap.get("bssid"),
                "band": ap.get("band"),
                "open": not ap.get("caps", {}).get("PRIVACY", True),
            }
    return sorted(best.values(), key=lambda a: -a["signal"])


def repeater_connect(ssid, bssid, key):
    params = {"ssid": ssid, "bssid": bssid, "remember": True}
    if key:
        params["key"] = key
    subprocess.Popen(["ubus", "call", "repeater", "connect", json.dumps(params)])


def get_remembered_repeater_keys():
    """SSID -> key for every network repeater_connect's remember=True has
    ever saved, straight from /etc/config/repeater's anonymous @network[]
    sections (this is where the ubus 'repeater' service itself persists
    them -- not a separate store of our own). Used so re-selecting a
    previously-connected network can reconnect immediately instead of
    demanding the password again."""
    out = run(["uci", "show", "repeater"])
    ssid_by_idx, key_by_idx = {}, {}
    for line in out.splitlines():
        if ".ssid=" not in line and ".key=" not in line:
            continue
        try:
            idx = line.split("[", 1)[1].split("]", 1)[0]
            value = line.split("=", 1)[1].strip().strip("'")
        except IndexError:
            continue
        if ".ssid=" in line:
            ssid_by_idx[idx] = value
        else:
            key_by_idx[idx] = value
    return {ssid_by_idx[i]: key_by_idx[i] for i in ssid_by_idx if i in key_by_idx}


def repeater_disconnect():
    subprocess.Popen(["ubus", "call", "repeater", "disconnect"])


# ---------- device settings ----------

def get_wifi_radio_state(iface):
    """iface: the AP-mode wifi-iface section name ('wifi2g'/'wifi5g' on
    this device -- the actual "GL-E5800" SSID), NOT the underlying radio
    device name ('wifi0'/'wifi1'). The radio device's own 'disabled' flag
    only gates whether the physical radio hardware is powered on at all
    (needed regardless, e.g. for this router's repeater-client uplink) --
    completely separate from whether the AP interface actually broadcasts
    its SSID. Confirmed live: wifi0/wifi1 (radio) read disabled=0 while
    wifi2g/wifi5g (the real AP) read disabled=1 -- toggling the
    radio-level flag never touched the setting that actually determines
    whether the WiFi network is visible, which is why the toggle could
    show "on" while the network was genuinely off the whole time."""
    ok, out = run_checked(["uci", "-q", "get", f"wireless.{iface}.disabled"])
    if ok:
        return out != "1"
    # Non-zero also means "key absent", which on OpenWrt is the normal way
    # of saying enabled -- so only call it unknown if the section itself
    # cannot be read either. Returning None rather than True keeps a broken
    # uci from being displayed as a confident "WiFi is on".
    if run_checked(["uci", "-q", "show", f"wireless.{iface}"])[0]:
        return True
    return None


_wifi_reload_state = {"running": False, "pending": False}
_wifi_reload_lock = threading.Lock()


def _wifi_reload_worker():
    """The try/finally matters: without it, any exception out of
    subprocess.run kills this thread while `running` stays True, and
    request_wifi_reload then refuses to ever start another worker -- every
    later WiFi toggle would write UCI and silently never reload, which is
    the exact desync this coalescing was added to prevent."""
    try:
        while True:
            with _wifi_reload_lock:
                _wifi_reload_state["pending"] = False
            try:
                subprocess.run(["/sbin/wifi", "reload"], timeout=60)
            except Exception:
                pass
            with _wifi_reload_lock:
                if not _wifi_reload_state["pending"]:
                    return
    finally:
        with _wifi_reload_lock:
            _wifi_reload_state["running"] = False


def request_wifi_reload():
    """`/sbin/wifi reload` serializes on its own file lock and takes ~8-10s
    per call -- firing one per toggle tap (the old behavior) let calls pile
    up faster than they drained, and a backlog of them was found stuck
    mid-queue after a round of testing, leaving the AP UCI state and the
    actual broadcasting hostapd state out of sync. This coalesces any
    reloads requested while one is already in flight into a single trailing
    reload instead of stacking a new subprocess per request."""
    with _wifi_reload_lock:
        if _wifi_reload_state["running"]:
            _wifi_reload_state["pending"] = True
            return
        _wifi_reload_state["running"] = True
    threading.Thread(target=_wifi_reload_worker, daemon=True).start()


def set_wifi_radio_state(iface, enabled):
    uci_set(f"wireless.{iface}.disabled", "0" if enabled else "1")
    request_wifi_reload()


def get_wifi_band_state():
    """5G and 6G share a single antenna path on this hardware and can only
    have one active at a time -- returns "5g"/"6g" for whichever AP
    interface is currently enabled, or "off" if neither is."""
    if get_wifi_radio_state("wifi5g"):
        return "5g"
    if get_wifi_radio_state("wifi6g"):
        return "6g"
    return "off"


def set_wifi_band_state(band):
    uci_set("wireless.wifi5g.disabled", "0" if band == "5g" else "1")
    uci_set("wireless.wifi6g.disabled", "0" if band == "6g" else "1")
    request_wifi_reload()


def get_wifi56_conflict_idx(rep):
    """Index into ["5G", "Off", "6G"] that must stay disabled because the
    repeater's upstream AP is already using the shared 5G/6G antenna path
    (2.4G/5G upstream conflicts with local 6G; 6G upstream conflicts with
    local 5G). None if there's no repeater conflict (not connected)."""
    if not rep.get("connected"):
        return None
    band = (rep.get("band") or "").lower()
    if band.startswith("6"):
        return 0
    if band:
        return 2
    return None


_lan_ip_cache = {"ts": 0.0, "val": None}


def get_system_info():
    """Reads /proc/uptime directly instead of spawning `cat`, and caches
    the LAN IP for a minute: mode_live calls this every 2s purely for the
    uptime line, and two process spawns every 2s is real load on this
    SoC for a number that changes once a minute."""
    uptime_min = 0
    try:
        with open("/proc/uptime") as f:
            uptime_min = int(float(f.read().split()[0]) / 60)
    except Exception:
        pass
    now = time.time()
    if _lan_ip_cache["val"] is None or now - _lan_ip_cache["ts"] > 60:
        _lan_ip_cache["val"] = uci_get("network.lan.ipaddr") or "192.168.8.1"
        _lan_ip_cache["ts"] = now
    return {"uptime_min": uptime_min, "lan_ip": _lan_ip_cache["val"]}


def reboot_router():
    subprocess.Popen(["/sbin/reboot"])


def shutdown_router():
    subprocess.Popen(["/sbin/poweroff"])


def switch_to_stock_ui():
    # Non-blocking AND detached: toggle.sh off stops citydash (this very
    # process), so it has to outlive us. Popen alone leaves it in our
    # process group, so procd's stop can take it down partway through --
    # after gl_screen has been stopped but before it is started again,
    # which leaves the physical screen dark with neither UI running.
    # start_new_session puts it in its own session so that can't happen.
    try:
        subprocess.Popen(["/root/dashboard/toggle.sh", "off"], start_new_session=True)
    except TypeError:      # very old Python without start_new_session
        subprocess.Popen(["setsid", "/root/dashboard/toggle.sh", "off"])


# ---------- system monitor (bandwidth + CPU/RAM/temp) ----------

def get_wan_iface():
    """Pick the interface holding the lowest-metric default route. This
    device dual-WANs (repeater WiFi uplink, usually wlan4, vs. the cellular
    modem rmnet_data0 as failover) so the active WAN interface isn't fixed --
    br-lan is just the local LAN bridge and stays near-zero unless another
    device is actively using this router's own AP, which made the old
    hardcoded br-lan reading look permanently decorative."""
    best_iface, best_metric = None, None
    try:
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                parts = line.split()
                if len(parts) < 7 or parts[1] != "00000000":
                    continue
                metric = int(parts[6])
                if best_metric is None or metric < best_metric:
                    best_iface, best_metric = parts[0], metric
    except Exception:
        pass
    return best_iface


def has_competing_wan():
    """True if a non-cellular WAN (repeater WiFi or ethernet) currently
    holds the default route. Confirmed live: this router's own backhaul
    manager (QCMAP/kmwan) will silently revert a manual cellular connect
    while a healthier WAN is already active, so the connect toggle's
    optimistic-then-verify animation is only needed in that case -- with
    no competing WAN, there's nothing to revert it, so the tap can just
    be trusted and left to search/connect in the background."""
    iface = get_wan_iface()
    return bool(iface) and not ("rmnet" in iface or "modem" in iface)


def _get_active_cell_info():
    """cell_info dict (mode/rsrp/etc) for the currently-active SIM slot, or
    {} if unavailable. Shared by get_wan_conn_type/get_cell_signal so they
    don't each make their own redundant ubus round trip."""
    modem = ubus_call("cellular.modem", "status", {"bus": "cpu"})
    try:
        slot = int(modem.get("current_sim_slot", 1))
    except (TypeError, ValueError):
        slot = 1
    net = ubus_call("cellular.network", "info", {"bus": "cpu", "slot": slot})
    for n in net.get("networks", []):
        cell = n.get("cell_info") or {}
        if cell.get("mode"):
            return cell
    return {}


def get_wan_conn_type(cell_info=None):
    """Short label for the header's connection-status indicator: Repeater
    (WiFi client uplink), Ethernet, or 4G/5G (cellular, radio access tech
    read from cellular.network's cell_info.mode, e.g. "LTE FDD" -> 4G,
    anything with "NR" -> 5G). Does a couple of ubus calls, so -- same
    lesson as the fx-history and repeater-scan fixes -- this is refreshed
    periodically in mode_live's idle loop, never called from inside a
    panel's own render function."""
    iface = get_wan_iface()
    if not iface:
        return None
    if iface.startswith("wlan"):
        return "Repeater"
    if iface.startswith("eth"):
        return "Ethernet"
    if "rmnet" in iface or "modem" in iface:
        mode_u = (cell_info or _get_active_cell_info()).get("mode", "").upper()
        if "NR" in mode_u:
            return "5G"
        if mode_u:
            return "4G"
        return "Cellular"
    return None


def get_cell_signal(cell_info=None):
    """(bars 0-4, "4G"/"5G") for the active SIM's current cellular signal,
    derived from RSRP (dBm); None if not registered/no signal at all --
    matches ordinary phone status-bar behavior of hiding the cellular
    indicator entirely when there's nothing to show. Independent of
    whether cellular is actually the active WAN (get_wan_conn_type) --
    this reflects the modem's own registration/signal, the same way a
    phone shows signal bars regardless of whether you're on WiFi."""
    cell = cell_info if cell_info is not None else _get_active_cell_info()
    mode = cell.get("mode", "")
    if not mode:
        return None
    try:
        rsrp = int(cell.get("rsrp"))
    except (TypeError, ValueError):
        return None
    if rsrp >= -80:
        bars = 4
    elif rsrp >= -95:
        bars = 3
    elif rsrp >= -105:
        bars = 2
    elif rsrp >= -115:
        bars = 1
    else:
        bars = 0
    rat = "5G" if "NR" in mode.upper() else "4G"
    return bars, rat


def sample_bandwidth(prev):
    """prev: (iface, ts, rx_bytes, tx_bytes) or None. Returns (new_sample, down_mbps, up_mbps).
    Rates are None until there's a previous sample on the *same* interface to
    diff against -- if the active WAN interface changed (failover) between
    calls, this resets rather than diffing two different interfaces' counters."""
    iface = get_wan_iface()
    if iface is None:
        return prev, None, None
    try:
        with open(f"/sys/class/net/{iface}/statistics/rx_bytes") as f:
            rx = int(f.read().strip())
        with open(f"/sys/class/net/{iface}/statistics/tx_bytes") as f:
            tx = int(f.read().strip())
    except Exception:
        return prev, None, None
    now = time.time()
    if prev is None or prev[0] != iface:
        return (iface, now, rx, tx), None, None
    _, pts, prx, ptx = prev
    dt = now - pts
    if dt <= 0:
        return (iface, now, rx, tx), None, None
    down_mbps = max(0.0, (rx - prx) * 8 / dt / 1_000_000)
    up_mbps = max(0.0, (tx - ptx) * 8 / dt / 1_000_000)
    return (iface, now, rx, tx), down_mbps, up_mbps


def sample_cpu(prev):
    """prev: (idle, total) or None, from /proc/stat's aggregate 'cpu' line.
    Returns (new_sample, cpu_pct). cpu_pct is None until there's a previous
    sample (needs a delta, not just a point-in-time read)."""
    try:
        with open("/proc/stat") as f:
            fields = [int(x) for x in f.readline().split()[1:]]
        idle = fields[3] + fields[4]
        total = sum(fields)
    except Exception:
        return prev, None
    if prev is None:
        return (idle, total), None
    pidle, ptotal = prev
    dtotal = total - ptotal
    if dtotal <= 0:
        return (idle, total), None
    pct = max(0.0, min(100.0, 100 * (1 - (idle - pidle) / dtotal)))
    return (idle, total), pct


def get_ram_stats():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.strip().split()[0])
        total_kb = info.get("MemTotal", 0)
        avail_kb = info.get("MemAvailable", 0)
        if not total_kb:
            return None, None, None
        used_kb = total_kb - avail_kb
        return 100 * used_kb / total_kb, used_kb / 1024 / 1024, total_kb / 1024 / 1024
    except Exception:
        return None, None, None


_TEMP_ZONE_PATH = None
_TEMP_ZONE_PREFERRED = ["cpuss-0", "aoss-0", "sys-therm-1"]


def get_temp_c():
    """thermal_zone0 on this hardware ('sdr0') is an unpowered sensor that
    always reports the sentinel -273000 (absolute zero); pick a real sensor
    by name instead of assuming zone index 0, and cache the path found."""
    global _TEMP_ZONE_PATH
    if _TEMP_ZONE_PATH is None:
        base = "/sys/class/thermal"
        candidates = {}
        try:
            for name in os.listdir(base):
                if not name.startswith("thermal_zone"):
                    continue
                try:
                    with open(f"{base}/{name}/type") as f:
                        ztype = f.read().strip()
                    with open(f"{base}/{name}/temp") as f:
                        raw = int(f.read().strip())
                except Exception:
                    continue
                if -50000 < raw < 150000:
                    candidates[ztype] = f"{base}/{name}/temp"
        except Exception:
            pass
        for name in _TEMP_ZONE_PREFERRED:
            if name in candidates:
                _TEMP_ZONE_PATH = candidates[name]
                break
        else:
            _TEMP_ZONE_PATH = next(iter(candidates.values()), False)
    if not _TEMP_ZONE_PATH:
        return None
    try:
        with open(_TEMP_ZONE_PATH) as f:
            return int(f.read().strip()) / 1000
    except Exception:
        return None


def openclash_installed():
    return os.path.exists("/etc/init.d/openclash")


def get_openclash_status():
    installed = openclash_installed()
    enabled = installed and uci_get("openclash.config.enable") == "1"
    mode = (uci_get("openclash.config.proxy_mode") if installed else None) or "rule"
    return {"installed": installed, "enabled": enabled, "mode": mode}


def set_openclash_enabled(enabled):
    if not openclash_installed():
        return
    uci_set("openclash.config.enable", "1" if enabled else "0")
    subprocess.Popen(["/etc/init.d/openclash", "start" if enabled else "stop"])


def set_openclash_mode(mode):
    if not openclash_installed():
        return
    uci_set("openclash.config.proxy_mode", mode)
    if uci_get("openclash.config.enable") == "1":
        subprocess.Popen(["/etc/init.d/openclash", "restart"])


def update_openclash_subscription(wait=False):
    """Re-fetches every configured subscription and reloads if changed --
    the same script LuCI's own subscription page runs. With no argument,
    openclash.sh iterates all openclash.@config_subscribe[] sections
    (config_foreach sub_info_get "config_subscribe" "$1", "$1" here being
    empty = no name filter = all of them). This does a real network fetch
    + config validation + possible restart, so it's Popen'd non-blocking
    like reboot_router/switch_to_stock_ui -- never run synchronously from
    the touch-handling path."""
    if not openclash_installed():
        return False
    proc = subprocess.Popen(["/usr/share/openclash/openclash.sh"])
    if not wait:
        return True
    try:
        proc.wait(timeout=180)
    except Exception:
        return False
    return proc.returncode == 0


_COUNTRY_NAME_HINTS = [
    ("HONGKONG", "Hong Kong"), ("HONG KONG", "Hong Kong"), ("HK", "Hong Kong"),
    ("香港", "Hong Kong"),
    ("TAIWAN", "Taiwan"), ("TW", "Taiwan"), ("台湾", "Taiwan"),
    ("SINGAPORE", "Singapore"), ("SG", "Singapore"), ("新加坡", "Singapore"), ("狮城", "Singapore"),
    ("KOREA", "South Korea"), ("KR", "South Korea"), ("韩国", "South Korea"), ("韓國", "South Korea"),
    ("JAPAN", "Japan"), ("TOKYO", "Japan"), ("JP", "Japan"), ("日本", "Japan"),
    ("BRITAIN", "UK"), ("LONDON", "UK"), ("UK", "UK"), ("GBR", "UK"), ("英国", "UK"), ("英國", "UK"),
    ("GERMANY", "Germany"), ("DE", "Germany"), ("德国", "Germany"), ("德國", "Germany"),
    ("FRANCE", "France"), ("FR", "France"), ("法国", "France"), ("法國", "France"),
    ("CHINA", "China"), ("CN", "China"), ("中国", "China"), ("中國", "China"), ("大陆", "China"), ("大陸", "China"),
    ("CANADA", "Canada"), ("CA", "Canada"), ("加拿大", "Canada"),
    ("AUSTRALIA", "Australia"), ("AU", "Australia"), ("澳洲", "Australia"), ("澳大利亚", "Australia"),
    ("AMERICA", "USA"), ("UNITED STATES", "USA"), ("US", "USA"), ("USA", "USA"), ("美国", "USA"), ("美國", "USA"),
]


def guess_country_from_name(name):
    if not name:
        return None
    upper = name.upper()
    for key, country in _COUNTRY_NAME_HINTS:
        if key in upper:
            return country
    return None


def _mihomo_api():
    port = uci_get("openclash.config.cn_port") or "9090"
    password = uci_get("openclash.config.dashboard_password")
    headers = ["-H", f"Authorization: Bearer {password}"] if password else []
    return f"http://127.0.0.1:{port}", headers


def openclash_traffic_empty():
    """Single source of truth for the shape panel_openclash and
    panel_node_picker expect. mode_live seeds its first frame from this
    too -- hand-writing a second "empty" dict there is exactly how you end
    up with a KeyError on the very first render, before any refresh has
    landed."""
    return {"running": False, "up_mb": None, "down_mb": None, "node_name": None,
            "node_country": None, "nodes": [], "group": None}


def get_openclash_traffic_and_node():
    if not openclash_installed():
        return openclash_traffic_empty()
    base, headers = _mihomo_api()
    conn_raw = run(["curl", "-s", "--max-time", "2"] + headers + [f"{base}/connections"])
    try:
        conn = json.loads(conn_raw)
        up_mb = conn.get("uploadTotal", 0) / 1024 / 1024
        down_mb = conn.get("downloadTotal", 0) / 1024 / 1024
    except Exception:
        return openclash_traffic_empty()

    node_name, node_country, nodes, group = None, None, [], None
    proxies_raw = run(["curl", "-s", "--max-time", "2"] + headers + [f"{base}/proxies"])
    try:
        proxies = json.loads(proxies_raw).get("proxies", {})
        for name, info in proxies.items():
            if info.get("type") == "Selector":
                group = name
                node_name = info.get("now")
                nodes = info.get("all", [])
                break
    except Exception:
        pass
    if node_name:
        node_country = guess_country_from_name(node_name)
    return {"running": True, "up_mb": up_mb, "down_mb": down_mb, "node_name": node_name,
            "node_country": node_country, "nodes": nodes, "group": group}


def select_openclash_node(group, name):
    base, headers = _mihomo_api()
    run(["curl", "-s", "--max-time", "3", "-X", "PUT"] + headers +
        ["-H", "Content-Type: application/json", "-d", json.dumps({"name": name}),
         f"{base}/proxies/{group}"])


# ---------- flags (simplified, drawn -- fonts don't have colour emoji) ----------

def _flag_stripes(d, x, y, w, h, colors, vertical):
    n = len(colors)
    if vertical:
        seg = w / n
        for i, c in enumerate(colors):
            d.rectangle([x + i * seg, y, x + (i + 1) * seg, y + h], fill=c)
    else:
        seg = h / n
        for i, c in enumerate(colors):
            d.rectangle([x, y + i * seg, x + w, y + (i + 1) * seg], fill=c)


def _flag_nordic(d, x, y, w, h, bg, cross):
    d.rectangle([x, y, x + w, y + h], fill=bg)
    cx = x + w * 0.35
    cw = max(2, h * 0.22)
    d.rectangle([cx - cw / 2, y, cx + cw / 2, y + h], fill=cross)
    chh = max(2, h * 0.22)
    d.rectangle([x, y + h / 2 - chh / 2, x + w, y + h / 2 + chh / 2], fill=cross)


def _flag_uk(d, x, y, w, h):
    d.rectangle([x, y, x + w, y + h], fill=(1, 33, 105))
    d.rectangle([x + w * 0.40, y, x + w * 0.60, y + h], fill=(255, 255, 255))
    d.rectangle([x, y + h * 0.38, x + w, y + h * 0.62], fill=(255, 255, 255))
    d.rectangle([x + w * 0.45, y, x + w * 0.55, y + h], fill=(200, 16, 46))
    d.rectangle([x, y + h * 0.44, x + w, y + h * 0.56], fill=(200, 16, 46))


def _flag_china(d, x, y, w, h):
    d.rectangle([x, y, x + w, y + h], fill=(222, 41, 16))
    scx, scy, r = x + w * 0.24, y + h * 0.32, h * 0.16
    d.regular_polygon((scx, scy, r), n_sides=5, fill=(255, 222, 0))


def _flag_japan(d, x, y, w, h):
    d.rectangle([x, y, x + w, y + h], fill=(255, 255, 255))
    r = h * 0.28
    d.ellipse([x + w / 2 - r, y + h / 2 - r, x + w / 2 + r, y + h / 2 + r], fill=(188, 0, 45))


def _flag_korea(d, x, y, w, h):
    d.rectangle([x, y, x + w, y + h], fill=(255, 255, 255))
    r = h * 0.24
    cx, cy = x + w / 2, y + h / 2
    d.pieslice([cx - r, cy - r, cx + r, cy + r], start=200, end=20, fill=(205, 46, 53))
    d.pieslice([cx - r, cy - r, cx + r, cy + r], start=20, end=200, fill=(0, 71, 160))


def _flag_usa(d, x, y, w, h):
    stripes = 5
    for i in range(stripes):
        c = (178, 34, 52) if i % 2 == 0 else (255, 255, 255)
        d.rectangle([x, y + i * h / stripes, x + w, y + (i + 1) * h / stripes], fill=c)
    d.rectangle([x, y, x + w * 0.4, y + h * 0.55], fill=(60, 59, 110))


def _flag_switzerland(d, x, y, w, h):
    d.rectangle([x, y, x + w, y + h], fill=(213, 43, 30))
    cw, chh = w * 0.18, h * 0.18
    d.rectangle([x + w / 2 - cw / 2, y + h * 0.2, x + w / 2 + cw / 2, y + h * 0.8], fill=(255, 255, 255))
    d.rectangle([x + w * 0.2, y + h / 2 - chh / 2, x + w * 0.8, y + h / 2 + chh / 2], fill=(255, 255, 255))


def _flag_australia(d, x, y, w, h):
    d.rectangle([x, y, x + w, y + h], fill=(0, 39, 118))
    for dxr, dyr in [(0.75, 0.25), (0.85, 0.5), (0.75, 0.75), (0.6, 0.85), (0.65, 0.35)]:
        px, py = x + w * dxr, y + h * dyr
        d.ellipse([px - 1.5, py - 1.5, px + 1.5, py + 1.5], fill=(255, 255, 255))
    d.rectangle([x, y, x + w * 0.35, y + h * 0.35], fill=(255, 255, 255))
    d.rectangle([x + w * 0.05, y + h * 0.05, x + w * 0.30, y + h * 0.30], fill=(0, 39, 118))


def _flag_hongkong(d, x, y, w, h):
    d.rectangle([x, y, x + w, y + h], fill=(222, 41, 16))
    r = h * 0.18
    cx, cy = x + w / 2, y + h / 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255))


def _flag_taiwan(d, x, y, w, h):
    d.rectangle([x, y, x + w, y + h], fill=(222, 41, 16))
    d.rectangle([x, y, x + w * 0.5, y + h * 0.5], fill=(0, 0, 149))
    r = h * 0.09
    cx, cy = x + w * 0.25, y + h * 0.25
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255))


FLAG_DRAW = {
    "UK": _flag_uk,
    "China": _flag_china,
    "Japan": _flag_japan,
    "South Korea": _flag_korea,
    "USA": _flag_usa,
    "Switzerland": _flag_switzerland,
    "Australia": _flag_australia,
    "Hong Kong": _flag_hongkong,
    "Taiwan": _flag_taiwan,
    "Sweden": lambda d, x, y, w, h: _flag_nordic(d, x, y, w, h, (0, 106, 167), (254, 205, 27)),
    "Denmark": lambda d, x, y, w, h: _flag_nordic(d, x, y, w, h, (198, 12, 48), (255, 255, 255)),
    "Norway": lambda d, x, y, w, h: _flag_nordic(d, x, y, w, h, (186, 12, 47), (255, 255, 255)),
    "Finland": lambda d, x, y, w, h: _flag_nordic(d, x, y, w, h, (255, 255, 255), (0, 53, 128)),
    "France": lambda d, x, y, w, h: _flag_stripes(d, x, y, w, h, [(0, 35, 149), (255, 255, 255), (237, 41, 28)], True),
    "Belgium": lambda d, x, y, w, h: _flag_stripes(d, x, y, w, h, [(0, 0, 0), (253, 200, 47), (237, 41, 28)], True),
    "Germany": lambda d, x, y, w, h: _flag_stripes(d, x, y, w, h, [(0, 0, 0), (221, 0, 0), (255, 206, 0)], False),
    "Netherlands": lambda d, x, y, w, h: _flag_stripes(d, x, y, w, h, [(174, 28, 40), (255, 255, 255), (33, 70, 139)], False),
    "Italy": lambda d, x, y, w, h: _flag_stripes(d, x, y, w, h, [(0, 146, 70), (255, 255, 255), (206, 43, 55)], True),
    "Spain": lambda d, x, y, w, h: _flag_stripes(d, x, y, w, h, [(170, 21, 27), (241, 191, 0), (170, 21, 27)], False),
    "Portugal": lambda d, x, y, w, h: _flag_stripes(d, x, y, w, h, [(0, 102, 0), (255, 0, 0)], True),
    "Russia": lambda d, x, y, w, h: _flag_stripes(d, x, y, w, h, [(255, 255, 255), (0, 57, 166), (213, 43, 30)], False),
    "Romania": lambda d, x, y, w, h: _flag_stripes(d, x, y, w, h, [(0, 43, 127), (252, 209, 22), (206, 43, 55)], True),
    "Slovakia": lambda d, x, y, w, h: _flag_stripes(d, x, y, w, h, [(255, 255, 255), (0, 101, 189), (238, 28, 37)], False),
    "Canada": lambda d, x, y, w, h: _flag_stripes(d, x, y, w, h, [(255, 0, 0), (255, 255, 255), (255, 0, 0)], True),
}


def draw_flag(d, x, y, w, h, country):
    fn = FLAG_DRAW.get(country)
    if fn:
        fn(d, x, y, w, h)
    else:
        d.rectangle([x, y, x + w, y + h], fill=(70, 75, 90))
        initials = (country[:2] if country else "??").upper()
        f = font("default_bold", int(h * 0.5))
        bbox = d.textbbox((0, 0), initials, font=f)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text((x + (w - tw) / 2, y + (h - th) / 2 - bbox[1]), initials, font=f, fill=(230, 230, 230))
    d.rectangle([x, y, x + w, y + h], outline=(0, 0, 0), width=1)


# ---------- weather icons (simple geometric, no image assets needed) ----------

_SUN = (255, 196, 66)
_CLOUD = (150, 160, 178)
_RAIN = (108, 168, 235)
_SNOW = (225, 232, 240)
_STORM = (210, 170, 60)
_FOG = (130, 138, 152)


def _icon_sun(d, cx, cy, r):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_SUN)
    for i in range(8):
        ang = i * math.pi / 4
        x0, y0 = cx + math.cos(ang) * r * 1.35, cy + math.sin(ang) * r * 1.35
        x1, y1 = cx + math.cos(ang) * r * 1.7, cy + math.sin(ang) * r * 1.7
        d.line([x0, y0, x1, y1], fill=_SUN, width=2)


def _icon_cloud(d, cx, cy, r, color=_CLOUD):
    d.ellipse([cx - r * 1.1, cy - r * 0.2, cx - r * 0.1, cy + r * 0.8], fill=color)
    d.ellipse([cx - r * 0.3, cy - r * 0.7, cx + r * 0.9, cy + r * 0.5], fill=color)
    d.ellipse([cx + r * 0.2, cy - r * 0.1, cx + r * 1.3, cy + r * 0.8], fill=color)
    d.rectangle([cx - r * 0.9, cy + r * 0.1, cx + r * 0.9, cy + r * 0.8], fill=color)


def _icon_cloud_sun(d, cx, cy, r):
    _icon_sun(d, cx - r * 0.35, cy - r * 0.35, r * 0.55)
    _icon_cloud(d, cx + r * 0.15, cy + r * 0.15, r * 0.85)


def _icon_rain(d, cx, cy, r):
    _icon_cloud(d, cx, cy - r * 0.25, r * 0.9)
    for dx in (-0.5, 0, 0.5):
        x0 = cx + dx * r
        d.line([x0, cy + r * 0.7, x0 - 2, cy + r * 1.3], fill=_RAIN, width=2)


def _icon_snow(d, cx, cy, r):
    _icon_cloud(d, cx, cy - r * 0.25, r * 0.9, color=_SNOW)
    for dx in (-0.5, 0, 0.5):
        x, y = cx + dx * r, cy + r * 1.0
        for ang in range(0, 180, 60):
            rad = math.radians(ang)
            d.line([x - 4 * math.cos(rad), y - 4 * math.sin(rad),
                    x + 4 * math.cos(rad), y + 4 * math.sin(rad)], fill=_SNOW, width=1)


def _icon_fog(d, cx, cy, r):
    for i, dy in enumerate([-0.3, 0.1, 0.5]):
        d.line([cx - r, cy + dy * r, cx + r, cy + dy * r], fill=_FOG, width=3)


def _icon_storm(d, cx, cy, r):
    _icon_cloud(d, cx, cy - r * 0.3, r * 0.9)
    d.polygon([(cx - 2, cy + r * 0.5), (cx + 6, cy + r * 0.5), (cx - 2, cy + r * 1.2),
               (cx + 2, cy + r * 0.9), (cx - 6, cy + r * 0.9)], fill=_STORM)


_ICON_DRAW = {
    "sun": _icon_sun, "cloud": _icon_cloud, "cloud_sun": _icon_cloud_sun,
    "rain": _icon_rain, "snow": _icon_snow, "fog": _icon_fog, "storm": _icon_storm,
}


def draw_weather_icon(d, cx, cy, r, icon_key):
    _ICON_DRAW.get(icon_key, _icon_cloud)(d, cx, cy, r)


def draw_analog_clock(d, cx, cy, r, dt, accent):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=FG, width=2)
    for h in range(12):
        ang = math.radians(h * 30 - 90)
        outer = r - 3
        inner = r - 9 if h % 3 == 0 else r - 6
        x0, y0 = cx + math.cos(ang) * inner, cy + math.sin(ang) * inner
        x1, y1 = cx + math.cos(ang) * outer, cy + math.sin(ang) * outer
        d.line([x0, y0, x1, y1], fill=DIM, width=2 if h % 3 == 0 else 1)

    hour_ang = math.radians((dt.hour % 12 + dt.minute / 60) * 30 - 90)
    min_ang = math.radians((dt.minute + dt.second / 60) * 6 - 90)
    sec_ang = math.radians(dt.second * 6 - 90)

    hl = r * 0.5
    d.line([cx, cy, cx + math.cos(hour_ang) * hl, cy + math.sin(hour_ang) * hl], fill=FG, width=4)
    ml = r * 0.75
    d.line([cx, cy, cx + math.cos(min_ang) * ml, cy + math.sin(min_ang) * ml], fill=FG, width=3)
    sl = r * 0.85
    d.line([cx, cy, cx + math.cos(sec_ang) * sl, cy + math.sin(sec_ang) * sl], fill=accent, width=1)
    d.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=accent)


def draw_digital_clock(d, cx, cy, dt, accent):
    f_time = font("default_mono_medium", 30)
    centered_text(d, cx, cy - 20, dt.strftime("%H:%M"), f_time, FG)
    f_sec = font("default_medium", 13)
    centered_text(d, cx, cy + 14, dt.strftime(":%S"), f_sec, accent)


def _icon_wifi_signal(d, cx, cy, r, color):
    d.ellipse([cx - 2, cy + r * 0.55 - 2, cx + 2, cy + r * 0.55 + 2], fill=color)
    for frac in (0.45, 0.72, 1.0):
        rr = r * frac
        bbox = [cx - rr, cy - rr * 0.4, cx + rr, cy + rr * 1.6]
        d.arc(bbox, start=222, end=318, fill=color, width=3)


def _icon_settings_gear(d, cx, cy, r, color):
    for y_frac, handle_frac in ((-0.55, 0.28), (0, 0.68), (0.55, 0.42)):
        y = cy + r * y_frac
        d.line([cx - r, y, cx + r, y], fill=(80, 86, 100), width=2)
        hx = cx - r + 2 * r * handle_frac
        d.ellipse([hx - 5, y - 5, hx + 5, y + 5], fill=color)


def _icon_lock(d, cx, cy, r, color):
    d.arc([cx - r * 0.6, cy - r * 1.3, cx + r * 0.6, cy - r * 0.1], start=180, end=360, fill=color, width=2)
    d.rounded_rectangle([cx - r, cy - r * 0.2, cx + r, cy + r], radius=2, fill=color)


def _icon_shield(d, cx, cy, r, color):
    pts = [(cx, cy - r), (cx + r, cy - r * 0.5), (cx + r, cy + r * 0.3),
           (cx, cy + r), (cx - r, cy + r * 0.3), (cx - r, cy - r * 0.5)]
    d.polygon(pts, outline=color, width=2)


def _icon_sms(d, cx, cy, r, color):
    x0, y0, x1, y1 = cx - r, cy - r * 0.7, cx + r, cy + r * 0.7
    d.rounded_rectangle([x0, y0, x1, y1], radius=3, outline=color, width=2)
    d.line([x0, y0, cx, cy + r * 0.15], fill=color, width=2)
    d.line([cx, cy + r * 0.15, x1, y0], fill=color, width=2)


# ---------- loading overlay ----------

SPINNER_SPEED_DPS = 300.0     # degrees/second -- iOS-ish, brisk but calm

# Upper bound for the "Switching to stock UI…" transition animation.
# toggle.sh's own worst case (wait_gone: six 0.2s polls, then SIGKILL any
# survivor) is ~1.2s, so this leaves real slack for gl_screen's own
# startup while still being short enough that the overlay itself never
# becomes the thing that looks stuck.
SWITCH_OVERLAY_MAX_S = 3.0


def draw_ring_spinner(d, cx, cy, r, phase_deg, accent, width=3, arc_deg=270, segments=18):
    """A rotating arc with a brightness ramp toward its leading end. PIL
    can only stroke an arc in one flat colour, so the ramp is drawn as
    `segments` short arcs -- cheap at this size and it reads as a proper
    fading tail rather than a plain spinning stick."""
    track = _mix(BG, accent, 0.18)
    d.arc([cx - r, cy - r, cx + r, cy + r], 0, 360, fill=track, width=width)
    step = arc_deg / segments
    for i in range(segments):
        t = (i + 1) / segments                     # 0 = tail, 1 = head
        col = _mix(track, accent, t * t)           # squared: longer dim tail
        a0 = phase_deg + i * step
        d.arc([cx - r, cy - r, cx + r, cy + r], a0, a0 + step + 1, fill=col, width=width)


def draw_loading_overlay(base, label=None, phase_deg=0.0, accent=None):
    """Dim whatever was on screen and float a spinner (and optional
    caption) over it. Keeping the real screen visible underneath -- rather
    than replacing it with a blank "loading" page -- is what makes a slow
    action read as *busy* instead of *crashed*, which was the whole
    complaint: several actions here take seconds with no feedback at all."""
    accent = accent or ACCENT["clock"]
    img = Image.blend(base, Image.new("RGB", (W, H), (0, 0, 0)), 0.62)
    d = ImageDraw.Draw(img)
    cx, cy = W // 2, H // 2 - (12 if label else 0)
    if label:
        f = font("default_medium", 13)
        tw = d.textlength(label, font=f)
        card_w = max(132, tw + 44)
        card_h = 116
        x0, y0 = cx - card_w / 2, cy - 44
        d.rounded_rectangle([x0, y0, x0 + card_w, y0 + card_h], radius=16,
                            fill=(26, 34, 52), outline=(48, 58, 82))
        draw_ring_spinner(d, cx, cy + 2, 17, phase_deg, accent)
        centered_text(d, cx, y0 + card_h - 28, label, f, FG)
    else:
        draw_ring_spinner(d, cx, cy, 19, phase_deg, accent)
    return img


def run_with_spinner(base_img, label, fn, accent=None, min_visible=0.4, fps=30):
    """Run fn() on a worker thread, animating the overlay until it returns.

    Used for the handful of genuinely slow, *user-initiated* actions
    (force-refresh, picking a weather city, applying a data cap). Those
    can't just be moved to the background refresher the way the periodic
    pulls were -- the user is waiting on the result -- so instead of
    freezing on a still frame they now get a live spinner over the screen
    they tapped from. min_visible stops a fast action from flashing the
    overlay for two frames."""
    box = {}

    def worker():
        try:
            box["value"] = fn()
        except Exception:
            box["value"] = None

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    t0 = time.time()
    interval = 1.0 / fps
    while True:
        elapsed = time.time() - t0
        if not th.is_alive() and elapsed >= min_visible:
            break
        write_frame(draw_loading_overlay(base_img, label, elapsed * SPINNER_SPEED_DPS, accent))
        time.sleep(interval)
    # Anything tapped while the overlay was up was aimed at the busy
    # screen, not at whatever comes next -- drop it rather than letting it
    # land on a screen the user never saw.
    with touch_state.lock:
        touch_state.release_pending = False
    return box.get("value")


# ---------- widgets ----------

def new_canvas():
    img = Image.new("RGB", (W, H), BG)
    return img, ImageDraw.Draw(img)


def draw_page_dots(d, active_idx, count=7):
    total_w = count * 16
    x0 = (W - total_w) // 2
    y = H - 18
    for i in range(count):
        x = x0 + i * 16
        r = 4 if i == active_idx else 3
        color = FG if i == active_idx else DIM
        d.ellipse([x - r, y - r, x + r, y + r], fill=color)


def _mix(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def draw_spinner(d, cx, cy, r, phase_deg, color, width=3):
    """A 270-degree arc that rotates with `phase_deg` -- classic
    "connecting" spinner look, redrawn each frame at a new phase."""
    start = phase_deg % 360
    d.arc([cx - r, cy - r, cx + r, cy + r], start=start, end=start + 270, fill=color, width=width)


def draw_signal_bars(d, x0, y_base, bars, color, dim_color, bar_w=3, gap=2, max_h=10):
    """4 ascending bars (classic phone signal icon), bottom-aligned at
    y_base, growing upward. `bars` (0-4) of them filled with `color`, the
    rest drawn dim (still visible against the colored header, just muted)."""
    for i in range(4):
        bh = max_h * (i + 1) / 4
        bx0 = x0 + i * (bar_w + gap)
        fill = color if i < bars else dim_color
        d.rectangle([bx0, y_base - bh, bx0 + bar_w, y_base], fill=fill)


def draw_header(d, label, accent, conn_type=None, cell_signal=None):
    d.rectangle([0, 0, W, 34], fill=accent)
    d.text((14, 8), label, font=font("default_bold", 18), fill=BG)

    right_x = W - 14
    if conn_type:
        f = font("default_medium", 12)
        bbox = d.textbbox((0, 0), conn_type, font=f)
        tw = bbox[2] - bbox[0]
        d.text((right_x - tw, 10), conn_type, font=f, fill=BG)
        right_x -= tw + 8

    if cell_signal:
        bars, rat = cell_signal
        if conn_type != rat:
            f2 = font("default_medium", 11)
            bbox2 = d.textbbox((0, 0), rat, font=f2)
            tw2 = bbox2[2] - bbox2[0]
            d.text((right_x - tw2, 11), rat, font=f2, fill=BG)
            right_x -= tw2 + 4
        bars_w = 4 * 3 + 3 * 2
        dim = _mix(BG, accent, 0.55)
        draw_signal_bars(d, right_x - bars_w, 23, bars, BG, dim)
        right_x -= bars_w + 6


def draw_back_header(d, label, accent):
    d.rectangle([0, 0, W, 34], fill=accent)
    d.text((12, 5), "‹", font=font("default_bold", 24), fill=BG)
    d.text((32, 8), label, font=font("default_bold", 16), fill=BG)


def draw_toggle(d, x, y, on, accent, w=52, h=28):
    r = h / 2
    color = accent if on else (60, 65, 80)
    d.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=color)
    knob_r = h / 2 - 3
    kx = x + w - r if on else x + r
    ky = y + h / 2
    d.ellipse([kx - knob_r, ky - knob_r, kx + knob_r, ky + knob_r], fill=(255, 255, 255))


def draw_segmented(d, x, y, w, h, labels, selected_idx, accent, fsize=14):
    d.rounded_rectangle([x, y, x + w, y + h], radius=h / 2, outline=accent, width=2)
    seg_w = w / len(labels)
    hx0 = x + selected_idx * seg_w
    d.rounded_rectangle([hx0 + 2, y + 2, hx0 + seg_w - 2, y + h - 2], radius=(h - 4) / 2, fill=accent)
    f = font("default_medium", fsize)
    for i, label in enumerate(labels):
        bbox = d.textbbox((0, 0), label, font=f)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = x + i * seg_w + (seg_w - tw) / 2
        ty = y + (h - th) / 2 - bbox[1]
        color = BG if i == selected_idx else FG
        d.text((tx, ty), label, font=f, fill=color)


def centered_text(d, cx, y, text, f, fill):
    bbox = d.textbbox((0, 0), text, font=f)
    tw = bbox[2] - bbox[0]
    d.text((cx - tw / 2, y), text, font=f, fill=fill)


def centered_text_box(d, x0, y0, x1, y1, text, f, fill):
    """Centers on both axes within [x0,y0,x1,y1] -- unlike centered_text
    (horizontal-only, `y` is a hand-picked top offset), this accounts for
    the font's actual glyph bbox so it lands in the true middle of a
    button regardless of font metrics or box height."""
    bbox = d.textbbox((0, 0), text, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (x0 + x1) / 2 - tw / 2
    ty = y0 + ((y1 - y0) - th) / 2 - bbox[1]
    d.text((tx, ty), text, font=f, fill=fill)


def truncate_to_width(d, text, f, max_w):
    if d.textbbox((0, 0), text, font=f)[2] <= max_w:
        return text
    while len(text) > 1:
        text = text[:-1]
        candidate = text + "…"
        if d.textbbox((0, 0), candidate, font=f)[2] <= max_w:
            return candidate
    return text[:1] + "…"


def wrap_text_to_lines(d, text, f, max_w):
    """Character-by-character wrap (not word-split) so it works for both
    space-separated scripts and CJK, which has no spaces between words."""
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for ch in paragraph:
            candidate = line + ch
            if line and d.textbbox((0, 0), candidate, font=f)[2] > max_w:
                lines.append(line)
                line = ch
            else:
                line = candidate
        lines.append(line)
    return lines


def draw_tile(d, x0, y0, x1, y1, icon_fn, label, subtitle, accent):
    d.rounded_rectangle([x0, y0, x1, y1], radius=10, fill=(22, 28, 40), outline=(42, 48, 60), width=1)
    cx = (x0 + x1) / 2
    icon_fn(d, cx, y0 + 32, 20, accent)
    centered_text(d, cx, y0 + 58, label, font("default_bold", 14), FG)
    if subtitle:
        f = font("default_medium", 11)
        text = truncate_to_width(d, subtitle, f, (x1 - x0) - 12)
        centered_text(d, cx, y0 + 78, text, f, DIM)


def draw_sparkline(d, x, y, w, h, points, color):
    """points: list of (label, value), oldest first. Thin line + a faint fill
    under it, a highlighted end dot on the latest value, min/max as direct
    labels in muted ink (not the series colour) rather than a dense axis."""
    if not points or len(points) < 2:
        centered_text(d, x + w / 2, y + h / 2 - 6, "not enough data yet", font("default_medium", 11), DIM)
        return
    vals = [v for _, v in points]
    vmin, vmax = min(vals), max(vals)
    span = vmax - vmin
    if span < 1e-9:
        span = max(abs(vmax) * 0.001, 1e-6)
    pad_top, pad_bot = 14, 14

    def px(i):
        return x + i * (w / (len(points) - 1))

    def py(v):
        return y + pad_top + (1 - (v - vmin) / span) * (h - pad_top - pad_bot)

    d.line([x, y + h - pad_bot, x + w, y + h - pad_bot], fill=(38, 42, 52), width=1)

    poly = [(px(i), py(v)) for i, (_, v) in enumerate(points)]
    fill_color = tuple(int(c * 0.16 + bg * 0.84) for c, bg in zip(color, BG))
    d.polygon(poly + [(px(len(points) - 1), y + h), (px(0), y + h)], fill=fill_color)
    d.line(poly, fill=color, width=2, joint="curve")

    ex, ey = poly[-1]
    d.ellipse([ex - 3, ey - 3, ex + 3, ey + 3], fill=color)

    f = font("default_medium", 10)
    d.text((x, y), f"{vmax:.3f}", font=f, fill=DIM)
    bbox = d.textbbox((0, 0), f"{vmin:.3f}", font=f)
    d.text((x, y + h - (bbox[3] - bbox[1]) - 2), f"{vmin:.3f}", font=f, fill=DIM)


# ---------- layout constants (shared by drawing and hit-testing) ----------

CLOCK_LEFT_ZONE = (0, 34, W // 2, 142)
CLOCK_RIGHT_ZONE = (W // 2, 34, W, 142)
REPEATER_TILE = (8, 150, 116, 244)
MORE_TILE = (124, 150, 232, 244)
SMS_TILE = (8, 250, 232, 296)

FX_TOP_ZONE = (34, 122)
FX_BOTTOM_ZONE = (128, 216)
FX_RANGE_RECT = (16, 224, 224, 246)
FX_STATUS_Y = 252
FX_BUTTON = (50, 268, 190, 288)

SIM_CHOICE_RECT = (16, 86, 156, 110)
SIM_ROAM_TOGGLE_RECT = (172, 86, 216, 110)
# Network (attach) and Data toggles sit side by side to the right of the
# country name, shifted toward center rather than hugging the panel edge,
# and sized to match Roam (44x24) so all three toggles feel equally easy
# to tap.
SIM_ATTACH_TOGGLE_RECT = (108, 57, 152, 81)
SIM_DATA_TOGGLE_RECT = (158, 57, 202, 81)

OC_TOGGLE_RECT = (172, 38, 218, 60)
OC_MODE_SEG_RECT = (16, 100, 224, 128)
OC_NODE_ZONE = (146, 192)
OC_UPDATE_BUTTON = (24, 252, 216, 272)

WEATHER_CITY_ZONE = (34, 66)

PICKER_TOP, PICKER_BOTTOM = 38, 316

PANEL_NAMES = ["clock", "sim", "monitor", "weather", "fx", "openclash", "games"]


# ---------- main panels ----------

def panel_clock(cfg, rep, conn_type=None, cell_signal=None, sms_messages=None):
    from zoneinfo import ZoneInfo
    img, d = new_canvas()
    draw_header(d, "HOME", ACCENT["clock"], conn_type, cell_signal)
    tz_l, tz_r = cfg["clock_top"], cfg["clock_bottom"]
    dt_l = datetime.now(ZoneInfo(tz_l))
    dt_r = datetime.now(ZoneInfo(tz_r))

    if cfg.get("clock_style") == "digital":
        draw_digital_clock(d, W / 4, 72, dt_l, ACCENT["clock"])
        draw_digital_clock(d, W * 3 / 4, 72, dt_r, ACCENT["clock"])
    else:
        draw_analog_clock(d, W / 4, 72, 30, dt_l, ACCENT["clock"])
        draw_analog_clock(d, W * 3 / 4, 72, 30, dt_r, ACCENT["clock"])
    centered_text(d, W / 4, 106, f"{city_name(tz_l)}  ›", font("default_medium", 12), DIM)
    centered_text(d, W * 3 / 4, 106, f"{city_name(tz_r)}  ›", font("default_medium", 12), DIM)
    centered_text(d, W / 2, 122, dt_l.strftime("%a %d %b"), font("default_medium", 12), DIM)

    d.line([16, 142, W - 16, 142], fill=(34, 38, 48))

    rep_sub = rep["ssid"] if rep["connected"] else "Not connected"
    draw_tile(d, 8, 150, 116, 244,
              lambda dd, cx, cy, r, ac: _icon_wifi_signal(dd, cx, cy, r, ac),
              "Repeater", rep_sub, ACCENT["clock"])
    draw_tile(d, 124, 150, 232, 244,
              lambda dd, cx, cy, r, ac: _icon_settings_gear(dd, cx, cy, r, ac),
              "More", "Settings", ACCENT["clock"])

    sx0, sy0, sx1, sy1 = SMS_TILE
    d.rounded_rectangle([sx0, sy0, sx1, sy1], radius=10, fill=(22, 28, 40), outline=(42, 48, 60), width=1)
    _icon_sms(d, sx0 + 28, (sy0 + sy1) / 2, 13, ACCENT["clock"])
    messages = sms_messages or []
    if messages:
        latest = messages[0]
        d.text((sx0 + 52, sy0 + 9), "Messages", font=font("default_bold", 14), fill=FG)
        sub = f"{latest['from']}: {latest['body'].replace(chr(10), ' ')}"
        f_sub = font("default_cn_medium", 11)
        sub = truncate_to_width(d, sub, f_sub, (sx1 - sx0) - 64)
        d.text((sx0 + 52, sy0 + 27), sub, font=f_sub, fill=DIM)
    else:
        d.text((sx0 + 52, sy0 + 9), "Messages", font=font("default_bold", 14), fill=FG)
        d.text((sx0 + 52, sy0 + 27), "No messages", font=font("default_medium", 11), fill=DIM)

    draw_page_dots(d, 0)
    return img


FX_RANGE_LABELS = {"week": "Week", "month": "Month", "year": "Year"}


def draw_fx_row(d, y_label, y_value, from_code, rate, to_code):
    """'1 {from} ›  =  {rate} {to} ›' -- from and to are each their
    own tap target (left half of the row vs right half, see hit_main_fx)."""
    f_label = font("default_medium", 15)
    f_value = font("default_bold", 17)
    x = 16
    seg = f"1 {from_code} ›  =  "
    d.text((x, y_label), seg, font=f_label, fill=DIM)
    x += d.textbbox((0, 0), seg, font=f_label)[2]
    val = f"{rate:.3f}" if rate is not None else "—"
    d.text((x, y_value), val, font=f_value, fill=FG)
    x += d.textbbox((0, 0), val, font=f_value)[2]
    d.text((x, y_label), f" {to_code} ›", font=f_label, fill=DIM)


def panel_fx(cfg, fx, fx_range, conn_type=None, cell_signal=None):
    img, d = new_canvas()
    draw_header(d, "CURRENCY", ACCENT["fx"], conn_type, cell_signal)
    top_from, top_to = cfg["fx_top_from"], cfg["fx_top_to"]
    bot_from, bot_to = cfg["fx_bottom_from"], cfg["fx_bottom_to"]
    top_rate = rate_between(top_from, top_to, fx)
    bot_rate = rate_between(bot_from, bot_to, fx)

    draw_fx_row(d, 40, 38, top_from, top_rate, top_to)
    top_hist = get_fx_history_cached(top_from, top_to, fx_range)
    draw_sparkline(d, 16, 62, W - 32, 56, top_hist, ACCENT["fx"])

    d.line([16, 126, W - 16, 126], fill=DIM)

    draw_fx_row(d, 132, 130, bot_from, bot_rate, bot_to)
    bot_hist = get_fx_history_cached(bot_from, bot_to, fx_range)
    draw_sparkline(d, 16, 154, W - 32, 56, bot_hist, ACCENT["fx"])

    rx0, ry0, rx1, ry1 = FX_RANGE_RECT
    sel_idx = FX_RANGES.index(fx_range)
    draw_segmented(d, rx0, ry0, rx1 - rx0, ry1 - ry0,
                   [FX_RANGE_LABELS[r] for r in FX_RANGES], sel_idx, ACCENT["fx"], fsize=13)

    if fx:
        age_min = int((time.time() - fx["ts"]) / 60)
        age_txt = "updated just now" if age_min <= 0 else f"updated {age_min} min ago"
    else:
        age_txt = "no rate yet"
    centered_text(d, W / 2, FX_STATUS_Y, age_txt, font("default_medium", 12), DIM)

    bx0, by0, bx1, by1 = FX_BUTTON
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=(by1 - by0) / 2, outline=ACCENT["fx"], width=2)
    centered_text_box(d, bx0, by0, bx1, by1, "Update Now", font("default_medium", 12), ACCENT["fx"])
    draw_page_dots(d, 4)
    return img


SIM_CHOICE_LABELS = ["SIM1", "eSIM"]
SIM_CHOICE_KEYS = ["sim1", "esim"]


SIM_WIREGUARD_TILE = (16, 248, W - 16, 286)


def panel_sim(cfg, sim, conn_type=None, cell_signal=None, wg_peers=None, wg_active=None):
    img, d = new_canvas()
    draw_header(d, "ACTIVE SIM", ACCENT["sim"], conn_type, cell_signal)
    country = sim["country"] or "unknown"
    d.text((16, 44), f"Slot {sim['slot']}", font=font("default_medium", 14), fill=DIM)
    draw_flag(d, 16, 60, 26, 18, country)
    d.text((50, 57), country, font=font("default_bold", 20), fill=FG)

    ax0, ay0, ax1, ay1 = SIM_ATTACH_TOGGLE_RECT
    centered_text(d, (ax0 + ax1) / 2, 44, "Net", font("default_medium", 10), DIM)
    draw_toggle(d, ax0, ay0, sim["attached"], ACCENT["sim"], w=ax1 - ax0, h=ay1 - ay0)

    dx0, dy0, dx1, dy1 = SIM_DATA_TOGGLE_RECT
    centered_text(d, (dx0 + dx1) / 2, 44, "Data", font("default_medium", 10), DIM)
    draw_toggle(d, dx0, dy0, sim["data_up"], ACCENT["sim"], w=dx1 - dx0, h=dy1 - dy0)

    cx0, cy0, cx1, cy1 = SIM_CHOICE_RECT
    sel_idx = SIM_CHOICE_KEYS.index(sim["sim_choice"])
    draw_segmented(d, cx0, cy0, cx1 - cx0, cy1 - cy0, SIM_CHOICE_LABELS, sel_idx, ACCENT["sim"], fsize=11)

    tx0, ty0, tx1, ty1 = SIM_ROAM_TOGGLE_RECT
    draw_toggle(d, tx0, ty0, sim["roaming"], ACCENT["sim"], w=tx1 - tx0, h=ty1 - ty0)
    centered_text(d, 194, 114, "Roam", font("default_medium", 10), DIM)

    d.text((16, 126), sim["phone"] or "—", font=font("default_mono_medium", 16), fill=DIM)

    d.line([16, 152, W - 16, 152], fill=DIM)

    d.text((16, 164), "Data used  ›", font=font("default_medium", 14), fill=DIM)
    used, cap = sim["traffic_mb"], sim["cap_mb"]
    d.text((16, 180), f"{used:.1f} MB" if used is not None else "n/a", font=font("default_mono_medium", 20), fill=FG)

    bx0, by0, bx1, by1 = 16, 206, W - 16, 222
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=8, outline=DIM, width=1)
    if used is not None and cap:
        pct = max(0, min(100, used / cap * 100))
        bar_w = int((bx1 - bx0 - 4) * pct / 100)
        if bar_w > 20:
            d.rounded_rectangle([bx0 + 2, by0 + 2, bx0 + 2 + bar_w, by1 - 2], radius=6, fill=ACCENT["sim"])
        elif bar_w > 0:
            # PIL's rounded_rectangle breaks ("x1 must be >= x0") when the
            # shape is thinner than ~2x the corner radius.
            d.rectangle([bx0 + 2, by0 + 2, bx0 + 2 + bar_w, by1 - 2], fill=ACCENT["sim"])
        caption = f"{pct:.0f}% of {cap_label(cap)} used"
    elif cap is None:
        caption = "no limit set · tap to set"
    else:
        caption = "tap to set a limit"
    centered_text(d, W / 2, 228, caption, font("default_medium", 11), DIM)

    wx0, wy0, wx1, wy1 = SIM_WIREGUARD_TILE
    d.rounded_rectangle([wx0, wy0, wx1, wy1], radius=10, fill=(22, 28, 40), outline=(42, 48, 60), width=1)
    _icon_shield(d, wx0 + 24, (wy0 + wy1) / 2, 11, ACCENT["sim"])
    peers = wg_peers or []
    active = next((p["name"] for p in peers if p["id"] == wg_active), None)
    d.text((wx0 + 44, wy0 + 4), "WireGuard", font=font("default_bold", 13), fill=FG)
    if active:
        sub = f"On · {active}"
    elif peers:
        sub = "Off"
    else:
        sub = "No configs · add in LuCI"
    d.text((wx0 + 44, wy0 + 20), sub, font=font("default_medium", 10), fill=DIM)

    draw_page_dots(d, 1)
    return img


def panel_openclash(oc, traf, conn_type=None, cell_signal=None):
    img, d = new_canvas()
    draw_header(d, "OPENCLASH", ACCENT["openclash"], conn_type, cell_signal)

    if not oc["installed"]:
        centered_text(d, W / 2, 130, "OpenClash isn't installed", font("default_medium", 14), DIM)
        centered_text(d, W / 2, 150, "on this device", font("default_medium", 14), DIM)
        draw_page_dots(d, 5)
        return img

    d.text((16, 44), "Enabled", font=font("default_medium", 16), fill=FG)
    tx0, ty0, tx1, ty1 = OC_TOGGLE_RECT
    draw_toggle(d, tx0, ty0, oc["enabled"], ACCENT["openclash"], w=tx1 - tx0, h=ty1 - ty0)

    d.line([16, 72, W - 16, 72], fill=(40, 44, 54))

    d.text((16, 80), "Mode", font=font("default_medium", 14), fill=DIM)
    sx0, sy0, sx1, sy1 = OC_MODE_SEG_RECT
    sel_idx = 0 if oc["mode"] == "global" else 1
    draw_segmented(d, sx0, sy0, sx1 - sx0, sy1 - sy0, ["Global", "Rule"], sel_idx, ACCENT["openclash"])

    d.line([16, 138, W - 16, 138], fill=(40, 44, 54))

    d.text((16, 146), "Node  ›", font=font("default_medium", 14), fill=DIM)
    if not traf["running"]:
        d.text((16, 166), "not running", font=font("default_medium", 15), fill=DIM)
    elif traf["node_name"]:
        if traf["node_country"]:
            draw_flag(d, 16, 164, 24, 16, traf["node_country"])
            d.text((48, 161), traf["node_country"], font=font("default_bold", 17), fill=FG)
        else:
            f = font("default_cn_medium", 15)
            short = truncate_to_width(d, traf["node_name"], f, W - 32)
            d.text((16, 164), short, font=f, fill=FG)
    else:
        d.text((16, 166), "no subscription yet", font=font("default_medium", 13), fill=DIM)

    d.line([16, 192, W - 16, 192], fill=(40, 44, 54))

    d.text((16, 200), "Traffic (session)", font=font("default_medium", 14), fill=DIM)
    if traf["running"] and traf["up_mb"] is not None:
        stats = f"↑ {traf['up_mb']:.1f} MB   ↓ {traf['down_mb']:.1f} MB"
        d.text((16, 220), stats, font=font("default_mono_medium", 15), fill=FG)
    else:
        d.text((16, 220), "—", font=font("default_mono_medium", 15), fill=DIM)

    bx0, by0, bx1, by1 = OC_UPDATE_BUTTON
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=(by1 - by0) / 2, outline=ACCENT["openclash"], width=2)
    centered_text_box(d, bx0, by0, bx1, by1, "Update Subscription", font("default_medium", 12), ACCENT["openclash"])
    draw_page_dots(d, 5)
    return img


MONITOR_CPU_BAR = (16, 144, W - 16, 158)
MONITOR_RAM_BAR = (16, 200, W - 16, 214)


def panel_monitor(net_down, net_up, net_iface, cpu_pct, ram_pct, ram_used_gb, ram_total_gb, temp_c, uptime_min, conn_type=None, cell_signal=None):
    img, d = new_canvas()
    draw_header(d, "MONITOR", ACCENT["monitor"], conn_type, cell_signal)

    bw_label = f"Bandwidth · {net_iface}" if net_iface else "Bandwidth"
    d.text((16, 44), bw_label, font=font("default_medium", 13), fill=DIM)
    d.text((16, 62), "Down", font=font("default_medium", 12), fill=DIM)
    down_txt = f"↓ {net_down:.1f} Mbps" if net_down is not None else "—"
    d.text((16, 78), down_txt, font=font("default_bold", 19), fill=FG)
    d.text((W / 2 + 8, 62), "Up", font=font("default_medium", 12), fill=DIM)
    up_txt = f"↑ {net_up:.1f} Mbps" if net_up is not None else "—"
    d.text((W / 2 + 8, 78), up_txt, font=font("default_bold", 19), fill=FG)

    d.line([16, 116, W - 16, 116], fill=(40, 44, 54))

    d.text((16, 124), "CPU", font=font("default_medium", 14), fill=FG)
    cpu_txt = f"{cpu_pct:.0f}%" if cpu_pct is not None else "—"
    centered_text(d, W - 30, 124, cpu_txt, font("default_medium", 13), DIM)
    bx0, by0, bx1, by1 = MONITOR_CPU_BAR
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=6, outline=DIM, width=1)
    if cpu_pct is not None:
        bw = int((bx1 - bx0 - 4) * cpu_pct / 100)
        if bw > 14:
            d.rounded_rectangle([bx0 + 2, by0 + 2, bx0 + 2 + bw, by1 - 2], radius=4, fill=ACCENT["monitor"])
        elif bw > 0:
            # PIL's rounded_rectangle breaks ("x1 must be >= x0") when the
            # shape is thinner than ~2x the corner radius -- plain
            # rectangle for anything that thin (same fix as elsewhere in
            # this file for the same underlying PIL quirk).
            d.rectangle([bx0 + 2, by0 + 2, bx0 + 2 + bw, by1 - 2], fill=ACCENT["monitor"])

    d.text((16, 180), "RAM", font=font("default_medium", 14), fill=FG)
    ram_txt = f"{ram_used_gb:.1f} / {ram_total_gb:.1f} GB" if ram_pct is not None else "—"
    centered_text(d, W - 60, 180, ram_txt, font("default_medium", 12), DIM)
    bx0, by0, bx1, by1 = MONITOR_RAM_BAR
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=6, outline=DIM, width=1)
    if ram_pct is not None:
        bw = int((bx1 - bx0 - 4) * ram_pct / 100)
        if bw > 14:
            d.rounded_rectangle([bx0 + 2, by0 + 2, bx0 + 2 + bw, by1 - 2], radius=4, fill=ACCENT["monitor"])
        elif bw > 0:
            d.rectangle([bx0 + 2, by0 + 2, bx0 + 2 + bw, by1 - 2], fill=ACCENT["monitor"])

    d.line([16, 230, W - 16, 230], fill=(40, 44, 54))

    temp_txt = f"{temp_c:.0f}°C" if temp_c is not None else "—"
    d.text((16, 240), f"Temp     {temp_txt}", font=font("default_medium", 15), fill=FG)
    up_h, up_m = divmod(uptime_min, 60)
    d.text((16, 264), f"Uptime   {up_h}h {up_m}m", font=font("default_medium", 15), fill=FG)

    draw_page_dots(d, 2)
    return img


def weather_day_labels(days):
    labels = ["Today", "Tomorrow"]
    for i in range(2, len(days)):
        try:
            wd = datetime.strptime(days[i]["date"], "%Y-%m-%d").strftime("%a")
        except Exception:
            wd = "Day " + str(i + 1)
        labels.append(wd)
    return labels


WEATHER_DAY_ZONE = (66, 226)
WEATHER_UPDATE_BUTTON = (50, 268, 190, 288)


def panel_weather(cfg, days, conn_type=None, cell_signal=None):
    img, d = new_canvas()
    draw_header(d, "WEATHER", ACCENT["weather"], conn_type, cell_signal)
    d.text((16, 44), f"{cfg['weather_city']}  ›", font=font("default_medium", 16), fill=FG)

    if not days:
        centered_text(d, W / 2, 150, "no data yet", font("default_medium", 14), DIM)
        draw_page_dots(d, 3)
        return img

    day_labels = weather_day_labels(days)

    col_w = (W - 32) / 3
    for i, day in enumerate(days[:3]):
        cx = 16 + col_w * i + col_w / 2
        label, icon_key = wmo_info(day["code"])
        d.text((cx - col_w / 2 + 4, 76), day_labels[i], font=font("default_medium", 13), fill=DIM)
        draw_weather_icon(d, cx, 128, 22, icon_key)
        centered_text(d, cx, 168, f"{round(day['tmax'])}°", font("default_bold", 18), FG)
        centered_text(d, cx, 190, f"{round(day['tmin'])}°", font("default_medium", 14), DIM)
        if day.get("precip") is not None:
            centered_text(d, cx, 210, f"{day['precip']:.0f}%", font("default_medium", 11), ACCENT["weather"])

    d.line([16, 236, W - 16, 236], fill=(40, 44, 54))
    today_label, _ = wmo_info(days[0]["code"])
    centered_text(d, W / 2, 246, today_label, font("default_medium", 14), FG)

    bx0, by0, bx1, by1 = WEATHER_UPDATE_BUTTON
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=(by1 - by0) / 2, outline=ACCENT["weather"], width=2)
    centered_text_box(d, bx0, by0, bx1, by1, "Update Now", font("default_medium", 12), ACCENT["weather"])

    draw_page_dots(d, 3)
    return img


WEATHER_DETAIL_ROW_H = 34
WEATHER_DETAIL_ROWS_TOP = 178


def panel_weather_detail(cfg, day, aq, day_label):
    img, d = new_canvas()
    draw_back_header(d, day_label, ACCENT["weather"])

    try:
        wd = datetime.strptime(day["date"], "%Y-%m-%d").strftime("%a %d %b")
    except Exception:
        wd = day["date"]
    centered_text(d, W / 2, 42, wd, font("default_medium", 13), DIM)

    label, icon_key = wmo_info(day["code"])
    draw_weather_icon(d, W / 2, 96, 30, icon_key)
    centered_text(d, W / 2, 130, label, font("default_medium", 14), FG)
    centered_text(d, W / 2, 150, f"{round(day['tmax'])}° / {round(day['tmin'])}°", font("default_bold", 20), FG)

    d.line([16, WEATHER_DETAIL_ROWS_TOP - 10, W - 16, WEATHER_DETAIL_ROWS_TOP - 10], fill=(40, 44, 54))

    feels_max, feels_min = day.get("feels_max"), day.get("feels_min")
    feels_val = (f"{round(feels_max)}° / {round(feels_min)}°"
                 if feels_max is not None and feels_min is not None else "No data yet")
    uv = day.get("uv_max")
    uv_val = f"{round(uv)} · {uv_category(uv)}" if uv is not None else "No data yet"
    wind = day.get("wind_max")
    wind_val = f"{round(wind)} km/h" if wind is not None else "No data yet"
    aqi = (aq or {}).get("aqi")
    aqi_val = f"{round(aqi)} · {aqi_category(aqi)}" if aqi is not None else "No data yet"

    rows = [
        ("Feels Like", feels_val),
        ("UV Index", uv_val),
        ("Wind", wind_val),
        ("Air Quality", aqi_val),
    ]
    for i, (rlabel, rval) in enumerate(rows):
        y = WEATHER_DETAIL_ROWS_TOP + i * WEATHER_DETAIL_ROW_H
        d.text((16, y), rlabel, font=font("default_medium", 14), fill=DIM)
        val_color = FG if rval != "No data yet" else DIM
        val_font = font("default_medium", 14)
        vbbox = d.textbbox((0, 0), rval, font=val_font)
        d.text((W - 16 - (vbbox[2] - vbbox[0]), y), rval, font=val_font, fill=val_color)
        if i > 0:
            d.line([16, y - 8, W - 16, y - 8], fill=(28, 32, 42))

    return img


def panel_weather_picker(cfg, scroll_px=0):
    items = [(name, name) for name, _, _ in WEATHER_CITIES]
    return panel_scroll_picker("Weather City", ACCENT["weather"], items, cfg["weather_city"], scroll_px)


# ---------- confirm dialog (generic, reused for reboot / disconnect) ----------

CONFIRM_YES_RECT = (30, 190, 210, 226)
CONFIRM_NO_RECT = (30, 236, 210, 272)


def panel_confirm(title, message, accent, yes_label="Yes", danger=False):
    img, d = new_canvas()
    draw_back_header(d, title, accent)
    # centered_text draws a single line with no wrapping -- a message
    # longer than "Reboot the router now?" (the button was written and
    # tested against) ran off both edges of the 240px screen instead of
    # wrapping, e.g. "Hand the screen back to the GL.iNet UI?". Wrap it
    # and stack the lines centered in the space above the Yes/Cancel
    # buttons instead of assuming everything fits on one line.
    f_msg = font("default_medium", 15)
    lines = wrap_text_to_lines(d, message, f_msg, W - 32)
    line_h = 20
    y0 = 110 - (len(lines) - 1) * line_h / 2
    for i, line in enumerate(lines):
        centered_text(d, W / 2, y0 + i * line_h, line, f_msg, FG)

    yx0, yy0, yx1, yy1 = CONFIRM_YES_RECT
    yes_color = (200, 80, 80) if danger else accent
    d.rounded_rectangle([yx0, yy0, yx1, yy1], radius=8, fill=yes_color)
    centered_text(d, (yx0 + yx1) / 2, yy0 + 9, yes_label, font("default_bold", 14), BG)

    nx0, ny0, nx1, ny1 = CONFIRM_NO_RECT
    d.rounded_rectangle([nx0, ny0, nx1, ny1], radius=8, outline=DIM, width=2)
    centered_text(d, (nx0 + nx1) / 2, ny0 + 9, "Cancel", font("default_medium", 14), DIM)
    return img


def hit_confirm(x, y):
    yx0, yy0, yx1, yy1 = CONFIRM_YES_RECT
    if yx0 <= x <= yx1 and yy0 <= y <= yy1:
        return "yes"
    nx0, ny0, nx1, ny1 = CONFIRM_NO_RECT
    if nx0 <= x <= nx1 and ny0 <= y <= ny1:
        return "no"
    return None


# ---------- More / settings ----------

MORE_WIFI24_TOGGLE = (176, 37, 224, 63)
MORE_WIFI56_SEG = (16, 104, 224, 128)
MORE_CLOCK_STYLE_SEG = (16, 184, 224, 208)
MORE_RETURN_STOCK_RECT = (16, 232, W - 16, 266)
MORE_REBOOT_RECT = (16, 272, 116, 306)
MORE_SHUTDOWN_RECT = (124, 272, W - 16, 306)


def panel_more(wifi24, wifi_band, clock_style, wifi56_disabled_idx=None):
    img, d = new_canvas()
    draw_back_header(d, "More", ACCENT["clock"])

    d.text((16, 44), "2.4GHz WiFi", font=font("default_medium", 15), fill=FG)
    tx0, ty0, tx1, ty1 = MORE_WIFI24_TOGGLE
    draw_toggle(d, tx0, ty0, wifi24, ACCENT["clock"], w=tx1 - tx0, h=ty1 - ty0)

    d.text((16, 81), "5GHz/6GHz", font=font("default_medium", 15), fill=FG)
    sx0, sy0, sx1, sy1 = MORE_WIFI56_SEG
    band_idx = {"5g": 0, "off": 1, "6g": 2}[wifi_band]
    draw_segmented(d, sx0, sy0, sx1 - sx0, sy1 - sy0, ["5G", "Off", "6G"], band_idx, ACCENT["clock"])
    if wifi56_disabled_idx is not None and wifi56_disabled_idx != band_idx:
        seg_w = (sx1 - sx0) / 3
        label = ["5G", "Off", "6G"][wifi56_disabled_idx]
        f = font("default_medium", 14)
        bbox = d.textbbox((0, 0), label, font=f)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = sx0 + wifi56_disabled_idx * seg_w + (seg_w - tw) / 2
        ty = sy0 + ((sy1 - sy0) - th) / 2 - bbox[1]
        d.text((tx, ty), label, font=f, fill=(60, 64, 74))
        centered_text(d, (sx0 + sx1) / 2, sy1 + 6, "Matches repeater band", font("default_medium", 10), DIM)

    d.line([16, 150, W - 16, 150], fill=(40, 44, 54))

    d.text((16, 160), "Clock Style", font=font("default_medium", 15), fill=FG)
    sx0, sy0, sx1, sy1 = MORE_CLOCK_STYLE_SEG
    sel_idx = 0 if clock_style == "analog" else 1
    draw_segmented(d, sx0, sy0, sx1 - sx0, sy1 - sy0, ["Analog", "Digital"], sel_idx, ACCENT["clock"])

    d.line([16, 222, W - 16, 222], fill=(40, 44, 54))

    rx0, ry0, rx1, ry1 = MORE_RETURN_STOCK_RECT
    d.rounded_rectangle([rx0, ry0, rx1, ry1], radius=8, outline=ACCENT["clock"], width=2)
    centered_text(d, (rx0 + rx1) / 2, ry0 + 10, "Return to Stock UI", font("default_bold", 14), ACCENT["clock"])

    rx0, ry0, rx1, ry1 = MORE_REBOOT_RECT
    d.rounded_rectangle([rx0, ry0, rx1, ry1], radius=8, outline=(200, 80, 80), width=2)
    centered_text(d, (rx0 + rx1) / 2, ry0 + 10, "Reboot", font("default_bold", 14), (220, 100, 100))

    rx0, ry0, rx1, ry1 = MORE_SHUTDOWN_RECT
    d.rounded_rectangle([rx0, ry0, rx1, ry1], radius=8, outline=(200, 80, 80), width=2)
    centered_text(d, (rx0 + rx1) / 2, ry0 + 10, "Shutdown", font("default_bold", 14), (220, 100, 100))

    return img


def hit_more(x, y, wifi56_disabled_idx=None):
    tx0, ty0, tx1, ty1 = MORE_WIFI24_TOGGLE
    if tx0 - 10 <= x <= tx1 + 10 and ty0 - 8 <= y <= ty1 + 8:
        return "wifi24"
    sx0, sy0, sx1, sy1 = MORE_WIFI56_SEG
    if sx0 <= x <= sx1 and sy0 <= y <= sy1:
        seg_w = (sx1 - sx0) / 3
        idx = min(2, max(0, int((x - sx0) // seg_w)))
        if idx == wifi56_disabled_idx:
            return None
        return ["wifi_5g", "wifi_off", "wifi_6g"][idx]
    sx0, sy0, sx1, sy1 = MORE_CLOCK_STYLE_SEG
    if sx0 <= x <= sx1 and sy0 <= y <= sy1:
        return "clock_analog" if x < (sx0 + sx1) / 2 else "clock_digital"
    rx0, ry0, rx1, ry1 = MORE_RETURN_STOCK_RECT
    if rx0 <= x <= rx1 and ry0 <= y <= ry1:
        return "return_stock"
    rx0, ry0, rx1, ry1 = MORE_REBOOT_RECT
    if rx0 <= x <= rx1 and ry0 <= y <= ry1:
        return "reboot"
    rx0, ry0, rx1, ry1 = MORE_SHUTDOWN_RECT
    if rx0 <= x <= rx1 and ry0 <= y <= ry1:
        return "shutdown"
    return None


# ---------- Repeater ----------

REPEATER_LIST_TOP = 110
REPEATER_LIST_BOTTOM = 316
REPEATER_ROW_H = 34
REPEATER_DISCONNECT_ZONE = (34, 108)
CONNECT_POLL_INTERVAL = 1.0
CONNECT_TIMEOUT = 20.0
SPINNER_FRAME_INTERVAL = 0.1


def panel_repeater(rep, networks, scroll_px=0, connecting_ssid=None, spin_phase=0, error=None):
    img, d = new_canvas()
    draw_back_header(d, "Repeater", ACCENT["clock"])

    if connecting_ssid:
        draw_spinner(d, 26, 52, 10, spin_phase, ACCENT["clock"])
        d.text((46, 44), "Connecting…", font=font("default_medium", 13), fill=ACCENT["clock"])
        label = truncate_to_width(d, connecting_ssid, font("default_bold", 15), W - 62)
        d.text((46, 62), label, font=font("default_bold", 15), fill=FG)
    elif rep["connected"]:
        d.text((16, 44), "Connected", font=font("default_medium", 13), fill=ACCENT["clock"])
        d.text((16, 62), rep["ssid"], font=font("default_bold", 17), fill=FG)
        sub = f"{rep['ip'] or '—'}  ·  {rep['signal']} dBm" if rep["signal"] is not None else (rep["ip"] or "")
        d.text((16, 84), sub, font=font("default_medium", 12), fill=DIM)
        centered_text(d, W - 46, 50, "Disconnect", font("default_medium", 11), (220, 120, 120))
    elif error:
        # A failed join used to be completely silent: the spinner just
        # stopped after CONNECT_TIMEOUT and the list came back, so a wrong
        # password looked exactly like a weak signal or a mistap.
        d.text((16, 44), "Couldn’t connect", font=font("default_medium", 15), fill=(230, 130, 130))
        d.text((16, 64), error, font=font("default_medium", 11), fill=DIM)
    else:
        d.text((16, 44), "Not connected", font=font("default_medium", 15), fill=DIM)
        d.text((16, 64), "Tap a network below to connect", font=font("default_medium", 11), fill=DIM)

    d.line([16, REPEATER_LIST_TOP - 4, W - 16, REPEATER_LIST_TOP - 4], fill=(40, 44, 54))

    if not networks:
        centered_text(d, W / 2, 160, "Scanning…", font("default_medium", 13), DIM)
        return img

    # Scrollable list (same mechanics as panel_scroll_picker -- render into
    # an off-canvas strip, clip rows outside the visible window, composite
    # in, draw a scrollbar thumb if the content overflows) rather than the
    # old fixed-row fit-to-screen approach, which silently dropped any
    # networks past whatever fit in the available height.
    list_h = REPEATER_LIST_BOTTOM - REPEATER_LIST_TOP
    list_img = Image.new("RGB", (W, list_h), BG)
    ld = ImageDraw.Draw(list_img)
    for i, ap in enumerate(networks):
        y0 = i * REPEATER_ROW_H - scroll_px
        if y0 + REPEATER_ROW_H < 0 or y0 > list_h:
            continue
        label = truncate_to_width(ld, ap["ssid"], font("default_medium", 14), 150)
        ld.text((20, y0 + 8), label, font=font("default_medium", 14), fill=FG)
        if ap["open"]:
            ld.text((W - 46, y0 + 9), "open", font=font("default_medium", 11), fill=DIM)
        else:
            _icon_lock(ld, W - 28, y0 + REPEATER_ROW_H / 2, 7, DIM)
        if i > 0:
            ld.line([16, y0, W - 16, y0], fill=(26, 30, 40))
    img.paste(list_img, (0, REPEATER_LIST_TOP))

    content_h = len(networks) * REPEATER_ROW_H
    max_scroll = max(0, content_h - list_h)
    if max_scroll > 0:
        thumb_h = max(20, list_h * list_h / content_h)
        thumb_y = REPEATER_LIST_TOP + (scroll_px / max_scroll) * (list_h - thumb_h)
        d.rectangle([W - 6, thumb_y, W - 2, thumb_y + thumb_h], fill=(70, 76, 90))
    return img


def truncate_ssid(ssid, limit=18):
    ssid = ssid or ""
    return ssid if len(ssid) <= limit else ssid[:limit - 1] + "…"


def repeater_scroll_max(n_networks):
    return max(0, n_networks * REPEATER_ROW_H - (REPEATER_LIST_BOTTOM - REPEATER_LIST_TOP))


def hit_repeater(x, y, rep, n_networks, scroll_px=0):
    if rep["connected"] and REPEATER_DISCONNECT_ZONE[0] <= y < REPEATER_DISCONNECT_ZONE[1]:
        return ("disconnect", None)
    if REPEATER_LIST_TOP <= y < REPEATER_LIST_BOTTOM:
        idx = int((y - REPEATER_LIST_TOP + scroll_px) / REPEATER_ROW_H)
        if 0 <= idx < n_networks:
            return ("select", idx)
    return (None, None)


# ---------- SMS ----------

SMS_ROW_H = 46
SMS_LIST_TOP = 38
SMS_LIST_BOTTOM = 316


def panel_sms(messages, scroll_px=0):
    img, d = new_canvas()
    draw_back_header(d, "Messages", ACCENT["clock"])

    if not messages:
        centered_text(d, W / 2, 160, "No messages", font("default_medium", 14), DIM)
        return img

    list_h = SMS_LIST_BOTTOM - SMS_LIST_TOP
    list_img = Image.new("RGB", (W, list_h), BG)
    ld = ImageDraw.Draw(list_img)
    f_sender = font("default_bold", 14)
    f_time = font("default_medium", 11)
    f_body = font("default_cn_medium", 12)
    for i, msg in enumerate(messages):
        y0 = i * SMS_ROW_H - scroll_px
        if y0 + SMS_ROW_H < 0 or y0 > list_h:
            continue
        ld.text((16, y0 + 6), msg["from"], font=f_sender, fill=FG)
        when = msg["sent"].strftime("%d %b %H:%M") if msg["sent"] else ""
        wbbox = ld.textbbox((0, 0), when, font=f_time)
        ld.text((W - 16 - (wbbox[2] - wbbox[0]), y0 + 9), when, font=f_time, fill=DIM)
        preview = truncate_to_width(ld, msg["body"].replace("\n", " "), f_body, W - 32)
        ld.text((16, y0 + 27), preview, font=f_body, fill=DIM)
        if i > 0:
            ld.line([16, y0, W - 16, y0], fill=(28, 32, 42))
    img.paste(list_img, (0, SMS_LIST_TOP))

    content_h = len(messages) * SMS_ROW_H
    max_scroll = max(0, content_h - list_h)
    if max_scroll > 0:
        thumb_h = max(20, list_h * list_h / content_h)
        thumb_y = SMS_LIST_TOP + (scroll_px / max_scroll) * (list_h - thumb_h)
        d.rectangle([W - 6, thumb_y, W - 2, thumb_y + thumb_h], fill=(70, 76, 90))
    return img


def sms_scroll_max(n_messages):
    return max(0, n_messages * SMS_ROW_H - (SMS_LIST_BOTTOM - SMS_LIST_TOP))


def hit_sms(y, n_messages, scroll_px=0):
    if not (SMS_LIST_TOP <= y < SMS_LIST_BOTTOM):
        return None
    idx = int((y - SMS_LIST_TOP + scroll_px) / SMS_ROW_H)
    return idx if 0 <= idx < n_messages else None


def panel_sms_detail(msg):
    img, d = new_canvas()
    draw_back_header(d, msg["from"], ACCENT["clock"])
    when = msg["sent"].strftime("%a %d %b %Y, %H:%M") if msg["sent"] else msg.get("sent_raw", "")
    centered_text(d, W / 2, 40, when, font("default_medium", 12), DIM)
    d.line([16, 62, W - 16, 62], fill=(40, 44, 54))

    f = font("default_cn_medium", 15)
    y = 74
    for line in wrap_text_to_lines(d, msg["body"], f, W - 32):
        if y > H - 22:
            break
        d.text((16, y), line, font=f, fill=FG)
        y += 22
    return img


# ---------- WireGuard ----------

WIREGUARD_ROW_H = 44
WIREGUARD_LIST_TOP = 44
WIREGUARD_TOGGLE_W, WIREGUARD_TOGGLE_H = 44, 24


def panel_wireguard(peers, active_id):
    img, d = new_canvas()
    draw_back_header(d, "WireGuard", ACCENT["sim"])
    if not peers:
        centered_text(d, W / 2, 140, "No WireGuard configs", font("default_medium", 14), DIM)
        centered_text(d, W / 2, 164, "add one in LuCI first", font("default_medium", 13), DIM)
        return img
    for i, peer in enumerate(peers):
        y0 = WIREGUARD_LIST_TOP + i * WIREGUARD_ROW_H
        is_on = peer["id"] == active_id
        name = truncate_to_width(d, peer["name"], font("default_medium", 16), W - 32 - WIREGUARD_TOGGLE_W)
        d.text((16, y0 + (WIREGUARD_ROW_H - 20) / 2), name, font=font("default_medium", 16), fill=FG)
        tx0 = W - 16 - WIREGUARD_TOGGLE_W
        ty0 = y0 + (WIREGUARD_ROW_H - WIREGUARD_TOGGLE_H) / 2
        draw_toggle(d, tx0, ty0, is_on, ACCENT["sim"], w=WIREGUARD_TOGGLE_W, h=WIREGUARD_TOGGLE_H)
        if i > 0:
            d.line([16, y0, W - 16, y0], fill=(28, 32, 42))
    return img


def hit_wireguard(y, n_peers):
    if not (WIREGUARD_LIST_TOP <= y < WIREGUARD_LIST_TOP + n_peers * WIREGUARD_ROW_H):
        return None
    idx = int((y - WIREGUARD_LIST_TOP) / WIREGUARD_ROW_H)
    return idx if 0 <= idx < n_peers else None


# ---------- Games ----------
#
# Reached by tapping into the "games" main panel (7th carousel page, a
# hub listing all four titles with best scores) -- NOT swipeable
# themselves. Each game is its own `view == "game"` sub-screen (same
# family as "more"/"repeater"/"sms": it owns touch completely while
# active) so none of them fight the main carousel's own swipe-to-page
# gesture, which several of these games would otherwise collide with
# (2048's swipe-to-merge most obviously, but even Snake's directional
# input sits close enough to a swipe to be ambiguous).
#
# All four share one on-screen contract: a persistent Exit pill in the
# chrome bar (top-right, same spot in every game) always returns to the
# hub -- tapping it never needs to be preceded by finding a header/back
# gesture first, and it's the same gesture regardless of which game is
# running or what state it's in (playing, paused, game over).
#
# Frame budget: measured live on this hardware -- new_canvas + a handful
# of shapes/text + RGB565 convert + fb0 write costs ~23ms/frame (~44fps
# ceiling). All continuous games run at 25fps (40ms), leaving comfortable
# headroom; Snake only needs to redraw on its own ~130ms movement step,
# since nothing changes on screen between steps.

GAME_NAMES = ["snake", "flappy", "breakout", "twenty48"]
GAME_LABELS = {"snake": "Snake", "flappy": "Flappy", "breakout": "Breakout", "twenty48": "2048"}

GAME_TICK_S = 0.04          # 25fps for the continuously-animated games
SNAKE_STEP_S = 0.13         # grid movement interval

GAME_EXIT_RECT = (170, 4, 234, 30)
GAMES_HUB_TOP = 48
GAMES_HUB_ROW_H = 58


def draw_game_chrome(d, title, accent, score_text=None):
    """Shared top bar for every game: title, optional score/best readout,
    and the persistent Exit pill. Deliberately the same 34px height as
    every other panel's header (CLOCK_LEFT_ZONE and friends all start
    their content at y=34) so a game screen doesn't look out of place
    next to the rest of the dashboard."""
    d.rectangle([0, 0, W, 34], fill=(16, 22, 34))
    d.line([0, 34, W, 34], fill=(40, 44, 54))
    f_title = font("default_bold", 16)
    d.text((12, 8), title, font=f_title, fill=accent)
    if score_text:
        # Anchored right after the title rather than right-aligned at a
        # fixed x: a fixed anchor overlapped the title whenever the game
        # name was long (e.g. "Breakout"), since the score's start point
        # depended only on ITS OWN width, not on where the title ended.
        # Falls back to a smaller font if the combination still wouldn't
        # clear the Exit pill (safety net for any future game name).
        title_w = d.textbbox((0, 0), title, font=f_title)[2]
        x0 = 12 + title_w + 8
        f = font("default_medium", 12)
        sw = d.textbbox((0, 0), score_text, font=f)[2]
        if x0 + sw > GAME_EXIT_RECT[0] - 6:
            f = font("default_medium", 10)
        d.text((x0, 11), score_text, font=f, fill=FG)
    ex0, ey0, ex1, ey1 = GAME_EXIT_RECT
    d.rounded_rectangle([ex0, ey0, ex1, ey1], radius=9, fill=(70, 26, 32))
    centered_text_box(d, ex0, ey0, ex1, ey1, "Exit", font("default_bold", 12), (255, 195, 195))


def hit_game_exit(x, y):
    ex0, ey0, ex1, ey1 = GAME_EXIT_RECT
    return ex0 <= x <= ex1 and ey0 <= y <= ey1


def draw_game_message(d, lines, sub=None):
    """Centered card for "tap to start" / "game over" -- reused by all
    four games instead of each hand-rolling its own overlay."""
    cx, cy = W / 2, 34 + (H - 34) / 2
    card_w = 208
    card_h = 64 + len(lines) * 22 + (20 if sub else 0)
    x0, y0 = cx - card_w / 2, cy - card_h / 2
    d.rounded_rectangle([x0, y0, x0 + card_w, y0 + card_h], radius=14,
                        fill=(20, 26, 40), outline=(52, 62, 86))
    f = font("default_bold", 16)
    top = y0 + 20
    for i, line in enumerate(lines):
        centered_text(d, cx, top + i * 22, line, f, FG)
    if sub:
        centered_text(d, cx, y0 + card_h - 26, sub, font("default_medium", 12), DIM)


def panel_games(scores):
    img, d = new_canvas()
    draw_header(d, "GAMES", ACCENT["games"])
    for i, key in enumerate(GAME_NAMES):
        y0 = GAMES_HUB_TOP + i * GAMES_HUB_ROW_H
        y1 = y0 + GAMES_HUB_ROW_H - 10
        d.rounded_rectangle([12, y0, W - 12, y1], radius=10, outline=(48, 58, 82), width=1)
        d.text((24, y0 + 13), GAME_LABELS[key], font=font("default_bold", 16), fill=FG)
        best = scores.get(key, 0)
        d.text((24, y0 + 33), "Best: %s" % best, font=font("default_medium", 12), fill=DIM)
        centered_text(d, W - 28, y0 + (y1 - y0) / 2 - 8, "\u203a", font("default_bold", 20), DIM)
    draw_page_dots(d, 6)
    return img


def hit_main_games(x, y):
    for i, key in enumerate(GAME_NAMES):
        y0 = GAMES_HUB_TOP + i * GAMES_HUB_ROW_H
        y1 = y0 + GAMES_HUB_ROW_H - 10
        if 12 <= x <= W - 12 and y0 <= y <= y1:
            return "play:" + key
    return None


def new_game_state(key):
    return {
        "snake": new_snake_state,
        "flappy": new_flappy_state,
        "breakout": new_breakout_state,
        "twenty48": new_2048_state,
    }[key]()


# ---- Snake ----
# Grid movement, direction chosen by tapping anywhere in the play area:
# whichever axis the tap sits further from center on wins (tap in the
# upper strip -> up, right strip -> right, etc). No fixed quadrant
# rectangles to tune -- this scales cleanly to the actual play area and
# never has a dead zone.

SNAKE_CELL = 12
SNAKE_COLS = W // SNAKE_CELL                    # 20
SNAKE_PLAY_TOP = 34
SNAKE_ROWS = (H - SNAKE_PLAY_TOP) // SNAKE_CELL  # 23


def new_snake_state():
    cx, cy = SNAKE_COLS // 2, SNAKE_ROWS // 2
    body = [(cx - 1, cy), (cx - 2, cy), (cx - 3, cy)]
    state = {"body": body, "dir": (1, 0), "pending_dir": (1, 0), "over": False,
             "score": 0, "last_step": 0.0}
    state["food"] = _snake_spawn_food(body)
    return state


def _snake_spawn_food(body):
    occupied = set(body)
    free = [(c, r) for c in range(SNAKE_COLS) for r in range(SNAKE_ROWS) if (c, r) not in occupied]
    return random.choice(free) if free else None


def snake_set_direction(state, tap_x, tap_y):
    if state["over"]:
        return
    cx, cy = W / 2, SNAKE_PLAY_TOP + (H - SNAKE_PLAY_TOP) / 2
    ddx, ddy = tap_x - cx, tap_y - cy
    want = (1, 0) if ddx > 0 else (-1, 0)
    if abs(ddy) > abs(ddx):
        want = (0, 1) if ddy > 0 else (0, -1)
    cur = state["dir"]
    if (want[0], want[1]) == (-cur[0], -cur[1]):
        return  # can't reverse directly into yourself
    state["pending_dir"] = want


def snake_step(state, scores):
    body = state["body"]
    state["dir"] = state["pending_dir"]
    hc, hr = body[0]
    dc, dr = state["dir"]
    nc, nr = hc + dc, hr + dr
    if not (0 <= nc < SNAKE_COLS and 0 <= nr < SNAKE_ROWS) or (nc, nr) in body:
        state["over"] = True
        if state["score"] > scores.get("snake", 0):
            scores["snake"] = state["score"]
            save_game_scores(scores)
        return
    body.insert(0, (nc, nr))
    if state["food"] is not None and (nc, nr) == state["food"]:
        state["score"] += 1
        state["food"] = _snake_spawn_food(body)
    else:
        body.pop()


def draw_snake(state, scores):
    img, d = new_canvas()
    best = scores.get("snake", 0)
    draw_game_chrome(d, "Snake", ACCENT["games"], "%d  (best %d)" % (state["score"], best))
    for i, (c, r) in enumerate(state["body"]):
        x0, y0 = c * SNAKE_CELL, SNAKE_PLAY_TOP + r * SNAKE_CELL
        color = ACCENT["games"] if i == 0 else (150, 170, 90)
        d.rounded_rectangle([x0 + 1, y0 + 1, x0 + SNAKE_CELL - 1, y0 + SNAKE_CELL - 1],
                            radius=3, fill=color)
    if state["food"] is not None:
        fc, fr = state["food"]
        x0, y0 = fc * SNAKE_CELL, SNAKE_PLAY_TOP + fr * SNAKE_CELL
        d.ellipse([x0 + 2, y0 + 2, x0 + SNAKE_CELL - 2, y0 + SNAKE_CELL - 2], fill=(230, 90, 90))
    if state["over"]:
        draw_game_message(d, ["Game Over"], "Tap to restart")
    return img


def tick_snake(state, now, scores, tap, tap_x, tap_y):
    if state["over"]:
        if tap:
            state = new_snake_state()
        return state, False
    if tap:
        snake_set_direction(state, tap_x, tap_y)
    stepped = False
    if now - state["last_step"] >= SNAKE_STEP_S:
        state["last_step"] = now
        snake_step(state, scores)
        stepped = True
    return state, stepped


# ---- Flappy ----

FLAPPY_BIRD_X = 60
FLAPPY_GRAVITY = 640.0
FLAPPY_FLAP_VY = -230.0
FLAPPY_PIPE_SPEED = 85.0
FLAPPY_PIPE_GAP = 100
FLAPPY_PIPE_W = 32
FLAPPY_PIPE_SPACING = 150
FLAPPY_PLAY_TOP = 34
FLAPPY_BIRD_R = 9


def new_flappy_state():
    return {"bird_y": H / 2, "bird_vy": 0.0, "pipes": [], "score": 0,
            "started": False, "over": False}


def _flappy_spawn_pipe(state, x):
    top = random.randint(FLAPPY_PLAY_TOP + 30, H - 30 - FLAPPY_PIPE_GAP)
    state["pipes"].append({"x": x, "gap_y": top, "passed": False})


def tick_flappy(state, dt, scores, tap):
    if state["over"]:
        if tap:
            state = new_flappy_state()
        return state
    if tap:
        state["started"] = True
        state["bird_vy"] = FLAPPY_FLAP_VY
    if not state["started"]:
        return state

    state["bird_vy"] += FLAPPY_GRAVITY * dt
    state["bird_y"] += state["bird_vy"] * dt

    for p in state["pipes"]:
        p["x"] -= FLAPPY_PIPE_SPEED * dt
    state["pipes"] = [p for p in state["pipes"] if p["x"] + FLAPPY_PIPE_W > 0]
    if not state["pipes"] or state["pipes"][-1]["x"] < W - FLAPPY_PIPE_SPACING:
        _flappy_spawn_pipe(state, W)

    bird_top, bird_bot = state["bird_y"] - FLAPPY_BIRD_R, state["bird_y"] + FLAPPY_BIRD_R
    hit = bird_top <= FLAPPY_PLAY_TOP or bird_bot >= H
    for p in state["pipes"]:
        if p["x"] < FLAPPY_BIRD_X + FLAPPY_BIRD_R and p["x"] + FLAPPY_PIPE_W > FLAPPY_BIRD_X - FLAPPY_BIRD_R:
            if bird_top < p["gap_y"] or bird_bot > p["gap_y"] + FLAPPY_PIPE_GAP:
                hit = True
        if not p["passed"] and p["x"] + FLAPPY_PIPE_W < FLAPPY_BIRD_X:
            p["passed"] = True
            state["score"] += 1
    if hit:
        state["over"] = True
        if state["score"] > scores.get("flappy", 0):
            scores["flappy"] = state["score"]
            save_game_scores(scores)
    return state


def draw_flappy(state, scores):
    img, d = new_canvas()
    best = scores.get("flappy", 0)
    draw_game_chrome(d, "Flappy", ACCENT["games"], "%d  (best %d)" % (state["score"], best))
    for p in state["pipes"]:
        d.rectangle([p["x"], FLAPPY_PLAY_TOP, p["x"] + FLAPPY_PIPE_W, p["gap_y"]], fill=(90, 200, 130))
        d.rectangle([p["x"], p["gap_y"] + FLAPPY_PIPE_GAP, p["x"] + FLAPPY_PIPE_W, H], fill=(90, 200, 130))
    bx, by = FLAPPY_BIRD_X, state["bird_y"]
    d.ellipse([bx - FLAPPY_BIRD_R, by - FLAPPY_BIRD_R, bx + FLAPPY_BIRD_R, by + FLAPPY_BIRD_R],
              fill=(255, 205, 80))
    if not state["started"]:
        draw_game_message(d, ["Flappy"], "Tap to start")
    elif state["over"]:
        draw_game_message(d, ["Game Over"], "Tap to restart")
    return img


# ---- Breakout ----

BREAKOUT_PADDLE_W = 50
BREAKOUT_PADDLE_H = 8
BREAKOUT_PADDLE_Y = 300
BREAKOUT_BALL_R = 5
BREAKOUT_ROWS, BREAKOUT_COLS = 4, 6
BREAKOUT_BRICK_TOP = 44
BREAKOUT_BRICK_H = 14
BREAKOUT_BRICK_GAP = 3
BREAKOUT_MARGIN = 10
BREAKOUT_BRICK_W = (W - 2 * BREAKOUT_MARGIN - (BREAKOUT_COLS - 1) * BREAKOUT_BRICK_GAP) / BREAKOUT_COLS
BREAKOUT_BALL_SPEED = 160.0
_BREAKOUT_BRICK_COLORS = [(230, 110, 110), (230, 170, 90), (210, 210, 100), (120, 200, 140)]


def _breakout_fresh_bricks():
    return [[True] * BREAKOUT_COLS for _ in range(BREAKOUT_ROWS)]


def _breakout_serve(state):
    state["ball_x"] = state["paddle_x"]
    state["ball_y"] = BREAKOUT_PADDLE_Y - BREAKOUT_BALL_R - 1
    state["ball_vx"] = BREAKOUT_BALL_SPEED * 0.4
    state["ball_vy"] = -BREAKOUT_BALL_SPEED
    state["started"] = False


def new_breakout_state():
    state = {"paddle_x": W / 2, "bricks": _breakout_fresh_bricks(), "lives": 3,
             "score": 0, "over": False, "started": False}
    _breakout_serve(state)
    return state


def tick_breakout(state, dt, scores, tap, drag_x):
    if state["over"]:
        if tap:
            state = new_breakout_state()
        return state
    if drag_x is not None:
        state["paddle_x"] = max(BREAKOUT_PADDLE_W / 2, min(W - BREAKOUT_PADDLE_W / 2, drag_x))
        if not state["started"]:
            state["ball_x"] = state["paddle_x"]
    if tap and not state["started"]:
        state["started"] = True
    if not state["started"]:
        return state

    state["ball_x"] += state["ball_vx"] * dt
    state["ball_y"] += state["ball_vy"] * dt

    if state["ball_x"] <= BREAKOUT_BALL_R:
        state["ball_x"] = BREAKOUT_BALL_R
        state["ball_vx"] = abs(state["ball_vx"])
    elif state["ball_x"] >= W - BREAKOUT_BALL_R:
        state["ball_x"] = W - BREAKOUT_BALL_R
        state["ball_vx"] = -abs(state["ball_vx"])
    if state["ball_y"] <= FLAPPY_PLAY_TOP + BREAKOUT_BALL_R:
        state["ball_y"] = FLAPPY_PLAY_TOP + BREAKOUT_BALL_R
        state["ball_vy"] = abs(state["ball_vy"])

    px, py = state["paddle_x"], BREAKOUT_PADDLE_Y
    # The upper bound (ball hasn't already passed the paddle's bottom
    # edge) matters: without it, a ball moving downward is still counted
    # as "hitting" the paddle no matter how far below it has already
    # fallen, since ball_y+R >= py stays true forever once the ball is
    # past that line. A single unusually large dt (or an intentionally
    # extreme test) could let the ball tunnel yards past the paddle in
    # one step and still bounce back up as if nothing happened, instead
    # of correctly falling through and costing a life.
    if (state["ball_vy"] > 0 and state["ball_y"] + BREAKOUT_BALL_R >= py
            and state["ball_y"] - BREAKOUT_BALL_R <= py + BREAKOUT_PADDLE_H
            and abs(state["ball_x"] - px) <= BREAKOUT_PADDLE_W / 2 + BREAKOUT_BALL_R):
        state["ball_y"] = py - BREAKOUT_BALL_R
        # Clamp to [-1, 1]: the hit test allows a small margin
        # (PADDLE_W/2 + BALL_R) beyond the paddle's own half-width, so an
        # edge hit can push |offset| slightly past 1 -- and speed**2 -
        # vx**2 going negative turns ball_vy into a *complex* number in
        # Python ((-1)**0.5 doesn't raise, it returns 1.2e-16+1j), not an
        # exception, silently poisoning every physics update after it.
        offset = max(-1.0, min(1.0, (state["ball_x"] - px) / (BREAKOUT_PADDLE_W / 2)))
        speed = (state["ball_vx"] ** 2 + state["ball_vy"] ** 2) ** 0.5
        state["ball_vx"] = offset * speed
        state["ball_vy"] = -max(0.0, speed ** 2 - state["ball_vx"] ** 2) ** 0.5

    bx0, by0 = state["ball_x"] - BREAKOUT_BALL_R, state["ball_y"] - BREAKOUT_BALL_R
    bx1, by1 = state["ball_x"] + BREAKOUT_BALL_R, state["ball_y"] + BREAKOUT_BALL_R
    hit_any = False
    for row in range(BREAKOUT_ROWS):
        for col in range(BREAKOUT_COLS):
            if not state["bricks"][row][col]:
                continue
            rx0 = BREAKOUT_MARGIN + col * (BREAKOUT_BRICK_W + BREAKOUT_BRICK_GAP)
            ry0 = BREAKOUT_BRICK_TOP + row * (BREAKOUT_BRICK_H + BREAKOUT_BRICK_GAP)
            rx1, ry1 = rx0 + BREAKOUT_BRICK_W, ry0 + BREAKOUT_BRICK_H
            if bx1 >= rx0 and bx0 <= rx1 and by1 >= ry0 and by0 <= ry1:
                state["bricks"][row][col] = False
                state["score"] += 10
                state["ball_vy"] = -state["ball_vy"]
                hit_any = True
                break
        if hit_any:
            break
    if hit_any and not any(any(r) for r in state["bricks"]):
        state["bricks"] = _breakout_fresh_bricks()
        state["ball_vx"] *= 1.08
        state["ball_vy"] *= 1.08
        _breakout_serve(state)

    if state["ball_y"] > H:
        state["lives"] -= 1
        if state["lives"] <= 0:
            state["over"] = True
            if state["score"] > scores.get("breakout", 0):
                scores["breakout"] = state["score"]
                save_game_scores(scores)
        else:
            _breakout_serve(state)
    return state


def draw_breakout(state, scores):
    img, d = new_canvas()
    best = scores.get("breakout", 0)
    # Plain ASCII, not a heart glyph: same reasoning as the hand-drawn
    # flags/weather icons elsewhere in this file -- the bundled font has
    # no colour emoji, and an unsupported glyph silently renders as a
    # tofu box instead of failing loudly, so it's easy to ship unnoticed.
    draw_game_chrome(d, "Breakout", ACCENT["games"],
                     "%d  x%d  b%d" % (state["score"], state["lives"], best))
    for row in range(BREAKOUT_ROWS):
        for col in range(BREAKOUT_COLS):
            if not state["bricks"][row][col]:
                continue
            rx0 = BREAKOUT_MARGIN + col * (BREAKOUT_BRICK_W + BREAKOUT_BRICK_GAP)
            ry0 = BREAKOUT_BRICK_TOP + row * (BREAKOUT_BRICK_H + BREAKOUT_BRICK_GAP)
            d.rectangle([rx0, ry0, rx0 + BREAKOUT_BRICK_W, ry0 + BREAKOUT_BRICK_H],
                       fill=_BREAKOUT_BRICK_COLORS[row % len(_BREAKOUT_BRICK_COLORS)])
    px = state["paddle_x"]
    d.rounded_rectangle([px - BREAKOUT_PADDLE_W / 2, BREAKOUT_PADDLE_Y,
                        px + BREAKOUT_PADDLE_W / 2, BREAKOUT_PADDLE_Y + BREAKOUT_PADDLE_H],
                        radius=3, fill=ACCENT["games"])
    bx, by = state["ball_x"], state["ball_y"]
    d.ellipse([bx - BREAKOUT_BALL_R, by - BREAKOUT_BALL_R, bx + BREAKOUT_BALL_R, by + BREAKOUT_BALL_R],
              fill=FG)
    if not state["started"]:
        draw_game_message(d, ["Breakout"], "Drag to aim, tap to launch")
    elif state["over"]:
        draw_game_message(d, ["Game Over"], "Tap to restart")
    return img


# ---- 2048 ----

G2048_COLS = 4
G2048_CELL = 48
G2048_GAP = 6
G2048_BOARD_W = G2048_COLS * G2048_CELL + (G2048_COLS - 1) * G2048_GAP
G2048_X0 = (W - G2048_BOARD_W) / 2
G2048_Y0 = 44
G2048_SWIPE_MIN_PX = 18
_G2048_TILE_COLORS = {
    0: (30, 36, 50), 2: (60, 68, 88), 4: (70, 84, 110), 8: (220, 150, 90),
    16: (225, 130, 80), 32: (230, 110, 80), 64: (235, 90, 70), 128: (220, 195, 100),
    256: (220, 195, 80), 512: (220, 190, 60), 1024: (220, 180, 40), 2048: (240, 200, 20),
}


def new_2048_state():
    grid = [[0] * G2048_COLS for _ in range(G2048_COLS)]
    _g2048_spawn(grid)
    _g2048_spawn(grid)
    return {"grid": grid, "score": 0, "over": False}


def _g2048_spawn(grid):
    empties = [(r, c) for r in range(G2048_COLS) for c in range(G2048_COLS) if grid[r][c] == 0]
    if not empties:
        return
    r, c = random.choice(empties)
    grid[r][c] = 4 if random.random() < 0.1 else 2


def _g2048_compress_merge(row):
    vals = [v for v in row if v != 0]
    gained = 0
    out = []
    i = 0
    while i < len(vals):
        if i + 1 < len(vals) and vals[i] == vals[i + 1]:
            merged = vals[i] * 2
            out.append(merged)
            gained += merged
            i += 2
        else:
            out.append(vals[i])
            i += 1
    out += [0] * (G2048_COLS - len(out))
    moved = out != row
    return out, gained, moved


def _g2048_move(grid, direction):
    """direction: 'left'/'right'/'up'/'down'. Returns (new_grid, gained, moved)."""
    n = G2048_COLS
    if direction in ("left", "right"):
        rows = [list(r) for r in grid]
        if direction == "right":
            rows = [list(reversed(r)) for r in rows]
        gained, moved = 0, False
        new_rows = []
        for r in rows:
            nr, g, m = _g2048_compress_merge(r)
            new_rows.append(nr)
            gained += g
            moved = moved or m
        if direction == "right":
            new_rows = [list(reversed(r)) for r in new_rows]
        return new_rows, gained, moved
    else:
        cols = [[grid[r][c] for r in range(n)] for c in range(n)]
        if direction == "down":
            cols = [list(reversed(c)) for c in cols]
        gained, moved = 0, False
        new_cols = []
        for c in cols:
            nc, g, m = _g2048_compress_merge(c)
            new_cols.append(nc)
            gained += g
            moved = moved or m
        if direction == "down":
            new_cols = [list(reversed(c)) for c in new_cols]
        new_grid = [[new_cols[c][r] for c in range(n)] for r in range(n)]
        return new_grid, gained, moved


def _g2048_no_moves_left(grid):
    n = G2048_COLS
    for r in range(n):
        for c in range(n):
            if grid[r][c] == 0:
                return False
            if c + 1 < n and grid[r][c] == grid[r][c + 1]:
                return False
            if r + 1 < n and grid[r][c] == grid[r + 1][c]:
                return False
    return True


def g2048_swipe(state, dx, dy, scores):
    if state["over"]:
        return
    if abs(dx) < G2048_SWIPE_MIN_PX and abs(dy) < G2048_SWIPE_MIN_PX:
        return
    direction = ("right" if dx > 0 else "left") if abs(dx) > abs(dy) else \
                ("down" if dy > 0 else "up")
    new_grid, gained, moved = _g2048_move(state["grid"], direction)
    if not moved:
        return
    state["grid"] = new_grid
    state["score"] += gained
    _g2048_spawn(state["grid"])
    if state["score"] > scores.get("twenty48", 0):
        scores["twenty48"] = state["score"]
        save_game_scores(scores)
    if _g2048_no_moves_left(state["grid"]):
        state["over"] = True


def draw_2048(state, scores):
    img, d = new_canvas()
    best = scores.get("twenty48", 0)
    draw_game_chrome(d, "2048", ACCENT["games"], "%d  (best %d)" % (state["score"], best))
    for r in range(G2048_COLS):
        for c in range(G2048_COLS):
            v = state["grid"][r][c]
            x0 = G2048_X0 + c * (G2048_CELL + G2048_GAP)
            y0 = G2048_Y0 + r * (G2048_CELL + G2048_GAP)
            color = _G2048_TILE_COLORS.get(v, (240, 200, 20))
            d.rounded_rectangle([x0, y0, x0 + G2048_CELL, y0 + G2048_CELL], radius=6, fill=color)
            if v:
                fsize = 18 if v < 100 else (15 if v < 1000 else 12)
                fg = (60, 50, 30) if v >= 8 else FG
                centered_text_box(d, x0, y0, x0 + G2048_CELL, y0 + G2048_CELL,
                                  str(v), font("default_bold", fsize), fg)
    hint_y = G2048_Y0 + G2048_BOARD_W + 14
    centered_text(d, W / 2, hint_y, "Swipe to merge tiles", font("default_medium", 12), DIM)
    if state["over"]:
        draw_game_message(d, ["Game Over"], "Tap to restart")
    return img


def tick_2048(state, scores, tap, swipe, dx, dy):
    if state["over"]:
        if tap:
            state = new_2048_state()
        return state
    if swipe:
        g2048_swipe(state, dx, dy, scores)
    return state


# ---------- on-screen keyboard ----------

KB_ROW_Y0 = 76
KB_ROW_H = 32
KB_KEY_H = 28
KB_CAPS_RECT = (8, 176, 46, 208)
KB_LAYER_TOGGLE_RECT = (50, 176, 96, 208)
KB_SPACE_RECT = (100, 176, 188, 208)
KB_BACKSPACE_RECT = (192, 176, 232, 208)
KB_CANCEL_RECT = (16, 214, 116, 250)
KB_CONNECT_RECT = (124, 214, 224, 250)

KB_LETTER_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
KB_SYMBOL_ROWS = ["1234567890", "-_/:;()&@\"", ".,?!'~#$%^"]


def kb_rows(layer, caps):
    src = KB_LETTER_ROWS if layer == "letters" else KB_SYMBOL_ROWS
    rows = [r.upper() for r in src] if (layer == "letters" and caps) else list(src)
    return [list(r) for r in rows]


def layout_row(labels, y, key_w=23, key_h=KB_KEY_H, gap=1):
    total_w = len(labels) * key_w + (len(labels) - 1) * gap
    x0 = (W - total_w) / 2
    rects = []
    x = x0
    for lbl in labels:
        rects.append((lbl, x, y, x + key_w, y + key_h))
        x += key_w + gap
    return rects


def panel_keyboard(title, text, layer, caps, accent, connect_label="Connect"):
    img, d = new_canvas()
    draw_back_header(d, title, accent)

    d.rounded_rectangle([16, 38, W - 16, 68], radius=6, outline=(50, 55, 68), width=1)
    shown = text if len(text) <= 20 else "…" + text[-19:]
    d.text((22, 45), shown if shown else " ", font=font("default_mono_medium", 15), fill=FG)

    y = KB_ROW_Y0
    for row in kb_rows(layer, caps):
        for lbl, x0, y0, x1, y1 in layout_row(row, y):
            d.rounded_rectangle([x0, y0, x1, y1], radius=4, fill=(28, 32, 42))
            centered_text(d, (x0 + x1) / 2, y0 + (y1 - y0) / 2 - 7, lbl, font("default_medium", 13), FG)
        y += KB_ROW_H

    cx0, cy0, cx1, cy1 = KB_CAPS_RECT
    d.rounded_rectangle([cx0, cy0, cx1, cy1], radius=6, fill=(28, 32, 42))
    centered_text(d, (cx0 + cx1) / 2, cy0 + 9, "CAP", font("default_medium", 12), accent if caps else FG)

    lx0, ly0, lx1, ly1 = KB_LAYER_TOGGLE_RECT
    d.rounded_rectangle([lx0, ly0, lx1, ly1], radius=6, fill=(28, 32, 42))
    centered_text(d, (lx0 + lx1) / 2, ly0 + 9, "ABC" if layer == "symbols" else "123", font("default_medium", 12), accent)

    sx0, sy0, sx1, sy1 = KB_SPACE_RECT
    d.rounded_rectangle([sx0, sy0, sx1, sy1], radius=6, fill=(28, 32, 42))
    centered_text(d, (sx0 + sx1) / 2, sy0 + 9, "Space", font("default_medium", 13), FG)

    bx0, by0, bx1, by1 = KB_BACKSPACE_RECT
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=6, fill=(28, 32, 42))
    centered_text(d, (bx0 + bx1) / 2, by0 + 9, "DEL", font("default_medium", 12), FG)

    ax0, ay0, ax1, ay1 = KB_CANCEL_RECT
    d.rounded_rectangle([ax0, ay0, ax1, ay1], radius=8, outline=DIM, width=2)
    centered_text(d, (ax0 + ax1) / 2, ay0 + 10, "Cancel", font("default_medium", 14), DIM)

    gx0, gy0, gx1, gy1 = KB_CONNECT_RECT
    d.rounded_rectangle([gx0, gy0, gx1, gy1], radius=8, fill=accent)
    centered_text(d, (gx0 + gx1) / 2, gy0 + 10, connect_label, font("default_bold", 14), BG)
    return img


def hit_keyboard(x, y, layer, caps):
    ky = KB_ROW_Y0
    for row in kb_rows(layer, caps):
        for lbl, x0, y0, x1, y1 in layout_row(row, ky):
            if x0 <= x <= x1 and y0 <= y <= y1:
                return ("char", lbl)
        ky += KB_ROW_H

    cx0, cy0, cx1, cy1 = KB_CAPS_RECT
    if cx0 <= x <= cx1 and cy0 <= y <= cy1:
        return ("caps", None)
    lx0, ly0, lx1, ly1 = KB_LAYER_TOGGLE_RECT
    if lx0 <= x <= lx1 and ly0 <= y <= ly1:
        return ("layer_toggle", None)
    sx0, sy0, sx1, sy1 = KB_SPACE_RECT
    if sx0 <= x <= sx1 and sy0 <= y <= sy1:
        return ("char", " ")
    bx0, by0, bx1, by1 = KB_BACKSPACE_RECT
    if bx0 <= x <= bx1 and by0 <= y <= by1:
        return ("backspace", None)
    ax0, ay0, ax1, ay1 = KB_CANCEL_RECT
    if ax0 <= x <= ax1 and ay0 <= y <= ay1:
        return ("cancel", None)
    gx0, gy0, gx1, gy1 = KB_CONNECT_RECT
    if gx0 <= x <= gx1 and gy0 <= y <= gy1:
        return ("connect", None)
    return (None, None)


# ---------- sub-screens ----------

def panel_picker(title, accent, items, selected):
    img, d = new_canvas()
    draw_back_header(d, title, accent)
    n = len(items)
    row_h = (PICKER_BOTTOM - PICKER_TOP) / n
    fsize = 13 if row_h < 22 else 15
    f = font("default_medium", fsize)
    for i, (key, label) in enumerate(items):
        y0 = PICKER_TOP + i * row_h
        sel = key == selected
        if sel:
            d.rectangle([0, y0, W, y0 + row_h], fill=(28, 40, 56))
        bbox = d.textbbox((0, 0), label, font=f)
        th = bbox[3] - bbox[1]
        color = accent if sel else FG
        d.text((20, y0 + (row_h - th) / 2 - bbox[1]), label, font=f, fill=color)
        if sel:
            d.text((W - 30, y0 + (row_h - th) / 2 - bbox[1]), "✓", font=f, fill=accent)
        if i > 0:
            d.line([0, y0, W, y0], fill=(28, 32, 42))
    return img


SCROLL_ROW_H = 32
SCROLL_FONT_SIZE = 15


def panel_scroll_picker(title, accent, items, selected, scroll_px, font_name="default_medium"):
    """Like panel_picker but with a fixed, larger row height and a vertical
    scroll offset -- for lists too long to shrink-to-fit on one screen.
    `font_name` lets callers with non-Latin content (e.g. Chinese-named
    OpenClash nodes) swap in a CJK-capable font without affecting the
    other pickers that share this renderer."""
    img, d = new_canvas()
    draw_back_header(d, title, accent)

    list_h = PICKER_BOTTOM - PICKER_TOP
    list_img = Image.new("RGB", (W, list_h), BG)
    ld = ImageDraw.Draw(list_img)
    f = font(font_name, SCROLL_FONT_SIZE)
    check_f = font("default_medium", SCROLL_FONT_SIZE)  # CJK fonts may lack a ✓ glyph
    max_label_w = W - 56  # room for left margin + checkmark on the right
    for i, (key, label) in enumerate(items):
        y0 = i * SCROLL_ROW_H - scroll_px
        if y0 + SCROLL_ROW_H < 0 or y0 > list_h:
            continue
        sel = key == selected
        if sel:
            ld.rectangle([0, y0, W, y0 + SCROLL_ROW_H], fill=(28, 40, 56))
        label = truncate_to_width(ld, label, f, max_label_w)
        bbox = ld.textbbox((0, 0), label, font=f)
        th = bbox[3] - bbox[1]
        color = accent if sel else FG
        ld.text((20, y0 + (SCROLL_ROW_H - th) / 2 - bbox[1]), label, font=f, fill=color)
        if sel:
            cbbox = ld.textbbox((0, 0), "✓", font=check_f)
            cth = cbbox[3] - cbbox[1]
            ld.text((W - 30, y0 + (SCROLL_ROW_H - cth) / 2 - cbbox[1]), "✓", font=check_f, fill=accent)
        if i > 0:
            ld.line([0, y0, W, y0], fill=(28, 32, 42))
    img.paste(list_img, (0, PICKER_TOP))

    content_h = len(items) * SCROLL_ROW_H
    max_scroll = max(0, content_h - list_h)
    if max_scroll > 0:
        thumb_h = max(20, list_h * list_h / content_h)
        thumb_y = PICKER_TOP + (scroll_px / max_scroll) * (list_h - thumb_h)
        # plain rectangle, not rounded -- too thin (4px) for PIL's corner math,
        # which raises ValueError on some Pillow builds at this aspect ratio
        d.rectangle([W - 6, thumb_y, W - 2, thumb_y + thumb_h], fill=(70, 76, 90))
    return img


def scroll_picker_max(n_items):
    return max(0, n_items * SCROLL_ROW_H - (PICKER_BOTTOM - PICKER_TOP))


def hit_scroll_picker(y, n_items, scroll_px):
    if not (PICKER_TOP <= y < PICKER_BOTTOM):
        return None
    idx = int((y - PICKER_TOP + scroll_px) / SCROLL_ROW_H)
    return idx if 0 <= idx < n_items else None


def panel_city_picker(slot, cfg, scroll_px=0):
    selected = cfg["clock_top"] if slot == "top" else cfg["clock_bottom"]
    items = list(CITIES)
    label = "Top" if slot == "top" else "Bottom"
    return panel_scroll_picker(f"{label} City", ACCENT["clock"], items, selected, scroll_px)


def panel_currency_picker(row, side, cfg, scroll_px=0):
    cfg_key = f"fx_{row}_{side}"
    selected = cfg[cfg_key]
    items = [(c, f"{c} · {CURRENCY_NAMES[c]}") for c in CURRENCIES]
    row_label = "Top" if row == "top" else "Bottom"
    side_label = "From" if side == "from" else "To"
    return panel_scroll_picker(f"{row_label} — {side_label}", ACCENT["fx"], items, selected, scroll_px)


def panel_datacap_picker(cfg, scroll_px=0):
    selected = cfg.get("data_cap_mb")
    items = [(v, cap_label(v)) for v in DATA_CAP_PRESETS]
    return panel_scroll_picker("Data Cap", ACCENT["sim"], items, selected, scroll_px)


def panel_node_picker(traf, scroll_px=0):
    if not traf["running"]:
        img, d = new_canvas()
        draw_back_header(d, "Node", ACCENT["openclash"])
        centered_text(d, W / 2, 140, "OpenClash isn't running", font("default_medium", 14), DIM)
        return img
    if not traf["nodes"]:
        img, d = new_canvas()
        draw_back_header(d, "Node", ACCENT["openclash"])
        centered_text(d, W / 2, 130, "No subscription configured", font("default_medium", 13), DIM)
        centered_text(d, W / 2, 154, "add one in LuCI first", font("default_medium", 13), DIM)
        return img
    items = [(n, n) for n in traf["nodes"]]
    return panel_scroll_picker("Node", ACCENT["openclash"], items, traf["node_name"], scroll_px,
                                font_name="default_cn_medium")


def render_sub(view, cfg, oc, traf):
    if view == "city_top":
        return panel_city_picker("top", cfg)
    if view == "city_bottom":
        return panel_city_picker("bottom", cfg)
    if view == "datacap":
        return panel_datacap_picker(cfg)
    if view == "oc_nodes":
        return panel_node_picker(traf)
    if view == "weather_city":
        return panel_weather_picker(cfg)


# ---------- hit-testing ----------

def hit_main_clock(x, y):
    rx0, ry0, rx1, ry1 = REPEATER_TILE
    if rx0 <= x <= rx1 and ry0 <= y <= ry1:
        return "repeater"
    mx0, my0, mx1, my1 = MORE_TILE
    if mx0 <= x <= mx1 and my0 <= y <= my1:
        return "more"
    sx0, sy0, sx1, sy1 = SMS_TILE
    if sx0 <= x <= sx1 and sy0 <= y <= sy1:
        return "sms"
    lx0, ly0, lx1, ly1 = CLOCK_LEFT_ZONE
    if lx0 <= x < lx1 and ly0 <= y < ly1:
        return "city_left"
    rx0, ry0, rx1, ry1 = CLOCK_RIGHT_ZONE
    if rx0 <= x < rx1 and ry0 <= y < ry1:
        return "city_right"
    return None


def hit_main_fx(x, y):
    bx0, by0, bx1, by1 = FX_BUTTON
    if bx0 <= x <= bx1 and by0 <= y <= by1:
        return "update"
    rx0, ry0, rx1, ry1 = FX_RANGE_RECT
    if rx0 <= x <= rx1 and ry0 <= y <= ry1:
        seg_w = (rx1 - rx0) / len(FX_RANGES)
        idx = min(len(FX_RANGES) - 1, max(0, int((x - rx0) / seg_w)))
        return f"range:{FX_RANGES[idx]}"
    if FX_TOP_ZONE[0] <= y < FX_TOP_ZONE[1]:
        return "top_from" if x < W / 2 else "top_to"
    if FX_BOTTOM_ZONE[0] <= y < FX_BOTTOM_ZONE[1]:
        return "bottom_from" if x < W / 2 else "bottom_to"
    return None


def hit_main_sim(x, y):
    ax0, ay0, ax1, ay1 = SIM_ATTACH_TOGGLE_RECT
    dx0, dy0, dx1, dy1 = SIM_DATA_TOGGLE_RECT
    # Network and Data toggles sit side by side with their labels above
    # (y=44) -- treated as one combined tap region split at the midpoint
    # of the gap between them, same reasoning as the earlier fix: a label
    # separated from its toggle by a real gap needs the tap zone extended
    # up to cover it, not just tight padding around the toggle itself.
    if min(ax0, dx0) - 8 <= x <= max(ax1, dx1) + 8 and 39 <= y <= max(ay1, dy1) + 8:
        mid = (ax1 + dx0) / 2
        return "attach_toggle" if x < mid else "data_toggle"
    cx0, cy0, cx1, cy1 = SIM_CHOICE_RECT
    if cx0 - 6 <= x <= cx1 + 6 and cy0 - 6 <= y <= cy1 + 6:
        seg_w = (cx1 - cx0) / len(SIM_CHOICE_KEYS)
        idx = min(len(SIM_CHOICE_KEYS) - 1, max(0, int((x - cx0) / seg_w)))
        return f"choice:{SIM_CHOICE_KEYS[idx]}"
    tx0, ty0, tx1, ty1 = SIM_ROAM_TOGGLE_RECT
    if tx0 - 8 <= x <= tx1 + 8 and ty0 - 8 <= y <= ty1 + 8:
        return "roam_toggle"
    wx0, wy0, wx1, wy1 = SIM_WIREGUARD_TILE
    if wx0 <= x <= wx1 and wy0 <= y <= wy1:
        return "wireguard"
    if 156 <= y < wy0:
        return "data_cap"
    return None


def hit_main_openclash(x, y):
    tx0, ty0, tx1, ty1 = OC_TOGGLE_RECT
    if tx0 - 10 <= x <= tx1 + 10 and ty0 - 8 <= y <= ty1 + 8:
        return "toggle"
    sx0, sy0, sx1, sy1 = OC_MODE_SEG_RECT
    if sx0 <= x <= sx1 and sy0 <= y <= sy1:
        return "mode_global" if x < (sx0 + sx1) / 2 else "mode_rule"
    if OC_NODE_ZONE[0] <= y < OC_NODE_ZONE[1]:
        return "node"
    bx0, by0, bx1, by1 = OC_UPDATE_BUTTON
    if bx0 - 6 <= x <= bx1 + 6 and by0 - 6 <= y <= by1 + 6:
        return "update_sub"
    return None


def hit_main_weather(x, y):
    if WEATHER_CITY_ZONE[0] <= y < WEATHER_CITY_ZONE[1]:
        return "city"
    bx0, by0, bx1, by1 = WEATHER_UPDATE_BUTTON
    if bx0 - 6 <= x <= bx1 + 6 and by0 - 6 <= y <= by1 + 6:
        return "update"
    if WEATHER_DAY_ZONE[0] <= y < WEATHER_DAY_ZONE[1] and 16 <= x <= W - 16:
        col_w = (W - 32) / 3
        idx = int((x - 16) / col_w)
        if 0 <= idx < 3:
            return f"day:{idx}"
    return None


def hit_picker(y, n_items):
    if not (PICKER_TOP <= y < PICKER_BOTTOM):
        return None
    row_h = (PICKER_BOTTOM - PICKER_TOP) / n_items
    idx = int((y - PICKER_TOP) / row_h)
    return idx if 0 <= idx < n_items else None


def hit_back(y):
    return y < 34


def to_rgb565_bytes(img):
    arr = np.asarray(img.convert("RGB"), dtype=np.uint32)
    r = (arr[:, :, 0] >> 3) << 11
    g = (arr[:, :, 1] >> 2) << 5
    b = (arr[:, :, 2] >> 3)
    packed = (r | g | b).astype("<u2")
    return packed.tobytes()


_fb = None


def _fb_handle():
    """Keep /dev/fb0 open for the process lifetime instead of reopening it
    per frame. During a drag this runs ~80x/s; open+close per frame is
    pure syscall overhead in the one path where smoothness matters most.
    Falls back to per-frame opens if the handle ever goes bad."""
    global _fb
    if _fb is None:
        _fb = open(FB_PATH, "r+b", buffering=0)
    return _fb


def write_frame(img):
    data = to_rgb565_bytes(img)
    global _fb
    try:
        fb = _fb_handle()
        fb.seek(0)
        fb.write(data)
    except Exception:
        try:
            if _fb is not None:
                _fb.close()
        except Exception:
            pass
        _fb = None
        with open(FB_PATH, "r+b") as fb:
            fb.write(data)


# ---------- modes ----------

def mode_preview(outdir):
    os.makedirs(outdir, exist_ok=True)
    cfg = load_config()
    fx = fetch_fx()
    sim = get_sim_status(cfg)
    oc = get_openclash_status()
    traf = get_openclash_traffic_and_node()
    wx = fetch_weather(cfg["weather_city"])
    aq = fetch_air_quality(cfg["weather_city"])
    sms_messages = get_sms_messages()
    wg_peers = get_wireguard_peers()
    wg_active = get_wireguard_active()
    rep = get_repeater_status()
    rep_networks = repeater_scan()
    sysinfo = get_system_info()
    wifi24 = get_wifi_radio_state("wifi2g")
    wifi_band = get_wifi_band_state()
    net_sample, _, _ = sample_bandwidth(None)
    cpu_sample, _ = sample_cpu(None)
    time.sleep(1)
    net_sample, net_down, net_up = sample_bandwidth(net_sample)
    net_iface = net_sample[0] if net_sample else None
    _, cpu_pct = sample_cpu(cpu_sample)
    ram_pct, ram_used_gb, ram_total_gb = get_ram_stats()
    temp_c = get_temp_c()
    _cell_info = _get_active_cell_info()
    conn_type = get_wan_conn_type(_cell_info)
    cell_signal = get_cell_signal(_cell_info)
    cfg_digital = dict(cfg, clock_style="digital")
    screens = [
        ("clock", panel_clock(cfg, rep, conn_type, cell_signal, sms_messages)),
        ("clock_digital", panel_clock(cfg_digital, rep, conn_type, cell_signal, sms_messages)),
        ("sim", panel_sim(cfg, sim, conn_type, cell_signal, wg_peers, wg_active)),
        ("weather", panel_weather(cfg, wx, conn_type, cell_signal)),
        ("monitor", panel_monitor(net_down, net_up, net_iface, cpu_pct, ram_pct, ram_used_gb, ram_total_gb, temp_c, sysinfo["uptime_min"], conn_type, cell_signal)),
        ("fx", panel_fx(cfg, fx, "month", conn_type, cell_signal)),
        ("openclash", panel_openclash(oc, traf, conn_type, cell_signal)),
        ("city_top", panel_city_picker("top", cfg)),
        ("city_bottom", panel_city_picker("bottom", cfg)),
        ("fx_top_from", panel_currency_picker("top", "from", cfg)),
        ("fx_top_to", panel_currency_picker("top", "to", cfg)),
        ("fx_bottom_from", panel_currency_picker("bottom", "from", cfg)),
        ("fx_bottom_to", panel_currency_picker("bottom", "to", cfg)),
        ("datacap", panel_datacap_picker(cfg)),
        ("oc_nodes", panel_node_picker(traf)),
        ("weather_city", panel_weather_picker(cfg)),
        ("weather_detail", panel_weather_detail(cfg, wx[0], aq[0] if aq else None, weather_day_labels(wx)[0]) if wx else new_canvas()[0]),
        ("sms", panel_sms(sms_messages or _DEMO_SMS_MESSAGES)),
        ("sms_detail", panel_sms_detail((sms_messages or _DEMO_SMS_MESSAGES)[0])),
        ("wireguard", panel_wireguard(wg_peers, wg_active)),
        ("more", panel_more(wifi24, wifi_band, cfg["clock_style"], get_wifi56_conflict_idx(rep))),
        ("repeater", panel_repeater(rep, rep_networks)),
        ("confirm", panel_confirm("Reboot", "Reboot the router now?", ACCENT["clock"], yes_label="Reboot", danger=True)),
        ("confirm_long", panel_confirm("Stock UI", "Hand the screen back to the GL.iNet UI?", ACCENT["clock"], yes_label="Switch")),
        ("keyboard", panel_keyboard("Wi-Fi Password", "myPass", "letters", True, ACCENT["clock"])),
        ("games_hub", panel_games(load_game_scores())),
        ("game_snake", draw_snake(new_snake_state(), load_game_scores())),
        ("game_flappy", draw_flappy(new_flappy_state(), load_game_scores())),
        ("game_breakout", draw_breakout(new_breakout_state(), load_game_scores())),
        ("game_2048", draw_2048(new_2048_state(), load_game_scores())),
    ]
    cols, pad = 5, 10
    rows = (len(screens) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (W + pad) + pad, rows * (H + pad) + pad), (30, 30, 30))
    for i, (name, img) in enumerate(screens):
        img.save(os.path.join(outdir, f"panel_{name}.png"))
        r, c = divmod(i, cols)
        sheet.paste(img, (pad + c * (W + pad), pad + r * (H + pad)))
    sheet.save(os.path.join(outdir, "contact_sheet.png"))
    print(f"wrote {len(screens)} preview PNGs to {outdir}")


def mode_calibrate():
    for name, color in [("RED", (255, 0, 0)), ("GREEN", (0, 255, 0)), ("BLUE", (0, 0, 255))]:
        img = Image.new("RGB", (W, H), color)
        write_frame(img)
        print(f"showing {name}")
        time.sleep(2)


_stop = False


def _on_term(signum, frame):
    global _stop
    _stop = True


# ---------- touch input ----------
# chsc_cap_touch on /dev/input/event0, Multitouch protocol B, confirmed by
# live capture on 2026-07-25: ABS_MT_POSITION_X=53, ABS_MT_POSITION_Y=54,
# ABS_MT_TRACKING_ID=57 (-1 on lift), coordinates in raw screen pixels.
TOUCH_DEV = "/dev/input/event0"
_EVENT_FMT = "qqHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)
EV_ABS = 3
ABS_MT_SLOT = 47
ABS_MT_POSITION_X = 53
ABS_MT_POSITION_Y = 54
ABS_MT_TRACKING_ID = 57

TAP_JITTER_PX = 10

# Flick paging: a short, fast swipe should page even though it never
# travelled the 30% of screen width the old distance-only rule demanded.
# That rule is why quick flicks felt like they "didn't take" -- the panel
# sprang back despite an obviously deliberate gesture.
FLICK_VELOCITY_PX_S = 260.0
_VEL_SMOOTHING = 0.6


class TouchState:
    """Shared between the reader thread and the render loop."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = False
        self.dx = 0
        self.dy = 0
        self.down_x = 0
        self.down_y = 0
        self.have_pos = False   # a position event has arrived for THIS touch
        self.vx = 0.0           # smoothed horizontal velocity, px/s
        self.release_pending = False
        self.release_dx = 0
        self.release_dy = 0
        self.release_vx = 0.0
        self.error = None       # set if the input device can't be read


touch_state = TouchState()


def _touch_reader():
    import os as _os
    start_x = start_y = None
    last_x = None
    last_t = 0.0
    down = False
    slot = 0
    try:
        f = open(TOUCH_DEV, "rb")
    except Exception as e:
        # Losing the touch device leaves a dashboard that still redraws
        # happily but ignores every tap. That used to be a single line on
        # stderr nobody reads; surface it on the screen instead so the
        # device doesn't just look wedged.
        with touch_state.lock:
            touch_state.error = "touch input unavailable"
        print(f"touch reader could not open {TOUCH_DEV}: {e}", file=sys.stderr)
        return
    try:
        with f:
            fd = f.fileno()
            _os.set_blocking(fd, False)
            while not _stop:
                try:
                    data = f.read(_EVENT_SIZE)
                except (BlockingIOError, TypeError):
                    data = None
                if not data or len(data) < _EVENT_SIZE:
                    time.sleep(0.008)
                    continue
                _, _, typ, code, val = struct.unpack(_EVENT_FMT, data)
                if typ != EV_ABS:
                    continue
                # Multitouch protocol B interleaves slots. Without tracking
                # ABS_MT_SLOT, a second finger's coordinates were folded
                # into the same gesture, which on a screen this small made
                # an accidental two-finger touch jump panels.
                if code == ABS_MT_SLOT:
                    slot = val
                    continue
                if slot != 0:
                    continue
                if code == ABS_MT_TRACKING_ID:
                    if val == -1 and down:
                        with touch_state.lock:
                            touch_state.active = False
                            touch_state.release_pending = True
                            touch_state.release_dx = touch_state.dx
                            touch_state.release_dy = touch_state.dy
                            touch_state.release_vx = touch_state.vx
                        down = False
                        start_x = start_y = last_x = None
                    else:
                        down = True
                        start_x = start_y = last_x = None
                        with touch_state.lock:
                            touch_state.active = True
                            touch_state.dx = 0
                            touch_state.dy = 0
                            touch_state.vx = 0.0
                            # down_x/down_y still hold the PREVIOUS touch's
                            # coordinates until the first position event of
                            # this one lands; have_pos marks them stale so a
                            # release in that window can't be hit-tested
                            # against where the last tap happened to be.
                            touch_state.have_pos = False
                elif code == ABS_MT_POSITION_X and down:
                    now_t = time.time()
                    if start_x is None:
                        start_x = val
                        with touch_state.lock:
                            touch_state.down_x = val
                            touch_state.have_pos = True
                    elif last_x is not None and now_t > last_t:
                        inst = (val - last_x) / (now_t - last_t)
                        with touch_state.lock:
                            touch_state.vx = (_VEL_SMOOTHING * touch_state.vx
                                              + (1 - _VEL_SMOOTHING) * inst)
                    last_x, last_t = val, now_t
                    with touch_state.lock:
                        touch_state.dx = val - start_x
                elif code == ABS_MT_POSITION_Y and down:
                    if start_y is None:
                        start_y = val
                        with touch_state.lock:
                            touch_state.down_y = val
                    with touch_state.lock:
                        touch_state.dy = val - start_y
    except Exception as e:
        with touch_state.lock:
            touch_state.error = "touch input stopped"
        print(f"touch reader stopped: {e}", file=sys.stderr)


def ease_out_quint(t):
    """Sharper initial move and a longer settle than cubic -- closer to the
    deceleration curve a paged iOS scroll view uses, which is most of why
    that feels "fast but not abrupt"."""
    return 1 - (1 - t) ** 5


def ease_out_cubic(t):
    return 1 - (1 - t) ** 3


def build_strip(cur_img, other_img, other_on_right):
    """Pre-compose the two panels side by side once per drag, so each
    animation frame is a single crop instead of allocating a fresh canvas
    and pasting two full 240x320 images into it. At 60fps on this SoC that
    per-frame allocation was a real slice of the frame budget."""
    strip = Image.new("RGB", (W * 2, H), BG)
    if other_on_right:
        strip.paste(cur_img, (0, 0))
        strip.paste(other_img, (W, 0))
    else:
        strip.paste(other_img, (0, 0))
        strip.paste(cur_img, (W, 0))
    return strip


def strip_frame(strip, dx, other_on_right):
    base = 0 if other_on_right else W
    x = max(0, min(W, base - dx))
    return strip.crop((x, 0, x + W, H))


ANIM_SECONDS = 0.26
ANIM_MIN_SECONDS = 0.14
FRAME_INTERVAL = 1.0 / 60


def flick_duration(distance_px, velocity_px_s):
    """Carry the finger's own speed into the settle animation instead of
    always taking a fixed 0.22s: a hard flick finishes quickly, a slow drag
    released near the threshold eases out gently. Same idea as UIKit
    handing a scroll view its release velocity."""
    v = abs(velocity_px_s)
    if v < 1:
        return ANIM_SECONDS
    return max(ANIM_MIN_SECONDS, min(ANIM_SECONDS, abs(distance_px) / v))


def mode_live():
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    reader = threading.Thread(target=_touch_reader, daemon=True)
    reader.start()

    cfg = load_config()
    fx_range = "week"
    weather_day_idx = 0
    sms_selected_idx = 0

    # Everything that touches the network, ubus or the SMS spool now runs
    # on the refresher's thread. The render loop below only ever reads
    # already-fetched values, so a slow curl or a wedged ubus call can no
    # longer stall drawing or touch handling.
    refresher = Refresher()
    refresher.add("fx", fetch_fx, 300)
    refresher.add("sim", lambda: get_sim_status(cfg), 30)
    refresher.add("oc", get_openclash_status, 30)
    refresher.add("traf", get_openclash_traffic_and_node, 20)
    refresher.add("wx", lambda: fetch_weather(cfg["weather_city"]), 1800)
    refresher.add("aq", lambda: fetch_air_quality(cfg["weather_city"]), 1800)
    refresher.add("sms", get_sms_messages, 15)
    refresher.add("rep", get_repeater_status, 30)
    refresher.add("wg_peers", get_wireguard_peers, 120)
    refresher.add("wg_active", get_wireguard_active, 30)
    refresher.add("cell", _get_active_cell_info, 20)
    refresher.start()

    # Seeded from cache/defaults so the first frame draws immediately
    # instead of waiting on the first refresher pass.
    fx = fetch_fx()
    sim = {"slot": "1", "country": None, "phone": "", "traffic_mb": None,
           "cap_mb": cfg.get("data_cap_mb"), "sim_choice": "sim1", "data_up": False,
           "iccid": None, "attached": False, "roaming": False}
    oc = {"installed": openclash_installed(), "enabled": False, "mode": "rule"}
    traf = openclash_traffic_empty()
    wx, aq = [], []
    sms_messages = []
    wg_peers, wg_active = [], None
    rep = {"connected": False, "ssid": None, "signal": None, "ip": None}

    net_sample, net_down, net_up = None, None, None
    cpu_sample, cpu_pct = None, None
    ram_pct, ram_used_gb, ram_total_gb = get_ram_stats()
    temp_c = get_temp_c()
    mon_uptime_min = get_system_info()["uptime_min"]
    last_mon_check = time.time()
    _cell_info = _get_active_cell_info()
    conn_type = get_wan_conn_type(_cell_info)
    cell_signal = get_cell_signal(_cell_info)

    def render_main(idx):
        img = _render_panel(idx)
        with touch_state.lock:
            err = touch_state.error
        if err:
            d = ImageDraw.Draw(img)
            d.rectangle([0, H - 20, W, H], fill=(90, 26, 34))
            centered_text(d, W / 2, H - 17, err, font("default_medium", 11), (255, 220, 220))
        return img

    def _render_panel(idx):
        name = PANEL_NAMES[idx]
        if name == "clock":
            return panel_clock(cfg, rep, conn_type, cell_signal, sms_messages)
        elif name == "fx":
            return panel_fx(cfg, fx, fx_range, conn_type, cell_signal)
        elif name == "sim":
            display_sim = sim if sim_connect_override is None else dict(sim, data_up=sim_connect_override)
            return panel_sim(cfg, display_sim, conn_type, cell_signal, wg_peers, wg_active)
        elif name == "openclash":
            return panel_openclash(oc, traf, conn_type, cell_signal)
        elif name == "weather":
            return panel_weather(cfg, wx, conn_type, cell_signal)
        elif name == "games":
            return panel_games(game_scores)
        else:
            net_iface = net_sample[0] if net_sample else None
            return panel_monitor(net_down, net_up, net_iface, cpu_pct, ram_pct, ram_used_gb, ram_total_gb, temp_c, mon_uptime_min, conn_type, cell_signal)

    panel_idx = 0
    view = "main"
    cur_img = render_main(panel_idx)
    write_frame(cur_img)
    last_draw = time.time()

    state = "idle"  # idle | dragging | animating  (main-carousel only)
    neighbor_img = None
    neighbor_on_right = True
    drag_strip = None
    last_dx_drawn = None
    anim_from_dx = anim_target_dx = anim_t0 = 0
    anim_seconds = ANIM_SECONDS
    anim_next_idx = panel_idx
    sub_dirty = True

    # state for the newer sub-screens (More, Repeater, Confirm, Keyboard)
    wifi24 = True
    wifi_band = "5g"
    rep_networks = []
    scan_state = {"result": None, "running": False}
    connecting_ssid = None
    connecting_since = 0.0
    last_connect_check = 0.0
    last_spinner_draw = 0.0
    connect_error = None

    # Games hub + the four games' runtime state
    game_scores = load_game_scores()
    active_game = None
    game_state = {}
    last_game_frame = 0.0

    def handle_game(now):
        """Owns touch completely while a game is on screen -- same family
        as handle_repeater_scroll/handle_sms_scroll. A tap on the
        persistent Exit pill always wins and returns to the hub before
        anything is handed to the game itself. Continuous games (Flappy,
        Breakout) redraw on a fixed GAME_TICK_S cadence using the real
        elapsed time as dt (not a fixed constant) so a delayed loop
        iteration doesn't make the game visibly slow down or speed up.
        Snake and 2048 only redraw when their own state actually changes."""
        nonlocal view, panel_idx, cur_img, last_draw, sub_dirty
        nonlocal game_state, last_game_frame

        with touch_state.lock:
            active = touch_state.active
            dx = touch_state.dx
            down_x, down_y = touch_state.down_x, touch_state.down_y
            have_pos = touch_state.have_pos
            released = touch_state.release_pending
            release_dx, release_dy = touch_state.release_dx, touch_state.release_dy
            touch_state.release_pending = False

        tap, tap_x, tap_y = False, 0, 0
        swipe, sdx, sdy = False, 0, 0
        if released:
            is_tap = have_pos and abs(release_dx) <= TAP_JITTER_PX and abs(release_dy) <= TAP_JITTER_PX
            if is_tap:
                if hit_game_exit(down_x, down_y):
                    view = "main"
                    panel_idx = PANEL_NAMES.index("games")
                    cur_img = render_main(panel_idx)
                    write_frame(cur_img)
                    last_draw = now
                    return
                tap, tap_x, tap_y = True, down_x, down_y
            else:
                swipe, sdx, sdy = True, release_dx, release_dy

        drag_x = (down_x + dx) if active and have_pos else None
        key = active_game

        if key == "snake":
            game_state, changed = tick_snake(game_state, now, game_scores, tap, tap_x, tap_y)
            if changed or sub_dirty:
                write_frame(draw_snake(game_state, game_scores))
                sub_dirty = False
        elif key == "flappy":
            if sub_dirty or now - last_game_frame >= GAME_TICK_S:
                dt = (now - last_game_frame) if last_game_frame else 0.0
                game_state = tick_flappy(game_state, dt, game_scores, tap)
                write_frame(draw_flappy(game_state, game_scores))
                last_game_frame = now
                sub_dirty = False
        elif key == "breakout":
            if sub_dirty or now - last_game_frame >= GAME_TICK_S:
                dt = (now - last_game_frame) if last_game_frame else 0.0
                game_state = tick_breakout(game_state, dt, game_scores, tap, drag_x)
                write_frame(draw_breakout(game_state, game_scores))
                last_game_frame = now
                sub_dirty = False
        elif key == "twenty48":
            if tap or swipe or sub_dirty:
                game_state = tick_2048(game_state, game_scores, tap, swipe, sdx, sdy)
                write_frame(draw_2048(game_state, game_scores))
                sub_dirty = False

    def start_repeater_scan():
        if scan_state["running"]:
            return
        scan_state["running"] = True

        def _worker():
            scan_state["result"] = repeater_scan()
            scan_state["running"] = False

        threading.Thread(target=_worker, daemon=True).start()

    def handle_scroll_picker(now, items, render_fn, on_select):
        """Shared drag-to-scroll / tap-to-select logic for any scrollable
        picker sub-screen (weather city, top/bottom currency, ...). `items`
        is a list of (key, label); `render_fn(scroll_px)` renders the
        screen; `on_select(key)` applies the choice. Handles going back to
        main on a header tap. Caller still owns sleep+continue."""
        nonlocal view, sub_dirty, cur_img, last_draw, picker_scroll_base
        max_scroll = scroll_picker_max(len(items))
        with touch_state.lock:
            active = touch_state.active
            dy, dx = touch_state.dy, touch_state.dx
            down_x, down_y = touch_state.down_x, touch_state.down_y
            have_pos = touch_state.have_pos
            released = touch_state.release_pending
            release_dx, release_dy = touch_state.release_dx, touch_state.release_dy
            touch_state.release_pending = False

        if active and (abs(dy) > TAP_JITTER_PX or abs(dx) > TAP_JITTER_PX):
            live_scroll = min(max_scroll, max(0, picker_scroll_base - dy))
            write_frame(render_fn(live_scroll))
        elif released:
            final_dx, final_dy = release_dx, release_dy
            is_tap = (have_pos and abs(final_dx) <= TAP_JITTER_PX
                      and abs(final_dy) <= TAP_JITTER_PX)
            if is_tap and hit_back(down_y):
                view = "main"
                cur_img = render_main(panel_idx)
                write_frame(cur_img)
                last_draw = now
            elif is_tap:
                idx = hit_scroll_picker(down_y, len(items), picker_scroll_base)
                if idx is not None:
                    # Hand the picker frame to on_select so a slow apply can
                    # dim *this* screen and spin over it, instead of freezing
                    # on it with no sign of life.
                    on_select(items[idx][0], render_fn(picker_scroll_base))
                    view = "main"
                    cur_img = render_main(panel_idx)
                    write_frame(cur_img)
                    last_draw = now
            else:
                picker_scroll_base = min(max_scroll, max(0, picker_scroll_base - final_dy))
                write_frame(render_fn(picker_scroll_base))
        elif sub_dirty:
            write_frame(render_fn(picker_scroll_base))
            sub_dirty = False

    def handle_repeater_scroll(now):
        """Repeater-specific sibling of handle_scroll_picker: same
        drag-to-scroll mechanics, but the tap action is more involved than
        a single on_select -- a network can connect immediately, need a
        password, or the disconnect zone above the (unscrolled) list can
        fire instead of anything in it."""
        nonlocal view, sub_dirty, cur_img, last_draw, picker_scroll_base
        nonlocal rep, kb_target_ssid, kb_target_bssid, kb_text, kb_layer, kb_caps
        nonlocal confirm_title, confirm_message, confirm_yes_label, confirm_action, confirm_return_view, confirm_danger
        nonlocal connecting_ssid, connecting_since, last_connect_check, last_spinner_draw
        nonlocal connect_error
        max_scroll = repeater_scroll_max(len(rep_networks))

        if connecting_ssid is not None:
            if now - last_connect_check >= CONNECT_POLL_INTERVAL:
                last_connect_check = now
                fresh = get_repeater_status()
                if fresh["connected"] and fresh["ssid"] == connecting_ssid:
                    rep = fresh
                    connecting_ssid = None
                    connect_error = None
                    sub_dirty = True
                elif now - connecting_since >= CONNECT_TIMEOUT:
                    connect_error = "%s didn’t accept the connection — wrong password?" % (
                        truncate_ssid(connecting_ssid))
                    connecting_ssid = None
                    sub_dirty = True
            if connecting_ssid is not None and now - last_spinner_draw >= SPINNER_FRAME_INTERVAL:
                last_spinner_draw = now
                sub_dirty = True

        with touch_state.lock:
            active = touch_state.active
            dy, dx = touch_state.dy, touch_state.dx
            down_x, down_y = touch_state.down_x, touch_state.down_y
            have_pos = touch_state.have_pos
            released = touch_state.release_pending
            release_dx, release_dy = touch_state.release_dx, touch_state.release_dy
            touch_state.release_pending = False

        if active and (abs(dy) > TAP_JITTER_PX or abs(dx) > TAP_JITTER_PX):
            live_scroll = min(max_scroll, max(0, picker_scroll_base - dy))
            write_frame(panel_repeater(rep, rep_networks, live_scroll, error=connect_error))
        elif released:
            final_dx, final_dy = release_dx, release_dy
            is_tap = (have_pos and abs(final_dx) <= TAP_JITTER_PX
                      and abs(final_dy) <= TAP_JITTER_PX)
            if is_tap and hit_back(down_y):
                view = "main"
                cur_img = render_main(panel_idx)
                write_frame(cur_img)
                last_draw = now
            elif is_tap:
                action, val = hit_repeater(down_x, down_y, rep, len(rep_networks), picker_scroll_base)
                if action == "disconnect":
                    confirm_title = "Repeater"
                    confirm_message = f"Disconnect from {rep['ssid']}?"
                    confirm_yes_label = "Disconnect"
                    confirm_danger = True
                    confirm_action = "repeater_disconnect"
                    confirm_return_view = "repeater"
                    view = "confirm"
                    sub_dirty = True
                elif action == "select":
                    ap = rep_networks[val]
                    remembered_key = None if ap["open"] else get_remembered_repeater_keys().get(ap["ssid"])
                    if ap["open"] or remembered_key is not None:
                        repeater_connect(ap["ssid"], ap["bssid"], remembered_key or "")
                        connect_error = None
                        connecting_ssid = ap["ssid"]
                        connecting_since = now
                        last_connect_check = now
                        start_repeater_scan()
                        sub_dirty = True
                    else:
                        kb_target_ssid = ap["ssid"]
                        kb_target_bssid = ap["bssid"]
                        kb_text = ""
                        kb_layer = "letters"
                        kb_caps = False
                        view = "keyboard_wifi"
                        sub_dirty = True
            else:
                picker_scroll_base = min(max_scroll, max(0, picker_scroll_base - final_dy))
                write_frame(panel_repeater(rep, rep_networks, picker_scroll_base, error=connect_error))
        elif sub_dirty:
            spin_phase = int(now * 240) % 360
            write_frame(panel_repeater(rep, rep_networks, picker_scroll_base, connecting_ssid,
                                       spin_phase, error=connect_error))
            sub_dirty = False

    def handle_sms_scroll(now):
        """SMS-list sibling of handle_repeater_scroll: same drag/tap
        mechanics, own row height (2-line rows need more space than the
        shared SCROLL_ROW_H), tap opens the full message instead of
        applying a single-value choice."""
        nonlocal view, sub_dirty, cur_img, last_draw, picker_scroll_base, sms_selected_idx
        max_scroll = sms_scroll_max(len(sms_messages))
        with touch_state.lock:
            active = touch_state.active
            dy, dx = touch_state.dy, touch_state.dx
            down_x, down_y = touch_state.down_x, touch_state.down_y
            have_pos = touch_state.have_pos
            released = touch_state.release_pending
            release_dx, release_dy = touch_state.release_dx, touch_state.release_dy
            touch_state.release_pending = False

        if active and (abs(dy) > TAP_JITTER_PX or abs(dx) > TAP_JITTER_PX):
            live_scroll = min(max_scroll, max(0, picker_scroll_base - dy))
            write_frame(panel_sms(sms_messages, live_scroll))
        elif released:
            final_dx, final_dy = release_dx, release_dy
            is_tap = (have_pos and abs(final_dx) <= TAP_JITTER_PX
                      and abs(final_dy) <= TAP_JITTER_PX)
            if is_tap and hit_back(down_y):
                view = "main"
                cur_img = render_main(panel_idx)
                write_frame(cur_img)
                last_draw = now
            elif is_tap:
                idx = hit_sms(down_y, len(sms_messages), picker_scroll_base)
                if idx is not None:
                    sms_selected_idx = idx
                    view = "sms_detail"
                    sub_dirty = True
            else:
                picker_scroll_base = min(max_scroll, max(0, picker_scroll_base - final_dy))
                write_frame(panel_sms(sms_messages, picker_scroll_base))
        elif sub_dirty:
            write_frame(panel_sms(sms_messages, picker_scroll_base))
            sub_dirty = False

    confirm_title = confirm_message = confirm_yes_label = confirm_action = confirm_return_view = ""
    confirm_danger = False
    kb_text = ""
    kb_layer = "letters"
    kb_caps = False
    kb_target_ssid = kb_target_bssid = None
    picker_scroll_base = 0.0
    fx_edit_side = "from"
    # Optimistic display state for the cellular connect toggle: this
    # router's own backhaul manager can silently revert a manual ifup
    # when a healthier WAN (the repeater WiFi) is already up, sometimes
    # within a couple of seconds -- from the user's side a tap that WAS
    # received looked identical to a tap that wasn't, since the toggle
    # never visibly moved. Show the tapped-for state immediately, then
    # after CONNECT_OPTIMISTIC_SECONDS re-check the real interface state
    # and snap back if it didn't actually take. This is purely cosmetic
    # confirmation that the tap registered -- it doesn't change whether
    # the connection itself succeeds.
    sim_connect_override = None
    sim_connect_override_until = 0.0
    CONNECT_OPTIMISTIC_SECONDS = 2.0
    last_switch_req_check = 0.0

    while not _stop:
        now = time.time()

        if is_screen_asleep():
            with touch_state.lock:
                touch_state.release_pending = False
            time.sleep(0.1)
            continue

        if now - last_switch_req_check > 0.3:
            last_switch_req_check = now
            if os.path.exists(SWITCH_REQUEST_FILE):
                try:
                    os.remove(SWITCH_REQUEST_FILE)
                except Exception:
                    pass
                confirm_title = "Stock UI"
                confirm_message = "Hand the screen back to the GL.iNet UI?"
                confirm_yes_label = "Switch"
                confirm_danger = False
                confirm_action = "return_stock"
                confirm_return_view = view if view != "confirm" else "main"
                view = "confirm"
                sub_dirty = True

        if view == "main":
            if state == "idle":
                # Pull whatever the background refresher has ready. These
                # are plain dict reads -- no I/O on this thread.
                fx = refresher.get("fx", fx)
                sim = refresher.get("sim", sim)
                oc = refresher.get("oc", oc)
                traf = refresher.get("traf", traf)
                wx = refresher.get("wx", wx)
                aq = refresher.get("aq", aq)
                sms_messages = refresher.get("sms", sms_messages)
                rep = refresher.get("rep", rep)
                wg_peers = refresher.get("wg_peers", wg_peers)
                wg_active = refresher.get("wg_active", wg_active)
                _cell = refresher.get("cell")
                if _cell is not None:
                    conn_type = get_wan_conn_type(_cell)
                    cell_signal = get_cell_signal(_cell)
                if now - last_mon_check > 2:
                    net_sample, net_down, net_up = sample_bandwidth(net_sample)
                    cpu_sample, cpu_pct = sample_cpu(cpu_sample)
                    ram_pct, ram_used_gb, ram_total_gb = get_ram_stats()
                    temp_c = get_temp_c()
                    mon_uptime_min = get_system_info()["uptime_min"]
                    last_mon_check = now
                # The override MUST always expire. It used to be skipped
                # entirely whenever sim_connect_override_until was None --
                # which is exactly what the "turning off" and "turning on
                # with no competing WAN" branches set it to -- so the Data
                # toggle then displayed the tapped-for value forever,
                # masking the real interface state for the rest of the
                # process's life. That inverted the whole point of the
                # feature, which is honest feedback.
                if sim_connect_override is not None and now >= sim_connect_override_until:
                    refresher.request("sim")
                    sim_connect_override = None
                if now - last_draw >= 1:
                    cur_img = render_main(panel_idx)
                    write_frame(cur_img)
                    last_draw = now

                with touch_state.lock:
                    active = touch_state.active
                    touch_state.release_pending = False
                if active:
                    state = "dragging"

            elif state == "dragging":
                with touch_state.lock:
                    active = touch_state.active
                    dx, dy = touch_state.dx, touch_state.dy
                    down_x, down_y = touch_state.down_x, touch_state.down_y
                    released = touch_state.release_pending
                    release_dx, release_dy = touch_state.release_dx, touch_state.release_dy
                    release_vx = touch_state.release_vx
                    have_pos = touch_state.have_pos
                    touch_state.release_pending = False

                if neighbor_img is None or (neighbor_on_right and dx > TAP_JITTER_PX) or \
                   (not neighbor_on_right and dx < -TAP_JITTER_PX):
                    if dx < 0:
                        neighbor_on_right = True
                        neighbor_img = render_main((panel_idx + 1) % len(PANEL_NAMES))
                        drag_strip = build_strip(cur_img, neighbor_img, True)
                    elif dx > 0:
                        neighbor_on_right = False
                        neighbor_img = render_main((panel_idx - 1) % len(PANEL_NAMES))
                        drag_strip = build_strip(cur_img, neighbor_img, False)
                    last_dx_drawn = None

                if drag_strip is not None:
                    dx_clamped = max(-W, min(W, dx))
                    # The finger only moves so fast; redrawing an identical
                    # offset just burns a frame's worth of RGB565 conversion
                    # that could have gone to the next real one.
                    if dx_clamped != last_dx_drawn:
                        write_frame(strip_frame(drag_strip, dx_clamped, neighbor_on_right))
                        last_dx_drawn = dx_clamped

                if released or not active:
                    final_dx = release_dx if released else dx
                    final_dy = release_dy if released else dy
                    # A release with no position event of its own would be
                    # hit-tested against the PREVIOUS touch's coordinates.
                    is_tap = (have_pos and abs(final_dx) <= TAP_JITTER_PX
                              and abs(final_dy) <= TAP_JITTER_PX)

                    if is_tap:
                        name = PANEL_NAMES[panel_idx]
                        zone = None
                        if name == "clock":
                            zone = hit_main_clock(down_x, down_y)
                        elif name == "fx":
                            zone = hit_main_fx(down_x, down_y)
                        elif name == "sim":
                            zone = hit_main_sim(down_x, down_y)
                        elif name == "openclash":
                            zone = hit_main_openclash(down_x, down_y)
                        elif name == "weather":
                            zone = hit_main_weather(down_x, down_y)
                        elif name == "games":
                            zone = hit_main_games(down_x, down_y)

                        new_view = None
                        if name == "clock" and zone == "city_left":
                            new_view = "city_top"
                            picker_scroll_base = 0
                        elif name == "clock" and zone == "city_right":
                            new_view = "city_bottom"
                            picker_scroll_base = 0
                        elif name == "clock" and zone == "repeater":
                            new_view = "repeater"
                            picker_scroll_base = 0
                            rep = get_repeater_status()
                            start_repeater_scan()
                        elif name == "clock" and zone == "more":
                            new_view = "more"
                            wifi24 = get_wifi_radio_state("wifi2g")
                            wifi_band = get_wifi_band_state()
                            rep = get_repeater_status()
                        elif name == "clock" and zone == "sms":
                            new_view = "sms"
                            picker_scroll_base = 0
                            sms_messages = get_sms_messages()
                        elif name == "fx" and zone == "top_from":
                            new_view, fx_edit_side = "fx_top", "from"
                            picker_scroll_base = 0
                        elif name == "fx" and zone == "top_to":
                            new_view, fx_edit_side = "fx_top", "to"
                            picker_scroll_base = 0
                        elif name == "fx" and zone == "bottom_from":
                            new_view, fx_edit_side = "fx_bottom", "from"
                            picker_scroll_base = 0
                        elif name == "fx" and zone == "bottom_to":
                            new_view, fx_edit_side = "fx_bottom", "to"
                            picker_scroll_base = 0
                        elif name == "fx" and zone == "update":
                            fx = run_with_spinner(cur_img, "Updating rates…",
                                                  lambda: fetch_fx(force=True),
                                                  ACCENT["fx"]) or fx
                            refresher.request("fx")
                        elif name == "fx" and zone and zone.startswith("range:"):
                            new_range = zone.split(":", 1)[1]
                            if new_range != fx_range:
                                fx_range = new_range
                        elif name == "sim" and zone and zone.startswith("choice:"):
                            choice = zone.split(":", 1)[1]
                            if choice != sim["sim_choice"]:
                                def _switch_sim(choice=choice):
                                    set_sim_choice(choice)
                                    return get_sim_status(cfg)

                                sim = run_with_spinner(cur_img, "Switching SIM…",
                                                       _switch_sim, ACCENT["sim"]) or sim
                        elif name == "sim" and zone == "attach_toggle":
                            # Network registration only (SMS/calls) --
                            # doesn't compete with a WiFi/ethernet WAN the
                            # way the data toggle below can, so no
                            # optimistic/snap-back dance needed here.
                            def _set_attach(want=not sim["attached"]):
                                set_network_attach_enabled(want)
                                return get_sim_status(cfg)

                            sim = run_with_spinner(cur_img, "Applying…", _set_attach,
                                                   ACCENT["sim"]) or sim
                        elif name == "sim" and zone == "data_toggle":
                            new_state = not sim["data_up"]
                            set_cellular_data_enabled(new_state)
                            sim_connect_override = new_state
                            if new_state and has_competing_wan():
                                # Turning on while repeater/ethernet is
                                # already active: that WAN's manager may
                                # revert this, so verify and snap back.
                                sim_connect_override_until = now + CONNECT_OPTIMISTIC_SECONDS
                            else:
                                # Turning off always "works" from the UI's
                                # perspective, and turning on with nothing
                                # competing has nothing to revert it --
                                # trust the tap, let it search/connect in
                                # the background without second-guessing.
                                sim_connect_override_until = None
                        elif name == "sim" and zone == "roam_toggle":
                            def _set_roam(iccid=sim["iccid"], want=not sim["roaming"]):
                                set_roaming_enabled(iccid, want)
                                return get_sim_status(cfg)

                            sim = run_with_spinner(cur_img, "Applying…", _set_roam,
                                                   ACCENT["sim"]) or sim
                        elif name == "sim" and zone == "wireguard":
                            new_view = "wireguard"
                            wg_peers = get_wireguard_peers()
                            wg_active = get_wireguard_active()
                        elif name == "sim" and zone == "data_cap":
                            new_view = "datacap"
                        elif name == "openclash" and zone == "toggle":
                            set_openclash_enabled(not oc["enabled"])
                            oc = get_openclash_status()
                        elif name == "openclash" and zone == "mode_global" and oc["mode"] != "global":
                            set_openclash_mode("global")
                            oc = get_openclash_status()
                        elif name == "openclash" and zone == "mode_rule" and oc["mode"] != "rule":
                            set_openclash_mode("rule")
                            oc = get_openclash_status()
                        elif name == "openclash" and zone == "node":
                            new_view = "oc_nodes"
                        elif name == "openclash" and zone == "update_sub":
                            # Was Popen-ed and immediately painted over by the
                            # next redraw, so the "Updating" flash lasted a
                            # single frame and there was never any completion
                            # signal at all.
                            run_with_spinner(cur_img, "Updating subscription…",
                                             lambda: update_openclash_subscription(wait=True),
                                             ACCENT["openclash"])
                            refresher.request("traf")
                        elif name == "weather" and zone == "city":
                            new_view = "weather_city"
                            picker_scroll_base = 0
                        elif name == "weather" and zone == "update":
                            def _refresh_weather():
                                return (fetch_weather(cfg["weather_city"], force=True),
                                        fetch_air_quality(cfg["weather_city"], force=True))

                            got = run_with_spinner(cur_img, "Updating weather…",
                                                   _refresh_weather, ACCENT["weather"])
                            if got:
                                wx, aq = got[0] or wx, got[1] or aq
                        elif name == "weather" and zone and zone.startswith("day:"):
                            # panel_weather renders "no data yet" when wx is
                            # empty, but the three day columns stay tappable
                            # and the detail view then did wx[0] on an empty
                            # list -- an uncaught IndexError that took the
                            # whole process down (and run.sh counts three of
                            # those as "fall back to the stock UI forever").
                            day_i = int(zone.split(":", 1)[1])
                            if day_i < len(wx):
                                weather_day_idx = day_i
                                new_view = "weather_detail"
                        elif name == "games" and zone and zone.startswith("play:"):
                            active_game = zone.split(":", 1)[1]
                            game_state = new_game_state(active_game)
                            last_game_frame = 0.0
                            new_view = "game"

                        neighbor_img = None
                        state = "idle"
                        if new_view:
                            view = new_view
                            sub_dirty = True
                        else:
                            cur_img = render_main(panel_idx)
                            write_frame(cur_img)
                            last_draw = now
                    else:
                        # Page on EITHER enough travel or enough speed. The
                        # old distance-only rule made a quick flick spring
                        # back, which reads as the gesture being ignored.
                        flick = abs(release_vx) > FLICK_VELOCITY_PX_S
                        same_way = (release_vx < 0) == (final_dx < 0)
                        if abs(final_dx) > W * 0.3 or (flick and same_way and abs(final_dx) > TAP_JITTER_PX):
                            anim_target_dx = -W if final_dx < 0 else W
                            anim_next_idx = (panel_idx + (1 if final_dx < 0 else -1)) % len(PANEL_NAMES)
                        else:
                            anim_target_dx = 0
                            anim_next_idx = panel_idx
                        anim_from_dx = max(-W, min(W, final_dx))
                        anim_seconds = flick_duration(anim_target_dx - anim_from_dx, release_vx)
                        anim_t0 = now
                        last_dx_drawn = None
                        state = "animating"

            elif state == "animating":
                t = (now - anim_t0) / max(0.001, anim_seconds)
                if t >= 1:
                    if anim_next_idx != panel_idx:
                        panel_idx = anim_next_idx
                    cur_img = render_main(panel_idx)
                    write_frame(cur_img)
                    last_draw = now
                    neighbor_img = None
                    drag_strip = None
                    state = "idle"
                else:
                    eased = ease_out_quint(t)
                    dx_now = int(anim_from_dx + (anim_target_dx - anim_from_dx) * eased)
                    if drag_strip is not None and dx_now != last_dx_drawn:
                        write_frame(strip_frame(drag_strip, dx_now, neighbor_on_right))
                        last_dx_drawn = dx_now

        else:  # sub-screen
            if view == "datacap":
                def _select_datacap(key, base_img):
                    nonlocal sim
                    cfg["data_cap_mb"] = key
                    save_config(cfg)
                    sim = run_with_spinner(base_img, "Applying…",
                                           lambda: get_sim_status(cfg), ACCENT["sim"]) or sim

                items = [(v, cap_label(v)) for v in DATA_CAP_PRESETS]
                handle_scroll_picker(now, items, lambda s: panel_datacap_picker(cfg, s), _select_datacap)
                time.sleep(0.012)
                continue

            if view == "oc_nodes":
                def _select_node(key, base_img):
                    nonlocal traf

                    def _apply():
                        select_openclash_node(traf["group"], key)
                        return get_openclash_traffic_and_node()

                    traf = run_with_spinner(base_img, "Switching node…", _apply,
                                            ACCENT["openclash"]) or traf

                items = [(n, n) for n in traf["nodes"]]
                handle_scroll_picker(now, items, lambda s: panel_node_picker(traf, s), _select_node)
                time.sleep(0.012)
                continue

            if view == "weather_city":
                def _select_weather_city(key, base_img):
                    nonlocal wx, aq
                    cfg["weather_city"] = key
                    save_config(cfg)

                    def _apply():
                        return fetch_weather(key), fetch_air_quality(key)

                    # Two uncached HTTP calls: this used to freeze on the
                    # picker for up to 16s with no feedback at all, so a tap
                    # that HAD registered looked exactly like one that had not.
                    got = run_with_spinner(base_img, "Loading weather…", _apply,
                                           ACCENT["weather"])
                    if got:
                        wx, aq = got[0] or [], got[1] or []
                    refresher.request("wx")
                    refresher.request("aq")

                items = [(name, name) for name, _, _ in WEATHER_CITIES]
                handle_scroll_picker(now, items, lambda s: panel_weather_picker(cfg, s), _select_weather_city)
                time.sleep(0.012)
                continue

            if view in ("city_top", "city_bottom"):
                slot = "top" if view == "city_top" else "bottom"
                cfg_key = "clock_top" if view == "city_top" else "clock_bottom"

                def _select_city(key, base_img=None, cfg_key=cfg_key):
                    cfg[cfg_key] = key
                    save_config(cfg)

                items = list(CITIES)
                handle_scroll_picker(now, items, lambda s: panel_city_picker(slot, cfg, s), _select_city)
                time.sleep(0.012)
                continue

            if view in ("fx_top", "fx_bottom"):
                slot = "top" if view == "fx_top" else "bottom"
                cfg_key = f"fx_{slot}_{fx_edit_side}"

                def _select_currency(key, base_img=None, cfg_key=cfg_key):
                    cfg[cfg_key] = key
                    save_config(cfg)

                items = [(c, f"{c} · {CURRENCY_NAMES[c]}") for c in CURRENCIES]
                handle_scroll_picker(now, items, lambda s: panel_currency_picker(slot, fx_edit_side, cfg, s), _select_currency)
                time.sleep(0.012)
                continue

            if view == "repeater" and scan_state["result"] is not None:
                rep_networks = scan_state["result"]
                scan_state["result"] = None
                sub_dirty = True

            if view == "repeater":
                handle_repeater_scroll(now)
                time.sleep(0.012)
                continue

            if view == "sms":
                handle_sms_scroll(now)
                time.sleep(0.012)
                continue

            if view == "game":
                handle_game(now)
                time.sleep(0.01)
                continue

            if sub_dirty:
                if view == "more":
                    img = panel_more(wifi24, wifi_band, cfg["clock_style"], get_wifi56_conflict_idx(rep))
                elif view == "confirm":
                    img = panel_confirm(confirm_title, confirm_message, ACCENT["clock"],
                                        yes_label=confirm_yes_label, danger=confirm_danger)
                elif view == "keyboard_wifi":
                    img = panel_keyboard("Wi-Fi Password", kb_text, kb_layer, kb_caps,
                                         ACCENT["clock"], connect_label="Connect")
                elif view == "weather_detail":
                    day = wx[weather_day_idx] if weather_day_idx < len(wx) else wx[0]
                    day_aq = aq[weather_day_idx] if weather_day_idx < len(aq) else None
                    day_label = weather_day_labels(wx)[weather_day_idx]
                    img = panel_weather_detail(cfg, day, day_aq, day_label)
                elif view == "sms_detail":
                    idx = sms_selected_idx if sms_selected_idx < len(sms_messages) else 0
                    img = panel_sms_detail(sms_messages[idx])
                elif view == "wireguard":
                    img = panel_wireguard(wg_peers, wg_active)
                else:
                    img = render_sub(view, cfg, oc, traf)
                write_frame(img)
                sub_dirty = False

            with touch_state.lock:
                dx, dy = touch_state.dx, touch_state.dy
                down_x, down_y = touch_state.down_x, touch_state.down_y
                have_pos = touch_state.have_pos
                released = touch_state.release_pending
                release_dx, release_dy = touch_state.release_dx, touch_state.release_dy
                touch_state.release_pending = False

            if released:
                final_dx, final_dy = release_dx, release_dy
                is_tap = (have_pos and abs(final_dx) <= TAP_JITTER_PX
                          and abs(final_dy) <= TAP_JITTER_PX)

                if view == "confirm" and (is_tap and (hit_back(down_y) or hit_confirm(down_x, down_y) == "no")):
                    view = confirm_return_view
                    sub_dirty = True
                elif view == "confirm" and is_tap and hit_confirm(down_x, down_y) == "yes":
                    if confirm_action == "reboot":
                        reboot_router()
                        view = "more"
                    elif confirm_action == "shutdown":
                        shutdown_router()
                        view = "more"
                    elif confirm_action == "return_stock":
                        # toggle.sh force-kills this very process within
                        # ~1.2s if it hasn't already exited (wait_gone's
                        # poll-then-SIGKILL), then starts gl_screen -- so
                        # the real-world gap is short, but nothing was
                        # ever drawn to the screen during it before, and
                        # a silent frozen dashboard for even a couple of
                        # seconds reads as "did my tap even register?"
                        # Paint an unmistakable "switching" spinner over
                        # the confirm dialog instead, for up to
                        # SWITCH_OVERLAY_MAX_S or until _stop fires
                        # (procd's own TERM has reached us and there's no
                        # point animating any further -- this process is
                        # already being torn down). If the stock UI
                        # somehow still hasn't taken over by the time the
                        # window elapses, falling through to the live
                        # dashboard is an honest fallback rather than
                        # freezing on "switching" forever.
                        base = panel_confirm(confirm_title, confirm_message, ACCENT["clock"],
                                             yes_label=confirm_yes_label, danger=confirm_danger)
                        switch_to_stock_ui()
                        t0 = time.time()
                        while time.time() - t0 < SWITCH_OVERLAY_MAX_S and not _stop:
                            elapsed = time.time() - t0
                            write_frame(draw_loading_overlay(base, "Switching to stock UI…",
                                                             elapsed * SPINNER_SPEED_DPS, ACCENT["clock"]))
                            time.sleep(1.0 / 25)
                        view = "main"
                    elif confirm_action == "repeater_disconnect":
                        repeater_disconnect()
                        time.sleep(0.3)
                        rep = get_repeater_status()
                        start_repeater_scan()
                        view = "repeater"
                    sub_dirty = True

                elif view == "keyboard_wifi" and is_tap and (hit_back(down_y) or hit_keyboard(down_x, down_y, kb_layer, kb_caps)[0] == "cancel"):
                    view = "repeater"
                    kb_text = ""
                    sub_dirty = True
                elif view == "keyboard_wifi" and is_tap:
                    action, val = hit_keyboard(down_x, down_y, kb_layer, kb_caps)
                    if action == "char":
                        kb_text += val
                        sub_dirty = True
                    elif action == "backspace":
                        kb_text = kb_text[:-1]
                        sub_dirty = True
                    elif action == "caps":
                        kb_caps = not kb_caps
                        sub_dirty = True
                    elif action == "layer_toggle":
                        kb_layer = "symbols" if kb_layer == "letters" else "letters"
                        sub_dirty = True
                    elif action == "connect":
                        repeater_connect(kb_target_ssid, kb_target_bssid, kb_text)
                        connect_error = None
                        connecting_ssid = kb_target_ssid
                        connecting_since = now
                        last_connect_check = now
                        kb_text = ""
                        start_repeater_scan()
                        view = "repeater"
                        sub_dirty = True

                elif view == "sms_detail" and is_tap and hit_back(down_y):
                    view = "sms"
                    sub_dirty = True
                elif is_tap and hit_back(down_y):
                    view = "main"
                    cur_img = render_main(panel_idx)
                    write_frame(cur_img)
                    last_draw = now
                elif is_tap and view == "more":
                    action = hit_more(down_x, down_y, get_wifi56_conflict_idx(rep))
                    if action == "wifi24":
                        wifi24 = not wifi24
                        set_wifi_radio_state("wifi2g", wifi24)
                        sub_dirty = True
                    elif action == "wifi_5g" and wifi_band != "5g":
                        wifi_band = "5g"
                        set_wifi_band_state(wifi_band)
                        sub_dirty = True
                    elif action == "wifi_off" and wifi_band != "off":
                        wifi_band = "off"
                        set_wifi_band_state(wifi_band)
                        sub_dirty = True
                    elif action == "wifi_6g" and wifi_band != "6g":
                        wifi_band = "6g"
                        set_wifi_band_state(wifi_band)
                        sub_dirty = True
                    elif action == "clock_analog" and cfg["clock_style"] != "analog":
                        cfg["clock_style"] = "analog"
                        save_config(cfg)
                        sub_dirty = True
                    elif action == "clock_digital" and cfg["clock_style"] != "digital":
                        cfg["clock_style"] = "digital"
                        save_config(cfg)
                        sub_dirty = True
                    elif action == "return_stock":
                        confirm_title = "Stock UI"
                        confirm_message = "Hand the screen back to the GL.iNet UI?"
                        confirm_yes_label = "Switch"
                        confirm_danger = False
                        confirm_action = "return_stock"
                        confirm_return_view = "more"
                        view = "confirm"
                        sub_dirty = True
                    elif action == "reboot":
                        confirm_title = "Reboot"
                        confirm_message = "Reboot the router now?"
                        confirm_yes_label = "Reboot"
                        confirm_danger = True
                        confirm_action = "reboot"
                        confirm_return_view = "more"
                        view = "confirm"
                        sub_dirty = True
                    elif action == "shutdown":
                        confirm_title = "Shutdown"
                        confirm_message = "Shut down the router now?"
                        confirm_yes_label = "Shutdown"
                        confirm_danger = True
                        confirm_action = "shutdown"
                        confirm_return_view = "more"
                        view = "confirm"
                        sub_dirty = True
                elif is_tap and view == "wireguard":
                    idx = hit_wireguard(down_y, len(wg_peers))
                    if idx is not None:
                        peer = wg_peers[idx]
                        turning_on = wg_active != peer["id"]
                        set_wireguard_enabled(peer["id"], turning_on)
                        wg_active = peer["id"] if turning_on else None
                        sub_dirty = True
                elif not is_tap and final_dx > W * 0.3:
                    # Swiping back used to always land on the main carousel,
                    # even from screens whose header-tap goes somewhere else
                    # -- swiping out of a message dumped you past the inbox,
                    # and swiping a confirm dialog away skipped the screen
                    # that raised it. Mirror the header-tap destination.
                    parent = {"sms_detail": "sms",
                              "keyboard_wifi": "repeater",
                              "confirm": confirm_return_view or "main"}.get(view, "main")
                    if view == "keyboard_wifi":
                        kb_text = ""
                    if parent == "main":
                        view = "main"
                        cur_img = render_main(panel_idx)
                        write_frame(cur_img)
                        last_draw = now
                    else:
                        view = parent
                        sub_dirty = True

        time.sleep(0.012)

    sys.exit(0)


if __name__ == "__main__":
    if "--preview" in sys.argv:
        idx = sys.argv.index("--preview")
        outdir = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else "/tmp/dash_preview"
        mode_preview(outdir)
    elif "--calibrate" in sys.argv:
        mode_calibrate()
    else:
        mode_live()
