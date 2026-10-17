#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本体 SHACL 门禁 —— 把运行时 5 族记录投影成 RDF，用 SHACL 形状校验。

## 为什么是 SHACL 而不是 OWL 推理

本项目的门禁要求"结构上可以变红"：SHACL 是**闭世界**的形状校验，缺一条边就是缺；
OWL 是开世界的，缺一条边只意味着"不知道"，推理机不会因此报错。
domain / range 在 OWL 里是**推理规则**（满足 domain 的主语会被推断为该类），
而不是约束 —— 用 OWL 推理机"检查" domain 永远不会失败。要把它当约束用，就得用 SHACL。

## 形状来源

- `ontology_turtle/shapes.ttl`：手写的结构形状（枚举取值、跨族引用、缺证据告警）。
- domain / range 形状：运行时从 `ontology.ttl` 的 rdfs:domain / rdfs:range **生成**，
  TTL 仍是唯一事实源 —— 改了 TTL 的 domain，形状自动跟着变，不会出现两份口径。
  `sh:class` 会沿 rdfs:subClassOf 闭包判定，所以 ontology.ttl 的类层次被一并并入数据图。

## 失败即关闭

pyshacl 不可用时**判为违反**，而不是跳过：静默跳过的门禁看起来是绿的，其实什么都没测。

用法：
  python tools/ontology_shacl.py                       # 校验激活版本
  python tools/ontology_shacl.py --version ontology_v8
"""
from __future__ import annotations

import argparse
import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote

import rdflib
from rdflib import OWL, RDF, Literal, Namespace, URIRef
from rdflib.collection import Collection

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.ontology_typing import NS as ONT, SAME_AS, TTL_DIR, local, vocabulary  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")   # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

WORKSPACE = ROOT / ".evoontology"
SHAPES_FILE = TTL_DIR / "shapes.ttl"
RT_BASE = "https://sapiens.ai/ontology/credit-card-ops/runtime/"
RT = Namespace(RT_BASE + "vocab#")
SH = Namespace("http://www.w3.org/ns/shacl#")
FAMILIES = ("terms", "mappings", "relations", "constraints", "evidence")


def node(kind: str, record_id: str) -> URIRef:
    return URIRef("%s%s/%s" % (RT_BASE, kind, quote(record_id, safe="")))


def _add_evidence(g: rdflib.Graph, subject: URIRef, record: Dict[str, Any]) -> None:
    for ref in record.get("evidence_refs") or []:
        g.add((subject, RT.evidence, node("evidence", ref)))


def to_rdf(rec: Dict[str, List[dict]]) -> rdflib.Graph:
    """运行时记录 → RDF。关系同时投影成直接三元组（供 domain/range）与 rt:Relation 节点（供结构检查）。"""
    g = rdflib.Graph()
    g.bind("rt", RT)
    g.bind("ont", ONT)
    for t in rec.get("terms") or []:
        s = node("term", t["id"])
        g.add((s, RDF.type, RT.Term))
        if t.get("class"):
            g.add((s, RDF.type, ONT[t["class"]]))
        state = (t.get("lifecycle") or {}).get("state")
        if state:
            g.add((s, RT.lifecycleState, Literal(state)))
        _add_evidence(g, s, t)
    for r in rec.get("relations") or []:
        s, o = node("term", r["source"]), node("term", r["target"])
        pred = r.get("predicate")
        if pred:
            g.add((s, OWL.sameAs if pred == SAME_AS else ONT[pred], o))
        rn = node("relation", r["id"])
        g.add((rn, RDF.type, RT.Relation))
        g.add((rn, RT.source, s))
        g.add((rn, RT.target, o))
        _add_evidence(g, rn, r)
    mapping_ids = {m["id"] for m in rec.get("mappings") or []}
    for m in rec.get("mappings") or []:
        mn = node("mapping", m["id"])
        g.add((mn, RDF.type, RT.Mapping))
        g.add((mn, RT["term"], node("term", m.get("term_id") or "")))
        if m.get("confidence"):
            g.add((mn, RT.confidence, Literal(m["confidence"])))
        _add_evidence(g, mn, m)
    for c in rec.get("constraints") or []:
        cn = node("constraint", c["id"])
        g.add((cn, RDF.type, RT.Constraint))
        tgt = c.get("target") or ""
        g.add((cn, RT.target, node("mapping" if tgt in mapping_ids else "term", tgt)))
        for key, prop in (("severity", RT.severity), ("constraint_type", RT.constraintType),
                          ("confidence", RT.confidence)):
            if c.get(key):
                g.add((cn, prop, Literal(c[key])))
        for kw in c.get("trigger_keywords") or []:
            g.add((cn, RT.triggerKeyword, Literal(kw)))
        _add_evidence(g, cn, c)
    for e in rec.get("evidence") or []:
        en = node("evidence", e["id"])
        g.add((en, RDF.type, RT.Evidence))
        if e.get("source"):
            g.add((en, RT.sourceText, Literal(str(e["source"]))))
    return g


@lru_cache(maxsize=1)
def class_hierarchy() -> rdflib.Graph:
    g = rdflib.Graph()
    g.parse(TTL_DIR / "ontology.ttl", format="turtle")
    out = rdflib.Graph()
    for s, p, o in g.triples((None, rdflib.RDFS.subClassOf, None)):
        out.add((s, p, o))
    return out


@lru_cache(maxsize=1)
def shapes_graph() -> rdflib.Graph:
    """手写形状 + 从 TTL 生成的 domain/range 形状。"""
    g = rdflib.Graph()
    g.parse(SHAPES_FILE, format="turtle")
    for prop, dr in sorted(vocabulary()["properties"].items()):
        for side, target_kind in (("domain", SH.targetSubjectsOf), ("range", SH.targetObjectsOf)):
            classes = dr.get(side) or ()
            if not classes:
                continue
            shape = URIRef("%sshape/%s/%s" % (RT_BASE, side, prop))
            g.add((shape, RDF.type, SH.NodeShape))
            g.add((shape, target_kind, ONT[prop]))
            if len(classes) == 1:
                g.add((shape, SH["class"], ONT[classes[0]]))
            else:                               # owl:unionOf → sh:or
                alts = []
                for cls in classes:
                    alt = rdflib.BNode()
                    g.add((alt, SH["class"], ONT[cls]))
                    alts.append(alt)
                head = rdflib.BNode()
                Collection(g, head, alts)
                g.add((shape, SH["or"], head))
            g.add((shape, SH.message, Literal("%s 的 %s 是 %s" % (prop, side, " ∪ ".join(classes)))))
    return g


def _shape_name(shape: Any) -> str:
    """生成的 domain/range 形状 → "domain/runsOn"；手写形状的嵌套属性形状 → 其父 NodeShape 名。"""
    s = str(shape)
    if "/shape/" in s:
        return s.split("/shape/")[1]
    if isinstance(shape, rdflib.BNode):
        parent = next(iter(shapes_graph().subjects(SH.property, shape)), None)
        return local(parent) if parent is not None else "-"
    return local(shape)


def _record_of(focus: Any) -> str:
    s = str(focus)
    if s.startswith(RT_BASE):
        kind, _, rid = s[len(RT_BASE):].partition("/")
        return "%s:%s" % (kind, unquote(rid))
    return s


def _relations_behind(rec: Dict[str, List[dict]], shape: str, focus: str) -> List[str]:
    """domain/range 违反的焦点是术语；反查是哪条关系造成的，报告才可操作。"""
    if "/shape/" not in shape:
        return []
    side, prop = shape.split("/shape/")[1].split("/")
    term_id = focus.split(":", 1)[1] if focus.startswith("term:") else ""
    end = "source" if side == "domain" else "target"
    return [r["id"] for r in rec.get("relations") or []
            if r.get("predicate") == prop and r.get(end) == term_id]


def validate_records(rec: Dict[str, List[dict]]) -> Dict[str, Any]:
    try:
        from pyshacl import validate
    except ImportError as e:
        return {"available": False, "conforms": False,
                "violations": [{"shape": "pyshacl", "focus": "-", "path": "", "value": "",
                                "relations": [], "message":
                                "pyshacl 不可用（%s）—— SHACL 门禁失败即关闭，不静默跳过" % e}],
                "warnings": []}
    data = to_rdf(rec) + class_hierarchy()
    conforms, report, _ = validate(data, shacl_graph=shapes_graph(), inference="none",
                                   allow_warnings=True)
    violations, warnings = [], []
    for res in report.subjects(RDF.type, SH.ValidationResult):
        sev = report.value(res, SH.resultSeverity)
        shape = report.value(res, SH.sourceShape)
        focus = _record_of(report.value(res, SH.focusNode))
        item = {"shape": _shape_name(shape),
                "focus": focus,
                "path": local(report.value(res, SH.resultPath)) if report.value(res, SH.resultPath) else "",
                "value": _record_of(report.value(res, SH.value)) if report.value(res, SH.value) else "",
                "message": str(report.value(res, SH.resultMessage) or "")}
        item["relations"] = _relations_behind(rec, str(shape), focus)
        (warnings if sev == SH.Warning else violations).append(item)
    key = lambda x: (x["shape"], x["focus"], x["path"])  # noqa: E731
    return {"available": True, "conforms": not violations,
            "violations": sorted(violations, key=key), "warnings": sorted(warnings, key=key)}


def record_scores(rec: Dict[str, List[dict]]) -> Dict[str, Any]:
    """逐记录 0/1：被任何 sh:Violation 牵连即 0（domain/range 违反算到造成它的那条关系上）。"""
    res = validate_records(rec)
    bad = set()
    for v in res["violations"]:
        bad.update("relation:%s" % r for r in v["relations"])
        if not v["relations"]:
            bad.add(v["focus"])
    kinds = {"terms": "term", "mappings": "mapping", "relations": "relation",
             "constraints": "constraint", "evidence": "evidence"}
    case_ids = ["%s:%s" % (kinds[f], r["id"]) for f in FAMILIES for r in rec.get(f) or []]
    return {"case_ids": case_ids, "scores": [0.0 if c in bad else 1.0 for c in case_ids], "shacl": res}


def _load(version: str) -> Dict[str, List[dict]]:
    vdir = WORKSPACE / "versions" / version
    out = {}
    for fam in FAMILIES:
        data = json.loads((vdir / ("%s.json" % fam)).read_text(encoding="utf-8"))
        out[fam] = data if isinstance(data, list) else data.get("items", data)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="本体 SHACL 门禁")
    ap.add_argument("--version", default="")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args(argv)
    version = args.version or json.loads(
        (WORKSPACE / "active.json").read_text(encoding="utf-8"))["active_version"]
    res = validate_records(_load(version))
    print("SHACL 校验 %s：违反 %d 条，告警 %d 条" % (version, len(res["violations"]), len(res["warnings"])))
    for label, items in (("违反", res["violations"]), ("告警", res["warnings"])):
        for v in items:
            print("  [%s] %-24s %-34s %s%s" % (label, v["shape"], v["focus"], v["message"],
                                              "  ← %s" % ", ".join(v["relations"]) if v["relations"] else ""))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if res["conforms"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
