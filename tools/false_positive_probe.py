# -*- coding: utf-8 -*-
"""
阴性探针 v4：**系统其实没坏时，诊断引擎会不会硬造一个根因？**

## 判据为什么必须读 category

引擎把"无故障"这个**判定**也放在 `root_cause` 对象里。因此唯一可靠的依据是
`root_cause.category`：

    category/description 表示无故障  ⇒ **正确拒答**（不是假阳性）
    其它 category                    ⇒ **断言了一个故障**（假阳性）

v3 拿 `entity_id` 当"根因"，于是把答案里**引用的实体**（`db:mysql-core`、`alert:p99`、
`env:container-cc-grafana`）当成了断言 —— 造出一批假警报。**判据要读断言字段，不能读引用字段。**

## 为什么要重复采样

agentic 路径**不确定**：同一个"今天天气怎么样？"一次返回 `无故障`（正确拒答），
另一次返回具体根因。单次采样分不清"不会拒答"与"偶尔拒答"，所以每例跑 N 次，
同时报**假阳性率**与**拒答稳定性**，并按路径（确定性 / agentic）分开。
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
API = "http://127.0.0.1:8088/api/agent/diagnose"
N = 3
# 后端有"每分钟诊断次数"上限（这是好设计）→ 探针必须主动节流，
# 而不是把 429 当成"引擎没有误报"。
PACE_S = 7
RETRY_429 = 5

DECLINE = ("无故障", "非真实故障", "无有效故障证据", "不是故障", "证据不足", "no fault",
           "not a real fault", "no anomaly")

CASES = [
    ("healthy_plain", "系统当前状态正常，没有异常"),
    ("residual_alert", "MySQLSlowQueries：MySQL 慢查询数偏高（该告警在本次变更前就已存在）"),
    ("single_transient", "AppHighErrorRate：应用副本 1 在 12:00:03 出现过一次 HTTP 500，之后 30 分钟无复现"),
    ("already_recovered", "MySQLReplicationBroken：复制中断告警已于 5 分钟前恢复，当前 io/sql 线程均为 ON"),
    ("informational", "例行配置变更：已把 long_query_time 从 0.1s 调整为 1s"),
    ("unrelated_alert", "GrafanaSlowRender：Grafana 自身的面板渲染耗时偏高"),
    ("out_of_scope", "今天天气怎么样？"),
    ("metrics_only_ok", "t_txn 行数增长与往常一致，副本延迟为 0"),
]


def verdict(body):
    if not isinstance(body, dict):
        return "invalid", "no dict"
    rc = body.get("root_cause")
    if rc is None:
        return "decline", "no root_cause field"
    if isinstance(rc, dict):
        cat = str(rc.get("category") or "")
        desc = str(rc.get("description") or "")
        if any(m in cat or m in desc for m in DECLINE):
            return "decline", cat or "declined"
        return "assert", cat or "?"
    if isinstance(rc, str):
        return ("decline" if any(m in rc for m in DECLINE) else "assert"), rc[:40]
    return "invalid", "unexpected type"


def post(msg):
    last = None
    for attempt in range(RETRY_429 + 1):
        time.sleep(PACE_S)                      # 主动节流：不与后端限流对抗
        rq = urllib.request.Request(API, data=json.dumps({"alert": msg}).encode("utf-8"),
                                    headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(rq, timeout=240) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            last = "HTTP %s: %s" % (e.code, body[:120])
            if e.code == 429 and attempt < RETRY_429:
                print("      （429 限流，等 65s 后重试 %d/%d）" % (attempt + 1, RETRY_429))
                time.sleep(65)
                continue
            return None, last
        except Exception as e:  # noqa: BLE001
            return None, "%s: %s" % (type(e).__name__, str(e)[:120])
    return None, last


def main():
    rows, fails = [], 0
    for cid, msg in CASES:
        runs = []
        for _ in range(N):
            body, err = post(msg)
            if err:
                fails += 1
                runs.append({"verdict": "invalid", "mode": None, "detail": err[:50]})
                continue
            v, detail = verdict(body)
            runs.append({"verdict": v, "mode": (body or {}).get("mode"), "detail": detail})
        asserts = sum(1 for r in runs if r["verdict"] == "assert")
        declines = sum(1 for r in runs if r["verdict"] == "decline")
        stable = len({r["verdict"] for r in runs}) == 1
        rows.append({"case": cid, "input": msg, "runs": runs, "n": len(runs),
                     "assert": asserts, "decline": declines,
                     "fp_rate": round(asserts / len(runs), 3), "stable": stable,
                     "modes": sorted({r["mode"] for r in runs if r["mode"]})})
        print("  %-17s 断言 %d/%d 拒答 %d/%d 一致=%-3s 路径=%-14s %s"
              % (cid, asserts, len(runs), declines, len(runs), "是" if stable else "否",
                 ",".join(rows[-1]["modes"]) or "-",
                 "/".join(str(r["detail"]) for r in runs)[:40]))

    tot = sum(r["n"] for r in rows)
    ta = sum(r["assert"] for r in rows)
    stable = sum(1 for r in rows if r["stable"])
    det = [x for r in rows for x in r["runs"] if x.get("mode") == "deterministic"]
    agt = [x for r in rows for x in r["runs"] if x.get("mode") == "agentic"]
    out = {
        "probe": "false-positive (healthy inputs), N runs per case",
        "verdict_rule": "no-fault category/description ⇒ correct decline; any other category ⇒ the "
                        "engine asserted a fault. Reference fields such as entity_id are NOT used "
                        "(using them produced a false alarm in v3).",
        "runs_per_case": N, "cases": len(rows), "total_runs": tot, "requests_failed": fails,
        "valid": fails == 0,
        "false_positive_rate": round(ta / tot, 4) if tot else None,
        "decline_stability": round(stable / len(rows), 4) if rows else None,
        "by_path": {
            "deterministic": {"runs": len(det), "fp_rate": round(
                sum(1 for x in det if x["verdict"] == "assert") / len(det), 4) if det else None},
            "agentic": {"runs": len(agt), "fp_rate": round(
                sum(1 for x in agt if x["verdict"] == "assert") / len(agt), 4) if agt else None}},
        "supersedes": "earlier runs of this probe (0/8 when the requests were rejected with 422, "
                      "then 8/8 when the engine's explicit no-fault verdict was counted as an "
                      "assertion) — both were measurement errors, not engine behaviour",
        "results": rows,
    }
    (ROOT / ".chaos").mkdir(exist_ok=True)
    for d in (ROOT / ".chaos", ROOT / "reports"):
        (d / "false_positive_probe.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                                     encoding="utf-8")
    print("\n  请求失败 %d/%d  有效=%s" % (fails, tot, out["valid"]))
    print("  假阳性 %d/%d = %.1f%%   拒答稳定 %d/%d 场景"
          % (ta, tot, 100.0 * (out["false_positive_rate"] or 0), stable, len(rows)))
    print("  分路径：确定性 %.1f%%（%d 次） / agentic %.1f%%（%d 次）"
          % (100.0 * (out["by_path"]["deterministic"]["fp_rate"] or 0), len(det),
             100.0 * (out["by_path"]["agentic"]["fp_rate"] or 0), len(agt)))
    return 0 if out["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
