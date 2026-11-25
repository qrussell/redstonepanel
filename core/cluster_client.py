"""HTTPS calls between cluster members (stdlib only).

The master calls its nodes with the node's token, checking the node's certificate against the
cluster CA (including the host name). A joining node calls the master once: first to fetch the
cluster CA (checked against the fingerprint in the join code), then to present the join code.
"""
import http.client
import json
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from core import cluster
from core import settings as panel_settings

TIMEOUT = 4
MAX_BODY = 1 << 20
OVERVIEW_TTL = 10
LAST_OK_EVERY = 60

_cache_lock = threading.Lock()
_cache = {}          # node id -> (time, result)


class NodeError(Exception):
    """A failed call. state: unreachable | rejected | cert_error | too_old | error."""
    def __init__(self, state, message, status=None, payload=None):
        super().__init__(message)
        self.state, self.status, self.payload = state, status, payload


def _split(url):
    u = urlsplit(url)
    if u.scheme != "https" or not u.hostname:
        raise NodeError("error", f"Bad address: {url}")
    return u.hostname, u.port or 443


def _request(url, method, path, ctx, headers=None, body=None, timeout=TIMEOUT):
    host, port = _split(url)
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=timeout)
    try:
        conn.request(method, path, body=data, headers=hdrs)
        res = conn.getresponse()
        raw = res.read(MAX_BODY + 1)
    except ssl.SSLCertVerificationError as e:
        raise NodeError("cert_error", f"Certificate problem: {e.verify_message or e}")
    except ssl.SSLError as e:
        raise NodeError("cert_error", f"TLS problem: {e.reason or e}")
    except (OSError, socket.timeout, http.client.HTTPException) as e:
        raise NodeError("unreachable", str(e) or e.__class__.__name__)
    finally:
        conn.close()
    return _answer(res.status, raw)


def _answer(status, raw):
    """A node's JSON answer, or the NodeError its status means."""
    if len(raw) > MAX_BODY:
        raise NodeError("error", "The answer was too large.")
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        payload = {}
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if status == 401:
        raise NodeError("rejected", detail or "Not accepted (token revoked, node left the cluster, or a newer master).",
                        status, payload)
    if status == 403:
        raise NodeError("too_old", detail or "Refused: this node may need a newer RedstonePanel.", status, payload)
    if status >= 400:
        raise NodeError("error", detail or f"HTTP {status}", status, payload)
    return payload


# ---------------------------------------------------------------------------
# Master -> node
# ---------------------------------------------------------------------------
def _cluster_ctx():
    ctx = ssl.create_default_context(cafile=cluster.ca_crt())   # checks the host name too
    return ctx


def node_ctx(node):
    """The cluster CA, or for a node with its own public certificate the system's trust store.
    Both check the host name."""
    return ssl.create_default_context() if cluster.node_verify(node) == "system" else _cluster_ctx()


def _node_headers(node, actor=None, role=None, perms=None):
    st = cluster.state()
    headers = {"Authorization": f"Bearer {node['token']}",
               "X-RedstonePanel-Epoch": str(st.get("epoch", 1))}
    if actor:
        headers["X-RedstonePanel-Actor"] = str(actor)[:64]
    if role:
        headers["X-RedstonePanel-Role"] = "admin" if role == "admin" else "user"
    if perms is not None:
        # 1.18: what this master user may do on the server the request is about
        headers["X-RedstonePanel-Perms"] = ",".join(sorted(perms))
    return headers


def call_node(node, method, path, body=None, actor=None, timeout=TIMEOUT, role=None):
    return _request(node["url"], method, path, node_ctx(node), _node_headers(node, actor, role), body, timeout)


# ---------------------------------------------------------------------------
# The proxy: a master user's request passed on to a node (every tab of a node's server)
# ---------------------------------------------------------------------------
ACTION_TIMEOUT = 180            # start, stop, restart on a node: a server saving its world takes a while
PROXY_TIMEOUT = 900             # deploying, updating or installing Java can take minutes
PASS_BACK = ("content-type", "content-disposition", "content-length", "cache-control")


class Proxied:
    """An open answer from a node: status, headers to pass back, and the body in chunks."""
    def __init__(self, conn, res):
        self.conn, self.res = conn, res
        self.status = res.status
        self.headers = {k: v for k, v in res.getheaders() if k.lower() in PASS_BACK}

    def read_all(self, limit=MAX_BODY):
        try:
            return self.res.read(limit)
        finally:
            self.close()

    def chunks(self):
        try:
            while True:
                chunk = self.res.read(1 << 16)
                if not chunk:
                    break
                yield chunk
        finally:
            self.close()

    def close(self):
        self.conn.close()


def open_proxy(node, method, path, body=None, length=None, content_type=None, actor=None, role="user",
               perms=None):
    """Sends one request to a node with its token and the master user's name and role. `body` is
    a file object of `length` bytes (or None). The caller streams the answer back."""
    host, port = _split(node["url"])
    headers = _node_headers(node, actor, role, perms)
    headers["Accept"] = "application/json, */*"
    if content_type:
        headers["Content-Type"] = content_type
    if body is not None:
        headers["Content-Length"] = str(length or 0)
    conn = http.client.HTTPSConnection(host, port, context=node_ctx(node), timeout=TIMEOUT)
    try:
        conn.connect()
        conn.sock.settimeout(PROXY_TIMEOUT)
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
    except ssl.SSLCertVerificationError as e:
        conn.close()
        raise NodeError("cert_error", f"Certificate problem: {e.verify_message or e}")
    except ssl.SSLError as e:
        conn.close()
        raise NodeError("cert_error", f"TLS problem: {e.reason or e}")
    except (OSError, socket.timeout, http.client.HTTPException) as e:
        conn.close()
        raise NodeError("unreachable", str(e) or e.__class__.__name__)
    return Proxied(conn, res)


def upload(node, path, chunks, length, actor=None, role=None):
    """PUTs `length` bytes from the iterable `chunks` to a node as they come (moving a server,
    1.16), without holding them in memory or on disk. Returns the node's JSON answer."""
    host, port = _split(node["url"])
    headers = _node_headers(node, actor, role)
    headers.update({"Accept": "application/json", "Content-Type": "application/octet-stream",
                    "Content-Length": str(length)})
    conn = http.client.HTTPSConnection(host, port, context=node_ctx(node), timeout=TIMEOUT)
    try:
        conn.connect()
        conn.sock.settimeout(PROXY_TIMEOUT)
        conn.request("PUT", path, body=chunks, headers=headers)
        res = conn.getresponse()
        raw = res.read(MAX_BODY + 1)
    except ssl.SSLCertVerificationError as e:
        raise NodeError("cert_error", f"Certificate problem: {e.verify_message or e}")
    except ssl.SSLError as e:
        raise NodeError("cert_error", f"TLS problem: {e.reason or e}")
    except (OSError, socket.timeout, http.client.HTTPException) as e:
        raise NodeError("unreachable", str(e) or e.__class__.__name__)
    finally:
        conn.close()
    return _answer(res.status, raw)


def _node_servers(node):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(node["id"])
        if hit and now - hit[0] < OVERVIEW_TTL:
            return hit[1]
    base = {"id": node["id"], "name": node["name"], "url": node["url"], "platform": node.get("platform"),
            "machine": node.get("machine"), "version": node.get("version"), "cert_expires": node.get("cert_expires"),
            "cluster_api": node.get("cluster_api"), "verify": cluster.node_verify(node),
            "terminal": node.get("terminal")}
    try:
        servers = call_node(node, "GET", "/api/servers")
        result = dict(base, state="ok", servers=servers if isinstance(servers, list) else [])
        if now - (node.get("last_ok") or 0) > LAST_OK_EVERY:
            fields = {"last_ok": int(now)}
            fields.update(_hello_fields(node))
            result.update({k: v for k, v in fields.items() if k in base})
            try:
                cluster.update_node(node["id"], **fields)
            except cluster.ClusterError:
                pass
    except NodeError as e:
        result = dict(base, state=e.state, error=str(e), servers=[])
    with _cache_lock:
        _cache[node["id"]] = (now, result)
    return result


def _hello_fields(node):
    """The node's version, contract and certificate expiry from /api/cluster/hello (updated
    packages change them), or {} if it doesn't answer."""
    try:
        h = call_node(node, "GET", "/api/cluster/hello")
    except NodeError:
        return {}
    out = {}
    if isinstance(h.get("version"), str):
        out["version"] = h["version"][:40]
    if isinstance(h.get("cluster_api"), int):
        out["cluster_api"] = h["cluster_api"]
    if isinstance(h.get("cert_expires"), int):
        out["cert_expires"] = h["cert_expires"]
    if cluster.clean_machine(h.get("machine")):
        out["machine"] = h["machine"]
    if h.get("platform"):
        out["platform"] = cluster.clean_system(h.get("platform"))
    out["terminal"] = cluster.clean_terminal(h.get("terminal"))      # None from a node before 1.21.10
    return out


def _node_host(node, summarize):
    key = ("host", node["id"])
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < OVERVIEW_TTL:
            return hit[1]
    base = {"id": node["id"], "name": node["name"], "url": node["url"], "version": node.get("version")}
    try:
        rep = call_node(node, "GET", "/api/host", timeout=15)
        result = dict(base, state="ok", summary=summarize(rep) if isinstance(rep, dict) else None)
    except NodeError as e:
        result = dict(base, state=e.state, error=str(e), summary=None)
    except Exception as e:                      # a node's odd answer mustn't break the page
        result = dict(base, state="error", error=f"{type(e).__name__}: {e}", summary=None)
    with _cache_lock:
        _cache[key] = (now, result)
    return result


def hosts(local_summary, summarize):
    """Every panel's host summary (core.host.summary), the master's own first."""
    try:
        local = {"id": "local", "name": panel_settings.display_name(), "local": True, "state": "ok",
                 "summary": local_summary()}
    except Exception as e:
        local = {"id": "local", "name": panel_settings.display_name(), "local": True, "state": "error",
                 "error": str(e), "summary": None}
    nodes = cluster.load_nodes()
    with ThreadPoolExecutor(max_workers=8) as pool:
        others = list(pool.map(lambda n: _node_host(n, summarize), nodes)) if nodes else []
    return {"cluster": cluster.state().get("name"), "nodes": [local] + others}


def forget_cached(node_id=None):
    with _cache_lock:
        if node_id is None:
            _cache.clear()
        else:
            _cache.pop(node_id, None)
            _cache.pop(("host", node_id), None)


def overview(list_local_servers):
    """Every node's servers, the master's own first. list_local_servers is manager.list_servers."""
    st = cluster.state()
    try:
        local = {"id": "local", "name": panel_settings.display_name(), "state": "ok", "local": True,
                 "servers": list_local_servers()}
    except Exception as e:
        local = {"id": "local", "name": panel_settings.display_name(), "state": "error", "local": True,
                 "error": str(e), "servers": []}
    nodes = cluster.load_nodes()
    with ThreadPoolExecutor(max_workers=8) as pool:
        others = list(pool.map(_node_servers, nodes)) if nodes else []
    return {"cluster": st.get("name"), "epoch": st.get("epoch"), "nodes": [local] + others}


# ---------------------------------------------------------------------------
# Node -> master (joining only)
# ---------------------------------------------------------------------------
def fetch_master_ca(master_url):
    """The master's cluster CA. Not verified here: the caller compares its fingerprint with
    the one in the join code before trusting it."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        data = _request(master_url, "GET", "/api/cluster/ca", ctx)
    except NodeError as e:
        raise cluster.ClusterError(f"Could not reach the master at {master_url}: {e}")
    pem = data.get("ca") if isinstance(data, dict) else None
    if not isinstance(pem, str) or "BEGIN CERTIFICATE" not in pem:
        raise cluster.ClusterError(f"{master_url} doesn't look like a RedstonePanel cluster master.")
    return pem


def post_join(master_url, ca_pem, body):
    # The cluster CA, plus the system's trust store for a master that serves its own public
    # (certbot) certificate. check_hostname stays on either way.
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cadata=ca_pem)
    try:
        data = _request(master_url, "POST", "/api/cluster/join", ctx, body=body, timeout=30)
    except NodeError as e:
        if e.state == "cert_error":
            raise cluster.ClusterError(f"The master's certificate doesn't cover {master_url} ({e}). "
                                       "Make the join code with the master's IP address.")
        raise cluster.ClusterError(f"The master refused: {e}")
    if not all(k in data for k in ("cluster_id", "cluster_name", "epoch", "master_name", "node_id", "cert", "ca")):
        raise cluster.ClusterError("The master's answer was incomplete.")
    return data


# ---------------------------------------------------------------------------
# Certificate renewal and the master's housekeeping (run.py starts maintenance())
# ---------------------------------------------------------------------------
def renew_node(node, actor):
    """Asks a node for a fresh CSR, signs it and installs the certificate (the node restarts).
    Returns the new expiry."""
    req = call_node(node, "POST", "/api/cluster/cert/csr", actor=actor)
    pem, sans = cluster.sign_renewal(node, req)
    call_node(node, "PUT", "/api/cluster/cert", {"cert": pem}, actor=actor)
    expires = cluster.cert_expires_pem(pem)
    cluster.update_node(node["id"], sans=sans, cert_expires=expires, renew_error=None)
    forget_cached(node["id"])
    return expires


MAINTENANCE_EVERY = 3600


def maintain_once(log, restart, now=None):
    """One pass on a master: renew node certificates that expire within 30 days (and this
    master's own cluster certificate, which needs a restart), then the nightly backup."""
    if cluster.role() != "master":
        return
    now = time.time() if now is None else now
    for node in cluster.load_nodes():
        if cluster.node_verify(node) == "system":
            continue
        fields = _hello_fields(node)
        if fields:
            try:
                cluster.update_node(node["id"], **fields)
            except cluster.ClusterError:
                pass
        exp = fields.get("cert_expires", node.get("cert_expires"))
        if not exp or exp - now > cluster.RENEW_WITHIN:
            continue
        try:
            expires = renew_node(node, "automatic renewal")
            log(f"[CLUSTER] Renewed the certificate of node '{node['name']}' automatically "
                f"(it expires {time.strftime('%Y-%m-%d', time.localtime(exp))}; now "
                f"{time.strftime('%Y-%m-%d', time.localtime(expires))}). The node restarts.")
        except (NodeError, cluster.ClusterError) as e:
            try:
                cluster.update_node(node["id"], renew_error=f"{time.strftime('%Y-%m-%d %H:%M')}: {e}")
            except cluster.ClusterError:
                pass
            log(f"[CLUSTER] Automatic renewal of node '{node['name']}' failed: {e}")
    if panel_settings.load()["https_mode"] == "cluster":
        own = cluster.cert_expires(cluster.node_crt())
        if own and own - now < cluster.RENEW_WITHIN:
            try:
                cluster.renew_own_cert()
                log("[CLUSTER] Renewed this master's certificate automatically; restarting the panel.")
                restart()
            except cluster.ClusterError as e:
                log(f"[CLUSTER] Automatic renewal of this master's certificate failed: {e}")
    if cluster.nightly_due(now):
        try:
            name = cluster.run_nightly(now)
            log(f"[CLUSTER] Nightly cluster backup written: {name}.")
        except cluster.ClusterError as e:
            log(f"[CLUSTER] {e}")


def maintenance(log, restart, every=MAINTENANCE_EVERY, first=120):
    """Runs maintain_once in a daemon thread: shortly after start, then every hour."""
    def loop():
        time.sleep(first)
        while True:
            try:
                maintain_once(log, restart)
            except Exception as e:          # never let housekeeping take the panel down
                log(f"[CLUSTER] Cluster housekeeping failed: {type(e).__name__}: {e}")
            time.sleep(every)
    t = threading.Thread(target=loop, name="cluster-maintenance", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# 1.21.10: a terminal on a node. The master opens a websocket to the node's
# /api/terminal/node-ws with its token; the node shows its own login prompt.
# ---------------------------------------------------------------------------
NODE_TERMINAL_PATH = "/api/terminal/node-ws"


class WebSocketLink:
    """A websocket to a node, with the same shape as the browser's side in core/terminal.py."""

    def __init__(self, ws):
        self.ws = ws

    async def recv(self):
        try:
            msg = await self.ws.recv()
        except Exception:                        # noqa: BLE001  (closed, either way)
            return ("closed", None)
        return ("bytes", msg) if isinstance(msg, (bytes, bytearray)) else ("text", msg)

    async def send_bytes(self, data):
        await self.ws.send(bytes(data))

    async def send_text(self, text):
        await self.ws.send(text)

    async def close(self, code=1000):
        await self.ws.close(code)


async def open_terminal(node, actor, client_ip):
    """A WebSocketLink to the node's terminal, or raises NodeError."""
    try:
        import websockets
    except ImportError:
        raise NodeError("error", "This panel can't open websockets (python3-websockets is missing).")
    scheme_host = node["url"].replace("https://", "wss://", 1)
    headers = dict(_node_headers(node, actor, "admin"), **{"X-RedstonePanel-Client-IP": str(client_ip or "")[:64]})
    try:
        try:
            from websockets.asyncio.client import connect        # websockets 13 and later
            ws = await connect(scheme_host + NODE_TERMINAL_PATH, ssl=node_ctx(node), additional_headers=headers,
                               max_size=1 << 20, open_timeout=10)
        except ImportError:
            ws = await websockets.connect(scheme_host + NODE_TERMINAL_PATH, ssl=node_ctx(node),     # Ubuntu 24.04: 10.4
                                          extra_headers=headers, max_size=1 << 20, open_timeout=10)
    except ssl.SSLError as e:
        raise NodeError("cert_error", f"{node['name']}'s certificate couldn't be checked ({e}).")
    except OSError as e:
        raise NodeError("unreachable", f"{node['name']} can't be reached ({e}).")
    except Exception as e:                       # noqa: BLE001  (refused: the node said no)
        raise NodeError("rejected", f"{node['name']} refused the terminal ({e}).")
    return WebSocketLink(ws)
