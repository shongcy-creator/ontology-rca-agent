#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证 Agentic 路径的 SSE 完整事件流（reasoning/tool_call/observation）。"""
import json
import sys
import time
import urllib.request

BASE = "http://localhost:3001"
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + name)
    else:
        FAIL += 1
        print("  [FAIL] " + name + " -- " + str(detail)[:200])


def consume(path, payload, timeout=600):
    data = json.dumps(payload).encode()
    r = urllib.request.Request(BASE + path, data=data, method="POST",
                               headers={"Content-Type": "application/json",
                                        "Accept": "text/event-stream"})
    events = []
    t0 = time.time()
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        buffer = ""
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                ev_name = None
                data_lines = []
                for line in block.split("\n"):
                    if line.startswith("event: "):
                        ev_name = line[7:].strip()
                    elif line.startswith("data: "):
                        data_lines.append(line[6:])
                if not ev_name:
                    continue
                try:
                    payload = json.loads("".join(data_lines)) if data_lines else None
                except Exception:
                    payload = None
                events.append((ev_name, payload))
                if ev_name in ("final", "error"):
                    return events, time.time() - t0
    return events, time.time() - t0


print("=" * 76)
print("AGENTIC SSE STREAM VERIFICATION")
print("=" * 76)

events, dt = consume("/api/agent/diagnose/stream", {
    "alert": "payment-app P99 延迟超过 500ms，MySQL 连接池耗尽",
    "severity": "P1",
    "mode": "agentic",
})

names = [e for e, _ in events]
from collections import Counter
cnt = Counter(names)
print("事件统计: %s" % json.dumps(dict(cnt), ensure_ascii=False))
print("总耗时: %.1fs" % dt)
print()

# 关键断言
check("connected 事件", "connected" in names)
check("route 事件", "route" in names)
check("run_start 事件", "run_start" in names)
check("reasoning 事件（推理流出）", "reasoning" in names, names)
check("tool_call 事件（工具调用流出）", "tool_call" in names, names)
check("observation 事件（观察流出）", "observation" in names, names)
check("final 事件", "final" in names)

# 推理内容非空
reasons = [p for e, p in events if e == "reasoning" and p and p.get("reasoning")]
check("推理内容非空", len(reasons) > 0, len(reasons))
if reasons:
    print("\n推理样例: %s" % reasons[0].get("reasoning", "")[:200].replace("\n", " "))

# 工具调用清单
tools = [p.get("name") for e, p in events if e == "tool_call" and p]
print("\n工具调用序列: %s" % (", ".join(tools) if tools else "无"))

# final 完整性
final = next((p for e, p in events if e == "final"), None)
check("final 有根因", final and (final.get("root_cause") or {}).get("entity_id"))
check("final 有证据链", final and len(final.get("evidence") or []) > 0,
      final and len(final.get("evidence") or []))
check("final 有 run_id", final and bool(final.get("run_id")))
check("final 有交叉验证", final and final.get("seed_agreement") is not None)

print()
print("根因: [%s] %s  conf=%s  seed_agreement=%s(%s)" % (
    (final or {}).get("root_cause") or {} and (final["root_cause"].get("category")),
    (final or {}).get("root_cause") or {} and (final["root_cause"].get("entity_id")),
    (final or {}).get("confidence"),
    (final or {}).get("seed_agreement"),
    (final or {}).get("seed_agreement_level"),
))

print()
print("=" * 76)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
print("=" * 76)
sys.exit(0 if FAIL == 0 else 1)
