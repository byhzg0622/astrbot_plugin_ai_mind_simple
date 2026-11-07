"""_conf_schema.json 的结构校验。

这个文件写错了不会报错在单元测试里，而是**直接让插件装不上** ——
AstrBot 会抛 KeyError: 'type'，安装界面只显示「加载插件时出现问题」。
2026-09 就踩过一次：加新分组时写成了扁平结构，少了 type/items 外层。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"

#: 类型 -> 默认值应该是什么
TYPE_CHECKS = {
    "bool": lambda v: isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "list": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


class ConfigSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_every_node_has_type(self) -> None:
        """每个节点都必须有 type；object 必须有 items。缺一个插件就装不上。"""
        problems: list[str] = []

        def walk(node: dict, trail: str = "") -> None:
            for key, value in node.items():
                here = f"{trail}.{key}" if trail else key
                if not isinstance(value, dict):
                    problems.append(f"{here}（值不是对象）")
                    continue
                kind = value.get("type")
                if not kind:
                    problems.append(f"{here}（缺 type）")
                    continue
                if kind == "object":
                    items = value.get("items")
                    if not isinstance(items, dict) or not items:
                        problems.append(f"{here}（object 缺 items）")
                    else:
                        walk(items, here)

        walk(self.schema)
        self.assertEqual(problems, [], "schema 结构不对，插件会加载失败：" + "；".join(problems))

    def test_defaults_match_declared_type(self) -> None:
        """默认值类型要和 type 对得上 —— 对不上时 WebUI 里的控件会错乱。"""
        problems: list[str] = []

        def walk(node: dict, trail: str = "") -> None:
            for key, value in node.items():
                here = f"{trail}.{key}" if trail else key
                if not isinstance(value, dict):
                    continue
                kind = value.get("type")
                if kind == "object":
                    walk(value.get("items") or {}, here)
                    continue
                if "default" not in value or kind not in TYPE_CHECKS:
                    continue
                if not TYPE_CHECKS[kind](value["default"]):
                    problems.append(f"{here}（{kind} 的默认值是 {value['default']!r}）")

        walk(self.schema)
        self.assertEqual(problems, [], "默认值类型不对：" + "；".join(problems))

    def test_options_contain_default(self) -> None:
        """下拉框的默认值必须在选项里，否则 WebUI 会显示成空白。"""
        problems: list[str] = []

        def walk(node: dict, trail: str = "") -> None:
            for key, value in node.items():
                here = f"{trail}.{key}" if trail else key
                if not isinstance(value, dict):
                    continue
                if value.get("type") == "object":
                    walk(value.get("items") or {}, here)
                    continue
                options = value.get("options")
                if isinstance(options, list) and options and "default" in value:
                    if value["default"] not in options:
                        problems.append(f"{here}（默认值 {value['default']!r} 不在选项里）")

        walk(self.schema)
        self.assertEqual(problems, [], "下拉框默认值有问题：" + "；".join(problems))

    def test_types_are_supported_by_astrbot(self) -> None:
        """抄自 astrbot/core/config/default.py 的 DEFAULT_VALUE_MAP。

        写了它不认识的字会抛 TypeError，提示「不受支持的配置类型」。
        """
        supported = {
            "int", "float", "bool", "string", "text", "list",
            "file", "object", "template_list", "dict",
        }
        used: set[str] = set()

        def walk(node: dict) -> None:
            for value in node.values():
                if not isinstance(value, dict):
                    continue
                kind = value.get("type")
                if kind:
                    used.add(kind)
                if kind == "object":
                    walk(value.get("items") or {})

        walk(self.schema)
        self.assertTrue(used <= supported, f"用了 AstrBot 不认识的类型：{sorted(used - supported)}")

    def test_simulate_astrbot_parse(self) -> None:
        """完全照抄 astrbot_config._config_schema_to_default_config 的逻辑跑一遍。

        真出问题的地方就是这里：它直接取 v["type"]，缺了就是 KeyError: 'type'，
        而安装界面只会显示一句「加载插件时出现问题」。
        """
        default_map = {
            "int": 0, "float": 0.0, "bool": False, "string": "", "text": "",
            "list": [], "file": [], "object": {}, "template_list": [], "dict": {},
        }

        def parse(node: dict) -> dict:
            conf: dict = {}
            for key, value in node.items():
                kind = value["type"]  # 少了就是 KeyError: 'type'
                if kind not in default_map:
                    raise TypeError(f"不受支持的配置类型 {kind}")
                default = value["default"] if "default" in value else default_map[kind]
                if kind == "object":
                    conf[key] = {}
                    conf[key].update(parse(value["items"]))
                else:
                    conf[key] = default
            return conf

        conf = parse(self.schema)
        self.assertEqual(sorted(conf), sorted(self.schema))
        self.assertIs(conf["enabled"], True)
        self.assertIs(conf["emotion"]["enabled"], True)
        self.assertIs(conf["memory"]["enabled"], True)
        self.assertEqual(conf["panel"]["curve_hours"], 72)


if __name__ == "__main__":
    unittest.main()
