# -*- coding: utf-8 -*-
"""
test_force_ui.py —— 兵力池管理面 + 控制台会话记忆的真实浏览器验收

为什么不用 element.click() 或只看截图：
  截图只能证明"看得见"。真实事故：地图页的导航按钮渲染正常、悬停还变色，
  但被 SDK 注入的透明浮层挡住，点了没反应 —— 截图完全看不出来，而
  element.click() 绕过命中测试，会给出假阳性。所以这里每次点击都做两件事：
    1. elementFromPoint(中心) 的顶层元素必须是目标本身（或其子孙）；
    2. 用 Input.dispatchMouseEvent 发真实鼠标事件。
  文本输入则通过原生 setter + input/change 事件写入（等价于用户敲键盘的结果）。

会在真实兵力库上建一个测试单位并最后删掉（含清理变更日志里的测试行），
跑之前建议先备份 db/joint_ops.db。

用法：python tests/test_force_ui.py
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
import urllib.parse
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
TID = "U-901"          # 手工增删改用的单位
TID2 = "U-903"         # 导入测试用的单位
TNAME = "界面测试旅"
# 所有会被这次测试写进去的编号，清理时必须一个不漏 ——
# 漏一个就会留在用户的真实兵力池里（海军凭空多一个支队，谁都说不清哪来的）
TEST_IDS = (TID, TID2)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


# ── 浏览器动作 ────────────────────────────────────────
async def js(c, expr):
    return await c.eval(expr)


async def wait_for(c, expr, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if await c.eval(f"!!({expr})"):
            return True
        await asyncio.sleep(0.25)
    return False


async def click_el(c, expr, label=""):
    """真实鼠标点击。expr 需返回一个元素。返回 (命中是否为目标, 顶层元素标识)。"""
    found = await c.eval(f"""(() => {{
      const e = ({expr});
      if (!e) return false;
      document.querySelectorAll('.__probe__').forEach(x => x.classList.remove('__probe__'));
      e.classList.add('__probe__');
      return true;
    }})()""")
    if not found:
        return False, "元素不存在"
    sel = ".__probe__"
    pt = await CP.center_of(c, sel)
    if not pt:
        await js(c, "document.querySelectorAll('.__probe__')"
                   ".forEach(x=>x.classList.remove('__probe__'))")
        return False, "元素不可见（尺寸为 0）"
    hit = await c.eval(f"""(() => {{
      const e = document.querySelector({sel!r});
      const t = document.elementFromPoint({pt['x']}, {pt['y']});
      return !!t && (t === e || e.contains(t) || t.contains(e));
    }})()""")
    top = await CP.topmost_at(c, pt["x"], pt["y"]) or {}
    ident = top.get("tag", "?") + (("#" + top["id"]) if top.get("id") else "")
    await CP.real_click(c, pt["x"], pt["y"])
    await js(c, "document.querySelectorAll('.__probe__')"
               ".forEach(x=>x.classList.remove('__probe__'))")
    return hit, ident


async def set_val(c, sel, value):
    """按 CSS 选择器写值。用原生 setter，React/Vue 那类受控组件也认。"""
    return await c.eval(f"""(() => {{
      const e = document.querySelector({sel!r});
      if (!e) return false;
      const proto = e.tagName === 'SELECT' ? HTMLSelectElement.prototype
        : (e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype
                                   : HTMLInputElement.prototype);
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(e, {json.dumps(str(value))});
      e.dispatchEvent(new Event('input', {{bubbles: true}}));
      e.dispatchEvent(new Event('change', {{bubbles: true}}));
      return true;
    }})()""")


async def pick_option(c, sel, label):
    return await c.eval(f"""(() => {{
      const s = document.querySelector({sel!r});
      if (!s) return 'no-select';
      const o = [...s.options].find(x => x.textContent.includes({json.dumps(label)}));
      if (!o) return 'no-option';
      s.value = o.value;
      s.dispatchEvent(new Event('change', {{bubbles: true}}));
      return 'ok:' + o.value;
    }})()""")


async def goto(c, path, settle=1.4):
    await c.call("Page.navigate", {"url": BASE + path})
    await CP.settle(c)
    await asyncio.sleep(settle)
    # 每次导航都是新的 JS 上下文，错误钩子必须重新装
    await c.eval("window.__errs = [];"
                 "window.addEventListener('error', e => window.__errs.push('' + e.message));"
                 "window.addEventListener('unhandledrejection',"
                 " e => window.__errs.push('unhandled: ' + e.reason));")


async def drain_errs(c, bucket):
    """把当前页捕到的 JS 报错收走。页面级异常不会让脚本崩，
    但会让某个按钮"点了没反应" —— 这正是最难查的一类问题，必须单独盯。"""
    got = await c.eval("(window.__errs || []).slice()") or []
    bucket.extend(got)
    await c.eval("window.__errs = []")


def db_rows(sql, params=()):
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


def cleanup():
    """把这次测试留下的痕迹清掉：测试单位 + 变更日志里的测试行。

    日志是给用户看"我的真实改动"的，不该混进自动化测试的记录。
    必须覆盖 TEST_IDS 里的每一个编号 —— 只清主测试单位、漏掉导入的那个，
    用户的海军就会凭空多出一个支队。
    """
    # 先事后端接口删一遍：走接口才会触发 reset_pool_snapshot()，
    # 否则服务进程里 HR-06 的兵力池快照还留着测试单位（直接改 SQL 是清不掉的）。
    for tid in TEST_IDS:
        try:
            req = urllib.request.Request(
                f"{BASE}/api/force/units/{urllib.parse.quote(tid)}", method="DELETE")
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:                                      # noqa: BLE001
            pass                                               # 不存在/服务没起都正常

    con = sqlite3.connect(str(DB))
    try:
        n = 0
        for tid in TEST_IDS:
            con.execute("DELETE FROM equipment WHERE unit_id=?", (tid,))
            con.execute("DELETE FROM units WHERE unit_id=?", (tid,))
            n += con.execute("DELETE FROM force_change_log WHERE target LIKE ?"
                             " OR before_json LIKE ? OR after_json LIKE ?",
                             (f"%{tid}%", f"%{tid}%", f"%{tid}%")).rowcount
        con.commit()
    finally:
        con.close()
    # seed 回写：测试单位若还留在 seed 里，下一次重建库就复活了
    for f in ("army.json", "navy.json", "airforce.json"):
        p = ROOT / "seed" / f
        data = json.loads(p.read_text(encoding="utf-8"))
        keep = [u for u in data if u.get("id") not in TEST_IDS]
        if len(keep) != len(data):
            p.write_text(json.dumps(keep, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    return n


async def run() -> int:
    # 起点必须是干净的，否则上一轮残留会让"新增"变成"更新"，测试结论就假了
    cleanup()
    check("起点的测试单位不存在", not db_rows(
        "SELECT 1 FROM units WHERE unit_id=?", (TID,)))

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

    # 记录控制台错误：有 JS 报错的话，后面的断言失败原因会指向它
    console_errors = []
    try:
        ws_url = await UP.wait_devtools()
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            c = UP.CDP(ws)
            await c.call("Page.enable")
            await c.call("Runtime.enable")

            # ── 1. 兵力池页面加载 ────────────────────────
            print("\n[1] 兵力池页加载与工具栏")
            await goto(c, "/force")
            ok = await wait_for(c, "document.querySelectorAll('#forceBox tbody tr').length > 0")
            check("页面渲染出表格", ok)
            n_btn = await js(c, "document.querySelectorAll('#adminBar button').length")
            check("工具栏 6 个按钮", n_btn == 6, f"{n_btn}")
            base_units = await js(c, "document.querySelectorAll('#forceBox .op-btn').length")
            check("表格出现了操作按钮", base_units > 0, f"{base_units} 个")

            # ── 2. 新增单位（走真实点击 + 真实填表）──────
            print("\n[2] 新增单位")
            hit, top = await click_el(c, "document.querySelector"
                                         "('#adminBar button[data-act=\"+ 新增单位\"]')")
            check("点「+ 新增单位」按钮命中目标", hit, top)
            ok = await wait_for(c, "document.querySelector('.modal')")
            check("弹窗打开", ok)
            await set_val(c, '.modal .inp[data-field="unit_id"]', TID)
            await set_val(c, '.modal .inp[data-field="unit_name"]', TNAME)
            await pick_option(c, '.modal .inp[data-field="service"]', "陆军")
            await set_val(c, '.modal .inp[data-field="branch"]', "合成兵种")
            await set_val(c, '.modal .inp[data-field="personnel"]', "1234")
            await set_val(c, '.modal .inp[data-field="base"]', "东山基地")
            await pick_option(c, '.modal .inp[data-field="readiness"]', "二级")
            hit, top = await click_el(c, "[...document.querySelectorAll('.modal-ft .btn')]"
                                         ".find(b => b.textContent.includes('添加'))")
            check("点「添加」命中目标", hit, top)
            ok = await wait_for(c, f"!document.querySelector('.modal') && "
                                   f"document.querySelector('#forceBox')"
                                   f".textContent.includes('{TID}')")
            check("弹窗关闭且列表里出现新单位", ok)
            row = db_rows("SELECT * FROM units WHERE unit_id=?", (TID,))
            check("数据库里确实写进去了", len(row) == 1)
            check("字段逐个对得上",
                  bool(row) and row[0]["personnel"] == 1234
                  and row[0]["service"] == "陆军" and row[0]["readiness"] == "二级",
                  str(row[0]) if row else "")
            check("seed/army.json 已同步",
                  any(u.get("id") == TID for u in json.loads(
                      (ROOT / "seed" / "army.json").read_text(encoding="utf-8"))))

            # ── 3. 非法输入 → 服务端错误要标到字段上 ─────
            print("\n[3] 非法输入的错误提示要落到字段上")
            await click_el(c, "document.querySelector"
                              "('#adminBar button[data-act=\"+ 新增单位\"]')")
            await wait_for(c, "document.querySelector('.modal')")
            await set_val(c, '.modal .inp[data-field="unit_id"]', "U-902")
            await set_val(c, '.modal .inp[data-field="unit_name"]', "坏数据旅")
            await set_val(c, '.modal .inp[data-field="personnel"]', "-5")
            await click_el(c, "[...document.querySelectorAll('.modal-ft .btn')]"
                              ".find(b => b.textContent.includes('添加'))")
            ok = await wait_for(c, "document.querySelector('.modal-msg.err')")
            check("错误提示出现", ok)
            msg = await js(c, "(document.querySelector('.modal-msg.err')||{}).textContent")
            check("提示说明了原因与范围", "必须在" in (msg or ""), msg or "")
            bad_field = await js(c, "!!document.querySelector"
                                   "('.modal .inp[data-field=\"personnel\"].bad')")
            check("出错字段被标红", bad_field)
            check("坏数据没进库", not db_rows("SELECT 1 FROM units WHERE unit_id='U-902'"))
            await click_el(c, "[...document.querySelectorAll('.modal-ft .btn')]"
                              ".find(b => b.textContent.includes('取消'))")
            await wait_for(c, "!document.querySelector('.modal')")

            # ── 4. 编辑（该行右侧「编辑」）───────────────
            print("\n[4] 行内「编辑」改人数")
            expr = ("[...document.querySelectorAll('#forceBox tbody tr')]"
                    f".find(tr => tr.querySelector('.mono') && "
                    f"tr.textContent.includes('{TID}'))"
                    ".querySelectorAll('.op-btn')[0]")
            hit, top = await click_el(c, expr)
            check("点行内「编辑」命中目标", hit, top)
            ok = await wait_for(c, f"document.querySelector('.modal') && "
                                   f"document.querySelector('.modal-title')"
                                   f".textContent.includes('编辑单位')")
            check("打开的是「编辑单位」", ok)
            cur = await js(c, "document.querySelector"
                              "('.modal .inp[data-field=\"personnel\"]').value")
            check("表单带出了原值", str(cur) == "1234", str(cur))
            await set_val(c, '.modal .inp[data-field="personnel"]', "4321")
            await click_el(c, "[...document.querySelectorAll('.modal-ft .btn')]"
                              ".find(b => b.textContent.includes('保存修改'))")
            ok = await wait_for(c, "!document.querySelector('.modal')")
            await asyncio.sleep(0.8)
            r = db_rows("SELECT personnel FROM units WHERE unit_id=?", (TID,))
            check("人数已改成 4321", bool(r) and r[0]["personnel"] == 4321,
                  str(r))

            # ── 5. 新增装备 ──────────────────────────────
            print("\n[5] 新增装备")
            hit, top = await click_el(c, "document.querySelector"
                                         "('#adminBar button[data-act=\"+ 新增装备\"]')")
            check("点「+ 新增装备」命中目标", hit, top)
            await wait_for(c, "document.querySelector('.modal')")
            await pick_option(c, '.modal .inp[data-field="unit_id"]', TID)
            await set_val(c, '.modal .inp[data-field="model"]', "99A-T")
            await set_val(c, '.modal .inp[data-field="etype"]', "主战坦克")
            await set_val(c, '.modal .inp[data-field="count"]', "7")
            await click_el(c, "[...document.querySelectorAll('.modal-ft .btn')]"
                              ".find(b => b.textContent.includes('添加'))")
            await wait_for(c, "!document.querySelector('.modal')")
            await asyncio.sleep(0.8)
            eq = db_rows("SELECT count FROM equipment WHERE unit_id=?", (TID,))
            check("装备写进库", len(eq) == 1 and eq[0]["count"] == 7, str(eq))

            print("\n[6] 同单位重复型号必须被拦住（不能静默覆盖数量）")
            await click_el(c, "document.querySelector"
                              "('#adminBar button[data-act=\"+ 新增装备\"]')")
            await wait_for(c, "document.querySelector('.modal')")
            await pick_option(c, '.modal .inp[data-field="unit_id"]', TID)
            await set_val(c, '.modal .inp[data-field="model"]', "99A-T")
            await set_val(c, '.modal .inp[data-field="count"]', "999")
            await click_el(c, "[...document.querySelectorAll('.modal-ft .btn')]"
                              ".find(b => b.textContent.includes('添加'))")
            ok = await wait_for(c, "document.querySelector('.modal-msg.err')")
            check("被拒绝并有提示", ok)
            eq = db_rows("SELECT count FROM equipment WHERE unit_id=?", (TID,))
            check("数量没被改成 999", eq and eq[0]["count"] == 7, str(eq))
            await click_el(c, "[...document.querySelectorAll('.modal-ft .btn')]"
                              ".find(b => b.textContent.includes('取消'))")
            await wait_for(c, "!document.querySelector('.modal')")

            # ── 7. 删除装备（二次确认）───────────────────
            print("\n[7] 删除装备需二次确认")
            expr = ("[...document.querySelectorAll('#forceBox tbody tr')]"
                    f".filter(tr => tr.textContent.includes('99A-T'))"
                    ".slice(-1)[0].querySelectorAll('.op-btn')[1]")
            hit, top = await click_el(c, expr)
            check("点「删除」命中目标", hit, top)
            ok = await wait_for(c, "[...document.querySelectorAll('.modal-title')]"
                                   ".some(t => t.textContent.includes('确认删除装备'))")
            check("确认框出来了", ok)
            warn = await js(c, "(document.querySelector('.del-warn')||{}).textContent")
            check("确认框写明了不可撤销", "不可撤销" in (warn or ""), warn or "")
            check("删除前数据还在",
                  len(db_rows("SELECT 1 FROM equipment WHERE unit_id=?", (TID,))) == 1)
            hit, top = await click_el(c, "[...document.querySelectorAll('.modal-ft .btn')]"
                                         ".find(b => b.textContent.includes('确认删除'))")
            check("点「确认删除」命中目标", hit, top)
            await wait_for(c, "!document.querySelector('.modal')")
            await asyncio.sleep(1.2)
            check("装备已删除",
                  not db_rows("SELECT 1 FROM equipment WHERE unit_id=?", (TID,)))

            # ── 8. 导入 JSON ─────────────────────────────
            print("\n[8] 导入 JSON（先校验，再入库）")
            hit, top = await click_el(c, "document.querySelector"
                                         "('#adminBar button[data-act=\"导入 JSON\"]')")
            check("点「导入 JSON」命中目标", hit, top)
            await wait_for(c, "document.querySelector('.modal')")
            payload = json.dumps([
                {"id": TID, "单位": TNAME, "军种": "陆军", "兵种": "合成兵种",
                 "人员": 5000, "驻地": "东山基地", "战备状态": "一级",
                 "主要装备": [{"型号": "PLZ-05", "类型": "自行火炮", "数量": 12}]},
                {"id": "U-903", "单位": "导入测试支队", "军种": "海军", "人员": 900,
                 "驻地": "南屿军港", "战备状态": "二级", "主要装备": []},
            ], ensure_ascii=False)
            await set_val(c, ".modal .inp-ta", payload)
            await asyncio.sleep(0.4)
            prev = await js(c, "(document.querySelector('.imp-prev')||{}).textContent")
            check("导入前校验给出了预览", "读到 2 条单位" in (prev or ""), prev or "")
            check("预览按军种分组统计", "陆军 1 条" in (prev or "") and "海军 1 条" in (prev or ""))
            hit, top = await click_el(c, "[...document.querySelectorAll('.modal-ft .btn')]"
                                         ".find(b => b.textContent.includes('开始导入'))")
            check("点「开始导入」命中目标", hit, top)
            await wait_for(c, "!document.querySelector('.modal')", timeout=20)
            await asyncio.sleep(1.2)
            a = db_rows("SELECT personnel FROM units WHERE unit_id=?", (TID,))
            b = db_rows("SELECT 1 FROM units WHERE unit_id='U-903'")
            check("已有单位被更新成 5000", bool(a) and a[0]["personnel"] == 5000, str(a))
            check("新单位 U-903 已导入", len(b) == 1)
            check("导入的装备替换了原有清单",
                  len(db_rows("SELECT 1 FROM equipment WHERE unit_id=? AND model='PLZ-05'",
                              (TID,))) == 1)

            # 坏的 JSON：客户端就该拦下来
            print("\n[9] 格式错误的 JSON 要在本地就挡住")
            await click_el(c, "document.querySelector('#adminBar button[data-act=\"导入 JSON\"]')")
            await wait_for(c, "document.querySelector('.modal')")
            await set_val(c, ".modal .inp-ta", '[{"id":"U-904", 坏数据')
            await asyncio.sleep(0.4)
            cls = await js(c, "(document.querySelector('.imp-prev')||{}).className")
            txt = await js(c, "(document.querySelector('.imp-prev')||{}).textContent")
            check("预览区标了错", "bad" in (cls or ""), cls or "")
            check("提示可读", "JSON 格式" in (txt or ""), (txt or "")[:60])
            await click_el(c, "[...document.querySelectorAll('.modal-ft .btn')]"
                              ".find(b => b.textContent.includes('取消'))")

            # ── 10. 导出 / 模板 / 变更记录 ───────────────
            print("\n[10] 导出 / 模板 / 变更记录")
            await click_el(c, "document.querySelector('#adminBar button[data-act=\"导出当前\"]')")
            ok = await wait_for(c, "document.querySelector('.modal .inp-ta-ro')")
            check("导出弹窗打开", ok)
            txt = await js(c, "(document.querySelector('.modal .inp-ta-ro')||{}).value")
            check("导出内容是中文键（与 seed 同格式）",
                  (txt or "").strip().startswith("[") and '"单位"' in (txt or ""))
            await click_el(c, "[...document.querySelectorAll('.modal-x')][0]")

            await click_el(c, "document.querySelector('#adminBar button[data-act=\"JSON 模板\"]')")
            await wait_for(c, "document.querySelector('.modal .inp-ta-ro')")
            txt = await js(c, "(document.querySelector('.modal .inp-ta-ro')||{}).value")
            check("模板里带注意事项", "写数据时注意" in (txt or ""))
            await click_el(c, "[...document.querySelectorAll('.modal-x')][0]")

            await click_el(c, "document.querySelector"
                              "('#adminBar button[data-act=\"变更记录\"]')")
            ok = await wait_for(c, "document.querySelector('.log-list')")
            check("变更记录弹窗打开", ok)
            n = await js(c, "document.querySelectorAll('.log-list .log-item').length")
            check("能看到刚才的改动", (n or 0) > 0, f"{n} 条")
            first = await js(c, "(document.querySelector('.log-item .log-diff')||{}).textContent")
            check("每条带前后值对比", "→" in (first or ""), first or "")
            await click_el(c, "[...document.querySelectorAll('.modal-x')][0]")

            # ── 11. 控制台：会话记忆 ─────────────────────
            print("\n[11] 控制台「历史会话」与记忆轮次")
            await drain_errs(c, console_errors)       # 收走兵力池页的报错
            await goto(c, "/")
            await js(c, "localStorage.clear()")           # 模拟"首次打开"
            await goto(c, "/", settle=2.2)
            ok = await wait_for(c, "document.querySelectorAll('#chatLog .msg').length > 0",
                                timeout=20)
            check("首访自动接上了最近会话（对话区不是空的）", ok)
            mem = await js(c, "(document.querySelector('#memTag')||{}).textContent")
            check("记忆轮次不再是 0", mem and "记忆 0 轮" not in mem, mem or "")
            tip = await js(c, "[...document.querySelectorAll('#chatLog .msg-meta')]"
                              ".map(x => x.textContent).join(' | ')")
            check("有『已自动接上』的提示", "自动接上" in (tip or ""), (tip or "")[:80])

            hit, top = await click_el(c, "document.querySelector('#histBtn')")
            check("点「历史会话」命中目标", hit, top)
            ok = await wait_for(c, "document.querySelector('#histPanel') && "
                                   "!document.querySelector('#histPanel').classList"
                                   ".contains('hidden')", timeout=15)
            check("历史面板展开", ok)
            ok = await wait_for(c, "document.querySelectorAll('#histList .hist-item')"
                                   ".length > 0", timeout=15)
            check("列出了历史会话", ok)
            cnt = await js(c, "document.querySelectorAll('#histList .hist-item').length")
            check("会话条数合理", (cnt or 0) > 0, f"{cnt} 个")
            first_title = await js(c, "(document.querySelector('#histList .hist-t')||{})"
                                      ".textContent")
            check("会话有可读标题", bool(first_title), (first_title or "")[:30])

            # ── 选验证目标。两个前提都不能靠猜 ──
            #  1) 目标必须 ≠ 当前已接上的会话 —— 点同一条是"原地点击"，
            #     #threadTag 本来就不会变，拿它断言"会话已切换"是假阴性。
            #     （真踩过：库里最新那条恰好带方案，测试误报失败，白查半天。）
            #  2) "这条到底有没有方案"要问后端 —— 标题里写着"联合登陆作战"的会话，
            #     也可能只跑过单域快问，压根没产出决心方案。
            cur_id = await js(c, """(() => {
              const t = (document.querySelector('#threadTag') || {}).textContent || '';
              const m = t.match(/[A-Za-z0-9_]{6,}/g);
              return m ? m[m.length - 1] : '';
            })()""") or ""

            sess = json.loads(urllib.request.urlopen(
                BASE + "/api/sessions", timeout=10).read().decode("utf-8"))
            plan_tid, any_tid = "", ""
            for s in (sess.get("sessions") or []):
                tid = s.get("thread_id") or ""
                if not tid or (cur_id and tid == cur_id):
                    continue
                any_tid = any_tid or tid
                if not plan_tid:
                    try:
                        h = json.loads(urllib.request.urlopen(
                            f"{BASE}/api/history?thread_id={urllib.parse.quote(tid)}",
                            timeout=10).read().decode("utf-8"))
                        if any(t.get("has_new_plan") for t in (h.get("turns") or [])):
                            plan_tid = tid
                    except Exception:                          # noqa: BLE001
                        pass
                if plan_tid and any_tid:
                    break

            check("库里存在『非当前会话』的历史（否则无法验证切换）", bool(any_tid),
                  f"当前 {cur_id or '无'}")
            check("其中至少一条真的产出过联合方案（否则方案还原断言会空跑）",
                  bool(plan_tid), plan_tid or "无")

            use_tid = plan_tid or any_tid
            use_idx = await js(c, f"""(() => {{
              const items = [...document.querySelectorAll('#histList .hist-item')];
              const tid = {json.dumps(use_tid)};
              return tid ? items.findIndex(x => (x.textContent || '').includes(tid)) : -1;
            }})()""")
            check("在历史列表里定位到了目标会话",
                  isinstance(use_idx, int) and use_idx >= 0,
                  f"{use_tid} → index={use_idx}")
            before_tag = await js(c, "document.querySelector('#threadTag').textContent")
            hit, top = await click_el(
                c, f"document.querySelectorAll('#histList .hist-item')[{use_idx}]")
            check("点历史会话命中目标", hit, top)
            ok = await wait_for(c, "(document.querySelector('#threadTag')||{})"
                                   f".textContent !== {json.dumps(before_tag)}", timeout=20)
            check("会话已切换（标题换了）", ok,
                  f"{before_tag} → " + str(await js(
                      c, "document.querySelector('#threadTag').textContent")))
            ok = await wait_for(c, "document.querySelectorAll('#chatLog .msg').length > 1",
                                timeout=25)
            check("会话内容被还原到对话区", ok)
            # 轮次要跟"所点那条会话标称的轮数"一致。
            # （不能拿"跟切换前不同"来判 —— 两个会话轮数相同时会误报失败）
            want = await js(c, f"""(() => {{
              const it = document.querySelectorAll('#histList .hist-item')[{use_idx}];
              const m = (it.textContent.match(/(\\d+) 轮/) || [])[1];
              return m ? '记忆 ' + m + ' 轮' : '';
            }})()""")
            mem2 = await js(c, "(document.querySelector('#memTag')||{}).textContent")
            check("轮次与所选会话一致", bool(want) and mem2 == want,
                  f"期望 {want}，实际 {mem2}")
            n_msg = await js(c, "document.querySelectorAll('#chatLog .msg').length")
            check("还原出了多轮问答", (n_msg or 0) >= 2, f"{n_msg} 条消息")
            if plan_tid and use_tid == plan_tid:
                plan = await js(c, "document.querySelector('#planBox')"
                                  ".textContent.trim().length")
                check("右栏联合方案也一起还原", (plan or 0) > 60, f"{plan} 字")

            # JS 报错汇总
            await drain_errs(c, console_errors)
            check("页面没有未捕获的 JS 报错", not console_errors,
                  "; ".join(console_errors[:3]))

    finally:
        proc.terminate()
        CP.wait_port_free(port)      # 等它真的退掉，别把旧实例留给下一个测试
        n = cleanup()
        print(f"\n（已清理测试单位与 {n} 条测试变更日志）")

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
