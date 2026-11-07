"""长期记忆的数据模型、文本工具与打分函数。

设计取舍
--------
1. **记忆分两类**：常驻档案（pinned，稳定事实，每轮都带上）与情景记忆（按相关度检索）。
   聊天机器人不需要"记住一切"，它需要"在对的时候想起对的事"。
2. **检索默认是纯本地的**：中文按字符二元组算余弦相似度，不依赖分词库、
   不额外请求任何模型、零延迟。配了 Embedding Provider 才会升级成语义检索。
3. 打分 = 词法相关度 + 时间新鲜度 + 重要度，三项都可配权重。
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# 记忆种类
# ---------------------------------------------------------------------------

KIND_FACT = "fact"            # 稳定事实：名字、生日、工作、身体情况
KIND_PREFERENCE = "preference"  # 喜好：爱吃什么、讨厌什么
KIND_EVENT = "event"          # 发生过的事：生病、考试、出差
KIND_PROMISE = "promise"      # 约定：答应过的事、计划
KIND_RELATION = "relation"    # 关系与称呼：怎么称呼、谁是谁
KIND_OTHER = "other"

KINDS: tuple[str, ...] = (
    KIND_FACT,
    KIND_PREFERENCE,
    KIND_EVENT,
    KIND_PROMISE,
    KIND_RELATION,
    KIND_OTHER,
)

KIND_LABELS: dict[str, str] = {
    KIND_FACT: "事实",
    KIND_PREFERENCE: "喜好",
    KIND_EVENT: "经历",
    KIND_PROMISE: "约定",
    KIND_RELATION: "关系",
    KIND_OTHER: "其他",
}

#: 常驻的记忆种类（默认会一直带上，除非被标成 sensitive 又处在群聊）
PINNABLE_KINDS: frozenset[str] = frozenset({KIND_FACT, KIND_PREFERENCE, KIND_RELATION, KIND_PROMISE})

# ---------------------------------------------------------------------------
# 作用域
# ---------------------------------------------------------------------------

SCOPE_SESSION = "session"  # 只在学到的那个会话里可用
SCOPE_USER = "user"        # 只在该用户出现时可用
SCOPE_GLOBAL = "global"    # 任何会话都可用

SCOPES: tuple[str, ...] = (SCOPE_SESSION, SCOPE_USER, SCOPE_GLOBAL)

SCOPE_LABELS: dict[str, str] = {
    SCOPE_SESSION: "本会话",
    SCOPE_USER: "该用户",
    SCOPE_GLOBAL: "通用",
}


def is_group_session(session_id: str) -> bool:
    """靠 unified_msg_origin 猜这个会话是不是群聊。

    形如 `aiocqhttp:GroupMessage:12345` / `telegram:GroupMessage:-100...`。
    猜不准时一律按"群聊"处理 —— 宁可少注入，也不要泄露。
    """
    if not session_id:
        return True
    lowered = session_id.lower()
    if "groupmessage" in lowered or "group_message" in lowered:
        return True
    if "friendmessage" in lowered or "friend_message" in lowered or "privatemessage" in lowered:
        return False
    # 平台标识里带 group / channel / guild / room 的也算群
    if any(token in lowered for token in ("group", "guild", "channel", "room")):
        return True
    # 完全认不出来的格式：按群聊处理。
    # 代价是这类会话里可能少想起一些私聊记忆；收益是永远不会把私事说漏嘴。
    return True


# ---------------------------------------------------------------------------
# 记忆本体
# ---------------------------------------------------------------------------


def now_ts() -> float:
    return time.time()


@dataclass
class Memory:
    """一条长期记忆。"""

    content: str = ""
    id: int = 0
    keywords: tuple[str, ...] = ()
    kind: str = KIND_FACT
    subject: str = ""
    scope: str = SCOPE_SESSION
    session_id: str = ""
    owner_id: str = ""
    importance: float = 0.5
    pinned: bool = False
    sensitive: bool = False
    sensitive_reasons: tuple[str, ...] = ()
    source: str = "auto"
    created_at: float = 0.0
    updated_at: float = 0.0
    last_hit: float = 0.0
    hit_count: int = 0
    embedding: list[float] | None = None
    embedding_model: str = ""

    # -- 打分用 -------------------------------------------------------------
    def searchable_text(self) -> str:
        """参与词法匹配的文本：正文 + 关键词 + 主体。"""
        parts = [self.content, " ".join(self.keywords), self.subject]
        return " ".join(p for p in parts if p)

    def age_days(self, now: float | None = None) -> float:
        now = now_ts() if now is None else now
        reference = self.updated_at or self.created_at or now
        return max(0.0, (now - reference) / 86400.0)

    def to_row(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "keywords": ",".join(self.keywords),
            "kind": self.kind,
            "subject": self.subject,
            "scope": self.scope,
            "session_id": self.session_id,
            "owner_id": self.owner_id,
            "importance": self.importance,
            "pinned": 1 if self.pinned else 0,
            "sensitive": 1 if self.sensitive else 0,
            "sensitive_reasons": ",".join(self.sensitive_reasons),
            "source": self.source,
            "created_at": self.created_at or now_ts(),
            "updated_at": self.updated_at or self.created_at or now_ts(),
            "last_hit": self.last_hit,
            "hit_count": self.hit_count,
        }


# ---------------------------------------------------------------------------
# 文本工具：中文按字符二元组，英文按单词
# ---------------------------------------------------------------------------

_NORMALIZE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[a-zA-Z0-9_]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

#: 这些字组成的二元组信息量太低，直接丢掉，减少噪声命中
STOP_CHARS: frozenset[str] = frozenset(
    "的了是我你他她它们在有和就不都一也很到说要去会着没看好自己这那么些什么吧呢啊呀哦嗯吗"
)


def normalize(text: str) -> str:
    """统一空白、全角转半角、转小写。"""
    if not text:
        return ""
    # 全角字符 -> 半角
    chars = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            chars.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - 0xFEE0))
        else:
            chars.append(ch)
    return _NORMALIZE_RE.sub(" ", "".join(chars)).strip().lower()


def tokenize(text: str) -> set[str]:
    """把文本切成用于相似度计算的词元集合。

    - 中文：相邻两字组成的二元组（不需要分词库，对中文短句够用）
    - 英文/数字：长度 >= 2 的单词
    """
    normalized = normalize(text)
    if not normalized:
        return set()

    tokens: set[str] = set()
    for match in _WORD_RE.finditer(normalized):
        tokens.add(match.group(0))

    cjk_only = "".join(ch for ch in normalized if _CJK_RE.match(ch))
    for index in range(len(cjk_only) - 1):
        gram = cjk_only[index : index + 2]
        if gram[0] in STOP_CHARS and gram[1] in STOP_CHARS:
            continue
        tokens.add(gram)
    # 单字中文也留一点信号（很短的查询整句可能只有一个字）
    if len(cjk_only) == 1:
        tokens.add(cjk_only)
    return tokens


def cosine_overlap(left: set[str], right: set[str]) -> float:
    """两个词元集合的余弦相似度（按集合大小归一，避免长文本占便宜）。"""
    if not left or not right:
        return 0.0
    inter = len(left & right)
    if inter == 0:
        return 0.0
    return inter / math.sqrt(len(left) * len(right))


def jaccard(left: set[str], right: set[str]) -> float:
    """词元集合的 Jaccard 相似度，用于判断"这两条记忆是不是同一件事"。"""
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


#: 自动抽关键词时丢掉的常见虚词组合（出现在两字组合里就整组丢掉）
KEYWORD_STOPWORDS: frozenset[str] = frozenset((
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "们", "这", "那",
    "什么", "怎么", "为什么", "一个", "一下", "可以", "没有", "不是", "就是",
    "还是", "但是", "因为", "所以", "如果", "已经", "自己", "时候", "现在", "真的",
))


def _cjk_terms(body: str, max_len: int = 6) -> list[tuple[str, int]]:
    """把中文切成「实义词块」的候选：(词, 起始位置)。

    纯标准库做不到真正的分词。与其硬切（「他喜欢喝冰美式」被切成
    「喜欢喝 + 冰美式」，甚至更糟的「主人今 + 天心情」），不如**不切**：

    - 用虚词（的/了/是/我/你/他…）当分隔，把句子断成实义词块；
    - 一个块就是一个候选 ——「冰美式」「牙医」「喜欢喝冰美式」各算一个；
    - 太长的块只取前 max_len 个字，免得关键词长到图上写不下。

    好处是**绝不会把一个词从中间切开**，而且同一段话每次结果完全一样，
    图谱不会自己变来变去。代价是长句会得到一个偏长的关键词 ——
    但它确实比「主人今」这种碎片可读。
    """
    out: list[tuple[str, int]] = []
    block_start = -1

    def emit(start: int, length: int) -> None:
        if length < 2:
            return
        piece = body[start : start + min(length, max_len)]
        if len(piece) >= 2:
            out.append((piece, start))

    for position, ch in enumerate(body):
        if _CJK_RE.match(ch) and ch not in STOP_CHARS:
            if block_start < 0:
                block_start = position
        else:
            if block_start >= 0:
                emit(block_start, position - block_start)
                block_start = -1
    if block_start >= 0:
        emit(block_start, len(body) - block_start)
    return out


def derive_keywords(text: str, limit: int = 6) -> tuple[str, ...]:
    """从一段话里自动抽关键词，用来在图谱上把记忆连成簇。

    抽出来的词同时也是 ``keyword_hit_score`` 认的那种词，
    所以「图谱上连成一片」和「检索时能被想起」用的是同一个口径。

    - 中文按虚词断块、从左到右切成 2~3 字的片段；
    - 英文/数字单词单独算一个词（长度 >= 2）；
    - 英文整词排在中文片段前面，重复出现的词排在前面；
    - 互相包含的词只留长的那个，免得图谱上挂一堆重复的灰色点。
    """
    body = normalize(text)
    if not body:
        return ()

    counts: dict[str, list[float]] = {}

    def bump(word: str, start: int, latin: bool) -> None:
        entry = counts.setdefault(word, [0.0, float(start), 0.0])
        entry[0] += 1.0
        if start < entry[1]:
            entry[1] = float(start)
        if latin:
            entry[2] = 1.0

    for match in _WORD_RE.finditer(body):
        word = match.group(0)
        if len(word) >= 2:
            bump(word, match.start(), True)

    for word, start in _cjk_terms(body):
        bump(word, start, False)

    if not counts:
        return ()

    # 英文整词优先，其次出现次数多的，再其次原文里靠前的；最后按字面兜底
    ranked = sorted(
        counts.items(),
        key=lambda kv: (-kv[1][2], -kv[1][0], kv[1][1], kv[0]),
    )

    picked: list[str] = []
    for word, _meta in ranked:
        if any(word in other or other in word for other in picked):
            continue
        picked.append(word)
        if len(picked) >= max(1, int(limit)):
            break
    return tuple(picked)


def cosine_vector(left: Iterable[float], right: Iterable[float]) -> float:
    """两组向量的余弦相似度；维度不一致或全零时返回 0。"""
    a = list(left)
    b = list(right)
    if not a or len(a) != len(b):
        return 0.0
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------


def recency_score(age_days: float, half_life_days: float) -> float:
    """越新越接近 1；每过 half_life_days 减半。"""
    if half_life_days <= 0:
        return 1.0
    return 0.5 ** (max(0.0, age_days) / half_life_days)


def keyword_hit_score(memory: Memory, query_text: str) -> float:
    """关键词命中度。

    中文短句换个说法以后，字符二元组几乎不重合
    （"我家那只猫" vs "他养了一只叫团子的猫" 一个二元组都对不上），
    这时 keywords 就是唯一的救命稻草 —— 也是它存在的意义。
    一次命中记 0.5，命中两次及以上记满分。
    """
    if not memory.keywords or not query_text:
        return 0.0
    normalized_query = normalize(query_text)
    if not normalized_query:
        return 0.0
    hits = 0
    for keyword in memory.keywords:
        token = normalize(keyword)
        if token and token in normalized_query:
            hits += 1
    if not hits:
        return 0.0
    return min(1.0, hits / 2.0)


@dataclass
class ScoreWeights:
    lexical: float = 0.6
    semantic: float = 0.0
    recency: float = 0.25
    importance: float = 0.15

    def renormalized(self) -> "ScoreWeights":
        total = self.lexical + self.semantic + self.recency + self.importance
        if total <= 0:
            return ScoreWeights(1.0, 0.0, 0.0, 0.0)
        return ScoreWeights(
            self.lexical / total,
            self.semantic / total,
            self.recency / total,
            self.importance / total,
        )


def score_memory(
    memory: Memory,
    query_tokens: set[str],
    *,
    weights: ScoreWeights,
    recency_half_life_days: float = 30.0,
    query_vector: list[float] | None = None,
    query_text: str = "",
    now: float | None = None,
) -> tuple[float, dict[str, float]]:
    """给一条记忆打分，返回 (总分, 明细)。"""
    lexical = cosine_overlap(query_tokens, tokenize(memory.searchable_text()))
    keyword = keyword_hit_score(memory, query_text)
    # 词法与关键词取较强者：关键词是"换个说法也能想起来"的兜底
    relevance = max(lexical, keyword)
    recency = recency_score(memory.age_days(now), recency_half_life_days)
    importance = max(0.0, min(1.0, memory.importance))

    semantic = 0.0
    if query_vector and memory.embedding:
        semantic = max(0.0, cosine_vector(query_vector, memory.embedding))

    w = weights
    total = (
        w.lexical * relevance
        + w.semantic * semantic
        + w.recency * recency
        + w.importance * importance
    )
    detail = {
        "lexical": lexical,
        "keyword": keyword,
        "relevance": relevance,
        "semantic": semantic,
        "recency": recency,
        "importance": importance,
    }
    return total, detail


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def humanize_ago(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"

