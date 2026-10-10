#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""双引擎诊断端到端验证：路由判定 + 两条路径 + 落库回放。"""
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
# 额度紧张：用更便宜的模型做验证
os.environ.setdefault("RCA_LLM_MODEL", "agnes-3.0-flash")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend.services import agent_store                      # noqa: E402
from backend.services.llm_config import llm_config            # noqa: E402
from backend.services.orchestrator import diagnose            # noqa: E402
from backend.services.rca_engine import RCAEngine             # noqa: E402
from backend.services.router import decide                    # noqa: E402
from backend.services.tools import build_default_registry     # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + name)
    else:
        FAIL += 1
        print("  [FAIL] " + name + " -- " + str(detail)[:250])


async def main():
    print("=" * 78)
    print("DUAL-ENGINE DIAGNOSIS VERIFICATION")
    print("=" * 78)
    cfg = llm_config()
    print("provider=%s model=%s fallback=%s  fast_path_threshold=%s" % (
        cfg["provider"], cfg["model"], cfg["fallback_model"], cfg["fast_path_confidence"]))

    # ── 1. 路由判定 ───────────────────────────────────────────────
    print("\n[1] Router decisions")
    engine = RCAEngine()

    cases = [
        ("已知模式（应走确定性快路径）",
         "payment-app P99 延迟超过 500ms，MySQL 连接池耗尽"),
        ("模糊症状（应走 Agent）",
         "用户反馈晚上 8 点支付偶尔很慢，白天正常"),
        ("磁盘告警（应走 Agent，类别不明确）",
         "磁盘空间不足 disk full"),
    ]
    decisions = {}
    for title, alert in cases:
        seed = await asyncio.to_thread(engine.infer, alert, "P1", "payment-app", False)
        d = decide(seed, cfg)
        decisions[title] = d
        print("     %-32s -> mode=%-13s conf=%.2f  %s" % (
            title[:32], d.mode, d.seed_confidence, d.reason[:80]))
    check("路由产出决策", len(decisions) == len(cases))
    check("存在确定性路径判定",
          any(d.mode == "deterministic" for d in decisions.values()),
          {k: v.mode for k, v in decisions.items()})
    check("存在 agentic 路径判定",
          any(d.mode == "agentic" for d in decisions.values()),
          {k: v.mode for k, v in decisions.items()})
    check("决策含路由依据", all(d.reason for d in decisions.values()))

    # ── 2. 确定性快路径 ───────────────────────────────────────────
    print("\n[2] Deterministic fast path (forced)")
    t0 = time.perf_counter()
    det = await diagnose(
        alert="payment-app P99 延迟超过 500ms，MySQL 连接池耗尽",
        severity="P1", force_mode="deterministic",
    )
    det_ms = (time.perf_counter() - t0) * 1000
    print("     mode=%s status=%s confidence=%s latency=%.0fms" % (
        det.get("mode"), det.get("status"), det.get("confidence"), det_ms))
    print("     root=[%s] %s" % (
        (det.get("root_cause") or {}).get("category"),
        (det.get("root_cause") or {}).get("entity_id")))
    check("确定性路径完成", det.get("mode") == "deterministic", det.get("mode"))
    check("确定性路径有根因", bool((det.get("root_cause") or {}).get("entity_id")))
    check("确定性路径零 token", det.get("total_tokens") == 0, det.get("total_tokens"))
    check("确定性路径有建议动作", len(det.get("next_actions") or []) > 0)
    check("快路径耗时 < 20s（含冷启动拓扑遍历）", det_ms < 20000, det_ms)
    check("含路由依据", bool((det.get("route") or {}).get("reason")))
    check("含本体 seed 快照", det.get("seed") is not None)
    det_incident = det.get("incident_id")

    # ── 3. Agentic 路径（真实 LLM）────────────────────────────────
    print("\n[3] Agentic path (real LLM)")
    t0 = time.perf_counter()
    ag = await diagnose(
        alert="payment-app P99 延迟超过 500ms，MySQL 连接池耗尽",
        severity="P1", force_mode="agentic",
    )
    ag_ms = (time.perf_counter() - t0) * 1000
    print("     mode=%s status=%s steps=%s tokens=%s confidence=%s latency=%.0fms" % (
        ag.get("mode"), ag.get("status"), ag.get("steps_used"),
        ag.get("total_tokens"), ag.get("confidence"), ag_ms))
    print("     tools_used=%s" % (", ".join(ag.get("tools_used") or []) or "无"))
    print("     root=[%s] %s" % (
        (ag.get("root_cause") or {}).get("category"),
        (ag.get("root_cause") or {}).get("entity_id")))
    print("     seed_agreement=%s" % ag.get("seed_agreement"))
    check("agentic 路径完成", ag.get("mode") == "agentic", ag.get("mode"))
    check("agentic 状态正常",
          ag.get("status") in ("completed", "timeout", "budget_exceeded"),
          ag.get("status") + " " + str(ag.get("error"))[:120])
    check("有推理步骤", any(s.get("phase") == "reason" for s in (ag.get("steps") or [])))
    check("有工具调用", len(ag.get("tools_used") or []) >= 1, ag.get("tools_used"))
    check("有根因", bool((ag.get("root_cause") or {}).get("entity_id")
                        or (ag.get("root_cause") or {}).get("category")))
    check("有证据链", len(ag.get("evidence") or []) > 0, len(ag.get("evidence") or []))
    check("token 被记录", ag.get("total_tokens", 0) > 0, ag.get("total_tokens"))
    check("与本体结论做了交叉验证", ag.get("seed_agreement") is not None,
          ag.get("seed_agreement"))
    check("有 run_id", bool(ag.get("run_id")), ag.get("run_id"))
    ag_run = ag.get("run_id")

    # ── 4. 落库与回放 ─────────────────────────────────────────────
    print("\n[4] Persistence & replay")
    if ag_run:
        run = agent_store.get_run(ag_run)
        check("run 已落库", bool(run), ag_run)
        thoughts = agent_store.get_thoughts(ag_run)
        check("thoughts 已落库", len(thoughts) > 0, len(thoughts))
        if thoughts:
            phases = {t.get("phase") for t in thoughts}
            print("     thoughts=%d  phases=%s" % (len(thoughts), sorted(phases)))
            check("含 observe 阶段", "observe" in phases, phases)
            check("含 reason 阶段", "reason" in phases, phases)
            check("含 act 阶段", "act" in phases, phases)
            reason_rows = [t for t in thoughts if t.get("phase") == "reason"]
            check("推理内容已保存",
                  any((t.get("reasoning") or "") for t in reason_rows), len(reason_rows))
            act_rows = [t for t in thoughts if t.get("phase") == "act"]
            check("工具观察已保存",
                  any((t.get("observation") or "") for t in act_rows), len(act_rows))
    else:
        check("run_id 存在", False, "no run_id")

    runs = agent_store.list_runs(limit=10)
    check("run 列表可查询", len(runs) > 0, len(runs))
    stats = agent_store.stats()
    print("     store stats: %s" % json.dumps(stats, ensure_ascii=False)[:200])
    check("统计后端为 mysql", stats.get("backend") == "mysql", stats.get("backend"))
    check("统计含 runs 数", stats.get("total_runs", 0) > 0, stats.get("total_runs"))

    # ── 5. 工具目录 ───────────────────────────────────────────────
    print("\n[5] Tool catalog")
    reg = build_default_registry()
    layers = reg.layers()
    print("     layers=%s  total=%d" % (
        json.dumps({k: len(v) for k, v in layers.items()}, ensure_ascii=False), len(reg.names())))
    check("四层工具齐备", set(layers.keys()) == {"ontology", "metrics", "env", "knowledge"},
          list(layers.keys()))
    check("工具数 >= 20", len(reg.names()) >= 20, len(reg.names()))
    check("Schema 可生成", len(reg.schemas()) == len(reg.names()))
    check("全部只读", all(s.read_only for s in reg._tools.values()))

    # ── 汇总 ──────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("RESULT: %d passed, %d failed" % (PASS, FAIL))
    print("=" * 78)
    print("\n[成本] deterministic=%d tokens  agentic=%d tokens" % (
        det.get("total_tokens", 0), ag.get("total_tokens", 0)))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
