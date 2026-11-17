"""RedstonePanel clusters: one master panel controls the game servers of other panels (nodes).

Design: docs/design/fleet.md. In short:
  * Every panel is "standalone" (the default), "master" or "node" (data/cluster/cluster.json).
  * The master runs a small certificate authority (the cluster CA) and issues every member's
    HTTPS certificate, so cluster traffic is always HTTPS checked against that CA.
  * A node joins with a single-use join code made on the master. The node creates a node token
    (it keeps only a scrypt hash) and hands it to the master, which sends it as a Bearer token.
  * A node token can only reach the routes in NODE_ROUTES. Never users, settings, sign-in or
    deleting servers.
  * The cluster epoch goes up when a node is promoted to master from a backup; nodes refuse a
    master with a lower epoch, so an old master that comes back can't control anything.

Only the Python standard library and the openssl command are used.
"""
import base64
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import ssl
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid

from core import auth, tls
from core import settings as panel_settings
from core.settings import DATA_DIR
from core.version import panel_version

CLUSTER_DIR = os.path.join(DATA_DIR, "cluster")

CLUSTER_API = 1                 # the master<->node contract version (see hello())
JOIN_TTL = 3600                 # join codes are single-use and valid for an hour
CERT_DAYS = 825
CA_DAYS = 3650
RENEW_WITHIN = 30 * 86400
TOKEN_CACHE_SECONDS = 60
CODE_PREFIX = "rpjoin1:"
BACKUP_FORMAT = 1
MIN_PASSPHRASE = 10

# What a cluster certificate may name. These are also the cluster CA's X.509 name constraints,
# so a CA imported into a browser can only vouch for private addresses and local names, never
# for a public website.
PERMITTED_DNS = ("local", "lan", "home.arpa", "internal")
PERMITTED_NETS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")

# Routes the master's node token may use on a node: (method, path regex). Everything else is
# refused, including /api/users*, /api/settings*, /api/auth/* and DELETE /api/servers/{name}.
NODE_ROUTES = (
    ("GET", r"/api/cluster/hello"),
    ("DELETE", r"/api/cluster/membership"),
    ("POST", r"/api/cluster/cert/csr"),
    ("PUT", r"/api/cluster/cert"),
    ("GET", r"/api/servers"),
    ("GET", r"/api/logs"),
    ("GET", r"/api/templates"),
    ("POST", r"/api/servers/[^/]+/action"),
)

_lock = threading.RLock()
_token_cache = {}               # sha256(token) -> cache expiry


class ClusterError(ValueError):
    """A cluster request that can't be done (message is safe to show)."""


class Throttled(ClusterError):
    """Too many failed attempts from this address."""


def _path(name):
    return os.path.join(CLUSTER_DIR, name)


def ca_crt():
    return _path("ca.crt")


def node_crt():
    return _path("node.crt")


def node_key():
    return _path("node.key")


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def _ensure_dir():
    os.makedirs(CLUSTER_DIR, exist_ok=True)
    os.chmod(CLUSTER_DIR, 0o700)


def _read_json(name, default):
    try:
        with open(_path(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_bytes(path, data, mode=0o600):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _write_json(name, data):
    _ensure_dir()
    _write_bytes(_path(name), json.dumps(data, indent=2).encode())


def state():
    """This panel's role and cluster details: {"role": "standalone"} until it creates or joins one."""
    st = _read_json("cluster.json", None)
    return st if isinstance(st, dict) and st.get("role") in ("master", "node") else {"role": "standalone"}


def role():
    return state()["role"]


def load_nodes():
    data = _read_json("nodes.json", {})
    return data.get("nodes", []) if isinstance(data, dict) else []


def _save_nodes(nodes):
    _write_json("nodes.json", {"nodes": nodes})


def _load_joins():
    data = _read_json("joins.json", {})
    return data.get("joins", []) if isinstance(data, dict) else []


def _save_joins(joins):
    _write_json("joins.json", {"joins": joins})


def find_node(node_id):
    return next((n for n in load_nodes() if n["id"] == node_id), None)


def public_node(n):
    """A node record without its token."""
    return {k: n.get(k) for k in ("id", "name", "url", "platform", "version", "cluster_api",
                                  "cert_expires", "added", "added_by", "last_ok", "sans")}


# ---------------------------------------------------------------------------
# Names and addresses a cluster certificate may carry
# ---------------------------------------------------------------------------
def allowed_name(name):
    n = (name or "").strip().lower().rstrip(".")
    return bool(panel_settings.HOSTNAME_RE.match(n)) and any(n == d or n.endswith("." + d) for d in PERMITTED_DNS)


def _ip(text):
    try:
        ip = ipaddress.ip_address(str(text).strip("[]"))
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def allowed_ip(text):
    ip = _ip(text)
    return bool(ip) and ip.version == 4 and any(ip in ipaddress.ip_network(n) for n in PERMITTED_NETS)


def own_identity():
    """This host's names and addresses that a cluster certificate can cover."""
    ident = tls.identity()
    names = set(ident["names"]) | set(panel_settings.load()["extra_hosts"])
    return {"names": sorted(n.lower() for n in names if allowed_name(n)),
            "ips": sorted({str(_ip(i)) for i in ident["ips"] if allowed_ip(i)})}


def _clean_sans(names, ips):
    if not isinstance(names, list) or not isinstance(ips, list):
        raise ClusterError("Names and addresses must be lists.")
    return ({str(n).strip().lower().rstrip(".") for n in names if allowed_name(str(n))},
            {str(_ip(i)) for i in ips if allowed_ip(i)})


# ---------------------------------------------------------------------------
# Join codes and tokens
# ---------------------------------------------------------------------------
def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_token(prefix):
    return prefix + _b64(secrets.token_bytes(32))


def encode_code(url, secret, ca_fp):
    return CODE_PREFIX + _b64(json.dumps({"u": url, "s": secret, "ca": ca_fp}, separators=(",", ":")).encode())


def decode_code(code):
    """{"u", "s", "ca"} from a join code, or ClusterError."""
    code = "".join((code or "").split())   # pasted codes may pick up line breaks
    if not code.startswith(CODE_PREFIX):
        raise ClusterError("That isn't a RedstonePanel join code (it should start with rpjoin1:).")
    try:
        data = json.loads(_unb64(code[len(CODE_PREFIX):]))
    except Exception:
        raise ClusterError("The join code is damaged. Copy it again from the master.")
    if not (isinstance(data, dict) and isinstance(data.get("u"), str) and data["u"].startswith("https://")
            and isinstance(data.get("s"), str) and data["s"].startswith("rpj_")
            and isinstance(data.get("ca"), str) and data["ca"].startswith("sha256:")):
        raise ClusterError("The join code is damaged. Copy it again from the master.")
    return data


def ca_fingerprint(pem):
    return "sha256:" + hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def read_ca():
    try:
        with open(ca_crt()) as f:
            return f.read()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# openssl helpers
# ---------------------------------------------------------------------------
def _openssl(args, data=None, env=None):
    r = subprocess.run(["openssl"] + args, capture_output=True, input=data, env=env)
    if r.returncode != 0:
        lines = (r.stderr + r.stdout).decode(errors="replace").strip().splitlines()
        detail = [x for x in lines if "error" in x.lower()][:2] or lines[-1:]
        raise ClusterError(f"openssl: {'; '.join(detail) or 'failed'}")
    return r.stdout


def _subject_text(name):
    """A certificate subject that openssl's -subj accepts and doesn't look like a host name."""
    return re.sub(r"[^A-Za-z0-9 _.-]", "", name)[:40].strip() or "cluster"


def _create_ca(cluster_name):
    nc = ["critical"] + [f"permitted;DNS:{d}" for d in PERMITTED_DNS]
    for net in PERMITTED_NETS:
        n = ipaddress.ip_network(net)
        nc.append(f"permitted;IP:{n.network_address}/{n.netmask}")
    _openssl(["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", _path("ca.key")])
    os.chmod(_path("ca.key"), 0o600)
    _openssl(["req", "-x509", "-new", "-key", _path("ca.key"), "-sha256", "-days", str(CA_DAYS),
              "-subj", f"/CN=RedstonePanel Cluster CA ({_subject_text(cluster_name)})",
              "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
              "-addext", "keyUsage=critical,keyCertSign,cRLSign",
              "-addext", "nameConstraints=" + ",".join(nc),
              "-out", ca_crt()])
    os.chmod(ca_crt(), 0o644)


def new_key_and_csr(key_path):
    """Writes a new EC key to key_path (0600) and returns a CSR for it (PEM text)."""
    old = os.umask(0o077)
    try:
        _openssl(["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", key_path])
    finally:
        os.umask(old)
    # The subject has a space in it on purpose: openssl checks a host-name-like CN against the
    # CA's name constraints, and the names that matter are in subjectAltName anyway.
    return _openssl(["req", "-new", "-key", key_path, "-subj", "/CN=RedstonePanel node"]).decode()


def sign_csr(csr_pem, names, ips):
    """Signs a CSR with the cluster CA for exactly these names/IPs (the CSR's own extensions are
    ignored). Returns the certificate (PEM text)."""
    if not names and not ips:
        raise ClusterError("No private address or local name to put in the certificate.")
    if not isinstance(csr_pem, str) or "BEGIN CERTIFICATE REQUEST" not in csr_pem or len(csr_pem) > 8192:
        raise ClusterError("Bad certificate request.")
    san = ",".join([f"DNS:{n}" for n in sorted(names)] + [f"IP:{i}" for i in sorted(ips)])
    with tempfile.TemporaryDirectory() as tmp:
        csr, ext, out = (os.path.join(tmp, x) for x in ("node.csr", "ext.cnf", "node.crt"))
        with open(csr, "w") as f:
            f.write(csr_pem)
        _openssl(["req", "-verify", "-noout", "-in", csr])          # the requester holds the key
        with open(ext, "w") as f:
            f.write("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                    f"extendedKeyUsage=serverAuth\nsubjectAltName={san}\n")
        _openssl(["x509", "-req", "-in", csr, "-CA", ca_crt(), "-CAkey", _path("ca.key"),
                  "-set_serial", "0x" + os.urandom(16).hex(), "-days", str(CERT_DAYS),
                  "-sha256", "-extfile", ext, "-out", out])
        with open(out) as f:
            return f.read()


def cert_expires(path):
    """Expiry of a certificate file as a Unix time, or None."""
    try:
        out = _openssl(["x509", "-in", path, "-noout", "-enddate"]).decode().strip()
        return int(ssl.cert_time_to_seconds(out.split("=", 1)[1]))
    except (ClusterError, ValueError, IndexError):
        return None


def cert_expires_pem(pem):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "c.crt")
        with open(path, "w") as f:
            f.write(pem)
        return cert_expires(path)


CLOCK_SKEW = 600


def verify_args(cert_path):
    """`openssl verify` arguments for a cluster certificate. Certificates start at the moment they
    are signed (OpenSSL 3.0 can't backdate them), so checking right away fails as "not yet valid"
    whenever the signer's clock is slightly ahead. Checking at a time CLOCK_SKEW seconds ahead
    tolerates that; real expiry is tracked separately (cert_expires)."""
    return ["verify", "-attime", str(int(time.time()) + CLOCK_SKEW), "-CAfile", ca_crt(), cert_path]


def _check_cert(cert_path, key_path):
    """The certificate chains to the cluster CA and matches the key."""
    _openssl(verify_args(cert_path))
    problem = tls.check_pair(cert_path, key_path)
    if problem:
        raise ClusterError(problem)


def _issue_own_cert():
    """Master only: (re)issues this panel's own certificate for this host's names."""
    ident = own_identity()
    key_new = node_key() + ".new"
    csr = new_key_and_csr(key_new)
    pem = sign_csr(csr, set(ident["names"]), set(ident["ips"]))
    _write_bytes(node_crt() + ".new", pem.encode(), 0o644)
    _check_cert(node_crt() + ".new", key_new)
    os.replace(key_new, node_key())
    os.replace(node_crt() + ".new", node_crt())
    return ident


def serving_cert():
    """(cert, key) for https_mode "cluster". A master renews its own certificate when it is
    about to expire; a node's is renewed by its master."""
    if role() == "master":
        exp = cert_expires(node_crt())
        if exp is None or exp - time.time() < RENEW_WITHIN:
            with _lock:
                _issue_own_cert()
    if not (os.path.exists(node_crt()) and os.path.exists(node_key())):
        raise ClusterError("This panel's cluster certificate is missing.")
    return node_crt(), node_key()


def _taken_sans(except_node=None):
    """Names/IPs already in use by the master or another node."""
    taken = set()
    ident = own_identity()
    taken |= set(ident["names"]) | set(ident["ips"])
    for n in load_nodes():
        if n["id"] != except_node:
            sans = n.get("sans") or {}
            taken |= set(sans.get("names", [])) | set(sans.get("ips", []))
    return taken


def _without_taken(names, ips, address, except_node=None):
    """The names/IPs for a node's certificate. `address` (how the master reaches the node) must
    be its own; other names and addresses already held by the master or another node are left
    out rather than refused, because hosts often share some (Docker's 172.17.0.1, for example)."""
    taken = _taken_sans(except_node)
    if address in taken:
        raise ClusterError(f"{address} is already used by the master or another node. "
                           "Each node needs its own address.")
    names, ips = set(names) - taken, set(ips) - taken
    (ips if allowed_ip(address) else names).add(address)
    return names, ips


# ---------------------------------------------------------------------------
# Node side: the master's token
# ---------------------------------------------------------------------------
def path_allowed(method, path):
    return any(method == m and re.fullmatch(p, path) for m, p in NODE_ROUTES)


def _token_ok(token, st):
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    if _token_cache.get(key, 0) > now:
        return True
    if auth.verify_password(token, st.get("token_hash", "")):
        _token_cache.clear()          # a node holds one token at a time
        _token_cache[key] = now + TOKEN_CACHE_SECONDS
        return True
    return False


def read_node_token(token, epoch_header, ip):
    """The identity of the cluster's master, or None. Counts failures toward the sign-in throttle."""
    st = state()
    keys = (f"ip:{ip}",)
    if st["role"] != "node" or not token.startswith("rpn_") or len(token) > 100:
        return None
    if auth._throttled(keys):
        return None
    if not _token_ok(token, st):
        auth._record_fail(keys)
        return None
    try:
        epoch = int(epoch_header)
    except (TypeError, ValueError):
        return None
    with _lock:
        st = state()
        if st.get("role") != "node" or epoch < int(st.get("epoch", 1)):
            return None                 # an old master after a promotion
        now = int(time.time())
        changed = epoch > int(st.get("epoch", 1))
        if changed:
            st["epoch"] = epoch
        if changed or now - int(st.get("last_used") or 0) > 60 or st.get("last_ip") != ip:
            st.update(last_used=now, last_ip=ip)
            _write_json("cluster.json", st)
    return {"username": f"master:{st.get('master_name', '?')}", "source": "cluster", "role": "user"}


def hello():
    st = state()
    return {"name": panel_settings.display_name(), "version": panel_version(), "cluster_api": CLUSTER_API,
            "platform": "linux", "cluster_id": st.get("cluster_id"), "cert_expires": cert_expires(node_crt())}


# ---------------------------------------------------------------------------
# Creating, joining and leaving
# ---------------------------------------------------------------------------
def _require(want):
    have = role()
    if have == want:
        return
    if want == "standalone":
        raise ClusterError(f"This panel is already a cluster {have}. Leave the cluster first.")
    if want == "master":
        raise ClusterError("Only the cluster's master can do this.")
    raise ClusterError("This panel isn't a cluster node.")


def _no_custom_cert():
    if panel_settings.load()["https_mode"] == "custom":
        raise ClusterError("This panel uses its own HTTPS certificate. Clusters don't support that yet: "
                           "switch to HTTP or self-signed HTTPS in Panel Settings first.")


def create_cluster(name, actor):
    """Makes this panel a cluster master. The caller restarts the panel (HTTPS changes)."""
    name = panel_settings.clean_panel_name(name)
    if not name:
        raise ClusterError("Give the cluster a name.")
    with _lock:
        _require("standalone")
        _no_custom_cert()
        if not own_identity()["ips"]:
            raise ClusterError("This server has no private network address (10.x, 172.16-31.x, 192.168.x "
                               "or 100.64-127.x), which cluster certificates need.")
        shutil.rmtree(CLUSTER_DIR, ignore_errors=True)
        _ensure_dir()
        try:
            _create_ca(name)
            _issue_own_cert()
            st = {"role": "master", "cluster_id": str(uuid.uuid4()), "name": name, "epoch": 1,
                  "created": int(time.time()), "created_by": actor}
            _save_nodes([])
            _save_joins([])
            _write_json("cluster.json", st)
        except Exception:
            shutil.rmtree(CLUSTER_DIR, ignore_errors=True)
            raise
        _set_https("cluster")
        return st


def _set_https(mode):
    s = panel_settings.load()
    s["https_mode"] = mode
    panel_settings.save(s)


def dissolve(actor):
    """Master with no nodes left: back to standalone (self-signed HTTPS)."""
    with _lock:
        _require("master")
        if load_nodes():
            raise ClusterError("Remove the nodes first.")
        shutil.rmtree(CLUSTER_DIR, ignore_errors=True)
        _set_https("self_signed")


def default_join_url(host_header):
    """The master's address for join codes: what the admin's browser used if the cluster
    certificate covers it, otherwise this host's first private address."""
    s = panel_settings.load()
    ident = own_identity()
    host = panel_settings.host_only(host_header)
    if host not in ident["names"] and host not in ident["ips"]:
        host = ident["ips"][0] if ident["ips"] else host
    port = "" if s["https_port"] == 443 else f":{s['https_port']}"
    return f"https://{host}{port}"


def create_join(actor, url):
    """A single-use join code (valid JOIN_TTL seconds). Returns {id, code, command, expires}."""
    url = (url or "").strip().rstrip("/")
    if not re.fullmatch(r"https://[A-Za-z0-9.\-\[\]:]+", url):
        raise ClusterError("The master address must look like https://192.168.1.10:8443")
    with _lock:
        _require("master")
        secret = "rpj_" + secrets.token_hex(4) + _b64(secrets.token_bytes(32))
        now = int(time.time())
        joins = [j for j in _load_joins() if j["expires"] > now]
        join = {"id": secret[4:12], "hash": auth.hash_password(secret), "expires": now + JOIN_TTL,
                "created_by": actor, "url": url}
        joins.append(join)
        _save_joins(joins)
    code = encode_code(url, secret, ca_fingerprint(read_ca()))
    return {"id": join["id"], "code": code, "expires": join["expires"],
            "command": f"sudo redstonepanel join '{code}'"}


def list_joins():
    now = int(time.time())
    return [{"id": j["id"], "expires": j["expires"], "created_by": j.get("created_by"), "url": j.get("url")}
            for j in _load_joins() if j["expires"] > now]


def cancel_join(join_id):
    with _lock:
        _save_joins([j for j in _load_joins() if j["id"] != join_id])


def accept_join(body, client_ip):
    """Master: a node presents a join secret. Returns what the node needs to finish joining."""
    keys = (f"ip:{client_ip}",)
    if auth._throttled(keys):
        raise Throttled("Too many failed attempts. Try again in a few minutes.")
    if not isinstance(body, dict):
        raise ClusterError("Bad request.")
    secret = str(body.get("secret") or "")
    with _lock:
        _require("master")
        now = int(time.time())
        joins = _load_joins()
        join = next((j for j in joins if j["id"] == secret[4:12] and j["expires"] > now), None)
        if not join or not auth.verify_password(secret, join["hash"]):
            auth._record_fail(keys)
            raise ClusterError("The join code is wrong, already used or expired. Make a new one on the master.")
        _save_joins([j for j in joins if j is not join and j["expires"] > now])   # single use

        try:
            name = panel_settings.clean_panel_name(body.get("name")) or "node"
        except panel_settings.SettingsError as e:
            raise ClusterError(str(e))
        token = str(body.get("token") or "")
        if not re.fullmatch(r"rpn_[A-Za-z0-9_-]{40,60}", token):
            raise ClusterError("Bad node token.")
        try:
            port = int(body.get("https_port"))
            api = int(body.get("cluster_api"))
        except (TypeError, ValueError):
            raise ClusterError("Bad request.")
        if not 1 <= port <= 65535:
            raise ClusterError("Bad HTTPS port.")
        if api < CLUSTER_API:
            raise ClusterError("Update RedstonePanel on this server before joining the cluster.")
        ip = _ip(client_ip)
        if not allowed_ip(client_ip):
            raise ClusterError(f"Nodes must reach the master from a private address; this one came from {client_ip}.")
        names, ips = _clean_sans(body.get("names", []), body.get("ips", []))
        names, ips = _without_taken(names, ips, str(ip))
        cert = sign_csr(body.get("csr"), names, ips)
        expires = cert_expires_pem(cert)

        nodes = load_nodes()
        node = {"id": "n-" + secrets.token_hex(3), "name": name, "url": f"https://{ip}:{port}",
                "token": token, "sans": {"names": sorted(names), "ips": sorted(ips)},
                "cert_expires": expires, "platform": str(body.get("platform") or "linux")[:20],
                "version": str(body.get("version") or "unknown")[:40], "cluster_api": api,
                "added": now, "added_by": join.get("created_by"), "last_ok": None}
        nodes.append(node)
        _save_nodes(nodes)
        st = state()
    return {"cluster_id": st["cluster_id"], "cluster_name": st["name"], "epoch": st["epoch"],
            "master_name": panel_settings.display_name(), "node_id": node["id"],
            "cert": cert, "ca": read_ca()}, node


def join(code, actor, client):
    """Node: joins the cluster in the join code. `client` is core.cluster_client (passed in to keep
    this module free of network code). The caller restarts the panel."""
    c = decode_code(code)
    with _lock:
        _require("standalone")
        _no_custom_cert()
        ca_pem = client.fetch_master_ca(c["u"])
        if ca_fingerprint(ca_pem) != c["ca"]:
            raise ClusterError("The master's certificate doesn't match the join code. Check the address, "
                               "or make a new code on the master.")
        shutil.rmtree(CLUSTER_DIR, ignore_errors=True)
        _ensure_dir()
        try:
            _write_bytes(ca_crt(), ca_pem.encode(), 0o644)
            token = new_token("rpn_")
            key_new = node_key() + ".new"
            csr = new_key_and_csr(key_new)
            ident = own_identity()
            s = panel_settings.load()
            resp = client.post_join(c["u"], ca_pem, {
                "secret": c["s"], "name": panel_settings.display_name(s), "token": token, "csr": csr,
                "names": ident["names"], "ips": ident["ips"], "https_port": s["https_port"],
                "version": panel_version(), "cluster_api": CLUSTER_API, "platform": "linux"})
            if ca_fingerprint(resp.get("ca", "")) != c["ca"]:
                raise ClusterError("The master answered with a different certificate authority.")
            _write_bytes(node_crt() + ".new", resp["cert"].encode(), 0o644)
            _check_cert(node_crt() + ".new", key_new)
            os.replace(key_new, node_key())
            os.replace(node_crt() + ".new", node_crt())
            st = {"role": "node", "cluster_id": resp["cluster_id"], "cluster_name": resp["cluster_name"],
                  "master_name": resp["master_name"], "master_url": c["u"], "node_id": resp["node_id"],
                  "epoch": int(resp["epoch"]), "token_hash": auth.hash_password(token),
                  "joined": int(time.time()), "joined_by": actor, "last_used": None, "last_ip": None}
            _write_json("cluster.json", st)
        except Exception:
            shutil.rmtree(CLUSTER_DIR, ignore_errors=True)
            raise
        _set_https("cluster")
        return st


def leave():
    """Node: back to standalone with self-signed HTTPS. The caller restarts the panel."""
    with _lock:
        _require("node")
        shutil.rmtree(CLUSTER_DIR, ignore_errors=True)
        _token_cache.clear()
        if panel_settings.load()["https_mode"] == "cluster":
            _set_https("self_signed")


def forget_node(node_id):
    with _lock:
        nodes = load_nodes()
        if not any(n["id"] == node_id for n in nodes):
            raise ClusterError("No such node.")
        _save_nodes([n for n in nodes if n["id"] != node_id])


def update_node(node_id, name=None, url=None, **fields):
    with _lock:
        nodes = load_nodes()
        n = next((x for x in nodes if x["id"] == node_id), None)
        if not n:
            raise ClusterError("No such node.")
        if name is not None:
            try:
                n["name"] = panel_settings.clean_panel_name(name) or n["name"]
            except panel_settings.SettingsError as e:
                raise ClusterError(str(e))
        if url is not None:
            url = url.strip().rstrip("/")
            if not re.fullmatch(r"https://[A-Za-z0-9.\-\[\]:]+", url):
                raise ClusterError("The node address must look like https://192.168.1.20:8443")
            n["url"] = url
        n.update(fields)
        _save_nodes(nodes)
        return n


# ---------------------------------------------------------------------------
# Certificate renewal
# ---------------------------------------------------------------------------
def cert_csr():
    """Node: a CSR for a fresh key (kept as node.key.new until the master sends the certificate)."""
    with _lock:
        _require("node")
        ident = own_identity()
        csr = new_key_and_csr(node_key() + ".new")
    return {"csr": csr, "names": ident["names"], "ips": ident["ips"]}


def install_cert(pem):
    """Node: installs the certificate the master signed for node.key.new. The caller restarts."""
    if not isinstance(pem, str) or "BEGIN CERTIFICATE" not in pem:
        raise ClusterError("Bad certificate.")
    with _lock:
        _require("node")
        key_new = node_key() + ".new"
        if not os.path.exists(key_new):
            raise ClusterError("No certificate request is pending.")
        _write_bytes(node_crt() + ".new", pem.encode(), 0o644)
        _check_cert(node_crt() + ".new", key_new)
        os.replace(key_new, node_key())
        os.replace(node_crt() + ".new", node_crt())


def sign_renewal(node, req):
    """Master: signs a node's renewal request. The node keeps the address the master uses."""
    names, ips = _clean_sans(req.get("names", []), req.get("ips", []))
    host = panel_settings.host_only(node["url"].split("://", 1)[1])
    if not (allowed_ip(host) or allowed_name(host)):
        raise ClusterError(f"The node's address {host} can't be in a cluster certificate.")
    names, ips = _without_taken(names, ips, str(_ip(host)) if allowed_ip(host) else host, except_node=node["id"])
    return sign_csr(req.get("csr"), names, ips), {"names": sorted(names), "ips": sorted(ips)}


def renew_own_cert():
    with _lock:
        _require("master")
        _issue_own_cert()


# ---------------------------------------------------------------------------
# Backup and promotion
# ---------------------------------------------------------------------------
BACKUP_FILES = {"cluster/cluster.json": lambda: _path("cluster.json"),
                "cluster/nodes.json": lambda: _path("nodes.json"),
                "cluster/ca.key": lambda: _path("ca.key"),
                "cluster/ca.crt": ca_crt,
                "users.json": lambda: auth.USERS_FILE,
                "templates.json": lambda: os.path.join(DATA_DIR, "templates.json")}


def _crypt(data, passphrase, decrypt=False):
    env = dict(os.environ, RP_BACKUP_PASS=passphrase)
    args = ["enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-pass", "env:RP_BACKUP_PASS"]
    if decrypt:
        args.append("-d")
    try:
        return _openssl(args, data, env)
    except ClusterError:
        raise ClusterError("Wrong passphrase, or not a RedstonePanel cluster backup.") if decrypt else \
            ClusterError("Could not encrypt the backup.")


def make_backup(passphrase, actor):
    """Master: the cluster's whole state (CA, nodes and their tokens, users, templates),
    encrypted with a passphrase. Returns bytes."""
    if not passphrase or len(passphrase) < MIN_PASSPHRASE:
        raise ClusterError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters.")
    with _lock:
        st = state()
        _require("master")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            manifest = json.dumps({"format": BACKUP_FORMAT, "cluster_id": st["cluster_id"], "cluster": st["name"],
                                   "master": panel_settings.display_name(), "epoch": st["epoch"],
                                   "created": int(time.time()), "created_by": actor}).encode()
            entries = [("manifest.json", manifest)]
            for arc, src in BACKUP_FILES.items():
                try:
                    with open(src(), "rb") as f:
                        entries.append((arc, f.read()))
                except FileNotFoundError:
                    pass
            for arc, data in entries:
                info = tarfile.TarInfo(arc)
                info.size, info.mode, info.mtime = len(data), 0o600, int(time.time())
                tar.addfile(info, io.BytesIO(data))
    return _crypt(buf.getvalue(), passphrase)


def read_backup(data, passphrase):
    """{archive name: bytes} from an encrypted backup (only the known file names are read)."""
    plain = _crypt(data, passphrase, decrypt=True)
    out = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
            for name in ["manifest.json", *BACKUP_FILES]:
                try:
                    m = tar.getmember(name)
                except KeyError:
                    continue
                if m.isfile() and m.size < 20 * 1024 * 1024:
                    out[name] = tar.extractfile(m).read()
    except tarfile.TarError:
        raise ClusterError("The backup is damaged.")
    try:
        manifest = json.loads(out["manifest.json"])
        st = json.loads(out["cluster/cluster.json"])
    except (KeyError, ValueError):
        raise ClusterError("The backup is damaged.")
    if manifest.get("format") != BACKUP_FORMAT or st.get("role") != "master" or \
            not all(k in out for k in ("cluster/ca.key", "cluster/ca.crt", "cluster/nodes.json")):
        raise ClusterError("Not a RedstonePanel cluster backup this version can restore.")
    return manifest, out


def promote(data, passphrase, actor):
    """Makes this panel the cluster's master from a backup, with a higher epoch so the old master
    is locked out. Replaces this panel's panel accounts and templates with the backup's.
    The caller restarts the panel."""
    manifest, files = read_backup(data, passphrase)
    with _lock:
        cur = state()
        if cur["role"] == "master":
            raise ClusterError("This panel is already a master.")
        if cur["role"] == "node" and cur.get("cluster_id") != manifest["cluster_id"]:
            raise ClusterError("This panel is a node of a different cluster. Leave it first.")
        _no_custom_cert()
        own_id = cur.get("node_id")
        old_dir = CLUSTER_DIR + ".old"
        shutil.rmtree(old_dir, ignore_errors=True)
        if os.path.exists(CLUSTER_DIR):
            os.rename(CLUSTER_DIR, old_dir)
        try:
            _ensure_dir()
            for arc in ("cluster/ca.key", "cluster/ca.crt"):
                _write_bytes(_path(arc.split("/", 1)[1]), files[arc], 0o600 if arc.endswith(".key") else 0o644)
            nodes = json.loads(files["cluster/nodes.json"]).get("nodes", [])
            _save_nodes([n for n in nodes if n["id"] != own_id])
            _save_joins([])
            st = json.loads(files["cluster/cluster.json"])
            st.update(epoch=int(st.get("epoch", 1)) + 1, promoted=int(time.time()), promoted_by=actor)
            _issue_own_cert()
            _write_json("cluster.json", st)
        except Exception:
            shutil.rmtree(CLUSTER_DIR, ignore_errors=True)
            if os.path.exists(old_dir):
                os.rename(old_dir, CLUSTER_DIR)
            raise
        shutil.rmtree(old_dir, ignore_errors=True)
        _token_cache.clear()
        os.makedirs(DATA_DIR, exist_ok=True)
        for arc in ("users.json", "templates.json"):
            if arc in files:
                _write_bytes(BACKUP_FILES[arc](), files[arc], 0o600)
        _set_https("cluster")
        return st
