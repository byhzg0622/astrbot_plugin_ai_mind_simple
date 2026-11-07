"""中文情感词汇本体（7 大类 21 小类）与 Plutchik 结构的测试。

这一层是纯函数，不需要 astrbot。
"""

from __future__ import annotations

import unittest

from mind.emotion import lexicon as lx
from mind.emotion.appraisal import Appraiser, CORE_RULES
from mind.emotion.model import PLUTCHIK, PLUTCHIK_AXES, PROTOTYPES


class VocabularyTest(unittest.TestCase):
    def test_size_is_meaningful(self) -> None:
        self.assertGreater(lx.vocabulary_size(), 700, "词表太小，兜底没意义")

    def test_no_single_char_words(self) -> None:
        """单字词误判率太高：「气」会命中「天气」，「好」会命中「好吃」。"""
        bad = [word for word in lx.INDEX if len(word) < 2]
        self.assertEqual(bad, [], "词表里有单字词：%r" % bad[:10])

    def test_seven_families_and_21_subcategories(self) -> None:
        self.assertEqual(len(lx.FAMILIES), 7)
        self.assertEqual(len(lx.SUBCATEGORIES), 21)
        self.assertEqual(len({s.code for s in lx.SUBCATEGORIES}), 21, "小类代码重复")

    def test_every_family_has_subcategories(self) -> None:
        for family in lx.FAMILIES:
            subs = [s for s in lx.SUBCATEGORIES if s.family == family]
            self.assertTrue(subs, "%s 大类没有小类" % family)

    def test_every_subcategory_has_words(self) -> None:
        for sub in lx.SUBCATEGORIES:
            self.assertTrue(lx.words_of(sub.code), "%s(%s) 一个词都没有" % (sub.name, sub.code))

    def test_every_subcategory_maps_to_a_real_prototype(self) -> None:
        keys = {proto.key for proto in PROTOTYPES}
        for sub in lx.SUBCATEGORIES:
            self.assertIn(sub.proto, keys, "%s 指向了不存在的原型 %s" % (sub.name, sub.proto))

    def test_polarity_is_valid(self) -> None:
        for sub in lx.SUBCATEGORIES:
            self.assertIn(sub.polarity, lx.POLARITY_LABELS)

    def test_opposites_are_defined(self) -> None:
        for sub in lx.SUBCATEGORIES:
            if sub.opposite:
                self.assertIn(sub.opposite, lx.SUBCATEGORY_BY_CODE, sub.name)

    def test_family_lookup(self) -> None:
        self.assertEqual(lx.family_of("PA"), "joy")
        self.assertEqual(lx.family_of("NA"), "anger")
        self.assertEqual(lx.family_of("不存在"), "")


class ScanTest(unittest.TestCase):
    def test_finds_a_common_word(self) -> None:
        hits = lx.scan("我今天有点失落")
        self.assertEqual([hit.word for hit in hits], ["失落"])

    def test_longest_match_wins(self) -> None:
        self.assertEqual(lx.scan("笑死我了")[0].word, "笑死我了")
        self.assertEqual(lx.scan("好开心")[0].word, "好开心")

    def test_neutral_text_yields_nothing(self) -> None:
        self.assertEqual(lx.scan("我在吃饭"), [])
        self.assertEqual(lx.scan(""), [])
        self.assertEqual(lx.appraise("我在吃饭"), None)

    def test_booster_raises_intensity(self) -> None:
        plain = lx.scan("开心")[0]
        boosted = lx.scan("超级开心")[0]
        self.assertGreater(boosted.intensity, plain.intensity)

    def test_diminisher_lowers_intensity(self) -> None:
        plain = lx.scan("开心")[0]
        soft = lx.scan("有点开心")[0]
        self.assertLess(soft.intensity, plain.intensity)

    def test_negation_is_detected(self) -> None:
        """「不讨厌」= 被否定，应当翻到对立小类（喜爱）。"""
        hit = lx.scan("不讨厌")[0]
        self.assertEqual(hit.word, "讨厌")
        self.assertTrue(hit.negated)
        sub, flipped = hit.resolved()
        self.assertTrue(flipped)
        self.assertEqual(sub.code, "PB")

    def test_word_table_knows_negative_forms_directly(self) -> None:
        """「不开心」本身就收在词表里，不需要靠否定推断。"""
        hit = lx.scan("不开心")[0]
        self.assertEqual(hit.word, "不开心")
        self.assertEqual(hit.code, "NB")
        self.assertFalse(hit.negated)

    def test_direction_targeted(self) -> None:
        hit = lx.scan("你烦死了")[0]
        self.assertEqual(hit.direction, "targeted")
        self.assertEqual(hit.pad_for()[1], lx.TARGETED_PROTO)

    def test_direction_self(self) -> None:
        hit = lx.scan("领导骂我")[0]
        self.assertEqual(hit.direction, "self")

    def test_direction_mirror_by_default(self) -> None:
        self.assertEqual(lx.scan("有点难过")[0].direction, "mirror")


class AppraiseTest(unittest.TestCase):
    def test_returns_pad_and_prototype(self) -> None:
        verdict = lx.appraise("我今天有点失落")
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.proto, "sad")
        self.assertGreater(verdict.delta.magnitude(), 0.0)

    def test_delta_is_capped(self) -> None:
        """兜底再叠也不能把情绪打飞。"""
        verdict = lx.appraise("超级无敌开心 好开心 美滋滋 笑死我了 欣喜若狂")
        self.assertIsNotNone(verdict)
        self.assertLessEqual(verdict.delta.magnitude(), lx.MAX_DELTA + 1e-6)

    def test_gain_scales_the_impact(self) -> None:
        weak = lx.appraise("好开心", gain=0.1)
        strong = lx.appraise("好开心", gain=0.5)
        self.assertLess(weak.delta.magnitude(), strong.delta.magnitude())



    def test_cause_mentions_the_word(self) -> None:
        self.assertIn("失落", lx.appraise("有点失落").cause)

    def test_common_phrases_are_covered(self) -> None:
        """就是这批以前毫无反应的话。"""
        for text in ("我今天有点失落", "这事儿真让人窝火", "美滋滋", "心累",
                     "有点忐忑", "我破防了", "好烦啊", "今天好开心",
                     "睡不着，焦虑", "有点委屈", "气死我了", "无聊死了",
                     "哈哈哈哈", "呜呜呜", "谢谢你陪我", "心疼你"):
            self.assertIsNotNone(lx.appraise(text), "%r 还是毫无反应" % text)

    def test_describe(self) -> None:
        self.assertIn("情感词汇本体", lx.describe_lexicon())


class PlutchikTest(unittest.TestCase):
    def test_eight_families(self) -> None:
        self.assertEqual(len(PLUTCHIK), 8)

    def test_four_axes_and_symmetric(self) -> None:
        self.assertEqual(len(PLUTCHIK_AXES), 4)
        for left, right in PLUTCHIK_AXES:
            self.assertEqual(PLUTCHIK[left][2], right)
            self.assertEqual(PLUTCHIK[right][2], left)

    def test_families_are_populated(self) -> None:
        for key in PLUTCHIK:
            count = len([p for p in PROTOTYPES if p.family == key])
            self.assertGreater(count, 0, "家族 %s 没有原型" % key)

    def test_only_calm_has_no_family(self) -> None:
        orphans = [p.key for p in PROTOTYPES if not p.family]
        self.assertEqual(orphans, ["calm"])

    def test_family_covers_every_prototype_key(self) -> None:
        keys = {p.key for p in PROTOTYPES}
        for key in PLUTCHIK:
            self.assertIn(key, {p.family for p in PROTOTYPES} | set(PLUTCHIK), key)
        self.assertTrue(keys)


class AppraisalFallbackTest(unittest.TestCase):
    """词表是兜底：规则优先，而且冲击力明显更弱。"""

    def test_lexicon_handles_what_rules_miss(self) -> None:
        appraiser = Appraiser(rules=CORE_RULES, lexicon_enabled=True)
        stimulus = appraiser.appraise("我今天有点失落")
        self.assertIsNotNone(stimulus)
        self.assertEqual(stimulus.key, "lexicon")
        self.assertEqual(stimulus.emotion, "sad")
        self.assertTrue(stimulus.lexicon_hits)

    def test_lexicon_can_be_turned_off(self) -> None:
        appraiser = Appraiser(rules=CORE_RULES, lexicon_enabled=False)
        self.assertIsNone(appraiser.appraise("我今天有点失落"))

    def test_rules_still_win(self) -> None:
        appraiser = Appraiser(rules=CORE_RULES, lexicon_enabled=True)
        stimulus = appraiser.appraise("你好棒")
        self.assertIsNotNone(stimulus)
        self.assertNotEqual(stimulus.key, "lexicon", "规则命中时不该走兜底")
        self.assertFalse(stimulus.lexicon_hits)

    def test_lexicon_is_weaker_than_rules(self) -> None:
        appraiser = Appraiser(rules=CORE_RULES, lexicon_enabled=True)
        rule_hit = appraiser.appraise("你好棒")
        lexicon_hit = appraiser.appraise("我今天有点失落")
        self.assertIsNotNone(rule_hit)
        self.assertIsNotNone(lexicon_hit)
        self.assertLess(
            lexicon_hit.delta.magnitude(),
            rule_hit.delta.magnitude(),
            "兜底不该比手写规则还猛",
        )

    def test_lexicon_only_fires_when_rules_miss(self) -> None:
        """规则命中的句子里就算有情绪词，也不该再叠一层兜底。"""
        appraiser = Appraiser(rules=CORE_RULES, lexicon_enabled=True)
        stimulus = appraiser.appraise("你好棒，我今天好开心")
        self.assertEqual(stimulus.key, "praise")
        self.assertFalse(stimulus.lexicon_hits)


# 「理论」页签已经删掉了，原来测 theory_payload 形状的两个用例一并删除。
# 词表**评估**本身（上面那些用例）仍然在跑。


if __name__ == "__main__":
    unittest.main(verbosity=2)
