#!/bin/bash
# End-to-end cluster test: three throwaway systemd containers (rpa, rpb, rpc) on a private Docker
# network install the .deb from dist/, then: rpa creates a cluster, rpb and rpc join, rpa controls
# a game server on rpc, node tokens are limited to their allowlist, rpb's certificate is renewed,
# rpc is promoted to master from rpa's backup (rpa is then locked out), and rpb leaves.
# Run from the repository root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-cluster.sh            (KEEP=1 leaves the containers running)
set -uo pipefail
FAILS=0
NET=rp-cluster-test
IMG=rp-systemd:24.04
PASS_ADMIN='cluster-test-pw-1'
PASSPHRASE='cluster test passphrase'
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$1" bash -c "$2"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
ip_of(){ docker inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" "$1"; }
wait_https() { for _ in $(seq 1 40); do dx "$1" "curl -skf https://127.0.0.1:8443/api/health" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
# API call inside a container, signed in as the test admin: api CONTAINER METHOD PATH [JSON]
api()  { dx "$1" "curl -sk -b /tmp/jar -X $2 https://127.0.0.1:8443$3 ${4:+-H 'Content-Type: application/json' -d '$4'}"; }
login(){ dx "$1" "curl -sk -c /tmp/jar -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PASS_ADMIN\"}' https://127.0.0.1:8443/api/auth/login" >/dev/null; }
# python3 on Linux/macOS; on Windows (Git Bash) "python3" may be the Microsoft Store stub
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
json() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Containers kept: rpa rpb rpc (docker rm -f rpa rpb rpc; docker network rm $NET)"; return; }
    docker rm -f rpa rpb rpc >/dev/null 2>&1
    docker network rm "$NET" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=$(ls dist/redstonepanel_*_all.deb 2>/dev/null | tail -1)
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== cluster test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f rpa rpb rpc >/dev/null 2>&1
docker network rm "$NET" >/dev/null 2>&1
docker network create --subnet 172.30.77.0/24 "$NET" >/dev/null
for c in rpa rpb rpc; do
    docker run -d --privileged --name "$c" --hostname "$c" --network "$NET" -v "$PWD/dist:/dist:ro" "$IMG" >/dev/null
done
sleep 3
for c in rpa rpb rpc; do
    ( dx "$c" "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") >/tmp/install.log 2>&1" \
        || { echo "install failed on $c"; dx "$c" "tail -20 /tmp/install.log"; } ) &
done
wait
for c in rpa rpb rpc; do check "$c: panel installed and running" dx "$c" "systemctl is-active --quiet redstonepanel"; done
A=$(ip_of rpa); B=$(ip_of rpb); C=$(ip_of rpc)
echo "  rpa=$A rpb=$B rpc=$C"

# --- a stand-in game server on rpc (the panel only needs a minecraft*.service unit)
dx rpc "mkdir -p /opt/minecraft-test && printf 'server-port=19132\n' > /opt/minecraft-test/server.properties
cat > /etc/systemd/system/minecraft-test.service <<'EOF'
[Unit]
Description=stand-in game server
[Service]
WorkingDirectory=/opt/minecraft-test
ExecStart=/bin/sleep infinity
EOF
systemctl daemon-reload"

# --- rpa creates the cluster
dx rpa "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
dx rpa "redstonepanel cluster create Home" >/dev/null
wait_https rpa && ok "rpa: master serves cluster HTTPS" || bad "rpa: master serves cluster HTTPS"
check "rpa: role is master" bash -c "docker exec rpa redstonepanel cluster status | grep -q 'Role:      master'"
dx rpa "curl -sk https://127.0.0.1:8443/api/cluster/ca.crt -o /tmp/ca.crt"

# --- rpb and rpc join with single-use codes
for c in rpb rpc; do
    code=$(dx rpa "redstonepanel cluster join-code" | grep '^rpjoin1:')
    out=$(dx "$c" "redstonepanel join '$code'" 2>&1)
    echo "$out" | grep -q "Joined cluster 'Home'" && ok "$c: joined" || { bad "$c: joined"; echo "$out"; }
    wait_https "$c" || bad "$c: back up on HTTPS"
done
check "rpb: role is node" bash -c "docker exec rpb redstonepanel cluster status | grep -q 'Role:      node'"
# rpc's code is spent: presenting it again is refused
secret=$(echo "$code" | $PY -c "import base64,json,sys; c=sys.stdin.read().strip()[8:]; print(json.loads(base64.urlsafe_b64decode(c + '=' * (-len(c) % 4)))['s'])")
out=$(dx rpb "curl -sk -H 'Content-Type: application/json' -d '{\"secret\":\"$secret\"}' https://$A:8443/api/cluster/join")
echo "$out" | grep -q "already used or expired" && ok "join codes work once" || { bad "join codes work once"; echo "$out"; }

# --- certificates: the master checks nodes against the cluster CA, with their host name/IP
check "rpb's certificate is valid for $B under the cluster CA" dx rpa "curl -sf --cacert /tmp/ca.crt https://$B:8443/api/health"
check "rpb's certificate is valid for rpb.local" dx rpa "curl -sf --cacert /tmp/ca.crt --resolve rpb.local:8443:$B https://rpb.local:8443/api/health"
check "rpb's certificate is not valid for another address" bash -c "! docker exec rpa curl -sf --cacert /tmp/ca.crt --resolve rpc.local:8443:$B https://rpc.local:8443/api/health"

# --- the cluster page
login rpa
ov=$(api rpa GET /api/cluster/overview)
states=$(echo "$ov" | json "' '.join(n['state'] for n in d['nodes'])")
[ "$states" = "ok ok ok" ] && ok "overview: all three panels ok" || { bad "overview: all three panels ok ($states)"; echo "$ov"; }
echo "$ov" | json "[s['name'] for n in d['nodes'] for s in n['servers']]" | grep -q minecraft-test \
    && ok "overview lists rpc's game server" || bad "overview lists rpc's game server"
rpc_id=$(echo "$ov" | json "[n['id'] for n in d['nodes'] if n.get('url','').startswith('https://$C')][0]")
rpb_id=$(echo "$ov" | json "[n['id'] for n in d['nodes'] if n.get('url','').startswith('https://$B')][0]")
api rpa POST "/api/cluster/nodes/$rpc_id/servers/minecraft-test/action" '{"action":"start"}' >/dev/null
sleep 1
check "start on rpc from the master" dx rpc "systemctl is-active --quiet minecraft-test"
check "rpc logged who did it" dx rpc "grep -q '\[CLUSTER\].* as admin: POST /api/servers/minecraft-test/action' /var/log/redstonepanel/panel.log"
api rpa POST "/api/cluster/nodes/$rpc_id/servers/minecraft-test/action" '{"action":"stop"}' >/dev/null
sleep 1
check "stop on rpc from the master" bash -c "! docker exec rpc systemctl is-active --quiet minecraft-test"
r=$(api rpa POST "/api/cluster/nodes/$rpc_id/servers/redstonepanel/action" '{"action":"stop"}')
echo "$r" | grep -q "No Minecraft server" && ok "can't stop non-game services" || { bad "can't stop non-game services"; echo "$r"; }
check "rpc's panel still running" dx rpc "systemctl is-active --quiet redstonepanel"

# --- what the node token can and can't do (as if the master were compromised)
tok=$(dx rpa "python3 -c \"import json;print([n['token'] for n in json.load(open('/var/lib/redstonepanel/cluster/nodes.json'))['nodes'] if n['url'].startswith('https://$B')][0])\"")
code_for() { dx rpa "curl -sk -o /dev/null -w '%{http_code}' -X $1 -H 'Authorization: Bearer $tok' -H 'X-RedstonePanel-Epoch: 1' https://$B:8443$2"; }
[ "$(code_for GET /api/servers)" = 200 ] && ok "token: list servers" || bad "token: list servers"
[ "$(code_for GET /api/users)" = 403 ] && ok "token: no users" || bad "token: no users"
[ "$(code_for GET /api/settings)" = 403 ] && ok "token: no settings" || bad "token: no settings"
[ "$(code_for DELETE /api/servers/minecraft-test)" = 403 ] && ok "token: no deleting servers" || bad "token: no deleting servers"
[ "$(dx rpa "curl -sk -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer $tok' https://$B:8443/api/servers")" = 401 ] \
    && ok "token: epoch header required" || bad "token: epoch header required"
[ "$(dx rpa "curl -sk -o /dev/null -w '%{http_code}' https://$B:8443/api/cluster/hello")" = 401 ] \
    && ok "hello needs the token" || bad "hello needs the token"

# --- renew rpb's certificate from the master
r=$(api rpa POST "/api/settings/cluster/nodes/$rpb_id/renew")
echo "$r" | grep -q '"cert_expires"' && ok "renewed rpb's certificate" || { bad "renewed rpb's certificate"; echo "$r"; }
sleep 3; wait_https rpb
check "rpb answers with the renewed certificate" dx rpa "curl -sf --cacert /tmp/ca.crt https://$B:8443/api/health"

# --- promote rpc to master from rpa's backup; rpa gets locked out
dx rpa "echo '$PASSPHRASE' | redstonepanel cluster backup /root/home.rpbackup" >/dev/null
# copied container to container through a pipe (no host paths, so it also runs from Git Bash)
docker exec rpa cat /root/home.rpbackup | docker exec -i rpc bash -c "cat > /root/home.rpbackup"
out=$(dx rpc "echo '$PASSPHRASE' | redstonepanel cluster promote /root/home.rpbackup" 2>&1)
echo "$out" | grep -q "now the master" && ok "rpc promoted to master" || { bad "rpc promoted to master"; echo "$out"; }
wait_https rpc
login rpc
ov=$(api rpc GET /api/cluster/overview)
echo "$ov" | json "' '.join(n['state'] for n in d['nodes'])" | grep -qx "ok ok" && ok "new master sees itself and rpb" \
    || { bad "new master sees itself and rpb"; echo "$ov"; }
sleep 11   # the old master's overview cache
ov=$(api rpa GET /api/cluster/overview)
echo "$ov" | json "[n['state'] for n in d['nodes'] if n.get('url','').startswith('https://$B')][0]" | grep -qx rejected \
    && ok "old master is locked out of rpb" || { bad "old master is locked out of rpb"; echo "$ov"; }

# --- the new master removes rpb, which goes back to standalone
rpb_id=$(echo "$(api rpc GET /api/cluster/overview)" | json "[n['id'] for n in d['nodes'] if n.get('url','').startswith('https://$B')][0]")
r=$(api rpc DELETE "/api/settings/cluster/nodes/$rpb_id")
echo "$r" | grep -q '"node_confirmed":true' && ok "rpb removed and told" || { bad "rpb removed and told"; echo "$r"; }
sleep 4
check "rpb is standalone again" bash -c "docker exec rpb redstonepanel cluster status | grep -q 'Role:      standalone'"
check "rpb serves self-signed HTTPS" bash -c "docker exec rpb grep -q '\"https_mode\": \"self_signed\"' /var/lib/redstonepanel/settings.json"

echo
[ "$FAILS" = 0 ] && echo "ALL PASSED" || echo "$FAILS FAILED"
exit $((FAILS > 0))
