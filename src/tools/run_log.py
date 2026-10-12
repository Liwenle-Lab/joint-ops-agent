# -*- coding: utf-8 -*-
"""
run_log.py —— 执行失败留痕

为什么需要它：
    2026-09-24 用户遇到 `NameError: FEEDBACK`，一轮筹划直接中断。但打开"历史会话"
    却发现那一轮**显示为 PASS，还挂着一版方案** —— 因为 State 是**累积**的：
    崩溃的那一轮只跑到 intent 就断了，快照里的 `final_plan` / `validation`
    全都是**上一轮继承来的**。于是"失败的一轮"在界面里冒充成功，
    用户看到的是"系统说成了，可它当时明明报错了"。

    这正是本项目最不能出的错：界面必须说实话。
    （项目里数字要带 sql:/env: 溯源、兵力池界面与参谋取数同源，都是同一条原则。）

    识别"中断轮"不能靠猜 checkpoint 的特征（几条快照、走到哪个节点），
    那是 LangGraph 的内部实现，随版本会变。**服务端本来就知道自己失败了** ——
    在捕获异常的地方直接记一笔，最可靠。

用法：
    RUNLOG.record_failure(thread_id, text, "NameError: ...")   # 出错时
    RUNLOG.failures_for(thread_id)                             # 折叠历史时读
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = ROOT / "db" / "joint_ops.db"

RUN_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_failures (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id   TEXT NOT NULL,
  text        TEXT NOT NULL,
  ts          REAL NOT NULL,
  error       TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_fail_thread ON run_failures(thread_id, id);
"""

MAX_ERROR = 400


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class RunLog:
    """执行失败台账。只增不改 —— 它是"当时到底发生了什么"的证据，
    和兵力改动日志一样，不许被后续操作抹掉。"""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path))
        con.row_factory = sqlite3.Row
        return con

    def ensure_schema(self) -> None:
        con = self._conn()
        try:
            con.executescript(RUN_LOG_SCHEMA)
            con.commit()
        finally:
            con.close()

    def record_failure(self, thread_id: str, text: str, error: str) -> int:
        """记一笔失败。★ 绝不能因为"日志写不进去"把调用方的异常处理搞崩 ——
        我们已经在处理一个错误了，这里再抛就会盖掉真正的根因。"""
        tid = (thread_id or "").strip()
        if not tid:
            return 0
        try:
            with self._lock:
                con = self._conn()
                try:
                    con.executescript(RUN_LOG_SCHEMA)     # 老库没有表也能自救
                    cur = con.execute(
                        "INSERT INTO run_failures(thread_id, text, ts, error, created_at)"
                        " VALUES(?,?,?,?,?)",
                        (tid, (text or "").strip(), time.time(),
                         (error or "")[:MAX_ERROR], _now_iso()))
                    con.commit()
                    return int(cur.lastrowid or 0)
                finally:
                    con.close()
        except Exception as e:                                    # noqa: BLE001
            print(f"[warn] 执行失败未能留痕（不影响本次报错）：{e}")
            return 0

    def failures_for(self, thread_id: str) -> List[Dict[str, Any]]:
        """某会话的失败记录，时间正序（折叠历史时按顺序对号入座）"""
        try:
            con = self._conn()
            try:
                con.executescript(RUN_LOG_SCHEMA)
                rows = con.execute(
                    "SELECT text, ts, error FROM run_failures"
                    " WHERE thread_id=? ORDER BY ts ASC, id ASC", (thread_id,)
                ).fetchall()
            finally:
                con.close()
            return [dict(r) for r in rows]
        except Exception as e:                                    # noqa: BLE001
            print(f"[warn] 失败记录读取失败，历史将按无中断处理：{e}")
            return []

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        con = self._conn()
        try:
            con.executescript(RUN_LOG_SCHEMA)
            rows = con.execute(
                "SELECT thread_id, text, ts, error, created_at FROM run_failures"
                " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]


RUNLOG = RunLog()
