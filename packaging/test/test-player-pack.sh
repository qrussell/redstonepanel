#!/bin/bash
# End-to-end test of player packs (1.21.6) in throwaway systemd containers that install the
# .deb from dist/:
#   1. one panel (pk) with a Fabric server whose mods folder holds four jars: which of them
#      players get, the .mrpack and the packwiz folder the join page hands out, an admin
#      overruling a line, the pack following the folder, and turning it off.
#   2. a cluster (pkm master, pkn node): the master builds the pack of a server on the node,
#      with a jar the admin chose to hand out fetched from the node.
# Nothing is downloaded but apt packages: Modrinth is a stand-in (RP_MODRINTH_API), a small
# Python program in each container that knows the test's jars by their checksums. The game
# servers are stand-ins too (a unit that sleeps). Run from the repository root with Docker,
# after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-player-pack.sh        (KEEP=1 leaves the containers running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
NET=rp-pack-test
IMG=${IMG:-rp-systemd:24.04}
ALL="pk pkm pkn"
PW_ADMIN='pack-test-admin-1'             # throwaway accounts inside the test containers only
PW_ALEX='pack-test-alex-1'
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$1" bash -c "$2"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
is()   { local name=$1 want=$2 got=$3; [ "$got" = "$want" ] && ok "$name" || bad "$name: wanted $want, got $got"; }
has()  { local name=$1 text=$2 body=$3; case "$body" in *"$text"*) ok "$name";; *) bad "$name: '$text' not on the page";; esac; }
lacks(){ local name=$1 text=$2 body=$3; case "$body" in *"$text"*) bad "$name: '$text' is on the page";; *) ok "$name";; esac; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
json() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
# The panel under test: container, and where its own API answers
P=pk; URL=http://127.0.0.1:8080; PUB=http://127.0.0.1:8444
api()  { dx $P "curl -sk -b /tmp/jar-$1 -X $2 '$URL$3' ${4:+-H 'Content-Type: application/json' -d '$4'}"; }
code() { dx $P "curl -sk -o /dev/null -w '%{http_code}' -b /tmp/jar-$1 -X $2 '$URL$3' ${4:+-H 'Content-Type: application/json' -d '$4'}"; }
login(){ dx $P "curl -sk -c /tmp/jar-$1 -H 'Content-Type: application/json' -d '{\"username\":\"$1\",\"password\":\"$2\"}' $URL/api/auth/login" >/dev/null; }
pub()  { dx $P "curl -sk -m 10 '$PUB$1'"; }                                   # a public page's body
pcode(){ dx $P "curl -sk -m 10 -o /dev/null -w '%{http_code}' -X ${2:-GET} '$PUB$1'"; }
page() { api admin PUT /api/join-pages/page "$1"; }
listening() { dx "$1" "ss -ltn | grep -q ':$2 '"; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Containers kept: $ALL (docker rm -f $ALL; docker network rm $NET)"; return; }
    docker rm -f $ALL >/dev/null 2>&1
    docker network rm "$NET" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== player packs test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f $ALL >/dev/null 2>&1
docker network rm "$NET" >/dev/null 2>&1
docker network create --subnet 172.30.88.0/24 "$NET" >/dev/null
for c in $ALL; do
    docker run -d --privileged --cgroupns=private --name "$c" --hostname "$c" --network "$NET" \
        -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
done
sleep 3
[ -n "${RP_TEST_SETUP:-}" ] && for c in $ALL; do bash "$RP_TEST_SETUP" "$c"; done
for c in $ALL; do
    ( dx "$c" "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") python3 iproute2 >/tmp/install.log 2>&1" \
        || { echo "install failed on $c"; dx "$c" "tail -20 /tmp/install.log"; } ) &
done
wait
for c in $ALL; do check "$c: panel installed and running" dx "$c" "systemctl is-active --quiet redstonepanel"; done

# --- stand-in servers (a unit that sleeps). standin CONTAINER NAME KIND PORT [EXTRA SHELL] [MOTD]
standin_tools() {
    dx "$1" "mkdir -p /usr/local/lib/rp-fakejava && printf '#!/bin/sh\nexec sleep infinity\n' > /usr/local/lib/rp-fakejava/java && chmod +x /usr/local/lib/rp-fakejava/java"
}
standin() {
    local c=$1 name=$2 kind=$3 port=$4 run
    run='/usr/local/lib/rp-fakejava/java -Xms256M -Xmx512M -jar server.jar nogui'
    dx "$c" "mkdir -p /opt/$name && cd /opt/$name && ${5:-true}
printf 'server-port=$port\nmotd=${6:-A Minecraft Server}\nserver-name=${6:-Stand-in}\n' > server.properties
chown -R minecraft:minecraft /opt/$name
cat > /etc/systemd/system/$name.service <<UNIT
[Unit]
Description=Minecraft Server ($name)
[Service]
User=minecraft
Group=minecraft
WorkingDirectory=/opt/$name
ExecStart=$run
Restart=no
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && systemctl start $name"
}

# --- a stand-in Modrinth on 127.0.0.1:8099, and the panel told to ask it
fake_modrinth() {
    dx "$1" "mkdir -p /usr/local/lib/rp-fakemodrinth && cat > /usr/local/lib/rp-fakemodrinth/serve.py <<'PYEOF'
#!/usr/bin/env python3
# Knows a jar by the mod id in its fabric.mod.json: KNOWN[id] = (client side, server side).
import glob, hashlib, json, zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
KNOWN = {'sodium': ('required', 'unsupported'), 'fabric-api': ('optional', 'optional'),
         'spark': ('unsupported', 'required'), 'lithium': ('optional', 'required')}
def jars():
    out = {}
    for path in glob.glob('/opt/*/mods/*.jar'):
        try:
            data = open(path, 'rb').read()
            mod = json.loads(zipfile.ZipFile(path).read('fabric.mod.json'))['id']
        except Exception:
            continue
        if mod in KNOWN:
            out[hashlib.sha512(data).hexdigest()] = (mod, path.rsplit('/', 1)[1], data)
    return out
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, body):
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        want = json.loads(self.rfile.read(int(self.headers['Content-Length'])))['hashes']
        found = jars()
        self.send({h: {'id': 'V' + found[h][0][:6], 'project_id': 'P' + found[h][0], 'version_number': '1.0.0',
                       'files': [{'primary': True, 'filename': found[h][1], 'size': len(found[h][2]),
                                  'url': 'https://cdn.modrinth.com/data/P%s/versions/V/%s' % (found[h][0], found[h][1]),
                                  'hashes': {'sha1': hashlib.sha1(found[h][2]).hexdigest(), 'sha512': h}}]}
                   for h in want if h in found})
    def do_GET(self):
        self.send([{'id': 'P' + m, 'slug': m, 'title': m.title(), 'client_side': c, 'server_side': s}
                   for m, (c, s) in KNOWN.items() if 'P' + m in self.path])
HTTPServer(('127.0.0.1', 8099), H).serve_forever()
PYEOF
cat > /etc/systemd/system/rp-fakemodrinth.service <<UNIT
[Service]
ExecStart=/usr/bin/python3 /usr/local/lib/rp-fakemodrinth/serve.py
UNIT
mkdir -p /etc/systemd/system/redstonepanel.service.d
printf '[Service]\nEnvironment=RP_MODRINTH_API=http://127.0.0.1:8099/v2\n' > /etc/systemd/system/redstonepanel.service.d/test-modrinth.conf
systemctl daemon-reload && systemctl start rp-fakemodrinth && systemctl restart redstonepanel"
}
# mod CONTAINER SERVER FILE ID [DEPENDS]: a small Fabric mod jar in the server's mods folder
mod() {
    dx "$1" "mkdir -p /opt/$2/mods && python3 - <<PYEOF
import json, zipfile
with zipfile.ZipFile('/opt/$2/mods/$3', 'w') as z:
    z.writestr('fabric.mod.json', json.dumps({'id': '$4', 'name': '$4'.title(), 'version': '1.0.0',
                                              'depends': {d: '*' for d in '${5:-}'.split()}}))
    z.writestr('about.txt', '$3 of $2')
PYEOF
chown -R minecraft:minecraft /opt/$2/mods"
}
FABRIC="printf '{\"loader\": \"fabric\", \"minecraft\": \"1.21.1\", \"build\": \"loader 0.16.9\"}' > .redstonepanel-server.json"
# What a downloaded pack says, in one line: read with python inside the container
packsay() { dx $P "curl -sk -m 10 -o /tmp/p.mrpack '$PUB$1' && python3 -c \"
import json, zipfile
z = zipfile.ZipFile('/tmp/p.mrpack'); i = json.loads(z.read('modrinth.index.json'))
print($2)\""; }

S=minecraft-mods
standin_tools pk
fake_modrinth pk
standin pk $S java 25601 "$FABRIC" 'Modded <b>Fun</b>'
mod pk $S sodium.jar sodium "fabric-api minecraft"
mod pk $S fabric-api.jar fabric-api
mod pk $S spark.jar spark
mod pk $S lithium.jar lithium
mod pk $S homemade.jar homemade
mod pk $S resting.jar.disabled lithium
standin pk minecraft-plain java 25602 "printf '{\"loader\": \"paper\", \"minecraft\": \"1.21.11\"}' > .redstonepanel-server.json" 'Plain'
sleep 2

dx pk "echo '$PW_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
dx pk "echo '$PW_ALEX' | redstonepanel add-user alex --role user" >/dev/null
for _ in $(seq 1 40); do dx pk "curl -sf $URL/api/health" >/dev/null 2>&1 && break; sleep 1; done
login admin "$PW_ADMIN"; login alex "$PW_ALEX"

echo "--- which mods players get"
V=$(api admin GET /api/servers/$S/player-pack)
state() { echo "$V" | json "' '.join(m['file'] + '=' + m['state'] for m in d['mods'])"; }
is "required, needed by a required mod, optional, server-only, unknown" \
   "fabric-api.jar=in homemade.jar=out lithium.jar=optional sodium.jar=in spark.jar=out" "$(state)"
is "  why Fabric API is in" "Needed by Sodium" "$(echo "$V" | json "[m['why'] for m in d['mods'] if m['file']=='fabric-api.jar'][0]")"
is "  on by itself, for the server's loader" "True fabric 1.21.1 0.16.9" "$(echo "$V" | json "str(d['active'])+' '+d['loader']+' '+d['minecraft']+' '+d['loader_version']")"
V1=$(echo "$V" | json "d['version']")
is "a server without mods has no player pack" 400 "$(code admin GET /api/servers/minecraft-plain/player-pack)"
is "an account with nothing on the server can't read it" 403 "$(code alex GET /api/servers/$S/player-pack)"
api admin PUT /api/users/alex/grants "{\"grants\":{\"$S\":[\"view\",\"mods\"]}}" >/dev/null
is "  with Mods & plugins it can" 200 "$(code alex GET /api/servers/$S/player-pack)"
is "  and change it" 200 "$(code alex PUT /api/servers/$S/player-pack '{"name":"Family pack"}')"
is "a choice the panel doesn't know is refused" 400 "$(code admin PUT /api/servers/$S/player-pack '{"mods":{"sodium.jar":"sometimes"}}')"
is "  a file name that climbs out too" 400 "$(code admin PUT /api/servers/$S/player-pack '{"mods":{"../x.jar":"in"}}')"

echo "--- the join page hands it out"
is "no join page yet: nothing public" 000 "$(pcode /join/mods)"
api admin PUT /api/port-forwarding/settings '{"public_address":"play.example.org"}' >/dev/null
api admin PUT /api/settings/join '{"enabled":true,"port":8444}' >/dev/null
page "{\"server\":\"$S\",\"enabled\":true,\"slug\":\"mods\"}" >/dev/null
page '{"server":"minecraft-plain","enabled":true,"slug":"plain"}' >/dev/null
for _ in $(seq 1 20); do listening pk 8444 && break; sleep 1; done
BODY=$(pub /join/mods)
has "the page offers the player pack" 'href="/join/mods/pack.mrpack"' "$BODY"
has "  says how many mods" "3 mods, 1 of them optional" "$BODY"
has "  names it as the admin did" "Family pack" "$BODY"
has "  lists a mod with its Modrinth page" 'href="https://modrinth.com/mod/sodium"' "$BODY"
has "  names the mod it can't include" "Homemade" "$BODY"
has "  gives the packwiz address" "/join/mods/packwiz/pack.toml" "$BODY"
lacks "  never the server-only mod" "Spark" "$BODY"
lacks "  never the disabled file" "resting" "$BODY"
is "the .mrpack downloads as a file" "200 application/x-modrinth-modpack+zip" "$(dx pk "curl -sk -o /dev/null -w '%{http_code} %{content_type}' $PUB/join/mods/pack.mrpack")"
is "  with the mods, each pointing at Modrinth" "mods/fabric-api.jar mods/lithium.jar mods/sodium.jar True" \
   "$(packsay /join/mods/pack.mrpack "' '.join(sorted(f['path'] for f in i['files'])), all(f['downloads'][0].startswith('https://cdn.modrinth.com/') for f in i['files'])")"
is "  the loader and game version" "{'fabric-loader': '0.16.9', 'minecraft': '1.21.1'}" "$(packsay /join/mods/pack.mrpack "dict(sorted(i['dependencies'].items()))")"
is "  the optional mod marked optional, the client-only one not for servers" "optional unsupported" \
   "$(packsay /join/mods/pack.mrpack "[f['env']['client'] for f in i['files'] if 'lithium' in f['path']][0], [f['env']['server'] for f in i['files'] if 'sodium' in f['path']][0]")"
is "  no mod files inside" "['modrinth.index.json']" "$(packsay /join/mods/pack.mrpack "z.namelist()")"
is "  the panel's own pack reader takes it" "True" "$(dx pk "cd /usr/share/redstonepanel && python3 -c \"
from core import modpacks
p = modpacks.read_mrpack('/tmp/p.mrpack'); print(p['loader'] == 'fabric' and p['client_only'] == 1 and len(p['files']) == 2)\"")"
is "packwiz: pack.toml, and its index checksum matches" "True" "$(dx pk "curl -sk $PUB/join/mods/packwiz/pack.toml -o /tmp/pack.toml && curl -sk $PUB/join/mods/packwiz/index.toml -o /tmp/index.toml && python3 -c \"
import hashlib, tomllib
p = tomllib.load(open('/tmp/pack.toml', 'rb')); i = tomllib.load(open('/tmp/index.toml', 'rb'))
print(p['index']['hash'] == hashlib.sha256(open('/tmp/index.toml', 'rb').read()).hexdigest() and p['versions'] == {'minecraft': '1.21.1', 'fabric': '0.16.9'} and len(i['files']) == 3)\"")"
is "  a mod's file there" 200 "$(pcode /join/mods/packwiz/mods/sodium.pw.toml)"
for bad in /join/mods/packwiz/mods/spark.pw.toml /join/mods/packwiz/nope.toml /join/mods/mods/sodium.jar /join/mods/packwiz/mods/a/b /join/plain/pack.mrpack /join/plain/packwiz/pack.toml; do
    is "  nothing at $bad" 404 "$(pcode $bad)"
done
lacks "a server without mods offers no pack" "pack.mrpack" "$(pub /join/plain)"
is "POST to the pack gets nothing" 404 "$(pcode /join/mods/pack.mrpack POST)"

echo "--- an admin overrules, and the pack follows the folder"
api admin PUT /api/servers/$S/player-pack '{"mods":{"homemade.jar":"in","lithium.jar":"out","spark.jar":"optional"}}' >/dev/null
V=$(api admin GET /api/servers/$S/player-pack)
is "the choices stand" "fabric-api.jar=in homemade.jar=bundled lithium.jar=out sodium.jar=in spark.jar=optional" "$(state)"
V2=$(echo "$V" | json "d['version']")
[ "$V1" != "$V2" ] && ok "the pack gets a new number" || bad "pack number unchanged: $V1"
is "the file Modrinth doesn't know is inside the pack" "True" "$(packsay /join/mods/pack.mrpack "'overrides/mods/homemade.jar' in z.namelist()")"
is "  the same bytes as on the server" "True" "$(dx pk "cd /tmp && python3 -c \"
import zipfile
print(zipfile.ZipFile('p.mrpack').read('overrides/mods/homemade.jar') == open('/opt/$S/mods/homemade.jar', 'rb').read())\"")"
is "  packwiz serves it too" 200 "$(pcode /join/mods/packwiz/mods/homemade.jar)"
dx pk "rm /opt/$S/mods/sodium.jar"
is "a mod removed from the server leaves the pack at once" "mods/fabric-api.jar mods/spark.jar optional" \
   "$(packsay /join/mods/pack.mrpack "' '.join(sorted(f['path'] for f in i['files'])), [f['env']['client'] for f in i['files'] if 'fabric-api' in f['path']][0]")"
api admin PUT /api/servers/$S/player-pack '{"mode":"off"}' >/dev/null
is "turned off: the pack is gone" "404 404" "$(pcode /join/mods/pack.mrpack) $(pcode /join/mods/packwiz/pack.toml)"
api admin PUT /api/servers/$S/player-pack '{"mode":"auto"}' >/dev/null
is "  and back" 200 "$(pcode /join/mods/pack.mrpack)"
dx pk "systemctl stop rp-fakemodrinth"; dx pk "touch /opt/$S/mods/fabric-api.jar"
is "with Modrinth out of reach the last pack stays" 200 "$(pcode /join/mods/pack.mrpack)"
check "the activity log says who changed it" dx pk "grep -q '\[PLAYERPACK\] admin changed the player pack of $S' /var/log/redstonepanel/panel.log"

echo "--- a cluster: the master builds the pack of a server on a node"
P=pkm; URL=https://127.0.0.1:8443; PUB=https://127.0.0.1:8444
dx pkm "echo '$PW_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
dx pkm "redstonepanel set-name Main && redstonepanel cluster create Home" >/dev/null
for _ in $(seq 1 60); do dx pkm "curl -skf $URL/api/health" >/dev/null 2>&1 && break; sleep 1; done
JOIN=$(dx pkm "redstonepanel cluster join-code" | grep '^rpjoin1:')
OUT=$(dx pkn "redstonepanel set-name Basement && redstonepanel join '$JOIN'" 2>&1)
echo "$OUT" | grep -q "Joined cluster 'Home'" && ok "pkn joined as Basement" || { bad "pkn joined"; echo "$OUT"; }
standin_tools pkn
fake_modrinth pkn
standin pkn minecraft-sky java 25601 "$FABRIC" 'Sky mods'
mod pkn minecraft-sky sodium.jar sodium
mod pkn minecraft-sky homemade.jar homemade
for _ in $(seq 1 60); do dx pkn "curl -skf https://127.0.0.1:8443/api/health" >/dev/null 2>&1 && break; sleep 1; done
login admin "$PW_ADMIN"
N1=$(api admin GET /api/cluster/overview | json "[n['id'] for n in d['nodes'] if n['name']=='Basement'][0]")
for _ in $(seq 1 30); do [ "$(api admin GET /api/join-pages | json "sum(len(m['servers']) for m in d['members'])")" = 1 ] && break; sleep 2; done
is "the master changes the node's player pack through the node" 200 "$(code admin PUT "/api/cluster/nodes/$N1/proxy/api/servers/minecraft-sky/player-pack" '{"mods":{"homemade.jar":"in"}}')"
api admin PUT /api/settings/join '{"enabled":true,"port":8444}' >/dev/null
is "its join page is made on the master" 200 "$(code admin PUT /api/join-pages/page "{\"member\":\"$N1\",\"server\":\"minecraft-sky\",\"enabled\":true,\"slug\":\"sky\"}")"
for _ in $(seq 1 20); do listening pkm 8444 && break; sleep 1; done
has "the master's page offers the node's pack" 'href="/join/sky/pack.mrpack"' "$(pub /join/sky)"
is "the pack has the node's mod" "mods/sodium.jar" "$(packsay /join/sky/pack.mrpack "' '.join(f['path'] for f in i['files'])")"
is "  and the jar the admin chose, fetched from the node and checked" "True" "$(packsay /join/sky/pack.mrpack "'overrides/mods/homemade.jar' in z.namelist()")"
is "  packwiz on the master" 200 "$(pcode /join/sky/packwiz/pack.toml)"
check "the node opened nothing for it" bash -c "! docker exec pkn bash -c \"ss -ltn | grep -q ':8444 '\""

echo
[ "$FAILS" = 0 ] && echo "ALL PASSED" || echo "$FAILS FAILED"
exit $((FAILS > 0))
