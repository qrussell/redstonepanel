#!/bin/bash
# Installs the newest redstonepanel .deb from the staging folder (development updates).
# Install once (as root):   install -o root -g root -m 755 redstonepanel-deploy.sh /usr/local/sbin/redstonepanel-deploy
# Usage:                    sudo /usr/local/sbin/redstonepanel-deploy
set -euo pipefail

SRC=${REDSTONEPANEL_STAGING:-/home/qrussell/redstonepanel-staging}
DEB=$(ls -t "$SRC"/redstonepanel_*_all.deb 2>/dev/null | head -1)
[ -n "$DEB" ] || { echo "No redstonepanel_*_all.deb in $SRC" >&2; exit 1; }
[ "$(dpkg-deb -f "$DEB" Package)" = redstonepanel ] || { echo "$DEB is not the redstonepanel package" >&2; exit 1; }
echo "installing $(basename "$DEB") ($(dpkg-deb -f "$DEB" Version))"

# --reinstall: development builds may reuse a version number
apt-get install -y -q --reinstall "$DEB" | grep -E "^(Setting up|Unpacking) redstonepanel" || true
systemctl restart redstonepanel.service

PORT=$(python3 -c "
import json
try: print(int(json.load(open('/var/lib/redstonepanel/settings.json')).get('http_port', 8080)))
except Exception: print(8080)")
for _ in $(seq 1 15); do
    # /api/health needs no login; then confirm the API is still locked down
    if curl -sfkL -o /dev/null "http://127.0.0.1:$PORT/api/health"; then
        code=$(curl -skL -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/servers")
        if [ "$code" != "401" ]; then
            echo "WARNING: /api/servers answered HTTP $code without a login (expected 401)" >&2
            exit 1
        fi
        echo "redstonepanel is up"
        exit 0
    fi
    sleep 1
done
echo "redstonepanel did not answer after install; check: journalctl -u redstonepanel -n 50" >&2
exit 1
