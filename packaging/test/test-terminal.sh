#!/bin/bash
# End-to-end test of the terminal (1.21.10) in throwaway systemd containers that install the .deb
# from dist/:
#   1. one panel (tm): off by default; a panel admin logs in as a Linux account and the shell
#      really is that account (uid, sudo refused, nothing left after closing); sudo works for a
#      sudo-group account; root, nologin and unknown accounts are refused; a Linux sign-in opens
#      straight into that account, and asks its password once the sign-in isn't recent; helpers,
#      signed-out cookies and plain HTTP from the network get no ticket; idle close; the join-page
#      port has no terminal; five wrong passwords block; the log has no password in it.
#   2. a cluster (tm master, tn node): no terminal on the node until the node's own admin allows
#      it from the master, then the master's admin logs in to a node account through the master.
# Nothing is downloaded but apt packages. Run from the repository root on a machine with Docker,
# after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-terminal.sh          (KEEP=1 leaves the containers running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
NET=rp-terminal-test
IMG=${IMG:-rp-systemd:24.04}
ALL="tm tn"
PW_ADMIN='term-test-admin-1'             # throwaway accounts inside the test containers only
PW_HELPER='term-test-helper-1'
PW_ALEX='term-test-alex-1'
PW_SAM='term-test-sam-1'
PW_ROOT='term-test-root-1'
PW_BOB='term-test-bob-1'
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$1" bash -c "$2"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
is()   { local name=$1 want=$2 got=$3; [ "$got" = "$want" ] && ok "$name" || bad "$name: wanted $want, got $got"; }
has()  { local name=$1 text=$2 body=$3; case "$body" in *"$text"*) ok "$name";; *) bad "$name: '$text' not there"; echo "$body" | tail -15;; esac; }
lacks(){ local name=$1 text=$2 body=$3; case "$body" in *"$text"*) bad "$name: '$text' is there";; *) ok "$name";; esac; }
URL=http://127.0.0.1:8080
# term CONTAINER URL PANEL_USER PASSWORD [client args and steps...]: the screen, and RC=<exit code>
term() { local c=$1; shift; docker exec "$c" python3 /tmp/terminal-client.py "$@" 2>&1; echo "RC=$?"; }
api()  { dx "$1" "curl -sk -b /tmp/jar-$2 -H 'Origin: $3' -X $4 '$3$5' ${6:+-H 'Content-Type: application/json' -d '$6'}"; }
login(){ dx "$1" "curl -sk -c /tmp/jar-$2 -H 'Content-Type: application/json' -d '{\"username\":\"$2\",\"password\":\"$3\"}' $4/api/auth/login" >/dev/null; }
PROMPT='$ '

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Containers kept: $ALL (docker rm -f $ALL; docker network rm $NET)"; return; }
    docker rm -f $ALL >/dev/null 2>&1
    docker network rm "$NET" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== terminal test with $(basename "$DEB")"
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
    ( dx "$c" "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") python3 iproute2 procps >/tmp/install.log 2>&1" \
        || { echo "install failed on $c"; dx "$c" "tail -20 /tmp/install.log"; } ) &
done
wait
for c in $ALL; do
    check "$c: panel installed and running" dx "$c" "systemctl is-active --quiet redstonepanel"
    check "$c: python3-websockets came with it" dx "$c" "python3 -c 'import websockets'"
    docker cp packaging/test/terminal-client.py "$c:/tmp/terminal-client.py" >/dev/null
done

# Short idle and "recent sign-in" windows for this test (environment, test instances only)
dx tm "mkdir -p /etc/systemd/system/redstonepanel.service.d && printf '[Service]\nEnvironment=RP_TERMINAL_IDLE_SECONDS=25 RP_TERMINAL_RECENT_SECONDS=10\n' > /etc/systemd/system/redstonepanel.service.d/test.conf && systemctl daemon-reload && systemctl restart redstonepanel"
for _ in $(seq 1 30); do dx tm "curl -sf $URL/api/health" >/dev/null 2>&1 && break; sleep 1; done
dx tm "echo '$PW_ADMIN' | redstonepanel add-user admin --role admin && echo '$PW_HELPER' | redstonepanel add-user helper --role user" >/dev/null
dx tm "useradd -m -s /bin/bash alex && echo 'alex:$PW_ALEX' | chpasswd && useradd -m -s /bin/bash -G sudo sam && echo 'sam:$PW_SAM' | chpasswd && echo 'root:$PW_ROOT' | chpasswd"
ALEX_UID=$(dx tm "id -u alex")

echo "--- off until an admin turns it on"
OUT=$(term tm $URL admin "$PW_ADMIN")
has "a ticket is refused while the terminal is off" "TICKET 403" "$OUT"
login tm admin "$PW_ADMIN" $URL
has "  and the Dashboard is told so" '"available":false' "$(api tm admin $URL GET /api/terminal)"
dx tm "redstonepanel terminal on" >/dev/null
has "redstonepanel terminal on: available" '"available":true' "$(api tm admin $URL GET /api/terminal)"

echo "--- a panel admin logs in as a Linux account"
OUT=$(term tm $URL admin "$PW_ADMIN" 'expect:login: ' 'send:alex\r' 'expect:Password: ' "send:$PW_ALEX\r" \
      "expect:$PROMPT" 'send:echo UID=$(id -u) USER=$(id -un); sudo -n true; echo SUDO=$?\r' 'expect:SUDO=' 'send:exit\r' 'closed')
has "the shell runs as alex, not root" "UID=$ALEX_UID USER=alex" "$OUT"
has "  and alex can't sudo" "SUDO=1" "$OUT"
lacks "  the password isn't on the screen" "$PW_ALEX" "$OUT"
sleep 2
is "nothing of alex's is left running" "" "$(dx tm "pgrep -u alex" || true)"
check "  and the system logged the login session (PAM, in the journal)" dx tm "journalctl -q --no-pager | grep -q 'session opened for user alex'"

OUT=$(term tm $URL admin "$PW_ADMIN" 'expect:login: ' 'send:sam\r' 'expect:Password: ' "send:$PW_SAM\r" \
      "expect:$PROMPT" "send:echo SUDO_UID_IS=\$(echo '$PW_SAM' | sudo -S -p '' id -u)\\r" 'expect:SUDO_UID_IS=' 'send:exit\r' 'closed')
has "a sudo-group account can sudo (uid 0 after its password)" "SUDO_UID_IS=0" "$OUT"

echo "--- refused accounts"
OUT=$(term tm $URL admin "$PW_ADMIN" 'expect:login: ' 'send:root\r' 'expect:Password: ' "send:$PW_ROOT\r" "expect:root can't sign in here")
has "root is refused by default" "root can't sign in here" "$OUT"
OUT=$(term tm $URL admin "$PW_ADMIN" 'expect:login: ' 'send:minecraft\r' 'expect:Password: ' 'send:x\r' 'expect:Login incorrect')
has "the minecraft account (nologin) can't log in" "Login incorrect" "$OUT"
OUT=$(term tm $URL admin "$PW_ADMIN" 'expect:login: ' 'send:alex\r' 'expect:Password: ' 'send:wrong\r' 'expect:Login incorrect')
has "a wrong password is refused" "Login incorrect" "$OUT"

echo "--- a Linux sign-in opens as that same account"
OUT=$(term tm $URL sam "$PW_SAM" --cookie-out /tmp/sam-cookie "expect:Signed in as sam" "expect:$PROMPT" 'send:id -un\r' 'expect:sam' 'send:exit\r' 'closed')
has "sam (signed in with Linux) gets a shell as sam with no prompt" "Signed in as sam (your Linux account)" "$OUT"
lacks "  no login prompt" "login: " "$OUT"
sleep 12
OUT=$(term tm $URL sam x --cookie-in /tmp/sam-cookie 'expect:Password for sam: ' "send:$PW_SAM\r" "expect:$PROMPT" 'send:exit\r' 'closed')
has "  once the sign-in isn't recent, sam's own password is asked" "Password for sam: " "$OUT"
lacks "  and never another account's name" "login: " "$OUT"

echo "--- who can't"
OUT=$(term tm $URL helper "$PW_HELPER")
has "a helper (user account) gets no ticket" "TICKET 403" "$OUT"
login tm helper "$PW_HELPER" $URL
has "  and no button" '"available":false' "$(api tm helper $URL GET /api/terminal)"
IP=$(dx tm "hostname -I | awk '{print \$1}'")
OUT=$(term tm "http://$IP:8080" admin "$PW_ADMIN")
has "plain HTTP from the network gets no ticket" "needs HTTPS" "$OUT"
dx tm "python3 - <<'EOF'
import http.cookiejar, json, urllib.request
jar = http.cookiejar.CookieJar(); o = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
h = {'Content-Type': 'application/json', 'Origin': '$URL'}
o.open(urllib.request.Request('$URL/api/auth/login', json.dumps({'username': 'admin', 'password': '$PW_ADMIN'}).encode(), h)).read()
c = next(x.value for x in jar if x.name == 'rp_session'); open('/tmp/admin-cookie', 'w').write(c)
o.open(urllib.request.Request('$URL/api/auth/logout', b'{}', h)).read()
EOF"
OUT=$(term tm $URL admin x --cookie-in /tmp/admin-cookie)
has "a signed-out cookie gets no ticket" "TICKET 403" "$OUT"

echo "--- idle close"
OUT=$(term tm $URL admin "$PW_ADMIN" 'expect:login: ' 'send:alex\r' 'expect:Password: ' "send:$PW_ALEX\r" "expect:$PROMPT" 'closed')
has "closed after no typing (25 s in this test)" "Closed after no typing" "$OUT"
has "  with a warning first" "Closing in a minute" "$OUT"

echo "--- never on the join pages port"
login tm admin "$PW_ADMIN" $URL
api tm admin $URL PUT /api/settings/join '{"enabled":true,"port":8444}' >/dev/null
for _ in $(seq 1 20); do dx tm "ss -ltn | grep -q ':8444 '" && break; sleep 1; done
for path in /api/terminal /api/terminal/ticket /api/terminal/ws; do
    is "join port: $path is not there" 404 "$(dx tm "curl -sk -o /dev/null -w '%{http_code}' http://127.0.0.1:8444$path")"
done
CODE=$(dx tm "curl -sk -o /dev/null -w '%{http_code}' -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' http://127.0.0.1:8444/api/terminal/ws")
[ "$CODE" != 101 ] && ok "  and no websocket opens there ($CODE)" || bad "  a websocket opened on the join port"

echo "--- five wrong passwords block"
term tm $URL admin "$PW_ADMIN" 'expect:login: ' 'send:alex\rbad1\r' 'expect:Login incorrect' 'send:alex\rbad2\r' 'sleep:3' 'send:alex\rbad3\r' 'closed' >/dev/null
OUT=$(term tm $URL admin "$PW_ADMIN" 'expect:login: ' 'send:alex\rbad4\r' 'expect:Login incorrect' 'send:alex\rbad5\r' 'sleep:3' "send:alex\\r$PW_ALEX\\r" 'expect:Too many wrong passwords')
has "the right password is refused after five wrong ones" "Too many wrong passwords" "$OUT"
LOG=$(dx tm "grep TERMINAL /var/log/redstonepanel/panel.log")
has "panel log: opened" "admin opened a shell as alex from 127.0.0.1" "$LOG"
has "panel log: closed, with why" "closed after 0 min (idle)" "$LOG"
has "panel log: failed logins" "Login failed for 'alex' (wrong password)" "$LOG"
has "panel log: the block" "too many wrong passwords" "$LOG"
for pw in "$PW_ALEX" "$PW_SAM" "$PW_ROOT"; do lacks "panel log: no password" "$pw" "$(dx tm "cat /var/log/redstonepanel/panel.log")"; done

echo "--- a cluster: the master opens a terminal on a node that allows it"
dx tm "rm -f /etc/systemd/system/redstonepanel.service.d/test.conf && systemctl daemon-reload"
dx tm "redstonepanel set-name Main && redstonepanel cluster create Home" >/dev/null
for _ in $(seq 1 60); do dx tm "curl -skf https://127.0.0.1:8443/api/health" >/dev/null 2>&1 && break; sleep 1; done
JOIN=$(dx tm "redstonepanel cluster join-code" | grep '^rpjoin1:')
OUT=$(dx tn "redstonepanel set-name Basement && redstonepanel join '$JOIN'" 2>&1)
echo "$OUT" | grep -q "Joined cluster 'Home'" && ok "tn joined as Basement" || { bad "tn joined"; echo "$OUT"; }
for _ in $(seq 1 60); do dx tn "curl -skf https://127.0.0.1:8443/api/health" >/dev/null 2>&1 && break; sleep 1; done
dx tn "useradd -m -s /bin/bash bob && echo 'bob:$PW_BOB' | chpasswd && echo '$PW_ADMIN' | redstonepanel add-user nodeadmin --role admin" >/dev/null
M=https://127.0.0.1:8443
login tm admin "$PW_ADMIN" $M
N1=$(api tm admin $M GET /api/cluster/overview | python3 -c "import json,sys; print([n['id'] for n in json.load(sys.stdin)['nodes'] if n['name']=='Basement'][0])")
OUT=$(term tm $M admin "$PW_ADMIN" --node "$N1")
has "no terminal on the node until it allows one" "doesn't allow a terminal from the master" "$OUT"
dx tn "redstonepanel terminal on" >/dev/null
OUT=$(term tm $M admin "$PW_ADMIN" --node "$N1")
has "  turning it on there isn't enough" "doesn't allow a terminal from the master" "$OUT"
login tn nodeadmin "$PW_ADMIN" $M
has "the node's own admin allows it" '"terminal_from_master":true' "$(api tn nodeadmin $M PUT /api/settings/terminal '{"terminal_from_master":true}')"
OUT=$(term tm $M admin "$PW_ADMIN" --node "$N1" 'expect:RedstonePanel terminal on Basement' 'expect:login: ' 'send:bob\r' 'expect:Password: ' \
      "send:$PW_BOB\\r" "expect:$PROMPT" 'send:echo HOST=$(hostname) WHO=$(id -un)\r' 'expect:WHO=' 'send:exit\r' 'closed')
has "through the master: a shell on the node as bob" "HOST=tn WHO=bob" "$OUT"
OUT=$(term tm $M admin "$PW_ADMIN" --node "$N1" 'expect:login: ' 'send:alex\r' 'expect:Password: ' "send:$PW_ALEX\\r" 'expect:Login incorrect')
has "  the node checks its own accounts (alex is only on the master)" "Login incorrect" "$OUT"
has "node log: who, through the master" "admin (master Main) opened a shell as bob" "$(dx tn "grep TERMINAL /var/log/redstonepanel/panel.log")"
has "master log: the relay" "admin opened a terminal on node Basement" "$(dx tm "grep TERMINAL /var/log/redstonepanel/panel.log")"

echo
[ "$FAILS" = 0 ] && echo "=== all terminal checks passed" || echo "=== $FAILS terminal check(s) FAILED"
exit "$FAILS"
