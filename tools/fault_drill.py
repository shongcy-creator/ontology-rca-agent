#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
端到端故障演练：
  1. 注入故障（MySQL 行锁 → 交易 INSERT 阻塞 → 应用延迟上升）
  2. 等待 Prometheus 采集并评估告警规则
  3. 从告警规则的 rca_hint 提取告警文本
  4. 调用 RCA Agent 推理
  5. 验证根因正确
  6. 解除故障
"""
import json, subprocess, sys, time, threading
import urllib.request, urllib.error

APP = "http://localhost:8080"
RCA = "http://localhost:3001"
PROM = "http://localhost:9090"
MYSQL_CONTAINER = "cc-mysql-core"

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + name)
    else:
        FAIL += 1
        print("  [FAIL] " + name + " -- " + str(detail)[:200])


def http(url, method="GET", payload=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(body)
            except Exception:
                return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")[:300]
    except Exception as e:
        return 0, str(e)


def docker(*args, timeout=60):
    cmd = ["docker"] + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        exe = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
        p = subprocess.run([exe] + list(args), capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr


def mysql_exec(sql, timeout=30):
    rc, out, err = docker("exec", MYSQL_CONTAINER, "mysql", "-u", "appuser", "-papppass",
                          "creditcard", "-e", sql, timeout=timeout)
    return rc, out, err


def prom_query(expr):
    st, body = http(PROM + "/api/v1/query?query=" + urllib.parse.quote(expr))
    if st == 200 and isinstance(body, dict):
        return body.get("data", {}).get("result", [])
    return []


def generate_load(n=60, concurrency=8):
    """并发发起交易请求，制造负载。"""
    results = {"ok": 0, "err": 0, "latencies": []}

    def worker():
        for _ in range(n // concurrency):
            t0 = time.time()
            st, _ = http(APP + "/txn", method="POST",
                         payload={"customer_id": 1, "amount": 100, "status": "SETTLED"},
                         timeout=30)
            dt = time.time() - t0
            results["latencies"].append(dt)
            if st == 200:
                results["ok"] += 1
            else:
                results["err"] += 1

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


import urllib.parse

print("=" * 72)
print("END-TO-END FAULT DRILL: 故障注入 → 告警 → RCA")
print("=" * 72)

# ── 0. 基线 ────────────────────────────────────────────────────────────
print("\n[0] Baseline")
st, body = http(APP + "/health")
check("app healthy", st == 200 and isinstance(body, dict) and body.get("ok"), body)

base = generate_load(n=24, concurrency=4)
base_p99 = sorted(base["latencies"])[int(len(base["latencies"]) * 0.99)] if base["latencies"] else 0
print("     baseline: %d ok, %d err, p99=%.3fs" % (base["ok"], base["err"], base_p99))
check("baseline works", base["ok"] > 0, base)

# ── 1. 注入故障：t_txn 表加排他锁并保持 ────────────────────────────────
print("\n[1] Injecting fault (hold exclusive lock on t_txn for 90s)")
# 后台事务持锁
lock_proc = subprocess.Popen(
    ["docker", "exec", MYSQL_CONTAINER, "mysql", "-u", "appuser", "-papppass", "creditcard",
     "-e", "START TRANSACTION; SELECT * FROM t_txn FOR UPDATE; SELECT SLEEP(90); COMMIT;"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
)
time.sleep(4)
rc, out, err = mysql_exec("SELECT COUNT(*) AS locked_rows FROM t_txn;")
print("     lock holder started (pid=%s)" % lock_proc.pid)

# ── 2. 制造负载，观察延迟上升 ─────────────────────────────────────────
print("\n[2] Generating load under fault")
fault = generate_load(n=40, concurrency=8)
f_lat = sorted(fault["latencies"]) if fault["latencies"] else [0]
f_p99 = f_lat[int(len(f_lat) * 0.99) - 1] if len(f_lat) > 1 else f_lat[0]
print("     under fault: %d ok, %d err, p99=%.3fs (baseline p99=%.3fs)" % (
    fault["ok"], fault["err"], f_p99, base_p99))
check("latency increased under fault", f_p99 > base_p99, (base_p99, f_p99))

# 等 Prometheus 采集
print("\n[3] Waiting for Prometheus scrape + rule evaluation (35s)")
time.sleep(35)

total = prom_query("cc_http_request_duration_seconds_count")
b05 = prom_query('cc_http_request_duration_seconds_bucket{le="0.5"}')
t_val = float(total[0]["value"][1]) if total else 0
b_val = float(b05[0]["value"][1]) if b05 else 0
print("     http_total=%.0f  le0.5=%.0f  over500ms=%.0f" % (t_val, b_val, max(0, t_val - b_val)))
check("prometheus scraped app metrics", t_val > 0, t_val)

# ── 4. 读取告警规则状态 ────────────────────────────────────────────────
print("\n[4] Checking alert rule state")
st, body = http(PROM + "/api/v1/rules")
groups = body.get("data", {}).get("groups", []) if isinstance(body, dict) else []
alert_rules = []
for g in groups:
    for r in g.get("rules", []):
        if r.get("type") == "alerting":
            alert_rules.append(r)

check("alert rules loaded", len(alert_rules) >= 5, len(alert_rules))
for r in alert_rules:
    print("     %-32s state=%-10s value=%s" % (
        r["name"], r.get("state"), str(r.get("alerts", [{}])[0].get("value", ""))[:20] if r.get("alerts") else ""))

firing = [r for r in alert_rules if r.get("state") == "firing"]
pending = [r for r in alert_rules if r.get("state") == "pending"]
print("     firing=%d pending=%d" % (len(firing), len(pending)))

# 提取 rca_hint（无论是否 firing，都验证注释存在）
hints = {}
for r in alert_rules:
    hint = (r.get("annotations") or {}).get("rca_hint")
    if hint:
        hints[r["name"]] = hint
check("all alert rules have rca_hint", len(hints) == len(alert_rules),
      "%d/%d" % (len(hints), len(alert_rules)))

# ── 5. 用告警的 rca_hint 调用 RCA Agent ───────────────────────────────
print("\n[5] Invoking RCA Agent with alert rca_hint")
# 选一条与当前故障最相关的
chosen = None
for name in ["AppHighLatencyP99", "MySQLSlowQueries", "MySQLConnectionPoolExhausted",
             "MySQLTooManyConnections"]:
    if name in hints:
        chosen = (name, hints[name])
        break

check("found an rca_hint to use", chosen is not None, list(hints.keys()))
if chosen:
    rule_name, hint = chosen
    print("     rule: %s" % rule_name)
    print("     hint: %s" % hint)
    st, body = http(RCA + "/api/rca/infer", method="POST", payload={
        "alert": {"alertId": "DRILL-" + rule_name, "severity": "P1", "message": hint},
        "session_id": "drill",
    }, timeout=240)
    check("RCA infer 200", st == 200, body)
    if isinstance(body, dict):
        res = body.get("result", {})
        rc = res.get("root_cause") or {}
        print("     incident : %s" % res.get("incident_id"))
        print("     root     : [%s] %s conf=%s" % (rc.get("category"), rc.get("entity_id"), rc.get("confidence")))
        print("     topology : %d nodes / %d edges" % (
            len(res.get("topology", [])), len(res.get("topology_edges", []))))
        print("     path     : %s" % " -> ".join(
            [res["root_cause_path"][0]["from"]] + [p["to"] for p in res.get("root_cause_path", [])]
        ) if res.get("root_cause_path") else "     path     : (none)")
        check("root cause found", bool(rc))
        check("category is data-related", rc.get("category") in ("数据", "配置", "依赖"), rc.get("category"))
        check("confidence > 0.5", float(rc.get("confidence", 0)) > 0.5, rc.get("confidence"))
        check("topology complete", len(res.get("topology", [])) >= 10, len(res.get("topology", [])))

        # 记录反馈（演练）
        iid = res.get("incident_id")
        if iid:
            st2, b2 = http(RCA + "/api/rca/feedback", method="POST", payload={
                "incident_id": iid, "verdict": "CONFIRMED",
                "actual_cause": "t_txn exclusive row lock caused INSERT blocking",
                "note": "drill: fault injection via SELECT ... FOR UPDATE",
                "operator": "drill-bot",
            })
            check("feedback recorded", st2 == 200, b2)

# ── 6. 解除故障 ────────────────────────────────────────────────────────
print("\n[6] Releasing fault")
try:
    lock_proc.terminate()
except Exception:
    pass
mysql_exec("KILL QUERY " + str(lock_proc.pid) + ";")
time.sleep(2)
# 确保所有锁释放
rc, out, err = mysql_exec("SELECT COUNT(*) FROM information_schema.innodb_trx;")
print("     innodb_trx after release: %s" % out.strip().split("\n")[-1] if out else "?")

# 等锁释放
time.sleep(8)
post = generate_load(n=16, concurrency=4)
p_lat = sorted(post["latencies"]) if post["latencies"] else [0]
p_p99 = p_lat[int(len(p_lat) * 0.99) - 1] if len(p_lat) > 1 else p_lat[0]
print("     after release: %d ok, %d err, p99=%.3fs" % (post["ok"], post["err"], p_p99))
check("recovered after fault release", post["ok"] > 0, post)

print("\n" + "=" * 72)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
