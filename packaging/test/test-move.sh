#!/bin/bash
# End-to-end test of moving and copying servers between cluster members (1.16): a master (mvm)
# and a node (mvn), two throwaway systemd containers on one Docker network, both installing the
# .deb from dist/. The game server is a stand-in Bedrock server (a bedrock_server script that
# sleeps, run under screen like the real one), so nothing is downloaded.
# Run from the repository root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-move.sh          (KEEP=1 leaves the containers running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
NET=rp-move-test
IMG=rp-systemd:24.04
PASS_ADMIN='move-test-admin-1'          # throwaway accounts inside the test containers only
PASS_USER='move-test-user-1'
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$1" bash -c "$2"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
wait_https() { for _ in $(seq 1 60); do dx "$1" "curl -skf https://127.0.0.1:8443/api/health" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
api()  { dx mvm "curl -sk -b /tmp/$1 -X $2 'https://127.0.0.1:8443$3' ${4:+-H 'Content-Type: application/json' -d '$4'}"; }
code() { dx mvm "curl -sk -o /dev/null -w '%{http_code}' -b /tmp/$1 -X $2 'https://127.0.0.1:8443$3' ${4:+-H 'Content-Type: application/json' -d '$4'}"; }
login(){ dx mvm "curl -sk -c /tmp/$1 -H 'Content-Type: application/json' -d '{\"username\":\"$2\",\"password\":\"$3\"}' https://127.0.0.1:8443/api/auth/login" >/dev/null; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
json() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

# move KIND SERVER FROM TO [START]: starts a move or copy and waits for it; prints the finished job
move() {
    local job id state
    job=$(api jar POST /api/cluster/moves "{\"kind\":\"$1\",\"server\":\"$2\",\"from_node\":\"$3\",\"to_node\":\"$4\",\"start\":${5:-false}}")
    id=$(echo "$job" | json "d['id']" 2>/dev/null) || { echo "$job"; return 1; }
    for _ in $(seq 1 120); do
        job=$(api jar GET "/api/cluster/moves/$id")
        state=$(echo "$job" | json "d['state']")
        [ "$state" != running ] && break
        sleep 1
    done
    echo "$job"
}

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Containers kept: mvm mvn (docker rm -f mvm mvn; docker network rm $NET)"; return; }
    docker rm -f mvm mvn >/dev/null 2>&1
    docker network rm "$NET" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== move test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f mvm mvn >/dev/null 2>&1
docker network rm "$NET" >/dev/null 2>&1
docker network create --subnet 172.30.80.0/24 "$NET" >/dev/null
for c in mvm mvn; do
    docker run -d --privileged --cgroupns=private --name "$c" --hostname "$c" --network "$NET" \
        -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
done
sleep 3
for c in mvm mvn; do
    ( dx "$c" "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") zip >/tmp/install.log 2>&1" \
        || { echo "install failed on $c"; dx "$c" "tail -20 /tmp/install.log"; } ) &
done
wait
for c in mvm mvn; do check "$c: panel installed and running" dx "$c" "systemctl is-active --quiet redstonepanel"; done

# --- the cluster: mvm is the master, mvn joins
dx mvm "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin && echo '$PASS_USER' | redstonepanel add-user alex --role user" >/dev/null
dx mvm "redstonepanel set-name Main && redstonepanel cluster create Home" >/dev/null
wait_https mvm || bad "mvm: master serves HTTPS"
join=$(dx mvm "redstonepanel cluster join-code" | grep '^rpjoin1:')
out=$(dx mvn "redstonepanel set-name Basement && redstonepanel join '$join'" 2>&1)
echo "$out" | grep -q "Joined cluster 'Home'" && ok "mvn joined" || { bad "mvn joined"; echo "$out"; }
wait_https mvn || bad "mvn: back up on HTTPS"
login jar admin "$PASS_ADMIN"
login ujar alex "$PASS_USER"
NID=$(api jar GET /api/cluster/overview | json "[n['id'] for n in d['nodes'] if not n.get('local')][0]")
api jar GET /api/cluster/overview | json "[n.get('cluster_api') for n in d['nodes'] if not n.get('local')][0]" | grep -qxE "[3-9]" \
    && ok "the node speaks cluster contract 3 or later" || bad "the node speaks cluster contract 3 or later"

# --- a stand-in Bedrock server on the master, imported through the panel so it makes the unit
dx mvm "mkdir -p /tmp/srv/worlds/Home/db && cd /tmp/srv && printf '#!/bin/sh\n[ -f /tmp/fail-here ] && exit 1\nexec sleep infinity\n' > bedrock_server && chmod +x bedrock_server \
    && printf 'server-port=19140\nserver-portv6=19141\nlevel-name=Home\nserver-name=Island\n' > server.properties \
    && printf '[]' > permissions.json && head -c 3000000 /dev/urandom > worlds/Home/db/000005.ldb && zip -qr /tmp/srv.zip ."
r=$(dx mvm "curl -sk -b /tmp/jar -F file=@/tmp/srv.zip https://127.0.0.1:8443/api/servers/import")
SRV=$(echo "$r" | json "d['server']" 2>/dev/null)
[ "$SRV" = minecraft-bedrock ] && ok "stand-in server imported as $SRV" || { bad "stand-in server imported"; echo "$r"; }
dx mvm "chmod +x /opt/$SRV/bedrock_server"
api jar POST "/api/servers/$SRV/properties/raw" '{"content":"server-port=19140\nserver-portv6=19141\nlevel-name=Home\nserver-name=Island\n"}' >/dev/null
api jar PUT "/api/servers/$SRV/backups/schedule" '{"enabled":true,"frequency":"daily","time":"03:30","keep":4}' >/dev/null
api jar PUT "/api/servers/$SRV/restarts" '{"enabled":true,"frequency":"daily","time":"05:15","warn":0}' >/dev/null
dx mvm "systemctl restart $SRV"; sleep 2
check "the server runs on the master" dx mvm "systemctl is-active --quiet $SRV"
WORLD_SUM=$(dx mvm "sha256sum < /opt/$SRV/worlds/Home/db/000005.ldb")

# --- who may move
[ "$(code ujar POST /api/cluster/moves "{\"kind\":\"move\",\"server\":\"$SRV\",\"from_node\":\"local\",\"to_node\":\"$NID\"}")" = 403 ] \
    && ok "a master user can't move servers" || bad "a master user can't move servers"
[ "$(code jar GET "/api/cluster/nodes/$NID/proxy/api/cluster/transfers/$(printf 'a%.0s' $(seq 32))/archive")" = 403 ] \
    && ok "the proxy never passes on transfer routes" || bad "the proxy never passes on transfer routes"
[ "$(dx mvn "curl -sk -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{\"server\":\"x\",\"stop\":true}' https://127.0.0.1:8443/api/cluster/transfers/export")" = 401 ] \
    && ok "the node refuses transfer calls without the master's token" || bad "node refuses unsigned transfer calls"

# --- a move that fails on the node: the node removes it, the master's server runs again
dx mvn "touch /tmp/fail-here"
job=$(move move "$SRV" local "$NID")
echo "$job" | json "d['state']" | grep -qx failed && ok "a move that can't start on the node fails" || { bad "failed move"; echo "$job"; }
check "after it, the server runs on the master again" dx mvm "systemctl is-active --quiet $SRV"
check "and the node has nothing left of it" bash -c "! docker exec mvn test -e /opt/$SRV -o -e /etc/systemd/system/$SRV.service"
dx mvn "rm /tmp/fail-here"

# --- the move
job=$(move move "$SRV" local "$NID")
echo "$job" | json "d['state']" | grep -qx done && ok "moved $SRV to the node" || { bad "move"; echo "$job"; }
echo "$job" | json "(d['result']['name'], d['result']['port'], d['result']['renamed'])" | grep -qx "('$SRV', 19140, False)" \
    && ok "same name and port on the node" || { bad "same name and port"; echo "$job"; }
check "it runs on the node" dx mvn "systemctl is-active --quiet $SRV"
[ "$(dx mvn "sha256sum < /opt/$SRV/worlds/Home/db/000005.ldb")" = "$WORLD_SUM" ] && ok "the world arrived intact" || bad "the world arrived intact"
check "its backup schedule came along" dx mvn "systemctl is-enabled --quiet redstonepanel-backup@$SRV.timer && grep -q '03:30' /etc/systemd/system/redstonepanel-backup@$SRV.timer"
check "its restart schedule came along" dx mvn "ls /etc/systemd/system/ | grep -q 'restart.*$SRV.*timer'"
check "the master no longer runs it" bash -c "! docker exec mvm test -e /etc/systemd/system/$SRV.service"
r=$(api jar GET /api/servers/removed)
echo "$r" | json "[s['moved_to'] for s in d if s['name']=='$SRV'][0]" | grep -q "on Basement" \
    && ok "the master keeps its files as a removed server marked 'moved to'" || { bad "moved_to on the master"; echo "$r"; }
check "no transfer archives are left" bash -c "[ -z \"\$(docker exec mvm ls /var/lib/redstonepanel/transfers)\" ] && [ -z \"\$(docker exec mvn ls /var/lib/redstonepanel/transfers)\" ]"

# --- a copy on the node itself: a new name and port, not started
job=$(move copy "$SRV" "$NID" "$NID" false)
COPY=$(echo "$job" | json "d['result']['name']")
[ "$COPY" = minecraft-bedrock-2 ] && ok "copied on the node as $COPY" || { bad "copy on the node"; echo "$job"; }
check "the original keeps running" dx mvn "systemctl is-active --quiet $SRV"
check "the copy isn't started" bash -c "! docker exec mvn systemctl is-active --quiet $COPY"
port=$(dx mvn "grep '^server-port=' /opt/$COPY/server.properties")
[ "$port" != "server-port=19140" ] && ok "the copy got its own port ($port)" || bad "the copy got its own port"

# --- the old copy: deleted only if a move left it, only by the master's admin
[ "$(code jar DELETE "/api/cluster/moved/$NID/$COPY")" = 404 ] && ok "a server no move left can't be deleted this way" || bad "refuse non-moved delete"
[ "$(code ujar DELETE "/api/cluster/moved/local/$SRV")" = 403 ] && ok "a master user can't delete the old copy" || bad "user delete refused"

# --- move it back: the master's name is taken by the old copy, so it gets another
job=$(move move "$SRV" "$NID" local)
echo "$job" | json "(d['state'], d['result']['name'], d['result']['renamed'])" | grep -qx "('done', 'minecraft-bedrock-2', True)" \
    && ok "moved back as minecraft-bedrock-2 (its name was taken)" || { bad "move back"; echo "$job"; }
[ "$(code jar DELETE "/api/cluster/moved/local/$SRV")" = 200 ] && ok "deleted the old copy on the master" || bad "delete the old copy on the master"
check "its folder is gone" bash -c "! docker exec mvm test -e /opt/$SRV"
[ "$(code jar DELETE "/api/cluster/moved/$NID/$SRV")" = 200 ] && ok "deleted the old copy on the node" || bad "delete the old copy on the node"
check "its folder is gone there too" bash -c "! docker exec mvn test -e /opt/$SRV"
check "logs name the move" dx mvm "grep -q '\[MOVE\] Moved minecraft-bedrock from Main to Basement' /var/log/redstonepanel/panel.log"

echo
[ "$FAILS" = 0 ] && echo "=== all passed" || echo "=== $FAILS failed"
exit "$FAILS"
