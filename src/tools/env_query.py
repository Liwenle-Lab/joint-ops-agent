# -*- coding: utf-8 -*-
"""
env_query.py —— 参谋的作战环境查询工具（气象 / 海洋 / 地形高程 / 地名）

与 force_query.py 同属"工具层"，遵守同一套纪律：
  1. 每个数字必须有 query_id，方案里写作 source: "env:<query_id>"，终验按 id 回查
  2. 本模块不做军事判断 —— 只给真实数值，能不能登陆由参谋/指挥层解读
  3. 每次查询落 trace/queries.jsonl（与兵力查询同文件，溯源口径统一）

★ 两个容易被忽略的工程约束：
  · 必须提供 async 版本。二期起三参谋是 asyncio.gather 并行的，
    同步 urllib 会阻塞事件循环，把并行打回串行。故内部同步实现 + to_thread 包装。
  · 网络失败必须降级而非抛异常。本系统一路都是 graceful degradation
    （无 key 降级 Mock、失败分层回退），环境查询断网时应返回"数据不可用"，
    让方案标注假设，而不是整条链路崩掉。

数据源：Open-Meteo（免费、无需 API key，非商用 CC BY 4.0）
  实测延迟（2026-09-23，本机直连）：forecast 0.9s · elevation 0.9s · marine 2.1s · geocode 1.4s
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = ROOT / "db" / "joint_ops.db"
QUERY_LOG = ROOT / "trace" / "queries.jsonl"
CACHE_PATH = ROOT / "trace" / "env_cache.json"

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

# 天气数据约每 6 小时更新，缓存 6 小时即可——避免重复调用也利于弱网环境
CACHE_TTL_SEC = 6 * 3600
HTTP_TIMEOUT = 15

ENDPOINTS = {
    "forecast": FORECAST_URL, "marine": MARINE_URL,
    "elevation": ELEVATION_URL, "geocode": GEOCODE_URL,
}

# WMO 天气代码 → 中文。模型读代码容易误判，给可读文本降低瞎猜概率
WMO_CODE = {
    0: "晴", 1: "大部晴朗", 2: "局部多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "轻毛毛雨", 53: "毛毛雨", 55: "浓毛毛雨",
    56: "轻冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "轻冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "小阵雨", 81: "中阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "大阵雪",
    95: "雷暴", 96: "雷暴伴小冰雹", 99: "雷暴伴大冰雹",
}


def wx(code: Any) -> str:
    try:
        return WMO_CODE.get(int(code), f"未知({code})")
    except (TypeError, ValueError):
        return "未知"


@dataclass
class EnvResult:
    """一次环境查询的凭证。rows 里的数字是唯一权威来源。"""
    query_id: str
    endpoint: str
    params: Dict[str, Any]
    columns: List[str]
    rows: List[Dict[str, Any]]
    note: str = ""
    available: bool = True          # False = 数据源不可用（网络失败/无锚点），不得当作数字使用
    error: str = ""
    cached: bool = False
    fetched_at: str = ""

    def source(self) -> str:
        return f"env:{self.query_id}"

    def to_llm_view(self) -> str:
        if not self.available:
            return (f"[env_id={self.query_id}] ⚠环境数据不可用：{self.error}"
                    f"（不得臆测，方案请标注该维度为假设）")
        head = f"[env_id={self.query_id}] {self.note}"
        if self.cached:
            head += "（本地缓存）"
        if not self.rows:
            return head + "\n（无数据）"
        body = "\n".join(" | ".join(f"{k}={v}" for k, v in row.items())
                         for row in self.rows)
        return f"{head}\n{body}"

    def __bool__(self) -> bool:
        """让 `if result:` 表达的是'数据可用'而不是'对象存在'"""
        return self.available


class EnvPool:
    def __init__(self, db_path: Path = DB_PATH, cache_ttl: int = CACHE_TTL_SEC,
                 offline: bool = False):
        self.db_path = Path(db_path)
        self.cache_ttl = cache_ttl
        self.offline = offline              # 离线模式：完全不联网，只走缓存
        self._lock = threading.Lock()
        self._registry: Dict[str, EnvResult] = {}
        self._cache: Dict[str, Tuple[float, dict]] = {}
        QUERY_LOG.parent.mkdir(parents=True, exist_ok=True)
        self._load_cache()

    # ── 缓存 ────────────────────────────────────────────
    def _load_cache(self) -> None:
        if not CACHE_PATH.exists():
            return
        try:
            raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            now = time.time()
            for k, v in raw.items():
                if now - v.get("_ts", 0) < self.cache_ttl:
                    self._cache[k] = (v["_ts"], v["data"])
        except Exception:                   # noqa: BLE001  缓存坏了就当没有，不影响主流程
            self._cache = {}

    def _save_cache(self) -> None:
        try:
            payload = {k: {"_ts": ts, "data": d}
                       for k, (ts, d) in self._cache.items()}
            CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False),
                                  encoding="utf-8")
        except Exception:                   # noqa: BLE001
            pass

    @staticmethod
    def _ck(endpoint: str, params: Dict[str, Any]) -> str:
        return endpoint + "?" + urllib.parse.urlencode(sorted(params.items()))

    # ── HTTP ────────────────────────────────────────────
    def _http(self, url: str, params: Dict[str, Any]) -> dict:
        full = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            full, headers={"User-Agent": "joint-ops-agent/1.0"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))

    def _fetch(self, endpoint: str, params: Dict[str, Any]
               ) -> Tuple[Optional[dict], bool, str]:
        """返回 (data, cached, error)。★ 永不抛异常 —— 失败降级是本系统的默认哲学"""
        ck = self._ck(endpoint, params)
        with self._lock:
            hit = self._cache.get(ck)
        if hit and time.time() - hit[0] < self.cache_ttl:
            return hit[1], True, ""
        if self.offline:
            return None, False, "离线模式：无联网权限且本地无缓存"
        try:
            data = self._http(ENDPOINTS[endpoint], params)
        except Exception as e:              # noqa: BLE001
            return None, False, f"{type(e).__name__}: {e}"
        with self._lock:
            self._cache[ck] = (time.time(), data)
            self._save_cache()
        return data, False, ""

    # ── 底层：凭证生成统一出口 ────────────────────────────
    def _wrap(self, endpoint: str, params: Dict[str, Any], note: str,
              columns: List[str], rows: List[Dict[str, Any]],
              cached: bool, error: str = "") -> EnvResult:
        available = bool(rows) and not error
        res = EnvResult(
            query_id="e_" + uuid.uuid4().hex[:8],
            endpoint=endpoint, params=params, columns=columns, rows=rows,
            note=note, available=available, error=error, cached=cached,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with self._lock:
            self._registry[res.query_id] = res
            with QUERY_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(res), ensure_ascii=False) + "\n")
        return res

    def get(self, query_id: str) -> Optional[EnvResult]:
        """按 query_id 回查（终验用）"""
        return self._registry.get(query_id)

    # ── 地理锚点：地名 → 坐标 ─────────────────────────────
    def anchor(self, base: str) -> Optional[Dict[str, Any]]:
        """驻地→经纬度。查不到返回 None —— 调用方负责降级提示"""
        if not self.db_path.exists():
            return None
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            r = con.execute("SELECT * FROM geo_anchor WHERE base=?", (base,)).fetchone()
            return dict(r) if r else None
        finally:
            con.close()

    def sea_anchor(self, base: str) -> Optional[Dict[str, Any]]:
        """海洋取样点。

        港口锚点常落在深入内陆的海湾里（实测：三都澳 26.62,119.68 处 Open-Meteo
        的海洋网格判定为陆地，海表温度恒为 None）。而"港"与"港外海面"物理上
        本就是两个位置 —— 岸上指挥所的坐标，不该直接拿去采海况。

        故 geo_anchor 可配 sea_lat/sea_lon 作为海面取样点；不配则退回主坐标。
        """
        a = self.anchor(base)
        if not a:
            return None
        if a.get("sea_lat") is not None and a.get("sea_lon") is not None:
            a = dict(a)
            a["lat"], a["lon"] = a["sea_lat"], a["sea_lon"]
            a["_sea_offset"] = True
        return a

    def bases_of(self, service: Optional[str] = None) -> List[Dict[str, Any]]:
        """某军种（或全部）涉及哪些驻地——批量环境查询的入口"""
        if not self.db_path.exists():
            return []
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            if service:
                rows = con.execute(
                    "SELECT DISTINCT base, lat, lon, kind, region, terrain "
                    "FROM v_unit_geo WHERE service=? AND lat IS NOT NULL "
                    "ORDER BY base",
                    (service,)).fetchall()
            else:
                rows = con.execute(
                    "SELECT DISTINCT base, lat, lon, kind, region, terrain "
                    "FROM v_unit_geo WHERE lat IS NOT NULL ORDER BY base").fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def all_anchors(self) -> List[Dict[str, Any]]:
        """锚点全表（含 sea_lat/sea_lon），供地图页与控制台共用一份坐标源。

        与 bases_of() 的区别：bases_of 走 v_unit_geo，只有"确实有部队驻扎"的驻地
        才出现；这里直接读 geo_anchor，把映射表本身完整交出去——地图上要画的
        是所有配置过的锚点，哪怕某个驻地暂时没编部队。
        """
        if not self.db_path.exists():
            return []
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT base, lat, lon, kind, region, terrain, sea_lat, sea_lon "
                "FROM geo_anchor ORDER BY kind, base").fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    # ── 环境查询（同步） ──────────────────────────────────
    def weather(self, lat: float, lon: float, days: int = 3,
                label: str = "") -> EnvResult:
        params = {
            "latitude": round(float(lat), 4), "longitude": round(float(lon), 4),
            "current": "temperature_2m,relative_humidity_2m,precipitation,"
                       "weather_code,cloud_cover,wind_speed_10m,"
                       "wind_direction_10m,wind_gusts_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_sum,precipitation_probability_max,"
                     "wind_speed_10m_max",
            "timezone": "auto", "forecast_days": max(1, min(int(days), 16)),
        }
        data, cached, err = self._fetch("forecast", params)
        rows: List[Dict[str, Any]] = []
        if data:
            cur = data.get("current") or {}
            rows.append({
                "日期": "当前", "气温℃": cur.get("temperature_2m"),
                "相对湿度%": cur.get("relative_humidity_2m"),
                "云量%": cur.get("cloud_cover"),
                "天气": wx(cur.get("weather_code")),
                "风速km/h": cur.get("wind_speed_10m"),
                "阵风km/h": cur.get("wind_gusts_10m"),
                "风向°": cur.get("wind_direction_10m"),
                "降水mm": cur.get("precipitation"),
            })
            d = data.get("daily") or {}
            for i, day in enumerate(d.get("time") or []):
                rows.append({
                    "日期": day,
                    "气温℃": f"{(d.get('temperature_2m_min') or [None]*99)[i]}"
                             f"~{(d.get('temperature_2m_max') or [None]*99)[i]}",
                    "相对湿度%": None, "云量%": None,
                    "天气": wx((d.get("weather_code") or [None]*99)[i]),
                    "风速km/h": (d.get("wind_speed_10m_max") or [None]*99)[i],
                    "阵风km/h": None, "风向°": None,
                    "降水mm": (d.get("precipitation_sum") or [None]*99)[i],
                })
        who = f"{label} " if label else ""
        return self._wrap("forecast", params, f"{who}陆上气象 {lat:.2f},{lon:.2f}",
                          list(rows[0].keys()) if rows else [], rows, cached, err)

    def marine(self, lat: float, lon: float, days: int = 3,
               label: str = "") -> EnvResult:
        """海洋：浪高/周期/方向/海表温度 —— 登陆与舰艇行动的关键约束"""
        params = {
            "latitude": round(float(lat), 4), "longitude": round(float(lon), 4),
            "daily": "wave_height_max,wave_period_max,wave_direction_dominant,"
                     "sea_surface_temperature_max",
            "timezone": "auto", "forecast_days": max(1, min(int(days), 16)),
        }
        data, cached, err = self._fetch("marine", params)
        rows: List[Dict[str, Any]] = []
        if data:
            d = data.get("daily") or {}
            for i, day in enumerate(d.get("time") or []):
                rows.append({
                    "日期": day,
                    "最大浪高m": (d.get("wave_height_max") or [None]*99)[i],
                    "最大周期s": (d.get("wave_period_max") or [None]*99)[i],
                    "主浪向°": (d.get("wave_direction_dominant") or [None]*99)[i],
                    "海表温度℃": (d.get("sea_surface_temperature_max") or [None]*99)[i],
                })
        who = f"{label} " if label else ""
        return self._wrap("marine", params, f"{who}海洋水文 {lat:.2f},{lon:.2f}",
                          list(rows[0].keys()) if rows else [], rows, cached, err)

    def terrain(self, lat: float, lon: float, label: str = "") -> EnvResult:
        """地形高程（90m DEM）"""
        params = {"latitude": round(float(lat), 4),
                  "longitude": round(float(lon), 4)}
        data, cached, err = self._fetch("elevation", params)
        rows: List[Dict[str, Any]] = []
        if data:
            el = data.get("elevation")
            if isinstance(el, list) and el:
                el = el[0]
            if el is not None:
                rows.append({"高程m": el})
        who = f"{label} " if label else ""
        return self._wrap("elevation", params, f"{who}地形高程 {lat:.2f},{lon:.2f}",
                          ["高程m"], rows, cached, err)

    def geocode(self, name: str) -> EnvResult:
        """把地名换成坐标（用于临时补锚点，或解析作战地域）"""
        params = {"name": name, "count": 5, "language": "zh", "format": "json"}
        data, cached, err = self._fetch("geocode", params)
        rows: List[Dict[str, Any]] = []
        if data:
            for g in data.get("results") or []:
                rows.append({"地名": g.get("name"), "纬度": g.get("latitude"),
                             "经度": g.get("longitude"),
                             "行政区": g.get("admin1") or "",
                             "国家": g.get("country") or ""})
        return self._wrap("geocode", params, f"地名检索「{name}」",
                          list(rows[0].keys()) if rows else [], rows, cached, err)

    def base_brief(self, base: str, days: int = 3) -> EnvResult:
        """一个驻地的一次性环境快照。按设施类型自动决定查什么：
        port → 气象+海洋；airfield/land → 气象+地形。"""
        a = self.anchor(base)
        if not a:
            return self._wrap("anchor", {"base": base}, base, [], [], False,
                              f"驻地「{base}」缺地理锚点，请补 seed/geo_anchor.json")
        kind = a.get("kind") or "land"
        parts: List[str] = []
        combined: List[Dict[str, Any]] = []
        err = ""

        w = self.weather(a["lat"], a["lon"], days, label=base)
        parts.append(w.to_llm_view())
        if w:
            combined.append({"维度": "气象", "摘要": _compact(w.rows[0]) if w.rows else ""})
        else:
            err = w.error

        if kind == "port":
            sa = self.sea_anchor(base) or a
            m = self.marine(sa["lat"], sa["lon"], days, label=base)
            parts.append(m.to_llm_view())
            if m:
                combined.append({"维度": "海洋", "摘要": _compact(m.rows[0]) if m.rows else ""})
            elif not err:
                err = m.error
        else:
            t = self.terrain(a["lat"], a["lon"], label=base)
            parts.append(t.to_llm_view())
            if t and t.rows:
                combined.append({"维度": "地形",
                                 "摘要": f"高程 {t.rows[0].get('高程m')} m"})

        res = self._wrap(
            "brief", {"base": base, "kind": kind}, f"{base}（{a.get('region','')}"
            f"·{a.get('terrain','')}）环境快照", ["维度", "摘要"], combined, False, err)
        res.available = bool(combined)
        return res

    # ── 结构化快照（给界面填格子用） ──────────────────────
    # 键名映射只在这里出现一次。曾经的教训：Web 层各自硬编码 "浪高m" 之类的键，
    # 而实际键名是 "最大浪高m"，字段对不上就静默少一格 —— 界面看着正常，
    # 数据却悄悄丢了。集中到数据层，前端只认 label。
    _BRIEF_COMMON = [("天气", "天气", ""), ("气温", "气温℃", "℃"),
                     ("风速", "风速km/h", "km/h")]
    _BRIEF_MAP = {
        "port":     [("浪高", "最大浪高m", "m"), ("浪涌周期", "最大周期s", "s"),
                     ("浪向", "主浪向°", "°"), ("海温", "海表温度℃", "℃")],
        "airfield": [("阵风", "阵风km/h", "km/h"), ("云量", "云量%", "%"),
                     ("湿度", "相对湿度%", "%")],
        "land":     [("湿度", "相对湿度%", "%"), ("降水", "降水mm", "mm")],
    }

    @staticmethod
    def _pairs(row: Dict[str, Any], spec: List[Tuple[str, str, str]]
               ) -> List[Dict[str, Any]]:
        out = []
        for label, key, unit in spec:
            v = row.get(key)
            if v is not None:
                out.append({"label": label, "value": v, "unit": unit})
        return out

    def brief(self, base: str, days: int = 1) -> Dict[str, Any]:
        """一个驻地的环境快照（结构化）。

        与 base_brief 的分工：base_brief 产出给 LLM 读的文本，这里产出给界面
        填格子的 items。同一份数据两种消费方式，都从同一次查询取数。
        """
        a = self.anchor(base)
        if not a:
            return {"base": base, "kind": "", "items": [], "sources": [],
                    "degraded": True,
                    "error": f"驻地「{base}」缺地理锚点，请补 seed/geo_anchor.json"}

        kind = a.get("kind") or "land"
        items: List[Dict[str, Any]] = []
        src: List[str] = []
        cached = False

        w = self.weather(a["lat"], a["lon"], days)
        if w.available and w.rows:
            src.append(w.source())
            cached = cached or w.cached
            items += self._pairs(w.rows[0],
                                 self._BRIEF_COMMON + self._BRIEF_MAP.get(kind, ()))

        if kind == "port":
            sa = self.sea_anchor(base) or a
            m = self.marine(sa["lat"], sa["lon"], days)
            if m.available and m.rows:
                src.append(m.source())
                cached = cached or m.cached
                items += self._pairs(m.rows[0], self._BRIEF_MAP["port"])

        # 港口也取高程：潮位基面与登陆场坡度判断都要它，0 m 本身是有效信息
        t = self.terrain(a["lat"], a["lon"])
        if t.available and t.rows:
            src.append(t.source())
            cached = cached or t.cached
            items += self._pairs(t.rows[0], [("高程", "高程m", "m")])

        return {
            "base": base, "kind": kind, "region": a.get("region"),
            "terrain": a.get("terrain"), "lat": a["lat"], "lon": a["lon"],
            "items": items, "sources": src,
            "degraded": not items,        # 一项都没拿到才算真降级
            "cached": cached,
        }

    # ── 异步版本：供 async 参谋节点调用 ────────────────────
    # 用 to_thread 包同步实现。★ 不用同步 HTTP 阻塞事件循环，
    #   否则二期改的 asyncio.gather 并行会被打回串行。
    async def a_weather(self, *a, **kw) -> EnvResult:
        return await asyncio.to_thread(self.weather, *a, **kw)

    async def a_marine(self, *a, **kw) -> EnvResult:
        return await asyncio.to_thread(self.marine, *a, **kw)

    async def a_terrain(self, *a, **kw) -> EnvResult:
        return await asyncio.to_thread(self.terrain, *a, **kw)

    async def a_base_brief(self, *a, **kw) -> EnvResult:
        return await asyncio.to_thread(self.base_brief, *a, **kw)


def _compact(row: Dict[str, Any]) -> str:
    """把一行数据压成一句话，便于放在联合表格里"""
    return " ".join(f"{k}={v}" for k, v in row.items()
                    if v is not None and k != "日期")


# ── OpenAI function-calling 工具表 ─────────────────────────
# 注意：每个 description 都明写"数字必须来自本工具" —— 与 force_query 同一口径，
# 否则模型会顺手编一个风速出来（HR-07 的存在就是为了堵这个）。
ENV_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "query_base_env",
            "description": "查询某驻地的作战环境快照（气象+地形，港口另加海洋水文），"
                           "含气温/风/降水/浪高等。数字必须来自本工具，禁止心算或凭印象。",
            "parameters": {
                "type": "object",
                "properties": {
                    "base": {"type": "string", "description": "驻地名称，如 南屿军港、临漳场站"},
                    "days": {"type": "integer", "description": "预报天数，默认 3，最大 16"},
                },
                "required": ["base"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_service_bases",
            "description": "列出某军种涉及的驻地及其坐标、地形类型。查环境前的第一步："
                           "先搞清楚要去查哪些地方。",
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
            "name": "query_marine",
            "description": "查询指定坐标的海洋水文（最大浪高、涌浪周期、主浪向、海表温度）。"
                           "登陆/舰艇行动评估必查。",
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {"type": "number", "description": "纬度"},
                    "lon": {"type": "number", "description": "经度"},
                    "days": {"type": "integer", "description": "预报天数，默认 3"},
                },
                "required": ["lat", "lon"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_terrain",
            "description": "查询指定坐标的地形高程（米，90m DEM）。判断通行性与观察条件必查。",
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {"type": "number", "description": "纬度"},
                    "lon": {"type": "number", "description": "经度"},
                },
                "required": ["lat", "lon"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_geocode",
            "description": "把地名转换成经纬度。用于解析作战地域，或补录新的驻地锚点。",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "地名"}},
                "required": ["name"],
            },
        },
    },
]


ENV_TOOL_DISPATCH = {
    "query_base_env": lambda pool, a: pool.base_brief(a["base"], a.get("days", 3)),
    "query_marine": lambda pool, a: pool.marine(a["lat"], a["lon"], a.get("days", 3)),
    "query_terrain": lambda pool, a: pool.terrain(a["lat"], a["lon"]),
    "query_geocode": lambda pool, a: pool.geocode(a["name"]),
}


def call_env_tool(pool: "EnvPool", name: str,
                  arguments: Dict[str, Any]) -> EnvResult:
    """query_service_bases 返回 dict（多行），统一包装成 EnvResult 以便同样溯源"""
    if name == "query_service_bases":
        rows = pool.bases_of(arguments.get("service"))
        return pool._wrap(
            "anchor", {"service": arguments.get("service")},
            f"{arguments.get('service') or '全部'}涉及驻地",
            list(rows[0].keys()) if rows else [], rows, False,
            "" if rows else "无驻地记录或缺少地理锚点")
    if name not in ENV_TOOL_DISPATCH:
        raise KeyError(f"未注册的环境工具：{name}")
    return ENV_TOOL_DISPATCH[name](pool, arguments)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    pool = EnvPool()
    print("=== 驻地列表 ===")
    for b in pool.bases_of():
        print(f"  {b['base']:<10} {b['kind']:<9} {b['lat']:>6},{b['lon']:>7}  {b['terrain']}")
    print("\n=== 陆军全部驻地环境快照 ===")
    t0 = time.time()
    for b in pool.bases_of("陆军"):
        r = pool.base_brief(b["base"], days=2)
        print(r.to_llm_view())
        print(f"  source = {r.source()}\n")
    print(f"耗时 {time.time()-t0:.1f}s")
