#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证 MySQL 持久化：incident 写入、重启存活、反馈、统计、轨迹."""
import json, sys, time
import urllib.request, urllib.error

BASE = "http://localhost:3001"   # 通过 nginx
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + name)
    else:
        FAIL += 1
        print("  [FAIL] " + name + " -- " + str(detail)[:220])


def jget(url, timeout=30):
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", errors="replace")[:300]}


def jpost(url, payload, timeout=240):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", errors="replace")[:300]}


print("=" * 72)
print("MYSQL PERSISTENCE VERIFICATION")
print("=" * 72)

# ── 1. 存储后端应为 mysql ──────────────────────────────────────────────
print("\n[1] Storage backend")
try:
    st, body = jget(BASE + "/api/rca/stats")
    check("stats 200", st == 200, body)
    backend = body.get("backend")
    print("     backend = " + str(backend))
    check("backend is mysql", backend == "mysql", body)
except Exception as e:
    check("stats", False, e)

# ── 2. 产生一条 incident ───────────────────────────────────────────────
print("\n[2] Create incident (persisted)")
iid = None
try:
    st, body = jpost(BASE + "/api/rca/infer", {
        "alert": {
            "alertId": "PERSIST-001",
            "severity": "P1",
            "message": "MySQL 慢查询增多，t_txn 表响应变慢",
        },
        "session_id": "persist",
    })
    check("infer 200", st == 200, body)
    iid = (body.get("result") or {}).get("incident_id")
    check("incident_id", bool(iid), iid)
    print("     incident_id = " + str(iid))
except Exception as e:
    check("infer", False, e)

# ── 3. 可读回 ─────────────────────────────────────────────────────────
print("\n[3] Read back")
if iid:
    st, body = jget(BASE + "/api/rca/incidents/" + iid)
    check("get incident 200", st == 200, body)
    check("same incident_id", (body.get("result") or {}).get("incident_id") == iid)
else:
    check("read back", False, "no incident_id")

# ── 4. 出现在列表中 ───────────────────────────────────────────────────
print("\n[4] Appears in list")
st, body = jget(BASE + "/api/rca/incidents")
ids = [i["incident_id"] for i in body.get("incidents", [])]
check("incident in list", iid in ids if iid else False, ids[:5])
print("     total incidents = " + str(body.get("count")))
check("count > 0", body.get("count", 0) > 0, body.get("count"))

# ── 5. 轨迹持久化 ─────────────────────────────────────────────────────
print("\n[5] Trajectory persisted")
if iid:
    st, body = jget(BASE + "/api/rca/incidents/" + iid + "/trajectory")
    traj = body.get("trajectory", [])
    check("trajectory 200", st == 200, body)
    check("trajectory non-empty", len(traj) > 0, len(traj))
    print("     trajectory steps = " + str(len(traj)))
    if traj:
        print("     step1 = [" + str(traj[0].get("type")) + "] " + str(traj[0].get("description"))[:60])
else:
    check("trajectory", False, "no incident_id")

# ── 6. 反馈写入 ───────────────────────────────────────────────────────
print("\n[6] Feedback")
if iid:
    st, body = jpost(BASE + "/api/rca/feedback", {
        "incident_id": iid,
        "verdict": "CONFIRMED",
        "actual_cause": "rc:slow-sql confirmed by DBA",
        "note": "added index idx_txn_customer_id",
        "operator": "oncall-01",
    })
    check("feedback 200", st == 200, body)
    check("feedback ok", body.get("ok") is True, body)

    st, body = jget(BASE + "/api/rca/stats")
    check("feedback counted", body.get("feedback_count", 0) >= 1, body)
    print("     feedback_count = " + str(body.get("feedback_count")))
    print("     by_category = " + json.dumps(body.get("by_category", {}), ensure_ascii=False))
    print("     by_severity = " + json.dumps(body.get("by_severity", {}), ensure_ascii=False))
else:
    check("feedback", False, "no incident_id")

print("\n" + "=" * 72)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
print("=" * 72)
print("INCIDENT_ID_FOR_RESTART_TEST=" + str(iid))
sys.exit(0 if FAIL == 0 else 1)
