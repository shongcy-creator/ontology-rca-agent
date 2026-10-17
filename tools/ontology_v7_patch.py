# -*- coding: utf-8 -*-
"""
第 7 轮本体迭代的候选 patch：**证据时效**（evidence freshness）。

## 为什么需要这一轮（问题来自审计，不是凭空加的）

`tools/ontology_freshness.py` 对 `ontology_v6` 的检查结果：8 条记录、11 处点名了
**声明环境（rca-agent/docker-compose.yml）里已不存在的实体**：

| 记录 | 点名 | 问题 |
|---|---|---|
| `env:container` name、`app:payment-app` aliases | `cc-credit-card-app` | 单实例时代的容器，集群里由 payment-app 的 3 个副本取代 |
| `map:app-container` | `cc-credit-card-app` | 接地映射指向一个不存在的容器 —— Agent 照着查只会得到空结果 |
| `rel:app-runs-on-container` / `rel:container-deployed-on-host` | `cc-credit-card-app` | 关系描述 |
| `db:mysql-cluster` aliases | `cc-mysql-cluster` | 长得像容器名、环境里却没有这个容器的别名 |
| 4 条证据 | `cc-credit-card-app` | 单实例时代的真实观测 |

另有一处检查器抓不到、但同属一类的过期陈述：`map:app-ip` 写死了旧容器的 IP `172.18.0.3`。

## 这一轮做什么（以及刻意不做什么）

1. 4 条证据标 `status: historical` 并写明 `observed_in` / `historical_reason`。
   **不改写观测内容** —— 它们是真实的过去观测；没重新观测就改结果等于伪造证据。
2. 修正描述**当前状态**的记录（术语名称/别名、映射、关系描述），使其只点名声明环境里的实体。
3. 只在**已有的集群时代证据确实观测到同一事实**时，才把引用改指过去（逐条见 `REPOINT_REASONS`）。
   其余只靠历史证据支撑的记录**不硬配证据**，由时效检查如实列为"待重新观测"告警。
4. 术语名称里的 ASCII 词元按全权计入打分（ontology_scoring._ascii_tokens），
   所以 `env:container` 的新名称只保留中文"支付容器"—— 中文 n-gram 与原来完全相同，
   去掉的 `cc-credit-card-app` 词元从未出现在任何告警文本里。引擎 top1 作为回归观察量实测。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

ROOT = Path(__file__).resolve().parent.parent
PARENT = "ontology_v6"

SINGLE_INSTANCE = "单实例栈（demo/docker-compose.yml，应用容器 cc-credit-card-app）"
GONE = ("集群化后应用由 compose 服务 payment-app 的 3 个副本（rca-agent-payment-app-1..3）承载，"
        "cc-credit-card-app 已不在当前环境中。观测内容保留为历史事实，不改写。")

HISTORICAL_EVIDENCE: Dict[str, Dict[str, Any]] = {
    "ev-docker-ps": {"observed_in": SINGLE_INSTANCE, "historical_reason": GONE,
                     "superseded_by": ["ev-cluster-topology"]},
    "ev-app-topology": {"observed_in": SINGLE_INSTANCE, "historical_reason": GONE},
    "ev-db-probe": {"observed_in": SINGLE_INSTANCE, "historical_reason": GONE},
    "ev:rca-inc_1790868998": {
        "observed_in": "ontology_v0 时代的一次 RCA 推理存档（当时 env:container 名为「支付容器 cc-credit-card-app」）",
        "historical_reason": "这是推理过程的快照，引用的是当时的术语名称与拓扑，不代表当前环境。",
    },
}

#: 改指证据的逐条理由 —— 只收"该集群时代证据的观测内容直接覆盖了这条陈述"的情形。
REPOINT_REASONS: Dict[str, Dict[str, str]] = {
    "app:payment-app": {"ev-cluster-topology": "观测到 payment-app 的 3 个副本 Up (healthy)"},
    "env:container": {"ev-cluster-topology": "观测到承载 payment-app 的 3 个容器",
                      "ev-cluster-quota": "docker inspect 读到这些容器的内存/CPU 配额"},
    "rel:app-runs-on-container": {"ev-cluster-topology": "payment-app 以容器形式运行（按 compose 服务标签过滤）"},
    "rel:container-deployed-on-host": {"ev-docker-runtime": "docker info：容器运行时为 Docker Desktop"},
    "rel:host-wsl-hosts-container": {"ev-docker-runtime": "docker info：容器运行时为 Docker Desktop"},
    "rel:app-derives-host": {"ev-cluster-topology": "派生链第一段 runsOn 的观测",
                             "ev-docker-runtime": "派生链第二段 deployedOn 的观测"},
    "map:app-container": {"ev-cluster-topology": "映射的过滤条件就是这条证据的查询条件"},
}


def _parent_records() -> Dict[str, List[dict]]:
    vdir = ROOT / ".evoontology" / "versions" / PARENT
    out = {}
    for fam in ("terms", "mappings", "relations", "constraints", "evidence"):
        data = json.loads((vdir / ("%s.json" % fam)).read_text(encoding="utf-8"))
        out[fam] = data if isinstance(data, list) else data.get("items", data)
    return out


def _by_id(records: List[dict]) -> Dict[str, dict]:
    return {r["id"]: r for r in records}


def _with_refs(record: dict, extra: Dict[str, str], replace: bool = False) -> List[str]:
    base = [] if replace else list(record.get("evidence_refs") or [])
    return base + [e for e in extra if e not in base]


def build_patch(base_term_ids: Optional[Set[str]] = None,
                base_evidence_ids: Optional[Set[str]] = None,
                base_mapping_ids: Optional[Set[str]] = None
                ) -> Dict[str, List[Dict[str, Any]]]:
    p = _parent_records()
    T, R, M, E = (_by_id(p[f]) for f in ("terms", "relations", "mappings", "evidence"))

    evidence = [{"id": eid, "status": "historical", **meta}
                for eid, meta in HISTORICAL_EVIDENCE.items()]

    terms = [
        {"id": "app:payment-app",
         "aliases": [a for a in T["app:payment-app"]["aliases"] if a != "cc-credit-card-app"],
         "evidence_refs": _with_refs(T["app:payment-app"], REPOINT_REASONS["app:payment-app"])},
        {"id": "env:container",
         "name": "支付容器",
         "definition": ("承载 payment-app 的容器实例。集群中为 compose 服务 payment-app 的 3 个副本"
                        "（rca-agent-payment-app-1..3，每个 mem_limit=256MB、cpus=0.50）。"),
         "evidence_refs": _with_refs(T["env:container"], REPOINT_REASONS["env:container"])},
        {"id": "db:mysql-cluster",
         "aliases": [a for a in T["db:mysql-cluster"]["aliases"] if a != "cc-mysql-cluster"]},
    ]

    relations = [
        {"id": "rel:app-runs-on-container",
         "description": "payment-app 运行于容器内（集群中为 compose 服务 payment-app 的 3 个副本容器）",
         "evidence_refs": _with_refs(R["rel:app-runs-on-container"],
                                     REPOINT_REASONS["rel:app-runs-on-container"])},
        {"id": "rel:container-deployed-on-host",
         "description": "payment-app 容器部署于 Docker Desktop WSL2 宿主",
         "evidence_refs": _with_refs(R["rel:container-deployed-on-host"],
                                     REPOINT_REASONS["rel:container-deployed-on-host"])},
        {"id": "rel:host-wsl-hosts-container",
         "evidence_refs": _with_refs(R["rel:host-wsl-hosts-container"],
                                     REPOINT_REASONS["rel:host-wsl-hosts-container"])},
        {"id": "rel:app-derives-host",
         "evidence_refs": _with_refs(R["rel:app-derives-host"],
                                     REPOINT_REASONS["rel:app-derives-host"])},
    ]

    mappings = [
        {"id": "map:app-container",
         "database_source": "docker:compose-service/payment-app",
         "semantic_filter": "label com.docker.compose.service=payment-app（3 个副本）",
         "validation": "docker ps --filter label=com.docker.compose.service=payment-app 输出 3 行",
         "evidence_refs": _with_refs(M["map:app-container"], REPOINT_REASONS["map:app-container"],
                                     replace=True)},
        {"id": "map:app-ip",
         "semantic_filter": ("containerIP —— 单实例时代观测值 172.18.0.3 已失效："
                             "集群中 3 个副本各有 IP，且随容器重建变化，不应写死")},
    ]
    assert all(e in E for refs in REPOINT_REASONS.values() for e in refs), "改指的证据必须已存在"
    return {"terms": terms, "mappings": mappings, "relations": relations,
            "constraints": [], "evidence": evidence}


def describe(parent_eval: Dict[str, Any], cand_eval: Dict[str, Any]) -> Dict[str, Any]:
    """本轮的假设 / 版本说明 / 局限 —— 数字全部取自成对评估结果，不手写。"""
    p_bad, c_bad = parent_eval["zero_credit"], cand_eval["zero_credit"]
    n = len(cand_eval["scores"])
    reobs = cand_eval["check"]["needs_reobservation"]
    hypothesis = (
        "ontology_v6 有 %d 条记录点名了声明环境（rca-agent/docker-compose.yml）之外的实体，"
        "主要是单实例时代的容器 cc-credit-card-app —— 包括接地映射 map:app-container，"
        "Agent 照着它查只会得到空结果；而没有任何检查会因此变红。"
        "假设：把 4 条单实例时代的证据标为 historical（不改写观测内容），"
        "修正描述当前状态的术语/映射/关系，只在集群时代证据确实观测到同一事实时改指引用后，"
        "证据时效违反记录由 %d/%d 降到 %d/%d，且引擎 top1 不下降（回归观察量）。"
        % (p_bad, p_bad, n, c_bad, n))
    summary = (
        "证据时效版本：新增 tools/ontology_freshness.py 并接入卫生闸门 —— 描述当前状态的记录"
        "不得点名声明环境之外的实体（F1），非历史证据同样不得（F2），历史证据必须写明观测环境与原因（F2b）。"
        "本轮把 ev-docker-ps / ev-app-topology / ev-db-probe / ev:rca-inc_1790868998 标为 historical，"
        "修正 env:container 名称与定义、app:payment-app 与 db:mysql-cluster 的别名、map:app-container 的接地条件、"
        "两条关系描述，并修正 map:app-ip 里写死的旧容器 IP。")
    limitations = [
        "时效检查只认**名字**（容器名、URL 主机名），认不出过期的字面量：map:app-ip 写死的 IP 就是靠人工审计发现的。",
        "同属单实例时代、但点名的实体在集群里仍然存在的证据（如 ev-mysql-vars 经服务名 mysql 观测）不会被标记。",
        "仍有 %d 条记录只靠历史证据支撑（%s）。本轮刻意不给它们硬配证据，"
        "它们以告警形式保留，需要集群在线时重新观测后补新证据。"
        % (len(reobs), ", ".join(r["id"] for r in reobs)),
    ]
    return {"hypothesis": hypothesis, "summary": summary, "limitations": limitations}
