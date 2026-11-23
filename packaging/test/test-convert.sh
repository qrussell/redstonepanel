#!/bin/bash
# End-to-end test of changing a server's type (1.19) with real server software: one world goes
# Vanilla -> Paper -> Vanilla -> Fabric -> Paper, an undo, a Fabric that can't start (which must
# put Vanilla back by itself), and an update that is rolled back. A block is placed in the
# Overworld, the Nether and the End first, and has to be there after every step, with the
# same seed and player data.
# Needs the internet (Mojang, PaperMC, Fabric meta, Modrinth) and about 3 GB of free memory.
# One throwaway systemd container, installing the .deb from dist/ and the Java the versions need.
# Run from the repository root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-convert.sh      (KEEP=1 leaves the container running)
# MC: the Minecraft version of the main world (default 1.21.11, the last one where Paper kept
# the Nether and the End in their own folders; MC=26.1.2 tests the layout every type shares).
# RP_TEST_HOOK: a script run with the container's name once it is up (a proxy for its downloads).
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
C=rp-convert
IMG=rp-systemd:24.04
MC=${MC:-1.21.11}
OLD=${OLD:-1.21.10}                    # the update that gets rolled back starts here, and goes to $MC
PASS_ADMIN='convert-test-admin-1'      # a throwaway account inside the test container only
S=minecraft-switch
API=http://127.0.0.1:8080
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$C" bash -c "$1"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
wait_http() { for _ in $(seq 1 60); do dx "curl -sf $API/api/health" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
login(){ dx "curl -s -c /tmp/jar -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PASS_ADMIN\"}' $API/api/auth/login" >/dev/null; }
get()  { dx "curl -s -b /tmp/jar '$API$1'"; }
# post PATH JSON: the HTTP code, the body in /tmp/body inside the container
post() { printf '%s' "${2:-{\}}" | docker exec -i "$C" bash -c "cat > /tmp/req.json; curl -s -o /tmp/body -w '%{http_code}' -b /tmp/jar -H 'Content-Type: application/json' -H 'Origin: $API' --max-time 900 -d @/tmp/req.json '$API$1'"; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
js()   { $PY -c "import json,sys; d=json.load(sys.stdin); print(($1))" 2>/dev/null; }
started() { for _ in $(seq 1 180); do dx "grep -q 'Done (' /opt/$1/logs/latest.log 2>/dev/null" && return 0; sleep 2; done; return 1; }
# rc SERVER COMMAND: a console command over the server's own RCON, through the panel's code
rc()   { dx "cd /usr/share/redstonepanel && python3 - '$1' <<'EOF'
import sys
from core import manager, rcon
p = manager.read_properties('/opt/' + sys.argv[1])
print(rcon.command('127.0.0.1', int(p['rcon.port']), p['rcon.password'], '''$2''', timeout=60))
EOF"; }
# rcon_up SERVER: the first Live Chat message sets RCON up and restarts the server
rcon_up() {
    post "/api/servers/$1/chat" '{"message":"hello"}' >/dev/null
    for _ in $(seq 1 90); do rc "$1" "list" 2>/dev/null | grep -q "players" && return 0; sleep 2; done
    return 1
}
# the three marker blocks: Overworld diamond, Nether gold, End emerald
mark() {
    rc "$1" "forceload add 0 0" >/dev/null; rc "$1" "setblock 0 200 0 minecraft:diamond_block" >/dev/null
    rc "$1" "execute in minecraft:the_nether run forceload add 0 0" >/dev/null
    rc "$1" "execute in minecraft:the_nether run setblock 0 100 0 minecraft:gold_block" >/dev/null
    rc "$1" "execute in minecraft:the_end run forceload add 0 0" >/dev/null
    rc "$1" "execute in minecraft:the_end run setblock 0 100 0 minecraft:emerald_block" >/dev/null
    rc "$1" "save-all flush" >/dev/null
}
marks() {
    local n=0
    rc "$1" "forceload add 0 0" >/dev/null
    rc "$1" "execute in minecraft:the_nether run forceload add 0 0" >/dev/null
    rc "$1" "execute in minecraft:the_end run forceload add 0 0" >/dev/null
    rc "$1" "execute if block 0 200 0 minecraft:diamond_block" | grep -q "Test passed" && n=$((n + 1))
    rc "$1" "execute in minecraft:the_nether if block 0 100 0 minecraft:gold_block" | grep -q "Test passed" && n=$((n + 1))
    rc "$1" "execute in minecraft:the_end if block 0 100 0 minecraft:emerald_block" | grep -q "Test passed" && n=$((n + 1))
    echo $n
}
world_ok() {       # NAME LABEL: the blocks in all three dimensions, the seed and the player file
    local n; n=$(marks "$1")
    [ "$n" = 3 ] && ok "  $2: the blocks in the Overworld, Nether and End are there" || bad "  $2: only $n of 3 marker blocks"
    rc "$1" "seed" | grep -q "8675309" && ok "  $2: same seed" || bad "  $2: seed $(rc "$1" seed)"
    [ "$(dx "sha256sum < /opt/$1/world/playerdata/$PLAYER.dat")" = "$PLAYER_SUM" ] && ok "  $2: player data untouched" || bad "  $2: player data changed"
}
info() { dx "cat /opt/$1/.redstonepanel-server.json"; }
# wait_job SERVER: waits for the running change to end; prints its JSON
wait_job() {
    local j
    for _ in $(seq 1 400); do
        j=$(get "/api/servers/$1/convert")
        [ "$(echo "$j" | js "d['job']['state']")" != running ] && { echo "$j"; return 0; }
        sleep 3
    done
    echo "$j"; return 1
}
# change SERVER TO [EXTRA JSON FIELDS]: previews, starts, waits; the final status in $JOB
change() {
    local p c
    p=$(get "/api/servers/$1/convert?to=$2")
    [ "$(echo "$p" | js "d['ok']")" = True ] || { bad "preview of $2 not ok: $(echo "$p" | js "d['blockers']")"; }
    PLAN=$p
    c=$(post "/api/servers/$1/convert" "{\"to\":\"$2\"${3:+,$3}}")
    [ "$c" = 200 ] || { bad "starting the change to $2 ($c $(dx 'cat /tmp/body'))"; JOB='{}'; return 1; }
    JOB=$(wait_job "$1")
}
job()  { echo "$JOB" | js "d['job']$1"; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Container kept: $C (docker rm -f $C)"; return; }
    docker rm -f "$C" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== Type change test with $(basename "$DEB"), Minecraft $MC"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f "$C" >/dev/null 2>&1
docker run -d --privileged --cgroupns=private --name "$C" -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
sleep 3
[ -n "${RP_TEST_HOOK:-}" ] && bash "$RP_TEST_HOOK" "$C"
dx "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") curl >/tmp/install.log 2>&1" \
    || { echo "install failed"; dx "tail -20 /tmp/install.log"; exit 1; }
check "panel installed" wait_http
dx "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
login
JAVA=$(get "/api/java/requirement?version=$MC" | js "d['package_major']")
dx "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends openjdk-$JAVA-jre-headless >>/tmp/install.log 2>&1" \
    && ok "Java $JAVA installed (what Minecraft $MC needs)" || { bad "installing Java $JAVA"; exit 1; }
SPLIT=$($PY -c "print(tuple(map(int, '$MC'.split('.'))) < (26, 1))")      # True: Paper keeps its own Nether and End folders

# --- a Vanilla world with a block in each dimension
c=$(post /api/servers/deploy "{\"edition\":\"java\",\"loader\":\"vanilla\",\"version\":\"$MC\",\"advanced\":{\"name\":\"switch\",\"port\":25710,\"heap\":\"1G\",\"seed\":\"8675309\",\"online_mode\":false}}")
[ "$c" = 200 ] && ok "Vanilla $MC deployed" || { bad "deploy ($c $(dx 'cat /tmp/body'))"; exit 1; }
started $S && ok "  started (Done)" || { bad "  didn't start"; dx "tail -20 /opt/$S/logs/latest.log"; exit 1; }
rcon_up $S
rc $S "list" | grep -q "players" && ok "  RCON answers" || { bad "  RCON: $(rc $S list 2>&1 | tail -1)"; exit 1; }
mark $S
PLAYER=069a79f4-44e9-4726-a5be-fca90e38aaf5
dx "mkdir -p /opt/$S/world/playerdata && head -c 2048 /dev/urandom > /opt/$S/world/playerdata/$PLAYER.dat && chown -R minecraft:minecraft /opt/$S/world/playerdata"
PLAYER_SUM=$(dx "sha256sum < /opt/$S/world/playerdata/$PLAYER.dat")
world_ok $S "Vanilla"

# --- the preview changes nothing, and says what it can't do
BEFORE=$(dx "ls -A /opt/$S | sort | sha256sum; cat /opt/$S/.redstonepanel-server.json")
P=$(get "/api/servers/$S/convert?to=paper")
[ "$(echo "$P" | js "d['ok'], d['to']['name'], d['to']['minecraft'], d['addons']['action'], d['confirm_name']")" = "(True, 'Paper', '$MC', 'none', False)" ] \
    && ok "Preview of Vanilla -> Paper $MC (build $(echo "$P" | js "d['to']['build']"))" || bad "preview: $P"
[ "$(dx "ls -A /opt/$S | sort | sha256sum; cat /opt/$S/.redstonepanel-server.json")" = "$BEFORE" ] && ok "  the preview changed nothing" || bad "  the preview changed the folder"
get "/api/servers/$S/convert?to=vanilla" | grep -q "Update Server" && ok "  same type: use Update Server" || bad "  same type not refused"
c=$(post "/api/servers/$S/convert" '{"to":"paper","minecraft":"1.20.1"}')
[ "$c" = 400 ] && dx "grep -q \"can't go back\" /tmp/body" && ok "  an older Minecraft version is refused" || bad "  older version ($c $(dx 'cat /tmp/body'))"

# --- Vanilla -> Paper
change $S paper
[ "$(job "['state']")" = done ] && ok "Vanilla -> Paper: done" || bad "Vanilla -> Paper: $(job "['state'], d['job']['error']")"
info $S | grep -q '"loader": "paper"' && ok "  runs Paper ($(info $S))" || bad "  info: $(info $S)"
dx "grep -q 'Paper\|paper' /opt/$S/logs/latest.log" && dx "systemctl is-active -q $S" && ok "  Paper is running" || bad "  Paper not running"
if [ "$SPLIT" = True ]; then
    dx "test -d /opt/$S/world_nether/DIM-1 && test -d /opt/$S/world_the_end/DIM1 && ! test -e /opt/$S/world/DIM-1" \
        && ok "  Paper moved the Nether and End into its own folders" || bad "  layout: $(dx "ls /opt/$S /opt/$S/world")"
else
    dx "! test -e /opt/$S/world_nether" && ok "  no separate Nether folder on $MC" || bad "  world_nether exists on $MC"
fi
world_ok $S "Paper"
B=$(get "/api/servers/$S/backups")
[ "$(echo "$B" | js "[(b['kind'], b['made_by']['label']) for b in d['backups']]")" = "[('pre-convert', 'Vanilla $MC')]" ] \
    && ok "  a \"Before type change\" backup, made by Vanilla $MC" || bad "  backups: $(echo "$B" | js "d['backups']")"
dx "test -f /opt/$S/server.jar.previous && grep -q vanilla /opt/$S/.redstonepanel-server.previous.json" && ok "  the old server.jar is kept" || bad "  server.jar.previous missing"
dx "grep -q 'admin changed $S from Vanilla $MC to Paper $MC (build' /var/log/redstonepanel/panel.log" && ok "  logged with who did it" || bad "  log line"
[ "$(get "/api/servers/$S/convert" | js "d['undo']['kind'], d['undo']['available'], d['current']['name']")" = "('convert', True, 'Paper $MC')" ] && ok "  Undo type change is offered" || bad "  undo: $(get "/api/servers/$S/convert")"
[ "$(dx "sed -n 's/^server-port=//p' /opt/$S/server.properties")" = 25710 ] && ok "  same port" || bad "  port changed"

# --- Paper -> Vanilla: the panel moves the dimensions back, plugins are set aside
mark $S
change $S vanilla
if [ "$SPLIT" = True ]; then
    [ "$(echo "$PLAN" | js "[m['to'] for m in d['world']['moves']]")" = "['world/DIM-1', 'world/DIM1']" ] && ok "Preview of Paper -> Vanilla lists both world moves" || bad "moves: $(echo "$PLAN" | js "d['world']")"
else
    echo "$PLAN" | js "[m['to'] for m in d['world']['moves']]" | grep -q "'world/data/minecraft/world_gen_settings.dat'" && ok "Preview of Paper -> Vanilla lists the world settings files Paper keeps elsewhere" || bad "moves: $(echo "$PLAN" | js "d['world']")"
fi
[ "$(job "['state']")" = done ] && ok "Paper -> Vanilla: done" || bad "Paper -> Vanilla: $(job "['state'], d['job']['error']")"
info $S | grep -q '"loader": "vanilla"' && ok "  runs Vanilla" || bad "  info: $(info $S)"
[ "$SPLIT" = True ] && { dx "test -d /opt/$S/world/DIM-1 && test -d /opt/$S/world/DIM1 && ! test -e /opt/$S/world_nether/DIM-1" \
    && ok "  the Nether and End are inside the world folder again" || bad "  layout: $(dx "ls /opt/$S /opt/$S/world")"; }
dx "test -d /opt/$S/plugins.off && ! test -e /opt/$S/plugins" && ok "  plugins is now plugins.off" || bad "  plugins folder: $(dx "ls -d /opt/$S/plugins*")"
world_ok $S "Vanilla again"

# --- Vanilla -> Fabric, with a mod from Modrinth
change $S fabric
[ "$(job "['state']")" = done ] && ok "Vanilla -> Fabric: done" || bad "Vanilla -> Fabric: $(job "['state'], d['job']['error']")"
dx "grep -q 'with Fabric Loader' /opt/$S/logs/latest.log" && ok "  runs Fabric" || bad "  Fabric not in the log"
world_ok $S "Fabric"
c=$(post "/api/servers/$S/addons/install" '{"source":"modrinth","project":"luckperms"}')
[ "$c" = 200 ] && dx "ls /opt/$S/mods | grep -qi luckperms" && ok "  LuckPerms (Fabric) installed from Modrinth" || bad "  installing LuckPerms ($c $(dx 'cat /tmp/body'))"
c=$(post "/api/servers/$S/addons/install" '{"source":"modrinth","project":"fabric-api"}')      # LuckPerms needs it
post "/api/servers/$S/action" '{"action":"restart"}' >/dev/null
for _ in $(seq 1 90); do rc $S "list" 2>/dev/null | grep -q players && dx "grep -qi 'luckperms' /opt/$S/logs/latest.log" && break; sleep 2; done
dx "grep -q 'Done (' /opt/$S/logs/latest.log" && dx "grep -qi 'luckperms' /opt/$S/logs/latest.log" && ok "  Fabric runs with LuckPerms and Fabric API" || bad "  Fabric with LuckPerms didn't start ($c)"

# --- Fabric -> Paper: mods set aside, the Paper build of the same project offered and installed,
#     plugins.off comes back, and the panel moves the dimensions into Paper's leftover folders
P=$(get "/api/servers/$S/convert?to=paper")
LP=$(echo "$P" | js "[i['equivalent']['project'] for i in d['addons']['items'] if i['equivalent'] and 'uckperms' in i['file'].lower()][0]")
[ -n "$LP" ] && ok "Preview of Fabric -> Paper offers LuckPerms' Paper build" || bad "no equivalent offered: $(echo "$P" | js "d['addons']")"
[ "$(echo "$P" | js "d['addons']['action'], d['addons']['restore']")" = "('off', 'plugins')" ] && ok "  mods are set aside, plugins.off comes back" || bad "  addons: $(echo "$P" | js "d['addons']")"
change $S paper "\"install\":[\"$LP\"]"
[ "$(job "['state']")" = done ] && ok "Fabric -> Paper: done" || bad "Fabric -> Paper: $(job "['state'], d['job']['error']")"
dx "test -d /opt/$S/mods.off && ! test -e /opt/$S/mods && test -d /opt/$S/plugins && ! test -e /opt/$S/plugins.off" && ok "  mods.off and plugins" || bad "  folders: $(dx "ls -d /opt/$S/mods* /opt/$S/plugins*")"
dx "ls /opt/$S/plugins | grep -qi 'luckperms.*jar'" && dx "grep -qi 'LuckPerms' /opt/$S/logs/latest.log" && ok "  LuckPerms' Paper build installed and loaded" || bad "  LuckPerms on Paper: $(job "['notes']")"
[ "$SPLIT" = True ] && { dx "test -d /opt/$S/world_nether/DIM-1 && ! test -e /opt/$S/world/DIM-1" && ok "  dimensions back in Paper's folders" || bad "  layout: $(dx "ls /opt/$S /opt/$S/world")"; }
world_ok $S "Paper again"

# --- Undo type change: Fabric, its mods and the world from before
mark $S
rc $S "setblock 0 201 0 minecraft:netherite_block" >/dev/null; rc $S "save-all flush" >/dev/null
c=$(post "/api/servers/$S/update/rollback"); [ "$c" = 404 ] && ok "Roll back the last update isn't offered for a type change" || bad "rollback of a type change ($c)"
c=$(post "/api/servers/$S/convert/undo")
[ "$c" = 200 ] && ok "Undo type change started" || bad "undo ($c $(dx 'cat /tmp/body'))"
JOB=$(wait_job $S)
[ "$(job "['state'], d['job']['action']")" = "('done', 'undo')" ] && ok "  done" || bad "  undo: $(job "['state'], d['job']['error']")"
info $S | grep -q '"loader": "fabric"' && dx "grep -q 'with Fabric Loader' /opt/$S/logs/latest.log" && ok "  runs Fabric again" || bad "  info: $(info $S)"
dx "ls /opt/$S/mods | grep -qi luckperms && ! test -e /opt/$S/mods.off && test -d /opt/$S/plugins.off" && ok "  its mods are back" || bad "  folders: $(dx "ls -d /opt/$S/mods* /opt/$S/plugins*")"
[ "$SPLIT" = True ] && { dx "test -d /opt/$S/world/DIM-1 && ! test -e /opt/$S/world_nether/DIM-1" && ok "  the world from before, in Fabric's layout" || bad "  layout: $(dx "ls /opt/$S /opt/$S/world")"; }
world_ok $S "Fabric after undo"
rc $S "execute if block 0 201 0 minecraft:netherite_block" | grep -q "Test passed" && bad "  a block placed after the change is still there" || ok "  what was built on Paper is not in the world (it is in a \"Before restore\" backup)"
get "/api/servers/$S/backups" | js "[b['kind'] for b in d['backups']]" | grep -q "pre-restore" && ok "  \"Before restore\" backup taken" || bad "  no pre-restore backup"
[ "$(get "/api/servers/$S/convert" | js "d['undo']")" = None ] && ok "  nothing more to undo" || bad "  undo still offered"

# --- a type that can't start is put back by itself: Fabric -> Vanilla works, then Vanilla ->
#     Fabric with a mod that needs something missing
change $S vanilla
[ "$(job "['state']")" = done ] && ok "Fabric -> Vanilla: done" || bad "Fabric -> Vanilla: $(job "['state'], d['job']['error']")"
dx "cd /opt/$S/mods.off && python3 - <<'EOF'
import zipfile
with zipfile.ZipFile('needy.jar', 'w') as z:
    z.writestr('fabric.mod.json', '{\"schemaVersion\": 1, \"id\": \"needy\", \"version\": \"1.0.0\", \"name\": \"Needy\", \"depends\": {\"not-a-real-mod\": \"*\"}}')
EOF
chown minecraft:minecraft /opt/$S/mods.off/needy.jar"
mark $S
change $S fabric
[ "$(job "['state'], d['job']['rolled_back']")" = "('failed', True)" ] && ok "Vanilla -> Fabric with a broken mod: failed and put back" || bad "broken Fabric: $(echo "$JOB" | js "d['job']")"
job "['error']" | grep -q "back on Vanilla $MC" && ok "  says it is back on Vanilla $MC" || bad "  error: $(job "['error']")"
job "['why']" | grep -qi "not-a-real-mod" && ok "  shows the log line that explains why" || bad "  why: $(job "['why']")"
info $S | grep -q '"loader": "vanilla"' && dx "systemctl is-active -q $S" && ok "  Vanilla is running again" || bad "  info: $(info $S), $(dx "systemctl is-active $S")"
dx "test -f /opt/$S/mods.off/needy.jar && ! test -e /opt/$S/mods" && ok "  mods.off is back where it was" || bad "  folders: $(dx "ls -d /opt/$S/mods*")"
for _ in $(seq 1 30); do rc $S "list" 2>/dev/null | grep -q players && break; sleep 2; done
world_ok $S "Vanilla after the failed change"
[ "$(get "/api/servers/$S/convert" | js "d['job']['state'], d['undo']")" = "('failed', None)" ] && ok "  the failure stays on the Maintenance tab" || bad "  status: $(get "/api/servers/$S/convert")"

# --- content mods: the name has to be typed
dx "cd /opt/$S/mods.off && rm needy.jar && python3 - <<'EOF'
import zipfile
with zipfile.ZipFile('farm.jar', 'w') as z:
    z.writestr('fabric.mod.json', '{\"schemaVersion\": 1, \"id\": \"farm\", \"version\": \"1.0.0\", \"name\": \"Farm Life\"}')
    z.writestr('assets/farm/blockstates/crop.json', '{\"variants\": {}}')
EOF
chown minecraft:minecraft farm.jar"
change $S fabric
[ "$(job "['state']")" = done ] && ok "Vanilla -> Fabric (with a mod that adds blocks): done" || bad "Vanilla -> Fabric: $(job "['state'], d['job']['error']")"
P=$(get "/api/servers/$S/convert?to=vanilla")
[ "$(echo "$P" | js "d['confirm_name'], [m['file'] for m in d['content']['mods']]")" = "(True, ['farm.jar'])" ] && ok "Preview of Fabric -> Vanilla names the mod that adds blocks" || bad "content: $(echo "$P" | js "d['content']")"
c=$(post "/api/servers/$S/convert" '{"to":"vanilla"}')
[ "$c" = 400 ] && dx "grep -q 'Type the server' /tmp/body" && ok "  refused without the server's name" || bad "  not refused ($c)"
dx "systemctl stop $S"

# --- an update, rolled back
c=$(post /api/servers/deploy "{\"edition\":\"java\",\"loader\":\"vanilla\",\"version\":\"$OLD\",\"advanced\":{\"name\":\"older\",\"port\":25720,\"heap\":\"1G\",\"online_mode\":false}}")
if [ "$c" = 200 ]; then
    U=minecraft-older
    started $U && ok "Vanilla $OLD deployed and started" || bad "Vanilla $OLD didn't start"
    rcon_up $U
    rc $U "forceload add 0 0" >/dev/null; rc $U "setblock 0 200 0 minecraft:diamond_block" >/dev/null; rc $U "save-all flush" >/dev/null
    c=$(post "/api/servers/$U/update" "{\"version\":\"$MC\"}")
    [ "$c" = 200 ] && info $U | grep -q "\"minecraft\": \"$MC\"" && ok "  updated to $MC" || bad "  update ($c $(dx 'cat /tmp/body'))"
    [ "$(get "/api/servers/$U/backups" | js "[(b['kind'], b['made_by']['label']) for b in d['backups']]")" = "[('pre-update', 'Vanilla $OLD')]" ] && ok "  a \"Before update\" backup, made by Vanilla $OLD" || bad "  backups: $(get "/api/servers/$U/backups")"
    started $U; sleep 5
    [ "$(get "/api/servers/$U/convert" | js "d['undo']['kind'], d['undo']['available']")" = "('update', True)" ] && ok "  Roll back the last update is offered" || bad "  undo: $(get "/api/servers/$U/convert")"
    c=$(post "/api/servers/$U/convert/undo"); [ "$c" = 404 ] && ok "  (not as Undo type change)" || bad "  undo of an update ($c)"
    c=$(post "/api/servers/$U/update/rollback")
    [ "$c" = 200 ] && ok "  roll back started" || bad "  rollback ($c $(dx 'cat /tmp/body'))"
    JOB=$(wait_job $U)
    [ "$(job "['state'], d['job']['action']")" = "('done', 'rollback')" ] && ok "  done" || bad "  rollback: $(job "['state'], d['job']['error']")"
    info $U | grep -q "\"minecraft\": \"$OLD\"" && dx "systemctl is-active -q $U" && ok "  runs Vanilla $OLD again" || bad "  info: $(info $U)"
    for _ in $(seq 1 30); do rc $U "list" 2>/dev/null | grep -q players && break; sleep 2; done
    rc $U "forceload add 0 0" >/dev/null
    rc $U "execute if block 0 200 0 minecraft:diamond_block" | grep -q "Test passed" && ok "  the world from before the update is back" || bad "  marker block missing"
    dx "grep -qi 'error.*world\|newer version\|downgrad' /opt/$U/logs/latest.log" && bad "  errors in the log after the roll back" || ok "  no errors about the world in the log"
    NEWER=$(get "/api/servers/$U/backups" | js "[b['file'] for b in d['backups'] if b['kind'] == 'pre-restore'][0]")
    c=$(post "/api/servers/$U/backups/$NEWER/restore")
    [ "$c" = 400 ] && dx "grep -q \"can't open a newer world\" /tmp/body" && ok "  restoring the $MC world onto $OLD is refused" || bad "  newer-world restore ($c $(dx 'cat /tmp/body'))"
else
    bad "deploying Vanilla $OLD ($c $(dx 'cat /tmp/body'))"
fi

echo "=== $([ $FAILS = 0 ] && echo 'ALL PASSED' || echo "$FAILS FAILED")"
exit $FAILS
