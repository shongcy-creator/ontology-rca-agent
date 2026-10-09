#!/usr/bin/env python
import urllib.request, json, sys

payload = json.dumps({
    "alert": {
        "alertId": "API-TEST-001",
        "severity": "P1",
        "message": "payment-app P99 延迟超过 500ms，MySQL 连接池耗尽"
    },
    "session_id": "prod_test"
}).encode()

req = urllib.request.Request(
    "http://localhost:8088/api/rca/infer",
    data=payload,
    headers={"Content-Type": "application/json"}
)

try:
    r = urllib.request.urlopen(req, timeout=60)
    d = json.loads(r.read())
    result = d.get("result", {})
    print("incident_id :", result.get("incident_id", "?"))
    print("elapsed_ms :", result.get("elapsed_ms", "?"))
    print("confidence :", result.get("confidence", "?"))
    rc = result.get("root_cause") or {}
    print("root_cause :", rc.get("category", "?"), rc.get("entity_id", "?"), "conf=", rc.get("confidence", "?"))
    print("topology   :", len(result.get("topology", [])), "nodes")
    print("candidates :", len(result.get("candidates", [])))
    print("chain steps:", len(result.get("rca_chain", [])))
    print()
    print("thresholds triggered:")
    for t in result.get("thresholds_triggered", []):
        print(" ", t.get("rule", "?"), t.get("status", "?"))
    print()
    print("OK")
except urllib.request.HTTPError as e:
    print("HTTP", e.code)
    body = e.read().decode()
    print("Body:", body[:500])
except Exception as e:
    import traceback
    traceback.print_exc()
