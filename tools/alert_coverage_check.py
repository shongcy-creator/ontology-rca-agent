# -*- coding: utf-8 -*-
"""
告警覆盖率实测：注入故障 → 等足够久 → 看**声明的告警是否真的 firing**。

## 为什么单独做这个工具

端到端验证（`cluster_rca_verify.py`）会同时做"注入→压测→诊断→评分"，
一次 5 分钟以上；而**告警是否可得**是它的前置条件，本身值得单独、廉价地测：
不调 LLM、不评分，只回答一个问题 —— *这个故障能不能被监控发现？*

实测暴露过的问题都属这一类：
  · 规则 `for` 窗口（60/120/180s）比验证的保持时间（55/70s）长 → 等不到告警；
  · 规则阈值贴着故障量级（`threads_running > 20` 而实测正好 20）→ 永不触发；
  · `rate(...[5m]) > 0.5` 而实测 [2m] 只有 0.105 → 永不触发；
  · 目标从服务发现消失后 `up{x} == 0` 匹配不到 → 容器被杀却报不了；
  · 应用层**根本没有 OOM 指标** → OOM Kill 不可观测。

因此本工具的三类输出都有用：
  ① `covered`：声明告警中真的有 firing 的；
  ② `declared_but_silent`：声明了却没响 —— **监控缺口**；
  ③ `firing_but_undeclared`：响了但没声明 —— **catalog 元数据不准**（测试口径问题）。

用法：
  python tools/alert_coverage_check.py                       # 全部 21 场景
  python tools/alert_coverage_check.py app_oom_kill res_db_memory
  python tools/alert_coverage_check.py --hold 240 --json-out .chaos/alert_coverage.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

from tools.chaos.core import (  # noqa: E402
    CHAOS_DIR, Injector, http_json, start_stress_background, stop_stress_background,
)
from tools.chaos import FAULTS  # noqa: E402

PROM = "http://localhost:9090"


def max_alert_for_seconds(default: float = 180.0) -> float:
    """告警规则里最大的 `for`（秒）—— 保持时间必须 ≥ 它 + 一个评估间隔。"""
    st, body = http_json(PROM + "/api/v1/rules", timeout=20)
    if st != 200 or not isinstance(body, dict):
        return default
    mx = 0.0
    for g in (body.get("data") or {}).get("groups") or []:
        for r in g.get("rules") or []:
            if r.get("type") != "alerting":
                continue
            try:
                mx = max(mx, float(r.get("duration") or 0))
            except (TypeError, ValueError):
                continue
    return mx if mx > 0 else default


def collect_alerts() -> List[Dict[str, Any]]:
    """当前 firing/pending 的告警（含注解文本）。"""
    st, body = http_json(PROM + "/api/v1/rules", timeout=20)
    out: List[Dict[str, Any]] = []
    if st != 200 or not isinstance(body, dict):
        return out
    for g in (body.get("data") or {}).get("groups") or []:
        for r in g.get("rules") or []:
            if r.get("type") != "alerting":
                continue
            if (r.get("state") or "inactive") not in ("firing", "pending"):
                continue
            ann = r.get("annotations") or {}
            labels = r.get("labels") or {}
            out.append({"name": r.get("name"), "state": r.get("state"),
                        "severity": labels.get("severity", ""),
                        "hint": ann.get("rca_hint", ""),
                        "since": r.get("activeAt", "")})
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="告警覆盖率实测（注入→等待→看告警是否 firing）")
    ap.add_argument("scenarios", nargs="*", help="场景 id（留空=全部）")
    ap.add_argument("--hold", type=float, default=None,
                    help="保持秒数；默认 = 告警规则最大 for + 2×评估间隔(30s)")
    ap.add_argument("--settle", type=float, default=300.0, help="开跑前等告警消退的最长秒数")
    ap.add_argument("--cooldown", type=float, default=20.0)
    ap.add_argument("--stress", default="--duration 200 --concurrency 24",
                    help="需压测场景的后台压测参数（保持期内持续施加，放大故障影响）")
    ap.add_argument("--json-out", default=str(CHAOS_DIR / "alert_coverage.json"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    ids = args.scenarios or sorted(FAULTS)
    unknown = [s for s in ids if s not in FAULTS]
    if unknown:
        print("未知场景: %s" % ", ".join(unknown), file=sys.stderr)
        return 2

    hold = args.hold
    mx_for = max_alert_for_seconds()
    if hold is None:
        # for + 2×评估间隔：Prometheus 每 30s 评估一次，"条件持续 for" 最早也要
        # for+30s 才 firing，再留一个间隔的裕量（实测只留 30s 会卡在边界上）。
        hold = mx_for + 60.0
    print("=" * 88)
    print("告警覆盖率实测：%d 个场景  hold=%.0fs（规则最大 for=%.0fs）" % (len(ids), hold, mx_for))
    print("=" * 88)

    eng = Injector(FAULTS, verbose=False)

    # 开跑前等告警清空：否则上一个场景的残留告警会被算进"本次响了的告警"
    deadline = time.time() + args.settle
    while time.time() < deadline:
        cur = collect_alerts()
        if not cur:
            break
        if not args.quiet:
            print("  等待告警消退（仍在 firing: %s）…"
                  % ", ".join(sorted({a["name"] for a in cur})[:4]))
        time.sleep(20)
    residual = collect_alerts()
    if residual:
        print("  ⚠ 仍有 %d 条告警在 firing，结果可能受残留影响：%s"
              % (len(residual), ", ".join(sorted({a["name"] for a in residual})[:5])))

    results: List[Dict[str, Any]] = []
    out_path = Path(args.json_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        for i, fid in enumerate(ids, 1):
            fault = FAULTS[fid]
            declared = list(getattr(fault, "alert_names", None) or [])
            print("\n[%d/%d] %s  %s" % (i, len(ids), fid, fault.title))
            inj = eng.inject(fid, wait_signals=not fault.signal_needs_stress,
                             signal_timeout=60.0)
            if not inj.get("ok"):
                print("    注入失败：%s" % inj.get("error"))
                results.append({"scenario": fid, "error": inj.get("error"),
                                "declared": declared, "covered": False})
                eng.recover_all()
                continue

            # 需要压测才能把故障放大成可观测的层间问题：起一个后台压测
            # （与端到端脚本用同一套 start/stop_stress_background，
            #   保证"告警能不能响"的条件与端到端一致）
            stress = None
            if fault.needs_stress or fault.signal_needs_stress:
                stress = start_stress_background(args.stress.split())

            # 注入后立刻拍一次快照（用于区分"本次新响的" vs "残留"）
            pre_names = {a["name"] for a in collect_alerts()}
            if not args.quiet:
                print("    保持 %.0fs 等告警评估（注入前已在响: %s）…"
                      % (hold, ", ".join(sorted(pre_names)) or "无"))
            time.sleep(hold)

            if stress:
                stop_stress_background(stress, grace_s=10.0)
            now = collect_alerts()
            firing = {a["name"] for a in now}
            new_firing = sorted(firing - pre_names)
            hit = sorted(set(declared) & firing)
            missing = sorted(set(declared) - firing)
            extra = sorted(set(new_firing) - set(declared))
            ok = bool(hit)
            print("    声明告警 = %s" % (", ".join(declared) or "(未声明)"))
            print("    → 覆盖=%s  命中=%s  未响=%s  本次新响但未声明=%s"
                  % ("✅" if ok else "❌", ", ".join(hit) or "-",
                     ", ".join(missing) or "-", ", ".join(extra) or "-"))
            results.append({
                "scenario": fid, "layer": fault.layer, "title": fault.title,
                "declared": declared, "firing": sorted(firing), "new_firing": new_firing,
                "hit": hit, "declared_but_silent": missing,
                "firing_but_undeclared": extra, "covered": ok, "hold_s": hold,
            })
            out_path.write_text(json.dumps(
                {"hold_s": hold, "max_for_s": mx_for, "results": results},
                ensure_ascii=False, indent=2), encoding="utf-8")
            eng.recover_all()
            if i < len(ids):
                time.sleep(args.cooldown)
    finally:
        # 不变式：无论如何都回滚（这是安全网）
        try:
            eng.recover_all()
        except Exception as e:  # noqa: BLE001
            print("  收尾回滚异常: %s" % e)

    cov = sum(1 for r in results if r.get("covered"))
    silent = [(r["scenario"], r.get("declared_but_silent")) for r in results
              if r.get("declared") and not r.get("covered")]
    undeclared = [(r["scenario"], r.get("firing_but_undeclared")) for r in results
                  if r.get("firing_but_undeclared")]
    print("\n" + "=" * 88)
    print("告警覆盖汇总")
    print("=" * 88)
    print("  场景数 %d | 覆盖率 %d/%d = %.0f%%（hold=%.0fs）"
          % (len(results), cov, len(results), 100.0 * cov / max(1, len(results)), hold))
    if silent:
        print("\n  ❌ 声明了但**没响**（监控缺口）：")
        for s, m in silent:
            print("     %-22s 未响: %s" % (s, ", ".join(m)))
    if undeclared:
        print("\n  ⚠ 响了但**未声明**（catalog 元数据不准）：")
        for s, m in undeclared:
            print("     %-22s 新响: %s" % (s, ", ".join(m)))
    print("\n报告: %s" % args.json_out)
    return 0 if not silent else 1


if __name__ == "__main__":
    raise SystemExit(main())
