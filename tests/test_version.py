"""Tests for core/version.py. Run: python -m unittest discover -s tests"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core import version as V  # noqa: E402


class FromChangelog(unittest.TestCase):
    def test_entry(self):
        self.assertEqual(V.from_changelog("redstonepanel (1.4.0) noble; urgency=medium\n\n  * x\n"), "1.4.0")

    def test_bom_and_debian_revision(self):
        self.assertEqual(V.from_changelog("﻿redstonepanel (1.4.1-1) noble; urgency=low\n"), "1.4.1-1")

    def test_not_an_entry(self):
        for text in ("", None, "  * just a bullet\n", "redstonepanel 1.4.0\n"):
            self.assertIsNone(V.from_changelog(text), text)

    def test_source_checkout_matches_changelog(self):
        with open(os.path.join(ROOT, "debian", "changelog"), encoding="utf-8") as f:
            expected = V.from_changelog(f.read())
        self.assertTrue(expected)
        self.assertEqual(V._source(), expected)


if __name__ == "__main__":
    unittest.main()
