"""Certificates in tests: the real `openssl` when the machine has it (there is nothing useful
to pretend about a certificate), else a failed command."""
import shutil

from core.platform.fake._result import done


def openssl(args, input=None, text=False, env=None):
    if shutil.which("openssl"):
        from core.platform.linux import certs
        return certs.openssl(args, input=input, text=text, env=env)
    return done(stdout="" if text else b"", stderr="openssl isn't installed" if text else b"openssl isn't installed",
                returncode=127)
