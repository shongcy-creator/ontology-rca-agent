#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模拟前端完整调用流程（与 chatStore.ts 行为一致）."""
import json, sys, urllib.request, urllib.error

BASE = "http://localhost:8088"
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + name)
    else:
        FAIL += 1
        print("  [FAIL] " + name + " -- " + str(detail)[:200])


def post(path, payload, timeout=120):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", errors="replace")[:300]}


def get(path, timeout=30):
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", errors="replace")[:300]}


print("=" * 70)
print("FRONTEND FLOW SIMULATION (mirrors chatStore.ts)")
print("=" * 70)

session = "fe-sim-001"
conversation = []

# ── Turn 1: user reports an alert ──────────────────────────────────────
print("\n[Turn 1] user reports alert")
user_text = "payment-app P99 延迟超过 500ms，MySQL 连接池耗尽"
conversation.append({"role": "user", "content": user_text})

st, body = post("/api/chat", {"messages": conversation, "session_id": session})
check("chat -> 200", st == 200, body)
assistant_content = body.get("message", {}).get("content", "")
incident_id = body.get("incident_id")
check("assistant content received", len(assistant_content) > 0, len(assistant_content))
check("incident_id present", bool(incident_id), incident_id)
conversation.append({"role": "assistant", "content": assistant_content})

# ── Store action: selectIncident(incident_id) ─────────────────────────
print("\n[Store] selectIncident(" + str(incident_id) + ")")
st, body = get("/api/rca/incidents/" + str(incident_id))
check("incident -> 200", st == 200, body)
result = body.get("result", {})
check("result.incident_id", result.get("incident_id") == incident_id, result.get("incident_id"))

# Verify every field RCAResultPanel / TopologyGraph / MetricsPanel consumes
check("result.alert.message", bool(result.get("alert", {}).get("message")))
check("result.alert.severity", bool(result.get("alert", {}).get("severity")))
check("result.confidence is number", isinstance(result.get("confidence"), (int, float)))
check("result.elapsed_ms is number", isinstance(result.get("elapsed_ms"), (int, float)))
check("result.topology non-empty", len(result.get("topology", [])) > 0)
check("result.candidates non-empty", len(result.get("candidates", [])) > 0)
check("result.root_cause present", result.get("root_cause") is not None)
check("result.rca_chain non-empty", len(result.get("rca_chain", [])) > 0)
check("result.evidence is dict", isinstance(result.get("evidence"), dict))
check("result.thresholds_triggered is list", isinstance(result.get("thresholds_triggered"), list))

# TopologyGraph consumes {id,name,type}
topo = result.get("topology", [])
if topo:
    n = topo[0]
    check("topology[0].id", bool(n.get("id")), n)
    check("topology[0].type", bool(n.get("type")), n)
    print("     topology[0] = " + json.dumps(n, ensure_ascii=False))

# RCAResultPanel consumes candidates {category,entity_id,confidence,reason}
cands = result.get("candidates", [])
if cands:
    c = cands[0]
    check("candidate.category", bool(c.get("category")), c)
    check("candidate.entity_id", bool(c.get("entity_id")), c)
    check("candidate.confidence", isinstance(c.get("confidence"), (int, float)), c)
    print("     candidate[0] = " + json.dumps(c, ensure_ascii=False)[:160])

# rca_chain consumes {step,type,description}
chain = result.get("rca_chain", [])
if chain:
    s = chain[0]
    check("chain.step", isinstance(s.get("step"), int), s)
    check("chain.type", bool(s.get("type")), s)
    check("chain.description key exists", "description" in s, list(s.keys()))

# ── Store action: loadIncidents() ─────────────────────────────────────
print("\n[Store] loadIncidents()")
st, body = get("/api/rca/incidents")
check("incidents -> 200", st == 200, body)
incidents = body.get("incidents", [])
check("incidents list non-empty", len(incidents) > 0, len(incidents))
if incidents:
    inc = incidents[0]
    for k in ["incident_id", "message", "severity", "category", "entity_id", "confidence", "elapsed_ms"]:
        check("incidentSummary." + k + " exists", k in inc, list(inc.keys()))
    print("     incidents[0] = " + json.dumps(inc, ensure_ascii=False)[:180])

# ── Turn 2: multi-turn follow-up ──────────────────────────────────────
print("\n[Turn 2] follow-up question")
conversation.append({"role": "user", "content": "这个问题的详细排查步骤是什么？"})
st, body = post("/api/chat", {"messages": conversation, "session_id": session})
check("multi-turn -> 200", st == 200, body)
check("assistant reply", body.get("message", {}).get("role") == "assistant")

# ── Turn 3: second incident (different category) ──────────────────────
print("\n[Turn 3] second alert (OOM)")
conversation2 = [{"role": "user", "content": "容器 OOM 重启，内存使用率 98%"}]
st, body = post("/api/chat", {"messages": conversation2, "session_id": "fe-sim-002"})
check("second chat -> 200", st == 200, body)
iid2 = body.get("incident_id")
check("second incident_id", bool(iid2), iid2)
if iid2:
    st, body = get("/api/rca/incidents/" + str(iid2))
    r2 = body.get("result", {})
    rc2 = r2.get("root_cause") or {}
    print("     second root_cause = [" + str(rc2.get("category")) + "] " + str(rc2.get("entity_id")))
    check("second incident has root cause", bool(rc2))

# ── Incident count grew ───────────────────────────────────────────────
st, body = get("/api/rca/incidents")
check("incident count >= 2", body.get("count", 0) >= 2, body.get("count"))

print("\n" + "=" * 70)
print("RESULT: " + str(PASS) + " passed, " + str(FAIL) + " failed")
print("=" * 70)
sys.exit(0 if FAIL == 0 else 1)
