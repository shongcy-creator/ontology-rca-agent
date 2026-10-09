#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""运行全部验证套件并汇总."""
import subprocess, sys, time, re

SUITES = [
    ("Production stack",   "tools/prod_verify.py"),
    ("Persistence",        "tools/persistence_verify.py"),
    ("RCA invariants",     "tools/scoring_verify.py"),
    ("Frontend flow",      "tools/fe_flow_verify.py"),
    ("Resizable panel",    "tools/resizable_verify.py"),
    ("Fault drill",        "tools/fault_drill.py"),
    ("Alert firing",       "tools/alert_firing_verify.py"),
]

ROOT = r"D:\05_code\credit-card-sys-ops"
PY = sys.executable

results = []
grand_pass = grand_fail = 0

print("=" * 78)
print("FULL VERIFICATION — RCA AGENT")
print("=" * 78)

for name, script in SUITES:
    print("\n" + "-" * 78)
    print("SUITE: %s  (%s)" % (name, script))
    print("-" * 78)
    t0 = time.time()
    p = subprocess.run([PY, script], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    dt = time.time() - t0

    # 提取 RESULT 行
    m = re.search(r"RESULT:\s*(\d+)\s*passed,\s*(\d+)\s*failed", out)
    if m:
        ps, fs = int(m.group(1)), int(m.group(2))
    else:
        ps, fs = 0, -1

    grand_pass += ps
    if fs > 0:
        grand_fail += fs
    results.append((name, ps, fs, dt, p.returncode))

    # 打印失败项
    if fs != 0:
        for line in out.splitlines():
            if "[FAIL]" in line or "Error" in line or "Traceback" in line:
                print("   " + line.strip()[:170])
    print("   -> %d passed, %d failed  (%.1fs)" % (ps, fs, dt))

print("\n" + "=" * 78)
print("SUMMARY")
print("=" * 78)
print("%-22s %8s %8s %10s" % ("SUITE", "PASSED", "FAILED", "TIME"))
print("-" * 78)
for name, ps, fs, dt, rc in results:
    status = "OK" if fs == 0 else "FAIL"
    print("%-22s %8d %8d %9.1fs  %s" % (name, ps, fs, dt, status))
print("-" * 78)
print("%-22s %8d %8d" % ("TOTAL", grand_pass, grand_fail))
print("=" * 78)
sys.exit(0 if grand_fail == 0 else 1)
