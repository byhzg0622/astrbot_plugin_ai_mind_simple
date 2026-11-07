"""模拟一段跨天的对话，直观看看长期记忆是怎么长出来、又是怎么被想起来的。

不依赖 AstrBot，也不需要任何模型 API：
"模型抽取出了什么"用预置数据模拟（真实运行时由你配置的模型产出）。

    python3 tests/demo_memory.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from mind.memory import (  # noqa: E402
    ExtractedItem,
    Memory,
    MemoryExtractor,
    MemoryStore,
    Retriever,
    Settings,
    detect_sensitive,
    render_memory_block,
)
from mind.memory.display import main_panel  # noqa: E402

PRIVATE = "aiocqhttp:FriendMessage:1000000001"
GROUP = "aiocqhttp:GroupMessage:123456"
OWNER = "1000000001"
BAR = "─" * 74


class Clock:
    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, days: float) -> None:
        self.now += days * 86400.0


#: (第几天, 会话, 谁说的, 内容)
SCRIPT: tuple[tuple[float, str, str, str], ...] = (
    (0.0, PRIVATE, "他", "我特别喜欢喝冰美式，一天两杯那种"),
    (0.0, PRIVATE, "他", "对了下周三我要期末考试，好烦"),
    (0.0, PRIVATE, "他", "我家住在河南省孟州市黄河路12号，回头你来玩"),
    (1.0, PRIVATE, "他", "今天好累啊"),
    (2.0, PRIVATE, "他", "考试考砸了，心情很差"),
    (3.0, GROUP, "路人", "他最近怎么样"),
    (6.0, PRIVATE, "他", "想喝点东西"),
    (9.0, PRIVATE, "他", "我今天搬家了"),
)

#: 模拟"模型从这一轮里抽出了什么"。真实运行时由 LLM 产出。
EXTRACTION: dict[str, list[ExtractedItem]] = {
    "我特别喜欢喝冰美式，一天两杯那种": [
        ExtractedItem("他特别喜欢喝冰美式，一天两杯", ("冰美式", "咖啡"), "preference", True, 0.85, "对方")
    ],
    "对了下周三我要期末考试，好烦": [
        ExtractedItem("他下周三要期末考试", ("考试", "期末"), "event", False, 0.7, "对方")
    ],
    "我家住在河南省孟州市黄河路12号，回头你来玩": [
        ExtractedItem("他家住在河南省孟州市黄河路12号", ("住址",), "fact", True, 0.9, "对方")
    ],
    "考试考砸了，心情很差": [
        ExtractedItem("他期末考试考砸了，心情很差", ("考试", "考砸"), "event", False, 0.6, "对方")
    ],
}


def indent(text: str, prefix: str = "    │ ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def main() -> None:
    clock = Clock(time.mktime(time.strptime("2026-05-01 21:00", "%Y-%m-%d %H:%M")))
    settings = Settings.from_config({})
    store = MemoryStore(Path(tempfile.mkdtemp()) / "demo.db")
    extractor = MemoryExtractor(settings)
    retriever = Retriever(settings)

    print(BAR)
    print("长期记忆演示 —— 记忆怎么长出来、怎么被想起来、又怎么被挡住")
    print(f"抽取阈值：攒 {settings.batch_turns} 轮或静默 {settings.idle_flush_seconds:.0f}s")
    print(BAR)

    for day, session, who, text in SCRIPT:
        clock.advance(day)
        now = clock()
        sender = OWNER if who != "路人" else "999"
        label = "私聊" if session == PRIVATE else "群聊"
        stamp = time.strftime("%m-%d %H:%M", time.localtime(now))

        print()
        print(f"[第 {int(day) + 1} 天 {stamp} · {label}] {who}：{text}")

        # 1) 先把这一轮记进记忆（模拟后台抽取完成）
        for item in EXTRACTION.get(text, []):
            added, merged, _ = extractor.integrate(
                store, [item], session_id=session, sender_id=sender, now=now
            )
            scope = "私聊可见" if not detect_sensitive(item.content) else "🔒仅私聊"
            if added:
                print(f"   ✎ 记住了：{item.content}　[{scope}]")
            elif merged:
                print(f"   ✎ 更新了已有记忆：{item.content}")

        # 2) 再看这一轮会带上哪些记忆
        result = retriever.retrieve(
            store, query=text, session_id=session, sender_id=sender, now=now
        )
        if not result:
            print("   → 这一轮没有带上任何记忆")
            if result.private_blocked:
                print("   → （有私聊里知道的记忆，因为这里是群聊没有带进来）")
            continue

        block = render_memory_block(result, settings=settings, session_id=session, now=now)
        print(indent(block))

    print()
    print(BAR)
    now = clock()
    result = retriever.retrieve(store, query="在吗", session_id=PRIVATE, sender_id=OWNER, now=now)
    print(main_panel(store=store, result=result, settings=settings, session_id=PRIVATE, now=now))
    print()
    print("敏感记忆清单（永远不出现在群聊）：")
    for memory in store.list_memories(sensitive=True, limit=10):
        print(f"  🔒 #{memory.id} {memory.content}")
    print(BAR)
    print(f"演示结束：共 {store.count()} 条记忆，写在临时目录里，没有污染任何真实数据。")
    store.close()


if __name__ == "__main__":
    main()

