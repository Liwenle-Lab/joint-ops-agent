# -*- coding: utf-8 -*-
"""
run.py —— 第一期最小闭环入口

用法：
    python run.py "陆军现在多少人，多少装备"
    python run.py --test          # 跑第一期验收用例集
"""
from __future__ import annotations
import asyncio
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.graph.graph import GRAPH, ROUTER           # noqa: E402

TRACE_DIR = ROOT / "trace"
TRACE_DIR.mkdir(parents=True, exist_ok=True)


async def run_query(text: str, thread_id: str = "") -> dict:
    # 二期：staff 节点已改 async，必须用 ainvoke 才会真并行；继续用 invoke 仍是串行。
    # 三期：带 thread_id 时走 checkpointer，恢复上一轮的槽位/约束/方案版本 → 增量重规划。
    state = {"user_input": text, "retry_count": 0,
             "staff_reports": [], "trace": [], "api_calls": {}}
    cfg = {"recursion_limit": 50}
    if thread_id:
        cfg["configurable"] = {"thread_id": thread_id}
        # 多轮：AsyncSqliteSaver 必须在 async 上下文内创建与关闭（连接绑事件循环）
        from src.graph.graph import async_checkpointer, build_graph
        async with async_checkpointer() as saver:
            out = await build_graph(saver).ainvoke(state, config=cfg)
    else:
        out = await GRAPH.ainvoke(state, config=cfg)
    # 落盘 trace
    ts = time.strftime("%Y%m%d_%H%M%S")
    (TRACE_DIR / f"session_{ts}.json").write_text(
        json.dumps({k: v for k, v in out.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return out


def show(out: dict) -> None:
    print("\n" + "─" * 60)
    print(f"路由      : {out.get('route')}")
    print(f"领域      : {out.get('domain')}")
    if out.get("clarify_question"):
        print(f"澄清      : {out['clarify_question']}")
    if out.get("answer"):
        print(f"\n【回答】\n{out['answer']}")
    if out.get("final_plan"):
        fp = out["final_plan"]
        print("\n【联合作战决心方案】")
        for k, v in fp.items():
            print(f"  · {k}: {v}")
    val = out.get("validation") or {}
    print(f"\n终验      : {val.get('code')} {'✅' if val.get('ok') else '❌'}")
    for v in val.get("violations", []):
        print(f"           - {v}")
    print(f"双档调用  : 主力 {ROUTER.counts.get('main', 0)} · 轻量 {ROUTER.counts.get('light', 0)}"
          f"   [{'MOCK' if ROUTER.mocked else '真实模型'}]")
    print(f"参谋产物  : {len(out.get('staff_reports') or [])} 份")
    if out.get("plan_version"):
        extra = ""
        if out.get("is_incremental"):
            extra = (f" · 增量（变更 {out.get('changed_keys') or '无'} → "
                     f"重算 {out.get('impacted') or []}，复用 {out.get('reused') or []}）")
        print(f"方案版本  : v{out['plan_version']}{extra}")
    print("─" * 60)


CASES = [
    ("T1 单域查询（判据1）", "陆军现在多少人，多少装备？"),
    ("T2 领域外拒答", "帮我写一份季度工作周报"),
    ("T3 全链路首案（判据2）", "陆海空联合登陆作战，5天内完成投送，请做联合筹划"),
    ("T4 海军单域查询", "海军有多少兵力？"),
    ("T5 需求不清", "那个事情怎么样了"),
]


def has_number(text: str, n: int, tol: int = 100) -> bool:
    """在回答文本里找数字 n。

    为什么不用 `str(n) in text`：真模型常写成「15,900」（千分位）或「1.59万」，
    字符串包含会误判成 FAIL——明明答对了却报失败。故统一转为数值比对。
    """
    t = re.sub(r"[,\s，]", "", text or "")
    if str(n) in t:
        return True
    for m in re.findall(r"(\d+(?:\.\d+)?)万", t):          # 1.59万
        if abs(float(m) * 10000 - n) < tol * 10:
            return True
    return False


async def run_tests() -> int:
    print(f"模式：{'MOCK（未配置 API Key）' if ROUTER.mocked else '真实模型'}")
    passed = 0
    for name, text in CASES:
        print(f"\n{'=' * 60}\n{name}\n输入：{text}")
        try:
            out = await run_query(text)
            show(out)
            ok = True
            if name.startswith("T1"):
                ok = (out.get("route") == "single_domain"
                      and (out.get("validation") or {}).get("ok") is True
                      and has_number(out.get("answer") or "", 15900))
            elif name.startswith("T2"):
                ok = out.get("route") == "out_of_scope"
            elif name.startswith("T3"):
                ok = (out.get("route") == "full_chain"
                      and (out.get("validation") or {}).get("ok") is True
                      and bool(out.get("final_plan")))
            elif name.startswith("T4"):
                ok = (out.get("route") == "single_domain"
                      and (out.get("validation") or {}).get("ok") is True)
            elif name.startswith("T5"):
                ok = out.get("route") in ("need_clarify", "single_domain", "full_chain")
            print(f"判定：{'PASS ✅' if ok else 'FAIL ❌'}")
            passed += 1 if ok else 0
        except Exception as e:                          # noqa: BLE001
            print(f"判定：FAIL ❌  异常：{type(e).__name__}: {e}")
    # T6 数字防篡改：注入一次篡改，validate 必须拦下并回退重跑，最终仍 PASS
    print(f"\n{'=' * 60}\nT6 数字防篡改（HR-07 实证）\n输入：{CASES[0][1]}（注入一次性数字篡改）")
    import os
    # Mock 模式走 MOCK_FAULT；接了真实模型它不认，改走 TAMPER_FIGURE（在 staff 节点真实篡改）
    os.environ["MOCK_FAULT"] = "figure_tamper"
    os.environ["TAMPER_FIGURE"] = "1"
    from src.llm.client import LLMRouter
    LLMRouter._fault_used = False
    from src.graph import graph as G
    G._TAMPER_USED[0] = False
    try:
        out = await run_query(CASES[0][1])
        traces = [t.get("msg", "") for t in (out.get("trace") or [])]
        caught = any("FIGURE_MISMATCH" in t or "数字对不上" in t for t in traces)
        final_ok = (out.get("validation") or {}).get("ok") is True
        ok = caught and final_ok
        show(out)
        print(f"是否被拦下：{'是 ✅' if caught else '否 ❌'}"
              f" · 最终是否通过：{'是 ✅' if final_ok else '否 ❌'}")
        print(f"判定：{'PASS ✅' if ok else 'FAIL ❌'}")
        passed += 1 if ok else 0
    finally:
        os.environ.pop("MOCK_FAULT", None)
        os.environ.pop("TAMPER_FIGURE", None)
        LLMRouter._fault_used = False
        G._TAMPER_USED[0] = False

    print(f"\n{'=' * 60}\n结果：{passed}/{len(CASES) + 1} 通过")
    return passed


async def chat(thread_id: str = "") -> None:
    """三期：多轮会话。同一 thread_id 下，改一个条件只重算受影响参谋。"""
    tid = thread_id or ("t_" + time.strftime("%m%d%H%M%S"))
    print(f"多轮会话 thread={tid}   （输入 q 退出）")
    print("例：先问「陆海空联合登陆作战，5天内完成投送」，")
    print("    再问「运输机运力减半怎么调」→ 只重算受运力影响的军种")
    while True:
        try:
            q = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in ("q", "quit", "exit"):
            break
        if not q:
            continue
        try:
            show(await run_query(q, thread_id=tid))
        except Exception as e:                          # noqa: BLE001
            print(f"[错误] {type(e).__name__}: {e}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        raise SystemExit(0 if asyncio.run(run_tests()) == len(CASES) + 1 else 1)
    if len(sys.argv) > 1 and sys.argv[1] == "--chat":
        asyncio.run(chat(sys.argv[2] if len(sys.argv) > 2 else ""))
    elif len(sys.argv) > 1:
        show(asyncio.run(run_query(" ".join(sys.argv[1:]))))
    else:
        print(__doc__)
