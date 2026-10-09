#!/usr/bin/env python3
"""直接调 MCP browse_semantics，输出结果"""
import json, sys
from subprocess import Popen, PIPE

py = r"C:\Users\41187\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
store = r"D:\05_code\credit-card-sys-ops\.evoontology"

msgs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
     "params": {"name": "browse_semantics",
                "arguments": {"query": "容器", "workspace": store, "kind": "all", "limit": 6}}},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "browse_semantics",
                "arguments": {"query": "payment-app 宿主机", "workspace": store, "kind": "all", "limit": 6}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "resolve_semantics",
                "arguments": {"mentions": ["t_txn", "慢查询"], "context": "RCA", "workspace": store}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
]

env = {"PYTHONPATH": r"D:\05_code\credit-card-sys-ops\vendor\EvoOntology",
       "PYTHONIOENCODING": "utf-8"}

p = Popen([py, "-m", "evoontology.runtime.mcp_server",
           "--store", store],
          stdin=PIPE, stdout=PIPE, stderr=PIPE, env=env)

out_lines = []
for msg in msgs:
    line = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
    p.stdin.write(line)
    p.stdin.flush()

p.stdin.close()
raw = p.stdout.read().decode("utf-8", errors="replace")
p.wait()

for line in raw.strip().split("\n"):
    if not line.strip():
        continue
    try:
        r = json.loads(line)
        if r.get("id") in {2, 3, 4, 5}:
            print(json.dumps(r, ensure_ascii=False, indent=2))
            print()
    except json.JSONDecodeError:
        pass
