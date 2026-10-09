# RCA Agent 智能化改造方案（讨论稿）

> 状态：**P0 + P1 + P2 + P3 全部实施并验证通过**（2026-02）
> 目标：把"关键词匹配 + 固定权重打分"的规则引擎，升级为**大模型驱动、遵循 观察→推理→行动 标准循环**的运维诊断智能体；同时保留本体确定性分析能力。

---

## LLM 路由（最终决策）

```
agnes-3.0-flash（主）
   └─ 不可用 → rsxermu666 / claude-opus-5-5（跨 provider 容灾）
                 └─ 仍不可用 → deepseek-chat
```

---

## P2 / P3 实施概要

### P2 — 流式推理

| 交付 | 说明 |
|---|---|
| `POST /api/agent/diagnose/stream` | SSE 流：connected/route/run_start/step_start/reasoning/tool_call/observation/step_end/final/error |
| `frontend/src/stores/agentStore.ts` | fetch 流式 SSE 解析 + 步骤状态机 |
| `frontend/src/components/ReasoningTrace.tsx` | 推理轨迹视图（观察/推理/行动时间线 + 证据链 + 建议动作） |
| `AgentPage` 集成 | 新增「🧠 推理」标签，发送按钮走流式诊断 |

### P3 — 成本 / 限流 / 自演化

| 交付 | 说明 |
|---|---|
| `services/rate_limiter.py` | 滑动窗口限流（默认 10 次/分钟）+ 各模型定价表 |
| `services/evolution_trigger.py` | 轨迹累积检测（≥30 条或 ≥7 天）+ `start_evolution_run` |
| `record_diagnosis_trajectory()` | 每次诊断回填 EvoOntology ontology task（prepare→start_task→record_event→finish_task） |
| `GET /api/agent/cost` | 成本概览（总 token、估算 USD、价格表） |
| `GET /api/agent/rate-limit` | 限流状态 |
| `GET /api/agent/evolution` | 自演化触发条件检测 |
| `POST /api/agent/evolution/trigger` | 显式触发自演化（需人工确认） |

### 顺手修复：Agent 发现的真实指标 bug

Agentic 路径诊断出的 `cc_mysql_pool_limit` 恒为 0 问题，修复如下：

| 根因 | 修复 |
|---|---|
| `getDb()` 每次调用都新建连接池（连接从不复用） | 改为模块级缓存池 |
| `metrics.k.set()` 藏在 try/catch 里，`_allConnections.filter` 抛异常时连带 limit 未设置 | 抽 `updatePoolMetrics()`，`finally` 兜底保证 limit 正确 |

修复后 `cc_mysql_pool_limit = 10`（原恒为 0）。**这是 LLM Agent 结合实测证据识别出的、规则引擎无法发现的故障。**

### 验证结果（累计 182 项全通过）

| 套件 | 结果 |
|---|---|
| LLM 客户端 / 工具层 / Agent 循环 / 双引擎 / 容器化 | 143 项 |
| P2+P3（SSE/成本/限流/自演化/前端推理视图） | 27/27 |
| Agentic SSE 完整事件流 | 12/12 |

### 实测亮点：LLM 识破规则引擎的陈旧结论

Agentic 路径对"P99+连接池耗尽"告警的真实诊断：

> `cc_mysql_pool_limit` 恒为 0（**指标暴露缺陷**），导致"连接池耗尽"告警恒被误触发；
> 实测 P99≈5ms、连接池 active 恒 0、MySQL 连接 2/200、无慢查询 →
> **本条 P1 是误报/陈旧告警**，根因应为 `ds:pay`（配置类）。

`seed_agreement=False` 正确标出：确定性引擎的 `rc:slow-sql` 结论与实测不符。

---

## 决策记录（Q1–Q7 全部确认）

| # | 决策项 | 确定结论 |
|---|---|---|
| Q1 | LLM 选型 | `agnes-2.5-pro` 为主 + `agnes-3.0-flash` 兜底，并支持跨 provider 容灾（→ `rsxermu666/claude`） |
| Q2 | 双路径策略 | **A：本体优先 + Agent 增强** |
| Q3 | 写操作 | **纯只读**，修复建议仅文本输出、人工执行 |
| Q4 | 预算上限 | `max_steps=8` / 超时 `180s` / `60k tokens` |
| Q5 | 流式体验 | SSE 流式（P2 实施） |
| Q6 | 实施范围 | **分阶段：先 P0+P1** |
| Q7 | 保留纯规则模式 | 保留（LLM 不可用时降级 + 交叉验证基线） |

---

## 实施状态

### 已完成并验证

| 模块 | 文件 | 状态 |
|---|---|---|
| LLM 配置与 Key 解析 | `services/llm_config.py` | ✅ 支持 env / .env / DSH 凭据三级解析 |
| LLM 客户端 | `services/llm_client.py` | ✅ OpenAI + Anthropic 双协议、Function Calling、思维链捕获、跨 provider 降级 |
| 工具基础设施 | `services/tools/base.py`、`tools/__init__.py` | ✅ 注册表、JSON-Schema 校验、注入防护、脱敏 |
| 本体层工具（5） | `services/tools/ontology_tools.py` | ✅ 拓扑/解析/约束/确定性诊断/搜索 |
| 监控层工具（5） | `services/tools/metrics_tools.py` | ✅ 即时/区间/告警/目标/快照 |
| 环境层工具（12） | `services/tools/env_tools.py`、`db_tools.py` | ✅ Docker 5 个 + MySQL 7 个（含锁等待取证） |
| 知识层工具（3） | `services/tools/history_tools.py` | ✅ 历史事件/人工反馈/本体证据 |
| ReAct Agent 循环 | `services/agent.py` | ✅ 观察→推理→行动、预算熔断、最后一步摘工具 |
| 双路径路由 | `services/router.py` | ✅ 已知根因放宽门槛（0.75）、模糊症状走 Agent |
| 诊断编排 | `services/orchestrator.py` | ✅ 快检→路由→执行→落库 统一入口 |
| Agent 持久化 | `services/agent_store.py` | ✅ `rca_agent_run` + `rca_agent_thought`，支持回放 |
| Agent API | `routers/agent.py` | ✅ diagnose/runs/tools/config/health/stats |
| 只读取证账号 | `monitoring/mysql/init/03_readonly_user.sql` | ✅ PROCESS + performance_schema，无写权限 |

### 关键问题与修复（实施中发现）

| # | 问题 | 影响 | 修复 |
|---|---|---|---|
| 1 | `_rows()` 默认 `limit=100` | `SHOW GLOBAL STATUS`(494行)/`VARIABLES`(631行) 被截断，查不到目标变量 | `limit=None` 表示不截断 |
| 2 | `appuser` 缺 PROCESS 权限 | InnoDB 状态/锁信息不可用 | 新建最小权限只读账号 `rca_readonly` |
| 3 | `incident_store` 连接未用 DictCursor | `agent_store` 全部字典访问失效（run 查不到、thoughts 为空 dict） | 显式 `cursor(DictCursor)` |
| 4 | 拓扑 BFS 每次 2.3s | 快路径慢 | 进程内缓存（版本号+mtime 失效），**2.3s → 0ms** |
| 5 | Prometheus 19 次串行查询 12.6s | 快路径慢 | 线程池并发，**12.6s → ~2s** |
| 6 | router 阈值 0.85 过高 | 已知模式 `rc:slow-sql`(0.83) 被误判走 Agent，浪费成本 | 命中已建模根因时放宽至 0.75 |
| 7 | **nginx `proxy_read_timeout 60s`** | **Agent 最长 180s → UI 上必然 504** | 提升至 300s（生产阻断级 bug） |
| 8 | 观察文本过长（4000/2500 字符） | 单次诊断 40k+ tokens | 降至 1600/900，并行查询 |

### 验证结果

| 套件 | 脚本 | 覆盖 | 结果 |
|---|---|---|---|
| LLM 客户端 | `tools/llm_client_verify.py` | 双协议、Function Calling、思维链、错误处理 | 14/14 |
| 工具层 | `tools/agent_tools_verify.py` | 25 工具真实取证 + 安全拦截 | 42/42 |
| Agent 循环 | `tools/agent_loop_verify.py` | 观察→推理→行动全链路 | 31/31 |
| 双引擎 | `tools/dual_engine_verify.py` | 路由判定 + 两条路径 + 落库回放 | 34/34 |
| 容器化 | `tools/container_agent_verify.py` | nginx 代理 + 容器内环境工具 | 22/22 |

**合计 143 项验证全部通过。**

### 实测效果对比

| 维度 | 改造前（纯规则） | 改造后 |
|---|---|---|
| 已知模式 `P99+连接池` | 返回 `rc:slow-sql`，无证据 | **确定性快路径**，7.5s / **0 token** |
| 同类问题走 Agent | — | `rc:slow-sql`，5 步 / 9 工具 / 46k token / **9 条证据链** |
| 模糊症状 `晚上8点偶尔慢` | ❌ 无关键词 → 通用回复 | Agent 自主选工具探索 → 形成假设 |
| 推理可解释性 | 打分表 | **思维链 + 逐步工具观察 + 证据链**，全部落库可回放 |
| 交叉验证 | 无 | Agent 结论 vs 本体结论自动比对（exact/category/divergent） |

### 未实施（P2/P3，按 Q6 决策留待下阶段）

> **状态更新（2026-10-08）**：本清单写于 10-02，其中三项**后续已完成**，逐条标注如下。
> 保留原清单是为了看清"当初打算做什么"与"后来实际做到了什么"的差别。

- ~~SSE 流式推送（`on_event` 回调已就绪，前端推理视图待做）~~
  → ✅ **已完成**：后端 `POST /api/agent/diagnose/stream`（`text/event-stream`），
  前端通过 `LiveTurn` 消费流式事件。
- ~~前端「推理过程」标签页~~
  → ✅ **已完成（且做得更好）**：推理过程没有做成独立标签页，而是**直接嵌入聊天框**
  （`ReasoningInline`，随对话流展开），右侧面板另保留 `ReasoningTrace` 汇总视图。
- **成本看板与限流** → ⚠ **部分完成**：后端已有 `GET /api/agent/cost`（总 token +
  单价估算 + `MODEL_PRICE_PER_1M_TOKENS`）、`GET /api/agent/rate-limit`（限流状态），
  单次运行的 tokens/耗时也已显示在推理视图里；**但仍没有聚合成本看板页面**
  （前端只有 `agent / chaos / stress` 三个标签）。这条**仍未做**。
- ~~轨迹驱动的本体自演化闭环~~
  → ✅ **已完成 5 轮**：`ontology_v1..v5`，每轮都走 EvoOntology 协议
  （`save_version → validate → record_evaluation ×2 → accept`）+ 成对评估闸门，
  回退均为 0；详见 `docs/EvoOntology_接入手册.md` §8 与验证手册 §11.22–§11.24。
  说明：这几轮由**评估指标与实测有效性**驱动（而非"轨迹条数达阈值"触发），
  触发条件与原设想的"轨迹积累到 30 条"不同。

### 仍未做（截至 2026-10-08）

- **动作的自动执行 / 回滚 / 验证闭环**（当前 `remediation` 只给命令与审批要求，
  不自动执行；见验证手册 §11.21 第 16 项）
- **成本按模型精算**（看板已交付，但估算用"总 token × $1/1M"的粗口径，未区分输入/输出）
- **TTL ↔ EvoOntology 双向映射**、**多集群/跨命名空间验证**（见 EvoOntology 手册 §6 路线图）

> 成本看板已于 2026-10-08 交付（验证手册 §11.30）：导航栏「💰 成本」页，
> 复用 `/api/agent/cost`、`/rate-limit`、`/evolution`。

---

## 一、现状诊断（改造前，供回溯）

### 1.1 当前实现（事实）

| 模块 | 实现方式 | 代码量 |
|---|---|---|
| `chat.py` | `is_rca_request()` 关键词列表匹配 → 拼 Markdown 模板 | 219 行 |
| `rca_engine.py` | `parse_alert()` 关键词分类 + `score_candidates()` 固定权重打分 | 440 行 |
| `TOOLS` 常量 | 定义了 5 个工具，**从未被任何 LLM 调用** | — |
| `/api/chat/tool` | 端点存在，但无调用方（前端不发 tool_calls） | — |

### 1.2 核心问题

1. **无 LLM**：整个诊断链路是 Python 规则，没有模型参与推理
2. **能力封闭**：只能识别 `KEYWORD_PATTERNS` 里预置的 8 类关键词，超出即失效
3. **无权探索**：Agent 无法主动"观察"环境（看容器日志、查进程列表、读配置）
4. **无假设验证**：一次打分定结果，不能"提出假设→取证→修正"
5. **不可解释**：输出的是打分表，不是推理过程
6. **`TOOLS` 是死代码**：声明了工具但没有 agent loop 去用

### 1.3 已具备的良好基础

- ✅ 本体图遍历（16 节点 / 20 边）与约束建模 —— **这正是 LLM 需要的领域知识**
- ✅ Prometheus 指标采集与聚合
- ✅ MySQL 持久化（incident / trajectory / feedback）
- ✅ 前端多面板 UI 与拓扑可视化
- ✅ 7 条告警规则（带 `rca_hint` / `onto_constraint`）

**结论**：不需要推倒重来，而是**在现有确定性内核之上加一层 LLM Agent 循环**。

---

## 二、Dify Agent 机制研究结论

参考实现：`api/core/agent/base_agent_runner.py`、Agent Strategy Plugin（ReAct 策略）

### 2.1 核心循环（ReAct 策略）

```python
iteration_step = 1
max_iteration_steps = maximum_iterations     # 默认 3
while run_agent_state and iteration_step <= max_iteration_steps:
    run_agent_state = False

    if iteration_step == max_iteration_steps:
        self._prompt_messages_tools = []      # 关键：最后一轮摘掉工具，强制收尾

    prompt_messages = self._organize_prompt_messages(agent_scratchpad, query)
    # 调 LLM（stream + stop=["Observation"]）
    scratchpad = parse_react_output(chunks)   # → thought / action / action_input

    if not scratchpad.action:
        final_answer = scratchpad.thought           # 无 action 即最终答案
    elif action.action_name.lower() == "final answer":
        final_answer = action.action_input          # 显式终结
    else:
        run_agent_state = True                      # 有工具调用 → 继续
        observation = invoke_tool(action)
        scratchpad.observation = observation

    iteration_step += 1
```

### 2.2 值得借鉴的关键设计

| 设计点 | Dify 做法 | 价值 |
|---|---|---|
| **最后一轮摘工具** | `if iteration_step == max: tools = []` | 防止无限调用，强制模型给结论 |
| **stop token** | `stop.append("Observation")` | 让模型停在行动前，由外部执行工具 |
| **Scratchpad 累积** | 每轮把 `Thought/Action/Observation` 拼进 assistant message | 保留完整推理轨迹 |
| **追加 "continue"** | prompt 末尾加 `UserPromptMessage("continue")` | 驱动多轮继续 |
| **工具不存在不崩** | 返回 `"there is not a tool named X"` 作为 observation | 模型可自我纠正 |
| **结构化思考记录** | `MessageAgentThought(thought, tool, tool_input, observation, answer, tokens, latency, position)` | 可观测 + 可回放 |
| **分层日志** | ROUND → LLM Thought → CALL tool，各有 start/finish | 前端可实时展示进度 |
| **工具 JSON-Schema** | `PromptMessageTool(name, description, parameters)` | OpenAI function-calling 标准 |

### 2.3 我们要改进的地方

Dify 的 ReAct 靠**文本解析**（`CotAgentOutputParser`）提取 Action，脆弱。
我们改为**优先 Function Calling**（结构化 `tool_calls`），理由：

- 可用的 `agnes` provider 是 OpenAI 兼容 API，原生支持 `tools` 参数
- 无需正则解析，无格式漂移
- 但**仍要求模型输出 `reasoning` 字段**（system prompt 约定），保留思考可解释性

---

## 三、目标架构

### 3.1 总览：双引擎 + 三阶段循环

```
                     ┌──────────────────────────────┐
  告警 / 用户提问 ──▶ │  Router（意图与复杂度判定）    │
                     └───────────┬──────────────────┘
                                 │
              ┌──────────────────┴───────────────────┐
              ▼                                      ▼
  ┌───────────────────────┐            ┌─────────────────────────────┐
  │ 快路径：本体确定性引擎  │            │ 慢路径：LLM Agent Loop       │
  │ (RCAEngine, <100ms)   │            │ (ReAct + Function Calling)   │
  │                       │            │                              │
  │ • 拓扑 BFS            │  结果作为   │  while step <= max_steps:    │
  │ • 约束匹配            │──首个观察──▶│    ① Observe 采集环境        │
  │ • 加权打分            │  注入       │    ② Reason  LLM 推理        │
  │ • 传播路径            │            │    ③ Act     调工具取证       │
  └───────────────────────┘            └──────────────┬──────────────┘
              │                                       │
              └───────────────┬───────────────────────┘
                              ▼
                   ┌──────────────────────┐
                   │ 结论融合 + 置信度校准  │
                   │ 落库 (incident/traj)  │
                   │ SSE 推送前端          │
                   └──────────────────────┘
```

### 3.2 确定性 vs 非确定性 的职责边界

| 维度 | 确定性（本体引擎） | 非确定性（LLM Agent） |
|---|---|---|
| **输入** | 告警关键词 | 告警 + 本体快照 + 环境快照 + 历史 |
| **方法** | 图遍历 + 约束匹配 + 规则打分 | ReAct 循环，自主选工具 |
| **优势** | 可复现、可审计、毫秒级、零幻觉 | 处理未知模式、跨层关联、假设验证 |
| **劣势** | 只认识预置模式 | 有幻觉风险、秒级、有成本 |
| **输出** | 根因 + 置信度 + 传播路径 | 根因假设 + 证据链 + 排查步骤 |
| **适用** | 已知故障模式 | 新型故障、复杂关联 |

**融合策略（关键设计）**：

```python
def diagnose(alert):
    # 阶段 A：确定性快检（始终执行，成本极低）
    fast = ontology_engine.infer(alert)
    
    # 阶段 B：路由判定
    if fast.confidence >= 0.85 and fast.matched_constraint:
        # 高置信度命中已知约束 → 直接返回，但附带环境证据
        fast.evidence = collect_standard_evidence()
        return fast, mode="deterministic"
    
    # 阶段 C：启动 LLM Agent，把 fast 结果作为"首个观察值"
    agent = RCAAgent(
        alert=alert,
        seed_observation=fast,        # ← 本体结论作为起点，避免从零猜
        tools=TOOL_REGISTRY,
        max_steps=8,
    )
    return agent.run(), mode="agentic"
```

**要点**：本体结果不是被替代，而是作为 Agent 的**先验知识**注入上下文，
让 LLM 在正确的假设空间里探索，而不是漫无目的地调工具。

### 3.3 标准循环的三阶段实现

严格对应「观察 → 推理 → 行动」：

```
┌─ Step N ─────────────────────────────────────────────────────┐
│                                                              │
│  ① OBSERVE（观察）                                            │
│     · 上一轮工具返回的 observation                             │
│     · Prometheus 指标快照（自动注入）                          │
│     · 本体拓扑与约束（自动注入）                                │
│     · 环境指纹（容器列表/状态，自动注入，轻量）                  │
│                                                              │
│  ② REASON（推理）                                             │
│     · LLM 接收 system(角色+工具+本体知识) + scratchpad         │
│     · 输出：reasoning（自然语言思考）+ tool_calls 或 final      │
│     · 推理写入 AgentThought(reasoning)                        │
│                                                              │
│  ③ ACT（行动）                                                │
│     · 执行 tool_calls（可并行多个）                            │
│     · 结果写入 AgentThought(observation)                      │
│     · 工具异常 → 错误文本作为 observation，不中断循环           │
│                                                              │
│  终止条件：                                                   │
│     · 模型返回 final_answer                                   │
│     · step == max_steps（最后一轮摘掉工具，强制收尾）           │
│     · 超时 / token 预算耗尽 → 返回当前最优假设                  │
└──────────────────────────────────────────────────────────────┘
```

---

## 四、工具矩阵设计

这是「让 Agent 能真正观察环境」的核心。分四层，**全部只读**：

### 4.1 本体层（确定性知识）

| 工具 | 作用 | 返回 |
|---|---|---|
| `ontology_topology` | 获取完整拓扑图 | `{nodes[16], edges[20]}` |
| `ontology_resolve` | 解析术语并展开关联边 | 术语 + 关系 + 约束 + 证据 |
| `ontology_diagnose` | **调用确定性引擎** | 根因 + 置信度 + 传播路径 |
| `ontology_constraints` | 列出相关约束及当前满足状态 | `con:p99-threshold` 等 |
| `ontology_search` | 语义搜索本体概念 | 匹配术语列表 |

> `ontology_diagnose` 让 LLM 可以"咨询"确定性引擎，二者协同而非互斥。

### 4.2 监控层（指标时间序列）

| 工具 | 作用 | 数据源 |
|---|---|---|
| `metrics_query` | PromQL 即时查询 | Prometheus |
| `metrics_range` | PromQL 区间查询（看趋势） | Prometheus |
| `metrics_alerts` | 当前 firing/pending 告警 | Alertmanager/Prometheus |
| `metrics_targets` | 抓取目标健康状态 | Prometheus |
| `metrics_summary` | 关键指标聚合快照 | 现有 `all_metrics()` |

### 4.3 环境层（真实环境观测）★ 新增重点

| 工具 | 作用 | 实现 |
|---|---|---|
| `env_containers` | 容器列表（名称/状态/镜像/重启次数） | `docker ps` 结构化 |
| `env_container_inspect` | 容器详情（资源限制/网络/挂载/环境变量脱敏） | `docker inspect` |
| `env_container_logs` | 容器日志尾部（关键字过滤） | `docker logs --tail` |
| `env_container_stats` | CPU/内存实时占用 | `docker stats --no-stream` |
| `env_host_info` | 宿主机 CPU/内存/磁盘/负载 | WMI / `/proc` |
| `db_status` | MySQL 全局状态 | `SHOW GLOBAL STATUS` |
| `db_processlist` | 当前连接与运行中的 SQL ★ 查锁等待 | `information_schema.processlist` |
| `db_innodb_status` | InnoDB 事务与锁 ★ | `SHOW ENGINE INNODB STATUS` |
| `db_slow_queries` | 慢查询日志摘要 | `slow_query_log` |
| `db_variables` | 关键配置变量 | `SHOW VARIABLES` |

> **`db_processlist` + `db_innodb_status`** 是新增的关键取证能力 ——
> 本次故障演练中"t_txn 行锁导致 INSERT 阻塞"正是这类工具才能直接定位的。

### 4.4 知识层（历史与反馈）

| 工具 | 作用 |
|---|---|
| `history_search` | 检索相似历史 incident（按根因/症状） |
| `history_feedback` | 查询历史结论的人工反馈（正确/错误） |
| `ontology_evidence` | 查询本体中已有的证据记录 |

> `history_feedback` 让 Agent 知道"上次同样症状判成了 X，但被标记为误判"，
> 形成经验纠正闭环，也是本体自演化的输入。

### 4.5 安全边界（硬性约束）

```python
TOOL_POLICY = {
    "read_only": True,                    # 全部工具只读，禁止任何写操作
    "forbidden": [
        "docker restart/stop/kill/rm",
        "KILL <session>",
        "ALTER/DROP/UPDATE/DELETE",
        "修改配置/重启服务",
    ],
    "injection_guard": "参数白名单 + 不使用 shell=True + 拒绝含 ; | & $ ` 的输入",
    "redaction": ["password", "token", "secret", "key", "PASSWORD"],
    "limits": {"max_steps": 8, "timeout_s": 180, "max_tokens": 60000},
}
```

**明确不做**：Agent 不会执行任何修复动作。所有建议以文本输出，由运维人工执行。
（如需自动化修复，应作为**独立的、需二次确认的**能力单独立项。）

---

## 五、技术选型

### 5.1 LLM 接入

复用环境已有的 provider（来自 DSH `cordis.patch.yml`）：

| Provider | 协议 | Base URL | 推荐模型 |
|---|---|---|---|
| `agnes` | OpenAI Chat Completions | `https://apihub.agnes-ai.com/v1` | `agnes-2.5-pro`（推理）/ `agnes-3.0-flash`（快） |
| `rsxermu666` | Anthropic Messages | `https://rsxermu666.cn` | `claude-opus-5`（复杂案例） |

**选型建议**：
- 主用 **`agnes-2.5-pro`**（OpenAI 兼容，Function Calling 支持好，国内可达）
- 兜底 **`agnes-3.0-flash`**（低延迟场景）
- 复杂疑难案例可切 `claude-opus-5`

实现方式：`httpx.AsyncClient` + `/chat/completions`，**不引入 LangChain 等重框架**
（依赖少、可控、易调试，与现有技术栈一致）。

### 5.2 流式输出

SSE（`text/event-stream`），事件类型对齐 Dify 的分层日志：

```
event: step_start     data: {"step":1, "phase":"observe"}
event: reasoning      data: {"text":"CPU 正常但连接数异常..."}
event: tool_call      data: {"name":"db_processlist","args":{}}
event: observation    data: {"name":"db_processlist","summary":"发现 1 个长时间运行事务..."}
event: step_end       data: {"step":1, "tokens":1234, "latency_ms":2100}
event: final          data: {"root_cause":{...}, "confidence":0.88, "evidence":[...]}
```

前端按事件渲染「思考卡片流」，实现"看得见的推理过程"。

### 5.3 数据模型（新增表）

```sql
-- Agent 运行实例
CREATE TABLE rca_agent_run (
  run_id        VARCHAR(64) PRIMARY KEY,
  incident_id   VARCHAR(64),
  alert_message TEXT,
  mode          VARCHAR(16),      -- deterministic | agentic
  status        VARCHAR(16),      -- running | completed | failed | timeout
  steps_used    INT,
  total_tokens  INT,
  total_cost    DECIMAL(10,6),
  latency_ms    INT,
  final_answer  JSON,
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Agent 思考步骤（对齐 Dify MessageAgentThought）
CREATE TABLE rca_agent_thought (
  thought_id    BIGINT PRIMARY KEY AUTO_INCREMENT,
  run_id        VARCHAR(64),
  step_no       INT,
  phase         VARCHAR(16),      -- observe | reason | act
  reasoning     TEXT,             -- LLM 的思考
  tool_name     VARCHAR(64),
  tool_input    JSON,
  observation   TEXT,             -- 工具返回（截断）
  observation_ok BOOLEAN,
  tokens        INT,
  latency_ms    INT,
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_run (run_id, step_no)
);
```

---

## 六、System Prompt 设计（草案）

```text
你是信用卡支付系统的资深 SRE，负责根因分析。

## 领域知识（本体）
当前系统拓扑（本体已建模，共 16 实体 / 20 关系）：
  app:payment-app --runsOn--> env:container --deployedOn--> env:host-wsl
  app:payment-app --accesses--> db:mysql-core --containsTable--> table:t_txn
  app:payment-app --hasDataSource--> ds:pay(poolLimit=10) --dataSourceOf--> db:mysql-core

已知约束：
  con:p99-threshold   HTTP P99 < 500ms
  con:pool-exhaust    连接池不得耗尽
  con:db-maxconn      连接数 < max_connections(200)

已知根因模式：
  rc:slow-sql   t_txn 慢查询 → 连接耗尽 → P99 上升

## 工作方式
你必须遵循 观察 → 推理 → 行动 循环：

1. 观察：阅读已提供的指标快照、本体结论、工具返回
2. 推理：在 `reasoning` 字段用中文说明你的判断依据与待验证假设
3. 行动：调用工具获取缺失证据；证据充分时给出最终结论

## 硬性规则
- 只做只读诊断，绝不建议直接执行破坏性命令（可在建议中给出，但标注需人工确认）
- 每次行动前必须说明"为什么要查这个"（reasoning 字段）
- 证据不足时不要臆断，明确说明"当前无法确定，建议进一步检查 X"
- 引用证据时给出具体数值和来源工具名
- 结论必须包含：根因、置信度(0-1)、证据链、影响范围、建议排查步骤

## 输出格式
最终结论以 JSON 返回：
{
  "root_cause": {"category":"数据|资源|配置|依赖|代码","entity_id":"...","description":"..."},
  "confidence": 0.0-1.0,
  "evidence": [{"source":"工具名","finding":"...","value":"..."}],
  "affected_entities": ["..."],
  "reasoning_summary": "一句话总结推理链",
  "next_actions": [{"urgency":"P0|P1|P2","action":"...","command":"...","needs_approval":true}]
}
```

---

## 七、前端改造

### 7.1 新增「推理过程」视图

在现有 4 个标签（RCA / 拓扑 / 指标 / 历史）基础上：

```
┌─ 标签栏 ────────────────────────────────────────────────┐
│ 📊 RCA │ 🔗 拓扑 │ 📈 指标 │ 🧠 推理 │ 🕘 历史          │
└─────────────────────────────────────────────────────────┘

┌─ 🧠 推理过程 ───────────────────────────────────────────┐
│                                                         │
│  ● Step 1  [观察] 已采集 Prometheus 快照、本体拓扑       │
│    ├ 指标: P99=0.83s ↑  连接池 8/10 ↑                   │
│    └ 本体: 命中约束 con:p99-threshold                    │
│                                                         │
│  ● Step 2  [推理] 连接池接近上限，需确认是否存在慢查询    │
│    "P99 上升同时连接池 8/10，典型慢查询导致连接不释放     │
│     的特征。需要查看 MySQL 当前运行的事务与锁等待。"      │
│                                                         │
│  ● Step 3  [行动] db_processlist                        │
│    └ 发现 1 个事务持有 t_txn 排他锁，阻塞 12 个 INSERT   │
│                                                         │
│  ● Step 4  [推理] 根因确认                               │
│    └ 行锁等待导致写入阻塞 → 连接不释放 → P99 上升        │
│                                                         │
│  ✅ 结论  rc:slow-sql (置信度 0.89)   用时 8.2s / 3 步   │
└─────────────────────────────────────────────────────────┘
```

### 7.2 对话区增强

- LLM 逐步输出（流式打字机效果）
- 每条消息可展开「查看推理轨迹」
- 显示 token 用量与耗时（成本可见）

### 7.3 现有面板保留

拓扑图 / 指标面板沿用，额外支持：
- Agent 调用过的工具在拓扑图上高亮对应实体
- 指标面板增加"Agent 关注过的指标"标记

---

## 八、实施计划（分阶段）

| 阶段 | 内容 | 交付物 | 预估 |
|---|---|---|---|
| **P0** | LLM 客户端 + Agent 循环骨架 | `services/llm_client.py`、`services/agent.py` | 核心 |
| **P0** | 工具注册表 + 本体层/监控层工具 | `services/tools/*.py`、`tool_registry.py` | 核心 |
| **P1** | 环境层工具（docker/db 取证）★ | `services/tools/env_tools.py`、`db_tools.py` | 关键增量 |
| **P1** | 数据模型 + 思考持久化 | `03_agent_schema.sql`、`agent_store.py` | — |
| **P1** | Router 双路径判定 + 融合 | `services/router.py` | — |
| **P2** | SSE 流式输出 | `routers/agent.py` | — |
| **P2** | 前端推理视图 | `ReasoningTrace.tsx` 等 | — |
| **P2** | 知识层工具（历史/反馈） | `history_tools.py` | — |
| **P3** | 安全加固、限流、脱敏 | `tool_policy.py` | — |
| **P3** | 验证套件扩展 | `tools/agent_verify.py` | — |

**保留不动**：`rca_engine.py`（确定性引擎继续工作，成为 Agent 的工具）、
现有 API、现有前端面板、现有持久化。

---

## 九、需要你确认的决策点

### Q1. LLM 选型
- **A**（推荐）`agnes-2.5-pro` 为主 + `agnes-3.0-flash` 兜底
- **B** 全部用 `agnes-3.0-flash`（更快更省）
- **C** 复杂案例切 `claude-opus-5`（效果最好，成本高）
- **D** 其他你指定的模型

### Q2. 双路径策略
- **A**（推荐）**本体优先 + Agent 增强**：先跑确定性引擎，高置信度直接返回，否则启动 Agent
- **B** **总是走 Agent**：本体结果仅作为工具供 LLM 调用（更"智能"，但每次都有 LLM 成本）
- **C** **用户可切换**：前端加开关，运维自己选模式

### Q3. Agent 是否允许写操作
- **A**（强烈推荐）**纯只读**，所有修复建议以文本输出，人工执行
- **B** 允许白名单内的安全操作（如重启单个容器），但需二次确认
- **C** 允许自动修复（风险最高，需完善的审批与回滚）

### Q4. 迭代与预算上限
- `max_steps`：建议 **8**（Dify 默认 3，运维排查通常需要更多取证）
- 单次超时：建议 **180s**
- 单次 token 上限：建议 **60k**
- 是否接受这个预算？

### Q5. 流式体验
- **A**（推荐）**SSE 流式**，实时展示推理过程（体验最好）
- **B** 一次性返回（实现简单）

### Q6. 实施范围
- **A**（推荐）**分阶段**：先 P0+P1 打通主链路并验证，再迭代 P2/P3
- **B** 一次性做完 P0–P3

### Q7. 是否保留纯规则模式
保留 `rca_engine.py` 作为独立可调度引擎（无 LLM 也能跑），用于：
- 无网络/LLM 不可用时的降级
- 单元测试的确定性基线
- 与 Agent 结果做交叉验证（一致性检查）

是否同意？

---

## 十、风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| LLM 幻觉出不存在的原因 | 误导排查 | 强制引用工具证据；无证据不给高置信度；与本体引擎交叉校验 |
| 工具调用失控（循环/爆炸） | 成本与延迟 | `max_steps` + 最后一轮摘工具 + 超时熔断 |
| 命令注入 | 安全 | 参数白名单、禁 `shell=True`、拒绝特殊字符 |
| 敏感信息泄露到 LLM | 合规 | 日志/配置脱敏后再入 prompt |
| LLM 服务不可用 | 功能降级 | 自动回落到确定性引擎 |
| 成本不可控 | 预算 | token 上限 + 按 incident 记录成本 + 可配开关 |
| 推理过程不可审计 | 运维不信任 | 全量 `rca_agent_thought` 落库，可回放 |

---

## 十一、预期效果对比

| 场景 | 当前 | 改造后 |
|---|---|---|
| `payment P99 > 500ms` | 关键词命中 → 返回 `rc:slow-sql` | 本体快检命中 + `db_processlist` 取证确认锁等待 → **证据链完整** |
| `接口偶发超时，无规律` | ❌ 无关键词 → 通用回复 | Agent 探索：查指标趋势 → 查容器重启 → 查 DB 慢查询 → **形成假设** |
| `新上线功能导致 5xx` | ❌ 不认识 | Agent 关联：查部署时间线 → 查错误日志 → 定位新代码路径 |
| `为什么昨天开始变慢` | ❌ 无时序能力 | `metrics_range` 查询区间 + 变更历史关联 |
| 结论可信度 | 固定权重，无法解释 | 每步有证据与推理，可审计可回放 |

---

## 附：核心代码骨架（示意，非最终实现）

```python
# services/agent.py
class RCAAgent:
    def __init__(self, alert, seed: dict, tools, max_steps=8):
        self.alert = alert
        self.seed = seed                    # 本体确定性结果作为先验
        self.tools = tools
        self.max_steps = max_steps
        self.scratchpad: list[Step] = []

    async def run(self) -> AgentResult:
        # ① 初始观察：指标 + 本体 + 环境指纹（不消耗 LLM 轮次）
        initial = await self.observe_initial()

        for step in range(1, self.max_steps + 1):
            # 最后一轮摘掉工具，强制收尾（借鉴 Dify）
            active_tools = [] if step == self.max_steps else self.tools.schemas()

            # ② 推理
            msg = await self.llm.chat(
                messages=self.build_messages(initial, self.scratchpad),
                tools=active_tools,
            )
            self.emit("reasoning", msg.reasoning)

            # ③ 行动 / 终结
            if not msg.tool_calls:
                return self.finalize(msg.content, step)

            for call in msg.tool_calls:
                result = await self.tools.invoke(call.name, call.args)
                self.scratchpad.append(Step(step, msg.reasoning, call, result))
                self.emit("observation", call.name, result)
                await self.store.save_thought(...)   # 落库可回放

        return self.finalize_best_effort()
```

---

**请就第九节的 Q1–Q7 给出决策，我再据此实施。**
