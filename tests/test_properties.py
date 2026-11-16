"""Tests for core/properties.py. Run: python -m unittest discover -s tests

The expected values in JAVA_CASES were produced by java.util.Properties.load() (Java 25),
which is how Minecraft Java Edition reads server.properties.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import properties as P  # noqa: E402

BS = "\\"   # one backslash

# (file content, what Java reads from it)
JAVA_CASES = [
    ('rcon.password=simplelowercase\n',
     {'rcon.password': 'simplelowercase'}),
    ('rcon.password=Mixed1Case-_x\n',
     {'rcon.password': 'Mixed1Case-_x'}),
    ('rcon.password=Ab1\\:c\\=d\\#e\\!f\n',
     {'rcon.password': 'Ab1:c=d#e!f'}),
    ('rcon.password=Ab1:c=d#e!f\n',
     {'rcon.password': 'Ab1:c=d#e!f'}),
    ('rcon.password=pa$$w0rd&!\n',
     {'rcon.password': 'pa$$w0rd&!'}),
    ('rcon.password=back\\\\slash\n',
     {'rcon.password': 'back\\slash'}),
    ('rcon.password=  spaced  \n',
     {'rcon.password': 'spaced  '}),
    ('rcon.password=\\ lead\n',
     {'rcon.password': ' lead'}),
    ('enable-rcon=true \n',
     {'enable-rcon': 'true '}),
    ('rcon.password=abc\\\nenable-rcon=true\nrcon.port=25575\n',
     {'rcon.password': 'abcenable-rcon=true', 'rcon.port': '25575'}),
    ('rcon.password=abc\\\\\nenable-rcon=true\n',
     {'enable-rcon': 'true', 'rcon.password': 'abc\\'}),
    ('  # comment\n! bang comment\nkey : value with spaces\nk2 v2\nk3\t=\tv3\n',
     {'k2': 'v2', 'k3': 'v3', 'key': 'value with spaces'}),
    ('a=1\r\nb=2\rc=3\n',
     {'a': '1', 'b': '2', 'c': '3'}),
    ('motd=\\u00a7aGreen \\u00e9t\\u00e9\n',
     {'motd': '\xa7aGreen \xe9t\xe9'}),
    ('motd=h\xe9llo w\xf6rld ✓\n',
     {'motd': 'h\xe9llo w\xf6rld ✓'}),
    ('dup=1\ndup=2\n',
     {'dup': '2'}),
    ('level-type=minecraft\\:flat\n',
     {'level-type': 'minecraft:flat'}),
    ('x=\\t\\n\\r\\f\\q\n',
     {'x': '\t\n\r\x0cq'}),
    ('multi=a\\\n   b\\\n   c\n',
     {'multi': 'abc'}),
    ('k\\ ey=v\n',
     {'k ey': 'v'}),
    ('trail=x\\',
     {'trail': 'x'}),
    ('empty=\nnosep\n',
     {'empty': '', 'nosep': ''}),
    ('=noKey\n',
     {'': 'noKey'}),
    ('a\\=b=c\n',
     {'a=b': 'c'}),
    ('key=value # not a comment\n',
     {'key': 'value # not a comment'}),
    ('cont=x\\\n# not a comment\n',
     {'cont': 'x# not a comment'}),
    ('   \n\t\nk=v\n',
     {'k': 'v'}),
]

# Values that must survive a panel write followed by a (Java-compatible) read
TRICKY = ["Ab1:c=d#e!f" + BS + "g h ", "  leading and trailing  ", "tab\there\nnewline",
          "ends with backslash" + BS, "h\xe9llo ✓ \xa7a", "#hash", "!bang", "",
          "minecraft:flat", "a=b=c", "pa$$w0rd&!"]


class ParseLikeJava(unittest.TestCase):
    def test_matches_java_properties(self):
        for content, expected in JAVA_CASES:
            with self.subTest(content=content):
                self.assertEqual(P.parse(content), expected)

    def test_bedrock_is_plain(self):
        self.assertEqual(P.parse("server-name=My: Server!\n", java=False), {"server-name": "My: Server!"})


class Write(unittest.TestCase):
    def test_round_trip(self):
        for value in TRICKY:
            with self.subTest(value=value):
                text = P.update("enable-rcon=true\n", {"rcon.password": value})
                self.assertEqual(P.parse(text)["rcon.password"], value)
                self.assertEqual(P.parse(text)["enable-rcon"], "true")

    def test_password_saved_by_minecraft_is_read_unescaped(self):
        saved = "rcon.password=Ab1" + BS + ":c" + BS + "=d" + BS + "#e" + BS + "!f\n"
        self.assertEqual(P.parse(saved)["rcon.password"], "Ab1:c=d#e!f")

    def test_unchanged_lines_keep_their_text(self):
        content = "#comment\nmotd=keep me" + BS + ": as is\nrcon.port=25575\n"
        self.assertEqual(P.update(content, {"motd": "keep me: as is", "rcon.port": "25575"}), content)

    def test_continued_and_repeated_keys_are_replaced(self):
        content = "rcon.password=old" + BS + "\n  continued\nenable-rcon=true\ndup=1\ndup=2\n"
        out = P.update(content, {"rcon.password": "new", "dup": "3"})
        self.assertEqual(out, "rcon.password=new\nenable-rcon=true\ndup=3\n")

    def test_new_keys_are_appended_escaped(self):
        self.assertEqual(P.update("a=1\n", {"b": "x:y"}), "a=1\nb=x" + BS + ":y\n")


if __name__ == "__main__":
    unittest.main()
