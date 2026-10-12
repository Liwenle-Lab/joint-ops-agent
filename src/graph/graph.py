# -*- coding: utf-8 -*-
"""
graph.py —— 第一期最小闭环：单域查询 + 全链路首案

本期范围（对应《项目书简易版》五期分期）：
  ✅ 真实兵力池（SQLite）+ 查询工具（query_id 溯源）
  ✅ 意图四态路由 · 指挥下发约束 · Send 并行三参谋 · 冲突检测 · 仲裁 · 硬规则终验
  ✅ 分层回退（plan_violation→commander / artifact_incomplete→arbitrate / constraint_conflict→intent）
  ⏸ 协商子循环（二期）· 多轮与增量重规划（三期）· 本地部署（四期）· MCP 与前端（五期）

判据：单域"陆军多少人多少装备"答得对且数字可追溯；跨域背景能出三军方案并通过硬规则。
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from rules_convergence import (RuleContext, attribute_failure, check_convergence,
                               detect_conflicts, diff_constraints, estimate_min_days,
                               ask_total, impacted_domains, run_hard_rules)
from src.graph import events as evs
from src.graph import prompts as P
from src.graph.state import JointOpsState
from src.llm.client import TIER_LIGHT, TIER_MAIN, LLMRouter
from src.tools.blackboard import Blackboard, norm_service
from src.tools.force_query import ForcePool
from src.tools.env_query import EnvPool
from src.tools.feedback import FEEDBACK


CFG = json.loads((ROOT / "config" / "constraints.json").read_text(encoding="utf-8"))
POOL = ForcePool()

# 环境数据池（第五期）。★ 环境查询是"必查项"而不是"模型可选调用的工具"：
#   作战筹划离不开天气/海况/地形，若交给模型自己决定要不要调，就会出现
#   "同一问题有时查有时不查"的不确定行为。所以走预注入——每次筹划必然带上，
#   query_id 也因而可控（参谋引用的每个环境数字都能在终验里回查）。
ENV = EnvPool()
ENV_ON = bool(CFG.get("env_enabled", True))
ENV_MAX_BASES = int(CFG.get("env_max_bases", 3))
ENV_DAYS = int(CFG.get("env_days", 1))

ROUTER = LLMRouter()
PLAN_DIR = ROOT / "plans"
PLAN_DIR.mkdir(parents=True, exist_ok=True)

REQUIRED_SECTIONS = ["行动时序", "风险与备选", "资源分配"]


# ── 小工具 ───────────────────────────────────────────────
def _log(tag: str, msg: str) -> Dict[str, str]:
    """写 trace 的同时旁路推一份给事件总线（Web 前端靠它做实时流水线）"""
    ev = {"tag": tag, "msg": msg, "t": time.strftime("%H:%M:%S")}
    evs.emit("node", **ev)
    return ev


def latest_by_domain(reports: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """add-reducer 只增不减：恢复重跑会累积同名域的旧版本，只取最后一个"""
    out: Dict[str, Dict[str, Any]] = {}
    for r in reports or []:
        out[r.get("domain", "?")] = r
    return list(out.values())


_POOL_SNAPSHOT: Optional[Dict[str, Dict[str, Any]]] = None


def force_pool_snapshot() -> Dict[str, Dict[str, Any]]:
    """HR-06 用：{军种: {unit_id: {...}}}，只加载一次"""
    global _POOL_SNAPSHOT
    if _POOL_SNAPSHOT is None:
        snap: Dict[str, Dict[str, Any]] = {}
        for sv in ("陆军", "海军", "空军"):
            res = POOL.units_of(sv)
            snap[sv] = {r["unit_id"]: r for r in res.rows}
        # HR-06 按 plan.domain 取池：联合方案的 domain 是"联合"，需给一个全集
        snap["联合"] = {uid: u for d in snap.values() for uid, u in d.items()}
        _POOL_SNAPSHOT = snap
    return _POOL_SNAPSHOT


def reset_pool_snapshot() -> None:
    """作废兵力池快照缓存。

    兵力池以前是只读的，缓存一次无所谓；现在用户能在界面上改兵力，
    若不作废，改完当天跑方案时 HR-06 还按旧名册判"单位不存在"——
    数取到了新的、校验用的是旧的，这比报错更难查。改完必须调这里。
    """
    global _POOL_SNAPSHOT
    _POOL_SNAPSHOT = None


def env_pack(service: str, max_bases: int = 0) -> tuple:
    """给某军种参谋的环境实况简报。

    返回 (文本, 本次可用的 env_id 集合)。id 集合用来过滤模型引用的来源——
    模型会凭印象编一个 "env:e_xxxx"，过滤掉比事后追责更省事。

    只取该军种前 N 个驻地：环境查询是真打外部接口，全量铺开会把一次筹划
    拖成几十个请求（三军 × 各驻地），既慢又容易触发限频。
    """
    if not ENV_ON:
        return "（环境感知已关闭：config/constraints.json 里 env_enabled=false）", set()
    try:
        bases = ENV.bases_of(service)
    except Exception as e:                          # noqa: BLE001
        return f"（环境数据不可用：{e}）", set()
    if not bases:
        return ("（该军种所辖驻地尚无地理锚点，环境数据缺失；"
                "补 seed/geo_anchor.json 后重建库即可）"), set()

    limit = max_bases or ENV_MAX_BASES
    picked = bases[:limit]
    lines, ids = [], set()
    for b in picked:
        r = ENV.base_brief(b["base"], days=ENV_DAYS)
        lines.append(r.to_llm_view())
        # base_brief 自己的 id 就是参谋该引用的那个 —— to_llm_view 里显示的
        # [env_id=...] 是它，ENV.get() 也按它回查，别去引子查询的 id。
        ids.add(r.query_id)
    note = ""
    if len(bases) > len(picked):
        note = f"\n（该军种共 {len(bases)} 个驻地，此处只列前 {len(picked)} 个）"
    return "\n".join(lines) + note, ids


def build_ctx(state: Dict[str, Any]) -> RuleContext:
    cons = state.get("constraints") or {}
    return RuleContext(
        force_cap=cons.get("force_cap", CFG["force_cap"]),
        deadline=cons.get("deadline_days", CFG["deadline_days"]),
        no_go_zones=cons.get("no_go_zones", CFG["no_go_zones"]),
        roe_allowed=cons.get("roe_allowed", CFG["roe_allowed"]),
        resource_cap=cons.get("resource_cap", CFG["resource_cap"]),
        force_pool=force_pool_snapshot(),
        required_personnel=int(state.get("required_personnel", 0) or 0),
        per_sortie_capacity=CFG["per_sortie_capacity"],
        sorties_per_day=CFG["sorties_per_day"],
    )


# ── 节点 ─────────────────────────────────────────────────
def intent(state: JointOpsState) -> Dict[str, Any]:
    ui = state["user_input"]
    # 三期：把上一轮上下文喂给 intent，否则追问（"运力减半"）会被当成从零提需求，
    #      因为没重复军种/时间窗而被判 need_clarify，多轮会话直接断掉。
    prev = "【上一轮上下文】\n（无，这是首轮）"
    if state.get("plan_version"):
        prev = ("【上一轮上下文】\n"
                f"已有方案 v{state['plan_version']}，领域={state.get('domain') or '联合'}\n"
                f"上一轮已下发约束：{json.dumps(state.get('constraints') or {}, ensure_ascii=False)}\n"
                "若本轮输入是对它的修改或追问，槽位沿用上一轮，不要要求重新澄清。")
    resp = ROUTER.chat(
        [{"role": "system", "content": P.INTENT_SYSTEM},
         {"role": "user", "content": P.INTENT_USER.format(
             user_input=ui, prev_context=prev)}],
        tier=TIER_LIGHT, json_mode=True, role_hint="intent")
    try:
        ticket = resp.json()
    except Exception:                                    # noqa: BLE001
        ticket = {"route": "need_clarify", "reason": "意图解析失败"}
    route = ticket.get("route", "need_clarify")
    # ★ answer 是 State 里的持久通道（只在单域快通道写）。规划轮次若不显式清空，
    #   上一轮快问的回答会一直挂在快照上，界面/接口就会把"上一轮的回话"当成
    #   这一轮的结论 —— 实测现象：追问联合方案时，气泡里显示的仍是上一轮问陆军的回答。
    #   清空只能在**单点节点**做：intent 每轮最先跑且只有一份，最合适。
    #   （别放进 staff —— 那是并行三份，同一步各写一次 answer 会抛 InvalidUpdateError。）
    #   单域轮次不受影响：staff 随后会把本轮回答写回来。
    return {"task_ticket": ticket, "route": route, "domain": ticket.get("domain"),
            "answer": "",
            "trace": [_log("intent", f"route={route} domain={ticket.get('domain')}")],
            "api_calls": dict(ROUTER.counts)}


def reject(state: JointOpsState) -> Dict[str, Any]:
    return {"answer": "本系统仅处理联合作战筹划领域的问题，该请求不在受理范围内。",
            "trace": [_log("reject", "领域外拒答")]}


def clarify(state: JointOpsState) -> Dict[str, Any]:
    ticket = state.get("task_ticket") or {}
    q = ticket.get("reason") or "请补充说明：涉及哪些军种？是查询兵力还是做作战筹划？时间窗与兵力上限是多少？"
    return {"clarify_question": q, "answer": f"需要澄清：{q}",
            "trace": [_log("clarify", q)]}


CONS_KEYS = ("force_cap", "deadline_days", "no_go_zones", "roe_allowed", "resource_cap")


def default_constraints() -> Dict[str, Any]:
    return {k: CFG[k] for k in CONS_KEYS}


def feedback_digest() -> str:
    """取历史反馈提炼出的改进线索。拿不到就当没有，绝不抛。

    ★ 反馈是**增值**功能，不是筹划的前提。这里一旦抛出去，整条链路会以
      "NameError / 读库失败" 告终 —— 用户看到的是方案出不来，
      却完全不知道根因是一个可选功能坏了。宁可少一段线索，也不能没方案。
      顺带挡住用户备注里的花括号等脏输入引发的意外。
    """
    try:
        return FEEDBACK.digest() or ""
    except Exception as e:                                      # noqa: BLE001
        print(f"[warn] 历史反馈读取失败，本轮按无反馈筹划：{type(e).__name__}: {e}")
        return ""


def commander(state: JointOpsState) -> Dict[str, Any]:
    # 三期：约束基线 = 上一轮已下发的（多轮）或配置默认（首轮）。
    # ★ 一期的写法是"约束永远照抄配置"，那在单轮下是防模型越权，但在多轮下会变成死结：
    #   用户说"运力减半"，约束纹丝不动 → diff 恒为空 → 增量重规划永远不触发。
    base = dict(state.get("last_constraints") or default_constraints())
    # 用户对既往结果的评价（赞/踩）提炼成一段改进线索，随本轮提示词带进去。
    # 没有反馈时是空串 —— 不塞"暂无反馈"这种占位，避免平白多出一段 token。
    fb = feedback_digest()
    resp = ROUTER.chat(
        [{"role": "system", "content": P.COMMANDER_SYSTEM},
         {"role": "user", "content": P.COMMANDER_USER.format(
             user_input=state["user_input"],
             ticket=json.dumps(state.get("task_ticket", {}), ensure_ascii=False),
             base_constraints=json.dumps(base, ensure_ascii=False),
             constraints=json.dumps(default_constraints(), ensure_ascii=False),
             feedback=fb)}],
        tier=TIER_MAIN, json_mode=True, role_hint="commander")
    data = resp.json()

    # 越权拦截（保留，但语义更准）：
    #   用户明确要求的变更 = 合法，必须落实；
    #   模型自作主张的 = 拦截（不得新增资源类型、不得把基线里没有的键塞进来）。
    given = data.get("constraints") or {}
    cons: Dict[str, Any] = {}
    for k in CONS_KEYS:
        if k == "resource_cap":
            merged = dict(base.get("resource_cap") or {})
            for r, v in (given.get("resource_cap") or {}).items():
                if r in merged and isinstance(v, (int, float)):   # 只许改基线已有的资源
                    merged[r] = int(v)
            cons[k] = merged
        else:
            v = given.get(k)
            cons[k] = v if v is not None else base.get(k, CFG.get(k))
    assigned = data.get("assigned_domains") or ["陆军", "海军", "空军"]
    trace = [_log("commander", f"下发约束 force_cap={cons['force_cap']} "
                               f"deadline=D+{cons['deadline_days']} "
                               f"assigned={assigned}")]
    # 反馈有没有真的进去，要让用户在流水线里看得见 —— 否则"会学习"只是一句承诺
    trace.append(_log("feedback", "本轮带入历史反馈作为改进线索" if fb
                                   else "暂无历史反馈，本轮按默认要求筹划"))
    return {"constraints": cons, "assigned": assigned,
            "commander_brief": {"objectives": data.get("objectives", []),
                                "sub_tasks": data.get("sub_tasks", [])},
            # 同上：清掉上一轮可能残留的快问回答。回退重跑时会走到这里而不是 intent，
            # 所以这处不能省，否则重跑那一版仍会顶着上一轮的回话。
            "answer": "",
            "trace": trace,
            "api_calls": dict(ROUTER.counts)}


def dispatch(state: JointOpsState) -> Dict[str, Any]:
    """分发汇聚点（不返回 Send：节点返回值必须是 dict，Send 只能由条件边给出）。

    三期：这里做「影响面分析」——拿本轮约束与上一轮 diff，算出哪些军种必须重算。
    未受影响的军种不派发，直接复用上一版方案（staff_reports 是 add-reducer，
    上一轮记录还在，latest_by_domain 自然取到旧的）——这就是"增量重规划"。
    """
    route = state.get("route")
    doms = ([state["domain"]] if state.get("domain") else []) \
        if route == "single_domain" else (state.get("assigned") or ["陆军", "海军", "空军"])

    cons = state.get("constraints") or {}
    last = state.get("last_constraints")
    version = int(state.get("plan_version", 0) or 0)

    # 单域快通道 / 首轮：不做增量
    if route == "single_domain" or not last or not state.get("staff_reports"):
        return {"plan_version": version or 1, "last_constraints": cons,
                "changed_keys": [], "impacted": [], "reused": [],
                "is_incremental": False,
                "trace": [_log("dispatch", f"派发 {len(doms)} 个参谋：{doms}（首轮全量）")]}

    changed = diff_constraints(last, cons)
    reports = latest_by_domain(state.get("staff_reports", []))
    impacted, reused = impacted_domains(reports, changed, last, cons)

    # 一个都没影响（例如只改了无关字段）→ 退化为全量，避免产出空方案
    if changed and not impacted:
        impacted, reused = list(doms), []

    return {"plan_version": version + 1, "last_constraints": cons,
            "changed_keys": changed, "impacted": impacted, "reused": reused,
            "is_incremental": bool(changed),
            "trace": [_log("dispatch",
                           f"v{version + 1} 增量重规划：变更 {changed or '无'} → "
                           f"重算 {impacted}，复用 {reused}")]}


def fanout(state: JointOpsState) -> List[Send]:
    """参与者裁剪：单域只派 1 个；跨域全量派 3 个，增量重规划只派受影响的。
    payload 必须自包含（子 agent 上下文全新）"""
    route = state.get("route")
    if route == "single_domain":
        doms = [state["domain"]] if state.get("domain") else []
        mode = "query"
    else:
        doms = state.get("assigned") or ["陆军", "海军", "空军"]
        # ★ 增量重规划：只重派受影响的军种，其余复用上一版
        if state.get("is_incremental") and state.get("impacted"):
            doms = [d for d in doms if d in set(state["impacted"])]
        mode = "plan"
    base = {"user_input": state["user_input"],
            "task_ticket": state.get("task_ticket", {}),
            "constraints": state.get("constraints", {}),
            "plan_version": int(state.get("plan_version", 1) or 1),
            "mode": mode}
    return [Send("staff", {**base, "domain": d}) for d in doms]


_TAMPER_USED = [False]      # 测试用篡改只生效一次（可变容器，便于测试重置）


async def staff(payload: Dict[str, Any]) -> Dict[str, Any]:
    """二期：改为 async，配合 ainvoke 让 Send 分派的三个参谋真正并发。

    一期的坑：fanout 用了 Send，但节点是同步的、入口也是 invoke，
    三个参谋实际是串行跑（实测 17.4 / 17.9 / 24.3s），并行收益为零。
    """
    domain = payload["domain"]
    mode = payload.get("mode", "plan")
    env_view, env_ids = "", set()

    if mode == "query":
        r1 = POOL.service_total(domain)
        r2 = POOL.equipment_by_model(domain)
        view = r1.to_llm_view() + "\n" + r2.to_llm_view()
        sysmsg = P.STAFF_QUERY_SYSTEM.format(service=domain)
        task = f"直接回答用户：{payload['user_input']}"
    else:
        r1 = POOL.units_of(domain)
        r2 = POOL.service_total(domain)
        r3 = POOL.resource_capacity()
        view = "\n".join(x.to_llm_view() for x in (r1, r2, r3))
        sysmsg = P.STAFF_PLAN_SYSTEM.format(service=domain)
        task = (f"按指挥层约束拟制{domain}方案；用户背景：{payload['user_input']}")
        # ★ 必须 to_thread：环境查询是真 HTTP 请求，直接在这里同步调用会把
        #   事件循环堵住，三个参谋的并行（二期特意改成 async 的成果）当场变串行。
        env_view, env_ids = await asyncio.to_thread(env_pack, domain)

    # 三期：产物按方案版本落盘（plans/{domain}.v{n}.json），可对比、可回滚
    plan_path = PLAN_DIR / f"{domain}.v{int(payload.get('plan_version', 1) or 1)}.json"
    resp = await ROUTER.achat(
        [{"role": "system", "content": sysmsg},
         {"role": "user", "content": P.STAFF_USER.format(
             task=task,
             constraints=json.dumps(payload.get("constraints", {}), ensure_ascii=False),
             query_view=view, env_view=env_view,
             plan_path=str(plan_path))}],
        tier=TIER_MAIN, json_mode=True, role_hint=f"staff:{domain}")
    data = resp.json()
    # 仅测试用：真实篡改注入，验证 HR-07 能在真模型下拦住错数字并回退重跑。
    # 与 MOCK_FAULT 的区别——那个只有 Mock 模型认，接了真模型就形同虚设。
    # 只注入一次，否则重跑永远过不了终验。
    if os.environ.get("TAMPER_FIGURE") == "1" and not _TAMPER_USED[0]:
        figs = data.get("figures") or {}
        if "personnel_total" in figs:
            try:
                figs["personnel_total"] = int(figs["personnel_total"]) + 1
                data["figures"] = figs
                _TAMPER_USED[0] = True
            except (TypeError, ValueError):
                pass
    plan_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    report = {"domain": domain, "kind": data.get("kind", mode), "data": data,
              "plan_path": str(plan_path), "asks": data.get("asks", {}),
              "source": data.get("source", ""),
              "env_sources": data.get("env_sources", {})}
    trace = [_log(f"staff:{domain}", f"{mode} -> {plan_path.name}")]
    patch: Dict[str, Any] = {"staff_reports": [report],
                             "trace": trace,
                             "api_calls": dict(ROUTER.counts)}
    if mode == "query":
        patch["answer"] = data.get("answer", "")
    else:
        # ★ answer 的"清空"不能在这里做：本节点是**并行三份**（Send 扇出），
        #   三个参谋在同一步各写一次 answer，LangGraph 会直接抛
        #   InvalidUpdateError: "At key 'answer': Can receive only one value per step"。
        #   清空已挪到单点节点（intent / commander），这里只管不发 answer。
        # 环境引用要过一遍白名单：模型常凭印象编一个 env:e_xxxx，与其事后
        # 在终验里报"来源无法回查"，不如在这里就只放行本次真给过它的 id。
        raw = data.get("env_sources") or {}
        used = []
        if isinstance(raw, dict):
            for key, val in raw.items():
                vid = str(val).replace("env:", "").strip()
                if vid in env_ids:
                    used.append(f"env:{vid}")
                else:
                    trace.append(_log(f"env:{domain}",
                                      f"丢弃无效来源 {key}={val}"))
        elif isinstance(raw, list):                 # 模型返回 list 时也认
            for val in raw:
                vid = str(val).replace("env:", "").strip()
                if vid in env_ids:
                    used.append(f"env:{vid}")
        patch["env_brief"] = {domain: env_view}
        patch["env_sources"] = used
        report["env_used"] = used
        trace.append(_log(f"env:{domain}",
                          f"可用 {len(env_ids)} 项，引用 {len(used)} 项"))
        if not used and env_ids:
            trace.append(_log(f"env:{domain}", "本次未引用环境数据"))
    return patch


def collect(state: JointOpsState) -> Dict[str, Any]:
    reports = latest_by_domain(state.get("staff_reports", []))
    if state.get("route") == "single_domain":
        return {"current_asks": [], "conflicts": [], "negotiation_round": 0,
                "trace": [_log("collect", "单域快通道：旁路仲裁，直接终验")]}
    asks = [r.get("asks", {}) for r in reports]
    cap = (state.get("constraints") or {}).get("resource_cap", CFG["resource_cap"])
    conflicts = detect_conflicts(asks, cap)
    msg = f"冲突 {len(conflicts)} 项" if conflicts else "无资源冲突"
    # 二期：开一份黑板，本次任务的协商发言全落在这里（State 只存钥匙，不存正文）
    sid = state.get("bb_session") or Blackboard().session_id
    return {"current_asks": asks, "conflicts": conflicts, "negotiation_round": 0,
            "bb_session": sid,
            "trace": [_log("collect", f"汇总{len(reports)}份方案，{msg}"
                                      + ("（进入协商）" if conflicts else ""))]}


def slim_plan(d: Dict[str, Any]) -> Dict[str, Any]:
    """仲裁输入瘦身：只保留裁决必需的要点。

    为什么不传原文：三份完整方案 + 约束 + 冲突清单塞进 prompt 后，
    推理型模型会把大量 token 耗在思考上，导致正文被 max_tokens 截断
    （finish=length），JSON 不完整 → 解析失败 → 整链路回退重跑。
    实测瘦身前仲裁单次 190s 且被截断，瘦身后稳定在 20s 内。
    """
    return {
        "domain": d.get("domain"),
        "units": [f"{u.get('unit_id')}({u.get('personnel')}人)"
                  for u in (d.get("units") or [])],
        "asks": d.get("asks") or {},
        "timeline": [f"{t.get('start')}-{t.get('end')}：{str(t.get('action'))[:36]}"
                     for t in (d.get("timeline") or [])],
        "areas": (d.get("areas") or [])[:3],
        "risks": [str(x)[:60] for x in (d.get("risks") or [])[:2]],
    }


# ── 二期：协商子循环 ───────────────────────────────────
def _parties_of_conflict(reports: List[Dict[str, Any]],
                         conflicts: List[Dict[str, Any]]) -> List[str]:
    """参与者裁剪：只拉卷入冲突的军种。

    《设计总纲》§5.4 第 4 条：空军没参与就不会来抢运力，别把三个都拉进来陪跑。
    一个资源超限 → 在该资源上有申索的军种才算相关方。
    """
    if not conflicts:
        return []
    res_conf = {c.get("resource") for c in conflicts}
    out = []
    for r in reports:
        asks = r.get("asks") or {}
        if any((ask_total(asks.get(res, 0)) or 0) > 0 for res in res_conf):
            out.append(r.get("domain", "?"))
    return out


async def negotiate(state: JointOpsState) -> Dict[str, Any]:
    """协商一轮：各方基于黑板做增量修订，发言全部落盘。"""
    rnd = int(state.get("negotiation_round", 0)) + 1
    sid = state.get("bb_session") or ""
    bb = Blackboard(sid) if sid else Blackboard()
    reports = latest_by_domain(state.get("staff_reports", []))
    conflicts = state.get("conflicts") or []
    cap = (state.get("constraints") or {}).get("resource_cap", CFG["resource_cap"])

    parties = _parties_of_conflict(reports, conflicts) or [r["domain"] for r in reports]
    if not parties:                                   # 没有冲突就没得谈
        return {"negotiation_status": "converged",
                "trace": [_log("negotiate", "无冲突，无需协商")]}

    prev = bb.asks_by_round()
    last_rnd = max(prev) if prev else 0
    last_conc: Dict[str, str] = {}
    for r in bb.fetch():
        if r["round"] == last_rnd and r["concession"]:
            last_conc[r["speaker"]] = r["concession"]
    board_view = bb.to_llm_view()

    # 把「缺口」明明白白算给模型看：只说"你超了"，模型会象征性让一两个架次，
    # 实测空军从 40 只肯让到 36，三方合计仍远超容量，协商永远收敛不了。
    gap_lines = [f"{c.get('resource')}: 三方合计 {c.get('total_ask')} / 容量 {c.get('cap')}"
                 f"，超 {c.get('excess')}，本轮三方合计须再降 {c.get('excess')}"
                 for c in conflicts]
    gap = "\n".join(gap_lines) or "（当前无超额缺口）"

    async def _one(domain: str) -> Dict[str, Any]:
        key = norm_service(domain)
        base = next((r for r in reports if r.get("domain") == domain), {})
        last_ask = (prev.get(last_rnd, {}).get(key)
                    if last_rnd else (base.get("asks") or {}))
        resp = await ROUTER.achat(
            [{"role": "system", "content": P.NEGOTIATE_SYSTEM.format(domain=domain)},
             {"role": "user", "content": P.NEGOTIATE_USER.format(
                 r=rnd, domain=domain,
                 conflicts=json.dumps(conflicts, ensure_ascii=False),
                 last_ask=json.dumps(last_ask or {}, ensure_ascii=False),
                 last_concession=last_conc.get(key, "（本轮为首次协商，尚无让步）"),
                 blackboard=board_view,
                 cap=json.dumps(cap, ensure_ascii=False),
                 gap=gap)}],
            tier=TIER_LIGHT, json_mode=True, role_hint=f"nego:{domain}")
        return {"domain": domain, "data": resp.json()}

    # 各方并行发言（真并发，不是串行）
    raw = await asyncio.gather(*[_one(d) for d in parties])

    # 落盘黑板 + 用协商结果覆盖该军种的申索
    new_reports: List[Dict[str, Any]] = []
    for item in raw:
        d = item["domain"]
        d0 = item["data"] or {}
        asks_new = d0.get("asks") or {}
        old = next((r for r in reports if r.get("domain") == d), {})
        if asks_new:
            data = dict(old.get("data") or {})
            data["asks"] = asks_new
            # add-reducer 只增不减：追加同域新版本，latest_by_domain 会取到它
            new_reports.append({**old, "data": data, "asks": asks_new})
        bb.append(rnd, d,
                  claim=d0.get("claim", ""),
                  objection=d0.get("objection", ""),
                  resource_ask=asks_new,
                  concession=d0.get("concession", ""))

    patch: Dict[str, Any] = {
        "negotiation_round": rnd, "bb_session": bb.session_id,
        "negotiation_parties": parties,
        "trace": [_log("negotiate", f"R{rnd} 参与 {parties}，发言 {len(raw)} 条已落盘")],
        "api_calls": dict(ROUTER.counts)}
    if new_reports:
        patch["staff_reports"] = new_reports
        merged = latest_by_domain((state.get("staff_reports") or []) + new_reports)
        asks_now = [r.get("asks", {}) for r in merged]
        patch["current_asks"] = asks_now
        patch["conflicts"] = detect_conflicts(asks_now, cap)
    return patch


def check_negotiation(state: JointOpsState) -> Dict[str, Any]:
    """收敛判定（0 API）：CV-1~CV-5 短路 + 时机B 硬规则即时校验。

    《设计总纲》§5.4 第 5 条：硬约束必须每轮即时校验，否则参谋会"谈出一个违规方案"。
    """
    bb = Blackboard(state.get("bb_session") or "")
    asks_by_round = bb.asks_by_round()
    obj_by_round = bb.objections_by_round()
    history = [{"round": r, "asks": asks_by_round[r],
                "objections": obj_by_round.get(r, [])}
               for r in sorted(asks_by_round)]
    if not history:
        return {"negotiation_status": "converged",
                "trace": [_log("check", "黑板为空，跳过协商判定")]}

    conflicts = state.get("conflicts") or []
    res = check_convergence(history, conflicts,
                            max_rounds=int(CFG.get("max_negotiation_rounds", 3)))

    # 时机B：每轮即时硬校验。
    # ★ 只校验「单方申索」是否离谱（单方独占就超过总容量 = 恶意要价，直接终止）。
    #   踩过的坑：若把三方合计拿来判 HR-02，协商中间轮次本来就是超的（还在谈），
    #   会每轮都误判"谈出违规方案"而强制终止——协商永远只能跑一轮，形同虚设。
    #   合计是否超限属于收敛判据（CV-1）的活，不是硬规则的活。
    #   同理不能把三军单位人数累加当"投入兵力"判 HR-01：裁减兵力是仲裁的职责，
    #   协商阶段一判就恒违规（三军满编 39800 > 上限 30000）。
    ctx = build_ctx(state)
    hard: List[str] = []
    for a in (state.get("current_asks") or []):
        # ★ 循环变量不能叫 res：Python 没有块级作用域，会覆盖上面的收敛判定结果
        for res_key, v in (a or {}).items():
            cap_v = ctx.resource_cap.get(res_key)
            got = ask_total(v)
            if cap_v is not None and got is not None and got > cap_v:
                hard.append(f"单方申索离谱：{res_key} 单方要 {got} > 总容量 {cap_v}")

    status = res["status"]
    if hard:
        status = "hard_violation"
    return {"negotiation_status": status,
            "trace": [_log("check", f"{res['status']}｜{res['reason']}"
                                    + (f"｜硬规则：{'; '.join(hard)}" if hard else ""))]}


def arbitrate(state: JointOpsState) -> Dict[str, Any]:
    reports = latest_by_domain(state.get("staff_reports", []))
    plans_txt = json.dumps([slim_plan(r["data"]) for r in reports], ensure_ascii=False)
    # 环境实况直接给仲裁层一份：三军各自的驻地环境不同，谁该在坏天气里让出
    # 出动窗口、哪个港口当天不适合装载，只有拿到同一份实况才裁得准。
    env_txt = "\n".join((state.get("env_brief") or {}).values()) or "（无环境数据）"
    fb = feedback_digest()
    resp = ROUTER.chat(
        [{"role": "system", "content": P.ARBITRATE_SYSTEM},
         {"role": "user", "content": P.ARBITRATE_USER.format(
             user_input=state["user_input"],
             constraints=json.dumps(state.get("constraints", {}), ensure_ascii=False),
             conflicts=json.dumps(state.get("conflicts", []), ensure_ascii=False),
             plans=plans_txt,
             env_view=env_txt,
             feedback=fb)}],
        tier=TIER_MAIN, json_mode=True, role_hint="arbitrate")
    data = resp.json()
    fp = data.get("final_plan", {})

    # 溯源不许全交给模型自觉。实测：环境来源一多，模型就把兵力来源那几条挤掉，
    # 「数字来源」变成清一色 env:（上一版还有 5 条 sql:）—— 功能没变，可追溯性
    # 却悄悄退化，而终验只查"有没有来源"、查不出"少了哪一类"。
    # 参谋报过的 sql: 由代码并回去：确定性的事不该指望模型。
    src = fp.get("数字来源") or []
    if isinstance(src, str):
        src = [src]
    src = [str(x) for x in src]
    seen = set(src)
    added = 0
    for r in reports:
        d = r.get("data") or {}
        cand = [u.get("source") for u in (d.get("units") or [])]
        cand += list((d.get("ask_sources") or {}).values())
        for s in cand:
            s = str(s or "")
            if s.startswith("sql:") and s not in seen:
                seen.add(s)
                src.append(s)
                added += 1
    fp["数字来源"] = src

    pv = int(state.get("plan_version", 1) or 1)
    (PLAN_DIR / f"final_plan.v{pv}.json").write_text(
        json.dumps(fp, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"final_plan": fp,
            "trace": [_log("arbitrate", f"合成决心方案，章节 {list(fp.keys())}"),
                      _log("arbitrate", f"数字来源 {len(src)} 条"
                                        f"（补回兵力来源 {added} 条）")],
            "api_calls": dict(ROUTER.counts)}


def validate(state: JointOpsState) -> Dict[str, Any]:
    """终验 0 API：硬规则 + 产物完整性 + 数字对回 SQL + 失败归因"""
    route = state.get("route")
    retry = int(state.get("retry_count", 0) or 0)
    reports = latest_by_domain(state.get("staff_reports", []))
    violations: List[str] = []

    if route == "single_domain":
        # 快通道：数字必须对回 SQL（防模型篡改/心算）
        if not reports:
            violations.append("HR-07 数字无源: 未拿到参谋查询结果")
        else:
            data = reports[0]["data"]
            figures = data.get("figures") or {}
            qid = str(data.get("source", "")).replace("sql:", "")
            qr = POOL.get(qid)
            if not qr or not qr.rows:
                violations.append(f"HR-07 数字无源: query_id={qid} 无法回查")
            else:
                row = qr.rows[0]
                for k, col in (("personnel_total", "personnel_total"),
                               ("equipment_total", "equipment_total"),
                               ("unit_count", "unit_count")):
                    if k in figures and figures[k] != row.get(col):
                        violations.append(
                            f"HR-07 数字对不上: {k}={figures[k]} 但 SQL 为 {row.get(col)}")
        ok = not violations
        validation = {"ok": ok, "code": "PASS" if ok else "FIGURE_MISMATCH",
                      "violations": violations}
        patch: Dict[str, Any] = {"validation": validation,
                                 "trace": [_log("validate", validation["code"])]}
        if not ok:
            patch.update({"failure_type": "plan_violation", "escalate_to": "dispatch",
                          "retry_count": retry + 1})
        return patch

    # 全链路：把三军方案 + 决心方案合成一份待验 plan
    units, asks, ask_sources, timeline, areas = [], {}, {}, [], []
    for r in reports:
        d = r["data"]
        units.extend(d.get("units", []))
        for k, v in (d.get("asks") or {}).items():
            asks[k] = asks.get(k, 0) + v
        ask_sources.update(d.get("ask_sources") or {})
        timeline.extend(d.get("timeline") or [])
        areas.extend(d.get("areas") or [])
    fp = state.get("final_plan") or {}
    # ★ 仲裁已裁决时，终验必须以「裁决结果」为准，不能拿三军参谋的原始申索去判违规
    #   （否则仲裁把 transport 裁到 50 合规、终验却按原始 90 判超限，仲裁等于白做）。
    alloc = dict(fp.get("资源分配") or {})
    force_committed = None
    res_keys = set((CFG.get("resource_cap") or {}).keys())
    if alloc:
        # 兵力键：prompt 已规范为 "force"，但模型常写成 forces / 兵力 / 总兵力，
        # 故先按名字认，认不出再退回"凡不在共享资源清单里的键即兵力"的兜底。
        force_key = next((k for k in alloc if k in ("force", "forces", "兵力", "总兵力")),
                         None) or next((k for k in alloc if k not in res_keys), None)
        if force_key is not None:
            force_committed = ask_total(alloc.pop(force_key))
        # ★ 只保留共享资源键：残留的杂键会让 HR-07 因为找不到 sql: 来源而误判 FAIL
        asks_final = {k: v for k, v in alloc.items()
                      if k in res_keys and ask_total(v) is not None}
    else:
        asks_final = {}
    final_asks = asks_final or asks

    plan = {"domain": "联合", "sections": list(fp.keys()), "units": units,
            "asks": final_asks, "ask_sources": ask_sources,
            "force_committed": force_committed,
            "timeline": timeline, "areas": areas,
            "fire_level": "常规"}

    ctx = build_ctx(state)
    # 有裁决结果就用裁决值做冲突/违规检测；没有才退回参谋原始申索
    asks_for_check = [final_asks] if asks_final else state.get("current_asks", [])
    # ★ 冲突也必须按裁决结果重算：用 collect 阶段那批（参谋原始申索算出的）旧冲突，
    #   会导致仲裁把 transport 裁到 50 之后仍然判定"超限"，仲裁永远无法通过。
    conflicts_for_check = (detect_conflicts(asks_for_check, ctx.resource_cap)
                           if asks_final else state.get("conflicts", []))
    result = attribute_failure([plan], asks_for_check, ctx,
                               conflicts_for_check,
                               required_sections=REQUIRED_SECTIONS)
    ok = result["failure_type"] is None
    validation = {"ok": ok, "code": "PASS" if ok else "VALIDATION_FAILED",
                  "violations": result["reasons"]}
    patch = {"validation": validation,
             "trace": [_log("validate", validation["code"] + " " + str(result["reasons"])[:120])]}
    if not ok:
        patch.update({"failure_type": result["failure_type"],
                      "escalate_to": result["escalate_to"],
                      "retry_count": retry + 1})
        if retry + 1 >= CFG["max_retry"]:
            patch.update({"degraded": True,
                          "answer": "方案未通过硬规则校验（已达重试上限），输出降级结果："
                                    + "; ".join(result["reasons"])})
    return patch


# ── 边 ───────────────────────────────────────────────────
def route_after_collect(state: JointOpsState) -> str:
    """单域直奔终验；跨域有冲突先进协商，无冲突直接仲裁"""
    if state.get("route") == "single_domain":
        return "validate"
    return "negotiate" if state.get("conflicts") else "arbitrate"


def route_after_check(state: JointOpsState) -> str:
    """continue → 再谈一轮；其余（收敛/震荡/封顶/停滞/硬违规）一律交仲裁"""
    return "negotiate" if state.get("negotiation_status") == "continue" else "arbitrate"


def route_after_intent(state: JointOpsState) -> str:
    return {"out_of_scope": "reject", "need_clarify": "clarify",
            "full_chain": "commander", "single_domain": "dispatch"}.get(
        state.get("route", "need_clarify"), "clarify")


def route_after_validate(state: JointOpsState) -> str:
    if (state.get("validation") or {}).get("ok"):
        return END
    if state.get("degraded"):
        return END
    esc = state.get("escalate_to")
    if state.get("failure_type") == "constraint_conflict":
        return "intent" if not state.get("clarify_question") else END
    return {"commander": "commander", "arbitrate": "arbitrate",
            "dispatch": "dispatch"}.get(esc, END)


def build_graph(checkpointer=None):
    g = StateGraph(JointOpsState)
    g.add_node("intent", intent)
    g.add_node("reject", reject)
    g.add_node("clarify", clarify)
    g.add_node("commander", commander)
    g.add_node("dispatch", dispatch)          # 独立分发节点：staff 挂条件边会自旋
    g.add_node("staff", staff)
    g.add_node("collect", collect)
    g.add_node("negotiate", negotiate)        # 二期：协商子循环
    g.add_node("check_negotiation", check_negotiation)
    g.add_node("arbitrate", arbitrate)
    g.add_node("validate", validate)

    g.add_edge(START, "intent")
    g.add_conditional_edges("intent", route_after_intent,
                            {"reject": "reject", "clarify": "clarify",
                             "commander": "commander", "dispatch": "dispatch"})
    g.add_edge("commander", "dispatch")
    g.add_conditional_edges("dispatch", fanout, {"staff": "staff"})
    g.add_edge("staff", "collect")
    g.add_conditional_edges("collect", route_after_collect,
                            {"validate": "validate", "negotiate": "negotiate",
                             "arbitrate": "arbitrate"})
    g.add_edge("negotiate", "check_negotiation")
    g.add_conditional_edges("check_negotiation", route_after_check,
                            {"negotiate": "negotiate", "arbitrate": "arbitrate"})
    g.add_edge("arbitrate", "validate")
    g.add_conditional_edges("validate", route_after_validate,
                            {"commander": "commander", "arbitrate": "arbitrate",
                             "dispatch": "dispatch", "intent": "intent", END: END})
    g.add_edge("reject", END)
    g.add_edge("clarify", END)
    # 三期：挂载 checkpointer（由调用方注入，可为 None）
    return g.compile(checkpointer=checkpointer)


# 无会话场景（单条提问、验收用例）用的图：不挂 checkpointer，省一次 DB 连接
GRAPH = build_graph()


def async_checkpointer():
    """多轮会话用的异步 checkpointer，必须在 async 上下文里进出。

    ★ 两个坑：
      1. 同步 SqliteSaver 配 ainvoke 会直接 NotImplementedError（不支持 async 方法）
      2. AsyncSqliteSaver 的连接绑定事件循环，不能做成模块级单例跨 asyncio.run 复用，
         故每次调用都新建、用完即关（checkpoint 数据在 DB 文件里，不受连接生命周期影响）
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    return AsyncSqliteSaver.from_conn_string(str(ROOT / "db" / "joint_ops.db"))
