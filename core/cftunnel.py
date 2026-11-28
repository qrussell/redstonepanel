"""Web addresses through Cloudflare Tunnel, set up by the panel (1.29).

Once cloudflared runs on the machine (the admin installs it with Cloudflare's own steps), the panel
keeps the tunnel's routes and the DNS names in step with its own settings: the admin panel at a name
like panel.example.org and the join pages (1.21.5) at play.example.org/join/...

There are two kinds of tunnel, and both are handled:

* Managed in Cloudflare ("remote", what the dashboard makes and `cloudflared service install
  <token>` runs). Its routes live in Cloudflare; the panel changes them through Cloudflare's API with
  a token the admin made. cloudflared picks the change up by itself.
* Managed in a file ("local", `config.yml`). The panel writes its routes between two marker lines
  at the top of the file's `ingress:` list, asks cloudflared to check the file, restarts cloudflared
  and puts the old file back when anything fails.

Rules kept everywhere: the panel only ever changes the routes, DNS records and Access application it
made itself (remembered in DATA_DIR/cloudflare-tunnel.json), never anyone else's. The API token is
kept in that file (0600), never sent back (only its last four characters) and never logged. Calls
go only to api.cloudflare.com. Names are what an admin typed, checked to be inside the chosen
domain. In a cluster only the master is published (Quentin, 27 Nov 2026): a node refuses.

The token of a tunnel managed in Cloudflare is on cloudflared's command line; the panel reads the
account and tunnel IDs from it and never keeps or shows its secret.
"""
import base64
import ipaddress
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from core import platform
from core.platform import paths

API = "https://api.cloudflare.com/client/v4"
STATE_FILE = os.path.join(paths.DATA_DIR, "cloudflare-tunnel.json")
DNS_COMMENT = "Made by RedstonePanel"
BEGIN = "# BEGIN RedstonePanel: these lines are the panel's own, and it replaces them"
END = "# END RedstonePanel"
JOIN_PATH = "^/join(/.*)?$"
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
ID_RE = re.compile(r"^[0-9a-f]{32}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{30,200}$")
EMAIL_RE = re.compile(r"^[^@\s<>\"',;]{1,64}@[a-z0-9.-]{1,190}\.[a-z]{2,63}$")
EMAILS_MAX = 20
METRICS_PORTS = range(20241, 20246)       # cloudflared's metrics page when its service names none
DETECT_TTL = 300

_lock = threading.RLock()
_detect_cache = {"at": 0.0, "value": None}
_state_cache = {"mtime": None, "data": None}


class TunnelError(ValueError):
    """Something to show the admin, as it is."""


class ApiError(TunnelError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# What the panel remembers
# ---------------------------------------------------------------------------
def _defaults():
    return {"token": "", "zone_id": "", "zone": "", "account": "", "zones": [],
            "panel_host": "", "join_host": "", "panel_public": True,
            "where": "here", "cloudflared_ip": "", "origin_host": "", "tunnel_id": "",
            "access_emails": [], "access": False,
            "applied": None, "last": None, "updated": None, "updated_by": None}


def load():
    try:
        mtime = os.path.getmtime(STATE_FILE)
    except OSError:
        mtime = None
    with _lock:
        if _state_cache["data"] is not None and _state_cache["mtime"] == mtime:
            return json.loads(json.dumps(_state_cache["data"]))
        out = _defaults()
        if mtime is not None:
            try:
                with open(STATE_FILE) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    out.update({k: data[k] for k in out if k in data})
            except (OSError, ValueError):
                pass
        for k in ("token", "zone_id", "zone", "account", "panel_host", "join_host", "where", "cloudflared_ip",
                  "origin_host", "tunnel_id"):
            out[k] = out[k] if isinstance(out[k], str) else ""
        out["where"] = out["where"] if out["where"] in ("here", "elsewhere") else "here"
        out["panel_public"], out["access"] = bool(out["panel_public"]), bool(out["access"])
        out["access_emails"] = [e for e in out["access_emails"] if isinstance(e, str)] \
            if isinstance(out["access_emails"], list) else []
        out["zones"] = [z for z in out["zones"] if isinstance(z, dict)] if isinstance(out["zones"], list) else []
        out["applied"] = out["applied"] if isinstance(out["applied"], dict) else None
        _state_cache.update(mtime=mtime, data=out)
        return json.loads(json.dumps(out))


def _save(cfg):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, STATE_FILE)
    with _lock:
        _state_cache["data"] = None


def _log(msg):
    try:
        from core import manager
        manager.write_log(msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Finding cloudflared
# ---------------------------------------------------------------------------
def token_ids(token):
    """(account, tunnel id) from a tunnel token (base64 JSON {"a", "t", "s"}); the secret "s" is
    dropped here and never kept. (None, None) when it isn't one."""
    raw = str(token or "").strip()
    if not raw or len(raw) > 4096:
        return None, None
    try:
        data = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_" if "-" in raw or "_" in raw
                                           else None, validate=False))
    except (ValueError, TypeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    a, t = str(data.get("a") or "").lower(), str(data.get("t") or "").lower()
    return (a if ID_RE.match(a) else None), (t if UUID_RE.match(t) else None)


def parse_command(argv):
    """cloudflared's argv -> {"program", "token", "token_file", "config"} (whatever it names)."""
    out = {"program": argv[0] if argv else "", "token": "", "token_file": "", "config": ""}
    i = 1
    while i < len(argv):
        a = argv[i]
        for flag, key in (("--token", "token"), ("--token-file", "token_file"), ("--config", "config")):
            if a == flag and i + 1 < len(argv):
                out[key] = argv[i + 1]
                i += 1
                break
            if a.startswith(flag + "="):
                out[key] = a[len(flag) + 1:]
                break
        i += 1
    return out


def _read_small(path, limit=65536):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read(limit)


def config_tunnel(text):
    """The `tunnel:` line of a config.yml (an ID or a name), or ''."""
    m = re.search(r"^tunnel:\s*['\"]?([A-Za-z0-9_.-]+)['\"]?\s*(#.*)?$", text or "", re.M)
    return m.group(1) if m else ""


def detect(fresh=False):
    """What runs on this machine: {"installed", "service", "running", "mode" ("remote", "local" or
    None), "account", "tunnel_id", "config", "program", "error"}. Reused for 5 minutes."""
    now = time.time()
    if not fresh and _detect_cache["value"] is not None and now - _detect_cache["at"] < DETECT_TTL:
        return dict(_detect_cache["value"])
    out = {"installed": False, "service": "", "running": False, "mode": None, "account": "", "tunnel_id": "",
           "config": "", "program": "", "error": ""}
    try:
        found = platform.tunnel.find()
    except Exception as e:
        found, out["error"] = [], f"Couldn't look for cloudflared: {e}"
    if found:
        svc = next((s for s in found if s["running"]), found[0])
        cmd = parse_command(svc.get("argv") or [])
        out.update(installed=True, service=svc["service"], running=bool(svc["running"]), program=cmd["program"])
        token = cmd["token"]
        if not token and cmd["token_file"]:
            try:
                token = _read_small(cmd["token_file"], 8192).strip()
            except OSError:
                token = ""
        if token:
            a, t = token_ids(token)
            out.update(mode="remote", account=a or "", tunnel_id=t or "")
        else:
            config = cmd["config"] or next((p for p in platform.tunnel.default_configs() if os.path.isfile(p)), "")
            if config:
                out.update(mode="local", config=config)
                try:
                    out["tunnel_id"] = config_tunnel(_read_small(config)).lower()
                except OSError as e:
                    out["error"] = f"Couldn't read {config}: {e.strerror or e}"
        del token
    _detect_cache.update(at=now, value=dict(out))
    return out


# ---------------------------------------------------------------------------
# Cloudflare's API
# ---------------------------------------------------------------------------
def _user_agent():
    try:
        from core import version
        return f"RedstonePanel/{version.VERSION}"
    except Exception:
        return "RedstonePanel"


def api(method, path, body=None, token=None, query=None, timeout=20):
    """One Cloudflare API call -> its "result". Raises ApiError with Cloudflare's own message."""
    if not path.startswith("/") or ".." in path:
        raise ValueError(path)
    url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
    headers = {"User-Agent": _user_agent(), "Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            got = json.loads(r.read(4 * 1024 * 1024) or b"{}")
    except urllib.error.HTTPError as e:
        try:
            got = json.loads(e.read(65536) or b"{}")
        except ValueError:
            got = {}
        raise ApiError(e.code, _message(got) or f"Cloudflare said {e.code} {e.reason}.")
    except (OSError, ValueError) as e:
        raise ApiError(0, f"Cloudflare can't be reached ({getattr(e, 'reason', None) or e}).")
    if not isinstance(got, dict) or got.get("success") is False:
        raise ApiError(200, _message(got) or "Cloudflare refused that.")
    return got.get("result")


def _message(got):
    errs = got.get("errors") if isinstance(got, dict) else None
    if isinstance(errs, list) and errs:
        parts = [str(e.get("message") or e.get("code") or "") for e in errs if isinstance(e, dict)]
        text = "; ".join(p for p in parts if p)[:300]
        return f"Cloudflare: {text}" if text else ""
    return ""


def check_token(token):
    """What a token may do: {"zones": [{"id", "name", "account"}], "accounts": [...]}. Raises
    TunnelError when it can do nothing useful."""
    try:
        zones = api("GET", "/zones", token=token, query={"per_page": 50, "status": "active"}) or []
    except ApiError as e:
        if e.status in (400, 401, 403):
            raise TunnelError("Cloudflare didn't accept that token. Make one under My Profile → API Tokens with "
                              "Cloudflare Tunnel: Edit and DNS: Edit, then paste it here.")
        raise
    out = [{"id": z.get("id"), "name": z.get("name"), "account": (z.get("account") or {}).get("id") or ""}
           for z in zones if isinstance(z, dict) and ID_RE.match(str(z.get("id") or ""))]
    if not out:
        raise TunnelError("That token can't see any of your domains. Give it DNS: Edit for the domain the panel's "
                          "names go in.")
    return {"zones": out}


def _need_token(cfg):
    if not cfg["token"]:
        raise TunnelError("Paste a Cloudflare API token first.")
    return cfg["token"]


def _account(cfg, det):
    """The Cloudflare account the tunnel is in: from cloudflared's token, else the domain's."""
    acct = det.get("account") or cfg["account"]
    if not acct:
        z = next((z for z in cfg["zones"] if z["id"] == cfg["zone_id"]), None)
        acct = (z or {}).get("account") or ""
    if not ID_RE.match(acct or ""):
        raise TunnelError("The panel couldn't tell which Cloudflare account the tunnel is in. Choose the domain again.")
    return acct


def _tunnel_id(cfg, det, token=None, account=None):
    """The tunnel's UUID: found on this machine, chosen by the admin, or a name in config.yml."""
    t = det.get("tunnel_id") if cfg["where"] == "here" else ""
    t = t or cfg["tunnel_id"]
    if t and not UUID_RE.match(t) and token and account:
        found = api("GET", f"/accounts/{account}/cfd_tunnel", token=token,
                    query={"name": t, "is_deleted": "false"}) or []
        t = (found[0] or {}).get("id", "") if found else ""
    if not UUID_RE.match(t or ""):
        raise TunnelError("The panel couldn't tell which tunnel to use. Choose it from the list.")
    return t


def list_tunnels(cfg=None):
    """The account's tunnels, for an admin whose cloudflared runs on another machine."""
    cfg = cfg or load()
    token = _need_token(cfg)
    acct = _account(cfg, {})
    got = api("GET", f"/accounts/{acct}/cfd_tunnel", token=token, query={"is_deleted": "false", "per_page": 50}) or []
    return [{"id": t.get("id"), "name": t.get("name"), "status": t.get("status"),
             "remote": t.get("remote_config") is not False and t.get("config_src") != "local"}
            for t in got if isinstance(t, dict) and UUID_RE.match(str(t.get("id") or ""))]


# ---------------------------------------------------------------------------
# Admin's settings
# ---------------------------------------------------------------------------
def clean_host(value, zone, what):
    host = str(value or "").strip().lower().rstrip(".")
    if not host:
        return ""
    if "://" in host or "/" in host or ":" in host:
        raise TunnelError(f"Enter the {what} name alone, like {what == 'join pages' and 'play' or 'panel'}.{zone or 'example.org'}.")
    if zone and host != zone and not host.endswith("." + zone):
        host = f"{host}.{zone}" if "." not in host else host
    if not HOST_RE.match(host):
        raise TunnelError(f"'{host[:80]}' isn't a usable {what} name.")
    if zone and host != zone and not host.endswith("." + zone):
        raise TunnelError(f"The {what} name must be in {zone}, like {what == 'join pages' and 'play' or 'panel'}.{zone}.")
    if host.startswith("*"):
        raise TunnelError("Wildcard names aren't used.")
    return host


def _clean_ip(value, what):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        raise TunnelError(f"The {what} must be an IP address, like 10.0.0.5.")
    if ip.is_unspecified or ip.is_multicast:
        raise TunnelError(f"The {what} must be a real machine's address.")
    return str(ip)


def clean_emails(value):
    if isinstance(value, str):
        value = re.split(r"[\s,;]+", value)
    if not isinstance(value, (list, tuple)):
        raise TunnelError("Send the email addresses as a list.")
    out = []
    for raw in value:
        e = str(raw or "").strip().lower()
        if not e:
            continue
        if not EMAIL_RE.match(e):
            raise TunnelError(f"'{e[:80]}' isn't an email address.")
        if e not in out:
            out.append(e)
    if len(out) > EMAILS_MAX:
        raise TunnelError(f"Enter at most {EMAILS_MAX} email addresses.")
    return out


def _refuse_on_node():
    from core import cluster
    if cluster.role() == "node":
        raise TunnelError("This panel is a cluster node. In a cluster only the master is reachable from the internet: "
                          "set up Cloudflare Tunnel on the master, which serves the join pages of every server.")


def save_settings(new, actor="?"):
    """Admin's change from Panel Settings -> Cloudflare Tunnel. Returns public()."""
    _refuse_on_node()
    with _lock:
        cfg = load()
        if new.get("forget_token"):
            if cfg["applied"]:
                raise TunnelError("Remove the panel's names from the tunnel first (Remove), then forget the token.")
            cfg.update(token="", zones=[], zone_id="", zone="", account="")
        token = str(new.get("token") or "").strip()
        if token and not new.get("forget_token"):
            if token_ids(token)[1]:
                raise TunnelError("That's the tunnel's own token (the one cloudflared runs with). The panel needs an "
                                  "API token: Cloudflare dashboard → My Profile → API Tokens → Create Token.")
            if not TOKEN_RE.match(token):
                raise TunnelError("That doesn't look like a Cloudflare API token.")
            cfg["zones"] = check_token(token)["zones"]
            cfg["token"] = token
            if cfg["zone_id"] not in [z["id"] for z in cfg["zones"]]:
                cfg["zone_id"] = cfg["zones"][0]["id"] if len(cfg["zones"]) == 1 else ""
        if "zone_id" in new:
            zid = str(new["zone_id"] or "")
            if zid and zid not in [z["id"] for z in cfg["zones"]]:
                raise TunnelError("Choose one of the domains the token can see.")
            cfg["zone_id"] = zid
        z = next((z for z in cfg["zones"] if z["id"] == cfg["zone_id"]), None)
        cfg["zone"], cfg["account"] = (z["name"], z["account"]) if z else ("", "")
        if "where" in new:
            if new["where"] not in ("here", "elsewhere"):
                raise TunnelError("Say where cloudflared runs: on this machine or another one.")
            cfg["where"] = new["where"]
        for key, what in (("cloudflared_ip", "address of the machine cloudflared runs on"),
                          ("origin_host", "address cloudflared reaches this panel at")):
            if key in new:
                cfg[key] = _clean_ip(new[key], what)
        if "tunnel_id" in new:
            t = str(new["tunnel_id"] or "").strip().lower()
            if t and not UUID_RE.match(t):
                raise TunnelError("Choose the tunnel from the list.")
            cfg["tunnel_id"] = t
        if "panel_public" in new:
            cfg["panel_public"] = bool(new["panel_public"])
        for key, what in (("panel_host", "panel"), ("join_host", "join pages")):
            if key in new:
                cfg[key] = clean_host(new[key], cfg["zone"], what)
            elif cfg[key] and cfg["zone"] and not (cfg[key] == cfg["zone"] or cfg[key].endswith("." + cfg["zone"])):
                cfg[key] = ""
        if "access_emails" in new:
            cfg["access_emails"] = clean_emails(new["access_emails"])
        if cfg["where"] == "elsewhere" and not cfg["cloudflared_ip"] and ("where" in new or "cloudflared_ip" in new):
            raise TunnelError("Enter the address of the machine cloudflared runs on, so the panel knows the visits it "
                              "forwards.")
        cfg.update(updated=int(time.time()), updated_by=actor)
        _save(cfg)
    _log(f"[TUNNEL] {actor} changed the Cloudflare Tunnel settings"
         + (" (new API token)" if token and not new.get("forget_token") else "")
         + (" and forgot the API token" if new.get("forget_token") else "") + ".")
    return public()


def set_access(on, actor="?"):
    """Cloudflare Access in front of the panel's name: on or off (applied at the next Apply)."""
    _refuse_on_node()
    with _lock:
        cfg = load()
        if on:
            if not cfg["access_emails"]:
                raise TunnelError("Enter the email addresses that may open the panel first.")
            if cfg["panel_host"] and cfg["panel_host"] == cfg["join_host"]:
                raise TunnelError("Cloudflare Access would also stop players at the join pages. Give the join pages "
                                  "their own name first.")
        cfg["access"] = bool(on)
        cfg.update(updated=int(time.time()), updated_by=actor)
        _save(cfg)
    _log(f"[TUNNEL] {actor} turned Cloudflare Access for the panel {'on' if on else 'off'}")
    return public()


# ---------------------------------------------------------------------------
# What the routes should be
# ---------------------------------------------------------------------------
def _origin(cfg):
    if cfg["where"] == "here":
        return "localhost"
    if cfg["origin_host"]:
        return f"[{cfg['origin_host']}]" if ":" in cfg["origin_host"] else cfg["origin_host"]
    try:
        from core.platform import system
        addrs = [a for a in system.addresses(timeout=3) if ":" not in a and not a.startswith("127.")]
    except Exception:
        addrs = []
    if not addrs:
        raise TunnelError("Enter the address cloudflared reaches this panel at (this machine's address on your network).")
    return addrs[0]


def desired(cfg=None, s=None):
    """The routes the panel wants: [{"hostname", "path"?, "service", "originRequest"?, "what"}]."""
    from core import settings as panel_settings
    cfg = cfg or load()
    s = s or panel_settings.load()
    host = _origin(cfg)
    out = []
    if cfg["join_host"] and s["join_enabled"]:
        https = s["https_mode"] != "off"
        rule = {"hostname": cfg["join_host"], "path": JOIN_PATH,
                "service": f"{'https' if https else 'http'}://{host}:{s['join_port']}", "what": "join"}
        if https:
            rule["originRequest"] = {"noTLSVerify": True}
        out.append(rule)
    if cfg["panel_host"] and cfg["panel_public"]:
        https = s["https_mode"] != "off"
        port = s["https_port"] if https else s["http_port"]
        rule = {"hostname": cfg["panel_host"], "service": f"{'https' if https else 'http'}://{host}:{port}",
                "what": "panel"}
        if https:
            rule["originRequest"] = {"noTLSVerify": True}
        out.append(rule)
    return out


def _bare(rule):
    return {k: v for k, v in rule.items() if k != "what"}


def _same_route(a, b):
    return a.get("hostname") == b.get("hostname") and (a.get("path") or "") == (b.get("path") or "")


def merge_ingress(existing, ours_before, ours_now, ports=()):
    """The tunnel's new route list: the panel's routes first, every other route kept in its order,
    the catch-all (a route with no name) last. A route of the panel's from before is replaced or
    dropped. Someone else's route for the same name and path is taken over only when it already
    goes to one of this panel's ports (the 1.21.9 copy box made those); otherwise TunnelError."""
    existing = [r for r in (existing or []) if isinstance(r, dict)]
    catch = [r for r in existing if not r.get("hostname")]
    others = []
    for r in existing:
        if not r.get("hostname"):
            continue
        if any(_same_route(r, o) for o in ours_before):
            continue
        clash = next((o for o in ours_now if _same_route(r, o)), None)
        if clash:
            svc = str(r.get("service") or "")
            if not any(svc.rstrip("/").endswith(f":{p}") for p in ports):
                raise TunnelError(f"The tunnel already sends {r['hostname']}{(r.get('path') or '')} to {svc[:80]}, which "
                                  "isn't this panel. Remove that route in Cloudflare or choose another name.")
            continue
        others.append(r)
    last = catch[-1:] or [{"service": "http_status:404"}]
    return [_bare(r) for r in ours_now] + others + last


# ---------------------------------------------------------------------------
# config.yml (a tunnel managed in a file)
# ---------------------------------------------------------------------------
def _yaml(value):
    return "'" + str(value).replace("'", "''") + "'"


def render_block(rules, indent):
    lines = [indent + BEGIN]
    for r in rules:
        lines.append(f"{indent}- hostname: {r['hostname']}")
        if r.get("path"):
            lines.append(f"{indent}  path: {_yaml(r['path'])}")
        lines.append(f"{indent}  service: {r['service']}")
        if (r.get("originRequest") or {}).get("noTLSVerify"):
            lines.append(f"{indent}  originRequest:")
            lines.append(f"{indent}    noTLSVerify: true")
    lines.append(indent + END)
    return lines


def edit_config(text, rules):
    """config.yml's text with the panel's block replaced by `rules` (no block when empty).
    TunnelError when the file isn't laid out in a way that can be edited safely."""
    if "\t" in text:
        raise TunnelError("config.yml uses tabs, so the panel left it alone. Paste the lines below yourself.")
    lines = text.split("\n")
    if any(l.strip() == BEGIN for l in lines):
        start = next(i for i, l in enumerate(lines) if l.strip() == BEGIN)
        end = next((i for i in range(start, len(lines)) if lines[i].strip() == END), None)
        if end is None:
            raise TunnelError("config.yml has the panel's first marker line but not its last, so the panel left it "
                              "alone. Fix or remove the block by hand.")
        del lines[start:end + 1]
    at = [i for i, l in enumerate(lines) if re.match(r"^ingress\s*:", l)]
    if len(at) > 1:
        raise TunnelError("config.yml has more than one ingress list, so the panel left it alone.")
    if not at:
        if any(re.match(r"^(url|hello-world)\s*:", l) for l in lines):
            raise TunnelError("config.yml sends everything to one address (url:), so the panel left it alone. Move it "
                              "to an ingress list first.")
        if not rules:
            return "\n".join(lines)
        while lines and lines[-1] == "":
            lines.pop()
        lines += ["ingress:"] + render_block(rules, "  ") + ["  - service: http_status:404", ""]
        return "\n".join(lines)
    i = at[0]
    rest = lines[i].split(":", 1)[1].strip()
    if rest and not rest.startswith("#"):
        raise TunnelError("config.yml writes its ingress list on one line, so the panel left it alone.")
    nxt = next((j for j in range(i + 1, len(lines)) if lines[j].strip() and not lines[j].strip().startswith("#")), None)
    if nxt is None or not lines[nxt].lstrip().startswith("- "):
        raise TunnelError("config.yml's ingress list is empty or laid out unusually, so the panel left it alone.")
    indent = lines[nxt][:len(lines[nxt]) - len(lines[nxt].lstrip())]
    if rules:
        lines[i + 1:i + 1] = render_block(rules, indent)
    return "\n".join(lines)


def _metrics_ready(timeout=30, quiet_after=5):
    """True when cloudflared's metrics page says it's connected again, False when it answers but
    isn't, None when there's no metrics page to ask (the service's own state is all there is)."""
    start, seen = time.time(), False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.time() - start < timeout:
        for port in METRICS_PORTS:
            try:
                with opener.open(f"http://127.0.0.1:{port}/ready", timeout=2) as r:
                    if r.status == 200:
                        return True
                    seen = True
            except urllib.error.HTTPError:
                seen = True
            except OSError:
                continue
        if not seen and time.time() - start >= quiet_after:
            return None
        time.sleep(1)
    return False if seen else None


def _write_local(det, rules, actor):
    """Writes the panel's block into config.yml, checks it, restarts cloudflared, rolls back."""
    path = det["config"]
    try:
        old = _read_small(path, 1024 * 1024)
    except OSError as e:
        raise TunnelError(f"Couldn't read {path}: {e.strerror or e}")
    new = edit_config(old, rules)
    if new == old:
        return False
    mode = os.stat(path).st_mode & 0o777
    tmp = os.path.join(os.path.dirname(path), ".redstonepanel-check.yml")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode or 0o600), "w", encoding="utf-8") as f:
        f.write(new)
    try:
        ok, said = platform.tunnel.validate(det["program"], tmp)
    except Exception as e:
        ok, said = False, str(e)
    if not ok:
        os.unlink(tmp)
        raise TunnelError(f"cloudflared found a problem in the new file, so nothing changed: {said[-300:]}")
    backup = path + ".redstonepanel-old"
    with open(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode or 0o600), "w", encoding="utf-8") as f:
        f.write(old)
    os.replace(tmp, path)
    _log(f"[TUNNEL] {actor} changed the panel's routes in {path} (the old file is {backup})")
    running = platform.tunnel.restart(det["service"]) if det["service"] else True
    ready = _metrics_ready() if running else False
    if running and ready is None and det["service"]:
        # No metrics page to ask: a few seconds on, is the service itself still running?
        running = any(f["service"] == det["service"] and f["running"] for f in platform.tunnel.find())
    if not running or ready is False:
        with open(path, "w", encoding="utf-8") as f:
            f.write(old)
        if det["service"]:
            platform.tunnel.restart(det["service"])
        _log(f"[TUNNEL] cloudflared didn't come back with the new {path}; the old file was put back")
        raise TunnelError("cloudflared didn't come back after the change, so the panel put the old file back and "
                          "restarted it. Check cloudflared's log.")
    return True


# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------
def _dns_ensure(token, zone_id, host, tunnel, made):
    """A proxied CNAME host -> <tunnel>.cfargotunnel.com. Returns (record id, made by the panel)."""
    target = f"{tunnel}.cfargotunnel.com"
    found = api("GET", f"/zones/{zone_id}/dns_records", token=token, query={"name": host}) or []
    for r in found:
        if not isinstance(r, dict):
            continue
        if r.get("type") == "CNAME" and str(r.get("content") or "").lower() == target and r.get("proxied"):
            return r.get("id"), r.get("id") in made
        if r.get("id") in made:
            api("PATCH", f"/zones/{zone_id}/dns_records/{r['id']}", token=token,
                body={"type": "CNAME", "name": host, "content": target, "proxied": True, "comment": DNS_COMMENT})
            return r["id"], True
        raise TunnelError(f"{host} already has a DNS record ({r.get('type')} {str(r.get('content') or '')[:60]}) "
                          "that the panel didn't make. Delete it in Cloudflare or choose another name.")
    r = api("POST", f"/zones/{zone_id}/dns_records", token=token,
            body={"type": "CNAME", "name": host, "content": target, "proxied": True, "ttl": 1, "comment": DNS_COMMENT})
    return (r or {}).get("id"), True


def _dns_delete(token, zone_id, rec_id):
    try:
        api("DELETE", f"/zones/{zone_id}/dns_records/{rec_id}", token=token)
    except ApiError as e:
        if e.status != 404:
            raise


# ---------------------------------------------------------------------------
# Cloudflare Access in front of the panel
# ---------------------------------------------------------------------------
def _access_body(host, emails):
    return {"name": f"RedstonePanel ({host})", "domain": host, "type": "self_hosted", "session_duration": "24h",
            "app_launcher_visible": False,
            "policies": [{"name": "RedstonePanel admins", "decision": "allow", "precedence": 1,
                          "include": [{"email": {"email": e}} for e in emails]}]}


def _access_sync(cfg, applied, token, account):
    """Makes, updates or removes the panel's Access application. Returns its id or ''."""
    want = cfg["access"] and cfg["panel_public"] and cfg["panel_host"] and cfg["access_emails"]
    app_id = (applied or {}).get("access_app", "")
    if not want:
        if app_id:
            try:
                api("DELETE", f"/accounts/{account}/access/apps/{app_id}", token=token)
            except ApiError as e:
                if e.status != 404:
                    raise
        return ""
    if cfg["join_host"] == cfg["panel_host"]:
        raise TunnelError("Cloudflare Access would also stop players at the join pages. Give the join pages their own "
                          "name to put Access in front of the panel.")
    body = _access_body(cfg["panel_host"], cfg["access_emails"])
    try:
        if app_id:
            api("PUT", f"/accounts/{account}/access/apps/{app_id}", token=token, body=body)
            return app_id
        got = api("POST", f"/accounts/{account}/access/apps", token=token, body=body) or {}
        return got.get("id") or ""
    except ApiError as e:
        if e.status in (401, 403):
            raise TunnelError("The token can't change Cloudflare Access. Add Access: Apps and Policies: Edit to it, or "
                              "turn Access off here.")
        raise


# ---------------------------------------------------------------------------
# Apply and Remove
# ---------------------------------------------------------------------------
def _ports(s):
    return [p for p in (s["join_port"], s["https_port"], s["http_port"]) if p]


def plan(cfg=None, s=None):
    """What Apply would change, in words, without changing anything."""
    cfg, det = cfg or load(), detect()
    rules = desired(cfg, s)
    mode = _mode(cfg, det)
    lines = [f"Route {r['hostname']}{'/join/...' if r.get('path') else ''} → {r['service']}" for r in rules]
    if cfg["token"] and cfg["zone_id"]:
        lines += [f"DNS name {r['hostname']} → the tunnel (proxied)" for r in {r["hostname"]: r for r in rules}.values()]
    if cfg["access"] and cfg["panel_public"] and cfg["panel_host"]:
        lines.append(f"Cloudflare Access in front of {cfg['panel_host']} for {', '.join(cfg['access_emails']) or 'nobody yet'}")
    was = (cfg["applied"] or {}).get("rules") or []
    gone = [r for r in was if not any(_same_route(r, n) for n in rules)]
    lines += [f"Take away {r['hostname']}{'/join/...' if r.get('path') else ''}" for r in gone]
    out = {"mode": mode, "changes": lines, "rules": [_bare(r) for r in rules]}
    if mode == "local" or (mode is None and not cfg["token"]):
        out["block"] = "\n".join(render_block(rules, "  "))
    return out


def _mode(cfg, det):
    if cfg["where"] == "elsewhere":
        return "remote"
    return det.get("mode")


def apply(actor="?", cfg=None, s=None):
    """Puts the panel's routes, DNS names and Access in place. Returns a result for the card."""
    from core import settings as panel_settings
    _refuse_on_node()
    with _lock:
        cfg = cfg or load()
        s = s or panel_settings.load()
        det = detect(fresh=True)
        mode = _mode(cfg, det)
        if cfg["where"] == "here" and not det["installed"]:
            raise TunnelError("cloudflared isn't installed on this machine. " + platform.tunnel.install_hint())
        if mode is None:
            raise TunnelError("cloudflared runs here, but the panel couldn't find its tunnel (no token and no config.yml).")
        if not cfg["panel_host"] and not cfg["join_host"]:
            raise TunnelError("Enter a name for the panel or the join pages first.")
        if cfg["join_host"] and not s["join_enabled"]:
            raise TunnelError("Turn the join pages on first (🔗 Join pages), or leave their name empty.")
        if cfg["where"] == "elsewhere" and not cfg["cloudflared_ip"]:
            raise TunnelError("Enter the address of the machine cloudflared runs on.")
        applied = dict(cfg["applied"] or {})
        before = applied.get("rules") or []
        rules = desired(cfg, s)
        token = cfg["token"]
        done = []
        if mode == "remote":
            token = _need_token(cfg)
            account = _account(cfg, det if cfg["where"] == "here" else {})
            tunnel = _tunnel_id(cfg, det, token, account)
            conf = api("GET", f"/accounts/{account}/cfd_tunnel/{tunnel}/configurations", token=token) or {}
            current = conf.get("config") if isinstance(conf.get("config"), dict) else {}
            ingress = merge_ingress(current.get("ingress"), before, rules, _ports(s))
            api("PUT", f"/accounts/{account}/cfd_tunnel/{tunnel}/configurations", token=token,
                body={"config": dict(current, ingress=ingress)})
            done.append("Routes updated in Cloudflare.")
        else:
            account = cfg["account"]
            if _write_local(det, rules, actor):
                done.append(f"Routes written to {det['config']} and cloudflared restarted.")
            tunnel = det.get("tunnel_id") or ""
            if token and account and tunnel and not UUID_RE.match(tunnel):
                tunnel = _tunnel_id(dict(cfg, tunnel_id=tunnel), {}, token, account)
        dns = dict(applied.get("dns") or {})          # host -> {"id", "made"}
        commands = []
        wanted_hosts = list(dict.fromkeys(r["hostname"] for r in rules))
        if token and cfg["zone_id"]:
            made = {v["id"] for v in dns.values() if v.get("made")}
            for host in wanted_hosts:
                rec, ours = _dns_ensure(token, cfg["zone_id"], host, tunnel, made)
                dns[host] = {"id": rec, "made": bool(ours or dns.get(host, {}).get("made"))}
            for host in [h for h in dns if h not in wanted_hosts]:
                if dns[host].get("made") and dns[host].get("id"):
                    _dns_delete(token, cfg["zone_id"], dns[host]["id"])
                del dns[host]
            done.append("DNS names in place.")
        else:
            commands = [f"cloudflared tunnel route dns {tunnel or '<tunnel>'} {h}" for h in wanted_hosts]
        access_app = applied.get("access_app", "")
        if token and (cfg["access"] or access_app):
            access_app = _access_sync(cfg, applied, token, account or _account(cfg, det))
            if access_app:
                done.append(f"Cloudflare Access in front of {cfg['panel_host']}.")
        old_panel = (cfg["applied"] or {}).get("panel_url", "")
        applied = {"mode": mode, "tunnel": tunnel, "account": account, "rules": [_bare(r) for r in rules],
                   "dns": dns, "access_app": access_app, "config": det.get("config") if mode == "local" else "",
                   "service": det.get("service") or "", "at": int(time.time()), "by": actor,
                   "panel_url": next((f"https://{r['hostname']}" for r in rules if r["what"] == "panel"), ""),
                   "join_url": next((f"https://{r['hostname']}" for r in rules if r["what"] == "join"), ""),
                   "added_proxies": (cfg["applied"] or {}).get("added_proxies") or [],
                   "set_join_url": (cfg["applied"] or {}).get("set_join_url", "")}
        cfg["applied"] = applied
        cfg["last"] = {"ok": True, "at": int(time.time()), "done": done, "commands": commands}
        _save(cfg)
        _follow(cfg, s, old_panel, actor)
    _log(f"[TUNNEL] {actor} applied the Cloudflare Tunnel names: "
         + (", ".join(f"{r['hostname']} ({r['what']})" for r in rules) or "none") + f" ({mode} tunnel)")
    return {"done": done, "commands": commands, "status": public()}


def _proxies_for(cfg):
    return ["127.0.0.1", "::1"] if cfg["where"] == "here" else [cfg["cloudflared_ip"]]


def _follow(cfg, s, old_panel_url, actor):
    """The panel's own settings that follow the tunnel: the join pages' address and proxies
    (1.21.9), and Discord's panel address (1.28)."""
    from core import settings as panel_settings
    applied = cfg["applied"]
    join_url = applied["join_url"]
    new = dict(panel_settings.load())
    added = [p for p in applied.get("added_proxies") or []]
    if join_url:
        if not new["join_url"] or new["join_url"] == applied.get("set_join_url"):
            new["join_url"] = join_url
            applied["set_join_url"] = join_url
        for p in _proxies_for(cfg):
            if p and p not in new["join_proxies"] and len(new["join_proxies"]) < panel_settings.JOIN_PROXIES_MAX:
                new["join_proxies"] = new["join_proxies"] + [p]
                added.append(p)
    else:
        if applied.get("set_join_url") and new["join_url"] == applied["set_join_url"]:
            new["join_url"] = ""
        applied["set_join_url"] = ""
        new["join_proxies"] = [p for p in new["join_proxies"] if p not in added]
        if new["join_proxy_only"] and not new["join_proxies"] and not new["join_cloudflare"]:
            new["join_proxy_only"] = False
        added = []
    applied["added_proxies"] = list(dict.fromkeys(added))
    try:
        panel_settings.save(panel_settings.validate(new))
    except panel_settings.SettingsError as e:
        _log(f"[TUNNEL] Couldn't follow the join pages' address: {e}")
    _save(cfg)
    try:
        from core import discordauth
        discordauth.adopt_panel_url(applied["panel_url"], old_panel_url, actor)
    except Exception as e:
        _log(f"[TUNNEL] Couldn't update Discord's panel address: {e}")


def remove(actor="?"):
    """Takes away only what the panel added: its routes, DNS records and Access application."""
    from core import settings as panel_settings
    with _lock:
        cfg = load()
        applied = cfg["applied"]
        if not applied:
            return {"done": ["Nothing to take away."], "status": public()}
        done = []
        token = cfg["token"]
        before = applied.get("rules") or []
        if applied.get("mode") == "remote":
            token = _need_token(cfg)
            account, tunnel = applied["account"], applied["tunnel"]
            conf = api("GET", f"/accounts/{account}/cfd_tunnel/{tunnel}/configurations", token=token) or {}
            current = conf.get("config") if isinstance(conf.get("config"), dict) else {}
            api("PUT", f"/accounts/{account}/cfd_tunnel/{tunnel}/configurations", token=token,
                body={"config": dict(current, ingress=merge_ingress(current.get("ingress"), before, []))})
            done.append("The panel's routes were taken out of the tunnel.")
        else:
            det = detect(fresh=True)
            det["config"] = applied.get("config") or det["config"]
            if det["config"] and _write_local(det, [], actor):
                done.append(f"The panel's lines were taken out of {det['config']} and cloudflared restarted.")
        if token and cfg["zone_id"]:
            for host, rec in (applied.get("dns") or {}).items():
                if rec.get("made") and rec.get("id"):
                    _dns_delete(token, cfg["zone_id"], rec["id"])
            done.append("The panel's DNS names were deleted.")
        if token and applied.get("access_app"):
            _access_sync(dict(cfg, access=False), applied, token, applied.get("account") or cfg["account"])
            done.append("The panel's Cloudflare Access application was deleted.")
        old_panel = applied.get("panel_url", "")
        cfg["applied"] = dict(applied, rules=[], dns={}, access_app="", panel_url="", join_url="")
        _follow(cfg, panel_settings.load(), old_panel, actor)
        cfg = load()
        cfg["applied"], cfg["last"] = None, {"ok": True, "at": int(time.time()), "done": done, "commands": []}
        _save(cfg)
    _log(f"[TUNNEL] {actor} removed the panel's Cloudflare Tunnel names")
    return {"done": done, "status": public()}


def sync(actor="the panel"):
    """After a setting the routes depend on changed (join port, HTTPS, join pages on or off):
    applies again when the panel's routes are in place and would differ. Never raises."""
    try:
        cfg = load()
        if not cfg["applied"]:
            return False
        now = [_bare(r) for r in desired(cfg)]
        if now == (cfg["applied"].get("rules") or []):
            return False
        apply(actor, cfg)
        return True
    except Exception as e:
        try:
            cfg = load()
            cfg["last"] = {"ok": False, "at": int(time.time()), "error": str(e)[:400]}
            _save(cfg)
        except Exception:
            pass
        _log(f"[TUNNEL] Couldn't bring the tunnel's routes up to date: {e}")
        return False


def sync_soon(actor="the panel"):
    threading.Thread(target=sync, args=(actor,), daemon=True).start()


# ---------------------------------------------------------------------------
# Checking from outside
# ---------------------------------------------------------------------------
def _outside(url):
    """(ok, words) for one request to the panel's own public name, the way a visitor makes it."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": _user_agent()}), timeout=15) as r:
            return r.status == 200, f"answers ({r.status})"
    except urllib.error.HTTPError as e:
        where = e.headers.get("Location") or ""
        if e.code in (301, 302, 303, 307) and "cloudflareaccess.com" in where:
            return True, "answers, behind Cloudflare Access"
        if e.code in (502, 503, 530):
            return False, f"Cloudflare can't reach the panel through the tunnel ({e.code}): is cloudflared running?"
        return False, f"answered {e.code}"
    except OSError as e:
        return False, f"can't be reached ({getattr(e, 'reason', None) or e})"


def check(actor="?"):
    """Each name from outside, and the tunnel's health from Cloudflare."""
    cfg = load()
    applied = cfg["applied"] or {}
    out = []
    for r in applied.get("rules") or []:
        url = f"https://{r['hostname']}" + ("/join/assets/join.css" if r.get("path") else "/api/health")
        ok, words = _outside(url)
        out.append({"name": r["hostname"] + ("/join" if r.get("path") else ""), "ok": ok, "text": words})
    health = None
    if cfg["token"] and applied.get("mode") == "remote" and applied.get("tunnel"):
        try:
            t = api("GET", f"/accounts/{applied['account']}/cfd_tunnel/{applied['tunnel']}", token=cfg["token"]) or {}
            health = {"status": t.get("status") or "unknown", "connections": len(t.get("connections") or [])}
        except TunnelError as e:
            health = {"status": "unknown", "error": str(e)}
    return {"names": out, "tunnel": health}


# ---------------------------------------------------------------------------
# Who a visitor is, through the tunnel
# ---------------------------------------------------------------------------
def visitor_ip(peer, headers):
    """The visitor's own address for a request that came through the tunnel (CF-Connecting-IP),
    used for the sign-in limit and the activity log. Only from the local cloudflared (or the machine
    the admin named); from anyone else the header means nothing."""
    try:
        cfg = load()
    except Exception:
        return peer
    if not cfg["applied"] or not peer:
        return peer
    try:
        ip = ipaddress.ip_address(peer)
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
    except ValueError:
        return peer
    trusted = ip.is_loopback if cfg["where"] == "here" else str(ip) == cfg["cloudflared_ip"]
    if not trusted:
        return peer
    raw = (headers.get("cf-connecting-ip") or "").strip()
    try:
        return str(ipaddress.ip_address(raw)) if raw else peer
    except ValueError:
        return peer


def public_panel_url():
    """https://panel.example.org while the panel is published through the tunnel, else ''."""
    try:
        return (load()["applied"] or {}).get("panel_url") or ""
    except Exception:
        return ""


def node_warning():
    """A cluster node that runs its own cloudflared is on the internet by itself (Quentin: only the
    master should be). The Needs attention text, or None."""
    from core import cluster
    if cluster.role() != "node":
        return None
    det = detect()
    if det["installed"] and det["running"]:
        return ("This cluster node runs cloudflared, so it may be reachable from the internet by itself. In a "
                "cluster only the master needs a tunnel: it serves every server's join page.")
    return None


def attention():
    """Needs attention lines: a node on the internet by itself, a failed update of the routes."""
    out = []
    warn = node_warning()
    if warn:
        out.append(warn)
    last = load().get("last") or {}
    if last.get("ok") is False:
        out.append("Cloudflare Tunnel: the panel couldn't bring the tunnel's routes up to date: "
                   + str(last.get("error") or "unknown error"))
    return out


def public():
    """What the settings card shows. Never the token."""
    from core import cluster
    cfg = load()
    det = detect()
    role = cluster.role()
    out = {k: cfg[k] for k in ("zone_id", "zone", "zones", "panel_host", "join_host", "panel_public", "where",
                               "cloudflared_ip", "origin_host", "tunnel_id", "access", "access_emails",
                               "last", "updated", "updated_by")}
    out["zones"] = [{"id": z["id"], "name": z["name"]} for z in cfg["zones"]]
    out.update(has_token=bool(cfg["token"]), token_hint=cfg["token"][-4:] if cfg["token"] else "",
               found=det, mode=_mode(cfg, det), cluster_role=role, applied=cfg["applied"],
               panel_url=public_panel_url(), install_hint=platform.tunnel.install_hint(),
               system=platform.name())
    try:
        out["preview"] = plan(cfg) if role != "node" else None
    except TunnelError as e:
        out["preview"] = {"error": str(e)}
    return out
