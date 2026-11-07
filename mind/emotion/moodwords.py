"""心境专属的「一键改成」词表 —— 和情绪那套原型锚点**不是一回事**。

情绪的「一键改成」点的是 PAD 原型（开心 / 害羞 / 生气 …），那描述的是
**此刻的情绪**；心境描述的是**长期底色**，而且它天生就是「相对气质基线的偏移」，
所以这里的每个词给的都是 offset，不是绝对坐标：

    心境 = 气质基线 + offset

每个词还带一个 target：它**应该**被判成哪个心境标签（D1 主导维度规则）。
tests 会逐个校验这条链 —— 点「心满意足」判出来必须是「满足」，
不许出现「点了心满意足、面板显示开心」这种对不上的情况。

**emoji 只挑老版本就有的**（🫙🫶 这类 14.0+ 的新字在系统字体里会画成方框 □）。

**只描述心境本身，不含任何「该怎么说话」的内容。**
"""

from __future__ import annotations

from dataclasses import dataclass

from typing import Any

from .model import PAD


@dataclass(frozen=True)
class MoodWord:
    """一个心境描述词：按钮上的字 + 它对应的基线偏移 + 期望的判定结果。"""

    key: str
    label: str
    emoji: str
    #: 相对气质基线的偏移 (p, a, d)
    offset: tuple[float, float, float]
    #: D1 规则应该判出来的标签 key（tests 校验）
    target: str = "calm"


#: 心境的一键改成：底色词（正向 → 负向），18 个
MOOD_WORDS: tuple[MoodWord, ...] = (
    MoodWord("calm", "平静如常", "😌", (0.00, 0.00, 0.00), "calm"),
    MoodWord("joy", "挺开心", "😊", (0.34, 0.16, 0.10), "joy"),
    MoodWord("content", "心满意足", "🥰", (0.38, -0.18, 0.14), "content"),
    MoodWord("excited", "有点上头", "🔥", (0.34, 0.38, 0.10), "excited"),
    MoodWord("proud", "得意洋洋", "😎", (0.26, 0.10, 0.40), "proud"),
    MoodWord("affection", "有点黏人", "🤗", (0.34, 0.02, -0.34), "affection"),
    MoodWord("bored", "有点闷", "🌫️", (0.02, -0.30, 0.00), "bored"),
    MoodWord("tired", "提不起劲", "😪", (-0.10, -0.34, -0.05), "tired"),
    MoodWord("down", "低气压", "🌧️", (-0.30, -0.22, -0.10), "down"),
    MoodWord("annoyed", "烦躁不安", "😤", (-0.24, 0.36, 0.02), "annoyed"),
    MoodWord("hurt", "心事重重", "💭", (-0.24, 0.00, -0.38), "hurt"),
    MoodWord("distant", "有点疏离", "😶", (-0.30, -0.10, 0.36), "cold"),
    # ---- 第二批：补上规则覆盖不到的几种，外加两个**底气驱动**的词 ----
    #（底气先判之后：好 + 有底气 → 得意；坏 + 有底气 → 冷淡 —— 拉底气就会换词）
    MoodWord("confident", "底气十足", "💪", (0.16, 0.05, 0.45), "proud"),
    MoodWord("stubborn", "爱答不理", "🙄", (-0.40, -0.25, 0.40), "cold"),
    MoodWord("angry", "火气上来了", "😠", (-0.45, 0.55, 0.25), "angry"),
    MoodWord("sad", "闷闷不乐", "😢", (-0.55, -0.30, -0.15), "sad"),
    MoodWord("curious", "有点好奇", "🤔", (0.08, 0.42, 0.05), "curious"),
    MoodWord("anxious", "忐忑不安", "😰", (-0.30, 0.40, -0.42), "anxious"),
)

MOOD_WORDS_BY_KEY: dict[str, MoodWord] = {word.key: word for word in MOOD_WORDS}


def resolve_mood_word(token: str) -> MoodWord | None:
    """把按钮上的 key（或直接给的中文词）解析成心境词；认不出返回 None。"""
    key = str(token or "").strip()
    if not key:
        return None
    word = MOOD_WORDS_BY_KEY.get(key)
    if word is not None:
        return word
    for candidate in MOOD_WORDS:
        if candidate.label == key:
            return candidate
    return None


def mood_word_pad(word: MoodWord, baseline: Any) -> PAD:
    """心境词 → 绝对 PAD（气质基线 + 偏移，逐维夹到 [-1, 1]）。"""
    p, a, d = word.offset
    base = PAD.from_any(baseline)
    return PAD.of(base.p + p, base.a + a, base.d + d)


def mood_words_payload() -> list[dict]:
    """面板渲染按钮用的结构（后端是唯一真源，前端不再自己写死一份词表）。"""
    return [
        {
            "key": word.key,
            "label": word.label,
            "emoji": word.emoji,
            "offset": [round(value, 4) for value in word.offset],
            "target": word.target,
        }
        for word in MOOD_WORDS
    ]


__all__ = [
    "MOOD_WORDS",
    "MOOD_WORDS_BY_KEY",
    "MoodWord",
    "mood_word_pad",
    "mood_words_payload",
    "resolve_mood_word",
]
