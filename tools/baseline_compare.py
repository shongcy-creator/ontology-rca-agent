# -*- coding: utf-8 -*-
"""
三方基线对照（离线，用归档报告的原始记录，不需要起集群）。

对照的三方：
  A. 本体引擎        —— 报告里 `diagnosis_runs[0].root_cause_id`（实际答案）
  B. 纯阈值基线      —— "**最严重的在响告警就是根因**"：按 severity 排序取第一条 firing 告警，
                        再按 catalog 的 alert→故障表映射到根因。这是绝大多数真实告警系统的做法。
  C. 纯 LLM（无本体）—— 需要外部模型；**只有确实可测时才计入**，否则标注为未测量（不编数字）。

判据统一到**根因术语层**：predicted 故障的 `rc:*` 术语 与 该场景真值 `expected_terms` 的 `rc:*`
有交集即算命中。这样 A/B 用同一把尺子。

纯阈值基线的固有弱点（正是本体要解决的东西）：
  · 只取一条告警 → 无法融合多信号
  · 严重度 ≠ 因果性 → P0 的下游症状会把 P1 的根因顶掉
  · 不知道哪些告警是**注入前就存在的噪声**（alerts_pre / residual）
  · 没有拓扑与约束推理 → 多个故障共用同一告警名时无法区分
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools.chaos.catalog import FAULTS  # noqa: E402

SEV = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def rc_terms(terms):
    return {t for t in (terms or []) if str(t).startswith("rc:")}


def alert_index():
    """alert 名 → 可能的故障 id 列表（从 catalog 声明来，相当于人工写的规则表）。"""
    idx = {}
    for fid, f in FAULTS.items():
        for a in (getattr(f, "alert_names", None) or []):
            idx.setdefault(a, []).append(fid)
    return idx


def main():
    rep = json.loads((ROOT / "reports" / "rca_diagnosis_report.json").read_text(encoding="utf-8"))
    idx = alert_index()
    rows = []

    for s in rep.get("results") or []:
        truth = rc_terms(s.get("expected_terms"))
        # ── A. 本体引擎（报告里已记录的答案）──
        runs = s.get("diagnosis_runs") or []
        a_id = runs[0].get("root_cause_id") if runs else None
        a_terms = {t for t in (runs[0].get("root_cause_terms") or [])} if runs else set()
        a_hit = bool(truth & (rc_terms(a_terms) or ({a_id} if a_id else set())))
        if not a_hit and a_id:
            a_hit = bool(truth & {a_id})

        # ── B. 纯阈值基线 ──
        fired = sorted(s.get("alerts_fired") or [],
                       key=lambda x: (SEV.get(x.get("severity"), 9), x.get("name") or ""))
        b_pred, b_alert, b_hit = None, (fired[0].get("name") if fired else None), False
        if fired:
            cands = idx.get(fired[0].get("name")) or []
            if cands:
                b_pred = sorted(cands)[0]
                b_hit = bool(truth & rc_terms(getattr(FAULTS[b_pred], "ontology_terms", [])))
        rows.append({
            "scenario": s.get("scenario"), "layer": s.get("layer"),
            "truth_terms": sorted(truth),
            "ontology": {"pred": a_id, "hit": a_hit, "how": (runs[0].get("how") if runs else None)},
            "threshold": {"pred": b_pred, "top_alert": b_alert, "hit": b_hit,
                          "n_alerts": len(s.get("alerts_fired") or []),
                          "pre_existing": s.get("alerts_pre") or []},
        })

    n = len(rows)
    a_ok = sum(1 for r in rows if r["ontology"]["hit"])
    b_ok = sum(1 for r in rows if r["threshold"]["hit"])
    # 纯阈值被"注入前就存在的噪声告警"带偏的次数
    b_misled = sum(1 for r in rows if not r["threshold"]["hit"] and r["threshold"]["top_alert"]
                   and r["threshold"]["top_alert"] in (r["threshold"]["pre_existing"] or []))
    out = {
        "method": "offline replay of the archived e2e report; scoring at root-cause-term level",
        "cases": n,
        "ontology": {"top1_hits": a_ok, "accuracy": round(a_ok / n, 4) if n else 0},
        "threshold_baseline": {
            "definition": "the most severe firing alert IS the root cause (mapped via the catalog)",
            "top1_hits": b_ok, "accuracy": round(b_ok / n, 4) if n else 0,
            "misled_by_preexisting_alert": b_misled,
        },
        "llm_baseline": {"measured": False,
                         "reason": "needs an external model credential; not available offline — "
                                   "left unmeasured on purpose rather than estimated"},
        "results": rows,
    }
    (ROOT / ".chaos").mkdir(exist_ok=True)
    for d in (ROOT / ".chaos", ROOT / "reports"):
        (d / "baseline_compare.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                                 encoding="utf-8")
    json.loads((ROOT / "reports" / "baseline_compare.json").read_text(encoding="utf-8"))

    print("  场景数 = %d" % n)
    print("  A 本体引擎   top-1 %d/%d = %.1f%%" % (a_ok, n, 100.0 * a_ok / n))
    print("  B 纯阈值基线 top-1 %d/%d = %.1f%%   （其中 %d 例被注入前的噪声告警带偏）"
          % (b_ok, n, 100.0 * b_ok / n, b_misled))
    print("  C 纯 LLM     未测量（需外部模型凭据，不估算、不编数字）")
    print("\n  本体比基线多命中：%s" % sorted(
        r["scenario"] for r in rows if r["ontology"]["hit"] and not r["threshold"]["hit"]))
    print("  基线比本体多命中：%s" % sorted(
        r["scenario"] for r in rows if r["threshold"]["hit"] and not r["ontology"]["hit"]))
    print("  两者都未命中    ：%s" % sorted(
        r["scenario"] for r in rows if not r["threshold"]["hit"] and not r["ontology"]["hit"]))
    print("\n  → reports/baseline_compare.json")
    return out


if __name__ == "__main__":
    main()
