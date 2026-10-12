# -*- coding: utf-8 -*-
"""
force_admin.py —— 兵力池的增删改（管理面）

与 force_query.py 的分工（刻意分开，不要合并）：
  · force_query：**只读**，给参谋取数用。跑砸了最多影响一次筹划。
  · force_admin：**可写**，给人用（界面按钮 / 导入 JSON）。改的是全军的名册真源。
  读写混在一个模块里，迟早会有人图省事让参谋链路上手写库。

双写纪律（本模块存在的核心理由）：
  db/joint_ops.db  —— 运行时真源。参谋取数、HR-06 单位校验都读它 → 改完立即生效。
  seed/*.json      —— 持久真源。db/build_db.py 会 DROP 掉 units/equipment 再从这里灌。
  两个都写、都成功，才算一次成功的修改。只写 DB 的话，用户今天在界面上加的兵，
  哪天重建一次库就静默消失了 —— 这种丢数据的方式最难查，因为它不报错。

本模块 0 API：校验与写库都是纯逻辑，不经过模型。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = ROOT / "db" / "joint_ops.db"
SEED_DIR = ROOT / "seed"
CONSTRAINTS = ROOT / "config" / "constraints.json"

SERVICES = ("陆军", "海军", "空军")
SERVICE_SEED = {"陆军": "army.json", "海军": "navy.json", "空军": "airforce.json"}
READINESS = ("一级", "二级", "三级")

RES_NAME = {
    "transport": "运输机架次",
    "fires": "火力支援单元",
    "satellite": "卫星过顶窗口",
    "sealift": "海运批次",
}

# 只增不删：与 negotiation_round 同理（见 db/build_db.py 的注释）。
# 重建兵力池是为了"重置名册"，不该顺手把"谁在什么时候把哪个旅改成了多少人"抹掉。
FORCE_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS force_change_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    action      TEXT NOT NULL,      -- create / update / delete / import
    target      TEXT NOT NULL,      -- unit:A-001 / equip:A-001|99A / resource:transport
    before_json TEXT,
    after_json  TEXT,
    note        TEXT
);

CREATE INDEX IF NOT EXISTS idx_force_log_ts ON force_change_log(ts DESC, id DESC);
"""


class ForceError(Exception):
    """校验不通过 → 接口层映射成 HTTP 400。带 field 便于前端标到对应输入框。"""

    def __init__(self, msg: str, field: str = ""):
        super().__init__(msg)
        self.msg = msg
        self.field = field


# ─────────────────────────────────────────────────────────
# 字段归一与校验
# ─────────────────────────────────────────────────────────
# 值一次、键两种：seed 文件用中文键（给人看），接口/前端用英文键（给代码用）。
# 导入时两种都收，用户从别处导出的数据不用先改键名。
UNIT_ALIASES = {
    "unit_id": ("unit_id", "id", "编号", "代码"),
    "unit_name": ("unit_name", "单位", "名称", "番号"),
    "service": ("service", "军种"),
    "branch": ("branch", "兵种"),
    "personnel": ("personnel", "人员", "人数", "兵力"),
    "base": ("base", "驻地", "基地"),
    "readiness": ("readiness", "战备状态", "战备"),
    "equipment": ("equipment", "主要装备", "装备"),
}
EQ_ALIASES = {
    "model": ("model", "型号"),
    "etype": ("etype", "type", "类型"),
    "count": ("count", "数量", "件数"),
}


def _pick(d: Dict[str, Any], aliases: Iterable[str], default: Any = None) -> Any:
    for k in aliases:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _text(v: Any, label: str, key: str = "", maxlen: int = 64,
          required: bool = True) -> str:
    """label 是给人看的字段名（进错误消息），key 是给前端找输入框用的（进 data-field）。
    这两个必须分开：早先把"人员"当 key 返回，前端按 personnel 找框，永远标不上红。"""
    s = "" if v is None else str(v).strip()
    if not s:
        if required:
            raise ForceError(f"{label}不能为空", key or label)
        return ""
    if len(s) > maxlen:
        raise ForceError(f"{label}最长 {maxlen} 个字符", key or label)
    return s


def _count(v: Any, label: str, key: str = "", lo: int = 0,
           hi: int = 2_000_000) -> int:
    if isinstance(v, bool):
        raise ForceError(f"{label}必须是整数", key or label)
    if isinstance(v, float) and not v.is_integer():
        raise ForceError(f"{label}必须是整数（收到 {v}）", key or label)
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise ForceError(f"{label}必须是整数（收到 {v!r}）", key or label)
    if n < lo or n > hi:
        raise ForceError(f"{label}必须在 {lo}~{hi} 之间（收到 {n}）", key or label)
    return n


def norm_service(s: Any, required: bool = True, key: str = "service") -> str:
    """军种归一。复用 force_query 的词表（army/陆/陆上 都算陆军），
    避免两处各写一份别名表、以后加军种漏改一处。"""
    from src.tools.force_query import norm_service as _n
    sv = _n("" if s is None else str(s))
    if not sv:
        if required:
            raise ForceError(f"军种只能是 陆军/海军/空军（收到 {s!r}）", key)
        return ""
    return sv


def normalize_unit(raw: Dict[str, Any], default_service: str = "",
                   require_id: bool = True) -> Dict[str, Any]:
    """把任意来源的一条单位数据收敛成内部结构。不合法直接抛，绝不"修正后放行"。"""
    if not isinstance(raw, dict):
        raise ForceError(f"单位数据必须是对象，收到 {type(raw).__name__}")
    uid = _text(_pick(raw, UNIT_ALIASES["unit_id"]), "单位编号", "unit_id",
                32, require_id)
    if uid and any(c.isspace() for c in uid):
        raise ForceError("单位编号不能含空格", "unit_id")
    sv_raw = _pick(raw, UNIT_ALIASES["service"], default_service)
    return {
        "unit_id": uid,
        "unit_name": _text(_pick(raw, UNIT_ALIASES["unit_name"]), "单位名称",
                           "unit_name", 64),
        "service": norm_service(sv_raw),
        "branch": _text(_pick(raw, UNIT_ALIASES["branch"]), "兵种", "branch",
                        64, required=False),
        "personnel": _count(_pick(raw, UNIT_ALIASES["personnel"]), "人员",
                            "personnel"),
        "base": _text(_pick(raw, UNIT_ALIASES["base"]), "驻地", "base",
                      64, required=False),
        "readiness": _readiness(_pick(raw, UNIT_ALIASES["readiness"])),
        "equipment": normalize_equipment_list(_pick(raw, UNIT_ALIASES["equipment"])),
        "has_equipment_key": _pick(raw, UNIT_ALIASES["equipment"]) is not None,
    }


def _readiness(v: Any) -> str:
    s = "" if v is None else str(v).strip()
    if not s:
        return ""
    if s not in READINESS:
        raise ForceError(f"战备状态只能是 一级/二级/三级（收到 {s!r}）", "readiness")
    return s


def normalize_equipment_list(v: Any) -> List[Dict[str, Any]]:
    if v in (None, ""):
        return []
    if not isinstance(v, list):
        raise ForceError("装备清单必须是数组")
    out, seen = [], set()
    for i, item in enumerate(v):
        if not isinstance(item, dict):
            raise ForceError(f"第 {i + 1} 条装备必须是对象")
        model = _text(_pick(item, EQ_ALIASES["model"]), f"第 {i + 1} 条装备的型号",
                      "model", 64)
        if model in seen:
            raise ForceError(f"装备型号「{model}」在同一单位里重复了", "model")
        seen.add(model)
        out.append({
            "model": model,
            "etype": _text(_pick(item, EQ_ALIASES["etype"]),
                           f"「{model}」的类型", "etype", 64, required=False),
            "count": _count(_pick(item, EQ_ALIASES["count"]),
                            f"「{model}」的数量", "count"),
        })
    return out


def normalize_equipment(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ForceError("装备数据必须是对象")
    return {
        "unit_id": _text(_pick(raw, UNIT_ALIASES["unit_id"]), "所属单位",
                         "unit_id", 32),
        "model": _text(_pick(raw, EQ_ALIASES["model"]), "装备型号", "model", 64),
        "etype": _text(_pick(raw, EQ_ALIASES["etype"]), "装备类型", "etype",
                       64, required=False),
        "count": _count(_pick(raw, EQ_ALIASES["count"]), "数量", "count"),
    }


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _atomic_write_json(path: Path, payload: Any) -> None:
    """先写临时文件再替换。中途断电最多丢这次改动，
    不会留下半个 JSON —— 半个 JSON 会让下一次 build_db 直接崩。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


# ─────────────────────────────────────────────────────────
# 管理面
# ─────────────────────────────────────────────────────────
class ForceAdmin:
    def __init__(self, db_path: Path = DB_PATH, seed_dir: Path = SEED_DIR,
                 constraints: Path = CONSTRAINTS):
        self.db_path = Path(db_path)
        self.seed_dir = Path(seed_dir)
        self.constraints = Path(constraints)
        # 同一进程内的写串行化。跨进程靠 sqlite 自己的锁（库已是 WAL，够用）。
        self._lock = threading.Lock()

    # ── 基础 ──────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise ForceError("兵力库不存在，请先运行 db/build_db.py 建库")
        con = sqlite3.connect(str(self.db_path), timeout=20)
        con.row_factory = sqlite3.Row
        return con

    def ensure_schema(self) -> None:
        con = self._conn()
        try:
            con.executescript(FORCE_LOG_SCHEMA)
            con.commit()
        finally:
            con.close()

    def _log(self, con: sqlite3.Connection, action: str, target: str,
             before: Any, after: Any, note: str = "") -> None:
        con.execute(
            "INSERT INTO force_change_log(ts,action,target,before_json,after_json,note)"
            " VALUES(?,?,?,?,?,?)",
            (_now(), action, target,
             json.dumps(before, ensure_ascii=False) if before is not None else None,
             json.dumps(after, ensure_ascii=False) if after is not None else None,
             note))

    # ── 读 ────────────────────────────────────────────────
    def _read_unit(self, con: sqlite3.Connection, unit_id: str) -> Optional[Dict[str, Any]]:
        r = con.execute(
            "SELECT unit_id, unit_name, service, branch, personnel, base, readiness"
            " FROM units WHERE unit_id=?", (unit_id,)).fetchone()
        return dict(r) if r else None

    def _read_equipment(self, con: sqlite3.Connection, unit_id: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in con.execute(
            "SELECT unit_id, model, etype, count FROM equipment"
            " WHERE unit_id=? ORDER BY model", (unit_id,))]

    def _warnings(self, con: sqlite3.Connection, unit: Dict[str, Any]) -> List[str]:
        """把"静默降级"说出来。单位加成了没人管的驻地，
        环境查询会查不到气象/地形 —— 不提醒的话，用户只会在跑方案时发现参谋
        莫名其妙没引用环境数据，然后以为系统坏了。"""
        out: List[str] = []
        base = unit.get("base") or ""
        if base:
            hit = con.execute("SELECT 1 FROM geo_anchor WHERE base=?", (base,)).fetchone()
            if not hit:
                out.append(f"驻地「{base}」还没配地理锚点，该单位的环境查询（气象/地形/海况）会降级。"
                           f"要补齐请编辑 seed/geo_anchor.json 后重建库。")
        return out

    def unit_with_equipment(self, unit_id: str) -> Optional[Dict[str, Any]]:
        con = self._conn()
        try:
            u = self._read_unit(con, unit_id)
            if not u:
                return None
            u["equipment"] = self._read_equipment(con, unit_id)
            return u
        finally:
            con.close()

    # ── 写：单位 ──────────────────────────────────────────
    def upsert_unit(self, raw: Dict[str, Any], orig_id: str = "") -> Dict[str, Any]:
        """新增或修改一个单位。

        orig_id 与 unit_id 不同 = 改编号。装备表是按 unit_id 关联的，
        所以必须把装备一起迁过去再删旧行，否则改完编号装备就"孤儿"了
        （库没开外键约束，SQLite 不会拦你，只会让 v_model_total 里少几行）。
        """
        u = normalize_unit(raw)
        with self._lock:
            con = self._conn()
            try:
                con.execute("BEGIN IMMEDIATE")
                key = (orig_id or "").strip() or u["unit_id"]
                before = self._read_unit(con, key)
                if orig_id and before is None:
                    raise ForceError(f"要修改的单位 {orig_id} 不存在", "unit_id")
                if before and key != u["unit_id"]:
                    if con.execute("SELECT 1 FROM units WHERE unit_id=?",
                                   (u["unit_id"],)).fetchone():
                        raise ForceError(f"单位编号「{u['unit_id']}」已被占用", "unit_id")
                    con.execute("UPDATE equipment SET unit_id=? WHERE unit_id=?",
                                (u["unit_id"], key))
                    con.execute("DELETE FROM units WHERE unit_id=?", (key,))
                con.execute(
                    "INSERT INTO units(unit_id,unit_name,service,branch,personnel,base,readiness)"
                    " VALUES(?,?,?,?,?,?,?)"
                    " ON CONFLICT(unit_id) DO UPDATE SET"
                    "   unit_name=excluded.unit_name, service=excluded.service,"
                    "   branch=excluded.branch, personnel=excluded.personnel,"
                    "   base=excluded.base, readiness=excluded.readiness",
                    (u["unit_id"], u["unit_name"], u["service"], u["branch"],
                     u["personnel"], u["base"], u["readiness"]))
                # 带了装备清单就整份替换；没带就完全不动（单位表单里没有装备字段，
                # 不该因为改了个兵员数就把装备清空）
                if u["has_equipment_key"]:
                    con.execute("DELETE FROM equipment WHERE unit_id=?", (u["unit_id"],))
                    con.executemany(
                        "INSERT INTO equipment(unit_id,model,etype,count) VALUES(?,?,?,?)",
                        [(u["unit_id"], e["model"], e["etype"], e["count"])
                         for e in u["equipment"]])
                after = self._read_unit(con, u["unit_id"])
                after["equipment"] = self._read_equipment(con, u["unit_id"])
                self._log(con, "update" if before else "create",
                          "unit:" + u["unit_id"], before, after)
                warnings = self._warnings(con, after)
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()
            self.resync_seed({(before or {}).get("service"), after["service"]})
        return {"action": "update" if before else "create", "unit": after,
                "warnings": warnings}

    def delete_unit(self, unit_id: str, note: str = "") -> Dict[str, Any]:
        with self._lock:
            con = self._conn()
            try:
                con.execute("BEGIN IMMEDIATE")
                before = self._read_unit(con, unit_id)
                if not before:
                    raise ForceError(f"单位 {unit_id} 不存在", "unit_id")
                before["equipment"] = self._read_equipment(con, unit_id)
                n_eq = con.execute("DELETE FROM equipment WHERE unit_id=?",
                                   (unit_id,)).rowcount
                con.execute("DELETE FROM units WHERE unit_id=?", (unit_id,))
                self._log(con, "delete", "unit:" + unit_id, before, None, note)
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()
            self.resync_seed({before["service"]})
        return {"action": "delete", "unit_id": unit_id, "equipment_deleted": n_eq}

    # ── 写：装备 ──────────────────────────────────────────
    def upsert_equipment(self, raw: Dict[str, Any],
                         orig: Optional[Dict[str, Any]] = None,
                         create_only: bool = False) -> Dict[str, Any]:
        """新增/修改一条装备。身份是 (unit_id, model)：
        要换单位或换型号，就把 orig 传进来，等价于"删旧的、加新的"。

        create_only=True（界面上的「新增装备」按钮用它）：撞上已有型号直接拒绝。
        默认的 upsert 语义会把数量悄悄改掉 —— 用户以为"我又加了一批"，实际是覆盖，
        这种静默改数在兵力池里是不能接受的。改现有那条请走编辑，那里数量是明摆着的。
        """
        e = normalize_equipment(raw)
        with self._lock:
            con = self._conn()
            try:
                con.execute("BEGIN IMMEDIATE")
                if not self._read_unit(con, e["unit_id"]):
                    raise ForceError(f"单位 {e['unit_id']} 不存在，请先建这个单位",
                                     "unit_id")
                o_uid = (orig or {}).get("unit_id", "").strip()
                o_model = (orig or {}).get("model", "").strip()
                before = None
                # moved：身份 (单位, 型号) 变了 = 旧的删掉、按新的插一条。
                # 必须区分"改数量"和"改身份"：都是 upsert，但一个走 UPDATE、
                # 一个走 INSERT，混了就会出现"旧的行删了、新的没插进去"——静默少一件装备。
                moved = False
                if o_uid and o_model and (o_uid, o_model) != (e["unit_id"], e["model"]):
                    row = con.execute(
                        "SELECT unit_id, model, etype, count FROM equipment"
                        " WHERE unit_id=? AND model=?", (o_uid, o_model)).fetchone()
                    if row is None:
                        raise ForceError(f"要修改的装备 {o_uid}/{o_model} 不存在", "model")
                    before = dict(row)
                    moved = True
                    con.execute("DELETE FROM equipment WHERE unit_id=? AND model=?",
                                (o_uid, o_model))
                    if con.execute("SELECT 1 FROM equipment WHERE unit_id=? AND model=?",
                                   (e["unit_id"], e["model"])).fetchone():
                        raise ForceError(
                            f"该单位下已有型号「{e['model']}」，不能重复添加", "model")
                else:
                    row = con.execute(
                        "SELECT unit_id, model, etype, count FROM equipment"
                        " WHERE unit_id=? AND model=?", (e["unit_id"], e["model"])).fetchone()
                    before = dict(row) if row else None
                    if create_only and before:
                        raise ForceError(
                            f"该单位下已有型号「{e['model']}」（现有 {before['count']} 件）。"
                            f"要改数量请点那条记录右侧的「编辑」，或换一个型号。", "model")
                if before and not moved:
                    con.execute("UPDATE equipment SET etype=?, count=?"
                                " WHERE unit_id=? AND model=?",
                                (e["etype"], e["count"], e["unit_id"], e["model"]))
                else:
                    con.execute("INSERT INTO equipment(unit_id,model,etype,count)"
                                " VALUES(?,?,?,?)",
                                (e["unit_id"], e["model"], e["etype"], e["count"]))
                row = con.execute(
                    "SELECT unit_id, model, etype, count FROM equipment"
                    " WHERE unit_id=? AND model=?", (e["unit_id"], e["model"])).fetchone()
                after = dict(row) if row else {"unit_id": e["unit_id"],
                                               "model": e["model"],
                                               "etype": e["etype"], "count": e["count"]}
                self._log(con, "update" if before else "create",
                          f"equip:{e['unit_id']}|{e['model']}", before, after)
                svc = self._read_unit(con, e["unit_id"])["service"]
                old_svc = ""
                if before:
                    old_svc = (self._read_unit(con, before["unit_id"]) or {}).get("service", "")
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()
            self.resync_seed({svc, old_svc})
        return {"action": "update" if before else "create", "equipment": after}

    def delete_equipment(self, unit_id: str, model: str,
                         note: str = "") -> Dict[str, Any]:
        with self._lock:
            con = self._conn()
            try:
                con.execute("BEGIN IMMEDIATE")
                before = con.execute(
                    "SELECT unit_id, model, etype, count FROM equipment"
                    " WHERE unit_id=? AND model=?", (unit_id, model)).fetchone()
                if before is None:
                    raise ForceError(f"装备 {unit_id}/{model} 不存在", "model")
                before = dict(before)
                con.execute("DELETE FROM equipment WHERE unit_id=? AND model=?",
                            (unit_id, model))
                self._log(con, "delete", f"equip:{unit_id}|{model}", before, None, note)
                svc = (self._read_unit(con, unit_id) or {}).get("service", "")
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()
            self.resync_seed({svc})
        return {"action": "delete", "equipment": before}

    # ── 写：共享资源容量 ──────────────────────────────────
    def set_resource(self, res_key: str, capacity: Any) -> Dict[str, Any]:
        """改共享资源容量。

        三处必须一致，缺一处就会出现"给人看的和终验算的不是一个数"：
          resources 表 → 参谋取数（force_query.resource_capacity）
          config/constraints.json → 下次建库/重启后的真源
          CFG（内存）→ 由调用方 server 侧同步，见 server.py 的 _sync_cfg_resource
        """
        key = (res_key or "").strip()
        if not key:
            raise ForceError("资源键不能为空", "res_key")
        cap = _count(capacity, "容量", "capacity", lo=0, hi=10_000_000)
        with self._lock:
            con = self._conn()
            try:
                con.execute("BEGIN IMMEDIATE")
                before = con.execute("SELECT res_key,res_name,capacity FROM resources"
                                     " WHERE res_key=?", (key,)).fetchone()
                if before is None:
                    raise ForceError(f"没有这项共享资源：{key}", "res_key")
                before = dict(before)
                after = {"res_key": key, "res_name": before["res_name"], "capacity": cap}
                con.execute("UPDATE resources SET capacity=? WHERE res_key=?", (cap, key))
                self._log(con, "update", "resource:" + key, before, after)
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()
            self._sync_constraints_resource(key, cap)
        return {"action": "update", "resource": after}

    def _sync_constraints_resource(self, key: str, cap: int) -> Optional[str]:
        if not self.constraints.exists():
            return f"未找到 {self.constraints.name}，容量只改了库；下次建库会回退成旧值。"
        try:
            cfg = json.loads(self.constraints.read_text(encoding="utf-8"))
        except Exception as e:                                    # noqa: BLE001
            return f"{self.constraints.name} 读不动（{e}），容量只改了库。"
        caps = cfg.setdefault("resource_cap", {})
        caps[key] = cap
        _atomic_write_json(self.constraints, cfg)
        return None

    # ── 写：导入 ──────────────────────────────────────────
    def import_units(self, items: Any, mode: str = "merge",
                     default_service: str = "") -> Dict[str, Any]:
        """批量导入。mode:
             merge   —— 按单位编号合并：有则更新，无则新增（默认，最安全）
             replace —— 把"本次数据涉及到的军种"整军种替换掉（先清后插）
        整体一个事务：任何一条不合法，整批都不落库 —— 半批数据比不导入更难收拾。
        """
        if isinstance(items, dict):
            for k in ("units", "items", "data", "单位", "units_list"):
                if k in items:
                    items = items[k]
                    break
        if not isinstance(items, list) or not items:
            raise ForceError("导入内容必须是数组，且不能为空")
        if mode not in ("merge", "replace"):
            raise ForceError(f"导入方式只能是 merge 或 replace（收到 {mode!r}）", "mode")
        ds = norm_service(default_service, required=False) if default_service else ""
        units = [normalize_unit(x, default_service=ds) for x in items]

        # 同一批里编号不能重
        ids = [u["unit_id"] for u in units]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ForceError("同一批数据里有重复的单位编号：" + "、".join(sorted(dup)),
                             "unit_id")
        services = {u["service"] for u in units}

        created = updated = deleted = 0
        warnings: List[str] = []
        with self._lock:
            con = self._conn()
            try:
                con.execute("BEGIN IMMEDIATE")
                if mode == "replace":
                    hosts = ",".join("?" * len(services))
                    rows = con.execute(
                        f"SELECT unit_id FROM units WHERE service IN ({hosts})",
                        tuple(sorted(services))).fetchall()
                    for r in rows:
                        before = self._read_unit(con, r["unit_id"])
                        before["equipment"] = self._read_equipment(con, r["unit_id"])
                        self._log(con, "delete", "unit:" + r["unit_id"], before, None,
                                  "导入-整军种替换")
                        deleted += 1
                    con.execute("DELETE FROM equipment WHERE unit_id IN"
                                f" (SELECT unit_id FROM units WHERE service IN ({hosts}))",
                                tuple(sorted(services)))
                    con.execute(f"DELETE FROM units WHERE service IN ({hosts})",
                                tuple(sorted(services)))
                for u in units:
                    exists = self._read_unit(con, u["unit_id"]) is not None
                    con.execute(
                        "INSERT INTO units(unit_id,unit_name,service,branch,personnel,"
                        " base,readiness) VALUES(?,?,?,?,?,?,?)"
                        " ON CONFLICT(unit_id) DO UPDATE SET"
                        "   unit_name=excluded.unit_name, service=excluded.service,"
                        "   branch=excluded.branch, personnel=excluded.personnel,"
                        "   base=excluded.base, readiness=excluded.readiness",
                        (u["unit_id"], u["unit_name"], u["service"], u["branch"],
                         u["personnel"], u["base"], u["readiness"]))
                    con.execute("DELETE FROM equipment WHERE unit_id=?", (u["unit_id"],))
                    con.executemany(
                        "INSERT INTO equipment(unit_id,model,etype,count) VALUES(?,?,?,?)",
                        [(u["unit_id"], e["model"], e["etype"], e["count"])
                         for e in u["equipment"]])
                    after = self._read_unit(con, u["unit_id"])
                    after["equipment"] = self._read_equipment(con, u["unit_id"])
                    self._log(con, "import", "unit:" + u["unit_id"],
                              None if not exists else {"unit_id": u["unit_id"]},
                              after, f"导入 mode={mode}")
                    warnings += self._warnings(con, after)
                    if exists:
                        updated += 1
                    else:
                        created += 1
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()
            self.resync_seed(services)
            totals = self._service_totals(services)
        return {"mode": mode, "created": created, "updated": updated,
                "deleted": deleted, "services": sorted(services),
                "totals": totals, "warnings": sorted(set(warnings))}

    def _service_totals(self, services: Iterable[str]) -> List[Dict[str, Any]]:
        con = self._conn()
        try:
            hosts = ",".join("?" * len(tuple(services))) if services else ""
            if not hosts:
                return []
            return [dict(r) for r in con.execute(
                "SELECT service, unit_count, personnel_total, equipment_total"
                f" FROM v_service_total WHERE service IN ({hosts}) ORDER BY service",
                tuple(services))]
        finally:
            con.close()

    # ── 导出 / 变更日志 ───────────────────────────────────
    def export_units(self, service: str = "") -> List[Dict[str, Any]]:
        """按 seed 文件的中文键导出，导出的东西能直接再导入回来（闭环）。"""
        sv = norm_service(service, required=False) if service else ""
        con = self._conn()
        try:
            sql = ("SELECT unit_id, unit_name, service, branch, personnel, base, readiness"
                   " FROM units")
            params: Tuple = ()
            if sv:
                sql += " WHERE service=?"
                params = (sv,)
            sql += " ORDER BY service, unit_id"
            out = []
            for r in con.execute(sql, params):
                u = dict(r)
                eq = self._read_equipment(con, u["unit_id"])
                out.append({
                    "id": u["unit_id"], "单位": u["unit_name"], "军种": u["service"],
                    "兵种": u["branch"] or "", "人员": u["personnel"],
                    "主要装备": [{"型号": e["model"], "类型": e["etype"] or "",
                                  "数量": e["count"]} for e in eq],
                    "驻地": u["base"] or "", "战备状态": u["readiness"] or "",
                })
            return out
        finally:
            con.close()

    def changelog(self, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 50), 500))
        con = self._conn()
        try:
            rows = []
            for r in con.execute(
                    "SELECT id, ts, action, target, before_json, after_json, note"
                    " FROM force_change_log ORDER BY id DESC LIMIT ?", (limit,)):
                d = dict(r)
                for k in ("before_json", "after_json"):
                    if d.get(k):
                        try:
                            d[k.replace("_json", "")] = json.loads(d[k])
                        except Exception:                         # noqa: BLE001
                            pass
                    d.pop(k, None)
                rows.append(d)
            return rows
        finally:
            con.close()

    # ── seed 回写 ─────────────────────────────────────────
    def resync_seed(self, services: Iterable[Optional[str]]) -> List[str]:
        """把 DB 里这几个军种的现状回写成 seed/*.json。

        以 DB 为准整份重生成，而不是"在原文里改几行"：增量改法一旦漏一个字段
        （比如改名时忘了同步 驻地），DB 和 seed 就会长期不一致，而且没人会发现。
        重生成保证"此刻的 seed 就是此刻的库"。seed 的键名/顺序与手写文件保持一致。
        """
        done: List[str] = []
        todo = sorted({s for s in services if s in SERVICE_SEED})
        if not todo:
            return done
        con = self._conn()
        try:
            for sv in todo:
                units = [dict(r) for r in con.execute(
                    "SELECT unit_id, unit_name, service, branch, personnel, base, readiness"
                    " FROM units WHERE service=? ORDER BY unit_id", (sv,))]
                eq_map: Dict[str, List[Dict[str, Any]]] = {}
                for e in con.execute(
                        "SELECT e.unit_id, e.model, e.etype, e.count FROM equipment e"
                        " JOIN units u ON u.unit_id = e.unit_id"
                        " WHERE u.service=? ORDER BY e.unit_id, e.model", (sv,)):
                    eq_map.setdefault(e["unit_id"], []).append({
                        "型号": e["model"], "类型": e["etype"] or "", "数量": e["count"]})
                payload = [{
                    "id": u["unit_id"], "单位": u["unit_name"], "军种": u["service"],
                    "兵种": u["branch"] or "", "人员": u["personnel"],
                    "主要装备": eq_map.get(u["unit_id"], []),
                    "驻地": u["base"] or "", "战备状态": u["readiness"] or "",
                } for u in units]
                path = self.seed_dir / SERVICE_SEED[sv]
                _atomic_write_json(path, payload)
                done.append(path.name)
        finally:
            con.close()
        return done


# 单例：server 与脚本共用，避免每次请求重开对象
ADMIN = ForceAdmin()
