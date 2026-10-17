# -*- coding: utf-8 -*-
"""
本体类型契约（typing contract）—— 运行时 5 族记录里的谓词与类，必须来自 TTL 声明的词表。

## 为什么需要

运行时关系的谓词原先只活在自由文本 `connection_condition` 里（如 `"runsOn: 应用部署运行在容器内"`），
引擎靠 `cond.startswith("attributedto")` 这种前缀匹配识别它；OWL2 的属性链写成了散文
（`"runsOn o deployedOn"`），等价关系是一句中文。运行时术语也没有类（只有 entity/metric 两种 type），
所以 TTL 里 94 个 rdfs:domain 和 97 个 rdfs:range 从来没被检查过。

## 契约（逐条记录）

| 规则 | 内容 |
|---|---|
| T1 | 每条关系有 `predicate`，且是 TTL 声明的 owl:ObjectProperty（或 `sameAs` = owl:sameAs） |
| T2 | 每个术语有 `class`，且是 TTL 声明的 owl:Class |
| T2b | 术语若通过别名对应到 TTL 个体，`class` 必须等于该个体的 rdf:type —— TTL 仍是类型的事实源 |
| T3 | 谓词若在 TTL 里声明了 owl:propertyChainAxiom，该关系的端点之间必须真的存在这条链 |

domain/range 由 SHACL 形状检查（tools/ontology_shacl.py），形状从 TTL 的 rdfs:domain/range 生成。
"""
from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

import rdflib
from rdflib import OWL, RDF, RDFS
from rdflib.collection import Collection

ROOT = Path(__file__).resolve().parent.parent
TTL_DIR = ROOT / "ontology_turtle"
TTL_FILES = ("ontology.ttl", "instances.ttl", "topology.ttl")
NS = rdflib.Namespace("https://sapiens.ai/ontology/credit-card-ops#")
SAME_AS = "sameAs"


def local(uri: Any) -> str:
    return str(uri).split("#")[-1]


@lru_cache(maxsize=1)
def ttl_graph() -> rdflib.Graph:
    g = rdflib.Graph()
    for f in TTL_FILES:
        g.parse(TTL_DIR / f, format="turtle")
    return g


def _class_set(g: rdflib.Graph, node: Any) -> Tuple[str, ...]:
    """rdfs:domain / rdfs:range 的取值：具名类，或 owl:unionOf 并集。"""
    if node is None:
        return ()
    union = g.value(node, OWL.unionOf)
    if union is not None:
        return tuple(local(x) for x in Collection(g, union))
    return (local(node),)


@lru_cache(maxsize=1)
def vocabulary() -> Dict[str, Any]:
    """TTL 词表：类、对象属性（含 domain/range）、属性链、个体类型。"""
    g = ttl_graph()
    classes = frozenset(local(c) for c in g.subjects(RDF.type, OWL.Class))
    props: Dict[str, Dict[str, Tuple[str, ...]]] = {}
    chains: Dict[str, Tuple[str, ...]] = {}
    for p in g.subjects(RDF.type, OWL.ObjectProperty):
        props[local(p)] = {"domain": _class_set(g, g.value(p, RDFS.domain)),
                           "range": _class_set(g, g.value(p, RDFS.range))}
        head = g.value(p, OWL.propertyChainAxiom)
        if head is not None:
            chains[local(p)] = tuple(local(x) for x in Collection(g, head))
    individuals = {local(s): local(o) for s, o in g.subject_objects(RDF.type)
                   if local(o) in classes}
    return {"classes": classes, "properties": props, "chains": chains,
            "individuals": individuals}


def predicates() -> FrozenSet[str]:
    return frozenset(vocabulary()["properties"]) | {SAME_AS}


def ttl_class_of(term: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """若术语的某个别名是 TTL 个体名，返回 (个体名, 其 rdf:type)。"""
    inds = vocabulary()["individuals"]
    for alias in term.get("aliases") or []:
        if alias in inds:
            return alias, inds[alias]
    return None


def _chain_holds(edges: Dict[str, Dict[str, set]], src: str, dst: str,
                 chain: Tuple[str, ...]) -> bool:
    frontier = {src}
    for pred in chain:
        frontier = {t for s in frontier for t in edges[pred].get(s, ())}
        if not frontier:
            return False
    return dst in frontier


def check_typing(rec: Dict[str, List[dict]]) -> Dict[str, Any]:
    voc = vocabulary()
    preds = predicates()
    problems: List[Dict[str, str]] = []

    for t in rec.get("terms") or []:
        cls = t.get("class")
        if not cls:
            problems.append({"rule": "T2", "id": t["id"], "detail": "缺少 class"})
            continue
        if cls not in voc["classes"]:
            problems.append({"rule": "T2", "id": t["id"], "detail": "class `%s` 不是 TTL 声明的类" % cls})
        anchor = ttl_class_of(t)
        if anchor and anchor[1] != cls:
            problems.append({"rule": "T2b", "id": t["id"],
                             "detail": "TTL 个体 %s 的类型是 %s，记录写的是 %s" % (anchor[0], anchor[1], cls)})

    edges: Dict[str, Dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for r in rec.get("relations") or []:
        p = r.get("predicate")
        if p:
            edges[p][r["source"]].add(r["target"])
    for r in rec.get("relations") or []:
        p = r.get("predicate")
        if not p:
            problems.append({"rule": "T1", "id": r["id"], "detail": "缺少 predicate"})
            continue
        if p not in preds:
            problems.append({"rule": "T1", "id": r["id"], "detail": "predicate `%s` 不在 TTL 词表里" % p})
            continue
        chain = voc["chains"].get(p)
        if chain and not _chain_holds(edges, r["source"], r["target"], chain):
            problems.append({"rule": "T3", "id": r["id"],
                             "detail": "%s 声明为 %s，但 %s → %s 之间不存在这条链"
                                       % (p, " o ".join(chain), r["source"], r["target"])})

    bad = {x["id"] for x in problems}
    case_ids = ["term:%s" % t["id"] for t in rec.get("terms") or []] + \
               ["relation:%s" % r["id"] for r in rec.get("relations") or []]
    scores = [0.0 if c.split(":", 1)[1] in bad else 1.0 for c in case_ids]
    return {"passed": not problems, "problems": problems, "case_ids": case_ids, "scores": scores}
