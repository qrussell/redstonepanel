"""A pretend machine."""
import json

from core.platform import fake

GAME_ACCOUNT = "minecraft"


def addresses(timeout=None):
    return list(fake.state.addresses)


def interfaces_json(timeout=10):
    return json.dumps([{"ifname": "eth0", "addr_info": [{"local": a, "prefixlen": 24}]} for a in fake.state.addresses])


def whoami():
    return "uid=0(root) gid=0(root) groups=0(root)"


def account_exists(name=GAME_ACCOUNT):
    return name in fake.state.accounts


def ensure_game_account(home):
    if GAME_ACCOUNT not in fake.state.accounts:
        fake.record("system.ensure_game_account", home)
        fake.state.accounts.add(GAME_ACCOUNT)


def give_to_game_account(path):
    fake.record("system.give_to_game_account", path)
    fake.state.owned.append(path)
    return 0


def java_version_text(java="java"):
    return 'openjdk version "25" 2025-09-16\n'


def delete_tree(path):
    """Really deletes (tests work in temp folders), and remembers that it did."""
    import shutil
    fake.record("system.delete_tree", path)
    fake.state.deleted.append(path)
    shutil.rmtree(path, ignore_errors=True)
    return 0


def make_executable(path):
    fake.record("system.make_executable", path)
    return 0


def tail(path, lines=3000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-int(lines):]), ""
    except OSError as e:
        return "", str(e)


def is_admin():
    return False


def as_game_account(cmd):
    return cmd


def take_for_game_account(path):
    fake.state.owned.append(path)
    return 0


def run_program(cmd, cwd=None, env=None, timeout=None):
    """Programs handed to the panel are really run (tests bring their own stand-ins)."""
    import subprocess
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, errors="replace", timeout=timeout)


def copy_into(source, dest, wrap=None):
    import shutil
    from core.platform.fake._result import done
    fake.record("system.copy_into", source, dest)
    shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)
    return done()


def remove_files(folder, names, wrap=None):
    import os
    from core.platform.fake._result import done
    fake.record("system.remove_files", folder, tuple(names))
    for name in names:
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass
    return done()


def can_run_as_game_account():
    return True


def lock(fd, wait=False):
    if fd in fake.state.locks:
        raise BlockingIOError("locked")
    fake.state.locks.add(fd)


def unlock(fd):
    fake.state.locks.discard(fd)


SYSTEM_ACCOUNTS = True
ACCOUNT_KIND = "Linux"


def check_system_password(username, password):
    return fake.state.passwords.get(username) == password


def system_account_exists(username):
    return username in fake.state.passwords


def system_account_allowed(username, groups=("sudo",)):
    return username in fake.state.passwords


def machine_file(name, root=None):
    return fake.state.machine_files.get(name, "")


def kernel():
    return "6.8.0-fake"


def load_average():
    return [0.0, 0.0, 0.0]


def filesystem(path):
    return {"device": 1, "mount": "/", "fstype": "ext4", "total": 100 << 30, "used": 10 << 30, "free": 90 << 30}


def root_path():
    return "/"


def process_alive(pid):
    return pid in fake.state.processes


def secure_folder(path):
    fake.record("system.secure_folder", path)
    return 0


def fs():
    import os
    return os


def hint(what, *args):
    if what == "firewall-name":
        return "ufw"
    if what == "allow-ports":
        return " && ".join(f"sudo ufw allow {a}" for a in args)
    if what == "close-ports":
        return " && ".join(f"sudo ufw delete allow {a}" for a in args)
    if what == "install-package":
        return f"sudo apt install {' '.join(args)}"
    return f"({what} {' '.join(str(a) for a in args)})".replace(" )", ")")


def check_servers_folder(folder):
    import os
    fake.record("system.check_servers_folder", folder)
    if not os.path.isabs(folder) or " " in folder:
        return None, "Give the full path of a folder, for example /srv/minecraft."
    if folder in getattr(fake.state, "bad_servers_folders", ()):
        return None, f"{folder} belongs to the system or to the panel itself; choose a folder somewhere else."
    return os.path.normpath(folder), ""


def prepare_servers_folder(folder):
    import os
    fake.record("system.prepare_servers_folder", folder)
    os.makedirs(folder, exist_ok=True)
    return ""


def servers_drives():
    return list(getattr(fake.state, "servers_drives", []))


def found_folder_problem(folder):
    import re
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", folder or ""):
        return f"The folder's path, {folder}, has a space or another character the panel can't put in a service."
    return getattr(fake.state, "found_folder_problem", "")


def game_programs(names=("java", "bedrock_server")):
    """What the test put in fake.state.game_programs; one started by a unit runs while that unit is active."""
    out = []
    for p in getattr(fake.state, "game_programs", []):
        unit = p.get("service")
        if unit and fake.state.units.get(unit, {}).get("state", "active") != "active":
            continue
        out.append(dict(p))
    return out


def owner_of(path):
    return getattr(fake.state, "owners", {}).get(path, "1000:1000")


def give_back(path, owner):
    from core.platform.fake._result import done
    fake.record("system.give_back", path, owner)
    return done()
