# Building the .ipk

This produces a real opkg package (`gl-e5800-dashboard_<version>_all.ipk`)
that can be installed either with `opkg install` over SSH, or through
LuCI's **System → Software → Upload Package...** with no SSH at all.

```sh
./build.sh
```

No OpenWrt SDK or cross-toolchain needed -- everything shipped is pure
Python/shell, nothing to compile. Needs a plain POSIX `tar` (tested with
both macOS's `bsdtar` and Linux's GNU tar).

`Architecture: all` is used deliberately (see `control/control`) since
there's no compiled code -- this isn't tied to `aarch64_cortex-a53`
specifically, only to the GL-E5800's filesystem paths (`/root/dashboard`,
`/etc/init.d/*`) and hardware (framebuffer/touch/backlight/power-key), the
same constraints described in the main README's
[Adapting to another model](../README.md#adapting-to-another-model)
section.

## ipk format gotcha

An `.ipk` looks like it should be an `ar` archive (that's the classic
ipkg/dpkg convention most guides describe: `debian-binary` +
`control.tar.gz` + `data.tar.gz` wrapped in `ar`). **It isn't, on this
opkg build.** It's a plain gzip-compressed tar containing those same three
files, in this order: `debian-binary`, `data.tar.gz`, `control.tar.gz`.
Building it as an `ar` archive makes real opkg reject it with
`pkg_init_from_file: Malformed package file` -- found by pulling a real
`.ipk` from GL.iNet's own feed (`opkg download <pkg>`) and inspecting it
byte-for-byte, after the `ar`-based build failed against the real device.
`build.sh` does it the tar way.

## postinst can't call opkg

opkg holds a single non-reentrant lock for the whole install transaction.
Calling `opkg` for *anything* -- even a read-only `list-installed` -- from
inside this package's own `postinst` deadlocks on that same lock
(`Could not lock /var/lock/opkg.lock: Resource temporarily unavailable`),
which aborts the postinst and leaves the package half-configured. This is
why `python3-pillow` (which conflicts with a file `gl-sdk4-screen-large`
already owns, so it needs `--nodeps` and can't be a normal `Depends`
either) is only *checked for* in postinst (`python3 -c "import PIL"`, no
opkg involved) with an instruction to run `opkg install --nodeps
python3-pillow` by hand if it's missing, rather than installed
automatically from there.

## What the scripts do

- **postinst**: checks for Pillow (see above), sets executable bits,
  enables and starts `homebutton` (the power-button watcher -- safe, runs
  independent of which screen UI is active). Does **not** enable or start
  `citydash` / switch the physical screen -- that stays a manual
  `/root/dashboard/toggle.sh on` step, same as the existing manual install
  flow, so installing the package never surprises you with a live UI swap.
- **prerm**: if the dashboard is currently the active screen, switches
  back to the stock `gl_screen` UI first (`toggle.sh off`) before its
  files are removed, so uninstalling can never leave the screen blank or
  crash-looping. Then stops/disables `homebutton`.
- **postrm**: removes the runtime-generated files that were never part of
  the package payload (`config.json`, cached weather/fx JSON, the preview/
  folder) for a genuinely clean uninstall.

Tested end-to-end against a real device for this project (install, remove
while the dashboard was live to confirm the stock-UI fallback, reinstall)
-- not just built and assumed correct.
