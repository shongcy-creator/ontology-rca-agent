#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
严格命中复算 + "改动只影响了该影响的场景"核对（可直接当闸门用）。

## 为什么需要它

端到端报告自带两个口径，**不可互换**：

  · `strict`（严格）：最终根因术语 ∈ 期望术语（`diagnosis_runs[0].hit_root_cause_exact`）
  · `ok`（宽松）    ：还认"期望术语出现在候选/证据/推理里"

本项目的结论数字用的是**严格**口径，且只在**可测量**场景上算分母
（`alert_coverage is False` 的场景 = 声明的告警一条都没触发、喂进去的是占位文本，
既不该算命中、也不该进分母）。这两条都得逐 case 复算，不能靠报告里的 summary 字段
——`summary` 是宽松口径的。

## 它核对什么

  1. 两份报告各自的严格命中（可测量口径）；逐 case 列出**变化**（转正 / 回退）；
  2. 可选断言（给了就核对，不给就只报告）：
     · `--expect-improved a,b`    转正集合必须**恰好**是这些
     · `--expect-regressed a,b`   回退集合必须**恰好**是这些（**默认断言为空**：
                                  本项目的纪律是"0 回退"，要放行请显式传 `--expect-regressed ""`）
     · `--expect-alert NAME=s1,s2` 某条告警必须**只**在这些场景触发（可重复传多次）
                                   —— 用来证明"新判据真的在区分故障"，而不是把答案写进输入

## 用法

  python tools/strict_score_check.py --baseline reports/rca_diagnosis_report.json \\
      --candidate reports/rca_diagnosis_report_postfix.json \\
      --expect-improved res_cluster_memory \\
      --expect-alert AppClusterMemoryCapacity=res_cluster_memory
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass


def _as_list(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def load_report(path: str) -> dict[str, dict[str, Any]]:
    """把端到端报告折成 {scenario: {measurable, strict, top1, expected, fired}}。"""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for r in d.get("results") or []:
        run = (r.get("diagnosis_runs") or [{}])[0]
        out[r.get("scenario")] = {
            "measurable": r.get("alert_coverage") is not False,
            "strict": bool(run.get("hit_root_cause_exact")),
            "ok_loose": bool(run.get("ok")),
            "top1": run.get("root_cause_id"),
            "expected": list(r.get("expected_terms") or []),
            "fired": [a.get("name") for a in (r.get("alerts_fired") or [])],
            "alert_text": r.get("alert_text") or "",
        }
    return out


def strict_summary(t: dict[str, dict[str, Any]]) -> tuple[int, int, list[str]]:
    meas = [k for k, v in t.items() if v["measurable"]]
    hits = [k for k in meas if t[k]["strict"]]
    return len(hits), len(meas), sorted(k for k in meas if not t[k]["strict"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="严格命中复算 + 变化范围核对")
    ap.add_argument("--baseline", required=True, help="改前报告")
    ap.add_argument("--candidate", required=True, help="改后报告")
    ap.add_argument("--expect-improved", default=None, dest="expect_improved",
                    help="转正集合（逗号分隔）；给了就必须恰好相等")
    ap.add_argument("--expect-regressed", default="", dest="expect_regressed",
                    help="回退集合（逗号分隔）；默认断言为空 = 0 回退")
    ap.add_argument("--expect-alert", action="append", default=[], dest="expect_alert",
                    metavar="NAME=s1,s2",
                    help="断言某条告警只在列出的场景触发（可重复）")
    ap.add_argument("--json-out", default="", dest="json_out")
    args = ap.parse_args(argv)

    base = load_report(args.baseline)
    cand = load_report(args.candidate)

    bh, bn, bmiss = strict_summary(base)
    ch, cn, cmiss = strict_summary(cand)

    print("=" * 100)
    print("严格命中复算（仅可测量场景）")
    print("  改前 %-46s %d/%d" % (args.baseline, bh, bn))
    print("  改后 %-46s %d/%d" % (args.candidate, ch, cn))
    print("=" * 100)

    common = sorted(set(base) & set(cand))
    improved = [k for k in common if cand[k]["measurable"] and cand[k]["strict"]
                and not base[k]["strict"]]
    regressed = [k for k in common if base[k]["measurable"] and base[k]["strict"]
                 and not cand[k]["strict"]]
    changed = [k for k in common
               if base[k]["measurable"] and cand[k]["measurable"]
               and (base[k]["strict"] != cand[k]["strict"]
                    or base[k]["top1"] != cand[k]["top1"])]

    if changed:
        print("\n逐 case 变化：")
        for k in changed:
            print("  %-22s %-24s -> %-24s  严格 %s -> %s"
                  % (k, base[k]["top1"], cand[k]["top1"], base[k]["strict"], cand[k]["strict"]))
    else:
        print("\n逐 case 变化：无")

    print("\n转正 %d 个: %s" % (len(improved), ", ".join(improved) or "-"))
    print("回退 %d 个: %s" % (len(regressed), ", ".join(regressed) or "-"))
    if cmiss:
        print("改后仍未命中（可测量）: %s" % ", ".join(cmiss))
    notmeas = sorted(k for k, v in cand.items() if not v["measurable"])
    if notmeas:
        print("不可测量（占位输入，不计分母）: %s" % ", ".join(notmeas))

    fails: list[str] = []
    print("\n断言：")
    if args.expect_improved is not None:
        want = sorted(_as_list(args.expect_improved))
        ok = want == sorted(improved)
        print("  [%s] 转正集合 == %s" % ("PASS" if ok else "FAIL", want or "[]"))
        if not ok:
            fails.append("expect-improved: 实际 %s" % (sorted(improved) or "[]"))
    want_reg = sorted(_as_list(args.expect_regressed))
    ok = want_reg == sorted(regressed)
    print("  [%s] 回退集合 == %s" % ("PASS" if ok else "FAIL", want_reg or "[]"))
    if not ok:
        fails.append("expect-regressed: 实际 %s" % (sorted(regressed) or "[]"))

    alert_scopes: dict[str, list[str]] = {}
    for spec in args.expect_alert:
        if "=" not in spec:
            print("  [FAIL] --expect-alert 需要 NAME=s1,s2 形式: %s" % spec)
            fails.append("expect-alert 格式: %s" % spec)
            continue
        name, _, scens = spec.partition("=")
        name = name.strip()
        want = sorted(_as_list(scens))
        got = sorted(k for k, v in cand.items() if name in v["fired"])
        alert_scopes[name] = got
        ok = want == got
        print("  [%s] 告警 %s 触发范围 == %s（实际 %s）"
              % ("PASS" if ok else "FAIL", name, want or "[]", got or "[]"))
        if not ok:
            fails.append("expect-alert %s: 实际 %s" % (name, got or "[]"))

    if args.json_out:
        out = {
            "baseline": args.baseline, "candidate": args.candidate,
            "baseline_strict": {"hits": bh, "measurable": bn, "misses": bmiss},
            "candidate_strict": {"hits": ch, "measurable": cn, "misses": cmiss},
            "improved": improved, "regressed": regressed, "changed": changed,
            "alert_scopes": alert_scopes, "failed_assertions": fails,
            "per_scenario": {k: {"baseline": base[k], "candidate": cand[k]} for k in common},
        }
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n报告: %s" % p)

    if fails:
        print("\n结论: FAIL（%d 条断言不成立）" % len(fails))
        return 1
    print("\n结论: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
