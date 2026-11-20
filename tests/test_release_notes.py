"""Release notes show the screenshots of their own version: an image link names the release's
tag, not main, or an old release would show today's UI (as 1.11-1.16.1 did after the 1.16.2
redesign). Run: python -m unittest discover -s tests"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTES = os.path.join(ROOT, "release-notes")
IMAGE = re.compile(r"qrussell/redstonepanel/(?:raw/)?([^/\s\"')]+)/docs/guide/images/")


class Screenshots(unittest.TestCase):
    def test_images_are_pinned_to_the_release_tag(self):
        for name in sorted(os.listdir(NOTES)):
            if not name.endswith(".md"):
                continue
            with open(os.path.join(NOTES, name), encoding="utf-8") as f:
                refs = IMAGE.findall(f.read())
            for ref in refs:
                self.assertEqual(ref, name[:-3], f"{name}: screenshot linked at {ref}, not the release's tag")


if __name__ == "__main__":
    unittest.main()
