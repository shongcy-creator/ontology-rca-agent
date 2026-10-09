# -*- coding: utf-8 -*-
"""生成信用卡系统运维智能体本体模型审核表（多 Sheet xlsx）。"""

import os
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side

OUT_DIR = r"D:\05_code\credit-card-sys-ops"
OUT_FILE = os.path.join(OUT_DIR, "本体模型设计_信用卡系统运维智能体.xlsx")

ws = Workbook()

HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
TITLE_FONT = Font(bold=True, size=14, color="1F4E78")
SECTION_FILL = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
THIN = Side(style="thin", color="808080")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(vertical="top", wrap_text=True)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)


def style_header_row(row_cells):
    for c in row_cells:
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.alignment = CENTER
        c.border = BORDER


def add_title(sheet, text):
    sheet["A1"] = text
    sheet["A1"].font = TITLE_FONT
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(sheet.max_column, 6))
    sheet["A1"].alignment = Alignment(vertical="center")


def add_table(sheet, start_row, headers, rows, col_widths):
    for i, h in enumerate(headers, start=1):
        sheet.cell(row=start_row, column=i, value=h)
    style_header_row([sheet.cell(row=start_row, column=i) for i in range(1, len(headers) + 1)])
    r = start_row + 1
    for row in rows:
        for i, v in enumerate(row, start=1):
            cell = sheet.cell(row=r, column=i, value=v)
            cell.alignment = WRAP
            cell.border = BORDER
        r += 1
    for i, w in enumerate(col_widths, start=1):
        from openpyxl.utils import get_column_letter
        sheet.column_dimensions[get_column_letter(i)].width = w
    return r


# =================================================================
# Sheet 1 说明
# =================================================================
s0 = ws.active
s0.title = "说明"
add_title(s0, "信用卡系统运维智能体 本体模型设计（审核版）")

intro = [
    ("文档目的", "面向信用卡系统运维智能体，以本体方法论驱动问题根因分析（Root Cause Analysis, RCA），输出待审核的本体模型草案。"),
    ("建模范围", "应用层（Application）、运行环境层（Runtime Environment）、数据库层（Database）三层，以及三层之间的拓扑关系。"),
    ("拓扑基线", "应用 -[运行于]--> 容器 -[部署于]--> 宿主机；应用 -[访问]--> 数据库。"),
    ("交付节奏", "本表为 Excel 审核稿；审核通过后，将据此生成 OWL/TTL 形式化文件（Ontology .ttl + 可选 SPARQL 查询样例 + 推理规则）。"),
    ("审核要点", "请关注：1) 类与层次是否完整；2) 属性/关系语义是否准确；3) 拓扑关系是否足以支撑 RCA；4) 是否需要补充指标/事件/告警等 RCA 支撑概念。"),
]
for i, (k, v) in enumerate(intro, start=3):
    s0.cell(row=i, column=1, value=k).font = Font(bold=True)
    s0.cell(row=i, column=1).border = BORDER
    s0.cell(row=i, column=2, value=v).alignment = WRAP
    s0.cell(row=i, column=2).border = BORDER
s0.column_dimensions["A"].width = 14
s0.column_dimensions["B"].width = 100

# =================================================================
# Sheet 2 概念类
# =================================================================
s1 = ws.create_sheet("概念类")
add_title(s1, "概念类（Classes）")

class_rows = [
    ("应用层", "应用", "Application", "Component",
     "信用卡系统中的一个可运行业务组件，如核心账务、支付、风控、批量等微服务/模块。",
     "appName, appId, version, owner, language, deployMode",
     "应用是 RCA 的观测对象与故障起点。"),
    ("应用层", "业务功能", "BusinessFunction", "Component",
     "应用内可独立度量的业务能力，如授权、清算、退款、限额校验。",
     "functionName, slaLevel, qps, criticality",
     "用于将指标异常定位到具体业务功能。"),
    ("应用层", "接口", "Interface", "Component",
     "应用对外或应用间调用的服务接口（RPC/HTTP/消息）。",
     "interfaceId, protocol, timeout, retryPolicy",
     "用于描述调用链与依赖。"),
    ("运行环境层", "运行环境", "RuntimeEnvironment", "Resource",
     "承载应用运行的环境抽象，具体化为容器或集群等。",
     "envType, region, cluster, namespace",
     "环境层是拓扑的中间节点。"),
    ("运行环境层", "容器", "Container", "RuntimeEnvironment",
     "应用所运行的容器实例（如 K8s Pod/Container）。",
     "containerId, image, cpuLimit, memLimit, replicas, restartCount",
     "RCA 中常因容器资源不足/重启触发。"),
    ("运行环境层", "宿主机", "HostMachine", "RuntimeEnvironment",
     "承载容器的物理或虚拟宿主机。",
     "hostId, ip, cpuCores, memory, region",
     "容器部署于宿主机；宿主机故障影响其上全部容器。"),
    ("运行环境层", "集群", "Cluster", "RuntimeEnvironment",
     "容器编排集群（如 Kubernetes Cluster），管理一组宿主机与容器。",
     "clusterName, version, nodeCount",
     "可选层，便于扩展。"),
    ("数据库层", "数据库", "Database", "Resource",
     "应用访问的数据存储资源，含关系库、缓存、消息队列等。",
     "dbId, dbType, version, endpoint, shardPolicy",
     "数据库故障通常表现为高延迟/连接耗尽。"),
    ("数据库层", "数据源", "DataSource", "Database",
     "应用配置中引用的一个逻辑数据库连接（JDBC URL 视角）。",
     "url, user, poolSize, minPool, maxPool",
     "连接池属性是 RCA 高频关注点。"),
    ("数据库层", "表", "Table", "Resource",
     "数据库中的逻辑表，承载核心业务数据。",
     "tableName, indexCount, rowCount, hot",
     "用于定位慢 SQL 与索引缺失。"),
    ("RCA支撑", "指标", "Metric", "Observation",
     "可采样的量化观测值（CPU、内存、QPS、延迟、GC 等）。",
     "metricName, unit, interval, threshold",
     "RCA 证据链的核心数据。"),
    ("RCA支撑", "事件", "Event", "Observation",
     "离散运维事件（部署、扩缩容、故障切换、告警触发）。",
     "eventId, eventTime, eventType, source",
     "用于关联变更与故障。"),
    ("RCA支撑", "告警", "Alert", "Observation",
     "由规则或模型产生的告警，含严重级别。",
     "alertId, severity, triggerRule, ackState",
     "RCA 的入口信号。"),
    ("RCA支撑", "故障", "Incident", "Problem",
     "一次可被确认的故障，聚合若干告警与影响。",
     "incidentId, detectedAt, resolvedAt, impact, rootCause",
     "RCA 的目标对象。"),
    ("RCA支撑", "根因", "RootCause", "Problem",
     "被归因的最小故障因素，关联到具体实体（容器/宿主机/数据库/表/指标）。",
     "rootCauseId, category, confidence, evidenceRef",
     "RCA 的输出结论。"),
]
add_table(s1, 3,
          ["所属层", "类名", "英文标识", "父类", "定义/说明", "关键数据属性", "备注"],
          class_rows, [10, 14, 16, 16, 42, 34, 26])

# =================================================================
# Sheet 3 属性
# =================================================================
s2 = ws.create_sheet("属性")
add_title(s2, "属性（Object & Data Properties）")

prop_rows = [
    # 对象属性
    ("hasComponent", "Object", "Component", "Component", "组件包含下级组件（如应用包含接口）。", "层次化包含。"),
    ("exposes", "Object", "Application", "Interface", "应用暴露的服务接口。", "用于调用链建模。"),
    ("invokes", "Object", "Interface", "Interface", "一个接口调用另一个接口（同步依赖）。", "有向依赖边。"),
    ("publishes", "Object", "Application", "Topic", "应用向消息主题发布消息。", "可选扩展。"),
    ("consumes", "Object", "Application", "Topic", "应用消费某主题。", "可选扩展。"),
    ("accesses", "Object", "Application", "Database", "应用访问数据库（拓扑核心关系）。", "基线：app->访问->db。"),
    ("hasDataSource", "Object", "Application", "DataSource", "应用引用数据源。", "连接池视角。"),
    ("dataSourceOf", "Object", "DataSource", "Database", "数据源指向具体数据库。", ""),
    ("containsTable", "Object", "Database", "Table", "数据库包含表。", ""),
    ("hasIndex", "Object", "Table", "Index", "表拥有索引。", "可选。"),
    ("runsOn", "Object", "Application", "Container", "应用运行于容器（拓扑核心关系）。", "基线：app->运行于->容器。"),
    ("deployedOn", "Object", "Container", "HostMachine", "容器部署于宿主机（拓扑核心关系）。", "基线：容器->部署于->宿主机。"),
    ("managedBy", "Object", "Container", "Cluster", "容器由某集群管理。", "可选层。"),
    ("hosts", "Object", "HostMachine", "Container", "宿主机承载容器（deployedOn 的逆向）。", "逆向便于查询。"),
    ("dependsOn", "Object", "Component", "Component", "通用依赖（弱语义），可由 invokes/accesses 派生。", "跨层依赖汇总。"),
    ("produces", "Object", "Component", "Metric", "组件产出某类指标。", "RCA 证据关联。"),
    ("generatesEvent", "Object", "Component", "Event", "组件产生运维事件。", "变更/故障关联。"),
    ("raises", "Object", "Component", "Alert", "组件触发告警。", ""),
    ("triggers", "Object", "Alert", "Incident", "告警升级为故障。", ""),
    ("hasRootCause", "Object", "Incident", "RootCause", "故障的根因结论。", "RCA 输出。"),
    ("attributedTo", "Object", "RootCause", "Resource", "根因归因到某实体（容器/宿主机/数据库/表）。", "RCA 闭环。"),
    ("evidencedBy", "Object", "RootCause", "Metric", "根因由某指标证据支撑。", "可解释性。"),
    # 数据属性
    ("appName", "Data", "Application", "xsd:string", "应用名称。", ""),
    ("version", "Data", "Application", "xsd:string", "版本/构建号。", ""),
    ("owner", "Data", "Application", "xsd:string", "责任团队。", ""),
    ("criticality", "Data", "BusinessFunction", "xsd:integer", "业务关键度（1-5）。", "RCA 定级。"),
    ("slaLevel", "Data", "BusinessFunction", "xsd:string", "SLA 等级。", ""),
    ("timeout", "Data", "Interface", "xsd:integer", "超时毫秒。", "RCA 关注。"),
    ("retryPolicy", "Data", "Interface", "xsd:string", "重试策略。", ""),
    ("protocol", "Data", "Interface", "xsd:string", "协议（HTTP/Dubbo/Kafka）。", ""),
    ("cpuLimit", "Data", "Container", "xsd:decimal", "CPU 限制（核）。", ""),
    ("memLimit", "Data", "Container", "xsd:decimal", "内存限制（GiB）。", ""),
    ("replicas", "Data", "Container", "xsd:integer", "副本数。", ""),
    ("restartCount", "Data", "Container", "xsd:integer", "重启次数。", "RCA 高频信号。"),
    ("image", "Data", "Container", "xsd:string", "镜像与 tag。", "变更关联。"),
    ("hostId", "Data", "HostMachine", "xsd:string", "宿主机标识。", ""),
    ("ip", "Data", "HostMachine", "xsd:string", "IP 地址。", ""),
    ("cpuCores", "Data", "HostMachine", "xsd:integer", "CPU 核数。", ""),
    ("memory", "Data", "HostMachine", "xsd:decimal", "内存（GiB）。", ""),
    ("region", "Data", "HostMachine", "xsd:string", "机房/地域。", ""),
    ("dbType", "Data", "Database", "xsd:string", "数据库类型（MySQL/Redis/Kafka）。", ""),
    ("endpoint", "Data", "Database", "xsd:string", "连接端点。", ""),
    ("shardPolicy", "Data", "Database", "xsd:string", "分片策略。", ""),
    ("poolSize", "Data", "DataSource", "xsd:integer", "连接池大小。", "RCA 关注。"),
    ("maxPool", "Data", "DataSource", "xsd:integer", "连接池上限。", "连接耗尽根因。"),
    ("tableName", "Data", "Table", "xsd:string", "表名。", ""),
    ("rowCount", "Data", "Table", "xsd:decimal", "行数。", ""),
    ("metricName", "Data", "Metric", "xsd:string", "指标名称。", ""),
    ("unit", "Data", "Metric", "xsd:string", "单位。", ""),
    ("interval", "Data", "Metric", "xsd:integer", "采样间隔（秒）。", ""),
    ("threshold", "Data", "Metric", "xsd:decimal", "阈值。", ""),
    ("eventTime", "Data", "Event", "xsd:dateTime", "事件时间。", "变更关联。"),
    ("eventType", "Data", "Event", "xsd:string", "事件类型（deploy/scale/failover）。", ""),
    ("severity", "Data", "Alert", "xsd:string", "严重级别（P0-P4）。", ""),
    ("incidentId", "Data", "Incident", "xsd:string", "故障编号。", ""),
    ("detectedAt", "Data", "Incident", "xsd:dateTime", "发现时间。", ""),
    ("resolvedAt", "Data", "Incident", "xsd:dateTime", "恢复时间。", ""),
    ("confidence", "Data", "RootCause", "xsd:decimal", "根因置信度（0-1）。", "RCA 输出。"),
    ("category", "Data", "RootCause", "xsd:string", "根因类别（资源/代码/配置/依赖/数据）。", ""),
]
add_table(s2, 3,
          ["属性名", "类型", "域 Domain", "值域 Range", "语义说明", "备注"],
          prop_rows, [16, 8, 16, 14, 40, 22])

# =================================================================
# Sheet 4 拓扑关系
# =================================================================
s3 = ws.create_sheet("拓扑关系")
add_title(s3, "拓扑关系（Topology，RCA 核心）")

topo_rows = [
    ("应用 ->[运行于]--> 容器", "应用部署运行在容器内（1 应用可对应多副本容器）。", "runsOn", "沿此边从应用故障上溯到容器资源/重启。"),
    ("容器 ->[部署于]--> 宿主机", "容器被调度部署在某台宿主机上。", "deployedOn", "容器异常时进一步上溯宿主机（CPU/内存/网络/磁盘）。"),
    ("应用 ->[访问]--> 数据库", "应用通过数据源访问数据库/缓存/消息。", "accesses / hasDataSource", "应用高延迟/连接耗尽时沿此边定位数据库层。"),
    ("数据源 ->[指向]--> 数据库", "连接配置到具体数据库实例。", "dataSourceOf", "把连接池参数与数据库实例关联。"),
    ("数据库 ->[包含]--> 表", "库内表。", "containsTable", "定位慢 SQL / 索引缺失。"),
    ("告警 ->[触发]--> 故障", "告警升级为故障。", "triggers", "RCA 入口。"),
    ("故障 ->[根因]--> 根因", "故障的根因结论。", "hasRootCause", "RCA 输出。"),
    ("根因 ->[归因于]--> 实体", "根因落到具体容器/宿主机/数据库/表/指标。", "attributedTo / evidencedBy", "可解释性闭环。"),
]
add_table(s3, 3,
          ["关系路径", "描述", "对应属性", "RCA 用途"],
          topo_rows, [30, 40, 26, 40])

# 传递性说明
s3.cell(row=12, column=1, value="传递性说明：").font = Font(bold=True)
trans = [
    "1. runsOn + deployedOn 可传递出 应用 -> 宿主机 的隐含关系（应用最终运行在宿主机上），供跨层故障传播分析。",
    "2. accesses 可经 hasDataSource -> dataSourceOf 展开到具体数据库实例，再经 containsTable 到表，形成 应用->数据源->库->表 的依赖链。",
    "3. 故障传播方向（影响分析）：宿主机故障 影响 其上容器 影响 容器内应用；数据库故障 影响 访问它的各应用。RCA 采用逆向（从表象往根因）遍历。",
]
r = 13
for line in trans:
    s3.cell(row=r, column=1, value=line).alignment = WRAP
    s3.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
    s3.row_dimensions[r].height = 30
    r += 1

# =================================================================
# Sheet 5 示例个体
# =================================================================
s4 = ws.create_sheet("示例个体")
add_title(s4, "示例个体（Individuals，用于核对建模粒度）")

ind_rows = [
    ("Application", "CoreBankingApp", "核心账务应用", "P0", "账务/批量/清算"),
    ("Application", "PaymentApp", "支付交易应用", "P0", "授权/扣款/退款"),
    ("Application", "RiskApp", "风控应用", "P1", "实时反欺诈/限额"),
    ("BusinessFunction", "AuthFunc", "授权", "criticality=5", "PaymentApp"),
    ("Interface", "authAPI", "授权接口", "protocol=RPC, timeout=500ms", "PaymentApp 暴露"),
    ("Container", "pay-pod-0", "支付容器0", "image=pay:v1.8.2, memLimit=4Gi", "runsOn HostH01"),
    ("Container", "pay-pod-1", "支付容器1", "image=pay:v1.8.2, memLimit=4Gi", "runsOn HostH02"),
    ("HostMachine", "HostH01", "宿主机H01", "ip=10.1.0.11, cpuCores=32, region=DC1", "承载 pay-pod-0 等"),
    ("HostMachine", "HostH02", "宿主机H02", "ip=10.1.0.12, cpuCores=32, region=DC1", "承载 pay-pod-1 等"),
    ("Database", "MySQLCore", "核心账务库", "dbType=MySQL 8.0, shardPolicy=分库分表", "承载账务数据"),
    ("DataSource", "pay_ds", "支付数据源", "maxPool=200, url=jdbc:mysql://MySQLCore", "PaymentApp 引用"),
    ("Table", "t_txn", "交易表", "hot=true, indexCount=6", "MySQLCore 内"),
    ("Metric", "pay_latency_p99", "支付 P99 延迟", "unit=ms, interval=15s, threshold=800", "PaymentApp produces"),
    ("Event", "deploy_20250112", "支付应用 v1.8.2 发布", "eventTime=2025-01-12T03:00Z, type=deploy", "PaymentApp"),
    ("Alert", "alert_p99_high", "支付 P99 超阈值告警", "severity=P1", "由 pay_latency_p99 触发"),
    ("Incident", "INC-20250112-001", "支付交易超时故障", "detectedAt=2025-01-12T03:10Z", "根因=MySQLCore 慢查询/连接耗尽"),
    ("RootCause", "rc_slow_sql", "t_txn 慢查询致连接耗尽", "category=数据, confidence=0.86", "evidencedBy t_txn 索引缺失"),
]
add_table(s4, 3,
          ["类", "个体名", "说明", "关键属性", "关联/备注"],
          ind_rows, [16, 20, 26, 34, 26])

# =================================================================
# Sheet 6 推理与扩展说明
# =================================================================
s5 = ws.create_sheet("推理与扩展")
add_title(s5, "推理与扩展说明（RCA 方法论映射）")

rules = [
    ("R1 资源传播", "若 容器 X 的 cpuLimit 被占满 且 X deployedOn H，则怀疑宿主机 H 争抢资源；沿 deployedOn 上溯。"),
    ("R2 数据库连接耗尽", "若 应用 A 的 DataSource maxPool 接近耗尽 且 A accesses D，则定位 D（慢 SQL / 锁 / 连接泄漏）；沿 accesses 展开。"),
    ("R3 变更关联", "若 Incident 发生时间窗口内存在 Event(type=deploy/scale)，则根因倾向为变更引入；事件时间与故障时间做差比对。"),
    ("R4 故障传播逆向", "从表象告警出发，沿 逆向拓扑（被访问/被承载）遍历候选根因实体，按证据（evidencedBy）打分排序。"),
    ("E1 可选扩展", "增加 Topic/索引/集群 等节点；增加 服务网格(sidecar)、依赖服务 等跨应用调用边。"),
    ("E2 数据落地", "个体与关系可来自 CMDB / K8s API / 链路追踪 / 数据库元数据，映射为本体实例。"),
    ("E3 输出", "最终 RCA 输出为 RootCause + attributedTo 实体 + evidencedBy 指标证据链，支撑可解释运维。"),
]
add_table(s5, 3,
          ["编号", "说明"],
          rules, [14, 100])

ws.save(OUT_FILE)
print("saved:", OUT_FILE)
