# -*- coding: utf-8 -*-
"""
故障目录（fault catalog）—— 应用层 / 数据库层 / 资源耗尽三大类场景。

每个场景都是 (inject, recover) 成对实现：
  · inject  返回 {"ok": bool, "recover_hint": {...}, "detail": {...}}
  · recover 必须 **幂等**，且只依赖 (targets, params, recover_hint)，
    这样即使注入进程被杀，也能靠 .chaos/fault_state.json 复原。

场景编号约定：
  app_*  —— 应用层（副本、网关、容器资源、网络）
  db_*   —— 数据库层（锁、慢查询、连接、复制、主从角色、磁盘临时表）
  res_*  —— 跨层资源耗尽（需叠加 stress_harness 压测才成灾）
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from .core import (
    APP_GATEWAY,
    MYSQL_APP_PWD,
    MYSQL_APP_USER,
    MYSQL_DB,
    MYSQL_PRIMARY,
    MYSQL_REPLICAS,
    Fault,
    FaultContext,
    all_mysql_nodes,
    app_replicas,
    container_ip,
    container_state,
    docker,
    docker_exec,
    http_json,
    kill_by_marker,
    mysql_kill_sessions,
    mysql_scalar,
    mysql_sql,
    prom_query,
)

# ── 压测放大用的辅助表 ─────────────────────────────────────────────────────
SEQ_TABLE = "t_seq"

# ── 注入进程的唯一标记 ────────────────────────────────────────────────────
# 每个"会起后台循环"的故障都必须带标记：回滚时按标记扫 /proc 精确杀掉，
# 不能依赖 pkill（mysql:8.0 镜像没有 procps），也不能依赖 SQL 注释
# （processlist.info 会丢掉注释）。
MARK_SLOW = "chaos-slow-query"
MARK_DBCPU = "chaos-db-cpu"
MARK_TMP = "chaos-tmp-disk"
MARK_DBMEM = "chaos-db-mem"
MARK_CONN = "chaos-conn-hold"
MARK_LOCK = "chaos-row-lock"
MARK_LAG = "chaos-lag-writer"


# ═════════════════════════════════════════════════════════════════════════════
# 共用小工具
# ═════════════════════════════════════════════════════════════════════════════

def _shq(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def _mysql_loop(node: str, sql: str, loops: int, marker: str,
                parallel: int = 1, sleep_s: float = 0.0,
                duration_s: float = 0.0) -> str:
    """
    构造"反复执行同一条 SQL"的后台循环命令（带标记，供回滚时精确击杀）。

    parallel > 1 时并行起 N 个循环 —— 有些场景（磁盘临时表）单循环速率
    不足以让 Prometheus/告警规则看到，需要并行放大。

    `duration_s > 0` 时按**时间**而非**次数**收尾（`loops` 变成安全上限）：
    这是踩过的坑 —— 原先只按次数循环，快查询几十秒就跑完，
    而告警规则的 `for` 要 1–3 分钟、端到端保持期要 4 分钟，
    于是"故障已经结束、告警还没 firing"或"采样时告警已恢复"，
    表现为 `alert_coverage=False`（实测 `db_conn_saturation`、`res_db_memory`）。
    经验规则：**注入时长 ≥ 保持期 + 告警最大 for**，否则测的不是同一件事。
    """
    one = ("mysql -u{u} -p{p} -h127.0.0.1 {db} -e {sql} >/dev/null 2>&1"
           .format(u=MYSQL_APP_USER, p=MYSQL_APP_PWD, db=MYSQL_DB, sql=_shq(sql)))
    body = ("for i in $(seq 1 {n}); do {one}; {sl}done"
            .format(n=loops, one=one, sl=("sleep %s; " % sleep_s) if sleep_s else ""))
    if duration_s and duration_s > 0:
        # 用 date +%s 而不是 $SECONDS（dash 不支持后者）
        body = ("__end=$(( $(date +%s) + {d} )); i=0; "
                "while [ $i -lt {n} ] && [ $(date +%s) -lt $__end ]; do {one}; "
                "{sl}i=$((i+1)); done"
                .format(d=int(duration_s), n=loops, one=one,
                        sl=("sleep %s; " % sleep_s) if sleep_s else ""))
    if parallel <= 1:
        return body
    parts = ["(%s) &" % body for _ in range(parallel)]
    return " ".join(parts) + " wait"


def _kill_loop(node: str, marker: str) -> Dict[str, Any]:
    """按标记杀掉后台循环（含循环 shell 与 mysql 客户端）。"""
    res = kill_by_marker(node, marker)
    # 服务端兜底：清掉仍在执行的长查询（客户端被杀后通常已随之断开）
    res["sessions_killed"] = mysql_kill_sessions(
        node, "command='Query' AND time >= 5 AND (info LIKE 'SELECT SLEEP%' "
              "OR info LIKE '%JOIN%' OR info LIKE '%GROUP BY%')")
    return res


def _restore_docker_memory(container: str, mb: int, swap_mb: int) -> None:
    docker(["update", "--memory", "%dm" % mb, "--memory-swap", "%dm" % swap_mb, container])


# ═════════════════════════════════════════════════════════════════════════════
# 数据集准备（让慢查询/CPU/临时表类故障有真实体量）
# ═════════════════════════════════════════════════════════════════════════════

def ensure_dataset(target_rows: int = 300_000) -> Dict[str, Any]:
    """
    确保主库上有足够的测试体量：
      · t_txn 至少 target_rows 行（用于全表扫描 / filesort / 临时表故障）
      · t_seq 小序列表（用于交叉连接放大 CPU 与扫描量）
    通过递归 CTE 批量插入，结果经复制自动同步到副本。
    """
    out: Dict[str, Any] = {"ok": True, "detail": {}}

    # ── 小序列表 ──────────────────────────────────────────────
    mysql_sql(MYSQL_PRIMARY, (
        "CREATE TABLE IF NOT EXISTS {seq} (n INT PRIMARY KEY);"
    ).format(seq=SEQ_TABLE), db=MYSQL_DB)
    have_seq = mysql_scalar(MYSQL_PRIMARY, "SELECT COUNT(*) FROM %s" % SEQ_TABLE, db=MYSQL_DB)
    if not have_seq or int(have_seq or 0) < 20:
        mysql_sql(MYSQL_PRIMARY, (
            "INSERT IGNORE INTO {seq} (n) "
            "WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM s WHERE n < 20) "
            "SELECT n FROM s;"
        ).format(seq=SEQ_TABLE), db=MYSQL_DB)
    out["detail"]["t_seq_rows"] = mysql_scalar(
        MYSQL_PRIMARY, "SELECT COUNT(*) FROM %s" % SEQ_TABLE, db=MYSQL_DB)

    # ── 交易表体量 ────────────────────────────────────────────
    cur = int(mysql_scalar(MYSQL_PRIMARY, "SELECT COUNT(*) FROM t_txn", db=MYSQL_DB) or 0)
    out["detail"]["t_txn_before"] = cur
    if cur < target_rows:
        need = target_rows - cur
        # 分块插入，避免单条 SQL 过大与 binlog 峰值。
        # 注意：递归 CTE 受 cte_max_recursion_depth（默认 1000）限制，
        # 必须按块大小显式提高，否则 INSERT 会静默失败（行数不增长）。
        chunk = 50_000
        while need > 0:
            n = min(chunk, need)
            sql = (
                "SET SESSION cte_max_recursion_depth={depth}; "
                "INSERT INTO t_txn (customer_id, amount, status) "
                "WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM s WHERE n < {n}) "
                "SELECT (n % 3) + 1, ROUND(10 + (n % 9973) * 1.37, 2), "
                "CASE WHEN n % 17 = 0 THEN 'FAILED' WHEN n % 5 = 0 THEN 'PENDING' ELSE 'SETTLED' END "
                "FROM s;"
            ).format(depth=n + 10, n=n)
            rc, _, err = mysql_sql(MYSQL_PRIMARY, sql, db=MYSQL_DB, timeout=300)
            if rc != 0:
                out["ok"] = False
                out["error"] = "批量插入失败: %s" % (err or "").strip()[:300]
                return out
            need -= n
        out["detail"]["inserted"] = target_rows - cur
    out["detail"]["t_txn_after"] = mysql_scalar(
        MYSQL_PRIMARY, "SELECT COUNT(*) FROM t_txn", db=MYSQL_DB)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# 应用层故障
# ═════════════════════════════════════════════════════════════════════════════

def _app_kill_inject(ctx: FaultContext) -> Dict[str, Any]:
    prev = {}
    for c in ctx.targets:
        prev[c] = container_state(c).get("status")
        docker(["kill", c], timeout=60)
    return {"ok": True, "recover_hint": {"prev_status": prev},
            "detail": {"killed": ctx.targets}}


def _app_kill_recover(ctx: FaultContext) -> Dict[str, Any]:
    detail = {}
    for c in ctx.targets:
        docker(["start", c], timeout=90)
        detail[c] = container_state(c)
    return {"ok": True, "detail": detail}


def _app_pause_inject(ctx: FaultContext) -> Dict[str, Any]:
    for c in ctx.targets:
        docker(["pause", c], timeout=60)
    return {"ok": True, "detail": {"paused": ctx.targets}}


def _app_pause_recover(ctx: FaultContext) -> Dict[str, Any]:
    for c in ctx.targets:
        docker(["unpause", c], timeout=60)
    return {"ok": True, "detail": {"unpaused": ctx.targets}}


def _app_stop_inject(ctx: FaultContext) -> Dict[str, Any]:
    for c in ctx.targets:
        docker(["stop", "-t", "2", c], timeout=90)
    return {"ok": True, "detail": {"stopped": ctx.targets}}


def _app_cpu_stress_inject(ctx: FaultContext) -> Dict[str, Any]:
    secs = int(ctx.p("seconds", 900))
    workers = int(ctx.p("workers", 4))
    for c in ctx.targets:
        docker_exec(c, "pkill -f stress-ng 2>/dev/null; true")
        docker_exec(
            c,
            "nohup stress-ng --cpu {w} --cpu-method matrixprod --timeout {s}s "
            "--metrics-brief >/tmp/chaos-cpu.log 2>&1 &".format(w=workers, s=secs),
            detach=True)
    return {"ok": True, "detail": {"cpu_workers": workers, "seconds": secs}}


def _app_cpu_stress_recover(ctx: FaultContext) -> Dict[str, Any]:
    detail = {}
    for c in ctx.targets:
        detail[c] = kill_by_marker(c, "stress-ng")
    return {"ok": True, "detail": detail}


def _app_memory_stress_inject(ctx: FaultContext) -> Dict[str, Any]:
    secs = int(ctx.p("seconds", 900))
    mb = int(ctx.p("mb", 200))
    for c in ctx.targets:
        docker_exec(c, "pkill -f stress-ng 2>/dev/null; true")
        docker_exec(
            c,
            "nohup stress-ng --vm 1 --vm-bytes {m}M --vm-keep --vm-hang 0 "
            "--timeout {s}s >/tmp/chaos-mem.log 2>&1 &".format(m=mb, s=secs),
            detach=True)
    return {"ok": True, "detail": {"vm_bytes_mb": mb, "seconds": secs}}


def _app_memory_stress_recover(ctx: FaultContext) -> Dict[str, Any]:
    detail = {}
    for c in ctx.targets:
        detail[c] = kill_by_marker(c, "stress-ng")
    return {"ok": True, "detail": detail}


def _app_oom_kill_inject(ctx: FaultContext) -> Dict[str, Any]:
    """
    真正的容器 OOMKill：把 cgroup 内存上限压到低于"稳态+申请量"，
    再申请内存 → 内核 OOM Killer 杀掉容器内进程 → 容器退出并按 restart
    策略重新拉起，docker inspect 的 RestartCount / OOMKilled 发生变化。

    这里刻意用 **docker inspect 轮询**作为注入是否成功的判据：
    这正是 RCA Agent 的 env_container_inspect 工具所看的同一份事实
    （Prometheus 15s 抓取可能错过短暂的 OOM 窗口）。

    recover 会把内存上限恢复为 compose 声明的值并确保容器在跑。
    """
    # 压到远低于稳态（应用 working set 约 44MB）+ 申请一大块：让内核**必然会杀**，
    # 而不是"压到接近上限、杀不杀看内核心情"（实测 48MB/256MB 时两次都没杀成）。
    shrink_mb = int(ctx.p("shrink_mb", 32))
    vm_mb = int(ctx.p("vm_mb", 512))
    observe_s = int(ctx.p("observe_s", 90))

    hint: Dict[str, Any] = {"prev": {}}
    before: Dict[str, Any] = {}
    update_rc: Dict[str, Any] = {}
    for c in ctx.targets:
        st = container_state(c)
        before[c] = {"restart_count": st.get("restart_count"),
                     "memory_limit_mb": st.get("memory_limit_mb"),
                     "nano_cpus": st.get("nano_cpus")}
        hint["prev"][c] = before[c]
        rc, out, err = docker(["update", "--memory", "%dm" % shrink_mb,
                               "--memory-swap", "%dm" % shrink_mb, c], timeout=60)
        update_rc[c] = {"rc": rc, "error": (err or "").strip()[:200] if rc != 0 else ""}
        # ★ 读回校验：**杠杆到底有没有动**。
        # 实测踩过：`docker update --memory 64m` 之后没有读回，于是
        # "限额压到 64MB"只是意图 —— 实际上限额没变，stress-ng 申请 200MB 落在
        # 268MB 的原始上限里，**既没触发 OOM Kill、也没造成内存压力**，
        # 而这个函数照样 `return ok=True`。读回一次就能立刻发现。
        back = container_state(c)
        got_mb = back.get("memory_limit_mb")
        update_rc[c]["limit_after_mb"] = got_mb
        # ★ 数值比较，不要用 isinstance(int)：实测读回是 48.0（float），
        # 而第一版写成 `isinstance(got_mb, int) and abs(...)<=1` → 恒为 False，
        # 于是"限额已生效"被误判成"没生效"（假阴性比不检查更糟：
        # 它会让人去查一个根本没坏的地方）。读到值就按数比。
        try:
            update_rc[c]["applied"] = (got_mb is not None
                                       and abs(float(got_mb) - float(shrink_mb)) <= 1.0)
        except (TypeError, ValueError):
            update_rc[c]["applied"] = False
        docker_exec(c, "pkill -f stress-ng 2>/dev/null; true")
        docker_exec(
            c,
            "nohup stress-ng --vm 1 --vm-bytes {m}M --vm-keep --vm-hang 0 "
            "--timeout 900s >/tmp/chaos-oom.log 2>&1 &".format(m=vm_mb), detach=True)

    # ── 用 docker inspect 观测 OOMKill（与 Agent 取证工具同源）────────
    observation: Dict[str, Any] = {}
    deadline = time.time() + observe_s
    while time.time() < deadline:
        all_oom = True
        for c in ctx.targets:
            st = container_state(c)
            before_rc = before[c].get("restart_count")
            rc_now = st.get("restart_count")
            restarted = (isinstance(rc_now, int) and isinstance(before_rc, int)
                         and rc_now > before_rc)
            observation[c] = {
                "status": st.get("status"),
                "oom_killed": st.get("oom_killed"),
                "restart_count_before": before_rc,
                "restart_count_now": rc_now,
                "restarted_after_oom": restarted,
                "exit_code": st.get("exit_code"),
            }
            if not (restarted or st.get("oom_killed")):
                all_oom = False
        if all_oom:
            break
        time.sleep(5)

    oom_observed = bool(observation) and all(
        bool(v.get("restarted_after_oom") or v.get("oom_killed"))
        for v in observation.values())
    limit_ok = all(v.get("applied") for v in update_rc.values())
    # ★ 注入的成败必须由**观测**决定，而不是由"我把命令发出去了"决定。
    # 原实现无论内核有没有杀进程都 `return {"ok": True}` —— 于是场景叫
    # 「构造真实容器 OOMKill」，却可能**一次都没杀**，而调用方看到的是成功。
    # 现在：限额没压下去、或没观测到杀死，都返回 ok=False 并带上原因。
    problems = []
    if not limit_ok:
        problems.append("cgroup 内存上限未生效：%s"
                        % {c: v.get("limit_after_mb") for c, v in update_rc.items()})
    if not oom_observed:
        problems.append("在 %ds 内未观测到 OOMKill（RestartCount/OOMKilled 未变化）" % observe_s)
    return {"ok": not problems,
            "error": "；".join(problems) if problems else "",
            "recover_hint": hint,
            "detail": {"shrunk_to_mb": shrink_mb, "vm_bytes_mb": vm_mb,
                       "docker_update": update_rc, "observation": observation,
                       "oom_observed": oom_observed, "limit_applied": limit_ok}}


def _app_oom_kill_recover(ctx: FaultContext) -> Dict[str, Any]:
    detail = {}
    for c in ctx.targets:
        prev = (ctx.recover_hint.get("prev") or {}).get(c) or {}
        mb = int(prev.get("memory_limit_mb") or 256)
        if mb <= 0:
            mb = 256
        _restore_docker_memory(c, mb, mb * 2)
        kill_by_marker(c, "stress-ng")
        docker(["start", c], timeout=90)
        detail[c] = container_state(c)
    return {"ok": True, "detail": detail}


def _app_network_delay_inject(ctx: FaultContext) -> Dict[str, Any]:
    delay = int(ctx.p("delay_ms", 300))
    jitter = int(ctx.p("jitter_ms", 30))
    for c in ctx.targets:
        docker_exec(c, "tc qdisc del dev eth0 root 2>/dev/null; true")
        rc, out, err = docker_exec(
            c, "tc qdisc add dev eth0 root netem delay %dms %dms" % (delay, jitter))
        if rc != 0:
            return {"ok": False, "error": "tc 注入失败（需要 NET_ADMIN）: %s" % (err or out)[:200]}
    return {"ok": True, "detail": {"delay_ms": delay, "jitter_ms": jitter}}


def _app_network_recover(ctx: FaultContext) -> Dict[str, Any]:
    for c in ctx.targets:
        docker_exec(c, "tc qdisc del dev eth0 root 2>/dev/null; true")
    return {"ok": True}


def _app_network_loss_inject(ctx: FaultContext) -> Dict[str, Any]:
    loss = int(ctx.p("loss_pct", 40))
    for c in ctx.targets:
        docker_exec(c, "tc qdisc del dev eth0 root 2>/dev/null; true")
        rc, out, err = docker_exec(
            c, "tc qdisc add dev eth0 root netem loss %d%%" % loss)
        if rc != 0:
            return {"ok": False, "error": "tc 注入失败（需要 NET_ADMIN）: %s" % (err or out)[:200]}
    return {"ok": True, "detail": {"loss_pct": loss}}


def _app_gateway_stop_inject(ctx: FaultContext) -> Dict[str, Any]:
    docker(["stop", "-t", "2", APP_GATEWAY], timeout=90)
    return {"ok": True, "detail": {"stopped": APP_GATEWAY}}


def _app_gateway_stop_recover(ctx: FaultContext) -> Dict[str, Any]:
    docker(["start", APP_GATEWAY], timeout=90)
    return {"ok": True, "detail": container_state(APP_GATEWAY)}


# ═════════════════════════════════════════════════════════════════════════════
# 数据库层故障
# ═════════════════════════════════════════════════════════════════════════════

LOCK_MARKER = MARK_LOCK


def _db_row_lock_inject(ctx: FaultContext) -> Dict[str, Any]:
    """
    持锁事务 + 受害者写入。

    关键：必须是**全表** `SELECT * FROM t_txn FOR UPDATE`。只锁单行（WHERE txn_id=1）
    时 INSERT 新行不会冲突，innodb_row_lock_current_waits 始终为 0 —— 故障"注入了
    但没造成任何阻塞"。全表 FOR UPDATE 在 REPEATABLE READ 下会加 next-key 锁覆盖
    supremum gap，新插入行的 insert-intention 锁会被阻塞，这才是真实的长事务危害。

    同时起若干个"受害者"写会话，使阻塞在注入阶段即可观测（不依赖外部压测）。
    """
    node = ctx.targets[0] if ctx.targets else MYSQL_PRIMARY
    secs = int(ctx.p("seconds", 900))
    victims = int(ctx.p("victims", 4))

    holder_sql = (
        "START TRANSACTION; "
        "SELECT * FROM t_txn FOR UPDATE; "
        "SELECT SLEEP({sec}) /* {m}-hold */; "
        "COMMIT;"
    ).format(sec=secs, m=LOCK_MARKER)
    docker_exec(
        node,
        "mysql -u{u} -p{p} -h127.0.0.1 {db} -e {sql} >/dev/null 2>&1".format(
            u=MYSQL_APP_USER, p=MYSQL_APP_PWD, db=MYSQL_DB, sql=_shq(holder_sql)),
        detach=True)
    time.sleep(2)

    victim_sql = (
        "INSERT INTO t_txn (customer_id, amount, status) VALUES (1, 1.00, 'SETTLED') "
        "/* {m}-victim */"
    ).format(m=LOCK_MARKER)
    docker_exec(
        node,
        "for i in $(seq 1 6); do for v in $(seq 1 {v}); do "
        "mysql -u{u} -p{p} -h127.0.0.1 {db} -e {sql} >/dev/null 2>&1 & done; "
        "sleep 2; done; wait".format(
            v=victims, u=MYSQL_APP_USER, p=MYSQL_APP_PWD, db=MYSQL_DB,
            sql=_shq(victim_sql)),
        detach=True)
    time.sleep(3)

    blocked = mysql_scalar(
        node, "SELECT COUNT(*) FROM information_schema.processlist "
              "WHERE state LIKE '%lock%' OR command = 'Query'")
    return {"ok": True,
            "recover_hint": {"node": node, "victims": victims},
            "detail": {"holder_sessions": "1", "victims": victims,
                       "query_sessions": blocked}}


def _db_row_lock_recover(ctx: FaultContext) -> Dict[str, Any]:
    node = ctx.recover_hint.get("node") or (ctx.targets[0] if ctx.targets else MYSQL_PRIMARY)
    kill = kill_by_marker(node, LOCK_MARKER)
    # 服务端兜底 1：清掉仍在等锁的会话
    waiting = mysql_kill_sessions(node, "state LIKE '%lock%'")
    # 服务端兜底 2：干掉残留的长事务（KILL 掉它的连接，释放 next-key 锁）
    rc, out, _ = mysql_sql(
        node,
        "SELECT trx_mysql_thread_id FROM information_schema.innodb_trx "
        "WHERE TIMESTAMPDIFF(SECOND, trx_started, NOW()) > 20 AND trx_mysql_thread_id > 0",
        user="root", pwd="rootpass")
    stuck = 0
    for line in (out or "").strip().splitlines():
        tid = line.strip()
        if tid.isdigit() and int(tid) > 0:
            mysql_sql(node, "KILL %d" % int(tid), user="root", pwd="rootpass")
            stuck += 1
    longq = mysql_kill_sessions(
        node, "command='Query' AND time >= 10 AND info LIKE 'SELECT SLEEP%'")
    trx = mysql_scalar(node, "SELECT COUNT(*) FROM information_schema.innodb_trx")
    dlw = mysql_scalar(node, "SELECT COUNT(*) FROM performance_schema.data_lock_waits")
    return {"ok": True, "detail": {"killed_processes": kill.get("killed"),
                                   "killed_waiting": waiting,
                                   "killed_stuck_trx": stuck,
                                   "killed_long_select": longq,
                                   "innodb_trx_left": trx,
                                   "data_lock_waits_left": dlw}}


SLOW_SQL_AMPLIFIED = (
    "SELECT COUNT(*) /* {m} */ FROM t_txn a JOIN {seq} s ON s.n <= {mul} "
    "WHERE a.amount > 0 AND a.status <> 'VOID'"
).format(m=MARK_SLOW, seq=SEQ_TABLE, mul=5)


def _db_slow_query_inject(ctx: FaultContext) -> Dict[str, Any]:
    node = ctx.targets[0] if ctx.targets else MYSQL_PRIMARY
    ensure_dataset()
    loops = int(ctx.p("loops", 400))
    # 按时间收尾：慢查询告警的 for 是 3m，注入必须活得比"保持期 + for"更久，
    # 否则采样时故障已结束、告警已恢复（实测过这类 alert_coverage=False）。
    dur = float(ctx.p("duration_s", 480))
    sql = ctx.p("sql", SLOW_SQL_AMPLIFIED)
    if MARK_SLOW not in sql:
        sql = sql + " /* %s */" % MARK_SLOW
    docker_exec(node, _mysql_loop(node, sql, loops, MARK_SLOW, duration_s=dur),
                detach=True)
    return {"ok": True, "detail": {"loops": loops, "duration_s": dur, "sql": sql[:200]}}


def _db_slow_query_recover(ctx: FaultContext) -> Dict[str, Any]:
    node = ctx.targets[0] if ctx.targets else MYSQL_PRIMARY
    res = _kill_loop(node, MARK_SLOW)
    return {"ok": True, "detail": res}


def _db_conn_saturation_inject(ctx: FaultContext) -> Dict[str, Any]:
    node = ctx.targets[0] if ctx.targets else MYSQL_PRIMARY
    n = int(ctx.p("connections", 180))
    hold_s = int(ctx.p("hold_s", 1800))
    # 单次 docker exec 内用 shell 循环并发起 N 个长期连接（避免 N 次 exec 开销）。
    #
    # ⚠ 结尾必须是 `wait` 而**不是** `sleep`。
    # 踩过的坑：原实现结尾写 `sleep {hold_s}`，即"父 shell 挂住不退出"。
    # 但那样父 shell 从不回收它 fork 出去的后台 `mysql` 客户端 ——
    # 回滚时 `kill_by_marker` 用 SIGKILL 杀掉这些客户端，它们立刻变成**僵尸**，
    # 而父进程（一个 `sleep 1800`）还活着且永不 wait() → **僵尸持续存在最长 30 分钟**。
    # 实测后果：一次 `db_conn_saturation` → `cc-mysql-core` 累积 170 个僵尸
    # （父进程 = `sleep 1800`），把 `doctor` 的僵尸检查打红，
    # 也拖慢清理（清理按标记扫 /proc 是 O(进程数)）。
    # `init: true` 在这里帮不上 —— 它们不是孤儿，父进程还活着。
    # 改成 `wait`：既保持"会话挂住"（子进程是 SELECT SLEEP(hold_s)，自然挂住同样时长），
    # 又会在子进程终止时立刻收尸。
    cmd = (
        "for i in $(seq 1 {n}); do "
        "mysql -u{u} -p{p} -h127.0.0.1 {db} -e "
        "\"SELECT SLEEP({s}) /* {m} */\" >/dev/null 2>&1 & "
        "done; wait"
    ).format(n=n, u=MYSQL_APP_USER, p=MYSQL_APP_PWD, db=MYSQL_DB, s=hold_s, m=MARK_CONN)
    docker_exec(node, cmd, detach=True)
    # 每个 mysql 客户端都要 fork + 建连，180 个串行起来需要十几秒才到峰值；
    # 等不够会让"信号是否可以观测"的判定变成随机结果。
    time.sleep(int(ctx.p("settle_s", 18)))
    total = mysql_scalar(node, "SELECT COUNT(*) FROM information_schema.processlist")
    maxconn = mysql_scalar(node, "SELECT @@max_connections")
    return {"ok": True, "recover_hint": {"node": node},
            "detail": {"requested": n, "total_connections": total, "max_connections": maxconn}}


def _db_conn_saturation_recover(ctx: FaultContext) -> Dict[str, Any]:
    node = ctx.recover_hint.get("node") or (ctx.targets[0] if ctx.targets else MYSQL_PRIMARY)
    kill = kill_by_marker(node, MARK_CONN)
    # 服务端兜底：清掉仍在跑的 SELECT SLEEP（注释在 processlist.info 里会被丢掉，
    # 所以只能用 command/info 前缀这类真实字段匹配）
    sleeping = mysql_kill_sessions(node, "command='Query' AND info LIKE 'SELECT SLEEP(%'")
    left = mysql_scalar(node, "SELECT COUNT(*) FROM information_schema.processlist")
    return {"ok": True, "detail": {"killed_processes": kill.get("killed"),
                                   "killed_sessions": sleeping,
                                   "connections_left": left}}


def _db_cpu_stress_inject(ctx: FaultContext) -> Dict[str, Any]:
    """
    数据库 CPU 压力。

    刻意用"单条不重、但并行度很高"的查询（30 万 × 8 行扫描 ≈ 0.5-1s）：
    重查询（交叉连接 20×20）单条要十几秒，并行度上不去，Threads_running 只到 2-3，
    打不出"CPU 资源不足"；轻查询 × 16 路并行才能把 Threads_running 稳在 12 以上、
    同时把 DB CPU 压满。

    信号必须限定主库并抬高阈值：主库基线的 Threads_running ≈ 5
    （含 2 个 Binlog Dump 线程），副本约 2，用 max(...) > 2 会恒为真。
    """
    node = ctx.targets[0] if ctx.targets else MYSQL_PRIMARY
    ensure_dataset()
    loops = int(ctx.p("loops", 600))
    parallel = int(ctx.p("parallel", 16))
    dur = float(ctx.p("duration_s", 480))
    sql = ("SELECT COUNT(*) /* {m} */ FROM t_txn a JOIN {seq} b ON b.n <= 8 "
           "WHERE a.amount > 0 AND a.status <> 'VOID'").format(m=MARK_DBCPU, seq=SEQ_TABLE)
    docker_exec(node, _mysql_loop(node, sql, loops, MARK_DBCPU, parallel=parallel,
                                  duration_s=dur),
                detach=True)
    return {"ok": True, "detail": {"loops": loops, "parallel": parallel, "sql": sql[:160]}}


def _db_cpu_stress_recover(ctx: FaultContext) -> Dict[str, Any]:
    node = ctx.targets[0] if ctx.targets else MYSQL_PRIMARY
    res = _kill_loop(node, MARK_DBCPU)
    return {"ok": True, "detail": res}


def _db_disk_temp_tables_inject(ctx: FaultContext) -> Dict[str, Any]:
    """
    构造"排序/分组内存不足落盘"：临时把 tmp_table_size 调到 1M，
    再并行跑超宽 GROUP BY（临时表必然超过 1M → Created_tmp_disk_tables 增长）。

    单循环速率不足以让 3m 窗口的 rate 超过告警阈值，因此默认 4 路并行。
    """
    node = ctx.targets[0] if ctx.targets else MYSQL_PRIMARY
    ensure_dataset()
    orig_tmp = mysql_scalar(node, "SELECT @@tmp_table_size")
    orig_heap = mysql_scalar(node, "SELECT @@max_heap_table_size")
    orig_engine = mysql_scalar(node, "SELECT @@internal_tmp_mem_storage_engine")
    # ⚠ 必须把内部临时表引擎切回 MEMORY，否则 `tmp_table_size` / `max_heap_table_size`
    # **根本管不到内部临时表**：MySQL 8.0 默认 `internal_tmp_mem_storage_engine=TempTable`，
    # 其落盘由 `temptable_max_ram`(默认 1GB) 与 `temptable_use_mmap=1` 决定，
    # 于是"压小 tmp_table_size"几乎不产生 `Created_tmp_disk_tables`。
    # 实测：只压 size 时 [2m] 速率峰值 0.028 且 50s 后就归零；
    # 切到 MEMORY 后临时表受 min(tmp_table_size, max_heap_table_size)=1MB 约束，
    # 落盘成为稳定症状（告警 MySQLDiskTempTables 也因此可达）。
    mysql_sql(node, "SET GLOBAL internal_tmp_mem_storage_engine=MEMORY; "
                    "SET GLOBAL tmp_table_size=1048576; "
                    "SET GLOBAL max_heap_table_size=1048576;", user="root", pwd="rootpass")
    sql = ("SELECT customer_id, MD5(CONCAT(created_at, amount, status)) AS k, "
           "COUNT(*) /* {m} */ FROM t_txn "
           "GROUP BY customer_id, k ORDER BY 3 DESC LIMIT 5").format(m=MARK_TMP)
    loops = int(ctx.p("loops", 200))
    parallel = int(ctx.p("parallel", 4))
    dur = float(ctx.p("duration_s", 480))
    docker_exec(node, _mysql_loop(node, sql, loops, MARK_TMP, parallel=parallel,
                                  duration_s=dur),
                detach=True)
    return {"ok": True, "recover_hint": {"node": node,
                                         "tmp_table_size": orig_tmp,
                                         "max_heap_table_size": orig_heap,
                                         "internal_tmp_mem_storage_engine": orig_engine},
            "detail": {"loops": loops, "parallel": parallel,
                       "tmp_table_size_forced_mb": 1,
                       "tmp_engine_forced": "MEMORY"}}


def _db_disk_temp_tables_recover(ctx: FaultContext) -> Dict[str, Any]:
    node = ctx.recover_hint.get("node") or (ctx.targets[0] if ctx.targets else MYSQL_PRIMARY)
    res = _kill_loop(node, MARK_TMP)
    tmp = ctx.recover_hint.get("tmp_table_size") or "16777216"
    heap = ctx.recover_hint.get("max_heap_table_size") or "16777216"
    eng = ctx.recover_hint.get("internal_tmp_mem_storage_engine") or "TempTable"
    mysql_sql(node, "SET GLOBAL tmp_table_size=%s; SET GLOBAL max_heap_table_size=%s; "
                    "SET GLOBAL internal_tmp_mem_storage_engine=%s;"
             % (tmp, heap, eng), user="root", pwd="rootpass")
    return {"ok": True, "detail": {"killed": res,
                                   "restored": {"tmp_table_size": tmp,
                                                "max_heap_table_size": heap,
                                                "internal_tmp_mem_storage_engine": eng}}}


def _db_primary_readonly_inject(ctx: FaultContext) -> Dict[str, Any]:
    """
    主库被误置只读（典型的"变更/配置漂移"根因）：
    写请求 503，读请求（副本）照常成功 —— 极难通过"服务是不是挂了"判断。
    """
    node = ctx.targets[0] if ctx.targets else MYSQL_PRIMARY
    prev_ro = mysql_scalar(node, "SELECT @@read_only")
    prev_sro = mysql_scalar(node, "SELECT @@super_read_only")
    mysql_sql(node, "SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=ON;",
              user="root", pwd="rootpass")
    now = mysql_scalar(node, "SELECT @@read_only")
    return {"ok": True,
            "recover_hint": {"node": node, "read_only": prev_ro, "super_read_only": prev_sro},
            "detail": {"read_only_before": prev_ro, "read_only_now": now}}


def _db_primary_readonly_recover(ctx: FaultContext) -> Dict[str, Any]:
    node = ctx.recover_hint.get("node") or (ctx.targets[0] if ctx.targets else MYSQL_PRIMARY)
    mysql_sql(node, "SET GLOBAL read_only=OFF; SET GLOBAL super_read_only=OFF;",
              user="root", pwd="rootpass")
    return {"ok": True, "detail": {"read_only_now": mysql_scalar(node, "SELECT @@read_only")}}


LAG_WRITER_MARKER = MARK_LAG


def _db_replica_lag_inject(ctx: FaultContext) -> Dict[str, Any]:
    """
    用 MySQL 原生 SOURCE_DELAY 制造可控复制延迟（两个线程都保持 Running，
    Seconds_Behind_Master 稳定在 delay 附近）——比 STOP SQL_THREAD 更真实：
    副本仍在跑，只是数据陈旧。

    注意：Seconds_Behind_Master 只有在**有新事件可回放**时才非 0。
    因此这里同时起一个后台写流（chaos-lag-writer），
    否则"延迟"不可观测（读到的永远是 0）。
    """
    delay = int(ctx.p("delay_s", 60))
    secs = int(ctx.p("seconds", 900))
    hint: Dict[str, Any] = {"prev_delay": {}, "writer_node": MYSQL_PRIMARY}
    for node in ctx.targets:
        prev = mysql_scalar(node, "SELECT @@source_delay")
        hint["prev_delay"][node] = prev
        mysql_sql(node,
                  "STOP REPLICA; CHANGE REPLICATION SOURCE TO SOURCE_DELAY=%d; START REPLICA;"
                  % delay, user="root", pwd="rootpass")

    # 后台写流：保证副本持续有新事件可（延迟）回放
    writer_sql = ("INSERT INTO t_txn (customer_id, amount, status) VALUES (2, 9.99, 'SETTLED') "
                  "/* %s */" % LAG_WRITER_MARKER)
    docker_exec(
        MYSQL_PRIMARY,
        "for i in $(seq 1 {n}); do mysql -u{u} -p{p} -h127.0.0.1 {db} -e {sql} "
        ">/dev/null 2>&1; sleep 0.5; done".format(
            n=min(int(secs * 2), 4000), u=MYSQL_APP_USER, p=MYSQL_APP_PWD,
            db=MYSQL_DB, sql=_shq(writer_sql)),
        detach=True)

    delays = {n: mysql_scalar(
        n, "SELECT DESIRED_DELAY FROM performance_schema.replication_applier_configuration LIMIT 1")
        for n in ctx.targets}
    return {"ok": True, "recover_hint": hint,
            "detail": {"delay_s": delay, "source_delay_now": delays,
                       "writer": LAG_WRITER_MARKER}}


def _db_replica_lag_recover(ctx: FaultContext) -> Dict[str, Any]:
    kill = kill_by_marker(MYSQL_PRIMARY, LAG_WRITER_MARKER)
    detail = {}
    for node in ctx.targets:
        mysql_sql(node,
                  "STOP REPLICA; CHANGE REPLICATION SOURCE TO SOURCE_DELAY=0; START REPLICA;",
                  user="root", pwd="rootpass")
        detail[node] = mysql_scalar(
            node, "SELECT DESIRED_DELAY FROM performance_schema.replication_applier_configuration LIMIT 1")

    # 关键：把 SOURCE_DELAY 归零只停止了"人为延迟"，副本还要把积压的中继日志回放完，
    # Seconds_Behind_Source 才会真正回落。等它追平再返回，
    # 否则外层"恢复校验"会在追平期间误判为未恢复。
    # 进度用 Prometheus 的 mysql_slave_status_seconds_behind_master 观测
    # （SHOW REPLICA STATUS 的字段在 performance_schema 里没有等价列）。
    caught: Dict[str, Any] = {}
    deadline = time.time() + int(ctx.p("catchup_timeout_s", 150))
    while time.time() < deadline:
        vals: Dict[str, float] = {}
        for r in prom_query("mysql_slave_status_seconds_behind_master"):
            try:
                vals[str(r["metric"].get("db_node") or "?")] = float(r["value"][1])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        caught = vals or caught
        if vals and max(vals.values()) <= 2.0:
            break
        time.sleep(5)

    return {"ok": True, "detail": {"source_delay": detail,
                                   "killed_writer_processes": kill.get("killed"),
                                   "seconds_behind_after_catchup": caught}}


def _db_replica_io_stop_inject(ctx: FaultContext) -> Dict[str, Any]:
    for node in ctx.targets:
        mysql_sql(node, "STOP REPLICA IO_THREAD;", user="root", pwd="rootpass")
    return {"ok": True, "detail": {"io_thread_stopped": ctx.targets}}


def _db_replica_io_stop_recover(ctx: FaultContext) -> Dict[str, Any]:
    for node in ctx.targets:
        mysql_sql(node, "START REPLICA IO_THREAD;", user="root", pwd="rootpass")
    return {"ok": True}


def _db_replica_kill_inject(ctx: FaultContext) -> Dict[str, Any]:
    for node in ctx.targets:
        docker(["kill", node], timeout=60)
    return {"ok": True, "detail": {"killed": ctx.targets}}


def _db_replica_kill_recover(ctx: FaultContext) -> Dict[str, Any]:
    detail = {}
    for node in ctx.targets:
        docker(["start", node], timeout=120)
        detail[node] = container_state(node)
    return {"ok": True, "detail": detail}


# ═════════════════════════════════════════════════════════════════════════════
# 资源耗尽（跨层，需叠加压测）
# ═════════════════════════════════════════════════════════════════════════════

def _res_cluster_cpu_inject(ctx: FaultContext) -> Dict[str, Any]:
    workers = int(ctx.p("workers", 4))
    secs = int(ctx.p("seconds", 900))
    for c in ctx.targets:
        docker_exec(c, "pkill -f stress-ng 2>/dev/null; true")
        docker_exec(c,
                    "nohup stress-ng --cpu {w} --cpu-method matrixprod --timeout {s}s "
                    ">/tmp/chaos-res-cpu.log 2>&1 &".format(w=workers, s=secs),
                    detach=True)
    return {"ok": True, "detail": {"replicas": ctx.targets, "cpu_workers": workers}}


def _res_cluster_memory_inject(ctx: FaultContext) -> Dict[str, Any]:
    mb = int(ctx.p("mb", 220))
    secs = int(ctx.p("seconds", 900))
    for c in ctx.targets:
        docker_exec(c, "pkill -f stress-ng 2>/dev/null; true")
        docker_exec(c,
                    "nohup stress-ng --vm 1 --vm-bytes {m}M --vm-keep --vm-hang 0 "
                    "--timeout {s}s >/tmp/chaos-res-mem.log 2>&1 &".format(m=mb, s=secs),
                    detach=True)
    return {"ok": True, "detail": {"replicas": ctx.targets, "vm_bytes_mb": mb}}


def _res_stress_recover(ctx: FaultContext) -> Dict[str, Any]:
    detail = {}
    for c in ctx.targets:
        detail[c] = kill_by_marker(c, "stress-ng")
    return {"ok": True, "detail": detail}


def _res_db_memory_inject(ctx: FaultContext) -> Dict[str, Any]:
    """
    数据库侧内存不足：压小排序/连接/临时表缓冲后跑"大分组 + 大排序"。

    注意：只用 `ORDER BY` 是不够的 —— 实测 `ORDER BY` 走 filesort，
    既不增加 `Created_tmp_disk_tables` 也（在 LIMIT 优化下）不产生 merge 文件。
    必须带 **GROUP BY** 才会生成内部临时表，才能在缓冲被压小后落盘，
    让 `Created_tmp_disk_tables` 与慢查询同时增长（两者都作为信号）。

    还必须一起压小 **`max_heap_table_size`**（这是踩过的坑）：
    内部临时表是否落盘取决于 `min(tmp_table_size, max_heap_table_size)`，
    只压前者而后者仍是默认 16M 时，临时表照样留在内存里 →
    `Created_tmp_disk_tables` 几乎不增长，实测告警与信号都观测不到
    （对照 `db_disk_temp_tables` 两个都压，速率能到 0.10/s 以上）。
    故障必须**可观测**，否则"验证通过"只是自欺。
    """
    node = ctx.targets[0] if ctx.targets else MYSQL_PRIMARY
    ensure_dataset()
    orig = {k: mysql_scalar(node, "SELECT @@%s" % k)
            for k in ("sort_buffer_size", "join_buffer_size", "tmp_table_size",
                      "max_heap_table_size", "internal_tmp_mem_storage_engine")}
    # 同 db_disk_temp_tables：必须切到 MEMORY 引擎，否则 tmp_table_size /
    # max_heap_table_size 对内部临时表无效（MySQL 8.0 默认 TempTable 引擎），
    # "落盘"症状就出不来（实测 [2m] 速率 < 0.03 且很快归零）。
    mysql_sql(node, "SET GLOBAL internal_tmp_mem_storage_engine=MEMORY; "
                    "SET GLOBAL sort_buffer_size=262144; SET GLOBAL join_buffer_size=131072; "
                    "SET GLOBAL tmp_table_size=1048576; "
                    "SET GLOBAL max_heap_table_size=1048576;",
              user="root", pwd="rootpass")
    sql = ("SELECT a.status, MD5(CONCAT(a.txn_id, a.amount, b.n)) AS k, COUNT(*) /* {m} */ "
           "FROM t_txn a JOIN {seq} b ON b.n <= 2 "
           "GROUP BY a.status, k ORDER BY 3 DESC").format(m=MARK_DBMEM, seq=SEQ_TABLE)
    loops = int(ctx.p("loops", 800))
    parallel = int(ctx.p("parallel", 4))
    dur = float(ctx.p("duration_s", 480))
    docker_exec(node, _mysql_loop(node, sql, loops, MARK_DBMEM, parallel=parallel,
                                  duration_s=dur),
                detach=True)
    return {"ok": True, "recover_hint": {"node": node, "orig": orig},
            "detail": {"loops": loops, "parallel": parallel, "duration_s": dur,
                       "slow_query_logging": "long_query_time=1, min_examined_row_limit=10000"}}


def _res_db_memory_recover(ctx: FaultContext) -> Dict[str, Any]:
    node = ctx.recover_hint.get("node") or (ctx.targets[0] if ctx.targets else MYSQL_PRIMARY)
    res = _kill_loop(node, MARK_DBMEM)
    orig = ctx.recover_hint.get("orig") or {}
    if orig:
        mysql_sql(node, "SET GLOBAL sort_buffer_size=%s; SET GLOBAL join_buffer_size=%s; "
                        "SET GLOBAL tmp_table_size=%s; SET GLOBAL max_heap_table_size=%s; "
                        "SET GLOBAL internal_tmp_mem_storage_engine=%s;"
                  % (orig.get("sort_buffer_size") or 262144,
                     orig.get("join_buffer_size") or 262144,
                     orig.get("tmp_table_size") or 16777216,
                     orig.get("max_heap_table_size") or 16777216,
                     orig.get("internal_tmp_mem_storage_engine") or "TempTable"),
                  user="root", pwd="rootpass")
    return {"ok": True, "detail": {"killed": res, "restored": orig}}


# ═════════════════════════════════════════════════════════════════════════════
# 注册表
# ═════════════════════════════════════════════════════════════════════════════

FAULTS: Dict[str, Fault] = {}


def _register(f: Fault) -> Fault:
    FAULTS[f.id] = f
    return f


# ── 应用层 ────────────────────────────────────────────────────────────────
_register(Fault(
    id="app_kill_replica", layer="application", category="资源",
    title="强杀一个应用副本（模拟容器崩溃/OOM 后消失）",
    description="docker kill 一个 payment-app 副本：进程立即消失，网关摘除该后端，"
                "集群在册副本数从 3 降到 2，剩余副本承接全部流量。",
    inject=_app_kill_inject, recover=_app_kill_recover,
    selector="roundrobin",
    signals=[
        'count(up{job="payment-app"} == 1) < 3',
        'count(probe_success{job="blackbox-app-replica"} == 0) > 0',
    ],
    recovered_signals=['count(up{job="payment-app"} == 1) < 3'],
    expected_root_cause="应用层某个副本容器被强制终止（崩溃/OOM Kill），集群副本数下降",
    ontology_terms=["app:payment-app", "env:container", "rc:replica-loss"],
    alert_names=["AppReplicaCountLow"],
    blast_radius="该副本承载的请求被 LB 转移到其余副本；集群吞吐下降约 1/3",
))

_register(Fault(
    id="app_pause_replica", layer="application", category="资源",
    title="暂停一个应用副本（假死：进程在但完全不响应）",
    description="docker pause 冻结 cgroup 内所有进程：TCP 连接能建立但永远不返回，"
                "比 kill 更难判断——容器状态仍是 Up，端口仍在监听。",
    inject=_app_pause_inject, recover=_app_pause_recover,
    selector="roundrobin",
    signals=[
        'count(probe_success{job="blackbox-app-replica"} == 0) > 0',
        'count(up{job="payment-app"} == 0) > 0',
    ],
    recovered_signals=['count(probe_success{job="blackbox-app-replica"} == 0) > 0'],
    expected_root_cause="应用副本容器被挂起（假死），进程存在但无法处理请求",
    ontology_terms=["app:payment-app", "env:container", "rc:replica-hang", "rc:replica-loss"],
    alert_names=["AppReplicaProbeFailing"],
    blast_radius="LB 健康检查超时后摘除该副本；期间经过该后端的请求超时",
))

_register(Fault(
    id="app_stop_replica", layer="application", category="资源",
    title="优雅下线一个应用副本",
    description="docker stop：先 SIGTERM 再 SIGKILL，模拟滚动发布/缩容导致的一个副本离线。",
    inject=_app_stop_inject, recover=_app_kill_recover,
    selector="last",
    signals=['count(up{job="payment-app"} == 1) < 3'],
    recovered_signals=['count(up{job="payment-app"} == 1) < 3'],
    expected_root_cause="应用副本因发布/缩容下线，集群副本数不足",
    ontology_terms=["app:payment-app", "env:container", "rc:replica-loss"],
    alert_names=["AppReplicaCountLow"],
    blast_radius="集群容量下降 1/3",
))

_register(Fault(
    id="app_cpu_stress", layer="runtime", category="资源",
    title="单副本 CPU 打满（触发 cgroup CPU 配额节流）",
    description="在副本内跑 stress-ng --cpu 4；容器配额 cpus=0.5，"
                "必然出现 nr_throttled 增长，请求处理被强行排队。",
    inject=_app_cpu_stress_inject, recover=_app_cpu_stress_recover,
    selector="roundrobin",
    default_params={"workers": 4, "seconds": 900},
    params_help={"workers": "stress-ng CPU 负载进程数", "seconds": "压力持续秒数"},
    signals=[
        'max(rate(cc_container_cpu_throttled_seconds_total[1m])) > 0.1',
        'max(rate(cc_container_cpu_nr_throttled_total[1m])) > 1',
    ],
    # 恢复校验必须用"量级阈值"而不是 > 0：实测空载/轻载下
    # rate(throttled_seconds[1m]) ≈ 0.001~0.003、rate(nr_throttled[1m]) ≈ 0.02~0.07
    # 恒 > 0，用 > 0 判定会永远认为"未恢复"。
    recovered_signals=['max(rate(cc_container_cpu_throttled_seconds_total[1m])) > 0.1'],
    expected_root_cause="应用副本容器 CPU 资源不足，被 cgroup cpu quota 节流",
    ontology_terms=["app:payment-app", "env:container", "metric:cpu-throttle", "rc:cpu-throttle"],
    alert_names=["AppContainerCpuThrottled", "AppReplicaLatencySkew"],
    needs_stress=True,
    blast_radius="仅该副本变慢（集群内延迟不均衡）",
))

_register(Fault(
    id="app_memory_stress", layer="runtime", category="资源",
    title="单副本容器内存耗尽（逼近 cgroup 上限，OOM 前兆）",
    description="在副本内申请 200MB 内存；容器 mem_limit=256m，"
                "working set 迅速冲到上限附近，随时可能触发 OOM Killer。",
    inject=_app_memory_stress_inject, recover=_app_memory_stress_recover,
    selector="roundrobin",
    default_params={"mb": 200, "seconds": 900},
    params_help={"mb": "申请内存 MB", "seconds": "压力持续秒数"},
    signals=[
        'max(cc_container_memory_working_set_bytes / (cc_container_memory_limit_bytes > 0)) > 0.8',
    ],
    recovered_signals=[
        'max(cc_container_memory_working_set_bytes / (cc_container_memory_limit_bytes > 0)) > 0.8',
    ],
    expected_root_cause="应用副本容器内存不足（内存泄漏/大对象），逼近 cgroup 内存上限",
    ontology_terms=["app:payment-app", "env:container", "metric:mem-pressure", "rc:oom-kill"],
    alert_names=["AppContainerMemoryPressure"],
    blast_radius="该副本随时被 OOM Kill，进而退化为副本丢失",
))

_register(Fault(
    id="app_oom_kill", layer="runtime", category="资源",
    title="构造真实容器 OOMKill（内存上限低于实际用量 + 内存申请）",
    description="先把 cgroup 内存上限压到 48MB（应用稳态约 44MB），再申请 256MB："
                "内核 OOM Killer 杀死容器内进程，容器退出并被 restart 策略拉起，"
                "docker inspect 的 OOMKilled=true、RestartCount 递增；"
                "cgroup memory.events 的 oom_kill 计数同步 +1。",
    inject=_app_oom_kill_inject, recover=_app_oom_kill_recover,
    selector="roundrobin",
    default_params={"shrink_mb": 32, "vm_mb": 512},
    params_help={"shrink_mb": "压缩后的 cgroup 内存上限 MB", "vm_mb": "内存申请量 MB"},
    signals=[
        # ★ 判据必须对齐场景名：名字承诺"真实 OOMKill"，就必须以**杀死计数器**为准。
        # 原先三条判据全是"压力/重启"类，其中压力指标在"没杀成"时也可能不成立
        # （实测该场景唯一一次未覆盖，就是既没杀、也没压力，却靠别的信号通过了验证）。
        # ★ 只保留**容器外**、且不会被重启清零的证据。
        # 实测结论：容器内的 cc_container_oom_kill_total 在容器被杀重启后从 0 开始，
        # Prometheus 眼里这条序列"生来是 0" → increase() 永远为 0（注入层确认 OOMKilled
        # =true 的同时，三个实例的该计数器全为 0）；而 "up 掉 0" 的瞬态在 15s 抓取下
        # 也抓不到（Node 应用几秒就起来）。因此判据改成读 Docker API 的 RestartCount
        # （见 monitoring/container/exporter.py），它单调且不随容器重启消失。
        'increase(cc_container_restart_count{container=~"rca-agent-payment-app-.*"}[5m]) > 0',
    ],
    # 恢复：只看"有没有被杀过"会永远为真（计数器只增），因此恢复判据用**重启后回到健康**
    # 恢复判据的语义是"**要消失的症状**"（必须变为假才算恢复），不是"恢复后应成立的条件"。
    # 我一度写成 `count(up == 1) >= 3`（健康时为真）→ 永远等不到它为假 → 超时判失败
    # （实测：result 里明明已经是 3，检查却 waited=150s ok=False）。
    recovered_signals=['count(up{job="payment-app"} == 1) < 3'],
    expected_root_cause="应用副本容器被内核 OOM Kill 后重启（内存超限）",
    ontology_terms=["app:payment-app", "env:container", "rc:oom-kill", "rc:replica-loss"],
    # AppContainerOOMKilled 是**新增**的告警：应用层原先只有"内存逼近上限"
    # 这一种证据，OOM Kill 后容器重启、内存回落 → 压力告警消失，
    # "被 OOM 杀过"在指标上不可观测。现由 cgroup memory.events 的 oom_kill
    # 只增计数器支撑（见 monitoring/prometheus/alerts.yml）。
    alert_names=["AppContainerOOMKilled", "AppContainerMemoryPressure", "AppReplicaCountLow"],
    blast_radius="该副本重启期间流量转移到其余副本；重启后连接池/缓存冷启动",
))

_register(Fault(
    id="app_network_delay", layer="application", category="依赖",
    title="单副本网络延迟注入（tc netem 300ms）",
    description="在副本 eth0 上加 300ms 延迟：DB 往返、探针、LB 转发全部变慢，"
                "但容器 CPU/内存指标完全正常 —— 典型的“指标看不出问题”。",
    inject=_app_network_delay_inject, recover=_app_network_recover,
    selector="roundrobin",
    default_params={"delay_ms": 300, "jitter_ms": 30},
    params_help={"delay_ms": "单向延迟毫秒", "jitter_ms": "抖动毫秒"},
    signals=[
        'max(probe_duration_seconds{job="blackbox-app-replica"}) > 0.2',
        'max(histogram_quantile(0.99, sum(rate(cc_http_request_duration_seconds_bucket[2m])) by (le, app_instance))) > 0.25',
    ],
    recovered_signals=['max(probe_duration_seconds{job="blackbox-app-replica"}) > 0.2'],
    expected_root_cause="应用副本网络链路延迟异常（网络/中间设备），非资源不足",
    ontology_terms=["app:payment-app", "env:container", "metric:net-delay", "rc:net-fault"],
    alert_names=["AppReplicaLatencySkew"],
    needs_stress=True,
    blast_radius="经过该副本的请求端到端延迟 +300ms×往返次数",
))

_register(Fault(
    id="app_network_loss", layer="application", category="依赖",
    title="单副本网络丢包注入（tc netem loss 40%）",
    description="40% 丢包：TCP 重传导致长尾延迟剧增、部分探针失败、"
                "DB 连接可能被重置。",
    inject=_app_network_loss_inject, recover=_app_network_recover,
    selector="roundrobin",
    default_params={"loss_pct": 40},
    params_help={"loss_pct": "丢包百分比"},
    signals=[
        'max(probe_duration_seconds{job="blackbox-app-replica"}) > 0.3',
        'count(probe_success{job="blackbox-app-replica"} == 0) > 0',
    ],
    recovered_signals=['count(probe_success{job="blackbox-app-replica"} == 0) > 0'],
    expected_root_cause="应用副本网络丢包严重，链路质量故障",
    ontology_terms=["app:payment-app", "env:container", "metric:net-loss", "rc:net-fault"],
    # 实测覆盖（tools/alert_coverage_check.py，hold=240s）：声明 `AppReplicaProbeFailing`
    # 时**不响** —— 40% 丢包下 blackbox 的副本探针是低频单次 GET，仍可能成功，
    # 凑不满 `probe_success == 0` 的 `for: 2m`；探针此时会偶发失败（信号里能观测到），
    # 但不足以形成告警。真正稳定可观测的是 `AppReplicaLatencySkew`
    # （丢包引起重传 → 该副本 P99 相对其余副本偏斜）。
    alert_names=["AppReplicaLatencySkew"],
    needs_stress=True,
    blast_radius="该副本成功率骤降，LB 视情况摘除",
))

_register(Fault(
    id="app_gateway_stop", layer="application", category="依赖",
    title="停掉应用层网关（整个应用入口不可用）",
    description="docker stop cc-app-gateway：副本全部健康、指标全部正常，"
                "但外部请求完全打不进来 —— 必须用黑盒探活才能发现。",
    inject=_app_gateway_stop_inject, recover=_app_gateway_stop_recover,
    selector="gateway",
    signals=['count(probe_success{job="blackbox-gateway"} == 0) > 0'],
    recovered_signals=['count(probe_success{job="blackbox-gateway"} == 0) > 0'],
    expected_root_cause="应用层网关（负载均衡入口）不可用，而非业务副本故障",
    ontology_terms=["app:payment-app", "env:gateway", "rc:gateway-down"],
    alert_names=["AppGatewayUnreachable"],
    blast_radius="整个应用层对外不可用（P0），但副本自身指标全部正常",
))

# ── 数据库层 ──────────────────────────────────────────────────────────────
_register(Fault(
    id="db_row_lock_hold", layer="database", category="数据",
    title="持有 t_txn 排他行锁（写事务被阻塞）",
    description="后台事务 SELECT ... FOR UPDATE 后长睡：所有写 t_txn 的语句进入锁等待，"
                "应用连接池被占满 → P99 飙升。",
    inject=_db_row_lock_inject, recover=_db_row_lock_recover,
    selector="mysql-primary",
    default_params={"seconds": 900, "victims": 4},
    params_help={"seconds": "持锁秒数", "victims": "受害者写会话数"},
    signals=[
        # 必须用**速率阈值**而不是 `> 0`：`increase(...) > 0` 在空闲基线就可能成立
        # （应用正常流量也会有零星行锁等待，实测 baseline 就有 1 条序列 → 信号恒真、
        # 失去判别力）。实测分离度：空闲基线 **0**，故障期 0.038–0.23 次/秒，
        # 因此取 0.02 —— 与告警规则 MySQLRowLockContention 的阈值保持一致。
        # （另：用 `rate[2m]` 而非 `rate[1m]`，避免"等待发生在注入后稍晚"的假阴性。）
        # 窗口必须短于验证等待（verify 只等 ~75s）：[2m] 永远红不了 → [30s]
        'rate(mysql_global_status_innodb_row_lock_waits[1m]) > 0.02',
        'max(cc_mysql_pool_active) > 4',
    ],
    # 恢复判据用 1 分钟窗口：回滚后要在 ~60s 内变假，否则超过恢复预算
    recovered_signals=['rate(mysql_global_status_innodb_row_lock_waits[1m]) > 0.02'],
    expected_root_cause="InnoDB 行锁等待：长事务持有 t_txn 排他锁阻塞写入，连接池被占满",
    ontology_terms=["db:mysql-core", "table:t_txn", "ds:pay", "rc:row-lock", "metric:conn-exhaust"],
    # 补进 alert_coverage 实测发现"响了但未声明"的告警：
    # 声明不全的后果不是好看不好看 —— 端到端取"本次新响的告警"来拼诊断输入时，
    # 未声明的告警会被判成"别人的残留"，于是**真的证据被丢掉**。
    alert_names=["MySQLRowLockContention", "MySQLConnectionPoolExhausted",
                 "AppHighLatencyP99", "AppHighErrorRate",
                 "MySQLThreadsRunningHigh", "MySQLPoolQueueBacklog"],
    needs_stress=True,
    blast_radius="写路径全面变慢并级联到应用层 P99",
))

_register(Fault(
    id="db_slow_query_flood", layer="database", category="数据",
    title="持续制造慢查询（全表扫描 + 连接放大）",
    description="反复执行对 t_txn 的无索引放大扫描：slow_queries 持续增长，"
                "DB CPU 上升；叠加应用并发后连接池排队。",
    inject=_db_slow_query_inject, recover=_db_slow_query_recover,
    selector="mysql-primary",
    default_params={"loops": 400},
    params_help={"loops": "重复执行次数"},
    # ⚠ 信号必须**高于环境基线**，并**限定在注入的主库**上。
    # 踩过的坑：原来写的是 `rate(slow_queries[2m]) > 0` / `[1m] > 0`，但
    # `long_query_time=0.1s` 很激进 —— 应用自身的读查询与我们自己的
    # `SELECT COUNT(*) FROM t_txn`（30 万行）都会被记为慢查询，
    # 于是**空闲基线就有 ~0.24/s**（副本 ~0.01–0.02）。
    # 后果有两层，都很隐蔽：
    #   · 注入信号 `> 0` 被基线满足 → 校验恒真，等于没校验；
    #   · 恢复信号 `> 0` 永远不可能变假 → 永远"回滚失败"，此前通过纯属采样空窗的运气。
    # 实测：主库空闲 ~0.24、本故障期 ~0.69 → 阈值取 0.45（两侧 ~1.5–1.9 倍余量）。
    signals=[
        # ⚠ 窗口必须**短于验证等待**（verify 只等 ~75s），否则这条判据结构上永远不可能成立。
        # 原来是 [2m]：2 分钟窗口要铺满才能算出速率 → 75s 内必然为假，
        # 于是"注入成功但未观测到信号"，看起来像故障没造成、实际是判据设计问题。
        # 改成 [30s]：注入后约 30~40s 即可成立，且语义不变（>0.45/s ≈ 27 次/分钟）。
        # 留 30s 窗口仍有余量：实测该场景慢查询计数器约 0.9/s 增长。
        'max(rate(mysql_global_status_slow_queries{db_node="primary"}[30s])) > 0.15',
    ],
    recovered_signals=[
        'max(rate(mysql_global_status_slow_queries{db_node="primary"}[1m])) > 0.45',
    ],
    expected_root_cause="t_txn 上存在无索引全表扫描的慢 SQL，数据库性能退化",
    ontology_terms=["db:mysql-core", "table:t_txn", "rc:slow-sql"],
    alert_names=["MySQLSlowQueries", "AppHighLatencyP99"],
    needs_stress=True,
    blast_radius="主库 CPU/IO 上升，所有依赖该库的写入变慢并复制到副本",
))

_register(Fault(
    id="db_conn_saturation", layer="database", category="配置",
    title="耗尽 MySQL max_connections（连接数打满）",
    description="持续建立并持有大量空闲连接，直到 threads_connected 逼近 "
                "max_connections=200；后续新连接被直接拒绝（Too many connections）。",
    inject=_db_conn_saturation_inject, recover=_db_conn_saturation_recover,
    selector="mysql-primary",
    default_params={"connections": 170},
    params_help={"connections": "持有的连接数"},
    signals=[
        'mysql_global_status_threads_connected / mysql_global_variables_max_connections > 0.85',
    ],
    recovered_signals=[
        'mysql_global_status_threads_connected / mysql_global_variables_max_connections > 0.85',
    ],    expected_root_cause="MySQL 连接资源耗尽（max_connections 打满），新连接被拒绝",
    ontology_terms=["db:mysql-core", "ds:pay", "rc:conn-exhaust", "metric:conn-exhaust"],
    alert_names=["MySQLTooManyConnections", "MySQLConnectionRefused", "MySQLThreadsRunningHigh"],
    blast_radius="所有需要新建连接的应用副本受影响；已建立连接的应用不受影响",
))

_register(Fault(
    id="db_cpu_stress", layer="database", category="资源",
    title="数据库 CPU 压力（交叉连接放大查询）",
    description="反复执行 300k × 20 × 20 的交叉连接聚合，把 DB CPU 打满，"
                "threads_running 持续偏高。",
    inject=_db_cpu_stress_inject, recover=_db_cpu_stress_recover,
    selector="mysql-primary",
    default_params={"loops": 300},
    params_help={"loops": "重复执行次数", "parallel": "并行循环数"},
    signals=[
        'mysql_global_status_threads_running{db_node="primary"} > 12',
    ],
    recovered_signals=[
        'mysql_global_status_threads_running{db_node="primary"} > 12',
    ],
    expected_root_cause="数据库服务器 CPU 资源不足，查询排队等待 CPU",
    ontology_terms=["db:mysql-core", "metric:db-cpu", "rc:db-cpu-starve"],
    alert_names=["MySQLThreadsRunningHigh"],
    blast_radius="主库所有查询变慢，读副本因复制单线程回放而延迟上升",
))

_register(Fault(
    id="db_disk_temp_tables", layer="database", category="配置",
    title="排序/分组内存不足落盘（磁盘临时表激增）",
    description="把 tmp_table_size/max_heap_table_size 降到 1MB 后跑超宽 GROUP BY："
                "临时表无法驻留内存，Created_tmp_disk_tables 持续增长，磁盘 IO 上升。",
    inject=_db_disk_temp_tables_inject, recover=_db_disk_temp_tables_recover,
    selector="mysql-primary",
    default_params={"loops": 120},
    params_help={"loops": "重复执行次数"},
    # 阈值 0.05：故障期实测 [2m] 速率 ~0.105（空闲基线恒为 0），
    # 原阈值 0.15 **高于故障可达上限** → 注入期信号永远等不到
    # （这正是"21/21"里 db_disk_temp_tables 被记为"0 信号"的原因，见 §11.21 第 9b 项）。
    # 与告警规则 MySQLDiskTempTables 的阈值保持一致。
    # 窗口必须短于验证等待（verify 只等 ~75s）：[2m] 永远红不了 → [30s]
    signals=['rate(mysql_global_status_created_tmp_disk_tables[30s]) > 0.05'],
    recovered_signals=['rate(mysql_global_status_created_tmp_disk_tables[1m]) > 0.05'],
    expected_root_cause="数据库排序/分组内存配置过小（tmp_table_size），临时表落盘导致 IO 瓶颈",
    ontology_terms=["db:mysql-core", "rc:tmp-disk", "con:db-tmp-table"],
    alert_names=["MySQLDiskTempTables"],
    blast_radius="涉及排序/分组的查询整体变慢，磁盘 IO 与延迟上升",
))

_register(Fault(
    id="db_primary_readonly", layer="database", category="配置",
    title="主库被置为只读（读写分离下最隐蔽的配置漂移）",
    description="SET GLOBAL read_only=ON：所有写请求立即失败（应用 503），"
                "而读请求走副本依旧成功 —— “服务没挂但交易全失败”。",
    inject=_db_primary_readonly_inject, recover=_db_primary_readonly_recover,
    selector="mysql-primary",
    signals=[
        'rate(cc_http_requests_total{path="/txn",status="503"}[2m]) > 0',
        'rate(cc_txn_total{status="failed"}[2m]) > 0',
    ],
    recovered_signals=['rate(cc_http_requests_total{path="/txn",status="503"}[1m]) > 0'],
    expected_root_cause="主库被置为只读（配置变更/切换未回滚），写路径全部失败而读路径正常",
    ontology_terms=["db:mysql-core", "rc:primary-readonly", "con:db-role"],
    # MySQLPrimaryReadOnly 是**新增**的专用告警。原先只声明泛化的
    # AppHighErrorRate，它的注解完全不含"只读/写入失败"判据 →
    # 诊断只能靠猜（实测这个场景确实被判成 rc:slow-sql）。
    # 主库 read_only 有直接的指标证据（mysql_global_variables_read_only{db_node="primary"}），
    # 因此用"配置态"而不是"5xx 症状"作为判据。
    alert_names=["MySQLPrimaryReadOnly", "AppHighErrorRate"],
    needs_stress=True,
    signal_needs_stress=True,
    blast_radius="全部写交易失败（P0），但健康检查与读接口全部正常，极易误判",
))

_register(Fault(
    id="db_replica_lag", layer="database", category="依赖",
    title="只读副本复制延迟（SOURCE_DELAY）",
    description="给副本设置 SOURCE_DELAY=60s：IO/SQL 线程都在 Running，"
                "Seconds_Behind_Master 稳定在 60s 左右，读路径返回陈旧数据。",
    inject=_db_replica_lag_inject, recover=_db_replica_lag_recover,
    selector="mysql-replicas",
    default_params={"delay_s": 60},
    params_help={"delay_s": "复制延迟秒数"},
    # ⚠ 故障信号与**恢复**信号必须看不同的东西（§11.21 第 11c 项的修法）：
    #   · `Seconds_Behind_Master` 是延迟的**后果** —— 拿它做恢复校验必然误判：
    #     故障解除后副本还要几分钟排空积压（实测 95s 预算内从未通过，报告里
    #     `db_replica_lag` 的"已回滚=False"就是这个假阴性）；
    #   · `DESIRED_DELAY` 是**配置态**（故障本身），回滚后瞬时归零。
    # 两者都作为故障信号：配置态立刻可观测（顺带修掉该场景"注入期 0 信号"），
    # 而滞后本身由告警规则 MySQLReplicationLag 覆盖。
    signals=[
        'max(cc_mysql_replica_desired_delay) > 0',
        'mysql_slave_status_seconds_behind_master > 5',
    ],
    recovered_signals=['max(cc_mysql_replica_desired_delay) > 0'],
    expected_root_cause="主从复制延迟过大，只读副本数据陈旧，读路径读到过期数据",
    ontology_terms=["db:mysql-core", "db:mysql-replica", "metric:replica-lag", "con:replica-lag", "rc:replica-lag"],
    alert_names=["MySQLReplicationLag", "MySQLSlowQueries"],
    # 不再需要"靠压测才可观测"：配置态信号（DESIRED_DELAY）注入后 20s 内即可见
    # （应用 exporter 每 20s 刷一次副本指标），因此注入期就能校验信号真的成立。
    # 原值 True 会让注入期**完全跳过**信号等待，报告中该场景的"注入期信号"一栏
    # 永远是空的（看起来像没验证，其实是被跳过了）。
    signal_needs_stress=False,
    blast_radius="读路径返回陈旧数据（对账/查询不一致），写路径不受影响",
))

_register(Fault(
    id="db_replica_io_stop", layer="database", category="依赖",
    title="停止副本 IO 线程（复制中断）",
    description="STOP REPLICA IO_THREAD：副本不再拉取主库 binlog，"
                "SQL 线程跑完中继日志后数据永久停滞。",
    inject=_db_replica_io_stop_inject, recover=_db_replica_io_stop_recover,
    selector="mysql-replicas",
    signals=['count(mysql_slave_status_slave_io_running == 0) > 0'],
    recovered_signals=['count(mysql_slave_status_slave_io_running == 0) > 0'],
    expected_root_cause="主从复制中断（IO 线程停止），副本数据停止同步",
    ontology_terms=["db:mysql-core", "db:mysql-replica", "con:replica-down", "rc:db-replica-loss"],
    alert_names=["MySQLReplicationBroken"],
    blast_radius="副本数据永久滞后；主库故障时无法安全切换",
))

_register(Fault(
    id="db_replica_kill", layer="database", category="资源",
    title="杀掉一个只读副本容器",
    description="docker kill 副本：exporter 同时消失，up{job=mysql-replica-*} 掉 0，"
                "应用读路径回退到另一个副本。",
    inject=_db_replica_kill_inject, recover=_db_replica_kill_recover,
    selector="mysql-replicas",
    # 注入判据用 **uptime 重置**：`docker kill` 后重启策略秒级拉起容器，而 Prometheus 抓取间隔 15s，
    # 想抓到 `mysql_up=0` 的瞬态几乎不可能（实测等 50s 都抓不到）。mysqld 重启后 uptime 是低值，
    # 会**持续数分钟**，属"必然可观测"的量。恢复判据仍用 up 计数，保持"副本已恢复"的语义。
    # 判据取**并集**，因为两条证据的出现时机不同：
    #   · `mysql_up == 0`：容器一死，抓取目标消失，约 15s 后就为 0，并**持续整个宕机期**
    #   · `uptime < 180`：容器回来后才成立（实测 MySQL 副本冷启动约 **167s**）
    # 单独用后者会失败：等待预算 120s < 167s，而此时 Prometheus 仍返回**陈旧的高 uptime**。
    signals=['count(mysql_up{db_role="replica"} == 1) < 2'
             ' or min(mysql_global_status_uptime{db_role="replica"}) < 180'],
    recovered_signals=['count(mysql_up{db_role="replica"} == 1) < 2'],
    expected_root_cause="数据库只读副本节点丢失，读路径容量下降",
    ontology_terms=["db:mysql-replica", "rc:db-replica-loss", "con:replica-down", "rc:replica-loss"],
    # 实测覆盖（tools/alert_coverage_check.py，hold=240s）：
    # `docker kill` 后 compose 的 restart 策略会把容器拉起来，因此在册目标没消失
    # （`MySQLReplicaDown` 正确地不响应），可观测症状是**重启期间 mysqld 还没就绪**
    # → `mysql_up == 0` → `MySQLDown` 触发。两条都声明：前者覆盖"节点真的没了"，
    # 后者覆盖"节点在但库不可用"。
    alert_names=["MySQLReplicaDown", "MySQLDown"],
    blast_radius="读容量下降；若主库同时故障则失去冗余",
))

# ── 资源耗尽（跨层） ──────────────────────────────────────────────────────
_register(Fault(
    id="res_cluster_cpu", layer="resource", category="资源",
    title="应用集群 CPU 全面耗尽（全副本 CPU 压力 + 压测）",
    description="在全部应用副本上跑满 CPU：集群整体进入 CPU 节流，"
                "P99 全面抬升而不是单副本倾斜。",
    inject=_res_cluster_cpu_inject, recover=_res_stress_recover,
    selector="app-all",
    default_params={"workers": 4, "seconds": 900},
    params_help={"workers": "每副本 CPU 负载进程数", "seconds": "持续秒数"},
    signals=[
        'count(rate(cc_container_cpu_throttled_seconds_total[1m]) > 0.1) >= 2',
        'max(rate(cc_container_cpu_nr_throttled_total[1m])) > 1',
    ],
    recovered_signals=['count(rate(cc_container_cpu_throttled_seconds_total[1m]) > 0.1) >= 2'],
    expected_root_cause="应用集群整体 CPU 资源不足（副本数不足或 CPU 配额过低）",
    ontology_terms=["app:payment-app", "metric:cpu-throttle", "rc:cluster-capacity", "rc:cpu-throttle"],
    alert_names=["AppContainerCpuThrottled", "AppHighLatencyP99"],
    needs_stress=True,
    blast_radius="应用层整体（P1），延迟与错误率同时上升",
))

_register(Fault(
    id="res_cluster_memory", layer="resource", category="资源",
    title="应用集群内存全面耗尽",
    description="全部副本各申请 220MB：三个副本同时逼近 256MB 上限，"
                "集群级 OOM 风险。",
    inject=_res_cluster_memory_inject, recover=_res_stress_recover,
    selector="app-all",
    default_params={"mb": 220, "seconds": 900},
    params_help={"mb": "每副本申请内存 MB", "seconds": "持续秒数"},
    signals=[
        'max(cc_container_memory_working_set_bytes / (cc_container_memory_limit_bytes > 0)) > 0.8',
    ],
    recovered_signals=[
        'max(cc_container_memory_working_set_bytes / (cc_container_memory_limit_bytes > 0)) > 0.8',
    ],
    expected_root_cause="应用集群内存资源不足，全部副本面临 OOM Kill 风险",
    ontology_terms=["app:payment-app", "metric:mem-pressure", "rc:cluster-capacity"],
    # 诊断输入必须是**集群级**告警：本场景 selector=app-all（3 个副本各 220MB），
    # 而 AppContainerMemoryPressure 是逐容器视角、措辞是"存在 OOM Kill 风险"。
    # 实测：声明它时，本场景与 app_memory_stress（selector=roundrobin，**单副本** 200MB）
    # 拿到的是逐字节相同的 rca_hint，两个语义不同的故障在输入上无法区分 ——
    # 确定性引擎只能给同一个 top1，严格命中被钉死在 19/20（证据：
    # reports/oom_counterfactual.json）。AppClusterMemoryCapacity 用"同时越限副本数 >= 3"
    # 做判据，单副本压力不会触发它，这才是本场景该喂给引擎的信号。
    alert_names=["AppClusterMemoryCapacity"],
    blast_radius="应用层整体（P1），可能全副本同时被 OOM Kill",
))

_register(Fault(
    id="res_db_memory", layer="resource", category="资源",
    title="数据库内存/排序资源不足（大排序落盘）",
    description="压小 sort/join/tmp 缓冲后跑大规模排序：排序被迫落盘，"
                "磁盘临时表与 IO 激增，查询延迟上升。",
    inject=_res_db_memory_inject, recover=_res_db_memory_recover,
    selector="mysql-primary",
    default_params={"loops": 800, "parallel": 4},
    params_help={"loops": "重复执行次数（必须足够大以覆盖保持期）", "parallel": "并行循环数"},
    # ⚠ 阈值必须按**实测量级**取，且 loops 必须**覆盖保持期**。
    # 踩过的坑（两个问题叠加，正是"21/21 通过"里两个"注入期 0 信号"场景的根因）：
    #  1) `_mysql_loop` 是 `for i in $(seq 1 loops)`，跑完就结束 —— loops=200 时
    #     约 1 分钟就停了，而注入信号与告警 `for` 都要在这个窗口内累积；
    #  2) 原阈值 0.1 高于故障可达上限（实测 [2m] 峰值 **0.028**）→ 永远等不到。
    # 现值 0.03 的依据：空闲基线恒为 0，故障期实测 0.028~0.055（见验证手册 §11.27）。
    signals=[
        'max(rate(mysql_global_status_created_tmp_disk_tables{db_node="primary"}[30s])) > 0.03',
        # 这里原先还有一条 `rate(slow_queries[30s]) > 0.2`，**已删除**：本场景注入的是
        # 大排序聚合，实测单次执行**均耗时 170s**（8 次执行），而 slow_queries 是
        # **执行完成后**才自增的计数器 → 30s 窗口内它根本来不及动，属结构上不可能成立。
        # 保留下面这条 tmp_disk_tables（它才是本场景真正的症状，实测能成立）。
        # 注意：这不是"调低阈值让它变绿"，而是删掉一条测错东西的判据。
    ],
    recovered_signals=[
        'max(rate(mysql_global_status_created_tmp_disk_tables{db_node="primary"}[1m])) > 0.03',
    ],
    expected_root_cause="数据库排序内存不足（sort_buffer/tmp_table 配置偏小），临时表落盘",
    ontology_terms=["db:mysql-core", "rc:tmp-disk", "con:db-tmp-table"],
    alert_names=["MySQLDiskTempTables"],
    blast_radius="数据库侧排序类查询全面变慢",
))
