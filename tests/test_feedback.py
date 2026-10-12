# -*- coding: utf-8 -*-
"""
test_feedback.py —— 答案反馈（点赞/点踩）与"answer 串轮"修复的回归测试

两件事放一个文件里，因为它们其实是一根链条：
  · 串轮：界面把上一轮的回答当成这一轮的结论 → 用户看到的就是错的
  · 反馈：基于"看到的那条回答"评价 → 如果气泡本身串了轮，反馈就锚到了错的轮次，
          下一轮筹划拿到的改进线索也就跟着错。
所以必须先保证"哪一轮的哪句话"是对的，反馈才有意义。

关键设计：反馈相关的写操作**全程在副本库上跑**；只有串轮那条是只读地
检查真实库的不变量（它本来就是只读逻辑，造不出副作用）。

用法：python tests/test_feedback.py
"""
from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.graph import prompts as P                                   # noqa: E402
from src.tools.feedback import FeedbackError, FeedbackStore        # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


def expect_error(name: str, fn, contains: str = "") -> None:
    """只测成功路径的测试是假的安全感 —— 校验逻辑坏了它照样全绿。"""
    try:
        fn()
    except FeedbackError as e:
        ok = (contains in e.msg) if contains else True
        check(name, ok, e.msg if not ok else "")
        return
    except Exception as e:                                           # noqa: BLE001
        check(name, False, f"抛了别的异常：{type(e).__name__}: {e}")
        return
    check(name, False, "本该拒绝，却成功了")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="fb_test_"))
    try:
        shutil.copy(ROOT / "db" / "joint_ops.db", tmp / "joint_ops.db")
        fb = FeedbackStore(tmp / "joint_ops.db")
        fb.ensure_schema()

        # ── 1. 写入与覆盖 ────────────────────────────────
        print("\n[1] 写入：一轮只有一条记录，改主意就是覆盖")
        r1 = fb.save({"thread_id": "T-1", "turn_index": 2, "rating": -1,
                      "reasons": ["太笼统", "没说依据"],
                      "note": "第 3 步只写了\"择机出动\"，看不出到底哪一天",
                      "question": "联合登陆作战", "answer": "已生成方案",
                      "route": "full_chain", "plan_version": 1, "has_plan": True})
        check("点踩能写进去", r1["id"] > 0 and r1["rating"] == -1, str(r1["id"]))
        check("原因按顺序留下", r1["reasons"] == ["太笼统", "没说依据"])

        r2 = fb.save({"thread_id": "T-1", "turn_index": 2, "rating": 1, "reasons": []})
        check("改主意会覆盖而不是新增", r2["id"] == r1["id"],
              f"{r1['id']} → {r2['id']}")
        items = fb.for_thread("T-1")
        check("该会话只有 1 条记录", len(items) == 1, f"{len(items)} 条")
        check("评价已改成赞", items and items[0]["rating"] == 1)
        check("覆盖后旧原因被清掉", items and items[0]["reasons"] == [])

        fb.save({"thread_id": "T-1", "turn_index": 1, "rating": 1, "reasons": []})
        check("同一会话不同轮次各存一条", len(fb.for_thread("T-1")) == 2)
        check("别的会话互不干扰", fb.for_thread("T-2") == [])

        # ── 2. 校验：该拒的必须拒 ────────────────────────
        print("\n[2] 校验：脏输入一条都不许进库")
        expect_error("评分为 0 被拒",
                     lambda: fb.save({"thread_id": "T-1", "turn_index": 9,
                                      "rating": 0}), "1（赞）或 -1")
        expect_error("点踩不给原因被拒",
                     lambda: fb.save({"thread_id": "T-1", "turn_index": 9,
                                      "rating": -1, "reasons": []}), "至少选一条")
        expect_error("乱编的原因标签被拒",
                     lambda: fb.save({"thread_id": "T-1", "turn_index": 9,
                                      "rating": -1, "reasons": ["我觉得不行"]}),
                     "未知的原因标签")
        expect_error("会话号为空被拒",
                     lambda: fb.save({"thread_id": "", "turn_index": 1,
                                      "rating": 1}), "不能为空")
        expect_error("轮次不是整数被拒",
                     lambda: fb.save({"thread_id": "T-1", "turn_index": "第三轮",
                                      "rating": 1}), "必须是整数")
        expect_error("轮次为 0 被拒",
                     lambda: fb.save({"thread_id": "T-1", "turn_index": 0,
                                      "rating": 1}), "必须在 1~")
        check("被拒的操作没有落库", not fb.for_thread("T-2"))

        # 真正要保证的是"去重"：同一原因被点两次不该算两条，
        # 否则统计出来的分布全是注水的（数量上限只是防呆，REASONS 本身只有 8 项）。
        fb.save({"thread_id": "T-1", "turn_index": 8, "rating": -1,
                 "reasons": ["太笼统", "太笼统", "太笼统"]})
        one = [x for x in fb.for_thread("T-1") if x["turn_index"] == 8]
        check("重复选同一原因只算一次",
              len(one) == 1 and one[0]["reasons"] == ["太笼统"], str(one))
        fb.save({"thread_id": "T-1", "turn_index": 8, "rating": 1, "reasons": []})

        # ── 3. 摘要与"学习"注入 ──────────────────────────
        print("\n[3] 摘要：反馈要能变成下一轮可以照做的改进线索")
        fb.save({"thread_id": "T-3", "turn_index": 1, "rating": -1,
                 "reasons": ["太笼统"], "note": "只说增强火力，没说哪个旅上"})
        fb.save({"thread_id": "T-4", "turn_index": 1, "rating": -1,
                 "reasons": ["太笼统", "时间线不可行"]})
        s = fb.summary()
        check("统计总数正确", s["total"] >= 4, f"total={s['total']}")
        check("赞/踩分别计数", s["up"] >= 2 and s["down"] >= 2,
              f"up={s['up']} down={s['down']}")
        by = {x["reason"]: x["count"] for x in s["by_reason"]}
        check("按原因聚合（太笼统应出现 2 次）", by.get("太笼统") == 2, str(by))
        d = s["digest"]
        check("摘要非空", bool(d.strip()))
        check("摘要含被指出的问题与次数", "太笼统（2 次）" in d)
        check("摘要含用户原话", "没说哪个旅上" in d)
        check("摘要写明不放宽硬规则", "不放宽任何硬规则" in d and "sql:/env:" in d)

        # ── 4. 备注里的花括号原样保留，且不能炸掉筹划链路 ──
        print("\n[4] 提示词注入：用户备注里的花括号既不能炸链路，也不能被改样")
        fb.save({"thread_id": "T-5", "turn_index": 1, "rating": -1,
                 "reasons": ["其它"], "note": "模板里 {plans} 那个位置是空的"})
        d = fb.digest()
        check("备注里的花括号原样保留", "{plans}" in d and "{{plans}}" not in d,
              d[-140:])
        try:
            rendered = P.COMMANDER_USER.format(
                user_input="x", ticket="{}", base_constraints="{}",
                constraints="{}", feedback=d)
            check("渲染后仍是原样（没被转义成双花括号）",
                  "{plans}" in rendered and "{{plans}}" not in rendered)
            P.ARBITRATE_USER.format(user_input="x", constraints="{}", conflicts="[]",
                                    plans="[]", env_view="-", feedback=d)
            check("带反馈渲染提示词不抛异常", True)
        except Exception as e:                                       # noqa: BLE001
            check("带反馈渲染提示词不抛异常", False, f"{type(e).__name__}: {e}")
        try:
            P.COMMANDER_USER.format(user_input="x", ticket="{}", base_constraints="{}",
                                    constraints="{}", feedback="")
            check("无反馈时（空串）也能渲染", True)
        except Exception as e:                                       # noqa: BLE001
            check("无反馈时（空串）也能渲染", False, f"{type(e).__name__}: {e}")

        # 没有反馈时应返回空串（调用方据此跳过注入，而不是塞一句"暂无"）
        empty = FeedbackStore(tmp / "joint_ops.db")
        con = sqlite3.connect(str(tmp / "joint_ops.db"))
        con.execute("DELETE FROM answer_feedback")
        con.commit()
        con.close()
        empty.invalidate()
        check("无反馈时摘要为空串", empty.digest() == "", repr(empty.digest()[:40]))

        # ── 5. 摘要缓存写后失效 ──────────────────────────
        print("\n[5] 缓存：写入之后摘要必须立刻变新")
        fb2 = FeedbackStore(tmp / "joint_ops.db")
        fb2.digest()                       # 先把缓存填上
        before = fb2.digest()
        fb2.save({"thread_id": "T-6", "turn_index": 1, "rating": -1,
                  "reasons": ["格式难读"]})
        after = fb2.digest()
        check("写入后摘要立即更新", before != after and "格式难读" in after)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ── 6. 串轮修复：只读检查真实库的不变量 ──────────────
    print("\n[6] 串轮：规划轮次不许携带上一轮那句快问回答")
    try:
        import server as S
        asyncio.run(_check_turns(S))
    except Exception as e:                                           # noqa: BLE001
        check("串轮不变量检查可运行", False, f"{type(e).__name__}: {e}")

    # ── 6b. 并发取摘要：绝不能有人拿到空串 ────────────────
    print("\n[6b] 并发取改进线索：两个请求同时来，不许有一个拿到空串")
    _check_digest_concurrency(fb)

    # ── 7. 中断轮次：失败的一轮不许冒充成功 ──────────────
    print("\n[7] 中断轮次：失败的一轮不许顶着上一轮的方案冒充成功")
    try:
        asyncio.run(_check_failed_rounds())
    except Exception as e:                                           # noqa: BLE001
        check("中断轮次标记检查可运行", False, f"{type(e).__name__}: {e}")

    print(f"\n合计 {len(PASS) + len(FAIL)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗", f)
    return 1 if FAIL else 0


def _check_digest_concurrency(fb) -> None:
    """两个请求同时要"改进线索"时，谁都不许拿到空串。

    真出过：重入保护用的是普通实例标志 `self._building`，于是 Web 服务
    两个请求并发时，后到的那个被误判成"正在递归"→ 返回空串 →
    界面上"agent 学到了什么"时有时无（表现为界面测试偶发 3 项失败，
    而且单跑必过、连跑/并跑才挂 —— 这种 flaky 最难查）。
    另外每次读都 `executescript(建表)` 会申请写锁，并发下同样把摘要打成空串。

    自带临时库：不受前面用例的清理影响（前几段会把临时库的反馈清掉，
    直接复用 fb 会因为"本来就没反馈"而假失败）。
    """
    tmp = Path(tempfile.mkdtemp(prefix="fb_conc_"))
    try:
        shutil.copy(ROOT / "db" / "joint_ops.db", tmp / "joint_ops.db")
        store = FeedbackStore(tmp / "joint_ops.db")
        store.ensure_schema()
        store.save({"thread_id": "CONC-1", "turn_index": 1, "rating": -1,
                    "reasons": ["太笼统"], "note": "并发验证用"})

        got: List[str] = []
        lock = threading.Lock()

        def worker() -> None:
            v = store.digest()
            with lock:
                got.append(v)

        store.invalidate()                   # 让 8 个线程撞在同一时刻重建
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        empties = [v for v in got if not v]
        check("8 个线程并发取摘要，全部拿到内容（无空串）",
              len(got) == 8 and not empties,
              f"拿到 {len(got)} 条，其中空串 {len(empties)} 条")
        check("并发结果一致", len(set(got)) == 1, f"{len(set(got))} 种不同内容")

        # ── 决定性验证（不靠时序撞运气）──────────────────────
        # "8 个线程同时来"能不能撞出旧 bug 全看运气。这里用手把手的握手，
        # 造出"线程A正在构建摘要时，线程B也来要摘要"这个确定场景：
        #   旧实现（实例标志）：B 被误判为重入 → 拿到空串
        #   新实现（线程局部）：B 照常构建 → 拿到完整内容
        # 只把 _stats 换成"第一下会停住"的版本，不改任何业务逻辑。
        store.invalidate()
        entered = threading.Event()
        release = threading.Event()
        first_call = {"seen": False}
        real_stats = store._stats

        def handshake_stats():
            if not first_call["seen"]:
                first_call["seen"] = True
                entered.set()
                release.wait(10)          # 等 B 进来后再放行，制造重叠窗口
            return real_stats()

        store._stats = handshake_stats
        result_a: List[str] = []
        result_b: List[str] = []

        ta = threading.Thread(target=lambda: result_a.append(store.digest()))
        ta.start()
        got_entered = entered.wait(10)
        check("已造出『A 正在构建摘要』的确定场景", got_entered)
        tb = threading.Thread(target=lambda: result_b.append(store.digest()))
        tb.start()
        tb.join(timeout=10)
        release.set()
        ta.join(timeout=10)
        store._stats = real_stats

        check("A 构建中时 B 也来取 → B 不该被当成重入而拿到空串",
              bool(result_b) and bool(result_b[0]),
              f"B 拿到 {len(result_b[0]) if result_b else 0} 字")
        check("A 自己也拿到完整内容", bool(result_a) and bool(result_a[0]),
              f"A 拿到 {len(result_a[0]) if result_a else 0} 字")
        check("并发也能拿到正确的统计口径",
              bool(result_b) and "不放宽" in result_b[0],
              (result_b[0][:40] if result_b else ""))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _check_turns(S) -> None:
    """遍历真实库里每个会话的每一轮，断言：
    route=full_chain 的轮次（且不是在澄清）不该带 answer ——
    带上了就说明它继承的是上一轮的内容，界面必然串轮。"""
    async with S.async_checkpointer() as saver:
        rows = S._db_rows("SELECT thread_id, COUNT(*) c FROM checkpoints"
                          " GROUP BY thread_id ORDER BY MAX(rowid) DESC LIMIT 20")
        if not rows:
            check("库里有会话可供检查", False, "一个会话都没有")
            return
        bad, checked, two_plus = [], 0, 0
        for r in rows:
            turns = await S._collect_turns(saver, r["thread_id"])
            if len(turns) >= 2:
                two_plus += 1
            for i, t in enumerate(turns, 1):
                checked += 1
                if t["route"] == "full_chain" and t["answer"] \
                        and not (t["result"] or {}).get("clarify_question"):
                    bad.append(f"{r['thread_id']}#{i}")
        check(f"扫描 {checked} 轮全部合规", not bad, "违规：" + ",".join(bad[:5]))
        check("存在多轮会话（否则这条检查是空跑）", two_plus > 0,
              f"{two_plus} 个多轮会话")
        # 反过来确认：快问轮次的回答必须保留（别为了修串轮把真回答也清掉）
        kept = 0
        for r in rows[:20]:
            for t in await _collect_turns_all(S, saver, r["thread_id"]):
                if t["route"] == "single_domain" and t["answer"]:
                    kept += 1
        check("单域快问的回答未被误清", kept > 0, f"{kept} 轮保留了回答")


async def _collect_turns_all(S, saver, tid):
    return await S._collect_turns(saver, tid)


async def _check_failed_rounds() -> None:
    """一轮筹划要是崩在中间，它的快照里 final_plan / validation 全是
    **上一轮继承来的** —— 不打标，界面就把"失败的一轮"显示成"出了方案、终验 PASS"。
    用户明明看到过报错，回头翻历史却成了成功，这是最伤信任的一类错。

    全程在副本库上做：造一条假的失败记录，绝不动用户真实的失败台账。
    """
    import server as S
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    tmp = Path(tempfile.mkdtemp(prefix="runlog_test_"))
    db_copy = tmp / "joint_ops.db"
    shutil.copy(ROOT / "db" / "joint_ops.db", db_copy)
    orig_path = S.RUNLOG.db_path
    S.RUNLOG.db_path = db_copy
    try:
        rows = S._db_rows("SELECT thread_id, COUNT(*) c FROM checkpoints"
                          " GROUP BY thread_id HAVING c > 3"
                          " ORDER BY MAX(rowid) DESC LIMIT 8")
        async with AsyncSqliteSaver.from_conn_string(str(db_copy)) as saver:
            tid, turns = None, None
            for r in rows:
                ts = await _collect_turns_all(S, saver, r["thread_id"])
                if len(ts) >= 2 and any(x.get("has_new_plan") for x in ts):
                    tid, turns = r["thread_id"], ts
                    break
            if tid is None:
                check("找到可用于验证的多轮会话", False, "库中没有含方案的多轮会话")
                return
            check("找到可用于验证的多轮会话", True, f"{tid} · {len(turns)} 轮")

            before = [dict(x) for x in turns]
            n = len(turns)
            # 模拟"最后一轮崩了"
            S.RUNLOG.record_failure(tid, turns[-1]["text"], "NameError: 模拟中断")
            after = await _collect_turns_all(S, saver, tid)

            check("轮次数不变（失败轮次仍然占一轮）", len(after) == n,
                  f"{n} → {len(after)}")
            last = after[-1]
            check("最后一轮被标为中断", last.get("failed") is True,
                  str(last.get("failed")))
            check("中断轮次不再挂着终验 PASS", last.get("validation") == {},
                  str(last.get("validation")))
            check("中断轮次不算'本轮产出了方案'",
                  last.get("has_new_plan") is False)
            check("中断轮次不再列方案章节", not last.get("sections"))
            check("中断轮次带上了失败原因", "NameError" in (last.get("error") or ""),
                  (last.get("error") or "")[:40])
            check("result 里的一致性字段也被抹掉（防前端绕回去读）",
                  (last.get("result") or {}).get("has_new_plan") is False
                  and (last.get("result") or {}).get("validation") == {})

            # 别的轮次一点都不能动 —— 抹过头会把真历史也毁掉
            same = all(
                before[i].get("has_new_plan") == after[i].get("has_new_plan")
                and before[i].get("sections") == after[i].get("sections")
                and (before[i].get("validation") or {}).get("code")
                    == (after[i].get("validation") or {}).get("code")
                for i in range(n - 1))
            check("前面几轮完全没被动过", same)

            # 文本对不上（崩在首节点之前，连 checkpoint 都没留）→ 补占位轮
            S.RUNLOG.record_failure(tid, "这句问法从未出现过", "RuntimeError: 首节点就崩")
            after2 = await _collect_turns_all(S, saver, tid)
            ph = [x for x in after2 if x.get("no_trace")]
            check("没留下轨迹的失败也补了一条占位轮", len(ph) == 1,
                  f"{len(ph)} 条")
            check("占位轮同样标为中断且带原因",
                  bool(ph) and ph[0].get("failed") is True
                  and "首节点" in (ph[0].get("error") or ""))
            check("占位轮不携带任何方案", bool(ph) and ph[0].get("sections") == [])

            # 串台检查：别的会话不该被这次失败污染
            other = next((r["thread_id"] for r in rows if r["thread_id"] != tid), None)
            if other:
                ot = await _collect_turns_all(S, saver, other)
                check("失败标记不会串到别的会话",
                      not any(x.get("failed") for x in ot))
    finally:
        S.RUNLOG.db_path = orig_path
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
