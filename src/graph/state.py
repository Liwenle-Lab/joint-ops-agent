# -*- coding: utf-8 -*-
"""
state.py —— 主图 State

四条互通通道（对应《节点实现规范》§1）：
  主图节点  共享同一份 State，节点返回增量 patch 自动 merge
  →参谋子图  Send 入参拷贝，返回后经 reducer 归并（所以简报必须自包含）
  子图内部   独立 State，主图看不到
  跨轮/大对象 文件系统 + checkpointer（State 只存路径不存正文）
"""
from __future__ import annotations
import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict


def add_list(a: List, b: List) -> List:
    """add-reducer：只增不减。恢复重跑会产生重复，取数时须 latest_by_domain"""
    return (a or []) + (b or [])


def merge_counts(a: Dict[str, int], b: Dict[str, int]) -> Dict[str, int]:
    """并行节点同时写计数器会撞车（同一步只允许一个值），取各档最大值（计数是累计量）"""
    out = dict(a or {})
    for k, v in (b or {}).items():
        out[k] = max(out.get(k, 0), v)
    return out


def merge_dict(a: Dict, b: Dict) -> Dict:
    """dict 合并：三参谋并行写同一 key（env_brief）会撞车，必须各自交自己的那份"""
    out = dict(a or {})
    out.update(b or {})
    return out


class JointOpsState(TypedDict, total=False):
    # ── 输入 ──
    user_input: str

    # ── 意图层 ──
    task_ticket: Dict[str, Any]          # 结构化任务单
    route: str                           # single_domain | full_chain | out_of_scope | need_clarify
    domain: Optional[str]                # 陆军 / 海军 / 空军 / 联合
    clarify_question: str

    # ── 指挥层 ──
    constraints: Dict[str, Any]          # 全局约束（只能由 commander 写）
    commander_brief: Dict[str, Any]      # 指挥层拆解结果（目标/子任务，不含约束本身）
    assigned: List[str]                  # 参与者裁剪后的军种列表

    # ── 参谋层 ──
    staff_reports: Annotated[List[Dict[str, Any]], add_list]
    current_asks: List[Dict[str, Any]]
    conflicts: List[Dict[str, Any]]

    # ── 仲裁与终验 ──
    final_plan: Dict[str, Any]
    validation: Dict[str, Any]
    answer: str                          # 单域快通道的直接回答

    # ── 控制 ──
    failure_type: str                    # plan_violation | artifact_incomplete | constraint_conflict
    escalate_to: str                     # commander | arbitrate | intent
    retry_count: int
    degraded: bool
    info_gap: List[str]

    # ── 二期：协商子循环 ──────────────────────────────
    negotiation_round: int               # 当前协商轮次（0 = 尚未协商）
    negotiation_status: str              # continue | converged | oscillating | cap_reached | stagnated
    bb_session: str                      # 黑板书 session_id（记录落盘在 SQLite，State 只存钥匙）
    negotiation_parties: List[str]       # 本轮实际参与者（裁剪后，不拉无关军种陪跑）

    # ── 五期：环境感知（L0 锚点 + L1 实况）──────────────
    # 参谋拿到的真实气象/海况/地形快照（按军种分开存），以及实际引用的 env_id。
    # 三个参谋并行各写自己那份 → 用 merge_dict，否则同一步多写会撞车。
    env_brief: Annotated[Dict[str, str], merge_dict]
    env_sources: Annotated[List[str], add_list]

    # ── 三期：多轮会话 + 增量重规划 ────────────────────
    # 多轮存的是「槽位 + 方案版本」，不是聊天记录：改一个约束 → 只让受影响参谋重算。
    plan_version: int                    # 方案版本号（v1/v2/...），产物落盘同名
    last_constraints: Dict[str, Any]     # 上一轮已下发的约束，用于与本轮做 diff
    changed_keys: List[str]              # 本轮相对上一轮的变更项（0 API 规则算出）
    impacted: List[str]                  # 受变更影响的军种（增量重规划只重派这些）
    reused: List[str]                    # 复用上一版方案的军种（未被重算）
    is_incremental: bool                 # 本轮是否走了增量重规划

    # ── 观测 ──
    trace: Annotated[List[Dict[str, Any]], add_list]
    api_calls: Annotated[Dict[str, int], merge_counts]
