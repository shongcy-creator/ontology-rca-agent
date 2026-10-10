#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证 Anthropic Messages 路径（rsxermu666）的 Function Calling。"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # <repo>，不写死宿主绝对路径
sys.path.insert(0, str(ROOT / "rca-agent"))

from backend.services.llm_client import LLMClient      # noqa: E402
from backend.services.llm_config import llm_config     # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TOOLS = [{
    "type": "function",
    "function": {
        "name": "db_processlist",
        "description": "查询 MySQL 当前会话与锁等待",
        "parameters": {
            "type": "object",
            "properties": {"min_time": {"type": "integer", "description": "最小运行秒数"}},
        },
    },
}]


async def main():
    cfg = llm_config()
    cfg["provider"] = "rsxermu666"
    cfg["api"] = "anthropic-messages"
    cfg["base_url"] = "https://rsxermu666.cn"
    cfg["model"] = "claude-sonnet-4-6"
    cfg["fallback_model"] = "claude-haiku-4-5"

    from backend.services.llm_config import resolve_api_key
    cfg["api_key"] = resolve_api_key("rsxermu666")

    print("=" * 72)
    print("ANTHROPIC PATH VERIFICATION")
    print("=" * 72)
    print("model=%s  key=%s" % (cfg["model"], (cfg["api_key"] or "")[:8] + "***"))

    # 1. 纯文本
    print("\n[1] Plain chat")
    async with LLMClient(cfg) as c:
        r = await c.chat([{"role": "user", "content": "回复：连接池耗尽常见原因（一句话）"}])
        print("     content  : %s" % (r.content or "")[:100].replace("\n", " "))
        print("     reasoning: %s" % (r.reasoning or "(无)")[:80].replace("\n", " "))
        print("     tokens=%s latency=%sms finish=%s" % (r.total_tokens, r.latency_ms, r.finish_reason))
        print("     error=%s" % (r.error or "无"))

    # 2. Function Calling
    print("\n[2] Function calling")
    async with LLMClient(cfg) as c:
        r = await c.chat(
            [
                {"role": "system", "content": "你是 SRE。需要数据时必须调用工具。"},
                {"role": "user", "content": "数据库好像有锁等待，帮我查一下当前会话。"},
            ],
            tools=TOOLS,
        )
        print("     finish=%s  tool_calls=%d" % (r.finish_reason, len(r.tool_calls)))
        for tc in r.tool_calls:
            print("       - %s(%s)" % (tc.name, json.dumps(tc.arguments, ensure_ascii=False)[:80]))
        print("     error=%s" % (r.error or "无"))

    # 3. Tool result 回填
    print("\n[3] Tool result round-trip")
    async with LLMClient(cfg) as c:
        msgs = [
            {"role": "system", "content": "你是 SRE。"},
            {"role": "user", "content": "查一下锁等待"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "db_processlist", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "name": "db_processlist",
             "content": "进程列表 4 条：活跃 2，1 个运行超过 5s 的语句（SELECT ... FOR UPDATE）"},
        ]
        r = await c.chat(msgs)
        print("     answer: %s" % (r.content or r.error or "")[:140].replace("\n", " "))

    print("\n" + "=" * 72)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
