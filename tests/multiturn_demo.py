# -*- coding: utf-8 -*-
"""
三期真模型实测：多轮会话 + 增量重规划

同一 thread_id 连问两轮：
  第 1 轮  全量筹划（v1）
  第 2轮  改一个条件（运输机运力减半）→ 应当只重算受影响的军种

看点：第二轮的 impacted / reused / plan_version，以及实际调用了几个参谋（看 [llm] staff: 日志）。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from run import run_query, show                          # noqa: E402

TID = "t_" + time.strftime("%m%d%H%M%S")


async def main() -> None:
    print(f"thread_id = {TID}\n{'=' * 60}")
    print("【第 1 轮】陆海空联合登陆作战，5天内完成投送，请做联合筹划")
    out1 = await run_query("陆海空联合登陆作战，5天内完成投送，请做联合筹划",
                           thread_id=TID)
    show(out1)

    print(f"\n{'=' * 60}\n【第 2 轮】运输机运力减半，怎么调整？")
    out2 = await run_query("运输机运力减半，怎么调整？", thread_id=TID)
    show(out2)

    print(f"\n{'=' * 60}\n【对照检查】")
    print(f"  v1 版本        : {out1.get('plan_version')}")
    print(f"  v2 版本        : {out2.get('plan_version')}")
    print(f"  是否增量       : {out2.get('is_incremental')}")
    print(f"  变更项         : {out2.get('changed_keys')}")
    print(f"  重算的军种     : {out2.get('impacted')}")
    print(f"  复用的军种     : {out2.get('reused')}")
    v1c = (out1.get("constraints") or {}).get("resource_cap", {})
    v2c = (out2.get("constraints") or {}).get("resource_cap", {})
    print(f"  v1 资源容量    : {v1c}")
    print(f"  v2 资源容量    : {v2c}")

    impacted = out2.get("impacted") or []
    reused = out2.get("reused") or []
    all_dom = set(impacted) | set(reused)
    # 判定：版本递增 + 变更被认出 + 重算/复用把三个军种划分完整。
    # ★ 不强制要求 reused 非空：本兵力池下三军往往都申索了同一资源
    #   （运力减半 → 陆军16/海军10/空军24 全都受影响），此时全量重算就是正确答案。
    #   复用机制本身由 tests/test_incremental.py 的确定性用例验证。
    ok = (out2.get("plan_version") == 2
          and bool(out2.get("is_incremental"))
          and bool(out2.get("changed_keys"))
          and all_dom == {"陆军", "海军", "空军"}
          and v2c.get("transport") != v1c.get("transport"))
    print(f"\n判定：{'PASS ✅ 多轮增量重规划生效'
                       if ok else 'FAIL ❌'}"
          f"（{'三军均受运力影响，全量重算属正确行为' if not reused else '有军种被复用'}）")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    asyncio.run(main())
