# RCA Agent — 信用卡系统运维智能体

基于**本体（EvoOntology）+ 监控指标（Prometheus）**驱动的多轮对话根因分析系统。

---

## 架构

```
                     ┌──────────────────────────────┐
   浏览器  ──────────▶│  rca-agent-frontend (nginx)  │  :3001
                     │  React SPA + /api 反向代理     │
                     └───────────────┬──────────────┘
                                     │ /api/*
                     ┌───────────────▼──────────────┐
                     │  rca-agent-backend (FastAPI)  │  :8088
                     │  多轮对话 / RCA 推理 / 事件存储  │
                     └──┬──────────┬──────────┬──────┘
                        │          │          │
          ┌─────────────▼──┐  ┌────▼─────┐  ┌─▼──────────────┐
          │ EvoOntology MCP│  │Prometheus│  │ MySQL          │
          │ (stdio JSON-RPC)│  │  :9090   │  │ rca_incident   │
          │ .evoontology/  │  │          │  │ rca_feedback   │
          └────────────────┘  └────┬─────┘  │ rca_trajectory │
                                   │        └────────────────┘
                    ┌──────────────┼──────────────┐
                    │              │              │
             ┌──────▼─────┐ ┌──────▼──────┐ ┌────▼────────┐
             │ payment-app│ │mysqld-exporter│ │  grafana    │
             │   :8080    │ │    :9104     │ │   :3000     │
             └──────┬─────┘ └──────┬───────┘ └─────────────┘
                    │              │
                    └──────┬───────┘
                    ┌──────▼──────┐
                    │ MySQL 8.0   │  :3306
                    └─────────────┘
```

---

## 快速开始

```bash
cd rca-agent
docker compose up -d --build
```

| 服务 | 地址 | 凭据 |
|---|---|---|
| **RCA Agent UI** | http://localhost:3001 | — |
| **API 文档** | http://localhost:8088/docs | — |
| Prometheus | http://localhost:9090 | — |
| Grafana | http://localhost:3000 | admin / admin123 |
| payment-app | http://localhost:8080 | — |
| MySQL | localhost:3306 | appuser / apppass |

验证部署：

```bash
python ../tools/prod_verify.py     # 生产栈验证（28 项）
python ../tools/verify_all.py      # 一键全量验证（241 项）
```

---

## 使用方式

### 1. 通过 UI 对话

打开 http://localhost:3001，输入告警描述，例如：

```
payment-app P99 延迟超过 500ms，MySQL 连接池耗尽
```

Agent 自动：解析关键词 → 采集 Prometheus 指标 → 本体拓扑 BFS 遍历 →
候选根因加权打分 → 追踪根因传播路径 → 写入 MySQL → 返回结论。

右侧面板展示：
- **RCA** — 置信度、根因、候选列表、推理链、传播路径
- **拓扑** — 力导向图（节点/边、路径高亮、可拖拽缩放）
- **指标** — Prometheus 实时数据（连接池/延迟分布/targets）
- **历史** — 历史事件列表（点击回看）

#### 面板布局调整

左右两个面板之间的**分隔条可自由拖拽**调宽：

| 操作 | 效果 |
|---|---|
| 拖拽分隔条 | 自由调整左右宽度（280–1100px） |
| 双击分隔条 | 复位到默认宽度（420px） |
| `←` / `→` | 聚焦分隔条后微调 20px（按住 `Shift` 为 60px） |
| `Home` | 同上复位默认宽度 |
| `Enter` / `空格` | 折叠 / 展开右侧面板 |
| 顶部「⏵ 折叠」按钮 | 折叠右侧面板（仅保留标签栏） |
| 点击折叠态标签 | 自动展开并切到该标签 |

宽度与折叠状态保存在 `localStorage`，刷新后保持。容器尺寸变化时自动重新夹取，
不会出现面板被挤出视口的情况。触摸屏同样支持拖拽。

### 2. 通过 API

```bash
# 单次推理
curl -X POST http://localhost:8088/api/rca/infer \
  -H 'Content-Type: application/json' \
  -d '{"alert":{"alertId":"A1","severity":"P1","message":"payment-app P99 延迟超过 500ms，MySQL 连接池耗尽"}}'

# 多轮对话
curl -X POST http://localhost:8088/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"容器 OOM 重启"}],"session_id":"s1"}'

# 事件查询
curl http://localhost:8088/api/rca/incidents
curl http://localhost:8088/api/rca/incidents/<incident_id>
curl http://localhost:8088/api/rca/incidents/<incident_id>/trajectory

# 运维反馈（供本体自演化）
curl -X POST http://localhost:8088/api/rca/feedback \
  -H 'Content-Type: application/json' \
  -d '{"incident_id":"INC-xxx","verdict":"CONFIRMED","actual_cause":"slow query on t_txn"}'

# 统计
curl http://localhost:8088/api/rca/stats
```

---

## API 参考

### Agent（大模型驱动，双引擎）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/agent/diagnose` | **统一诊断入口**：自动路由（确定性快路径 / LLM Agent） |
| `GET` | `/api/agent/runs` | 历史 Agent 运行列表 |
| `GET` | `/api/agent/runs/{run_id}` | 单次运行详情 + 完整推理轨迹（可回放） |
| `GET` | `/api/agent/runs/{run_id}/thoughts` | 仅推理步骤 |
| `GET` | `/api/agent/tools` | 工具目录（按层分组，25 个） |
| `GET` | `/api/agent/config` | Agent 配置（Key 脱敏） |
| `GET` | `/api/agent/health` | LLM 连通性 + 存储状态 |
| `GET` | `/api/agent/stats` | 运行统计（token 成本、模式分布） |

```bash
# 自动路由诊断（推荐）
curl -X POST http://localhost:8088/api/agent/diagnose \
  -H 'Content-Type: application/json' \
  -d '{"alert":"payment-app P99 延迟超过 500ms，MySQL 连接池耗尽","severity":"P1"}'

# 强制走 LLM Agent（跳过路由）
curl -X POST http://localhost:8088/api/agent/diagnose \
  -H 'Content-Type: application/json' \
  -d '{"alert":"用户反馈晚上8点支付偶尔很慢","mode":"agentic"}'

# 回放推理过程
curl http://localhost:8088/api/agent/runs/<run_id>
```

### RCA / 对话 / 监控（既有）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/chat` | 多轮对话（自动识别告警并触发 RCA） |
| `GET` | `/api/chat/tools` | 可用工具清单 |
| `GET` | `/api/chat/history/{session_id}` | 会话历史 |
| `POST` | `/api/rca/infer` | 单次确定性 RCA 推理 |
| `GET` | `/api/rca/incidents` | 事件列表 |
| `GET` | `/api/rca/incidents/{id}` | 事件详情 |
| `GET` | `/api/rca/incidents/{id}/trajectory` | 分析轨迹 |
| `POST` | `/api/rca/feedback` | 运维反馈 |
| `GET` | `/api/rca/stats` | 聚合统计 |
| `POST` | `/api/topology/` | 拓扑查询 |
| `GET` | `/api/metrics/summary` | Prometheus 指标聚合 |
| `GET` | `/api/metrics/targets` | 抓取目标状态 |
| `GET` | `/api/health` | 健康检查 |

---

## 智能体机制（观察 → 推理 → 行动）

### 双引擎协同

```
告警 → 本体确定性快检（始终执行，0 token）
         ├─ 命中已建模根因/约束 且 置信度 ≥ 0.75 → 直接返回（含实测证据）
         └─ 否则 → LLM Agent 循环（把确定性结论作为先验注入）
```

设计要点（参考 Dify Agent 机制并改进）：

| 机制 | 实现 |
|---|---|
| 循环 | 观察 → 推理 → 行动，最多 8 轮 |
| 推理记录 | 捕获 `reasoning_content`（模型原生思维链），非文本解析 |
| 工具调用 | Function Calling 结构化 `tool_calls`（支持并行多工具） |
| 收尾 | **最后一步摘掉工具**，强制模型给结论（借鉴 Dify） |
| 容错 | 工具不存在/失败 → 错误文本作为观察，循环不中断 |
| 降级 | 主模型 → 同 provider fallback → 跨 provider → 确定性引擎 |
| 可回放 | 每步思考与观察落库 `rca_agent_thought` |
| 交叉验证 | Agent 结论与本体结论自动比对（exact / category / divergent） |

### 工具矩阵（25 个，全部只读）

| 层 | 数量 | 工具 |
|---|---|---|
| 本体 | 5 | `ontology_topology` `ontology_resolve` `ontology_constraints` `ontology_diagnose` `ontology_search` |
| 监控 | 5 | `metrics_query` `metrics_range` `metrics_alerts` `metrics_targets` `metrics_summary` |
| 环境 | 12 | Docker：`env_containers` `env_container_inspect` `env_container_logs` `env_container_stats` `env_host_info`<br>MySQL：`db_status` `db_processlist` `db_innodb_status` `db_slow_queries` `db_variables` `db_table_info` `db_explain` |
| 知识 | 3 | `history_search` `history_feedback` `ontology_evidence` |

### 安全边界（Q3 决策：纯只读）

- 全部工具 `read_only=True`，**无任何写操作**
- 参数拒绝 shell 元字符；不使用 `shell=True`
- SQL 白名单（仅 SELECT/SHOW/EXPLAIN），拒绝写关键字与多语句
- 输出脱敏（password/token/key/`sk-*`）
- 修复建议以文本输出，标注 `needs_approval`，由人工执行

### 预算与成本（Q4 决策）

| 项 | 默认值 | 环境变量 |
|---|---|---|
| 最大迭代轮数 | 8 | `RCA_AGENT_MAX_STEPS` |
| 单次超时 | 180s | `RCA_AGENT_TIMEOUT_S` |
| token 上限 | 60000 | `RCA_AGENT_MAX_TOKENS` |
| LLM 单次超时 | 120s | `RCA_LLM_TIMEOUT_S` |
| 快路径置信度门槛 | 0.85（命中已建模根因时 0.75） | `RCA_FAST_PATH_CONFIDENCE` |

### LLM 配置

在 `rca-agent/.env` 或环境变量中配置：

```bash
RCA_LLM_PROVIDER=agnes              # agnes | rsxermu666 | deepseek
RCA_LLM_MODEL=agnes-2.5-pro
RCA_LLM_FALLBACK_MODEL=agnes-3.0-flash
AGNES_API_KEY=sk-...
RSXERMU666_API_KEY=sk-...           # 跨 provider 容灾（额度耗尽时自动切换）
```

Key 解析优先级：环境变量 → `rca-agent/.env` → `~/.dsh/.credentials.yaml`。

---

## API 参考（历史版）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/chat` | 多轮对话（自动识别告警并触发 RCA） |
| `GET` | `/api/chat/tools` | 可用工具清单 |
| `GET` | `/api/chat/history/{session_id}` | 会话历史 |
| `POST` | `/api/rca/infer` | 单次 RCA 推理 |
| `GET` | `/api/rca/incidents` | 事件列表 |
| `GET` | `/api/rca/incidents/{id}` | 事件详情 |
| `GET` | `/api/rca/incidents/{id}/trajectory` | 分析轨迹 |
| `POST` | `/api/rca/feedback` | 运维反馈 |
| `GET` | `/api/rca/stats` | 聚合统计 |
| `POST` | `/api/topology/` | 拓扑查询 |
| `GET` | `/api/metrics/summary` | Prometheus 指标聚合 |
| `GET` | `/api/metrics/targets` | 抓取目标状态 |
| `GET` | `/api/health` | 健康检查 |

---

## 推理算法

### 1. 告警解析

关键词分组匹配，得到 `keywords` 与 `matched_categories`：

| 类别 | 关键词示例 |
|---|---|
| 延迟 | 延迟 / latency / p99 / 超时 / timeout / slow |
| 连接 | 连接 / pool / conn / exhaust / max_connections |
| 数据库 | mysql / 数据库 / db / sql / 慢查询 / 索引 |
| 容器 | 容器 / container / OOM / 重启 / restart / crash |
| 部署 | 部署 / deploy / 发布 / 变更 |
| 可用性 | 不可用 / down / 5xx / 502 / 503 / 504 |
| 网络 | 网络 / network / 连接失败 |
| 存储 | 磁盘 / storage / disk / 空间不足 |

### 2. 拓扑获取（BFS 图遍历）

从 `app:payment-app` 出发，对每个节点调用 `resolve_semantics` 展开关系边，
对端入队继续展开（最多 4 层 / 40 节点），得到完整 `{nodes, edges}`。

当前本体：**16 节点 / 20 边**。

### 3. 候选根因加权打分

```
score = 0.35 × 类别基线
      + 0.20 × min(命中数, 3) / 3
      + 0.30 × 告警类别对齐(0|1)
      + 0.15 × 严重级别权重
      − relation 节点降权 (0.25)
```

| 类别 | 基线 | 说明 |
|---|---|---|
| 数据 | 0.65 | 慢查询 / 索引 / 连接池 |
| 资源 | 0.55 | CPU / 内存 / OOM / 容器重启 |
| 配置 | 0.50 | 超时 / 池上限 / 重试 |
| 依赖 | 0.40 | 下游不可用 |
| 代码 | 0.30 | Bug / 异常 / 5xx |

理论区间 `[0.10, 0.88]`，**不饱和**，保证候选可排序。

### 4. 根因传播路径

以无向边做 BFS，从根因实体追踪到 `app:payment-app`，输出
`[{from, to, relation, relation_type}]` 序列。

### 5. 阈值校验

基于 Prometheus histogram **累加桶**语义正确计算：

```
超过阈值比例 = (count − bucket{le=阈值}) / count
```

---

## 监控告警规则

`monitoring/prometheus/alerts.yml` 定义 7 条规则，与**本体约束对齐**：

| 规则 | 表达式 | 本体约束 |
|---|---|---|
| `AppHighLatencyP99` | `histogram_quantile(0.99, ...) > 0.5` | `con:p99-threshold` |
| `AppHighErrorRate` | 5xx 比例 > 5% | — |
| `AppDown` | `up{job="payment-app"} == 0` | — |
| `MySQLConnectionPoolExhausted` | 池使用率 > 80% | `con:pool-exhaust` |
| `MySQLTooManyConnections` | 连接占用 > 90% | `con:db-maxconn` |
| `MySQLSlowQueries` | 慢查询速率 > 0.1/s | — |
| `MySQLDown` | `mysql_up == 0` | — |

每条规则带 `rca_hint` 注解 —— 即为可直接投喂给 RCA Agent 的告警文本。

---

## 持久化

| 表 | 用途 |
|---|---|
| `rca_incident` | 事件全文（含完整 JSON payload） |
| `rca_trajectory` | 分析轨迹（供本体自演化） |
| `rca_feedback` | 运维反馈（CONFIRMED / REJECTED / PARTIAL） |

MySQL 不可用时自动降级为内存存储，接口不变。

---

## 端到端验证

| 套件 | 脚本 | 覆盖 | 结果 |
|---|---|---|---|
| 生产栈 | `tools/prod_verify.py` | 7 服务、nginx 代理、容器化推理 | 28/28 |
| 持久化 | `tools/persistence_verify.py` | MySQL 写入/读回/轨迹/反馈/统计 | 13/13 |
| 推理不变量 | `tools/scoring_verify.py` | 7 类告警 × 14 项不变量 | 98/98 |
| 前端流程 | `tools/fe_flow_verify.py` | 多轮对话、事件回看、字段契约 | 38/38 |
| 故障演练 | `tools/fault_drill.py` | 注入→告警→RCA→恢复 | 14/14 |
| 告警触发 | `tools/alert_firing_verify.py` | 规则真实 firing | 10/10 |
| 可调宽面板 | `tools/resizable_verify.py` | 拖拽/键控/持久化/产物 | 40/40 |

**合计 241 项验证全部通过。**

---

## 目录结构

```
rca-agent/
├── docker-compose.yml              # 7 服务编排
├── backend/
│   ├── Dockerfile                  # Alpine + Python3
│   ├── requirements.txt
│   ├── main.py                     # FastAPI 入口
│   ├── models.py                   # Pydantic 模型
│   ├── routers/
│   │   ├── chat.py                 # 多轮对话
│   │   ├── rca.py                  # RCA 推理 + 事件 + 反馈
│   │   ├── topology.py             # 拓扑查询
│   │   └── metrics.py              # Prometheus 查询
│   └── services/
│       ├── rca_engine.py           # 推理引擎
│       ├── evo_ontology.py         # EvoOntology MCP 客户端 + BFS
│       ├── prometheus.py           # Prometheus 客户端
│       └── incident_store.py       # MySQL 持久化 + 内存回退
├── frontend/
│   ├── Dockerfile                  # node 构建 + alpine nginx
│   ├── nginx.conf
│   └── src/
│       ├── pages/AgentPage.tsx
│       ├── hooks/useResizablePanel.ts  # 面板拖拽调宽
│       ├── components/
│       │   ├── ChatWindow.tsx
│       │   ├── MessageBubble.tsx
│       │   ├── RCAResultPanel.tsx
│       │   ├── TopologyGraph.tsx   # D3 力导向图
│       │   ├── MetricsPanel.tsx
│       │   └── IncidentList.tsx
│       └── stores/chatStore.ts
└── monitoring/
    ├── prometheus/{prometheus.yml,alerts.yml}
    ├── grafana/{provisioning,dashboards}
    └── mysql/{Dockerfile.exporter,exporter.cnf,init/}
```

---

## 故障排查

```bash
# 查看服务状态
docker compose ps

# 查看后端日志
docker compose logs rca-agent-backend --tail 50

# 检查 Prometheus 抓取目标
curl -s http://localhost:9090/api/v1/targets | python -m json.tool

# 检查告警规则状态
curl -s http://localhost:9090/api/v1/rules | python -m json.tool

# 检查事件表
docker exec cc-mysql-core mysql -u appuser -papppass creditcard \
  -e "SELECT incident_id, severity, root_category, confidence, created_at FROM rca_incident ORDER BY created_at DESC LIMIT 10;"

# 重启后端
docker compose restart rca-agent-backend
```

### 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `stats.backend = memory` | MySQL 不可达 | 检查 `DB_*` 环境变量与 mysql 健康状态 |
| 拓扑节点少于 16 | 本体工作区未挂载 | 确认 `../.evoontology` 已挂载到 `/app/.evoontology` |
| 告警一直 inactive | 阈值未达或 `for` 未满 | 查看 `/api/v1/rules` 的 `value` 与 `for` |
| nginx 502 | 后端未就绪 | `docker compose logs rca-agent-backend` |
