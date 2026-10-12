# -*- coding: utf-8 -*-
"""
force_query.py —— 参谋的兵力池查询工具

三条纪律：
  1. 参谋只能通过本模块拿数字，禁止心算、禁止凭记忆报数（HR-07）
  2. 每次查询返回 query_id，方案里必须写 source: "sql:<query_id>"，
     终验时按 query_id 回查，对不上的数字判违规
  3. 本模块 0 API —— 纯 SQL，判定与取数都不经过模型

对外同时提供 OpenAI function-calling 的 tools schema（供真模型直接调工具）。
"""
from __future__ import annotations
import json
import sqlite3
import sys
import threading
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = ROOT / "db" / "joint_ops.db"
QUERY_LOG = ROOT / "trace" / "queries.jsonl"

SERVICE_ALIAS = {
    "陆军": "陆军", "陆": "陆军", "army": "陆军", "陆上": "陆军",
    "海军": "海军", "海": "海军", "navy": "海军", "海上": "海军",
    "空军": "空军", "空": "空军", "air": "空军", "airforce": "空军", "航空": "空军",
}


def norm_service(s: str) -> Optional[str]:
    if not s:
        return None
    s = str(s).strip()
    if s in SERVICE_ALIAS:
        return SERVICE_ALIAS[s]
    for k, v in SERVICE_ALIAS.items():
        if k in s:
            return v
    return None


@dataclass
class QueryResult:
    """一次 SQL 查询的凭证。rows 里的数字是唯一权威来源。"""
    query_id: str
    sql: str
    columns: List[str]
    rows: List[Dict[str, Any]]
    note: str = ""

    def source(self) -> str:
        return f"sql:{self.query_id}"

    def to_llm_view(self) -> str:
        """给模型看的紧凑文本（仍带 query_id，提示它必须引用）"""
        head = f"[query_id={self.query_id}] {self.note}"
        body = "\n".join(
            " | ".join(f"{k}={v}" for k, v in row.items()) for row in self.rows
        )
        return f"{head}\n{body}" if body else head


class ForcePool:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"兵力库不存在：{self.db_path}，请先运行 python db/build_db.py")
        self._lock = threading.Lock()
        self._registry: Dict[str, QueryResult] = {}
        QUERY_LOG.parent.mkdir(parents=True, exist_ok=True)

    # ── 底层：所有查询的唯一出口 ──────────────────────────
    def query(self, sql: str, params: tuple = (), note: str = "") -> QueryResult:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            cur = con.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
            columns = list(rows[0].keys()) if rows else []
        finally:
            con.close()

        qid = "q_" + uuid.uuid4().hex[:8]
        res = QueryResult(query_id=qid, sql=" ".join(sql.split()),
                          columns=columns, rows=rows, note=note)
        with self._lock:
            self._registry[qid] = res
            with QUERY_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(res), ensure_ascii=False) + "\n")
        return res

    def get(self, query_id: str) -> Optional[QueryResult]:
        """按 query_id 回查（终验用）"""
        return self._registry.get(query_id)

    # ── 面向参谋的高层查询 ────────────────────────────────
    def service_total(self, service: str) -> QueryResult:
        sv = norm_service(service)
        if not sv:
            raise ValueError(f"未知军种：{service}")
        return self.query(
            "SELECT service, unit_count, personnel_total, equipment_total "
            "FROM v_service_total WHERE service = ?", (sv,),
            note=f"{sv}兵力与装备合计")

    def equipment_by_model(self, service: str) -> QueryResult:
        sv = norm_service(service)
        if not sv:
            raise ValueError(f"未知军种：{service}")
        return self.query(
            "SELECT model, etype, total FROM v_model_total "
            "WHERE service = ? ORDER BY total DESC", (sv,),
            note=f"{sv}各型号装备数量")

    def units_of(self, service: str) -> QueryResult:
        sv = norm_service(service)
        if not sv:
            raise ValueError(f"未知军种：{service}")
        return self.query(
            "SELECT unit_id, unit_name, branch, personnel, base, readiness "
            "FROM units WHERE service = ? ORDER BY personnel DESC", (sv,),
            note=f"{sv}所属单位清单")

    def unit_detail(self, unit_id: str) -> QueryResult:
        return self.query(
            "SELECT u.unit_id, u.unit_name, u.service, u.branch, u.personnel,"
            " u.base, u.readiness, e.model, e.etype, e.count "
            "FROM units u LEFT JOIN equipment e ON e.unit_id = u.unit_id "
            "WHERE u.unit_id = ?", (unit_id,),
            note=f"单位 {unit_id} 编成明细")

    def resource_capacity(self, res_key: Optional[str] = None) -> QueryResult:
        """共享资源容量（参谋申索前必查，使 asks 也有 sql 来源）"""
        if res_key:
            return self.query(
                "SELECT res_key, res_name, capacity FROM resources WHERE res_key = ?",
                (res_key,), note=f"共享资源 {res_key} 容量")
        return self.query("SELECT res_key, res_name, capacity FROM resources",
                          note="全部共享资源容量")

    def all_services(self) -> QueryResult:
        return self.query(
            "SELECT service, unit_count, personnel_total, equipment_total "
            "FROM v_service_total", note="三军种总览")


# ── OpenAI function-calling 工具表（真模型直接用） ─────────────
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "query_service_total",
            "description": "查询某军种的人员总数、装备总件数、单位数。数字必须来自此工具。",
            "parameters": {
                "type": "object",
                "properties": {"service": {"type": "string", "description": "陆军/海军/空军"}},
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_equipment_by_model",
            "description": "查询某军种各型号装备的数量明细。",
            "parameters": {
                "type": "object",
                "properties": {"service": {"type": "string", "description": "陆军/海军/空军"}},
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_units_of",
            "description": "列出某军种下辖单位（番号、兵种、人数、驻地、战备状态）。",
            "parameters": {
                "type": "object",
                "properties": {"service": {"type": "string", "description": "陆军/海军/空军"}},
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_unit_detail",
            "description": "查询单个单位的编成与装备明细。",
            "parameters": {
                "type": "object",
                "properties": {"unit_id": {"type": "string", "description": "如 A-001"}},
                "required": ["unit_id"],
            },
        },
    },
]

TOOLS_SCHEMA.append({
    "type": "function",
    "function": {
        "name": "query_resource_capacity",
        "description": "查询共享资源（运输机架次/火力支援/卫星窗口/海运批次）的容量上限。申索前必查。",
        "parameters": {
            "type": "object",
            "properties": {"res_key": {"type": "string",
                                        "description": "transport|fires|satellite|sealift，留空查全部"}},
            "required": [],
        },
    },
})

TOOL_DISPATCH = {
    "query_resource_capacity": lambda pool, a: pool.resource_capacity(a.get("res_key")),
    "query_service_total": lambda pool, a: pool.service_total(a["service"]),
    "query_equipment_by_model": lambda pool, a: pool.equipment_by_model(a["service"]),
    "query_units_of": lambda pool, a: pool.units_of(a["service"]),
    "query_unit_detail": lambda pool, a: pool.unit_detail(a["unit_id"]),
}


def call_tool(pool: ForcePool, name: str, arguments: Dict[str, Any]) -> QueryResult:
    if name not in TOOL_DISPATCH:
        raise KeyError(f"未注册的工具：{name}")
    return TOOL_DISPATCH[name](pool, arguments)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    pool = ForcePool()
    r = pool.service_total("陆军")
    print(r.to_llm_view())
    print("source =", r.source())
    r2 = pool.equipment_by_model("陆军")
    print(r2.to_llm_view())
