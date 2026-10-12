# -*- coding: utf-8 -*-
"""
check_llm.py —— 接真模型前的三步自检（配置 / 连通 / JSON 模式）

为什么需要它：
  模型名会退役（例如 deepseek-chat 已于 2026-07-24 下线），"配置写对了"不等于"能跑通"。
  本脚本不问你猜，而是直接向平台要可用模型清单，并实测 JSON 输出是否可用——
  因为本系统所有节点都依赖 JSON 结构化输出，这一步不通，全链路必挂。

用法：
    python check_llm.py            # 完整自检
    python check_llm.py --models   # 只列可用模型（改 .env 前先看这个）
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

ENV_PATH = ROOT / "config" / ".env"


def mask(k: str) -> str:
    if not k:
        return "(空)"
    return k[:8] + "*" * max(0, len(k) - 12) + (k[-4:] if len(k) > 12 else "")


def load_env() -> dict:
    cfg = {}
    if not ENV_PATH.exists():
        return cfg
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg


def main() -> int:
    print("=" * 64)
    print("接真模型自检")
    print("=" * 64)

    # ── 步骤 1：配置 ────────────────────────────────────
    print("\n[1/3] 配置检查")
    cfg = load_env()
    if not ENV_PATH.exists():
        print(f"  ✗ 未找到 {ENV_PATH}")
        print("    → 执行：cp config/.env.example config/.env  然后填入 API Key")
        return 1

    base = cfg.get("LLM_BASE_URL", "").strip()
    key = cfg.get("LLM_API_KEY", "").strip()
    main_m = cfg.get("LLM_MODEL_MAIN", "").strip()
    light_m = cfg.get("LLM_MODEL_LIGHT", "").strip() or main_m

    print(f"  文件       : {ENV_PATH}")
    print(f"  BASE_URL   : {base or '(空)'}")
    print(f"  API_KEY    : {mask(key)}")
    print(f"  主力档模型 : {main_m or '(空)'}")
    print(f"  轻量档模型 : {light_m or '(空)'}")

    problems = []
    if not base:
        problems.append("LLM_BASE_URL 未填")
    if not key:
        problems.append("LLM_API_KEY 未填（留空会降级 MOCK，不会真的调用模型）")
    if not main_m:
        problems.append("LLM_MODEL_MAIN 未填")
    if base and not key:
        print("\n  当前处于 MOCK 模式：链路能跑，但不会产生真实推理，无法验证语义层。")
    if problems:
        print("\n  待处理：" + "；".join(problems))
        if not key:
            return 1

    # ── 步骤 2：连通 + 列模型 ───────────────────────────
    print("\n[2/3] 连通性与可用模型")
    try:
        from openai import OpenAI
    except ImportError:
        print("  ✗ 未安装 openai SDK → pip install openai")
        return 1

    client = OpenAI(base_url=base, api_key=key, timeout=int(cfg.get("LLM_TIMEOUT", "60")))
    available: list[str] = []
    try:
        models = client.models.list()
        available = sorted(m.id for m in models.data)
        print(f"  ✓ 连通成功，账号可用模型 {len(available)} 个：")
        for m in available:
            mark = " ←主力" if m == main_m else (" ←轻量" if m == light_m else "")
            print(f"      - {m}{mark}")
    except Exception as e:                                    # noqa: BLE001
        print(f"  ✗ 连通失败：{type(e).__name__}: {e}")
        print("\n  常见原因：")
        print("    · API Key 填错/已吊销 → 到平台后台重新生成")
        print("    · base_url 少了或多了 /v1 → 两种都试一次")
        print("    · 账户余额为 0 或未实名 → 先充值（多数平台新用户有免费额度）")
        print("    · 网络/代理问题 → 检查是否能访问该域名")
        return 1

    if "--models" in sys.argv:
        return 0

    if available:
        for tier, name in (("主力", main_m), ("轻量", light_m)):
            if name and name not in available:
                print(f"\n  ⚠ 配置的{tier}档模型 '{name}' 不在可用清单里 —— 多半已改名或退役。")
                hint = [m for m in available if m.startswith(name.split("-")[0])][:5]
                if hint:
                    print(f"    可考虑改用：{', '.join(hint)}")

    # ── 步骤 3：JSON 模式实测 ───────────────────────────
    print("\n[3/3] JSON 结构化输出实测（本系统所有节点都依赖它）")
    probe = [
        {"role": "system",
         "content": "你是联合作战参谋。只输出 JSON，不要任何解释文字。"},
        {"role": "user",
         "content": '请输出 JSON：{"kind":"probe","ok":true,"num":42}'},
    ]
    for tier, model in (("主力", main_m), ("轻量", light_m)):
        if not model:
            continue
        try:
            resp = client.chat.completions.create(
                model=model, messages=probe,
                response_format={"type": "json_object"})
            text = (resp.choices[0].message.content or "").strip()
            if text.startswith("```"):
                import re
                text = re.sub(r"^```[a-zA-Z]*\n", "", text)
                text = re.sub(r"\n```$", "", text)
            data = json.loads(text)
            ok = data.get("ok") is True and data.get("num") == 42
            print(f"  {'✓' if ok else '⚠'} {tier}档 {model}：JSON 可解析"
                  f"{'，内容正确' if ok else '，但内容不符（可能是随机生成，不影响使用）'}")
        except Exception as e:                                # noqa: BLE001
            print(f"  ✗ {tier}档 {model}：{type(e).__name__}: {e}")
            # 错误分类：不同状态码对应完全不同的处置，混为一谈会误导排查方向
            # （曾把 402 余额不足误诊成"模型不支持 JSON"）
            code = getattr(e, "status_code", None)
            msg = f"{e}".lower()
            if code == 402 or "insufficient balance" in msg or "余额" in msg:
                print("    → 【余额不足】账户没钱了，模型本身没问题。")
                print("      去平台后台充值即可（多数平台新用户有赠送额度）。")
                print("      https://platform.deepseek.com/top_up")
            elif code in (401, 403) or "unauthorized" in msg or "invalid api key" in msg:
                print("    → 【认证失败】API Key 错误或已吊销。")
                print("      到平台后台重新生成一个 key 并更新 .env。")
            elif code == 404 or "model" in msg and "not found" in msg:
                print("    → 【模型不存在】模型名已改名或退役。")
                print("      跑 python check_llm.py --models 查看账号真实可用模型名。")
            elif "response_format" in msg or "json_object" in msg or "json" in msg:
                print("    → 【不支持 JSON 模式】本系统所有节点都依赖结构化输出。")
                print("      换一个支持 response_format=json_object 的模型。")
            else:
                print("    → 未知错误，请把上面这行完整报错发出来排查。")
            return 1

    print("\n" + "=" * 64)
    print("自检通过。下一步：")
    print("  python run.py --test     跑第一期验收用例（此时为真实模型）")
    print("  python run.py \"陆军现在多少人多少装备\"   单条提问")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
