# -*- coding: utf-8 -*-
"""基于现有 TTL 本体 + Docker 模拟环境数据，生成 .evoontology 工作区的 ontology_v0。

映射关系（TTL 概念 -> EvoOntology 记录族）：
  TTL 类/个体 (Application/Container/HostMachine/Database/...) -> Term
  TTL 数据属性落地到具体数据源 (docker inspect / MySQL 元数据) -> Mapping
  TTL 对象属性 (runsOn/deployedOn/accesses/containsTable/...)  -> Relation
    映射为 5 种受控 relation_type:
      runsOn/deployedOn -> composition (整体-部分)
      accesses/hasDataSource -> association
      同义实体 (HostWsl/HostPhys 同指) -> equivalence
      可推导实体 (应用->宿主机 传递路径) -> derivation
  RCA 业务规则 (连接池耗尽/慢 SQL 判定) -> Constraint
  可复现观测 (docker inspect 输出 / MySQL 变量查询结果) -> Evidence

用法：
  python -m tools.init_evo_ontology
或
  python tools/init_evo_ontology.py

⚠️ 该脚本是"**首次构建**"工具：它会把 ontology_v0 重新写一遍并把 active.json
指回 ontology_v0。如果本体已经演化过（例如当前激活 ontology_v1），
直接运行会**回退激活版本**。因此脚本会检测这种情况并要求显式 --force。
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vendor" / "EvoOntology"))

from evoontology.ontology.store import SemanticStore  # noqa: E402
from evoontology.workspace import save_project        # noqa: E402

WORKSPACE = ROOT / ".evoontology"

NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")

# ===================================================================
# 数据源：复用已采集的快照
# ===================================================================
snap = json.loads((ROOT / "demo" / "topology-snapshot.json").read_text(encoding="utf-8-sig"))
top = snap["topologyBaseline"]
host = snap["hostMachine"]
docker = snap["docker"]
raw = snap["rawProbe"]

# ===================================================================
# Evidence（可复现观测）
# ===================================================================
evidence = [
    {
        "id": "ev-docker-ps",
        "source": "docker-compose",
        "query": "docker ps --format 'table {{.Names}}\\t{{.Status}}'",
        "result": "cc-credit-card-app: Up (healthy); cc-mysql-core: Up (healthy)",
        "validation_method": "read-only docker CLI",
        "timestamp": NOW,
    },
    {
        "id": "ev-app-topology",
        "source": "http://cc-credit-card-app:8080/topology/raw",
        "query": "GET /topology/raw",
        "result": json.dumps({
            "app": raw["app"],
            "containerIP": raw["network"]["containerIP"],
            "db": {"endpoint": f'{raw["db"]["host"]}:{raw["db"]["port"]}',
                   "reachable": raw["db"]["probe"]["reachable"],
                   "version": raw["db"]["probe"]["ping"]["version"],
                   "maxConn": raw["db"]["probe"]["ping"]["maxConn"]},
        }, ensure_ascii=False),
        "validation_method": "http probe",
        "timestamp": NOW,
    },
    {
        "id": "ev-mysql-vars",
        "source": "mysql:creditcard",
        "query": "SELECT @@version, @@max_connections, @@wait_timeout",
        "result": f'{raw["db"]["probe"]["ping"]["version"]}, {raw["db"]["probe"]["ping"]["maxConn"]}, {raw["db"]["probe"]["ping"]["waitTimeout"]}',
        "validation_method": "MySQL @@ variables",
        "timestamp": NOW,
    },
    {
        "id": "ev-mysql-tables",
        "source": "mysql:creditcard",
        "query": "SELECT table_name, table_rows FROM information_schema.tables WHERE table_schema='creditcard'",
        "result": "t_customer=3, t_txn=3, t_audit=0",
        "validation_method": "information_schema",
        "timestamp": NOW,
    },
    {
        "id": "ev-host-facts",
        "source": "windows-host",
        "query": "Get-CimInstance Win32_Processor / Win32_OperatingSystem / Win32_PhysicalMemory",
        "result": f'{host["name"]}: {host["cpu"]} {host["cpuCores"]}C/{host["logicalCores"]}T, {host["ramGB"]}GB RAM, {host["os"]}',
        "validation_method": "WMI",
        "timestamp": NOW,
    },
    {
        "id": "ev-db-probe",
        "source": "http://cc-credit-card-app:8080/topology/raw",
        "query": "GET /topology/raw -> db.probe",
        "result": f'reachable=true, latency={raw["db"]["probe"]["latencyMs"]}ms',
        "validation_method": "app-side JDBC probe",
        "timestamp": NOW,
    },
    {
        "id": "ev-mysql-indexes",
        "source": "mysql:information_schema.statistics",
        "query": ("SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, NON_UNIQUE, CARDINALITY "
                  "FROM information_schema.statistics "
                  "WHERE TABLE_SCHEMA='creditcard' AND TABLE_NAME='t_txn'"),
        "result": ("idx_txn_created(created_at), idx_txn_customer(customer_id), PRIMARY(txn_id) "
                   "—— 用于判断慢查询是否命中索引（配合 EXPLAIN type=ALL / Using filesort）"),
        "validation_method": "MySQL information_schema.statistics 只读查询",
        "timestamp": NOW,
    },
]

# ===================================================================
# Terms（概念/指标/实体/维度/类别）
# ===================================================================
terms = [
    {"id": "app:payment-app", "name": "支付交易应用 payment-app", "type": "entity",
     "definition": "信用卡支付业务 Node.js 应用，版本 1.8.2，暴露 /health 与 /topology 端点",
     "scope": "应用层", "aliases": ["PaymentApp", "cc-credit-card-app"],
     "evidence_refs": ["ev-docker-ps", "ev-app-topology"]},
    {"id": "fn:auth", "name": "授权业务功能", "type": "entity",
     "definition": "payment-app 内的授权（Auth）功能，P0 关键度",
     "scope": "应用层", "aliases": ["AuthFunc"], "evidence_refs": ["ev-app-topology"]},
    {"id": "api:auth", "name": "授权接口 authAPI", "type": "entity",
     "definition": "RPC 授权接口，超时 500ms，3 次指数退避重试",
     "scope": "应用层", "aliases": ["AuthAPI"], "evidence_refs": []},
    {"id": "env:container", "name": "支付容器 cc-credit-card-app", "type": "entity",
     "definition": "承载 payment-app 的容器实例",
     "scope": "运行环境层", "aliases": ["PayContainer"],
     "evidence_refs": ["ev-docker-ps", "ev-app-topology"]},
    {"id": "env:host-wsl", "name": "宿主机（Docker Desktop WSL2）", "type": "entity",
     "definition": "Docker Desktop 的 WSL2 虚拟宿主，承载容器调度",
     "scope": "运行环境层", "aliases": ["HostWsl", "docker-desktop"],
     "evidence_refs": ["ev-docker-ps", "ev-host-facts"]},
    {"id": "env:host-phys", "name": "物理宿主机（遥遥领先）", "type": "entity",
     "definition": f'Windows 物理主机：{host["cpu"]}，{host["ramGB"]}GB RAM',
     "scope": "运行环境层", "aliases": ["HostPhys", host["name"]],
     "evidence_refs": ["ev-host-facts"]},
    {"id": "env:cluster", "name": "Docker Desktop 集群", "type": "entity",
     "definition": "Docker Desktop 引擎（WSL2 后端），8 CPU / 7.66 GB",
     "scope": "运行环境层", "aliases": ["DockerCluster"],
     "evidence_refs": ["ev-docker-ps", "ev-host-facts"]},
    {"id": "db:mysql-core", "name": "核心账务库 creditcard (MySQL 8.0)", "type": "entity",
     "definition": "MySQL 关系库，含 t_customer/t_txn/t_audit 三表；max_connections=200",
     "scope": "数据库层", "aliases": ["MysqlCore", "cc-mysql-core"],
     "evidence_refs": ["ev-mysql-vars", "ev-mysql-tables"]},
    {"id": "ds:pay", "name": "支付数据源", "type": "entity",
     "definition": "payment-app 的 JDBC 数据源，连接池上限 10，指向 mysql:3306",
     "scope": "数据库层", "aliases": ["PayDataSource"], "evidence_refs": ["ev-app-topology"]},
    {"id": "table:t_txn", "name": "交易表 t_txn", "type": "entity",
     "definition": "信用卡交易流水表，热表；含 customer_id 与 created_at 索引",
     "scope": "数据库层", "aliases": ["TTxn"], "evidence_refs": ["ev-mysql-tables"]},
    {"id": "table:t_customer", "name": "客户表 t_customer", "type": "entity",
     "definition": "信用卡客户与卡号表", "scope": "数据库层", "aliases": ["TCustomer"],
     "evidence_refs": ["ev-mysql-tables"]},
    {"id": "table:t_audit", "name": "审计表 t_audit", "type": "entity",
     "definition": "交易审计事件表（JSON payload）", "scope": "数据库层",
     "aliases": ["TAudit"], "evidence_refs": ["ev-mysql-tables"]},
    {"id": "metric:mysql-p99", "name": "MySQL P99 延迟", "type": "metric",
     "definition": "MySQL 查询 P99 延迟（ms），阈值 100ms",
     "scope": "RCA 支撑", "aliases": ["MysqlLatencyP99"], "evidence_refs": ["ev-db-probe"]},
    {"id": "metric:conn-exhaust", "name": "连接池耗尽信号", "type": "metric",
     "definition": "活跃连接数逼近 poolLimit(10) 的耗尽信号",
     "scope": "RCA 支撑", "aliases": ["ConnExhaustion"], "evidence_refs": ["ev-app-topology"]},
    {"id": "evt:deploy", "name": "应用发布事件", "type": "entity",
     "definition": "CI/CD 部署 payment-app 的事件（eventType=deploy）",
     "scope": "RCA 支撑", "aliases": ["DeployEvent"], "evidence_refs": []},
    {"id": "alert:p99", "name": "MySQL P99 超阈值告警", "type": "entity",
     "definition": "P1 告警，触发规则 mysql.latency.p99>100 for 5m",
     "scope": "RCA 支撑", "aliases": ["AlertP99"], "evidence_refs": ["ev-mysql-vars"]},
    {"id": "incident:payment-timeout", "name": "支付交易超时故障", "type": "entity",
     "definition": "P0 故障：支付成功率下降，关联 t_txn 慢查询",
     "scope": "RCA 支撑", "aliases": ["IncPaymentTimeout"], "evidence_refs": ["ev-db-probe"]},
    {"id": "rc:slow-sql", "name": "根因：t_txn 慢查询致连接耗尽", "type": "entity",
     "definition": "数据类根因，置信度 0.86，归因于 t_txn 索引缺失/慢查询",
     "scope": "RCA 支撑", "aliases": ["RootSlowSql"], "evidence_refs": ["ev-mysql-tables"]},

    # ── 补齐 TTL 里存在但首版没建的个体（索引）────────────────────────
    {"id": "idx:txn-created", "name": "t_txn 时间索引 idx_txn_created", "type": "entity",
     "definition": "t_txn(created_at) 索引；EXPLAIN 未命中时应归因到索引而非泛泛的『库慢』",
     "scope": "数据库层", "aliases": ["IdxTxnCreated"], "evidence_refs": ["ev-mysql-indexes"]},
    {"id": "idx:txn-customer", "name": "t_txn 客户索引 idx_txn_customer", "type": "entity",
     "definition": "t_txn(customer_id) 索引；按客户维度查询应命中该索引",
     "scope": "数据库层", "aliases": ["IdxTxnCustomer"], "evidence_refs": ["ev-mysql-indexes"]},
]

# ===================================================================
# Mappings（概念 -> 物理数据源的接地）
# ===================================================================
mappings = [
    {"id": "map:app-container", "term_id": "env:container",
     "database_source": "docker:cc-credit-card-app", "table": "docker_ps",
     "column": "NAMES", "semantic_filter": "NAMES='cc-credit-card-app'",
     "grain": "container", "validation": "docker ps 输出匹配",
     "evidence_refs": ["ev-docker-ps"], "confidence": "high"},
    {"id": "map:app-ip", "term_id": "env:container",
     "database_source": "http://127.0.0.1:8080/topology/raw", "table": "network",
     "column": "containerIP", "grain": "container",
     "semantic_filter": f"containerIP={raw['network']['containerIP']}",
     "validation": "GET /topology/raw", "evidence_refs": ["ev-app-topology"], "confidence": "high"},
    {"id": "map:db-version", "term_id": "db:mysql-core",
     "database_source": "mysql:creditcard", "table": "performance_schema",
     "column": "server_status", "semantic_filter": "Variable_name='@@version'",
     "aggregation_semantics": "scalar", "grain": "server",
     "validation": "SELECT @@version", "evidence_refs": ["ev-mysql-vars"], "confidence": "high"},
    {"id": "map:db-maxconn", "term_id": "db:mysql-core",
     "database_source": "mysql:creditcard", "table": "performance_schema",
     "column": "server_status", "semantic_filter": "Variable_name='@@max_connections'",
     "grain": "server", "validation": "SELECT @@max_connections",
     "evidence_refs": ["ev-mysql-vars"], "confidence": "high"},
    {"id": "map:table-rows", "term_id": "table:t_txn",
     "database_source": "mysql:information_schema", "table": "tables",
     "column": "table_rows", "semantic_filter": "table_schema='creditcard' AND table_name='t_txn'",
     "grain": "table", "validation": "information_schema 行数",
     "evidence_refs": ["ev-mysql-tables"], "confidence": "high"},
    {"id": "map:host-cpu", "term_id": "env:host-phys",
     "database_source": "windows:wmi", "table": "Win32_Processor",
     "column": "NumberOfCores", "grain": "host",
     "validation": "Get-CimInstance Win32_Processor",
     "evidence_refs": ["ev-host-facts"], "confidence": "high"},
    {"id": "map:latency-p99", "term_id": "metric:mysql-p99",
     "database_source": "http://127.0.0.1:8080/topology/raw", "table": "db.probe",
     "column": "latencyMs", "grain": "query",
     "validation": "app-side JDBC ping latency", "evidence_refs": ["ev-db-probe"],
     "confidence": "medium"},
]

# ===================================================================
# Relations（5 种受控类型）
# ===================================================================
relations = [
    # ---- composition（整体-部分）：应用->容器->宿主机 ----
    {"id": "rel:app-runs-on-container", "source": "app:payment-app",
     "relation_type": "composition", "target": "env:container",
     "connection_condition": "runsOn: 应用部署运行在容器内",
     "description": "payment-app 运行于 cc-credit-card-app 容器",
     "evidence_refs": ["ev-docker-ps", "ev-app-topology"]},
    {"id": "rel:container-deployed-on-host", "source": "env:container",
     "relation_type": "composition", "target": "env:host-wsl",
     "connection_condition": "deployedOn: 容器被调度到 WSL2 宿主",
     "description": "cc-credit-card-app 容器部署于 Docker Desktop WSL2 宿主",
     "evidence_refs": ["ev-docker-ps"]},
    {"id": "rel:host-wsl-hosts-container", "source": "env:host-wsl",
     "relation_type": "composition", "target": "env:container",
     "connection_condition": "hosts: 宿主机承载容器（deployedOn 逆向）",
     "description": "WSL2 宿主机承载支付容器", "evidence_refs": ["ev-docker-ps"]},
    {"id": "rel:db-contains-txn", "source": "db:mysql-core",
     "relation_type": "composition", "target": "table:t_txn",
     "connection_condition": "containsTable: 库内表",
     "description": "creditcard 库包含 t_txn 表", "evidence_refs": ["ev-mysql-tables"]},
    {"id": "rel:db-contains-customer", "source": "db:mysql-core",
     "relation_type": "composition", "target": "table:t_customer",
     "connection_condition": "containsTable: 库内表",
     "description": "creditcard 库包含 t_customer 表", "evidence_refs": ["ev-mysql-tables"]},
    {"id": "rel:db-contains-audit", "source": "db:mysql-core",
     "relation_type": "composition", "target": "table:t_audit",
     "connection_condition": "containsTable: 库内表",
     "description": "creditcard 库包含 t_audit 表", "evidence_refs": ["ev-mysql-tables"]},
    {"id": "rel:app-exposes-api", "source": "app:payment-app",
     "relation_type": "composition", "target": "api:auth",
     "connection_condition": "exposes: 应用暴露接口",
     "description": "payment-app 暴露授权接口", "evidence_refs": ["ev-app-topology"]},
    {"id": "rel:app-has-fn", "source": "app:payment-app",
     "relation_type": "composition", "target": "fn:auth",
     "connection_condition": "hasComponent: 应用包含业务功能",
     "description": "payment-app 包含授权功能", "evidence_refs": []},

    # ---- association（访问/引用/证据）----
    {"id": "rel:app-accesses-db", "source": "app:payment-app",
     "relation_type": "association", "target": "db:mysql-core",
     "connection_condition": "accesses: 应用通过数据源访问数据库",
     "description": "payment-app 访问 creditcard 库",
     "evidence_refs": ["ev-app-topology", "ev-db-probe"]},
    {"id": "rel:app-datasource", "source": "app:payment-app",
     "relation_type": "association", "target": "ds:pay",
     "connection_condition": "hasDataSource: 应用引用数据源",
     "description": "payment-app 使用支付数据源", "evidence_refs": ["ev-app-topology"]},
    {"id": "rel:ds-points-db", "source": "ds:pay",
     "relation_type": "association", "target": "db:mysql-core",
     "connection_condition": "dataSourceOf: 数据源指向数据库",
     "description": "支付数据源指向 mysql:3306", "evidence_refs": ["ev-app-topology"]},
    {"id": "rel:incident-rc", "source": "incident:payment-timeout",
     "relation_type": "association", "target": "rc:slow-sql",
     "connection_condition": "hasRootCause: 故障的根因",
     "description": "支付超时故障的根因为 t_txn 慢查询", "evidence_refs": ["ev-db-probe"]},
    {"id": "rel:rc-attr-table", "source": "rc:slow-sql",
     "relation_type": "association", "target": "table:t_txn",
     "connection_condition": "attributedTo: 根因归因到实体",
     "description": "根因归因到 t_txn 表（索引缺失/慢 SQL）",
     "evidence_refs": ["ev-mysql-tables"]},
    {"id": "rel:rc-evidence-p99", "source": "rc:slow-sql",
     "relation_type": "association", "target": "metric:mysql-p99",
     "connection_condition": "evidencedBy: 根因由指标证据支撑",
     "description": "P99 延迟指标佐证慢查询", "evidence_refs": ["ev-db-probe"]},
    {"id": "rel:rc-evidence-conn", "source": "rc:slow-sql",
     "relation_type": "association", "target": "metric:conn-exhaust",
     "connection_condition": "evidencedBy: 根因由指标证据支撑",
     "description": "连接池耗尽指标佐证连接泄漏", "evidence_refs": ["ev-app-topology"]},
    {"id": "rel:alert-triggers-incident", "source": "alert:p99",
     "relation_type": "association", "target": "incident:payment-timeout",
     "connection_condition": "triggers: 告警升级为故障",
     "description": "P99 告警触发支付超时故障", "evidence_refs": []},
    {"id": "rel:app-produces-metric", "source": "app:payment-app",
     "relation_type": "association", "target": "metric:mysql-p99",
     "connection_condition": "produces: 组件产出指标",
     "description": "payment-app 产出 MySQL P99 延迟指标", "evidence_refs": ["ev-db-probe"]},

    # ---- equivalence（同指/等价）----
    {"id": "rel:host-wsl-equiv-phys", "source": "env:host-wsl",
     "relation_type": "equivalence", "target": "env:host-phys",
     "connection_condition": "同机等价：WSL2 虚拟宿主与物理主机同指一台机器",
     "description": "Docker Desktop WSL2 宿主与 Windows 物理主机指向同一台硬件",
     "evidence_refs": ["ev-host-facts"]},

    # ---- derivation（传递/推导）----
    {"id": "rel:app-derives-host", "source": "app:payment-app",
     "relation_type": "derivation", "target": "env:host-wsl",
     "connection_condition": "runsOn o deployedOn: 应用->容器->宿主机 传递路径",
     "description": "由 runsOn+deployedOn 派生 payment-app 最终运行于宿主机",
     "evidence_refs": ["ev-docker-ps"]},
    {"id": "rel:db-derives-tables", "source": "db:mysql-core",
     "relation_type": "derivation", "target": "table:t_txn",
     "connection_condition": "containsTable: 库派生到表（定位慢 SQL）",
     "description": "数据库故障可派生定位到具体表", "evidence_refs": ["ev-mysql-tables"]},

    # ── 补齐首版漏映射的 TTL 对象属性 ────────────────────────────────────
    # 首版转换器只映射了 TTL 对象属性的一个子集，漏掉 managedBy / generatesEvent /
    # hasIndex。后果：只靠这些属性连接的个体变成**游离节点**（关系图度为 0）——
    # 不在引擎拓扑、永不做候选根因，却会出现在 browse_semantics 结果里污染语义检索；
    # 索引个体更是连 Term 都没被建出来，导致"慢查询是否索引缺失"这条推理缺一环。
    # 见 tools/ontology_hygiene.py 与 docs/集群化_故障注入_验证手册.md §5。
    {"id": "rel:container-managed-by-cluster", "source": "env:container",
     "relation_type": "composition", "target": "env:cluster",
     "connection_condition": "managedBy: 容器由运行时引擎管理（TTL :managedBy）",
     "description": "支付容器由 Docker 运行时引擎调度", "evidence_refs": ["ev-docker-ps"]},
    {"id": "rel:app-generates-deploy", "source": "app:payment-app",
     "relation_type": "association", "target": "evt:deploy",
     "connection_condition": "generatesEvent: 发布/重启产生变更事件（TTL :generatesEvent）",
     "description": "payment-app 的每次发布或重启产生一个变更事件",
     "evidence_refs": ["ev-app-topology"]},
    {"id": "rel:txn-has-idx-customer", "source": "table:t_txn",
     "relation_type": "composition", "target": "idx:txn-customer",
     "connection_condition": "hasIndex: 表上的索引（TTL :hasIndex）",
     "description": "t_txn 拥有 customer_id 索引", "evidence_refs": ["ev-mysql-indexes"]},
    {"id": "rel:txn-has-idx-created", "source": "table:t_txn",
     "relation_type": "composition", "target": "idx:txn-created",
     "connection_condition": "hasIndex: 表上的索引（TTL :hasIndex）",
     "description": "t_txn 拥有 created_at 索引", "evidence_refs": ["ev-mysql-indexes"]},
    {"id": "rel:rc-slow-sql-idx", "source": "rc:slow-sql",
     "relation_type": "association", "target": "idx:txn-created",
     "connection_condition": "attributedTo: 慢查询执行计划未命中该索引（需 EXPLAIN 确认）",
     "description": "把『慢查询』与『索引是否生效』接起来",
     "evidence_refs": ["ev-mysql-indexes"]},
]

# ===================================================================
# Constraints（RCA 业务规则）
# ===================================================================
constraints = [
    {"id": "con:pool-exhaust", "target": "metric:conn-exhaust",
     "constraint_type": "business_rule",
     "trigger_keywords": ["连接池", "exhausted", "maxPool", "connection limit"],
     "severity": "block", "scope": "ds:pay",
     "confidence": "high",
     "description": "当活跃连接数逼近 poolLimit(10) 时判定连接池耗尽，RCA 优先定位 t_txn 慢 SQL",
     "evidence_refs": ["ev-app-topology"]},
    {"id": "con:p99-threshold", "target": "metric:mysql-p99",
     "constraint_type": "threshold",
     "trigger_keywords": ["p99", "latency", "延迟", "timeout"],
     "severity": "warn", "scope": "db:mysql-core",
     "confidence": "high",
     "description": "MySQL P99 延迟阈值 100ms，超限 5m 触发 P1 告警",
     "evidence_refs": ["ev-mysql-vars"]},
    {"id": "con:db-maxconn", "target": "db:mysql-core",
     "constraint_type": "capacity",
     "trigger_keywords": ["max_connections", "连接数", "capacity"],
     "severity": "warn", "scope": "db:mysql-core",
     "description": f"MySQL max_connections={top['database']['maxConnections']}，应用池上限 {top['database']['appUserPoolLimit']}，需预留管理连接",
     "evidence_refs": ["ev-mysql-vars"]},
]

# ===================================================================
# 写入 workspace
# ===================================================================
records = {
    "terms": terms,
    "mappings": mappings,
    "relations": relations,
    "constraints": constraints,
    "evidence": evidence,
}

# 1. project context（rolling_trajectory 模式：种子工作负载 + 自演化）
project = {
    "schema_version": 1,
    "mode": "rolling_trajectory",
    "data_source": {
        "type": "docker-compose",
        "endpoint": "mysql:3306 (cc-mysql-core) + http://127.0.0.1:8080 (cc-credit-card-app)",
        "description": "信用卡系统运维模拟环境：payment-app 容器 + MySQL creditcard 库",
    },
    "workload_source": {
        "type": "rca-scenarios",
        "description": "信用卡运维根因分析场景：故障定位、变更关联、跨层传播",
        "seed_questions": [
            "支付交易超时，沿拓扑上溯定位根因",
            "MySQL P99 突增，是慢 SQL 还是连接耗尽？",
            "某容器 OOM 重启，哪些业务受影响？",
            "发布 v1.8.2 后故障率上升，是否变更引入？",
            "宿主机 CPU 争抢，定位受影响的应用与容器",
        ],
    },
    "evaluation": {
        "type": "trajectory",
        "description": "Rolling 模式：累积任务轨迹作为演化证据",
    },
    "boundary": {
        "type": "rolling",
        "construction_scope": "demo/topology-snapshot.json + demo/mysql + demo/app",
        "notes": "RCA 场景不划分 held-out，轨迹持续累积",
    },
}
save_project(project, str(WORKSPACE))

# ── 危险操作守卫 ────────────────────────────────────────────────────────
# 本脚本会把 active.json 指回 ontology_v0。若本体已经演化过（激活的是更高
# 版本），直接运行等于"回退生产本体"。要求显式 --force 才继续。
_FORCE = "--force" in sys.argv
try:
    if (WORKSPACE / "active.json").is_file():
        _cur = SemanticStore.active_version(str(WORKSPACE))
        if _cur and _cur != "ontology_v0" and not _FORCE:
            print(json.dumps({
                "refused": True,
                "reason": ("当前激活版本是 %s，而本脚本会把激活版本回退到 ontology_v0。"
                           "如确认要重建基线，请加 --force。" % _cur),
                "current_active_version": _cur,
                "hint": "本体迭代请用 tools/evolve_ontology_cluster.py，不要重跑本脚本。",
            }, ensure_ascii=False, indent=2))
            sys.exit(2)
except SystemExit:
    raise
except Exception:
    pass

# 2. save_version ontology_v0
#    ⚠️ 覆盖保护：版本目录一旦存在，先**归档**再写。
#    本体版本应当是"不可变的历史"；早期版本只做了 --force 拦截，
#    但 `--force` 之后仍然是**原地覆盖**，会悄无声息地改掉历史版本
#    （本轮开发中就误覆盖过一次 ontology_v0）。这里改为写前归档到
#    `.evoontology/archive/<version>-<UTC 时间戳>/`，出问题可回溯。
_vdir = WORKSPACE / "versions" / "ontology_v0"
if _vdir.is_dir():
    import shutil
    _arch = WORKSPACE / "archive" / ("ontology_v0-%s" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    _arch.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_vdir, _arch)
    print("[archive] 已归档旧版本 -> %s" % _arch)

version_dir = SemanticStore.save_version(str(WORKSPACE), "ontology_v0", records)

# 3. 写 active.json
SemanticStore.set_active(str(WORKSPACE), "ontology_v0")

# 4. 初始化 evolution 触发器
from evoontology.trigger.trigger import EvolutionTrigger
state = EvolutionTrigger(str(WORKSPACE)).initialize()

print(json.dumps({
    "workspace": str(WORKSPACE),
    "version_dir": version_dir,
    "counts": {k: len(v) for k, v in records.items()},
    "active_version": "ontology_v0",
    "evolution_state": state,
}, ensure_ascii=False, indent=2))
