#!/bin/bash
# End-to-end test of 1.21.8's game-folder hardening in a throwaway systemd container that
# installs the .deb from dist/. The panel runs as root, but a game server's folder belongs to
# the minecraft account, and the server (or a plugin) can put links in it. This plants such
# links as minecraft and checks that the panel's actions on that folder never read or change
# a root-only file through them: settings, operators, whitelist, downloads, world rename, logs.
# Nothing is downloaded: the server is an imported folder with a stand-in server.jar.
# Run from the repository root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-game-files.sh          (KEEP=1 leaves the container running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
C=rp-game-files-test
IMG=rp-systemd:24.04
PASS_ADMIN='game-files-admin-1'         # a throwaway account inside the test container only
B=http://127.0.0.1:8080
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$C" bash -c "$1"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
# api METHOD PATH [JSON]: prints the body, then the HTTP code on the last line
api()  { dx "curl -s -b /tmp/jar -H 'Origin: $B' -w '\n%{http_code}' -X $1 '$B$2' ${3:+-H 'Content-Type: application/json' -d '$3'}"; }
code() { api "$@" | tail -1; }
as_mc(){ dx "cd /opt/\$(cat /tmp/S) && runuser -u minecraft -- bash -c '$1'"; }
# hidden NAME METHOD PATH: the answer must not contain the root-only secret
hidden(){ local name=$1; shift; if api "$@" | grep -q TOPSECRET; then bad "$name"; else ok "$name"; fi; }
target_intact() { dx "test \"\$(cat /etc/rp-target)\" = ORIGINAL && test \"\$(stat -c %U /etc/rp-target)\" = root"; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Container kept: $C (docker rm -f $C)"; return; }
    docker rm -f "$C" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== game folder test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f "$C" >/dev/null 2>&1
docker run -d --privileged --cgroupns=private --name "$C" --hostname "$C" -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
sleep 3
dx "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") zip unzip >/tmp/install.log 2>&1" \
    || { echo "install failed"; dx "tail -20 /tmp/install.log"; exit 1; }
check "panel installed and running" dx "systemctl is-active --quiet redstonepanel"
dx "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
for _ in $(seq 1 30); do dx "curl -sf $B/api/health" >/dev/null 2>&1 && break; sleep 1; done
dx "curl -s -c /tmp/jar -H 'Content-Type: application/json' -H 'Origin: $B' -d '{\"username\":\"admin\",\"password\":\"$PASS_ADMIN\"}' $B/api/auth/login" >/dev/null
check "upgrade step ran once (marker)" dx "test -f /var/lib/redstonepanel/game-folders-1.21.8"

# --- root-only things the game account must never reach through the panel
dx "printf ORIGINAL > /etc/rp-target && chmod 644 /etc/rp-target"          # readable, not writable
# neither; shaped like a chat line, so the chat window would show it if it read the file
dx "printf '[10:00:00] [Server thread/INFO]: <Steve> TOPSECRET\\n' > /root/rp-secret && chmod 600 /root/rp-secret"
dx "mkdir -p /srv/rp-dir && echo keep > /srv/rp-dir/keep"

# --- a Java server from an imported folder (stand-in jar; it won't start, and needn't)
dx "rm -rf /tmp/imp && mkdir -p /tmp/imp/world && cd /tmp/imp && printf 'not a jar' > server.jar \
    && printf 'server-port=25571\nonline-mode=false\nlevel-name=world\n' > server.properties \
    && printf x > world/level.dat && zip -qr /tmp/imp.zip ."
S=$(dx "curl -s -b /tmp/jar -H 'Origin: $B' -F file=@/tmp/imp.zip $B/api/servers/import" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("server",""))' 2>/dev/null)
[ -n "$S" ] && ok "imported a server ($S)" || { bad "import"; exit 1; }
dx "echo $S > /tmp/S; systemctl stop $S; systemctl disable $S" >/dev/null 2>&1
check "the server folder belongs to minecraft" dx "test \"\$(stat -c %U /opt/$S/server.properties)\" = minecraft"
dx "cp /opt/$S/server.properties /tmp/props.good"
good_props() { as_mc "rm -f server.properties && cp /tmp/props.good server.properties"; }

# --- server.properties as a link
as_mc "ln -sfn /etc/rp-target server.properties"
check "raw settings write through a link refused" test "$(code POST /api/servers/$S/properties/raw '{"content":"HACKED"}')" = 403
check "settings form write through a link refused" test "$(code POST /api/servers/$S/properties '{"properties":{"motd":"HACKED"}}')" != 200
check "  ...the root file is untouched" target_intact
as_mc "ln -sfn /root/rp-secret server.properties"
hidden "raw settings read doesn't show a root-only file" GET /api/servers/$S/properties/raw
good_props

# --- operators (ops.json) and whitelist as links
as_mc "ln -sfn /etc/rp-target ops.json"
check "operators file write through a link refused" test "$(code POST /api/servers/$S/permissions/raw '{"content":"[]"}')" = 403
api POST /api/servers/$S/players/role '{"player":"Steve","role":"operator","player_id":"069a79f4-44e9-4726-a5be-fca90e38aaf5"}' >/dev/null
check "  ...role change didn't write through it either" target_intact
as_mc "rm -f ops.json"
as_mc "ln -sfn /etc/rp-target whitelist.json"
api POST /api/servers/$S/whitelist/add '{"player":"Steve"}' >/dev/null
check "whitelist add didn't write through a link" target_intact
as_mc "rm -f whitelist.json"
check "whitelist add works on a real file" test "$(code POST /api/servers/$S/whitelist/add '{"player":"Alex"}')" = 200
check "  ...and the file it made belongs to minecraft" dx "test \"\$(stat -c %U /opt/$S/whitelist.json)\" = minecraft"
check "role change works on a real file" test "$(code POST /api/servers/$S/players/role '{"player":"Steve","role":"operator","player_id":"069a79f4-44e9-4726-a5be-fca90e38aaf5"}')" = 200
check "  ...ops.json belongs to minecraft" dx "test \"\$(stat -c %U /opt/$S/ops.json)\" = minecraft"

# --- downloads: links are left out, never followed
as_mc "ln -sfn /root/rp-secret leak.txt && ln -sfn /root rootdir"
dx "curl -s -b /tmp/jar -o /tmp/export.zip $B/api/servers/$S/export"
check "server download is a zip with its files" dx "unzip -l /tmp/export.zip | grep -q server.properties"
check "  ...without the linked root-only file" dx "! unzip -l /tmp/export.zip | grep -q -e leak.txt -e rootdir && ! unzip -p /tmp/export.zip | grep -q TOPSECRET"
as_mc "rm -f leak.txt rootdir && mv world world.real && ln -sfn /root world"
check "world download through a link refused" test "$(code GET /api/servers/$S/worlds/export)" = 404
as_mc "rm -f world && mv world.real world"
as_mc "sed -i s/^level-name=.*/level-name=..\\\\/..\\\\/srv\\\\/rp-dir/ server.properties"
check "world download outside the folder refused" test "$(code GET /api/servers/$S/worlds/export)" = 404
check "world rename outside the folder refused" test "$(code POST /api/servers/$S/worlds/rename '{"new_name":"stolen"}')" = 400
check "  ...the outside folder is still there" dx "test -f /srv/rp-dir/keep && test ! -e /opt/$S/stolen"
good_props
check "world rename still works" test "$(code POST /api/servers/$S/worlds/rename '{"new_name":"renamed"}')" = 200
check "  ...folder renamed, level-name follows" dx "test -d /opt/$S/renamed && grep -q '^level-name=renamed' /opt/$S/server.properties"
dx "systemctl stop $S" >/dev/null 2>&1

# --- logs as links: the chat window and player log don't show root-only files
as_mc "mkdir -p logs && ln -sfn /root/rp-secret logs/latest.log"
hidden "chat doesn't show a linked root-only log" GET /api/servers/$S/chat

# --- a server that runs as root (a hand-made unit with no User=): it has root's rights itself, so
#     the panel changes its root-owned folder as root, as before 1.21.8, instead of refusing
dx "mkdir -p /opt/minecraft-rootsrv && printf 'server-port=25572\nmotd=hi\n' > /opt/minecraft-rootsrv/server.properties
chmod 755 /opt/minecraft-rootsrv && chmod 644 /opt/minecraft-rootsrv/server.properties
printf '[Service]\nWorkingDirectory=/opt/minecraft-rootsrv\nExecStart=/bin/sleep infinity\n' > /etc/systemd/system/minecraft-rootsrv.service
systemctl daemon-reload"
check "a root server's settings can be saved" test "$(code POST /api/servers/minecraft-rootsrv/properties/raw '{"content":"motd=saved"}')" = 200
check "  ...and land in its file, still root's" dx "grep -q motd=saved /opt/minecraft-rootsrv/server.properties && test \"\$(stat -c %U /opt/minecraft-rootsrv/server.properties)\" = root"
check "  ...while the minecraft server's link is still refused" test "$(as_mc 'ln -sfn /etc/rp-target server.properties' >/dev/null; code POST /api/servers/$S/properties/raw '{"content":"HACKED"}')" = 403
good_props

check "nothing was ever written to the root file" target_intact
check "panel still running" dx "systemctl is-active --quiet redstonepanel"
echo "=== $FAILS failed"
exit $((FAILS > 0))
