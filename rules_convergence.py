# -*- coding: utf-8 -*-
"""
rules_convergence.py —— 硬规则引擎 · 冲突检测 · 收敛判据 · 失败归因

设计约束（对应《项目设计文档》§4.3 / §4.5）：
  · 全部 0 API —— 判定类逻辑绝不交给 LLM（成本 + 可控性双保险）
  · 纯函数：输入 (plan, ctx, history) → 输出结构化判定，不依赖任何上下文
  · 三处调用时机：A 参谋拟案后自纠 / B 协商每轮修订后 / C validate 终验

用法：被 staff 子图、collect、negotiate、validate 四个节点 import 调用。
"""
from __future__ import annotations
import sys
from dataclasses import dataclass, field
from math import ceil
from typing import Any, Callable, Dict, List, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ────────────────────────────────────────────────────────────
# 1. 上下文与返回值
# ────────────────────────────────────────────────────────────
@dataclass
class RuleContext:
    """指挥层下发的全局约束 + 资源容量 + 兵力池快照（全部来自配置与 SQL）"""
    force_cap: int                       # 兵力上限（人）
    deadline: int                        # 时间窗（D+n 的 n）
    no_go_zones: List[str] = field(default_factory=list)
    roe_allowed: List[str] = field(default_factory=lambda: ["常规"])
    resource_cap: Dict[str, int] = field(default_factory=dict)   # 共享资源容量
    force_pool: Dict[str, Dict] = field(default_factory=dict)    # {domain: {unit_id: {...}}}
    required_personnel: int = 0          # 任务需求兵力（用于可行性下界计算）
    per_sortie_capacity: int = 800       # 单架次投送能力（人）
    sorties_per_day: int = 2             # 每日出动架次


@dataclass
class Verdict:
    ok: bool
    code: str = "PASS"
    violations: List[str] = field(default_factory=list)


# ────────────────────────────────────────────────────────────
# 2. 硬规则表（加规则 = 加一个函数，引擎不动，对应 s02 dispatch table 纪律）
# ────────────────────────────────────────────────────────────
HARD_RULES: List[tuple] = []


def rule(rid: str, name: str):
    def deco(fn: Callable):
        HARD_RULES.append((rid, name, fn))
        return fn
    return deco


def _parse_dn(s: str) -> int:
    """'D+2' → 2；'D+0' → 0"""
    try:
        return int(str(s).upper().replace("D+", "").strip())
    except Exception:
        return 0


@rule("HR-01", "兵力上限")
def hr01_force_cap(plan: dict, ctx: RuleContext) -> List[str]:
    # 仲裁若已给出裁决后的投送兵力（force_committed），以它为准；
    # 否则才按方案中各单位人数累加（参谋初案场景）。
    override = plan.get("force_committed")
    committed = override if isinstance(override, (int, float)) else \
        sum(u.get("personnel", 0) for u in plan.get("units", []))
    return [f"HR-01 兵力上限: 投入{committed} > 上限{ctx.force_cap}"] if committed > ctx.force_cap else []


# 模型给「按军种拆分」时，总和键的叫法五花八门：合计/总计/总量/小计/上限…
TOTAL_KEYS = ("总计", "合计", "总量", "总额", "小计", "总和", "上限", "限额",
              "total", "sum", "all", "cap", "capacity")
# 明确表示「实际投入/占用」的键，优先于一切求和
COMMIT_KEYS = ("投入", "实投", "首批", "实际", "实占", "占用",
               "committed", "used", "allocated", "assigned")
# 不计入「投入兵力」的键：预备队不入列，不算占额度
RESERVE_KEYS = ("预备队", "预备", "后备", "reserve", "backup", "二梯队")


def _ask_total(ask) -> Optional[float]:
    """把申索值归一成数字。

    模型会返回两种形态：
      形态A（参谋方案）  {"transport": 30}                      → 直接取 30
      形态B（仲裁裁决）  {"transport": {"陆军":25,"海军":5,"总计":50}}

    三个坑（都真实踩过）：
      1. 只认形态A → 形态B 上 `ask > cap` 抛 TypeError，整条链路崩掉
      2. 无脑对全部分支求和 → 总和键也被算进分子（30+5+15+50=100），凭空多一倍
      3. ★ 反过来「优先取总和键」同样有坑：模型常把容量写进 `cap` 键，
         {"cap":50,"陆军":60} 会被判成 50 而放行真正的超限——假阴性。
         故改为：拆分项求和优先，总和键只作兜底。
    """
    if isinstance(ask, bool):
        return None
    if isinstance(ask, (int, float)):
        return ask
    if isinstance(ask, dict):
        # 1) 有明确的「投入/占用」键，直接采信
        for k in COMMIT_KEYS:
            if k in ask and isinstance(ask.get(k), (int, float)):
                return ask[k]
        # 2) 拆分项求和（排除总和键与预备类键）——最贴近真实占用
        skip = set(TOTAL_KEYS) | set(RESERVE_KEYS)
        parts = [v for k, v in ask.items()
                 if k not in skip and isinstance(v, (int, float))
                 and not isinstance(v, bool)]
        if parts:
            return sum(parts)
        # 3) 只有总和键时兜底
        for k in TOTAL_KEYS:
            if k in ask and isinstance(ask.get(k), (int, float)):
                return ask[k]
        return None
    return None


# 对外名字（graph 层也要用来归一化裁决结果）
ask_total = _ask_total


@rule("HR-02", "共享资源申索不得超容量")
def hr02_resource_ask(plan: dict, ctx: RuleContext) -> List[str]:
    out = []
    for res, ask in (plan.get("asks") or {}).items():
        cap = ctx.resource_cap.get(res)
        total = _ask_total(ask)
        if cap is not None and total is not None and total > cap:
            out.append(f"HR-02 资源超限: {res} 申索{total} > 容量{cap}")
    return out


@rule("HR-03", "时间窗")
def hr03_deadline(plan: dict, ctx: RuleContext) -> List[str]:
    ends = [_parse_dn(t.get("end")) for t in plan.get("timeline", [])] or [0]
    latest = max(ends)
    return [f"HR-03 时间窗: 计划完成 D+{latest} > 期限 D+{ctx.deadline}"] if latest > ctx.deadline else []


@rule("HR-04", "禁区")
def hr04_no_go(plan: dict, ctx: RuleContext) -> List[str]:
    hit = [a for a in plan.get("areas", []) if a in ctx.no_go_zones]
    return [f"HR-04 禁区: 涉及 {hit}"] if hit else []


@rule("HR-05", "交战规则 ROE")
def hr05_roe(plan: dict, ctx: RuleContext) -> List[str]:
    lvl = plan.get("fire_level")
    if lvl and ctx.roe_allowed and lvl not in ctx.roe_allowed:
        return [f"HR-05 ROE: 火力等级「{lvl}」不在许可集{ctx.roe_allowed}"]
    return []


@rule("HR-06", "编成必须真实存在（防幻觉单位）")
def hr06_unit_exists(plan: dict, ctx: RuleContext) -> List[str]:
    pool = ctx.force_pool.get(plan.get("domain", ""), {})
    out = []
    for u in plan.get("units", []):
        if u.get("unit_id") not in pool:
            out.append(f"HR-06 编成不存在: {u.get('unit_id')} 不在兵力池")
    return out


# 可接受的溯源前缀。
#   sql: 本地兵力池（force_query）
#   env: 外部环境数据（env_query，第五期新增）
# 原先只认 sql:，导致引用真实气象/高程数字反被判违规 —— 规则本意是"不许凭空造数"，
# 而不是"不许用 SQL 以外的工具"。越把来源限定死，模型越倾向于不用工具直接编。
TRUSTED_SOURCE = ("sql:", "env:")


@rule("HR-07", "数字可溯源（必须来自工具）")
def hr07_traceable(plan: dict, ctx: RuleContext) -> List[str]:
    out = []
    for u in plan.get("units", []):
        if "personnel" in u and not str(u.get("source", "")).startswith(TRUSTED_SOURCE):
            out.append(f"HR-07 数字无源: {u.get('unit_id')}.personnel 缺少 sql:/env: 来源")
    for res, ask in (plan.get("asks") or {}).items():
        if not str(plan.get("ask_sources", {}).get(res, "")).startswith(TRUSTED_SOURCE):
            out.append(f"HR-07 数字无源: asks.{res} 缺少 sql:/env: 来源")
    # 环境数字单独校验：必须是 env: 前缀，不能拿 sql: 糊弄过去
    for key, src in (plan.get("env_sources") or {}).items():
        s = str(src)
        if not s.startswith("env:"):
            out.append(f"HR-07 环境数据无源: env.{key} 需 env: 来源（当前 {s or '空'}）")
    return out


def run_hard_rules(plan: dict, ctx: RuleContext) -> Verdict:
    """执行全部硬规则（时机 A/B/C 共用）"""
    vs: List[str] = []
    for _rid, _name, fn in HARD_RULES:
        vs.extend(fn(plan, ctx))
    return Verdict(ok=not vs, code="PASS" if not vs else "HARD_RULE_VIOLATION", violations=vs)


# ────────────────────────────────────────────────────────────
# 3. 冲突检测（共享资源争抢，collect 节点，0 API）
# ────────────────────────────────────────────────────────────
def detect_conflicts(asks_list: List[dict], cap: Dict[str, int]) -> List[dict]:
    """各方申索求和，超容量即为冲突。

    申索值既可能是数字（参谋初案），也可能是按军种拆分的 dict（仲裁裁决），
    统一走 ask_total 归一，否则求和时会对 dict 直接相加而崩溃。
    """
    out = []
    for res, c in cap.items():
        total = sum(ask_total(a.get(res, 0)) or 0 for a in asks_list)
        if total > c:
            parties = [i for i, a in enumerate(asks_list)
                       if (ask_total(a.get(res, 0)) or 0) > 0]
            out.append({"resource": res, "total_ask": total, "cap": c, "excess": total - c,
                        "parties": parties})
    return out


# ────────────────────────────────────────────────────────────
# 4. 收敛判据（check_convergence，0 API）
# ────────────────────────────────────────────────────────────
MAX_ROUNDS = 3
MARGIN_THRESHOLD = 0.02      # 边际收益阈值：总申索降幅 < 2% 视为停滞


def _total(asks: Dict[str, dict]) -> int:
    return sum(sum(v.values()) for v in asks.values())


def check_convergence(history: List[dict],
                      conflicts: List[dict],
                      max_rounds: int = MAX_ROUNDS) -> dict:
    """
    history: [{"round":1, "asks":{"army":{"transport":30,...}, ...}, "objections":["..."]}, ...]
    判定顺序（短路，先判定最刚性的）：
      CV-2 轮次封顶 > CV-5 震荡 > CV-1 无冲突 > CV-4 无新反对 > CV-3 边际停滞 > 继续
    返回 {"status": converged|oscillating|cap_reached|stagnated|continue, "reason":...}
    """
    rnd = history[-1]["round"] if history else 0
    cur = history[-1]["asks"] if history else {}

    # CV-2 轮次封顶
    if rnd >= max_rounds:
        return {"status": "cap_reached", "round": rnd,
                "reason": f"CV-2 已达轮次上限 {max_rounds}，强制裁决"}

    # CV-5 震荡：任一方把上一轮的让步收回（申索不降反增）
    if len(history) >= 2:
        prev = history[-2]["asks"]
        for party, ask in cur.items():
            for res, v in ask.items():
                if v > prev.get(party, {}).get(res, 0):
                    return {"status": "oscillating", "round": rnd,
                            "reason": f"CV-5 震荡：{party}.{res} 由 {prev.get(party, {}).get(res, 0)} 回升到 {v}"}

    # CV-1 无冲突
    if not conflicts:
        return {"status": "converged", "round": rnd, "reason": "CV-1 无资源冲突"}

    # CV-4 无新增 objection
    if len(history) >= 2:
        seen = set()
        for h in history[:-1]:
            seen.update(h.get("objections", []))
        new_objs = [o for o in history[-1].get("objections", []) if o not in seen]
        if not new_objs:
            return {"status": "converged", "round": rnd, "reason": "CV-4 本轮无新增反对意见"}

    # CV-3 边际收益停滞
    if len(history) >= 2:
        t_prev, t_cur = _total(history[-2]["asks"]), _total(cur)
        if t_prev > 0 and (t_prev - t_cur) / t_prev < MARGIN_THRESHOLD:
            return {"status": "stagnated", "round": rnd,
                    "reason": f"CV-3 边际停滞：总申索 {t_prev}→{t_cur}，降幅 {(t_prev - t_cur) / t_prev:.1%} < {MARGIN_THRESHOLD:.0%}"}

    return {"status": "continue", "round": rnd, "reason": "未收敛，进入下一轮"}


# ────────────────────────────────────────────────────────────
# 4b. 三期：约束 diff 与影响面分析（0 API，机器可判）
# ────────────────────────────────────────────────────────────
def diff_constraints(old: Optional[Dict[str, Any]],
                     new: Optional[Dict[str, Any]]) -> List[str]:
    """对比两轮约束，返回变更项清单。首轮（old 为空）视为无变更。

    返回形如 ["resource_cap.transport", "force_cap", "no_go_zones"]，
    供 impacted_domains 判断谁需要重算。
    """
    old, new = old or {}, new or {}
    # ★ 首轮没有旧约束时，所有键都是"新增"——但那不是"变更"，不应触发增量重规划。
    #   不判这一条，首轮会被误当成增量，plan_version 平白跳到 v2。
    if not old:
        return []
    changed: List[str] = []
    for k in set(old) | set(new):
        ov, nv = old.get(k), new.get(k)
        if k == "resource_cap":
            for r in set((ov or {})) | set((nv or {})):
                if (ov or {}).get(r) != (nv or {}).get(r):
                    changed.append(f"resource_cap.{r}")
            continue
        if isinstance(ov, list) or isinstance(nv, list):
            if (ov or []) != (nv or []):
                changed.append(k)
            continue
        if ov != nv:
            changed.append(k)
    return sorted(changed)


def impacted_domains(reports: List[dict], changed: List[str],
                     old: Optional[Dict[str, Any]] = None,
                     new: Optional[Dict[str, Any]] = None
                     ) -> tuple[List[str], List[str]]:
    """按变更项判定哪些军种需要重算，哪些可以直接复用上一版方案。

    判据（全部机器可判，不靠模型判断）：
      resource_cap.X 变了 → 只在 X 上有申索的军种受影响
      force_cap 变了     → 全体（兵力额度是全局分配）
      deadline_days 变了 → 全体（时序贯穿三军）
      no_go_zones 变了   → 方案 areas 涉及变更区域的军种
      roe_allowed 变了   → fire_level 已不在新许可集的军种
    """
    if not changed:
        return [], [r.get("domain", "?") for r in reports]

    res_changed = {c.split(".", 1)[1] for c in changed if c.startswith("resource_cap.")}
    global_changed = any(c in ("force_cap", "deadline_days") for c in changed)
    zones_changed = "no_go_zones" in changed
    roe_changed = "roe_allowed" in changed
    new_zones = set((new or {}).get("no_go_zones") or [])
    old_zones = set((old or {}).get("no_go_zones") or [])
    zone_delta = (new_zones ^ old_zones) if zones_changed else set()
    allowed = set((new or {}).get("roe_allowed") or [])

    impacted, reused = [], []
    for r in reports:
        dom = r.get("domain", "?")
        data = r.get("data") or {}
        hit = False
        if global_changed:
            hit = True
        if not hit and res_changed:
            asks = r.get("asks") or data.get("asks") or {}
            if any((_ask_total(asks.get(x, 0)) or 0) > 0 for x in res_changed):
                hit = True
        if not hit and zone_delta:
            if set(data.get("areas") or []) & zone_delta:
                hit = True
        if not hit and roe_changed and allowed:
            lvl = data.get("fire_level")
            if lvl and lvl not in allowed:
                hit = True
        (impacted if hit else reused).append(dom)
    return impacted, reused


# ────────────────────────────────────────────────────────────
# 5. 失败归因（validate 节点，0 API）→ 决定回退层
# ────────────────────────────────────────────────────────────
def estimate_min_days(required_personnel: int, transport_cap: int,
                      per_sortie: int = 800, sorties_per_day: int = 2) -> int:
    """可行性下界：投送 X 人至少需要的天数（机器可判，非 LLM 估算）"""
    if transport_cap <= 0:
        return 10 ** 6
    return ceil(required_personnel / (transport_cap * per_sortie * sorties_per_day))


def attribute_failure(plans: List[dict], asks_list: List[dict],
                      ctx: RuleContext, conflicts: List[dict],
                      required_sections: List[str] = None) -> dict:
    """
    归因优先级 = 根因优先：约束不可满足 > 方案违规 > 产物残缺
    返回 {"failure_type": ..., "escalate_to": ..., "reasons": [...]}
    """
    required_sections = required_sections or ["时序表", "风险", "资源来源"]
    reasons: List[str] = []

    # ① 约束自相矛盾：需求兵力在给定运力下所需天数 > 时间窗，或上限低于需求
    min_days = estimate_min_days(ctx.required_personnel,
                                 ctx.resource_cap.get("transport", 0),
                                 ctx.per_sortie_capacity, ctx.sorties_per_day)
    if min_days > ctx.deadline:
        reasons.append(f"约束不可满足: 投送{ctx.required_personnel}人需{ctx.deadline}天内完成，"
                       f"但运力下界需 {min_days} 天")
        return {"failure_type": "constraint_conflict", "escalate_to": "intent", "reasons": reasons}
    if ctx.required_personnel and ctx.required_personnel > ctx.force_cap:
        reasons.append(f"约束不可满足: 需求兵力{ctx.required_personnel} > 兵力上限{ctx.force_cap}")
        return {"failure_type": "constraint_conflict", "escalate_to": "intent", "reasons": reasons}

    # ② 方案违规（含汇总后的资源超限）
    for p in plans:
        v = run_hard_rules(p, ctx)
        reasons.extend(v.violations)
    for c in conflicts:
        reasons.append(f"硬规则违规: {c['resource']} 合计{c['total_ask']} > 容量{c['cap']}")
    if reasons:
        return {"failure_type": "plan_violation", "escalate_to": "commander", "reasons": reasons}

    # ③ 产物残缺
    missing = []
    for p in plans:
        for sec in required_sections:
            if sec not in p.get("sections", []):
                missing.append(f"{p.get('domain')}缺「{sec}」")
    if missing:
        return {"failure_type": "artifact_incomplete", "escalate_to": "arbitrate",
                "reasons": ["产物缺章节:" + ",".join(missing)]}

    return {"failure_type": None, "escalate_to": None, "reasons": []}


# ────────────────────────────────────────────────────────────
# 6. 自测
# ────────────────────────────────────────────────────────────
def _ctx(**kw) -> RuleContext:
    base = dict(force_cap=30000, deadline=3, no_go_zones=["禁区A"],
                roe_allowed=["常规", "有限"], resource_cap={"transport": 50, "fires": 15},
                force_pool={"army": {"A-001": {"人员": 4200}}, "navy": {"N-001": {"人员": 1800}}},
                required_personnel=0)
    base.update(kw)
    return RuleContext(**base)


def _plan(**kw) -> dict:
    base = dict(domain="army",
                units=[{"unit_id": "A-001", "personnel": 4200, "source": "sql:q12"}],
                asks={"transport": 30, "fires": 10},
                ask_sources={"transport": "sql:q07", "fires": "sql:q09"},
                timeline=[{"phase": "装载", "start": "D+0", "end": "D+2"}],
                areas=["XX海域"], fire_level="常规",
                sections=["时序表", "风险", "资源来源"])
    base.update(kw)
    return base


if __name__ == "__main__":
    results = []

    # R1 正常方案
    v = run_hard_rules(_plan(), _ctx())
    results.append(("R1 正常方案通过硬规则", v.ok, v.violations))

    # R2 超兵力上限
    v = run_hard_rules(_plan(units=[{"unit_id": "A-001", "personnel": 35000, "source": "sql:q1"}]),
                       _ctx(force_cap=30000))
    results.append(("R2 超兵力上限被拦截", not v.ok, v.violations))

    # R3 幻觉编成
    v = run_hard_rules(_plan(units=[{"unit_id": "A-999", "personnel": 100, "source": "sql:q1"}]), _ctx())
    results.append(("R3 幻觉单位被拦截", not v.ok, v.violations))

    # R4 数字无来源
    v = run_hard_rules(_plan(units=[{"unit_id": "A-001", "personnel": 4200}]), _ctx())
    results.append(("R4 无源数字被拦截", not v.ok, v.violations))

    # R5 禁区 + ROE
    v = run_hard_rules(_plan(areas=["禁区A"], fire_level="核"), _ctx())
    results.append(("R5 禁区+ROE 违规被拦截", len(v.violations) == 2, v.violations))

    # R6 冲突检测
    cf = detect_conflicts([{"transport": 30, "fires": 10}, {"transport": 25, "fires": 8}],
                          {"transport": 50, "fires": 15})
    # 注：transport 30+25=55>50 与 fires 10+8=18>15 **同时**冲突，故期望 2 项
    results.append(("R6 冲突检测: transport 55>50 且 fires 18>15",
                    len(cf) == 2 and cf[0]["excess"] == 5 and cf[1]["excess"] == 3, cf))

    # R7 收敛：第1轮有冲突 → continue
    h = [{"round": 1, "asks": {"army": {"transport": 30}, "navy": {"transport": 25}}, "objections": ["抢运力"]}]
    r = check_convergence(h, detect_conflicts([{"transport": 30}, {"transport": 25}], {"transport": 50}))
    results.append(("R7 第1轮: 继续协商", r["status"] == "continue", r["reason"]))

    # R8 收敛：第2轮无冲突 → converged
    h.append({"round": 2, "asks": {"army": {"transport": 25}, "navy": {"transport": 25}}, "objections": []})
    r = check_convergence(h, detect_conflicts([{"transport": 25}, {"transport": 25}], {"transport": 50}))
    results.append(("R8 第2轮: 无冲突收敛", r["status"] == "converged", r["reason"]))

    # R9 震荡：第2轮把让步收回
    h2 = [{"round": 1, "asks": {"army": {"transport": 30}, "navy": {"transport": 25}}, "objections": ["抢运力"]},
          {"round": 2, "asks": {"army": {"transport": 32}, "navy": {"transport": 20}}, "objections": ["仍不够"]}]
    r = check_convergence(h2, detect_conflicts([{"transport": 32}, {"transport": 20}], {"transport": 50}))
    results.append(("R9 震荡检出并强制裁决", r["status"] == "oscillating", r["reason"]))

    # R10 轮次封顶
    h3 = [{"round": i, "asks": {"army": {"transport": 30 - i}}, "objections": [f"o{i}"]} for i in (1, 2, 3)]
    r = check_convergence(h3, [{"resource": "transport"}])
    results.append(("R10 轮次封顶强制裁决", r["status"] == "cap_reached", r["reason"]))

    # R11 边际停滞
    h4 = [{"round": 1, "asks": {"army": {"transport": 30}, "navy": {"transport": 25}}, "objections": ["a"]},
          {"round": 2, "asks": {"army": {"transport": 30}, "navy": {"transport": 25}}, "objections": ["b"]}]
    r = check_convergence(h4, [{"resource": "transport"}])
    results.append(("R11 边际停滞检出", r["status"] == "stagnated", r["reason"]))

    # R12 归因：3天投送8万人 + 10架次 → constraint_conflict
    a = attribute_failure([_plan()], [{"transport": 30}],
                          _ctx(required_personnel=80000, deadline=3,
                               resource_cap={"transport": 10, "fires": 15}), [])
    results.append(("R12 约束矛盾→回 intent", a["failure_type"] == "constraint_conflict"
                    and a["escalate_to"] == "intent", a["reasons"]))

    # R13 归因：方案违规 → commander
    a = attribute_failure([_plan(units=[{"unit_id": "A-001", "personnel": 99999, "source": "sql:q1"}])],
                          [{"transport": 30}], _ctx(), [])
    results.append(("R13 方案违规→回 commander", a["failure_type"] == "plan_violation"
                    and a["escalate_to"] == "commander", a["reasons"]))

    # R14 归因：产物残缺 → arbitrate
    a = attribute_failure([_plan(sections=["时序表"])], [{"transport": 30}], _ctx(), [])
    results.append(("R14 产物残缺→回 arbitrate 局部修复", a["failure_type"] == "artifact_incomplete"
                    and a["escalate_to"] == "arbitrate", a["reasons"]))

    # ── R15~R17：裁决结果是「按军种拆分的 dict」时的归一化（真实踩坑区）──
    # 模型把容量写进 cap 键：不能拿容量当申索放行，必须看出真实拆分 60
    v = ask_total({"cap": 50, "陆军": 60})
    results.append(("R15 dict: cap 键不被当申索（拆分优先）", v == 60, f"得到 {v}"))

    # 规范输出：三军拆分 + 合计，两者自洽 → 50
    v = ask_total({"陆军": 18, "海军": 4, "空军": 28, "合计": 50})
    results.append(("R16 dict: 排除合计键后求和", v == 50, f"得到 {v}"))

    # 兵力：预备队不入列，不计入 HR-01 的「投入」
    v = ask_total({"陆军": 15900, "海军": 8600, "空军": 5400, "预备队": 6600})
    results.append(("R17 dict: 预备队不计入投入兵力", v == 29900, f"得到 {v}"))

    print("========== 硬规则 / 冲突 / 收敛 / 归因 自测 ==========")
    for name, ok, detail in results:
        print(f"  {name}: {'PASS' if ok else 'FAIL'}   {detail if not ok or '违规' in name or '拦截' in name else ''}")
    print(f"\n总体: {'ALL PASS' if all(o for _, o, _ in results) else 'HAS FAILURES'}  ({len(results)} 项)")
    sys.exit(0 if all(o for _, o, _ in results) else 1)
