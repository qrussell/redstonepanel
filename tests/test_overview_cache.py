"""1.24.1: a server created on a node from the master shows in the server list at once.

The master keeps each node's server list for OVERVIEW_TTL seconds. The list after a change asks
afresh (fresh=True), and a fetch that started before forget_cached() never puts its older answer
back."""
import os
import sys
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="rp-test-overview-")
os.environ.setdefault("RP_LOG_FILE", os.path.join(_TMP, "panel.log"))
os.environ.setdefault("RP_DATA_DIR", os.path.join(_TMP, "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import cluster, cluster_client as CC  # noqa: E402

NODE = {"id": "n-1", "name": "pc", "url": "https://10.0.0.5:8443", "last_ok": 10 ** 12}


class OverviewCache(unittest.TestCase):
    def setUp(self):
        CC._cache.clear()
        CC._forgotten.clear()
        self.lists = [[{"name": "minecraft-a"}]]
        p = [mock.patch.object(CC, "call_node", lambda node, method, path, *a, **k: list(self.lists[-1])),
             mock.patch.object(cluster, "load_nodes", lambda: [NODE]),
             mock.patch.object(cluster, "state", lambda: {"name": "Home", "epoch": 1})]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def names(self, fresh=False):
        ov = CC.overview(lambda: [], fresh=fresh)
        return [s["name"] for s in ov["nodes"][1]["servers"]]

    def test_fresh_asks_the_node_again(self):
        self.assertEqual(self.names(), ["minecraft-a"])
        self.lists.append([{"name": "minecraft-a"}, {"name": "minecraft-b"}])
        self.assertEqual(self.names(), ["minecraft-a"], "the 10-second copy")
        self.assertEqual(self.names(fresh=True), ["minecraft-a", "minecraft-b"])
        self.assertEqual(self.names(), ["minecraft-a", "minecraft-b"], "and the copy is the new list")

    def test_a_fetch_from_before_a_change_isnt_kept(self):
        def slow(node, method, path, *a, **k):
            answer = list(self.lists[-1])
            CC.forget_cached(node["id"])           # the server is created while this fetch runs
            self.lists.append([{"name": "minecraft-a"}, {"name": "minecraft-b"}])
            return answer
        with mock.patch.object(CC, "call_node", slow):
            self.assertEqual(self.names(), ["minecraft-a"])
        self.assertNotIn(NODE["id"], CC._cache, "the older answer wasn't cached")
        self.assertEqual(self.names(), ["minecraft-a", "minecraft-b"])


if __name__ == "__main__":
    unittest.main()
