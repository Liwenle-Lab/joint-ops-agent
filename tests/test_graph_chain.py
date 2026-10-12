# -*- coding: utf-8 -*-
"""
test_graph_chain.py —— **真跑一遍筹划链路**的回归测试

为什么必须有这个文件（血的教训，2026-09-24）：
    那天我给"指挥层/仲裁层注入历史反馈"加了 `FEEDBACK.digest()`，
    顺手在参谋节点里清空 answer。两处都是**节点函数体内部**的代码：
      · graph.py 漏了 `from src.tools.feedback import FEEDBACK`
        → 真模型下一提问就 `NameError`，方案出不来；
      · 清空 answer 放进了**并行三份**的 staff 节点
        → 联合方案一轮就 `InvalidUpdateError: At key 'answer'`。
    而当时那 202 项测试（单测 + 界面测试）**没有一项真的调用过图**，
    所以全绿 —— 用户却一用就炸。

    教训：只要动的是**节点函数内部**，单测/界面测试再全也保不了；必须有一条
    "从 intent 一路走到 validate"的真链路测试。本文件就是那条。

跑法：python tests/test_graph_chain.py
    强制 MOCK（不花钱、秒级），但走的是与真实模型**完全相同的图与节点**。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ★ 必须在导入 src.llm.client 之前设：ROUTER 在模块导入时就读环境变量定档。
#   空 Key → MOCK，只跑链路不产生真实推理开销（兵力数字仍来自真实 SQLite）。
os.environ["LLM_API_KEY"] = ""
os.environ.pop("TAMPER_FIGURE", None)
os.environ.pop("MOCK_FAULT", None)

import src.graph.graph as G                                          # noqa: E402
from src.graph.graph import ROUTER, build_graph                       # noqa: E402
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver          # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail else ""))


def init_state(text: str) -> dict:
    """与服务端 _run_graph / run.py 完全一致的初始 State —— 别自己另立一套，
    否则测过的是"我以为的入口"而不是"真的入口"。"""
    return {"user_input": text, "retry_count": 0,
            "staff_reports": [], "trace": [], "api_calls": {}}


async def run_once(text: str, saver=None, thread_id: str = "") -> dict:
    cfg = {"recursion_limit": 50}
    g = build_graph(saver) if saver is not None else G.GRAPH
    if thread_id:
        cfg["configurable"] = {"thread_id": thread_id}
    return await g.ainvoke(init_state(text), config=cfg)


async def run_captured(text: str, saver=None, thread_id: str = "") -> tuple:
    """跑一轮并把它打印到 stdout 的东西截下来。

    ★ 为什么要截：`feedback_digest()` 有兜底（反馈坏了也不能没方案），
      它会 print 一行 `[warn] 历史反馈读取失败…`。兜底本身是对的，
      但**兜底被触发 = 某个功能在静默失效** —— 正是我们要抓的东西。
      所以这里把输出抓下来，断言这行警告**没出现过**。
      没有这条断言，兜底会把"漏 import""读库失败"通通吃掉，测试照样全绿
      （这个坑我当场踩过一次：把 import 注释掉，24 项依然全过）。
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = await run_once(text, saver, thread_id)
    return out, buf.getvalue()


def check_no_swallow(name: str, captured: str) -> None:
    bad = "历史反馈读取失败" in captured
    check(name, not bad, captured.strip()[-120:] if bad else "")


async def main() -> int:
    print(f"模式：{'MOCK' if ROUTER.mocked else '真实模型'}")
    if not ROUTER.mocked:
        # 不硬拦，但要吼一声：真实模型下这个文件会慢且花钱，而且断言依赖 Mock 的确定性
        print("  ⚠ 当前是真实模型档位，本测试预期走 MOCK；结果可能不稳定、且产生调用费用")

    tmp = Path(tempfile.mkdtemp(prefix="chain_test_"))
    try:
        db = tmp / "joint_ops.db"
        shutil.copy(ROOT / "db" / "joint_ops.db", db)

        # 反馈单例指到副本库：反馈注入要测，但绝不能把测试记录写进用户的反馈表。
        # ★ 改 db_path 而不是往 graph 模块里塞一个新对象 —— 后者会把"graph 漏 import"
        #   这个 bug 顺手补掉，测试就失去咬合力（实测：注释掉 import 仍 24 项全过）。
        from src.tools.feedback import FEEDBACK
        FEEDBACK.db_path = db
        FEEDBACK.ensure_schema()
        FEEDBACK.invalidate()
        check("graph.py 确实导入了 FEEDBACK（漏了会在真模型下 NameError）",
              hasattr(G, "FEEDBACK"))

        # ── 1. 单域快通道：answer 必须由 staff 写回 ────────────
        print("\n[1] 单域快问：链路能跑完，且 answer 是本轮的")
        out, cap1 = await run_captured("陆军现在多少人，多少装备？")
        check("没有抛异常（节点内代码可执行）", True)
        check_no_swallow("单域轮次兜底没被触发", cap1)
        check("路由 = single_domain", out.get("route") == "single_domain",
              str(out.get("route")))
        ans = out.get("answer") or ""
        check("answer 非空（staff 写回了）", bool(ans), repr(ans[:40]))
        check("answer 里带着本轮查到的兵力数字", "15900" in ans, repr(ans[:60]))
        check("终验通过", (out.get("validation") or {}).get("ok") is True,
              str((out.get("validation") or {}).get("code")))

        # ── 2. 全链路：这是出过 InvalidUpdateError 的那条 ──────
        print("\n[2] 联合方案（三参谋并行）：不许再抛 InvalidUpdateError")
        try:
            out2, cap2 = await run_captured("陆海空联合登陆作战，5天内完成投送")
            raised = ""
        except Exception as e:                                       # noqa: BLE001
            out2, cap2, raised = {}, "", f"{type(e).__name__}: {e}"
        check("全链路跑通没抛异常", not raised, raised[:120])
        # 这条就是当初能抓住 NameError 的：指挥层跑不通时兜底会留痕
        check_no_swallow("全链路兜底没被触发（触发=节点里有异常被吞）", cap2)
        check("路由 = full_chain", out2.get("route") == "full_chain",
              str(out2.get("route")))
        check("产出了联合方案", bool(out2.get("final_plan")))
        check("方案有实质章节", len(out2.get("final_plan") or {}) >= 4,
              f"{len(out2.get('final_plan') or {})} 节")
        check("终验通过", (out2.get("validation") or {}).get("ok") is True,
              str((out2.get("validation") or {}).get("code")))
        fb_tr2 = [t for t in (out2.get("trace") or [])
                  if isinstance(t, dict) and t.get("tag") == "feedback"]
        check("指挥层确实执行了反馈取用（无反馈也应留一条）", bool(fb_tr2),
              str(fb_tr2[-1].get("msg"))[:40] if fb_tr2 else "无 feedback 记录")

        # ── 3. ★ 串轮不变量：规划轮次的 answer 必须为空 ────────
        print("\n[3] 串轮不变量：规划轮次不许继承上一轮快问的回答")
        check("全链路轮次的 answer 为空", (out2.get("answer") or "") == "",
              repr((out2.get("answer") or "")[:60]))

        # ── 4. ★ 用户实际踩到的路径：同会话「先快问、后要方案」 ──
        print("\n[4] 同会话两轮：第1轮问兵力，第2轮要方案（用户截图里那条）")
        # 第2轮的问法用"陆海空…联合登陆"：MOCK 的路由是**关键词规则**，
        # 用户原话"五天后在东海的布防方案"里没有联合类关键词，会被上一轮上下文里的
        # "陆军"带成单域（真实模型判的是 full_chain，见用户截图）。
        # 本项要守的是"规划轮次不许继承上一轮 answer"这个**不变量**，
        # 它只取决于路由结果、与措辞无关，所以用 MOCK 能稳定判定的说法即可。
        tid = "CHAIN-TEST-1"
        async with AsyncSqliteSaver.from_conn_string(str(db)) as saver:
            r1 = await run_once("陆军现在多少人，多少装备？", saver, tid)
            r2 = await run_once("陆海空联合登陆作战，5天内完成投送", saver, tid)
        a1, a2 = r1.get("answer") or "", r2.get("answer") or ""
        check("第1轮走单域快通道且答出了数字", "15900" in a1, repr(a1[:50]))
        check("第2轮被判为联合筹划", r2.get("route") == "full_chain",
              str(r2.get("route")))
        check("第2轮 answer 不再等于第1轮回答",
              not (a2 and a2 == a1), f"round2={a2[:50]!r}")
        check("第2轮 answer 为空 → 界面回落到本轮方案摘要", a2 == "", repr(a2[:50]))
        check("第2轮确实产出了方案", bool(r2.get("final_plan")),
              f"v{r2.get('plan_version')}")

        # ── 5. 反馈注入不能把链路带崩（含脏输入）────────────────
        print("\n[5] 历史反馈注入：正常与脏输入都不许拖垮筹划")
        FEEDBACK.save({"thread_id": "CHAIN-TEST-2", "turn_index": 1, "rating": -1,
                       "reasons": ["太笼统", "没说依据"],
                       "note": "只说增强火力，没说哪个旅上；模板里 {plans} 是空的"})
        check("反馈摘要能生成（不递归卡死）", len(FEEDBACK.digest() or "") > 10)
        try:
            out3, cap3 = await run_captured("陆海空联合登陆作战，5天内完成投送")
            raised3 = ""
        except Exception as e:                                       # noqa: BLE001
            out3, cap3, raised3 = {}, "", f"{type(e).__name__}: {e}"
        check("带反馈时全链路仍跑通", not raised3, raised3[:120])
        check("带反馈时仍产出方案", bool(out3.get("final_plan")))
        check_no_swallow("带反馈时兜底没被触发（触发=功能静默失效）", cap3)
        fb_trace = [t for t in (out3.get("trace") or [])
                    if isinstance(t, dict) and t.get("tag") == "feedback"]
        check("流水线里能看到反馈已带入（不是静默生效）",
              bool(fb_trace) and "带入" in str(fb_trace[-1].get("msg", "")),
              str(fb_trace[-1].get("msg"))[:40] if fb_trace else "无 feedback 记录")

        # 兜底本身要有效：读反馈炸掉时不能把方案带走。这里临时换掉 FEEDBACK，
        # 用完必须还原 —— 否则后面的用例会静默跑在假对象上。
        # 用 getattr 取：万一 graph 漏了 import，这里要报"失败"而不是抛栈中断，
        # 否则后面的用例根本跑不到、汇总也打不出来，等于问题被藏起来。
        import src.graph.graph as GG
        orig = getattr(GG, "FEEDBACK", None)

        class _Boom:
            def digest(self, *a, **k):
                raise RuntimeError("模拟读库失败")

        if orig is None:
            check("读反馈炸了也不抛（宁可少一段线索，不能没方案）", False,
                  "graph 模块里没有 FEEDBACK —— 导入缺失，兜底无从验证")
        else:
            try:
                GG.FEEDBACK = _Boom()
                check("读反馈炸了也不抛（宁可少一段线索，不能没方案）",
                      GG.feedback_digest() == "")
            finally:
                GG.FEEDBACK = orig
            check("兜底验证后已还原 FEEDBACK", GG.FEEDBACK is orig)

        # ── 6. 领域外 / 需求不清：两条短路径也要能收尾 ──────────
        print("\n[6] 另外两条退出路径")
        o4 = await run_once("今天天气怎么样")
        check("领域外 → out_of_scope 且有拒答文本",
              o4.get("route") == "out_of_scope" and bool(o4.get("answer")),
              str(o4.get("route")))
        o5 = await run_once("那个事情怎么样了")
        check("需求不清 → need_clarify 且有澄清问句",
              o5.get("route") == "need_clarify"
              and bool(o5.get("clarify_question")),
              str(o5.get("route")))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 60}")
    print(f"合计 {len(PASS) + len(FAIL)} 项，失败 {len(FAIL)} 项")
    for n in FAIL:
        print(f"  ✗ {n}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
