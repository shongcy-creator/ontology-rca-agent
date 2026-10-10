#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""复现：agnes-2.5-pro 在大上下文 + tools 下的表现。"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # <repo>，不写死宿主绝对路径
sys.path.insert(0, str(ROOT / "rca-agent"))
from backend.services.llm_config import resolve_api_key   # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

key = resolve_api_key("agnes")
print("key:", key[:8] + "***" if key else "MISSING")

TOOLS = [{
    "type": "function",
    "function": {
        "name": "db_status",
        "description": "MySQL 全局状态",
        "parameters": {"type": "object", "properties": {}},
    },
}]


def call(model, prompt_chars, with_tools=True, timeout=180):
    big = "以下是已采集的观测数据：\n" + (
        '{"status": {"Threads_connected": "3", "Slow_queries": "142", '
        '"Innodb_row_lock_waits": "139"}, "note": "%s"}' % ("x" * 200)
    ) * (prompt_chars // 260)

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是 SRE，负责根因分析。"},
            {"role": "user", "content": "告警：payment-app P99 > 500ms。\n" + big},
        ],
    }
    if with_tools:
        body["tools"] = TOOLS
        body["tool_choice"] = "auto"

    n_chars = sum(len(m["content"]) for m in body["messages"])
    req = urllib.request.Request(
        "https://apihub.agnes-ai.com/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        dt = (time.time() - t0) * 1000
        msg = d["choices"][0]["message"]
        return {
            "ok": True, "ms": dt, "chars": n_chars,
            "tokens": (d.get("usage") or {}).get("total_tokens"),
            "has_tool": bool(msg.get("tool_calls")),
            "finish": d["choices"][0].get("finish_reason"),
        }
    except urllib.error.HTTPError as e:
        return {"ok": False, "ms": (time.time() - t0) * 1000, "chars": n_chars,
                "err": "HTTP %s: %s" % (e.code, e.read().decode()[:200])}
    except Exception as e:
        return {"ok": False, "ms": (time.time() - t0) * 1000, "chars": n_chars,
                "err": "%s: %s" % (type(e).__name__, str(e)[:150])}


print("=" * 78)
print("LARGE CONTEXT / TOOLS REPRODUCTION")
print("=" * 78)

for model in ["agnes-2.5-pro", "agnes-3.0-flash"]:
    print("\n### %s" % model)
    for chars, tools in [(2000, False), (20000, False), (20000, True), (60000, True), (120000, True)]:
        r = call(model, chars, tools, timeout=200)
        status = "OK " if r["ok"] else "ERR"
        print("  %s chars=%-7d tools=%-5s %7.0fms  %s" % (
            status, r["chars"], tools, r["ms"],
            ("tokens=%s tool=%s finish=%s" % (r.get("tokens"), r.get("has_tool"), r.get("finish")))
            if r["ok"] else r.get("err", "")[:120]))
