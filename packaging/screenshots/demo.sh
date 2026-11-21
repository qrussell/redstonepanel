#!/bin/bash
# Builds the demo cluster the user guide's screenshots come from, then takes them.
#
#   "Survival Box" (master of cluster "Home"): a Paper server with plugins and a Bedrock server
#   "Creative Lab" (node):                     a Fabric server with mods, created from the master
#
# Two throwaway systemd containers on one Docker network install the newest .deb from dist/.
# Servers, Java, mods and plugins are downloaded from the internet, so this takes a while.
# Run from the repository root after building the .deb (see README → Building from source):
#
#   bash packaging/screenshots/demo.sh            build the demo, take the screenshots, clean up
#   KEEP=1 bash packaging/screenshots/demo.sh     leave the containers running afterwards
#   SHOOT=0 bash packaging/screenshots/demo.sh    build the demo only (implies KEEP=1)
#   SETUP=0 bash packaging/screenshots/demo.sh    retake the screenshots on a demo kept earlier
#
# If taking the screenshots fails, the containers are kept so SETUP=0 can try again.
# Screenshots are written to docs/guide/images/ in the dark theme and docs/guide/images/light/
# in the light theme (see shoot.py for the list). The demo account's
# password is random and only exists inside the containers.
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
NET=rp-demo
IMG=rp-systemd:24.04
M=rp-demo-m
N=rp-demo-n
PLAYWRIGHT=${PLAYWRIGHT:-mcr.microsoft.com/playwright/python:v1.49.0-noble}
SHOOT=${SHOOT:-1}
SETUP=${SETUP:-1}
[ "$SHOOT" = 1 ] || KEEP=1
[ "$SETUP" = 1 ] || KEEP=1
PASS=$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')
PASSPHRASE=$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')

say()  { echo "--- $*"; }
dx()   { docker exec "$1" bash -c "$2"; }
ip_of(){ docker inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" "$1"; }
wait_https() { for _ in $(seq 1 90); do dx "$1" "curl -skf https://127.0.0.1:8443/api/health" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
# api METHOD PATH [JSON]: the demo admin on the master
api()  { dx $M "curl -sk -b /tmp/jar -X $1 'https://127.0.0.1:8443$2' ${3:+-H 'Content-Type: application/json' -d '$3'}"; echo; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Containers kept: $M $N (docker rm -f $M $N; docker network rm $NET)"; return; }
    docker rm -f $M $N >/dev/null 2>&1
    docker network rm "$NET" >/dev/null 2>&1
}
trap cleanup EXIT

if [ "$SETUP" = 1 ]; then
DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first."; exit 1; }
say "demo with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f $M $N >/dev/null 2>&1
docker network rm "$NET" >/dev/null 2>&1
docker network create --subnet 172.30.79.0/24 "$NET" >/dev/null
docker run -d --privileged --cgroupns=private --name $M --hostname survival-box --network "$NET" \
    -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
docker run -d --privileged --cgroupns=private --name $N --hostname creative-lab --network "$NET" \
    -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
sleep 3
for c in $M $N; do
    dx $c "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq /dist/$(basename "$DEB") openjdk-21-jre-headless ufw >/tmp/install.log 2>&1 && ufw allow 8080/tcp >/dev/null && ufw allow 8443/tcp >/dev/null && ufw --force enable >/dev/null" \
        || { echo "install failed on $c"; dx $c "tail -20 /tmp/install.log"; exit 1; } &
done
wait

say "cluster Home: Survival Box (master) and Creative Lab (node)"
dx $M "echo '$PASS' | redstonepanel add-user admin --role admin && (umask 077; echo '$PASS' > /root/.demo-pass)" >/dev/null
dx $M "redstonepanel set-name 'Survival Box' && redstonepanel cluster create Home" >/dev/null
wait_https $M || { echo "master isn't serving HTTPS"; exit 1; }
code=$(dx $M "redstonepanel cluster join-code" | grep '^rpjoin1:')
dx $N "redstonepanel set-name 'Creative Lab' && redstonepanel join '$code'" || { echo "the node couldn't join"; exit 1; }
wait_https $N || { echo "node isn't serving HTTPS"; exit 1; }
dx $M "curl -sk -c /tmp/jar -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PASS\"}' https://127.0.0.1:8443/api/auth/login" >/dev/null
NID=$(api GET /api/cluster/overview | python3 -c "import json,sys; d=json.load(sys.stdin); print([n['id'] for n in d['nodes'] if not n.get('local')][0])" 2>/dev/null \
      || api GET /api/cluster/overview | python -c "import json,sys; d=json.load(sys.stdin); print([n['id'] for n in d['nodes'] if not n.get('local')][0])")
P="/api/cluster/nodes/$NID/proxy"

say "servers (downloads Paper, Bedrock and Fabric)"
api POST /api/servers/deploy '{"edition":"java","loader":"paper","version":"1.21.4"}'
api POST /api/servers/deploy '{"edition":"bedrock"}'
api POST "$P/api/servers/deploy" '{"edition":"java","loader":"fabric","version":"1.21.4"}'
api POST /api/servers/minecraft-java/action '{"action":"start"}'
api POST /api/servers/minecraft-bedrock/action '{"action":"start"}'
api POST "$P/api/servers/minecraft-java/action" '{"action":"start"}'

say "plugins and mods from the catalog"
api POST /api/servers/minecraft-java/addons/bundle '{"bundle":"essentials"}'
api POST "$P/api/servers/minecraft-java/addons/bundle" '{"bundle":"performance"}'

say "waiting for the servers to finish starting"
for _ in $(seq 1 90); do
    dx $M "grep -q 'Done (' /opt/minecraft-java/logs/latest.log 2>/dev/null" \
        && dx $N "grep -q 'Done (' /opt/minecraft-java/logs/latest.log 2>/dev/null" && break
    sleep 2
done
api POST /api/servers/minecraft-java/chat/setup
api POST "$P/api/servers/minecraft-java/chat/setup"
api POST /api/servers/minecraft-java/action '{"action":"restart"}'
api POST "$P/api/servers/minecraft-java/action" '{"action":"restart"}'
sleep 40

say "backups, restarts, a template, nightly cluster backups"
for s in /api/servers/minecraft-java /api/servers/minecraft-bedrock "$P/api/servers/minecraft-java"; do
    api PUT "$s/backups/schedule" '{"enabled":true,"frequency":"daily","time":"04:00","keep":7}'
    api POST "$s/backups"
done
api PUT /api/servers/minecraft-java/restarts '{"enabled":true,"frequency":"daily","time":"05:00","warn":5}'
api POST /api/servers/minecraft-java/template/save '{"name":"Paper survival"}'
dx $M "printf '%s\n%s\n' '$PASSPHRASE' '$PASSPHRASE' | redstonepanel cluster nightly on" >/dev/null 2>&1 \
    || api PUT /api/settings/cluster/nightly "{\"enabled\":true,\"passphrase\":\"$PASSPHRASE\"}"
api POST /api/servers/minecraft-java/chat '{"message":"Welcome to Survival Box!"}'

say "off-site copies: an SSH storage (on Creative Lab) for the master's servers"
dx $N "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends openssh-server rsync >/dev/null 2>&1 \
    && (id mcbackup >/dev/null 2>&1 || useradd -m -s /bin/bash mcbackup) && install -d -m 700 -o mcbackup -g mcbackup /home/mcbackup/.ssh \
    && systemctl enable --now ssh >/dev/null 2>&1"
dx $M "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends rsync openssh-client >/dev/null 2>&1"
KEY=$(api GET /api/settings/backup-storage | python3 -c "import json,sys; print(json.load(sys.stdin)['public_key'])" 2>/dev/null \
      || api GET /api/settings/backup-storage | python -c "import json,sys; print(json.load(sys.stdin)['public_key'])")
dx $N "echo '$KEY' > /home/mcbackup/.ssh/authorized_keys && chown mcbackup:mcbackup /home/mcbackup/.ssh/authorized_keys && chmod 600 /home/mcbackup/.ssh/authorized_keys"
api POST /api/settings/backup-storage "{\"type\":\"ssh\",\"name\":\"Creative Lab disk\",\"host\":\"$(ip_of $N)\",\"user\":\"mcbackup\",\"folder\":\"minecraft-backups\",\"keep_count\":14,\"keep_days\":30}" >/dev/null
api POST /api/servers/minecraft-java/backups >/dev/null
api POST /api/servers/minecraft-bedrock/backups >/dev/null

else
    PASS=$(docker exec $M cat /root/.demo-pass) || { echo "No demo to reuse: run without SETUP=0 first."; exit 1; }
fi

if [ "$SHOOT" = 1 ]; then
    say "screenshots"
    BASE="https://$(ip_of $M):8443"
    docker run --rm --network "$NET" -e BASE="$BASE" -e RP_USER=admin -e RP_PASS="$PASS" \
        -v "$HOST_PWD/packaging/screenshots:/shots:ro" -v "$HOST_PWD/docs/guide/images:/out" \
        "$PLAYWRIGHT" bash -c "pip install -q playwright==1.49.0 2>/dev/null || pip install -q --break-system-packages playwright==1.49.0; python3 /shots/shoot.py" \
        || { KEEP=1; echo "Taking the screenshots failed; retry with SETUP=0."; exit 1; }
else
    echo "Demo ready: https://$(ip_of $M):8443 (docker network $NET)"
fi
