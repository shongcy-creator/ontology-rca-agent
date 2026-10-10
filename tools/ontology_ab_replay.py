#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本体 A/B 回放：**同一批真实告警文本**，在父/候选本体上成对复算"严格命中"。

## 为什么需要它（而不是再跑一遍端到端）

端到端（`tools/cluster_rca_verify.py`）跑一轮 21 场景要 2 小时以上，
而且它的**输入**（Prometheus 告警的 rca_hint）是随环境变化的。
但 `.chaos/rca_diagnosis_report.json` 已经把**当时真正喂给引擎的告警文本**
逐场景记下来了（`alert_text`）。于是可以：

  · 用**真实的**引擎输入（不是"标题+期望根因"这种自带答案的口径）；
  · 在同一批输入上，把父版本与候选版本的确定性打分**成对**复算；
  · 秒级出结果、可反复跑，差异只来自本体本身。

## 口径（与端到端严格口径一致）

  · **strict**：`root_cause.entity_id` ∈ 期望术语（`SCENARIO_EXPECTED_TERMS`）；
  · **top5**：期望术语出现在候选中；
  · **not_measurable**：该场景当时没有告警文本（占位文本）→ 单列，不计入分母。

## 用法

  python tools/ontology_ab_replay.py                                  # 本体 A/B：v5 → active
  python tools/ontology_ab_replay.py --parent ontology_v5 --candidate ontology_v6
  python tools/ontology_ab_replay.py --parent ontology_v6 --candidate-records .chaos/cand.json

  # 输入 A/B（同一本体、两套输入）：验证"给场景换一条更贴切的告警"带来的变化
  python tools/ontology_ab_replay.py --parent ontology_v6 \\
      --report .chaos/rca_diagnosis_report.json \\
      --report-b .chaos/rca_retest_oom_input_fix.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rca-agent"))
sys.path.insert(0, str(ROOT / "rca-agent" / "backend"))
sys.path.insert(0, str(ROOT / "vendor" / "EvoOntology"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

WORKSPACE = ROOT / ".evoontology"
FAMILIES = ("terms", "mappings", "relations", "constraints", "evidence")


# ── 输入：真实告警文本 ─────────────────────────────────────────────────────

def load_inputs(report: Path) -> List[Dict[str, Any]]:
    """从端到端报告里取"当时真正喂给引擎的告警文本"与期望术语。"""
    from tools.ontology_cluster_patch import SCENARIO_EXPECTED_TERMS

    rep = json.loads(report.read_text(encoding="utf-8"))
    out: List[Dict[str, Any]] = []
    for r in rep.get("results") or []:
        txt = r.get("alert_text")
        if not txt:
            continue
        scen = r.get("scenario")
        expected = list(r.get("expected_terms") or SCENARIO_EXPECTED_TERMS.get(scen) or [])
        out.append({
            "scenario": scen,
            "alert_text": txt,
            "severity": r.get("severity") or "P1",
            "expected": expected,
            # 端到端当时"没有告警文本"的场景 = 不可测量（占位文本），单列不计分母
            "measurable": r.get("alert_coverage") is not False,
            "e2e_top1": ((r.get("diagnosis_runs") or [{}])[0]).get("root_cause_id"),
            "e2e_strict_hit": bool(((r.get("diagnosis_runs") or [{}])[0])
                                   .get("hit_root_cause_exact")),
        })
    return out


# ── 记录加载 ───────────────────────────────────────────────────────────────

def load_version(version: str) -> Dict[str, List[dict]]:
    vdir = WORKSPACE / "versions" / version
    out: Dict[str, List[dict]] = {}
    for fam in FAMILIES:
        p = vdir / ("%s.json" % fam)
        out[fam] = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else []
    return out


def active_version() -> str:
    return json.loads((WORKSPACE / "active.json").read_text(encoding="utf-8"))["active_version"]


# ── 单版本复算 ─────────────────────────────────────────────────────────────

def replay(records: Dict[str, List[dict]], label: str,
           inputs: List[Dict[str, Any]], top_k: int = 5) -> Dict[str, Any]:
    """
    在给定 5 族记录上回放全部真实告警文本，返回逐场景的 top1/严格命中。

    **不落盘、不激活**：`OntologyScoring.from_records` 直接吃内存记录，
    因此候选版本在发布前就能被同一口径评分（这是"成对"的前提）。
    """
    from backend.services.rca_engine import RCAEngine
    from backend.services.ontology_scoring import OntologyScoring
    from tools.ontology_scoring_verify import build_topology

    scoring = OntologyScoring.from_records(records, label=label)
    if not scoring.available:
        return {"version": label, "available": False, "error": scoring.error}

    topo = build_topology(records)
    engine = RCAEngine()
    engine.scoring = scoring

    rows: List[Dict[str, Any]] = []
    for item in inputs:
        parsed = engine.parse_alert(item["alert_text"], item.get("severity") or "P1")
        cands = engine.score_candidates(topo["nodes"], parsed)
        ids = [str(c["entity_id"]) for c in cands]
        top1 = ids[0] if ids else ""
        exp = item["expected"]
        rows.append({
            "scenario": item["scenario"],
            "alert_text": item["alert_text"][:200],
            "expected": exp,
            "measurable": item["measurable"],
            "top1": top1,
            "top1_name": (cands[0].get("entity_name") if cands else ""),
            "top1_score": (cands[0].get("confidence") if cands else None),
            "strict_hit": bool(top1 and top1 in exp),
            "topk_hit": any(i in exp for i in ids[:top_k]),
            "top5": ids[:top_k],
            # "为什么是这个结论"——本体命中的约束与关键词（可审计）
            "constraint_hits": [c.get("constraint_id") for c in (parsed.get("constraint_hits") or [])],
            "matched_keywords": sorted({str(k) for c in (parsed.get("constraint_hits") or [])
                                        for k in (c.get("matched_keywords") or [])}),
            "e2e_top1": item["e2e_top1"],
            "e2e_strict_hit": item["e2e_strict_hit"],
        })

    meas = [r for r in rows if r["measurable"]]
    n = len(meas)
    return {
        "version": label,
        "available": True,
        "terms": len(records.get("terms") or []),
        "constraints": len(records.get("constraints") or []),
        "relations": len(records.get("relations") or []),
        "n_total": len(rows),
        "n_measurable": n,
        "strict": sum(1 for r in meas if r["strict_hit"]),
        "topk": sum(1 for r in meas if r["topk_hit"]),
        "strict_seconds": 0,  # 占位：由调用方汇总
        "top1_distribution": dict(Counter(r["top1"] or "(none)" for r in rows)),
        "rows": rows,
    }


def evaluate_version(version: str, inputs: List[Dict[str, Any]],
                     top_k: int = 5) -> Dict[str, Any]:
    if version in ("", None):
        version = active_version()
    return replay(load_version(version), version, inputs, top_k=top_k)


# ── 主流程 ─────────────────────────────────────────────────────────────────

def _paired_table(parent: Dict[str, Any], cand: Dict[str, Any]) -> Tuple[List[dict], List[str], List[str]]:
    pmap = {r["scenario"]: r for r in parent["rows"]}
    improved, regressed = [], []
    merged: List[dict] = []
    for c in cand["rows"]:
        p = pmap.get(c["scenario"], {})
        if c["strict_hit"] and not p.get("strict_hit"):
            tag = "★ 转正"
            improved.append(c["scenario"])
        elif p.get("strict_hit") and not c["strict_hit"]:
            tag = "⚠ 回退"
            regressed.append(c["scenario"])
        elif c["strict_hit"]:
            tag = "· 仍命中"
        else:
            tag = "· 仍未命中"
        merged.append({"parent": p, "candidate": c, "tag": tag})
    return merged, improved, regressed


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="本体 A/B 回放（真实告警文本，成对严格命中）")
    ap.add_argument("--report", default=str(ROOT / ".chaos" / "rca_diagnosis_report.json"),
                    help="端到端报告（提供真实 alert_text）")
    ap.add_argument("--parent", default="ontology_v5", help="父版本")
    ap.add_argument("--candidate", default="", help="候选版本（默认 active）")
    ap.add_argument("--candidate-records", default="",
                    help="候选 5 族记录 JSON（{terms:[...],...}），用于评分尚未落盘的候选")
    ap.add_argument("--report-b", default="", dest="report_b",
                    help="第二份端到端报告：切到「同一本体、两套输入」的成对模式，"
                         "用于验证**改输入**（例如给场景换一条更贴切的告警）带来的变化")
    ap.add_argument("--top-k", type=int, default=5, dest="top_k")
    ap.add_argument("--json-out", default=str(ROOT / ".chaos" / "ontology_ab_replay.json"))
    args = ap.parse_args(argv)

    inputs = load_inputs(Path(args.report))
    if not inputs:
        print("报告里没有可回放的 alert_text", file=sys.stderr)
        return 2

    # ── 模式 B：同一本体 + 两套输入（改的是输入，不是知识）────────────────
    if args.report_b:
        inputs_b = load_inputs(Path(args.report_b))
        if not inputs_b:
            print("第二份报告里没有可回放的 alert_text", file=sys.stderr)
            return 2
        ver = args.parent or active_version()
        recs = load_version(ver)
        label_a, label_b = "改前输入", "改后输入"
        # B 集 = A 集全体，仅把**报告 B 里出现过的场景**替换掉（其余场景的输入不变）。
        # 必须这样合并：报告 B 往往只重跑了受影响的少数场景，
        # 若直接拿它当全集，分母会缩成 1~2 条，"19/20 → 20/20" 就成了假的。
        merged = {i["scenario"]: i for i in inputs}
        overridden = []
        for i in inputs_b:
            if i["scenario"] in merged:
                overridden.append(i["scenario"])
            merged[i["scenario"]] = i
        inputs_new = [merged[k] for k in sorted(merged)]
        print("=" * 104)
        print("输入 A/B 回放（同一本体 %s，两套真实告警文本）" % ver)
        print("  A（改前）: %s（%d 条，可测量 %d）"
              % (args.report, len(inputs), sum(1 for i in inputs if i["measurable"])))
        print("  B（改后）: %s（覆盖 %d 个场景: %s；其余沿用 A）"
              % (args.report_b, len(overridden), ", ".join(sorted(overridden)) or "-"))
        print("=" * 104)
        parent = replay(recs, label_a, inputs, top_k=args.top_k)
        cand = replay(recs, label_b, inputs_new, top_k=args.top_k)
        parent["overridden_scenarios"] = []
        cand["overridden_scenarios"] = sorted(overridden)
        mode = "input_ab"
    else:
        # ── 模式 A：同一批输入 + 两个本体版本（差异只可能来自本体）────────
        cand_label = args.candidate or (
            (Path(args.candidate_records).stem + "(in-memory)") if args.candidate_records
            else active_version())
        label_a, label_b = args.parent, cand_label
        print("=" * 104)
        print("本体 A/B 回放（真实告警文本，成对严格命中）")
        print("  %s  →  %s      输入: %s（%d 条，其中可测量 %d）"
              % (args.parent, cand_label, args.report, len(inputs),
                 sum(1 for i in inputs if i["measurable"])))
        print("=" * 104)
        parent = evaluate_version(args.parent, inputs, top_k=args.top_k)
        if not parent.get("available"):
            print("父版本不可用: %s" % parent.get("error"), file=sys.stderr)
            return 2
        if args.candidate_records:
            recs = json.loads(Path(args.candidate_records).read_text(encoding="utf-8"))
            cand = replay(recs, cand_label, inputs, top_k=args.top_k)
        else:
            cand = evaluate_version(args.candidate, inputs, top_k=args.top_k)
        mode = "ontology_ab"

    merged, improved, regressed = _paired_table(parent, cand)

    print("\n  %-22s %-24s %-24s %-10s %s"
          % ("场景", "%s top1" % label_a, "%s top1" % label_b, "严格", "期望"))
    print("  " + "-" * 100)
    for m in merged:
        p, c = m["parent"], m["candidate"]
        if not c["measurable"]:
            continue
        print("  %-22s %-24s %-24s %-10s %s"
              % (c["scenario"], str(p.get("top1"))[:24], str(c.get("top1"))[:24],
                 m["tag"], ",".join(c["expected"])[:30]))

    print("\n  %-30s %-16s %-16s %s" % ("口径", label_a, label_b, "变化"))
    for key, label in (("strict", "严格命中（根因级）"), ("topk", "top%d 命中" % args.top_k)):
        print("  %-30s %-16s %-16s %+d"
              % (label, "%d/%d" % (parent[key], parent["n_measurable"]),
                 "%d/%d" % (cand[key], cand["n_measurable"]), cand[key] - parent[key]))

    print("\n  转正 %d 个: %s" % (len(improved), ", ".join(improved) or "-"))
    print("  回退 %d 个: %s" % (len(regressed), ", ".join(regressed) or "-"))

    # 未命中场景的"为什么"（约束命中 + 命中关键词）——审计用
    miss = [m for m in merged if m["candidate"]["measurable"] and not m["candidate"]["strict_hit"]]
    if miss:
        print("\n  仍未命中（候选）的判别依据：")
        for m in miss:
            c = m["candidate"]
            print("    %-22s top1=%-22s 期望=%-24s 约束命中=%s"
                  % (c["scenario"], c["top1"], ",".join(c["expected"]),
                     ",".join(c["constraint_hits"]) or "-"))

    out = {"mode": mode, "report": args.report, "report_b": args.report_b,
           "top_k": args.top_k, "candidate_records": args.candidate_records,
           "label_a": label_a, "label_b": label_b,
           "parent": parent, "candidate": cand,
           "improved": improved, "regressed": regressed,
           "paired": [{"scenario": m["candidate"]["scenario"], "tag": m["tag"],
                       "parent_top1": m["parent"].get("top1"),
                       "candidate_top1": m["candidate"]["top1"],
                       "candidate_alert_text": m["candidate"].get("alert_text"),
                       "expected": m["candidate"]["expected"],
                       "parent_strict": m["parent"].get("strict_hit"),
                       "candidate_strict": m["candidate"]["strict_hit"],
                       "candidate_constraint_hits": m["candidate"]["constraint_hits"]}
                      for m in merged]}
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n  报告: %s" % args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
