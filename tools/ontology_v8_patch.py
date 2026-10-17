# -*- coding: utf-8 -*-
"""
第 8 轮本体迭代的候选 patch：**类型契约**（关系有谓词、术语有类，取值都来自 TTL）。

## 为什么需要这一轮

运行时关系的谓词只写在自由文本 `connection_condition` 里，引擎靠前缀匹配
（`cond.startswith("attributedto")`）识别；属性链与等价关系是散文；术语没有类。
于是 TTL 声明的 rdfs:domain / rdfs:range 从来没有被检查过 —— 形式化本体里最有用的那部分没有生效。

## 这一轮做什么

1. 87 条关系补 `predicate`：从 `connection_condition` 的前缀机械提取，取值必须在 TTL 词表里（T1）。
   4 条无法机械提取或提取结果有问题的，逐条见 `PREDICATE_OVERRIDES` 的理由。
2. 56 个术语补 `class` 与 `class_source`：别名对应到 TTL 个体的，**直接取该个体的 rdf:type**（T2b 保证二者一致）；
   其余（演化中派生的 rc:/metric: 等）按前缀规则赋类。
3. 不改任何端点、关键词、名称 —— 本轮只补类型，不改图结构；引擎 top1 作为回归观察量。

domain/range 违反**不在本轮修**：本轮如实按记录自称的谓词定类型，违反由第 3 项的 SHACL 门禁统一发现、
另起一轮修正。先修再查会让检查器看起来"一上来就全绿"，测不出它的价值。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from tools.ontology_typing import predicates, ttl_class_of, vocabulary

ROOT = Path(__file__).resolve().parent.parent
PARENT = "ontology_v7"

#: 前缀提取不出、或提取结果与记录端点不符的关系 —— 逐条写明理由。
PREDICATE_OVERRIDES: Dict[str, Dict[str, str]] = {
    "rel:host-wsl-equiv-phys": {
        "predicate": "sameAs",
        "why": "relation_type=equivalence：WSL2 虚拟宿主与物理主机同指一台机器，即 owl:sameAs",
    },
    "rel:instance-equiv-container": {
        "predicate": "sameAs",
        "why": ("relation_type=equivalence，按记录自称定为 owl:sameAs。注意它与 "
                "rel:instance-1-runs-on-container（runsOn）重复且语义更强：副本与容器并非同一个体 —— "
                "已列入局限，不在类型轮里改语义"),
    },
    "rel:app-derives-host": {
        "predicate": "runsOnHost",
        "why": "条件写的是 `runsOn o deployedOn`：TTL 已把它声明为属性链 :runsOnHost，T3 会验证两段链真实存在",
    },
    "rel:derive-client-to-cluster": {
        "predicate": "routesTo",
        "why": ("条件写的是 `routesTo o hasInstance`，但记录的端点是 网关 → 集群（一跳 routesTo），"
                "与它自称的链不符 —— 若按链定类型，T3 会判它不成立。按端点定为 routesTo"),
    },
}

#: 没有对应 TTL 个体的术语：按 id 前缀赋类；少数前缀不足以判定的逐条指定。
PREFIX_CLASS = {"rc": "RootCause", "metric": "Metric"}
TERM_CLASS_OVERRIDES = {
    "env:cluster": ("Cluster", "与 TTL 个体 :DockerCluster 同指 Docker 运行时引擎（:DockerCluster a :Cluster）"),
    "db:mysql-replica": ("DatabaseNode", "只读副本节点组，TTL 个体 :MysqlReplica1/2 的类型均为 :DatabaseNode"),
}

COND_PREFIX = re.compile(r"^\s*([A-Za-z]+)\s*:")


def _parent_records() -> Dict[str, List[dict]]:
    vdir = ROOT / ".evoontology" / "versions" / PARENT
    out = {}
    for fam in ("terms", "relations"):
        data = json.loads((vdir / ("%s.json" % fam)).read_text(encoding="utf-8"))
        out[fam] = data if isinstance(data, list) else data.get("items", data)
    return out


def predicate_for(relation: Dict[str, Any]) -> Dict[str, str]:
    rid = relation["id"]
    if rid in PREDICATE_OVERRIDES:
        o = PREDICATE_OVERRIDES[rid]
        return {"predicate": o["predicate"], "predicate_source": "override", "typing_note": o["why"]}
    m = COND_PREFIX.match(str(relation.get("connection_condition") or ""))
    if not m or m.group(1) not in predicates():
        raise ValueError("%s 的谓词无法从 connection_condition 提取，需要在 PREDICATE_OVERRIDES 里逐条说明" % rid)
    return {"predicate": m.group(1), "predicate_source": "connection_condition"}


def class_for(term: Dict[str, Any]) -> Dict[str, str]:
    anchor = ttl_class_of(term)
    if anchor:
        return {"class": anchor[1], "class_source": "ttl-individual:%s" % anchor[0]}
    if term["id"] in TERM_CLASS_OVERRIDES:
        cls, why = TERM_CLASS_OVERRIDES[term["id"]]
        return {"class": cls, "class_source": "assigned", "typing_note": why}
    prefix = term["id"].split(":", 1)[0]
    if prefix not in PREFIX_CLASS:
        raise ValueError("%s 没有对应的 TTL 个体，也没有前缀规则；需要逐条指定" % term["id"])
    return {"class": PREFIX_CLASS[prefix], "class_source": "prefix:%s" % prefix}


def build_patch(base_term_ids: Optional[Set[str]] = None,
                base_evidence_ids: Optional[Set[str]] = None,
                base_mapping_ids: Optional[Set[str]] = None
                ) -> Dict[str, List[Dict[str, Any]]]:
    p = _parent_records()
    assert all(v["predicate"] in predicates() for v in PREDICATE_OVERRIDES.values())
    assert all(c in vocabulary()["classes"] for c, _ in TERM_CLASS_OVERRIDES.values())
    return {
        "terms": [{"id": t["id"], **class_for(t)} for t in p["terms"]],
        "relations": [{"id": r["id"], **predicate_for(r)} for r in p["relations"]],
        "mappings": [], "constraints": [], "evidence": [],
    }


def describe(parent_eval: Dict[str, Any], cand_eval: Dict[str, Any]) -> Dict[str, Any]:
    n = len(cand_eval["scores"])
    hypothesis = (
        "运行时关系的谓词只写在自由文本 connection_condition 里，引擎靠字符串前缀识别；"
        "属性链与等价关系是散文；术语没有类 —— TTL 的 rdfs:domain/range 因此从未被检查。"
        "假设：给全部关系补上取自 TTL 词表的 predicate、给全部术语补上 class（有 TTL 个体的取其 rdf:type），"
        "并把 runsOn o deployedOn 声明为可验证的属性链后，类型契约由 %d/%d 提升到 %d/%d，"
        "且引擎 top1 不下降（本轮不改任何端点、关键词与名称）。"
        % (parent_eval["full_credit"], n, cand_eval["full_credit"], n))
    patch = build_patch()
    rels, terms = patch["relations"], patch["terms"]
    from_cond = sum(1 for r in rels if r["predicate_source"] == "connection_condition")
    from_ttl = sum(1 for t in terms if t["class_source"].startswith("ttl-individual:"))
    summary = (
        "类型契约版本：%d 条关系补 predicate（%d 条从 connection_condition 前缀提取，%d 条逐条说明），"
        "%d 个术语补 class（%d 个取自 TTL 个体的 rdf:type，其余按前缀规则或逐条指定）。"
        % (len(rels), from_cond, len(rels) - from_cond, len(terms), from_ttl) +
        "TTL 补声明 :exposes / :hasComponent，并把散文写的 `runsOn o deployedOn` 声明为属性链 :runsOnHost。"
        "引擎改为优先读 predicate 字段，不再对 connection_condition 做字符串前缀匹配（缺字段时回退旧逻辑）。")
    limitations = [
        "本轮如实按记录自称的谓词定类型，domain/range 违反留给 SHACL 门禁发现并另起一轮修正。",
        "rel:instance-equiv-container 声称副本 owl:sameAs 容器 —— 两者并非同一个体，"
        "且与已有的 runsOn 关系重复；SHACL 不检查 sameAs，需要人工评审后改语义。",
        "rel:derive-client-to-cluster 的条件文本仍写着 routesTo o hasInstance，与端点不符；"
        "本轮按端点定为 routesTo，没有改它的端点（那会改变图结构，应单独评估）。",
    ]
    return {"hypothesis": hypothesis, "summary": summary, "limitations": limitations}
