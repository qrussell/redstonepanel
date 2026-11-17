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
    def __init__(self, state, message):
        super().__init__(message)
        self.state = state


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
    if len(raw) > MAX_BODY:
        raise NodeError("error", "The answer was too large.")
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        payload = {}
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if res.status == 401:
        raise NodeError("rejected", detail or "Not accepted (token revoked, node left the cluster, or a newer master).")
    if res.status == 403:
        raise NodeError("too_old", detail or "Refused: this node may need a newer RedstonePanel.")
    if res.status >= 400:
        raise NodeError("error", detail or f"HTTP {res.status}")
    return payload


# ---------------------------------------------------------------------------
# Master -> node
# ---------------------------------------------------------------------------
def _cluster_ctx():
    ctx = ssl.create_default_context(cafile=cluster.ca_crt())   # checks the host name too
    return ctx


def call_node(node, method, path, body=None, actor=None, timeout=TIMEOUT):
    st = cluster.state()
    headers = {"Authorization": f"Bearer {node['token']}",
               "X-RedstonePanel-Epoch": str(st.get("epoch", 1))}
    if actor:
        headers["X-RedstonePanel-Actor"] = str(actor)[:64]
    return _request(node["url"], method, path, _cluster_ctx(), headers, body, timeout)


def _node_servers(node):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(node["id"])
        if hit and now - hit[0] < OVERVIEW_TTL:
            return hit[1]
    base = {"id": node["id"], "name": node["name"], "url": node["url"], "platform": node.get("platform"),
            "version": node.get("version"), "cert_expires": node.get("cert_expires")}
    try:
        servers = call_node(node, "GET", "/api/servers")
        result = dict(base, state="ok", servers=servers if isinstance(servers, list) else [])
        if now - (node.get("last_ok") or 0) > LAST_OK_EVERY:
            try:
                cluster.update_node(node["id"], last_ok=int(now))
            except cluster.ClusterError:
                pass
    except NodeError as e:
        result = dict(base, state=e.state, error=str(e), servers=[])
    with _cache_lock:
        _cache[node["id"]] = (now, result)
    return result


def forget_cached(node_id=None):
    with _cache_lock:
        if node_id is None:
            _cache.clear()
        else:
            _cache.pop(node_id, None)


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
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cadata=ca_pem)       # check_hostname stays on
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
