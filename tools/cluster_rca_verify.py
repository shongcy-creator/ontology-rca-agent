#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
端到端：故障注入 → 压测放大 → 告警 → **智能体诊断** → 自动评分。

这是"验证智能体问题诊断分析能力"的主程序。对每个故障场景：

  1. 注入故障（tools/chaos）
  2. 后台叠加压力测试（tools/stress_harness.py），把单点故障放大成可观测的层间故障
  3. 等待 Prometheus 采集与告警规则评估
  4. **从告警系统里取告警文本**（Prometheus 规则的 rca_hint 注解，firing/pending 都收）
     —— 刻意不使用故障目录里的描述，避免把答案泄露给智能体；
        智能体看到的信息与真实值班人员看到的完全一致
  5. 调 POST /api/agent/diagnose 让双引擎（本体确定性快检 + LLM Agent）诊断
  6. 用 ground truth（场景 → 期望根因 Term）给结论打分：
       命中 = 最终根因实体 ∈ 期望 Term
            或 期望 Term 出现在候选/证据/推理摘要里
            或 命中该场景的语义关键词（文本级回退）
  7. 回滚故障、校验恢复，进入下一个场景

选用的告警文本来自 Prometheus，因此**同时验证了监控告警规则与本体约束的对接**
（每条告警的 onto_constraint 指向本体约束 id，rca_hint 是给智能体的自然语言线索）。

用法：
  python tools/cluster_rca_verify.py                       # 全部场景，确定性快路径
  python tools/cluster_rca_verify.py --mode agentic        # 走 LLM Agent（慢、耗 token）
  python tools/cluster_rca_verify.py --mode both           # 两条路径都跑并对比
  python tools/cluster_rca_verify.py app_oom_kill db_row_lock_hold --hold 75
  python tools/cluster_rca_verify.py --json-out .chaos/rca_diagnosis_report.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.chaos import FAULTS, Injector                                  # noqa: E402
from tools.chaos.core import (                                            # noqa: E402
    CHAOS_DIR,
    PROM,
    RCA_API,
    active_faults,
    app_replicas,
    container_state,
    http_json,
    prom_query,
    prom_wait,
    start_stress_background,
    stop_stress_background,
)
from tools.ontology_cluster_patch import SCENARIO_EXPECTED_TERMS          # noqa: E402

# ── 文本级回退关键词（同一场景的不同说法都应该算命中）────────────────────
SCENARIO_MATCH_KEYWORDS: Dict[str, List[str]] = {
    "app_kill_replica":    ["副本丢失", "副本数", "replica", "replicas", "容器被终止", "被杀", "killed"],
    "app_stop_replica":    ["副本下线", "副本数", "replica", "缩容", "下线"],
    "app_pause_replica":   ["假死", "挂起", "pause", "无响应", "探活失败", "unresponsive"],
    "app_oom_kill":        ["OOM", "OOMKill", "内存超限", "被内核杀死", "内存不足", "mem_limit"],
    "app_cpu_stress":      ["CPU 节流", "节流", "throttle", "cpu quota", "CPU 配额", "CPU 资源"],
    "app_memory_stress":   ["内存压力", "内存不足", "内存逼近", "memory", "OOM"],
    "app_network_delay":   ["网络延迟", "链路", "延迟", "network", "netem"],
    "app_network_loss":    ["丢包", "网络", "链路", "loss", "packet"],
    "app_gateway_stop":    ["网关", "gateway", "负载均衡", "入口", "不可达"],
    "db_row_lock_hold":    ["行锁", "锁等待", "lock", "长事务", "阻塞"],
    "db_slow_query_flood": ["慢查询", "全表扫描", "索引", "slow", "sql"],
    "db_conn_saturation":  ["连接耗尽", "连接数", "max_connections", "连接资源", "Too many connections"],
    "db_cpu_stress":       ["数据库 CPU", "CPU 资源不足", "threads_running", "并发线程", "计算资源"],
    "db_disk_temp_tables": ["临时表", "落盘", "排序", "tmp", "磁盘 IO", "内存不足"],
    "db_primary_readonly": ["只读", "read_only", "配置漂移", "写入失败", "写请求失败"],
    "db_replica_lag":      ["复制延迟", "主从延迟", "lag", "数据陈旧", "Seconds_Behind"],
    "db_replica_io_stop":  ["复制中断", "复制停止", "io thread", "sql thread", "副本同步"],
    "db_replica_kill":     ["副本丢失", "副本不可用", "replica down", "节点不可达"],
    "res_cluster_cpu":     ["集群容量", "整体 CPU", "CPU 不足", "节流", "容量不足"],
    "res_cluster_memory":  ["集群内存", "内存不足", "内存压力", "容量不足"],
    "res_db_memory":       ["排序内存", "sort_buffer", "临时表", "落盘", "内存不足"],
}

LAYER_LABEL = {
    "application": "应用层", "database": "数据库层",
    "runtime": "运行环境层", "resource": "跨层资源",
}


# ═════════════════════════════════════════════════════════════════════════════
# 告警采集（智能体的"输入"只能来自监控系统）
# ═════════════════════════════════════════════════════════════════════════════

def max_alert_for_seconds(default: float = 180.0) -> float:
    """
    取告警规则里**最大的 `for` 窗口**（秒）。用于推导"保持多久才可能等到告警"。

    为什么需要它：告警从"条件成立"到"firing"要经过 `for` 窗口 + 一次评估间隔，
    而端到端脚本原先硬编码 `--hold 55 / --db-hold 70`。实测本环境 21 条规则的
    `for` 是 **60s×9 + 120s×9 + 180s×3** —— 也就是说超过一半的场景
    **在保持期内根本来不及 firing**，于是拿不到任何告警文本，
    端到端实际上测的是"没有告警时让智能体硬猜"，结果随 LLM 路径大幅波动。
    """
    st, body = http_json(PROM + "/api/v1/rules", timeout=20)
    if st != 200 or not isinstance(body, dict):
        return default
    mx = 0.0
    for g in (body.get("data") or {}).get("groups") or []:
        for r in g.get("rules") or []:
            if r.get("type") != "alerting":
                continue
            try:
                mx = max(mx, float(r.get("duration") or 0))
            except (TypeError, ValueError):
                continue
    return mx if mx > 0 else default


def collect_alerts() -> Dict[str, Any]:
    """
    读取 Prometheus 规则状态，收集 firing/pending 告警及其 rca_hint。

    返回 {"hints": [...], "alerts": [{name,state,severity,hint,value,constraint}]}
    """
    st, body = http_json(PROM + "/api/v1/rules", timeout=20)
    out: List[Dict[str, Any]] = []
    if st == 200 and isinstance(body, dict):
        for g in (body.get("data") or {}).get("groups") or []:
            for r in g.get("rules") or []:
                if r.get("type") != "alerting":
                    continue
                state = r.get("state") or "inactive"
                if state not in ("firing", "pending"):
                    continue
                labels = r.get("labels") or {}
                ann = r.get("annotations") or {}
                val = ""
                try:
                    val = str((r.get("alerts") or [{}])[0].get("value", ""))
                except Exception:  # noqa: BLE001
                    pass
                out.append({
                    "name": r.get("name"),
                    "state": state,
                    "severity": labels.get("severity", ""),
                    "layer": labels.get("layer", ""),
                    "onto_constraint": labels.get("onto_constraint", ""),
                    "hint": ann.get("rca_hint", ""),
                    "summary": ann.get("summary", ""),
                    "value": val[:40],
                })
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    out.sort(key=lambda a: (order.get(a["severity"], 9), 0 if a["state"] == "firing" else 1))
    hints = [a["hint"] for a in out if a.get("hint")]
    return {"alerts": out, "hints": hints}


def build_alert_text(alerts: Dict[str, Any], fault: Any,
                     pre_names: Optional[set] = None) -> str:
    """
    组装喂给智能体的告警文本（**只用与该场景相关的告警**）。

    优先级（每一级都要求"告警确实在 firing"）：

      ① 本次新出现 **且** 属于该场景声明告警的（`new ∩ declared`）—— 最干净：
         "这条故障引发的、且我们预期它会引发的告警"；
      ② 该场景声明告警中正在 firing 的（`declared ∩ firing`）—— 覆盖
         "故障前就在 firing（例如上一轮残留尚未消退）"的情况；
      ③ 都没有 → 返回"监控无对应告警但业务异常"，并把 `alert_coverage` 记为 False。
         这是**有价值的情形**（说明该故障没能触发它该触发的告警），
         而不是拿一条无关告警凑数。

    ⚠ 两条血泪教训（都来自实测）：

    · **不能直接用"所有 firing 告警"**：Prometheus 告警在故障恢复后仍 firing
      数分钟，连续跑 21 场景时后面的场景会重复拿到前面的 `rca_hint`。
      实测（未过滤时）：`gateway_stop` 判成 `rc:cpu-throttle`、
      `kill_replica` 判成 `rc:replica-lag`、`memory_stress` 判成 `rc:slow-sql`。
    · **也不能直接用"新出现的告警"**：新告警可能与本故障无关。
      实测：`app_cpu_stress` 注入期间新出现的是 `MySQLReplicationLag`
      （前几轮残留的复制延迟刚好在这时触发），于是被喂了复制延迟的 hint，
      诊断成 `rc:replica-lag` —— 看起来像引擎错了，其实是**输入选错了**。
    """
    items = alerts.get("alerts") or []
    pre = set(pre_names or ())
    declared = set(getattr(fault, "alert_names", None) or [])
    names_now = {a.get("name") for a in items}

    chosen: List[Dict[str, Any]] = []
    if declared:
        # ① 本次新出现且属于声明告警
        chosen = [a for a in items
                  if a.get("name") in declared and a.get("name") not in pre]
        # ② 声明告警中正在 firing 的（不管新旧）
        if not chosen:
            chosen = [a for a in items if a.get("name") in declared]
    if not chosen:
        # ⚠ 回退文案**不能带故障标题**。
        # 踩过的坑：原实现是 "…业务侧出现异常（%s）" % fault.title，
        # 而 title 就是场景名（例如"单副本网络延迟注入（tc netem 300ms）"）——
        # 等于把答案写进了输入。实测：app_network_delay / app_network_loss
        # 两个场景 alert_coverage=False（没有告警文本），却都判对了 rc:net-fault，
        # 靠的正是标题里的"网络延迟/丢包"。那样的"命中"没有意义。
        # 现在只给层级这种非标识性信息，命不命中都如实反映
        # "没有告警时 RCA 几乎是空手诊断"这一事实。
        return "监控无对应告警，但业务侧出现异常（层级=%s）" % (
            LAYER_LABEL.get(getattr(fault, "layer", ""), getattr(fault, "layer", "")) or "未知")

    hints: List[str] = []
    for a in chosen:
        h = a.get("hint")
        if h and h not in hints:
            hints.append(h)
    if hints:
        return "；".join(hints[:4])
    # 声明告警在 firing，但规则没写 rca_hint → 退回 summary
    sums = [a.get("summary") for a in chosen if a.get("summary")]
    if sums:
        return "；".join(sums[:4])
    return "监控无对应告警，但业务侧出现异常（%s）" % getattr(fault, "title", "")


# ═════════════════════════════════════════════════════════════════════════════
# 智能体调用与评分
# ═════════════════════════════════════════════════════════════════════════════

def call_agent(alert: str, mode: str, severity: str, timeout: float = 300.0) -> Dict[str, Any]:
    payload = {"alert": alert, "severity": severity, "persist": True,
               "app_name": "payment-app"}
    if mode in ("deterministic", "agentic"):
        payload["mode"] = mode
    st, body = http_json(RCA_API + "/api/agent/diagnose", method="POST",
                         payload=payload, timeout=timeout)
    return {"status": st, "body": body}


def _agent_text_blob(body: Any) -> str:
    """把智能体输出压成一段可检索文本（根因 + 候选 + 证据 + 推理摘要 + 工具观察）。"""
    if not isinstance(body, dict):
        return str(body)
    parts: List[str] = []
    rc = body.get("root_cause") or {}
    if isinstance(rc, dict):
        parts += [str(rc.get("entity_id", "")), str(rc.get("entity_name", "")),
                  str(rc.get("category", "")), str(rc.get("reason", ""))]
    seed = body.get("seed") or {}
    for c in (seed.get("candidates") or []):
        if isinstance(c, dict):
            parts += [str(c.get("entity_id", "")), str(c.get("entity_name", "")),
                      str(c.get("reason", ""))]
    parts.append(str(body.get("reasoning_summary", "")))
    for e in (body.get("evidence") or []):
        if isinstance(e, dict):
            parts += [str(e.get("source", "")), str(e.get("finding", ""))]
    for s in (body.get("steps") or []):
        if isinstance(s, dict):
            parts.append(str(s.get("observation", ""))[:2000])
            parts.append(str(s.get("reasoning", ""))[:2000])
    for a in (body.get("next_actions") or []):
        if isinstance(a, dict):
            parts.append(str(a.get("action", "")))
    return " ".join(parts).lower()


def _term_names(workspace: Path) -> Dict[str, str]:
    """读取当前激活版本，拿到 Term id → name 映射（用于文本级匹配）。"""
    try:
        active = json.loads((workspace / "active.json").read_text(encoding="utf-8"))
        version = active.get("active_version") or active.get("version")
        terms = json.loads((workspace / "versions" / version / "terms.json")
                           .read_text(encoding="utf-8"))
        return {t["id"]: str(t.get("name", "")) for t in terms}
    except Exception:  # noqa: BLE001
        return {}


def score_diagnosis(body: Any, expected_terms: List[str],
                    keywords: List[str], names: Dict[str, str]) -> Dict[str, Any]:
    """
    给一次诊断打分。

    命中判定（任一成立即 ok）：
      A. 最终根因实体 id ∈ 期望 Term
      B. 期望 Term 出现在候选/证据/推理摘要文本里（id 或名称）
      C. 场景语义关键词出现在同一段文本里（文本级回退）
    同时判定"类别是否对齐"，作为部分信用（partial）。
    """
    if not isinstance(body, dict):
        return {"ok": False, "reason": "智能体返回非结构化结果", "partial": False}

    blob = _agent_text_blob(body)
    rc = body.get("root_cause") or {}
    rc_id = str(rc.get("entity_id") or "") if isinstance(rc, dict) else ""
    rc_name = str(rc.get("entity_name") or "") if isinstance(rc, dict) else ""
    rc_cat = str(rc.get("category") or "") if isinstance(rc, dict) else ""

    hit_a = rc_id in expected_terms if rc_id else False
    hit_b_terms = []
    for t in expected_terms:
        nm = names.get(t, "")
        if (t and t.lower() in blob) or (nm and nm.lower() in blob):
            hit_b_terms.append(t)
    hit_b = bool(hit_b_terms)
    hit_kw = [k for k in keywords if k.lower() in blob]
    hit_c = bool(hit_kw)

    ok = bool(hit_a or hit_b or hit_c)
    how = ("root_cause 精确命中" if hit_a else
           "期望术语出现在候选/证据/推理中(%s)" % ",".join(hit_b_terms[:3]) if hit_b else
           "语义关键词命中(%s)" % ",".join(hit_kw[:3]) if hit_c else "未命中")

    expected_cats = set()
    for t in expected_terms:
        if t.startswith("rc:") or t.startswith("metric:") or t.startswith("con:"):
            pass
    fault_cat_hint = None
    partial = ok or (rc_cat and rc_cat not in ("", "未知"))
    return {
        "ok": ok, "how": how, "partial": bool(partial),
        "root_cause_id": rc_id, "root_cause_name": rc_name, "root_cause_category": rc_cat,
        "root_cause_confidence": (rc.get("confidence") if isinstance(rc, dict) else None),
        "hit_root_cause_exact": hit_a,
        "hit_terms_in_text": hit_b_terms,
        "hit_keywords": hit_kw[:6],
        "expected_terms": expected_terms,
        "route_mode": (body.get("route") or {}).get("mode"),
        "route_reason": (body.get("route") or {}).get("reason"),
        "mode": body.get("mode"),
        "status": body.get("status"),
        "model": body.get("model"),
        "steps_used": body.get("steps_used"),
        "tools_used": body.get("tools_used"),
        "total_tokens": body.get("total_tokens"),
        "latency_ms": body.get("total_latency_ms"),
        "reasoning_summary": (body.get("reasoning_summary") or "")[:600],
    }


# ═════════════════════════════════════════════════════════════════════════════
# 观测快照（证明"故障真的发生了"）
# ═════════════════════════════════════════════════════════════════════════════

def observability_snapshot() -> Dict[str, Any]:
    snap: Dict[str, Any] = {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def val(expr: str) -> Any:
        r = prom_query(expr)
        if not r:
            return None
        try:
            return float(r[0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    snap["healthy_app_replicas"] = val('count(up{job="payment-app"} == 1)')
    snap["app_replicas_total"] = val('count(up{job="payment-app"})')
    snap["gateway_probe"] = val('probe_success{job="blackbox-gateway"}')
    snap["replica_probe_failures"] = val('count(probe_success{job="blackbox-app-replica"} == 0)')
    snap["p99_seconds"] = val(
        'max(histogram_quantile(0.99, sum(rate(cc_http_request_duration_seconds_bucket[2m])) by (le)))')
    snap["http_5xx_rate"] = val(
        'sum(rate(cc_http_requests_total{status=~"5.."}[2m]))')
    snap["pool_active_max"] = val('max(cc_mysql_pool_active)')
    snap["pool_waiting_max"] = val('max(cc_mysql_pool_waiting)')
    snap["cpu_throttled_rate"] = val('max(rate(cc_container_cpu_nr_throttled_total[1m]))')
    snap["container_mem_ratio"] = val(
        'max(cc_container_memory_working_set_bytes / (cc_container_memory_limit_bytes > 0))')
    snap["mysql_threads_connected"] = val('max(mysql_global_status_threads_connected)')
    snap["mysql_threads_running"] = val('max(mysql_global_status_threads_running)')
    snap["mysql_slow_queries_rate"] = val('max(rate(mysql_global_status_slow_queries[2m]))')
    snap["mysql_row_lock_waits"] = val('max(mysql_global_status_innodb_row_lock_current_waits)')
    snap["replica_lag_max"] = val('max(mysql_slave_status_seconds_behind_master)')
    snap["replica_io_down"] = val('count(mysql_slave_status_slave_io_running == 0)')
    snap["tmp_disk_rate"] = val('max(rate(mysql_global_status_created_tmp_disk_tables[2m]))')
    snap["mysql_nodes_up"] = val('count(up{job=~"mysql-.*"} == 1)')
    snap["containers"] = [
        {k: v for k, v in container_state(r["name"]).items()
         if k in ("name", "status", "oom_killed", "restart_count", "memory_limit_mb")}
        for r in app_replicas()
    ]
    return snap


# ═════════════════════════════════════════════════════════════════════════════
# 单场景验证
# ═════════════════════════════════════════════════════════════════════════════

def verify_one(fid: str, eng: Injector, args: argparse.Namespace,
               names: Dict[str, str]) -> Dict[str, Any]:
    fault = FAULTS[fid]
    expected = SCENARIO_EXPECTED_TERMS.get(fid) or []
    keywords = SCENARIO_MATCH_KEYWORDS.get(fid, [])

    res: Dict[str, Any] = {
        "scenario": fid, "title": fault.title, "layer": fault.layer,
        "layer_label": LAYER_LABEL.get(fault.layer, fault.layer),
        "category": fault.category,
        "expected_root_cause": fault.expected_root_cause,
        "expected_terms": expected,
        "alert_names_expected": fault.alert_names,
        "needs_stress": fault.needs_stress,
        "ok": False,
    }
    t0 = time.time()

    # ── 0. 注入**前**的告警快照（用于识别"本次故障新引发的告警"）────────
    # ⚠ 必须有这一步。Prometheus 的告警在故障恢复后仍会 firing 数分钟
    # （`for` 窗口 + 恢复判定延迟），连续跑 21 个场景时，
    # 后面的场景会重复拿到前面场景的 rca_hint → 诊断结果雷同。
    # 实测（未加本步骤时）：gateway_stop 被判成 rc:cpu-throttle、
    # kill_replica 被判成 rc:replica-lag、memory_stress 被判成 rc:slow-sql ——
    # 全是"上一个场景残留的 CPU 节流/慢查询告警"造成的，属**测试输入污染**。
    pre = collect_alerts()
    pre_names = {a.get("name") for a in (pre.get("alerts") or [])}
    res["alerts_pre"] = sorted(n for n in pre_names if n)

    # ── 1. 注入（signal_needs_stress 的场景先不等待）────────────────
    inj = eng.inject(fid, wait_signals=not fault.signal_needs_stress,
                     signal_timeout=args.signal_timeout)
    res["inject"] = {
        "ok": inj.get("ok"), "signal_observed": inj.get("signal_observed"),
        "detail": (inj.get("inject_result") or {}).get("detail"),
        "targets": inj.get("targets"),
        "warning": inj.get("warning"),
    }
    if not inj.get("ok"):
        res["error"] = inj.get("error")
        eng.recover(fid)
        res["elapsed_s"] = round(time.time() - t0, 1)
        return res

    # ── 2. 后台压测放大 ───────────────────────────────────────────
    stress = None
    if args.stress:
        stress = start_stress_background(args.stress.split())

    # ── 3. 若注入阶段未观测到信号，在压测窗口内继续等 ──────────────
    if not inj.get("signal_observed") and fault.signals:
        checks = []
        deadline = time.time() + args.signal_timeout
        any_ok = False
        for expr in fault.signals:
            remain = max(5.0, deadline - time.time())
            r = prom_wait(expr, timeout_s=remain, interval_s=5.0, expect="truthy")
            checks.append(r)
            if r.get("ok"):
                any_ok = True
                break
        res["inject"]["signal_checks_under_load"] = checks
        res["inject"]["signal_observed"] = any_ok

    # ── 4. 保持故障，等告警规则评估 ────────────────────────────────
    hold = args.hold
    if fault.layer == "database" or fault.needs_stress:
        hold = max(hold, args.db_hold)
    eng.log("[verify] 保持 %.0fs 等待告警评估 ..." % hold)
    time.sleep(hold)

    res["observability"] = observability_snapshot()
    alerts = collect_alerts()
    res["alerts_fired"] = alerts["alerts"]
    # 本次**新**出现的告警 vs 残留告警，分开记录（便于发现"输入被污染"）
    res["alerts_new"] = sorted(a.get("name") for a in (alerts.get("alerts") or [])
                               if a.get("name") not in pre_names)
    res["alerts_residual"] = sorted(a.get("name") for a in (alerts.get("alerts") or [])
                                    if a.get("name") in pre_names)
    res["alert_coverage"] = bool(
        {a.get("name") for a in (alerts.get("alerts") or [])} & set(fault.alert_names or []))

    alert_text = build_alert_text(alerts, fault, pre_names)
    res["alert_text"] = alert_text
    # severity 只从**该场景相关**的告警里取（拿残留告警的级别会串味）
    _declared = set(fault.alert_names or [])
    _chosen = [a for a in (alerts.get("alerts") or []) if a.get("name") in _declared] \
        if _declared else []
    res["severity"] = _chosen[0]["severity"] if _chosen else "P1"

    # ── 5. 智能体诊断（可跑 deterministic / agentic / both）────────
    modes = ["deterministic", "agentic"] if args.mode == "both" else [args.mode]
    runs: List[Dict[str, Any]] = []
    for m in modes:
        t1 = time.time()
        call = call_agent(alert_text, m, res["severity"], timeout=args.agent_timeout)
        sc = score_diagnosis(call.get("body"), expected, keywords, names)
        sc["requested_mode"] = m
        sc["http_status"] = call.get("status")
        sc["wall_s"] = round(time.time() - t1, 1)
        runs.append(sc)
        eng.log("[verify]   mode=%-14s ok=%s  root=%s  (%ss)"
                % (m, sc.get("ok"), sc.get("root_cause_id") or "-", sc["wall_s"]))
    res["diagnosis_runs"] = runs
    res["ok"] = any(r.get("ok") for r in runs)

    # ── 6. 停压测 + 回滚 + 恢复校验 ────────────────────────────────
    if stress:
        res["stress"] = stop_stress_background(stress, grace_s=args.stress_grace)
    rec = eng.recover(fid, targets=inj.get("targets"))
    res["recover"] = {"ok": rec.get("ok"), "results": rec.get("results")}

    rec_checks = []
    deadline = time.time() + args.recovery_timeout
    for expr in fault.recovered_signals:
        remain = max(5.0, deadline - time.time())
        rec_checks.append(prom_wait(expr, timeout_s=remain, interval_s=5.0, expect="falsy"))
    res["recovery_checks"] = rec_checks
    res["recovered"] = all(c.get("ok") for c in rec_checks) if rec_checks else True
    res["elapsed_s"] = round(time.time() - t0, 1)
    return res


def probe_only(args: argparse.Namespace, names: Dict[str, str]) -> int:
    """
    只做"本体检索能力"探针：不注入任何故障，直接把每个场景的**症状描述**喂给智能体，
    记录确定性快路径给出的根因与候选集。

    用途：本体迭代的 A/B 对照 —— 同一批告警文本，在父版本与候选版本下分别探针，
    可以直观看到"集群根因术语是否进入了候选集"。因为不涉及真实故障，
    这里的指标衡量的是**本体的可检索性/知识覆盖**，而不是端到端诊断准确率。
    """
    from tools.chaos.core import http_json as _http

    ids = args.scenarios or sorted(FAULTS)
    out: List[Dict[str, Any]] = []
    print("=" * 92)
    print("本体检索探针（不注入故障）：%d 个场景" % len(ids))
    print("=" * 92)
    for idx, fid in enumerate(ids):
        fault = FAULTS[fid]
        alert = "%s：%s" % (fault.title, fault.expected_root_cause)
        # 后端有速率限制（默认 10 次/分钟）。探针是连续快调用，
        # 不主动节流会被 429 打成"假未命中"——必须按节奏来。
        if idx > 0 and args.pace > 0:
            time.sleep(args.pace)
        st, body = _http(RCA_API + "/api/agent/diagnose", "POST",
                         {"alert": alert, "severity": "P1",
                          "mode": args.mode if args.mode in ("deterministic", "agentic") else None,
                          "persist": False}, timeout=args.agent_timeout)
        expected = SCENARIO_EXPECTED_TERMS.get(fid) or []
        rec: Dict[str, Any] = {"scenario": fid, "layer": fault.layer,
                               "alert": alert, "expected_terms": expected,
                               "http_status": st}
        if isinstance(body, dict):
            rc = body.get("root_cause") or {}
            cands = [c.get("entity_id") for c in ((body.get("seed") or {}).get("candidates") or [])]
            rec.update({
                "root_cause_id": rc.get("entity_id"),
                "root_cause_category": rc.get("category"),
                "root_cause_confidence": rc.get("confidence"),
                "candidates": cands,
                "route_mode": (body.get("route") or {}).get("mode"),
                "hit_top1": rc.get("entity_id") in expected if rc.get("entity_id") else False,
                "hit_topk": any(c in expected for c in cands[:args.top_k]),
            })
        else:
            rec["error"] = str(body)[:300]
        out.append(rec)
        print("  %-22s top1=%-24s topk_hit=%-5s candidates=%s"
              % (fid, rec.get("root_cause_id"), rec.get("hit_topk"),
                 ",".join((rec.get("candidates") or [])[:5])))
    hit1 = sum(1 for r in out if r.get("hit_top1"))
    hitk = sum(1 for r in out if r.get("hit_topk"))
    summary = {"total": len(out), "top1_hits": hit1, "topk_hits": hitk,
               "top_k": args.top_k,
               "top1_rate": round(hit1 / len(out), 4) if out else 0.0,
               "topk_rate": round(hitk / len(out), 4) if out else 0.0,
               "mode": args.mode}
    path = Path(args.json_out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"summary": summary, "results": out},
                               ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("-" * 92)
    print("  top1 命中 %d/%d   top%d 命中 %d/%d"
          % (hit1, len(out), args.top_k, hitk, len(out)))
    print("  报告: %s" % path)
    return 0


# ═════════════════════════════════════════════════════════════════════════════
# main
# ═════════════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="端到端验证：故障注入 → 压测 → 告警 → 智能体诊断 → 评分",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("scenarios", nargs="*", help="场景 id（留空=全部）")
    p.add_argument("--mode", choices=["deterministic", "agentic", "both", "auto"],
                   default="deterministic",
                   help="auto=不强制，让编排器按路由判定（本体优先，必要时用 LLM）")
    p.add_argument("--hold", type=float, default=None,
                   help="故障保持秒数（等告警评估）；默认 = 告警规则最大 for + 30s")
    p.add_argument("--db-hold", type=float, default=None, dest="db_hold",
                   help="数据库层/需压测场景的最小保持秒数；默认同 --hold")
    p.add_argument("--stress", default="--duration 150 --concurrency 24",
                   help="压测参数串；空串关闭压测")
    p.add_argument("--stress-grace", type=float, default=20.0, dest="stress_grace")
    p.add_argument("--signal-timeout", type=float, default=60.0, dest="signal_timeout")
    p.add_argument("--recovery-timeout", type=float, default=95.0, dest="recovery_timeout")
    p.add_argument("--agent-timeout", type=float, default=300.0, dest="agent_timeout")
    p.add_argument("--cooldown", type=float, default=10.0)
    p.add_argument("--json-out", default=str(CHAOS_DIR / "rca_diagnosis_report.json"))
    p.add_argument("--probe-only", action="store_true", dest="probe_only",
                   help="不注入故障，只做本体检索探针（本体迭代 A/B 对照用）")
    p.add_argument("--top-k", type=int, default=5, dest="top_k")
    p.add_argument("--pace", type=float, default=7.0,
                   help="探针模式下每次诊断的间隔秒数（后端限流 10 次/分钟）")
    p.add_argument("--settle", type=float, default=300.0,
                   help="开跑前等待告警消退的最长秒数（避免上一轮残留告警污染输入；默认 300s）")
    p.add_argument("--no-settle", action="store_true", dest="no_settle",
                   help="跳过开跑前等待（仅在确认环境已无告警时使用）")
    p.add_argument("--quiet", action="store_true")
    return p


def summarize(results: List[Dict[str, Any]], mode: str) -> Dict[str, Any]:
    by_layer: Dict[str, Dict[str, int]] = {}
    for r in results:
        L = r.get("layer_label", r.get("layer", "?"))
        d = by_layer.setdefault(L, {"total": 0, "diagnosed": 0, "injected": 0,
                                    "recovered": 0, "alert_covered": 0})
        d["total"] += 1
        if r.get("inject", {}).get("ok"):
            d["injected"] += 1
        if r.get("ok"):
            d["diagnosed"] += 1
        if r.get("recovered"):
            d["recovered"] += 1
        if r.get("alert_coverage"):
            d["alert_covered"] += 1

    # 实际走到的诊断模式（auto 模式下可能混有 deterministic / agentic）
    modes_used: Dict[str, int] = {}
    for r in results:
        for run in r.get("diagnosis_runs") or []:
            k = str(run.get("mode") or run.get("requested_mode"))
            modes_used[k] = modes_used.get(k, 0) + 1

    total = len(results)
    # ── 三态评分：不可测量 ≠ 未命中 ──────────────────────────────────────
    # 依据（实测）：当场景声明的告警一条都没触发时，harness 发给引擎的是占位文本
    # （"监控无对应告警，但业务侧出现异常"）。那样的回答**既不该算命中**
    # （它只是从空输入里猜出来的），**也不该把"必然失分"算进分母**。
    # 因此把它单列出来，准确率只在**可测量**场景上计算。
    not_meas = [r.get("scenario") for r in results if r.get("alert_coverage") is False]
    measurable = total - len(not_meas)
    diag = sum(1 for r in results if r.get("ok"))
    diag_meas = sum(1 for r in results if r.get("ok") and r.get("alert_coverage") is not False)
    inj = sum(1 for r in results if r.get("inject", {}).get("ok"))
    rec = sum(1 for r in results if r.get("recovered"))
    alert_cov = sum(1 for r in results if r.get("alert_coverage"))
    lat = [run["latency_ms"] for r in results for run in (r.get("diagnosis_runs") or [])
           if isinstance(run.get("latency_ms"), (int, float))]
    tokens = [run["total_tokens"] for r in results for run in (r.get("diagnosis_runs") or [])
              if isinstance(run.get("total_tokens"), (int, float))]
    return {
        "total_scenarios": total,
        "not_measurable": len(not_meas),
        "not_measurable_scenarios": not_meas,
        "measurable_scenarios": measurable,
        "injected_ok": inj,
        "diagnosed_ok": diag_meas,
        "diagnosed_ok_all": diag,
        "diagnosis_accuracy": round(diag_meas / measurable, 4) if measurable else 0.0,
        "alert_coverage": round(alert_cov / total, 4) if total else 0.0,
        "recovered_ok": rec,
        "recovery_rate": round(rec / total, 4) if total else 0.0,
        "by_layer": by_layer,
        "diagnosis_modes": modes_used,
        "mean_diagnosis_latency_ms": round(sum(lat) / len(lat), 1) if lat else None,
        "mean_tokens": round(sum(tokens) / len(tokens), 1) if tokens else None,
    }


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    ids = args.scenarios or sorted(FAULTS)
    unknown = [s for s in ids if s not in FAULTS]
    if unknown:
        print("未知场景: %s" % ", ".join(unknown), file=sys.stderr)
        return 2

    leftover = active_faults()
    if leftover:
        print("[verify] 清理残留故障: %s" % [a.get("fault_id") for a in leftover])
        Injector(FAULTS, verbose=False).recover_all()

    # ── 开跑前等告警清空 ────────────────────────────────────────────────
    # 为什么必须等：Prometheus 告警在故障恢复后仍会 firing 数分钟，
    # 若上一轮（或上一次实验）的告警还没消退就开跑，
    # 场景 1 的"注入前快照"里就带着它们，后面的判定全被污染。
    # 实测过一次：未等待时 app_cpu_stress 的新告警是 MySQLReplicationLag
    # （上一轮复制延迟残留），于是被喂了复制延迟的 hint、诊断成 rc:replica-lag。
    # 有界等待（默认最多 300s），等不到就**明确告警**而不是假装干净。
    if not args.probe_only and not args.no_settle:
        quiet_deadline = time.time() + args.settle
        while time.time() < quiet_deadline:
            cur = collect_alerts().get("alerts") or []
            if not cur:
                break
            names_now = sorted({a.get("name") for a in cur})
            print("[verify] 等待告警消退（仍在 firing: %s），最多再等 %.0fs ..."
                  % (", ".join(names_now[:4]), quiet_deadline - time.time()))
            time.sleep(20)
        still = collect_alerts().get("alerts") or []
        if still:
            print("[verify] ⚠ 仍有 %d 条告警在 firing（%s）—— 本次结果可能受残留告警影响"
                  % (len(still), ", ".join(sorted({a.get("name") for a in still})[:4])))
        else:
            print("[verify] 告警已清空，开始验证")

    eng = Injector(FAULTS, verbose=not args.quiet)
    names = _term_names(ROOT / ".evoontology")

    if args.probe_only:
        return probe_only(args, names)

    # ── 保持时间：默认按告警规则的 for 窗口推导（而不是硬编码 55/70）────────
    # 见 max_alert_for_seconds 的注释：wait 不够就拿不到告警文本，
    # 端到端会退化成"无输入硬猜"。这里推导一次并打印依据，避免下次又踩。
    if args.hold is None or args.db_hold is None:
        mx_for = max_alert_for_seconds()
        # + 2×评估间隔（规则组 interval=30s）：Prometheus 每 30s 评估一次，
        # "条件持续 for" 最早也要 for+30s 才 firing，只留 30s 会卡在边界上
        # （实测：hold=210s 时 for=180s 的规则仍等不到）。
        derived = mx_for + 60.0
        if args.hold is None:
            args.hold = derived
        if args.db_hold is None:
            args.db_hold = derived
        print("[verify] 告警规则最大 for=%.0fs → 保持时间取 %.0fs（for + 2×30s 裕量）"
              % (mx_for, derived))
        if derived < 60:
            print("[verify] ⚠ 推导值偏小，请确认 Prometheus 规则是否已加载")

    print("=" * 96)
    print("端到端诊断能力验证：%d 个场景  mode=%s  hold=%.0fs  压测=%s"
          % (len(ids), args.mode, args.hold, args.stress))
    print("=" * 96)

    results: List[Dict[str, Any]] = []
    out_path = Path(args.json_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def flush(complete: bool) -> None:
        """增量落盘：任何时刻中断都能拿到已完成场景的结果。"""
        rep = {"mode": args.mode, "hold": args.hold, "stress": args.stress,
               "complete": complete, "requested": ids,
               "summary": summarize(results, args.mode), "results": results}
        out_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")

    for i, fid in enumerate(ids, 1):
        print("\n" + "#" * 96)
        print("# [%d/%d] %s  %s" % (i, len(ids), fid, FAULTS[fid].title))
        print("#" * 96)
        try:
            r = verify_one(fid, eng, args, names)
        except KeyboardInterrupt:
            print("\n[verify] 中断，正在回滚 ...")
            eng.recover_all()
            flush(False)
            break
        except Exception as e:  # noqa: BLE001
            print("[verify] 场景异常: %s: %s" % (type(e).__name__, e))
            eng.recover_all()
            r = {"scenario": fid, "title": FAULTS[fid].title,
                 "layer": FAULTS[fid].layer, "ok": False,
                 "error": "%s: %s" % (type(e).__name__, e)}
        results.append(r)
        flush(False)
        print("[verify] 结论: 诊断命中=%s  告警覆盖=%s  已回滚=%s  用时=%.0fs"
              % (r.get("ok"), r.get("alert_coverage"), r.get("recovered"),
                 r.get("elapsed_s") or 0))
        if i < len(ids):
            time.sleep(max(0.0, args.cooldown))

    eng.recover_all()
    summary = summarize(results, args.mode)
    report = {"mode": args.mode, "hold": args.hold, "stress": args.stress,
              "complete": len(results) == len(ids), "requested": ids,
              "summary": summary, "results": results}

    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")

    print("\n" + "=" * 96)
    print("诊断能力验证汇总")
    print("=" * 96)
    print("  场景总数            %d" % summary["total_scenarios"])
    print("  故障注入成功        %d/%d" % (summary["injected_ok"], summary["total_scenarios"]))
    print("  告警覆盖（有告警文本）%d/%d  (%.0f%%)"
          % (int(summary["alert_coverage"] * summary["total_scenarios"]),
             summary["total_scenarios"], summary["alert_coverage"] * 100))
    print("  诊断命中            %d/%d  (%.0f%%)"
          % (summary["diagnosed_ok"], summary["measurable_scenarios"],
             summary["diagnosis_accuracy"] * 100))
    print("  故障恢复成功        %d/%d  (%.0f%%)"
          % (summary["recovered_ok"], summary["total_scenarios"],
             summary["recovery_rate"] * 100))
    print("  平均诊断耗时        %s ms" % summary["mean_diagnosis_latency_ms"])
    print("  平均 token          %s" % summary["mean_tokens"])
    print("  分层次:")
    for layer, d in summary["by_layer"].items():
        print("    %-10s 诊断 %d/%d   回滚 %d/%d   告警覆盖 %d/%d"
              % (layer, d["diagnosed"], d["total"], d["recovered"], d["total"],
                 d["alert_covered"], d["total"]))
    print("-" * 96)
    print("  未命中场景:")
    for r in results:
        if not r.get("ok"):
            runs = r.get("diagnosis_runs") or [{}]
            print("    %-22s root=%s  how=%s  alert_coverage=%s"
                  % (r.get("scenario"), runs[0].get("root_cause_id"),
                     runs[0].get("how"), r.get("alert_coverage")))
    print("=" * 96)
    print("报告: %s" % out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
