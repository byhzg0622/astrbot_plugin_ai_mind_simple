"""隐私：拒绝名单 / 忘我 / 导出。"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

_spec = importlib.util.spec_from_file_location(
    "smoke_privacy", PLUGIN_DIR / "tests" / "test_plugin_smoke.py"
)
smoke = importlib.util.module_from_spec(_spec)
sys.modules["smoke_privacy"] = smoke
_spec.loader.exec_module(smoke)

FakeEvent = smoke.FakeEvent
FakeProviderRequest = smoke.FakeProviderRequest
FakeResult = smoke.FakeResult
Plain = smoke.Plain


class PrivacyTest(smoke.PluginHarness, unittest.TestCase):
    def seed(self, plugin, uid: str = smoke.OWNER, count: int = 3) -> None:
        from mind import Memory

        for index in range(count):
            plugin.store.add(Memory(
                content="第 %d 条关于他的事，写得长一点好通过校验" % index,
                keywords=("测试",),
                session_id=smoke.PRIVATE,
                owner_id=uid,
                scope="user",
            ))

    def test_summary_counts(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.seed(plugin, smoke.OWNER, 3)
            res = await self.api(plugin, "privacy", query={"uid": smoke.OWNER})
            self.assertTrue(res["ok"])
            self.assertEqual(res["data"]["summary"]["memories"], 3)
            self.assertEqual(res["data"]["denied"], [])
            await plugin.terminate()

        self.run_async(scenario())


    def test_forget_requires_uid(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            res = await self.api(plugin, "privacy/forget", body={})
            self.assertFalse(res["ok"])
            await plugin.terminate()

        self.run_async(scenario())

    def test_deny_list_round_trip(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            res = await self.api(plugin, "privacy/deny", body={"uid": "999", "action": "add"})
            self.assertIn("999", res["data"]["denied"])
            self.assertTrue(plugin._is_denied("999"))
            res = await self.api(plugin, "privacy/deny", body={"uid": "999", "action": "remove"})
            self.assertNotIn("999", res["data"]["denied"])
            self.assertFalse(plugin._is_denied("999"))
            await plugin.terminate()

        self.run_async(scenario())


    def test_denied_owner_memories_are_filtered(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.seed(plugin, "999", 2)
            kept = plugin._filter_denied_memories(plugin.store.list_memories(limit=10))
            self.assertTrue(kept, "没在拒绝名单里的应当保留")
            await self.api(plugin, "privacy/deny", body={"uid": "999", "action": "add"})
            kept = plugin._filter_denied_memories(plugin.store.list_memories(limit=10))
            self.assertEqual(kept, [], "关于被拒绝的人的记忆不该再被注入")
            await plugin.terminate()

        self.run_async(scenario())

    def test_export_contains_the_archive(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.seed(plugin, smoke.OWNER, 2)
            res = await self.api(plugin, "privacy/export", body={"uid": smoke.OWNER})
            self.assertTrue(res["ok"])
            archive = res["data"]["archive"]
            self.assertEqual(archive["uid"], smoke.OWNER)
            self.assertEqual(len(archive["memories"]), 2)
            self.assertEqual(archive["memories"][0]["scope"], "user")
            await plugin.terminate()

        self.run_async(scenario())

    def test_save_settings_round_trip(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            res = await self.api(plugin, "privacy/save", body={"settings": {
                "allow_self_delete": False, "consent_required": True,
            }})
            self.assertTrue(res["ok"], res)
            self.assertFalse(plugin.settings.privacy.allow_self_delete)
            self.assertTrue(plugin.settings.privacy.consent_required)
            self.assertTrue((plugin.data_dir / "privacy_overrides.json").is_file())
            res = await self.api(plugin, "privacy/save", body={"settings": {"没这个键": 1}})
            self.assertFalse(res["ok"])
            await plugin.terminate()

        self.run_async(scenario())

    def test_forget_me_command(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.seed(plugin, smoke.OWNER, 2)
            event = FakeEvent("忘我", umo=smoke.PRIVATE, uid=smoke.OWNER)
            text = await plugin._forget_me_text(event)
            self.assertIn("删干净了", text)
            self.assertIn("记忆 2 条", text)
            self.assertEqual(plugin.store.stats()["total"], 0)
            # 再删一次：本来就没有了
            text = await plugin._forget_me_text(event)
            self.assertIn("本来就没有", text)
            await plugin.terminate()

        self.run_async(scenario())

    def test_forget_me_can_be_disabled(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin({"privacy": {"allow_self_delete": False}})
            await plugin.initialize()
            event = FakeEvent("忘我", umo=smoke.PRIVATE, uid=smoke.OWNER)
            text = await plugin._forget_me_text(event)
            self.assertIn("不允许", text)
            await plugin.terminate()

        self.run_async(scenario())

    def test_hostile_uid_is_handled(self) -> None:
        """奇怪的用户 ID 不能把删除逻辑搞崩，也不能靠字符串就删掉别人的数据。"""

        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            self.seed(plugin, smoke.OWNER, 2)
            # 空 ID 直接拒绝
            for uid in ("", "   "):
                res = await self.api(plugin, "privacy/forget", body={"uid": uid})
                self.assertFalse(res["ok"], repr(uid))
            # 长得像注入的 ID：参数化查询会当成普通字符串，删不掉任何东西，表也得还在
            for uid in ("'; DROP TABLE memories; --", "🙂", "1 OR 1=1"):
                res = await self.api(plugin, "privacy/forget", body={"uid": uid})
                self.assertTrue(res["ok"], uid)
                self.assertEqual(res["data"]["removed"]["memories"], 0, uid)
            self.assertEqual(plugin.store.stats()["total"], 2, "别人的记忆不该被误删")
            await plugin.terminate()

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)

