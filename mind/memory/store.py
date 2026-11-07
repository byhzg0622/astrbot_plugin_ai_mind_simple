"""长期记忆的持久化：SQLite。

为什么用 SQLite 而不是 JSON
---------------------------
记忆是**只增不减、需要按条件筛选、需要按时间排序**的数据。
用 JSON 意味着每次新增一条都要重写整个文件，几千条之后就不可接受了；
SQLite 是 Python 标准库自带，无需任何依赖，增删查改都是常数级。

并发
----
AstrBot 的事件循环是单线程的，但为了将来可能出现的
`asyncio.to_thread` 调用，连接用 `check_same_thread=False` 打开并加了锁。
所有操作都是毫秒级的短事务。
"""

from __future__ import annotations

import sqlite3
import threading
from array import array
from pathlib import Path
from typing import Any, Iterable

from .model import (
    SCOPE_GLOBAL,
    SCOPE_SESSION,
    SCOPE_USER,
    Memory,
    now_ts,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    content           TEXT    NOT NULL,
    keywords          TEXT    NOT NULL DEFAULT '',
    kind              TEXT    NOT NULL DEFAULT 'fact',
    subject           TEXT    NOT NULL DEFAULT '',
    scope             TEXT    NOT NULL DEFAULT 'session',
    session_id        TEXT    NOT NULL DEFAULT '',
    owner_id          TEXT    NOT NULL DEFAULT '',
    importance        REAL    NOT NULL DEFAULT 0.5,
    pinned            INTEGER NOT NULL DEFAULT 0,
    sensitive         INTEGER NOT NULL DEFAULT 0,
    sensitive_reasons TEXT    NOT NULL DEFAULT '',
    source            TEXT    NOT NULL DEFAULT 'auto',
    created_at        REAL    NOT NULL DEFAULT 0,
    updated_at        REAL    NOT NULL DEFAULT 0,
    last_hit          REAL    NOT NULL DEFAULT 0,
    hit_count         INTEGER NOT NULL DEFAULT 0,
    embedding         BLOB,
    embedding_model   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mem_scope   ON memories(scope, session_id, owner_id);
CREATE INDEX IF NOT EXISTS idx_mem_pinned  ON memories(pinned, importance);
CREATE INDEX IF NOT EXISTS idx_mem_updated ON memories(updated_at);
"""


def _pack_vector(vector: Iterable[float] | None) -> bytes | None:
    if not vector:
        return None
    try:
        return array("f", [float(x) for x in vector]).tobytes()
    except (TypeError, ValueError):
        return None


def _unpack_vector(blob: Any) -> list[float] | None:
    if not blob:
        return None
    try:
        raw = array("f")
        raw.frombytes(bytes(blob))
        return list(raw)
    except (TypeError, ValueError):
        return None


def _split_keywords(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, (list, tuple)):
        return tuple(str(x).strip() for x in raw if str(x).strip())
    if not raw:
        return ()
    parts = [p.strip() for p in str(raw).replace("，", ",").split(",")]
    return tuple(p for p in parts if p)


def row_to_memory(row: sqlite3.Row) -> Memory:
    return Memory(
        id=int(row["id"]),
        content=str(row["content"] or ""),
        keywords=_split_keywords(row["keywords"]),
        kind=str(row["kind"] or "fact"),
        subject=str(row["subject"] or ""),
        scope=str(row["scope"] or SCOPE_SESSION),
        session_id=str(row["session_id"] or ""),
        owner_id=str(row["owner_id"] or ""),
        importance=float(row["importance"] or 0.0),
        pinned=bool(row["pinned"]),
        sensitive=bool(row["sensitive"]),
        sensitive_reasons=_split_keywords(row["sensitive_reasons"]),
        source=str(row["source"] or "auto"),
        created_at=float(row["created_at"] or 0.0),
        updated_at=float(row["updated_at"] or 0.0),
        last_hit=float(row["last_hit"] or 0.0),
        hit_count=int(row["hit_count"] or 0),
        embedding=_unpack_vector(row["embedding"]),
        embedding_model=str(row["embedding_model"] or ""),
    )


def _coerce_ids(values: Iterable[Any]) -> list[int]:
    """把任意输入转成干净的 id 列表：非数字直接丢掉，0 也丢掉。"""
    ids: list[int] = []
    for item in values or []:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value and value not in ids:
            ids.append(value)
    return ids


class MemoryStore:
    """记忆仓库。所有异常都被吞掉并返回安全的空值，绝不让插件崩掉。"""

    def __init__(self, path: Path, logger: Any = None) -> None:
        self.path = Path(path)
        self.logger = logger
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._closed = False
        self._reopen_warned = False
        #: 最近一次打开失败的原因。面板要拿它说话 —— 只写日志的话，
        #: 用户看到的就是「曲线一片空白、记忆全没了」而不知道为什么。
        self.last_open_error = ""

    # -- 连接与建表 ---------------------------------------------------------
    @property
    def connection(self) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        if self._closed:
            # 已经显式关闭过了。但「关过」不等于「这个实例死了」：宿主重载
            # 插件时会先调 terminate()（我们这时把库关了），老实例却有可能
            # 还在继续收消息。要是就这么一直返回 None，记忆、曲线、词表会
            # 全部静默失效 —— 面板上还看不出原因，只能对着空白猜。
            # 所以这里重新打开，并在日志里留下线索。
            self._closed = False
            if not self._reopen_warned:
                self._reopen_warned = True
                if self.logger is not None:
                    self.logger.warning(
                        "[ai_memory] 记忆库关闭后又被使用（宿主重载插件时常见），已自动重新打开"
                    )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
            conn.row_factory = sqlite3.Row
            # WAL 在部分文件系统上开不起来（网络盘、某些同步目录、只读挂载）。
            # 开不起来不该让整个记忆库瘫掉 —— 退回默认日志模式接着用。
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error as exc:
                if self.logger is not None:
                    self.logger.warning(
                        f"[ai_memory] WAL 模式开不起来，退回默认日志模式：{exc}"
                    )
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
            self._conn = conn
            self.last_open_error = ""
        except (sqlite3.Error, OSError) as exc:
            # 目录不可写、磁盘满、文件损坏、路径非法 —— 都只降级，不让插件加载失败。
            # 但原因必须留下来：面板上要说清是哪一个。
            self.last_open_error = f"{type(exc).__name__}: {exc}"
            if self.logger is not None:
                self.logger.error(
                    f"[ai_memory] 打开记忆库失败（{self.path}）：{self.last_open_error}"
                )
            return None
        return self._conn

    def reset_connection(self) -> None:
        """把连接丢掉，下次访问重开一个。

        谁把库关了都行（宿主重载、老实例收尾、别处 close()）——
        上层发现「已关闭的数据库」时会调这里，而不是一路静默失败。
        """
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
            self._conn = None
            self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._conn is not None:
                try:
                    self._conn.commit()
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor | None:
        conn = self.connection
        if conn is None:
            return None
        with self._lock:
            try:
                cursor = conn.execute(sql, tuple(params))
                conn.commit()
                return cursor
            except (sqlite3.Error, OSError) as exc:
                if self.logger is not None:
                    self.logger.warning(f"[ai_memory] SQL 执行失败：{exc} | {sql[:80]}")
                return None

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        cursor = self._execute(sql, params)
        if cursor is None:
            return []
        try:
            return list(cursor.fetchall())
        except sqlite3.Error:
            return []

    # -- 写 -----------------------------------------------------------------
    def add(self, memory: Memory) -> int:
        row = memory.to_row()
        cursor = self._execute(
            """
            INSERT INTO memories (
                content, keywords, kind, subject, scope, session_id, owner_id,
                importance, pinned, sensitive, sensitive_reasons, source,
                created_at, updated_at, last_hit, hit_count, embedding, embedding_model
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["content"], row["keywords"], row["kind"], row["subject"], row["scope"],
                row["session_id"], row["owner_id"], row["importance"], row["pinned"],
                row["sensitive"], row["sensitive_reasons"], row["source"],
                row["created_at"], row["updated_at"], row["last_hit"], row["hit_count"],
                _pack_vector(memory.embedding), memory.embedding_model,
            ),
        )
        return int(cursor.lastrowid) if cursor is not None and cursor.lastrowid else 0

    def merge(self, memory_id: int, *, content: str, keywords: tuple[str, ...] = (),
              importance: float | None = None, now: float | None = None) -> bool:
        """把新信息合并进已有的一条记忆（同一件事被再次提到时用）。"""
        now = now_ts() if now is None else now
        fields = ["content = ?", "updated_at = ?"]
        params: list[Any] = [content, now]
        if keywords:
            fields.append("keywords = ?")
            params.append(",".join(keywords))
        if importance is not None:
            fields.append("importance = ?")
            params.append(max(0.0, min(1.0, importance)))
        params.append(memory_id)
        cursor = self._execute(f"UPDATE memories SET {', '.join(fields)} WHERE id = ?", params)
        return cursor is not None and cursor.rowcount > 0

    def set_pinned(self, memory_id: int, pinned: bool) -> bool:
        cursor = self._execute(
            "UPDATE memories SET pinned = ?, updated_at = ? WHERE id = ?",
            (1 if pinned else 0, now_ts(), memory_id),
        )
        return cursor is not None and cursor.rowcount > 0

    def touch(self, memory_ids: Iterable[int], now: float | None = None) -> None:
        ids = [int(i) for i in memory_ids if i]
        if not ids:
            return
        now = now_ts() if now is None else now
        placeholders = ",".join("?" for _ in ids)
        self._execute(
            f"UPDATE memories SET last_hit = ?, hit_count = hit_count + 1 WHERE id IN ({placeholders})",
            [now, *ids],
        )

    # -- 已删功能留下的多余表 -------------------------------------------------
    #: 这些表属于早就整块删掉的功能，新代码既不建表也不读写。
    #: 老版本留在磁盘上的，启动时由 `drop_legacy_tables()` 直接 DROP 掉。
    LEGACY_TABLES: tuple[str, ...] = (
        "memories_trash",   # 记忆垃圾桶（删除早已改成直接真删，这张表整个没用了）
        "style_examples",   # 表达示例
        "humanize_log",     # 去 AI 味体检记录
        "image_assets",     # 关键词配图
        "image_triggers",   # 关键词配图的触发词
    )
    #: 绝对不许碰的表：记忆、情绪曲线、词表命中是数据本体
    PROTECTED_TABLES: tuple[str, ...] = ("memories", "emotion_samples", "lexicon_hits")

    def drop_legacy_tables(self) -> list[str]:
        """删掉多余表，返回这一次真正删掉的名字（幂等，删过的不再出现）。

        只认 `LEGACY_TABLES` 这份白名单，且与 `PROTECTED_TABLES` 交叉核对 ——
        记忆（memories）和情绪曲线（emotion_samples）无论如何都不会被碰到。
        """
        conn = self.connection
        if conn is None:
            return []
        dropped: list[str] = []
        for name in self.LEGACY_TABLES:
            if name in self.PROTECTED_TABLES or not name.replace("_", "").isalnum():
                continue
            try:
                with self._lock:
                    row = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                        (name,),
                    ).fetchone()
                    if row is None:
                        continue
                    conn.execute(f"DROP TABLE IF EXISTS {name}")
                    conn.commit()
                dropped.append(name)
            except sqlite3.Error as exc:
                if self.logger is not None:
                    self.logger.warning(f"[ai_memory] 删掉多余表 {name} 失败：{exc}")
        return dropped

    def delete(self, memory_id: int) -> bool:
        cursor = self._execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return cursor is not None and cursor.rowcount > 0

    def clear(self, *, session_id: str | None = None, owner_id: str | None = None,
              everything: bool = False) -> int:
        if everything:
            cursor = self._execute("DELETE FROM memories")
        elif owner_id:
            cursor = self._execute("DELETE FROM memories WHERE owner_id = ?", (owner_id,))
        elif session_id:
            cursor = self._execute("DELETE FROM memories WHERE session_id = ?", (session_id,))
        else:
            return 0
        return int(cursor.rowcount) if cursor is not None else 0

    def set_embedding(self, memory_id: int, vector: list[float], model: str = "") -> bool:
        cursor = self._execute(
            "UPDATE memories SET embedding = ?, embedding_model = ? WHERE id = ?",
            (_pack_vector(vector), model, memory_id),
        )
        return cursor is not None and cursor.rowcount > 0

    # -- 读 -----------------------------------------------------------------
    def get(self, memory_id: int) -> Memory | None:
        rows = self._query("SELECT * FROM memories WHERE id = ?", (memory_id,))
        return row_to_memory(rows[0]) if rows else None

    def count(self) -> int:
        rows = self._query("SELECT COUNT(*) AS c FROM memories")
        return int(rows[0]["c"]) if rows else 0

    def stats(self) -> dict[str, int]:
        row = self._query(
            """
            SELECT COUNT(*) AS total,
                   SUM(pinned) AS pinned,
                   SUM(sensitive) AS sensitive,
                   SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded
            FROM memories
            """
        )
        if not row:
            return {"total": 0, "pinned": 0, "sensitive": 0, "embedded": 0}
        first = row[0]
        return {
            "total": int(first["total"] or 0),
            "pinned": int(first["pinned"] or 0),
            "sensitive": int(first["sensitive"] or 0),
            "embedded": int(first["embedded"] or 0),
        }

    def candidates(
        self,
        *,
        session_id: str,
        sender_id: str,
        limit: int = 1500,
    ) -> list[Memory]:
        """取出"有可能出现在这个会话里"的记忆（粗筛，精细判定交给 privacy.allows）。"""
        rows = self._query(
            """
            SELECT * FROM memories
            WHERE scope = ?
               OR (scope = ? AND session_id = ?)
               OR (scope = ? AND owner_id = ?)
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (SCOPE_GLOBAL, SCOPE_SESSION, session_id, SCOPE_USER, str(sender_id or ""), int(limit)),
        )
        return [row_to_memory(r) for r in rows]

    def list_memories(
        self,
        *,
        session_id: str | None = None,
        owner_id: str | None = None,
        pinned: bool | None = None,
        sensitive: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Memory]:
        where: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            where.append("(session_id = ? OR scope = ?)")
            params.extend([session_id, SCOPE_GLOBAL])
        if owner_id is not None:
            where.append("(owner_id = ? OR scope = ?)")
            params.extend([owner_id, SCOPE_GLOBAL])
        if pinned is not None:
            where.append("pinned = ?")
            params.append(1 if pinned else 0)
        if sensitive is not None:
            where.append("sensitive = ?")
            params.append(1 if sensitive else 0)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.extend([int(limit), int(offset)])
        rows = self._query(
            f"SELECT * FROM memories {clause} ORDER BY pinned DESC, updated_at DESC LIMIT ? OFFSET ?",
            params,
        )
        return [row_to_memory(r) for r in rows]

    def same_scope_snapshot(
        self,
        *,
        scope: str,
        session_id: str,
        owner_id: str,
        limit: int = 500,
    ) -> list[tuple[int, str]]:
        """同一作用域下已有的记忆正文，用于去重。"""
        if scope == SCOPE_SESSION:
            rows = self._query(
                "SELECT id, content FROM memories WHERE scope = ? AND session_id = ? LIMIT ?",
                (scope, session_id, int(limit)),
            )
        elif scope == SCOPE_USER:
            rows = self._query(
                "SELECT id, content FROM memories WHERE scope = ? AND owner_id = ? LIMIT ?",
                (scope, owner_id, int(limit)),
            )
        else:
            rows = self._query(
                "SELECT id, content FROM memories WHERE scope = ? LIMIT ?", (scope, int(limit))
            )
        return [(int(r["id"]), str(r["content"] or "")) for r in rows]

    def delete_many(self, memory_ids: Iterable[int]) -> int:
        """批量删除，返回实际删掉的条数。"""
        ids = _coerce_ids(memory_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cursor = self._execute(
            f"DELETE FROM memories WHERE id IN ({placeholders})", ids
        )
        return int(cursor.rowcount) if cursor is not None else 0

    def update_many(self, memory_ids: Iterable[int], **fields: Any) -> int:
        """批量改字段（常驻、作用域、重要度…），返回实际改动的条数。"""
        allowed = {"pinned", "scope", "importance", "kind", "session_id", "owner_id"}
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed or value is None:
                continue
            if key == "pinned":
                value = 1 if value else 0
            sets.append(f"{key} = ?")
            params.append(value)
        ids = _coerce_ids(memory_ids)
        if not sets or not ids:
            return 0
        sets.append("updated_at = ?")
        params.append(now_ts())
        placeholders = ",".join("?" for _ in ids)
        cursor = self._execute(
            f"UPDATE memories SET {', '.join(sets)} WHERE id IN ({placeholders})",
            [*params, *ids],
        )
        return int(cursor.rowcount) if cursor is not None else 0

    def query_page(
        self,
        *,
        q: str = "",
        scope: str = "",
        kind: str = "",
        pinned: Any = None,
        sensitive: Any = None,
        session_id: str = "",
        owner_id: str = "",
        source: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Memory], int]:
        """面板用的分页查询；返回 (结果, 命中总数)。

        ``session_id`` 有值时取「这个会话的记忆 + 通用记忆（session_id 为空）」。
        加通用那一半是刻意的：手动记的档案类内容不该因为绑了会话就在列表里
        消失，图谱那侧的过滤口径也和这里保持一致。
        """
        where: list[str] = []
        params: list[Any] = []
        if q:
            where.append("(content LIKE ? OR keywords LIKE ?)")
            needle = f"%{q}%"
            params.extend([needle, needle])
        if scope:
            where.append("scope = ?")
            params.append(scope)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if pinned is not None and pinned != "":
            where.append("pinned = ?")
            params.append(1 if pinned else 0)
        if sensitive is not None and sensitive != "":
            where.append("sensitive = ?")
            params.append(1 if sensitive else 0)
        if session_id:
            where.append("(session_id = ? OR session_id = '')")
            params.append(str(session_id))
        if owner_id:
            where.append("owner_id = ?")
            params.append(str(owner_id))
        if source:
            where.append("source = ?")
            params.append(str(source))
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        count_rows = self._query(f"SELECT COUNT(*) AS c FROM memories {clause}", params)
        total = int(count_rows[0]["c"]) if count_rows else 0

        rows = self._query(
            f"""SELECT * FROM memories {clause}
                ORDER BY pinned DESC, updated_at DESC LIMIT ? OFFSET ?""",
            [*params, int(limit), int(offset)],
        )
        return [row_to_memory(r) for r in rows], total

    def count_for_owner(self, owner_id: str) -> int:
        """某个人的记忆有多少条（隐私页和忘我都用）。"""
        rows = self._query(
            "SELECT COUNT(*) AS c FROM memories WHERE owner_id = ?", (str(owner_id),)
        )
        return int(rows[0]["c"]) if rows else 0

    def count_for_session(self, session_id: str) -> int:
        """这个会话里有多少条记忆（管理页用）。"""
        rows = self._query(
            "SELECT COUNT(*) AS c FROM memories WHERE session_id = ?", (str(session_id),)
        )
        return int(rows[0]["c"]) if rows else 0

    def distinct_sessions(self) -> list[str]:
        """记忆里出现过的会话 ID（面板要靠它把群也列出来）。"""
        rows = self._query(
            "SELECT DISTINCT session_id FROM memories WHERE session_id != ''"
        )
        return [str(row["session_id"]) for row in rows if row["session_id"]]
    def unembedded(self, *, model: str, limit: int = 32) -> list[Memory]:
        rows = self._query(
            """
            SELECT * FROM memories
            WHERE embedding IS NULL OR embedding_model != ?
            ORDER BY updated_at DESC LIMIT ?
            """,
            (model, int(limit)),
        )
        return [row_to_memory(r) for r in rows]

    def prune(self, *, max_memories: int, importance_floor: float) -> int:
        """超量时先砍掉"低重要度 + 从没被想起过"的记忆。"""
        total = self.count()
        if total <= max_memories:
            return 0
        overflow = total - max_memories
        cursor = self._execute(
            """
            DELETE FROM memories WHERE id IN (
                SELECT id FROM memories
                WHERE pinned = 0 AND sensitive = 0 AND importance < ?
                ORDER BY hit_count ASC, importance ASC, updated_at ASC
                LIMIT ?
            )
            """,
            (importance_floor, overflow),
        )
        removed = int(cursor.rowcount) if cursor is not None else 0
        if removed < overflow:
            cursor = self._execute(
                """
                DELETE FROM memories WHERE id IN (
                    SELECT id FROM memories WHERE pinned = 0
                    ORDER BY hit_count ASC, importance ASC, updated_at ASC
                    LIMIT ?
                )
                """,
                (overflow - removed,),
            )
            removed += int(cursor.rowcount) if cursor is not None else 0
        return removed

