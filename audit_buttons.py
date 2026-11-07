"""按钮体检：HTML 上每个 <button id> 有没有处理函数、点了有没有可见反馈。

- 处理函数缺失 → 点了完全没反应（这正是报过的 bug）
- 函数体里既不 toast、不改文字、也不调有反馈的辅助函数 → 看着像没反应

用法：python audit_buttons.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TARGET = Path("pages/mind/index.html")

#: 这些辅助函数内部都会给用户一句提示（toast 或写进提示行）
HAS_FEEDBACK = (
    "toast(", "manageCall(", "privacyCall(", "bulkAction(", "gpBatch(",
    "loadSettings(", "renderSettings(", "refreshAll(", "needConfirm(",
)

#: 靠事件委托的（容器上绑一次）
DELEGATED = {
    "data-fold": '分组折叠：绑在 $("st-groups") 上',
}


def main() -> int:
    text = TARGET.read_text(encoding="utf-8")
    html, js = text.split("<script src=", 1)

    buttons = re.findall(r'<button[^>]*\bid="([^"]+)"[^>]*>(.*?)</button>', html, re.S)
    # 动态渲染出来的按钮
    delegated_ids = re.findall(r"<button[^>]*data-fold=[^>]*>", html)

    print(f"HTML 按钮：{len(buttons)} 个；动态按钮：{len(delegated_ids)} 个（分组折叠）\n")

    problems = []
    for bid, label in buttons:
        clean = re.sub(r"<[^>]+>", "", label).strip().replace("\n", " ")[:24]

        # 1) 动态绑定：$("tab-" + name)
        if bid.startswith("tab-") and 'tab-" + name' in js:
            print(f"  {bid:16s} {clean:24s} ✅ 动态绑定（切页签）")
            continue

        # 2) 取出处理函数体（从赋值处往后扫，直到本级 `};`）
        m = re.search(r'\$\("' + re.escape(bid) + r'"\)\.on\w+ = ', js)
        if not m:
            problems.append((bid, clean, "没有处理函数"))
            print(f"  {bid:16s} {clean:24s} ❌ 没有处理函数")
            continue
        body = js[m.end(): m.end() + 700]
        # 单行或行内写法也能覆盖
        if "toast(" in body:
            fb = "✅ toast"
        elif any(h in body for h in ("textContent", "innerHTML", "classList", "style.")):
            fb = "✅ 界面变化"
        elif any(h in body for h in HAS_FEEDBACK):
            fb = "✅ 辅助函数里有提示"
        else:
            fb = "⚠️ 看不出可见反馈"
            problems.append((bid, clean, fb))
        print(f"  {bid:16s} {clean:24s} {fb}")

    # 动态按钮：确认容器上有委托
    fold = '$("st-groups").onclick' in js
    print(f"\n  {'data-fold(动态)':16s} {'分组折叠':24s} " + ("✅ 已委托到 st-groups" if fold else "❌ 没有委托"))
    if not fold:
        problems.append(("data-fold", "分组折叠", "没有委托"))

    print()
    if problems:
        print(f"⚠️  {len(problems)} 个需要处理：")
        for bid, clean, why in problems:
            print(f"   - #{bid:16s} {why:24s} 「{clean}」")
    else:
        print("✅ 所有按钮都有处理函数和可见反馈")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
