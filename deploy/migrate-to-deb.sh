#!/bin/bash
# One-time: replace a manual /opt/redstonepanel install with the redstonepanel .deb.
#   sudo bash ~/migrate-to-deb.sh ~/redstonepanel-staging/redstonepanel_1.0.0_all.deb
#
# Keeps everything: panel accounts, settings, session key (people stay signed in),
# certificates, templates, chat history and the activity log move to the package's
# locations (/var/lib/redstonepanel, /var/log/redstonepanel). The old install is renamed,
# not deleted, so it can be rolled back. Minecraft servers (/opt/minecraft-*) aren't touched.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run this with sudo." >&2; exit 1; }
DEB=$(readlink -f "${1:?Usage: sudo bash migrate-to-deb.sh /path/to/redstonepanel_X_all.deb}")
OLD=/opt/redstonepanel
STAMP=$(date +%Y%m%d-%H%M%S)
KEEP=$OLD.pre-deb-$STAMP
[ -d "$OLD" ] || { echo "$OLD not found; nothing to migrate (just: sudo apt install $DEB)" >&2; exit 1; }
[ "$(dpkg-deb -f "$DEB" Package)" = redstonepanel ] || { echo "$DEB is not the redstonepanel package" >&2; exit 1; }

echo "== 1. Stop the old panel and remove its systemd units (they would override the package's)"
systemctl stop redstonepanel.service 2>/dev/null || true
systemctl disable --now redstonepanel-redirect.service 2>/dev/null || true
[ -f /etc/systemd/system/redstonepanel.service ] && mv /etc/systemd/system/redstonepanel.service "$OLD/redstonepanel.service.pre-deb"
rm -f /etc/systemd/system/redstonepanel-redirect.service /etc/systemd/system/multi-user.target.wants/redstonepanel.service
rm -rf /etc/systemd/system/redstonepanel.service.d
systemctl daemon-reload

echo "== 2. Copy panel state to the package locations"
install -d -m 700 -o root -g root /var/lib/redstonepanel
install -d -m 750 -o root -g adm /var/log/redstonepanel
[ -d "$OLD/data" ] && cp -a "$OLD/data/." /var/lib/redstonepanel/
[ -d "$OLD/tls" ]  && cp -a "$OLD/tls" /var/lib/redstonepanel/
[ -f "$OLD/panel.log" ] && cp -a "$OLD/panel.log" /var/log/redstonepanel/panel.log
chown -R root:root /var/lib/redstonepanel
ls -A /var/lib/redstonepanel | sed 's/^/   /'

echo "== 3. Install the package (dependencies come from Ubuntu)"
apt-get install -y "$DEB"

# Development updates from now on install .deb files (same sudoers rule, new script)
USER_HOME=$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)
if [ -f "$USER_HOME/redstonepanel-deploy.sh" ]; then
    install -o root -g root -m 755 "$USER_HOME/redstonepanel-deploy.sh" /usr/local/sbin/redstonepanel-deploy
    echo "   updated /usr/local/sbin/redstonepanel-deploy (now installs .deb files)"
fi

echo "== 4. Check the panel"
PORT=$(python3 -c "
import json
try: print(int(json.load(open('/var/lib/redstonepanel/settings.json')).get('http_port', 8080)))
except Exception: print(8080)")
for _ in $(seq 1 20); do
    if curl -sfkL -o /dev/null "http://127.0.0.1:$PORT/api/health"; then
        mv "$OLD" "$KEEP"
        cat <<EOF

Migration done. Panel: http://$(hostname -I | awk '{print $1}'):$PORT/   ($(systemctl show -p ExecStart --value redstonepanel | grep -o '/usr/share/redstonepanel/run.py'))
Old install kept at $KEEP (delete it once you're happy).

Roll back if needed:
  sudo apt remove redstonepanel
  sudo mv $KEEP $OLD
  sudo mv $OLD/redstonepanel.service.pre-deb /etc/systemd/system/redstonepanel.service
  sudo systemctl daemon-reload && sudo systemctl enable --now redstonepanel
EOF
        exit 0
    fi
    sleep 1
done
echo "The packaged panel did not answer on port $PORT. Old install is still at $OLD." >&2
echo "Check: journalctl -u redstonepanel -n 50" >&2
exit 1
