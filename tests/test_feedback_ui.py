# -*- coding: utf-8 -*-
"""
test_feedback_ui.py —— 控制台「串轮 / 点赞点踩 / 可点追问入口」的真实浏览器验收

三个被测点都来自用户实际反馈：
  1. 串轮：要求联合方案时，右栏出的是新方案，会话框里回的却是上一轮那句回答。
     根因是 state.answer 是持久通道，规划轮次不写它 → 快照里残留上一轮的 answer。
  2. 「继续追问」只有一行灰字，没有可点的东西。
  3. 想要点赞/点踩，让 agent 能"学到"自己的偏好。

点击一律走真实鼠标事件 + 命中测试（理由见 test_force_ui.py 文件头）。

用法：python tests/test_feedback_ui.py
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SKILL = Path.home() / ".workbuddy" / "skills" / "local-web-ui-verify" / "assets"
if not SKILL.exists():
    SKILL = Path(r"C:\Users\LE LE\.workbuddy\skills\local-web-ui-verify\assets")
sys.path.insert(0, str(SKILL))
import ui_probe as UP          # noqa: E402
import click_probe as CP       # noqa: E402
import websockets              # noqa: E402

BASE = "http://127.0.0.1:8000"
DB = ROOT / "db" / "joint_ops.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


async def js(c, expr):
    return await c.eval(expr)


async def wait_for(c, expr, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if await c.eval(f"!!({expr})"):
            return True
        await asyncio.sleep(0.25)
    return False


async def click_el(c, expr):
    found = await c.eval(f"""(() => {{
      const e = ({expr});
      if (!e) return false;
      document.querySelectorAll('.__probe__').forEach(x => x.classList.remove('__probe__'));
      e.classList.add('__probe__');
      return true;
    }})()""")
    if not found:
        return False, "元素不存在"
    pt = await CP.center_of(c, ".__probe__")
    if not pt:
        await js(c, "document.querySelectorAll('.__probe__')"
                   ".forEach(x=>x.classList.remove('__probe__'))")
        return False, "元素不可见"
    hit = await c.eval(f"""(() => {{
      const e = document.querySelector('.__probe__');
      const t = document.elementFromPoint({pt['x']}, {pt['y']});
      return !!t && (t === e || e.contains(t) || t.contains(e));
    }})()""")
    top = await CP.topmost_at(c, pt["x"], pt["y"]) or {}
    ident = top.get("tag", "?") + (("#" + top["id"]) if top.get("id") else "")
    await CP.real_click(c, pt["x"], pt["y"])
    await js(c, "document.querySelectorAll('.__probe__')"
               ".forEach(x=>x.classList.remove('__probe__'))")
    return hit, ident


async def goto(c, path, settle=1.6):
    await c.call("Page.navigate", {"url": BASE + path})
    await CP.settle(c)
    await asyncio.sleep(settle)
    await c.eval("window.__errs = [];"
                 "window.addEventListener('error', e => window.__errs.push('' + e.message));"
                 "window.addEventListener('unhandledrejection',"
                 " e => window.__errs.push('unhandled: ' + e.reason));")


def db_rows(sql, params=()):
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def db_exec(sql, params=()):
    con = sqlite3.connect(str(DB))
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


# 测试期间实际操作的会话号与轮次。不能自己臆造一个会话号：
# 开屏逻辑发现记住的会话取不到内容时，会自动接上最近一次会话并改写 threadId，
# 所以必须以界面当前真实的会话号为准，否则断言查的是另一个会话，永远查不到。
_active: dict = {"tid": "", "keys": []}


def cleanup():
    """清掉这次测试制造的反馈记录。只删记录下来的那几个 (会话, 轮次)，
    用户真实的评价一条都不能动。"""
    for tid, idx in _active["keys"]:
        db_exec("DELETE FROM answer_feedback WHERE thread_id=? AND turn_index=?",
                (tid, idx))
    _active["keys"] = []


async def run() -> int:
    cleanup()
    console_errors = []

    chrome = UP._find_chrome()
    # 端口必须现挑：连跑多个浏览器测试时，固定端口会让我们连到上一个
    # 还没退干净的实例上，表现为"随机几项失败、单跑又全过"。
    port = CP.pick_free_port()
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
            await c.call("Page.enable")
            await c.call("Runtime.enable")

            # ── 1. 串轮：每一轮气泡必须对应自己那一问 ──────────
            print("\n[1] 会话还原：气泡内容要对得上自己那一问（串轮回归）")
            await goto(c, "/")
            ok = await wait_for(c, "document.querySelectorAll('#chatLog .msg').length > 1")
            check("开屏自动还原了历史会话", ok)

            # 后端给的每一轮：text 是问题，answer 是本轮回答（串轮修复后为空则回落到方案摘要）
            sess = json.loads(urllib.request.urlopen(
                BASE + "/api/sessions?limit=30", timeout=10).read().decode())
            multi = [s for s in sess.get("sessions", []) if (s.get("turns") or 0) >= 2]
            check("存在多轮会话可供验证", bool(multi),
                  f"{len(multi)} 个多轮会话")

            target = multi[0]["thread_id"]
            hist = json.loads(urllib.request.urlopen(
                BASE + "/api/history?thread_id=" + target, timeout=20).read().decode())
            turns = hist.get("turns", [])
            check("该会话确有 2 轮以上", len(turns) >= 2, f"{len(turns)} 轮")

            # ★ 关键断言：规划轮次不得携带上一轮那句快问回答
            bad = []
            for i, t in enumerate(turns):
                has_plan = bool((t.get("result") or {}).get("final_plan"))
                ans = (t.get("answer") or "").strip()
                # 规划轮次如果带了 answer，那它必须是本轮自己产生的，而不是继承来的
                if has_plan and ans and not t.get("has_new_plan"):
                    # 有方案但方案不是本轮新生成的 → answer 是继承来的，属于串轮
                    bad.append((i + 1, t.get("text", "")[:20], ans[:30]))
            check("规划轮次没有继承上一轮的回答", not bad, str(bad[:2]))

            # 界面上：点开这个会话，气泡串起来看是否与问题数一致
            await js(c, f"""(() => {{
              localStorage.setItem('threadId', {json.dumps(target)});
            }})()""")
            await goto(c, "/")
            ok = await wait_for(c, "document.querySelectorAll('#chatLog .msg').length >= 2")
            n_msg = await js(c, "document.querySelectorAll('#chatLog .msg').length")
            check("界面气泡数 = 轮次 × 2（一问一答）",
                  n_msg == len(turns) * 2, f"{n_msg} 条 / {len(turns)} 轮")

            # ── 2. 可点的「继续追问」入口 ──────────────────────
            print("\n[2] 「继续追问」必须是能点的按钮，不能只是一行灰字")
            n_act = await js(c, "document.querySelectorAll('#chatLog .act-row').length")
            check("会话区出现了可点入口区", (n_act or 0) >= 1, f"{n_act} 个")
            labels = await js(c,
                "[...document.querySelectorAll('#chatLog .act-chip')].map(b=>b.textContent)")
            check("第一个是「继续追问」", bool(labels) and "继续追问" in labels[0],
                  str(labels[:3]))

            hit, top = await click_el(c, "document.querySelector('#chatLog .act-chip')")
            check("点「继续追问」命中按钮本身（没被遮挡）", hit, top)
            focused = await wait_for(c, "document.activeElement && "
                                        "document.activeElement.id === 'input'")
            check("点了之后光标落到输入框", focused)

            # ── 3. 点赞 / 点踩 ────────────────────────────────
            print("\n[3] 每条回答下方有 👍 / 👎，点了要真落库")
            # 以界面当前真实的会话号为准（开屏逻辑可能已自动接上最近会话），
            # 并把本次要动的 (会话, 轮次) 记下来，收尾只删这几条。
            tid = await js(c, "localStorage.getItem('threadId') || ''")
            _active["tid"] = tid
            check("界面上有当前会话号", bool(tid), tid)

            n_fb = await js(c, "document.querySelectorAll('#chatLog .fb-row').length")
            check("历史还原时每条回答都带反馈条",
                  (n_fb or 0) >= len(turns), f"{n_fb} 条 / {len(turns)} 轮")
            n_btns = await js(c, "document.querySelectorAll('#chatLog .fb-btn').length")
            check("反馈条上有赞/踩按钮", (n_btns or 0) >= 2, f"{n_btns} 个")

            await goto(c, "/")
            idx1 = await js(c, """(() => {
              const r = document.querySelector('#chatLog .fb-row');
              return r ? Number(r.dataset.turn) : 0;
            })()""")
            _active["keys"].append((tid, idx1))
            # 只清这一轮（若上次跑残了评价，界面会显示"已记下"而点不到按钮）。
            # 范围是 (会话, 轮次) 这一格，不动别的轮次、更不动别的会话。
            db_exec("DELETE FROM answer_feedback WHERE thread_id=? AND turn_index=?",
                    (tid, idx1))
            await goto(c, "/")

            hit, top = await click_el(c, "document.querySelector('#chatLog .fb-btn.up')")
            check("点 👍 命中按钮", hit, top)
            ok = await wait_for(c, "document.querySelector('#chatLog .fb-done.up')", timeout=15)
            check("点赞后变成『已记下』状态", ok)
            rows = db_rows("SELECT * FROM answer_feedback WHERE thread_id=? AND turn_index=?",
                           (tid, idx1))
            check("点赞落库了", bool(rows) and rows[0]["rating"] == 1,
                  f"{len(rows)} 条 rating={rows[0]['rating'] if rows else '-'}")

            # 再对另一条点踩：要弹出原因面板
            print("\n[4] 点踩要问清原因，并写进下一轮提示词")
            await goto(c, "/")     # 重新加载，拿回未评价的反馈条
            ok = await wait_for(c, "document.querySelector('#chatLog .fb-btn.down')")
            check("有未评价的回答可点踩", ok)
            idx2 = await js(c, """(() => {
              const rs = [...document.querySelectorAll('#chatLog .fb-row')];
              const r = rs.find(x => x.querySelector('.fb-btn.down'));
              return r ? Number(r.dataset.turn) : 0;
            })()""")
            _active["keys"].append((tid, idx2))
            hit, top = await click_el(c, "document.querySelector('#chatLog .fb-btn.down')")
            check("点 👎 命中按钮", hit, top)
            ok = await wait_for(c, "document.querySelector('#chatLog .fb-panel')")
            check("弹出原因选择面板", ok)
            chips = await js(c,
                "[...document.querySelectorAll('#chatLog .fb-chip')].map(b=>b.textContent)")
            check("列出了原因标签", len(chips or []) >= 5, f"{len(chips or [])} 个")

            send_disabled = await js(c, "document.querySelector('.fb-send').disabled")
            check("没选原因时提交按钮是禁用的", send_disabled is True)

            hit, top = await click_el(c, "document.querySelector('#chatLog .fb-chip')")
            check("选一个原因命中标签", hit, top)
            await wait_for(c, "!document.querySelector('.fb-send').disabled")
            ok = await js(c, "!document.querySelector('.fb-send').disabled")
            check("选了原因后提交按钮可用", ok)

            hit, top = await click_el(c, "document.querySelector('.fb-send')")
            check("点提交命中按钮", hit, top)
            ok = await wait_for(c, "document.querySelector('#chatLog .fb-done.down')",
                                timeout=15)
            check("点踩后变成『已记下（不准确）』", ok)
            down_rows = db_rows("SELECT * FROM answer_feedback"
                                " WHERE thread_id=? AND turn_index=?", (tid, idx2))
            check("点踩落库且带原因", bool(down_rows)
                  and down_rows[0]["rating"] == -1
                  and bool((down_rows[0]["reasons_json"] or "").strip()),
                  (str(down_rows[0]["reasons_json"])[:40] if down_rows else "无"))

            # ── 5. 反馈要真的进提示词 ─────────────────────────
            print("\n[5] 反馈要能被下一轮筹划看到（不是存着好看）")
            sm = json.loads(urllib.request.urlopen(BASE + "/api/feedback/summary",
                                                   timeout=10).read().decode())
            check("摘要统计到了反馈", sm.get("total", 0) >= 2,
                  f"total={sm.get('total')} up={sm.get('up')} down={sm.get('down')}")
            dg = sm.get("digest") or ""
            check("生成了给模型的改进线索", len(dg) > 20, dg[:60].replace("\n", " | "))
            check("线索里写明不放宽硬规则", "不放宽" in dg)
            check("点赞不会把硬规则放开（线索里保留来源要求）", "来源" in dg)

            stat = await js(c, "(document.querySelector('#fbStat')||{}).textContent")
            check("顶栏显示累计反馈", "赞" in (stat or "") or "反馈" in (stat or ""),
                  stat or "")

            # ── 6. JS 报错 ────────────────────────────────────
            got = await js(c, "(window.__errs || []).slice()") or []
            console_errors.extend(got)
            check("页面没有未捕获的 JS 报错", not console_errors,
                  "; ".join(console_errors[:3]))
    finally:
        proc.terminate()
        CP.wait_port_free(port)      # 等它真的退掉，别把旧实例留给下一个测试
        cleanup()
        print("\n（已清理本次测试的反馈记录）")

    print()
    print("=" * 62)
    print(f"  合计 {len(PASS) + len(FAIL)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"    ✗ {f}")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                          # noqa: BLE001
        pass
    sys.exit(asyncio.run(run()))
