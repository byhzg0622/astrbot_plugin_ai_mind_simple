"""中文情感词汇本体（7 大类 21 小类）—— 情绪评估的兜底词表。

学术出处
--------
徐琳宏、林鸿飞等《情感词汇本体的构造》(2008)，大连理工大学信息检索研究室。
分类体系基于 Ekman 的 6 大类，再加入「好」以细分褒义情感，最终为 **7 大类 21 小类**：

    乐：快乐(PA)、安心(PE)
    好：尊敬(PD)、赞扬(PH)、相信(PG)、喜爱(PB)、祝愿(PK)
    怒：愤怒(NA)
    哀：悲伤(NB)、失望(NJ)、疚(NH)、思(PF)
    惧：慌(NI)、恐惧(NC)、羞(NG)
    恶：烦闷(NE)、憎恶(ND)、贬责(NN)、妒忌(NK)、怀疑(NL)
    惊：惊奇(PC)

强度分 1/3/5/7/9 五档，极性 0 中性 / 1 褒义 / 2 贬义 / 3 兼有。
官方词表共 27466 条；这里收的是**现代口语高频子集**（书面成语只留最常用的），
既够用又不至于把插件撑大，也避免生僻词造成的误判。

为什么需要它
------------
手写规则的覆盖面就一百来个触发词，而且是围着某个人设写的。
「今天好开心」「气死我了」「有点委屈」这种最普通的表达如果没写进规则，
情绪就一动不动。词表是开放词表，负责在规则没命中时兜底。

两条设计上的取舍
----------------
1. **兜底，不抢戏**：只有手写规则一条都没命中时才用词表，而且冲击力明显更弱
   （默认 0.25 倍），你精心写的「宝宝」「喜欢你」这类规则永远是主角。
2. **情绪传染 + 指向修正**：默认按词的小类走（她跟着对方的情绪变）；
   但如果负面词说的是「你」（指向她），就改成受伤 / 生气。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from .model import PAD

# ---------------------------------------------------------------------------
# 7 大类 -> Plutchik 的 8 条基本情绪轴
# ---------------------------------------------------------------------------
#: 大类 -> (Plutchik 家族, 说明)
FAMILIES: dict[str, tuple[str, str]] = {
    "乐": ("joy", "高兴、愉快、满足"),
    "好": ("trust", "认可、喜爱、信任、祝愿"),
    "怒": ("anger", "被冒犯、被惹火"),
    "哀": ("sadness", "失落、难过、思念"),
    "惧": ("fear", "不安、害怕、害羞"),
    "恶": ("disgust", "厌烦、反感、鄙视、怀疑"),
    "惊": ("surprise", "意外、吃惊"),
}

#: 极性代码
POLARITY_NEUTRAL = 0
POLARITY_POSITIVE = 1
POLARITY_NEGATIVE = 2
POLARITY_BOTH = 3

POLARITY_LABELS = {
    POLARITY_NEUTRAL: "中性",
    POLARITY_POSITIVE: "褒义",
    POLARITY_NEGATIVE: "贬义",
    POLARITY_BOTH: "兼有",
}

#: 官方强度档位
INTENSITY_STEPS = (1, 3, 5, 7, 9)

#: 强度 -> 冲击力倍率（9 档最猛，也才 1.6 倍）
INTENSITY_SCALE = {1: 0.45, 3: 0.70, 5: 1.00, 7: 1.30, 9: 1.60}


@dataclass(frozen=True)
class SubCategory:
    """一个情感小类。"""

    code: str          # PA / PE / ...
    name: str          # 快乐 / 安心 / ...
    family: str        # 乐 / 好 / 怒 / 哀 / 惧 / 恶 / 惊
    polarity: int
    pad: tuple[float, float, float]   # 她自己被"传染"到的方向
    proto: str         # 落到哪个情绪原型
    opposite: str = ""  # 前面加「不」时翻到哪个小类
    #: 这类词说出口时通常指向别人（「你真烦」），需要判断是不是在说她
    outward: bool = False


SUBCATEGORIES: tuple[SubCategory, ...] = (
    SubCategory("PA", "快乐", "乐", POLARITY_POSITIVE, (0.62, 0.45, 0.32), "joy", "NB"),
    SubCategory("PE", "安心", "乐", POLARITY_POSITIVE, (0.42, -0.32, 0.42), "content", "NI"),
    SubCategory("PD", "尊敬", "好", POLARITY_POSITIVE, (0.38, 0.28, -0.08), "grateful", "NN"),
    SubCategory("PH", "赞扬", "好", POLARITY_POSITIVE, (0.55, 0.42, 0.30), "proud", "NN"),
    SubCategory("PG", "相信", "好", POLARITY_POSITIVE, (0.34, 0.06, 0.32), "content", "NL"),
    SubCategory("PB", "喜爱", "好", POLARITY_POSITIVE, (0.70, 0.22, 0.06), "affection", "ND"),
    SubCategory("PK", "祝愿", "好", POLARITY_POSITIVE, (0.40, 0.46, 0.12), "expect", "NJ"),
    SubCategory("NA", "愤怒", "怒", POLARITY_NEGATIVE, (-0.55, 0.68, 0.30), "angry", "PE", True),
    SubCategory("NB", "悲伤", "哀", POLARITY_NEGATIVE, (-0.62, -0.20, -0.42), "sad", "PA"),
    SubCategory("NJ", "失望", "哀", POLARITY_NEGATIVE, (-0.48, -0.14, -0.34), "down", "PK"),
    SubCategory("NH", "疚", "哀", POLARITY_NEGATIVE, (-0.34, 0.12, -0.42), "down", "PE"),
    SubCategory("PF", "思", "哀", POLARITY_NEUTRAL, (-0.28, -0.06, -0.38), "lonely", "PE"),
    SubCategory("NI", "慌", "惧", POLARITY_NEGATIVE, (-0.30, 0.62, -0.55), "anxious", "PE"),
    SubCategory("NC", "恐惧", "惧", POLARITY_NEGATIVE, (-0.50, 0.72, -0.68), "anxious", "PE"),
    SubCategory("NG", "羞", "惧", POLARITY_NEUTRAL, (0.30, 0.58, -0.52), "shy", "PE"),
    SubCategory("NE", "烦闷", "恶", POLARITY_NEGATIVE, (-0.40, 0.44, -0.10), "annoyed", "PE", True),
    SubCategory("ND", "憎恶", "恶", POLARITY_NEGATIVE, (-0.62, 0.54, 0.22), "angry", "PB", True),
    SubCategory("NN", "贬责", "恶", POLARITY_NEGATIVE, (-0.52, 0.38, 0.18), "hurt", "PH", True),
    SubCategory("NK", "妒忌", "恶", POLARITY_NEGATIVE, (-0.26, 0.54, -0.12), "jealous", "PE", True),
    SubCategory("NL", "怀疑", "恶", POLARITY_NEGATIVE, (-0.22, 0.34, -0.28), "confused", "PG", True),
    SubCategory("PC", "惊奇", "惊", POLARITY_NEUTRAL, (0.08, 0.78, -0.28), "surprised", "PE"),
)

SUBCATEGORY_BY_CODE: dict[str, SubCategory] = {s.code: s for s in SUBCATEGORIES}

#: 被「你」指着说的时候，落到哪个原型（受伤 / 生气）
TARGETED_PROTO = "hurt"
TARGETED_PAD = (-0.58, 0.34, -0.66)

#: 词前面的程度副词（越靠前越强）
BOOSTERS: tuple[tuple[str, int], ...] = (
    ("欣喜若狂", 0),  # 占位，保持元组结构整齐
    ("超级", 2), ("非常", 2), ("特别", 2), ("极其", 2), ("极其", 2), ("无比", 2),
    ("太", 1), ("超", 1), ("好", 1), ("很", 1), ("挺", 1), ("蛮", 1),
    ("巨", 2), ("贼", 2), ("爆", 2), ("狂", 2), ("极", 2), ("死", 2), ("炸", 2),
    ("有点", -1), ("有些", -1), ("稍微", -1), ("略微", -1), ("一点点", -1), ("略", -1),
)

#: 否定词（出现在词前 2 个字内就当作被否定）
NEGATIONS = ("不", "没", "无", "别", "甭", "莫", "非", "未")

#: 指向她的代词（出现在词前 3 个字内 → 这句话是在说她）
TARGET_PRONOUNS = ("你", "妳", "您", "尼")

#: 指向自己的代词（→ 这是对方在说自己）
SELF_PRONOUNS = ("我", "俺", "咱", "自己", "本人", "老子", "被")

_DIRECTIONS = ("mirror", "targeted", "self")

# ---------------------------------------------------------------------------
# 词表本体：小类 -> ((强度, "词 词 词"), ...)
# ---------------------------------------------------------------------------
_WORDS: dict[str, tuple[tuple[int, str], ...]] = {
    "PA": (
        (9, "欣喜若狂 欢天喜地 乐疯了 心花怒放 喜出望外 狂喜 乐不可支 喜极而泣 眉开眼笑 手舞足蹈 高兴坏了 笑死我了 乐死我了 开心到飞起 快乐得冒泡"),
        (7, "太开心 好开心 超开心 开心极了 喜悦 欢喜 欢欣 喜滋滋 美滋滋 乐呵呵 兴高采烈 喜气洋洋 欢欣鼓舞 爽翻了 爽死了 开心死了 快活得不行 喜上眉梢 欢喜得紧"),
        (5, "开心 高兴 快乐 愉快 快活 欣喜 开怀 舒畅 痛快 惬意 舒心 舒服 满足 好耶 太棒了 太好了 好棒 棒极了 给力 惊喜 乐呵 乐 爽 美 甜 快乐呀 心里美 偷着乐 笑得合不拢嘴 心里开花 舒畅极啦"),
        (3, "还不错 心情不错 挺好 蛮好 还行 知足 有点开心 小小的开心 微微一笑 舒坦了些 还挺乐 稍微高兴"),
    ),
    "PE": (
        (9, "彻底放心了 心里一块石头落地 如释重负 问心无愧 定心丸 彻底安心"),
        (7, "松了一口气 放心了 安心了 心里踏实了 踏实多了 踏实 安心 心里有底"),
        (5, "宽心 放心 心安 安定 稳妥 靠谱 可靠 安心感 有底 稳了 妥当 安稳 舒心 心里舒坦"),
        (3, "还好 还好啦 问题不大 没事就好 差不多吧 还算稳"),
    ),
    "PD": (
        (9, "五体投地 肃然起敬 敬佩不已 毕恭毕敬 敬仰之至"),
        (7, "佩服得不行 敬佩 敬仰 钦佩 崇拜 敬重 肃然 仰慕"),
        (5, "佩服 尊敬 恭敬 致敬 敬爱 服气 服了 心服口服 高看一眼 尊重"),
        (3, "挺厉害的 还挺强 有点东西 不错嘛"),
    ),
    "PH": (
        (9, "完美无缺 无可挑剔 出类拔萃 卓越非凡 了不起极了 太优秀了 举世无双"),
        (7, "优秀 杰出 一流 出色 厉害 太强了 好厉害 真厉害 牛 牛逼 绝了 顶 无敌 天花板 神了"),
        (5, "棒 很棒 真棒 好棒 表扬 夸奖 称赞 赞美 认可 点赞 赞 不错 挺好 可以 靠谱 给力 靠谱 有水平 长脸 争气"),
        (3, "还行吧 还挺好 还不错 勉强可以 有点厉害"),
    ),
    "PG": (
        (9, "深信不疑 毋庸置疑 坚信不疑 完全信任"),
        (7, "坚信 深信 完全相信 特别信任 特别相信"),
        (5, "信任 信赖 相信 靠谱 可靠 信得过 依赖 笃定 信你 信得过你 放心交给你"),
        (3, "姑且信你 应该是吧 大概吧 就信你一回"),
    ),
    "PB": (
        (9, "爱不释手 一见钟情 倾慕已久 爱死你了 超喜欢你 喜欢得不得了"),
        (7, "很喜欢 好喜欢 超喜欢 特别喜欢你 喜欢你 爱你 最爱 心动 迷恋 痴迷 心动不已 好爱好爱"),
        (5, "喜欢 好感 中意 欣赏 偏爱 稀罕 宝贝 心头好 宠爱 疼 爱 宠 亲亲 抱抱 想抱 撒娇 稀罕你"),
        (3, "有点喜欢 挺喜欢的 还不错吧 有点好感"),
    ),
    "PK": (
        (9, "万事如意 福寿绵长 心想事成 万寿无疆 一生顺遂"),
        (7, "一路顺风 平安顺遂 祝福你 保佑你 愿你 希望你"),
        (5, "祝 祝福 祝愿 保佑 希望 期待 盼 愿 加油 好运 顺利 平安 健康 顺心 保重 万事顺利"),
        (3, "早点休息 多喝热水 注意身体 好好的 别太累"),
    ),
    "NA": (
        (9, "七窍生烟 大发雷霆 暴跳如雷 怒不可遏 火冒三丈 气疯了 气炸了 气得发抖 怒火中烧"),
        (7, "气死了 气死我了 好气 气炸 火大 恼火 暴怒 愤怒 生气 气愤 发火 发怒 气人 恼羞成怒 一肚子火 气不打一处来 气得不行"),
        (5, "烦死 可恶 不爽 窝火 憋火 上火 气得 火 怒了 不爽快 来气 冒火 恨得慌"),
        (3, "有点生气 不太高兴 有点不爽 略气 稍微有点气"),
    ),
    "NB": (
        (9, "心如刀割 悲痛欲绝 痛不欲生 肝肠寸断 心碎了 撕心裂肺 泪流满面 哭到崩溃"),
        (7, "好难过 很难过 难过死了 悲伤 忧伤 悲苦 伤心死了 痛哭 大哭 泪崩 崩溃 破防 心态崩了 难受死了 心里堵得慌"),
        (5, "难过 伤心 悲伤 忧伤 心痛 心酸 失落 沮丧 低落 想哭 难受 丧 郁闷 沉重 心里不好受 鼻子一酸 提不起精神 消沉 低落极了 心里堵"),
        (3, "有点难过 不太开心 心情不好 有点低落 不开心 高兴不起来 心里空落落 有点沉 稍微失落"),
    ),
    "NJ": (
        (9, "绝望 心灰意冷 万念俱灰 彻底没戏 死心了 寒透了心"),
        (7, "太失望了 失望透顶 灰心丧气 没指望了 白高兴一场 空欢喜 心凉了 彻底凉了"),
        (5, "失望 灰心 泄气 没劲 没意思 落空 没希望 算了 不指望 无望 白搭 心凉"),
        (3, "有点失望 不太指望 随缘吧 差不多得了"),
    ),
    "NH": (
        (9, "后悔莫及 追悔莫及 问心有愧 良心不安 悔恨交加"),
        (7, "特别内疚 特别后悔 特别抱歉 特别惭愧"),
        (5, "内疚 愧疚 后悔 忏悔 自责 过意不去 对不起 抱歉 惭愧 亏欠 不好意思 罪过 我的错 怪我"),
        (3, "有点后悔 有点抱歉 有点不好意思 稍微自责"),
    ),
    "PF": (
        (9, "朝思暮想 牵肠挂肚 相思成疾 想你想得睡不着 日思夜想"),
        (7, "特别想你 好想你 很想你 思念 相思 牵挂 惦记 挂念 想念 想你 怀念 惦记着 想你了 老是想起你"),
        (5, "挂心 念着 记挂 想着 忆起 回忆 惦记你 放心不下 念念不忘"),
        (3, "有点想你 偶尔想起 忽然想起 稍微想了一下"),
    ),
    "NI": (
        (9, "不知所措 手忙脚乱 六神无主 慌了神 心急如焚 急得团团转 慌得一批"),
        (7, "慌张 心慌 慌乱 着急 焦急 急死 心急 发慌 手足无措 忐忑 忐忑不安 七上八下 坐立不安 心急火燎 急死了"),
        (5, "慌 急 紧张 着急忙慌 心里发慌 沉不住气 不稳 抓瞎 忙乱 心里打鼓"),
        (3, "有点慌 有点急 稍微有点紧张 有点打鼓"),
    ),
    "NC": (
        (9, "胆颤心惊 魂飞魄散 惊恐万状 吓得半死 吓死了 毛骨悚然 魂都吓飞了"),
        (7, "恐惧 害怕 胆怯 惧怕 惊恐 心惊肉跳 担惊受怕 后背发凉 瘆得慌 好怕 吓人 可怕 恐怖 畏惧"),
        (5, "怕 惧 心里发毛 不安 忐忑不安 心有余悸 发怵 打怵 心惊 害怕极了"),
        (3, "有点怕 略怕 心里有点没底 稍微有点不安"),
    ),
    "NG": (
        (9, "无地自容 面红耳赤 羞死了 恨不得钻地缝 羞得不行"),
        (7, "特别害羞 特别不好意思 尴尬死了 社死了 羞耻 臊得慌 脸都红了"),
        (5, "害羞 害臊 羞涩 羞 脸红 不好意思 难为情 尴尬 社死 羞怯 汗颜 局促"),
        (3, "有点害羞 有点不好意思 略微尴尬 稍微脸红"),
    ),
    "NE": (
        (9, "心烦意乱 自寻烦恼 烦死了 烦透了 受不了了 崩溃了 烦得不行 厌烦透顶"),
        (7, "好烦 很烦 烦躁 烦闷 憋闷 心烦 郁闷 抓狂 烦人 腻烦 心累 累觉不爱 烦得很 心里烦 堵得慌"),
        (5, "烦 无聊 没劲 乏味 无趣 提不起劲 闷 堵 闹心 糟心 消极 疲惫 厌倦 厌烦 无感 没意思 腻了 疲倦 疲了"),
        (3, "有点烦 有点无聊 略闷 稍微有点腻"),
    ),
    "ND": (
        (9, "恨之入骨 深恶痛绝 恨死了 厌恶至极 恶心死了 恶心透顶"),
        (7, "厌恶 憎恶 憎恨 痛恨 反感 恶心 可恶 讨厌死了 恨 恶心人 讨厌至极"),
        (5, "讨厌 嫌 嫌弃 不待见 膈应 看不惯 不屑 厌 厌弃 唾弃 恶心吧啦 烦透"),
        (3, "有点讨厌 不太喜欢 略微反感 稍微有点膈应"),
    ),
    "NN": (
        (9, "一无是处 心狠手辣 不可理喻 一文不值 差劲透了 烂透了 垃圾透了"),
        (7, "太差劲 太糟糕 太烂 蠢死了 笨死了 莫名其妙 无理取闹 太过分了 差得离谱"),
        (5, "差劲 糟糕 烂 蠢 笨 呆板 虚荣 虚伪 敷衍 自私 幼稚 过分 差 糟 不靠谱 没用 废物 垃圾 拉胯 离谱 无聊透顶 不讲道理 变态 恶心"),
        (3, "有点差 不太行 一般般 勉强 说不上好 也就那样"),
    ),
    "NK": (
        (9, "醋坛子 嫉贤妒能 嫉妒死了 酸死了 醋意大发"),
        (7, "特别嫉妒 特别羡慕 嫉妒 妒忌 吃醋 眼红 羡慕嫉妒恨 酸溜溜"),
        (5, "酸 羡慕 醋 眼馋 嫉妒心 心里不平衡 不是滋味"),
        (3, "有点羡慕 有点酸 稍微眼馋"),
    ),
    "NL": (
        (9, "疑神疑鬼 疑心重重 满腹狐疑 完全不信"),
        (7, "特别怀疑 特别可疑 太可疑了 一看就有鬼"),
        (5, "怀疑 多心 生疑 将信将疑 半信半疑 疑心 不信 可疑 蹊跷 有鬼 糊弄 骗人 撒谎 假的吧 骗我吧 有诈"),
        (3, "有点怀疑 不太信 存疑 有点蹊跷"),
    ),
    "PC": (
        (9, "大吃一惊 瞠目结舌 目瞪口呆 震惊 吓一跳 万万没想到 惊掉下巴 震碎三观"),
        (7, "太意外了 想不到 没想到 惊了 震惊 意外 出乎意料 惊天 天呐 我的天 卧槽 我靠 离谱 什么鬼"),
        (5, "奇怪 好奇 惊讶 吃惊 惊奇 竟然 居然 神奇 真的假的 咦 诶 新鲜 少见 稀奇 怪了 反常 不对劲"),
        (3, "有点意外 有点奇怪 稍微惊讶 咦"),
    ),
}



#: 补充词：口语和网络用语里高频、但本体库原表里少见的表达。
#: 和 _WORDS 一起进索引；同一个词以 _WORDS 里的强度为准。
_EXTRA: dict[str, tuple[tuple[int, str], ...]] = {
    "PA": (
        (7, "笑死 笑死我 笑喷 乐死 太快乐了 好耶 高兴死了"),
        (5, "哈哈 哈哈哈 嘿嘿 嘻嘻 快乐乐 开心心 美美哒 好爽 爽到 心里美滋滋"),
    ),
    "PE": ((5, "心里踏实 稳当 顺心 安逸"),),
    "PD": ((5, "膜拜 大佬 辛苦你了 厉害了我的 谢谢 谢谢你 感谢 多谢 谢了 辛苦了"),),
    "PH": ((7, "太可了 爱了 磕到了 太顶了 你最强 真优秀"), (5, "好强 真强 优秀呀 有你的")),
    "PG": ((5, "信你 交给你 靠你了"),),
    "PB": ((7, "爱了爱了 好爱好爱 想抱抱"), (5, "亲亲 抱抱 么么 贴贴 稀罕 想你了")),
    "PK": (
        (5, "晚安 早安 午安 好梦 安安 顺利呀 加油呀"),
        (3, "早点睡 注意休息 照顾好自己"),
    ),
    "NA": (
        (7, "气到发抖 火大极了 气得不行 太气人"),
        (5, "来气 冒火 气不过 气人 惹火"),
    ),
    "NB": (
        (9, "破大防 心态炸了 蚌埠住了 绷不住 裂开 心态崩了"),
        (7, "委屈 好委屈 心里难受 呜呜 呜呜呜 emo 麻了 好想哭"),
        (5, "心塞 堵心 憋屈 心里酸 心里空 难受 不是滋味"),
    ),
    "NJ": ((5, "无语 无奈 没办法 认命 没辙 无所谓了"),),
    "NH": ((5, "我的错 怪我 对不住"),),
    "PF": ((5, "舍不得 放心不下 心里有你"),),
    "NI": (
        (7, "焦虑 忧虑 焦心 担忧 不放心 心里没底 睡不着 失眠 慌得一批"),
        (5, "紧张 悬着心 心里打鼓"),
    ),
    "NC": ((7, "担心 好担心 特别担心 害怕极了 心疼 心疼你"), (5, "心里发怵 没底 发慌")),
    "NG": ((5, "丢人 出丑 丢脸"),),
    "NE": (
        (7, "无语 麻了 顶不住 受够了 遭不住"),
        (5, "糟心 闹心 腻了 疲倦 疲了 心好累"),
    ),
    "ND": ((7, "恶心 烦人"), (5, "看不惯 嫌")),
    "NN": (
        (7, "挨骂 被骂 骂了 骂我 骂人 被喷 被怼 过分 讨厌鬼"),
        (5, "差 烂 废物 垃圾 笨 蠢 呆 幼稚 自私"),
    ),
    "NK": ((5, "眼馋 心里不平衡"),),
    "NL": ((5, "不信 有诈 假的吧 骗人 撒谎 糊弄 忽悠"),),
    "PC": (
        (7, "真的假的 什么鬼 惊了 天呐 我的天 卧槽 我超"),
        (5, "咦 诶 稀奇 反常 不对劲"),
    ),
}

# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------
def _build_index() -> dict[str, tuple[SubCategory, int]]:
    index: dict[str, tuple[SubCategory, int]] = {}
    for source in (_WORDS, _EXTRA):
        for code, groups in source.items():
            sub = SUBCATEGORY_BY_CODE.get(code)
            if sub is None:
                continue
            for intensity, blob in groups:
                for word in blob.split():
                    word = word.strip()
                    # 单字词一律不要：「气」会命中「天气」，「好」会命中「好吃」
                    if len(word) < 2:
                        continue
                    index.setdefault(word, (sub, intensity))
    return index


INDEX: dict[str, tuple[SubCategory, int]] = _build_index()
MAX_WORD_LEN: int = max((len(word) for word in INDEX), default=2)


def vocabulary_size() -> int:
    return len(INDEX)


def words_of(code: str) -> list[tuple[str, int]]:
    """某个小类下的全部词（按强度从高到低）。"""
    return sorted(
        ((word, entry[1]) for word, entry in INDEX.items() if entry[0].code == code),
        key=lambda item: (-item[1], item[0]),
    )


def family_of(code: str) -> str:
    sub = SUBCATEGORY_BY_CODE.get(code)
    return FAMILIES.get(sub.family, ("", ""))[0] if sub else ""


# ---------------------------------------------------------------------------
# 上下文修正：否定 / 程度 / 指向
# ---------------------------------------------------------------------------
def _negated(text: str, start: int) -> bool:
    window = text[max(0, start - 2) : start]
    return any(neg in window for neg in NEGATIONS)


def _direction(text: str, start: int, word: str = "") -> str:
    """这句话是在说她、在说他自己、还是单纯在说一件事。"""
    window = text[max(0, start - 3) : start]
    if any(pron in window for pron in TARGET_PRONOUNS):
        return "targeted"
    if any(pron in window + word for pron in SELF_PRONOUNS):
        return "self"
    return "mirror"


def _booster(text: str, start: int) -> int:
    """返回强度档位的偏移：超/好/很 → 上一档，有点/稍微 → 下一档。"""
    shift = 0
    for token, amount in BOOSTERS:
        if not token or not amount:
            continue
        if text[max(0, start - len(token)) : start].endswith(token):
            if abs(amount) > abs(shift):
                shift = amount
    return shift


def adjust_intensity(base: int, shift: int) -> int:
    steps = list(INTENSITY_STEPS)
    try:
        position = steps.index(base)
    except ValueError:
        position = 2
    position = max(0, min(len(steps) - 1, position + shift))
    return steps[position]


@dataclass(frozen=True)
class Hit:
    """一次词表命中。"""

    word: str
    code: str
    name: str
    family: str
    intensity: int
    polarity: int
    direction: str
    negated: bool
    offset: int

    @property
    def sub(self) -> SubCategory:
        return SUBCATEGORY_BY_CODE[self.code]

    def resolved(self) -> tuple[SubCategory, bool]:
        """把否定也算进去，返回真正生效的小类和"是否翻面"。"""
        sub = self.sub
        if self.negated and sub.opposite:
            other = SUBCATEGORY_BY_CODE.get(sub.opposite)
            if other is not None:
                return other, True
        return sub, False

    def pad_for(self) -> tuple[PAD, str]:
        """这个词让她往哪个方向动、落到哪个情绪原型。"""
        sub, _flipped = self.resolved()
        if sub.outward:
            if self.direction == "targeted":
                # 说的是「你真烦」：这是在说她，那就受伤 / 生气
                return PAD.from_any(TARGETED_PAD), TARGETED_PROTO
            if self.direction == "self":
                # 说的是「领导骂我」：被欺负的是他，替他生气
                return PAD.from_any(SUBCATEGORY_BY_CODE["NA"].pad), "angry"
        return PAD.from_any(sub.pad), sub.proto

    @property
    def scale(self) -> float:
        return INTENSITY_SCALE.get(self.intensity, 1.0)

    def describe(self) -> str:
        bits = [f"{self.name}({self.code})", f"强度 {self.intensity}"]
        if self.negated:
            bits.append("否定")
        if self.direction == "targeted":
            bits.append("指向她")
        return f"「{self.word}」 " + " · ".join(bits)


#: 起手扫的时候最多看几个字（中文情绪词很少超过 4 个字）
_SCAN_LENGTHS = (4, 3, 2)


def scan(text: str, limit: int = 3) -> list[Hit]:
    """从左到右扫一遍，同一个位置优先匹配最长的词。"""
    if not text:
        return []
    hits: list[Hit] = []
    index = 0
    length = len(text)
    while index < length and len(hits) < max(1, limit):
        matched: tuple[str, SubCategory, int] | None = None
        for size in _SCAN_LENGTHS:
            if size > MAX_WORD_LEN or index + size > length:
                continue
            entry = INDEX.get(text[index : index + size])
            if entry is not None:
                matched = (text[index : index + size], entry[0], entry[1])
                break
        if matched is None:
            index += 1
            continue
        word, sub, base = matched
        hits.append(
            Hit(
                word=word,
                code=sub.code,
                name=sub.name,
                family=sub.family,
                intensity=adjust_intensity(base, _booster(text, index)),
                polarity=sub.polarity,
                direction=_direction(text, index, word),
                negated=_negated(text, index),
                offset=index,
            )
        )
        index += len(word)
    return hits


# ---------------------------------------------------------------------------
# 兜底评估
# ---------------------------------------------------------------------------
#: 默认冲击力倍率：手写规则大约是这个数的 3~4 倍，词表只负责"有反应"
DEFAULT_GAIN = 0.25

#: 再怎么叠也不超过这个幅度，免得一句话把情绪打飞
MAX_DELTA = 0.34

#: 第二个命中打几折
SECOND_HIT_DECAY = 0.45


@dataclass
class Verdict:
    """词表给出的情绪冲击。"""

    delta: PAD
    proto: str
    cause: str
    hits: list[Hit]

    def describe(self) -> str:
        return " · ".join(hit.describe() for hit in self.hits)


def appraise(
    text: str,
    gain: float = DEFAULT_GAIN,
    max_hits: int = 2,
) -> Verdict | None:
    """没命中任何手写规则时，用词表兜底给一个情绪冲击。"""
    hits = scan(text, limit=max_hits)
    if not hits:
        return None
    total = PAD()
    proto = ""
    best = -1.0
    for order, hit in enumerate(hits):
        pad, hit_proto = hit.pad_for()
        factor = hit.scale * (1.0 if order == 0 else SECOND_HIT_DECAY) * max(0.0, gain)
        total = total + pad * factor
        strength = pad.magnitude() * hit.scale
        if strength > best:
            best, proto = strength, hit_proto
    magnitude = total.magnitude()
    if magnitude > MAX_DELTA:
        total = total * (MAX_DELTA / magnitude)
    top = hits[0]
    cause = f"对方说「{top.word}」（{top.name}）"
    if len(hits) > 1:
        cause += f"，还有「{hits[1].word}」"
    return Verdict(delta=total, proto=proto, cause=cause, hits=hits)


def describe_lexicon() -> str:
    return (
        f"情感词汇本体：{len(FAMILIES)} 大类 / {len(SUBCATEGORIES)} 小类 / "
        f"{vocabulary_size()} 个常用词"
    )


__all__ = [
    "BOOSTERS",
    "DEFAULT_GAIN",
    "FAMILIES",
    "Hit",
    "INDEX",
    "INTENSITY_SCALE",
    "INTENSITY_STEPS",
    "MAX_DELTA",
    "NEGATIONS",
    "POLARITY_LABELS",
    "SUBCATEGORIES",
    "SUBCATEGORY_BY_CODE",
    "SubCategory",
    "Verdict",
    "adjust_intensity",
    "appraise",
    "describe_lexicon",
    "family_of",
    "scan",
    "vocabulary_size",
    "words_of",
]
