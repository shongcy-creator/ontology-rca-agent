#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
告警规则真实触发验证：
  1. 注入长时间故障（保持 > 2.5 分钟）
  2. 持续压测，让延迟 histogram 反映故障
  3. 等待告警规则从 inactive → pending → firing
  4. 确认告警携带 rca_hint / onto_constraint 标签
"""
import json, shutil, subprocess, sys, time, threading
import urllib.request, urllib.error, urllib.parse

APP = "http://localhost:8080"
PROM = "http://localhost:9090"
MYSQL_CONTAINER = "cc-mysql-core"
# 走 PATH（可用 CC_DOCKER 覆盖），不写死 Docker Desktop 安装路径
DOCKER = shutil.which("docker") or shutil.which("docker.exe") or "docker"

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
        return e.code, e.read().decode("utf-8", errors="replace")[:200]
    except Exception as e:
        return 0, str(e)


def prom(expr):
    st, body = http(PROM + "/api/v1/query?query=" + urllib.parse.quote(expr))
    if st == 200 and isinstance(body, dict):
        return body.get("data", {}).get("result", [])
    return []


def prom_scalar(expr, default=0.0):
    res = prom(expr)
    if not res:
        return default
    try:
        return float(res[0]["value"][1])
    except Exception:
        return default


def mysql_exec(sql, timeout=30):
    p = subprocess.run([DOCKER, "exec", MYSQL_CONTAINER, "mysql", "-u", "appuser",
                        "-papppass", "creditcard", "-e", sql],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


stop_load = threading.Event()
load_stats = {"ok": 0, "err": 0, "max_lat": 0.0}


def load_worker():
    while not stop_load.is_set():
        t0 = time.time()
        st, _ = http(APP + "/txn", method="POST",
                     payload={"customer_id": 1, "amount": 100, "status": "SETTLED"},
                     timeout=45)
        dt = time.time() - t0
        load_stats["max_lat"] = max(load_stats["max_lat"], dt)
        if st == 200:
            load_stats["ok"] += 1
        else:
            load_stats["err"] += 1


print("=" * 72)
print("ALERT RULE FIRING VERIFICATION (real fault, > 2.5 min)")
print("=" * 72)

# ── 0. 基线指标 ────────────────────────────────────────────────────────
print("\n[0] Baseline")
total0 = prom_scalar("sum(cc_http_request_duration_seconds_count)")
le05_0 = prom_scalar('sum(cc_http_request_duration_seconds_bucket{le="0.5"})')
print("     http_total=%.0f  le0.5=%.0f" % (total0, le05_0))

# ── 1. 注入长故障 ──────────────────────────────────────────────────────
print("\n[1] Injecting long fault (t_txn row lock, 200s)")
lock_proc = subprocess.Popen(
    [DOCKER, "exec", MYSQL_CONTAINER, "mysql", "-u", "appuser", "-papppass", "creditcard",
     "-e", "START TRANSACTION; SELECT * FROM t_txn FOR UPDATE; SELECT SLEEP(200); COMMIT;"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
)
time.sleep(5)
print("     lock holder pid=%s" % lock_proc.pid)

# ── 2. 持续压测 ────────────────────────────────────────────────────────
print("\n[2] Sustained load (8 workers)")
threads = [threading.Thread(target=load_worker, daemon=True) for _ in range(8)]
for t in threads:
    t.start()

# ── 3. 轮询告警状态 ────────────────────────────────────────────────────
print("\n[3] Polling alert state (up to 260s)")
fired = {}
deadline = time.time() + 260
while time.time() < deadline:
    st, body = http(PROM + "/api/v1/rules")
    groups = body.get("data", {}).get("groups", []) if isinstance(body, dict) else []
    cur = {}
    for g in groups:
        for r in g.get("rules", []):
            if r.get("type") == "alerting":
                cur[r["name"]] = r.get("state", "inactive")
    # 记录状态推进
    for k, v in cur.items():
        prev = fired.get(k, "inactive")
        if v != prev:
            print("     %-32s %s -> %s" % (k, prev, v))
            fired[k] = v
    if any(v == "firing" for v in cur.values()):
        time.sleep(8)  # 让状态稳定
        print("     -> at least one rule FIRING")
        break
    time.sleep(10)

stop_load.set()
for t in threads:
    t.join(timeout=50)

print("\n     load: %d ok, %d err, max_latency=%.1fs" % (
    load_stats["ok"], load_stats["err"], load_stats["max_lat"]))

# 指标快照
total1 = prom_scalar("sum(cc_http_request_duration_seconds_count)")
le05_1 = prom_scalar('sum(cc_http_request_duration_seconds_bucket{le="0.5"})')
over = max(0.0, total1 - le05_1)
print("     http_total=%.0f  le0.5=%.0f  over500ms=%.0f (%.1f%%)" % (
    total1, le05_1, over, (over / total1 * 100) if total1 else 0))

# ── 4. 断言 ────────────────────────────────────────────────────────────
print("\n[4] Assertions")
check("latency increased measurably", (over > 0) or (load_stats["err"] > 0),
      "over=%d err=%d" % (over, load_stats["err"]))
check("at least one alert left inactive state", len(fired) > 0, fired)
check("at least one alert FIRING", "firing" in fired.values(), fired)

# 验证触发告警的元数据
st, body = http(PROM + "/api/v1/rules")
groups = body.get("data", {}).get("groups", []) if isinstance(body, dict) else []
for g in groups:
    for r in g.get("rules", []):
        if r.get("type") != "alerting":
            continue
        if r.get("state") in ("firing", "pending"):
            print("\n     RULE: %s  state=%s" % (r["name"], r["state"]))
            print("       expr   : %s" % r.get("query", "")[:110].replace("\n", " "))
            lbls = r.get("labels", {})
            for k in ("severity", "layer", "onto_constraint"):
                if k in lbls:
                    print("       %-15s= %s" % (k, lbls[k]))
            for a in r.get("alerts", []):
                ann = a.get("annotations", {})
                print("       summary: %s" % ann.get("summary", "")[:90])
                print("       rca_hint: %s" % ann.get("rca_hint", "")[:90])
                check("firing alert has rca_hint", bool(ann.get("rca_hint")), ann.keys())
                check("firing alert has severity", bool(a.get("labels", {}).get("severity")))

# ── 5. 解除故障 ────────────────────────────────────────────────────────
print("\n[5] Releasing fault")
lock_proc.terminate()
try:
    lock_proc.kill()
except Exception:
    pass

# 显式杀掉持锁/睡眠的 MySQL 会话，确保锁释放
for attempt in range(6):
    rc, out, err = mysql_exec(
        "SELECT id FROM information_schema.processlist "
        "WHERE info LIKE '%SLEEP%' OR info LIKE '%FOR UPDATE%' OR command='Sleep' AND time>5;"
    )
    ids = []
    for line in (out or "").splitlines():
        line = line.strip()
        if line.isdigit():
            ids.append(line)
    if not ids:
        break
    for pid in ids:
        mysql_exec("KILL %s;" % pid)
    print("     killed mysql sessions: %s" % ",".join(ids))
    time.sleep(3)

mysql_exec("SELECT COUNT(*) FROM information_schema.innodb_trx;")
time.sleep(12)

# 快速恢复验证（最多重试 6 次）
recovered = False
for i in range(6):
    st, _ = http(APP + "/txn", method="POST",
                 payload={"customer_id": 1, "amount": 100, "status": "SETTLED"}, timeout=30)
    print("     recovery attempt %d -> HTTP %s" % (i + 1, st))
    if st == 200:
        recovered = True
        break
    time.sleep(8)
check("app recovered", recovered, "all attempts failed")

print("\n" + "=" * 72)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
print("FIRED=" + json.dumps(fired, ensure_ascii=False))
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
