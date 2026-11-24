"""Typing into a game server's console on Windows.

The runner holds every game server's console and listens on a named pipe that only
administrators and the system can open (\\\\.\\pipe\\RedstonePanel.console.<server>). A line written
to the pipe is typed into the server. Java servers still use RCON for chat and commands, as on
Linux; the pipe is how a server is told to `stop` cleanly, and what Bedrock will use (1.23).
"""
import os
import subprocess

from core.platform.windows import _win

PROGRAM = "console"
ACCOUNT = "minecraft"
PIPE_PREFIX = "\\\\.\\pipe\\RedstonePanel.console."


def available():
    """True: the runner holds the console, nothing else needs installing."""
    return True


def program():
    return PROGRAM


def pipe(server):
    return PIPE_PREFIX + server


def send_command(server, text, program_path=PROGRAM):
    """A command line that types `text` and Enter into the server's console."""
    code = ("import sys;f=open(sys.argv[1],'wb',buffering=0);f.write((sys.argv[2]+'\\n').encode());f.close()")
    return [_win.python(), *_win.UTF8, "-c", code, pipe(server), text]


def send(server, text, timeout=None, run=None, find=True):
    """Types `text` and Enter into the server's console. Returns a CompletedProcess; returncode 1
    when the server isn't running (there is no console to type into)."""
    if run is not None:
        return run(send_command(server, text), capture_output=True, text=True)
    try:
        with open(pipe(server), "wb", buffering=0) as f:
            f.write((text.rstrip("\r\n") + "\n").encode("utf-8"))
    except OSError as e:
        return subprocess.CompletedProcess([], 1, "", str(e))
    return subprocess.CompletedProcess([], 0, "", "")


def start_command(server, folder, command="./bedrock_server", program_path="console"):
    """What a Bedrock server's unit runs. The runner holds the console itself, so this is just
    the program (bedrock_server.exe in the server's folder)."""
    name = os.path.basename(command.replace("/", os.sep)) or "bedrock_server"
    return os.path.join(folder, name if name.lower().endswith(".exe") else name + ".exe")
