#!/bin/bash
# End-to-end test of clusters phase 2 (1.15): a master (rpm) and a node (rpn), two throwaway
# systemd containers on one Docker network, both installing the .deb from dist/. The master manages
# the node's game server through its proxy (every tab), with admin and user rights kept apart, pushes
# a template, creates a server on the node (installing Java there first), switches its own HTTPS to
# a custom certificate, renews the node's certificate automatically and writes a nightly backup.
# Run from the repository root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-cluster-phase2.sh       (KEEP=1 leaves the containers running,
#                                                    DEPLOY=0 skips the deploy, which downloads Java
#                                                    and a Minecraft server from the internet)
set -uo pipefail
# Git Bash on Windows: keep container paths as they are, and mount the Windows path of dist/
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
NET=rp-phase2-test
IMG=rp-systemd:24.04
PASS_ADMIN='phase2-test-admin-1'      # throwaway accounts inside the test containers only
PASS_USER='phase2-test-user-1'
PASSPHRASE='phase2 nightly passphrase'
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$1" bash -c "$2"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
ip_of(){ docker inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" "$1"; }
wait_https() { for _ in $(seq 1 60); do dx "$1" "curl -skf https://127.0.0.1:8443/api/health" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
# API calls on the master, as the admin (jar) or the user (ujar): api JAR METHOD PATH [JSON]
api()  { dx rpm "curl -sk -b /tmp/$1 -X $2 'https://127.0.0.1:8443$3' ${4:+-H 'Content-Type: application/json' -d '$4'}"; }
code() { dx rpm "curl -sk -o /dev/null -w '%{http_code}' -b /tmp/$1 -X $2 'https://127.0.0.1:8443$3' ${4:+-H 'Content-Type: application/json' -d '$4'}"; }
login(){ dx rpm "curl -sk -c /tmp/$1 -H 'Content-Type: application/json' -d '{\"username\":\"$2\",\"password\":\"$3\"}' https://127.0.0.1:8443/api/auth/login" >/dev/null; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
json() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Containers kept: rpm rpn (docker rm -f rpm rpn; docker network rm $NET)"; return; }
    docker rm -f rpm rpn >/dev/null 2>&1
    docker network rm "$NET" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== cluster phase 2 test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f rpm rpn >/dev/null 2>&1
docker network rm "$NET" >/dev/null 2>&1
docker network create --subnet 172.30.78.0/24 "$NET" >/dev/null
for c in rpm rpn; do
    docker run -d --privileged --cgroupns=private --name "$c" --hostname "$c" --network "$NET" \
        -v "$HOST_PWD/dist:/dist:ro" ${PUBLISH:+$( [ "$c" = rpm ] && echo "-p $PUBLISH:8443")} "$IMG" >/dev/null
done
sleep 3
for c in rpm rpn; do
    ( dx "$c" "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") >/tmp/install.log 2>&1" \
        || { echo "install failed on $c"; dx "$c" "tail -20 /tmp/install.log"; } ) &
done
wait
for c in rpm rpn; do check "$c: panel installed and running" dx "$c" "systemctl is-active --quiet redstonepanel"; done
M=$(ip_of rpm); N=$(ip_of rpn)
echo "  rpm=$M rpn=$N"

# --- a stand-in game server on the node (a minecraft*.service unit is all the panel needs)
dx rpn "mkdir -p /opt/minecraft-test && printf 'server-port=25570\nmotd=hello\n' > /opt/minecraft-test/server.properties
printf '[]' > /opt/minecraft-test/ops.json
cat > /etc/systemd/system/minecraft-test.service <<'EOF'
[Unit]
Description=stand-in game server
[Service]
WorkingDirectory=/opt/minecraft-test
ExecStart=/bin/sleep infinity
EOF
systemctl daemon-reload"

# --- the cluster: rpm is the master, rpn joins
dx rpm "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin && echo '$PASS_USER' | redstonepanel add-user alex --role user" >/dev/null
dx rpm "redstonepanel set-name Main && redstonepanel cluster create Home" >/dev/null
wait_https rpm || bad "rpm: master serves HTTPS"
code_=$(dx rpm "redstonepanel cluster join-code" | grep '^rpjoin1:')
out=$(dx rpn "redstonepanel set-name Basement && redstonepanel join '$code_'" 2>&1)
echo "$out" | grep -q "Joined cluster 'Home'" && ok "rpn joined" || { bad "rpn joined"; echo "$out"; }
wait_https rpn || bad "rpn: back up on HTTPS"
login jar admin "$PASS_ADMIN"
login ujar alex "$PASS_USER"

ov=$(api jar GET /api/cluster/overview)
NID=$(echo "$ov" | json "[n['id'] for n in d['nodes'] if not n.get('local')][0]")
echo "$ov" | json "[n.get('cluster_api') for n in d['nodes'] if not n.get('local')][0]" | grep -qxE "[2-9]" \
    && ok "overview: node speaks cluster contract 2 or later" || { bad "overview: node speaks cluster contract 2 or later"; echo "$ov"; }
P="/api/cluster/nodes/$NID/proxy"
S="$P/api/servers/minecraft-test"

# --- every tab through the proxy
r=$(api jar GET "$S/properties")
echo "$r" | grep -q '"server-port": *"25570"' && ok "proxy: read the node server's properties" || { bad "proxy: read properties"; echo "$r"; }
api jar POST "$S/properties/raw" '{"content":"server-port=25570\nmotd=from the master\n"}' >/dev/null
check "proxy: properties written on the node" dx rpn "grep -q 'motd=from the master' /opt/minecraft-test/server.properties"
r=$(api jar POST "$S/action" '{"action":"start"}'); sleep 1
check "proxy: start the node's server" dx rpn "systemctl is-active --quiet minecraft-test"
api jar POST "$S/action" '{"action":"stop"}' >/dev/null; sleep 1
check "proxy: stop it again" bash -c "! docker exec rpn systemctl is-active --quiet minecraft-test"
[ "$(code jar GET "$S/players")" = 200 ] && ok "proxy: players" || bad "proxy: players"
[ "$(code jar GET "$S/backups")" = 200 ] && ok "proxy: backups tab" || bad "proxy: backups tab"
r=$(api jar POST "$S/backups")
BK=$(echo "$r" | json "d['file']" 2>/dev/null)
[ -n "$BK" ] && ok "proxy: back up now on the node ($BK)" || { bad "proxy: back up now"; echo "$r"; }
size=$(dx rpm "curl -sk -b /tmp/jar -o /tmp/bk.tar.gz -w '%{size_download}' 'https://127.0.0.1:8443$S/backups/$BK' && tar tzf /tmp/bk.tar.gz >/dev/null && echo ok")
echo "$size" | grep -q ok && ok "proxy: download the node's backup (streamed)" || { bad "proxy: download backup"; echo "$size"; }

# files: admins only, on the node too
r=$(api jar PUT "$S/files/text" '{"path":"notes.txt","content":"written from the master\n"}')
check "proxy: admin writes a file on the node" dx rpn "grep -q 'written from the master' /opt/minecraft-test/notes.txt"
[ "$(dx rpm "curl -sk -b /tmp/jar 'https://127.0.0.1:8443$S/files/download?path=notes.txt'")" = "written from the master" ] \
    && ok "proxy: download a file" || bad "proxy: download a file"
dx rpm "head -c 300000 /dev/urandom > /tmp/blob.bin"
dx rpm "curl -sk -b /tmp/jar -F file=@/tmp/blob.bin 'https://127.0.0.1:8443$S/files/upload?path='" >/dev/null
[ "$(dx rpm "sha256sum < /tmp/blob.bin")" = "$(dx rpn "sha256sum < /opt/minecraft-test/blob.bin")" ] \
    && ok "proxy: upload a file (multipart, 300 KB)" || bad "proxy: upload a file"
[ "$(code ujar GET "$S/files")" = 403 ] && ok "proxy: a master user can't use the node's file manager" || bad "proxy: user file manager refused"
[ "$(code ujar GET "$P/api/java/runtimes")" = 403 ] && ok "proxy: a master user can't manage Java" || bad "proxy: user Java refused"
[ "$(code jar GET "$P/api/java/runtimes")" = 200 ] && ok "proxy: a master admin can" || bad "proxy: admin Java"
[ "$(code ujar GET "$S/properties")" = 200 ] && ok "proxy: a master user can configure servers" || bad "proxy: user configure"

# never: users, settings, membership, erasing files, panel updates
for t in "GET /api/users" "PUT /api/settings" "DELETE /api/cluster/membership" "POST /api/panel/update" \
         "DELETE /api/servers/removed/minecraft-test" "GET /api/settings/cluster"; do
    set -- $t
    c=$(code jar "$1" "$P$2")
    [ "$c" = 403 ] && ok "proxy refuses $1 $2" || bad "proxy refuses $1 $2 (got $c)"
done
[ "$(code jar DELETE "$S?keep_world=false")" = 403 ] && ok "proxy: purging a node's server is refused" || bad "proxy: purge refused"

# logs on both panels name the master's user
check "node log: [CLUSTER] admin@Main" dx rpn "grep -q '\[CLUSTER\] admin@Main: PUT /api/servers/minecraft-test/files/text' /var/log/redstonepanel/panel.log"
check "master log: [CLUSTER] admin@Main ... on Basement" dx rpm "grep -q '\[CLUSTER\] admin@Main: PUT /api/servers/minecraft-test/files/text on Basement' /var/log/redstonepanel/panel.log"

# --- templates: Save as Template from the node's server lands on the master; pushing copies it back
api jar POST "/api/cluster/nodes/$NID/servers/minecraft-test/template" '{"name":"From node"}' >/dev/null
TID=$(api jar GET /api/templates | json "[t['id'] for t in d if t['name']=='From node'][0]")
[ -n "$TID" ] && ok "a node's server saved as a master template" || bad "save template from node"
api jar POST "/api/cluster/nodes/$NID/templates" "{\"template_id\":\"$TID\"}" >/dev/null
check "template pushed to the node" dx rpn "grep -q '$TID' /var/lib/redstonepanel/templates.json"

# --- New Server on the node: Java is missing there, installed there, then the server is created
if [ "${DEPLOY:-1}" = 1 ]; then
    r=$(dx rpm "curl -sk -b /tmp/jar -w ' %{http_code}' -H 'Content-Type: application/json' -d '{\"edition\":\"java\",\"loader\":\"vanilla\",\"version\":\"1.21.4\"}' 'https://127.0.0.1:8443$P/api/servers/deploy'")
    if echo "$r" | grep -q ' 409$'; then
        need=$(echo "${r% 409}" | json "d['java']['required']")
        ok "deploy on the node: 409 asks for Java $need (from the node)"
        api jar POST "$P/api/java/runtimes/install" "{\"major\":$need}" >/dev/null
        for _ in $(seq 1 120); do
            st=$(api jar GET "$P/api/java/runtimes/job" | json "(d['job'] or {}).get('state','none')")
            [ "$st" = running ] || break; sleep 3
        done
        [ "$st" = done ] && ok "Java $need installed on the node from the master" || bad "Java install on the node ($st)"
        check "java is on the node, not the master" bash -c "docker exec rpn bash -c 'ls /usr/lib/jvm | grep -q $need' && ! docker exec rpm bash -c 'ls /usr/lib/jvm 2>/dev/null | grep -q $need'"
        r=$(dx rpm "curl -sk -b /tmp/jar -w ' %{http_code}' -H 'Content-Type: application/json' -d '{\"edition\":\"java\",\"loader\":\"vanilla\",\"version\":\"1.21.4\"}' 'https://127.0.0.1:8443$P/api/servers/deploy'")
    fi
    echo "$r" | grep -q ' 200$' && ok "deploy on the node from the master" || { bad "deploy on the node"; echo "$r"; }
    check "the new server exists on the node only" bash -c "docker exec rpn bash -c 'ls /etc/systemd/system/minecraft-java*.service' && ! docker exec rpm bash -c 'ls /etc/systemd/system/minecraft-java*.service'"
    NEW=$(api jar GET "$P/api/servers" | json "[s['name'] for s in d if s['name'].startswith('minecraft-java')][0]")

    # uninstall (keeping the world) and restore it, from the master
    [ "$(code jar DELETE "$P/api/servers/$NEW?keep_world=true")" = 200 ] && ok "proxy: uninstall $NEW keeping the world" || bad "proxy: uninstall"
    api jar GET "$P/api/servers/removed" | grep -q "$NEW" && ok "proxy: listed as removed on the node" || bad "proxy: removed list"
    r=$(api jar POST "$P/api/servers/removed/$NEW/restore")
    echo "$r" | grep -q '"status": *"success"' && ok "proxy: restore it" || { bad "proxy: restore"; echo "$r"; }
    check "restored on the node" dx rpn "test -f /etc/systemd/system/$NEW.service"
else
    [ "$(code jar DELETE "$S?keep_world=true")" = 200 ] && ok "proxy: uninstall keeping the world" || bad "proxy: uninstall"
    api jar GET "$P/api/servers/removed" | grep -q minecraft-test && ok "proxy: listed as removed on the node" || bad "proxy: removed list"
fi

# --- certificates: automatic renewal 30 days before expiry (simulated by running the check 800 days ahead)
before=$(dx rpn "openssl x509 -in /var/lib/redstonepanel/cluster/node.crt -noout -serial")
dx rpm "cd /usr/share/redstonepanel && python3 -c 'import time; from core import cluster_client; cluster_client.maintain_once(print, lambda: None, now=time.time() + 800 * 86400)'" > "${TMPDIR:-/tmp}/rp-maint.log" 2>&1
grep -q "Renewed the certificate of node 'Basement' automatically" "${TMPDIR:-/tmp}/rp-maint.log" && ok "node certificate renewed automatically" \
    || { bad "node certificate renewed automatically"; cat "${TMPDIR:-/tmp}/rp-maint.log"; }
sleep 3; wait_https rpn
after=$(dx rpn "openssl x509 -in /var/lib/redstonepanel/cluster/node.crt -noout -serial")
[ "$before" != "$after" ] && ok "node serves a new certificate" || bad "node serves a new certificate"
[ "$(code jar GET "$S/properties")" = 200 ] && ok "proxy works with the renewed certificate" || bad "proxy after renewal"

# --- nightly backups
r=$(api jar PUT /api/settings/cluster/nightly "{\"enabled\":true,\"passphrase\":\"$PASSPHRASE\"}")
echo "$r" | grep -q '"enabled": *true' && ok "nightly backups turned on" || { bad "nightly on"; echo "$r"; }
dx rpm "cd /usr/share/redstonepanel && python3 -c 'from core import cluster; print(cluster.run_nightly())'" >/dev/null
f=$(dx rpm "ls /var/lib/redstonepanel/cluster/backups/")
[ -n "$f" ] && ok "nightly backup written ($f)" || bad "nightly backup written"
dx rpm "curl -sk -b /tmp/jar -o /tmp/n.rpbackup 'https://127.0.0.1:8443/api/settings/cluster/backups/$f' && cd /usr/share/redstonepanel && python3 -c \"from core import cluster; print(cluster.read_backup(open('/tmp/n.rpbackup','rb').read(), '$PASSPHRASE')[0]['cluster'])\"" | grep -qx Home \
    && ok "nightly backup downloads and decrypts" || bad "nightly backup downloads and decrypts"
[ "$(code ujar GET "/api/settings/cluster/backups/$f")" = 403 ] && ok "only admins download it" || bad "only admins download it"

# --- the master switches to its own certificate (as certbot would give it); nodes are unaffected
dx rpm "openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout /etc/ssl/private/own.key -out /etc/ssl/certs/own.crt -days 90 -subj /CN=panel.example.com -addext subjectAltName=DNS:panel.example.com >/dev/null 2>&1"
settings=$(api jar GET /api/settings | json "json.dumps(dict(d['settings'], https_mode='custom', cert_file='/etc/ssl/certs/own.crt', key_file='/etc/ssl/private/own.key'))")
dx rpm "cat > /tmp/s.json <<'EOF'
$settings
EOF
curl -sk -b /tmp/jar -X PUT -H 'Content-Type: application/json' --data @/tmp/s.json https://127.0.0.1:8443/api/settings" | grep -q '"restart": *true' \
    && ok "master switches to its own certificate" || bad "master switches to its own certificate"
sleep 3; wait_https rpm
check "master serves its own certificate" dx rpm "curl -sf --cacert /etc/ssl/certs/own.crt --resolve panel.example.com:8443:127.0.0.1 https://panel.example.com:8443/api/health"
login jar admin "$PASS_ADMIN"
[ "$(code jar GET "$S/properties")" = 200 ] && ok "and still manages the node" || bad "manages the node with its own certificate"
r=$(api jar POST /api/settings/cluster/joins '{}')
echo "$r" | json "d['code']" | $PY -c "import base64,json,sys; c=sys.stdin.read().strip()[8:]; print(json.loads(base64.urlsafe_b64decode(c + '=' * (-len(c) % 4)))['u'])" | grep -q '^https://panel.example.com:8443$' \
    && ok "join codes point at the certificate's name" || { bad "join codes point at the certificate's name"; echo "$r"; }

echo
[ "$FAILS" = 0 ] && echo "ALL PASSED" || echo "$FAILS FAILED"
exit $((FAILS > 0))
