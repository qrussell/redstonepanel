#!/bin/bash
# End-to-end test of the Port forwarding page (1.16.14): a master (pfm) and a node (pfn),
# throwaway systemd containers with ufw on one Docker network, both installing the .deb from
# dist/. Game servers are stand-in Bedrock servers (a bedrock_server script that sleeps), one on
# each machine and both on the same port, so the page must call out the clash. Nothing is
# downloaded. The containers' "public address" is the master's own address, so "Check from
# here" connects straight to it, the way a router with NAT reflection would loop it back.
# Run from the repository root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-port-forwarding.sh          (KEEP=1 leaves the containers running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
NET=rp-ports-test
IMG=rp-systemd:24.04
ALL="pfm pfn"
PASS_ADMIN='ports-test-admin-1'         # throwaway accounts inside the test containers only
PASS_USER='ports-test-user-1'
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$1" bash -c "$2"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
wait_https() { for _ in $(seq 1 60); do dx "$1" "curl -skf https://127.0.0.1:8443/api/health" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
api()  { dx pfm "curl -sk -b /tmp/$1 -X $2 'https://127.0.0.1:8443$3' ${4:+-H 'Content-Type: application/json' -d '$4'}"; }
code() { dx pfm "curl -sk -o /dev/null -w '%{http_code}' -b /tmp/$1 -X $2 'https://127.0.0.1:8443$3' ${4:+-H 'Content-Type: application/json' -d '$4'}"; }
login(){ dx pfm "curl -sk -c /tmp/$1 -H 'Content-Type: application/json' -d '{\"username\":\"$2\",\"password\":\"$3\"}' https://127.0.0.1:8443/api/auth/login" >/dev/null; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
json() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
# rows MEMBER-NAME SERVER KIND: "19140/tcp 19140/udp" sorted, from the master's page
rows() { api jar GET /api/port-forwarding | json "' '.join(sorted(r['port']+'/'+r['proto'] for m in d['members'] if m['name']=='$1' for s in m['servers'] if s['name']=='$2' for r in s['rows'] if r['kind']=='$3'))"; }
result() { echo "$CHECK" | json "[r['result'] for r in d['results'] if r['member']=='local' and r['proto']=='$1'][0]"; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Containers kept: $ALL (docker rm -f $ALL; docker network rm $NET)"; return; }
    docker rm -f $ALL >/dev/null 2>&1
    docker network rm "$NET" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== port forwarding test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f $ALL >/dev/null 2>&1
docker network rm "$NET" >/dev/null 2>&1
docker network create --subnet 172.30.82.0/24 "$NET" >/dev/null
for c in $ALL; do
    docker run -d --privileged --cgroupns=private --name "$c" --hostname "$c" --network "$NET" \
        -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
done
sleep 3
for c in $ALL; do
    ( dx "$c" "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") ufw zip python3 >/tmp/install.log 2>&1" \
        || { echo "install failed on $c"; dx "$c" "tail -20 /tmp/install.log"; } ) &
done
wait
for c in $ALL; do
    check "$c: panel installed and running" dx "$c" "systemctl is-active --quiet redstonepanel"
    dx "$c" "ufw allow 8443/tcp && ufw --force enable" >/dev/null 2>&1 || true      # the cluster talks on 8443
done
MIP=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" pfm)
NIP=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" pfn)

# --- the cluster: pfm is the master, pfn joins
dx pfm "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin && echo '$PASS_USER' | redstonepanel add-user alex --role user" >/dev/null
dx pfm "redstonepanel set-name Main && redstonepanel cluster create Home" >/dev/null
wait_https pfm || bad "pfm: master serves HTTPS"
join=$(dx pfm "redstonepanel cluster join-code" | grep '^rpjoin1:')
out=$(dx pfn "redstonepanel set-name Garage && redstonepanel join '$join'" 2>&1)
echo "$out" | grep -q "Joined cluster 'Home'" && ok "pfn joined as Garage" || { bad "pfn joined"; echo "$out"; }
wait_https pfn || bad "pfn: back up on HTTPS"
login jar admin "$PASS_ADMIN"
login ujar alex "$PASS_USER"
N1=$(api jar GET /api/cluster/overview | json "[n['id'] for n in d['nodes'] if n['name']=='Garage'][0]")

# --- a stand-in Bedrock server on each machine, both on 19140
mkzip() {
    dx pfm "rm -rf /tmp/srv && mkdir -p /tmp/srv/worlds/Home/db && cd /tmp/srv && printf '#!/bin/sh\nexec sleep infinity\n' > bedrock_server && chmod +x bedrock_server \
        && printf 'server-port=19140\nserver-portv6=19141\nlevel-name=Home\nserver-name=Island\n' > server.properties \
        && printf '[]' > permissions.json && rm -f /tmp/srv.zip && zip -qr /tmp/srv.zip ."
}
mkzip
SRV=$(dx pfm "curl -sk -b /tmp/jar -F file=@/tmp/srv.zip https://127.0.0.1:8443/api/servers/import" | json "d['server']")
[ -n "$SRV" ] && ok "stand-in server on the master: $SRV" || { bad "stand-in server on the master"; exit 1; }
NSRV=$(dx pfm "curl -sk -b /tmp/jar -F file=@/tmp/srv.zip https://127.0.0.1:8443/api/cluster/nodes/$N1/proxy/api/servers/import" | json "d['server']")
[ -n "$NSRV" ] && ok "stand-in server on Garage: $NSRV" || bad "stand-in server on Garage"
P=$(dx pfm "sed -n 's/^server-port=//p' /opt/$SRV/server.properties")
NP=$(dx pfn "sed -n 's/^server-port=//p' /opt/$NSRV/server.properties")

# --- who may see it
[ "$(code ujar GET /api/port-forwarding)" = 403 ] && ok "a panel user can't see port forwarding" || bad "user refused"
[ "$(code ujar PUT /api/port-forwarding/settings '{"public_address":"1.2.3.4"}')" = 403 ] && ok "or change it" || bad "user can't change"
[ "$(dx pfn "curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:8443/api/port-forwarding/local")" = 401 ] \
    && ok "a node's own list needs sign-in or the master's token" || bad "node list needs auth"

# --- what to forward, per machine, with each machine's LAN address
PF=$(api jar GET /api/port-forwarding)
[ "$(echo "$PF" | json "[m['lan_ip'] for m in d['members'] if m['name']=='Main'][0]")" = "$MIP" ] \
    && ok "the master's LAN address: $MIP" || bad "master lan_ip: $(echo "$PF" | json "[m.get('lan_ip') for m in d['members']]")"
[ "$(echo "$PF" | json "[m['forward_to'] for m in d['members'] if m['name']=='Garage'][0]")" = "$NIP" ] \
    && ok "the node's LAN address, as the node reports it: $NIP" || bad "node lan_ip: $(echo "$PF" | json "[(m['name'], m.get('lan_ip'), m.get('state'), m.get('error')) for m in d['members']]")"
[ "$(rows Main "$SRV" forward)" = "$P/tcp $P/udp" ] && ok "forward $P/tcp and $P/udp to the master" || bad "master rows: $(rows Main "$SRV" forward)"
[ "$(rows Main "$SRV" lan)" = "7551/udp" ] && ok "7551/udp is listed as LAN-only" || bad "lan rows: $(rows Main "$SRV" lan)"
[ "$(rows Garage "$NSRV" forward)" = "$NP/tcp $NP/udp" ] && ok "forward $NP/tcp and $NP/udp to Garage" || bad "node rows: $(rows Garage "$NSRV" forward)"
echo "$PF" | json "all(r['ufw'] for m in d['members'] if m['name']=='Main' for s in m['servers'] for r in s['rows'] if r['kind']=='forward')" | grep -qx True \
    && ok "ufw on the master allows every port it forwards" || skip_ufw=1
[ "${skip_ufw:-}" = 1 ] && echo "SKIP  ufw isn't active in this container"
if [ "$P" = "$NP" ]; then
    echo "$PF" | json "[(c['proto'], c['port']) for c in d['conflicts']]" | grep -q "('tcp', '$P')" \
        && ok "the same port on both machines is called out" || bad "conflict: $(echo "$PF" | json "d['conflicts']")"
fi
echo "$PF" | json "d['members'][0]['web']['port']" | grep -qx 8443 && ok "the panel's own port is listed (8443)" || bad "web port"

# --- a port changed in Configuration: the page follows
api jar POST "/api/servers/$SRV/properties" '{"properties":{"server-port":"19150","server-udp-ports":"19150"}}' >/dev/null
[ "$(rows Main "$SRV" forward)" = "19150/tcp 19150/udp" ] && ok "after a port change: 19150/tcp and 19150/udp" || bad "rows after change: $(rows Main "$SRV" forward)"
api jar GET /api/port-forwarding | json "len(d['conflicts'])" | grep -qx 0 && ok "and no clash any more" || bad "conflict after change"
P=19150

# --- the public address
[ "$(code jar PUT /api/port-forwarding/settings '{"public_address":"not an address"}')" = 400 ] && ok "a bad address is refused" || bad "bad address"
api jar PUT /api/port-forwarding/settings "{\"public_address\":\"$MIP\"}" >/dev/null
[ "$(api jar GET /api/port-forwarding | json "d['public_kind']")" = private ] && ok "a private public address is flagged (double NAT)" || bad "public_kind"
api jar PUT /api/port-forwarding/settings '{"public_address":"100.72.1.2"}' >/dev/null
[ "$(api jar GET /api/port-forwarding | json "d['public_kind']")" = cgnat ] && ok "a 100.64/10 address is flagged (carrier-grade NAT)" || bad "cgnat"
LOOK=$(code jar POST /api/port-forwarding/lookup)
[ "$LOOK" = 400 ] && ok "no NAT-PMP or UPnP on Docker's gateway: a clear message" || bad "lookup without a router: $LOOK"
dx pfm "grep -F '[PORTS]' /var/log/redstonepanel/panel.log" | grep -q "100.72.1.2" && ok "address changes are logged" || bad "log line"

# --- Check from here: nothing listens until something does
api jar PUT /api/port-forwarding/settings "{\"public_address\":\"$MIP\"}" >/dev/null
CHECK=$(api jar POST /api/port-forwarding/check)
[ "$(result tcp)" = no_answer ] && ok "nothing on TCP $P yet: No answer" || bad "check before: $CHECK"
[ "$(result udp)" = unchecked ] && ok "UDP gameplay can't be pinged: Not tried" || bad "udp check: $(result udp)"
dx pfm "nohup python3 -c 'import socket,time; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind((\"0.0.0.0\", $P)); s.listen(5); time.sleep(60)' >/dev/null 2>&1 &"
sleep 1
CHECK=$(api jar POST /api/port-forwarding/check)
[ "$(result tcp)" = reached ] && ok "a listener on TCP $P: Reached" || bad "check after: $CHECK"

echo
[ "$FAILS" = 0 ] && echo "=== all port forwarding checks passed" || echo "=== $FAILS check(s) failed"
exit $((FAILS > 0))
