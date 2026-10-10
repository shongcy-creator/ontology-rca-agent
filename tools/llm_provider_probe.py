#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试所有可用 LLM provider 的连通性与余额状态。"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # <repo>，不写死宿主绝对路径
sys.path.insert(0, str(ROOT / "rca-agent"))
from backend.services.llm_config import available_providers, resolve_api_key   # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def probe_openai(base, key, model, timeout=60):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        return True, "%.0fms tokens=%s" % (
            (time.time() - t0) * 1000, (d.get("usage") or {}).get("total_tokens")), ""
    except urllib.error.HTTPError as e:
        return False, "HTTP %s" % e.code, e.read().decode()[:220]
    except Exception as e:
        return False, type(e).__name__, str(e)[:180]


def probe_anthropic(base, key, model, timeout=60):
    body = {"model": model, "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]}
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        u = d.get("usage") or {}
        return True, "%.0fms in=%s out=%s" % (
            (time.time() - t0) * 1000, u.get("input_tokens"), u.get("output_tokens")), ""
    except urllib.error.HTTPError as e:
        return False, "HTTP %s" % e.code, e.read().decode()[:220]
    except Exception as e:
        return False, type(e).__name__, str(e)[:180]


print("=" * 78)
print("LLM PROVIDER AVAILABILITY")
print("=" * 78)

for name, info in available_providers().items():
    key = resolve_api_key(name)
    print("\n### %s  (%s)" % (name, info["key_masked"]))
    if not key:
        print("   key 未配置")
        continue

    models = [info["default_model"], info["fallback_model"]]
    for m in models:
        if info["api"] == "anthropic-messages":
            ok, detail, err = probe_anthropic(info["base_url"], key, m)
        else:
            ok, detail, err = probe_openai(info["base_url"], key, m)
        if ok:
            print("   [OK ] %-28s %s" % (m, detail))
        else:
            print("   [ERR] %-28s %s  %s" % (m, detail, err.replace("\n", " ")[:170]))
