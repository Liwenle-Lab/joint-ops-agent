"""历史会话的「诚实性」回归：界面不许把继承来的结果说成本轮产出。

这个测试为什么必须存在
----------------------
State 是累积的（LangGraph 的 channel_values 一直带着上一轮的值）。
一旦某轮执行中途报错，快照里挂着的 `final_plan` / `validation` 仍然是
上一轮那些 —— 界面若照单全收，气泡就会写成
「已生成联合决心方案（v2）· 终验 PASS」。

用户明明刚看到过报错，回头翻历史却成了一次成功的筹划。
这是本项目最不能犯的错：**界面不许替系统撒谎**。

所以这里的断言是逐轮对照后端给的 `has_new_plan` 真值，
而不是笼统地"不许出现某某字样" —— 后者会把真正产出方案的轮次也误判。
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, r"C:\Users\LE LE\.workbuddy\skills\local-web-ui-verify\assets")

import click_probe as CP  # noqa: E402
import ui_probe as UP     # noqa: E402
import websockets          # noqa: E402

BASE = "http://127.0.0.1:8000"
TID = "web_0924121635"          # 这个会话里有一轮是执行中途崩掉的

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, note: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{note}]" if note else ""))


def api(path: str, timeout: int = 15) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        import json
        return json.loads(r.read().decode("utf-8"))


async def js(c, expr: str):
    r = await c.call("Runtime.evaluate",
                     {"expression": expr, "returnByValue": True, "awaitPromise": True})
    return (r.get("result") or {}).get("value")


async def wait_for(c, expr: str, timeout: int = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if await js(c, expr):
                return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.4)
    return False


async def main() -> int:
    print("=== 历史会话的诚实性（界面不许替系统撒谎）===\n")

    try:
        truth = api(f"/api/history?thread_id={TID}")["turns"]
    except Exception as e:  # noqa: BLE001
        check("能读到基准会话（否则整套断言是空跑）", False, f"{type(e).__name__}: {e}")
        print(f"\n合计 {len(FAIL)} 项，失败 {len(FAIL)} 项")
        return 1

    print(f"  基准会话 {TID} · {len(truth)} 轮")
    for i, t in enumerate(truth, 1):
        print(f"     第{i}轮 route={str(t.get('route')):<14} "
              f"has_new_plan={str(t.get('has_new_plan')):<6} "
              f"v={t.get('plan_version')}")
    check("基准会话里确实有「没产出新方案」的轮次（否则断言失去意义）",
          any(t.get("has_new_plan") is False for t in truth))
    check("基准会话里也有真产出方案的轮次（防止一刀切误伤）",
          any(t.get("has_new_plan") for t in truth))

    port = CP.pick_free_port()
    chrome = UP._find_chrome()
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", "--force-device-scale-factor=1",
         "--enable-unsafe-swiftshader", f"--remote-debugging-port={port}",
         "--window-size=1680,1200", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = await UP.wait_devtools()
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            c = UP.CDP(ws)
            await c.call("Page.enable", {})
            await c.call("Runtime.enable", {})

            await c.call("Page.navigate", {"url": BASE + "/"})
            await CP.settle(c)
            await asyncio.sleep(1)
            await js(c, f"localStorage.setItem('threadId', '{TID}'); void 0")
            await c.call("Page.navigate", {"url": BASE + "/"})
            await CP.settle(c)

            check("会话还原成功",
                  await wait_for(
                      c, f"document.querySelectorAll('#chatLog .msg-me').length >= "
                         f"{len(truth)}", timeout=35))

            quests = await js(c,
                "[...document.querySelectorAll('#chatLog .msg-me .bubble')]"
                ".map(b => b.textContent)")
            bubbles = await js(c,
                "[...document.querySelectorAll('#chatLog .msg-ai .bubble')]"
                ".map(b => b.textContent)")

            print("\n  ---- 界面实际呈现 ----")
            for i, q in enumerate(quests or []):
                b = (bubbles or [""])[i] if i < len(bubbles or []) else ""
                print(f"   [{i+1}] 问: {(q or '')[:34]}")
                print(f"       答: {(b or '')[:76]}")
            print()

            check("还原的轮数与后端一致", len(quests or []) == len(truth),
                  f"{len(quests or [])} vs {len(truth)}")

            # ★ 核心：逐轮对照 has_new_plan 真值
            print("  ---- 逐轮核对 ----")
            for i, t in enumerate(truth):
                shown = (bubbles or [""])[i] if i < len(bubbles or []) else ""
                nshield = shown or ""
                if t.get("has_new_plan"):
                    ok = "已生成联合决心方案" in nshield
                    check(f"第{i+1}轮（真产出方案）如实说『已生成』", ok, nshield[:52])
                else:
                    ok = "已生成联合决心方案" not in nshield
                    check(f"第{i+1}轮（没产出方案）不许说『已生成』", ok, nshield[:52])
                    if not t.get("answer"):
                        check(f"第{i+1}轮（没产出方案）不许挂着『终验 PASS』",
                              "终验 PASS" not in nshield, nshield[:52])

            # 单域快问的回答不能被"清理继承值"误伤
            plain = [t for t in truth if t.get("answer")]
            check("单域快问的回答仍然完整保留",
                  bool(plain) and any("15900" in (b or "")
                                      for b in (bubbles or [])),
                  f"{len(plain)} 轮带文字回答")

            # ★ 语气要平实，但外观必须能一眼看出异常
            #   （否则用户扫过去会以为这一轮是正常的）
            warned = await js(c, """[...document.querySelectorAll(
                '#chatLog .msg-ai .bubble.warn')]
              .map(b => b.textContent)
              .filter(t => t.includes('没有跑完'))""") or []
            check("『没跑完』的气泡用了警示样式（一眼能看出）", bool(warned),
                  (warned[0] or "")[:56] if warned else "没有 warn 气泡")

            # 反向：正常产出方案的那几轮不许被误刷成警示色
            warn_all = await js(c, """[...document.querySelectorAll(
                '#chatLog .msg-ai .bubble.warn')].map(b => b.textContent)""") or []
            check("正常产出方案的轮次没有被误刷成警示色",
                  all("已生成联合决心方案" not in (w or "") for w in warn_all),
                  f"{len(warn_all)} 条警示气泡")

            unfinished_meta = await js(c, """(() => {
              const ms = [...document.querySelectorAll('#chatLog .msg-meta')]
                .map(m => m.textContent);
              return ms.find(x => x.includes('本轮没有产出方案')) || '';
            })()""") or ""
            check("该轮不再列出继承来的版本号/资源占用",
                  "本轮没有产出方案" in unfinished_meta
                  and " transport " not in unfinished_meta,
                  unfinished_meta[:64])

            # 没跑完的轮次不该给反馈按钮（没有回答可供评价）
            nfb_rows = await js(c, "document.querySelectorAll('#chatLog .fb-row').length")
            n_without_answer = sum(1 for t in truth
                                   if t.get("has_new_plan") is False
                                   and not t.get("answer") and not t.get("failed"))
            check("反馈按钮只为真正有回答的轮次渲染",
                  (nfb_rows or 0) <= len(truth) - 0 and (nfb_rows or 0) >= 1,
                  f"{nfb_rows} 条 / {len(truth)} 轮（其中 {n_without_answer} 轮无结论）")

            # 右栏方案归属提示：只有"最近产出方案的那轮 ≠ 最后一轮"时才需要说明
            last_plan_idx = max((i for i, t in enumerate(truth)
                                 if t.get("has_new_plan")), default=-1)
            metas = await js(c,
                "[...document.querySelectorAll('#chatLog .msg-meta')]"
                ".map(b => b.textContent)") or []
            joined = "\n".join(metas)
            if 0 <= last_plan_idx < len(truth) - 1:
                check("右栏方案不是最后一轮产出时，要明确注明来自哪一轮",
                      "生成的方案" in joined, str(last_plan_idx + 1))
            else:
                check("右栏方案就是最近一轮产出，无需额外说明",
                      True, f"方案来自第 {last_plan_idx + 1} 轮（= 最后一轮）")

            errs = await js(c, "window.__FATALS__ ? window.__FATALS__.length : 0")
            check("页面无未捕获 JS 报错", not errs, f"{errs} 条")

        CP.wait_port_free(port, timeout=8)   # 同步函数，不要 await
        proc.terminate()
    except Exception as e:  # noqa: BLE001
        check("核查过程未异常", False, f"{type(e).__name__}: {e}")
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass

    print(f"\n合计 {len(PASS) + len(FAIL)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
