import os
#!/usr/bin/env python
import subprocess, time, sys

# Kill old backend
for proc in subprocess.run((["taskkill", "/F", "/IM", "python.exe"] if os.name == "nt" else ["pkill", "-f", "python"]),
        capture_output=True, text=True).stdout:
    print(proc)

# Wait for port to free
time.sleep(3)

# Start new backend
proc = subprocess.Popen(
    [r"C:\Users\41187\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe",
     r"D:\05_code\credit-card-sys-ops\rca-agent\start_fixed.py"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
proc.stdout and proc.stderr
time.sleep(10)

import urllib.request, json

tests = [
    ("GET", "http://localhost:8088/api/health", None),
    ("GET", "http://localhost:8088/api/chat/tools", None),
    ("GET", "http://localhost:8088/ui/", None),
]

for method, url, data in tests:
    try:
        r = urllib.request.urlopen(url, timeout=8)
        body = r.read(100).decode("utf-8", errors="replace")
        print("OK", url, "--", body[:80])
    except Exception as e:
        print("ERR", url, str(e)[:80])
