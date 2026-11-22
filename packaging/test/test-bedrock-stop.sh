#!/bin/bash
# End-to-end test that Bedrock servers save before they stop (1.16.12): the game servers'
# drop-in runs `redstonepanel stop-unit` as ExecStop, which types Bedrock's own `stop` into its
# console and waits for it to quit, whatever stopped the unit. One throwaway systemd container
# installs the .deb from dist/. The Bedrock server is a stand-in that only writes its world
# when it gets `stop` (like BDS, it loses unsaved play when screen hangs up on it), so nothing is
# downloaded. A real Bedrock Dedicated Server is tested by hand (see the roadmap's As built notes).
# Run from the repository root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-bedrock-stop.sh          (KEEP=1 leaves the container running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
C=rp-bedrock-stop
IMG=rp-systemd:24.04
PASS_ADMIN='stop-test-admin-1'          # a throwaway account inside the test container only
SRV=minecraft-bedrock
D=/opt/$SRV
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$C" bash -c "$1"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
api()  { dx "curl -s -b /tmp/jar -X $1 'http://127.0.0.1:8080$2' ${3:+-H 'Content-Type: application/json' -d '$3'}"; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
json() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
saves()   { dx "cat $D/worlds/Home/db/SAVES 2>/dev/null || echo 0"; }
starts()  { dx "tr -d '\r' < $D/server.log 2>/dev/null | grep -c 'Server started' || true"; }
running() { [ "$(dx "systemctl is-active $SRV")" = active ] && [ "$(starts)" -ge "$1" ]; }
wait_running() { for _ in $(seq 1 40); do running "$1" && return 0; sleep 1; done; return 1; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Container kept: $C (docker rm -f $C)"; return; }
    docker rm -f "$C" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== Bedrock safe stop test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f "$C" >/dev/null 2>&1
docker run -d --privileged --cgroupns=private --name "$C" --hostname "$C" -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
sleep 3
dx "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") screen zip >/tmp/install.log 2>&1" \
    || { echo "install failed"; dx "tail -20 /tmp/install.log"; exit 1; }
check "panel installed and running" dx "systemctl is-active --quiet redstonepanel"
check "the game servers' drop-in has the stop hook" dx "systemctl cat minecraft-.service 2>/dev/null | grep -q 'ExecStop=-+/usr/sbin/redstonepanel stop-unit %N' || grep -q 'ExecStop=-+/usr/sbin/redstonepanel stop-unit %N' /usr/lib/systemd/system/minecraft-.service.d/redstonepanel.conf"
for _ in $(seq 1 30); do dx "curl -sf http://127.0.0.1:8080/api/health" >/dev/null 2>&1 && break; sleep 1; done
dx "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
dx "curl -s -c /tmp/jar -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PASS_ADMIN\"}' http://127.0.0.1:8080/api/auth/login" >/dev/null

# --- a stand-in Bedrock server: saves (SAVES + 1) and logs "Quit correctly" only on `stop`;
# with an ignore-stop file in its folder it ignores `stop`, like a hung server
dx "rm -rf /tmp/srv && mkdir -p /tmp/srv/worlds/Home/db"
docker exec -i "$C" bash -c 'cat > /tmp/srv/bedrock_server' <<'EOF'
#!/bin/bash
echo "[INFO] Version: 1.0.0.1"
echo "[INFO] Server started."
while read -r line; do
    echo "console: $line"
    if [ "$line" = stop ] && [ ! -e ignore-stop ]; then
        echo "[INFO] Server stop requested."
        sleep 2                                   # saving takes a moment
        n=$(cat worlds/Home/db/SAVES 2>/dev/null || echo 0)
        echo $((n + 1)) > worlds/Home/db/SAVES
        echo "Quit correctly"
        exit 0
    fi
done
sleep infinity
EOF
dx "cd /tmp/srv && chmod +x bedrock_server && printf 'server-port=19150\nserver-portv6=19151\nlevel-name=Home\nserver-name=Island\n' > server.properties \
    && printf '[]' > permissions.json && zip -qr /tmp/srv.zip ."
NAME=$(dx "curl -s -b /tmp/jar -F file=@/tmp/srv.zip http://127.0.0.1:8080/api/servers/import" | json "d['server']")
[ "$NAME" = "$SRV" ] && ok "stand-in Bedrock server imported" || bad "import ($NAME)"
api POST "/api/servers/$SRV/action" '{"action":"start"}' >/dev/null
wait_running 1 && ok "it runs" || { bad "started"; dx "cat $D/server.log"; }

# --- the Stop button
T0=$(date +%s)
api POST "/api/servers/$SRV/action" '{"action":"stop"}' >/dev/null
T=$(( $(date +%s) - T0 ))
[ "$(saves)" = 1 ] && ok "Stop: the world was saved (Bedrock got its stop command)" || { bad "Stop saved ($(saves))"; dx "tail -20 $D/server.log"; }
check "Stop: it logged Quit correctly" dx "grep -q 'Quit correctly' $D/server.log"
check "Stop: the unit is stopped" dx "! systemctl is-active --quiet $SRV"
[ $T -lt 20 ] && ok "Stop: it took ${T}s (no waiting for systemd's timeout)" || bad "Stop took ${T}s"
check "Stop: the panel log says it asked Bedrock to save" dx "grep -q 'Asked $SRV to save its world and stop' /var/log/redstonepanel/panel.log"

# --- the Restart button
api POST "/api/servers/$SRV/action" '{"action":"start"}' >/dev/null
wait_running 2 || bad "started again"
api POST "/api/servers/$SRV/action" '{"action":"restart"}' >/dev/null
[ "$(saves)" = 2 ] && ok "Restart: the world was saved first" || bad "Restart saved ($(saves))"
wait_running 3 && ok "Restart: it runs again" || bad "running after Restart"

# --- a scheduled restart (what the restart timer runs)
dx "redstonepanel restart-server $SRV" >/dev/null 2>&1
[ "$(saves)" = 3 ] && ok "scheduled restart: the world was saved first" || bad "scheduled restart saved ($(saves))"
wait_running 4 && ok "scheduled restart: it runs again" || bad "running after scheduled restart"

# --- plain systemctl stop (backups, restores, moves, uninstall and shutdown all use it)
dx "systemctl stop $SRV"
[ "$(saves)" = 4 ] && ok "systemctl stop: the world was saved" || bad "systemctl stop saved ($(saves))"

# --- none of these counted as a crash, and nothing started it again behind our back
check "no stop was reported as a crash" dx "! grep -q '\[CRASH\] $SRV' /var/log/redstonepanel/panel.log"
sleep 25
check "it stays stopped (no crash restart)" dx "! systemctl is-active --quiet $SRV"

# --- a server that ignores stop: systemd still stops it after the wait
dx "touch $D/ignore-stop && chown minecraft: $D/ignore-stop"
api POST "/api/servers/$SRV/action" '{"action":"start"}' >/dev/null
wait_running 5 || bad "started (ignoring stop)"
T0=$(date +%s)
dx "systemctl stop $SRV"
T=$(( $(date +%s) - T0 ))
check "a server that ignores stop is still stopped" dx "! systemctl is-active --quiet $SRV"
[ $T -ge 70 ] && [ $T -lt 95 ] && ok "after the 75 s wait (${T}s)" || bad "ignoring stop took ${T}s"
check "and the panel log says so" dx "grep -q \"$SRV didn't stop within 75 s\" /var/log/redstonepanel/panel.log"
[ "$(saves)" = 4 ] && ok "(it didn't save, as expected)" || bad "ignoring stop saved ($(saves))"

# --- a stopped server stops at once (no screen session)
T0=$(date +%s); dx "systemctl stop $SRV"; T=$(( $(date +%s) - T0 ))
[ $T -lt 5 ] && ok "stopping a stopped server is instant (${T}s)" || bad "stopped server took ${T}s"

echo
[ $FAILS = 0 ] && echo "=== all Bedrock safe stop checks passed" || echo "=== $FAILS check(s) FAILED"
exit $((FAILS > 0))
