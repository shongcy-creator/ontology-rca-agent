# -*- coding: utf-8 -*-
"""
容器级指标导出器（**从容器外**观测，重启不会清零）。

## 为什么需要它（这是一次实测暴露的结构性缺口）

应用自己暴露的 `cc_container_oom_kill_total` 是**容器内**读 cgroup 得到的：
容器一旦被 OOM 杀掉并重启，**新容器从 0 开始**，Prometheus 眼里这条序列"生来就是 0"，
于是 `increase(cc_container_oom_kill_total[5m]) > 0` **永远为假**。
实测证据：注入层已确认 `oom_observed=true`（docker inspect 看到 OOMKilled），
而同时刻三个应用实例的该计数器**全部为 0**。

同类问题还有："容器掉 0" 这种瞬态判据在 15s 抓取间隔下基本抓不到
（Node 应用几秒就起来）。

结论：**"容器被杀过"这件事必须在容器外观测**。本导出器读 Docker API，
暴露容器生命周期里**单调**的量：

    cc_container_restart_count{container="..."}   容器重启次数（注入后 +1，可 increase()）
    cc_container_oom_killed{container="..."}      当前容器最近一次退出是否因 OOM（0/1）
    cc_container_running{container="..."}         是否在运行（0/1）

## 实现取舍

只用 **标准库**（`http.client` 直接走 UNIX socket 的 Docker API），
因此镜像可以是干净的 `python:3.11-alpine`，**不需要 Dockerfile、不需要 docker CLI**。
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
PORT = int(os.environ.get("EXPORTER_PORT", "9105"))
# 只关心这些容器（前缀匹配，逗号分隔）；留空 = 全部
PREFIXES = [p for p in os.environ.get("CONTAINER_PREFIXES", "").split(",") if p]
CACHE_TTL = float(os.environ.get("CACHE_TTL_S", "10"))


class _UnixHTTPConnection(http.client.HTTPConnection):
    """让 http.client 走 UNIX socket（Docker 的 API 入口）。"""

    def __init__(self, path: str, timeout: float = 10.0):
        super().__init__("localhost", timeout=timeout)
        self._unix_path = path

    def connect(self) -> None:  # noqa: D102
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._unix_path)


def _api(path: str):
    c = _UnixHTTPConnection(SOCK)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return json.loads(r.read().decode("utf-8"))
    finally:
        c.close()


def collect():
    """返回 [(name, restart_count, oom_killed, running), ...]。"""
    rows = []
    for c in _api("/containers/json?all=true"):
        names = [n.lstrip("/") for n in (c.get("Names") or [])]
        name = names[0] if names else c.get("Id", "")[:12]
        if PREFIXES and not any(name.startswith(p) for p in PREFIXES):
            continue
        try:
            d = _api("/containers/%s/json" % c["Id"])
        except Exception:  # noqa: BLE001
            continue
        st = d.get("State") or {}
        rows.append((name, int(d.get("RestartCount") or 0),
                     1 if st.get("OOMKilled") else 0,
                     1 if st.get("Running") else 0))
    return rows


_cache = {"at": 0.0, "body": b""}


def render() -> bytes:
    now = time.time()
    if now - _cache["at"] < CACHE_TTL and _cache["body"]:
        return _cache["body"]
    lines = []
    for name, restarts, oom, running in collect():
        lbl = '{container="%s"}' % name
        lines.append("cc_container_restart_count%s %d" % (lbl, restarts))
        lines.append("cc_container_oom_killed%s %d" % (lbl, oom))
        lines.append("cc_container_running%s %d" % (lbl, running))
    body = ("\n".join(lines) + "\n").encode("utf-8")
    _cache.update({"at": now, "body": body})
    return body


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") not in ("/metrics", ""):
            self.send_error(404)
            return
        try:
            body = render()
        except Exception as e:  # noqa: BLE001
            body = ("# exporter error: %s\n" % str(e)[:200]).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 静默：15s 一次抓取，不需要访问日志
        return


if __name__ == "__main__":
    print("[container-exporter] sock=%s port=%d prefixes=%s"
          % (SOCK, PORT, PREFIXES or "all"), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    sys.exit(0)
