"""单会话调试模式（隐藏开关）。

在指定的人于私聊里发一串约定好的口令时，**这一个会话**进入调试模式：插件完全让路，
忽略人格设定、直话直说，方便在真实聊天里查她到底怎么想的。

为什么要口令 + 指定 QQ 两道门槛：这是个能绕过人格的后门，不该被随手触发。
为什么不做进 Web UI：面板是公开给人看的，这种开关不该出现在上面被误触；
而且「面板上有没有这个状态」本身就会泄露它的存在。

状态存在 data/debug_sessions.json 里，重启还在 —— 调试到一半重启不该丢。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: 进入 / 退出的口令
DEBUG_CODE = "369951"
#: 只认这个 QQ
DEBUG_UID = "2020689845"


class DebugSessions:
    """哪些会话正处于调试模式。"""

    def __init__(self, path: Path, logger: Any = None) -> None:
        self.path = Path(path)
        self.logger = logger
        self.active: set[str] = set()
        self.load()

    def load(self) -> None:
        try:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
            items = data.get("active") if isinstance(data, dict) else data
            self.active = {str(item) for item in (items or []) if str(item)}
        except Exception as exc:  # noqa: BLE001 - 读不回来就当没开过
            if self.logger is not None:
                self.logger.warning(f"[ai_mind] 调试模式状态读不回来：{exc}")

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"active": sorted(self.active)}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            if self.logger is not None:
                self.logger.warning(f"[ai_mind] 调试模式状态存不下来：{exc}")

    def is_active(self, session_key: str) -> bool:
        return str(session_key) in self.active

    def toggle(self, session_key: str) -> bool:
        """开 / 关，返回切换后的状态。"""
        key = str(session_key)
        if key in self.active:
            self.active.discard(key)
            on = False
        else:
            self.active.add(key)
            on = True
        self.save()
        return on


__all__ = ["DEBUG_CODE", "DEBUG_UID", "DebugSessions"]

