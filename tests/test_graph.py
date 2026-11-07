"""记忆图谱的构图逻辑。

不需要 astrbot：图是从记忆库直接长出来的，给一个真的 SQLite 存储就能测。
"""

from __future__ import annotations

import itertools
import tempfile
import unittest
from pathlib import Path

from mind import graph
from mind.memory.model import Memory
from mind.memory.store import MemoryStore


#: 整份测试共用一个临时目录，退出时自动清掉（省得刷一堆 ResourceWarning）
_TMP = tempfile.mkdtemp(prefix="ai_mind_graph_")


def make_store(entries) -> MemoryStore:
    import itertools

    counter = next(_COUNTER)
    store = MemoryStore(Path(_TMP) / ("mind_%d.db" % counter))
    for item in entries:
        store.add(Memory(**item))
    return store


_COUNTER = itertools.count()


BASE = dict(session_id="aiocqhttp:FriendMessage:1", owner_id="1000000001", importance=0.6)


class BuildGraphTest(unittest.TestCase):
    def test_empty_store_gives_empty_graph(self) -> None:
        store = make_store([])
        result = graph.build_graph(store)
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["edges"], [])
        self.assertEqual(result["stats"]["memories_total"], 0)

    def test_nodes_and_edges(self) -> None:
        store = make_store([
            dict(content="他喜欢喝冰美式", keywords=("冰美式", "咖啡"), kind="preference", **BASE),
            dict(content="他常去楼下那家咖啡店", keywords=("咖啡", "咖啡店"), kind="fact", **BASE),
        ])
        result = graph.build_graph(store)
        kinds = {}
        for node in result["nodes"]:
            kinds[node["type"]] = kinds.get(node["type"], 0) + 1
        self.assertEqual(kinds.get("memory"), 2)
        self.assertEqual(kinds.get("keyword"), 3)
        self.assertEqual(kinds.get("user"), 1)
        self.assertEqual(kinds.get("session"), 1)
        self.assertTrue(any(edge["kind"] == "has" for edge in result["edges"]))
        self.assertTrue(any(edge["kind"] == "about" for edge in result["edges"]))
        self.assertTrue(any(edge["kind"] == "in" for edge in result["edges"]))

    def test_shared_keyword_clusters_memories(self) -> None:
        """共享关键词的记忆要落到同一簇里，不共享的各成一簇。"""
        store = make_store([
            dict(content="他喜欢喝冰美式", keywords=("咖啡", "冰美式"), kind="preference", **BASE),
            dict(content="他常去咖啡店", keywords=("咖啡", "咖啡店"), kind="fact", **BASE),
            dict(content="他养了一只猫", keywords=("猫", "宠物"), kind="fact", **BASE),
        ])
        result = graph.build_graph(store)
        cluster_of = {node["id"]: node["cluster"] for node in result["nodes"] if node["type"] == "memory"}
        ids = sorted(cluster_of)
        self.assertEqual(cluster_of[ids[0]], cluster_of[ids[1]], "共享关键词的该在同一簇")
        self.assertNotEqual(cluster_of[ids[0]], cluster_of[ids[2]])
        self.assertGreaterEqual(result["stats"]["clusters"], 2)

    def test_clusters_are_numbered_by_size(self) -> None:
        store = make_store([
            dict(content="他喜欢喝冰美式", keywords=("咖啡", "冰美式"), kind="preference", **BASE),
            dict(content="他常去咖啡店", keywords=("咖啡", "咖啡店"), kind="fact", **BASE),
            dict(content="他养了一只猫", keywords=("猫",), kind="fact", **BASE),
        ])
        result = graph.build_graph(store)
        self.assertEqual(result["clusters"][0]["name"], "C/01")
        self.assertGreaterEqual(result["clusters"][0]["size"], result["clusters"][-1]["size"])
        self.assertIn("咖啡", result["clusters"][0]["keywords"])

    def test_high_fanout_keyword_does_not_glue_everything(self) -> None:
        """一个被所有记忆共用的高频词不该把整张图拉成一坨。"""
        entries = [
            dict(content="第 %d 条记忆内容" % index, keywords=("测试", "词%d" % index),
                 kind="fact", **BASE)
            for index in range(graph.MAX_KEYWORD_FANOUT + 6)
        ]
        store = make_store(entries)
        result = graph.build_graph(store)
        self.assertGreater(result["stats"]["clusters"], 1, "高频词被当成桥了")

    def test_pinned_only_filter(self) -> None:
        store = make_store([
            dict(content="常驻的那一条记忆", keywords=("常驻",), kind="fact", pinned=True, **BASE),
            dict(content="普通的那一条记忆", keywords=("普通",), kind="fact", **BASE),
        ])
        result = graph.build_graph(store, pinned_only=True)
        contents = [node["data"]["content"] for node in result["nodes"] if node["type"] == "memory"]
        self.assertEqual(contents, ["常驻的那一条记忆"])

    def test_kind_filter(self) -> None:
        store = make_store([
            dict(content="他喜欢喝冰美式", keywords=("咖啡",), kind="preference", **BASE),
            dict(content="他养了一只猫", keywords=("猫",), kind="fact", **BASE),
        ])
        result = graph.build_graph(store, kinds=["preference"])
        contents = [node["data"]["content"] for node in result["nodes"] if node["type"] == "memory"]
        self.assertEqual(contents, ["他喜欢喝冰美式"])

    def test_min_importance_filter(self) -> None:
        store = make_store([
            dict(content="重要的一条记忆", keywords=("重要",), kind="fact",
                 session_id="s", owner_id="u", importance=0.9),
            dict(content="不重要的一条记忆", keywords=("不重要",), kind="fact",
                 session_id="s", owner_id="u", importance=0.1),
        ])
        result = graph.build_graph(store, min_importance=0.5)
        contents = [node["data"]["content"] for node in result["nodes"] if node["type"] == "memory"]
        self.assertEqual(contents, ["重要的一条记忆"])

    def test_memory_node_carries_the_details(self) -> None:
        store = make_store([
            dict(content="他喜欢喝冰美式", keywords=("咖啡",), kind="preference",
                 session_id="s1", owner_id="u1", importance=0.8, pinned=True),
        ])
        node = [n for n in graph.build_graph(store)["nodes"] if n["type"] == "memory"][0]
        self.assertEqual(node["data"]["kind"], "preference")
        self.assertEqual(node["data"]["owner"], "u1")
        self.assertTrue(node["data"]["pinned"])
        self.assertIn("冰美式", node["data"]["content"])

    def test_stats_are_consistent(self) -> None:
        store = make_store([
            dict(content="他喜欢喝冰美式", keywords=("咖啡",), kind="preference", **BASE),
            dict(content="他养了一只猫", keywords=("猫",), kind="fact", **BASE),
        ])
        stats = graph.build_graph(store)["stats"]
        self.assertEqual(stats["memories_total"], 2)
        self.assertEqual(stats["memories_shown"], 2)
        self.assertEqual(stats["sessions"], 1)
        self.assertEqual(stats["nodes"], len(graph.build_graph(store)["nodes"]))

    def test_limit_is_clamped(self) -> None:
        store = make_store([
            dict(content="第 %d 条记忆内容" % index, keywords=("词%d" % index,), kind="fact", **BASE)
            for index in range(5)
        ])
        result = graph.build_graph(store, limit=1)
        self.assertGreaterEqual(result["filters"]["limit"], 10)

    def test_session_filter_is_applied_before_limit(self) -> None:
        """会话条件下推到 SQL —— 这条守着「图谱跟着顶部会话选择器走」。

        以前 `query_page` 不带 session_id，LIMIT 会先按**全局**取最新 260 条，
        再在 Python 里过滤会话：只要另一个会话更新得更勤，选中会话的记忆
        整个被截掉，图上就是一张空图 —— 表现成「图谱没跟会话联动」。
        """
        chosen = [
            dict(content="A 会话第 %03d 条" % index, keywords=("a%d" % index,),
                 kind="fact", session_id="session-a", owner_id="1",
                 created_at=1000 + index, updated_at=1000 + index)
            for index in range(200)
        ]
        busier = [
            dict(content="B 会话第 %03d 条" % index, keywords=("b%d" % index,),
                 kind="fact", session_id="session-b", owner_id="9",
                 created_at=9000 + index, updated_at=9000 + index)
            for index in range(60)
        ]
        store = make_store(chosen + busier)

        result = graph.build_graph(store, session_id="session-a", limit=50)
        contents = [n["data"]["content"] for n in result["nodes"] if n["type"] == "memory"]
        self.assertTrue(contents, "选中会话的记忆被 limit 截没了 —— 会话条件下推没生效")
        self.assertTrue(all(c.startswith("A 会话") for c in contents), contents[:3])
        self.assertEqual(len(contents), 50)

        other = graph.build_graph(store, session_id="session-b", limit=50)
        other_contents = [n["data"]["content"] for n in other["nodes"]
                          if n["type"] == "memory"]
        self.assertTrue(other_contents)
        self.assertTrue(all(c.startswith("B 会话") for c in other_contents))

    def test_broken_store_returns_empty_graph(self) -> None:
        class Broken:
            def query_page(self, **_kwargs):
                raise RuntimeError("数据库炸了")

        result = graph.build_graph(Broken())
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["stats"]["nodes"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
