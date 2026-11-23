import os
import platform
import subprocess
import shutil
import re
import json
import urllib.request
import urllib.parse
import uuid
import secrets
import time
import threading

import tempfile
from core.settings import DATA_DIR, LOG_FILE
from core import properties, rcon, proxies, bedrock_net, javaservers
from core.platform import console, firewall, paths, services, system

TEMPLATE_FILE = os.path.join(DATA_DIR, "templates.json")

class DependencyError(RuntimeError):
    """A program the panel needs is not installed (message says which package provides it)."""

class JavaMissing(DependencyError):
    """The Java a server needs isn't installed; an admin can install it from the panel."""

    def __init__(self, err):
        super().__init__(str(err))
        self.required = err.required
        self.major = err.major
        self.package = err.package

# program -> Ubuntu package (the .deb depends on/recommends these)
TOOL_PACKAGES = {"java": "openjdk-25-jre-headless", "screen": "screen", "unzip": "unzip",
                 "curl": "curl", "wget": "wget", "jq": "jq"}

# Mojang ships Bedrock Dedicated Server for x86-64 Linux only, so ARM64 machines (a Raspberry
# Pi, an ARM VPS) run Java servers only.
BEDROCK_MACHINES = ("x86_64", "amd64")

def bedrock_supported(machine=None):
    if machine is None and paths.WINDOWS:
        return False                               # the Windows preview (1.22) runs Java servers only
    return (machine or platform.machine()).lower() in BEDROCK_MACHINES

def bedrock_unsupported_message():
    if paths.WINDOWS:
        return ("Bedrock servers aren't in the Windows preview yet; they come in a later release. Java "
                "servers work here, and Bedrock players can join one through Geyser.")
    return (f"Bedrock servers can't run on this machine ({platform.machine()}): Mojang only makes Bedrock "
            f"Dedicated Server for 64-bit Intel and AMD (x86-64) machines. Java servers work here, and "
            f"Bedrock players can join one through Geyser.")

def require_bedrock_machine():
    if not bedrock_supported():
        raise ServerError(bedrock_unsupported_message())

def require_tools(*tools):
    missing = [t for t in tools if not shutil.which(t)]
    if missing:
        pkgs = " ".join(TOOL_PACKAGES.get(t, t) for t in missing)
        raise DependencyError(f"Missing {', '.join(missing)}. Install with: sudo apt install {pkgs}")

def ensure_minecraft_user(home):
    """The .deb creates the 'minecraft' system user; older/manual installs create it on first deploy."""
    system.ensure_game_account(home)

def open_firewall(port, proto):
    """Opens the game port in ufw when ufw is installed (it's optional)."""
    if firewall.installed():
        firewall.allow(port, proto)
        _firewall_changed()


def _firewall_changed():
    """The dashboard re-reads ufw and the servers on its next load instead of showing a cached
    report from before the change."""
    try:
        from core import host
        for key in ("ufw", "report"):
            host._cache.pop(key, None)
    except Exception:
        pass

# NetherNet Bedrock servers announce themselves to consoles on the LAN on this UDP port (all of
# them share it); RakNet servers answer LAN pings on their own UDP server-port instead.
NETHERNET_LAN_PORT = 7551


def _udp_port_specs(value):
    """server-udp-ports -> the ports this machine listens on, as ufw port specs: '19141',
    '21200:21300'. The address form (IP:public:local) listens on the local part."""
    out = []
    for part in bedrock_net.plain_udp_ports(value or "").split(","):
        local = part.strip().split(":")[-1]
        lo, _, hi = local.partition("-")
        if lo.isdigit() and (hi.isdigit() if hi else True) and (not hi or int(lo) <= int(hi)):
            out.append(f"{lo}:{hi}" if hi else lo)
    return out


def _udp_ports_used(value):
    """The ports in server-udp-ports, as ints (a range counts in full, up to 1000 ports)."""
    used = set()
    for spec in _udp_port_specs(value):
        lo, _, hi = spec.partition(":")
        lo = int(lo)
        hi = int(hi) if hi else lo
        used.update(range(lo, min(hi, lo + 999) + 1))
    return used


def _follow_udp_port(props, old_port, new_port):
    """The server-udp-ports to write when a Bedrock server's port moves from old_port to new_port:
    a single port equal to the old one moves with it (Mojang's server.properties ships
    server-udp-ports=19132, the same as server-port). None when it should stay as it is."""
    cur = str(props.get("server-udp-ports", "")).strip()
    old, new = str(old_port).strip(), str(new_port)
    return new if cur and cur == old and old != new else None


def bedrock_transport(props):
    """The transport a Bedrock server really uses. BDS 1.26.50 and later default to NetherNet
    when server.properties has no transport line (checked on 1.26.66.1: TCP server-port and UDP
    7551, nothing on UDP server-port), and the panel only ever downloads the latest BDS. Only an
    explicit transport=raknet means RakNet."""
    return "raknet" if str(props.get("transport") or "").strip().lower() == "raknet" else "nethernet"


def is_nethernet(props):
    return bedrock_transport(props) == "nethernet"


def firewall_rules(edition, props):
    """The ufw rules (port spec, proto) a server needs, from its server.properties.
    Java: TCP server-port. Bedrock over NetherNet (the default): TCP server-port (signaling, one
    dual-stack socket, so server-portv6 isn't used), UDP server-udp-ports (gameplay, opened once a
    player connects) and UDP 7551 (LAN discovery, open even with enable-lan-visibility=false).
    Bedrock over RakNet: UDP server-port and server-portv6."""
    port = str(props.get("server-port", "")).strip()
    if not port.isdigit():
        return set()
    if str(edition).lower() == "java":
        return {(port, "tcp")}
    if is_nethernet(props):
        return {(port, "tcp"), (str(NETHERNET_LAN_PORT), "udp")} | \
            {(p, "udp") for p in _udp_port_specs(props.get("server-udp-ports"))}
    v6 = str(props.get("server-portv6", "")).strip()
    return {(port, "udp")} | ({(v6, "udp")} if v6.isdigit() else set())


def fixed_udp_ports(d):
    """A Bedrock server without server-udp-ports gets gameplay ports from the kernel's random
    range, which no firewall rule or router forward can match. Gives it UDP on its own
    server-port, as a new server gets (takes effect at its next start). True if it changed."""
    props = read_properties(d)
    port = str(props.get("server-port", "")).strip()
    if str(props.get("server-udp-ports", "")).strip() or not port.isdigit():
        return False
    write_properties(d, {"server-udp-ports": port})
    return True


BEDROCK_FIREWALL_DONE = "bedrock-firewall-1.16.11"


def upgrade_bedrock_firewall():
    """Once, after upgrading to 1.16.11: Bedrock servers whose server.properties has no transport
    line were treated as RakNet, so only UDP server-port was opened and players couldn't reach
    them. Gives them a fixed gameplay port and opens what they need now. Nothing is closed."""
    marker = os.path.join(DATA_DIR, BEDROCK_FIREWALL_DONE)
    if os.path.exists(marker):
        return
    for s in list_servers():
        if s.get("edition") != "Bedrock":
            continue
        try:
            if fixed_udp_ports(s["directory"]):
                write_log(f"[FIREWALL] {s['name']} had no server-udp-ports; set it to {s['port']} "
                          f"(used from its next restart).")
            rules = server_firewall_rules(s["name"])
            if firewall.installed():
                open_firewall_rules(rules)
                firewall_record(s["name"], rules)
                write_log(f"[FIREWALL] {s['name']}: allowed " + ", ".join(f"{p}/{q}" for p, q in sorted(rules)) + ".")
        except Exception as e:                 # one odd server mustn't stop the others or the panel
            write_log(f"[FIREWALL] Couldn't check {s.get('name')}: {e}")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(marker, "w") as f:
        f.write("done\n")


def server_firewall_rules(server_name):
    return firewall_rules("java" if _is_java(server_name) else "bedrock", read_properties(get_server_dir(server_name)))


def open_firewall_rules(rules):
    for port, proto in sorted(rules):
        open_firewall(port, proto)


def phantom_firewall_rules(phantoms=None):
    """The UDP ports Phantom proxies on this machine listen on: their bind port, and 19132 for
    LAN discovery while there is any."""
    try:
        phantoms = proxies.find_phantoms() if phantoms is None else phantoms
    except Exception:
        return set()
    rules = {(str(p["bind_port"]), "udp") for p in phantoms if str(p.get("bind_port") or "").isdigit()}
    return rules | ({(str(proxies.PHANTOM_LAN_PORT), "udp")} if phantoms else set())


def rules_in_use(servers=None):
    """Every ufw rule a server on the panel or a Phantom proxy needs."""
    needed = set()
    for s in list_servers() if servers is None else servers:
        needed |= {tuple(r) for r in s.get("firewall_rules") or []}
    try:
        from core import sftp            # 1.18: the SFTP port, while SFTP is on
        needed |= sftp.firewall_rules()
    except Exception:
        pass
    return needed | phantom_firewall_rules()


def close_firewall_rules(rules):
    """Removes rules no server on the panel needs any more (another server may share one, as
    every NetherNet server shares 7551)."""
    if not rules or not firewall.installed():
        return
    for port, proto in sorted(set(rules) - rules_in_use()):
        firewall.remove(port, proto)
    _firewall_changed()


# The rules the panel last opened for each server, so a change it didn't make (server.properties
# edited over SSH, a unit deleted by hand) can be followed: {"minecraft-x": [["19132", "tcp"], ...]}
FIREWALL_RECORD = "firewall-rules.json"
_fw_lock = threading.RLock()


def _fw_path():
    return os.path.join(DATA_DIR, FIREWALL_RECORD)


def _fw_load():
    try:
        with open(_fw_path()) as f:
            data = json.load(f)
        return {k: {tuple(r) for r in v} for k, v in data.items() if isinstance(v, list)}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _fw_save(rec):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _fw_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump({k: sorted(map(list, v)) for k, v in sorted(rec.items())}, f, indent=1)
    os.replace(tmp, _fw_path())


def firewall_record(server_name, rules):
    """Notes the rules the panel opened for a server (None: the server left the panel)."""
    with _fw_lock:
        rec = _fw_load()
        if rules is None:
            rec.pop(server_name, None)
        else:
            rec[server_name] = set(rules)
        _fw_save(rec)


def open_server_firewall(server_name, edition, d):
    """Opens the rules a server needs from its server.properties, and notes them."""
    rules = firewall_rules(edition, read_properties(d))
    open_firewall_rules(rules)
    firewall_record(server_name, rules)


def sync_firewall():
    """Keeps ufw in step with server.properties when it changed without the panel: a server whose
    ports or transport were edited by hand gets its new rules and loses the old ones; a server
    whose unit is gone loses its rules. Rules another server or a Phantom still needs stay.
    A server seen for the first time is only noted (the dashboard names rules it lacks)."""
    if not firewall.installed():
        return []
    done = []
    with _fw_lock:
        servers = list_servers()
        with _busy_lock:
            busy = set(_busy)
        rec = _fw_load()
        now = {s["name"]: {tuple(r) for r in s.get("firewall_rules") or []} for s in servers}
        in_use = rules_in_use(servers)
        changed = False
        for name, need in now.items():
            if name in busy:
                continue
            had = rec.get(name)
            if had is None:
                rec[name] = need
                changed = True
            elif had != need:
                opened, closed = need - had, had - need - in_use
                open_firewall_rules(opened)
                for port, proto in sorted(closed):
                    firewall.remove(port, proto)
                rec[name] = need
                changed = True
                done.append(name)
                write_log(f"[FIREWALL] {name}'s server.properties changed outside the panel: "
                          + "; ".join(x for x in (
                              opened and "allowed " + ", ".join(f"{p}/{q}" for p, q in sorted(opened)),
                              closed and "removed " + ", ".join(f"{p}/{q}" for p, q in sorted(closed))) if x)
                          + ".")
        for name in sorted(set(rec) - set(now) - busy):
            closed = rec.pop(name) - in_use
            changed = True
            for port, proto in sorted(closed):
                firewall.remove(port, proto)
            if closed:
                done.append(name)
                write_log(f"[FIREWALL] {name} is no longer on the panel: removed "
                          + ", ".join(f"{p}/{q}" for p, q in sorted(closed)) + ".")
        if changed:
            _fw_save(rec)
    if done:
        _firewall_changed()
    return done


def firewall_watcher(every=120, first=20):
    """Runs sync_firewall() in a daemon thread: shortly after start, then every two minutes."""
    def loop():
        time.sleep(first)
        while True:
            try:
                sync_firewall()
            except Exception as e:             # never let this take the panel down
                write_log(f"[FIREWALL] Checking the firewall failed: {e}")
            time.sleep(every)
    t = threading.Thread(target=loop, name="firewall", daemon=True)
    t.start()
    return t


def properties_changed(server_name, before):
    """After server.properties changed: opens the rules the server needs now and closes the
    ones it no longer needs (a new port, or a switch between RakNet and NetherNet)."""
    after = server_firewall_rules(server_name)
    if after != before:
        open_firewall_rules(after - before)
        close_firewall_rules(before - after)
    firewall_record(server_name, after)

# Never part of a template: each server keeps its own RCON password. Templates saved before
# 1.15.1 may still hold one; it's left out when they're read.
TEMPLATE_SKIP = ("rcon.password",)

def template_properties(props):
    """A server's properties as a template keeps them: without the server's own secrets."""
    return {k: v for k, v in (props or {}).items() if k not in TEMPLATE_SKIP}

def load_templates():
    if not os.path.exists(TEMPLATE_FILE): return []
    with open(TEMPLATE_FILE, 'r') as f: data = json.load(f)
    for t in data:
        if isinstance(t, dict) and isinstance(t.get("properties"), dict):
            t["properties"] = template_properties(t["properties"])
    return data

def save_templates(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TEMPLATE_FILE, 'w') as f: json.dump(data, f, indent=2)

def save_server_as_template(server_name, template_name):
    templates = load_templates()
    props = template_properties(read_properties(get_server_dir(server_name)))
    perms = read_permissions(server_name)
    
    exec_start = services.exec_start(server_name)
    edition = "Java" if "java" in exec_start else "Bedrock"
    
    temp_id = str(uuid.uuid4())
    templates.append({
        "id": temp_id,
        "name": template_name,
        "edition": edition,
        "properties": props,
        "permissions": perms
    })
    save_templates(templates)
    return temp_id

class TemplateError(ValueError):
    """A template that doesn't exist, or doesn't match the server's edition."""

def get_checked_template(template_id, edition):
    """Returns the template, or raises TemplateError if it is missing or made for the other edition."""
    temp = next((t for t in load_templates() if t.get("id") == template_id), None)
    if not temp:
        raise TemplateError("Template not found.")
    if str(temp.get("edition", "")).lower() != str(edition).lower():
        raise TemplateError(f"'{temp.get('name')}' is a {temp.get('edition')} template; "
                            f"it can't be applied to a {str(edition).capitalize()} server.")
    return temp

def apply_template_to_server(server_name, template_id, edition=None):
    """edition: 'java' / 'bedrock'; detected from the systemd unit when not given."""
    if edition is None:
        edition = "java" if _is_java(server_name) else "bedrock"
    temp = get_checked_template(template_id, edition)

    # Merge properties to preserve the host ports to prevent collisions
    current_props = read_properties(get_server_dir(server_name))
    port = current_props.get("server-port")
    portv6 = current_props.get("server-portv6")
    udp_ports = current_props.get("server-udp-ports")
    rcon_port = current_props.get("rcon.port")
    query_port = current_props.get("query.port")  # Extract existing query port
    
    # The server keeps its own RCON password (template_properties leaves it out)
    merged = {**current_props, **template_properties(temp["properties"])}
    
    # Re-apply the unique ports so the template does not cause conflicts
    if port: merged["server-port"] = port
    if portv6: merged["server-portv6"] = portv6
    # Bedrock gameplay port: the server's own, never another server's from the template
    if udp_ports: merged["server-udp-ports"] = udp_ports
    else: merged.pop("server-udp-ports", None)
    if rcon_port: merged["rcon.port"] = rcon_port
    if query_port: merged["query.port"] = query_port  # Protect query port
        
    write_properties(get_server_dir(server_name), merged)
    write_permissions(server_name, temp["permissions"])
    return True

def update_template(template_id, properties, permissions):
    templates = load_templates()
    for t in templates:
        if t["id"] == template_id:
            t["properties"] = template_properties(properties)
            t["permissions"] = permissions
    save_templates(templates)

def delete_template(template_id):
    templates = load_templates()
    templates = [t for t in templates if t["id"] != template_id]
    save_templates(templates)

def write_log(msg):
    with open(LOG_FILE, "a") as f:
        f.write(msg + "\n")

def run_and_log(cmd, shell=False):
    """Executes a command, streams output to panel.log, and waits for completion."""
    cmd_str = cmd if shell else " ".join(cmd)
    write_log(f"\n[SYSTEM] Executing: {cmd_str}")
    
    process = subprocess.Popen(cmd, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in process.stdout:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    
    process.wait()
    if process.returncode != 0:
        write_log(f"[ERROR] Command exited with code {process.returncode}")
    return process.returncode

def get_panel_logs(lines=100):
    if not os.path.exists(LOG_FILE):
        return "Awaiting commands..."
    with open(LOG_FILE, "r") as f:
        return "".join(f.readlines()[-lines:])

def get_server_dir(svc_name):
    d = services.working_dir(svc_name)
    return d if d else paths.server_dir(svc_name)

def _java_properties(dir_path):
    """Java Edition escapes server.properties like java.util.Properties; Bedrock doesn't."""
    return not os.path.exists(os.path.join(dir_path, "bedrock_server"))

def _read_text(path):
    if not os.path.exists(path):
        return ""
    # surrogateescape: bytes that aren't UTF-8 are written back exactly as they were
    with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()

def read_properties(dir_path):
    """The settings as the game server sees them (Java escapes decoded)."""
    content = _read_text(os.path.join(dir_path, "server.properties"))
    return properties.parse(content, java=_java_properties(dir_path))

def write_properties(dir_path, new_props):
    """Changes the given keys, escaped the way the game server expects; other lines stay as they are."""
    prop_file = os.path.join(dir_path, "server.properties")
    content = properties.update(_read_text(prop_file), new_props, java=_java_properties(dir_path))
    with open(prop_file, "w", encoding="utf-8", errors="surrogateescape", newline="\n") as f:
        f.write(content)

def read_raw_properties(dir_path):
    prop_file = os.path.join(dir_path, "server.properties")
    if os.path.exists(prop_file):
        with open(prop_file, "r") as f:
            return f.read()
    return ""

def write_raw_properties(dir_path, content):
    with open(os.path.join(dir_path, "server.properties"), "w") as f:
        f.write(content)

SERVERS_ROOT = paths.SERVERS_ROOT
PORT_KEYS = ("server-port", "server-portv6", "query.port", "rcon.port")

def _used_ports(exclude_dir=None):
    """Every port that a managed server (/opt/minecraft-*) actually opens, per its server.properties.
    Bedrock has no RCON, so an rcon.port line in a Bedrock server's file (e.g. from a Java
    template) is ignored and never blocks a Java server from using that port."""
    used = set()
    if not os.path.isdir(SERVERS_ROOT):
        return used
    skip = os.path.realpath(exclude_dir) if exclude_dir else None
    for name in os.listdir(SERVERS_ROOT):
        path = os.path.join(SERVERS_ROOT, name)
        if not name.startswith("minecraft-") or os.path.realpath(path) == skip:
            continue
        props = read_properties(path)
        is_bedrock = os.path.exists(os.path.join(path, "bedrock_server"))
        for k in PORT_KEYS:
            if is_bedrock and k == "rcon.port":
                continue
            v = str(props.get(k, "")).strip()
            if v.isdigit():
                used.add(int(v))
        if is_bedrock:
            used |= _udp_ports_used(props.get("server-udp-ports"))
    return used

def _port_bindable(port):
    """True if nothing else on the host is listening on this TCP port right now."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False

def _free_rcon_port(server_dir, start=25575):
    """First port from 25575 up that no managed server claims (this server's own game
    ports included) and that no other process is listening on."""
    used = _used_ports(exclude_dir=server_dir)
    own = read_properties(server_dir)
    for k in ("server-port", "server-portv6", "query.port"):
        v = str(own.get(k, "")).strip()
        if v.isdigit():
            used.add(int(v))
    port = start
    while port in used or not _port_bindable(port):
        port += 1
    return str(port)

# ---------------------------------------------------------------------------
# Minecraft / Java versions
# ---------------------------------------------------------------------------
_version_cache = {}          # path -> ((mtime, size), result)
_java_cache = {"at": 0.0, "major": None}
BEDROCK_VERSION_RE = re.compile(r"Version:? ([0-9]+(?:\.[0-9]+)+)")
_VERSION_LINE_RE = re.compile(rb"Version:? [0-9]+(\.[0-9]+)+")
_START_LINE_RE = re.compile(bedrock_net.GREP_PATTERN.encode())


def _matching_lines(path, pattern):
    """The lines of a (possibly big, possibly not quite text) log that match a bytes pattern."""
    try:
        with open(path, "rb") as f:
            return [line.decode("utf-8", "replace").rstrip("\r\n") for line in f if pattern.search(line)]
    except OSError:
        return []

def _cached(path, compute):
    """compute(path) re-run only when the file changes."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime, st.st_size)
    hit = _version_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    result = compute(path)
    _version_cache[path] = (key, result)
    return result

def _java_jar_info(jar):
    """Official server jars carry version.json: {"name": "26.3", "java_version": 25, ...}."""
    import zipfile
    try:
        with zipfile.ZipFile(jar) as z:
            info = json.loads(z.read("version.json"))
        return {"version": str(info.get("name") or info.get("id") or "") or None,
                "java_required": int(info["java_version"]) if info.get("java_version") else None}
    except Exception:
        return None

def _bedrock_log_version(log):
    """BDS prints 'Version: 1.26.63.1' at every start; the last one is the running build."""
    found = BEDROCK_VERSION_RE.findall("\n".join(_matching_lines(log, _VERSION_LINE_RE)))
    return {"version": found[-1], "java_required": None} if found else None

_start_cache = {}            # server.log path -> ((mtime, size), status)

def bedrock_start_status(d):
    """What BDS logged at its last start (see bedrock_net.last_start_status); None without a server.log."""
    log = os.path.join(d, "server.log")
    try:
        st = os.stat(log)
    except OSError:
        return None
    key = (st.st_mtime, st.st_size)
    hit = _start_cache.get(log)
    if hit and hit[0] == key:
        return hit[1]
    status = bedrock_net.last_start_status(_matching_lines(log, _START_LINE_RE))
    _start_cache[log] = (key, status)
    return status

_journal_cache = {}          # service -> (checked_at, result)

def _bedrock_journal_version(svc):
    """Older installs run BDS in screen without a server.log; its startup lines may be in the journal."""
    hit = _journal_cache.get(svc)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    found = BEDROCK_VERSION_RE.findall(services.log(services.unit(svc)))
    result = {"version": found[-1], "java_required": None} if found else None
    _journal_cache[svc] = (time.time(), result)
    return result

def _java_server_info(jar):
    """Loader, Minecraft version and needed Java of a Java server folder (cached on its jar)."""
    info = javaservers.read_info(os.path.dirname(jar))
    jar_info = (_java_jar_info(jar) if jar.endswith(".jar") else None) or {}
    if not info.get("minecraft") and jar_info.get("version"):
        info = {"loader": "vanilla", "minecraft": jar_info["version"]}
    return {"version": javaservers.describe(info) or jar_info.get("version"),
            "loader": info.get("loader"),
            "minecraft": info.get("minecraft"),
            "java_required": info.get("java_required") or jar_info.get("java_required")}

def server_version(d, is_java, svc=None):
    empty = {"version": None, "java_required": None, "loader": None, "minecraft": None}
    if is_java:
        jar = os.path.join(d, "server.jar")
        if not os.path.isfile(jar):
            # Forge and NeoForge have no server.jar: what the panel recorded at install
            return _cached(os.path.join(d, javaservers.INFO_FILE), _java_server_info) or empty
        return _cached(jar, _java_server_info) or empty
    return (_cached(os.path.join(d, "server.log"), _bedrock_log_version)
            or (svc and _bedrock_journal_version(svc)) or empty)

def java_major():
    """Major version of the default 'java' (e.g. 25), or None if Java isn't installed."""
    now = time.time()
    if now - _java_cache["at"] > 60:
        major = None
        if shutil.which("java"):
            out = system.java_version_text()
            m = re.search(r'version "(\d+)(?:\.(\d+))?', out)
            if m:
                major = int(m.group(2)) if m.group(1) == "1" else int(m.group(1))   # "1.8" -> 8
        _java_cache.update(at=now, major=major)
    return _java_cache["major"]

def list_servers():
    servers = []
    names = [u.replace(services.SUFFIX, "") for u in services.unit_files("minecraft")]
    phantoms = None              # looked up once, only if there's a Bedrock server

    for svc in names:
        st = services.unit_state(svc)
        d = get_server_dir(svc)
        props = read_properties(d)
        exec_start = services.exec_start(svc)
        ver = server_version(d, "java" in exec_start, svc)
        is_bedrock = "java" not in exec_start and "bedrock" in exec_start
        proxy_list = []
        transport = bedrock_transport(props) if is_bedrock else None
        udp_ports = props.get("server-udp-ports", "")
        start = bedrock_start_status(d) if is_bedrock else None
        if is_bedrock:
            if phantoms is None:
                phantoms = proxies.find_phantoms()
            proxy_list = proxies.proxies_for_server(phantoms, props.get("server-port", "19132"))

        try:
            from core import updates
            update = updates.server_update(svc, d, "java" in exec_start)
        except Exception:
            update = None                       # never let the alert break the server list
        servers.append({
            "name": svc,
            # A newer release than it runs (cached by the update check, core/updates.py), or None
            "update": update,
            # Minecraft version of the installed server, and (Java) the Java it needs vs. what's installed
            "version": ver["version"],
            "java_required": ver["java_required"],
            "java_installed": unit_java_major(exec_start) if "java" in exec_start else None,
            "java_path": unit_java(exec_start) if "java" in exec_start else None,
            "jvm_flags": _flags_of(svc) if "java" in exec_start else None,
            "loader": ver.get("loader"),
            "minecraft": ver.get("minecraft"),
            "status": "online" if st == "active" else "offline",
            "directory": d,
            "port": props.get("server-port", "Unknown"),
            # Bedrock chat goes through the screen session, not RCON
            "rcon_port": props.get("rcon.port", "Not Configured") if "java" in exec_start else "Not used (Bedrock)",
            "edition": "Java" if "java" in exec_start else "Bedrock" if "bedrock" in exec_start else "Unknown",
            "ram": re.search(r'-Xmx([^ ]+)', exec_start).group(1) if "java" in exec_start and re.search(r'-Xmx([^ ]+)', exec_start) else "N/A",
            "motd": props.get("motd", props.get("server-name", "Minecraft Server")),
            # Bedrock network transport. NetherNet: TCP server-port for signaling + UDP server-udp-ports for gameplay
            "transport": transport,
            "udp_ports": udp_ports if is_bedrock else None,
            # public-address form (IP:public:local) stopped the signaling listener on 1.26.63.1
            "udp_ports_address": bedrock_net.udp_ports_have_address(udp_ports) if is_bedrock else False,
            "udp_ports_plain": bedrock_net.plain_udp_ports(udp_ports) if is_bedrock else None,
            "transport_rejected": bool(start and start["transport_rejected"]),
            # NetherNet only: finished starting but never logged "Accepting clients on ..." (players can't connect)
            "signaling_closed": bool(is_bedrock and transport == "nethernet" and st == "active"
                                     and start and start["started"] and not start["accepting"]),
            # Phantom proxies on this machine that point at this server or hold its port
            "proxies": proxy_list,
            # ufw rules it needs: [[port spec, proto], ...]
            "firewall_rules": sorted([list(r) for r in firewall_rules(
                "java" if "java" in exec_start else "bedrock", props)]),
        })
    return servers

# ---------------------------------------------------------------------------
# PLAYER ROLES: everyone seen in the logs + everyone in ops.json / permissions.json
# ---------------------------------------------------------------------------
JAVA_ROLES = ["operator", "member"]
BEDROCK_ROLES = ["operator", "member", "visitor"]
ROLE_RANK = {"operator": 0, "member": 1, "visitor": 2}

JAVA_JOIN_RE = re.compile(r'([A-Za-z0-9_]{3,16})(?:\[[^\]]*\])? joined the game')
JAVA_UUID_RE = re.compile(r'UUID of player ([A-Za-z0-9_]{3,16}) is ([0-9a-fA-F-]{32,36})')
# "Player connected: Name, xuid: 123"  /  "Player Spawned: Name xuid: 123, pfid: ..."
BE_PLAYER_RE = re.compile(r'Player (?:connected|disconnected|Spawned):\s*(.+?),?\s+xuid:\s*(\d+)')

class PlayerError(ValueError):
    """A role change that can't be made (unknown role, ID not resolvable)."""

def _load_json_list(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _player_log_text(server_name, d, is_java):
    """Journal plus the server's own log file (Bedrock writes its console to server.log via screen)."""
    parts = [services.log(services.unit(server_name), lines=5000, output=None)]
    log_file = os.path.join(d, "logs", "latest.log") if is_java else os.path.join(d, "server.log")
    if os.path.exists(log_file):
        parts.append(_read_tail(log_file, 20000)[0])
    return ANSI_RE.sub('', "\n".join(parts))

def _players_from_logs(server_name, d, is_java):
    """Returns {name_lower: (name, id_or_None)} for every player the logs mention."""
    text = _player_log_text(server_name, d, is_java)
    seen = {}
    if is_java:
        for name in JAVA_JOIN_RE.findall(text):
            seen.setdefault(name.lower(), (name, None))
        for name, pid in JAVA_UUID_RE.findall(text):
            seen[name.lower()] = (name, pid)
    else:
        for name, xuid in BE_PLAYER_RE.findall(text):
            name = name.strip()
            if name:
                seen[name.lower()] = (name, xuid)
    return seen

def get_recent_players(server_name):
    """Consolidated player list: logs + the permission file, merged by name or ID.
    Each player: name, role, id (uuid/xuid), in_log, in_file, name_known."""
    d = get_server_dir(server_name)
    is_java = _is_java(server_name)
    perm_file = "ops.json" if is_java else "permissions.json"

    players = {}            # key -> player dict
    by_id = {}              # id -> key

    def upsert(name, pid=None):
        key = None
        if pid and pid in by_id:
            key = by_id[pid]
        elif name and name.lower() in players:
            key = name.lower()
        if key is None:
            key = name.lower() if name else f"id:{pid}"
            players[key] = {"name": name or f"xuid {pid}", "role": "member", "id": None,
                            "in_log": False, "in_file": False, "name_known": bool(name)}
        p = players[key]
        if pid and not p["id"]:
            p["id"] = pid
            by_id[pid] = key
        if name and not p["name_known"]:
            p["name"], p["name_known"] = name, True
        return p

    for name, pid in _players_from_logs(server_name, d, is_java).values():
        upsert(name, pid)["in_log"] = True

    for entry in _load_json_list(os.path.join(d, perm_file)):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip() or None
        pid = str(entry.get("uuid" if is_java else "xuid") or "").strip() or None
        if not name and not pid:
            continue
        p = upsert(name, pid)
        p["in_file"] = True
        p["role"] = "operator" if is_java else str(entry.get("permission", "member")).lower()

    result = sorted(players.values(), key=lambda p: (ROLE_RANK.get(p["role"], 9), p["name"].lower()))
    return {"players": result, "roles": JAVA_ROLES if is_java else BEDROCK_ROLES, "file": perm_file}

def _unit_exec_start(server_name):
    return services.exec_start(server_name)

def unit_java(exec_start):
    """The java binary a Java unit runs (from systemctl's ExecStart, or the unit line)."""
    m = re.search(r"path=(\S+)", exec_start) or re.match(r"\s*(\S+)", exec_start)
    return m.group(1) if m else None

def unit_java_major(exec_start):
    path = unit_java(exec_start)
    if path and os.path.realpath(path).startswith(os.path.realpath(javaservers.JVM_ROOT) + os.sep) and \
            os.path.dirname(path) != os.path.dirname(paths.DEFAULT_JAVA):
        return javaservers.java_major_of(path)
    return java_major()                      # /usr/bin/java: the system default

def unit_heap(exec_start):
    heap = {}
    for key in ("xms", "xmx"):
        m = re.search(rf"-X{key[1:]}(\S+)", exec_start)
        if m and HEAP_RE.match(m.group(1)):
            heap[key] = m.group(1)
    return heap

def update_server_binary(server_name, version=None, backup=True, actor="Update button"):
    """Downloads the server software again. A Java server gets the chosen Minecraft version
    (default: the newest) of the loader it already uses, and its unit the Java that needs.
    backup (Java): take a "Before update" backup first and keep the old server.jar, so Roll back
    the last update (core/convert.py, 1.19) can put both back."""
    _check_server(server_name)
    d = get_server_dir(server_name)
    exec_start = _unit_exec_start(server_name)
    is_java = "java" in exec_start
    if not is_java:
        if version:
            raise ServerError("Bedrock servers always update to the newest release.")
        require_bedrock_machine()
        # Downloaded and checked first, a "Before update" backup, and the old version put back
        # if the new one doesn't start (core/updates.py, 1.16.12)
        from core import updates
        try:
            updates.run_update(server_name, actor="Update button", force=True)
        except updates.UpdateError as e:
            raise ServerError(f"Bedrock update failed: {e}")
        return

    if is_java:
        current = javaservers.read_info(d)
        loader = current.get("loader") or "vanilla"
        from core import backups, convert, updates
        state = {"pre": None}

        def before_swap(info):
            was_running = backups._is_active(server_name)
            services.stop(server_name, elevate=True)
            if backup:
                try:
                    state["pre"] = backups.run_backup(server_name, "pre-update")["file"]
                    updates._prune_pre_update(server_name)
                except Exception as e:
                    if was_running:
                        services.start(server_name, elevate=True)
                    raise ServerError(f"The backup before updating failed, so nothing was changed: {e}")
            old_java = unit_java(exec_start) or paths.DEFAULT_JAVA
            convert.keep_previous(d, current)
            if current.get("loader") and current.get("minecraft"):
                convert.remember(server_name, "update", current, info, state["pre"], old_java, actor=actor,
                                 was_running=was_running)

        info, java = reinstall_java(server_name, loader, version, None, current, before_swap=before_swap)
        pre = state["pre"]
        write_log(f"[UPDATE] {server_name} now runs {javaservers.describe(info)} with {java}"
                  + (f" (was {javaservers.describe(current)}; backup from before: {pre})." if pre else "."))
    from core import updates
    updates.forget(server_name)
    system.give_to_game_account(d)
    services.start(server_name, elevate=True)

def reinstall_java(server_name, loader, version=None, build=None, current=None, before_swap=None):
    """Downloads a Java server's software again (a chosen Minecraft version and build, default
    the newest) and swaps it in, rewriting the unit when the Java or what it starts changed.
    Doesn't stop or start the server: before_swap(info) runs once the download is ready.
    Returns (info, java path)."""
    d = get_server_dir(server_name)
    exec_start = _unit_exec_start(server_name)
    current = javaservers.read_info(d) if current is None else current
    staging = tempfile.mkdtemp(prefix=".redstonepanel-update-", dir=d)
    try:
        # Download next to the server first, so a failed download leaves it untouched
        # (Quilt, Forge and NeoForge run their installer there, as the minecraft user)
        info, java = _install_java(staging, loader, version, current, build=build)
        if before_swap:
            before_swap(info)
        before = javaservers.launch_args(d, current)
        javaservers.adopt(staging, d)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if os.path.realpath(unit_java(exec_start) or "") != os.path.realpath(java) \
            or javaservers.launch_args(d, info) != before:
        _write_unit(server_name, d, "java", java=java, **unit_heap(exec_start))
    return info, java

def _install_java(d, loader="vanilla", version=None, current=None, build=None, snapshot=False, java_major=None):
    """Downloads a Java server's jar into d. Returns (info, java path)."""
    write_log(f"[SYSTEM] Downloading {javaservers.LOADERS.get(loader, loader)} {version or '(newest)'}"
              + (f" build {build}" if build else "") + " ...")
    try:
        info, java = javaservers.install(d, loader, version, current, build=build, snapshot=snapshot,
                                         java_major=java_major)
    except javaservers.MissingJavaError as e:
        write_log(f"[WARNING] {e}")
        raise JavaMissing(e)
    except javaservers.JavaServerError as e:
        write_log(f"[WARNING] {e}")
        raise ServerError(str(e))
    write_log(f"[SYSTEM] Downloaded {javaservers.describe(info)}"
              + (f" (build {info['build']})" if info.get("build") else "")
              + f"; it needs Java {info['java_required']}, using {java}.")
    return info, java

def java_for_folder(d):
    """The java to run an existing Java server folder with (import, restore)."""
    info = javaservers.read_info(d)
    need = info.get("java_required") or (_java_jar_info(os.path.join(d, "server.jar")) or {}).get("java_required")
    java_max = javaservers.forge_java_max(info["minecraft"]) if info.get("loader") == "forge" \
        and javaservers.is_release(info.get("minecraft")) else None
    if not need and info.get("minecraft"):
        try:
            need = javaservers.mojang_version(info["minecraft"])["java_required"]
        except javaservers.JavaServerError:
            need = None
    if not need:
        return paths.DEFAULT_JAVA
    try:
        return javaservers.choose_java(need, info.get("minecraft"), java_max)
    except javaservers.MissingJavaError as e:
        raise JavaMissing(e)

def _fetch_bedrock(d):
    """Downloads the latest Bedrock Dedicated Server and unpacks it into d, keeping the folder's
    own server.properties, permissions.json and allowlist.json (see core/updates.py)."""
    from core import updates
    try:
        updates.fetch_bedrock(d)
    except updates.UpdateError as e:
        write_log(f"[WARNING] {e}")
        raise ServerError(str(e))

def meminfo_text(path=None):
    """This machine's memory as /proc/meminfo has it ("MemTotal: N kB" ...); on a system without
    /proc, the same text from the platform layer. "" when it can't be read."""
    try:
        with open(path or os.path.join(paths.PROC, "meminfo")) as f:
            return f.read()
    except OSError:
        return system.machine_file("meminfo") if path in (None, os.path.join(paths.PROC, "meminfo")) else ""

def default_heap(meminfo=os.path.join(paths.PROC, "meminfo")):
    """A new Java server's heap: 1-2 GB, or 512 MB-1 GB on a machine with 3 GB of memory or less
    (a 2 GB Raspberry Pi), where 2 GB would leave nothing for the system."""
    try:
        kb = int(re.search(r"^MemTotal:\s+([0-9]+) kB", meminfo_text(meminfo), re.M).group(1))
    except (AttributeError, ValueError):
        kb = 0
    return {"xms": "512M", "xmx": "1G"} if 0 < kb <= 3 * 1024 * 1024 else {"xms": "1G", "xmx": "2G"}

def _write_unit(svc_name, mc_dir, edition, xms=None, xmx=None, java=paths.DEFAULT_JAVA, jvm=None):
    """Writes the game server's systemd unit and reloads systemd (doesn't enable or start it).
    A Java server without a heap given gets default_heap().
    jvm: startup flags ({"preset", "extra"}, see core/deployopts.py) to store and use; None keeps
    the ones stored for this server, {} removes them."""
    from core import deployopts
    if not (xms and xmx):
        heap = default_heap()
        xms, xmx = xms or heap["xms"], xmx or heap["xmx"]
    if edition == "bedrock":
        exec_start = console.start_command(svc_name, mc_dir, "./bedrock_server", paths.SCREEN)
        env_vars = "Environment=\"LD_LIBRARY_PATH=.\""
    else:
        if jvm is not None:
            deployopts.set_flags(svc_name, jvm)
        flags = deployopts.get_flags(svc_name)
        if flags and flags.get("preset") == "aikar":
            xms = xmx                                  # Aikar's flags: the whole heap up front
        extra = " ".join(deployopts.unit_flags(flags, xmx))
        # "-jar server.jar", or what a Quilt, Forge or NeoForge install starts with (1.20)
        launch = javaservers.launch_args(mc_dir)
        exec_start = f"{java} -Xms{xms} -Xmx{xmx} {extra + ' ' if extra else ''}{launch} nogui"
        env_vars = ""
    svc_content = f"""[Unit]
Description=Minecraft Server ({svc_name})
After=network.target

[Service]
User=minecraft
Group=minecraft
WorkingDirectory={mc_dir}
{env_vars}
ExecStart={exec_start}
Restart=on-failure
RestartSec=20

[Install]
WantedBy=multi-user.target
"""
    with open(_unit_path(svc_name), "w") as f:
        f.write(svc_content)
    services.reload()

class ServerError(ValueError):
    """A request about a server that can't be done (message is safe to show)."""

POWER_ACTIONS = ("start", "stop", "restart")
SERVICE_NAME_RE = re.compile(r"^minecraft[A-Za-z0-9_.@-]*$")

def control_server(server_name, action):
    # Only start/stop/restart, and only the panel's own game server units: never another service
    if action not in POWER_ACTIONS:
        raise ServerError(f"Unknown action '{action}'.")
    if not SERVICE_NAME_RE.match(server_name or "") or \
            not os.path.isfile(_unit_path(server_name)):
        raise ServerError(f"No Minecraft server called '{server_name}' on this panel.")
    if action != "stop":
        from core import transfer
        if transfer.is_moving(server_name):
            raise ServerError(f"{server_name} is being moved to another cluster member; it can't be started here.")
    services.control(services.unit(server_name), action, elevate=True)

def _unit_path(server_name):
    return paths.unit_file(services.unit(server_name))

def _check_server(server_name):
    if not SERVICE_NAME_RE.match(server_name or "") or not os.path.isfile(_unit_path(server_name)):
        raise ServerError(f"No Minecraft server called '{server_name}' on this panel.")

# ---------------------------------------------------------------------------
# REMOVED SERVERS: "Uninstall (Keep World)" deletes only the systemd unit. The folder in
# /opt stays, and can be restored: a new unit, the same name, a free port.
# ---------------------------------------------------------------------------
# Written into the folder on uninstall. The folder belongs to the minecraft user, so this
# file is never trusted: only an edition and validated heap sizes are read from it.
REMOVED_INFO = ".redstonepanel-removed.json"
HEAP_RE = re.compile(r"^[0-9]{1,6}[KkMmGg]$")

# Names being deployed, imported or restored right now: their folders exist before their
# units do, so they must not be listed as removed or picked again.
_busy = set()
_busy_lock = threading.Lock()

def _reserve_name(edition):
    """Picks the next free minecraft-<edition>[-N] name and game port, and holds the name until
    _release_name(), so parallel deploys/imports can't take the same one."""
    base_name = "minecraft-java" if edition == "java" else "minecraft-bedrock"
    port = 25565 if edition == "java" else 19132
    step = 1 if edition == "java" else 2
    counter, suffix = 1, ""
    with _busy_lock:
        while True:
            name = f"{base_name}{suffix}"
            taken = (name in _busy or os.path.exists(os.path.join(SERVERS_ROOT, name))
                     or os.path.exists(_unit_path(name)))
            if not taken:
                # Check every port other servers claim (RCON included), and Bedrock's IPv6 port too
                needed = {port, port + 1} if edition == "bedrock" else {port}
                taken = bool(needed & _used_ports())
            if not taken:
                _busy.add(name)
                return name, port
            counter += 1
            suffix = f"-{counter}"
            port += step

def _release_name(name):
    with _busy_lock:
        _busy.discard(name)

def remove_server(server_name, keep_world, note=None):
    """Stops the server and deletes its systemd unit, so it leaves the panel.
    keep_world=True keeps the whole folder (server binary included), so it can be restored
    later; keep_world=False deletes the folder too. note: more fields for the removed-server
    note (a move adds moved_to)."""
    _check_server(server_name)
    d = get_server_dir(server_name)
    exec_start = services.exec_start(server_name)
    rules = firewall_rules("java" if "java" in exec_start else "bedrock", read_properties(d))
    services.stop(server_name, elevate=True)
    services.disable(server_name, elevate=True)

    if os.path.exists(_unit_path(server_name)):
        os.remove(_unit_path(server_name))
    services.reload(elevate=True)
    # If it had failed, systemd would keep listing it as a failed unit with no unit file
    services.reset_failed(services.unit(server_name))
    # Its ports are closed again unless another server uses them (restoring opens them again)
    close_firewall_rules(rules)
    firewall_record(server_name, None)
    # Its backup and restart timers go too; the backups themselves stay in /var/backups/redstonepanel
    from core import backups, restarts, updates, deployopts
    backups.on_server_removed(server_name, purged=not keep_world)
    if not keep_world:
        deployopts.set_flags(server_name, None)     # kept with the folder, for a restore
    restarts.on_server_removed(server_name, purged=not keep_world)
    updates.on_server_removed(server_name, purged=not keep_world)
    from core import convert
    convert.forget(server_name)
    from core import perf
    perf.on_server_removed(server_name, purged=not keep_world)
    # Its Phantom LAN proxy goes too (Phantom units made by hand stay)
    from core import phantom
    try:
        phantom.on_server_removed(server_name)
    except Exception as e:      # never block removing the server
        write_log(f"[PHANTOM] Couldn't remove the Phantom of {server_name}: {e}")

    if keep_world:
        if os.path.isdir(d):
            info = {"edition": "java" if "java" in exec_start else "bedrock" if "bedrock" in exec_start else None,
                    "removed_at": int(time.time())}
            info.update(unit_heap(exec_start))
            info.update(note or {})
            try:
                with open(os.path.join(d, REMOVED_INFO), "w") as f:
                    json.dump(info, f)
            except OSError:
                pass          # only a hint for restoring; the folder alone is enough
        write_log(f"[REMOVE] Removed {server_name} from the panel. Its files stay in {d}; "
                  f"restore it from New Server > Removed servers.")
    elif os.path.exists(d) and d.startswith(os.path.join(paths.SERVERS_ROOT, "")):
        system.delete_tree(d)
        write_log(f"[REMOVE] Removed {server_name} and deleted {d}.")

def _read_removed_info(d):
    try:
        with open(os.path.join(d, REMOVED_INFO)) as f:
            info = json.load(f)
        return info if isinstance(info, dict) else {}
    except (OSError, ValueError):
        return {}

def _unit_dirs():
    """The folder of every minecraft*.service unit, including ones written by hand."""
    dirs = set()
    unit_dir = paths.UNIT_DIR
    if not os.path.isdir(unit_dir):
        return dirs
    for f in os.listdir(unit_dir):
        if not (f.startswith("minecraft") and f.endswith(".service")):
            continue
        try:
            with open(os.path.join(unit_dir, f)) as fh:
                for line in fh:
                    if line.startswith("WorkingDirectory="):
                        dirs.add(os.path.realpath(line.split("=", 1)[1].strip()))
        except OSError:
            pass
    return dirs

def _removed_edition(d, name, info):
    """'java', 'bedrock' or None, from the files first and the folder's name last."""
    if os.path.isfile(os.path.join(d, "bedrock_server")):
        return "bedrock"
    if javaservers.is_java_folder(d):
        return "java"
    if info.get("edition") in ("java", "bedrock"):
        return info["edition"]
    if name.startswith("minecraft-bedrock"):
        return "bedrock"
    if name.startswith("minecraft-java"):
        return "java"
    if os.path.isdir(os.path.join(d, "worlds")):
        return "bedrock"
    if os.path.isfile(os.path.join(d, "eula.txt")):
        return "java"
    return None

def list_removed_servers():
    """Server folders in /opt that no systemd unit uses: servers uninstalled with
    'Keep World', or whose unit was deleted some other way."""
    if not os.path.isdir(SERVERS_ROOT):
        return []
    in_use = _unit_dirs()
    with _busy_lock:
        busy = set(_busy)
    removed = []
    for name in sorted(os.listdir(SERVERS_ROOT)):
        d = os.path.join(SERVERS_ROOT, name)
        if (not name.startswith("minecraft-") or not SERVICE_NAME_RE.match(name)
                or os.path.islink(d) or not os.path.isdir(d)):
            continue
        if name in busy or os.path.exists(_unit_path(name)) or os.path.realpath(d) in in_use:
            continue
        info = _read_removed_info(d)
        edition = _removed_edition(d, name, info)
        props = read_properties(d)
        binary = "bedrock_server" if edition == "bedrock" else "server.jar"
        removed_at = info.get("removed_at")
        removed.append({
            "name": name,
            "directory": d,
            "edition": {"java": "Java", "bedrock": "Bedrock"}.get(edition, "Unknown"),
            "port": props.get("server-port", ""),
            "motd": props.get("motd", props.get("server-name", "")),
            "world": props.get("level-name", ""),
            "has_binary": bool(edition) and (javaservers.runnable(d) if edition == "java"
                                             else os.path.isfile(os.path.join(d, binary))),
            "removed_at": removed_at if isinstance(removed_at, int) else None,
            "can_restore": edition is not None,
            # Left behind by a move to another cluster member (1.16): its name there
            "moved_to": info["moved_to"][:100] if isinstance(info.get("moved_to"), str) else None,
        })
    return removed

def _restore_ports(d, edition):
    """Keeps the server's own port unless another server took it meanwhile; then picks the
    next free one. Returns (game port, old port if it changed else None)."""
    props = read_properties(d)
    used = _used_ports(exclude_dir=d)
    base = 25565 if edition == "java" else 19132
    step = 1 if edition == "java" else 2
    need = (lambda p: {p, p + 1}) if edition == "bedrock" else (lambda p: {p})
    old = str(props.get("server-port", "")).strip()
    port = int(old) if old.isdigit() else base
    if need(port) & used:
        port = base
        while need(port) & used:
            port += step
    changes = {}
    if str(port) != old:
        changes["server-port"] = str(port)
        if edition == "bedrock":
            changes["server-portv6"] = str(port + 1)
            udp = _follow_udp_port(props, old, port)
            if udp:
                changes["server-udp-ports"] = udp
        if str(props.get("query.port", "")).strip() in ("", old):
            changes["query.port"] = str(port)
    if changes:
        write_properties(d, changes)
    if edition == "bedrock" and fixed_udp_ports(d):
        write_log(f"[RESTORE] server-udp-ports wasn't set; gameplay now uses UDP {port}.")
    if edition == "java":
        rcon_port = str(props.get("rcon.port", "")).strip()
        if rcon_port.isdigit() and int(rcon_port) in used | {port}:
            write_properties(d, {"rcon.port": _free_rcon_port(d)})
            write_log(f"[RESTORE] RCON port {rcon_port} is used by another server; moved it.")
    return port, (old if str(port) != old else None)

def restore_server(name, same_version=False):
    """Brings a removed server back: downloads the server binary only if it's missing, keeps
    its port unless it's taken, writes a new systemd unit, then enables and starts it.
    same_version: a missing Java server.jar is downloaded at the Minecraft version the folder
    records (a server made from an off-site backup, 1.16.11), not the newest."""
    match = next((s for s in list_removed_servers() if s["name"] == name), None)
    if not match:
        raise ServerError(f"There's no removed server called '{name}'.")
    if not match["can_restore"]:
        raise ServerError(f"Can't tell whether {name} is a Java or a Bedrock server: it has no server.jar, "
                          f"bedrock_server, eula.txt or worlds folder.")
    edition = match["edition"].lower()
    d = match["directory"]
    with _busy_lock:
        if name in _busy:
            raise ServerError(f"{name} is already being restored.")
        _busy.add(name)
    try:
        write_log(f"[RESTORE] Restoring {name} from {d}.")
        if edition != "java":                     # Java itself is checked below, per Minecraft version
            require_bedrock_machine()
            require_tools("screen")
        downloaded = False
        if not match["has_binary"]:
            write_log(f"[RESTORE] {name} has no server binary; downloading the latest {edition.capitalize()} release.")
            if edition == "java":
                current = javaservers.read_info(d)
                loader = current.get("loader") or "vanilla"
                if loader in javaservers.INSTALLER_LOADERS:
                    # Mods are built for one loader version: the same one is installed again
                    _install_java(d, loader, current.get("minecraft"), current,
                                  build=str(current.get("build") or "").replace("loader ", "") or None)
                else:
                    _install_java(d, loader, current.get("minecraft") if same_version else None, current)
            else:
                _fetch_bedrock(d)
            downloaded = True
        java = java_for_folder(d) if edition == "java" else None

        if edition == "java":
            eula = os.path.join(d, "eula.txt")
            if not os.path.exists(eula):
                with open(eula, "w") as f:
                    f.write("eula=true\n")
        else:
            write_screenrc(d)

        port, old_port = _restore_ports(d, edition)
        if old_port:
            write_log(f"[RESTORE] Port {old_port} is used by another server; {name} now uses port {port}.")

        info = _read_removed_info(d)
        heap = {k: info[k] for k in ("xms", "xmx") if isinstance(info.get(k), str) and HEAP_RE.match(info[k])}

        open_server_firewall(name, edition, d)
        ensure_minecraft_user(d)
        system.give_to_game_account(d)
        _write_unit(name, d, edition, **heap, **({"java": java} if java else {}))
        services.enable(name)
        services.start(name)
        try:
            os.remove(os.path.join(d, REMOVED_INFO))
        except OSError:
            pass
        from core import backups, restarts
        backups.on_server_restored(name)          # its backup and restart schedules, if it had them
        restarts.on_server_restored(name)
        from core import perf
        perf.on_server_restored(name)       # its memory and CPU limits, if it had them
        write_log(f"[RESTORE] Restored {name} (port {port}).")
        return {"name": name, "port": port, "old_port": old_port, "downloaded": downloaded}
    finally:
        _release_name(name)

def delete_removed_server(name):
    """Permanently deletes a removed server's folder."""
    match = next((s for s in list_removed_servers() if s["name"] == name), None)
    if not match:
        raise ServerError(f"There's no removed server called '{name}'.")
    d = os.path.realpath(match["directory"])
    if os.path.dirname(d) != os.path.realpath(SERVERS_ROOT):
        raise ServerError(f"{name} isn't a folder in {SERVERS_ROOT}.")
    system.delete_tree(d)
    from core import deployopts
    deployopts.set_flags(name, None)
    write_log(f"[REMOVE] Deleted the files of removed server {name} ({d}).")

def export_server(server_name):
    """Zips the server folder into a fresh temp dir (the caller deletes the dir after sending)."""
    d = get_server_dir(server_name)
    export_base = os.path.join(tempfile.mkdtemp(prefix="redstonepanel-export-"), server_name)
    shutil.make_archive(export_base, 'zip', d)
    return f"{export_base}.zip"

def import_server(archive_path):
    """Creates a new server from a .zip / .tar.gz / .tar backup. Raises archive.ArchiveError."""
    from core import archive
    staging, root = archive.unpack(archive_path, SERVERS_ROOT)
    try:
        return _import_unpacked(staging, root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)   # gone already if it was renamed into place

def _import_unpacked(staging, root):
    from core import archive
    if root != staging:
        write_log(f"[IMPORT] Backup was wrapped in the folder '{os.path.basename(root)}'; using its contents.")

    # Detect Edition from the files themselves
    if os.path.isfile(os.path.join(root, "bedrock_server")):
        edition = "bedrock"
    elif javaservers.is_java_folder(root) or os.path.isfile(os.path.join(root, "server.properties")):
        edition = "java"
    else:
        raise archive.ArchiveError("This doesn't look like a Minecraft server backup: no bedrock_server, "
                                   "server.jar or server.properties at its top level.")
    if edition == "bedrock" and not bedrock_supported():
        raise archive.ArchiveError(bedrock_unsupported_message())
    if edition == "java" and not javaservers.runnable(root):
        write_log("[WARNING] The backup has no server.jar; use Update on the new server to download one.")

    svc_name, port = _reserve_name(edition)       # auto-allocated name and port
    try:
        mc_dir = os.path.join(SERVERS_ROOT, svc_name)
        # The unpacked files become the server folder (a rename: same filesystem, no copying)
        os.rename(root, mc_dir)
        # A backup of a removed server may still carry the removal note
        if os.path.exists(os.path.join(mc_dir, REMOVED_INFO)):
            os.remove(os.path.join(mc_dir, REMOVED_INFO))
        write_log(f"[IMPORT] Imported a {edition.capitalize()} server as {svc_name} (port {port}).")

        props = read_properties(mc_dir)
        old_port = props.get("server-port", "")
        props["server-port"] = str(port)
        if edition == "bedrock":
            props["server-portv6"] = str(port + 1)
            udp = _follow_udp_port(props, old_port, port)
            if udp:
                props["server-udp-ports"] = udp
        write_properties(mc_dir, props)
        if edition == "bedrock" and fixed_udp_ports(mc_dir):
            write_log(f"[IMPORT] server-udp-ports wasn't set; gameplay uses UDP {port}.")

        java = None
        if edition == "bedrock":
            system.make_executable(f"{mc_dir}/bedrock_server")
            write_screenrc(mc_dir)
        else:
            try:
                java = java_for_folder(mc_dir)
            except DependencyError as e:
                write_log(f"[WARNING] {e} The server was imported, but won't start until it is.")
                java = paths.DEFAULT_JAVA

        open_server_firewall(svc_name, edition, mc_dir)
        ensure_minecraft_user(mc_dir)
        system.give_to_game_account(mc_dir)
        _write_unit(svc_name, mc_dir, edition, **({"java": java} if java else {}), jvm={})
        services.enable(svc_name)
        services.start(svc_name)
        return svc_name
    finally:
        _release_name(svc_name)

def _reserve_exact(name):
    """Holds this exact name if no server, folder or deploy has it. True if it's now ours."""
    if not (SERVICE_NAME_RE.match(name or "") and name.startswith("minecraft-")):
        return False
    with _busy_lock:
        if name in _busy or os.path.lexists(os.path.join(SERVERS_ROOT, name)) or os.path.exists(_unit_path(name)):
            return False
        _busy.add(name)
        return True

def import_transferred(root, edition, prefer_name=None, heap=None, java=None, start=True, on_placed=None, jvm=None):
    """A server folder moved or copied from another cluster member (1.16), unpacked at root.
    Keeps prefer_name and the server's ports when they're free here, else picks others.
    on_placed(name) is called once the folder is in place, so a failure after it can be undone.
    Returns {name, port, old_port, renamed}."""
    if edition == "bedrock":
        require_bedrock_machine()
    name = prefer_name if prefer_name and _reserve_exact(prefer_name) else None
    if not name:
        name, _ = _reserve_name(edition)
    try:
        mc_dir = os.path.join(SERVERS_ROOT, name)
        os.rename(root, mc_dir)
        if on_placed:
            on_placed(name)
        if os.path.exists(os.path.join(mc_dir, REMOVED_INFO)):
            os.remove(os.path.join(mc_dir, REMOVED_INFO))
        port, old_port = _restore_ports(mc_dir, edition)
        if edition == "bedrock":
            system.make_executable(f"{mc_dir}/bedrock_server")
            write_screenrc(mc_dir)
        else:
            java = java or java_for_folder(mc_dir)
            eula = os.path.join(mc_dir, "eula.txt")
            if not os.path.exists(eula):
                with open(eula, "w") as f:
                    f.write("eula=true\n")
        heap = {k: v for k, v in (heap or {}).items() if k in ("xms", "xmx") and HEAP_RE.match(str(v))}
        open_server_firewall(name, edition, mc_dir)
        ensure_minecraft_user(mc_dir)
        system.give_to_game_account(mc_dir)
        _write_unit(name, mc_dir, edition, **heap, **({"java": java} if java else {}), jvm=jvm or {})
        services.enable(name)
        if start:
            services.start(name)
        write_log(f"[MOVE] Installed {name} here (port {port}"
                  + (f", was {old_port}" if old_port else "") + ").")
        return {"name": name, "port": port, "old_port": old_port, "renamed": bool(prefer_name) and name != prefer_name}
    finally:
        _release_name(name)

def discard_server(name):
    """Undoes import_transferred: its unit, timers, firewall rules and folder go."""
    if os.path.exists(_unit_path(name)):
        remove_server(name, keep_world=False)
        return
    d = os.path.realpath(os.path.join(SERVERS_ROOT, name))
    if SERVICE_NAME_RE.match(name or "") and os.path.dirname(d) == os.path.realpath(SERVERS_ROOT) and os.path.isdir(d):
        system.delete_tree(d)

def _reserve_chosen(edition, name=None, port=None):
    """Advanced options (1.17.2): hold the chosen name and/or game port, picking the other as
    usual. Raises ServerError when the name or port is taken."""
    if name and not _reserve_exact(name):
        raise ServerError(f"There's already a server or folder called {name} on this machine.")
    if not name:
        name, auto_port = _reserve_name(edition)
    else:
        auto_port = None
    try:
        if port is None:
            if auto_port is None:                     # chosen name, automatic port
                port = 25565 if edition == "java" else 19132
                used = _used_ports()
                while ({port, port + 1} if edition == "bedrock" else {port}) & used:
                    port += 1 if edition == "java" else 2
            else:
                port = auto_port
        else:
            needed = {port, port + 1} if edition == "bedrock" else {port}
            clash = needed & _used_ports()
            if clash:
                raise ServerError(f"Port {min(clash)} is already used by another server on this machine.")
            if edition == "java" and not _port_bindable(port):
                raise ServerError(f"Something else on this machine is already listening on port {port}.")
        return name, port
    except Exception:
        _release_name(name)
        raise

def _heap_fits(xmx):
    """Refuses a heap that leaves too little for the system: 1 GB, or 768 MB on a machine with
    3 GB or less (as 1.17's Memory does)."""
    from core import deployopts
    try:
        total = int(re.search(r"^MemTotal:\s+([0-9]+) kB", meminfo_text(), re.M).group(1)) * 1024
    except (AttributeError, ValueError):
        return
    reserve = 768 * 1024 ** 2 if total <= 3 * 1024 ** 3 else 1024 ** 3
    if deployopts.heap_bytes(xmx) > total - reserve:
        room = max(0, (total - reserve) // 1024 ** 2)
        raise ServerError(f"A {xmx} heap leaves too little memory for the system on this machine "
                          f"(at most {room} MB here).")

def _apply_advanced_settings(svc_name, mc_dir, edition, plan, minecraft=None):
    """Writes the World and Settings choices before the first start, and fills the whitelist
    and ops list. Player names that can't be looked up are skipped with a log line."""
    from core import deployopts
    props = dict(plan.get("properties") or {})
    if plan.get("level_type"):
        props["level-type"] = deployopts.level_type_value(plan["level_type"], edition, minecraft)
    for k in ("gamemode", "difficulty"):
        if plan.get(k):
            props[k] = deployopts.mode_value(k, plan[k], edition, minecraft)
    if props:
        write_properties(mc_dir, props)
    if plan.get("whitelist_players"):
        from core import whitelist
        for player in plan["whitelist_players"]:
            try:
                whitelist.add(svc_name, player)
            except whitelist.WhitelistError as e:
                write_log(f"[WARNING] {svc_name}: {e}")
    if plan.get("ops") and edition == "java":
        offline = str(props.get("online-mode", "true")).lower() == "false"
        from core import whitelist
        ops = [e for e in _load_json_list(os.path.join(mc_dir, "ops.json")) if isinstance(e, dict)]
        for player in plan["ops"]:
            if any(str(e.get("name", "")).lower() == player.lower() for e in ops):
                continue
            pid = whitelist.offline_uuid(player) if offline else get_java_uuid(player)
            if not pid:
                write_log(f"[WARNING] {svc_name}: couldn't find the UUID for {player}; not made an operator.")
                continue
            ops.append({"uuid": pid, "name": player, "level": 4, "bypassesPlayerLimit": False})
        with open(os.path.join(mc_dir, "ops.json"), "w") as f:
            json.dump(ops, f, indent=2)

def _flags_of(server):
    from core import deployopts
    return deployopts.get_flags(server)

def set_server_flags(server_name, flags, actor="admin"):
    """Startup flags of an existing Java server (Maintenance): rewrites its unit with the same
    Java and heap, and restarts it if it's running."""
    from core import deployopts
    _check_server(server_name)
    if not _is_java(server_name):
        raise ServerError(f"{server_name} isn't a Java server.")
    try:
        flags = deployopts.clean_flags(flags)
    except deployopts.OptionError as e:
        raise ServerError(str(e))
    exec_start = _unit_exec_start(server_name)
    _write_unit(server_name, get_server_dir(server_name), "java", java=unit_java(exec_start) or paths.DEFAULT_JAVA,
                **unit_heap(exec_start), jvm=flags)
    write_log(f"[JAVA] {actor} set {server_name}'s startup flags: {deployopts.describe_flags(flags) or 'none'}.")
    active = services.state(server_name) == "active"
    if active:
        services.restart(server_name)
    return {"restarted": active, "flags": deployopts.get_flags(server_name)}

def deploy_server(edition, template_id=None, loader="vanilla", version=None, advanced=None,
                  is_admin=True, actor=None, prepare=None):
    """A new server. Java: loader is one of javaservers.LOADERS and version a Minecraft release
    (default: the newest the loader offers). advanced: New server's Advanced options (1.17.2),
    checked by core/deployopts.py before anything is downloaded. prepare(mc_dir): called once
    the server software is in place and before the first start, to add files (a modpack's
    mods and config, 1.20); if it fails the folder is removed. Returns the server's name."""
    from core import deployopts
    edition = edition.lower()
    if edition not in ("java", "bedrock"):
        raise TemplateError(f"Unknown edition '{edition}'.")
    loader = (loader or "vanilla").lower()
    if edition == "java" and loader not in javaservers.LOADERS:
        raise ServerError(f"Unknown server type '{loader}'.")
    if edition == "bedrock" and (loader != "vanilla" or version):
        raise ServerError("Bedrock servers are always the newest official release.")
    try:
        plan = deployopts.validate(advanced, edition, loader, is_admin=is_admin)
    except deployopts.OptionError as e:
        raise ServerError(str(e))
    if plan.get("build") and loader in ("paper", "purpur", "forge", "neoforge") and not version:
        raise ServerError(f"Choose the Minecraft version a {javaservers.LOADERS[loader]} build belongs to.")
    if version and not plan.get("snapshot") and not javaservers.is_release(version):
        raise ServerError("Choose a Minecraft release such as 1.21.4, or allow snapshots.")
    if plan.get("heap"):
        _heap_fits(plan["heap"])
    if isinstance(plan.get("memory_limit"), str) and plan["memory_limit"] != "auto":
        from core import perf
        lim = deployopts.heap_bytes(plan["memory_limit"])
        xmx = deployopts.heap_bytes(plan.get("heap") or default_heap()["xmx"]) if edition == "java" else 0
        if xmx and lim < xmx + perf.LIMIT_HEADROOM:
            raise ServerError(f"The hard memory limit must be well above the heap: at least "
                              f"{perf.heap_text(xmx + perf.LIMIT_HEADROOM)}, or Automatic.")
    if template_id:
        get_checked_template(template_id, edition)   # fail before downloading anything
    if edition == "bedrock":
        require_bedrock_machine()
        require_tools("screen")

    if plan.get("name") or plan.get("port"):
        svc_name, port = _reserve_chosen(edition, plan.get("name"), plan.get("port"))
    else:
        svc_name, port = _reserve_name(edition)       # auto-allocated name and port
    try:
        mc_dir = os.path.join(SERVERS_ROOT, svc_name)

        # Java isn't part of the package: a missing one raises JavaMissing, and the panel offers to install it
        ensure_minecraft_user(mc_dir)
        os.makedirs(mc_dir, exist_ok=True)

        # Download & Extract Binary
        java = None
        info = None
        if edition == "java":
            try:
                info, java = _install_java(mc_dir, loader, version, build=plan.get("build"),
                                           snapshot=plan.get("snapshot", False), java_major=plan.get("java"))
            except (DependencyError, ServerError):
                shutil.rmtree(mc_dir, ignore_errors=True)    # nothing useful in it yet
                raise
            with open(f"{mc_dir}/eula.txt", "w") as f:
                f.write("eula=true\n")
            with open(f"{mc_dir}/server.properties", "w") as f:
                f.write(f"server-port={port}\nquery.port={port}\nmotd=A Managed Java Minecraft Server\n")
        else:
            try:
                _fetch_bedrock(mc_dir)
            except ServerError:
                shutil.rmtree(mc_dir, ignore_errors=True)    # nothing useful in it yet
                raise
            # The zip ships a full server.properties with every setting; BDS never writes missing
            # keys back, so only the ports change and the rest stays as Mojang shipped it. It also
            # ships server-udp-ports=19132: each server gets its own (UDP on the same number as
            # its TCP server-port), or a second Bedrock server would clash with the first.
            write_properties(mc_dir, {"server-port": port, "server-portv6": port + 1,
                                      "server-udp-ports": port, "query.port": port})
            write_screenrc(mc_dir)
        if prepare:
            system.give_to_game_account(mc_dir)
            try:
                prepare(mc_dir)
            except Exception:
                shutil.rmtree(mc_dir, ignore_errors=True)
                raise

        system.give_to_game_account(mc_dir)
        heap = {}
        if plan.get("heap"):
            # Java starts with half the heap (at least 512 MB) and grows to it; Aikar's flags
            # want both the same, which _write_unit does for them.
            half = max(512, deployopts.heap_bytes(plan["heap"]) // 1024 ** 2 // 2)
            heap = {"xms": f"{half}M" if half % 1024 else f"{half // 1024}G", "xmx": plan["heap"]}
        _write_unit(svc_name, mc_dir, edition, **heap, **({"java": java} if java else {}),
                    jvm=plan.get("flags") or {})
        if template_id:
            # After daemon-reload, so systemd already knows the new unit's working directory
            apply_template_to_server(svc_name, template_id, edition=edition)
        if plan:
            # After the template: what was chosen here wins over the template's values
            _apply_advanced_settings(svc_name, mc_dir, edition, plan, (info or {}).get("minecraft"))
            system.give_to_game_account(mc_dir)
            write_log(f"[DEPLOY] {actor + ' created ' if actor else 'Created '}{svc_name} with advanced options: "
                      f"{deployopts.summary(plan) or 'name only'}.")
        if plan.get("memory_limit") or plan.get("cpu"):
            # 1.17's hard limits, as Maintenance > Memory and limits sets them (a systemd drop-in)
            from core import perf
            try:
                perf.set_resources(svc_name, memory=plan.get("memory_limit"), cpu=plan.get("cpu"),
                                   actor=actor or "New server")
            except perf.PerfError as e:
                write_log(f"[WARNING] {svc_name}: hard limits not set: {e}")
        # The ports it needs, from what server.properties says now (a template may set the transport)
        open_server_firewall(svc_name, edition, mc_dir)
        services.enable(svc_name)
        services.start(svc_name)
        return svc_name
    finally:
        _release_name(svc_name)

def read_permissions(server_name):
    d = get_server_dir(server_name)
    exec_start = services.exec_start(server_name)
    is_java = "java" in exec_start
    
    target_file = "ops.json" if is_java else "permissions.json"
    perm_file = os.path.join(d, target_file)
    
    if os.path.exists(perm_file):
        with open(perm_file, "r") as f:
            return f.read()
    return "[\n  \n]"

def write_permissions(server_name, content):
    d = get_server_dir(server_name)
    exec_start = services.exec_start(server_name)
    is_java = "java" in exec_start
    
    target_file = "ops.json" if is_java else "permissions.json"
    perm_file = os.path.join(d, target_file)
    
    with open(perm_file, "w") as f:
        f.write(content)
        
def fallback_allowlist_xuid(server_name, gamertag):
    """Last resort: Uses the native Bedrock engine to resolve the XUID."""
    d = get_server_dir(server_name)
    allowlist_path = os.path.join(d, "allowlist.json")
    
    # 1. Stop the background service to free the port
    services.stop(server_name, elevate=True)
    
    # 2. Backup the current allowlist so we don't permanently alter it
    existing_allowlist = "[]"
    if os.path.exists(allowlist_path):
        with open(allowlist_path, "r") as f:
            existing_allowlist = f.read()
            
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = d
    
    # 3. Launch the server locally so we can pipe commands to its STDIN
    proc = subprocess.Popen([f"{d}/bedrock_server"], cwd=d, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    
    try:
        # Request the XUID from Xbox Live, then command the server to shut down
        proc.communicate(input=f'allowlist add "{gamertag}"\nstop\n', timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        
    # 4. Extract the resolved XUID from the newly modified file
    xuid = None
    if os.path.exists(allowlist_path):
        with open(allowlist_path, "r") as f:
            try:
                data = json.load(f)
                for entry in data:
                    if entry.get("name", "").lower() == gamertag.lower() and "xuid" in entry:
                        xuid = entry["xuid"]
                        break
            except Exception:
                pass
                
    # 5. Restore original allowlist & restart daemon
    with open(allowlist_path, "w") as f:
        f.write(existing_allowlist)
        
    services.start(server_name, elevate=True)
    return xuid

def get_xuid(server_name, gamertag):
    """Attempts to resolve an Xbox Live Gamertag to an XUID using 3 cascading methods."""
    # Method 1: Primary API (PlayerDB)
    try:
        url = f"https://playerdb.co/api/player/xbox/{urllib.parse.quote(gamertag)}"
        req = urllib.request.Request(url, headers={'User-Agent': 'RedstonePanel'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            if data.get("code") == "player.found":
                write_log(f"[API] XUID for {gamertag} resolved via PlayerDB.")
                return data["data"]["player"]["id"]
    except Exception:
        pass

    # Method 2: Backup API (GeyserMC)
    try:
        url = f"https://api.geysermc.org/v2/xbox/xuid/{urllib.parse.quote(gamertag)}"
        req = urllib.request.Request(url, headers={'User-Agent': 'RedstonePanel'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            if "xuid" in data:
                write_log(f"[API] XUID for {gamertag} resolved via GeyserMC.")
                return str(data["xuid"])
    except Exception:
        pass
        
    # Method 3: Native Fallback (Requires 10s Server Restart)
    write_log(f"[SYSTEM] APIs failed. Falling back to native allowlist resolution for {gamertag}...")
    return fallback_allowlist_xuid(server_name, gamertag)

def set_player_role(server_name, player_name, role, player_id=None):
    """Java: operator = entry in ops.json, member = no entry.
    Bedrock: operator / visitor = entry in permissions.json, member = no entry (server default).
    player_id: uuid/xuid if the panel already knows it (from the file or the logs)."""
    d = get_server_dir(server_name)
    is_java = _is_java(server_name)
    role = str(role).lower()
    if role not in (JAVA_ROLES if is_java else BEDROCK_ROLES):
        raise PlayerError(f"'{role}' is not a valid role for this edition.")

    file_path = os.path.join(d, "ops.json" if is_java else "permissions.json")
    id_key = "uuid" if is_java else "xuid"
    name_l = str(player_name).lower()

    # 1. Find the player's ID without the network if we can: request, then the file, then the logs
    pid = player_id or None
    if not pid:
        for e in _load_json_list(file_path):
            if isinstance(e, dict) and str(e.get("name", "")).lower() == name_l and e.get(id_key):
                pid = str(e[id_key])
                break
    if not pid:
        pid = (_players_from_logs(server_name, d, is_java).get(name_l) or (None, None))[1]

    needs_entry = role != "member"
    if needs_entry and not pid:
        # 2. Slow lookups only when nothing local knows the ID
        pid = get_java_uuid(player_name) if is_java else get_xuid(server_name, player_name)
        if not pid:
            write_log(f"[ERROR] Could not resolve {'UUID' if is_java else 'XUID'} for {player_name}. Role not changed.")
            raise PlayerError(f"Could not find the {'UUID' if is_java else 'XUID'} for {player_name}; role not changed.")

    # 3. Read the file right before writing; drop this player's entries (by name or ID)
    entries = [e for e in _load_json_list(file_path)
               if not (isinstance(e, dict) and (str(e.get("name", "")).lower() == name_l
                                                or (pid and str(e.get(id_key, "")) == str(pid))))]

    # 4. Add the new entry
    if needs_entry:
        if is_java:
            entries.append({"uuid": pid, "name": player_name, "level": 4, "bypassesPlayerLimit": False})
        else:
            entries.append({"permission": role, "name": player_name, "xuid": pid})

    with open(file_path, "w") as f:
        json.dump(entries, f, indent=2)
    write_log(f"[SYSTEM] {player_name} is now {role} on {server_name}.")


def get_java_uuid(username):
    """Resolves a Java Edition username to a properly formatted Mojang UUID."""
    try:
        url = f"https://api.mojang.com/users/profiles/minecraft/{urllib.parse.quote(username)}"
        req = urllib.request.Request(url, headers={'User-Agent': 'RedstonePanel'})
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status == 200:
                data = json.loads(response.read().decode())
                raw_uuid = data["id"]
                # Format with standard UUID dashes (8-4-4-4-12)
                return f"{raw_uuid[:8]}-{raw_uuid[8:12]}-{raw_uuid[12:16]}-{raw_uuid[16:20]}-{raw_uuid[20:]}"
    except Exception as e:
        print(f"Failed to fetch Java UUID: {e}")
    return None
    
# ---------------------------------------------------------------------------
# LIVE CHAT
# ---------------------------------------------------------------------------
ANSI_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
CHAT_DIR = DATA_DIR

def _is_java(server_name):
    exec_start = services.exec_start(server_name)
    return "java" in exec_start.lower()

def _sent_file(server_name):
    return os.path.join(CHAT_DIR, f"chat_sent_{server_name}.jsonl")

def _record_sent(server_name, text):
    """Panel-originated broadcasts never appear in the server log, so remember them ourselves."""
    try:
        with open(_sent_file(server_name), "a") as f:
            f.write(json.dumps({"ts": time.time(), "text": f"[Server] {text}"}) + "\n")
    except Exception as e:
        write_log(f"[ERROR] Could not record sent chat: {e}")

def _load_sent(server_name, limit=200):
    path = _sent_file(server_name)
    out = []
    if os.path.exists(path):
        with open(path, "r") as f:
            for line in f.readlines()[-limit:]:
                try:
                    j = json.loads(line)
                    out.append((float(j["ts"]), j["text"]))
                except Exception:
                    pass
    return out

# Java (journalctl -o short-unix):  1731500000.123 host java[123]: [10:00:00] [Server thread/INFO]: <Steve> hi
JOURNAL_RE = re.compile(r'^(\d+(?:\.\d+)?)\s+\S+\s+[^\s:\[]+(?:\[\d+\])?:\s(.*)$')
JAVA_PREFIX_RE = re.compile(r'^\[[^\]]*\]\s+\[[^\]]*\]:\s(.*)$')
# Bedrock (screen log):  [2026-11-13 10:00:00:123 INFO] Player connected: Steve, xuid: 123
BEDROCK_RE = re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}):\d+\s+\w+\]\s*(.*)$')

CHAT_RE = re.compile(r'^(?:\[Not Secure\]\s*)?<([^>]+)>\s(.*)$')
JOINLEAVE_RE = re.compile(r'^(\S+) (joined|left) the game$')
SAY_RE = re.compile(r'^\[(?:Server|Rcon[^\]]*)\]\s*(.+)$', re.IGNORECASE)
BE_CONN_RE = re.compile(r'^Player (connected|disconnected):\s*([^,]+),')
BE_PACK_RE = re.compile(r'\[Scripting\]\s*RSCHAT\|([^|]*)\|(.*)$')

def _classify(msg):
    """Turns one console message into a chat-window line, or None if it is not chat-related."""
    m = CHAT_RE.match(msg)
    if m:
        return f"<{m.group(1)}> {m.group(2)}"
    m = BE_PACK_RE.search(msg)
    if m:
        return f"<{m.group(1)}> {m.group(2)}"
    m = JOINLEAVE_RE.match(msg)
    if m:
        return f"* {m.group(1)} {m.group(2)} the game"
    m = BE_CONN_RE.match(msg)
    if m:
        return f"* {m.group(2).strip()} {'joined' if m.group(1) == 'connected' else 'left'} the game"
    m = SAY_RE.match(msg)
    if m:
        text = m.group(1).strip()
        if text and not any(p in text.lower() for p in ("logged in", "shutting down", "rcon client")):
            return f"[Server] {text}"
    return None

JAVA_FILE_RE = re.compile(r'^\[(\d{2}:\d{2}:\d{2})\]\s+\[[^\]]*\]:\s(.*)$')

def _read_tail(path, lines=3000):
    """Reads the tail of a file the minecraft user owns. Returns (text, error)."""
    return system.tail(path, lines)

def _hms_to_epoch(hms):
    now = time.time()
    t = time.strptime(time.strftime("%Y-%m-%d ") + hms, "%Y-%m-%d %H:%M:%S")
    ts = time.mktime(t)
    return ts - 86400 if ts > now + 300 else ts   # a line "from the future" belongs to yesterday

def _java_file_entries(raw, log_mtime):
    """latest.log lines only carry HH:MM:SS. Walk backwards from the file's last-modified
    date and step back a day whenever the clock jumps forward, so multi-day logs sort correctly."""
    import datetime as _dt
    day = _dt.date.fromtimestamp(log_mtime)
    prev = None
    out = []
    for line in reversed(ANSI_RE.sub('', raw).replace('\r', '').splitlines()):
        m = JAVA_FILE_RE.match(line.strip())
        if not m:
            continue
        h, mi, se = (int(x) for x in m.group(1).split(":"))
        secs = h * 3600 + mi * 60 + se
        if prev is not None and secs > prev + 60:
            day -= _dt.timedelta(days=1)
        prev = secs
        text = _classify(m.group(2).strip())
        if text:
            out.append((_dt.datetime.combine(day, _dt.time(h, mi, se)).timestamp(), text))
    out.reverse()
    return out

def _clear_file(server_name):
    return os.path.join(CHAT_DIR, f"chat_cleared_{server_name}")

def clear_chat(server_name):
    with open(_clear_file(server_name), "w") as f:
        f.write(str(time.time()))

def _cleared_at(server_name):
    try:
        with open(_clear_file(server_name)) as f:
            return float(f.read().strip())
    except Exception:
        return 0.0

def _collect_chat(server_name):
    """Returns (entries, debug). entries = [(epoch, text)]"""
    d = get_server_dir(server_name)
    entries, dbg = [], {"server_dir": d}

    if _is_java(server_name):
        dbg["edition"] = "java"
        latest = os.path.join(d, "logs", "latest.log")
        raw, err = ("", "latest.log not found")
        if os.path.exists(latest):
            raw, err = _read_tail(latest)
        dbg["latest_log_error"] = err
        try:
            mtime = os.path.getmtime(latest)
        except OSError:
            mtime = time.time()
        entries.extend(_java_file_entries(raw, mtime))
        dbg["source"] = "logs/latest.log"

        if not entries:   # fall back to the journal
            dbg["source"] = "journalctl"
            r = services.log_result(services.unit(server_name), lines=3000, output="short-unix")
            dbg["journal_error"] = (r.stderr or "").strip()[:300]
            for raw_line in ANSI_RE.sub('', r.stdout).splitlines():
                jm = JOURNAL_RE.match(raw_line.strip())
                if not jm:
                    continue
                pm = JAVA_PREFIX_RE.match(jm.group(2))
                text = _classify(pm.group(1).strip() if pm else jm.group(2).strip())
                if text:
                    entries.append((float(jm.group(1)), text))
    else:
        dbg["edition"] = "bedrock"
        dbg["source"] = "server.log"
        log_file = os.path.join(d, "server.log")
        raw, err = ("", "server.log not found")
        if os.path.exists(log_file):
            raw, err = _read_tail(log_file)
        dbg["server_log_error"] = err
        last_ts = 0.0
        for line in ANSI_RE.sub('', raw).replace('\r', '\n').splitlines():
            bm = BEDROCK_RE.search(line)
            if not bm:
                continue
            try:
                last_ts = time.mktime(time.strptime(bm.group(1), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                pass
            text = _classify(bm.group(2).strip())
            if text:
                entries.append((last_ts, text))

    dbg["parsed_from_log"] = len(entries)
    entries.extend(_load_sent(server_name))
    cutoff = _cleared_at(server_name)
    entries = sorted([e for e in entries if e[0] > cutoff], key=lambda e: e[0])
    dbg["after_clear_filter"] = len(entries)
    return entries, dbg

def get_chat_history(server_name, limit=50):
    entries, _ = _collect_chat(server_name)
    return [t for _, t in entries][-limit:]

def get_chat_debug(server_name):
    entries, dbg = _collect_chat(server_name)
    dbg["last_entries"] = [t for _, t in entries][-5:]
    dbg["whoami"] = system.whoami()
    return dbg

class ChatError(RuntimeError):
    """A chat message or command couldn't be delivered; the message says why and what to do."""

def send_chat_message(server_name, message):
    """Java: RCON.  Bedrock: stuff the command into the server's screen session."""
    d = get_server_dir(server_name)
    props = read_properties(d)
    is_java = _is_java(server_name)
    is_command = message.startswith('/')

    if is_command:
        command = message[1:]
    else:
        # json.dumps escapes quotes/backslashes so a chat message can't break the JSON
        text = f"<Server> {message}"
        command = ('tellraw @a ' + json.dumps({"text": text})) if is_java \
            else ('tellraw @a ' + json.dumps({"rawtext": [{"text": text}]}))

    if is_java:
        rcon_enabled = str(props.get("enable-rcon", "false")).lower() == "true"
        rcon_pass = props.get("rcon.password", "")
        rcon_port = str(props.get("rcon.port", "") or "")

        # A port another managed server also claims means this server's RCON either failed to
        # bind or, worse, mcrcon would talk to the other server. Move this one to a free port.
        port_clash = rcon_port.isdigit() and int(rcon_port) in _used_ports(exclude_dir=d)

        if not rcon_enabled or not rcon_pass or not rcon_port or port_clash:
            props["enable-rcon"] = "true"
            if not rcon_pass:
                props["rcon.password"] = secrets.token_urlsafe(16)
            if not rcon_port or port_clash:
                props["rcon.port"] = _free_rcon_port(d)
            write_properties(d, props)
            reason = f"RCON port {rcon_port} was shared with another server" if port_clash else "RCON was not configured"
            write_log(f"[SYSTEM] {reason}; set rcon.port={props['rcon.port']} for {server_name}. Restarting...")
            services.restart(server_name, elevate=True)
            return "restarting"

        try:
            output = rcon.command("127.0.0.1", int(rcon_port), rcon_pass, command)
        except rcon.RconError as e:
            write_log(f"[ERROR] RCON to {server_name} failed: {e}")
            if isinstance(e, rcon.RconAuthError):
                raise ChatError("The server rejected the RCON password. If rcon.password was changed "
                                "while the server was running, restart the server so it uses the new one.")
            if isinstance(e, rcon.RconRefused):
                raise ChatError(f"RCON isn't answering on port {rcon_port}. If the server is still "
                                "starting, try again in a moment; otherwise restart it so it picks up "
                                "its RCON settings from server.properties.")
            raise ChatError(f"Failed to connect to RCON: {e}")
        if not is_command:
            _record_sent(server_name, message)
        return ANSI_RE.sub('', output).strip()

    # ---- Bedrock ----
    # The text and a real carriage return go to the console as one item: nothing parses quotes or escapes.
    result = console.send(server_name, command)
    if result is None:
        write_log("[ERROR] screen executable not found. Please install it.")
        return "error"
    if result.returncode != 0:
        write_log(f"[ERROR] screen session '{server_name}' not reachable: {(result.stdout + result.stderr).strip()}")
        return "error"
    if not is_command:
        _record_sent(server_name, message)
    return ""   # bedrock gives no command output to show

# ---------------------------------------------------------------------------
# Bedrock: relay in-game chat into the console log (vanilla BDS never prints it)
# ---------------------------------------------------------------------------
CHAT_PACK_UUID = "1ca9a5dd-a1ca-4a6a-97e6-4c55e6c35811"
CHAT_PACK_MODULE_UUID = "a2727b01-939a-487a-a366-eebf6e86a086"
# If the pack fails to load, the server console names the problem. Check this version string
# against the Script API docs for your BDS release ("2.0.0-beta" if chatSend is still beta there).
CHAT_PACK_SERVER_MODULE = "2.0.0"

CHAT_PACK_MAIN_JS = '''import { world } from "@minecraft/server";

world.afterEvents.chatSend.subscribe((ev) => {
  const msg = String(ev.message).replace(/[\\r\\n|]/g, " ");
  console.warn(`RSCHAT|${ev.sender.name}|${msg}`);
});
'''

def write_screenrc(mc_dir):
    with open(os.path.join(mc_dir, "screenrc"), "w") as f:
        f.write("logfile flush 1\nlogfile " + os.path.join(mc_dir, "server.log") + "\n")

def install_bedrock_chat_pack(server_name):
    """Installs the chat relay behavior pack and enables it for the active world. Restart afterwards."""
    d = get_server_dir(server_name)
    pack_dir = os.path.join(d, "behavior_packs", "redstonepanel_chat")
    os.makedirs(os.path.join(pack_dir, "scripts"), exist_ok=True)

    manifest = {
        "format_version": 2,
        "header": {"name": "RedstonePanel Chat Relay", "description": "Prints player chat to the server console",
                   "uuid": CHAT_PACK_UUID, "version": [1, 0, 0], "min_engine_version": [1, 21, 80]},
        "modules": [{"type": "script", "language": "javascript", "uuid": CHAT_PACK_MODULE_UUID,
                     "version": [1, 0, 0], "entry": "scripts/main.js"}],
        "dependencies": [{"module_name": "@minecraft/server", "version": CHAT_PACK_SERVER_MODULE}],
    }
    with open(os.path.join(pack_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    with open(os.path.join(pack_dir, "scripts", "main.js"), "w") as f:
        f.write(CHAT_PACK_MAIN_JS)

    world_dir = os.path.join(d, "worlds", get_active_world_name(server_name))
    os.makedirs(world_dir, exist_ok=True)
    wbp = os.path.join(world_dir, "world_behavior_packs.json")
    packs = []
    if os.path.exists(wbp):
        try:
            with open(wbp, "r") as f:
                packs = json.load(f)
        except Exception:
            packs = []
    if not any(p.get("pack_id") == CHAT_PACK_UUID for p in packs):
        packs.append({"pack_id": CHAT_PACK_UUID, "version": [1, 0, 0]})
    with open(wbp, "w") as f:
        json.dump(packs, f, indent=2)

    system.give_to_game_account(d)
    write_log(f"[SYSTEM] Chat relay pack installed for {server_name}. Restart the server to activate it.")


def get_active_world_name(server_name):
    """Reads the current active world folder name from server.properties."""
    props = read_properties(get_server_dir(server_name))
    return props.get("level-name", "world")

def export_world(server_name):
    """Zips the active world directory for download."""
    d = get_server_dir(server_name)
    world_name = get_active_world_name(server_name)
    
    # Bedrock stores its active world inside a 'worlds' subdirectory
    exec_start = services.exec_start(server_name)
    if "bedrock" in exec_start:
        world_path = os.path.join(d, "worlds", world_name)
    else:
        world_path = os.path.join(d, world_name)
        
    if not os.path.exists(world_path):
        raise FileNotFoundError(f"World folder '{world_name}' not found.")
        
    export_base = os.path.join(tempfile.mkdtemp(prefix="redstonepanel-export-"), f"{server_name}_{world_name}")
    shutil.make_archive(export_base, 'zip', world_path)
    return f"{export_base}.zip", world_name

class WorldError(ValueError):
    """A world rename that can't be done safely."""

def rename_world(server_name, new_name):
    """Renames the world folder and updates server.properties to match."""
    d = get_server_dir(server_name)
    old_name = get_active_world_name(server_name)
    
    exec_start = services.exec_start(server_name)
    is_bedrock = "bedrock" in exec_start
    
    base_dir = os.path.join(d, "worlds") if is_bedrock else d
    old_path = os.path.join(base_dir, old_name)
    new_path = os.path.join(base_dir, new_name)

    # 0. Refuse anything that would leave level-name pointing at a folder that isn't this world
    #    (the server would then quietly generate a brand-new empty world).
    if not new_name or new_name in (".", "..") or "/" in new_name or "\\" in new_name:
        raise WorldError("World name can't be empty or contain / or \\.")
    if new_name == old_name:
        raise WorldError(f"The world is already called '{old_name}'.")
    if not os.path.isdir(old_path):
        raise WorldError(f"Current world folder '{old_name}' was not found; nothing renamed.")
    if os.path.exists(new_path):
        raise WorldError(f"A folder named '{new_name}' already exists; choose another name.")

    # 1. Stop the server safely before modifying world files
    services.stop(server_name, elevate=True)

    # 2. Rename the directory
    os.rename(old_path, new_path)
        
    # 3. Update server.properties
    props = read_properties(d)
    props["level-name"] = new_name
    write_properties(d, props)
    
    # 4. Restart the server with the new configuration
    services.start(server_name, elevate=True)