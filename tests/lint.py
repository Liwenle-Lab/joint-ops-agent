# -*- coding: utf-8 -*-
"""
lint.py —— 静态检查闸门：专抓「写了却没导入」这类只在运行时才炸的错

为什么要有这道闸（2026-09-24 一天内踩了两次）：
  1. `src/graph/graph.py` 用了 `FEEDBACK` 却没 import
     → 真模型下一提问就 `NameError`，方案直接出不来，用户截图报错。
  2. `server.py` 里 `full = g["full"]` 被误并进上一行的注释
     → 历史折叠函数里 `full` 未定义，一点「历史会话」就炸。

  这两种错 `ast.parse()` / `node --check` **都查不出来**（语法合法），
  单测和界面测试也可能全都照过（没走到那行代码）。而 pyflakes 一秒就能抓出来。

  结论：跑任何测试之前先过这道闸。它比任何一条用例都更早、更便宜。

判定口径：
  · **未定义名（undefined name）= 失败**（就是上面那两类，必炸）
  · 无用导入 / f-string 缺占位 之类 = 仅提示，不失败
    （项目里遗留的噪音不少，混在一起会把真问题淹掉）

用法：python tests/lint.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 只扫我们自己写的代码；第三方库、临时探针不算
TARGETS = [
    "server.py", "run.py", "rules_convergence.py",
    "db/build_db.py",
    "src/**/*.py",
    "tests/*.py",
]

# 真正会让程序崩的：未定义名 / 未定义变量
FATAL = re.compile(r"\bundefined name\b|\bundefined (local|variable)\b", re.I)
# 噪音：不失败，只统计
NOISE = re.compile(r"imported but unused|f-string is missing placeholders"
                   r"|redefinition of unused|unable to detect undefined names"
                   r"|'.*' assigned to but never used", re.I)


def collect_files() -> list:
    files = []
    for pat in TARGETS:
        files.extend(sorted(ROOT.glob(pat)))
    return [f for f in files if f.is_file()]


def main() -> int:
    try:
        import pyflakes                                          # noqa: F401
    except ImportError:
        print("！未安装 pyflakes，静态检查被跳过。安装方法：")
        print("  <python> -m pip install pyflakes")
        print("  （跳过不等于通过 —— 这类错只在真跑时才现形）")
        return 0

    files = collect_files()
    print(f"静态检查 {len(files)} 个文件（pyflakes）…")
    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes"] + [str(f) for f in files],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]

    fatal = [ln for ln in lines if FATAL.search(ln)]
    noise = [ln for ln in lines if not FATAL.search(ln) and NOISE.search(ln)]
    other = [ln for ln in lines if ln not in fatal and ln not in noise]

    for ln in fatal:
        print(f"  ✗ {ln}")
    if other:
        print("  （其它提示）")
        for ln in other[:10]:
            print(f"    · {ln}")

    print()
    print(f"未定义名 {len(fatal)} 处 · 其它提示 {len(noise) + len(other)} 处")
    if fatal:
        print("\n静态检查未通过：上面每一条都会在运行时抛 NameError。")
        return 1
    print("静态检查通过（没有「写了却没导入」这类必炸的错）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
