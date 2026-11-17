"""Bedrock Dedicated Server networking checks (NetherNet era, BDS 1.26.x).

With transport=nethernet, BDS accepts clients on TCP server-port (an HTTP signaling handshake,
logged as "Accepting clients on 0.0.0.0:19132") and carries gameplay over UDP server-udp-ports.
Two ways a server ends up unreachable without any error in its log are detected here:
  * the build only supports NetherNet but transport is something else ("TRANSPORT TYPE ERROR");
  * the signaling listener never started (no "Accepting clients" line since the last start).
    Seen on 1.26.63.1 when server-udp-ports has the documented public-address form
    (e.g. 203.0.113.7:19141:19141); the plain form (19141) works.
"""
import re

START_RE = re.compile(r"Version:? [0-9]")
# 203.0.113.7:19141:19141, [2001:db8::1]:21200-21300:19400-19500
ADDRESS_PREFIX_RE = re.compile(r"^\s*(\[[0-9A-Fa-f:.]+\]|\d{1,3}(\.\d{1,3}){3}):")

GREP_PATTERN = r"Version:? [0-9]|TRANSPORT TYPE ERROR|Accepting clients on|Server started"


def last_start_status(log_lines):
    """From the server log lines (any subset that includes the matches of GREP_PATTERN), for the last start:
    started: finished starting ("Server started." after the last "Version:" line)
    transport_rejected: BDS said the configured transport isn't supported
    accepting: the address it accepts NetherNet clients on, or None"""
    starts = [i for i, l in enumerate(log_lines) if START_RE.search(l)]
    since = log_lines[starts[-1]:] if starts else log_lines
    accepting = None
    for l in since:
        m = re.search(r"Accepting clients on (\S+)", l)
        if m:
            accepting = m.group(1)
    return {
        "started": bool(starts) and any("Server started" in l for l in since),
        "transport_rejected": any("TRANSPORT TYPE ERROR" in l for l in since),
        "accepting": accepting,
    }


def udp_ports_have_address(value):
    """server-udp-ports written with a public address prefix (IP:public:local)."""
    return bool(value) and bool(ADDRESS_PREFIX_RE.match(value))


def plain_udp_ports(value):
    """The same setting without the address: '203.0.113.7:19141:19141' -> '19141:19141' (or '19141' when equal)."""
    rest = ADDRESS_PREFIX_RE.sub("", value or "").strip()
    pub, _, local = rest.partition(":")
    return pub if pub and pub == local else rest
