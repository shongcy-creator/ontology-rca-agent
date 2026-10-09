#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试 LLM 客户端：连通性、Function Calling、reasoning_content、降级。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, r"D:\05_code\credit-card-sys-ops\rca-agent")

from backend.services.llm_client import LLMClient, health_check
from backend.services.llm_config import llm_config

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + name)
    else:
        FAIL += 1
        print("  [FAIL] " + name + " -- " + str(detail)[:200])


TOOLS = [{
    "type": "function",
    "function": {
        "name": "metrics_query",
        "description": "查询 Prometheus 指标（PromQL）",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "PromQL 表达式"}},
            "required": ["query"],
        },
    },
}]


async def main():
    print("=" * 72)
    print("LLM CLIENT VERIFICATION")
    print("=" * 72)

    cfg = llm_config()
    print("\n[0] Config")
    print("     provider=%s model=%s fallback=%s" % (cfg["provider"], cfg["model"], cfg["fallback_model"]))
    check("api key present", bool(cfg["api_key"]))

    # ── 1. 健康检查 ────────────────────────────────────────────────
    print("\n[1] Health check")
    h = await health_check()
    print("     ok=%s model=%s latency=%sms tokens=%s" % (
        h.get("ok"), h.get("model"), h.get("latency_ms"), h.get("tokens")))
    check("health ok", h.get("ok") is True, h)
    check("latency recorded", (h.get("latency_ms") or 0) > 0)

    # ── 2. 纯文本对话 + reasoning ──────────────────────────────────
    print("\n[2] Plain chat (expect reasoning_content)")
    async with LLMClient(cfg) as c:
        r = await c.chat([{"role": "user", "content": "用一句话说明：MySQL 连接池耗尽会导致什么？"}])
        print("     content   : %s" % (r.content or "")[:90].replace("\n", " "))
        print("     reasoning : %s" % (r.reasoning or "")[:90].replace("\n", " "))
        print("     tokens=%s latency=%sms finish=%s" % (r.total_tokens, r.latency_ms, r.finish_reason))
        check("no error", r.error is None, r.error)
        check("content non-empty", len(r.content) > 0)
        check("usage recorded", r.total_tokens > 0, r.usage)
        check("latency recorded", r.latency_ms > 0)

    # ── 3. Function Calling ────────────────────────────────────────
    print("\n[3] Function calling")
    async with LLMClient(cfg) as c:
        r = await c.chat(
            [
                {"role": "system", "content": "你是 SRE。需要观测数据时必须调用工具，不要凭空回答。"},
                {"role": "user", "content": "payment-app 的 P99 延迟涨到 800ms 了，先查一下指标。"},
            ],
            tools=TOOLS,
        )
        print("     finish_reason : %s" % r.finish_reason)
        print("     tool_calls    : %d" % len(r.tool_calls))
        for tc in r.tool_calls[:3]:
            print("       - %s(%s)" % (tc.name, str(tc.arguments)[:80]))
        print("     tokens=%s" % r.total_tokens)
        check("no error", r.error is None, r.error)
        check("returned tool calls", len(r.tool_calls) > 0, r.finish_reason)
        check("tool name correct", all(tc.name == "metrics_query" for tc in r.tool_calls),
              [tc.name for tc in r.tool_calls])
        check("arguments parsed to dict", all(isinstance(tc.arguments, dict) for tc in r.tool_calls))
        check("arguments contain query",
              all("query" in tc.arguments for tc in r.tool_calls),
              [tc.arguments for tc in r.tool_calls])

    # ── 4. 工具结果回填 → 收敛 ─────────────────────────────────────
    print("\n[4] Tool result round-trip → final answer")
    async with LLMClient(cfg) as c:
        msgs = [
            {"role": "system", "content": "你是 SRE。基于观测数据给结论。"},
            {"role": "user", "content": "P99 800ms，连接池 8/10，t_txn 有 1 个长事务持有排他锁。根因是什么？"},
        ]
        r = await c.chat(msgs)
        check("final answer produced", len(r.content) > 0, r.error)
        print("     answer: %s" % (r.content or "")[:120].replace("\n", " "))

    # ── 5. 错误处理（错误 key）──────────────────────────────────────
    print("\n[5] Error handling (bad key)")
    bad = dict(cfg)
    bad["api_key"] = "sk-invalid-key-for-test"
    async with LLMClient(bad) as c:
        r = await c.chat([{"role": "user", "content": "hi"}], max_retries=0)
        print("     error: %s" % str(r.error)[:100])
        check("error captured not raised", r.error is not None)

    print("\n" + "=" * 72)
    print("RESULT: %d passed, %d failed" % (PASS, FAIL))
    print("=" * 72)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
