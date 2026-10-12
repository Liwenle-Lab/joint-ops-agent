# -*- coding: utf-8 -*-
"""
二期验收：协商子循环

判据（出自《项目设计文档简易版》第二期）：三军争资源能自动检出并裁决。
拆成四件可实证的事：
  N1 协商能真收敛（不是靠轮次封顶蒙过去）
  N2 震荡能被拦截（收回让步 → 强制裁决）
  N3 轮次封顶能兜底（谈不拢不至于死循环）
  N4 黑板可追溯（事后能翻出第 N 轮谁让了什么）

N1 由 fc_nego4.txt 的真模型实测佐证（一轮谈到 49/15/4/11，全进容量）；
N2~N4 在这里用构造场景做确定性验证（0 API，秒级可复跑）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.graph.graph import (check_negotiation, route_after_check,      # noqa: E402
                             _parties_of_conflict)
from src.tools.blackboard import Blackboard                             # noqa: E402

CAP = {"transport": 50, "fires": 15, "satellite": 4, "sealift": 12}


def _tmp_db() -> Path:
    return Path(tempfile.mkdtemp()) / "nego.db"


def _mk_bb(db: Path) -> Blackboard:
    # ★ 关键：check_negotiation 内部会自己 new 一个 Blackboard（只带 session_id，不带路径），
    #   默认连项目库。若不把模块级 DB_PATH 也指向临时库，它会读到空黑板而误判 converged。
    import src.tools.blackboard as B
    B.DB_PATH = db
    bb = Blackboard(db_path=db)
    con = bb._con()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS negotiation_round (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            round INTEGER NOT NULL, speaker TEXT NOT NULL, claim TEXT,
            objection TEXT, resource_ask TEXT, concession TEXT,
            created_at TEXT NOT NULL);""")
    con.commit()
    con.close()
    return bb


def _state(bb: Blackboard, asks: list, conflicts: list) -> dict:
    return {"bb_session": bb.session_id, "current_asks": asks,
            "conflicts": conflicts, "constraints": {"resource_cap": CAP},
            "staff_reports": []}


results = []

# ── N2 震荡：第 2 轮陆军把上一轮的让步收回去 ──────────────
db = _tmp_db()
bb = _mk_bb(db)
bb.append(1, "陆军", claim="要30架次", resource_ask={"transport": 30}, concession="先让5")
bb.append(1, "海军", resource_ask={"transport": 25}, concession="让5")
bb.append(2, "陆军", claim="改主意了", resource_ask={"transport": 32})   # 不降反增
bb.append(2, "海军", resource_ask={"transport": 20})

out = check_negotiation(_state(bb, [{"transport": 32}, {"transport": 20}],
                               [{"resource": "transport", "total_ask": 52,
                                 "cap": 50, "excess": 2}]))
results.append(("N2 震荡被拦截（让步被收回）",
                out["negotiation_status"] == "oscillating",
                out["negotiation_status"]))
results.append(("N2 震荡后强制裁决（不再继续谈）",
                route_after_check(out) == "arbitrate", route_after_check(out)))

# ── N1 收敛：第 2 轮谈到容量内，无冲突 ───────────────────
db2 = _tmp_db()
bb2 = _mk_bb(db2)
bb2.append(1, "army", resource_ask={"transport": 30}, concession="让5")
bb2.append(1, "navy", resource_ask={"transport": 25}, concession="让5")
bb2.append(2, "army", resource_ask={"transport": 25}, concession="再让5")
bb2.append(2, "navy", resource_ask={"transport": 24}, concession="再让1")

out2 = check_negotiation(_state(bb2, [{"transport": 25}, {"transport": 24}], []))
results.append(("N1 谈拢后判收敛", out2["negotiation_status"] == "converged",
                out2["negotiation_status"]))
results.append(("N1 收敛后不再空转（交仲裁出终案）",
                route_after_check(out2) == "arbitrate", route_after_check(out2)))

# ── N3 轮次封顶：连谈 3 轮仍超 → 兜底强制裁决 ────────────
db3 = _tmp_db()
bb3 = _mk_bb(db3)
for r in (1, 2, 3):
    for name, v in (("army", 30), ("navy", 25)):      # 每轮都让一点点，但始终谈不拢
        bb3.append(r, name, resource_ask={"transport": v - r}, concession=f"R{r}让1")
out3 = check_negotiation(_state(bb3, [{"transport": 27}, {"transport": 22}],
                                [{"resource": "transport", "total_ask": 49,
                                  "cap": 50, "excess": -1}]))
results.append(("N3 轮次封顶兜底（不会死循环）",
                out3["negotiation_status"] == "cap_reached",
                out3["negotiation_status"]))

# ── N4 黑板可追溯：能按轮次翻出谁主张了什么、让了什么 ────
rows = bb2.fetch()
r2 = [r for r in rows if r["round"] == 2]
results.append(("N4 黑板按轮可翻", len(rows) == 4 and len(r2) == 2, f"{len(rows)}条"))
results.append(("N4 让步有记录（可回答\"为什么这么定\"）",
                all(r["concession"] for r in r2), [r["concession"] for r in r2]))
results.append(("N4 军种名归一（陆军→army）",
                {r["speaker"] for r in rows} == {"army", "navy"},
                {r["speaker"] for r in rows}))

# ── N5 参与者裁剪：不拉没参与争抢的军种陪跑 ─────────────
reports = [
    {"domain": "陆军", "asks": {"transport": 30, "fires": 6}},
    {"domain": "海军", "asks": {"transport": 25, "fires": 5}},
    {"domain": "空军", "asks": {"satellite": 3}},          # 只在卫星上有申索
]
parties = _parties_of_conflict(reports, [{"resource": "transport", "total_ask": 55}])
results.append(("N5 只派卷入冲突的军种（空军不陪跑）",
                parties == ["陆军", "海军"], parties))
parties_all = _parties_of_conflict(reports, [{"resource": "satellite", "total_ask": 6}])
results.append(("N5 换一个争议资源，参与者随之变化",
                parties_all == ["空军"], parties_all))

print("========== 二期验收：协商子循环 ==========")
ok = True
for name, passed, detail in results:
    print(f"  {name}: {'PASS' if passed else 'FAIL'}   {'' if passed else detail}")
    ok = ok and passed
print(f"\n总体: {'ALL PASS' if ok else 'HAS FAILURES'}  ({len(results)} 项)")
raise SystemExit(0 if ok else 1)
