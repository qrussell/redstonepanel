#!/bin/bash
# End-to-end test of "Found on this computer" (1.21.3) in a throwaway systemd container that
# installs the .deb from dist/: Minecraft servers someone set up outside the panel are found,
# handed to the panel in place (Manage with RedstonePanel) and handed back (Undo).
# The servers are stand-ins: a real OpenJDK from apt runs a twenty-line jar, built here, that
# listens on the port in server.properties as a real server does. One is run by its owner's own
# systemd unit, one sits in a home folder the minecraft account can't enter, and one is started
# from a terminal. Nothing is downloaded but apt packages. Run from the repository root on a
# machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-found.sh          (KEEP=1 leaves the container running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
C=rp-found-test
IMG=rp-systemd:24.04
PASS_ADMIN='found-test-admin-1'          # a throwaway account inside the test container only
OWN=/srv/own/survival
HOME_MC=/home/steve/mc
JAR=paper-1.21.4.jar
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$C" bash -c "$1"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
api()  { dx "curl -s -b /tmp/jar -X $1 'http://127.0.0.1:8080$2' ${3:+-H 'Content-Type: application/json' -d '$3'}"; }
code() { dx "curl -s -o /tmp/last.json -w '%{http_code}' -b /tmp/jar -X $1 'http://127.0.0.1:8080$2' ${3:+-H 'Content-Type: application/json' -d '$3'}"; }
last() { dx "cat /tmp/last.json"; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
json() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
found(){ api GET /api/found-servers; }
waitfor() { local n=$1; shift; for _ in $(seq 1 "$n"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Container kept: $C (docker rm -f $C)"; return; }
    docker rm -f "$C" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== found servers test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f "$C" >/dev/null 2>&1
docker run -d --privileged --cgroupns=private --name "$C" --hostname "$C" -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
sleep 3
dx "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") openjdk-21-jdk-headless python3 >/tmp/install.log 2>&1" \
    || { echo "install failed"; dx "tail -20 /tmp/install.log"; exit 1; }
check "panel installed and running" dx "systemctl is-active --quiet redstonepanel"
dx "echo '$PASS_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
for _ in $(seq 1 30); do dx "curl -sf http://127.0.0.1:8080/api/health" >/dev/null 2>&1 && break; sleep 1; done
dx "curl -s -c /tmp/jar -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PASS_ADMIN\"}' http://127.0.0.1:8080/api/auth/login" >/dev/null

# --- the stand-in server: listens on server-port and writes a log, like the real one
dx "mkdir -p /tmp/standin && cd /tmp/standin && cat > Main.java <<'JAVA'
import java.io.*; import java.net.*; import java.nio.file.*; import java.util.*;
public class Main {
    public static void main(String[] a) throws Exception {
        Properties p = new Properties();
        try (InputStream in = new FileInputStream(\"server.properties\")) { p.load(in); }
        int port = Integer.parseInt(p.getProperty(\"server-port\", \"25565\"));
        Files.createDirectories(Paths.get(\"logs\"));
        Files.write(Paths.get(\"logs/latest.log\"), (\"Done (0.1s)! on \" + port + \" as \" + System.getProperty(\"user.name\") + \"\\n\").getBytes());
        try (ServerSocket s = new ServerSocket(port)) { while (true) { s.accept().close(); } }
    }
}
JAVA
javac Main.java && printf 'Main-Class: Main\n' > m.txt && jar cfm $JAR m.txt Main.class
umask 022
useradd -m steve && chmod 750 /home/steve
for d in $OWN $HOME_MC; do
  mkdir -p \$d/world && cp $JAR \$d/ && echo 'eula=true' > \$d/eula.txt && echo keep > \$d/world/level.dat
done
printf 'server-port=25610\nlevel-name=world\nmotd=Own\n' > $OWN/server.properties
printf 'server-port=25611\nlevel-name=world\n' > $HOME_MC/server.properties
chown -R steve:steve /srv/own /home/steve
for u in own:$OWN home:$HOME_MC; do
cat > /etc/systemd/system/mc-\${u%%:*}.service <<UNIT
[Unit]
Description=Somebody's own Minecraft server
[Service]
User=steve
WorkingDirectory=\${u#*:}
ExecStart=/usr/bin/java -Xms128M -Xmx256M -jar $JAR nogui
SuccessExitStatus=143
[Install]
WantedBy=multi-user.target
UNIT
done
systemctl daemon-reload && systemctl enable -q --now mc-own mc-home" >/tmp/rp-found-setup.log 2>&1 || { bad "setting up the stand-in servers"; tail -20 /tmp/rp-found-setup.log; }
listening() { dx "curl -s -m 2 -o /dev/null telnet://127.0.0.1:$1 </dev/null; ss -ltn | grep -q ':$1 '"; }
check "the stand-in servers run under their own units" waitfor 30 listening 25610
waitfor 30 listening 25611

# 1. Finding them: looking changes nothing
F=$(found)
[ "$(echo "$F" | json "len(d['servers'])")" = 2 ] && ok "both servers are found" || bad "found: $F"
E=$(echo "$F" | json "[(s['edition'], s['port'], s['service'], s['user'], s['heap'], s['jar'], s['running']) for s in d['servers'] if s['folder']=='$OWN'][0]")
[ "$E" = "('Java', 25610, 'mc-own.service', 'steve', '256M', '$JAR', True)" ] && ok "  with folder, port, service, account, memory and jar" || bad "  entry: $E"
[ "$(api GET /api/servers | json "len(d if isinstance(d, list) else d.get('servers', []))")" = 0 ] && ok "  they are not on the panel's own list" || bad "  servers list not empty"
check "  nothing was changed by looking" dx "systemctl is-active -q mc-own && systemctl is-enabled -q mc-own && [ \$(stat -c %U $OWN) = steve ] && [ -f $OWN/$JAR ]"

# 2. Refusals, each with a reason and nothing changed
[ "$(code POST /api/found-servers/manage '{"folder":"/srv/nowhere"}')" = 400 ] && ok "a folder that isn't a found server is refused" || bad "unknown folder: $(last)"
[ "$(code POST /api/found-servers/manage "{\"folder\":\"$HOME_MC\"}")" = 400 ] && last | grep -q "can't get into" \
    && ok "a server in a closed home folder is refused, saying why" || bad "home folder: $(last)"
check "  and it keeps running under its own unit" dx "systemctl is-active -q mc-home && systemctl is-enabled -q mc-home && [ \$(stat -c %U $HOME_MC) = steve ]"

# 3. Manage with RedstonePanel: in place
[ "$(code POST /api/found-servers/manage "{\"folder\":\"$OWN\"}")" = 200 ] && ok "Manage with RedstonePanel works" || bad "manage: $(last)"
N=$(last | json "d['name']")
echo "      the panel calls it: $N"
check "  the old unit is stopped and turned off" dx "! systemctl is-active -q mc-own && ! systemctl is-enabled -q mc-own"
check "  ... and not deleted" dx "test -f /etc/systemd/system/mc-own.service"
check "  the panel's unit runs the same folder" dx "systemctl is-active -q $N && [ \"\$(systemctl show -p WorkingDirectory --value $N)\" = $OWN ]"
check "  it listens on its own port again" waitfor 30 listening 25610
check "  as the minecraft account, with the memory it had" dx "pgrep -u minecraft -f -- '-Xmx256M -jar server.jar' >/dev/null && grep -q 'as minecraft' $OWN/logs/latest.log"
check "  the jar has the name the panel uses; nothing was copied" dx "[ -f $OWN/server.jar ] && [ ! -e $OWN/$JAR ] && [ \$(cat $OWN/world/level.dat) = keep ] && [ ! -e /opt/$N ]"
check "  the folder belongs to the minecraft account" dx "[ \$(stat -c %U $OWN/world/level.dat) = minecraft ]"
api GET /api/servers | json "[s['port'] for s in (d if isinstance(d, list) else d['servers']) if s['name']=='$N'][0]" | grep -qx 25610 \
    && ok "  it is on the panel's list with its port" || bad "  not on the list: $(api GET /api/servers)"
F=$(found)
[ "$(echo "$F" | json "[s['folder'] for s in d['servers']]")" = "['$HOME_MC']" ] && ok "  it is no longer among the found ones" || bad "  found after: $F"
[ "$(echo "$F" | json "[(t['name'], t['service']) for t in d['taken_over']]")" = "[('$N', 'mc-own.service')]" ] && ok "  it is listed as taken over" || bad "  taken over: $F"
[ "$(code GET "/api/servers/$N/files?path=")" = 200 ] && last | grep -q server.properties && ok "  the file manager shows its files" || bad "  files: $(last)"
[ "$(code POST "/api/servers/$N/backups" '{}')" = 200 ] && ok "  a backup of it works" || bad "  backup: $(last)"

# 4. Undo: back as it was
[ "$(code POST /api/found-servers/minecraft-other/undo)" = 400 ] && ok "Undo is refused for a server that wasn't taken over" || bad "undo other: $(last)"
[ "$(code POST "/api/found-servers/$N/undo")" = 200 ] && ok "Undo works" || bad "undo: $(last)"
check "  the panel's unit is gone" dx "! test -e /etc/systemd/system/$N.service && ! systemctl is-active -q $N"
check "  the old unit runs and is turned on again" dx "systemctl is-active -q mc-own && systemctl is-enabled -q mc-own"
check "  it listens on its port" waitfor 30 listening 25610
check "  the folder is its owner's again, the jar has its own name" dx "[ \$(stat -c %U:%G $OWN/world/level.dat) = steve:steve ] && [ -f $OWN/$JAR ] && [ ! -e $OWN/server.jar ]"
check "  the world is untouched and nothing of the panel is left in the folder" dx "[ \$(cat $OWN/world/level.dat) = keep ] && ! ls -A $OWN | grep -q redstonepanel"
[ "$(api GET /api/servers | json "len(d if isinstance(d, list) else d.get('servers', []))")" = 0 ] && ok "  it left the panel's list" || bad "  still listed"
[ "$(api GET /api/servers/removed | json "len(d)")" = 0 ] && ok "  and is not a removed server either" || bad "  listed as removed"
F=$(found)
[ "$(echo "$F" | json "sorted(s['folder'] for s in d['servers'])")" = "['$HOME_MC', '$OWN']" ] && [ "$(echo "$F" | json "d['taken_over']")" = "[]" ] \
    && ok "  it is found again, as before" || bad "  found after undo: $F"

# 5. One started from a terminal: the panel won't stop what it can't stop safely
dx "systemctl disable -q --now mc-own; runuser -u steve -- bash -c 'cd $OWN && nohup java -Xmx200M -jar $JAR nogui >/dev/null 2>&1 &'"
waitfor 30 listening 25610
F=$(found)
[ "$(echo "$F" | json "[(s['service'], s['running']) for s in d['servers'] if s['folder']=='$OWN'][0]")" = "('', True)" ] && ok "a server started from a terminal is found" || bad "terminal: $F"
[ "$(code POST /api/found-servers/manage "{\"folder\":\"$OWN\"}")" = 400 ] && last | grep -q "Stop it where you started it" \
    && ok "  while it runs, Manage asks its owner to stop it" || bad "  running terminal server: $(last)"
check "  and leaves it running" listening 25610
dx "pkill -u steve -f -- '-Xmx200M -jar $JAR'; sleep 2"
F=$(found)
[ "$(echo "$F" | json "[s['running'] for s in d['servers'] if s['folder']=='$OWN'][0]")" = False ] && ok "  once stopped it stays listed, as stopped" || bad "  stopped: $F"
[ "$(code POST /api/found-servers/manage "{\"folder\":\"$OWN\"}")" = 200 ] && ok "  then Manage works" || bad "  manage stopped: $(last)"
N2=$(last | json "d['name']")
check "  and the panel runs it" waitfor 30 listening 25610
[ "$(code POST "/api/found-servers/$N2/undo")" = 200 ] && ok "  Undo hands the folder back" || bad "  undo 2: $(last)"
check "  to its owner, with nothing started in its place" dx "[ \$(stat -c %U $OWN) = steve ] && [ -f $OWN/$JAR ] && ! ss -ltn | grep -q ':25610 ' && ! systemctl is-active -q mc-own"
check "the other server was never touched" dx "systemctl is-active -q mc-home && [ \$(stat -c %U $HOME_MC/$JAR) = steve ]"
dx "grep -c '\[FOUND\]' /var/log/redstonepanel/panel.log" | grep -qv '^0$' && ok "the activity log says who handed what over and back" || bad "no [FOUND] lines in the log"

echo
[ "$FAILS" = 0 ] && echo "ALL PASSED" || { echo "$FAILS FAILED"; dx "tail -30 /var/log/redstonepanel/panel.log"; }
exit "$FAILS"
