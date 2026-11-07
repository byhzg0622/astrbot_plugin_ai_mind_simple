"""消息里的协议占位标记清理 —— <quoted_message …/> / [CQ:reply,…]。

QQ/OneBot 会把「引用回复」序列化成占位标记，直接存进「原话」，面板上就是
一串看不懂的乱码（情绪起因卡、事件流、注入给模型的提示词都会看见它）。

三条防线都要测到：

1. **截断前就剥**：snippet() —— 否则 24 个字全被标记吃掉；
2. **落库/回读都剥**：MoodEvent.__post_init__ —— 老存档里的乱码同样要清；
3. **入口就剥**：情绪评估 + 记忆采集（标记进了抽取提示词，会被模型当成「他说的话」）。

运行::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from tests.test_plugin_smoke import PRIVATE, PluginHarness  # noqa: E402

from mind.emotion import MoodEvent  # noqa: E402
from mind.emotion.appraisal import snippet  # noqa: E402
from mind.textfix import strip_message_markup, tidy_cjk_spaces  # noqa: E402

#: OneBot 的引用回复占位标记
REPLY = '<quoted_message sender="小明" id="123456"/>'
PRAISE = "你今天真的好棒啊"


class StripMarkupTest(unittest.TestCase):
    """纯函数：剥标记、别误伤正常文本。"""

    def test_quoted_message_is_removed(self) -> None:
        self.assertEqual(
            strip_message_markup(REPLY + "今天几点下课？"), "今天几点下课？",
            "引用回复标记要整个剥掉，只留人话",
        )

    def test_cq_code_is_removed(self) -> None:
        self.assertEqual(strip_message_markup("[CQ:reply,id=1]在吗"), "在吗")

    def test_unknown_tag_is_removed(self) -> None:
        # 认不出的占位标记（比如 <keyboard …/>）也要剥 —— 面板上没人看得懂它
        self.assertEqual(strip_message_markup("<keyboard foo=\"1\"/>好"), "好")

    def test_normal_text_survives(self) -> None:
        for text in ("今天几点下课？", "1 < 2 > 0", "<3 我爱你", "温度 <0 了", ""):
            self.assertEqual(strip_message_markup(text), text, text)

    def test_it_is_idempotent(self) -> None:
        once = strip_message_markup(REPLY + PRAISE)
        self.assertEqual(strip_message_markup(once), once)

    def test_tidy_cjk_spaces_still_works(self) -> None:
        # 别因为加了新函数把老功能带坏
        self.assertEqual(tidy_cjk_spaces("好 吧 行 。"), "好吧行。")

    def test_snippet_strips_before_truncating(self) -> None:
        """24 字的额度要留给人话，不能全被标记吃掉。"""
        self.assertEqual(snippet(REPLY + PRAISE), PRAISE)

    def test_mood_event_cleans_its_message(self) -> None:
        event = MoodEvent(
            t=1.0, label="好奇", emoji="🧐", cause="被认真问了个问题",
            delta=(0.1, 0.1, 0.0), message=REPLY + PRAISE,
        )
        self.assertEqual(event.message, PRAISE)
        self.assertEqual(event.to_dict()["message"], PRAISE)

    def test_old_saved_events_are_cleaned_on_load(self) -> None:
        """老存档里已经存了乱码：读回来时也得清干净（面板和提示词都读它）。"""
        loaded = MoodEvent.from_dict({
            "t": 1.0, "label": "好奇", "emoji": "🧐",
            "cause": "被认真问了个问题", "delta": [0.1, 0.1, 0.0],
            "message": REPLY + "旧数据里的原话",
        })
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.message, "旧数据里的原话")
        self.assertNotIn("quoted_message", loaded.message)


class MarkupEndToEndTest(PluginHarness, unittest.TestCase):
    """整条链路：带标记的消息 → 情绪事件 / 面板 / 记忆采集，处处都干净。"""

    async def _plugin(self):
        plugin = self.make_plugin()
        await plugin.initialize()
        return plugin

    def test_placeholder_never_reaches_panel_or_memory(self) -> None:
        async def scenario() -> None:
            plugin = await self._plugin()
            await self.send(plugin, REPLY + PRAISE)

            # ① 情绪事件里的原话
            data = self._body(await self.api(plugin, "emotion", query={"session": PRIVATE}))
            events = [item for item in (data.get("events") or []) if item.get("message")]
            self.assertTrue(events, "这句话应该记下事件（否则这条断言就是空跑）")
            for item in events:
                message = str(item["message"])
                self.assertNotIn("quoted_message", message, message)
                self.assertNotIn("CQ:", message, message)
            self.assertTrue(
                any(PRAISE in str(item["message"]) for item in events),
                f"原话要留下人话本身：{[item['message'] for item in events]}",
            )

            # ② 引擎里存的事件也是同一条 message
            session = plugin.engine.resolve(PRIVATE, 1000.0)
            for item in session.history:
                self.assertNotIn("quoted_message", item.message or "")

            # ③ 记忆采集：标记进了抽取提示词，会被模型当成「他说的话」
            turns = plugin._buffers.get(PRIVATE) or []
            self.assertTrue(turns, "这句话应该被暂存待抽取")
            self.assertNotIn("quoted_message", turns[-1].user, turns[-1].user)
            self.assertIn(PRAISE, turns[-1].user)

            await plugin.terminate()

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()
