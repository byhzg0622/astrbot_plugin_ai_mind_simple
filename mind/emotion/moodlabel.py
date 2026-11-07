"""心境标签：让「判心境」更像人。

几何最近邻（拿心境的绝对坐标在 23 个锚点里找最近的）有两个毛病：

1. **硬切** —— 心境挪一点点就可能从「期待」跳到「好奇」，词一直在抖；
2. **说不出依据** —— 判出来就完了，没人知道为什么是这个词。

这里改成四件套（用户选的 D1/D2/D3/D7）：

- **D1 主导维度**：人判心境的顺序是「先看好坏(P) → 再看亢奋还是提不起劲(A)
  → 再看有没有底气(D)」。用一张规则表代替几何最近邻（见 :func:`direction`）。
- **D2 程度副词**：同一个词按偏移幅度加「有点 / 」/「很」，轻重读得出来（:func:`degree_of`）。
- **D3 迟滞驻留**：换词要**连续待够**驻留时长、且偏移跨过入口阈值（死区 ×1.25），
  否则沿用旧词 —— 一段对话里心境词不来回抖（驻留由引擎按配置执行）。
- **D7 依据**：每次判定都给出人话依据（:func:`reason_of`），换词时由引擎把
  「最近那次引起变化的事」一起记下来当起因。

**只描述「往哪偏」，不掺任何「该怎么说话」的内容。**
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import PAD, PROTOTYPE_BY_KEY, clamp, to_float

#: 死区：偏移小于它就老老实实叫「平静」
DEAD_ZONE = 0.10
#: 入口阈值：要**进入**一个有名字的心境，偏移必须 ≥ 死区 ×1.25（迟滞的另一半）
ENTRY_MAGNITUDE = DEAD_ZONE * 1.25
#: D2 分档：0.10~0.22 → 「有点」，0.22~0.45 → 不加词，≥0.45 → 「很」
SOFT_MAX = 0.22
STRONG_MIN = 0.45


@dataclass(frozen=True)
class MoodLabel:
    """一次心境判定的结果。"""

    key: str = "calm"
    name: str = "平静"
    emoji: str = "🙂"
    degree: str = ""          # "有点" / "" / "很"
    reason: str = ""          # D7：人话依据（相对基线偏了多少）
    magnitude: float = 0.0     # 偏移长度
    score: float = 0.0         # 好坏轴 -1~1（面板的迷你条）
    since: float = 0.0         # 这个词从什么时候开始用的

    @property
    def text(self) -> str:
        """带副词的完整说法：「有点开心」「很烦」。"""
        return f"{self.degree}{self.name}"


def _proto(key: str):
    return PROTOTYPE_BY_KEY.get(key) or PROTOTYPE_BY_KEY["calm"]


def offsets(mood: Any, baseline: Any) -> tuple[float, float, float]:
    """心境相对气质基线的逐维偏移。"""
    delta = PAD.from_any(mood) - PAD.from_any(baseline)
    return delta.p, delta.a, delta.d


def score_of(p: float, a: float, d: float) -> float:
    """好坏轴（-1~1）：P 主导，掺一点唤醒与掌控 —— 面板迷你条用它。"""
    return clamp(p + 0.2 * a + 0.1 * d, -1.0, 1.0)


#: 心境迷你条的满量程 = 三根滑杆的上限（±0.6）：**单轴拉到头就要顶到最左/最右**
MOOD_BAR_SCALE = 0.6


def bar_value(mood: Any, baseline: Any, scale: float = MOOD_BAR_SCALE) -> float:
    """迷你条的显示值（-1~1）：方向按好坏轴 score_of，幅度取三轴里最大的那个。

    老版本直接用加权 score —— 能量/底气拉满也只有两三成长度，到不了边；
    现在按满量程归一：任何一根轴拉到 ±0.6，条子就顶到最左/最右。
    """
    p, a, d = offsets(mood, baseline)
    magnitude = max(abs(p), abs(a), abs(d))
    score = score_of(p, a, d)
    if score > 0:
        sign = 1.0
    elif score < 0:
        sign = -1.0
    else:
        return 0.0
    return clamp(sign * magnitude / max(0.01, scale), -1.0, 1.0)


def degree_of(magnitude: float) -> str:
    """D2：把幅度翻成副词。"""
    value = to_float(magnitude, 0.0)
    if value < SOFT_MAX:
        return "有点"
    if value < STRONG_MIN:
        return ""
    return "很"


def direction(p: float, a: float, d: float) -> str:
    """**D1 主导维度规则**：按「好坏 → 底气 / 亢奋度 → 兜底」判方向（**底气先判**）。

    入参是**相对基线的偏移**，不是绝对坐标 —— 心境本来就是「比平时差多少」。
    返回情绪原型的 key（emoji/中文名直接从原型表取）。
    """
    if p >= 0.15:                                    # 好的一面
        # **底气先判**：心境里「有没有底气」最容易被感知 ——
        # 老版本把它排在 A 后面，动了能量之后底气怎么拉都不会换词（报过的 bug）。
        if d <= -0.30:
            return "affection"                      # 好，但没底气 → 亲昵/依赖
        if d >= 0.30 and a >= -0.15:
            return "proud"                          # 好、有底气 → 得意
        if a >= 0.30:
            return "excited"                        # 又好又亢奋
        if a >= 0.12:
            return "joy"                            # 开心
        if a <= -0.15:
            return "content"                        # 好，但很安静 → 满足
        if d >= 0.30:
            return "content"                        # 有底气却提不起劲 → 踏实的满足
        return "joy"
    if p <= -0.15:                                   # 坏的一面
        if a >= 0.45 and d >= 0.15:
            return "angry"                          # 又气又有底气
        if a >= 0.35 and d <= -0.30:
            return "anxious"                        # 坏 + 亢奋 + 没底 → 忐忑
        if a >= 0.22:
            return "annoyed"                        # 烦躁
        if d >= 0.30 and a <= -0.15:
            return "cold"                           # 坏 + 没劲 + 有底气 → 冷淡/硬气
        if a <= -0.15:
            return "sad" if p <= -0.45 else "down"  # 提不起劲 → 低落/难过
        if d <= -0.30:
            return "hurt"                           # 坏 + 没底气（能量中性）→ 委屈
        if d >= 0.30:
            return "cold"                           # 坏 + 有底气（能量中性）→ 冷淡
        return "down"
    # 好坏不明显：只看亢奋度与底气
    if a >= 0.20:
        return "curious"                            # 有劲头
    if a <= -0.20:
        return "tired" if p <= -0.05 else "bored"   # 提不起劲
    if d <= -0.30:
        return "hurt"
    if d >= 0.35:
        return "proud"                              # 底气足
    return "calm"
def reason_of(p: float, a: float, d: float) -> str:
    """**D7**：把偏移翻成人话依据，例如
    「愉悦比平时高 0.39；唤醒比平时高 0.19」。"""
    parts: list[str] = []
    for label, value in (("愉悦", p), ("唤醒", a), ("掌控", d)):
        if abs(value) < 0.08:
            continue
        where = "高" if value > 0 else "低"
        parts.append(f"{label}比平时{where} {abs(value):.2f}")
    if not parts:
        return "和气质基线基本重合，没有明显偏向"
    return "；".join(parts)


def evaluate(mood: Any, baseline: Any, *, since: float = 0.0) -> MoodLabel:
    """做一次判定（不含驻留/迟滞 —— 那部分由引擎按配置执行）。"""
    p, a, d = offsets(mood, baseline)
    magnitude = to_float((PAD(p, a, d)).magnitude(), 0.0)
    score = score_of(p, a, d)
    if magnitude < DEAD_ZONE:
        key = "calm"
    else:
        key = direction(p, a, d)
    if key == "calm":
        # 平静就是平静：方向判定落在中性区时也不许挂副词
        #（否则会冒出「有点平静」「很平静」这种话）
        degree = ""
        reason = "偏移小于死区，和平时的心境一样" if magnitude < DEAD_ZONE else reason_of(p, a, d)
    else:
        degree = degree_of(magnitude)
        reason = reason_of(p, a, d)
    proto = _proto(key)
    return MoodLabel(
        key=key,
        name=proto.name,
        emoji=proto.emoji,
        degree=degree,
        reason=reason,
        magnitude=round(magnitude, 4),
        score=round(score, 4),
        since=since,
    )


__all__ = [
    "DEAD_ZONE",
    "ENTRY_MAGNITUDE",
    "MOOD_BAR_SCALE",
    "MoodLabel",
    "SOFT_MAX",
    "STRONG_MIN",
    "bar_value",
    "degree_of",
    "direction",
    "evaluate",
    "offsets",
    "reason_of",
    "score_of",
]
