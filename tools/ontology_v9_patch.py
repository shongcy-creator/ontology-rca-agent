# -*- coding: utf-8 -*-
"""
第 9 轮本体迭代的候选 patch：**SHACL 一致性**（关系的谓词符合 TTL 的 domain / range）。

## 为什么需要这一轮（问题由新门禁发现，不是凭空加的）

第 8 轮给关系补上了谓词，SHACL 门禁（tools/ontology_shacl.py，形状由 TTL 的 rdfs:domain/range 生成）
第一次跑 ontology_v8 就报出 11 处 domain/range 违反，涉及 19 条关系。逐条评审（CONTRIBUTING 第 2 条：
先判断是判据错还是数据错，并记录决定）：

| 违反 | 判定 | 处理 |
|---|---|---|
| produces / runsOn / triggers / attributedTo / managedBy 的 5 处 | TTL 口径过窄 | 已在 ontology.ttl 放宽并逐条注释理由（不属本轮） |
| `env:gateway` / `evt:deploy` 用 evidencedBy（domain=RootCause） | **数据错** | 改 observedBy —— 本轮 |
| `db:primary replicatesTo db:mysql-cluster`（range=DatabaseNode） | **数据错** | 改 memberOf —— 本轮 |

前两条不只是类型问题：引擎按 evidencedBy 建"指标 → 以它为证据的根因"索引，
于是 `metric:gateway-probe` 会把 **网关组件本身**当成根因召回、`metric:uptime-since-deploy` 会召回发布事件。
TTL 的 observedBy 注释写的正是"集群/节点的健康必须由外部探活或指标观测" —— 这才是作者本意。

第三条：主库并不复制到"集群"（正确的复制关系是已有的 rel:primary-replicates-to-replica），
这条记录要表达的是主库属于该集群，即 memberOf；端点不变。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

ROOT = Path(__file__).resolve().parent.parent
PARENT = "ontology_v8"

RETYPE: Dict[str, Dict[str, str]] = {
    "rel:gateway-evidenced-by-probe": {
        "predicate": "observedBy",
        "connection_condition": "observedBy: 网关可用性由黑盒探活观测",
        "why": "evidencedBy 的 domain 是 RootCause，网关是组件；组件被外部探活观测应为 observedBy",
    },
    "rel:deploy-window-metric": {
        "predicate": "observedBy",
        "connection_condition": "observedBy: 发布事件由启动时长指标观测",
        "why": "evidencedBy 的 domain 是 RootCause，发布是事件；事件被指标观测应为 observedBy",
    },
    "rel:derive-primary-to-replica": {
        "predicate": "memberOf",
        "connection_condition": "memberOf: 主库节点属于数据库集群（主库的写放大会影响整个集群）",
        "why": ("replicatesTo 的 range 是 DatabaseNode，而目标是集群；主库到副本的复制已由 "
                "rel:primary-replicates-to-replica 表达，这条要说的是主库属于该集群"),
    },
}


def _parent_relations() -> Dict[str, dict]:
    path = ROOT / ".evoontology" / "versions" / PARENT / "relations.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {r["id"]: r for r in (data if isinstance(data, list) else data.get("items", data))}


def build_patch(base_term_ids: Optional[Set[str]] = None,
                base_evidence_ids: Optional[Set[str]] = None,
                base_mapping_ids: Optional[Set[str]] = None
                ) -> Dict[str, List[Dict[str, Any]]]:
    parent = _parent_relations()
    relations = []
    for rid, fix in RETYPE.items():
        assert rid in parent, rid
        relations.append({"id": rid, "predicate": fix["predicate"],
                          "connection_condition": fix["connection_condition"],
                          "predicate_source": "shacl-review",
                          "typing_note": "原谓词 %s；%s" % (parent[rid].get("predicate"), fix["why"])})
    return {"terms": [], "mappings": [], "relations": relations, "constraints": [], "evidence": []}


def describe(parent_eval: Dict[str, Any], cand_eval: Dict[str, Any]) -> Dict[str, Any]:
    hypothesis = (
        "SHACL 门禁（形状由 TTL 的 domain/range 生成）第一次跑就报出 11 处违反。逐条评审后，"
        "5 处判定为 TTL 口径过窄（已在 ontology.ttl 放宽并注释理由），3 处判定为数据错："
        "网关与发布事件误用 evidencedBy（其 domain 是 RootCause，导致引擎按证据召回时把组件/事件当根因），"
        "主库 replicatesTo 集群（range 是 DatabaseNode）。"
        "假设：把这 3 条改为 observedBy / memberOf 后，SHACL 违反记录由 %d 降到 %d，且引擎 top1 不下降。"
        % (parent_eval["zero_credit"], cand_eval["zero_credit"]))
    summary = (
        "SHACL 一致性版本：rel:gateway-evidenced-by-probe 与 rel:deploy-window-metric 改为 observedBy，"
        "rel:derive-primary-to-replica 改为 memberOf（端点均不变）。同时新增 SHACL 门禁："
        "运行时 5 族记录投影为 RDF，domain/range 形状由 ontology.ttl 生成，结构形状见 ontology_turtle/shapes.ttl。")
    limitations = [
        "SHACL 不检查 owl:sameAs：rel:instance-equiv-container 声称副本与容器是同一个体，仍需人工评审。",
        "TTL 的 5 处放宽是评审判断（理由逐条写在 ontology.ttl 注释里），不是由数据推出的结论；"
        "放宽后的 domain/range 用 owl:unionOf 表达，SHACL 侧对应 sh:or。",
        "3 条告警未处理：con:db-maxconn 缺 confidence，rel:alert-triggers-incident 与 rel:app-has-fn 没有证据。",
    ]
    return {"hypothesis": hypothesis, "summary": summary, "limitations": limitations}
