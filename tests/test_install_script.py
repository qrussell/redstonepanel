"""install.sh's system check: which /etc/os-release contents it accepts, warns about or refuses.

Runs the check part of install.sh (from `system=` to the end of its os-release `case`) in sh with
the os-release fields set, so nothing is installed and no root is needed."""
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _check_block():
    with open(os.path.join(ROOT, "install.sh"), encoding="utf-8") as f:
        text = f.read()
    m = re.search(r'^system=.*?^esac$', text, re.M | re.S)
    assert m, "install.sh has no os-release check"
    return m.group(0)


def check(**fields):
    """-> (problem, warning) for an os-release with these fields."""
    script = "say() { printf 'WARN %s\\n' \"$*\"; }\n" + _check_block() + '\nprintf "PROBLEM %s\\n" "$problem"\n'
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    env.update(fields)
    out = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, check=True).stdout
    problem = out.rsplit("PROBLEM ", 1)[1].strip()
    warn = "\n".join(line[5:] for line in out.splitlines() if line.startswith("WARN "))
    return problem, warn


@unittest.skipUnless(shutil.which("sh"), "needs sh")
class InstallScriptSystemCheck(unittest.TestCase):
    def test_supported(self):
        for f in ({"ID": "ubuntu", "VERSION_ID": "24.04"}, {"ID": "ubuntu", "VERSION_ID": "26.04"},
                  {"ID": "ubuntu", "VERSION_ID": "26.10"}, {"ID": "debian", "VERSION_ID": "13"}):
            self.assertEqual(check(**f), ("", ""), f)

    def test_linux_mint_22(self):
        # /etc/os-release of Linux Mint 22.2 "Zara" (Ubuntu 24.04 noble underneath).
        for ver in ("22", "22.1", "22.2", "22.3"):
            self.assertEqual(check(ID="linuxmint", ID_LIKE="ubuntu debian", VERSION_ID=ver,
                                   UBUNTU_CODENAME="noble", PRETTY_NAME=f"Linux Mint {ver}"), ("", ""))

    def test_linux_mint_23(self):
        # Linux Mint 23 is built on Ubuntu 26.04 "resolute".
        self.assertEqual(check(ID="linuxmint", ID_LIKE="ubuntu debian", VERSION_ID="23",
                               UBUNTU_CODENAME="resolute", PRETTY_NAME="Linux Mint 23"), ("", ""))

    def test_linux_mint_21_refused(self):
        problem, _ = check(ID="linuxmint", ID_LIKE="ubuntu debian", VERSION_ID="21.3",
                           UBUNTU_CODENAME="jammy", PRETTY_NAME="Linux Mint 21.3")
        self.assertIn("Linux Mint 22 or newer", problem)
        self.assertIn("Linux Mint 21.3", problem)

    def test_other_mint_refused(self):
        # LMDE (Debian based) and anything without a known Ubuntu base.
        problem, _ = check(ID="linuxmint", ID_LIKE="debian", VERSION_ID="6",
                           DEBIAN_CODENAME="bookworm", PRETTY_NAME="LMDE 6 (faye)")
        self.assertIn("supports Linux Mint 22 and 23", problem)

    def test_old_ubuntu_warns(self):
        problem, warn = check(ID="ubuntu", VERSION_ID="25.10", PRETTY_NAME="Ubuntu 25.10")
        self.assertEqual(problem, "")
        self.assertIn("no longer gets security updates", warn)

    def test_refused(self):
        self.assertIn("Debian 13 or newer", check(ID="debian", VERSION_ID="12")[0])
        self.assertIn("32-bit", check(ID="raspbian", VERSION_ID="13")[0])
        self.assertIn("Linux Mint 22", check(ID="ubuntu", VERSION_ID="22.04", PRETTY_NAME="Ubuntu 22.04")[0])
        self.assertIn("supports Ubuntu", check(ID="fedora", VERSION_ID="41")[0])


if __name__ == "__main__":
    unittest.main()
