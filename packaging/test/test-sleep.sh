#!/bin/bash
# End-to-end test of sleep mode (1.26) and Start on the join page, in a throwaway systemd
# container that installs the .deb from dist/ and runs REAL game servers (vanilla Java 1.21.11
# and Bedrock; both are downloaded, so the container needs the internet):
#   1. Java: sleep mode on (1 minute) disables the unit; empty for a minute it stops and the panel
#      answers on 25565 ("Sleeping" in the server list); the Port check sees no conflict; a
#      player joining is told to join again and the server starts; it sleeps again when empty;
#      Stop while asleep means stopped; a panel restart keeps it asleep; a machine restart
#      (container restart) puts it to sleep, or leaves it stopped with Start at boot off.
#   2. Join page: Start is refused until the admin allows it, needs its header, then starts the
#      server; a second press within the cooldown doesn't start it again.
#   3. Bedrock: RakNet answers the ping as sleeping and the first connection request starts it;
#      NetherNet (no transport line) starts on the first TCP connection that sends something.
# With MF_DIR=<folder with node_modules/mineflayer> a real Minecraft client (mineflayer, offline
# mode) joins: kicked with "join again", then joins the started server. Without it a small
# Python client does the same handshake and login start.
# Run from the repository root after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-sleep.sh        (KEEP=1 keeps the container; RP_TEST_SETUP=script NAME
#                                             runs before installing, for proxy plumbing)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
C=rp-sleep
NET=rp-sleep-test
IMG=${IMG:-rp-systemd:24.04}
PW_ADMIN='sleep-test-admin-1'            # a throwaway account inside the test container only
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$C" bash -c "$1"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
is()   { local name=$1 want=$2 got=$3; [ "$got" = "$want" ] && ok "$name" || bad "$name: wanted $want, got $got"; }
has()  { local name=$1 text=$2 body=$3; case "$body" in *"$text"*) ok "$name";; *) bad "$name: '$text' not in: ${body:0:300}";; esac; }
URL=http://127.0.0.1:8080
api()  { dx "curl -s -m 60 -b /tmp/jar -X $1 '$URL$2' ${3:+-H 'Content-Type: application/json' -d '$3'}"; }
code() { dx "curl -s -m 60 -o /tmp/body -w '%{http_code}' -b /tmp/jar -X $1 '$URL$2' ${3:+-H 'Content-Type: application/json' -d '$3'}"; }
py()   { dx "cd /usr/share/redstonepanel && python3 -c \"$1\""; }
field(){ dx "curl -s -b /tmp/jar $URL/api/servers" | python3 -c "import json,sys; s=[x for x in json.load(sys.stdin) if x['name']=='$1']; print(s[0].get('$2') if s else 'missing')"; }
state(){ api GET "/api/servers/$1/sleep" | python3 -c "import json,sys; print(json.load(sys.stdin).get('state'))"; }
wait_for() {      # wait_for SECONDS DESCRIPTION COMMAND...
    local n=$1 what=$2; shift 2
    for _ in $(seq "$n"); do "$@" >/dev/null 2>&1 && { ok "$what"; return 0; }; sleep 1; done
    bad "$what (waited $n s)"; return 1
}
active()   { dx "systemctl is-active --quiet $1"; }
inactive() { ! dx "systemctl is-active --quiet $1"; }
asleep()   { [ "$(state "$1")" = asleep ]; }
listens()  { dx "ss -H -l${2:-t}np 'sport = :$1' | grep -q python3"; }       # the panel (python3) holds it
nolisten() { ! dx "ss -H -l${2:-t}np 'sport = :$1' | grep -q python3"; }
ping_java(){ py "from core import joinpage; print(joinpage.java_ping('127.0.0.1', $1)['motd'])"; }
online_java() { py "from core import joinpage; d=joinpage.java_ping('127.0.0.1', $1); assert 'Sleeping' not in d['motd']"; }
realpong() { nolisten "$1" u && raknet "$1"; }
raknet()   { py "from core import sleep; print(';'.join(sleep._bedrock_pong('127.0.0.1', $1)))"; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Container kept: $C (docker rm -f $C; docker network rm $NET)"; return; }
    docker rm -f $C >/dev/null 2>&1
    docker network rm "$NET" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== sleep mode test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f $C >/dev/null 2>&1
docker network rm "$NET" >/dev/null 2>&1
docker network create --subnet 172.30.88.0/24 "$NET" >/dev/null
docker run -d --privileged --cgroupns=private --name "$C" --hostname "$C" --network "$NET" --ip 172.30.88.2 \
    -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
IP=172.30.88.2
sleep 3
[ -n "${RP_TEST_SETUP:-}" ] && bash "$RP_TEST_SETUP" "$C"
dx "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") \
    python3 iproute2 curl openjdk-21-jre-headless >/tmp/install.log 2>&1" || { dx "tail -20 /tmp/install.log"; exit 1; }
check "panel installed and running" dx "systemctl is-active --quiet redstonepanel"
login() {         # signs in again; waits for the panel to answer after a restart
    for _ in $(seq 60); do
        dx "curl -s -c /tmp/jar -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PW_ADMIN\"}' $URL/api/auth/login" 2>/dev/null | grep -q 200 && return 0
        sleep 1
    done
    bad "signing in"
}
dx "echo '$PW_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
login

# A Minecraft client: mineflayer when MF_DIR has it, else a Python handshake + login start
join_java() {     # join_java -> prints "kicked: <reason>" or "spawned"
    if [ -n "${MF_DIR:-}" ] && [ -d "$MF_DIR/node_modules/mineflayer" ]; then
        NODE_PATH="$MF_DIR/node_modules" timeout 60 node -e "
            const b = require('mineflayer').createBot({ host: '$IP', port: 25565, username: 'Steve', auth: 'offline', version: '1.21.11' });
            b.once('spawn', () => { console.log('spawned'); setTimeout(() => { b.quit(); process.exit(0); }, 3000); });
            b.once('kicked', r => { console.log('kicked: ' + (typeof r === 'string' ? r : JSON.stringify(r))); process.exit(0); });
            b.once('error', e => { console.log('error: ' + e.message); process.exit(0); });" 2>&1 | tail -1
    else
        py "
import socket, struct
from core import sleep as Z
s = socket.create_connection(('127.0.0.1', 25565), timeout=10)
hs = b'\x00' + Z._varint(774) + Z._mc_string('localhost') + struct.pack('>H', 25565) + b'\x02'
s.sendall(Z._varint(len(hs)) + hs)
ls = b'\x00' + Z._mc_string('Steve') + bytes(16)
s.sendall(Z._varint(len(ls)) + ls)
pid, data, i = Z._packet(s)
print('kicked: ' + Z._string(data, i)[0] if pid == 0 else 'spawned')"
    fi
}

# ---------------------------------------------------------------------------
echo "--- Java"
R=$(code POST /api/servers/deploy '{"edition":"java","loader":"vanilla","version":"1.21.11"}')
is "vanilla 1.21.11 deployed" 200 "$R"
J=minecraft-java
wait_for 240 "  it runs and answers the server list" online_java 25565
dx "sed -i 's/^online-mode=.*/online-mode=false/' /opt/$J/server.properties"
api POST "/api/servers/$J/action" '{"action":"restart"}' >/dev/null
wait_for 180 "  restarted in offline mode (for the test client)" online_java 25565
is "sleep mode is off by default" "False" "$(api GET /api/servers/$J/sleep | python3 -c 'import json,sys; print(json.load(sys.stdin)["enabled"])')"
is "sleep mode on, 1 minute" 200 "$(code PUT /api/servers/$J/sleep '{"enabled":true,"minutes":1}')"
is "  state awake" awake "$(state $J)"
is "  Start at boot unit disabled" disabled "$(dx "systemctl is-enabled $J" 2>/dev/null)"
is "  the Start at boot switch still shows on" "True" "$(api GET /api/servers/$J/boot | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["enabled"] and d["sleep"])')"
wait_for 200 "empty for a minute: asleep" asleep $J
check "  the server is stopped" inactive $J
wait_for 10 "  the panel listens on TCP 25565" listens 25565
has "  the server list says Sleeping" "Sleeping" "$(ping_java 25565)"
is "  the server list reports it as sleeping" "True" "$(field $J sleeping)"
P=$(dx "redstonepanel ports" 2>&1)
case "$P" in *"25565"*"panel"*|*"held by"*25565*) bad "  Port check flags the panel's listener: $P";; *) ok "  Port check sees no conflict on 25565";; esac
has "  log says why" "Nobody has played on $J for 1 minute" "$(dx "grep SLEEP /var/log/redstonepanel/panel.log")"

K=$(join_java)
has "a player joining is told to join again" "Join again in about 30 seconds" "$K"
wait_for 10 "  the panel let go of 25565" nolisten 25565
wait_for 180 "  the server started and answers" online_java 25565
is "  state awake" awake "$(state $J)"
if [ -n "${MF_DIR:-}" ]; then
    is "  the player joins the started server" spawned "$(join_java)"
fi
wait_for 200 "empty again: back to sleep" asleep $J
wait_for 10 "  listening again" listens 25565

api POST "/api/servers/$J/action" '{"action":"stop"}' >/dev/null
is "Stop while asleep means stopped" off "$(state $J)"
check "  nothing listens on 25565" nolisten 25565
is "  joining doesn't start it" "" "$(dx "python3 -c \"import socket; socket.create_connection(('127.0.0.1',25565),timeout=2)\" 2>/dev/null && echo open")"
is "Sleep now from stopped: asleep" 200 "$(code POST /api/servers/$J/sleep/now)"
wait_for 20 "  listening again" listens 25565

dx "systemctl restart redstonepanel"
wait_for 60 "a panel restart keeps it asleep" listens 25565
is "  state asleep" asleep "$(state $J)"

echo "--- machine restart"
docker restart $C >/dev/null
wait_for 90 "  the panel is back" dx "systemctl is-active --quiet redstonepanel"
login
check "  the system didn't start it" inactive $J
wait_for 60 "  Start at boot on: asleep after the restart" listens 25565
is "  state asleep" asleep "$(state $J)"
api PUT "/api/servers/$J/boot" '{"enabled":false}' >/dev/null
docker restart $C >/dev/null
wait_for 90 "  the panel is back" dx "systemctl is-active --quiet redstonepanel"
login
sleep 15
check "  Start at boot off: stopped after the restart" inactive $J
check "  and not listening" nolisten 25565
is "  state off" off "$(state $J)"
api PUT "/api/servers/$J/boot" '{"enabled":true}' >/dev/null

echo "--- join page Start"
api PUT /api/settings/join '{"enabled":true,"port":8444}' >/dev/null
api PUT /api/join-pages/page '{"member":"local","server":"minecraft-java","enabled":true,"slug":"java"}' >/dev/null
PUB=http://127.0.0.1:8444
pcode(){ dx "curl -s -m 10 -o /tmp/pbody -w '%{http_code}' -X POST ${2:+-H 'X-Join-Start: 1'} '$PUB$1'"; }
is "Start refused until allowed (404)" 404 "$(pcode /join/java/start 1)"
api PUT /api/join-pages/page '{"member":"local","server":"minecraft-java","enabled":true,"slug":"java","allow_start":true}' >/dev/null
has "  the page shows Start the server" "Start the server" "$(dx "curl -s $PUB/join/java")"
is "  without its header: 400" 400 "$(pcode /join/java/start)"
is "  pressed: 202" 202 "$(pcode /join/java/start 1)"
has "  the page says Starting" '"starting"' "$(dx "curl -s $PUB/join/java/status.json")"
wait_for 180 "  the server started" online_java 25565
has "  log names the visitor" "started minecraft-java from its join page" "$(dx "grep JOIN /var/log/redstonepanel/panel.log")"
api POST "/api/servers/$J/action" '{"action":"stop"}' >/dev/null
wait_for 60 "  stopped" inactive $J
is "  a second press within 2 minutes: not repeated" 202 "$(pcode /join/java/start 1)"
sleep 5
check "  and the server stays stopped" inactive $J

echo "--- Bedrock"
R=$(code POST /api/servers/deploy '{"edition":"bedrock"}')
is "Bedrock deployed" 200 "$R"
B=minecraft-bedrock
BP=$(field $B port)
if dx "test -e /proc/net/if_inet6"; then          # Bedrock over RakNet won't start without IPv6
    # RakNet takes UDP server-port itself, so the NetherNet gameplay ports line has to go
    api POST "/api/servers/$B/action" '{"action":"stop"}' >/dev/null
    dx "sed -i '/^transport=/d; /^server-udp-ports=/d' /opt/$B/server.properties && echo transport=raknet >> /opt/$B/server.properties"
    api POST "/api/servers/$B/action" '{"action":"restart"}' >/dev/null
    wait_for 90 "  RakNet server answers" raknet "$BP"
    is "  sleep mode on" 200 "$(code PUT /api/servers/$B/sleep '{"enabled":true,"minutes":15}')"
    is "  Sleep now" 200 "$(code POST /api/servers/$B/sleep/now)"
    wait_for 120 "  asleep, the panel holds UDP $BP" listens "$BP" u
    has "  the RakNet pong says sleeping" "sleeping" "$(raknet "$BP")"
    py "import socket; from core import sleep as Z; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b'\x05' + Z.RAKNET_MAGIC + b'\x0b' + bytes(400), ('127.0.0.1', $BP))"
    wait_for 60 "  a connection request starts it" active $B
    wait_for 60 "  the real server answers the ping" realpong "$BP"
else
    echo "SKIP  Bedrock over RakNet: this machine has no IPv6, which Bedrock needs for RakNet"
fi

echo "--- Bedrock over NetherNet"
api POST "/api/servers/$B/action" '{"action":"stop"}' >/dev/null
wait_for 60 "  stopped" inactive $B
dx "sed -i '/^transport=/d; /^server-udp-ports=/d' /opt/$B/server.properties && echo server-udp-ports=$BP >> /opt/$B/server.properties"
is "  Sleep now" 200 "$(code POST /api/servers/$B/sleep/now)"
wait_for 30 "  asleep, the panel holds TCP $BP" listens "$BP" t
py "import socket; s=socket.create_connection(('127.0.0.1', $BP), timeout=5); s.sendall(b'hello')" 2>/dev/null
wait_for 60 "  the first connection that sends something starts it" active $B

echo
[ "$FAILS" = 0 ] && echo "ALL PASSED" || echo "$FAILS FAILED"
exit $FAILS
