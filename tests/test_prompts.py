"""三块提示词（emotion.rules / emotion.crisis / memory.rules）的默认值测试。

**提示词覆盖层已经整个删掉**（`mind/prompts.py` 与数据目录里的 `prompts.json`）：
三块提示词一律直接用代码里的默认值，不存在"可编辑"这一层。

所以这里守三件事：

1. 默认值**存在**：非空，而且各自的标记就在块里 ——
   标记丢了，`on_llm_request` 里"已经注入过"的判断会失效（要么重复追加，要么不追加）。
2. 默认值**能被注入**：`main.py` 直接读这三个常量，中间没有任何覆盖层。
3. 覆盖层确实没了：`PromptStore` / `render_prompt` 这些入口一个不剩，
   启动时还会把数据目录里的 `prompts.json` 删掉。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import mind  # noqa: E402
from mind import (  # noqa: E402
    CRISIS_BLOCK,
    EMOTION_RULES_BLOCK,
    EMOTION_RULES_MARKER,
    MEMORY_RULES_BLOCK,
    MEMORY_RULES_MARKER,
)
from mind.memory import Settings  # noqa: E402  （记忆侧的 Settings，带 max_inject_chars）
from tests.test_plugin_smoke import (  # noqa: E402
    FakeEvent,
    FakeProviderRequest,
    PluginHarness,
)


class TestDefaultsExist(unittest.TestCase):
    """默认值必须存在、非空、标记对得上 —— 注入全靠它们。"""

    def test_emotion_rules(self) -> None:
        self.assertTrue(EMOTION_RULES_BLOCK.strip(), "emotion.rules 默认值是空的")
        self.assertIn(EMOTION_RULES_MARKER, EMOTION_RULES_BLOCK)
        # 只讲情绪，不规定说话方式
        self.assertIn("不规定你怎么说话", EMOTION_RULES_BLOCK)
        self.assertNotIn("表达基调", EMOTION_RULES_BLOCK)

    def test_emotion_crisis(self) -> None:
        self.assertTrue(CRISIS_BLOCK.strip(), "emotion.crisis 默认值是空的")
        self.assertIn("<safety_override", CRISIS_BLOCK)
        self.assertIn("心理援助热线", CRISIS_BLOCK)

    def test_memory_rules(self) -> None:
        self.assertTrue(MEMORY_RULES_BLOCK.strip(), "memory.rules 默认值是空的")
        self.assertIn(MEMORY_RULES_MARKER, MEMORY_RULES_BLOCK)
        self.assertIn("不要编", MEMORY_RULES_BLOCK)
        # 人称：记忆条目是第三人称档案体，注入时必须把话说成「我」，
        # 不能让模型顺嘴念出「该用户喜欢…」这种腔调
        self.assertIn("第一人称「我」", MEMORY_RULES_BLOCK)
        self.assertIn("该用户", MEMORY_RULES_BLOCK, "反面例子要在，否则模型不知道躲什么")
        # 称呼：叫对方的名字（或人设里的固定叫法），不是「用户」「那个人」
        self.assertIn("称呼对方用他的名字", MEMORY_RULES_BLOCK)
        self.assertIn("别叫他「用户」", MEMORY_RULES_BLOCK)
        # 指代规则不能越界去规定说话风格（人设管的是那部分）
        self.assertIn("并不在同一个物理空间", MEMORY_RULES_BLOCK)

    def test_all_three_are_distinct(self) -> None:
        blocks = {EMOTION_RULES_BLOCK, CRISIS_BLOCK, MEMORY_RULES_BLOCK}
        self.assertEqual(len(blocks), 3, "三块提示词不该互相串文本")


class NamedEvent(FakeEvent):
    """假事件默认把发消息人叫「他」—— 那不是名字，得换个真的。"""

    def get_sender_name(self) -> str:
        return "小明"


class TestMemoryAddressInjection(PluginHarness, unittest.TestCase):
    """注入时要把「这轮在跟谁聊」递给模型，规则块负责第一人称与称呼。"""

    def test_the_senders_name_is_handed_to_the_model(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            from mind.memory import Memory

            plugin.store.add(Memory(
                content="他喜欢喝冰美式", kind="preference", scope="global",
                pinned=True, importance=0.9,
            ))
            req = FakeProviderRequest()
            await plugin.on_llm_request(NamedEvent("你爱喝什么？"), req)
            blocks = self.blocks_of(req)

            self.assertIn("<long_term_memory>", blocks, "这一轮应该注入记忆")
            self.assertIn(
                "这轮在和「小明」说话", blocks,
                f"名字没递给模型，规则块那句「用他的名字称呼他」就落不了地：{blocks}",
            )
            self.assertIn("第一人称「我」", blocks, "记忆规则块没进 system_prompt")
            await plugin.terminate()

        self.run_async(scenario())

    def test_note_only_appears_when_there_is_a_real_name(self) -> None:
        """平台没给昵称时（拿到的是「他」这种代词）别占一行，规则块照样管人称。"""
        from mind.memory import Memory as Mem
        from mind.memory import RetrievalResult
        from mind.memory.prompt import render_address_note, render_memory_block

        # 有真名 → 有这一行；代词 / 空白 → 不占行
        self.assertIn("小明", render_address_note("小明"))
        for blank in ("他", "她", "对方", "   ", "", "未知"):
            self.assertEqual(render_address_note(blank), "", blank)

        result = RetrievalResult(pinned=[Mem(id=1, content="通用设定", scope="global")])
        settings = Settings.from_config({})
        session = "aiocqhttp:FriendMessage:1"
        with_note = render_memory_block(
            result, settings=settings, session_id=session, address="小明",
        )
        self.assertIn("这轮在和「小明」说话", with_note)
        self.assertIn("「他/她/对方」都指他", with_note)
        # 不带名字（或带的是代词）时，一行都不该多出来
        for blank in ("", "他"):
            block = render_memory_block(
                result, settings=settings, session_id=session, address=blank,
            )
            self.assertNotIn("这轮在和", block, blank)


class TestMainInjectsDefaults(unittest.TestCase):
    """main.py 必须直接用这三个常量，中间不能有覆盖层。"""

    @staticmethod
    def _source() -> str:
        return (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")

    def test_no_override_layer_left(self) -> None:
        src = self._source()
        self.assertNotIn("PromptStore", src, "main.py 还在用提示词仓库")
        self.assertNotIn("self.prompts", src, "main.py 还在读 self.prompts")
        self.assertNotIn("def _prompt(", src, "还有走覆盖层的取值函数")
        self.assertNotIn("self._prompt(", src, "还有走覆盖层的取值函数")

    def test_blocks_are_wired_in(self) -> None:
        src = self._source()
        self.assertIn("return EMOTION_RULES_BLOCK", src, "情绪规则块没直接用默认值")
        self.assertIn("+ MEMORY_RULES_BLOCK", src, "记忆规则块没直接用默认值")
        self.assertIn("crisis_template=CRISIS_BLOCK", src, "危机覆盖没直接用默认值")
        # 启动自检：默认值缺失要能喊出来
        self.assertIn("_check_prompt_defaults", src)

    def test_prompts_module_is_gone(self) -> None:
        self.assertFalse(hasattr(mind, "PromptStore"), "PromptStore 还在导出")
        self.assertFalse(hasattr(mind, "render_prompt"), "render_prompt 还在导出")
        self.assertFalse((PLUGIN_DIR / "mind" / "prompts.py").is_file(), "mind/prompts.py 应当已删除")


if __name__ == "__main__":
    unittest.main(verbosity=2)
