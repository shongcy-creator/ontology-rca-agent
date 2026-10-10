# -*- coding: utf-8 -*-
"""
故障注入内核（chaos core）。

职责划分：
  · core.py     —— 基础设施：docker / SQL / Prometheus 访问，Fault 模型，状态文件，注入引擎
  · catalog.py  —— 故障目录：每个场景的 inject/recover 实现与期望根因
  · ../fault_injector.py —— CLI

三条设计原则：
  1. **可回滚**：每次注入都把"回滚所需的全部信息"写进 .chaos/fault_state.json。
     即使注入进程被 Ctrl-C 杀掉，`fault_injector.py recover --all` 仍能复原。
     回滚由 fault_id + targets + params + recover_hint 数据驱动，不依赖内存闭包。
  2. **可验证**：每个故障声明 PromQL `signals`，注入后引擎会等待其中至少一条
     变为真，避免"注入了但没有任何可观测证据"的假成功。
  3. **只读边界**：故障注入只操作模拟环境的容器与数据库，不修改业务代码；
     Agent 侧工具依旧全部只读（诊断与注入严格分离）。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
CHAOS_DIR = ROOT / ".chaos"
STATE_FILE = CHAOS_DIR / "fault_state.json"

#: 服务地址 —— 默认给"在宿主机上直接跑 CLI"用；容器内（控制台）通过
#: CHAOS_PROM_URL / CHAOS_APP_LB / CHAOS_RCA_API 覆盖为 compose 服务名。
PROM = os.environ.get("CHAOS_PROM_URL", "http://localhost:9090")
APP_LB = os.environ.get("CHAOS_APP_LB", "http://localhost:8080")
RCA_API = os.environ.get("CHAOS_RCA_API", "http://localhost:8088")
#: 控制台（前端 nginx）—— 用于验证"浏览器 → nginx → 后端"这条真实路径是否通。
#: 单独列出来是因为"容器都 healthy 但这条代理断掉"是实测踩过的故障模式。
CONSOLE = os.environ.get("CHAOS_CONSOLE_URL", "http://localhost:3001")

MYSQL_PRIMARY = "cc-mysql-core"
MYSQL_REPLICAS = ["cc-mysql-replica-1", "cc-mysql-replica-2"]
APP_GATEWAY = "cc-app-gateway"
COMPOSE_SERVICE_APP = "payment-app"

MYSQL_ROOT_PWD = "rootpass"
MYSQL_APP_USER = "appuser"
MYSQL_APP_PWD = "apppass"
MYSQL_DB = "creditcard"

#: 应用副本的 compose 服务标签（用于动态发现副本，不写死容器名）
COMPOSE_LABEL_APP = "com.docker.compose.service=" + COMPOSE_SERVICE_APP


# ═════════════════════════════════════════════════════════════════════════════
# 进程 / docker / 数据库 / Prometheus 基础访问
# ═════════════════════════════════════════════════════════════════════════════

def docker(args: List[str], timeout: int = 120,
           input_bytes: Optional[bytes] = None) -> Tuple[int, str, str]:
    """执行 docker 子命令，返回 (rc, stdout, stderr)。"""
    exe = (os.environ.get("CC_DOCKER") or shutil.which("docker")
           or shutil.which("docker.exe") or "docker")
    try:
        p = subprocess.run([exe] + args, input=input_bytes, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "docker 命令超时: %s" % " ".join(args[:3])
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def docker_exec(container: str, command: str, detach: bool = False,
                timeout: int = 60, env: Optional[Dict[str, str]] = None
                ) -> Tuple[int, str, str]:
    """在容器内执行 shell 命令（登录 shell，具备 apk/iproute2 等环境）。"""
    args = ["exec"]
    if detach:
        args.append("-d")
    for k, v in (env or {}).items():
        args += ["-e", "%s=%s" % (k, v)]
    args += [container, "sh", "-lc", command]
    return docker(args, timeout=timeout)


def kill_by_marker(container: str, marker: str, timeout: int = 90) -> Dict[str, Any]:
    """
    按"命令行标记"杀掉容器内的注入进程（不依赖 pkill/pgrep）。

    为什么不用 pkill：`mysql:8.0` 镜像基于 Oracle Linux，**没有安装 procps**
    （`ps`/`pkill`/`pgrep` 全部缺失）。早期版本用 `pkill -f <pattern>` 回滚，
    命令静默失败（`2>/dev/null; true` 还让 rc=0），注入的循环继续跑，
    表现为"回滚后信号不消失"——四个场景的恢复校验失败全部源于此。

    这里的实现只依赖 POSIX sh 内建 + `cat`：
      · 扫描 /proc/<pid>/cmdline，用 `case` 做子串匹配（不需要 grep/tr）
      · 标记值通过 **环境变量** 传入，扫描 shell 自身的 cmdline 里只有
        `$CHAOS_MARK` 字面量、不含标记值，因此不会自杀
      · 显式跳过 PID 1（容器主进程）

    Args:
        container: 目标容器
        marker: 唯一标记（会被注入命令带上，如 SQL 注释 / 参数名）

    Returns:
        {"ok": bool, "killed": int, "rc": int, "stderr": str}
    """
    script = (
        'n=0; '
        # 用**单次 grep** 读出所有匹配的 /proc/<pid>/cmdline 路径。
        #
        # 之前是 `for d in /proc/[0-9]*; do cmd=$(cat $d/cmdline); ...` ——
        # 每个 PID fork 一个 `cat`。当容器里有大量进程时这是灾难：
        # cc-mysql-core 曾累积 1769 个僵尸 → 1769 次 fork → 单条命令 ~10s，
        # 6 个容器的清理从 17s 涨到 75.6s（看起来像卡死）。
        # grep 只 fork 一次，读全部 cmdline。
        #
        # 自身安全：glob 在 shell 里**先于** grep 的 fork 展开，所以 grep 自己的
        # /proc 条目不在列表里；而本 sh 的 cmdline 里只有 `$CHAOS_MARK` 字面量、
        # 不含标记值 → 不会自杀。
        'for f in $(grep -l -a "$CHAOS_MARK" /proc/[0-9]*/cmdline 2>/dev/null); do '
        'pid=${f#/proc/}; pid=${pid%%/*}; '
        '[ "$pid" = "1" ] && continue; '
        'if kill -9 "$pid" 2>/dev/null; then n=$((n+1)); fi; '
        'done; '
        'echo "killed=$n"'
    )
    rc, out, err = docker_exec(container, script, env={"CHAOS_MARK": marker},
                              timeout=timeout)
    killed = 0
    for line in (out or "").splitlines():
        if line.startswith("killed="):
            try:
                killed = int(line.split("=", 1)[1])
            except ValueError:
                killed = 0
    return {"ok": rc == 0, "killed": killed, "rc": rc, "stderr": (err or "")[-200:]}


def zombie_count(container: str, timeout: int = 30) -> Dict[str, Any]:
    """
    统计容器内的**僵尸进程**数。

    ⚠ 为什么不能直接用 `ps`：`mysql:8.0` 镜像基于 Oracle Linux，**没装 procps**
    （`ps`/`pkill`/`pgrep` 全部缺失）。早期体检脚本里写的
    `ps -eo stat | grep -c '^Z'` 因为 `ps: command not found` 而**静默返回 0** ——
    于是一个真实存在 1769 个僵尸的容器被报告为"僵尸 0 ✅"。**假阴性比没有检查更坏**。

    这里只依赖 shell + grep 读 `/proc/<pid>/status` 的 `State:` 行，
    与镜像是否装了 procps 无关。

    Returns:
        {"ok": bool, "total": int, "by_comm": {name: count}, "top": "name=count, ..."}
    """
    script = (
        'grep -l "^State:.*Z" /proc/[0-9]*/status 2>/dev/null | '
        'while read f; do cat "${f%status}comm" 2>/dev/null; echo; done '
        '| sort | uniq -c | sort -rn'
    )
    rc, out, err = docker_exec(container, script, timeout=timeout)
    by_comm: Dict[str, int] = {}
    total = 0
    for line in (out or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            n = int(parts[0])
            by_comm[parts[1].strip()] = n
            total += n
    return {"ok": rc == 0, "total": total, "by_comm": by_comm,
            "top": ", ".join("%s=%d" % (k, v) for k, v in list(by_comm.items())[:4]),
            "stderr": (err or "")[-120:]}


def mysql_kill_sessions(container: str, where: str, user: str = "root",
                        pwd: str = MYSQL_ROOT_PWD) -> int:
    """
    按条件杀掉 MySQL 会话（服务端侧兜底）。

    注意：`processlist.info` **不保留 SQL 注释**（实测 `SELECT SLEEP(30) /* mark */`
    的 info 只有 `SELECT SLEEP(30)`），所以不能用注释标记来匹配会话，
    必须用 `command` / `state` / `time` 这类服务端真实字段。
    """
    rc, out, _ = mysql_sql(container,
                           "SELECT id FROM information_schema.processlist WHERE %s" % where,
                           user=user, pwd=pwd)
    killed = 0
    if rc == 0:
        for line in (out or "").strip().splitlines():
            sid = line.strip()
            if sid.isdigit():
                mysql_sql(container, "KILL %d" % int(sid), user=user, pwd=pwd)
                killed += 1
    return killed


def mysql_sql(container: str, sql: str, user: str = "root", pwd: str = MYSQL_ROOT_PWD,
              db: str = "", timeout: int = 60) -> Tuple[int, str, str]:
    """在 MySQL 容器内执行 SQL（批量、无表头输出）。"""
    args = ["exec", container, "mysql", "-u", user, "-p" + pwd, "-N", "-B"]
    if db:
        args.append(db)
    args += ["-e", sql]
    return docker(args, timeout=timeout)


def mysql_scalar(container: str, sql: str, user: str = "root", pwd: str = MYSQL_ROOT_PWD,
                 db: str = "") -> str:
    rc, out, _ = mysql_sql(container, sql, user=user, pwd=pwd, db=db)
    return (out or "").strip().splitlines()[0].strip() if rc == 0 and (out or "").strip() else ""


def http_json(url: str, method: str = "GET", payload: Any = None,
              timeout: float = 20.0) -> Tuple[int, Any]:
    """HTTP 调用，返回 (status, parsed_body)。失败返回 (0, error_str)。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body[:500]
    except Exception as e:  # noqa: BLE001
        return 0, "%s: %s" % (type(e).__name__, e)


# ── Prometheus ───────────────────────────────────────────────────────────────

def prom_query(expr: str, timeout: float = 15.0) -> List[Dict[str, Any]]:
    """瞬时查询，返回 result 列表（失败返回空列表）。"""
    url = PROM + "/api/v1/query?query=" + urllib.parse.quote(expr)
    st, body = http_json(url, timeout=timeout)
    if st == 200 and isinstance(body, dict):
        return ((body.get("data") or {}).get("result")) or []
    return []


def prom_value(expr: str) -> Optional[float]:
    """标量查询：返回第一个样本的值（无样本返回 None）。"""
    res = prom_query(expr)
    if not res:
        return None
    try:
        return float(res[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def prom_labels(expr: str, label: str) -> List[str]:
    """查询并提取某个标签的取值集合。"""
    out: List[str] = []
    for r in prom_query(expr):
        v = (r.get("metric") or {}).get(label)
        if v and v not in out:
            out.append(v)
    return out


def prom_wait(expr: str, timeout_s: float = 90.0, interval_s: float = 5.0,
              expect: str = "truthy",
              should_stop: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """
    等待某个 PromQL 信号表达式满足条件。

    信号表达式书写约定（catalog 里必须遵守，否则判定会失真）：
      表达式在"条件成立"时应返回 **数值 > 0** 的样本序列。
        ✔ count(up{job="x"} == 1) < 3        （成立时返回 1）
        ✔ max(a / b) > 0.8                    （成立时返回 1）
        ✔ rate(x[2m]) > 0                     （成立时返回 1）
        ✘ up{job="x"} == 0   ← 过滤式，成立时返回的是 **0**，会被误判为不成立
          应改写为 count(up{job="x"} == 0) > 0
      （PromQL 里 `bool` 是比较运算符的修饰符而不是函数，不能用 bool(...) 包。）

    expect:
      truthy —— 存在样本且最大值 > 0
      falsy  —— 无样本，或所有样本值均为 0（用于"恢复校验"）
      any    —— 至少有一个样本
      empty  —— 没有任何样本

    should_stop: 可选的取消回调。返回 True 时**立即**返回（用于人工"停止演练"，
      否则一个场景最长要在这里卡 75s，用户会以为点了没反应）。
    """
    deadline = time.time() + timeout_s
    last: List[Dict[str, Any]] = []

    def _maxval(res: List[Dict[str, Any]]) -> Optional[float]:
        vals: List[float] = []
        for r in res:
            try:
                vals.append(float(r["value"][1]))
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        return max(vals) if vals else None

    while True:
        if should_stop and should_stop():
            return {"ok": False, "expr": expr, "waited_s": 0.0, "result": last[:5],
                    "cancelled": True, "error": "已按请求取消等待"}
        res = prom_query(expr)
        last = res
        waited = round(timeout_s - max(0.0, deadline - time.time()), 1)
        if expect == "any" and res:
            return {"ok": True, "expr": expr, "waited_s": waited, "result": res[:5]}
        if expect == "empty" and not res:
            return {"ok": True, "expr": expr, "waited_s": waited, "result": []}
        if expect == "truthy":
            mv = _maxval(res)
            if mv is not None and mv > 0:
                return {"ok": True, "expr": expr, "waited_s": waited, "result": res[:5]}
        if expect == "falsy":
            mv = _maxval(res)
            if not res or mv == 0:
                return {"ok": True, "expr": expr, "waited_s": waited, "result": res[:5]}
        if time.time() >= deadline:
            break
        time.sleep(interval_s)

    return {"ok": False, "expr": expr, "waited_s": timeout_s, "result": last[:5],
            "error": "等待超时（%ss 内未满足 expect=%s）" % (timeout_s, expect)}


# ═════════════════════════════════════════════════════════════════════════════
# 集群成员发现
# ═════════════════════════════════════════════════════════════════════════════

def app_replicas(only_running: bool = True) -> List[Dict[str, str]]:
    """发现应用层副本（基于 compose service 标签，不写死容器名）。"""
    filt = ["ps"]
    if only_running:
        filt.append("-a")     # 包含已退出副本，便于"副本丢失"类故障观察
    filt += ["--filter", "label=" + COMPOSE_LABEL_APP,
             "--format", "{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Status}}"]
    rc, out, _ = docker(filt)
    reps: List[Dict[str, str]] = []
    if rc != 0:
        return reps
    for line in (out or "").strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            reps.append({"id": parts[0], "name": parts[1], "state": parts[2], "status": parts[3]})
    return sorted(reps, key=lambda r: r["name"])


def pick_app_replica(selector: str = "first") -> Optional[Dict[str, str]]:
    """
    选择目标副本。

    selector: first / last / random-ish(round robin by time) / 容器名或 ID 片段
    """
    reps = app_replicas()
    if not reps:
        return None
    if selector in ("first", "", None):
        return reps[0]
    if selector == "last":
        return reps[-1]
    if selector == "roundrobin":
        return reps[int(time.time()) % len(reps)]
    for r in reps:
        if selector in r["name"] or selector == r["id"][:12]:
            return r
    return None


def all_mysql_nodes() -> List[str]:
    return [MYSQL_PRIMARY] + MYSQL_REPLICAS


def container_ip(name: str) -> str:
    rc, out, _ = docker([
        "inspect", name,
        "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"])
    return (out or "").strip() if rc == 0 else ""


def container_state(name: str) -> Dict[str, Any]:
    """容器状态摘要（含 OOMKilled / RestartCount，RCA 环境层工具同源）。"""
    rc, out, _ = docker([
        "inspect", name,
        "--format",
        "{{.State.Status}}|{{.State.OOMKilled}}|{{.RestartCount}}|{{.State.ExitCode}}|"
        "{{.HostConfig.Memory}}|{{.HostConfig.NanoCpus}}|{{.State.StartedAt}}"])
    if rc != 0:
        return {"name": name, "exists": False}
    parts = (out or "").strip().split("|")
    while len(parts) < 7:
        parts.append("")
    return {
        "name": name,
        "exists": True,
        "status": parts[0],
        "oom_killed": parts[1].lower() == "true",
        "restart_count": int(parts[2] or 0) if parts[2].isdigit() else parts[2],
        "exit_code": parts[3],
        "memory_limit_mb": round(int(parts[4] or 0) / 1024 / 1024, 1) if parts[4].isdigit() else None,
        "nano_cpus": parts[5] or None,
        "started_at": parts[6],
    }


# ═════════════════════════════════════════════════════════════════════════════
# Fault 模型
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class FaultContext:
    """一次注入/回滚的上下文。"""
    fault_id: str
    targets: List[str]
    params: Dict[str, Any]
    recover_hint: Dict[str, Any] = field(default_factory=dict)

    def p(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)


@dataclass
class Fault:
    """故障场景定义。"""
    id: str
    layer: str                    # application | database | runtime | resource
    title: str
    description: str
    category: str                 # 期望的 RCA 根因类别：资源/配置/依赖/数据/代码
    inject: Callable[[FaultContext], Dict[str, Any]]
    recover: Callable[[FaultContext], Dict[str, Any]]
    signals: List[str] = field(default_factory=list)     # 注入后应变为真的 PromQL
    recovered_signals: List[str] = field(default_factory=list)  # 回滚后应恢复的 PromQL
    default_params: Dict[str, Any] = field(default_factory=dict)
    params_help: Dict[str, str] = field(default_factory=dict)
    selector: str = "first"       # 目标选择器：first/last/roundrobin/all/自定义
    expected_root_cause: str = ""
    ontology_terms: List[str] = field(default_factory=list)
    needs_stress: bool = False    # 是否必须叠加压测才能成灾
    signal_needs_stress: bool = False  # 信号是否必须叠加压测才可观测
    alert_names: List[str] = field(default_factory=list)  # 对应告警规则名
    blast_radius: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "layer": self.layer, "title": self.title,
            "description": self.description, "category": self.category,
            "signals": self.signals, "recovered_signals": self.recovered_signals,
            "selector": self.selector,
            "default_params": self.default_params, "params_help": self.params_help,
            "expected_root_cause": self.expected_root_cause,
            "ontology_terms": self.ontology_terms,
            "needs_stress": self.needs_stress,
            "signal_needs_stress": self.signal_needs_stress,
            "alert_names": self.alert_names,
            "blast_radius": self.blast_radius,
        }


# ═════════════════════════════════════════════════════════════════════════════
# 注入状态（可回滚）
# ═════════════════════════════════════════════════════════════════════════════

def _load_state() -> Dict[str, Any]:
    if not STATE_FILE.is_file():
        return {"updated_at": None, "active": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"updated_at": None, "active": []}


def _save_state(state: Dict[str, Any]) -> None:
    CHAOS_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def active_faults(fault_id: Optional[str] = None) -> List[Dict[str, Any]]:
    act = _load_state().get("active") or []
    if fault_id:
        act = [a for a in act if a.get("fault_id") == fault_id]
    return act


def _record_active(entry: Dict[str, Any]) -> None:
    st = _load_state()
    st["active"] = [a for a in (st.get("active") or [])
                    if not (a.get("fault_id") == entry["fault_id"]
                            and a.get("targets") == entry["targets"])]
    st["active"].append(entry)
    _save_state(st)


def _clear_active(fault_id: str, targets: Optional[List[str]] = None) -> None:
    st = _load_state()
    st["active"] = [
        a for a in (st.get("active") or [])
        if not (a.get("fault_id") == fault_id
                and (targets is None or a.get("targets") == targets))
    ]
    _save_state(st)


# ═════════════════════════════════════════════════════════════════════════════
# 注入引擎
# ═════════════════════════════════════════════════════════════════════════════

class Injector:
    """故障注入引擎：注入 → 记录状态 → 验证信号 → （可选）自动回滚。"""

    def __init__(self, registry: Dict[str, "Fault"], verbose: bool = True) -> None:
        self.registry = registry
        self.verbose = verbose

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    # ── 目标解析 ──────────────────────────────────────────────────

    def resolve_targets(self, fault: Fault) -> List[str]:
        sel = fault.selector
        if sel == "mysql-primary":
            return [MYSQL_PRIMARY]
        if sel == "mysql-replicas":
            return list(MYSQL_REPLICAS)
        if sel == "mysql-all":
            return all_mysql_nodes()
        if sel == "gateway":
            return [APP_GATEWAY]
        if sel == "app-all":
            return [r["name"] for r in app_replicas()]
        # 默认：应用副本
        if sel == "first":
            r = pick_app_replica("first")
            return [r["name"]] if r else []
        if sel == "last":
            r = pick_app_replica("last")
            return [r["name"]] if r else []
        if sel == "roundrobin":
            r = pick_app_replica("roundrobin")
            return [r["name"]] if r else []
        r = pick_app_replica(sel)
        return [r["name"]] if r else [sel]

    # ── 注入 ──────────────────────────────────────────────────────

    def inject(self, fault_id: str, params: Optional[Dict[str, Any]] = None,
               targets: Optional[List[str]] = None, wait_signals: bool = True,
               signal_timeout: float = 75.0,
               should_stop: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
        fault = self.registry.get(fault_id)
        if fault is None:
            return {"ok": False, "error": "未知故障场景: %s（可用: %s）"
                    % (fault_id, ", ".join(sorted(self.registry)))}

        merged = dict(fault.default_params)
        merged.update(params or {})
        tgt = targets or self.resolve_targets(fault)

        result: Dict[str, Any] = {
            "ok": False, "fault_id": fault_id, "layer": fault.layer,
            "title": fault.title, "category": fault.category,
            "targets": tgt, "params": merged,
            "injected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "expected_root_cause": fault.expected_root_cause,
            "ontology_terms": fault.ontology_terms,
            "blast_radius": fault.blast_radius,
            "needs_stress": fault.needs_stress,
        }

        fail = ", ".join(str(t) for t in tgt) if tgt else "(未匹配到目标)"
        self.log("[inject] %-26s target=%s" % (fault_id, fail))

        try:
            ctx = FaultContext(fault_id=fault_id, targets=tgt, params=merged)
            out = fault.inject(ctx) or {}
        except Exception as e:  # noqa: BLE001
            result["error"] = "%s: %s" % (type(e).__name__, e)
            return result

        result["inject_result"] = out
        if not out.get("ok", True):
            result["error"] = out.get("error") or "注入失败"
            return result

        hint = out.get("recover_hint") or {}
        result["recover_hint"] = hint

        entry = {
            "fault_id": fault_id,
            "layer": fault.layer,
            "category": fault.category,
            "targets": tgt,
            "params": merged,
            "recover_hint": hint,
            "injected_at": result["injected_at"],
            "title": fault.title,
            "expected_root_cause": fault.expected_root_cause,
        }
        _record_active(entry)
        self.log("[inject] 已记录回滚状态 → %s" % STATE_FILE.name)

        # ── 信号验证：至少一条声明信号变为真 ──────────────────────
        if wait_signals and fault.signals:
            checks = []
            any_ok = False
            deadline = time.time() + signal_timeout
            for expr in fault.signals:
                remain = max(5.0, deadline - time.time())
                r = prom_wait(expr, timeout_s=remain, interval_s=5.0, expect="truthy",
                              should_stop=should_stop)
                checks.append({"expr": expr, **r})
                if r.get("ok"):
                    any_ok = True
                    break
                if r.get("cancelled"):
                    # 取消也要把"注入已完成"这件事记下来（状态文件已写），
                    # 由调用方负责回滚 —— 绝不在这里"假装没注入过"。
                    result["signal_checks"] = checks
                    result["signal_observed"] = False
                    result["cancelled"] = True
                    result["ok"] = True
                    result["warning"] = "已按请求取消等待信号（注入已完成，需要回滚）"
                    self.log("[inject] ⏹ 已按请求取消等待信号")
                    return result
            result["signal_checks"] = checks
            result["signal_observed"] = any_ok
            if not any_ok:
                result["ok"] = True      # 注入本身成功，但信号未观测到
                result["warning"] = ("注入成功，但 %ds 内未观测到任何声明信号；"
                                     "可能需要叠加压测或延长等待" % int(signal_timeout))
                self.log("[inject] ⚠ " + result["warning"])
                return result

        result["ok"] = True
        self.log("[inject] ✔ %s 完成" % fault_id)
        return result

    # ── 回滚 ──────────────────────────────────────────────────────

    def recover(self, fault_id: str, targets: Optional[List[str]] = None) -> Dict[str, Any]:
        fault = self.registry.get(fault_id)
        if fault is None:
            return {"ok": False, "error": "未知故障场景: %s" % fault_id}

        entries = [a for a in active_faults(fault_id)
                   if targets is None or a.get("targets") == targets]
        if not entries:
            # 没有记录也尝试盲回滚（幂等），保证 recover --all 永远安全
            entries = [{"fault_id": fault_id, "targets": targets,
                        "params": dict(fault.default_params), "recover_hint": {}}]

        out: List[Dict[str, Any]] = []
        for e in entries:
            ctx = FaultContext(fault_id=fault_id,
                               targets=e.get("targets") or [],
                               params=e.get("params") or {},
                               recover_hint=e.get("recover_hint") or {})
            self.log("[recover] %-26s target=%s" % (fault_id, ", ".join(ctx.targets) or "-"))
            try:
                r = fault.recover(ctx) or {}
            except Exception as ex:  # noqa: BLE001
                r = {"ok": False, "error": "%s: %s" % (type(ex).__name__, ex)}
            out.append({"targets": ctx.targets, **r})
            if r.get("ok", True):
                _clear_active(fault_id, e.get("targets") or [])

        ok = all(o.get("ok", True) for o in out)
        return {"ok": ok, "fault_id": fault_id, "results": out}

    def recover_all(self) -> Dict[str, Any]:
        st = _load_state()
        act = list(st.get("active") or [])
        results = []
        for a in act:
            results.append(self.recover(a.get("fault_id"), a.get("targets")))
        # 兜底：清空状态（即使某个回滚抛错也不应残留）
        st["active"] = []
        _save_state(st)
        return {"ok": all(r.get("ok", True) for r in results), "recovered": results}

    # ── 组合：注入 → 压测窗口 → 回滚 ───────────────────────────────

    def run(self, fault_id: str, hold_s: float = 90.0,
            stress_args: Optional[List[str]] = None,
            params: Optional[Dict[str, Any]] = None,
            signal_timeout: float = 75.0,
            recovery_timeout: float = 150.0,
            should_stop: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
        """
        完整演练：注入 → 后台压测 → 观测信号 → 保持 → 停压测 → 回滚 → 校验恢复。

        stress_args 为传给 tools/stress_harness.py 的参数（不含脚本路径）。

        信号观测分两阶段：
          · signal_needs_stress=False 的场景：inject 阶段就能观测（自带可观测后果）
          · signal_needs_stress=True  的场景（如"主库只读"必须有人发写请求）：
            注入时不等待，改为在压测窗口内观测

        should_stop：**协作式取消**回调（人工"停止并回滚"用）。
        取消点覆盖每一处长阻塞（等信号 / 保持期 / 恢复校验），并且 ——
        关键不变式 —— **无论是否被取消，回滚一定会执行**。
        这样"停止演练"永远等价于"停止并强制回滚"，不会留下半注入状态。
        """
        def _stopped() -> bool:
            return bool(should_stop and should_stop())

        def _nap(seconds: float) -> bool:
            """可中断等待；返回 True 表示是被取消打断的。"""
            end = time.time() + max(0.0, seconds)
            while time.time() < end:
                if _stopped():
                    return True
                time.sleep(min(0.5, max(0.0, end - time.time())))
            return _stopped()

        fault = self.registry[fault_id]
        report: Dict[str, Any] = {"fault_id": fault_id, "started_at": time.time()}
        self.log("[run] 场景 %s（%s）" % (fault_id, fault.title))

        if _stopped():
            report.update({"ok": False, "cancelled": True, "error": "已按请求取消（未开始注入）"})
            return report

        inj = self.inject(fault_id, params=params,
                          wait_signals=not fault.signal_needs_stress,
                          signal_timeout=signal_timeout,
                          should_stop=should_stop)
        report["inject"] = inj
        if not inj.get("ok"):
            self.recover_all()
            report["ok"] = False
            return report

        # 若注入阶段就收到取消（等信号时被打断），直接进入回滚
        cancelled = bool(inj.get("cancelled"))

        # ── 后台压测（不阻塞信号观测）───────────────────────────
        stress = None
        if not cancelled and stress_args:
            stress = start_stress_background(stress_args)
        if stress:
            report["stress_cmd"] = stress["cmd"]

        # ── 压测窗口内观测信号 ──────────────────────────────────
        if not cancelled and not inj.get("signal_observed") and fault.signals:
            checks: List[Dict[str, Any]] = []
            any_ok = False
            deadline = time.time() + signal_timeout
            for expr in fault.signals:
                remain = max(5.0, deadline - time.time())
                r = prom_wait(expr, timeout_s=remain, interval_s=5.0,
                              expect="truthy", should_stop=should_stop)
                checks.append(r)
                if r.get("ok"):
                    any_ok = True
                    break
                if r.get("cancelled"):
                    cancelled = True
                    break
            report["signal_checks_under_load"] = checks
            inj["signal_observed"] = any_ok

        report["signals_peak"] = snapshot_signals(fault.signals)

        if not cancelled:
            self.log("[run] 保持故障 %.0fs ..." % hold_s)
            cancelled = _nap(hold_s)
            if cancelled:
                self.log("[run] 收到取消请求 → 提前进入回滚")

        report["signals_end"] = snapshot_signals(fault.signals)

        if stress:
            # 取消时不要在压测宽限期上磨 —— 用户要的是"立刻停"。
            # 正常结束仍走 15s 宽限（让压测自己收尾，报告更完整）。
            report["stress"] = stop_stress_background(
                stress, grace_s=(3.0 if cancelled else 15.0))

        # ★ 不变式：无论是否取消，这里一定执行回滚
        rec = self.recover(fault_id, targets=inj.get("targets"))
        report["recover"] = rec

        # 恢复校验：取消时缩短宽限期（用户要的是"尽快复原"，不是"等到证据漂亮"）
        recov_checks = []
        budget = recovery_timeout if not cancelled else min(30.0, recovery_timeout)
        deadline = time.time() + budget
        for expr in fault.recovered_signals:
            remain = max(5.0, deadline - time.time())
            recov_checks.append(prom_wait(expr, timeout_s=remain, interval_s=5.0,
                                          expect="falsy", should_stop=should_stop))
        report["recovery_checks"] = recov_checks
        report["cancelled"] = cancelled
        report["ok"] = (not cancelled) and bool(rec.get("ok")) and all(
            c.get("ok") for c in recov_checks)
        if cancelled:
            report["error"] = "已按请求取消（已回滚）"
        report["finished_at"] = time.time()
        return report


def snapshot_signals(exprs: List[str]) -> List[Dict[str, Any]]:
    """抓取一组 PromQL 的当前值（用于"故障期间指标确实劣化"的证据）。"""
    out: List[Dict[str, Any]] = []
    for expr in exprs:
        res = prom_query(expr)
        vals = []
        for r in res[:8]:
            m = r.get("metric") or {}
            key = m.get("app_instance") or m.get("instance") or m.get("db_node") or ""
            try:
                v = float(r["value"][1])
            except (KeyError, IndexError, TypeError, ValueError):
                v = None
            vals.append({"labels": {k: m[k] for k in ("app_instance", "instance", "db_node")
                                    if k in m}, "value": v, "key": key})
        out.append({"expr": expr, "samples": vals})
    return out


def run_stress(args: List[str], timeout: int = 900) -> Dict[str, Any]:
    """同步调用压测程序（子进程），返回其 stdout 尾部与退出码。"""
    script = ROOT / "tools" / "stress_harness.py"
    cmd = [sys.executable, str(script)] + args
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "压测超时"}
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    return {"ok": p.returncode == 0, "rc": p.returncode,
            "stdout_tail": out[-1500:], "stderr_tail": err[-600:]}


def start_stress_background(args: List[str]) -> Dict[str, Any]:
    """
    后台启动压测（不阻塞信号观测）。

    返回句柄，供 stop_stress_background 收敛。压测自身按 --duration 结束，
    stop 只是兜底（避免场景提前回滚时压测还在跑）。
    """
    script = ROOT / "tools" / "stress_harness.py"
    cmd = [sys.executable, str(script), "--quiet"] + list(args)
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as e:  # noqa: BLE001
        return {"proc": None, "cmd": " ".join(cmd), "error": "%s: %s" % (type(e).__name__, e)}
    return {"proc": p, "cmd": " ".join(cmd), "started_at": time.time()}


def stop_stress_background(handle: Dict[str, Any], grace_s: float = 15.0) -> Dict[str, Any]:
    """
    等待压测自然结束（宽限期内），否则终止并收集输出。

    `grace_s` 由调用方决定：正常演练结束时给足宽限（报告更完整）；
    收到取消请求时给很短（用户要的是"立刻停"，不是漂亮的压测报告）。
    """
    p = (handle or {}).get("proc")
    if p is None:
        return {"ok": False, "error": (handle or {}).get("error", "压测未启动"),
                "cmd": (handle or {}).get("cmd", "")}
    killed = False
    try:
        p.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        killed = True
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    out_b, err_b = b"", b""
    try:
        out_b, err_b = p.communicate(timeout=10)
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True,
        "rc": p.returncode,
        "killed": killed,
        "cmd": handle.get("cmd", ""),
        "elapsed_s": round(time.time() - handle.get("started_at", time.time()), 1),
        "stdout_tail": out_b.decode("utf-8", "replace")[-2000:],
        "stderr_tail": err_b.decode("utf-8", "replace")[-800:],
    }
