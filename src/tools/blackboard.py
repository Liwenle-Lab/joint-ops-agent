# -*- coding: utf-8 -*-
"""
blackboard.py —— 协商黑板书（二期）

设计出处：《设计总纲》§5.5 / 《项目设计文档》§协商黑板。
存在意义只有一个：事后能回答"为什么这么定"。
——第 2 轮陆军提了什么反建议、空军让了什么，必须能翻出来，不能只留下一个结果。

落盘策略：SQLite 表 negotiation_round，只增不删。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = ROOT / "db" / "joint_ops.db"

# 军种名统一到英文 key，避免模型一会儿写"陆军"一会儿写"army"导致查不回来
SERVICE_ALIAS = {
    "army": "army", "陆军": "army",
    "navy": "navy", "海军": "navy",
    "airforce": "airforce", "空军": "airforce", "air": "airforce",
}


def norm_service(name: str) -> str:
    return SERVICE_ALIAS.get(str(name).strip().lower(), str(name).strip())


def new_session_id() -> str:
    return "s_" + uuid.uuid4().hex[:12]


class Blackboard:
    """一次任务一份黑板。所有协商发言都往这里追加。"""

    def __init__(self, session_id: Optional[str] = None, db_path: Optional[Path] = None):
        self.session_id = session_id or new_session_id()
        self.db_path = Path(db_path) if db_path else DB_PATH

    def _con(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.db_path)

    def append(self, round_no: int, speaker: str, claim: str = "",
               objection: str = "", resource_ask: Any = None,
               concession: str = "") -> int:
        """追加一条发言。resource_ask 是 dict 就存 JSON 字符串。"""
        ask_txt = (resource_ask if isinstance(resource_ask, str)
                   else json.dumps(resource_ask or {}, ensure_ascii=False))
        with self._con() as con:
            cur = con.execute(
                "INSERT INTO negotiation_round"
                "(session_id,round,speaker,claim,objection,resource_ask,concession,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (self.session_id, int(round_no), norm_service(speaker),
                 str(claim or ""), str(objection or ""), ask_txt,
                 str(concession or ""), time.strftime("%Y-%m-%d %H:%M:%S")))
            return int(cur.lastrowid or 0)

    def fetch(self) -> List[Dict[str, Any]]:
        """本会话全部发言，按轮次、时间排序"""
        with self._con() as con:
            rows = con.execute(
                "SELECT round,speaker,claim,objection,resource_ask,concession,created_at"
                " FROM negotiation_round WHERE session_id=?"
                " ORDER BY round, id", (self.session_id,)).fetchall()
        out = []
        for rnd, spk, claim, obj, ask, conc, ts in rows:
            try:
                ask_v: Any = json.loads(ask) if ask else {}
            except (TypeError, ValueError):
                ask_v = ask
            out.append({"round": rnd, "speaker": spk, "claim": claim,
                        "objection": obj, "resource_ask": ask_v,
                        "concession": conc, "created_at": ts})
        return out

    def rounds(self) -> List[int]:
        return sorted({r["round"] for r in self.fetch()})

    def asks_by_round(self) -> Dict[int, Dict[str, Dict[str, Any]]]:
        """{round: {speaker: resource_ask}} —— 直接喂给 check_convergence 做 CV-5 震荡判定"""
        out: Dict[int, Dict[str, Dict[str, Any]]] = {}
        for r in self.fetch():
            ask = r["resource_ask"]
            if isinstance(ask, dict):
                out.setdefault(r["round"], {})[r["speaker"]] = ask
        return out

    def objections_by_round(self) -> Dict[int, List[str]]:
        out: Dict[int, List[str]] = {}
        for r in self.fetch():
            if r["objection"]:
                out.setdefault(r["round"], []).append(r["objection"])
        return out

    def to_llm_view(self, last_n_rounds: int = 2) -> str:
        """给参谋看的黑板摘要：只给最近 N 轮，避免 prompt 无限膨胀"""
        rows = self.fetch()
        if not rows:
            return "（黑板为空，本轮为首次协商）"
        maxr = max(r["round"] for r in rows)
        rows = [r for r in rows if r["round"] >= maxr - last_n_rounds + 1]
        lines = []
        for r in rows:
            lines.append(
                f"[R{r['round']}] {r['speaker']} 主张：{r['claim'] or '—'}"
                f" | 反对：{r['objection'] or '—'}"
                f" | 申索：{json.dumps(r['resource_ask'], ensure_ascii=False)}"
                f" | 让步：{r['concession'] or '—'}")
        return "\n".join(lines)


# ── 自测 ──────────────────────────────────────────────────
def _self_test() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    con = sqlite3.connect(tmp)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS negotiation_round (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            round INTEGER NOT NULL, speaker TEXT NOT NULL, claim TEXT,
            objection TEXT, resource_ask TEXT, concession TEXT, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_nego_session
            ON negotiation_round(session_id, round);""")
    con.commit()
    con.close()

    bb = Blackboard(db_path=tmp)
    bb.append(1, "陆军", claim="要30架次", objection="", resource_ask={"transport": 30})
    bb.append(1, "navy", claim="要20架次", objection="陆军要太多",
              resource_ask={"transport": 20}, concession="让5架次")
    bb.append(2, "army", claim="降到25", resource_ask={"transport": 25},
              concession="再让5架次")

    r1, r2 = bb.asks_by_round(), bb.objections_by_round()
    checks = [
        ("落盘 3 条", len(bb.fetch()) == 3),
        ("军种名归一（陆军→army）", {r["speaker"] for r in bb.fetch()} == {"army", "navy"}),
        ("按轮次聚合申索", r1[1]["army"]["transport"] == 30 and r1[2]["army"]["transport"] == 25),
        ("反对点按轮归集", r2.get(1) == ["陆军要太多"] and 2 not in r2),
        ("轮次列表", bb.rounds() == [1, 2]),
        ("LLM 视图非空", "army" in bb.to_llm_view()),
        ("会话隔离", Blackboard(db_path=tmp).fetch() == []),
    ]
    print("========== 协商黑板书 自测 ==========")
    ok = True
    for name, passed in checks:
        print(f"  {name}: {'PASS' if passed else 'FAIL'}")
        ok = ok and passed
    print(f"\n总体: {'ALL PASS' if ok else 'HAS FAILURES'}  ({len(checks)} 项)")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(_self_test())
