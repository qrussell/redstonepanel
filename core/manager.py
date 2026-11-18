import os
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
from core import properties, rcon, proxies, bedrock_net

TEMPLATE_FILE = os.path.join(DATA_DIR, "templates.json")

class DependencyError(RuntimeError):
    """A program the panel needs is not installed (message says which package provides it)."""

# program -> Ubuntu package (the .deb depends on/recommends these)
TOOL_PACKAGES = {"java": "openjdk-25-jre-headless", "screen": "screen", "unzip": "unzip",
                 "curl": "curl", "wget": "wget", "jq": "jq"}

def require_tools(*tools):
    missing = [t for t in tools if not shutil.which(t)]
    if missing:
        pkgs = " ".join(TOOL_PACKAGES.get(t, t) for t in missing)
        raise DependencyError(f"Missing {', '.join(missing)}. Install with: sudo apt install {pkgs}")

def ensure_minecraft_user(home):
    """The .deb creates the 'minecraft' system user; older/manual installs create it on first deploy."""
    import pwd
    try:
        pwd.getpwnam("minecraft")
    except KeyError:
        run_and_log(["useradd", "-r", "-m", "-U", "-d", home, "-s", "/bin/bash", "minecraft"])

def open_firewall(port, proto):
    """Opens the game port in ufw when ufw is installed (it's optional)."""
    if shutil.which("ufw"):
        run_and_log(["ufw", "allow", f"{port}/{proto}"])

def load_templates():
    if not os.path.exists(TEMPLATE_FILE): return []
    with open(TEMPLATE_FILE, 'r') as f: return json.load(f)

def save_templates(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TEMPLATE_FILE, 'w') as f: json.dump(data, f, indent=2)

def save_server_as_template(server_name, template_name):
    templates = load_templates()
    props = read_properties(get_server_dir(server_name))
    perms = read_permissions(server_name)
    
    exec_start = subprocess.run(["systemctl", "show", "-p", "ExecStart", "--value", f"{server_name}.service"], capture_output=True, text=True).stdout.strip()
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
    rcon_port = current_props.get("rcon.port")
    query_port = current_props.get("query.port")  # Extract existing query port
    
    merged = {**current_props, **temp["properties"]}
    
    # Re-apply the unique ports so the template does not cause conflicts
    if port: merged["server-port"] = port
    if portv6: merged["server-portv6"] = portv6
    if rcon_port: merged["rcon.port"] = rcon_port
    if query_port: merged["query.port"] = query_port  # Protect query port
        
    write_properties(get_server_dir(server_name), merged)
    write_permissions(server_name, temp["permissions"])
    return True

def update_template(template_id, properties, permissions):
    templates = load_templates()
    for t in templates:
        if t["id"] == template_id:
            t["properties"] = properties
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
    res = subprocess.run(["systemctl", "show", "-p", "WorkingDirectory", "--value", f"{svc_name}.service"], capture_output=True, text=True)
    d = res.stdout.strip()
    if not d:
        res = subprocess.run(f"systemctl show -p WorkingDirectory {svc_name}.service | cut -d'=' -f2", shell=True, capture_output=True, text=True)
        d = res.stdout.strip()
    return d if d else f"/opt/{svc_name}"

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

SERVERS_ROOT = "/opt"
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
    # grep (POSIX ERE, no Python-only syntax) finds the lines fast in a big log; Python extracts the number
    r = subprocess.run(["grep", "-a", "-o", "-E", r"Version:? [0-9]+(\.[0-9]+)+", log], capture_output=True, text=True)
    found = BEDROCK_VERSION_RE.findall(r.stdout)
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
    r = subprocess.run(["grep", "-a", "-E", bedrock_net.GREP_PATTERN, log], capture_output=True, text=True)
    status = bedrock_net.last_start_status(r.stdout.splitlines())
    _start_cache[log] = (key, status)
    return status

_journal_cache = {}          # service -> (checked_at, result)

def _bedrock_journal_version(svc):
    """Older installs run BDS in screen without a server.log; its startup lines may be in the journal."""
    hit = _journal_cache.get(svc)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    r = subprocess.run(["journalctl", "-u", f"{svc}.service", "--no-pager", "-o", "cat"], capture_output=True, text=True)
    found = BEDROCK_VERSION_RE.findall(r.stdout)
    result = {"version": found[-1], "java_required": None} if found else None
    _journal_cache[svc] = (time.time(), result)
    return result

def server_version(d, is_java, svc=None):
    empty = {"version": None, "java_required": None}
    if is_java:
        return _cached(os.path.join(d, "server.jar"), _java_jar_info) or empty
    return (_cached(os.path.join(d, "server.log"), _bedrock_log_version)
            or (svc and _bedrock_journal_version(svc)) or empty)

def java_major():
    """Major version of the default 'java' (e.g. 25), or None if Java isn't installed."""
    now = time.time()
    if now - _java_cache["at"] > 60:
        major = None
        if shutil.which("java"):
            out = subprocess.run(["java", "-version"], capture_output=True, text=True).stderr
            m = re.search(r'version "(\d+)(?:\.(\d+))?', out)
            if m:
                major = int(m.group(2)) if m.group(1) == "1" else int(m.group(1))   # "1.8" -> 8
        _java_cache.update(at=now, major=major)
    return _java_cache["major"]

def warn_if_java_too_old(jar):
    """After downloading a Java server jar: say so clearly if the installed Java can't run it."""
    info = _java_jar_info(jar) or {}
    need, have = info.get("java_required"), java_major()
    if need and have and have < need:
        write_log(f"[WARNING] Minecraft {info.get('version')} needs Java {need}, but the default Java is {have}. "
                  f"Install it with: sudo apt install openjdk-{need}-jre-headless")

def list_servers():
    servers = []
    res = subprocess.run(["find", "/etc/systemd/system/", "-maxdepth", "1", "-name", "minecraft*.service", "-printf", "%f\n"], capture_output=True, text=True)
    services = [s.replace(".service", "") for s in res.stdout.splitlines() if s]
    phantoms = None              # looked up once, only if there's a Bedrock server

    for svc in services:
        st = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True).stdout.strip()
        d = get_server_dir(svc)
        props = read_properties(d)
        exec_start = subprocess.run(["systemctl", "show", "-p", "ExecStart", "--value", f"{svc}.service"], capture_output=True, text=True).stdout.strip()
        ver = server_version(d, "java" in exec_start, svc)
        is_bedrock = "java" not in exec_start and "bedrock" in exec_start
        proxy_list = []
        transport = (props.get("transport") or "").lower() or None if is_bedrock else None
        udp_ports = props.get("server-udp-ports", "")
        start = bedrock_start_status(d) if is_bedrock else None
        if is_bedrock:
            if phantoms is None:
                phantoms = proxies.find_phantoms()
            proxy_list = proxies.proxies_for_server(phantoms, props.get("server-port", "19132"))

        servers.append({
            "name": svc,
            # Minecraft version of the installed server, and (Java) the Java it needs vs. what's installed
            "version": ver["version"],
            "java_required": ver["java_required"],
            "java_installed": java_major() if "java" in exec_start else None,
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
    parts = [subprocess.run(["journalctl", "-u", f"{server_name}.service", "-n", "5000", "--no-pager"],
                            capture_output=True, text=True).stdout]
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

BEDROCK_KEEP_FILES = ("server.properties", "permissions.json", "allowlist.json", "whitelist.json")

def update_server_binary(server_name):
    d = get_server_dir(server_name)
    exec_start = subprocess.run(["systemctl", "show", "-p", "ExecStart", "--value", f"{server_name}.service"], capture_output=True, text=True).stdout.strip()
    require_tools("curl", "wget", "jq", *([] if "java" in exec_start else ["unzip"]))

    run_and_log(["sudo", "systemctl", "stop", f"{server_name}.service"])
    if "java" in exec_start:
        _fetch_java_jar(d)
    else:
        _fetch_bedrock(d)
    run_and_log(["chown", "-R", "minecraft:minecraft", d])
    run_and_log(["sudo", "systemctl", "start", f"{server_name}.service"])

def _fetch_java_jar(d):
    """Downloads the latest release's server.jar into d."""
    fetch_cmd = """
    VERSION=$(curl -s https://piston-meta.mojang.com/mc/game/version_manifest_v2.json | jq -r '.latest.release')
    VERSION_URL=$(curl -s https://piston-meta.mojang.com/mc/game/version_manifest_v2.json | jq -r ".versions[] | select(.id == \\\"$VERSION\\\") | .url")
    SERVER_URL=$(curl -s "$VERSION_URL" | jq -r '.downloads.server.url')
    wget "$SERVER_URL" -O """ + d + "/server.jar"
    run_and_log(fetch_cmd, shell=True)
    warn_if_java_too_old(f"{d}/server.jar")

def _fetch_bedrock(d):
    """Downloads the latest Bedrock Dedicated Server and unpacks it into d."""
    fetch_cmd = """
    SERVER_URL=$(curl -s https://net-secondary.web.minecraft-services.net/api/v1.0/download/links | jq -r '.result.links[] | select(.downloadType=="serverBedrockLinux") | .downloadUrl' 2>/dev/null)
    if [ -z "$SERVER_URL" ] || [ "$SERVER_URL" == "null" ]; then
        SERVER_URL=$(curl -s -H "User-Agent: Mozilla/4.0" https://www.minecraft.net/en-us/download/server/bedrock | grep -o 'https://[^"]*/bin-linux/bedrock-server-[^"]*\\.zip' | head -n 1)
    fi
    wget "$SERVER_URL" -O """ + d + "/bedrock-server.zip"
    
    run_and_log(fetch_cmd, shell=True)

    # The official zip ships default copies of these files; unzip -o would overwrite
    # the server's ports, operators and allowlist. Keep ours and put them back.
    saved = {}
    for name in BEDROCK_KEEP_FILES:
        p = os.path.join(d, name)
        if os.path.exists(p):
            with open(p, "rb") as f:
                saved[name] = f.read()
    try:
        run_and_log(["unzip", "-o", "-q", f"{d}/bedrock-server.zip", "-d", d])
    finally:
        for name, data in saved.items():
            with open(os.path.join(d, name), "wb") as f:
                f.write(data)
        if saved:
            write_log(f"[SYSTEM] Kept existing {', '.join(saved)}.")
    if os.path.exists(f"{d}/bedrock-server.zip"):
        os.remove(f"{d}/bedrock-server.zip")
    run_and_log(["chmod", "+x", f"{d}/bedrock_server"])

def _write_unit(svc_name, mc_dir, edition, xms="1G", xmx="2G"):
    """Writes the game server's systemd unit and reloads systemd (doesn't enable or start it)."""
    if edition == "bedrock":
        exec_start = f"/usr/bin/screen -c {mc_dir}/screenrc -L -Logfile {mc_dir}/server.log -DmS {svc_name} ./bedrock_server"
        env_vars = "Environment=\"LD_LIBRARY_PATH=.\""
    else:
        exec_start = f"/usr/bin/java -Xms{xms} -Xmx{xmx} -jar server.jar nogui"
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
    with open(f"/etc/systemd/system/{svc_name}.service", "w") as f:
        f.write(svc_content)
    run_and_log(["systemctl", "daemon-reload"])

class ServerError(ValueError):
    """A request about a server that can't be done (message is safe to show)."""

POWER_ACTIONS = ("start", "stop", "restart")
SERVICE_NAME_RE = re.compile(r"^minecraft[A-Za-z0-9_.@-]*$")

def control_server(server_name, action):
    # Only start/stop/restart, and only the panel's own game server units: never another service
    if action not in POWER_ACTIONS:
        raise ServerError(f"Unknown action '{action}'.")
    if not SERVICE_NAME_RE.match(server_name or "") or \
            not os.path.isfile(f"/etc/systemd/system/{server_name}.service"):
        raise ServerError(f"No Minecraft server called '{server_name}' on this panel.")
    run_and_log(["sudo", "systemctl", action, f"{server_name}.service"])

def _unit_path(server_name):
    return f"/etc/systemd/system/{server_name}.service"

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

def remove_server(server_name, keep_world):
    """Stops the server and deletes its systemd unit, so it leaves the panel.
    keep_world=True keeps the whole folder (server binary included), so it can be restored
    later; keep_world=False deletes the folder too."""
    _check_server(server_name)
    d = get_server_dir(server_name)
    exec_start = subprocess.run(["systemctl", "show", "-p", "ExecStart", "--value", f"{server_name}.service"],
                                capture_output=True, text=True).stdout.strip()
    run_and_log(["sudo", "systemctl", "stop", f"{server_name}.service"])
    run_and_log(["sudo", "systemctl", "disable", f"{server_name}.service"])

    if os.path.exists(_unit_path(server_name)):
        os.remove(_unit_path(server_name))
    run_and_log(["sudo", "systemctl", "daemon-reload"])
    # Its backup timer goes too; the backups themselves stay in /var/backups/redstonepanel
    from core import backups
    backups.on_server_removed(server_name, purged=not keep_world)

    if keep_world:
        if os.path.isdir(d):
            info = {"edition": "java" if "java" in exec_start else "bedrock" if "bedrock" in exec_start else None,
                    "removed_at": int(time.time())}
            for key in ("xms", "xmx"):
                m = re.search(rf"-X{key[1:]}(\S+)", exec_start)
                if m and HEAP_RE.match(m.group(1)):
                    info[key] = m.group(1)
            try:
                with open(os.path.join(d, REMOVED_INFO), "w") as f:
                    json.dump(info, f)
            except OSError:
                pass          # only a hint for restoring; the folder alone is enough
        write_log(f"[REMOVE] Removed {server_name} from the panel. Its files stay in {d}; "
                  f"restore it from New Server > Removed servers.")
    elif os.path.exists(d) and d.startswith("/opt/"):
        run_and_log(["rm", "-rf", d])
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
    unit_dir = "/etc/systemd/system"
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
    if os.path.isfile(os.path.join(d, "server.jar")):
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
            "has_binary": bool(edition) and os.path.isfile(os.path.join(d, binary)),
            "removed_at": removed_at if isinstance(removed_at, int) else None,
            "can_restore": edition is not None,
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
        if str(props.get("query.port", "")).strip() in ("", old):
            changes["query.port"] = str(port)
    if changes:
        write_properties(d, changes)
    if edition == "java":
        rcon_port = str(props.get("rcon.port", "")).strip()
        if rcon_port.isdigit() and int(rcon_port) in used | {port}:
            write_properties(d, {"rcon.port": _free_rcon_port(d)})
            write_log(f"[RESTORE] RCON port {rcon_port} is used by another server; moved it.")
    return port, (old if str(port) != old else None)

def restore_server(name):
    """Brings a removed server back: downloads the server binary only if it's missing, keeps
    its port unless it's taken, writes a new systemd unit, then enables and starts it."""
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
        if edition == "java":
            require_tools("java", *([] if match["has_binary"] else ["curl", "wget", "jq"]))
        else:
            require_tools("screen", *([] if match["has_binary"] else ["curl", "wget", "jq", "unzip"]))
        downloaded = False
        if not match["has_binary"]:
            write_log(f"[RESTORE] {name} has no server binary; downloading the latest {edition.capitalize()} release.")
            if edition == "java":
                _fetch_java_jar(d)
            else:
                _fetch_bedrock(d)
            downloaded = True

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

        open_firewall(port, "tcp" if edition == "java" else "udp")
        ensure_minecraft_user(d)
        run_and_log(["chown", "-R", "minecraft:minecraft", d])
        _write_unit(name, d, edition, **heap)
        run_and_log(["systemctl", "enable", f"{name}.service"])
        run_and_log(["systemctl", "start", f"{name}.service"])
        try:
            os.remove(os.path.join(d, REMOVED_INFO))
        except OSError:
            pass
        from core import backups
        backups.on_server_restored(name)          # its backup schedule, if it had one
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
    run_and_log(["rm", "-rf", d])
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
    elif os.path.isfile(os.path.join(root, "server.jar")) or os.path.isfile(os.path.join(root, "server.properties")):
        edition = "java"
    else:
        raise archive.ArchiveError("This doesn't look like a Minecraft server backup: no bedrock_server, "
                                   "server.jar or server.properties at its top level.")
    if edition == "java" and not os.path.isfile(os.path.join(root, "server.jar")):
        write_log("[WARNING] The backup has no server.jar; use Update on the new server to download one.")

    proto = "tcp" if edition == "java" else "udp"
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
        props["server-port"] = str(port)
        if edition == "bedrock":
            props["server-portv6"] = str(port + 1)
        write_properties(mc_dir, props)

        if edition == "bedrock":
            run_and_log(["chmod", "+x", f"{mc_dir}/bedrock_server"])
            write_screenrc(mc_dir)

        open_firewall(port, proto)
        ensure_minecraft_user(mc_dir)
        run_and_log(["chown", "-R", "minecraft:minecraft", mc_dir])
        _write_unit(svc_name, mc_dir, edition)
        run_and_log(["systemctl", "enable", f"{svc_name}.service"])
        run_and_log(["systemctl", "start", f"{svc_name}.service"])
        return svc_name
    finally:
        _release_name(svc_name)

def deploy_server(edition, template_id=None):
    edition = edition.lower()
    if edition not in ("java", "bedrock"):
        raise TemplateError(f"Unknown edition '{edition}'.")
    if template_id:
        get_checked_template(template_id, edition)   # fail before downloading anything
    require_tools("curl", "wget", "jq", *(["java"] if edition == "java" else ["unzip", "screen"]))
    proto = "tcp" if edition == "java" else "udp"

    svc_name, port = _reserve_name(edition)       # auto-allocated name and port
    try:
        mc_dir = os.path.join(SERVERS_ROOT, svc_name)

        # Dependencies come from the redstonepanel package (Java is a Recommends); just make sure
        open_firewall(port, proto)
        ensure_minecraft_user(mc_dir)
        os.makedirs(mc_dir, exist_ok=True)

        # Download & Extract Binary
        if edition == "java":
            _fetch_java_jar(mc_dir)
            with open(f"{mc_dir}/eula.txt", "w") as f:
                f.write("eula=true\n")
            with open(f"{mc_dir}/server.properties", "w") as f:
                f.write(f"server-port={port}\nquery.port={port}\nmotd=A Managed Java Minecraft Server\n")
        else:
            _fetch_bedrock(mc_dir)
            with open(f"{mc_dir}/server.properties", "w") as f:
                f.write(f"server-port={port}\nserver-portv6={port+1}\nquery.port={port}\n")
            write_screenrc(mc_dir)

        run_and_log(["chown", "-R", "minecraft:minecraft", mc_dir])
        _write_unit(svc_name, mc_dir, edition)
        if template_id:
            # After daemon-reload, so systemd already knows the new unit's working directory
            apply_template_to_server(svc_name, template_id, edition=edition)
        run_and_log(["systemctl", "enable", f"{svc_name}.service"])
        run_and_log(["systemctl", "start", f"{svc_name}.service"])
        return svc_name
    finally:
        _release_name(svc_name)

def read_permissions(server_name):
    d = get_server_dir(server_name)
    exec_start = subprocess.run(["systemctl", "show", "-p", "ExecStart", "--value", f"{server_name}.service"], capture_output=True, text=True).stdout.strip()
    is_java = "java" in exec_start
    
    target_file = "ops.json" if is_java else "permissions.json"
    perm_file = os.path.join(d, target_file)
    
    if os.path.exists(perm_file):
        with open(perm_file, "r") as f:
            return f.read()
    return "[\n  \n]"

def write_permissions(server_name, content):
    d = get_server_dir(server_name)
    exec_start = subprocess.run(["systemctl", "show", "-p", "ExecStart", "--value", f"{server_name}.service"], capture_output=True, text=True).stdout.strip()
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
    run_and_log(["sudo", "systemctl", "stop", f"{server_name}.service"])
    
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
        
    run_and_log(["sudo", "systemctl", "start", f"{server_name}.service"])
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
    exec_start = subprocess.run(["systemctl", "show", "-p", "ExecStart", "--value", f"{server_name}.service"],
                                capture_output=True, text=True).stdout.strip()
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
    for cmd in (["sudo", "-n", "tail", "-n", str(lines), path], ["tail", "-n", str(lines), path]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout, ""
        err = (r.stderr or "").strip()
    return "", err

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
            r = subprocess.run(["journalctl", "-u", f"{server_name}.service", "-n", "3000", "-o", "short-unix", "--no-pager"],
                               capture_output=True, text=True)
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
    dbg["whoami"] = subprocess.run(["id"], capture_output=True, text=True).stdout.strip()
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
            run_and_log(["sudo", "systemctl", "restart", f"{server_name}.service"])
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
    screen_path = shutil.which("screen")
    if not screen_path:
        write_log("[ERROR] screen executable not found. Please install it.")
        return "error"

    # Pass the text and a real carriage return as ONE argv item: no screen-side quote/escape parsing.
    result = subprocess.run(["sudo", "-u", "minecraft", screen_path, "-S", server_name, "-p", "0",
                             "-X", "stuff", command + "\r"], capture_output=True, text=True)
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

    run_and_log(["chown", "-R", "minecraft:minecraft", d])
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
    exec_start = subprocess.run(["systemctl", "show", "-p", "ExecStart", "--value", f"{server_name}.service"], capture_output=True, text=True).stdout.strip()
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
    
    exec_start = subprocess.run(["systemctl", "show", "-p", "ExecStart", "--value", f"{server_name}.service"], capture_output=True, text=True).stdout.strip()
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
    run_and_log(["sudo", "systemctl", "stop", f"{server_name}.service"])

    # 2. Rename the directory
    os.rename(old_path, new_path)
        
    # 3. Update server.properties
    props = read_properties(d)
    props["level-name"] = new_name
    write_properties(d, props)
    
    # 4. Restart the server with the new configuration
    run_and_log(["sudo", "systemctl", "start", f"{server_name}.service"])