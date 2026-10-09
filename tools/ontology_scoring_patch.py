# -*- coding: utf-8 -*-
"""
第二轮本体增量：为根因术语补上**打分消歧字段**（ontology_v2）。

## 为什么需要这一轮

第一轮（集群化）解决了"知识有没有"：top5 命中从 1/21 提升到 **21/21**。
但 top1 只有 7/21 —— 因为 `rc:replica-loss` 的词典里有"副本"，
而"副本假死""副本 OOM""副本被节流"的告警文本里**也都有**"副本"，
关键词打分无法区分同一实体的不同故障模式。

这不是"本体缺知识"，而是"本体缺**表达能力**"：术语只能声明自己是什么，
不能声明"什么情况下**不是**我"。于是第二轮迭代给 Term 增加两个字段：

  · `scoring_keywords`  —— 补充的判别性正向关键词（比 name/alias 更贴近告警措辞）
  · `negative_keywords` —— 出现即**扣分**的排除词（"我不是这个故障"的显式声明）

引擎侧（`ontology_scoring.py`）按
  `weighted = primary + 0.5×secondary − |negative_hits|`
计算，把消歧知识交还给本体维护者，而不是写进代码。

## 这也是"双向迭代"的证据

  第 1 轮：环境变了 → 本体补知识 → 引擎才可能命中（top5 1/21 → 21/21）
  第 2 轮：引擎精度不够 → 本体补**表达力** → top1 提升

## 评估口径

第 1 轮用"结构性可诊断性"（知识是否存在/连通/可观测）；
第 2 轮知识已完备（1.000 恒定，无法作为闸门），
因此改用 **引擎 top1 精确命中率**（每 case 0/1，ground truth = 场景期望根因 Term）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Set

NOW = "2026-10-02T06:00:00+00:00"


def _term_patch(tid: str, scoring: List[str], negative: List[str],
                rationale: str, role: str = "root_cause") -> Dict[str, Any]:
    """生成一条"术语更新"记录（按 id upsert 覆盖父版本的同名 Term）。"""
    return {"id": tid, "role": role,
            "scoring_keywords": scoring, "negative_keywords": negative,
            "scoring_rationale": rationale}


#: Term.role —— 本体对"这个术语在 RCA 里扮演什么角色"的显式声明。
#  引擎据此做权重（根因 > 组件 > 观测/规则），而不是靠 id 前缀猜。
ROLE_ROOT_CAUSE = "root_cause"
ROLE_COMPONENT = "component"
ROLE_OBSERVATION = "observation"
ROLE_RULE = "rule"


def _con(cid: str, target: str, ctype: str, keywords: List[str], severity: str,
         scope: str, desc: str, evidence: List[str]) -> Dict[str, Any]:
    return {"id": cid, "target": target, "constraint_type": ctype,
            "trigger_keywords": keywords, "severity": severity, "scope": scope,
            "confidence": "high", "description": desc, "evidence_refs": evidence}


def build_patch(base_term_ids: Set[str] | None = None,
                base_evidence_ids: Set[str] | None = None,
                base_mapping_ids: Set[str] | None = None
                ) -> Dict[str, List[Dict[str, Any]]]:
    """
    构建消歧增量。返回 5 族补丁：
      · terms       —— 对既有根因术语的 **upsert**（补两个打分字段）
      · evidence    —— 新增 3 条可复现观测（A/B 结果、词表来源、门禁口径）
      · constraints —— 新增 1 条"快路径采信条件"约束（把路由规则也本体化一份）
    """
    base_term_ids = base_term_ids or set()

    scoring_terms: List[Dict[str, Any]] = [
        _term_patch(
            "rc:replica-loss",
            ["副本丢失", "副本数下降", "在册副本", "副本减少", "被杀", "被终止", "退出",
             "崩溃", "killed", "terminated", "replica loss", "replicas 不足", "容器消失"],
            ["假死", "挂起", "pause", "节流", "throttle", "内存", "oom", "延迟", "丢包", "锁"],
            "副本丢失与其他副本类故障共享『副本』一词，必须用排除词把"
            "假死/节流/OOM/网络/锁区分开，否则它会在所有副本类告警里排第一。"),
        _term_patch(
            "rc:replica-hang",
            ["假死", "挂起", "pause", "无响应", "不响应", "僵尸", "hang", "端口在但不返回",
             "探活失败"],
            ["被杀", "终止", "退出", "killed", "内存", "oom", "节流", "throttle"],
            "假死的判别特征是『进程还在但完全不返回』，与『被杀/退出』互斥。"),
        _term_patch(
            "rc:oom-kill",
            ["oom", "oomkill", "oom kill", "oom killed", "内存超限", "被内核杀死",
             "内存不足", "内存压力", "内存逼近", "mem_limit", "memory limit", "内存使用率"],
            ["cpu", "节流", "throttle", "网络", "丢包", "延迟", "锁等待", "复制"],
            "OOMKill 的证据是内存用量触顶 + 容器重启，与 CPU/网络/锁类故障的指标正交。"),
        _term_patch(
            "rc:cpu-throttle",
            ["cpu 节流", "节流", "throttle", "cpu quota", "cpu 配额", "cpu 不足",
             "cpu 饱和", "nr_throttled", "cpu 资源"],
            ["内存", "oom", "网络", "丢包", "锁等待", "复制延迟", "置为只读", "read_only"],
            "CPU 节流由 cgroup 配额造成，必须排除内存/网络/锁等同类关键词。"),
        _term_patch(
            "rc:gateway-down",
            ["网关", "gateway", "负载均衡", "入口", "lb ", "502", "503",
             "外部不可达", "探活失败", "打不进来"],
            ["副本丢失", "oom", "行锁", "复制延迟", "临时表"],
            "入口不可用与业务副本故障的判别点是『副本指标全绿但外部探活为 0』。"),
        _term_patch(
            "rc:net-fault",
            ["网络", "延迟", "丢包", "netem", "network", "rtt", "packet loss",
             "链路", "重传"],
            ["行锁", "复制", "临时表", "置为只读", "read_only", "oom"],
            "网络类故障的特征是资源指标正常但链路耗时/丢包异常。"),
        _term_patch(
            "rc:row-lock",
            ["行锁", "锁等待", "lock wait", "长事务", "阻塞写入", "排他锁",
             "innodb_row_lock", "等锁"],
            ["复制延迟", "replica lag", "临时表", "落盘", "置为只读", "read_only", "内存"],
            "行锁等待是 InnoDB 事务层面的证据（innodb_row_lock_*），"
            "与复制/内存类问题不共享指标。"),
        _term_patch(
            "rc:conn-exhaust",
            ["连接耗尽", "连接数", "max_connections", "too many connections",
             "连接资源", "连接被拒", "threads_connected"],
            ["行锁", "复制延迟", "临时表", "置为只读", "read_only", "cpu"],
            "连接耗尽的证据是 threads_connected/max_connections 逼近 1，"
            "且新连接被拒（Too many connections）。"),
        _term_patch(
            "rc:db-cpu-starve",
            ["threads_running", "数据库 cpu", "db cpu", "并发线程", "cpu 资源",
             "计算资源", "查询排队"],
            ["行锁", "复制延迟", "置为只读", "read_only", "内存", "临时表"],
            "数据库 CPU 不足的证据是并发执行线程持续偏高、查询排队。"),
        _term_patch(
            "rc:tmp-disk",
            ["临时表", "tmp_disk", "落盘", "排序", "filesort", "磁盘临时",
             "created_tmp_disk_tables", "tmp_table_size"],
            ["行锁", "复制延迟", "置为只读", "read_only", "oom", "cpu 节流"],
            "磁盘临时表由排序/分组内存不足造成，证据是 created_tmp_disk_tables 速率。"),
        _term_patch(
            "rc:primary-readonly",
            ["置为只读", "read_only", "read_only", "super_read_only", "写入失败", "配置漂移",
             "1290", "写请求失败", "写路径失败"],
            ["副本丢失", "行锁", "复制延迟", "cpu", "内存"],
            "主库只读的特征是『写全失败但读成功、健康检查全绿』，"
            "错误码 1290 是直接证据。"),
        _term_patch(
            "rc:replica-lag",
            ["复制延迟", "主从延迟", "replica lag", "replication lag",
             "seconds_behind", "数据陈旧", "副本滞后", "读到旧数据"],
            ["副本丢失", "行锁", "临时表", "置为只读", "read_only", "cpu"],
            "复制延迟发生在回放链路上（两个线程都 Running），"
            "与『复制中断/副本丢失』（线程停止或节点消失）互斥。"),
        _term_patch(
            "rc:db-replica-loss",
            ["副本丢失", "副本不可用", "replica down", "复制中断", "io thread",
             "sql thread", "副本节点不可达", "复制停止"],
            ["复制延迟", "数据陈旧", "行锁", "临时表", "置为只读", "read_only"],
            "副本丢失/复制中断的判别点是复制线程或节点本身消失。"),
        _term_patch(
            "rc:cluster-capacity",
            ["集群容量", "整体资源不足", "容量不足", "全部副本", "多副本同时",
             "副本数不足", "整体 cpu", "整体内存"],
            ["单副本", "置为只读", "read_only", "行锁", "复制延迟"],
            "集群容量不足强调『多点同时受限』，与单点故障互斥。"),
        _term_patch(
            "rc:slow-sql",
            ["慢查询", "全表扫描", "索引缺失", "slow query", "slow_queries",
             "sql 变慢", "扫描行数"],
            ["行锁", "锁等待", "连接耗尽", "复制延迟", "临时表", "置为只读", "read_only"],
            "慢查询的证据是 slow_queries 增长与疑似全表扫描，"
            "需要与锁等待、连接耗尽等『由他人拖慢』的情形区分。"),
    ]

    present = [t for t in scoring_terms if not base_term_ids or t["id"] in base_term_ids]

    # ── 组件/观测术语也显式声明 role + 负向词 ──────────────────────────────
    # 关键泄漏点：`con:db-role`（主库被置为只读）的 trigger_keywords 含"置为只读", "read_only"，
    # 而它的 target 是 db:primary —— 于是"置为只读", "read_only"被塞进主库的打分词典，
    # 导致"只读副本节点丢失"这类告警把**主库**顶到第一。
    # 正确做法是在本体里声明"主库不会被称作只读副本"。
    component_terms: List[Dict[str, Any]] = [
        _term_patch(
            "db:primary", ["主库", "primary", "主节点", "写入", "binlog"], 
            ["只读副本", "副本节点", "副本丢失", "副本不可用", "replica down", "复制中断"],
            "主库的判别词是『写入/binlog/主节点』；『只读副本』指的是副本，"
            "必须用负向词排除，否则 con:db-role 的『只读』关键词会污染主库。",
            role=ROLE_COMPONENT),
        _term_patch(
            "db:mysql-replica", ["只读副本", "replica", "副本节点", "从库", "回放"],
            ["主库", "primary", "写入失败"],
            "只读副本的判别词是『只读副本/节点/回放』。",
            role=ROLE_COMPONENT),
        _term_patch(
            "db:mysql-core", ["核心库", "creditcard", "账务库", "业务库"],
            ["只读副本", "副本丢失", "复制中断"],
            "库（逻辑数据库）与『节点/副本』是不同粒度，避免粒度混淆。",
            role=ROLE_COMPONENT),
        _term_patch(
            "env:gateway", ["网关", "gateway", "负载均衡", "入口", "lb"],
            ["副本丢失", "行锁", "复制延迟"],
            "网关是入口组件；它的故障模式是『外部打不进来』。",
            role=ROLE_COMPONENT),
        _term_patch(
            "app:payment-app-cluster", ["集群", "副本数", "在册副本", "副本不足"],
            ["副本丢失", "副本假死", "副本离线", "单副本", "被杀", "下线"],
            "集群是副本的集合视图；它描述『整体/多点』，"
            "因此必须用负向词排除单副本的故障模式（丢失/假死/离线），"
            "否则任何含『副本』的告警都会把集群实体顶到第一。",
            role=ROLE_COMPONENT),
        _term_patch(
            "table:t_txn", ["交易表", "t_txn", "流水表", "交易流水"],
            ["审计表", "客户表"],
            "表级定位词。",
            role=ROLE_COMPONENT),
    ]
    present.extend([t for t in component_terms if not base_term_ids or t["id"] in base_term_ids])

    evidence: List[Dict[str, Any]] = [
        {
            "id": "ev-scoring-ab-round1",
            "source": "tools/ontology_scoring_verify.py",
            "query": "python tools/ontology_scoring_verify.py --json-out .chaos/ontology_scoring_ab.json",
            "result": ("① 旧引擎+父本体 top1=1/21 top5=1/21；"
                       "② 新引擎+父本体 top1=1/21 top5=1/21；"
                       "③ 新引擎+ontology_v1 top1=7/21 top5=21/21，"
                       "快路径 16 例且全部被本体约束支撑（旧引擎的 4 例快路径均为无支撑的过期根因）"),
            "validation_method": "同一批 21 个故障场景告警文本的离线确定性复算",
            "timestamp": NOW,
        },
        {
            "id": "ev-scoring-keywords-source",
            "source": "rca-agent/backend/services/ontology_scoring.py",
            "query": "OntologyScoring(workspace).index",
            "result": ("打分词典来源：Constraint.trigger_keywords（全权）+ Term.aliases/id 词元（全权）"
                       "+ Term.name 中文 n-gram（半权）− negative_keywords 命中数；"
                       "类别由约束 target/scope → Term.scope → 邻居多数表决 → id 前缀推导"),
            "validation_method": "代码可读 + A/B 报告可复算",
            "timestamp": NOW,
        },
        {
            "id": "ev-scoring-gate-metric",
            "source": "EvoOntology EvaluationGate(protocol=ground_truth)",
            "query": "record_evolution_evaluation(candidate, {gate_input:{protocol:ground_truth,...}})",
            "result": ("第 1 轮闸门=结构性可诊断性（知识完备性，0.048→1.000）；"
                       "第 2 轮知识已完备（恒定 1.000，不可作闸门），"
                       "改用引擎 top1 精确命中率（每 case 0/1）"),
            "validation_method": "成对评估（父 vs 候选，同一批 21 case）",
            "timestamp": NOW,
        },
    ]

    constraints: List[Dict[str, Any]] = [
        _con("con:fastpath-trust", "metric:replica-healthy-count", "business_rule",
             ["快路径", "确定性", "fast path", "路由", "采信"], "warn",
             "app:payment-app-cluster",
             "快路径采信条件：规则引擎的根因实体必须是某条被告警文本命中的本体约束的 "
             "target（或在其 2 跳邻域内），否则一律交 LLM Agent 复核。"
             "该规则把『路由策略』本身也登记进本体，避免再次退化为硬编码白名单。",
             ["ev-scoring-ab-round1"]),
        # ── 为「缺少管辖约束」的根因补上容量/规则约束 ─────────────────
        # 作用有二：① 让类别推导有据可依（capacity→资源、business_rule→依赖）；
        #          ② 让 favoured 更精确（约束命中即指向对应类别）
        _con("con:replica-loss-detection", "rc:replica-loss", "capacity",
             ["副本丢失", "副本数下降", "在册副本", "副本减少", "容器被杀", "replicas 不足"],
             "block", "app:payment-app-cluster",
             "在册健康副本数 < 期望副本数即判定副本丢失（容量问题）",
             ["ev-scoring-ab-round1"]),
        _con("con:replica-hang-detection", "rc:replica-hang", "capacity",
             ["假死", "挂起", "pause", "无响应", "不响应", "探活失败"],
             "block", "app:payment-app-cluster",
             "容器状态 Up 但健康探针持续失败即判定副本假死（进程在、不干活）",
             ["ev-scoring-ab-round1"]),
        _con("con:oom-detection", "rc:oom-kill", "capacity",
             ["oom", "oomkill", "内存超限", "被内核杀死", "内存压力", "内存不足"],
             "block", "app:payment-app-cluster",
             "容器内存使用率逼近 cgroup 上限并伴随重启/OOMKilled 即判定 OOMKill",
             ["ev-scoring-ab-round1"]),
        _con("con:cluster-capacity", "rc:cluster-capacity", "capacity",
             ["集群容量", "整体资源不足", "容量不足", "全部副本", "多副本同时"],
             "warn", "app:payment-app-cluster",
             "多数副本同时出现节流/内存压力即判定集群整体容量不足（应扩容或提配额）",
             ["ev-scoring-ab-round1"]),
        _con("con:tmp-disk", "rc:tmp-disk", "capacity",
             ["临时表", "落盘", "排序内存不足", "filesort", "tmp_disk"],
             "warn", "db:primary",
             "磁盘临时表速率持续 > 阈值即判定排序/分组内存不足导致落盘",
             ["ev-scoring-ab-round1"]),
        _con("con:conn-exhaust", "rc:conn-exhaust", "capacity",
             ["连接耗尽", "连接数", "max_connections", "too many connections", "连接被拒"],
             "block", "db:mysql-cluster",
             "threads_connected/max_connections 逼近 1 或出现 Too many connections 即判定连接资源耗尽",
             ["ev-scoring-ab-round1"]),
        _con("con:db-cpu-capacity", "rc:db-cpu-starve", "capacity",
             ["threads_running", "数据库 cpu", "并发线程", "查询排队", "cpu 资源"],
             "warn", "db:primary",
             "数据库并发执行线程持续偏高（Threads_running 高企）即判定 DB CPU 资源不足",
             ["ev-scoring-ab-round1"]),
        _con("con:db-replica-down", "rc:db-replica-loss", "threshold",
             ["复制中断", "副本丢失", "副本节点丢失", "副本不可用", "io thread",
              "sql thread", "复制停止", "副本节点不可达"],
             "block", "db:mysql-replica",
             "复制线程停止或副本节点 exporter 不可达即判定副本丢失/复制中断"
             "（与『复制延迟』互斥：后者两个线程都 Running）",
             ["ev-scoring-ab-round1"]),
        _con("con:net-quality", "rc:net-fault", "business_rule",
             ["网络", "延迟", "丢包", "netem", "链路", "重传", "rtt"],
             "warn", "app:payment-app-cluster",
             "资源指标正常但链路耗时/丢包异常即判定网络质量故障（依赖问题）",
             ["ev-scoring-ab-round1"]),
        _con("con:gateway-down", "rc:gateway-down", "business_rule",
             ["网关", "gateway", "负载均衡", "入口", "502", "503", "打不进来"],
             "block", "env:gateway",
             "副本全健康但网关外部探活失败即判定入口不可用（依赖/组件问题）",
             ["ev-scoring-ab-round1"]),
    ]

    return {"terms": present, "mappings": [], "relations": [],
            "constraints": constraints, "evidence": evidence}


def _self_check(patch: Dict[str, List[Dict[str, Any]]]) -> None:
    for t in patch["terms"]:
        if not t.get("scoring_keywords") and not t.get("negative_keywords"):
            raise ValueError("术语 %s 既无 scoring_keywords 也无 negative_keywords" % t["id"])
    for c in patch["constraints"]:
        if not c.get("trigger_keywords"):
            raise ValueError("约束 %s 缺少 trigger_keywords" % c["id"])
