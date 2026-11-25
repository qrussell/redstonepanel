#!/bin/bash
# End-to-end test of removing the panel safely (core/uninstall.py, 1.23.2), in a throwaway systemd
# container with ufw that installs the .deb from dist/. Three stand-in Java servers (a Python
# listener on the game port) run and start at boot. Checks: the Remove RedstonePanel API lists
# them; Delete is refused without a backup and without DELETE; the choices are carried out (stop =
# service gone, folder kept; delete = folder gone, backups kept; keep = still running); a plain
# `apt remove` never stops the kept server but turns off the panel's own timers and warns about it;
# installing again turns its backup schedule back on and the stopped one can be restored;
# `redstonepanel uninstall --servers stop --yes` stops everything and removes the package; the
# install message says how to remove the panel. Nothing is downloaded. Run from the repository
# root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-uninstall.sh          (KEEP=1 leaves the container running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
C=rp-uninstall-test
IMG=rp-systemd:24.04
PASS_ADMIN='uninstall-test-admin-1'      # a throwaway account inside the test container only
K=minecraft-keep; S=minecraft-stop; D=minecraft-del
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$C" bash -c "$1"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
api()  { dx "curl -s -b /tmp/jar -X $1 'http://127.0.0.1:8080$2' ${3:+-H 'Content-Type: application/json' -d '$3'}"; }
code() { dx "curl -s -o /dev/null -w '%{http_code}' -b /tmp/jar -X $1 'http://127.0.0.1:8080$2' ${3:+-H 'Content-Type: application/json' -d '$3'}"; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
json() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
active(){ dx "systemctl is-active --quiet $1"; }
enabled(){ dx "systemctl is-enabled --quiet $1"; }
login() {
    for _ in $(seq 1 30); do dx "curl -sf http://127.0.0.1:8080/api/health" >/dev/null 2>&1 && break; sleep 1; done
    dx "curl -s -c /tmp/jar -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PASS_ADMIN\"}' http://127.0.0.1:8080/api/auth/login" >/dev/null
}

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Container kept: $C (docker rm -f $C)"; return; }
    docker rm -f "$C" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== uninstall test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f "$C" >/dev/null 2>&1
docker run -d --privileged --cgroupns=private --name "$C" --hostname "$C" \
    -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
sleep 3
dx "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") ufw python3 >/tmp/install.log 2>&1" \
    || { echo "install failed"; dx "tail -20 /tmp/install.log"; exit 1; }
check "panel installed and running" active redstonepanel
dx "grep -q 'sudo redstonepanel uninstall' /tmp/install.log" && ok "the install message says how to remove the panel safely" \
    || bad "install message: $(dx "tail -15 /tmp/install.log")"
dx "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null

# --- three stand-in Java servers: a listener on the game port, started at boot
dx "mkdir -p /usr/local/lib/rp-fakejava && cat > /usr/local/lib/rp-fakejava/listen.py <<'PYEOF'
import re, socket, time
game = int(re.search(r'^server-port=(\d+)', open('server.properties').read(), re.M).group(1))
t = socket.socket(); t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); t.bind(('0.0.0.0', game)); t.listen()
print('[Server thread/INFO]: Done (0.5s)! For help, type \"help\"', flush=True)
while True:
    time.sleep(3600)
PYEOF
printf '#!/bin/bash\nexec python3 /usr/local/lib/rp-fakejava/listen.py\n' > /usr/local/lib/rp-fakejava/java
chmod +x /usr/local/lib/rp-fakejava/java"
make_server() {  # name, game port
    dx "mkdir -p /opt/$1/world && cd /opt/$1 && printf 'server-port=$2\nenable-rcon=false\nlevel-name=world\n' > server.properties \
        && echo 'a world' > world/level.dat && touch server.jar && chown -R minecraft:minecraft /opt/$1 && cat > /etc/systemd/system/$1.service <<UNIT
[Unit]
Description=Minecraft Server ($1)
[Service]
User=minecraft
Group=minecraft
WorkingDirectory=/opt/$1
ExecStart=/usr/local/lib/rp-fakejava/java -Xms256M -Xmx512M -jar server.jar nogui
Restart=on-failure
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && systemctl enable --now $1"
}
make_server $K 25611; make_server $S 25612; make_server $D 25613
login
api PUT /api/servers/$K/backups/schedule '{"enabled":true,"frequency":"daily","time":"04:00","keep":3}' >/dev/null
check "a backup schedule for the server that stays" enabled "redstonepanel-backup@$K.timer"

# 1. What the page shows
O=$(api GET /api/uninstall)
[ "$(echo "$O" | json "sorted(s['name'] for s in d['servers'])")" = "['$D', '$K', '$S']" ] && ok "Remove RedstonePanel lists the three servers" || bad "overview: $O"
[ "$(echo "$O" | json "sorted(d['left_behind'])")" = "['$D', '$K', '$S']" ] && ok "  all three would be left behind" || bad "  left_behind: $O"
[ "$(echo "$O" | json "d['keep_possible'] and d['can_run'] and d['command']")" = "sudo apt remove redstonepanel" ] && ok "  keep running is offered, Uninstall now can run" || bad "  $O"

# 2. Refusals change nothing
CH="{\"choices\":{\"$K\":\"keep\",\"$S\":\"stop\",\"$D\":\"delete\"},\"confirm\":\"DELETE\"}"
[ "$(code POST /api/uninstall/apply "$CH")" = 400 ] && ok "delete refused without a recent backup" || bad "delete without a backup wasn't refused"
api POST /api/servers/$D/backups >/dev/null
[ "$(code POST /api/uninstall/apply "{\"choices\":{\"$K\":\"keep\",\"$S\":\"stop\",\"$D\":\"delete\"},\"confirm\":\"yes\"}")" = 400 ] \
    && ok "delete refused without typing DELETE" || bad "wrong confirm word wasn't refused"
[ "$(code POST /api/uninstall/apply "{\"choices\":{\"$K\":\"keep\"}}")" = 400 ] && ok "every server needs a choice" || bad "missing choices weren't refused"
active $S && active $D && dx "test -d /opt/$D" && ok "  nothing changed by the refusals" || bad "  a refusal changed something"

# 3. Carry out the choices
R=$(api POST /api/uninstall/apply "$CH")
[ "$(echo "$R" | json "(d['stopped'], d['kept'], d['deleted'])")" = "(['$S'], ['$K'], ['$D'])" ] && ok "choices carried out" || bad "apply: $R"
! active $S && dx "test ! -e /etc/systemd/system/$S.service && test -f /opt/$S/world/level.dat" && ok "  stop: service gone, world kept" || bad "  stop: $(dx "ls /etc/systemd/system/$S.service /opt/$S 2>&1")"
dx "test ! -e /opt/$D && ls /var/backups/redstonepanel/$D/ | grep -q tar" && ok "  delete: folder gone, backup kept" || bad "  delete: $(dx "ls /opt /var/backups/redstonepanel/$D 2>&1")"
active $K && ok "  keep: still running" || bad "  keep isn't running"
dx "grep -q '\[UNINSTALL\] admin got this machine ready' /var/log/redstonepanel/panel.log" && ok "  the panel log says who did it" || bad "  no log line"

# 4. A plain apt remove: never stops the kept server, turns off the panel's own timers, warns
dx "DEBIAN_FRONTEND=noninteractive apt-get remove -y redstonepanel >/tmp/remove.log 2>&1"
check "apt remove worked" dx "! dpkg -s redstonepanel 2>/dev/null | grep -q 'Status: install ok installed'"
active $K && enabled $K && ok "  the kept server still runs and starts at boot" || bad "  kept server stopped"
dx "grep -q 'nothing to' /tmp/remove.log && grep -q '$K' /tmp/remove.log && grep -q 'systemctl disable --now minecraft-NAME' /tmp/remove.log" \
    && ok "  apt's output warns about it, with the command to stop it" || bad "  remove output: $(dx "cat /tmp/remove.log")"
! enabled "redstonepanel-backup@$K.timer" && ok "  its backup timer is off" || bad "  backup timer still on"
! enabled redstonepanel-updates.timer && ! active redstonepanel-updates.timer && ok "  the update check timer is off" || bad "  update timer still on"
dx "grep -q '$S' /tmp/remove.log" && ok "  it mentions the stopped server to restore later" || bad "  no mention of $S"

# 5. Installing again: the schedule comes back, the stopped server can be restored
dx "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq /dist/$(basename "$DEB") >/tmp/reinstall.log 2>&1"
check "installed again" active redstonepanel
for _ in $(seq 1 20); do enabled "redstonepanel-backup@$K.timer" && break; sleep 1; done
enabled "redstonepanel-backup@$K.timer" && ok "  the kept server's backup schedule is on again" || bad "  backup timer still off"
login
api GET /api/servers/removed | grep -q "$S" && ok "  the stopped server is under Removed servers" || bad "  removed: $(api GET /api/servers/removed)"
R=$(dx "redstonepanel restore $S 2>&1"); sleep 2
# (the panel writes its own unit with the real Java, which this container doesn't have, so it can't start)
echo "$R" | grep -q "Restored $S" && dx "test -f /etc/systemd/system/$S.service" && ok "  and restores" || bad "  restore: $R"

# 6. The command line, without questions: stop everything, remove the package
dx "redstonepanel uninstall --servers stop --yes >/tmp/cli.log 2>&1"
check "redstonepanel uninstall --servers stop --yes removed the package" dx "! dpkg -s redstonepanel 2>/dev/null | grep -q 'Status: install ok installed'"
! active $K && ! active $S && ok "  every server stopped" || bad "  still running: $(dx "systemctl list-units 'minecraft-*' --no-legend")"
dx "test -f /opt/$K/world/level.dat && test -f /opt/$S/world/level.dat" && ok "  worlds kept" || bad "  a world is missing"
dx "! grep -q 'These game servers stay' /tmp/cli.log" && ok "  nothing left behind to warn about" || bad "  cli: $(dx "cat /tmp/cli.log")"
dx "systemctl list-units --state=failed --no-legend | grep -q redstonepanel" && bad "  a panel unit failed" || ok "  no failed panel units"

echo
[ "$FAILS" = 0 ] && echo "ALL PASSED" || echo "$FAILS FAILED"
exit $((FAILS > 0))
