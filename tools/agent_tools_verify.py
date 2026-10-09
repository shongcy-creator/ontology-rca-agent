#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""工具层验证：逐个调用工具，检查能否真实取证。"""
import asyncio
import json
import os
import sys

sys.path.insert(0, r"D:\05_code\credit-card-sys-ops\rca-agent")

# 本地开发：连宿主机映射端口
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("PROMETHEUS_URL", "http://localhost:9090")
os.environ.setdefault("EVO_WORKSPACE", r"D:\05_code\credit-card-sys-ops\.evoontology")
os.environ.setdefault("PYTHON_EXE", sys.executable)
# 取证账号（最小权限只读）
os.environ.setdefault("RCA_DB_USER", "rca_readonly")
os.environ.setdefault("RCA_DB_PASSWORD", "rca_readonly_pwd")

# 控制台/管道统一 UTF-8，避免 ⚠ 等字符触发 GBK 编码错误
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend.services.tools import build_default_registry   # noqa: E402

PASS = FAIL = 0
RESULTS = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + name)
    else:
        FAIL += 1
        print("  [FAIL] " + name + " -- " + str(detail)[:220])


async def run_tool(reg, name, args=None):
    r = await reg.invoke(name, args or {})
    RESULTS.append((name, r.ok, r.meta.get("latency_ms"), r.summary))
    return r


async def main():
    print("=" * 76)
    print("TOOL LAYER VERIFICATION (live)")
    print("=" * 76)

    reg = build_default_registry()

    # ── 本体层 ────────────────────────────────────────────────────
    print("\n[1] Ontology tools")
    r = await run_tool(reg, "ontology_topology")
    print("     %s" % r.summary[:130])
    check("ontology_topology ok", r.ok, r.error)
    check("topology has nodes+edges",
          r.ok and len(r.data.get("nodes", [])) >= 10 and len(r.data.get("edges", [])) > 0,
          (len(r.data.get("nodes", [])) if r.ok else 0))

    r = await run_tool(reg, "ontology_constraints")
    print("     %s" % r.summary[:150])
    check("ontology_constraints ok", r.ok, r.error)
    check("constraints loaded", r.ok and len(r.data.get("constraints", [])) > 0)

    r = await run_tool(reg, "ontology_diagnose",
                       {"message": "payment-app P99 延迟超过 500ms，MySQL 连接池耗尽"})
    print("     %s" % r.summary[:150])
    check("ontology_diagnose ok", r.ok, r.error)
    check("diagnose has root cause", r.ok and bool((r.data or {}).get("root_cause")))

    r = await run_tool(reg, "ontology_search", {"query": "连接池"})
    check("ontology_search ok", r.ok, r.error)
    print("     %s" % r.summary[:120])

    # ── 监控层 ────────────────────────────────────────────────────
    print("\n[2] Metrics tools")
    r = await run_tool(reg, "metrics_summary")
    print("     %s" % r.summary[:160])
    check("metrics_summary ok", r.ok, r.error)
    check("metrics_summary has pool", r.ok and "mysql_pool" in (r.data or {}))

    r = await run_tool(reg, "metrics_targets")
    print("     %s" % r.summary[:130])
    check("metrics_targets ok", r.ok, r.error)

    r = await run_tool(reg, "metrics_alerts")
    print("     %s" % r.summary[:130])
    check("metrics_alerts ok", r.ok, r.error)

    r = await run_tool(reg, "metrics_query",
                       {"query": 'cc_mysql_pool_active{app="payment-app"}'})
    print("     %s" % r.summary[:120])
    check("metrics_query ok", r.ok, r.error)

    r = await run_tool(reg, "metrics_range",
                       {"query": 'cc_http_request_duration_seconds_count', "minutes": 30})
    print("     %s" % r.summary[:120])
    check("metrics_range ok", r.ok, r.error)

    # ── 数据库层 ──────────────────────────────────────────────────
    print("\n[3] DB forensics tools")
    r = await run_tool(reg, "db_status")
    print("     %s" % r.summary[:180])
    check("db_status ok", r.ok, r.error)
    check("db_status has warnings field", r.ok and "warnings" in (r.data or {}))

    r = await run_tool(reg, "db_processlist", {"min_time": 0})
    print("     %s" % r.summary[:150])
    check("db_processlist ok", r.ok, r.error)
    check("processlist returns rows", r.ok and "processes" in (r.data or {}))

    r = await run_tool(reg, "db_innodb_status")
    print("     %s" % r.summary[:150])
    check("db_innodb_status ok", r.ok, r.error)

    r = await run_tool(reg, "db_slow_queries", {"limit": 10})
    print("     %s" % r.summary[:160])
    check("db_slow_queries ok", r.ok, r.error)

    r = await run_tool(reg, "db_variables")
    print("     %s" % r.summary[:170])
    check("db_variables ok", r.ok, r.error)
    check("max_connections present", r.ok and (r.data or {}).get("max_connections"))

    r = await run_tool(reg, "db_table_info", {"table": "t_txn"})
    print("     %s" % r.summary[:140])
    check("db_table_info ok", r.ok, r.error)

    r = await run_tool(reg, "db_explain",
                       {"sql": "SELECT * FROM t_txn WHERE customer_id=1"})
    print("     %s" % r.summary[:150])
    check("db_explain ok", r.ok, r.error)

    # ── 环境层（Docker）───────────────────────────────────────────
    print("\n[4] Env tools (docker)")
    r = await run_tool(reg, "env_containers")
    print("     %s" % r.summary[:160])
    check("env_containers ok", r.ok, r.error)
    if r.ok:
        check("found containers", len(r.data.get("containers", [])) >= 5,
              len(r.data.get("containers", [])))

    r = await run_tool(reg, "env_container_inspect", {"container": "cc-credit-card-app"})
    print("     %s" % r.summary[:150])
    check("env_container_inspect ok", r.ok, r.error)
    if r.ok:
        check("inspect env redacted",
              all("***" in e for e in r.data.get("env", []) if "PASSWORD" in e.upper()),
              r.data.get("env"))

    r = await run_tool(reg, "env_container_logs", {"container": "cc-credit-card-app", "tail": 30})
    print("     %s" % r.summary[:140])
    check("env_container_logs ok", r.ok, r.error)

    r = await run_tool(reg, "env_container_stats")
    print("     %s" % r.summary[:140])
    check("env_container_stats ok", r.ok, r.error)

    r = await run_tool(reg, "env_host_info")
    print("     %s" % r.summary[:120])
    check("env_host_info ok", r.ok, r.error)

    # ── 知识层 ────────────────────────────────────────────────────
    print("\n[5] Knowledge tools")
    r = await run_tool(reg, "history_search", {"limit": 5})
    print("     %s" % r.summary[:150])
    check("history_search ok", r.ok, r.error)

    r = await run_tool(reg, "history_feedback", {"limit": 10})
    print("     %s" % r.summary[:170])
    check("history_feedback ok", r.ok, r.error)

    r = await run_tool(reg, "ontology_evidence", {"limit": 5})
    print("     %s" % r.summary[:130])
    check("ontology_evidence ok", r.ok, r.error)

    # ── 安全校验 ──────────────────────────────────────────────────
    print("\n[6] Security guards")
    r = await run_tool(reg, "nonexistent_tool", {})
    check("unknown tool returns error not raise", not r.ok and "不存在" in r.error, r.error)

    r = await reg.invoke("env_container_logs", {"container": "cc-app; rm -rf /"})
    check("injection blocked", not r.ok, r.summary)

    r = await reg.invoke("db_explain", {"sql": "DELETE FROM t_txn"})
    check("write SQL blocked", not r.ok, r.error or r.summary)
    check("write SQL block reason mentions SELECT",
          "SELECT" in (r.error + r.summary), r.error or r.summary)

    r = await reg.invoke("db_explain", {"sql": "SELECT 1; DROP TABLE t_txn"})
    check("stacked SQL blocked", not r.ok, r.error or r.summary)

    r = await reg.invoke("db_table_info", {"table": "t_txn", "evil": "1"})
    check("unknown arg rejected", not r.ok, r.error)

    r = await reg.invoke("db_table_info", {})
    check("missing required arg rejected", not r.ok, r.error)

    # 类型强制转换：int 12345 -> "12345"（非法 PromQL），应优雅失败而非抛异常
    r = await reg.invoke("metrics_query", {"query": 12345})
    check("type coercion + graceful failure on bad PromQL",
          r.ok or bool(r.error or r.summary), (r.ok, r.error[:80] if r.error else r.summary[:80]))

    # ── 汇总 ──────────────────────────────────────────────────────
    print("\n[7] Tool invocation summary")
    # 排除故意构造的负向用例
    negative = {"nonexistent_tool"}
    live = [(n, ok) for n, ok, _, _ in RESULTS if n not in negative]
    ok_count = sum(1 for _, ok in live if ok)
    print("     invoked=%d (negative=%d) ok=%d fail=%d" % (
        len(live), len(negative), ok_count, len(live) - ok_count))
    print("     registry layers: %s" % json.dumps(reg.stats()["layers"], ensure_ascii=False))
    check("all live tools succeeded", ok_count == len(live),
          [n for n, ok in live if not ok])

    print("\n" + "=" * 76)
    print("RESULT: %d passed, %d failed" % (PASS, FAIL))
    print("=" * 76)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
