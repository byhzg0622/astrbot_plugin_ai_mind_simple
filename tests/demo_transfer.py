"""备份迁移演示：导出 → 拷到「新机器」→ 连着导三次 → 打印核对结果。

它跑的是真接口、真临时库（和 tests/test_transfer.py 同一套夹具），
所以能当作「搬迁前先在这台机器上试一遍」的自检工具：

    python3 tests/demo_transfer.py

输出里每一行都有「核对」两个字才算数：设置逐项读回、记忆逐条比对、图谱逐节点对比。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from mind.graph import build_graph  # noqa: E402
from mind.panel import session_entries  # noqa: E402
from tests.test_plugin_smoke import ConfigWithSave, PluginHarness  # noqa: E402

SEED = (
    {"content": "她怕打雷", "scope": "global", "session_id": "", "owner_id": ""},
    {"content": "这个群每周五聚餐", "scope": "session",
     "session_id": "aiocqhttp:GroupMessage:777", "owner_id": ""},
    {"content": "他叫小明", "scope": "user", "session_id": "", "owner_id": "12345"},
)


class DemoHarness(PluginHarness):
    """演示脚本用的夹具：Harness.api() 里用了 unittest 的断言，这里补两个最小实现。"""

    def assertIn(self, member, container, msg: str = "") -> None:  # noqa: D102
        if member not in container:
            raise AssertionError(f"{member!r} 不在 {container!r} 里 {msg}")


def shape(store) -> tuple:
    graph = build_graph(store, session_id="", limit=200)
    nodes = sorted((n["type"], n["label"]) for n in graph["nodes"])
    return nodes, len(graph["edges"])


def main() -> int:
    harness = DemoHarness()
    harness.setUp()
    try:
        Memory = harness.module.Memory
        print("[1/5] 源机器：塞 3 条记忆（通用 / 会话 / 用户），再改 1 项设置")
        src = harness.make_plugin()
        asyncio.run(src.initialize())
        for index, item in enumerate(SEED):
            src.store.add(Memory(
                content=item["content"], keywords=("演示",), kind="fact",
                scope=item["scope"], session_id=item["session_id"],
                owner_id=item["owner_id"], importance=0.7, source="manual",
            ))
        src.config = ConfigWithSave(src.config)
        src._set_config_value("emotion.dynamics.mood_cold_after_minutes", 45)
        src.config.save_config()

        export = harness._body(asyncio.run(
            harness.api(src, "transfer/export", body={})
        ))
        if not export.get("ok"):
            print(f"      ❌ 导出失败：{export}")
            return 1
        bundle_path = Path(export["path"])
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        print(f"      → {bundle_path.name}（{export['bytes']} 字节）")
        print(f"        记忆 {bundle['counts']['memories']} 条"
              f"（通用 {bundle['counts']['global']}）· 设置 {bundle['counts']['settings']} 项")

        print("[2/5] 新机器（全新数据目录）：把文件拷进 exports/ → 按文件名导入")
        dst = harness.make_plugin()
        asyncio.run(dst.initialize())
        dst.config = ConfigWithSave(dst.config)
        export_dir = Path(dst.data_dir) / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundle_path, export_dir / bundle_path.name)
        first = harness._body(asyncio.run(
            harness.api(dst, "transfer/import", body={"path": bundle_path.name})
        ))
        if not first.get("ok"):
            print(f"      ❌ 导入没通过核对：{first}")
            return 1
        print(f"        记忆 +{first['memories']['added']}"
              f"（重复 {first['memories']['duplicates']}，现在共 {first['memories']['total']}）"
              f" · 设置 {first['settings']['applied']} 项"
              f" · 核对 {first['verify']['found']}/{first['verify']['expected']} 通过")
        dormant = first["memories"].get("dormant") or {}
        if dormant.get("memories"):
            print(f"        待激活 {dormant['memories']} 条"
                  f"（{dormant['sessions_total']} 个会话这台机器还没聊过 —— "
                  f"存着不丢，聊到就自动读）")

        print("[3/5] 同一个备份再导两次（幂等：条数不许涨）")
        for round_no in (2, 3):
            again = harness._body(asyncio.run(
                harness.api(dst, "transfer/import", body={"path": bundle_path.name})
            ))
            print(f"        第 {round_no} 次：+{again['memories']['added']}"
                  f" · 总数 {again['memories']['total']}"
                  f" · {'核对通过' if again['ok'] else '核对没通过'}")
            if not again["ok"] or again["memories"]["added"] != 0:
                print("      ❌ 导入不是幂等的")
                return 1

        print("[4/5] 记忆图谱（按记忆现算）+ 设置逐项核对")
        src_shape, dst_shape = shape(src.store), shape(dst.store)
        print(f"        图谱：源 {len(src_shape[0])} 节点/{src_shape[1]} 边"
              f" → 新 {len(dst_shape[0])} 节点/{dst_shape[1]} 边"
              f" {'✅ 一致' if src_shape == dst_shape else '❌ 不一致'}")
        bad = []
        for key in sorted(src._settings_leaf_paths()):
            want = harness.module.cfg_get(src.config, key, None)
            if want is None:
                continue
            got = harness.module.cfg_get(dst.config, key, None)
            if not src._value_matches(want, got):
                bad.append(f"{key}: {want!r} → {got!r}")
        print(f"        设置：{len(src._settings_leaf_paths())} 项，"
              f"{'✅ 全部一致' if not bad else '❌ 对不上：' + '；'.join(bad[:3])}")
        print(f"        热更新：新机器的心境超基线触发时长 = "
              f"{dst.settings.emotion.mood_cold_after_minutes} 分钟（应为 45）")

        print("[5/5] 会话下拉：没聊过的会话要看得见，并且标着「（未激活）」")

        def dropdown() -> dict:
            return {
                item["key"]: item for item in session_entries(
                    engine=dst.engine, sample_store=dst.samples, memory_store=dst.store,
                )
            }

        group = str(SEED[1]["session_id"])
        for item in dropdown().values():
            mark = "（未激活）" if item.get("active") is False else ""
            print(f"        {item['label']}{mark}")
        before_active = (dropdown().get(group) or {}).get("active", None)
        # 激活：那条会话里来了第一条消息
        asyncio.run(harness.send(dst, "周六几点出发", umo=group))
        after_active = (dropdown().get(group) or {}).get("active", None)
        marker_ok = (before_active is False) and bool(after_active)
        print(f"        发一条消息之后 → "
              f"{'标记消失（已激活）✅' if after_active else '❌ 还是未激活'}")
        print(f"        标记：{'✅ 先标（未激活）、聊过就消失' if marker_ok else '❌ 标记不对'}")

        ok = ((src_shape == dst_shape) and not bad
              and dst.settings.emotion.mood_cold_after_minutes == 45
              and marker_ok)
        print("")
        print("✅ 搬迁链路跑通：导出 → 拷文件 → 导入 ×3 → 核对 + 会话标记全部通过"
              if ok else "❌ 有核对没通过，别搬")
        asyncio.run(src.terminate())
        asyncio.run(dst.terminate())
        return 0 if ok else 1
    finally:
        harness.tearDown()


if __name__ == "__main__":
    sys.exit(main())
