#!/bin/sh
# Installs RedstonePanel on Ubuntu 24.04 or 25.04 from the signed qrussell-repo apt repository,
# so that `apt upgrade` brings every later version.
#
#   curl -fsSL https://raw.githubusercontent.com/qrussell/redstonepanel/main/install.sh | sudo sh
#
# It does what the README's manual steps do: adds the repository's key (checking its fingerprint)
# and source, then `apt install redstonepanel`. Read it first if you like; it's short.
set -eu

KEY_URL=https://qrussell.github.io/qrussell-repo/deb/qrussell-repo.gpg
REPO="deb [signed-by=/usr/share/keyrings/qrussell-repo.gpg] https://qrussell.github.io/qrussell-repo/deb ./"
FINGERPRINT=9994A9EB39EB3C101C949FB2012BA14E0FE3A355
KEYRING=/usr/share/keyrings/qrussell-repo.gpg

say() { printf '\033[1;31m▸\033[0m %s\n' "$*"; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run this as root, for example: curl -fsSL …/install.sh | sudo sh"
[ -r /etc/os-release ] && . /etc/os-release
if [ "${ID:-}" != ubuntu ] || { [ "${VERSION_ID:-}" != 24.04 ] && [ "${VERSION_ID:-}" != 25.04 ]; }; then
    [ "${FORCE:-}" = 1 ] || die "RedstonePanel supports Ubuntu 24.04 and 25.04; this is ${PRETTY_NAME:-unknown}. Set FORCE=1 to try anyway."
    say "Not a supported release (${PRETTY_NAME:-unknown}); carrying on because FORCE=1."
fi

export DEBIAN_FRONTEND=noninteractive
say "Installing curl and the CA certificates"
apt-get update -qq
apt-get install -y -qq curl ca-certificates gnupg >/dev/null

say "Adding the qrussell-repo signing key"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
curl -fsSL "$KEY_URL" -o "$tmp"
gpg --show-keys --with-colons "$tmp" 2>/dev/null | grep -q "^fpr:*$FINGERPRINT:" \
    || die "the downloaded key isn't the qrussell-repo key ($FINGERPRINT); nothing was installed"
install -m 0644 "$tmp" "$KEYRING"
echo "$REPO" > /etc/apt/sources.list.d/qrussell.list

say "Installing RedstonePanel"
apt-get update -qq
apt-get install -y redstonepanel

echo
say "Done. New versions arrive with: sudo apt update && sudo apt upgrade"
echo "  User manual: https://github.com/qrussell/redstonepanel/blob/main/docs/guide/README.md"
