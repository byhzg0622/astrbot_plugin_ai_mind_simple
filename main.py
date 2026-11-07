"""AstrBot 心智插件 —— 情绪 + 长期记忆，外加一块可视化面板。

把原来两个插件合成了一个：

- **情绪**：PAD 三维模型，随时间自然衰减，按消息内容做规则化情绪评估，
  识别到危机信号时强制进入认真关心模式。
  **插件只把"她此刻是什么心情"告诉模型** —— 不带任何表达基调、不说她该怎么说话，
  说话方式完全交给机器人自己的人设提示词。
- **记忆**：攒批交给模型抽取长期事实，按「常驻 + 最近 + 相关」三路检索注入，
  隐私优先（敏感信息只在本人私聊出现）。

合并后新增：

- **情绪曲线**：情绪按时间采样存进 SQLite，面板上能看到它怎么涨、怎么落、
  怎么自己衰减回基线。
- **可控**：可以在面板上直接拖点改历史、设定此刻的心情、冻结/重置情绪。
- **可查**：面板上能直接看到 AI 到底抽取了哪些记忆，并增删改。

面板页面在 `pages/mind/index.html`，由 AstrBot 自动发现；
后端接口通过 `context.register_web_api` 挂在 `/api/plug/<插件目录名>/` 下。

数据统一落在 data/plugin_data/astrbot_plugin_ai_mind/ 下的两个文件：
`emotion_state.json`（状态快照）与 `mind.db`（记忆 + 情绪曲线）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import OrderedDict
from pathlib import Path
from urllib import parse as urllib_parse
from typing import Any, Iterable

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools

# ---------------------------------------------------------------------------
# 抢回自己的包名：绝不让别的插件的同名 mind 包顶替
# ---------------------------------------------------------------------------
# Python 里 sys.modules 谁先注册谁赢。如果另一个插件（合并前那两个）已经
# import 过它自己的 mind 包，我们再 from mind.xxx import ... 拿到的就是
# **别人那份代码** —— 日志里的模块名会是光秃秃的 mind.samples，而不是我们的
# astrbot_plugin_ai_mind.mind.samples。后果：改自己的文件改十几版，
# 跑的还是别人的代码。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _reclaim_mind_package() -> list:
    """把不属于本插件目录的 mind.* 从 sys.modules 里清掉。"""
    dropped: list = []
    for name in [n for n in list(sys.modules) if n == "mind" or n.startswith("mind.")]:
        module = sys.modules.get(name)
        where = os.path.abspath(str(getattr(module, "__file__", "") or ""))
        if where and _HERE in where:
            continue                      # 已经是我们自己的了
        sys.modules.pop(name, None)
        dropped.append(name)
    return dropped


_RECLAIMED = _reclaim_mind_package()
try:  # AstrBot 以包形式加载插件（data.plugins.<插件名>.main）
    from .mind import (
        CRISIS_BLOCK,
        EMOTION_RULES_BLOCK,
        EMOTION_RULES_MARKER,
        MEMORY_RULES_BLOCK,
        MEMORY_RULES_MARKER,
        EMOTION_TAG_RE,
        PAD,
        EmotionEngine,
        PrivacySettings,
        decide_scope,
        derive_keywords,
        detect_sensitive,
        KINDS,
        KIND_LABELS,
        Memory,
        MemoryExtractor,
        MemoryStore,
        MindSettings,
        Retriever,
        LexiconStore,
        SessionMood,
        Snapshot,
        Turn,
        build_emotion_injection,
        cfg_get,
        classify,
        emotion_payload,
        format_existing_for_prompt,
        heuristic_self_emotion,
        is_group_session,
        level_of_intensity,
        memory_payload,
        mood_word_pad,
        parse_extraction,
        render_memory_block,
        resolve_mood_word,
        resolve_prototype,
        session_entries,
        session_intensity,
        should_sample,
    )
    from .mind.debugmode import DEBUG_CODE, DEBUG_UID, DebugSessions
    from .mind.textfix import strip_message_markup, tidy_cjk_spaces
    from .mind.emotion.display import detail_panel, help_panel, history_panel, mood_panel
    from .mind.memory.display import clear_panel, forget_panel, help_panel as memory_help_panel, main_panel as memory_main_panel, remember_panel, search_panel
except ImportError:  # 兜底：以单模块方式加载时，把插件目录加进 sys.path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mind import (  # type: ignore[no-redef]
        CRISIS_BLOCK,
        EMOTION_RULES_BLOCK,
        EMOTION_RULES_MARKER,
        MEMORY_RULES_BLOCK,
        MEMORY_RULES_MARKER,
        EMOTION_TAG_RE,
        PAD,
        EmotionEngine,
        PrivacySettings,
        decide_scope,
        derive_keywords,
        detect_sensitive,
        KINDS,
        KIND_LABELS,
        Memory,
        MemoryExtractor,
        MemoryStore,
        MindSettings,
        Retriever,
        LexiconStore,
        SessionMood,
        Snapshot,
        Turn,
        build_emotion_injection,
        cfg_get,
        classify,
        emotion_payload,
        format_existing_for_prompt,
        heuristic_self_emotion,
        is_group_session,
        level_of_intensity,
        memory_payload,
        mood_word_pad,
        parse_extraction,
        render_memory_block,
        resolve_mood_word,
        resolve_prototype,
        session_entries,
        session_intensity,
        should_sample,
    )
    from mind.debugmode import DEBUG_CODE, DEBUG_UID, DebugSessions  # type: ignore[no-redef]
    from mind.textfix import strip_message_markup, tidy_cjk_spaces  # type: ignore[no-redef]
    from mind.emotion.display import detail_panel, help_panel, history_panel, mood_panel  # type: ignore[no-redef]
    from mind.memory.display import clear_panel, forget_panel, help_panel as memory_help_panel, main_panel as memory_main_panel, remember_panel, search_panel  # type: ignore[no-redef]

def _parse_intensity(value: Any) -> float | None:
    """把面板滑块 / 指令传来的强度洗成 0~1；认不出就返回 None。

    面板滑块发的是 0~1，也有人习惯直接打 62 这样的百分数 —— 两种都收。
    """
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number > 1.0:
        number = number / 100.0
    return max(0.0, min(1.0, number))


try:
    import astrbot.api.message_components as Comp
except ImportError:  # 极老版本没有消息组件模块
    Comp = None  # type: ignore[assignment]




try:  # 身份判定（拒绝名单要用的「平台:uid」写法解析）
    from .mind import identity as identity_engine
except ImportError:  # pragma: no cover
    from mind import identity as identity_engine  # type: ignore[no-redef]

try:  # 记忆图谱（把记忆库铺成节点与边）
    from .mind import graph as graph_engine
except ImportError:  # pragma: no cover
    from mind import graph as graph_engine  # type: ignore[no-redef]

try:
    from astrbot.api.star import register
except ImportError:  # 极老版本没有 register，退化成空装饰器
    def register(*_args: Any, **_kwargs: Any):
        def decorator(cls):
            return cls

        return decorator

try:
    from astrbot.api.web import error_response, json_response, request as web_request
except ImportError:
    # 老版本没有这套助手：直接返回 dict 即可，FastAPI 会自己序列化成 JSON。
    # 这样"面板后端"就不会因为一个可选导入而整个失效。
    web_request = None  # type: ignore[assignment]

    def json_response(data: Any = None, **_kwargs: Any) -> Any:  # type: ignore[misc]
        return {} if data is None else data

    def error_response(message: str, **_kwargs: Any) -> Any:  # type: ignore[misc]
        return {"status": "error", "message": message, "data": None}

#: 主动发送的三种结果。分这么细是因为「抛异常」和「确定没发出去」必须区别对待：
#: 前者可能已经发出去了，再补一遍就是刷屏。
def _optional_hook(name: str):
    """拿一个可能不存在的钩子装饰器。

    老版本 AstrBot 没有 on_using_llm_tool，直接 getattr 会在导入期就炸掉整个插件。
    拿不到就返回一个空装饰器：功能降级，但插件照常加载。
    """
    decorator = getattr(filter, name, None)
    if callable(decorator):
        return decorator

    def _noop(*_args: Any, **_kwargs: Any):
        def _wrap(func: Any) -> Any:
            return func

        return _wrap

    return _noop


on_using_llm_tool = _optional_hook("on_using_llm_tool")
on_waiting_llm_request = _optional_hook("on_waiting_llm_request")
llm_tool = _optional_hook("llm_tool")

SEND_OK = "ok"
SEND_FAILED = "failed"
SEND_UNKNOWN = "unknown"

PLUGIN_NAME = "astrbot_plugin_ai_mind"
PLUGIN_AUTHOR = "挽风随行+DSH(主代码编写)、冰与火之歌"
#: 插件版本（注册、备份文件、README/CHANGELOG 用的是同一个号；两段式：主.次）
PLUGIN_VERSION = "2.1"
#: 面板接口的备用命名空间。
#: 刻意用 ASCII 而不是署名 —— 宿主会把它拼进 URL，中文和括号要转义，
#: 很容易被解析成别的路径（面板接口之前就是这么坏过一次的）。
PLUGIN_ID = "dsh/" + PLUGIN_NAME

#: 以这些字符开头的消息当成指令，不参与记忆/情绪处理
COMMAND_PREFIXES = ("/", "／", "#", "!", "！", ".", "。")


def _coerce_int_list(raw: Any) -> list[int]:
    """把面板/桥传过来的 id 列表洗成干净的整数列表。

    可能是数组，也可能是逗号分隔的字符串（桥有时会这样传），一律认。
    """
    if isinstance(raw, str):
        raw = [part for part in raw.replace(" ", ",").replace("，", ",").split(",") if part.strip()]
    if not isinstance(raw, (list, tuple, set)):
        return []
    ids: list[int] = []
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value and value not in ids:
            ids.append(value)
    return ids


def _split_keywords(raw: Any) -> list[str]:
    """把面板传来的关键词拆成列表。

    用户可能用逗号、顿号、分号、换行或空格分隔，也可能直接传数组 —— 全都认。
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        parts = [str(item) for item in raw]
    else:
        text = str(raw)
        for separator in ("，", "、", ";", "；", ",", chr(10), chr(13), chr(9)):
            text = text.replace(separator, " ")
        parts = text.split(" ")
    unique: list[str] = []
    for part in parts:
        value = part.strip()
        if value and value not in unique:
            unique.append(value)
    return unique

_PAD_TAG_RE = None


def _pad_tag_regex():
    global _PAD_TAG_RE
    if _PAD_TAG_RE is None:
        _PAD_TAG_RE = re.compile(
            r"p\s*=\s*(-?\d*\.?\d+)\s*[,，\s]\s*a\s*=\s*(-?\d*\.?\d+)\s*[,，\s]\s*d\s*=\s*(-?\d*\.?\d+)",
            re.IGNORECASE,
        )
    return _PAD_TAG_RE


DEBUG_PROMPT = (
    "\n\n<debug_session>\n"
    "现在是单会话调试模式：忽略上面所有角色设定、称呼、口头禅与说话风格，"
    "不要扮演任何人。\n"
    "直接、简短、准确地回答；问到插件内部状态时照实说，不用顾虑人设。\n"
    "</debug_session>"
)

DEBUG_ON_TEXT = "🔧 调试模式已开启：本会话她不再扮演，直接回答问题。再发一次口令关闭。"
DEBUG_OFF_TEXT = "🔧 调试模式已关闭：恢复人设。"

DEBUG_HELP = "\n".join([
    "🔧 调试模式命令（只在开了调试模式的会话、只有你能用）",
    "#状态　插件 / 情绪 / 关系 / 曲线 / 待抽取，一屏看完",
    "#曲线　采样点数量、最近一点、数据库能不能写",
    "#记忆　最近 5 条记忆 + 待抽取轮数",
    "#抽取　立刻把攒着的对话送去抽记忆",
    "#工具　工具闸门的开关与名单",
    "#帮助　这份清单",
    "",
    "调试模式里插件完全让路：不注入情绪 / 记忆 / 身份，不分段，不记记忆。",
    "再发一次 " + DEBUG_CODE + " 退出。",
])

class _SampleRow:
    """一条采样点。面板那边是按**属性**读的（.t / .pad()），所以不能给 dict。"""

    __slots__ = ("id", "session_id", "t", "p", "a", "d", "kind")

    def __init__(self, item: Any) -> None:
        self.id = int(item["id"])
        self.session_id = str(item["session_id"])
        self.t = float(item["t"])
        self.p = float(item["p"])
        self.a = float(item["a"])
        self.d = float(item["d"])
        self.kind = str(item["kind"] or "auto")

    def pad(self) -> Any:
        try:
            from .mind.emotion.model import PAD
        except ImportError:  # pragma: no cover
            from mind.emotion.model import PAD  # type: ignore[no-redef]
        return PAD(p=self.p, a=self.a, d=self.d)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "t": self.t, "p": self.p, "a": self.a,
            "d": self.d, "kind": self.kind,
        }


class _SampleStore:
    """情绪曲线采样点仓库（自足版）。

    为什么不放在 mind/samples.py：那个包曾经被另一个插件的同名 mind 包顶替过
    （sys.modules 里先注册的那个赢），于是 mind/samples.py 换了多少次都不生效 ——
    采样一路写进「已关闭的数据库」，日志里只有一句 warning。main.py 不会认错，
    它就在插件目录里。所以曲线这块改成自足：自己的连接、自己的表、坏了就丢开重连。
    """

    _DDL = (
        "CREATE TABLE IF NOT EXISTS emotion_samples ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " session_id TEXT NOT NULL, t REAL NOT NULL,"
        " p REAL NOT NULL, a REAL NOT NULL, d REAL NOT NULL,"
        " kind TEXT NOT NULL DEFAULT {q}auto{q});"
        "CREATE INDEX IF NOT EXISTS idx_sample_session"
        " ON emotion_samples (session_id, t);"
    ).format(q=chr(39))

    # 兼容旧的测试钩子：以前这些字段挂在 ConnectionSource 上。
    # _db_path 非空时以它为准 —— 测试靠它模拟「这个路径写不进去」。
    _db_path: Any = None
    _source: Any = None
    _spare_conn: Any = None
    _schema_ready: bool = False

    def __init__(self, path: Any, logger: Any = None) -> None:
        self.path = Path(path)
        self.logger = logger
        self.last_error = ""
        self._conn: Any = None

    def _file(self) -> str:
        return str(self._db_path or self.path)

    # -- 连接 -----------------------------------------------------------
    def _open(self) -> Any:
        try:
            conn = sqlite3.connect(self._file(), check_same_thread=False, timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.executescript(self._DDL)
            conn.commit()
        except (sqlite3.Error, OSError) as exc:
            self.last_error = f"打开曲线库失败：{type(exc).__name__}: {exc}"
            if self.logger is not None:
                self.logger.warning(f"[ai_mind] {self.last_error}")
            return None
        self._conn = conn
        self.last_error = ""
        return conn

    @property
    def conn(self) -> Any:
        conn = self._conn
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except sqlite3.Error:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
        return self._open()

    @conn.setter
    def conn(self, value: Any) -> None:
        self._conn = value

    def _sql(self, sql: str, params: Iterable[Any] = ()) -> Any:
        for attempt in (0, 1):
            conn = self.conn
            if conn is None:
                return None
            try:
                cursor = conn.execute(sql, tuple(params))
                conn.commit()
                return cursor
            except sqlite3.Error as exc:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
                if attempt:
                    self.last_error = f"曲线 SQL 失败：{type(exc).__name__}: {exc}"
                    if self.logger is not None:
                        self.logger.warning(f"[ai_mind] {self.last_error}")
                    return None
        return None

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[Any]:
        cursor = self._sql(sql, params)
        if cursor is None:
            return []
        try:
            return list(cursor.fetchall())
        except sqlite3.Error:
            return []

    @staticmethod
    def _row(item: Any) -> _SampleRow:
        return _SampleRow(item)

    # -- 写 -------------------------------------------------------------
    def append(self, session_id: str, t: float, pad: Any, kind: str = "auto") -> None:
        self._sql(
            "INSERT INTO emotion_samples (session_id, t, p, a, d, kind)"
            " VALUES (?,?,?,?,?,?)",
            (str(session_id), float(t), float(pad.p), float(pad.a), float(pad.d), str(kind)),
        )

    def replace_at(self, session_id: str, t: float, pad: Any,
                   *, tolerance: float = 1.0) -> int:
        cursor = self._sql(
            "UPDATE emotion_samples SET p = ?, a = ?, d = ?, kind = ?"
            " WHERE session_id = ? AND t BETWEEN ? AND ?",
            (float(pad.p), float(pad.a), float(pad.d), "manual", str(session_id),
             float(t) - float(tolerance), float(t) + float(tolerance)),
        )
        return int(cursor.rowcount) if cursor is not None else 0

    def delete_at(self, session_id: str, t: float, *, tolerance: float = 1.0) -> int:
        cursor = self._sql(
            "DELETE FROM emotion_samples WHERE session_id = ? AND t BETWEEN ? AND ?",
            (str(session_id), float(t) - float(tolerance), float(t) + float(tolerance)),
        )
        return int(cursor.rowcount) if cursor is not None else 0

    def clear(self, session_id: str | None = None) -> int:
        if session_id is None:
            cursor = self._sql("DELETE FROM emotion_samples")
        else:
            cursor = self._sql(
                "DELETE FROM emotion_samples WHERE session_id = ?", (str(session_id),)
            )
        return int(cursor.rowcount) if cursor is not None else 0

    def prune_all(self, max_points: int) -> int:
        if int(max_points) <= 0:
            return 0
        removed = 0
        for key in self.sessions():
            cursor = self._sql(
                "DELETE FROM emotion_samples WHERE session_id = ? AND id NOT IN ("
                " SELECT id FROM emotion_samples WHERE session_id = ?"
                " ORDER BY t DESC LIMIT ?)",
                (key, key, int(max_points)),
            )
            if cursor is not None:
                removed += int(cursor.rowcount)
        return removed

    # -- 读 -------------------------------------------------------------
    def since(self, session_id: str, since: float, limit: int = 1500) -> list[Any]:
        rows = self._rows(
            "SELECT * FROM emotion_samples WHERE session_id = ? AND t >= ?"
            " ORDER BY t ASC LIMIT ?",
            (str(session_id), float(since), int(limit)),
        )
        return [self._row(item) for item in rows]

    def latest(self, session_id: str) -> Any:
        rows = self._rows(
            "SELECT * FROM emotion_samples WHERE session_id = ?"
            " ORDER BY t DESC LIMIT 1",
            (str(session_id),),
        )
        return self._row(rows[0]) if rows else None

    def count(self, session_id: str | None = None) -> int:
        if session_id is None:
            rows = self._rows("SELECT COUNT(*) AS c FROM emotion_samples")
        else:
            rows = self._rows(
                "SELECT COUNT(*) AS c FROM emotion_samples WHERE session_id = ?",
                (str(session_id),),
            )
        return int(rows[0]["c"]) if rows else 0

    def sessions(self) -> list[str]:
        rows = self._rows("SELECT DISTINCT session_id FROM emotion_samples")
        return [
            str(item["session_id"]) for item in rows
            if item["session_id"] and not str(item["session_id"]).startswith("__")
        ]

    def selfcheck(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "ok": False, "conn": self.conn is not None, "table": True,
            "write": False, "rows": 0, "error": "", "path": self._file(),
            "exists": False, "size": 0,
        }
        try:
            target = Path(self._file())
            info["exists"] = target.exists()
            info["size"] = target.stat().st_size if info["exists"] else 0
        except OSError:
            pass
        conn = self.conn
        if conn is None:
            info["error"] = self.last_error or "曲线库打不开"
            return info
        probe = "__selfcheck__"
        err = ""
        try:
            conn.execute("DELETE FROM emotion_samples WHERE session_id = ?", (probe,))
            conn.execute(
                "INSERT INTO emotion_samples (session_id, t, p, a, d, kind)"
                " VALUES (?,?,?,?,?,?)",
                (probe, time.time(), 0.0, 0.0, 0.0, "probe"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT COUNT(*) FROM emotion_samples WHERE session_id = ?", (probe,)
            ).fetchone()
            info["write"] = bool(row and int(row[0]) > 0)
            conn.execute("DELETE FROM emotion_samples WHERE session_id = ?", (probe,))
            conn.commit()
        except (sqlite3.Error, OSError) as exc:
            err = f"{type(exc).__name__}: {exc}"
        info["ok"] = bool(info["write"])
        info["error"] = "" if info["write"] else (err or self.last_error or "插入没有生效")
        info["rows"] = self.count()
        return info

@register(
    PLUGIN_NAME,
    PLUGIN_AUTHOR,
    "AI 心智 · 情绪与记忆（精简版）：情绪 + 长期记忆 + 记忆图谱",
    PLUGIN_VERSION,
)

class AIMindPlugin(Star):
    """情绪与记忆合体后的主类。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        self.data_dir = self._resolve_data_dir()
        self._privacy_data = self._load_simple_overrides("privacy_overrides.json", "隐私")
        self._schema_cache: dict[str, Any] | None = None
        self._settings_saved_at = 0.0
        self.settings = MindSettings.from_config(
            self.config, privacy=self._privacy_data,
        )

        self.emotion_injection_mode = str(
            cfg_get(self.config, "emotion.injection.mode", "content_part") or "content_part"
        ).strip().lower()
        self.emotion_inject_rules = bool(cfg_get(self.config, "emotion.injection.inject_rules", True))
        self.emotion_trusted = bool(cfg_get(self.config, "emotion.injection.trusted", True))
        self.memory_injection_mode = str(
            cfg_get(self.config, "memory.injection.mode", "content_part") or "content_part"
        ).strip().lower()
        self.memory_inject_rules = bool(cfg_get(self.config, "memory.injection.inject_rules", True))
        self.embedding_timeout = float(cfg_get(self.config, "memory.embedding.timeout_seconds", 3) or 3)


        # ---- 情绪 ----
        self.engine = EmotionEngine(self.settings.emotion, self.data_dir, logger)
        self.engine.set_custom_rules(cfg_get(self.config, "emotion.persona.custom_rules", []))

        # ---- 记忆 ----
        # 单会话调试模式：聊天里发口令开关，刻意不做进面板（见 mind/debugmode.py）
        self.debug = DebugSessions(self.data_dir / "debug_sessions.json", logger)
        db_file = self.data_dir / "mind.db"
        self.store = MemoryStore(db_file, logger)
        # 传的是仓库本身而不是 self.store.connection：宿主重载插件会先调
        # terminate()（我们把库关掉），老实例却可能还在收消息 —— 存下来的裸连接
        # 就成了「已关闭的数据库」，之后所有写入静默失败（曲线永远空、记忆写不进）。
        # 传仓库进去，每次都现取连接，库被重开也能跟上。
        # 曲线存储自足：自己的连接、自己的表 —— 不再和别人共用连接对象
        # （以前那个 mind/samples.py 会被别的插件的同名包顶替，改了也不生效）
        self.samples = _SampleStore(db_file, logger)
        self.lexicons = LexiconStore(self.store, logger)
        # 把库文件的路径也交给各仓库：主连接要是被别处关掉，
        # 它们能自己连上这个文件继续干活，而不是一路静默失败。
        for _store in (self.samples, self.lexicons):
            _store._db_path = db_file
        self.extractor = MemoryExtractor(self.settings.memory, logger)
        self.retriever = Retriever(self.settings.memory, logger)

        # ---- 运行时状态 ----
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._llm_messages: OrderedDict[str, float] = OrderedDict()
        self._last_sender: dict[str, str] = {}
        self._buffers: dict[str, list[Turn]] = {}
        self._last_append: dict[str, float] = {}
        self._extracting: set[str] = set()
        self._extract_tasks: set[asyncio.Task[Any]] = set()
        self._last_sample: dict[str, Any] = {}
        self._prune_tick = 0
        self._sample_broken = False
        self._sample_warned = False
        # 曲线为什么不画线 —— 面板上要能一眼看出卡在哪一步。
        # 计数只在本次进程内有效，重启就归零，所以文案写「本次启动以来」。
        self._diag = {
            "turns": 0, "prepares": 0, "gated": 0, "frozen": 0,
            "samples": 0, "skipped": 0, "writes": 0,
        }
        self._last_flush = 0.0
        self._task: asyncio.Task[None] | None = None
        self._embedding_key = ""
        # 被面板冻结的会话：情绪完全停住（衰减也停）。
        # 这里**故意用引擎那一份**的别名 —— 面板、指令、后台维护看到的是同一个集合，
        # 引擎那边才好据此跳过衰减与落盘冻结状态。
        self._frozen: set[str] = self.engine.frozen

        self._register_web_apis()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def _resolve_data_dir(self) -> Path:
        getter = getattr(StarTools, "get_data_dir", None)
        if callable(getter):
            try:
                return Path(getter(PLUGIN_NAME))
            except Exception:  # noqa: BLE001
                pass
        return Path(__file__).resolve().parent / "data"

    async def initialize(self) -> None:
        self.engine.load()
        stats = self.store.stats()
        logger.info(
            f"[ai_mind] 已就绪：{self.engine.session_count} 个会话的情绪、"
            f"{stats['total']} 条记忆、{self.samples.count()} 个曲线采样点"
        )
        # 代码指纹 + 代码目录：装的是不是新代码、加载的是哪个文件夹，
        # 一眼就能看出来（混装过，查了好几轮才发现）。
        try:
            from .mind import dbconn as _dbconn
            from .mind import samples as _samples
        except ImportError:  # pragma: no cover
            import mind.dbconn as _dbconn  # type: ignore[no-redef]
            import mind.samples as _samples  # type: ignore[no-redef]
        logger.info(
            "[ai_mind] 代码指纹："
            f"{getattr(_dbconn, 'STAMP', '?')} / {getattr(_samples, 'STAMP', '?')}"
            f"｜目录 {os.path.dirname(os.path.abspath(__file__))}"
        )
        try:
            from .mind import panel as _panel
        except ImportError:  # pragma: no cover
            import mind.panel as _panel  # type: ignore[no-redef]
        logger.info(
            "[ai_mind] 实际加载的模块："
            f"samples={getattr(_samples, '__file__', '?')}"
            f"｜panel={getattr(_panel, '__file__', '?')}"
            + (f"｜抢回被顶替的包 {_RECLAIMED}" if _RECLAIMED else "")
        )
        try:
            from .mind.emotion import lexicon as _lexicon
        except ImportError:  # pragma: no cover
            from mind.emotion import lexicon as _lexicon
        logger.info(f"[ai_mind] {_lexicon.describe_lexicon()}")
        self._check_prompt_defaults()
        self._cleanup_legacy_files()
        type(self)._LIVE = self      # 认领：以后旧实例会把请求转给我
        self._load_pending()
        self._ensure_task()

    def _check_prompt_defaults(self) -> None:
        """三块提示词全用代码默认值 —— 启动时验一遍它们**存在且能注入**。

        没有覆盖层了（prompts.json 已删），所以这里就是唯一的一道保险：
        块非空、标记在块里（标记不在，`on_llm_request` 的去重判断会失效，
        要么重复追加、要么压根不追加）。有问题直接 error 日志喊出来。
        """
        blocks = (
            ("emotion.rules", EMOTION_RULES_BLOCK, EMOTION_RULES_MARKER),
            ("emotion.crisis", CRISIS_BLOCK, '<safety_override'),
            ("memory.rules", MEMORY_RULES_BLOCK, MEMORY_RULES_MARKER),
        )
        broken = [
            name for name, text, marker in blocks
            if not (text or "").strip() or marker not in text
        ]
        if broken:
            logger.error(
                f"[ai_mind] ⚠️ 提示词默认值缺失或标记对不上，注入会失效：{broken}"
            )
            return
        logger.info(
            "[ai_mind] 三块提示词走代码默认值（无覆盖层）："
            + "、".join(f"{name} {len(text)} 字" for name, text, _ in blocks)
        )

    def _cleanup_legacy_files(self) -> None:
        """删掉「已删功能」留下的覆盖配置、多余数据表与人格预设文件。

        分段、护栏、表达示例、消息防抖、去 AI 味、关系这些功能整块去掉了，
        对应的 `*_overrides.json` 插件已经**不再读取**；留着只会让人以为
        改它还有用。启动时自动清掉并打一条日志，幂等。

        一并清掉的还有：老版本建出来的多余数据表（memories_trash 等）、
        已经没人读的自定义人格目录 `presets/`，以及废弃的提示词覆盖 `prompts.json`
        （三块提示词现在一律用代码默认值）。
        """
        legacy = (
            "splitter_overrides.json",
            "guard_overrides.json",
            "style_overrides.json",
            "debounce_overrides.json",
            "humanize_overrides.json",
            "relationship_overrides.json",
        )
        for name in legacy:
            path = self.data_dir / name
            try:
                if path.is_file():
                    path.unlink()
                    logger.info(f"[ai_mind] 已清掉不再使用的覆盖配置：{name}")
            except OSError as exc:
                logger.warning(f"[ai_mind] 清不掉 {name}：{exc}")

        # prompts.json 已彻底废弃：三块提示词（emotion.rules / emotion.crisis /
        # memory.rules）全部直接用代码里的默认值，没有任何覆盖层。
        # 磁盘上留着它只会让人以为改它还有用，启动时直接删掉。
        try:
            stale_file = self.data_dir / "prompts.json"
            if stale_file.is_file():
                stale_file.unlink()
                logger.info("[ai_mind] 已删除不再生效的 prompts.json（提示词一律用代码默认值）")
        except OSError as exc:
            logger.warning(f"[ai_mind] 删除 prompts.json 失败：{exc}")

        # 已删功能留下的多余数据表：直接 DROP。
        # 只认 store.LEGACY_TABLES 这份白名单，记忆（memories）和情绪曲线
        # （emotion_samples）永远不在名单里，删不掉。
        try:
            dropped = self.store.drop_legacy_tables()
            if dropped:
                logger.info(f"[ai_mind] 已删除多余数据表：{'、'.join(dropped)}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 清理多余数据表失败：{exc}")

        # 自定义人格预设目录 presets/：「人格定制」早就整块删掉了，
        # 里面的文件没有任何代码再读，留着只会让人以为还能改。
        try:
            preset_dir = self.data_dir / "presets"
            if preset_dir.is_dir():
                shutil.rmtree(preset_dir, ignore_errors=True)
                if not preset_dir.exists():
                    logger.info("[ai_mind] 已删除不再使用的人格预设目录 presets/")
        except OSError as exc:
            logger.warning(f"[ai_mind] 清理 presets/ 失败：{exc}")

    async def terminate(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        try:
            await self.flush_memories(force=True)
            pending = set(self._extract_tasks)
            if pending:
                await asyncio.wait(pending, timeout=20)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 退出前整理记忆失败：{exc}")
        try:
            self.engine.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 退出前保存情绪失败：{exc}")
        if type(self)._LIVE is self:
            type(self)._LIVE = None
        self.store.close()

    def _ensure_task(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._maintenance_loop())

    async def _maintenance_loop(self) -> None:
        interval = self.settings.maintenance_interval
        while True:
            try:
                await asyncio.sleep(interval)
                now = time.time()
                self.engine.tick_all(now)
                self._heartbeat(now)
                if self.engine.dirty:
                    await asyncio.to_thread(self.engine.flush, now)
                await self.flush_memories()
                await self.embed_pending()
                # 裁剪不必每轮都做：它要扫全库、还要按重要度排序，
                # 是这一圈里最贵的一步。每 10 圈跑一次足够 ——
                # 记忆多到该裁的程度，不会在这几分钟里突然爆掉。
                self._prune_tick += 1
                if self._prune_tick >= 10:
                    self._prune_tick = 0
                    self.store.prune(
                        max_memories=self.settings.memory.max_memories,
                        importance_floor=self.settings.memory.prune_importance_floor,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[ai_mind] 后台维护出错：{exc}", exc_info=True)

    # ------------------------------------------------------------------
    # 情绪曲线采样
    # ------------------------------------------------------------------
    def _maybe_sample(self, session_key: str, pad: PAD, kind: str, now: float, force: bool = False) -> None:
        last = self._last_sample.get(session_key)
        if last is None:
            last = self.samples.latest(session_key)
            self._last_sample[session_key] = last
        previous = None
        if last is not None and self.settings.emotion.emotion_half_life > 0:
            if session_key in self._frozen:
                # 冻结中情绪不动：拿上次的原始值比，不该再按衰减去算"应该变多少"
                previous = last.pad()
            else:
                previous = last.pad().decay_toward(
                    self.settings.emotion.baseline,
                    self.settings.emotion.emotion_half_life,
                    max(0.0, now - last.t),
                )
        self._diag["samples"] += 1
        if not should_sample(previous, pad, self.settings.panel.sample_epsilon, force=force):
            self._diag["skipped"] += 1
            return
        try:
            self.samples.append(session_key, now, pad, kind)
        except Exception as exc:  # noqa: BLE001
            # 以前这里不兜，异常会一路冒到 on_llm_request 被记成「注入失败」，
            # 真正的原因（曲线写不进去）反而看不见了。
            self._sample_broken = True
            if not self._sample_warned:
                self._sample_warned = True
                logger.warning(f"[ai_mind] 曲线采样失败，面板上的情绪曲线会退化：{exc}")
            return
        self._last_sample[session_key] = self.samples.latest(session_key)
        self._diag["writes"] += 1
        why = ""
        if self.samples.conn is None:
            why = "曲线表不可用"
        elif getattr(self.samples, "last_error", ""):
            why = str(self.samples.last_error)
        if why:
            # 别静默失败 —— 否则面板上就是一片空白，还查不出原因
            self._sample_broken = True
            if not self._sample_warned:
                self._sample_warned = True
                logger.warning(f"[ai_mind] 曲线采样写不进去，情绪曲线会退化（{why}）")

    def _heartbeat(self, now: float) -> None:
        """没有事件时也定期打点，曲线上才看得见"自己衰减回去"的过程。"""
        for key, session in list(self.engine.sessions.items()):
            last = self._last_sample.get(key)
            if last is None:
                last = self.samples.latest(key)
                self._last_sample[key] = last
            if last is None:
                self._maybe_sample(key, session.emotion, "auto", now, force=True)
                continue
            if now - last.t < self.settings.panel.heartbeat_seconds:
                continue
            self._maybe_sample(key, session.emotion, "auto", now)
        self.samples.prune_all(self.settings.panel.curve_max_points)

    # ------------------------------------------------------------------
    # 对话采集（记忆）
    # ------------------------------------------------------------------
    @staticmethod
    def _event_key(event: AstrMessageEvent) -> str:
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        return f"{event.unified_msg_origin}|{message_id}" if message_id else ""

    def _mark_llm(self, event: AstrMessageEvent) -> None:
        key = self._event_key(event)
        if not key:
            return
        self._llm_messages[key] = time.time()
        while len(self._llm_messages) > 500:
            self._llm_messages.popitem(last=False)

    def _record_turn(self, event: AstrMessageEvent, reply: str) -> None:
        if self._sender_denied(event):
            return
        # 剥掉协议占位标记（<quoted_message …/> 之类）：它们进了抽取提示词，
        # 就会被模型当成「他说的话」，最后落成一条乱码记忆
        user_text = strip_message_markup(event.message_str or "").strip()
        memory = self.settings.memory
        if len(user_text) < memory.min_turn_chars and len(reply) < memory.min_turn_chars:
            return
        session_id = event.unified_msg_origin
        buffer = self._buffers.setdefault(session_id, [])
        buffer.append(Turn(user=user_text, assistant=reply.strip(), at=time.time()))
        overflow = len(buffer) - memory.max_turns_kept
        if overflow > 0:
            del buffer[:overflow]
        self._last_append[session_id] = time.time()
        self._save_pending()

    def _save_pending(self) -> None:
        """把还没抽取的对话写到磁盘。

        这些对话原来只存在内存里：攒够 6 轮、或者静默 3 分钟才会被抽取，
        中途重启一次就全没了 —— 表现就是「手动写入可以，自动写入从来不见效」，
        而且越常重启越明显（更新插件就是在重启）。
        """
        try:
            path = self.data_dir / "pending_turns.json"
            data = {
                key: [
                    {"user": t.user, "assistant": t.assistant, "at": t.at}
                    for t in turns
                ]
                for key, turns in self._buffers.items() if turns
            }
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - 存不下不能影响聊天
            if self.settings.debug_log:
                logger.debug(f"[ai_mind] 暂存待抽取对话失败：{exc}")

    def _load_pending(self) -> None:
        """把上次没来得及抽取的对话捡回来。"""
        try:
            path = self.data_dir / "pending_turns.json"
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 读取待抽取对话失败：{exc}")
            return
        restored = 0
        for key, items in (data or {}).items():
            if not isinstance(items, list):
                continue
            turns = [
                Turn(
                    user=str(item.get("user") or ""),
                    assistant=str(item.get("assistant") or ""),
                    at=float(item.get("at") or 0.0),
                )
                for item in items if isinstance(item, dict)
            ]
            if turns:
                self._buffers[str(key)] = turns
                restored += len(turns)
        if restored:
            logger.info(f"[ai_mind] 捡回上次没抽取的 {restored} 轮对话，稍后一起抽记忆")

    def _ready_sessions(self, now: float, force: bool = False) -> list[str]:
        memory = self.settings.memory
        ready: list[str] = []
        for session_id, buffer in list(self._buffers.items()):
            if not buffer or session_id in self._extracting:
                continue
            idle = now - self._last_append.get(session_id, now)
            if force or len(buffer) >= memory.batch_turns or idle >= memory.idle_flush_seconds:
                ready.append(session_id)
        return ready

    async def flush_memories(self, force: bool = False) -> None:
        if not self.settings.memory.enabled:
            return
        now = time.time()
        for session_id in self._ready_sessions(now, force=force):
            turns = self._buffers.get(session_id) or []
            if not turns:
                continue
            sender_id = str(self._last_sender.get(session_id, "") or "")
            self._buffers[session_id] = []
            self._save_pending()
            task = asyncio.create_task(self._extract_session(session_id, sender_id, list(turns)))
            self._extract_tasks.add(task)
            task.add_done_callback(self._extract_tasks.discard)

    async def _extract_session(self, session_id: str, sender_id: str, turns: list[Turn]) -> None:
        if session_id in self._extracting:
            return
        self._extracting.add(session_id)
        try:
            provider = await self._resolve_provider(session_id)
            if provider is None:
                # 静默返回过一次就够难受的了：用户看到的是「自动写入从来不生效」，
                # 却没有任何线索指向「拿不到对话模型」。
                logger.warning(
                    "[ai_mind] 记忆抽取：拿不到对话模型，这一批先跳过"
                    "（去 AstrBot 里确认默认对话模型设置好了）"
                )
                return
            # 去重参考只给**当前这个会话**能看到的记忆（本会话的 + 通用的）。
            # 老版本是 `list_memories(limit=40)`（全库最新 40 条）——
            # 别的群的记忆也会被当成"已有记忆"塞给模型，白烧 token 还帮倒忙。
            existing = [
                m
                for m in self.store.list_memories(session_id=session_id, limit=40)
                if not m.sensitive
            ]
            prompt = self.extractor.build_prompt(turns, format_existing_for_prompt(existing, 40))
            raw = await self.extractor.request(provider, prompt)
            if not raw:
                return
            items = parse_extraction(raw, max_items=self.settings.memory.max_items_per_batch)
            if not items:
                return
            added, merged, skipped = self.extractor.integrate(
                self.store, items, session_id=session_id, sender_id=sender_id
            )
            if added or merged:
                logger.info(f"[ai_mind] 记忆抽取：新增 {added} / 合并 {merged} / 跳过 {skipped}")
            elif self.settings.debug_log:
                logger.debug(f"[ai_mind] 记忆抽取：这一批没什么可记的（跳过 {skipped}）")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ai_mind] 记忆抽取出错：{exc}", exc_info=True)
        finally:
            self._extracting.discard(session_id)

    async def _resolve_provider(self, session_id: str) -> Any:
        context = getattr(self, "context", None)
        if context is None:
            return None
        provider_id = self.settings.memory.provider_id
        if provider_id:
            try:
                provider = context.get_provider_by_id(provider_id=provider_id)
                if provider is not None:
                    return provider
            except Exception:  # noqa: BLE001
                pass
        try:
            return await context.get_using_provider_async(umo=session_id)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # 向量检索（可选）
    # ------------------------------------------------------------------
    def _embedding_provider(self) -> Any:
        if not self.settings.memory.use_embedding:
            return None
        context = getattr(self, "context", None)
        if context is None:
            return None
        try:
            if self.settings.memory.embedding_provider_id:
                return context.get_provider_by_id(provider_id=self.settings.memory.embedding_provider_id)
            providers = context.get_all_embedding_providers()
            return providers[0] if providers else None
        except Exception:  # noqa: BLE001
            return None

    def _embedding_model_key(self, provider: Any) -> str:
        if self._embedding_key:
            return self._embedding_key
        try:
            dim = int(provider.get_dim())
        except Exception:  # noqa: BLE001
            dim = 0
        self._embedding_key = f"{self.settings.memory.embedding_provider_id or 'auto'}|{dim}"
        return self._embedding_key

    async def _embed_query(self, text: str) -> list[float] | None:
        provider = self._embedding_provider()
        if provider is None or not text:
            return None
        try:
            return await asyncio.wait_for(provider.get_embedding(text), timeout=self.embedding_timeout)
        except Exception:  # noqa: BLE001
            return None

    async def embed_pending(self) -> None:
        provider = self._embedding_provider()
        if provider is None:
            return
        key = self._embedding_model_key(provider)
        pending = self.store.unembedded(model=key, limit=32)
        if not pending:
            return
        texts = [m.searchable_text() for m in pending]
        try:
            if hasattr(provider, "get_embeddings"):
                vectors = await asyncio.wait_for(
                    provider.get_embeddings(texts), timeout=max(10.0, self.embedding_timeout * 4)
                )
            else:
                vectors = [
                    await asyncio.wait_for(provider.get_embedding(t), timeout=self.embedding_timeout)
                    for t in texts
                ]
        except Exception:  # noqa: BLE001
            return
        for memory, vector in zip(pending, vectors or []):
            if vector:
                self.store.set_embedding(memory.id, list(vector), key)

    # ------------------------------------------------------------------
    # 钩子
    # ------------------------------------------------------------------
    def _prepare_emotion(self, event: AstrMessageEvent) -> tuple[SessionMood, Any, Snapshot]:
        now = time.time()
        self.engine.load()
        session_key = event.unified_msg_origin
        uid = str(event.get_sender_id() or "")
        name = event.get_sender_name() or ""

        session = self.engine.resolve(session_key, now)
        relation = self.engine.relation_of(session, uid, name, now)

        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        dedup_key = f"{session_key}|{message_id}" if message_id else f"{session_key}|{round(now, 2)}"

        frozen = session_key in self._frozen
        fresh = self._mark_seen(dedup_key)
        self._diag["prepares"] += 1
        kind = "stimulus"
        if frozen:
            self._diag["frozen"] += 1
        else:
            if not fresh:
                self._diag["gated"] += 1
            else:
                gap = max(0.0, now - session.last_interaction)
                self.engine.apply_idle(session, now)
                self.engine.touch(session, relation, now)
                session.last_gap = gap
                # 评估用的也得是**人话**：占位标记既可能误触规则，也会被存进「原话」
                stimulus = self.engine.appraiser.appraise(
                    strip_message_markup(event.message_str or "")
                )
                if stimulus is not None:
                    if (stimulus.source == "crisis"
                            and not self.settings.emotion.crisis_enabled):
                        stimulus = None
                    else:
                        self.engine.apply_stimulus(session, stimulus, relation, now)
                        hits = getattr(stimulus, "lexicon_hits", ()) or ()
                        if hits:
                            self._record_lexicon_hits(
                                session_key, uid, hits, getattr(stimulus, "emotion", "")
                            )
                        if stimulus.source == "crisis":
                            kind = "crisis"
                        self._maybe_sample(
                            session_key, session.emotion, kind, now, force=True,
                        )
            # 曲线是「观测」，不该因为这条消息被判成重复就整条断掉。
            # 以前采样和去重绑在同一个 if 里：去重一旦误判，曲线就永远空着。
            self._maybe_sample(session_key, session.emotion, kind, now)
            self._schedule_flush()

        return session, relation, self.engine.snapshot(session, relation, now)

    def _record_lexicon_hits(self, session_key: str, uid: str, hits: Any, proto: str) -> None:
        """把这一轮词表命中的词记下来：面板上要看「她是因为哪句话有反应的」。"""
        try:
            self.lexicons.record(hits, session_id=session_key, uid=uid, proto=proto)
        except Exception as exc:  # noqa: BLE001 - 记不上不能影响聊天
            logger.warning(f"[ai_mind] 记录词表命中失败：{exc}")

    def _mark_seen(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen[key] = time.time()
        while len(self._seen) > 500:
            self._seen.popitem(last=False)
        return True

    def _schedule_flush(self) -> None:
        now = time.time()
        if now - self._last_flush < max(10.0, float(cfg_get(self.config, "advanced.save_interval", 60) or 60)):
            return
        self._last_flush = now
        try:
            self.engine.flush(now)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 保存情绪失败：{exc}")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """请求 LLM 前：结算情绪 + 检索记忆，然后把两样注入这一轮。"""
        if not self.settings.enabled:
            return
        if self.debug.is_active(event.unified_msg_origin):
            # 调试模式：插件完全让路 —— 不注入情绪 / 身份 / 记忆 / 语气，
            # 只在系统提示词末尾盖一条覆盖指令，把人格压下去。
            req.system_prompt = ((req.system_prompt or "") + DEBUG_PROMPT).strip()
            return
        try:
            if event.is_stopped():
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            self.engine.load()
            self._ensure_task()
            session_key = event.unified_msg_origin
            sender_id = str(event.get_sender_id() or "")
            # 拒绝名单：既不注入，也不采集（放在 _mark_llm 之前，免得被当成一轮对话记下来）
            if self._sender_denied(event):
                if self.settings.debug_log:
                    logger.info(f"[ai_mind] {sender_id} 在拒绝名单里，这一轮什么都不做")
                return
            self._last_sender[session_key] = sender_id
            self._mark_llm(event)

            # 每轮都会变的内容攒在这里，最后统一贴到请求尾部（见 _cache_or_place）
            cache_tail: list[str] = []

            # ---------- 情绪 ----------
            self._diag["turns"] += 1
            if self.settings.emotion.enabled:
                _session, _relation, snapshot = self._prepare_emotion(event)
                if self.emotion_inject_rules:
                    current = req.system_prompt or ""
                    if EMOTION_RULES_MARKER not in current:
                        req.system_prompt = (
                            current + "\n\n" + self._emotion_rules_block()
                        ).strip()
                block = build_emotion_injection(
                    snapshot,
                    verbosity=str(cfg_get(self.config, "emotion.injection.verbosity", "normal") or "normal"),
                    bot_name=self.settings.emotion.bot_name,
                    with_trace=bool(cfg_get(self.config, "emotion.injection.with_trace", False)),
                    # 危机覆盖一律用代码里的默认值（prompts.json 覆盖层已删除）
                    crisis_template=CRISIS_BLOCK,
                )
                self._cache_or_place(
                    req, block, cache_tail,
                    trusted=self.emotion_trusted, mode=self.emotion_injection_mode,
                )

            # ---------- 记忆 ----------
            if self.settings.memory.enabled:
                query = event.message_str or ""
                query_vector = await self._embed_query(query) if self.settings.memory.use_embedding else None
                result = self.retriever.retrieve(
                    self.store,
                    query=query,
                    session_id=session_key,
                    sender_id=sender_id,
                    query_vector=query_vector,
                )
                # 注意：RetrievalResult 是三个桶（pinned/recent/relevant）+ memories()，
                # 没有 .items —— 之前这里按 .items 用，AttributeError 被 except 吞掉，
                # 结果拒绝名单根本没生效，那些人的记忆照样会被端上来。
                kept = None
                try:
                    kept = self._filter_denied_memories(result.memories())
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[ai_mind] 过滤拒绝名单失败，这一轮不注入记忆：{exc}")
                if kept is None:
                    # 过滤不了就一条都别注入：拒绝名单是隐私承诺，
                    # 宁可这一轮不回忆，也不能把不该出现的人的记忆端上去。
                    result.retain([])
                elif len(kept) != len(result.memories()):
                    result.retain(kept)
                memory_block = render_memory_block(
                    result, settings=self.settings.memory, session_id=session_key,
                    # 名字得有人递给模型：规则块要求「用他的名字称呼他」，
                    # 而条目里写的是第三人称的「他/她/对方」
                    address=str(event.get_sender_name() or ""),
                )
                if memory_block:
                    if self.memory_inject_rules:
                        current = req.system_prompt or ""
                        if MEMORY_RULES_MARKER not in current:
                            # 记忆规则块：一律用代码里的默认值（覆盖层已删除）
                            req.system_prompt = (
                            current
                            + chr(10) + chr(10)
                            + MEMORY_RULES_BLOCK
                        ).strip()
                    self._cache_or_place(
                        req, memory_block, cache_tail,
                        trusted=False, mode=self.memory_injection_mode,
                    )
                    self.store.touch(result.ids())

            # ---------- 把每轮都变的内容贴到请求最后 ----------
            # 放这里而不是开头，是为了让前缀（persona + 历史）保持稳定，
            # 前缀缓存才命得中。
            if cache_tail:
                joined = "\n\n".join(cache_tail)
                if not self._append_content_part(req, joined):
                    # 这个 AstrBot 版本不支持附加到用户消息：退回受信任位。
                    # 宁可少省点 token，也不能把情绪和记忆弄丢。
                    self._place_trusted(req, joined)
        except Exception as exc:  # noqa: BLE001 - 钩子里的异常绝不能打断对话
            logger.error(f"[ai_mind] 注入失败：{exc}", exc_info=True)

    def _emotion_rules_block(self) -> str:
        """情绪规则块（写进系统提示词的那一段稳定说明）。

        **一律用代码里的默认值**（`mind/emotion/prompt.py::RULES_BLOCK`）——
        提示词覆盖层（prompts.json）已经整个删掉，改配置、改文件都影响不到它。

        这里只讲情绪本身：情绪是内在状态、不要念数值、有惯性、安全优先；
        不带任何"该怎么说话"的内容 —— 说话方式一律由机器人自己的人设提示词决定。
        """
        return EMOTION_RULES_BLOCK

    def _cache_or_place(
        self,
        req: ProviderRequest,
        block: str,
        tail: list[str],
        *,
        trusted: bool = True,
        mode: str = "content_part",
    ) -> None:
        """放一块「每轮都会变」的内容。

        为什么要有这个：DeepSeek / OpenAI 的前缀缓存是按「从头开始的最长公共
        前缀」命中的。以前这些块塞在 System Prompt 后面（受信任位），每轮一变
        就把后面的整段历史全废掉，缓存一次都命中不了 —— 越聊越贵、越聊越慢。

        缓存友好模式（默认开）下攒进 tail，最后统一贴到当前用户消息后面；
        这样前缀 = persona + 历史记录，稳定不动，缓存能一直吃到。
        """
        if not block:
            return
        if self.settings.cache_friendly:
            tail.append(block)
            return
        if trusted:
            self._place_trusted(req, block)
        else:
            self._place(req, block, mode)

    @staticmethod
    def _place_trusted(req: ProviderRequest, block: str, tail: list[str] | None = None) -> bool:
        """把身份信息放进系统提示词。

        这里**曾经**往 req.contexts 里塞一条 role=system，那是个要命的误解：
        AstrBot 里 req.contexts 就是**对话历史本体**（每轮从 conv.history
        json.loads 出来的消息数组），不是「系统提示词之后的空位」。塞进去等于
        在历史末尾追加一条系统指令 —— 模型会把末尾的 system 当成**新一轮的设定**，
        于是前面刚聊过的内容像被洗掉一样，问过的事又问一遍。

        现在只写系统提示词：那是真正受信任的位置，而且身份块本身是稳定的，
        放在最前面照样吃满前缀缓存。
        """
        if not block:
            return False
        req.system_prompt = ((req.system_prompt or "") + chr(10) + chr(10) + block).strip()
        return True

    def _place(self, req: ProviderRequest, block: str, mode: str) -> None:
        if not block:
            return
        if mode not in {"content_part", "system_prompt", "both"}:
            mode = "content_part"
        placed = False
        if mode in {"content_part", "both"}:
            placed = self._append_content_part(req, block)
        if mode == "system_prompt" or (mode == "content_part" and not placed) or mode == "both":
            req.system_prompt = ((req.system_prompt or "") + "\n\n" + block).strip()

    @staticmethod
    def _append_content_part(req: ProviderRequest, text: str) -> bool:
        parts = getattr(req, "extra_user_content_parts", None)
        if parts is None:
            return False
        part: Any
        try:
            from astrbot.core.agent.message import TextPart

            part = TextPart(text=text)
            mark = getattr(part, "mark_as_temp", None)
            if callable(mark):
                part = mark() or part
        except Exception:  # noqa: BLE001
            part = {"type": "text", "text": text}
        try:
            parts.append(part)
            return True
        except Exception:  # noqa: BLE001
            return False

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """消息发出前：摘掉情绪标记 + 记录这一轮对话。"""
        if not self.settings.enabled:
            return
        if self.debug.is_active(event.unified_msg_origin):
            # 调试模式：不记记忆 —— 要的就是原文。
            return
        session_key = event.unified_msg_origin
        try:
            reply = await self._emotion_self_feedback(event, session_key)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ai_mind] 自表达回读失败：{exc}", exc_info=True)
            reply = ""
        try:
            key = self._event_key(event)
            is_llm = bool(key) and key in self._llm_messages
            if key:
                self._llm_messages.pop(key, None)
            # 排版清理：中文之间不留空格。放在记录之前 —— 记的是真正发出去的那版。
            try:
                self._tidy_reply_spaces(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_mind] 清理中文空格失败：{exc}")
            if is_llm and self.settings.memory.enabled:
                final_reply = self._reply_text(event) or reply
                if final_reply:
                    self._record_turn(event, final_reply)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ai_mind] 记录对话失败：{exc}", exc_info=True)

    #: 当前活着的实例。插件重载时旧实例不会立刻消失，它的路由还会抢走
    #: 面板请求 —— 而旧实例的库是关着的、计数器停在 0，于是面板显示成
    #: 「写不进去、收到 0 轮」，用户只能靠重启整个进程才能甩掉它。
    #: 有了这个指针，旧实例就把请求转交给新实例，不用再重启。
    _LIVE: Any = None

    def _live_peer(self) -> Any:
        live = type(self)._LIVE
        if live is not None and live is not self:
            return live
        return None

    def _disabled_notice(self) -> str:
        """我们是不是被 AstrBot 停用了？是的话返回一句人话。

        AstrBot 把停用的插件记在 data/shared_preferences.json 的
        inactivated_plugins 里。停用的插件会正常加载、面板也打得开，
        但所有钩子都不会被调用 —— 从插件内部看不出任何异常。
        """
        try:
            name = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
            root = Path(self.data_dir).parent.parent
            pref = root / "shared_preferences.json"
            if not pref.exists():
                return ""
            data = json.loads(pref.read_text(encoding="utf-8"))
            inactive = data.get("inactivated_plugins") or []
        except Exception:  # noqa: BLE001 - 读不到就当没停用
            return ""
        for item in inactive:
            if name and name in str(item):
                return (
                    "插件在 AstrBot 里是【停用】状态 —— 面板打得开，但她的钩子"
                    "一次都不会被调用。去 AstrBot 面板 › 插件管理 把 " + name
                    + " 打开（或删掉 data/shared_preferences.json 里"
                    " inactivated_plugins 的这一条），然后重启 AstrBot。"
                )
        return ""

    def _tidy_reply_spaces(self, event: AstrMessageEvent) -> int:
        """把她这句话里中文中间的空格去掉（纯排版清理，与人设无关）。

        模型会写出「切 干嘛」「好 吧」这种空格。这里只动中文与中文之间的空白，
        「AstrBot 很好用」里的那个空格保持原样。
        它不规定任何说话方式 —— 说什么、怎么说仍然完全由机器人自己的提示词决定。
        """
        try:
            result = event.get_result()
        except Exception:  # noqa: BLE001
            return 0
        chain = getattr(result, "chain", None) or []
        fixed = 0
        for comp in chain:
            text = getattr(comp, "text", None)
            if not isinstance(text, str) or not text:
                continue
            tidy = tidy_cjk_spaces(text)
            if tidy == text:
                continue
            try:
                comp.text = tidy
                fixed += 1
            except Exception:  # noqa: BLE001 - 改不动就算了，绝不能弄丢回复
                continue
        if fixed:
            logger.debug(f"[ai_mind] 清掉了 {fixed} 段里中文中间的空格")
        return fixed

    async def _emotion_self_feedback(self, event: AstrMessageEvent, session_key: str) -> str:
        if not self.settings.emotion.enabled or self.settings.emotion.self_feedback == "off":
            return self._reply_text(event)
        try:
            result = event.get_result()
            chain = getattr(result, "chain", None)
        except Exception:  # noqa: BLE001
            return ""
        if not chain:
            return ""

        pieces: list[str] = []
        tags: list[str] = []
        for component in chain:
            if type(component).__name__ != "Plain":
                continue
            raw = getattr(component, "text", None)
            if not isinstance(raw, str) or not raw:
                continue
            cleaned, found = self._strip_tags(raw)
            if found:
                tags.extend(found)
                try:
                    component.text = cleaned
                except Exception:  # noqa: BLE001
                    pass
            pieces.append(cleaned)

        text = "\n".join(pieces).strip()
        if not text:
            return ""

        proto_key: str | None = None
        strength = 0.0
        for tag in tags:
            match = _pad_tag_regex().search(tag)
            if match:
                pad = PAD.of(float(match.group(1)), float(match.group(2)), float(match.group(3)))
                proto_key, strength = classify(pad).key, 1.0
                break
            proto = resolve_prototype(tag)
            if proto is not None:
                proto_key, strength = proto.key, 0.85
                break

        if proto_key is None and self.settings.emotion.self_feedback == "heuristic":
            hint = heuristic_self_emotion(text)
            if hint is not None:
                proto_key, strength = hint

        if proto_key is not None and session_key not in self._frozen:
            now = time.time()
            session = self.engine.resolve(session_key, now)
            if self.engine.apply_self_expression(session, proto_key, strength, now):
                self._maybe_sample(session_key, session.emotion, "self", now, force=True)
                self._schedule_flush()
        return text

    @staticmethod
    def _strip_tags(text: str) -> tuple[str, list[str]]:
        import re

        found: list[str] = []

        def _collect(match: Any) -> str:
            found.append(match.group(1).strip())
            return ""

        cleaned = EMOTION_TAG_RE.sub(_collect, text)
        if found:
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned, found

    @staticmethod
    def _reply_text(event: AstrMessageEvent) -> str:
        try:
            result = event.get_result()
        except Exception:  # noqa: BLE001
            return ""
        chain = getattr(result, "chain", None)
        if not chain:
            return ""
        pieces: list[str] = []
        for component in chain:
            text = getattr(component, "text", None)
            if isinstance(text, str) and text.strip():
                pieces.append(text.strip())
        return "\n".join(pieces).strip()


    # ------------------------------------------------------------------
    # 拟人分段（把一条回复拆成几口气说出来）
    # ------------------------------------------------------------------







    # ---- 语音兼容 ----






    # ------------------------------------------------------------------
    # Web API（面板后端）
    # ------------------------------------------------------------------
    def _route_defs(self) -> list[tuple[str, Any, str]]:
        """面板的全部后端接口：(子路径, 处理函数, 说明)。

        精简后只剩「情绪 + 长期记忆 + 记忆图谱」三块。
        """
        return [
            ("sessions", self._api_sessions, "会话列表"),
            ("settings", self._api_settings, "通用设置"),
            ("settings/save", self._api_settings_save, "保存设置"),
            ("settings/reset", self._api_settings_reset, "恢复默认配置"),
            ("privacy", self._api_privacy, "隐私与数据"),
            ("privacy/save", self._api_privacy_save, "保存隐私设置"),
            ("privacy/deny", self._api_privacy_deny, "拒绝名单"),
            ("privacy/forget", self._api_privacy_forget, "忘掉某个人"),
            ("privacy/export", self._api_privacy_export, "导出档案"),
            ("transfer/export", self._api_transfer_export, "导出备份（设置 + 记忆）"),
            ("transfer/import", self._api_transfer_import, "导入备份（设置 + 记忆）"),
            ("manage", self._api_manage, "管理与维护"),
            ("manage/memory", self._api_manage_memory, "批量清理记忆"),
            ("manage/data", self._api_manage_data, "维护数据"),
            ("emotion", self._api_emotion, "情绪状态与曲线"),
            ("emotion/set", self._api_emotion_set, "设定当前情绪"),
            ("emotion/mood", self._api_emotion_mood, "调节心境"),
            ("emotion/intensity", self._api_emotion_intensity, "调节情绪强度"),
            ("emotion/point", self._api_emotion_point, "增删改曲线上的点"),
            ("emotion/reset", self._api_emotion_reset, "重置情绪"),
            ("emotion/curve/clear", self._api_curve_clear, "清空曲线"),
            ("emotion/freeze", self._api_freeze, "冻结/解冻自动情绪"),
            ("memory", self._api_memory, "记忆列表"),
            ("memory/add", self._api_memory_add, "手动添加记忆"),
            ("memory/update", self._api_memory_update, "修改记忆"),
            ("memory/delete", self._api_memory_delete, "删除记忆"),
            ("memory/batch", self._api_memory_batch, "批量管理记忆"),
            ("graph", self._api_graph, "记忆图谱"),
            ("_echo", self._api_echo, "自检：原样返回收到的请求信息"),
        ]

    def _register_web_apis(self) -> None:
        context = getattr(self, "context", None)
        register = getattr(context, "register_web_api", None)
        if not callable(register):
            logger.warning(
                "[ai_mind] 这个 AstrBot 版本没有 context.register_web_api，"
                "心智面板的后端接口不可用（聊天功能不受影响）"
            )
            return

        routes = self._route_defs()
        self._routes = {name: handler for name, handler, _ in routes}

        # 桥既有 GET 风格也有 POST 风格；而 AstrBot 的路由匹配会先看 HTTP 方法，
        # 只注册 GET 的话，一旦宿主用 POST 发就会直接落进「未找到该路由」。
        # 所以两种都注册，参数也从 query 与 body 两处一起读。
        methods = ["GET", "POST"]
        # 宿主可能用目录名，也可能用 plugin_id（作者/插件名）做命名空间，两边都铺。
        namespaces = (PLUGIN_NAME, PLUGIN_ID)

        ok = 0
        for namespace in namespaces:
            for name, handler, desc in routes:
                try:
                    register(f"/{namespace}/{name}", handler, methods, f"心智面板：{desc}")
                    ok += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[ai_mind] 注册面板接口 {namespace}/{name} 失败：{exc}")
            # 兜底：这个命名空间下没直接匹配上的路径都进这里。
            # 一来把宿主真正发的路径记进日志（上次排查就缺这个），
            # 二来按后缀分发，尽量把事情做成。
            try:
                register(f"/{namespace}/<path:rest>", self._api_fallback, methods, "心智面板：兜底")
                ok += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_mind] 注册兜底路由失败：{exc}")

        logger.info(
            f"[ai_mind] 心智面板后端已注册 {ok} 个路由"
            f"（命名空间 {'、'.join(namespaces)}，方法 GET+POST）"
        )

    async def _params(self) -> dict[str, Any]:
        """把 query 与请求体合并成一份参数表。

        桥有 GET 和 POST 两种风格，参数放哪儿也不一定，所以三处都读：
        查询串、JSON body、以及"读不出 JSON 就直接读原始 body"兜底。

        注意不要给 web_request.json() 传 default 之类的关键字参数 ——
        那是后加的，老版本会直接 TypeError，然后被这里的 except 吞掉，
        结果是所有 POST 接口都收不到参数（这个坑真踩过）。
        """
        merged: dict[str, Any] = {}
        if web_request is None:
            return merged
        try:
            for key in list(web_request.query.keys()):
                merged[key] = web_request.query.get(key)
        except Exception:  # noqa: BLE001
            pass

        body = await self._read_body()
        if isinstance(body, dict):
            # 只把**这些名字**当成包装层展开；别的字典是普通参数值，
            # 不能再被当成包装层丢掉 —— 这个坑 list 和 dict 各踩过一次。
            wrappers = ("params", "data", "query", "body")
            for wrapper in wrappers:
                inner = body.get(wrapper)
                if isinstance(inner, dict):
                    merged.update(inner)
            for key, value in body.items():
                if key in merged or key in wrappers:
                    continue
                merged[key] = value
        return merged

    async def _read_body(self) -> Any:
        """尽最大努力把请求体读成 dict。读不到就返回 None，绝不抛。"""
        if web_request is None:
            return None
        # 1) 先按 JSON 读（不传任何关键字参数，兼容老版本）
        try:
            data = await web_request.json()
            if isinstance(data, dict) and data:
                return data
        except Exception:  # noqa: BLE001
            pass
        # 2) 读不出就自己啃原始 body：JSON 或表单编码都认
        try:
            raw = await web_request.body()
        except Exception:  # noqa: BLE001
            return None
        if not raw:
            return None
        try:
            text = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
        except Exception:  # noqa: BLE001
            return None
        text = text.strip()
        if not text:
            return None
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, dict) else None
            except ValueError:
                return None
        # 表单编码
        try:
            parsed_form = {key: values[0] for key, values in urllib_parse.parse_qs(text).items() if values}
            return parsed_form or None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _int_param(params: dict[str, Any], name: str) -> tuple[int | None, str]:
        """取一个必填整数参数；取不到时，把"后端到底收到了什么"一并带回去。

        这样界面上弹出的错误就能直接说明问题，不用再靠猜。
        """
        try:
            value = int(params.get(name))
            if value:
                return value, ""
        except (TypeError, ValueError):
            pass
        keys = "、".join(sorted(str(key) for key in params.keys())) or "（空）"
        return None, "缺少参数 " + name + "（后端实际收到：" + keys + "）"

    async def _api_echo(self) -> Any:
        """自检接口：把后端看到的路径与方法原样吐回来，方便定位桥的约定。"""
        info: dict[str, Any] = {
            "ok": True,
            "plugin": PLUGIN_NAME,
            "plugin_id": PLUGIN_ID,
            "author": PLUGIN_AUTHOR,
        }
        if web_request is not None:
            try:
                info["path"] = web_request.path
                info["method"] = web_request.method
                info["plugin_name_seen"] = web_request.plugin_name
                info["username"] = web_request.username
                info["query"] = {k: web_request.query.get(k) for k in web_request.query.keys()}
            except Exception:  # noqa: BLE001
                pass
        info["params"] = await self._params()
        return json_response(info)

    async def _api_fallback(self, rest: str = "") -> Any:
        """兜底路由：命名空间下没有直接匹配上的请求都会落到这里。"""
        live = self._live_peer()
        if live is not None:
            # 我是重载时留下的旧实例：库关了、计数器是 0。
            # 转交给活着的那个，用户就不用重启整个 AstrBot 了。
            return await live._api_fallback(rest)
        sub = str(rest or "").strip("/")
        # 宿主可能又套了一层命名空间（.../astrbot_plugin_ai_mind/astrbot_plugin_ai_mind/...），
        # 这里把多余的剥掉，免得只能靠后缀去猜
        for prefix in (PLUGIN_NAME + "/", PLUGIN_ID + "/"):
            while sub.startswith(prefix):
                sub = sub[len(prefix):]
        table = getattr(self, "_routes", {})
        logger.info(f"[ai_mind] 面板兜底路由收到：{sub!r}")
        handler = table.get(sub)
        if handler is None:
            for name in sorted(table, key=len, reverse=True):
                if sub == name or sub.endswith("/" + name):
                    handler = table[name]
                    break
        if handler is None:
            return error_response(f"未知的面板接口：{sub}", status_code=404)
        try:
            return await handler()
        except Exception as exc:  # noqa: BLE001
            # 面板接口抛异常时，宿主只会回一个 502，用户看到的就是
            # 「Request failed with status code 502」—— 什么线索都没有。
            # 这里自己兜住：原始异常进日志，人话进面板。
            logger.error(f"[ai_mind] 面板接口 {sub} 出错：{exc}", exc_info=True)
            return error_response(
                f"{sub} 出错：{type(exc).__name__}: {exc}", status_code=500
            )

    async def _api_sessions(self) -> Any:
        entries = session_entries(
            engine=self.engine, sample_store=self.samples, memory_store=self.store
        )
        for entry in entries:
            entry["frozen"] = entry["key"] in self._frozen
        return json_response({"sessions": entries, "stats": self.store.stats()})

    async def _api_emotion(self) -> Any:
        params = await self._params()
        key = str(params.get("session") or "").strip() or self._session_from_request()
        if not key:
            return json_response({"sessions": [], "empty": True})
        try:
            hours = float(params.get("hours") or self.settings.panel.curve_hours)
        except (TypeError, ValueError):
            hours = self.settings.panel.curve_hours
        hours = max(0.5, min(hours, 24 * 365.0))
        payload = emotion_payload(
            engine=self.engine,
            sample_store=self.samples,
            session_key=key,
            hours=hours,
            max_points=self.settings.panel.max_points_returned,
        )
        payload["frozen"] = key in self._frozen
        # 让面板能直接说出「采样坏了、为什么坏」，而不是只显示一片空白
        sample_error = str(getattr(self.samples, "last_error", "") or "")
        payload["samples_broken"] = bool(self._sample_broken) or bool(sample_error)
        payload["samples_error"] = sample_error
        # 最坑的一种「坏了」：插件根本没启用。AstrBot 里停用的插件照样加载、
        # 面板照样打得开，但钩子一次都不会被调用 —— 于是情绪结算 0 次、
        # 曲线永远是空的，而日志里一个错都没有。
        payload["plugin_disabled"] = self._disabled_notice()
        try:
            payload["db_check"] = self.samples.selfcheck()
        except Exception as exc:  # noqa: BLE001
            payload["db_check"] = {"ok": False, "write": False, "error": str(exc), "rows": 0}
        payload["diag"] = dict(self._diag)
        payload["diag"]["emotion_enabled"] = bool(self.settings.emotion.enabled)
        payload["settings"] = {
            "curve_hours": self.settings.panel.curve_hours,
            "sample_epsilon": self.settings.panel.sample_epsilon,
            "heartbeat_seconds": self.settings.panel.heartbeat_seconds,
        }
        return json_response(payload)

    async def _api_emotion_set(self) -> Any:
        params = await self._params()
        key = self._session_or_latest(params)
        if not key:
            return error_response("还没有任何会话", status_code=400)
        try:
            pad = PAD.of(float(params.get("p", 0)), float(params.get("a", 0)), float(params.get("d", 0)))
        except (TypeError, ValueError):
            return error_response("情绪数值不合法", status_code=400)
        now = time.time()
        session = self.engine.resolve(key, now)
        self.engine.set_emotion(session, pad, now)
        # 强度也是这一发里可以带上的：0~1（写成 0~100 的整数也认）
        intensity = _parse_intensity(params.get("intensity"))
        if intensity is not None:
            self.engine.set_intensity(session, intensity, now)
        self._maybe_sample(key, session.emotion, "manual", now, force=True)
        self.engine.flush(now)
        return json_response({
            "ok": True,
            "emotion": pad.to_dict(),
            "intensity": round(session_intensity(session), 4),
        })


    async def _api_emotion_intensity(self) -> Any:
        """调节情绪强度（面板上的强度滑块走这里）。

        强度是**主动产物**：可以被直接设定，也会按情绪配置里的策略自己衰减；
        会话被冻结时它照样能被手动调，但不会自己衰减。
        """
        params = await self._params()
        key = self._session_or_latest(params)
        if not key:
            return error_response("还没有任何会话", status_code=400)
        value = _parse_intensity(params.get("intensity"))
        if value is None:
            return error_response("强度数值不合法（要 0~1 或 0~100）", status_code=400)
        now = time.time()
        session = self.engine.resolve(key, now)
        applied = self.engine.set_intensity(session, value, now)
        self._maybe_sample(key, session.emotion, "manual", now, force=True)
        self.engine.flush(now)
        level, level_name = level_of_intensity(applied)
        return json_response({
            "ok": True,
            "session": key,
            "intensity": round(applied, 4),
            "level": level,
            "level_name": level_name,
            "frozen": key in self._frozen,
            "decay": self.settings.emotion.intensity_decay,
        })
    async def _api_emotion_mood(self) -> Any:
        """**调节心境**（面板「调节心境」卡）—— 心境有自己的语义：

        它天生是「相对**心境基线**的偏移」，所以接口按这个优先级收：

        1. `word`   心境描述词（`mood_words_payload` 里的 key）→ 心境基线 + 词的偏移；
        2. `offset` {p,a,d} 偏移 → 心境基线 + 偏移（滑杆发的就是这个）；
        3. `p/a/d`  绝对值（兜底）；
        4. `action=reset` 只重置心境。

        与手动设定情绪一样：冻结时照样生效，改完**立刻换词**、不等驻留，
        并且**不算心境起因**（E2）。
        """
        params = await self._params()
        key = self._session_or_latest(params)
        if not key:
            return error_response("还没有任何会话", status_code=400)
        now = time.time()
        session = self.engine.resolve(key, now)
        action = str(params.get("action") or "set").strip().lower()
        # 偏移的零点是**心境基线**（气质基线 + 配置偏移）：面板滑杆的 0 位就是它，
        # 所以配置改了基线，页面上也不会看出多出一块偏移
        baseline = self.settings.emotion.mood_rest
        word_used = ""
        note = ""

        if action in {"reset", "重置"}:
            self.engine.reset_mood(session, now)
        elif action in {"freeze", "冻结"}:
            # **判定同情绪冻结**：冻住 = 心境完全不动（推进/衰减/换词/漂移都停），
            # 面板上的手动调仍然生效；情绪与强度不受影响。
            if not self.engine.freeze_mood(key):
                note = "心境已经冻着了"
        elif action in {"unfreeze", "解冻"}:
            if not self.engine.unfreeze_mood(key):
                note = "心境本来就没冻着"
            elif key in self._frozen:
                note = "会话仍处于冻结（情绪也冻着），心境要等会话解冻才会动"
        else:
            pad = None
            token = str(params.get("word") or "").strip()
            if token:
                word = resolve_mood_word(token)
                if word is None:
                    return error_response(f"不认识的心境词：{token}", status_code=400)
                pad = mood_word_pad(word, baseline)
                word_used = word.key
            if pad is None:
                raw = params.get("offset")
                if isinstance(raw, (dict, list, tuple)):
                    offset = PAD.from_any(raw)
                    pad = PAD.of(
                        baseline.p + offset.p, baseline.a + offset.a, baseline.d + offset.d,
                    ).clamped()
            if pad is None:
                try:
                    pad = PAD.of(
                        float(params.get("p", 0)), float(params.get("a", 0)),
                        float(params.get("d", 0)),
                    )
                except (TypeError, ValueError):
                    return error_response(
                        "心境数值不合法（要 word / offset / p a d）", status_code=400
                    )
            self.engine.set_mood(session, pad, now)

        self.engine.flush(now)
        label = session.mood_label(now, self.settings.emotion)
        offset_now = session.mood - PAD.from_any(baseline)
        return json_response({
            "ok": True,
            "action": action,
            "word": word_used,
            "mood": {
                "p": round(session.mood.p, 4),
                "a": round(session.mood.a, 4),
                "d": round(session.mood.d, 4),
            },
            # 滑杆是「偏移」语义，回一份给面板直接回填
            "offset": {
                "p": round(offset_now.p, 4),
                "a": round(offset_now.a, 4),
                "d": round(offset_now.d, 4),
            },
            "label": label.text,
            "emoji": label.emoji,
            "trend": session.mood_trend(),
            # 冻结状态：心境自己的 + 会话级的（面板取或显示）
            "mood_frozen": bool(session.frozen_mood),
            "session_frozen": key in self._frozen,
            "note": note,
            "frozen": key in self._frozen,
        })

    async def _api_emotion_point(self) -> Any:
        params = await self._params()
        key = self._session_or_latest(params)
        if not key:
            return error_response("还没有任何会话", status_code=400)
        action = str(params.get("action") or "update").lower()
        try:
            t = float(params.get("t"))
        except (TypeError, ValueError):
            return error_response("时间点不合法", status_code=400)
        try:
            tolerance = max(0.5, float(params.get("tolerance") or 2.0))
        except (TypeError, ValueError):
            tolerance = 2.0

        now = time.time()
        session = self.engine.resolve(key, now)

        if action == "delete":
            removed = self.samples.delete_at(key, t, tolerance=tolerance)
            # 强度是另一套锚点（事件里的 intensity）：一起删才算「删掉附近的点」
            marks = self.engine.edit_intensity_marks(session, t, tolerance, mode="delete")
            self.engine.flush(now)
            return json_response({
                "ok": True, "removed": removed, "removed_intensity": marks,
            })

        try:
            pad = PAD.of(float(params.get("p", 0)), float(params.get("a", 0)), float(params.get("d", 0)))
        except (TypeError, ValueError):
            return error_response("情绪数值不合法", status_code=400)

        if action == "insert":
            self.samples.append(key, t, pad, "manual")
            self._last_sample.pop(key, None)
            marks = self.engine.edit_intensity_marks(session, t, tolerance, mode="insert")
            self.engine.flush(now)
            return json_response({
                "ok": True, "inserted": True, "inserted_intensity": marks,
            })

        changed = self.samples.replace_at(key, t, pad, tolerance=tolerance)
        if not changed:
            self.samples.append(key, t, pad, "manual")
            changed = 1
        self._last_sample.pop(key, None)
        # 「改成当前滑块的值」也要改到强度：附近锚点统一改成当前强度
        marks = self.engine.edit_intensity_marks(
            session, t, tolerance, mode="update", value=session_intensity(session),
        )
        self.engine.flush(now)
        return json_response({
            "ok": True, "changed": changed, "changed_intensity": marks,
        })

    async def _api_emotion_reset(self) -> Any:
        params = await self._params()
        key = self._session_or_latest(params)
        if not key:
            return error_response("还没有任何会话", status_code=400)
        now = time.time()
        session = self.engine.resolve(key, now)
        self.engine.reset(session, now, keep_relations=bool(params.get("keep_relations", True)))
        self._maybe_sample(key, session.emotion, "manual", now, force=True)
        self.engine.flush(now)
        return json_response({"ok": True})

    async def _api_curve_clear(self) -> Any:
        params = await self._params()
        key = self._session_or_latest(params)
        if not key:
            return error_response("还没有任何会话", status_code=400)
        removed = self.samples.clear(key)
        self._last_sample.pop(key, None)
        return json_response({"ok": True, "removed": removed})

    async def _api_freeze(self) -> Any:
        params = await self._params()
        key = self._session_or_latest(params)
        if not key:
            return error_response("还没有任何会话", status_code=400)
        frozen = bool(params.get("frozen", True))
        if frozen:
            self.engine.freeze(key)
            # 冻结点打一个采样：曲线上从这里开始保持水平（见 panel.build_curve）
            try:
                session = self.engine.resolve(key)
                self._maybe_sample(key, session.emotion, "manual", time.time(), force=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_mind] 冻结时打点失败：{exc}")
        else:
            self.engine.unfreeze(key)
        self.engine.flush(time.time())
        return json_response({"ok": True, "frozen": frozen})

    async def _api_memory(self) -> Any:
        params = await self._params()

        def _pick(name: str, default: Any) -> Any:
            value = params.get(name)
            return default if value in (None, "") else value

        try:
            limit = int(_pick("limit", 50) or 50)
            offset = int(_pick("offset", 0) or 0)
        except (TypeError, ValueError):
            limit, offset = 50, 0
        # 面板顶部的会话选择器粘到这一页：选中哪个会话就看哪个会话的记忆
        # （外加没绑会话的通用记忆）。不传 session 时保持原来的「看全部」。
        session_id = str(_pick("session", "") or "").strip()
        payload = memory_payload(
            store=self.store,
            query=str(_pick("q", "") or ""),
            scope=str(_pick("scope", "") or ""),
            kind=str(_pick("kind", "") or ""),
            pinned=_pick("pinned", None),
            sensitive=_pick("sensitive", None),
            session_id=session_id,
            source=str(_pick("source", "") or ""),
            limit=max(1, min(limit, 200)),
            offset=max(0, offset),
        )
        payload["stats"] = dict(payload.get("stats") or {})
        if session_id:
            try:
                payload["stats"]["session"] = int(
                    self.store.count_for_session(session_id)
                )
                payload["stats"]["total"] = int(
                    payload["stats"].get("total") or 0
                )
            except Exception:  # noqa: BLE001
                pass
        return json_response(payload)

    async def _api_graph(self) -> Any:
        """记忆图谱：节点 + 边 + 簇，布局交给浏览器算。"""
        params = await self._params()

        def _number(name: str, default: float, low: float, high: float) -> float:
            raw = params.get(name)
            if raw is None or raw == "":
                return default
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        kinds = _split_keywords(params.get("kind") or params.get("kinds"))
        scopes = _split_keywords(params.get("scope") or params.get("scopes"))
        # 顶部会话选择器直接决定这张图里有哪些记忆：只画选中会话的
        # （外加没绑会话的通用记忆）。session_id 是给老桥用的别名。
        session_id = str(
            params.get("session_id") or params.get("session") or ""
        ).strip()
        return json_response(
            graph_engine.build_graph(
                self.store,
                kinds=kinds,
                scopes=scopes,
                session_id=session_id,
                query=str(params.get("q") or ""),
                limit=int(_number("limit", 260, 10, 800)),
                min_importance=_number("min_importance", 0.0, 0.0, 1.0),
                pinned_only=str(params.get("pinned_only") or "").lower()
                in {"1", "true", "yes", "on"},
            )
        )


    async def _api_memory_add(self) -> Any:
        """手动加一条记忆。

        面板顶部的会话选择器就是「加到哪儿」：手动加的这条会挂在选中会话上，
        所以它立刻会出现在该会话的记忆列表和记忆图谱里。
        关键词留空时自动从内容里抽 —— 图谱上的灰色小点就是这些词。
        """
        params = await self._params()
        content = str(params.get("content") or "").strip()
        if len(content) < 2:
            return error_response("内容太短", status_code=400)

        # 类型必须落在这几个里面，否则图谱和列表的分类会乱
        kind = str(params.get("kind") or "fact").strip().lower() or "fact"
        if kind not in set(KINDS):
            kind = "fact"

        reasons = tuple(detect_sensitive(content)) if self.settings.memory.scan_sensitive else ()
        sensitive = bool(reasons)

        keywords = _split_keywords(params.get("keywords"))
        if not keywords:
            # 没手填就自动生成：这是「加完能在图谱里看到关键词点」的关键一步
            try:
                keywords = list(derive_keywords(content, limit=6))
            except Exception as exc:  # noqa: BLE001 - 抽词失败不该挡住记录
                logger.warning(f"[ai_mind] 自动抽关键词失败，先不加关键词：{exc}")
                keywords = []

        # 作用域不再暴露给面板，但隐私红线要保住：
        # 敏感内容永远只留在「本人私聊」，其余挂到当前选中的会话上。
        if sensitive:
            scope = "user"
        else:
            scope = str(params.get("scope") or "").strip() or "session"
            if scope not in {"session", "user", "global"}:
                scope = "session"

        session_id = str(params.get("session") or "").strip() or self._session_or_latest(params)
        owner_id = str(params.get("owner_id") or "").strip()
        if scope == "user" and not owner_id:
            owner_id = (str(params.get("owner") or "").strip()
                        or self._owner_of_session(session_id))

        now = time.time()
        memory = Memory(
            content=content,
            keywords=tuple(str(k).strip() for k in keywords if str(k).strip()),
            kind=kind,
            scope=scope,
            # 图省事不再有「常驻」开关：手动记的东西默认一直保存
            session_id=session_id if scope != "global" else "",
            owner_id=owner_id if scope == "user" else "",
            importance=float(params.get("importance") or 1.0),
            pinned=True,
            sensitive=sensitive,
            sensitive_reasons=reasons,
            source="manual",
            created_at=now,
            updated_at=now,
        )
        memory.id = self.store.add(memory)
        if not memory.id:
            return error_response("写入失败", status_code=500)
        return json_response({
            "ok": True,
            "id": memory.id,
            "sensitive": sensitive,
            "keywords": list(memory.keywords),
            "session_id": memory.session_id,
            "scope": memory.scope,
        })

    async def _api_memory_update(self) -> Any:
        params = await self._params()
        try:
            memory_id = int(params.get("id"))
        except (TypeError, ValueError):
            return error_response("缺少 id", status_code=400)
        memory = self.store.get(memory_id)
        if memory is None:
            return error_response("记忆不存在", status_code=404)
        content = str(params.get("content") or memory.content).strip()
        if len(content) < 2:
            return error_response("内容太短", status_code=400)
        sensitive = bool(detect_sensitive(content)) if self.settings.memory.scan_sensitive else False
        now = time.time()
        try:
            importance = float(params.get("importance", memory.importance))
        except (TypeError, ValueError):
            importance = memory.importance
        self.store._execute(
            "UPDATE memories SET content = ?, pinned = ?, importance = ?, sensitive = ?, updated_at = ? WHERE id = ?",
            (
                content,
                1 if bool(params.get("pinned", memory.pinned)) else 0,
                importance,
                1 if sensitive else 0,
                now,
                memory_id,
            ),
        )
        return json_response({"ok": True})

    async def _api_memory_batch(self) -> Any:
        """批量管理记忆：删除 / 设为常驻 / 取消常驻 / 改作用域。"""
        params = await self._params()
        ids = _coerce_int_list(params.get("ids"))
        if not ids:
            return error_response("没有选中任何记忆", status_code=400)
        action = str(params.get("action") or "").strip().lower()
        if action in {"delete", "remove", "删除"}:
            affected = self.store.delete_many(ids)
        elif action in {"pin", "set_pinned", "常驻"}:
            affected = self.store.update_many(ids, pinned=True)
        elif action in {"unpin", "unset_pinned", "取消常驻"}:
            affected = self.store.update_many(ids, pinned=False)
        elif action in {"scope", "set_scope", "作用域"}:
            scope = str(params.get("scope") or "").strip().lower()
            if scope not in {"session", "user", "global"}:
                return error_response("作用域不合法", status_code=400)
            fields: dict[str, Any] = {"scope": scope}
            # 换作用域时必须把不属于它的归属字段清掉，
            # 否则会留下"scope=global 却还挂着某个 session_id"的孤儿记忆
            if scope == "global":
                fields["session_id"] = ""
                fields["owner_id"] = ""
            elif scope == "user":
                fields["session_id"] = ""
            else:
                fields["owner_id"] = ""
                # 「设成当前会话私有」要指明是哪个会话，不传就用最活跃的那个
                sid = str(params.get("session") or "").strip() or self._session_from_request()
                if sid:
                    fields["session_id"] = sid
            affected = self.store.update_many(ids, **fields)
        else:
            return error_response("不认识的操作：" + (action or "（空）"), status_code=400)

        return json_response({"ok": True, "action": action, "affected": affected, "ids": ids})

    async def _api_memory_delete(self) -> Any:
        params = await self._params()
        memory_id, why = self._int_param(params, "id")
        if memory_id is None:
            return error_response(why, status_code=400)
        return json_response({"ok": bool(self.store.delete(memory_id))})

    def _session_from_request(self) -> str:
        sessions = self.engine.session_keys()
        if sessions:
            sessions.sort(key=lambda k: self.engine.sessions[k].last_interaction, reverse=True)
            return sessions[0]
        return ""

    def _owner_of_session(self, session_id: str) -> str:
        """这个会话里主要在跟谁说话 —— 敏感记忆要挂到他名下才会被放行。

        `allows()` 对敏感记忆要求 owner 等于说话人；面板上没有「关于谁」这个
        下拉了（记忆跟着顶部选中的会话走），所以这里从会话里反查那个人。
        私聊查不到关系时，退化成从会话 key 尾部取 uid。
        """
        try:
            session = self.engine.sessions.get(str(session_id or ""))
            users = getattr(session, "users", None) if session else None
            if users:
                return str(max(users.values(), key=lambda r: r.last_seen or 0).uid)
        except Exception:  # noqa: BLE001
            pass
        if not is_group_session(str(session_id or "")):
            parts = str(session_id or "").split(":")
            if len(parts) >= 3:
                return parts[-1]
        return ""

    def _session_or_latest(self, params: dict[str, Any]) -> str:
        key = str((params or {}).get("session") or "").strip()
        return key or self._session_from_request()











    # ------------------------------------------------------------------
    # 面板覆盖配置的读写（人格预设页与拟人分段面板接口都已删除）
    # ------------------------------------------------------------------










    def _load_simple_overrides(self, filename: str, label: str) -> dict[str, Any]:
        """读一份「面板覆盖配置」（存在插件数据目录里，盖在 config 之上）。"""
        path = self.data_dir / filename
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    return data
        except Exception as exc:  # noqa: BLE001
            logger.warning(f'[ai_mind] 读取{label}覆盖配置失败：{exc}')
        return {}

    def _save_simple_overrides(self, filename: str, data: dict[str, Any], label: str) -> bool:
        path = self.data_dir / filename
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            json.loads(path.read_text(encoding='utf-8'))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f'[ai_mind] 保存{label}覆盖配置失败：{exc}', exc_info=True)
            return False






    # ------------------------------------------------------------------
    # 隐私：拒绝名单 / 忘我 / 导出
    # ------------------------------------------------------------------
    def _is_denied(self, uid: str, platform: str = "", platform_id: str = "") -> bool:
        """这个人在拒绝名单里吗（既不采集也不注入）。"""
        for entry in self.settings.privacy.denied_users or ():
            if identity_engine.identity_matches(entry, uid, platform, platform_id):
                return True
        return False

    def _sender_denied(self, event: AstrMessageEvent) -> bool:
        try:
            uid, platform, platform_id = self._identity_of(event)
        except Exception:  # noqa: BLE001
            return False
        return self._is_denied(uid, platform, platform_id)

    def _filter_denied_memories(self, memories: Any) -> list[Any]:
        """把"关于被拒绝的人"的记忆从注入里摘掉。"""
        denied = set()
        for entry in self.settings.privacy.denied_users or ():
            scope, target = identity_engine.parse_identity(entry)
            if target and target != "*":
                denied.add(target)
        if not denied:
            return list(memories or ())
        return [
            item for item in (memories or ())
            if str(getattr(item, "owner_id", "") or "") not in denied
        ]

    def _privacy_summary(self, uid: str) -> dict[str, Any]:
        """某个人在插件里留下了什么（忘我和导出都用它）。"""
        if not uid:
            return {"memories": 0, "relations": 0, "lexicon_hits": 0}
        memories = 0
        try:
            memories = int(self.store.count_for_owner(uid))
        except Exception:  # noqa: BLE001
            memories = 0
        relations = 0
        try:
            relations = len([
                relation for _session, relation in self.engine.all_relations()
                if str(getattr(relation, "uid", "") or "") == uid
            ])
        except Exception:  # noqa: BLE001
            relations = 0
        hits = 0
        try:
            conn = self.lexicons.conn
            if conn is not None:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM lexicon_hits WHERE uid = ?", (uid,)
                ).fetchone()
                hits = int(row["c"] or 0) if row else 0
        except Exception:  # noqa: BLE001
            hits = 0
        return {"memories": memories, "relations": relations,
                "lexicon_hits": hits}

    def _forget_user(self, uid: str, *, include_group: bool = False) -> dict[str, int]:
        """把他留下的东西全删掉。"""
        removed = {"memories": 0, "relations": 0, "lexicon_hits": 0}
        if not uid:
            return removed
        try:
            removed["memories"] = int(self.store.clear(owner_id=uid) or 0)
            if include_group:
                for session in self.store.distinct_sessions():
                    if uid in str(session):
                        removed["memories"] += int(
                            self.store.clear(session_id=str(session)) or 0
                        )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 删除记忆失败：{exc}")
        try:
            removed["relations"] = int(self.engine.forget_user(uid) or 0)
            self.engine.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 删除关系失败：{exc}")
        try:
            removed["lexicon_hits"] = int(self.lexicons.clear_by_uid(uid) or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 删除词表命中失败：{exc}")
        return removed

    def _export_user(self, uid: str) -> dict[str, Any]:
        """导出某个人的档案（给自己留一份，或者交给本人）。"""
        payload: dict[str, Any] = {
            "uid": uid,
            "exported_at": time.time(),
            "plugin": PLUGIN_NAME,
            "version": "v2.2.0",
            "memories": [],
            "relations": [],
        }
        try:
            rows, _total = self.store.query_page(limit=1000, offset=0)
            payload["memories"] = [
                {
                    "content": item.content,
                    "kind": item.kind,
                    "scope": item.scope,
                    "pinned": bool(item.pinned),
                    "sensitive": bool(item.sensitive),
                    "importance": round(float(item.importance or 0.0), 2),
                    "created_at": item.created_at,
                }
                for item in rows
                if str(getattr(item, "owner_id", "") or "") == uid
            ]
        except Exception:  # noqa: BLE001
            pass
        try:
            for session, relation in self.engine.all_relations():
                if str(getattr(relation, "uid", "") or "") != uid:
                    continue
                payload["relations"].append({
                    "session": session,
                    "name": relation.name,
                    "msg_count": int(relation.msg_count or 0),
                })
        except Exception:  # noqa: BLE001
            pass
        return payload

    async def _api_privacy(self) -> Any:
        params = await self._params()
        uid = str(params.get("uid") or "").strip() or str(self._last_sender.get(
            str(params.get("session") or ""), ""
        ) or "")
        return json_response({
            "settings": self.settings.privacy.to_dict(),
            "denied": list(self.settings.privacy.denied_users),
            "uid": uid,
            "summary": self._privacy_summary(uid) if uid else {},
            "has_overrides": bool(self._privacy_data),
        })

    async def _api_privacy_save(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = dict(params)
        raw = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
        known = set(PrivacySettings().to_dict().keys())
        incoming = {
            str(key): value for key, value in (raw or {}).items()
            if str(key) in known and value is not None
        }
        if not incoming:
            return error_response("没有收到任何要保存的设置")
        merged = dict(self._privacy_data)
        merged.update(incoming)
        self._privacy_data = merged
        if not self._save_simple_overrides("privacy_overrides.json", merged, "隐私"):
            return error_response("写入隐私设置失败，详细原因见 AstrBot 日志")
        self._reload_from_config()
        result = {
            "settings": self.settings.privacy.to_dict(),
            "denied": list(self.settings.privacy.denied_users),
            "has_overrides": True,
            "saved": sorted(incoming.keys()),
        }
        return json_response(result)

    async def _api_privacy_deny(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = dict(params)
        entry = str(payload.get("uid") or "").strip()
        action = str(payload.get("action") or "add").strip().lower()
        if not entry:
            return error_response("要指定一个人")
        current = list(self.settings.privacy.denied_users)
        if action in {"remove", "allow", "移除", "允许"}:
            current = [item for item in current if item != entry]
        else:
            if entry not in current:
                current.append(entry)
        merged = dict(self._privacy_data)
        merged["denied_users"] = current
        self._privacy_data = merged
        if not self._save_simple_overrides("privacy_overrides.json", merged, "隐私"):
            return error_response("写入拒绝名单失败")
        self._reload_from_config()
        logger.info(f"[ai_mind] 拒绝名单更新：{action} {entry}（共 {len(current)} 人）")
        return json_response({
            "settings": self.settings.privacy.to_dict(),
            "denied": list(self.settings.privacy.denied_users),
            "action": action,
        })

    async def _api_privacy_forget(self) -> Any:
        """忘我 / 管理员删人：把某个人留下的东西全删掉。"""
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = dict(params)
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            return error_response("要指定删谁的")
        include_group = str(payload.get("include_group") or "").lower() in {
            "1", "true", "yes", "on"
        }
        removed = self._forget_user(uid, include_group=include_group)
        logger.warning(f"[ai_mind] 忘掉 {uid}：{removed}")
        return json_response({"ok": True, "uid": uid, "removed": removed,
                              "summary": self._privacy_summary(uid)})

    async def _api_privacy_export(self) -> Any:
        """导出某个人的档案 —— **必须落盘并告诉用户文件在哪儿**。"

        老版本只把 JSON 从接口吐回去，面板上只剩一句「导出档案完成」，
        东西在哪儿、写没写成全都不知道。现在写进
        `<数据目录>/exports/<uid>-<时间>.json`，路径、大小、条数一起回给面板。
        """
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = dict(params)
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            return error_response("要指定导出谁的")
        data = self._export_user(uid)
        safe = "".join(ch if (ch.isalnum() or ch in "-_.:") else "_" for ch in uid)
        safe = safe.strip("._")[:60] or "user"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            export_dir = Path(self.data_dir) / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            target = export_dir / f"{safe}-{stamp}.json"
            target.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            size = int(target.stat().st_size)
        except Exception as exc:  # noqa: BLE001 - 写不进去要明说，别假装成功
            logger.warning(f"[ai_mind] 导出 {uid} 的档案写盘失败：{exc}")
            return error_response(f"导出失败：文件写不到磁盘（{exc}）", status_code=500)
        logger.info(
            f"[ai_mind] 导出 {uid} 的档案 -> {target}"
            f"（{len(data['memories'])} 条记忆、{len(data['relations'])} 条关系，{size} 字节）"
        )
        return json_response({
            "ok": True,
            "path": str(target),
            "file": target.name,
            "bytes": size,
            "memories": len(data["memories"]),
            "relations": len(data["relations"]),
            "archive": data,
        })


    # ------------------------------------------------------------------
    # 备份与迁移：设置 + 全部记忆
    # （记忆图谱是按记忆现算的 —— 记忆导进去了，图就跟着回来了）
    # ------------------------------------------------------------------
    #: 备份文件的自报家门：对不上就不认，免得把别的 JSON 灌进配置和记忆库
    BACKUP_KIND = "ai_mind.backup/v1"

    def _iter_all_memories(self) -> Iterable[Memory]:
        """分页把**全部**记忆翻出来：别的会话、别人的、通用（global）的都要带上。"""
        offset = 0
        while True:
            rows, total = self.store.query_page(limit=500, offset=offset)
            if not rows:
                return
            for item in rows:
                yield item
            offset += len(rows)
            if offset >= int(total or 0):
                return

    @staticmethod
    def _memory_to_backup(mem: Memory) -> dict[str, Any]:
        """一条记忆 → 可移植的 JSON。

        **故意不带向量**：embedding 比正文大几百倍，搬过去还得按新机器的模型重算；
        缺向量的记忆会被后台的 embed_pending 慢慢补上（没配向量模型就走词法检索），
        文件本身也因此能小到随手就拷走。
        """
        return {
            "content": str(mem.content or ""),
            "keywords": [str(k) for k in (mem.keywords or ())],
            "kind": str(mem.kind or ""),
            "subject": str(mem.subject or ""),
            "scope": str(mem.scope or "session"),
            "session_id": str(mem.session_id or ""),
            "owner_id": str(mem.owner_id or ""),
            "importance": round(float(mem.importance or 0.0), 4),
            "pinned": bool(mem.pinned),
            "sensitive": bool(mem.sensitive),
            "sensitive_reasons": [str(x) for x in (mem.sensitive_reasons or ())],
            "source": str(mem.source or "auto"),
            "created_at": float(mem.created_at or 0.0),
            "updated_at": float(mem.updated_at or 0.0),
            "last_hit": float(mem.last_hit or 0.0),
            "hit_count": int(mem.hit_count or 0),
        }

    @staticmethod
    def _backup_memory_key(content: Any, scope: Any, session_id: Any, owner_id: Any) -> tuple[str, str, str, str]:
        """去重口径：同正文 + 同作用域 + 同会话 + 同主人，才算同一条记忆。"""
        return (
            str(content or "").strip(),
            str(scope or ""),
            str(session_id or ""),
            str(owner_id or ""),
        )

    def _existing_memory_keys(self) -> set[tuple[str, str, str, str]]:
        """库里已有记忆的去重键（导入前后各取一次，用来核对是不是真的进去了）。"""
        return {
            self._backup_memory_key(m.content, m.scope, m.session_id, m.owner_id)
            for m in self._iter_all_memories()
        }

    def _live_sessions(self) -> set[str]:
        """这台机器上**真的跟她说过话**的会话（有 relation 记录）。

        备份里那些 session_id 不在这里面的会话，就是「还没激活的会话」——
        记忆照存不误（导入不校验会话存不存在），等那个会话第一条消息来了自动生效，
        面板的会话下拉会把它们标成「（未激活）」。

        **口径必须和面板 session_entries() 的 active 完全一致**（下拉怎么标、
        导入就怎么报）；而且只认 relation：面板一点开会话就 resolve() 出空会话、
        心跳还会给它打点 —— 拿「有没有情绪状态」当判据，标记点一下就自己没了。
        """
        keys: set[str] = set()
        try:
            for key, session in self.engine.sessions.items():
                if getattr(session, "users", None):
                    keys.add(str(key))
        except Exception:  # noqa: BLE001 - 情绪状态不可用不该拖垮导入
            pass
        return {key for key in keys if key}

    def _backup_bundle(self) -> dict[str, Any]:
        """整份可迁移的备份：**设置（配置页每一项）+ 全部记忆**。"""
        defaults = self._schema_defaults()
        settings: dict[str, Any] = {}
        for path in sorted(self._settings_leaf_paths()):
            value = cfg_get(self.config, path, None)
            if value is None:
                value = defaults.get(path)
            if value is None:
                continue
            settings[path] = value
        memories = [self._memory_to_backup(m) for m in self._iter_all_memories()]
        return {
            "kind": self.BACKUP_KIND,
            "plugin": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
            "exported_at": time.time(),
            "settings": settings,
            "memories": memories,
            "counts": {
                "settings": len(settings),
                "memories": len(memories),
                "global": sum(1 for m in memories if m["scope"] == "global"),
                "sessions": len({m["session_id"] for m in memories if m["session_id"]}),
            },
        }

    def _load_backup_file(self, raw: str) -> dict[str, Any]:
        """按文件名读备份 —— 只认插件数据目录下 exports/ 底下的文件（防路径穿越）。

        搬家的实际路径就是：A 机器导出 → 把文件拷进 B 机器的 exports/ → B 机器按文件名导入。
        """
        name = str(raw or "").strip()
        if not name:
            raise ValueError("没给文件名")
        base = (Path(self.data_dir) / "exports").resolve()
        candidate = Path(name)
        if not candidate.is_absolute():
            candidate = base / name
        candidate = candidate.resolve()
        if not candidate.is_relative_to(base):
            raise ValueError("只允许导入 exports/ 目录下的备份文件")
        if not candidate.is_file():
            raise ValueError(f"找不到文件：{candidate.name}")
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"文件不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("备份文件的顶层不是 JSON 对象")
        return data

    @staticmethod
    def _coerce_backup_setting(node: Any, value: Any) -> tuple[bool, Any]:
        """按 schema 的 type 把备份里的值洗成配置收得下的样子；洗不动就返回 (False, None)。"""
        kind = ""
        if isinstance(node, dict):
            kind = str(node.get("type") or "").strip().lower()
        if kind == "bool":
            if isinstance(value, bool):
                return True, value
            if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) in (0.0, 1.0):
                return True, bool(value)
            if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                return True, value.strip().lower() in {"true", "1", "yes", "on"}
            return False, None
        if kind == "int":
            if isinstance(value, bool):
                return False, None
            if isinstance(value, int):
                return True, int(value)
            if isinstance(value, float) and float(value).is_integer():
                return True, int(value)
            if isinstance(value, str):
                try:
                    return True, int(float(value.strip()))
                except ValueError:
                    return False, None
            return False, None
        if kind == "float":
            if isinstance(value, bool):
                return False, None
            if isinstance(value, (int, float)):
                return True, float(value)
            if isinstance(value, str):
                try:
                    return True, float(value.strip())
                except ValueError:
                    return False, None
            return False, None
        if kind in {"string", "text"}:
            if isinstance(value, str):
                return True, value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return True, str(value)
            return False, None
        if kind in {"list", "file", "template_list"}:
            return (True, list(value)) if isinstance(value, (list, tuple)) else (False, None)
        if kind in {"object", "dict"}:
            return (True, dict(value)) if isinstance(value, dict) else (False, None)
        # 认不出的类型：原样放过去，让保存那一步去兜
        return True, value

    def _import_settings(self, incoming: Any) -> dict[str, Any]:
        """把备份里的设置写回配置：不认识的跳过、类型对不上的跳过、写完逐项读回核对。"""
        if not isinstance(incoming, dict):
            return {"applied": 0, "paths": [], "skipped": [], "not_saved": [], "writable": True}
        known = self._settings_leaf_paths()
        leaves = self._schema_leaves()
        applied: dict[str, Any] = {}
        skipped: list[dict[str, Any]] = []
        for path in sorted(str(k) for k in incoming.keys()):
            value = incoming.get(path)
            if path not in known:
                skipped.append({"path": path, "why": "不认识的配置项（插件版本对不上？）"})
                continue
            if value is None:
                skipped.append({"path": path, "why": "备份里这一项是空的"})
                continue
            ok, coerced = self._coerce_backup_setting(leaves.get(path), value)
            if not ok:
                skipped.append({"path": path, "why": f"类型对不上（备份里是 {type(value).__name__}）"})
                continue
            applied[path] = coerced

        writable = callable(getattr(self.config, "save_config", None))
        if applied and not writable:
            # 配置回写不了就**先停下**：不能让「设置没进去、记忆进去了」这种半截状态发生
            raise RuntimeError(
                "这个 AstrBot 版本的插件配置不支持回写，设置没法导入"
                "（配置页的保存也一样）——请到 AstrBot 的「配置」页手动改，或手动编辑配置文件"
            )
        not_saved: list[dict[str, Any]] = []
        if applied and writable:
            for path, value in applied.items():
                self._set_config_value(path, value)
            save = getattr(self.config, "save_config", None)
            try:
                save()
            except TypeError:
                try:
                    save(dict(self.config))
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(f"写入配置文件失败：{exc}") from exc
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"写入配置文件失败：{exc}") from exc
            self._settings_saved_at = time.time()
            try:
                self._reload_from_config()
            except Exception as exc:  # noqa: BLE001 - 落盘成功就算成功，热更新失败只告警
                logger.warning(f"[ai_mind] 导入备份后热更新失败：{exc}")
            not_saved = self._verify_saved(applied)
            logger.info(f"[ai_mind] 导入备份写回 {len(applied)} 项设置")
        return {
            "applied": len(applied),
            "paths": sorted(applied),
            "skipped": skipped,
            "not_saved": not_saved,
            "writable": writable,
        }

    def _import_memories(
        self, incoming: Any, *, replace: bool, live_sessions: set[str] | None = None,
    ) -> dict[str, Any]:
        """把备份里的记忆灌进库：同作用域同正文算重复，直接跳过（导几次都一样）。"""
        if not isinstance(incoming, list):
            incoming = []
        cleaned: list[Memory] = []
        invalid = 0
        for raw in incoming:
            if not isinstance(raw, dict):
                invalid += 1
                continue
            content = str(raw.get("content") or "").strip()
            if not content:
                invalid += 1
                continue
            kind = str(raw.get("kind") or "").strip().lower()
            if kind not in set(KINDS):
                kind = "fact"
            scope = str(raw.get("scope") or "session").strip().lower()
            if scope not in {"session", "user", "global"}:
                scope = "session"
            keywords = raw.get("keywords")
            reasons = raw.get("sensitive_reasons")

            def _num(key: str, default: float = 0.0) -> float:
                try:
                    return float(raw.get(key, default) or default)
                except (TypeError, ValueError):
                    return default

            created = _num("created_at") or time.time()
            cleaned.append(Memory(
                content=content[:300],
                keywords=tuple(str(k) for k in keywords) if isinstance(keywords, (list, tuple)) else (),
                kind=kind,
                subject=str(raw.get("subject") or ""),
                scope=scope,
                session_id=str(raw.get("session_id") or ""),
                owner_id=str(raw.get("owner_id") or ""),
                importance=max(0.0, min(1.0, _num("importance", 0.5))),
                pinned=bool(raw.get("pinned")),
                sensitive=bool(raw.get("sensitive")),
                sensitive_reasons=tuple(str(x) for x in reasons) if isinstance(reasons, (list, tuple)) else (),
                source=str(raw.get("source") or "auto"),
                created_at=created,
                updated_at=_num("updated_at") or created,
                last_hit=_num("last_hit"),
                hit_count=int(_num("hit_count")),
            ))

        cleared = 0
        if replace:
            cleared = int(self.store.clear(everything=True) or 0)

        existing = self._existing_memory_keys()
        added = duplicates = failed = 0
        for mem in cleaned:
            key = self._backup_memory_key(mem.content, mem.scope, mem.session_id, mem.owner_id)
            if key in existing:
                duplicates += 1
                continue
            try:
                if self.store.add(mem):
                    existing.add(key)
                    added += 1
                else:
                    failed += 1
            except Exception as exc:  # noqa: BLE001 - 单条写不进去不该毁掉整次导入
                logger.warning(f"[ai_mind] 导入记忆失败（{mem.content[:24]}…）：{exc}")
                failed += 1

        # 逐条核对：备份里**有效**的记忆是不是都在库里了 —— 少一条都要报出来
        after = self._existing_memory_keys()
        want = {
            self._backup_memory_key(m.content, m.scope, m.session_id, m.owner_id)
            for m in cleaned
        }
        missing = sorted(want - after)

        # 「待激活」：绑在这台机器上还**没出现过**的会话上的记忆。
        # 它们**不清、不丢** —— 只是在那个会话第一条消息来之前，检索不会用到它们；
        # 面板的会话下拉已经能选到（distinct_sessions 从记忆里取），
        # 那边一聊起来就自动读到，不用再导一次。
        live = set(live_sessions or ())
        dormant_sessions = sorted({
            str(m.session_id) for m in cleaned
            if m.session_id and str(m.session_id) not in live
        })
        dormant_count = sum(
            1 for m in cleaned
            if m.session_id and str(m.session_id) not in live
        )
        return {
            "dormant": {
                "memories": dormant_count,
                "sessions_total": len(dormant_sessions),
                "sessions": dormant_sessions[:8],
                "note": (
                    "这些会话这台机器上还没聊过 —— 记忆已经存好、不会被清掉；"
                    "那个会话第一条消息来了会自动读到，不用再导一次"
                ),
            },
            "added": added,
            "duplicates": duplicates,
            "invalid": invalid,
            "failed": failed,
            "cleared": cleared,
            "total": len(after),
            "expected": len(want),
            "missing": len(missing),
            "missing_sample": [item[0][:40] for item in missing[:5]],
        }

    async def _api_transfer_export(self) -> Any:
        """导出备份：设置 + 全部记忆。**落盘并把文件位置报给面板**（照「导出档案」的老规矩）。"""
        try:
            bundle = self._backup_bundle()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 打包备份失败：{exc}")
            return error_response(f"打包失败：{exc}", status_code=500)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            export_dir = Path(self.data_dir) / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            target = export_dir / f"ai-mind-backup-{stamp}.json"
            text = json.dumps(bundle, ensure_ascii=False, indent=2)
            target.write_text(text, encoding="utf-8")
            size = int(target.stat().st_size)
        except Exception as exc:  # noqa: BLE001 - 写不进去要明说，别假装成功
            logger.warning(f"[ai_mind] 备份写盘失败：{exc}")
            return error_response(f"导出失败：文件写不到磁盘（{exc}）", status_code=500)
        counts = bundle["counts"]
        logger.info(
            f"[ai_mind] 导出备份 -> {target}"
            f"（{counts['memories']} 条记忆、{counts['settings']} 项设置，{size} 字节）"
        )
        return json_response({
            "ok": True,
            "path": str(target),
            "file": target.name,
            "bytes": size,
            "counts": counts,
            # 整份内容一起回给面板：目标机器可能根本没法拷文件（别人的服务器），
            # 面板要靠它开两条**免拷贝**通道 ——「下载到本地」和「复制粘贴」。
            "bundle_text": text,
        })

    async def _api_transfer_import(self) -> Any:
        """导入备份：可以贴整份 JSON（bundle），也可以给 exports/ 下的文件名（path）。

        导完**自己核对一遍**（设置逐项读回、记忆逐条比对），核对不过就明说少在哪 ——
        「搬迁要一次成功」靠的就是这一步，不能只回一句「完成」。
        """
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = dict(params)

        mode = str(payload.get("mode") or "merge").strip().lower()
        replace = mode == "replace"
        if replace and not payload.get("confirm"):
            return error_response(
                "「先清空再导入」不可撤销：要确认就带上 confirm=true（面板上会让你再点一次）"
            )

        bundle: Any = payload.get("bundle")
        if not isinstance(bundle, dict):
            raw_path = str(payload.get("path") or "").strip()
            if raw_path:
                try:
                    bundle = self._load_backup_file(raw_path)
                except ValueError as exc:
                    return error_response(str(exc))
            elif payload.get("memories") is not None or payload.get("settings") is not None:
                bundle = payload  # 面板把整份备份直接 POST 过来
            else:
                return error_response(
                    "没拿到备份：要么贴整份 JSON（bundle），要么给 exports/ 下的文件名（path）"
                )
        if str(bundle.get("kind") or "") != self.BACKUP_KIND:
            return error_response(
                f"这不是本插件的备份文件（kind 要是 {self.BACKUP_KIND}）"
            )

        before_memories = len(self._existing_memory_keys())
        live_sessions = self._live_sessions()
        try:
            settings_result = self._import_settings(bundle.get("settings"))
        except RuntimeError as exc:
            return error_response(str(exc), status_code=500)
        memories_result = self._import_memories(
            bundle.get("memories"), replace=replace, live_sessions=live_sessions
        )

        verify_ok = (
            not memories_result["missing"]
            and not settings_result["not_saved"]
            and not memories_result["failed"]
        )
        logger.info(
            f"[ai_mind] 导入备份：设置 {settings_result['applied']} 项"
            f"（跳过 {len(settings_result['skipped'])}），"
            f"记忆 +{memories_result['added']} / 重复 {memories_result['duplicates']}"
            f" / 无效 {memories_result['invalid']}（{before_memories} → {memories_result['total']}），"
            f"核对{'通过' if verify_ok else '没通过'}"
        )
        return json_response({
            "ok": verify_ok,
            "mode": "replace" if replace else "merge",
            "settings": settings_result,
            "memories": memories_result,
            "verify": {
                "ok": verify_ok,
                "expected": memories_result["expected"],
                "found": memories_result["expected"] - memories_result["missing"],
                "missing": memories_result["missing"],
                "missing_sample": memories_result["missing_sample"],
                "settings_not_saved": [
                    item["path"] for item in settings_result["not_saved"]
                ],
            },
            "counts": bundle.get("counts") if isinstance(bundle.get("counts"), dict) else {},
        })

    # ------------------------------------------------------------------
    # 让她自己会回忆、会记事（LLM 工具）
    # ------------------------------------------------------------------
    @llm_tool(name="recall_long_term_memory")
    async def tool_recall(self, event: AstrMessageEvent, query: str = "") -> str:
        """回忆关于对方的长期记忆。想不起他之前说过什么、要确认某个细节时调用。

        Args:
            query(string): 想回忆的关键词或话题；留空就返回最重要和最常被想起的几条
        """
        if not self.settings.memory.enabled:
            return "记忆功能没开。"
        session_id = event.unified_msg_origin
        sender_id = str(event.get_sender_id() or "")
        try:
            if query:
                found = self._search(str(query).strip(), session_id, sender_id)
                if found:
                    return "想起来了：" + chr(10) + chr(10).join(
                        "- " + item.content for item in found[:8]
                    )
            result = self.retriever.retrieve(
                self.store, query=str(query or ""),
                session_id=session_id, sender_id=sender_id,
            )
            found_memories = result.memories()
            if not found_memories:
                return "关于他，我这儿什么都没记着。"
            self.store.touch(result.ids())
            return "我记得这些：" + chr(10) + chr(10).join(
                "- " + item.content for item in found_memories[:8]
            )
        except Exception as exc:  # noqa: BLE001 - 工具报错不能把对话搞崩
            logger.warning(f"[ai_mind] 回忆工具失败：{exc}")
            return "想不起来了（内部出错了）。"

    @llm_tool(name="memorize_long_term_memory")
    async def tool_memorize(
        self, event: AstrMessageEvent, content: str, kind: str = "fact"
    ) -> str:
        """把值得长期记住的信息记下来。对方说了重要的事、喜好、约定、身体情况时调用。

        Args:
            content(string): 要记住的内容，一句话写清楚
            kind(string): 类型：fact 事实 / preference 喜好 / event 经历 / promise 约定 / relation 关系 / other
        """
        if not self.settings.memory.enabled:
            return "记忆功能没开，没记。"
        body = str(content or "").strip()
        if len(body) < 2:
            return "内容太短，没记。"
        sender_id = str(event.get_sender_id() or "")
        session_id = event.unified_msg_origin
        try:
            group = bool(is_group_session(session_id))
        except Exception:  # noqa: BLE001
            group = True
        kind_value = str(kind or "fact").strip().lower()
        if kind_value not in set(KINDS):
            kind_value = "fact"
        try:
            sensitive = tuple(detect_sensitive(body))
        except Exception:  # noqa: BLE001
            sensitive = ()
        try:
            scope = decide_scope(
                sensitive=bool(sensitive), session_id=session_id, owner_id=sender_id,
                group=group, group_scope="session", private_scope="user",
            )
        except TypeError:
            scope = "session" if group else "user"
        memory = Memory(
            content=body[:300],
            keywords=tuple(derive_keywords(body)),
            kind=kind_value,
            scope=scope,
            session_id="" if scope != "session" else session_id,
            owner_id="" if scope == "global" else sender_id,
            importance=0.75,
            sensitive=bool(sensitive),
            sensitive_reasons=sensitive,
            source="manual",
        )
        try:
            memory_id = self.store.add(memory)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 记事工具失败：{exc}")
            return "没记住（内部出错了）。"
        if not memory_id:
            return "这条已经记过了。"
        logger.info(f"[ai_mind] 她自己记下一条（{kind_value}）：{body[:32]}")
        return "记住了。"






    # ------------------------------------------------------------------
    # 鉴权：谁能用管理员指令
    # ------------------------------------------------------------------
    def _identity_of(self, event: AstrMessageEvent) -> tuple[str, str, str]:
        """(用户 ID, 平台类型, 平台实例 ID)。"""
        uid = str(event.get_sender_id() or "")
        platform = platform_id = ""
        try:
            platform = str(event.get_platform_name() or "")
        except Exception:  # noqa: BLE001
            platform = ""
        try:
            platform_id = str(event.get_platform_id() or "")
        except Exception:  # noqa: BLE001
            platform_id = ""
        return uid, platform, platform_id

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1001)
    async def on_debug_code(self, event: AstrMessageEvent) -> None:
        """隐藏调试开关 + 调试命令，只作用于发口令的那一个会话。

        优先级比消息闸门还高，免得口令被当成可疑消息拦掉。
        刻意不做进面板、也不在任何接口里暴露 —— 面板是给人看的，
        这种能绕过人格的后门不该出现在上面被误触。
        """
        try:
            text = (event.message_str or "").strip()
        except Exception:  # noqa: BLE001
            return
        if not text:
            return
        # 早退：这个钩子对**每一条消息**都会跑，先按首字符筛掉绝大多数，
        # 别为了一个口令把每条群消息都拖进来做比较。
        if text[:1] != "#" and text != DEBUG_CODE:
            return
        try:
            # 口令对但人不对：装作没看见，别露痕迹
            if str(event.get_sender_id() or "") != DEBUG_UID:
                return
        except Exception:  # noqa: BLE001
            return
        session_key = event.unified_msg_origin
        if text == DEBUG_CODE:
            on = self.debug.toggle(session_key)
            logger.info(f"[ai_mind] 调试模式{'开启' if on else '关闭'}：{session_key}")
            await self._debug_reply(event, DEBUG_ON_TEXT if on else DEBUG_OFF_TEXT)
            return
        # 口令之外的 # 命令只在已经开了调试模式的会话里认
        if not self.debug.is_active(session_key):
            return
        reply = await self._debug_command(event, text)
        await self._debug_reply(event, reply)

    async def _debug_reply(self, event: AstrMessageEvent, text: str) -> None:
        session_key = event.unified_msg_origin
        try:
            await self.context.send_message(session_key, [Comp.Plain(str(text))])
        except Exception:  # noqa: BLE001
            try:
                await event.send(event.plain_result(str(text)))
            except Exception:  # noqa: BLE001
                pass
        event.stop_event()          # 调试口令 / 命令本身绝不该发给模型

    async def _debug_command(self, event: AstrMessageEvent, text: str) -> str:
        word = text.lstrip("#").strip().lower()
        key = event.unified_msg_origin
        if word in ("帮助", "help", "h", "?", "？"):
            return DEBUG_HELP
        try:
            if word in ("状态", "status", "st"):
                return await self._debug_status(event, key)
            if word in ("曲线", "curve"):
                return self._debug_curve(key)
            if word in ("记忆", "memory", "mem"):
                return self._debug_memory()
            if word in ("抽取", "extract"):
                return await self._debug_extract()
        except Exception as exc:  # noqa: BLE001 - 调试命令出错要说出来，不能静默
            return f"调试命令出错：{type(exc).__name__}: {exc}"
        return "不认识的调试命令。发 #帮助 看有哪些。"

    async def _debug_status(self, event: AstrMessageEvent, key: str) -> str:
        out = [
            "【会话】" + key,
            "调试模式：开（再发 " + DEBUG_CODE + " 退出）",
            "情绪 " + ("开" if self.settings.emotion.enabled else "关")
            + "｜记忆 " + ("开" if self.settings.memory.enabled else "关"),
        ]
        session = self.engine.sessions.get(key)
        if session is None:
            out.append("情绪状态：还没建（在这个会话说一句就会建）")
        else:
            pad = session.emotion
            out.append("情绪 P/A/D：%+.2f / %+.2f / %+.2f" % (pad.p, pad.a, pad.d))
            relation = session.users.get(str(event.get_sender_id() or ""))
            if relation is not None:
                out.append("聊过 %d 条" % relation.msg_count)
        try:
            check = self.samples.selfcheck()
            out.append(
                "曲线：本会话 %d 点 / 全库 %d 点｜数据库%s"
                % (self.samples.count(key), check.get("rows", 0),
                   "可写" if check.get("write")
                   else ("写不进去 —— " + str(check.get("error") or "")))
            )
        except Exception as exc:  # noqa: BLE001
            out.append("曲线自检失败：" + str(exc))
        out.append(
            "待抽取：本会话 %d 轮 / 全部 %d 轮"
            % (len(self._buffers.get(key) or []),
               sum(len(v) for v in self._buffers.values()))
        )
        return "\n".join(out)

    def _debug_curve(self, key: str) -> str:
        check = self.samples.selfcheck()
        last = self.samples.latest(key)
        out = [
            "库文件：" + str(check.get("path") or "?")
            + ("（存在，%d 字节）" % int(check.get("size") or 0)
               if check.get("exists") else "（不存在）"),
            "写入自检：" + ("通过" if check.get("write")
                            else ("失败 —— " + str(check.get("error") or ""))),
            "采样点：本会话 %d / 全库 %d" % (self.samples.count(key), check.get("rows", 0)),
        ]
        if last is None:
            out.append("最近一点：无")
        else:
            pad = last.pad()
            out.append(
                "最近一点：%s（%.0f 分钟前）P/A/D %+.2f/%+.2f/%+.2f"
                % (time.strftime("%m-%d %H:%M", time.localtime(last.t)),
                   max(0.0, (time.time() - last.t) / 60.0), pad.p, pad.a, pad.d)
            )
        if self._sample_broken:
            out.append("注意：采样已被标记为不可用（面板上会出红条）")
        return "\n".join(out)

    def _debug_memory(self) -> str:
        rows = self.store.list_memories(limit=5)
        out = ["记忆共 %d 条，最近 5 条：" % int(self.store.stats().get("total", 0))]
        for item in rows:
            mark = "🔒" if item.sensitive else ("📌" if item.pinned else "·")
            out.append("%s [%s] %s" % (mark, item.scope, (item.content or "")[:60]))
        if not rows:
            out.append("（还没有记忆）")
        out.append("待抽取 %d 轮" % sum(len(v) for v in self._buffers.values()))
        return "\n".join(out)

    async def _debug_extract(self) -> str:
        if not self.settings.memory.enabled:
            return "记忆功能关着，抽不了。"
        pending = sum(len(v) for v in self._buffers.values())
        if not pending:
            return ("没有待抽取的对话 —— 调试模式里这个会话不记，"
                    "换个普通会话聊两句再来。")
        await self.flush_memories(force=True)
        return ("已把 %d 轮排队抽取（要调一次模型，几秒后看日志里的"
                "「记忆抽取：新增 N / 合并 N / 跳过 N」）。" % pending)

    def _debug_tools(self) -> str:
        return "\n".join([
            "工具闸门已经随护栏一起去掉了 —— 现在插件不再摘任何工具。",
            "想看模型实际拿到哪些工具：打开 advanced.debug_log，",
            "每轮日志里会有一行「本轮模型可用工具 N 个：…」。",
        ])


















    # ------------------------------------------------------------------
    # 提示词：注入给模型的原话，面板上直接改
    # ------------------------------------------------------------------








    # ------------------------------------------------------------------
    # 全部功能设置：把配置直接搬到面板上改
    # ------------------------------------------------------------------
    def _settings_schema(self) -> dict[str, Any]:
        """读插件自带的 _conf_schema.json（缓存住，不用每次读盘）。"""
        if self._schema_cache is not None:
            return self._schema_cache
        path = Path(__file__).resolve().parent / "_conf_schema.json"
        try:
            self._schema_cache = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 读不到 _conf_schema.json，面板设置页只能显示空的：{exc}")
            self._schema_cache = {}
        return self._schema_cache

    def _settings_leaf(self, path: str, child: dict[str, Any]) -> dict[str, Any]:
        """一个叶子字段（能直接渲染成控件的那些）。"""
        return {
            "path": path,
            "key": path.rsplit(".", 1)[-1],
            "label": str(child.get("description") or path),
            "hint": str(child.get("hint") or ""),
            "type": str(child.get("type") or "string").strip().lower(),
            "options": [str(item) for item in (child.get("options") or [])],
            "value": cfg_get(self.config, path, child.get("default")),
            "default": child.get("default"),
        }

    def _settings_fields(self, spec: Any, prefix: str = "") -> list[dict[str, Any]]:
        """把一个配置块摊平成字段列表（对象递归成 children）。"""
        items = spec.get("items") if isinstance(spec, dict) else None
        if not isinstance(items, dict):
            return []
        out: list[dict[str, Any]] = []
        for key, child in items.items():
            if not isinstance(child, dict):
                continue
            path = f"{prefix}.{key}" if prefix else str(key)
            if str(child.get("type") or "").strip().lower() == "object":
                children = self._settings_fields(child, path)
                if not children:
                    continue
                out.append({
                    "path": path,
                    "key": str(key),
                    "label": str(child.get("description") or key),
                    "hint": str(child.get("hint") or ""),
                    "type": "object",
                    "options": [],
                    "children": children,
                })
                continue
            out.append(self._settings_leaf(path, child))
        return out

    def _schema_leaves(self, schema: Any = None) -> dict[str, dict[str, Any]]:
        """{叶子配置路径: 它的 schema 节点}。

        导入备份时要按 type 逐项校验，光有路径不够，所以先有这个；
        路径集合由它派生，两边不会再各走一套遍历、慢慢对不上。
        """
        schema = self._settings_schema() if schema is None else schema
        out: dict[str, dict[str, Any]] = {}

        def walk(spec: Any, prefix: str) -> None:
            items = spec.get("items") if isinstance(spec, dict) else None
            if not isinstance(items, dict):
                return
            for key, child in items.items():
                if not isinstance(child, dict):
                    continue
                path = f"{prefix}.{key}" if prefix else str(key)
                if str(child.get("type") or "").lower() == "object" and isinstance(
                    child.get("items"), dict
                ):
                    walk(child, path)
                else:
                    out[path] = child

        # 顶层是「键 -> 配置块」的平面结构，没有包一层 object，这里补上
        walk({"items": schema}, "")
        return out

    def _settings_leaf_paths(self, schema: Any = None) -> set[str]:
        return set(self._schema_leaves(schema))

    def _set_config_value(self, path: str, value: Any) -> bool:
        """沿路径写一个配置值：**逐层取→改→写回父层**。

        只改最里层那个 dict 在有的配置对象上会白改（它把嵌套值复制一份再返回），
        表现就是「面板上改完、保存也提示成功，值却自己弹回」。
        这里从根到叶摆好整条链，再从内往外逐层塞回父节点，两种实现都写得进去。
        """
        parts = [part for part in str(path).split(".") if part]
        if not parts:
            return False
        nodes: list[Any] = [self.config]
        for part in parts[:-1]:
            child = nodes[-1].get(part) if hasattr(nodes[-1], "get") else None
            if not isinstance(child, dict):
                child = {}
            nodes.append(child)
        nodes[-1][parts[-1]] = value
        # 从最内层往外，逐层写回父节点（普通 dict 里是同一个对象，等于没动；
        # 副本式的配置对象则靠这一步把新值真正落进 self.config）
        for index in range(len(parts) - 2, -1, -1):
            nodes[index][parts[index]] = nodes[index + 1]
        return True

    @staticmethod
    def _value_matches(want: Any, got: Any) -> bool:
        """写进配置的值，和读回来的值算不算同一个？

        面板保存后要**逐项核对**：写不进去的项必须报出来，
        否则用户看到的就是「改完自己弹回」，还不知道是哪一项没存上。
        """
        if isinstance(want, bool) or isinstance(got, bool):
            return isinstance(want, bool) and isinstance(got, bool) and want == got
        if isinstance(want, (int, float)) and isinstance(got, (int, float)):
            return abs(float(want) - float(got)) < 1e-6
        if isinstance(want, (list, tuple)) or isinstance(got, (list, tuple)):
            if not isinstance(want, (list, tuple)) or not isinstance(got, (list, tuple)):
                return False
            return list(want) == list(got)
        if want is None or got is None:
            return want is None and got is None
        return str(want) == str(got)

    def _verify_saved(self, updates: dict[str, Any]) -> list[dict[str, Any]]:
        """把刚写进去的值逐项读回来，对不上的列成 not_saved。"""
        bad: list[dict[str, Any]] = []
        for saved_path, saved_value in updates.items():
            got = cfg_get(self.config, saved_path, None)
            if not self._value_matches(saved_value, got):
                bad.append({"path": saved_path, "wanted": saved_value, "got": got})
        if bad:
            logger.warning(
                "[ai_mind] 这些设置没能写进配置（面板会提示）："
                + "、".join(item["path"] for item in bad)
            )
        return bad

    def _settings_payload(self) -> dict[str, Any]:
        groups: list[dict[str, Any]] = []
        for key, spec in self._settings_schema().items():
            if not isinstance(spec, dict):
                continue
            key = str(key)
            if str(spec.get("type") or "").strip().lower() == "object":
                fields = self._settings_fields(spec, key)
            else:
                # 顶层也可能是单个开关（例如 enabled），它自己就是一项
                fields = [self._settings_leaf(key, spec)]
            if not fields:
                continue
            groups.append({
                "key": str(key),
                "label": str(spec.get("description") or key),
                "hint": str(spec.get("hint") or ""),
                "fields": fields,
            })
        save = getattr(self.config, "save_config", None)
        config_file = ""
        raw_path = str(getattr(self.config, "config_path", "") or "")
        if raw_path:
            config_file = os.path.basename(raw_path)
        return {
            "groups": groups,
            "writable": callable(save),
            "config_file": config_file,
            "saved_at": float(getattr(self, "_settings_saved_at", 0.0) or 0.0),
            "overrides": {
                "privacy": bool(self._privacy_data),
            },
        }

    async def _api_settings(self) -> Any:
        return json_response(self._settings_payload())

    def _schema_defaults(self, schema: Any = None) -> dict[str, Any]:
        """从 `_conf_schema.json` 收集每个叶子配置的默认值（= 插件出厂值）。"""
        schema = self._settings_schema() if schema is None else schema
        out: dict[str, Any] = {}

        def walk(spec: Any, prefix: str) -> None:
            items = spec.get("items") if isinstance(spec, dict) else None
            if not isinstance(items, dict):
                return
            for key, child in items.items():
                if not isinstance(child, dict):
                    continue
                path = f"{prefix}.{key}" if prefix else str(key)
                if str(child.get("type") or "").lower() == "object" and isinstance(
                    child.get("items"), dict
                ):
                    walk(child, path)
                elif "default" in child:
                    out[path] = child.get("default")

        walk({"items": schema}, "")
        return out

    async def _api_settings_reset(self) -> Any:
        """把所有配置项一次性还原成 `_conf_schema.json` 里的默认值。

        面板「恢复默认」按钮调它：逐个叶子写回默认值 → `save_config()` 落盘 →
        热更新（内存里的 Settings 立刻按默认值重建）→ 回完整的新 payload。
        **不碰** `privacy_overrides.json`（拒绝名单等面板覆盖项不在这份 schema 里）。
        """
        defaults = self._schema_defaults()
        if not defaults:
            return error_response("读不到 _conf_schema.json 的默认值，没法恢复")
        save = getattr(self.config, "save_config", None)
        if not callable(save):
            return error_response(
                "这个 AstrBot 版本的插件配置不支持回写，请到「配置」页恢复默认"
            )
        for path, value in defaults.items():
            self._set_config_value(path, value)
        try:
            save()
        except TypeError:
            try:
                save(dict(self.config))
            except Exception as exc:  # noqa: BLE001
                return error_response(f"写入配置文件失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            return error_response(f"写入配置文件失败：{exc}")
        self._settings_saved_at = time.time()
        try:
            self._reload_from_config()
        except Exception as exc:  # noqa: BLE001 - 落盘成功就算成功，热更新失败只告警
            logger.warning(f"[ai_mind] 恢复默认后热更新失败：{exc}")
        logger.info(f"[ai_mind] 面板把 {len(defaults)} 项配置恢复成默认值")
        result = self._settings_payload()
        result["reset"] = sorted(defaults)
        result["not_saved"] = self._verify_saved(defaults)
        return json_response(result)

    async def _api_settings_save(self) -> Any:
        params = await self._params()
        body = await self._read_body()
        payload: dict[str, Any] = body if isinstance(body, dict) else {}
        if not payload and isinstance(params, dict):
            payload = params
        values = payload.get("values")
        if not isinstance(values, dict) or not values:
            return error_response("没有收到任何要保存的设置")
        known = self._settings_leaf_paths()
        updates = {str(key): value for key, value in values.items() if str(key) in known}
        if not updates:
            return error_response("这些设置项不认识，可能是插件版本对不上")
        save = getattr(self.config, "save_config", None)
        if not callable(save):
            return error_response(
                "这个 AstrBot 版本的插件配置不支持回写，请到「配置」页改，或者手动编辑配置文件"
            )
        for path, value in updates.items():
            self._set_config_value(path, value)
        try:
            save()
        except TypeError:
            try:
                save(dict(self.config))
            except Exception as exc:  # noqa: BLE001
                return error_response(f"写入配置文件失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            return error_response(f"写入配置文件失败：{exc}")
        self._settings_saved_at = time.time()
        self._reload_from_config()
        logger.info(
            f"[ai_mind] 面板改了 {len(updates)} 项设置：{'、'.join(sorted(updates)[:8])}"
        )
        result = self._settings_payload()
        result["saved"] = sorted(updates)
        # 逐项读回：写不进去的（配置对象不认、被 schema 顶掉……）要说出来，
        # 不然面板上就是「改完自己弹回」，用户完全不知道哪一项没存上。
        result["not_saved"] = self._verify_saved(updates)
        return json_response(result)

    # ------------------------------------------------------------------
    # 管理：批量清理与维护
    # ------------------------------------------------------------------
    def _manage_stats(self) -> dict[str, Any]:
        try:
            memory = self.store.stats()
        except Exception:  # noqa: BLE001
            memory = {"total": 0, "pinned": 0, "sensitive": 0, "embedded": 0}
        db_bytes = 0
        try:
            path = self.data_dir / "mind.db"
            if path.is_file():
                db_bytes = int(path.stat().st_size)
        except Exception:  # noqa: BLE001
            db_bytes = 0
        try:
            sessions = int(self.engine.session_count)
        except Exception:  # noqa: BLE001
            sessions = 0
        return {
            "memory": memory,
            "sessions": sessions,
            "samples": int(self.samples.count()),
            "lexicon_hits": int(self.lexicons.count()),
            "db_bytes": db_bytes,
            "frozen": len(self._frozen),
        }

    async def _api_manage(self) -> Any:
        params = await self._params()
        session = str(params.get("session") or "").strip()
        if not session:
            session = self._session_or_latest(params)
        session_memories = 0
        if session:
            try:
                session_memories = self.store.count_for_session(session)
            except Exception:  # noqa: BLE001
                session_memories = 0
        return json_response({
            "stats": self._manage_stats(),
            "session": session,
            "session_memories": session_memories,
            "frozen": session in self._frozen,
            "kinds": list(KINDS),
            "kind_labels": dict(KIND_LABELS),
            "scopes": ["session", "user", "global"],
            "scope_labels": {"session": "本会话", "user": "本人", "global": "全局"},
        })

    async def _api_manage_memory(self) -> Any:
        params = await self._params()
        action = str(params.get("action") or "").strip().lower()
        session = str(params.get("session") or "").strip()
        removed = 0
        if action in {"clear_session", "清空会话"}:
            if not session:
                return error_response("没有指定要清理的会话")
            removed = self.store.clear(session_id=session)
        elif action in {"clear_all", "清空全部"}:
            removed = self.store.clear(everything=True)
        elif action in {"prune", "清理"}:
            rows, _total = self.store.query_page(
                kind=str(params.get("kind") or ""),
                scope=str(params.get("scope") or ""),
                pinned=params.get("pinned"),
                sensitive=params.get("sensitive"),
                limit=5000,
                offset=0,
            )
            try:
                floor = float(params.get("max_importance"))
            except (TypeError, ValueError):
                floor = None
            never_used = str(params.get("never_used") or "").lower() in {"1", "true", "yes", "on"}
            keep_pinned = str(params.get("keep_pinned") or "").lower() in {"1", "true", "yes", "on"}
            targets = []
            for memory in rows:
                if keep_pinned and memory.pinned:
                    continue
                if floor is not None and float(memory.importance or 0.0) > floor:
                    continue
                if never_used and int(memory.hit_count or 0) > 0:
                    continue
                targets.append(memory.id)
            removed = self.store.delete_many(targets) if targets else 0
        else:
            return error_response("不认识的操作：" + (action or "（空）"))
        logger.info(f"[ai_mind] 管理页执行 {action}，影响 {removed} 条记忆")
        return json_response({
            "ok": True, "action": action, "removed": removed, "stats": self._manage_stats()
        })

    async def _api_manage_data(self) -> Any:
        params = await self._params()
        action = str(params.get("action") or "").strip().lower()
        removed = 0
        if action in {"prune_samples", "压缩曲线"}:
            try:
                keep = int(float(params.get("keep") or 1500))
            except (TypeError, ValueError):
                keep = 1500
            keep = max(50, min(100000, keep))
            removed = self.samples.prune_all(keep)
        elif action in {"clear_lexicon", "清空词表命中"}:
            removed = self.lexicons.clear()
        elif action in {"vacuum", "整理数据库"}:
            conn = getattr(self.store, "connection", None)
            if conn is None:
                return error_response("数据库没打开")
            try:
                conn.execute("VACUUM")
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                return error_response(f"整理失败：{exc}")
        elif action in {"thaw_all", "解冻全部"}:
            removed = self.engine.unfreeze_all()
            return json_response({
                "ok": True, "action": action, "removed": removed,
                "sessions": removed, "stats": self._manage_stats(),
            })
        elif action in {"freeze_all", "全部冻结", "集体冻结"}:
            # 集体冻结：所有会话一起冻住（情绪与强度都不再动），
            # 每个会话在冻结点打一个采样，曲线上从这里开始走平。
            now = time.time()
            frozen = 0
            for key in list(self.engine.session_keys()):
                if not self.engine.freeze(key):
                    continue
                frozen += 1
                try:
                    session = self.engine.sessions.get(key)
                    if session is not None:
                        self._maybe_sample(key, session.emotion, "manual", now, force=True)
                except Exception as exc:  # noqa: BLE001 - 打点失败不挡冻结
                    logger.warning(f"[ai_mind] 集体冻结时打点失败：{exc}")
            self.engine.flush(now)
            removed = frozen
            return json_response({
                "ok": True, "action": action, "removed": removed,
                "frozen": frozen, "sessions": frozen, "stats": self._manage_stats(),
            })
        else:
            return error_response("不认识的操作：" + (action or "（空）"))
        logger.info(f"[ai_mind] 管理页执行 {action}，影响 {removed} 条")
        return json_response({
            "ok": True, "action": action, "removed": removed, "stats": self._manage_stats()
        })
    # ------------------------------------------------------------------
    # 配置热更新
    # ------------------------------------------------------------------
    def _reload_settings(self) -> None:
        self.settings = MindSettings.from_config(
            self.config, privacy=self._privacy_data,
        )
        self.engine.settings = self.settings.emotion

    def _reload_from_config(self) -> None:
        """面板改完配置之后，把运行中用到的东西重建一遍（内存里的情绪状态不动）。"""
        self.settings = MindSettings.from_config(
            self.config, privacy=self._privacy_data,
        )
        self.emotion_injection_mode = str(
            cfg_get(self.config, "emotion.injection.mode", "content_part") or "content_part"
        ).strip().lower()
        self.emotion_inject_rules = bool(cfg_get(self.config, "emotion.injection.inject_rules", True))
        self.emotion_trusted = bool(cfg_get(self.config, "emotion.injection.trusted", True))
        self.memory_injection_mode = str(
            cfg_get(self.config, "memory.injection.mode", "content_part") or "content_part"
        ).strip().lower()
        self.memory_inject_rules = bool(cfg_get(self.config, "memory.injection.inject_rules", True))
        self.embedding_timeout = float(
            cfg_get(self.config, "memory.embedding.timeout_seconds", 3) or 3
        )
        try:
            self.engine.apply_settings(self.settings.emotion)
            self.engine.set_custom_rules(cfg_get(self.config, "emotion.persona.custom_rules", []))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_mind] 热更新情绪设置失败：{exc}")

# ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    @filter.command("forgetme", alias={"忘我", "删除我的数据"})
    async def cmd_forget_me(self, event: AstrMessageEvent):
        """把你自己的数据全删掉（记忆、关系、情绪命中记录）。"""
        yield event.plain_result(await self._forget_me_text(event))

    async def _forget_me_text(self, event: AstrMessageEvent) -> str:
        if not self.settings.privacy.allow_self_delete:
            return "这个插件不允许自己删数据，找管理员。"
        uid = str(event.get_sender_id() or "")
        if not uid:
            return "认不出你是谁，删不了。"
        include_group = bool(self.settings.privacy.self_delete_group_memories)
        removed = self._forget_user(uid, include_group=include_group)
        total = sum(int(value) for value in removed.values())
        if not total:
            return "我这儿本来就没有你的东西。"
        return (
            "删干净了。"
            + chr(10)
            + "记忆 %d 条 · 关系 %d 处 · 情绪记录 %d 条"
            % (removed["memories"], removed["relations"], removed["lexicon_hits"])
        )

    @filter.command("privacy", alias={"隐私"})
    async def cmd_privacy(self, event: AstrMessageEvent):
        """看你自己留下了什么；管理员可以管拒绝名单。"""
        text = (event.message_str or "").strip()
        parts = [item for item in text.split() if item]
        uid = str(event.get_sender_id() or "")
        if len(parts) >= 3 and parts[1] in {"拒绝", "deny", "允许", "allow"}:
            if not self._is_admin(event):
                yield event.plain_result("这个是管理员的事。")
                return
            target = parts[2]
            add = parts[1] in {"拒绝", "deny"}
            current = list(self.settings.privacy.denied_users)
            if add and target not in current:
                current.append(target)
            if not add:
                current = [item for item in current if item != target]
            merged = dict(self._privacy_data)
            merged["denied_users"] = current
            self._privacy_data = merged
            if not self._save_simple_overrides("privacy_overrides.json", merged, "隐私"):
                yield event.plain_result("没写进去，看日志。")
                return
            self._reload_from_config()
            yield event.plain_result(
                ("已加入拒绝名单：" if add else "已移出拒绝名单：")
                + target
                + "（共 %d 人）" % len(current)
            )
            return
        summary = self._privacy_summary(uid)
        lines = [
            "你在我这儿留下的是：",
            "- 记忆 %d 条" % summary["memories"],
            "- 关系 %d 处" % summary["relations"],
            "- 情绪记录 %d 条" % summary["lexicon_hits"],
        ]
        if self._is_denied(uid):
            lines.append("你目前在拒绝名单里，我不会记你。")
        lines.append("想全删掉就发 /忘我。")
        yield event.plain_result(chr(10).join(lines))

    @filter.command("mood", alias={"情绪", "心情"})
    async def cmd_mood(self, event: AstrMessageEvent):
        """查看 / 管理情绪状态。"""
        if not self.settings.commands_enabled:
            yield event.plain_result("指令已被管理员关闭。")
            return
        try:
            text = self._mood_text(event)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ai_mind] 情绪指令出错：{exc}", exc_info=True)
            text = "情绪系统刚刚打了个结，稍后再试试吧。"
        yield event.plain_result(text)

    @filter.command("memory", alias={"记忆", "回忆"})
    async def cmd_memory(self, event: AstrMessageEvent):
        """查看 / 管理长期记忆。"""
        if not self.settings.commands_enabled:
            yield event.plain_result("指令已被管理员关闭。")
            return
        try:
            text = await self._memory_text(event)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ai_mind] 记忆指令出错：{exc}", exc_info=True)
            text = "记忆库刚刚卡了一下，稍后再试试。"
        yield event.plain_result(text)

    def _mood_text(self, event: AstrMessageEvent) -> str:
        now = time.time()
        session = self.engine.resolve(event.unified_msg_origin, now)
        relation = self.engine.relation_of(
            session, str(event.get_sender_id() or ""), event.get_sender_name() or "", now
        )
        snapshot = self.engine.snapshot(session, relation, now)
        tokens = (event.message_str or "").strip().split()
        args = tokens[1:] if len(tokens) > 1 else []
        sub = args[0].lower() if args else ""

        if sub in {"help", "帮助", "?", "？"}:
            return help_panel()
        if sub in {"history", "历史", "记录"}:
            return history_panel(snapshot)
        if sub in {"detail", "详细", "调试"}:
            return detail_panel(snapshot, session.key, self.engine.session_count)
        if sub in {"freeze", "冻结"}:
            if self.engine.freeze(session.key):
                # 冻结点打一个采样：曲线上从这里开始保持水平
                self._maybe_sample(session.key, session.emotion, "manual", now, force=True)
                self._schedule_flush()
            return (
                "🧊 已冻结：情绪与强度一起停住 —— 聊天不会改它，"
                "随时间的衰减也停了（面板上的手动设定仍然生效）。"
            )
        if sub in {"unfreeze", "解冻"}:
            self.engine.unfreeze(session.key)
            self._schedule_flush()
            return "🔥 已解冻：情绪与强度恢复随时间衰减，聊天也会重新影响它。"
        if sub in {"reset", "重置"}:
            if not self._is_admin(event):
                return "🚫 这个指令只有管理员能用。"
            self.engine.reset(session, now, keep_relations=True)
            self._maybe_sample(session.key, session.emotion, "manual", now, force=True)
            self._schedule_flush()
            return "🫧 好了，情绪和心境都归零了。"
        if sub in {"set", "设定", "设置"}:
            if not self._is_admin(event):
                return "🚫 这个指令只有管理员能用。"
            numbers = args[1:4]
            if len(numbers) < 3:
                return "用法：/情绪 设定 <愉悦> <唤醒> <掌控>，例如 /情绪 设定 0.6 -0.4 0.2"
            try:
                pad = PAD.of(float(numbers[0]), float(numbers[1]), float(numbers[2]))
            except (TypeError, ValueError):
                return "🚫 三个参数都要是 -1 ~ 1 之间的数字。"
            self.engine.set_emotion(session, pad, now)
            self._maybe_sample(session.key, session.emotion, "manual", now, force=True)
            self._schedule_flush()
            return f"✅ 已手动设定此刻情绪：{pad.pretty()}"
        if sub in {"intensity", "强度"}:
            if not self._is_admin(event):
                return "🚫 这个指令只有管理员能用。"
            if len(args) < 2:
                return "用法：/情绪 强度 <0~1>，例如 /情绪 强度 0.7（写 70 也认）"
            value = _parse_intensity(args[1])
            if value is None:
                return "🚫 强度要写成 0 ~ 1（或 0 ~ 100）。"
            applied = self.engine.set_intensity(session, value, now)
            self._maybe_sample(session.key, session.emotion, "manual", now, force=True)
            self._schedule_flush()
            level, level_name = level_of_intensity(applied)
            note = "（会话冻结中：强度不会自己衰减）" if session.key in self._frozen else ""
            return (
                f"✅ 强度已调到 {int(round(applied * 100))}%（{level}/5 {level_name}）"
                f"{note} —— 之后会按配置的衰减策略自己回落。"
            )
        return mood_panel(snapshot, self.settings.emotion)

    async def _memory_text(self, event: AstrMessageEvent) -> str:
        session_id = event.unified_msg_origin
        sender_id = str(event.get_sender_id() or "")
        tokens = (event.message_str or "").strip().split(maxsplit=2)
        sub = tokens[1].strip().lower() if len(tokens) > 1 else ""
        rest = tokens[2].strip() if len(tokens) > 2 else ""
        now = time.time()

        if sub in {"帮助", "help", "?", "？"}:
            return memory_help_panel()
        if sub in {"搜索", "search", "查", "找"}:
            if not rest:
                return "用法：/记忆 搜索 <关键词>"
            return search_panel(self._search(rest, session_id, sender_id), rest)
        if sub in {"记住", "remember", "记"}:
            if not rest:
                return "用法：/记忆 记住 <内容>"
            return self._manual_remember(rest, session_id, sender_id)
        if sub in {"忘记", "forget", "删", "删除"}:
            if not rest.isdigit():
                return "用法：/记忆 忘记 <编号>"
            return self._forget(int(rest), session_id, sender_id)
        if sub in {"清空", "clear", "重置"}:
            # 用配置页里那一个开关（commands.admin_only_mutations）——
            # schema 的说明写的就是它管「/记忆 清空」，别再用隐藏的第二个开关。
            if self.settings.admin_only_mutations and not self._is_admin(event):
                return "🚫 清空记忆只有管理员能用。"
            if rest in {"全部", "all", "所有"}:
                return clear_panel(self.store.clear(everything=True), "整个记忆库")
            removed = self.store.clear(session_id=session_id) + self.store.clear(owner_id=sender_id)
            return clear_panel(removed, "本会话与你相关")

        result = self.retriever.retrieve(
            self.store, query=event.message_str or "", session_id=session_id, sender_id=sender_id, now=now
        )
        return memory_main_panel(
            store=self.store, result=result, settings=self.settings.memory, session_id=session_id, now=now
        )

    def _search(self, keyword: str, session_id: str, sender_id: str) -> list[Memory]:
        return self.retriever.search(
            self.store, keyword=keyword, session_id=session_id, sender_id=sender_id
        )

    def _manual_remember(self, content: str, session_id: str, sender_id: str) -> str:
        content = content.strip()
        if len(content) < 2:
            return "内容太短了，写清楚一点吧。"
        reasons = detect_sensitive(content) if self.settings.memory.scan_sensitive else []
        sensitive = bool(reasons)
        scope = decide_scope(
            session_id=session_id,
            sensitive=sensitive,
            group_scope=self.settings.memory.group_scope,
            private_scope=self.settings.memory.private_scope,
        )
        now = time.time()
        memory = Memory(
            content=content,
            keywords=derive_keywords(content),
            scope=scope,
            session_id=session_id if scope != "global" else "",
            owner_id=sender_id if scope != "global" else "",
            importance=1.0,
            pinned=True,
            sensitive=sensitive,
            sensitive_reasons=tuple(reasons),
            source="manual",
            created_at=now,
            updated_at=now,
        )
        memory.id = self.store.add(memory)
        if not memory.id:
            return "写入记忆库失败了，看看日志吧。"
        return remember_panel(memory, sensitive)

    def _forget(self, memory_id: int, session_id: str, sender_id: str) -> str:
        memory = self.store.get(memory_id)
        if memory is None:
            return forget_panel(None, str(memory_id), False)
        if not self._can_manage(memory, session_id, sender_id):
            return "🚫 这条记忆不属于你，删不了。"
        return forget_panel(memory, str(memory_id), self.store.delete(memory_id))

    @staticmethod
    def _can_manage(memory: Memory, session_id: str, sender_id: str) -> bool:
        if memory.owner_id and str(memory.owner_id) == str(sender_id):
            return True
        if memory.session_id and memory.session_id == session_id:
            return True
        return False

    @staticmethod
    def _looks_like_command(text: str) -> bool:
        stripped = (text or "").strip()
        return bool(stripped) and stripped.startswith(COMMAND_PREFIXES)

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:  # noqa: BLE001
                pass
        try:
            return str(getattr(event, "role", "") or "") == "admin"
        except Exception:  # noqa: BLE001
            return False
