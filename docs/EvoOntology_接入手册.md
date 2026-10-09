# EvoOntology 接入手册 — 信用卡系统运维智能体

> 本文档描述 EvoOntology 在本项目中的**完整能力接入**：本体构建、MCP 运行时、自演化、可视化、验证。

## 0. 能力全景

| 能力 | 实现 | 入口 |
|---|---|---|
| **本体构建** | `tools/init_evo_ontology.py`（TTL→5 族记录映射）+ `save_version` | CLI / MCP |
| **MCP 运行时** | `evoontology.runtime.mcp_server`（零依赖 stdio JSON-RPC） | `mcp__evoontology__*` |
| **自演化** | `EvolutionSession` + `EvolutionTrigger` + 评估闸门 | `start_evolution_run` … `accept_evolution` |
| **验证** | `evoontology.validate`（5 族文件 + 交叉引用 + 运行时可加载） | `validate_semantics` |
| **可视化** | 单文件 HTML（Cytoscape vendored，离线）+ 回环 preview server | `visualize_ontology` / `python -m evoontology.visualization` |
| **轨迹记录** | `ProjectWorkflow`（任务、事件、轨迹） | `start_ontology_task` … `finish_ontology_task` |

## 1. 目录结构

```
credit-card-sys-ops/
├── vendor/EvoOntology/          # 克隆的核心 (git: ruc-datalab/EvoOntology)
├── .evoontology/                # 工作区 (已生成 ontology_v0)
│   ├── active.json              # 当前激活版本指针
│   ├── project.json           # 项目上下文 (rolling_trajectory 模式)
│   ├── state.json             # 演化触发器检查点
│   ├── versions/ontology_v0/  # 5 族记录文件
│   │   ├── terms.json         # 18 个概念/指标/实体
│   │   ├── mappings.json      # 7 条概念→物理数据源接地
│   │   ├── relations.json     # 20 条受控关系边 (5 种类型)
│   │   ├── constraints.json   # 3 条 RCA 业务规则
│   │   └── evidence.json      # 6 条可复现观测
│   ├── trajectories/          # 任务轨迹 (演化证据)
│   ├── evolution/             # 演化运行记录
│   └── visualizations/
│       └── ontology-layer-explorer.html   # 多版本离线可视
├── demo/                        # Docker 模拟环境 (cc-credit-card-app + cc-mysql-core)
├── ontology_turtle/            # 规范定义 (OWL/TTL, 单一事实源)
├── tools/init_evo_ontology.py  # TTL→EvoOntology 5 族映射 + 工作区初始化
├── config/evoontology.cordis.yml # DSH dsh-mcp-client 接入 overlay
└── tools/
```

## 2. 首次构建（已完成）

```bash
# 1. 克隆 EvoOntology (已执行)
git clone --depth 1 https://github.com/ruc-datalab/EvoOntology.git vendor/EvoOntology

# 2. 初始化 .evoontology 工作区 + ontology_v0 (已执行)
python tools/init_evo_ontology.py
# 输出: counts={terms:18, mappings:7, relations:20, constraints:3, evidence:6}
#        active_version=ontology_v0, evolution_state 已初始化

# 3. 验证 (已执行, 0 错误)
python -m evoontology.validate --root .evoontology

# 4. 生成可视化 (已执行)
python -m evoontology.visualization --root .evoontology --no-browser
# 输出: .evoontology/visualizations/ontology-layer-explorer.html
```

**TTL 概念 → EvoOntology 5 族映射规则**（`tools/init_evo_ontology.py` 内置）：

| TTL 概念 | EvoOntology 族 | 映射 |
|---|---|---|
| 类/个体（Application、Container、HostMachine、Database、Table、Metric、Incident、RootCause…） | **Term** | `id` 用 `scope:entity` 形式 |
| 数据属性落地（docker inspect、MySQL 元数据、app /topology 端点） | **Mapping** | `table`+`column` 指向物理源 |
| 对象属性（`runsOn`/`deployedOn`/`hosts`/`accesses`/`hasDataSource`/`containsTable`/`triggers`/`hasRootCause`/`attributedTo`/`evidencedBy`） | **Relation** | 按 5 种受控类型归类 |
| 业务规则（连接池耗尽、P99 阈值、maxConnections 容量） | **Constraint** | `trigger_keywords` 让 RCA 可发现 |
| 可复现观测（docker ps、@@ 变量查询、information_schema、WMI） | **Evidence** | 支撑所有语义断言 |

### 对象属性 → 受控 relation_type 归类

| TTL 边 | relation_type | 语义 |
|---|---|---|
| `runsOn` / `deployedOn` / `hosts` / `containsTable` / `exposes` / `hasComponent` | `composition` | 整体-部分 |
| `accesses` / `hasDataSource` / `dataSourceOf` / `triggers` / `hasRootCause` / `attributedTo` / `evidencedBy` / `produces` | `association` | 访问/引用/证据 |
| 同指（HostWsl≡HostPhys） | `equivalence` | 等价 |
| 传递派生（`runsOn ∘ deployedOn` → 应用→宿主机） | `derivation` | 推导 |
| 弱相关（如表与指标） | `association` | 兜底 |

## 3. 接入 DSH（dsh-mcp-client）

### 3.1 一次性接入

```bash
# 方式 A: 临时 overlay (本次会话)
dsh web --patch "D:\05_code\credit-card-sys-ops\config\evoontology.cordis.yml"

# 方式 B: 跨 profile 持久化
# 把 config/evoontology.cordis.yml 的 insert 段落合并到 $DSH_HOME/cordis.patch.yml
# (不要覆盖已有内容)
```

### 3.2 验证接入

DSH 启动后，工具会以 `mcp__evoontology__<tool>` 形式注册。冒烟：

| 工具 | 用途 | 示例参数 |
|---|---|---|
| `mcp__evoontology__browse_semantics` | 发现与某需求相关的语义概念 | `{"query":"宿主机 容器 payment-app","kind":"all","limit":6}` |
| `mcp__evoontology__resolve_semantics` | 把提及解析到接地映射 + 关联对象 | `{"mentions":["t_txn","慢查询","连接池耗尽"],"context":"RCA"}` |
| `mcp__evoontology__validate_semantics` | 验证某版本 | `{"workspace":"...\.evoontology","version":"ontology_v0"}` |
| `mcp__evoontology__list_versions` | 列出版本 | `{"workspace":"..."}` |
| `mcp__evoontology__visualize_ontology` | 生成多版本 HTML | `{"workspace":"...","open_browser":true}` |
| `mcp__evoontology__evolution_status` | 查演化触发器 | `{"workspace":"..."}` |

### 3.3 环境注意

- `command` 指向 DSH 自带运行时 `C:\Users\41187\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`
- `env.PYTHONPATH` 指向 `vendor/EvoOntology`（core 包）
- `env.PYTHONIOENCODING=utf-8`（Windows 代码页防 mojibake；server 内部也做了 `force_utf8_stdio`）
- DSH 会自动清洗含 `KEY/PASSWORD/SECRET/TOKEN` 的 env，本项目 MCP server 无敏感依赖

## 4. 自演化（本体持续演进）

### 4.1 触发器

`EvolutionTrigger` 默认：`min_new_trajectories=30` 或 `min_days=7`（可经 `state.json.thresholds` 覆盖）。
触发器**只提醒**，不自动启动演化。

### 4.2 演化会话协议

```
start_evolution_run(parent_version)         # 冻结轮次预算
  ↓
begin_evolution_round(hypothesis, candidate_version)   # 开一轮
  ↓ (LLM 诊断轨迹 → 提候选 patch → save_version(candidate))
record_evolution_evaluation(subject, result, role)     # 记录成对评估
  ↓
record_evolution_round(decision="reject", ...)         # 拒绝，继续
  或
accept_evolution(new_version)                          # 接受：验证→发布→激活→推进检查点
  ↓
finalize_evolution_run()                              # 终态 + 自动渲染结果页
```

- 预算耗尽/外部阻塞：`mark_evolution_incomplete(reason)`
- 用户续期：`extend_evolution_budget(max_rounds)`
- 评估必须**Parent vs Candidate** 在相同数据/agent/解码设置下成对比较

### 4.3 轨迹来源（自演化证据）

- `start_ontology_task` → `record_ontology_task_event` → `finish_ontology_task` 累积
- `execute_ontology_query`：仅 SQLite 数据源可直接执行；本项目 MySQL 用 host 工具（`docker exec mysql`）+ `record_ontology_task_event` 记录真实结果
- `resolve_ontology_task`：把任务内的提及解析到固定语义版本

### 4.4 何时演化

| 信号 | 建议 |
|---|---|
| 新增 app/容器/数据库 | 走 `start_evolution_run`，候选 patch 加 Term + Mapping + Relation |
| RCA 新发现根因类别 | 加 Term（RootCause）+ Constraint + Evidence |
| 指标口径变更 | 修 Mapping（`aggregation_semantics`）+ 更新 Evidence |
| 拓扑变化（容器迁移宿主机） | 修 Relation（`deployedOn` 端点） |

## 5. 可视化

### 5.1 多版本离线 HTML

```bash
python -m evoontology.visualization --root .evoontology --no-browser
# → .evoontology/visualizations/ontology-layer-explorer.html
```

- 单文件自包含（Cytoscape.js 已 vendored），**离线可用**
- 含全部版本切换对比 + Schema 层 + Tool 层 manifest + 各版本经验
- 节点 4 族（Term/Mapping/Constraint/Evidence）+ 关系边 5 种 + 结构引用边 3 种（`grounded_by`/`constrained_by`/`supported_by`）

### 5.2 回环预览服务器

```python
from evoontology.visualization.preview import ensure_preview
url = ensure_preview(html_path)
# → http://127.0.0.1:<port>/<token>/ontology.html
```

- 127.0.0.1 回环 + token 鉴权，空闲 1 小时自动退出
- DSH 会话内：可让 agent 调 `mcp__evoontology__visualize_ontology` 拿 `browser_url`，在 GUI 打开

## 6. 本体演进路线图

> **2026-10-05 更新**：原表格写着"自演化 ⏳ 待轨迹达 30 条或 7 天后启动首轮"，
> 但实际**已完成 5 轮并全部通过成对评估闸门**（见 §8.6/§8.10/§8.11）。
> 文档落后于事实会直接误导读者对成熟度的判断，故按实测状态重写。

| 阶段 | 能力 | 现状 |
|---|---|---|
| **TTL 规范定义** | OWL/TTL 作为单一事实源 | ✅ `ontology_turtle/` 已交付 |
| **EvoOntology 运行时本体** | 5 族记录 + 版本化 | ✅ `ontology_v0` 起，当前激活 **`ontology_v5`**（54 术语 / 20 映射 / 82 关系 / 29 约束 / 21 证据），全部版本 `validate` 0 错误 |
| **MCP 接入** | agent 可 browse/resolve | ✅ 配置 + 冒烟通过 |
| **可视化** | 多版本离线 HTML | ✅ 已生成（每轮重渲染） |
| **轨迹积累** | 任务轨迹累积 | ✅ 已积累 `run_1..run_6` 六次演化运行 + 21 场景验证/端到端报告作为轨迹来源 |
| **自演化** | 归因驱动候选 patch + 成对评估闸门 | ✅ **已完成 5 轮**：集群化(0.048→1.000)、打分消歧(top1 11→20/21)、卫生(契约 0.699→1.000)、处置动作入库(0.000→1.000)、命令可执行性(0.853→1.000)；每轮 0 回退 |
| **处置动作（remediation）** | 根因术语自带"怎么办" | ✅ ontology_v4 引入、v5 修正可执行性；实测有效性 **10/10**（§11.23） |
| **TTL↔EvoOntology 双向映射** | 规范定义 → 运行时本体 自动同步 | ⏳ **未做**：当前 `tools/init_evo_ontology.py` 单向。注意 `scoring_keywords` / `negative_keywords` / `lifecycle` / `remediation` 都是**运行时侧扩展字段**，反向映射时需决定是否回写 TTL |
| **多集群/跨命名空间** | 跨环境推理 | ⏳ **未做**：本体有 `app:*-cluster` 概念，但只有单套 compose 环境可验证 |
| **端到端严格口径** | `root_cause.entity_id` 落在期望术语上 | ✅ 已实质解决：早期 0/24 → 全量重测 **17/21 = 81%**；完整链路那轮 11/21，**有告警子集 11/14 = 79%**。差距来自**输入可得性**（无告警文本时引擎近乎空手），不是引擎退化 |


## 7. 常见问题

**Q: DSH MCP 工具未出现？**
- 确认 overlay 是否传入 `dsh web --patch ...`
- 确认 `python` 路径存在且 `PYTHONPATH` 指向 `vendor/EvoOntology`
- 看 DSH 日志 `mcp-evoontology` 条目；`failOnStartupError:false` 下失败不阻塞启动

**Q: 可视化 HTML 在 DSH 会话里打不开？**
- 用 `ensure_preview` 拿回环 URL，或在 DSH GUI 的 Files 侧栏打开 `ontology-layer-explorer.html`
- DSH 的 `present` 卡片也可交付该 HTML

**Q: 如何把新发现的 RCA 知识加进本体？**
- 走自演化协议：`start_evolution_run` → 候选 patch → `save_version` → `record_evolution_evaluation` → `accept_evolution`
- 不要直接改 `versions/ontology_v0/`（那是激活版本），应新增候选版本再提升

**Q: 与 TTL 的同步策略？**
- 当前：TTL 为规范定义（人读、审计、跨工具）；EvoOntology 5 族为运行时（agent 查改）
- 建议：TTL 修改后重跑 `tools/init_evo_ontology.py` 作为新一版候选；EvoOntology 演化发布后反向生成 TTL diff 供审计

---

## 8. 第四次扩展实录：集群化本体迭代（ontology_v1）

环境从"单容器 + 单库"扩展为"3 副本应用 + Nginx 网关 + 1 主 2 只读副本 MySQL"后，
本体必须同步长出集群概念。这次迭代**完整走了一遍自演化协议**，
并留下了可复算的成对评估证据。

### 8.1 执行

```bash
python tools/evolve_ontology_cluster.py --dry-run   # 演练：只评估不发布
python tools/evolve_ontology_cluster.py             # 正式执行
python tools/evolve_ontology_cluster.py --status    # 查看演化运行
```

协议轨迹（`\.evoontology/evolution/run_1/`）：

```
start_run(parent_version="ontology_v0-rca-agent",
          adapter="ground_truth", max_rounds=3,
          acceptance={"protocol": "ground_truth"})   → run_1
begin_round(hypothesis, candidate_version="ontology_v1-cluster")   → round=1
SemanticStore.save_version(ws, "ontology_v1-cluster", parent ∪ patch)
validate(ws, version="ontology_v1-cluster")           → passed=True, errors=[]
confirm_trajectory_sources([...])                    → trajectory-sources.json
record_evaluation("ontology_v0-rca-agent", {...}, role="parent")
record_evaluation("ontology_v1-cluster", {..., gate_input={...}}, role="candidate")
accept(new_version="ontology_v1")                    → EvaluationGate.decide_gt 通过
finalize()                                           → status=accepted
```

### 8.2 增量规模（5 族）

| 族 | 父版本 | 增量 | 候选/发布 |
|---|---|---|---|
| terms | 18 | **+33** | 51 |
| mappings | 7 | **+9** | 16 |
| relations | 20 | **+54** | 74 |
| constraints | 3 | **+13** | 16 |
| evidence | 7 | **+7** | 14 |

新增内容覆盖：应用集群 / 副本 / 网关、数据库主从集群 / 只读副本 / 复制链路、
CPU 配额节流与内存压力、行锁 / 连接耗尽 / 磁盘临时表 / 主库只读 / 复制延迟中断、
黑盒探活与健康副本数等可观测连接，以及 13 条与告警规则 `onto_constraint` 对齐的约束。

### 8.3 成对评估（评估闸门）

指标：**本体可诊断性**（structural diagnosability，3 项等权）——
① 期望根因 Term 已定义；② 该 Term 与用户可见入口在关系图中 ≤4 跳连通；
③ 该 Term 可观测（metric / 直连 metric / 约束 target）。

| 版本 | 平均分 | 满分 case | 零分 case |
|---|---|---|---|
| 父 `ontology_v0-rca-agent` | **0.048** | 1 / 21 | 20 / 21 |
| 候选 `ontology_v1-cluster` | **1.000** | 21 / 21 | 0 / 21 |
| Δ | **+0.952** | +20 | −20 |

`EvaluationGate.decide_gt(parent_scores, candidate_scores)` →
`{"accept": true, ...}`（候选均值严格大于父版本），且 `unacceptable_regressions=false`
（逐 case 比对，**无任何 case 分数下降**）。

### 8.4 一个诚实的限制（也是下一轮迭代的目标）

本体侧知识补齐后，**确定性规则引擎的候选集只提升到 top1 2/21、top5 4/21**
（`tools/cluster_rca_verify.py --probe-only`，迭代前为 1/21、1/21）。
原因：`rca_engine.category_keywords` 是**代码内置**的关键词表，
`rc:row-lock` / `rc:replica-loss` 这类术语不会被关键词命中。

结论：本体迭代解决的是"知识是否存在 / 是否连通 / 是否可观测"，
解决不了"引擎是否会用这份知识"。下一步应把关键词表也变成本体驱动
（从 Term 的 `aliases` 与 Constraint 的 `trigger_keywords` 动态构建），
这是本体迭代能力的自然延伸目标。

### 8.5 审计产物

| 产物 | 路径 |
|---|---|
| 演化运行记录 | `.evoontology/evolution/run_1/{run.json, rounds.jsonl, evaluations/, trajectory-sources.json}` |
| 父/候选成对评估 | `.evoontology/evolution/run_1/evaluations/round1_{parent,candidate}_*.json` |
| 版本说明与限制 | `.evoontology/reports/ontology_v1.json` |
| 迭代报告（含 Δ） | `.chaos/ontology_evolution.json` |
| 多版本离线可视化 | `.evoontology/visualizations/ontology-layer-explorer.html` |
| 检索能力 A/B 探针 | `.chaos/ontology_probe_before.json` / `.chaos/ontology_probe_after.json` |
| TTL 单一事实源增量 | `ontology_turtle/{ontology,instances,topology}.ttl` + `sparql_queries.txt` Q7–Q12 |

### 8.6 版本谱系

```
ontology_v0            首次构建（TTL → 5 族映射，单实例视图）
ontology_v0-rca        RCA evidence 注入
ontology_v0-rca-agent  双引擎 Agent 接入后的 evidence 累积（第 1 轮的父版本）
ontology_v1-cluster    集群化候选版本（未激活，作为发布来源保留）
ontology_v1            ✅ 第 1 轮发布：集群化运行时本体（validate 0 错误）
ontology_v2-scoring    消歧/角色候选版本（未激活，作为发布来源保留）
ontology_v2            ✅ 第 2 轮发布：打分消歧 + 角色声明（反向迭代）
ontology_v3-hygiene    卫生清理候选版本（未激活，作为发布来源保留）
ontology_v3            ✅ 第 3 轮发布：游离术语清零 + 索引补齐 + lifecycle 声明
ontology_v4-remediation 处置动作候选版本（未激活，作为发布来源保留）
ontology_v4            ✅ 第 4 轮发布：15 个根因术语登记 remediation（可执行性）
ontology_v5-command-targets 命令可执行性修正候选版本（未激活，作为发布来源保留）
ontology_v5            ✅ 当前激活：修正指向不存在指标的/散文式命令，契约加 ④⑤ 条（第 5 轮·实测驱动）
```

### 8.11 第 5 轮：实测驱动的命令修正（ontology_v5）

第 4 轮的契约只验证**纸面可用性**（动作具体 / urgency 合法 / 声明审批）。
`tools/remediation_efficacy.py` 用"注入真实故障 → 真跑诊断 → 执行 P0 命令"的闭环实测，
挖出两类纸面上看不出来的缺陷：

| 类型 | 实例 | 后果 |
|---|---|---|
| 命令引用的指标**不存在** | `rc:cpu-throttle` 写 `cc_container_cpu_cfs_throttled_periods_total`（Prometheus 里 0 series） | 照着排查只得到空结果 —— 与"恒为绿"的证据同样危险 |
| `command` 字段里放的是**散文** | 12 条，如 `按 app_instance 对比 P99`、`逐个副本 /health 探活` | 界面按等宽命令渲染，误导操作人且无法自动验证 |

| 项 | 值 |
|---|---|
| 轮次 | `--round command-targets`（run_6） |
| 修正 | 11 个术语的命令（含占位符统一为 ASCII） |
| 契约加固 | ④ 指标必须真实存在、⑤ `command` 必须是不含说明文字的命令 |
| 成对评估 | 父 v4 在新口径下 **0.853**（满分 5/15）→ 候选 **1.000**（15/15）；改善 10、回退 0 |
| 发布闸门 | 全术语命令可执行性扫描 PASS + 卫生闸门 PASS → 激活 `ontology_v5` |

**处置动作的实测有效性**（`tools/remediation_efficacy.py`，同批 10 个可测场景）：
术语级 **10/10**（至少一条 P0 探针在"基线阴性 + 故障期阳性"上成立）；
同时实测暴露 **A/B 口径的输入含答案描述** —— 换成真实告警注解后 top1 为
**16/21 = 76%**，而 A/B 口径为 20/21 = 95%。详见验证手册 §11.23 / §11.24。

### 8.7 第 2 轮：反向迭代（引擎精度需求 → 本体 schema 扩展）

第 1 轮只解决"知识有没有"（知识完备性 0.048 → 1.000）。但把打分词典改为
本体驱动后（`rca-agent/backend/services/ontology_scoring.py`），
引擎 top1 仍只有 11/21 —— "副本丢失 / 副本假死 / 副本 OOM / 副本被节流"
共享"副本"一词，**关键词打分无法区分同一实体的不同故障模式**。

这不是缺知识，而是**缺表达能力**：术语只能声明自己是什么，
不能声明"什么情况下**不是**我"。于是第 2 轮给 Term 增加三个字段：

| 字段 | 语义 | 示例 |
|---|---|---|
| `scoring_keywords` | 判别性正向关键词（比 name/alias 更贴近告警措辞） | `rc:oom-kill`: `oom killed`、`内存超限`、`被内核杀死` |
| `negative_keywords` | 排除词（"我不是这个故障"的显式声明），引擎按命中数扣分 | `rc:replica-loss`: `!假死`、`!pause`、`!节流`、`!内存` |
| `role` | 术语在 RCA 中的角色 → 权重 | `root_cause` 1.15 / `component` 1.00 / `observation` 0.85 / `rule` 0.80 |

并新增 11 条管辖约束（`con:replica-loss-detection`、`con:db-cpu-capacity`、`con:tmp-disk`、
`con:net-quality`、`con:fastpath-trust` …），把"哪个约束管这个根因"也写进本体。

**评估口径的切换**：第 1 轮用结构性可诊断性；第 2 轮知识已完备（恒为 1.000，不可作闸门），
改用 **引擎 top1 精确命中率**（每 case 0/1，ground truth = 场景期望根因 Term）。

**执行**：`python tools/evolve_ontology_cluster.py --round scoring`

```
run_3: start_run(ontology_v1, acceptance={protocol:ground_truth})
  begin_round(hypothesis, "ontology_v2-scoring")
  save_version(...)  → validate(passed=True, errors=[])
  record_evaluation(ontology_v1, {...}) / record_evaluation(candidate, {gate_input})
  accept("ontology_v2")  → EvaluationGate.decide_gt: accept=True, Δ=+0.429
  finalize()  → status=accepted
```

| 项 | 值 |
|---|---|
| 增量 | terms updated 21、constraints +11、evidence +3 |
| 父版本 top1 | 0.524（11/21） |
| 候选版本 top1 | **0.952（20/21）**，top5 21/21 |
| 提升 / 回退 case | **9 / 0** |
| 闸门 | `{"accept": true, "parent_score": 0.5227, "candidate_score": 0.9524, "delta": 0.4286}` |

> ⚠️ `scoring_keywords` / `negative_keywords` / `role` 是对 EvoOntology Term schema 的
> **项目侧扩展**：`validate()` 只强制 `id`，额外字段可安全共存，
> 运行时（`Term.from_dict`）会忽略未知字段。若上游要正式支持，建议纳入 `ontology-schema.md`。

### 8.9 第 3 轮：本体卫生（游离术语 + 缺失概念 + lifecycle）

**提问式发现**："v2 里为什么有很多游离、没有任何关联关系的实体，比如『应用发布事件』？
在什么场景会用到？" —— 这是**本体质量**问题，不是知识缺口问题。

```bash
python tools/ontology_hygiene.py --version ontology_v2   # 审计 + 闸门（失败 exit 1）
```

**审计 `ontology_v2`**：游离术语 **2 个**（`env:cluster`、`evt:deploy`，关系图度=0），
从入口不可达 2 个、无证据 2 个、引擎可见拓扑 49/51、
**51 个术语全部未声明 lifecycle**。期望根因 19 个全部可达，所以 top1 不受影响 ——
但它们会出现在 `browse_semantics` 结果里污染语义检索
（实测 `browse_semantics('发布 变更 deploy 上线')` **唯一返回的就是 `evt:deploy`**，
而 `resolve_semantics` 只给 `coverage_status=partial` + 空证据）。

**根因**：首版 TTL→5 族转换器漏映射 3 个对象属性 ——
`managedBy`（→`env:cluster` 游离）、`generatesEvent`（→`evt:deploy` 游离）、
`hasIndex`（→索引个体连 Term 都没建）。

**三项修复**（`tools/ontology_v3_patch.py` + `init_evo_ontology.py`）：

| 工作 | 内容 |
|---|---|
| A 接线 | `evt:deploy` 接入变更关联链条（`generatesEvent` + `metric:uptime-since-deploy` + `con:change-correlation`）；`env:cluster` 补 `managedBy` 并改名消歧；补 `idx:txn-created`/`idx:txn-customer` + `hasIndex` + 接地到 `information_schema.statistics` + `con:index-usage` |
| B lifecycle | 54 个术语全部声明；未接线的必须标 `draft`（运行时只在 validated/active 时参与查询） |
| C 闸门 | `accept()` 前跑 `gate_records()`：期望根因游离 / 未声明 lifecycle / active 却游离 / 不可确认根因 → `mark_incomplete("unreliable_evaluation")` 并拒绝发布 |

**评估口径 = 本体卫生契约**（`ontology_hygiene.term_contract()`，逐术语 4 条等权）：

| 契约 | 含义 |
|---|---|
| C1 | 声明了 lifecycle |
| C2 | 关系图度为 0 → 必须标 `draft` |
| C3 | 声明 `active`/`validated` → 必须至少有一条关系 |
| C4 | 根因类术语必须可观测（metric 关联 / 本身是 metric / 是约束 target） |

**执行**：`python tools/evolve_ontology_cluster.py --round hygiene`

```
run_4: start_run(ontology_v2, acceptance={protocol:ground_truth})
  begin_round(hypothesis, "ontology_v3-hygiene")
  save_version → validate(passed=True, errors=[])
  record_evaluation(parent) / record_evaluation(candidate, {gate_input})
  accept("ontology_v3")  → decide_gt: accept=True, Δ=+0.301
  hygiene gate: PASS（游离 0、契约 1.000）
  finalize() → status=accepted
```

| 项 | 值 |
|---|---|
| 增量 | terms added 3 / updated 51、mappings +4、relations +8、constraints +2、evidence +4 |
| 卫生契约均分 | 0.699 → **1.000**（提升 54 case、回退 0） |
| 引擎 top1（回归观察） | 0.952 → **0.952**（无回归），top5 21/21 |
| 游离 / 不可达 / 不在拓扑 | 2 → **0** / 2 → **0** / 2 → **0** |

> **两个 EvoOntology 接入注意点（本轮踩到）**
>
> 1. **闸门要求成对 case 集等长**。本轮新增了 3 个术语（51 → 54 个 case），
>    直接提交会被 `_publication_gate` 拒绝。正确做法是**按 case_id 并集对齐**，
>    父版本没有的术语记 0 分（"不存在"本就等于"契约不满足"），
>    而不是取交集把新术语排除在评估之外。
> 2. **版本目录必须视为不可变历史**。`SemanticStore.save_version` 会**原地覆盖**同名版本；
>    `init_evo_ontology.py` 早期只拦了"无参数重跑"，`--force` 之后仍是覆盖。
>    现已改为**写前归档**到 `.evoontology/archive/<version>-<UTC 时间戳>/`。
>
> 另外提醒：`lifecycle.state` 不在 `{"validated","active"}` 时，`runtime.py`
> 会把它当作非激活术语、不再参与语义查询 —— 这正是"草稿态术语"该有的行为，
> 也是第 3 轮清理游离术语的机制基础。

### 8.8 双向闭环的完整证据链（四轮）

| 配置 | top1 | top5 | 快路径例数 | 快路径 top1 精确率 | 卫生契约 |
|---|---|---|---|---|---|
| ① 旧引擎 + 父本体 | 1/21 | 1/21 | 0 | n/a | — |
| ② 新引擎 + 父本体 | 1/21 | 1/21 | 2 | 50% | — |
| ③ 新引擎 + `ontology_v1` | 11/21 | 20/21 | 16 | 62% | — |
| ④ 新引擎 + `ontology_v2` | **20/21** | **21/21** | 21 | **95%** | 0.699 |
| ⑤ 新引擎 + `ontology_v3` | **20/21** | **21/21** | 21 | **95%** | **1.000** |
| ⑥ 新引擎 + `ontology_v4` | **20/21** | **21/21** | 21 | **95%** | **1.000** |
| | | | | | 处置契约 **1.000** |

* **②→③**：引擎改造本身不产生知识（1→1），**本体才产生知识**（→11）。
* **③→④**：知识完备后仍不足，**本体补表达力**才能到 20/21。
* **④→⑤**：能力达标后再查**卫生**（游离/证据/生命周期），top1 不回归而契约拉满。
* **⑤→⑥**：打分为零回归的前提下把「怎么办」也交还本体（`remediation`），
  处置契约 0.000 → 1.000 —— 这一轮验证的是**可执行性**，不是精度。

线上探针（真实后端 `/api/agent/diagnose`）与离线 A/B 结果一致：
top1 **1/21 → 20/21**、top5 **1/21 → 21/21**。

复现：`python tools/ontology_scoring_verify.py --mid ontology_v1`
（第 4 轮回归观察：`--parent ontology_v3 --active ontology_v4`，应得"新命中 0 / 回退 0"）


### 8.10 第 4 轮：可执行性（结论的"怎么办"也交还本体）

前三轮解决的是"**知道是什么**"：知识完备性 → 打分消歧 → 术语卫生。
但诊断结论的最后一段"怎么办"一直是**代码里按 5 个类别硬编码**的表
（`orchestrator._suggest_actions(category)`），而根因有 15 个 ——
粒度对不上，于是"磁盘临时表"拿到的是"确认容器是否因内存/CPU 受限被杀"。

| 项 | 内容 |
|---|---|
| 新增字段 | Term.`remediation`：一组动作，结构与 `next_actions` 同构（`urgency / action / command / needs_approval`） |
| 覆盖 | 15 个 `rc:` 根因术语，共 50 条动作 |
| 评估口径 | `remediation_contract`：逐术语 3 条契约（动作具体 ≥12 字 / urgency ∈ P0-P2 / 显式声明 needs_approval）等权 |
| 成对评估 | 父 v3 **0.000**（0/15）→ 候选 **1.000**（15/15）；改善 15、回退 0 |
| 回归观察 | 引擎 top1 **20/21 → 20/21**、快路径精确率 95% 不变（本轮不动打分字段） |
| 发布闸门 | 处置动作不变式（**P0 一律只读**）+ 本体卫生闸门，双双 PASS → 激活 `ontology_v4` |

**关键设计：动作结构与 `next_actions` 同构。** 这样"本体动作"与"兜底动作"
在下游完全不可区分地形同，前端/markdown 一行都不用改；
只在每条动作上多带一个 `source`（`ontology` / `category`），界面据此标注出处。

**新增硬不变式：P0 必须只读。** 边界是"诊断只读、写操作需人工审批"；
若 P0 里混进需审批的写操作，界面上"最紧急的事"就变成"等人批准的事"。
这条已固化为代码并在 `accept` 前拦截 —— 第一次写补丁就违反了它
（`rc:db-replica-loss` 把"摘读流量"标成 P0），被自己的闸门拦下改成 P1 + 需审批。

**教训（写进纪律）**：本体自己的**发布记录**也要准确。
① 版本说明的分支曾只有 `cluster` / `else`，导致第 3 轮是用"打分消歧版本"的说明发布的；
② 本轮发布的假设里把术语总数（54）当成了根因数，实际 15 个。
两处都已修正 —— 发布记录写错，等于演化历史失真。

详细的动机、验证与限制见验证手册 §11.22。
