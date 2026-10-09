# -*- coding: utf-8 -*-
"""
第三轮本体增量：清理游离术语 + 补齐缺失概念 + 声明 lifecycle（ontology_v3）。

## 这一轮要解决什么

`tools/ontology_hygiene.py` 在 `ontology_v2` 上量出：

  · **游离术语 2 个**：`env:cluster`、`evt:deploy`（关系图度为 0）→ 不在引擎拓扑、
    永不做候选根因，却会出现在 `browse_semantics` 结果里，对 LLM Agent 是噪声；
  · **缺失概念**：TTL 里的索引个体（`:IdxTxnCustomer` / `:IdxTxnCreated`）
    在首版转换时**根本没被建成 Term**，于是"慢查询是不是索引缺失"这条推理在本体里缺一环；
  · **51 个术语全部未声明 lifecycle** —— EvoOntology 提供的"草稿态"机制一次都没用上，
    游离术语无法被自动过滤。

根因是 `tools/init_evo_ontology.py`（首版 TTL→5 族转换器）**只映射了 TTL 对象属性的
一个子集**，漏了 `managedBy` / `generatesEvent` / `hasIndex`。本轮既补产物，也修生成器。

## 三项工作

**A. 把游离术语真正用起来**
  · `evt:deploy`（应用发布事件）→ 接线到应用与故障，并给出**可观测的部署时间源**
    （容器镜像 + `StartedAt`），新增 `con:change-correlation` 让"是不是发布引入的"
    成为本体可推理的问题；
  · `env:cluster` → 补 `managedBy` 关系接回主图，**改名并加负向词**消除它与
    `app:payment-app-cluster` 的"集群"撞车（它是 Docker 运行时引擎，不是业务集群）；
  · 索引个体 → 补成 Term，接 `hasIndex`，接地到 `information_schema.statistics`，
    并加 `con:index-usage`。

**B. 声明 lifecycle**：给所有既有术语补 `lifecycle.state`（已接线且可观测的 → `active`）。
   未接线的必须声明为 `draft`，否则演化闸门拒绝 accept。
   本轮 A 做完后，全部术语都接线了，因此全部 `active`。

**C. 闸门**：`EvolutionSession.accept()` 之前先跑 `ontology_hygiene.gate()`，
   违反"期望根因游离 / 未声明 lifecycle / active 却游离 / 不可确认的根因"任一条即拒绝。

## 评估口径

本轮的目标不是提高 top1，而是**卫生契约**。因此 ground truth 用
`ontology_hygiene.term_contract()` 的**逐术语 4 条契约得分**（0~1），
并同时记录引擎 top1 作为**回归观察量**（要求不下降）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Set

NOW = "2026-10-02T07:30:00+00:00"


def _term(tid: str, name: str, ttype: str, definition: str, scope: str,
          aliases: List[str], evidence: List[str], role: str,
          scoring: List[str], negative: List[str],
          lifecycle: str = "active") -> Dict[str, Any]:
    return {"id": tid, "name": name, "type": ttype, "definition": definition,
            "scope": scope, "aliases": aliases, "evidence_refs": evidence,
            "role": role, "scoring_keywords": scoring, "negative_keywords": negative,
            "lifecycle": {"state": lifecycle}}


def _rel(rid: str, src: str, rtype: str, tgt: str, cond: str, desc: str,
         evidence: List[str]) -> Dict[str, Any]:
    return {"id": rid, "source": src, "relation_type": rtype, "target": tgt,
            "connection_condition": cond, "description": desc, "evidence_refs": evidence}


def _map(mid: str, term_id: str, source: str, table: str, column: str,
         sem_filter: str, grain: str, validation: str, evidence: List[str]) -> Dict[str, Any]:
    return {"id": mid, "term_id": term_id, "database_source": source, "table": table,
            "column": column, "semantic_filter": sem_filter, "grain": grain,
            "validation": validation, "evidence_refs": evidence, "confidence": "high"}


def _con(cid: str, target: str, ctype: str, keywords: List[str], severity: str,
         scope: str, desc: str, evidence: List[str]) -> Dict[str, Any]:
    return {"id": cid, "target": target, "constraint_type": ctype,
            "trigger_keywords": keywords, "severity": severity, "scope": scope,
            "confidence": "high", "description": desc, "evidence_refs": evidence}


def build_patch(base_term_ids: Set[str] | None = None,
                base_evidence_ids: Set[str] | None = None,
                base_mapping_ids: Set[str] | None = None
                ) -> Dict[str, List[Dict[str, Any]]]:
    base_term_ids = base_term_ids or set()

    # ── Evidence（真实采集值，可复现）──────────────────────────────────
    evidence: List[Dict[str, Any]] = [
        {
            "id": "ev-mysql-indexes",
            "source": "mysql:information_schema.statistics",
            "query": ("SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, NON_UNIQUE, CARDINALITY "
                      "FROM information_schema.statistics "
                      "WHERE TABLE_SCHEMA='creditcard' AND TABLE_NAME='t_txn' "
                      "ORDER BY INDEX_NAME, SEQ_IN_INDEX"),
            "result": ("idx_txn_created(created_at, unique=1, cardinality=20); "
                       "idx_txn_customer(customer_id, unique=1, cardinality=3); "
                       "PRIMARY(txn_id, unique=0, cardinality=187)"),
            "validation_method": "MySQL information_schema.statistics 只读查询",
            "timestamp": NOW,
        },
        {
            "id": "ev-deploy-source",
            "source": "docker inspect <payment-app 副本>",
            "query": ("docker inspect <name> --format "
                      "'{{.Config.Image}} | {{.Created}} | {{.State.StartedAt}}'"),
            "result": ("image=sha256:b9b3f4dd097ef274dee5efb2e33c08baed2f1cd563236f7a9694dc9d665c89be, "
                       "container.Created=2026-10-02T02:19:00Z, "
                       "State.StartedAt=2026-10-02T02:28:20Z "
                       "→ 容器启动时间 = 最近一次发布/重启时间，可作为变更时间窗起点"),
            "validation_method": "read-only docker CLI（镜像 ID + 容器创建/启动时间）",
            "timestamp": NOW,
        },
        {
            "id": "ev-docker-runtime",
            "source": "docker info",
            "query": "docker info --format '{{.ServerVersion}} | {{.OperatingSystem}} | {{.NCPU}} | {{.MemTotal}} | {{.Driver}}'",
            "result": ("29.8.1 | Docker Desktop | NCPU=8 | MemTotal=8221057024 (≈7.66GB) | "
                       "Driver=overlayfs —— 这是**容器运行时引擎**，不是业务集群"),
            "validation_method": "read-only docker CLI",
            "timestamp": NOW,
        },
        {
            "id": "ev-hygiene-audit",
            "source": "tools/ontology_hygiene.py",
            "query": "python tools/ontology_hygiene.py --version ontology_v2",
            "result": ("ontology_v2: 游离术语 2 个（env:cluster / evt:deploy）、"
                       "不可达 2 个、无证据 2 个（api:auth / evt:deploy）、"
                       "不可确认根因 0 个、lifecycle 声明 0/51；"
                       "期望根因 19 个全部可达 → 游离术语未影响 top1(20/21)，"
                       "但污染语义检索（browse_semantics('发布 变更 deploy 上线') 唯一返回 evt:deploy）"),
            "validation_method": "本体卫生审计（关系图度 + 可达性 + 契约评分）",
            "timestamp": NOW,
        },
    ]

    # ── Terms：修游离 + 补索引 + 声明 lifecycle ────────────────────────
    terms: List[Dict[str, Any]] = [
        # A-1 修 env:cluster：接回主图 + 改名消歧
        _term("env:cluster", "Docker 容器运行时引擎（非业务集群）", "entity",
              "承载容器的 Docker Desktop 运行时（WSL2 后端，8 CPU / ≈7.66GB，overlayfs）。"
              "注意：这是**运行时引擎**，业务侧的『集群』是 app:payment-app-cluster"
              "（3 副本应用集群）与 db:mysql-cluster（主从数据库集群），三者不要混用。",
              "运行环境层", ["DockerEngine", "DockerVM", "docker-desktop"], 
              ["ev-docker-runtime", "ev-host-facts"],
              role="component",
              scoring=["docker desktop", "运行时引擎", "docker 引擎", "wsl2", "overlayfs"],
              negative=["业务集群", "副本", "payment-app 集群", "主从"],
              lifecycle="active"),
        # A-2 修 evt:deploy：接事件链 + 给出可观测的部署时间源
        _term("evt:deploy", "应用发布/重启事件", "entity",
              "payment-app 的发布或重启事件（CI/CD 部署、镜像更新、容器重启）。"
              "可观测代理指标：容器镜像 ID + 容器 Created/StartedAt"
              "（即最近一次发布/重启的时间戳）。用于回答『是不是发布引入的』。",
              "RCA 支撑", ["DeployEvent", "ReleaseEvent", "Rollout"], 
              ["ev-deploy-source"],
              role="observation",
              scoring=["发布", "上线", "变更", "deploy", "版本升级", "rollout",
                       "镜像更新", "重启", "灰度"],
              negative=["行锁", "复制延迟", "临时表落盘"],
              lifecycle="active"),
        # A-2 配套：部署时间窗指标
        _term("metric:uptime-since-deploy", "距最近一次发布/重启的时长", "metric",
              "now - 容器 State.StartedAt：判断故障是否落在发布后的敏感窗口内。"
              "这是『变更关联』里唯一可离线复算的时间依据（模拟环境没有 CI 事件源）。",
              "RCA 支撑", ["UptimeSinceDeploy", "DeployWindow"], ["ev-deploy-source"],
              role="observation",
              scoring=["发布后时长", "启动时长", "uptime", "since deploy", "发布窗口"],
              negative=[]),
        # A-3 补索引个体（首版转换器漏掉的 TTL 个体）
        _term("idx:txn-created", "t_txn 时间索引 idx_txn_created", "entity",
              "t_txn(created_at) 索引，cardinality=20。慢查询若未命中该索引"
              "（EXPLAIN type=ALL / Using filesort）即判定索引未生效。",
              "数据库层", ["IdxTxnCreated"], ["ev-mysql-indexes"],
              role="component",
              scoring=["idx_txn_created", "created_at 索引", "时间索引", "idx txn created"],
              negative=["idx_txn_customer"]),
        _term("idx:txn-customer", "t_txn 客户索引 idx_txn_customer", "entity",
              "t_txn(customer_id) 索引，cardinality=3。按客户维度查询应命中该索引。",
              "数据库层", ["IdxTxnCustomer"], ["ev-mysql-indexes"],
              role="component",
              scoring=["idx_txn_customer", "customer_id 索引", "客户索引", "idx txn customer"],
              negative=["idx_txn_created"]),
    ]

    # B. 给所有既有术语补 lifecycle 声明（本轮 A 之后全部已接线 → active）
    lifecycle_upserts = [{"id": tid, "lifecycle": {"state": "active"}}
                         for tid in sorted(base_term_ids)
                         if tid not in {t["id"] for t in terms}]
    terms.extend(lifecycle_upserts)

    # ── Mappings：把新概念接地到物理源 ────────────────────────────────
    mappings: List[Dict[str, Any]] = [
        _map("map:deploy-event", "evt:deploy",
             "docker:docker-inspect", "Container", "Config.Image / Created / State.StartedAt",
             "label=com.docker.compose.service=payment-app", "container",
             "docker inspect → 镜像 ID 与容器创建/启动时间；镜像 ID 变化即一次发布",
             ["ev-deploy-source"]),
        _map("map:uptime-since-deploy", "metric:uptime-since-deploy",
             "docker:docker-inspect", "Container", "State.StartedAt",
             "now() - StartedAt", "container",
             "docker inspect 计算距最近一次发布/重启的时长（秒）",
             ["ev-deploy-source"]),
        _map("map:idx-txn-created", "idx:txn-created",
             "mysql:information_schema", "statistics", "INDEX_NAME/COLUMN_NAME",
             "table_schema='creditcard' AND table_name='t_txn' AND index_name='idx_txn_created'",
             "index",
             "SELECT ... FROM information_schema.statistics", ["ev-mysql-indexes"]),
        _map("map:idx-txn-customer", "idx:txn-customer",
             "mysql:information_schema", "statistics", "INDEX_NAME/COLUMN_NAME",
             "table_schema='creditcard' AND table_name='t_txn' AND index_name='idx_txn_customer'",
             "index",
             "SELECT ... FROM information_schema.statistics", ["ev-mysql-indexes"]),
    ]

    # ── Relations：把游离节点接回主图 ─────────────────────────────────
    relations: List[Dict[str, Any]] = [
        # env:cluster 接回主图（TTL 里本来就有的 managedBy）
        _rel("rel:container-managed-by-runtime", "env:container", "composition",
             "env:cluster", "managedBy: 容器由 Docker 运行时引擎管理",
             "支付容器由 Docker Desktop 运行时引擎调度（TTL `:PayContainer :managedBy "
             ":DockerCluster` 的落地，首版转换器漏映射）", ["ev-docker-runtime"]),
        _rel("rel:app-cluster-managed-by-runtime", "app:payment-app-cluster", "composition",
             "env:cluster", "managedBy: 应用集群运行在容器运行时引擎之上",
             "业务集群（3 副本 + 网关）整体运行在 Docker 运行时引擎上", ["ev-docker-runtime"]),
        # evt:deploy 接回事件链
        _rel("rel:app-generates-deploy", "app:payment-app", "association",
             "evt:deploy", "generatesEvent: 发布/重启产生变更事件",
             "payment-app 的每次发布/重启产生一个变更事件（TTL `:PaymentApp "
             ":generatesEvent :DeployEvent` 的落地，首版转换器漏映射）",
             ["ev-deploy-source"]),
        _rel("rel:deploy-window-metric", "evt:deploy", "association",
             "metric:uptime-since-deploy", "evidencedBy: 发布事件由启动时长指标观测",
             "变更事件的可观测代理：距最近一次发布/重启的时长", ["ev-deploy-source"]),
        _rel("rel:deploy-candidate-incident", "evt:deploy", "association",
             "incident:payment-timeout",
             "triggers: 发布窗口与故障时间窗重叠时作为变更嫌疑",
             "当故障发生时间落在发布后敏感窗口内，变更应作为优先候选根因之一"
             "（而不是无条件归因于数据库）", ["ev-deploy-source"]),
        # 索引接回
        _rel("rel:txn-has-idx-created", "table:t_txn", "composition",
             "idx:txn-created", "hasIndex: 表上的时间索引",
             "t_txn 拥有 created_at 索引（TTL `:TTxn :hasIndex :IdxTxnCreated` 的落地，"
             "首版转换器漏映射且未建 Term）", ["ev-mysql-indexes"]),
        _rel("rel:txn-has-idx-customer", "table:t_txn", "composition",
             "idx:txn-customer", "hasIndex: 表上的客户索引",
             "t_txn 拥有 customer_id 索引", ["ev-mysql-indexes"]),
        _rel("rel:rc-slow-sql-idx", "rc:slow-sql", "association",
             "idx:txn-created",
             "attributedTo: 慢查询的执行计划未命中该索引（需 EXPLAIN 确认）",
             "把『慢查询』与『索引是否生效』接起来：EXPLAIN 显示 type=ALL / "
             "Using filesort 时即可判定索引未生效", ["ev-mysql-indexes"]),
    ]

    # ── Constraints：把新概念变成可判定的业务规则 ──────────────────────
    constraints: List[Dict[str, Any]] = [
        _con("con:change-correlation", "evt:deploy", "business_rule",
             ["发布", "上线", "变更", "deploy", "版本", "升级", "rollout", "重启", "灰度"],
             "warn", "app:payment-app-cluster",
             "故障时间落在发布/重启后的敏感窗口内（距 StartedAt 很近）时，"
             "『变更引入』应与其它根因并列作为候选，而不是被忽略 —— "
             "本约束让『是不是发布引入的』成为本体可回答的问题。",
             ["ev-deploy-source"]),
        _con("con:index-usage", "idx:txn-created", "threshold",
             ["全表扫描", "索引缺失", "type=ALL", "filesort", "using where",
              "using filesort", "explain", "未命中索引"],
             "warn", "table:t_txn",
             "EXPLAIN 出现 type=ALL 或 Using filesort 即判定索引未生效，"
             "慢查询应归因到索引/执行计划而不是泛泛的『数据库慢』。",
             ["ev-mysql-indexes"]),
    ]

    return {"terms": terms, "mappings": mappings, "relations": relations,
            "constraints": constraints, "evidence": evidence}
