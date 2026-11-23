"""A pretend console: what was typed is kept in fake.state.console."""
from core.platform import fake
from core.platform.fake._result import done

PROGRAM = "console"
ACCOUNT = "minecraft"


def available():
    return fake.state.console_installed


def program():
    return PROGRAM if fake.state.console_installed else None


def send_command(server, text, program_path=PROGRAM):
    return [program_path, server, text]


def send(server, text, timeout=None, run=None, find=True):
    if find and not fake.state.console_installed:
        return None
    running = fake.state.servers.get(server, {}).get("state") == "active"
    fake.record("console.send", server, text)
    if running:
        fake.state.console.append((server, text))
    return done(returncode=0 if running else 1)


def start_command(server, folder, command="./bedrock_server", program_path=PROGRAM):
    return f"{program_path} {server} {command}"
