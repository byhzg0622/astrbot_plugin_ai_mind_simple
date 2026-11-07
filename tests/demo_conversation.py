"""模拟一段真实对话，直观看看情绪怎么涨落、又怎么自己衰减回去。

不依赖 AstrBot，直接驱动情绪内核：

    python3 tests/demo_conversation.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from mind.emotion import EmotionEngine, Settings, build_injection  # noqa: E402
from mind.emotion.display import history_panel, mood_panel  # noqa: E402

SPECIAL = "1000000001"
UMO = f"aiocqhttp:FriendMessage:{SPECIAL}"

BAR = "─" * 68


class Clock:
    """可控时钟，让几个小时的衰减在脚本里瞬间发生。"""

    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


#: (距上一句的秒数, 内容)
SCRIPT: tuple[tuple[float, str], ...] = (
    (0, "在吗"),
    (25, "宝宝"),
    (40, "你今天真的好棒啊，我超喜欢你"),
    (90, "我兄弟今天给我买了个礼物"),
    (4 * 3600, "我发烧了，头好疼"),
    (600, "在干嘛"),
    (30, "我不想活了，撑不下去了"),
)


def indent(text: str, prefix: str = "    │ ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def main() -> None:
    clock = Clock(time.mktime(time.strptime("2026-05-03 21:30", "%Y-%m-%d %H:%M")))
    settings = Settings.from_config(
        {
            "persona": {"preset": "tsundere", "bot_name": "她"},
        }
    )

    with tempfile.TemporaryDirectory() as tmp:
        engine = EmotionEngine(settings, Path(tmp), clock=clock)
        engine.load()

        print(BAR)
        print(f"情绪参数：{settings.preset_key}　气质基线：{settings.baseline.pretty()}")
        print(f"即时情绪半衰期 {settings.emotion_half_life:.0f}s　心境半衰期 {settings.mood_half_life:.0f}s")
        print(BAR)

        for delay, text in SCRIPT:
            clock.advance(delay)
            now = clock()

            session = engine.resolve(UMO, now)
            relation = engine.relation_of(session, SPECIAL, "他", now)

            gap = max(0.0, now - session.last_interaction)
            engine.apply_idle(session, now)
            engine.touch(session, relation, now)
            session.last_gap = gap

            stimulus = engine.appraiser.appraise(text)
            if stimulus is not None:
                engine.apply_stimulus(session, stimulus, relation, now)

            snapshot = engine.snapshot(session, relation, now)
            stamp = time.strftime("%H:%M", time.localtime(now))

            print()
            print(f"[{stamp}] 他：{text}")
            if stimulus is not None:
                print(
                    f"   → 触发「{stimulus.cause}」 Δ={stimulus.delta.pretty()}"
                                    )
            else:
                print("   → 没触发任何情绪规则")
            print(
                f"   → 此刻：{snapshot.proto.emoji} {snapshot.proto.name}"
                f"（{snapshot.level}/5 {snapshot.level_name}）"
                
            )
            block = build_injection(snapshot, "normal", settings.bot_name)
            print(indent(block))

        print()
        print(BAR)
        session = engine.resolve(UMO, clock())
        relation = engine.relation_of(session, SPECIAL, "他", clock())
        snapshot = engine.snapshot(session, relation, clock())
        print(mood_panel(snapshot, settings))
        print()
        print(history_panel(snapshot))
        print(BAR)
        print("模拟结束：状态已写入临时目录，未污染任何真实数据。")


if __name__ == "__main__":
    main()

