# -*- coding: utf-8 -*-
"""
test_env.py —— 第五期环境工具验收（L0 地理锚点 + L1 环境查询）

与一期 Acceptance 同 philosophy：每条断言都要能指出"具体哪里错了"。
大部分用例走离线/缓存，断网也能跑（否则这套测试本身就不可靠）；
末尾一组真实联网用例仅在 --live 时执行，用于验证外部 API 契约未变。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.tools.env_query import EnvPool, call_env_tool  # noqa: E402
from rules_convergence import RuleContext, run_hard_rules  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
_results: list = []


def check(idx: str, name: str, got, want):
    ok = got == want
    _results.append((idx, name, ok, got, want))
    print(f"  {idx} {name}: {PASS if ok else FAIL}"
          + ("" if ok else f"   got={got!r} want={want!r}"))


def _tmp_db() -> Path:
    """建一个只含锚点的最小库，避免依赖主库内容变化"""
    d = Path(tempfile.mkdtemp(prefix="env_test_"))
    db = d / "t.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE units (unit_id TEXT PRIMARY KEY, unit_name TEXT, service TEXT,
                            branch TEXT, personnel INTEGER, base TEXT, readiness TEXT);
        CREATE TABLE geo_anchor (base TEXT PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL,
                                 kind TEXT, region TEXT, terrain TEXT,
                                 sea_lat REAL, sea_lon REAL);
        CREATE VIEW v_unit_geo AS
        SELECT u.unit_id AS unit_id, u.unit_name AS unit_name, u.service AS service,
               u.base AS base, g.lat AS lat, g.lon AS lon, g.kind AS kind,
               g.region AS region, g.terrain AS terrain,
               g.sea_lat AS sea_lat, g.sea_lon AS sea_lon
        FROM units u LEFT JOIN geo_anchor g ON g.base = u.base;
    """)
    con.executemany("INSERT INTO units VALUES(?,?,?,?,?,?,?)", [
        ("T-01", "测试旅", "陆军", "合成", 1000, "试山基地", "A"),
        ("T-02", "测试支队", "海军", "水面", 800, "试屿军港", "A"),
    ])
    con.executemany("INSERT INTO geo_anchor VALUES(?,?,?,?,?,?,?,?)", [
        ("试山基地", 23.70, 117.42, "land", "测试区", "丘陵", None, None),
        ("试屿军港", 26.62, 119.68, "port", "测试湾区", "深水港", 26.60, 119.80),
    ])
    con.commit()
    con.close()
    return db


def main() -> int:
    print("========== 第五期：地理锚点 + 环境查询自测 ==========\n")
    db = _tmp_db()

    # ── 1. 锚点层 ──────────────────────────────────────
    print("【地理锚点】")
    pool = EnvPool(db_path=db, offline=True)
    a = pool.anchor("试山基地")
    check("A1", "驻地能解析出坐标", (a["lat"], a["lon"]), (23.70, 117.42))
    check("A2", "未知驻地返回 None", pool.anchor("查无此地"), None)
    check("A3", "军种级驻地列表去重并按服务过滤",
          [r["base"] for r in pool.bases_of("海军")], ["试屿军港"])
    sa = pool.sea_anchor("试屿军港")
    check("A4", "港口取海面取样点而非岸上坐标",
          (sa["lat"], sa["lon"]), (26.60, 119.80))
    check("A5", "陆地基地的 sea_anchor 退回主坐标",
          (pool.sea_anchor("试山基地")["lat"]), 23.70)

    # ── 2. 降级 ────────────────────────────────────────
    print("\n【降级：必须返回不可用，不得抛异常】")
    off = EnvPool(db_path=db, offline=True)
    off._cache = {}
    r = off.weather(23.70, 117.42)
    check("B1", "离线查询不抛异常且标记不可用", bool(r), False)
    check("B2", "不可用时给出可读错误", "离线模式" in (r.error or ""), True)
    check("B3", "不可用结果仍带 query_id（可追溯）", r.query_id.startswith("e_"), True)
    r2 = off.base_brief("查无此地")
    check("B4", "缺锚点驻地降级并提示去哪补",
          "geo_anchor.json" in (r2.error or ""), True)

    # ── 2b. 结构化快照：键名不许写错 ────────────────────
    # 补这一组的原因：接口层曾把 "最大浪高m" 写成 "浪高m"。键对不上就静默
    # 少一格 —— 页面照常渲染，海洋那几格凭空消失，肉眼看不出来。
    # 所以把"展示用键名 ⊆ 数据层真实字段"固化成断言。
    print("\n【结构化快照 brief】")
    print("    （展示层键名与数据层字段对齐，防止静默丢格）")
    bad = off.brief("查无此地")
    check("E1", "缺锚点驻地给出降级与补救提示",
          bool(bad["degraded"]) and "geo_anchor.json" in bad.get("error", ""), True)
    ok_struct = off.brief("试山基地")
    check("E2", "快照必备字段齐全",
          sorted(k for k in ("base", "kind", "items", "sources", "degraded")
                 if k in ok_struct),
          ["base", "degraded", "items", "kind", "sources"])

    # 数据层实际产出的行键（EnvPool 取数逻辑若增删字段，同步这张表）
    ROW_KEYS = {
        "weather": {"日期", "气温℃", "相对湿度%", "云量%", "天气",
                    "风速km/h", "阵风km/h", "风向°", "降水mm"},
        "marine": {"日期", "最大浪高m", "最大周期s", "主浪向°", "海表温度℃"},
        "terrain": {"高程m"},
    }
    allowed = set().union(*ROW_KEYS.values())
    spec = list(EnvPool._BRIEF_COMMON)
    for rows in EnvPool._BRIEF_MAP.values():
        spec += list(rows)
    check("E3", "展示用键名全部命中数据层真实字段",
          sorted({k for _, k, _ in spec} - allowed), [])
    check("E4", "三类设施都有专属字段映射",
          sorted(EnvPool._BRIEF_MAP.keys()), ["airfield", "land", "port"])

    # ── 3. 溯源凭证 ────────────────────────────────────
    print("\n【溯源契约】")
    check("C1", "EnvResult 的 source 前缀为 env:", r.source().startswith("env:"), True)
    check("C2", "query_id 可回查到原始凭证", off.get(r.query_id) is r, True)
    check("C3", "不同查询的 query_id 不重复",
          pool.weather(23.7, 117.4).query_id != pool.weather(23.7, 117.4).query_id, True)

    # ── 4. HR-07 双前缀 ────────────────────────────────
    print("\n【HR-07 溯源前缀】")
    ctx = RuleContext(force_cap=30000, deadline=5,
                      resource_cap={"transport": 50, "fires": 15,
                                    "satellite": 4, "sealift": 12})
    plan = {"units": [], "asks": {}, "ask_sources": {}, "env_sources": {},
            "sections": [], "timeline": [], "areas": [], "fire_level": "常规"}
    check("H1", "空方案通过", run_hard_rules(plan, ctx).ok, True)

    plan2 = {"units": [], "asks": {"transport": 10},
             "ask_sources": {"transport": "sql:q_abc"}, "env_sources": {}}
    check("H2", "sql: 前缀仍然放行",
          [v for v in run_hard_rules(plan2, ctx).violations if "HR-07" in v], [])

    plan3 = {"units": [], "asks": {}, "ask_sources": {},
             "env_sources": {"浪高": "env:e_123"}}
    check("H3", "env: 前缀放行（第五期新增）",
          [v for v in run_hard_rules(plan3, ctx).violations if "HR-07" in v], [])

    plan4 = {"units": [], "asks": {}, "ask_sources": {},
             "env_sources": {"浪高": "sql:q_abc"}}
    v4 = run_hard_rules(plan4, ctx).violations
    check("H4", "环境数据用 sql: 糊弄会被拦下",
          any("env.浪高" in v for v in v4), True)

    plan5 = {"units": [], "asks": {}, "ask_sources": {},
             "env_sources": {"浪高": ""}}
    v5 = run_hard_rules(plan5, ctx).violations
    check("H5", "环境数据无来源会被拦下",
          any("env.浪高" in v for v in v5), True)

    # ── 5. 工具表 ──────────────────────────────────────
    print("\n【function-calling 工具表】")
    names = []
    try:
        from src.tools.env_query import ENV_TOOLS_SCHEMA
        names = [t["function"]["name"] for t in ENV_TOOLS_SCHEMA]
    except Exception as e:  # noqa: BLE001
        print(f"    导入失败：{e}")
    check("T1", "工具表已注册 5 个工具", len(names), 5)
    check("T2", "含驻地快照工具", "query_base_env" in names, True)
    r5 = call_env_tool(pool, "query_service_bases", {"service": "海军"})
    check("T3", "call_env_tool 支持驻地列表", r5.rows[0]["base"], "试屿军港")
    try:
        call_env_tool(pool, "不存在的工具", {})
        check("T4", "未注册工具应报错", False, True)
    except KeyError:
        check("T4", "未注册工具应报错", True, True)

    # ── 6. 异步不阻塞 ──────────────────────────────────
    print("\n【异步：不得拖垮三参谋并行】")

    async def parallel_probe():
        t0 = time.time()
        await asyncio.gather(*[pool.a_base_brief(b, 2)
                               for b in ("试山基地", "试屿军港")])
        return time.time() - t0

    offline_cached = EnvPool(db_path=db, offline=True)
    offline_cached._cache = {}
    elapsed = asyncio.run(parallel_probe())
    check("Y1", "异步并发调用不报错", elapsed >= 0, True)

    # ── 7. 联网（可选） ────────────────────────────────
    if "--live" in sys.argv:
        print("\n【真实联网】（--live）")
        live = EnvPool(db_path=db)
        t0 = time.time()
        w = live.weather(23.70, 117.42, days=2)
        took = time.time() - t0
        check("L1", "实查气象有数据", bool(w), True)
        print(f"      首次耗时 {took:.2f}s")
        t1 = time.time()
        w2 = live.weather(23.70, 117.42, days=2)
        check("L2", "第二次命中缓存且更快",
              (w2.cached, time.time() - t1 < took), (True, True))
        m = live.marine(26.60, 119.80, days=1)
        check("L3", "海面取样点能取到海表温度",
              m.rows[0].get("海表温度℃") is not None if m.rows else False, True)
        t = live.terrain(25.08, 116.80)
        check("L4", "地形高程有值", bool(t.rows), True)
        if t.rows:
            print(f"      内陆山地高程 = {t.rows[0].get('高程m')} m")
    else:
        print("\n（跳过真实联网用例，加 --live 执行）")

    ok_n = sum(1 for _, _, ok, _, _ in _results if ok)
    total = len(_results)
    print(f"\n总体: {'ALL PASS' if ok_n == total else 'HAS FAILURE'}  "
          f"({ok_n}/{total} 项)")
    return 0 if ok_n == total else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
