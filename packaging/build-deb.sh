#!/bin/bash
# Builds the redstonepanel .deb inside a clean Ubuntu container and checks it with lintian.
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" ubuntu:24.04 bash /src/packaging/build-deb.sh
# The package is Architecture: all, so one build serves Ubuntu 24.04 and 25.04.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends build-essential debhelper lintian >/dev/null

rm -rf /build && mkdir -p /build/redstonepanel
cd /src
cp -r main.py run.py https_redirect.py core static packaging debian /build/redstonepanel/
cd /build/redstonepanel
rm -rf core/__pycache__
# Sources may come from a Windows checkout: drop CRs from text files only. grep -I skips binary
# files, which this would corrupt (every PNG's header contains \r\n).
{ grep -rIl $'\r' . || true; } | xargs -r sed -i 's/\r$//'
find . -type d -exec chmod 755 {} + && find . -type f -exec chmod 644 {} +   # Windows mounts mark everything executable
chmod +x debian/rules packaging/redstonepanel

dpkg-buildpackage -us -uc -b > /build/build.log 2>&1 || { tail -40 /build/build.log; exit 1; }
DEB=$(ls /build/redstonepanel_*_all.deb)
echo "== lintian"
lintian --info --no-tag-display-limit "$DEB" || true
echo "== contents"
dpkg-deb -c "$DEB" | awk '{print $1, $6}'
mkdir -p /out && cp "$DEB" /out/
echo "built: $(basename "$DEB")"
