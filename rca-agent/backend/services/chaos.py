# -*- coding: utf-8 -*-
"""
故障注入控制台后端服务（人工手动注入）。

## 为什么需要它

`tools/fault_injector.py` 已经是一个完整的注入器（21 场景 / 信号验证 / 数据驱动回滚），
但它只有 CLI —— 人工演练要记命令、拼参数、盯着终端输出。
本模块把同一个注入引擎（`tools.chaos`）包成 HTTP 服务，供前端控制台调用。

**关键设计：不在容器里重写注入逻辑，而是挂载并复用 `tools/chaos` 同一份代码**
（compose: `../tools:/app/tools:ro`）。这样 CLI 与控制台永远不会出现"两套场景定义"。

## 三个必须处理的工程问题

1. **注入是长阻塞操作**：`Injector.inject` 会等 PromQL 信号成立（最长 75s），
   `db_replica_lag` 的回滚要等复制回放（最长 150s）。
   HTTP 同步请求会超时、前端也无法显示进度 →
   **所有写操作都变成后台任务（job）**，接口立即返回 `job_id`，前端轮询日志。

2. **必须串行**：两个注入同时跑会互相污染信号，回滚也会互相踩。
   → **全局单飞锁（single-flight）**，同一时刻只允许一个任务。

3. **状态文件必须与 CLI 共享**：`.chaos/fault_state.json` 是回滚的唯一依据
   （进程被杀也能复原）。compose 里把 `.chaos/` 读写挂载进来，
   所以控制台看到的"激活故障"和 `fault_injector.py status` 完全一致。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ── 环境开关 ────────────────────────────────────────────────────────────────
ENABLED = os.environ.get("CHAOS_UI_ENABLED", "1") not in ("0", "false", "False")
API_TOKEN = os.environ.get("CHAOS_API_TOKEN", "").strip()

#: 每层的中文名（前端直接用）
LAYER_LABELS = {
    "application": "应用层集群",
    "database": "数据库层集群",
    "runtime": "应用层集群",
    "resource": "资源耗尽",
}

#: 危险等级：按"影响面"给人工操作做提示（不是注入难度）
_RISK = {
    "app_gateway_stop": ("critical", "整个集群外部不可达，所有请求 502/503"),
    "app_kill_replica": ("high", "该副本进程消失，容量 -1"),
    "app_stop_replica": ("high", "优雅下线，容量 -1"),
    "app_oom_kill": ("high", "容器被内核 OOMKill 并重启"),
    "app_pause_replica": ("medium", "该副本挂起（进程在但不响应）"),
    "app_cpu_stress": ("medium", "该副本 CPU 被 cgroup 节流"),
    "app_memory_stress": ("medium", "该副本内存逼近上限"),
    "app_network_delay": ("medium", "该副本网络延迟 300ms"),
    "app_network_loss": ("medium", "该副本网络丢包 40%"),
    "db_primary_readonly": ("critical", "主库写入全部失败（读仍正常）"),
    "db_replica_kill": ("high", "副本节点消失，读容量 -1"),
    "db_replica_io_stop": ("high", "复制中断，副本停止同步"),
    "db_replica_lag": ("medium", "副本数据滞后，读路径返回陈旧数据"),
    "db_conn_saturation": ("medium", "数据库连接被占满"),
    "db_cpu_stress": ("medium", "数据库 CPU 资源被抢占"),
    "db_slow_query_flood": ("medium", "慢查询洪泛，连接被长事务占用"),
    "db_row_lock_hold": ("medium", "行锁持有，写路径阻塞"),
    "db_disk_temp_tables": ("low", "临时表落盘，磁盘 IO 上升"),
    "res_cluster_cpu": ("high", "全部副本 CPU 受限（集群级容量不足）"),
    "res_cluster_memory": ("high", "全部副本内存逼近上限（集群级 OOM 风险）"),
    "res_db_memory": ("high", "数据库排序/临时表内存不足"),
}


# ═════════════════════════════════════════════════════════════════════════════
# 延迟导入 tools.chaos（挂载缺失时服务仍能启动，接口给出明确错误）
# ═════════════════════════════════════════════════════════════════════════════
_IMPORT_ERR = ""


def _chaos():
    """返回 (core, catalog, fault_injector) 三个模块，失败时抛 RuntimeError。"""
    global _IMPORT_ERR
    try:
        from tools.chaos import core as chaos_core          # type: ignore
        from tools.chaos import catalog as chaos_catalog    # type: ignore
        import tools.fault_injector as fault_injector       # type: ignore
        return chaos_core, chaos_catalog, fault_injector
    except Exception as e:  # noqa: BLE001
        _IMPORT_ERR = "%s: %s" % (type(e).__name__, e)
        raise RuntimeError(
            "无法加载故障注入引擎（tools/chaos）。请确认 compose 已把 "
            "../tools 挂载到 /app/tools 且 PYTHONPATH 含 /app。原因：%s" % _IMPORT_ERR
        )


def available() -> Dict[str, Any]:
    """探测注入引擎是否可用（前端用来决定是否禁用按钮）。"""
    if not ENABLED:
        return {"available": False, "enabled": False,
                "reason": "注入接口已被 CHAOS_UI_ENABLED=0 停用"}
    try:
        chaos_core, chaos_catalog, _ = _chaos()
    except RuntimeError as e:
        return {"available": False, "enabled": True, "reason": str(e)}
    return {
        "available": True, "enabled": True, "reason": "",
        "scenarios": len(chaos_catalog.FAULTS),
        "state_file": str(chaos_core.STATE_FILE),
        "state_file_exists": chaos_core.STATE_FILE.is_file(),
        "prometheus": chaos_core.PROM,
        "app_lb": chaos_core.APP_LB,
        "docker_cli": _which_docker(),
        "token_required": bool(API_TOKEN),
    }


def _which_docker() -> str:
    import shutil
    return shutil.which("docker") or ""


# ═════════════════════════════════════════════════════════════════════════════
# 场景目录
# ═════════════════════════════════════════════════════════════════════════════

def scenarios() -> List[Dict[str, Any]]:
    """全部 21 个场景的完整定义（含默认参数与参数说明，供前端渲染表单）。"""
    chaos_core, chaos_catalog, _ = _chaos()
    out: List[Dict[str, Any]] = []
    for fid in sorted(chaos_catalog.FAULTS):
        f = chaos_catalog.FAULTS[fid]
        d = f.to_dict()
        d["layer_label"] = LAYER_LABELS.get(f.layer, f.layer)
        risk, impact = _RISK.get(fid, ("medium", f.blast_radius or "影响面未知"))
        d["risk"] = risk
        d["impact"] = impact
        d["params_spec"] = [
            {"name": k, "default": (f.default_params or {}).get(k),
             "help": (f.params_help or {}).get(k, ""),
             "type": "int" if isinstance((f.default_params or {}).get(k), int) else "str"}
            for k in (f.default_params or {})
        ]
        out.append(d)
    return out


def scenario(fault_id: str) -> Optional[Dict[str, Any]]:
    for s in scenarios():
        if s["id"] == fault_id:
            return s
    return None


def resolve_targets(fault_id: str) -> Dict[str, Any]:
    """
    预览该场景会被注入到哪些容器。

    人工操作最怕"点下去不知道打到谁" —— 所以把 selector 的解析结果先展示出来，
    并由前端把它回传给 `/inject`（`targets` 字段），做到**预览即实际目标**。
    """
    chaos_core, chaos_catalog, _ = _chaos()
    f = chaos_catalog.FAULTS.get(fault_id)
    if not f:
        return {"ok": False, "error": "未知场景: %s" % fault_id}
    inj = chaos_core.Injector(chaos_catalog.FAULTS, verbose=False)
    try:
        targets = inj.resolve_targets(f)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "fault_id": fault_id, "selector": f.selector,
            "targets": targets, "count": len(targets)}


def allowed_targets() -> set:
    """
    允许被当作注入目标的容器白名单。

    前端会把预览到的目标回传给 `/inject`，因此这里必须校验 ——
    否则等于把"向任意容器投递命令"的能力开放给 HTTP 调用方。
    白名单 = 运行中的应用副本 + 网关 + 三个 MySQL 节点。
    """
    chaos_core, _chaos_catalog, _ = _chaos()
    names = set()
    try:
        names |= {r["name"] for r in chaos_core.app_replicas(only_running=False)}
    except Exception:  # noqa: BLE001
        pass
    names.add(chaos_core.APP_GATEWAY)
    names.add(chaos_core.MYSQL_PRIMARY)
    names |= set(chaos_core.MYSQL_REPLICAS)
    return names


# ═════════════════════════════════════════════════════════════════════════════
# 集群状态 / 体检
# ═════════════════════════════════════════════════════════════════════════════

def _signal_state(entry: Dict[str, Any]) -> Dict[str, Any]:
    """
    评估一条激活故障记录的"信号是否还成立"。

    为什么需要：`.chaos/fault_state.json` 是回滚依据，但它只是**文件记录** ——
    进程被杀在"注入完成、回滚未执行"的窗口里，或者一次误操作，都会留下
    **名存实亡**的记录（实测遇到过：网关明明健康，控制台却显示
    "当前激活故障 1 · app_gateway_stop"）。面板若只信文件就会撒谎。

    三种状态（只做提示，**绝不自动删除记录** —— 删了就没法数据驱动回滚了）：
      · active            —— 至少一条声明信号成立，故障确实在生效
      · quiet             —— 信号全部不成立，且该场景不需要叠加压测 → 疑似已失效
      · quiet_under_load  —— 信号全部不成立，但该场景必须叠加压测才可观测 → 无法判定
    """
    chaos_core, chaos_catalog, _ = _chaos()
    fid = str(entry.get("fault_id") or "")
    fault = chaos_catalog.FAULTS.get(fid)
    if fault is None or not fault.signals:
        return {"state": "unknown", "reason": "该场景未声明可判定信号"}
    truthy = []
    errors = []
    for expr in fault.signals:
        try:
            v = chaos_core.prom_value(expr)
        except Exception as e:  # noqa: BLE001
            errors.append("%s: %s" % (type(e).__name__, e))
            continue
        truthy.append(v is not None and v > 0)
    if any(truthy):
        return {"state": "active", "reason": "声明信号已成立，故障确实在生效"}
    if errors:
        return {"state": "unknown", "reason": "信号查询失败：%s" % "; ".join(errors[:2])}
    if fault.signal_needs_stress:
        return {"state": "quiet_under_load",
                "reason": "声明信号均未成立，但该场景需叠加压测才可观测，无法据此判定"}
    return {"state": "quiet",
            "reason": "声明信号全部不成立 → 该记录疑似已失效（残留记录）；"
                      "确认后建议回滚以清掉它"}


def cluster_status() -> Dict[str, Any]:
    """激活故障 + 集群快照（副本 / MySQL / 网关 / Prometheus）。"""
    chaos_core, chaos_catalog, fi = _chaos()
    actives = []
    for a in chaos_core.active_faults():
        item = dict(a)
        try:
            item["signal_state"] = _signal_state(a)
        except Exception as e:  # noqa: BLE001
            item["signal_state"] = {"state": "unknown", "reason": "%s: %s" % (type(e).__name__, e)}
        actives.append(item)

    out: Dict[str, Any] = {
        "active_faults": actives,
        "state_file": str(chaos_core.STATE_FILE),
        "now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "app_replicas": [], "mysql": {}, "gateway": None, "prometheus_up": None,
        "layers": {},
        # 有几条记录"名存实亡" —— 面板据此提示，避免把残留记录当成正在生效的故障
        "stale_count": sum(1 for a in actives
                           if (a.get("signal_state") or {}).get("state") == "quiet"),
    }

    try:
        out["app_replicas"] = chaos_core.app_replicas(only_running=False)
    except Exception as e:  # noqa: BLE001
        out["app_replicas_error"] = str(e)
    try:
        out["mysql"] = fi.mysql_snapshot()
    except Exception as e:  # noqa: BLE001
        out["mysql_error"] = str(e)
    try:
        out["gateway"] = chaos_core.container_state(chaos_core.APP_GATEWAY)
    except Exception as e:  # noqa: BLE001
        out["gateway_error"] = str(e)

    # Prometheus 目标健康数：up == 1 的数量（顺带给出总量）
    try:
        res = chaos_core.prom_query("up")
        ups = [r for r in res if float(r.get("value", [0, "0"])[1]) == 1]
        out["prometheus_up"] = {"up": len(ups), "total": len(res)}
    except Exception as e:  # noqa: BLE001
        out["prometheus_error"] = str(e)

    # 激活故障按层归类（给前端做角标）
    for a in out["active_faults"]:
        lay = a.get("layer") or "unknown"
        out["layers"][lay] = out["layers"].get(lay, 0) + 1

    # 注入引擎可用性
    out["engine"] = available()
    return out


def doctor() -> Dict[str, Any]:
    """
    环境体检（复用 `fault_injector.py doctor` 的检查集，返回结构化结果）。

    返回结构：{passed, total, ok_count, checks: [{name, ok, detail}]}
    """
    chaos_core, chaos_catalog, fi = _chaos()
    checks = fi.doctor_checks()
    ok = sum(1 for c in checks if c["ok"])
    return {"passed": ok == len(checks), "ok_count": ok, "total": len(checks),
            "checks": checks}


def signal_snapshot(fault_id: str) -> Dict[str, Any]:
    """
    实时求值某场景声明的全部 PromQL 信号（用于面板上的"信号灯"）。

    注入前应全为 false —— 这正是"环境干净"的证据；
    注入后至少一条 true 才算注入真的生效。
    """
    chaos_core, chaos_catalog, _ = _chaos()
    f = chaos_catalog.FAULTS.get(fault_id)
    if not f:
        return {"ok": False, "error": "未知场景: %s" % fault_id}
    rows = []
    for kind, exprs in (("inject", f.signals), ("recover", f.recovered_signals)):
        for e in exprs:
            v = None
            err = ""
            try:
                v = chaos_core.prom_value(e)
            except Exception as ex:  # noqa: BLE001
                err = str(ex)
            rows.append({"kind": kind, "expr": e, "value": v,
                         "truthy": (v is not None and v > 0), "error": err})
    return {"ok": True, "fault_id": fault_id, "signals": rows}


# ═════════════════════════════════════════════════════════════════════════════
# 异步任务引擎（单飞锁 + 日志缓冲 + 轮询）
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class Job:
    id: str
    kind: str                       # inject | recover | recover_all | cleanup | doctor | verify
    fault_id: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    targets: List[str] = field(default_factory=list)   # 注入目标（前端预览确认过的）
    status: str = "queued"          # queued | running | done | failed | cancelled
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    logs: List[Dict[str, Any]] = field(default_factory=list)
    result: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    step: str = ""                  # 当前阶段（给进度条用）
    #: 协作式取消标志。置 True 后，任务在**下一个取消点**停止并回滚。
    #: 用 threading.Event 是为了跨线程可见性（普通 bool 在 CPython 下虽然也可见，
    #: 但 Event 语义更明确，也让"取消请求"本身可被 wait 等待）。
    cancel_event: Any = field(default_factory=threading.Event)

    @property
    def cancel_requested(self) -> bool:
        return bool(self.cancel_event and self.cancel_event.is_set())

    def log(self, msg: str, level: str = "info") -> None:
        self.logs.append({"t": round(time.time() - self.started_at, 2),
                          "level": level, "msg": msg})
        if len(self.logs) > 400:
            del self.logs[:100]

    def to_dict(self, with_logs: bool = True) -> Dict[str, Any]:
        d = {
            "id": self.id, "kind": self.kind, "fault_id": self.fault_id,
            "params": self.params, "targets": self.targets,
            "status": self.status, "step": self.step,
            "cancel_requested": self.cancel_requested,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "duration_s": round((self.finished_at or time.time()) - self.started_at, 1),
            "result": self.result, "error": self.error, "log_count": len(self.logs),
        }
        if with_logs:
            d["logs"] = self.logs
        return d


class JobManager:
    """全局单飞的任务管理器：同一时刻只跑一个注入/回滚任务。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._current: Optional[str] = None

    # ── 查询 ──────────────────────────────────────────────────────
    def current(self) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(self._current) if self._current else None

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock:
            ids = list(reversed(self._order))[:limit]
            return [self._jobs[i].to_dict(with_logs=False) for i in ids]

    def busy(self) -> bool:
        with self._lock:
            cur = self._jobs.get(self._current) if self._current else None
            return bool(cur and cur.status in ("queued", "running"))

    def force_abort(self, reason: str = "") -> Dict[str, Any]:
        """
        强制把当前任务标记为失败并**释放单飞锁**。

        为什么需要这个"危险出口"：协作式取消依赖任务自己在取消点检查
        `cancel_requested`。如果任务卡在一个不可中断的调用里（实测遇到过
        `docker exec` 扫 /proc 慢到 75s），协作式取消就拿它没办法 ——
        而此时**单飞锁仍被占着**，所有操作都返回 409 busy。
        结果是最需要安全网的时刻，安全网自己把门锁上了。

        代价必须说清楚：worker 线程可能**仍在运行**，所以强制解除后
        新的任务可能与它短暂并发。因此这个接口只作为最后的出口，
        响应里会带 `warning`，前端也要二次确认。
        """
        with self._lock:
            cur = self._jobs.get(self._current) if self._current else None
            if cur is None or cur.status not in ("queued", "running"):
                return {"ok": True, "aborted": False, "note": "没有占用中的任务"}
            cur.cancel_event.set()
            cur.status = "failed"
            cur.error = reason or "被人工强制解除占用（worker 可能仍在收尾）"
            cur.finished_at = time.time()
            aborted = cur.to_dict(with_logs=False)
            self._current = None
        return {"ok": True, "aborted": True, "job": aborted,
                "warning": "已强制释放占用。原任务的线程可能仍在收尾，"
                           "请稍后确认环境状态（health / doctor）再继续操作。"}

    # ── 提交 ──────────────────────────────────────────────────────
    def submit(self, kind: str, fn: Callable[[Job], Dict[str, Any]],
               fault_id: str = "", params: Optional[Dict[str, Any]] = None,
               targets: Optional[List[str]] = None
               ) -> Dict[str, Any]:
        if not ENABLED:
            return {"ok": False, "error": "注入接口已被 CHAOS_UI_ENABLED=0 停用"}
        with self._lock:
            cur = self._jobs.get(self._current) if self._current else None
            if cur and cur.status in ("queued", "running"):
                return {"ok": False, "busy": True, "current_job": cur.to_dict(with_logs=False),
                        "error": "已有任务在运行（%s %s），请等它结束或先「紧急全部回滚」"
                                 % (cur.kind, cur.fault_id)}
            job = Job(id="job_%d_%s" % (int(time.time() * 1000) % 1000000, kind),
                      kind=kind, fault_id=fault_id, params=params or {},
                      targets=list(targets or []))
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._current = job.id
            if len(self._order) > 200:
                for old in self._order[:-100]:
                    self._jobs.pop(old, None)
                self._order = self._order[-100:]

        def _run() -> None:
            job.status = "running"
            try:
                job.result = fn(job) or {}
                if job.result.get("cancelled") or job.result.get("stopped_early"):
                    # 用户主动停止 ≠ 失败：给一个独立状态，前端不用红字吓人
                    job.status = "cancelled"
                else:
                    job.status = "done" if job.result.get("ok", True) else "failed"
                    if not job.result.get("ok", True):
                        job.error = job.result.get("error") or "任务返回失败"
            except Exception as e:  # noqa: BLE001
                job.status = "failed"
                job.error = "%s: %s" % (type(e).__name__, e)
                job.log(traceback.format_exc()[-1500:], "error")
            finally:
                job.finished_at = time.time()
                job.step = {"done": "完成", "cancelled": "已停止并回滚"}.get(job.status, "失败")

        threading.Thread(target=_run, name="chaos-" + job.id, daemon=True).start()
        return {"ok": True, "job_id": job.id, "job": job.to_dict(with_logs=False)}

    # ── 启动前把状态标成 cancelled（紧急停止用）────────────────────
    def request_cancel(self) -> Dict[str, Any]:
        """
        请求停止当前任务。

        实现为**协作式取消**：置位 `cancel_event`，任务在下一个取消点
        （场景边界 / 等信号 / 保持期 / 恢复校验）停止，并**必定执行回滚**。

        为什么不强杀线程：注入动作是"有副作用的多步操作"（改 cgroup、
        起后台循环、改全局变量、置只读）。在任意一行强杀会留下**半注入状态**，
        而回滚依赖这些步骤是否被记录 —— 半注入比"多等几十秒"危险得多。
        所以这里选择"快速收敛到可回滚点"，并把取消点铺得足够密（每处 ≤5s）。
        """
        with self._lock:
            cur = self._jobs.get(self._current) if self._current else None
        if not cur or cur.status not in ("queued", "running"):
            return {"ok": False, "error": "当前没有运行中的任务",
                    "busy": self.busy()}
        already = cur.cancel_requested
        cur.cancel_event.set()
        if not already:
            cur.log("⏹ 收到停止请求：将在当前步骤的取消点停止，并**自动回滚**所有注入",
                    "warn")
        return {"ok": True, "already_requested": already,
                "job": cur.to_dict(with_logs=False),
                "note": "已请求停止；任务会在 ≤5s 内停止并自动回滚（无需再点别的按钮）"}


MANAGER = JobManager()

#: 「批量演练」的**代表性子集**（界面上不勾"全部"时用它）。
#:
#: 为什么要有它：全套 21 个场景是一键 20–40 分钟的破坏性操作，而界面上只是一个按钮。
#: 子集按"层次 × 类别"选取，覆盖注入/回滚链路的所有关键路径
#: （副本丢失、资源节流、入口不可用、行锁、复制延迟、配置漂移、落盘资源），
#: 十几分钟即可回答"注入与回滚链路是否健康"，足够日常自检。
REPRESENTATIVE_SUBSET: tuple = (
    "app_kill_replica",        # 应用层：副本丢失
    "app_cpu_stress",          # 应用层：资源节流
    "app_gateway_stop",        # 应用层：入口不可用
    "db_row_lock_hold",        # 数据库层：锁竞争
    "db_replica_lag",          # 数据库层：复制延迟
    "db_primary_readonly",     # 数据库层：配置漂移（写路径失败但探活正常）
    "res_db_memory",           # 跨层资源：排序落盘
)


# ═════════════════════════════════════════════════════════════════════════════
# 压测台（tools/stress_harness.py 的界面外壳）
# ═════════════════════════════════════════════════════════════════════════════
#
# 设计要点（都是安全考量，不是洁癖）：
#  1. **目标必须是白名单枚举，不能是自由 URL**。否则这个接口就成了"对任意主机
#     发起高并发请求"的工具 —— 一个内部运维控制台不该具备这种能力。
#  2. **参数在服务端夹紧**（并发/时长/总请求数上限），不信任前端传值。
#  3. **危险组合要二次确认**（打满 max_connections、高并发），与故障注入同一套机制。
#  4. 复用全局单飞锁：压测与注入不会同时跑（注入完成后故障仍然生效，
#     所以"先注入 → 再压测 → 再回滚"这个正确用法完全不受影响）。

#: 压测可选的执行目标（白名单）
def stress_targets() -> List[Dict[str, Any]]:
    chaos_core, _catalog, _fi = _chaos()
    reps = []
    try:
        reps = [r["name"] for r in chaos_core.app_replicas(only_running=True)]
    except Exception:  # noqa: BLE001
        pass
    out: List[Dict[str, Any]] = [
        {"id": "gateway", "label": "应用网关（推荐 · 走负载均衡）",
         "url": chaos_core.APP_LB, "kind": "http"},
        {"id": "mysql", "label": "MySQL 主库（直连数据库）",
         "url": "", "kind": "mysql"},
    ]
    for i, name in enumerate(reps, 1):
        ip = ""
        try:
            ip = chaos_core.container_ip(name)
        except Exception:  # noqa: BLE001
            pass
        if ip:
            out.append({"id": "replica:%s" % name, "label": "应用副本 %s（单点直连）" % name,
                        "url": "http://%s:8080" % ip, "kind": "http"})
    return out


#: 参数上限（服务端夹紧；前端只作提示）
STRESS_LIMITS = {
    "concurrency": {"min": 1, "max": 256, "default": 16},
    "duration": {"min": 1, "max": 900, "default": 30},
    "requests": {"min": 0, "max": 2_000_000, "default": 0},
    "timeout": {"min": 1, "max": 60, "default": 10},
    "ramp_up": {"min": 0, "max": 120, "default": 0},
    "mysql_hold_connections": {"min": 0, "max": 64, "default": 0},
}

#: 一键预设（"基线"就是文档里那份 stress_baseline.json 的配方，可直接复现）
STRESS_PRESETS = [
    {"id": "light", "label": "轻量探活（16 × 30s）",
     "params": {"concurrency": 16, "duration": 30, "ramp_up": 0}},
    {"id": "baseline", "label": "基线（32 × 45s，与文档基线一致）",
     "params": {"concurrency": 32, "duration": 45, "ramp_up": 0}},
    {"id": "saturate", "label": "压满入口（128 × 60s + 10s 爬坡）",
     "params": {"concurrency": 128, "duration": 60, "ramp_up": 10}, "confirm": True},
    {"id": "conn_exhaust", "label": "打满 max_connections（4×60 连接保持）",
     "params": {"mode": "mysql", "concurrency": 4, "duration": 60,
                "mysql_hold_connections": 60}, "confirm": True},
]

_STRESS_DIR = ".chaos"


def stress_options() -> Dict[str, Any]:
    """压测表单的**唯一事实来源**：模式/目标/默认值/上限/预设/历史。"""
    return {
        "modes": [
            {"id": "http", "label": "HTTP — 打应用入口"},
            {"id": "mysql", "label": "MySQL — 直连数据库"},
            {"id": "mixed", "label": "mixed — 两者各半"},
        ],
        "targets": stress_targets(),
        "limits": STRESS_LIMITS,
        "defaults": {
            "mode": "http", "target": "gateway", "path": "/txn", "method": "POST",
            "body": '{"customer_id": 1, "amount": 100, "status": "SETTLED"}',
            "concurrency": 16, "duration": 30, "requests": 0, "timeout": 10,
            "ramp_up": 0, "label": "",
            "mysql_query": "SELECT COUNT(*) FROM t_txn",
            "mysql_hold_connections": 0,
        },
        "presets": STRESS_PRESETS,
        "history": stress_history(limit=8),
    }


def stress_history(limit: int = 20) -> List[Dict[str, Any]]:
    """最近若干次压测的**紧凑摘要**（从 .chaos/stress_*.json 里读回）。"""
    chaos_core, _catalog, _fi = _chaos()
    root = chaos_core.CHAOS_DIR
    out: List[Dict[str, Any]] = []
    try:
        files = sorted(root.glob("stress_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:  # noqa: BLE001
        return out
    for f in files[:limit]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append(_stress_summary(d, source=f.name))
        except Exception:  # noqa: BLE001
            continue
    return out


def _stress_summary(d: Dict[str, Any], source: str = "") -> Dict[str, Any]:
    req = d.get("requests") or {}
    lat = d.get("latency_ms") or {}
    thr = d.get("throughput") or {}
    return {
        "source": source,
        "label": d.get("label") or "",
        "mode": d.get("mode"),
        "started_at": d.get("started_at"),
        "wall_seconds": d.get("wall_seconds"),
        "concurrency": d.get("concurrency"),
        "target": d.get("target"),
        "requests": req,
        "latency_ms": lat,
        "throughput": thr,
        "status_codes": d.get("status_codes") or {},
        "error_kinds": d.get("error_kinds") or {},
        "time_series": d.get("time_series") or [],
    }


def _clamp(name: str, value: Any) -> Any:
    spec = STRESS_LIMITS.get(name)
    if not spec:
        return value
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        v = spec["default"]
    return max(spec["min"], min(spec["max"], v))


def stress_validate(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    校验 + 夹紧压测参数，返回 (params, error, need_confirm, reason)。

    目标只允许白名单 id —— **不接受任意 URL**（见本节顶部的安全说明）。
    """
    targets = {t["id"]: t for t in stress_targets()}
    tid = str(payload.get("target") or "gateway")
    if tid not in targets:
        return {}, "目标不在允许列表内：%s（允许：%s）" % (tid, sorted(targets)), False, ""
    tgt = targets[tid]

    p: Dict[str, Any] = {
        "mode": str(payload.get("mode") or "http"),
        "target": tid,
        "target_label": tgt["label"],
        "target_url": tgt["url"],
        "path": str(payload.get("path") or "/txn"),
        "method": str(payload.get("method") or "POST").upper(),
        "body": payload.get("body"),
        "label": str(payload.get("label") or "")[:40],
        "mysql_query": str(payload.get("mysql_query") or "SELECT COUNT(*) FROM t_txn"),
    }
    if p["mode"] not in ("http", "mysql", "mixed"):
        return {}, "未知模式：%s" % p["mode"], False, ""
    if tgt["kind"] == "mysql" and p["mode"] == "http":
        p["mode"] = "mysql"          # 选了数据库目标却用 http 模式 → 自动纠正
    for k in ("concurrency", "duration", "requests", "timeout", "ramp_up",
              "mysql_hold_connections"):
        p[k] = _clamp(k, payload.get(k, STRESS_LIMITS[k]["default"]))

    # 危险组合：高并发 / 大量连接保持 / 长时长
    need, why = False, ""
    if p["concurrency"] > 96:
        need, why = True, "并发 %d 较高，会显著挤压应用副本（cpus=0.5 / 256MB）" % p["concurrency"]
    if p["mysql_hold_connections"] > 0:
        need, why = True, "连接保持 %d 条/线程，会逼近 max_connections=200，可能让正常业务连不上库" % p[
            "mysql_hold_connections"]
    if p["duration"] > 180:
        need, why = True, "持续 %ds 超过 3 分钟，压测窗口内不宜做其他演示" % p["duration"]
    return p, "", need, why


def _precheck_stress_target(mode: str, url: str, mhost: str, mport: int) -> tuple:
    """
    压测**开跑前**先确认目标真的可达，不可达就当场拒绝并说明原因。

    为什么要这道前置：harness 会把"连不上"记成每一次请求的错误，
    于是 842 次连接失败在报告里长得像"压测把系统打挂了"（延迟全 0 是唯一线索）。
    实测就是这么发生的：控制台发的 mysql 压测因为目标写成 127.0.0.1（容器内不是 MySQL）
    而 842/842 全失败 —— 一个配置错误被误读成一次性能结论。
    宁可开跑前花 5 秒探测并明确报错。
    """
    if mode in ("mysql", "mixed"):
        try:
            with socket.create_connection((mhost, mport), timeout=5):
                pass
        except Exception as e:  # noqa: BLE001
            return False, ("MySQL 目标不可达 %s:%s（%s）。容器内跑压测时地址要用 compose "
                           "服务名（如 mysql / cc-mysql-core），127.0.0.1 指的是容器自己。"
                           % (mhost, mport, str(e)[:80]))
    if mode in ("http", "mixed"):
        import urllib.error
        import urllib.request
        try:
            urllib.request.urlopen(url.rstrip("/") + "/health", timeout=5).close()
        except urllib.error.HTTPError:
            pass                     # 有 HTTP 响应就说明可达（404/500 也算可达）
        except Exception as e:  # noqa: BLE001
            return False, "压测目标不可达 %s（%s）" % (url, str(e)[:80])
    return True, ""


def job_stress(job: Job) -> Dict[str, Any]:
    """执行一次压测（流式把 harness 的进度写进任务日志）。"""
    chaos_core, _catalog, _fi = _chaos()
    p = job.params or {}
    if job.cancel_requested:
        return {"ok": False, "cancelled": True, "error": "已按请求取消（未开始压测）"}

    # 输出文件按时间戳命名，便于历史回看（stress_history 读的就是这些）
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    label = (p.get("label") or p.get("mode") or "run").replace("/", "_")[:24]
    out_path = chaos_core.CHAOS_DIR / ("stress_%s_%s.json" % (stamp, label))

    argv: List[str] = [
        sys.executable, str(Path(chaos_core.ROOT) / "tools" / "stress_harness.py"),
        "--mode", str(p.get("mode") or "http"),
        "--concurrency", str(p.get("concurrency")),
        "--duration", str(p.get("duration")),
        "--requests", str(p.get("requests")),
        "--timeout", str(p.get("timeout")),
        "--ramp-up", str(p.get("ramp_up")),
        "--label", str(p.get("label") or stamp),
        "--json-out", str(out_path),
    ]
    if p.get("mode") in ("http", "mixed"):
        argv += ["--url", str(p.get("target_url") or chaos_core.APP_LB),
                 "--path", str(p.get("path") or "/txn"),
                 "--method", str(p.get("method") or "POST")]
        if p.get("body"):
            argv += ["--body", str(p["body"])]
    if p.get("mode") in ("mysql", "mixed"):
        # ★ 必须把连接参数**传下去**：harness 的 --mysql-host 默认是 127.0.0.1，
        # 而它是被本服务用 subprocess 拉起、跑在**后端容器内**的 ——
        # 容器里的 127.0.0.1 是它自己，不是 MySQL。实测后果：一次 mysql 压测
        # 842/842 全部 mysql_connect_error、延迟 0（立刻连不上），
        # 界面看起来像"压测把库打挂了"，实际是**连接目标写错**。
        # 连接参数沿用本服务自己的那份环境变量（DB_HOST 默认就是 compose 服务别名），
        # 这是唯一的事实源，不要再抄一份默认值。
        argv += ["--mysql-query", str(p.get("mysql_query") or "SELECT COUNT(*) FROM t_txn"),
                 "--mysql-host", os.environ.get("DB_HOST", "mysql"),
                 "--mysql-port", str(os.environ.get("DB_PORT", "3306")),
                 "--mysql-user", os.environ.get("DB_USER", "appuser"),
                 "--mysql-password", os.environ.get("DB_PASSWORD", "apppass"),
                 "--mysql-db", os.environ.get("DB_NAME", "creditcard")]
        if p.get("mysql_hold_connections"):
            argv += ["--mysql-hold-connections", str(p["mysql_hold_connections"])]

    # ★ 开跑前探测目标（见 _precheck_stress_target 的说明）
    _mhost = os.environ.get("DB_HOST", "mysql")
    _mport = int(os.environ.get("DB_PORT", "3306"))
    _url = str(p.get("target_url") or chaos_core.APP_LB)
    ok_t, why_t = _precheck_stress_target(str(p.get("mode") or "http"), _url, _mhost, _mport)
    if not ok_t:
        job.log("✘ 压测未开始：%s" % why_t)
        return {"ok": False, "error": "目标不可达，已拒绝开跑：%s" % why_t,
                "precheck": {"mode": p.get("mode"), "target": _url,
                             "mysql": "%s:%s" % (_mhost, _mport)}}
    job.log("前置探测：目标可达 ✅（%s）"
            % ("MySQL %s:%s" % (_mhost, _mport)
               if p.get("mode") in ("mysql", "mixed") else _url))

    job.step = "压测中"
    job.log("目标：%s（%s）" % (p.get("target_label"), p.get("target_url") or "直连 MySQL"))
    job.log("参数：mode=%s concurrency=%s duration=%ss ramp_up=%ss requests=%s"
            % (p.get("mode"), p.get("concurrency"), p.get("duration"),
               p.get("ramp_up"), p.get("requests")))
    job.log("命令：%s" % " ".join(argv[2:]))
    job.log("开始压测 …（可随时点「⏹ 停止并回滚」中止）")

    t0 = time.time()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    tail: List[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            del tail[:-40]
            job.log(line)
            if job.cancel_requested:
                job.log("⏹ 收到停止请求 → 终止压测进程", "warn")
                proc.terminate()
                break
        rc = proc.wait(timeout=30)
    except Exception as e:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": "压测执行失败：%s: %s" % (type(e).__name__, e)}
    finally:
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass

    cancelled = job.cancel_requested
    report: Dict[str, Any] = {}
    try:
        if out_path.is_file():
            report = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        job.log("报告解析失败：%s" % e, "warn")

    if report:
        summary = _stress_summary(report, source=out_path.name)
        req, lat, thr = summary["requests"], summary["latency_ms"], summary["throughput"]
        job.log("结果：%s 请求 / %s 失败 / %s rps / p50 %sms · p95 %sms · p99 %sms"
                % (req.get("total"), req.get("error"), round(thr.get("requests_per_sec") or 0, 1),
                   lat.get("p50"), lat.get("p95"), lat.get("p99")),
                "success" if not req.get("error") else "warn")
        job.step = "完成（%.1fs）" % (time.time() - t0)
        return {"ok": not cancelled, "cancelled": cancelled,
                "report_file": out_path.name, "summary": summary, "report": report,
                "error": "" if not cancelled else "已按请求停止压测（已终止进程）"}

    job.step = "未产出报告"
    return {"ok": False, "cancelled": cancelled, "returncode": rc,
            "error": "压测未产出报告（exit=%s）；末尾输出：%s" % (rc, " | ".join(tail[-6:]))}


# ═════════════════════════════════════════════════════════════════════════════
# 具体任务实现
# ═════════════════════════════════════════════════════════════════════════════

def _active_ids(chaos_core) -> List[str]:
    return sorted({a.get("fault_id") for a in chaos_core.active_faults() if a.get("fault_id")})


def job_inject(job: Job) -> Dict[str, Any]:
    chaos_core, chaos_catalog, _ = _chaos()
    fid = job.fault_id
    f = chaos_catalog.FAULTS.get(fid)
    if not f:
        return {"ok": False, "error": "未知场景: %s" % fid}

    if job.cancel_requested:
        return {"ok": False, "cancelled": True, "error": "已按请求取消（未开始注入）"}

    # ── 安全护栏：不允许在已有激活故障时叠加注入 ────────────────────
    act = _active_ids(chaos_core)
    if act:
        return {"ok": False, "active_faults": act,
                "error": "当前已有激活故障 %s；请先回滚（或点「全部回滚」）再注入新故障，"
                         "否则信号会互相污染、RCA 结论不可信" % ", ".join(act)}

    inj = chaos_core.Injector(chaos_catalog.FAULTS, verbose=False)
    job.step = "解析注入目标"
    # 「预览即实际目标」：前端把预览过的目标回传，就用它注入，
    # 避免 roundrobin 场景出现"卡片说 A、日志说 B"的不一致。
    targets = list(job.targets) or inj.resolve_targets(f)
    job.log("场景：%s（%s）" % (f.title, f.layer))
    job.log("目标容器：%s%s" % (", ".join(targets) or "(未匹配到)",
                              "（已按预览目标注入）" if job.targets else "（按 selector 解析）"))
    job.log("参数：%s" % (job.params or f.default_params))
    if not targets:
        return {"ok": False, "error": "未解析到任何目标容器，请检查集群是否在运行"}

    # 把 Injector 的 stdout 也引到任务日志里（人工操作需要看到过程）
    job.step = "注入中"
    job.log("开始注入 …（会在容器内执行，随后等待 PromQL 信号成立，最长约 75s）")
    t0 = time.time()
    # verbose=False 避免污染后端 stdout；但把关键信息补进 job 日志
    res = inj.inject(fid, params=job.params, targets=targets,
                     wait_signals=True, signal_timeout=75.0,
                     should_stop=lambda: job.cancel_requested)
    job.log("注入阶段耗时 %.1fs" % (time.time() - t0))

    checks = res.get("signal_checks") or []
    for c in checks:
        job.log("信号 %s → %s（%s）"
                % ("✅ 成立" if c.get("ok") else "❌ 未成立", c.get("expr"),
                   c.get("last_value", c.get("error", ""))),
                "info" if c.get("ok") else "warn")
    if res.get("signal_observed"):
        job.step = "注入成功（信号已验证）"
        job.log("注入成功，且声明信号已被观测到", "success")
    elif res.get("ok"):
        job.step = "注入成功（信号未观测到）"
        job.log(res.get("warning") or "注入成功但未观测到信号", "warn")
    else:
        job.step = "注入失败"
        job.log("注入失败：%s" % res.get("error"), "error")
    return res


def job_recover(job: Job) -> Dict[str, Any]:
    chaos_core, chaos_catalog, _ = _chaos()
    inj = chaos_core.Injector(chaos_catalog.FAULTS, verbose=False)
    fid = job.fault_id

    if fid in ("", "all", "__all__"):
        job.step = "全部回滚"
        job.log("回滚全部激活故障：%s" % (", ".join(_active_ids(chaos_core)) or "(无)"))
        res = inj.recover_all()
        job.log("recover_all 完成：ok=%s" % res.get("ok"), "success" if res.get("ok") else "error")
        return res

    job.step = "回滚 %s" % fid
    job.log("回滚 %s（数据驱动：读 .chaos/fault_state.json 的注入记录）" % fid)
    res = inj.recover(fid)
    job.log("回滚完成：ok=%s" % res.get("ok"), "success" if res.get("ok") else "error")
    return res


def job_cleanup(job: Job) -> Dict[str, Any]:
    """
    紧急清理：不依赖状态文件的兜底回滚。

    复用 `fault_injector.py cleanup` 的实现（扫容器内残留的 stress-ng / 网络规则 /
    只读配置 / 长事务会话），因为它才是"进程被杀也能复原"的那条路径。
    """
    _chaos_core, _chaos_catalog, fi = _chaos()
    job.step = "紧急清理残留"
    job.log("开始无状态清理（不依赖 fault_state.json）…")
    actions = fi.cleanup_all()
    for a in actions:
        job.log("%s %s" % ("✅" if a.get("ok") else "❌", a.get("desc") or a.get("action")),
                "info" if a.get("ok") else "warn")
    job.step = "清理完成"
    ok = all(a.get("ok") for a in actions) if actions else True
    return {"ok": ok, "actions": actions,
            "error": "" if ok else "部分清理动作失败，请查看日志"}


def job_doctor(job: Job) -> Dict[str, Any]:
    _chaos_core, _chaos_catalog, fi = _chaos()
    job.step = "环境体检"
    checks = fi.doctor_checks()
    for c in checks:
        job.log("%s %s%s" % ("✅" if c["ok"] else "❌", c["name"],
                             "" if c["ok"] else "  ← %s" % c.get("detail", "")),
                "info" if c["ok"] else "warn")
    ok = sum(1 for c in checks if c["ok"])
    job.step = "体检完成 %d/%d" % (ok, len(checks))
    return {"ok": ok == len(checks), "ok_count": ok, "total": len(checks),
            "checks": checks}


def job_verify(job: Job) -> Dict[str, Any]:
    """
    批量自动演练（注入 → 等信号 → 保持 → 回滚 → 校验恢复）。

    这是"机器跑"的路径；人工路径就是上面的单次 inject/recover。
    默认跑全部 21 个场景，耗时约 20–40 分钟 —— 因此：
      · 支持**协作式取消**（前端「⏹ 停止并回滚」）：停止后自动做一次 cleanup，
        保证不留残留；
      · 结束（无论正常/取消/异常）都再跑一次 `cleanup_all()`，
        因为批量演练会在 6 个容器间反复留痕，单靠 recover_all 不够干净。
    """
    _chaos_core, _chaos_catalog, fi = _chaos()
    ids = job.params.get("fault_ids") or []
    hold = float(job.params.get("hold") or 20)
    # ⚠ 压测强度必须**复现文档里的验证配方**（`--duration 20 --concurrency 12`）。
    # 踩过的坑：控制台一开始用了更重的默认值 `60s×24`，结果 21 场景里
    # `db_slow_query_flood` / `res_db_memory` 恢复校验必然超时 ——
    # 因为它们的 recovered_signals 是 `rate(mysql_global_status_slow_queries[1m]) > 0`，
    # 而重压下**副本自身也会持续产生慢查询**（last 样本落在 db_node=replica-1），
    # 基线不为 0 → 这条 `> 0` 永远不可能变假。
    # 结论：这不是注入失败，而是"用不同负载跑出的结果无法与文档结论对比"。
    stress = job.params.get("stress") or "--duration 20 --concurrency 12"
    job.step = "批量演练"
    job.log("批量演练：场景=%s hold=%ss 压测=%s（文档验证配方）"
            % (ids or "全部 21 个", hold, stress))
    job.log("提示：随时可点「⏹ 停止并回滚」中止；停止后会自动回滚并清理残留")

    res = fi.verify_scenarios(
        fault_ids=ids or None, hold=hold, stress=stress,
        # recovery_timeout 必须够长：部分场景的恢复校验是 `rate(...[1m])`，
        # 需要等这 1 分钟窗口把故障期数据"排出"后才会变假。
        # 给 80s 会偶发超时（曾误判为"回滚失败"），统一到 110s。
        signal_timeout=50.0, recovery_timeout=110.0, cooldown=8.0,
        progress=lambda m: job.log(m),
        should_stop=lambda: job.cancel_requested)
    # 控制台发起的（子集）演练也走独立产物路径，避免覆盖 CLI 全量证据
    job.log("演练产物: %s" % ("fault_verify_report_subset.json"
                             if res.get("scope") == "subset"
                             else "fault_verify_report.json"))

    stopped = bool(res.get("stopped_early"))
    if stopped:
        job.step = "已停止，正在强制回滚"
        job.log("⏹ 演练已按请求停止（%s）→ 执行强制清理" % res.get("stopped_at"), "warn")

    # 收尾清理：批量演练反复在 6 个容器留痕，recover_all 不足以清干净
    job.step = "收尾清理残留"
    job.log("收尾清理（扫压测进程 / 注入循环 / 残留会话 / 全局变量 / 角色 / cgroup 上限）…")
    try:
        actions = fi.cleanup_all(progress=lambda m: job.log(m))
        bad = [a for a in actions if not a.get("ok")]
        job.log("收尾清理完成：%d 项，失败 %d 项" % (len(actions), len(bad)),
                "success" if not bad else "warn")
    except Exception as e:  # noqa: BLE001
        job.log("收尾清理异常：%s: %s" % (type(e).__name__, e), "error")

    passed, total = res.get("passed"), res.get("total")
    attempted = res.get("attempted", total)
    job.step = ("已停止并回滚（完成 %s/%s）" % (attempted, total)) if stopped \
        else ("演练完成 %s/%s" % (passed, attempted))
    return {
        "ok": (not stopped) and passed == attempted,
        "cancelled": stopped,
        "stopped_early": stopped,
        "stopped_at": res.get("stopped_at", ""),
        "passed": passed, "total": total, "attempted": attempted,
        "failed": res.get("failed"), "results": res.get("results"),
        "error": "" if (not stopped) and passed == attempted
                 else ("已按请求停止（已回滚并清理）" if stopped else "存在未通过场景"),
    }
