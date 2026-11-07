"""情绪状态的持久化。

- 数据统一存放在 AstrBot 的 data/plugin_data/<插件名>/ 目录下，
  绝不在插件自身目录里写文件，避免插件更新时数据被覆盖。
- 写入使用"临时文件 + os.replace"的原子替换，中途断电也不会写出半个 JSON。
- 会话数量有上限（LRU 淘汰），防止群特别多时文件无限膨胀。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class JsonStore:
    """一个极简的、容错的 JSON 状态仓库。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._backup = self.path.with_suffix(self.path.suffix + ".bak")

    # -- 读 -----------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        """读取状态文件；任何损坏都退化成空状态，绝不抛异常。"""
        for candidate in (self.path, self._backup):
            data = self._read_one(candidate)
            if data is not None:
                return data
        return {}

    @staticmethod
    def _read_one(path: Path) -> dict[str, Any] | None:
        try:
            if not path.is_file():
                return None
            raw = path.read_text(encoding="utf-8")
            if not raw.strip():
                return None
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError, UnicodeDecodeError):
            return None

    # -- 写 -----------------------------------------------------------------
    def save(self, data: dict[str, Any]) -> bool:
        """原子写入；成功返回 True，失败只返回 False（由调用方决定是否记日志）。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 先把旧文件留一份备份，主文件损坏时还能救回来
            if self.path.is_file():
                try:
                    if not self._backup.is_file() or (
                        time.time() - self._backup.stat().st_mtime > 300
                    ):
                        self._backup.write_bytes(self.path.read_bytes())
                except OSError:
                    pass
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            payload = json.dumps(data, ensure_ascii=False, indent=1)
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            return True
        except (OSError, TypeError, ValueError):
            return False

