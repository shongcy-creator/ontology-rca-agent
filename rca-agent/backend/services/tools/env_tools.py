# -*- coding: utf-8 -*-
"""
运行环境层工具 — Docker 容器观测。

通过挂载的 /var/run/docker.sock 调用 docker CLI（只读子命令）。
安全：子命令白名单（ps/inspect/logs/stats/version），无 shell=True，参数逐个传参。
"""
from __future__ import annotations
import asyncio
import json
import shutil
from typing import Any, Dict, List, Optional

from .base import ToolResult, ToolSpec, ToolSecurityError

# 允许的只读子命令
ALLOWED_SUBCOMMANDS = {"ps", "inspect", "logs", "stats", "version", "info"}

# 容器名白名单字符（防止参数注入）
_SAFE_NAME = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,80}$")


def _docker_bin() -> Optional[str]:
    return shutil.which("docker") or shutil.which("docker.exe")


async def _run_docker(args: List[str], timeout: float = 15.0) -> tuple[int, str, str]:
    """执行 docker 子命令（无 shell，参数分离）。"""
    if not args or args[0] not in ALLOWED_SUBCOMMANDS:
        raise ToolSecurityError("不允许的 docker 子命令: %s" % (args[0] if args else "(空)"))

    exe = _docker_bin()
    if not exe:
        raise RuntimeError("容器内未安装 docker CLI")

    proc = await asyncio.create_subprocess_exec(
        exe, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _check_name(name: str) -> str:
    if not name or not _SAFE_NAME.match(name):
        raise ToolSecurityError("非法的容器/对象名: %r" % name)
    return name


# ── Handlers ──────────────────────────────────────────────────────────────

async def _env_containers(args: Dict[str, Any]) -> ToolResult:
    fmt = "{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Status}}\t{{.Ports}}"
    code, out, err = await _run_docker(["ps", "-a", "--format", fmt])
    if code != 0 and not out.strip():
        return ToolResult.fail("docker ps 失败: %s" % err.strip()[:200])

    containers = []
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        status = parts[3]
        restarts = 0
        try:
            if "Restarting" in status:
                restarts = 1
        except Exception:
            pass
        containers.append({
            "name": parts[0], "image": parts[1], "state": parts[2],
            "status": status, "ports": parts[4] if len(parts) > 4 else "",
            "suspicious": ("Restarting" in status or "Exited" in status or "unhealthy" in status.lower()),
        })

    bad = [c for c in containers if c["suspicious"]]
    return ToolResult(
        ok=True,
        summary="容器 %d 个，异常 %d 个%s" % (
            len(containers), len(bad),
            ("：" + "; ".join("%s(%s)" % (c["name"], c["status"][:40]) for c in bad)) if bad else ""),
        data={"containers": containers, "unhealthy": bad},
    )


async def _env_container_inspect(args: Dict[str, Any]) -> ToolResult:
    name = _check_name(args.get("container") or "")
    code, out, err = await _run_docker(["inspect", name], timeout=15)
    if code != 0:
        return ToolResult.fail("inspect 失败: %s" % err.strip()[:200])

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return ToolResult.fail("inspect 输出无法解析")

    if not data:
        return ToolResult.fail("未找到容器 %s" % name)

    c = data[0]
    cfg = c.get("Config") or {}
    host = c.get("HostConfig") or {}
    state = c.get("State") or {}
    net = c.get("NetworkSettings") or {}

    # 环境变量脱敏
    import re as _re
    safe_env = []
    for e in (cfg.get("Env") or []):
        if _re.match(r"(?i).*(password|secret|token|key).*=", e):
            k = e.split("=", 1)[0]
            safe_env.append(k + "=***")
        else:
            safe_env.append(e)

    info = {
        "name": (c.get("Name") or "").lstrip("/"),
        "image": cfg.get("Image"),
        "state": {
            "status": state.get("Status"),
            "running": state.get("Running"),
            "restartCount": c.get("RestartCount"),
            "oomKilled": state.get("OOMKilled"),
            "exitCode": state.get("ExitCode"),
            "startedAt": state.get("StartedAt"),
            "finishedAt": state.get("FinishedAt"),
        },
        "resources": {
            "memory_limit_mb": round((host.get("Memory") or 0) / 1024 / 1024, 1),
            "nano_cpus": host.get("NanoCpus"),
            "cpu_shares": host.get("CpuShares"),
            "restart_policy": (host.get("RestartPolicy") or {}).get("Name"),
        },
        "env": safe_env,
        "ip": (net.get("IPAddress") or ""),
        "networks": list((net.get("Networks") or {}).keys()),
    }

    st = info["state"]
    flags = []
    if st.get("restartCount"):
        flags.append("重启 %s 次" % st["restartCount"])
    if st.get("oomKilled"):
        flags.append("曾被 OOM Kill ★")
    if st.get("status") != "running":
        flags.append("当前状态 %s" % st.get("status"))

    return ToolResult(
        ok=True,
        summary="容器 %s：状态 %s，重启 %s 次%s" % (
            info["name"], st.get("status"), st.get("restartCount"),
            ("　⚠ " + "; ".join(flags)) if flags else ""),
        data=info,
    )


async def _env_container_logs(args: Dict[str, Any]) -> ToolResult:
    name = _check_name(args.get("container") or "")
    tail = max(1, min(int(args.get("tail", 100)), 1000))
    grep = (args.get("grep") or "").strip()

    code, out, err = await _run_docker(["logs", "--tail", str(tail), name], timeout=20)
    text = out or err     # docker logs 可能写 stderr
    if code != 0 and not text.strip():
        return ToolResult.fail("logs 失败: %s" % err.strip()[:200])

    lines = text.splitlines()
    if grep:
        import re as _re
        try:
            pat = _re.compile(grep, _re.IGNORECASE)
            lines = [l for l in lines if pat.search(l)]
        except _re.error:
            lines = [l for l in lines if grep.lower() in l.lower()]

    # 错误聚类
    err_pat = __import__("re").compile(r"(?i)\b(error|exception|fatal|panic|timeout|refused|denied|fail)\b")
    errors = [l for l in lines if err_pat.search(l)]
    kinds: Dict[str, int] = {}
    for l in errors:
        m = err_pat.search(l)
        kinds[m.group(1).lower()] = kinds.get(m.group(1).lower(), 0) + 1

    return ToolResult(
        ok=True,
        summary="日志 %d 行（匹配 %d 行），其中疑似错误 %d 行%s" % (
            len(text.splitlines()), len(lines), len(errors),
            ("，类型分布 " + str(kinds)) if kinds else ""),
        data={"lines": lines[-200:], "error_lines": errors[-40:], "error_kinds": kinds},
    )


async def _env_container_stats(args: Dict[str, Any]) -> ToolResult:
    name = args.get("container")
    cmd = ["stats", "--no-stream", "--format",
           "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.NetIO}}\t{{.BlockIO}}"]
    if name:
        cmd.append(_check_name(name))

    code, out, err = await _run_docker(cmd, timeout=25)
    if code != 0 and not out.strip():
        return ToolResult.fail("stats 失败: %s" % err.strip()[:200])

    stats = []
    for line in out.strip().splitlines():
        p = line.split("\t")
        if len(p) < 4:
            continue
        stats.append({
            "name": p[0], "cpu": p[1], "mem_usage": p[2], "mem_perc": p[3],
            "net_io": p[4] if len(p) > 4 else "", "block_io": p[5] if len(p) > 5 else "",
        })

    hot = []
    for s in stats:
        try:
            if float(s["cpu"].rstrip("%")) > 80:
                hot.append("%s CPU %s" % (s["name"], s["cpu"]))
            if float(s["mem_perc"].rstrip("%")) > 85:
                hot.append("%s 内存 %s" % (s["name"], s["mem_perc"]))
        except (ValueError, AttributeError):
            continue

    return ToolResult(
        ok=True,
        summary="资源占用已获取（%d 个容器）%s" % (
            len(stats), ("　⚠ " + "; ".join(hot)) if hot else ""),
        data={"stats": stats, "high_usage": hot},
    )


async def _env_host_info(args: Dict[str, Any]) -> ToolResult:
    """宿主机基础信息（容器内可见的有限视图）。"""
    import os
    import platform

    info: Dict[str, Any] = {
        "container_hostname": platform.node(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }
    # loadavg（Linux 容器内可用）
    try:
        info["loadavg"] = list(os.getloadavg())
    except (AttributeError, OSError):
        info["loadavg"] = None
    # 内存
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            mem = {}
            for line in f:
                k, _, v = line.partition(":")
                mem[k.strip()] = v.strip()
        info["mem_total"] = mem.get("MemTotal")
        info["mem_available"] = mem.get("MemAvailable")
    except OSError:
        pass
    # 磁盘
    try:
        du = shutil.disk_usage("/")
        info["disk"] = {"total_gb": round(du.total / 1e9, 1),
                        "used_gb": round(du.used / 1e9, 1),
                        "free_gb": round(du.free / 1e9, 1),
                        "used_pct": round(du.used / du.total * 100, 1)}
    except OSError:
        pass

    # docker 引擎信息
    try:
        code, out, err = await _run_docker(["info", "--format", "{{.ServerVersion}}|{{.Containers}}|{{.Images}}|{{.NCPU}}|{{.MemTotal}}"], timeout=15)
        if code == 0 and out.strip():
            p = out.strip().split("|")
            info["docker"] = {
                "server_version": p[0] if len(p) > 0 else "",
                "containers": p[1] if len(p) > 1 else "",
                "images": p[2] if len(p) > 2 else "",
                "ncpu": p[3] if len(p) > 3 else "",
                "mem_total": p[4] if len(p) > 4 else "",
            }
    except Exception:
        pass

    return ToolResult(ok=True, summary="宿主机信息已采集", data=info)


# ── 规格定义 ──────────────────────────────────────────────────────────────

ENV_TOOLS = [
    ToolSpec(
        name="env_containers",
        layer="env",
        description=(
            "列出所有容器（名称/镜像/状态/端口），自动标记异常容器"
            "（Restarting / Exited / unhealthy）。排查'服务是不是挂了/反复重启'时首先调用。"
        ),
        parameters={"type": "object", "properties": {}},
        handler=_env_containers,
        timeout_s=25,
    ),
    ToolSpec(
        name="env_container_inspect",
        layer="env",
        description=(
            "查看容器详细配置：运行状态、重启次数、是否被 OOM Kill、资源限制、"
            "网络、环境变量（已脱敏）。判断'是否因资源限制被杀'时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {"container": {"type": "string", "description": "容器名，如 cc-credit-card-app"}},
            "required": ["container"],
        },
        handler=_env_container_inspect,
        timeout_s=25,
    ),
    ToolSpec(
        name="env_container_logs",
        layer="env",
        description=(
            "获取容器日志尾部，自动聚类错误行。支持 grep 关键字过滤"
            "（如 'error|timeout|refused'）。用于从应用侧找异常堆栈与报错。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "container": {"type": "string", "description": "容器名"},
                "tail": {"type": "integer", "description": "取末尾行数（1-1000）", "default": 100},
                "grep": {"type": "string", "description": "正则/关键字过滤（可选）", "default": ""},
            },
            "required": ["container"],
        },
        handler=_env_container_logs,
        timeout_s=30,
        injection_exempt=["grep"],
    ),
    ToolSpec(
        name="env_container_stats",
        layer="env",
        description="获取容器实时 CPU/内存/网络/磁盘 IO 占用，自动标出高占用容器。",
        parameters={
            "type": "object",
            "properties": {"container": {"type": "string", "description": "容器名（留空=全部）", "default": ""}},
        },
        handler=_env_container_stats,
        timeout_s=35,
    ),
    ToolSpec(
        name="env_host_info",
        layer="env",
        description="获取宿主机与 Docker 引擎信息（CPU 核数、负载、内存、磁盘、Docker 版本与容器总数）。",
        parameters={"type": "object", "properties": {}},
        handler=_env_host_info,
        timeout_s=25,
    ),
]
