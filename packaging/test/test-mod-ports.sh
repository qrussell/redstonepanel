#!/bin/bash
# End-to-end test of the ports that mods and plugins open (core/modports.py, 1.21.9), in a
# throwaway systemd container with ufw that installs the .deb from dist/. Two stand-in Java
# servers run Simple Voice Chat on the same UDP port, like a real Fabric server (mod in
# config/voicechat/) and a real Paper server (plugin in plugins/voicechat/): their "java" opens the
# voice port from that config and, as the Fabric mod does, exits 1 with the real log lines when the
# port is taken. Checks: a start from the panel moves the second server to the next free port; a
# start outside the panel crash-loops once, the crash hook moves the port before systemd's restart
# and the Last crash card names the voice port instead of the game port; ufw and Port forwarding
# get the voice ports; a busy port is skipped. Nothing is downloaded. Run from the repository
# root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-mod-ports.sh          (KEEP=1 leaves the container running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
C=rp-modports-test
IMG=rp-systemd:24.04
PASS_ADMIN='modports-test-admin-1'       # a throwaway account inside the test container only
A=minecraft-voice-a                      # "Fabric": config/voicechat/
B=minecraft-voice-b                      # "Paper": plugins/voicechat/
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
skip() { echo "SKIP  $1"; }
dx()   { docker exec "$C" bash -c "$1"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
api()  { dx "curl -s -b /tmp/jar -X $1 'http://127.0.0.1:8080$2' ${3:+-H 'Content-Type: application/json' -d '$3'}"; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
json() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
voice(){ dx "sed -n 's/^port=//p' /opt/$1/$2/voicechat/voicechat-server.properties"; }
rules(){ dx "ufw show added" | sed -n 's/^ufw allow //p' | sort | tr '\n' ' '; }
has()  { rules | tr ' ' '\n' | grep -qx "$1"; }
active(){ dx "systemctl is-active --quiet $1"; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Container kept: $C (docker rm -f $C)"; return; }
    docker rm -f "$C" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== mod and plugin ports test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f "$C" >/dev/null 2>&1
docker run -d --privileged --cgroupns=private --name "$C" --hostname "$C" \
    -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
sleep 3
dx "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") ufw python3 >/tmp/install.log 2>&1" \
    || { echo "install failed"; dx "tail -20 /tmp/install.log"; exit 1; }
check "panel installed and running" dx "systemctl is-active --quiet redstonepanel"
dx "ufw --force enable" >/dev/null 2>&1 && ok "ufw is on" || skip "ufw can't be turned on in this container; checking the rules it holds"
dx "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
for _ in $(seq 1 30); do dx "curl -sf http://127.0.0.1:8080/api/health" >/dev/null 2>&1 && break; sleep 1; done
dx "curl -s -c /tmp/jar -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PASS_ADMIN\"}' http://127.0.0.1:8080/api/auth/login" >/dev/null

# --- the stand-in server: opens TCP server-port and the voice chat UDP port from its config
dx "mkdir -p /usr/local/lib/rp-fakejava && cat > /usr/local/lib/rp-fakejava/voice.py <<'PYEOF'
import glob, os, re, socket, sys, time
log = open('logs/latest.log', 'w', buffering=1)
def say(line):
    log.write(line + '\n'); print(line, flush=True)
game = int(re.search(r'^server-port=(\d+)', open('server.properties').read(), re.M).group(1))
conf = (glob.glob('config/voicechat/voicechat-server.properties') + glob.glob('plugins/voicechat/voicechat-server.properties'))[0]
port = int(re.search(r'^port=(\d+)', open(conf).read(), re.M).group(1))
t = socket.socket(); t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); t.bind(('0.0.0.0', game)); t.listen()
say(f'[05:08:48] [Server thread/INFO]: Starting Minecraft server on *:{game}')
say('[05:08:49] [Server thread/INFO]: Done (0.583s)! For help, type \"help\"')
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    u.bind(('0.0.0.0', port))
except OSError:
    say(f'[05:08:49] [VoiceChatServerThread/ERROR]: [voicechat] Failed to run voice chat at UDP port {port}, make sure no other application is running at that port')
    say('[05:08:49] [VoiceChatServerThread/ERROR]: [voicechat] Voice chat server error')
    say('java.net.BindException: Address already in use')
    say('\tat knot//de.maxhenkel.voicechat.voice.server.Server.run(Server.java:125)')
    say('[05:08:49] [VoiceChatServerThread/ERROR]: [voicechat] Shutting down server')
    say('[05:08:49] [Server thread/INFO]: Stopping server')
    sys.exit(1)
say(f'[05:08:49] [VoiceChatServerThread/INFO]: [voicechat] Voice chat server started at port {port}')
while True:
    time.sleep(3600)
PYEOF
printf '#!/bin/bash\nexec python3 /usr/local/lib/rp-fakejava/voice.py\n' > /usr/local/lib/rp-fakejava/java
chmod +x /usr/local/lib/rp-fakejava/java"
make_server() {  # name, game port, voice folder (config|plugins)
    dx "mkdir -p /opt/$1/logs /opt/$1/$3/voicechat && cd /opt/$1 && printf 'server-port=$2\nenable-rcon=false\n' > server.properties \
        && printf '# Simple Voice Chat server config\nport=24454\nbind_address=\nmax_voice_distance=48.0\n' > $3/voicechat/voicechat-server.properties \
        && chown -R minecraft:minecraft /opt/$1 && cat > /etc/systemd/system/$1.service <<UNIT
[Unit]
Description=Minecraft Server ($1)
[Service]
User=minecraft
Group=minecraft
WorkingDirectory=/opt/$1
ExecStart=/usr/local/lib/rp-fakejava/java -Xms256M -Xmx512M -jar server.jar nogui
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload"
}
dx "systemctl stop redstonepanel"
make_server $A 25601 config
make_server $B 25602 plugins
# as a panel before 1.21.9 left them: the game ports allowed and recorded, the voice ports not
dx "ufw allow 25601/tcp && ufw allow 25602/tcp && printf '{\"$A\": [[\"25601\", \"tcp\"]], \"$B\": [[\"25602\", \"tcp\"]]}' > /var/lib/redstonepanel/firewall-rules.json" >/dev/null
dx "systemctl start redstonepanel"
for _ in $(seq 1 30); do dx "curl -sf http://127.0.0.1:8080/api/health" >/dev/null 2>&1 && break; sleep 1; done
dx "curl -s -c /tmp/jar -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PASS_ADMIN\"}' http://127.0.0.1:8080/api/auth/login" >/dev/null
dx "systemctl start $A"; sleep 2
check "server A runs with voice chat on 24454" active $A
dx "ss -ulpn | grep -q ':24454 '" && ok "  A holds UDP 24454" || bad "  A doesn't hold 24454"

# 1. Starting B from the panel: its voice port moves first, so it starts
api POST /api/servers/$B/action '{"action":"start"}' >/dev/null
sleep 3
[ "$(voice $B plugins)" = 24455 ] && ok "start from the panel: B's voice port moved to 24455" || bad "B's voice port: $(voice $B plugins)"
[ "$(voice $A config)" = 24454 ] && ok "  A keeps 24454" || bad "  A's port: $(voice $A config)"
check "  B runs" active $B
dx "grep -q 'voice chat server started at port 24455' -i /opt/$B/logs/latest.log" && ok "  B's voice chat opened 24455" || bad "  B log: $(dx "cat /opt/$B/logs/latest.log")"
check "  config still belongs to the game account" dx "test \$(stat -c %U /opt/$B/plugins/voicechat/voicechat-server.properties) = minecraft"
check "  the rest of the file is kept" dx "grep -qx 'max_voice_distance=48.0' /opt/$B/plugins/voicechat/voicechat-server.properties"
dx "grep -q '\[PORTS\] $B: Simple Voice Chat was set to UDP 24454, which $A also use; moved it to 24455' /var/log/redstonepanel/panel.log /var/lib/redstonepanel/*.log 2>/dev/null || journalctl -u redstonepanel | grep -q 'moved it to 24455'" \
    && ok "  the panel log says so" || skip "  panel log line not found where expected"
H=$(api GET /api/host)
echo "$H" | json "[a['text'] for a in d['attention'] if a.get('server')=='$B' and 'moved' in a['text']]" | grep -q '^\[\]$' \
    && ok "  Needs attention: nothing left once B started on the new port" || bad "  attention: $(echo "$H" | json "d['attention']")"
has 24454/udp && has 24455/udp && has 25601/tcp && has 25602/tcp && ok "ufw now allows both voice ports (and still both game ports)" || bad "ufw rules: $(rules)"
P=$(api GET /api/port-forwarding/local)
echo "$P" | grep -q '24455' && ok "Port forwarding lists B's voice port" || bad "port forwarding: $P"

# 2. Outside the panel (systemd's own restart, a reboot): B on 24454 again crash-loops once
dx "systemctl stop $B; sed -i 's/^port=.*/port=24454/' /opt/$B/plugins/voicechat/voicechat-server.properties; rm -f /var/lib/redstonepanel/crashes/$B.json"
dx "systemctl start $B" 2>/dev/null; sleep 3
R=$(dx "cat /var/lib/redstonepanel/crashes/$B.json 2>/dev/null")
[ "$(echo "$R" | json "d['causes'][0]['id']")" = extra_port_in_use ] && ok "crash hook: the cause is the voice port" || bad "crash record: $R"
T=$(echo "$R" | json "d['causes'][0]['text'] + ' | ' + d['causes'][0]['fix']")
echo "$T" | grep -q "UDP 24454" && ok "  names UDP 24454" || bad "  text: $T"
echo "$T" | grep -q "25602" && bad "  blames the game port: $T" || ok "  doesn't blame the game port"
echo "$T" | grep -q "$A on this machine is set to the same voice port" && ok "  names the other server" || bad "  other server: $T"
echo "$T" | grep -q "moved this server's voice port to 24455" && ok "  says the port moved" || bad "  move: $T"
[ "$(voice $B plugins)" = 24455 ] && ok "  hook moved it before the restart" || bad "  port after hook: $(voice $B plugins)"
H=$(api GET /api/host)
echo "$H" | json "[a['text'] for a in d['attention'] if a.get('server')=='$B']" | grep -q "voice port\|Simple Voice Chat" \
    && ok "  Needs attention explains it" || bad "  attention: $(echo "$H" | json "d['attention']")"
for _ in $(seq 1 15); do active $B && break; sleep 1; done
check "  systemd's restart runs on the new port" active $B
dx "grep -qi 'voice chat server started at port 24455' /opt/$B/logs/latest.log" && ok "  B's voice chat opened 24455" || bad "  B log: $(dx "cat /opt/$B/logs/latest.log")"

# 3. A port held by some other program is skipped
dx "systemctl stop $B; sed -i 's/^port=.*/port=24454/' /opt/$B/plugins/voicechat/voicechat-server.properties"
dx "nohup python3 -c \"import socket,time; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind(('0.0.0.0',24455)); time.sleep(600)\" >/dev/null 2>&1 &"
sleep 1
api POST /api/servers/$B/action '{"action":"start"}' >/dev/null
sleep 3
[ "$(voice $B plugins)" = 24456 ] && ok "a busy port (24455) is skipped: B gets 24456" || bad "busy port: $(voice $B plugins)"
check "  B runs" active $B
api GET /api/host >/dev/null                 # the dashboard checks the firewall
has 24456/udp && ok "  ufw allows 24456" || bad "  ufw rules: $(rules)"

echo
[ "$FAILS" = 0 ] && echo "=== all mod port checks passed" || echo "=== $FAILS check(s) failed"
exit $((FAILS > 0))
