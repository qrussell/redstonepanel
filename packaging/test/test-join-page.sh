#!/bin/bash
# End-to-end test of the join pages (1.21.5) in throwaway systemd containers that install the
# .deb from dist/:
#   1. one panel (jp): the public port is closed until an admin turns it on, serves each
#      server's page once that page is turned on, and answers NOTHING else: every route the
#      panel has is tried on the public port. What a page says is checked for a plugin server,
#      a modpack server and a Bedrock server, with and without player names.
#   2. a cluster (jpm master, jpn node): the master serves the page of a server on the node,
#      over HTTPS with the panel's certificate, and the node opens nothing.
#   3. behind HAProxy (1.21.8): a real HAProxy in jpn sends /join to jpm's join port. Each
#      player keeps their own rate limit, a forged X-Forwarded-For from anyone else is
#      ignored, and "only through the proxy" closes the port to everyone but HAProxy.
# The servers are stand-ins: a small Python program at .../rp-fakejava/java that answers the
# Minecraft server list ping with two players, and a bedrock_server that sleeps. Nothing is
# downloaded but apt packages. Run from the repository root on a machine with Docker, after
# building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-join-page.sh          (KEEP=1 leaves the containers running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
NET=rp-join-test
IMG=${IMG:-rp-systemd:24.04}
ALL="jp jpm jpn"
PW_ADMIN='join-test-admin-1'             # throwaway accounts inside the test containers only
PW_ALEX='join-test-alex-1'
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
P=jp; URL=http://127.0.0.1:8080; PUB=http://127.0.0.1:8444
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
echo "=== join pages test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f $ALL >/dev/null 2>&1
docker network rm "$NET" >/dev/null 2>&1
docker network create --subnet 172.30.87.0/24 "$NET" >/dev/null
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

# --- stand-in servers. standin CONTAINER NAME KIND PORT [EXTRA SHELL]
standin_tools() {
    dx "$1" "mkdir -p /usr/local/lib/rp-fakejava /usr/local/lib/rp-fakebedrock && cat > /usr/local/lib/rp-fakejava/java <<'PYEOF'
#!/usr/bin/env python3
# Answers the Minecraft server list ping (handshake, status request) like a real server.
import json, socket
props = dict(l.strip().split('=', 1) for l in open('server.properties') if '=' in l)
def varint(n):
    out = b''
    while True:
        b = n & 0x7F; n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n: return out
reply = json.dumps({'version': {'name': 'Stand-in 1.21.11', 'protocol': 774},
                    'players': {'max': 20, 'online': 2, 'sample': [{'name': 'Steve', 'id': '1'}, {'name': 'Alex', 'id': '2'}]},
                    'description': {'text': props.get('motd', 'A Minecraft Server')}}).encode()
packet = b'\x00' + varint(len(reply)) + reply
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('0.0.0.0', int(props['server-port']))); s.listen(5)
while True:
    c, _ = s.accept()
    try:
        c.settimeout(3); c.recv(1024); c.sendall(varint(len(packet)) + packet)
    except OSError: pass
    finally: c.close()
PYEOF
printf '#!/bin/sh\nexec sleep infinity\n' > /usr/local/lib/rp-fakebedrock/bedrock_server
chmod +x /usr/local/lib/rp-fakejava/java /usr/local/lib/rp-fakebedrock/bedrock_server"
}
standin() {
    local c=$1 name=$2 kind=$3 port=$4 run
    [ "$kind" = bedrock ] && run='/usr/local/lib/rp-fakebedrock/bedrock_server' \
                          || run='/usr/local/lib/rp-fakejava/java -Xms256M -Xmx512M -jar server.jar nogui'
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
J=minecraft-survival; M=minecraft-pack; B=minecraft-bedrock
standin_tools jp
standin jp $J java 25601 "printf '{\"loader\": \"paper\", \"minecraft\": \"1.21.11\"}' > .redstonepanel-server.json" 'Family <b>World</b>'
standin jp $M java 25602 "printf '{\"loader\": \"fabric\", \"minecraft\": \"1.21.1\", \"loader_version\": \"0.16.9\"}' > .redstonepanel-server.json; printf '{\"kind\": \"mrpack\", \"name\": \"Cobblemon Official\", \"version\": \"1.6.1\", \"minecraft\": \"1.21.1\", \"loader\": \"fabric\", \"project\": \"5FFgwNNP\", \"slug\": \"cobblemon-fabric\", \"version_id\": \"AbC123xy\", \"files\": [], \"overrides\": []}' > .redstonepanel-modpack.json" 'Cobblemon night'
standin jp $B bedrock 19140 true 'Pocket world'
sleep 2

dx jp "echo '$PW_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
dx jp "echo '$PW_ALEX' | redstonepanel add-user alex --role user" >/dev/null
for _ in $(seq 1 40); do dx jp "curl -sf $URL/api/health" >/dev/null 2>&1 && break; sleep 1; done
login admin "$PW_ADMIN"; login alex "$PW_ALEX"
api admin PUT /api/users/alex/grants '{"grants":{"*":["view","console","power","players","settings","backups","files","mods"]}}' >/dev/null

echo "--- off until an admin turns it on"
check "nothing listens on the join port after install" bash -c "! docker exec jp bash -c \"ss -ltn | grep -q ':8444 '\""
V=$(api admin GET /api/join-pages)
is "the admin page: off, port 8444, three servers, no page on" "False 8444 3 0" \
   "$(echo "$V" | json "str(d['listener']['enabled'])+' '+str(d['listener']['port'])+' '+str(len(d['members'][0]['servers']))+' '+str(sum(s['page']['enabled'] for s in d['members'][0]['servers']))")"
is "a user with every permission can't see the join pages" 403 "$(code alex GET /api/join-pages)"
is "  or turn the port on" 403 "$(code alex PUT /api/settings/join '{"enabled":true,"port":8444}')"
is "  or make a page public" 403 "$(code alex PUT /api/join-pages/page "{\"server\":\"$J\",\"enabled\":true}")"
is "signed out: refused" 401 "$(dx jp "curl -s -o /dev/null -w '%{http_code}' $URL/api/join-pages")"
is "a page turned on while the port is off is saved" 200 "$(code admin PUT /api/join-pages/page "{\"server\":\"$J\",\"enabled\":true}")"
check "  and still nothing listens" bash -c "! docker exec jp bash -c \"ss -ltn | grep -q ':8444 '\""
is "the game server's own port is refused as the join port" 400 "$(code admin PUT /api/settings/join '{"enabled":true,"port":25601}')"
is "  and the panel's" 400 "$(code admin PUT /api/settings/join '{"enabled":true,"port":8080}')"

echo "--- turned on"
api admin PUT /api/port-forwarding/settings '{"public_address":"play.example.org"}' >/dev/null
R=$(api admin PUT /api/settings/join '{"enabled":true,"port":8444}')
is "the port is on and running" "True True 8444" "$(echo "$R" | json "str(d['listener']['enabled'])+' '+str(d['listener']['running'])+' '+str(d['listener']['serving_port'])")"
check "something listens on 8444" listening jp 8444
is "the admin page gives the public link" "http://play.example.org:8444/join" "$(echo "$R" | json "d['public_url']")"
BODY=$(pub /join/survival)
is "the server's page answers" 200 "$(pcode /join/survival)"
has "  its name, escaped" "Family &lt;b&gt;World&lt;/b&gt;" "$BODY"
lacks "  never as markup" "<b>World</b>" "$BODY"
has "  the address players type, with the port that isn't 25565" 'data-copy="play.example.org:25601"' "$BODY"
has "  version and no mods needed" "Java Edition 1.21.11</b>. No mods are needed." "$BODY"
has "  online, with the player count from the server itself" "2 of 20 playing" "$BODY"
lacks "  but not who is playing" "Steve" "$BODY"
for secret in "$J" jp /opt/ 172.30.87 25611 "$(dx jp "dpkg-query -W -f '\${Version}' redstonepanel")" admin alex; do
    lacks "  nothing about the machine: $secret" "$secret" "$BODY"
done
is "the other servers' pages are still off" "404 404" "$(pcode /join/pack) $(pcode /join/bedrock)"
is "there is no list of pages unless asked for" 404 "$(pcode /join)"
is "the status that refreshes" "online 2 20 None" "$(pub /join/survival/status.json | json "d['status']+' '+str(d['players']['online'])+' '+str(d['players']['max'])+' '+str(d['players']['names'])")"
page "{\"server\":\"$J\",\"enabled\":true,\"show_players\":true,\"note\":\"Be kind. <script>x()</script>\",\"link_url\":\"https://discord.gg/abc\",\"link_label\":\"Our Discord\"}" >/dev/null
BODY=$(pub /join/survival)
has "with names turned on, the page says who is playing" "Playing now: Steve, Alex" "$BODY"
has "  the note, escaped" "Be kind. &lt;script&gt;x()&lt;/script&gt;" "$BODY"
has "  and the admin's link" 'href="https://discord.gg/abc"' "$BODY"
is "a link that isn't a web link is refused" 400 "$(code admin PUT /api/join-pages/page "{\"server\":\"$J\",\"enabled\":true,\"link_url\":\"javascript:alert(1)\"}")"

echo "--- the public port answers nothing but the join pages"
PATHS=$(api admin GET /openapi.json | json "' '.join(sorted(p.replace('{server_name}', '$J').replace('{', '').replace('}', '') for p in d['paths']))")
N=0; LEAK=""
for p in $PATHS / /static/index.html /static/login.html /static/js/app.js /static/css/tailwind.css /static/join/join.css /docs /redoc /openapi.json \
         /join/../api/health /join/survival/../../api/health /join/assets/../../static/index.html /join/assets/app.js /join/%2e%2e/api/health \
         /join/survival/status.json/x /join/nope /favicon.ico /robots.txt /.env /join/assets/ ; do
    N=$((N + 1))
    c=$(dx jp "curl -s --path-as-is -m 10 -o /dev/null -w '%{http_code}' '$PUB$p'")
    [ "$c" = 404 ] || LEAK="$LEAK $p=$c"
done
[ -z "$LEAK" ] && [ "$N" -gt 150 ] && ok "all $N panel routes and files tried on the public port: Not found" || bad "the public port answered:$LEAK (tried $N)"
LEAK=""
for m in POST PUT DELETE PATCH OPTIONS; do
    for p in /join/survival /api/auth/login /join/survival/status.json; do
        c=$(pcode "$p" $m); [ "$c" = 404 ] || LEAK="$LEAK $m$p=$c"
    done
done
[ -z "$LEAK" ] && ok "nothing but GET and HEAD is answered" || bad "answered:$LEAK"
HEAD=$(dx jp "curl -s -m 10 -D - -o /dev/null $PUB/join/survival" | tr -d '\r' | tr 'A-Z' 'a-z')
lacks "no cookie is set" "set-cookie" "$HEAD"
lacks "the web server doesn't name itself" "server:" "$HEAD"
has "the page may load only its own files" "content-security-policy: default-src 'none'" "$HEAD"
has "  and can't be framed" "x-frame-options: deny" "$HEAD"
is "the page's stylesheet and script are served" "200 200" "$(pcode /join/assets/join.css) $(pcode /join/assets/join.js)"
check "the page has no inline script" bash -c "[ \"\$(docker exec jp curl -s $PUB/join/survival | grep -o '<script' | wc -l)\" = 1 ]"
is "the panel's own port has no join page" 404 "$(dx jp "curl -s -o /dev/null -w '%{http_code}' $URL/join/survival")"

echo "--- a modpack server, a Bedrock server"
page "{\"server\":\"$M\",\"enabled\":true}" >/dev/null
BODY=$(pub /join/pack)
has "the modpack page links the exact pack version on Modrinth" 'href="https://modrinth.com/modpack/cobblemon-fabric/version/AbC123xy"' "$BODY"
has "  opens it in Modrinth App" 'href="modrinth://modpack/cobblemon-fabric"' "$BODY"
has "  names the loader and version" "Java Edition 1.21.1</b> with <b>Fabric</b>" "$BODY"
has "  and has steps for Prism Launcher" "With Prism Launcher" "$BODY"
page "{\"server\":\"$B\",\"enabled\":true,\"address\":\"bedrock.example.org:19150\"}" >/dev/null
BODY=$(pub /join/bedrock)
has "the Bedrock page: an address of its own, and the outside port typed with it" 'data-copy="bedrock.example.org"' "$BODY"
has "  the port apart, as Bedrock asks for it" 'data-copy="19150"' "$BODY"
has "  an Add server button" "minecraft://?addExternalServer=Pocket%20world%7Cbedrock.example.org%3A19150" "$BODY"
lacks "  no Java instructions" "Java Edition" "$BODY"

echo "--- unlisted pages and the list"
is "two pages can't share a link name" 400 "$(code admin PUT /api/join-pages/page "{\"server\":\"$M\",\"enabled\":true,\"slug\":\"survival\"}")"
T=$(page "{\"server\":\"$M\",\"enabled\":true,\"unlisted\":true}" | json "d['path']")
[ "${#T}" -ge 16 ] && ok "an unlisted page gets a random link" || bad "random link: $T"
is "  it answers there and not at its name" "200 404" "$(pcode "/join/$T") $(pcode /join/pack)"
api admin PUT /api/settings/join '{"enabled":true,"port":8444,"index":true}' >/dev/null
BODY=$(pub /join)
has "the list shows the public pages" 'href="/join/survival"' "$BODY"
has "  the Bedrock one too" 'href="/join/bedrock"' "$BODY"
lacks "  never the unlisted one" "Cobblemon" "$BODY"

echo "--- an address that asks too much is slowed down"
RATE=$(dx jp "for i in \$(seq 1 260); do curl -sk -m 10 -o /dev/null -w '%{http_code}\n' $PUB/join/survival; done | sort | uniq -c | tr -s ' \n' ' '")
has "past 240 requests in a minute the public port answers 429" " 429" "$RATE"
is "  the panel itself still answers" 200 "$(code admin GET /api/join-pages)"
# The count is kept in memory, and a fast machine gets here inside the same minute as the
# sweep above: start the panel again so the checks below aren't the ones slowed down.
dx jp "systemctl restart redstonepanel"
for _ in $(seq 1 40); do dx jp "curl -sf $URL/api/health" >/dev/null 2>&1 && listening jp 8444 && break; sleep 1; done
is "  after a restart the page answers again" 200 "$(pcode /join/survival)"

echo "--- it follows the server and the panel"
dx jp "systemctl stop $J"
for _ in $(seq 1 45); do [ "$(pub /join/survival/status.json | json "d['status']")" = offline ] && break; sleep 1; done
is "a stopped server shows as offline, with no player count" "offline None" "$(pub /join/survival/status.json | json "d['status']+' '+str(d['players'])")"
dx jp "systemctl start $J"
dx jp "systemctl restart redstonepanel"
for _ in $(seq 1 40); do dx jp "curl -sf $URL/api/health" >/dev/null 2>&1 && break; sleep 1; done
for _ in $(seq 1 20); do listening jp 8444 && break; sleep 1; done
is "after the panel restarts the pages are back" 200 "$(pcode /join/survival)"
api admin PUT /api/settings/join '{"enabled":true,"port":8555,"index":true}' >/dev/null
is "moving the port: the new one answers, the old one is closed" "200 000" "$(dx jp "curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8555/join/survival") $(pcode /join/survival)"
api admin PUT /api/settings/join '{"enabled":false,"port":8555}' >/dev/null
check "turned off: nothing listens" bash -c "! docker exec jp bash -c \"ss -ltn | grep -Eq ':(8444|8555) '\""
check "the activity log says who turned it on and off" dx jp "grep -q '\[JOIN\] admin turned the join pages off' /var/log/redstonepanel/panel.log"

echo "--- a cluster: the master serves the page of a server on a node"
P=jpm; URL=https://127.0.0.1:8443; PUB=https://127.0.0.1:8444
dx jpm "echo '$PW_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
dx jpm "redstonepanel set-name Main && redstonepanel cluster create Home" >/dev/null
for _ in $(seq 1 60); do dx jpm "curl -skf $URL/api/health" >/dev/null 2>&1 && break; sleep 1; done
JOIN=$(dx jpm "redstonepanel cluster join-code" | grep '^rpjoin1:')
OUT=$(dx jpn "redstonepanel set-name Basement && redstonepanel join '$JOIN'" 2>&1)
echo "$OUT" | grep -q "Joined cluster 'Home'" && ok "jpn joined as Basement" || { bad "jpn joined"; echo "$OUT"; }
for _ in $(seq 1 60); do dx jpn "curl -skf https://127.0.0.1:8443/api/health" >/dev/null 2>&1 && break; sleep 1; done
standin_tools jpn
standin jpn minecraft-skyblock java 25601 "printf '{\"loader\": \"purpur\", \"minecraft\": \"1.21.11\"}' > .redstonepanel-server.json" 'Skyblock'
sleep 2
login admin "$PW_ADMIN"
N1=$(api admin GET /api/cluster/overview | json "[n['id'] for n in d['nodes'] if n['name']=='Basement'][0]")
for _ in $(seq 1 30); do [ "$(api admin GET /api/join-pages | json "sum(len(m['servers']) for m in d['members'])")" = 1 ] && break; sleep 2; done
V=$(api admin GET /api/join-pages)
is "the master's admin page lists the node's server" "Basement minecraft-skyblock" "$(echo "$V" | json "[m['name']+' '+m['servers'][0]['name'] for m in d['members'] if m['servers']][0]")"
api admin PUT /api/port-forwarding/settings '{"public_address":"203.0.113.7"}' >/dev/null
api admin PUT /api/settings/join '{"enabled":true,"port":8444}' >/dev/null
is "its page is made on the master" 200 "$(code admin PUT /api/join-pages/page "{\"member\":\"$N1\",\"server\":\"minecraft-skyblock\",\"enabled\":true,\"show_players\":true}")"
is "a member that doesn't exist is refused" 404 "$(code admin PUT /api/join-pages/page '{"member":"n-ffffffff","server":"minecraft-skyblock","enabled":true}')"
BODY=$(pub /join/skyblock)
has "the master serves it over HTTPS, with what the node says about the server" "Skyblock" "$BODY"
has "  the node's players" "Playing now: Steve, Alex" "$BODY"
has "  the typed address and the node's game port" 'data-copy="203.0.113.7:25601"' "$BODY"
for secret in Basement jpn jpm 172.30.87 "$N1" minecraft-skyblock; do lacks "  nothing about the cluster: $secret" "$secret" "$BODY"; done
check "the node opened nothing for it" bash -c "! docker exec jpn bash -c \"ss -ltn | grep -q ':8444 '\""
is "the master can't turn a node's own join pages on" 403 "$(code admin PUT "/api/cluster/nodes/$N1/proxy/api/settings/join" '{"enabled":true,"port":8444}')"
is "  or read them" 403 "$(code admin GET "/api/cluster/nodes/$N1/proxy/api/join-pages")"
is "plain HTTP on the public port gets no page" 000 "$(dx jpm "curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8444/join/skyblock")"
dx jpn "systemctl stop redstonepanel"
for _ in $(seq 1 45); do [ "$(pub /join/skyblock/status.json | json "d['status']" 2>/dev/null)" = offline ] && break; sleep 1; done
is "with the node unreachable the page stays up and says offline" "200 offline" "$(pcode /join/skyblock) $(pub /join/skyblock/status.json | json "d['status']")"

echo "--- behind HAProxy (1.21.8)"
dx jpn "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends haproxy >/tmp/haproxy-install.log 2>&1" \
    || { bad "haproxy installed"; dx jpn "tail -5 /tmp/haproxy-install.log"; }
PROXY_IP=$(dx jpn "hostname -I | awk '{print \$1}'")
MASTER_IP=$(dx jpm "hostname -I | awk '{print \$1}'")
# HAProxy runs on exactly the lines the panel offers to copy, around a plain frontend
is "a proxy and an address for players are saved" 200 "$(code admin PUT /api/settings/join "{\"enabled\":true,\"port\":8444,\"proxies\":\"$PROXY_IP\",\"url\":\"play.example.org\"}")"
V=$(api admin GET /api/join-pages)
is "  the panel's links use that address" "https://play.example.org/join" "$(echo "$V" | json "d['public_url']")"
is "  the proxy is stored" "$PROXY_IP" "$(echo "$V" | json "','.join(d['proxy']['addresses'])")"
has "  the HAProxy lines to copy point at this panel over HTTPS" ":8444 check ssl verify none" "$(echo "$V" | json "d['proxy']['haproxy']")"
has "  and there are Cloudflare Tunnel lines too" "service: https://localhost:8444" "$(echo "$V" | json "d['proxy']['cloudflared']")"
echo "$V" | json "d['proxy']['haproxy']" | sed 's/server panel 127.0.0.1:/server panel jpm:/' > /tmp/rp-haproxy-lines
docker cp /tmp/rp-haproxy-lines jpn:/tmp/lines >/dev/null
dx jpn "{ printf 'global\n    log stdout format raw local0\ndefaults\n    mode http\n    timeout connect 5s\n    timeout client 30s\n    timeout server 30s\nfrontend web\n    bind :80\n'; cat /tmp/lines; } > /etc/haproxy/haproxy.cfg"
check "HAProxy accepts the lines the panel offers" dx jpn "haproxy -c -f /etc/haproxy/haproxy.cfg"
dx jpn "systemctl restart haproxy"
for _ in $(seq 1 20); do [ "$(dx jpn "curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1/join/skyblock")" = 200 ] && break; sleep 1; done
via()   { dx jpn "curl -s -m 10 -o /dev/null -w '%{http_code}' ${2:+-H 'X-Forwarded-For: $2'} http://127.0.0.1$1"; }
burst() { dx "$1" "for i in \$(seq 1 250); do curl -s${3:+k} -m 10 -o /dev/null -w '%{http_code}\\n' ${4:+-H 'X-Forwarded-For: $4'} $2; done | sort | uniq -c | tr -s ' \\n' ' '"; }
is "the page loads through HAProxy, past its health check" 200 "$(via /join/skyblock)"
is "  and its stylesheet" 200 "$(via /join/assets/join.css)"
check "HAProxy's other paths don't reach the panel" bash -c "[ \"\$(docker exec jpn curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1/api/join-pages)\" != 200 ]"
is "a proxy of 'everyone' is refused" 400 "$(code admin PUT /api/settings/join '{"enabled":true,"port":8444,"proxies":"0.0.0.0/0"}')"
A=$(burst jpn http://127.0.0.1/join/skyblock)
has "one player who asks too much through HAProxy is slowed down" " 429" "$A"
is "  another player through the same HAProxy is not" 200 "$(dx jpm "curl -s -m 10 -o /dev/null -w '%{http_code}' http://$PROXY_IP/join/skyblock")"
F=$(burst jp https://$MASTER_IP:8444/join/skyblock k "198.51.100.\$i")
has "a forged X-Forwarded-For from someone else doesn't dodge the limit" " 429" "$F"
is "'only through the proxy' needs the proxy's address" 400 "$(code admin PUT /api/settings/join '{"enabled":true,"port":8444,"proxies":"","proxy_only":true}')"
is "'only through the proxy' is saved" 200 "$(code admin PUT /api/settings/join "{\"enabled\":true,\"port\":8444,\"proxies\":\"$PROXY_IP\",\"proxy_only\":true}")"
is "  then the port refuses anyone else" 404 "$(dx jpm "curl -sk -m 10 -o /dev/null -w '%{http_code}' https://127.0.0.1:8444/join/skyblock")"
is "  and still answers through HAProxy" 200 "$(dx jpm "curl -s -m 10 -o /dev/null -w '%{http_code}' http://$PROXY_IP/join/skyblock")"

echo
[ "$FAILS" = 0 ] && echo "ALL PASSED" || echo "$FAILS FAILED"
exit "$FAILS"
