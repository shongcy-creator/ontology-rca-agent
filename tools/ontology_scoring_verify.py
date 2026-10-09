#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本体驱动打分的离线 A/B 验证（不注入故障、不走 HTTP、不受后端限流影响）。

回答一个问题：**"把打分词典从代码内置改成从激活本体派生"到底改变了什么？**

做法：对同一批 21 个故障场景的告警文本，分别在两个本体版本上离线复算
确定性引擎的候选集与路由判定：

  · 父版本  `ontology_v0-rca-agent`（18 Term / 3 Constraint，单实例视图）
  · 当前版本（默认取 active.json，即迭代后的 `ontology_v1`，51 Term / 16 Constraint）

对比指标：
  · top1 / topK 命中率（候选根因是否命中场景期望的本体 Term）
  · 命中的候选根因分布（是否还全是 `rc:slow-sql`）
  · 路由判定分布（deterministic / agentic），验证"快路径过度自信"是否被修掉
  · 打分词典来源（ontology vs legacy 兜底）

用法：
  python tools/ontology_scoring_verify.py
  python tools/ontology_scoring_verify.py --parent ontology_v0-rca-agent --active ontology_v1
  python tools/ontology_scoring_verify.py --json-out .chaos/ontology_scoring_ab.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rca-agent"))            # backend 包
sys.path.insert(0, str(ROOT / "vendor" / "EvoOntology"))

from tools.chaos import FAULTS                                  # noqa: E402
from tools.ontology_cluster_patch import SCENARIO_EXPECTED_TERMS  # noqa: E402

WORKSPACE = ROOT / ".evoontology"
PARENT_DEFAULT = "ontology_v0-rca-agent"


# ── 离线拓扑构建（与 EvoOntologyClient._topology_bfs 同构，但直接读文件）─────

def build_topology(records: Dict[str, List[dict]], app_name: str = "payment-app",
                   max_nodes: int = 80, max_depth: int = 4) -> Dict[str, Any]:
    terms = {t["id"]: t for t in records.get("terms") or [] if t.get("id")}
    adj: Dict[str, List[dict]] = {}
    for r in records.get("relations") or []:
        s, t = r.get("source"), r.get("target")
        if s and t:
            adj.setdefault(s, []).append(r)
            adj.setdefault(t, []).append(r)

    seeds = [tid for tid, t in terms.items()
             if app_name.lower() in tid.lower()
             or app_name.lower() in str(t.get("name", "")).lower()][:5]

    seen: Dict[str, Dict[str, Any]] = {}
    edges: Dict[str, Dict[str, Any]] = {}
    visited: Dict[str, int] = {}
    queue = [(s, 0) for s in seeds]
    while queue:
        tid, depth = queue.pop(0)
        if tid in seen or depth > max_depth or len(seen) >= max_nodes:
            continue
        t = terms.get(tid, {})
        seen[tid] = {"id": tid, "name": t.get("name", tid), "type": t.get("type", "entity"),
                     "scope": t.get("scope", ""), "definition": t.get("definition", "")}
        for r in adj.get(tid, []):
            rid, s, tg = r.get("id"), r.get("source"), r.get("target")
            if not rid or not tg:
                continue
            edges.setdefault(rid, {"id": rid, "source": s, "target": tg,
                                   "relation_type": r.get("relation_type", ""),
                                   "condition": r.get("connection_condition", "")})
            other = tg if s == tid else s
            if other and other not in seen and other not in visited:
                visited[other] = depth + 1
                queue.append((other, depth + 1))

    node_ids = set(seen)
    final_edges = [e for e in edges.values() if e["source"] in node_ids and e["target"] in node_ids]
    return {"nodes": list(seen.values()), "edges": final_edges}


# ── 单版本评估 ─────────────────────────────────────────────────────────────

def evaluate_records(records: Dict[str, List[dict]], scoring: Any, top_k: int = 5,
                     legacy: bool = False, label: str = "") -> Dict[str, Any]:
    """
    用给定的 5 族记录 + 打分索引评估 21 个场景。

    抽成独立函数是为了让本体演化工具能评估**尚未落盘的候选版本**
    （`OntologyScoring.from_records(candidate_records)`）。
    """
    from backend.services.rca_engine import RCAEngine
    from backend.services import router as router_mod

    if not legacy and not scoring.available:
        return {"version": label or scoring.version, "available": False,
                "error": scoring.error}

    topo = build_topology(records)

    engine = RCAEngine()
    engine.scoring = scoring

    per_case: List[Dict[str, Any]] = []
    for fid in sorted(FAULTS):
        fault = FAULTS[fid]
        expected = SCENARIO_EXPECTED_TERMS.get(fid) or []
        alert = "%s：%s" % (fault.title, fault.expected_root_cause)
        parsed = engine.parse_alert(alert, "P1")
        cands = engine.score_candidates(topo["nodes"], parsed)
        ids = [c["entity_id"] for c in cands]
        top1 = ids[0] if ids else None

        seed = {"alert": parsed, "root_cause": cands[0] if cands else None,
                "candidates": cands, "confidence": cands[0]["confidence"] if cands else 0.0,
                "thresholds_triggered": []}
        route = router_mod.decide(seed, {"fast_path_confidence": 0.85}, scoring=scoring)

        per_case.append({
            "case_id": fid,
            "layer": fault.layer,
            "expected_terms": expected,
            "alert": alert,
            "top1": top1,
            "candidates": ids,
            "top1_hit": bool(top1 and top1 in expected),
            "topk_hit": any(i in expected for i in ids[:top_k]),
            "scoring_sources": sorted({c.get("scoring_source", "") for c in cands}),
            "route_mode": route.mode,
            "route_reason": route.reason,
            "constraint_hits": route.signals.get("constraint_hits") or [],
            "seed_supported": route.signals.get("seed_supported_by_constraint"),
        })

    n = len(per_case)
    det = [c for c in per_case if c["route_mode"] == "deterministic"]
    agt = [c for c in per_case if c["route_mode"] == "agentic"]
    return {
        "version": label or scoring.version,
        "mode": "legacy-engine" if legacy else "ontology-driven",
        "available": True,
        "terms": len(records["terms"]),
        "constraints": len(records["constraints"]),
        "relations": len(records["relations"]),
        "terms_indexed": len(scoring.index),
        "known_root_causes": sorted(scoring.known_root_causes),
        "category_distribution": scoring.summary()["category_distribution"],
        "topology_nodes": len(topo["nodes"]),
        "topology_edges": len(topo["edges"]),
        "top1_hits": sum(1 for c in per_case if c["top1_hit"]),
        "topk_hits": sum(1 for c in per_case if c["topk_hit"]),
        "total": n,
        "top1_rate": round(sum(1 for c in per_case if c["top1_hit"]) / n, 4) if n else 0.0,
        "topk_rate": round(sum(1 for c in per_case if c["topk_hit"]) / n, 4) if n else 0.0,
        "route_distribution": dict(Counter(c["route_mode"] for c in per_case)),
        # 路由质量：快路径给出的答案对不对？——这是判断"是否过度自信"的核心指标
        "deterministic_cases": len(det),
        "deterministic_top1_hits": sum(1 for c in det if c["top1_hit"]),
        "deterministic_topk_hits": sum(1 for c in det if c["topk_hit"]),
        "deterministic_top1_precision": (round(sum(1 for c in det if c["top1_hit"]) / len(det), 4)
                                         if det else None),
        "agentic_cases": len(agt),
        "agentic_top1_hits": sum(1 for c in agt if c["top1_hit"]),
        "top1_distribution": dict(Counter(c["top1"] or "(none)" for c in per_case)),
        "constraint_matched_cases": sum(1 for c in per_case if c["constraint_hits"]),
        "per_case": per_case,
    }


def evaluate_version(version: str, top_k: int = 5,
                     legacy: bool = False) -> Dict[str, Any]:
    """
    离线评估某个本体版本下的确定性引擎表现。

    legacy=True 时禁用本体词典，走原来的硬编码表 —— 用于给出"旧引擎"参照线。
    """
    from backend.services.ontology_scoring import OntologyScoring

    scoring = OntologyScoring.for_version(str(WORKSPACE), version)
    if not scoring.available:
        return {"version": version, "available": False, "error": scoring.error}

    records = {}
    for fam in ("terms", "mappings", "relations", "constraints", "evidence"):
        p = WORKSPACE / "versions" / version / ("%s.json" % fam)
        records[fam] = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else []

    if legacy:
        class _Unavailable:
            available = False
            index: Dict[str, Any] = {}
            version = ""
            error = "legacy mode"
            known_root_causes: Set[str] = set()

            @staticmethod
            def summary() -> Dict[str, Any]:
                return {"category_distribution": {}}
        scoring = _Unavailable()      # type: ignore[assignment]

    return evaluate_records(records, scoring, top_k=top_k, legacy=legacy, label=version)


def _row(c: Dict[str, Any], tag: str) -> str:
    return "  %-22s %-6s top1=%-22s hit=%-5s route=%-14s" % (
        c["case_id"], tag, str(c["top1"]), c["topk_hit"], c["route_mode"])


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="本体驱动打分的离线 A/B 验证")
    ap.add_argument("--parent", default=PARENT_DEFAULT, help="父版本（迭代前）")
    ap.add_argument("--mid", default="", help="中间版本（第 1 轮迭代后，可选）")
    ap.add_argument("--active", default="", help="对比版本（默认取 active.json）")
    ap.add_argument("--top-k", type=int, default=5, dest="top_k")
    ap.add_argument("--json-out", default=str(ROOT / ".chaos" / "ontology_scoring_ab.json"))
    args = ap.parse_args(argv)

    active = args.active
    if not active:
        active = json.loads((WORKSPACE / "active.json").read_text(encoding="utf-8")).get(
            "active_version") or ""

    print("=" * 100)
    print("本体驱动打分 A/B：%s  →  %s" % (args.parent, active))
    print("=" * 100)

    legacy = evaluate_version(args.parent, args.top_k, legacy=True)
    parent = evaluate_version(args.parent, args.top_k)
    mid = evaluate_version(args.mid, args.top_k) if args.mid else None
    current = evaluate_version(active, args.top_k)

    rows = [("① 旧引擎 + 父本体（参照线）", legacy),
            ("② 新引擎 + 父本体（只换词典来源）", parent)]
    if mid is not None:
        rows.append(("③ 新引擎 + 第 1 轮本体（集群知识）", mid))
        rows.append(("④ 新引擎 + 第 2 轮本体（消歧 + 角色）", current))
    else:
        rows.append(("③ 新引擎 + 迭代后本体（闭环）", current))

    for tag, r in rows:
        if not r.get("available"):
            print("  [%s] %s 不可用: %s" % (tag, r.get("version"), r.get("error")))
            continue
        print("\n[%s] %s" % (tag, r["version"]))
        print("     terms=%d constraints=%d relations=%d 拓扑=%d节点/%d边"
              % (r["terms"], r["constraints"], r["relations"],
                 r["topology_nodes"], r["topology_edges"]))
        print("     已知根因（本体派生）: %s"
              % (", ".join(r["known_root_causes"]) or "(本体不可用)"))
        print("     类别分布: %s" % json.dumps(r["category_distribution"], ensure_ascii=False))
        print("     top1 命中 %d/%d (%.0f%%)   top%d 命中 %d/%d (%.0f%%)   约束命中场景 %d/%d"
              % (r["top1_hits"], r["total"], r["top1_rate"] * 100,
                 args.top_k, r["topk_hits"], r["total"], r["topk_rate"] * 100,
                 r["constraint_matched_cases"], r["total"]))
        print("     路由分布: %s" % json.dumps(r["route_distribution"], ensure_ascii=False))
        print("     快路径质量: deterministic %d 例，其中 top1 命中 %d 例（精确率 %s）"
              % (r["deterministic_cases"], r["deterministic_top1_hits"],
                 "n/a" if r["deterministic_top1_precision"] is None
                 else "%.0f%%" % (r["deterministic_top1_precision"] * 100)))
        print("     top1 根因分布: %s"
              % json.dumps(dict(sorted(r["top1_distribution"].items(),
                                       key=lambda kv: -kv[1])), ensure_ascii=False))

    if parent.get("available") and current.get("available"):
        print("\n" + "-" * 100)
        print("逐场景对比（top%d 是否命中期望本体 Term）" % args.top_k)
        print("-" * 100)
        pmap = {c["case_id"]: c for c in parent["per_case"]}
        improved, regressed, same_hit = [], [], []
        for c in current["per_case"]:
            p = pmap.get(c["case_id"], {})
            mark = ""
            if c["topk_hit"] and not p.get("topk_hit"):
                mark = "  ★ 新命中"
                improved.append(c["case_id"])
            elif p.get("topk_hit") and not c["topk_hit"]:
                mark = "  ⚠ 回退"
                regressed.append(c["case_id"])
            elif c["topk_hit"]:
                same_hit.append(c["case_id"])
            print("  %-22s ②:%-24s ③:%-24s 路由 %-14s%s"
                  % (c["case_id"], str(p.get("top1")), str(c["top1"]),
                     c["route_mode"], mark))
        print("-" * 100)
        print("  新命中 %d 个: %s" % (len(improved), ", ".join(improved) or "-"))
        print("  回退   %d 个: %s" % (len(regressed), ", ".join(regressed) or "-"))
        print("  两者都命中 %d 个: %s" % (len(same_hit), ", ".join(same_hit) or "-"))
        print("  top1: ① %d  →  ② %d%s  →  %s %d"
              % (legacy["top1_hits"], parent["top1_hits"],
                 ("  →  ③ %d" % mid["top1_hits"]) if mid is not None else "",
                 "④" if mid is not None else "③", current["top1_hits"]))
        print("  top%d: ① %d  →  ② %d%s  →  %s %d"
              % (args.top_k, legacy["topk_hits"], parent["topk_hits"],
                 ("  →  ③ %d" % mid["topk_hits"]) if mid is not None else "",
                 "④" if mid is not None else "③", current["topk_hits"]))
        seq = [("①", legacy), ("②", parent)]
        if mid is not None:
            seq.append(("③", mid))
        seq.append(("④" if mid is not None else "③", current))
        for tag, r in seq:
            rd = r["route_distribution"]
            print("  路由%s deterministic=%-3d agentic=%-3d 快路径 top1 精确率=%s"
                  % (tag, rd.get("deterministic", 0), rd.get("agentic", 0),
                     "n/a" if r["deterministic_top1_precision"] is None
                     else "%.0f%%" % (r["deterministic_top1_precision"] * 100)))

    out = {"legacy_engine": legacy, "new_engine_parent_ontology": parent,
           "new_engine_mid_ontology": mid,
           "new_engine_current_ontology": current, "top_k": args.top_k}
    p = Path(args.json_out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\n报告: %s" % p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
