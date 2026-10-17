# 信用卡系统运维智能体（RCA Agent）—— 架构说明

> **项目定位**：本体方法论驱动的运维根因分析（Root Cause Analysis）系统，
> 附带一套可复现的故障注入基准。
>
> **适用场景**：信用卡支付系统、金融交易平台的故障定位与根因推理。
>
> **核心能力**：把告警文本 / 自然语言提问转化为结构化 RCA 结论，输出根因实体、
> 置信度、拓扑路径与可解释证据链。
>
> 本文档描述**当前代码**（集群化形态）。历史的单实例版本已不再对应实际拓扑 ——
> 若你看到"单容器 + 单库"的描述，那是过期文档，以本文与代码为准。

---

## 一、两条主线

这个仓库其实是**两个东西绑在一起**，读代码时先把它们分开：

| 主线 | 位置 | 是什么 |
|---|---|---|
| **故障注入基准** | `tools/`、`demo/` | 21 个故障场景，每个声明"注入期必须成立的信号""应触发的告警""预期根因"，然后逐项去**测** |
| **RCA 智能体** | `rca-agent/` | FastAPI 后端 + React 控制台；确定性本体快路径 + LLM 智能体双引擎 |

基准是给智能体用的**考场**，也是这个仓库更值钱的部分：它测的是通常没人测的东西
—— 故障真的复现了自己的信号吗？声明的告警真的响了吗？诊断对了吗？回滚后判据真的消失了吗？

---

## 二、系统架构总览

```
客户端
  │
  ▼
cc-app-gateway  (nginx:1.27-alpine, least_conn + Docker DNS, :8080)
  ├─→ payment-app 副本 ×3   (Node.js + prom-client + mysql2, cpus=0.5 / mem=256m)
  │        写 → cc-mysql-core          (mysql:8.0, PRIMARY, GTID)
  │        读 → cc-mysql-replica-1/2   (只读副本)
  │
  └─ 容器外观测：cc-container-exporter（重启计数不随容器清零）

监控面
  cc-prometheus (:9090)  ← DNS-SD 发现全部副本
                          ← cc-mysqld-exporter-primary / -replica-1 / -replica-2 (:9104)
                          ← cc-blackbox-exporter (:9115，外部探活)
  4 组 / 24 条告警规则：cc-app-cluster(4) cc-app-alerts(8) cc-db-cluster(4) cc-db-alerts(8)
  ⚠ alerting.alertmanagers: [] —— **没有部署 Alertmanager**，告警不经外部路由
  cc-grafana (:3000)

诊断面
  rca-agent-backend (FastAPI, :8088)
    ├── PrometheusClient ──→ cc-prometheus
    ├── EvoOntologyClient ──→ .evoontology/  (stdio JSON-RPC, MCP server)
    ├── incident_store ──→ MySQL（不可用时降级内存）
    └── 双引擎
          ├─ 确定性引擎（本体快路径，0 token）
          └─ LLM 智能体（工具 + 本体，强制路由）
  rca-agent-frontend (React 控制台, :3001)
    4 个页签：agent(诊断) / chaos(故障注入) / stress(压测) / cost(成本)
```

**所有宿主端口默认只绑回环**：compose 里一律写成 `${CC_BIND:-127.0.0.1}:...`。
要对外暴露必须显式 `CC_BIND=0.0.0.0`，并同时替换全部明文口令（见 `SECURITY.md`）。

---

## 三、技术栈

| 层次 | 选型 | 版本 | 用途 |
|---|---|---|---|
| 前端 | React + TypeScript | 18 / 5.5 | 控制台 |
| 前端状态 | Zustand | 5.0+ | 全局状态、运行中作业的跨页保持 |
| 拓扑可视化 | D3.js | 7.9+ | 拓扑图 |
| 数据获取 | TanStack Query | 5.56+ | 请求缓存 |
| 构建 | Vite | 5.4+ | 开发服务器与生产构建（**生产构建关闭 sourcemap**） |
| 后端 | FastAPI + Uvicorn | 0.110+ / 0.29+ | REST + SSE |
| 运行时 | Python | 3.11+ | 后端与全部验证脚本 |
| 应用侧 | Node.js + prom-client + mysql2 | 20+ / ^15 / ^3 | 被观测的支付服务 |
| 网关 | Nginx | 1.27-alpine | 负载均衡（`least_conn`） |
| 数据库 | MySQL | 8.0 | 业务库 + 事件持久化（GTID 主从） |
| 监控 | Prometheus / Grafana / blackbox-exporter / mysqld-exporter | 2.55.1 / 11.5.2 / 0.25.0 / 0.16.0 | 指标、面板、外部探活、DB 指标 |
| 本体 | EvoOntology（vendored） | `vendor/` | 本体演化引擎 + MCP server |

---

## 四、目录结构

```
credit-card-sys-ops/
│
├── rca-agent/                       ← 智能体（compose 栈 + 后端 + 前端 + 监控）
│   ├── docker-compose.yml           ← **主栈**（14 个服务，见第二节）
│   ├── run_local.py                 ← 本地起后端（不经容器）
│   ├── gateway/nginx.conf           ← 网关配置
│   │
│   ├── backend/
│   │   ├── main.py                  ← 应用入口、路由注册、CORS
│   │   ├── models.py                ← Pydantic 请求/响应模型
│   │   ├── routers/                 ← chat / rca / agent / topology / metrics / chaos
│   │   └── services/                ← 核心逻辑（见第五节）
│   │
│   ├── frontend/src/
│   │   ├── pages/                   ← AgentPage / FaultInjectionPage / StressPage / CostPage
│   │   ├── components/              ← ChatWindow / RCAResultPanel / TopologyGraph /
│   │   │                              MetricsPanel / IncidentList / LLMSettings /
│   │   │                              ReasoningTrace / ReasoningInline / MessageBubble
│   │   ├── stores/                  ← agentStore / chatStore
│   │   └── state/runningJob.ts      ← 切页时作业状态的持久化与再附着
│   │
│   └── monitoring/
│       ├── prometheus/{prometheus.yml, alerts.yml}   ← 24 条规则 / 4 组
│       ├── blackbox/blackbox.yml
│       └── grafana/                 ← 面板与数据源 provisioning
│
├── tools/                           ← 基准与验证（80 个脚本）
│   ├── fault_injector.py            ← 故障注入 CLI：doctor/list/inject/recover/run/verify/seed
│   ├── chaos/{core.py,catalog.py}   ← 引擎与 21 场景目录（+ FAST_SUBSET）
│   ├── stress_harness.py            ← 压测台（http / mysql / mixed）
│   ├── cluster_bootstrap.py         ← 建 schema + 配主从复制（**不在 compose 里**）
│   ├── dev_up.py                    ← 一条命令把环境带到可用状态
│   ├── alert_coverage_check.py      ← 注入 → 等够久 → 声明的告警是否真的 firing
│   ├── cluster_rca_verify.py        ← 端到端：注入 → 压测 → 告警 → 诊断 → 评分
│   ├── evolve_ontology_cluster.py   ← 一轮本体演化（成对评估 + 闸门）
│   ├── ontology_hygiene.py          ← 本体卫生契约
│   └── publish_reports.py           ← 报告卫生闸门（宿主路径 → exit 1）
│
├── .evoontology/                    ← 版本化本体（知识库）
│   ├── active.json                  ← 当前 {"active_version": "ontology_v9"}
│   ├── versions/                    ← ontology_v0 … v9（每轮带后缀，如 v9-shapes）
│   └── trajectories/ tasks/ workload/ evolution/ reports/ visualizations/
│
├── ontology_turtle/                 ← OWL/TTL 单一事实源（手工维护）+ shapes.ttl（SHACL 结构形状）
├── vendor/EvoOntology/              ← MIT 许可的本体演化引擎（上游第三方）
├── demo/                            ← **单实例简化栈**（讲解用，见第七节）
├── docs/                            ← 手册、迁移说明、lessons（中文为主）
│   └── lessons/                     ← 6 篇真实事故复盘（01–06）
├── reports/                         ← 已发布的可复现报告（24 个 JSON）
├── config/                          ← evoontology.cordis.yml
└── dsh-plugin-chaos-console/        ← 控制台插件
```

> **已删除的旧路径**：`rca-agent/startup.py`、`run_backend.py`、`write_backend.py`、
> `write_chat.py` 都不存在了。起后端的入口是 `dev_up.py`（容器）或 `run_local.py`（本机）。

---

## 五、核心模块设计（`rca-agent/backend/services/`）

### 5.1 双引擎路由 —— 由**输入**决定，不由结论决定

入口：`POST /api/agent/diagnose`（及 `/diagnose/stream`，SSE）
→ `orchestrator.diagnose()` → `router.decide()`

**关键设计**：走哪条路是由**输入**（告警文本 + 本体约束 + 类别共识）判定的，
不是"先算完再看置信度够不够"。所以同一个问题不会因为本次推理结果不同而换引擎。

`router.py:79` 的顺序规则（先命中先返回）：

| 条件 | 路由 |
|---|---|
| 无种子节点 | `agentic` |
| 命中"无故障"正则（明确的"一切正常"表述） | `deterministic` |
| 类别未知/为空 | `agentic` |
| **约束匹配上了，但引擎给出的根因既不在约束目标 2 跳内、类别也不对齐** | **强制 `agentic`** |
| 已知根因（`rc:` 术语）+ 种子支持 + 置信度 ≥ 0.75 | `deterministic` |
| 置信度 ≥ 0.85 且约束匹配 | `deterministic` |
| 置信度 ≥ 0.85 且类别共识唯一 | `deterministic` |
| 其余 | `agentic` |

响应里带 `route.mode` 与 `route.reason`（HTTP 层），持久化列名为 `route_reason`
（`agent_store.save_run`）。**所以"它为什么走了那条路"是可查的，不是黑盒。**

阈值可配：`RCA_FAST_PATH_CONFIDENCE`（默认 `0.85`）。

### 5.2 确定性推理引擎（`rca_engine.py`）

```
输入：告警文本 + 严重级别
  ① 解析告警（关键词 → matched_categories / severity_boost）
  ② 采集证据（Prometheus：HTTP P50/P95/P99、连接池、threads_connected…）
  ③ 拓扑遍历（EvoOntology BFS：种子 = 匹配应用名的术语，深度 ≤4、节点 ≤40）
  ④ 候选打分（见下）
  ⑤ 指标面判别（仅 OOM 歧义时介入，见 5.3）
  ⑥ 路径追踪（无向图 BFS：根因实体 → app:payment-app，回溯边序列）
  ⑦ 构建推理链
```

**打分公式（本体路径）** `rca_engine.py:305`：

```
score = 0.25×类别基线 + 0.50×关键词特异度 + 0.15×告警类别对齐 + 0.10×严重级别加成
        − 0.25（若该节点是 relation 边）
score *= ROLE_WEIGHT[角色]
```

- 类别基线 `CATEGORY_BASE_SCORE`：数据 .65 / 资源 .55 / 配置 .50 / 依赖 .40 / 代码 .30
- 角色权重 `ROLE_WEIGHT`：root_cause 1.15 / component 1.00 / observation 0.85 / rule 0.80
- 特异度 `specificity = min(词命中权重, 3) / 3`，
  其中 `词命中权重 = 主命中数 + 0.5×最长 n-gram(≤4) − 负命中数`
- **组件→根因提升**：命中组件级实体时，经 `attributedTo` / `evidencedBy` 关系提升到 `rc:` 根因

> 旧文档里的 `0.35/0.20/0.30/0.15` 公式**仍在代码里，但只作为本体读取失败时的降级路径**
> （`rca_engine.py:325`）。它是兜底，不是现行算法。

### 5.3 指标面判别器（`metric_discriminator.py`）

这是**文字面判不了、只能靠指标判**的那一类歧义，被单独隔离成一个模块：

- **触发条件极窄**：仅当候选中**同时**出现 `rc:oom-kill` 与 `rc:mem-pressure`，
  且 top1 是其中之一时才介入。
- **规则 1**：文本里有硬证据（`HARD_KILL_EVIDENCE`）→ 直接判 oom-kill，不查指标。
- **规则 2**：查 `increase(cc_container_oom_kill_total[5m])` → `>0` 为 oom-kill，`==0` 为 mem-pressure。
- **规则 3**：**指标不可用 → 不干预**（保持引擎原判断，而不是猜）。
- 干预时会重排候选并追加一条可审计的理由。

**为什么这条链是这个形状**：证据不能放在被观测对象自己身上。
容器自己暴露的 `oom_kill` 计数器会随容器重启清零 —— 所以计数器必须由
**容器外**的 `container-exporter` 采集（见 `docs/lessons/06`）。

### 5.4 LLM 智能体（`agent.py`）

循环形状：`observe → reason → act`。预算：`max_steps=8` / `180s` / `60k tokens`。

- 初始观测预跑 `metrics_summary / alerts / targets`
- **确定性引擎的结果作为先验注入**，并附一句"不要盲信"的指令
- 每步调 LLM；无 `tool_calls` 即视为给出最终 JSON
- 最后一步剥离工具（逼它收敛）

**工具集（全部只读）** —— `services/tools/`：

| 层 | 工具 |
|---|---|
| 本体 | `ontology_topology` `ontology_resolve` `ontology_constraints` `ontology_diagnose` `ontology_search` |
| 指标 | `metrics_query` `metrics_range` `metrics_alerts` `metrics_targets` `metrics_summary` |
| 环境 | `env_containers` `env_container_inspect` `env_container_logs` `env_container_stats` `env_host_info` |
| 数据库 | `db_processlist` `db_innodb_status` `db_status` `db_slow_queries` `db_variables` `db_table_info` `db_explain` |
| 知识 | `history_search` `history_feedback` `ontology_evidence` |

多供应商抽象在 `llm_client.py`（归一化 `LLMResponse`，支持 fallback 与跨供应商候选）；
计费价表在 `rate_limiter.py:MODEL_PRICE_PER_1M_TOKENS`，按 prompt/completion 分别计价。

### 5.5 本体客户端（`evo_ontology.py`）

通过 **stdio JSON-RPC** 调用 EvoOntology MCP Server：

```python
subprocess.Popen(["python", "-m", "evoontology.runtime.mcp_server", "--store", workspace],
                 stdin=PIPE, stdout=PIPE)   # initialize + tools/call
```

核心方法：`browse_semantics` / `resolve_semantics` / `validate_semantics` / `get_topology_graph`（带缓存的 BFS）。

注意分工：**`OntologyScoring` 不走 MCP，直接读工作区文件**（`terms/relations/constraints/...json`），
因为它要在每次打分时高频取用；MCP 留给拓扑遍历与语义解析。

### 5.6 事件存储（`incident_store.py`）

双模：优先 MySQL（`rca_incident` / `rca_feedback` / `rca_trajectory`），
写入失败降级到内存 dict（读取时优先查内存）。`agent_store.py` 另存运行轨迹与 `route_reason`。

### 5.7 Prometheus 客户端（`prometheus.py`）

| 方法 | 查询 | 用途 |
|---|---|---|
| `http_latency_buckets()` | `cc_http_request_duration_seconds_bucket` | P50/P95/P99 |
| `mysql_pool_status()` | `cc_mysql_pool_{active,idle,limit}` | 连接池 |
| `mysql_global_status()` | `mysql_global_status_threads_connected` | 全局连接 |
| `txn_summary()` | `cc_txn_total{status=...}` | 交易统计 |

降级：`self.base_url` → 失败则 `localhost:9090` → 再失败抛 `RuntimeError`。

---

## 六、数据模型

### 6.1 本体类层次

```
Component（组件）
├── Application           ← app:payment-app
│   ├── BusinessFunction  ← 授权 / 清算
│   └── Interface         ← authAPI
Resource（资源）
├── RuntimeEnvironment
│   ├── Container         ← env:container
│   └── Cluster           ← cluster:cc-cluster
├── HostMachine
└── Database              ← db:mysql-core
    └── DataSource
        └── Table         ← table:t_txn / table:t_customer
Observation（观测对象）：Metric / Event / Alert
Problem（问题对象）：Incident / RootCause   ← rc:* 术语
```

**当前版本 `ontology_v9`**：56 terms / 87 relations / 30 constraints / 22 evidence / 20 mappings。

运行时记录与 TTL 的对应（ontology_v8 起）：

- 每个术语带 `class`（TTL 的类）与 `class_source`。别名对应到 TTL 个体的（30 个），类直接取自该个体的
  `rdf:type`；其余（演化中派生的 `rc:*` / `metric:*` 等）按前缀规则或逐条指定。
- 每条关系带 `predicate`（TTL 的对象属性，或 `sameAs` = owl:sameAs）。`connection_condition` 退回为说明文字；
  引擎读 `predicate`，不再对文本做前缀匹配。
- 原先写成散文的属性链 `runsOn o deployedOn` 已在 TTL 声明为 `:runsOnHost`（`owl:propertyChainAxiom`），
  门禁会验证两段链在数据里真实存在。

### 6.2 拓扑关系

| 关系 | 方向 | 说明 |
|---|---|---|
| `runsOn` | 应用 → 容器 | 应用运行于容器（1:N 多副本） |
| `deployedOn` / `hosts` | 容器 ↔ 宿主机 | 正/逆向 |
| `accesses` | 应用 → 数据库 | 拓扑核心关系 |
| `hasDataSource` / `dataSourceOf` | 应用 → 数据源 → 数据库 | 连接池视角 |
| `containsTable` | 数据库 → 表 | 库内表 |
| `produces` | 组件 → 指标 | RCA 证据关联 |
| `raises` | 组件 → 告警 | 触发告警 |
| `triggers` | 告警 → 故障 | 告警升级为故障 |
| `hasRootCause` | 故障 → 根因 | RCA 输出 |
| `attributedTo` | 根因 → 实体 | 根因归因（组件→根因提升用） |

---

## 七、API 接口清单（实际注册路由）

路由注册见 `main.py:30-36`。

| 前缀 | 方法 | 说明 |
|---|---|---|
| `/api` | POST `/chat`、`/chat/tool`，GET `/chat/history/{id}`、`/chat/tools` | 对话式 RCA |
| `/api/rca` | POST `/infer`、`/feedback`；GET `/incidents`、`/incidents/{id}`、`/incidents/{id}/trajectory`、`/stats` | 结构化 RCA |
| `/api/agent` | POST `/diagnose`、`/diagnose/stream`；GET `/cost` | 双引擎诊断（含 SSE 与成本） |
| `/api/topology` | POST `/` | 拓扑查询 |
| `/api/metrics` | GET `/summary`、`/targets` | 指标与目标状态 |
| `/api/chaos` | 故障注入 / 压测 / 回滚 | 控制台后端（高风险场景要 `confirm=true`） |
| — | GET `/api/health`、`/` | 健康检查 / SPA 重定向 |

---

## 八、部署与运行

### 8.1 一条命令（推荐）

```bash
python tools/dev_up.py        # 等价于 make up
```

内部四步，缺一步环境就是坏的：

| 步骤 | 命令 | 漏掉会怎样 |
|---|---|---|
| 1 起栈 | `docker compose -f rca-agent/docker-compose.yml up -d` | — |
| 2 引导 | `python tools/cluster_bootstrap.py` | **副本是空实例**：无 appuser / 无表 / 复制为空。症状是读路径 Access denied + 两条 replication FAIL，而**每个容器都报 healthy** |
| 3 数据集 | `python tools/fault_injector.py seed` | 场景所需的测试体量不存在 |
| 4 体检 | `python tools/fault_injector.py doctor`（带重试） | 19 项检查，含体量检查与「抓取目标全部在线」 |

> 步骤 2 曾经在三处文档 / CI / compose 之间不一致，被漏过三次。
> 现在只有一个入口。注意 `doctor` 校验数据集体量，**所以 seed 必须在门之前** ——
> 顺序反了这个门永远不可能通过（`CONTRIBUTING.md` 第 10 条）。

结束：控制台 <http://localhost:3001>，4 个页签 agent / chaos / stress / cost。

### 8.2 简化栈（讲解用）

`demo/` 是**单实例**栈（1 app + 1 MySQL + Prometheus + Grafana + mysqld-exporter），
用于快速理解指标面，**不是**主栈、也**不是**生产参考。见 `demo/README.md`。

### 8.3 环境变量（`.env.example`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `CC_BIND` | `127.0.0.1` | 宿主端口绑定地址；改 `0.0.0.0` 前必须先换掉全部口令 |
| `CHAOS_API_TOKEN` | 空 | 注入令牌（可选） |
| `CHAOS_UI_ENABLED` | `1` | 设为 `0` 直接关掉注入控制台 |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | `mysql` / `3306` / `appuser` / — / `creditcard` | 业务库 |
| `RCA_DB_USER` / `RCA_DB_PASSWORD` | `rca_readonly` | 后端用的**只读**账号 |
| `RCA_FAST_PATH_CONFIDENCE` | `0.85` | 快路径置信度阈值 |
| `RCA_AGENT_MAX_TOKENS` | `60000` | 智能体 token 预算 |
| `CHAOS_PROM_URL` / `CHAOS_APP_LB` / `CHAOS_RCA_API` / `CHAOS_CONSOLE_URL` | 本机回环各端口 | 验证脚本的连接目标 |
| `GRAFANA_ADMIN_PASSWORD` | 演示默认值 | 对外暴露前必须替换 |

### 8.4 安全口径

- 所有宿主端口默认绑 `127.0.0.1`；`CHAOS_UI_ENABLED=0` 可彻底关闭注入面
- 高风险场景要求 `confirm=true`；`/jobs/current/abort` 始终可用
- 处置动作：只读诊断自动执行；**写操作需要 `--approve`**，带逆操作与后置校验，校验失败自动回滚
- **不变量：最紧急（P0）的处置动作永远只读** —— 作为本体**发布门禁**强制，不是注释

---

## 九、本体方法论映射与演化

### 9.1 推理规则

| 规则 | 触发 | 动作 |
|---|---|---|
| R1 资源传播 | 容器 CPU 配额被占满 | 沿 `deployedOn` 上溯 |
| R2 连接耗尽 | DataSource 连接池接近耗尽 | 沿 `accesses` 定位数据库 |
| R3 变更关联 | 故障窗口内有 deploy / scale | 时间差比对 |
| R4 传播逆向 | 从告警出发 | 逆向遍历 + 证据打分 |

### 9.2 版本化演化（EvoOntology 协议）

```
候选版本 → 成对评估（对父版本）→ 门禁 → 采纳
   ↑                                    │
   └── tools/evolve_ontology_cluster.py --round <name>
```

- **绝不手工编辑** `.evoontology/versions/*`，也绝不"顺手改个关键词"
- 门禁是真的会拒绝的：`reports/round7_candidate_rejected.json` 就是一次被拒的候选
- 当前激活 `ontology_v9`；历史版本 `v0 → v9` 各带一轮主题后缀
  （`-cluster` / `-scoring` / `-hygiene` / `-remediation` / `-command-targets` / `-memory` /
  `-freshness` / `-typing` / `-shapes`）
- 第 7 轮起，每轮的假设、版本说明与局限由 patch 模块的 `describe()` 提供，数字取自成对评估结果

### 9.3 发布门禁（`tools/ontology_hygiene.py` → `gate_records`）

每一轮在 `accept` 之前、以及 CI 的 lint job 里都会跑同一组检查，任何一条违反就不发布：

| 检查 | 模块 | 内容 |
|---|---|---|
| 卫生 | `ontology_hygiene.py` | 游离 / 不可达 / lifecycle 声明 / 根因可观测 |
| 证据时效 | `ontology_freshness.py` | 当前状态记录与非历史证据不得点名声明环境（`rca-agent/docker-compose.yml`）之外的容器 / 主机（F1/F2）；历史证据须写明观测环境与原因（F2b）；只靠历史证据支撑的记录 → 告警 |
| 类型契约 | `ontology_typing.py` | 谓词与类取自 TTL 词表（T1/T2）；与 TTL 个体的类型一致（T2b）；属性链端点之间链真实存在（T3） |
| SHACL | `ontology_shacl.py` + `ontology_turtle/shapes.ttl` | 运行时记录投影为 RDF；domain/range 形状**由 TTL 生成**（并集 → `sh:or`），结构形状手写；pyshacl 缺失即判失败 |

为什么用 SHACL 而不是 OWL 推理机做 domain/range：OWL 是开世界的，`rdfs:domain` 是**推理规则**
（满足 domain 的主语会被推断为该类），推理机永远不会因此报错；SHACL 是闭世界的**约束**，才能变红。

证据时效检查刻意**不执行** `evidence.query`：那些是自由文本（docker CLI / SQL / PromQL / PowerShell /
Python 表达式），把本体数据当命令执行就是命令注入面。它只检查可判定的部分 —— 点名的实体是否还存在；
过期的字面量（如写死的 IP）它认不出。
- 运维反馈（`rca_incident` / `rca_feedback` / `rca_trajectory`）是自演化的输入

---

## 十、基准侧：故障注入与压测

- `tools/chaos/catalog.py` 声明 **21 个场景**：application 6 / database 9 / runtime 3 / resource 3
- 每个场景声明：`signals`（注入期必须成立）、`recovered_signals`（恢复期必须**变得不成立**）、
  `expected_root_cause`、`ontology_terms`、`alert_names`、`blast_radius`
- 一次只跑一个故障；`verify_scenarios` 在 `finally` 里强制全量回滚，
  `run` 即使被取消也保证回滚
- 回滚是**数据驱动**的（靠 `recover_hint` 与 `.chaos/fault_state.json`），
  所以进程被杀也能复原
- `verify` 有两种范围：全量 21，或 `--fast` 的**固定 7 场景子集**
  （`catalog.FAST_SUBSET`：四层全覆盖、且都不依赖压测台）

**"通过"的准确含义**（读报告时必须知道）：`passed` = 注入成功 **且** 回滚成功，
**不要求**声明的信号全部成立。报告里另有两个字段把弱结论摆到台面上：
`signals_observed_full`（注入期全部声明信号成立数）与 `signals_observed_none`。
详见 `tools/fault_injector.py` 中 `verify_scenarios` 的注释。

---

## 十一、已知限制

| 限制 | 说明 |
|---|---|
| 单体后端 | `rca-agent-backend` 未拆微服务 |
| 注入层仅 Docker | 目标是硬编码容器名；K8s 需重写执行层 |
| 数据库仅 MySQL | 方言未抽象 |
| 无 Alertmanager | `alertmanagers: []`，告警不经外部路由，没有告警通知链路 |
| 本体依赖 vendor | EvoOntology 为外部 vendored 依赖 |
| 降级存储 | MySQL 不可用时仅内存有效，多实例部署需共用 MySQL |
| 拓扑 BFS 上限 | 40 节点 / 深度 4 |
| 应用名硬编码 | `payment-app` / `app:payment-app` 未配置化 |
| 打分关键词过拟合 | held-out 表述 4/10 = 40%，远低于调优集的 81–86%（**刻意公开的负面结果**） |
| `--fast` 有意的覆盖洞 | 不含网络 / 慢查询 / 锁类场景，不能当作"系统没问题"的证据 |
| 16 条记录只靠历史证据 | 单实例时代的观测，需集群在线时重新观测（证据时效检查列为告警） |
| TTL 生成器过时 | `generate_ontology_ttl.py` 早于集群化，`ontology.ttl` 的集群部分是手工维护的；**重跑生成器会抹掉它** |

---

## 十二、相关文档

| 文档 | 语言 | 内容 |
|---|---|---|
| [`docs/集群化_故障注入_验证手册.md`](集群化_故障注入_验证手册.md) | 中文 | **主手册**：设计、40+ 已验证结论、自我修正记录 |
| [`docs/lessons/`](lessons/README.md) | EN/ZH | **6 篇真实事故复盘**：让坏系统看起来健康的那些陷阱 |
| [`docs/EvoOntology_接入手册.md`](EvoOntology_接入手册.md) | 中文 | 本体协议接入与轮次 |
| [`docs/Linux迁移适配.md`](Linux迁移适配.md) | 中文 | Windows Docker → Linux Docker |
| [`docs/RCA_Agent_智能化改造方案.md`](RCA_Agent_智能化改造方案.md) | 中文 | 智能体设计与路线图 |
| [`CONTRIBUTING.md`](../CONTRIBUTING.md) | EN | 10 条不可妥协的纪律（每条都因为被违反过一次） |
| [`README.md`](../README.md) | EN/ZH | 概览与实测数字（每个数字都标注测量依据） |

---

*最后更新：2026-10-17。本文档描述集群化形态；如果你看到"单容器 / 单库"的描述，那是过期内容。*
