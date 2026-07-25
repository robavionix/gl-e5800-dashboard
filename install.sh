#!/bin/sh
# Run from a machine with SSH access to the router (not on the router itself).
# Usage: ./install.sh [router-ip]   (default: 192.168.8.1)

set -e
ROUTER="${1:-192.168.8.1}"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing dependencies on root@$ROUTER"
ssh "root@$ROUTER" '
    opkg update
    opkg install python3 python3-numpy
    opkg install zoneinfo-europe zoneinfo-asia zoneinfo-america zoneinfo-australia-nz zoneinfo-pacific
    opkg install --nodeps python3-pillow
    opkg install libtiff6
    mkdir -p /root/dashboard
'

echo "==> Copying files"
scp -O "$DIR"/src/*.py "$DIR"/src/*.sh "root@$ROUTER:/root/dashboard/"
scp -O "$DIR"/init.d/citydash "$DIR"/init.d/homebutton "root@$ROUTER:/etc/init.d/"

echo "==> Setting permissions, enabling the power-button watcher"
ssh "root@$ROUTER" '
    chmod +x /root/dashboard/*.py /root/dashboard/*.sh /etc/init.d/citydash /etc/init.d/homebutton
    /etc/init.d/homebutton enable
    /etc/init.d/homebutton start
'

cat <<EOF

Installed. The stock screen is still active -- nothing changed yet.

Preview first (renders to PNGs, does not touch the live screen):
  ssh root@$ROUTER python3 /root/dashboard/dashboard.py --preview /root/dashboard/preview

When you're ready to switch the physical screen over:
  ssh root@$ROUTER /root/dashboard/toggle.sh on

To go back to the stock GL.iNet UI at any time:
  ssh root@$ROUTER /root/dashboard/toggle.sh off
  (or triple-press the power button on the device itself)
EOF
