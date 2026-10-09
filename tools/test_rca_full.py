#!/usr/bin/env python
import urllib.request, json

payload = json.dumps({
    "alert": {"alertId": "T", "severity": "P1", "message": "payment-app P99"},
    "session_id": "x"
}).encode()

req = urllib.request.Request(
    "http://localhost:8088/api/rca/infer",
    data=payload,
    headers={"Content-Type": "application/json"}
)
try:
    r = urllib.request.urlopen(req, timeout=60)
    print(r.read().decode()[:500])
except urllib.request.HTTPError as e:
    body = e.read().decode()
    print("=== FULL ERROR BODY ===")
    print(body)
    print("=== END ===")
