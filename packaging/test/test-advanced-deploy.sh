#!/bin/bash
# End-to-end test of New server's Advanced options (1.17.2) with real downloads: a Paper server
# with every option set (name, port, pinned build, Java, heap, Aikar's flags plus an extra flag,
# seed, world type and name, settings, whitelist and ops), a Fabric server with a pinned loader,
# and a Vanilla snapshot when Mojang has one out. Each must start ("Done (" in its log).
# Needs the internet (Mojang, PaperMC, Fabric meta) and about 3 GB of free memory.
# One throwaway systemd container, installing the .deb from dist/ and, from apt, the Java the
# newest Paper release needs (the panel's own /api/java/requirement says which).
# Run from the repository root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-advanced-deploy.sh      (KEEP=1 leaves the container running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
C=rp-adv
IMG=rp-systemd:24.04
PASS_ADMIN='adv-test-admin-1'          # a throwaway account inside the test container only
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
skip() { echo "SKIP  $1"; }
dx()   { docker exec "$C" bash -c "$1"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
wait_http() { for _ in $(seq 1 60); do dx "curl -sf http://127.0.0.1:8080/api/health" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
login(){ dx "curl -s -c /tmp/jar -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PASS_ADMIN\"}' http://127.0.0.1:8080/api/auth/login" >/dev/null; }
# deploy JSON: the HTTP code, the body in /tmp/body inside the container
deploy() { printf '%s' "$1" | docker exec -i "$C" bash -c "cat > /tmp/req.json; curl -s -o /tmp/body -w '%{http_code}' -b /tmp/jar -H 'Content-Type: application/json' -H 'Origin: http://127.0.0.1:8080' --max-time 900 -d @/tmp/req.json http://127.0.0.1:8080/api/servers/deploy"; }
prop() { dx "sed -n 's/^$2=//p' /opt/$1/server.properties"; }
unit() { dx "sed -n 's/^ExecStart=//p' /etc/systemd/system/$1.service"; }
started() { for _ in $(seq 1 180); do dx "grep -q 'Done (' /opt/$1/logs/latest.log 2>/dev/null" && return 0; sleep 2; done; return 1; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Container kept: $C (docker rm -f $C)"; return; }
    docker rm -f "$C" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== Advanced options test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f "$C" >/dev/null 2>&1
docker run -d --privileged --cgroupns=private --name "$C" -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
sleep 3
dx "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") curl >/tmp/install.log 2>&1" \
    || { echo "install failed"; dx "tail -20 /tmp/install.log"; exit 1; }
check "panel installed" wait_http
dx "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
login
PAPER=$(dx "curl -s https://fill.papermc.io/v3/projects/paper" | $PY -c "import json,sys,re; d=json.load(sys.stdin); v=[x for g in d['versions'].values() for x in g if re.match(r'^[0-9]+(\.[0-9]+)+$', x)]; print(sorted(v, key=lambda s: tuple(map(int, s.split('.'))))[-1])")
JAVA=$(dx "curl -s -b /tmp/jar 'http://127.0.0.1:8080/api/java/requirement?version=$PAPER'" | $PY -c "import json,sys; print(json.load(sys.stdin)['package_major'])")
dx "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends openjdk-$JAVA-jre-headless >>/tmp/install.log 2>&1" \
    && ok "Java $JAVA installed (what Minecraft $PAPER needs)" || { bad "installing Java $JAVA"; exit 1; }

# --- Paper with every option
BUILDS=$(dx "curl -s -b /tmp/jar 'http://127.0.0.1:8080/api/java/builds?loader=paper&version=$PAPER'")
BUILD=$(echo "$BUILDS" | $PY -c "import json,sys; b=[x['build'] for x in json.load(sys.stdin)['builds'] if '(' not in x['label']]; print(b[1] if len(b) > 1 else b[0])")
[ -n "$BUILD" ] && ok "Paper $PAPER builds listed; pinning $BUILD (not the newest)" || bad "Paper builds ($BUILDS)"
c=$(deploy "{\"edition\":\"java\",\"loader\":\"paper\",\"version\":\"$PAPER\",\"advanced\":{\"name\":\"survival\",\"port\":25700,\"build\":\"$BUILD\",\"java\":$JAVA,\"heap\":\"1G\",\"memory_limit\":\"auto\",\"cpu\":1.5,\"flags\":{\"preset\":\"aikar\",\"extra\":[\"-Dfile.encoding=UTF-8\"]},\"seed\":\"8675309\",\"level_type\":\"flat\",\"level_name\":\"main\",\"motd\":\"Advanced test\",\"gamemode\":\"creative\",\"difficulty\":\"hard\",\"max_players\":7,\"online_mode\":true,\"whitelist_players\":\"Notch\",\"ops\":\"Notch\"}}")
[ "$c" = 200 ] && ok "Paper deployed with every option" || bad "Paper deploy ($c $(dx 'cat /tmp/body'))"
S=minecraft-survival
check "  named $S"                          dx "test -f /etc/systemd/system/$S.service"
[ "$(prop $S server-port)" = 25700 ] && ok "  on port 25700" || bad "  port $(prop $S server-port)"
dx "grep -q '\"build\": \"$BUILD\"' /opt/$S/.redstonepanel-server.json" && ok "  build $BUILD installed" || bad "  build: $(dx "cat /opt/$S/.redstonepanel-server.json")"
U=$(unit $S)
echo "$U" | grep -q -- "-Xms1G -Xmx1G -XX:+UseG1GC" && ok "  unit: 1G heap up front with Aikar's flags" || bad "  unit: $U"
echo "$U" | grep -q -- "-Dfile.encoding=UTF-8 -jar server.jar nogui" && ok "  unit: extra flag before -jar" || bad "  unit extra: $U"
echo "$U" | grep -q "java-$JAVA" && ok "  unit: Java $JAVA" || bad "  unit java: $U"
D=$(dx "cat /etc/systemd/system/$S.service.d/redstonepanel-limits.conf 2>/dev/null")
echo "$D" | grep -qx "MemoryMax=1610612736" && echo "$D" | grep -qx "CPUQuota=150%" \
    && ok "  hard limits: Automatic memory (1.5G) and 1.5 cores in the drop-in" || bad "  limits drop-in: $D"
[ "$(dx "systemctl show -p CPUQuotaPerSecUSec --value $S")" = 1.500000s ] && ok "  systemd applies the CPU limit" || bad "  CPUQuota not applied"
for kv in level-seed=8675309 level-type=minecraft:flat level-name=main motd=Advanced\ test gamemode=creative difficulty=hard max-players=7 online-mode=true white-list=true; do
    # Paper rewrites server.properties and escapes the colon (minecraft\:flat): the same value
    [ "$(prop $S "${kv%%=*}" | sed 's/\\:/:/g')" = "${kv#*=}" ] && ok "  $kv" || bad "  ${kv%%=*}=$(prop $S "${kv%%=*}")"
done
dx "grep -q '\"name\": \"Notch\"' /opt/$S/whitelist.json" && ok "  Notch whitelisted (UUID from Mojang)" || bad "  whitelist: $(dx "cat /opt/$S/whitelist.json")"
dx "grep -q '069a79f4-44e9-4726-a5be-fca90e38aaf5' /opt/$S/ops.json" && ok "  Notch is an operator" || bad "  ops: $(dx "cat /opt/$S/ops.json")"
started $S && ok "  started (Done)" || { bad "  didn't start"; dx "tail -20 /opt/$S/logs/latest.log"; }
dx "test -d /opt/$S/main" && ok "  world folder 'main'" || bad "  world folder"
dx "grep -q 'created minecraft-survival with advanced options' /var/log/redstonepanel/panel.log" && ok "  logged" || bad "  log line"
dx "ps -eo args | grep -q '[U]seG1GC.*-jar server.jar'" && ok "  the running Java has the flags" || bad "  running process"
dx "systemctl stop $S"

# --- Fabric with a pinned loader, chosen name only
# Not the newest stable loader, so the pin shows: the next stable one, else the newest unstable one
LOADER=$(dx "curl -s -b /tmp/jar 'http://127.0.0.1:8080/api/java/builds?loader=fabric'" | $PY -c "
import json,sys
b=json.load(sys.stdin)['builds']; stable=[x['build'] for x in b if '(' not in x['label']]; other=[x['build'] for x in b if '(' in x['label']]
print(stable[1] if len(stable) > 1 else other[0])")
[ -n "$LOADER" ] || { bad "no Fabric loader to pin"; exit 1; }
c=$(deploy "{\"edition\":\"java\",\"loader\":\"fabric\",\"advanced\":{\"name\":\"modded\",\"build\":\"$LOADER\"}}")
[ "$c" = 200 ] && ok "Fabric deployed with loader $LOADER" || bad "Fabric deploy ($c $(dx 'cat /tmp/body'))"
dx "grep -q 'loader $LOADER' /opt/minecraft-modded/.redstonepanel-server.json" && ok "  loader $LOADER recorded" || bad "  loader: $(dx 'cat /opt/minecraft-modded/.redstonepanel-server.json')"
[ "$(prop minecraft-modded server-port)" != 25700 ] && ok "  got a free port ($(prop minecraft-modded server-port))" || bad "  port"
started minecraft-modded && ok "  started (Done)" || { bad "  didn't start"; dx "tail -20 /opt/minecraft-modded/logs/latest.log"; }
dx "grep -q 'Loading Minecraft .* with Fabric Loader $LOADER' /opt/minecraft-modded/logs/latest.log" && ok "  runs Fabric Loader $LOADER" || bad "  loader in log"
dx "systemctl stop minecraft-modded"

# --- a Vanilla snapshot, if Mojang has one out now
SNAP=$(dx "curl -s -b /tmp/jar http://127.0.0.1:8080/api/java/snapshots" | $PY -c "import json,sys; v=json.load(sys.stdin)['versions']; print(v[0] if v else '')")
if [ -n "$SNAP" ]; then
    c=$(deploy "{\"edition\":\"java\",\"loader\":\"vanilla\",\"version\":\"$SNAP\",\"advanced\":{\"name\":\"preview\",\"snapshot\":true}}")
    [ "$c" = 200 ] && ok "Vanilla snapshot $SNAP deployed" || bad "snapshot deploy ($c $(dx 'cat /tmp/body'))"
    started minecraft-preview && ok "  started (Done)" || { bad "  didn't start"; dx "tail -20 /opt/minecraft-preview/logs/latest.log"; }
    REL=$(dx "curl -s 'http://127.0.0.1:8080/api/java/versions?loader=vanilla' -b /tmp/jar" | $PY -c "import json,sys; print(json.load(sys.stdin)['versions'][0])")
    c=$(dx "curl -s -o /tmp/body -w '%{http_code}' -b /tmp/jar -H 'Content-Type: application/json' -H 'Origin: http://127.0.0.1:8080' -d '{\"version\":\"$REL\"}' http://127.0.0.1:8080/api/servers/minecraft-preview/update")
    [ "$c" != 200 ] && dx "grep -q \"can't be moved\" /tmp/body" && ok "  updating it back to $REL refused" || bad "  update to $REL ($c $(dx 'cat /tmp/body'))"
    dx "systemctl stop minecraft-preview"
else
    skip "no Vanilla snapshot is out right now"
fi

echo "=== $([ $FAILS = 0 ] && echo 'ALL PASSED' || echo "$FAILS FAILED")"
exit $FAILS
