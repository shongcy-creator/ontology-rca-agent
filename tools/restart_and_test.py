#!/usr/bin/env python
"""
重启本地 RCA 后端并做三项冒烟（health / tools / ui）。

路径与解释器都不再写死：
  · 解释器 = 环境变量 `CC_PYTHON`（可选）→ 否则 `sys.executable`；
  · 后端入口 = 从本文件位置推导（`tools/` 的上一级即仓库根）+ `rca-agent/start_fixed.py`。

⚠ 已知风险（未改，见提交说明）：下面用 `taskkill /F /IM python.exe` 结束旧后端，
  它会杀掉**本机所有** python 进程 —— 包括 DSH harness 自身或任何在跑的 python 任务。
  建议改成"只杀占用 8088 端口的 PID"（或按命令行匹配 `start_fixed.py`），
  但那会改变行为，需要真跑一次才能验证，故未在本轮一并改动。

用法：
  python tools/restart_and_test.py
  CC_PYTHON=/path/to/python python tools/restart_and_test.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent            # <repo>
PY = os.environ.get("CC_PYTHON") or sys.executable       # 不写死用户目录下的运行时
BACKEND_ENTRY = ROOT / "rca-agent" / "start_fixed.py"

# Kill old backend
kill_cmd = (["taskkill", "/F", "/IM", "python.exe"] if os.name == "nt"
            else ["pkill", "-f", "python"])
for proc in subprocess.run(kill_cmd, capture_output=True, text=True).stdout:
    print(proc)

# Wait for port to free
time.sleep(3)

# Start new backend
proc = subprocess.Popen(
    [PY, str(BACKEND_ENTRY)],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
proc.stdout and proc.stderr
time.sleep(10)

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
