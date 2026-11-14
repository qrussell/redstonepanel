#!/bin/bash
# One-time switch of an existing RedstonePanel install to the run.py launcher.
#   sudo bash ~/migrate-to-launcher.sh
#
# After this, HTTP/HTTPS and sign-in mode are chosen in the panel (Panel Settings) instead of
# in systemd. It removes the HTTPS override + redirect service made by redstonepanel-https.sh,
# and the panel comes back as plain HTTP on port 8080 with sign-in required.
# Existing certificates in /opt/redstonepanel/tls are left in place.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run this with sudo." >&2; exit 1; }

APP=/opt/redstonepanel
USER_HOME=$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)

# 1. New deploy script, then install the staged code (incl. run.py) while the old setup still runs
install -o root -g root -m 755 "$USER_HOME/redstonepanel-deploy.sh" /usr/local/sbin/redstonepanel-deploy
/usr/local/sbin/redstonepanel-deploy

# 2. Remove the old systemd-level HTTPS (drop-in + separate redirect service)
systemctl disable --now redstonepanel-redirect.service 2>/dev/null || true
rm -f /etc/systemd/system/redstonepanel-redirect.service
rm -f /etc/systemd/system/redstonepanel.service.d/https.conf
rmdir /etc/systemd/system/redstonepanel.service.d 2>/dev/null || true

# 3. The service now starts run.py, which reads data/settings.json
cat > /etc/systemd/system/redstonepanel.service <<EOF
[Unit]
Description=RedstonePanel Web Dashboard and API
After=network.target

[Service]
User=root
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python $APP/run.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl restart redstonepanel.service
for _ in $(seq 1 15); do
    if curl -sfkL -o /dev/null http://127.0.0.1:8080/api/health; then
        echo
        echo "Done. Panel: http://$(hostname -I | awk '{print $1}'):8080/  (HTTPS and sign-in options: Panel Settings)"
        echo "Locked out later? sudo $APP/venv/bin/python $APP/run.py reset-security"
        exit 0
    fi
    sleep 1
done
echo "Panel did not answer on port 8080; check: journalctl -u redstonepanel -n 50" >&2
exit 1
