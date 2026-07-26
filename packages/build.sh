#!/bin/sh
# Builds the gl-e5800-dashboard .ipk directly from ../src and ../init.d --
# no OpenWrt SDK/cross-toolchain needed, since everything shipped is pure
# Python/shell with nothing to compile. Needs a plain POSIX tar (macOS's
# bsdtar or Linux's GNU tar both work).
#
# ipk format note: despite looking similar to a .deb, this is NOT an ar
# archive -- it's a plain gzip-compressed tar containing debian-binary,
# data.tar.gz, and control.tar.gz, in that order. Confirmed by pulling a
# real .ipk from GL.iNet's own opkg feed and inspecting it byte-for-byte;
# building it as an ar archive (the classic ipkg/dpkg convention many
# guides describe) makes real opkg reject it as "Malformed package file".
set -e
cd "$(dirname "$0")"

PKG_NAME=gl-e5800-dashboard
VERSION=$(awk -F': ' '/^Version:/ {print $2}' control/control)
OUT="${PKG_NAME}_${VERSION}_all.ipk"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/data/root/dashboard" "$WORK/data/etc/init.d"
cp ../src/*.py ../src/*.sh "$WORK/data/root/dashboard/"
cp ../init.d/citydash ../init.d/homebutton "$WORK/data/etc/init.d/"
chmod 755 "$WORK/data/root/dashboard/"* "$WORK/data/etc/init.d/"*

mkdir -p "$WORK/control"
cp control/control control/postinst control/prerm control/postrm "$WORK/control/"
chmod 644 "$WORK/control/control"
chmod 755 "$WORK/control/postinst" "$WORK/control/prerm" "$WORK/control/postrm"

echo "2.0" > "$WORK/debian-binary"

export COPYFILE_DISABLE=1
OWNER_FLAGS="--uid=0 --gid=0 --uname=root --gname=root"
tar --format=gnutar $OWNER_FLAGS -czf "$WORK/control.tar.gz" -C "$WORK/control" .
tar --format=gnutar $OWNER_FLAGS -czf "$WORK/data.tar.gz" -C "$WORK/data" .

rm -f "$OUT"
tar --format=gnutar $OWNER_FLAGS -czf "$OUT" -C "$WORK" debian-binary data.tar.gz control.tar.gz

echo "wrote $OUT"
