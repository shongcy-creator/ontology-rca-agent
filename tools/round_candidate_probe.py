#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
候选轮次**开轮次前的三道固定输入筛**（pre-publication screen）。

## 为什么要有它

第 6 轮暴露过一个模式：闸门用"自撰的不变量"当口径时，**0/9 → 9/9 也能通过**，
而引擎在真实输入上一动不动（`reports/oom_evidence_replay.json`）。
第 7 轮验证了另一面：候选 patch 在自撰口径下"看起来对"，却会在固定输入上**回退** ——
本轮候选（把症状词从 `rc:oom-kill` 的术语词典迁到 `rc:mem-pressure`）实测：

  · 21 场景引擎 top1      20/21 → **19/21**（`app_memory_stress` 掉出）
  · held-out 考卷 top1     4/10 → 4/10
  · 归档真实告警严格命中   19/20 → **17/20**（`app_oom_kill`、`app_memory_stress` 被改判 `rc:mem-pressure`）

按这三条就不该开轮次 —— 于是把它做成工具：**任何候选在进版本化流程之前先跑这里，
出现回退就 exit 1**，而不是等发布之后才发现。

## 三套输入各是什么（都固定、可复算）

  ① `--catalog`（默认含）：21 个故障场景的**离线形态**（`title：expected_root_cause`），
     算引擎 top1/top5 —— 与驱动里的"回归观察量"同口径。
  ② `--heldout`（默认含）：`tools/heldout_verify.HELDOUT_PHRASINGS` 的 10 条**没参与调参**的措辞。
     ⚠ 它是**观察量**：一旦照着它改词典，它就退化成第二份训练集。本工具只报数，不据此迭代。
  ③ `--report`（默认含）：归档端到端报告里的**真实告警文本**，按可测量场景算**严格命中**。

用法：
  python tools/round_candidate_probe.py                       # 内置候选（第 7 轮，已被否）
  python tools/round_candidate_probe.py --candidate-records candidate.json
  python tools/round_candidate_probe.py --json-out .chaos/round_candidate_probe.json
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
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

PARENT_VERSION = "ontology_v6"


# ── 内置候选：第 7 轮（实测被否，留作可复算的记录）────────────────────────
#: 假设（源自手册 §11.42 的诊断）：`rc:oom-kill` 的定义要求"已被杀"的直接证据，
#: 而它的**术语词典**仍把"内存压力/内存不足/内存超限/被内核杀死"当作自己的证据。
#: 做法：把这些词迁给 `rc:mem-pressure`，并给 `rc:oom-kill` 加"假设性措辞"负向词。
#: 结果：见模块 docstring —— **回退**，因此没有开这一轮。
TERM_LAYER_MOVE = {
    "rc:oom-kill": {
        "remove_pos": ["内存超限", "内存不足", "内存压力", "内存逼近", "内存使用率", "被内核杀死"],
        "add_pos": ["已被内核杀死", "oom_kill 计数增加"],
        "add_neg": ["风险", "可能被", "逼近", "前兆", "未 OOM", "未被杀死"],
    },
    "rc:mem-pressure": {
        "add_pos": ["内存压力", "内存不足", "内存超限", "内存逼近", "逼近上限", "内存使用率"],
    },
    "con:oom-detection": {"rename_kw": {"被内核杀死": "已被内核杀死"}},
}


def build_term_layer_move(base: Dict[str, List[dict]]) -> Dict[str, List[dict]]:
    recs = copy.deepcopy(base)
    for t in recs["terms"]:
        spec = TERM_LAYER_MOVE.get(t["id"])
        if not spec:
            continue
        pos = [k for k in (t.get("scoring_keywords") or []) if k not in spec.get("remove_pos", [])]
        for k in spec.get("add_pos", []):
            if k not in pos:
                pos.append(k)
        if pos:
            t["scoring_keywords"] = pos
        if spec.get("add_neg"):
            neg = list(t.get("negative_keywords") or [])
            for k in spec["add_neg"]:
                if k not in neg:
                    neg.append(k)
            t["negative_keywords"] = neg
    for c in recs["constraints"]:
        spec = TERM_LAYER_MOVE.get(c["id"])
        if spec and spec.get("rename_kw"):
            c["trigger_keywords"] = [spec["rename_kw"].get(k, k)
                                     for k in (c.get("trigger_keywords") or [])]
    return recs


VARIANTS = {
    "term-layer-move": ("第 7 轮候选：把症状词从 rc:oom-kill 的术语词典迁到 rc:mem-pressure",
                        build_term_layer_move),
}


# ── 三套固定输入 ───────────────────────────────────────────────────────────

def _scoring(recs, label):
    from backend.services.ontology_scoring import OntologyScoring
    from tools.ontology_scoring_verify import build_topology
    sc = OntologyScoring.from_records(recs, label=label)
    return sc, build_topology(recs)


def eval_catalog(recs, label):
    from tools.evolve_ontology_cluster import evaluate_engine, scenario_cases
    ev = evaluate_engine(recs, scenario_cases(), label=label)
    return {"top1": ev["full_credit"], "n": len(ev["scores"]),
            "misses": [c["case_id"] for c in ev["per_case"] if c["score"] < 1.0]}


def eval_heldout(recs, label):
    from backend.services.rca_engine import RCAEngine
    from tools.heldout_verify import HELDOUT_PHRASINGS
    sc, topo = _scoring(recs, label)
    eng = RCAEngine()
    eng.scoring = sc
    rows = []
    for c in HELDOUT_PHRASINGS:
        parsed = eng.parse_alert(c["text"], "P1")
        cands = eng.score_candidates(topo["nodes"], parsed)
        ids = [str(x["entity_id"]) for x in cands]
        top1 = ids[0] if ids else ""
        rows.append({"id": c["id"], "expected": c["expected"], "top1": top1,
                     "hit": top1 in c["expected"]})
    return {"top1": sum(1 for r in rows if r["hit"]), "n": len(rows), "rows": rows}


def eval_report(recs, label, report: Path):
    from tools.ontology_ab_replay import load_inputs, replay
    inputs = load_inputs(report)
    ev = replay(recs, label, inputs)
    return {"strict": ev["strict"], "n": ev["n_measurable"],
            "top1": {r["scenario"]: r["top1"] for r in ev["rows"]}}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="候选轮次开轮次前的三道固定输入筛")
    ap.add_argument("--variant", default="term-layer-move", choices=sorted(VARIANTS))
    ap.add_argument("--candidate-records", default="", dest="candidate_records",
                    help="直接用一份 5 族记录做候选（优先于 --variant）")
    ap.add_argument("--report", default=str(ROOT / "reports" / "rca_diagnosis_report.json"),
                    help="真实告警文本来源（默认：改前基线）")
    ap.add_argument("--json-out", default=str(ROOT / ".chaos" / "round_candidate_probe.json"))
    args = ap.parse_args(argv)

    from tools.ontology_ab_replay import load_version
    parent = load_version(PARENT_VERSION)
    if args.candidate_records:
        cand = json.loads(Path(args.candidate_records).read_text(encoding="utf-8"))
        cand_label = Path(args.candidate_records).stem
    else:
        note, builder = VARIANTS[args.variant]
        cand = builder(parent)
        cand_label = args.variant

    print("=" * 100)
    print("候选轮次开轮次前的三道固定输入筛")
    print("  父版本: %s   候选: %s" % (PARENT_VERSION, cand_label))
    if not args.candidate_records:
        print("  %s" % VARIANTS[args.variant][0])
    print("=" * 100)

    verdicts: List[Dict[str, Any]] = []

    p, c = eval_catalog(parent, PARENT_VERSION), eval_catalog(cand, cand_label)
    print("\n① 21 场景（故障目录离线形态）引擎 top1： %d/%d  →  %d/%d"
          % (p["top1"], p["n"], c["top1"], c["n"]))
    if p["misses"] != c["misses"]:
        print("   未命中变化： %s → %s" % (p["misses"], c["misses"]))
    verdicts.append({"set": "catalog21", "parent": p["top1"], "candidate": c["top1"],
                     "delta": c["top1"] - p["top1"]})

    hp, hc = eval_heldout(parent, PARENT_VERSION), eval_heldout(cand, cand_label)
    print("\n② held-out 考卷（观察量，不据此迭代）： top1 %d/%d  →  %d/%d"
          % (hp["top1"], hp["n"], hc["top1"], hc["n"]))
    for a, b in zip(hp["rows"], hc["rows"]):
        flag = "⚠ 回退" if (a["hit"] and not b["hit"]) else ("★ 转正" if (b["hit"] and not a["hit"]) else "")
        if a["top1"] != b["top1"] or flag:
            print("   %-6s %-22s → %-22s %s" % (a["id"], a["top1"], b["top1"], flag))
    verdicts.append({"set": "heldout", "parent": hp["top1"], "candidate": hc["top1"],
                     "delta": hc["top1"] - hp["top1"]})

    rp = eval_report(parent, PARENT_VERSION, Path(args.report))
    rc = eval_report(cand, cand_label, Path(args.report))
    print("\n③ 归档真实告警文本（严格命中，可测量）： %d/%d  →  %d/%d"
          % (rp["strict"], rp["n"], rc["strict"], rc["n"]))
    changed = [(k, rp["top1"][k], rc["top1"][k]) for k in rp["top1"]
               if rp["top1"][k] != rc["top1"][k]]
    for k, a, b in changed:
        print("   %-22s %-22s → %-22s" % (k, a, b))
    if not changed:
        print("   结论变化：无")
    verdicts.append({"set": "report_strict", "parent": rp["strict"], "candidate": rc["strict"],
                     "delta": rc["strict"] - rp["strict"]})

    regressed = [v for v in verdicts if v["delta"] < 0]
    improved = [v for v in verdicts if v["delta"] > 0]
    print("\n" + "-" * 100)
    print("  回退的输入集: %s" % ([v["set"] for v in regressed] or "无"))
    print("  提升的输入集: %s" % ([v["set"] for v in improved] or "无"))
    ok = not regressed
    print("  结论: %s" % ("PASS —— 可以进版本化轮次（仍须过闸门）" if ok else
                          "REJECT —— 任一固定输入回退就不该开这一轮"))
    print("-" * 100)

    out = {"parent_version": PARENT_VERSION, "candidate": cand_label,
           "report": args.report, "verdicts": verdicts,
           "catalog_misses": {"parent": p["misses"], "candidate": c["misses"]},
           "changed_on_real_inputs": changed, "reject": bool(regressed)}
    pth = Path(args.json_out)
    pth.parent.mkdir(parents=True, exist_ok=True)
    pth.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  报告: %s" % pth)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
