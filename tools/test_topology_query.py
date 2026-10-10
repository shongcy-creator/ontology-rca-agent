#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试 EvoOntology MCP 的拓扑查询能力：browse vs resolve"""
import sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # <repo>，不写死宿主绝对路径
sys.path.insert(0, str(ROOT / "rca-agent"))
from backend.services.evo_ontology import EvoOntologyClient

c = EvoOntologyClient()

print("=" * 70)
print("1) browse_semantics('payment-app 容器 宿主机 MySQL 数据库 表 交易')")
r1 = c.browse_semantics("payment-app 容器 宿主机 MySQL 数据库 表 交易", limit=30)
items = r1.get("items", [])
print("   items:", len(items))
for it in items:
    print("   -", it.get("id"), "|", it.get("type"))

print()
print("=" * 70)
print("2) resolve_semantics(['app:payment-app'])")
r2 = c.resolve_semantics(["app:payment-app"], context="topology")
print("   keys:", list(r2.keys()))
results = r2.get("results", [])
print("   results:", len(results))
for item in results:
    term = item.get("term", {})
    print("   -", term.get("id"), "| status:", item.get("status"))
    for rel in (item.get("relations") or []):
        print("       rel:", rel.get("id"), "|", rel.get("relation_type"), "|", rel.get("target") or rel.get("to"))

print()
print("=" * 70)
print("3) resolve_semantics(['t_txn', 'db:mysql-core', 'env:container', 'ds:pay'])")
r3 = c.resolve_semantics(["t_txn", "db:mysql-core", "env:container", "ds:pay"], context="topology")
for item in (r3.get("results") or []):
    term = item.get("term", {})
    rels = item.get("relations") or []
    print("   -", term.get("id"), "| rels:", len(rels))
    for rel in rels:
        print("       ", json.dumps(rel, ensure_ascii=False)[:150])
