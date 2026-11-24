#!/bin/bash
# End-to-end test that upgrades don't restart game servers (1.21.9). In a throwaway systemd
# container with the .deb from dist/ and Ubuntu's needrestart:
#   a stand-in game server (minecraft-nrtest) and a stand-in Phantom proxy, plus an ordinary
#   service, all run a program that uses a library which is then deleted, as an apt upgrade
#   does. needrestart in automatic mode must restart the ordinary service and leave the game
#   server and the proxy running with the same process.
# Run from the repository root on a machine with Docker, after building the .deb:
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-needrestart.sh          (KEEP=1 leaves the container running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
C=nr
IMG=${IMG:-rp-systemd:24.04}
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$C" bash -c "$1"; }
check(){ local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
is()   { local name=$1 want=$2 got=$3; [ "$got" = "$want" ] && ok "$name" || bad "$name: wanted $want, got $got"; }
cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Container kept: $C (docker rm -f $C)"; return; }
    docker rm -f "$C" >/dev/null 2>&1
}
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
echo "=== needrestart test with $(basename "$DEB")"
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f "$C" >/dev/null 2>&1
docker run -d --privileged --cgroupns=private --name "$C" --hostname "$C" -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
sleep 3
[ -n "${RP_TEST_SETUP:-}" ] && bash "$RP_TEST_SETUP" "$C"
dx "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") needrestart >/tmp/install.log 2>&1" \
    || { bad "install"; dx "tail -20 /tmp/install.log"; }
check "the panel is installed and running" dx "systemctl is-active --quiet redstonepanel"
check "the needrestart rule is installed" dx "test -f /etc/needrestart/conf.d/redstonepanel.conf"
check "  and needrestart reads it without complaint" dx "perl -e 'our %nrconf; do \"/etc/needrestart/needrestart.conf\"; die \$@ if \$@; do \"/etc/needrestart/conf.d/redstonepanel.conf\"; die \$@ if \$@'"

# Three services that each hold a library which an upgrade then deletes
LIB=$(dx "ls /usr/lib/*-linux-gnu/libz.so.1 | head -1")
for u in minecraft-nrtest phantom-minecraft-nrtest nrtest-other; do
    dx "cp $LIB /usr/lib/rp-$u.so && cat > /etc/systemd/system/$u.service <<UNIT
[Service]
Environment=LD_PRELOAD=/usr/lib/rp-$u.so
ExecStart=/usr/bin/sleep infinity
UNIT
systemctl daemon-reload && systemctl start $u"
done
sleep 1
pids() { dx "for u in minecraft-nrtest phantom-minecraft-nrtest nrtest-other; do systemctl show -p MainPID --value \$u; done | tr '\n' ' '"; }
BEFORE=$(pids)
dx "rm -f /usr/lib/rp-minecraft-nrtest.so /usr/lib/rp-phantom-minecraft-nrtest.so /usr/lib/rp-nrtest-other.so"
OUT=$(dx "needrestart -r l -b 2>&1")
echo "$OUT" | grep -q "NEEDRESTART-SVC: minecraft-nrtest.service" && ok "needrestart sees the game server's outdated library" \
    || { bad "needrestart sees the outdated library"; echo "$OUT"; }
# What apt runs after an upgrade, in automatic mode: restart whatever it may
OUT=$(dx "needrestart -r a -u NeedRestart::UI::stdio 2>&1")
echo "$OUT" | grep -q "systemctl restart nrtest-other.service" && ok "needrestart restarts an ordinary service" || { bad "needrestart restarts an ordinary service"; echo "$OUT"; }
AFTER=$(pids)
set -- $BEFORE; B1=$1 B2=$2 B3=$3
set -- $AFTER;  A1=$1 A2=$2 A3=$3
is "the game server kept running, same process" "$B1" "$A1"
is "the Bedrock proxy kept running, same process" "$B2" "$A2"
[ "$B3" != "$A3" ] && [ "$A3" != 0 ] && ok "an ordinary service was restarted, so needrestart did act" || bad "ordinary service: before $B3, after $A3"
check "  the panel removes the rule with the package" dx "DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq redstonepanel >/dev/null 2>&1; ! test -f /etc/needrestart/conf.d/redstonepanel.conf"
# And without the rule, needrestart would have restarted the game server
dx "cp $LIB /usr/lib/rp-minecraft-nrtest.so && systemctl restart minecraft-nrtest"
sleep 1
B1=$(dx "systemctl show -p MainPID --value minecraft-nrtest")
dx "rm -f /usr/lib/rp-minecraft-nrtest.so; needrestart -r a -u NeedRestart::UI::stdio >/dev/null 2>&1"
A1=$(dx "systemctl show -p MainPID --value minecraft-nrtest")
[ "$B1" != "$A1" ] && ok "without the rule the game server would have been restarted" || bad "without the rule: before $B1, after $A1"

echo
[ "$FAILS" = 0 ] && echo "ALL PASSED" || echo "$FAILS FAILED"
exit "$FAILS"
