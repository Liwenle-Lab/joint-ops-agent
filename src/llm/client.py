# -*- coding: utf-8 -*-
"""
llm/client.py —— LLM 适配层（OpenAI 兼容 + Mock 兜底 + 双档 + 韧性）

设计约束（对应《项目设计文档》§4.7 与 s11）：
  · 双档模型：main（主力，规划/仲裁） / light（轻量，路由/抽取/改写），省约 80% 辅助成本
  · 无 API Key 时自动降级 MOCK：只跑链路，不产生真实推理开销
  · MOCK 模式下兵力数字仍来自真实 SQLite —— Mock 只负责"把已查到的数字组织成文"
  · 429 / 529 / overloaded 走指数退避 min(0.5*2^n, 32s) + 25% 抖动

用法：
    router = LLMRouter()
    resp = router.chat(tier="main", messages=[...], role_hint="staff")
"""
from __future__ import annotations
import asyncio
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = ROOT / "config" / ".env"

# 旁路事件总线：只给 Web 前端推实时进度，不参与推理逻辑。
# events.py 不 import 任何内部模块，放进来零代价且不会形成循环依赖。
from src.graph import events as evs            # noqa: E402

TIER_MAIN = "main"
TIER_LIGHT = "light"


# ── 环境变量 ────────────────────────────────────────────
def load_env(path: Path = ENV_PATH) -> None:
    """极简 .env 解析：避免为一个配置文件引入依赖"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


@dataclass
class LLMResponse:
    content: str
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    model: str = "mock"
    tier: str = TIER_MAIN
    mocked: bool = True

    def json(self) -> Dict[str, Any]:
        """解析 content 为 JSON（失败抛错，由调用方走恢复回环）"""
        text = self.content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n", "", text)
            text = re.sub(r"\n```$", "", text)
        return json.loads(text)


class LLMRouter:
    def __init__(self, env_path: Path = ENV_PATH):
        load_env(env_path)
        self.base_url = os.environ.get("LLM_BASE_URL", "").strip()
        self.api_key = os.environ.get("LLM_API_KEY", "").strip()
        self.model_main = os.environ.get("LLM_MODEL_MAIN", "").strip() or "deepseek-chat"
        self.model_light = os.environ.get("LLM_MODEL_LIGHT", "").strip() or self.model_main
        self.timeout = int(os.environ.get("LLM_TIMEOUT", "60"))
        # ★ 必须显式限制输出长度：不设的话模型可能一直生成到平台上限，
        #   实测曾导致仲裁节点单次调用超过 8 分钟仍未返回（输入含三份完整方案）。
        self.max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "3072"))
        self.verbose = os.environ.get("LLM_VERBOSE", "1") == "1"
        self.mocked = not (self.base_url and self.api_key)
        self.counts: Dict[str, int] = {TIER_MAIN: 0, TIER_LIGHT: 0}
        self._client = None
        self._aclient = None          # 二期：异步客户端，Send 并行节点真并发靠它
        if not self.mocked:
            from openai import AsyncOpenAI, OpenAI
            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key,
                                  timeout=self.timeout)
            self._aclient = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key,
                                        timeout=self.timeout)

    # ── 同步 / 异步共用的准备与解析 ──────────────────────
    def _prepare(self, messages: List[Dict[str, Any]], tier: str,
                 tools: Optional[List[Dict[str, Any]]], tool_choice: Optional[str],
                 json_mode: bool) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": self.model_main if tier == TIER_MAIN else self.model_light,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        kwargs["max_tokens"] = self.max_tokens
        return kwargs

    @staticmethod
    def _decode(msg: Any, kwargs: Dict[str, Any], tier: str) -> LLMResponse:
        calls = []
        if getattr(msg, "tool_calls", None):
            for c in msg.tool_calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                calls.append({"name": c.function.name, "arguments": args, "id": c.id})
        return LLMResponse(content=msg.content or "", tool_calls=calls,
                           model=kwargs["model"], tier=tier, mocked=False)

    def _empty_retry(self, content: Optional[str], attempt: int,
                     kwargs: Dict[str, Any]) -> bool:
        """防御：推理模型会把额度耗在内部思考上，导致正文为空。
        这不是"答错了"而是"额度不够"，自动加倍重试。返回 True 表示需重试。"""
        if not (content or "").strip() and attempt < 3:
            kwargs["max_tokens"] = min(kwargs["max_tokens"] * 2, 32768)
            tip = (f"⚠空输出（多半被思考耗尽）→ max_tokens 提升到"
                   f" {kwargs['max_tokens']} 重试")
            print(f"    [llm] {tip}", flush=True)
            evs.emit("warn", msg=tip)          # 前端要能看到"还在重试着"
            return True
        return False

    def _log_done(self, role_hint: str, tier: str, t0: float,
                  content: Optional[str], finish: Any) -> None:
        if not self.verbose:
            return
        sec = time.time() - t0
        line = (f"{role_hint or tier} 用时 {sec:.1f}s"
                f" · 输出 {len(content or '')} 字 · finish={finish}"
                f"{'' if content else '  ⚠空内容！'}")
        print(f"    [llm] {line}", flush=True)
        # 旁路推给 Web 前端：这条最有用，能看出哪一步在慢、有没有被截断
        evs.emit("llm", role=role_hint or tier, tier=tier, sec=round(sec, 1),
                 chars=len(content or ""), finish=str(finish),
                 model=(self.model_main if tier == TIER_MAIN else self.model_light))

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(0.5 * (2 ** attempt), 32) * (1 + 0.25 * random.random())

    # ── 统一入口（同步）────────────────────────────────
    def chat(self,
             messages: List[Dict[str, Any]],
             tier: str = TIER_MAIN,
             tools: Optional[List[Dict[str, Any]]] = None,
             tool_choice: Optional[str] = None,
             json_mode: bool = False,
             role_hint: str = "") -> LLMResponse:
        self.counts[tier] = self.counts.get(tier, 0) + 1
        if self.mocked:
            return self._mock(messages, tier, role_hint)

        kwargs = self._prepare(messages, tier, tools, tool_choice, json_mode)
        last_err: Optional[Exception] = None
        t0 = time.time()
        for attempt in range(5):
            try:
                resp = self._client.chat.completions.create(**kwargs)
                msg = resp.choices[0].message
                if self._empty_retry(msg.content, attempt, kwargs):
                    continue
                self._log_done(role_hint, tier, t0, msg.content,
                               getattr(resp.choices[0], "finish_reason", "?"))
                return self._decode(msg, kwargs, tier)
            except Exception as e:                       # noqa: BLE001
                last_err = e
                if not self._retryable(e):
                    break
                time.sleep(self._backoff(attempt))
        raise RuntimeError(f"LLM 调用失败：{last_err}")

    # ── 统一入口（异步，二期并行用）──────────────────────
    # 与 chat 同构，只把阻塞 IO 换成 await。若拿不到异步客户端（如 openai 包过旧），
    # 自动退化成线程池里的同步调用——并行度下降但不至于崩。
    async def achat(self,
                    messages: List[Dict[str, Any]],
                    tier: str = TIER_MAIN,
                    tools: Optional[List[Dict[str, Any]]] = None,
                    tool_choice: Optional[str] = None,
                    json_mode: bool = False,
                    role_hint: str = "") -> LLMResponse:
        self.counts[tier] = self.counts.get(tier, 0) + 1
        if self.mocked:
            return self._mock(messages, tier, role_hint)
        if self._aclient is None:
            return await asyncio.to_thread(
                self.chat, messages, tier, tools, tool_choice, json_mode, role_hint)

        kwargs = self._prepare(messages, tier, tools, tool_choice, json_mode)
        last_err: Optional[Exception] = None
        t0 = time.time()
        for attempt in range(5):
            try:
                resp = await self._aclient.chat.completions.create(**kwargs)
                msg = resp.choices[0].message
                if self._empty_retry(msg.content, attempt, kwargs):
                    continue
                self._log_done(role_hint, tier, t0, msg.content,
                               getattr(resp.choices[0], "finish_reason", "?"))
                return self._decode(msg, kwargs, tier)
            except Exception as e:                       # noqa: BLE001
                last_err = e
                if not self._retryable(e):
                    break
                await asyncio.sleep(self._backoff(attempt))
        raise RuntimeError(f"LLM 调用失败：{last_err}")

    @staticmethod
    def _retryable(e: Exception) -> bool:
        s = f"{e}".lower()
        code = getattr(e, "status_code", None)
        return code in (429, 500, 502, 503, 529) or any(
            k in s for k in ("429", "529", "rate limit", "overloaded", "timeout", "timed out"))

    # ── MOCK：规则化生成，数字只从已注入的 SQL 结果里取 ──────
    def _mock(self, messages: List[Dict[str, Any]], tier: str, role_hint: str) -> LLMResponse:
        # 只取最后一条 user 消息：system 里的示例词（如"周报"）不能污染意图判定
        text = next((m.get("content", "") for m in reversed(messages)
                     if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
        facts = self._parse_facts(text)

        # graph.py 传来的 role_hint 带军种后缀（如 "staff:陆军"/"nego:海军"），
        # 必须取冒号前的主干匹配，否则会落到兜底分支只回 {"ok": True}，
        # 使 Mock 参谋/协商产物为空、终验 HR-07 直接 FAIL。
        role = role_hint.split(":", 1)[0]
        if role == "intent":
            return LLMResponse(content=json.dumps(self._mock_intent(text), ensure_ascii=False),
                               tier=tier, mocked=True)
        if role == "commander":
            return LLMResponse(content=json.dumps(self._mock_commander(text), ensure_ascii=False),
                               tier=tier, mocked=True)
        if role == "staff":
            return LLMResponse(content=json.dumps(self._mock_staff(text, facts), ensure_ascii=False),
                               tier=tier, mocked=True)
        if role == "nego":
            # Mock 协商：不动申索，原样把参谋方案交回（协商收敛由真模型负责）。
            return LLMResponse(content=json.dumps({"claim": "", "objection": "",
                                                   "concession": "", "asks": {}},
                                                  ensure_ascii=False),
                               tier=tier, mocked=True)
        if role == "arbitrate":
            return LLMResponse(content=json.dumps(self._mock_arbitrate(text, facts), ensure_ascii=False),
                               tier=tier, mocked=True)
        return LLMResponse(content=json.dumps({"ok": True}, ensure_ascii=False),
                           tier=tier, mocked=True)

    @staticmethod
    def _parse_facts(text: str) -> Dict[str, Any]:
        """从注入的查询结果里抓 key=value，Mock 只能用这里的数字，不许自己造"""
        facts: Dict[str, Any] = {}
        for k, v in re.findall(r"([a-z_]+)=(-?\d+(?:\.\d+)?)", text):
            facts.setdefault(k, int(float(v)))
        for k, v in re.findall(r"([a-z_]+)=([一-龥A-Za-z0-9\-]+)", text):
            facts.setdefault(k, v)
        for qid in re.findall(r"query_id=(q_[0-9a-f]+)", text):
            facts.setdefault("query_ids", [])
            if qid not in facts["query_ids"]:
                facts["query_ids"].append(qid)
        # 环境数字同样带 query_id（写作 env_id=e_xxxx）。Mock 仲裁要把它们
        # 一并写进「数字来源」，才能忠实复现真模型「含 sql 与 env 两类」的产出，
        # 也便于在真模型限频时验证溯源补齐逻辑对环境 id 同样无损。
        for eid in re.findall(r"env_id=(e_[0-9a-f]+)", text):
            facts.setdefault("env_ids", [])
            if eid not in facts["env_ids"]:
                facts["env_ids"].append(eid)
        # 多值字段：单位清单会重复出现，不能用 setdefault
        ids = re.findall(r"unit_id=([A-Za-z0-9\-]+)", text)
        if ids:
            facts["unit_ids"] = list(dict.fromkeys(ids))
        pns = re.findall(r"\bpersonnel=(\d+)", text)
        if pns:
            facts["unit_personnels"] = [int(x) for x in pns]
        caps = re.findall(r"capacity=(\d+)", text)
        if caps:
            facts["capacities"] = [int(x) for x in caps]
        return facts

    # 以下四个 Mock 生成器：只做"结构化拼装"，不产生任何新数字
    @staticmethod
    def _mock_intent(text: str) -> Dict[str, Any]:
        t = text
        if any(k in t for k in ("周报", "翻译", "写代码", "写诗", "天气")):
            return {"route": "out_of_scope", "reason": "领域外请求",
                    "task_type": "other", "domain": None, "complexity": None}
        asked = [d for d in ("陆军", "海军", "空军") if d in t]
        is_query = any(k in t for k in ("多少人", "多少装备", "多少人多少装备", "兵力", "装备数量"))
        if asked and is_query and len(asked) == 1:
            return {"route": "single_domain", "domain": asked[0],
                    "task_type": "query", "complexity": "单域",
                    "constraints": {}, "resolved_refs": []}
        if len(asked) >= 2 or any(k in t for k in ("登陆", "联合", "作战", "推演", "规划")):
            return {"route": "full_chain", "domain": "联合",
                    "task_type": "planning", "complexity": "跨域",
                    "constraints": {}, "resolved_refs": []}
        if asked:
            return {"route": "single_domain", "domain": asked[0],
                    "task_type": "query", "complexity": "单域",
                    "constraints": {}, "resolved_refs": []}
        return {"route": "need_clarify", "reason": "未能判定领域",
                "task_type": "unknown", "domain": None, "complexity": None}

    @staticmethod
    def _mock_commander(text: str) -> Dict[str, Any]:
        cfg = json.loads((ROOT / "config" / "constraints.json").read_text(encoding="utf-8"))
        return {
            "objectives": ["按用户给定背景完成跨域筹划"],
            "constraints": {
                "force_cap": cfg["force_cap"],
                "deadline_days": cfg["deadline_days"],
                "no_go_zones": cfg["no_go_zones"],
                "roe_allowed": cfg["roe_allowed"],
                "resource_cap": cfg["resource_cap"],
            },
            "sub_tasks": [
                {"id": "T1", "owner": "陆军", "desc": "地面主攻方向兵力编成与时序"},
                {"id": "T2", "owner": "海军", "desc": "海上投送与火力支援"},
                {"id": "T3", "owner": "空军", "desc": "制空与空中投送"},
            ],
            "assigned_domains": ["陆军", "海军", "空军"],
        }

    @staticmethod
    def _mock_staff(text: str, facts: Dict[str, Any]) -> Dict[str, Any]:
        qids = facts.get("query_ids", [])
        src = f"sql:{qids[0]}" if qids else "sql:<missing>"
        # 单域查询：直接回答，数字全部来自查询结果
        if "回答用户" in text or "直接作答" in text:
            sv = facts.get("service", "该军种")
            figures = {
                "personnel_total": facts.get("personnel_total"),
                "equipment_total": facts.get("equipment_total"),
                "unit_count": facts.get("unit_count"),
            }
            # 故障注入（仅测试用）：篡改数字，验证 validate 能否对回 SQL 拦下
            if os.environ.get("MOCK_FAULT") == "figure_tamper" and not getattr(
                    LLMRouter, "_fault_used", False):
                LLMRouter._fault_used = True
                figures = {k: (v + 7 if isinstance(v, int) else v) for k, v in figures.items()}
            return {
                "kind": "answer",
                "answer": (f"{sv}现有 {figures.get('personnel_total')} 人、装备 "
                           f"{figures.get('equipment_total')} 件，下辖 "
                           f"{figures.get('unit_count')} 个单位。数据来源：{src}"),
                "figures": figures,
                "source": src,
            }
        # 跨域规划：编成取查询结果里的真实单位，人数照抄 SQL，不做心算
        ids = facts.get("unit_ids", [])
        pns = facts.get("unit_personnels", [])
        units = []
        for i, uid in enumerate(ids[:2]):                  # 只投前 2 个单位，避免超上限
            p = pns[i] if i < len(pns) else 0
            units.append({"unit_id": uid, "personnel": p, "source": src})
        asks = {"transport": 10, "fires": 4, "satellite": 1, "sealift": 2}
        caps = facts.get("capacities") or []
        ask_src = f"sql:{qids[-1]}" if qids else src
        # 申索不得超过查到的容量（Mock 也要守规矩）
        for i, k in enumerate(("transport", "fires", "satellite", "sealift")):
            if i < len(caps) and asks[k] > caps[i]:
                asks[k] = max(0, caps[i] // 3)
        return {
            "kind": "plan",
            "domain": facts.get("service") or "未知",
            "units": units,
            "asks": asks,
            "ask_sources": {k: ask_src for k in asks},
            "timeline": [{"start": "D+0", "end": "D+2", "action": "按指挥层约束展开行动"}],
            "areas": [],
            "fire_level": "常规",
            "risks": ["（Mock）样例风险条目"],
        }

    @staticmethod
    def _mock_arbitrate(text: str, facts: Dict[str, Any]) -> Dict[str, Any]:
        # 两类来源都要写：兵力/资源来自 sql:，环境来自 env:（对应 prompts.ARBITRATE 纪律）。
        sources = list(facts.get("query_ids", [])) + list(facts.get("env_ids", []))
        return {
            "final_plan": {
                "态势判断": "（Mock）三军种方案已合并",
                "兵力编成": "（Mock）见各参谋方案",
                "行动时序": ["D+1 准备", "D+2 展开", "D+3 主攻"],
                "风险与备选": ["（Mock）样例风险"],
                "资源分配": {"transport": 30, "fires": 9, "satellite": 3},
                "数字来源": sources,
            }
        }
