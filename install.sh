#!/bin/sh
# Installs RedstonePanel from the signed qrussell-repo apt repository, so that `apt upgrade`
# brings every later version. It runs on Ubuntu 24.04, 26.04 and 26.10, Debian 13 and 64-bit
# Raspberry Pi OS (Debian 13 based, Raspberry Pi 4 or 5), on x86-64 (amd64) or ARM64 machines.
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

# Supported: Ubuntu 24.04, 26.04, 26.10; Debian 13 (Raspberry Pi OS 64-bit says ID=debian).
# Ubuntu 25.04 and 25.10 no longer get updates but still work, so existing machines can reinstall.
system="${PRETTY_NAME:-unknown}"
case "${ID:-}:${VERSION_ID:-}" in
    ubuntu:24.04|ubuntu:26.04|ubuntu:26.10|debian:13) problem= ;;
    ubuntu:25.04|ubuntu:25.10)
        problem=
        say "$system no longer gets security updates; consider upgrading to Ubuntu 26.04 LTS." ;;
    debian:12|raspbian:12)
        problem="RedstonePanel needs Debian 13 or newer (Python 3.12 or newer); this is $system. On a Raspberry Pi, install the current 64-bit Raspberry Pi OS." ;;
    raspbian:*)
        problem="this is 32-bit Raspberry Pi OS ($system). RedstonePanel needs the 64-bit Raspberry Pi OS on a Raspberry Pi 4 or 5." ;;
    *)
        problem="RedstonePanel supports Ubuntu 24.04, 26.04 and 26.10, Debian 13 and 64-bit Raspberry Pi OS; this is $system." ;;
esac
arch=$(dpkg --print-architecture 2>/dev/null || echo unknown)
case "$arch" in
    amd64|arm64) ;;
    armhf|armel) problem="${problem:-this is a 32-bit ARM system ($arch). RedstonePanel needs a 64-bit (arm64) system: on a Raspberry Pi 4 or 5, install the 64-bit Raspberry Pi OS.}" ;;
    *) problem="${problem:-RedstonePanel runs on x86-64 (amd64) and ARM64 machines; this one is $arch.}" ;;
esac
if [ -n "$problem" ]; then
    [ "${FORCE:-}" = 1 ] || die "$problem Set FORCE=1 to try anyway."
    say "Not a supported system: $problem Carrying on because FORCE=1."
fi
if [ "$arch" = arm64 ]; then
    say "ARM64 machine: Java servers only (Mojang makes the Bedrock server for x86-64 only)."
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
