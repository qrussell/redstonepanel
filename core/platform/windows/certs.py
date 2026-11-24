"""Certificates on Windows: the `cryptography` library the installer carries.

Windows has no `openssl` command. The panel's certificate code spells its work out as openssl
command lines (core/tls.py), so this module does what those command lines do, with the same
files in and out and the same words printed. It understands the commands the panel uses for
its own HTTPS certificate; anything else is refused with a message, as a failed command.

    ecparam -name prime256v1 -genkey -noout -out KEY
    req -x509 -new -key KEY -sha256 -days N -subj /CN=... -addext ... -out CRT
    req -new -key KEY -subj /CN=... -out CSR
    x509 -req -in CSR -CA CRT -CAkey KEY -set_serial 0x.. -days N -sha256 -extfile FILE -out CRT
    x509 -in CRT -noout [-subject] [-enddate] [-fingerprint -sha256] [-checkend SECONDS]
    verify -CAfile CA CRT
"""
import datetime
import ipaddress
import subprocess


class _Refused(Exception):
    pass


def _result(args, code, out, err, text):
    if not text:
        out, err = out.encode(), err.encode()
    return subprocess.CompletedProcess(["openssl"] + list(args), code, out, err)


def openssl(args, input=None, text=False, env=None):
    """Does what `openssl ARGS` does -> CompletedProcess. text=False gives bytes."""
    args = list(args)
    try:
        return _result(args, 0, _run(args, input) or "", "", text)
    except _Refused as e:
        return _result(args, 1, "", str(e), text)
    except ImportError:
        return _result(args, 1, "", "The cryptography library is missing; reinstall RedstonePanel.", text)
    except (OSError, ValueError, TypeError, KeyError, IndexError) as e:
        return _result(args, 1, "", f"{type(e).__name__}: {e}", text)


def _options(args, flags=()):
    """["-in", "x", "-noout"] -> {"in": "x", "noout": True}; an option given twice keeps a list."""
    opts, rest, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            key = a[1:]
            if key in flags or i + 1 >= len(args):
                opts[key] = True
            else:
                i += 1
                if key in opts:
                    opts[key] = (opts[key] if isinstance(opts[key], list) else [opts[key]]) + [args[i]]
                else:
                    opts[key] = args[i]
        else:
            rest.append(a)
        i += 1
    return opts, rest


def _name(subj):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    oids = {"CN": NameOID.COMMON_NAME, "O": NameOID.ORGANIZATION_NAME, "OU": NameOID.ORGANIZATIONAL_UNIT_NAME,
            "C": NameOID.COUNTRY_NAME}
    attrs = []
    for part in (subj or "").strip("/").split("/"):
        k, _, v = part.partition("=")
        if k in oids and v:
            attrs.append(x509.NameAttribute(oids[k], v))
    return x509.Name(attrs)


def _general_name(text):
    from cryptography import x509
    kind, _, value = text.partition(":")
    if kind == "DNS":
        return x509.DNSName(value)
    if kind == "IP":
        if "/" in value:
            return x509.IPAddress(ipaddress.ip_network(value, strict=False))
        return x509.IPAddress(ipaddress.ip_address(value))
    raise _Refused(f"Unsupported name '{text}'")


def _extension(line):
    """"keyUsage=critical,keyCertSign,cRLSign" -> (extension, critical)."""
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID
    name, _, value = line.partition("=")
    name = name.strip()
    words = [w.strip() for w in value.split(",") if w.strip()]
    critical = "critical" in words
    words = [w for w in words if w != "critical"]
    if name == "basicConstraints":
        ca = "CA:TRUE" in words
        path = next((int(w.split(":")[1]) for w in words if w.startswith("pathlen:")), None)
        return x509.BasicConstraints(ca=ca, path_length=path if ca else None), critical
    if name == "keyUsage":
        return x509.KeyUsage(digital_signature="digitalSignature" in words, content_commitment=False,
                             key_encipherment="keyEncipherment" in words, data_encipherment=False,
                             key_agreement="keyAgreement" in words, key_cert_sign="keyCertSign" in words,
                             crl_sign="cRLSign" in words, encipher_only=False, decipher_only=False), critical
    if name == "extendedKeyUsage":
        oids = {"serverAuth": ExtendedKeyUsageOID.SERVER_AUTH, "clientAuth": ExtendedKeyUsageOID.CLIENT_AUTH}
        return x509.ExtendedKeyUsage([oids[w] for w in words]), critical
    if name == "subjectAltName":
        return x509.SubjectAlternativeName([_general_name(w) for w in words]), critical
    if name == "nameConstraints":
        permitted = [_general_name(w.split(";", 1)[1]) for w in words if w.startswith("permitted;")]
        excluded = [_general_name(w.split(";", 1)[1]) for w in words if w.startswith("excluded;")]
        return x509.NameConstraints(permitted or None, excluded or None), critical
    raise _Refused(f"Unsupported extension '{name}'")


def _load_key(path):
    from cryptography.hazmat.primitives import serialization
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), None)


def _load_cert(path):
    from cryptography import x509
    with open(path, "rb") as f:
        return x509.load_pem_x509_certificate(f.read())


def _write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def _not_after(cert):
    t = getattr(cert, "not_valid_after_utc", None)
    return t if t is not None else cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)


def _run(args, stdin=None):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    PEM = serialization.Encoding.PEM
    cmd, rest = args[0], args[1:]
    now = datetime.datetime.now(datetime.timezone.utc)

    if cmd == "ecparam":
        opts, _ = _options(rest, flags=("genkey", "noout"))
        if opts.get("name") != "prime256v1" or not opts.get("genkey") or not opts.get("out"):
            raise _Refused("Only `ecparam -name prime256v1 -genkey -noout -out FILE` is supported.")
        key = ec.generate_private_key(ec.SECP256R1())
        _write(opts["out"], key.private_bytes(PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                              serialization.NoEncryption()))
        return ""

    if cmd == "req":
        opts, _ = _options(rest, flags=("x509", "new", "sha256", "noout", "verify"))
        if not opts.get("new") or not opts.get("key"):
            raise _Refused("Only `req -new -key KEY ...` is supported.")
        key = _load_key(opts["key"])
        subject = _name(opts.get("subj"))
        if opts.get("x509"):
            b = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
                 .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
                 .not_valid_after(now + datetime.timedelta(days=int(opts.get("days", 30)))))
            ext = opts.get("addext") or []
            for line in ([ext] if isinstance(ext, str) else ext):
                e, critical = _extension(line)
                b = b.add_extension(e, critical=critical)
            b = b.add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            data = b.sign(key, hashes.SHA256()).public_bytes(PEM)
        else:
            data = x509.CertificateSigningRequestBuilder().subject_name(subject).sign(key, hashes.SHA256()) \
                .public_bytes(PEM)
        if opts.get("out"):
            _write(opts["out"], data)
            return ""
        return data.decode()

    if cmd == "x509":
        opts, _ = _options(rest, flags=("req", "noout", "sha256", "subject", "enddate", "fingerprint"))
        if opts.get("req"):
            with open(opts["in"], "rb") as f:
                csr = x509.load_pem_x509_csr(f.read())
            if not csr.is_signature_valid:
                raise _Refused("The request's signature is wrong.")
            ca, ca_key = _load_cert(opts["CA"]), _load_key(opts["CAkey"])
            serial = int(str(opts.get("set_serial", "0")), 0) or x509.random_serial_number()
            b = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(ca.subject)
                 .public_key(csr.public_key()).serial_number(serial)
                 .not_valid_before(now - datetime.timedelta(minutes=5))
                 .not_valid_after(now + datetime.timedelta(days=int(opts.get("days", 30)))))
            if opts.get("extfile"):
                with open(opts["extfile"], encoding="utf-8") as f:
                    for line in f.read().splitlines():
                        if "=" in line and not line.lstrip().startswith(("#", "[")):
                            e, critical = _extension(line)
                            b = b.add_extension(e, critical=critical)
            b = b.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()), critical=False)
            _write(opts["out"], b.sign(ca_key, hashes.SHA256()).public_bytes(PEM))
            return ""
        cert = _load_cert(opts["in"])
        if "checkend" in opts:
            if _not_after(cert) <= now + datetime.timedelta(seconds=int(opts["checkend"])):
                raise _Refused("Certificate will expire")
            return "Certificate will not expire\n"
        out = []
        if opts.get("subject"):
            out.append("subject=" + ", ".join(f"{a.rfc4514_attribute_name} = {a.value}" for a in cert.subject))
        if opts.get("enddate"):
            t = _not_after(cert)
            out.append(f"notAfter={t:%b} {t.day:2d} {t:%H:%M:%S %Y} GMT")
        if opts.get("fingerprint"):
            fp = cert.fingerprint(hashes.SHA256()).hex().upper()
            out.append("sha256 Fingerprint=" + ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)))
        if not out:
            raise _Refused("Nothing asked of the certificate that the panel does on Windows.")
        return "\n".join(out) + "\n"

    if cmd == "verify":
        opts, files = _options(rest)
        ca = _load_cert(opts["CAfile"])
        for path in files:
            cert = _load_cert(path)
            if cert.issuer != ca.subject:
                raise _Refused(f"{path}: not issued by this certificate authority")
            ca.public_key().verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm))
            if _not_after(cert) <= now:
                raise _Refused(f"{path}: certificate has expired")
        return "".join(f"{p}: OK\n" for p in files)

    raise _Refused(f"`openssl {cmd}` isn't something the panel does on Windows yet (clusters come in 1.24).")
