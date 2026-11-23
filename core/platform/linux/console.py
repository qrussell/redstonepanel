"""Typing into a game server's console on Linux.

Bedrock Dedicated Server has no RCON, so its unit runs it inside `screen` and commands are
"stuffed" into that session as the `minecraft` account. Java servers use RCON instead.
"""
import shutil
import subprocess

PROGRAM = "screen"
ACCOUNT = "minecraft"


def available():
    """True when the program that holds consoles is installed."""
    return bool(shutil.which(PROGRAM))


def program():
    return shutil.which(PROGRAM)


def send_command(server, text, program_path=PROGRAM):
    """The command line that types `text` and Enter into the server's console."""
    return ["sudo", "-u", ACCOUNT, program_path, "-S", server, "-p", "0", "-X", "stuff", text + "\r"]


def send(server, text, timeout=None, run=None, find=True):
    """Types `text` and Enter into the server's console. Returns the CompletedProcess, or None
    when there is no console program. find=False runs "screen" from PATH without looking for
    it first (the unit's stop hook, where PATH is systemd's)."""
    path = PROGRAM
    if find:
        path = program()
        if not path:
            return None
    kw = {"capture_output": True, "text": True}
    if timeout is not None:
        kw["timeout"] = timeout
    return (run or subprocess.run)(send_command(server, text, path), **kw)


def start_command(server, folder, command="./bedrock_server", program_path="/usr/bin/screen"):
    """What the server's unit runs so its console can be typed into and is logged to
    <folder>/server.log."""
    return f"{program_path} -c {folder}/screenrc -L -Logfile {folder}/server.log -DmS {server} {command}"
