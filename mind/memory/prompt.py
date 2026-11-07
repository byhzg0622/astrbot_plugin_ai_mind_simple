"""把检索到的记忆渲染成注入给 LLM 的文本。

和情绪插件一样的两条铁律：
1. **不碰人设** —— 只提供事实，不规定她该怎么说话。
2. **缓存友好** —— 走 extra_user_content_parts，不动 system_prompt。

额外还有第三条，是这个插件特有的：
3. **不诱导编造** —— 明确写清"只用这里写明的信息，不确定就说不记得"，
   否则模型很容易顺着语境把记忆脑补成一段根本没发生过的往事。

第四条管的是**人称**（见 RULES_BLOCK）：
4. **只管指代，不管腔调** —— 「第一人称「我」/ 用名字称呼对方」是在纠**指代**，
   不是规定说话风格：记忆条目是第三人称档案体（「他喜欢冰美式」），直接念出来就成了
   「该用户喜欢冰美式」这种机器腔；而怎么称呼、什么语气，仍然听用户自己人设的。
"""

from __future__ import annotations

from typing import Any

from .model import KIND_LABELS, Memory, humanize_ago, truncate
from .privacy import describe_blocked, is_group_session
from .retriever import RetrievalResult

#: 用来判断这段规则说明是否已经注入过（避免重复）
RULES_MARKER = "<long_term_memory_rules>"

#: 恒定不变的行为准则 —— 追加到 system_prompt，内容固定所以不破坏 prompt 缓存
RULES_BLOCK = """<long_term_memory_rules>
你拥有关于对方的长期记忆，它们来自你们真实的聊天记录。
- 只使用记忆里写明的信息。记忆里没有提到的细节不要编，不确定就直接说不记得。
- **人称别搞混**：这些是**你自己**记下来的事 —— 说出口时用第一人称「我」
  （「我记得你说过…」「这个我一直记着」），不要用「该用户」「对方」「他/她」这种档案腔。
- **称呼对方用他的名字**（或你人设里对他的固定叫法）：条目里的「他/她/对方」指的都是
  正在和你说话的这个人，转述时把称呼换成他的名字，别叫他「用户」「那个人」。
- 不要把记忆念出来，也不要解释"我记得是因为…"，自然地用就行。
- 记忆里的事都发生在聊天里，你们并不在同一个物理空间。
- 记忆是背景，不是待办清单：大多数时候正常聊天就好，别每条都提。
- 记忆有优先级：刚发生的事比很久以前的事更值得被提起。
</long_term_memory_rules>"""

#: 群聊里额外的一句提醒
GROUP_NOTE = "（当前是群聊：标「私事」「私下知道」的不要主动说出来，除非他本人先提起。）"

#: 平台可能返回的、没有信息量的「名字」—— 有它们就不必占一行了
_EMPTY_NAMES = frozenset(
    {"", "他", "她", "它", "对方", "未知", "匿名", "unknown", "null", "none"}
)


def render_address_note(address: str) -> str:
    """把「这轮在跟谁聊」写成一行；没有像样的名字就返回空串。

    规则块里那句「用名字称呼对方」要落地，得先让模型知道**名字是什么** ——
    这一行就是把发消息人的名字递过去，同时把条目里的「他/她/对方」对上人。
    """
    parts = str(address or "").strip().splitlines()
    name = parts[0].strip() if parts else ""
    if len(name) > 24:
        name = name[:24] + "…"
    if name.lower() in _EMPTY_NAMES:
        return ""
    return f"（这轮在和「{name}」说话：下面条目里的「他/她/对方」都指他）"


#: 兼容旧名字
MARKER = RULES_MARKER


def _tag_for(memory: Memory) -> str:
    """给一条记忆打标签，顺序很重要：敏感优先于其它一切。"""
    if memory.sensitive:
        return "（私事，别说出去）"
    if memory.scope == "user":
        return "（私下知道）"
    if memory.kind in KIND_LABELS:
        return f"（{KIND_LABELS[memory.kind]}）"
    return ""


def render_memory_block(
    result: RetrievalResult,
    *,
    settings: Any,
    session_id: str,
    now: float | None = None,
    max_chars: int | None = None,
    address: str = "",
) -> str:
    """渲染这一轮的记忆条目；没有可用记忆时返回空串（不占用 token）。

    address = 这轮发消息的人的名字（平台拿得到就传）—— 规则块要求
    「用他的名字称呼他」，名字总得有人递给模型。
    """
    memories = result.memories()
    if not memories:
        return ""

    limit = settings.max_inject_chars if max_chars is None else max_chars
    group = is_group_session(session_id)

    lines: list[str] = ["<long_term_memory>"]
    note = render_address_note(address)
    if note:
        lines.append(note)
    if group:
        lines.append(GROUP_NOTE)

    def emit(title: str, items: list[Memory]) -> None:
        if not items:
            return
        lines.append(f"【{title}】")
        for memory in items:
            lines.append(f"- {memory.content}{_tag_for(memory)}")

    emit("一直记得的", result.pinned)
    emit("最近发生", result.recent)
    emit("可能和这轮有关", result.relevant)
    lines.append("</long_term_memory>")

    block = "\n".join(lines)
    if len(block) <= limit:
        return block

    # 超长就整条整条地丢，保证标签闭合、不出现半截内容
    tail = lines[-1]
    budget = max(0, limit - len(tail) - 2)
    kept: list[str] = [lines[0]]
    used = len(lines[0]) + 1
    # 头部说明（这轮在跟谁聊 / 群聊提醒）比条目短、也更要紧：先占位，
    # 剩下的预算再往里塞条目 —— 顺序不能反，否则小 limit 下会把条目全挤掉。
    heads = [line for line in (note, GROUP_NOTE if group else "") if line]
    for head in heads:
        if used + len(head) + 1 > budget:
            continue
        kept.append(head)
        used += len(head) + 1
    head_set = set(heads)
    for line in lines[1:]:
        if line == tail or line in head_set:
            continue
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    kept.append(tail)
    return "\n".join(kept)


def render_digest(memories: list[Memory], limit: int = 12, now: float | None = None) -> str:
    """调试/指令用的简短清单。"""
    if not memories:
        return "（空）"
    lines = []
    for memory in memories[:limit]:
        lines.append(f"#{memory.id} {truncate(memory.content, 50)}")
    return "\n".join(lines)


def render_blocked_hint(result: RetrievalResult) -> str:
    if not result.private_blocked:
        return ""
    return "（有私聊里知道的记忆，因为这里是群聊没有带进来）"


def describe_memory(memory: Memory, now: float | None = None) -> str:
    """一行式描述，指令面板用。"""
    bits = [f"#{memory.id}", describe_blocked(memory)]
    if memory.pinned:
        bits.append("常驻")
    bits.append(KIND_LABELS.get(memory.kind, memory.kind))
    return " · ".join(bits)


def describe_ago(memory: Memory, now: float | None = None) -> str:
    import time as _time

    now = _time.time() if now is None else now
    return humanize_ago(now - (memory.updated_at or memory.created_at or now))

