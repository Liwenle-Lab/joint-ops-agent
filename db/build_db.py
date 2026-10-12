# -*- coding: utf-8 -*-
"""
build_db.py —— 把 seed/*.json 灌进 SQLite

设计约束（对应《项目设计文档》§3）：
  · JSON 只是"权威种子"（相当于 3 个 Excel），运行时一律查 SQL，不许模型心算
  · 粒度：单位级（旅/支队/团 + 装备型号数量 + 驻地）
  · 表：units（单位）+ equipment（装备）+ 两个聚合视图

用法：python db/build_db.py  →  生成 db/joint_ops.db
"""
from __future__ import annotations
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "seed"
DB_PATH = ROOT / "db" / "joint_ops.db"

sys.path.insert(0, str(ROOT))
# 兵力改动日志的 DDL 只写一份，放在 force_admin 里（谁写库谁定义表结构），
# 这里 import 过来一起建。在本文件抄一份，迟早会和那边不一致。
from src.tools.force_admin import FORCE_LOG_SCHEMA          # noqa: E402
from src.tools.feedback import FEEDBACK_SCHEMA              # noqa: E402
from src.tools.run_log import RUN_LOG_SCHEMA                # noqa: E402

SCHEMA = """
DROP TABLE IF EXISTS units;
DROP TABLE IF EXISTS equipment;
DROP TABLE IF EXISTS resources;      -- 漏了这行会导致第二次建库报 "table resources already exists"
DROP TABLE IF EXISTS geo_anchor;     -- 同上，凡主 SCHEMA 里的表都得配一行 DROP
DROP VIEW  IF EXISTS v_service_total;
DROP VIEW  IF EXISTS v_model_total;
DROP VIEW  IF EXISTS v_unit_geo;

CREATE TABLE units (
    unit_id    TEXT PRIMARY KEY,
    unit_name  TEXT NOT NULL,
    service    TEXT NOT NULL,
    branch     TEXT,
    personnel  INTEGER NOT NULL,
    base       TEXT,
    readiness  TEXT
);

CREATE TABLE equipment (
    eq_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id   TEXT NOT NULL REFERENCES units(unit_id),
    model     TEXT NOT NULL,
    etype     TEXT,
    count     INTEGER NOT NULL
);

-- 共享资源容量：参谋申索运输/火力/卫星/海运时，须先查此表（HR-07 要求申索也有 sql 来源）
CREATE TABLE resources (
    res_key   TEXT PRIMARY KEY,
    res_name  TEXT,
    capacity  INTEGER NOT NULL
);

CREATE INDEX idx_units_service    ON units(service);
CREATE INDEX idx_equipment_unit   ON equipment(unit_id);
CREATE INDEX idx_equipment_model  ON equipment(model);

-- 驻地地理锚点：虚构地名 → 真实经纬度
-- 为什么必须有：气象/地形/海洋 API 一律按经纬度查询，地名查不了。
-- 而兵力池驻地是虚构地名，没有坐标就什么都查不到。本表是那座桥。
--  anchor 变化时随兵力池一起重建（它是 seed/geo_anchor.json 的镜像，不是历史数据）
CREATE TABLE geo_anchor (
    base      TEXT PRIMARY KEY,      -- 与 units.base 对齐
    lat       REAL NOT NULL,         -- WGS84 纬度
    lon       REAL NOT NULL,         -- WGS84 经度
    kind      TEXT,                  -- land | port | airfield，决定参谋默认查哪个端点
    region    TEXT,                  -- 对应的公开地理位置（便于人工核对）
    terrain   TEXT,                  -- 地形类型备注
    sea_lat   REAL,                  -- 海面取样点（可选）。港口若在深入内陆的海湾，
    sea_lon   REAL                   -- 岸上坐标去查海洋数据会落到陆地网格，故单独给海上点
);
-- 注意：人数与装备必须分别聚合再拼接。若写成 units LEFT JOIN equipment 后
-- SUM(personnel)，人数会按该单位的装备行数被重复累加（陆军会算出 42700 而非 15900）。
CREATE VIEW v_service_total AS
WITH p AS (
    SELECT service,
           COUNT(*)        AS unit_count,
           SUM(personnel)  AS personnel_total
    FROM units GROUP BY service
),
q AS (
    SELECT u.service        AS service,
           SUM(e.count)     AS equipment_total
    FROM equipment e JOIN units u ON u.unit_id = e.unit_id
    GROUP BY u.service
)
SELECT p.service                            AS service,
       p.unit_count                         AS unit_count,
       p.personnel_total                    AS personnel_total,
       COALESCE(q.equipment_total, 0)       AS equipment_total
FROM p LEFT JOIN q ON p.service = q.service;

-- 型号聚合视图：某型号在各军种的总数
CREATE VIEW v_model_total AS
SELECT e.model        AS model,
       e.etype        AS etype,
       u.service      AS service,
       SUM(e.count)   AS total
FROM equipment e
JOIN units u ON u.unit_id = e.unit_id
GROUP BY e.model, e.etype, u.service;

-- 单位带坐标视图：LEFT JOIN 保证没配锚点的单位也查得出来，
-- 只是 lat/lon 为 NULL —— 由查询侧显式提示"该驻地缺锚点"，而不是让它悄悄消失。
CREATE VIEW v_unit_geo AS
SELECT u.unit_id    AS unit_id,
       u.unit_name  AS unit_name,
       u.service    AS service,
       u.base       AS base,
       g.lat        AS lat,
       g.lon        AS lon,
       g.kind       AS kind,
       g.region     AS region,
       g.terrain    AS terrain
FROM units u LEFT JOIN geo_anchor g ON g.base = u.base;
"""

# ── 二期：协商黑板书 ────────────────────────────────────────
# ★ 刻意不放进上面的 SCHEMA：那段每次 build() 都会 DROP 重建（兵力池要能重置），
#   而协商记录是「事后追溯」用的证据——用户问"为什么这么定"，要能翻出第 2 轮谁让了什么。
#   建库重置顺手把历史证据清掉是灾难，故独立建表且只增不删。
NEGOTIATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS negotiation_round (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,      -- 一次任务一个，否则多次运行的记录会混在一起
    round        INTEGER NOT NULL,
    speaker      TEXT NOT NULL,      -- army / navy / airforce（或中文军种名）
    claim        TEXT,               -- 本方主张
    objection    TEXT,               -- 对他人主张的反对点（CV-4 判据依据）
    resource_ask TEXT,               -- 本轮索取（JSON，CV-5 震荡判据依据）
    concession   TEXT,               -- 本轮做出的让步
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nego_session ON negotiation_round(session_id, round);
"""

FILES = {"army": "army.json", "navy": "navy.json", "airforce": "airforce.json"}


def load_seed() -> tuple[list, list]:
    units, equips = [], []
    for key, fname in FILES.items():
        path = SEED_DIR / fname
        if not path.exists():
            print(f"[warn] 缺少种子文件：{path}")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for u in data:
            units.append((
                u["id"], u["单位"], u["军种"], u.get("兵种", ""),
                int(u["人员"]), u.get("驻地", ""), u.get("战备状态", ""),
            ))
            for e in u.get("主要装备", []):
                equips.append((u["id"], e["型号"], e.get("类型", ""), int(e["数量"])))
    return units, equips


RES_NAME = {
    "transport": "运输机架次", "fires": "火力支援单元",
    "satellite": "卫星过顶窗口", "sealift": "海运批次",
}


def load_resources() -> list:
    """共享资源容量来自 config/constraints.json（单一真源，避免两处不一致）"""
    cfg_path = ROOT / "config" / "constraints.json"
    if not cfg_path.exists():
        return []
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    return [(k, RES_NAME.get(k, k), int(v))
            for k, v in cfg.get("resource_cap", {}).items()]


def load_geo_anchor() -> list:
    """驻地坐标来自 seed/geo_anchor.json —— 用户只需改这一个文件"""
    path = SEED_DIR / "geo_anchor.json"
    if not path.exists():
        print(f"[warn] 缺少地理锚点文件：{path}（环境查询将不可用）")
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for base, a in (data.get("anchors") or {}).items():
        lat, lon = a.get("lat"), a.get("lon")
        if lat is None or lon is None:
            print(f"[warn] 锚点 {base} 缺 lat/lon，已跳过")
            continue
        # 粗校验：坐标必须在中国境内大致范围，写反或写错洲会被挡下来
        if not (3.0 <= float(lat) <= 54.0 and 73.0 <= float(lon) <= 136.0):
            print(f"[warn] 锚点 {base} 坐标 ({lat},{lon}) 越界，疑似写反，已跳过")
            continue
        rows.append((base, float(lat), float(lon), a.get("kind", "land"),
                     a.get("region", ""), a.get("terrain", ""),
                     _f_or_none(a.get("sea_lat")), _f_or_none(a.get("sea_lon"))))
    return rows


def _f_or_none(v) -> Optional[float]:
    """sea_lat/sea_lon 允许留空。写不进去要比写成 0 好得多 —— 
    0,0 是几内亚湾，会让海洋查询指向完全错误的位置。"""
    return float(v) if v is not None and v != "" else None


def build() -> None:
    units, equips = load_seed()
    resources = load_resources()
    anchors = load_geo_anchor()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.executescript(NEGOTIATION_SCHEMA)   # 只增：重建兵力池不会抹掉协商历史
    con.executescript(FORCE_LOG_SCHEMA)     # 同上：兵力改动日志也只增不删
    con.executescript(FEEDBACK_SCHEMA)      # 同上：用户一条条点出来的评价更不能重建一次就没
    con.executescript(RUN_LOG_SCHEMA)       # 同上：失败留痕是"当时发生了什么"的证据，不能重建就丢
    con.executemany(
        "INSERT INTO units(unit_id,unit_name,service,branch,personnel,base,readiness)"
        " VALUES(?,?,?,?,?,?,?)", units)
    con.executemany(
        "INSERT INTO equipment(unit_id,model,etype,count) VALUES(?,?,?,?)", equips)
    con.executemany(
        "INSERT INTO resources(res_key,res_name,capacity) VALUES(?,?,?)", resources)
    con.executemany(
        "INSERT OR REPLACE INTO geo_anchor(base,lat,lon,kind,region,terrain,"
        "sea_lat,sea_lon) VALUES(?,?,?,?,?,?,?,?)", anchors)
    con.commit()

    print(f"[ok] 建库 -> {DB_PATH}")
    print(f"     单位 {len(units)} 条 · 装备 {len(equips)} 条 · 共享资源 {len(resources)} 项"
          f" · 地理锚点 {len(anchors)} 个")

    # 缺锚点检查：驻地配不全，环境查询会静默降级，必须在这里显式暴露出来
    missing = con.execute(
        "SELECT DISTINCT base FROM units WHERE base IS NOT NULL AND base != ''"
        " AND base NOT IN (SELECT base FROM geo_anchor)").fetchall()
    if missing:
        print(f"\n[注意] {len(missing)} 个驻地未配地理锚点，这些单位查不到气象/地形：")
        print("      " + "、".join(r[0] for r in missing))
        print("      请补到 seed/geo_anchor.json 后重建。")
    print("\n军种聚合（来自视图 v_service_total）：")
    for row in con.execute(
            "SELECT service, unit_count, personnel_total, equipment_total"
            " FROM v_service_total ORDER BY service"):
        print(f"     {row[0]:<8} 单位{row[1]}个 · 人员{row[2]:>6} · 装备{row[3]:>4}件")
    con.close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    build()
