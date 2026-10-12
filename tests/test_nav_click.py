# -*- coding: utf-8 -*-
"""test_nav_click.py —— 顶栏导航「真实鼠标点击」回归测试

为什么必须有这个测试
-------------------
2026-09-24 用户反馈："地图上那几个按钮看得见，就是点不动。"
根因：腾讯地图 SDK 初始化时往地图容器里注入一层 `z-index:1000` 的透明 div，
把所有子元素的鼠标事件全吃掉。当时的验证只做了"页面渲染出来了 + 截图看着对"，
所以漏了 —— **截图能证明看得见，证明不了点得动**。

因此约定：凡是导航、按钮、链接类改动，必须用 `Input.dispatchMouseEvent` 发真实
鼠标事件，并核对两件事：
  1. `document.elementFromPoint(按钮中心)` 的**最顶层元素就是目标 <a>**
     （顶层不是它 = 有浮层在挡，必然点不动，哪怕肉眼完全看不出来）；
  2. 点击后 `location.pathname` 真的变成了期望值。

用法（服务需已在本机 8000 端口运行）：
    python tests/test_nav_click.py
    python tests/test_nav_click.py --base http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ui_probe as UP  # noqa: E402

import websockets  # noqa: E402

# (起始页, 点击的选择器, 期望跳转到的 pathname, 用例名)
CASES = [
    ("/map",   "#navConsole", "/",      "地图页 → 指挥控制台"),
    ("/map",   "#navForce",   "/force", "地图页 → 兵力池"),
    ("/map",   "#navHome",    "/home",  "地图页 → 产品首页"),
    ("/force", "#navConsole", "/",      "兵力池页 → 指挥控制台"),
    ("/force", "#navMap",     "/map",   "兵力池页 → 环境地图"),
    ("/",      "#navForce",   "/force", "控制台页 → 兵力池"),
    ("/",      "#navMap",     "/map",   "控制台页 → 环境地图（同标签，不再新开）"),
    ("/home",  "#navConsole", "/",      "首页 → 指挥控制台"),
]


async def center_of(c, sel):
    return await c.eval(f"""(() => {{
      const e = document.querySelector({sel!r});
      if (!e) return null;
      const r = e.getBoundingClientRect();
      return {{x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)}};
    }})()""")


async def topmost_at(c, x, y):
    return await c.eval(f"""(() => {{
      const e = document.elementFromPoint({x}, {y});
      if (!e) return null;
      return e.tagName + (e.id ? '#' + e.id : '');
    }})()""")


async def real_click(c, x, y):
    for typ in ("mousePressed", "mouseReleased"):
        await c.call("Input.dispatchMouseEvent", {
            "type": typ, "x": x, "y": y, "button": "left",
            "clickCount": 1, "buttons": 1 if typ == "mousePressed" else 0,
        })
        await asyncio.sleep(0.08)


async def wait_path(c, expect, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if await c.eval("location.pathname") == expect:
            return True, time.time() - t0
        await asyncio.sleep(0.3)
    return False, time.time() - t0


async def settle(c):
    for _ in range(60):
        if await c.eval("document.readyState") == "complete":
            break
        await asyncio.sleep(0.25)
    await asyncio.sleep(2.5)          # 等地图 SDK / 兵力池取数就绪


async def run(base: str) -> int:
    chrome = UP._find_chrome()
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", "--force-device-scale-factor=1",
         "--enable-unsafe-swiftshader", f"--remote-debugging-port={UP.PORT}",
         "--window-size=1680,1050", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = await UP.wait_devtools()
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            c = UP.CDP(ws)
            await c.call("Page.enable")
            rows, bad = [], 0
            for start, sel, expect, label in CASES:
                await c.call("Page.navigate", {"url": base + start})
                await settle(c)
                pt = await center_of(c, sel)
                if not pt:
                    rows.append((label, "❌ 元素不存在（id 被改掉？）"))
                    bad += 1
                    continue
                top = await topmost_at(c, pt["x"], pt["y"])
                await real_click(c, pt["x"], pt["y"])
                ok, dt = await wait_path(c, expect)
                hit = str(top or "").lower().startswith("a#")
                if ok and hit:
                    rows.append((label, f"✅ 顶层={top} 跳转={expect} ({dt:.1f}s)"))
                else:
                    reason = ("被浮层遮挡：" + str(top)) if not hit else "点击后未跳转"
                    rows.append((label, f"❌ {reason}"))
                    bad += 1
            print("\n===== 顶栏导航真实点击回归 =====")
            for label, res in rows:
                print(f"  {label:34s} {res}")
            print(f"\n合计 {len(rows)} 项，失败 {bad} 项")
            return 1 if bad else 0
    finally:
        proc.terminate()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000",
                    help="本机服务地址（需先启动 server.py）")
    a = ap.parse_args()
    return asyncio.run(run(a.base))


if __name__ == "__main__":
    sys.exit(main())
