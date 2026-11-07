"""检索：决定"这一轮该想起哪些事"。

三条来源互补，缺一不可
----------------------
1. **常驻档案**：名字、生日、称呼、长期爱好 —— 这些每轮都该在，
   但如果只靠它们，机器人就成了一个只会背档案的客服。
2. **最近发生**：他上周感冒了、明天要考试 —— 这些不需要"被检索到"，
   因为它们本来就该在短期上下文里，只是跨会话之后就丢了。
3. **可能相关**：靠词法（可选再加语义）从旧记忆里捞出来的。

词法检索用**字符二元组**而不是分词：中文不需要额外依赖，
对"他喜欢喝冰美式"这种短句效果已经够用，而且永不失败。
配了 Embedding Provider 时，语义分会叠加进来，两者加权求和。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .model import Memory, cosine_overlap, now_ts, score_memory, tokenize
from .privacy import allows, is_group_session


@dataclass
class RetrievalResult:
    """一次检索的结果。"""

    pinned: list[Memory] = field(default_factory=list)
    recent: list[Memory] = field(default_factory=list)
    relevant: list[Memory] = field(default_factory=list)
    scores: dict[int, float] = field(default_factory=dict)
    scanned: int = 0
    allowed: int = 0
    blocked: int = 0
    private_blocked: bool = False   # 本次是否有私聊记忆因为处在群聊被挡下

    def memories(self) -> list[Memory]:
        """按注入顺序返回全部记忆（已去重）。"""
        seen: set[int] = set()
        ordered: list[Memory] = []
        for bucket in (self.pinned, self.recent, self.relevant):
            for memory in bucket:
                key = memory.id or id(memory)
                if key in seen:
                    continue
                seen.add(key)
                ordered.append(memory)
        return ordered

    def ids(self) -> list[int]:
        return [m.id for m in self.memories() if m.id]

    def retain(self, keep: Sequence[Memory]) -> int:
        """只留下 keep 里的这几条（按对象身份比对），返回摘掉了几条。

        给「拒绝名单」过滤用：不该出现的人的记忆，一条都不能被注入。
        keep 传空列表就等于清空。
        """
        allowed = {id(item) for item in (keep or ())}
        removed = 0
        for name in ("pinned", "recent", "relevant"):
            bucket = getattr(self, name)
            fresh = [item for item in bucket if id(item) in allowed]
            removed += len(bucket) - len(fresh)
            setattr(self, name, fresh)
        return removed

    def __bool__(self) -> bool:
        return bool(self.pinned or self.recent or self.relevant)


class Retriever:
    """按配置的权重，从记忆库里挑出这一轮要用的记忆。"""

    def __init__(self, settings: Any, logger: Any = None) -> None:
        self.settings = settings
        self.logger = logger

    def retrieve(
        self,
        store: Any,
        *,
        query: str,
        session_id: str,
        sender_id: str,
        group: bool | None = None,
        query_vector: Sequence[float] | None = None,
        now: float | None = None,
    ) -> RetrievalResult:
        now = now_ts() if now is None else now
        settings = self.settings
        result = RetrievalResult()

        group = is_group_session(session_id) if group is None else group
        raw = store.candidates(session_id=session_id, sender_id=sender_id, limit=settings.scan_limit)
        result.scanned = len(raw)

        allowed: list[Memory] = []
        for memory in raw:
            if allows(
                memory,
                session_id=session_id,
                sender_id=sender_id,
                group=group,
                allow_private_in_group=settings.private_memory_in_group,
            ):
                allowed.append(memory)
            else:
                result.blocked += 1
                if group and memory.scope == "user" and str(memory.owner_id) == str(sender_id):
                    result.private_blocked = True
        result.allowed = len(allowed)

        if not allowed:
            return result

        # 1) 常驻档案：重要度优先
        pinned = [m for m in allowed if m.pinned]
        pinned.sort(key=lambda m: (-m.importance, -m.updated_at))
        result.pinned = pinned[: settings.pinned_limit]

        rest = [m for m in allowed if not m.pinned]

        # 2) 最近发生：时间优先，但只取时间窗内的 ——
        #    否则一条没什么关系的旧记忆会被永远塞进每一轮对话里。
        recent_pool = [
            m for m in rest if m.age_days(now) <= settings.recent_max_age_days
        ]
        recent = sorted(recent_pool, key=lambda m: -m.updated_at)[: settings.recent_limit]
        result.recent = recent

        # 3) 可能相关：打分
        if settings.relevant_limit > 0 and rest:
            taken = {m.id for m in recent}
            taken.update(m.id for m in result.pinned)
            query_tokens = tokenize(query) if query else set()
            scored: list[tuple[float, Memory]] = []
            for memory in rest:
                if memory.id in taken:
                    continue
                total, detail = score_memory(
                    memory,
                    query_tokens,
                    weights=settings.weights,
                    recency_half_life_days=settings.recency_half_life_days,
                    query_vector=list(query_vector) if query_vector else None,
                    query_text=query,
                    now=now,
                )
                # "可能和这轮有关"必须真的有关：
                # 若词法与语义都没有任何命中，这条就只剩"新鲜 + 重要"在撑分，
                # 那和前两栏重复，还会让过期的旧事一直阴魂不散。
                if detail["relevance"] <= 0.0 and detail["semantic"] <= 0.0:
                    continue
                if total >= settings.min_relevant_score:
                    scored.append((total, memory))
            scored.sort(key=lambda pair: (-pair[0], -pair[1].updated_at))
            chosen = scored[: settings.relevant_limit]
            result.relevant = [memory for _score, memory in chosen]
            result.scores = {memory.id: score for score, memory in chosen}

        return result

    def search(
        self,
        store: Any,
        *,
        keyword: str,
        session_id: str,
        sender_id: str,
        limit: int = 12,
    ) -> list[Memory]:
        """用户主动搜索：只在他有权看到的记忆里找。

        主动搜索优先用**子串命中**而不是相似度 —— 用户输入关键词时，
        期待的是"包含这个词的都列出来"，而不是"语义上最像的几条"。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        settings = self.settings
        group = is_group_session(session_id)
        needle = keyword.lower()
        tokens = tokenize(keyword)

        hits: list[tuple[int, float, Memory]] = []
        for memory in store.candidates(
            session_id=session_id, sender_id=sender_id, limit=settings.scan_limit
        ):
            if not allows(
                memory,
                session_id=session_id,
                sender_id=sender_id,
                group=group,
                allow_private_in_group=settings.private_memory_in_group,
            ):
                continue
            haystack = memory.searchable_text().lower()
            substring = 1 if needle in haystack else 0
            overlap = cosine_overlap(tokens, tokenize(haystack))
            if not substring and overlap < 0.12:
                continue
            hits.append((substring, overlap, memory))

        hits.sort(key=lambda item: (-item[0], -item[1], -item[2].updated_at))
        return [memory for _s, _o, memory in hits[:limit]]

