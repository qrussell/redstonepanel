# Ubuntu with systemd as PID 1, for testing the .deb like on a real server.
#   docker build --build-arg REL=24.04 -t rp-systemd:24.04 packaging/test
#   docker run -d --privileged --name rp-2404 -v "$PWD/dist:/dist:ro" rp-systemd:24.04
ARG REL=24.04
FROM ubuntu:${REL}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends systemd systemd-sysv dbus curl ca-certificates iproute2 \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /usr/sbin/policy-rc.d
# ^ Docker images block package installs from starting services; a real server doesn't.
#   (Docs/man pages are still stripped by the image's dpkg excludes.)
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
