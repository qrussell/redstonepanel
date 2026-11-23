#!/bin/bash
# End-to-end test of the 1.20 server types and modpacks, with real downloads:
#   * Purpur, Quilt, NeoForge and Forge servers (Forge both ways: a 1.20.1 one started from the
#     installer's arguments file, and a 1.12.2 one started from Forge's own jar on Java 8),
#     each of which must start ("Done (" in its log);
#   * a mod dropped on each: one for that loader is accepted, one for another loader refused;
#   * Update on a NeoForge server pinned to an older build (the installer runs again);
#   * a server from a real Modrinth modpack (Adrenaserver, a small server-side Fabric pack), from
#     an older version of it, then the update check and the update to the newest version;
#   * a server from an uploaded .mrpack and from an uploaded server pack (built here around a
#     real Forge mod, with a start script that must never run), and the refusals.
# Needs the internet (Mojang, PurpurMC, QuiltMC, MinecraftForge, NeoForged, Modrinth) and about
# 6 GB of free memory; takes 20 to 40 minutes. Servers are stopped again once they've started.
# One throwaway systemd container, installing the .deb from dist/ and Java 8, 17 and 21 from apt.
# Run from the repository root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-loaders-modpacks.sh     (KEEP=1 leaves the container running;
#       ONLY="purpur quilt neoforge forge oldforge mods update modpack upload switch" runs some parts;
#       RP_TEST_SETUP=script runs "script <container>" once it's up, e.g. to set a proxy)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
C=rp-loaders
IMG=rp-systemd:24.04
PASS_ADMIN='loaders-test-admin-1'      # a throwaway account inside the test container only
ONLY=${ONLY:-purpur quilt neoforge forge oldforge mods update modpack upload switch}
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
skip() { echo "SKIP  $1"; }
want() { case " $ONLY " in *" $1 "*) return 0;; esac; return 1; }
dx()   { docker exec "$C" bash -c "$1"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
wait_http() { for _ in $(seq 1 60); do dx "curl -sf http://127.0.0.1:8080/api/health" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
login(){ dx "curl -s -c /tmp/jar -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PASS_ADMIN\"}' http://127.0.0.1:8080/api/auth/login" >/dev/null; }
CURL="curl -s -b /tmp/jar -H 'Origin: http://127.0.0.1:8080' --noproxy 127.0.0.1"
api()  { dx "$CURL 'http://127.0.0.1:8080$1'"; }
# deploy JSON: the HTTP code, the body in /tmp/body inside the container
deploy() { printf '%s' "$1" | docker exec -i "$C" bash -c "cat > /tmp/req.json; $CURL -o /tmp/body -w '%{http_code}' -H 'Content-Type: application/json' --max-time 1800 -d @/tmp/req.json http://127.0.0.1:8080/api/servers/deploy"; }
# post PATH FORM...: a multipart POST; the HTTP code, the body in /tmp/body
post() { local path=$1; shift; local f=""; for a in "$@"; do f="$f -F '$a'"; done
         dx "$CURL -o /tmp/body -w '%{http_code}' --max-time 2400 -X POST $f 'http://127.0.0.1:8080$path'"; }
body() { dx "cat /tmp/body"; }
unit() { dx "sed -n 's/^ExecStart=//p' /etc/systemd/system/$1.service"; }
info() { dx "cat /opt/$1/.redstonepanel-server.json"; }
started() { for _ in $(seq 1 ${2:-300}); do dx "grep -q 'Done (' /opt/$1/logs/latest.log 2>/dev/null" && return 0; sleep 2; done; return 1; }
stop() { dx "systemctl stop $1.service" >/dev/null 2>&1; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
jget() { $PY -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {'d': d}))" "$1" 2>/dev/null; }
# started_ok NAME LABEL [tries]: must reach "Done", then is stopped to free its memory
started_ok() {
    if started "$1" "${3:-300}"; then ok "  $2 started (Done)"; else bad "  $2 didn't start"; dx "tail -25 /opt/$1/logs/latest.log 2>/dev/null; journalctl -u $1 -n 15 --no-pager -o cat"; fi
}
# a Modrinth project's newest file for a loader and Minecraft version, saved as /tmp/NAME in the container
modrinth_file() {
    local url
    url=$(dx "curl -s 'https://api.modrinth.com/v2/project/$1/version?loaders=%5B%22$2%22%5D&game_versions=%5B%22$3%22%5D'" \
          | jget "[f['url'] for v in d for f in v['files'] if f.get('primary') or len(v['files']) == 1][0]")
    [ -n "$url" ] && dx "curl -s -L -o /tmp/$4 '$url'" && dx "test -s /tmp/$4"
}
drop() { dx "$CURL -o /tmp/body -w '%{http_code}' -F 'file=@/tmp/$2' 'http://127.0.0.1:8080/api/servers/$1/addons'"; }

# postj PATH JSON: a JSON POST; the HTTP code, the body in /tmp/body
postj() { dx "$CURL -o /tmp/body -w '%{http_code}' --max-time 1800 -X POST -H 'Content-Type: application/json' -d '${2:-{\}}' 'http://127.0.0.1:8080$1'"; }
# wait_job SERVER: waits for a type change, undo or roll back (1.19) to end; prints its state
wait_job() {
    local st=running
    for _ in $(seq 1 500); do
        st=$(api "/api/servers/$1/convert" | jget "d['job']['state'] if d.get('job') else 'none'")
        [ "$st" != running ] && break
        sleep 3
    done
    echo "$st"
}
# change SERVER TO LABEL: previews, changes, waits
change() {
    local p c st
    p=$(api "/api/servers/$1/convert?to=$2")
    [ "$(echo "$p" | jget "d['ok']")" = True ] || bad "  preview of $3 not ok: $(echo "$p" | jget "d['blockers']") $p"
    PLAN=$p
    c=$(postj "/api/servers/$1/convert" "{\"to\":\"$2\"}")
    [ "$c" = 200 ] || { bad "starting the change to $3 ($c $(body))"; return 1; }
    st=$(wait_job "$1")
    [ "$st" = done ] && ok "changed $1 to $3; it started and was checked" || bad "change to $3: $st $(api "/api/servers/$1/convert" | jget "d['job'].get('error')")"
}
undo() {       # SERVER LABEL
    local c st
    c=$(postj "/api/servers/$1/convert/undo"); [ "$c" = 200 ] || { bad "undo ($c $(body))"; return 1; }
    st=$(wait_job "$1")
    [ "$st" = done ] && ok "undone: $1 is $2 again and started" || bad "undo to $2: $st $(api "/api/servers/$1/convert" | jget "d['job'].get('error')")"
}

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Container kept: $C (docker rm -f $C)"; return; }
    docker rm -f "$C" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== Server types and modpacks test with $(basename "$DEB") (parts: $ONLY)"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f "$C" >/dev/null 2>&1
docker run -d --privileged --cgroupns=private --name "$C" -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
sleep 3
[ -n "${RP_TEST_SETUP:-}" ] && bash "$RP_TEST_SETUP" "$C"
dx "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") curl zip >/tmp/install.log 2>&1" \
    || { echo "install failed"; dx "tail -20 /tmp/install.log"; exit 1; }
check "panel installed" wait_http
dx "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
login
JAVAS="21"; want forge && JAVAS="$JAVAS 17"; want upload && JAVAS="$JAVAS 17"; want oldforge && JAVAS="$JAVAS 8"
for j in $(echo "$JAVAS" | tr ' ' '\n' | sort -un); do
    dx "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends openjdk-$j-jre-headless >>/tmp/install.log 2>&1" \
        && ok "Java $j installed" || { bad "installing Java $j"; dx "tail -5 /tmp/install.log"; }
done

# --- The lists the dialog shows
for l in purpur quilt forge neoforge; do
    n=$(api "/api/java/versions?loader=$l" | jget "len(d['versions'])")
    [ "${n:-0}" -gt 5 ] && ok "$l: $n Minecraft versions listed" || bad "$l versions ($n)"
done

# --- Purpur: a server.jar like Paper
if want purpur; then
    c=$(deploy '{"edition":"java","loader":"purpur","version":"1.21.4","advanced":{"name":"purpur","port":25710,"heap":"1G"}}')
    [ "$c" = 200 ] && ok "Purpur 1.21.4 deployed" || bad "Purpur deploy ($c $(body))"
    S=minecraft-purpur
    info $S | grep -q '"loader": "purpur"' && ok "  recorded as Purpur, build $(info $S | jget "d['build']")" || bad "  info: $(info $S)"
    unit $S | grep -q -- "-jar server.jar nogui" && ok "  unit starts server.jar" || bad "  unit: $(unit $S)"
    started_ok $S Purpur
    dx "grep -qi 'purpur' /opt/$S/logs/latest.log" && ok "  the log says Purpur" || bad "  no Purpur in the log"
    [ "$(api "/api/servers/$S/addons" | jget "d['folder']")" = plugins ] && ok "  takes plugins" || bad "  add-ons folder"
    n=$(api "/api/servers/$S/addons/catalog" | jget "sum(len(g['items']) for g in d['groups'])")
    [ "${n:-0}" -gt 5 ] && ok "  catalog lists $n plugins (Modrinth and Hangar)" || bad "  catalog ($n)"
    stop $S
fi

# --- Quilt: its installer, then quilt-server-launch.jar
if want quilt; then
    c=$(deploy '{"edition":"java","loader":"quilt","version":"1.21.4","advanced":{"name":"quilt","port":25711,"heap":"1G"}}')
    [ "$c" = 200 ] && ok "Quilt 1.21.4 deployed" || bad "Quilt deploy ($c $(body))"
    S=minecraft-quilt
    unit $S | grep -q -- "-Xmx1G -jar quilt-server-launch.jar nogui" && ok "  unit starts quilt-server-launch.jar" || bad "  unit: $(unit $S)"
    check "  the vanilla server.jar and libraries are there" dx "test -s /opt/$S/server.jar && test -d /opt/$S/libraries/org/quiltmc"
    check "  the installer and its leftovers are gone"       dx "! ls -A /opt/$S | grep -E 'installer|^run\.|user_jvm_args'"
    check "  everything belongs to minecraft"                 dx "test -z \"\$(find /opt/$S ! -user minecraft | head -1)\""
    started_ok $S Quilt
    dx "grep -qi 'quilt' /opt/$S/logs/latest.log" && ok "  the log says Quilt" || bad "  no Quilt in the log"
    stop $S
fi

# --- NeoForge: its installer, then @libraries/.../unix_args.txt
if want neoforge || want mods || want update; then
    BUILDS=$(api "/api/java/builds?loader=neoforge&version=1.21.1")
    OLDB=$(echo "$BUILDS" | jget "[b['build'] for b in d['builds'] if 'beta' not in b['build']][5]")
    NEWB=$(echo "$BUILDS" | jget "[b['build'] for b in d['builds'] if 'beta' not in b['build']][0]")
    [ -n "$OLDB" ] && ok "NeoForge builds for 1.21.1 listed; pinning $OLDB (newest is $NEWB)" || bad "NeoForge builds ($BUILDS)"
    c=$(deploy "{\"edition\":\"java\",\"loader\":\"neoforge\",\"version\":\"1.21.1\",\"advanced\":{\"name\":\"neoforge\",\"port\":25712,\"heap\":\"2G\",\"build\":\"$OLDB\",\"flags\":{\"preset\":\"none\",\"extra\":[\"-Dfile.encoding=UTF-8\"]}}}")
    [ "$c" = 200 ] && ok "NeoForge 1.21.1 deployed" || bad "NeoForge deploy ($c $(body))"
    S=minecraft-neoforge
    unit $S | grep -q -- "-Xmx2G -Dfile.encoding=UTF-8 @libraries/net/neoforged/neoforge/$OLDB/unix_args.txt nogui" \
        && ok "  unit: heap and flags from the panel, then the installer's arguments file" || bad "  unit: $(unit $S)"
    check "  no server.jar, start script or user_jvm_args.txt"  dx "! ls -A /opt/$S | grep -E '^server\.jar$|^run\.|user_jvm_args|installer'"
    check "  everything belongs to minecraft"                   dx "test -z \"\$(find /opt/$S ! -user minecraft | head -1)\""
    L=$(api "/api/servers" | jget "[(s['loader'], s['minecraft'], s['version'], s['edition'], s['ram']) for s in d if s['name'] == '$S'][0]")
    [ "$L" = "('neoforge', '1.21.1', 'NeoForge 1.21.1', 'Java', '2G')" ] && ok "  the server list shows NeoForge 1.21.1" || bad "  server list: $L"
    started_ok $S NeoForge 450
    dx "grep -q 'neoforge' /opt/$S/logs/latest.log" && ok "  the log says NeoForge" || bad "  no NeoForge in the log"
fi

# --- Mods: what each loader takes
if want mods; then
    S=minecraft-neoforge
    modrinth_file spark neoforge 1.21.1 spark-neoforge.jar && modrinth_file spark fabric 1.21.1 spark-fabric.jar \
        && ok "spark for NeoForge and for Fabric downloaded from Modrinth" || bad "couldn't download test mods"
    c=$(drop $S spark-neoforge.jar); [ "$c" = 200 ] && ok "  a NeoForge mod is accepted on NeoForge" || bad "  NeoForge mod ($c $(body))"
    c=$(drop $S spark-fabric.jar)
    [ "$c" = 400 ] && body | grep -q "This is a Fabric mod, and this is a NeoForge server" && ok "  a Fabric mod is refused there, with the reason" || bad "  Fabric mod on NeoForge ($c $(body))"
    M=$(api "/api/servers/$S/addons" | jget "[(i['type'], i['id'], i['problem']) for i in d['items']]")
    [ "$M" = "[('neoforge', 'spark', None)]" ] && ok "  listed as spark, read from neoforge.mods.toml" || bad "  mods list: $M"
    n=$(api "/api/servers/$S/addons/search?q=jei" | jget "len(d['hits'])")
    [ "${n:-0}" -ge 1 ] && ok "  Find mods searches Modrinth for NeoForge 1.21.1" || bad "  search ($n)"
    c=$(dx "$CURL -o /tmp/body -w '%{http_code}' -H 'Content-Type: application/json' -d '{\"source\":\"modrinth\",\"project\":\"ferrite-core\"}' http://127.0.0.1:8080/api/servers/$S/addons/install")
    [ "$c" = 200 ] && ok "  FerriteCore installed from the catalog" || bad "  catalog install ($c $(body))"
fi

# --- Update: the installer runs again for the newest build; mods stay
if want update; then
    S=minecraft-neoforge
    c=$(dx "$CURL -o /tmp/body -w '%{http_code}' --max-time 1800 -H 'Content-Type: application/json' -d '{\"version\":\"1.21.1\"}' http://127.0.0.1:8080/api/servers/$S/update")
    [ "$c" = 200 ] && ok "NeoForge updated from $OLDB" || bad "update ($c $(body))"
    unit $S | grep -q -- "-Xmx2G -Dfile.encoding=UTF-8 @libraries/net/neoforged/neoforge/$NEWB/unix_args.txt nogui" \
        && ok "  unit now starts $NEWB, same heap and flags" || bad "  unit: $(unit $S)"
    want mods && { check "  the mods are still there" dx "ls /opt/$S/mods | grep -qi spark"; }
    check "  no update staging folder left" dx "! ls -A /opt/$S | grep -q redstonepanel-update"
    dx "rm -f /opt/$S/logs/latest.log"; dx "systemctl restart $S"
    started_ok $S "NeoForge $NEWB" 450
    # 1.19's Roll back the last update: nothing of the old NeoForge was kept, its installer runs again
    U=$(api "/api/servers/$S/convert" | jget "d['undo']['kind'], d['undo']['available']")
    [ "$U" = "('update', True)" ] && ok "  Roll back the last update is offered" || bad "  undo record: $U"
    c=$(postj "/api/servers/$S/update/rollback"); [ "$c" = 200 ] || bad "  rollback ($c $(body))"
    st=$(wait_job $S)
    [ "$st" = done ] && ok "rolled back to NeoForge $OLDB with the world from before" || bad "rollback: $st $(api "/api/servers/$S/convert" | jget "d['job'].get('error')")"
    unit $S | grep -q -- "@libraries/net/neoforged/neoforge/$OLDB/unix_args.txt nogui" && ok "  unit starts $OLDB again" || bad "  unit: $(unit $S)"
    want mods && { check "  the mods are still there" dx "ls /opt/$S/mods | grep -qi spark"; }
fi
stop minecraft-neoforge

# --- Forge 1.20.1 (Java 17, arguments file)
if want forge; then
    c=$(deploy '{"edition":"java","loader":"forge","version":"1.20.1","advanced":{"name":"forge","port":25713,"heap":"2G"}}')
    [ "$c" = 200 ] && ok "Forge 1.20.1 deployed" || bad "Forge deploy ($c $(body))"
    S=minecraft-forge
    FB=$(info $S | jget "d['build']")
    unit $S | grep -q -- "java-17.*-Xmx2G @libraries/net/minecraftforge/forge/1.20.1-$FB/unix_args.txt nogui" \
        && ok "  unit: Java 17 and Forge $FB's arguments file (the recommended build)" || bad "  unit: $(unit $S)"
    started_ok $S Forge 450
    stop $S
fi

# --- Forge 1.12.2 (Java 8 only, started from Forge's own jar)
if want oldforge; then
    c=$(deploy '{"edition":"java","loader":"forge","version":"1.12.2","advanced":{"name":"oldforge","port":25714,"heap":"1G"}}')
    [ "$c" = 200 ] && ok "Forge 1.12.2 deployed" || bad "Forge 1.12.2 deploy ($c $(body))"
    S=minecraft-oldforge
    unit $S | grep -Eq -- "java-8-openjdk[^ ]*/bin/java -Xms\S+ -Xmx1G -jar forge-1\.12\.2-[0-9.]+\.jar nogui" \
        && ok "  unit: Java 8 and Forge's own jar" || bad "  unit: $(unit $S)"
    info $S | grep -q '"java_max": 8' && ok "  recorded that it needs Java 8 at most" || bad "  info: $(info $S)"
    started_ok $S "Forge 1.12.2" 300
    stop $S
fi

# --- A Modrinth modpack: an older version, the update check, the update
if want modpack; then
    n=$(api "/api/modpacks/search?q=adrenaline" | jget "[h['slug'] for h in d['hits']].count('adrenaline')")
    [ "$n" = 1 ] && ok "modpack search finds Adrenaline on Modrinth" || bad "modpack search ($n)"
    VERS=$(api "/api/modpacks/versions?project=adrenaserver")
    OLDV=$(echo "$VERS" | jget "[v['id'] for v in d['versions'] if v['minecraft'] == ['1.20.6'] and v['channel'] == 'release'][-1]")
    OLDN=$(echo "$VERS" | jget "[v['version'] for v in d['versions'] if v['id'] == '$OLDV'][0]")
    [ -n "$OLDV" ] && ok "  its versions are listed; starting from $OLDN" || bad "  versions"
    c=$(post /api/servers/modpack source=modrinth project=adrenaserver "version_id=$OLDV" name=pack port=25720 memory=2G)
    [ "$c" = 200 ] && ok "server created from Adrenaserver $OLDN" || bad "modpack create ($c $(body))"
    S=minecraft-pack
    info $S | grep -q '"loader": "fabric", "minecraft": "1.20.6"' && ok "  Fabric for Minecraft 1.20.6, as the pack says" || bad "  info: $(info $S)"
    WANT=$(dx "cat /opt/$S/.redstonepanel-modpack.json" | jget "d['loader_version']")
    info $S | grep -q "\"build\": \"loader $WANT\"" && ok "  the pack's own Fabric loader ($WANT)" || bad "  loader: $(info $S) wanted $WANT"
    N1=$(dx "ls /opt/$S/mods/*.jar | wc -l")
    R1=$(dx "cat /opt/$S/.redstonepanel-modpack.json" | jget "len(d['files'])")
    [ "$N1" -gt 5 ] && [ "$N1" = "$R1" ] && ok "  $N1 mods downloaded and recorded" || bad "  mods: $N1 in the folder, $R1 recorded"
    unit $S | grep -q -- "-Xmx2G -jar server.jar nogui" && ok "  2G heap as chosen" || bad "  unit: $(unit $S)"
    [ "$(dx "sed -n 's/^server-port=//p' /opt/$S/server.properties")" = 25720 ] && ok "  on port 25720" || bad "  port"
    check "  everything belongs to minecraft"   dx "test -z \"\$(find /opt/$S ! -user minecraft | head -1)\""
    check "  no staging folder left in /opt"    dx "! ls -A /opt | grep -q redstonepanel-pack"
    started_ok $S "the modpack server" 450
    P=$(api "/api/servers/$S/modpack" | jget "(d['pack']['name'], d['pack']['version'], d['pack']['can_check'])")
    [ "$P" = "('Adrenaserver', '$OLDN', True)" ] && ok "  Maintenance shows Adrenaserver $OLDN" || bad "  status: $P"
    # a world, a mod of the owner's and a changed setting, which the update must keep
    dx "echo mine > /opt/$S/mods/owners-own.jar.disabled; chown minecraft: /opt/$S/mods/owners-own.jar.disabled; sed -i 's/^motd=.*/motd=Kept/' /opt/$S/server.properties"
    WORLD=$(dx "md5sum /opt/$S/world/level.dat | cut -d' ' -f1")
    c=$(post "/api/servers/$S/modpack/check"); CHK=$(body)
    NEWN=$(echo "$CHK" | jget "d['latest']['version']"); NEWV=$(echo "$CHK" | jget "d['latest']['id']")
    [ "$c" = 200 ] && [ "$(echo "$CHK" | jget "d['available']")" = True ] && ok "  update check: $NEWN is available" || bad "  check ($c $CHK)"
    c=$(post "/api/servers/$S/modpack/update" source=modrinth "version_id=$NEWV" backup=true)
    [ "$c" = 200 ] && ok "updated to $NEWN" || bad "modpack update ($c $(body))"
    BK=$(body | jget "d['backup']")
    dx "test -s /var/backups/redstonepanel/$S/$BK" && ok "  a backup was taken first ($BK)" || bad "  backup: $BK"
    NEWMC=$(dx "cat /opt/$S/.redstonepanel-modpack.json" | jget "d['minecraft']")
    info $S | grep -q "\"minecraft\": \"$NEWMC\"" && ok "  Fabric reinstalled for Minecraft $NEWMC" || bad "  info: $(info $S)"
    [ "$(dx "cat /opt/$S/.redstonepanel-modpack.json" | jget "d['version']")" = "$NEWN" ] && ok "  recorded as $NEWN" || bad "  record"
    N2=$(dx "ls /opt/$S/mods/*.jar | wc -l"); R2=$(dx "cat /opt/$S/.redstonepanel-modpack.json" | jget "len(d['files'])")
    [ "$N2" = "$R2" ] && ok "  the old version's mods are gone, the new one's $N2 are in" || bad "  mods: $N2 in the folder, $R2 recorded"
    check "  the owner's own mod is still there"  dx "test -f /opt/$S/mods/owners-own.jar.disabled"
    [ "$(dx "sed -n 's/^motd=//p' /opt/$S/server.properties")" = Kept ] && ok "  server.properties kept" || bad "  motd changed"
    dx "test -f /opt/$S/world/level.dat" && ok "  the world is still there" || bad "  world gone"
    started_ok $S "the updated modpack server" 450
    c=$(post "/api/servers/$S/modpack/update" source=modrinth "version_id=$NEWV")
    [ "$c" = 400 ] && body | grep -q "already runs" && ok "  updating to the same version again is refused" || bad "  same version ($c $(body))"
    stop $S
fi

# --- Changing type (1.19) with the new types: Paper <-> Purpur, Fabric <-> Quilt
if want switch; then
    c=$(deploy '{"edition":"java","loader":"paper","version":"1.21.4","advanced":{"name":"swp","port":25730,"heap":"1G"}}')
    S=minecraft-swp
    [ "$c" = 200 ] && ok "Paper 1.21.4 deployed for the type changes" || bad "Paper deploy ($c $(body))"
    started_ok $S Paper
    modrinth_file luckperms paper 1.21.4 luckperms-paper.jar && [ "$(drop $S luckperms-paper.jar)" = 200 ] || bad "  couldn't add a plugin"
    change $S purpur Purpur
    [ "$(echo "$PLAN" | jget "d['addons']['action'], d['world']['moves']")" = "('keep', [])" ] && ok "  preview: plugins stay, nothing in the world moves" || bad "  plan: $PLAN"
    info $S | grep -q '"loader": "purpur"' && dx "ls /opt/$S/plugins | grep -qi luckperms && ! test -e /opt/$S/plugins.off" \
        && ok "  it is Purpur, with its plugins in place" || bad "  after Paper -> Purpur: $(info $S)"
    dx "grep -qi 'purpur' /opt/$S/logs/latest.log" && dx "grep -qi 'LuckPerms' /opt/$S/logs/latest.log" && ok "  Purpur loaded the plugin" || bad "  the log doesn't show Purpur with LuckPerms"
    undo $S "Paper 1.21.4"
    info $S | grep -q '"loader": "paper"' && ok "  recorded as Paper again" || bad "  after undo: $(info $S)"
    stop $S

    c=$(deploy '{"edition":"java","loader":"fabric","version":"1.21.4","advanced":{"name":"swf","port":25731,"heap":"1G"}}')
    S=minecraft-swf
    [ "$c" = 200 ] && ok "Fabric 1.21.4 deployed for the type changes" || bad "Fabric deploy ($c $(body))"
    started_ok $S Fabric
    modrinth_file lithium fabric 1.21.4 lithium-fabric.jar && [ "$(drop $S lithium-fabric.jar)" = 200 ] || bad "  couldn't add a mod"
    change $S quilt Quilt
    [ "$(echo "$PLAN" | jget "d['addons']['action'], d['confirm_name']")" = "('keep', False)" ] && ok "  preview: the mods stay" || bad "  plan: $PLAN"
    unit $S | grep -q -- "-jar quilt-server-launch.jar nogui" && ok "  unit starts Quilt's own jar" || bad "  unit: $(unit $S)"
    dx "ls /opt/$S/mods | grep -qi lithium && test -s /opt/$S/server.jar.previous" && ok "  mods in place, Fabric's jar kept for undo" || bad "  after Fabric -> Quilt: $(dx "ls -A /opt/$S")"
    dx "grep -qi 'lithium' /opt/$S/logs/latest.log" && ok "  Quilt loaded the Fabric mod" || bad "  the log doesn't show Lithium on Quilt"
    undo $S "Fabric 1.21.4"
    unit $S | grep -q -- "-jar server.jar nogui" && dx "! test -e /opt/$S/quilt-server-launch.jar" && ok "  unit starts server.jar again; Quilt's jar is gone" || bad "  unit: $(unit $S)"
    change $S quilt Quilt
    change $S fabric Fabric
    dx "! test -e /opt/$S/quilt-server-launch.jar && ! test -e /opt/$S/server.jar.previous" && ok "  Quilt -> Fabric: Quilt's jar gone, nothing kept (its installer runs again for an undo)" || bad "  leftovers: $(dx "ls -A /opt/$S")"
    undo $S "Quilt 1.21.4"
    unit $S | grep -q -- "-jar quilt-server-launch.jar nogui" && info $S | grep -q '"loader": "quilt"' && ok "  Quilt reinstalled by the undo" || bad "  unit: $(unit $S) $(info $S)"
    check "  no staging folder left" dx "! ls -A /opt/$S | grep -q 'redstonepanel-convert\|redstonepanel-update'"
    stop $S
fi

# --- Uploads: a .mrpack, a server pack, and what's refused
if want upload; then
    # A .mrpack of our own: one real mod from Modrinth by hash, one client-only file, overrides
    META=$(dx "curl -s 'https://api.modrinth.com/v2/project/lithium/version?loaders=%5B%22fabric%22%5D&game_versions=%5B%221.21.1%22%5D'")
    URL=$(echo "$META" | jget "d[0]['files'][0]['url']"); H512=$(echo "$META" | jget "d[0]['files'][0]['hashes']['sha512']")
    FN=$(echo "$META" | jget "d[0]['files'][0]['filename']"); SZ=$(echo "$META" | jget "d[0]['files'][0]['size']")
    dx "rm -rf /tmp/mp && mkdir -p /tmp/mp/overrides/config /tmp/mp/server-overrides /tmp/mp/client-overrides && cd /tmp/mp
        echo 'a=1' > overrides/config/test.toml; echo 'fov=90' > client-overrides/options.txt
        printf 'motd=From the pack\nserver-port=1\nenable-rcon=true\n' > server-overrides/server.properties
        echo 'touch /tmp/SCRIPT_RAN' > overrides/start.sh
        cat > modrinth.index.json <<EOF
{\"formatVersion\":1,\"game\":\"minecraft\",\"versionId\":\"0.1\",\"name\":\"Upload Test\",
 \"dependencies\":{\"minecraft\":\"1.21.1\",\"fabric-loader\":\"0.16.9\"},
 \"files\":[{\"path\":\"mods/$FN\",\"hashes\":{\"sha512\":\"$H512\"},\"env\":{\"client\":\"optional\",\"server\":\"required\"},\"downloads\":[\"$URL\"],\"fileSize\":$SZ},
          {\"path\":\"mods/client-only.jar\",\"hashes\":{\"sha512\":\"$H512\"},\"env\":{\"client\":\"required\",\"server\":\"unsupported\"},\"downloads\":[\"$URL\"],\"fileSize\":$SZ}]}
EOF
        zip -qr /tmp/test.mrpack . && sed -i 's/$H512/$(printf 'a%.0s' $(seq 128))/' modrinth.index.json && zip -qr /tmp/badhash.mrpack . \
        && sed -i 's#https://cdn.modrinth.com#https://example.com#' modrinth.index.json && zip -qr /tmp/badhost.mrpack ."
    c=$(post /api/servers/modpack source=mrpack "file=@/tmp/test.mrpack" name=uploaded port=25721 memory=1G)
    [ "$c" = 200 ] && ok "server created from an uploaded .mrpack" || bad ".mrpack upload ($c $(body))"
    S=minecraft-uploaded
    [ "$(dx "ls /opt/$S/mods")" = "$FN" ] && ok "  its server-side mod is in, the client-only one isn't" || bad "  mods: $(dx "ls /opt/$S/mods")"
    info $S | grep -q '"build": "loader 0.16.9"' && ok "  the pack's Fabric loader 0.16.9" || bad "  info: $(info $S)"
    check "  overrides copied, client-overrides and scripts not" dx "test -f /opt/$S/config/test.toml && ! test -e /opt/$S/options.txt && ! test -e /opt/$S/start.sh"
    [ "$(dx "sed -n 's/^motd=//p' /opt/$S/server.properties")" = "From the pack" ] && [ "$(dx "sed -n 's/^server-port=//p' /opt/$S/server.properties")" = 25721 ] \
        && ! dx "grep -q '^enable-rcon=true' /opt/$S/server.properties" && ok "  the pack's settings applied, but not its port or RCON" || bad "  properties: $(dx "cat /opt/$S/server.properties")"
    started_ok $S "the uploaded pack's server" 450
    stop $S
    c=$(post /api/servers/modpack source=mrpack "file=@/tmp/badhash.mrpack" name=badhash)
    [ "$c" = 502 ] || [ "$c" = 400 ] && body | grep -q "checksum" && ok "a file that doesn't match the pack's hash stops the install" || bad "bad hash ($c $(body))"
    c=$(post /api/servers/modpack source=mrpack "file=@/tmp/badhost.mrpack" name=badhost)
    [ "$c" = 400 ] && body | grep -q "doesn't allow" && ok "a download from another host is refused" || bad "bad host ($c $(body))"
    check "  nothing left behind by the refused packs" dx "! ls -A /opt | grep -Eq 'badhash|badhost|redstonepanel-pack'"

    # A server pack like CurseForge's: mods and config, a start script naming Forge, an installer jar
    modrinth_file spark forge 1.20.1 spark-forge.jar || bad "couldn't download spark for Forge"
    dx "rm -rf /tmp/sp && mkdir -p '/tmp/sp/Test Pack Server/mods' '/tmp/sp/Test Pack Server/config' && cd '/tmp/sp/Test Pack Server'
        cp /tmp/spark-forge.jar mods/; echo 'x=1' > config/pack.toml; echo 'junk' > forge-1.20.1-47.2.0-installer.jar
        printf '#!/bin/bash\nFORGE_VERSION=47.2.0\nMINECRAFT_VERSION=1.20.1\ntouch /tmp/SCRIPT_RAN\njava -jar forge-installer.jar\n' > startserver.sh
        echo '-Xmx99G' > user_jvm_args.txt; printf 'motd=Server pack\nmax-players=33\n' > server.properties
        cd /tmp/sp && zip -qr /tmp/serverpack.zip ."
    c=$(post /api/servers/modpack source=serverpack "file=@/tmp/serverpack.zip;filename=TestPack-Server-1.0.zip" name=serverpack port=25722 memory=2G)
    [ "$c" = 200 ] && ok "server created from an uploaded server pack" || bad "server pack upload ($c $(body))"
    S=minecraft-serverpack
    info $S | grep -q '"loader": "forge", "minecraft": "1.20.1", "build": "47.2.0"' && ok "  Forge 47.2.0 for 1.20.1, read from the pack's start script" || bad "  info: $(info $S)"
    check "  the pack's mods and config are in"                    dx "test -f /opt/$S/mods/spark-forge.jar && test -f /opt/$S/config/pack.toml"
    check "  its scripts, installer and JVM options are not"       dx "! ls -A /opt/$S | grep -E 'startserver|installer|user_jvm_args'"
    check "  its start script never ran"                           dx "! test -e /tmp/SCRIPT_RAN"
    unit $S | grep -q -- "-Xmx2G @libraries/net/minecraftforge/forge/1.20.1-47.2.0/unix_args.txt nogui" && ok "  the panel's own unit starts it" || bad "  unit: $(unit $S)"
    [ "$(dx "sed -n 's/^max-players=//p' /opt/$S/server.properties")" = 33 ] && ok "  the pack's server.properties settings applied" || bad "  properties"
    started_ok $S "the server pack's server" 450
    dx "grep -qi 'spark' /opt/$S/logs/latest.log" && ok "  and loaded the pack's mod" || bad "  spark not in the log"
    stop $S
    dx "cd /tmp && rm -rf cl && mkdir cl && cd cl && echo '{\"minecraft\":{\"version\":\"1.20.1\",\"modLoaders\":[{\"id\":\"forge-47.2.0\"}]}}' > manifest.json && mkdir overrides && zip -qr /tmp/client.zip ."
    c=$(post /api/servers/modpack source=serverpack "file=@/tmp/client.zip" name=client)
    [ "$c" = 400 ] && body | grep -q "Server Pack" && ok "a CurseForge client zip is refused, saying to get the Server Pack" || bad "client zip ($c $(body))"
    # only admins
    dx "echo 'helper-pass-123' | redstonepanel add-user helper --role user" >/dev/null
    dx "curl -s -c /tmp/jar2 -H 'Content-Type: application/json' -d '{\"username\":\"helper\",\"password\":\"helper-pass-123\"}' http://127.0.0.1:8080/api/auth/login" >/dev/null
    c=$(dx "curl -s -o /dev/null -w '%{http_code}' -b /tmp/jar2 -H 'Origin: http://127.0.0.1:8080' -F source=modrinth -F project=adrenaserver http://127.0.0.1:8080/api/servers/modpack")
    [ "$c" = 403 ] && ok "a user who isn't an admin can't install a modpack" || bad "non-admin ($c)"
fi

dx "grep -c 'MODPACK' /var/log/redstonepanel/panel.log" >/dev/null && ok "the panel log has the modpack lines" || true
echo
if [ "$FAILS" = 0 ]; then echo "ALL PASSED"; else echo "$FAILS FAILED"; fi
exit "$FAILS"
