# -*- coding: utf-8 -*-
"""
联合作战指挥多Agent系统 —— LangGraph 骨架可执行性测试 (v2.2)
v2.2 变更（回应"失败了为什么回 commander 而不是回 intent"的质疑）：
  ★ 失败恢复由"单条回 commander"升级为【分层回退 + 归因分派】：
      ① 方案求解失败(超限/违规)       → 回 commander 重新下发约束（约束没错，是求解没做到）
      ② 产物格式残缺(缺章节/数字无源) → 局部修复：回 arbitrate 重合成（不重跑参谋，省成本）
      ③ 任务单约束自相矛盾            → 回 intent 并【升级为用户澄清】(interrupt 语义)，不硬重试
    归因为规则判定（0 API）；每层 retry<=2 封顶，仍失败则结构化降级输出。
v2.1 内容（保留）：失败恢复回环（不崩溃）、产物完整性闸门、参谋失败降级。
验证点：
  T1 图能编译  T2 四态路由  T3 Send 并行三参谋  T4 协商循环封顶<=3
  T5 双档模型计数  T6 硬规则终验
  T7 归因=方案违规 → 回 commander 恢复成功
  T8 归因=产物残缺 → 局部修复（回仲裁，参谋不重跑）成功
  T9 重试封顶降级（注入必败故障→2次后输出结构化失败报告而非崩溃）
  T10 参谋失败降级（海军失联→两军种规划+信息缺口标注）
  T11 归因=约束矛盾 → 回 intent 并升级为用户澄清（NEED_USER_CLARIFICATION）
  T12 三层回退均被实际走到（intent / commander / arbitrate）
所有 LLM 均为 Mock：验证控制流，不验证语义。故障注入通过任务单 fault 字段模拟。
"""
import json, sys
from typing import Annotated, Literal, TypedDict
from operator import add
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

# ---------- State ----------
class JointOpsState(TypedDict, total=False):
    user_input: str
    task_order: dict
    route: str                       # single_domain | full_chain | out_of_scope | need_clarify
    staff_reports: Annotated[list, add]
    current_asks: list
    conflicts: list
    negotiation_round: int
    blackboard: Annotated[list, add]
    final_plan: dict
    final_asks: list
    retry_count: int                 # 恢复回环计数（每层封顶2）
    validation_failed: bool
    failure_reason: list
    failure_type: str                # plan_violation | artifact_incomplete | constraint_conflict
    escalate_to: str                 # commander | arbitrate | intent
    clarify_question: str            # 升级到人工澄清时的问题文本
    info_gap: list

class MockLLM:
    def __init__(self):
        self.tier_counters = {"main": 0, "light": 0}
    def invoke(self, tier: str, node: str, prompt: str) -> str:
        self.tier_counters[tier] += 1
        return f"[{tier}|{node}] mock"
llm = MockLLM()

DOMAIN_STAFF = {"陆军": "army", "海军": "navy", "空军": "airforce"}
CAP = {"transport": 50, "fires": 15}
INIT_ASKS = {"陆军": {"transport": 30, "fires": 10},
             "海军": {"transport": 20, "fires": 5},
             "空军": {"transport": 25, "fires": 8}}

def find_conflicts(asks):
    return [{"resource": k, "total_ask": sum(a[k] for a in asks), "cap": CAP[k]}
            for k in CAP if sum(a[k] for a in asks) > CAP[k]]

def latest_by_domain(reports):
    out = {}
    for r in reports:
        out[r["domain"]] = r
    return list(out.values())

# ---------- 节点 ----------
def intent_agent(state) -> dict:
    llm.invoke("light", "intent_agent", state["user_input"])
    # ★ 从 validate 升级回来的情形：不再硬重试，直接转人工澄清
    if state.get("escalate_to") == "intent":
        return {"route": "need_clarify",
                "clarify_question": "任务单约束自相矛盾，请确认优先级：" + "; ".join(state.get("failure_reason", []))}
    text = state["user_input"]
    if "周报" in text:
        return {"route": "out_of_scope",
                "task_order": {"domain": "OOS", "type": "reject"}}
    fault = ("validate_once" if "校验故障" in text else
             "incomplete_once" if "残缺故障" in text else
             "always" if "必败故障" in text else None)
    staff_fail = "海军失联" in text
    infeasible = "约束矛盾" in text
    for dom in DOMAIN_STAFF:
        if dom in text and "作战" not in text and "联合" not in text:
            return {"route": "single_domain",
                    "task_order": {"domain": dom, "type": "query", "need_joint": False}}
    return {"route": "full_chain",
            "task_order": {"domain": "联合", "type": "plan", "need_joint": True,
                           "constraints": {"deadline": "D+2", "force_cap": "60%",
                                           "infeasible": infeasible},
                           "fault": fault, "staff_fail": staff_fail}}

def reject_node(state) -> dict:
    return {"final_plan": {"status": "REJECTED_OUT_OF_SCOPE",
                           "reason": "领域外请求，垂直 agent 明示拒答"}}

def clarify_node(state) -> dict:
    """人工澄清出口（对应 s20 的 interrupt() 语义）"""
    return {"final_plan": {"status": "NEED_USER_CLARIFICATION",
                           "question": state.get("clarify_question")}}

def commander(state) -> dict:
    """拆解+约束；仅在归因为方案求解失败时被回退到此并重下发"""
    llm.invoke("main", "commander", json.dumps(state["task_order"], ensure_ascii=False))
    to = dict(state["task_order"])
    events = [{"round": 0, "speaker": "commander", "event": "下发全局约束"}]
    if state.get("validation_failed") and state.get("escalate_to") == "commander":
        if to.get("fault") != "always":
            to["fault"] = None
        events = [{"round": state.get("negotiation_round", 0), "speaker": "commander",
                   "event": "失败恢复:修正后重新下发约束(" + ";".join(state["failure_reason"]) + ")"}]
    return {"task_order": to, "blackboard": events}

def dispatch(state):
    need = ["陆军", "海军", "空军"] if state["task_order"]["need_joint"] \
           else [state["task_order"]["domain"]]
    return [Send("staff", {"task_order": state["task_order"], "domain": d})
            for d in need]

def staff(state) -> dict:
    d = state["domain"]
    if state["task_order"].get("staff_fail") and d == "海军":
        return {"staff_reports": [{"domain": d, "error": "spawn_failure", "version": 1}]}
    llm.invoke("light", f"staff_{DOMAIN_STAFF[d]}_dbquery", "SELECT SUM(...) FROM units")
    llm.invoke("main", f"staff_{DOMAIN_STAFF[d]}", "拟案")
    return {"staff_reports": [{"domain": d, "resource_ask": dict(INIT_ASKS[d]),
                               "version": 1}]}

def collect(state) -> dict:
    reports = latest_by_domain(state["staff_reports"])
    ok = [r for r in reports if "error" not in r]
    gaps = [f"{r['domain']}参谋失败({r.get('error')})，降级为其余军种规划"
            for r in reports if "error" in r]
    asks = [r["resource_ask"] for r in ok]
    if state.get("route") == "single_domain":
        return {"current_asks": asks, "conflicts": [], "negotiation_round": 0,
                "final_asks": [a["transport"] for a in asks],
                "final_plan": {"status": "APPROVED_QUICK_PATH",
                               "answer_from": "SQL直查，跳过指挥与仲裁",
                               "negotiation_rounds": 0}}
    d = {"current_asks": asks, "conflicts": find_conflicts(asks),
         "negotiation_round": 0}
    if gaps:
        d["info_gap"] = gaps
    return d

def negotiate(state) -> dict:
    rnd = state["negotiation_round"] + 1
    for _ in state["current_asks"]:
        llm.invoke("main", "negotiate", f"round{rnd}")
    revised = [{**a, "transport": max(0, int(a["transport"] * 0.7))}
               for a in state["current_asks"]]
    return {"negotiation_round": rnd, "current_asks": revised,
            "blackboard": [{"round": rnd, "speaker": "all", "event": "让步修订"}]}

def check_convergence(state) -> Literal["negotiate", "arbitrate"]:
    if state["negotiation_round"] >= 3:
        return "arbitrate"
    return "negotiate" if find_conflicts(state["current_asks"]) else "arbitrate"

def arbitrate(state) -> dict:
    llm.invoke("main", "arbitrate", json.dumps(state["conflicts"], ensure_ascii=False))
    asks = state["current_asks"]
    fault = state["task_order"].get("fault")
    # 局部修复回退：本次是产物残缺，重合成时不再注入残缺
    if state.get("validation_failed") and state.get("failure_type") == "artifact_incomplete":
        fault = None
    total = sum(a["transport"] for a in asks)
    cut = CAP["transport"] / total if total > CAP["transport"] else 1.0
    final = [int(a["transport"] * cut) for a in asks]
    plan = {"status": "APPROVED", "negotiation_rounds": state["negotiation_round"],
            "时序表": "mock-时序", "风险": "mock-风险", "资源来源": "SQL"}
    if fault in ("validate_once", "always"):
        final = [final[0] + 100] + final[1:]
    elif fault == "incomplete_once":
        plan.pop("风险")
    if state.get("info_gap"):
        plan["信息缺口"] = ";".join(state["info_gap"])
        plan["status"] = "APPROVED_DEGRADED"
    return {"final_asks": final, "final_plan": plan}

def validate(state) -> dict:
    """终验 = 硬规则 + 完整性闸门 + ★失败归因（0 API）→ 决定回退层"""
    plan = dict(state.get("final_plan", {}))
    retry = state.get("retry_count") or 0
    route = state.get("route")
    failures, ftype = [], None

    # 归因①：任务单约束自相矛盾 → 意图层问题（指挥层无解）
    if (state.get("task_order") or {}).get("constraints", {}).get("infeasible"):
        failures.append("约束不可满足:任务单约束自相矛盾")
        ftype = "constraint_conflict"
    # 硬规则
    total = sum(state.get("final_asks", []))
    if total > CAP["transport"]:
        failures.append(f"硬规则违规:transport总数{total}>{CAP['transport']}")
        ftype = ftype or "plan_violation"
    # 归因②：产物完整性
    required = (["status", "answer_from"] if route == "single_domain"
                else ["status", "时序表", "风险", "资源来源"])
    missing = [k for k in required if k not in plan]
    if missing:
        failures.append("产物缺章节:" + ",".join(missing))
        ftype = ftype or "artifact_incomplete"
    if route != "single_domain" and plan.get("资源来源") != "SQL":
        failures.append("数字未对回SQL来源")
        ftype = ftype or "artifact_incomplete"

    if not failures:
        return {"validation_failed": False,
                "final_plan": {**plan, "hard_rules": "PASS", "completeness": "PASS"}}

    layer = {"constraint_conflict": "intent",          # ③ 升级：回意图层并转人工澄清
             "plan_violation": "commander",            # ① 回到约束/编排层重下发
             "artifact_incomplete": "arbitrate"}[ftype]  # ② 局部修复：只重合成产物
    if retry >= 2:
        return {"validation_failed": False,
                "final_plan": {"status": "FAILED_VALIDATION_DEGRADED",
                               "reason": failures, "retry_count": retry}}
    return {"validation_failed": True, "retry_count": retry + 1,
            "failure_reason": failures, "failure_type": ftype, "escalate_to": layer,
            "blackboard": [{"round": state.get("negotiation_round", 0), "speaker": "validate",
                            "event": f"失败归因:{ftype} → 回退层:{layer}"}]}

def build_graph():
    g = StateGraph(JointOpsState)
    for n, f in [("intent", intent_agent), ("reject", reject_node), ("clarify", clarify_node),
                 ("commander", commander), ("dispatch", lambda s: {}), ("staff", staff),
                 ("collect", collect), ("negotiate", negotiate),
                 ("arbitrate", arbitrate), ("validate", validate)]:
        g.add_node(n, f)
    g.add_edge(START, "intent")
    g.add_conditional_edges("intent", lambda s: s["route"], {
        "single_domain": "dispatch", "full_chain": "commander",
        "out_of_scope": "reject", "need_clarify": "clarify"})
    g.add_edge("reject", END)
    g.add_edge("clarify", END)
    g.add_edge("commander", "dispatch")
    g.add_conditional_edges("dispatch", dispatch, ["staff"])
    g.add_edge("staff", "collect")
    g.add_conditional_edges("collect",
        lambda s: "validate" if s["route"] == "single_domain"
                  else ("negotiate" if s["conflicts"] else "arbitrate"))
    g.add_conditional_edges("negotiate", check_convergence,
                            {"negotiate": "negotiate", "arbitrate": "arbitrate"})
    g.add_edge("arbitrate", "validate")
    # ★ v2.2 分层回退：归因决定回退目标层（intent / commander / arbitrate）
    g.add_conditional_edges("validate",
        lambda s: s.get("escalate_to") if s.get("validation_failed") else "finish",
        {"intent": "intent", "commander": "commander",
         "arbitrate": "arbitrate", "finish": END})
    return g.compile()

def run_case(name, text):
    result = build_graph().invoke({"user_input": text, "staff_reports": [],
                                   "blackboard": []})
    layers = [e["event"] for e in result.get("blackboard", []) if "回退层" in e.get("event", "")]
    print(f"\n=== 用例: {name} ===")
    print(f"  输入: {text}")
    print(f"  参谋报告数: {len(result.get('staff_reports', []))}  协商轮: {result.get('negotiation_round')}")
    if layers:
        print(f"  归因轨迹: {layers}")
    print(f"  最终方案: {json.dumps(result.get('final_plan', {}), ensure_ascii=False)}")
    return result

if __name__ == "__main__":
    r1 = run_case("领域外拒答", "帮我写个周报")
    r2 = run_case("单域快通道", "陆军现在多少人多少装备")
    r3 = run_case("全链路正常", "复杂登陆作战背景，陆海空联合行动规划")
    r4 = run_case("方案超限(应回 commander)", "联合登陆规划，注入校验故障")
    r5 = run_case("产物残缺(应局部修复·参谋不重跑)", "联合登陆规划，注入残缺故障")
    r6 = run_case("必败故障(应封顶降级)", "联合登陆规划，注入必败故障")
    r7 = run_case("海军失联(应降级规划)", "联合登陆规划，海军失联")
    r8 = run_case("约束矛盾(应回 intent 并转人工澄清)", "联合登陆规划，约束矛盾")

    print("\n========== 测试汇总 ==========")
    checks = []
    checks.append(("T1 图编译可执行", True))
    checks.append(("T2 拒答/快通道/全链路路由",
                   r1["final_plan"]["status"] == "REJECTED_OUT_OF_SCOPE"
                   and r2["task_order"].get("domain") == "陆军"
                   and len(r3["staff_reports"]) == 3))
    checks.append(("T4 协商循环<=3轮封顶", 0 < (r3["negotiation_round"] or 0) <= 3))
    checks.append(("T6 硬规则终验通过", r3["final_plan"].get("hard_rules") == "PASS"))
    ev = lambda r, kw: any(kw in e.get("event", "") for e in r["blackboard"])
    checks.append(("T7 归因=方案违规→回 commander 恢复成功",
                   r4["final_plan"].get("hard_rules") == "PASS"
                   and ev(r4, "回退层:commander") and ev(r4, "失败恢复")))
    checks.append(("T8 归因=产物残缺→局部修复(参谋不重跑)",
                   r5["final_plan"].get("completeness") == "PASS"
                   and ev(r5, "回退层:arbitrate")
                   and len(r5["staff_reports"]) == 3))
    checks.append(("T9 重试封顶:结构化降级输出而非崩溃",
                   r6["final_plan"].get("status") == "FAILED_VALIDATION_DEGRADED"
                   and (r6.get("retry_count") or 0) >= 2))
    checks.append(("T10 参谋失败降级:两军种规划+信息缺口标注",
                   r7["final_plan"].get("status") == "APPROVED_DEGRADED"
                   and "信息缺口" in r7["final_plan"]))
    checks.append(("T11 归因=约束矛盾→回 intent 并升级人工澄清",
                   r8["final_plan"].get("status") == "NEED_USER_CLARIFICATION"
                   and ev(r8, "回退层:intent")))
    checks.append(("T12 三层回退均被实际走到",
                   ev(r4, "回退层:commander") and ev(r5, "回退层:arbitrate") and ev(r8, "回退层:intent")))
    for name, ok in checks:
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    print(f"  T5 双档计数(八用例累计): 主力档={llm.tier_counters['main']}次, "
          f"轻量档={llm.tier_counters['light']}次")
    all_ok = all(ok for _, ok in checks)
    print(f"\n总体: {'ALL PASS' if all_ok else 'HAS FAILURES'}")
    sys.exit(0 if all_ok else 1)
