"""情绪状态的数据模型与动力学。

设计要点
--------
1. 用 **PAD** 三维连续量描述情绪（Pleasure 愉悦 / Arousal 唤醒 / Dominance 掌控），
   比离散标签更能表达"又生气又有点想他"这类混合状态。
2. 情绪分两层：emotion（即时情绪，衰减快）+ mood（心境，衰减慢），
   两者都围绕一个长期固定的 baseline（气质基线）做指数衰减。
3. 所有衰减都按"时间差"惰性计算，不依赖定时器：
   插件重启、进程卡顿、长时间挂机都不会让情绪算错。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    """把数值夹进 [low, high]。"""
    if value < low:
        return low
    if value > high:
        return high
    return value


def to_float(value: Any, default: float = 0.0) -> float:
    """宽容地把任意输入转成有限浮点数。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return result


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    if value is None:
        return default
    return bool(value)


def now_ts() -> float:
    return time.time()


def decay_scalar(current: float, target: float, half_life: float, dt: float) -> float:
    """指数衰减：每经过 half_life 秒，与 target 的距离减半。

        v(t) = target + (v0 - target) * 0.5 ** (dt / half_life)
    """
    if dt <= 0 or half_life <= 0:
        return current
    return target + (current - target) * (0.5 ** (dt / half_life))


#: 情绪强度的衰减策略（配置项 emotion.intensity.decay 就是在选它们）。
#:
#: - exponential 指数衰减：每过一个半衰期，与目标的差距减半（默认，最像「自然消退」）
#: - linear      线性衰减：在一个半衰期内等速回到目标（半衰期即「归位时长」）
#: - off         不衰减：强度只涨不落，除非手动调
INTENSITY_DECAY_MODES: tuple[str, ...] = ("exponential", "linear", "off")


def decay_intensity(
    current: float,
    target: float,
    half_life: float,
    dt: float,
    mode: str = "exponential",
) -> float:
    """强度的**主动衰减** —— 强度不再只是情绪算出来的一个副产物，
    它是独立状态，按配置里的衰减策略自己往回退。

        exponential : v(t) = target + (v0 - target) * 0.5 ** (dt / half_life)
        linear      : v(t) = target + (v0 - target) * max(0, 1 - dt / half_life)
        off         : 原样返回
    """
    current = to_float(current, target)
    target = to_float(target, 0.0)
    if dt <= 0 or half_life <= 0:
        return current
    strategy = str(mode or "exponential").strip().lower()
    if strategy in {"off", "none", "hold", "freeze"}:
        return current
    if strategy == "linear":
        return target + (current - target) * max(0.0, 1.0 - dt / half_life)
    return decay_scalar(current, target, half_life, dt)


def humanize_seconds(seconds: float) -> str:
    """把秒数说成人话：3 分钟前 / 2 小时前 / 昨天。"""
    seconds = max(0.0, to_float(seconds))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


# ---------------------------------------------------------------------------
# PAD 向量
# ---------------------------------------------------------------------------

#: 分类时各维度的权重。愉悦度最能决定"这是什么情绪"，唤醒与掌控次之。
DISTANCE_WEIGHTS = (1.0, 0.75, 0.55)


@dataclass
class PAD:
    """三维情绪向量，每一维取值范围都是 [-1, 1]。"""

    p: float = 0.0  # Pleasure  愉悦：不开心 <-> 开心
    a: float = 0.0  # Arousal   唤醒：昏昏欲睡 <-> 亢奋
    d: float = 0.0  # Dominance 掌控：被压制 <-> 占上风

    # -- 构造 ---------------------------------------------------------------
    @staticmethod
    def of(p: float = 0.0, a: float = 0.0, d: float = 0.0) -> "PAD":
        return PAD(to_float(p), to_float(a), to_float(d)).clamped()

    @staticmethod
    def from_any(value: Any) -> "PAD":
        """从 dict / list / tuple / PAD / None 构造，失败一律返回原点。"""
        if isinstance(value, PAD):
            return value.clamped()
        if isinstance(value, dict):
            return PAD.of(value.get("p"), value.get("a"), value.get("d"))
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            return PAD.of(value[0], value[1], value[2])
        return PAD()

    # -- 运算 ---------------------------------------------------------------
    def clamped(self) -> "PAD":
        return PAD(clamp(self.p), clamp(self.a), clamp(self.d))

    def __add__(self, other: "PAD") -> "PAD":
        return PAD(self.p + other.p, self.a + other.a, self.d + other.d).clamped()

    def __sub__(self, other: "PAD") -> "PAD":
        return PAD(self.p - other.p, self.a - other.a, self.d - other.d).clamped()

    def __mul__(self, k: float) -> "PAD":
        return PAD(self.p * k, self.a * k, self.d * k).clamped()

    __rmul__ = __mul__

    def lerp(self, other: "PAD", t: float) -> "PAD":
        """向 other 线性靠拢（0 = 不动，1 = 完全变成 other）。"""
        t = clamp(t, 0.0, 1.0)
        return PAD(
            self.p + (other.p - self.p) * t,
            self.a + (other.a - self.a) * t,
            self.d + (other.d - self.d) * t,
        ).clamped()

    # -- 度量 ---------------------------------------------------------------
    def magnitude(self) -> float:
        """情绪强度（向量长度），范围 [0, sqrt(3)]。"""
        return math.sqrt(self.p * self.p + self.a * self.a + self.d * self.d)

    def distance_to(self, other: Any) -> float:
        o = PAD.from_any(other)
        wp, wa, wd = DISTANCE_WEIGHTS
        return math.sqrt(
            wp * (self.p - o.p) ** 2
            + wa * (self.a - o.a) ** 2
            + wd * (self.d - o.d) ** 2
        )

    def limit(self, max_magnitude: float) -> "PAD":
        """按比例缩放，使向量长度不超过 max_magnitude（保留方向）。"""
        mag = self.magnitude()
        if mag <= max_magnitude or mag <= 1e-9:
            return self.clamped()
        return (self * (max_magnitude / mag)).clamped()

    # -- 时间演化 -----------------------------------------------------------
    def decay_toward(self, target: Any, half_life: float, dt: float) -> "PAD":
        base = PAD.from_any(target)
        return PAD(
            decay_scalar(self.p, base.p, half_life, dt),
            decay_scalar(self.a, base.a, half_life, dt),
            decay_scalar(self.d, base.d, half_life, dt),
        ).clamped()

    # -- 序列化 -------------------------------------------------------------
    def to_dict(self) -> dict[str, float]:
        return {"p": round(self.p, 4), "a": round(self.a, 4), "d": round(self.d, 4)}

    def pretty(self) -> str:
        return f"{self.p:+.2f} / {self.a:+.2f} / {self.d:+.2f}"


ORIGIN = PAD()


# ---------------------------------------------------------------------------
# 情绪原型：把连续的 PAD 映射成人能读懂的情绪词
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Prototype:
    """一个情绪原型：PAD 空间里的锚点，附带"这种情绪叫什么"。

    这里**不放任何"该怎么说话"的描述** —— 表达基调整个功能已经删掉，
    怎么表达情绪由机器人自己的人设提示词决定。
    """

    key: str
    name: str
    emoji: str
    pad: tuple[float, float, float]
    aliases: tuple[str, ...] = ()
    #: Plutchik 的 8 条基本情绪轴之一（见 PLUTCHIK）
    family: str = ""


#: Plutchik 情绪轮（1980）：8 条基本情绪轴，四条对立轴两两相对。
#: key -> (中文名, emoji, 对立家族)
PLUTCHIK: dict[str, tuple[str, str, str]] = {
    "joy": ("喜悦", "😊", "sadness"),
    "trust": ("信任", "🤝", "disgust"),
    "fear": ("恐惧", "😨", "anger"),
    "surprise": ("惊讶", "😲", "anticipation"),
    "sadness": ("悲伤", "😢", "joy"),
    "disgust": ("厌恶", "😖", "trust"),
    "anger": ("愤怒", "😠", "fear"),
    "anticipation": ("期待", "🤗", "surprise"),
}

#: 四条对立轴（画图用）
PLUTCHIK_AXES: tuple[tuple[str, str], ...] = (
    ("joy", "sadness"),
    ("trust", "disgust"),
    ("fear", "anger"),
    ("surprise", "anticipation"),
)

PROTOTYPES: tuple[Prototype, ...] = (
    Prototype("calm", "平静", "🙂", (0.0, 0.0, 0.0)),
    Prototype("joy", "开心", "😊", (0.65, 0.45, 0.35), ("高兴", "愉快", "喜悦", "快乐", "开森"), family="joy"),
    Prototype("excited", "兴奋", "🤩", (0.70, 0.90, 0.50), ("激动", "亢奋", "上头"), family="joy"),
    Prototype("content", "满足", "😌", (0.50, -0.45, 0.35), ("惬意", "安心", "舒服"), family="joy"),
    Prototype("affection", "亲昵", "🥰", (0.75, 0.15, 0.05), ("喜欢", "爱意", "温柔", "甜蜜", "宠"), family="trust"),
    Prototype("proud", "得意", "😎", (0.55, 0.50, 0.80), ("骄傲", "自信", "得瑟", "嚣张"), family="joy"),
    Prototype("curious", "好奇", "🤔", (0.20, 0.60, 0.05), ("兴趣", "关注", "八卦"), family="anticipation"),
    Prototype("surprised", "惊讶", "😲", (0.10, 0.85, -0.35), ("震惊", "意外", "吃惊"), family="surprise"),
    Prototype("confused", "困惑", "😕", (-0.10, 0.40, -0.45), ("迷惑", "不解", "懵", "迷惑不解"), family="surprise"),
    Prototype("shy", "害羞", "😳", (0.50, 0.60, -0.50), ("羞涩", "脸红", "不好意思", "羞"), family="fear"),
    Prototype("bored", "无聊", "😐", (-0.20, -0.70, 0.00), ("没劲", "乏味", "闲"), family="disgust"),
    Prototype("tired", "疲惫", "😪", (-0.25, -0.75, -0.20), ("困", "累", "倦", "困倦"), family="disgust"),
    Prototype("lonely", "寂寞", "🌙", (-0.45, -0.25, -0.50), ("想念", "孤单", "想他", "想你了"), family="sadness"),
    Prototype("down", "低落", "😔", (-0.55, -0.40, -0.35), ("郁闷", "丧", "消沉", "不开心"), family="sadness"),
    Prototype("sad", "难过", "😢", (-0.80, -0.15, -0.60), ("伤心", "悲伤", "哭"), family="sadness"),
    Prototype("hurt", "委屈", "🥺", (-0.60, 0.30, -0.75), ("受伤", "被冤枉", "不甘"), family="sadness"),
    Prototype("angry", "生气", "😠", (-0.70, 0.80, 0.60), ("愤怒", "火大", "气", "恼"), family="anger"),
    Prototype("annoyed", "烦躁", "😤", (-0.50, 0.65, 0.00), ("烦", "恼火", "嫌烦", "不耐烦"), family="anger"),
    Prototype("anxious", "担心", "😰", (-0.45, 0.70, -0.80), ("不安", "焦虑", "担心", "慌", "心慌", "心疼"), family="fear"),
    Prototype("jealous", "吃醋", "😒", (-0.25, 0.55, -0.10), ("醋意", "酸", "嫉妒"), family="anger"),
    # 「感动」以前用 🥹（U+1F979，Emoji 14.0 / 2021）—— 老一点的系统字体没有它，
    # 面板上会显示成方框 □。换成 🤗（2010 年就有），全平台都画得出来。
    Prototype("grateful", "感动", "🤗", (0.80, 0.40, -0.15), ("感谢", "触动", "暖心"), family="trust"),
    Prototype("expect", "期待", "✨", (0.45, 0.55, 0.10), ("盼望", "跃跃欲试"), family="anticipation"),
    Prototype("cold", "冷淡", "😶", (-0.35, -0.30, 0.30), ("疏离", "冷战", "不想理"), family="disgust"),
)

PROTOTYPE_BY_KEY: dict[str, Prototype] = {p.key: p for p in PROTOTYPES}

#: 情绪别名 -> 原型 key（用于解析 <emotion>开心</emotion> 这类自表达标签）
PROTOTYPE_BY_ALIAS: dict[str, str] = {}
for _p in PROTOTYPES:
    PROTOTYPE_BY_ALIAS[_p.key] = _p.key
    PROTOTYPE_BY_ALIAS[_p.name] = _p.key
    for _alias in _p.aliases:
        PROTOTYPE_BY_ALIAS[_alias] = _p.key
del _p


def resolve_prototype(token: str) -> Prototype | None:
    """把任意情绪词解析成情绪原型。"""
    if not token:
        return None
    token = token.strip().strip("。.!！~～,， \t")
    if not token:
        return None
    key = PROTOTYPE_BY_ALIAS.get(token) or PROTOTYPE_BY_ALIAS.get(token.lower())
    if key:
        return PROTOTYPE_BY_KEY[key]
    # 退化匹配：包含关系（"有点开心" -> 开心）
    best: tuple[int, str] | None = None
    for alias, k in PROTOTYPE_BY_ALIAS.items():
        if len(alias) >= 2 and alias in token:
            if best is None or len(alias) > best[0]:
                best = (len(alias), k)
    return PROTOTYPE_BY_KEY[best[1]] if best else None


def classify(pad: PAD) -> Prototype:
    """找出离当前 PAD 最近的情绪原型。"""
    target = PAD.from_any(pad)
    best = PROTOTYPES[0]
    best_dist = float("inf")
    for proto in PROTOTYPES:
        dist = target.distance_to(PAD.from_any(proto.pad))
        if dist < best_dist:
            best, best_dist = proto, dist
    return best


#: 强度分级阈值（归一化后的向量长度）
INTENSITY_LEVELS: tuple[tuple[float, str], ...] = (
    (0.12, "几乎没有"),
    (0.30, "轻微"),
    (0.52, "明显"),
    (0.78, "强烈"),
    (9.99, "极强"),
)


def intensity_of(pad: PAD) -> float:
    """把向量长度归一化成 [0, 1] 的强度。"""
    return clamp(pad.magnitude() / 1.35, 0.0, 1.0)


def intensity_level(pad: PAD) -> tuple[int, str]:
    """【旧口径】按 PAD 向量长度推出来的强度等级。

    它只是情绪算出来的一个**副产物**，现在已经不再用它对外报强度 ——
    对外一律用 level_of_intensity（读会话里那个可调的强度状态）。
    保留它是为了调试时还能看到「光看 PAD 会算出多少」。
    """
    return level_of_intensity(intensity_of(pad))


def level_of_intensity(value: Any) -> tuple[int, str]:
    """把 **0~1 的强度状态** 映射成 (1~5 的等级, 文字描述)。

    这是现在唯一对外的强度口径：面板、下拉框、提示词、指令都读它。
    """
    score = clamp(to_float(value, 0.0), 0.0, 1.0)
    for index, (threshold, label) in enumerate(INTENSITY_LEVELS, start=1):
        if score < threshold:
            return index, label
    return 5, INTENSITY_LEVELS[-1][1]


def intensity_percent(value: Any) -> int:
    """强度状态 -> 0~100 的整数（面板与提示词里的写法）。"""
    return int(round(clamp(to_float(value, 0.0), 0.0, 1.0) * 100))



#: 心境的「无方向」死区：离气质基线不到这么远，就说不出是哪种心境
MOOD_DIRECTION_DEADZONE = 0.10
#: 判方向时把偏移向量拉到的典型长度（情绪原型的锚点大致就在这个距离上）
MOOD_DIRECTION_SCALE = 0.55


def classify_mood(
    mood: Any,
    baseline: Any,
    dead_zone: float = MOOD_DIRECTION_DEADZONE,
) -> Prototype:
    """心境标签：先按绝对坐标找最近原型，**卡在「平静」时再按偏移方向判**。

    心境的数值永远贴着气质基线（基线 (0.08, 0.22, 0.30)，心境也在这附近慢慢晃），
    而情绪原型的锚点离原点都不近 —— 于是「直接拿绝对坐标找最近原型」几乎每次都
    落在「平静」上，面板上的心境就像卡住了一样（这就是「判定不出来」的根源）。

    分两步，保证不乱标：

    1. 绝对坐标判出来就不是平静 → 直接用（心境真的跑到某个锚点附近时最准）；
    2. 判出来是平静，但**离基线已经有方向可言**（默认 ≥ 0.10）→
       把「心境 − 气质基线」这个偏移向量拉到锚点的典型长度再找最近原型 ——
       判的是「往哪边偏」，而不是「偏了多少」；
    3. 真的贴着基线 → 老老实实说「平静」。
    """
    current = PAD.from_any(mood)
    base = PAD.from_any(baseline)
    direct = classify(current)
    if direct.key != "calm":
        return direct
    offset = current - base
    distance = offset.magnitude()
    if distance < max(0.0, to_float(dead_zone, MOOD_DIRECTION_DEADZONE)) or distance <= 1e-9:
        return direct
    return classify(offset * (MOOD_DIRECTION_SCALE / distance))



@dataclass
class CauseInfo:
    """**结构化的「起因」**（F1 / R-A 全改路线）。

    以前这里就是一个字符串，于是：

    - 想在面板上加原话 / 时间 / delta，只能再拼字符串；
    - 提示词、指令、面板三处各自拼，改一处漏两处。

    现在起因是一条记录：文案 + 时间 + **原话（A1，只留最近三轮）** + 这次情绪变化的
    delta + 来源 + 命中的规则（主因/次因，B3）。三处读取都改成读字段。
    """

    text: str = ""                      # 渲染好的文案（主因 + 次因）
    t: float = 0.0                      # 这件事发生的时刻
    ago: str = ""                       # 「2 分钟前」
    message: str = ""                   # 原话（截断 + 过滤敏感信息，只留最近三轮）
    delta: tuple[float, float, float] = (0.0, 0.0, 0.0)   # 这次事件带来的 PAD 变化
    source: str = "user"                # user | crisis | self | idle | manual
    primary: str = ""                   # 主因规则 key
    secondary: tuple[str, ...] = ()      # 次因规则 key

    def __bool__(self) -> bool:
        return bool(self.text or self.message)

    def __str__(self) -> str:
        # 万一还有地方直接 f-string（新写的代码不该这样），也不至于打印对象 repr
        return self.text or ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "t": round(self.t, 2),
            "ago": self.ago,
            "message": self.message,
            "delta": {"p": round(self.delta[0], 3), "a": round(self.delta[1], 3),
                      "d": round(self.delta[2], 3)},
            "source": self.source,
            "primary": self.primary,
            "secondary": list(self.secondary),
        }

    def compact(self) -> str:
        """一行紧凑文案：`被查户口了（还顺带：被提问）·「你几岁了」· 2 分钟前`。"""
        bits = [self.text] if self.text else []
        if self.message:
            bits.append(f"「{self.message}」")
        if self.ago and self.ago != "刚刚":
            bits.append(self.ago)
        return " · ".join(bits) or ""

    @staticmethod
    def from_event(event: Any, now: float, delta: Any = None) -> "CauseInfo":
        """从一条情绪事件生成起因（事件里没有 message 就是老数据/没存原话）。"""
        cause_delta = tuple(getattr(event, "delta", (0.0, 0.0, 0.0)) or (0.0, 0.0, 0.0))
        if len(cause_delta) < 3:
            cause_delta = (0.0, 0.0, 0.0)
        at = to_float(getattr(event, "t", 0.0), 0.0)
        age = max(0.0, now - at) if at else 0.0
        return CauseInfo(
            text=str(getattr(event, "cause", "") or ""),
            t=at,
            ago=humanize_seconds(age) if at else "",
            message=str(getattr(event, "message", "") or ""),
            delta=(cause_delta[0], cause_delta[1], cause_delta[2]),
            source=str(getattr(event, "source", "user") or "user"),
            primary=str(getattr(event, "primary", "") or ""),
            secondary=tuple(getattr(event, "secondary", ()) or ()),
        )
# ---------------------------------------------------------------------------
# 谁在跟我说话
# ---------------------------------------------------------------------------


@dataclass
class Relation:
    """插件对某一个用户的长期关系记忆。"""

    uid: str = ""
    name: str = ""
    msg_count: int = 0
    last_seen: float = 0.0

    def touch(self, now: float) -> None:
        self.msg_count += 1
        self.last_seen = now

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "name": self.name,
            "msg_count": self.msg_count,
            "last_seen": round(self.last_seen, 2),
        }

    @staticmethod
    def from_dict(data: Any) -> "Relation":
        if not isinstance(data, dict):
            return Relation()
        return Relation(
            uid=str(data.get("uid", "") or ""),
            name=str(data.get("name", "") or ""),
            msg_count=to_int(data.get("msg_count"), 0),
            last_seen=to_float(data.get("last_seen"), 0.0),
        )

