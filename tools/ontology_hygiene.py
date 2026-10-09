#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本体卫生检查（ontology hygiene）—— 找出"游离/不可达/无证据"的术语，并量化其影响。

## 为什么需要这个工具

本体的规模不等于本体的能力。第 1 轮集群化迭代后，`ontology_v2` 有 51 个 Term，
但其中一部分是**游离节点**（在关系图里度为 0）：它们既不在拓扑 BFS 的遍历范围里，
也永远不会成为确定性引擎的候选根因 —— 但会出现在 `browse_semantics` 的语义检索结果里，
对 LLM Agent 形成"这个本体认识它"的误导。

实测根因（可复现）：`tools/init_evo_ontology.py`（首版 TTL→5 族转换器）
**只映射了 TTL 对象属性的一个子集**。TTL 里声明了但转换器没映射的属性：

  · `:PayContainer :managedBy :DockerCluster`   → `env:cluster` 游离
  · `:PaymentApp :generatesEvent :DeployEvent`  → `evt:deploy` 游离
  · `:TTxn :hasIndex :IdxTxn*`                  → 索引个体**根本没被建成 Term**

凡是只通过被漏掉的属性与其它个体相连的实体，都会变成游离节点。

## 检查项

  1. **游离术语**：在 relations 里 degree == 0
  2. **不可达术语**：从用户可见入口（app:payment-app）出发的无向 BFS 走不到
     （比"游离"更宽：可能是某个与主图断开的子图）
  3. **无证据术语**：`evidence_refs` 为空 —— 没有可复现观测支撑
  4. **无接地映射**：没有任何 Mapping 指向它 —— Agent 拿不到物理数据源
  5. **生命周期**：`lifecycle.state` 分布（EvoOntology 只在 validated/active 时参与查询）
  6. **影响评估**：游离/不可达术语是否进入引擎候选（用离线拓扑复算交叉验证）；
     以及是否有**期望根因术语**落到其中（那就是真故障，会直接打掉诊断能力）

用法：
  python tools/ontology_hygiene.py                       # 检查当前激活版本
  python tools/ontology_hygiene.py --version ontology_v1
  python tools/ontology_hygiene.py --json-out .chaos/ontology_hygiene.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Set

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rca-agent"))

from tools.ontology_cluster_patch import SCENARIO_EXPECTED_TERMS  # noqa: E402

WORKSPACE = ROOT / ".evoontology"
ENTRY = "app:payment-app"


def _load(version: str) -> Dict[str, List[dict]]:
    rec: Dict[str, List[dict]] = {}
    for fam in ("terms", "mappings", "relations", "constraints", "evidence"):
        p = WORKSPACE / "versions" / version / ("%s.json" % fam)
        rec[fam] = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else []
    return rec


def audit(version: str) -> Dict[str, Any]:
    return audit_records(_load(version), version)


def audit_records(rec: Dict[str, List[dict]], version: str) -> Dict[str, Any]:
    """审计给定的 5 族记录（可来自内存，用于评估尚未落盘的候选版本）。"""
    terms = {t["id"]: t for t in rec["terms"] if t.get("id")}
    adj: Dict[str, Set[str]] = defaultdict(set)
    degree: Dict[str, int] = defaultdict(int)
    for r in rec["relations"]:
        s, t = str(r.get("source") or ""), str(r.get("target") or "")
        if not s or not t:
            continue
        degree[s] += 1
        degree[t] += 1
        adj[s].add(t)
        adj[t].add(s)

    # 无向可达性（从用户可见入口出发）
    reachable: Set[str] = set()
    if ENTRY in terms:
        q = deque([ENTRY])
        reachable.add(ENTRY)
        while q:
            cur = q.popleft()
            for nxt in adj.get(cur, ()):  # type: ignore[arg-type]
                if nxt not in reachable:
                    reachable.add(nxt)
                    q.append(nxt)

    mapped_terms = {str(m.get("term_id") or "") for m in rec["mappings"]}
    expected_terms: Set[str] = set()
    for v in SCENARIO_EXPECTED_TERMS.values():
        expected_terms |= set(v)

    orphans, unreachable, no_evidence, no_mapping = [], [], [], []
    for tid, t in terms.items():
        if degree.get(tid, 0) == 0:
            orphans.append(tid)
        if tid not in reachable:
            unreachable.append(tid)
        if not (t.get("evidence_refs") or []):
            no_evidence.append(tid)
        if tid not in mapped_terms:
            no_mapping.append(tid)

    # 影响评估：离线拓扑里到底有哪些节点（引擎只看得到这些）
    from tools.ontology_scoring_verify import build_topology
    topo = build_topology(rec, max_nodes=160, max_depth=6)
    topo_nodes = {n["id"] for n in topo["nodes"]}

    # 可观测性：role=root_cause 但没有任何 metric 关联、也不是约束 target → 无法被确认
    metric_ids = {tid for tid, t in terms.items()
                  if str(t.get("type")) == "metric" or tid.startswith("metric:")}
    metric_linked: Set[str] = set()
    for r in rec["relations"]:
        s, t = str(r.get("source") or ""), str(r.get("target") or "")
        if s in metric_ids and t:
            metric_linked.add(t)
        if t in metric_ids and s:
            metric_linked.add(s)
    cons_targets = {str(c.get("target") or "") for c in rec["constraints"]}
    unverifiable = sorted(
        tid for tid, t in terms.items()
        if (tid.startswith("rc:") or t.get("role") == "root_cause")
        and tid not in metric_ids and tid not in metric_linked and tid not in cons_targets
    )

    lifecycle = defaultdict(int)
    for t in terms.values():
        ls = t.get("lifecycle_state") or (t.get("lifecycle") or {}).get("state") or "(未声明)"
        lifecycle[str(ls)] += 1

    return {
        "version": version,
        "counts": {k: len(v) for k, v in rec.items()},
        "terms_total": len(terms),
        "orphans": sorted(orphans),
        "unreachable_from_entry": sorted(unreachable),
        "no_evidence": sorted(no_evidence),
        "no_mapping": sorted(no_mapping),
        "unverifiable_root_causes": unverifiable,
        "lifecycle": dict(lifecycle),
        "topology_nodes": len(topo_nodes),
        "not_in_topology": sorted(set(terms) - topo_nodes),
        "expected_terms": sorted(expected_terms),
        "expected_but_orphan": sorted(expected_terms & set(orphans)),
        "expected_but_unreachable": sorted(expected_terms & set(unreachable)),
        "orphans_in_topology": sorted(set(orphans) & topo_nodes),
        "detail": {
            tid: {
                "name": terms[tid].get("name", ""),
                "type": terms[tid].get("type", ""),
                "scope": terms[tid].get("scope", ""),
                "definition": terms[tid].get("definition", ""),
                "evidence_refs": terms[tid].get("evidence_refs") or [],
                "has_mapping": tid in mapped_terms,
                "degree": degree.get(tid, 0),
            }
            for tid in sorted(set(orphans) | set(unreachable))
        },
    }


def render(a: Dict[str, Any]) -> List[str]:
    L: List[str] = []
    L.append("=" * 96)
    L.append("本体卫生检查  version=%s   terms=%d" % (a["version"], a["terms_total"]))
    L.append("=" * 96)
    L.append("  族规模            %s" % json.dumps(a["counts"], ensure_ascii=False))
    L.append("  生命周期分布      %s" % json.dumps(a["lifecycle"], ensure_ascii=False))
    L.append("  引擎可见拓扑节点  %d / %d" % (a["topology_nodes"], a["terms_total"]))
    L.append("-" * 96)
    L.append("  ① 游离术语（关系图度为 0）         %d 个" % len(a["orphans"]))
    for tid in a["orphans"]:
        d = a["detail"][tid]
        L.append("       %-24s %-10s %-8s evidence=%d mapping=%s  %s"
                 % (tid, d["type"], d["scope"], len(d["evidence_refs"]),
                    d["has_mapping"], d["name"]))
    L.append("  ② 从入口 %s 不可达           %d 个" % (ENTRY, len(a["unreachable_from_entry"])))
    for tid in a["unreachable_from_entry"]:
        if tid in a["orphans"]:
            continue
        d = a["detail"].get(tid, {})
        L.append("       %-24s %s" % (tid, d.get("name", "")))
    L.append("  ③ 无证据（evidence_refs 为空）      %d 个：%s"
             % (len(a["no_evidence"]), ", ".join(a["no_evidence"]) or "-"))
    L.append("  ④ 无接地映射（Agent 拿不到物理源）   %d 个"
             % len(a["no_mapping"]))
    by_prefix: Dict[str, int] = defaultdict(int)
    for tid in a["no_mapping"]:
        by_prefix[tid.split(":")[0] + ":"] += 1
    L.append("       按前缀分布：%s" % json.dumps(dict(sorted(by_prefix.items())),
                                                ensure_ascii=False))
    L.append("       （注：rc:/metric: 等派生术语本来就靠关系关联指标而非直接接地，"
             "真正该看的是下一条）")
    L.append("  ⑤ 不可确认的根因（无 metric 关联且非约束 target）  %d 个：%s"
             % (len(a["unverifiable_root_causes"]),
                ", ".join(a["unverifiable_root_causes"]) or "无 ✅"))
    L.append("  ⑥ 不在引擎拓扑里                    %d 个：%s"
             % (len(a["not_in_topology"]), ", ".join(a["not_in_topology"]) or "-"))
    L.append("-" * 96)
    L.append("  期望根因术语 %d 个；其中游离 %d 个、不可达 %d 个"
             % (len(a["expected_terms"]), len(a["expected_but_orphan"]),
                len(a["expected_but_unreachable"])))
    if a["expected_but_orphan"] or a["expected_but_unreachable"]:
        L.append("      ⚠ 这是**真故障**：期望根因不在引擎可见拓扑里，诊断必然失败")
        L.append("      %s" % (a["expected_but_orphan"] + a["expected_but_unreachable"]))
    else:
        L.append("      ✅ 所有期望根因术语都在引擎可见拓扑内（游离术语不影响当前诊断能力）")
    L.append("=" * 96)
    return L


def term_contract(version: str) -> Dict[str, Any]:
    return term_contract_records(_load(version), version)


def term_contract_records(rec: Dict[str, List[dict]], version: str) -> Dict[str, Any]:
    """
    逐术语的**本体卫生契约**评分（0~1），可直接作为演化评估闸门的 ground truth。

    契约 4 条，等权：
      C1 声明了 lifecycle（`lifecycle.state` / `lifecycle_state`）
      C2 若关系图度为 0 → 必须声明为 `draft`（未接线的东西不许自称 active）
      C3 若声明为 active/validated → 必须至少有一条关系（激活的术语不许游离）
      C4 根因类术语必须可观测（有 metric 关联，或本身是 metric，或是约束 target）

    这样"清理游离术语"这件事就有了可复算、可成对比较的量化口径，
    而不是靠"看起来干净了"。
    """
    terms = {t["id"]: t for t in rec["terms"] if t.get("id")}
    degree: Dict[str, int] = defaultdict(int)
    adj: Dict[str, Set[str]] = defaultdict(set)
    for r in rec["relations"]:
        s, t = str(r.get("source") or ""), str(r.get("target") or "")
        if not s or not t:
            continue
        degree[s] += 1
        degree[t] += 1
        adj[s].add(t)
        adj[t].add(s)

    metric_ids = {tid for tid, t in terms.items()
                  if str(t.get("type")) == "metric" or tid.startswith("metric:")}
    metric_linked: Set[str] = set()
    for r in rec["relations"]:
        s, t = str(r.get("source") or ""), str(r.get("target") or "")
        if s in metric_ids and t:
            metric_linked.add(t)
        if t in metric_ids and s:
            metric_linked.add(s)
    cons_targets = {str(c.get("target") or "") for c in rec["constraints"]}

    per_term: List[Dict[str, Any]] = []
    for tid, t in terms.items():
        state = (t.get("lifecycle_state")
                 or (t.get("lifecycle") or {}).get("state") or "")
        deg = degree.get(tid, 0)
        is_rc = tid.startswith("rc:") or t.get("role") == "root_cause"
        observable = (tid in metric_ids or tid in metric_linked or tid in cons_targets)
        c1 = bool(state)
        c2 = (deg > 0) or (state == "draft")
        c3 = (state not in ("active", "validated")) or (deg > 0)
        c4 = (not is_rc) or observable
        checks = [c1, c2, c3, c4]
        per_term.append({
            "term": tid,
            "score": round(sum(1 for c in checks if c) / 4.0, 4),
            "lifecycle_state": state or None,
            "degree": deg,
            "is_root_cause": is_rc,
            "observable": observable,
            "failed": [n for n, c in zip(("C1_lifecycle", "C2_orphan_must_be_draft",
                                          "C3_active_must_be_wired",
                                          "C4_rootcause_must_be_observable"), checks)
                       if not c],
        })
    scores = [p["score"] for p in per_term]
    return {
        "version": version,
        "case_ids": [p["term"] for p in per_term],
        "scores": scores,
        "mean": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "full_credit": sum(1 for s in scores if s >= 1.0),
        "zero_credit": sum(1 for s in scores if s <= 0.0),
        "per_case": per_term,
    }


#: 演化闸门的硬前置条件：违反任意一条即拒绝 accept
def gate(version: str) -> Dict[str, Any]:
    return gate_records(_load(version), version)


def gate_records(rec: Dict[str, List[dict]], version: str) -> Dict[str, Any]:
    a = audit_records(rec, version)
    violations: List[str] = []
    s = term_contract_records(rec, version)
    undeclared = [p["term"] for p in s["per_case"] if "C1_lifecycle" in p["failed"]]
    active_orphans = [p["term"] for p in s["per_case"] if "C3_active_must_be_wired" in p["failed"]]
    if a["expected_but_orphan"] or a["expected_but_unreachable"]:
        violations.append("期望根因术语游离/不可达：%s"
                          % (a["expected_but_orphan"] + a["expected_but_unreachable"]))
    if undeclared:
        violations.append("未声明 lifecycle 的术语 %d 个：%s" % (len(undeclared), undeclared[:8]))
    if active_orphans:
        violations.append("声明 active 却游离的术语：%s" % active_orphans)
    if a["unverifiable_root_causes"]:
        violations.append("不可确认的根因：%s" % a["unverifiable_root_causes"])
    # 非阻断告警：active 但无证据支撑的术语（可能只是建模了环境里没实现的概念）
    warnings = []
    no_ev_active = [p["term"] for p in s["per_case"]
                    if p["lifecycle_state"] in ("active", "validated")]
    ev_map = {t["id"]: (t.get("evidence_refs") or []) for t in rec["terms"] if t.get("id")}
    no_ev = [tid for tid in no_ev_active if not ev_map.get(tid)]
    if no_ev:
        warnings.append("声明 active 但无 evidence_refs 的术语 %d 个：%s "
                        "—— 若该概念在环境中并未实现，应改标 lifecycle.state=draft"
                        % (len(no_ev), no_ev))
    return {"passed": not violations, "violations": violations, "warnings": warnings,
            "orphans": a["orphans"], "unverifiable_root_causes": a["unverifiable_root_causes"],
            "contract_mean": s["mean"]}


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="本体卫生检查：游离/不可达/无证据术语")
    ap.add_argument("--version", default="", help="指定版本（默认取 active.json）")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args(argv)

    version = args.version
    if not version:
        version = json.loads((WORKSPACE / "active.json").read_text(encoding="utf-8")).get(
            "active_version") or ""
    a = audit(version)
    for line in render(a):
        print(line)
    g = gate(version)
    c = term_contract(version)
    print("  卫生契约（4 条/术语）  平均 %.3f，满分 %d/%d"
          % (c["mean"], c["full_credit"], len(c["scores"])))
    print("  演化闸门               %s" % ("PASS ✅" if g["passed"] else "FAIL ❌"))
    for v in g["violations"]:
        print("      - %s" % v)
    for w in g.get("warnings") or []:
        print("      ⚠ %s" % w)
    print("=" * 96)
    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"audit": a, "gate": g, "contract": c},
                                ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print("报告: %s" % p)
    return 0 if g["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
