#!/bin/bash
# End-to-end test of "why did it crash?" (1.17.1) in a throwaway systemd container that installs
# the .deb from dist/. The game server is a stand-in Fabric server: its "java" is a script that
# prints what a real server prints for each failure (the lines come from tests/crash_samples/,
# which were taken from real servers) and exits the way the real one does. It checks that the
# systemd drop-in's ExecStopPost hook writes the Last crash record as root, that the webhook
# gets the cause, that Needs attention lists it, and that Turn off and restart works.
# Nothing is downloaded. Run from the repository root on a machine with Docker, after building
# the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-crashes.sh          (KEEP=1 leaves the container running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
C=rp-crash-test
IMG=rp-systemd:24.04
PASS_ADMIN='crash-test-admin-1'          # a throwaway account inside the test container only
S=minecraft-crashy
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$C" bash -c "$1"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
api()  { dx "curl -s -b /tmp/jar -X $1 'http://127.0.0.1:8080$2' ${3:+-H 'Content-Type: application/json' -d '$3'}"; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
json() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
rec()  { dx "cat /var/lib/redstonepanel/crashes/$S.json 2>/dev/null"; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Container kept: $C (docker rm -f $C)"; return; }
    docker rm -f "$C" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== crash explanations test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f "$C" >/dev/null 2>&1
docker run -d --privileged --cgroupns=private --name "$C" --hostname "$C" \
    -v "$HOST_PWD/dist:/dist:ro" -v "$HOST_PWD/tests/crash_samples:/samples:ro" "$IMG" >/dev/null
sleep 3
dx "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") zip python3 >/tmp/install.log 2>&1" \
    || { echo "install failed"; dx "tail -20 /tmp/install.log"; exit 1; }
check "panel installed and running" dx "systemctl is-active --quiet redstonepanel"
dx "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
for _ in $(seq 1 30); do dx "curl -sf http://127.0.0.1:8080/api/health" >/dev/null 2>&1 && break; sleep 1; done
dx "curl -s -c /tmp/jar -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PASS_ADMIN\"}' http://127.0.0.1:8080/api/auth/login" >/dev/null

# --- a webhook that records what it gets
dx "cat > /tmp/hook.py <<'PYEOF'
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        with open('/tmp/hook.jsonl', 'ab') as f:
            f.write(body + b'\n')
        self.send_response(204); self.end_headers()
    def log_message(self, *a): pass
http.server.HTTPServer(('127.0.0.1', 9099), H).serve_forever()
PYEOF
nohup python3 /tmp/hook.py >/dev/null 2>&1 &"
sleep 1
api PUT /api/settings/notifications '{"url":"http://127.0.0.1:9099/hook","events":["crashed","stopped"]}' >/dev/null

# --- the stand-in Fabric server: "java" plays the scenario in /opt/S/scenario
dx "mkdir -p /usr/local/lib/rp-fakejava /opt/$S/mods /opt/$S/logs && cat > /usr/local/lib/rp-fakejava/java <<'SH'
#!/bin/bash
cd /opt/minecraft-crashy
case \$(cat scenario) in
  missing)  cp /samples/fabric-missing-dep-latest.log logs/latest.log; exit 1 ;;
  java)     cat /samples/wrong-java-journal.txt; exit 1 ;;
  port)     cp /samples/port-latest.log logs/latest.log; exit 0 ;;
  run)      exec sleep 3600 ;;
esac
SH
chmod +x /usr/local/lib/rp-fakejava/java
cd /tmp && rm -rf modjar && mkdir modjar && cd modjar && printf '{\"schemaVersion\":1,\"id\":\"ledger\",\"version\":\"1.3.7\",\"name\":\"Ledger\",\"depends\":{\"fabric\":\">=0.110.5\",\"fabric-language-kotlin\":\"*\"}}' > fabric.mod.json && zip -q /opt/$S/mods/ledger-1.3.7-fix.jar fabric.mod.json
printf '{\"loader\":\"fabric\",\"minecraft\":\"1.21.4\"}' > /opt/$S/.redstonepanel-server.json
echo 'server-port=25601' > /opt/$S/server.properties
echo run > /opt/$S/scenario
chown -R minecraft:minecraft /opt/$S
cat > /etc/systemd/system/$S.service <<'UNIT'
[Unit]
Description=Minecraft Server (minecraft-crashy)
[Service]
User=minecraft
Group=minecraft
WorkingDirectory=/opt/minecraft-crashy
ExecStart=/usr/local/lib/rp-fakejava/java -Xms256M -Xmx512M -jar server.jar nogui
Restart=no
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload"
scenario() { dx "echo $1 > /opt/$S/scenario; rm -f /var/lib/redstonepanel/crashes/$S.json /var/lib/redstonepanel/notify-state.json /tmp/hook.jsonl; systemctl start $S; sleep 3"; }

# 1. A Fabric mod missing its dependencies: a crash (exit 1), cause from logs/latest.log
scenario missing
R=$(rec)
[ "$(echo "$R" | json "d['causes'][0]['id']")" = missing_dependency ] && ok "missing dependency: recorded by the hook" || bad "missing dependency record: $R"
check "  record is root-only" bash -c "docker exec $C stat -c %a /var/lib/redstonepanel/crashes/$S.json | grep -qx 600"
[ "$(echo "$R" | json "d['causes'][0]['text']")" = "Ledger requires Fabric API and Fabric Language Kotlin, which aren't installed." ] \
    && ok "  cause in plain words" || bad "  cause text: $(echo "$R" | json "d['causes']")"
[ "$(echo "$R" | json "d['suspects'][0]['file']")" = ledger-1.3.7-fix.jar ] && ok "  names the installed mod" || bad "  suspects: $(echo "$R" | json "d['suspects']")"
dx "grep -q 'crashed (exit status 1).*Ledger requires Fabric API' /tmp/hook.jsonl" && ok "  webhook has the cause" || bad "  webhook: $(dx 'cat /tmp/hook.jsonl')"
A=$(api GET /api/host)
echo "$A" | json "[a for a in d['attention'] if a.get('where')=='crash' and a['server']=='$S'][0]['text']" | grep -q "Ledger requires" \
    && ok "  Needs attention lists it" || bad "  attention: $(echo "$A" | json "d['attention']")"
G=$(api GET /api/servers/$S/crash)
[ "$(echo "$G" | json "d['crash']['suspects'][0]['enabled']")" = True ] && ok "  API shows the card" || bad "  API: $G"

# Turn off and restart: the jar is renamed, the server starts again
dx "echo run > /opt/$S/scenario"
T=$(api POST /api/servers/$S/crash/turn-off '{"file":"ledger-1.3.7-fix.jar"}')
sleep 2
check "  Turn off and restart renames the jar" dx "test -f /opt/$S/mods/ledger-1.3.7-fix.jar.disabled -a ! -e /opt/$S/mods/ledger-1.3.7-fix.jar"
check "  and restarts the server" dx "systemctl is-active --quiet $S"
[ "$(api GET /api/servers/$S/crash | json "d['crash']['dismissed']")" = True ] && ok "  card closed after the fix" || bad "  card after fix"
[ "$(dx "curl -s -o /dev/null -w '%{http_code}' -b /tmp/jar -X POST http://127.0.0.1:8080/api/servers/$S/crash/turn-off -H 'Content-Type: application/json' -d '{\"file\":\"../../etc/passwd\"}'")" = 400 ] \
    && ok "  only the suspected file can be turned off" || bad "  other file refused"

# 2. A requested stop is never analysed
dx "rm -f /var/lib/redstonepanel/crashes/$S.json; systemctl stop $S; sleep 1"
check "requested stop: no record" dx "test ! -e /var/lib/redstonepanel/crashes/$S.json"

# 3. The wrong Java: the server never opens its log, the cause comes from the journal
dx "echo 'old run' > /opt/$S/logs/latest.log; touch -d '2 hours ago' /opt/$S/logs/latest.log"
scenario java
R=$(rec)
[ "$(echo "$R" | json "d['source']")" = journal ] && ok "wrong Java: read from this run's journal" || bad "wrong Java source: $R"
echo "$R" | json "d['causes'][0]['text']" | grep -q "needs Java 25 or newer. It was started with Java 21" \
    && ok "  needs Java 25, started with 21" || bad "  wrong Java text: $(echo "$R" | json "d['causes']")"

# 4. Port in use: the server exits 0, nobody asked it to stop: kept, with the port
scenario port
R=$(rec)
[ "$(echo "$R" | json "d['crashed']")" = False ] && [ "$(echo "$R" | json "d['causes'][0]['id']")" = port_in_use ] \
    && ok "port in use: a failed start is kept" || bad "port record: $R"
echo "$R" | json "d['causes'][0]['text']" | grep -q "port 25601" && ok "  names the port" || bad "  port text"
dx "grep -q 'stopped.*already using port 25601' /tmp/hook.jsonl" && ok "  'stopped' webhook has the cause" || bad "  webhook: $(dx 'cat /tmp/hook.jsonl')"

# Dismiss hides it from Needs attention
api DELETE /api/servers/$S/crash >/dev/null
api GET /api/host | json "[a for a in d['attention'] if a.get('where')=='crash']" | grep -q '^\[\]$' \
    && ok "dismissed: gone from Needs attention" || bad "dismiss"

echo
[ "$FAILS" = 0 ] && echo "=== all crash explanation checks passed" || echo "=== $FAILS check(s) failed"
exit $((FAILS > 0))
