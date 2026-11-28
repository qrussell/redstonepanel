#!/bin/bash
# End-to-end test of Cloudflare Tunnel set up by the panel (1.29), for a tunnel managed in a file,
# in a throwaway systemd container that installs the .deb from dist/ and Cloudflare's real
# cloudflared package (downloaded on the host from GitHub, so the container needs no internet):
#   * the panel finds cloudflared's systemd service and its config.yml;
#   * Apply writes the panel's routes into config.yml, keeps the admin's own route, has the real
#     cloudflared check the file and restarts the service; the real cloudflared then sends
#     play.example.org/join/... to the join port and panel.example.org to the panel, and
#     everything else where it went before (`cloudflared tunnel ingress rule`);
#   * the join pages' address and proxy follow, and the sign-in limit counts each visitor's own
#     address (CF-Connecting-IP) from the local cloudflared only;
#   * a new join port is followed by itself; cloudflared not coming back puts the old file back;
#   * Remove leaves config.yml exactly as it was.
# cloudflared can't reach Cloudflare here, so its service is a small stand-in that runs the real
# program's own check of the file and then answers /ready like cloudflared's metrics page.
# A tunnel managed in Cloudflare (the API side) is covered by tests/test_cftunnel.py against a
# stand-in API, and by hand against a real account.
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
#   bash packaging/test/test-cloudflare-tunnel.sh       (KEEP=1 leaves the container running)
set -uo pipefail
export MSYS_NO_PATHCONV=1
HOST_PWD=$(pwd -W 2>/dev/null || pwd)
FAILS=0
IMG=${IMG:-rp-systemd:24.04}
C=cft
PW_ADMIN='tunnel-test-admin-1'            # a throwaway account inside the test container only
TUNNEL=6ff42ae2-765d-4adf-8112-31c55c1551ef
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS + 1)); }
dx()   { docker exec "$C" bash -c "$1"; }
is()   { local name=$1 want=$2 got=$3; [ "$got" = "$want" ] && ok "$name" || bad "$name: wanted $want, got $got"; }
has()  { local name=$1 text=$2 body=$3; case "$body" in *"$text"*) ok "$name";; *) bad "$name: '$text' not in: ${body:0:300}";; esac; }
lacks(){ local name=$1 text=$2 body=$3; case "$body" in *"$text"*) bad "$name: '$text' is there";; *) ok "$name";; esac; }
PY=python3; $PY -c 'import json' 2>/dev/null || PY=python
json() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
URL=http://127.0.0.1:8080
api()  { dx "curl -s -b /tmp/jar -X $1 '$URL$2' ${3:+-H 'Content-Type: application/json' -d '$3'}"; }
code() { dx "curl -s -o /dev/null -w '%{http_code}' -b /tmp/jar -X $1 '$URL$2' ${3:+-H 'Content-Type: application/json' -d '$3'}"; }
rule() { dx "/usr/bin/cloudflared --config /etc/cloudflared/config.yml tunnel ingress rule '$1' 2>&1" | sed -n 's/.*service: *//p' | tail -1; }
invocation() { dx "systemctl show cloudflared -p InvocationID --value"; }

cleanup() {
    [ "${KEEP:-}" = 1 ] && { echo "Container kept: $C (docker rm -f $C)"; return; }
    docker rm -f $C >/dev/null 2>&1
    rm -rf "$WORK"
}
WORK=$(mktemp -d)
trap cleanup EXIT

DEB=${DEB:-$(ls dist/redstonepanel_*_all.deb 2>/dev/null | sort -V | tail -1)}
[ -n "$DEB" ] || { echo "No dist/redstonepanel_*_all.deb: build it first (see the top of this file)."; exit 1; }
ARCH=$(uname -m | sed 's/x86_64/amd64/; s/aarch64/arm64/')
CFD=${CFD:-https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH.deb}
echo "=== Cloudflare Tunnel test with $(basename "$DEB")"
curl -fsSL -o "$WORK/cloudflared.deb" "$CFD" || { echo "Couldn't download cloudflared from $CFD"; exit 1; }
docker build -q -t "$IMG" packaging/test >/dev/null
docker rm -f $C >/dev/null 2>&1
docker run -d --privileged --cgroupns=private --name $C --hostname $C -v "$HOST_PWD/dist:/dist:ro" "$IMG" >/dev/null
sleep 3
[ -n "${RP_TEST_SETUP:-}" ] && bash "$RP_TEST_SETUP" $C
docker cp "$WORK/cloudflared.deb" $C:/tmp/cloudflared.deb
dx "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends /dist/$(basename "$DEB") python3 iproute2 >/tmp/install.log 2>&1 && dpkg -i /tmp/cloudflared.deb >>/tmp/install.log 2>&1" \
    || { echo "install failed"; dx "tail -20 /tmp/install.log"; exit 1; }
is "panel running" active "$(dx 'systemctl is-active redstonepanel')"
has "real cloudflared installed" "cloudflared version" "$(dx '/usr/bin/cloudflared --version 2>&1')"

# --- cloudflared's service, managed in a file. The stand-in at /usr/local/sbin/cloudflared hands
# everything to the real program except `tunnel run`, which checks the file with the real program,
# then answers /ready on cloudflared's metrics port. /etc/cloudflared/fail-next makes one start fail.
dx "mkdir -p /etc/cloudflared && cat > /etc/cloudflared/config.yml <<'EOF'
tunnel: $TUNNEL
credentials-file: /etc/cloudflared/$TUNNEL.json

ingress:
  # the admin's own route, which must stay
  - hostname: nas.example.org
    service: http://10.0.0.9:5000
  - service: http_status:404
EOF
echo '{\"AccountTag\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"TunnelID\":\"$TUNNEL\",\"TunnelSecret\":\"c2VjcmV0\"}' > /etc/cloudflared/$TUNNEL.json
cp /etc/cloudflared/config.yml /tmp/config-original.yml
cat > /usr/local/sbin/cloudflared <<'EOF'
#!/bin/bash
case \" \$* \" in
  *' tunnel run'*)
    cfg=\$(echo \"\$*\" | sed -n 's/.*--config \\([^ ]*\\).*/\\1/p')
    if [ -e /etc/cloudflared/fail-next ]; then rm -f /etc/cloudflared/fail-next; exit 1; fi
    /usr/bin/cloudflared --config \"\$cfg\" tunnel ingress validate || exit 1
    exec python3 -c '
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == \"/ready\" else 404); self.end_headers(); self.wfile.write(b\"{}\")
    def log_message(self, *a): pass
http.server.HTTPServer((\"127.0.0.1\", 20241), H).serve_forever()' ;;
  *) exec /usr/bin/cloudflared \"\$@\" ;;
esac
EOF
chmod +x /usr/local/sbin/cloudflared
cat > /etc/systemd/system/cloudflared.service <<'EOF'
[Unit]
Description=cloudflared (stand-in that checks its file with the real program)
[Service]
ExecStart=/usr/local/sbin/cloudflared --no-autoupdate --config /etc/cloudflared/config.yml tunnel run
Restart=no
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now cloudflared >/dev/null 2>&1"
sleep 2
is "cloudflared's service runs" active "$(dx 'systemctl is-active cloudflared')"
is "before: nas goes to the admin's own service" "http://10.0.0.9:5000" "$(rule https://nas.example.org/)"
is "before: play.example.org isn't routed" "http_status:404" "$(rule https://play.example.org/join/x)"

dx "echo '$PW_ADMIN' | redstonepanel add-user admin --role admin" >/dev/null
dx "curl -s -c /tmp/jar -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PW_ADMIN\"}' $URL/api/auth/login" >/dev/null
is "admin signed in" 200 "$(code GET /api/settings/tunnel)"

T=$(api POST /api/settings/tunnel/detect)
is "found: managed in a file" "local /etc/cloudflared/config.yml $TUNNEL True" \
   "$(echo "$T" | json "d['found']['mode'], d['found']['config'], d['found']['tunnel_id'], d['found']['running']" | tr -d "(),'")"
is "join pages on" 200 "$(code PUT /api/settings/join '{"enabled":true,"port":8444}')"
is "names saved without a token" 200 "$(code PUT /api/settings/tunnel '{"panel_host":"panel.example.org","join_host":"play.example.org"}')"
has "the preview names both routes" "Route play.example.org/join/... → http://localhost:8444" "$(api GET /api/settings/tunnel)"

BEFORE=$(invocation)
A=$(api POST /api/settings/tunnel/apply)
has "apply: written and restarted" "cloudflared restarted" "$A"
has "apply: DNS names to add by hand without a token" "cloudflared tunnel route dns $TUNNEL play.example.org" "$A"
[ "$(invocation)" != "$BEFORE" ] && ok "cloudflared was restarted" || bad "cloudflared wasn't restarted"
is "cloudflared runs again" active "$(dx 'systemctl is-active cloudflared')"
is "real cloudflared: play.example.org/join goes to the join port" "http://localhost:8444" "$(rule https://play.example.org/join/survival)"
is "real cloudflared: play.example.org elsewhere doesn't reach the panel" "http_status:404" "$(rule https://play.example.org/api/settings)"
is "real cloudflared: panel.example.org goes to the panel" "http://localhost:8080" "$(rule https://panel.example.org/)"
is "real cloudflared: the admin's own route stays" "http://10.0.0.9:5000" "$(rule https://nas.example.org/)"
has "the old file was kept" "nas.example.org" "$(dx 'cat /etc/cloudflared/config.yml.redstonepanel-old')"
S=$(api GET /api/join-pages)
is "join pages address follows" "https://play.example.org" "$(echo "$S" | json "d['proxy']['url']")"
is "join pages proxy is the local cloudflared" "['127.0.0.1', '::1']" "$(echo "$S" | json "d['proxy']['addresses']")"
is "the panel's public address" "https://panel.example.org" "$(api GET /api/settings/tunnel | json "d['panel_url']")"

# --- the sign-in limit counts the visitor that cloudflared names, from cloudflared only
for i in 1 2 3 4 5 6; do
    dx "curl -s -o /dev/null -H 'CF-Connecting-IP: 198.51.100.7' -H 'Content-Type: application/json' -d '{\"username\":\"nobody$i\",\"password\":\"x\"}' $URL/api/auth/login"
done
has "one visitor through the tunnel is stopped after 5 tries" "Too many failed sign-in" \
   "$(dx "curl -s -H 'CF-Connecting-IP: 198.51.100.7' -H 'Content-Type: application/json' -d '{\"username\":\"nobody9\",\"password\":\"x\"}' $URL/api/auth/login")"
lacks "another visitor through the tunnel isn't" "Too many failed sign-in" \
   "$(dx "curl -s -H 'CF-Connecting-IP: 198.51.100.8' -H 'Content-Type: application/json' -d '{\"username\":\"nobody10\",\"password\":\"x\"}' $URL/api/auth/login")"
ME=$(dx "hostname -I | awk '{print \$1}'")
dx "curl -s -o /dev/null -H 'CF-Connecting-IP: 198.51.100.66' -H 'Content-Type: application/json' -d '{\"username\":\"nobody11\",\"password\":\"x\"}' http://$ME:8080/api/auth/login"
has "a header from anyone but cloudflared means nothing" "'nobody11' from $ME" "$(dx 'grep -h nobody11 /var/log/redstonepanel/panel.log')"
has "the activity log names the visitor" "from 198.51.100.7" "$(dx 'grep -h "Failed sign-in" /var/log/redstonepanel/panel.log | tail -3')"
dx "systemctl restart redstonepanel" && sleep 4
dx "curl -s -c /tmp/jar -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PW_ADMIN\"}' $URL/api/auth/login" >/dev/null

# --- a new join port is followed by itself
is "join port changed" 200 "$(code PUT /api/settings/join '{"enabled":true,"port":8555}')"
for i in $(seq 1 30); do [ "$(rule https://play.example.org/join/x)" = "http://localhost:8555" ] && break; sleep 1; done
is "real cloudflared: the new join port" "http://localhost:8555" "$(rule https://play.example.org/join/x)"
is "still one panel block" 1 "$(dx 'grep -c "BEGIN RedstonePanel" /etc/cloudflared/config.yml')"

# --- public access to the admin panel off
api PUT /api/settings/tunnel '{"panel_public":false}' >/dev/null
api POST /api/settings/tunnel/apply >/dev/null
is "panel off: panel.example.org no longer reaches the panel" "http_status:404" "$(rule https://panel.example.org/)"
is "panel off: join pages stay" "http://localhost:8555" "$(rule https://play.example.org/join/x)"

# --- cloudflared not coming back: the old file goes back
GOOD=$(dx 'cat /etc/cloudflared/config.yml')
api PUT /api/settings/tunnel '{"panel_public":true}' >/dev/null
dx "touch /etc/cloudflared/fail-next"
F=$(api POST /api/settings/tunnel/apply)
has "a failed restart is reported" "put the old file back" "$F"
is "the old file is back" "$GOOD" "$(dx 'cat /etc/cloudflared/config.yml')"
is "cloudflared runs with it" active "$(dx 'systemctl is-active cloudflared')"

# --- a token is checked with Cloudflare, and never shown again
P=$(api PUT /api/settings/tunnel '{"token":"AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-ab"}')
case "$P" in *"can't be reached"*|*"didn't accept"*) ok "the token is checked with Cloudflare";; *) bad "token check: $P";; esac
lacks "no token in the settings" "AbCdEfGhIjKlMnOpQrStUvWxYz" "$(api GET /api/settings/tunnel)"
lacks "no token in the log" "AbCdEfGhIjKlMnOpQrStUvWxYz" "$(dx 'cat /var/log/redstonepanel/panel.log')"

# --- Remove: config.yml exactly as it was
R=$(api POST /api/settings/tunnel/remove)
has "remove" "taken out of /etc/cloudflared/config.yml" "$R"
is "config.yml is as it was" "$(dx 'cat /tmp/config-original.yml')" "$(dx 'cat /etc/cloudflared/config.yml')"
is "join pages address back to none" "None" "$(api GET /api/join-pages | json "d['proxy']['url'] or None")"
is "real cloudflared: play.example.org isn't routed again" "http_status:404" "$(rule https://play.example.org/join/x)"
has "every change is in the activity log" "[TUNNEL] admin removed" "$(dx 'cat /var/log/redstonepanel/panel.log')"

echo
[ "$FAILS" = 0 ] && echo "ALL PASSED" || echo "$FAILS FAILED"
exit $((FAILS > 0))
