# GL-E5800 Touch Dashboard

A custom, fully interactive replacement for the stock `gl_screen` UI on the
GL.iNet GL-E5800's built-in 240x320 touchscreen. Swipe between six panels,
tap into any of them for detail or control, and manage the router without
ever opening the web UI.

![Home](screenshots/panel_clock.png)

No image assets, no external UI framework — every icon, chart, and widget
is drawn at runtime with Pillow directly onto the framebuffer (`/dev/fb0`,
RGB565). Pure Python, one file.

## Panels

| Home | Active SIM | Weather |
|---|---|---|
| ![Home](screenshots/panel_clock.png) | ![SIM](screenshots/panel_sim.png) | ![Weather](screenshots/panel_weather.png) |

| Monitor | Currency | OpenClash |
|---|---|---|
| ![Monitor](screenshots/panel_monitor.png) | ![Currency](screenshots/panel_fx.png) | ![OpenClash](screenshots/panel_openclash.png) |

**Home** — two small clocks (independently pickable cities/timezones,
switchable between analog and digital from More), today's date, and two
tiles: **Repeater** and **More**, so you never need the stock GL.iNet home
page.

| Analog | Digital |
|---|---|
| ![Analog clock](screenshots/panel_clock.png) | ![Digital clock](screenshots/panel_clock_digital.png) |

**Active SIM** — country flag, full number, a SIM1 / SIM2 / eSIM switch, a
cellular data on/off toggle, and a data-usage bar against a cap you set.

**Weather** — 3-day forecast (hand-drawn icons: sun/cloud/rain/snow/fog/storm)
for a city you pick from a scrollable list of 40+ cities.

**Monitor** — bandwidth (down/up Mbps on whichever interface currently holds
the default route, so it keeps tracking the right link through a WAN
failover), CPU%, RAM used/total, SoC temperature, and uptime. Read-only,
refreshes every 2 seconds.

**Currency** — two rows, each `1 {from} = {rate} {to}`, with `from` and
`to` independently pickable from a 10-currency list (not fixed to any one
target currency), plus a real historical line chart per row (week/month/
year, pulled from [Frankfurter](https://frankfurter.dev), ECB reference
rates).

**OpenClash** — on/off, Global/Rule mode, current node (flag + guessed
country from the node name) with a tap-to-switch node list, and session
traffic — all read from Mihomo's local REST API. Gracefully shows "not
installed" instead of breaking if OpenClash isn't on the device.

| Repeater | More | On-screen keyboard |
|---|---|---|
| ![Repeater](screenshots/panel_repeater.png) | ![More](screenshots/panel_more.png) | ![Keyboard](screenshots/panel_keyboard.png) |

**Repeater** (from the Home tile) — scans and lists nearby WiFi, connects to
open networks directly or opens the on-screen keyboard for a password.
Uses the same `ubus` `repeater` object the stock GL.iNet UI uses.

**More** (from the Home tile) — 2.4GHz/5GHz radio toggles, an Analog/Digital
clock-style switch, a **Return to Stock UI** button (switches back
immediately, no confirm dialog — same effect as the power-button hold
gesture, just more discoverable/reliable), and a confirm-gated reboot.

**On-screen keyboard** — built because this screen has no physical or
pop-up keyboard. Two layers (letters/symbols), persistent caps toggle.

## Interaction

- **Swipe** left/right between the 6 main panels — real finger-tracking with
  an eased iOS-style snap/cancel animation, not a hard cut.
- **Tap** into a panel for detail screens (pick a city, pick a currency, set
  a data cap, configure OpenClash's node, etc). Tap the header or swipe
  right to go back.
- **Hold the power button ~1-2.5s** to switch between this dashboard and the
  stock GL.iNet screen at any time (round-trip tested at ~0.3-1.5s). Or use
  **More → Return to Stock UI** for the same switch without touching the
  power button at all.
- **Quick-tap the power button** to sleep/wake the screen — real backlight
  control, not a fake black frame. Classified by press *duration* on
  release (tap vs. hold), not by counting presses — an earlier tap-counting
  design (single tap = sleep, triple tap = switch UI) turned out to be
  fragile against contact bounce, where one physical tap could register as
  two raw press/release edges; duration-based detection sidesteps that
  since each press/release pair is handled the instant it completes.
  Holding past ~2.5s is left alone entirely and falls through to the
  hardware's own long-press-to-poweroff path.

## Requirements

Built and tested specifically on a **GL.iNet GL-E5800**, OpenWrt 23.05.4,
GL firmware 4.8.x. It depends on hardware/paths specific to this model:

- 240x320 RGB565 framebuffer at `/dev/fb0`
- Capacitive touchscreen at `/dev/input/event0` (Multitouch protocol B)
- Power key at `/dev/input/event1` (`KEY_POWER`)
- Backlight control at `/sys/class/backlight/soc:backlight/brightness`
- GL.iNet's `repeater` and `cellular.*` ubus objects

It will very likely **not** work unmodified on other GL.iNet models — the
touch/framebuffer/backlight paths would need re-verifying (see
[Adapting to another model](#adapting-to-another-model) below). It should be
safe to try, though: the install keeps the stock `gl_screen` UI installed
and switchable back at any time (see [Uninstalling](#uninstalling)).

## Installation

### Option A: install via LuCI (no SSH needed to get the files on)

Grab the `.ipk` from [Releases](https://github.com/robavionix/gl-e5800-dashboard/releases/latest) and, in LuCI, go to
**System → Software → Upload Package...**, pick the file, install. It's a
real opkg package — installs the same files to the same paths as the
manual steps below, pulls in `python3`/`python3-numpy`/`libtiff6`/all 5
`zoneinfo-*` packages as normal opkg dependencies, and enables the
power-button watcher. It deliberately does **not** switch the physical
screen over by itself; that stays a manual step (see step 5 below or the
Interaction section) so installing the package never surprises you with a
live UI swap. Note this doesn't add a menu entry inside LuCI itself — the
dashboard runs on the physical touchscreen, not as a web page; LuCI here
is just the install mechanism.

Prefer the CLI? `opkg install gl-e5800-dashboard_*.ipk` over SSH does the
same thing. See [packages/](packages/) if you want to rebuild the `.ipk`
yourself (also documents a couple of real opkg gotchas found while
building it — worth a read if you're packaging anything else for this
device).

### Option B: manual install over SSH

**Fast path:** clone this repo on a machine that can SSH to the router, then
`./install.sh [router-ip]` (default `192.168.8.1`). It does everything in
steps 1-3 below. Read on for what it's actually doing, or to do it by hand.

SSH into the router as root, then:

```sh
# 1. Dependencies (from GL.iNet's own opkg feed)
opkg update
opkg install python3 python3-numpy
# All 5 zoneinfo packages -- the Clock city picker includes cities from
# every region (e.g. Auckland, Toronto), and this firmware splits its
# timezone database into separate per-region opkg packages. Installing
# only zoneinfo-europe/asia will crash-loop the dashboard the moment
# someone picks a city outside those two regions.
opkg install zoneinfo-europe zoneinfo-asia zoneinfo-america zoneinfo-australia-nz zoneinfo-pacific
# python3-pillow conflicts with a file gl-sdk4-screen-large already owns
# (a bundled libfreetype) -- --nodeps works because that file is already
# on disk, just owned by the other package.
opkg install --nodeps python3-pillow
opkg install libtiff6

# 2. Copy the files (from your machine, adjust the path to wherever you
#    cloned this repo)
scp -O src/*.py src/*.sh root@192.168.8.1:/root/dashboard/
scp -O init.d/* root@192.168.8.1:/etc/init.d/

# 3. On the router: permissions + services
ssh root@192.168.8.1
chmod +x /root/dashboard/*.py /root/dashboard/*.sh /etc/init.d/citydash /etc/init.d/homebutton
/etc/init.d/homebutton enable
/etc/init.d/homebutton start

# 4. Preview before committing to it (renders to PNGs, doesn't touch the
#    live screen)
python3 /root/dashboard/dashboard.py --preview /root/dashboard/preview
# pull /root/dashboard/preview/*.png back and eyeball them

# 5. Go live
/root/dashboard/toggle.sh on
```

`toggle.sh on` disables the stock `gl_screen` service and enables/starts
`citydash` — the choice persists across reboots (mutually exclusive
enable flags), and there's a 3-strikes crash guard in `run.sh` that
automatically restores the stock UI if the Python process dies repeatedly,
so a bug can't permanently blank the screen.

### Uninstalling

Installed via the `.ipk` (Option A): `opkg remove gl-e5800-dashboard`, or
the same from LuCI's Software page — this switches the screen back to
stock automatically first if the dashboard was live, then removes
everything including the runtime-generated config/cache files.

Installed manually (Option B):

```sh
/root/dashboard/toggle.sh off
/etc/init.d/homebutton stop
/etc/init.d/homebutton disable
rm -rf /root/dashboard /etc/init.d/citydash /etc/init.d/homebutton
```

## Configuration

There's no settings UI for these — edit directly:

- **Cities offered for Clock/Weather pickers**: `CITIES` / `WEATHER_CITIES`
  lists near the top of `dashboard.py` (need an IANA timezone name and, for
  weather, lat/lon — no geocoding, no free-text search, since there's no
  keyboard for it outside the WiFi-password flow). Make sure the matching
  `zoneinfo-*` opkg package is installed for any region you add cities from.
- **Currencies offered**: `CURRENCIES` list.
- **Data cap presets**: `DATA_CAP_PRESETS`.
- **Monitor's tracked interface**: auto-detected (`get_wan_iface()` follows
  whichever interface holds the lowest-metric default route) rather than a
  fixed name, so it keeps working across a repeater-WiFi/cellular failover
  without editing anything.
- User's actual picks (which city/currency/cap) persist at
  `/root/dashboard/config.json`, separate from the source.

## Known limitations

- **SIM2 vs eSIM**: this hardware shares one physical slot (slot 2) between
  a physical nano-SIM and the eSIM profile. There's no confirmed-safe
  documented `ubus` call to distinguish "activate eSIM profile" from
  "activate physical SIM2" specifically — both buttons currently just
  reorder modem slot priority to prefer slot 2. If you rely on eSIM
  specifically, verify this does what you expect before trusting it.
- **OpenClash node country** is guessed by keyword-matching the node's
  display name (`UK`, `Japan`, `HK`, ...) — accuracy depends entirely on
  your subscription's naming convention. No live GeoIP lookup.
- **Repeater scan is slow** (~5-8s on this hardware, `ubus call repeater
  scan '{"cached":true}'` is not actually fast despite the flag name) — it
  runs in a background thread so the UI doesn't freeze, but the network
  list takes a few seconds to populate after opening the Repeater screen.
- Fonts on this firmware (`/etc/gl_screen/language/ttf/`) render `‹ › ✓`
  fine but silently box unsupported Unicode (confirmed failures: `⇧ ⌫ 🔒`).
  Anything beyond plain ASCII + those three marks is drawn as a small PIL
  icon rather than assumed to render as text — see `_icon_lock`,
  `_icon_wifi_signal`, `draw_analog_clock` for the pattern if you add UI.
- **Monitor's bandwidth reading only reflects traffic that actually passes
  through this router.** A client connected directly to some *other*
  router's WiFi (bypassing this one entirely) will correctly show as 0 —
  there's no way to see traffic that never touches this device.
- `thermal_zone0` on this SoC (`sdr0`) is an unpowered sensor that always
  reports a `-273000` millidegree sentinel — `get_temp_c()` scans
  `/sys/class/thermal/thermal_zone*/type` for a known-good sensor name
  instead of assuming zone 0 is real. If you're porting this to another
  board, don't assume zone numbering means anything either — check `type`.

## Adapting to another model

The parts of this code that are GL-E5800-specific are isolated at the top
of `dashboard.py` and in `button_watch.py`:

- `W, H` and the RGB565 packing in `to_rgb565_bytes` — check your model's
  actual framebuffer size/format (`cat /sys/class/graphics/fb0/virtual_size`,
  `.../bits_per_pixel`).
- `TOUCH_DEV` and the multitouch event codes in `_touch_reader` — capture
  raw events while touching the screen to confirm your model reports the
  same protocol (see the calibration approach described in this repo's
  companion write-up, or just `cat /dev/input/eventN | xxd` while tapping).
- `BACKLIGHT_PATH` in `dashboard.py` and `screen_sleep.sh` — check
  `/sys/class/backlight/*/brightness` exists on your model.
- `DEV = "/dev/input/event1"` / `KEY_POWER` in `button_watch.py` — confirm
  which event node your model's power key reports on.

If your GL.iNet model doesn't have a `repeater` or `cellular.*` ubus object,
the Repeater tile and Active SIM panel will need adjusting or removing.

## License

MIT — see [LICENSE](LICENSE).
