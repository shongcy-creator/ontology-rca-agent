#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
信用卡运维演示环境压力测试台（stress harness）。

在 RCA 演示中主动制造负载 / 故障窗口，并输出可与告警时间窗对齐的分秒级统计
（time_series）。三种模式：

  * http  ：对应用入口（默认 http://localhost:8080/txn）持续发压；
  * mysql ：直连 MySQL 主库（默认 127.0.0.1:3306）反复执行轻量查询；可用
             --mysql-hold-connections 让每个工作线程开 N 个连接并持住，打满 max_connections；
  * mixed ：一半线程走 HTTP、一半线程走 MySQL，观察链路耦合。

设计要点：
  1. 仅标准库；PyMySQL 只在 mysql/mixed 模式下延迟导入，缺失时给出明确提示。
  2. 线程-每-工作线程模型：threading + 共享 Stats（threading.Lock 保护），到达
     --duration 或 --requests 上限后用 threading.Event 干净停止并 join 全部线程；
     等待一律用 Event.wait()，不做忙等。
  3. 单次请求的任何异常都被捕获并归类（timeout / connection_refused / http_5xx /
     json_decode / ...），压测台不会因为目标不可用而崩溃。
  4. 退出码：0 = 压测台正常完成；非 0 只表示压测台无法启动（URL 非法、--body 非
     JSON、mysql 模式缺少 PyMySQL）。目标返回 5xx 不影响退出码。

用法示例：
  python tools/stress_harness.py --duration 60 --concurrency 32
  python tools/stress_harness.py --mode mysql --mysql-hold-connections 8 --concurrency 16
  python tools/stress_harness.py --mode mixed --duration 120 --json-out out/stress.json --label fault
"""
import argparse
import json
import math
import os
import socket
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

#: 报告中输出的百分位（毫秒）；算法见 percentile()。
PERCENTILES = (50, 90, 95, 99)

#: /txn 默认请求体。
DEFAULT_BODY = '{"customer_id": 1, "amount": 100, "status": "SETTLED"}'

#: MySQL 模式默认查询（轻量、只读）。
DEFAULT_MYSQL_QUERY = "SELECT COUNT(*) FROM t_txn"

#: HTTP 非 2xx 的 error_kind 前缀（如 http_4xx / http_5xx）。
_HTTP_ERROR_PREFIX = "http_"


def percentile(sorted_values: List[float], p: float) -> float:
    """最近秩（nearest-rank）百分位。

    sorted_values 必须是已升序排序的样本列表（调用方负责排序，函数内不再排序）；
    p 取 0-100。返回选中样本的浮点值，样本为空时返回 0.0。

    定义：rank = ceil(p / 100 * n)，结果 = sorted_values[rank - 1]（秩从 1 开始）；
    p <= 0 取最小值，p >= 100 取最大值。该定义对小样本（n = 1、2 ...）同样给出
    真实观测值，不做线性插值，因此不会出现比实际更快/更慢的乐观估计。
    """
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if p <= 0:
        return float(sorted_values[0])
    if p >= 100:
        return float(sorted_values[-1])
    rank = int(math.ceil(p / 100.0 * n))
    return float(sorted_values[min(max(rank - 1, 0), n - 1)])


def summarize_latency(sorted_values: List[float]) -> Dict[str, float]:
    """对已排序延迟样本生成 min/p50/p90/p95/p99/max/mean 摘要（单位 ms）。"""
    summary: Dict[str, float] = {
        "min": round(float(sorted_values[0]), 3) if sorted_values else 0.0,
        "max": round(float(sorted_values[-1]), 3) if sorted_values else 0.0,
        "mean": round(float(statistics.fmean(sorted_values)), 3) if sorted_values else 0.0,
        "samples": len(sorted_values),
    }
    for p in PERCENTILES:
        summary["p%d" % p] = round(percentile(sorted_values, p), 3)
    return summary


class Stats(object):
    """线程安全的共享统计容器：所有读写都在 self._lock 下完成。"""

    def __init__(self, request_cap: int) -> None:
        self._lock = threading.Lock()
        self.stop_event = threading.Event()
        self.request_cap = request_cap      # 总请求上限，0 表示不限
        self.scheduled = 0                  # 已认领的请求配额
        self.start_time = time.time()
        self.deadline = 0.0                 # 绝对截止时间戳，0 表示不按时间截止
        self.total = 0
        self.success = 0
        self.errors = 0
        self.bytes_total = 0
        self.bytes_samples = 0              # 能拿到响应体字节数的请求数
        self.status_codes: Dict[str, int] = {}
        self.error_kinds: Dict[str, int] = {}
        self.latencies: List[float] = []
        self.bucket_latency: Dict[int, List[float]] = {}   # 分秒桶：秒 -> 延迟样本
        self.bucket_count: Dict[int, int] = {}
        self.bucket_errors: Dict[int, int] = {}

    def reserve(self) -> bool:
        """认领一个请求配额；达到 --requests 上限时返回 False。"""
        with self._lock:
            if self.request_cap and self.scheduled >= self.request_cap:
                return False
            self.scheduled += 1
            return True

    def expired(self) -> bool:
        """是否已越过 --duration 对应的截止时刻。"""
        return bool(self.deadline) and time.time() >= self.deadline

    def stop(self) -> None:
        """请求全体工作线程停止。"""
        self.stop_event.set()

    def record(self, ok: bool, status_code: int, error_kind: str,
               latency_ms: float, nbytes: int, bucket: int) -> None:
        """记录一次请求结果；bucket 为相对开始时间的整数秒。"""
        with self._lock:
            self.total += 1
            if ok:
                self.success += 1
            else:
                self.errors += 1
            self.latencies.append(latency_ms)
            key = str(status_code) if status_code else "no-response"
            self.status_codes[key] = self.status_codes.get(key, 0) + 1
            if error_kind:
                self.error_kinds[error_kind] = self.error_kinds.get(error_kind, 0) + 1
            if nbytes >= 0:
                self.bytes_total += nbytes
                self.bytes_samples += 1
            self.bucket_count[bucket] = self.bucket_count.get(bucket, 0) + 1
            if not ok:
                self.bucket_errors[bucket] = self.bucket_errors.get(bucket, 0) + 1
            self.bucket_latency.setdefault(bucket, []).append(latency_ms)

    def snapshot_counts(self) -> Tuple[int, int, int]:
        """返回 (total, success, errors) 的瞬时快照，供进度输出使用。"""
        with self._lock:
            return self.total, self.success, self.errors

    def sorted_latencies(self) -> List[float]:
        """返回升序排序后的全部延迟样本。"""
        with self._lock:
            return sorted(self.latencies)

    def counters(self) -> Dict[str, Any]:
        """返回计数器快照（含状态码与错误类型直方图）。"""
        with self._lock:
            return {
                "total": self.total,
                "success": self.success,
                "errors": self.errors,
                "bytes_total": self.bytes_total,
                "bytes_samples": self.bytes_samples,
                "status_codes": dict(self.status_codes),
                "error_kinds": dict(self.error_kinds),
            }

    def build_time_series(self) -> List[Dict[str, Any]]:
        """生成分秒序列 [{t, count, errors, p99_ms}]，t 为相对开始的整数秒。"""
        with self._lock:
            series: List[Dict[str, Any]] = []
            for bucket in sorted(self.bucket_count.keys()):
                lats = sorted(self.bucket_latency.get(bucket, []))
                series.append({
                    "t": bucket,
                    "count": self.bucket_count.get(bucket, 0),
                    "errors": self.bucket_errors.get(bucket, 0),
                    "p99_ms": round(percentile(lats, 99), 3) if lats else 0.0,
                })
            return series


def _header_value(headers: Any, name: str) -> str:
    """安全读取响应头（失败返回空串）。"""
    if headers is None:
        return ""
    try:
        return str(headers.get(name) or "")
    except Exception:                                          # noqa: BLE001
        return ""


def _header_length(headers: Any) -> Optional[int]:
    """从响应头读取 Content-Length；不可用时返回 None。"""
    raw = _header_value(headers, "Content-Length")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _classify_url_error(exc: BaseException) -> str:
    """把 urllib / socket 异常映射为稳定的 error_kind。"""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(reason, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(reason, ConnectionResetError):
        return "connection_reset"
    if isinstance(reason, socket.gaierror):
        return "dns_error"
    if isinstance(exc, urllib.error.URLError):
        return "connection_error"
    return "exception:%s" % type(exc).__name__


def _looks_like_json(content_type: str, raw: bytes) -> bool:
    """是否应做 JSON 解析：Content-Type 声明 json，或响应体以 { / [ 开头。"""
    if not raw:
        return False
    return "json" in content_type.lower() or raw.lstrip()[:1] in (b"{", b"[")


def http_request(cfg: "Config", headers: Dict[str, str]) -> Dict[str, Any]:
    """执行一次 HTTP 请求并返回统一结果字典（永不抛异常）。

    字段：ok / status / error_kind / error / latency_ms / bytes / bytes_known。
    判定：状态码 2xx/3xx 且（若服务端声明 JSON 或响应体形如 JSON）解析成功才算 ok。
    """
    url = cfg.url.rstrip("/") + cfg.path
    data: Optional[bytes] = None
    if cfg.method in ("POST", "PUT", "PATCH", "DELETE") and cfg.body:
        data = cfg.body.encode("utf-8")
    request = urllib.request.Request(url, data=data, method=cfg.method, headers=headers)

    started = time.perf_counter()
    status = 0
    body = b""
    content_length: Optional[int] = None
    content_type = ""
    error_kind = ""
    error_msg = ""
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
            status = int(getattr(response, "status", 0) or response.getcode())
            content_length = _header_length(response.headers)
            content_type = _header_value(response.headers, "Content-Type")
            body = response.read()
    except urllib.error.HTTPError as exc:      # 4xx / 5xx：服务端有响应，也是一次有效采样
        status = int(exc.code)
        content_length = _header_length(getattr(exc, "headers", None))
        content_type = _header_value(getattr(exc, "headers", None), "Content-Type")
        error_kind = "%s%dxx" % (_HTTP_ERROR_PREFIX, status // 100)
        error_msg = "HTTP %d" % status
        try:
            body = exc.read()
        except Exception:                                          # noqa: BLE001
            body = b""
    except Exception as exc:                   # 连接/超时/DNS 等：绝不向外抛
        error_kind = _classify_url_error(exc)
        error_msg = "%s: %s" % (type(exc).__name__, exc)

    latency_ms = (time.perf_counter() - started) * 1000.0
    nbytes = content_length if content_length is not None else len(body)

    if not error_kind and 200 <= status < 400 and _looks_like_json(content_type, body):
        try:
            json.loads(body.decode("utf-8", errors="replace"))
        except Exception as exc:                                   # noqa: BLE001
            error_kind = "json_decode"
            error_msg = "%s: %s" % (type(exc).__name__, exc)

    if not error_kind and not 200 <= status < 400:
        error_kind = ("%s%dxx" % (_HTTP_ERROR_PREFIX, status // 100)) if status else "no_response"
    return {
        "ok": not error_kind and 200 <= status < 400,
        "status": status,
        "error_kind": error_kind,
        "error": error_msg,
        "latency_ms": latency_ms,
        "bytes": nbytes,
        "bytes_known": content_length is not None,
    }


def _load_pymysql():
    """延迟导入 PyMySQL；缺失时抛出带修复建议的 ImportError。"""
    try:
        import pymysql                                        # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "MySQL 模式需要 PyMySQL，但当前解释器未安装：%s\n"
            "  安装：python -m pip install PyMySQL\n"
            "  或改用 HTTP 模式：--mode http" % exc
        ) from exc
    return pymysql


def _mysql_connect(cfg: "Config", pymysql_module: Any):
    """建立一个 MySQL 连接；超时统一取 --timeout。"""
    timeout = max(1, int(cfg.timeout))
    return pymysql_module.connect(
        host=cfg.mysql_host, port=cfg.mysql_port, user=cfg.mysql_user,
        password=cfg.mysql_password, database=cfg.mysql_db, charset="utf8mb4",
        autocommit=True, connect_timeout=timeout, read_timeout=timeout, write_timeout=timeout,
    )


def mysql_request(cfg: "Config", connection: Any) -> Dict[str, Any]:
    """执行一次 MySQL 查询并返回与 http_request 同构的结果字典。"""
    started = time.perf_counter()
    try:
        with connection.cursor() as cursor:
            cursor.execute(cfg.mysql_query)
            rows = cursor.fetchall()
        return {
            "ok": True, "status": 200, "error_kind": "", "error": "",
            "latency_ms": (time.perf_counter() - started) * 1000.0,
            "bytes": len(str(rows)) if rows is not None else 0,
            "bytes_known": True, "reconnect": False,
        }
    except Exception as exc:                                       # noqa: BLE001
        kind = "timeout" if isinstance(exc, (socket.timeout, TimeoutError)) \
            else "mysql_error:%s" % type(exc).__name__
        return {
            "ok": False, "status": 0, "error_kind": kind,
            "error": "%s: %s" % (type(exc).__name__, exc),
            "latency_ms": (time.perf_counter() - started) * 1000.0,
            "bytes": -1, "bytes_known": False, "reconnect": True,
        }


def _mysql_hold(cfg: "Config", stats: Stats, pymysql_module: Any) -> None:
    """持有模式：为当前线程开 N 个长连接并持住，直到压测结束。

    每次连接尝试按一次「请求」记录（成功 = 连接建立成功、延迟 = 建连耗时），
    便于与 max_connections 打满窗口在 time_series 上对齐。
    """
    connections = []
    for _ in range(cfg.mysql_hold_connections):
        if stats.stop_event.is_set():
            break
        bucket = int(time.time() - stats.start_time)
        started = time.perf_counter()
        try:
            connections.append(_mysql_connect(cfg, pymysql_module))
            stats.record(True, 200, "", (time.perf_counter() - started) * 1000.0, -1, bucket)
        except Exception as exc:                                   # noqa: BLE001
            stats.record(False, 0, "mysql_connect_error",
                         (time.perf_counter() - started) * 1000.0, -1, bucket)
            if not cfg.quiet:
                sys.stderr.write("[hold] 连接失败: %s: %s\n" % (type(exc).__name__, exc))
    stats.stop_event.wait()        # 阻塞保持连接（不忙等），直到主线程发出停止信号
    for conn in connections:
        try:
            conn.close()
        except Exception:                                          # noqa: BLE001
            pass


def worker(worker_id: int, cfg: "Config", stats: Stats, headers: Dict[str, str],
           pymysql_module: Any) -> None:
    """单工作线程主循环：认领配额 -> 发一次请求 -> 记录，直到停止条件命中。"""
    mode = cfg.mode
    if mode == "mixed":            # 偶数号线程走 HTTP，奇数号线程走 MySQL
        mode = "http" if worker_id % 2 == 0 else "mysql"
    if mode == "mysql" and pymysql_module is None:
        return
    if cfg.ramp_up > 0 and cfg.concurrency > 1:                # 铺开启动，避免惊群
        if stats.stop_event.wait(cfg.ramp_up * (worker_id / float(cfg.concurrency))):
            return
    if mode == "mysql" and cfg.mysql_hold_connections > 0:
        _mysql_hold(cfg, stats, pymysql_module)
        return

    connection = None
    try:
        while not stats.stop_event.is_set():
            if stats.expired() or not stats.reserve():
                break
            bucket = int(time.time() - stats.start_time)
            if mode == "http":
                result = http_request(cfg, headers)
            else:
                if connection is None:
                    try:
                        connection = _mysql_connect(cfg, pymysql_module)
                    except Exception as exc:                       # noqa: BLE001
                        stats.record(False, 0, "mysql_connect_error", 0.0, -1, bucket)
                        if not cfg.quiet:
                            sys.stderr.write("[mysql] 连接失败: %s: %s\n"
                                             % (type(exc).__name__, exc))
                        if stats.stop_event.wait(0.5):             # 退避，避免打爆客户端
                            break
                        continue
                result = mysql_request(cfg, connection)
                if result["reconnect"]:
                    try:
                        connection.close()
                    except Exception:                              # noqa: BLE001
                        pass
                    connection = None
            stats.record(bool(result["ok"]), int(result["status"]), str(result["error_kind"]),
                         float(result["latency_ms"]), int(result["bytes"]), bucket)
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:                                      # noqa: BLE001
                pass
        stats.stop_event.set()     # 任一分支退出都收敛全局停止信号


class Config(object):
    """压测参数载体（argparse.Namespace 的轻量封装，便于类型提示）。"""

    def __init__(self, args: argparse.Namespace) -> None:
        self.url: str = args.url
        self.path: str = args.path
        self.method: str = args.method.upper()
        self.body: Optional[str] = args.body
        self.concurrency: int = args.concurrency
        self.duration: float = args.duration
        self.requests: int = args.requests
        self.timeout: float = args.timeout
        self.ramp_up: float = args.ramp_up
        self.mode: str = args.mode
        self.mysql_host: str = args.mysql_host
        self.mysql_port: int = args.mysql_port
        self.mysql_user: str = args.mysql_user
        self.mysql_password: str = args.mysql_password
        self.mysql_db: str = args.mysql_db
        self.mysql_query: str = args.mysql_query
        self.mysql_hold_connections: int = args.mysql_hold_connections
        self.json_out: Optional[str] = args.json_out
        self.label: str = args.label
        self.quiet: bool = args.quiet


def build_arg_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（标志名英文，帮助文本中文）。"""
    parser = argparse.ArgumentParser(
        prog="stress_harness.py",
        description="HTTP / MySQL 压测台：输出统计报告与分秒级时间序列，便于与故障窗口对齐。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--url", default="http://localhost:8080", help="目标基地址（HTTP 模式）")
    parser.add_argument("--path", default="/txn", help="请求路径")
    parser.add_argument("--method", default="POST", help="HTTP 方法，如 GET/POST")
    parser.add_argument("--body", default=DEFAULT_BODY, help="请求体 JSON 字符串；传空串表示不带 body")
    parser.add_argument("--concurrency", type=int, default=16, help="并发工作线程数")
    parser.add_argument("--duration", type=float, default=30.0, help="持续秒数（0 表示只受 --requests 限制）")
    parser.add_argument("--requests", type=int, default=0, help="总请求数上限，0 表示不限（跑满 --duration）")
    parser.add_argument("--timeout", type=float, default=10.0, help="单请求超时秒数")
    parser.add_argument("--ramp-up", type=float, default=0.0, help="工作线程启动铺开秒数，0 表示同时启动")
    parser.add_argument("--mode", choices=("http", "mysql", "mixed"), default="http",
                        help="http=打应用入口；mysql=直连数据库；mixed=两者各半")
    parser.add_argument("--mysql-host", default="127.0.0.1", help="MySQL 主机")
    parser.add_argument("--mysql-port", type=int, default=3306, help="MySQL 端口")
    parser.add_argument("--mysql-user", default="appuser", help="MySQL 用户")
    parser.add_argument("--mysql-password", default="apppass", help="MySQL 密码")
    parser.add_argument("--mysql-db", default="creditcard", help="MySQL 库名")
    parser.add_argument("--mysql-query", default=DEFAULT_MYSQL_QUERY, help="MySQL 模式执行的查询")
    parser.add_argument("--mysql-hold-connections", type=int, default=0,
                        help="每个工作线程开 N 个连接并持住（用于打满 max_connections）")
    parser.add_argument("--json-out", default=None, help="把完整报告写入指定 JSON 文件")
    parser.add_argument("--label", default="", help="本次运行的标签，便于归档区分")
    parser.add_argument("--quiet", action="store_true", help="静默模式：不打印进度，仅输出最终报告")
    parser.add_argument("--version", action="version", version="stress_harness 1.0")
    return parser


def validate(cfg: Config) -> Optional[str]:
    """启动前校验；返回错误信息字符串表示压测台无法启动。"""
    if cfg.mode in ("http", "mixed"):
        parsed = urllib.parse.urlparse(cfg.url if "://" in cfg.url else "http://" + cfg.url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return "无法解析目标地址: %r" % cfg.url
    if cfg.concurrency < 1:
        return "--concurrency 必须 >= 1"
    if cfg.duration < 0 or cfg.requests < 0:
        return "--duration / --requests 不能为负"
    if cfg.timeout <= 0:
        return "--timeout 必须 > 0"
    if cfg.mysql_hold_connections < 0:
        return "--mysql-hold-connections 不能为负"
    if cfg.body:
        try:
            json.loads(cfg.body)
        except Exception as exc:                                   # noqa: BLE001
            return "--body 不是合法 JSON: %s" % exc
    return None


def _sorted_counts(counts: Dict[str, int], numeric: bool = False) -> Dict[str, int]:
    """状态码按数值升序、错误类型按数量倒序输出，保证报告稳定可读。"""
    if numeric:
        keys = sorted(counts.keys(), key=lambda k: int(k) if k.isdigit() else 10 ** 9)
    else:
        keys = sorted(counts.keys(), key=lambda k: (-counts[k], k))
    return {k: counts[k] for k in keys}


def build_report(cfg: Config, stats: Stats, wall_seconds: float) -> Dict[str, Any]:
    """汇总一次运行的完整报告（文本表格与 JSON 输出共用同一份数据）。"""
    counters = stats.counters()
    total, success, errors = counters["total"], counters["success"], counters["errors"]
    wall = wall_seconds if wall_seconds > 0 else 1e-9
    bytes_total, bytes_samples = counters["bytes_total"], counters["bytes_samples"]
    mysql_dsn = "%s@%s:%d/%s" % (cfg.mysql_user, cfg.mysql_host, cfg.mysql_port, cfg.mysql_db)
    return {
        "label": cfg.label,
        "mode": cfg.mode,
        "target": {
            "url": cfg.url,
            "path": cfg.path,
            "method": cfg.method,
            "mysql": mysql_dsn if cfg.mode in ("mysql", "mixed") else None,
            "mysql_query": cfg.mysql_query if cfg.mode in ("mysql", "mixed") else None,
        },
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stats.start_time)),
        "wall_seconds": round(wall_seconds, 3),
        "concurrency": cfg.concurrency,
        "ramp_up_seconds": cfg.ramp_up,
        "requests": {
            "total": total,
            "success": success,
            "error": errors,
            "success_rate": round(success / total, 4) if total else 0.0,
        },
        "status_codes": _sorted_counts(counters["status_codes"], numeric=True),
        "error_kinds": _sorted_counts(counters["error_kinds"]),
        "latency_ms": summarize_latency(stats.sorted_latencies()),
        "throughput": {
            "requests_per_sec": round(total / wall, 3),
            "bytes_per_sec": round(bytes_total / wall, 3) if bytes_samples else None,
            "bytes_total": bytes_total if bytes_samples else None,
        },
        "time_series": stats.build_time_series(),
        "percentile_method": "nearest-rank: rank = ceil(p/100 * n) on sorted samples",
    }


def _print_kv(rows: List[Tuple[str, Any]], width: int = 22) -> None:
    """打印对齐的 key/value 表。"""
    for key, value in rows:
        print("  %-*s : %s" % (width, key, value))


def _print_histogram(counts: Dict[str, int], width: int = 22) -> None:
    """打印带百分比与条形图的直方图。"""
    if not counts:
        print("  (无)")
        return
    total = sum(counts.values()) or 1
    for key, value in counts.items():
        pct = value * 100.0 / total
        print("  %-*s : %8d  %6.2f%%  %s" % (width, key, value, pct, "#" * int(round(pct / 2.0))))


def print_report(report: Dict[str, Any]) -> None:
    """以对齐文本表格打印报告。"""
    line = "=" * 64
    target = report["target"]
    if report["mode"] == "mysql":
        target_text = "mysql://%s  query=%s" % (target["mysql"], target["mysql_query"])
    else:
        target_text = "%s %s%s" % (target["method"], target["url"], target["path"])
    req, lat, thr = report["requests"], report["latency_ms"], report["throughput"]
    rows: List[Tuple[str, Any]] = [
        ("target", target_text),
        ("started_at", report["started_at"]),
        ("wall_seconds", report["wall_seconds"]),
        ("concurrency", report["concurrency"]),
        ("ramp_up_seconds", report["ramp_up_seconds"]),
        ("total", req["total"]),
        ("success", req["success"]),
        ("error", req["error"]),
        ("success_rate", "%.2f%%" % (req["success_rate"] * 100.0)),
        ("requests_per_sec", thr["requests_per_sec"]),
        ("bytes_per_sec", thr["bytes_per_sec"] if thr["bytes_per_sec"] is not None else "n/a"),
    ]
    for key in ("min", "p50", "p90", "p95", "p99", "max", "mean", "samples"):
        rows.append(("latency_ms.%s" % key, lat.get(key, 0.0)))
    print(line)
    print("压力测试报告  label=%s  mode=%s" % (report["label"] or "-", report["mode"]))
    print(line)
    _print_kv(rows)
    print("-" * 64)
    print("状态码直方图")
    _print_histogram(report["status_codes"])
    print("-" * 64)
    print("错误类型计数")
    _print_histogram(report["error_kinds"])
    print("-" * 64)
    print("分秒序列 (t, count, errors, p99_ms)")
    series = report["time_series"]
    if not series:
        print("  (无样本)")
    for point in series:
        print("  %6s  %8s  %6s  %10s"
              % (point["t"], point["count"], point["errors"], point["p99_ms"]))
    print(line)


def run(cfg: Config) -> Dict[str, Any]:
    """执行一次压测：起线程 -> 节拍监控 -> 停止并 join -> 生成报告。"""
    pymysql_module = None
    if cfg.mode in ("mysql", "mixed"):
        pymysql_module = _load_pymysql()       # 缺失时抛 ImportError，由 main 处理
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "credit-card-sys-ops/stress_harness",
        "Connection": "keep-alive",
    }
    stats = Stats(request_cap=cfg.requests)
    stats.start_time = time.time()
    if cfg.duration > 0:
        stats.deadline = stats.start_time + cfg.duration
    threads = [
        threading.Thread(target=worker, name="stress-%03d" % i, daemon=True,
                         args=(i, cfg, stats, headers, pymysql_module))
        for i in range(cfg.concurrency)
    ]
    for thread in threads:
        thread.start()
    try:                                       # 主线程只当节拍器：每秒一次进度，绝不忙等
        while not stats.stop_event.is_set():
            if stats.expired() or (cfg.requests and stats.snapshot_counts()[0] >= cfg.requests):
                break
            if stats.stop_event.wait(1.0):
                break
            if not cfg.quiet:
                total, success, errors = stats.snapshot_counts()
                print("[%6.1fs] total=%d success=%d error=%d"
                      % (time.time() - stats.start_time, total, success, errors))
    except KeyboardInterrupt:
        if not cfg.quiet:
            print("\n收到中断信号，正在停止工作线程 ...")
    finally:
        stats.stop()
        join_deadline = time.time() + max(cfg.timeout, 1.0) + 10.0
        for thread in threads:
            thread.join(timeout=max(0.1, join_deadline - time.time()))
    alive = [t.name for t in threads if t.is_alive()]
    if alive and not cfg.quiet:
        sys.stderr.write("警告：以下线程未在超时内退出：%s\n" % ", ".join(alive))
    return build_report(cfg, stats, time.time() - stats.start_time)


def write_json_out(path: str, report: Dict[str, Any]) -> Optional[str]:
    """把报告写入 JSON 文件；成功返回 None，失败返回错误信息。"""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        return None
    except Exception as exc:                                       # noqa: BLE001
        return "%s: %s" % (type(exc).__name__, exc)


def main(argv: Optional[List[str]] = None) -> int:
    """入口：返回进程退出码。0 = 压测台正常完成（目标 5xx 不算失败）。"""
    cfg = Config(build_arg_parser().parse_args(argv))
    problem = validate(cfg)
    if problem:
        sys.stderr.write("启动失败：%s\n" % problem)
        return 2
    try:
        report = run(cfg)
    except ImportError as exc:
        sys.stderr.write("启动失败：%s\n" % exc)
        return 3
    except KeyboardInterrupt:
        sys.stderr.write("已中断。\n")
        return 0
    except Exception as exc:                                       # noqa: BLE001
        sys.stderr.write("压测台异常终止：%s: %s\n" % (type(exc).__name__, exc))
        return 4
    print_report(report)
    if cfg.json_out:
        error = write_json_out(cfg.json_out, report)
        if error:
            sys.stderr.write("写入 --json-out 失败：%s\n" % error)
        elif not cfg.quiet:
            print("报告已写入 %s" % cfg.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
