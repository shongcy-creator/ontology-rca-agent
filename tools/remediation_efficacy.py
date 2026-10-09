# -*- coding: utf-8 -*-
"""
处置动作的**实测闭环**：本体给的 P0 动作，真的能揭示对应故障吗？

## 为什么需要它

第 4 轮（ontology_v4）把「怎么办」交还本体，但当时的评估口径
`remediation_contract` 只校验**可用性**（动作具体 / urgency 合法 / 是否声明审批），
**无法回答"这条动作在真实故障里是否真的管用"**。本工具补这一环。

## 测试设计（关键在"判别力"，不只是"能跑"）

对每个故障场景做完整闭环：

    ① 基线（无故障）执行全部探针 → 记录
    ② 注入故障 → 等信号成立
    ③ **真跑一次诊断**（deterministic）→ 取 top-1 根因术语（用户实际会看到的那套动作）
    ④ 再次执行全部探针 → 记录
    ⑤ 回滚 + 清理

判定该术语的 P0 探针是否有效，**必须同时满足**：

    · 基线阴性（无故障时不报）—— 否则它是"恒为真"的废探针
    · 故障期阳性（有故障时报警）

只测"故障期能报"是不够的：一个 `> 0` 的判断式在基线不为 0 时永远成立，
看起来"有效"其实毫无判别力（本项目已经栽过两次：`slow_queries > 0`、
`created_tmp_disk_tables > 0` —— 见验证手册 §11.9）。

## 口径说明

探针只覆盖**机器可执行**的 P0 命令（PromQL / SQL）。P0 里还有一类是散文描述
（"按 app_instance 对比 P99"）或带 `<占位符>` 的命令，无法自动执行 ——
本工具会把它们单列为 `not_verifiable`，**这也是实测结论的一部分**：
"可执行性"如果只停留在文字上，就没法被验证。

用法：
  python tools/remediation_efficacy.py --dry-run          # 只看探针清单
  python tools/remediation_efficacy.py --faults db_disk_temp_tables,db_row_lock_hold
  python tools/remediation_efficacy.py                    # 全部可测场景
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rca-agent"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

from tools.chaos import FAULTS, Injector                                    # noqa: E402
from tools.chaos.core import (                                              # noqa: E402
    CHAOS_DIR, MYSQL_PRIMARY, MYSQL_REPLICAS, app_replicas,
    mysql_scalar, mysql_sql, prom_query,
)

# ═════════════════════════════════════════════════════════════════════════════
# 探针定义：术语 → 一组 (探针名, 取值函数, 阳性判定) 的**只读**观测
# ═════════════════════════════════════════════════════════════════════════════

def _prom(expr: str) -> Optional[float]:
    """取 PromQL 的最大值（跨 series），拿不到返回 None。"""
    rows = prom_query(expr)
    vals: List[float] = []
    for r in rows or []:
        try:
            v = float((r.get("value") or [None, None])[1])
        except (TypeError, ValueError):
            continue
        if v == v and abs(v) != float("inf"):
            vals.append(v)
    return max(vals) if vals else None


def _mysql_int(node: str, sql: str) -> Optional[float]:
    v = mysql_scalar(node, sql)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _replication_service_state(node: str, which: str) -> str:
    sql = ("SELECT SERVICE_STATE FROM performance_schema."
           + ("replication_connection_status" if which == "io"
              else "replication_applier_status"))
    return str(mysql_scalar(node, sql) or "")


def _running_replicas() -> Optional[float]:
    try:
        reps = app_replicas()
    except Exception:  # noqa: BLE001
        return None
    return float(len([r for r in reps if r.get("state") == "running"]))


def _oom_evidence() -> Optional[float]:
    """任一应用副本出现 OOMKilled 或 RestartCount>0 记为 1。"""
    try:
        import subprocess
        names = [r["name"] for r in app_replicas()]
    except Exception:  # noqa: BLE001
        return None
    for n in names:
        p = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.OOMKilled}} {{.RestartCount}}", n],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = (p.stdout or "").strip().split()
        if len(out) == 2:
            if out[0].lower() == "true":
                return 1.0
            try:
                if int(out[1]) > 0:
                    return 1.0
            except ValueError:
                pass
    return 0.0


def _replication_states(node: str) -> str:
    """副本复制线程状态，形如 "io=ON,sql=ON"。"""
    return "io=%s,sql=%s" % (_replication_service_state(node, "io"),
                             _replication_service_state(node, "sql"))


def _replica_delay(node: str) -> Optional[float]:
    """副本的期望复制延迟（SOURCE_DELAY／DESIRED_DELAY）；仅延迟注入时非 0。"""
    return _mysql_int(node, "SELECT DESIRED_DELAY FROM "
                            "performance_schema.replication_applier_configuration LIMIT 1")


def _mem_pressure_ratio() -> Optional[float]:
    """容器内存用量 / 限额的最大比值（内存压力类故障的特征量）。"""
    return _prom("max(cc_container_memory_working_set_bytes / cc_container_memory_limit_bytes)")


#: 术语 → [(探针名, 取值, 阳性判定, 人类可读口径)]
PROBES: Dict[str, List[Tuple[str, Callable[[], Any], Callable[[Any], bool], str]]] = {
    "rc:tmp-disk": [
        ("created_tmp_disk_tables_rate",
         lambda: _prom("rate(mysql_global_status_created_tmp_disk_tables[2m])"),
         lambda v: v is not None and v > 0.05,
         "主库磁盘临时表速率 > 0.05/s"),
    ],
    "rc:row-lock": [
        ("innodb_row_lock_waits_rate",
         lambda: _prom("rate(mysql_global_status_innodb_row_lock_waits[2m])"),
         # 阈值 0.02（原为 0.05）：三次实测故障期分别是 0.19 / 0.23 / **0.038**，
         # 0.05 贴着故障量级下沿 → 最后一次判成 never_on、动作有效率从 10/10 掉到 9/10。
         # 空闲基线恒为 0，因此 0.02 仍有充分区分度。与告警规则 MySQLRowLockContention 一致。
         lambda v: v is not None and v > 0.02,
         "行锁等待新增速率 > 0.02/s（空闲基线为 0；原阈值 0.05 曾误判为无效）"),
    ],
    "rc:conn-exhaust": [
        ("threads_connected",
         lambda: _mysql_int(MYSQL_PRIMARY, "SELECT VARIABLE_VALUE FROM performance_schema.global_status "
                                           "WHERE VARIABLE_NAME='Threads_connected'"),
         lambda v: v is not None and v >= 120,
         "Threads_connected ≥ 120（max_connections=200 的 60%）"),
    ],
    "rc:db-cpu-starve": [
        ("threads_running",
         lambda: _mysql_int(MYSQL_PRIMARY, "SELECT VARIABLE_VALUE FROM performance_schema.global_status "
                                           "WHERE VARIABLE_NAME='Threads_running'"),
         lambda v: v is not None and v > 8,
         "Threads_running > 8（并发执行线程高企）"),
    ],
    "rc:primary-readonly": [
        ("read_only",
         lambda: _mysql_int(MYSQL_PRIMARY, "SELECT @@read_only"),
         lambda v: v == 1,
         "主库 read_only = 1"),
    ],
    "rc:replica-lag": [
        # 延迟是 SOURCE_DELAY 造的：两个复制线程都保持 Running，
        # 所以"线程状态"这个探针**测不出来**（第一版就错在这，判成 never_on）。
        # 忠实于动作原文（"确认复制线程状态与落后量"）的探针是落后量本身。
        ("replica_desired_delay",
         lambda: _replica_delay(MYSQL_REPLICAS[0]),
         lambda v: v is not None and v > 0,
         "副本 SOURCE_DELAY > 0（数据陈旧；线程仍 ON）"),
        ("replica_threads_state",
         lambda: _replication_states(MYSQL_REPLICAS[0]),
         lambda v: "io=ON" not in str(v) or "sql=ON" not in str(v),
         "复制线程非全 ON（对延迟注入不敏感，仅作对照）"),
    ],
    "rc:db-replica-loss": [
        # io_stop 停的是 IO 线程、kill 是整节点不可达：探针要覆盖"任一复制线程非 ON"，
        # 只盯 SQL 线程会漏掉 io_stop（第一版就是这样判成 never_on）。
        ("replication_threads_state",
         lambda: _replication_states(MYSQL_REPLICAS[0]),
         lambda v: "io=ON" not in str(v) or "sql=ON" not in str(v),
         "复制 IO 或 SQL 线程非 ON（中断/不可达）"),
    ],
    "rc:replica-loss": [
        ("running_replicas",
         _running_replicas,
         lambda v: v is not None and v < 3,
         "在册 running 应用副本 < 3"),
    ],
    "rc:oom-kill": [
        # OOMKill 是**结果**；内存压力是**过程**。只看 OOMKilled 标志会漏掉
        # "正在被内存压垮但还没被杀"的故障（app_memory_stress 就是这种）。
        ("mem_pressure_ratio",
         _mem_pressure_ratio,
         lambda v: v is not None and v > 0.9,
         "容器内存用量 / 限额 > 0.9（正在被压垮）"),
        ("oom_evidence",
         _oom_evidence,
         lambda v: v is not None and v >= 1,
         "存在 OOMKilled 或 RestartCount>0（已被杀）"),
    ],
    "rc:cpu-throttle": [
        # 阈值 0.1 与窗口 [1m] 都取自该场景**已在用的**告警/信号口径：
        # 实测空载基线 rate(throttled_seconds[1m]) ≈ 0.001~0.003，用 `> 0` 会恒为真；
        # 而窗口用 [5m] 会把刚注入 1~2 分钟的尖峰稀释到 0.096（低于阈值）→ 误判 never_on。
        # 教训：探针的阈值与窗口必须与"判定这件事成立与否"的实际口径一致，
        # 否则量到的是**窗口设置**，不是故障。
        ("cpu_throttled_rate",
         lambda: _prom("rate(cc_container_cpu_throttled_seconds_total[1m])"),
         lambda v: v is not None and v > 0.1,
         "cgroup CPU 节流速率 > 0.1/s（窗口 1m；基线约 0.001~0.003）"),
    ],
}


def _analyze_commands() -> Dict[str, Any]:
    """盘点 P0 命令里有多少是机器可执行的（实测结论的一部分）。"""
    import re
    act = json.loads((ROOT / ".evoontology" / "active.json").read_text(encoding="utf-8"))
    terms = json.loads((ROOT / ".evoontology" / "versions" / act["active_version"]
                        / "terms.json").read_text(encoding="utf-8"))
    ph = re.compile(r"<[^>]+>")
    stat = {"total_p0": 0, "promql": 0, "sql": 0, "shell_prose": 0, "placeholder": 0, "empty": 0}
    detail: List[Dict[str, str]] = []
    for t in terms:
        if not str(t.get("id") or "").startswith("rc:"):
            continue
        for a in t.get("remediation") or []:
            if a.get("urgency") != "P0":
                continue
            stat["total_p0"] += 1
            cmd = str(a.get("command") or "").strip()
            if not cmd:
                kind = "empty"
            elif ph.search(cmd):
                kind = "placeholder"
            elif re.match(r"^(rate|sum|max|min|avg|irate|increase|histogram_quantile)\(", cmd):
                kind = "promql"
            elif re.match(r"^(SELECT|SHOW|EXPLAIN|SET)\b", cmd, re.I):
                kind = "sql"
            else:
                kind = "shell_prose"
            stat[kind] += 1
            detail.append({"term": t["id"], "kind": kind, "command": cmd[:80]})
    return {"stats": stat, "detail": detail}


def _rule_annotations() -> Dict[str, str]:
    """告警名 → 注解文本（summary + description + rca_hint）。"""
    import urllib.request
    try:
        d = json.loads(urllib.request.urlopen("http://localhost:9090/api/v1/rules",
                                             timeout=20).read().decode())
    except Exception:  # noqa: BLE001
        return {}
    out: Dict[str, str] = {}
    for g in (d.get("data") or {}).get("groups") or []:
        for r in g.get("rules") or []:
            if r.get("type") != "alerting":
                continue
            a = r.get("annotations") or {}
            txt = "；".join(str(a.get(k)) for k in ("summary", "rca_hint", "description")
                            if a.get(k))
            if txt:
                out[str(r.get("name"))] = txt
    return out


def _firing_alert_text() -> Tuple[str, List[str]]:
    """
    当前 firing 告警的**真实注解文本** —— 这才是生产环境 RCA 的输入。

    为什么要这么做：A/B 评估用的输入是 `fault.title + fault.expected_root_cause`，
    而 `expected_root_cause` 本身就是**答案的自然语言描述**
    （例如"数据库排序/分组内存配置过小（tmp_table_size），临时表落盘导致 IO 瓶颈"）。
    把它喂给引擎再统计准确率，等于让考生看着答案答题。
    本函数只取 Prometheus 里**真的在 fire** 的告警及其注解，
    不含场景目录里的任何"答案"文字。
    """
    names = [str(a.get("metric", {}).get("alertname") or "")
             for a in (prom_query('ALERTS{alertstate="firing"}') or [])]
    names = [n for n in names if n]
    ann = _rule_annotations()
    parts = [ann.get(n, "") for n in names]
    return "；".join(p for p in parts if p), names


def _alert_text_for(fault: Any) -> Tuple[str, List[str], str]:
    """
    取该场景的**告警注解文本**作为诊断输入 —— 生产环境 RCA 的真实输入。

    默认用该场景**声明的告警名**（`fault.alert_names`）的注解。
    这也正是控制台交接给智能体时用的口径（前端 `neutralPrompt()` 就是
    "告警来源：<alert_names>"），因此与线上一致。

    ⚠ 为什么不默认用"此刻正在 firing 的告警"：Prometheus 的告警在故障恢复后
    还会**继续 firing 数分钟**（`for` 窗口 + 恢复判定延迟）。连续跑多个场景时，
    上一个场景的残留告警会污染下一个场景的输入 —— 实测出现过 10/10 场景
    都拿到同一个残留告警（`MySQLSlowQueries`），导致诊断结果全部相同、
    看起来像"引擎全错"，其实是**测试输入被污染**。
    只有拿不到声明告警时才回退到 firing。

    Returns: (文本, 告警名, 来源)
    """
    ann = _rule_annotations()
    declared = [str(n) for n in (getattr(fault, "alert_names", None) or [])]
    txt = "；".join(ann.get(n, "") for n in declared if ann.get(n))
    if txt:
        return txt, declared, "declared"
    firing = [str(a.get("metric", {}).get("alertname") or "")
              for a in (prom_query('ALERTS{alertstate="firing"}') or [])]
    firing = [n for n in firing if n]
    txt2 = "；".join(ann.get(n, "") for n in firing if ann.get(n))
    return txt2, firing, "firing"


def _diagnose_top1(alert: str, retries: int = 4) -> Tuple[str, float, str]:
    """
    真跑一次确定性诊断，取 top-1 术语（用户实际看到的那套动作）。

    后端有**限流**（`services/rate_limiter.py`）：连续调用会拿到 429。
    这里按 429 退避重试 —— 限流本身是对的，回放工具应当尊重它而不是绕过它。
    """
    import urllib.error
    import urllib.request
    delay = 4.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            "http://localhost:8088/api/agent/diagnose",
            data=json.dumps({"alert": alert, "severity": "P1",
                             "mode": "deterministic", "persist": False}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=180).read().decode())
            rc = d.get("root_cause") or {}
            return (str(rc.get("entity_id") or ""), float(rc.get("confidence") or 0),
                    str(d.get("mode") or ""))
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == retries:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def _run_all_probes() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for tid, items in PROBES.items():
        for name, getter, pred, human in items:
            key = "%s::%s" % (tid, name)
            try:
                v = getter()
                out[key] = {"value": v, "positive": bool(pred(v)), "human": human}
            except Exception as e:  # noqa: BLE001
                out[key] = {"value": None, "positive": False,
                            "error": "%s: %s" % (type(e).__name__, e), "human": human}
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="处置动作实测闭环（P0 探针是否有判别力）")
    ap.add_argument("--faults", default="", help="逗号分隔的故障 id；默认取所有有探针的场景")
    ap.add_argument("--signal-timeout", type=int, default=50)
    ap.add_argument("--delay", type=float, default=6.0,
                    help="诊断之间的间隔秒数（尊重后端限流；默认 6s）")
    ap.add_argument("--hold", type=float, default=0.0, help="注入后额外保持秒数（供探针观测）")
    ap.add_argument("--json-out", default=str(CHAOS_DIR / "remediation_efficacy.json"))
    ap.add_argument("--dry-run", action="store_true", help="只打印探针与命令盘点，不注入")
    ap.add_argument("--diagnose-only", action="store_true",
                    help="不注入：只用**场景声明的告警注解**（无答案泄漏）回放全部 21 个场景的"
                         "诊断 top1，与 A/B 口径对比，量化'输入含答案'带来的虚高")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    def say(msg: str) -> None:
        if not args.quiet:
            print(msg, flush=True)

    say("=" * 88)
    say("处置动作实测闭环（ontology_v4 remediation 的 P0 探针判别力）")
    say("=" * 88)

    ana = _analyze_commands()
    s = ana["stats"]
    say("\n[1] P0 命令可执行性盘点（%d 条 P0 动作）" % s["total_p0"])
    say("    PromQL 可直接执行 : %d" % s["promql"])
    say("    SQL    可直接执行 : %d" % s["sql"])
    say("    散文描述（非命令）: %d   ← 无法自动验证" % s["shell_prose"])
    say("    含 <占位符>       : %d   ← 需人工代入" % s["placeholder"])
    say("    空                : %d" % s["empty"])

    # 探针覆盖的术语
    covered = [t for t in PROBES if PROBES[t]]
    say("\n[2] 探针覆盖 %d 个根因术语（机器可验证的部分）" % len(covered))
    for t in covered:
        say("    %-20s %s" % (t, PROBES[t][0][3]))

    # 场景选择：期望根因落在探针覆盖范围内的场景。
    # 注意用 `ontology_terms`（术语 id）而不是 `expected_root_cause`
    # —— 后者是人话描述（"数据库排序/分组内存配置过小…"），没有 id。
    by_term: Dict[str, List[str]] = {}
    for fid, f in FAULTS.items():
        for t in (getattr(f, "ontology_terms", None) or []):
            t = str(t)
            if t.startswith("rc:") and t in PROBES:
                by_term.setdefault(t, []).append(fid)
    plan: List[Tuple[str, str]] = []
    for term, fids in sorted(by_term.items()):
        plan.append((fids[0], term))          # 每个术语取一个代表场景
    if args.faults:
        want = {x.strip() for x in args.faults.split(",") if x.strip()}
        plan = [(f, t) for f, t in plan if f in want]
    say("\n[3] 计划测试 %d 个场景（每个根因术语取一个代表场景）：" % len(plan))
    for fid, term in plan:
        say("    %-24s → %s" % (fid, term))

    if args.dry_run:
        out = {"mode": "dry-run", "command_stats": ana, "plan": [
            {"fault": f, "term": t} for f, t in plan]}
        Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        say("\n[dry-run] 报告: %s" % args.json_out)
        return 0

    # ── 只跑诊断：用场景声明的告警注解回放全部 21 个场景 ──────────────────
    # 不需要注入，因为打分引擎只吃文本。这样能在几分钟内得到
    # **无答案泄漏**的全量 top1，与 A/B 口径（输入=title+expected_root_cause）直接对比。
    if args.diagnose_only:
        from tools.ontology_cluster_patch import SCENARIO_EXPECTED_TERMS as EXP
        say("\n[5] 诊断回放（输入=场景声明的告警注解，**不含答案描述**）")
        say("    说明：A/B 口径（输入 = title + expected_root_cause）的准确率由官方工具测得，")
        say("    见 `tools/ontology_scoring_verify.py`（top1 20/21）；这里只量无泄漏口径。")
        rows = []
        hit_decl = 0
        ann = _rule_annotations()
        for fid in sorted(FAULTS):
            f = FAULTS[fid]
            exp = [str(t) for t in (EXP.get(fid) or []) if str(t).startswith("rc:")]
            if not exp:
                continue
            decl = "；".join(ann.get(str(n), "") for n in (f.alert_names or []) if ann.get(str(n)))
            try:
                t_decl = _diagnose_top1(decl or f.title)[0]
            except Exception as e:  # noqa: BLE001
                say("  %-24s 诊断失败: %s" % (fid, e))
                continue
            ok = t_decl in exp
            hit_decl += 1 if ok else 0
            rows.append({"fault": fid, "expected": exp, "declared_input_top1": t_decl,
                         "ok": ok, "alert_text": decl[:160]})
            say("  %-24s 期望=%-22s top1=%-22s %s"
                % (fid, ",".join(exp), t_decl or "(空)", "OK " if ok else "MISS"))
            time.sleep(args.delay)
        n = len(rows)
        say("\n  **无泄漏 top1（场景声明的告警注解）= %d/%d = %.0f%%**"
            % (hit_decl, n, 100.0 * hit_decl / n if n else 0))
        Path(args.json_out).write_text(json.dumps(
            {"mode": "diagnose-only", "rows": rows, "declared_input": [hit_decl, n]},
            ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        say("\n报告: %s" % args.json_out)
        return 0

    inj = Injector(FAULTS, verbose=False)
    results: List[Dict[str, Any]] = []

    say("\n[4] 基线采集（无故障，探针应为阴性）")
    try:
        inj.recover_all()
    except Exception:  # noqa: BLE001
        pass
    # 基线必须**干净**：上一次运行的故障若还没衰减（例如 CPU 节流的 rate 窗口
    # 仍含上一轮的尖峰），探针会在基线就阳性 → 该术语会被误判成 always_on。
    # 实测踩过：单场景测试后紧接着跑全量，cpu_throttled_rate 基线高达 1.306。
    # 这里轮询等待衰减，最多 BASE_WAIT 秒；仍不干净就明确标注"基线受污染"。
    BASE_WAIT = 150
    t0 = time.time()
    baseline = _run_all_probes()
    while time.time() - t0 < BASE_WAIT:
        pos = [k for k, v in baseline.items() if v.get("positive")]
        if not pos:
            break
        say("    等待基线衰减（仍阳性 %d 项：%s）…"
            % (len(pos), ", ".join(p.split("::")[1] for p in pos[:3])))
        time.sleep(15)
        baseline = _run_all_probes()
    pos_base = [k for k, v in baseline.items() if v.get("positive")]
    say("    阳性探针 %d / %d %s" % (len(pos_base), len(baseline),
                                    ("← 基线受污染（等待 %ds 仍未衰减）：%s"
                                     % (BASE_WAIT, ", ".join(pos_base)))
                                    if pos_base else "（全部阴性，符合预期）"))

    for i, (fid, term) in enumerate(plan, 1):
        say("\n" + "-" * 88)
        say("[%d/%d] %s（期望根因 %s）" % (i, len(plan), fid, term))
        rec: Dict[str, Any] = {"fault": fid, "expected_term": term}
        try:
            res = inj.inject(fid, wait_signals=True, signal_timeout=args.signal_timeout)
            rec["inject_ok"] = bool(res.get("ok"))
            rec["signal_observed"] = res.get("signal_observed")
            say("    注入: ok=%s 信号成立=%s" % (res.get("ok"), res.get("signal_observed")))
            if args.hold:
                time.sleep(args.hold)

            # (a) 告警注解作为输入（无答案泄漏）
            try:
                f0 = FAULTS.get(fid)
                alert_txt, firing, src = _alert_text_for(f0)
                rec["firing_alerts"] = firing
                rec["alert_source"] = src
                rec["alert_text"] = alert_txt[:300]
                say("    告警来源=%s 告警=%s" % (src, ", ".join(firing) or "(无)"))
                top1, conf, mode = _diagnose_top1(alert_txt or "集群出现异常")
                rec.update({"top1_real_alert": top1, "confidence": conf, "mode": mode})
                say("    诊断(告警注解): top1=%s (conf=%.2f)" % (top1, conf))
            except Exception as e:  # noqa: BLE001
                rec["diagnose_error"] = "%s: %s" % (type(e).__name__, e)
                say("    诊断失败: %s" % rec["diagnose_error"])

            # (b) A/B 口径作为输入（含答案描述，用于量化泄漏影响）
            try:
                f = FAULTS.get(fid)
                ab_txt = "%s：%s" % (getattr(f, "title", ""), getattr(f, "expected_root_cause", ""))
                top1_ab, _c, _m = _diagnose_top1(ab_txt)
                rec["top1_ab_input"] = top1_ab
                say("    诊断(A/B 口径·含答案描述): top1=%s" % top1_ab)
            except Exception as e:  # noqa: BLE001
                rec["diagnose_ab_error"] = "%s: %s" % (type(e).__name__, e)

            fault_p = _run_all_probes()
            rec["baseline"] = baseline
            rec["under_fault"] = fault_p

            # 判定：**期望术语**的探针是否有判别力（动作有效性本身，
            # 与"诊断是否猜对"是两件事，分开量）
            verdicts = []
            for name, _g, _p, human in PROBES.get(term, []):
                key = "%s::%s" % (term, name)
                b = baseline.get(key, {})
                fv = fault_p.get(key, {})
                if not b.get("positive") and fv.get("positive"):
                    verdict = "effective"
                elif b.get("positive") and fv.get("positive"):
                    verdict = "always_on"
                elif b.get("positive") and not fv.get("positive"):
                    verdict = "inverted"
                else:
                    verdict = "never_on"
                verdicts.append({"probe": name, "human": human, "baseline": b.get("value"),
                                 "under_fault": fv.get("value"), "verdict": verdict,
                                 "term": term})
            # 术语级判定：**至少一条 P0 探针有判别力**即视为有效
            # （一套排查建议只要有一条能查到东西，操作人就不会空手而归；
            #   同时保留逐条明细，避免"一条有效掩盖其余无效"）
            rec["term_verdict"] = (
                "effective" if any(v["verdict"] == "effective" for v in verdicts)
                else "always_on" if any(v["verdict"] == "always_on" for v in verdicts)
                else "inverted" if any(v["verdict"] == "inverted" for v in verdicts)
                else "never_on")
            rec["verdicts"] = verdicts
            for v in verdicts:
                say("    探针[%s] %-26s 基线=%s 故障=%s → %s"
                    % (term, v["probe"], v["baseline"], v["under_fault"], v["verdict"]))
            say("    → 术语判定: %s（%d 条 P0 探针）" % (rec["term_verdict"], len(verdicts)))
        except Exception as e:  # noqa: BLE001
            rec["error"] = "%s: %s" % (type(e).__name__, e)
            say("    场景失败: %s" % rec["error"])
        finally:
            try:
                inj.recover(fid)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)
        results.append(rec)

    # ── 汇总 ────────────────────────────────────────────────────────────
    try:
        inj.recover_all()
    except Exception:  # noqa: BLE001
        pass

    # 汇总：**按术语**统计（术语级口径 = 至少一条 P0 探针有判别力）
    # 基线受污染的探针不算 always_on（那反映的是"测试开始前环境没干净"，
    # 不是"这套建议没有判别力"），单列为 contaminated。
    term_verdicts: List[str] = []
    for r in results:
        v = r.get("term_verdict")
        if v:
            term_verdicts.append(v)
    tally: Dict[str, int] = {}
    for v in term_verdicts:
        tally[v] = tally.get(v, 0) + 1
    probe_verdicts = [v for r in results for v in (r.get("verdicts") or [])]
    probe_tally: Dict[str, int] = {}
    for v in probe_verdicts:
        probe_tally[v["verdict"]] = probe_tally.get(v["verdict"], 0) + 1
    say("\n" + "=" * 88)
    say("汇总")
    say("=" * 88)
    say("  本体版本 = %s" % json.loads((ROOT / ".evoontology" / "active.json")
                                       .read_text(encoding="utf-8"))["active_version"])
    say("  场景数 = %d | 术语判定数 = %d | 探针条数 = %d"
        % (len(results), len(term_verdicts), len(probe_verdicts)))
    if pos_base:
        say("  ⚠ 基线受污染探针 %d 项：%s —— 相关术语的 always_on **不计入结论**"
            % (len(pos_base), ", ".join(pos_base)))
    for k in ("effective", "always_on", "never_on", "inverted"):
        if tally.get(k):
            say("    术语 %-12s %d" % (k, tally[k]))
    for k in ("effective", "always_on", "never_on", "inverted"):
        if probe_tally.get(k):
            say("    探针 %-12s %d" % (k, probe_tally[k]))
    eff = tally.get("effective", 0)
    say("  **动作有效率（术语级：至少一条 P0 探针有判别力）= %d/%d = %.0f%%**"
        % (eff, len(term_verdicts), (100.0 * eff / len(term_verdicts)) if term_verdicts else 0))

    # 诊断准确率（两种输入口径对比，用于量化"A/B 输入含答案"的影响）
    real_ok = sum(1 for r in results if r.get("top1_real_alert") == r.get("expected_term"))
    ab_ok = sum(1 for r in results if r.get("top1_ab_input") == r.get("expected_term"))
    n = len(results)
    say("  诊断 top1（告警注解，无泄漏）        = %d/%d" % (real_ok, n))
    say("  诊断 top1（A/B 口径，输入含答案描述）= %d/%d" % (ab_ok, n))

    report = {"ontology_version": json.loads((ROOT / ".evoontology" / "active.json")
                                             .read_text(encoding="utf-8"))["active_version"],
              "command_stats": ana, "probe_covered_terms": covered,
              "plan": [{"fault": f, "term": t} for f, t in plan],
              "baseline_positive": pos_base, "results": results,
              "tally": tally, "probe_tally": probe_tally,
              "top1_real_alert": [real_ok, n], "top1_ab_input": [ab_ok, n]}
    Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                                   encoding="utf-8")
    say("\n报告: %s" % args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
