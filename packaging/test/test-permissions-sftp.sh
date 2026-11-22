#!/bin/bash
# End-to-end test of per-server permissions and SFTP (1.18) in a throwaway systemd container
# that installs the .deb from dist/ with python3-paramiko. Two stand-in game servers (their
# "java" just sleeps), an admin, two `user` accounts and a Linux account in the sudo group. It
# checks what each account sees and may do through the real API, then the SFTP service with
# OpenSSH's own sftp client: the switch in Panel Settings, password and SSH-key sign-in, one
# server per sign-in, files written as minecraft, symlinks not followed, the shared sign-in
# limit, and that accounts made before 1.18 keep what they could do. Nothing is downloaded but
# packages. Run from the repository root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-permissions-sftp.sh          (KEEP=1 leaves the container running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
C=rp-perm-test
IMG=${IMG:-rp-systemd:24.04}
PW_ADMIN='perm-test-admin-1'             # throwaway accounts inside the test container only
PW_ALEX='perm-test-alex-1'
PW_SAM='perm-test-sam-1'
PW_TUX='perm-test-tux-1'
A=minecraft-alpha
B=minecraft-beta
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
# sftp_as LOGIN PASSWORD 'batch commands' [port] -> sftp's exit status (password sign-in only)
sftp_as() { dx "printf '%s\n' $3 > /tmp/batch; SSHPASS='$2' sshpass -e sftp -q -P ${4:-2022} -o BatchMode=no -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=no -o PreferredAuthentications=password -o NumberOfPasswordPrompts=1 -b /tmp/batch '$1@127.0.0.1' >/tmp/sftp.out 2>&1"; }
listening() { dx "ss -ltn | grep -q ':$1 '"; }
panel_up() { for _ in $(seq 1 40); do dx "curl -sf http://127.0.0.1:8080/api/health" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Container kept: $C (docker rm -f $C)"; return; }
    docker rm -f "$C" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== permissions and SFTP test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f "$C" >/dev/null 2>&1
docker run -d --privileged --cgroupns=private --name "$C" --hostname "$C" -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
sleep 3
dx "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") python3 python3-paramiko openssh-client sshpass iproute2 >/tmp/install.log 2>&1" \
    || { echo "install failed"; dx "tail -20 /tmp/install.log"; exit 1; }
check "panel installed and running" dx "systemctl is-active --quiet redstonepanel"
echo "      paramiko $(dx "python3 -c 'import paramiko; print(paramiko.__version__)'")"

# --- two stand-in servers
dx "mkdir -p /usr/local/lib/rp-fakejava && printf '#!/bin/bash\nexec sleep 86400\n' > /usr/local/lib/rp-fakejava/java && chmod +x /usr/local/lib/rp-fakejava/java
for S in $A $B; do
  mkdir -p /opt/\$S/plugins
  echo \"server-port=\$([ \$S = $A ] && echo 25601 || echo 25602)\" > /opt/\$S/server.properties
  echo \"only \$S\" > /opt/\$S/secret.txt
  chown -R minecraft:minecraft /opt/\$S
  cat > /etc/systemd/system/\$S.service <<UNIT
[Unit]
Description=Minecraft Server (\$S)
[Service]
User=minecraft
Group=minecraft
WorkingDirectory=/opt/\$S
ExecStart=/usr/local/lib/rp-fakejava/java -Xms256M -Xmx512M -jar server.jar nogui
Restart=no
[Install]
WantedBy=multi-user.target
UNIT
done
systemctl daemon-reload && systemctl start $A $B"

# --- accounts made BEFORE 1.18's first start are simulated further down; these are made on 1.18
dx "echo '$PW_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
dx "echo '$PW_ALEX' | redstonepanel add-user alex --role user" >/dev/null
dx "echo '$PW_SAM' | redstonepanel add-user sam --role user" >/dev/null
dx "useradd -m -G sudo tux && echo 'tux:$PW_TUX' | chpasswd"
panel_up
for u in admin:$PW_ADMIN alex:$PW_ALEX sam:$PW_SAM tux:$PW_TUX; do login "${u%%:*}" "${u#*:}" >/dev/null; done

echo "--- per-server permissions"
is "admin sees both servers" 2 "$(api admin GET /api/servers | json 'len(d)')"
is "a Linux sudo account is an admin too" 2 "$(api tux GET /api/servers | json 'len(d)')"
is "a new user account sees no servers" 0 "$(api alex GET /api/servers | json 'len(d)')"
is "  and has no grants" "{}" "$(api alex GET /api/auth/me | json "d['grants']")"
is "  and can't restart one" 403 "$(code alex POST /api/servers/$A/action '{"action":"restart"}')"
is "a user can't set their own grants" 403 "$(code alex PUT /api/users/alex/grants "{\"grants\":{\"$A\":[\"files\"]}}")"
is "admin makes alex a Moderator on $A" 200 "$(code admin PUT /api/users/alex/grants "{\"grants\":{\"$A\":[\"view\",\"console\",\"players\"]}}")"
L=$(api alex GET /api/servers)
is "alex now sees only $A" "['$A']" "$(echo "$L" | json "[s['name'] for s in d]")"
is "  with what a Moderator may do" "['view', 'console', 'players']" "$(echo "$L" | json "d[0]['perms']")"
is "  the dashboard lists only $A" "['$A']" "$(api alex GET /api/host | json "[s['name'] for s in d['servers']]")"
is "  players: allowed" 200 "$(code alex GET /api/servers/$A/whitelist)"
is "  performance (view): allowed" 200 "$(code alex GET /api/servers/$A/perf)"
is "  restart: refused" 403 "$(code alex POST /api/servers/$A/action '{"action":"restart"}')"
api alex POST /api/servers/$A/action '{"action":"restart"}' | grep -q 'Start/stop/restart' && ok "  the refusal says which permission is missing" || bad "  refusal text"
is "  files: refused" 403 "$(code alex GET "/api/servers/$A/files")"
is "  backups: refused" 403 "$(code alex GET /api/servers/$A/backups)"
is "  settings: refused" 403 "$(code alex GET /api/servers/$A/properties)"
is "  the other server: refused" 403 "$(code alex GET /api/servers/$B/perf)"
is "  users, settings, new servers, removed servers: admins only" "403 403 403 403" \
   "$(code alex GET /api/users) $(code alex GET /api/settings) $(code alex POST /api/servers/deploy '{"edition":"java"}') $(code alex GET /api/servers/removed)"
is "admin adds Start/stop/restart and Files" 200 "$(code admin PUT /api/users/alex/grants "{\"grants\":{\"$A\":[\"view\",\"console\",\"players\",\"power\",\"files\"]}}")"
is "  restart: allowed, at once" 200 "$(code alex POST /api/servers/$A/action '{"action":"restart"}')"
is "  files: allowed" 200 "$(code alex GET "/api/servers/$A/files")"
is "  deleting the server: still admins only" 403 "$(code alex DELETE /api/servers/$A)"
is "  restoring a backup: still admins only" 403 "$(code alex POST /api/servers/$A/backups/x.tar.gz/restore)"
LOG=$(api alex GET /api/logs | json "d['content']")
echo "$LOG" | grep -q "$B\|\[AUTH\]" && bad "alex's console shows other servers or accounts: $LOG" || ok "alex's action console has nothing about $B or accounts"
dx "grep -q \"admin set what 'alex' may do: $A: view, console, power, players, files\" /var/log/redstonepanel/panel.log" && ok "the change is in the panel log with who made it" || bad "grant change not logged"
is "grants.json is root-only" 600 "$(dx 'stat -c %a /var/lib/redstonepanel/grants.json')"

echo "--- SFTP: off until turned on"
check "nothing listens on 2022 after install" bash -c "! docker exec $C bash -c \"ss -ltn | grep -q ':2022 '\""
is "the service doesn't exist yet" "" "$(dx 'ls /etc/systemd/system/redstonepanel-sftp.service 2>/dev/null')"
is "GET /api/sftp says off" False "$(api alex GET /api/sftp | json "d['enabled']")"
is "a user can't turn it on" 403 "$(code alex PUT /api/settings/sftp '{"enabled":true,"port":2022}')"
is "port 22 and the panel's port are refused" "400 400" "$(code admin PUT /api/settings/sftp '{"enabled":true,"port":22}') $(code admin PUT /api/settings/sftp '{"enabled":true,"port":8080}')"
R=$(api admin PUT /api/settings/sftp '{"enabled":true,"port":2022}')
sleep 3
is "admin turns it on" True "$(echo "$R" | json "d['enabled']")"
check "redstonepanel-sftp.service is active and enabled" dx "systemctl is-active --quiet redstonepanel-sftp && systemctl is-enabled --quiet redstonepanel-sftp"
check "it listens on 2022" listening 2022
ST=$(api alex GET /api/sftp)
is "the Files tab is told it runs, the port and the account" "True 2022 alex" "$(echo "$ST" | json "str(d['running'])+' '+str(d['port'])+' '+d['account']")"
echo "$ST" | json "d['host_key']" | grep -q '^SHA256:' && ok "  and the host key's fingerprint" || bad "  no host key fingerprint: $ST"
is "the host key is root-only" 600 "$(dx 'stat -c %a /var/lib/redstonepanel/sftp_host_key')"
is "saving the other Panel Settings keeps SFTP on" True "$(api admin GET /api/settings | json "json.dumps(d['settings'])" | dx "cat > /tmp/s.json; curl -s -b /tmp/jar-admin -X PUT -H 'Content-Type: application/json' -d @/tmp/s.json http://127.0.0.1:8080/api/settings >/dev/null; curl -s -b /tmp/jar-admin http://127.0.0.1:8080/api/sftp" | json "d['enabled']")"

echo "--- SFTP: signing in and files"
dx "head -c 3000000 /dev/urandom > /tmp/big.jar"
sftp_as alex.$A "$PW_ALEX" "'ls' 'put /tmp/big.jar plugins/big.jar' 'mkdir world2' 'rename secret.txt renamed.txt' 'get plugins/big.jar /tmp/back.jar'"
is "alex signs in as alex.$A with the panel password and moves files" 0 $?
check "  the upload arrived whole" dx "cmp /tmp/big.jar /opt/$A/plugins/big.jar && cmp /tmp/big.jar /tmp/back.jar"
is "  new files and folders belong to minecraft" "minecraft:minecraft minecraft:minecraft" "$(dx "stat -c %U:%G /opt/$A/plugins/big.jar /opt/$A/world2 | tr '\n' ' ' | sed 's/ $//'")"
check "  the rename happened" dx "test -f /opt/$A/renamed.txt && ! test -e /opt/$A/secret.txt"
check "  the game server (as minecraft) can read the upload" dx "runuser -u minecraft -- test -r /opt/$A/plugins/big.jar"
dx "grep -q '\[SFTP\] alex signed in to $A from 127.0.0.1 (password)' /var/log/redstonepanel/panel.log && grep -q '\[SFTP\] alex uploaded $A/plugins/big.jar (3000000 bytes)' /var/log/redstonepanel/panel.log && grep -q '\[SFTP\] alex renamed $A/secret.txt to renamed.txt' /var/log/redstonepanel/panel.log" \
    && ok "  sign-in, upload and rename are in the panel log with alex's name" || bad "  SFTP actions not logged: $(dx 'grep SFTP /var/log/redstonepanel/panel.log | tail -5')"
sftp_as alex.$A "$PW_ALEX" "'ls /../$B' 'get /../$B/secret.txt /tmp/stolen.txt'"
check "  there is nothing above the server folder" dx "! test -e /tmp/stolen.txt"
dx "ln -s /etc /opt/$A/etc-link && ln -s /opt/$B/secret.txt /opt/$A/beta-secret && chown -h minecraft:minecraft /opt/$A/etc-link /opt/$A/beta-secret"
sftp_as alex.$A "$PW_ALEX" "'get etc-link/passwd /tmp/passwd.txt' 'get beta-secret /tmp/beta.txt' 'put /tmp/big.jar etc-link/planted'"
check "  symlinks planted in the folder aren't followed (read or write)" dx "! test -e /tmp/passwd.txt && ! test -e /tmp/beta.txt && ! test -e /etc/planted"
sftp_as alex.$B "$PW_ALEX" "'ls'";              [ $? -ne 0 ] && ok "alex.$B is refused (no Files there)" || bad "alex reached $B"
sftp_as alex "$PW_ALEX" "'ls'";                 [ $? -ne 0 ] && ok "a name without the server is refused" || bad "plain 'alex' signed in"
sftp_as sam.$A "$PW_SAM" "'ls'";                [ $? -ne 0 ] && ok "sam (no grants) is refused" || bad "sam signed in"
dx "SSHPASS='$PW_ALEX' sshpass -e ssh -p 2022 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=no alex.$A@127.0.0.1 id >/tmp/ssh.out 2>&1"
[ $? -ne 0 ] && ! dx "grep -q uid= /tmp/ssh.out" && ok "no shell or commands, only SFTP" || bad "ssh ran a command: $(dx 'cat /tmp/ssh.out')"
sftp_as tux.$B "$PW_TUX" "'put /tmp/big.jar from-tux.jar'"
is "a Linux sudo account signs in with its Linux password (PAM) and reaches any server" 0 $?
check "  its upload is minecraft's too" dx "[ \$(stat -c %U /opt/$B/from-tux.jar) = minecraft ]"

echo "--- SFTP: SSH keys"
dx "rm -f /tmp/key /tmp/key.pub; ssh-keygen -q -t ed25519 -N '' -C laptop -f /tmp/key"
KEYOPTS="-q -P 2022 -i /tmp/key -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PasswordAuthentication=no -o BatchMode=yes"
dx "echo ls > /tmp/batch; sftp $KEYOPTS -b /tmp/batch alex.$A@127.0.0.1 >/dev/null 2>&1"; [ $? -ne 0 ] && ok "a key nobody added is refused" || bad "unknown key accepted"
is "alex adds the public key under Password > SSH keys" 200 "$(dx "curl -s -o /dev/null -w '%{http_code}' -b /tmp/jar-alex -H 'Content-Type: application/json' -d \"{\\\"key\\\": \\\"\$(cat /tmp/key.pub)\\\", \\\"label\\\": \\\"\\\"}\" http://127.0.0.1:8080/api/auth/ssh-keys")"
is "  it's listed by its fingerprint" "$(dx "ssh-keygen -lf /tmp/key.pub | cut -d' ' -f2")" "$(api alex GET /api/auth/ssh-keys | json "d['keys'][0]['fingerprint']")"
dx "echo ls > /tmp/batch; sftp $KEYOPTS -b /tmp/batch alex.$A@127.0.0.1 >/tmp/sftp.out 2>&1"; is "  alex signs in with the key, no password" 0 $?
dx "echo ls > /tmp/batch; sftp $KEYOPTS -b /tmp/batch sam.$A@127.0.0.1 >/dev/null 2>&1"; [ $? -ne 0 ] && ok "  the key isn't a pass for another account" || bad "sam signed in with alex's key"
dx "echo ls > /tmp/batch; sftp $KEYOPTS -b /tmp/batch alex.$B@127.0.0.1 >/dev/null 2>&1"; [ $? -ne 0 ] && ok "  or for another server" || bad "alex's key reached $B"
is "ssh_keys.json is root-only" 600 "$(dx 'stat -c %a /var/lib/redstonepanel/ssh_keys.json')"

echo "--- taking access away"
is "admin takes Files away from alex" 200 "$(code admin PUT /api/users/alex/grants "{\"grants\":{\"$A\":[\"view\",\"console\"]}}")"
sftp_as alex.$A "$PW_ALEX" "'ls'"; [ $? -ne 0 ] && ok "alex's password no longer opens SFTP" || bad "alex still signs in"
dx "echo ls > /tmp/batch; sftp $KEYOPTS -b /tmp/batch alex.$A@127.0.0.1 >/dev/null 2>&1"; [ $? -ne 0 ] && ok "nor does the key" || bad "key still signs in"
is "and the file manager refuses" 403 "$(code alex GET "/api/servers/$A/files")"

echo "--- the sign-in limit is shared"
for _ in 1 2 3 4 5; do sftp_as sam.$A wrong-password "'ls'"; done
login sam "$PW_SAM" | grep -q "Too many failed" && ok "five wrong SFTP passwords lock the browser sign-in too" || bad "browser sign-in not limited after SFTP failures"
dx "systemctl restart redstonepanel"; panel_up
login sam "$PW_SAM" | grep -q "Too many failed" && bad "limit survived a panel restart" || ok "restarting the panel clears it, as before"
SINCE=$(dx "systemctl show -p ActiveEnterTimestampMonotonic --value redstonepanel-sftp")
dx "systemctl restart redstonepanel"; panel_up; sleep 2
is "restarting the panel leaves the SFTP service (and its transfers) alone" "$SINCE" "$(dx "systemctl show -p ActiveEnterTimestampMonotonic --value redstonepanel-sftp")"
for u in admin:$PW_ADMIN alex:$PW_ALEX; do login "${u%%:*}" "${u#*:}" >/dev/null; done

echo "--- changing the port, turning it off"
api admin PUT /api/settings/sftp '{"enabled":true,"port":2200}' >/dev/null; sleep 3
check "moved to port 2200" listening 2200
check "  and gone from 2022" bash -c "! docker exec $C bash -c \"ss -ltn | grep -q ':2022 '\""
sftp_as tux.$A "$PW_TUX" "'ls'" 2200; is "  sign-in works there" 0 $?
is "the host key stayed the same" "$(echo "$ST" | json "d['host_key']")" "$(api admin GET /api/sftp | json "d['host_key']")"
api admin PUT /api/settings/sftp '{"enabled":false,"port":2200}' >/dev/null; sleep 2
check "turned off: nothing listens" bash -c "! docker exec $C bash -c \"ss -ltn | grep -q ':2200 '\""
check "  and the service is gone" dx "! systemctl is-active --quiet redstonepanel-sftp && ! test -e /etc/systemd/system/redstonepanel-sftp.service"

echo "--- updating from before 1.18"
dx "systemctl stop redstonepanel && rm -f /var/lib/redstonepanel/grants.json && systemctl start redstonepanel"; panel_up
login admin "$PW_ADMIN" >/dev/null; login sam "$PW_SAM" >/dev/null
G=$(api sam GET /api/auth/me | json "d['grants']")
is "user accounts from before keep what they could do, on every server" "{'*': ['view', 'console', 'power', 'players', 'settings', 'backups']}" "$G"
is "  sam sees both servers again" 2 "$(api sam GET /api/servers | json 'len(d)')"
is "  can restart one" 200 "$(code sam POST /api/servers/$B/action '{"action":"restart"}')"
is "  files stay admin-only for them, as before" 403 "$(code sam GET "/api/servers/$B/files")"
check "  the panel log says so" dx "grep -q 'Per-server permissions: alex, sam kept what user accounts could' /var/log/redstonepanel/panel.log"
dx "echo '$PW_SAM-2' | redstonepanel add-user newbie --role user" >/dev/null; login newbie "$PW_SAM-2" >/dev/null
is "an account made after the update starts with nothing" 0 "$(api newbie GET /api/servers | json 'len(d)')"

echo "--- without python3-paramiko"
dx "DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq python3-paramiko >/dev/null 2>&1"
R=$(api admin PUT /api/settings/sftp '{"enabled":true,"port":2022}')
echo "$R" | grep -q "sudo apt install python3-paramiko" && ok "turning SFTP on says which package to install" || bad "no install hint: $R"
is "the panel itself runs without it" 200 "$(code admin GET /api/servers)"

echo
[ "$FAILS" = 0 ] && echo "All permissions and SFTP checks passed." || echo "$FAILS check(s) FAILED."
exit "$FAILS"
