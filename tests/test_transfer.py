"""备份与迁移：导出 → 拷到新机器 → 导入（设置 + 全部记忆 + 记忆图谱）。

「搬迁要一次成功」靠的是三件事，用例也照这三件写：

1. **导得全**：配置页每一项设置 + 三种作用域（通用 / 会话 / 用户）的记忆一个不落，
   记忆图谱是按记忆现算的 —— 记忆进去了图就回来了；
2. **导得进**：目标机器是全新数据目录，按 exports/ 下的文件名导入，
   导完**逐项读回设置、逐条比对记忆**，少一条都要报出来；
3. **多来几遍也一样**：同一个备份连导三次，第一次 +N、后面 0，总数不变（幂等）。

运行::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from tests.test_plugin_smoke import ConfigWithSave, PluginHarness  # noqa: E402

from mind.graph import build_graph  # noqa: E402

#: 备份里要出现的三种作用域：通用（全局）、会话、用户
SEED = (
    {"content": "她怕打雷", "scope": "global", "session_id": "", "owner_id": ""},
    {"content": "这个群每周五聚餐", "scope": "session",
     "session_id": "aiocqhttp:GroupMessage:777", "owner_id": ""},
    {"content": "他叫小明", "scope": "user", "session_id": "", "owner_id": "12345"},
)


class TransferTest(PluginHarness, unittest.TestCase):
    """导出 / 导入的端到端（真跑插件、真读写临时库）。"""

    async def _new(self, config: dict | None = None) -> Any:
        plugin = self.make_plugin(config)
        await plugin.initialize()
        return plugin

    def _seed(self, plugin: Any) -> None:
        Memory = self.module.Memory
        for index, item in enumerate(SEED):
            plugin.store.add(Memory(
                content=item["content"],
                keywords=("测试", f"第{index}条"),
                kind="fact",
                scope=item["scope"],
                session_id=item["session_id"],
                owner_id=item["owner_id"],
                importance=0.7,
                pinned=index == 0,
                source="manual",
            ))

    async def _export(self, plugin: Any) -> tuple[dict, Path]:
        res = self._body(await self.api(plugin, "transfer/export", body={}))
        self.assertTrue(res.get("ok"), res)
        path = Path(res["path"])
        self.assertTrue(path.is_file(), "导出必须真的落盘（文件在哪儿要能指出来）")
        self.assertGreater(int(res.get("bytes") or 0), 0)
        bundle = json.loads(path.read_text(encoding="utf-8"))
        return bundle, path

    def _copy_to_target(self, src_file: Path, target: Any) -> str:
        """模拟搬家：把 A 机器导出的文件拷进 B 机器的 exports/ 目录。"""
        export_dir = Path(target.data_dir) / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_file, export_dir / src_file.name)
        return src_file.name

    # -- 导得出 -------------------------------------------------------------
    def test_export_packs_settings_and_every_scope(self) -> None:
        async def scenario() -> None:
            plugin = await self._new()
            self._seed(plugin)
            bundle, path = await self._export(plugin)

            self.assertEqual(
                set(bundle["settings"]), plugin._settings_leaf_paths(),
                "配置页每一项都得进备份（少一项，搬过去就丢了）",
            )
            defaults = plugin._schema_defaults()
            for key, value in bundle["settings"].items():
                current = self.module.cfg_get(plugin.config, key, None)
                want = defaults.get(key) if current is None else current
                self.assertTrue(
                    plugin._value_matches(want, value),
                    f"导出的 {key} 和当前配置对不上：{value!r} != {want!r}",
                )
            self.assertEqual(bundle["counts"]["memories"], len(SEED))
            self.assertEqual(bundle["counts"]["global"], 1, "通用（全局）记忆要被数到")
            self.assertEqual(
                {m["scope"] for m in bundle["memories"]}, {"global", "session", "user"},
            )
            # 向量不进备份：体积大几百倍，过去还得按新模型重算
            self.assertNotIn("embedding", bundle["memories"][0])
            self.assertTrue(path.name.startswith("ai-mind-backup-"), path.name)
            await plugin.terminate()

        self.run_async(scenario())

    def test_bundle_carries_the_plugins_version(self) -> None:
        async def scenario() -> None:
            plugin = await self._new()
            bundle, _path = await self._export(plugin)
            metadata = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
            meta_version = ""
            for line in metadata.splitlines():
                if line.startswith("version:"):
                    meta_version = line.split(":", 1)[1].strip()
            self.assertEqual(bundle["version"], self.module.PLUGIN_VERSION)
            self.assertEqual(
                meta_version, f"v{self.module.PLUGIN_VERSION}",
                "metadata.yaml 和代码里的版本号必须是同一个（备份要能对上号）",
            )
            self.assertEqual(bundle["kind"], self.module.AIMindPlugin.BACKUP_KIND)
            await plugin.terminate()

        self.run_async(scenario())

    # -- 导得进 -------------------------------------------------------------
    def test_round_trip_into_a_fresh_machine(self) -> None:
        async def scenario() -> None:
            src = await self._new()
            self._seed(src)
            # 设置也动一项，好证明「设置」真的被搬过去了
            src.config = ConfigWithSave(src.config)
            res0 = self._body(await self.api(src, "settings/save", body={
                "values": {"emotion.dynamics.mood_cold_after_minutes": 45},
            }))
            self.assertEqual(
                res0.get("saved"), ["emotion.dynamics.mood_cold_after_minutes"], res0
            )
            self.assertEqual(res0.get("not_saved"), [], res0)
            bundle, path = await self._export(src)

            dst = await self._new()
            dst.config = ConfigWithSave(dst.config)
            name = self._copy_to_target(path, dst)
            res = self._body(await self.api(dst, "transfer/import", body={"path": name}))

            self.assertTrue(res.get("ok"), res)
            self.assertEqual(res["memories"]["added"], len(SEED))
            self.assertEqual(res["memories"]["missing"], 0, res["verify"])
            self.assertEqual(res["verify"]["found"], len(SEED))
            self.assertEqual(
                src._existing_memory_keys(), dst._existing_memory_keys(),
                "搬过去之后记忆一条都不能少、也不能多",
            )
            self.assertTrue(res["settings"]["applied"] > 0, res["settings"])
            self.assertEqual(res["settings"]["not_saved"], [], "有设置没写进去")
            self.assertAlmostEqual(
                dst.settings.emotion.mood_cold_after_minutes, 45.0, places=3,
                msg="导入的设置要热更新进运行中的插件",
            )
            for key in src._settings_leaf_paths():
                want = self.module.cfg_get(src.config, key, None)
                if want is None:
                    continue
                got = self.module.cfg_get(dst.config, key, None)
                self.assertTrue(
                    src._value_matches(want, got),
                    f"设置没搬对：{key} = {want!r} → {got!r}",
                )
            await src.terminate()
            await dst.terminate()

        self.run_async(scenario())

    def test_graph_comes_back_with_the_memories(self) -> None:
        """记忆图谱不用单独导：它是按记忆现算的，记忆进去了图就回来了。"""
        async def scenario() -> None:
            src = await self._new()
            self._seed(src)
            _bundle, path = await self._export(src)
            dst = await self._new()
            dst.config = ConfigWithSave(dst.config)
            name = self._copy_to_target(path, dst)
            res = self._body(await self.api(dst, "transfer/import", body={"path": name}))
            self.assertTrue(res.get("ok"), res)

            def shape(store: Any) -> tuple:
                graph = build_graph(store, session_id="", limit=200)
                nodes = sorted((n["type"], n["label"]) for n in graph["nodes"])
                return nodes, len(graph["edges"])

            self.assertEqual(shape(src.store), shape(dst.store),
                             "搬过去之后图谱的节点与连边要一模一样")
            await src.terminate()
            await dst.terminate()

        self.run_async(scenario())

    def test_importing_the_same_backup_three_times_is_stable(self) -> None:
        """连着导三次：第一次 +N，后面两次 0，总数一直不变。"""
        async def scenario() -> None:
            src = await self._new()
            self._seed(src)
            _bundle, path = await self._export(src)
            dst = await self._new()
            dst.config = ConfigWithSave(dst.config)
            name = self._copy_to_target(path, dst)

            totals = []
            for _round in range(3):
                res = self._body(await self.api(dst, "transfer/import", body={"path": name}))
                self.assertTrue(res.get("ok"), res)
                self.assertEqual(res["memories"]["missing"], 0, res["verify"])
                totals.append((res["memories"]["added"], res["memories"]["total"]))

            self.assertEqual(totals[0], (len(SEED), len(SEED)), "第一次该全部加进去")
            self.assertEqual(totals[1][0], 0, "第二次一条都不该重加")
            self.assertEqual(totals[2][0], 0, "第三次同样")
            self.assertEqual({t[1] for t in totals}, {len(SEED)}, "总数不许越导越多")
            self.assertEqual(dst.store.count(), len(SEED))
            await src.terminate()
            await dst.terminate()

        self.run_async(scenario())

    def test_unknown_sessions_are_staged_and_activate_later(self) -> None:
        """搬过去时对方机器上**没有那些会话**：导入不许失败，记忆要存着，聊到就自动读。"""
        group = "aiocqhttp:GroupMessage:999888"

        async def scenario() -> None:
            src = await self._new()
            Memory = self.module.Memory
            src.store.add(Memory(
                content="群里约好周六早上八点爬山", keywords=("爬山",), kind="event",
                scope="session", session_id=group, importance=0.9, pinned=True,
                source="manual",
            ))
            _bundle, path = await self._export(src)

            dst = await self._new()
            dst.config = ConfigWithSave(dst.config)
            name = self._copy_to_target(path, dst)
            res = self._body(await self.api(dst, "transfer/import", body={"path": name}))

            # ① 会话不存在**不是**失败理由：记忆照存，并且报成「待激活」
            self.assertTrue(res.get("ok"), f"会话不存在不该让导入失败：{res}")
            self.assertEqual(res["memories"]["added"], 1)
            self.assertEqual(res["memories"]["dormant"]["sessions"], [group])
            self.assertEqual(res["memories"]["dormant"]["memories"], 1)

            # ② 面板已经能选到它（会话下拉从记忆里取）—— 「暂存」不等于「看不见就丢了」
            from mind.panel import session_entries

            entries = session_entries(
                engine=dst.engine, sample_store=dst.samples, memory_store=dst.store,
            )
            by_key = {item["key"]: item for item in entries}
            self.assertIn(group, by_key, "没聊过的会话也必须出现在下拉里（看得见才不算丢）")
            self.assertFalse(
                by_key[group]["active"],
                "还没聊过 → 下拉里要标成「（未激活）」",
            )
            self.assertEqual(dst._live_sessions(), set(), "口径要和面板一致：一个都没聊过")

            # ③ 维护里的裁剪也不许把它清掉
            self.assertEqual(dst.store.prune(max_memories=3000, importance_floor=0.15), 0)
            self.assertEqual(dst.store.count(), 1)

            # ④ 激活：那条会话里来了第一条消息 → 记忆被自动读进提示词
            req = await self.send(dst, "周六几点出发", umo=group)
            self.assertIn("爬山", self.blocks_of(req))
            # 激活之后：下拉里的「（未激活）」标记要消失，导入报告的口径也跟着变
            entries_after = session_entries(
                engine=dst.engine, sample_store=dst.samples, memory_store=dst.store,
            )
            by_key_after = {item["key"]: item for item in entries_after}
            self.assertTrue(by_key_after[group]["active"], "聊过之后就不是未激活了")
            self.assertIn(group, dst._live_sessions())

            # ⑤ 激活之后再导同一个备份：它不再算「待激活」，也依旧一条不重
            again = self._body(await self.api(dst, "transfer/import", body={"path": name}))
            self.assertTrue(again.get("ok"), again)
            self.assertEqual(again["memories"]["added"], 0)
            self.assertEqual(
                again["memories"]["dormant"]["memories"], 0,
                "会话都聊过了，不该还算待激活",
            )
            await src.terminate()
            await dst.terminate()

        self.run_async(scenario())

    def test_replace_mode_clears_first_and_needs_confirm(self) -> None:
        async def scenario() -> None:
            src = await self._new()
            self._seed(src)
            _bundle, path = await self._export(src)
            dst = await self._new()
            dst.config = ConfigWithSave(dst.config)
            name = self._copy_to_target(path, dst)
            Memory = self.module.Memory
            # 目标机器上有一条**不在备份里**的旧记忆
            dst.store.add(Memory(content="旧机器独有的记忆", kind="fact", scope="global"))

            # ① 不带 confirm：拒绝（不可撤销的操作不能手滑）
            refused = await self.api(dst, "transfer/import", body={
                "path": name, "mode": "replace",
            })
            self.assertFalse(refused["ok"], refused)
            self.assertIn("confirm", str(refused.get("message", "")))
            self.assertEqual(dst.store.count(), 1, "被拒绝的那次不许动库")

            # ② 带 confirm：先清空再导入，旧记忆没了、备份里的都在
            ok = self._body(await self.api(dst, "transfer/import", body={
                "path": name, "mode": "replace", "confirm": True,
            }))
            self.assertTrue(ok.get("ok"), ok)
            self.assertEqual(ok["memories"]["cleared"], 1)
            self.assertEqual(ok["memories"]["added"], len(SEED))
            contents = {m.content for m in dst._iter_all_memories()}
            self.assertNotIn("旧机器独有的记忆", contents)
            self.assertEqual({item["content"] for item in SEED}, contents)
            await src.terminate()
            await dst.terminate()

        self.run_async(scenario())

    # -- 坏输入不许伤到库 ---------------------------------------------------
    def test_bad_bundles_are_refused(self) -> None:
        async def scenario() -> None:
            plugin = await self._new()
            self._seed(plugin)
            before = plugin.store.count()

            cases = [
                ({}, "没拿到备份"),
                ({"bundle": {"kind": "别人的备份"}}, "kind"),
                ({"path": "../../etc/passwd"}, "只允许"),
                ({"path": "根本没有这个文件.json"}, "找不到"),
                ({"memories": [{"content": "裸的"}]}, "kind"),
            ]
            for body, needle in cases:
                res = await self.api(plugin, "transfer/import", body=body)
                self.assertFalse(res["ok"], body)
                self.assertIn(needle, str(res.get("message", "")), body)
            self.assertEqual(plugin.store.count(), before, "被拒绝的导入不许动库")
            await plugin.terminate()

        self.run_async(scenario())

    def test_unwritable_config_stops_before_touching_memories(self) -> None:
        """配置回写不了时要**先停下**：不许留下「设置没进去、记忆进去了」的半截状态。"""
        async def scenario() -> None:
            plugin = await self._new()
            self._seed(plugin)
            before = plugin.store.count()
            # 普通 dict 没有 save_config —— 老版本 AstrBot 就是这个形状
            self.assertFalse(callable(getattr(plugin.config, "save_config", None)))
            bundle = {
                "kind": plugin.BACKUP_KIND,
                "settings": {"emotion.appraisal.lexicon_gain": 0.42},
                "memories": [{"content": "导入不该写进来", "scope": "global"}],
            }
            res = await self.api(plugin, "transfer/import", body={"bundle": bundle})
            self.assertFalse(res["ok"], res)
            self.assertIn("回写", str(res.get("message", "")))
            self.assertEqual(plugin.store.count(), before, "被拒绝的导入一个字都不许写")
            await plugin.terminate()

        self.run_async(scenario())

    def test_bad_setting_values_are_skipped_not_fatal(self) -> None:
        """类型对不上的跳过、认不出的键跳过，其余照常写进去。"""
        async def scenario() -> None:
            plugin = await self._new()
            plugin.config = ConfigWithSave(plugin.config)
            bundle = {
                "kind": self.module.AIMindPlugin.BACKUP_KIND,
                "settings": {
                    "emotion.dynamics.mood_cold_after_minutes": "六个月",
                    "这个配置项.不存在": 1,
                    "emotion.appraisal.lexicon_gain": 0.42,
                },
                "memories": [],
            }
            res = self._body(await self.api(plugin, "transfer/import", body={"bundle": bundle}))
            self.assertTrue(res.get("ok"), f"跳过脏数据不该让整次导入失败：{res}")
            reasons = {item["path"]: item["why"] for item in res["settings"]["skipped"]}
            self.assertIn("emotion.dynamics.mood_cold_after_minutes", reasons)
            self.assertIn("类型对不上", reasons["emotion.dynamics.mood_cold_after_minutes"])
            self.assertIn("这个配置项.不存在", reasons)
            self.assertEqual(res["settings"]["applied"], 1)
            self.assertAlmostEqual(plugin.settings.emotion.lexicon_gain, 0.42, places=3)
            await plugin.terminate()

        self.run_async(scenario())


if __name__ == "__main__":
    unittest.main()
