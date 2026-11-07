"""情绪引擎：多会话状态的创建、演化、持久化。

对外只暴露几个动作：取状态、施加冲击、读快照、落盘。
所有数学都在 model.py，所有规则都在 appraisal.py / presets.py，
所以这一层读起来应该像"流程"而不是"算法"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from ..textfix import strip_message_markup
from .appraisal import Appraiser, Rule, Stimulus, parse_custom_rule
from .moodlabel import DEAD_ZONE, ENTRY_MAGNITUDE, MoodLabel, evaluate as evaluate_mood_label
from .model import (
    PAD,
    CauseInfo,
    Prototype,
    Relation,
    clamp,
    classify,
    classify_mood,
    decay_intensity,
    humanize_seconds,
    intensity_level,
    intensity_of,
    level_of_intensity,
    to_bool,
    to_float,
)
from .model import PROTOTYPE_BY_KEY
from .presets import DEFAULT_PRESET, PRESETS, Preset, get_preset
from .store import JsonStore

STATE_VERSION = 1

#: 触发超基线之后，心境往超基线回落的半衰期（秒）。
#: 触发本身已经等了 mood_cold_after_minutes，回落再拖十几个小时就不像
#: 「把很长时间不找机器人体现在心境上」了 —— 30 分钟半衰期意味着
#: 触发后约 1.5 小时就会读到「冷淡」。
COLD_HALF_LIFE = 1800.0

#: 超基线的起因文案：心境起因（E2）只认对方说的话，长时间没人说话是
#: 唯一的例外 —— 不写清楚的话，「很冷淡」会挂上上一次聊天的起因。
CAUSE_NO_CONTACT = "很久没说话了"


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------


def cfg_get(config: Mapping[str, Any] | None, path: str, default: Any = None) -> Any:
    """从（可能嵌套的）插件配置里安全地取一个值。"""
    current: Any = config
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return default
            current = current[part]
        else:
            return default
    return default if current is None else current


def _clampf(value: Any, default: float, low: float, high: float) -> float:
    return clamp(to_float(value, default), low, high)


def _pad_cfg(config: Mapping[str, Any] | None, path: str, default: PAD) -> PAD:
    """从配置里读一个 {p, a, d} 偏移；缺字段/格式不对就用默认。"""
    raw = cfg_get(config, path, None)
    if isinstance(raw, Mapping):
        return PAD.of(
            raw.get("p", default.p), raw.get("a", default.a), raw.get("d", default.d),
        )
    return PAD.from_any(default)


def _intensity_decay_mode(value: Any) -> str:
    """把配置里的强度衰减策略洗成引擎认的三种之一。"""
    mode = str(value or "exponential").strip().lower()
    aliases = {
        "exp": "exponential",
        "exponential": "exponential",
        "指数": "exponential",
        "线性": "linear",
        "none": "off",
        "关闭": "off",
        "不衰减": "off",
    }
    return aliases.get(mode, "exponential") if mode not in {"linear", "off"} else mode


def session_intensity(session: "SessionMood") -> float:
    """读一个会话**当前的强度状态**（0~1）。

    老存档里没有这个字段：按 PAD 长度回填一次，之后就一直用自己的值 ——
    也就是说强度从这一刻起从「副产物」升级成「独立状态」。
    """
    raw = getattr(session, "intensity", -1.0)
    if raw is None or raw < 0:
        return clamp(intensity_of(session.emotion), 0.0, 1.0)
    return clamp(to_float(raw, 0.0), 0.0, 1.0)


def at_mood_rest(mood: Any, settings: "Settings") -> bool:
    """心境是不是停在**基线（平静）**上。

    判定口径和标签一致：离心境基线不到一个死区就算「在基线上」——
    超基线的触发时长（mood_cold_after_minutes）数的就是这段连续时间。
    """
    return (PAD.from_any(mood) - settings.mood_rest).magnitude() < DEAD_ZONE


@dataclass
class Settings:
    """把插件配置翻译成引擎能直接用的数值。"""

    enabled: bool = True
    preset_key: str = DEFAULT_PRESET
    bot_name: str = ""
    baseline: PAD = field(default_factory=PAD)
    emotion_half_life: float = 240.0
    #: 心境半衰期：6 小时（1.5 小时的话 3 小时就散干净了，算不上「长期心境」）
    mood_half_life: float = 21600.0
    mood_coupling: float = 0.10
    #: 对话窗口（秒）：窗口内心境持续朝「最近一次互动时的情绪」靠拢
    mood_live_window: float = 1800.0
    #: 心境换词驻留（秒）：新词要连续待够才替换（防抖）
    mood_label_hold: float = 300.0
    #: **心境基线**：心境的静息点 = 气质基线 + 这个偏移。
    #: 只在配置里设（emotion.dynamics.mood_baseline），情绪曲线页不放它的控件 ——
    #: 页面上看到的「平静 / 迷你条中点 / 滑杆 0 位」跟着它走，所以配置的效果
    #: 在页面上永远显示成「正常」，看不出这里被调过。
    mood_baseline_offset: PAD = field(default_factory=PAD)
    #: **超基线**：心境基线上的静息维持太久、又一直没人说话时，心境被拉向这里
    #: （默认方向就是「冷淡」—— 长时间不找机器人，就体现在心境上）
    mood_cold_offset: PAD = field(
        default_factory=lambda: PAD.of(-0.30, -0.12, 0.35)
    )
    #: 超基线触发时长（分钟）：心境在**基线（平静）**上连续停留这么久、
    #: 且这段时间没有对话，才开始被拉向超基线。0 = 关掉超基线。
    mood_cold_after_minutes: float = 360.0
    max_delta: float = 0.62
    idle_enabled: bool = True
    idle_threshold: float = 3 * 3600.0
    idle_max_effect: float = 0.30
    crisis_enabled: bool = True
    crisis_hold: float = 600.0
    self_feedback: str = "heuristic"  # heuristic | tag | off
    self_feedback_gain: float = 0.22
    #: 规则明确声明的情绪标签保持多久（几何最近邻之前的"权威答案"）
    hint_hold: float = 300.0
    #: 词表兜底（中文情感词汇本体）
    lexicon_enabled: bool = True
    lexicon_gain: float = 0.25
    # ------------------------------------------------------------------
    # 情绪强度：独立状态，可手动调（面板滑块），也会自己衰减
    # ------------------------------------------------------------------
    #: 衰减策略 exponential / linear / off（配置 emotion.intensity.decay）
    intensity_decay: str = "exponential"
    #: 强度半衰期（秒）：exponential 下每过这么久与目标的差距减半
    intensity_half_life: float = 900.0
    #: 衰减目标：没事时强度停在这里（0~1）
    intensity_floor: float = 0.15
    #: 每次情绪冲击换算成多少强度（0~1 的增量比例）
    intensity_gain: float = 0.55
    #: 新会话 / 重置之后的初始强度
    intensity_default: float = 0.30
    history_size: int = 20
    max_sessions: int = 300
    session_ttl_days: float = 90.0

    @property
    def mood_rest(self) -> PAD:
        """心境的静息点（面板说的「基线」）= 气质基线 + 配置的基线偏移。"""
        off = self.mood_baseline_offset
        return PAD.of(
            self.baseline.p + off.p, self.baseline.a + off.a, self.baseline.d + off.d,
        )

    @property
    def mood_cold(self) -> PAD:
        """超基线 = 心境基线 + 超基线偏移（默认方向就是冷淡）。"""
        base = self.mood_rest
        off = self.mood_cold_offset
        return PAD.of(base.p + off.p, base.a + off.a, base.d + off.d)

    @staticmethod
    def from_config(config: Mapping[str, Any] | None) -> "Settings":
        # 人格预设（persona.preset / 自定义人格文件）已经整体删除：
        # 无论配置里残留着什么，都只用这一套默认情绪参数。
        preset = get_preset(None)

        # 默认一切都用上面这套参数，避免 WebUI 里的默认值把它"盖掉"。
        # 想手调的人把 custom_temperament / custom_values 打开即可。
        if to_bool(cfg_get(config, "persona.custom_temperament", False), False):
            baseline = PAD.of(
                to_float(cfg_get(config, "persona.temperament.pleasure"), preset.baseline.p),
                to_float(cfg_get(config, "persona.temperament.arousal"), preset.baseline.a),
                to_float(cfg_get(config, "persona.temperament.dominance"), preset.baseline.d),
            )
        else:
            baseline = PAD.from_any(preset.baseline)

        custom_dynamics = to_bool(cfg_get(config, "dynamics.custom_values", False), False)
        emotion_hl = (
            to_float(cfg_get(config, "dynamics.emotion_half_life"), preset.emotion_half_life)
            if custom_dynamics
            else preset.emotion_half_life
        )
        mood_hl = (
            to_float(cfg_get(config, "dynamics.mood_half_life"), preset.mood_half_life)
            if custom_dynamics
            else preset.mood_half_life
        )
        coupling = (
            to_float(cfg_get(config, "dynamics.mood_coupling"), preset.mood_coupling)
            if custom_dynamics
            else preset.mood_coupling
        )
        max_delta = (
            to_float(cfg_get(config, "dynamics.max_delta"), 0.62) if custom_dynamics else 0.62
        )

        return Settings(
            enabled=to_bool(cfg_get(config, "enabled", True), True),
            preset_key=preset.key,
            bot_name=str(cfg_get(config, "persona.bot_name", "") or ""),
            baseline=baseline,
            emotion_half_life=max(5.0, emotion_hl),
            mood_half_life=max(60.0, mood_hl),
            mood_coupling=_clampf(coupling, preset.mood_coupling, 0.0, 1.0),
            mood_live_window=max(
                0.0,
                to_float(
                    cfg_get(config, "dynamics.mood_live_window_minutes", 30),
                    30.0,
                )
                * 60.0,
            ),
            mood_label_hold=max(
                0.0,
                to_float(
                    cfg_get(config, "dynamics.mood_label_hold_seconds", 300),
                    300.0,
                ),
            ),
            # 心境基线 / 超基线 / 触发时长 —— 只在配置里设（不进情绪曲线页）
            mood_baseline_offset=_pad_cfg(
                config, "dynamics.mood_baseline", PAD(),
            ),
            mood_cold_offset=_pad_cfg(
                config, "dynamics.mood_cold", PAD.of(-0.30, -0.12, 0.35),
            ),
            mood_cold_after_minutes=max(
                0.0,
                to_float(
                    cfg_get(config, "dynamics.mood_cold_after_minutes", 360),
                    360.0,
                ),
            ),
            max_delta=_clampf(max_delta, 0.62, 0.05, 1.0),
            idle_enabled=to_bool(cfg_get(config, "idle.enabled", True), True),
            idle_threshold=max(60.0, to_float(cfg_get(config, "idle.threshold_minutes", 180), 180) * 60.0),
            idle_max_effect=_clampf(cfg_get(config, "idle.max_effect", 0.30), 0.30, 0.0, 0.8),
            crisis_enabled=to_bool(cfg_get(config, "safety.crisis_detection", True), True),
            crisis_hold=max(0.0, to_float(cfg_get(config, "safety.hold_minutes", 10), 10) * 60.0),
            self_feedback=str(cfg_get(config, "expression.self_feedback", "heuristic") or "heuristic").strip().lower(),
            self_feedback_gain=_clampf(cfg_get(config, "expression.self_feedback_gain", 0.22), 0.22, 0.0, 1.0),
            hint_hold=max(0.0, to_float(cfg_get(config, "expression.style_hold_seconds", 300), 300)),
            lexicon_enabled=to_bool(cfg_get(config, "appraisal.lexicon", True), True),
            lexicon_gain=_clampf(cfg_get(config, "appraisal.lexicon_gain", 0.25), 0.25, 0.05, 1.0),
            intensity_decay=_intensity_decay_mode(
                cfg_get(config, "intensity.decay", preset.intensity_decay)
            ),
            intensity_half_life=max(
                5.0,
                to_float(
                    cfg_get(config, "intensity.half_life", preset.intensity_half_life),
                    preset.intensity_half_life,
                ),
            ),
            intensity_floor=_clampf(
                cfg_get(config, "intensity.floor", preset.intensity_floor),
                preset.intensity_floor, 0.0, 1.0,
            ),
            intensity_gain=_clampf(
                cfg_get(config, "intensity.gain", preset.intensity_gain),
                preset.intensity_gain, 0.0, 2.0,
            ),
            intensity_default=_clampf(
                cfg_get(config, "intensity.default", preset.intensity_default),
                preset.intensity_default, 0.0, 1.0,
            ),
            history_size=int(_clampf(cfg_get(config, "advanced.history_size", 20), 20, 3, 200)),
            max_sessions=int(_clampf(cfg_get(config, "advanced.max_sessions", 300), 300, 10, 5000)),
            session_ttl_days=_clampf(cfg_get(config, "advanced.session_ttl_days", 90), 90, 1, 3650),
        )

# ---------------------------------------------------------------------------
# 状态容器
# ---------------------------------------------------------------------------


def format_mood_cause(
    cause: str,
    at: float,
    now: float,
    live_window: float,
) -> str:
    """**E2**：心境起因的人话版。

    心境起因只认**对方说的话**（见 apply_stimulus 里那处 capture）；
    如果这件事已经过去了很久（超出对话窗口），就要说清这是「延续」而不是新原因：

        被夸了（5 分钟前）              ← 还在窗口内
        延续自 3 小时前的「被夸了」——不是新原因   ← 窗口外
    """
    text = str(cause or "")
    if not text:
        return ""
    age = max(0.0, now - at) if at else 0.0
    ago = humanize_seconds(age)
    if text == CAUSE_NO_CONTACT:
        # 「很久没说话了」不是对方说的话，也就谈不上「延续自上次那句话」——
        # 它就是**现在**的状况，照直说隔了多久。
        return f"{text}（{ago}）" if ago else text
    if at and live_window and age > live_window:
        return f"延续自 {ago}的「{text}」——不是新原因"
    return f"{text}（{ago}）"


@dataclass
class MoodShift:
    """一条**心境**事件（和情绪事件分开记，面板上是「最近心境事件」）。

    心境是连续推进的，不能每 20 秒记一条，所以只记**里程碑**：
    换词（label）、手动设定（manual）、重置（reset）、明显漂移（drift，每 10 分钟
    最多一条且要挪够 0.10）。offset 是相对气质基线的偏移 —— 看的就是「比平时差多少」。
    """

    t: float = 0.0
    key: str = "calm"
    label: str = "平静"          # 带副词的完整说法
    emoji: str = "🙂"
    cause: str = ""              # 心境起因 / 判定依据
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    source: str = "label"        # label | manual | reset | drift

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": round(self.t, 2),
            "key": self.key,
            "label": self.label,
            "emoji": self.emoji,
            "cause": self.cause,
            "offset": [round(v, 3) for v in self.offset],
            "source": self.source,
        }

    @staticmethod
    def from_dict(data: Any) -> "MoodShift | None":
        if not isinstance(data, dict):
            return None
        raw = data.get("offset") or [0.0, 0.0, 0.0]
        if not isinstance(raw, (list, tuple)):
            raw = [0.0, 0.0, 0.0]
        while len(raw) < 3:
            raw.append(0.0)
        return MoodShift(
            t=to_float(data.get("t"), 0.0),
            key=str(data.get("key", "calm") or "calm"),
            label=str(data.get("label", "") or ""),
            emoji=str(data.get("emoji", "") or ""),
            cause=str(data.get("cause", "") or ""),
            offset=(to_float(raw[0]), to_float(raw[1]), to_float(raw[2])),
            source=str(data.get("source", "label") or "label"),
        )


@dataclass
class MoodEvent:
    """情绪史上的一条记录。"""

    t: float
    label: str
    emoji: str
    cause: str
    delta: tuple[float, float, float]
    source: str = "user"
    #: 这次事件发生**之后**的强度状态（0~1）。曲线靠它当锚点重建强度曲线；
    #: -1 = 老记录没有这个字段（重建曲线时会被跳过）。
    intensity: float = -1.0
    #: A1 原话（截断 + 过滤过敏感信息；只保留最近三轮，更早的滚动丢弃）
    message: str = ""
    #: B3 主因 / 次因规则 key（F1 结构化起因要用）
    primary: str = ""
    secondary: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """原话里可能夹着协议占位标记（<quoted_message …/>、[CQ:reply,…]）。

        新事件在 snippet() 里已经剥过一次，这里再过一遍是为了**老数据**：
        从 emotion_state.json 读回来的旧事件同样要干净 —— 面板的「情绪起因」卡、
        事件流、注入给模型的提示词，取的都是这一条 message，不能让人看见乱码。
        """
        if self.message:
            self.message = strip_message_markup(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": round(self.t, 2),
            "label": self.label,
            "emoji": self.emoji,
            "cause": self.cause,
            "delta": [round(v, 3) for v in self.delta],
            "source": self.source,
            "intensity": round(self.intensity, 4),
            "message": self.message,
            "primary": self.primary,
            "secondary": list(self.secondary),
        }

    @staticmethod
    def from_dict(data: Any) -> "MoodEvent | None":
        if not isinstance(data, dict):
            return None
        raw_delta = data.get("delta") or [0.0, 0.0, 0.0]
        if not isinstance(raw_delta, (list, tuple)):
            raw_delta = [0.0, 0.0, 0.0]
        while len(raw_delta) < 3:
            raw_delta.append(0.0)
        return MoodEvent(
            t=to_float(data.get("t"), 0.0),
            label=str(data.get("label", "") or ""),
            emoji=str(data.get("emoji", "") or ""),
            cause=str(data.get("cause", "") or ""),
            delta=(to_float(raw_delta[0]), to_float(raw_delta[1]), to_float(raw_delta[2])),
            source=str(data.get("source", "user") or "user"),
            intensity=to_float(data.get("intensity", -1), -1.0),
            message=str(data.get("message", "") or ""),
            primary=str(data.get("primary", "") or ""),
            secondary=tuple(str(item) for item in (data.get("secondary") or ())),
        )


@dataclass
class SessionMood:
    """一个会话（群 / 私聊）的情绪状态。"""

    key: str = ""
    emotion: PAD = field(default_factory=PAD)
    mood: PAD = field(default_factory=PAD)
    last_tick: float = 0.0
    last_interaction: float = 0.0
    last_idle_mark: float = 0.0
    last_gap: float = 0.0      # 本条消息到来之前，距离上一次互动隔了多久
    crisis_until: float = 0.0
    emotion_hint: str = ""     # 由具体规则声明的情绪（比几何最近邻更准）
    emotion_hint_until: float = 0.0
    #: 情绪强度（0~1）。**独立状态**：可以被面板滑块直接调，会按配置的策略主动衰减，
    #: 不再只是从 PAD 长度算出来的副产物。-1 = 老存档没存，读的时候才回填。
    intensity: float = -1.0
    #: 冻结时刻 / 冻结那一刻的强度 —— 冻结连强度一起冻住（不衰减、曲线走平）
    frozen_at: float = 0.0
    frozen_intensity: float = -1.0
    #: **心境**的冻结开关（和会话级冻结是两回事，但判定一致：冻住就是完全不动）
    frozen_mood: bool = False
    # ------------------------------------------------------------------
    # 心境（mood）的推进与判定
    # ------------------------------------------------------------------
    #: 心境追赶的目标 = **最近一次互动时的情绪**（A3/A4：只在对话窗口内追）
    mood_target: PAD | None = None
    #: D3 迟滞驻留：当前词 / 起用时间 / 依据，以及正在排队的新词
    mood_label_key: str = ""
    mood_label_since: float = 0.0
    mood_label_reason: str = ""
    mood_label_pending: str = ""
    mood_label_pending_since: float = 0.0
    #: D7 心境起因：换词那一刻「最近一次引起变化的事」
    mood_cause: str = ""
    mood_cause_at: float = 0.0
    #: 每次情绪事件都记一份，供换词时抓取
    mood_last_cause: str = ""
    mood_last_cause_at: float = 0.0
    #: G2 趋势参照点（每 10 分钟记一次 P 值）
    mood_ref_t: float = 0.0
    mood_ref_p: float = 0.0
    #: **超基线的两个计时**（都随状态落盘，重启不丢）：
    #: mood_calm_since —— 心境「连续停在基线（平静）上」的起点，挪出死区就清零；
    #: mood_cold_since —— 超基线**已触发**的时刻，0 = 还没触发。
    mood_calm_since: float = 0.0
    mood_cold_since: float = 0.0
    #: 最近的心境事件（里程碑：换词 / 手动 / 重置 / 明显漂移）
    mood_history: list[MoodShift] = field(default_factory=list)
    users: dict[str, Relation] = field(default_factory=dict)
    history: list[MoodEvent] = field(default_factory=list)

    # -- 时间演化 -----------------------------------------------------------
    def tick(self, now: float, settings: Settings) -> None:
        """按真实流逝的时间做惰性衰减 + 心境的连续推进。"""
        if self.last_tick <= 0:
            self.last_tick = now
            return
        dt = now - self.last_tick
        if dt <= 0:
            return
        # 时钟回拨 / 极端跳变时，不让情绪被"算飞"
        dt = min(dt, 30 * 86400.0)
        self.emotion = self.emotion.decay_toward(settings.baseline, settings.emotion_half_life, dt)
        # 心境冻结（**判定同情绪冻结**）：推进 / 衰减 / 换词 / 漂移全部停住，
        # 只有情绪与强度照常走 —— 冻结的是心境这一层，不是整个会话。
        frozen_mood = self.frozen_mood
        if not frozen_mood:
            # 心境 A3/A4：**按时间连续**朝「最近一次互动时的情绪」靠拢，
            # 而且只在对话窗口内追（窗口过了就只按半衰期回落，不会被情绪拖回基线）
            self.chase_mood(now, settings, dt, check_window=True)
            # 回落的目标是**心境基线**（气质基线 + 配置偏移），不是气质基线本身 ——
            # 曲线页那条虚线仍然画气质基线，动的只是心境这一层的静息点。
            self.mood = self.mood.decay_toward(
                settings.mood_rest, settings.mood_half_life, dt
            )
            # 「在基线（平静）上」的连续计时：进死区就记起点，挪出去就清零。
            # 超基线要数的正是这段连续时间，所以必须放在回落之后按**本 tick 的
            # 结束位置**算 —— 挪回基线的那一 tick 就开始计时。
            if at_mood_rest(self.mood, settings):
                if self.mood_calm_since <= 0:
                    self.mood_calm_since = now
            else:
                self.mood_calm_since = 0.0
            # 超基线：时长满足了才触发，触发之后一路滑向「冷淡」
            self._advance_cold(now, settings, dt)
        # 强度也在**主动衰减**：策略与参数来自情绪配置（emotion.intensity.*）
        value = session_intensity(self)
        self.intensity = decay_intensity(
            value,
            settings.intensity_floor,
            settings.intensity_half_life,
            dt,
            settings.intensity_decay,
        )
        self.last_tick = now
        if frozen_mood:
            return
        # D1/D2/D3：心境标签在这里跟着走（驻留期满才换词）
        self.mood_label(now, settings)
        # G2 趋势参照点（每 10 分钟记一次 P 值）+ 心境漂移事件（同频，最多一条/10 分钟）
        if self.mood_ref_t <= 0 or now - self.mood_ref_t >= 600.0:
            self.mood_ref_t = now
            self.mood_ref_p = self.mood.p
            self._maybe_record_mood_drift(now, settings)

    # -- 心境推进与判定 ------------------------------------------------------
    def chase_mood(
        self,
        now: float,
        settings: Settings,
        dt: float,
        check_window: bool = True,
    ) -> None:
        """**心境的连续推进**（A3/A4）。

        心境朝「最近一次互动时的情绪」(mood_target) 连续靠拢，速率 =
        `mood_coupling × (0.5 + 强度)` 每分钟 —— 强烈度的事件才改得动心境。

        `check_window=True`（tick 走这条）时还要看对话窗口：
        最近 mood_live_window 内有互动才追。否则对话一停、情绪在 4 分钟内衰减
        回基线，心境会被它一路拖回基线，mood_half_life 就白设了。
        **心境冻结（frozen_mood）时直接不动** —— 判定与情绪冻结一致。
        """
        if self.frozen_mood:
            return
        if dt <= 0 or self.mood_target is None:
            return
        if check_window and settings.mood_live_window > 0:
            if self.last_interaction > 0 and (now - self.last_interaction) > settings.mood_live_window:
                return
        alpha = clamp(settings.mood_coupling * (0.5 + session_intensity(self)), 0.0, 0.9)
        if alpha <= 0:
            return
        self.mood = self.mood.lerp(self.mood_target, 1.0 - (1.0 - alpha) ** (dt / 60.0))

    def _advance_cold(self, now: float, settings: Settings, dt: float) -> None:
        """**超基线**：很长时间没人找她，心境从基线（平静）滑向「冷淡」。

        触发必须**同时**满足两件事，缺一不可：

        ① 心境在**基线（平静）**上连续停了 `mood_cold_after_minutes` ——
           `mood_calm_since` 记的就是这段连续时间的起点，中途挪出死区就清零；
        ② 同样长的时间**没有对话**（`last_interaction` 也够久）。
           聊着天还一直「平静」不算冷淡，那只是没话说。

        触发之后状态自己锁住（`mood_cold_since`），之后每次 tick 都往超基线回落；
        只有**有人再说话**（或手动设定/重置心境）才会解锁，之后重新攒时长。
        `mood_cold_after_minutes = 0` 表示关掉这套逻辑。
        """
        # 有人来了：超基线解除，心境交还给对话（时长从头攒）
        if self.mood_cold_since > 0 and self.last_interaction > self.mood_cold_since:
            self.mood_cold_since = 0.0
        window = max(0.0, settings.mood_cold_after_minutes) * 60.0
        if window <= 0:
            return  # 配成 0 = 关掉超基线
        if self.mood_cold_since <= 0:
            if self.mood_calm_since <= 0 or self.last_interaction <= 0:
                return
            calm_for = now - self.mood_calm_since
            idle_for = now - self.last_interaction
            if calm_for < window or idle_for < window:
                return  # 在基线上待得还不够久 / 最近还说过话
            self.mood_cold_since = now
            # 起因换成「很久没说话了」：不写的话，「很冷淡」会挂上上次聊天的起因
            self._capture_mood_cause(CAUSE_NO_CONTACT, self.last_interaction)
            self.mood_cause = CAUSE_NO_CONTACT
            self.mood_cause_at = self.last_interaction
        # 已触发：往超基线回落（半衰期见 COLD_HALF_LIFE —— 触发已经等够了时长，
        # 回落本身要快到「看得见」，但仍是渐进的，不是一下子跳过去）
        self.mood = self.mood.decay_toward(settings.mood_cold, COLD_HALF_LIFE, dt)

    def _capture_mood_cause(self, cause: str, now: float) -> None:
        """每次情绪事件先记一份「最近一次引起变化的事」，换词时抓走当起因。"""
        self.mood_last_cause = cause or ""
        self.mood_last_cause_at = now

    def _adopt_mood_label(self, wanted: MoodLabel, now: float, capture_cause: bool) -> bool:
        """采纳新词；返回是不是**从另一个词换过来的**（换词才记事件）。"""
        previous = self.mood_label_key
        self.mood_label_key = wanted.key
        self.mood_label_since = now
        self.mood_label_reason = wanted.reason
        self.mood_label_pending = ""
        self.mood_label_pending_since = 0.0
        if capture_cause and wanted.key != "calm":
            self.mood_cause = self.mood_last_cause
            self.mood_cause_at = self.mood_last_cause_at
        return bool(previous and previous != wanted.key)

    def _held_mood_label(self, wanted: MoodLabel) -> MoodLabel:
        """还不能换词时：沿用旧词，但幅度/副词/分数照最新算。"""
        proto = PROTOTYPE_BY_KEY.get(self.mood_label_key) or PROTOTYPE_BY_KEY["calm"]
        return MoodLabel(
            key=self.mood_label_key,
            name=proto.name,
            emoji=proto.emoji,
            degree=wanted.degree,
            reason=self.mood_label_reason,
            magnitude=wanted.magnitude,
            score=wanted.score,
            since=self.mood_label_since,
        )

    def mood_label(self, now: float, settings: Settings) -> MoodLabel:
        """**D1/D2/D3/D7 的执行处**：读心境标签，顺手推进它的状态。

        - 第一次判定：直接认（不驻留，面板一打开就有词）；
        - 平静 → 有名有姓的心境：只要**跨过入口阈值**（死区 ×1.25）就认；
        - 有名有姓 → 平静：立即（死区本身就稳）；
        - **词与词之间**换：必须连续驻留 `mood_label_hold` 秒，期间沿用旧词
          —— 那才是来回抖的高发区。
        """
        if self.frozen_mood and self.mood_label_key:
            # 心境冻结：标签也一起冻住（不换词、不换依据、不记新事件），
            # 照常返回当前这个词，幅度/分数仍按当下算（面板显示不会失真）。
            return replace(
                self._held_mood_label(evaluate_mood_label(self.mood, settings.mood_rest)),
                since=self.mood_label_since,
            )
        wanted = evaluate_mood_label(self.mood, settings.mood_rest)
        current = self.mood_label_key

        def adopt(wanted_label: MoodLabel, capture: bool) -> MoodLabel:
            # 换词 → 记一条「最近心境事件」（首次判定不算换词，不记）
            if self._adopt_mood_label(wanted_label, now, capture):
                self.record_mood_event(
                    now, settings, self.mood_cause or wanted_label.reason,
                    "label", label=wanted_label,
                )
            return replace(wanted_label, since=self.mood_label_since)

        if not current:
            return adopt(wanted, True)
        if wanted.key == current:
            # 同一个词：依据与副词可以实时刷新，驻留计时清空
            self.mood_label_reason = wanted.reason
            self.mood_label_pending = ""
            self.mood_label_pending_since = 0.0
            return replace(wanted, since=self.mood_label_since)
        held = self._held_mood_label(wanted)
        if wanted.key == "calm":
            return adopt(wanted, False)
        if wanted.magnitude < ENTRY_MAGNITUDE:
            return held  # 没跨过入口阈值：维持原词
        if current == "calm":
            # 从平静进入一个有名有姓的心境：跨过入口阈值就认，
            # 不用等驻留 —— 聊着聊着还一直显示「平静」才是最怪的。
            # 驻留给「词与词之间」用（那才是来回抖的地方）。
            return adopt(wanted, True)
        if self.mood_label_pending != wanted.key:
            self.mood_label_pending = wanted.key
            self.mood_label_pending_since = now
            return held
        if now - self.mood_label_pending_since < max(0.0, settings.mood_label_hold):
            return held  # 驻留期还没满
        return adopt(wanted, True)

    def mood_trend(self) -> str:
        """**G2**：相对 10 分钟前，心境在变好(↑)、稳定(→)还是变差(↓)。"""
        if self.mood_ref_t <= 0:
            return "flat"
        delta = self.mood.p - self.mood_ref_p
        if delta > 0.03:
            return "up"
        if delta < -0.03:
            return "down"
        return "flat"
    def record_mood_event(
        self,
        now: float,
        settings: Settings,
        cause: str,
        source: str,
        label: MoodLabel | None = None,
    ) -> MoodShift | None:
        """记一条**心境**事件（手动/重置/换词都走这里）。

        相邻两条要是「同一个词 + 基线偏移几乎没变 + 同来源」，就不重复记 ——
        面板上的列表不会被刷屏。
        """
        current = label if label is not None else self.mood_label(now, settings)
        offset = self.mood - PAD.from_any(settings.mood_rest)
        previous = self.mood_history[-1] if self.mood_history else None
        if (
            previous is not None
            and previous.key == current.key
            and previous.source == source
            and (offset - PAD.from_any(previous.offset)).magnitude() < 0.05
        ):
            return None
        shift = MoodShift(
            t=now,
            key=current.key,
            label=current.text,
            emoji=current.emoji,
            cause=cause or current.reason,
            offset=(offset.p, offset.a, offset.d),
            source=source,
        )
        self.mood_history.append(shift)
        if len(self.mood_history) > 40:
            del self.mood_history[:-40]
        return shift

    def _maybe_record_mood_drift(self, now: float, settings: Settings) -> None:
        """每 10 分钟（跟着趋势参照点走）看一眼：心境挪够了就记一条「漂移」。"""
        offset = self.mood - PAD.from_any(settings.mood_rest)
        previous = self.mood_history[-1] if self.mood_history else None
        reference = PAD.from_any(previous.offset) if previous is not None else PAD()
        if (offset - reference).magnitude() < 0.10:
            return
        label = self.mood_label(now, settings)
        cause = self.mood_cause or self.mood_label_reason or label.reason
        self.record_mood_event(now, settings, cause, "drift", label=label)

    # -- 序列化 -------------------------------------------------------------
    def to_dict(self, history_size: int) -> dict[str, Any]:
        return {
            "emotion": self.emotion.to_dict(),
            "mood": self.mood.to_dict(),
            "last_tick": round(self.last_tick, 2),
            "last_interaction": round(self.last_interaction, 2),
            "last_idle_mark": round(self.last_idle_mark, 2),
            "last_gap": round(self.last_gap, 2),
            "crisis_until": round(self.crisis_until, 2),
            "emotion_hint": self.emotion_hint,
            "emotion_hint_until": round(self.emotion_hint_until, 2),
            "intensity": round(session_intensity(self), 4),
            "frozen_at": round(self.frozen_at, 2),
            "frozen_intensity": round(self.frozen_intensity, 4),
            "frozen_mood": bool(self.frozen_mood),
            "mood_target": (
                self.mood_target.to_dict() if self.mood_target is not None else None
            ),
            "mood_label_key": self.mood_label_key,
            "mood_label_since": round(self.mood_label_since, 2),
            "mood_label_reason": self.mood_label_reason,
            "mood_label_pending": self.mood_label_pending,
            "mood_label_pending_since": round(self.mood_label_pending_since, 2),
            "mood_cause": self.mood_cause,
            "mood_cause_at": round(self.mood_cause_at, 2),
            "mood_last_cause": self.mood_last_cause,
            "mood_last_cause_at": round(self.mood_last_cause_at, 2),
            "mood_ref_t": round(self.mood_ref_t, 2),
            "mood_ref_p": round(self.mood_ref_p, 4),
            "mood_calm_since": round(self.mood_calm_since, 2),
            "mood_cold_since": round(self.mood_cold_since, 2),
            "mood_history": [shift.to_dict() for shift in self.mood_history[-40:]],
            "users": {uid: rel.to_dict() for uid, rel in self.users.items()},
            "history": [e.to_dict() for e in self.history[-history_size:]],
        }

    @staticmethod
    def from_dict(key: str, data: Any, settings: Settings) -> "SessionMood":
        if not isinstance(data, dict):
            data = {}
        session = SessionMood(
            key=key,
            emotion=PAD.from_any(data.get("emotion")),
            mood=PAD.from_any(data.get("mood")),
            last_tick=to_float(data.get("last_tick"), 0.0),
            last_interaction=to_float(data.get("last_interaction"), 0.0),
            last_idle_mark=to_float(data.get("last_idle_mark"), 0.0),
            last_gap=to_float(data.get("last_gap"), 0.0),
            crisis_until=to_float(data.get("crisis_until"), 0.0),
            emotion_hint=str(data.get("emotion_hint", "") or ""),
            emotion_hint_until=to_float(data.get("emotion_hint_until"), 0.0),
        )
        # 强度：老存档没有这个字段时按 PAD 长度回填（session_intensity 会兜）
        raw_intensity = data.get("intensity", -1)
        try:
            session.intensity = float(raw_intensity)
        except (TypeError, ValueError):
            session.intensity = -1.0
        session.frozen_at = to_float(data.get("frozen_at"), 0.0)
        try:
            session.frozen_intensity = float(data.get("frozen_intensity", -1))
        except (TypeError, ValueError):
            session.frozen_intensity = -1.0
        session.frozen_mood = bool(data.get("frozen_mood", False))
        # 心境推进/判定的状态；老存档没有就按默认回填
        raw_target = data.get("mood_target")
        session.mood_target = (
            PAD.from_any(raw_target) if isinstance(raw_target, dict)
            else PAD.from_any(settings.mood_rest)
        )
        session.mood_label_key = str(data.get("mood_label_key", "") or "")
        session.mood_label_since = to_float(data.get("mood_label_since"), 0.0)
        session.mood_label_reason = str(data.get("mood_label_reason", "") or "")
        session.mood_label_pending = str(data.get("mood_label_pending", "") or "")
        session.mood_label_pending_since = to_float(
            data.get("mood_label_pending_since"), 0.0
        )
        session.mood_cause = str(data.get("mood_cause", "") or "")
        session.mood_cause_at = to_float(data.get("mood_cause_at"), 0.0)
        session.mood_last_cause = str(data.get("mood_last_cause", "") or "")
        session.mood_last_cause_at = to_float(data.get("mood_last_cause_at"), 0.0)
        session.mood_ref_t = to_float(data.get("mood_ref_t"), 0.0)
        session.mood_ref_p = to_float(data.get("mood_ref_p"), 0.0)
        # 超基线的两个计时：老存档没有 → 0（= 没在基线上待过、也没触发过）
        session.mood_calm_since = to_float(data.get("mood_calm_since"), 0.0)
        session.mood_cold_since = to_float(data.get("mood_cold_since"), 0.0)
        raw_shifts = data.get("mood_history")
        if isinstance(raw_shifts, list):
            for item in raw_shifts[-40:]:
                shift = MoodShift.from_dict(item)
                if shift is not None:
                    session.mood_history.append(shift)
        raw_users = data.get("users")
        if isinstance(raw_users, dict):
            for uid, payload in raw_users.items():
                relation = Relation.from_dict(payload)
                relation.uid = str(uid)
                session.users[str(uid)] = relation
        raw_history = data.get("history")
        if isinstance(raw_history, list):
            for item in raw_history:
                event = MoodEvent.from_dict(item)
                if event is not None:
                    session.history.append(event)
        session.history = session.history[-settings.history_size :]
        return session


# ---------------------------------------------------------------------------
# 快照（渲染层的输入）
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    key: str
    taken_at: float
    emotion: PAD
    mood: PAD
    baseline: PAD
    proto: Prototype       # 对外的情绪标签（优先采用规则声明的结果）
    raw_proto: Prototype   # 纯由 PAD 几何最近邻算出的结果（调试用）
    #: 情绪强度（0~1）—— 会话里那个**可调节的独立状态**，不是从 PAD 推出来的
    intensity: float
    level: int
    level_name: str
    mood_proto: Prototype
    #: F1（R-A 全改）：起因是一条记录，不再是裸字符串
    cause: CauseInfo
    relation: Relation | None
    idle_seconds: float   # 当前距上次互动的实时时长（面板展示用）
    gap_seconds: float    # 这条消息到来之前隔了多久（注入给模型用）
    history: list[MoodEvent]
    crisis: bool
    #: 调试用：只看 PAD 长度能算出来的「旧口径」强度（对照看）
    pad_intensity: float = 0.0
    #: 心境的新标签（D1 主导维度 + D2 程度副词，含依据与起用时间）
    mood_label: MoodLabel = field(default_factory=MoodLabel)
    #: D7：心境的起因（换词那一刻最近一次引起变化的事）
    mood_cause: str = ""
    mood_cause_at: float = 0.0
    #: G2：趋势 up / flat / down
    mood_trend: str = "flat"
    #: E2：心境起因的人话版（含「延续自…」判定）
    mood_cause_display: str = ""
    #: 超基线触发时刻（0 = 没触发）：调试详情里说清「她是怎么变冷淡的」
    mood_cold_since: float = 0.0
    #: **心境基线**：气质基线 + 配置的心境基线偏移（心境的静息点与判定参照）。
    #: baseline 字段仍然是气质基线 —— 曲线那条虚线、情绪的回落目标都用它，两者别混。
    mood_baseline: PAD | None = None

# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class EmotionEngine:
    """把所有会话的情绪状态管起来。"""

    def __init__(
        self,
        settings: Settings,
        data_dir: Path,
        logger: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.logger = logger
        self.clock = clock
        self.preset: Preset = PRESETS.get(settings.preset_key) or PRESETS[DEFAULT_PRESET]
        self.appraiser = self._build_appraiser(settings)
        self.store = JsonStore(Path(data_dir) / "emotion_state.json")
        self._sessions: dict[str, SessionMood] = {}
        #: 冻结的会话：情绪**完全停住** —— 聊天不影响它，随时间的衰减也停住。
        #: 面板/指令直接改这个集合（main 里 `self._frozen` 就是它的别名）。
        self.frozen: set[str] = set()
        #: 上次落盘时的冻结名单 —— 靠它发现「有人直接改了集合」，好把冻结状态也存下来。
        self._frozen_saved: tuple[str, ...] = ()
        self._dirty = False
        self._loaded = False

    # -- 构建 ---------------------------------------------------------------
    def _build_appraiser(self, settings: Settings) -> Appraiser:
        from .appraisal import CORE_RULES

        rules: list[Rule] = list(CORE_RULES) + list(self.preset.extra_rules)
        for line in self.custom_rule_lines():
            rule = parse_custom_rule(line)
            if rule is not None:
                rules.append(rule)
            elif self.logger is not None:
                self.logger.warning(f"[ai_emotion] 自定义规则格式不对，已跳过：{line!r}")
        return Appraiser(
            rules=tuple(rules),
            max_delta=settings.max_delta,
            lexicon_enabled=settings.lexicon_enabled,
            lexicon_gain=settings.lexicon_gain,
        )

    def custom_rule_lines(self) -> list[str]:
        raw = getattr(self, "_custom_rules", None)
        if raw is None:
            return []
        if isinstance(raw, str):
            raw = [line for line in raw.splitlines() if line.strip()]
        if not isinstance(raw, (list, tuple)):
            return []
        return [str(line).strip() for line in raw if str(line).strip()]

    def apply_settings(self, settings: Settings) -> None:
        """配置热更新：换掉基线与规则评估器，已有的情绪状态原样保留。"""
        self.settings = settings
        self.appraiser = self._build_appraiser(settings)

    def set_custom_rules(self, lines: Any) -> None:
        self._custom_rules = lines
        self.appraiser = self._build_appraiser(self.settings)

    # -- 载入 / 落盘 --------------------------------------------------------
    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        raw = self.store.load()
        payload = raw.get("sessions")
        now = self.clock()
        if isinstance(payload, dict):
            for key, value in payload.items():
                try:
                    self._sessions[str(key)] = SessionMood.from_dict(str(key), value, self.settings)
                except Exception:  # 单条损坏不影响其它会话
                    continue
        for session in self._sessions.values():
            if session.last_tick <= 0:
                session.last_tick = now
            if session.last_interaction <= 0:
                session.last_interaction = now
        # 冻结名单也一起恢复（重启后不该自己解冻）
        raw_frozen = raw.get("frozen")
        if isinstance(raw_frozen, (list, tuple, set)):
            self.frozen.clear()
            self.frozen.update(str(item) for item in raw_frozen if str(item))
        self._frozen_saved = tuple(sorted(self.frozen))
        if self.logger is not None:
            self.logger.info(
                f"[ai_emotion] 已载入 {len(self._sessions)} 个会话的情绪状态"
                + (f"（冻结 {len(self.frozen)} 个）" if self.frozen else "")
            )

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        now = self.clock() if now is None else now
        self.prune(now)
        return {
            "version": STATE_VERSION,
            "saved_at": round(now, 2),
            "preset": self.settings.preset_key,
            "frozen": sorted(self.frozen),
            "sessions": {
                key: session.to_dict(self.settings.history_size)
                for key, session in self._sessions.items()
            },
        }

    def flush(self, now: float | None = None) -> bool:
        # 冻结名单可能被外面直接 add/discard/clear 改过（没走 _dirty），
        # 所以拿「当前名单 vs 上次落盘的名单」一起判断要不要写。
        frozen_now = tuple(sorted(self.frozen))
        if not self._dirty and self._loaded and frozen_now == self._frozen_saved:
            return True
        ok = self.store.save(self.to_dict(now))
        if ok:
            self._dirty = False
            self._frozen_saved = frozen_now
        elif self.logger is not None:
            self.logger.warning("[ai_emotion] 情绪状态写入失败，稍后会重试")
        return ok

    # -- 冻结 / 解冻 ---------------------------------------------------------
    def freeze(self, key: str) -> bool:
        """冻结一个会话：情绪与**强度**从此完全不动（衰减也停）。已冻结返回 False。"""
        key = str(key or "")
        if not key or key in self.frozen:
            return False
        self.frozen.add(key)
        session = self._sessions.get(key)
        if session is not None:
            # 记下冻结这一刻的强度：曲线上从这里开始走平（见 panel.build_intensity_curve）
            session.frozen_at = self.clock()
            session.frozen_intensity = session_intensity(session)
        self._dirty = True
        return True

    def unfreeze(self, key: str) -> bool:
        key = str(key or "")
        if key not in self.frozen:
            return False
        self.frozen.discard(key)
        session = self._sessions.get(key)
        if session is not None:
            session.frozen_at = 0.0
            session.frozen_intensity = -1.0
        self._dirty = True
        return True

    def unfreeze_all(self) -> int:
        count = len(self.frozen)
        mood_frozen = sum(1 for s in self._sessions.values() if s.frozen_mood)
        if count or mood_frozen:
            self.frozen.clear()
            for session in self._sessions.values():
                session.frozen_at = 0.0
                session.frozen_intensity = -1.0
                session.frozen_mood = False
            self._dirty = True
        return max(count, mood_frozen)

    # -- 心境冻结（判定同情绪冻结：冻住就是「完全不动」） --------------------
    def freeze_mood(self, key: str) -> bool:
        """冻结**心境**这一层：推进 / 衰减 / 换词 / 漂移全部停住。

        与会话级冻结的区别：情绪与强度照常走；面板上的手动调心境仍然生效
        （和情绪冻结时手动设定照常生效是同一套判定）。已冻结返回 False。
        """
        key = str(key or "")
        if not key:
            return False
        session = self._sessions.get(key)
        if session is None or session.frozen_mood:
            return False
        session.frozen_mood = True
        self._dirty = True
        return True

    def unfreeze_mood(self, key: str) -> bool:
        key = str(key or "")
        session = self._sessions.get(key)
        if session is None or not session.frozen_mood:
            return False
        session.frozen_mood = False
        # 解冻后心境从这一刻重新计时：把参照点拨到现在，冻结期不补算漂移
        session.mood_ref_t = self.clock()
        session.mood_ref_p = session.mood.p
        self._dirty = True
        return True

    def mood_frozen(self, key: str) -> bool:
        """心境现在到底冻没冻 —— 会话级冻结时心境同样不动，对外按「冻着」显示。"""
        key = str(key or "")
        session = self._sessions.get(key)
        return bool(session and session.frozen_mood) or key in self.frozen

    # -- 曲线上的点编辑（和强度锚点联动） ----------------------------------
    def edit_intensity_marks(
        self,
        session: SessionMood,
        t: float,
        tolerance: float = 0.0,
        mode: str = "update",
        value: float | None = None,
    ) -> int:
        """曲线上「删除附近的点 / 改成当前值 / 插点」也要动**强度的锚点**。

        强度曲线的锚点是情绪事件（MoodEvent.intensity），和 PAD 采样点是两套存储 ——
        只动采样点的话，面板上怎么点，强度那条线都不会变（报过的 bug）。

        - delete：作废附近的强度锚点（置 -1，重建曲线时跳过），
          事件本身留在情绪事件流里 —— 它记录的是「当时情绪确实动过」；
        - update：把附近的强度锚点改成给定值（默认当前强度）；
        - insert：补一个锚点，事件流里会多一条「在曲线上手动插点」。
        返回受影响的事件数。
        """
        mode = str(mode or "update").strip().lower()
        if mode == "insert":
            self._record(session, t, "在曲线上手动插点", PAD(), "manual", force=True)
            # 插的点可能比最后一事件更早：按时间排回去，事件流才不会乱序
            session.history.sort(key=lambda event: event.t)
            self._dirty = True
            return 1
        window = max(0.0, to_float(tolerance, 0.0))
        hits = [
            event for event in session.history
            if event.intensity >= 0 and abs(to_float(event.t, 0.0) - t) <= window
        ]
        if not hits:
            return 0
        if mode == "delete":
            for event in hits:
                event.intensity = -1.0
        else:
            target = clamp(to_float(value, session_intensity(session)), 0.0, 1.0)
            for event in hits:
                event.intensity = target
        self._dirty = True
        return len(hits)

    def prune(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        ttl = self.settings.session_ttl_days * 86400.0
        expired = [
            key
            for key, session in self._sessions.items()
            if now - max(session.last_interaction, session.last_tick) > ttl
        ]
        for key in expired:
            self._sessions.pop(key, None)
        overflow = len(self._sessions) - self.settings.max_sessions
        if overflow > 0:
            ordered = sorted(self._sessions.items(), key=lambda kv: kv[1].last_interaction)
            for key, _ in ordered[:overflow]:
                self._sessions.pop(key, None)

    # -- 会话与关系 ---------------------------------------------------------
    def _advance(self, session: SessionMood, now: float) -> None:
        """按真实时间推进一个会话。

        **冻结的会话只拨时钟、不衰减** —— 这就是「冻结不改变情绪」：
        情绪数值原地不动；等解冻之后，衰减也只从解冻这一刻开始算，
        冻结期间的那段时间不会被补扣。
        """
        if session.key in self.frozen:
            # 只把时钟往前拨（面板读取可能带来更早的时间戳，不能往回退）——
            # 这样解冻之后，衰减只从解冻这一刻开始算，冻结期不补扣。
            if now > session.last_tick:
                session.last_tick = now
        else:
            session.tick(now, self.settings)

    def resolve(self, key: str, now: float | None = None) -> SessionMood:
        now = self.clock() if now is None else now
        session = self._sessions.get(key)
        if session is None:
            session = SessionMood(
                key=key,
                emotion=PAD.from_any(self.settings.baseline),
                # 心境从**心境基线**起步（气质基线 + 配置偏移）：一上来就在「平静」上，
                # 超基线的连续计时也从这一刻开始攒
                mood=PAD.from_any(self.settings.mood_rest),
                mood_target=PAD.from_any(self.settings.mood_rest),
                mood_calm_since=now,
                intensity=clamp(self.settings.intensity_default, 0.0, 1.0),
                last_tick=now,
                last_interaction=now,
            )
            self._sessions[key] = session
        self._advance(session, now)
        return session

    def forget_user(self, uid: str) -> int:
        """把某个人的关系数据从所有会话里抹掉，返回抹掉几条。"""
        target = str(uid or "")
        if not target:
            return 0
        removed = 0
        for session in self._sessions.values():
            if target in session.users:
                session.users.pop(target, None)
                removed += 1
        if removed:
            self._dirty = True
        return removed

    def all_relations(self) -> list[tuple[str, Relation]]:
        """(会话, 关系) 的全量快照 —— 面板靠它列出"聊过的人"。"""
        out: list[tuple[str, Relation]] = []
        for key, session in self._sessions.items():
            for relation in session.users.values():
                out.append((key, relation))
        return out

    def relation_of(
        self,
        session: SessionMood,
        uid: str,
        name: str = "",
        now: float | None = None,
        platform: str = "",
        platform_id: str = "",
    ) -> Relation:
        now = self.clock() if now is None else now
        uid = str(uid or "unknown")
        relation = session.users.get(uid)
        if relation is None:
            relation = Relation(uid=uid, name=name or "", last_seen=now)
            session.users[uid] = relation
        if name:
            relation.name = name
        return relation

    def touch(
        self,
        session: SessionMood,
        relation: Relation,
        now: float | None = None,
    ) -> None:
        now = self.clock() if now is None else now
        relation.touch(now)
        session.last_interaction = now
        session.last_idle_mark = now
        self._dirty = True

    # -- 情绪施加 -----------------------------------------------------------
    def apply_stimulus(
        self,
        session: SessionMood,
        stimulus: Stimulus,
        relation: Relation | None = None,
        now: float | None = None,
    ) -> None:
        now = self.clock() if now is None else now
        if session.key in self.frozen:
            return  # 冻结中：聊天、空闲、危机都不许再动情绪
        before = session.emotion
        session.emotion = (session.emotion + stimulus.delta).limit(1.0)
        # 强度跟着冲击走，但**增益按当前强度边际递减**（F2）：
        # 否则三句夸奖就把 100% 打满，强度从此不再是有效信号。
        strength = session_intensity(session)
        session.intensity = clamp(
            strength
            + self.settings.intensity_gain
            * min(1.0, stimulus.delta.magnitude())
            * (1.0 - strength),
            0.0,
            1.0,
        )
        # 心境：目标改成「事件之后的情绪」，再按 1 分钟的连续推进先推一把
        #（真正的连续推进在 tick 里做，见 chase_mood）
        session.mood_target = session.emotion
        session.chase_mood(now, self.settings, 60.0, check_window=False)
        session._capture_mood_cause(stimulus.cause, now)
        if stimulus.source == "crisis":
            session.crisis_until = now + self.settings.crisis_hold
        if stimulus.emotion or stimulus.source != "idle":
            # 标签跟着"最近一次明确知道是什么情绪的事件"走
            session.emotion_hint = stimulus.emotion
            session.emotion_hint_until = now + self.settings.hint_hold
        if stimulus.source != "idle":
            session.last_interaction = now
            session.last_idle_mark = now
        self._record(
            session,
            now,
            stimulus.cause,
            session.emotion - before,
            stimulus.source,
            message=stimulus.message,
            primary=stimulus.primary,
            secondary=stimulus.secondary,
        )
        self._dirty = True

    def apply_self_expression(
        self,
        session: SessionMood,
        proto_key: str,
        strength: float = 1.0,
        now: float | None = None,
        source: str = "self",
    ) -> bool:
        """把她自己说出口的情绪，轻轻反馈回状态（形成闭环）。"""
        if session.key in self.frozen:
            return False
        proto = PROTOTYPE_BY_KEY.get(proto_key)
        if proto is None:
            return False
        now = self.clock() if now is None else now
        gain = clamp(self.settings.self_feedback_gain * clamp(strength, 0.0, 1.0), 0.0, 0.6)
        if gain <= 0:
            return False
        session.emotion_hint = proto_key
        session.emotion_hint_until = now + self.settings.hint_hold
        before = session.emotion
        session.emotion = session.emotion.lerp(PAD.from_any(proto.pad), gain)
        # 心境冻结时**任何路径都不许动心境**（含这些直接 lerp 的旁路）
        if not session.frozen_mood:
            session.mood = session.mood.lerp(session.emotion, self.settings.mood_coupling * 0.5)
        session.intensity = clamp(session_intensity(session) + gain * 0.5, 0.0, 1.0)
        session.mood_target = session.emotion
        session.chase_mood(now, self.settings, 60.0, check_window=False)
        # G1：自回读不再是「原因」，只是次要点缀（D2 的来源标签会另标「她的语气」）；
        # E2：它也不再当成心境起因 —— 心境起因只认**对方说的话**。
        self._record(
            session,
            now,
            f"顺着当时的心情（{proto.name}）",
            session.emotion - before,
            source,
        )
        self._dirty = True
        return True

    def apply_idle(
        self,
        session: SessionMood,
        now: float | None = None,
    ) -> bool:
        """长时间没人说话：有点空落落的，还带一点点被晾着的不爽。"""
        if not self.settings.idle_enabled:
            return False
        if session.key in self.frozen:
            return False
        now = self.clock() if now is None else now
        if session.last_idle_mark >= session.last_interaction:
            return False  # 这一轮空闲已经结算过了
        idle = now - session.last_interaction
        if idle < self.settings.idle_threshold:
            return False
        span = max(1.0, self.settings.idle_threshold)
        ratio = clamp((idle - self.settings.idle_threshold) / span, 0.0, 1.0)
        if ratio <= 0.01:
            return False
        delta = PAD.of(-0.34, -0.16, -0.26) * (self.settings.idle_max_effect * ratio)
        session.emotion = (session.emotion + delta).limit(1.0)
        if not session.frozen_mood:
            session.mood = session.mood.lerp(session.emotion, self.settings.mood_coupling)
        session.intensity = clamp(
            session_intensity(session)
            + self.settings.intensity_gain * 0.35 * ratio,
            0.0,
            1.0,
        )
        session.mood_target = session.emotion
        session.chase_mood(now, self.settings, 60.0, check_window=False)
        session.last_idle_mark = now
        self._record(session, now, "好久没人说话了", delta, "idle")
        self._dirty = True
        return True

    def reset(self, session: SessionMood, now: float | None = None, keep_relations: bool = True) -> None:
        now = self.clock() if now is None else now
        session.emotion = PAD.from_any(self.settings.baseline)
        session.mood = PAD.from_any(self.settings.mood_rest)
        session.mood_target = PAD.from_any(self.settings.mood_rest)
        # 重置 = 回到基线（平静）：超基线解除，连续计时从头开始
        session.mood_calm_since = now
        session.mood_cold_since = 0.0
        session.mood_label_key = ""
        session.mood_label_since = 0.0
        session.mood_label_reason = ""
        session.mood_label_pending = ""
        session.mood_label_pending_since = 0.0
        session.mood_cause = ""
        session.mood_cause_at = 0.0
        session.mood_last_cause = ""
        session.mood_last_cause_at = 0.0
        session.mood_ref_t = 0.0
        session.mood_ref_p = 0.0
        session.intensity = clamp(self.settings.intensity_default, 0.0, 1.0)
        session.crisis_until = 0.0
        session.emotion_hint = ""
        session.emotion_hint_until = 0.0
        session.last_tick = now
        session.last_interaction = now
        session.last_idle_mark = now
        # **不清历史**：事件史是强度曲线与情绪事件流的锚点 ——
        # 清了整条强度线会塌成一条基准线（就是报过的那个 bug）。
        # 「清空曲线」才是专门清观测的按钮；这里只补一条重置事件当新锚点。
        self._record(
            session, now, "被重置情绪", PAD(), "manual", force=True, primary="reset"
        )
        if not keep_relations:
            session.users.clear()
        self._dirty = True

    def set_emotion(self, session: SessionMood, pad: PAD, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        # 面板/指令的手动设定在冻结时**照样生效**，只是不许顺带衰减
        self._advance(session, now)
        before = session.emotion
        # 手动设定是明确的用户意图，只做逐维夹取，不按向量长度缩放。
        # （自动累积才需要 limit()，那是为了防止多条规则叠出夸张数值。）
        session.emotion = PAD.from_any(pad).clamped()
        # 手动改情绪时顺带推一把心境 —— 但心境冻结了就不许推
        if not session.frozen_mood:
            session.mood = session.mood.lerp(session.emotion, self.settings.mood_coupling)
        session.mood_target = session.emotion
        session.chase_mood(now, self.settings, 60.0, check_window=False)
        self._record(session, now, "被手动设定", session.emotion - before, "manual")
        self._dirty = True

    def set_mood(self, session: SessionMood, pad: PAD, now: float | None = None) -> None:
        """**主动调节心境**（面板「调节心境」的设为此刻心境 / 描述词）。

        与手动设定情绪一样，冻结时照样生效；为了让它站得住还要做三件事：
        - 目标一起改：否则对话窗口内的连续推进会把它拽回「最近一次互动的情绪」；
        - 清掉标签状态：新词**立刻**生效，不等 300 秒驻留；
        - 记一条心境事件（面板「最近心境事件」要显示）。

        **注意**：手动设定**不算**心境起因（E2 说过「不算」），所以这里只把事件记成
        manual，不碰 mood_cause。
        """
        now = self.clock() if now is None else now
        self._advance(session, now)
        session.mood = PAD.from_any(pad).clamped()
        session.mood_target = session.mood
        # 手动设定是明确的用户意图：先解除超基线（不然下一 tick 又被拽回冷淡），
        # 「在基线上」的连续计时由下一次 tick 按新位置重新起算
        session.mood_cold_since = 0.0
        session.mood_calm_since = 0.0
        session.mood_label_key = ""
        session.mood_label_pending = ""
        session.mood_label_pending_since = 0.0
        label = session.mood_label(now, self.settings)
        session.record_mood_event(now, self.settings, "心境被手动设定", "manual", label=label)
        self._dirty = True

    def reset_mood(self, session: SessionMood, now: float | None = None) -> None:
        """只把心境归零（情绪与强度不动）。"""
        now = self.clock() if now is None else now
        self._advance(session, now)
        session.mood = PAD.from_any(self.settings.mood_rest)
        session.mood_target = session.mood
        # 手动归零同样解除超基线（否则下一 tick 又被拽回冷淡）
        session.mood_calm_since = now
        session.mood_cold_since = 0.0
        session.mood_label_key = ""
        session.mood_label_reason = ""
        session.mood_label_pending = ""
        session.mood_label_pending_since = 0.0
        session.mood_cause = ""
        session.mood_cause_at = 0.0
        label = session.mood_label(now, self.settings)
        session.record_mood_event(now, self.settings, "心境被重置", "reset", label=label)
        self._dirty = True

    def set_intensity(
        self,
        session: SessionMood,
        value: float,
        now: float | None = None,
        cause: str = "强度被手动调节",
    ) -> float:
        """**主动**设定情绪强度（面板滑块 / 指令走这里）。

        与情绪一样：冻结时手动设定照样生效，只是不许顺带衰减。
        返回设定后的强度（0~1）。
        """
        now = self.clock() if now is None else now
        self._advance(session, now)
        before = session_intensity(session)
        target = clamp(to_float(value, before), 0.0, 1.0)
        session.intensity = target
        self._record(session, now, cause, PAD(), "manual", force=target != before)
        self._dirty = True
        return target

    # -- 内部 ---------------------------------------------------------------
    def _record(
        self,
        session: SessionMood,
        now: float,
        cause: str,
        delta: PAD,
        source: str,
        force: bool = False,
        message: str = "",
        primary: str = "",
        secondary: Any = (),
    ) -> None:
        """记一条情绪事件。强度、原话（A1）与主/次因（B3）也一起记下来。"""
        if delta.magnitude() < 0.01 and not force:
            return
        snippet = str(message or "")
        if snippet:
            # 隐私第一道闸：敏感信息（证件号/手机号/住址/学校…）一律不落库。
            # 拒绝名单的人根本进不到情绪管线（main 里先 return 了），这里只管内容。
            try:
                from ..memory.privacy import detect_sensitive

                if detect_sensitive(snippet):
                    snippet = ""
            except Exception:  # noqa: BLE001 - 查不到就当敏感，宁可不存
                snippet = ""
        proto = None
        if session.emotion_hint and now < session.emotion_hint_until:
            proto = PROTOTYPE_BY_KEY.get(session.emotion_hint)
        if proto is None:
            proto = classify(session.emotion)
        session.history.append(
            MoodEvent(
                t=now,
                label=proto.name,
                emoji=proto.emoji,
                cause=cause or proto.name,
                delta=(delta.p, delta.a, delta.d),
                source=source,
                intensity=session_intensity(session),
                message=snippet,
                primary=str(primary or ""),
                secondary=tuple(secondary or ()),
            )
        )
        # A1：原话只留**最近三轮**，更早的滚动丢弃（不进面板、不进提示词、不落库）
        seen = 0
        for event in reversed(session.history):
            if not event.message:
                continue
            seen += 1
            if seen > 3:
                event.message = ""
        limit = self.settings.history_size
        if len(session.history) > limit * 2:
            del session.history[:-limit]

    # -- 快照 ---------------------------------------------------------------
    def snapshot(
        self,
        session: SessionMood,
        relation: Relation | None = None,
        now: float | None = None,
    ) -> Snapshot:
        now = self.clock() if now is None else now
        proto = classify(session.emotion)
        raw_proto = proto
        if session.emotion_hint and now < session.emotion_hint_until:
            hinted = PROTOTYPE_BY_KEY.get(session.emotion_hint)
            if hinted is not None:
                proto = hinted
        intensity = session_intensity(session)
        level, level_name = level_of_intensity(intensity)
        mood_label = session.mood_label(now, self.settings)
        last_event = None
        for event in reversed(session.history):
            if event.source in {"user", "crisis"}:
                last_event = event
                break
        cause_info = (
            CauseInfo.from_event(last_event, now) if last_event is not None else CauseInfo()
        )
        return Snapshot(
            key=session.key,
            taken_at=now,
            emotion=session.emotion,
            mood=session.mood,
            baseline=self.settings.baseline,
            proto=proto,
            raw_proto=raw_proto,
            intensity=intensity,
            level=level,
            level_name=level_name,
            mood_proto=classify_mood(session.mood, self.settings.mood_rest),
            mood_baseline=self.settings.mood_rest,
            mood_cold_since=session.mood_cold_since,
            cause=cause_info,
            relation=relation,
            idle_seconds=max(0.0, now - session.last_interaction),
            gap_seconds=max(0.0, session.last_gap),
            history=list(session.history[-self.settings.history_size :]),
            crisis=now < session.crisis_until,
            pad_intensity=intensity_of(session.emotion),
            mood_label=mood_label,
            mood_cause=session.mood_cause,
            mood_cause_at=session.mood_cause_at,
            mood_trend=session.mood_trend(),
            mood_cause_display=format_mood_cause(
                session.mood_cause,
                session.mood_cause_at,
                now,
                self.settings.mood_live_window,
            ),
        )

    # -- 后台维护 -----------------------------------------------------------
    @staticmethod
    def _discrete_mood_state(session: SessionMood) -> tuple[Any, ...]:
        """心境里那些**变了就该落盘**的离散字段（连续的数值漂移不算在内）。"""
        return (
            session.mood_calm_since,
            session.mood_cold_since,
            session.mood_label_key,
            session.mood_cause,
            session.mood_cause_at,
        )

    def tick_all(self, now: float | None = None) -> None:
        """让所有会话按真实流逝的时间推进一次（由后台任务调用）。

        冻结的会话只拨时钟不衰减 —— 见 `_advance()`。
        """
        now = self.clock() if now is None else now
        for session in self._sessions.values():
            # 心境的**离散状态**（在基线上待了多久 / 超基线触发没触发 / 换了词 / 起因）
            # 在后台这一圈里也会变 —— 变了必须标脏，否则维护循环看见 dirty=False
            # 就不落盘，这些状态只活到「下一次有人说话」为止（重启就丢）。
            # 连续的数值漂移不算在内：那部分本来就等下一次交互或退出时一起存。
            before = self._discrete_mood_state(session)
            self._advance(session, now)
            if self._discrete_mood_state(session) != before:
                self._dirty = True
        self.prune(now)

    # 人格预设换装（reconfigure）已随「内置人格」一起删除：
    # 现在只有一套默认情绪参数，配置热更新走 apply_settings()。

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def sessions(self) -> dict[str, SessionMood]:
        """所有已知会话（面板只读用）。"""
        return self._sessions

    def session_keys(self) -> list[str]:
        return list(self._sessions.keys())

    @property
    def session_count(self) -> int:
        return len(self._sessions)

