# -*- coding: utf-8 -*-
"""
集群化本体补丁（ontology patch）。

背景：环境从"单容器 + 单库"扩展为"3 副本应用 + 网关 + 1 主 2 从 MySQL 集群"
之后，**本体没有跟着长**——原有的 18 个 Term 里没有任何集群概念：
没有"副本/集群/网关/只读副本/复制延迟/CPU 节流配额/OOMKill"。

这个模块把新知识显式写成 EvoOntology 的 5 族记录（增量），
由 `tools/evolve_ontology_cluster.py` 通过自演化协议
（start_run → begin_round → save_version(candidate) → 成对评估 → accept）
发布为新版本，而不是直接改激活版本。

设计约束（validate() 会强制校验交叉引用）：
  · 每条 relation 的 source/target 必须是已存在的 Term
  · 每条 mapping 的 term_id 必须是已存在的 Term
  · 每条 constraint 的 target 必须是已存在的 Term 或 Mapping
  · 所有 evidence_refs 必须指向已存在的 Evidence

因此这里把"新增 Evidence → 新增 Term → 新增 Mapping/Relation/Constraint"
按依赖顺序组织，并在 build_patch() 末尾做一次自检（assert）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Set

NOW = "2026-10-02T00:00:00+00:00"


def _term(tid: str, name: str, ttype: str, definition: str, scope: str,
          aliases: List[str], evidence: List[str]) -> Dict[str, Any]:
    return {"id": tid, "name": name, "type": ttype, "definition": definition,
            "scope": scope, "aliases": aliases, "evidence_refs": evidence}


def _rel(rid: str, src: str, rtype: str, tgt: str, cond: str, desc: str,
         evidence: List[str]) -> Dict[str, Any]:
    return {"id": rid, "source": src, "relation_type": rtype, "target": tgt,
            "connection_condition": cond, "description": desc, "evidence_refs": evidence}


def _map(mid: str, term_id: str, source: str, table: str, column: str,
         sem_filter: str, grain: str, validation: str, evidence: List[str],
         confidence: str = "high", agg: str = "") -> Dict[str, Any]:
    rec = {"id": mid, "term_id": term_id, "database_source": source, "table": table,
           "column": column, "semantic_filter": sem_filter, "grain": grain,
           "validation": validation, "evidence_refs": evidence, "confidence": confidence}
    if agg:
        rec["aggregation_semantics"] = agg
    return rec


def _con(cid: str, target: str, ctype: str, keywords: List[str], severity: str,
         scope: str, desc: str, evidence: List[str]) -> Dict[str, Any]:
    return {"id": cid, "target": target, "constraint_type": ctype,
            "trigger_keywords": keywords, "severity": severity, "scope": scope,
            "confidence": "high", "description": desc, "evidence_refs": evidence}


# 关联到故障场景的稳定映射：场景 id → 期望根因 Term
# 供 RCA 验证脚本与演化评估闸门共用（同一份 ground truth）。
SCENARIO_EXPECTED_TERMS: Dict[str, List[str]] = {
    "app_kill_replica":       ["rc:replica-loss"],
    "app_stop_replica":       ["rc:replica-loss"],
    "app_pause_replica":      ["rc:replica-hang", "rc:replica-loss"],
    "app_oom_kill":           ["rc:oom-kill", "rc:replica-loss"],
    "app_cpu_stress":         ["rc:cpu-throttle", "metric:cpu-throttle"],
    "app_memory_stress":      ["metric:mem-pressure", "rc:oom-kill"],
    "app_network_delay":      ["rc:net-fault", "metric:net-delay"],
    "app_network_loss":       ["rc:net-fault", "metric:net-loss"],
    "app_gateway_stop":       ["rc:gateway-down"],
    "db_row_lock_hold":       ["rc:row-lock"],
    "db_slow_query_flood":    ["rc:slow-sql"],
    "db_conn_saturation":     ["rc:conn-exhaust"],
    "db_cpu_stress":          ["rc:db-cpu-starve"],
    "db_disk_temp_tables":    ["rc:tmp-disk"],
    "db_primary_readonly":    ["rc:primary-readonly"],
    "db_replica_lag":         ["rc:replica-lag"],
    "db_replica_io_stop":     ["rc:db-replica-loss"],
    "db_replica_kill":        ["rc:db-replica-loss", "rc:replica-loss"],
    "res_cluster_cpu":        ["rc:cluster-capacity", "rc:cpu-throttle"],
    "res_cluster_memory":     ["rc:cluster-capacity", "metric:mem-pressure"],
    "res_db_memory":          ["rc:tmp-disk"],
}


def build_patch(base_term_ids: Optional[Set[str]] = None,
                base_evidence_ids: Optional[Set[str]] = None,
                base_mapping_ids: Optional[Set[str]] = None
                ) -> Dict[str, List[Dict[str, Any]]]:
    """
    构建集群化增量补丁（5 族）。

    Args:
        base_*: 父版本已有的 id 集合。补丁里的 relation 会引用父版本的
                Term（如 db:mysql-core / table:t_txn），自检时必须把父版本
                的 id 一起纳入，否则会误报"未知引用"。
    返回 {terms, mappings, relations, constraints, evidence}。
    """

    # ── 1. Evidence（可复现观测）──────────────────────────────────────────
    evidence: List[Dict[str, Any]] = [
        {
            "id": "ev-cluster-topology",
            "source": "docker compose ps / docker ps --filter label=com.docker.compose.service=payment-app",
            "query": "docker ps --filter label=com.docker.compose.service=payment-app "
                     "--format '{{.Names}}\\t{{.Status}}'",
            "result": "rca-agent-payment-app-1/2/3 Up (healthy)；cc-app-gateway Up (healthy) "
                      "（应用层 3 副本 + 1 网关）",
            "validation_method": "read-only docker CLI",
            "timestamp": NOW,
        },
        {
            "id": "ev-cluster-replication",
            "source": "mysql:SHOW REPLICA STATUS",
            "query": "SHOW REPLICA STATUS\\G  (cc-mysql-replica-1 / cc-mysql-replica-2)",
            "result": "Replica_IO_Running=Yes, Replica_SQL_Running=Yes, "
                      "Seconds_Behind_Source=0, Source_Host=mysql, Auto_Position=1；"
                      "GTID 复制，server_id 1(主)/11/12(从)",
            "validation_method": "MySQL 复制状态查询",
            "timestamp": NOW,
        },
        {
            "id": "ev-cluster-prometheus-targets",
            "source": "http://localhost:9090/api/v1/targets",
            "query": "GET /api/v1/targets",
            "result": "job=payment-app 经 dns_sd 发现 3 个副本目标；"
                      "job=mysql-primary/replica-1/replica-2 各 1 个 exporter；"
                      "job=blackbox-gateway / blackbox-app-replica 探活正常",
            "validation_method": "Prometheus 目标发现结果",
            "timestamp": NOW,
        },
        {
            "id": "ev-cluster-quota",
            "source": "docker inspect --format '{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}'",
            "query": "docker inspect rca-agent-payment-app-N",
            "result": "应用副本 mem_limit=268435456(256MB), cpus=0.50；"
                      "MySQL 主库 mem_limit=1g；副本 mem_limit=768m",
            "validation_method": "容器 cgroup 配置读取",
            "timestamp": NOW,
        },
        {
            "id": "ev-cluster-blackbox",
            "source": "http://localhost:9090/api/v1/query?query=probe_success",
            "query": "probe_success{job=~\"blackbox-.*\"}",
            "result": "blackbox-gateway=1；blackbox-app-replica 3 个目标均为 1",
            "validation_method": "blackbox_exporter 外部探活",
            "timestamp": NOW,
        },
        {
            "id": "ev-cluster-fault-catalog",
            "source": "tools/fault_injector.py list --json",
            "query": "python tools/fault_injector.py list --json",
            "result": "21 个故障场景：应用层 9（副本 kill/pause/stop、CPU/内存压力、OOMKill、"
                      "网络延迟/丢包、网关下线）、数据库层 9（行锁、慢查询、连接耗尽、CPU、"
                      "磁盘临时表、主库只读、复制延迟/中断/副本丢失）、资源耗尽 3（集群 CPU/内存、"
                      "DB 排序内存）；每场景声明 PromQL 信号与期望根因",
            "validation_method": "故障目录清单 + 逐场景信号验证",
            "timestamp": NOW,
        },
        {
            "id": "ev-cluster-stress",
            "source": "tools/stress_harness.py",
            "query": "python tools/stress_harness.py --mode http|mysql|mixed --json-out ...",
            "result": "HTTP 模式实测 ~250 rps / 16 并发；mysql 模式可用 "
                      "--mysql-hold-connections 直接打满 max_connections；"
                      "输出分秒 time_series 供与故障窗口对齐",
            "validation_method": "压测报告（成功率/百分位/时序）",
            "timestamp": NOW,
        },
    ]

    # ── 2. Terms ─────────────────────────────────────────────────────────
    terms: List[Dict[str, Any]] = [
        # ── 应用层集群 ──────────────────────────────────────────────
        _term("app:payment-app-cluster", "支付应用集群 payment-app-cluster", "entity",
              "payment-app 的集群视图：3 个等价副本 + 1 个网关负载均衡；"
              "集群健康度由「在册健康副本数」而非单副本存活决定",
              "应用层", ["PaymentAppCluster", "pay-cluster"],
              ["ev-cluster-topology", "ev-cluster-prometheus-targets"]),
        _term("app:instance-1", "payment-app 副本 #1", "entity",
              "集群中的第一个应用副本容器（compose service 的 replica）",
              "应用层", ["PaymentAppInstance1"], ["ev-cluster-topology"]),
        _term("app:instance-2", "payment-app 副本 #2", "entity",
              "集群中的第二个应用副本容器", "应用层", ["PaymentAppInstance2"],
              ["ev-cluster-topology"]),
        _term("app:instance-3", "payment-app 副本 #3", "entity",
              "集群中的第三个应用副本容器", "应用层", ["PaymentAppInstance3"],
              ["ev-cluster-topology"]),
        _term("env:gateway", "应用层网关 cc-app-gateway", "entity",
              "Nginx 负载均衡网关，对外唯一入口 :8080，least_conn 策略 + Docker DNS 发现副本；"
              "副本全健康但网关挂掉时对外仍完全不可用",
              "运行环境层", ["AppGateway", "LoadBalancer"],
              ["ev-cluster-topology", "ev-cluster-blackbox"]),
        _term("metric:replica-healthy-count", "在册健康副本数", "metric",
              "count(up{job=\"payment-app\"} == 1)：集群可用副本数，低于期望副本数即容量下降",
              "RCA 支撑", ["HealthyReplicaCount"], ["ev-cluster-prometheus-targets"]),
        _term("metric:gateway-probe", "网关黑盒探活成功率", "metric",
              "probe_success{job=\"blackbox-gateway\"}：从外部视角判断入口是否可用——"
              "副本自报的 /metrics 无法证明这一点",
              "RCA 支撑", ["GatewayProbeSuccess"], ["ev-cluster-blackbox"]),
        _term("metric:http-error-rate", "HTTP 5xx 错误率", "metric",
              "sum(rate(cc_http_requests_total{status=~\"5..\"}[5m])) / sum(rate(...))，"
              "写路径被拒绝时的最直接表征",
              "RCA 支撑", ["HttpErrorRate"], ["ev-cluster-prometheus-targets"]),
        _term("metric:cpu-throttle", "容器 CPU 配额节流", "metric",
              "cc_container_cpu_nr_throttled_total / cc_container_cpu_throttled_seconds_total："
              "cgroup cpu quota 把进程强制排队，CPU/内存指标却可能都不高",
              "RCA 支撑", ["CpuThrottle", "CpuQuotaThrottle"], ["ev-cluster-quota"]),
        _term("metric:mem-pressure", "容器内存压力", "metric",
              "cc_container_memory_working_set_bytes / cc_container_memory_limit_bytes："
              "逼近 1.0 时容器随时被内核 OOM Killer 杀死",
              "RCA 支撑", ["MemPressure", "ContainerMemRatio"], ["ev-cluster-quota"]),
        _term("metric:net-delay", "副本网络延迟", "metric",
              "probe_duration_seconds / netem 注入后的端到端延迟：容器资源指标正常但链路变慢",
              "RCA 支撑", ["NetDelay"], ["ev-cluster-blackbox"]),
        _term("metric:net-loss", "副本网络丢包率", "metric",
              "netem loss 造成的丢包：TCP 重传→长尾延迟与探活失败",
              "RCA 支撑", ["NetLoss"], ["ev-cluster-blackbox"]),
        _term("rc:replica-loss", "根因：应用副本丢失", "entity",
              "某个应用副本容器被终止/崩溃（docker kill、OOMKill、退出），"
              "集群在册副本数下降，剩余副本承接全部流量",
              "RCA 支撑", ["RootReplicaLoss"], ["ev-cluster-topology", "ev-cluster-fault-catalog"]),
        _term("rc:replica-hang", "根因：应用副本假死", "entity",
              "副本进程被挂起（pause/长 GC/死锁）：容器状态仍是 Up、端口仍在监听，"
              "但请求永不返回——比崩溃更难识别",
              "RCA 支撑", ["RootReplicaHang"], ["ev-cluster-fault-catalog"]),
        _term("rc:oom-kill", "根因：容器内存超限被 OOM Kill", "entity",
              "容器 cgroup 内存用量触顶，内核 OOM Killer 终止容器内进程；"
              "docker inspect 的 OOMKilled=true / RestartCount 递增是直接证据",
              "RCA 支撑", ["RootOomKill"], ["ev-cluster-quota", "ev-cluster-fault-catalog"]),
        _term("rc:cpu-throttle", "根因：容器 CPU 配额节流", "entity",
              "副本 CPU 需求超过 cpus 配额，cgroup 每周期强制限流；"
              "表现为单副本延迟明显高于其他副本（副本间不均衡）",
              "RCA 支撑", ["RootCpuThrottle"], ["ev-cluster-quota"]),
        _term("rc:gateway-down", "根因：应用网关不可用", "entity",
              "负载均衡入口不可用：全部副本健康、副本指标正常，仅外部可达性为 0",
              "RCA 支撑", ["RootGatewayDown"], ["ev-cluster-blackbox"]),
        _term("rc:net-fault", "根因：网络延迟/丢包", "entity",
              "副本所在链路质量故障（延迟或丢包），CPU/内存指标全部正常",
              "RCA 支撑", ["RootNetFault"], ["ev-cluster-blackbox"]),
        _term("rc:cluster-capacity", "根因：集群整体容量不足", "entity",
              "全部/多数副本同时资源受限：不是单副本故障，而是副本数或配额相对于流量不足",
              "RCA 支撑", ["RootClusterCapacity"], ["ev-cluster-quota", "ev-cluster-fault-catalog"]),

        # ── 数据库层集群 ────────────────────────────────────────────
        _term("db:mysql-cluster", "creditcard MySQL 集群", "entity",
              "1 主 2 只读副本的 GTID 异步复制集群；写只在主库、读可走副本；"
              "集群健康需同时看主库与副本（复制延迟/中断/副本丢失）",
              "数据库层", ["MysqlCluster", "cc-mysql-cluster"],
              ["ev-cluster-replication", "ev-cluster-prometheus-targets"]),
        _term("db:primary", "MySQL 主库节点", "entity",
              "cc-mysql-core：server_id=1，read_only=0，承担全部写入并产生 binlog",
              "数据库层", ["MysqlPrimary", "cc-mysql-core"],
              ["ev-cluster-replication"]),
        _term("db:mysql-replica", "MySQL 只读副本节点", "entity",
              "cc-mysql-replica-1/2：server_id=11/12，read_only=ON，"
              "通过 GTID 异步复制回放主库事务；读路径依赖它",
              "数据库层", ["MysqlReplica", "cc-mysql-replica"],
              ["ev-cluster-replication"]),
        _term("metric:replica-lag", "复制延迟 Seconds_Behind_Master", "metric",
              "mysql_slave_status_seconds_behind_master：副本回放落后主库的秒数；"
              "阈值 5s，超过即读路径返回陈旧数据",
              "RCA 支撑", ["ReplicaLag", "SecondsBehindMaster"], ["ev-cluster-replication"]),
        _term("metric:replica-io-running", "副本复制线程状态", "metric",
              "mysql_slave_status_slave_io_running / slave_sql_running："
              "任一为 0 即复制中断，副本数据永久停止更新",
              "RCA 支撑", ["ReplicaIoRunning", "ReplicationThreadState"],
              ["ev-cluster-replication"]),
        _term("metric:db-cpu", "数据库并发执行线程", "metric",
              "mysql_global_status_threads_running：数据库侧 CPU 资源不足时持续偏高",
              "RCA 支撑", ["DbThreadsRunning"], ["ev-cluster-prometheus-targets"]),
        _term("metric:db-tmp-disk", "磁盘临时表速率", "metric",
              "rate(mysql_global_status_created_tmp_disk_tables[5m])："
              "排序/分组内存不足被迫落盘，磁盘 IO 成为瓶颈",
              "RCA 支撑", ["TmpDiskTables"], ["ev-cluster-prometheus-targets"]),
        _term("rc:row-lock", "根因：InnoDB 行锁等待", "entity",
              "长事务持有 t_txn 的 next-key 锁（全表 FOR UPDATE），"
              "阻塞其他写入并逐级占满应用连接池",
              "RCA 支撑", ["RootRowLock"], ["ev-cluster-fault-catalog"]),
        _term("rc:conn-exhaust", "根因：数据库连接资源耗尽", "entity",
              "threads_connected 逼近 max_connections=200，新连接被拒绝"
              "（Too many connections / Aborted_connects 上升）",
              "RCA 支撑", ["RootConnExhaust"], ["ev-cluster-fault-catalog"]),
        _term("rc:db-cpu-starve", "根因：数据库 CPU 资源不足", "entity",
              "交叉连接/聚合类重查询把 DB CPU 打满，所有查询排队",
              "RCA 支撑", ["RootDbCpuStarve"], ["ev-cluster-fault-catalog"]),
        _term("rc:tmp-disk", "根因：排序内存不足导致磁盘临时表", "entity",
              "tmp_table_size / sort_buffer 偏小，GROUP BY/ORDER BY 溢出到磁盘临时表",
              "RCA 支撑", ["RootTmpDisk"], ["ev-cluster-fault-catalog"]),
        _term("rc:primary-readonly", "根因：主库被置为只读", "entity",
              "配置漂移：主库 read_only=ON 后写请求全部失败（1290），"
              "读请求走副本依旧成功——健康检查全绿但交易全失败",
              "RCA 支撑", ["RootPrimaryReadonly"], ["ev-cluster-fault-catalog"]),
        _term("rc:replica-lag", "根因：主从复制延迟", "entity",
              "副本回放落后（SOURCE_DELAY 或回放能力不足），"
              "IO/SQL 线程都 Running，但读到的数据是旧的",
              "RCA 支撑", ["RootReplicaLag"], ["ev-cluster-replication"]),
        _term("rc:db-replica-loss", "根因：数据库只读副本丢失", "entity",
              "副本节点不可达或复制线程停止：读容量下降，主库失去冗余",
              "RCA 支撑", ["RootDbReplicaLoss"], ["ev-cluster-replication"]),
    ]

    # ── 3. Mappings（概念 → 物理数据源接地）───────────────────────────────
    mappings: List[Dict[str, Any]] = [
        _map("map:cluster-replica-count", "metric:replica-healthy-count",
             "http://localhost:9090", "prometheus", "up",
             "count(up{job=\"payment-app\"} == 1)", "cluster",
             "Prometheus 目标发现 + up 指标", ["ev-cluster-prometheus-targets"], agg="count"),
        _map("map:cluster-app-instance", "app:payment-app-cluster",
             "docker:compose", "docker_ps", "Names",
             "label=com.docker.compose.service=payment-app", "replica",
             "docker ps 按 compose service 标签枚举副本", ["ev-cluster-topology"]),
        _map("map:gateway-probe", "metric:gateway-probe",
             "http://localhost:9090", "probe_success", "value",
             "probe_success{job=\"blackbox-gateway\"}", "gateway",
             "blackbox_exporter 外部 HTTP 探活", ["ev-cluster-blackbox"]),
        _map("map:cpu-throttle", "metric:cpu-throttle",
             "http://127.0.0.1:8080/metrics", "cc_container_cpu_nr_throttled_total", "value",
             "rate(cc_container_cpu_nr_throttled_total[5m]) > 0", "container",
             "app /metrics 读取 /sys/fs/cgroup/cpu.stat", ["ev-cluster-quota"]),
        _map("map:mem-pressure", "metric:mem-pressure",
             "http://127.0.0.1:8080/metrics", "cc_container_memory_working_set_bytes", "ratio",
             "working_set / memory_limit", "container",
             "app /metrics 读取 /sys/fs/cgroup/memory.current 与 memory.max",
             ["ev-cluster-quota"]),
        _map("map:replica-lag", "metric:replica-lag",
             "mysql:performance_schema", "replication_applier_status_by_worker", "SECONDS_BEHIND_MASTER",
             "SHOW REPLICA STATUS -> Seconds_Behind_Source", "replica",
             "mysqld-exporter mysql_slave_status_seconds_behind_master",
             ["ev-cluster-replication"]),
        _map("map:replica-io-running", "metric:replica-io-running",
             "mysql:performance_schema", "replication_connection_status", "SERVICE_STATE",
             "SHOW REPLICA STATUS -> Replica_IO_Running", "replica",
             "mysqld-exporter mysql_slave_status_slave_io_running", ["ev-cluster-replication"]),
        _map("map:db-tmp-disk", "metric:db-tmp-disk",
             "mysql:performance_schema", "global_status", "Created_tmp_disk_tables",
             "rate(mysql_global_status_created_tmp_disk_tables[5m])", "server",
             "mysqld-exporter SHOW GLOBAL STATUS", ["ev-cluster-prometheus-targets"]),
        _map("map:conn-processlist", "metric:conn-exhaust",
             "mysql:information_schema", "processlist", "COUNT(*)",
             "threads_connected / max_connections", "server",
             "information_schema.processlist 与 @@max_connections",
             ["ev-cluster-prometheus-targets"]),
    ]

    # ── 4. Relations（5 种受控类型）───────────────────────────────────────
    relations: List[Dict[str, Any]] = [
        # composition：集群 → 成员
        _rel("rel:app-member-of-cluster", "app:payment-app", "composition",
             "app:payment-app-cluster", "memberOf: 应用实例属于集群",
             "payment-app 是集群的逻辑身份，物理上由 3 个副本承载",
             ["ev-cluster-topology"]),
        _rel("rel:cluster-has-instance-1", "app:payment-app-cluster", "composition",
             "app:instance-1", "hasInstance: 集群包含副本",
             "集群包含副本 #1", ["ev-cluster-topology"]),
        _rel("rel:cluster-has-instance-2", "app:payment-app-cluster", "composition",
             "app:instance-2", "hasInstance: 集群包含副本",
             "集群包含副本 #2", ["ev-cluster-topology"]),
        _rel("rel:cluster-has-instance-3", "app:payment-app-cluster", "composition",
             "app:instance-3", "hasInstance: 集群包含副本",
             "集群包含副本 #3", ["ev-cluster-topology"]),
        _rel("rel:instance-1-runs-on-container", "app:instance-1", "composition",
             "env:container", "runsOn: 副本运行在容器内",
             "副本 #1 运行于容器", ["ev-cluster-topology"]),
        _rel("rel:db-member-of-cluster", "db:mysql-core", "composition",
             "db:mysql-cluster", "memberOf: 库实例属于数据库集群",
             "creditcard 库由主从集群承载", ["ev-cluster-replication"]),
        _rel("rel:cluster-has-primary", "db:mysql-cluster", "composition",
             "db:primary", "hasNode: 集群包含主库节点",
             "集群包含主库 cc-mysql-core", ["ev-cluster-replication"]),
        _rel("rel:cluster-has-replica", "db:mysql-cluster", "composition",
             "db:mysql-replica", "hasNode: 集群包含只读副本节点",
             "集群包含 2 个只读副本", ["ev-cluster-replication"]),
        # association：网关/读写路径/复制/证据
        _rel("rel:cluster-lb-by-gateway", "app:payment-app-cluster", "association",
             "env:gateway", "loadBalancedBy: 集群由网关负载均衡",
             "集群对外入口是 Nginx 网关", ["ev-cluster-topology"]),
        _rel("rel:gateway-routes-to-cluster", "env:gateway", "association",
             "app:payment-app-cluster", "routesTo: 网关转发到集群副本",
             "网关按 least_conn 转发到 3 个副本", ["ev-cluster-topology"]),
        _rel("rel:primary-replicates-to-replica", "db:primary", "association",
             "db:mysql-replica", "replicatesTo: 主库异步复制到副本",
             "主库通过 GTID 异步复制把事务同步到只读副本",
             ["ev-cluster-replication"]),
        _rel("rel:app-reads-replica", "app:payment-app", "association",
             "db:mysql-replica", "readsFrom: 读路径轮询只读副本",
             "应用读路径（/txn/recent）走只读副本，写路径走主库——读写分离",
             ["ev-cluster-topology", "ev-cluster-replication"]),
        _rel("rel:gateway-evidenced-by-probe", "env:gateway", "association",
             "metric:gateway-probe", "evidencedBy: 网关可用性由黑盒探活支撑",
             "网关是否可用只能由外部探活证明", ["ev-cluster-blackbox"]),
        _rel("rel:cluster-produces-replica-count", "app:payment-app-cluster", "association",
             "metric:replica-healthy-count", "produces: 集群产出健康副本数",
             "集群健康度由在册健康副本数表征", ["ev-cluster-prometheus-targets"]),
        _rel("rel:instance-produces-throttle", "app:instance-1", "association",
             "metric:cpu-throttle", "produces: 副本产出 CPU 节流指标",
             "副本容器产出 CPU 节流计数", ["ev-cluster-quota"]),
        _rel("rel:instance-produces-mem", "app:instance-1", "association",
             "metric:mem-pressure", "produces: 副本产出内存压力指标",
             "副本容器产出内存使用率", ["ev-cluster-quota"]),
        _rel("rel:replica-produces-lag", "db:mysql-replica", "association",
             "metric:replica-lag", "produces: 副本产出复制延迟指标",
             "副本产出 Seconds_Behind_Master", ["ev-cluster-replication"]),
        _rel("rel:replica-produces-io", "db:mysql-replica", "association",
             "metric:replica-io-running", "produces: 副本产出复制线程状态",
             "副本产出 IO/SQL 线程状态", ["ev-cluster-replication"]),
        _rel("rel:primary-produces-tmpdisk", "db:primary", "association",
             "metric:db-tmp-disk", "produces: 主库产出磁盘临时表指标",
             "主库产出 Created_tmp_disk_tables", ["ev-cluster-prometheus-targets"]),
        _rel("rel:primary-produces-dbcpu", "db:primary", "association",
             "metric:db-cpu", "produces: 主库产出并发线程指标",
             "主库产出 Threads_running", ["ev-cluster-prometheus-targets"]),
        # association：根因 → 归因实体 / 证据
        _rel("rel:rc-replica-loss-attr-cluster", "rc:replica-loss", "association",
             "app:payment-app-cluster", "attributedTo: 副本丢失归因到集群",
             "副本丢失是集群容量问题，不是应用代码问题",
             ["ev-cluster-fault-catalog"]),
        _rel("rel:rc-replica-loss-evidenced", "rc:replica-loss", "association",
             "metric:replica-healthy-count", "evidencedBy: 健康副本数佐证",
             "健康副本数下降是副本丢失的直接证据", ["ev-cluster-prometheus-targets"]),
        _rel("rel:rc-replica-hang-attr-cluster", "rc:replica-hang", "association",
             "app:payment-app-cluster", "attributedTo: 假死副本归因到集群",
             "单个副本假死同样表现为集群容量下降",
             ["ev-cluster-fault-catalog"]),
        _rel("rel:rc-replica-hang-evidenced", "rc:replica-hang", "association",
             "metric:replica-healthy-count", "evidencedBy: 健康副本数佐证假死",
             "假死副本的 up/probe 掉 0 → 健康副本数下降",
             ["ev-cluster-prometheus-targets"]),
        _rel("rel:rc-cpu-throttle-attr-cluster", "rc:cpu-throttle", "association",
             "app:payment-app-cluster", "attributedTo: CPU 节流归因到副本集群",
             "节流发生在具体副本容器上，影响表现为集群内延迟不均衡",
             ["ev-cluster-quota"]),
        _rel("rel:rc-net-fault-attr-cluster", "rc:net-fault", "association",
             "app:payment-app-cluster", "attributedTo: 网络故障归因到副本集群",
             "链路故障作用于副本与网关/数据库之间的网络",
             ["ev-cluster-blackbox"]),
        _rel("rel:rc-net-fault-evidenced-loss", "rc:net-fault", "association",
             "metric:net-loss", "evidencedBy: 丢包指标佐证网络故障",
             "丢包率上升佐证链路质量故障", ["ev-cluster-blackbox"]),
        _rel("rel:rc-cluster-capacity-attr", "rc:cluster-capacity", "association",
             "app:payment-app-cluster", "attributedTo: 集群容量不足归因到集群整体",
             "不是单副本故障，而是副本数/配额相对流量不足",
             ["ev-cluster-quota"]),
        _rel("rel:rc-cluster-capacity-evidenced-cpu", "rc:cluster-capacity", "association",
             "metric:cpu-throttle", "evidencedBy: 多副本同时 CPU 节流",
             "多数副本同时节流是集群容量不足的证据", ["ev-cluster-quota"]),
        _rel("rel:rc-cluster-capacity-evidenced-mem", "rc:cluster-capacity", "association",
             "metric:mem-pressure", "evidencedBy: 多副本同时内存压力",
             "多数副本同时逼近内存上限是集群容量不足的证据", ["ev-cluster-quota"]),
        _rel("rel:rc-db-replica-loss-attr", "rc:db-replica-loss", "association",
             "db:mysql-replica", "attributedTo: 副本丢失归因到只读副本节点",
             "副本节点不可达或复制线程停止", ["ev-cluster-replication"]),
        _rel("rel:rc-oom-kill-attr-instance", "rc:oom-kill", "association",
             "app:payment-app-cluster", "attributedTo: OOMKill 归因到集群副本",
             "OOMKill 发生在具体副本容器上", ["ev-cluster-fault-catalog"]),
        _rel("rel:rc-oom-kill-evidenced", "rc:oom-kill", "association",
             "metric:mem-pressure", "evidencedBy: 内存压力佐证 OOMKill",
             "内存使用率触顶佐证 OOMKill", ["ev-cluster-quota"]),
        _rel("rel:rc-cpu-throttle-evidenced", "rc:cpu-throttle", "association",
             "metric:cpu-throttle", "evidencedBy: 节流计数佐证 CPU 节流",
             "cgroup 节流计数增长佐证 CPU 配额不足", ["ev-cluster-quota"]),
        _rel("rel:rc-gateway-down-attr", "rc:gateway-down", "association",
             "env:gateway", "attributedTo: 归因到网关组件",
             "入口不可用归因到网关而非业务副本", ["ev-cluster-blackbox"]),
        _rel("rel:rc-gateway-down-evidenced", "rc:gateway-down", "association",
             "metric:gateway-probe", "evidencedBy: 黑盒探活佐证",
             "probe_success=0 是网关不可用的直接证据", ["ev-cluster-blackbox"]),
        _rel("rel:rc-net-fault-evidenced", "rc:net-fault", "association",
             "metric:net-delay", "evidencedBy: 网络延迟佐证",
             "探活耗时上升佐证链路异常", ["ev-cluster-blackbox"]),
        _rel("rel:rc-row-lock-attr-table", "rc:row-lock", "association",
             "table:t_txn", "attributedTo: 行锁等待归因到 t_txn",
             "锁等待发生在 t_txn 上", ["ev-cluster-fault-catalog"]),
        _rel("rel:rc-row-lock-evidenced", "rc:row-lock", "association",
             "metric:conn-exhaust", "evidencedBy: 连接池指标佐证",
             "锁等待→连接占满，连接池指标是级联证据",
             ["ev-cluster-prometheus-targets"]),
        _rel("rel:rc-conn-exhaust-attr", "rc:conn-exhaust", "association",
             "db:mysql-cluster", "attributedTo: 连接耗尽归因到数据库集群",
             "连接资源属于数据库集群容量", ["ev-cluster-fault-catalog"]),
        _rel("rel:rc-conn-exhaust-evidenced", "rc:conn-exhaust", "association",
             "metric:conn-exhaust", "evidencedBy: 连接数指标佐证",
             "threads_connected/max_connections 佐证连接耗尽",
             ["ev-cluster-prometheus-targets"]),
        _rel("rel:rc-replica-lag-attr", "rc:replica-lag", "association",
             "db:mysql-replica", "attributedTo: 复制延迟归因到副本节点",
             "延迟发生在副本回放链路上", ["ev-cluster-replication"]),
        _rel("rel:rc-replica-lag-evidenced", "rc:replica-lag", "association",
             "metric:replica-lag", "evidencedBy: 复制延迟指标佐证",
             "Seconds_Behind_Master 佐证", ["ev-cluster-replication"]),
        _rel("rel:rc-db-replica-loss-evidenced", "rc:db-replica-loss", "association",
             "metric:replica-io-running", "evidencedBy: 复制线程状态佐证",
             "IO/SQL 线程停止或节点缺失佐证副本丢失", ["ev-cluster-replication"]),
        _rel("rel:rc-primary-readonly-evidenced", "rc:primary-readonly", "association",
             "metric:http-error-rate", "evidencedBy: 写路径 5xx 佐证",
             "主库只读时写请求全部 5xx，而读路径正常", ["ev-cluster-fault-catalog"]),
        _rel("rel:rc-primary-readonly-attr", "rc:primary-readonly", "association",
             "db:primary", "attributedTo: 主库只读归因到主库节点",
             "写失败发生在主库角色配置上；传播路径 主库→集群→应用",
             ["ev-cluster-fault-catalog"]),
        _rel("rel:cluster-produces-error-rate", "app:payment-app-cluster", "association",
             "metric:http-error-rate", "produces: 集群产出 5xx 错误率",
             "写路径被拒绝时由集群侧 5xx 错误率表征",
             ["ev-cluster-prometheus-targets"]),
        _rel("rel:rc-tmp-disk-attributed", "rc:tmp-disk", "association",
             "db:primary", "attributedTo: 磁盘临时表归因到主库实例",
             "排序落盘发生在主库", ["ev-cluster-fault-catalog"]),
        _rel("rel:rc-tmp-disk-evidenced", "rc:tmp-disk", "association",
             "metric:db-tmp-disk", "evidencedBy: 临时表指标佐证",
             "Created_tmp_disk_tables 速率佐证", ["ev-cluster-prometheus-targets"]),
        _rel("rel:rc-db-cpu-starve-evidenced", "rc:db-cpu-starve", "association",
             "metric:db-cpu", "evidencedBy: 并发线程数佐证",
             "Threads_running 持续偏高佐证 DB CPU 不足",
             ["ev-cluster-prometheus-targets"]),
        # derivation：跨层传播路径
        _rel("rel:derive-client-to-cluster", "env:gateway", "derivation",
             "app:payment-app-cluster",
             "routesTo o hasInstance: 入口 → 集群 → 副本 → 容器 → 宿主机 的跨层传播路径",
             "由网关→集群→副本→容器→宿主机派生完整的故障传播链",
             ["ev-cluster-topology"]),
        _rel("rel:derive-cluster-instance", "app:payment-app-cluster", "derivation",
             "app:instance-1",
             "hasInstance: 集群故障可派生定位到具体副本",
             "集群级告警（副本数不足）可派生到具体副本容器",
             ["ev-cluster-topology"]),
        _rel("rel:derive-primary-to-replica", "db:primary", "derivation",
             "db:mysql-cluster",
             "replicatesTo: 主库写放大可派生到整个集群",
             "主库写入压力可派生影响副本回放（复制延迟）",
             ["ev-cluster-replication"]),
        # equivalence
        _rel("rel:instance-equiv-container", "app:instance-1", "equivalence",
             "env:container", "等价：集群副本在环境中即承载应用的容器",
             "副本 #1 与承载 payment-app 的容器同指",
             ["ev-cluster-topology"]),
    ]

    # ── 5. Constraints（RCA 业务规则，与告警规则的 onto_constraint 对齐）──
    constraints: List[Dict[str, Any]] = [
        _con("con:app-replica-quorum", "metric:replica-healthy-count", "capacity",
             ["副本", "replica", "replicas", "集群", "cluster", "quorum", "在册副本"],
             "block", "app:payment-app-cluster",
             "期望副本数 3；在册健康副本数 < 3 即容量不足，= 0 即整层不可用",
             ["ev-cluster-topology"]),
        _con("con:gateway-availability", "metric:gateway-probe", "business_rule",
             ["网关", "gateway", "负载均衡", "loadbalancer", "入口", "502", "503"],
             "block", "env:gateway",
             "网关黑盒探活 probe_success=0 持续 2m 判定入口不可用（副本健康不能替代此判定）",
             ["ev-cluster-blackbox"]),
        _con("con:app-mem-limit", "metric:mem-pressure", "capacity",
             ["内存", "memory", "oom", "OOMKill", "mem_limit", "内存不足"],
             "block", "app:payment-app-cluster",
             "容器 mem_limit=256MB；working_set/limit > 0.85 持续 1m 即 OOMKill 前兆",
             ["ev-cluster-quota"]),
        _con("con:app-cpu-throttle", "metric:cpu-throttle", "capacity",
             ["cpu", "节流", "throttle", "quota", "配额", "cpu 不足"],
             "warn", "app:payment-app-cluster",
             "容器 cpus=0.5；cgroup 节流周期速率 > 0.1/s 持续 2m 即 CPU 配额不足",
             ["ev-cluster-quota"]),
        _con("con:replica-lag", "metric:replica-lag", "threshold",
             ["复制延迟", "replica lag", "replication lag", "lag", "主从", "陈旧", "延迟"],
             "block", "db:mysql-replica",
             "Seconds_Behind_Master > 5s 判定复制延迟，读路径数据陈旧",
             ["ev-cluster-replication"]),
        _con("con:replica-down", "metric:replica-io-running", "business_rule",
             ["复制中断", "replication broken", "io thread", "sql thread", "副本丢失", "replica down"],
             "block", "db:mysql-replica",
             "副本 IO/SQL 线程任一为 0，或副本 exporter 不可达，判定复制中断/副本丢失",
             ["ev-cluster-replication"]),
        _con("con:db-availability", "db:mysql-cluster", "business_rule",
             ["数据库不可用", "mysql down", "数据库挂了"],
             "block", "db:mysql-cluster",
             "任一 MySQL 节点 mysql_up=0 持续 1m 判定该节点不可达",
             ["ev-cluster-prometheus-targets"]),
        _con("con:db-role", "db:primary", "business_rule",
             ["只读", "read_only", "super_read_only", "写失败", "配置漂移"],
             "block", "db:primary",
             "主库 read_only=ON 时写请求返回 1290 → 应用 5xx；"
             "此约束用于把\"健康检查正常但交易失败\"导向主库角色配置",
             ["ev-cluster-fault-catalog"]),
        _con("con:row-lock", "rc:row-lock", "business_rule",
             ["行锁", "lock wait", "锁等待", "长事务", "innodb_row_lock"],
             "block", "db:primary",
             "Innodb_row_lock_current_waits > 0 持续 1m 判定存在行锁等待（长事务持锁）",
             ["ev-cluster-fault-catalog"]),
        _con("con:pool-waiting", "metric:conn-exhaust", "capacity",
             ["排队", "waiting", "queue", "连接池等待"],
             "warn", "ds:pay",
             "应用连接池出现排队（waiting > 0）即池容量不足，"
             "通常由上游 DB 变慢（锁/慢查询）级联导致",
             ["ev-cluster-prometheus-targets"]),
        _con("con:db-tmp-table", "metric:db-tmp-disk", "capacity",
             ["临时表", "tmp_table", "tmp_disk_tables", "排序落盘", "filesort"],
             "warn", "db:primary",
             "磁盘临时表速率 > 1/s 持续 3m 判定排序内存不足（tmp_table_size 偏小）",
             ["ev-cluster-prometheus-targets"]),
        _con("con:app-error-budget", "metric:http-error-rate", "threshold",
             ["5xx", "错误率", "error rate", "失败率"],
             "block", "app:payment-app-cluster",
             "应用 5xx 错误率 > 5% 持续 2m 判定写路径/依赖故障",
             ["ev-cluster-prometheus-targets"]),
        _con("con:cluster-cpu-capacity", "metric:cpu-throttle", "capacity",
             ["集群容量", "cluster capacity", "整体资源不足", "容量不足"],
             "warn", "app:payment-app-cluster",
             "多数副本同时出现 CPU 节流/内存压力时，判定为集群整体容量不足"
             "（应扩副本或提高配额），而非单副本故障",
             ["ev-cluster-quota"]),
    ]

    patch = {"terms": terms, "mappings": mappings, "relations": relations,
             "constraints": constraints, "evidence": evidence}
    _self_check(patch,
                base_term_ids=base_term_ids or set(),
                base_evidence_ids=base_evidence_ids or set(),
                base_mapping_ids=base_mapping_ids or set())
    return patch


def _self_check(patch: Dict[str, List[Dict[str, Any]]],
                base_term_ids: Set[str],
                base_evidence_ids: Set[str],
                base_mapping_ids: Set[str]) -> None:
    """补丁交叉引用自检（与 EvoOntology validate() 同规则，提前发现低级错误）。"""
    ev_ids: Set[str] = {e["id"] for e in patch["evidence"]} | set(base_evidence_ids)
    term_ids: Set[str] = {t["id"] for t in patch["terms"]} | set(base_term_ids)
    map_ids: Set[str] = {m["id"] for m in patch["mappings"]} | set(base_mapping_ids)
    problems: List[str] = []

    for t in patch["terms"]:
        for ref in t.get("evidence_refs") or []:
            if ref not in ev_ids:
                problems.append("term %s -> 未知 evidence %s" % (t["id"], ref))
    for m in patch["mappings"]:
        if m["term_id"] not in term_ids:
            problems.append("mapping %s -> 未知 term %s" % (m["id"], m["term_id"]))
        for ref in m.get("evidence_refs") or []:
            if ref not in ev_ids:
                problems.append("mapping %s -> 未知 evidence %s" % (m["id"], ref))
    for r in patch["relations"]:
        if r["source"] not in term_ids:
            problems.append("relation %s -> 未知 source %s" % (r["id"], r["source"]))
        if r["target"] not in term_ids:
            problems.append("relation %s -> 未知 target %s" % (r["id"], r["target"]))
        for ref in r.get("evidence_refs") or []:
            if ref not in ev_ids:
                problems.append("relation %s -> 未知 evidence %s" % (r["id"], ref))
    for c in patch["constraints"]:
        if c["target"] not in term_ids and c["target"] not in map_ids:
            problems.append("constraint %s -> 未知 target %s" % (c["id"], c["target"]))
        for ref in c.get("evidence_refs") or []:
            if ref not in ev_ids:
                problems.append("constraint %s -> 未知 evidence %s" % (c["id"], ref))

    if problems:
        raise ValueError("本体补丁自检失败:\n  - " + "\n  - ".join(problems))
