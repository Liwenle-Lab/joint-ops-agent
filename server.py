# -*- coding: utf-8 -*-
"""
server.py —— Web 服务入口（第四期：前端 + 本地部署）

为什么必须有它：
  CLI 跑一次全链路要 1~3 分钟，中间是黑箱。浏览器不可能干等 90 秒不看反馈，
  所以走 SSE：一边跑图一边把节点日志、LLM 耗时推给前端。

关键设计：
  · SSE 而非 WebSocket —— 单向够用，穿透性好，实现简单
  · 全局互斥锁 —— 同一时刻只跑一个推理，避免 API 并发把额度打爆，
    也避免两个请求写同一份 plans/ 文件互相覆盖
  · 轮询 drain() 而非深入 LangGraph 内部：协议稳定，不依赖框架私有行为

用法：
    python server.py                 # 默认 http://127.0.0.1:8000
    python server.py --port 8080
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

WEB_DIR = ROOT / "web"
PLAN_DIR = ROOT / "plans"
DB_PATH = ROOT / "db" / "joint_ops.db"

# server 自己是唯一的日志出口：关掉事件总线的终端回显，避免和 uvicorn 日志打架
from src.graph import events as evs                    # noqa: E402
evs.set_echo(False)

from src.graph.graph import (CFG, GRAPH, ROUTER, async_checkpointer,
                             latest_by_domain, reset_pool_snapshot)  # noqa: E402
from rules_convergence import ask_total                  # noqa: E402
from src.tools.env_query import EnvPool                 # noqa: E402
from src.tools.feedback import FEEDBACK, FeedbackError, REASONS as FEEDBACK_REASONS  # noqa: E402
from src.tools.run_log import RUNLOG                    # noqa: E402
from src.tools.force_admin import ADMIN, ForceError     # noqa: E402

# 同一时刻只允许一个推理在跑（详见文件头设计说明）
_GATE = asyncio.Lock()

# 环境查询池：与参谋节点共用同一套缓存与降级逻辑（第五期）
_ENV = EnvPool()


# ─────────────────────────────────────────────────────────
# FastAPI
# ─────────────────────────────────────────────────────────
from fastapi.staticfiles import StaticFiles                            # noqa: E402
from fastapi.middleware.cors import CORSMiddleware                     # noqa: E402
from fastapi import FastAPI, HTTPException              # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, HTMLResponse  # noqa: E402
from pydantic import BaseModel                          # noqa: E402

app = FastAPI(title="联合作战指挥多Agent系统", version="p5")

# 地图页可能被 file:// 直接打开（浏览器给这类页面的 Origin 是 "null"），
# 要让它同时也能读本机 API。服务只监听 127.0.0.1，未对外暴露，故放开来源。
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── 前端资源防缓存 ────────────────────────────────────────────
# 踩过的坑：改完 app.js，用户浏览器里还是旧版，"点了没反应"反复复现，
# 而服务端 curl 下来明明是新版 —— 排查方向全被带偏。
# 根因是浏览器对不带 Cache-Control 的响应会走「启发式新鲜度」，可能不校验直接用缓存。
# 两道保险：① 所有响应强制 no-cache（有 ETag，命中就 304，开销极小）；
#          ② 页面里的 /static/xxx.js 自动带上 ?v=<构建号>，版本一变 URL 就变，缓存彻底失效。
def _build_stamp() -> str:
    """构建号 = web 目录下所有前端文件最新修改时间。改任何一个文件它都会变。"""
    try:
        return str(int(max((p.stat().st_mtime for p in WEB_DIR.rglob("*")
                            if p.is_file()), default=0)))
    except OSError:
        return "0"


def _bust(html: str, stamp: str) -> str:
    import re
    # 只给「后面紧跟引号」的引用加版本号 —— 已经带 ?v= 的自然不会匹配，不会重复追加
    html = re.sub(r'(/static/[\w.-]+\.(?:js|css))(?=["\'])',
                  r'\1?v=' + stamp, html)
    return html.replace("</head>",
                        f'<script>window.__BUILD__="{stamp}";</script></head>')


@app.middleware("http")
async def _no_cache(request, call_next):
    resp = await call_next(request)
    # 本地应用，一律不缓存。别按 content-type 挑：StaticFiles 把 .js 发成
    # text/javascript（不是 application/javascript），按类型过滤会漏掉它。
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    # 顺手去掉协商缓存用的 ETag / Last-Modified：本地应用没必要省这点流量，
    # 留着反而可能让浏览器拿旧版去协商。注意 MutableHeaders 没有 pop()，要用 del。
    for h in ("etag", "last-modified"):
        if h in resp.headers:
            del resp.headers[h]
    return resp


@app.get("/api/build")
async def build():
    """前端构建号。reload_guard.js 每隔一会儿来问一次，
    跟页面加载时注入的不一致就提示用户刷新 —— 避免有人一直开着旧标签页，
    服务端早修好了、他屏幕上还是老样子。"""
    return {"build": _build_stamp()}


def _serve_page(path: Path) -> HTMLResponse:
    """读 html → 打版本号 → 下发。所有页面统一走这里，别再各写一份 FileResponse。"""
    html = path.read_text(encoding="utf-8")
    return HTMLResponse(content=_bust(html, _build_stamp()), media_type="text/html")

# 兵力改动日志表随用随建：老库（在这个功能之前建的）里没有这张表，
# 不建的话用户第一次改兵力就撞 "no such table: force_change_log"。
try:
    ADMIN.ensure_schema()
except Exception as _e:                                     # noqa: BLE001
    print(f"[warn] 兵力改动日志表未就绪：{_e}")
# 反馈表同理：老库里没有 answer_feedback，不建的话第一次点赞就 500。
try:
    FEEDBACK.ensure_schema()
except Exception as _e:                                     # noqa: BLE001
    print(f"[warn] 反馈表未就绪：{_e}")
# 失败台账同理：老库没有 run_failures，不建的话"中断轮次"就标不出来。
try:
    RUNLOG.ensure_schema()
except Exception as _e:                                     # noqa: BLE001
    print(f"[warn] 失败台账未就绪：{_e}")


class ChatReq(BaseModel):
    text: str
    thread_id: str = ""


def _sse(ev: str, data: Any) -> str:
    return f"event: {ev}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _slim_report(r: Dict[str, Any]) -> Dict[str, Any]:
    """参谋方案摘要：前端只列要点，全文走 /api/plan 单独取"""
    d = r.get("data") or {}
    return {
        "domain": r.get("domain"),
        "kind": r.get("kind"),
        "units": [{"unit_id": u.get("unit_id"),
                   "personnel": u.get("personnel")}
                  for u in (d.get("units") or [])],
        "asks": d.get("asks") or {},
        "timeline": d.get("timeline") or [],
        "answer": d.get("answer"),
        "risks": (d.get("risks") or [])[:5],
    }


def _resource_used(final_plan: Dict[str, Any]) -> Dict[str, Any]:
    """把「资源分配」归一成可展示的数字。

    ★ 这一步必须在后端做，不能让前端自己遍历求和：
      模型给的 force 形如 {"陆军":15900,"海军":12500,"空军":1200,"预备队":10200}，
      预备队是"裁下来没投入的兵"，算进投入就会虚高（实测得出 39800 而实际 29600）。
      规则引擎里的 ask_total 早就处理了预备队/合计键这些坑，
      前端再写一套简化版必然分叉——两处实现 = 迟早不一致。
    """
    alloc = (final_plan or {}).get("资源分配") or {}
    if not isinstance(alloc, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in alloc.items():
        n = ask_total(v)
        out[k] = n if n is not None else v
    return out


def _collect_result(out: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    final_plan = out.get("final_plan") or {}
    return {
        "thread_id": thread_id,
        "route": out.get("route"),
        "domain": out.get("domain"),
        "clarify_question": out.get("clarify_question"),
        "answer": out.get("answer"),
        "final_plan": final_plan,
        "resource_used": _resource_used(final_plan),
        "validation": out.get("validation") or {},
        "trace": out.get("trace") or [],
        "plan_version": out.get("plan_version"),
        "is_incremental": out.get("is_incremental"),
        "changed_keys": out.get("changed_keys") or [],
        "impacted": out.get("impacted") or [],
        "reused": out.get("reused") or [],
        "bb_session": out.get("bb_session"),
        # ★ staff_reports 是 add-reducer（只增不减）：失败回退重跑、多轮会话
        #   都会把同名军种的旧版本留在列表里。直接全量回给前端会出现重复卡片，
        #   必须过 latest_by_domain 取每个军种的最新一份。
        "staff": [_slim_report(r)
                  for r in latest_by_domain(out.get("staff_reports") or [])],
        "calls": {"main": ROUTER.counts.get("main", 0),
                  "light": ROUTER.counts.get("light", 0)},
        "mocked": ROUTER.mocked,
    }


async def _run_graph(text: str, thread_id: str) -> Dict[str, Any]:
    """跑图。带 thread_id 时挂 checkpointer → 多轮 + 增量重规划"""
    # 旧会话的事件残留在队列里，先清掉，否则下一条 SSE 会串台
    evs.drain()
    state = {"user_input": text, "retry_count": 0,
             "staff_reports": [], "trace": [], "api_calls": {}}
    cfg: Dict[str, Any] = {"recursion_limit": 50}
    if thread_id:
        cfg["configurable"] = {"thread_id": thread_id}
        from src.graph.graph import build_graph as _bg
        async with async_checkpointer() as saver:
            return await _bg(saver).ainvoke(state, config=cfg)
    return await GRAPH.ainvoke(state, config=cfg)


@app.post("/api/chat")
async def chat(req: ChatReq):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "请输入内容")
    thread_id = (req.thread_id or "").strip() or ("web_" + time.strftime("%m%d%H%M%S"))

    async def gen():
        queued = False
        if _GATE.locked():
            queued = True
            yield _sse("status", {"msg": "已有任务在执行，排队等待…"})
        async with _GATE:
            if queued:
                yield _sse("status", {"msg": "开始执行"})
            evs.drain()                                   # 丢弃本请求之前的残留
            yield _sse("begin", {"thread_id": thread_id, "text": text,
                                 "mocked": ROUTER.mocked,
                                 "model": ROUTER.model_main})
            task = asyncio.create_task(_run_graph(text, thread_id))
            seen_steps = 0
            t0 = time.time()
            try:
                while True:
                    for ev in evs.drain():
                        kind = ev.get("kind", "log")
                        ev.pop("kind")
                        yield _sse(kind, ev)
                    if task.done():
                        for ev in evs.drain():            # 收尾可能还有
                            kind = ev.get("kind", "log")
                            ev.pop("kind")
                            yield _sse(kind, ev)
                        out = task.result()
                        yield _sse("done", {
                            "sec": round(time.time() - t0, 1),
                            "result": _collect_result(out, thread_id),
                        })
                        break
                    seen_steps += 1
                    if seen_steps % 16 == 0:              # 每 4 秒一次心跳，防代理超时断连
                        yield _sse("hb", {"sec": round(time.time() - t0, 1)})
                    await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                task.cancel()
                raise
            except Exception as e:                        # noqa: BLE001
                if not task.done():
                    task.cancel()
                # ★ 留痕：这一轮是**中断**的，不是成功的。
                #   不留的话，State 的累积性会让它在"历史会话"里顶着上一轮的
                #   方案和终验收显示成 PASS —— 界面就在说谎。
                RUNLOG.record_failure(thread_id, text, f"{type(e).__name__}: {e}")
                yield _sse("error", {"msg": f"{type(e).__name__}: {e}",
                                     "detail": traceback.format_exc()[-1500:]})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/health")
async def health():
    return {
        "mocked": ROUTER.mocked,
        "model_main": ROUTER.model_main,
        "model_light": ROUTER.model_light,
        "max_tokens": ROUTER.max_tokens,
        "busy": _GATE.locked(),
        "db": DB_PATH.exists(),
    }


@app.get("/api/constraints")
async def constraints():
    """当前生效的资源容量上限。前端要把「裁决值 vs 容量」并排显示，
    否则看不出仲裁到底有没有真的把申索压进线里。"""
    return {
        "resource_cap": CFG.get("resource_cap", {}),
        "force_cap": CFG.get("force_cap"),
        "deadline_days": CFG.get("deadline_days"),
    }


@app.get("/api/env/anchors")
async def env_anchors():
    """驻地地理锚点全表 —— 地图页与控制台共用的唯一坐标源。

    坐标只从 seed/geo_anchor.json 经建库落到 geo_anchor 表，前端不再硬编码一份，
    否则改一处忘一处，地图上的点和参谋引用的坐标就会对不上。
    """
    return {"anchors": _ENV.all_anchors(), "cache_ttl": _ENV.cache_ttl}


@app.get("/api/env/brief")
def env_brief(base: str = "", days: int = 1):
    """单个驻地的环境实况（气象 + 地形；港口加海洋，场站加阵风/云量）。

    为什么不把组装逻辑写在这儿：字段名一旦在展示层硬编码，数据层改个键名
    这里就静默少一格（本轮已踩过——"最大浪高m" 被写成 "浪高m"，界面照常
    显示，海洋那几格凭空消失）。所以组装收在 EnvPool.brief() 里，
    这里只负责传输与错误码。
    """
    d = _ENV.brief(base, days=days)
    if d.get("error"):
        raise HTTPException(404, d["error"])
    return d


@app.get("/api/plans")
async def plans():
    """已落盘的方案版本，供前端做版本对照"""
    if not PLAN_DIR.exists():
        return {"plans": []}
    items = []
    for p in sorted(PLAN_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        items.append({"name": p.name,
                      "mtime": time.strftime("%H:%M:%S",
                                             time.localtime(p.stat().st_mtime))})
    return {"plans": items}


@app.get("/api/plan")
async def plan(name: str):
    p = (PLAN_DIR / name).resolve()
    # 目录穿越防护：只允许读 plans/ 下的文件
    if not str(p).startswith(str(PLAN_DIR.resolve())) or not p.exists():
        raise HTTPException(404, "not found")
    return JSONResponse(json.loads(p.read_text(encoding="utf-8")))


@app.get("/api/blackboard")
async def blackboard(session: str = ""):
    """协商黑板记录：证明"第 N 轮谁让了什么"是可查的"""

    # 落盘时用的是英文 service key，展示层归一成中文（前端不该关心这层差异）
    SVC_CN = {"army": "陆军", "navy": "海军", "airforce": "空军", "air": "空军"}
    import sqlite3
    con = sqlite3.connect(str(DB_PATH))
    try:
        if not session:
            row = con.execute(
                "SELECT session_id FROM negotiation_round "
                "ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
            session = row[0] if row else ""
        if not session:
            return {"session": "", "rounds": []}
        rows = con.execute(
            "SELECT round,speaker,claim,objection,resource_ask,concession "
            "FROM negotiation_round WHERE session_id=? ORDER BY round,id",
            (session,)).fetchall()
    finally:
        con.close()

    rounds: List[Dict[str, Any]] = []
    for rnd, spk, claim, obj, ask, conc in rows:
        try:
            parsed = json.loads(ask) if ask else {}
        except json.JSONDecodeError:
            parsed = {}
        rounds.append({"round": rnd, "speaker": SVC_CN.get(spk, spk),
                       "claim": claim, "objection": obj, "asks": parsed,
                       "concession": conc})
    return {"session": session, "rounds": rounds}


def _db_rows(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """展示层只读取数。

    刻意不走 ForcePool：那个池子每查一次都会发一个 query_id 并写 trace/queries.jsonl，
    而那是「参谋取数的凭证台账」（HR-07 终验要用 query_id 回查数字来源）。
    把界面翻页的查询混进台账，等于往证据里掺沙子 —— 所以这里直连 sqlite 只读。
    """
    import sqlite3
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


@app.get("/api/force/overview")
async def force_overview():
    """兵力池总览 —— 控制台「兵力池」页与地图页入口共用。

    数字全部取自 db/joint_ops.db 的视图（v_service_total / v_model_total / units / resources），
    和参谋节点用的是同一个库、同一套表。界面上看到的 15900 就是方案里写进"数字来源"的 15900，
    不做第二份缓存，避免"给人看一套、给决策用另一套"。
    """
    if not DB_PATH.exists():
        raise HTTPException(503, "兵力库未就绪，请先运行 db/build_db.py 建库")
    return {
        "services": _db_rows(
            "SELECT service, unit_count, personnel_total, equipment_total "
            "FROM v_service_total ORDER BY personnel_total DESC"),
        "units": _db_rows(
            "SELECT unit_id, unit_name, service, branch, personnel, base, readiness "
            "FROM units ORDER BY service, personnel DESC"),
        # 按型号汇总（给人看的汇报口径）+ 带单位归属的明细（要能改就得知道改的是谁）
        "equipment": _db_rows(
            "SELECT service, model, etype, total FROM v_model_total "
            "ORDER BY service, total DESC"),
        "equipment_rows": _db_rows(
            "SELECT e.unit_id, u.unit_name, u.service, e.model, e.etype, e.count "
            "FROM equipment e JOIN units u ON u.unit_id = e.unit_id "
            "ORDER BY u.service, u.unit_id, e.model"),
        "resources": _db_rows(
            "SELECT res_key, res_name, capacity FROM resources"),
        "cap": {"resource_cap": CFG.get("resource_cap", {}),
                "force_cap": CFG.get("force_cap"),
                "deadline_days": CFG.get("deadline_days")},
    }


# ─────────────────────────────────────────────────────────
# 兵力池：增删改（管理面）
# ─────────────────────────────────────────────────────────
# 与上面的总览接口分工：总览只读、谁都能看；这里的每一个都会改全军的真源。
#
# 三条纪律：
#   1. 跑方案的过程中不许改 —— 一次筹划要 1~3 分钟，中途换名册会让"取数用的是旧的、
#      终验用的是新的"，出的方案自相矛盾且事后无从复现。改成 409 明确挡住。
#   2. 改完必须作废 HR-06 的兵力池快照（reset_pool_snapshot），否则新加的单位
#      会被判"不存在"，数是新的、校验是旧的，比直接报错更难查。
#   3. 错误一律 400 + 指出哪个字段，前端把红字标到对应输入框上；
#      不要吞掉原因只回"操作失败"，那样用户只能靠猜。
def _force_guard() -> None:
    if _GATE.locked():
        raise HTTPException(
            409, "正在执行一次筹划（约 1~3 分钟），此刻改兵力池会让这次筹划前后不一致。"
                 "请等它跑完再改。")


@app.post("/api/force/units")
async def force_unit_upsert(payload: Dict[str, Any]):
    """新增或修改一个单位。带 orig_id 且与原编号不同 = 改编号。"""
    _force_guard()
    body = payload or {}
    try:
        out = await asyncio.to_thread(ADMIN.upsert_unit,
                                      body.get("unit") or body,
                                      (body.get("orig_id") or "").strip())
    except ForceError as e:
        raise HTTPException(400, {"msg": e.msg, "field": e.field})
    reset_pool_snapshot()
    return out


@app.delete("/api/force/units/{unit_id}")
async def force_unit_delete(unit_id: str, note: str = ""):
    """删除单位。库没开外键约束，SQLite 不会替你删装备，
    这里显式连带删除，返回件数让界面能说清"连 3 件装备一起删了"。"""
    _force_guard()
    try:
        out = await asyncio.to_thread(ADMIN.delete_unit, unit_id, note)
    except ForceError as e:
        raise HTTPException(400, {"msg": e.msg, "field": e.field})
    reset_pool_snapshot()
    return out


@app.post("/api/force/equipment")
async def force_equipment_upsert(payload: Dict[str, Any]):
    """新增或修改一条装备。身份是 (所属单位, 型号)。

    create_only=true 用于界面上的「新增装备」：撞上已有型号直接拒绝，
    不让"再加一批"变成"覆盖成这个数"。
    """
    _force_guard()
    body = payload or {}
    try:
        out = await asyncio.to_thread(ADMIN.upsert_equipment,
                                      body.get("equipment") or body,
                                      body.get("orig") or None,
                                      bool(body.get("create_only")))
    except ForceError as e:
        raise HTTPException(400, {"msg": e.msg, "field": e.field})
    return out


@app.delete("/api/force/equipment")
async def force_equipment_delete(unit_id: str, model: str, note: str = ""):
    _force_guard()
    try:
        out = await asyncio.to_thread(ADMIN.delete_equipment, unit_id, model, note)
    except ForceError as e:
        raise HTTPException(400, {"msg": e.msg, "field": e.field})
    return out


@app.put("/api/force/resources/{res_key}")
async def force_resource_set(res_key: str, payload: Dict[str, Any]):
    """改共享资源容量。

    除了写库与回写 constraints.json，还要同步内存里的 CFG —— 终验的容量校验读的是
    CFG（服务启动时加载），只改文件的话，界面上已经是 60 了，终验还按 50 判超限。
    """
    _force_guard()
    cap = (payload or {}).get("capacity")
    try:
        out = await asyncio.to_thread(ADMIN.set_resource, res_key, cap)
    except ForceError as e:
        raise HTTPException(400, {"msg": e.msg, "field": e.field})
    try:
        CFG.setdefault("resource_cap", {})[out["resource"]["res_key"]] = \
            out["resource"]["capacity"]
    except Exception:                                          # noqa: BLE001
        pass                                                   # 生效失败不影响已落盘的结果
    hint = await asyncio.to_thread(ADMIN._sync_constraints_resource,
                                   out["resource"]["res_key"],
                                   out["resource"]["capacity"])
    return {**out, "hint": hint}


@app.post("/api/force/import")
async def force_import(payload: Dict[str, Any]):
    """批量导入 JSON（陆军/海军/空军，或三军混在一个文件里）。

    mode=merge（默认）按编号合并；mode=replace 会把"本次数据涉及到的军种"整军种替换。
    整批一个事务：有一条不合格就整批不落库。
    """
    _force_guard()
    body = payload or {}
    try:
        out = await asyncio.to_thread(ADMIN.import_units,
                                      body.get("items", body.get("units")),
                                      (body.get("mode") or "merge").strip(),
                                      (body.get("default_service") or "").strip())
    except ForceError as e:
        raise HTTPException(400, {"msg": e.msg, "field": e.field})
    reset_pool_snapshot()
    return out


@app.get("/api/force/changelog")
async def force_changelog(limit: int = 50):
    """谁在什么时候把兵力改成了什么 —— 只增不删，重建库也不会丢。"""
    if not DB_PATH.exists():
        raise HTTPException(503, "兵力库未就绪")
    return {"changes": await asyncio.to_thread(ADMIN.changelog, limit)}


@app.get("/api/force/export")
async def force_export(service: str = ""):
    """导出成 seed 文件那种中文键格式，导出的东西能直接再导入回来（闭环）。"""
    if not DB_PATH.exists():
        raise HTTPException(503, "兵力库未就绪")
    try:
        units = await asyncio.to_thread(ADMIN.export_units, service)
    except ForceError as e:
        raise HTTPException(400, {"msg": e.msg, "field": e.field})
    return {"count": len(units), "units": units}


@app.get("/api/force/template")
async def force_template(service: str = "陆军"):
    """给一份可直接照抄的样例，省得用户猜字段名。

    用例里最常出错的三个点写进 _注意：单位编号要唯一、"军种"可省略（界面会带上）、
    "主要装备"是数组且型号不能在同一单位里重复。
    """
    sv = service or "陆军"
    return {
        "_注意": [
            "单位编号（id）全库唯一，重复会覆盖已有单位",
            "军种只能是 陆军/海军/空军；“军种”字段可省略，导入时选中的军种会补上",
            "“主要装备”是数组，同一单位里型号不能重复",
            "人员与数量必须是整数（不能写 4200.5 或 “约4000”）",
            "驻地要能在 seed/geo_anchor.json 里找到，否则该单位查不到气象/地形",
        ],
        "样例": [
            {"id": "A-901", "单位": "示例合成旅", "军种": sv, "兵种": "合成兵种",
             "人员": 4000, "驻地": "东山基地", "战备状态": "一级",
             "主要装备": [{"型号": "99A", "类型": "主战坦克", "数量": 50},
                          {"型号": "PLZ-05", "类型": "自行火炮", "数量": 20}]},
        ],
    }


# ─────────────────────────────────────────────────────────
# 答案反馈（点赞 / 点踩）
# ─────────────────────────────────────────────────────────
# 这不是"训练模型"：本机跑不动微调，也没有外部训练集。做法是
#   把用户踩过的原因 + 备注提炼成一小段中文，注入下一轮的指挥层 / 仲裁层提示词
#   （见 src/tools/feedback.py 的 digest），让"被指出的问题"在后续筹划里被规避。
#   规则类硬约束（数字要带来源、人数要对齐兵力池）不受反馈影响，防止用户
#   一句"别写来源"就把可追溯性抹掉。


@app.post("/api/feedback")
async def feedback_save(payload: Dict[str, Any]):
    """记一次评价。同一 (会话, 轮次) 重复提交=改主意，覆盖而不是叠加。"""
    body = payload or {}
    try:
        out = await asyncio.to_thread(FEEDBACK.save, body)
    except FeedbackError as e:
        raise HTTPException(400, {"msg": e.msg, "field": e.field})
    return out


@app.get("/api/feedback")
async def feedback_list(thread_id: str = ""):
    """某个会话的逐轮评价。前端还原历史时用它回显"我上次踩过哪条"。"""
    try:
        return {"items": await asyncio.to_thread(FEEDBACK.for_thread,
                                                 thread_id)}
    except FeedbackError as e:
        raise HTTPException(400, {"msg": e.msg, "field": e.field})


@app.get("/api/feedback/summary")
async def feedback_summary():
    """累计统计 + 当前会注入提示词的那段文字（让你看见 agent"学到了什么"）。"""
    try:
        return await asyncio.to_thread(FEEDBACK.summary)
    except FeedbackError as e:
        raise HTTPException(400, {"msg": e.msg, "field": e.field})


@app.get("/api/feedback/reasons")
async def feedback_reasons():
    """点踩时可选的原因清单（前端与后端共用同一份，避免两处各写一份）。"""
    return {"reasons": list(FEEDBACK_REASONS)}


# ─────────────────────────────────────────────────────────
# 会话记忆
# ─────────────────────────────────────────────────────────
# 记忆本身一直是有的：多轮会话走 AsyncSqliteSaver，每步落一份 State 快照进
# db/joint_ops.db 的 checkpoints 表 —— 关浏览器、重启服务都还在。
# 缺的只是「看不见」：界面上没有入口能翻出以前问过什么。下面三个函数补这块。
#
# 一轮提问会写 6~7 个 checkpoint（每个节点存一步），而且 channel_values 是整份快照，
# 直接把行摊开就会看到 7 条一模一样的话。所以按 user_input 折叠连续重复项：
# 同一句话连续出现 = 同一次提问的中间态，只算一轮。
async def _collect_turns(saver, thread_id: str) -> List[Dict[str, Any]]:
    """把一个会话的 checkpoint 还原成「轮次」列表（时间正序）。

    每轮保留一份完整快照（该轮最新那份，字段最全），并直接用 _collect_result
    转成前端已有的结果结构 —— 这样「点开历史会话」能连方案一起复原，
    不用在历史这条路上再写一套渲染，也不会和实时渲染分叉。
    """
    groups: List[Dict[str, Any]] = []
    async for tup in saver.alist({"configurable": {"thread_id": thread_id}}):
        cv: Dict[str, Any] = (tup.checkpoint or {}).get("channel_values") or {}
        text = (cv.get("user_input") or "").strip()
        if not text:
            continue
        ts = str((tup.checkpoint or {}).get("ts") or "")
        cur = groups[-1] if groups else None
        if cur is None or cur["text"] != text:          # alist 倒序，同轮重复紧挨着
            # full 取该组第一条（倒序里第一条 = 该轮最后一步 = 字段最全）
            groups.append({"text": text, "ts": ts, "full": dict(cv)})
            continue
        # 同组后续条目只补缺：答案/终验/方案在第一轮快照里可能还没写出来
        merged = cur["full"]
        if not merged.get("answer") and cv.get("answer"):
            merged["answer"] = cv["answer"]
        if not merged.get("validation") and cv.get("validation"):
            merged["validation"] = cv["validation"]
        if not merged.get("final_plan") and cv.get("final_plan"):
            merged["final_plan"] = cv["final_plan"]
        if not merged.get("staff_reports") and cv.get("staff_reports"):
            merged["staff_reports"] = cv["staff_reports"]
        if not merged.get("route") and cv.get("route"):
            merged["route"] = cv["route"]

    out: List[Dict[str, Any]] = []
    prev_plan = ""
    for g in reversed(groups):                          # 还原成时间正序
        full = g["full"]
        try:
            res = _collect_result(full, thread_id)
        except Exception:                               # noqa: BLE001
            res = {}
        res.pop("trace", None)                          # 历史列表不需要逐步 trace，太占体积
        res["calls"] = {}
        fp = full.get("final_plan") if isinstance(full.get("final_plan"), dict) else {}
        route = full.get("route")

        # ★ answer 在 State 里是"持久通道"：只有单域快通道/澄清/领域外会写它，
        #   规划轮次不写。所以规划轮次的快照里留着的是**上一轮**那句快问回答 ——
        #   不在这里剔掉，界面就会串轮（实测：追问联合方案，气泡里还是上一轮
        #   "陆军多少人"的回答）。新写入的轮次已在 graph.py 里清空，
        #   这里同时修好库里已经存下的历史。
        answer = full.get("answer") or ""
        if route == "full_chain" and not full.get("clarify_question"):
            answer = ""

        # 方案同理：单域快问轮次不会产出新方案，快照里的 final_plan 是继承来的。
        # 标记出来，界面才能说清"右栏这版方案是哪一轮生成的"。
        plan_key = json.dumps(fp, ensure_ascii=False, sort_keys=True)
        has_new_plan = bool(fp) and plan_key != prev_plan
        if fp:
            prev_plan = plan_key

        res["answer"] = answer                          # result 那份也要一致，否则前端读 result 又串回去
        res["has_new_plan"] = has_new_plan
        out.append({
            "text": g["text"], "ts": g["ts"],
            "route": route,
            "domain": full.get("domain"),
            "plan_version": full.get("plan_version"),
            "is_incremental": bool(full.get("is_incremental")),
            "changed_keys": full.get("changed_keys") or [],
            "answer": answer,
            "has_new_plan": has_new_plan,
            "validation": full.get("validation") or {},
            "sections": list(fp.keys()),
            "resource_used": res.get("resource_used") or {},
            "result": res,                              # 完整快照，供"点开历史会话"复原界面
        })
    _mark_failed_rounds(out, thread_id)
    return out


def _ts_epoch(s: Any) -> float:
    """checkpoint 的 ts 是 ISO 串；失败台账存的是 epoch。统一到 epoch 才能比。"""
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:                                           # noqa: BLE001
        return -1.0


def _mark_failed_rounds(out: List[Dict[str, Any]], thread_id: str) -> None:
    """给「执行中断」的轮次打标（就地修改 out）。

    ★ 为什么必须打这个标：State 是**累积**的。一轮筹划要是崩在中间，
      快照里的 final_plan / validation 全是**上一轮继承来的** ——
      界面就会把"失败的一轮"显示成"PASS 且产出了方案"。
      用户看到的是"系统说成了，可它当时明明报错了"，这是最伤信任的一类错。

    对号入座用**文本 + 时间**：同一句话可能被问两次（先失败后成功），
    只比对文本会张冠李戴；取"文本相同且时间不晚于失败时刻"的最后一条最稳。
    """
    if not thread_id:
        return
    fails = RUNLOG.failures_for(thread_id)
    if not fails:
        return

    taken = set()
    unmatched = []
    for f in fails:
        ftext = (f.get("text") or "").strip()
        fts = float(f.get("ts") or 0)
        hit = None
        for i, t in enumerate(out):
            if i in taken or (t.get("text") or "").strip() != ftext:
                continue
            if _ts_epoch(t.get("ts")) > fts:      # 这是失败之后才发生的轮次，不是它
                continue
            hit = i
        if hit is None:
            unmatched.append(f)
            continue
        taken.add(hit)
        turn = out[hit]
        turn["failed"] = True
        turn["error"] = f.get("error") or ""
        # 把继承来的"成功痕迹"一并抹掉：它们属于更早的轮次，
        # 留在这里就会让界面把"中断的一轮"显示成"出了方案、终验 PASS"。
        turn["has_new_plan"] = False
        turn["validation"] = {}
        turn["sections"] = []
        turn["resource_used"] = {}
        res = turn.get("result")
        if isinstance(res, dict):
            res["has_new_plan"] = False
            res["validation"] = {}
            res["failed"] = True
            res["error"] = turn["error"]

    # 连一轮 checkpoint 都没留下（首节点就崩）时，历史里本该有这一轮却完全没有。
    # 补一条"中断"占位，否则用户会觉得"我明明问过，怎么记录里没有"。
    for f in unmatched:
        fts = float(f.get("ts") or 0)
        item = {
            "text": (f.get("text") or "").strip(),
            "ts": datetime.fromtimestamp(fts).isoformat() if fts else "",
            "route": None, "domain": None, "plan_version": None,
            "is_incremental": False, "changed_keys": [], "answer": "",
            "has_new_plan": False, "validation": {}, "sections": [],
            "resource_used": {}, "result": {},
            "failed": True, "error": f.get("error") or "",
            "no_trace": True,                     # 没有 checkpoint，纯占位
        }
        pos = len(out)
        for i, t in enumerate(out):
            if _ts_epoch(t.get("ts")) > fts:
                pos = i
                break
        out.insert(pos, item)


@app.get("/api/sessions")
async def sessions(limit: int = 12):
    """历史会话目录：谁在什么时候问过什么，各几轮。

    只开一个 checkpointer 连接把 limit 个会话一次读完 —— 每个会话单开一个连接
    在本地 sqlite 上没必要，还会和服务正在跑的推理抢锁。
    """
    if not DB_PATH.exists():
        return {"sessions": []}
    rows = _db_rows(
        "SELECT thread_id, COUNT(*) AS steps, MAX(rowid) AS last_row "
        "FROM checkpoints GROUP BY thread_id ORDER BY last_row DESC LIMIT ?",
        (max(1, min(int(limit), 50)),))
    out: List[Dict[str, Any]] = []
    try:
        async with async_checkpointer() as saver:
            for r in rows:
                turns = await _collect_turns(saver, r["thread_id"])
                out.append({
                    "thread_id": r["thread_id"],
                    "steps": r["steps"],
                    "turns": len(turns),
                    "title": (turns[0]["text"][:26] if turns else "(空会话)"),
                    "last": (turns[-1]["ts"][11:19] if turns else ""),
                    "date": (turns[-1]["ts"][:10] if turns else ""),
                })
    except Exception as e:                                # noqa: BLE001
        # 记忆是"增值功能"，读不出来不该把控制台拖垮 —— 返回空目录即可
        print("[sessions] 读取失败:", type(e).__name__, e, flush=True)
        return {"sessions": [], "error": f"{type(e).__name__}: {e}"}
    return {"sessions": out}


@app.get("/api/history")
async def history(thread_id: str):
    """单个会话的完整轮次：每轮问了什么、系统回了什么、方案到第几版、终验过没过。"""
    if not thread_id or not DB_PATH.exists():
        return {"thread_id": thread_id, "turns": []}
    try:
        async with async_checkpointer() as saver:
            turns = await _collect_turns(saver, thread_id)
    except Exception as e:                                # noqa: BLE001
        raise HTTPException(500, f"读取会话失败：{type(e).__name__}: {e}")
    return {"thread_id": thread_id, "turns": turns}


@app.get("/")
async def index():
    return _serve_page(WEB_DIR / "index.html")


@app.get("/map")
async def map_page():
    """作战环境地图。

    改用 HTMLResponse 而非 FileResponse：读取 html 源文件后，把其中的
    空占位符 `const MAP_KEY = '';` 替换为 config/.env 里的真实 key 再下发。
    好处——作战环境地图.html 源文件永远不含明文 key（不进前端源码、不进 git），
    直接 file:// 打开时仍是体验额度，只有经过本服务 /map 才注入正式 key。
    """
    f = ROOT / "作战环境地图.html"
    if not f.exists():
        raise HTTPException(404, "地图页缺失")
    html = f.read_text(encoding="utf-8")
    # B 方案：key 仅存于 config/.env，由服务端注入。load_dotenv 兜底再加载一次无害。
    load_dotenv(ROOT / "config" / ".env")
    map_key = os.getenv("TENCENT_MAP_KEY", "")
    if map_key:
        html = html.replace("const MAP_KEY = '';",
                            "const MAP_KEY = %s;" % json.dumps(map_key))
    return HTMLResponse(content=_bust(html, _build_stamp()), media_type="text/html")


@app.get("/force")
async def force_page():
    """兵力池 —— 与控制台、地图并列的顶层页面。

    原先它是控制台右栏的一个隐藏页签，用户怎么也找不到；2026-09-24 提升为
    独立页面，四处入口（首页/控制台/地图/本页）横向并列，见样式注释。
    与控制台共用同一份渲染实现 web/force_view.js，避免两处改动不同步。
    """
    f = WEB_DIR / "force.html"
    if not f.exists():
        raise HTTPException(404, "兵力池页缺失")
    return _serve_page(f)


@app.get("/home")
async def home():
    """产品首页（前端友好入口）。

    双击 `0_启动产品.bat` 默认打开这里：介绍产品 + 两个大入口
    （控制台 / 地图）+ 新手三步。/ 仍是控制台、/map 仍是地图，互不干扰。
    """
    f = WEB_DIR / "home.html"
    if not f.exists():
        raise HTTPException(404, "首页缺失")
    return _serve_page(f)


# 静态资源（css/js）。务必放在所有 API 路由之后：路由按定义顺序匹配，
# 静态目录若在前面，会把后面的 API 路径一并吞掉。
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.exception_handler(404)
async def not_found(request, exc):
    return JSONResponse({"error": "not found"}, status_code=404)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    import uvicorn
    print("=" * 60)
    print(f"  联合作战指挥多Agent系统 · Web 控制台")
    print(f"  地址：http://{args.host}:{args.port}")
    print(f"  模型：{ROUTER.model_main}"
          f"   [{'MOCK（未配置 Key）' if ROUTER.mocked else '真实模型'}]")
    print("  按 Ctrl+C 停止")
    print("=" * 60, flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
