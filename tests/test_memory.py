"""长期记忆内核的单元测试（完全不依赖 AstrBot）。

    python3 -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import sys

#: 一个保证写不进去的路径：Windows 用不存在的盘符，POSIX 用 /proc 下面的伪目录。
#: 原来直接写 /proc/... —— 在 Windows 上那不是只读，反而会被真的创建出来。
BAD_WRITE_DIR = (
    "Z:\\dsh-no-such-drive" if sys.platform.startswith("win")
    else "/proc/definitely/not/writable"
)

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from mind.memory import (  # noqa: E402
    KIND_EVENT,
    KIND_FACT,
    KIND_PREFERENCE,
    SCOPE_GLOBAL,
    SCOPE_SESSION,
    SCOPE_USER,
    Memory,
    MemoryExtractor,
    MemoryStore,
    ScoreWeights,
    Settings,
    Turn,
    allows,
    cosine_overlap,
    decide_scope,
    detect_sensitive,
    extract_json_object,
    is_group_session,
    jaccard,
    parse_extraction,
    render_memory_block,
    score_memory,
    tokenize,
)
from mind.memory.retriever import Retriever  # noqa: E402

PRIVATE = "aiocqhttp:FriendMessage:1000000001"
GROUP = "aiocqhttp:GroupMessage:123456"
OWNER = "1000000001"


def make_store(tmp: str) -> MemoryStore:
    return MemoryStore(Path(tmp) / "memories.db")


def _find(result, needle: str):
    for memory in result.memories():
        if needle in memory.content:
            return memory
    return None


class TestTextTools(unittest.TestCase):
    def test_tokenize_chinese_bigrams(self):
        tokens = tokenize("他喜欢喝冰美式")
        self.assertIn("冰美", tokens)
        self.assertIn("美式", tokens)

    def test_tokenize_english_words(self):
        self.assertIn("coffee", tokenize("I like coffee"))

    def test_cosine_overlap_related_beats_unrelated(self):
        query = tokenize("我今天喝了冰美式")
        related = tokenize("他喜欢喝冰美式")
        unrelated = tokenize("他明天要去考试")
        self.assertGreater(cosine_overlap(query, related), cosine_overlap(query, unrelated))
        self.assertGreater(cosine_overlap(query, related), 0.2)

    def test_jaccard_identical(self):
        self.assertEqual(jaccard(tokenize("他喜欢喝冰美式"), tokenize("他喜欢喝冰美式")), 1.0)

    def test_empty_inputs(self):
        self.assertEqual(tokenize(""), set())
        self.assertEqual(cosine_overlap(set(), set()), 0.0)


class TestSessionKind(unittest.TestCase):
    def test_group_detected(self):
        self.assertTrue(is_group_session(GROUP))
        self.assertTrue(is_group_session("telegram:GroupMessage:-100123"))

    def test_private_detected(self):
        self.assertFalse(is_group_session(PRIVATE))
        self.assertFalse(is_group_session("telegram:FriendMessage:42"))

    def test_unknown_treated_as_group(self):
        """猜不准时一律当群聊处理 —— 宁可少注入，也不要泄露。"""
        self.assertTrue(is_group_session(""))
        self.assertTrue(is_group_session("something-weird"))


class TestPrivacy(unittest.TestCase):
    def test_id_card_detected(self):
        self.assertTrue(detect_sensitive("我的身份证是410883201211030824"))

    def test_phone_detected(self):
        self.assertTrue(detect_sensitive("我手机号13812345678"))

    def test_address_detected(self):
        self.assertTrue(detect_sensitive("我家住在河南省孟州市xx路12号"))

    def test_school_detected(self):
        self.assertTrue(detect_sensitive("我在实验中学上学"))

    def test_plain_like_dislikes_not_sensitive(self):
        self.assertFalse(detect_sensitive("他喜欢喝冰美式"))
        self.assertFalse(detect_sensitive("他下周三要考试"))

    def test_scope_sensitive_is_user(self):
        scope = decide_scope(session_id=PRIVATE, sensitive=True)
        self.assertEqual(scope, SCOPE_USER)

    def test_scope_group_defaults_to_session(self):
        self.assertEqual(decide_scope(session_id=GROUP, sensitive=False), SCOPE_SESSION)

    def test_scope_private_defaults_to_user(self):
        self.assertEqual(decide_scope(session_id=PRIVATE, sensitive=False), SCOPE_USER)

    def _memory(self, **kwargs):
        base = dict(content="他喜欢喝冰美式", scope=SCOPE_USER, owner_id=OWNER)
        base.update(kwargs)
        return Memory(**base)

    def test_user_scope_visible_in_private(self):
        self.assertTrue(allows(self._memory(), session_id=PRIVATE, sender_id=OWNER, group=False))

    def test_user_scope_hidden_in_group_by_default(self):
        self.assertFalse(allows(self._memory(), session_id=GROUP, sender_id=OWNER, group=True))

    def test_user_scope_visible_in_group_when_allowed(self):
        self.assertTrue(
            allows(
                self._memory(),
                session_id=GROUP,
                sender_id=OWNER,
                group=True,
                allow_private_in_group=True,
            )
        )

    def test_sensitive_never_in_group_even_when_allowed(self):
        memory = self._memory(sensitive=True, content="我家住在河南省孟州市")
        self.assertFalse(
            allows(
                memory,
                session_id=GROUP,
                sender_id=OWNER,
                group=True,
                allow_private_in_group=True,
            )
        )
        self.assertTrue(allows(memory, session_id=PRIVATE, sender_id=OWNER, group=False))

    def test_sensitive_invisible_to_others(self):
        memory = self._memory(sensitive=True)
        self.assertFalse(allows(memory, session_id=PRIVATE, sender_id="999", group=False))

    def test_session_scope_isolated(self):
        memory = Memory(content="群里发生的事", scope=SCOPE_SESSION, session_id=GROUP)
        self.assertTrue(allows(memory, session_id=GROUP, sender_id="1", group=True))
        self.assertFalse(allows(memory, session_id="other:GroupMessage:9", sender_id="1", group=True))

    def test_global_scope_always_visible(self):
        memory = Memory(content="这是个通用设定", scope=SCOPE_GLOBAL)
        self.assertTrue(allows(memory, session_id=GROUP, sender_id="1", group=True))
        self.assertTrue(allows(memory, session_id=PRIVATE, sender_id="2", group=False))


class TestStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self._tmp.name)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_add_and_get(self):
        mid = self.store.add(
            Memory(
                content="他喜欢喝冰美式",
                keywords=("咖啡", "饮品"),
                pinned=True,
                importance=0.9,
                scope=SCOPE_USER,
                owner_id=OWNER,
            )
        )
        self.assertGreater(mid, 0)
        got = self.store.get(mid)
        self.assertIsNotNone(got)
        self.assertEqual(got.content, "他喜欢喝冰美式")
        self.assertEqual(got.keywords, ("咖啡", "饮品"))
        self.assertTrue(got.pinned)

    def test_candidates_respect_scope(self):
        self.store.add(Memory(content="私聊记忆", scope=SCOPE_USER, owner_id=OWNER))
        self.store.add(Memory(content="别的群的事", scope=SCOPE_SESSION, session_id="other:GroupMessage:1"))
        self.store.add(Memory(content="通用设定", scope=SCOPE_GLOBAL))
        found = self.store.candidates(session_id=PRIVATE, sender_id=OWNER)
        contents = {m.content for m in found}
        self.assertIn("私聊记忆", contents)
        self.assertIn("通用设定", contents)
        self.assertNotIn("别的群的事", contents)

    def test_merge_updates_content(self):
        mid = self.store.add(Memory(content="他喜欢喝咖啡", scope=SCOPE_USER, owner_id=OWNER))
        self.assertTrue(self.store.merge(mid, content="他喜欢喝冰美式", importance=0.95))
        self.assertEqual(self.store.get(mid).content, "他喜欢喝冰美式")
        self.assertEqual(self.store.count(), 1)

    def test_delete(self):
        mid = self.store.add(Memory(content="临时记忆", scope=SCOPE_GLOBAL))
        self.assertTrue(self.store.delete(mid))
        self.assertIsNone(self.store.get(mid))

    def test_embedding_round_trip(self):
        mid = self.store.add(Memory(content="带向量的记忆", scope=SCOPE_GLOBAL))
        self.assertTrue(self.store.set_embedding(mid, [0.25, 0.5, 0.125], "toy"))
        got = self.store.get(mid)
        self.assertEqual(got.embedding_model, "toy")
        self.assertAlmostEqual(got.embedding[1], 0.5, places=5)

    def test_unembedded_query(self):
        self.store.add(Memory(content="没有向量", scope=SCOPE_GLOBAL))
        self.assertEqual(len(self.store.unembedded(model="toy", limit=10)), 1)

    def test_prune_keeps_pinned(self):
        self.store.add(Memory(content="重要档案", pinned=True, importance=1.0, scope=SCOPE_GLOBAL))
        for index in range(30):
            self.store.add(
                Memory(content=f"琐事 {index}", importance=0.05, scope=SCOPE_GLOBAL, hit_count=0)
            )
        removed = self.store.prune(max_memories=10, importance_floor=0.2)
        self.assertGreater(removed, 0)
        contents = {m.content for m in self.store.list_memories(limit=100)}
        self.assertIn("重要档案", contents)

    def test_stats(self):
        self.store.add(Memory(content="一条", pinned=True, scope=SCOPE_GLOBAL))
        self.store.add(Memory(content="敏感的一条", sensitive=True, scope=SCOPE_USER, owner_id=OWNER))
        stats = self.store.stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["pinned"], 1)
        self.assertEqual(stats["sensitive"], 1)

    def test_delete_many(self):
        ids = [
            self.store.add(Memory(content=f"批量 {i}", scope=SCOPE_GLOBAL)) for i in range(4)
        ]
        self.assertEqual(self.store.delete_many(ids[:3]), 3)
        self.assertEqual(self.store.count(), 1)

    def test_delete_many_ignores_garbage(self):
        self.store.add(Memory(content="留着", scope=SCOPE_GLOBAL))
        self.assertEqual(self.store.delete_many([0, None, "x"]), 0)
        self.assertEqual(self.store.count(), 1)

    def test_update_many_pin(self):
        ids = [self.store.add(Memory(content=f"条目 {i}", scope=SCOPE_GLOBAL)) for i in range(3)]
        self.assertEqual(self.store.update_many(ids, pinned=True), 3)
        self.assertTrue(all(m.pinned for m in self.store.list_memories(limit=10)))
        self.assertEqual(self.store.update_many(ids, pinned=False), 3)
        self.assertFalse(any(m.pinned for m in self.store.list_memories(limit=10)))

    def test_update_many_scope_clears_ownership(self):
        """改成 global 时必须把 session_id / owner_id 清掉，否则会留下孤儿记忆。"""
        mid = self.store.add(
            Memory(content="会话里的", scope=SCOPE_SESSION, session_id="s1", owner_id="42")
        )
        self.assertEqual(self.store.update_many([mid], scope=SCOPE_GLOBAL, session_id="", owner_id=""), 1)
        got = self.store.get(mid)
        self.assertEqual(got.scope, SCOPE_GLOBAL)
        self.assertEqual(got.session_id, "")
        self.assertEqual(got.owner_id, "")

    def test_update_many_rejects_unknown_fields(self):
        mid = self.store.add(Memory(content="x", scope=SCOPE_GLOBAL))
        self.assertEqual(self.store.update_many([mid], hacker="yes"), 0)
        self.assertEqual(self.store.update_many([], pinned=True), 0)

    def test_corrupted_db_path_does_not_raise(self):
        broken = MemoryStore(Path(BAD_WRITE_DIR) / "nope" / "memories.db")
        self.assertEqual(broken.count(), 0)
        self.assertEqual(broken.add(Memory(content="x")), 0)


class TestExtraction(unittest.TestCase):
    def test_parse_plain_json(self):
        raw = '{"memories":[{"content":"他喜欢喝冰美式","keywords":["咖啡"],"kind":"preference","pinned":true,"importance":0.8}]}'
        items = parse_extraction(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].content, "他喜欢喝冰美式")
        self.assertEqual(items[0].kind, KIND_PREFERENCE)
        self.assertTrue(items[0].pinned)

    def test_parse_fenced_json(self):
        raw = '好的，这是结果：\n```json\n{"memories":[{"content":"他下周三要考试","kind":"event"}]}\n```'
        items = parse_extraction(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].content, "他下周三要考试")
        self.assertEqual(items[0].kind, KIND_EVENT)

    def test_parse_bare_array(self):
        raw = '[{"content":"他家有一只猫"}]'
        items = parse_extraction(raw)
        self.assertEqual(items[0].content, "他家有一只猫")

    def test_parse_garbage_returns_empty(self):
        self.assertEqual(parse_extraction("我觉得没什么好记的"), [])
        self.assertEqual(parse_extraction(""), [])

    def test_parse_skips_too_short(self):
        items = parse_extraction('{"memories":[{"content":"嗯"},{"content":"他喜欢猫"}]}')
        self.assertEqual([i.content for i in items], ["他喜欢猫"])

    def test_parse_respects_max_items(self):
        raw = '{"memories":[{"content":"事实一"},{"content":"事实二"},{"content":"事实三"}]}'
        self.assertEqual(len(parse_extraction(raw, max_items=2)), 2)

    def test_parse_unknown_kind_falls_back(self):
        items = parse_extraction('{"memories":[{"content":"他喜欢狗","kind":"乱七八糟"}]}')
        self.assertEqual(items[0].kind, KIND_FACT)

    def test_extract_json_object_handles_prefix_text(self):
        payload = extract_json_object('模型说：{"memories":[]} 以上')
        self.assertEqual(payload, {"memories": []})

    def test_parse_strips_role_prefix(self):
        items = parse_extraction('{"memories":[{"content":"他：喜欢喝冰美式"}]}')
        self.assertEqual(items[0].content, "喜欢喝冰美式")


class TestIntegrate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self._tmp.name)
        self.settings = Settings.from_config({})
        self.extractor = MemoryExtractor(self.settings)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _item(self, content, **kwargs):
        from mind.memory import ExtractedItem

        return ExtractedItem(content=content, **kwargs)

    def test_new_items_added(self):
        added, merged, skipped = self.extractor.integrate(
            self.store,
            [self._item("他喜欢喝冰美式"), self._item("他下周三要考试", kind=KIND_EVENT)],
            session_id=PRIVATE,
            sender_id=OWNER,
        )
        self.assertEqual((added, merged, skipped), (2, 0, 0))

    def test_duplicate_merged_not_added(self):
        self.extractor.integrate(
            self.store, [self._item("他喜欢喝冰美式")], session_id=PRIVATE, sender_id=OWNER
        )
        added, merged, _ = self.extractor.integrate(
            self.store, [self._item("他喜欢喝冰美式咖啡")], session_id=PRIVATE, sender_id=OWNER
        )
        self.assertEqual((added, merged), (0, 1))
        self.assertEqual(self.store.count(), 1)

    def test_sensitive_auto_downgraded(self):
        self.extractor.integrate(
            self.store,
            [self._item("他家住在河南省孟州市xx路12号")],
            session_id=PRIVATE,
            sender_id=OWNER,
        )
        memory = self.store.list_memories(limit=5)[0]
        self.assertTrue(memory.sensitive)
        self.assertEqual(memory.scope, SCOPE_USER)

    def test_group_learned_stays_in_group(self):
        self.extractor.integrate(
            self.store, [self._item("群里说明天聚会")], session_id=GROUP, sender_id="1"
        )
        memory = self.store.list_memories(limit=5)[0]
        self.assertEqual(memory.scope, SCOPE_SESSION)
        self.assertEqual(memory.session_id, GROUP)


    def test_records_who_and_where(self):
        """群里学到的要留下「谁告诉我的」和「在哪个会话说的」——
        面板的人员筛选、来源标签、图谱的会话过滤全靠它们。"""
        self.extractor.integrate(
            self.store, [self._item("群里说明天聚会")], session_id=GROUP, sender_id="1"
        )
        memory = self.store.list_memories(limit=5)[0]
        self.assertEqual(memory.owner_id, "1")
        self.assertEqual(memory.session_id, GROUP)

    def test_private_binds_to_its_conversation(self):
        """私聊里的记忆也要挂回它自己的会话，否则换个会话也能看见。"""
        self.extractor.integrate(
            self.store, [self._item("他喜欢喝冰美式")], session_id=PRIVATE, sender_id=OWNER
        )
        memory = self.store.list_memories(limit=5)[0]
        self.assertEqual(memory.scope, SCOPE_USER)
        self.assertEqual(memory.owner_id, OWNER)
        self.assertEqual(memory.session_id, PRIVATE)

    def test_global_scope_has_no_binding(self):
        """通用 = 谁都能看，不挂人也不挂会话。"""
        settings = Settings.from_config({"privacy": {"private_scope": "global"}})
        extractor = MemoryExtractor(settings)
        extractor.integrate(
            self.store, [self._item("地球是圆的")], session_id=PRIVATE, sender_id=OWNER
        )
        memory = self.store.list_memories(limit=5)[0]
        self.assertEqual(memory.scope, "global")
        self.assertEqual(memory.owner_id, "")
        self.assertEqual(memory.session_id, "")

    def test_source_filter_manual_vs_auto(self):
        """手动加的和 AI 抽的要分得开（面板「手动/自动」那个下拉的数据来源）。"""
        self.extractor.integrate(
            self.store, [self._item("群里说明天聚会")], session_id=GROUP, sender_id="1"
        )
        self.extractor.integrate(
            self.store, [self._item("他喜欢喝冰美式")], session_id=GROUP, sender_id="9"
        )
        self.store.add(Memory(
            content="手动补的一条", session_id=GROUP, scope=SCOPE_SESSION, source="manual"
        ))

        _rows, manual = self.store.query_page(source="manual", session_id=GROUP)
        self.assertEqual(manual, 1)
        _rows, auto = self.store.query_page(source="auto", session_id=GROUP)
        self.assertEqual(auto, 2)
        _rows, all_rows = self.store.query_page(session_id=GROUP)
        self.assertEqual(all_rows, 3)

    def test_owner_filter_narrows_the_page(self):
        self.extractor.integrate(
            self.store, [self._item("群里说明天聚会")], session_id=GROUP, sender_id="1"
        )
        self.extractor.integrate(
            self.store, [self._item("他喜欢喝冰美式")], session_id=GROUP, sender_id="9"
        )
        rows, total = self.store.query_page(owner_id="9", session_id=GROUP)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0].owner_id, "9")


class TestRetriever(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self._tmp.name)
        self.settings = Settings.from_config({})
        self.retriever = Retriever(self.settings)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_empty_store(self):
        result = self.retriever.retrieve(
            self.store, query="你好", session_id=PRIVATE, sender_id=OWNER
        )
        self.assertFalse(result)
        self.assertEqual(render_memory_block(result, settings=self.settings, session_id=PRIVATE), "")

    def test_pinned_and_recent_selected(self):
        self.store.add(
            Memory(content="他喜欢喝冰美式", pinned=True, importance=0.9, scope=SCOPE_USER, owner_id=OWNER)
        )
        self.store.add(Memory(content="他昨天去看了电影", scope=SCOPE_USER, owner_id=OWNER))
        result = self.retriever.retrieve(
            self.store, query="今天好累", session_id=PRIVATE, sender_id=OWNER
        )
        self.assertTrue(any(m.pinned for m in result.pinned))
        self.assertTrue(any("电影" in m.content for m in result.recent))

    def test_keyword_rescues_paraphrased_recall(self):
        """换个说法也要想得起来 —— 这正是 keywords 字段存在的意义。"""
        long_ago = time.time() - 20 * 86400
        self.store.add(
            Memory(
                content="他养了一只叫团子的猫",
                keywords=("猫", "团子"),
                importance=0.6,
                scope=SCOPE_USER,
                owner_id=OWNER,
                created_at=long_ago,
                updated_at=long_ago,
            )
        )
        tokens = tokenize("我家那只猫怎么样了")
        memory = self.store.list_memories(limit=1)[0]
        self.assertEqual(cosine_overlap(tokens, tokenize(memory.searchable_text())), 0.0,
                         "前提：这两句的字面二元组确实对不上")
        from mind.memory import keyword_hit_score

        self.assertGreater(keyword_hit_score(memory, "我家那只猫怎么样了"), 0)

        result = self.retriever.retrieve(
            self.store, query="我家那只猫怎么样了", session_id=PRIVATE, sender_id=OWNER
        )
        self.assertTrue(any("团子" in m.content for m in result.relevant))

    def test_relevant_picked_up_by_keyword(self):
        self.store.add(Memory(content="他喜欢喝冰美式", scope=SCOPE_USER, owner_id=OWNER))
        result = self.retriever.retrieve(
            self.store, query="今天想喝点冰美式", session_id=PRIVATE, sender_id=OWNER
        )
        picked = result.memories()
        self.assertTrue(any("冰美式" in m.content for m in picked))
        self.assertIsNotNone(_find(result, "冰美式"))

    def test_stale_irrelevant_memory_not_forced_in(self):
        '''两个月前的一条无关记忆，不该出现在今天的对话里。'''
        long_ago = time.time() - 60 * 86400
        self.store.add(
            Memory(
                content="他养了一只叫团子的猫",
                scope=SCOPE_USER,
                owner_id=OWNER,
                created_at=long_ago,
                updated_at=long_ago,
            )
        )
        result = self.retriever.retrieve(
            self.store, query="今天天气不错", session_id=PRIVATE, sender_id=OWNER
        )
        self.assertEqual(result.recent, [])
        self.assertEqual(result.relevant, [])

    def test_fresh_memory_lands_in_recent(self):
        self.store.add(Memory(content="他昨天说工作很烦", scope=SCOPE_USER, owner_id=OWNER))
        result = self.retriever.retrieve(
            self.store, query="在吗", session_id=PRIVATE, sender_id=OWNER
        )
        self.assertTrue(any("工作很烦" in m.content for m in result.recent))

    def test_other_users_memory_never_selected(self):
        self.store.add(Memory(content="别人的秘密", scope=SCOPE_USER, owner_id="999"))
        result = self.retriever.retrieve(
            self.store, query="别人的秘密", session_id=PRIVATE, sender_id=OWNER
        )
        self.assertFalse(result)

    def test_group_blocks_private_memory(self):
        self.store.add(Memory(content="私聊里说的事", scope=SCOPE_USER, owner_id=OWNER))
        result = self.retriever.retrieve(self.store, query="私聊里说的事", session_id=GROUP, sender_id=OWNER)
        self.assertFalse(result)
        self.assertTrue(result.private_blocked)

    def test_relevant_requires_an_actual_match(self):
        '''只靠"新鲜 + 重要"不该进"可能相关"那一栏 —— 那会和另两栏重复。'''
        long_ago = time.time() - 20 * 86400  # 落在"最近发生"的 14 天窗口之外
        self.store.add(
            Memory(
                content="他养了一只叫团子的猫",
                keywords=("猫", "团子"),
                importance=1.0,
                scope=SCOPE_USER,
                owner_id=OWNER,
                created_at=long_ago,
                updated_at=long_ago,
            )
        )
        result = self.retriever.retrieve(
            self.store, query="今天天气不错", session_id=PRIVATE, sender_id=OWNER
        )
        self.assertEqual(result.relevant, [])

        # 但真的提到猫的时候，它必须被想起来
        hit = self.retriever.retrieve(
            self.store, query="你家那只猫怎么样了", session_id=PRIVATE, sender_id=OWNER
        )
        self.assertTrue(any("团子" in m.content for m in hit.relevant))

    def test_search_prefers_substring(self):
        self.store.add(Memory(content="他喜欢喝冰美式", scope=SCOPE_USER, owner_id=OWNER))
        self.store.add(Memory(content="他明天要考试", scope=SCOPE_USER, owner_id=OWNER))
        hits = self.retriever.search(
            self.store, keyword="冰美式", session_id=PRIVATE, sender_id=OWNER
        )
        self.assertTrue(hits)
        self.assertIn("冰美式", hits[0].content)

    def test_search_respects_privacy(self):
        self.store.add(Memory(content="身份证号相关", sensitive=True, scope=SCOPE_USER, owner_id=OWNER))
        self.assertEqual(
            self.retriever.search(self.store, keyword="身份证", session_id=GROUP, sender_id=OWNER), []
        )

    def test_scoring_prefers_relevant_over_newer(self):
        old_relevant = Memory(
            content="他非常喜欢喝冰美式咖啡", importance=0.5, created_at=1.0, updated_at=1.0
        )
        new_irrelevant = Memory(content="他说今天天气不错", importance=0.5, created_at=1e12, updated_at=1e12)
        weights = ScoreWeights(0.6, 0.0, 0.25, 0.15)
        query = tokenize("想喝冰美式")
        now = 1e12 + 86400
        s1, _ = score_memory(old_relevant, query, weights=weights, now=now)
        s2, _ = score_memory(new_irrelevant, query, weights=weights, now=now)
        self.assertGreater(s1, s2)


class TestRender(unittest.TestCase):
    def setUp(self):
        self.settings = Settings.from_config({})

    def test_block_contains_memory_and_tags(self):
        from mind.memory import RetrievalResult

        result = RetrievalResult(
            pinned=[Memory(id=1, content="他喜欢喝冰美式", pinned=True, scope=SCOPE_USER, owner_id=OWNER)]
        )
        block = render_memory_block(result, settings=self.settings, session_id=PRIVATE)
        self.assertIn("<long_term_memory>", block)
        self.assertIn("他喜欢喝冰美式", block)
        self.assertIn("私下知道", block)

    def test_sensitive_tag_wins_over_scope_tag(self):
        from mind.memory import RetrievalResult

        result = RetrievalResult(
            pinned=[
                Memory(
                    id=1,
                    content="他家住在黄河路12号",
                    pinned=True,
                    sensitive=True,
                    scope=SCOPE_USER,
                    owner_id=OWNER,
                )
            ]
        )
        block = render_memory_block(result, settings=self.settings, session_id=PRIVATE)
        self.assertIn("私事", block)
        self.assertNotIn("私下知道", block)

    def test_rules_block_carries_the_guardrails(self):
        """行为准则必须是恒定文本，才能安全地放进 system_prompt 吃缓存。"""
        from mind.memory import RULES_BLOCK, RULES_MARKER

        self.assertIn(RULES_MARKER, RULES_BLOCK)
        self.assertIn("不要编", RULES_BLOCK)
        self.assertIn("并不在同一个物理空间", RULES_BLOCK)
        self.assertIn("别每条都提", RULES_BLOCK)

    def test_group_block_warns_about_private(self):
        from mind.memory import RetrievalResult

        result = RetrievalResult(pinned=[Memory(id=1, content="通用设定", scope=SCOPE_GLOBAL)])
        block = render_memory_block(result, settings=self.settings, session_id=GROUP)
        self.assertIn("群聊", block)

    def test_empty_returns_blank(self):
        from mind.memory import RetrievalResult

        self.assertEqual(
            render_memory_block(RetrievalResult(), settings=self.settings, session_id=PRIVATE), ""
        )

    def test_address_note_survives_truncation(self):
        """超长被裁时：名字这行要保住，条目也不能被它挤没，标签照样闭合。"""
        from mind.memory import RetrievalResult

        memories = [
            Memory(id=i, content="很长的一条记忆内容" * 6, scope=SCOPE_GLOBAL) for i in range(30)
        ]
        result = RetrievalResult(pinned=memories)
        block = render_memory_block(
            result, settings=self.settings, session_id=PRIVATE,
            max_chars=300, address="小明",
        )
        self.assertLessEqual(len(block), 330, len(block))
        self.assertTrue(block.rstrip().endswith("</long_term_memory>"))
        self.assertEqual(block.count("<long_term_memory>"), 1)
        self.assertIn("小明", block, "「这轮在跟谁聊」不该被裁掉")
        self.assertIn("很长的一条记忆内容", block, "名字说明不该把条目挤光")

    def test_truncation_keeps_tags_closed(self):
        from mind.memory import RetrievalResult

        memories = [
            Memory(id=i, content="很长的一条记忆内容" * 6, scope=SCOPE_GLOBAL) for i in range(30)
        ]
        result = RetrievalResult(pinned=memories)
        block = render_memory_block(
            result, settings=self.settings, session_id=PRIVATE, max_chars=300
        )
        self.assertLessEqual(len(block), 320)
        self.assertTrue(block.rstrip().endswith("</long_term_memory>"))
        self.assertEqual(block.count("<long_term_memory>"), 1)


class TestPromptBuilding(unittest.TestCase):
    def test_prompt_contains_transcript_and_existing(self):
        settings = Settings.from_config({})
        extractor = MemoryExtractor(settings)
        prompt = extractor.build_prompt(
            [Turn(user="我下周要考试", assistant="那你快去复习")],
            ["他喜欢喝冰美式"],
        )
        self.assertIn("我下周要考试", prompt)
        self.assertIn("他喜欢喝冰美式", prompt)
        self.assertIn("宁缺毋滥", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)

