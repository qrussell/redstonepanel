#!/bin/bash
# End-to-end test of backup copies on another cluster member (1.16.8): a master (bcm) and two
# nodes (bcn1, bcn2), throwaway systemd containers on one Docker network, all installing the
# .deb from dist/. Game servers are stand-in Bedrock servers (a bedrock_server script that
# sleeps), kept stopped so backups don't wait for "save hold"; nothing is downloaded.
# Run from the repository root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-backup-copies.sh          (KEEP=1 leaves the containers running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
NET=rp-copies-test
IMG=rp-systemd:24.04
ALL="bcm bcn1 bcn2"
PASS_ADMIN='copies-test-admin-1'        # throwaway accounts inside the test containers only
PASS_USER='copies-test-user-1'
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$1" bash -c "$2"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
wait_https() { for _ in $(seq 1 60); do dx "$1" "curl -skf https://127.0.0.1:8443/api/health" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
api()  { dx bcm "curl -sk -b /tmp/$1 -X $2 'https://127.0.0.1:8443$3' ${4:+-H 'Content-Type: application/json' -d '$4'}"; }
code() { dx bcm "curl -sk -o /dev/null -w '%{http_code}' -b /tmp/$1 -X $2 'https://127.0.0.1:8443$3' ${4:+-H 'Content-Type: application/json' -d '$4'}"; }
login(){ dx bcm "curl -sk -c /tmp/$1 -H 'Content-Type: application/json' -d '{\"username\":\"$2\",\"password\":\"$3\"}' https://127.0.0.1:8443/api/auth/login" >/dev/null; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
json() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
# copies CONTAINER PANEL SERVER: the copies a member keeps of PANEL's SERVER
copies() { dx "$1" "ls /var/backups/redstonepanel/remote/$2/$3/ 2>/dev/null | grep -c 'tar.gz$'"; }
# wait_copies CONTAINER PANEL SERVER N
wait_copies() { for _ in $(seq 1 60); do [ "$(copies "$1" "$2" "$3")" = "$4" ] && return 0; sleep 1; done; return 1; }
# copy_state MEMBER SERVER: ok / failed / none, from the master's Backups tab view
copy_state() { api jar GET "/api/cluster/backup-copies/$1/$2" | json "'none' if not d.get('status') else ('ok' if d['status']['ok'] else 'failed')"; }
wait_state() { for _ in $(seq 1 60); do [ "$(copy_state "$1" "$2")" = "$3" ] && return 0; sleep 1; done; return 1; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Containers kept: $ALL (docker rm -f $ALL; docker network rm $NET)"; return; }
    docker rm -f $ALL >/dev/null 2>&1
    docker network rm "$NET" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== backup copies test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f $ALL >/dev/null 2>&1
docker network rm "$NET" >/dev/null 2>&1
docker network create --subnet 172.30.81.0/24 "$NET" >/dev/null
for c in $ALL; do
    docker run -d --privileged --cgroupns=private --name "$c" --hostname "$c" --network "$NET" \
        -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
done
sleep 3
for c in $ALL; do
    ( dx "$c" "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") zip >/tmp/install.log 2>&1" \
        || { echo "install failed on $c"; dx "$c" "tail -20 /tmp/install.log"; } ) &
done
wait
for c in $ALL; do check "$c: panel installed and running" dx "$c" "systemctl is-active --quiet redstonepanel"; done

# --- the cluster: bcm is the master, bcn1 and bcn2 join
dx bcm "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin && echo '$PASS_USER' | redstonepanel add-user alex --role user" >/dev/null
dx bcm "redstonepanel set-name Main && redstonepanel cluster create Home" >/dev/null
wait_https bcm || bad "bcm: master serves HTTPS"
for pair in bcn1:Basement bcn2:Attic; do
    c=${pair%%:*}; n=${pair#*:}
    join=$(dx bcm "redstonepanel cluster join-code" | grep '^rpjoin1:')
    out=$(dx "$c" "redstonepanel set-name $n && redstonepanel join '$join'" 2>&1)
    echo "$out" | grep -q "Joined cluster 'Home'" && ok "$c joined as $n" || { bad "$c joined"; echo "$out"; }
    wait_https "$c" || bad "$c: back up on HTTPS"
done
login jar admin "$PASS_ADMIN"
login ujar alex "$PASS_USER"
# 1.18: a new user account starts with no servers; give alex what users could always do, everywhere
api jar PUT /api/users/alex/grants '{"grants":{"*":["view","console","power","players","settings","backups"]}}' >/dev/null
OV=$(api jar GET /api/cluster/overview)
N1=$(echo "$OV" | json "[n['id'] for n in d['nodes'] if n['name']=='Basement'][0]")
N2=$(echo "$OV" | json "[n['id'] for n in d['nodes'] if n['name']=='Attic'][0]")
echo "$OV" | json "all((n.get('cluster_api') or 0) >= 4 for n in d['nodes'] if not n.get('local'))" | grep -qx True \
    && ok "the nodes speak cluster contract 4 or later" || { bad "cluster contract 4"; echo "$OV"; }

# --- stand-in Bedrock servers: one on the master, one on Basement (imported through the master)
mkzip() {   # CONTAINER SEED-BYTES
    dx "$1" "rm -rf /tmp/srv && mkdir -p /tmp/srv/worlds/Home/db && cd /tmp/srv && printf '#!/bin/sh\nexec sleep infinity\n' > bedrock_server && chmod +x bedrock_server \
        && printf 'server-port=19140\nserver-portv6=19141\nlevel-name=Home\nserver-name=Island\n' > server.properties \
        && printf '[]' > permissions.json && head -c $2 /dev/urandom > worlds/Home/db/000005.ldb && rm -f /tmp/srv.zip && zip -qr /tmp/srv.zip ."
}
mkzip bcm 2000000
SRV=$(dx bcm "curl -sk -b /tmp/jar -F file=@/tmp/srv.zip https://127.0.0.1:8443/api/servers/import" | json "d['server']")
[ "$SRV" = minecraft-bedrock ] && ok "stand-in server on the master: $SRV" || bad "stand-in server on the master"
mkzip bcm 1500000
NSRV=$(dx bcm "curl -sk -b /tmp/jar -F file=@/tmp/srv.zip https://127.0.0.1:8443/api/cluster/nodes/$N1/proxy/api/servers/import" | json "d['server']")
[ -n "$NSRV" ] && ok "stand-in server on Basement: $NSRV" || bad "stand-in server on Basement"
dx bcm "systemctl stop $SRV"; dx bcn1 "systemctl stop $NSRV"
NWORLD=$(dx bcn1 "sha256sum < /opt/$NSRV/worlds/Home/db/000005.ldb")

# --- who may do what
[ "$(code ujar PUT "/api/cluster/backup-copies/local/$SRV" "{\"to\":\"$N1\"}")" = 403 ] \
    && ok "a master user can't choose where copies go" || bad "user can't set copies"
[ "$(code jar PUT "/api/cluster/backup-copies/local/$SRV" '{"to":"local"}')" = 400 ] \
    && ok "a copy on the same member is refused" || bad "same-member copy refused"
[ "$(code jar GET "/api/cluster/nodes/$N1/proxy/api/cluster/held-backups")" = 403 ] \
    && ok "the proxy never passes on holder routes" || bad "proxy blocks holder routes"
[ "$(dx bcn1 "curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:8443/api/cluster/held-backups")" = 401 ] \
    && ok "a node refuses holder calls without the master's token" || bad "node refuses unsigned holder calls"

# --- the master's server, copied to Basement after Back up now
[ "$(code jar PUT "/api/cluster/backup-copies/local/$SRV" "{\"to\":\"$N1\"}")" = 200 ] && ok "copies of $SRV go to Basement" || bad "set target"
api jar POST "/api/servers/$SRV/backups" >/dev/null
api jar POST "/api/cluster/backup-copies/local/$SRV/sync" >/dev/null
wait_copies bcn1 master "$SRV" 1 && ok "Basement keeps the copy" || bad "Basement keeps the copy"
F=$(dx bcm "ls /var/backups/redstonepanel/$SRV/ | grep tar.gz | head -1")
[ "$(dx bcm "sha256sum < /var/backups/redstonepanel/$SRV/$F")" = "$(dx bcn1 "sha256sum < /var/backups/redstonepanel/remote/master/$SRV/$F")" ] \
    && ok "the copy is identical" || bad "the copy is identical"
check "the copy is root-only" dx bcn1 "[ \$(stat -c %a /var/backups/redstonepanel/remote/master/$SRV/$F) = 600 ]"
wait_state local "$SRV" ok && ok "the Backups tab shows it copied" || bad "status ok"

# --- the node's server, copied to the other node through the master
[ "$(code jar PUT "/api/cluster/backup-copies/$N1/$NSRV" "{\"to\":\"$N2\"}")" = 200 ] && ok "copies of Basement's $NSRV go to Attic" || bad "set node target"
api jar POST "/api/cluster/nodes/$N1/proxy/api/servers/$NSRV/backups" >/dev/null
api jar POST "/api/cluster/backup-copies/$N1/$NSRV/sync" >/dev/null
wait_copies bcn2 "$N1" "$NSRV" 1 && ok "Attic keeps a copy of Basement's server" || bad "node to node copy"

# --- the same retention as the source: Keep 2 scheduled backups
api jar PUT "/api/servers/$SRV/backups/schedule" '{"enabled":true,"frequency":"daily","time":"03:30","keep":2}' >/dev/null
for _ in 1 2 3; do dx bcm "RP_BACKUP_KIND=scheduled redstonepanel backup $SRV" >/dev/null; sleep 1.2; done
api jar POST "/api/cluster/backup-copies/local/$SRV/sync" >/dev/null
sleep 2
wait_copies bcn1 master "$SRV" 3 && ok "Basement keeps the manual copy and 2 scheduled ones" \
    || { bad "retention on the holder"; dx bcn1 "ls /var/backups/redstonepanel/remote/master/$SRV/"; }

# --- a member that's down: Needs attention and the next pass catches up
dx bcn1 "systemctl stop redstonepanel"
api jar POST "/api/servers/$SRV/backups" >/dev/null
api jar POST "/api/cluster/backup-copies/local/$SRV/sync" >/dev/null
wait_state local "$SRV" failed && ok "a copy to a stopped member fails" || bad "failure recorded"
api jar GET /api/host | json "[a['text'] for a in d['attention']]" | grep -q "Copying the backups of $SRV to Basement failed" \
    && ok "it's a Needs attention item" || bad "needs attention"
dx bcn1 "systemctl start redstonepanel"; wait_https bcn1
api jar POST "/api/cluster/backup-copies/local/$SRV/sync" >/dev/null
wait_state local "$SRV" ok && ok "copying works again once it's back" || bad "recovered"
wait_copies bcn1 master "$SRV" 4 && ok "and the missed backup was copied" || bad "missed backup copied"
check "logs name the failure" dx bcm "grep -q '\[BACKUP\] Copying $SRV.s backups to Basement failed' /var/log/redstonepanel/panel.log"

# --- Basement is lost: its server's world comes back from Attic's copy into a new server on the master
docker stop bcn1 >/dev/null
ALLC=$(api jar GET /api/cluster/backup-copies)
echo "$ALLC" | json "[m['name'] for m in d['unreachable']]" | grep -q Basement && ok "Basement is listed as not answering" || bad "unreachable listed"
C=$(echo "$ALLC" | json "[c for c in d['copies'] if c['panel']=='$N1' and c['server']=='$NSRV'][0]['file']")
[ -n "$C" ] && ok "Attic's copy is listed" || { bad "copy listed"; echo "$ALLC"; }
mkzip bcm 1000
NEW=$(dx bcm "curl -sk -b /tmp/jar -F file=@/tmp/srv.zip https://127.0.0.1:8443/api/servers/import" | json "d['server']")
dx bcm "systemctl stop $NEW"
[ "$(code ujar POST "/api/cluster/backup-copies/file/$N2/$N1/$NSRV/$C/into" "{\"member\":\"local\",\"server\":\"$NEW\"}")" = 403 ] \
    && ok "a master user can't put a copy into a server" || bad "user put-into refused"
r=$(api jar POST "/api/cluster/backup-copies/file/$N2/$N1/$NSRV/$C/into" "{\"member\":\"local\",\"server\":\"$NEW\"}")
INTO=$(echo "$r" | json "d['file']")
[ -n "$INTO" ] && ok "put the copy into $NEW's backups as $INTO" || { bad "put into"; echo "$r"; }
r=$(api jar POST "/api/servers/$NEW/backups/$INTO/restore")
[ "$(dx bcm "sha256sum < /opt/$NEW/worlds/Home/db/000005.ldb")" = "$NWORLD" ] && ok "restored Basement's world on the master" \
    || { bad "restore from the copy"; echo "$r"; }
[ "$(dx bcm "curl -sk -b /tmp/ujar https://127.0.0.1:8443/api/cluster/backup-copies/file/$N2/$N1/$NSRV/$C | sha256sum")" \
  = "$(dx bcn2 "sha256sum < /var/backups/redstonepanel/remote/$N1/$NSRV/$C")" ] && ok "a master user can download the copy" || bad "download"
[ "$(code jar DELETE "/api/cluster/backup-copies/file/$N2/$N1/$NSRV/$C")" = 200 ] && ok "an admin deleted the copy" || bad "delete copy"
check "it's gone from Attic" bash -c "[ \"\$(docker exec bcn2 bash -c 'ls /var/backups/redstonepanel/remote/$N1/$NSRV/ | grep -c tar.gz$')\" = 0 ]"

echo
[ "$FAILS" = 0 ] && echo "=== all passed" || echo "=== $FAILS failed"
exit "$FAILS"
