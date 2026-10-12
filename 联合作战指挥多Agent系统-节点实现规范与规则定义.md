# 联合作战指挥多 Agent 系统 · 节点实现规范与规则定义

> 版本：v1.0　｜　日期：2026-09-16　｜　上游：《项目设计文档 v2.0》
> 回答三个落地问题：**每个节点具体怎么做 · 硬规则怎么定 · 收敛规则是哪些**（含 State 互通机制）
> 配套可执行实现：`rules_convergence.py`（硬规则引擎 + 冲突检测 + 收敛判据 + 失败归因，**14 项自测全 PASS**）

---

## 1. State 互通机制（先回答"流图 State 互通吗"）

**答案：主图内完全互通，子图单向拷贝，子图内部隔离，跨轮靠 checkpointer。** 共四条通道：

| # | 通道 | 互通方式 | 互通什么 | 不互通什么 |
|---|---|---|---|---|
| ① | **主图内节点** | 共享同一份 `JointOpsState`，节点返回**增量 patch**，LangGraph 自动 merge | 全部主图字段：`task_order / route / conflicts / negotiation_round / blackboard / retry_count / failure_type / final_plan` | — |
| ② | **主图 → 参谋子图** | `Send("staff", {...})` **入参拷贝**；子图返回后经 **reducer 归并**（`staff_reports` 用 `add`） | 入参：`domain + brief`；出参：1 条 `staff_reports` 条目 | 子图拿不到主图其它字段，参谋之间也互不可见 |
| ③ | **子图内部** | 独立 State + 独立 checkpointer | `brief / plan_steps / db_result / draft / hard_check_result / summary` —— **仅子图可见** | 主图看不到；**参谋之间互不可见**（s06 隔离） |
| ④ | **大对象 / 跨轮** | 文件系统 + `checkpointer(thread_id)` | `plans/*.json`、`trace/*.log`、兵力池 SQLite（**State 只存路径，不存正文**）；跨轮恢复槽位·约束·方案版本 | 正文不进 State，否则 checkpointer 每次序列化拖垮性能 |

### 1.1 reducer 语义（决定 merge 行为，写错就是静默 bug）

| 声明 | 行为 | 用途 |
|---|---|---|
| 普通字段 | **后写覆盖** | `route / negotiation_round / retry_count / final_plan` |
| `Annotated[list, add]` | **追加（只增不减）** | `staff_reports / blackboard` |
| 未声明字段 | **被 LangGraph 静默丢弃** | ⚠ 实测踩过：`clarify_question` 输出 `null` |

### 1.2 三个必须绕开的坑（均实测验证）

1. **`add` 只增不减** → 失败恢复重跑会累积重复报告、资源申请翻倍。解法：`latest_by_domain()` 取每域最新版本。
2. **节点同时挂"条件分发边"和"出边"会无限自旋**（参谋自己给自己派活）。解法：分发必须是独立 `dispatch` 节点。
3. **字段未声明即丢弃** → 新增 State 字段时必须在 `JointOpsState` 里同步声明。

> **推论**：正因为通道 ②/③ 不共享内存，才有了契约 C1（简报必须自包含）——子图看不到任何历史，所有信息必须随 `Send` 一起拷进去。

---

## 2. 节点实现规范

### 2.1 节点总表（输入 → 处理 → 输出 → 失败）

| 节点 | 读 State | 处理（是否调 LLM） | 写 State（patch） | 失败处理 |
|---|---|---|---|---|
| `intent` | `user_input`、`escalate_to` | 🟡 LLM：意图分类 → 填任务单 schema；**规则**：四态路由判定 | `task_order`、`route`、（`clarify_question`） | 分类置信度低 → `need_clarify` |
| `reject` | `route` | 规则：模板拒答 | `final_plan = REJECTED_OUT_OF_SCOPE` | — |
| `clarify` | `clarify_question` | 规则：模板输出 | `final_plan = NEED_USER_CLARIFICATION` | — |
| `commander` | `task_order`、`validation_failed`、`failure_reason` | 🔴 LLM：拆解 → 任务 DAG + 约束包；**规则**：约束合法性校验 | `task_order`（含 `constraints`）、`blackboard` | 约束自相矛盾 → 直接置 `infeasible` 让终验归因 |
| `dispatch` | `task_order` | **规则**：参与者裁剪 + `Send` 分发 | 无（返回 Send 列表） | 参谋失败由 `collect` 降级 |
| `staff`×3（子图） | `brief`（Send 入参） | 🟡 查库改写 → SQL → 🔴 拟案 → **规则**：硬规则自纠 → 落盘 + 🟡 摘要 | `staff_reports += 1 条` | 超限/异常 → 返回 `error_code` 条目 |
| `collect` | `staff_reports` | **规则**：取最新版本 · 冲突检测 · 快通道旁路 · 失败降级 | `current_asks`、`conflicts`、`info_gap`、（快通道）`final_plan` | 单参谋失败 → 其余继续 + 标注信息缺口 |
| `negotiate` | `current_asks`、`conflicts`、`blackboard` | 🔴 LLM：仅拉冲突方做增量修订 | `negotiation_round+1`、`current_asks`、`blackboard += 本轮` | 修订后立刻跑硬规则 |
| `check_convergence` | `blackboard`、`negotiation_round`、`current_asks` | **规则**：CV-1~CV-5 短路判定 | `converged` / `oscillating` | — |
| `arbitrate` | `conflicts`、`current_asks`、`constraints` | **规则**：硬规则过滤 → 🔴 LLM：软冲突一次性折中 | `final_asks`、`final_plan` | — |
| `validate` | `final_plan`、`final_asks`、`task_order` | **规则**：硬规则终验 + 完整性闸门 + **失败归因** | `validation_failed`、`failure_type`、`escalate_to`、`retry_count+1` | → 分层回退 |
| `compose_plan` / `respond` | `final_plan`、`blackboard` | 规则：合成 + 让步记录 | `final_plan` | — |
| `extract_memory` / `checkpoint` | 全部 | 🟡 记忆提取 / 持久化 | `memory_index` | — |

### 2.2 关键实现要点（按节点）

**`intent`** —— 产出**结构化任务单**而非自由文本；路由是**读字段**决定的，不是模型"感觉"。

```python
def intent(state):
    order = llm_json(LIGHT, INTENT_SCHEMA, state["user_input"])   # 强制 JSON 输出
    order["constraints"] = merge_slots(state.get("task_order"), order)  # 多轮槽位合并
    route = ("out_of_scope" if order["domain"] == "OOS"
             else "need_clarify" if not order.get("constraints")
             else "single_domain" if not order["need_joint"] else "full_chain")
    return {"task_order": order, "route": route}
```

**`commander`** —— 唯一有权下发约束的节点；失败恢复时**带失败原因**重下发（不改任务单，只改约束与分工）。

```python
def commander(state):
    to = dict(state["task_order"])
    if state.get("validation_failed") and state.get("escalate_to") == "commander":
        to["constraints"] = tighten(to["constraints"], state["failure_reason"])  # 收紧，不改目标
    to["tasks"] = llm_json(MAIN, DAG_SCHEMA, to)      # 任务 DAG（blockedBy 依赖边）
    return {"task_order": to, "blackboard": [{"speaker": "commander", "event": "下发全局约束"}]}
```

**`dispatch`** —— 参与者裁剪：单域只派 1 个；跨域派 3 个；协商时**只派冲突相关方**（通常 2 个）。

**`staff` 子图** —— 内部是 s20 式小循环（读简报 → 列步骤 → 查库 → 拟案 → 硬规则自纠 → 落盘 → 摘要）。`max_turns=30` 超限返回 `MAX_TURNS`。

**`validate`** —— 三件事合一（终验 + 完整性 + 归因），**全程 0 API**：

```python
def validate(state):
    ctx = RuleContext(**state["task_order"]["constraints"], ...)   # 约束来自指挥层，不是模型自定
    verdict = attribute_failure(state["plans"], state["final_asks"], ctx, state["conflicts"])
    if not verdict["failure_type"]:
        return {"validation_failed": False, "final_plan": {**plan, "hard_rules": "PASS", "completeness": "PASS"}}
    if (state.get("retry_count") or 0) >= 2:
        return {"validation_failed": False, "final_plan": {"status": "FAILED_VALIDATION_DEGRADED", ...}}
    return {"validation_failed": True, "retry_count": retry + 1,
            "failure_type": verdict["failure_type"], "escalate_to": verdict["escalate_to"],
            "failure_reason": verdict["reasons"]}
```

### 2.3 三份 prompt 骨架（契约 C1 的落地）

**① 参谋简报（自包含，Send 入参）**

```
# 任务式简报 · {domain}参谋（你看不到任何历史，以下即全部信息）
## 1 任务单（不可修改）
{ task_order JSON }
## 2 全局约束（越权即打回，你无权修改）
时间窗 / 兵力上限 / 禁区 / 优先级 / ROE
## 3 本域可用兵力（SQL 查询结果，数字唯一来源）
{ units JSON，每条带 source: sql:<query_id> }
## 4 共享资源可用窗口
{ transport / fires 容量 }
## 5 输出契约
写 plans/{domain}.v{n}.json（schema 见下）；回传 ≤200 字摘要，摘要中不得出现任何结论数字
## 6 禁止
修改约束 · 引用非 SQL 数字 · 编造单位编号 · 与其他军种直接通信
```

**② 协商增量消息（Round N）**

```
# 协商 Round {r} · {domain}
## 争议资源   {conflicts：谁、超了多少}
## 你上一轮  申索 {last_ask} → 已让步 {concession}
## 对方反对  {blackboard 中针对你的 objection}
## 硬上限    {cap}（不可突破，突破即整轮作废）
## 要求      只输出：修订后 asks + ≤50 字理由 + 本轮让步说明
```

**③ 指挥层恢复重下发（失败回环）**

```
# 重下发 · 第 {retry} 次
## 上次失败原因（归因结果）  {failure_type + reasons}
## 不变  任务目标、领域、输出契约
## 需调整  全局约束（收紧项已标出）
## 要求  重新拆解或收紧约束后下发；不得更改任务目标
```

---

## 3. 硬规则怎么定

### 3.1 三处调用时机（同一套引擎，不是三套）

| 时机 | 位置 | 目的 |
|---|---|---|
| **A 参谋拟案后** | `staff` 子图 `hard_rule_check` | 自纠：不让违规方案出子图 |
| **B 协商每轮修订后** | `negotiate` 结束前 | 防"谈出一个违规方案"还得回头拆 |
| **C 终验** | `validate` | 最后闸门 + 完整性 + 失败归因 |

### 3.2 规则表（加规则 = 加一个函数，引擎不动）

| ID | 规则 | 判据 | 参数来源 | 违规动作 |
|---|---|---|---|---|
| **HR-01** | 兵力上限 | `Σ 投入兵力 ≤ force_cap` | 约束包 | 打回 + 记 violation |
| **HR-02** | 共享资源申索 | `asks[r] ≤ resource_cap[r]` | 资源容量表 | 打回 |
| **HR-03** | 时间窗 | `max(timeline.end) ≤ deadline` | 约束包 | 打回 |
| **HR-04** | 禁区 | `plan.areas ∩ no_go_zones = ∅` | 约束包 | 打回 |
| **HR-05** | 交战规则 ROE | `fire_level ∈ roe_allowed` | 约束包 | 打回 |
| **HR-06** | **编成真实存在** | 每个 `unit_id` 必须在兵力池中 | 兵力池 SQL | 打回（**防幻觉单位**） |
| **HR-07** | **数字可溯源** | 每个数字字段须带 `source: sql:<query_id>` | 查询结果 | 打回（**防心算**） |
| **HR-08** | 产物完整性 | 必填章节齐备（见 §3.4） | 输出 schema | 归因 `artifact_incomplete` |

> HR-06 / HR-07 是"数字可追溯"这条硬原则的**机器可判形式**——没有它们，"禁止模型编造"只是句提示词。

### 3.3 约束配置示例（`config/constraints.yaml`）

```yaml
force_cap: 30000              # 兵力上限（人）
deadline: 3                   # 时间窗 D+3
no_go_zones: ["禁区A", "禁区B"]
roe_allowed: ["常规", "有限"]
resource_cap:                 # 共享资源容量
  transport: 50               # 运输机架次
  fires: 15                   # 火力支援单位
max_negotiation_rounds: 3
retry_cap: 2
max_turns: 30
```

**约束只能由 `commander` 从配置/任务单生成，参谋无权读写此文件**（s03 权限闸门）。

### 3.4 产物 Schema（完整性闸门依据）

```json
{
  "domain": "army",
  "units":   [{"unit_id": "A-001", "personnel": 4200, "source": "sql:q12"}],
  "asks":    {"transport": 30, "fires": 10},
  "ask_sources": {"transport": "sql:q07"},
  "timeline":[{"phase": "装载", "start": "D+0", "end": "D+2"}],
  "areas":   ["XX海域"],
  "fire_level": "常规",
  "sections": ["时序表", "风险", "资源来源"]
}
```

### 3.5 违规 → 归因 → 回退（根因优先）

```
① 约束自相矛盾（需求在给定运力/上限下无解） → constraint_conflict → 回 intent（升级人工澄清）
② 方案违反硬规则                            → plan_violation      → 回 commander（重下发约束）
③ 产物缺章节 / 数字无源                      → artifact_incomplete → 回 arbitrate（局部修复，不重跑参谋）
```

**约束不可满足的机器判据**（非 LLM 估算）：

```python
min_days = ceil(required_personnel / (transport_cap × per_sortie_capacity × sorties_per_day))
if min_days > deadline: → constraint_conflict
```
实测：投送 8 万人、运力 10 架次 → `min_days = 5 > 3` → 判定约束矛盾（**即"3 天投送 8 万人"这个经典无解输入**）。

---

## 4. 收敛规则（哪些）

### 4.1 五条判据（全部机器可判，0 API）

| ID | 判据 | 定义 | 结果 |
|---|---|---|---|
| **CV-1** | 无冲突 | `conflicts = ∅` | ✅ 收敛 |
| **CV-2** | 轮次封顶 | `round ≥ max_negotiation_rounds(3)` | ⛔ 强制裁决 |
| **CV-3** | 边际停滞 | `(total[r-1] − total[r]) / total[r-1] < 2%` | ⛔ 停止（再谈无收益） |
| **CV-4** | 无新增反对 | 本轮 `objections` ⊆ 历史已提 | ✅ 收敛（谈无可谈） |
| **CV-5** | 震荡 | 任一方 `ask_p[r] > ask_p[r-1]`（让步被收回） | ⛔ 强制裁决 |

### 4.2 判定顺序（短路，先判最刚性的）

```
CV-2 轮次封顶 > CV-5 震荡 > CV-1 无冲突 > CV-4 无新反对 > CV-3 边际停滞 > continue
```
震荡优先于无冲突：一旦检出让步被收回，即使本轮恰好无冲突也要强制裁决——否则参谋会学会"反复横跳"。

### 4.3 两条易错认知

- **收敛 ≠ 一致同意。** 收敛只表示"不再有新增信息"，最终仍要进 `arbitrate` 做一次性折中并记录让步。
- **封顶不是失败。** 轮次到顶、震荡、停滞都是**设计内的正常出口**，产物是"带残余风险的裁决方案 + 完整让步记录"，不是报错。

### 4.4 防震荡的版本 diff

每轮结束时对 `asks` 做逐方逐资源 diff：任一项**不降反增**即标记 `oscillating`，直接退出协商。**记录必须落盘**（`blackboard` / `negotiation_round` 表）——用户事后问"为什么这么定"，要能翻出第 2 轮谁让了什么。

---

## 5. 实测记录（`rules_convergence.py`，14/14 PASS）

| # | 用例 | 结果 |
|---|---|---|
| R1 | 正常方案通过硬规则 | PASS |
| R2 | 超兵力上限（35000 > 30000） | PASS · `HR-01` |
| R3 | **幻觉单位** A-999 不在兵力池 | PASS · `HR-06` |
| R4 | 数字无 `sql:` 来源 | PASS · `HR-07` |
| R5 | 禁区 + ROE 双违规 | PASS · `HR-04` + `HR-05` |
| R6 | 冲突检测（transport 55>50 **且** fires 18>15） | PASS · 两项同时检出 |
| R7 | 第 1 轮有冲突 → `continue` | PASS |
| R8 | 第 2 轮无冲突 → `converged` | PASS |
| R9 | 让步被收回 → `oscillating` | PASS |
| R10 | 轮次封顶 → `cap_reached` | PASS |
| R11 | 边际停滞（降幅 0%）→ `stagnated` | PASS |
| R12 | 3 天投送 8 万人 / 10 架次 → `constraint_conflict` → 回 `intent` | PASS |
| R13 | 方案违规 → `plan_violation` → 回 `commander` | PASS |
| R14 | 产物残缺 → `artifact_incomplete` → 回 `arbitrate` | PASS |

> R6 初跑报 FAIL，排查后是**测试断言写错**（漏算 fires 也超限），引擎判对了——这类"期望错而非实现错"的情况，正是规则必须写成可执行代码的理由之一。

---

## 6. 与主文档的关系

| 本文回答 | 主文档 § |
|---|---|
| State 互通四通道 + reducer 语义 | §4.2 State 关键字段 |
| 节点实现规范 | §4.3 节点清单与 API 档位 |
| 硬规则 HR-01~HR-08 | §4.3「三个判定环节永远 0 API」 |
| 收敛判据 CV-1~CV-5 | §4.1 四个循环（协商回环） |
| 失败归因与分层回退 | §4.5 分层回退 + 归因分派 |
