"""情绪评估（Appraisal）：把一条消息变成情绪冲击。

这是整个插件"有没有活人感"的关键。设计取舍：

- **纯规则、零额外 token**：不额外调用 LLM，情绪反应是即时的、免费的、可解释的。
- **支持自定义规则**：用户可以在配置里追加拿自己圈子里的梗。
- **危机信号优先**：一旦识别到自伤 / 轻生信号，直接短路掉所有普通情绪规则，
  交给 main.py 注入"放下扮演、认真关心"的覆盖指令。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..textfix import strip_message_markup
from .model import PAD, to_float

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Stimulus:
    """一次情绪冲击。"""

    delta: PAD
    cause: str = ""
    key: str = ""
    source: str = "user"  # user | self | idle | crisis
    emotion: str = ""     # 规则明确声明的情绪原型 key（比几何最近邻更准）
    lexicon_hits: tuple = ()   # 如果是词表兜底命中的，这里放命中的词（面板要看）
    #: A1 原话（截断到 24 字；引擎落库前还会再过一次敏感信息闸门）
    message: str = ""
    #: B3 主因 / 次因规则 key —— 起因结构化（F1）时要一起带进事件
    primary: str = ""
    secondary: tuple = ()


@dataclass(frozen=True)
class Rule:
    """一条情绪触发规则。

    只负责"这句话让她怎么了"（情绪冲击 + 情绪标签），
    **不带任何表达基调 / 说话方式** —— 那一层已经整体删除。
    """

    key: str
    pattern: str
    pad: tuple[float, float, float] = (0.0, 0.0, 0.0)
    cause: str = ""
    weight: float = 1.0
    exclude: str = ""
    emotion: str = ""     # 命中时，直接声明这是哪种情绪
    flags: int = re.IGNORECASE


# ---------------------------------------------------------------------------
# 危机信号识别（最高优先级，命中即短路）
# ---------------------------------------------------------------------------

#: 自伤 / 轻生相关信号。宁可多报（最多是语气认真一点），不可漏报。
CRISIS_PATTERNS: tuple[str, ...] = (
    r"自杀|自尽|轻生|跳楼|跳河|割腕|上吊|服毒|烧炭",
    r"自残|自虐|伤害自己|伤害我自己|拿刀划",
    r"不想活(?:了|着|下去)?|活不下去|活够了|活着没(?:有)?(?:意思|意义|必要)|生无可恋",
    r"想死(?!我)|想去死|死了算了|不如死(?:了|掉)|一了百了",
    r"遗书|永别了|最后(?:一条|一次)(?:消息|话)|跟你告个别",
    r"撑不下去|熬不下去|没力气活|解脱了",
)

_CRISIS_RES = tuple(re.compile(p, re.IGNORECASE) for p in CRISIS_PATTERNS)


def detect_crisis(text: str) -> str | None:
    """命中返回触发片段，否则返回 None。"""
    if not text:
        return None
    for regex in _CRISIS_RES:
        match = regex.search(text)
        if match:
            return match.group(0)
    return None


#: 起因里带的原话截断长度（A1）
SNIPPET_LIMIT = 24


def snippet(text: str, limit: int = SNIPPET_LIMIT) -> str:
    """把原话压成一句短引文：压空格 + 截断 + 省略号。

    空文本返回 ""。敏感信息由引擎在落库时再过滤一次（拒绝名单的人根本进不到这里）。
    """
    # 先剥协议占位标记（<quoted_message …/>、[CQ:reply,…]），再压空格、再截断 ——
    # 否则 24 个字全被标记吃掉，面板上只剩一串乱码
    compact = re.sub(r"\s+", " ", strip_message_markup(str(text or ""))).strip()
    if not compact:
        return ""
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "…"


def render_cause(picked: list[str]) -> str:
    """**B3 主因 + 次因**：主因整句，次因收敛成「还顺带」。

    老版本是 `" + ".join(...)` —— 读起来像日志拼串，也分不出哪句才是真正在推动情绪的。
    """
    if not picked:
        return ""
    if len(picked) == 1:
        return picked[0]
    head, rest = picked[0], picked[1:]
    if len(rest) > 2:
        return f"{head}（还顺带：{'、'.join(rest[:2])} 等 {len(rest)} 件事）"
    return f"{head}（还顺带：{'、'.join(rest)}）"


#: 「在问我问题」的信息词（C1：用它把真问题和随口一问分开）
QUESTION_WORDS = (
    "什么|谁|哪|几|多大|多少|为什么|为啥|怎么|如何|是不是|能不能|可不可以"
    "|会不会|要不要|有没有|在哪|什么时候"
)

# ---------------------------------------------------------------------------
# 内置规则集
# ---------------------------------------------------------------------------

CORE_RULES: tuple[Rule, ...] = (
    # ---------------------------- 亲密与善意 ----------------------------
    Rule(
        "praise",
        r"好棒|太棒|真棒|棒棒|好厉害|太厉害|真厉害|厉害|优秀|好强|太强|真强|牛[逼bＢ]|太牛|nb|yyds|绝了|满分|做得好|干得好|好聪明|好贴心|好可爱|可爱|喜欢你这样",
        (0.42, 0.20, 0.20),
        "被夸了",
        weight=1.2,
    ),
    Rule(
        "thanks",
        r"谢谢|感谢|多谢|辛苦了|辛苦啦|麻烦你了|麻烦你|ありがとう|thanks|thank\s*you",
        (0.28, 0.05, 0.05),
        "被道谢",
        emotion="grateful",
    ),
    Rule(
        "love",
        r"喜欢你|爱你|最喜欢你|好想你|想你了|抱抱|要抱抱|亲亲|贴贴|摸摸头|么么|muah|love\s*you",
        (0.58, 0.45, -0.10),
        "被表白/被撒娇",
        weight=1.4,
        emotion="affection",
    ),
    Rule(
        "miss_you",
        r"好久不见|想我了|回来啦|终于来|等你|想见你",
        (0.45, 0.35, 0.05),
        "被惦记着",
    ),
    Rule(
        "comfort",
        r"别难过|抱抱你|安慰你|没事的|我懂你|理解你|开心点|别伤心|有我在",
        (0.44, -0.12, 0.12),
        "被安慰",
        emotion="grateful",
    ),
    Rule(
        "trust",
        r"只告诉你|秘密|悄悄话|别告诉别人|相信我|就跟你一个人说",
        (0.30, 0.18, 0.18),
        "被信任",
        emotion="affection",
    ),
    Rule(
        "gift",
        r"送你|给你买|请你吃|奖励你|给你带",
        (0.36, 0.28, 0.06),
        "被送东西",
    ),
    Rule(
        "apology",
        r"对不起|抱歉|不好意思|是我的错|我错了|原谅我|别生气",
        (0.20, -0.16, 0.14),
        "被道歉",
    ),
    Rule(
        "joke",
        r"哈哈|hhh|233|笑死|笑不活|😂|🤣|嘿嘿|嘻嘻|乐了|绷不住",
        (0.26, 0.30, 0.05),
        "被逗笑",
        emotion="joy",
    ),
    # ---------------------------- 负向刺激 ----------------------------
    Rule(
        "insult",
        r"傻[逼b比]|煞笔|沙比|蠢[货猪驴]|废物|垃圾|滚[吧开远]|闭嘴|烦人|恶心|讨厌你|去死|有病|脑残|智障|弱智|你妈|妈的|妈的死|贱|下贱",
        (-0.78, 0.62, -0.35),
        "被骂了",
        weight=1.6,
        emotion="angry",
    ),
    Rule(
        "reject",
        r"不想理你|别烦我|走开|别说了|不用你管|少管|懒得理|不感兴趣|关你屁事",
        (-0.46, 0.12, -0.30),
        "被冷落",
        emotion="hurt",
    ),
    Rule(
        "doubt",
        r"你行不行|你会吗|你懂[个啥]|没用|不靠谱|算了吧|指望不上|就这\??|就这\?",
        (-0.42, 0.20, -0.48),
        "被质疑",
        emotion="hurt",
    ),
    Rule(
        "complain",
        r"太差了|什么破|垃圾东西|失望|骗人|难用|不好用|没用的东西",
        (-0.36, 0.16, -0.26),
        "被抱怨",
        emotion="down",
    ),
    Rule(
        "argue",
        r"你错了|你不对|胡说|瞎说|乱说|明明是|才不是|别乱讲",
        (-0.24, 0.30, -0.10),
        "被反驳",
    ),
    Rule(
        "perfunctory",
        r"^[哦嗯切额啊这6]+$|^行吧$|^随便$|^你随意$|^好的吧$",
        (-0.26, 0.14, 0.16),
        "被敷衍",
        emotion="cold",
    ),
    Rule(
        "yelled_at",
        r"！{3,}|!{3,}|？{3,}|\?{3,}",
        (-0.06, 0.36, -0.06),
        "被大声吼",
    ),
    # ---------------------------- 中性/信息类 ----------------------------
    # C1：「提问」拆成两种 —— 真的在问我问题 / 随口一问（降权 0.6）。
    # 老版本只有一条「被提问」，任何一句问话都命中，于是起因动不动就是它。
    # 两条用 exclude 互斥：同一句话不会叠两次。
    Rule(
        "ask_info",
        QUESTION_WORDS,
        (0.06, 0.28, -0.10),
        "被认真问了个问题",
    ),
    Rule(
        "question",
        r"[?？]|吗$|呢$",
        (0.06, 0.28, -0.10),
        "被随口问了一句",
        weight=0.6,
        exclude=QUESTION_WORDS,
    ),
    Rule(
        "ask_help",
        r"帮帮|帮忙|求助|救命|求你了|拜托|教我|教教",
        (0.10, 0.22, 0.28),
        "被求助",
    ),
    Rule(
        "greeting",
        r"^早|早上好|早安|中午好|下午好|晚上好|你好|您好|hi|hello|嗨|在吗|在么|在不在|在嘛",
        (0.20, 0.15, 0.08),
        "被打招呼",
        weight=0.5,   # C1：中性规则降权，不再抢起因
    ),
    Rule(
        "farewell",
        r"再见|拜拜|88|晚安|睡了|先走|下线|下次聊|出门了",
        (-0.12, -0.22, -0.05),
        "对方要走了",
    ),
    Rule(
        "emoji_pos",
        r"😊|😄|😁|😍|🥰|😘|❤|💕|💖|💗|👍|🎉|✨|😆|😌|🤗|💪",
        (0.24, 0.18, 0.05),
        "收到开心的表情",
    ),
    Rule(
        "emoji_neg",
        r"😭|😢|💔|😡|🤬|😞|😔|😩|😫|🥲|😿|🙁|☹",
        (-0.32, 0.22, -0.15),
        "收到难过的表情",
    ),
)


# ---------------------------------------------------------------------------
# 评估器
# ---------------------------------------------------------------------------


@dataclass
class Appraiser:
    """把消息文本评估成情绪冲击。"""

    rules: tuple[Rule, ...] = CORE_RULES
    max_delta: float = 0.62
    combo_decay: float = 0.65
    long_text_threshold: int = 120
    #: 规则一条都没命中时，用中文情感词汇本体兜底（见 lexicon.py）
    lexicon_enabled: bool = True
    lexicon_gain: float = 0.25
    _compiled: dict[str, re.Pattern[str]] = field(default_factory=dict, repr=False)

    # -- 编译与匹配 ---------------------------------------------------------
    def _compile(self, pattern: str, flags: int) -> re.Pattern[str] | None:
        key = f"{flags}:{pattern}"
        cached = self._compiled.get(key)
        if cached is not None:
            return cached
        try:
            compiled = re.compile(pattern, flags)
        except re.error:
            return None
        self._compiled[key] = compiled
        return compiled

    def _matches(self, rule: Rule, text: str) -> bool:
        if rule.exclude:
            guard = self._compile(rule.exclude, rule.flags)
            if guard is not None and guard.search(text):
                return False
        regex = self._compile(rule.pattern, rule.flags)
        return bool(regex is not None and regex.search(text))

    # -- 主入口 -------------------------------------------------------------
    def appraise(
        self,
        text: str,
    ) -> Stimulus | None:
        """评估一条用户消息，返回情绪冲击；没有触发任何规则时返回 None。"""
        text = (text or "").strip()
        if not text:
            return None

        # 1) 危机信号：直接短路，交给上层走高优先级关心流程
        crisis = detect_crisis(text)
        if crisis:
            return Stimulus(
                delta=PAD.of(-0.40, 0.72, 0.18),
                cause="对方说了让人害怕的话",
                key="crisis",
                source="crisis",
                emotion="anxious",
                message=snippet(text),
            )

        # 2) 常规规则
        hits: list[Rule] = []
        for rule in self.rules:
            if self._matches(rule, text):
                hits.append(rule)

        # 3) 非规则信号：认真倾诉的长文
        compact = re.sub(r"\s+", "", text)
        if len(compact) >= self.long_text_threshold:
            hits.append(
                Rule("long_text", r".", (0.18, 0.05, 0.16), "对方认真说了很长一段", weight=0.5)
            )

        if not hits:
            # 3.5) 词表兜底：规则没覆盖到的日常表达，靠中文情感词汇本体接住。
            #      冲击力明显弱于手写规则，只负责"她有反应"，不抢戏。
            if self.lexicon_enabled:
                from . import lexicon as lexicon_mod

                verdict = lexicon_mod.appraise(text, gain=self.lexicon_gain)
                if verdict is not None:
                    return Stimulus(
                        delta=verdict.delta,
                        cause=verdict.cause,
                        key="lexicon",
                        source="user",
                        emotion=verdict.proto,
                        lexicon_hits=tuple(verdict.hits),
                        message=snippet(text),
                    )
            return None

        # 4) 按"冲击力"排序，后续命中依次衰减，避免一句话叠加出夸张数值
        hits.sort(key=lambda r: r.weight * max(1e-3, PAD.from_any(r.pad).magnitude()), reverse=True)
        delta = PAD()
        picked: list[str] = []
        emotion = ""
        for index, rule in enumerate(hits[:4]):
            factor = self.combo_decay ** index
            delta = delta + PAD.from_any(rule.pad) * (rule.weight * factor)
            picked.append(rule.cause or rule.key)
            if not emotion and rule.emotion:
                emotion = rule.emotion

        delta = delta.limit(self.max_delta)
        if delta.magnitude() < 0.015:
            return None

        return Stimulus(
            delta=delta,
            # B3：主因整句 + 次因「还顺带」（排序已经是 weight×冲击力）
            cause=render_cause(picked),
            key=hits[0].key,
            primary=hits[0].key,
            secondary=tuple(rule.key for rule in hits[1:4]),
            source="user",
            emotion=emotion,
            message=snippet(text),
        )


# ---------------------------------------------------------------------------
# 自定义规则解析
# ---------------------------------------------------------------------------


def parse_custom_rule(line: str) -> Rule | None:
    """解析配置里的一行自定义规则。

    格式（用 => 分段，前两段必填）::

        关键词1|关键词2 => 0.4,0.2,0.1 => 被夸得飘了

    老版本的规则里第三段是**好感度增量**（形如 `=> +3 => 说明`）——
    好感度功能去掉之后这一段会被识别成数字并自动跳过，
    这样配置里已有的老规则不用改。
    """
    if not line or not isinstance(line, str):
        return None
    parts = [seg.strip() for seg in line.split("=>")]
    if len(parts) < 2:
        return None
    keywords = [k.strip() for k in parts[0].split("|") if k.strip()]
    if not keywords:
        return None
    # 关键词整体当正则用；纯文字关键词自动转义，避免用户被正则坑到
    chunks: list[str] = []
    for keyword in keywords:
        if re.search(r"[\\^$.\[\]()*+?{}|]", keyword):
            chunks.append(f"(?:{keyword})")
        else:
            chunks.append(re.escape(keyword))
    pattern = "|".join(chunks)

    numbers = [to_float(x.strip(), 0.0) for x in parts[1].split(",")]
    while len(numbers) < 3:
        numbers.append(0.0)
    cause = keywords[0]
    if len(parts) > 2 and parts[2]:
        if re.fullmatch(r"[+\-]?\d+(?:\.\d+)?", parts[2]) and len(parts) > 3 and parts[3]:
            cause = parts[3]        # 旧格式：第三段是好感度，说明在第四段
        else:
            cause = parts[2]
    return Rule(
        key=f"custom:{keywords[0]}",
        pattern=pattern,
        pad=(numbers[0], numbers[1], numbers[2]),
        cause=cause,
        weight=1.0,
    )

