"""Certificates for the panel's HTTPS option (uses the openssl command, no extra packages).

Self-signed mode creates a small private CA plus a server certificate in <app>/tls.
The CA carries X.509 name constraints: it can only vouch for this machine's own names and
IPv4 addresses, so importing it on a PC can't be abused to impersonate any other website.
"""
import json
import os
import socket
import ssl
import subprocess
import tempfile

from core.settings import TLS_DIR
CA_KEY, CA_CRT = os.path.join(TLS_DIR, "ca.key"), os.path.join(TLS_DIR, "ca.crt")
SRV_KEY, SRV_CRT = os.path.join(TLS_DIR, "server.key"), os.path.join(TLS_DIR, "server.crt")
META = os.path.join(TLS_DIR, "meta.json")
SERVER_DAYS = 825
RENEW_WITHIN = 30 * 86400


class TLSError(RuntimeError):
    pass


def _run(args, stdin=None):
    r = subprocess.run(["openssl"] + args, capture_output=True, text=True, input=stdin)
    if r.returncode != 0:
        raise TLSError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout) else "openssl failed")
    return r.stdout


def identity():
    """The names/addresses the self-signed certificate should cover."""
    host = socket.gethostname().split(".")[0].lower()
    names = sorted({host, f"{host}.local", "localhost"})
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        out = ""
    ips = sorted({ip for ip in out.split() if ip.count(".") == 3} | {"127.0.0.1"})
    return {"names": names, "ips": ips}


def check_pair(cert, key):
    """None if cert+key load as a matching pair, else a readable error."""
    if not os.path.isfile(cert):
        return f"Certificate file not found: {cert}"
    if not os.path.isfile(key):
        return f"Key file not found: {key}"
    try:
        ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(cert, key)
    except (ssl.SSLError, OSError) as e:
        return f"Certificate and key don't load as a pair: {e}"
    return None


def _expiring(path):
    try:
        _run(["x509", "-checkend", str(RENEW_WITHIN), "-noout", "-in", path])
        return False
    except TLSError:
        return True


def ensure_self_signed(force=False):
    """Creates/renews the CA and server certificate when needed. Returns (cert, key) paths."""
    want = identity()
    try:
        with open(META) as f:
            have = json.load(f)
    except Exception:
        have = None
    new_ca = force or have != want or not (os.path.exists(CA_KEY) and os.path.exists(CA_CRT))
    new_srv = new_ca or not (os.path.exists(SRV_KEY) and os.path.exists(SRV_CRT)) or _expiring(SRV_CRT)
    if not new_srv:
        return SRV_CRT, SRV_KEY

    os.makedirs(TLS_DIR, exist_ok=True)
    os.chmod(TLS_DIR, 0o700)
    old_umask = os.umask(0o077)
    try:
        if new_ca:
            nc = ["critical"] + [f"permitted;DNS:{n}" for n in want["names"]] + \
                 [f"permitted;IP:{ip}/255.255.255.255" for ip in want["ips"]]
            _run(["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", CA_KEY])
            _run(["req", "-x509", "-new", "-key", CA_KEY, "-sha256", "-days", "3650",
                  "-subj", f"/CN=RedstonePanel Local CA ({want['names'][0]})",
                  "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                  "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                  "-addext", "nameConstraints=" + ",".join(nc),
                  "-out", CA_CRT])
        san = ",".join([f"DNS:{n}" for n in want["names"]] + [f"IP:{ip}" for ip in want["ips"]])
        with tempfile.TemporaryDirectory() as tmp:
            ext, csr = os.path.join(tmp, "ext.cnf"), os.path.join(tmp, "server.csr")
            with open(ext, "w") as f:
                f.write("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                        f"extendedKeyUsage=serverAuth\nsubjectAltName={san}\n")
            _run(["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", SRV_KEY])
            _run(["req", "-new", "-key", SRV_KEY, "-subj", f"/CN={want['names'][0]}", "-out", csr])
            _run(["x509", "-req", "-in", csr, "-CA", CA_CRT, "-CAkey", CA_KEY,
                  "-set_serial", "0x" + os.urandom(16).hex(), "-days", str(SERVER_DAYS),
                  "-sha256", "-extfile", ext, "-out", SRV_CRT])
        _run(["verify", "-CAfile", CA_CRT, SRV_CRT])
        with open(META, "w") as f:
            json.dump(want, f)
    finally:
        os.umask(old_umask)
    for p in (CA_CRT, SRV_CRT):
        os.chmod(p, 0o644)
    return SRV_CRT, SRV_KEY


def cert_info(path):
    """{subject, expires, sha256} for display, or None."""
    if not os.path.isfile(path):
        return None
    try:
        out = _run(["x509", "-in", path, "-noout", "-subject", "-enddate", "-fingerprint", "-sha256"])
    except TLSError:
        return None
    info = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        k = k.lower()
        if k == "subject":
            info["subject"] = v.strip()
        elif k == "notafter":
            info["expires"] = v.strip()
        elif "fingerprint" in k:
            info["sha256"] = v.strip()
    return info
