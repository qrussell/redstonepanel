#!/bin/bash
# End-to-end test of the Port check (core/portaudit.py, 1.23.1), in a throwaway systemd container
# with ufw that installs the .deb from dist/. Stand-in Java servers open their game port, their
# RCON port when enable-rcon is on, and a "web map" port from config/webmap.yml (a mod the panel
# doesn't know). Another program, run as its own systemd unit, holds a third server's game port.
# Checks: the Port check names that program (name, process id, unit, command line); a start from
# the panel is refused with the reason instead of crash-looping; Move gives the server a free
# port and opens it in ufw; two servers with the same RCON port get the second one moved by
# itself; a port the unknown mod opened is learnt, and when another server needs it the one
# config line that sets it is changed; `redstonepanel ports` prints the same; Needs attention
# links to it. Nothing is downloaded. Run from the repository root on a machine with Docker,
# after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-port-check.sh          (KEEP=1 leaves the container running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
C=rp-portcheck-test
IMG=rp-systemd:24.04
PASS_ADMIN='portcheck-test-admin-1'      # a throwaway account inside the test container only
A=minecraft-pc-a                         # runs; RCON 25575; web map mod on TCP 8100
B=minecraft-pc-b                         # RCON 25575 too (set by hand)
O=minecraft-pc-c                         # its game port is held by another program
D=minecraft-pc-d                         # its game port is the one A's web map opened
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
skip() { echo "SKIP  $1"; }
dx()   { docker exec "$C" bash -c "$1"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
api()  { dx "curl -s -b /tmp/jar -X $1 'http://127.0.0.1:8080$2' ${3:+-H 'Content-Type: application/json' -d '$3'}"; }
apic() { dx "curl -s -o /dev/null -w '%{http_code}' -b /tmp/jar -X $1 'http://127.0.0.1:8080$2' ${3:+-H 'Content-Type: application/json' -d '$3'}"; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
json() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
prop() { dx "sed -n 's/^$2=//p' /opt/$1/server.properties"; }
rules(){ dx "ufw show added" | sed -n 's/^ufw allow //p' | sort | tr '\n' ' '; }
has()  { rules | tr ' ' '\n' | grep -qx "$1"; }
active(){ dx "systemctl is-active --quiet $1"; }
login(){ dx "curl -s -c /tmp/jar -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PASS_ADMIN\"}' http://127.0.0.1:8080/api/auth/login" >/dev/null; }
problems() { api GET /api/port-check | json "[p for p in d['problems'] if $1]"; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Container kept: $C (docker rm -f $C)"; return; }
    docker rm -f "$C" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== port check test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f "$C" >/dev/null 2>&1
docker run -d --privileged --cgroupns=private --name "$C" --hostname "$C" \
    -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
sleep 3
dx "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") ufw python3 iproute2 >/tmp/install.log 2>&1" \
    || { echo "install failed"; dx "tail -20 /tmp/install.log"; exit 1; }
check "panel installed and running" dx "systemctl is-active --quiet redstonepanel"
dx "ufw allow 8080/tcp >/dev/null; ufw --force enable" >/dev/null 2>&1 && ok "ufw is on" || skip "ufw can't be turned on in this container; checking the rules it holds"
dx "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null

# --- the stand-in server: game port, RCON when on, and the web map port from config/webmap.yml
dx "mkdir -p /usr/local/lib/rp-fakejava && cat > /usr/local/lib/rp-fakejava/server.py <<'PYEOF'
import os, re, socket, sys, time
props = dict(l.split('=', 1) for l in open('server.properties').read().splitlines() if '=' in l)
log = open('logs/latest.log', 'w', buffering=1)
def say(line):
    log.write(line + '\n'); print(line, flush=True)
def listen(port, what):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(('0.0.0.0', port))
    except OSError:
        say(f'[05:08:48] [Server thread/WARN]: **** FAILED TO BIND TO PORT! ({what} {port})')
        say('java.net.BindException: Address already in use')
        sys.exit(1)
    s.listen(); return s
keep = [listen(int(props['server-port']), 'game')]
say('[05:08:49] [Server thread/INFO]: Done (0.583s)! For help, type \"help\"')
if props.get('enable-rcon') == 'true':
    keep.append(listen(int(props.get('rcon.port', '25575')), 'rcon'))
if os.path.exists('config/webmap.yml'):
    port = int(re.search(r'^\s*port:\s*(\d+)', open('config/webmap.yml').read(), re.M).group(1))
    keep.append(listen(port, 'web map'))
while True:
    time.sleep(3600)
PYEOF
printf '#!/bin/bash\nexec python3 /usr/local/lib/rp-fakejava/server.py\n' > /usr/local/lib/rp-fakejava/java
chmod +x /usr/local/lib/rp-fakejava/java"
make_server() {  # name, game port, extra server.properties lines, web map port (optional)
    local map=${4:-}
    dx "mkdir -p /opt/$1/logs /opt/$1/config && cd /opt/$1 && printf 'server-port=$2\n$3' > server.properties \
        && { [ -z '$map' ] || printf '# web map\nwebserver:\n  bind: 0.0.0.0\n  port: $map\n' > config/webmap.yml; } \
        && chown -R minecraft:minecraft /opt/$1 && cat > /etc/systemd/system/$1.service <<UNIT
[Unit]
Description=Minecraft Server ($1)
[Service]
User=minecraft
Group=minecraft
WorkingDirectory=/opt/$1
ExecStart=/usr/local/lib/rp-fakejava/java -Xms256M -Xmx512M -jar server.jar nogui
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload"
}
dx "systemctl stop redstonepanel"
make_server $A 25601 'enable-rcon=true\nrcon.port=25575\nrcon.password=x\n' 8100
make_server $B 25602 'enable-rcon=true\nrcon.port=25575\nrcon.password=y\n'
make_server $O 25603 'enable-rcon=false\n'
make_server $D 8100 'enable-rcon=false\n'
dx "systemctl start redstonepanel"
for _ in $(seq 1 30); do dx "curl -sf http://127.0.0.1:8080/api/health" >/dev/null 2>&1 && break; sleep 1; done
login
# another program on C's game port, as its own systemd unit (like an old server or a container)
dx "systemd-run --unit=outside-mc --property=User=nobody python3 -c \"import socket,time; s=socket.socket(); s.bind(('0.0.0.0',25603)); s.listen(); time.sleep(3600)\"" >/dev/null 2>&1
dx "systemctl start $A"; sleep 2
check "server A runs (game 25601, RCON 25575, web map 8100)" active $A

# 1. Another program on a server's game port: named, start refused, Move
P=$(problems "p['server']=='$O'")
echo "$P" | grep -q "25603" && ok "the Port check finds C's game port taken" || bad "problems: $(api GET /api/port-check)"
echo "$P" | grep -q "systemd unit outside-mc.service" && ok "  names the unit" || bad "  unit: $P"
echo "$P" | grep -q "python3 (process [0-9]*" && ok "  names the program and process id" || bad "  program: $P"
echo "$P" | grep -q "socket.socket()" && ok "  gives its command line" || bad "  command line: $P"
echo "$P" | grep -q "'fix': 'move'" && ok "  offers Move" || bad "  fix: $P"
CODE=$(apic POST /api/servers/$O/action '{"action":"start"}')
R=$(api POST /api/servers/$O/action '{"action":"start"}')
[ "$CODE" = 400 ] && echo "$R" | grep -q "can't start: its game port TCP 25603 is used by python3" && ok "start from the panel is refused with the reason" || bad "start: $CODE $R"
sleep 1
active $O && bad "  C was started anyway" || ok "  C wasn't started (no crash loop)"
dx "redstonepanel ports" > /tmp/rp-ports.txt 2>&1
grep -q "outside-mc.service" /tmp/rp-ports.txt && ok "redstonepanel ports names the program" || bad "CLI: $(cat /tmp/rp-ports.txt)"
grep -q "^25603/tcp" /tmp/rp-ports.txt && ok "  and lists the port table" || bad "  CLI table: $(cat /tmp/rp-ports.txt)"
H=$(api GET /api/host)
echo "$H" | json "[a for a in d['attention'] if a.get('where')=='ports' and a.get('server')=='$O']" | grep -q "outside-mc" \
    && ok "Needs attention shows it" || bad "attention: $(echo "$H" | json "d['attention']")"
M=$(api POST /api/servers/$O/ports/move '{}')
[ "$(echo "$M" | json "d['new']")" = 25604 ] && ok "Move: C goes to 25604" || bad "move: $M"
[ "$(prop $O server-port)" = 25604 ] && ok "  server.properties says 25604" || bad "  server-port: $(prop $O server-port)"
echo "$M" | grep -q "change it to 25604" && ok "  says what to change on the router" || bad "  note: $M"
has 25604/tcp && ok "  ufw allows 25604" || bad "  ufw: $(rules)"
[ "$(apic POST /api/servers/$O/action '{"action":"start"}')" = 200 ] && sleep 2 && active $O && ok "  C starts on the new port" || bad "  C didn't start"

# 2. Two servers with the same RCON port: the second one's moves by itself
CODE=$(apic POST /api/servers/$B/action '{"action":"start"}')
sleep 2
[ "$(prop $B rcon.port)" = 25576 ] && ok "start B: its RCON port moved off A's 25575 to 25576" || bad "B rcon.port: $(prop $B rcon.port) (start $CODE)"
[ "$(prop $A rcon.port)" = 25575 ] && ok "  A keeps 25575" || bad "  A rcon.port: $(prop $A rcon.port)"
check "  B runs" active $B
dx "grep -q '\[PORTS\] $B: its RCON port TCP 25575 was in use' /var/log/redstonepanel/panel.log" && ok "  the panel log says so" || bad "  log: $(dx "grep PORTS /var/log/redstonepanel/panel.log | tail -3")"

# 3. A mod the panel doesn't know: its port is learnt, and moved when another server needs it
U=$(api GET /api/port-check | json "[u['port'] for u in d['unknown'] if u['server']=='$A']")
[ "$U" = "[8100]" ] && ok "A's web map port 8100 is listed as opened by a mod" || bad "unknown: $U"
dx "grep -q 8100 /var/lib/redstonepanel/port-check.json" && ok "  and learnt" || bad "  state: $(dx "cat /var/lib/redstonepanel/port-check.json")"
R=$(api POST /api/servers/$D/action '{"action":"start"}')
echo "$R" | grep -q "can't start: its game port TCP 8100 is used by" && ok "start D (game port 8100): refused while A holds it" || bad "start D: $R"
dx "grep -q '^  port: 8101$' /opt/$A/config/webmap.yml" && ok "  A's config/webmap.yml line set to 8101" || bad "  webmap: $(dx "cat /opt/$A/config/webmap.yml")"
check "  the rest of the file is kept" dx "grep -qx '  bind: 0.0.0.0' /opt/$A/config/webmap.yml"
check "  it still belongs to the game account" dx "test \$(stat -c %U /opt/$A/config/webmap.yml) = minecraft"
api GET /api/port-check | json "d['moved']" | grep -q "8101" && ok "  the Port check says A must restart" || bad "  moved: $(api GET /api/port-check)"
dx "systemctl restart $A"; sleep 2
dx "ss -ltn | grep -q ':8101 '" && ok "  after A restarts its map is on 8101" || bad "  A listens: $(dx 'ss -ltn')"
[ "$(apic POST /api/servers/$D/action '{"action":"start"}')" = 200 ] && sleep 2 && active $D && ok "  D starts on 8100" || bad "  D didn't start"
P=$(api GET /api/port-check | json "[p['text'] for p in d['problems']]")
[ "$P" = "[]" ] && ok "no problems left" || bad "left: $P"

echo
[ "$FAILS" = 0 ] && echo "=== all port check checks passed" || echo "=== $FAILS check(s) failed"
exit $((FAILS > 0))
