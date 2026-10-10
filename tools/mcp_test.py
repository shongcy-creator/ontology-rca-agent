#!/usr/bin/env python3
"""
直接调 MCP browse_semantics，输出结果 —— 自查 DSH 的 EvoOntology MCP 接线。

为什么不写死解释器与仓库路径：

  本脚本原先硬编码了**某个用户目录下的 DSH 运行时**
  （`C:\\Users\\<user>\\.dsh\\dsh-runtimes\\...\\python.exe`）以及仓库绝对路径。
  两个后果：① 把用户名与家目录布局一起提交进仓库；② 换台机器/换个用户名就跑不起来。

  现在：
    · 解释器 = 环境变量 `CC_PYTHON`（可选）→ 否则 `sys.executable`（**用哪个 Python 跑它就用哪个**）；
    · 其它路径 = 从本文件位置推导（`tools/` 的上一级即仓库根），与宿主目录无关。

用法：
  python tools/mcp_test.py
  CC_PYTHON=/path/to/python python tools/mcp_test.py     # 指定解释器（可选）
"""
import json
import os
import sys
from pathlib import Path
from subprocess import PIPE, Popen

ROOT = Path(__file__).resolve().parent.parent          # <repo>
PY = os.environ.get("CC_PYTHON") or sys.executable     # 不写死用户目录下的运行时
STORE = ROOT / ".evoontology"

msgs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
     "params": {"name": "browse_semantics",
                "arguments": {"query": "容器", "workspace": str(STORE), "kind": "all", "limit": 6}}},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "browse_semantics",
                "arguments": {"query": "payment-app 宿主机", "workspace": str(STORE), "kind": "all", "limit": 6}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "resolve_semantics",
                "arguments": {"mentions": ["t_txn", "慢查询"], "context": "RCA", "workspace": str(STORE)}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
]

# 在原 env 之上叠加（原实现只传这两个键 → 子进程丢掉 SystemRoot 等，属于脆弱的做法）
env = dict(os.environ)
env.update({"PYTHONPATH": str(ROOT / "vendor" / "EvoOntology"),
            "PYTHONIOENCODING": "utf-8"})

p = Popen([PY, "-m", "evoontology.runtime.mcp_server",
           "--store", str(STORE)],
          stdin=PIPE, stdout=PIPE, stderr=PIPE, env=env)

for msg in msgs:
    line = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
    p.stdin.write(line)
    p.stdin.flush()

p.stdin.close()
raw = p.stdout.read().decode("utf-8", errors="replace")
err = p.stderr.read().decode("utf-8", errors="replace")
p.wait()

printed = 0
for line in raw.strip().split("\n"):
    if not line.strip():
        continue
    try:
        r = json.loads(line)
        if r.get("id") in {2, 3, 4, 5}:
            print(json.dumps(r, ensure_ascii=False, indent=2))
            print()
            printed += 1
    except json.JSONDecodeError:
        pass

if not printed:
    print("MCP server 没有返回可用结果（解释器=%s）" % PY, file=sys.stderr)
    if err.strip():
        print(err.strip()[:800], file=sys.stderr)
    raise SystemExit(1)
