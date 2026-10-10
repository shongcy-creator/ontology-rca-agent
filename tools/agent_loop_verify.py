#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Agent 循环验证：观察→推理→行动 全链路（真实 LLM + 真实环境取证）。"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # <repo>，不写死宿主绝对路径

sys.path.insert(0, str(ROOT / "rca-agent"))

os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("PROMETHEUS_URL", "http://localhost:9090")
os.environ.setdefault("EVO_WORKSPACE", str(ROOT / ".evoontology"))
os.environ.setdefault("PYTHON_EXE", sys.executable)
os.environ.setdefault("RCA_DB_USER", "rca_readonly")
os.environ.setdefault("RCA_DB_PASSWORD", "rca_readonly_pwd")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend.services.agent import RCAAgent              # noqa: E402
from backend.services.rca_engine import RCAEngine        # noqa: E402
from backend.services.tools import build_default_registry  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + name)
    else:
        FAIL += 1
        print("  [FAIL] " + name + " -- " + str(detail)[:250])


async def run_case(title, alert, severity="P1", verbose=True):
    print("\n" + "=" * 78)
    print("CASE: %s" % title)
    print("ALERT: %s" % alert)
    print("=" * 78)

    # ① 本体确定性快检（Q2-A 快路径）
    t0 = time.perf_counter()
    seed = await asyncio.to_thread(RCAEngine().infer, alert, severity, "payment-app", False)
    seed_ms = (time.perf_counter() - t0) * 1000
    seed_rc = seed.get("root_cause") or {}
    print("[SEED] 确定性引擎: [%s] %s  conf=%s  (%.0fms)" % (
        seed_rc.get("category"), seed_rc.get("entity_id"),
        seed_rc.get("confidence"), seed_ms))

    # ② Agent 循环
    events = []
    agent = RCAAgent(
        alert=alert, severity=severity, seed=seed,
        registry=build_default_registry(),
        on_event=lambda ev, data: events.append((ev, data)),
    )
    result = await agent.run()

    print("\n[AGENT] run_id=%s  mode=%s  status=%s" % (
        result.run_id, result.mode, result.status))
    print("        steps_used=%d  tokens=%d  latency=%.0fms  model=%s" % (
        result.steps_used, result.total_tokens, result.latency_ms, result.model))
    print("        tools_used=%s" % (", ".join(result.tools_used) or "无"))
    rc = result.root_cause or {}
    print("        root_cause=[%s] %s" % (rc.get("category"), rc.get("entity_id")))
    print("        confidence=%s   seed_agreement=%s" % (result.confidence, result.seed_agreement))
    if result.reasoning_summary:
        print("        summary=%s" % result.reasoning_summary[:160])

    if verbose:
        print("\n  推理轨迹：")
        for s in result.steps:
            if s.phase == "reason" and s.reasoning:
                print("   [Step %d 推理] %s" % (s.step_no, s.reasoning[:220].replace("\n", " ")))
            elif s.phase == "act":
                mark = "OK " if s.observation_ok else "ERR"
                print("   [Step %d 行动] %-24s %s  %s" % (
                    s.step_no, s.tool_name, mark, s.observation[:150].replace("\n", " ")))
            elif s.phase == "observe":
                print("   [初始观察] %-24s %s" % (s.tool_name, s.observation[:110].replace("\n", " ")))

    if result.evidence:
        print("\n  证据链（%d 条）：" % len(result.evidence))
        for e in result.evidence[:6]:
            print("   - [%s] %s" % (e.get("source"), str(e.get("finding"))[:130]))
    if result.next_actions:
        print("\n  建议动作（%d 条）：" % len(result.next_actions))
        for a in result.next_actions[:5]:
            print("   - [%s] %s%s" % (a.get("urgency"), str(a.get("action"))[:90],
                                      "  (需人工确认)" if a.get("needs_approval") else ""))

    # 事件流
    ev_kinds = {}
    for ev, _ in events:
        ev_kinds[ev] = ev_kinds.get(ev, 0) + 1
    print("\n  事件流: %s" % json.dumps(ev_kinds, ensure_ascii=False))

    return seed, result, events


async def main():
    print("=" * 78)
    print("RCA AGENT LOOP VERIFICATION")
    print("=" * 78)

    # ── 用例 1：已知模式（P99 + 连接池）────────────────────────────
    seed, result, events = await run_case(
        "已知模式 - P99 延迟 + 连接池耗尽",
        "payment-app P99 延迟超过 500ms，MySQL 连接池耗尽",
    )

    print("\n[断言]")
    check("agent 完成", result.status == "completed", result.status + " " + result.error)
    check("mode 为 agentic", result.mode == "agentic", result.mode)
    check("有推理步骤", any(s.phase == "reason" for s in result.steps))
    check("有行动步骤（调用了工具）", any(s.phase == "act" for s in result.steps),
          [s.tool_name for s in result.steps])
    check("有初始观察", any(s.phase == "observe" for s in result.steps))
    check("调用了 >=2 个工具", len(result.tools_used) >= 2, result.tools_used)
    check("有根因", bool(result.root_cause.get("category") or result.root_cause.get("entity_id")),
          result.root_cause)
    check("置信度在 [0,1]", 0 <= result.confidence <= 1, result.confidence)
    check("有证据链", len(result.evidence) > 0, len(result.evidence))
    check("有建议动作", len(result.next_actions) > 0, len(result.next_actions))
    check("建议动作标注需人工确认",
          all(a.get("needs_approval") for a in result.next_actions) if result.next_actions else True,
          [a.get("needs_approval") for a in result.next_actions])
    check("token 用量被记录", result.total_tokens > 0, result.total_tokens)
    check("事件流含 reasoning", any(e == "reasoning" for e, _ in events))
    check("事件流含 tool_call", any(e == "tool_call" for e, _ in events))
    check("事件流含 observation", any(e == "observation" for e, _ in events))
    check("seed_agreement 已计算", result.seed_agreement is not None, result.seed_agreement)
    check("推理内容非空", any(len(s.reasoning) > 20 for s in result.steps if s.phase == "reason"))

    # ── 用例 2：非确定性（无预置关键词）─────────────────────────────
    seed2, result2, _ = await run_case(
        "非确定性 - 无预置关键词的模糊症状",
        "用户反馈晚上 8 点左右支付偶尔要等很久才成功，白天正常",
    )

    print("\n[断言]")
    check("模糊告警也能产出结论", result2.status in ("completed", "budget_exceeded"),
          result2.status)
    check("调用了探索型工具", len(result2.tools_used) >= 2, result2.tools_used)
    check("有推理链", any(s.phase == "reason" for s in result2.steps))
    check("置信度合理（不虚高）", result2.confidence <= 0.95, result2.confidence)

    print("\n" + "=" * 78)
    print("RESULT: %d passed, %d failed" % (PASS, FAIL))
    print("=" * 78)
    print("\n[成本] case1 tokens=%d  case2 tokens=%d  合计=%d" % (
        result.total_tokens, result2.total_tokens,
        result.total_tokens + result2.total_tokens))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
