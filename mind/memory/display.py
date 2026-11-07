"""指令输出的面板。"""

from __future__ import annotations

import time as _time
from typing import Any, Sequence

from .model import KIND_LABELS, Memory, humanize_ago, truncate
from .privacy import describe_blocked
from .retriever import RetrievalResult


def _line(memory: Memory, *, now: float, show_score: float | None = None) -> str:
    bits = [f"#{memory.id}"]
    if memory.pinned:
        bits.append("📌")
    if memory.sensitive:
        bits.append("🔒")
    bits.append(KIND_LABELS.get(memory.kind, memory.kind))
    head = " · ".join(bits)
    ago = humanize_ago(now - (memory.updated_at or memory.created_at or now))
    score = f"　匹配 {show_score:.2f}" if show_score is not None else ""
    return f"  {head}　{ago}{score}\n      {truncate(memory.content, 70)}"


def main_panel(
    *,
    store: Any,
    result: RetrievalResult,
    settings: Any,
    session_id: str,
    now: float | None = None,
) -> str:
    now = _time.time() if now is None else now
    stats = store.stats()
    lines = [
        "🧠 长期记忆",
        "",
        f"  记忆库共 {stats['total']} 条 · 常驻 {stats['pinned']} · 敏感 {stats['sensitive']}",
    ]
    if settings.use_embedding:
        lines.append(f"  已向量化 {stats['embedded']}/{stats['total']} 条")
    lines.append(
        f"  扫描 {result.scanned} 条，本会话可用 {result.allowed} 条"
        + (f"，挡下 {result.blocked} 条" if result.blocked else "")
    )
    if result.private_blocked:
        lines.append("  （有私聊里知道的记忆，因为这里是群聊没有带进来）")

    def section(title: str, memories: Sequence[Memory], scores: dict[int, float] | None = None) -> None:
        if not memories:
            return
        lines.append("")
        lines.append(title)
        for memory in memories:
            lines.append(_line(memory, now=now, show_score=(scores or {}).get(memory.id)))

    section("📌 一直记得的", result.pinned)
    section("🕐 最近发生", result.recent)
    section("🔎 可能和这轮有关", result.relevant, result.scores)

    if not (result.pinned or result.recent or result.relevant):
        lines.extend(["", "  （还没有可用记忆。多聊几句，或者用 /记忆 记住 手动加一条）"])

    lines.extend(
        [
            "",
            "  可用：/记忆 搜索 <词>　/记忆 记住 <内容>　/记忆 忘记 <编号>",
        ]
    )
    return "\n".join(lines)


def search_panel(memories: Sequence[Memory], query: str, now: float | None = None) -> str:
    now = _time.time() if now is None else now
    if not memories:
        return f"🔍 没找到和「{query}」有关的记忆。"
    lines = [f"🔍 找到 {len(memories)} 条和「{query}」有关的记忆", ""]
    for memory in memories:
        lines.append(_line(memory, now=now))
        lines.append(f"      {describe_blocked(memory)}")
    return "\n".join(lines)


def forget_panel(memory: Memory | None, memory_id: str, ok: bool) -> str:
    if ok and memory is not None:
        return f"🗑️ 已经忘记 #{memory.id}：{truncate(memory.content, 50)}"
    return f"🤔 没找到编号为 {memory_id} 的记忆。"


def remember_panel(memory: Memory, sensitive: bool) -> str:
    lines = [f"📝 记住了 #{memory.id}：{truncate(memory.content, 60)}"]
    if sensitive:
        lines.append("🔒 这条被判定为私人信息，只会在他本人的私聊里出现，不会进群聊。")
    lines.append(f"   作用域：{describe_blocked(memory)}")
    return "\n".join(lines)


def clear_panel(removed: int, scope_label: str) -> str:
    if removed <= 0:
        return f"📭 {scope_label}里没有可清除的记忆。"
    return f"🧹 已清除 {scope_label}的 {removed} 条记忆。"


def help_panel(prefix: str = "/") -> str:
    return "\n".join(
        [
            "🧠 长期记忆 · 指令一览",
            "",
            f"  {prefix}记忆　　　　　　　 看看这一轮会带上哪些记忆",
            f"  {prefix}记忆 搜索 <关键词>  全文搜索记忆库",
            f"  {prefix}记忆 记住 <内容>　　手动加一条（默认常驻、仅你可见）",
            f"  {prefix}记忆 忘记 <编号>　　删掉一条",
            f"  {prefix}记忆 清空　　　　　【管理员】清空本会话留下的记忆",
            f"  {prefix}记忆 清空 全部　　 【管理员】清空整个记忆库",
            f"  {prefix}记忆 帮助　　　　　 显示这份说明",
            "",
            "  记忆由后台自动整理，也可以随时手动补。",
            "  标 🔒 的是私人信息，只在私聊里使用。",
        ]
    )


def provider_note(text: str) -> str:
    return f"⚠️ {text}"

