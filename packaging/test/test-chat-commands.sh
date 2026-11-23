#!/bin/bash
# End-to-end test of Live Chat's command help (1.21.4) in a throwaway systemd container that
# installs the .deb from dist/: the command list and script are served, the panel learns who is
# online and which commands and game rules a running Java server has, a command built in the
# page reaches the server unchanged, and only accounts with "Console & chat" may ask.
# The servers are stand-ins. The Java one is a small Python program at .../rp-fakejava/java that
# speaks RCON and answers `list`, `help` and `help gamerule` with what a real 1.21.11 server
# sends (taken from one), and writes every command it gets to /opt/<name>/rcon.log. The Bedrock
# one sleeps and has a server.log. Nothing is downloaded but apt packages. Run from the
# repository root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-chat-commands.sh          (KEEP=1 leaves the container running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
C=rp-chat-test
IMG=${IMG:-rp-systemd:24.04}
PW_ADMIN='chat-test-admin-1'             # throwaway accounts inside the test container only
PW_ALEX='chat-test-alex-1'
J=minecraft-java
B=minecraft-bedrock
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$C" bash -c "$1"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
# api USER METHOD PATH [JSON] -> body ; code USER METHOD PATH [JSON] -> HTTP status
api()  { dx "curl -s -b /tmp/jar-$1 -X $2 'http://127.0.0.1:8080$3' ${4:+-H 'Content-Type: application/json' -d '$4'}"; }
code() { dx "curl -s -o /dev/null -w '%{http_code}' -b /tmp/jar-$1 -X $2 'http://127.0.0.1:8080$3' ${4:+-H 'Content-Type: application/json' -d '$4'}"; }
is()   { local name=$1 want=$2 got=$3; [ "$got" = "$want" ] && ok "$name" || bad "$name: wanted $want, got $got"; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
json() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
login(){ dx "curl -s -c /tmp/jar-$1 -H 'Content-Type: application/json' -d '{\"username\":\"$1\",\"password\":\"$2\"}' http://127.0.0.1:8080/api/auth/login"; }
sent() { dx "cat /opt/$J/rcon.log 2>/dev/null | tr '\n' ',' | sed 's/,\$//'"; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Container kept: $C (docker rm -f $C)"; return; }
    docker rm -f "$C" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== Live Chat command help test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f "$C" >/dev/null 2>&1
docker run -d --privileged --cgroupns=private --name "$C" --hostname "$C" -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
sleep 3
dx "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") python3 >/tmp/install.log 2>&1" \
    || { echo "install failed"; dx "tail -20 /tmp/install.log"; exit 1; }
check "panel installed and running" dx "systemctl is-active --quiet redstonepanel"

# --- the stand-in Java server: RCON on rcon.port, answers like a real 1.21.11 server
dx "mkdir -p /usr/local/lib/rp-fakejava /usr/local/lib/rp-fakebedrock && cat > /usr/local/lib/rp-fakejava/java <<'PYEOF'
#!/usr/bin/env python3
import socket, struct
props = dict(l.strip().split('=', 1) for l in open('server.properties') if '=' in l)
HELP = ('/advancement (grant|revoke)/clear [<targets>]/defaultgamemode <gamemode>/difficulty [peaceful|easy|normal|hard]'
        '/gamemode <gamemode> [<target>]/gamerule (keep_inventory|minecraft:keep_inventory|advance_time|minecraft:advance_time)'
        '/give <targets> <item> [<count>]/kick <targets> [<reason>]/list [uuids]/minecraft:list [uuids]/spark (profiler|tps|health)'
        '/tp -> teleport/teleport (<location>|<destination>|<targets>)/xp -> experience/stop')
RULES = ''.join('/gamerule %s [<value>]/gamerule minecraft:%s [<value>]' % (r, r) for r in ('spawn_wardens', 'keep_inventory', 'advance_time', 'pvp'))
def answer(cmd):
    open('rcon.log', 'a').write(cmd + chr(10))
    if cmd == 'list': return 'There are 2 of a max of 20 players online: Steve, Alex'
    if cmd == 'help': return HELP
    if cmd == 'help gamerule': return RULES
    if cmd.startswith('gamemode creative'): return 'Set ' + cmd.split()[2] + chr(39) + 's game mode to Creative Mode'
    if cmd.startswith('tellraw'): return ''
    return 'Unknown or incomplete command, see below for error' + cmd + '<--[HERE]'
def read(c, n):
    data = b''
    while len(data) < n:
        chunk = c.recv(n - len(data))
        if not chunk: raise EOFError
        data += chunk
    return data
def send(c, rid, kind, body):
    p = struct.pack('<ii', rid, kind) + body.encode() + bytes(2)
    c.sendall(struct.pack('<i', len(p)) + p)
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', int(props['rcon.port']))); s.listen(5)
while True:
    c, _ = s.accept()
    try:
        while True:
            (n,) = struct.unpack('<i', read(c, 4)); d = read(c, n)
            rid, kind = struct.unpack('<ii', d[:8]); body = d[8:-2].decode()
            if kind == 3: send(c, rid if body == props['rcon.password'] else -1, 2, '')
            else: send(c, rid, 0, answer(body) if body else '')
    except (EOFError, OSError, struct.error): pass
    finally: c.close()
PYEOF
printf '#!/bin/sh\nexec sleep infinity\n' > /usr/local/lib/rp-fakebedrock/bedrock_server
chmod +x /usr/local/lib/rp-fakejava/java /usr/local/lib/rp-fakebedrock/bedrock_server
mkdir -p /opt/$J /opt/$B
printf 'server-port=25601\nenable-rcon=true\nrcon.port=25611\nrcon.password=stand-in-rcon\n' > /opt/$J/server.properties
printf 'server-port=19140\nserver-name=Stand-in\n' > /opt/$B/server.properties
chown -R minecraft:minecraft /opt/$J /opt/$B
unit() { cat > /etc/systemd/system/\$1.service <<UNIT
[Unit]
Description=Minecraft Server (\$1)
[Service]
User=minecraft
Group=minecraft
WorkingDirectory=/opt/\$1
ExecStart=\$2
Restart=no
[Install]
WantedBy=multi-user.target
UNIT
}
unit $J '/usr/local/lib/rp-fakejava/java -Xms256M -Xmx512M -jar server.jar nogui'
unit $B '/usr/local/lib/rp-fakebedrock/bedrock_server'
systemctl daemon-reload && systemctl start $J $B"
sleep 2
check "the stand-in Java server answers RCON" dx "ss -ltn 2>/dev/null | grep -q ':25611 ' || python3 -c 'import socket; socket.create_connection((\"127.0.0.1\", 25611), 2)'"

dx "echo '$PW_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
dx "echo '$PW_ALEX' | redstonepanel add-user alex --role user" >/dev/null
for _ in $(seq 1 40); do dx "curl -sf http://127.0.0.1:8080/api/health" >/dev/null 2>&1 && break; sleep 1; done
login admin "$PW_ADMIN" >/dev/null; login alex "$PW_ALEX" >/dev/null

echo "--- what the page loads"
check "the page links the script with the panel version" dx "curl -sL -b /tmp/jar-admin http://127.0.0.1:8080/ | grep -q '\"/static/js/chat-commands.js?v=[0-9]'"
is "the script is served" 200 "$(code admin GET /static/js/chat-commands.js)"
LIST=$(api admin GET /static/commands.json)
is "the command list is served and has both editions" "True True" \
   "$(echo "$LIST" | json "str(any('java' in c['editions'] for c in d['commands']))+' '+str(any('bedrock' in c['editions'] for c in d['commands']))")"
is "  no one-click command needs anything filled in or stops the server" "[]" \
   "$(echo "$LIST" | json "[q['command'] for q in d['quick'] if q['command'].split()[0] in ('stop','kick','ban','kill','clear','op')]")"

echo "--- a running Java server"
A=$(api admin GET /api/servers/$J/chat/assist)
is "edition, running, who is online (from the server)" "java True ['Alex', 'Steve'] rcon" \
   "$(echo "$A" | json "d['edition']+' '+str(d['running'])+' '+str(d['online'])+' '+d['online_source']")"
NAMES=$(echo "$A" | json "' '.join(c['name'] for c in d['commands'])")
is "the server's own commands, mods' too, without namespaced copies" \
   "advancement clear defaultgamemode difficulty gamemode gamerule give kick list spark tp teleport xp stop" "$NAMES"
is "  with the server's usage line" "(profiler|tps|health)" "$(echo "$A" | json "[c['usage'] for c in d['commands'] if c['name']=='spark'][0]")"
is "its game rules, as this version names them" "['advance_time', 'keep_inventory', 'pvp', 'spawn_wardens']" "$(echo "$A" | json "d['gamerules']")"
is "the panel only asked, it changed nothing" "list,help,help gamerule" "$(sent)"
api admin GET /api/servers/$J/chat/assist >/dev/null
is "asking again reads who is online, not the command list" "list,help,help gamerule,list" "$(sent)"
R=$(api admin POST /api/servers/$J/chat '{"message":"/gamemode creative Steve"}')
is "a built command reaches the server as typed" "gamemode creative Steve" "$(dx "tail -1 /opt/$J/rcon.log")"
is "  and its answer comes back" "success Set Steve's game mode to Creative Mode" "$(echo "$R" | json "d['status']+' '+d['response']")"
dx "systemctl restart $J"; sleep 2
api admin GET /api/servers/$J/chat/assist >/dev/null
is "after the server restarts its commands are asked again" "list,help,help gamerule" "$(dx "tail -3 /opt/$J/rcon.log | tr '\n' ',' | sed 's/,\$//'")"
dx "systemctl stop $J"
S=$(api admin GET /api/servers/$J/chat/assist)
is "a stopped server: nobody online, no commands, still answers" "False [] []" "$(echo "$S" | json "str(d['running'])+' '+str(d['online'])+' '+str(d['commands'])")"
dx "systemctl start $J"; sleep 2

echo "--- Bedrock"
dx "printf '[%s:000 INFO] Player connected: Sam Carter, xuid: 2535400000000001\n' \"\$(date '+%Y-%m-%d %H:%M:%S')\" >> /opt/$B/server.log; chown minecraft:minecraft /opt/$B/server.log"
for _ in $(seq 1 50); do [ "$(api admin GET /api/servers/$B/chat/assist | json "len(d['online'])")" = 1 ] && break; sleep 1; done
S=$(api admin GET /api/servers/$B/chat/assist)
is "edition and who is online, from the player log (names may have spaces)" "bedrock ['Sam Carter'] log" \
   "$(echo "$S" | json "d['edition']+' '+str(d['online'])+' '+d['online_source']")"
is "  no command list (Bedrock doesn't answer), the page's own list is used" "[] []" "$(echo "$S" | json "str(d['commands'])+' '+str(d['gamerules'])")"
dx "printf '[%s:000 INFO] Player disconnected: Sam Carter, xuid: 2535400000000001\n' \"\$(date '+%Y-%m-%d %H:%M:%S')\" >> /opt/$B/server.log"
for _ in $(seq 1 50); do [ "$(api admin GET /api/servers/$B/chat/assist | json "len(d['online'])")" = 0 ] && break; sleep 1; done
is "after leaving, the player is offered as seen before" "[] ['Sam Carter']" "$(api admin GET /api/servers/$B/chat/assist | json "str(d['online'])+' '+str(d['known'])")"

echo "--- permissions"
is "an account with no servers is refused" 403 "$(code alex GET /api/servers/$J/chat/assist)"
code admin PUT /api/users/alex/grants "{\"grants\":{\"$J\":[\"view\",\"players\"]}}" >/dev/null
is "View and Players aren't enough" 403 "$(code alex GET /api/servers/$J/chat/assist)"
code admin PUT /api/users/alex/grants "{\"grants\":{\"$J\":[\"view\",\"console\"]}}" >/dev/null
is "Console & chat is" 200 "$(code alex GET /api/servers/$J/chat/assist)"
is "  on that server only" 403 "$(code alex GET /api/servers/$B/chat/assist)"
is "signed out: refused" 401 "$(dx "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/servers/$J/chat/assist")"

echo
[ "$FAILS" = 0 ] && echo "ALL PASSED" || echo "$FAILS FAILED"
exit "$FAILS"
