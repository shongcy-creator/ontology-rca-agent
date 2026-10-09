---
name: rca-root-cause-analysis
description: 对信用卡系统的告警或故障进行根因分析（RCA），基于本体拓扑图遍历、Prometheus 指标与加权置信度评分定位故障源头。用于故障定位、连接池耗尽、慢查询、容器 OOM、变更关联等运维场景。
---

# 根因分析（RCA）— 信用卡系统运维智能体

## 触发条件

用户报告故障、告警、异常，或明确要求根因分析时，激活本技能。

## 系统入口

| 入口 | 地址 | 用途 |
|---|---|---|
| **RCA Agent UI** | http://localhost:3001 | 多轮对话 + 拓扑可视化 + 指标面板 |
| **REST API** | http://localhost:8088 | 程序化调用 |
| **API 文档** | http://localhost:8088/docs | Swagger |

启动：`cd rca-agent && docker compose up -d --build`

---

## 处理流程

### Step 1 — 获取告警文本

告警来源有三种，优先使用**第一种**（真实告警驱动）：

1. **Prometheus 告警规则**（推荐）—— 查询 `/api/v1/rules`，读取 firing 规则的
   `annotations.rca_hint`，该字段即为可直接投喂的告警文本：

   ```bash
   curl -s http://localhost:9090/api/v1/rules \
     | python -c "import sys,json; [print(a['annotations']['rca_hint']) for g in json.load(sys.stdin)['data']['groups'] for r in g['rules'] if r.get('state')=='firing' for a in r.get('alerts',[])]"
   ```

2. 用户直接描述故障现象。

3. 监控面板（Grafana http://localhost:3000，admin/admin123）观察到的异常指标。

### Step 2 — 调用 RCA 推理

**方式 A：REST API（推荐）**

```bash
curl -X POST http://localhost:8088/api/rca/infer \
  -H 'Content-Type: application/json' \
  -d '{"alert":{"alertId":"A1","severity":"P1","message":"payment-app P99 延迟超过 500ms，MySQL 连接池耗尽"}}'
```

**方式 B：多轮对话**（保留上下文）

```bash
curl -X POST http://localhost:8088/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"<告警文本>"}],"session_id":"s1"}'
```

**方式 C：本地推理引擎**（无需 Docker，读本地本体）

```bash
python tools/rca_engine.py --alert '<告警消息>'
python tools/rca_engine.py --interactive
```

### Step 3 — 解读结果

推理输出包含 5 个部分：

| 字段 | 含义 |
|---|---|
| `root_cause` | 根因实体 + 类别 + 置信度 |
| `topology` / `topology_edges` | 本体拓扑图（16 节点 / 20 边） |
| `root_cause_path` | 根因 → 应用入口的传播路径 |
| `candidates` | 候选根因（置信度降序，不饱和） |
| `thresholds_triggered` | Prometheus 阈值校验结果 |
| `rca_chain` | 完整推理链（可解释） |

### Step 4 — 输出 RCA 结论

```
## RCA 结论

**告警**：[alertId] [severity] [message]
**incident_id**：[id]（可通过 /api/rca/incidents/{id} 回看）

**根因**：[类别] `[entity_id]`  置信度 [confidence]

**传播路径**：`[根因]` → `[中间实体]` → `app:payment-app`

**候选根因**：
1. [类别] `[entity_id]` conf=[x]
2. ...

**证据**：Prometheus 实时指标（连接池 / 延迟分布 / targets）

**建议操作**：
- 数据类 → 检查慢查询、补索引、扩容连接池
- 资源类 → 检查容器 CPU/内存、查看重启日志
- 其他 → 对照 Prometheus 趋势与变更记录
```

### Step 5 — 记录反馈（可选，供本体自演化）

```bash
curl -X POST http://localhost:8088/api/rca/feedback \
  -H 'Content-Type: application/json' \
  -d '{"incident_id":"INC-xxx","verdict":"CONFIRMED","actual_cause":"t_txn 慢查询","operator":"oncall"}'
```

`verdict` 取值：`CONFIRMED` / `REJECTED` / `PARTIAL`

---

## 推理算法

### 拓扑获取 — BFS 图遍历

从 `app:payment-app` 出发调用 `resolve_semantics` 展开关系边，对端入队继续
展开（≤4 层 / 40 节点），返回完整 `{nodes, edges}`。

### 候选根因加权打分

```
score = 0.35 × 类别基线
      + 0.20 × min(命中数, 3) / 3
      + 0.30 × 告警类别对齐
      + 0.15 × 严重级别权重
      − 0.25 × (relation 节点)
```

| 类别 | 基线 | 典型实体 |
|---|---|---|
| 数据 | 0.65 | `rc:slow-sql` / `db:mysql-core` / `table:t_txn` |
| 资源 | 0.55 | `env:container` / `env:host-wsl` |
| 配置 | 0.50 | `ds:pay`（连接池配置） |
| 依赖 | 0.40 | `rel:app-accesses-db` |
| 代码 | 0.30 | — |

**告警类别 → 打分类别映射**：

| 告警命中类别 | 优先打分类别 |
|---|---|
| 延迟 / 连接 | 数据、配置 |
| 数据库 | 数据、依赖 |
| 容器 / 存储 | 资源 |
| 部署 | 配置、代码 |
| 可用性 / 网络 | 依赖、代码 |

### 阈值校验（histogram 累加桶语义）

```
超过阈值比例 = (count − bucket{le=阈值}) / count
```

---

## 关键本体实体

| ID | 说明 |
|---|---|
| `app:payment-app` | 支付交易应用 v1.8.2 |
| `env:container` | 容器 cc-credit-card-app |
| `env:host-wsl` | Docker Desktop WSL2 宿主机 |
| `env:host-phys` | 物理宿主机「遥遥领先」 |
| `db:mysql-core` | MySQL 8.0.46（max_connections=200） |
| `ds:pay` | 支付数据源（poolLimit=10） |
| `table:t_txn` | 交易表（热点） |
| `metric:mysql-p99` | MySQL P99 延迟（阈值 100ms） |
| `metric:conn-exhaust` | 连接耗尽信号 |
| `alert:p99` | P99 阈值告警规则 |
| `incident:payment-timeout` | 支付超时事件 |
| `rc:slow-sql` | 根因：t_txn 慢查询致连接耗尽 |
| `con:p99-threshold` | 约束：P99 < 500ms |
| `con:pool-exhaust` | 约束：连接池不耗尽 |
| `con:db-maxconn` | 约束：连接数 < max_connections |

---

## 典型场景与预期结果

| 告警文本 | 预期根因类别 | 预期实体 |
|---|---|---|
| `payment-app P99 latency > 500ms, MySQL connection pool exhausted` | 数据 | `rc:slow-sql` |
| `容器 OOM 重启，内存使用率 98%` | 资源 | `env:container` |
| `MySQL 慢查询增多，t_txn 表响应变慢` | 数据 | `rc:slow-sql` |
| `磁盘空间不足 disk full` | 资源 | `env:container` |
| `网络超时，连接失败` | 数据 | `rc:slow-sql` |
| `部署后服务不可用 502` | 依赖 | `incident:payment-timeout` |

---

## 验证

```bash
python tools/prod_verify.py            # 生产栈 28 项
python tools/scoring_verify.py         # 推理不变量 98 项
python tools/persistence_verify.py     # MySQL 持久化 13 项
python tools/fe_flow_verify.py         # 前端流程 38 项
python tools/fault_drill.py            # 故障演练 14 项
python tools/alert_firing_verify.py    # 告警触发 10 项
```

---

## 限制与注意事项

- 置信度反映**关键词与本体匹配度**，非统计学概率；高置信度仍需人工确认
- `relation` 节点（`rel:*`）是边，被降权，不会成为最终根因
- 拓扑完整性依赖 `.evoontology` 工作区；未挂载时节点数会显著减少
- EvoOntology 自演化需累积 ≥30 条轨迹或 ≥7 天
- 无 Docker 时可用 `python tools/rca_engine.py`（仅需本地 Python + 本体文件）
