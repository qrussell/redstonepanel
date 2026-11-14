#!/bin/bash
# Rehearses deploy/migrate-to-deb.sh: builds an "old style" /opt/redstonepanel install like the
# live server, signs in, migrates to the .deb, and checks nothing was lost. Run as root in a
# disposable systemd container with the source at /src and the .deb in /dist.
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
FAILS=0
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
wait_up() { for _ in $(seq 1 30); do curl -sfkL -o /dev/null "$1" && return 0; sleep 1; done; return 1; }
B=http://127.0.0.1:8080; J="Content-Type: application/json"; O="Origin: $B"; C=/tmp/c

echo "== old-style install in /opt/redstonepanel (systemd unit in /etc, data next to the code)"
apt-get update -qq
apt-get install -y -qq python3-fastapi python3-uvicorn python3-python-multipart openssl curl >/dev/null 2>&1
mkdir -p /opt/redstonepanel
for f in main.py run.py https_redirect.py core static; do cp -r /src/$f /opt/redstonepanel/; done
find /opt/redstonepanel -type f -exec sed -i 's/\r$//' {} +
rm -rf /opt/redstonepanel/core/__pycache__
cat > /etc/systemd/system/redstonepanel.service <<EOF
[Service]
WorkingDirectory=/opt/redstonepanel
ExecStart=/usr/bin/python3 /opt/redstonepanel/run.py
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now redstonepanel >/dev/null 2>&1
echo 'Old-panel-pw-1' | python3 /opt/redstonepanel/run.py add-user olduser >/dev/null
echo '[{"id":"t1","name":"tmpl","edition":"Java","properties":{},"permissions":"[]"}]' > /opt/redstonepanel/data/templates.json
wait_up $B/api/health && ok "old install running" || bad "old install running"
curl -s -c $C -o /dev/null -X POST $B/api/auth/login -H "$J" -H "$O" -d '{"username":"olduser","password":"Old-panel-pw-1"}'
check "signed in to old install"          test "$(curl -s -b $C -o /dev/null -w '%{http_code}' $B/api/auth/me)" = 200
mkdir -p /opt/minecraft-java/world && echo keep > /opt/minecraft-java/world/level.dat

echo "== migrate"
sed 's/\r$//' /src/deploy/migrate-to-deb.sh > /root/migrate.sh
bash /root/migrate.sh /dist/redstonepanel_*_all.deb 2>&1 | grep -vE "^(Get|Selecting|Preparing|Unpacking|Setting up|Processing|Reading|Building|Need|After|The following|  )" | sed 's/^/  | /'

echo "== after migration"
check "service runs the packaged code"    bash -c 'systemctl show -p ExecStart --value redstonepanel | grep -q /usr/share/redstonepanel/run.py'
check "service active"                    systemctl is-active --quiet redstonepanel
check "old /etc unit removed"             test ! -e /etc/systemd/system/redstonepanel.service
check "old session still valid"           test "$(curl -s -b $C -o /dev/null -w '%{http_code}' $B/api/auth/me)" = 200
check "panel user migrated"               grep -q olduser /var/lib/redstonepanel/users.json
check "template migrated"                 grep -q tmpl /var/lib/redstonepanel/templates.json
check "secret.key migrated"               test -s /var/lib/redstonepanel/secret.key
check "data dir root-only"                test "$(stat -c %a /var/lib/redstonepanel)" = 700
check "old install kept for rollback"     bash -c 'ls -d /opt/redstonepanel.pre-deb-* '
check "/opt/redstonepanel moved away"     test ! -e /opt/redstonepanel
check "game server untouched"             test -f /opt/minecraft-java/world/level.dat

echo "== development update through the new deploy script"
mkdir -p /home/qrussell/redstonepanel-staging && cp /dist/redstonepanel_*_all.deb /home/qrussell/redstonepanel-staging/
sed 's/\r$//' /src/deploy/redstonepanel-deploy.sh > /root/deploy.sh
bash /root/deploy.sh 2>&1 | sed 's/^/  | /'
check "deploy script works"               systemctl is-active --quiet redstonepanel
check "still signed in after deploy"      test "$(curl -s -b $C -o /dev/null -w '%{http_code}' $B/api/auth/me)" = 200

echo "=== $([ $FAILS = 0 ] && echo 'ALL PASSED' || echo "$FAILS FAILED")"
exit $FAILS
