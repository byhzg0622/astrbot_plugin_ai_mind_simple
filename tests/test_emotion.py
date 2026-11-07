"""情绪内核的单元测试（完全不依赖 AstrBot）。

运行::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

#: 一个保证写不进去的路径：Windows 用不存在的盘符，POSIX 用 /proc 下面的伪目录。
#: 原来直接写 /proc/... —— 在 Windows 上那不是只读，反而会被真的创建出来。
BAD_WRITE_DIR = (
    "Z:\\dsh-no-such-drive" if sys.platform.startswith("win")
    else "/proc/definitely/not/writable"
)

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from mind.emotion import (  # noqa: E402
    EMOTION_TAG_RE,
    PAD,
    PROTOTYPES,
    Appraiser,
    EmotionEngine,
    JsonStore,
    Relation,
    Settings,
    classify,
    detect_crisis,
    get_preset,
    heuristic_self_emotion,
    parse_custom_rule,
    resolve_prototype,
    session_intensity,
)
from mind.emotion.model import (  # noqa: E402
    decay_intensity,
    decay_scalar,
    intensity_level,
    intensity_percent,
    level_of_intensity,
)


class TestPAD(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(PAD.of(3, -9, 0.5).to_dict(), {"p": 1.0, "a": -1.0, "d": 0.5})

    def test_arithmetic(self):
        result = PAD.of(0.5, 0.5, 0.5) + PAD.of(0.2, -0.1, 0.0)
        self.assertAlmostEqual(result.p, 0.7, places=6)
        self.assertAlmostEqual(result.a, 0.4, places=6)

    def test_limit_preserves_direction(self):
        limited = PAD.of(1, 1, 1).limit(0.5)
        self.assertAlmostEqual(limited.magnitude(), 0.5, places=6)
        self.assertAlmostEqual(limited.p, limited.a, places=6)

    def test_from_any(self):
        self.assertEqual(PAD.from_any({"p": 0.2, "a": 0.3, "d": 0.4}).p, 0.2)
        self.assertEqual(PAD.from_any([0.1, 0.2, 0.3]).d, 0.3)
        self.assertEqual(PAD.from_any("乱七八糟").magnitude(), 0.0)


class TestDecay(unittest.TestCase):
    def test_half_life(self):
        # 经过一个半衰期，与目标的距离应当正好减半
        self.assertAlmostEqual(decay_scalar(1.0, 0.0, 100.0, 100.0), 0.5, places=6)

    def test_zero_dt(self):
        self.assertEqual(decay_scalar(1.0, 0.0, 100.0, 0.0), 1.0)

    def test_converges_to_target(self):
        value = 1.0
        for _ in range(200):
            value = decay_scalar(value, 0.2, 10.0, 10.0)
        self.assertAlmostEqual(value, 0.2, places=3)


class TestClassify(unittest.TestCase):
    def test_origin_is_calm(self):
        self.assertEqual(classify(PAD.of(0, 0, 0)).key, "calm")

    def test_positive_is_joyish(self):
        self.assertIn(classify(PAD.of(0.8, 0.5, 0.4)).key, {"joy", "excited", "proud"})

    def test_negative_arousal_is_angry(self):
        self.assertEqual(classify(PAD.of(-0.7, 0.8, 0.6)).key, "angry")

    def test_intensity_levels(self):
        self.assertEqual(intensity_level(PAD.of(0, 0, 0))[0], 1)
        self.assertEqual(intensity_level(PAD.of(1, 1, 1))[0], 5)


class TestCrisis(unittest.TestCase):
    def test_detects_real_signals(self):
        for text in ["我不想活了", "想自杀", "活着没意思", "写好了遗书", "撑不下去了"]:
            self.assertIsNotNone(detect_crisis(text), text)

    def test_does_not_flag_hyperbole(self):
        for text in ["想死我了", "笑死我了", "困死了", "累死", "这题难死了", "爱死你了"]:
            self.assertIsNone(detect_crisis(text), text)


class TestAppraisal(unittest.TestCase):
    def setUp(self):
        self.appraiser = Appraiser()
        self.relation = Relation(uid="1")

    def test_praise_raises_pleasure(self):
        stim = self.appraiser.appraise("你好棒啊真的好厉害")
        self.assertIsNotNone(stim)
        self.assertGreater(stim.delta.p, 0.2)

    def test_insult_lowers_pleasure(self):
        stim = self.appraiser.appraise("你就是个废物，滚")
        self.assertIsNotNone(stim)
        self.assertLess(stim.delta.p, -0.3)

    def test_crisis_short_circuits(self):
        stim = self.appraiser.appraise("我不想活了")
        self.assertEqual(stim.source, "crisis")
        self.assertGreater(stim.delta.a, 0.5)

    def test_neutral_message_no_stimulus(self):
        self.assertIsNone(self.appraiser.appraise("今天星期二"))


    def test_tsundere_petname_hits_hard(self):
        preset = get_preset()
        appraiser = Appraiser(rules=preset.extra_rules)
        stim = appraiser.appraise("宝宝")
        self.assertIsNotNone(stim)
        self.assertGreater(stim.delta.a, 0.3)

    def test_petname_only_fires_in_address_position(self):
        """称呼必须落在称呼位；裸的 `我的`/词面出现不再算「被叫了特别的称呼」。

        老版本 petname 是裸子串 —— 「我的猫好可爱」「老婆饼」「乖乖听话」
        这类根本不是称呼的句子也会触发害羞。
        """
        preset = get_preset()
        appraiser = Appraiser(rules=preset.extra_rules)

        # ① 真的在叫她
        for text in ("宝宝你今天好棒", "哈哈，我的宝贝", "我的宝贝，过来", "亲爱的，晚上见", "乖乖"):
            stim = appraiser.appraise(text)
            self.assertIsNotNone(stim, text)
            self.assertEqual(stim.key, "petname", text)
            self.assertEqual(stim.emotion, "shy", text)

        # ② 只是词面出现 —— 一个都不许触发
        for text in ("我的猫好可爱", "这是我的", "他媳妇给我买了个礼物", "老婆饼很好吃",
                     "乖乖听话", "我家宝宝睡了", "亲爱的用户您好"):
            stim = appraiser.appraise(text)
            key = stim.key if stim else None
            self.assertNotEqual(key, "petname", f"「{text}」不该被当成「被叫了特别的称呼」")

    def test_call_master_only_fires_in_address_position(self):
        """关系词必须落在「称呼位」才算他真的在叫她（否则只是词面出现）。

        老版本是裸正则 `主人|妈妈|妈|女王|大人`：「你叫主人」这种
        **让别人当主人**的句子也会记成「他真的乖乖叫了那声称呼」。
        """
        preset = get_preset()
        appraiser = Appraiser(rules=preset.extra_rules)

        # ① 真的在叫她：句首 / 标点后 / 句尾收尾
        for text in ("主人，我回来了", "好的主人~", "妈，我想吃你做的饭", "主人叫我吃饭"):
            stim = appraiser.appraise(text)
            self.assertIsNotNone(stim, text)
            self.assertEqual(stim.key, "call_master", text)
            self.assertEqual(stim.emotion, "shy", text)

        # ② 只是出现关系词、并不是在叫她 —— 一个都不许触发
        for text in ("你叫主人", "以后叫我主人", "叫主人", "我妈来了", "我主人不在家",
                     "主人公精神要发扬", "女主人回来了", "他让我当主人"):
            stim = appraiser.appraise(text)
            key = stim.key if stim else None
            self.assertNotEqual(key, "call_master", f"「{text}」不该被当成「叫她主人」")

    def test_short_filler_reads_as_perfunctory(self):
        # 一个"嗯"在她眼里就是敷衍 —— 这是刻意的，不是误判
        stim = self.appraiser.appraise("嗯")
        self.assertIsNotNone(stim)
        self.assertEqual(stim.key, "perfunctory")
        self.assertLess(stim.delta.p, 0)

    def test_rule_declares_emotion_label(self):
        """他生病时规则直接声明"担心"这个情绪标签（不再给表达基调）。"""
        preset = get_preset()
        appraiser = Appraiser(rules=preset.extra_rules)
        stim = appraiser.appraise("我发烧了头好疼")
        self.assertIsNotNone(stim)
        self.assertEqual(stim.emotion, "anxious")

    def test_jealousy_when_others_buy_gifts(self):
        preset = get_preset()
        appraiser = Appraiser(rules=preset.extra_rules)
        stim = appraiser.appraise("我兄弟今天给我买了个礼物")
        self.assertIsNotNone(stim)
        self.assertIn("别人", stim.cause)
        self.assertLess(stim.delta.p, 0)

    def test_long_neutral_text_barely_moves(self):
        # 只有"长文本"这条弱规则命中时，情绪波动应当很小
        stim = self.appraiser.appraise("x" * 140)
        self.assertIsNotNone(stim)
        self.assertLess(stim.delta.magnitude(), 0.2)


class TestCustomRules(unittest.TestCase):
    def test_parse_ok(self):
        rule = parse_custom_rule("摸鱼|划水 => 0.1,0.4,0.05 => +1 => 一起摸鱼")
        self.assertIsNotNone(rule)
        self.assertEqual(rule.pad, (0.1, 0.4, 0.05))
        self.assertEqual(rule.cause, "一起摸鱼")

    def test_parse_bad(self):
        self.assertIsNone(parse_custom_rule("没有箭头"))
        self.assertIsNone(parse_custom_rule(""))


class TestPreset(unittest.TestCase):
    def test_no_speech_style_anywhere(self):
        """表达基调已整体删除：参数、规则里都不该再有任何"怎么说话"的字段。"""
        preset = get_preset()
        self.assertFalse(hasattr(preset, "style_overrides"))
        self.assertFalse(hasattr(preset, "style_note"))
        for rule in preset.extra_rules:
            self.assertFalse(hasattr(rule, "style"), rule.key)
        for proto in PROTOTYPES:
            self.assertFalse(hasattr(proto, "style"), proto.key)

    def test_unknown_preset_falls_back(self):
        self.assertEqual(get_preset("不存在的性格").key, "default")


class TestSelfFeedback(unittest.TestCase):
    def test_shy_detection(self):
        hint = heuristic_self_emotion("你...你干嘛，闭嘴，别得寸进尺")
        self.assertIsNotNone(hint)
        self.assertEqual(hint[0], "shy")

    def test_jealous_detection(self):
        hint = heuristic_self_emotion("哦，你们关系挺好啊")
        self.assertIsNotNone(hint)
        self.assertEqual(hint[0], "jealous")

    def test_plain_text_no_hint(self):
        self.assertIsNone(heuristic_self_emotion(""))

    def test_tag_regex(self):
        text = "哼<emotion>害羞</emotion>才没有"
        match = EMOTION_TAG_RE.search(text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "害羞")
        self.assertEqual(EMOTION_TAG_RE.sub("", text), "哼才没有")


class TestRelation(unittest.TestCase):

    def test_resolve_prototype(self):
        self.assertEqual(resolve_prototype("开心").key, "joy")
        self.assertEqual(resolve_prototype("有点害羞").key, "shy")
        self.assertIsNone(resolve_prototype("完全不存在的词"))


class _Clock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestEngine(unittest.TestCase):
    def _make(self, tmp: str, clock: _Clock) -> EmotionEngine:
        settings = Settings.from_config(
            {"persona": {"preset": "tsundere"}}
        )
        engine = EmotionEngine(settings, Path(tmp), clock=clock)
        engine.load()
        return engine

    def test_decays_back_to_baseline(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            engine.apply_stimulus(session, Appraiser().appraise("你好棒") or None, None, clock())
            self.assertGreater(session.emotion.p, engine.settings.baseline.p)
            clock.advance(3600)  # 一小时
            engine.resolve("umo", clock())
            self.assertAlmostEqual(session.emotion.p, engine.settings.baseline.p, places=2)


    def test_idle_applies_once(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            clock.advance(6 * 3600)
            self.assertTrue(engine.apply_idle(session, clock()))
            self.assertFalse(engine.apply_idle(session, clock()))  # 同一段空闲只结算一次


    def test_declared_emotion_hint_expires(self):
        """规则声明的情绪标签只在 hint_hold 时间内盖过几何结果，之后自动过期。"""
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            relation = engine.relation_of(session, "42", now=clock())
            stim = engine.appraiser.appraise("我发烧了")
            engine.apply_stimulus(session, stim, relation, clock())
            self.assertEqual(engine.snapshot(session, relation, clock()).proto.key, "anxious")
            clock.advance(engine.settings.hint_hold + 10)
            after = engine.snapshot(session, relation, clock())
            self.assertNotEqual(after.proto.key, "anxious")
            # 几何最近邻接管
            self.assertEqual(after.proto.key, after.raw_proto.key)


    def test_freeze_stops_decay_and_stimulus(self):
        """冻结 = 情绪完全停住：不随时间衰减，聊天也动不了它。"""
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            engine.apply_stimulus(session, Appraiser().appraise("你好棒啊"), None, clock())
            before = engine.snapshot(session, None, clock()).emotion

            self.assertTrue(engine.freeze("umo"))
            # ① 快进两小时的后台维护：一丁点都不许衰减
            clock.advance(7200)
            engine.resolve("umo", clock())
            engine.tick_all(clock())
            held = engine.snapshot(session, None, clock()).emotion
            self.assertEqual(held.to_dict(), before.to_dict(), "冻结了还在衰减")

            # ② 冻结中来消息也不接受情绪冲击
            engine.apply_stimulus(session, Appraiser().appraise("你就是个废物，滚"), None, clock())
            self.assertEqual(
                engine.snapshot(session, None, clock()).emotion.to_dict(),
                before.to_dict(),
                "冻结中还接受了情绪冲击",
            )

            # ③ 解冻：从这一刻才开始衰减（冻结的两小时不补扣）
            self.assertTrue(engine.unfreeze("umo"))
            clock.advance(1)
            engine.resolve("umo", clock())
            engine.tick_all(clock())
            moved = engine.snapshot(session, None, clock()).emotion
            self.assertLess(moved.p, before.p, "解冻后应当恢复衰减")
            self.assertTrue(engine.freeze("umo"))
            self.assertFalse(engine.freeze("umo"))  # 重复冻结没意义


    def test_frozen_flag_survives_reload(self):
        """冻结状态要跟着 emotion_state.json 走 —— 重启不该自己解冻。"""
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            engine.resolve("umo", clock())
            # 面板/指令是直接改集合的（没走 _dirty），flush 也要发现得了
            engine.frozen.add("umo")
            self.assertTrue(engine.flush(clock()))

            reborn = self._make(tmp, clock)
            self.assertIn("umo", reborn.frozen, "重启后冻结丢了")
            # 冻结中的会话：快进时间不衰减
            session = reborn.resolve("umo", clock())
            pad_before = session.emotion.to_dict()
            clock.advance(7200)
            reborn.tick_all(clock())
            self.assertEqual(session.emotion.to_dict(), pad_before)

            self.assertTrue(reborn.unfreeze_all())
            self.assertEqual(reborn.frozen, set())


    def test_state_round_trip(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            relation = engine.relation_of(session, "42", "小明", clock())
            engine.touch(session, relation, clock())
            engine.apply_stimulus(session, Appraiser().appraise("你好厉害"), relation, clock())
            self.assertTrue(engine.flush(clock()))

            reborn = self._make(tmp, clock)
            restored = reborn.resolve("umo", clock())
            self.assertAlmostEqual(restored.emotion.p, session.emotion.p, places=3)
            self.assertIn("42", restored.users)
            self.assertEqual(restored.users["42"].name, "小明")

    def test_corrupted_file_does_not_crash(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "emotion_state.json"
            target.write_text("{ 这不是合法 json", encoding="utf-8")
            engine = self._make(tmp, clock)
            self.assertEqual(engine.session_count, 0)
            self.assertTrue(engine.flush(clock()))

    def test_store_reports_failure_quietly(self):
        store = JsonStore(Path(BAD_WRITE_DIR) / "state.json")
        self.assertFalse(store.save({"a": 1}))

    def test_snapshot_crisis_window(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            relation = engine.relation_of(session, "42", now=clock())
            stim = engine.appraiser.appraise("我不想活了")
            engine.apply_stimulus(session, stim, relation, clock())
            self.assertTrue(engine.snapshot(session, relation, clock()).crisis)
            clock.advance(engine.settings.crisis_hold + 10)
            self.assertFalse(engine.snapshot(session, relation, clock()).crisis)


class TestIntensity(unittest.TestCase):
    """强度是**可调节的主动产物**：能被设定、会主动衰减、冻结时一起停住。"""

    def _make(self, tmp: str, clock: _Clock, config: dict | None = None) -> EmotionEngine:
        settings = Settings.from_config(config or {})
        engine = EmotionEngine(settings, Path(tmp), clock=clock)
        engine.load()
        return engine

    def test_decay_strategies(self):
        """配置里选的衰减策略必须真的生效（指数 / 线性 / 关闭）。"""
        self.assertAlmostEqual(
            decay_intensity(0.8, 0.2, 100.0, 100.0, "exponential"), 0.5, places=6
        )
        self.assertAlmostEqual(
            decay_intensity(0.8, 0.2, 100.0, 100.0, "linear"), 0.2, places=6
        )
        self.assertEqual(decay_intensity(0.8, 0.2, 100.0, 100.0, "off"), 0.8)
        self.assertEqual(decay_intensity(0.8, 0.2, 100.0, 0.0, "exponential"), 0.8)

    def test_decay_strategy_lives_in_the_emotion_config(self):
        """衰减策略读自情绪配置 emotion.intensity.*，默认值也在这儿。"""
        settings = Settings.from_config({
            "intensity": {
                "decay": "linear",
                "half_life": 600,
                "floor": 0.2,
                "gain": 0.9,
                "default": 0.5,
            }
        })
        self.assertEqual(settings.intensity_decay, "linear")
        self.assertEqual(settings.intensity_half_life, 600.0)
        self.assertAlmostEqual(settings.intensity_floor, 0.2, places=6)
        self.assertAlmostEqual(settings.intensity_gain, 0.9, places=6)
        self.assertAlmostEqual(settings.intensity_default, 0.5, places=6)
        # 没配：用内置默认（指数衰减，15 分钟半衰期，停在 0.15）
        defaults = Settings.from_config({})
        self.assertEqual(defaults.intensity_decay, "exponential")
        self.assertEqual(defaults.intensity_half_life, 900.0)
        self.assertAlmostEqual(defaults.intensity_floor, 0.15, places=6)
        # 乱写的策略名兜回指数衰减，不会让引擎跑飞
        weird = Settings.from_config({"intensity": {"decay": "乱写的"}})
        self.assertEqual(weird.intensity_decay, "exponential")

    def test_intensity_is_adjustable_not_a_byproduct(self):
        """强度可以被直接设定，与 PAD 长度无关（这就是「主动产物」）。"""
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            engine.set_intensity(session, 0.9, clock())
            snap = engine.snapshot(session, None, clock())
            self.assertAlmostEqual(snap.intensity, 0.9, places=6)
            self.assertEqual(snap.level, 5)
            self.assertEqual(snap.level_name, "极强")
            self.assertEqual(intensity_percent(snap.intensity), 90)
            # PAD 一点没动，强度照样是 0.9 —— 不再由情绪推导
            self.assertNotEqual(snap.intensity, snap.pad_intensity)
            # 越界一律夹回 0~1
            self.assertAlmostEqual(engine.set_intensity(session, 99.0, clock()), 1.0, places=6)
            self.assertAlmostEqual(engine.set_intensity(session, -3.0, clock()), 0.0, places=6)
            # 等级口径跟着强度状态走
            self.assertEqual(level_of_intensity(0.4)[0], 3)
            self.assertEqual(level_of_intensity(0.9)[0], 5)

    def test_stimulus_raises_intensity_then_it_decays(self):
        """冲击会抬高强度，之后强度自己往回落（主动衰减）。"""
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            start = session_intensity(session)
            engine.apply_stimulus(
                session, engine.appraiser.appraise("你好棒啊真的好厉害"), None, clock()
            )
            peak = session_intensity(session)
            self.assertGreater(peak, start, "情绪冲击没有抬高强度")
            # 半衰期的几倍之后：明显回落，但不会跌破配置的目标值
            clock.advance(int(engine.settings.intensity_half_life * 3))
            engine.resolve("umo", clock())
            cooled = session_intensity(session)
            self.assertLess(cooled, peak, "强度没有主动衰减")
            self.assertGreaterEqual(cooled, engine.settings.intensity_floor - 1e-6)

    def test_freeze_holds_intensity_too(self):
        """冻结要关联强度：不衰减、聊天也改不了它，解冻才恢复。"""
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            engine.set_intensity(session, 0.85, clock())
            engine.set_emotion(session, PAD.of(0.6, 0.4, 0.2), clock())
            self.assertTrue(engine.freeze("umo"))
            self.assertAlmostEqual(session.frozen_intensity, 0.85, places=6)
            self.assertGreater(session.frozen_at, 0)

            clock.advance(7200)
            engine.tick_all(clock())
            self.assertAlmostEqual(
                session_intensity(session), 0.85, places=6, msg="冻结了强度还在衰减"
            )
            engine.apply_stimulus(
                session, engine.appraiser.appraise("你就是个废物，滚"), None, clock()
            )
            self.assertAlmostEqual(
                session_intensity(session), 0.85, places=6, msg="冻结中强度还被冲击改掉"
            )

            self.assertTrue(engine.unfreeze("umo"))
            self.assertEqual(session.frozen_at, 0.0)
            clock.advance(1800)
            engine.tick_all(clock())
            self.assertLess(session_intensity(session), 0.85, "解冻后强度没恢复衰减")

    def test_intensity_survives_reload_and_backfills_old_state(self):
        """强度要落盘；老存档没有它时按 PAD 回填一次。"""
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            engine.set_intensity(session, 0.66, clock())
            self.assertTrue(engine.flush(clock()))

            reborn = self._make(tmp, clock)
            restored = reborn.resolve("umo", clock())
            self.assertAlmostEqual(session_intensity(restored), 0.66, places=3)

            # 老存档：没有 intensity 字段
            legacy = {
                "version": 1,
                "saved_at": clock(),
                "preset": "default",
                "frozen": [],
                "sessions": {
                    "old": {
                        "emotion": {"p": 0.9, "a": 0.0, "d": 0.0},
                        "mood": {"p": 0.0, "a": 0.0, "d": 0.0},
                        "last_tick": clock(),
                        "last_interaction": clock(),
                        "users": {},
                        "history": [],
                    }
                },
            }
            (Path(tmp) / "emotion_state.json").write_text(
                json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
            )
            old_engine = self._make(tmp, clock)
            old = old_engine.resolve("old", clock())
            # 0.9 的 PAD 长度换算过来 ≈ 0.667
            self.assertAlmostEqual(session_intensity(old), 0.9 / 1.35, places=3)

    def test_prompt_injects_intensity(self):
        """情绪强度必须进提示词（而且是我们这个强度，不是 PAD 推的）。"""
        from mind.emotion.prompt import render_state_block

        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            engine.set_intensity(session, 0.72, clock())
            snap = engine.snapshot(session, None, clock())
            text = render_state_block(snap)
            self.assertIn("情绪强度：72%", text)
            self.assertIn("强度 72%", text)
            self.assertIn("4/5", text)

    def test_intensity_curve_rebuilt_for_the_panel(self):
        """曲线面板上的强度曲线：会衰减、冻结走平、关闭策略时是直线。"""
        from mind.panel import build_intensity_curve

        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock)
            session = engine.resolve("umo", clock())
            engine.set_intensity(session, 0.95, clock())
            for _ in range(3):
                clock.advance(60)
                engine.tick_all(clock())
            engine.set_intensity(session, 0.4, clock())
            until = clock()
            since = until - 600
            args = dict(
                history=session.history,
                current=session_intensity(session),
                floor=engine.settings.intensity_floor,
                half_life=engine.settings.intensity_half_life,
                mode="exponential",
                since=since,
                until=until,
                points=60,
            )
            curve = build_intensity_curve(**args)
            self.assertEqual(len(curve), 60)
            self.assertTrue(all(0.0 <= point["v"] <= 1.0 for point in curve))
            self.assertAlmostEqual(curve[-1]["v"], session_intensity(session), places=3)
            # 关闭衰减 + 没有事件：除了尾巴上的当前值，整段都停在**基准值**
            flat = build_intensity_curve(**dict(args, mode="off", history=[]))
            head = [point["v"] for point in flat[:-1]]
            self.assertTrue(
                all(abs(value - args["floor"]) < 1e-6 for value in head),
                f"没数据的时段没按基准值填：{head[:5]}",
            )
            self.assertAlmostEqual(
                flat[-1]["v"], session_intensity(session), places=3,
                msg="尾巴要落在当前强度上",
            )
            # 冻结：从冻结点起保持原值
            frozen = build_intensity_curve(
                **dict(args, frozen=True, frozen_at=until - 300, frozen_intensity=0.66)
            )
            tail = [point for point in frozen if point["t"] >= until - 300]
            self.assertTrue(tail)
            self.assertTrue(all(abs(point["v"] - 0.66) < 1e-6 for point in tail))


    def test_intensity_curve_baseline_does_not_move_when_adjusted(self):
        """强度曲线的基线要钉死：没数据的时段填基准值，
        手动调节只改有数据之后的部分，不会把整条历史一起挪走。"""
        from mind.panel import build_intensity_curve

        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._make(tmp, clock, {"intensity": {"floor": 0.2}})
            session = engine.resolve("umo", clock())
            until = clock()
            since = until - 3600
            args = dict(
                current=session_intensity(session),
                floor=engine.settings.intensity_floor,
                half_life=engine.settings.intensity_half_life,
                mode="exponential",
                since=since,
                until=until,
                points=40,
            )

            # ① 一条事件都没有：整段填基准值，尾巴是当前值
            empty = build_intensity_curve([], **args)
            head = [point["v"] for point in empty[:-1]]
            self.assertTrue(
                all(abs(value - 0.2) < 1e-6 for value in head),
                f"没数据时没填基准值：{head[:5]}",
            )
            self.assertAlmostEqual(empty[-1]["v"], args["current"], places=4)

            # ② 手动把强度拉到 0.9：**历史段（0.2 那段）不许跟着跑**
            engine.set_intensity(session, 0.9, clock())
            until = clock()
            args["current"] = session_intensity(session)
            args["until"] = until
            adjusted = build_intensity_curve(session.history, **args)
            early = [point for point in adjusted if point["t"] <= since + 60]
            self.assertTrue(early, "起点那一段没了")
            self.assertTrue(
                all(abs(point["v"] - 0.2) < 1e-6 for point in early),
                f"手动调节把基线也带跑了：{early}",
            )
            self.assertAlmostEqual(
                adjusted[-1]["v"], 0.9, places=3, msg="尾巴要落在新的强度上"
            )

    def test_intensity_floor_is_read_from_the_emotion_config(self):
        """衰减最低值（基准值）是情绪配置里的一项，面板能读到它。"""
        settings = Settings.from_config({"intensity": {"floor": 0.42}})
        self.assertAlmostEqual(settings.intensity_floor, 0.42, places=6)
        # 配置文件里得有这一项（面板「基准值」滑块就是写它）
        import json as _json

        schema = _json.loads(
            (PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8")
        )
        floor_node = schema["emotion"]["items"]["intensity"]["items"]["floor"]
        self.assertEqual(floor_node["type"], "float")
        self.assertIn("最低值", floor_node["description"] + floor_node.get("hint", ""))

class TestMoodLabel(unittest.TestCase):
    """心境判定：不许永远停在「平静」，也不许乱标。"""

    BASELINE = (0.08, 0.22, 0.30)

    def test_mood_label_uses_direction_when_close_to_baseline(self):
        from mind.emotion.model import classify_mood

        baseline = PAD.of(*self.BASELINE)

        # ① 就在基线上：平静（不许乱标）
        self.assertEqual(classify_mood(baseline, baseline).key, "calm")
        # ② 心境跑到某个锚点附近：绝对坐标直接命中
        self.assertEqual(
            classify_mood(PAD.of(-0.80, -0.15, -0.60), baseline).key, "sad"
        )
        # ③ 只比基线差一点（绝对坐标会判成平静）→ 按**方向**给出心境
        drifted = PAD.of(*self.BASELINE) + PAD.of(-0.16, -0.10, -0.12)
        self.assertEqual(
            classify(drifted).key, "calm",
            "这条本来就该判成平静，否则测不到新逻辑",
        )
        self.assertNotEqual(
            classify_mood(drifted, baseline).key, "calm",
            "心境已经明显偏离基线却还显示平静 —— 判定等于没判",
        )
        # ④ 偏移小于死区：不许瞎猜
        tiny = PAD.of(*self.BASELINE) + PAD.of(0.04, 0.0, 0.0)
        self.assertEqual(classify_mood(tiny, baseline).key, "calm")

    def test_snapshot_mood_uses_the_direction_rule(self) -> None:
        """快照里的心境标签也得走同一套口径。"""
        from mind.emotion.model import classify_mood

        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = TestIntensity()._make(tmp, clock)
            session = engine.resolve("umo", clock())
            snap = engine.snapshot(session, None, clock())
            self.assertEqual(snap.mood_proto.key, "calm", "刚起步的心境就该是平静")
            session.mood = (
                PAD.from_any(engine.settings.baseline) + PAD.of(-0.16, -0.10, -0.12)
            )
            snap = engine.snapshot(session, None, clock())
            self.assertNotEqual(
                snap.mood_proto.key, "calm", "心境偏了，标签要跟着变"
            )
            self.assertEqual(
                snap.mood_proto.key, classify_mood(session.mood, engine.settings.baseline).key
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)

