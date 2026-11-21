#!/bin/bash
# End-to-end test of the ufw rules for Bedrock servers (1.16.11). Bedrock 1.26.50 and later use
# NetherNet when server.properties has no transport line: TCP server-port, UDP server-udp-ports
# and UDP 7551. Older panels opened only UDP server-port for such servers. Also checks that the
# panel follows server.properties edited by hand and units deleted by hand, and warns about
# game-port rules no server uses.
# One throwaway systemd container with ufw, installing the .deb from dist/. The imported server
# is a stand-in (a bedrock_server script that sleeps). With the internet, a real Bedrock server
# is deployed too and the ports it listens on are compared with the rules the panel opened.
# Run from the repository root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-bedrock-firewall.sh      (KEEP=1 leaves the container running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
C=rp-bfw
IMG=rp-systemd:24.04
PASS_ADMIN='bfw-test-admin-1'          # a throwaway account inside the test container only
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
skip() { echo "SKIP  $1"; }
dx()   { docker exec "$C" bash -c "$1"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
wait_http() { for _ in $(seq 1 60); do dx "curl -sf http://127.0.0.1:8080/api/health" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
api()  { dx "curl -s -b /tmp/jar -X $1 'http://127.0.0.1:8080$2' ${3:+-H 'Content-Type: application/json' -d '$3'}"; }
login(){ dx "curl -s -c /tmp/jar -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PASS_ADMIN\"}' http://127.0.0.1:8080/api/auth/login" >/dev/null; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
json() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
# The rules ufw holds (works whether or not ufw is active): "19132/tcp 7551/udp ..." sorted
rules() { dx "ufw show added" | sed -n 's/^ufw allow //p' | sort | tr '\n' ' ' | sed 's/ $//'; }
has()  { rules | tr ' ' '\n' | grep -qx "$1"; }
prop() { dx "sed -n 's/^$2=//p' /opt/$1/server.properties"; }
restart_panel() { dx "systemctl restart redstonepanel"; wait_http; login; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Container kept: $C (docker rm -f $C)"; return; }
    docker rm -f "$C" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== Bedrock firewall test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f "$C" >/dev/null 2>&1
docker run -d --privileged --cgroupns=private --name "$C" -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
sleep 3
dx "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") ufw zip screen curl wget jq unzip iproute2 >/tmp/install.log 2>&1" \
    || { echo "install failed"; dx "tail -20 /tmp/install.log"; exit 1; }
check "panel installed and running" wait_http
dx "ufw --force enable" >/dev/null 2>&1 && ok "ufw is on" || skip "ufw can't be turned on in this container; checking the rules it holds"
dx "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
login

# --- an older Bedrock backup: no transport line, no server-udp-ports
dx "rm -rf /tmp/srv && mkdir -p /tmp/srv/worlds/Home/db && cd /tmp/srv && printf '#!/bin/sh\nexec sleep infinity\n' > bedrock_server && chmod +x bedrock_server \
    && printf 'server-name=Island\nserver-port=19132\nserver-portv6=19133\nlevel-name=Home\nenable-lan-visibility=true\n' > server.properties \
    && printf '[]' > permissions.json && zip -qr /tmp/srv.zip ."
SRV=$(dx "curl -s -b /tmp/jar -F file=@/tmp/srv.zip http://127.0.0.1:8080/api/servers/import" | json "d['server']")
[ -n "$SRV" ] && ok "imported the stand-in server: $SRV" || { bad "import"; exit 1; }
P=$(prop "$SRV" server-port)
[ "$(prop "$SRV" server-udp-ports)" = "$P" ] && ok "it gets a fixed gameplay port: server-udp-ports=$P" || bad "server-udp-ports after import ($(prop "$SRV" server-udp-ports))"
[ "$(rules)" = "$P/tcp $P/udp 7551/udp" ] && ok "ufw allows $P/tcp, $P/udp and 7551/udp" || bad "rules after import: $(rules)"
ROW=$(api GET /api/servers)
echo "$ROW" | json "[s for s in d if s['name']=='$SRV'][0]['transport']" | grep -qx nethernet \
    && ok "the panel shows it as NetherNet" || bad "transport shown: $(echo "$ROW" | json "[s for s in d if s['name']=='$SRV'][0]['transport']")"

# --- switching to RakNet and back follows the transport
api POST "/api/servers/$SRV/properties" '{"properties":{"transport":"raknet"}}' >/dev/null
P6=$(prop "$SRV" server-portv6)
[ "$(rules)" = "$P/udp $P6/udp" ] && ok "transport=raknet: only $P/udp and $P6/udp" || bad "rules for raknet: $(rules)"
api POST "/api/servers/$SRV/properties" '{"properties":{"transport":"nethernet"}}' >/dev/null
[ "$(rules)" = "$P/tcp $P/udp 7551/udp" ] && ok "transport=nethernet: the NetherNet rules are back" || bad "rules for nethernet: $(rules)"

# --- an existing server from before 1.16.11: only UDP server-port open, no server-udp-ports
dx "systemctl stop redstonepanel; ufw delete allow $P/tcp; ufw delete allow 7551/udp; sed -i '/^server-udp-ports=/d; /^transport=/d' /opt/$SRV/server.properties; rm -f /var/lib/redstonepanel/bedrock-firewall-1.16.11" >/dev/null
[ "$(rules)" = "$P/udp" ] && ok "set up as before the upgrade: only $P/udp" || bad "setup: $(rules)"
dx "systemctl start redstonepanel"; wait_http; login
sleep 2
[ "$(rules)" = "$P/tcp $P/udp 7551/udp" ] && ok "after the upgrade the panel opens $P/tcp and 7551/udp" || bad "rules after upgrade: $(rules)"
[ "$(prop "$SRV" server-udp-ports)" = "$P" ] && ok "and gives it server-udp-ports=$P" || bad "server-udp-ports after upgrade"
check "it records that it did this" dx "[ -f /var/lib/redstonepanel/bedrock-firewall-1.16.11 ]"
dx "grep -F '[FIREWALL]' /var/log/redstonepanel/panel.log" | grep -q "$SRV" && ok "and logs it" || bad "log line"
dx "ufw delete allow 7551/udp" >/dev/null
restart_panel
has 7551/udp && bad "a rule the admin removed came back on the next start" || ok "it only does this once: a rule removed afterwards stays removed"
dx "ufw allow 7551/udp" >/dev/null

# --- server.properties edited by hand: opening the dashboard follows it
dx "sed -i 's/^server-port=.*/server-port=19150/; s/^server-portv6=.*/server-portv6=19151/; s/^server-udp-ports=.*/server-udp-ports=19150/' /opt/$SRV/server.properties"
api GET /api/host >/dev/null
[ "$(rules)" = "19150/tcp 19150/udp 7551/udp" ] && ok "a port changed by hand: the panel moves the rules to 19150" || bad "rules after a hand edit: $(rules)"
dx "grep -F '[FIREWALL]' /var/log/redstonepanel/panel.log" | grep -q "changed outside the panel" && ok "and logs it" || bad "hand edit log line"
P=19150

# --- a rule nothing uses gets a warning with the command to close it
dx "ufw allow 19170/udp" >/dev/null
api GET /api/host | json "[a['text'] for a in d['attention']]" | grep -q "sudo ufw delete allow 19170/udp" \
    && ok "a game-port rule no server uses gets a warning" || bad "leftover warning"
dx "ufw delete allow 19170/udp" >/dev/null

# --- a server whose unit was deleted by hand loses its rules; shared 7551 stays
SRV2=$(dx "curl -s -b /tmp/jar -F file=@/tmp/srv.zip http://127.0.0.1:8080/api/servers/import" | json "d['server']")
P2=$(prop "$SRV2" server-port)
[ "$(rules)" = "$(printf '%s\n' $P/tcp $P/udp $P2/tcp $P2/udp 7551/udp | sort | tr '\n' ' ' | sed 's/ $//')" ] \
    && ok "a second server: $P2/tcp and $P2/udp added" || bad "rules with two servers: $(rules)"
dx "systemctl disable --now $SRV2 >/dev/null 2>&1; rm -f /etc/systemd/system/$SRV2.service; systemctl daemon-reload"
api GET /api/host >/dev/null
[ "$(rules)" = "$P/tcp $P/udp 7551/udp" ] && ok "its unit deleted by hand: its rules go, 7551 stays for the other" || bad "rules after unit deleted: $(rules)"

# --- removing the server closes its rules
api DELETE "/api/servers/$SRV?keep_world=false" >/dev/null
[ -z "$(rules)" ] && ok "removing the server closes its rules" || bad "rules after removal: $(rules)"

# --- a real Bedrock server: the panel's rules match what it listens on
if dx "curl -s -m 10 -o /dev/null https://net-secondary.web.minecraft-services.net/api/v1.0/download/links"; then
    api POST /api/servers/deploy '{"edition":"bedrock"}' >/dev/null
    NEW=$(api GET /api/servers | json "[s['name'] for s in d if s['edition']=='Bedrock'][0]")
    [ -n "$NEW" ] && ok "deployed a real Bedrock server: $NEW" || bad "deploy"
    NP=$(prop "$NEW" server-port)
    for _ in $(seq 1 60); do dx "ss -lnt | grep -q ':$NP '" && break; sleep 1; done
    dx "grep -m1 -o 'Version:\? [0-9.]*' /opt/$NEW/server.log 2>/dev/null || journalctl -u $NEW --no-pager | grep -m1 -o 'Version:\? [0-9.]*'" | sed 's/^/      Bedrock /'
    check "it listens on TCP $NP" dx "ss -lnt | grep -q ':$NP '"
    check "and on UDP 7551" dx "ss -lnu | grep -q ':7551 '"
    [ "$(rules)" = "$NP/tcp $NP/udp 7551/udp" ] && ok "ufw allows exactly $NP/tcp, $NP/udp and 7551/udp" || bad "rules for the real server: $(rules)"
    dx "ss -lntu | grep bedrock || ss -lntup | grep -E ':($NP|7551) '" | sed 's/^/      /'
else
    skip "no internet in the container: the real Bedrock server can't be downloaded"
fi

echo
[ "$FAILS" = 0 ] && echo "=== all Bedrock firewall checks passed" || echo "=== $FAILS check(s) failed"
exit $((FAILS > 0))
