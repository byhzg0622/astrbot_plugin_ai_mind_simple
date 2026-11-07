"""把情绪状态渲染成用户能看的文字（指令输出）。"""

from __future__ import annotations

import time as _time

from .engine import Settings, Snapshot
from .moodlabel import MoodLabel
from .model import (
    PAD,
    Relation,
    clamp,
    humanize_seconds,
    intensity_of,
    intensity_percent,
)

FILL = "█"
EMPTY = "░"

TREND_TEXT = {"up": "↗ 在变好", "down": "↘ 在变差", "flat": "→ 基本稳定"}


def bipolar_bar(value: float, width: int = 10) -> str:
    """双极进度条：负值往左填，正值往右填。"""
    half = max(1, width // 2)
    value = clamp(value, -1.0, 1.0)
    filled = int(round(abs(value) * half))
    if value >= 0:
        return EMPTY * half + FILL * filled + EMPTY * (half - filled)
    return EMPTY * (half - filled) + FILL * filled + EMPTY * half


def unipolar_bar(value: float, maximum: float = 100.0, width: int = 10) -> str:
    """单极进度条：0 到 maximum。"""
    if maximum <= 0:
        return EMPTY * width
    ratio = clamp(value / maximum, 0.0, 1.0)
    filled = int(round(ratio * width))
    return FILL * filled + EMPTY * (width - filled)


def _cause_of(snapshot: Snapshot) -> str:
    """F1（R-A）：起因是 CauseInfo —— 文案 · 原话 · 时间一次给全。"""
    cause = snapshot.cause
    if not cause:
        return "没什么特别的"
    return cause.compact() or "没什么特别的"


def mood_panel(snapshot: Snapshot, settings: Settings) -> str:
    """主面板：/情绪 的输出。"""
    who = settings.bot_name or "她"
    emotion = snapshot.emotion
    label = getattr(snapshot, "mood_label", None) or MoodLabel()
    lines = [
        f"🎭 {who}此刻的心情",
        "",
        f"  {snapshot.proto.emoji} {snapshot.proto.name}",
        f"  强度  {bipolar_bar(snapshot.intensity * 2 - 1)}  "
        f"{intensity_percent(snapshot.intensity)}% · {snapshot.level}/5 {snapshot.level_name}",
        "",
        f"  愉悦  {bipolar_bar(emotion.p)}  {emotion.p:+.2f}",
        f"  唤醒  {bipolar_bar(emotion.a)}  {emotion.a:+.2f}",
        f"  掌控  {bipolar_bar(emotion.d)}  {emotion.d:+.2f}",
        "",
        f"  💭 起因　{_cause_of(snapshot)}",
        f"  🌤 心境　{label.emoji} {label.text} {TREND_TEXT.get(getattr(snapshot, 'mood_trend', 'flat'), '→ 基本稳定')}",
        f"　　　　　（{snapshot.mood.p:+.2f} / {snapshot.mood.a:+.2f} / "
        f"{snapshot.mood.d:+.2f}）　{label.reason}",
        f"　　　　　起因　{getattr(snapshot, 'mood_cause', '') or '—'}",
    ]

    if snapshot.idle_seconds >= 60:
        lines.append(f"     上次说话：{humanize_seconds(snapshot.idle_seconds)}")

    # 「✍️ 表达基调」已经删除：插件不再规定她该怎么说话。
    if snapshot.crisis:
        lines.extend(["", "  ⚠️ 当前处于安全优先状态：玩笑与敷衍已临时关闭"])
    return "\n".join(lines)


def history_panel(snapshot: Snapshot, limit: int = 8) -> str:
    """情绪史：/情绪 历史 的输出。"""
    events = list(snapshot.history)
    if not events:
        return "📭 还没有记录到任何情绪波动，她的心情一直很平。"

    now = snapshot.taken_at or _time.time()
    lines = ["📜 最近的情绪波动（由近到远）", ""]
    for event in reversed(events[-limit:]):
        delta = (event.delta[0] + event.delta[1] + event.delta[2]) / 3.0
        arrow = "↗" if delta >= 0 else "↘"
        source_tag = {"self": "她自己", "idle": "没人理她", "manual": "手动"}.get(event.source, "")
        suffix = f"（{source_tag}）" if source_tag else ""
        lines.append(
            f"  {humanize_seconds(max(0.0, now - event.t)):<10}"
            f"{event.emoji} {event.label:<4} {arrow}{abs(delta):.2f}  {event.cause}{suffix}"
        )
    return "\n".join(lines)


def help_panel(prefix: str = "/") -> str:
    return "\n".join(
        [
            "🎭 情绪系统 · 指令一览",
            "",
            f"  {prefix}情绪　　　　　　看看她此刻什么心情",
            f"  {prefix}情绪 历史　　　　最近的情绪波动记录",
            f"  {prefix}情绪 详细　　　　调试用：数值 / 会话 / 关系原始信息",
            f"  {prefix}情绪 重置　　　　【管理员】把情绪与心境归零",
            f"  {prefix}情绪 设定 p a d　【管理员】手动设定情绪（-1 ~ 1）",
            f"  {prefix}情绪 强度 0~1　　【管理员】手动调节情绪强度（可调、会自己衰减）",
            "",
            "  数值说明：愉悦(P) / 唤醒(A) / 掌控(D)，取值 -1 ~ 1；情绪强度 0 ~ 1（可调、会衰减）",
        ]
    )


def detail_panel(snapshot: Snapshot, session_key: str, session_count: int) -> str:
    """调试用：/情绪 详细 的输出。"""
    emotion: PAD = snapshot.emotion
    mood_label = getattr(snapshot, "mood_label", None) or MoodLabel()
    # 心境基线 = 气质基线 + 配置偏移；超基线触发了没有，这里得说清楚
    mood_rest = getattr(snapshot, "mood_baseline", None) or snapshot.baseline
    cold_since = float(getattr(snapshot, "mood_cold_since", 0.0) or 0.0)
    cold_state = (
        f"{humanize_seconds(max(0.0, snapshot.taken_at - cold_since))}开始，正往冷淡走"
        if cold_since > 0
        else "未触发（要心境在基线上待够时长、这段时间还没有对话）"
    )
    lines = [
        "🔬 调试详情",
        "",
        f"  会话　　　{session_key}",
        f"  已知会话　{session_count} 个",
        f"  即时情绪　{emotion.pretty()}　强度(PAD推导) {intensity_of(emotion):.3f}",
        f"  情绪强度　{snapshot.intensity:.3f}（{intensity_percent(snapshot.intensity)}% · "
        f"{snapshot.level}/5 {snapshot.level_name}）—— 独立状态：面板可调、会按配置衰减",
        f"  心境　　　{snapshot.mood.pretty()}",
        f"  心境标签　{mood_label.emoji} {mood_label.text}（{mood_label.key}，"
        f"偏移 {mood_label.magnitude:.2f}）",
        f"  心境依据　{mood_label.reason}",
        f"  心境趋势　{TREND_TEXT.get(getattr(snapshot, 'mood_trend', 'flat'), '→ 基本稳定')}",
        f"  心境起因　{getattr(snapshot, 'mood_cause', '') or '—'}",
        f"  气质基线　{snapshot.baseline.pretty()}",
        f"  心境基线　{mood_rest.pretty()}（气质基线 + 配置偏移）",
        f"  超基线　　{cold_state}",
        f"  情绪标签　{snapshot.proto.key}（{snapshot.proto.name}）",
        f"  几何最近　{snapshot.raw_proto.key}（{snapshot.raw_proto.name}）",
        f"  离线时长　{humanize_seconds(snapshot.idle_seconds)}",
        f"  危机状态　{'是' if snapshot.crisis else '否'}",
        f"  历史条数　{len(snapshot.history)}",
    ]
    if snapshot.relation is not None:
        relation = snapshot.relation
        lines.append(
            f"  关系　　　uid={relation.uid} name={relation.name or '-'} "
            f"聊过 {relation.msg_count} 条"
        )
    return "\n".join(lines)

