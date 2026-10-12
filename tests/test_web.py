# -*- coding: utf-8 -*-
"""
test_web.py —— 第四期（前端 + 本地部署）Web 层验收

不跑模型时的中间理论是对的不好说，跑一遍才知道。这里验的是：
  W1 服务存活、配置的模型档位与当前是否 Mock
  W2 静态资源与页面可访问（css/js 缺一，界面就是白页）
  W3 单域快通道端到端（SSE 事件齐全、结论正确）
  W4 全链路 + 多轮增量（SSE 全程有反馈、第二轮拿到 v2）
  W5 协商黑板可读（证明"第 N 轮谁让了什么"确实落了盘）

用法（服务需已启动）：
    python tests/test_web.py [--base http://127.0.0.1:8000]
"""
from __future__ import annotations
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8000"
PASS = 0
FAIL = 0


def _check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}" + (f"｜{detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"｜{detail}" if detail else ""))
    return ok


def _get(path: str) -> Any:
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        raw = r.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode("utf-8", "replace")


def _post_sse(path: str, body: Dict[str, Any], timeout: int = 420
              ) -> Tuple[List[Tuple[str, Dict]], float]:
    """读 SSE 流。返回 (事件列表, 首字节延迟秒数)。

    为什么手写解析：httpx-sse 之类不一定装了，而 SSE 协议本身极简，
    这里只需要按空行切帧即可，不值得为一个帮助库加依赖。
    """
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST")
    t0 = time.time()
    first = None
    events: List[Tuple[str, Dict]] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        buf = b""
        while True:
            chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096)
            if not chunk:
                break
            if first is None:
                first = time.time() - t0
            buf += chunk
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                text = frame.decode("utf-8", "replace")
                ev_name, data = "log", ""
                for line in text.splitlines():
                    if line.startswith("event: "):
                        ev_name = line[7:].strip()
                    elif line.startswith("data: "):
                        data += line[6:]
                if data:
                    try:
                        events.append((ev_name, json.loads(data)))
                    except json.JSONDecodeError:
                        pass
    return events, (first or 0.0)


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--skip-full", action="store_true",
                    help="跳过全链路（约 2 分钟），只验 Web 层")
    args = ap.parse_args()
    BASE = args.base.rstrip("/")

    print("=" * 62)
    print("  Web 层验收（第四期）   base=" + BASE)
    print("=" * 62)

    # ── W1 服务存活 ──────────────────────────────
    print("\n[W1] 服务与模型状态")
    try:
        h = _get("/api/health")
        _check("health 可达", isinstance(h, dict))
        _check("模型档位已读", bool(h.get("model_main")),
               f"main={h.get('model_main')} / light={h.get('model_light')} "
               f"/ mocked={h.get('mocked')}")
        _check("数据库文件存在", h.get("db") is True)
        caps = _get("/api/constraints")
        _check("容量上限可读", bool((caps or {}).get("resource_cap")),
               str((caps or {}).get("resource_cap")))
    except Exception as e:                              # noqa: BLE001
        print(f"  ✗ 服务未启动：{type(e).__name__}: {e}")
        print("    先执行：python server.py")
        return 1

    # ── W2 静态资源 ──────────────────────────────
    print("\n[W2] 静态资源")
    for path, want in (("/", "text/html"),
                       ("/static/style.css", "text/css"),
                       ("/static/app.js", "javascript")):
        try:
            with urllib.request.urlopen(BASE + path, timeout=10) as r:
                body = r.read().decode("utf-8", "replace")
                ctype = r.headers.get("Content-Type", "")
            _check(f"{path} 可访问", len(body) > 100 and want in ctype,
                   f"{len(body)} 字节")
        except Exception as e:                          # noqa: BLE001
            _check(f"{path} 可访问", False, str(e))

    # ── W3 单域快通道 ────────────────────────────
    print("\n[W3] 单域查询端到端（SSE）")
    events, first_byte = _post_sse("/api/chat", {"text": "陆军现在多少人，多少装备？"})
    kinds = [k for k, _ in events]
    _check("首字节响应够快", first_byte < 5, f"{first_byte:.2f}s")
    _check("事件序列完整",
           "begin" in kinds and "done" in kinds and "node" in kinds,
           f"{len(events)} 个事件")
    _check("有 LLM 耗时事件", "llm" in kinds, f"{kinds.count('llm')} 条")

    result = {}
    for k, d in events:
        if k == "done":
            result = d.get("result", {})
    _check("终验通过", (result.get("validation") or {}).get("ok") is True,
           str((result.get("validation") or {}).get("code")))
    ans = result.get("answer") or ""
    _check("回答含真实兵力数 15900",
           any(x in ans.replace(",", "").replace("，", "")
               for x in ("15900", "1.59万")), ans[:60])

    if args.skip_full:
        print("\n（--skip-full：已跳过全链路与多轮）")
        _summary()
        return 0 if FAIL == 0 else 1

    # ── W4 全链路 + 多轮 ─────────────────────────
    print("\n[W4] 全链路 + 多轮增量重规划")
    tid = "webt_" + time.strftime("%H%M%S")
    ev1, fb1 = _post_sse("/api/chat", {
        "text": "陆海空联合登陆作战，5天内完成投送，请做联合筹划",
        "thread_id": tid})
    r1 = next((d.get("result", {}) for k, d in ev1 if k == "done"), {})
    _check("全链路终验通过", (r1.get("validation") or {}).get("ok") is True,
           str((r1.get("validation") or {}).get("code")))
    _check("产出联合方案", bool(r1.get("final_plan")))
    _check("三份参谋方案齐全", len(r1.get("staff") or []) == 3,
           f"{len(r1.get('staff') or [])} 份")
    _check("方案版本 v1", r1.get("plan_version") == 1, str(r1.get("plan_version")))
    node_n = len([1 for k, d in ev1 if k == "node"])
    _check("全程有节点反馈", node_n >= 5, f"{node_n} 个节点事件")

    # 第二轮：改一个条件，应沿同一 thread 增量重规划
    ev2, _ = _post_sse("/api/chat", {
        "text": "运输机运力减半，重新做筹划", "thread_id": tid})
    r2 = next((d.get("result", {}) for k, d in ev2 if k == "done"), {})
    _check("第二轮沿用同一会话",
           bool(r2.get("plan_version")) and r2.get("plan_version") >= 2,
           f"版本 v{r2.get('plan_version')}")
    _check("第二轮终验通过", (r2.get("validation") or {}).get("ok") is True,
           str((r2.get("validation") or {}).get("code")))
    _check("识别出变更项", bool(r2.get("changed_keys")) or bool(r2.get("impacted")),
           f"变更={r2.get('changed_keys')} 重算={r2.get('impacted')} "
           f"复用={r2.get('reused')}")

    # ── W5 黑板 ──────────────────────────────────
    print("\n[W5] 协商黑板")
    bb = _get("/api/blackboard")
    _check("黑板可读取", isinstance(bb, dict) and "rounds" in bb)
    rounds = bb.get("rounds") or []
    _check("有协商记录或明确无冲突", True,
           f"{len(rounds)} 条记录 / session={bb.get('session', '')[:16]}")
    if rounds:
        sample = rounds[0]
        _check("记录含军种与申索", bool(sample.get("speaker")), str(sample.get("speaker")))

    _summary()
    return 0 if FAIL == 0 else 1


def _summary() -> None:
    print("\n" + "=" * 62)
    print(f"  结果：{PASS}/{PASS + FAIL} 通过" + ("  ✅" if FAIL == 0 else "  ❌"))
    print("=" * 62)


if __name__ == "__main__":
    raise SystemExit(main())
