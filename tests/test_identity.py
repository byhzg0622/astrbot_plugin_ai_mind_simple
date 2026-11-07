"""署名一致性检查。

署名同时出现在四个地方：metadata.yaml（插件页展示）、main.py（注册装饰器）、
面板页面（给人的署名）、README 顶部。改的时候很容易只改一处 —— 这里卡住它。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

AUTHOR = "挽风随行+DSH(主代码编写)、冰与火之歌"


class TestAuthorSignature(unittest.TestCase):
    def test_metadata_yaml(self):
        text = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("author: " + AUTHOR, text)

    def test_readme(self):
        text = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn(AUTHOR, text, "README 顶部也要写全开发者")

    def test_main_register_decorator(self):
        text = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
        self.assertIn('PLUGIN_AUTHOR = "' + AUTHOR + '"', text)
        self.assertIn("PLUGIN_AUTHOR,", text, "注册装饰器应当用 PLUGIN_AUTHOR")

    def test_panel_footer(self):
        text = (PLUGIN_DIR / "pages" / "mind" / "index.html").read_text(encoding="utf-8")
        self.assertIn(AUTHOR, text, "面板上也要显示署名")

    def test_namespace_stays_ascii(self):
        """署名可以随便改，但路由命名空间必须是 ASCII —— 它会被拼进 URL。"""
        text = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
        match = re.search(r'PLUGIN_ID = "([^"]+)"', text)
        self.assertIsNotNone(match, "找不到 PLUGIN_ID")
        namespace = match.group(1)
        namespace = namespace.replace('" + PLUGIN_NAME + "', "astrbot_plugin_ai_mind")
        self.assertTrue(namespace.isascii(), f"命名空间含非 ASCII 字符：{namespace}")
        self.assertNotIn("(", namespace)
        self.assertNotIn(")", namespace)


if __name__ == "__main__":
    unittest.main(verbosity=2)
