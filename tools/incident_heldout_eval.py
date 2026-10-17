#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
留存评估集复算：把 `<workspace>/rca_evidence_inbox.jsonl` 里的**真实事故记录**
变成一份可复算的评测材料（**只报不改**）。

## 这是什么（以及不是什么）

不是"把证据并进本体"（那是版本化轮次的事，且对逐次事故记录而言是空转）；
也不是调参工具。它做的是三件**只读**的事：

  1. **复算**：对每条记录的 `query`（当时喂给引擎的告警文本），用**当前激活本体**离线复算
     确定性快路径的结论，与记录里当时写下的结论对比 —— 不一致就是"知识漂移"或"当时用了别的版本"，
     两者都值得看（记录里带着 `ontology_version`，可区分）。
  2. **有 ground truth 时打分**：如果这条 query 与某条已知场景的告警文本**逐字相同**
     （来自归档端到端报告 / 故障目录），就用该场景的期望术语算**严格命中**。
  3. **挑出"知识缺口"候选**：置信度低于快路径门槛、候选为空、或没有任何本体约束支撑的记录 ——
     这些是下一轮本体迭代（补术语/约束）的输入，而不是被悄悄并进版本。

## 为什么不能拿它调参

累积起来的这批记录是**没有被调过参的真实输入**。一旦拿它去改打分词典/关键词，它就退化成
第二份训练集 —— 项目已有的 held-out 教训（`reports/heldout_report.json`：措辞一换严格命中 4/10）
就是这么来的。所以本工具只有"报告"这一个动作。

用法：
  python tools/incident_heldout_eval.py                      # 复算当前收件箱
  python tools/incident_heldout_eval.py --inbox <path.jsonl>
  python tools/incident_heldout_eval.py --json-out .chaos/incident_heldout_eval.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

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
DEFAULT_INBOX = WORKSPACE / "rca_evidence_inbox.jsonl"

#: 可用来给 query 找 ground truth 的归档报告（真实告警文本 → 场景/期望术语）
GT_REPORTS = [
    "reports/rca_diagnosis_report.json",
    "reports/rca_diagnosis_report_postfix.json",
    "reports/rca_retest_oom_input_fix.json",
    "reports/rca_retest_res_cluster_memory.json",
    ".chaos/rca_diagnosis_report.json",
]


# ── 输入 ───────────────────────────────────────────────────────────────────

def load_inbox(path: Path) -> List[Dict[str, Any]]:
    """读收件箱：一行一条 JSON（也兼容 JSON 数组，便于用别处的导出做回归）。"""
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.lstrip().startswith("["):
        return list(json.loads(text))
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def load_version(version: str) -> Dict[str, List[dict]]:
    vdir = WORKSPACE / "versions" / version
    out: Dict[str, List[dict]] = {}
    for fam in FAMILIES:
        p = vdir / ("%s.json" % fam)
        out[fam] = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else []
    return out


def active_version() -> str:
    return json.loads((WORKSPACE / "active.json").read_text(encoding="utf-8"))["active_version"]


def ground_truth_index() -> Dict[str, List[Dict[str, Any]]]:
    """
    alert_text → [{scenario, expected_terms, source}, ...]。

    两个来源：
      · 归档端到端报告里的 `results[].alert_text`（**与线上同源**：都是 Prometheus 的 rca_hint）；
      · 故障目录的离线形式（`title：expected_root_cause`，`ontology_ab_replay` 用的那种）。
    只有**逐字相同**才算命中 ground truth —— 模糊匹配会把评测变成猜谜。

    刻意保留"一段文本 → 多个场景"的情形（列表而非单值）：本项目真出现过
    `app_memory_stress` 与 `res_cluster_memory` 拿到**逐字节相同**的 rca_hint、
    期望却互斥的情况（reports/oom_counterfactual.json）。**这种歧义必须被报出来，
    而不是悄悄取第一个** —— 否则评测会把"基准自身的缺陷"记成"引擎判错/判对"。
    """
    idx: Dict[str, List[Dict[str, Any]]] = {}

    def _add(txt: str, scenario: Any, expected: List[str], source: str) -> None:
        txt = (txt or "").strip()
        if not txt:
            return
        bucket = idx.setdefault(txt, [])
        if not any(e["scenario"] == scenario and e["source"] == source for e in bucket):
            bucket.append({"scenario": scenario, "expected_terms": list(expected),
                           "source": source})

    for rel in GT_REPORTS:
        p = ROOT / rel
        if not p.is_file():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for r in d.get("results") or []:
            _add(r.get("alert_text") or "", r.get("scenario"),
                 list(r.get("expected_terms") or []), rel)
    try:
        from tools.chaos import FAULTS
        from tools.ontology_cluster_patch import SCENARIO_EXPECTED_TERMS
        for fid, fault in FAULTS.items():
            _add("%s：%s" % (fault.title, fault.expected_root_cause), fid,
                 list(SCENARIO_EXPECTED_TERMS.get(fid) or []), "catalog(offline form)")
    except Exception:  # noqa: BLE001
        pass
    return idx


# ── 复算 ───────────────────────────────────────────────────────────────────

def _recorded(result_field: Any) -> Dict[str, Any]:
    """记录里的 `result` 是一个 JSON **字符串**（老实现留下的形状），这里解回 dict。"""
    if isinstance(result_field, dict):
        return result_field
    if isinstance(result_field, str):
        try:
            return json.loads(result_field)
        except json.JSONDecodeError:
            return {}
    return {}


def evaluate(records_source: Dict[str, List[dict]], label: str,
             inbox: List[Dict[str, Any]], gt: Dict[str, List[Dict[str, Any]]],
             min_confidence: float) -> Dict[str, Any]:
    from backend.services.rca_engine import RCAEngine
    from backend.services.ontology_scoring import OntologyScoring
    from tools.ontology_scoring_verify import build_topology

    scoring = OntologyScoring.from_records(records_source, label=label)
    if not scoring.available:
        return {"version": label, "available": False, "error": scoring.error}
    topo = build_topology(records_source)
    engine = RCAEngine()
    engine.scoring = scoring

    rows: List[Dict[str, Any]] = []
    for rec in inbox:
        query = (rec.get("query") or "").strip()
        got = _recorded(rec.get("result"))
        parsed = engine.parse_alert(query, "P1")
        cands = engine.score_candidates(topo["nodes"], parsed)
        ids = [str(c.get("entity_id")) for c in cands]
        top1 = ids[0] if ids else ""
        conf = float(cands[0].get("confidence") or 0.0) if cands else 0.0

        entries = gt.get(query) or []
        # 一段文本对应多个**互不相同**的期望 → 基准自身有歧义，不参与严格打分
        exp_sets = {frozenset(e.get("expected_terms") or []) for e in entries}
        ambiguous = len(exp_sets) > 1
        single = entries[0] if (entries and not ambiguous) else None
        # 归档报告有好多份，同一场景会在多份里出现 —— 这里按场景去重
        amb_scenarios = sorted({str(e.get("scenario")) for e in entries}) if ambiguous else None

        rec_conf = float(got.get("confidence") or 0.0)
        rows.append({
            "id": rec.get("id"),
            "timestamp": rec.get("timestamp"),
            "recorded_ontology_version": rec.get("ontology_version"),
            "query": query[:200],
            "recorded": {"entity_id": got.get("entity_id"), "confidence": rec_conf},
            "recomputed": {"entity_id": top1, "confidence": conf,
                           "constraints": [c.get("constraint_id") for c in
                                           (parsed.get("constraint_hits") or [])]},
            # 记录时与现在的结论是否一致（不一致 = 知识漂移 / 当时用了别的版本）
            "drift": bool(got.get("entity_id") and top1 and got.get("entity_id") != top1),
            "ground_truth": single,
            "ground_truth_ambiguous": amb_scenarios,
            "ground_truth_ambiguous_expectations": ([sorted(s) for s in exp_sets]
                                                    if ambiguous else None),
            "strict_recorded": bool(single and got.get("entity_id") in single["expected_terms"]),
            "strict_recomputed": bool(single and top1 in single["expected_terms"]),
            "constraint_supported": bool(parsed.get("constraint_hits")),
            "low_confidence": conf < min_confidence,
            "no_candidate": not ids,
        })

    n = len(rows)
    with_gt = [r for r in rows if r["ground_truth"]]
    amb = [r for r in rows if r["ground_truth_ambiguous"]]
    return {
        "version": label,
        "available": True,
        "n_records": n,
        "n_with_ground_truth": len(with_gt),
        "n_ambiguous_ground_truth": len(amb),
        "strict_recorded": sum(1 for r in with_gt if r["strict_recorded"]),
        "strict_recomputed": sum(1 for r in with_gt if r["strict_recomputed"]),
        "drift": sum(1 for r in rows if r["drift"]),
        "mean_confidence": round(sum(r["recomputed"]["confidence"] for r in rows) / n, 4) if n else 0.0,
        "low_confidence_ids": [r["id"] for r in rows if r["low_confidence"]],
        "no_candidate_ids": [r["id"] for r in rows if r["no_candidate"]],
        "unsupported_ids": [r["id"] for r in rows if not r["constraint_supported"]],
        "ambiguous_ids": [r["id"] for r in amb],
        "version_distribution": dict(Counter(r["recorded_ontology_version"] for r in rows)),
        "rows": rows,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="留存评估集复算（只报不改）")
    ap.add_argument("--inbox", default=str(DEFAULT_INBOX),
                    help="收件箱路径（jsonl；默认 <workspace>/rca_evidence_inbox.jsonl）")
    ap.add_argument("--ontology", default="", help="用于复算的本体版本（默认激活版本）")
    ap.add_argument("--min-confidence", type=float, default=0.85, dest="min_confidence",
                    help="快路径门槛；低于它的记录进「知识缺口候选」（默认 0.85）")
    ap.add_argument("--json-out", default=str(ROOT / ".chaos" / "incident_heldout_eval.json"))
    args = ap.parse_args(argv)

    inbox = load_inbox(Path(args.inbox))
    print("=" * 100)
    print("留存评估集复算（只报不改）")
    print("  收件箱: %s（%d 条记录）" % (args.inbox, len(inbox)))
    print("=" * 100)
    if not inbox:
        print("\n收件箱为空 —— 还没有可复算的真实事故记录。")
        print("记录会随 `POST /api/rca/infer`（或任何 inject_evidence 的推理）自动追加；")
        print("本工具**不**需要为此改动本体：它是只读的评测材料。")
        return 0

    gt = ground_truth_index()
    ver = args.ontology or active_version()
    res = evaluate(load_version(ver), ver, inbox, gt, args.min_confidence)
    if not res.get("available"):
        print("本体不可用: %s" % res.get("error"), file=sys.stderr)
        return 2

    print("\n  复算本体版本 : %s" % res["version"])
    print("  记录数       : %d（其中 %d 条能对上已知场景的 ground truth）"
          % (res["n_records"], res["n_with_ground_truth"]))
    if res["n_ambiguous_ground_truth"]:
        print("  ⚠ 基准歧义   : %d 条 —— 同一段告警文本对应**互不相同**的期望术语，"
              "不参与严格打分" % res["n_ambiguous_ground_truth"])
        for r in res["rows"]:
            if r["ground_truth_ambiguous"]:
                print("      %-24s ↔ 场景 %s；期望互不相同：%s"
                      % (str(r["id"])[:24], r["ground_truth_ambiguous"],
                         r["ground_truth_ambiguous_expectations"]))
    print("  记录时的版本 : %s" % json.dumps(res["version_distribution"], ensure_ascii=False))
    print("  平均置信度   : %.3f" % res["mean_confidence"])
    print("  与记录不一致 : %d 条（知识漂移 / 当时是别的版本）" % res["drift"])
    if res["n_with_ground_truth"]:
        print("  严格命中     : 记录时 %d/%d；现在复算 %d/%d"
              % (res["strict_recorded"], res["n_with_ground_truth"],
                 res["strict_recomputed"], res["n_with_ground_truth"]))
    print("  知识缺口候选 : 低置信 %s；无候选 %s；无约束支撑 %s"
          % (res["low_confidence_ids"] or "-", res["no_candidate_ids"] or "-",
             res["unsupported_ids"] or "-"))

    print("\n  %-26s %-22s %-22s %-6s %s" % ("记录", "记录时的结论", "复算结论", "漂移", "ground truth"))
    print("  " + "-" * 94)
    for r in res["rows"]:
        if r["ground_truth_ambiguous"]:
            g = "歧义(%s)" % ",".join(r["ground_truth_ambiguous"])[:30]
        elif r["ground_truth"]:
            g = r["ground_truth"]["scenario"]
        else:
            g = "-"
        print("  %-26s %-22s %-22s %-6s %s"
              % (str(r["id"])[:26], str(r["recorded"]["entity_id"])[:22],
                 str(r["recomputed"]["entity_id"])[:22], "是" if r["drift"] else "否", g))

    out = {"inbox": args.inbox, "active_version": active_version(),
           "ground_truth_entries": len(gt), "result": res}
    p = Path(args.json_out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n  报告: %s" % p)
    print("  ⚠ 这批记录是**没被调过参**的真实输入：只能用来报告，不能拿来调打分词典/关键词，")
    print("    否则它就退化成第二份训练集（见 reports/heldout_report.json 的教训）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
