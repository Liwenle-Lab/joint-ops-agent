# -*- coding: utf-8 -*-
"""
events.py —— 运行期事件总线

为什么需要它：
  全链路一次要跑 1~3 分钟，中间有十几秒到几十秒的黑箱（等 LLM 返回）。
  命令行模式靠 print 出日志就够了，但 Web 前端要知道"现在跑到哪一步了"，
  光靠 invoke 的返回值只能等到全部结束——用户盯着空白页干等。

设计取舍：
  · 用线程安全的 queue.Queue，不用 asyncio.Queue
    理由：LangGraph 在 ainvoke 下会把「同步节点」丢进线程池执行，
          那些线程没有运行中的事件循环，asyncio.Queue 非线程安全会直接把数据写坏。
  · 有界队列 + 满则丢弃最旧
    理由：CLI 模式没人消费队列，事件会无限堆积把内存吃光。
          Web 端的事件是"锦上添花"，丢几条不致命，内存泄漏才是致命的。
  · emit 永不抛异常
    理由：这只是旁路日志。因为日志崩掉整条链路，是典型的本末倒置。

事件类型：
  node  节点完成    {tag, msg}
  llm   LLM 调用完成 {role, tier, sec, chars, finish}
  warn  异常/重试   {msg}
"""
from __future__ import annotations
import queue
import time
from typing import Any, Dict, List, Optional

_MAX = 1000
_Q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=_MAX)

# CLI 模式下是否同时打印。默认关闭：节点日志与 LLM 耗时在命令行里已经各有
# 一条 print（run.py / client.py），再回显一遍就成双份刷屏了。
# 排障时可用 set_echo(True) 打开，看事件到底有没有发出来。
_ECHO = False


def set_echo(on: bool) -> None:
    """Web 模式下关掉终端回显：事件已经走 SSE 推给浏览器了"""
    global _ECHO
    _ECHO = bool(on)


def emit(kind: str, **payload: Any) -> None:
    """旁路事件推送。任何情况下都必须安全返回"""
    ev = {"kind": kind, "ts": time.time(), **payload}
    try:
        if _Q.full():
            _Q.get_nowait()                    # 丢弃最旧的，保证队列能继续收
        _Q.put_nowait(ev)
    except Exception:                          # noqa: BLE001
        return
    if _ECHO:
        try:
            print(f"    [ev:{kind}] {payload}", flush=True)
        except Exception:                      # noqa: BLE001
            pass


def drain() -> List[Dict[str, Any]]:
    """取走当前所有待发事件（非阻塞）"""
    out: List[Dict[str, Any]] = []
    while True:
        try:
            out.append(_Q.get_nowait())
        except queue.Empty:
            break
    return out


def pending() -> int:
    return _Q.qsize()


def _self_test() -> None:
    set_echo(False)
    try:
        while not _Q.empty():
            _Q.get_nowait()
        emit("node", tag="a", msg="1")
        emit("llm", role="staff", sec=1.5)
        got = drain()
        assert len(got) == 2, f"应取到 2 条，实际 {len(got)}"
        assert got[0]["kind"] == "node" and got[0]["tag"] == "a"
        assert drain() == [], "取空后应为空"
        # 超量入队不能崩，且必须保留最新的一批
        for i in range(_MAX + 50):
            emit("node", tag="x", msg=str(i))
        assert pending() <= _MAX, f"队列应有界，实际 {pending()}"
        tail = drain()
        assert tail[-1]["msg"] == str(_MAX + 49), "满队列时应丢弃最旧、保留最新"
        print("事件总线自测 OK（有界·丢弃最旧·取后即空）")
    finally:
        set_echo(True)


if __name__ == "__main__":
    _self_test()
