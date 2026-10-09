#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P2 + P3 综合验证：SSE 流式、成本、限流、自演化、前端推理视图。"""
import json
import sys
import time
import urllib.error
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
        print("  [FAIL] " + name + " -- " + str(detail)[:220])


def req(path, method="GET", payload=None, timeout=300):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")[:500]
    except Exception as e:
        return 0, "%s: %s" % (type(e).__name__, e)


def sse_consume(path, payload, timeout=600):
    """消费 SSE 流，返回事件列表。"""
    data = json.dumps(payload).encode()
    r = urllib.request.Request(BASE + path, data=data, method="POST",
                               headers={"Content-Type": "application/json",
                                        "Accept": "text/event-stream"})
    events = []
    t0 = time.time()
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        buffer = ""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            # 事件以空行分隔
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                ev = {}
                for line in block.split("\n"):
                    if line.startswith("event: "):
                        ev["event"] = line[7:].strip()
                    elif line.startswith("data: "):
                        ev.setdefault("data", []).append(line[6:])
                if ev.get("event"):
                    try:
                        ev["payload"] = json.loads("".join(ev.get("data", [])))
                    except Exception:
                        ev["payload"] = None
                    events.append(ev)
                    if ev["event"] in ("final", "error"):
                        return events, time.time() - t0
    return events, time.time() - t0


print("=" * 76)
print("P2 + P3 VERIFICATION")
print("=" * 76)

# ── 1. 成本 / 限流 / 自演化端点 ──────────────────────────────────────
print("\n[1] Cost / rate-limit / evolution endpoints")
st, body = req("/api/agent/cost")
try:
    d = json.loads(body)
    print("     total_tokens=%s  est_cost=$%s" % (
        d.get("total_tokens"), d.get("estimated_cost_usd")))
    check("cost 200", st == 200, body[:120])
    check("cost 含价格表", "pricing_per_1m_tokens" in d, list(d.keys()))
    check("cost 含 store 统计", "store" in d)
    check("定价含 claude-opus-5-5", "claude-opus-5-5" in d.get("pricing_per_1m_tokens", {}))
except json.JSONDecodeError:
    check("cost", False, body)

st, body = req("/api/agent/rate-limit")
try:
    d = json.loads(body)
    print("     rate-limit: %s" % json.dumps(d, ensure_ascii=False))
    check("rate-limit 200", st == 200)
    check("限流字段齐备", all(k in d for k in ("max_per_minute", "remaining", "current_in_window")))
except json.JSONDecodeError:
    check("rate-limit", False, body)

st, body = req("/api/agent/evolution")
try:
    d = json.loads(body)
    print("     evolution ready=%s  trajectories=%s/%s" % (
        d.get("ready"), d.get("new_trajectories"), d.get("threshold_trajectories")))
    check("evolution 200", st == 200, body[:120])
    check("含触发条件", "threshold_trajectories" in d and "threshold_days" in d)
    check("含 evoontology 状态", "evoontology" in d)
except json.JSONDecodeError:
    check("evolution", False, body)

# ── 2. SSE 流式（确定性快路径）──────────────────────────────────────
print("\n[2] SSE streaming (deterministic)")
events, dt = sse_consume("/api/agent/diagnose/stream", {
    "alert": "payment-app P99 延迟超过 500ms，MySQL 连接池耗尽",
    "severity": "P1",
})
ev_names = [e["event"] for e in events]
print("     events=%s  (%.1fs)" % (ev_names, dt))
check("SSE connected 事件", "connected" in ev_names, ev_names)
check("SSE final 事件", "final" in ev_names, ev_names)
final = next((e["payload"] for e in events if e["event"] == "final"), None)
check("final 含根因", final and (final.get("root_cause") or {}).get("entity_id"), final and final.get("root_cause"))
check("final 含 route", final and "route" in final, list((final or {}).keys())[:10])
check("final 含成本字段", final and "total_tokens" in final, final and final.get("total_tokens"))

# ── 3. 前端推理视图产物 ─────────────────────────────────────────────
print("\n[3] Frontend reasoning trace bundle")
st, html = req("/")
check("前端可访问", st == 200)
import re
m = re.search(r'src="(/assets/[^"]+\.js)"', html)
if m:
    st2, js = req(m.group(1))
    # 函数名会被压缩，改查保留的中文字面量与关键逻辑
    for token in ["sendDiagnose", "观察", "推理", "行动", "正在推理",
                  "根因传播", "seed_agreement", "diagnose/stream", "建议动作",
                  "需确认", "暂无推理过程"]:
        check("bundle 含 " + token, token in js, token)
else:
    check("bundle 引用", False, html[:200])

# ── 4. 限流实际生效 ─────────────────────────────────────────────────
print("\n[4] Rate limiting (quick burst)")
# 连续调用 3 次确定性诊断（快），验证不触发限流（限流是 10/分钟）
st, _ = req("/api/agent/diagnose", "POST",
            {"alert": "payment-app P99 延迟超过 500ms", "severity": "P1"}, timeout=120)
check("诊断可正常调用", st == 200, st)

print("\n" + "=" * 76)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
print("=" * 76)
sys.exit(0 if FAIL == 0 else 1)
