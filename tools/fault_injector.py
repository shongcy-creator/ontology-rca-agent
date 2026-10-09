#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
故障注入 CLI —— 应用层集群 / 数据库层集群 / 资源耗尽 三类场景的统一入口。

子命令：
  doctor            环境体检（容器、LB、MySQL 复制、Prometheus 是否就绪）
  list              列出全部故障场景（可按层过滤）
  status            查看当前激活的故障 + 集群快照
  inject ID         注入一个故障（注入后自动校验 PromQL 信号）
  recover ID|--all  回滚（数据驱动，进程被杀也能复原）
  run ID            完整演练：注入 → 叠加压测 → 保持 → 观测 → 回滚 → 校验恢复
  verify [ID ...]   批量演练并输出机器可读报告（供端到端 RCA 验证消费）
  seed              准备测试体量（t_txn 30 万行 + t_seq 小表）

示例：
  python tools/fault_injector.py doctor
  python tools/fault_injector.py list --layer database
  python tools/fault_injector.py inject app_pause_replica
  python tools/fault_injector.py run db_row_lock_hold --hold 60 \
      --stress "--duration 60 --concurrency 24"
  python tools/fault_injector.py recover --all
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── 控制台编码兜底 ────────────────────────────────────────────────────────────
# 踩过的坑：Windows 默认控制台是 GBK，打印 `✘`（U+2718）会抛 UnicodeEncodeError。
# 而 `cleanup` 是**安全网** —— 它恰好在"某个清理动作失败、正要打印 ✘"的路径上崩溃，
# 结果**后面的清理步骤一个都没跑**（最需要它的时候它自己断了）。
# 这里统一把 stdout/stderr 设成 errors="replace"：编码不了就退化成 '?'，
# 绝不让日志问题中断清理流程。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")   # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

#: 批量演练的**默认压测配方** —— 必须与文档 §10.2 的验证命令一致，
#: 否则"同一套场景、不同负载"会得出无法对比的结论。
#:
#: 为什么不能更重：`db_slow_query_flood` / `res_db_memory` 的 recovered_signals 是
#: `rate(mysql_global_status_slow_queries[1m]) > 0`，而重压下**副本自身**也会持续
#: 产生慢查询（失败样本的 last 落在 db_node=replica-1），基线不为 0 →
#: 这条 `> 0` 的恢复校验永远不可能变假，看起来像"回滚失败"，其实是"基线漂了"。
DEFAULT_VERIFY_STRESS = "--duration 20 --concurrency 12"

from tools.chaos import FAULTS, Injector, ensure_dataset          # noqa: E402
from tools.chaos.core import (                                     # noqa: E402
    APP_GATEWAY,
    APP_LB,
    CHAOS_DIR,
    CONSOLE,
    MYSQL_PRIMARY,
    MYSQL_REPLICAS,
    PROM,
    active_faults,
    app_replicas,
    docker,
    http_json,
    mysql_scalar,
    prom_query,
    prom_wait,
    zombie_count,
)


def _all_container_names() -> List[str]:
    """当前运行的容器名（用于僵尸体检等"逐容器"检查）。"""
    rc, out, _ = docker(["ps", "--format", "{{.Names}}"])
    if rc != 0:
        return []
    return [l.strip() for l in (out or "").splitlines() if l.strip()]

LAYERS = ("application", "database", "runtime", "resource")


# ── 输出工具 ───────────────────────────────────────────────────────────────

def _dump(obj: Any, json_out: str = "") -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    print(text)
    if json_out:
        p = Path(json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print("\n[已写入] %s" % p)


def _coerce(v: str) -> Any:
    low = v.strip().lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def parse_params(pairs: Optional[List[str]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for item in pairs or []:
        if "=" not in item:
            continue
        k, _, v = item.partition("=")
        out[k.strip()] = _coerce(v)
    return out


# ── 子命令实现 ─────────────────────────────────────────────────────────────

def cmd_list(args: argparse.Namespace) -> int:
    faults = [f for f in FAULTS.values() if not args.layer or f.layer == args.layer]
    if args.json:
        _dump([f.to_dict() for f in faults], args.json_out)
        return 0
    print("=" * 96)
    print("故障注入场景目录（共 %d 个）" % len(faults))
    print("=" * 96)
    cur_layer = None
    for f in sorted(faults, key=lambda x: (LAYERS.index(x.layer) if x.layer in LAYERS else 9, x.id)):
        if f.layer != cur_layer:
            cur_layer = f.layer
            print("\n── %s ──────────────────────────────────────────────────────────" % cur_layer)
        print("  %-24s [%s] %s" % (f.id, f.category, f.title))
        print("      %s" % f.description)
        print("      目标 selector=%s  需要压测=%s  告警=%s"
              % (f.selector, "是" if f.needs_stress else "否",
                 ",".join(f.alert_names) or "-"))
        print("      期望根因: %s" % f.expected_root_cause)
        if f.default_params:
            print("      参数: %s" % json.dumps(f.default_params, ensure_ascii=False))
    print("\n共 %d 个场景" % len(faults))
    return 0


def doctor_checks() -> List[Dict[str, Any]]:
    """
    环境体检检查集（结构化返回，**供 CLI 与故障注入控制台复用**）。

    抽出来的原因：控制台（`/api/chaos/doctor`）需要的是数据而不是打印文本；
    如果控制台自己再写一份检查逻辑，两边就会漂移。
    """
    checks: List[Dict[str, Any]] = []

    def rec(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    rc, out, _ = docker(["version", "--format", "{{.Server.Version}}"])
    rec("docker", rc == 0, (out or "").strip())

    reps = app_replicas()
    running = [r for r in reps if r["state"] == "running"]
    rec("app 副本（running/总数）", len(running) >= 3, "%d/%d" % (len(running), len(reps)))

    st, body = http_json(APP_LB + "/health")
    rec("应用网关 /health", st == 200, body if st != 200 else "200")

    st, body = http_json(APP_LB + "/txn/recent", timeout=15)
    ok_read = st == 200 and isinstance(body, dict) and body.get("ok")
    rec("读路径（副本）", ok_read,
        (body.get("error") if isinstance(body, dict) else body) if not ok_read else "200")

    for node in [MYSQL_PRIMARY] + MYSQL_REPLICAS:
        v = mysql_scalar(node, "SELECT @@server_id")
        rec("mysql %s" % node, bool(v), "unreachable" if not v else "server_id=%s" % v)

    # ── 事实表一致性 ────────────────────────────────────────────────────────
    # 项目里存在**两张**"期望根因"事实表：
    #   ① tools/chaos/catalog.py           每个 Fault.ontology_terms（控制台用它展示关联术语）
    #   ② tools/ontology_cluster_patch.py  SCENARIO_EXPECTED_TERMS（A/B 评估的 ground truth）
    # 实测发现 9/21 场景两张表不一致 —— 6 个场景在 catalog 里**连根因术语都没有**
    # （db_replica_lag / app_cpu_stress / app_network_* / app_memory_stress / db_replica_io_stop），
    # 3 个漏了合法术语。事实表漂移的后果是：**所有准确率数字都不可信**，
    # 而且控制台会向使用人展示不完整的"关联本体术语"。因此把它变成环境体检的一项。
    try:
        from tools.ontology_cluster_patch import SCENARIO_EXPECTED_TERMS as _EXP
        missing = []
        for fid, f in FAULTS.items():
            cat = {str(t) for t in (getattr(f, "ontology_terms", None) or [])
                   if str(t).startswith("rc:")}
            exp = {str(t) for t in (_EXP.get(fid) or []) if str(t).startswith("rc:")}
            if not exp.issubset(cat):
                missing.append("%s 缺 %s" % (fid, ",".join(sorted(exp - cat))))
        rec("期望根因事实表一致（catalog vs A/B）", not missing,
            "；".join(missing[:3]) + ("…" if len(missing) > 3 else "") if missing else
            "21 个场景一致")
    except Exception as e:  # noqa: BLE001
        rec("期望根因事实表一致（catalog vs A/B）", False, "%s: %s" % (type(e).__name__, e))

    # 为什么必须用 /proc 而不是 `ps`：`mysql:8.0` 没装 procps，`ps` 不存在。
    # 早期体检脚本用 `ps -eo stat | grep -c '^Z'` → 命令失败 → **静默返回 0**，
    # 于是一个真有 1769 个僵尸的容器被报告成"僵尸 0 ✅"。假阴性比不检查更坏。
    #
    # 僵尸从哪来：注入会在容器里起 `sh` 循环，回滚用 `kill -9` 杀循环外壳，
    # 在飞的子进程被孤儿化并 reparent 到 PID 1；PID 1 若没有 init 就不回收。
    # 连锁反应：清理脚本按标记扫 /proc 是 O(进程数)，条目从 ~10 涨到 1769 →
    # 整体清理从 17s 变成 75.6s，看起来像卡死。
    try:
        zombie_bad: List[str] = []
        for c in _all_container_names():
            z = zombie_count(c)
            if z.get("total"):
                zombie_bad.append("%s=%d(%s)" % (c, z["total"], z.get("top") or ""))
        rec("容器无僵尸进程", not zombie_bad,
            "；".join(zombie_bad) if zombie_bad else "全部为 0")
    except Exception as e:  # noqa: BLE001
        rec("容器无僵尸进程", False, "%s: %s" % (type(e).__name__, e))

    for node in MYSQL_REPLICAS:
        io = mysql_scalar(node, "SELECT SERVICE_STATE FROM performance_schema.replication_connection_status LIMIT 1")
        sql_th = mysql_scalar(node, "SELECT SERVICE_STATE FROM performance_schema.replication_applier_status LIMIT 1")
        rec("replication %s" % node, io == "ON" and sql_th == "ON",
            "io=%s sql=%s" % (io, sql_th))

    st, body = http_json(PROM + "/api/v1/query?query=up")
    rec("prometheus", st == 200, "" if st == 200 else str(body)[:120])

    n_app_targets = len(prom_query('up{job="payment-app"}'))
    rec("prometheus 应用副本目标", n_app_targets >= 3, n_app_targets)

    probe = prom_query('probe_success{job="blackbox-gateway"}')
    rec("blackbox 网关探活", bool(probe), "no probe_success yet" if not probe else "ok")

    prom_query("ALERTS")  # 仅为触发一次连接
    st, body = http_json(PROM + "/api/v1/rules")
    n_rules = 0
    if st == 200 and isinstance(body, dict):
        for g in (body.get("data") or {}).get("groups") or []:
            n_rules += len(g.get("rules") or [])
    rec("告警规则已加载", n_rules >= 20, n_rules)

    txn = mysql_scalar(MYSQL_PRIMARY, "SELECT COUNT(*) FROM t_txn", db="creditcard")
    rec("测试体量 t_txn", bool(txn) and int(txn or 0) >= 100_000,
        "当前 %s 行，可执行 seed" % (txn or 0))

    act = active_faults()
    rec("无残留故障", not act, [a.get("fault_id") for a in act] if act else "clean")

    # ── 前端 → 后端 代理连通（踩过的坑，必须自动化）────────────────────────
    # 症状：控制台的故障注入/压测/成本三个页面全报 **502 Bad Gateway**，
    # 而 `docker ps` 里前后端都 healthy、直连后端 8088 也是 200。
    # 根因：nginx 的 `proxy_pass http://rca-agent-backend:8088`（字面主机名）
    # 只在**启动时**解析一次并缓存 IP；后端容器一旦重建换了 IP，这条路就永久断掉。
    # 这类"所有健康检查都绿、功能却全挂"最难查 —— 所以让 doctor 直接模拟
    # "浏览器 → nginx → 后端"这条真实路径，而不是只看容器状态。
    try:
        st_p, _ = http_json(CONSOLE + "/api/health")
        rec("前端→后端 代理连通（nginx）", st_p == 200,
            "HTTP %s（502 通常=nginx 缓存了旧的后端 IP，重启前端或改用 resolver 变量）" % st_p)
    except Exception as e:  # noqa: BLE001
        rec("前端→后端 代理连通（nginx）", False, str(e)[:80])
    return checks


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = doctor_checks()
    ok_all = all(c["ok"] for c in checks)
    print("=" * 88)
    print("环境体检")
    print("=" * 88)
    for c in checks:
        print("  %-28s %s %s" % (c["name"], "OK  " if c["ok"] else "FAIL",
                                 "" if c["ok"] else c["detail"]))
    print("-" * 88)
    print("总体: %s" % ("PASS" if ok_all else "FAIL"))
    if args.json_out:
        _dump({"ok": ok_all, "checks": {c["name"]: {"ok": c["ok"], "detail": c["detail"]}
                                        for c in checks}}, args.json_out)
    return 0 if ok_all else 1


def mysql_snapshot() -> Dict[str, Any]:
    """三个 MySQL 节点的关键状态（CLI status 与控制台共用）。"""
    snap: Dict[str, Any] = {}
    for node in [MYSQL_PRIMARY] + MYSQL_REPLICAS:
        snap[node] = {
            "server_id": mysql_scalar(node, "SELECT @@server_id"),
            "read_only": mysql_scalar(node, "SELECT @@read_only"),
            "threads_connected": mysql_scalar(node, "SELECT COUNT(*) FROM information_schema.processlist"),
            "txn_rows": mysql_scalar(node, "SELECT COUNT(*) FROM t_txn", db="creditcard"),
        }
    return snap


def cmd_status(args: argparse.Namespace) -> int:
    snap = {
        "active_faults": active_faults(),
        "app_replicas": app_replicas(),
        "mysql": mysql_snapshot(),
        "prometheus_up": {
            "payment-app": len(prom_query('up{job="payment-app"}')),
            "blackbox-gateway": [
                {"value": r["value"][1]} for r in prom_query('probe_success{job="blackbox-gateway"}')
            ],
        },
    }
    _dump(snap, args.json_out)
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    print("[seed] 准备测试体量 ...")
    out = ensure_dataset(target_rows=args.rows)
    _dump(out, args.json_out)
    return 0 if out.get("ok") else 1


def _engine(args: argparse.Namespace) -> Injector:
    return Injector(FAULTS, verbose=not args.quiet)


def cmd_inject(args: argparse.Namespace) -> int:
    eng = _engine(args)
    res = eng.inject(
        args.fault_id,
        params=parse_params(args.param),
        targets=[t.strip() for t in args.target.split(",")] if args.target else None,
        wait_signals=not args.no_wait,
        signal_timeout=args.signal_timeout,
    )
    _dump(res, args.json_out)
    return 0 if res.get("ok") else 1


def cmd_recover(args: argparse.Namespace) -> int:
    eng = _engine(args)
    if args.all:
        res = eng.recover_all()
    elif args.fault_id:
        res = eng.recover(
            args.fault_id,
            targets=[t.strip() for t in args.target.split(",")] if args.target else None)
    else:
        print("需要指定 fault_id 或 --all", file=sys.stderr)
        return 2
    _dump(res, args.json_out)
    return 0 if res.get("ok") else 1


def cmd_run(args: argparse.Namespace) -> int:
    eng = _engine(args)
    stress_args = args.stress.split() if args.stress else None
    res = eng.run(
        args.fault_id,
        hold_s=args.hold,
        stress_args=stress_args,
        params=parse_params(args.param),
        signal_timeout=args.signal_timeout,
        recovery_timeout=args.recovery_timeout,
    )
    _dump(res, args.json_out)
    return 0 if res.get("ok") else 1


def verify_scenarios(fault_ids: Optional[List[str]] = None,
                     hold: float = 20.0,
                     stress: Optional[str] = None,
                     params: Optional[List[str]] = None,
                     signal_timeout: float = 60.0,
                     recovery_timeout: float = 90.0,
                     cooldown: float = 8.0,
                     progress: Optional[Callable[[str], None]] = None,
                     should_stop: Optional[Callable[[], bool]] = None,
                     json_out: str = "") -> Dict[str, Any]:
    """
    批量演练：每个场景 inject → 压测 → 保持 → 回滚 → 校验恢复（结构化返回）。

    抽出来的原因：CLI 只打印，而控制台的"批量演练"按钮需要结构化结果与进度回调。
    `progress(line)` 会在每个关键节点被调用一次。

    `should_stop()`：**协作式取消**（人工"停止并回滚"）。
    取消点在①每个场景开始前 ②场景内部各处长阻塞（透传给 `Injector.run`）
    ③场景间隔的恢复等待；命中即停止后续场景，
    并且 —— 关键不变式 —— **函数返回前一定执行一次 recover_all()**，
    保证"停止演练"不会留下激活故障。
    """
    def say(msg: str) -> None:
        print(msg, flush=True)
        if progress:
            try:
                progress(msg)
            except Exception:  # noqa: BLE001
                pass

    def _stopped() -> bool:
        return bool(should_stop and should_stop())

    def _nap(seconds: float) -> bool:
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            if _stopped():
                return True
            time.sleep(min(0.5, max(0.0, end - time.time())))
        return _stopped()

    ids = fault_ids or sorted(FAULTS)
    eng = Injector(FAULTS, verbose=False)
    results: List[Dict[str, Any]] = []
    total = len(ids)
    stress_args = stress if stress is not None else DEFAULT_VERIFY_STRESS
    stopped_early = False
    stopped_at = ""

    try:
        for i, fid in enumerate(ids, 1):
            if _stopped():
                stopped_early = True
                stopped_at = "场景 %d/%d 开始前" % (i, total)
                say("⏹ 收到停止请求 → 不再开始新场景（当前已完成 %d/%d）" % (i - 1, total))
                break
            if fid not in FAULTS:
                results.append({"fault_id": fid, "ok": False, "error": "unknown fault"})
                continue
            say("[%d/%d] %s  %s" % (i, total, fid, FAULTS[fid].title))
            t0 = time.time()
            r = eng.run(fid, hold_s=hold,
                        stress_args=stress_args.split() if stress_args else None,
                        params=parse_params(params),
                        signal_timeout=signal_timeout,
                        recovery_timeout=recovery_timeout,
                        should_stop=should_stop)
            r["scenario_seconds"] = round(time.time() - t0, 1)
            r["title"] = FAULTS[fid].title
            r["layer"] = FAULTS[fid].layer
            r["category"] = FAULTS[fid].category
            r["expected_root_cause"] = FAULTS[fid].expected_root_cause
            results.append(r)
            if r.get("cancelled"):
                stopped_early = True
                stopped_at = "场景 %d/%d 执行中（已回滚）" % (i, total)
                say("⏹ %s 已按请求取消并回滚" % fid)
                eng.recover_all()
                break
            say("    → %s（%.1fs）" % ("通过" if r.get("ok") else "失败: %s" % r.get("error"),
                                       r["scenario_seconds"]))
            if i < total:
                say("场景间隔恢复 %ds ..." % int(cooldown))
                if _nap(cooldown):
                    stopped_early = True
                    stopped_at = "场景 %d/%d 间隔期" % (i, total)
                    say("⏹ 收到停止请求 → 中止后续场景")
                    break
            eng.recover_all()
    finally:
        # ★ 不变式：无论正常结束、异常还是被取消，都做一次全量回滚
        try:
            eng.recover_all()
        except Exception as e:  # noqa: BLE001
            say("收尾回滚异常: %s" % e)

    ok_n = sum(1 for r in results if r.get("ok"))
    # ★ "通过"的**准确含义**必须写清楚，否则数字会被过度解读。
    #
    # `Injector.inject` 在"注入成功但 50s 内未观测到任何声明信号"时也返回 ok=True
    # （只带一个 warning）—— 这是刻意的（注入确实发生了、也必须回滚），
    # 但它意味着 **"21/21 通过" ≠ "21 个场景的声明信号都成立"**。
    # 实测：某次全量 21/21 里，有 2 个场景注入期 0 条声明信号成立
    # （db_disk_temp_tables / res_db_memory —— 它们的信号是 [2m] 速率窗口，
    #   而验证只等 50s，窗口没铺满），另有 3 个只部分成立。
    # 因此额外统计"注入期**全部**声明信号都成立"的场景数并写进报告，
    # 让弱结论显式可见，而不是藏在 warning 里。
    sig_full = sum(1 for r in results
                   if (r.get("inject") or {}).get("signal_checks")
                   and all(c.get("ok") for c in (r["inject"].get("signal_checks") or [])))
    sig_none = sum(1 for r in results
                   if (r.get("inject") or {}).get("signal_checks")
                   and not any(c.get("ok") for c in (r["inject"].get("signal_checks") or [])))
    # 产物路径：**子集运行绝不覆盖全量证据**。
    # 踩过的坑：用 `--only app_memory_stress` 做一次回归，就把 21/21 的
    # fault_verify_report.json（137KB）覆盖成了 1/1 —— 报告里"故障注入自检"
    # 那一节会瞬间变成假数据。所以按范围分开落盘。
    subset = bool(fault_ids) and set(fault_ids) != set(FAULTS)
    summary = {"total": total, "attempted": len(results), "passed": ok_n,
               "failed": len(results) - ok_n,
               # passed = 注入成功且回滚成功；不等于"声明信号全部成立"
               "passed_meaning": "注入成功且回滚成功（不要求声明信号全部成立）",
               "signals_observed_full": sig_full,
               "signals_observed_none": sig_none,
               "scope": "subset" if subset else "full",
               "fault_ids": list(fault_ids) if fault_ids else sorted(FAULTS),
               "stopped_early": stopped_early, "stopped_at": stopped_at,
               "results": results}
    if json_out:
        out = json_out
    elif subset:
        out = str(CHAOS_DIR / "fault_verify_report_subset.json")
    else:
        out = str(CHAOS_DIR / "fault_verify_report.json")
    try:
        _dump(summary, out)
    except Exception as e:  # noqa: BLE001
        summary["report_write_error"] = str(e)
    say("批量演练: %d/%d 通过%s" % (ok_n, len(results),
                                 "（已按请求提前停止）" if stopped_early else ""))
    # 把"通过"的弱含义摆到台面上：通过 = 注入成功且回滚成功；
    # 其中"注入期全部声明信号成立"的只有 sig_full 个。
    say("  其中注入期**全部声明信号成立** %d/%d；完全未观测到信号 %d/%d"
        % (sig_full, len(results), sig_none, len(results)))
    if sig_none:
        say("  （未观测到信号的那几个场景，信号是 [2m] 级速率窗口，"
            "而验证只等 %ss，窗口未铺满）" % int(signal_timeout))
    return summary


def cmd_verify(args: argparse.Namespace) -> int:
    """批量演练（CLI 包装）。"""
    summary = verify_scenarios(
        fault_ids=args.fault_ids, hold=args.hold, stress=args.stress,
        params=args.param, signal_timeout=args.signal_timeout,
        recovery_timeout=args.recovery_timeout, cooldown=args.cooldown,
        json_out=args.json_out or "",
    )
    return 0 if summary["passed"] == summary["total"] else 1


def cleanup_all(progress: Optional[Callable[[str], None]] = None) -> List[Dict[str, Any]]:
    """
    安全网：把所有"注入可能留下的痕迹"清干净，**返回结构化动作列表**。

    比 `recover --all` 更激进 —— 不依赖状态文件，直接按环境事实清理：
      · 应用容器内的 stress-ng 压力进程
      · MySQL 容器内的注入后台循环（按 /proc 标记扫描，不依赖 pkill）
      · 长期 Sleep / 等锁 / SELECT SLEEP 会话（注入残留连接）
      · 故障可能改过的全局变量（tmp_table_size / sort / join buffer / max_heap）
      · 主库 read_only 误置、副本复制线程误停
      · 应用副本被压小的 cgroup 内存上限
    """
    from tools.chaos.core import (  # noqa: PLC0415
        CHAOS_DIR, MYSQL_PRIMARY, MYSQL_REPLICAS, app_replicas,
        container_state, docker, kill_by_marker,
        mysql_kill_sessions, mysql_scalar, mysql_sql,
    )
    from tools.chaos.catalog import (  # noqa: PLC0415
        MARK_CONN, MARK_DBCPU, MARK_DBMEM, MARK_LAG, MARK_LOCK, MARK_SLOW, MARK_TMP,
    )

    actions: List[Dict[str, Any]] = []

    def emit(action: str, desc: str, ok: bool, detail: Any = "") -> None:
        actions.append({"action": action, "desc": desc, "ok": bool(ok), "detail": detail})
        line = "  %s %s" % ("✔" if ok else "✘", desc)
        print(line, flush=True)
        if progress:
            try:
                progress(line + ("" if ok else "  (%s)" % detail))
            except Exception:  # noqa: BLE001
                pass

    print("=" * 88, flush=True)
    print("环境清理（安全网）", flush=True)
    print("=" * 88, flush=True)

    # 1) 状态文件回滚（必须最先做：先把故障"解除"，再清残留）
    try:
        res = Injector(FAULTS, verbose=False).recover_all()
        emit("recover_all", "按 .chaos/fault_state.json 回滚全部激活故障",
             res.get("ok", True), res.get("error", ""))
    except Exception as e:  # noqa: BLE001
        emit("recover_all", "按状态文件回滚", False, "%s: %s" % (type(e).__name__, e))

    # 2)+3) 六个容器**并行**清理。
    #    每个容器的清理都是独立的 docker exec 往返（实测单节点 ~1-2s、
    #    MySQL 节点 ~5s），串行要 15s+ —— 而"停止演练"时用户正在等这一屏，
    #    所以并行化到 max_workers=6，整体收敛到最慢那个节点的时间。
    app_names = [r["name"] for r in app_replicas()]
    mysql_nodes = [MYSQL_PRIMARY] + MYSQL_REPLICAS

    def _clean_app(name: str) -> Dict[str, Any]:
        try:
            kill = kill_by_marker(name, "stress-ng")
            st = container_state(name)
            mem_mb = st.get("memory_limit_mb") or 0
            restored = False
            if 0 < mem_mb < 256:
                docker(["update", "--memory", "256m", "--memory-swap", "512m", name])
                restored = True
            if st.get("status") != "running":
                docker(["start", name], timeout=90)
            return {"action": "app:" + name, "ok": True,
                    "desc": "%s 清理压测进程 / 恢复内存上限 / 确保运行" % name,
                    "detail": {"stress_killed": kill.get("killed"),
                               "mem_limit_restored": restored}}
        except Exception as e:  # noqa: BLE001
            return {"action": "app:" + name, "ok": False, "desc": "%s 清理" % name,
                    "detail": "%s: %s" % (type(e).__name__, e)}

    def _clean_mysql(node: str) -> Dict[str, Any]:
        try:
            marks = {}
            for m in (MARK_SLOW, MARK_DBCPU, MARK_TMP, MARK_DBMEM, MARK_CONN, MARK_LOCK, MARK_LAG):
                marks[m] = kill_by_marker(node, m).get("killed", 0)
            killed = {
                "sleep_query": mysql_kill_sessions(node, "command='Query' AND info LIKE 'SELECT SLEEP(%'"),
                "long_idle": mysql_kill_sessions(node, "command='Sleep' AND time >= 300"),
                "lock_wait": mysql_kill_sessions(node, "state LIKE '%lock%'"),
            }
            mysql_sql(node, "SET GLOBAL tmp_table_size=16777216; "
                            "SET GLOBAL max_heap_table_size=16777216; "
                            "SET GLOBAL sort_buffer_size=262144; "
                            "SET GLOBAL join_buffer_size=262144;",
                      user="root", pwd="rootpass")
            if node == MYSQL_PRIMARY:
                mysql_sql(node, "SET GLOBAL read_only=OFF; SET GLOBAL super_read_only=OFF;",
                          user="root", pwd="rootpass")
            else:
                mysql_sql(node, "START REPLICA;", user="root", pwd="rootpass")
                mysql_sql(node, "SET GLOBAL read_only=ON; SET GLOBAL super_read_only=ON;",
                          user="root", pwd="rootpass")
            return {"action": "mysql:" + node, "ok": True,
                    "desc": "%s 清理注入循环/残留会话，恢复全局变量与角色" % node,
                    "detail": {"marker_kills": marks, "session_kills": killed,
                               "read_only": mysql_scalar(node, "SELECT @@read_only")}}
        except Exception as e:  # noqa: BLE001
            return {"action": "mysql:" + node, "ok": False, "desc": "%s 清理" % node,
                    "detail": "%s: %s" % (type(e).__name__, e)}

    tasks: List[Any] = []
    for n in app_names:
        tasks.append((n, _clean_app))
    for n in mysql_nodes:
        tasks.append((n, _clean_mysql))

    results: Dict[str, Dict[str, Any]] = {}
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=max(1, len(tasks)), thread_name_prefix="cleanup") as pool:
            futs = {pool.submit(fn, name): name for name, fn in tasks}
            for fut in as_completed(futs):
                name = futs[fut]
                try:
                    results[name] = fut.result()
                except Exception as e:  # noqa: BLE001
                    results[name] = {"action": name, "ok": False, "desc": name,
                                     "detail": "%s: %s" % (type(e).__name__, e)}
    except Exception:  # noqa: BLE001
        # 线程池不可用时退回串行（功能优先于速度）
        for name, fn in tasks:
            results[name] = fn(name)

    # 按稳定顺序输出（先 app 后 mysql），避免并行导致报告顺序抖动
    for name in app_names + mysql_nodes:
        r = results.get(name) or {"action": name, "ok": False, "desc": name,
                                 "detail": "未执行"}
        emit(r["action"], r["desc"], r["ok"], r.get("detail", ""))

    report = {"ok": all(a["ok"] for a in actions), "actions": actions}
    try:
        CHAOS_DIR.mkdir(parents=True, exist_ok=True)
        (CHAOS_DIR / "cleanup_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print("-" * 88, flush=True)
    print("清理完成，报告: .chaos/cleanup_report.json", flush=True)
    return actions


def cmd_cleanup(args: argparse.Namespace) -> int:
    actions = cleanup_all()
    return 0 if all(a["ok"] for a in actions) else 1


# ── main ───────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fault_injector.py",
        description="应用层 / 数据库层 / 资源耗尽 故障注入器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--quiet", action="store_true", help="静默模式")
    p.add_argument("--json-out", default="", help="把结果写入 JSON 文件")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="列出故障场景")
    sp.add_argument("--layer", choices=LAYERS)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("doctor", help="环境体检")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("status", help="当前激活故障与集群状态")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("seed", help="准备测试体量")
    sp.add_argument("--rows", type=int, default=300_000)
    sp.set_defaults(func=cmd_seed)

    sp = sub.add_parser("cleanup", help="安全网：清理一切注入残留（不依赖状态文件）")
    sp.set_defaults(func=cmd_cleanup)

    sp = sub.add_parser("inject", help="注入故障")
    sp.add_argument("fault_id")
    sp.add_argument("--param", action="append", help="场景参数 k=v，可重复")
    sp.add_argument("--target", default="", help="覆盖目标容器（逗号分隔）")
    sp.add_argument("--no-wait", action="store_true", help="不等待 PromQL 信号")
    sp.add_argument("--signal-timeout", type=float, default=120.0)
    sp.set_defaults(func=cmd_inject)

    sp = sub.add_parser("recover", help="回滚故障")
    sp.add_argument("fault_id", nargs="?")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--target", default="")
    sp.set_defaults(func=cmd_recover)

    sp = sub.add_parser("run", help="完整演练（注入→压测→保持→回滚）")
    sp.add_argument("fault_id")
    sp.add_argument("--hold", type=float, default=90.0, help="故障保持秒数")
    sp.add_argument("--stress", default="", help="压测参数（stress_harness.py 的参数串）")
    sp.add_argument("--param", action="append")
    sp.add_argument("--signal-timeout", type=float, default=75.0)
    sp.add_argument("--recovery-timeout", type=float, default=150.0,
                    help="回滚后等待信号回到不成立的秒数预算")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("verify", help="批量演练并输出报告")
    sp.add_argument("fault_ids", nargs="*")
    sp.add_argument("--hold", type=float, default=60.0)
    sp.add_argument("--stress", default=None,
                    help="压测参数串；默认 '%s'（与文档 §10.2 验证配方一致）"
                         % DEFAULT_VERIFY_STRESS)
    sp.add_argument("--param", action="append")
    sp.add_argument("--signal-timeout", type=float, default=75.0)
    sp.add_argument("--recovery-timeout", type=float, default=150.0,
                    help="每个场景回滚后等待信号恢复的秒数预算")
    sp.add_argument("--cooldown", type=float, default=20.0)
    sp.set_defaults(func=cmd_verify)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
