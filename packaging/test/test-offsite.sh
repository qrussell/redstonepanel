#!/bin/bash
# End-to-end test of backup copies off the machine (1.16.11): a panel (offp) copies a server's
# backups to an SSH server (offs, openssh-server + rsync) and to SeaweedFS's S3 server (offs3,
# which checks request signatures like real S3); a second panel (offn) plays the new machine that brings the server back
# from the SSH storage. Throwaway systemd containers on one Docker network, installing the .deb
# from dist/. The game server is a stand-in Bedrock server (a bedrock_server script that sleeps).
# Bringing it back on the new machine downloads the real Bedrock server, so that last part needs
# the internet; without it the test checks the world arrived and stops there.
# Run from the repository root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-offsite.sh          (KEEP=1 leaves the containers running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
NET=rp-offsite-test
IMG=rp-systemd:24.04
# MinIO no longer publishes images anyone can pull, so the S3 stand-in is SeaweedFS (pinned)
S3_IMAGE=${S3_IMAGE:-chrislusf/seaweedfs:3.80}
ALL="offp offn offs offs3"
SSH_IP=172.30.83.10
S3_IP=172.30.83.11
PASS_ADMIN='offsite-test-admin-1'       # throwaway accounts and keys inside the test containers only
PASS_USER='offsite-test-user-1'
S3_KEY='rptestaccess'
S3_SECRET='rptest-secret-key-123'
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
skip() { echo "SKIP  $1"; }
dx()   { docker exec "$1" bash -c "$2"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
wait_http() { for _ in $(seq 1 60); do dx "$1" "curl -sf http://127.0.0.1:8080/api/health" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
# api CONTAINER JAR METHOD PATH [JSON]
api()  { dx "$1" "curl -s -b /tmp/$2 -X $3 'http://127.0.0.1:8080$4' ${5:+-H 'Content-Type: application/json' -d '$5'}"; }
code() { dx "$1" "curl -s -o /dev/null -w '%{http_code}' -b /tmp/$2 -X $3 'http://127.0.0.1:8080$4' ${5:+-H 'Content-Type: application/json' -d '$5'}"; }
login(){ dx "$1" "curl -s -c /tmp/$2 -H 'Content-Type: application/json' -d '{\"username\":\"$3\",\"password\":\"$4\"}' http://127.0.0.1:8080/api/auth/login" >/dev/null; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
json() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
# on_ssh PANEL-ID SERVER: the copies the SSH storage keeps
on_ssh() { dx offs "ls /home/mcbackup/mc/$1/$2/ 2>/dev/null | grep -c 'tar.gz$'"; }
wait_ssh() { for _ in $(seq 1 90); do [ "$(on_ssh "$1" "$2")" = "$3" ] && return 0; sleep 1; done; return 1; }
# state TARGET: ok / failed / none, from the Backups tab
state() { api offp jar GET "/api/servers/$SRV/offsite" | json "([t for t in d['targets'] if t['id']=='$1'] or [{}])[0].get('status') and ('ok' if [t for t in d['targets'] if t['id']=='$1'][0]['status']['ok'] else 'failed') or 'none'"; }
wait_state() { for _ in $(seq 1 90); do [ "$(state "$1")" = "$2" ] && return 0; sleep 1; done; return 1; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Containers kept: $ALL (docker rm -f $ALL; docker network rm $NET)"; return; }
    docker rm -f $ALL >/dev/null 2>&1
    docker network rm "$NET" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== off-site backups test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f $ALL >/dev/null 2>&1
docker network rm "$NET" >/dev/null 2>&1
docker network create --subnet 172.30.83.0/24 "$NET" >/dev/null
docker run -d --privileged --cgroupns=private --name offp --hostname survival-box --network "$NET" -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
docker run -d --privileged --cgroupns=private --name offn --hostname new-box --network "$NET" -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
docker run -d --privileged --cgroupns=private --name offs --hostname nas --network "$NET" --ip "$SSH_IP" "$IMG" >/dev/null
S3=0
S3_CONF="{\"identities\":[{\"name\":\"rp\",\"credentials\":[{\"accessKey\":\"$S3_KEY\",\"secretKey\":\"$S3_SECRET\"}],\"actions\":[\"Admin\",\"Read\",\"Write\",\"List\",\"Tagging\"]}]}"
if docker run -d --name offs3 --network "$NET" --ip "$S3_IP" --entrypoint sh "$S3_IMAGE" \
        -c "echo '$S3_CONF' > /tmp/s3.json && exec weed server -s3 -s3.config=/tmp/s3.json -dir=/data" >/dev/null 2>&1; then
    S3=1
else
    skip "the S3 server image ($S3_IMAGE) can't be pulled here: the S3 checks are skipped"
fi
sleep 3
for c in offp offn; do
    # rsync and openssh-client are Recommends; install them as apt would
    ( dx "$c" "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") rsync openssh-client zip >/tmp/install.log 2>&1" \
        || { echo "install failed on $c"; dx "$c" "tail -20 /tmp/install.log"; } ) &
done
( dx offs "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends openssh-server rsync >/tmp/install.log 2>&1 \
    && useradd -m -s /bin/bash mcbackup && install -d -m 700 -o mcbackup -g mcbackup /home/mcbackup/.ssh && systemctl enable --now ssh >/dev/null 2>&1" \
    || echo "ssh server setup failed" ) &
wait
for c in offp offn; do check "$c: panel installed and running" wait_http "$c"; done
check "offs: SSH server running" dx offs "systemctl is-active --quiet ssh || systemctl is-active --quiet ssh.socket"

dx offp "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin && echo '$PASS_USER' | redstonepanel add-user alex --role user" >/dev/null
dx offn "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
login offp jar admin "$PASS_ADMIN"; login offp ujar alex "$PASS_USER"; login offn jar admin "$PASS_ADMIN"

# --- a stand-in Bedrock server with a 2 MB world
dx offp "rm -rf /tmp/srv && mkdir -p /tmp/srv/worlds/Home/db && cd /tmp/srv && printf '#!/bin/sh\nexec sleep infinity\n' > bedrock_server && chmod +x bedrock_server \
    && printf 'server-name=Island\ngamemode=survival\nforce-gamemode=false\ndifficulty=easy\nallow-cheats=false\nmax-players=10\nonline-mode=true\nallow-list=false\nserver-port=19132\nserver-portv6=19133\nenable-lan-visibility=true\nview-distance=32\ntick-distance=4\nplayer-idle-timeout=30\nmax-threads=8\nlevel-name=Home\nlevel-seed=\ndefault-player-permission-level=member\ntexturepack-required=false\ncontent-log-file-enabled=false\ncompression-threshold=1\nserver-authoritative-movement-strict=false\nchat-restriction=None\n' > server.properties \
    && printf '[]' > permissions.json && head -c 2000000 /dev/urandom > worlds/Home/db/000005.ldb \
    && head -c 1000 /dev/urandom > worlds/Home/marker.bin && zip -qr /tmp/srv.zip ."
SRV=$(dx offp "curl -s -b /tmp/jar -F file=@/tmp/srv.zip http://127.0.0.1:8080/api/servers/import" | json "d['server']")
[ "$SRV" = minecraft-bedrock ] && ok "stand-in server: $SRV" || bad "stand-in server"
dx offp "systemctl stop $SRV"
WORLD=$(dx offp "sha256sum < /opt/$SRV/worlds/Home/db/000005.ldb")
MARKER=$(dx offp "sha256sum < /opt/$SRV/worlds/Home/marker.bin")
# A port of its own, as a server that has been around a while would have: it must survive the trip
dx offp "sed -i 's/^server-port=.*/server-port=19140/; s/^server-portv6=.*/server-portv6=19141/' /opt/$SRV/server.properties"

# --- who may do what
[ "$(code offp ujar GET /api/settings/backup-storage)" = 403 ] && ok "a user can't see backup storage" || bad "user can't see storage"

# --- SSH storage: the panel's key goes into authorized_keys
KEY=$(api offp jar GET /api/settings/backup-storage | json "d['public_key']")
echo "$KEY" | grep -q '^ssh-ed25519 ' && ok "the panel made its SSH key" || bad "panel SSH key"
dx offs "echo '$KEY' >> /home/mcbackup/.ssh/authorized_keys && chown mcbackup:mcbackup /home/mcbackup/.ssh/authorized_keys && chmod 600 /home/mcbackup/.ssh/authorized_keys"
SSH_T="{\"type\":\"ssh\",\"name\":\"NAS\",\"host\":\"$SSH_IP\",\"user\":\"mcbackup\",\"folder\":\"mc\",\"keep_count\":2}"
api offp jar POST /api/settings/backup-storage/test "{\"fields\":{\"type\":\"ssh\",\"name\":\"x\",\"host\":\"$SSH_IP\",\"user\":\"nobody\",\"folder\":\"mc\"}}" \
    | json "d['error']" | grep -q "refused the panel's SSH key" && ok "a user without the key is explained" || bad "refused key explained"
OUT=$(api offp jar POST /api/settings/backup-storage "$SSH_T")
TS=$(echo "$OUT" | json "[t['id'] for t in d['targets'] if t['name']=='NAS'][0]")
PID=$(echo "$OUT" | json "d['panel_id']")
echo "$PID" | grep -q '^survival-box-[0-9a-f]\{4\}$' && ok "panel id $PID" || bad "panel id ($PID)"
api offp jar POST /api/settings/backup-storage/test "{\"id\":\"$TS\"}" | json "d['ok'] and ','.join(d['steps'])" \
    | grep -qx "signed in,wrote a file,listed it,deleted it" && ok "Test: signs in, writes, lists, deletes" || bad "SSH test"
api offp jar GET /api/settings/backup-storage | json "[t['host_key'] for t in d['targets'] if t['id']=='$TS'][0]" | grep -q "SHA256:" \
    && ok "the host key was recorded on first use" || bad "host key recorded"

# --- Back up now: copied and checked
[ "$(code offp ujar PUT "/api/servers/$SRV/offsite/$TS" '{"enabled":false}')" = 403 ] && ok "a user can't turn copies off" || bad "user toggle refused"
api offp jar POST "/api/servers/$SRV/backups" >/dev/null
wait_ssh "$PID" "$SRV" 1 && ok "Back up now is copied to the SSH storage" || bad "copied after Back up now"
F=$(dx offp "ls /var/backups/redstonepanel/$SRV/ | grep tar.gz | head -1")
[ "$(dx offp "sha256sum < /var/backups/redstonepanel/$SRV/$F" | cut -d' ' -f1)" = "$(dx offs "cat /home/mcbackup/mc/$PID/$SRV/$F.sha256")" ] \
    && [ "$(dx offs "sha256sum < /home/mcbackup/mc/$PID/$SRV/$F" | cut -d' ' -f1)" = "$(dx offs "cat /home/mcbackup/mc/$PID/$SRV/$F.sha256")" ] \
    && ok "the copy and its .sha256 match" || bad "copy checksum"
wait_state "$TS" ok && ok "the Backups tab shows it copied" || bad "status ok"

# --- scheduled backups: copied by their own unit, even with the panel stopped; Keep 2 on the storage
api offp jar PUT "/api/servers/$SRV/backups/schedule" '{"enabled":true,"frequency":"daily","time":"03:30","keep":5}' >/dev/null
dx offp "systemctl stop redstonepanel"
for _ in 1 2 3; do dx offp "RP_BACKUP_KIND=scheduled redstonepanel backup $SRV" >/dev/null; sleep 4; done
wait_ssh "$PID" "$SRV" 3 && ok "scheduled backups are copied with the panel stopped, keeping 2 there (plus the manual one)" \
    || { bad "scheduled copies / retention"; dx offs "ls /home/mcbackup/mc/$PID/$SRV/"; dx offp "tail -20 /var/log/redstonepanel/panel.log"; }
dx offp "grep -q 'Deleted the old copy' /var/log/redstonepanel/panel.log" && ok "the oldest scheduled copy was deleted there" || bad "retention logged"
[ "$(dx offp "ls /var/backups/redstonepanel/$SRV/ | grep -c scheduled")" = 3 ] && ok "all 3 stay here (Keep 5)" || bad "local Keep"
dx offp "systemctl start redstonepanel"; wait_http offp; login offp jar admin "$PASS_ADMIN"

# --- the storage is down: Needs attention, then it catches up
dx offs "systemctl stop ssh.socket ssh.service 2>/dev/null; systemctl stop ssh 2>/dev/null"
api offp jar POST "/api/servers/$SRV/backups" >/dev/null
wait_state "$TS" failed && ok "a copy to storage that's down fails" || bad "failure recorded"
api offp jar GET /api/host | json "[a['text'] for a in d['attention']]" | grep -q "Copying the backups of $SRV to 'NAS' failed" \
    && ok "it's a Needs attention item" || bad "needs attention"
dx offs "systemctl start ssh.socket 2>/dev/null; systemctl start ssh"
api offp jar POST "/api/servers/$SRV/offsite/sync" >/dev/null
wait_state "$TS" ok && ok "copying works again once it's back" || bad "recovered"

# --- restore from a copy after the local backups are gone
dx offp "rm -f /var/backups/redstonepanel/$SRV/*.tar.gz"
dx offp "head -c 1000 /dev/urandom > /opt/$SRV/worlds/Home/db/000005.ldb"
G=$(api offp jar GET "/api/servers/$SRV/offsite" | json "[c['file'] for t in d['targets'] if t['id']=='$TS' for c in t['copies'] if c['kind']=='manual'][0]")
R=$(api offp jar POST "/api/servers/$SRV/offsite/$TS/fetch" "{\"file\":\"$G\"}" | json "d['file']")
[ "$R" = "$G" ] && ok "a copy is downloaded and checked into the backups" || bad "fetch ($R)"
[ "$(code offp jar POST "/api/servers/$SRV/backups/$R/restore")" = 200 ] && ok "and restored" || bad "restore from copy"
[ "$(dx offp "sha256sum < /opt/$SRV/worlds/Home/db/000005.ldb")" = "$WORLD" ] && ok "the world is the one backed up" || bad "world restored"
dx offs "printf 'damaged' >> /home/mcbackup/mc/$PID/$SRV/$G"
dx offp "rm -f /var/backups/redstonepanel/$SRV/$G"
api offp jar POST "/api/servers/$SRV/offsite/$TS/fetch" "{\"file\":\"$G\"}" | json "d['detail']" | grep -q damaged \
    && ok "a damaged copy is refused" || bad "damaged copy refused"
check "and nothing is left of it here" dx offp "! ls /var/backups/redstonepanel/$SRV/ | grep -q '$G'"
[ "$(code offp jar DELETE "/api/servers/$SRV/offsite/$TS/$G")" = 200 ] && ok "a copy can be deleted" || bad "delete copy"
check "it's gone from the storage" dx offs "[ ! -e /home/mcbackup/mc/$PID/$SRV/$G ] && [ ! -e /home/mcbackup/mc/$PID/$SRV/$G.sha256 ]"

# --- the storage machine's host key changes: nothing is sent until an admin forgets the old one
dx offs "rm -f /etc/ssh/ssh_host_* && ssh-keygen -A >/dev/null && (systemctl restart ssh 2>/dev/null; true)"
api offp jar POST /api/settings/backup-storage/test "{\"id\":\"$TS\"}" | json "d['error']" | grep -q "host key isn't the one seen before" \
    && ok "a changed host key stops copies" || bad "host key change detected"
api offp jar POST "/api/settings/backup-storage/$TS/forget-host-key" >/dev/null
api offp jar POST /api/settings/backup-storage/test "{\"id\":\"$TS\"}" | json "d['ok']" | grep -qx True \
    && ok "Forget host key, then it works again" || bad "forget host key"

# --- S3: a backup over 100 MB goes up in parts
if [ "$S3" = 1 ]; then
    for _ in $(seq 1 60); do dx offp "curl -s -o /dev/null http://$S3_IP:8333/" && break; sleep 1; done
    S3_T="{\"type\":\"s3\",\"name\":\"Bucket\",\"endpoint\":\"http://$S3_IP:8333\",\"bucket\":\"rp-backups\",\"prefix\":\"mc\",\"region\":\"us-east-1\",\"access_key\":\"$S3_KEY\",\"secret_key\":\"$S3_SECRET\"}"
    # make the bucket with the panel's own S3 client
    dx offp "cd /usr/share/redstonepanel && python3 -c \"from core import backup_targets as T; T.S3Target({'endpoint': 'http://$S3_IP:8333', 'bucket': 'rp-backups', 'prefix': '', 'region': 'us-east-1', 'access_key': '$S3_KEY', 'secret_key': '$S3_SECRET'}).request('PUT', '')\"" \
        && ok "S3 bucket made" || bad "S3 bucket made"
    OUT=$(api offp jar POST /api/settings/backup-storage "$S3_T")
    T3=$(echo "$OUT" | json "[t['id'] for t in d['targets'] if t['name']=='Bucket'][0]")
    echo "$OUT" | grep -q "$S3_SECRET" && bad "the secret is never sent back" || ok "the secret is never sent back"
    api offp jar POST /api/settings/backup-storage/test "{\"id\":\"$T3\"}" | json "d['ok'] and ','.join(d['steps'])" \
        | grep -qx "wrote a file,listed it,deleted it" && ok "S3 Test: writes, lists, deletes" || bad "S3 test"
    api offp jar POST /api/settings/backup-storage/test "{\"id\":\"$T3\",\"fields\":$(echo "$S3_T" | sed 's/rptest-secret-key-123/wrong-secret/')}" \
        | json "d['error']" | grep -q "refused the access key" && ok "a wrong secret is explained" || bad "wrong secret explained"
    dx offp "head -c 110000000 /dev/urandom > /opt/$SRV/worlds/Home/db/000009.ldb"
    api offp jar POST "/api/servers/$SRV/backups" >/dev/null
    BIG=$(dx offp "ls -t /var/backups/redstonepanel/$SRV/ | grep manual | head -1")
    for _ in $(seq 1 120); do
        api offp jar GET "/api/servers/$SRV/offsite" | json "[c['file'] for t in d['targets'] if t['id']=='$T3' for c in t['copies']]" | grep -q "$BIG" && break
        sleep 2
    done
    api offp jar GET "/api/servers/$SRV/offsite" | json "[c['size'] for t in d['targets'] if t['id']=='$T3' for c in t['copies'] if c['file']=='$BIG'][0]" \
        | grep -qx "$(dx offp "stat -c %s /var/backups/redstonepanel/$SRV/$BIG")" && ok "a 110 MB backup went up in parts" || bad "S3 multipart copy"
    dx offp "grep -q \"Copied .* of $SRV to 'Bucket'\" /var/log/redstonepanel/panel.log" && ok "logged" || bad "S3 copy logged"
    dx offp "rm -f /var/backups/redstonepanel/$SRV/$BIG"
    [ "$(api offp jar POST "/api/servers/$SRV/offsite/$T3/fetch" "{\"file\":\"$BIG\"}" | json "d['file']")" = "$BIG" ] \
        && ok "and comes back checked" || bad "S3 fetch"
    dx offp "rm -f /opt/$SRV/worlds/Home/db/000009.ldb"
    api offp jar PUT "/api/servers/$SRV/offsite/$T3" '{"enabled":false}' >/dev/null
fi

# --- the machine died: a new panel brings the server back from the SSH storage
KEY2=$(api offn jar GET /api/settings/backup-storage | json "d['public_key']")
dx offs "echo '$KEY2' >> /home/mcbackup/.ssh/authorized_keys"
TN=$(api offn jar POST /api/settings/backup-storage "$(echo "$SSH_T" | sed 's/"keep_count":2/"servers":[]/')" | json "d['targets'][0]['id']")
api offn jar GET "/api/settings/backup-storage/$TN/browse" | json "sorted({c['panel'] for c in d['copies']})" | grep -q "$PID" \
    && ok "the new machine sees the old one's copies" || bad "browse"
NEWEST=$(api offn jar GET "/api/settings/backup-storage/$TN/browse" | json "[c['file'] for c in d['copies'] if c['panel']=='$PID'][0]")
ONLINE=0; dx offn "curl -s -m 10 -o /dev/null https://net-secondary.web.minecraft-services.net/api/v1.0/download/links" && ONLINE=1
OUT=$(api offn jar POST "/api/settings/backup-storage/$TN/import" "{\"panel\":\"$PID\",\"server\":\"$SRV\",\"file\":\"$NEWEST\"}")
# (a file Bedrock leaves alone: a real Bedrock server replaces the stand-in's LevelDB files)
[ "$(dx offn "sha256sum < /opt/$SRV/worlds/Home/marker.bin 2>/dev/null")" = "$MARKER" ] && ok "the world folder arrived on the new machine" || bad "world on new machine"
if [ "$ONLINE" = 1 ]; then
    echo "$OUT" | json "d['name']" | grep -qx "$SRV" && ok "it's a server again, with its old name" || { bad "imported server"; echo "$OUT"; }
    check "its Bedrock server was downloaded again and it runs" dx offn "[ -x /opt/$SRV/bedrock_server ] && systemctl is-enabled --quiet $SRV"
    echo "$OUT" | json "d['port']" | grep -qx 19140 && ok "it keeps its port, 19140" || { bad "port kept"; echo "$OUT"; }
    sleep 15    # let the real Bedrock server start and write its files
    check "the running server still has port 19140 in server.properties" dx offn "grep -qx 'server-port=19140' /opt/$SRV/server.properties"
    check "and listens on it" dx offn "ss -lun | grep -q ':19140 '"
else
    skip "no internet in the container: the Bedrock server itself can't be downloaded"
    { dx offn "[ -f /etc/systemd/system/$SRV.service ]" \
        || api offn jar GET /api/servers/removed | json "[s['name'] for s in d]" | grep -q "$SRV"; } \
        && ok "it has a service again, or waits under Removed servers" || { bad "set up on the new machine"; echo "$OUT"; }
    check "it keeps its port, 19140" dx offn "grep -qx 'server-port=19140' /opt/$SRV/server.properties"
fi

echo
[ "$FAILS" = 0 ] && echo "=== all off-site backup checks passed" || echo "=== $FAILS check(s) failed"
exit $((FAILS > 0))
