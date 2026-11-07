"""情绪曲线的采样存储。

为什么曲线数据不放在情绪状态的那个 JSON 里
------------------------------------------
情绪状态本身很小（当前值 + 心境 + 关系），但曲线是**时间序列**：
随着时间推移会无限增长。把它塞进 JSON 意味着每次保存都要重写整条曲线，
几千个点之后就不可接受了。

所以这里用 SQLite 单开一张表，只追加、可按时间区间查询、可单点修改。
合并之后记忆也用同一个数据库文件，省得开两个。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from .dbconn import ConnectionSource
from .emotion.model import PAD, now_ts

#: 代码指纹，启动时会打到日志里（换了新代码这里就该变）
STAMP = "samples/建表自愈+连接自愈+最后一招/3"

SAMPLE_KINDS = ("auto", "stimulus", "self", "idle", "manual")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS emotion_samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    t          REAL NOT NULL,
    p          REAL NOT NULL,
    a          REAL NOT NULL,
    d          REAL NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'auto'
);
CREATE INDEX IF NOT EXISTS idx_sample_session ON emotion_samples(session_id, t);
"""


@dataclass
class Sample:
    t: float
    p: float
    a: float
    d: float
    kind: str = "auto"
    id: int = 0

    def pad(self) -> PAD:
        return PAD(self.p, self.a, self.d)

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": round(self.t, 2),
            "p": round(self.p, 4),
            "a": round(self.a, 4),
            "d": round(self.d, 4),
            "kind": self.kind,
        }


class SampleStore(ConnectionSource):
    """情绪曲线采样点仓库。所有异常都降级为空结果，不让插件崩掉。"""

    _schema = _SCHEMA

    def __init__(self, connection: sqlite3.Connection | None, logger: Any = None) -> None:
        self.conn = connection
        self.logger = logger
        #: 最近一次写失败的原因。面板要拿它说话 —— 光说「采样没生效」
        #: 帮不上忙，得说清是 database is locked 还是表没建起来。
        self.last_error = ""
        self._isolated_warned = False
        # 建表交给 ConnectionSource：谁取连接谁顺手建，
        # 构造那一刻连不上的话也不会漏掉这张表。
        self.ensure_schema()

    # -- 基础 ---------------------------------------------------------------
    def _run(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor | None:
        # 连接被关掉时 execute 会自己重开一次再跑，这里只管收尾。
        if self.conn is None and not self.last_error:
            self.last_error = "曲线表不可用（数据库连不上）"
        try:
            return self.execute(sql, params)
        except (sqlite3.Error, OSError) as exc:
            # 现有连接、来源、备用连接全不行了 —— 自己单开一个把这条跑完。
            # 曲线不该因为连接对象的生命周期问题而永远是空的。
            cursor = self.run_isolated(sql, params)
            if cursor is not None:
                if not self._isolated_warned:
                    self._isolated_warned = True
                    if self.logger is not None:
                        self.logger.warning(
                            f"[ai_mind] 曲线连接已失效（{exc}），改用自带连接继续写"
                        )
                return cursor
            self.last_error = f"曲线 SQL 失败：{exc}"
            if self.logger is not None:
                self.logger.warning(f"[ai_mind] {self.last_error}")
            return None

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        cursor = self._run(sql, params)
        if cursor is None:
            return []
        try:
            return list(cursor.fetchall())
        except sqlite3.Error:
            return []

    # -- 写 -----------------------------------------------------------------
    def append(self, session_id: str, t: float, pad: PAD, kind: str = "auto") -> None:
        self._run(
            "INSERT INTO emotion_samples (session_id, t, p, a, d, kind) VALUES (?,?,?,?,?,?)",
            (str(session_id), float(t), float(pad.p), float(pad.a), float(pad.d), kind),
        )

    def replace_at(self, session_id: str, t: float, pad: PAD, *, tolerance: float = 1.0) -> int:
        """把某个时刻附近的采样点改成新的值（面板上拖点用）。返回改动条数。"""
        cursor = self._run(
            """
            UPDATE emotion_samples SET p = ?, a = ?, d = ?, kind = 'manual'
            WHERE session_id = ? AND t BETWEEN ? AND ?
            """,
            (float(pad.p), float(pad.a), float(pad.d), str(session_id), t - tolerance, t + tolerance),
        )
        return int(cursor.rowcount) if cursor is not None else 0

    def delete_at(self, session_id: str, t: float, *, tolerance: float = 1.0) -> int:
        cursor = self._run(
            "DELETE FROM emotion_samples WHERE session_id = ? AND t BETWEEN ? AND ?",
            (str(session_id), t - tolerance, t + tolerance),
        )
        return int(cursor.rowcount) if cursor is not None else 0

    def clear(self, session_id: str) -> int:
        cursor = self._run("DELETE FROM emotion_samples WHERE session_id = ?", (str(session_id),))
        return int(cursor.rowcount) if cursor is not None else 0

    def clear_all(self) -> int:
        cursor = self._run("DELETE FROM emotion_samples")
        return int(cursor.rowcount) if cursor is not None else 0

    def prune(self, session_id: str, max_points: int) -> int:
        """只保留最新的 max_points 个点。"""
        if max_points <= 0:
            return 0
        cursor = self._run(
            """
            DELETE FROM emotion_samples WHERE session_id = ? AND id NOT IN (
                SELECT id FROM emotion_samples WHERE session_id = ?
                ORDER BY t DESC LIMIT ?
            )
            """,
            (str(session_id), str(session_id), int(max_points)),
        )
        return int(cursor.rowcount) if cursor is not None else 0

    def prune_all(self, max_points: int) -> int:
        removed = 0
        for session_id in self.sessions():
            removed += self.prune(session_id, max_points)
        return removed

    # -- 读 -----------------------------------------------------------------
    def since(self, session_id: str, since: float, limit: int = 1500) -> list[Sample]:
        rows = self._all(
            """
            SELECT id, t, p, a, d, kind FROM emotion_samples
            WHERE session_id = ? AND t >= ?
            ORDER BY t ASC LIMIT ?
            """,
            (str(session_id), float(since), int(limit)),
        )
        return [
            Sample(
                id=int(r["id"]),
                t=float(r["t"]),
                p=float(r["p"]),
                a=float(r["a"]),
                d=float(r["d"]),
                kind=str(r["kind"] or "auto"),
            )
            for r in rows
        ]

    def latest(self, session_id: str) -> Sample | None:
        rows = self._all(
            "SELECT id, t, p, a, d, kind FROM emotion_samples WHERE session_id = ? ORDER BY t DESC LIMIT 1",
            (str(session_id),),
        )
        if not rows:
            return None
        r = rows[0]
        return Sample(
            id=int(r["id"]), t=float(r["t"]), p=float(r["p"]), a=float(r["a"]),
            d=float(r["d"]), kind=str(r["kind"] or "auto"),
        )

    def selfcheck(self) -> dict[str, Any]:
        """真插一条再删掉，验证「能不能写」。

        面板上必须能区分「压根没调用采样」和「调用了但写不进去」——
        这两种情况的修法完全不同。这里**绕过 _run** 直接抓原始异常：
        _run 会吞掉错误，而任何一次成功又会把错误清掉。
        """
        src = self._source
        path = str(getattr(src, "path", "") or "")
        info: dict[str, Any] = {
            "ok": False, "conn": self.conn is not None, "table": False,
            "write": False, "rows": 0, "error": "", "path": path,
            "exists": False, "size": 0,
        }
        try:
            info["exists"] = bool(path) and os.path.exists(path)
            info["size"] = os.path.getsize(path) if info["exists"] else 0
        except OSError:
            pass
        conn = self.conn
        if conn is None:
            info["error"] = (
                getattr(src, "last_open_error", "") or self.last_error
                or "数据库连接不可用"
            )
            return info
        probe = "__selfcheck__"
        err = ""
        try:
            conn.execute("DELETE FROM emotion_samples WHERE session_id = ?", (probe,))
            info["table"] = True
            conn.execute(
                "INSERT INTO emotion_samples (session_id, t, p, a, d, kind)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (probe, now_ts(), 0.0, 0.0, 0.0, "probe"),
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
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        if info["write"]:
            info["ok"] = True
            info["error"] = ""
        else:
            info["error"] = (
                err or self.last_error or self._schema_error
                or "插入没有生效（原因未知）"
            )
        info["rows"] = self.count()
        return info
    def count(self, session_id: str | None = None) -> int:
        if session_id is None:
            rows = self._all("SELECT COUNT(*) AS c FROM emotion_samples")
        else:
            rows = self._all(
                "SELECT COUNT(*) AS c FROM emotion_samples WHERE session_id = ?", (str(session_id),)
            )
        return int(rows[0]["c"]) if rows else 0

    def sessions(self) -> list[str]:
        rows = self._all("SELECT DISTINCT session_id FROM emotion_samples")
        # __selfcheck__ 是自检写的探针，别当成真会话列到面板上
        return [
            str(r["session_id"]) for r in rows
            if r["session_id"] and not str(r["session_id"]).startswith("__")
        ]


# ---------------------------------------------------------------------------
# 词表命中记录（隐私页的「情绪记录 N 条」、/忘我 的清理、管理页的清空都用它）
# ---------------------------------------------------------------------------
_LEXICON_SCHEMA = """
CREATE TABLE IF NOT EXISTS lexicon_hits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    t          REAL NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    uid        TEXT NOT NULL DEFAULT '',
    word       TEXT NOT NULL,
    code       TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    family     TEXT NOT NULL DEFAULT '',
    intensity  INTEGER NOT NULL DEFAULT 5,
    direction  TEXT NOT NULL DEFAULT 'mirror',
    negated    INTEGER NOT NULL DEFAULT 0,
    proto      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_lexicon_t ON lexicon_hits(t);
CREATE INDEX IF NOT EXISTS idx_lexicon_word ON lexicon_hits(word);
"""


@dataclass
class LexiconHit:
    """一次词表命中的落库记录。"""

    t: float
    word: str
    code: str
    name: str = ""
    family: str = ""
    intensity: int = 5
    direction: str = "mirror"
    negated: bool = False
    proto: str = ""
    session_id: str = ""
    uid: str = ""
    id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "t": round(self.t, 2),
            "word": self.word,
            "code": self.code,
            "name": self.name,
            "family": self.family,
            "intensity": self.intensity,
            "direction": self.direction,
            "negated": bool(self.negated),
            "proto": self.proto,
            "session": self.session_id,
        }


class LexiconStore(ConnectionSource):
    """词表命中记录。只追加，按时间和词聚合查询。"""

    _schema = _LEXICON_SCHEMA

    def __init__(self, connection: sqlite3.Connection | None, logger: Any = None) -> None:
        self.conn = connection
        self.logger = logger
        self.ensure_schema()

    def record(
        self,
        hits: Iterable[Any],
        *,
        session_id: str = "",
        uid: str = "",
        proto: str = "",
        now: float | None = None,
    ) -> int:
        """把一次评估里命中的词写下来。hits 是 lexicon.Hit 的序列。"""
        if self.conn is None:
            return 0
        rows = []
        stamp = float(now if now is not None else now_ts())
        for hit in hits or ():
            word = str(getattr(hit, "word", "") or "")
            if not word:
                continue
            rows.append(
                (
                    stamp,
                    str(session_id or ""),
                    str(uid or ""),
                    word,
                    str(getattr(hit, "code", "") or ""),
                    str(getattr(hit, "name", "") or ""),
                    str(getattr(hit, "family", "") or ""),
                    int(getattr(hit, "intensity", 5) or 5),
                    str(getattr(hit, "direction", "mirror") or "mirror"),
                    1 if getattr(hit, "negated", False) else 0,
                    str(proto or ""),
                )
            )
        if not rows:
            return 0
        try:
            self.conn.executemany(
                "INSERT INTO lexicon_hits (t, session_id, uid, word, code, name, family,"
                " intensity, direction, negated, proto) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self.conn.commit()
            return len(rows)
        except sqlite3.Error as exc:
            if self.logger is not None:
                self.logger.warning(f"[ai_mind] 记录词表命中失败：{exc}")
            return 0

    def recent(self, limit: int = 30) -> list[LexiconHit]:
        if self.conn is None:
            return []
        try:
            rows = self.conn.execute(
                "SELECT * FROM lexicon_hits ORDER BY t DESC, id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [self._row(row) for row in rows]

    def top_words(self, since: float = 0.0, limit: int = 24) -> list[dict[str, Any]]:
        if self.conn is None:
            return []
        try:
            rows = self.conn.execute(
                "SELECT word, code, name, COUNT(*) AS n, AVG(intensity) AS strength"
                " FROM lexicon_hits WHERE t >= ? GROUP BY word ORDER BY n DESC, word LIMIT ?",
                (float(since), max(1, int(limit))),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [
            {
                "word": str(row["word"]),
                "code": str(row["code"]),
                "name": str(row["name"]),
                "count": int(row["n"] or 0),
                "intensity": round(float(row["strength"] or 5.0), 1),
            }
            for row in rows
        ]

    def count(self, since: float = 0.0) -> int:
        if self.conn is None:
            return 0
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM lexicon_hits WHERE t >= ?", (float(since),)
            ).fetchone()
        except sqlite3.Error:
            return 0
        return int(row["n"] or 0) if row else 0

    def clear_by_uid(self, uid: str) -> int:
        """删掉某个人触发的词表命中记录。"""
        if self.conn is None or not str(uid or "").strip():
            return 0
        try:
            cursor = self.conn.execute("DELETE FROM lexicon_hits WHERE uid = ?", (str(uid),))
            self.conn.commit()
            return int(cursor.rowcount or 0)
        except sqlite3.Error:
            return 0

    def clear(self) -> int:
        if self.conn is None:
            return 0
        try:
            cursor = self.conn.execute("DELETE FROM lexicon_hits")
            self.conn.commit()
            return int(cursor.rowcount or 0)
        except sqlite3.Error:
            return 0

    @staticmethod
    def _row(row: sqlite3.Row) -> LexiconHit:
        return LexiconHit(
            id=int(row["id"]),
            t=float(row["t"]),
            session_id=str(row["session_id"] or ""),
            uid=str(row["uid"] or ""),
            word=str(row["word"]),
            code=str(row["code"]),
            name=str(row["name"] or ""),
            family=str(row["family"] or ""),
            intensity=int(row["intensity"] or 5),
            direction=str(row["direction"] or "mirror"),
            negated=bool(row["negated"]),
            proto=str(row["proto"] or ""),
        )


def should_sample(previous: PAD | None, current: PAD, epsilon: float, force: bool = False) -> bool:
    """变化太小就不采样 —— 否则心跳会把曲线填成一条噪声带。"""
    if force or previous is None:
        return True
    if epsilon <= 0:
        return True
    return (
        abs(current.p - previous.p) >= epsilon
        or abs(current.a - previous.a) >= epsilon
        or abs(current.d - previous.d) >= epsilon
    )


__all__ = ["SAMPLE_KINDS", "Sample", "SampleStore", "should_sample", "now_ts"]

