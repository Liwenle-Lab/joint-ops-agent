# -*- coding: utf-8 -*-
"""
三期验收：增量重规划的影响面分析（0 API，确定性可复跑）

判据（项目书第三期）：改一个条件只重算受影响参谋。

为什么必须单独做确定性用例：真模型实测中三军往往都申索了同一资源
（运力减半 → 陆军16/海军10/空军24 全都受影响），此时"全量重算"就是正确答案，
看不出复用。复用机制是否正确，要用申索稀疏的场景来验。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rules_convergence import diff_constraints, impacted_domains   # noqa: E402

BASE = {"force_cap": 30000, "deadline_days": 5,
        "no_go_zones": ["A禁区", "B雷区"], "roe_allowed": ["常规", "有限"],
        "resource_cap": {"transport": 50, "fires": 15, "satellite": 4, "sealift": 12}}

# 申索故意做得稀疏：空军只在卫星上有申索，陆军/海军只在运力上
REPORTS = [
    {"domain": "陆军", "asks": {"transport": 30, "fires": 6},
     "data": {"areas": ["A禁区"], "fire_level": "常规"}},
    {"domain": "海军", "asks": {"transport": 25, "fires": 5},
     "data": {"areas": ["B雷区"], "fire_level": "常规"}},
    {"domain": "空军", "asks": {"satellite": 3},
     "data": {"areas": [], "fire_level": "有限"}},
]

results = []


def case(name, new, expect_impacted, expect_reused):
    ch = diff_constraints(BASE, new)
    imp, reuse = impacted_domains(REPORTS, ch, BASE, new)
    ok = set(imp) == set(expect_impacted) and set(reuse) == set(expect_reused)
    results.append((name, ok, f"变更={ch} 重算={imp} 复用={reuse}"))


# ── 1. 只改运力 → 只有申索运力的两军重算，空军复用 ──────────
new1 = {**BASE, "resource_cap": {**BASE["resource_cap"], "transport": 25}}
case("C1 运力减半→只重算陆军海军，空军复用",
     new1, ["陆军", "海军"], ["空军"])

# ── 2. 只改卫星 → 只有空军重算 ───────────────────────────
new2 = {**BASE, "resource_cap": {**BASE["resource_cap"], "satellite": 2}}
case("C2 卫星减少→只重算空军，陆海军复用",
     new2, ["空军"], ["陆军", "海军"])

# ── 3. 改兵力上限 → 全局，三军全重算 ──────────────────────
case("C3 兵力上限变化→全体重算（全局量）",
     {**BASE, "force_cap": 20000}, ["陆军", "海军", "空军"], [])

# ── 4. 改时限 → 全局 ────────────────────────────────────
case("C4 时限压缩→全体重算（时序贯穿三军）",
     {**BASE, "deadline_days": 3}, ["陆军", "海军", "空军"], [])

# ── 5. 改禁区 → 只影响方案涉及该区域的军种 ────────────────
case("C5 新增禁区C→无人涉及则无人重算（不误伤）",
     {**BASE, "no_go_zones": ["A禁区", "B雷区", "C新区"]}, [], ["陆军", "海军", "空军"])
case("C6 取消A禁区→只有陆军（其方案涉及A）重算",
     {**BASE, "no_go_zones": ["B雷区"]}, ["陆军"], ["海军", "空军"])

# ── 6. 改 ROE → 只影响火力等级已不合新规的军种 ────────────
case("C7 ROE只留常规→只有空军（现为有限）重算",
     {**BASE, "roe_allowed": ["常规"]}, ["空军"], ["陆军", "海军"])

# ── 7. 无变更 → 全部复用，一个都不重算 ────────────────────
ch0 = diff_constraints(BASE, dict(BASE))
imp0, reuse0 = impacted_domains(REPORTS, ch0, BASE, dict(BASE))
results.append(("C8 约束无变化→全部复用", imp0 == [] and len(reuse0) == 3,
                f"重算={imp0} 复用={reuse0}"))

# ── 9. diff 本身的正确性 ────────────────────────────────
results.append(("C9 diff 只报真正变了的项",
                diff_constraints(BASE, new1) == ["resource_cap.transport"],
                diff_constraints(BASE, new1)))
results.append(("C10 首轮（无旧约束）视为无变更",
                diff_constraints(None, BASE) == [], diff_constraints(None, BASE)))
results.append(("C11 多项变更一次报全",
                sorted(diff_constraints(BASE, {**BASE, "force_cap": 1,
                                               "resource_cap": {**BASE["resource_cap"],
                                                                "fires": 3}}))
                == ["force_cap", "resource_cap.fires"],
                diff_constraints(BASE, {**BASE, "force_cap": 1,
                                        "resource_cap": {**BASE["resource_cap"],
                                                         "fires": 3}})))

print("========== 三期验收：增量重规划影响面分析 ==========")
ok = True
for name, passed, detail in results:
    print(f"  {name}: {'PASS' if passed else 'FAIL'}   {'' if passed else detail}")
    ok = ok and passed
print(f"\n总体: {'ALL PASS' if ok else 'HAS FAILURES'}  ({len(results)} 项)")
raise SystemExit(0 if ok else 1)
