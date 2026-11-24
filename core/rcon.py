"""Minimal Minecraft (Java Edition) RCON client, so the panel doesn't need the mcrcon program.

Protocol (Source RCON as used by Minecraft): little-endian packets
    int32 length | int32 request id | int32 type | ascii body | 0x00 0x00
type 3 = login, 2 = command; the server answers a failed login with request id -1.
"""
import re
import socket
import struct

LOGIN, COMMAND = 3, 2
COLOR_RE = re.compile("§.")   # Minecraft section-sign colour/format codes


class RconError(RuntimeError):
    pass


class RconAuthError(RconError):
    """The server rejected the password."""


class RconRefused(RconError):
    """Nothing listens on the RCON port (RCON is off, or the server is down)."""


def _send(sock, req_id, kind, body):
    payload = struct.pack("<ii", req_id, kind) + body.encode("utf-8") + b"\x00\x00"
    sock.sendall(struct.pack("<i", len(payload)) + payload)


def _recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise RconError("connection closed by the server")
        data += chunk
    return data


def _recv(sock):
    (length,) = struct.unpack("<i", _recv_exact(sock, 4))
    if not 10 <= length <= 1024 * 1024:
        raise RconError(f"invalid packet length {length}")
    data = _recv_exact(sock, length)
    req_id, kind = struct.unpack("<ii", data[:8])
    return req_id, kind, data[8:-2].decode("utf-8", errors="replace")


def command(host, port, password, cmd, timeout=5.0):
    """Logs in, runs one command, returns its output with colour codes removed."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            _send(sock, 1, LOGIN, password)
            req_id, _, _ = _recv(sock)
            if req_id == -1:
                raise RconAuthError("wrong RCON password")
            _send(sock, 2, COMMAND, cmd)
            # Long outputs arrive in several packets; a second, empty "marker" command tells us
            # when the real answer is complete (the server answers in order). The marker goes out
            # only after the first part has come back: Minecraft reads one packet at a time and
            # hangs up when two arrive together, which Windows makes likely.
            parts = []
            marker_sent = False
            while True:
                req_id, _, body = _recv(sock)
                if req_id == 2:
                    parts.append(body)
                    if not marker_sent:
                        _send(sock, 3, COMMAND, "")
                        marker_sent = True
                elif req_id == 3:
                    break
            return COLOR_RE.sub("", "".join(parts))
    except ConnectionRefusedError as e:
        raise RconRefused(str(e)) from e
    except (OSError, struct.error) as e:
        raise RconError(str(e)) from e
