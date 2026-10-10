#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断 agent_store 持久化问题。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # <repo>，不写死宿主绝对路径

sys.path.insert(0, str(ROOT / "rca-agent"))
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("RCA_DB_USER", "rca_readonly")
os.environ.setdefault("RCA_DB_PASSWORD", "rca_readonly_pwd")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend.services import agent_store
from backend.services.incident_store import _get_conn

print("=== 1. 连接与权限 ===")
conn = _get_conn()
print("conn:", type(conn).__name__ if conn else None)
if conn:
    with conn.cursor() as cur:
        cur.execute("SELECT CURRENT_USER() AS u, DATABASE() AS d")
        print("user/db:", cur.fetchone())
        cur.execute("SELECT COUNT(*) AS n FROM rca_agent_run")
        print("existing runs:", cur.fetchone())
        cur.execute("SELECT COUNT(*) AS n FROM rca_agent_thought")
        print("existing thoughts:", cur.fetchone())

print()
print("=== 2. 直接尝试 save_run ===")
class FakeStep:
    def __init__(self, n, phase, reasoning="", tool="", obs=""):
        self.step_no, self.phase = n, phase
        self.reasoning, self.tool_name, self.observation = reasoning, tool, obs
        self.tool_input, self.observation_ok = {}, True
        self.tokens, self.latency_ms = 0, 0
    def to_dict(self):
        return {"step": self.step_no, "phase": self.phase, "reasoning": self.reasoning,
                "tool_name": self.tool_name, "tool_input": self.tool_input,
                "observation": self.observation, "observation_ok": self.observation_ok,
                "tokens": self.tokens, "latency_ms": self.latency_ms}

class FakeResult:
    def __init__(self):
        self.run_id = "RUN-DEBUG-001"
        self.incident_id = "INC-DEBUG-001"
        self.mode = "agentic"
        self.status = "completed"
        self.root_cause = {"category": "数据", "entity_id": "rc:slow-sql", "description": "debug"}
        self.confidence = 0.88
        self.evidence = []
        self.affected_entities = []
        self.reasoning_summary = "debug"
        self.next_actions = []
        self.steps = [FakeStep(1, "reason", "分析中"), FakeStep(1, "act", tool="db_status", obs="ok")]
        self.steps_used = 1
        self.total_tokens = 100
        self.latency_ms = 1000
        self.seed_agreement = True
        self.tools_used = ["db_status"]
        self.model = "test"
        self.error = ""
    def to_dict(self):
        d = dict(self.__dict__)
        d["steps"] = [s.to_dict() for s in self.steps]
        return d

r = FakeResult()
print("save_run ->", agent_store.save_run(r, "debug alert", "P1", "debug route", "INC-DEBUG-001"))
print("save_thoughts ->", agent_store.save_thoughts("RUN-DEBUG-001", r.steps))

print()
print("=== 3. 读回 ===")
run = agent_store.get_run("RUN-DEBUG-001")
print("get_run:", "FOUND" if run else "NOT FOUND")
if run:
    print("  keys:", list(run.keys())[:8])
    print("  mode:", run.get("mode"), " status:", run.get("status"))

thoughts = agent_store.get_thoughts("RUN-DEBUG-001")
print("get_thoughts:", len(thoughts))
for t in thoughts:
    print("  raw row:", t)

print()
print("=== 4. raw SQL 检查 ===")
if conn:
    with conn.cursor() as cur:
        cur.execute("SELECT run_id, step_no, phase, tool_name, LEFT(observation,40) AS obs "
                    "FROM rca_agent_thought WHERE run_id='RUN-DEBUG-001'")
        for row in cur.fetchall():
            print("  ", row)
