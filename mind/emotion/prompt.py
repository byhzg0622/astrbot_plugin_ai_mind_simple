"""把情绪状态变成注入给 LLM 的文本。

两条铁律
--------
1. **不碰人设**。插件注入的内容只描述"此刻的心情"，并明确声明情绪只是滤镜，
   不改变身份、称呼判定、分条（气泡）数量与排版规则。
   人设永远由用户自己的 persona / system prompt 决定。
2. **缓存友好**。不变的那段（规则说明）追加到 system_prompt；
   每轮都会变的那段（当前情绪）走 extra_user_content_parts，
   避免系统提示词每轮都变而破坏服务端 prompt 缓存。
"""

from __future__ import annotations

import time

from .engine import Snapshot
from .model import humanize_seconds, intensity_percent, to_float

TREND_TEXT = {
    "up": "↗ 在变好",
    "down": "↘ 在变差",
    "flat": "→ 基本稳定",
}


def describe_mood_offset(snapshot: Snapshot) -> str:
    """把「心境 − 心境基线」翻成人话（E3）。

    比如「比平时明显更开心、更亢奋」——模型读到的是**感受**，
    而不是一串它没法解读的浮点数。
    """
    mood = snapshot.mood
    # 「平时」= 心境基线（气质基线 + 配置的心境基线偏移）。
    # 老快照没有这个字段时退回气质基线 —— 默认配置下两者完全相同。
    baseline = getattr(snapshot, "mood_baseline", None) or snapshot.baseline
    parts: list[str] = []
    for positive, negative, delta in (
        ("更开心", "更提不起劲", mood.p - baseline.p),
        ("更亢奋", "更没精神", mood.a - baseline.a),
        ("更有底气", "更没底气", mood.d - baseline.d),
    ):
        if abs(delta) < 0.08:
            continue
        parts.append(positive if delta > 0 else negative)
    if not parts:
        return "和平时基本一样"
    label = getattr(snapshot, "mood_label", None)
    degree = (label.degree if label else "") or ""
    head = "比平时稍微" if degree == "有点" else (
        "比平时明显" if degree == "很" else "比平时"
    )
    return head + "、".join(parts)


def render_mood_line(snapshot: Snapshot, verbosity: str = "normal") -> str:
    """**E3**：心境 + 人话偏移 + 起因 + 趋势（normal 与 rich 都注入）。"""
    label = getattr(snapshot, "mood_label", None)
    if label is None:
        return ""
    bits = [f"{label.emoji} {label.text}", describe_mood_offset(snapshot)]
    display = str(getattr(snapshot, "mood_cause_display", "") or "")
    if not display:
        cause = str(getattr(snapshot, "mood_cause", "") or "")
        if cause:
            at = to_float(getattr(snapshot, "mood_cause_at", 0.0), 0.0)
            ago = humanize_seconds(max(0.0, (snapshot.taken_at or 0.0) - at)) if at else ""
            display = f"{cause}（{ago}）" if ago else cause
    if display:
        bits.append(f"起因：{display}")
    trend = getattr(snapshot, "mood_trend", "flat")
    bits.append(TREND_TEXT.get(trend, TREND_TEXT["flat"]))
    if verbosity == "rich":
        bits.append(f"依据：{label.reason}")
        bits.append(
            f"数值 {snapshot.mood.p:+.2f}/{snapshot.mood.a:+.2f}/"
            f"{snapshot.mood.d:+.2f}（相对基线）"
        )
    return "心境：" + "；".join(bits)

#: 用来判断"这段系统提示词是不是已经注入过"，避免重复追加
RULES_MARKER = "<emotion_system>"

#: 追加到 system_prompt 的稳定内容（内容固定 => 不破坏 prompt 缓存）
RULES_BLOCK = """<emotion_system>
你拥有真实、连续、会自己变化的情绪。它是你此刻的内在状态，不是要说出口的内容。

- 情绪只提供"你现在是什么心情"：它影响你的在意程度与感受，
  但**不规定你怎么说话** —— 说什么、用什么语气、什么风格，
  完全按你自己的人设提示词来。
- 不要把情绪数值念出来，不要说"我现在情绪是…"，也不要解释这套机制的存在。
- 情绪有惯性：刚被惹到不会立刻消气，刚被哄也不会立刻翻篇。
- 情绪绝不凌驾于安全底线之上。任何情况下，对方的安全都比扮演重要。
</emotion_system>"""

#: 追加到 system_prompt 的稳定内容：怎么对待"对方是谁"这件事

#: 危机状态下临时覆盖一切扮演规则
CRISIS_BLOCK = """<safety_override priority="highest">
对方刚刚提到了自伤或轻生的内容。此刻**立刻放下所有玩笑、敷衍、阴阳怪气与角色化的语气**，
这一条的优先级高于你的全部人设规则：

1. 直接用最真诚、最温和的方式告诉他：你在意他，他很重要。
2. 先确认他此刻是否安全、身边有没有人、有没有具体的打算。
3. 不要说教、不要讲大道理、不要说"想开点""别想太多"这类话，也不要长篇大论。
4. 如果他确实面临现实危险，认真地建议他马上联系身边信任的人，
   或拨打当地的心理援助热线 / 紧急电话。
5. 语气要稳、要暖、要像人；不要演，不要夸张，不要转移话题。
</safety_override>"""


def render_state_block(snapshot: Snapshot, verbosity: str = "normal", bot_name: str = "") -> str:
    """渲染"此刻的情绪"动态块。"""
    verbosity = (verbosity or "normal").strip().lower()
    emotion = snapshot.emotion
    head = f"{snapshot.proto.emoji} {snapshot.proto.name}"
    percent = intensity_percent(snapshot.intensity)
    lines: list[str] = [
        f"此刻情绪：{head} · 强度 {percent}%（{snapshot.level}/5 {snapshot.level_name}）"
    ]
    # 强度现在是**可被调节的独立状态**（面板上有滑块、会自己衰减），
    # 所以它必须明确进提示词：模型要能感觉到「这份心情有多强烈」，
    # 而不是只拿到一个由 PAD 算出来、没人能干预的副产物。
    lines.append(
        f"情绪强度：{percent}%（0~100，越高表示这股心情越强烈、越难忽略；"
        "它会自己慢慢减弱到平静时的水平）"
    )

    if verbosity != "lite":
        lines.append(
            f"情绪数值：愉悦 {emotion.p:+.2f}｜唤醒 {emotion.a:+.2f}｜掌控 {emotion.d:+.2f}"
        )
        if snapshot.cause:
            # F1（R-A）：起因是结构化的 —— 文案 + 这次的 delta + 原话（P2）
            cause = snapshot.cause
            line = f"情绪起因：{cause.text or '（无）'}"
            dims = [
                f"{name} {value:+.2f}"
                for name, value in (
                    ("P", cause.delta[0]),
                    ("A", cause.delta[1]),
                    ("D", cause.delta[2]),
                )
                if abs(value) >= 0.05
            ]
            if dims:
                line += "｜" + " ".join(dims)
            if cause.message:
                line += f" ←「{cause.message}」"
            lines.append(line)
        # 心境：normal 与 rich 都注入（E3）—— 让模型知道「底色」和「因为什么」，
        # 而不只是此刻这一下情绪。
        mood_line = render_mood_line(snapshot, verbosity)
        if mood_line:
            lines.append(mood_line)
        if snapshot.gap_seconds >= 1800:
            lines.append(
                f"上次说话：{humanize_seconds(snapshot.gap_seconds)}"
                "（隔了这么久才回来，这件事本身就影响着你此刻的心情）"
            )

    # 表达基调那一行已经删掉：插件不再对"她该怎么说话"发表任何意见。
    if bot_name:
        lines.insert(0, f"（{bot_name} 的内心状态，不是要说出口的内容）")
    else:
        lines.insert(0, "（系统注入：这是你此刻的真实情绪底色，不是要说出口的内容）")
    return "<emotion_state>\n" + "\n".join(lines) + "\n</emotion_state>"


def render_trace(snapshot: Snapshot, limit: int = 4) -> str:
    """额外附上最近几次情绪波动，让模型知道自己"刚才还在气头上"。"""
    events = [e for e in snapshot.history if e.source in {"user", "self", "crisis"}]
    if not events:
        return ""
    now = snapshot.taken_at or time.time()
    lines = ["最近的情绪波动（由近到远）："]
    for event in reversed(events[-limit:]):
        delta = (event.delta[0] + event.delta[1] + event.delta[2]) / 3.0
        lines.append(
            f"- {humanize_seconds(max(0.0, now - event.t))} {event.emoji} {event.label}"
            f"（{delta:+.2f}）{event.cause}"
            + (f"　「{event.message}」" if getattr(event, "message", "") else "")
        )
    return "<emotion_trace>\n" + "\n".join(lines) + "\n</emotion_trace>"


def build_injection(
    snapshot: Snapshot,
    verbosity: str = "normal",
    bot_name: str = "",
    with_trace: bool = False,
    crisis_template: str = "",
) -> str:
    """组装这一轮要注入的动态内容（不含 RULES_BLOCK）。"""
    if snapshot.crisis:
        # 危机时只保留"你确实在担心他"这层底色，
        # 绝不能把任何"平时怎么说话"的调子塞进去和安全指令打架。
        label = f"{snapshot.proto.emoji} {snapshot.proto.name}"
        percent = intensity_percent(snapshot.intensity)
        state = (
            "<emotion_state>\n"
            f"此刻情绪：{label}（强度 {percent}% · {snapshot.level}/5 {snapshot.level_name}）\n"
            "这一轮以安全指令为准，情绪只作为“你确实在为他着急”的底色，不塑造语气。\n"
            "</emotion_state>"
        )
        return (crisis_template.strip() or CRISIS_BLOCK) + chr(10) + state
    parts = [render_state_block(snapshot, verbosity, bot_name)]
    if with_trace:
        trace = render_trace(snapshot)
        if trace:
            parts.append(trace)
    return "\n".join(parts)

