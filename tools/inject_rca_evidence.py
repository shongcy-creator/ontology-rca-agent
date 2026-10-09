#!/usr/bin/env python3
"""
将 RCA 推理结果注入 EvoOntology ontology_v0 工作区，
作为 Evidence 节点 + Relation/Term evidence_refs 关联，
生成 ontology_v0-rca 新版本，然后可视化。

Evidence 关联拓扑：
  ev:rca-alert-p99      → app:payment-app, db:mysql-core
  ev:rca-topology-path  → rel:app-accesses-db
  ev:rca-root-cause     → rel:app-accesses-db, rc:slow-sql
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "vendor" / "EvoOntology"))
from evoontology.ontology.store import SemanticStore
from evoontology.validate import validate

WORKSPACE    = Path(__file__).parent.parent / ".evoontology"
BASE_VERSION = "ontology_v0"
NEW_VERSION  = "ontology_v0-rca"


def main():
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── 1. 构造 3 条 Evidence ────────────────────────────────
    ev_alert = {
        "id": "ev:rca-alert-p99",
        "source": "Prometheus monitoring",
        "query": "cc_http_request_duration_seconds P99 > 0.5s for 5min",
        "result": json.dumps({
            "http_total_requests": 168,
            "http_p99_gt_500ms_pct": 100,
            "mysql_threads_connected": 2,
            "mysql_max_used_conn": 3,
            "incident_id": "INC-ALERT-P99-1790828814",
            "severity": "P1",
            "alert": "payment-app P99 latency > 500ms, MySQL connection pool exhausted"
        }),
        "validation_method": "Prometheus query + RCA engine",
        "timestamp": ts,
    }

    ev_topology = {
        "id": "ev:rca-topology-path",
        "source": "EvoOntology resolve_semantics",
        "query": "payment-app container host MySQL database table",
        "result": json.dumps({
            "topology_path": [
                "app:payment-app",
                "rel:app-runs-on-container",
                "env:container",
                "rel:app-accesses-db",
                "db:mysql-core",
                "ds:pay",
            ],
            "node_count": 6,
        }),
        "validation_method": "EvoOntology MCP browse_semantics",
        "timestamp": ts,
    }

    ev_rootcause = {
        "id": "ev:rca-root-cause",
        "source": "RCA engine inference",
        "query": "P99 latency + connection pool exhausted",
        "result": json.dumps({
            "category": "data",
            "entity_id": "rel:app-accesses-db",
            "confidence": 0.99,
            "reason": "keywords matched: 延迟/latency/P99 + 连接池/pool + MySQL",
            "candidates": [
                {"entity_id": "rel:app-accesses-db", "category": "数据", "confidence": 0.99},
                {"entity_id": "rel:app-runs-on-container", "category": "资源", "confidence": 0.99},
                {"entity_id": "env:container", "category": "资源", "confidence": 0.99},
            ],
            "rca_chain": [
                {"step": 1, "type": "alert",      "description": "[P1] payment-app P99 latency > 500ms"},
                {"step": 2, "type": "topology",   "description": "拓扑逆向遍历发现 6 个节点"},
                {"step": 3, "type": "candidate", "description": "[数据] rel:app-accesses-db conf=0.99"},
                {"step": 4, "type": "candidate", "description": "[资源] rel:app-runs-on-container conf=0.99"},
                {"step": 5, "type": "candidate", "description": "[资源] env:container conf=0.99"},
            ]
        }),
        "validation_method": "RCA engine confidence scoring",
        "timestamp": ts,
    }

    # ── 2. 读取原始 records ────────────────────────────────
    loaded_version, records = SemanticStore.load_records(WORKSPACE, version=BASE_VERSION)
    print("Loaded base version: {}".format(loaded_version))
    print("  terms: {}  relations: {}  evidence: {}".format(
        len(records.get("terms", [])),
        len(records.get("relations", [])),
        len(records.get("evidence", [])),
    ))

    # ── 3. 添加 Evidence 记录 ─────────────────────────────
    records.setdefault("evidence", []).extend([ev_alert, ev_topology, ev_rootcause])
    print("\n  + ev:rca-alert-p99       (Prometheus monitoring)")
    print("  + ev:rca-topology-path   (EvoOntology topology)")
    print("  + ev:rca-root-cause      (RCA engine inference)")

    # ── 4. 追加 evidence_refs ──────────────────────────────
    def add_ref(records, family, item_id, ev_id):
        for item in records.get(family, []):
            if item.get("id") == item_id:
                refs = item.setdefault("evidence_refs", [])
                if ev_id not in refs:
                    refs.append(ev_id)
                    print("  + {} -> {}".format(item_id, ev_id))
                return True
        print("  ! {} not found in {}".format(item_id, family))
        return False

    add_ref(records, "relations", "rel:app-accesses-db", "ev:rca-root-cause")
    add_ref(records, "relations", "rel:app-accesses-db", "ev:rca-topology-path")
    add_ref(records, "terms",     "app:payment-app", "ev:rca-alert-p99")
    add_ref(records, "terms",     "db:mysql-core",   "ev:rca-alert-p99")
    add_ref(records, "terms",     "rc:slow-sql",     "ev:rca-root-cause")

    # ── 5. 保存为新版本 ───────────────────────────────────
    version_dir = SemanticStore.save_version(WORKSPACE, NEW_VERSION, records)
    print("\nSaved to: {}".format(version_dir))

    # ── 6. 设为 active ────────────────────────────────────
    SemanticStore.set_active(WORKSPACE, NEW_VERSION)
    print("Active version set to: {}".format(NEW_VERSION))

    # ── 7. 验证 ───────────────────────────────────────────
    result = validate(Path(version_dir))
    if result.get("passed"):
        print("Validation: PASS")
    else:
        errs = result.get("errors", [])
        print("Validation errors ({}):".format(len(errs)))
        for e in errs[:5]:
            print("  ! {}".format(e))
    return NEW_VERSION


if __name__ == "__main__":
    main()
