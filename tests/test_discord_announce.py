"""packaging/discord_announce.py: the release announcement built from release notes."""

import importlib.util
import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "discord_announce", os.path.join(ROOT, "packaging", "discord_announce.py"))
da = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(da)

NOTES = """A short summary
on two lines.

## New

- **Sleep mode.** Servers stop when empty.
- **Join page Start button:** players start it.

## Fixed

- **Update now works.** Details.

## Upgrading an existing panel

- **Not listed.** This section is left out.
"""


class DiscordAnnounceTests(unittest.TestCase):
    def test_message_from_notes(self):
        msg = da.build_message("v1.26.0", NOTES)
        self.assertIn("1.26.0", msg["content"])
        embed = msg["embeds"][0]
        self.assertEqual(embed["url"], "https://github.com/qrussell/redstonepanel/releases/tag/v1.26.0")
        d = embed["description"]
        self.assertTrue(d.startswith("A short summary on two lines."))
        self.assertIn("• Sleep mode\n• Join page Start button", d)
        self.assertIn("**Fixed**\n• Update now works", d)
        self.assertNotIn("Not listed", d)
        self.assertEqual(msg["allowed_mentions"], {"parse": []})

    def test_long_notes_fit_discord(self):
        notes = "Summary.\n\n## New\n\n" + "".join(f"- **Item {i} {'x' * 80}**\n" for i in range(100))
        self.assertLessEqual(len(da.build_message("v1.0.0", notes)["embeds"][0]["description"]), 4096)

    def test_every_release_notes_file_builds(self):
        folder = os.path.join(ROOT, "release-notes")
        for name in os.listdir(folder):
            if name.endswith(".md"):
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    msg = da.build_message(name[:-3], f.read())
                self.assertLessEqual(len(msg["embeds"][0]["description"]), 4096, name)

    def test_no_webhook_passes_quietly(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"DISCORD_RELEASE_WEBHOOK": ""}), \
                mock.patch.object(da.urllib.request, "urlopen") as urlopen, redirect_stdout(out):
            self.assertEqual(da.main(["x", "v1.25.0"]), 0)
        urlopen.assert_not_called()
        self.assertIn("not posting", out.getvalue())

    def test_bad_tag_or_url_never_posts(self):
        with mock.patch.object(da.urllib.request, "urlopen") as urlopen, redirect_stdout(io.StringIO()):
            self.assertEqual(da.main(["x", "main"]), 2)
            with mock.patch.dict(os.environ, {"DISCORD_RELEASE_WEBHOOK": "https://example.com/hook"}):
                self.assertEqual(da.main(["x", "v1.25.0"]), 0)
        urlopen.assert_not_called()

    def test_posts_and_never_fails(self):
        hook = "https://discord.com/api/webhooks/1/abc"
        with mock.patch.dict(os.environ, {"DISCORD_RELEASE_WEBHOOK": hook}), \
                mock.patch.object(da.urllib.request, "urlopen") as urlopen, redirect_stdout(io.StringIO()) as out:
            urlopen.return_value.__enter__.return_value.status = 200
            self.assertEqual(da.main(["x", "v1.25.0"]), 0)
            req = urlopen.call_args[0][0]
            self.assertEqual(req.full_url, hook + "?wait=true")
            urlopen.side_effect = OSError("no network")
            self.assertEqual(da.main(["x", "v1.25.0"]), 0)
        self.assertNotIn(hook, out.getvalue())


if __name__ == "__main__":
    unittest.main()
