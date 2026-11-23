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
