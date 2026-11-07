"""发送前的文本清理。

人设规定她说话**中文之间不留空格**，但模型时不时会写出「切 干嘛」「好 吧」
「行 。」这种。这是硬规则，靠提示词碰运气不靠谱 —— 落地前统一清一遍。

只动「中文↔中文」和「中文标点前面」的空白，中英之间该有的空格保持不动：
「AstrBot 很好用」原样保留。
"""

from __future__ import annotations

import re

# 汉字（含扩展 A 区与兼容区）
_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
# 中文标点：CJK 符号与标点、全角形式
_CJK_PUNCT = "\u3000-\u303f\uff01-\uff65"

_BETWEEN_CJK = re.compile("(?<=[" + _CJK + "])[ \t\u3000]+(?=[" + _CJK + "])")
_BEFORE_CJK_PUNCT = re.compile("[ \t\u3000]+(?=[" + _CJK_PUNCT + "])")


def tidy_cjk_spaces(text: str) -> str:
    """去掉中文之间、以及中文标点前面多出来的空格。"""
    if not text:
        return text
    out = _BETWEEN_CJK.sub("", text)
    return _BEFORE_CJK_PUNCT.sub("", out)


#: 消息里夹带的**协议占位标记**：QQ/OneBot 把「引用回复」序列化成
#: `<quoted_message sender="…" id="…"/>`，Go-CQHttp 那边是 `[CQ:reply,…]`。
#: 直接存进「原话」，面板上就是一串看不懂的乱码（报过的 bug），所以统一剥掉。
_MARKUP = re.compile(
    # 已知的占位标记（自闭合或成对都行）
    r"</?(?:quoted_message|reply|forward|node|at|image|record|video|file|json|xml)\b[^<>]*>"
    # Go-CQHttp 的 CQ 码
    r"|\[CQ:[^\]]*\]"
    # 兜底：形如 <tag …> 的标记（第一个字符得是字母，免得误伤「a<b」这类正常文本）
    r"|<[a-zA-Z][^<>]{0,200}>",
)


def strip_message_markup(text: str) -> str:
    """剥掉消息里的协议占位标记，只留人看得懂的话（幂等，空文本原样返回）。"""
    if not text:
        return text
    return _MARKUP.sub("", text)


__all__ = ["strip_message_markup", "tidy_cjk_spaces"]

