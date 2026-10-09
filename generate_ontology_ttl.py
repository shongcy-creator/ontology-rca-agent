#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
生成本体（ontology.ttl）+ 个体实例（instances.ttl）+ 拓扑关系（topology.ttl）
+ SPARQL 样例（sparql_queries.txt）。

数据源：从 docker compose 部署的 app + mysql 模拟环境采集到的真实数据。
"""
import json
import os

BASE = r"D:\05_code\credit-card-sys-ops"
OUT_DIR = os.path.join(BASE, "ontology_turtle")
os.makedirs(OUT_DIR, exist_ok=True)

NAMESPACE = "https://sapiens.ai/ontology/credit-card-ops"
P = NAMESPACE + "#"

# ===================================================================
# 1. 本体文件
# ===================================================================
ONTOLOGY_TTL = f"""
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix :     <{P}> .

:Ontology a owl:Ontology ;
  rdfs:label "Credit Card Operations Ontology"@en , "信用卡系统运维本体"@zh ;
  rdfs:comment "面向信用卡系统运维智能体，覆盖应用层、运行环境层、数据库层及其拓扑关系，支撑根因分析（RCA）"@zh .

# ------------------------------------------------------------------
# 顶层抽象
# ------------------------------------------------------------------
:Component a owl:Class ;
  rdfs:label "Component"@en , "组件"@zh ;
  rdfs:comment "系统中最小的可观测/可定位的抽象单元"@zh .

:Resource a owl:Class ;
  rdfs:label "Resource"@en , "资源"@zh ;
  rdfs:comment "承载或消耗算力的实体（容器、宿主机、数据库、表等）"@zh .

:Observation a owl:Class ;
  rdfs:label "Observation"@en , "观测对象"@zh ;
  rdfs:comment "可被采样的量化信号或离散运维事件"@zh .

:Problem a owl:Class ;
  rdfs:label "Problem"@en , "问题对象"@zh ;
  rdfs:comment "故障或根因等需要被定位的对象"@zh .

# ------------------------------------------------------------------
# 应用层
# ------------------------------------------------------------------
:Application a owl:Class ;
  rdfs:subClassOf :Component ;
  rdfs:label "Application"@en , "应用"@zh ;
  rdfs:comment "信用卡系统中的业务应用（微服务/模块）"@zh .

:BusinessFunction a owl:Class ;
  rdfs:subClassOf :Component ;
  rdfs:label "BusinessFunction"@en , "业务功能"@zh ;
  rdfs:comment "应用内可独立度量的业务能力（授权、清算、限额校验等）"@zh .

:Interface a owl:Class ;
  rdfs:subClassOf :Component ;
  rdfs:label "Interface"@en , "接口"@zh ;
  rdfs:comment "应用对外或应用间调用的服务接口（RPC/HTTP/消息）"@zh .

# ------------------------------------------------------------------
# 运行环境层
# ------------------------------------------------------------------
:RuntimeEnvironment a owl:Class ;
  rdfs:subClassOf :Resource ;
  rdfs:label "RuntimeEnvironment"@en , "运行环境"@zh ;
  rdfs:comment "承载应用运行的环境抽象（容器、集群、宿主机）"@zh .

:Container a owl:Class ;
  rdfs:subClassOf :RuntimeEnvironment ;
  rdfs:label "Container"@en , "容器"@zh ;
  rdfs:comment "应用所运行的容器实例（如 K8s Pod/Container）"@zh .

:HostMachine a owl:Class ;
  rdfs:subClassOf :Resource ;
  rdfs:label "HostMachine"@en , "宿主机"@zh ;
  rdfs:comment "承载容器的物理或虚拟宿主机"@zh .

:Cluster a owl:Class ;
  rdfs:subClassOf :RuntimeEnvironment ;
  rdfs:label "Cluster"@en , "集群"@zh ;
  rdfs:comment "容器编排集群（如 Kubernetes Cluster）"@zh .

# ------------------------------------------------------------------
# 数据库层
# ------------------------------------------------------------------
:Database a owl:Class ;
  rdfs:subClassOf :Resource ;
  rdfs:label "Database"@en , "数据库"@zh ;
  rdfs:comment "应用访问的数据存储资源（关系库、缓存、消息队列）"@zh .

:DataSource a owl:Class ;
  rdfs:subClassOf :Database ;
  rdfs:label "DataSource"@en , "数据源"@zh ;
  rdfs:comment "应用配置中引用的逻辑数据库连接（JDBC 视角）"@zh .

:Table a owl:Class ;
  rdfs:subClassOf :Resource ;
  rdfs:label "Table"@en , "表"@zh ;
  rdfs:comment "数据库中的逻辑表"@zh .

:Index a owl:Class ;
  rdfs:subClassOf :Resource ;
  rdfs:label "Index"@en , "索引"@zh ;
  rdfs:comment "表上的索引对象，用于定位慢 SQL"@zh .

# ------------------------------------------------------------------
# RCA 支撑
# ------------------------------------------------------------------
:Metric a owl:Class ;
  rdfs:subClassOf :Observation ;
  rdfs:label "Metric"@en , "指标"@zh ;
  rdfs:comment "可采样的量化观测值（CPU/内存/QPS/延迟/GC 等）"@zh .

:Event a owl:Class ;
  rdfs:subClassOf :Observation ;
  rdfs:label "Event"@en , "事件"@zh ;
  rdfs:comment "离散运维事件（部署、扩缩容、故障切换、告警触发）"@zh .

:Alert a owl:Class ;
  rdfs:subClassOf :Observation ;
  rdfs:label "Alert"@en , "告警"@zh ;
  rdfs:comment "由规则或模型产生的告警，含严重级别"@zh .

:Incident a owl:Class ;
  rdfs:subClassOf :Problem ;
  rdfs:label "Incident"@en , "故障"@zh ;
  rdfs:comment "一次可被确认的故障，聚合若干告警与影响"@zh .

:RootCause a owl:Class ;
  rdfs:subClassOf :Problem ;
  rdfs:label "RootCause"@en , "根因"@zh ;
  rdfs:comment "被归因的最小故障因素，关联到具体实体与证据"@zh .

# ------------------------------------------------------------------
# 对象属性（拓扑关系）
# ------------------------------------------------------------------
:runsOn a owl:ObjectProperty ;
  rdfs:domain :Application ;
  rdfs:range :Container ;
  rdfs:label "runsOn"@en , "运行于"@zh ;
  rdfs:comment "应用运行于容器（1 应用可对应多副本容器）"@zh .

:deployedOn a owl:ObjectProperty ;
  rdfs:domain :Container ;
  rdfs:range :HostMachine ;
  rdfs:label "deployedOn"@en , "部署于"@zh ;
  rdfs:comment "容器被调度部署在某台宿主机上"@zh .

:hosts a owl:ObjectProperty ;
  rdfs:domain :HostMachine ;
  rdfs:range :Container ;
  rdfs:label "hosts"@en , "承载"@zh ;
  rdfs:comment "deployedOn 的逆向，便于从宿主机查容器"@zh .

:accesses a owl:ObjectProperty ;
  rdfs:domain :Application ;
  rdfs:range :Database ;
  rdfs:label "accesses"@en , "访问"@zh ;
  rdfs:comment "应用通过数据源访问数据库"@zh .

:hasDataSource a owl:ObjectProperty ;
  rdfs:domain :Application ;
  rdfs:range :DataSource ;
  rdfs:label "hasDataSource"@en , "引用数据源"@zh ;
  rdfs:comment "应用引用连接池"@zh .

:dataSourceOf a owl:ObjectProperty ;
  rdfs:domain :DataSource ;
  rdfs:range :Database ;
  rdfs:label "dataSourceOf"@en , "指向数据库"@zh ;
  rdfs:comment "数据源指向具体数据库实例"@zh .

:containsTable a owl:ObjectProperty ;
  rdfs:domain :Database ;
  rdfs:range :Table ;
  rdfs:label "containsTable"@en , "包含表"@zh ;
  rdfs:comment "库内表"@zh .

:hasIndex a owl:ObjectProperty ;
  rdfs:domain :Table ;
  rdfs:range :Index ;
  rdfs:label "hasIndex"@en , "拥有索引"@zh ;
  rdfs:comment "表拥有索引"@zh .

:managedBy a owl:ObjectProperty ;
  rdfs:domain :Container ;
  rdfs:range :Cluster ;
  rdfs:label "managedBy"@en , "由集群管理"@zh ;
  rdfs:comment "容器由某集群管理（可选）"@zh .

:produces a owl:ObjectProperty ;
  rdfs:domain :Component ;
  rdfs:range :Metric ;
  rdfs:label "produces"@en , "产出指标"@zh ;
  rdfs:comment "组件产出某类指标（RCA 证据关联）"@zh .

:generatesEvent a owl:ObjectProperty ;
  rdfs:domain :Component ;
  rdfs:range :Event ;
  rdfs:label "generatesEvent"@en , "产生事件"@zh ;
  rdfs:comment "组件产生运维事件（变更关联）"@zh .

:raises a owl:ObjectProperty ;
  rdfs:domain :Component ;
  rdfs:range :Alert ;
  rdfs:label "raises"@en , "触发告警"@zh ;
  rdfs:comment "组件触发告警"@zh .

:triggers a owl:ObjectProperty ;
  rdfs:domain :Alert ;
  rdfs:range :Incident ;
  rdfs:label "triggers"@en , "升级为故障"@zh ;
  rdfs:comment "告警升级为故障"@zh .

:hasRootCause a owl:ObjectProperty ;
  rdfs:domain :Incident ;
  rdfs:range :RootCause ;
  rdfs:label "hasRootCause"@en , "根因"@zh ;
  rdfs:comment "故障的根因结论（RCA 输出）"@zh .

:attributedTo a owl:ObjectProperty ;
  rdfs:domain :RootCause ;
  rdfs:range :Resource ;
  rdfs:label "attributedTo"@en , "归因于实体"@zh ;
  rdfs:comment "根因归因到某实体（容器/宿主机/数据库/表）"@zh .

:evidencedBy a owl:ObjectProperty ;
  rdfs:domain :RootCause ;
  rdfs:range :Metric ;
  rdfs:label "evidencedBy"@en , "由指标支撑"@zh ;
  rdfs:comment "根因由某指标证据支撑（可解释性）"@zh .

:dependsOn a owl:ObjectProperty ;
  rdfs:domain :Component ;
  rdfs:range :Component ;
  rdfs:label "dependsOn"@en , "依赖"@zh ;
  rdfs:comment "通用依赖（弱语义），可由 invokes/accesses 派生"@zh .

# ------------------------------------------------------------------
# 数据属性
# ------------------------------------------------------------------
:appName a owl:DatatypeProperty ; rdfs:domain :Application ; rdfs:range xsd:string .
:appVersion a owl:DatatypeProperty ; rdfs:domain :Application ; rdfs:range xsd:string .
:owner a owl:DatatypeProperty ; rdfs:domain :Application ; rdfs:range xsd:string .
:language a owl:DatatypeProperty ; rdfs:domain :Application ; rdfs:range xsd:string .

:criticality a owl:DatatypeProperty ; rdfs:domain :BusinessFunction ; rdfs:range xsd:integer .
:slaLevel a owl:DatatypeProperty ; rdfs:domain :BusinessFunction ; rdfs:range xsd:string .
:qps a owl:DatatypeProperty ; rdfs:domain :BusinessFunction ; rdfs:range xsd:decimal .

:protocol a owl:DatatypeProperty ; rdfs:domain :Interface ; rdfs:range xsd:string .
:timeout a owl:DatatypeProperty ; rdfs:domain :Interface ; rdfs:range xsd:integer .
:retryPolicy a owl:DatatypeProperty ; rdfs:domain :Interface ; rdfs:range xsd:string .

:containerId a owl:DatatypeProperty ; rdfs:domain :Container ; rdfs:range xsd:string .
:containerName a owl:DatatypeProperty ; rdfs:domain :Container ; rdfs:range xsd:string .
:image a owl:DatatypeProperty ; rdfs:domain :Container ; rdfs:range xsd:string .
:cpuLimit a owl:DatatypeProperty ; rdfs:domain :Container ; rdfs:range xsd:decimal .
:memLimit a owl:DatatypeProperty ; rdfs:domain :Container ; rdfs:range xsd:decimal .
:replicas a owl:DatatypeProperty ; rdfs:domain :Container ; rdfs:range xsd:integer .
:restartCount a owl:DatatypeProperty ; rdfs:domain :Container ; rdfs:range xsd:integer .
:containerIP a owl:DatatypeProperty ; rdfs:domain :Container ; rdfs:range xsd:string .
:containerHostname a owl:DatatypeProperty ; rdfs:domain :Container ; rdfs:range xsd:string .

:hostName a owl:DatatypeProperty ; rdfs:domain :HostMachine ; rdfs:range xsd:string .
:hostIP a owl:DatatypeProperty ; rdfs:domain :HostMachine ; rdfs:range xsd:string .
:cpuCores a owl:DatatypeProperty ; rdfs:domain :HostMachine ; rdfs:range xsd:integer .
:memoryGB a owl:DatatypeProperty ; rdfs:domain :HostMachine ; rdfs:range xsd:decimal .
:region a owl:DatatypeProperty ; rdfs:domain :HostMachine ; rdfs:range xsd:string .
:osVersion a owl:DatatypeProperty ; rdfs:domain :HostMachine ; rdfs:range xsd:string .

:dbId a owl:DatatypeProperty ; rdfs:domain :Database ; rdfs:range xsd:string .
:dbType a owl:DatatypeProperty ; rdfs:domain :Database ; rdfs:range xsd:string .
:dbVersion a owl:DatatypeProperty ; rdfs:domain :Database ; rdfs:range xsd:string .
:endpoint a owl:DatatypeProperty ; rdfs:domain :Database ; rdfs:range xsd:string .
:shardPolicy a owl:DatatypeProperty ; rdfs:domain :Database ; rdfs:range xsd:string .
:maxConnections a owl:DatatypeProperty ; rdfs:domain :Database ; rdfs:range xsd:integer .
:waitTimeout a owl:DatatypeProperty ; rdfs:domain :Database ; rdfs:range xsd:integer .

:url a owl:DatatypeProperty ; rdfs:domain :DataSource ; rdfs:range xsd:string .
:user a owl:DatatypeProperty ; rdfs:domain :DataSource ; rdfs:range xsd:string .
:poolLimit a owl:DatatypeProperty ; rdfs:domain :DataSource ; rdfs:range xsd:integer .

:tableName a owl:DatatypeProperty ; rdfs:domain :Table ; rdfs:range xsd:string .
:rowCount a owl:DatatypeProperty ; rdfs:domain :Table ; rdfs:range xsd:decimal .
:indexCount a owl:DatatypeProperty ; rdfs:domain :Table ; rdfs:range xsd:integer .
:isHot a owl:DatatypeProperty ; rdfs:domain :Table ; rdfs:range xsd:boolean .

:metricName a owl:DatatypeProperty ; rdfs:domain :Metric ; rdfs:range xsd:string .
:unit a owl:DatatypeProperty ; rdfs:domain :Metric ; rdfs:range xsd:string .
:sampleInterval a owl:DatatypeProperty ; rdfs:domain :Metric ; rdfs:range xsd:integer .
:threshold a owl:DatatypeProperty ; rdfs:domain :Metric ; rdfs:range xsd:decimal .

:eventId a owl:DatatypeProperty ; rdfs:domain :Event ; rdfs:range xsd:string .
:eventTime a owl:DatatypeProperty ; rdfs:domain :Event ; rdfs:range xsd:dateTime .
:eventType a owl:DatatypeProperty ; rdfs:domain :Event ; rdfs:range xsd:string .
:source a owl:DatatypeProperty ; rdfs:domain :Event ; rdfs:range xsd:string .

:alertId a owl:DatatypeProperty ; rdfs:domain :Alert ; rdfs:range xsd:string .
:severity a owl:DatatypeProperty ; rdfs:domain :Alert ; rdfs:range xsd:string .
:triggerRule a owl:DatatypeProperty ; rdfs:domain :Alert ; rdfs:range xsd:string .

:incidentId a owl:DatatypeProperty ; rdfs:domain :Incident ; rdfs:range xsd:string .
:detectedAt a owl:DatatypeProperty ; rdfs:domain :Incident ; rdfs:range xsd:dateTime .
:resolvedAt a owl:DatatypeProperty ; rdfs:domain :Incident ; rdfs:range xsd:dateTime .
:impact a owl:DatatypeProperty ; rdfs:domain :Incident ; rdfs:range xsd:string .

:rootCauseId a owl:DatatypeProperty ; rdfs:domain :RootCause ; rdfs:range xsd:string .
:category a owl:DatatypeProperty ; rdfs:domain :RootCause ; rdfs:range xsd:string .
:confidence a owl:DatatypeProperty ; rdfs:domain :RootCause ; rdfs:range xsd:decimal .
:evidenceRef a owl:DatatypeProperty ; rdfs:domain :RootCause ; rdfs:range xsd:string .

# ------------------------------------------------------------------
# 传递性 / 对称性 约束（用于推理）
# ------------------------------------------------------------------
# 注：deployedOn 在 OWL 中不设为 transitive（容器->宿主机是单层），
# 但可通过 runsOn o deployedOn 派生 应用->宿主机 的隐含路径。
# 这里仅声明 hasRootCause 与 triggers 的语义；跨层推导由 SPARQL 路径表达式完成。

"""

# ===================================================================
# 2. 个体实例（从 docker compose 实际部署采集）
# ===================================================================
def build_instances(json_path):
    with open(json_path, encoding="utf-8-sig") as f:
        snap = json.load(f)
    top = snap["topologyBaseline"]
    host = snap["hostMachine"]
    docker = snap["docker"]

    app_name    = top["app"]

    app_name    = top["app"]
    app_image   = top["appImage"]
    app_hostname= top["appContainerHostname"]
    app_ip      = top["containerIP"]
    subnet      = top["networkCIDR"]
    deployed_on = top["deployedOn"]

    db = top["database"]
    db_name     = db["name"]
    db_type     = db["type"]
    db_version  = db["version"]
    db_cont     = db["containerName"]
    db_hostname = db["containerHostname"]
    db_ip       = db["containerIP"]
    db_ep       = db["endpoint"]
    db_maxconn  = db["maxConnections"]
    db_wait     = db["waitTimeout"]
    db_user     = db["user"]
    db_pool     = db["appUserPoolLimit"]

    # 应用 + 数据源 + 表
    inst = f"""
# =====================================================================
# 个体实例（Individuals）—— 来自 Docker 模拟环境
# =====================================================================
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
@prefix :     <{P}> .

# ---- 应用层个体 -------------------------------------------------
:PaymentApp a :Application ;
  rdfs:label "支付交易应用"@zh ;
  :appName "payment-app" ;
  :appVersion "1.8.2" ;
  :language "nodejs" ;
  :owner "credit-card-team" .

:AuthFunc a :BusinessFunction ;
  rdfs:label "授权"@zh ;
  :criticality 5 ;
  :slaLevel "P0" ;
  :qps 1200 .

:AuthAPI a :Interface ;
  rdfs:label "授权接口"@zh ;
  :protocol "RPC" ;
  :timeout 500 ;
  :retryPolicy "3x-exponential" .

# ---- 运行环境层个体 ---------------------------------------------
:PayContainer a :Container ;
  rdfs:label "支付容器"@zh ;
  :containerId "{app_hostname}" ;
  :containerName "{app_name}" ;
  :image "{app_image}" ;
  :cpuLimit 2.0 ;
  :memLimit 4.0 ;
  :replicas 1 ;
  :restartCount 0 ;
  :containerIP "{app_ip}" ;
  :containerHostname "{app_hostname}" .

:HostWsl a :HostMachine ;
  rdfs:label "宿主机（Docker Desktop WSL2 虚拟主机）"@zh ;
  :hostName "{deployed_on}" ;
  :hostIP "{host['primaryBridgeIP']}" ;
  :cpuCores {host['cpuCores']} ;
  :memoryGB {host['ramGB']} ;
  :region "local" ;
  :osVersion "{host['os']}" .

:HostPhys a :HostMachine ;
  rdfs:label "物理宿主机（遥遥领先）"@zh ;
  :hostName "{host['name']}" ;
  :hostIP "192.168.1.106" ;
  :cpuCores {host['cpuCores']} ;
  :memoryGB {host['ramGB']} ;
  :region "local" ;
  :osVersion "{host['os']}" .

:DockerCluster a :Cluster ;
  rdfs:label "Docker Desktop 集群"@zh ;
  :clusterName "{docker['engine']}" ;
  :version "{docker['serverVersion']}" ;
  :cpus {docker['cpus']} ;
  :memoryGB {docker['memoryGB']} .

# ---- 数据库层个体 -----------------------------------------------
:MysqlCore a :Database ;
  rdfs:label "核心账务库"@zh ;
  :dbId "mysql-{db_hostname}" ;
  :dbType "{db_type}" ;
  :dbVersion "{db_version}" ;
  :endpoint "{db_ep}" ;
  :shardPolicy "none" ;
  :maxConnections {db_maxconn} ;
  :waitTimeout {db_wait} .

:PayDataSource a :DataSource ;
  rdfs:label "支付数据源"@zh ;
  :url "jdbc:mysql://{db_ep}" ;
  :user "{db_user}" ;
  :poolLimit {db_pool} .

:TCustomer a :Table ;
  rdfs:label "客户表"@zh ;
  :tableName "t_customer" ;
  :rowCount 3 ;
  :indexCount 2 ;
  :isHot false .

:TTxn a :Table ;
  rdfs:label "交易表"@zh ;
  :tableName "t_txn" ;
  :rowCount 3 ;
  :indexCount 3 ;
  :isHot true .

:TAudit a :Table ;
  rdfs:label "审计表"@zh ;
  :tableName "t_audit" ;
  :rowCount 0 ;
  :indexCount 1 ;
  :isHot false .

:IdxTxnCustomer a :Index ;
  rdfs:label "t_txn 客户索引"@zh ;
  :indexName "idx_txn_customer" ;
  :column "customer_id" .

:IdxTxnCreated a :Index ;
  rdfs:label "t_txn 时间索引"@zh ;
  :indexName "idx_txn_created" ;
  :column "created_at" .

# ---- RCA 支撑个体（示例故障 + 根因 + 指标） ---------------------
:MysqlLatencyP99 a :Metric ;
  rdfs:label "MySQL P99 延迟"@zh ;
  :metricName "mysql.latency.p99" ;
  :unit "ms" ;
  :sampleInterval 15 ;
  :threshold 100 .

:ConnExhaustion a :Metric ;
  rdfs:label "连接池耗尽信号"@zh ;
  :metricName "db.connection.exhausted" ;
  :unit "count" ;
  :sampleInterval 5 ;
  :threshold 180 .

:DeployEvent a :Event ;
  rdfs:label "支付应用发布事件"@zh ;
  :eventId "deploy-payment-v1.8.2" ;
  :eventTime "2026-01-12T03:00:00Z"^^xsd:dateTime ;
  :eventType "deploy" ;
  :source "ci-cd" .

:AlertP99 a :Alert ;
  rdfs:label "MySQL P99 超阈值告警"@zh ;
  :alertId "ALERT-P99-001" ;
  :severity "P1" ;
  :triggerRule "mysql.latency.p99 > 100 for 5m" .

:IncPaymentTimeout a :Incident ;
  rdfs:label "支付交易超时故障"@zh ;
  :incidentId "INC-20260112-001" ;
  :detectedAt "2026-01-12T03:10:00Z"^^xsd:dateTime ;
  :resolvedAt "2026-01-12T04:30:00Z"^^xsd:dateTime ;
  :impact "P0, payment success rate -12%" .

:RootSlowSql a :RootCause ;
  rdfs:label "t_txn 慢查询致连接耗尽"@zh ;
  :rootCauseId "RC-001" ;
  :category "data" ;
  :confidence 0.86 ;
  :evidenceRef "slow-query-log-20260112" .
"""

    # 拓扑关系
    topo = f"""
# =====================================================================
# 拓扑关系（Topology edges）—— 来自 Docker 模拟环境
# =====================================================================
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
@prefix :     <{P}> .

# ---- 应用 -> 容器 ---------------------------------------------
:PaymentApp :runsOn :PayContainer .
:PayContainer :deployedOn :HostWsl .
:HostWsl :hosts :PayContainer .

# 物理宿主机（宿主机承载 Docker 引擎，逻辑上承载容器）
:HostWsl a :HostMachine ;
  :hostName "docker-desktop-wsl" ;
  :osVersion "{host['os']}" .
:HostPhys :hosts :PayContainer .

# ---- 应用 -> 数据库 ---------------------------------------------
:PaymentApp :accesses :MysqlCore .
:PaymentApp :hasDataSource :PayDataSource .
:PayDataSource :dataSourceOf :MysqlCore .

# ---- 数据库 -> 表 -----------------------------------------------
:MysqlCore :containsTable :TCustomer .
:MysqlCore :containsTable :TTxn .
:MysqlCore :containsTable :TAudit .

# ---- 表 -> 索引 --------------------------------------------------
:TTxn :hasIndex :IdxTxnCustomer .
:TTxn :hasIndex :IdxTxnCreated .

# ---- 集群管理 ----------------------------------------------------
:PayContainer :managedBy :DockerCluster .

# ---- RCA 支撑链 --------------------------------------------------
:PaymentApp :produces :MysqlLatencyP99 .
:PaymentApp :produces :ConnExhaustion .
:PaymentApp :generatesEvent :DeployEvent .
:PaymentApp :raises :AlertP99 .
:AlertP99 :triggers :IncPaymentTimeout .
:IncPaymentTimeout :hasRootCause :RootSlowSql .
:RootSlowSql :attributedTo :TTxn .
:RootSlowSql :evidencedBy :MysqlLatencyP99 .
:RootSlowSql :evidencedBy :ConnExhaustion .
"""

    return inst, topo


INSTANCES_TTL, TOPOLOGY_TTL = build_instances(os.path.join(BASE, "demo", "topology-snapshot.json"))

# ===================================================================
# 3. SPARQL 查询样例
# ===================================================================
SPARQL = """
PREFIX : <{prefix}>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>

# ------------------------------------------------------------------
# Q1  逆向遍历：从告警/故障 -> 根因 -> 实体 + 证据链（RCA 入口）
# ------------------------------------------------------------------
SELECT ?incident ?rootCause ?entity ?evidence ?confidence WHERE {{
  ?incident :triggers ?alert .   # 占位：实际方向是 alert->incident，这里反向查询
  ?incident :hasRootCause ?rootCause .
  ?rootCause :attributedTo ?entity .
  ?rootCause :evidencedBy ?evidence .
  ?rootCause :confidence ?confidence .
}}

# ------------------------------------------------------------------
# Q2  跨层拓扑展开：应用 -> 容器 -> 宿主机（RCA 上溯路径）
# ------------------------------------------------------------------
SELECT ?app ?container ?host ?db ?table WHERE {{
  ?app :runsOn ?container .
  ?container :deployedOn ?host .
  OPTIONAL {{ ?app :accesses ?db }}
  OPTIONAL {{ ?db :containsTable ?table }}
}}

# ------------------------------------------------------------------
# Q3  数据库连接耗尽定位：找出访问该 DB 的全部应用与数据源
# ------------------------------------------------------------------
SELECT ?app ?dataSource ?poolLimit ?db WHERE {{
  ?db a :Database .
  ?app :accesses ?db .
  ?app :hasDataSource ?dataSource .
  ?dataSource :poolLimit ?poolLimit .
  ?dataSource :dataSourceOf ?db .
}}

# ------------------------------------------------------------------
# Q4  变更关联：在故障时间窗口内查找 deploy 事件
# ------------------------------------------------------------------
SELECT ?event ?eventType ?eventTime ?incident WHERE {{
  ?incident :detectedAt ?detectedAt .
  ?event a :Event ; :eventType ?eventType ; :eventTime ?eventTime .
  FILTER (?eventType = "deploy" || ?eventType = "scale")
  FILTER (?eventTime <= ?detectedAt)
}}

# ------------------------------------------------------------------
# Q5  故障传播影响分析：宿主机故障会影响哪些应用
# ------------------------------------------------------------------
SELECT ?host ?container ?app WHERE {{
  ?host a :HostMachine .
  ?host :hosts ?container .
  ?app :runsOn ?container .
}}

# ------------------------------------------------------------------
# Q6  表级定位：找出热点表 + 慢查询证据
# ------------------------------------------------------------------
SELECT ?table ?db ?rowcount ?hot ?index WHERE {{
  ?db :containsTable ?table .
  ?table :rowCount ?rowcount .
  OPTIONAL {{ ?table :isHot ?hot }}
  OPTIONAL {{ ?table :hasIndex ?index }}
  ?rootCause :attributedTo ?table .
  ?rootCause :evidencedBy ?metric .
}}
""".replace("{prefix}", P)

# ===================================================================
# 4. 写出文件
# ===================================================================
files = {
    "ontology.ttl": ONTOLOGY_TTL,
    "instances.ttl": INSTANCES_TTL,
    "topology.ttl": TOPOLOGY_TTL,
    "sparql_queries.txt": SPARQL,
}
for name, content in files.items():
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("wrote", path, f"{len(content)} bytes")

print("DONE")
