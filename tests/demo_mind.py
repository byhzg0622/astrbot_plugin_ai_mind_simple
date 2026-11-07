"""终端版"心智面板"：不用开 WebUI 也能看到情绪曲线和抽取到的记忆。

不依赖 AstrBot、不调用任何模型 API —— 抽取结果用预置数据模拟。

    python3 tests/demo_mind.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from mind import (  # noqa: E402
    EmotionEngine,
    ExtractedItem,
    MemoryExtractor,
    MemoryStore,
    Retriever,
    SampleStore,
    Settings,
    build_curve,
    classify,
    render_memory_block,
    should_sample,
)
from mind.config import MindSettings, PanelSettings  # noqa: E402
from mind.emotion.engine import session_intensity  # noqa: E402
from mind.emotion.model import level_of_intensity  # noqa: E402
from mind.panel import emotion_payload  # noqa: E402

PRIVATE = "aiocqhttp:FriendMessage:1000000001"
GROUP = "aiocqhttp:GroupMessage:123456"
OWNER = "1000000001"
BAR = "─" * 78

BLOCKS = "▁▂▃▄▅▆▇█"


class Clock:
    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


#: (距上一句的秒数, 会话, 内容, 模型会抽出的记忆)
SCRIPT = (
    (0, PRIVATE, "我特别喜欢喝冰美式，一天两杯那种",
     [ExtractedItem("他特别喜欢喝冰美式，一天两杯", ("冰美式", "咖啡"), "preference", True, 0.85)]),
    (40, PRIVATE, "宝宝", []),
    (60, PRIVATE, "你今天真的好棒啊，我超喜欢你", []),
    (120, PRIVATE, "我兄弟今天给我买了个礼物", []),
    (900, PRIVATE, "我发烧了，头好疼",
     [ExtractedItem("他前几天发烧了", ("发烧", "生病"), "event", False, 0.5)]),
    (3600, PRIVATE, "在干嘛", []),
    (2 * 3600, PRIVATE, "我今天搬家了",
     [ExtractedItem("他搬家了", ("搬家", "住址"), "event", False, 0.6)]),
    (60, GROUP, "他最近怎么样", []),
    (3600, PRIVATE, "我不想活了，撑不下去了", []),
)


def sparkline(values: list[float], width: int = 72) -> tuple[str, float, float]:
    """把序列压成一行 ▁▂▃▄▅▆▇█，并**按实际范围自动缩放**。

    情绪大部分时间贴在基线上（事件之间衰减得很快），固定按 -1~1 画出来
    几乎是一条直线。自动缩放才能看清波形，代价是要把范围标出来 —— 所以
    这个函数把 min/max 一起返回。
    """
    if not values:
        return "", 0.0, 0.0
    low, high = min(values), max(values)
    span = high - low
    step = max(1, len(values) // width)
    sampled = values[::step][:width]
    out = []
    for value in sampled:
        ratio = 0.5 if span < 1e-9 else (value - low) / span
        out.append(BLOCKS[min(len(BLOCKS) - 1, int(ratio * len(BLOCKS)))])
    return "".join(out), low, high


def indent(text: str, prefix: str = "  │ ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def main() -> None:
    clock = Clock(time.mktime(time.strptime("2026-05-01 21:00", "%Y-%m-%d %H:%M")))
    config = {
        "emotion": {"persona": {"preset": "tsundere", "bot_name": "小怡"}},
        "memory": {},
        "panel": {"sample_epsilon": 0.002},
    }
    settings = MindSettings.from_config(config)
    store = MemoryStore(Path(tempfile.mkdtemp()) / "mind.db")
    samples = SampleStore(store.connection)
    engine = EmotionEngine(settings.emotion, Path(tempfile.mkdtemp()), clock=clock)
    engine.set_custom_rules([])
    extractor = MemoryExtractor(settings.memory)
    retriever = Retriever(settings.memory)

    print(BAR)
    print("AI 心智演示 —— 情绪曲线 + 长期记忆")
    print(f"情绪半衰期 {settings.emotion.emotion_half_life:.0f}s　"
          f"心境半衰期 {settings.emotion.mood_half_life:.0f}s　"
          f"采样灵敏度 {settings.panel.sample_epsilon}")
    print(BAR)

    for delay, session_key, text, items in SCRIPT:
        clock.advance(delay)
        now = clock()
        session = engine.resolve(session_key, now)
        relation = engine.relation_of(session, OWNER if session_key == PRIVATE else "999", "他", now)

        gap = max(0.0, now - session.last_interaction)
        engine.apply_idle(session, now)
        engine.touch(session, relation, now)
        session.last_gap = gap
        stimulus = engine.appraiser.appraise(text)
        if stimulus is not None:
            engine.apply_stimulus(session, stimulus, relation, now)
        samples.append(session_key, now, session.emotion, "stimulus")

        for item in items:
            extractor.integrate(store, [item], session_id=session_key, sender_id=OWNER, now=now)

        label = "私聊" if session_key == PRIVATE else "群聊"
        stamp = time.strftime("%m-%d %H:%M", time.localtime(now))
        proto = classify(session.emotion)
        level, level_name = level_of_intensity(session_intensity(session))
        print()
        print(f"[{stamp} · {label}] 他：{text}")
        if stimulus is not None:
            print(f"   ⇢ 触发「{stimulus.cause}」 Δ={stimulus.delta.pretty()}")
        if items:
            for item in items:
                print(f"   ✎ 记住：{item.content}")
        print(f"   ⇢ 此刻：{proto.emoji} {proto.name}（{level}/5 {level_name}）"
              f"　强度 {int(round(session_intensity(session) * 100))}%")

    # ---------------- 曲线 ----------------
    print()
    print(BAR)
    now = clock()
    since = now - 14 * 3600
    points = samples.since(PRIVATE, since, limit=5000)
    curve = build_curve(points, baseline=settings.emotion.baseline,
                        half_life=settings.emotion.emotion_half_life,
                        since=since, until=now, points=600)
    print(f"情绪波动线（最近 14 小时，{len(curve)} 个重算点，源自 {len(points)} 个采样）")
    print()
    for key, name in (("p", "愉悦 P"), ("a", "唤醒 A"), ("d", "掌控 D")):
        line, low, high = sparkline([pt[key] for pt in curve])
        print(f"  {name}  {high:+.2f} │{line}│ {low:+.2f}")
    print()
    print("  （纵向按实际范围自动缩放，两端标注的是真实数值）")
    print("  ├" + "─" * 72 + "┤")
    print(f"  {time.strftime('%m-%d %H:%M', time.localtime(since))}"
          f"{' ' * 52}{time.strftime('%m-%d %H:%M', time.localtime(now))}")

    # ---------------- 面板数据 ----------------
    print()
    print(BAR)
    payload = emotion_payload(
        engine=engine, sample_store=samples, session_key=PRIVATE, hours=14,
        max_points=600, now=now,
    )
    cur = payload["current"]
    print(f"面板会这样显示：{cur['emoji']} {cur['label']} · "
          f"强度 {int(round(cur['intensity'] * 100))}%（{cur['level']}/5 {cur['level_name']}）")
    cause = cur.get("cause") or {}
    if isinstance(cause, dict):
        cause_text = cause.get("text") or "—"
        if cause.get("message"):
            cause_text += f"　「{cause['message']}」"
    else:
        cause_text = cause or "—"
    print(f"  起因　{cause_text}")
    print(f"  心境　{payload['mood']['emoji']} {payload['mood']['label']}")
    print(f"  曲线　{len(payload['curve'])} 点（强度曲线 {len(payload['intensity_curve'])} 点）　"
          f"事件 {len(payload['events'])} 条　用户 {len(payload['users'])} 人")

    # ---------------- 记忆 ----------------
    print()
    print(BAR)
    stats = store.stats()
    print(f"AI 抽取到的记忆：共 {stats['total']} 条（常驻 {stats['pinned']} · 敏感 {stats['sensitive']}）")
    print()
    for memory in store.list_memories(limit=20):
        flag = "📌" if memory.pinned else "  "
        lock = "🔒" if memory.sensitive else "  "
        print(f"  {flag}{lock} #{memory.id} [{memory.kind}] {memory.content}")

    print()
    print("群聊里会带上哪些？（私聊记忆与敏感信息应当被挡住）")
    group_result = retriever.retrieve(
        store, query="他最近怎么样", session_id=GROUP, sender_id="999", now=now
    )
    block = render_memory_block(group_result, settings=settings.memory, session_id=GROUP, now=now)
    print(indent(block) if block else "  │ （什么都不带 —— 私事不外传）")

    print()
    print(BAR)
    print("演示结束：数据写在临时目录里，没有污染任何真实数据。")
    store.close()


if __name__ == "__main__":
    main()

