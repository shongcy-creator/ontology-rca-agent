# -*- coding: utf-8 -*-
"""
监控层工具 — Prometheus 指标查询。

提供即时查询、区间查询、告警状态、抓取目标、关键指标快照。
工具描述中内置本系统真实存在的指标名，避免模型臆造不存在的指标。
"""
from __future__ import annotations
from typing import Any, Dict, List

from .base import ToolResult, ToolSpec


# 本系统真实指标清单（写进工具描述，抑制幻觉）
METRIC_CATALOG = """
可用指标（必须使用这些真实名称）：
应用指标 (job=payment-app)：
  cc_http_requests_total{method,path,status}                HTTP 请求计数
  cc_http_request_duration_seconds_bucket{le,...}           HTTP 延迟直方图
  cc_http_request_duration_seconds_count                    HTTP 请求总数
  cc_mysql_pool_active / cc_mysql_pool_idle / cc_mysql_pool_limit   连接池
  cc_mysql_query_duration_seconds_bucket{le,...}            SQL 延迟直方图
  cc_txn_total{status}                                      交易计数
  cc_container_cpu_seconds / cc_container_memory_bytes      进程资源
MySQL 指标 (job=mysql-exporter)：
  mysql_up                                                  MySQL 存活
  mysql_global_status_threads_connected                     当前连接数
  mysql_global_status_max_used_connections                  历史最大连接数
  mysql_global_status_aborted_connects                      失败连接数
  mysql_global_status_slow_queries                          慢查询计数
  mysql_global_status_uptime                                运行时长
  mysql_global_variables_max_connections                    max_connections
  mysql_global_status_innodb_row_lock_waits                 InnoDB 行锁等待
  mysql_global_status_innodb_row_lock_time_avg              平均锁等待时间(ms)
  mysql_global_status_innodb_deadlocks                      死锁数
  mysql_global_status_queries                               SQL 执行总数
  mysql_global_status_com_select/insert/update/delete       各类语句计数
系统指标：
  up{job}                                                   抓取目标存活
""".strip()


# ── Handlers ──────────────────────────────────────────────────────────────

def _client():
    from ..prometheus import PrometheusClient
    return PrometheusClient()


def _metrics_query(args: Dict[str, Any]) -> ToolResult:
    expr = (args.get("query") or "").strip()
    if not expr:
        return ToolResult.fail("query 不能为空")
    data = _client().query(expr)
    result = data.get("result", [])
    if not result:
        return ToolResult(
            ok=True,
            summary="查询无数据（可能是指标名不存在、时间窗口内无样本，或标签不匹配）。表达式: %s" % expr,
            data=[],
        )
    simplified = []
    for r in result[:20]:
        m = r.get("metric") or {}
        label = ",".join("%s=%s" % (k, v) for k, v in m.items() if not k.startswith("__"))
        simplified.append({"labels": label, "value": r.get("value", [None, None])[1]})
    return ToolResult(
        ok=True,
        summary="返回 %d 条序列" % len(result),
        data=simplified,
    )


def _metrics_range(args: Dict[str, Any]) -> ToolResult:
    import time
    expr = (args.get("query") or "").strip()
    if not expr:
        return ToolResult.fail("query 不能为空")
    minutes = int(args.get("minutes", 30))
    end = int(time.time())
    start = end - minutes * 60
    step = args.get("step") or "%ds" % max(15, minutes * 60 // 60)

    data = _client().query_range(expr, start, end, step)
    result = data.get("result", [])
    if not result:
        return ToolResult(ok=True, summary="区间内无数据", data=[])

    out: List[dict] = []
    for series in result[:10]:
        m = series.get("metric") or {}
        label = ",".join("%s=%s" % (k, v) for k, v in m.items() if not k.startswith("__"))
        vals = series.get("values") or []
        try:
            nums = [float(v[1]) for v in vals if v[1] not in ("NaN", "+Inf", "-Inf")]
        except (ValueError, IndexError):
            nums = []
        stat = {}
        if nums:
            stat = {
                "first": round(nums[0], 4),
                "last": round(nums[-1], 4),
                "min": round(min(nums), 4),
                "max": round(max(nums), 4),
                "avg": round(sum(nums) / len(nums), 4),
                "trend": ("上升" if nums[-1] > nums[0] * 1.1 else
                          "下降" if nums[-1] < nums[0] * 0.9 else "平稳"),
            }
        out.append({"labels": label, "points": len(nums), **stat})
    return ToolResult(
        ok=True,
        summary="区间 %d 分钟，%d 条序列趋势已汇总" % (minutes, len(result)),
        data=out,
    )


def _metrics_alerts(args: Dict[str, Any]) -> ToolResult:
    rules = _client().rules()
    groups = (rules.get("data") or {}).get("groups") or []
    firing, pending = [], []
    for g in groups:
        for r in (g.get("rules") or []):
            if r.get("type") != "alerting":
                continue
            state = r.get("state")
            if state not in ("firing", "pending"):
                continue
            entry = {
                "name": r.get("name"),
                "state": state,
                "labels": r.get("labels") or {},
                "annotations": r.get("annotations") or {},
                "value": None,
            }
            alerts = r.get("alerts") or []
            if alerts:
                entry["value"] = alerts[0].get("value")
                entry["activeAt"] = alerts[0].get("activeAt")
            (firing if state == "firing" else pending).append(entry)

    return ToolResult(
        ok=True,
        summary="当前告警：firing=%d, pending=%d" % (len(firing), len(pending)),
        data={"firing": firing, "pending": pending},
    )


def _metrics_targets(args: Dict[str, Any]) -> ToolResult:
    targets = _client().targets()
    out = []
    down = []
    for t in targets:
        lbl = t.get("labels") or {}
        entry = {
            "job": lbl.get("job"),
            "instance": lbl.get("instance"),
            "health": t.get("health"),
            "lastError": t.get("lastError") or "",
            "lastScrape": t.get("lastScrape"),
        }
        out.append(entry)
        if entry["health"] != "up":
            down.append(entry)
    return ToolResult(
        ok=True,
        summary="抓取目标 %d 个，异常 %d 个%s" % (
            len(out), len(down),
            ("：" + "; ".join("%s(%s)" % (d["job"], d["lastError"][:40]) for d in down)) if down else ""),
        data={"targets": out, "down": down},
    )


def _metrics_summary(args: Dict[str, Any]) -> ToolResult:
    m = _client().all_metrics()
    pool = m.get("mysql_pool") or {}
    mg = m.get("mysql_global") or {}
    buckets = m.get("http_latency_buckets") or {}
    total = float(m.get("http_request_total") or 0)
    over = max(0.0, total - float(buckets.get("0.5") or 0))
    ratio = (over / total * 100) if total else 0.0
    return ToolResult(
        ok=True,
        summary=("连接池 %s/%s；MySQL 连接 %s/%s，慢查询 %s；"
                 "HTTP 总请求 %.0f，延迟>500ms 占比 %.1f%%") % (
            pool.get("active"), pool.get("limit"),
            mg.get("threads_connected"), m.get("mysql_max_connections"),
            mg.get("slow_queries", "N/A"),
            total, ratio),
        data=m,
    )


# ── 规格定义 ──────────────────────────────────────────────────────────────

METRICS_TOOLS = [
    ToolSpec(
        name="metrics_query",
        layer="metrics",
        description=(
            "执行 PromQL 即时查询。\n\n" + METRIC_CATALOG +
            "\n\n注意：直方图分位数用 histogram_quantile(0.99, sum by (le) (rate(..._bucket[5m])))。"
        ),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "PromQL 表达式"}},
            "required": ["query"],
        },
        handler=_metrics_query,
        timeout_s=30,
        injection_exempt=["query"],   # PromQL 中 | 等字符合法
    ),
    ToolSpec(
        name="metrics_range",
        layer="metrics",
        description=(
            "执行 PromQL 区间查询并返回趋势统计（首值/末值/最小/最大/均值/趋势方向）。"
            "用于回答'什么时候开始变慢''是突增还是渐进'这类时序问题。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "PromQL 表达式"},
                "minutes": {"type": "integer", "description": "回溯分钟数", "default": 30},
                "step": {"type": "string", "description": "采样步长，如 30s", "default": ""},
            },
            "required": ["query"],
        },
        handler=_metrics_range,
        timeout_s=40,
        injection_exempt=["query", "step"],
    ),
    ToolSpec(
        name="metrics_alerts",
        layer="metrics",
        description=(
            "获取当前 Prometheus 告警规则的 firing/pending 状态，"
            "含 severity、layer、onto_constraint 标签与 rca_hint 注解。"
            "排查的起点通常是这里。"
        ),
        parameters={"type": "object", "properties": {}},
        handler=_metrics_alerts,
        timeout_s=30,
    ),
    ToolSpec(
        name="metrics_targets",
        layer="metrics",
        description="检查所有 Prometheus 抓取目标的健康状态。用于判断'是采集问题还是真实故障'。",
        parameters={"type": "object", "properties": {}},
        handler=_metrics_targets,
        timeout_s=30,
    ),
    ToolSpec(
        name="metrics_summary",
        layer="metrics",
        description=(
            "一次性获取关键指标快照：连接池占用、MySQL 连接与慢查询、"
            "HTTP 请求总量与超阈值占比、交易计数、抓取目标。"
            "作为排查起点的低成本全景观测。"
        ),
        parameters={"type": "object", "properties": {}},
        handler=_metrics_summary,
        timeout_s=40,
    ),
]
