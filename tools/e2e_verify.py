#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RCA Agent 端到端验证脚本."""
import json, sys, time
import urllib.request, urllib.error

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


def get(path, timeout=15):
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def post(path, payload, timeout=90):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", errors="replace")[:300]}


print("=" * 70)
print("RCA AGENT END-TO-END VERIFICATION")
print("=" * 70)

# ── 1. Health ─────────────────────────────────────────────────────────
print("\n[1] Health & static")
try:
    st, body = get("/api/health")
    check("GET /api/health -> 200", st == 200)
    check("health payload", body.get("status") == "ok", body)
except Exception as e:
    check("GET /api/health", False, e)

try:
    req = urllib.request.Request(BASE + "/ui/")
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read(200).decode("utf-8", errors="replace")
        check("GET /ui/ (SPA) -> 200", r.status == 200)
        check("/ui/ returns HTML", "<!doctype html" in html.lower(), html[:60])
except Exception as e:
    check("GET /ui/", False, e)

# ── 2. Tools ──────────────────────────────────────────────────────────
print("\n[2] Tool registry")
try:
    st, body = get("/api/chat/tools")
    tools = body.get("tools", [])
    check("GET /api/chat/tools -> 200", st == 200)
    check("tools registered >= 5", len(tools) >= 5, len(tools))
    names = [t.get("function", {}).get("name") for t in tools]
    for expected in ["rca_infer", "query_prometheus", "get_topology", "browse_ontology"]:
        check("tool: " + expected, expected in names, names)
except Exception as e:
    check("tools", False, e)

# ── 3. Metrics ────────────────────────────────────────────────────────
print("\n[3] Prometheus metrics")
try:
    st, body = get("/api/metrics/summary", timeout=30)
    check("GET /api/metrics/summary -> 200", st == 200)
    check("has mysql_pool", "mysql_pool" in body, list(body.keys()))
    check("has mysql_global", "mysql_global" in body, list(body.keys()))
    check("has targets", "targets" in body, list(body.keys()))
    mg = body.get("mysql_global", {})
    print("     mysql threads_connected = " + str(mg.get("threads_connected")))
    print("     max_used_connections    = " + str(mg.get("max_used_connections")))
    print("     targets                 = " + str(len(body.get("targets", []))))
except Exception as e:
    check("metrics summary", False, e)

# ── 4. Topology ───────────────────────────────────────────────────────
print("\n[4] Ontology topology")
try:
    st, body = post("/api/topology/", {"app_name": "payment-app", "session_id": "e2e"}, timeout=60)
    check("POST /api/topology/ -> 200", st == 200, body)
    nodes = body.get("nodes", [])
    check("topology nodes > 0", len(nodes) > 0, len(nodes))
    if nodes:
        ids = [n.get("id") for n in nodes]
        print("     nodes: " + ", ".join(ids[:8]))
except Exception as e:
    check("topology", False, e)

# ── 5. RCA inference ──────────────────────────────────────────────────
print("\n[5] RCA inference (direct)")
rca_payload = {
    "alert": {
        "alertId": "E2E-001",
        "severity": "P1",
        "message": "payment-app P99 latency > 500ms, MySQL connection pool exhausted",
    },
    "session_id": "e2e-session",
}
try:
    st, body = post("/api/rca/infer", rca_payload, timeout=120)
    check("POST /api/rca/infer -> 200", st == 200, body)
    result = body.get("result", {})
    check("has incident_id", bool(result.get("incident_id")), result.get("incident_id"))
    check("has topology", len(result.get("topology", [])) > 0, len(result.get("topology", [])))
    check("has candidates", len(result.get("candidates", [])) > 0, len(result.get("candidates", [])))
    check("has root_cause", result.get("root_cause") is not None)
    check("confidence > 0", (result.get("confidence") or 0) > 0, result.get("confidence"))
    check("has rca_chain", len(result.get("rca_chain", [])) > 0, len(result.get("rca_chain", [])))
    rc = result.get("root_cause") or {}
    print("     incident_id : " + str(result.get("incident_id")))
    print("     root_cause  : [" + str(rc.get("category")) + "] " + str(rc.get("entity_id")))
    print("     confidence  : " + str(result.get("confidence")))
    print("     topology    : " + str(len(result.get("topology", []))) + " nodes")
    print("     candidates  : " + str(len(result.get("candidates", []))))
    print("     elapsed_ms  : " + str(result.get("elapsed_ms")))
except Exception as e:
    check("rca infer", False, e)

# ── 6. Chat flow ──────────────────────────────────────────────────────
print("\n[6] Chat multi-turn")
chat_payload = {
    "messages": [{
        "role": "user",
        "content": "payment-app P99 延迟超过 500ms，MySQL 连接池耗尽",
    }],
    "session_id": "e2e-chat",
}
try:
    st, body = post("/api/chat", chat_payload, timeout=120)
    check("POST /api/chat -> 200", st == 200, body)
    msg = body.get("message", {})
    check("assistant role", msg.get("role") == "assistant", msg.get("role"))
    check("content non-empty", len(msg.get("content", "")) > 0)
    check("incident_id returned", bool(body.get("incident_id")), body.get("incident_id"))
    iid = body.get("incident_id")
    print("     incident_id: " + str(iid))
    print("     content len: " + str(len(msg.get("content", ""))))

    # Follow-up turn (non-alert message)
    st2, body2 = post("/api/chat", {
        "messages": [
            {"role": "user", "content": "payment-app P99 延迟超过 500ms，MySQL 连接池耗尽"},
            {"role": "assistant", "content": msg.get("content", "")},
            {"role": "user", "content": "谢谢"},
        ],
        "session_id": "e2e-chat",
    }, timeout=60)
    check("multi-turn follow-up -> 200", st2 == 200, body2)
    check("follow-up assistant reply", body2.get("message", {}).get("role") == "assistant")

    # History
    st3, body3 = get("/api/chat/history/e2e-chat")
    check("history recorded", len(body3.get("messages", [])) >= 2, len(body3.get("messages", [])))

    # Incident retrieval
    if iid:
        st4, body4 = get("/api/rca/incidents/" + str(iid))
        check("incident retrievable", st4 == 200 and "result" in body4, body4)
        st5, body5 = get("/api/rca/incidents")
        check("incident list", st5 == 200 and body5.get("count", 0) > 0, body5.get("count"))
except Exception as e:
    check("chat", False, e)

# ── Summary ───────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("RESULT: " + str(PASS) + " passed, " + str(FAIL) + " failed")
print("=" * 70)
sys.exit(0 if FAIL == 0 else 1)
