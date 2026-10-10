import os as _os
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parent.parent
#!/usr/bin/env python3
import sys, os
sys.path.insert(0, str(_ROOT / "rca-agent"))
os.environ["PYTHONPATH"] = str(_ROOT / "rca-agent")
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PROMETHEUS_URL"] = "http://localhost:9090"
os.environ["EVO_WORKSPACE"] = str(_ROOT / ".evoontology")

from backend.services.rca_engine import RCAEngine

try:
    engine = RCAEngine(
        prometheus_url="http://localhost:9090",
        evo_workspace=str(_ROOT / ".evoontology")
    )
    result = engine.infer(
        message="payment-app P99 延迟超过 500ms，MySQL 连接池耗尽",
        severity="P1",
        inject_evidence=False
    )
    print("incident_id:", result["incident_id"])
    print("elapsed_ms:", result["elapsed_ms"])
    print("confidence:", result["confidence"])
    rc = result.get("root_cause")
    if rc:
        print("root_cause:", rc["category"], rc["entity_id"], "conf=", rc["confidence"])
    print("topology nodes:", len(result.get("topology", [])))
    print("candidates:", len(result.get("candidates", [])))
    print("chain steps:", len(result.get("rca_chain", [])))
except Exception as e:
    import traceback
    traceback.print_exc()
