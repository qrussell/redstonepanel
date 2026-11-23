"""Backup copies off the machine: an SSH server or S3-compatible storage (1.16.11).

1.16.8 copies backups to another cluster member, which only helps people with a cluster. Most
panels are a single machine, and a backup on the same disk doesn't survive the disk. Here each
panel copies its own servers' backups to storage of its own choosing:

    SSH/rsync   host, port, user and folder. The panel has its own SSH key (shown in Panel
                Settings to add to the target's authorized_keys) and checks the host key on
                first use, as ssh does. Copies run `rsync -e ssh --partial`, so a cut-off copy
                resumes; the target needs rsync and sha256sum (or shasum).
    S3          Backblaze B2, Wasabi, Cloudflare R2, MinIO, AWS S3 and others: endpoint, region,
                bucket, prefix, access key and secret. Requests are signed with AWS Signature
                Version 4 using hmac and hashlib; archives over 100 MB go up in parts. Every
                request carries its body's SHA-256, which the storage checks.

Layout on a target:  <folder or prefix>/<panel id>/<server>/<backup file>  plus  <file>.sha256

<panel id> names this machine (its host name and four random hex digits, made once), so two
panels can share a bucket, and a fresh machine can find the old one's backups (Import → From
backup storage).

What gets copied: every manual backup, and the scheduled ones the target's retention keeps
(the newest N, where N is the target's own count or the server's Keep, and none older than the
target's age limit). Before-restore and before-update backups stay local. Scheduled copies
beyond the count or age are deleted from the target; the newest copy of a server is never
deleted by age, so a server that has been stopped for a month still has one.

When: right after a backup (the backup command starts a separate transient systemd unit, so a
slow upload never holds the server's save or the backup timer), after Back up now, and every
few minutes from the panel for anything missing or failed. A failed copy is a Needs attention
item and one `backup_copy_failed` webhook event, as in 1.16.8.

Targets and secrets live in DATA_DIR/backup-targets.json (0600, root only); secrets never go
back to the browser. Results go to DATA_DIR/backup-targets-status.json.
"""
import calendar
import datetime
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from core import backups, manager, notify
from core.settings import DATA_DIR
from core.platform import paths, services, system

CONFIG_FILE = os.path.join(DATA_DIR, "backup-targets.json")
STATUS_FILE = os.path.join(DATA_DIR, "backup-targets-status.json")
LOCK_DIR = os.path.join(DATA_DIR, "backup-targets-locks")
SSH_DIR = os.path.join(DATA_DIR, "ssh")
SSH_KEY = os.path.join(SSH_DIR, "id_ed25519")
KNOWN_HOSTS = os.path.join(SSH_DIR, "known_hosts")

SSH = "ssh"                     # the commands, replaceable in tests
RSYNC = "rsync"
SSH_KEYGEN = "ssh-keygen"
SYSTEMD_RUN = "systemd-run"
PANEL_CMD = paths.PANEL_CMD

TYPES = ("ssh", "s3")
COPY_KINDS = ("scheduled", "manual")
MAX_TARGETS = 10
MAX_DAYS = 3650
SWEEP_EVERY = 300
FIRST_SWEEP = 120
REFRESH_EVERY = 6 * 3600        # look at the target itself at least this often, even with nothing new
TIMEOUT = 60
CHUNK = 1 << 20
MULTIPART_OVER = 100 * 1024 * 1024
PART_SIZE = 64 * 1024 * 1024
MAX_PARTS = 10000
SHA_SUFFIX = ".sha256"
TEST_DIR = ".redstonepanel-test"

TARGET_ID_RE = re.compile(r"^t-[0-9a-f]{8}$")
PANEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
                     r"|\[?[0-9A-Fa-f:.]+\]?)$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
FOLDER_RE = re.compile(r"^[A-Za-z0-9_./~-]{1,200}$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9_./-]{0,200}$")
REGION_RE = re.compile(r"^[a-z0-9-]{1,40}$")
KEY_RE = re.compile(r"^[\x21-\x7e]{1,200}$")

_lock = threading.Lock()


class TargetError(ValueError):
    """Something a user can act on (message is safe to show)."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def _read(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load():
    data = _read(CONFIG_FILE)
    targets = data.get("targets")
    data["targets"] = {k: v for k, v in (targets or {}).items()
                       if TARGET_ID_RE.fullmatch(k) and isinstance(v, dict) and v.get("type") in TYPES} \
        if isinstance(targets, dict) else {}
    return data


def panel_id():
    """This machine's folder name on every target: its host name and four random hex digits."""
    with _lock:
        data = load()
        pid = data.get("panel_id")
        if isinstance(pid, str) and PANEL_ID_RE.fullmatch(pid):
            return pid
        host = re.sub(r"[^a-z0-9-]+", "-", socket.gethostname().lower()).strip("-")[:40] or "panel"
        pid = f"{host}-{secrets.token_hex(2)}"
        data["panel_id"] = pid
        _write(CONFIG_FILE, data)
        return pid


def get_target(tid):
    t = load()["targets"].get(tid)
    if not t:
        raise TargetError("No such backup storage.", 404)
    return {**t, "id": tid}


def _int(value, low, high, what):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise TargetError(f"{what} must be a whole number.")
    if not low <= n <= high:
        raise TargetError(f"{what} must be between {low} and {high}.")
    return n


def _clean(fields, old=None):
    """A target's settings from the form, checked. Blank secret = keep the old one."""
    old = old or {}
    kind = fields.get("type") or old.get("type")
    if kind not in TYPES:
        raise TargetError("Choose SSH or S3-compatible storage.")
    name = str(fields.get("name") or "").strip()[:60]
    if not name:
        raise TargetError("Give the storage a name.")
    t = {"type": kind, "name": name,
         "keep_count": _int(fields.get("keep_count") or 0, 0, backups.MAX_KEEP, "Copies to keep"),
         "keep_days": _int(fields.get("keep_days") or 0, 0, MAX_DAYS, "Days to keep copies")}
    servers = fields.get("servers", old.get("servers", "all"))
    if servers != "all":
        if not isinstance(servers, list) or not all(isinstance(s, str) and backups.NAME_RE.fullmatch(s)
                                                    for s in servers):
            raise TargetError("Bad list of servers.")
        servers = sorted(set(servers))
    t["servers"] = servers
    if kind == "ssh":
        host = str(fields.get("host") or "").strip()
        if not HOST_RE.fullmatch(host) or host.startswith("-"):
            raise TargetError("Give the SSH server's host name or IP address.")
        user = str(fields.get("user") or "").strip()
        if not USER_RE.fullmatch(user):
            raise TargetError("Give the user to sign in as on the SSH server.")
        folder = str(fields.get("folder") or "").strip().rstrip("/") or "redstonepanel-backups"
        if not FOLDER_RE.fullmatch(folder) or ".." in folder.split("/") or folder.startswith("-"):
            raise TargetError("The folder may only use letters, digits and _ . / ~ -")
        t.update(host=host.strip("[]"), port=_int(fields.get("port") or 22, 1, 65535, "The SSH port"),
                 user=user, folder=folder)
    else:
        endpoint = str(fields.get("endpoint") or "").strip().rstrip("/")
        u = urllib.parse.urlsplit(endpoint)
        if u.scheme not in ("https", "http") or not u.hostname or u.path not in ("", "/") or u.query \
                or u.username or u.password:
            raise TargetError("The endpoint is a web address such as https://s3.eu-central-003.backblazeb2.com")
        bucket = str(fields.get("bucket") or "").strip()
        if not BUCKET_RE.fullmatch(bucket):
            raise TargetError("Give the bucket's name (lowercase letters, digits, dots and dashes).")
        prefix = str(fields.get("prefix") or "").strip().strip("/")
        if not PREFIX_RE.fullmatch(prefix) or ".." in prefix.split("/"):
            raise TargetError("The folder in the bucket may only use letters, digits and _ . / -")
        region = str(fields.get("region") or "").strip() or "us-east-1"
        if not REGION_RE.fullmatch(region):
            raise TargetError("The region looks like us-east-1 (Cloudflare R2: auto).")
        access = str(fields.get("access_key") or "").strip() or old.get("access_key", "")
        secret = str(fields.get("secret_key") or "").strip() or old.get("secret_key", "")
        if not KEY_RE.fullmatch(access) or not KEY_RE.fullmatch(secret):
            raise TargetError("Give the access key and the secret key.")
        t.update(endpoint=f"{u.scheme}://{u.netloc}", bucket=bucket, prefix=prefix, region=region,
                 access_key=access, secret_key=secret,
                 path_style=bool(fields.get("path_style", old.get("path_style", True))))
    return t


def public(t):
    """A target as the browser sees it: never the secret key."""
    out = {k: v for k, v in t.items() if k != "secret_key"}
    if t.get("type") == "s3":
        out["secret_set"] = bool(t.get("secret_key"))
        out["access_key"] = (t.get("access_key") or "")[:4] + "…" if t.get("access_key") else ""
        out["where"] = f"{t['bucket']}/{t['prefix'] + '/' if t.get('prefix') else ''} on {urllib.parse.urlsplit(t['endpoint']).hostname}"
    else:
        out["where"] = f"{t['user']}@{t['host']}:{t['folder']}" + (f" (port {t['port']})" if t.get("port", 22) != 22 else "")
        out["host_key"] = host_key(t)
    return out


def save_target(tid, fields, actor):
    """Adds (tid None) or changes a target. Returns its public view."""
    with _lock:
        data = load()
        is_new = not tid
        if tid:
            if tid not in data["targets"]:
                raise TargetError("No such backup storage.", 404)
            t = _clean(fields, data["targets"][tid])
            t["created"] = data["targets"][tid].get("created")
        else:
            if len(data["targets"]) >= MAX_TARGETS:
                raise TargetError(f"At most {MAX_TARGETS} backup storages.")
            t = _clean(fields)
            t["created"] = int(time.time())
            tid = f"t-{secrets.token_hex(4)}"
        data["targets"][tid] = t
        _write(CONFIG_FILE, data)
    manager.write_log(f"[BACKUP] {actor} {'added' if is_new else 'changed'} "
                      f"backup storage '{t['name']}' ({public({**t, 'id': tid})['where']}).")
    return public({**t, "id": tid})


def remove_target(tid, actor):
    with _lock:
        data = load()
        t = data["targets"].pop(tid, None)
        if not t:
            raise TargetError("No such backup storage.", 404)
        _write(CONFIG_FILE, data)
        status = _read(STATUS_FILE)
        keep = {k: v for k, v in status.items() if not k.startswith(f"{tid}/")}
        if keep != status:
            _write(STATUS_FILE, keep)
    manager.write_log(f"[BACKUP] {actor} removed backup storage '{t['name']}'. The copies already there stay there.")
    return {"status": "success"}


def targets_for(server):
    """The ids of the targets that keep copies of this server's backups."""
    return [tid for tid, t in sorted(load()["targets"].items())
            if t.get("servers") == "all" or server in (t.get("servers") or [])]


def set_server(tid, server, enabled, actor):
    """Turns copies of one server's backups to one target on or off."""
    if not backups.NAME_RE.fullmatch(server or ""):
        raise TargetError("Bad server name.")
    with _lock:
        data = load()
        t = data["targets"].get(tid)
        if not t:
            raise TargetError("No such backup storage.", 404)
        servers = t.get("servers", "all")
        if servers == "all":
            if enabled:
                return
            servers = [s["name"] for s in manager.list_servers() if s["name"] != server]
        else:
            servers = sorted(set(servers) | {server}) if enabled else [s for s in servers if s != server]
        t["servers"] = servers
        _write(CONFIG_FILE, data)
    manager.write_log(f"[BACKUP] {actor}: {'copying' if enabled else 'no longer copying'} {server}'s backups to "
                      f"'{t['name']}'." + ("" if enabled else " Copies already there stay there."))
    if enabled:
        sync_soon(server)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def _key(tid, server):
    return f"{tid}/{server}"


def status(tid, server):
    return _read(STATUS_FILE).get(_key(tid, server))


def _set_status(tid, server, **fields):
    with _lock:
        data = _read(STATUS_FILE)
        cur = data.get(_key(tid, server)) or {}
        cur.update(fields)
        data[_key(tid, server)] = cur
        _write(STATUS_FILE, data)
        return cur


def attention():
    """Needs attention items: copies to a target that keep failing."""
    out = []
    targets = load()["targets"]
    for key, st in _read(STATUS_FILE).items():
        tid, _, server = key.partition("/")
        t = targets.get(tid)
        if not t or not isinstance(st, dict) or st.get("ok") is not False:
            continue
        if t.get("servers") != "all" and server not in (t.get("servers") or []):
            continue
        out.append({"level": "warn", "server": server, "where": "backups",
                    "text": f"Copying the backups of {server} to '{t['name']}' failed: {st.get('error') or 'unknown error'}"})
    return out


# ---------------------------------------------------------------------------
# What to copy and what to delete
# ---------------------------------------------------------------------------
def _limits(t, server):
    keep = t.get("keep_count") or backups.get_schedule(server)["keep"]
    return int(keep), int(t.get("keep_days") or 0)


def scheduled_to_keep(entries, keep, keep_days, now=None):
    """Of entries (newest first, each with kind and created), the scheduled ones retention keeps:
    the newest `keep`, minus those older than keep_days, but always the newest one."""
    now = now or time.time()
    scheduled = [e for e in entries if e["kind"] == "scheduled"]
    kept = scheduled[:keep]
    if keep_days:
        kept = [e for e in kept if now - e["created"] <= keep_days * 86400] or scheduled[:1]
    return kept


def wanted(local, keep, keep_days, now=None):
    """The local backups that should have a copy: every manual one, plus the scheduled ones the
    target's retention keeps."""
    sched = {e["file"] for e in scheduled_to_keep(local, keep, keep_days, now)}
    return [b for b in local if b["kind"] == "manual" or b["file"] in sched]


def to_delete(remote, keep, keep_days, now=None):
    """The scheduled copies on the target that retention no longer keeps."""
    kept = {e["file"] for e in scheduled_to_keep(remote, keep, keep_days, now)}
    return [e for e in remote if e["kind"] == "scheduled" and e["file"] not in kept]


def _entry(panel, server, filename, size):
    m = backups.FILE_RE.fullmatch(filename)
    if not m or m.group("server") != server:
        return None
    return {"panel": panel, "server": server, "file": filename, "kind": m.group("kind"), "size": size,
            "created": calendar.timegm(time.strptime(m.group("stamp"), "%Y%m%d-%H%M%S"))}


# ---------------------------------------------------------------------------
# SSH/rsync
# ---------------------------------------------------------------------------
def ensure_key():
    """This panel's SSH key pair; made on first use. Returns the public key line."""
    if not os.path.isfile(SSH_KEY):
        os.makedirs(SSH_DIR, mode=0o700, exist_ok=True)
        r = subprocess.run([SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", f"redstonepanel@{socket.gethostname()}",
                            "-f", SSH_KEY], capture_output=True, text=True)
        if r.returncode != 0:
            raise TargetError(f"Couldn't make an SSH key: {(r.stderr or r.stdout).strip() or 'ssh-keygen failed'}. "
                              "Is openssh-client installed? (sudo apt install openssh-client)")
        manager.write_log("[BACKUP] Made this panel's SSH key for backup storage.")
    with open(SSH_KEY + ".pub") as f:
        return f.read().strip()


def public_key():
    try:
        return ensure_key()
    except (OSError, FileNotFoundError):
        raise TargetError("ssh-keygen isn't installed. Install it with: sudo apt install openssh-client rsync")


def _known_name(t):
    host = t["host"]
    return host if int(t.get("port", 22)) == 22 else f"[{host}]:{t['port']}"


def host_key(t):
    """The fingerprint ssh recorded for the target on first use, or None."""
    if not os.path.isfile(KNOWN_HOSTS):
        return None
    try:
        r = subprocess.run([SSH_KEYGEN, "-l", "-F", _known_name(t), "-f", KNOWN_HOSTS],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in r.stdout.splitlines():
        if line and not line.startswith("#"):
            parts = line.split()
            fp = next((p for p in parts if p.startswith("SHA256:")), None)
            if fp:
                return f"{parts[1] if len(parts) > 1 and not parts[1].startswith('SHA256:') else ''} {fp}".strip()
    return None


def forget_host_key(tid, actor):
    t = get_target(tid)
    if t["type"] != "ssh":
        raise TargetError("Only SSH storage has a host key.")
    if os.path.isfile(KNOWN_HOSTS):
        subprocess.run([SSH_KEYGEN, "-R", _known_name(t), "-f", KNOWN_HOSTS], capture_output=True)
        try:
            os.remove(KNOWN_HOSTS + ".old")
        except OSError:
            pass
    manager.write_log(f"[BACKUP] {actor} forgot the SSH host key of '{t['name']}'; the next copy accepts the key it "
                      f"finds then.")
    return public(t)


class SSHTarget:
    def __init__(self, t):
        self.t = t
        self.base = t["folder"]

    def _ssh_opts(self):
        return ["-i", SSH_KEY, "-p", str(self.t.get("port", 22)), "-o", "BatchMode=yes",
                "-o", "IdentitiesOnly=yes", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
                "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20",
                "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4"]

    def _dest(self):
        host = self.t["host"]
        return f"{self.t['user']}@{'[' + host + ']' if ':' in host else host}"

    def _q(self, *parts):
        """A remote path, quoted for the remote shell. ~ at the start stays the home folder."""
        path = "/".join(p for p in parts if p)
        if path.startswith("~/"):
            return '"$HOME"/' + shlex.quote(path[2:])
        if path == "~":
            return '"$HOME"'
        return shlex.quote(path)

    def run(self, command, timeout=120):
        ensure_key()
        os.makedirs(SSH_DIR, mode=0o700, exist_ok=True)
        try:
            r = subprocess.run([SSH, *self._ssh_opts(), self._dest(), command],
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TargetError(f"{self.t['host']} didn't answer in {timeout} seconds.")
        except FileNotFoundError:
            raise TargetError("ssh isn't installed. Install it with: sudo apt install openssh-client rsync")
        if r.returncode == 255:
            raise TargetError(_ssh_error(r.stderr, self.t))
        return r

    def _check(self, r, what):
        if r.returncode != 0:
            raise TargetError(f"{what} failed on {self.t['host']}: {(r.stderr or r.stdout).strip()[-300:] or f'exit {r.returncode}'}")
        return r.stdout

    def list(self, panel=None, server=None):
        """[(panel, server, file, size)] under the folder (one panel or server if given)."""
        depth = 3 - bool(panel) - bool(server)
        where = self._q(self.base, panel or "", server or "")
        r = self.run(f"cd {where} 2>/dev/null || exit 0; find . -mindepth {depth} -maxdepth {depth} -type f "
                     f"-name '*.tar.gz' -exec ls -ln {{}} +")
        out = []
        for line in self._check(r, "Listing the backups").splitlines():
            parts = line.split()
            if len(parts) < 6 or not parts[4].isdigit():
                continue
            rel = parts[-1][2:] if parts[-1].startswith("./") else parts[-1]
            bits = ([panel] if panel else []) + ([server] if server else []) + rel.split("/")
            if len(bits) == 3:
                out.append((bits[0], bits[1], bits[2], int(parts[4])))
        return out

    def sha(self, panel, server, filename):
        r = self.run(f"cat {self._q(self.base, panel, server, filename + SHA_SUFFIX)} 2>/dev/null")
        sha = r.stdout.strip()
        return sha if re.fullmatch(r"[0-9a-f]{64}", sha) else None

    def _rsh(self):
        return " ".join([SSH, *self._ssh_opts()])

    def upload(self, path, panel, server, filename, sha, size):
        d = self._q(self.base, panel, server)
        self._check(self.run(f"mkdir -p {d}"), "Making the folder")
        remote = "/".join(p for p in (self.base, panel, server, filename + ".partial") if p)
        if remote.startswith("~/"):
            remote = remote[2:]                  # rsync paths are relative to the home folder already
        try:
            r = subprocess.run([RSYNC, "--partial", "--inplace", "--times", "--chmod=F600", "-e", self._rsh(),
                                path, f"{self._dest()}:{remote}"], capture_output=True, text=True, timeout=24 * 3600)
        except FileNotFoundError:
            raise TargetError("rsync isn't installed here. Install it with: sudo apt install rsync")
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip()
            if "command not found" in err or r.returncode == 127:
                err = "rsync isn't installed there. Install it on that machine (sudo apt install rsync)."
            raise TargetError(_ssh_error(err, self.t) if r.returncode == 255 else f"rsync failed: {err[-300:]}")
        f, part, side = (self._q(self.base, panel, server, filename + s) for s in ("", ".partial", SHA_SUFFIX))
        check = (f"h=$( (sha256sum {part} || shasum -a 256 {part}) 2>/dev/null | cut -d' ' -f1); "
                 f"if [ \"$h\" = {sha} ]; then printf %s {sha} > {side} && mv -f {part} {f}; "
                 f"else rm -f {part}; echo \"checksum $h\" >&2; exit 3; fi")
        r = self.run(check, timeout=3600)
        if r.returncode == 3:
            raise TargetError(f"The copy on {self.t['host']} arrived damaged (SHA-256 doesn't match) and was deleted; "
                              "it's copied again next time.")
        self._check(r, "Checking the copy")

    def download(self, panel, server, filename, dest):
        src = "/".join((self.base, panel, server, filename))
        if src.startswith("~/"):
            src = src[2:]
        try:
            r = subprocess.run([RSYNC, "--partial", "--times", "-e", self._rsh(), f"{self._dest()}:{src}", dest],
                               capture_output=True, text=True, timeout=24 * 3600)
        except FileNotFoundError:
            raise TargetError("rsync isn't installed here. Install it with: sudo apt install rsync")
        if r.returncode != 0:
            raise TargetError(f"Downloading the copy failed: {(r.stderr or r.stdout).strip()[-300:]}")

    def delete(self, panel, server, filename):
        self._check(self.run(f"rm -f {self._q(self.base, panel, server, filename)} "
                             f"{self._q(self.base, panel, server, filename + SHA_SUFFIX)}"), "Deleting the copy")

    def test(self):
        """Signs in, writes, lists and deletes a small file. Returns the steps that worked."""
        name = f"{TEST_DIR}/{secrets.token_hex(4)}.txt"
        steps = []
        r = self.run("command -v rsync >/dev/null 2>&1 || echo NO-RSYNC; "
                     "(command -v sha256sum || command -v shasum) >/dev/null 2>&1 || echo NO-SHA", timeout=40)
        if "NO-RSYNC" in r.stdout:
            raise TargetError("Signed in, but rsync isn't installed there. Install it on that machine "
                              "(sudo apt install rsync).")
        if "NO-SHA" in r.stdout:
            raise TargetError("Signed in, but neither sha256sum nor shasum is there to check copies.")
        steps.append("signed in")
        path = self._q(self.base, name)
        self._check(self.run(f"mkdir -p {self._q(self.base, TEST_DIR)} && echo redstonepanel > {path}"),
                    "Writing a test file")
        steps.append("wrote a file")
        self._check(self.run(f"ls {path}"), "Listing it")
        steps.append("listed it")
        self._check(self.run(f"rm -f {path} && rmdir {self._q(self.base, TEST_DIR)} 2>/dev/null; true"),
                    "Deleting it")
        steps.append("deleted it")
        return steps


def _ssh_error(stderr, t):
    text = (stderr or "").strip()
    if "REMOTE HOST IDENTIFICATION HAS CHANGED" in text or "Host key verification failed" in text:
        return (f"{t['host']}'s host key isn't the one seen before, so nothing was sent. If that machine was "
                f"reinstalled, use Forget host key in Panel Settings → Backup storage.")
    if "Permission denied" in text:
        return (f"{t['host']} refused the panel's SSH key for {t['user']}. Add the public key shown in Panel Settings "
                f"→ Backup storage to ~{t['user']}/.ssh/authorized_keys there.")
    if "Could not resolve" in text or "Name or service not known" in text:
        return f"Can't find {t['host']}: check the host name."
    if "Connection refused" in text:
        return f"{t['host']} refused the connection on port {t.get('port', 22)}: is SSH running there?"
    if "timed out" in text:
        return f"{t['host']} didn't answer on port {t.get('port', 22)}."
    lines = [l for l in text.splitlines() if l and not l.startswith("Warning: Permanently added")]
    return f"SSH to {t['host']} failed: {(lines[-1] if lines else 'no reason given')[:300]}"


# ---------------------------------------------------------------------------
# S3-compatible storage, signed with AWS Signature Version 4
# ---------------------------------------------------------------------------
EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def _hmac(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _uri(path):
    return urllib.parse.quote(path, safe="/-_.~")


def _query_string(query):
    return "&".join(f"{urllib.parse.quote(str(k), safe='-_.~')}={urllib.parse.quote(str(v), safe='-_.~')}"
                    for k, v in sorted(query.items()))


def sign(method, host, path, query, headers, payload_sha, access_key, secret_key, region, amz_date):
    """Adds the Authorization header (AWS Signature Version 4, service s3) to headers and returns it.
    headers must not yet hold host or x-amz-*; they're added here."""
    headers = {**headers, "host": host, "x-amz-content-sha256": payload_sha, "x-amz-date": amz_date}
    lower = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    names = sorted(lower)
    canonical = "\n".join([method, _uri(path), _query_string(query),
                           "".join(f"{n}:{lower[n]}\n" for n in names), ";".join(names), payload_sha])
    day = amz_date[:8]
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), day), region), "s3"), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                                f"SignedHeaders={';'.join(names)}, Signature={signature}")
    return headers


def _file_part(path, offset, length):
    with open(path, "rb") as f:
        f.seek(offset)
        left = length
        while left > 0:
            chunk = f.read(min(CHUNK, left))
            if not chunk:
                return
            left -= len(chunk)
            yield chunk


def _sha_part(path, offset, length):
    h = hashlib.sha256()
    for chunk in _file_part(path, offset, length):
        h.update(chunk)
    return h.hexdigest()


class S3Target:
    def __init__(self, t):
        self.t = t
        u = urllib.parse.urlsplit(t["endpoint"])
        self.scheme, self.netloc = u.scheme, u.netloc
        self.virtual = not t.get("path_style", True)
        self.host = f"{t['bucket']}.{u.netloc}" if self.virtual else u.netloc
        self.base = t.get("prefix") or ""

    def _key(self, *parts):
        return "/".join(p for p in (self.base, *parts) if p)

    def _path(self, key=""):
        return ("/" + key) if self.virtual else (f"/{self.t['bucket']}" + (f"/{key}" if key else "/"))

    def _conn(self):
        host = self.host
        proxy = None
        env = urllib.request.getproxies()
        if self.scheme in env and not urllib.request.proxy_bypass(host.split(":")[0]):
            proxy = urllib.parse.urlsplit(env[self.scheme])
        if self.scheme == "https":
            ctx = ssl.create_default_context()
            if proxy:
                c = http.client.HTTPSConnection(proxy.hostname, proxy.port or 3128, timeout=TIMEOUT, context=ctx)
                c.set_tunnel(host)
                return c
            return http.client.HTTPSConnection(host, timeout=TIMEOUT, context=ctx)
        if proxy:
            c = http.client.HTTPConnection(proxy.hostname, proxy.port or 3128, timeout=TIMEOUT)
            c.set_tunnel(host)
            return c
        return http.client.HTTPConnection(host, timeout=TIMEOUT)

    def request(self, method, key="", query=None, body=None, payload_sha=EMPTY_SHA, headers=None, length=None,
                ok=(200,), stream_to=None):
        query = query or {}
        amz_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self._path(key)
        h = sign(method, self.host, path, query, headers or {}, payload_sha, self.t["access_key"],
                 self.t["secret_key"], self.t["region"], amz_date)
        if length is not None:
            h["Content-Length"] = str(length)
        elif body is None and method in ("PUT", "POST"):
            h["Content-Length"] = "0"
        url = _uri(path) + (f"?{_query_string(query)}" if query else "")
        conn = self._conn()
        try:
            conn.putrequest(method, url, skip_host=True, skip_accept_encoding=True)
            for k, v in h.items():
                conn.putheader(k, v)
            conn.endheaders()
            if body is not None:
                for chunk in ([body] if isinstance(body, bytes) else body):
                    conn.send(chunk)
            res = conn.getresponse()
            if res.status not in ok:
                raise TargetError(_s3_error(res.status, res.read(), self.t))
            if stream_to:
                while True:
                    chunk = res.read(CHUNK)
                    if not chunk:
                        break
                    stream_to(chunk)
                return res, b""
            return res, res.read()
        except (OSError, http.client.HTTPException) as e:
            raise TargetError(f"Can't reach {self.host}: {e}")
        finally:
            conn.close()

    def list(self, panel=None, server=None):
        prefix = self._key(panel or "", server or "")
        prefix = prefix + "/" if prefix else ""
        out, token = [], None
        while True:
            q = {"list-type": "2", "prefix": prefix}
            if token:
                q["continuation-token"] = token
            _, xml = self.request("GET", "", q)
            root = ET.fromstring(xml)
            ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
            for c in root.iter(f"{ns}Contents"):
                key, size = c.findtext(f"{ns}Key") or "", c.findtext(f"{ns}Size") or "0"
                rel = key[len(self.base) + 1:] if self.base else key
                bits = rel.split("/")
                if len(bits) == 3 and bits[2].endswith(".tar.gz") and size.isdigit():
                    out.append((bits[0], bits[1], bits[2], int(size)))
            if (root.findtext(f"{ns}IsTruncated") or "").lower() == "true":
                token = root.findtext(f"{ns}NextContinuationToken")
                if not token:
                    break
            else:
                break
        return out

    def sha(self, panel, server, filename):
        try:
            _, body = self.request("GET", self._key(panel, server, filename + SHA_SUFFIX))
        except TargetError:
            return None
        sha = body.decode(errors="replace").strip()
        return sha if re.fullmatch(r"[0-9a-f]{64}", sha) else None

    def _put_small(self, key, data):
        self.request("PUT", key, body=data, payload_sha=hashlib.sha256(data).hexdigest(), length=len(data))

    def upload(self, path, panel, server, filename, sha, size):
        key = self._key(panel, server, filename)
        meta = {"x-amz-meta-sha256": sha}
        if size <= MULTIPART_OVER:
            self.request("PUT", key, body=_file_part(path, 0, size), payload_sha=sha, length=size, headers=meta)
        else:
            self._multipart(path, key, size, meta)
        self._put_small(key + SHA_SUFFIX, sha.encode())

    def _multipart(self, path, key, size, meta):
        part = max(PART_SIZE, -(-size // MAX_PARTS))
        _, xml = self.request("POST", key, {"uploads": ""}, headers=meta)
        root = ET.fromstring(xml)
        ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
        upload_id = root.findtext(f"{ns}UploadId")
        if not upload_id:
            raise TargetError("The storage didn't start the upload (no UploadId).")
        try:
            etags = []
            for n, offset in enumerate(range(0, size, part), start=1):
                length = min(part, size - offset)
                res, _ = self.request("PUT", key, {"partNumber": str(n), "uploadId": upload_id},
                                      body=_file_part(path, offset, length), payload_sha=_sha_part(path, offset, length),
                                      length=length)
                etags.append((n, res.getheader("ETag") or ""))
            done = "<CompleteMultipartUpload>" + "".join(
                f"<Part><PartNumber>{n}</PartNumber><ETag>{e}</ETag></Part>" for n, e in etags) + "</CompleteMultipartUpload>"
            data = done.encode()
            _, body = self.request("POST", key, {"uploadId": upload_id}, body=data,
                                   payload_sha=hashlib.sha256(data).hexdigest(), length=len(data))
            if b"<Error>" in body:               # S3 can answer 200 and still fail the completion
                raise TargetError(_s3_error(200, body, self.t))
        except BaseException:
            try:
                self.request("DELETE", key, {"uploadId": upload_id}, ok=(200, 204, 404))
            except TargetError:
                pass
            raise

    def download(self, panel, server, filename, dest):
        with open(dest, "wb") as f:
            self.request("GET", self._key(panel, server, filename), stream_to=f.write)

    def delete(self, panel, server, filename):
        for suffix in ("", SHA_SUFFIX):
            self.request("DELETE", self._key(panel, server, filename + suffix), ok=(200, 204, 404))

    def test(self):
        key = self._key(TEST_DIR, f"{secrets.token_hex(4)}.txt")
        steps = []
        self._put_small(key, b"redstonepanel\n")
        steps.append("wrote a file")
        _, xml = self.request("GET", "", {"list-type": "2", "prefix": key})
        if key.encode() not in xml:
            raise TargetError("Wrote a test file, but listing the bucket didn't show it.")
        steps.append("listed it")
        self.request("DELETE", key, ok=(200, 204))
        steps.append("deleted it")
        return steps


def _s3_error(status, body, t):
    code = msg = ""
    try:
        root = ET.fromstring(body)
        code, msg = root.findtext("Code") or "", root.findtext("Message") or ""
    except ET.ParseError:
        pass
    if code in ("InvalidAccessKeyId", "SignatureDoesNotMatch") or status == 401:
        return f"The storage refused the access key or secret ({code or status}). Check them, and the region."
    if code == "NoSuchBucket":
        return f"There's no bucket called {t['bucket']} there."
    if code == "AccessDenied" or status == 403:
        return f"Access denied ({msg or code or status}): the key needs to read, write, list and delete in {t['bucket']}."
    if code == "AuthorizationHeaderMalformed" or code == "PermanentRedirect":
        return f"{msg or code}: check the endpoint and region."
    return f"The storage answered HTTP {status}" + (f": {code} {msg}".rstrip() if code or msg else ".")


def client(t):
    return SSHTarget(t) if t["type"] == "ssh" else S3Target(t)


def test_target(tid=None, fields=None):
    """Tries a saved target (tid) or one being added (fields). Returns {ok, steps, error, host_key}."""
    if tid:
        t = get_target(tid)
        if fields:
            t = {**_clean(fields, t), "id": tid}
    else:
        t = _clean(fields or {})
    c = client(t)
    steps = []
    try:
        steps = c.test()
        return {"ok": True, "steps": steps, "error": None, "host_key": host_key(t) if t["type"] == "ssh" else None}
    except TargetError as e:
        return {"ok": False, "steps": steps, "error": str(e), "host_key": host_key(t) if t["type"] == "ssh" else None}


# ---------------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------------
class _FileLock:
    """One copy job per target and server at a time, across the panel and the backup command."""
    def __init__(self, tid, server):
        os.makedirs(LOCK_DIR, mode=0o700, exist_ok=True)
        self.fd = os.open(os.path.join(LOCK_DIR, f"{tid}-{server}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            system.lock(self.fd)
        except BlockingIOError:
            os.close(self.fd)
            raise TargetError(f"{server}'s backups are being copied already.", 409)

    def release(self):
        os.close(self.fd)


def sync_one(tid, server, force=False, now=None):
    """Copies what's missing of server's backups to target tid and applies its retention.
    Records the result; raises TargetError on failure. Returns {"copied": [...], "deleted": [...]}.
    Without force, the target is only contacted when something is new, due for deletion, or
    REFRESH_EVERY has passed since it was last listed."""
    t = get_target(tid)
    keep, keep_days = _limits(t, server)
    local = backups.list_backups(server)
    want = wanted(local, keep, keep_days, now)
    prev = status(tid, server) or {}
    have = set(prev.get("have") or [])
    known = [e for e in (_entry(prev.get("panel"), server, f, 0) for f in have) if e]
    if not force and prev.get("ok") and prev.get("panel") == panel_id() \
            and time.time() - (prev.get("listed_at") or 0) < REFRESH_EVERY \
            and {b["file"] for b in want} <= have and not to_delete(sorted(known, key=lambda e: e["created"], reverse=True),
                                                                   keep, keep_days, now):
        return {"copied": [], "deleted": [], "skipped": True}

    lock = _FileLock(tid, server)
    started = time.time()
    copied, deleted = [], []
    pid = panel_id()
    try:
        c = client(t)
        remote = {f: size for p, s, f, size in c.list(pid, server)}
        for b in sorted(want, key=lambda x: x["created"]):
            if remote.get(b["file"]) == b["size"]:
                continue
            path = backups.backup_path(server, b["file"])
            sha = _sha256_file(path)
            c.upload(path, pid, server, b["file"], sha, b["size"])
            remote[b["file"]] = b["size"]
            copied.append(b["file"])
        entries = sorted((e for e in (_entry(pid, server, f, s) for f, s in remote.items()) if e),
                         key=lambda e: (e["created"], e["file"]), reverse=True)
        for e in to_delete(entries, keep, keep_days, now):
            c.delete(pid, server, e["file"])
            remote.pop(e["file"], None)
            deleted.append(e["file"])
    except Exception as e:
        msg = str(e) if isinstance(e, (TargetError, backups.BackupError)) else f"{type(e).__name__}: {e}"
        _set_status(tid, server, ok=False, at=int(started), error=msg, panel=pid,
                    failing_since=prev.get("failing_since") if prev.get("ok") is False else int(started))
        manager.write_log(f"[BACKUP] Copying {server}'s backups to '{t['name']}' failed: {msg}")
        if prev.get("ok") is not False:         # once per failure, not every few minutes
            notify.send("backup_copy_failed", server, f"Copying {server}'s backups to '{t['name']}' failed: {msg}")
        raise TargetError(f"Copying to '{t['name']}' failed: {msg}", 502)
    finally:
        lock.release()
    fields = {"ok": True, "at": int(started), "error": None, "failing_since": None, "panel": pid,
              "have": sorted(remote), "listed_at": int(started)}
    if copied:
        fields.update(last_copied=copied[-1], last_copied_at=int(time.time()))
    _set_status(tid, server, **fields)
    if prev.get("ok") is False:
        manager.write_log(f"[BACKUP] Copying {server}'s backups to '{t['name']}' works again.")
    if copied:
        manager.write_log(f"[BACKUP] Copied {len(copied)} backup{'s' if len(copied) != 1 else ''} of {server} "
                          f"to '{t['name']}'.")
    for f in deleted:
        manager.write_log(f"[BACKUP] Deleted the old copy {f} of {server} from '{t['name']}' "
                          f"(keeping {keep}{f', at most {keep_days} days old' if keep_days else ''}).")
    return {"copied": copied, "deleted": deleted}


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def sync(server, force=False):
    """sync_one() for every target of this server. Returns {tid: result or error message}."""
    out = {}
    for tid in targets_for(server):
        try:
            out[tid] = sync_one(tid, server, force)
        except TargetError as e:
            out[tid] = {"error": str(e)}
        except Exception as e:                  # never let one target stop the others
            manager.write_log(f"[BACKUP] Copying {server}'s backups failed: {type(e).__name__}: {e}")
            out[tid] = {"error": str(e)}
    return out


def sync_soon(server, force=True):
    """sync() in a background thread."""
    if not targets_for(server):
        return False
    threading.Thread(target=sync, args=(server, force), daemon=True, name=f"offsite-{server}").start()
    return True


def start_after_backup(server):
    """Called by `redstonepanel backup` once the backup is saved: copies run in their own
    transient unit, so the backup unit (and anything waiting for it) finishes now."""
    if not targets_for(server):
        return False
    try:
        r = services.run_detached(f"redstonepanel-offsite-{server}", [PANEL_CMD, "offsite-copy", server],
                                  low_priority=True, no_block=True, timeout=30, program=SYSTEMD_RUN)
        if r.returncode == 0:
            return True
        if "already" in (r.stderr or "") or "exists" in (r.stderr or ""):
            return True                         # one is running; the panel's next pass picks this up
    except (OSError, subprocess.TimeoutExpired):
        pass
    sync(server, force=True)                    # no systemd-run: copy here, after the backup
    return True


def sweep():
    """One pass: every server with a target, contacting a target only when there's work."""
    servers = {s["name"] for s in manager.list_servers() if backups.NAME_RE.fullmatch(s["name"])}
    for server in sorted(servers):
        for tid in targets_for(server):
            try:
                sync_one(tid, server)
            except TargetError:
                pass                            # recorded by sync_one
            except Exception as e:
                manager.write_log(f"[BACKUP] Copying {server}'s backups failed: {type(e).__name__}: {e}")


def sweeper(every=SWEEP_EVERY, first=FIRST_SWEEP):
    def loop():
        time.sleep(first)
        while True:
            try:
                if load()["targets"]:
                    sweep()
            except Exception as e:              # never let this take the panel down
                manager.write_log(f"[BACKUP] Off-site copies failed: {type(e).__name__}: {e}")
            time.sleep(every)
    t = threading.Thread(target=loop, name="offsite-copies", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# The Backups tab, restoring, deleting, and importing on a fresh machine
# ---------------------------------------------------------------------------
def view(server, list_copies=True):
    """The Backups tab's off-site section for one of this panel's servers."""
    manager._check_server(server)
    data = load()
    pid = data.get("panel_id") or panel_id()
    out = {"targets": [], "panel_id": pid}
    for tid, t in sorted(data["targets"].items()):
        on = t.get("servers") == "all" or server in (t.get("servers") or [])
        keep, keep_days = _limits(t, server)
        row = {"id": tid, "name": t["name"], "type": t["type"], "where": public({**t, "id": tid})["where"],
               "enabled": on, "all_servers": t.get("servers") == "all", "keep": keep, "keep_days": keep_days,
               "status": status(tid, server), "copies": [], "error": None}
        if on and list_copies:
            try:
                row["copies"] = sorted((e for e in (_entry(p, s, f, size) for p, s, f, size
                                                    in client(t).list(pid, server)) if e),
                                       key=lambda e: (e["created"], e["file"]), reverse=True)
            except TargetError as e:
                row["error"] = str(e)
        out["targets"].append(row)
    return out


def _download_checked(t, panel, server, filename, dest):
    """Downloads one copy to dest and checks it against its recorded SHA-256."""
    c = client(t)
    sha = c.sha(panel, server, filename)
    if not sha:
        raise TargetError(f"{filename} has no checksum on '{t['name']}', so it can't be checked. Download it from "
                          f"the storage yourself if you need it.")
    c.download(panel, server, filename, dest)
    if _sha256_file(dest) != sha:
        os.remove(dest)
        raise TargetError(f"The copy of {filename} on '{t['name']}' is damaged (SHA-256 doesn't match).")
    return sha


def fetch(tid, server, filename, actor, panel=None, from_server=None):
    """Downloads a copy into this server's own backups (to restore from there). The copy may be
    of another server or panel; it's renamed to this server. Returns {file, existed}."""
    manager._check_server(server)
    t = get_target(tid)
    panel = panel or panel_id()
    from_server = from_server or server
    if not PANEL_ID_RE.fullmatch(panel) or not backups.NAME_RE.fullmatch(from_server):
        raise TargetError("No such copy.", 404)
    m = backups.FILE_RE.fullmatch(filename or "")
    if not m or m.group("server") != from_server:
        raise TargetError("No such copy.", 404)
    new_name = f"{server}-{m.group('stamp')}-{m.group('kind')}.tar.gz"
    final = os.path.join(backups.server_backup_dir(server), new_name)
    if os.path.exists(final):
        return {"file": new_name, "existed": True}
    os.makedirs(os.path.dirname(final), mode=0o700, exist_ok=True)
    partial = final + ".partial"
    try:
        _download_checked(t, panel, from_server, filename, partial)
        os.chmod(partial, 0o600)
        os.replace(partial, final)
    finally:
        if os.path.exists(partial):
            os.remove(partial)
    manager.write_log(f"[BACKUP] {actor}: downloaded the copy {filename} from '{t['name']}' into {server}'s backups"
                      + (f" as {new_name}" if new_name != filename else "") + ".")
    return {"file": new_name, "existed": False}


def delete_copy(tid, server, filename, actor):
    manager._check_server(server)
    t = get_target(tid)
    if not backups.FILE_RE.fullmatch(filename or ""):
        raise TargetError("No such copy.", 404)
    client(t).delete(panel_id(), server, filename)
    st = status(tid, server)
    if st and filename in (st.get("have") or []):
        _set_status(tid, server, have=[f for f in st["have"] if f != filename])
    manager.write_log(f"[BACKUP] {actor} deleted the copy {filename} of {server} from '{t['name']}'.")
    return {"status": "success"}


def browse(tid):
    """Every backup on a target, from every panel: for Import → From backup storage."""
    t = get_target(tid)
    out = [e for e in (_entry(p, s, f, size) for p, s, f, size in client(t).list()) if e]
    out.sort(key=lambda e: (e["created"], e["file"]), reverse=True)
    return {"panel_id": panel_id(), "copies": out}


def import_copy(tid, panel, server, filename, actor):
    """The machine died: makes a new server here from a copy, as a removed server brought back.
    The server binary is downloaded again (same Minecraft version and loader for Java when the
    backup records them). Returns restore_server()'s result."""
    from core import archive
    t = get_target(tid)
    if not PANEL_ID_RE.fullmatch(panel or "") or not backups.NAME_RE.fullmatch(server or ""):
        raise TargetError("No such copy.", 404)
    if not backups.FILE_RE.fullmatch(filename or "") or backups.FILE_RE.fullmatch(filename).group("server") != server:
        raise TargetError("No such copy.", 404)
    name = _free_name(server)
    os.makedirs(manager.SERVERS_ROOT, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".redstonepanel-offsite-", suffix=".tar.gz", dir=manager.SERVERS_ROOT)
    os.close(fd)
    staging = None
    try:
        _download_checked(t, panel, server, filename, tmp)
        staging, root = archive.unpack(tmp, manager.SERVERS_ROOT)
        edition = _edition_of(root)
        if edition == "bedrock" and not manager.bedrock_supported():
            raise TargetError(manager.bedrock_unsupported_message())
        if not manager._reserve_exact(name):
            raise TargetError(f"{name} was taken meanwhile; try again.", 409)
        try:
            dest = os.path.join(manager.SERVERS_ROOT, name)
            os.rename(root, dest)
            with open(os.path.join(dest, manager.REMOVED_INFO), "w") as f:
                json.dump({"edition": edition, "removed_at": int(time.time()),
                           "from_backup": f"{t['name']}: {panel}/{server}/{filename}"}, f)
        finally:
            manager._release_name(name)
    finally:
        os.remove(tmp) if os.path.exists(tmp) else None
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
    manager.write_log(f"[BACKUP] {actor}: unpacked {filename} from '{t['name']}' (panel {panel}) as {name}; "
                      f"setting it up as a server.")
    try:
        return manager.restore_server(name, same_version=True)
    except Exception as e:
        manager.write_log(f"[BACKUP] {name} is unpacked but couldn't be started: {e}. It's listed under Removed "
                          f"servers, where Restore tries again.")
        raise


def _edition_of(root):
    if os.path.isfile(os.path.join(root, "bedrock_server")):
        return "bedrock"
    if os.path.isfile(os.path.join(root, "server.jar")) or os.path.isfile(os.path.join(root, "eula.txt")) \
            or os.path.isfile(os.path.join(root, ".redstonepanel-server.json")):
        return "java"
    if os.path.isdir(os.path.join(root, "worlds")):
        return "bedrock"
    if os.path.isfile(os.path.join(root, "server.properties")):
        return "java"
    raise TargetError("This doesn't look like a Minecraft server backup.")


def _free_name(server):
    """server if nothing here uses that name, else server-2, server-3, …"""
    def free(n):
        return not os.path.lexists(os.path.join(manager.SERVERS_ROOT, n)) and not os.path.exists(manager._unit_path(n))
    if server.startswith("minecraft-") and manager.SERVICE_NAME_RE.match(server) and free(server):
        return server
    base = server if server.startswith("minecraft-") else "minecraft-" + server[len("minecraft"):].lstrip("-_.")
    for i in range(2, 100):
        n = f"{base}-{i}"
        if manager.SERVICE_NAME_RE.match(n) and free(n):
            return n
    raise TargetError("No free name for the imported server.")
