# -*- coding: utf-8 -*-
"""
ui_probe.py —— 控制台界面探针（真实浏览器交互 + 截图）

为什么不用 agent-browser：
  它要额外下载一份 Chromium；本机已有 Chrome，直接走 CDP（Chrome DevTools Protocol）
  更省事，而且能精确控制"等结果出现再截图"这件事——headless 的
  `--screenshot` 是页面加载完就拍，而本页面的结果要等几十秒异步返回，拍早了只有空白。

用法：
    # 只截初始态
    python tests/ui_probe.py --out shot_idle.png

    # 输入问题、等结果出来再截图
    python tests/ui_probe.py --q "陆军现在多少人，多少装备？" --out shot_run.png

    # 全链路（等得更久）
    python tests/ui_probe.py --q "陆海空联合登陆作战，5天内完成投送" --out shot_full.png --timeout 240
"""
from __future__ import annotations
import argparse
import asyncio
import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]
PORT = 9223


def _find_chrome() -> str:
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    raise SystemExit("找不到 Chrome/Edge")


class CDP:
    """极简 CDP 客户端：够用就好，不引第三方包"""

    def __init__(self, ws):
        self.ws = ws
        self._id = 0

    async def call(self, method: str, params: dict | None = None, timeout: float = 30):
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": method,
                                       "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=max(1, deadline - time.time()))
            msg = json.loads(raw)
            if msg.get("id") == mid:                 # 事件（无 id）直接丢弃
                if "error" in msg:
                    raise RuntimeError(f"{method} 失败：{msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"{method} 超时")

    async def eval(self, expr: str, timeout: float = 30):
        r = await self.call("Runtime.evaluate",
                            {"expression": expr, "returnByValue": True,
                             "awaitPromise": True}, timeout=timeout)
        return (r.get("result") or {}).get("value")


async def wait_devtools(timeout: float = 30) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list",
                                        timeout=2) as r:
                tabs = json.loads(r.read())
            for t in tabs:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception:                             # noqa: BLE001
            pass
        await asyncio.sleep(0.4)
    raise SystemExit("DevTools 端口未就绪")


async def shoot(url: str, out: Path, question: str = "", timeout: float = 200,
                full_height: bool = False) -> int:
    import websockets

    chrome = _find_chrome()
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", "--force-device-scale-factor=1",
         f"--remote-debugging-port={PORT}",
         "--window-size=1680,1050", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        ws_url = await wait_devtools()
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            c = CDP(ws)
            await c.call("Page.enable")
            await c.call("Page.navigate", {"url": url})

            # 等 DOM 就绪
            for _ in range(60):
                if await c.eval("document.readyState") == "complete":
                    break
                await asyncio.sleep(0.25)
            await asyncio.sleep(0.8)                  # 让 health 请求回来

            if question:
                # 填值并触发一次真实查询，走的是页面自己的发送逻辑
                ok = await c.eval(
                    "(() => { const t = document.querySelector('#input');"
                    f" if (!t) return false; t.value = {json.dumps(question)};"
                    " document.querySelector('#send').click(); return true; })()")
                if not ok:
                    print("[warn] 没找到输入框，可能页面结构变了", flush=True)

                t0 = time.time()
                last_note = ""
                while time.time() - t0 < timeout:
                    done = await c.eval(
                        "(() => { const b = document.querySelector('#busyBadge');"
                        " return b ? b.textContent : ''; })()") or ""
                    note = await c.eval(
                        "(() => { const l = document.querySelectorAll('.log-line');"
                        " return l.length + '|' +"
                        " (document.querySelector('#threadTag')||{}).textContent; })()")
                    if note != last_note:
                        last_note = note
                        print(f"  [{time.time() - t0:5.1f}s] {note}", flush=True)
                    if done and "空闲" in done and time.time() - t0 > 2:
                        break
                    await asyncio.sleep(1.0)
                print(f"  执行结束，用时 {time.time() - t0:.0f}s", flush=True)
                await asyncio.sleep(1.0)              # 等最后一帧渲染

            if full_height:
                m = await c.call("Page.getLayoutMetrics")
                h = int(m["cssContentSize"]["height"])
                await c.call("Emulation.setDeviceMetricsOverride",
                             {"width": 1680, "height": h, "deviceScaleFactor": 1,
                              "mobile": False})

            shot = await c.call("Page.captureScreenshot", {"format": "png"},
                                timeout=60)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(base64.b64decode(shot["data"]))
            print(f"已保存 {out}（{out.stat().st_size // 1024} KB）", flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:                             # noqa: BLE001
            proc.kill()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--out", default="shot.png")
    ap.add_argument("--q", default="", help="要输入的问题；留空则只截初始态")
    ap.add_argument("--timeout", type=float, default=200)
    ap.add_argument("--full", action="store_true", help="按整页高度截图")
    a = ap.parse_args()
    return asyncio.run(shoot(a.url, Path(a.out), a.q, a.timeout, a.full))


if __name__ == "__main__":
    raise SystemExit(main())
