# -*- coding: utf-8 -*-
"""
一键验证流水线：按正确顺序跑完全部验证并产出一份**自带新鲜度标记**的汇总报告。

## 为什么需要它

在这之前，复现全量证据要手工按顺序跑 5~6 个脚本（其中 21 场景注入约 25 分钟），
很容易漏步、顺序错或**用了过期的产物当结论** —— 本项目就真实发生过：
`fault_verify_report.json` 里的"21/21"产出于改动注入实现之前，
却被当成当前代码的能力证据。

所以本脚本除了串流程，还做两件针对这个教训的事：

  1. **代码指纹**：把关键实现文件的摘要记进报告。读者的第一句话应该是
     "这份报告对应哪一版代码"，而不是默认它是最新的。
  2. **产物新鲜度**：记录每个产物的 mtime，并与"本轮运行开始时间"比较，
     标出 `fresh` / `stale`（旧文件没被本次运行覆盖 = 结论可能是旧的）。

## 步骤顺序（不能随意调换）

    doctor（环境干净）→ 本体卫生 → A/B（打分回归）→ 21 场景故障验证
    → 处置动作有效性 → 压测基线 → 端到端注入→诊断 → **cleanup（finally 必跑）**

  · `doctor` 必须最先：环境不干净时后面所有数字都无意义；
  · 破坏性步骤（故障注入 / 端到端）必须在读类步骤之后；
  · `cleanup` 放在 `finally`：无论中途失败还是被 Ctrl+C，都要回滚——
    "清理是安全网"这条不变式在本项目里已经被违反过一次（见验证手册 §11.17）。

用法：
  python tools/run_all_verification.py --fast          # 只跑读类快速步骤（默认）
  python tools/run_all_verification.py --full          # 全量（含 21 场景注入与端到端，约 1 小时）
  python tools/run_all_verification.py --only doctor,hygiene
  python tools/run_all_verification.py --full --skip end_to_end
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

CHAOS = ROOT / ".chaos"
PY = sys.executable

#: 参与"代码指纹"的关键文件 —— 它们的变更会让旧结论失效
FINGERPRINT_FILES = [
    "tools/chaos/catalog.py",
    "tools/chaos/core.py",
    "tools/fault_injector.py",
    "tools/remediation_efficacy.py",
    "tools/cluster_rca_verify.py",
    "tools/evolve_ontology_cluster.py",
    "tools/ontology_remediation_patch.py",
    "tools/ontology_v5_patch.py",
    "rca-agent/backend/services/rca_engine.py",
    "rca-agent/backend/services/ontology_scoring.py",
    "rca-agent/backend/services/orchestrator.py",
    "rca-agent/monitoring/prometheus/alerts.yml",
    "rca-agent/docker-compose.yml",
]


def code_fingerprint() -> Dict[str, Any]:
    """
    关键实现文件的指纹（路径 → 大小+mtime 的短哈希）。

    目的：让"这份证据对应哪一版代码"成为报告里的第一等信息。
    """
    items: Dict[str, str] = {}
    h = hashlib.sha256()
    for rel in FINGERPRINT_FILES:
        p = ROOT / rel
        if not p.exists():
            items[rel] = "MISSING"
            continue
        st = p.stat()
        sig = "%s:%d:%.0f" % (rel, st.st_size, st.st_mtime)
        digest = hashlib.sha256(sig.encode()).hexdigest()[:10]
        items[rel] = digest
        h.update(sig.encode())
    return {"combined": h.hexdigest()[:16], "files": items}


#: 步骤定义。required=True 的步骤失败 → 整条流水线判 FAIL。
STEPS: List[Dict[str, Any]] = [
    {
        "name": "doctor", "title": "环境体检（18 项）", "required": True,
        "cmd": [PY, "tools/fault_injector.py", "doctor"],
        "artifacts": [], "fast": True, "timeout": 600,
    },
    {
        "name": "hygiene", "title": "本体卫生 + 演化工件", "required": True,
        "cmd": [PY, "tools/ontology_hygiene.py"],
        "artifacts": [], "fast": True, "timeout": 600,
    },
    {
        "name": "scoring_ab", "title": "打分 A/B（引擎 top1 回归）", "required": True,
        "cmd": [PY, "tools/ontology_scoring_verify.py",
                "--parent", "{prev_version}", "--active", "{active_version}",
                "--top-k", "1", "--json-out", ".chaos/ontology_scoring_ab.json"],
        "artifacts": [".chaos/ontology_scoring_ab.json"], "fast": True, "timeout": 1800,
    },
    {
        "name": "fault_verify", "title": "21 场景故障注入验证", "required": True,
        "cmd": [PY, "tools/fault_injector.py", "verify",
                "--signal-timeout", "50", "--recovery-timeout", "110"],
        "artifacts": [".chaos/fault_verify_report.json"], "fast": False, "timeout": 5400,
    },
    {
        "name": "action_efficacy", "title": "处置动作有效性（实测闭环）", "required": False,
        "cmd": [PY, "tools/remediation_efficacy.py",
                "--json-out", ".chaos/remediation_efficacy.json"],
        "artifacts": [".chaos/remediation_efficacy.json"], "fast": False, "timeout": 3600,
    },
    {
        "name": "cleanup_pre_baseline", "title": "回滚全部残留（基线前置）", "required": True,
        "cmd": [PY, "tools/fault_injector.py", "cleanup"],
        "artifacts": [], "fast": False, "timeout": 600,
    },
    {
        # 不是"跑个命令"，而是**断言环境干净**：有激活故障就中止流水线。
        # 破坏性步骤之后接测量步骤时，这道断言比"睡一会儿"可靠得多。
        "name": "assert_clean", "title": "断言无激活故障（基线/端到端前置）",
        "required": True, "check": "no_active_faults",
        "artifacts": [], "fast": False, "timeout": 120,
    },
    {
        "name": "stress_baseline", "title": "压测基线（32×45s）", "required": False,
        # ⚠ 基线必须在**干净环境**下测。实测踩过：这一步紧跟在 action_efficacy 之后，
        # 而上一步注入的 `stress-ng --timeout 900s`（15 分钟）在回滚后仍可能残留，
        # 于是"基线"测出来是 704 请求 / 15 rps / p50 2050ms —— 而真正的基线是
        # 405 rps / p50 74ms，差了 26 倍。数字被污染比没有数字更糟。
        # 因此这里串了两道：先 cleanup，再**断言**无激活故障（断言失败即中止）。
        "cmd": [PY, "tools/stress_harness.py", "--mode", "http", "--concurrency", "32",
                "--duration", "45", "--label", "pipeline-baseline",
                "--json-out", ".chaos/stress_pipeline.json"],
        "artifacts": [".chaos/stress_pipeline.json"], "fast": False, "timeout": 900,
    },
    {
        "name": "alert_coverage", "title": "告警覆盖率实测（注入→等足够久→看告警是否 firing）",
        "required": False,
        # 单独成步的理由：端到端要 4~6 分钟/场景（含诊断与评分），而"告警可不可得"
        # 是它的**前置条件**，本身值得廉价地单独测（不调 LLM、不评分）。
        # 实测暴露过的规则阈值/窗口/漏报写法问题都靠它定位（见验证手册 §11.27）。
        "cmd": [PY, "tools/alert_coverage_check.py",
                "--json-out", ".chaos/alert_coverage.json", "--quiet"],
        "artifacts": [".chaos/alert_coverage.json"], "fast": False, "timeout": 10800,
    },
    {
        "name": "promotion_ab", "title": "组件级→根因级升级 成对评估（回放真实告警文本）",
        "required": False,
        # 回放端到端产物里的 alert_text，on/off 对比严格命中率与"落在组件上"的比例。
        # 秒级完成，因此放在端到端之后、作为它的补充口径。
        "cmd": [PY, "tools/rootcause_promotion_verify.py",
                "--json-out", ".chaos/promotion_ab.json"],
        "artifacts": [".chaos/promotion_ab.json"], "fast": True, "timeout": 900,
    },
    {
        "name": "cleanup_pre_e2e", "title": "回滚全部残留（端到端前置）", "required": True,
        "cmd": [PY, "tools/fault_injector.py", "cleanup"],
        "artifacts": [], "fast": False, "timeout": 600,
    },
    {
        "name": "assert_clean_e2e", "title": "断言无激活故障（端到端前置）",
        "required": True, "check": "no_active_faults",
        "artifacts": [], "fast": False, "timeout": 120,
    },
    {
        "name": "end_to_end", "title": "端到端：注入→压测→告警→诊断→评分", "required": False,
        # 不加 --quiet：这条要跑约 1 小时，日志里必须能看到逐场景进度，
        # 否则中途无法判断它是"在跑"还是"卡住"。
        "cmd": [PY, "tools/cluster_rca_verify.py", "--mode", "auto",
                "--json-out", ".chaos/rca_diagnosis_report.json"],
        # 实测耗时口径：21 场景 ×（hold 240s + 压测 150s + 诊断 ~20s + 回滚/间隔）≈ 380s/场景
        # → 全程约 8000s。原先设 7200s，结果**跑到 19/21 被超时掐断**（artifact 已落盘但不完整）。
        # 预算必须按"实测每场景耗时 × 场景数"给，而不是拍一个看起来够大的数。
        "artifacts": [".chaos/rca_diagnosis_report.json"], "fast": False, "timeout": 12600,
    },
    {
        # 跨集群归属：比 top1 更粗、但更可操作（"该找谁"：应用值班 / DBA / 平台）。
        # 放在端到端之后 —— 它回放端到端产出的真实告警文本与诊断结果。
        "name": "crosscluster", "title": "跨集群归属验证（根因落在哪个集群）",
        "required": False,
        "cmd": [PY, "tools/crosscluster_verify.py",
                "--json-out", ".chaos/crosscluster_report.json"],
        "artifacts": [".chaos/crosscluster_report.json"], "fast": False, "timeout": 600,
    },
]


def _active_versions() -> Dict[str, str]:
    """当前激活本体 + 它的前一个已发布版本（用于 A/B 的 --parent）。"""
    out = {"active_version": "", "prev_version": ""}
    try:
        act = json.loads((ROOT / ".evoontology" / "active.json").read_text(encoding="utf-8"))
        out["active_version"] = str(act.get("active_version") or "")
    except Exception:  # noqa: BLE001
        return out
    published: List[str] = []
    vdir = ROOT / ".evoontology" / "versions"
    if vdir.is_dir():
        for d in sorted(vdir.iterdir()):
            n = d.name
            # 只取"已发布"版本（形如 ontology_vN，不带 -cluster/-scoring 这类候选后缀）
            if n.startswith("ontology_v") and "-" not in n:
                published.append(n)
    cur = out["active_version"]
    if cur in published:
        i = published.index(cur)
        if i > 0:
            out["prev_version"] = published[i - 1]
    return out


def _check_no_active_faults() -> tuple:
    """
    断言"当前没有激活故障 + 没有残留 stress-ng"。

    为什么要有这道断言：破坏性步骤（注入/动作有效性）之后紧跟测量步骤（压测基线/端到端）时，
    "回滚发出去了"不等于"负载真的停了" —— 实测就有 `stress-ng --timeout 900s` 残留，
    把压测基线从 405 rps 变成 15 rps。**被污染的数字比没有数字更糟**，
    所以这里宁可让流水线中止，也不产出一个看起来正常的错误基线。
    """
    import json as _json
    import urllib.request as _ur
    problems = []
    try:
        st = _json.loads(_ur.urlopen("http://localhost:8088/api/chaos/status",
                                     timeout=30).read().decode())
        act = st.get("active_faults") or []
        if act:
            problems.append("仍有激活故障: %s" % [a.get("fault_id") for a in act])
    except Exception as e:  # noqa: BLE001
        problems.append("读取 /api/chaos/status 失败（无法确认环境干净）: %s" % str(e)[:80])
    for c in ("rca-agent-payment-app-1", "rca-agent-payment-app-2",
              "rca-agent-payment-app-3", "cc-mysql-core"):
        try:
            p = subprocess.run(["docker", "exec", c, "sh", "-c",
                                "ps -eo args 2>/dev/null | grep -c '[s]tress-ng' || true"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=30)
            n = int((p.stdout or "0").strip().splitlines()[-1] or 0)
            if n:
                problems.append("%s 仍有 %d 个 stress-ng 进程残留" % (c, n))
        except Exception:  # noqa: BLE001
            pass
    return (not problems), "；".join(problems)


def run_step(step: Dict[str, Any], ctx: Dict[str, str], started: float) -> Dict[str, Any]:
    # ── 断言型步骤：不是"跑命令"，而是"检查环境" ──────────────────────────
    if step.get("check") == "no_active_faults":
        rec = {"name": step["name"], "title": step["title"],
               "required": bool(step.get("required")), "cmd": "(环境断言)",
               "status": "RUNNING"}
        print("\n" + "=" * 88)
        print("[%s] %s" % (step["name"], step["title"]))
        print("=" * 88, flush=True)
        t0 = time.time()
        ok, why = _check_no_active_faults()
        rec["detail"] = why
        rec["duration_s"] = round(time.time() - t0, 1)
        rec["status"] = "OK" if ok else "FAIL"
        print("  → %s%s" % (rec["status"], ("：" + why) if why else ""), flush=True)
        rec["artifacts"] = []
        return rec

    cmd = [c.format(**ctx) for c in step["cmd"]]
    rec: Dict[str, Any] = {"name": step["name"], "title": step["title"],
                           "required": bool(step.get("required")),
                           "cmd": " ".join(cmd[1:]), "status": "RUNNING"}
    print("\n" + "=" * 88)
    print("[%s] %s%s" % (step["name"], step["title"],
                         "" if step.get("required") else "（非必需）"))
    print("  $ %s" % " ".join(cmd[1:]))
    print("=" * 88, flush=True)
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), timeout=step.get("timeout") or None)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        rc = -9
        print("  ⏱ 超时（%ss）" % step.get("timeout"))
    except KeyboardInterrupt:
        rec.update({"status": "INTERRUPTED", "duration_s": round(time.time() - t0, 1)})
        raise
    rec["returncode"] = rc
    rec["duration_s"] = round(time.time() - t0, 1)
    rec["status"] = "OK" if rc == 0 else "FAIL"

    # 产物新鲜度：本次运行开始**之后**被写入才算 fresh
    arts = []
    for rel in step.get("artifacts") or []:
        p = ROOT / rel
        if not p.exists():
            arts.append({"path": rel, "exists": False, "fresh": False})
            continue
        mtime = p.stat().st_mtime
        arts.append({"path": rel, "exists": True,
                     "mtime": datetime.fromtimestamp(mtime, timezone.utc).strftime(
                         "%Y-%m-%dT%H:%M:%SZ"),
                     "fresh": mtime >= started,
                     "size": p.stat().st_size})
    rec["artifacts"] = arts
    print("  → %s（%.1fs）" % (rec["status"], rec["duration_s"]), flush=True)
    for a in arts:
        print("     %s %s%s" % ("✔" if a.get("fresh") else "⚠",
                                a["path"], "" if a.get("fresh") else "  ← 本次未更新（可能是旧结论）"))
    return rec


def cleanup() -> None:
    """安全网：无论如何都跑一次回滚（本项目的硬不变式）。"""
    print("\n" + "=" * 88)
    print("[cleanup] 兜底回滚（finally 必跑）")
    print("=" * 88, flush=True)
    try:
        subprocess.run([PY, "tools/fault_injector.py", "cleanup"],
                       cwd=str(ROOT), timeout=600)
    except Exception as e:  # noqa: BLE001
        print("  cleanup 失败：%s" % e)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="一键验证流水线（顺序化 + 新鲜度标记）")
    ap.add_argument("--full", action="store_true", help="包含耗时步骤（21 场景注入、端到端）")
    ap.add_argument("--fast", action="store_true", help="只跑读类快速步骤（默认）")
    ap.add_argument("--only", default="", help="只跑这些步骤（逗号分隔）")
    ap.add_argument("--skip", default="", help="跳过这些步骤（逗号分隔）")
    ap.add_argument("--no-cleanup", action="store_true", help="不跑收尾回滚（不推荐）")
    ap.add_argument("--json-out", default=str(CHAOS / "run_all_verification.json"))
    args = ap.parse_args(argv)

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    skip = {x.strip() for x in args.skip.split(",") if x.strip()}
    selected = []
    for s in STEPS:
        if only and s["name"] not in only:
            continue
        if s["name"] in skip:
            continue
        if not only and not args.full and not s.get("fast"):
            continue
        selected.append(s)

    ctx = _active_versions()
    started = time.time()
    fp = code_fingerprint()

    print("=" * 88)
    print("一键验证流水线")
    print("=" * 88)
    print("  模式            %s" % ("--full（含耗时步骤）" if args.full else "--fast（读类）"))
    print("  步骤            %s" % ", ".join(s["name"] for s in selected))
    print("  激活本体        %s（A/B 父版本 %s）" % (ctx["active_version"] or "?",
                                                    ctx["prev_version"] or "?"))
    print("  代码指纹        %s" % fp["combined"])
    print("  开始            %s" % datetime.fromtimestamp(started, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"))

    results: List[Dict[str, Any]] = []
    interrupted = False
    aborted = False
    try:
        for s in selected:
            try:
                rec = run_step(s, ctx, started)
            except KeyboardInterrupt:
                interrupted = True
                print("\n[!] 收到中断，停止后续步骤（仍会执行收尾回滚）")
                break
            results.append(rec)
            # ── 必需步骤失败 → **不再往下跑** ─────────────────────────────
            # 步骤顺序的全部意义就在于"环境不干净时不要做破坏性验证"：
            # doctor 失败还接着跑 21 场景注入，只会在不可信的环境里产出一堆数字。
            # （非必需步骤失败不拦，只如实记进汇总。）
            if s.get("required") and rec["status"] != "OK":
                skipped = [x["name"] for x in selected[selected.index(s) + 1:]]
                if skipped:
                    print("\n[!] 必需步骤 %s 失败 → 跳过后续步骤：%s"
                          % (s["name"], ", ".join(skipped)))
                aborted = True
                break
            aborted = False
    finally:
        if not args.no_cleanup:
            cleanup()

    req_fail = [r for r in results if r["required"] and r["status"] != "OK"]
    stale = [(r["name"], a["path"]) for r in results
             for a in (r.get("artifacts") or []) if a.get("exists") and not a.get("fresh")]
    summary = {
        "mode": "full" if args.full else "fast",
        "started_at": datetime.fromtimestamp(started, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "duration_s": round(time.time() - started, 1),
        "code_fingerprint": fp,
        "ontology": ctx,
        "steps": results,
        "required_failures": [r["name"] for r in req_fail],
        "stale_artifacts": [{"step": s, "path": p} for s, p in stale],
        "ok": not req_fail and not interrupted,
        "interrupted": interrupted,
        # 因必需步骤失败而提前收工（后续步骤未执行）—— 必须在报告里说清楚，
        # 否则读者会以为"列出的步骤就是全部步骤"
        "aborted_early": aborted,
    }
    Path(args.json_out).write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                  encoding="utf-8")

    print("\n" + "=" * 88)
    print("流水线汇总")
    print("=" * 88)
    print("  代码指纹 %s | 激活本体 %s | 用时 %.1fs"
          % (fp["combined"], ctx["active_version"] or "?", summary["duration_s"]))
    for r in results:
        print("    %-16s %-10s %6.1fs  %s"
              % (r["name"], r["status"], r["duration_s"], r["title"]))
    if req_fail:
        print("  ❌ 必需步骤失败：%s" % ", ".join(r["name"] for r in req_fail))
    if stale:
        print("  ⚠ 产物未在本次更新（结论可能是旧的）：%s"
              % ", ".join(p for _s, p in stale))
    if aborted:
        print("  ⚠ 因必需步骤失败而提前收工：后续步骤未执行")
    print("  总体: %s" % ("PASS" if summary["ok"] else
                          ("INTERRUPTED" if interrupted else "FAIL")))
    print("\n报告: %s" % args.json_out)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
