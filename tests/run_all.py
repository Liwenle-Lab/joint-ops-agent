# -*- coding: utf-8 -*-
"""
run_all.py —— 一条命令跑完全部检查

为什么要有这个入口（2026-09-24 的教训）：
    那天我改了 `graph.py` 的节点内部代码，跑遍了当时 202 项测试——**全绿**。
    然后用户一提问就 `NameError`，方案出不来。
    原因：那 202 项里**没有一项真的调用过图**（全是单测和界面测试），
    而 `run.py --test` 这条真正跑全链路的验收套件，我压根没跑。

    "测试全过"和"功能是好的"是两件事。想让它俩等价，就必须有一个
    **固定顺序、固定清单**的总入口，而不是每次凭记忆挑几个文件跑。

顺序（从便宜到贵，早失败早停不算错，但这里全跑完再汇总）：
    1. 静态检查  tests/lint.py        —— 秒级，抓「写了却没导入」
    2. 单元测试  6 个文件             —— 秒级，不花钱、不依赖服务
    3. 验收套件  run.py --test        —— MOCK 模式跑完整筹划链路（免费）
    4. 界面测试  4 个文件             —— 需要本地服务在跑（真浏览器点击）
    5. 接口契约  tests/test_web.py    —— 打运行中的服务，验 SSE/多轮/黑板（约 5 分钟）

用法：python tests/run_all.py
     加 --with-live 让验收套件改用真实模型（慢、花钱，默认不跑）
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8000"

# (显示名, [命令参数], 是否强制 MOCK, 是否需要本地服务)
UNIT = [
    ("环境数据", ["tests/test_env.py"], False),
    ("增量重规划", ["tests/test_incremental.py"], False),
    ("协商收敛", ["tests/test_negotiation.py"], False),
    ("反馈与串轮", ["tests/test_feedback.py"], True),
    ("兵力增删改", ["tests/test_force_admin.py"], False),
    ("筹划全链路", ["tests/test_graph_chain.py"], True),
]
UI = [
    ("界面导航点击", ["tests/test_nav_click.py"]),
    ("界面兵力池", ["tests/test_force_ui.py"]),
    ("界面反馈与追问", ["tests/test_feedback_ui.py"]),
    # 「没跑完的轮次不许冒充成功」——这是本项目最不能破的底线，必须每次都跑
    ("历史诚实性", ["tests/test_history_honesty.py"]),
]
# 接口契约（test_web.py）单独跑，因为它明显更慢（约 5 分钟）：
#   ★ 它打的是**正在运行的服务**，所以用不用真实模型由**服务端**决定 ——
#     这里传 mock 与否**没有效果**（试过：LLM_API_KEY= 只影响子进程自己）。
#     别再写"默认 MOCK"这种看着生效、其实没生效的开关。
#   它验的是"SSE 事件齐不齐、多轮有没有拿到 v2、黑板读不读得出"：
#   契约本身与模型能力无关，但链路真的会调模型，所以慢、且会受模型限流影响
#   （真出过一次连跑掉 4 项、单跑 23/23 —— 先单跑确认，再判断是不是产品问题）。
CONTRACT = ("接口契约", ["tests/test_web.py"])

COUNT = re.compile(r"合计\s*(\d+)\s*项，失败\s*(\d+)\s*项")
TOTAL = re.compile(r"总体:\s*(ALL PASS|FAIL)[^\d]*(\d+)")


def run(args, mock: bool, timeout: int = 900):
    env = dict(os.environ)
    if mock:
        # 空 Key → ROUTER 降级 MOCK：跑的是与真实模型**完全相同的图与节点**，
        # 只是不产生推理开销。免费、秒级，适合每次改动都跑。
        env["LLM_API_KEY"] = ""
        env.pop("TAMPER_FIGURE", None)
        env.pop("MOCK_FAULT", None)
    try:
        p = subprocess.run([sys.executable] + args, cwd=str(ROOT), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout}s）"


def summarize(out: str) -> str:
    m = COUNT.search(out)
    if m:
        return f"{m.group(1)} 项，失败 {m.group(2)} 项"
    m = TOTAL.search(out)
    if m:
        return f"{m.group(2)} 项，{'全过' if m.group(1) == 'ALL PASS' else '有失败'}"
    m = re.search(r"结果：(\d+)/(\d+)\s*通过", out)
    if m:
        return f"{m.group(2)} 项，通过 {m.group(1)} 项"
    tail = [ln for ln in out.strip().splitlines() if ln.strip()]
    return tail[-1].strip()[:60] if tail else "(无输出)"


def server_up() -> bool:
    try:
        with urllib.request.urlopen(BASE + "/api/health", timeout=4) as r:
            return r.status == 200
    except Exception:                                             # noqa: BLE001
        return False


def main() -> int:
    live = "--with-live" in sys.argv
    results = []          # (名称, 是否通过, 摘要)

    print("=" * 68)
    print("静态检查")
    print("=" * 68)
    code, out = run(["tests/lint.py"], mock=False, timeout=180)
    print(out.strip()[-600:])
    results.append(("静态检查（未定义名）", code == 0, summarize(out)))

    def section(title):
        print()
        print("=" * 68)
        print(title)
        print("=" * 68)

    section("单元与链路测试（不依赖服务）")
    for name, args, mock in UNIT:
        code, out = run(args, mock=mock)
        ok = code == 0
        summ = summarize(out)
        print(f"  {'✓' if ok else '✗'} {name:<12} {summ}")
        if not ok:
            for ln in out.strip().splitlines():
                if "✗" in ln:
                    print(f"      {ln.strip()[:110]}")
        results.append((name, ok, summ))

    section(f"验收套件 run.py --test（{'真实模型' if live else 'MOCK 模式'}）")
    code, out = run(["run.py", "--test"], mock=not live, timeout=1800)
    print("  " + "\n  ".join(
        ln for ln in out.strip().splitlines()
        if ln.startswith("T") or ln.startswith("结果")))
    m = re.search(r"结果：(\d+)/(\d+)\s*通过", out)
    ok = bool(m) and m.group(1) == m.group(2) and code == 0
    if not m:
        ok = False
        print("  ！没拿到验收结论（可能中途崩了），请直接跑 run.py --test 看输出")
    summ = f"{m.group(2)} 项，通过 {m.group(1)} 项" if m else "无结论"
    print(f"  {'✓' if ok else '✗'} {summ}")
    results.append(("验收套件（全链路）", ok, summ))

    section("界面测试（需本地服务在 127.0.0.1:8000）")
    if server_up():
        print("  服务在线，开始跑真实浏览器点击用例…")
        for name, args in UI:
            code, out = run(args, mock=False)
            ok = code == 0
            summ = summarize(out)
            print(f"  {'✓' if ok else '✗'} {name:<12} {summ}")
            if not ok:
                for ln in out.strip().splitlines():
                    if "✗" in ln:
                        print(f"      {ln.strip()[:110]}")
            results.append((name, ok, summ))

        name, args = CONTRACT
        print("  （接口契约打的是正在运行的服务，走服务端当前模型，约 5 分钟）")
        code, out = run(args, mock=False, timeout=1800)
        ok = code == 0
        summ = summarize(out)
        print(f"  {'✓' if ok else '✗'} {name:<12} {summ}")
        if not ok:
            for ln in out.strip().splitlines():
                if "[FAIL]" in ln:
                    print(f"      {ln.strip()[:110]}")
        results.append((name, ok, summ))
    else:
        print(f"  ⚠ 服务没在跑，界面测试已跳过（这 {len(UI) + 1} 项**未经检查**）。")
        print("    要跑它们：双击 0_启动产品.bat，再重跑本脚本。")
        for name, _ in UI:
            results.append((name + "（跳过）", None, "服务未启动"))
        results.append((CONTRACT[0] + "（跳过）", None, "服务未启动"))

    print()
    print("=" * 68)
    failed = [r for r in results if r[1] is False]
    skipped = [r for r in results if r[1] is None]
    for name, ok, summ in results:
        mark = "跳过" if ok is None else ("通过" if ok else "失败")
        print(f"  {mark}  {name:<20} {summ}")
    print("=" * 68)
    print(f"共 {len(results)} 项检查：通过 {len(results) - len(failed) - len(skipped)}"
          f" · 失败 {len(failed)} · 跳过 {len(skipped)}")
    if failed:
        print("失败项：" + "、".join(r[0] for r in failed))
        return 1
    if skipped:
        print("注意：有跳过项 —— 只有全部跑完才算真的全过。")
        return 0
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
