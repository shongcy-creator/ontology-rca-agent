#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打印可调宽面板实现摘要."""
import re, subprocess
from pathlib import Path

ROOT = Path(r"D:\05_code\credit-card-sys-ops\rca-agent")

docker = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
p = subprocess.run([docker, "compose", "ps", "--format",
                    "table {{.Service}}\t{{.Status}}"],
                   cwd=str(ROOT), capture_output=True, text=True)
print("=== 容器状态 ===")
print(p.stdout.strip())

hook = (ROOT / "frontend/src/hooks/useResizablePanel.ts").read_text(encoding="utf-8")
page = (ROOT / "frontend/src/pages/AgentPage.tsx").read_text(encoding="utf-8")

print()
print("=== 宽度约束 ===")
for label, pat in [("默认宽度", r"defaultWidth: (\d+)"),
                   ("最小宽度", r"minWidth: (\d+)"),
                   ("聊天区最小", r"minOtherWidth: (\d+)"),
                   ("最大宽度", r"maxWidth: (\d+)")]:
    m = re.search(pat, page) or re.search(pat, hook)
    print("  %-12s %s px" % (label, m.group(1) if m else "?"))

print()
print("=== 交互能力 ===")
caps = [
    ("鼠标拖拽",   "onHandleMouseDown", hook),
    ("触摸拖拽",   "onHandleTouchStart", hook),
    ("键盘微调",   "ArrowLeft", hook),
    ("双击复位",   "onHandleDoubleClick", hook),
    ("折叠/展开",  "toggleCollapsed", hook),
    ("本地持久化", "localStorage", hook),
    ("尺寸自适应", "ResizeObserver", hook),
    ("无障碍语义", 'role="separator"', page),
    ("拖拽态样式", "is-dragging", page),
]
for name, token, src in caps:
    print("  %-12s %s" % (name, "YES" if token in src else "NO"))

print()
print("=== 访问地址 ===")
print("  UI      http://localhost:3001")
print("  API     http://localhost:8088/docs")
