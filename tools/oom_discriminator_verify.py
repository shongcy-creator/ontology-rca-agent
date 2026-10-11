#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
指标平面判别器的规则自测（离线，秒级，不需要集群）。

判别器 [`backend/services/metric_discriminator.py`](../rca-agent/backend/services/metric_discriminator.py)
的每条规则都在这里被**成对**验证：给一个 stub 查询函数，断言它给出的规则与胜者。
这样"OOM 还是压力"这条判据就有一个**能失败**的回归测试，而不是只能靠真跑一次故障去猜。

用法：
  python tools/oom_discriminator_verify.py            # 只跑规则自测
  python tools/oom_discriminator_verify.py --live     # 另查一次真实 Prometheus 的计数值
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rca-agent"))
sys.path.insert(0, str(ROOT / "rca-agent" / "backend"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

from backend.services.metric_discriminator import (  # noqa: E402
    apply, decide, query_oom_metric,
)

OOM, MEM = "rc:oom-kill", "rc:mem-pressure"

PASS = 0
FAIL = 0

#: 真实场景里那两种告警文本
TEXT_KILL = ("payment-app 容器发生 OOM Kill（cgroup oom_kill 计数增加），内存超限被内核杀死并重启")
TEXT_RISK = ("payment-app 容器内存使用逼近 cgroup 上限，存在 OOM Kill 风险，容器可能被内核杀死并重启")
TEXT_PRESSURE = "容器内存用量逼近限额，随时可能被杀"          # 无 OOM 字样、只有症状/假设
TEXT_OTHER = "MySQL 出现 InnoDB 行锁等待，长事务持有排他锁阻塞写入"


def check(name: str, cond: bool, detail: Any = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s -- %s" % (name, str(detail)[:300]))


def stub(value: float, calls: List[str] | None = None):
    """返回一个 Prometheus query 的 stub：把 increase(...) 固定成 value。"""
    def _q(expr: str) -> Dict[str, Any]:
        if calls is not None:
            calls.append(expr)
        if "increase(" in expr:
            return {"result": [{"metric": {"app_instance": "app-1"}, "value": [0, str(value)]}]}
        return {"result": [{"metric": {"app_instance": "app-1"}, "value": [0, "7"]}]}
    return _q


def boom(expr: str) -> Dict[str, Any]:
    raise RuntimeError("Prometheus unreachable (stub)")


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="指标平面判别器的规则自测")
    ap.add_argument("--live", action="store_true", help="另查一次真实 Prometheus 的计数值")
    args = ap.parse_args(argv)

    print("=" * 96)
    print("指标平面判别器 · 规则自测")
    print("=" * 96)

    print("\n[1] 规则 0：不构成歧义时不介入")
    d = decide(TEXT_OTHER, ["rc:row-lock", OOM], {})
    check("只有单一 OOM 候选 → 不介入", d["rule"] == "0-not-ambiguous" and not d["applicable"], d)
    d = decide(TEXT_RISK, ["rc:row-lock", OOM, MEM], {})
    check("top1 是别的根因 → 不介入", not d["applicable"], d)
    d = decide("", [], {})
    check("候选为空 → 不介入", not d["applicable"], d)
    calls0: List[str] = []
    apply(TEXT_OTHER, [{"entity_id": "rc:row-lock"}, {"entity_id": OOM}], stub(0.0, calls0))
    check("不构成歧义时不查指标", calls0 == [], calls0)

    print("\n[2] 规则 1：文本有『已被杀』硬证据 → 判 oom-kill，且**不查指标**")
    calls: List[str] = []
    cands = [{"entity_id": MEM, "reason": "压力"}, {"entity_id": OOM, "reason": "被杀"}]
    out, d = apply(TEXT_KILL, cands, stub(0.0, calls))
    check("规则 = 1-text-hard-evidence", d["rule"] == "1-text-hard-evidence", d)
    check("胜者 = rc:oom-kill", d["winner"] == OOM, d)
    check("硬证据被识别", "oom_kill 计数增加" in d["text_hard_evidence"], d["text_hard_evidence"])
    check("没有查询指标（文本已足够）", calls == [], calls)
    check("候选被重排，oom-kill 置顶", out[0]["entity_id"] == OOM, [c["entity_id"] for c in out])
    check("理由里写进判据", "指标平面" in out[0]["reason"], out[0]["reason"][-80:])

    print("\n[3] 规则 2：只有症状/假设措辞 → 由指标裁决")
    out, d = apply(TEXT_RISK, list(cands), stub(0.42))
    check("increase > 0 → 判 oom-kill", d["rule"] == "2-metric-kill-observed" and d["winner"] == OOM, d)
    out, d = apply(TEXT_RISK, list(cands), stub(0.0))
    check("increase == 0 → 判 mem-pressure", d["rule"] == "2-metric-no-kill" and d["winner"] == MEM, d)
    check("mem-pressure 被置顶", out[0]["entity_id"] == MEM, [c["entity_id"] for c in out])
    out, d = apply(TEXT_PRESSURE, list(cands), stub(0.0))
    check("无 OOM 字样的压力文本 → mem-pressure", d["winner"] == MEM, d)

    print("\n[4] 规则 3：指标不可用 → 不介入（保持文本顺序，宁可回到文本）")
    before = [{"entity_id": OOM, "reason": "x"}, {"entity_id": MEM, "reason": "y"}]
    out, d = apply(TEXT_RISK, [dict(c) for c in before], boom)
    check("规则 = 3-metric-unavailable", d["rule"] == "3-metric-unavailable", d)
    check("胜者为空（不介入）", d["winner"] is None, d)
    check("候选顺序不变", [c["entity_id"] for c in out] == [OOM, MEM], [c["entity_id"] for c in out])
    check("记录了不可用原因", "unreachable" in d["why"], d["why"])

    print("\n[5] 取数：max(increase(...[5m])) 与当前计数值")
    m = query_oom_metric(stub(1.5))
    check("available", m["available"] is True, m)
    check("max_increase = 1.5", m["max_increase"] == 1.5, m)
    check("kill_observed", m["kill_observed"] is True, m)
    check("表达式含 5m 窗口", "[5m]" in m["expr"] and "increase(" in m["expr"], m["expr"])
    check("counter_now 取到（审计用）", m["counter_now"] == 7.0, m)
    m = query_oom_metric(boom)
    check("查询异常 → available=False 且不抛", m["available"] is False and "error" in m, m)

    print("\n" + "=" * 96)
    print("RESULT: %d passed, %d failed" % (PASS, FAIL))
    print("=" * 96)

    if args.live:
        print("\n[6] --live：真实 Prometheus 的计数值（只读）")
        from backend.services.prometheus import PrometheusClient
        prom = PrometheusClient()
        try:
            live = query_oom_metric(prom.query)
            print("     expr        : %s" % live.get("expr"))
            print("     available   : %s" % live.get("available"))
            print("     max_increase: %s" % live.get("max_increase"))
            print("     counter_now : %s" % live.get("counter_now"))
            for s in (live.get("series") or []):
                print("       %-30s increase=%.3f" % (s.get("instance"), s.get("increase")))
            if not live.get("series"):
                print("     （没有序列 —— 说明计数值为 0 或该指标未暴露）")
        except Exception as e:  # noqa: BLE001
            print("     查询失败: %s: %s" % (type(e).__name__, e))

    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
