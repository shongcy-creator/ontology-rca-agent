# 信用卡系统运维智能体（RCA Agent）

> **项目定位**：基于本体方法论的智能运维根因分析（Root Cause Analysis）系统
>
> **适用场景**：信用卡支付系统、金融交易平台的故障定位与根因推理
>
> **核心能力**：将告警/故障描述转化为结构化 RCA 结论，输出根因实体、置信度、拓扑路径与可解释证据链

---

## 一、系统架构总览

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              前端层（Frontend）                              │
│   React 18 + TypeScript + Vite + Zustand + D3.js                          │
│   ┌──────────┐  ┌──────────────┐  ┌────────────┐  ┌─────────────────┐    │
│   │ ChatWindow│  │RCAResultPanel│  │TopologyGraph│  │MetricsPanel    │    │
│   └──────────┘  └──────────────┘  └────────────┘  └─────────────────┘    │
│                                                                             │
│   AgentPage.tsx ←─ chatStore.ts（Zustand 状态管理）                         │
│         ↓                                                                   │
│   /api/chat  /api/rca  /api/topology  /api/metrics                          │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼ HTTP/REST
┌─────────────────────────────────────────────────────────────────────────────┐
│                           后端服务层（Backend）                               │
│   FastAPI + Uvicorn + Python 3.10+                                          │
│                                                                             │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │                         路由层（Routers）                            │   │
│   │   chat.py          rca.py           topology.py      metrics.py     │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                      │                                      │
│                                      ▼                                      │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │                        业务服务层（Services）                        │   │
│   │   ┌────────────────┐  ┌──────────────────┐  ┌─────────────────┐    │   │
│   │   │  RCAEngine     │  │  EvoOntologyClient│  │ PrometheusClient │    │   │
│   │   │  推理引擎       │  │  本体客户端       │  │ 监控查询        │    │   │
│   │   └────────────────┘  └──────────────────┘  └─────────────────┘    │   │
│   │                               │                                      │   │
│   │   ┌────────────────────────────────────────────────────────────┐   │   │
│   │   │                   incident_store.py                        │   │   │
│   │   │         MySQL 持久化 + 内存回退（双模式存储）                 │   │   │
│   │   └────────────────────────────────────────────────────────────┘   │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
         │                              │                      │
         ▼                              ▼                      ▼
┌─────────────────┐        ┌─────────────────┐       ┌─────────────────┐
│  Prometheus     │        │  MySQL          │       │  EvoOntology    │
│  监控指标       │        │  事件持久化     │       │  本体语义库     │
│  :9090          │        │  :3306          │       │  (MCP Server)   │
└─────────────────┘        └─────────────────┘       └─────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           模拟演示层（Demo）                                 │
│                                                                             │
│   demo/app/server.js     ← Node.js + prom-client + mysql2                   │
│   demo/mysql/init/*.sql  ← 信用卡业务库初始化                                │
│                                                                             │
│   rca-agent/monitoring/ ← Grafana + Prometheus + MySQL 监控栈                │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 二、技术栈明细

| 层次 | 技术选型 | 版本 | 用途 |
|------|----------|------|------|
| **前端框架** | React | 18.3+ | UI 渲染 |
| **状态管理** | Zustand | 5.0+ | 前端全局状态 |
| **图表库** | D3.js | 7.9+ | 拓扑图可视化 |
| **HTTP 客户端** | TanStack Query | 5.56+ | 数据获取与缓存 |
| **构建工具** | Vite | 5.4+ | 开发服务器 + 生产构建 |
| **类型系统** | TypeScript | 5.5+ | 类型安全 |
| **后端框架** | FastAPI | 0.110+ | REST API |
| **ASGI 服务器** | Uvicorn | 0.29+ | 异步服务 |
| **Python 版本** | Python | 3.10+ | 后端运行时 |
| **监控客户端** | prom-client | ^15 | Node.js 指标暴露 |
| **数据库驱动** | mysql2/promise | ^3 | MySQL 连接池 |
| **图数据库** | EvoOntology | vendor | 本体推理 |
| **监控时序库** | Prometheus | 2.x | 指标存储查询 |

---

## 三、目录结构

```
credit-card-sys-ops/
│
├── docs/                          ← 项目文档
│   └── ARCHITECTURE.md            ← 本文档
│
├── demo/                          ← 模拟演示环境
│   ├── app/                       ← Node.js 应用（支付服务）
│   │   ├── server.js              ← 主服务入口 + Prometheus 指标
│   │   ├── package.json
│   │   └── Dockerfile
│   ├── mysql/
│   │   └── init/
│   │       └── 01_init.sql        ← 信用卡业务库初始化
│   ├── host-machine.json           ← 宿主机信息快照
│   └── topology-snapshot.json      ← 拓扑基线快照
│
├── rca-agent/                     ← RCA 推理智能体（核心）
│   │
│   ├── backend/                   ← FastAPI 后端
│   │   ├── main.py                ← 应用入口、路由注册、CORS 配置
│   │   ├── models.py              ← Pydantic 请求/响应模型
│   │   │
│   │   ├── routers/               ← API 路由
│   │   │   ├── chat.py            ← 对话式 RCA 入口
│   │   │   ├── rca.py            ← 结构化 RCA 接口
│   │   │   ├── topology.py        ← 拓扑查询
│   │   │   └── metrics.py         ← 监控指标
│   │   │
│   │   └── services/               ← 核心业务逻辑
│   │       ├── rca_engine.py      ← RCA 推理引擎（关键词解析、拓扑打分）
│   │       ├── evo_ontology.py    ← EvoOntology MCP 客户端
│   │       ├── prometheus.py      ← Prometheus 查询封装
│   │       └── incident_store.py  ← MySQL + 内存双模存储
│   │
│   ├── frontend/                  ← React 前端
│   │   ├── src/
│   │   │   ├── main.tsx           ← React 入口
│   │   │   ├── index.css          ← 全局样式
│   │   │   ├── pages/
│   │   │   │   └── AgentPage.tsx  ← 主页面（布局 + 快捷输入）
│   │   │   ├── components/
│   │   │   │   ├── ChatWindow.tsx      ← 对话窗口
│   │   │   │   ├── RCAResultPanel.tsx  ← RCA 结果展示
│   │   │   │   ├── TopologyGraph.tsx    ← D3 拓扑图
│   │   │   │   ├── MetricsPanel.tsx     ← Prometheus 指标
│   │   │   │   └── IncidentList.tsx     ← 历史记录列表
│   │   │   └── stores/
│   │   │       └── chatStore.ts   ← Zustand 状态管理
│   │   ├── package.json
│   │   ├── tsconfig.json
│   │   └── dist/                  ← Vite 构建产物
│   │
│   ├── monitoring/                ← 监控栈（可选）
│   │   ├── grafana/
│   │   │   └── dashboards/
│   │   │       └── cc-ops-dashboard.json
│   │   └── mysql/
│   │       └── init/
│   │           ├── 01_init.sql        ← 业务库
│   │           └── 02_rca_schema.sql  ← RCA 表结构
│   │
│   ├── startup.py                 ← 后端启动脚本（单文件）
│   ├── run_backend.py            ← 后端运行脚本
│   ├── write_backend.py          ← 代码生成脚本（生成静态路由代码）
│   └── write_chat.py             ← 对话记录导出脚本
│
├── ontology_turtle/               ← 本体 OWL/TTL 文件（生成产物）
│   ├── ontology.ttl              ← 本体定义（类、属性、约束）
│   ├── instances.ttl             ← 实例数据（应用、容器、数据库）
│   ├── topology.ttl              ← 拓扑关系（三层拓扑）
│   └── sparql_queries.txt        ← SPARQL 查询样例
│
├── vendor/                        ← 第三方依赖（vendored）
│   └── EvoOntology/              ← 本体推理框架
│
├── generate_ontology_review.py    ← 生成 Excel 审核稿（元数据导出）
└── generate_ontology_ttl.py      ← 生成 TTL 本体文件（从 Docker 快照）
```

---

## 四、核心模块设计

### 4.1 RCA 推理引擎（`rca_engine.py`）

```
输入：告警文本 + 严重级别
          │
          ▼
┌──────────────────────────────────────────────────────────────────┐
│  ① 告警解析（parse_alert）                                        │
│     - 关键词模式匹配（延迟/连接/数据库/容器/部署/可用性/网络/存储）  │
│     - 提取 matched_categories                                      │
│     - 计算 severity_boost                                          │
└──────────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────────┐
│  ② 指标采集（Prometheus）                                         │
│     - HTTP P99 延迟 histogram                                      │
│     - MySQL 连接池使用率                                           │
│     - threads_connected / max_connections                         │
│     - 阈值触发判断                                                 │
└──────────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────────┐
│  ③ 拓扑遍历（EvoOntology BFS）                                    │
│     - 从 app:payment-app 出发                                     │
│     - 深度优先展开 4 层                                            │
│     - 节点最多 40 个                                               │
└──────────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────────┐
│  ④ 候选打分（score_candidates）                                    │
│     加权评分公式：                                                  │
│       score = 0.35×类别基线                                        │
│              + 0.20×关键词贴合度                                   │
│              + 0.30×告警类别对齐                                   │
│              + 0.15×严重级别                                       │
│              - 0.25×relation节点降权                               │
│     输出：Top 6 候选根因                                           │
└──────────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────────┐
│  ⑤ 路径追踪（trace_path_to_root）                                 │
│     - BFS 无向图最短路径                                           │
│     - 从根因实体 → app:payment-app                                │
│     - 输出 [{from, to, relation, relation_type}] 序列              │
└──────────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────────┐
│  ⑥ 推理链构建（build_rca_chain）                                  │
│     - Step 1: 告警信号                                            │
│     - Step 2: Prometheus 阈值检查                                 │
│     - Step 3: 拓扑节点列表                                         │
│     - Step 4: 根因传播路径                                         │
│     - Step 5+: 候选根因（置信度排序）                               │
└──────────────────────────────────────────────────────────────────┘
          │
          ▼
输出：结构化 RCA 结果
```

**根因类别与基线置信度**

| 类别 | 基线分数 | 典型证据 |
|------|----------|----------|
| 数据 | 0.65 | mysql_pool_active, mysql_query_duration, 慢查询 |
| 资源 | 0.55 | container_cpu, container_memory, OOM, restart |
| 配置 | 0.50 | timeout, pool_limit, max_connections |
| 依赖 | 0.40 | mysql_up, threads_connected |
| 代码 | 0.30 | 5xx, error_rate |

### 4.2 EvoOntology 客户端（`evo_ontology.py`）

通过 **stdio JSON-RPC** 调用 EvoOntology MCP Server：

```python
subprocess.Popen(
    ["python", "-m", "evoontology.runtime.mcp_server", "--store", workspace],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE
)
# 发送 initialize + tools/call JSON-RPC
```

**核心方法**

| 方法 | 功能 |
|------|------|
| `browse_semantics()` | 搜索本体术语 |
| `resolve_semantics()` | 解析术语关系边 |
| `validate_semantics()` | 验证本体一致性 |
| `get_topology_graph()` | BFS 拓扑图遍历（返回 {nodes, edges}） |

**拓扑 BFS 遍历策略**

```
1. 种子节点 = 匹配 app_name 的术语 id（精确优先，最多 5 个）
2. 队列初始化：[(seed, 0), ...]
3. 循环直到队列空或节点数 >= 40 或深度 > 4
   - 读取 terms.json 获取节点元数据
   - 调用 resolve_semantics 展开关系边
   - 对端节点入队（深度 +1）
   - 反向探索（逆向关系声明）
4. 仅保留两端均在 seen 中的边
```

### 4.3 事件存储（`incident_store.py`）

**双模存储设计**

```
优先 → MySQL（rca_incident / rca_feedback / rca_trajectory）
  │
  ├─ 写入成功 → 返回
  └─ 写入失败 → 降级到内存存储（_mem dict）
                      │
                      ├─ 读取时优先查内存
                      └─ MySQL 恢复后下次写入重试
```

**数据库表结构**

```sql
rca_incident     -- 事件主记录（incident_id PK）
rca_feedback     -- 运维人员反馈（incident_id FK）
rca_trajectory   -- 分析轨迹（incident_id FK，30条/7天触发自演化）
```

### 4.4 Prometheus 客户端（`prometheus.py`）

**指标采集策略**

| 方法 | Prometheus Query | 用途 |
|------|------------------|------|
| `http_latency_buckets()` | `cc_http_request_duration_seconds_bucket{le="X"}` | P50/P95/P99 计算 |
| `mysql_pool_status()` | `cc_mysql_pool_{active,idle,limit}` | 连接池状态 |
| `mysql_global_status()` | `mysql_global_status_threads_connected` | 全局连接数 |
| `txn_summary()` | `cc_txn_total{status="X"}` | 交易统计 |

**降级策略**

```
优先 → self.base_url（Docker 网络地址）
  │
  └─ 失败 → 降级到 localhost:9090（本地开发）
              │
              └─ 再失败 → 抛 RuntimeError
```

---

## 五、数据模型

### 5.1 本体类层次

```
Component（组件）
├── Application（应用）           ← payment-app
│   ├── BusinessFunction（业务功能）  ← 授权/清算
│   └── Interface（接口）          ← authAPI
│
Resource（资源）
├── RuntimeEnvironment（运行环境）
│   ├── Container（容器）          ← K8s Pod
│   └── Cluster（集群）            ← K8s Cluster
├── HostMachine（宿主机）          ← Docker Desktop / 物理机
└── Database（数据库）
    └── DataSource（数据源）      ← JDBC 连接池
        └── Table（表）            ← t_txn / t_customer

Observation（观测对象）
├── Metric（指标）               ← mysql.latency.p99
├── Event（事件）                ← deploy / scale
└── Alert（告警）                ← P0-P4 告警

Problem（问题对象）
├── Incident（故障）             ← 支付超时事件
└── RootCause（根因）            ← t_txn 慢查询
```

### 5.2 拓扑关系

| 关系 | 方向 | 说明 |
|------|------|------|
| `runsOn` | 应用 → 容器 | 应用运行于容器内（1:N 多副本） |
| `deployedOn` | 容器 → 宿主机 | 容器部署于宿主机 |
| `hosts` | 宿主机 → 容器 | deployedOn 的逆向 |
| `accesses` | 应用 → 数据库 | 拓扑核心关系 |
| `hasDataSource` | 应用 → 数据源 | 连接池视角 |
| `dataSourceOf` | 数据源 → 数据库 | 指向具体数据库实例 |
| `containsTable` | 数据库 → 表 | 库内表 |
| `produces` | 组件 → 指标 | RCA 证据关联 |
| `raises` | 组件 → 告警 | 触发告警 |
| `triggers` | 告警 → 故障 | 告警升级为故障 |
| `hasRootCause` | 故障 → 根因 | RCA 输出 |
| `attributedTo` | 根因 → 实体 | 根因归因到具体组件 |

### 5.3 API 请求/响应模型

```typescript
// RCA 推理请求
interface RCARequest {
  alert: { alertId: string; severity: string; message: string }
  session_id: string
}

// RCA 推理响应
interface RCAResponse {
  result: {
    incident_id: string
    alert: { message: string; severity: string; keywords: string[]; matched_categories: string[] }
    evidence: Record<string, any>              // Prometheus 指标
    thresholds_triggered: Threshold[]           // 触发阈值
    topology: TopologyNode[]                   // 拓扑节点
    topology_edges: TopologyEdge[]              // 拓扑边
    root_cause_path: PathHop[]                 // 根因路径
    candidates: RCACandidate[]                 // 候选根因（Top 6）
    root_cause: RCACandidate | null             // 最佳根因
    confidence: number                         // 置信度 [0, 1]
    rca_chain: RCAChainStep[]                  // 推理链
    elapsed_ms: number                          // 执行耗时
  }
  session_id: string
}
```

---

## 六、API 接口清单

### 6.1 对话式 RCA

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat` | 自然语言对话入口，自动识别 RCA 请求 |
| POST | `/api/chat/tool` | 工具调用（rca_infer / query_prometheus / get_topology / browse_ontology） |
| GET | `/api/chat/history/{session_id}` | 获取会话历史 |
| GET | `/api/chat/tools` | 列出可用工具定义 |

### 6.2 结构化 RCA

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/rca/infer` | 结构化 RCA 推理接口 |
| GET | `/api/rca/incidents` | 列出历史事件（摘要） |
| GET | `/api/rca/incidents/{id}` | 获取单个事件完整结果 |
| GET | `/api/rca/incidents/{id}/trajectory` | 获取分析轨迹（自演化输入） |
| POST | `/api/rca/feedback` | 提交运维反馈（CONFIRMED/REJECTED/PARTIAL） |
| GET | `/api/rca/stats` | 聚合统计（根因分布/严重级别） |

### 6.3 拓扑与指标

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/topology/` | 获取应用拓扑节点 |
| GET | `/api/metrics/summary` | Prometheus 指标汇总 |
| GET | `/api/metrics/targets` | 监控目标状态 |

### 6.4 健康检查

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 服务健康检查 |
| GET | `/` | 根路径（SPA 重定向） |

---

## 七、部署架构

### 7.1 开发环境（本地）

```bash
# 1. 启动后端
cd rca-agent
python startup.py
# 监听 :8088

# 2. 启动前端开发服务器
cd rca-agent/frontend
npm run dev
# 监听 :5173

# 3. 启动模拟应用
cd demo/app
docker build -t demo-app .
docker run -p 8080:8080 --link cc-mysql demo-app

# 4. 启动 MySQL
docker run -d --name cc-mysql -e MYSQL_ROOT_PASSWORD=root \
  -e MYSQL_DATABASE=creditcard \
  -v $(pwd)/demo/mysql/init:/docker-entrypoint-initdb.d \
  mysql:8.0

# 5. 启动 Prometheus（可选）
docker run -d -p 9090:9090 \
  -v $(pwd)/prometheus.yml:/etc/prometheus/prometheus.yml \
  prom/prometheus
```

### 7.2 Docker Compose 部署

```yaml
services:
  mysql:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: root
      MYSQL_DATABASE: creditcard
    volumes:
      - ./demo/mysql/init:/docker-entrypoint-initdb.d

  payment-app:
    build: ./demo/app
    ports:
      - "8080:8080"
    environment:
      DB_HOST: mysql
      DB_USER: appuser
      DB_PASSWORD: apppass

  rca-backend:
    build: ./rca-agent
    ports:
      - "8088:8088"
    environment:
      DB_HOST: mysql
      PROMETHHEUS_URL: http://prometheus:9090
      EVO_WORKSPACE: /app/.evoontology

  prometheus:
    image: prom/prometheus
    ports:
      - "9090:9090"

  grafana:
    image: grafana/grafana
    ports:
      - "3001:3000"
```

---

## 八、环境变量配置

### 8.1 后端环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DB_HOST` | `mysql` | MySQL 主机 |
| `DB_PORT` | `3306` | MySQL 端口 |
| `DB_USER` | `appuser` | 数据库用户 |
| `DB_PASSWORD` | `apppass` | 数据库密码 |
| `DB_NAME` | `creditcard` | 数据库名 |
| `PROMETHEUS_URL` | `http://localhost:9090` | Prometheus 地址 |
| `EVO_WORKSPACE` | 项目根/.evoontology | EvoOntology 工作区 |
| `RCA_FRONTEND_ORIGIN` | `http://localhost:5173` | CORS 允许的前端源 |
| `PYTHON_EXE` | 当前解释器 | Python 路径 |

### 8.2 前端环境变量

| 变量 | 说明 |
|------|------|
| `VITE_API_BASE` | API 基础路径（默认 `/api`） |

---

## 九、本体方法论映射

### 9.1 RCA 推理规则

| 规则 | 触发条件 | 推理动作 |
|------|----------|----------|
| R1 资源传播 | 容器 cpuLimit 被占满 | 沿 `deployedOn` 上溯宿主机 |
| R2 连接耗尽 | DataSource maxPool 接近耗尽 | 沿 `accesses` 定位数据库 |
| R3 变更关联 | 故障窗口内存在 deploy/scale 事件 | 时间差比对 |
| R4 传播逆向 | 从告警出发 | 逆向遍历拓扑，证据打分排序 |

### 9.2 自演化机制

```
运维人员反馈（CONFIRMED/REJECTED）
         │
         ▼
rca_feedback 表记录
         │
         ▼
30 条记录 或 7 天周期
         │
         ▼
触发 EvoOntology 本体自演化
         │
         ▼
更新根因类别基线分数
更新关键词 → 类别映射
```

---

## 十、扩展指南

### 10.1 新增根因类别

编辑 `rca_engine.py` 中的常量：

```python
# 新增类别
CATEGORY_BASE_SCORE["网络"] = 0.50

# 新增关键词映射
CATEGORY_EVIDENCE_FIELDS["网络"] = ["network_error", "connection_refused"]

# 更新告警 → 打分类别映射
ALERT_CAT_TO_SCORE_CAT["网络"] = {"依赖"}
```

### 10.2 新增拓扑节点类型

1. 在 `ontology.ttl` 中添加类定义
2. 在 `generate_ontology_ttl.py` 中添加实例生成
3. 在 `rca_engine.py` 的 `KEYWORD_PATTERNS` 中添加相关关键词

### 10.3 新增监控指标

在 `prometheus.py` 中添加查询方法：

```python
def custom_metric(self) -> Dict[str, Any]:
    q = 'your_custom_metric_total'
    return self.query(q)
```

然后在 `all_metrics()` 中注册。

### 10.4 前端组件扩展

```
src/components/
├── ChatWindow.tsx      ← 对话消息展示
├── RCAResultPanel.tsx  ← RCA 结果详情
├── TopologyGraph.tsx   ← D3 拓扑可视化
├── MetricsPanel.tsx   ← 指标面板
├── IncidentList.tsx   ← 历史记录
└── MyNewComponent.tsx ← 新组件（按需添加）
```

---

## 十一、已知限制

| 限制 | 说明 | 建议方案 |
|------|------|----------|
| 单体架构 | 后端未拆分微服务 | 规模增长后按域拆分 |
| 本体依赖 | EvoOntology 为外部 vendor | 解耦后替换为标准 OWL API |
| 内存存储降级 | MySQL 不可用时仅本地有效 | 多实例部署需共用 MySQL |
| 拓扑 BFS 上限 | 最多 40 节点 / 深度 4 | 按需调参 |
| 硬编码应用名 | `payment-app` / `app:payment-app` | 配置化或动态发现 |
| CORS 配置 | 开发环境含 `*` | 生产环境限定具体源 |

---

## 十二、相关文档

| 文档 | 路径 | 说明 |
|------|------|------|
| 本体模型审核表 | `本体模型设计_信用卡系统运维智能体.xlsx` | Excel 格式，概念类/属性/拓扑审核 |
| 本体 TTL 文件 | `ontology_turtle/` | OWL/Turtle 格式本体 |
| SPARQL 查询样例 | `ontology_turtle/sparql_queries.txt` | 本体推理查询（含 Q7–Q12 集群查询） |
| Grafana 面板 | `rca-agent/monitoring/grafana/dashboards/` | 监控面板 JSON |
| **集群化 · 故障注入 · 验证手册** | `docs/集群化_故障注入_验证手册.md` | 第四次扩展：应用/数据库集群化、21 场景故障注入、压测构造资源不足、智能体诊断能力验证、本体迭代 |
| EvoOntology 接入手册 | `docs/EvoOntology_接入手册.md` | 本体构建 / MCP / 自演化 / 可视化接入 |

---

## 十三、集群化扩展（第四次迭代）

应用层与数据库层已由单实例扩展为集群，架构增量为：

```
客户端 → cc-app-gateway(Nginx LB :8080, least_conn + Docker DNS)
           └→ payment-app ×3  (cpus=0.5 / mem=256m / NET_ADMIN)
                写 → cc-mysql-core (PRIMARY, GTID)
                读 → cc-mysql-replica-1/2 (只读副本)
监控：Prometheus DNS-SD 发现全部副本 + 3×mysqld-exporter + blackbox 外部探活
```

新增能力（详见 `docs/集群化_故障注入_验证手册.md`）：

| 能力 | 入口 |
|---|---|
| 集群引导（MySQL 主从，幂等） | `tools/cluster_bootstrap.py` |
| 故障注入（21 场景，可回滚 + 信号验证） | `tools/fault_injector.py` |
| 压力测试（http/mysql/mixed，构造资源不足） | `tools/stress_harness.py` |
| 智能体诊断能力端到端验证 | `tools/cluster_rca_verify.py` |
| 本体迭代（集群知识 → ontology_v1） | `tools/evolve_ontology_cluster.py` |

---

*本文档由 Claude Code 生成，最后更新于 2026-10-02*
