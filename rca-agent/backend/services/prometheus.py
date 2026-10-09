# -*- coding: utf-8 -*-
"""Prometheus 查询客户端."""
from __future__ import annotations
import os, urllib.request, urllib.parse, json
from typing import Optional, List, Dict, Any


def _resolve_url(base: Optional[str]) -> str:
    if base:
        return base.rstrip("/")
    env_url = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
    return env_url.rstrip("/")


class PrometheusClient:
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = _resolve_url(base_url)
        self._local   = "http://localhost:9090"  # 本地开发 fallback

    def _get(self, path: str) -> Dict[str, Any]:
        # 优先用 Docker 网络地址（生产），失败则降级到 localhost（本地开发）
        for url in [self.base_url, self._local]:
            try:
                req = urllib.request.Request(url + path)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read())
            except Exception:
                continue
        raise RuntimeError("Prometheus unreachable at " + self.base_url + " and " + self._local)

    def query(self, expr: str) -> Dict[str, Any]:
        q = urllib.parse.urlencode({"query": expr})
        data = self._get("/api/v1/query?" + q)
        if data.get("status") != "success":
            raise RuntimeError("Prometheus query failed: " + str(data))
        return data["data"]

    def query_range(self, expr: str, start: int, end: int, step: str = "15s") -> Dict[str, Any]:
        params = urllib.parse.urlencode({"query": expr, "start": start, "end": end, "step": step})
        data = self._get("/api/v1/query_range?" + params)
        if data.get("status") != "success":
            raise RuntimeError("Prometheus query_range failed: " + str(data))
        return data["data"]

    def targets(self) -> List[Dict[str, Any]]:
        data = self._get("/api/v1/targets")
        if data.get("status") != "success":
            raise RuntimeError("Prometheus targets failed: " + str(data))
        return data["data"].get("activeTargets", [])

    def rules(self) -> Dict[str, Any]:
        return self._get("/api/v1/rules")

    # ── 快捷查询 ───────────────────────────────────────────────────────────

    def http_latency_summary(self) -> Dict[str, Any]:
        results = {}
        for pct in ["0.5", "0.95", "0.99"]:
            pname = "p" + str(int(float(pct) * 100))
            try:
                q = "histogram_quantile(" + pct + ", rate(cc_http_request_duration_seconds_bucket[5m]))"
                results[pname] = self.query(q).get("result", [])
            except Exception:
                results[pname] = []
        return results

    def _series(self, expr: str) -> List[Dict[str, Any]]:
        """查询并返回全部 series（**不要只取 result[0]**）。"""
        try:
            return self.query(expr).get("result", []) or []
        except Exception:
            return []

    @staticmethod
    def _nums(series: List[Dict[str, Any]]) -> List[float]:
        out: List[float] = []
        for s in series:
            v = (s.get("value") or [None, None])[1]
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f == f and abs(f) != float("inf"):   # 过滤 NaN / ±Inf
                out.append(f)
        return out

    def mysql_pool_status(self) -> Dict[str, Any]:
        """
        连接池状态。

        ⚠ 这里**不能只取 `result[0]`**：连接池指标是按副本上报的（3 条 series），
        实测某个副本的 `cc_mysql_pool_limit` 因为自身原因报 0，
        而 `result[0]` 恰好取到它 → 面板显示"连接上限 0"（另两个副本明明是 10）。
        正确做法是按语义聚合，并把"副本之间不一致"暴露出来：
          · active / idle → **sum**（全集群占用）
          · limit         → **max**（配置值，各副本应当相同）
        """
        out: Dict[str, Any] = {}
        active = self._nums(self._series('cc_mysql_pool_active{app="payment-app"}'))
        idle = self._nums(self._series('cc_mysql_pool_idle{app="payment-app"}'))
        limit = self._nums(self._series('cc_mysql_pool_limit{app="payment-app"}'))
        out["active"] = str(int(sum(active))) if active else "N/A"
        out["idle"] = str(int(sum(idle))) if idle else "N/A"
        out["limit"] = str(int(max(limit))) if limit else "N/A"
        out["replicas"] = str(max(len(active), len(idle), len(limit)))
        # 各副本 limit 不一致 → 面板要能看出来这是数据源问题，不是真的没配额
        out["limit_mismatch"] = "1" if limit and min(limit) != max(limit) else "0"
        out["limit_values"] = ",".join(str(int(v)) for v in limit)
        # ── 指标就绪度（`cc_mysql_pool_metrics_ok`）────────────────────────
        # 连接池指标是**懒创建**的：没有流量的副本不会建连接，于是 active/idle 报 0。
        # 面板若只显示 0，会被读成"这个集群真的没有连接" —— 实际上只是那个副本
        # 还没暴露这套指标。把"有几个副本的池指标是就绪的"直接显示出来。
        ok = self._nums(self._series('cc_mysql_pool_metrics_ok{app="payment-app"}'))
        out["metrics_ok"] = str(int(sum(1 for v in ok if v >= 1))) if ok else "0"
        out["metrics_ok_values"] = ",".join(str(int(v)) for v in ok)
        out["pool_all_zero"] = ("1" if (active and idle and sum(active) == 0 and sum(idle) == 0)
                                else "0")
        return out

    def mysql_global_status(self) -> Dict[str, str]:
        results = {}
        for k, q in [
            ("threads_connected",      "mysql_global_status_threads_connected"),
            ("max_used_connections",   "mysql_global_status_max_used_connections"),
            ("aborted_connects",      "mysql_global_status_aborted_connects"),
            ("uptime",               "mysql_global_status_uptime"),
        ]:
            try:
                d = self.query(q)
                results[k] = d.get("result", [{}])[0].get("value", [None, "N/A"])[1]
            except Exception:
                results[k] = "N/A"
        return results

    def txn_summary(self) -> Dict[str, str]:
        """交易汇总（按状态求和，同样是多副本上报）。"""
        results = {}
        for status in ["SETTLED", "PENDING", "FAILED"]:
            q = 'sum(cc_txn_total{status="' + status + '"})'
            try:
                d = self.query(q)
                results[status.lower()] = d.get("result", [{}])[0].get("value", [None, "0"])[1]
            except Exception:
                results[status.lower()] = "0"
        return results

    def http_latency_buckets(self) -> Dict[str, Any]:
        """
        HTTP 延迟 histogram 的**累计桶计数**，按 le 聚合到全集群。

        两个必须避开的坑（都踩过）：
          1. **`le` 的字符串形式**：Prometheus 里 1 秒的桶 label 是 `le="1"`，
             不是 `le="1.0"`。原先按固定列表查 `le="1.0"` → 0 条 series →
             默认 0 → 分布图上"≤1.0s = 0"，而 ≤2.5s 却有值（累积直方图不可能非单调）。
             现在改成**查一次 `sum by (le)`，用返回的真实 le 值**，不再硬编码。
          2. **不要只取 `result[0]`**：单条 series 只是"某个副本的某条路径"
             （实测 ~827），集群合计是 19826。必须 `sum`。
        """
        series = self._series("sum by (le) (cc_http_request_duration_seconds_bucket)")
        buckets: Dict[str, float] = {}
        total: float = 0.0
        for s in series:
            le = str((s.get("metric") or {}).get("le") or "")
            try:
                v = float((s.get("value") or [None, 0])[1])
            except (TypeError, ValueError):
                continue
            if v != v:
                continue
            if le in ("+Inf", "Inf"):
                total = v
                continue
            buckets[le] = v
        # 按数值排序，保证图表顺序正确（字符串排序会把 "2.5" 排在 "0.1" 前）
        def _key(x: str) -> float:
            try:
                return float(x)
            except ValueError:
                return float("inf")
        ordered = {k: buckets[k] for k in sorted(buckets, key=_key)}
        return {"buckets": ordered, "total": total}


    def _scalar(self, expr: str, default: float = 0.0) -> float:
        """查询返回单个标量。"""
        try:
            d = self.query(expr)
            res = d.get("result", [])
            if not res:
                return default
            return float(res[0].get("value", [None, default])[1])
        except Exception:
            return default

    def http_request_total(self) -> float:
        """HTTP 请求总数（histogram count）。"""
        return self._scalar("cc_http_request_duration_seconds_count")

    def mysql_max_connections(self) -> float:
        """MySQL max_connections。"""
        return self._scalar("mysql_global_variables_max_connections", 200.0)

    def all_metrics(self) -> Dict[str, Any]:
        """
        聚合所有关键指标。

        约 19 次独立查询，串行耗时可达 12s+，因此并发执行
        （urllib 为同步调用，用线程池并行）。整体耗时降至单次查询量级。
        """
        from concurrent.futures import ThreadPoolExecutor

        jobs = {
            "http_latency":          self.http_latency_summary,
            "http_latency_buckets":  self.http_latency_buckets,
            "http_request_total":    self.http_request_total,
            "mysql_pool":            self.mysql_pool_status,
            "mysql_global":          self.mysql_global_status,
            "mysql_max_connections": self.mysql_max_connections,
            "txn":                   self.txn_summary,
            "targets":               self._targets_meta,
        }

        out: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {name: pool.submit(fn) for name, fn in jobs.items()}
            for name, fut in futures.items():
                try:
                    out[name] = fut.result(timeout=25)
                except Exception as e:  # noqa: BLE001
                    out[name] = {} if name != "targets" else []
                    out[name + "_error"] = "%s: %s" % (type(e).__name__, e)
        return out

    def _targets_meta(self) -> List[Dict[str, Any]]:
        return [
            {"job": t["labels"]["job"], "health": t["health"],
             "lastError": t.get("lastError", "")}
            for t in (self.targets() or [])
        ]
