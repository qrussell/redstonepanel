#!/bin/bash
# Install/upgrade/remove/purge test for the redstonepanel .deb. Run as root inside a
# disposable systemd container (see Dockerfile). Prints PASS/FAIL per check.
#   test-install.sh [--no-install-recommends]
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
FAILS=0
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
code() { curl -s -o /tmp/body -w '%{http_code}' "$@"; }
wait_up() { for _ in $(seq 1 30); do curl -sfkL -o /dev/null "$1" && return 0; sleep 1; done; return 1; }

B=http://127.0.0.1:8080
J="Content-Type: application/json"
O="Origin: $B"
DEB=$(ls /dist/redstonepanel_*_all.deb)
. /etc/os-release; ARCH=$(dpkg --print-architecture); SYSTEM="$PRETTY_NAME, $ARCH"
echo "=== $SYSTEM, installing $(basename "$DEB") $*"

apt-get update -qq
apt-get install -y -qq "$@" "$DEB" > /tmp/install.log 2>&1 || { tail -30 /tmp/install.log; echo "FAIL  install"; exit 1; }
ok "apt install"
grep -A4 "RedstonePanel is running" /tmp/install.log | head -6
echo "  python3-fastapi $(dpkg-query -W -f='${Version}' python3-fastapi), uvicorn $(dpkg-query -W -f='${Version}' python3-uvicorn), multipart: $(dpkg-query -W -f='${Package} ${Version}' python3-python-multipart python3-multipart 2>/dev/null | tr '\n' ' ')"
echo "  java: $(command -v java || echo 'not installed')"

# --- service, users, files
check "service active"                    systemctl is-active --quiet redstonepanel
check "service enabled"                   systemctl is-enabled --quiet redstonepanel
check "group redstonepanel exists"        getent group redstonepanel
check "user minecraft exists"             id minecraft
check "/var/lib/redstonepanel is 0700"    test "$(stat -c %a /var/lib/redstonepanel)" = 700
check "/var/log/redstonepanel exists"     test -d /var/log/redstonepanel
check "man page in package"               bash -c 'dpkg -L redstonepanel | grep -q man8/redstonepanel.8.gz'
wait_up $B/api/health && ok "API answers" || bad "API answers"
check "local Tailwind CSS served"          test "$(code $B/static/css/tailwind.css)" = 200
check "UI has no CDN references"          bash -c '! grep -rq "cdn.tailwindcss" /usr/share/redstonepanel/static'
check "no login -> 401"                   test "$(code $B/api/servers)" = 401

# --- Linux sign-in through PAM (test accounts exist only in this throwaway container)
useradd -m -s /bin/bash -G sudo rpadmin && echo 'rpadmin:Linux-test-pw-1' | chpasswd
useradd -m -s /bin/bash rpnobody && echo 'rpnobody:Linux-test-pw-2' | chpasswd
L=/tmp/linux.cookies
check "Linux sudo user signs in (PAM)"    test "$(code -c $L -X POST $B/api/auth/login -H "$J" -H "$O" -d '{"username":"rpadmin","password":"Linux-test-pw-1"}')" = 200
grep -q '"source":"linux"' /tmp/body && ok "  ...as a Linux admin" || bad "  ...as a Linux admin ($(cat /tmp/body))"
check "wrong Linux password -> 401"       test "$(code -X POST $B/api/auth/login -H "$J" -H "$O" -d '{"username":"rpadmin","password":"nope-nope"}')" = 401
check "user outside sudo group -> 401"    test "$(code -X POST $B/api/auth/login -H "$J" -H "$O" -d '{"username":"rpnobody","password":"Linux-test-pw-2"}')" = 401
for ep in servers templates users settings logs auth/me; do
    check "GET /api/$ep as admin"         test "$(code -b $L $B/api/$ep)" = 200
done

# --- CLI
echo 'Panel-test-pw-1' | redstonepanel add-user webadmin >/dev/null && ok "redstonepanel add-user" || bad "redstonepanel add-user"
check "panel user signs in"               test "$(code -X POST $B/api/auth/login -H "$J" -H "$O" -d '{"username":"webadmin","password":"Panel-test-pw-1"}')" = 200
redstonepanel status | sed 's/^/  /'
check "users.json is 0600"                test "$(stat -c %a /var/lib/redstonepanel/users.json)" = 600

# --- panel name: host name by default, public (sign-in page), settable by CLI and API
check "panel name defaults to host name"  bash -c "curl -s $B/api/panel-info | grep -q '\"name\":\"$(hostname -s)\"'"
redstonepanel set-name "Test Box 1" >/dev/null && ok "redstonepanel set-name" || bad "redstonepanel set-name"
check "set-name shows without a restart"  bash -c "curl -s $B/api/panel-info | grep -q 'Test Box 1'"
check "set-name rejects 41+ characters"   bash -c '! redstonepanel set-name "$(printf "x%.0s" $(seq 41))" 2>/dev/null'
c=$(code -b $L -X PUT $B/api/settings -H "$J" -H "$O" -d '{"panel_name":"  Basement   Survival  ","login_mode":"required"}')
[ "$c" = 200 ] && grep -q '"name":"Basement Survival"' /tmp/body && ok "rename via Panel Settings (spaces tidied)" || bad "rename via API ($c $(cat /tmp/body))"
check "status shows the name"             bash -c 'redstonepanel status | grep -q "Name:      Basement Survival"'
redstonepanel set-name "" >/dev/null && check "empty name -> host name again" bash -c "curl -s $B/api/panel-info | grep -q '\"name\":\"$(hostname -s)\"'"
check "panel.log in /var/log"             test -s /var/log/redstonepanel/panel.log

# --- two-factor sign-in (1.16.7): setup with an SVG QR code, app codes (no replays), one-use
#     recovery codes, other sessions signed out, "Require two-factor for admins", admin reset, reset-2fa
# totp SECRET [STEPS_AHEAD]: the code an authenticator app shows (RFC 6238, SHA-1, 30 s, 6 digits)
totp() { python3 - "$1" "${2:-0}" <<'EOF'
import base64, hashlib, hmac, struct, sys, time
s = sys.argv[1].upper(); key = base64.b32decode(s + "=" * (-len(s) % 8))
d = hmac.new(key, struct.pack(">Q", int(time.time() // 30) + int(sys.argv[2])), hashlib.sha1).digest()
o = d[-1] & 15; print(str((struct.unpack(">I", d[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6).zfill(6))
EOF
}
jget() { python3 -c "import json; d=json.load(open('/tmp/body')); print($1)" 2>/dev/null; }
pw_login() { code -c "$1" -b "$1" -X POST $B/api/auth/login -H "$J" -H "$O" -d '{"username":"webadmin","password":"Panel-test-pw-1"}'; }
# Wrong passwords and codes count towards the 5-in-10-minutes limit per address; a restart clears it
systemctl restart redstonepanel; wait_up $B/api/health
W=/tmp/web.cookies W2=/tmp/web2.cookies
rm -f $W $W2; pw_login $W >/dev/null; pw_login $W2 >/dev/null
c=$(code -b $W -X POST $B/api/auth/2fa/setup -H "$O"); SECRET=$(jget 'd["secret"]')
[ "$c" = 200 ] && [ -n "$SECRET" ] && grep -q '<svg' /tmp/body && grep -q 'otpauth://totp/' /tmp/body \
    && ok "2FA setup: secret, otpauth URI, SVG QR code" || bad "2FA setup ($c $(head -c 300 /tmp/body))"
check "2FA wrong code at setup -> 400"    test "$(code -b $W -X POST $B/api/auth/2fa/confirm -H "$J" -H "$O" -d '{"code":"000000"}')" = 400
c=$(code -b $W -c $W -X POST $B/api/auth/2fa/confirm -H "$J" -H "$O" -d "{\"code\":\"$(totp "$SECRET")\"}")
RC1=$(jget 'd["recovery_codes"][0]'); RC2=$(jget 'd["recovery_codes"][1]')
[ "$c" = 200 ] && [ "$(jget 'len(d["recovery_codes"])')" = 10 ] && ok "2FA confirm -> 10 recovery codes" || bad "2FA confirm ($c $(cat /tmp/body))"
check "  totp.json is 0600"               test "$(stat -c %a /var/lib/redstonepanel/totp.json)" = 600
check "  this browser stays signed in"    test "$(code -b $W $B/api/auth/me)" = 200
check "  other sessions signed out"       test "$(code -b $W2 $B/api/auth/me)" = 401
rm -f $W2; c=$(pw_login $W2)
[ "$c" = 200 ] && grep -q '"code_needed":true' /tmp/body && ok "password alone -> code needed" || bad "password alone ($c $(cat /tmp/body))"
check "  no session before the code"     test "$(code -b $W2 $B/api/servers)" = 401
check "  wrong code -> 401"               test "$(code -b $W2 -c $W2 -X POST $B/api/auth/login/code -H "$J" -H "$O" -d '{"code":"000000"}')" = 401
NEXT=$(totp "$SECRET" 1)
check "  next app code signs in"          test "$(code -b $W2 -c $W2 -X POST $B/api/auth/login/code -H "$J" -H "$O" -d "{\"code\":\"$NEXT\"}")" = 200
check "  signed in"                       test "$(code -b $W2 $B/api/servers)" = 200
rm -f $W2; pw_login $W2 >/dev/null
check "  same app code again -> 401"      test "$(code -b $W2 -c $W2 -X POST $B/api/auth/login/code -H "$J" -H "$O" -d "{\"code\":\"$NEXT\"}")" = 401
c=$(code -b $W2 -c $W2 -X POST $B/api/auth/login/code -H "$J" -H "$O" -d "{\"code\":\"$RC1\"}")
[ "$c" = 200 ] && [ "$(jget 'd["recovery_left"]')" = 9 ] && ok "recovery code signs in, 9 left" || bad "recovery code ($c $(cat /tmp/body))"
rm -f $W2; pw_login $W2 >/dev/null
check "  same recovery code again -> 401" test "$(code -b $W2 -c $W2 -X POST $B/api/auth/login/code -H "$J" -H "$O" -d "{\"code\":\"$RC1\"}")" = 401
check "sign-ins logged with how"          grep -q 'webadmin (panel) signed in from .* with a recovery code' /var/log/redstonepanel/panel.log
systemctl restart redstonepanel; wait_up $B/api/health
rm -f $W2; c=$(pw_login $W2)
[ "$c" = 200 ] && grep -q '"code_needed":true' /tmp/body && ok "2FA still needed after a panel restart" || bad "2FA after restart ($c $(cat /tmp/body))"
# Require two-factor for admins: refused for an admin without it (would lock them out), then the
# Linux admin without 2FA can only reach the setup API
check "require-2FA refused for an admin without 2FA" test "$(code -b $L -X PUT $B/api/settings -H "$J" -H "$O" -d '{"login_mode":"required","require_2fa_admins":true}')" = 400
check "require-2FA set by an admin with 2FA" test "$(code -b $W -X PUT $B/api/settings -H "$J" -H "$O" -d '{"login_mode":"required","require_2fa_admins":true}')" = 200
check "  admin without 2FA -> 403"        test "$(code -b $L $B/api/servers)" = 403
check "  ...but can reach the setup API"  test "$(code -b $L $B/api/auth/2fa)" = 200
check "  can't turn own 2FA off while required" test "$(code -b $W -X POST $B/api/auth/2fa/disable -H "$J" -H "$O" -d "{\"code\":\"$RC2\"}")" = 400
check "require-2FA turned off again"      test "$(code -b $W -X PUT $B/api/settings -H "$J" -H "$O" -d '{"login_mode":"required","require_2fa_admins":false}')" = 200
check "  Linux admin back in"             test "$(code -b $L $B/api/servers)" = 200
# Lost phone: another admin resets it (Users), or root runs reset-2fa
check "admin resets webadmin's 2FA"       test "$(code -b $L -X POST $B/api/users/webadmin/reset-2fa -H "$O")" = 200
rm -f $W2; c=$(pw_login $W2)
[ "$c" = 200 ] && ! grep -q code_needed /tmp/body && ok "  password alone signs in again" || bad "  after admin reset ($c $(cat /tmp/body))"
code -b $W2 -X POST $B/api/auth/2fa/setup -H "$O" >/dev/null; SECRET=$(jget 'd["secret"]')
check "2FA set up again"                  test "$(code -b $W2 -c $W2 -X POST $B/api/auth/2fa/confirm -H "$J" -H "$O" -d "{\"code\":\"$(totp "$SECRET")\"}")" = 200
redstonepanel reset-2fa webadmin >/dev/null && ok "redstonepanel reset-2fa" || bad "redstonepanel reset-2fa"
rm -f $W2; c=$(pw_login $W2)
[ "$c" = 200 ] && ! grep -q code_needed /tmp/body && ok "  password alone signs in again" || bad "  after reset-2fa ($c $(cat /tmp/body))"
check "reset-2fa with no 2FA set up fails" bash -c '! redstonepanel reset-2fa webadmin'

# --- missing Java gives a clear error instead of a broken deploy
if ! command -v java >/dev/null; then
    c=$(code -b $L -X POST $B/api/servers/deploy -H "$J" -H "$O" -d '{"edition":"java"}')
    [ "$c" = 409 ] && grep -q '"package":"openjdk-25-jre-headless"' /tmp/body && ok "Java deploy without Java -> 409 naming the package" || bad "Java deploy without Java ($c $(cat /tmp/body))"
fi

# --- the Java versions offered are the ones this system's package sources have (Debian 13: 21, 25)
APT_JAVA=$(apt-cache pkgnames openjdk- | sed -nE 's/^openjdk-([0-9]+)-jre-headless$/\1/p' | sort -n | tr '\n' ' ')
code -b $L $B/api/java/runtimes >/dev/null
python3 - "$APT_JAVA" <<'EOF' && ok "Java versions offered = this system's openjdk packages" || bad "Java versions offered"
import json, sys
apt = sorted(int(x) for x in sys.argv[1].split() if int(x) >= 8)
got = json.load(open("/tmp/body"))
offered = sorted([i["major"] for i in got["installable"]] + [j["major"] for j in got["installed"]])
print(f"  apt has Java {apt}; the panel offers {offered}")
sys.exit(0 if offered == apt else 1)
EOF

# --- Bedrock: x86-64 only (Mojang makes no ARM64 build)
code -b $L $B/api/auth/me >/dev/null
if [ "$ARCH" = amd64 ]; then
    check "Bedrock offered on amd64"       grep -q '"bedrock_supported":true' /tmp/body
else
    check "Bedrock not offered on $ARCH"   grep -q '"bedrock_supported":false' /tmp/body
    c=$(code -b $L -X POST $B/api/servers/deploy -H "$J" -H "$O" -d '{"edition":"bedrock"}')
    [ "$c" = 400 ] && grep -q 'x86-64' /tmp/body && ! ls -d /opt/minecraft-bedrock* >/dev/null 2>&1 \
        && ok "Bedrock deploy on $ARCH -> 400, nothing made" || bad "Bedrock deploy on $ARCH ($c $(cat /tmp/body))"
fi

# --- import server backups: .tar.gz and .zip, each wrapped in a folder; hostile archives refused
BK=/tmp/backups && mkdir -p $BK/bedrock-server_backup_20261110/worlds/w
printf '#!/bin/sh\nexec sleep 600\n' > $BK/bedrock-server_backup_20261110/bedrock_server
chmod +x $BK/bedrock-server_backup_20261110/bedrock_server
echo "server-port=19132" > $BK/bedrock-server_backup_20261110/server.properties
tar -C $BK -czf /tmp/bedrock-backup.tar.gz bedrock-server_backup_20261110
c=$(code -b $L -X POST $B/api/servers/import -H "$O" -F "file=@/tmp/bedrock-backup.tar.gz")
if [ "$ARCH" = amd64 ]; then
    [ "$c" = 200 ] && grep -q '"server":"minecraft-bedrock"' /tmp/body && ok "import wrapped .tar.gz Bedrock backup" || bad "import .tar.gz ($c $(cat /tmp/body))"
    check "  files at the top of /opt/minecraft-bedrock" test -x /opt/minecraft-bedrock/bedrock_server
    check "  world kept"                       test -d /opt/minecraft-bedrock/worlds/w
    check "  owned by minecraft"               test "$(stat -c %U /opt/minecraft-bedrock/bedrock_server)" = minecraft
    check "  service starts"                   bash -c 'sleep 2; systemctl is-active --quiet minecraft-bedrock'
    check "  valid RestartSec in unit"         grep -qx "RestartSec=20" /etc/systemd/system/minecraft-bedrock.service
else
    [ "$c" = 400 ] && grep -q 'x86-64' /tmp/body && ok "Bedrock backup import on $ARCH -> 400" || bad "Bedrock import on $ARCH ($c $(cat /tmp/body))"
    check "  nothing left in /opt"             bash -c '! ls -d /opt/minecraft-bedrock* /opt/.redstonepanel-import-* 2>/dev/null'
fi
python3 - <<'EOF'
import zipfile
with zipfile.ZipFile("/tmp/java-backup.zip", "w") as z:
    z.writestr("MyJavaServer/server.jar", b"PK-not-really")
    z.writestr("MyJavaServer/server.properties", "server-port=25565\nmotd=hi\n")
    z.writestr("MyJavaServer/world/level.dat", b"x")
    z.writestr("__MACOSX/MyJavaServer/._server.jar", b"junk")
EOF
c=$(code -b $L -X POST $B/api/servers/import -H "$O" -F "file=@/tmp/java-backup.zip")
[ "$c" = 200 ] && grep -q '"server":"minecraft-java"' /tmp/body && ok "import wrapped .zip Java backup" || bad "import .zip ($c $(cat /tmp/body))"
check "  files at the top of /opt/minecraft-java" test -f /opt/minecraft-java/server.jar -a -f /opt/minecraft-java/world/level.dat
check "  no __MACOSX junk"                 test ! -e /opt/minecraft-java/__MACOSX
mkdir -p $BK/evil && echo x > $BK/evil/f && tar -C $BK -czf /tmp/evil.tar.gz --transform 's#^evil/f#../../../etc/rp-evil#' evil/f
c=$(code -b $L -X POST $B/api/servers/import -H "$O" -F "file=@/tmp/evil.tar.gz")
[ "$c" = 400 ] && [ ! -e /etc/rp-evil ] && ok "archive with ../ paths refused, nothing written" || bad "evil archive ($c $(cat /tmp/body))"
check "no unpack leftovers in /opt"        bash -c '! ls -d /opt/.redstonepanel-import-* 2>/dev/null'
echo "not an archive" > /tmp/notes.txt
check "non-archive upload refused (400)"   test "$(code -b $L -X POST $B/api/servers/import -H "$O" -F "file=@/tmp/notes.txt")" = 400

# --- performance (1.17): history, Memory and hard limits, on a stand-in Java server (a "java"
#     that only sleeps, so nothing is downloaded and it runs without a real JRE)
mkdir -p /usr/local/lib/rp-fakejava /opt/minecraft-perfjava
printf '#!/bin/bash\nexec -a "java $*" sleep 3600\n' > /usr/local/lib/rp-fakejava/java
chmod +x /usr/local/lib/rp-fakejava/java
echo "server-port=25599" > /opt/minecraft-perfjava/server.properties
chown -R minecraft:minecraft /opt/minecraft-perfjava
cat > /etc/systemd/system/minecraft-perfjava.service <<'UNIT'
[Unit]
Description=Minecraft Server (minecraft-perfjava)
[Service]
User=minecraft
Group=minecraft
WorkingDirectory=/opt/minecraft-perfjava
ExecStart=/usr/local/lib/rp-fakejava/java -Xms256M -Xmx512M -jar server.jar nogui
Restart=on-failure
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && systemctl start minecraft-perfjava
PJ=$B/api/servers/minecraft-perfjava
DROPIN=/etc/systemd/system/minecraft-perfjava.service.d/redstonepanel-limits.conf
AUTO=$(( (768 + 512) * 1024 * 1024 ))
check "perf: Performance card answers"    test "$(code -b $L $PJ/perf)" = 200
check "  tick health needs RCON (not a guess)" grep -q 'RCON is off' /tmp/body
check "  week range answers"              test "$(code -b $L "$PJ/perf?range=week")" = 200
check "  unknown range -> 400"            test "$(code -b $L "$PJ/perf?range=year")" = 400
check "perf: resources show the heap"     bash -c "test \"\$(curl -s -o /tmp/body -w '%{http_code}' -b $L $PJ/resources)\" = 200 && grep -q '\"xmx\":\"512M\"' /tmp/body"
c=$(code -b $L -X PUT $PJ/resources -H "$J" -H "$O" -d '{"xms":"256M","xmx":"64G"}')
[ "$c" = 400 ] && grep -q 'leave at least 1 GB' /tmp/body && ok "  heap over the machine's memory -> 400" || bad "  heap over memory ($c $(cat /tmp/body))"
c=$(code -b $L -X PUT $PJ/resources -H "$J" -H "$O" -d '{"memory":"600M"}')
[ "$c" = 400 ] && grep -q 'well above' /tmp/body && ok "  hard limit under the heap -> 400" || bad "  limit under heap ($c $(cat /tmp/body))"
c=$(code -b $L -X PUT $PJ/resources -H "$J" -H "$O" -d '{"xms":"256M","xmx":"768M","memory":"auto","cpu":0.5,"restart":true}')
[ "$c" = 200 ] && grep -q '"restarted":true' /tmp/body && ok "  set Memory 768M, automatic limit, 0.5 CPU, restart" || bad "  set resources ($c $(cat /tmp/body))"
check "  unit has the new heap, same java" grep -q '^ExecStart=/usr/local/lib/rp-fakejava/java -Xms256M -Xmx768M -jar server.jar nogui' /etc/systemd/system/minecraft-perfjava.service
check "  restarted with the new heap"     bash -c 'sleep 2; pgrep -f "java -Xms256M -Xmx768M"'
check "  drop-in has MemoryMax and CPUQuota" bash -c "grep -qx 'MemoryMax=$AUTO' $DROPIN && grep -qx 'CPUQuota=50%' $DROPIN"
check "  systemd applies MemoryMax"       test "$(systemctl show -p MemoryMax --value minecraft-perfjava)" = "$AUTO"
check "  systemd applies CPUQuota"        test "$(systemctl show -p CPUQuotaPerSecUSec --value minecraft-perfjava)" = 500ms
CG=/sys/fs/cgroup/system.slice/minecraft-perfjava.service
if [ -f $CG/memory.max ]; then
    check "  cgroup memory.max set"        test "$(cat $CG/memory.max)" = "$AUTO"
    check "  cgroup cpu.max set"           grep -q '^50000 100000' $CG/cpu.max
fi
check "  no runtime override left behind"  test ! -e /run/systemd/system.control/minecraft-perfjava.service.d
check "  change logged with who"          grep -q '\[PERF\] rpadmin set minecraft-perfjava' /var/log/redstonepanel/panel.log
c=$(code -b $L -X PUT $PJ/resources -H "$J" -H "$O" -d '{"memory":"off","cpu":0}')
[ "$c" = 200 ] && ok "  limits off" || bad "  limits off ($c $(cat /tmp/body))"
check "  drop-in removed, no limit live"  bash -c "test ! -e $DROPIN && test \"\$(systemctl show -p MemoryMax --value minecraft-perfjava)\" = infinity"
check "  heap kept"                       grep -q -- '-Xmx768M' /etc/systemd/system/minecraft-perfjava.service
# History: the sampler writes once a minute (first sample about 20 s after the panel starts)
for _ in $(seq 1 45); do
    [ -s /var/lib/redstonepanel/metrics/minecraft-perfjava.ring ] && curl -s -b $L $B/api/host/history | grep -q '"points":\[\[' && break
    sleep 2
done
check "perf: host history has points"     bash -c "curl -s -b $L $B/api/host/history | grep -q '\"points\":\[\['"
check "  server ring file, fixed size, 0600" bash -c 'f=/var/lib/redstonepanel/metrics/minecraft-perfjava.ring; test "$(stat -c %a $f)" = 600 && test "$(stat -c %s $f)" = $((8 + 2112 * 24))'
check "  metrics folder root-only"        test "$(stat -c %a /var/lib/redstonepanel/metrics)" = 700
check "  dashboard rows carry TPS"        bash -c "curl -s -b $L $B/api/host | grep -q '\"tps\"'"
c=$(code -b $L -X PUT $PJ/resources -H "$J" -H "$O" -d '{"cpu":1}')
check "  limit set again before uninstall" test "$c" = 200
check "  uninstall (keep world) removes the drop-in" bash -c "test \"\$(curl -s -o /dev/null -w '%{http_code}' -b $L -X DELETE -H '$O' '$PJ?keep_world=true')\" = 200 && test ! -e $DROPIN"

# --- HTTPS switch from Panel Settings, restart by systemd
c=$(code -b $L -X PUT $B/api/settings -H "$J" -H "$O" -d '{"https_mode":"self_signed","http_port":8080,"https_port":8443,"login_mode":"required"}')
[ "$c" = 200 ] && ok "enable self-signed HTTPS" || bad "enable self-signed HTTPS ($c $(cat /tmp/body))"
sleep 3
wait_up https://127.0.0.1:8443/api/health && ok "HTTPS up on 8443 after self-restart" || bad "HTTPS up on 8443"
check "HTTPS verifies with the panel CA"  curl -sf --cacert /var/lib/redstonepanel/tls/ca.crt https://127.0.0.1:8443/api/health
check "8080 redirects (302)"              test "$(curl -s -o /dev/null -w '%{http_code}' $B/)" = 302
check "TLS keys are root-only"            test "$(stat -c %a /var/lib/redstonepanel/tls/server.key)" = 600
redstonepanel reset-security >/dev/null && sleep 3
wait_up $B/api/health && ok "reset-security -> back on HTTP 8080" || bad "reset-security"

# --- reinstall (upgrade path) keeps data and restarts
apt-get install -y -qq --reinstall "$DEB" >/dev/null 2>&1 && ok "reinstall" || bad "reinstall"
wait_up $B/api/health && ok "running after reinstall" || bad "running after reinstall"
check "panel user kept after reinstall"   grep -q webadmin /var/lib/redstonepanel/users.json

# --- remove keeps data, purge deletes panel data but never game servers
mkdir -p /opt/minecraft-test/world && echo keep > /opt/minecraft-test/world/level.dat
apt-get remove -y -qq redstonepanel >/dev/null 2>&1 && ok "apt remove" || bad "apt remove"
check "service gone after remove"         bash -c '! systemctl is-active --quiet redstonepanel'
check "data kept after remove"            test -f /var/lib/redstonepanel/users.json
apt-get purge -y -qq redstonepanel >/dev/null 2>&1 && ok "apt purge" || bad "apt purge"
check "panel data deleted on purge"       test ! -e /var/lib/redstonepanel
check "game server untouched by purge"    test -f /opt/minecraft-test/world/level.dat

echo "=== $([ $FAILS = 0 ] && echo 'ALL PASSED' || echo "$FAILS FAILED") on $SYSTEM"
exit $FAILS
