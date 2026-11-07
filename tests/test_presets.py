"""情绪默认参数（原「人格预设」模块）的测试。

这轮改造把**内置人格与说话方式整块删掉了**，所以这里守三件事：

1. 默认情绪参数的数值原样不动（基线、半衰期、心境联动）——
   那是情绪曲线的根基，不能被顺手改坏。
2. 情绪触发规则还在（她照样会对"被叫宝宝""他生病了"起反应），
   但**任何规则里都不许再出现"怎么说话"的字段**。
3. 自定义人格文件（`presets/*.json`）的读写入口已经全部删除 ——
   插件不再加载任何人设，说话方式完全交给机器人自己的提示词。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from mind.emotion import presets as presets_mod  # noqa: E402
from mind.emotion.presets import (  # noqa: E402
    DEFAULT_PRESET,
    PRESETS,
    TRIGGER_RULES,
    get_preset,
)

#: 默认情绪参数的"指纹"。任何改动碰坏它，这里就会红。
DEFAULT_FINGERPRINT = {
    "key": "default",
    "label": "默认",
    "baseline": {"p": 0.08, "a": 0.22, "d": 0.3},
    "emotion_half_life": 240.0,
    "mood_half_life": 21600.0,
    "mood_coupling": 0.1,
    # 情绪强度的衰减策略（强度是可调的主动产物，默认值写在情绪配置里）
    "intensity_decay": "exponential",
    "intensity_half_life": 900.0,
    "intensity_floor": 0.15,
    "intensity_gain": 0.55,
    "intensity_default": 0.3,
}


class TestDefaultPreset(unittest.TestCase):
    def test_fingerprint(self):
        preset = PRESETS[DEFAULT_PRESET]
        self.assertEqual(preset.key, DEFAULT_FINGERPRINT["key"])
        self.assertEqual(preset.label, DEFAULT_FINGERPRINT["label"])
        self.assertEqual(preset.baseline.to_dict(), DEFAULT_FINGERPRINT["baseline"])
        self.assertEqual(preset.emotion_half_life, DEFAULT_FINGERPRINT["emotion_half_life"])
        self.assertEqual(preset.mood_half_life, DEFAULT_FINGERPRINT["mood_half_life"])
        self.assertEqual(preset.mood_coupling, DEFAULT_FINGERPRINT["mood_coupling"])

    def test_intensity_decay_strategy_is_in_the_emotion_config(self):
        """强度的衰减策略属于情绪配置：默认值在这里，配置项能覆盖它。"""
        preset = PRESETS[DEFAULT_PRESET]
        self.assertEqual(preset.intensity_decay, "exponential")
        self.assertEqual(preset.intensity_half_life, 900.0)
        self.assertEqual(preset.intensity_floor, 0.15)
        self.assertEqual(preset.intensity_gain, 0.55)
        self.assertEqual(preset.intensity_default, 0.3)
        # 配置里能改（引擎读 emotion.intensity.*）
        from mind.emotion import Settings

        tuned = Settings.from_config({
            "intensity": {"decay": "linear", "half_life": 300, "floor": 0.4}
        })
        self.assertEqual(tuned.intensity_decay, "linear")
        self.assertEqual(tuned.intensity_half_life, 300.0)
        self.assertAlmostEqual(tuned.intensity_floor, 0.4, places=6)
        # 没配的项退回情绪配置里的默认值
        self.assertAlmostEqual(tuned.intensity_gain, 0.55, places=6)

    def test_trigger_rules_still_cover_key_scenes(self):
        """情绪触发规则不能因为删人格被误删 —— 靠它们"跑感情"。"""
        self.assertGreaterEqual(len(TRIGGER_RULES), 12)
        keys = {rule.key for rule in TRIGGER_RULES}
        for expected in (
            "petname", "confess", "call_master", "care_sick", "other_person",
        ):
            self.assertIn(expected, keys)

    def test_any_key_returns_the_same_defaults(self):
        """人格选择没有了：传什么都不影响，拿到的都是这一套。"""
        for name in (None, "", "tsundere", "neutral", "不存在的性格"):
            self.assertEqual(get_preset(name).key, DEFAULT_PRESET, name)


class TestNoSpeechStyle(unittest.TestCase):
    """表达基调 / 说话方式必须彻底消失。"""

    def test_preset_has_no_style_fields(self):
        preset = get_preset()
        self.assertFalse(hasattr(preset, "style_overrides"))
        self.assertFalse(hasattr(preset, "style_note"))

    def test_rules_carry_no_style_text(self):
        for rule in TRIGGER_RULES:
            self.assertFalse(hasattr(rule, "style"), rule.key)

    def test_module_has_no_style_tables(self):
        for name in ("TSUNDERE_STYLE", "TSUNDERE_RULES"):
            self.assertFalse(hasattr(presets_mod, name), name)

    def test_custom_preset_file_api_is_gone(self):
        """自定义人格文件（presets/*.json）的读写入口要一个不剩。"""
        for name in (
            "load_custom_presets",
            "save_custom_preset",
            "delete_custom_preset",
            "preset_to_dict",
            "preset_from_dict",
            "all_presets",
            "preset_dir",
            "PRESET_DIR_NAME",
        ):
            self.assertFalse(hasattr(presets_mod, name), name)

    def test_self_feedback_helpers_still_here(self):
        """自表达回读只喂情绪状态，不进提示词 —— 它必须还在。"""
        self.assertTrue(callable(presets_mod.heuristic_self_emotion))
        self.assertIsNotNone(presets_mod.EMOTION_TAG_RE)
        self.assertTrue(presets_mod.SELF_HINTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
