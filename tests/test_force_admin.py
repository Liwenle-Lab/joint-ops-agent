# -*- coding: utf-8 -*-
"""
test_force_admin.py —— 兵力池增删改的回归测试

为什么要有这个文件：
  兵力池以前是只读的，改坏了顶多界面显示错。现在它能写，写错就是"参谋按错的名册
  做方案"——而且不会报错，只会算出一个看着很合理的结果。所以每个写操作都要有测试。

关键设计：**全程在副本上跑**。
  db/joint_ops.db 复制到临时目录，seed/*.json 与 config/constraints.json 同样复制，
  ForceAdmin 指向副本。跑测试永远不会动到真实兵力数据，可以随便删。

用法：python tests/test_force_admin.py
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.tools.force_admin import ForceAdmin, ForceError              # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


def expect_error(name: str, fn, contains: str = "") -> None:
    """验证"该拒绝的确实拒绝了"。
    只测成功路径的测试是假的安全感 —— 校验逻辑坏了它照样全绿。"""
    try:
        fn()
    except ForceError as e:
        ok = (contains in e.msg) if contains else True
        check(name, ok, e.msg if not ok else "")
        return
    except Exception as e:                                    # noqa: BLE001
        check(name, False, f"抛了别的异常：{type(e).__name__}: {e}")
        return
    check(name, False, "本该拒绝，却成功了")


def rows(con, sql, params=()):
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql, params)]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="force_test_"))
    try:
        # ── 搭副本 ────────────────────────────────────────
        shutil.copy(ROOT / "db" / "joint_ops.db", tmp / "joint_ops.db")
        (tmp / "seed").mkdir()
        for f in ("army.json", "navy.json", "airforce.json", "geo_anchor.json"):
            shutil.copy(ROOT / "seed" / f, tmp / "seed" / f)
        shutil.copy(ROOT / "config" / "constraints.json", tmp / "constraints.json")

        db = tmp / "joint_ops.db"
        admin = ForceAdmin(db, tmp / "seed", tmp / "constraints.json")
        admin.ensure_schema()

        def con():
            c = sqlite3.connect(str(db))
            c.row_factory = sqlite3.Row
            return c

        baseline = {}
        with con() as c:
            for r in rows(c, "SELECT service, unit_count, personnel_total, equipment_total"
                             " FROM v_service_total"):
                baseline[r["service"]] = r
        print(f"副本基线：陆军 {baseline['陆军']['personnel_total']} 人 · "
              f"海军 {baseline['海军']['personnel_total']} 人 · "
              f"空军 {baseline['空军']['personnel_total']} 人")

        # ── 1. 新增单位 ───────────────────────────────────
        print("\n[1] 新增单位")
        out = admin.upsert_unit({
            "unit_id": "T-901", "unit_name": "测试合成旅", "service": "陆军",
            "branch": "合成兵种", "personnel": 1000, "base": "东山基地",
            "readiness": "二级",
            "equipment": [{"model": "99A", "etype": "主战坦克", "count": 10},
                          {"model": "PLZ-05", "etype": "自行火炮", "count": 5}],
        })
        check("返回动作是 create", out["action"] == "create")
        with con() as c:
            t = rows(c, "SELECT personnel_total, equipment_total, unit_count"
                        " FROM v_service_total WHERE service='陆军'")[0]
            neq = rows(c, "SELECT model, count FROM equipment WHERE unit_id='T-901'")
        check("陆军人数 +1000（视图跟着变）",
              t["personnel_total"] == baseline["陆军"]["personnel_total"] + 1000,
              f"{t['personnel_total']}")
        check("陆军装备 +15 件",
              t["equipment_total"] == baseline["陆军"]["equipment_total"] + 15)
        check("单位数 +1", t["unit_count"] == baseline["陆军"]["unit_count"] + 1)
        check("装备写入 2 条", len(neq) == 2)

        # ── 2. 改人数 / 改驻地 ────────────────────────────
        print("\n[2] 修改单位")
        admin.upsert_unit({"unit_id": "T-901", "unit_name": "测试合成旅",
                           "service": "陆军", "branch": "合成兵种",
                           "personnel": 2500, "base": "东山基地", "readiness": "一级"})
        with con() as c:
            u = rows(c, "SELECT * FROM units WHERE unit_id='T-901'")[0]
            n2 = rows(c, "SELECT personnel_total FROM v_service_total"
                         " WHERE service='陆军'")[0]["personnel_total"]
        check("人数改成 2500", u["personnel"] == 2500)
        check("战备改成一级", u["readiness"] == "一级")
        check("视图同步到 +2500",
              n2 == baseline["陆军"]["personnel_total"] + 2500, f"{n2}")
        check("不带装备字段时装备不被清空",
              len(rows(con(), "SELECT 1 FROM equipment WHERE unit_id='T-901'")) == 2)

        # ── 3. 改编号：装备要跟着迁 ───────────────────────
        print("\n[3] 改单位编号（装备不能变孤儿）")
        admin.upsert_unit({"unit_id": "T-902", "unit_name": "测试合成旅",
                           "service": "陆军", "branch": "合成兵种",
                           "personnel": 2500, "base": "东山基地", "readiness": "一级"},
                          orig_id="T-901")
        with con() as c:
            check("旧编号已不存在",
                  not rows(c, "SELECT 1 FROM units WHERE unit_id='T-901'"))
            check("装备迁到新编号",
                  len(rows(c, "SELECT 1 FROM equipment WHERE unit_id='T-902'")) == 2)
            check("没有孤儿装备（全库装备都能对上单位）",
                  not rows(c, "SELECT e.unit_id FROM equipment e"
                              " LEFT JOIN units u ON u.unit_id=e.unit_id"
                              " WHERE u.unit_id IS NULL"))

        # ── 4. 装备增改删 ─────────────────────────────────
        print("\n[4] 装备增改删")
        admin.upsert_equipment({"unit_id": "T-902", "model": "直-20",
                                "etype": "通用直升机", "count": 6})
        with con() as c:
            m = rows(c, "SELECT total FROM v_model_total WHERE service='陆军'"
                         " AND model='直-20'")[0]["total"]
        base_zh20 = None
        with con() as c:
            r = rows(c, "SELECT SUM(count) s FROM equipment e JOIN units u"
                         " ON u.unit_id=e.unit_id WHERE u.service='陆军'"
                         " AND e.model='直-20' AND e.unit_id!='T-902'")
            base_zh20 = r[0]["s"] or 0
        check("新增装备计入型号汇总", m == base_zh20 + 6, f"{m} vs {base_zh20}+6")

        admin.upsert_equipment({"unit_id": "T-902", "model": "直-20",
                                "etype": "通用直升机", "count": 9})
        with con() as c:
            check("改数量不是新增一行",
                  len(rows(c, "SELECT 1 FROM equipment WHERE unit_id='T-902'"
                              " AND model='直-20'")) == 1)
            check("数量已更新为 9",
                  rows(c, "SELECT count FROM equipment WHERE unit_id='T-902'"
                          " AND model='直-20'")[0]["count"] == 9)

        admin.upsert_equipment({"unit_id": "T-902", "model": "直-8B",
                                "etype": "运输直升机", "count": 4},
                               orig={"unit_id": "T-902", "model": "直-20"})
        with con() as c:
            check("改型号后旧型号没了",
                  not rows(c, "SELECT 1 FROM equipment WHERE unit_id='T-902'"
                              " AND model='直-20'"))
            check("新型号在库里",
                  rows(c, "SELECT count FROM equipment WHERE unit_id='T-902'"
                          " AND model='直-8B'")[0]["count"] == 4)

        admin.delete_equipment("T-902", "直-8B")
        with con() as c:
            check("删除生效",
                  not rows(c, "SELECT 1 FROM equipment WHERE unit_id='T-902'"
                              " AND model='直-8B'"))

        # ── 5. 该拒绝的必须拒绝 ───────────────────────────
        print("\n[5] 非法输入必须拒绝")
        expect_error("人员负数被拒",
                     lambda: admin.upsert_unit({"unit_id": "T-903", "unit_name": "X",
                                                "service": "陆军", "personnel": -5}),
                     "人员")
        expect_error("人员小数被拒",
                     lambda: admin.upsert_unit({"unit_id": "T-903", "unit_name": "X",
                                                "service": "陆军", "personnel": 4000.5}),
                     "整数")
        expect_error("人员写汉字被拒",
                     lambda: admin.upsert_unit({"unit_id": "T-903", "unit_name": "X",
                                                "service": "陆军", "personnel": "约四千"}),
                     "整数")
        expect_error("军种乱写被拒",
                     lambda: admin.upsert_unit({"unit_id": "T-903", "unit_name": "X",
                                                "service": "火箭军", "personnel": 100}),
                     "军种")
        expect_error("编号含空格被拒",
                     lambda: admin.upsert_unit({"unit_id": "T 903", "unit_name": "X",
                                                "service": "陆军", "personnel": 100}),
                     "空格")
        expect_error("缺编号被拒",
                     lambda: admin.upsert_unit({"unit_name": "X", "service": "陆军",
                                                "personnel": 100}), "编号")
        expect_error("战备状态乱写被拒",
                     lambda: admin.upsert_unit({"unit_id": "T-903", "unit_name": "X",
                                                "service": "陆军", "personnel": 100,
                                                "readiness": "特级"}), "战备")
        expect_error("给不存在的单位加装备被拒",
                     lambda: admin.upsert_equipment({"unit_id": "NOPE", "model": "99A",
                                                     "count": 1}), "不存在")
        expect_error("「新增装备」撞已有机型被拒（不能悄悄覆盖）",
                     lambda: admin.upsert_equipment({"unit_id": "T-902", "model": "99A",
                                                     "count": 1}, create_only=True),
                     "已有型号")
        with con() as c:
            check("被拒后原数量没被改掉",
                  rows(c, "SELECT count FROM equipment WHERE unit_id='T-902'"
                          " AND model='99A'")[0]["count"] == 10)
        expect_error("编号被占用被拒",
                     lambda: admin.upsert_unit({"unit_id": "A-001", "unit_name": "抢编号",
                                                "service": "陆军", "personnel": 1},
                                               orig_id="T-902"), "已被占用")
        expect_error("删不存在的单位被拒",
                     lambda: admin.delete_unit("NOPE"), "不存在")
        expect_error("改不存在的资源被拒",
                     lambda: admin.set_resource("nope", 5), "没有这项")
        with con() as c:
            check("被拒的操作一条都没落库",
                  not rows(c, "SELECT 1 FROM units WHERE unit_id='T-903'"))

        # 错误里的 field 必须是前端能认的英文键。
        # 早先这里返回的是"人员"这种中文标签，前端按 data-field="personnel" 找输入框
        # 永远找不到，于是"字段标红"这个功能静默失效 —— 消息对、位置错，最难发现。
        print("\n[5b] 错误的 field 必须是前端能用的键名")
        def field_of(fn):
            try:
                fn()
            except ForceError as e:
                return e.field
            return "(未报错)"
        cases = [
            ("人员越界 → personnel",
             lambda: admin.upsert_unit({"unit_id": "T-904", "unit_name": "X",
                                        "service": "陆军", "personnel": -5}),
             "personnel"),
            ("军种乱写 → service",
             lambda: admin.upsert_unit({"unit_id": "T-904", "unit_name": "X",
                                        "service": "火箭军", "personnel": 1}),
             "service"),
            ("缺编号 → unit_id",
             lambda: admin.upsert_unit({"unit_name": "X", "service": "陆军",
                                        "personnel": 1}), "unit_id"),
            ("战备乱写 → readiness",
             lambda: admin.upsert_unit({"unit_id": "T-904", "unit_name": "X",
                                        "service": "陆军", "personnel": 1,
                                        "readiness": "特级"}), "readiness"),
            ("装备数量越界 → count",
             lambda: admin.upsert_equipment({"unit_id": "T-902", "model": "X",
                                             "count": -1}), "count"),
            ("容量越界 → capacity",
             lambda: admin.set_resource("transport", -1), "capacity"),
        ]
        for name, fn, want in cases:
            got = field_of(fn)
            check(name, got == want, f"得到 {got!r}")

        # ── 6. 共享资源容量 ───────────────────────────────
        print("\n[6] 共享资源容量")
        r = admin.set_resource("transport", 60)
        with con() as c:
            cap = rows(c, "SELECT capacity FROM resources WHERE res_key='transport'")[0]
        cfg = json.loads((tmp / "constraints.json").read_text(encoding="utf-8"))
        check("resources 表已更新", cap["capacity"] == 60)
        check("constraints.json 已回写", cfg["resource_cap"]["transport"] == 60)
        check("其它字段没被改坏",
              cfg.get("force_cap") == 30000 and cfg.get("per_service_ok", True))
        expect_error("容量负数被拒", lambda: admin.set_resource("transport", -1), "容量")

        # ── 7. 导入 ───────────────────────────────────────
        print("\n[7] JSON 导入")
        imp = admin.import_units([
            {"id": "N-901", "单位": "测试支队", "军种": "海军", "兵种": "水面舰艇",
             "人员": 1200, "驻地": "南屿军港", "战备状态": "一级",
             "主要装备": [{"型号": "052D", "类型": "驱逐舰", "数量": 2}]},
            {"id": "A-001", "单位": "第1合成旅（改）", "军种": "陆军", "兵种": "合成兵种",
             "人员": 4300, "驻地": "东山基地", "战备状态": "一级",
             "主要装备": [{"型号": "99A", "类型": "主战坦克", "数量": 60}]},
        ], mode="merge")
        check("merge 新增 1 个", imp["created"] == 1, str(imp))
        check("merge 更新 1 个", imp["updated"] == 1)
        with con() as c:
            check("A-001 人数已改成 4300",
                  rows(c, "SELECT personnel FROM units WHERE unit_id='A-001'"
                          )[0]["personnel"] == 4300)
            check("A-001 装备整份替换成 1 条",
                  len(rows(c, "SELECT 1 FROM equipment WHERE unit_id='A-001'")) == 1)
            navy_n = rows(c, "SELECT personnel_total FROM v_service_total"
                             " WHERE service='海军'")[0]["personnel_total"]
        check("海军人数 +1200",
              navy_n == baseline["海军"]["personnel_total"] + 1200, f"{navy_n}")

        with con() as c:
            n_before = rows(c, "SELECT COUNT(*) c FROM units WHERE service='海军'")[0]["c"]
        rep = admin.import_units([
            {"id": "N-911", "单位": "替换后唯一支队", "军种": "海军", "人员": 800,
             "驻地": "南屏军港", "战备状态": "二级", "主要装备": []},
        ], mode="replace")
        with con() as c:
            n_after = rows(c, "SELECT COUNT(*) c FROM units WHERE service='海军'")[0]["c"]
            navy_now = rows(c, "SELECT personnel_total FROM v_service_total"
                               " WHERE service='海军'")[0]["personnel_total"]
            army_ok = rows(c, "SELECT COUNT(*) c FROM units WHERE service='陆军'")[0]["c"]
        check(f"replace 清掉海军原有 {n_before} 个", n_after == 1, f"剩 {n_after}")
        check("replace 后海军人数 = 800", navy_now == 800)
        check("replace 不碰其它军种", army_ok > 0)
        check("replace 返回删除数", rep["deleted"] == n_before, str(rep["deleted"]))

        # 原子性：一批里有一条坏的，整批都不能落库
        with con() as c:
            snapshot = rows(c, "SELECT unit_id, personnel FROM units ORDER BY unit_id")
        expect_error("导入含非法行 → 整批拒绝",
                     lambda: admin.import_units([
                         {"id": "A-990", "单位": "半批好数据", "军种": "陆军",
                          "人员": 500},
                         {"id": "A-991", "单位": "坏数据", "军种": "陆军",
                          "人员": "很多"},
                     ], mode="merge"), "整数")
        with con() as c:
            after = rows(c, "SELECT unit_id, personnel FROM units ORDER BY unit_id")
        check("原子性：一条不合格 → 整批一条没进", snapshot == after)
        expect_error("导入空数组被拒", lambda: admin.import_units([]), "不能为空")
        expect_error("导入重复编号被拒",
                     lambda: admin.import_units([
                         {"id": "A-992", "单位": "甲", "军种": "陆军", "人员": 1},
                         {"id": "A-992", "单位": "乙", "军种": "陆军", "人员": 2}]),
                     "重复")
        expect_error("导入方式乱写被拒",
                     lambda: admin.import_units(
                         [{"id": "A-993", "单位": "甲", "军种": "陆军", "人员": 1}],
                         mode="覆盖"), "方式")
        check("军种可省略（按界面选的补）",
              admin.import_units([{"id": "A-994", "单位": "省略军种", "人员": 300}],
                                 mode="merge", default_service="陆军")["created"] == 1)

        # ── 8. seed 回写 ──────────────────────────────────
        print("\n[8] seed 回写（重建库不丢改动）")
        for sv, fname in (("陆军", "army.json"), ("海军", "navy.json")):
            seed = json.loads((tmp / "seed" / fname).read_text(encoding="utf-8"))
            with con() as c:
                db_units = rows(c, "SELECT unit_id, unit_name, personnel FROM units"
                                   " WHERE service=? ORDER BY unit_id", (sv,))
            same = (len(seed) == len(db_units)
                    and all(s["id"] == d["unit_id"] and s["人员"] == d["personnel"]
                            for s, d in zip(seed, db_units)))
            check(f"{fname} 与库一致", same,
                  f"seed {len(seed)} 条 / 库 {len(db_units)} 条")
        seed_army = json.loads((tmp / "seed" / "army.json").read_text(encoding="utf-8"))
        check("新加的部队写进了 seed",
              any(u["id"] == "T-902" for u in seed_army))
        check("改过的人数是新值",
              any(u["id"] == "A-001" and u["人员"] == 4300 for u in seed_army))
        eq_ok = False
        for u in seed_army:
            if u["id"] == "T-902":
                eq_ok = (len(u["主要装备"]) == 2
                         and {e["型号"] for e in u["主要装备"]} == {"99A", "PLZ-05"})
        check("装备也写回了 seed", eq_ok)

        # ── 9. 真·重建库，改动仍在 ────────────────────────
        print("\n[9] 重建库后改动仍在（双写的真正意义）")
        import db.build_db as B
        B.DB_PATH = db
        B.SEED_DIR = tmp / "seed"
        B.build()
        with con() as c:
            check("重建后新单位还在",
                  bool(rows(c, "SELECT 1 FROM units WHERE unit_id='T-902'")))
            check("重建后改动过的人数还是 4300",
                  rows(c, "SELECT personnel FROM units WHERE unit_id='A-001'"
                          )[0]["personnel"] == 4300)
            check("重建后装备明细还在",
                  len(rows(c, "SELECT 1 FROM equipment WHERE unit_id='T-902'")) == 2)
            check("重建后变更日志没被清掉",
                  len(rows(c, "SELECT 1 FROM force_change_log")) > 0)

        # ── 10. 变更日志 ──────────────────────────────────
        print("\n[10] 变更日志")
        log = admin.changelog(100)
        acts = {x["action"] for x in log}
        check("记到了 create/update/delete/import",
              {"create", "update", "delete", "import"} <= acts, str(sorted(acts)))
        check("日志带改动前后值",
              any(x.get("before") and x.get("after") for x in log))
        check("最新一条在最前",
              log[0]["ts"] >= log[-1]["ts"])

        # ── 11. 导出可回灌（闭环） ────────────────────────
        print("\n[11] 导出→再导入 闭环")
        exp = admin.export_units("陆军")
        check("导出带中文键（与 seed 同格式）",
              all({"id", "单位", "军种", "人员", "主要装备", "驻地"} <= set(u)
                  for u in exp), f"{len(exp)} 条")
        before_cnt = len(exp)
        again = admin.import_units(exp, mode="merge")
        check("导出的东西能原样导回来，且全是更新不是新增",
              again["created"] == 0 and again["updated"] == before_cnt,
              f"created={again['created']} updated={again['updated']}")

        print()
        print("=" * 62)
        print(f"  合计 {len(PASS) + len(FAIL)} 项，失败 {len(FAIL)} 项")
        if FAIL:
            for f in FAIL:
                print(f"    ✗ {f}")
        print("=" * 62)
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                          # noqa: BLE001
        pass
    sys.exit(main())
