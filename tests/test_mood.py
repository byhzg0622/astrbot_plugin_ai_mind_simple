"""心境（mood）的推进与判定测试。

A4 窗口 + A3 连续推进 / B1 对称 / C2 6 小时 / D1 主导维度 / D2 副词 /
D3 迟滞驻留 / D7 起因 / E3 提示词注入 / F2 强度边际递减。

运行::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import tempfile  # noqa: E402

from mind.emotion import (  # noqa: E402
    PAD,
    EmotionEngine,
    Settings,
)
from mind.emotion.moodlabel import (  # noqa: E402
    DEAD_ZONE,
    ENTRY_MAGNITUDE,
    degree_of,
    direction,
    evaluate,
)
from mind.emotion.moodwords import mood_word_pad, resolve_mood_word  # noqa: E402
from mind.emotion.prompt import render_state_block  # noqa: E402


class _Clock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _engine(tmp: str, clock: _Clock, config: dict | None = None) -> EmotionEngine:
    engine = EmotionEngine(Settings.from_config(config or {}), Path(tmp), clock=clock)
    engine.load()
    return engine


class MoodLabelRulesTest(unittest.TestCase):
    """D1 主导维度 + D2 程度副词。"""

    def test_direction_follows_human_order(self) -> None:
        # 先看好坏(P) → 再看亢奋度(A) → 再看底气(D)
        self.assertEqual(direction(0.3, 0.2, 0.1), "joy")        # 好 + 有点亢奋
        self.assertEqual(direction(0.3, 0.4, 0.1), "excited")    # 好 + 很亢奋
        self.assertEqual(direction(0.3, -0.3, 0.1), "content")   # 好但安静 → 满足
        self.assertEqual(direction(0.3, 0.0, 0.4), "proud")      # 好且有底气
        self.assertEqual(direction(0.3, 0.0, -0.4), "affection") # 好但没底气
        self.assertEqual(direction(-0.3, 0.4, 0.1), "annoyed")   # 坏 + 烦
        self.assertEqual(direction(-0.5, -0.3, 0.1), "sad")      # 坏 + 提不起劲
        self.assertEqual(direction(-0.3, -0.3, 0.1), "down")
        self.assertEqual(direction(-0.3, 0.1, -0.5), "hurt")     # 坏 + 没底气 → 委屈
        self.assertEqual(direction(0.0, 0.4, 0.0), "curious")     # 好坏不明显 → 有劲头
        self.assertEqual(direction(0.0, -0.4, 0.0), "bored")      # 提不起劲
        self.assertEqual(direction(0.0, 0.0, 0.0), "calm")

    def test_degree_buckets(self) -> None:
        # D2：有点 / 不加词 / 很
        self.assertEqual(degree_of(DEAD_ZONE), "有点")
        self.assertEqual(degree_of(0.30), "")
        self.assertEqual(degree_of(0.60), "很")

    def test_evaluate_reports_reason_and_score(self) -> None:
        baseline = PAD.of(0.08, 0.22, 0.30)
        calm = evaluate(baseline, baseline)
        self.assertEqual(calm.key, "calm")
        self.assertEqual(calm.text, "平静")

        good = evaluate(baseline + PAD.of(0.30, 0.10, 0.05), baseline)
        self.assertEqual(good.key, "joy")
        self.assertTrue(good.text.startswith(("有点", "", "很")))
        self.assertIn("愉悦比平时高", good.reason, "D7 要给出人话依据")
        self.assertGreater(good.score, 0, "迷你条要落在右侧")

        bad = evaluate(baseline + PAD.of(-0.30, -0.10, -0.05), baseline)
        self.assertIn("愉悦比平时低", bad.reason)
        self.assertLess(bad.score, 0)


class MoodDynamicsTest(unittest.TestCase):
    """A3/A4 推进 + C2 半衰期 + B1 对称。"""

    def test_defaults_match_the_chosen_options(self) -> None:
        settings = Settings.from_config({})
        self.assertEqual(settings.mood_half_life, 21600.0, "C2：心境半衰期要 6 小时")
        self.assertEqual(settings.mood_live_window, 1800.0, "A4：30 分钟对话窗口")
        self.assertEqual(settings.mood_label_hold, 300.0, "D3：换词驻留 5 分钟")
        self.assertAlmostEqual(settings.mood_coupling, 0.10, places=6, msg="B1：正负对称")

        tuned = Settings.from_config({
            "dynamics": {"mood_live_window_minutes": 15, "mood_label_hold_seconds": 60},
        })
        self.assertEqual(tuned.mood_live_window, 900.0)
        self.assertEqual(tuned.mood_label_hold, 60.0)

    def test_mood_chases_target_only_inside_the_window(self) -> None:
        """A4：窗口内连续推进；出了窗口只按半衰期回落，不会被情绪拖回基线。"""
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            target = PAD.of(0.60, 0.40, 0.40)
            # ① 窗口内（刚互动过）
            session.mood = PAD.from_any(engine.settings.baseline)
            session.mood_target = target
            session.last_interaction = clock()
            before = session.mood
            clock.advance(600)
            engine.tick_all(clock())
            moved = (session.mood - before).magnitude()
            self.assertGreater(moved, 0.10, "窗口内心境要持续朝目标推进")

            # ② 窗口外（1 小时没说话）：推进停掉，只剩 6 小时半衰期的回落
            session.mood = PAD.from_any(engine.settings.baseline)
            session.mood_target = target
            session.last_interaction = clock() - 7200
            before = session.mood
            clock.advance(600)
            engine.tick_all(clock())
            still = (session.mood - before).magnitude()
            self.assertLess(still, 0.05, "出了对话窗口就不该再被目标拽着走")

    def test_mood_keeps_the_conversation_level_after_it_ends(self) -> None:
        """对话结束后心境要留在原地（C2），而不是跟着情绪 4 分钟就没了。"""
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            session.last_interaction = clock()
            stim = engine.appraiser.appraise("你好棒啊真的好厉害")
            engine.apply_stimulus(session, stim, None, clock())
            peak = session.mood

            # 对话结束 1 小时（远超情绪半衰期 4 分钟）
            clock.advance(3600)
            engine.tick_all(clock())
            lost = (peak - session.mood).magnitude()
            # 1 小时 / 6 小时半衰期 ≈ 掉 11%；若按老的 1.5 小时会掉 75%
            self.assertLess(lost, (peak - engine.settings.baseline).magnitude() * 0.30,
                            "心境没留住（半衰期还是老的 1.5 小时？）")

    def test_intensity_gain_diminishes(self) -> None:
        """F2：增益按当前强度边际递减，三句话不许直接打满 100%。"""
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            stim = engine.appraiser.appraise("你好棒啊真的好厉害")
            values = [session.intensity]
            for _ in range(3):
                engine.apply_stimulus(session, stim, None, clock())
                values.append(session.intensity)
                clock.advance(60)
                engine.tick_all(clock())
            self.assertLess(values[-1], 0.95, f"三句就打满了：{values}")
            self.assertTrue(all(b >= a for a, b in zip(values, values[1:])), values)
            steps = [b - a for a, b in zip(values, values[1:])]
            self.assertTrue(all(steps[i + 1] <= steps[i] + 1e-9 for i in range(len(steps) - 1)),
                            f"增益没有递减：{steps}")


class MoodLabelStateTest(unittest.TestCase):
    """D3 迟滞驻留 + D7 起因。"""

    def test_switching_waits_for_residence_and_captures_cause(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            settings = engine.settings
            baseline = settings.baseline
            hold = settings.mood_label_hold

            # ① 起点在基线 → 第一次判定直接认「平静」
            session.mood = PAD.from_any(baseline)
            first = session.mood_label(clock(), settings)
            self.assertEqual(first.key, "calm")

            # ② 跨过入口阈值 → 从平静进入有名有姓的心境：**立刻认**（不等驻留），
            #    并把「起因」一起收下（D7）
            session.mood_last_cause = "他夸了她"
            session.mood_last_cause_at = clock()
            session.mood = PAD.from_any(baseline) + PAD.of(0.30, 0.20, 0.10)
            wanted = evaluate(session.mood, baseline)
            self.assertEqual(wanted.key, "joy")
            self.assertGreaterEqual(wanted.magnitude, ENTRY_MAGNITUDE, "没跨过入口阈值就进不来")
            entered = session.mood_label(clock() + 10, settings)
            self.assertEqual(entered.key, "joy", "从平静进入有名心境不该等驻留")
            self.assertEqual(session.mood_cause, "他夸了她")

            # ③ **词与词之间**换词（joy → 低落方向）：必须驻留 —— 那才是抖的地方
            session.mood = PAD.from_any(baseline) + PAD.of(-0.35, -0.25, -0.10)
            target = evaluate(session.mood, baseline)
            self.assertIn(target.key, ("down", "sad"))
            held = session.mood_label(clock() + 11, settings)
            self.assertEqual(held.key, "joy", "刚提出换词就换 = 没有驻留")
            still = session.mood_label(clock() + 10 + hold - 1, settings)
            self.assertEqual(still.key, "joy")
            switched = session.mood_label(clock() + 10 + hold + 1, settings)
            self.assertEqual(switched.key, target.key)
            self.assertEqual(switched.since, clock() + 10 + hold + 1)

            # ④ 回到基线：回到「平静」是立即的（死区本身够稳）
            session.mood = PAD.from_any(baseline)
            back = session.mood_label(clock() + 10 + hold + 2, settings)
            self.assertEqual(back.key, "calm")

    def test_small_drift_never_enters_a_named_label(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            settings = engine.settings
            session.mood = PAD.from_any(settings.baseline)
            session.mood_label(clock(), settings)
            # 偏移在死区和入口阈值之间：不许换成有名有姓的词
            session.mood = PAD.from_any(settings.baseline) + PAD.of(0.11, 0.0, 0.0)
            label = session.mood_label(clock() + 4000, settings)
            self.assertEqual(label.key, "calm")
            self.assertLess(ENTRY_MAGNITUDE, 0.13)


class MoodPromptAndPayloadTest(unittest.TestCase):
    """E3 注入 + 面板载荷。"""

    def test_prompt_injects_mood_in_normal_and_rich_but_not_lite(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            session.mood = PAD.from_any(engine.settings.baseline) + PAD.of(0.30, 0.20, 0.10)
            session.mood_target = session.mood
            session._capture_mood_cause("他夸了她", clock())
            snap = engine.snapshot(session, None, clock())

            normal = render_state_block(snap, "normal")
            self.assertIn("心境：", normal)
            self.assertIn("比平时", normal, "E3 要注入人话偏移")
            self.assertIn("起因：他夸了她", normal)

            rich = render_state_block(snap, "rich")
            self.assertIn("心境：", rich)
            self.assertIn("依据：", rich, "rich 档要带上判定依据")

            lite = render_state_block(snap, "lite")
            self.assertNotIn("心境：", lite, "lite 档保持精简")

    def test_payload_carries_the_whole_mood_block(self) -> None:
        from mind.panel import emotion_payload

        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            session.mood = PAD.from_any(engine.settings.baseline) + PAD.of(0.30, 0.20, 0.10)
            session.mood_target = session.mood
            session._capture_mood_cause("他夸了她", clock())
            payload = emotion_payload(
                engine=engine, sample_store=None, session_key="k",
                hours=1, max_points=60, now=clock(),
            )
            mood = payload["mood"]
            for key in ("label", "name", "emoji", "reason", "cause", "cause_ago",
                        "magnitude", "score", "since_ago", "trend", "trend_text"):
                self.assertIn(key, mood, f"心境块少了 {key}")
            self.assertEqual(mood["cause"], "他夸了她")
            self.assertGreater(mood["magnitude"], 0.0)
            self.assertIn("trend_text", mood)
            self.assertEqual(payload["params"]["mood_live_window"], 1800.0)
            self.assertEqual(payload["params"]["mood_half_life"], 21600.0)

    def test_schema_declares_the_mood_options(self) -> None:
        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        items = schema["emotion"]["items"]["dynamics"]["items"]
        self.assertEqual(items["mood_half_life"]["default"], 21600)
        self.assertEqual(items["mood_live_window_minutes"]["default"], 30)
        self.assertEqual(items["mood_label_hold_seconds"]["default"], 300)


class MoodOpsTest(unittest.TestCase):
    """「调节心境」：偏移维度 + 心境描述词（和操纵情绪不是同一套）。"""

    def test_every_mood_word_classifies_as_its_target(self) -> None:
        """点「心满意足」判出来就必须是「满足」—— 词表和 D1 判定不能对不上。"""
        from mind.emotion.moodwords import (
            MOOD_WORDS,
            mood_word_pad,
            mood_words_payload,
            resolve_mood_word,
        )

        baseline = Settings.from_config({}).baseline
        self.assertEqual(len(MOOD_WORDS), 18)
        self.assertEqual(len(mood_words_payload()), 18)
        for word in MOOD_WORDS:
            pad = mood_word_pad(word, baseline)
            self.assertEqual(
                (pad.p, pad.a, pad.d),
                (baseline.p + word.offset[0], baseline.a + word.offset[1], baseline.d + word.offset[2]),
                f"「{word.label}」不是基线 + 偏移",
            )
            got = evaluate(pad, baseline)
            self.assertEqual(
                got.key, word.target,
                f"点「{word.label}」判成了「{got.name}」（期望 {word.target}）",
            )
            self.assertEqual(resolve_mood_word(word.key).key, word.key)
            self.assertEqual(resolve_mood_word(word.label).key, word.key, "中文词也要认")

    def test_set_mood_applies_immediately_and_records(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            target = mood_word_pad(resolve_mood_word("content"), engine.settings.baseline)
            engine.set_mood(session, target, clock())

            self.assertEqual(session.mood.to_dict(), target.to_dict())
            self.assertEqual(
                session.mood_target.to_dict(), target.to_dict(),
                "目标要一起改，否则对话窗口内的推进会把它拽回去",
            )
            label = session.mood_label(clock(), engine.settings)
            self.assertEqual(label.key, "content", "调完要立刻换词（不等驻留）")
            self.assertTrue(session.mood_history, "没记心境事件")
            self.assertEqual(session.mood_history[-1].source, "manual")
            self.assertEqual(session.mood_cause, "", "手动设定不算心境起因（E2）")

    def test_offset_is_relative_to_baseline(self) -> None:
        """滑杆发的是「偏移」：面板换算成 绝对值 = 基线 + 偏移。"""
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            base = engine.settings.baseline
            engine.set_mood(
                session,
                PAD.of(base.p + 0.22, base.a - 0.18, base.d + 0.10).clamped(),
                clock(),
            )
            back = session.mood - PAD.from_any(base)
            self.assertAlmostEqual(back.p, 0.22, places=6)
            self.assertAlmostEqual(back.a, -0.18, places=6)
            self.assertAlmostEqual(back.d, 0.10, places=6)

    def test_reset_mood_only_touches_mood(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            engine.set_mood(
                session,
                mood_word_pad(resolve_mood_word("down"), engine.settings.baseline),
                clock(),
            )
            emotion_before = session.emotion.to_dict()
            engine.reset_mood(session, clock())
            self.assertEqual(
                session.mood.to_dict(),
                PAD.from_any(engine.settings.baseline).to_dict(),
            )
            self.assertEqual(session.emotion.to_dict(), emotion_before, "重置心境不该动情绪")
            self.assertEqual(session.mood_label(clock(), engine.settings).key, "calm")
            self.assertEqual(session.mood_history[-1].source, "reset")

    def test_label_switch_and_drift_are_recorded(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            session.mood_label(clock(), engine.settings)          # 首次判定：平静（不记事件）
            self.assertFalse(session.mood_history, "首次判定不该算一条事件")

            # 换词：平静 → 开心（走 label 路径）
            session.mood = PAD.from_any(engine.settings.baseline) + PAD.of(0.30, 0.20, 0.10)
            session.mood_label(clock() + 10, engine.settings)
            self.assertEqual(session.mood_history[-1].source, "label")

            # 漂移：换成更强但方向不变的值，6 小时后（一个半衰期）回落够 0.10 ——
            # 词没变，所以只该多出一条 drift，而不是又一条 label
            engine.set_mood(session, PAD.of(0.53, 0.52, 0.45), clock())
            self.assertEqual(session.mood_history[-1].source, "manual")
            clock.advance(21600)
            engine.tick_all(clock())
            sources = [shift.source for shift in session.mood_history]
            self.assertIn("drift", sources, "心境回落够 0.10 却没记成漂移事件")
            self.assertEqual(sources.count("manual"), 1, "手动设定被记了多次")

    def test_payload_carries_mood_words_and_events(self) -> None:
        from mind.panel import emotion_payload

        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            engine.set_mood(
                session,
                mood_word_pad(resolve_mood_word("joy"), engine.settings.baseline),
                clock(),
            )
            payload = emotion_payload(
                engine=engine, sample_store=None, session_key="k",
                hours=1, max_points=50, now=clock(),
            )
            words = payload["mood_words"]
            self.assertEqual(len(words), 18, "心境词表要整份发给面板")
            keys = [item["key"] for item in words]
            self.assertIn("content", keys)
            for item in words:
                for key in ("key", "label", "emoji", "offset", "target"):
                    self.assertIn(key, item, f"心境词少了 {key}")
                self.assertEqual(len(item["offset"]), 3)
            events = payload["mood_events"]
            self.assertTrue(events, "面板要拿得到最近心境事件")
            item = events[-1]
            for key in ("t", "ago", "key", "label", "emoji", "cause", "offset", "source"):
                self.assertIn(key, item, f"心境事件少了 {key}")
            self.assertEqual(item["source"], "manual")
            self.assertEqual(len(item["offset"]), 3)

    def test_determinism_decides_within_the_same_branch(self) -> None:
        """**底气必须能换词**（报过的 bug：动过能量后底气怎么拉心境都不变）。"""
        # 同样的好底色 + 中性能量，只拉底气：开心 ↔ 得意 ↔ 亲昵
        self.assertEqual(direction(0.30, 0.05, 0.05), "joy")
        self.assertEqual(direction(0.30, 0.05, 0.45), "proud")
        self.assertEqual(direction(0.30, 0.05, -0.45), "affection")
        # 坏底色 + 没劲：有底气 → 冷淡（以前会判成低落/难过）
        self.assertEqual(direction(-0.30, -0.30, 0.40), "cold")
        # 坏底色 + 亢奋 + 没底 → 忐忑（新增的分支）
        self.assertEqual(direction(-0.30, 0.40, -0.42), "anxious")
        # 能量仍然说了算（高兴奋度不会被底气盖掉）
        self.assertEqual(direction(0.30, 0.45, 0.05), "excited")
        # 难过锚点没被带歪
        self.assertEqual(direction(-0.88, -0.37, -0.90), "sad")

    def test_mood_bar_reaches_both_edges(self) -> None:
        """迷你条：任何一根滑杆拉到 ±0.6 就要顶到最左/最右（报过的 bug：到不了边）。"""
        from mind.emotion.moodlabel import MOOD_BAR_SCALE, bar_value

        baseline = Settings.from_config({}).baseline
        self.assertEqual(MOOD_BAR_SCALE, 0.6)
        # 基线 → 中点
        self.assertEqual(bar_value(baseline, baseline), 0.0)
        # 底色拉满 → 顶到最右 / 最左
        self.assertAlmostEqual(
            bar_value(baseline + PAD.of(0.6, 0.0, 0.0), baseline), 1.0, places=6,
        )
        self.assertAlmostEqual(
            bar_value(baseline + PAD.of(-0.6, 0.0, 0.0), baseline), -1.0, places=6,
        )
        # 能量拉满、底气拉满同样要到边（老版用加权 score，只有两三成长度）
        self.assertAlmostEqual(
            bar_value(baseline + PAD.of(0.0, 0.6, 0.0), baseline), 1.0, places=6,
        )
        self.assertAlmostEqual(
            bar_value(baseline + PAD.of(0.0, 0.0, 0.6), baseline), 1.0, places=6,
        )
        # 方向仍按好坏轴：能量为负（提不起劲）→ 落在左边
        self.assertAlmostEqual(
            bar_value(baseline + PAD.of(0.0, -0.6, 0.0), baseline), -1.0, places=6,
        )
        # 词表里的满格词（底气十足）也要接近满量程
        confident = resolve_mood_word("底气十足")
        self.assertIsNotNone(confident, "新词没进词表")
        self.assertGreater(
            abs(bar_value(mood_word_pad(confident, baseline), baseline)), 0.7,
            "底气十足 的条子太短了",
        )

    def test_mood_freeze_matches_emotion_freeze(self) -> None:
        """心境冻结：判定和情绪冻结一致 —— 冻住就是完全不动，手动照样生效。"""
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            engine.set_emotion(session, PAD.of(0.6, 0.4, 0.3), clock())   # 让情绪有得衰减
            engine.set_mood(
                session,
                mood_word_pad(resolve_mood_word("joy"), engine.settings.baseline),
                clock(),
            )
            mood_before = session.mood.to_dict()
            label_before = session.mood_label(clock(), engine.settings).key
            emotion_before = session.emotion.to_dict()

            self.assertTrue(engine.freeze_mood("k"))
            self.assertFalse(engine.freeze_mood("k"), "重复冻结要返回 False")
            self.assertTrue(engine.mood_frozen("k"))

            clock.advance(7200)
            engine.tick_all(clock())
            self.assertEqual(session.mood.to_dict(), mood_before, "冻结后心境还在动（推进/衰减没停）")
            self.assertEqual(
                session.mood_label(clock(), engine.settings).key, label_before,
                "冻结后换了词",
            )
            self.assertNotEqual(
                session.emotion.to_dict(), emotion_before,
                "情绪与强度照常走 —— 冻结的是心境这一层",
            )

            # 手动调在冻结时仍然生效（判定同情绪冻结）
            target = mood_word_pad(resolve_mood_word("down"), engine.settings.baseline)
            engine.set_mood(session, target, clock())
            self.assertEqual(
                session.mood.to_dict(), target.to_dict(), "冻结时手动调没生效"
            )
            self.assertEqual(
                session.mood_label(clock(), engine.settings).key, "down",
                "手动调之后标签要当场换",
            )

            # 解冻：恢复衰减
            self.assertTrue(engine.unfreeze_mood("k"))
            self.assertFalse(engine.unfreeze_mood("k"), "重复解冻返回 False")
            self.assertFalse(engine.mood_frozen("k"))
            held = session.mood.to_dict()
            clock.advance(7200)
            engine.tick_all(clock())
            self.assertNotEqual(session.mood.to_dict(), held, "解冻后没有恢复衰减")

            # 面板要能看到冻没冻
            from mind.panel import emotion_payload

            payload = emotion_payload(
                engine=engine, sample_store=None, session_key="k",
                hours=1, max_points=40, now=clock(),
            )
            self.assertIn("frozen", payload["mood"], "心境块要带冻结状态")

if __name__ == "__main__":
    unittest.main(verbosity=2)
