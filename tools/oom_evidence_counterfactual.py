#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
反事实检验：**继续收紧 `con:oom-detection` 的关键词，能不能把 `res_cluster_memory` 救成严格命中？**

## 背景（都是实测，不是推测）

第 6 轮（`--round memory`）已经把 `con:oom-detection` 的 trigger_keywords 里的
"内存压力 / 内存不足" 这类**症状词**删掉了，闸门也以 9/9 通过、发布了 `ontology_v6`。
但把**端到端当时真正喂给引擎的告警文本**（`.chaos/rca_diagnosis_report.json` 的
`alert_text`）回放一遍，严格命中是 **19/20 → 19/20（Δ0，零个 case 变化）**：

  res_cluster_memory 的真实输入是
    「payment-app 容器内存使用逼近 cgroup 上限，存在 OOM Kill 风险，容器可能被内核杀死并重启」
  它仍然命中 `con:oom-detection`，靠的是**收紧后仍然保留**的
    · `oom`   —— 出现在 "OOM Kill **风险**" 里（风险 ≠ 已发生）
    · `被内核杀死` —— 出现在 "**可能**被内核杀死" 里（可能 ≠ 已发生）

于是这个脚本把"再收紧一步"的各种变体构造出来，用**同一批真实输入**成对评分，
回答两个问题：
  ① 收紧到只剩"已被杀"的硬证据后，`res_cluster_memory` 会变成什么？
  ② 有没有任何一个变体能到 20/20？

## 关键事实（本脚本会再次确认）

`res_cluster_memory` 与 `app_memory_stress` 的**告警文本逐字节相同**
（两者都声明 `AppContainerMemoryPressure`，拿到的是同一条 `rca_hint`），
但期望根因**互不相交**：

  app_memory_stress : 期望 {rc:oom-kill, metric:mem-pressure}
  res_cluster_memory: 期望 {rc:cluster-capacity, metric:mem-pressure}

确定性引擎对同一段文本只能给**同一个** top1（记作 X）。两个 case 同时命中的充要条件是
`X ∈ {metric:mem-pressure}` —— 也就是"把一条**观测指标**当成根因结论"。
只要求 X 是根因术语（rc:*），则两个 case **至多命中一个** ⇒ 严格命中上限 = **19/20**。

用法：
  python tools/oom_evidence_counterfactual.py
  python tools/oom_evidence_counterfactual.py --json-out .chaos/oom_counterfactual.json
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

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

PAIR = ("app_memory_stress", "res_cluster_memory")


# ── 变体构造 ───────────────────────────────────────────────────────────────

def _fix_constraint(records: Dict[str, List[dict]], cid: str,
                    keywords: List[str]) -> None:
    for c in records.get("constraints") or []:
        if c.get("id") == cid:
            c["trigger_keywords"] = list(keywords)
            return
    raise KeyError(cid)


def _append_constraint_keywords(records: Dict[str, List[dict]], cid: str,
                                extra: List[str]) -> None:
    for c in records.get("constraints") or []:
        if c.get("id") == cid:
            kws = [str(k) for k in (c.get("trigger_keywords") or [])]
            for k in extra:
                if k not in kws:
                    kws.append(k)
            c["trigger_keywords"] = kws
            return
    raise KeyError(cid)


def _append_negative(records: Dict[str, List[dict]], tid: str,
                     extra: List[str]) -> None:
    for t in records.get("terms") or []:
        if t.get("id") == tid:
            neg = [str(k) for k in (t.get("negative_keywords") or [])]
            for k in extra:
                if k not in neg:
                    neg.append(k)
            t["negative_keywords"] = neg
            return
    raise KeyError(tid)


def variants(base: Dict[str, List[dict]]) -> List[Dict[str, Any]]:
    """待检验的关键词变体（全部只动本体，不动 ground truth，也不动告警文本）。"""
    out: List[Dict[str, Any]] = []

    out.append({"name": "v6_baseline", "records": copy.deepcopy(base),
                "note": "当前已发布版本（第 6 轮：已删掉「内存压力/内存不足」）"})

    r = copy.deepcopy(base)
    _fix_constraint(r, "con:oom-detection",
                    ["OOMKilled", "oom_kill 计数增加",
                     "increase(cc_container_oom_kill_total", "已被内核杀死"])
    out.append({"name": "V1_hard_evidence_only", "records": r,
                "note": "只保留「已被杀」的硬证据；删掉裸 oom / 被内核杀死（风险措辞）/ 容器重启"})

    r = copy.deepcopy(r)
    _append_negative(r, "rc:oom-kill", ["风险", "可能被", "逼近"])
    out.append({"name": "V2_V1_plus_negative_on_rc_oom_kill", "records": r,
                "note": "V1 + 给 rc:oom-kill 加 negative_keywords（「什么情况下不是我」）：风险/可能被/逼近"})

    r = copy.deepcopy(base)
    _fix_constraint(r, "con:oom-detection",
                    ["OOMKilled", "oom_kill 计数增加",
                     "increase(cc_container_oom_kill_total", "已被内核杀死"])
    _append_constraint_keywords(r, "con:cluster-capacity", ["容器内存", "内存使用逼近", "内存"])
    out.append({"name": "V3_force_cluster_capacity", "records": r,
                "note": "V1 + 硬把「容器内存/内存」塞进 con:cluster-capacity，试图强推 rc:cluster-capacity"})
    return out


# ── 主流程 ─────────────────────────────────────────────────────────────────

def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="收紧 con:oom-detection 的反事实检验")
    ap.add_argument("--report", default=str(ROOT / ".chaos" / "rca_diagnosis_report.json"),
                    help="端到端报告（真实 alert_text 的来源）")
    ap.add_argument("--json-out", default=str(ROOT / ".chaos" / "oom_counterfactual.json"))
    args = ap.parse_args(argv)

    from tools.ontology_ab_replay import active_version, load_inputs, load_version, replay
    from tools.ontology_v6_patch import N_INVARIANTS, check_matrix as check_memory_matrix

    inputs = load_inputs(Path(args.report))
    base = load_version(active_version())

    # 先确认"两个 case 的输入逐字节相同、期望互不相交"这个前提
    by_id = {i["scenario"]: i for i in inputs}
    a, b = by_id.get(PAIR[0]), by_id.get(PAIR[1])
    same_text = bool(a and b and a["alert_text"] == b["alert_text"])
    inter = sorted(set(a["expected"]) & set(b["expected"])) if (a and b) else []

    print("=" * 104)
    print("反事实检验：继续收紧 con:oom-detection 能否让 res_cluster_memory 严格命中？")
    print("  基线版本: %s    输入: %s" % (active_version(), args.report))
    print("=" * 104)
    print("\n[前提] %s 与 %s 的告警文本逐字节相同: %s" % (PAIR[0], PAIR[1], same_text))
    if same_text:
        print("       文本: %r" % a["alert_text"])
        print("       %-18s 期望 %s" % (PAIR[0], a["expected"]))
        print("       %-18s 期望 %s" % (PAIR[1], b["expected"]))
        print("       期望交集: %s" % (inter or "∅"))
        print("       ⇒ 同一输入只能给同一个 top1 X；两个 case 同时命中 ⟺ X ∈ %s" % (inter or "∅"))

    rows: List[Dict[str, Any]] = []
    for v in variants(base):
        recs = v["records"]
        ev = replay(recs, v["name"], inputs)
        matrix = check_memory_matrix(recs)
        inv_passed = sum(1 for cid in sorted(matrix) if not matrix.get(cid))
        by_scen = {r["scenario"]: r for r in ev["rows"]}
        pair = {s: {"top1": by_scen[s]["top1"], "strict": by_scen[s]["strict_hit"]}
                for s in PAIR if s in by_scen}
        regressed = [r["scenario"] for r in ev["rows"]
                     if r["measurable"] and not r["strict_hit"]
                     and r["scenario"] not in PAIR]
        rows.append({
            "variant": v["name"], "note": v["note"],
            "invariants_passed": "%d/%d" % (inv_passed, N_INVARIANTS),
            "strict": ev["strict"], "n": ev["n_measurable"],
            "topk": ev["topk"],
            "pair": pair,
            "non_pair_misses": regressed,
            "oom_detection_keywords": next(
                (c.get("trigger_keywords") for c in recs["constraints"]
                 if c.get("id") == "con:oom-detection"), None),
        })

    print("\n  %-38s %-10s %-12s %-22s %-22s" %
          ("变体", "不变量", "严格命中", PAIR[0], PAIR[1]))
    print("  " + "-" * 108)
    for r in rows:
        pa = r["pair"].get(PAIR[0], {})
        pb = r["pair"].get(PAIR[1], {})
        print("  %-38s %-10s %-12s %-22s %-22s"
              % (r["variant"], r["invariants_passed"],
                 "%d/%d" % (r["strict"], r["n"]),
                 "%s %s" % (pa.get("top1", "-"), "✅" if pa.get("strict") else "❌"),
                 "%s %s" % (pb.get("top1", "-"), "✅" if pb.get("strict") else "❌")))

    best = max(r["strict"] for r in rows)
    print("\n  最高严格命中: %d/%d" % (best, rows[0]["n"]))
    for r in rows:
        if r["variant"] != "v6_baseline":
            print("  · %-38s 关键词=%s" % (r["variant"], r["oom_detection_keywords"]))

    out = {"report": args.report, "baseline_version": active_version(),
           "pair": list(PAIR), "pair_same_input": same_text,
           "pair_expected_intersection": inter,
           "variants": rows, "best_strict": best, "n": rows[0]["n"]}
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n  报告: %s" % args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
