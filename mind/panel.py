"""面板数据组装：把内核状态整理成前端能直接画的 JSON。

曲线是**服务端解析重建**的，不是把采样点直连起来画
--------------------------------------------------
情绪在两次事件之间是按指数衰减走的，把采样点用直线连起来会失真。
但我们也不需要为此存密集的点：只要拿到"每个事件之后的情绪值"，
就能用半衰期公式把中间任意时刻的值算出来。

    value(t) = baseline + (v0 - baseline) × 0.5 ^ ((t - t0) / half_life)

所以前端拿到的是已经算好的、平滑的曲线，而不是原始噪声点。
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from .emotion.engine import format_mood_cause, session_intensity
from .emotion.moodlabel import bar_value
from .emotion.moodwords import mood_words_payload
from .emotion.model import (
    PAD,
    PROTOTYPE_BY_KEY,
    PROTOTYPES,
    clamp,
    classify,
    decay_intensity,
    intensity_level,
    intensity_of,
    level_of_intensity,
    to_float,
)
from .memory.model import humanize_ago
from .samples import Sample

MAX_CURVE_POINTS = 900


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


def split_session_key(key: str) -> tuple[str, str, str]:
    """把 unified_msg_origin 拆成 (平台, 类型, 会话 ID)。"""
    parts = str(key or "").split(":")
    if len(parts) >= 3:
        return parts[0], parts[1], ":".join(parts[2:])
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return "", "", str(key or "")


def describe_session(key: str) -> str:
    platform, kind, ident = split_session_key(key)
    lowered = kind.lower()
    if "group" in lowered:
        label = "群聊"
    elif "friend" in lowered or "private" in lowered:
        label = "私聊"
    elif kind:
        label = kind
    else:
        label = "会话"
    return f"{label} · {ident}" if ident else label


def is_group(key: str) -> bool:
    from .memory.privacy import is_group_session

    return is_group_session(key)


def session_label(session: Any, now: float, settings: Any = None) -> tuple[str, str]:
    """会话下拉里那格情绪：**和面板「此刻心情」用同一套口径**。

    优先级：
    1. 规则声明的情绪（emotion_hint，还新鲜的话）—— 和面板一样；
    2. PAD 几何最近邻；
    3. 情绪已经衰减回基线（算出来是「平静」）时，改看**心境**
       （走 D1/D2/D3 的新判定，带副词）—— 否则只要隔几分钟，下拉里就清一色
       「平静」，什么都看不出来。

    老版本只有第 2 步，所以「会话选择里全是平静」。
    """
    emotion_proto = classify(session.emotion)
    hint = getattr(session, "emotion_hint", "") or ""
    hint_until = to_float(getattr(session, "emotion_hint_until", 0.0), 0.0)
    if hint and now < hint_until:
        hinted = PROTOTYPE_BY_KEY.get(hint)
        if hinted is not None:
            return hinted.name, hinted.emoji
    if emotion_proto.key == "calm" and settings is not None:
        mood = session.mood_label(now, settings)
        if mood.key != "calm":
            return mood.text, mood.emoji
    return emotion_proto.name, emotion_proto.emoji



def session_entries(
    *,
    engine: Any,
    sample_store: Any,
    memory_store: Any,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """所有已知会话的摘要列表，供面板顶部切换。"""
    now = time.time() if now is None else now
    engine_settings = getattr(engine, "settings", None)
    keys: dict[str, dict[str, Any]] = {}

    def ensure(key: str) -> dict[str, Any]:
        entry = keys.get(key)
        if entry is None:
            entry = {
                "key": key,
                "label": ("👥 " if is_group(key) else "💬 ") + describe_session(key),
                "group": is_group(key),
                "samples": 0,
                "users": 0,
                "messages": 0,
                "last_active": 0.0,
                "emotion": None,
                "emotion_emoji": "",
                "emotion_level": 0,
                "emotion_intensity": 0.0,
                # 「未激活」= 这台机器上还没在这个会话里跟她说过话
                #（多半是从备份导进来的会话）。**只看有没有情绪状态是不够的**：
                # 面板一点开会话就 resolve() 出一个空会话、心跳还会给它打点，
                # 那样标记点一下就自己没了。只有 relation（真消息才建）才算聊过。
                "active": False,
            }
            keys[key] = entry
        return entry

    for key, session in getattr(engine, "sessions", {}).items():
        entry = ensure(key)
        entry["users"] = len(session.users)
        if session.users:
            # 有关系记录 = 真的有消息进来过 → 已激活
            entry["active"] = True
        entry["messages"] = sum(int(r.msg_count) for r in session.users.values())
        entry["last_active"] = max(entry["last_active"], session.last_interaction, session.last_tick)
        label, emoji = session_label(session, now, engine_settings)
        entry["emotion"] = label
        entry["emotion_emoji"] = emoji
        value = session_intensity(session)
        entry["emotion_intensity"] = round(value, 4)
        entry["emotion_level"] = level_of_intensity(value)[0]

    # 有记忆、但还没产生情绪状态的群/会话也要列出来，否则面板上根本选不到它
    if memory_store is not None:
        try:
            for key in memory_store.distinct_sessions():
                ensure(key)
        except Exception:
            pass

    # 采样表里也留着会话 id。记忆库万一不可用，至少这些会话还能选到，
    # 否则面板顶部的会话列表会凭空少掉一群群聊。
    if sample_store is not None:
        try:
            for key in sample_store.sessions():
                ensure(key)
        except Exception:
            pass
    for key in sample_store.sessions() if sample_store is not None else []:
        entry = ensure(key)
        entry["samples"] = sample_store.count(key)
        latest = sample_store.latest(key)
        if latest is not None:
            entry["last_active"] = max(entry["last_active"], latest.t)
            if entry["emotion"] is None:
                proto = classify(latest.pad())
                entry["emotion"] = proto.name
                entry["emotion_emoji"] = proto.emoji
                value = intensity_of(latest.pad())
                entry["emotion_intensity"] = round(value, 4)
                entry["emotion_level"] = level_of_intensity(value)[0]

    for key in keys:
        entry = keys[key]
        entry["last_active_ago"] = humanize_ago(now - entry["last_active"]) if entry["last_active"] else "-"

    return sorted(keys.values(), key=lambda item: item["last_active"], reverse=True)


# ---------------------------------------------------------------------------
# 曲线
# ---------------------------------------------------------------------------


def build_curve(
    samples: Sequence[Sample],
    *,
    baseline: PAD,
    half_life: float,
    since: float,
    until: float,
    points: int = 400,
    hold_last: bool = False,
) -> list[dict[str, float]]:
    """按指数衰减公式重建区间内的平滑曲线。

    ``hold_last=True``（会话被**冻结**时）：最后一个采样点之后不再按衰减画，
    而是保持它的原值 —— 冻结的意思就是"情绪不动"，曲线也该是平的。
    """
    points = max(2, min(int(points), MAX_CURVE_POINTS))
    if until <= since:
        return []
    ordered = sorted((s for s in samples if s.t <= until), key=lambda s: s.t)
    if not ordered:
        # 一条采样点都没有：诚实地返回空，
        # 否则前端会画出一条贴在基线上的假直线，看起来像"她一直很平静"。
        return []
    step = (until - since) / (points - 1)
    curve: list[dict[str, float]] = []
    index = 0
    last = ordered[-1]
    for i in range(points):
        t = since + i * step
        while index < len(ordered) - 1 and ordered[index + 1].t <= t:
            index += 1
        current = ordered[index]
        if hold_last and t >= last.t:
            # 冻结中：最后一个采样点之后一律用它的原值（不再衰减）
            value = last.pad()
        elif current.t > t or half_life <= 0:
            value = baseline if current.t > t else current.pad()
        else:
            value = current.pad().decay_toward(baseline, half_life, max(0.0, t - current.t))
        curve.append(
            {"t": round(t, 2), "p": round(value.p, 4), "a": round(value.a, 4), "d": round(value.d, 4)}
        )
    return curve


def build_curve_from_history(
    history: list[Any],
    *,
    current: PAD,
    baseline: PAD,
    half_life: float,
    since: float,
    until: float,
    points: int = 400,
) -> list[dict[str, float]]:
    """采样表是空的时候，用情绪事件倒推一条近似曲线。

    每个事件记的是"这次变化了多少"（delta）。从**现在的情绪**往回减，
    就能还原出每个事件刚发生完时的情绪值；再用衰减公式把这些点铺开。

    这是近似值（两次事件之间的衰减没有精确重建），但比"什么都没有"有用得多，
    尤其对刚装上插件、采样点还没攒起来的会话。
    """
    events = [e for e in (history or []) if getattr(e, "t", 0) > 0]
    if not events:
        return []
    events = sorted(events[-200:], key=lambda e: e.t)

    value = PAD.from_any(current).clamped()
    marks: list[tuple[float, PAD]] = [(float(until), value)]
    for event in reversed(events):
        delta = event.delta or (0.0, 0.0, 0.0)
        value = PAD(
            value.p - delta[0], value.a - delta[1], value.d - delta[2]
        ).clamped()
        marks.append((float(event.t), value))
    marks.reverse()

    samples = [
        Sample(t=t, p=pad.p, a=pad.a, d=pad.d, kind="estimated")
        for t, pad in marks
        if t <= until
    ]
    if not samples:
        return []
    return build_curve(
        samples, baseline=baseline, half_life=half_life,
        since=since, until=until, points=points,
    )



def build_intensity_curve(
    history: Sequence[Any],
    *,
    current: float,
    floor: float,
    half_life: float,
    mode: str = "exponential",
    since: float,
    until: float,
    points: int = 400,
    frozen: bool = False,
    frozen_at: float = 0.0,
    frozen_intensity: float = -1.0,
) -> list[dict[str, float]]:
    """重建 0~1 的**强度曲线**（前端曲线面板上的第四条线）。

    强度是独立状态，所以它的曲线不能靠 PAD 采样点推 —— 锚点来自情绪事件：
    每条事件都记下了「事件发生之后的强度」。两个锚点之间按情绪配置里的
    衰减策略（exponential / linear / off）铺开；被冻结时从冻结点起走平。
    """
    points = max(2, min(int(points), MAX_CURVE_POINTS))
    if until <= since:
        return []

    def clean(value: Any) -> float:
        score = to_float(value, -1.0)
        return -1.0 if score < 0 else clamp(score, 0.0, 1.0)

    marks: list[tuple[float, float]] = []
    for event in history or []:
        t = to_float(getattr(event, "t", 0.0), 0.0)
        value = clean(getattr(event, "intensity", -1.0))
        if t <= 0 or value < 0 or t > until:
            continue
        marks.append((t, value))
    marks.sort(key=lambda pair: pair[0])
    # **没有数据的时间段一律填基准值**：曲线的「底」钉在 floor 上，
    # 手动调强度只影响有数据之后的那一段，不会把整条线（连历史）一起挪走 ——
    # 老版本取「第一个锚点的值」当底，调一次整个基线就跟着跑。
    if not marks or marks[0][0] > since:
        marks.insert(0, (float(since), clamp(floor, 0.0, 1.0)))

    def decay_from(t0: float, value: float, t: float) -> float:
        if t <= t0:
            return value
        return decay_intensity(value, floor, half_life, t - t0, mode)

    def value_before(t: float) -> float:
        """取 t 之前最后一个锚点，再按衰减策略走到 t。"""
        best: tuple[float, float] | None = None
        for mark in marks:
            if mark[0] <= t:
                best = mark
            else:
                break
        if best is None:
            return marks[0][1] if marks else clamp(current, 0.0, 1.0)
        return decay_from(best[0], best[1], t)

    # 冻结：从冻结点起强度保持原值（配置成不衰减时这里自然也是平的）
    hold_from = 0.0
    held: float | None = None
    if frozen:
        if frozen_at and frozen_at > 0:
            hold_from = min(max(frozen_at, since), until)
        elif marks:
            hold_from = min(max(marks[-1][0], since), until)
        else:
            hold_from = since
        if frozen_intensity is not None and frozen_intensity >= 0:
            held = clamp(to_float(frozen_intensity, 0.0), 0.0, 1.0)
        else:
            held = value_before(hold_from)
        marks = [mark for mark in marks if mark[0] <= hold_from]
        marks.append((hold_from, held))
    else:
        # 「现在」的强度是权威值：把它也当一个锚点，曲线尾巴必须落在它上面
        marks.append((float(until), clamp(to_float(current, 0.0), 0.0, 1.0)))
    marks.sort(key=lambda pair: pair[0])

    step = (until - since) / (points - 1)
    curve: list[dict[str, float]] = []
    for index in range(points):
        t = since + index * step
        if held is not None and t >= hold_from:
            value = held  # 冻结中：平的
        else:
            value = value_before(t)
        curve.append({"t": round(t, 2), "v": round(value, 4)})
    return curve

def emotion_payload(
    *,
    engine: Any,
    sample_store: Any,
    session_key: str,
    hours: float,
    max_points: int,
    now: float | None = None,
) -> dict[str, Any]:
    """情绪页需要的全部数据。"""
    now = time.time() if now is None else now
    settings = engine.settings
    session = engine.resolve(session_key, now)
    relation = None
    latest_sender = ""
    if session.users:
        latest_sender = max(session.users.values(), key=lambda r: r.last_seen or 0).uid
        relation = session.users.get(latest_sender)

    snapshot = engine.snapshot(session, relation, now)
    # 冻结 = 情绪不动：曲线从最后一个采样点起保持水平，不按衰减往下画
    frozen = session_key in getattr(engine, "frozen", set())
    since = now - max(3600.0, hours * 3600.0)
    samples = sample_store.since(session_key, since, limit=max_points) if sample_store is not None else []
    if frozen and sample_store is not None:
        # 冻结点可能比当前时间窗还早（冻结之后就不会再有新采样）：
        # 把最新那个采样点也带上，曲线才有「从哪儿开始保持水平」的锚。
        try:
            anchor = sample_store.latest(session_key)
        except Exception:  # noqa: BLE001
            anchor = None
        if anchor is not None and (not samples or anchor.t < samples[0].t):
            samples = [anchor] + list(samples)

    curve = build_curve(
        samples,
        baseline=settings.baseline,
        half_life=settings.emotion_half_life,
        since=since,
        until=now,
        points=min(max_points, MAX_CURVE_POINTS),
        hold_last=frozen,
    )
    curve_source = "samples" if curve else "none"
    if not curve and frozen:
        # 冻结中却一个采样点都没有（写入坏了之类）：画一条当前情绪的水平线，
        # 也比画出一条「自己衰减回基线」的假曲线强。
        flat_sample = Sample(
            t=since, p=session.emotion.p, a=session.emotion.a, d=session.emotion.d
        )
        curve = build_curve(
            [flat_sample],
            baseline=settings.baseline,
            half_life=settings.emotion_half_life,
            since=since,
            until=now,
            points=min(max_points, MAX_CURVE_POINTS),
            hold_last=True,
        )
        if curve:
            curve_source = "frozen"
    if not curve:
        # 采样表还是空的：用情绪事件倒推一条近似曲线，
        # 免得刚装上插件的人只看到一片空白
        curve = build_curve_from_history(
            session.history,
            current=session.emotion,
            baseline=settings.baseline,
            half_life=settings.emotion_half_life,
            since=since,
            until=now,
            points=min(max_points, MAX_CURVE_POINTS),
        )
        if curve:
            curve_source = "history"

    events = []
    for event in session.history[-120:]:
        events.append(
            {
                "t": round(event.t, 2),
                "label": event.label,
                "emoji": event.emoji,
                "cause": event.cause,
                "source": event.source,
                "delta": [round(v, 3) for v in event.delta],
                # A1 原话（只留最近三轮）+ B3 主/次因 —— 面板事件流要靠它分层与串因果
                "message": getattr(event, "message", "") or "",
                "primary": getattr(event, "primary", "") or "",
                "secondary": list(getattr(event, "secondary", ()) or ()),
            }
        )

    mood_events = []
    for shift in session.mood_history[-40:]:
        mood_events.append(
            {
                "t": round(shift.t, 2),
                "ago": humanize_ago(now - shift.t) if shift.t else "-",
                "key": shift.key,
                "label": shift.label,
                "emoji": shift.emoji,
                "cause": shift.cause,
                # 相对**心境基线**的偏移：看的就是「比平时差多少」
                "offset": [round(v, 3) for v in shift.offset],
                "source": shift.source,
            }
        )

    intensity = session_intensity(session)
    level, level_name = level_of_intensity(intensity)
    # 心境：走 D1/D2/D3 的新判定（带副词、依据、驻留）
    mood_label = session.mood_label(now, settings)
    # 强度曲线：与 PAD 曲线同窗，冻结时同样走平
    intensity_curve = build_intensity_curve(
        session.history,
        current=intensity,
        floor=settings.intensity_floor,
        half_life=settings.intensity_half_life,
        mode=settings.intensity_decay,
        since=since,
        until=now,
        points=min(max_points, MAX_CURVE_POINTS),
        frozen=frozen,
        frozen_at=session.frozen_at,
        frozen_intensity=session.frozen_intensity,
    )

    return {
        "session": {
            "key": session_key,
            "label": describe_session(session_key),
            "group": is_group(session_key),
        },
        "now": now,
        "since": since,
        "hours": hours,
        "current": {
            "p": round(session.emotion.p, 4),
            "a": round(session.emotion.a, 4),
            "d": round(session.emotion.d, 4),
            "label": snapshot.proto.name,
            "emoji": snapshot.proto.emoji,
            "level": level,
            "level_name": level_name,
            # 现在的强度 = 会话里那个可调节、会自己衰减的独立状态
            "intensity": round(intensity, 4),
            # 旧口径（只看 PAD 长度）留给调试对照，面板不再显示它
            "pad_intensity": round(intensity_of(session.emotion), 4),
            "cause": snapshot.cause.to_dict(),
            "crisis": snapshot.crisis,
        },
        "mood": {
            "p": round(session.mood.p, 4),
            "a": round(session.mood.a, 4),
            "d": round(session.mood.d, 4),
            # 心境自己的冻结开关（会话级冻结时也按冻着显示，由面板取或）
            "frozen": bool(getattr(session, "frozen_mood", False)),
            # 超基线触发中（长时间没人找她）—— 只发状态，不发设置：
            # 基线/超基线怎么设永远只住在配置页
            "cold": bool(getattr(session, "mood_cold_since", 0.0) > 0),
            "key": mood_label.key,
            # 带副词的说法：「有点开心」「很烦」
            "label": mood_label.text,
            "name": mood_label.name,
            "degree": mood_label.degree,
            "emoji": mood_label.emoji,
            # D7：判定依据 + 心境起因
            "reason": mood_label.reason,
            "cause": session.mood_cause,
            "cause_display": format_mood_cause(
                session.mood_cause, session.mood_cause_at, now, settings.mood_live_window
            ),
            "cause_ago": (
                humanize_ago(now - session.mood_cause_at) if session.mood_cause_at else "-"
            ),
            "magnitude": mood_label.magnitude,
            # 迷你条：-1（更差）~ 0（基线）~ 1（更好）——**按滑杆满量程归一**，
            # 单轴拉到 ±0.6 就顶到最左/最右（老的加权 score 永远到不了边）。
            # 中点是**心境基线**（气质基线 + 配置偏移）：配置调过基线时，
            # 页面上这里照样显示 0 —— 基线设置只体现在配置页。
            "bar": round(bar_value(session.mood, settings.mood_rest), 4),
            "score": mood_label.score,
            "since": round(mood_label.since, 2),
            "since_ago": (
                humanize_ago(now - mood_label.since) if mood_label.since else "-"
            ),
            # G2 趋势
            "trend": session.mood_trend(),
            "trend_text": {
                "up": "↗ 在变好", "down": "↘ 在变差", "flat": "→ 稳定"
            }.get(session.mood_trend(), "→ 稳定"),
        },
        # 气质基线：曲线上那条虚线 + 情绪的回落目标（**不是**心境的基线）
        "baseline": {
            "p": settings.baseline.p,
            "a": settings.baseline.a,
            "d": settings.baseline.d,
        },
        # 心境基线：心境的静息点 = 气质基线 + 配置偏移。
        # 滑杆/读取当前值的 0 位按它算，所以配置在页面上看不出痕迹（只在配置页）。
        "mood_baseline": {
            "p": settings.mood_rest.p,
            "a": settings.mood_rest.a,
            "d": settings.mood_rest.d,
        },
        "params": {
            "emotion_half_life": settings.emotion_half_life,
            "mood_half_life": settings.mood_half_life,
            "preset": settings.preset_key,
            "bot_name": settings.bot_name,
            # 强度的衰减策略（照抄自情绪配置 emotion.intensity.*）
            "intensity_decay": settings.intensity_decay,
            "intensity_half_life": settings.intensity_half_life,
            "intensity_floor": settings.intensity_floor,
            "intensity_gain": settings.intensity_gain,
            # 心境推进/判定的参数（面板上的「心境」卡要说人话时用得到）
            "mood_half_life": settings.mood_half_life,
            "mood_live_window": settings.mood_live_window,
            "mood_label_hold": settings.mood_label_hold,
        },
        #: 情绪原型全表（PAD 锚点 + emoji）：面板「一键改成」直接拿它当按钮，
        #: 保证点哪个按钮就分类回哪个情绪 —— 前端不再自己写死一份容易过期的数值。
        "prototypes": [
            {
                "key": proto.key,
                "name": proto.name,
                "emoji": proto.emoji,
                "p": round(float(proto.pad[0]), 4),
                "a": round(float(proto.pad[1]), 4),
                "d": round(float(proto.pad[2]), 4),
            }
            for proto in PROTOTYPES
        ],
        "curve": curve,
        "curve_source": curve_source,
        # 曲线面板上的第四条线：情绪强度（0~1，右轴）
        "intensity_curve": intensity_curve,
        "sample_count": sample_store.count(session_key) if sample_store is not None else 0,
        "sample_total": sample_store.count() if sample_store is not None else 0,
        "history_count": len(session.history),
        "samples": [s.to_dict() for s in samples[-600:]],
        "events": events,
        # 「最近心境事件」：里程碑（换词/手动/重置/漂移），与情绪事件分开展示
        "mood_events": mood_events,
        # 心境的一键改成：**心境描述词**（不是情绪原型那套锚点），后端是唯一真源
        "mood_words": mood_words_payload(),
        "users": [
            {
                "uid": r.uid,
                "name": r.name,
                "msg_count": r.msg_count,
                "last_seen_ago": humanize_ago(now - r.last_seen) if r.last_seen else "-",
            }
            for r in sorted(session.users.values(), key=lambda x: -x.last_seen, reverse=True)[:20]
        ],
        "idle_seconds": round(max(0.0, now - session.last_interaction), 2),
    }


# ---------------------------------------------------------------------------
# 记忆
# ---------------------------------------------------------------------------


def memory_to_dict(memory: Any, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    return {
        "id": memory.id,
        "content": memory.content,
        "keywords": list(memory.keywords),
        "kind": memory.kind,
        "subject": memory.subject,
        "scope": memory.scope,
        "session_id": memory.session_id,
        "owner_id": memory.owner_id,
        "importance": round(memory.importance, 3),
        "pinned": bool(memory.pinned),
        "sensitive": bool(memory.sensitive),
        "sensitive_reasons": list(memory.sensitive_reasons),
        "source": memory.source,
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
        "hit_count": memory.hit_count,
        "ago": humanize_ago(now - (memory.updated_at or memory.created_at or now)),
        "embedded": memory.embedding is not None,
    }


def memory_payload(
    *,
    store: Any,
    query: str = "",
    scope: str = "",
    kind: str = "",
    pinned: Any = None,
    sensitive: Any = None,
    session_id: str = "",
    source: str = "",
    limit: int = 50,
    offset: int = 0,
    now: float | None = None,
) -> dict[str, Any]:
    """面板「长期记忆」页的数据。

    ``session_id`` 有值时只回这个会话的记忆，外加 ``session_id`` 为空的
    通用记忆 —— 这样粘在顶部选中会话上的列表才不会漏掉全局条目。
    """
    now = time.time() if now is None else now
    items, total = store.query_page(
        q=query,
        scope=scope,
        kind=kind,
        pinned=pinned,
        sensitive=sensitive,
        limit=limit,
        offset=offset,
        session_id=session_id,
        source=source,
    )
    return {
        "items": [memory_to_dict(m, now) for m in items],
        "total": total,
        "offset": offset,
        "limit": limit,
        "session_id": session_id,
        "stats": store.stats(),
    }


__all__ = [
    "MAX_CURVE_POINTS",
    "build_curve_from_history",
    "build_curve",
    "build_intensity_curve",
    "describe_session",
    "emotion_payload",
    "is_group",
    "memory_payload",
    "memory_to_dict",
    "session_entries",
    "split_session_key",
]
