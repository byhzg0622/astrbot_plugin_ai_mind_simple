"""面板按钮/接口的逐个回归：**每个按钮都要有正常返回**。

对应用户反馈：

- 导出档案要给出文件位置（写盘 + 回路径）；
- 设置改完不能自己弹回（保存后逐项读回核对，没存上的要点名）；
- 情绪曲线下方的按钮（集体冻结、清理…）一个都不能哑火。

运行::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import json
import sys
import time
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from tests.test_plugin_smoke import (  # noqa: E402
    ConfigWithSave,
    OWNER,
    PRIVATE,
    PluginHarness,
)

GROUP_SESSION = "aiocqhttp:GroupMessage:123456"

#: 面板上每个按钮背后的接口（和 _route_defs 一一对应，增减了这里就会红）
AUDITED_ROUTES = {
    "sessions", "settings", "settings/save", "settings/reset",
    "privacy", "privacy/save", "privacy/deny", "privacy/forget", "privacy/export",
    "transfer/export", "transfer/import",
    "manage", "manage/memory", "manage/data",
    "emotion", "emotion/set", "emotion/mood", "emotion/intensity", "emotion/point",
    "emotion/reset", "emotion/curve/clear", "emotion/freeze",
    "memory", "memory/add", "memory/update", "memory/delete", "memory/batch",
    "graph", "_echo",
}


def flatten_fields(payload: dict) -> dict:
    """把设置页 payload 摊平成 {路径: 字段}。"""
    out: dict = {}

    def walk(nodes) -> None:
        for node in nodes:
            if node.get("children"):
                walk(node["children"])
            else:
                out[node["path"]] = node

    for group in payload.get("groups", []):
        walk(group.get("fields", []))
    return out


class PanelButtonsAnswerTest(PluginHarness, unittest.TestCase):
    """面板上每一个按钮调的接口，都必须正常返回 ok。"""

    def test_every_route_is_audited(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            routes = {name for name, _handler, _desc in plugin._route_defs()}
            self.assertEqual(
                routes, AUDITED_ROUTES,
                "后端接口增减了，记得同步这份「按钮逐个试一遍」的清单",
            )
            await plugin.terminate()

        self.run_async(scenario())

    def test_every_button_endpoint_returns_ok(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.config = ConfigWithSave(plugin.config, "cfg.json")
            await self.send(plugin, "在吗")
            from mind import Memory, PAD

            for index in range(2):
                plugin.store.add(Memory(
                    content=f"按钮审计用记忆 {index}", session_id=PRIVATE,
                    owner_id=OWNER, importance=0.3,
                ))
            plugin.engine.resolve(PRIVATE)
            plugin.samples.append(PRIVATE, 1000.0, PAD.of(0.4, 0.1, 0.2))

            listing = await self.api(plugin, "memory", query={"limit": 5})
            items = (listing.get("data") or {}).get("items") or []
            self.assertGreaterEqual(len(items), 2, "先造两条记忆出来")
            mem_id = items[0]["id"]
            mem_id2 = items[1]["id"]

            # 顺序有讲究：会删数据的（忘记 / 清空）放在**改数据的**后面，
            # 否则编辑、批量这些按钮跑到一半目标就没了 —— 那就不叫「都能用」。
            calls: list[tuple[str, dict, str]] = [
                ("sessions", {}, "顶部会话下拉"),
                ("settings", {}, "通用配置"),
                ("settings/save", {"values": {"advanced.debug_log": False}}, "保存改动"),
                ("memory", {"session": PRIVATE}, "记忆列表"),
                ("memory/add", {"content": "审计手动记忆", "session": PRIVATE}, "手动添加"),
                ("memory/update", {"id": mem_id, "content": "改过的记忆"}, "编辑记忆"),
                ("memory/batch", {"ids": [mem_id2], "action": "scope", "scope": "global"},
                 "设为通用"),
                ("memory/batch", {"ids": [mem_id2], "action": "delete"}, "删除选中"),
                ("memory/delete", {"id": mem_id}, "删除记忆"),
                ("graph", {"session": PRIVATE}, "记忆图谱"),
                ("privacy", {}, "隐私页"),
                ("privacy/save", {"settings": {"allow_admin_export": True}}, "保存隐私设置"),
                ("privacy/deny", {"uid": OWNER, "action": "add"}, "加入拒绝名单"),
                ("privacy/deny", {"uid": OWNER, "action": "remove"}, "移出拒绝名单"),
                ("privacy/export", {"uid": OWNER}, "导出档案"),
                ("transfer/export", {}, "导出备份"),
                ("transfer/import", {"bundle": {
                    "kind": plugin.BACKUP_KIND,
                    "settings": {"advanced.debug_log": False},
                    "memories": [],
                }}, "导入备份"),
                ("privacy/forget", {"uid": OWNER}, "删掉这个人的数据"),
                ("manage", {"session": PRIVATE}, "管理页"),
                ("manage/memory", {"action": "clear_session", "session": PRIVATE}, "清空本会话记忆"),
                ("manage/memory", {"action": "clear_all"}, "清空全部记忆"),
                ("manage/memory", {"action": "prune", "max_importance": 0.9}, "按条件清理"),
                ("manage/data", {"action": "prune_samples", "keep": 1500}, "压缩曲线"),
                ("manage/data", {"action": "clear_lexicon"}, "清空词表命中"),
                ("manage/data", {"action": "freeze_all"}, "冻结全部会话"),
                ("manage/data", {"action": "thaw_all"}, "解冻全部会话"),
                ("manage/data", {"action": "vacuum"}, "整理数据库"),
                ("emotion", {"session": PRIVATE}, "情绪页"),
                ("emotion/set", {"session": PRIVATE, "p": 0.3, "a": 0.1, "d": 0.2,
                                "intensity": 0.5}, "设为此刻心情"),
                ("emotion/mood", {"session": PRIVATE, "word": "content"},
                 "一键改成：心满意足"),
                ("emotion/mood", {"session": PRIVATE,
                                  "offset": {"p": 0.2, "a": -0.1, "d": 0.1}},
                 "调节心境滑杆"),
                ("emotion/mood", {"session": PRIVATE, "action": "reset"}, "重置心境"),
                ("emotion/intensity", {"session": PRIVATE, "intensity": 0.6}, "强度滑块"),
                ("emotion/point", {"session": PRIVATE, "action": "insert", "t": 1000.5,
                                  "p": 0.1, "a": 0.1, "d": 0.1}, "插入曲线点"),
                ("emotion/point", {"session": PRIVATE, "action": "update", "t": 1000.5,
                                  "p": 0.2, "a": 0.2, "d": 0.2}, "修改曲线点"),
                ("emotion/point", {"session": PRIVATE, "action": "delete", "t": 1000.5},
                 "删除曲线点"),
                ("emotion/reset", {"session": PRIVATE}, "重置情绪"),
                ("emotion/curve/clear", {"session": PRIVATE}, "清空曲线"),
                ("emotion/freeze", {"session": PRIVATE, "frozen": True}, "冻结"),
                ("emotion/freeze", {"session": PRIVATE, "frozen": False}, "解冻"),
                ("_echo", {}, "自检"),
            ]

            audited: set[str] = set()
            for path, body, label in calls:
                res = await self.api(plugin, path, body=body)
                self.assertTrue(
                    (res or {}).get("ok"),
                    f"「{label}」({path}) 没有正常返回：{res}",
                )
                audited.add(path)

            # 恢复默认放在最后（它会把配置全写回默认值）
            res = await self.api(plugin, "settings/reset", body={})
            self.assertTrue(res.get("ok"), res)
            audited.add("settings/reset")
            self.assertEqual(
                AUDITED_ROUTES - audited, set(),
                f"这些接口没被试到：{sorted(AUDITED_ROUTES - audited)}",
            )
            await plugin.terminate()

        self.run_async(scenario())

    def test_point_edit_also_touches_intensity_marks(self) -> None:
        """「删除附近的点 / 改成当前值 / 插点」要同时动强度锚点。

        报过的 bug：两个按钮只改 PAD 采样点，强度那条线怎么点都不变。
        """

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.send(plugin, "你好棒啊真的好厉害")   # 攒一条带强度的事件
            session = plugin.engine.resolve(PRIVATE, time.time())
            self.assertTrue(session.history, "先攒一条事件")
            event = session.history[-1]
            t = event.t

            # 1) 改成当前滑块的值 → 附近强度锚点统一改成当前强度
            res = self._body(await self.api(plugin, "emotion/point", body={
                "session": PRIVATE, "action": "update", "t": t, "tolerance": 300,
                "p": 0.1, "a": 0.1, "d": 0.1,
            }))
            self.assertTrue(res.get("ok"), res)
            self.assertGreaterEqual(
                res.get("changed_intensity", 0), 1, "改点没有动到强度锚点",
            )
            self.assertAlmostEqual(
                event.intensity, session.intensity, places=4,
                msg="强度锚点没有改成当前强度",
            )

            # 2) 删除附近的点 → 强度锚点作废（-1，重建曲线时跳过）
            res2 = self._body(await self.api(plugin, "emotion/point", body={
                "session": PRIVATE, "action": "delete", "t": t, "tolerance": 300,
            }))
            self.assertTrue(res2.get("ok"), res2)
            self.assertGreaterEqual(
                res2.get("removed_intensity", 0), 1, "删点没有作废强度锚点",
            )
            self.assertEqual(event.intensity, -1.0, "强度锚点没被作废")
            payload = self._body(await self.api(plugin, "emotion", query={"session": PRIVATE}))
            curve = payload.get("intensity_curve") or []
            self.assertTrue(curve, "强度曲线没了")
            self.assertTrue(
                all(0.0 <= point["v"] <= 1.0 for point in curve),
                "强度曲线出现了越界值",
            )

            # 3) 插点 → 补一个锚点，事件流里多一条「在曲线上手动插点」
            res3 = self._body(await self.api(plugin, "emotion/point", body={
                "session": PRIVATE, "action": "insert", "t": t - 60, "tolerance": 300,
                "p": 0.2, "a": 0.0, "d": 0.0,
            }))
            self.assertTrue(res3.get("ok"), res3)
            self.assertGreaterEqual(res3.get("inserted_intensity", 0), 1)
            payload2 = self._body(await self.api(plugin, "emotion", query={"session": PRIVATE}))
            causes = [item["cause"] for item in payload2["events"]]
            self.assertIn("在曲线上手动插点", causes, "插点没进事件流")
            await plugin.terminate()

        self.run_async(scenario())

    def test_mood_freeze_button_api(self) -> None:
        """心境冻结按钮：action=freeze/unfreeze；判定同情绪冻结（心境停、情绪照走）。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.send(plugin, "在吗")

            res = self._body(await self.api(
                plugin, "emotion/mood", body={"session": PRIVATE, "action": "freeze"},
            ))
            self.assertTrue(res.get("ok"), res)
            self.assertTrue(res.get("mood_frozen"), res)
            self.assertFalse(res.get("session_frozen"), "心境冻结不该动会话级冻结")

            payload = self._body(await self.api(plugin, "emotion", query={"session": PRIVATE}))
            self.assertTrue(
                (payload.get("mood") or {}).get("frozen"),
                "面板读不到心境冻结状态",
            )
            mood_before = (payload["mood"]["p"], payload["mood"]["a"], payload["mood"]["d"])

            plugin.engine.tick_all(time.time() + 7200)
            after = self._body(await self.api(plugin, "emotion", query={"session": PRIVATE}))
            self.assertEqual(
                (after["mood"]["p"], after["mood"]["a"], after["mood"]["d"]),
                mood_before,
                "冻结后心境还在动",
            )
            self.assertNotEqual(after["current"]["p"], payload["current"]["p"],
                                "情绪应该照常衰减")

            res2 = self._body(await self.api(
                plugin, "emotion/mood", body={"session": PRIVATE, "action": "unfreeze"},
            ))
            self.assertTrue(res2.get("ok"), res2)
            self.assertFalse(res2.get("mood_frozen"), res2)
            await plugin.terminate()

        self.run_async(scenario())

    def test_freeze_all_and_thaw_all_answer_with_session_count(self) -> None:
        """集体冻结 / 解冻全部：真的动了状态，还要回「几个会话」。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.engine.resolve(PRIVATE)
            plugin.engine.resolve(GROUP_SESSION)

            res = await self.api(plugin, "manage/data", body={"action": "freeze_all"})
            self.assertTrue(res.get("ok"), res)
            data = res["data"]
            self.assertEqual(data["frozen"], 2)
            self.assertEqual(data["sessions"], 2, "前端按 sessions 显示「N 个会话」")
            self.assertEqual(len(plugin.engine.frozen), 2)

            held = {key: round(session.intensity, 4)
                    for key, session in plugin.engine.sessions.items()}
            plugin.engine.tick_all(time.time() + 7200)
            for key, session in plugin.engine.sessions.items():
                self.assertAlmostEqual(
                    session.intensity, held[key], places=4,
                    msg=f"{key} 冻结了强度还在衰减",
                )

            res = await self.api(plugin, "manage/data", body={"action": "thaw_all"})
            self.assertTrue(res.get("ok"), res)
            self.assertEqual(res["data"]["sessions"], 2)
            self.assertEqual(plugin.engine.frozen, set())
            await plugin.terminate()

        self.run_async(scenario())

    def test_export_writes_a_file_and_tells_you_where(self) -> None:
        """导出档案必须给出位置：路径存在、能打开、条数对得上。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await self.send(plugin, "我叫小明，喜欢冰美式")

            res = await self.api(plugin, "privacy/export", body={"uid": OWNER})
            self.assertTrue(res.get("ok"), res)
            payload_data = res.get("data") or {}
            path = payload_data.get("path")
            self.assertTrue(path, "导出没给文件位置")
            target = Path(path)
            self.assertTrue(target.is_file(), f"导出的文件不存在：{path}")
            self.assertGreater(payload_data.get("bytes", 0), 0)
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["uid"], OWNER)
            self.assertIn("memories", payload)
            self.assertEqual(payload_data["memories"], len(payload["memories"]))
            self.assertIn("exports", target.parts)

            bad = await self.api(plugin, "privacy/export", body={"uid": ""})
            self.assertFalse(bad.get("ok"), "没给 uid 要报错，不能假装成功")
            await plugin.terminate()

        self.run_async(scenario())


class SessionDropdownLabelTest(PluginHarness, unittest.TestCase):
    """会话下拉里的情绪要和面板对得上，不能清一色「平静」。"""

    def test_label_prefers_hint_then_mood_then_calm(self) -> None:
        async def scenario() -> None:
            from mind import PAD
            from mind.panel import session_entries

            plugin = self.make_plugin()
            await plugin.initialize()
            now = time.time()
            session = plugin.engine.resolve(PRIVATE, now)

            def label_at(moment: float) -> str:
                entries = session_entries(
                    engine=plugin.engine, sample_store=plugin.samples,
                    memory_store=plugin.store, now=moment,
                )
                mine = [item for item in entries if item["key"] == PRIVATE]
                self.assertTrue(mine, "当前会话不在列表里")
                return mine[0]["emotion"]

            # ① 规则声明的情绪还新鲜：和面板一样显示它（老版本这里也是平静）
            session.emotion_hint = "shy"
            session.emotion_hint_until = now + 300
            self.assertEqual(label_at(now), "害羞")

            # ② hint 过期、情绪回到基线：改看**心境**，否则全是平静
            #    （D2 副词：心境离基线很远，所以是「很难过」而不是光秃秃的「难过」）
            session.emotion_hint_until = 0
            session.mood = PAD.of(-0.8, -0.15, -0.6)   # 难过的锚点
            self.assertEqual(label_at(now), "很难过")

            # ③ 情绪与心境都回到基线：这才老老实实显示「平静」
            session.mood = PAD.from_any(plugin.engine.settings.baseline)
            self.assertEqual(label_at(now), "平静")

            # ④ 强度仍然读新的强度模型
            plugin.engine.set_intensity(session, 0.7, now)
            entries = session_entries(
                engine=plugin.engine, sample_store=plugin.samples,
                memory_store=plugin.store, now=now,
            )
            mine = [item for item in entries if item["key"] == PRIVATE][0]
            self.assertAlmostEqual(mine["emotion_intensity"], 0.7, places=3)
            self.assertEqual(mine["emotion_level"], 4)
            await plugin.terminate()

        self.run_async(scenario())


class SettingsStickTest(PluginHarness, unittest.TestCase):
    """设置改完不能自己弹回：保存后逐项读回核对。"""

    def test_save_verifies_and_reports_not_saved(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            plugin.config = ConfigWithSave(plugin.config, "cfg.json")

            # 1) 正常写入：读回全对，not_saved 必须是空的
            res = await self.api(plugin, "settings/save", body={"values": {
                "memory.retrieval.pinned_limit": 30,
                "memory.retrieval.recent_limit": 7,
                "advanced.debug_log": True,
            }})
            self.assertTrue(res.get("ok"), res)
            self.assertEqual(res["data"].get("not_saved"), [], res["data"].get("not_saved"))
            again = await self.api(plugin, "settings", query={})
            fields = flatten_fields(again["data"])
            self.assertEqual(fields["memory.retrieval.pinned_limit"]["value"], 30)
            self.assertEqual(fields["memory.retrieval.recent_limit"]["value"], 7)
            self.assertTrue(fields["advanced.debug_log"]["value"])

            # 2) 配置对象不认这次写入：必须**点名**哪一项没存上，
            #    否则用户看到的就是「改完自己弹回」，还查不出是哪个字段
            class Stubborn(dict):
                """取嵌套值给副本、顶层再吞掉 memory —— 两条写入路径都堵死。"""

                def __init__(self, data):
                    super().__init__(data)
                    self.config_path = "stubborn.json"
                    self.saved = 0

                def save_config(self, *args, **kwargs):
                    self.saved += 1

                def _unwrap(self, value):
                    return copy.deepcopy(value) if isinstance(value, dict) else value

                def __getitem__(self, key):
                    return self._unwrap(super().__getitem__(key))

                def get(self, key, default=None):
                    return self._unwrap(super().get(key, default))

                def __setitem__(self, key, value):
                    if key == "memory":
                        return
                    super().__setitem__(key, value)

            plugin.config = Stubborn(dict(plugin.config))
            res = await self.api(plugin, "settings/save", body={"values": {
                "memory.retrieval.pinned_limit": 77,
                "panel.curve_hours": 48,
            }})
            self.assertTrue(res.get("ok"), res)
            bad = res["data"].get("not_saved") or []
            paths = [item["path"] for item in bad]
            self.assertIn("memory.retrieval.pinned_limit", paths, "没存上的项要点名")
            self.assertNotIn("panel.curve_hours", paths, "存进去的不该被误报")
            hit = next(item for item in bad if item["path"] == "memory.retrieval.pinned_limit")
            self.assertEqual(hit["wanted"], 77)
            await plugin.terminate()

        self.run_async(scenario())

    def test_value_matches_types(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            match = plugin._value_matches
            self.assertTrue(match(12, 12))
            self.assertTrue(match(12.0, 12))
            self.assertTrue(match(True, True))
            self.assertFalse(match(True, 1))
            self.assertTrue(match(["a", "b"], ["a", "b"]))
            self.assertFalse(match(["a"], ["b"]))
            self.assertTrue(match("x", "x"))
            self.assertFalse(match(3, None))
            await plugin.terminate()

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)