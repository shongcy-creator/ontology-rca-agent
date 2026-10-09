#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""容器化 Agent 端到端验证（通过 nginx 前端入口）。"""
import json
import sys
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


def req(path, method="GET", payload=None, timeout=240):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", errors="replace")[:400]}
    except Exception as e:
        return 0, {"error": "%s: %s" % (type(e).__name__, e)}


print("=" * 76)
print("CONTAINERIZED AGENT VERIFICATION")
print("=" * 76)

# ── 1. 健康检查 ───────────────────────────────────────────────────────
print("\n[1] Agent health")
st, d = req("/api/agent/health")
llm = d.get("llm") or {}
store = d.get("store") or {}
print("     llm.ok=%s model=%s latency=%sms" % (
    llm.get("ok"), llm.get("model"), llm.get("latency_ms")))
print("     store.backend=%s runs=%s thoughts=%s" % (
    store.get("backend"), store.get("total_runs"), store.get("total_thoughts")))
check("health 200", st == 200, d)
check("LLM 连通", llm.get("ok") is True, llm)
check("存储为 mysql", store.get("backend") == "mysql", store)

# ── 2. 工具目录 ───────────────────────────────────────────────────────
print("\n[2] Tool catalog")
st, d = req("/api/agent/tools")
print("     total=%s layers=%s" % (
    d.get("total"), json.dumps({k: len(v) for k, v in (d.get("layers") or {}).items()},
                               ensure_ascii=False)))
check("25 个工具", d.get("total") == 25, d.get("total"))
check("四层齐备", set((d.get("layers") or {}).keys()) == {"ontology", "metrics", "env", "knowledge"},
      list((d.get("layers") or {}).keys()))
check("策略为只读", (d.get("policy") or {}).get("read_only") is True, d.get("policy"))

# ── 3. 确定性快路径（应被路由选中）────────────────────────────────────
print("\n[3] Deterministic route (auto)")
st, d = req("/api/agent/diagnose", "POST", {
    "alert": "payment-app P99 延迟超过 500ms，MySQL 连接池耗尽",
    "severity": "P1",
}, timeout=300)
route = d.get("route") or {}
print("     ok=%s mode=%s status=%s confidence=%s" % (
    d.get("ok"), d.get("mode"), d.get("status"), d.get("confidence")))
print("     route=%s" % str(route.get("reason"))[:110])
print("     root=[%s] %s" % ((d.get("root_cause") or {}).get("category"),
                            (d.get("root_cause") or {}).get("entity_id")))
check("诊断成功", d.get("ok") is True, d)
check("路由选了确定性路径", d.get("mode") == "deterministic", d.get("mode"))
check("零 token", d.get("total_tokens") == 0, d.get("total_tokens"))
check("有根因", bool((d.get("root_cause") or {}).get("entity_id")))
check("有路由依据", bool(route.get("reason")))
check("已落库", d.get("persisted") is True, d.get("persisted"))

# ── 4. 强制 Agent 路径（验证容器内环境工具）──────────────────────────
print("\n[4] Agentic route (forced) - verifies env tools in container")
st, d = req("/api/agent/diagnose", "POST", {
    "alert": "payment-app P99 延迟超过 500ms，MySQL 连接池耗尽",
    "severity": "P1",
    "mode": "agentic",
}, timeout=600)
tools = d.get("tools_used") or []
print("     status=%s steps=%s tokens=%s confidence=%s" % (
    d.get("status"), d.get("steps_used"), d.get("total_tokens"), d.get("confidence")))
print("     tools_used(%d)=%s" % (len(tools), ", ".join(tools)))
print("     root=[%s] %s  seed_agreement=%s(%s)" % (
    (d.get("root_cause") or {}).get("category"),
    (d.get("root_cause") or {}).get("entity_id"),
    d.get("seed_agreement"), d.get("seed_agreement_level")))
check("agentic 完成", d.get("status") in ("completed", "timeout", "budget_exceeded"), d.get("status"))
check("调用了工具", len(tools) >= 1, tools)
check("有证据链", len(d.get("evidence") or []) > 0, len(d.get("evidence") or []))
# 容器内环境工具（需 docker socket + docker-cli）
env_tools = [t for t in tools if t.startswith("env_") or t.startswith("db_")]
check("使用了环境/数据库取证工具", len(env_tools) >= 1, tools)

run_id = d.get("run_id")
check("有 run_id", bool(run_id), run_id)

# ── 5. 回放 ───────────────────────────────────────────────────────────
print("\n[5] Replay")
if run_id:
    st, r = req("/api/agent/runs/" + run_id)
    check("run 可回放", st == 200 and bool(r.get("run")), r)
    thoughts = r.get("thoughts") or []
    phases = {t.get("phase") for t in thoughts}
    print("     thoughts=%d phases=%s" % (len(thoughts), sorted(x for x in phases if x)))
    check("thoughts 含 reason", "reason" in phases, phases)
    check("thoughts 含 act", "act" in phases, phases)
    if thoughts:
        sample = [t for t in thoughts if t.get("phase") == "reason" and t.get("reasoning")]
        if sample:
            print("     推理样例: %s" % sample[0]["reasoning"][:150].replace("\n", " "))

st, d2 = req("/api/agent/runs?limit=5")
check("run 列表可用", len(d2.get("runs") or []) > 0, len(d2.get("runs") or []))

st, d3 = req("/api/agent/stats")
print("     stats: %s" % json.dumps(d3, ensure_ascii=False)[:220])
check("统计数据可用", d3.get("backend") == "mysql", d3.get("backend"))

print("\n" + "=" * 76)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
print("=" * 76)
sys.exit(0 if FAIL == 0 else 1)
