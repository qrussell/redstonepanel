# Ubuntu or Debian with systemd as PID 1, for testing the .deb like on a real server.
#   docker build --build-arg REL=24.04 -t rp-systemd:24.04 packaging/test
#   docker build --build-arg DISTRO=debian --build-arg REL=13 -t rp-systemd:debian-13 packaging/test
#   docker build --platform linux/arm64 --build-arg REL=26.04 -t rp-systemd:26.04-arm64 packaging/test
#   docker run -d --privileged --name rp-2404 -v "$PWD/dist:/dist:ro" rp-systemd:24.04
ARG DISTRO=ubuntu
ARG REL=24.04
FROM ${DISTRO}:${REL}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends systemd systemd-sysv dbus curl ca-certificates iproute2 \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /usr/sbin/policy-rc.d
# ^ Docker images block package installs from starting services; a real server doesn't.
#   (Docs/man pages are still stripped by the image's dpkg excludes.)
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
