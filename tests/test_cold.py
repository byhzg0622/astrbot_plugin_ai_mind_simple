"""心境基线 / 超基线：「很长时间不找机器人」体现在心境上。

要卡住的三件事：

1. **设置只住在配置页**：心境基线（相对气质基线的偏移）、超基线偏移、
   超基线触发时长三个配置项读得出来、默认值和代码一致；情绪曲线页不放它们的
   控件，只拿一个「零点」（mood_baseline）用来归零滑杆与迷你条 ——
   所以配置调过基线，页面上看起来依然一切正常。
2. **触发有门槛**：心境必须在**基线（平静）**上连续待够触发时长，
   **且**这段时间没有对话，才开始被拉向超基线；只满足其中一个不算。
3. **触发之后真的变冷淡**，起因也改成「很久没说话了」；
   有人再说话（或手动设定/重置心境、心境冻结）都会让它停下来。

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
    SessionMood,
    Settings,
    humanize_seconds,
)
from mind.emotion.engine import (  # noqa: E402
    CAUSE_NO_CONTACT,
    format_mood_cause,
)
from mind.emotion.moodlabel import DEAD_ZONE  # noqa: E402

SCHEMA_PATH = PLUGIN_DIR / "_conf_schema.json"
HTML_PATH = PLUGIN_DIR / "pages" / "mind" / "index.html"
#: 配置里的三个新项（配置页有、曲线页没有）
CONFIG_KEYS = ("mood_baseline", "mood_cold", "mood_cold_after_minutes")


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


def _run(engine: EmotionEngine, clock: _Clock, seconds: float, step: float = 300.0) -> None:
    """模拟后台维护循环：每 step 秒 tick_all 一次（线上是 20 秒一圈，同一条路）。"""
    end = clock() + seconds
    while clock() < end:
        clock.advance(step)
        engine.tick_all(clock())


class MoodBaselineConfigTest(unittest.TestCase):
    """基线 / 超基线 / 触发时长：配置页里设得出来、代码读得到、默认值一致。"""

    def test_defaults_put_the_rest_point_on_the_temperament_baseline(self) -> None:
        settings = Settings.from_config({})
        self.assertEqual(
            settings.mood_rest.to_dict(), settings.baseline.to_dict(),
            "没配偏移时心境基线就该等于气质基线",
        )
        cold = settings.mood_cold
        base = PAD.from_any(settings.baseline)
        self.assertAlmostEqual(cold.p, base.p - 0.30, places=6, msg="默认朝冷淡：愉悦更低")
        self.assertAlmostEqual(cold.a, base.a - 0.12, places=6)
        self.assertAlmostEqual(cold.d, base.d + 0.35, places=6, msg="有底气的冷淡，不是低落")
        self.assertEqual(settings.mood_cold_after_minutes, 360.0, "默认 6 小时")

    def test_the_three_keys_are_read_from_the_emotion_config(self) -> None:
        settings = Settings.from_config({
            "dynamics": {
                "mood_baseline": {"p": -0.25, "a": -0.10, "d": 0.05},
                "mood_cold": {"p": -0.20, "a": -0.30, "d": 0.40},
                "mood_cold_after_minutes": 90,
            }
        })
        base = PAD.from_any(settings.baseline)
        rest = settings.mood_rest
        self.assertAlmostEqual(rest.p, base.p - 0.25, places=6, msg="心境基线 = 气质基线 + 偏移")
        self.assertAlmostEqual(rest.a, base.a - 0.10, places=6)
        self.assertAlmostEqual(rest.d, base.d + 0.05, places=6)
        # 超基线是相对**心境基线**的，不是相对气质基线
        cold = settings.mood_cold
        self.assertAlmostEqual(cold.p, rest.p - 0.20, places=6)
        self.assertAlmostEqual(cold.a, rest.a - 0.30, places=6)
        self.assertAlmostEqual(cold.d, rest.d + 0.40, places=6)
        self.assertEqual(settings.mood_cold_after_minutes, 90.0)

    def test_negative_duration_is_clamped_to_zero(self) -> None:
        settings = Settings.from_config({"dynamics": {"mood_cold_after_minutes": -5}})
        self.assertEqual(settings.mood_cold_after_minutes, 0.0)

    def test_the_keys_flow_through_the_plugin_settings_slice(self) -> None:
        """整条链路：配置页写 emotion.dynamics.* → 插件总成 → 情绪引擎。"""
        from mind.config import MindSettings

        settings = MindSettings.from_config({
            "emotion": {"dynamics": {
                "mood_baseline": {"p": 0.10, "a": -0.05, "d": 0.0},
                "mood_cold": {"p": -0.30, "a": -0.12, "d": 0.35},
                "mood_cold_after_minutes": 120,
            }},
        })
        emotion = settings.emotion
        self.assertAlmostEqual(emotion.mood_rest.p, emotion.baseline.p + 0.10, places=6)
        self.assertAlmostEqual(emotion.mood_rest.a, emotion.baseline.a - 0.05, places=6)
        self.assertEqual(emotion.mood_cold_after_minutes, 120.0)
        # 没配的那一块要用默认值，不能被别处带偏
        self.assertAlmostEqual(emotion.mood_cold.d, emotion.mood_rest.d + 0.35, places=6)

    def test_the_curve_page_only_gets_the_cold_state_not_the_settings(self) -> None:
        """设置不下发，但**状态**要下发 —— 否则右上角那句提示会说反话。"""
        from mind.panel import emotion_payload

        def payload() -> dict:
            return emotion_payload(
                engine=engine, sample_store=None, session_key="k",
                hours=1, max_points=50, now=clock(),
            )

        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock, {"dynamics": {"mood_cold_after_minutes": 60}})
            session = engine.resolve("k", clock())

            self.assertFalse(payload()["mood"]["cold"], "还没触发就该是 false")
            _run(engine, clock, 61 * 60)
            self.assertGreater(session.mood_cold_since, 0.0, "前置条件：已触发")
            data = payload()
            self.assertTrue(data["mood"]["cold"], "触发了要告诉面板，提示才不会说反话")
            for key in CONFIG_KEYS[1:]:
                self.assertNotIn(key, data, f"曲线页的数据包不该下发 {key}")

    def test_schema_defaults_match_the_code_defaults(self) -> None:
        """配置页的默认值必须和代码里的默认值同一个数 —— 否则改一次配置就跑偏。"""
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        dynamics = schema["emotion"]["items"]["dynamics"]["items"]
        settings = Settings.from_config({})
        for key in CONFIG_KEYS:
            self.assertIn(key, dynamics, f"配置页少了 {key}")
        self.assertEqual(
            dynamics["mood_cold_after_minutes"]["type"], "int",
            "触发时长是个分钟数，得用 int 才能在配置页里填",
        )
        self.assertEqual(
            dynamics["mood_cold_after_minutes"]["default"],
            int(settings.mood_cold_after_minutes),
        )
        for dim in ("p", "a", "d"):
            self.assertAlmostEqual(
                dynamics["mood_cold"]["items"][dim]["default"],
                getattr(settings.mood_cold_offset, dim),
                places=6,
                msg=f"超基线默认值和代码不一致（{dim}）",
            )
            self.assertEqual(dynamics["mood_baseline"]["items"][dim]["default"], 0.0)

    def test_the_settings_are_not_exposed_on_the_curve_page(self) -> None:
        """基线 / 超基线的**设置**只在配置页：曲线页只拿「零点」。"""
        html = HTML_PATH.read_text(encoding="utf-8")
        for key in CONFIG_KEYS[1:]:  # 超基线偏移与触发时长连提都不该提
            self.assertNotIn(key, html, f"情绪曲线页不该出现 {key} 的控件")

        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock, {"dynamics": {
                "mood_baseline": {"p": -0.25, "a": -0.10, "d": 0.05},
                "mood_cold": {"p": -0.30, "a": -0.12, "d": 0.35},
                "mood_cold_after_minutes": 90,
            }})
            from mind.panel import emotion_payload

            payload = emotion_payload(
                engine=engine, sample_store=None, session_key="k",
                hours=1, max_points=50, now=clock(),
            )
            for key in CONFIG_KEYS[1:]:
                self.assertNotIn(key, payload, f"曲线页的数据包不该下发 {key}")
                self.assertNotIn(key, payload.get("params", {}), f"params 不该下发 {key}")


class ColdTriggerTest(unittest.TestCase):
    """超基线的触发门槛：在基线（平静）上待够时长 + 这段时间没有对话。"""

    def _cold_engine(self, tmp: str, clock: _Clock, minutes: int = 60) -> EmotionEngine:
        return _engine(tmp, clock, {"dynamics": {"mood_cold_after_minutes": minutes}})

    def test_waits_for_the_whole_window_while_sitting_on_the_baseline(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._cold_engine(tmp, clock, minutes=60)
            session = engine.resolve("k", clock())
            rest = PAD.from_any(engine.settings.mood_rest)

            # ① 时长只走了一半：还在攒，不许动它
            _run(engine, clock, 30 * 60)
            self.assertEqual(session.mood_cold_since, 0.0, "还没到时长就触发了")
            self.assertGreater(session.mood_calm_since, 0.0, "在基线上就该在计时")
            self.assertLess(
                (session.mood - rest).magnitude(), DEAD_ZONE,
                "触发之前心境必须一直待在基线（平静）上",
            )

            # ② 撑满时长：触发，并且开始往超基线走
            _run(engine, clock, 31 * 60)
            self.assertGreater(session.mood_cold_since, 0.0, "时长够了就该触发")
            self.assertGreater(
                (session.mood - rest).magnitude(), 0.02,
                "触发之后心境要开始往超基线（冷淡）挪",
            )

    def test_silence_alone_is_not_enough_it_must_be_calm_on_the_baseline(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._cold_engine(tmp, clock, minutes=60)
            session = engine.resolve("k", clock())
            rest = PAD.from_any(engine.settings.mood_rest)
            # 十小时没人说话（远超触发时长），但心境一直停在基线**之外**
            session.last_interaction = clock() - 10 * 3600
            session.mood = rest + PAD.of(-0.6, 0.0, 0.0)
            session.mood_target = session.mood

            _run(engine, clock, 120 * 60)
            self.assertEqual(
                session.mood_calm_since, 0.0,
                "心境一直没回到基线（平静）",
            )
            self.assertEqual(
                session.mood_cold_since, 0.0,
                "光是没人说话还不够 —— 得先在基线上待够时长",
            )

    def test_default_window_is_six_hours(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock)
            session = engine.resolve("k", clock())
            self.assertEqual(engine.settings.mood_cold_after_minutes, 360.0)

            _run(engine, clock, 5 * 3600 + 55 * 60)
            self.assertEqual(session.mood_cold_since, 0.0, "5 小时 55 分还不该触发")

            _run(engine, clock, 10 * 60)
            self.assertGreater(session.mood_cold_since, 0.0, "满 6 小时就该触发")

    def test_zero_disables_the_whole_thing(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._cold_engine(tmp, clock, minutes=0)
            session = engine.resolve("k", clock())
            _run(engine, clock, 8 * 3600)
            self.assertEqual(session.mood_cold_since, 0.0, "配成 0 = 关掉超基线")
            rest = PAD.from_any(engine.settings.mood_rest)
            self.assertLess((session.mood - rest).magnitude(), DEAD_ZONE)

    def test_frozen_mood_never_turns_cold(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._cold_engine(tmp, clock, minutes=60)
            session = engine.resolve("k", clock())
            rest = PAD.from_any(engine.settings.mood_rest)
            self.assertTrue(engine.freeze_mood("k"))

            _run(engine, clock, 8 * 3600)
            self.assertEqual(session.mood_cold_since, 0.0, "心境冻着就完全不动")
            self.assertEqual(session.mood.to_dict(), rest.to_dict())


class ColdBehaviourTest(unittest.TestCase):
    """触发之后：真的变冷淡、说清为什么、有人来就松手。"""

    def _triggered(self, tmp: str, clock: _Clock) -> tuple[EmotionEngine, SessionMood]:
        engine = _engine(tmp, clock, {"dynamics": {"mood_cold_after_minutes": 60}})
        session = engine.resolve("k", clock())
        _run(engine, clock, 61 * 60)
        self.assertGreater(session.mood_cold_since, 0.0, "前置条件：超基线已经触发")
        return engine, session

    def test_turns_cold_and_says_it_is_because_nobody_talked(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine, session = self._triggered(tmp, clock)
            self.assertEqual(session.mood_cause, CAUSE_NO_CONTACT, "起因要写「很久没说话了」")

            # 起因文案不许写成「延续自…——不是新原因」：它不是对方说过的哪句话
            text = format_mood_cause(
                session.mood_cause, session.mood_cause_at, clock(),
                engine.settings.mood_live_window,
            )
            self.assertNotIn("延续", text)
            self.assertEqual(
                text,
                f"{CAUSE_NO_CONTACT}（{humanize_seconds(clock() - session.mood_cause_at)}）",
            )

            # 触发后约 1.5 小时（COLD_HALF_LIFE × ~2.8）就该判成「冷淡」
            _run(engine, clock, 120 * 60)
            label = session.mood_label(clock(), engine.settings)
            self.assertEqual(label.key, "cold", "超基线要真的体现在心境上")
            cold = PAD.from_any(engine.settings.mood_cold)
            self.assertLess(
                (session.mood - cold).magnitude(), 0.10,
                "心境要基本贴到超基线上",
            )

    def test_a_new_message_lifts_the_cold_state(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine, session = self._triggered(tmp, clock)
            rest = PAD.from_any(engine.settings.mood_rest)
            far = (session.mood - rest).magnitude()
            self.assertGreater(far, 0.0, "前置条件：心境确实被拉离了基线")

            # 一条没触发任何情绪规则的消息：走 touch（线上每条消息都会 touch 一次）
            relation = engine.relation_of(session, "u1", "他", clock())
            engine.touch(session, relation, clock())
            _run(engine, clock, 1 * 60)  # 后台下一拍
            self.assertEqual(session.mood_cold_since, 0.0, "有人来了就该解除超基线")

            _run(engine, clock, 50 * 60)  # 一个对话窗口多一点（总静默 55 分钟 < 60 分钟时长）
            self.assertEqual(session.mood_cold_since, 0.0, "时长还没重新攒够，不许再触发")
            self.assertLess(
                (session.mood - rest).magnitude(), far,
                "心境要往基线（平静）回落",
            )
            self.assertLess(
                (session.mood - rest).magnitude(), DEAD_ZONE,
                "聊过之后该回到平静",
            )

    def test_a_matched_message_also_lifts_it(self) -> None:
        """带情绪冲击的消息（会改情绪的那种）同样要解除超基线 ——
        之后心境跟着这次互动的情绪走（A3/A4），那是另一码事。"""
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine, session = self._triggered(tmp, clock)

            engine.apply_stimulus(
                session, engine.appraiser.appraise("嗯"), None, clock()
            )
            _run(engine, clock, 1 * 60)
            self.assertEqual(session.mood_cold_since, 0.0, "带情绪的对话同样解除超基线")

    def test_manual_set_clears_the_cold_state(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine, session = self._triggered(tmp, clock)
            rest = PAD.from_any(engine.settings.mood_rest)

            engine.set_mood(session, rest + PAD.of(0.40, 0.20, 0.10), clock())
            self.assertEqual(session.mood_cold_since, 0.0, "手动设定要能压过超基线")
            self.assertEqual(session.mood_calm_since, 0.0, "计时按新位置重新起算")

            # 心境被手动放在基线之外：光等是等不来下一次触发的
            _run(engine, clock, 61 * 60)
            self.assertEqual(session.mood_cold_since, 0.0)

    def test_reset_puts_it_back_on_the_baseline(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine, session = self._triggered(tmp, clock)
            rest = PAD.from_any(engine.settings.mood_rest)

            engine.reset_mood(session, clock())
            self.assertEqual(session.mood.to_dict(), rest.to_dict())
            self.assertEqual(session.mood_cold_since, 0.0, "重置要解除超基线")
            self.assertGreater(session.mood_calm_since, 0.0, "重置 = 回到基线，计时重新开始")

    def test_the_cold_timers_survive_a_restart(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine, session = self._triggered(tmp, clock)
            engine.flush(clock())

            reloaded = _engine(tmp, clock)
            again = reloaded.resolve("k", clock())
            self.assertGreater(again.mood_cold_since, 0.0, "重启后不能把超基线状态弄丢")
            self.assertEqual(again.mood_calm_since, session.mood_calm_since)
            self.assertEqual(again.mood_cold_since, session.mood_cold_since)

    def test_snapshot_carries_the_mood_baseline(self) -> None:
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _engine(tmp, clock, {"dynamics": {
                "mood_baseline": {"p": -0.25, "a": -0.10, "d": 0.05},
            }})
            session = engine.resolve("k", clock())
            snap = engine.snapshot(session, None, clock())
            self.assertIsNotNone(snap.mood_baseline)
            self.assertEqual(snap.mood_baseline.to_dict(), engine.settings.mood_rest.to_dict())
            # 气质基线仍然原样留着：曲线那条虚线用的是它
            self.assertEqual(snap.baseline.to_dict(), engine.settings.baseline.to_dict())


if __name__ == "__main__":
    unittest.main()
