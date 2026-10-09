# -*- coding: utf-8 -*-
"""
成对评估：**"组件级 → 根因级"升级**到底有没有用。

## 为什么用"回放真实告警文本"而不是重跑端到端

重跑 21 场景端到端要 2 小时，而且结果受 LLM 随机性影响。
但升级逻辑位于**确定性打分**里，而端到端产物
（`.chaos/rca_diagnosis_report.json`）已经**逐场景记录了当时真正喂给引擎的
告警文本**（`alert_text`）。于是可以直接回放这些文本：
  · 输入是真实的（不是"含答案的标题"这种泄漏口径）；
  · 秒级出结果、可反复跑；
  · 同一份输入下 on/off 成对比较，差异只来自升级本身。

## 口径

  · **严格**：`root_cause.entity_id` ∈ 期望术语（与端到端一致）；
  · 同时给出"组件级命中率" —— 升级要治的正是这一类（结论落在组件上）。

用法：
  python tools/rootcause_promotion_verify.py                       # 用最新端到端报告
  python tools/rootcause_promotion_verify.py --report .chaos/rca_diagnosis_report.json
  python tools/rootcause_promotion_verify.py --json-out .chaos/promotion_ab.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rca-agent"))
sys.path.insert(0, str(ROOT / "rca-agent" / "backend"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass


def expected_terms_for(scenario: str, fallback: List[str]) -> List[str]:
    """期望术语：优先用端到端报告里的，缺失时回落到 catalog/本体映射。"""
    if fallback:
        return list(fallback)
    try:
        from tools.ontology_cluster_patch import SCENARIO_EXPECTED_TERMS
        return list(SCENARIO_EXPECTED_TERMS.get(scenario) or [])
    except Exception:  # noqa: BLE001
        return []


def evaluate(alert_texts: List[Dict[str, Any]], promote: bool) -> Dict[str, Any]:
    """在给定开关下回放全部告警文本，返回严格/组件级口径的命中情况。"""
    if promote:
        os.environ.pop("RCA_DISABLE_COMPONENT_PROMOTION", None)
    else:
        os.environ["RCA_DISABLE_COMPONENT_PROMOTION"] = "1"

    from backend.services.rca_engine import RCAEngine
    engine = RCAEngine()
    # RCAEngine.traverse_topology() 内部走本体客户端（带缓存），是引擎真正用的那份拓扑。
    nodes = engine.traverse_topology()

    rows = []
    for item in alert_texts:
        parsed = engine.parse_alert(item["alert_text"], item.get("severity") or "P1")
        cands = engine.score_candidates(nodes, parsed)
        top1 = str(cands[0]["entity_id"]) if cands else ""
        role = str(cands[0].get("role") or "") if cands else ""
        exp = item["expected"]
        rows.append({
            "scenario": item["scenario"],
            "alert_text": item["alert_text"][:160],
            "expected": exp,
            "top1": top1,
            "top1_role": role,
            "strict_hit": bool(top1 and top1 in exp),
            "component_level": role == "component",
            "promoted_from": (cands[0].get("promoted_from") if cands else None),
            "promotion_basis": (cands[0].get("promotion_basis") if cands else None),
            "top5": [str(c["entity_id"]) for c in cands[:5]],
        })
    n = len(rows)
    return {
        "n": n,
        "strict": sum(1 for r in rows if r["strict_hit"]),
        "component_level": sum(1 for r in rows if r["component_level"]),
        "top5_hit": sum(1 for r in rows if any(e in r["top5"] for e in r["expected"])),
        "rows": rows,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="组件级→根因级升级的成对评估")
    ap.add_argument("--report", default=str(ROOT / ".chaos" / "rca_diagnosis_report.json"),
                    help="端到端报告（提供真实 alert_text 与期望术语）")
    ap.add_argument("--json-out", default=str(ROOT / ".chaos" / "promotion_ab.json"))
    args = ap.parse_args(argv)

    rep = json.loads(Path(args.report).read_text(encoding="utf-8"))
    results = rep.get("results") or []
    texts: List[Dict[str, Any]] = []
    for r in results:
        txt = r.get("alert_text")
        if not txt:
            continue
        texts.append({
            "scenario": r.get("scenario"),
            "alert_text": txt,
            "severity": r.get("severity") or "P1",
            "expected": expected_terms_for(r.get("scenario"), r.get("expected_terms") or []),
        })
    if not texts:
        print("报告里没有可回放的 alert_text", file=sys.stderr)
        return 2

    print("=" * 92)
    print("组件级→根因级升级：成对评估（回放 %d 条**真实**告警文本）" % len(texts))
    print("=" * 92)

    off = evaluate(texts, promote=False)
    on = evaluate(texts, promote=True)

    print("\n  %-24s %-22s %-22s %s" % ("场景", "关闭升级 top1", "开启升级 top1", "期望"))
    print("  " + "-" * 88)
    for a, b in zip(off["rows"], on["rows"]):
        mark = "  " if a["top1"] == b["top1"] else "→ "
        print("  %-24s %s%-22s %-22s %s"
              % (a["scenario"], mark, a["top1"][:22], b["top1"][:22],
                 ",".join(a["expected"])[:26]))

    def pct(x, n):
        return "%.0f%%" % (100.0 * x / n) if n else "-"

    print("\n  %-26s %-14s %-14s %s" % ("口径", "关闭升级", "开启升级", "变化"))
    for key, label in (("strict", "严格命中（根因级）"),
                       ("component_level", "结论落在组件上"),
                       ("top5_hit", "top5 命中")):
        print("  %-26s %-14s %-14s %s"
              % (label, "%d (%s)" % (off[key], pct(off[key], off["n"])),
                 "%d (%s)" % (on[key], pct(on[key], on["n"])),
                 "%+d" % (on[key] - off[key])))

    promoted = [r for r in on["rows"] if r.get("promoted_from")]
    print("\n  实际发生升级的场景 %d 个：" % len(promoted))
    for r in promoted:
        changed = next((o for o in off["rows"] if o["scenario"] == r["scenario"]), None)
        good = "✅ 转正" if r["strict_hit"] and not (changed or {}).get("strict_hit") else (
            "❌ 转坏" if (changed or {}).get("strict_hit") and not r["strict_hit"] else "· 无变化")
        print("    %-22s %s → %-22s %s" % (r["scenario"], r["promoted_from"], r["top1"], good))

    out = {"report": args.report, "n": off["n"], "off": off, "on": on}
    Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n  报告: %s" % args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
