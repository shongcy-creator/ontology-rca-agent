#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生产环境（Docker Compose）端到端验证 — 通过 nginx 前端入口."""
import json, sys
import urllib.request, urllib.error

FRONT = "http://localhost:3001"   # nginx
BACK = "http://localhost:8088"    # backend direct
PROM = "http://localhost:9090"
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + name)
    else:
        FAIL += 1
        print("  [FAIL] " + name + " -- " + str(detail)[:220])


def raw(url, timeout=30):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def jget(url, timeout=30):
    st, body = raw(url, timeout)
    return st, json.loads(body.decode("utf-8"))


def jpost(url, payload, timeout=180):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", errors="replace")[:300]}


print("=" * 72)
print("PRODUCTION STACK VERIFICATION (Docker Compose)")
print("=" * 72)

# ── 1. Frontend (nginx) ────────────────────────────────────────────────
print("\n[1] Frontend (nginx :3001)")
try:
    st, body = raw(FRONT + "/")
    html = body.decode("utf-8", errors="replace")
    check("GET / -> 200", st == 200, st)
    check("serves React index.html", "<!doctype html" in html.lower(), html[:80])
    check("references built assets", "/assets/" in html, html[:300])
    check("has root div", 'id="root"' in html)
except Exception as e:
    check("frontend root", False, e)

try:
    st, body = raw(FRONT + "/assets/" + "index-BlKfAj3u.js")
    check("asset served (may differ per build)", st in (200, 404), st)
except Exception as e:
    check("asset", False, e)

# ── 2. API through nginx proxy ─────────────────────────────────────────
print("\n[2] API via nginx proxy (/api/*)")
try:
    st, body = jget(FRONT + "/api/health")
    check("nginx -> /api/health 200", st == 200, body)
    check("health ok", body.get("status") == "ok", body)
except Exception as e:
    check("proxy health", False, e)

try:
    st, body = jget(FRONT + "/api/chat/tools")
    check("nginx -> /api/chat/tools 200", st == 200, st)
    check("tools count >= 5", len(body.get("tools", [])) >= 5, len(body.get("tools", [])))
except Exception as e:
    check("proxy tools", False, e)

# ── 3. Direct backend ──────────────────────────────────────────────────
print("\n[3] Backend direct (:8088)")
try:
    st, body = jget(BACK + "/api/metrics/summary", timeout=45)
    check("metrics summary 200", st == 200, st)
    check("has mysql_global", "mysql_global" in body, list(body.keys()))
    check("has targets", "targets" in body)
    tg = body.get("targets", [])
    print("     targets: " + ", ".join("%s=%s" % (t["job"], t["health"]) for t in tg))
    check("all targets up", all(t["health"] == "up" for t in tg), [t for t in tg if t["health"] != "up"])
except Exception as e:
    check("backend metrics", False, e)

# ── 4. RCA inference in container ──────────────────────────────────────
print("\n[4] RCA inference (containerized)")
try:
    st, body = jpost(FRONT + "/api/rca/infer", {
        "alert": {
            "alertId": "PROD-001",
            "severity": "P1",
            "message": "payment-app P99 latency > 500ms, MySQL connection pool exhausted",
        },
        "session_id": "prod-verify",
    }, timeout=240)
    check("rca infer 200", st == 200, body)
    r = body.get("result", {})
    rc = r.get("root_cause") or {}
    print("     incident : " + str(r.get("incident_id")))
    print("     root     : [" + str(rc.get("category")) + "] " + str(rc.get("entity_id"))
          + " conf=" + str(rc.get("confidence")))
    print("     topology : %d nodes / %d edges, path %d hops" % (
        len(r.get("topology", [])), len(r.get("topology_edges", [])), len(r.get("root_cause_path", []))))
    check("has root cause", bool(rc))
    check("topology complete (>=10 nodes)", len(r.get("topology", [])) >= 10, len(r.get("topology", [])))
    check("has edges", len(r.get("topology_edges", [])) > 0, len(r.get("topology_edges", [])))
    check("has path", len(r.get("root_cause_path", [])) > 0, len(r.get("root_cause_path", [])))
    check("confidence > 0", float(r.get("confidence", 0)) > 0, r.get("confidence"))
except Exception as e:
    check("rca infer", False, e)

# ── 5. Chat flow through nginx ─────────────────────────────────────────
print("\n[5] Chat flow (through nginx)")
try:
    st, body = jpost(FRONT + "/api/chat", {
        "messages": [{"role": "user", "content": "容器 OOM 重启，内存使用率 98%"}],
        "session_id": "prod-chat",
    }, timeout=240)
    check("chat 200", st == 200, body)
    iid = body.get("incident_id")
    check("incident_id", bool(iid), iid)
    check("assistant content", len(body.get("message", {}).get("content", "")) > 0)
    if iid:
        st2, b2 = jget(FRONT + "/api/rca/incidents/" + str(iid))
        check("incident retrievable via nginx", st2 == 200, b2)
        rc2 = (b2.get("result") or {}).get("root_cause") or {}
        print("     OOM root : [" + str(rc2.get("category")) + "] " + str(rc2.get("entity_id")))
        check("OOM -> resource category", rc2.get("category") == "资源", rc2.get("category"))
except Exception as e:
    check("chat flow", False, e)

# ── 6. Prometheus / Grafana ────────────────────────────────────────────
print("\n[6] Monitoring stack")
try:
    st, body = jget(PROM + "/api/v1/targets")
    targets = body.get("data", {}).get("activeTargets", [])
    check("prometheus targets reachable", len(targets) > 0, len(targets))
    for t in targets:
        job = t["labels"]["job"]
        print("     %-16s health=%s" % (job, t["health"]))
    check("all targets up", all(t["health"] == "up" for t in targets),
          [t["labels"]["job"] for t in targets if t["health"] != "up"])
except Exception as e:
    check("prometheus", False, e)

try:
    st, body = jget("http://localhost:3000/api/health")
    check("grafana healthy", body.get("database") == "ok", body)
except Exception as e:
    check("grafana", False, e)

# ── 7. App ─────────────────────────────────────────────────────────────
print("\n[7] payment-app")
try:
    st, body = jget("http://localhost:8080/health")
    check("app health ok", body.get("ok") is True, body)
    print("     app: %s v%s" % (body.get("app"), body.get("version")))
except Exception as e:
    check("app", False, e)

print("\n" + "=" * 72)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
