"""语法自检：把插件目录下的每个 .py 编译一遍，报出第一个出错位置。

用法（在插件目录里跑）：
    python syntax_check.py

只做 compile()，不 import —— 所以不需要装 AstrBot，也不会碰任何数据。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    bad = 0
    total = 0
    for path in sorted(ROOT.rglob("*.py")):
        if any(part in {".git", "__pycache__"} for part in path.parts):
            continue
        total += 1
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"[跳过] {path.relative_to(ROOT)}: {exc}")
            continue
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            bad += 1
            print(
                f"[语法错误] {path.relative_to(ROOT)}:{exc.lineno}: {exc.msg}"
            )
            if exc.text:
                print(f"           {exc.text.rstrip()}")
    print(f"\n检查 {total} 个文件，{bad} 个有语法错误。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
