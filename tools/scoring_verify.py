#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证 RCA 推理的正确性不变量（而非硬编码期望）."""
import json, sys
import urllib.request, urllib.error

BASE = "http://localhost:8088"
PASS, FAIL = 0, 0

# 告警关键词类别 → 打分类别（与 rca_engine.py 保持一致）
ALERT_CAT_TO_SCORE_CAT = {
    "延迟":   {"数据", "配置"},
    "连接":   {"数据", "配置"},
    "数据库": {"数据", "依赖"},
    "容器":   {"资源"},
    "部署":   {"配置", "代码"},
    "可用性": {"依赖", "代码"},
    "网络":   {"依赖"},
    "存储":   {"资源"},
}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + name)
    else:
        FAIL += 1
        print("  [FAIL] " + name + " -- " + str(detail)[:220])


def infer(message, severity="P1"):
    data = json.dumps({
        "alert": {"alertId": "INV", "severity": severity, "message": message},
        "session_id": "inv-test",
    }).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/rca/infer", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))["result"]


ALERTS = [
    "payment-app P99 latency > 500ms, MySQL connection pool exhausted",
    "容器 OOM 重启，内存使用率 98%",
    "MySQL 慢查询增多，t_txn 表响应变慢",
    "5xx 错误率突增，请求大量失败",
    "磁盘空间不足 disk full",
    "网络超时，连接失败",
    "部署后服务不可用 502",
]

print("=" * 72)
print("RCA INFERENCE INVARIANTS")
print("=" * 72)

for msg in ALERTS:
    print("\n[ALERT] " + msg)
    try:
        r = infer(msg)
        alert = r.get("alert", {})
        matched = set(alert.get("matched_categories") or [])
        rc = r.get("root_cause") or {}
        cat = rc.get("category")
        conf = rc.get("confidence", 0)
        topo = r.get("topology", [])
        edges = r.get("topology_edges", [])
        path = r.get("root_cause_path", [])
        cands = r.get("candidates", [])

        print("     matched=%s  root=[%s] %s conf=%s" % (
            sorted(matched), cat, rc.get("entity_id"), conf))
        print("     topology=%d nodes / %d edges / path=%d hops / candidates=%d" % (
            len(topo), len(edges), len(path), len(cands)))

        # ── 不变量断言 ──────────────────────────────────────────
        expected_cats = set()
        for m in matched:
            expected_cats |= ALERT_CAT_TO_SCORE_CAT.get(m, set())

        check("root_cause exists", bool(rc), r.get("confidence"))
        check("root_cause category aligned with alert categories",
              cat in expected_cats, "got %s expected one of %s" % (cat, sorted(expected_cats)))
        check("root_cause is not a bare relation",
              not str(rc.get("entity_id", "")).startswith("rel:"), rc.get("entity_id"))
        check("confidence in (0,1)", 0 < float(conf) <= 0.99, conf)
        check("topology non-empty", len(topo) > 0, len(topo))
        check("topology has edges", len(edges) > 0, len(edges))
        check("every edge endpoint exists in nodes",
              all(e["source"] in {n["id"] for n in topo} and e["target"] in {n["id"] for n in topo}
                  for e in edges),
              [e for e in edges if e["source"] not in {n["id"] for n in topo}][:2])
        check("candidates sorted desc",
              all(cands[i]["confidence"] >= cands[i + 1]["confidence"]
                  for i in range(len(cands) - 1)),
              [c["confidence"] for c in cands])
        check("root_cause == top candidate",
              cands and cands[0]["entity_id"] == rc.get("entity_id"),
              (cands[0]["entity_id"] if cands else None, rc.get("entity_id")))
        check("path starts at root_cause entity",
              (not path) or path[0]["from"] == rc.get("entity_id"),
              path[:1])
        check("path ends at app:payment-app",
              (not path) or path[-1]["to"] == "app:payment-app",
              path[-1:] if path else [])
        check("rca_chain non-empty", len(r.get("rca_chain", [])) > 0, len(r.get("rca_chain", [])))
        check("evidence included", isinstance(r.get("evidence"), dict) and len(r["evidence"]) > 0,
              list((r.get("evidence") or {}).keys()))

        # 置信度不应全部饱和
        if len(cands) >= 3:
            check("candidate confidences not all identical",
                  len({c["confidence"] for c in cands[:3]}) > 1,
                  [c["confidence"] for c in cands[:3]])
    except Exception as e:
        check("infer: " + msg[:40], False, e)

print("\n" + "=" * 72)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
