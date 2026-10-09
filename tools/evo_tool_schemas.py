#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""查询 EvoOntology 演化协议工具的输入 schema。"""
import json, subprocess, os

EVO = r"D:\05_code\credit-card-sys-ops\vendor\EvoOntology"
STORE = r"D:\05_code\credit-card-sys-ops\.evoontology"
env = {**os.environ}
env["PYTHONPATH"] = EVO
env["PYTHONIOENCODING"] = "utf-8"

init = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18"}}).encode()
listreq = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                      "params": {}}).encode()

p = subprocess.run(["python", "-m", "evoontology.runtime.mcp_server", "--store", STORE],
                   input=init + b"\n" + listreq + b"\n",
                   capture_output=True, env=env, timeout=60)

tools = []
for line in p.stdout.decode("utf-8", errors="replace").splitlines():
    try:
        d = json.loads(line)
        if d.get("id") == 1:
            tools = d["result"]["tools"]
            break
    except Exception:
        pass

targets = ["start_ontology_task", "record_ontology_task_event", "finish_ontology_task",
           "start_evolution_run", "record_evolution_evaluation", "begin_evolution_round",
           "finalize_evolution_run", "accept_evolution"]

for t in tools:
    name = t.get("name", "")
    if name in targets:
        print("=" * 70)
        print("###", name)
        print((t.get("description") or "")[:240])
        schema = t.get("inputSchema", {})
        print(json.dumps(schema, ensure_ascii=False, indent=1)[:600])
        print()
