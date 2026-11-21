#!/bin/bash
# End-to-end test of update alerts and automatic Bedrock updates (1.16.12) in one throwaway
# systemd container that installs the .deb from dist/. The Bedrock server is a stand-in (a
# bedrock_server script that logs like BDS and echoes what's typed into its console), and
# Mojang's download-links API is a local stand-in served on 127.0.0.1:8099 (RP_BEDROCK_LINKS),
# so nothing is downloaded from the internet. Real Bedrock downloads are tested by hand (see
# the roadmap's As built notes).
# Run from the repository root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-bedrock-update.sh          (KEEP=1 leaves the container running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
C=rp-bedrock-update
IMG=rp-systemd:24.04
PASS_ADMIN='update-test-admin-1'        # a throwaway account inside the test container only
SRV=minecraft-bedrock
D=/opt/$SRV
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$C" bash -c "$1"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
api()  { dx "curl -s -b /tmp/jar -X $1 'http://127.0.0.1:8080$2' ${3:+-H 'Content-Type: application/json' -d '$3'}"; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
json() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
# started: the stand-in logged "Server started." for VERSION after its last start
# (screen writes its log with CRLF line ends)
started() { dx "tr -d '\r' < $D/server.log 2>/dev/null | tac | sed '/Version: /q' > /tmp/last-start; grep -q 'Server started' /tmp/last-start && grep -q 'Version: $1\$' /tmp/last-start"; }
wait_started() { for _ in $(seq 1 60); do started "$1" && return 0; sleep 1; done; return 1; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Container kept: $C (docker rm -f $C)"; return; }
    docker rm -f "$C" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== Bedrock update test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f "$C" >/dev/null 2>&1
docker run -d --privileged --cgroupns=private --name "$C" --hostname "$C" -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
sleep 3
dx "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") screen zip python3 >/tmp/install.log 2>&1" \
    || { echo "install failed"; dx "tail -20 /tmp/install.log"; exit 1; }
check "panel installed and running" dx "systemctl is-active --quiet redstonepanel"
for _ in $(seq 1 30); do dx "systemctl is-enabled --quiet redstonepanel-updates.timer" && break; sleep 1; done
check "the panel set up the update check timer" dx "systemctl is-enabled --quiet redstonepanel-updates.timer && systemctl is-active --quiet redstonepanel-updates.timer"
check "it runs every 6 hours" dx "grep -q 'OnCalendar=\*-\*-\* 00/6:15:00' /etc/systemd/system/redstonepanel-updates.timer"

# --- Mojang stand-in: links API and release zips on 127.0.0.1:8099; the panel and the timer use it
# mkrelease VERSION [broken]: a release zip whose bedrock_server logs VERSION; "broken" ones log
# their version, scribble on the world (as a newer BDS converting it would) and die.
mkrelease() {
    dx "rm -rf /tmp/rel && mkdir -p /tmp/rel /tmp/mojang/bin-linux && cd /tmp/rel \
        && printf 'server-port=19132\nserver-name=Dedicated Server\nlevel-name=Bedrock level\n' > server.properties \
        && printf '[]' > permissions.json && printf '[]' > allowlist.json && echo '$1' > release-notes.txt"
    if [ "${2:-}" = broken ]; then
        dx "printf '#!/bin/bash\necho \"[INFO] Version: $1\"\necho converted >> worlds/Home/db/CURRENT\necho \"[ERROR] crashed\"\nexit 1\n' > /tmp/rel/bedrock_server"
    else
        dx "printf '#!/bin/bash\necho \"[INFO] Version: $1\"\necho \"[INFO] Server started.\"\nwhile read -r line; do echo \"console: \$line\"; [ \"\$line\" = stop ] && { echo \"Quit correctly\"; exit 0; }; done\nsleep infinity\n' > /tmp/rel/bedrock_server"
    fi
    dx "chmod +x /tmp/rel/bedrock_server && cd /tmp/rel && rm -f /tmp/mojang/bin-linux/bedrock-server-$1.zip && zip -qr /tmp/mojang/bin-linux/bedrock-server-$1.zip . \
        && printf '{\"result\":{\"links\":[{\"downloadType\":\"serverBedrockWindows\",\"downloadUrl\":\"http://127.0.0.1:8099/bin-win/bedrock-server-9.9.9.9.zip\"},{\"downloadType\":\"serverBedrockLinux\",\"downloadUrl\":\"http://127.0.0.1:8099/bin-linux/bedrock-server-$1.zip\"}]}}' > /tmp/mojang/links.json"
}
mkrelease 1.0.0.2
dx "systemd-run --unit=fake-mojang python3 -m http.server 8099 --bind 127.0.0.1 --directory /tmp/mojang >/dev/null 2>&1"
ENVS="[Service]\nEnvironment=RP_BEDROCK_LINKS=http://127.0.0.1:8099/links.json\nEnvironment=RP_UPDATE_WARN_MINUTES=1\n"
dx "for u in redstonepanel redstonepanel-updates; do mkdir -p /etc/systemd/system/\$u.service.d && printf '$ENVS' > /etc/systemd/system/\$u.service.d/test.conf; done \
    && systemctl daemon-reload && systemctl restart redstonepanel"
for _ in $(seq 1 30); do dx "curl -sf http://127.0.0.1:8099/links.json" >/dev/null 2>&1 && dx "curl -sf http://127.0.0.1:8080/api/health" >/dev/null 2>&1 && break; sleep 1; done

dx "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
dx "curl -s -c /tmp/jar -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PASS_ADMIN\"}' http://127.0.0.1:8080/api/auth/login" >/dev/null

# --- a stand-in Bedrock 1.0.0.1 server, imported and started
dx "rm -rf /tmp/srv && mkdir -p /tmp/srv/worlds/Home/db && cd /tmp/srv \
    && printf '#!/bin/bash\necho \"[INFO] Version: 1.0.0.1\"\necho \"[INFO] Server started.\"\nwhile read -r line; do echo \"console: \$line\"; [ \"\$line\" = stop ] && { echo \"Quit correctly\"; exit 0; }; done\nsleep infinity\n' > bedrock_server && chmod +x bedrock_server \
    && printf 'server-port=19140\nserver-portv6=19141\nlevel-name=Home\nserver-name=Island\n' > server.properties \
    && printf '[{\"permission\":\"operator\",\"xuid\":\"123\"}]' > permissions.json && echo world-v1 > worlds/Home/db/CURRENT && zip -qr /tmp/srv.zip ."
NAME=$(dx "curl -s -b /tmp/jar -F file=@/tmp/srv.zip http://127.0.0.1:8080/api/servers/import" | json "d['server']")
[ "$NAME" = "$SRV" ] && ok "stand-in Bedrock server imported" || bad "import ($NAME)"
api POST "/api/servers/$SRV/action" '{"action":"start"}' >/dev/null
wait_started 1.0.0.1 && ok "it runs 1.0.0.1" || { bad "started 1.0.0.1"; dx "cat $D/server.log"; }

# --- the alert
U=$(api GET "/api/servers/$SRV/updates")
echo "$U" | json "d['edition'] == 'bedrock' and d['supported'] and not d['auto'] and d['current'] == '1.0.0.1'" | grep -qx True \
    && ok "Maintenance shows Bedrock, off by default, runs 1.0.0.1" || { bad "updates overview"; echo "$U"; }
U=$(api POST "/api/servers/$SRV/updates/check")
echo "$U" | json "d['update']['label']" | grep -qx "Bedrock 1.0.0.2" && ok "Check now finds Bedrock 1.0.0.2" || { bad "check now"; echo "$U"; }
api GET /api/servers | json "[s['update']['latest'] for s in d if s['name'] == '$SRV'][0]" | grep -qx 1.0.0.2 \
    && ok "the server list carries the alert (sidebar, dashboard, cluster master)" || bad "server list alert"
dx "redstonepanel check-updates" | grep -q "Bedrock 1.0.0.2 is available" && ok "check-updates reports it" || bad "check-updates output"
check "off by default: nothing was updated" started 1.0.0.1

# --- turning it on, and the timer's run: warn, back up, update, start
[ "$(api PUT "/api/servers/$SRV/updates" '{"enabled":true}' | json "d['auto']")" = True ] && ok "Update automatically turned on" || bad "turn on"
dx "grep -q '$SRV' /var/lib/redstonepanel/auto-updates.json" && ok "kept in auto-updates.json" || bad "auto-updates.json"
dx "systemctl start redstonepanel-updates.service"        # waits for the 1-minute warning (RP_UPDATE_WARN_MINUTES=1)
check "players were warned in game" dx "grep -q 'console: say Server updates to the new Minecraft version in 1 minute' $D/server.log"
check "and 10 seconds before" dx "grep -q 'console: say Server updates in 10 seconds' $D/server.log"
wait_started 1.0.0.2 && ok "the server runs 1.0.0.2 after the timer's run" || { bad "updated to 1.0.0.2"; dx "tail -30 /var/log/redstonepanel/panel.log"; }
check "it stopped cleanly first" dx "grep -q 'Quit correctly' $D/server.log"
check "a Before update backup was taken" dx "ls /var/backups/redstonepanel/$SRV/ | grep -q 'pre-update.tar.gz$'"
dx "grep -q '^server-name=Island' $D/server.properties" && ok "its server.properties is kept" || { bad "server.properties kept"; dx "cat $D/server.properties"; }
check "its permissions.json is kept" dx "grep -q operator $D/permissions.json"
check "its world is untouched" dx "grep -qx world-v1 $D/worlds/Home/db/CURRENT"
check "the new files belong to minecraft" dx "[ \$(stat -c %U $D/release-notes.txt) = minecraft ]"
check "no staging folder is left" dx "! ls -d $D/.redstonepanel-update-* 2>/dev/null"
U=$(api GET "/api/servers/$SRV/updates")
echo "$U" | json "d['update'] is None and d['last']['ok'] and d['last']['from'] == '1.0.0.1' and d['last']['to'] == '1.0.0.2'" | grep -qx True \
    && ok "the alert is gone and the last update is shown" || { bad "after update"; echo "$U"; }
api GET /api/servers/$SRV/backups | grep -q '"pre-update"' && ok "the Backups tab lists the Before update backup" || bad "backup listed"

# --- a release that won't start: the old version and its world go back
mkrelease 1.0.0.3 broken
dx "RP_BEDROCK_LINKS=http://127.0.0.1:8099/links.json RP_UPDATE_WARN_MINUTES=0 redstonepanel check-updates" >/tmp/rp-bu.out 2>&1
RC=$?
[ $RC = 1 ] && ok "check-updates exits 1 when an update fails" || bad "exit code $RC"
wait_started 1.0.0.2 && ok "the server is back on 1.0.0.2" || { bad "rolled back"; dx "tail -40 /var/log/redstonepanel/panel.log"; }
check "the old program is back" dx "grep -qx 1.0.0.2 $D/release-notes.txt"
check "the world from before the update is back" dx "grep -qx world-v1 $D/worlds/Home/db/CURRENT"
check "the world the new version touched is kept as a Before restore backup" dx "ls /var/backups/redstonepanel/$SRV/ | grep -q 'pre-restore.tar.gz$'"
check "the recorded version is the old one" dx "grep -q '\"1.0.0.2\"' $D/.redstonepanel-bedrock.json"
U=$(api GET "/api/servers/$SRV/updates")
echo "$U" | json "not d['last']['ok'] and d['last']['rolled_back'] and d['last']['to'] == '1.0.0.3'" | grep -qx True \
    && ok "Maintenance shows the failed update, rolled back" || { bad "failed update shown"; echo "$U"; }
for _ in $(seq 1 15); do api GET /api/host | grep -q "Updating $SRV to Bedrock 1.0.0.3 failed" && break; sleep 2; done
api GET /api/host | grep -q "Updating $SRV to Bedrock 1.0.0.3 failed" && ok "Needs attention shows it" || bad "needs attention"

# --- the Update button: the same safe path, without the warning
mkrelease 1.0.0.4
[ "$(api POST "/api/servers/$SRV/update" | json "d.get('status')")" = success ] && ok "Check & Update installs 1.0.0.4" || bad "update button"
wait_started 1.0.0.4 && ok "the server runs 1.0.0.4" || bad "runs 1.0.0.4"
for _ in $(seq 1 15); do api GET /api/host | grep -q "Updating $SRV" || break; sleep 2; done
api GET /api/host | grep -q "Updating $SRV" && bad "the failure cleared after a good update" || ok "the failure cleared after a good update"
[ "$(dx "ls /var/backups/redstonepanel/$SRV/ | grep -c 'pre-update.tar.gz$'")" -le 3 ] && ok "at most 3 Before update backups are kept" || bad "pre-update pruning"

# --- downloads only from minecraft.net, and a broken download changes nothing
STARTS=$(dx "grep -c 'Version: ' $D/server.log")
dx "printf '{\"result\":{\"links\":[{\"downloadType\":\"serverBedrockLinux\",\"downloadUrl\":\"https://evil.example/bin-linux/bedrock-server-1.0.0.5.zip\"}]}}' > /tmp/mojang/links.json"
[ "$(dx "curl -s -o /dev/null -w '%{http_code}' -b /tmp/jar -X POST http://127.0.0.1:8080/api/servers/$SRV/update")" = 400 ] \
    && ok "a download from another host is refused" || bad "other host refused"
mkrelease 1.0.0.6 && dx "head -c 1000 /dev/urandom > /tmp/mojang/bin-linux/bedrock-server-1.0.0.6.zip"
[ "$(dx "curl -s -o /dev/null -w '%{http_code}' -b /tmp/jar -X POST http://127.0.0.1:8080/api/servers/$SRV/update")" = 400 ] \
    && ok "a damaged download is refused" || bad "damaged zip refused"
check "and the server kept running 1.0.0.4" started 1.0.0.4
check "without being stopped" dx "[ \$(grep -c 'Version: ' $D/server.log) = $STARTS ] && systemctl is-active --quiet $SRV"

# --- turning it off; removing the server forgets it
[ "$(api PUT "/api/servers/$SRV/updates" '{"enabled":false}' | json "d['auto']")" = False ] && ok "Update automatically turned off" || bad "turn off"
api DELETE "/api/servers/$SRV?keep_world=false" >/dev/null
check "a purged server is forgotten" dx "! grep -q '$SRV' /var/lib/redstonepanel/updates.json /var/lib/redstonepanel/auto-updates.json"
check "the timer stays for the machine" dx "systemctl is-enabled --quiet redstonepanel-updates.timer"

echo
[ $FAILS = 0 ] && echo "=== all Bedrock update checks passed" || echo "=== $FAILS check(s) FAILED"
exit $((FAILS > 0))
