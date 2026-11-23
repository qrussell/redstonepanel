"""Certificates on Linux: the `openssl` command."""
import subprocess


def openssl(args, input=None, text=False, env=None):
    """`openssl ARGS` -> CompletedProcess. text=False gives bytes (keys and certificates)."""
    kw = {"capture_output": True, "input": input}
    if text:
        kw["text"] = True
    if env is not None:
        kw["env"] = env
    return subprocess.run(["openssl"] + list(args), **kw)
