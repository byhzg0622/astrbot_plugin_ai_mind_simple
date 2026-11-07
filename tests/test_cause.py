"""起因（cause）的因果链测试：A1 原话 / B3 主次因 / C1 规则人话化 /
E2 心境起因只认对话侧 / F1 结构化 / G1 自回读弱化。

运行::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from mind.emotion import PAD, EmotionEngine, Settings  # noqa: E402
from mind.emotion.appraisal import (  # noqa: E402
    Appraiser,
    SNIPPET_LIMIT,
    render_cause,
    snippet,
)
from mind.emotion.engine import CauseInfo, format_mood_cause  # noqa: E402
from mind.emotion.prompt import render_state_block  # noqa: E402


class _Clock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _engine(tmp: str, clock: _Clock) -> EmotionEngine:
    engine = EmotionEngine(Settings.from_config({}), Path(tmp), clock=clock)
    engine.load()
    return engine


class CauseShapeTest(unittest.TestCase):
    """B3 主次因 + C1 规则人话化。"""

    def test_render_cause_is_primary_plus_secondary(self) -> None:
        self.assertEqual(render_cause([]), "")
        self.assertEqual(render_cause(["被夸了"]), "被夸了")
        self.assertEqual(render_cause(["被夸了", "被打招呼"]), "被夸了（还顺带：被打招呼）")
        self.assertEqual(
            render_cause(["被夸了", "被打招呼", "被提问"]),
            "被夸了（还顺带：被打招呼、被提问）",
        )
        # 命中太多时收敛成「等 N 件事」
        self.assertEqual(
            render_cause(["被夸了", "被打招呼", "被提问", "被敷衍"]),
            "被夸了（还顺带：被打招呼、被提问 等 3 件事）",
        )

    def test_multi_hits_no_longer_join_with_plus(self) -> None:
        appraiser = Appraiser()
        stim = appraiser.appraise("今天几点下课？在吗")
        self.assertIsNotNone(stim)
        self.assertNotIn(" + ", stim.cause, "B3：不能再机械拼串")
        self.assertIn("还顺带", stim.cause)
        self.assertEqual(stim.primary, "ask_info")
        self.assertIn("greeting", stim.secondary)

    def test_question_rule_is_split(self) -> None:
        """C1：真问题 ≠ 随口一问，两条互斥。"""
        appraiser = Appraiser()

        real = appraiser.appraise("今天几点下课？")
        self.assertIsNotNone(real)
        self.assertEqual(real.key, "ask_info")
        self.assertEqual(real.cause, "被认真问了个问题")

        small = appraiser.appraise("忙不忙？")
        self.assertIsNotNone(small)
        self.assertEqual(small.key, "question")
        self.assertEqual(small.cause, "被随口问了一句")

    def test_neutral_rules_are_down_weighted(self) -> None:
        """打招呼不该再有资格抢起因。"""
        appraiser = Appraiser()
        greeting = appraiser.appraise("你好")
        self.assertIsNotNone(greeting)
        self.assertLess(greeting.delta.magnitude(), 0.2, "greeting 没降权（老版是 0.27）")
        praise = appraiser.appraise("你好棒啊真的好厉害")
        self.assertIsNotNone(praise)
        self.assertGreater(praise.delta.magnitude(), greeting.delta.magnitude())


class CauseMessageTest(unittest.TestCase):
    """A1 原话：截断、敏感信息、三轮滚动丢弃。"""

    def test_snippet_truncates(self) -> None:
        self.assertEqual(snippet("  你好  呀 "), "你好 呀")
        out = snippet("今天几点下课呀老师说要拖堂所以我们可能要晚一点才能放学回家")
        self.assertTrue(out.endswith("…"))
        self.assertEqual(len(out), SNIPPET_LIMIT + 1)
        self.assertEqual(snippet(""), "")

    def test_stimulus_carries_the_message(self) -> None:
        appraiser = Appraiser()
        stim = appraiser.appraise("你几岁了？住在哪")
        self.assertIsNotNone(stim)
        self.assertEqual(stim.message, "你几岁了？住在哪")

    def test_sensitive_message_is_dropped_before_storage(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            stim = engine.appraiser.appraise("我的身份证是410883201211030824")
            self.assertIsNotNone(stim, "身份证应该命中 nosy 规则")
            self.assertTrue(stim.message, "评估阶段先带上原文")
            engine.apply_stimulus(session, stim, None, clock())
            self.assertEqual(
                session.history[-1].message, "",
                "敏感信息落库了 —— 隐私闸门没生效",
            )
            self.assertTrue(session.history[-1].cause, "起因标签要留着")

    def test_message_keeps_only_the_last_three_rounds(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            for index in range(5):
                # 每轮把情绪放回基线：否则连夸五句情绪早就顶到 ±1 饱和，
                # 第 5 条事件的 delta 会是 0（不记事件），测的就不是滚动丢弃了。
                session.emotion = PAD.from_any(engine.settings.baseline)
                text = f"你好棒呀 {index}"
                stim = engine.appraiser.appraise(text)
                self.assertIsNotNone(stim)
                engine.apply_stimulus(session, stim, None, clock())
                clock.advance(30)
            with_message = [event for event in session.history if event.message]
            self.assertEqual(len(session.history), 5, "五轮都要记成事件")
            self.assertEqual(len(with_message), 3, f"原话只该留三轮，实际 {len(with_message)}")
            self.assertEqual(with_message[-1].message, "你好棒呀 4")
            self.assertEqual(with_message[0].message, "你好棒呀 2")
            older = [event for event in session.history[:2] if event.message]
            self.assertFalse(older, "更早的原话没有滚动丢弃")


class CauseStructureTest(unittest.TestCase):
    """F1（R-A 全改）：Snapshot.cause 是结构化的，三处读者都要改。"""

    def test_snapshot_cause_is_structured(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            stim = engine.appraiser.appraise("你几岁了？住在哪")
            engine.apply_stimulus(session, stim, None, clock())
            snap = engine.snapshot(session, None, clock())

            self.assertIsInstance(snap.cause, CauseInfo)
            cause = snap.cause
            self.assertTrue(cause.text)
            self.assertEqual(cause.message, "你几岁了？住在哪")
            self.assertEqual(cause.source, "user")
            self.assertTrue(cause.ago)
            self.assertTrue(any(abs(v) > 0.01 for v in cause.delta))
            data = cause.to_dict()
            self.assertEqual(data["message"], "你几岁了？住在哪")
            self.assertIn("delta", data)
            # str()/bool() 兜底：漏改的地方也不至于打印对象 repr
            self.assertEqual(str(cause), cause.text)
            self.assertTrue(cause)
            self.assertFalse(bool(CauseInfo()))

    def test_prompt_quotes_the_message(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            stim = engine.appraiser.appraise("你几岁了？住在哪")
            engine.apply_stimulus(session, stim, None, clock())
            snap = engine.snapshot(session, None, clock())

            text = render_state_block(snap, "normal")
            self.assertIn("情绪起因：", text)
            self.assertIn("←「你几岁了？住在哪」", text, "P2：原话要进提示词")
            lite = render_state_block(snap, "lite")
            self.assertNotIn("情绪起因：", lite, "lite 档保持精简")

    def test_payload_carries_cause_object(self) -> None:
        from mind.panel import emotion_payload

        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            stim = engine.appraiser.appraise("你几岁了？住在哪")
            engine.apply_stimulus(session, stim, None, clock())
            payload = emotion_payload(
                engine=engine, sample_store=None, session_key="k",
                hours=1, max_points=50, now=clock(),
            )
            cause = payload["current"]["cause"]
            self.assertIsInstance(cause, dict, "R-A：面板拿到的是结构，不是字符串")
            self.assertEqual(cause["message"], "你几岁了？住在哪")
            self.assertIn("primary", cause)
            events = payload["events"]
            self.assertTrue(events)
            self.assertIn("message", events[-1])
            self.assertIn("primary", events[-1])


class MoodCauseScopeTest(unittest.TestCase):
    """E2：心境起因只认对话侧；G1：自回读文案弱化。"""

    def test_self_idle_manual_do_not_become_mood_cause(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())

            engine.set_emotion(session, PAD.of(0.6, 0.4, 0.3), clock())
            self.assertEqual(session.mood_cause, "", "手动设定不算对话侧")
            self.assertEqual(session.mood_last_cause, "", "手动设定也不该进事件侧的起因池")

            engine.apply_self_expression(session, "shy", strength=1.0, now=clock())
            self.assertEqual(session.mood_cause, "", "她自己的语气不算心境起因")
            self.assertEqual(session.mood_last_cause, "", "自回读同样不进起因池")
            self.assertEqual(
                session.history[-1].cause, "顺着当时的心情（害羞）",
                "G1：自回读文案要弱化，不能还写「她自己的语气」",
            )
            self.assertNotIn("她自己的语气", session.history[-1].cause)

            session.last_interaction = clock() - 4 * 3600
            self.assertTrue(engine.apply_idle(session, clock()))
            self.assertEqual(session.mood_cause, "", "没人说话不算对话侧")
            self.assertEqual(session.mood_last_cause, "", "空闲事件也不进起因池")

            # 只有对方说的话才算：事件侧先记下（D7 在换词那一刻才收进 mood_cause）
            stim = engine.appraiser.appraise("今天几点下课？")
            engine.apply_stimulus(session, stim, None, clock())
            self.assertEqual(session.mood_last_cause, stim.cause, "对话事件要记起因")

            # 心境推到有方向的位置 → 第一次判定直接认 → 起因随之收下
            session.mood = PAD.from_any(engine.settings.baseline) + PAD.of(0.30, 0.20, 0.10)
            session.mood_label(clock(), engine.settings)
            self.assertEqual(session.mood_cause, stim.cause, "换词后没把起因收下来")

    def test_format_mood_cause_marks_continuation(self) -> None:
        now = 1_700_000_000.0
        live = 1800.0
        fresh = format_mood_cause("被夸了", now - 300, now, live)
        self.assertIn("被夸了", fresh)
        self.assertNotIn("延续", fresh)

        stale = format_mood_cause("被夸了", now - 3 * 3600, now, live)
        self.assertTrue(stale.startswith("延续自"), stale)
        self.assertIn("不是新原因", stale)
        self.assertEqual(format_mood_cause("", now, now, live), "")

    def test_snapshot_carries_the_display_form(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            snap = engine.snapshot(session, None, clock())
            self.assertEqual(snap.mood_cause_display, "", "没起因就别硬造")

            session.mood_cause = "被夸了"
            session.mood_cause_at = clock()
            snap = engine.snapshot(session, None, clock())
            self.assertIn("被夸了", snap.mood_cause_display)

            session.mood_cause_at = clock() - 4 * 3600
            snap = engine.snapshot(session, None, clock())
            self.assertIn("延续自", snap.mood_cause_display)
            text = render_state_block(snap, "normal")
            self.assertIn("延续自", text, "提示词也要说清这是延续、不是新原因")


if __name__ == "__main__":
    unittest.main(verbosity=2)
