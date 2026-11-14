#!/bin/bash
# Install/upgrade/remove/purge test for the redstonepanel .deb. Run as root inside a
# disposable systemd container (see Dockerfile). Prints PASS/FAIL per check.
#   test-install.sh [--no-install-recommends]
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
FAILS=0
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
code() { curl -s -o /tmp/body -w '%{http_code}' "$@"; }
wait_up() { for _ in $(seq 1 30); do curl -sfkL -o /dev/null "$1" && return 0; sleep 1; done; return 1; }

B=http://127.0.0.1:8080
J="Content-Type: application/json"
O="Origin: $B"
DEB=$(ls /dist/redstonepanel_*_all.deb)
. /etc/os-release; echo "=== Ubuntu $VERSION_ID, installing $(basename "$DEB") $*"

apt-get update -qq
apt-get install -y -qq "$@" "$DEB" > /tmp/install.log 2>&1 || { tail -30 /tmp/install.log; echo "FAIL  install"; exit 1; }
ok "apt install"
grep -A4 "RedstonePanel is running" /tmp/install.log | head -6
echo "  python3-fastapi $(dpkg-query -W -f='${Version}' python3-fastapi), uvicorn $(dpkg-query -W -f='${Version}' python3-uvicorn), multipart: $(dpkg-query -W -f='${Package} ${Version}' python3-python-multipart python3-multipart 2>/dev/null | tr '\n' ' ')"
echo "  java: $(command -v java || echo 'not installed')"

# --- service, users, files
check "service active"                    systemctl is-active --quiet redstonepanel
check "service enabled"                   systemctl is-enabled --quiet redstonepanel
check "group redstonepanel exists"        getent group redstonepanel
check "user minecraft exists"             id minecraft
check "/var/lib/redstonepanel is 0700"    test "$(stat -c %a /var/lib/redstonepanel)" = 700
check "/var/log/redstonepanel exists"     test -d /var/log/redstonepanel
check "man page in package"               bash -c 'dpkg -L redstonepanel | grep -q man8/redstonepanel.8.gz'
wait_up $B/api/health && ok "API answers" || bad "API answers"
check "local Tailwind CSS served"          test "$(code $B/static/css/tailwind.css)" = 200
check "UI has no CDN references"          bash -c '! grep -rq "cdn.tailwindcss" /usr/share/redstonepanel/static'
check "no login -> 401"                   test "$(code $B/api/servers)" = 401

# --- Linux sign-in through PAM (test accounts exist only in this throwaway container)
useradd -m -s /bin/bash -G sudo rpadmin && echo 'rpadmin:Linux-test-pw-1' | chpasswd
useradd -m -s /bin/bash rpnobody && echo 'rpnobody:Linux-test-pw-2' | chpasswd
L=/tmp/linux.cookies
check "Linux sudo user signs in (PAM)"    test "$(code -c $L -X POST $B/api/auth/login -H "$J" -H "$O" -d '{"username":"rpadmin","password":"Linux-test-pw-1"}')" = 200
grep -q '"source":"linux"' /tmp/body && ok "  ...as a Linux admin" || bad "  ...as a Linux admin ($(cat /tmp/body))"
check "wrong Linux password -> 401"       test "$(code -X POST $B/api/auth/login -H "$J" -H "$O" -d '{"username":"rpadmin","password":"nope-nope"}')" = 401
check "user outside sudo group -> 401"    test "$(code -X POST $B/api/auth/login -H "$J" -H "$O" -d '{"username":"rpnobody","password":"Linux-test-pw-2"}')" = 401
for ep in servers templates users settings logs auth/me; do
    check "GET /api/$ep as admin"         test "$(code -b $L $B/api/$ep)" = 200
done

# --- CLI
echo 'Panel-test-pw-1' | redstonepanel add-user webadmin >/dev/null && ok "redstonepanel add-user" || bad "redstonepanel add-user"
check "panel user signs in"               test "$(code -X POST $B/api/auth/login -H "$J" -H "$O" -d '{"username":"webadmin","password":"Panel-test-pw-1"}')" = 200
redstonepanel status | sed 's/^/  /'
check "users.json is 0600"                test "$(stat -c %a /var/lib/redstonepanel/users.json)" = 600
check "panel.log in /var/log"             test -s /var/log/redstonepanel/panel.log

# --- missing Java gives a clear error instead of a broken deploy
if ! command -v java >/dev/null; then
    c=$(code -b $L -X POST $B/api/servers/deploy -H "$J" -H "$O" -d '{"edition":"java"}')
    [ "$c" = 400 ] && grep -q openjdk-25-jre-headless /tmp/body && ok "Java deploy without Java -> clear 400" || bad "Java deploy without Java ($c $(cat /tmp/body))"
fi

# --- HTTPS switch from Panel Settings, restart by systemd
c=$(code -b $L -X PUT $B/api/settings -H "$J" -H "$O" -d '{"https_mode":"self_signed","http_port":8080,"https_port":8443,"login_mode":"required"}')
[ "$c" = 200 ] && ok "enable self-signed HTTPS" || bad "enable self-signed HTTPS ($c $(cat /tmp/body))"
sleep 3
wait_up https://127.0.0.1:8443/api/health && ok "HTTPS up on 8443 after self-restart" || bad "HTTPS up on 8443"
check "HTTPS verifies with the panel CA"  curl -sf --cacert /var/lib/redstonepanel/tls/ca.crt https://127.0.0.1:8443/api/health
check "8080 redirects (302)"              test "$(curl -s -o /dev/null -w '%{http_code}' $B/)" = 302
check "TLS keys are root-only"            test "$(stat -c %a /var/lib/redstonepanel/tls/server.key)" = 600
redstonepanel reset-security >/dev/null && sleep 3
wait_up $B/api/health && ok "reset-security -> back on HTTP 8080" || bad "reset-security"

# --- reinstall (upgrade path) keeps data and restarts
apt-get install -y -qq --reinstall "$DEB" >/dev/null 2>&1 && ok "reinstall" || bad "reinstall"
wait_up $B/api/health && ok "running after reinstall" || bad "running after reinstall"
check "panel user kept after reinstall"   grep -q webadmin /var/lib/redstonepanel/users.json

# --- remove keeps data, purge deletes panel data but never game servers
mkdir -p /opt/minecraft-test/world && echo keep > /opt/minecraft-test/world/level.dat
apt-get remove -y -qq redstonepanel >/dev/null 2>&1 && ok "apt remove" || bad "apt remove"
check "service gone after remove"         bash -c '! systemctl is-active --quiet redstonepanel'
check "data kept after remove"            test -f /var/lib/redstonepanel/users.json
apt-get purge -y -qq redstonepanel >/dev/null 2>&1 && ok "apt purge" || bad "apt purge"
check "panel data deleted on purge"       test ! -e /var/lib/redstonepanel
check "game server untouched by purge"    test -f /opt/minecraft-test/world/level.dat

echo "=== $([ $FAILS = 0 ] && echo 'ALL PASSED' || echo "$FAILS FAILED") on Ubuntu $VERSION_ID"
exit $FAILS
