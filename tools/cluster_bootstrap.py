#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据库层集群引导（MySQL GTID 异步复制）。

职责（全部幂等，可重复执行）：
  1. 等待 compose 栈内 3 个 MySQL 节点健康
  2. 在 **每个** 节点上创建/刷新只读取证账号 rca_readonly（exporter 与 RCA Agent 共用）
  3. 在主库上创建复制账号 repl
  4. 首次引导副本：
       a. RESET BINARY LOGS AND GTIDS   清空副本自身 gtid_executed
       b. mysqldump 主库 creditcard（--single-transaction --set-gtid-purged=ON）
       c. 导入副本（自带 SET @@GLOBAL.GTID_PURGED）
       d. CHANGE REPLICATION SOURCE TO ... SOURCE_AUTO_POSITION=1; START REPLICA
  5. 校验复制状态（IO/SQL 线程 Running、Seconds_Behind_Source）
  6. 汇总集群快照并写入 .chaos/cluster_state.json（供故障注入/验证脚本消费）

设计取舍：
  · 只复制业务库 creditcard（不含 mysql 系统库），账号在各节点独立创建，
    避免把主库的账号体系/时序问题带进副本。
  · 副本读取用 --read-only=ON（不用 super_read_only），使 root 仍可做引导，
    引导完成后由本脚本通过 SET GLOBAL super_read_only=ON 收紧。

用法：
  python tools/cluster_bootstrap.py                # 引导 + 校验 + 落快照
  python tools/cluster_bootstrap.py --status       # 只看状态
  python tools/cluster_bootstrap.py --reset        # 强制重建复制（重新灌数据）
  python tools/cluster_bootstrap.py --up           # 先 docker compose up -d 再引导
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_DIR = ROOT / "rca-agent"
STATE_DIR = ROOT / ".chaos"
STATE_FILE = STATE_DIR / "cluster_state.json"

PRIMARY = "cc-mysql-core"
REPLICAS = ["cc-mysql-replica-1", "cc-mysql-replica-2"]
ALL_NODES = [PRIMARY] + REPLICAS

ROOT_PWD = "rootpass"
APP_DB = "creditcard"

# 业务账号（应用使用）
APP_USER, APP_PWD = "appuser", "apppass"
# 只读取证账号（mysqld-exporter + RCA Agent 取证工具）
RO_USER, RO_PWD = "rca_readonly", "rca_readonly_pwd"
# 复制账号
REPL_USER, REPL_PWD = "repl", "replpass"


# ── docker / mysql 基础封装 ────────────────────────────────────────────────

def docker(args: List[str], input_bytes: Optional[bytes] = None,
           timeout: int = 180) -> Tuple[int, str, str]:
    cmd = ["docker"] + args
    try:
        p = subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        exe = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
        p = subprocess.run([exe] + args, input=input_bytes, capture_output=True, timeout=timeout)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def mysql_exec(container: str, sql: str, user: str = "root", pwd: str = ROOT_PWD,
               db: str = "", timeout: int = 60, batch: bool = True) -> Tuple[int, str, str]:
    """在容器内执行 SQL（-e），返回 (rc, stdout, stderr)。"""
    args = ["exec", container, "mysql", "-u", user, "-p" + pwd]
    if batch:
        args += ["-N", "-B"]
    if db:
        args += [db]
    args += ["-e", sql]
    return docker(args, timeout=timeout)


def mysql_import(container: str, sql_bytes: bytes, user: str = "root",
                 pwd: str = ROOT_PWD, timeout: int = 300) -> Tuple[int, str, str]:
    """把 SQL 文本通过 stdin 导入容器内 mysql 客户端。"""
    args = ["exec", "-i", container, "mysql", "-u", user, "-p" + pwd]
    return docker(args, input_bytes=sql_bytes, timeout=timeout)


def mysql_scalar(container: str, sql: str, user: str = "root", pwd: str = ROOT_PWD) -> str:
    rc, out, err = mysql_exec(container, sql, user=user, pwd=pwd)
    if rc != 0:
        return ""
    return (out or "").strip().splitlines()[0].strip() if (out or "").strip() else ""


# ── 步骤实现 ───────────────────────────────────────────────────────────────

def wait_healthy(nodes: List[str], timeout_s: int = 180) -> Dict[str, bool]:
    """等待所有 MySQL 节点可接受连接。"""
    deadline = time.time() + timeout_s
    ok: Dict[str, bool] = {n: False for n in nodes}
    while time.time() < deadline:
        for n in nodes:
            if ok[n]:
                continue
            rc, out, _ = docker(["exec", n, "mysqladmin", "ping", "-h", "127.0.0.1",
                                 "-u", "root", "-p" + ROOT_PWD], timeout=20)
            if rc == 0 and "alive" in (out or "").lower():
                ok[n] = True
        if all(ok.values()):
            break
        time.sleep(3)
    return ok


def ensure_users(nodes: List[str]) -> Dict[str, str]:
    """
    在每个节点上创建/刷新只读取证账号与应用账号权限（幂等）。

    注意：副本在引导完成后会被本脚本置为 super_read_only=ON，此时
    CREATE USER / GRANT 会被拒绝（ERROR 1290）。因此这里先临时降级
    只读保护，改完再恢复——保证脚本可以反复执行。
    """
    results: Dict[str, str] = {}
    for n in nodes:
        mysql_exec(n, "SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=OFF;", batch=False)
        sql = (
            "CREATE USER IF NOT EXISTS '{ro}'@'%' IDENTIFIED BY '{ropwd}'; "
            "ALTER USER '{ro}'@'%' IDENTIFIED BY '{ropwd}'; "
            "GRANT PROCESS, REPLICATION CLIENT ON *.* TO '{ro}'@'%'; "
            "GRANT SELECT ON {db}.* TO '{ro}'@'%'; "
            "GRANT SELECT ON performance_schema.* TO '{ro}'@'%'; "
            "CREATE USER IF NOT EXISTS '{app}'@'%' IDENTIFIED BY '{appwd}'; "
            "ALTER USER '{app}'@'%' IDENTIFIED BY '{appwd}'; "
            "GRANT PROCESS, REPLICATION CLIENT ON *.* TO '{app}'@'%'; "
            # 副本上应用只有读权限（写只在主库）；主库上该账号本来就是 ALL，
            # 追加 SELECT 是幂等空操作。缺这一条会出现"读写分离后读路径 503"。
            "GRANT SELECT ON {db}.* TO '{app}'@'%'; "
            "GRANT SELECT ON performance_schema.* TO '{app}'@'%'; "
            "FLUSH PRIVILEGES;"
        ).format(ro=RO_USER, ropwd=RO_PWD, db=APP_DB, app=APP_USER, appwd=APP_PWD)
        rc, out, err = mysql_exec(n, sql)
        if rc != 0:
            results[n] = "FAIL: " + (err or out).strip()[:200]
            continue
        # 恢复副本只读保护（主库与 read_only=OFF 的节点无副作用）
        if n in REPLICAS:
            mysql_exec(n, "SET GLOBAL read_only=ON; SET GLOBAL super_read_only=ON;", batch=False)
        results[n] = "ok"
    return results


def ensure_repl_user() -> str:
    """在主库创建复制账号。"""
    sql = (
        "CREATE USER IF NOT EXISTS '{u}'@'%' IDENTIFIED WITH mysql_native_password BY '{p}'; "
        "ALTER USER '{u}'@'%' IDENTIFIED WITH mysql_native_password BY '{p}'; "
        "GRANT REPLICATION SLAVE ON *.* TO '{u}'@'%'; "
        "FLUSH PRIVILEGES;"
    ).format(u=REPL_USER, p=REPL_PWD)
    rc, out, err = mysql_exec(PRIMARY, sql)
    return "ok" if rc == 0 else "FAIL: " + (err or out).strip()[:300]


def replica_status(container: str) -> Dict[str, Any]:
    """
    读取复制状态。

    MySQL 8.0 的 SHOW REPLICA STATUS 未配置复制时返回空集。
    用 \G 输出解析，避免列名版本差异。
    """
    rc, out, err = mysql_exec(container, r"SHOW REPLICA STATUS\G", batch=False)
    if rc != 0:
        return {"configured": False, "error": (err or out).strip()[:300]}

    fields: Dict[str, str] = {}
    for line in (out or "").splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        fields[k.strip()] = v.strip()

    if not fields:
        return {"configured": False}

    def num(key: str) -> Optional[float]:
        try:
            return float(fields.get(key, ""))
        except (TypeError, ValueError):
            return None

    return {
        "configured": True,
        "io_running": fields.get("Replica_IO_Running", ""),
        "sql_running": fields.get("Replica_SQL_Running", ""),
        "seconds_behind_source": num("Seconds_Behind_Source"),
        "last_io_error": fields.get("Last_IO_Error", ""),
        "last_sql_error": fields.get("Last_SQL_Error", ""),
        "source_host": fields.get("Source_Host", ""),
        "retrieved_gtid_set": fields.get("Retrieved_Gtid_Set", "")[:200],
        "executed_gtid_set": fields.get("Executed_Gtid_Set", "")[:200],
        "healthy": fields.get("Replica_IO_Running") == "Yes"
                   and fields.get("Replica_SQL_Running") == "Yes",
    }


def dump_primary() -> bytes:
    """一致性快照导出（含 GTID 元信息）。"""
    rc, out, err = docker([
        "exec", PRIMARY, "mysqldump", "-uroot", "-p" + ROOT_PWD,
        "--single-transaction", "--set-gtid-purged=ON",
        "--skip-lock-tables", "--databases", APP_DB,
    ], timeout=300)
    if rc != 0 or "-- GTID state" not in out:
        raise RuntimeError("mysqldump 失败: rc=%s err=%s" % (rc, (err or out).strip()[:400]))
    return out.encode("utf-8")


def bootstrap_replica(container: str, dump: bytes) -> Dict[str, Any]:
    """首次引导单个副本（清 GTID → 灌数据 → 建立复制 → 启动）。"""
    log: List[str] = []

    # a. 清理副本自身的复制配置与 GTID 历史（未配置时报错可忽略）
    mysql_exec(container, "STOP REPLICA;", batch=False)
    mysql_exec(container, "RESET REPLICA ALL;", batch=False)
    rc, out, err = mysql_exec(container, "RESET BINARY LOGS AND GTIDS;", batch=False)
    if rc != 0:
        # 老版本回退
        rc2, out2, err2 = mysql_exec(container, "RESET MASTER;", batch=False)
        log.append("RESET BINARY LOGS AND GTIDS 不可用，回退 RESET MASTER rc=%s" % rc2)
        if rc2 != 0:
            return {"ok": False, "error": "无法清空副本 GTID: %s" % (err or err2).strip()[:300], "log": log}
    log.append("GTID 历史已清空")

    # b. 放开只读以便导入（导入用 root，read_only 已允许，但显式松开更稳）
    mysql_exec(container, "SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=OFF;", batch=False)

    # c. 导入主库快照（其中含 SET @@GLOBAL.GTID_PURGED=...）
    rc, out, err = mysql_import(container, dump)
    if rc != 0:
        return {"ok": False, "error": "导入快照失败: %s" % (err or out).strip()[:400], "log": log}
    log.append("主库快照导入完成")

    # d. 配置并启动复制
    sql = (
        "CHANGE REPLICATION SOURCE TO "
        "SOURCE_HOST='mysql', SOURCE_PORT=3306, "
        "SOURCE_USER='{u}', SOURCE_PASSWORD='{p}', "
        "SOURCE_AUTO_POSITION=1, GET_SOURCE_PUBLIC_KEY=1; "
        "START REPLICA; "
        "SET GLOBAL read_only=ON;"
    ).format(u=REPL_USER, p=REPL_PWD)
    rc, out, err = mysql_exec(container, sql, batch=False)
    if rc != 0:
        return {"ok": False, "error": "CHANGE/START REPLICA 失败: %s" % (err or out).strip()[:400], "log": log}
    log.append("复制已启动（AUTO_POSITION=1）")
    return {"ok": True, "log": log}


def snapshot() -> Dict[str, Any]:
    """汇总集群快照。"""
    nodes: Dict[str, Any] = {}
    for n in ALL_NODES:
        rc, out, err = mysql_exec(n, (
            "SELECT @@server_id, @@hostname, @@read_only, @@gtid_mode, "
            "@@max_connections, @@version, "
            "(SELECT COUNT(*) FROM {db}.t_txn)".format(db=APP_DB)
        ))
        row = (out or "").strip().split("\t") if rc == 0 else []
        nodes[n] = {
            "reachable": rc == 0,
            "server_id": row[0] if len(row) > 0 else None,
            "hostname": row[1] if len(row) > 1 else None,
            "read_only": row[2] if len(row) > 2 else None,
            "gtid_mode": row[3] if len(row) > 3 else None,
            "max_connections": row[4] if len(row) > 4 else None,
            "version": row[5] if len(row) > 5 else None,
            "txn_rows": row[6] if len(row) > 6 else None,
            "error": None if rc == 0 else (err or out).strip()[:200],
        }
        if n in REPLICAS:
            nodes[n]["replication"] = replica_status(n)

    app_replicas: List[Dict[str, Any]] = []
    rc, out, _ = docker(["ps", "--filter", "label=com.docker.compose.service=payment-app",
                         "--format", "{{.ID}}\t{{.Names}}\t{{.Status}}"])
    if rc == 0:
        for line in (out or "").strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                app_replicas.append({"id": parts[0], "name": parts[1], "status": parts[2]})

    return {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "primary": PRIMARY,
        "replicas": REPLICAS,
        "nodes": nodes,
        "app_replicas": app_replicas,
        "app_replica_count": len(app_replicas),
    }


# ── 主流程 ─────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="MySQL 集群（主从）引导与校验")
    ap.add_argument("--status", action="store_true", help="只打印集群状态，不做任何变更")
    ap.add_argument("--reset", action="store_true", help="强制重建复制（重新灌数据）")
    ap.add_argument("--up", action="store_true", help="先执行 docker compose up -d")
    ap.add_argument("--timeout", type=int, default=240, help="等待 MySQL 健康的秒数")
    ap.add_argument("--json", action="store_true", help="只输出 JSON 快照")
    args = ap.parse_args(argv)

    if args.up:
        print("[bootstrap] docker compose up -d ...")
        rc, out, err = docker(["compose", "up", "-d"], timeout=900)
        print((out or err).strip()[-2000:])
        if rc != 0:
            print("[bootstrap] compose up 失败")
            return 2
        # docker() 的 cwd 是当前进程 cwd，这里显式在 compose 目录重跑
        p = subprocess.run(["docker", "compose", "up", "-d"], cwd=str(COMPOSE_DIR),
                           capture_output=True, timeout=900)
        if p.returncode != 0:
            print("[bootstrap] compose up 失败: " + p.stderr.decode("utf-8", "replace")[-1500:])
            return 2

    print("[bootstrap] 等待 MySQL 节点健康 ...")
    health = wait_healthy(ALL_NODES, timeout_s=args.timeout)
    for n, ok in health.items():
        print("   %-22s %s" % (n, "healthy" if ok else "UNREACHABLE"))
    if not all(health.values()):
        print("[bootstrap] 有节点不可达，终止")
        return 3

    if args.status:
        snap = snapshot()
        _emit(snap, args.json)
        return 0 if all(v.get("reachable") for v in snap["nodes"].values()) else 3

    print("[bootstrap] 创建/刷新账号 ...")
    for n, r in ensure_users(ALL_NODES).items():
        print("   %-22s %s" % (n, r))
    print("   repl@primary           %s" % ensure_repl_user())

    need_bootstrap: List[str] = []
    for r in REPLICAS:
        st = replica_status(r)
        if args.reset or not st.get("configured") or not st.get("healthy"):
            need_bootstrap.append(r)
        print("   %-22s replication=%s" % (r, json.dumps(st, ensure_ascii=False)[:160]))

    if need_bootstrap:
        print("[bootstrap] 引导副本: %s" % ", ".join(need_bootstrap))
        dump = dump_primary()
        print("   mysqldump 完成（%d bytes）" % len(dump))
        for r in need_bootstrap:
            res = bootstrap_replica(r, dump)
            print("   %-22s %s" % (r, "OK" if res.get("ok") else "FAIL " + str(res.get("error"))))
            for line in res.get("log") or []:
                print("        - " + line)

    print("[bootstrap] 等待复制追平 ...")
    deadline = time.time() + 90
    ok_all = False
    while time.time() < deadline:
        sts = {r: replica_status(r) for r in REPLICAS}
        if all(s.get("healthy") for s in sts.values()):
            lag = [s.get("seconds_behind_source") for s in sts.values()]
            if all((l is None) or (l == 0) for l in lag):
                ok_all = True
                break
        time.sleep(3)

    snap = snapshot()
    _emit(snap, args.json)

    healthy = all(v.get("reachable") for v in snap["nodes"].values())
    replicating = all(snap["nodes"][r].get("replication", {}).get("healthy") for r in REPLICAS)
    print("\n[bootstrap] 结果: nodes_reachable=%s replication_healthy=%s lag_synced=%s"
          % (healthy, replicating, ok_all))
    if not (healthy and replicating):
        return 4

    # 复制正常后收紧副本写权限
    for r in REPLICAS:
        mysql_exec(r, "SET GLOBAL super_read_only=ON;", batch=False)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[bootstrap] 集群快照已写入 %s" % STATE_FILE)
    return 0


def _emit(snap: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return
    print("\n" + "=" * 74)
    print("数据库层集群状态")
    print("=" * 74)
    for n, v in snap["nodes"].items():
        role = "PRIMARY" if n == PRIMARY else "REPLICA"
        print("%-22s %-8s server_id=%-4s read_only=%-4s txn_rows=%-6s %s" % (
            n, role, v.get("server_id"), v.get("read_only"), v.get("txn_rows"),
            "" if v.get("reachable") else "UNREACHABLE " + str(v.get("error"))))
        rep = v.get("replication")
        if rep:
            print("    replication: healthy=%s io=%s sql=%s lag=%s %s" % (
                rep.get("healthy"), rep.get("io_running"), rep.get("sql_running"),
                rep.get("seconds_behind_source"),
                ("err=" + (rep.get("last_io_error") or rep.get("last_sql_error"))[:120])
                if (rep.get("last_io_error") or rep.get("last_sql_error")) else ""))
    print("-" * 74)
    print("应用副本 %d 个:" % snap["app_replica_count"])
    for a in snap["app_replicas"]:
        print("    %-12s %-34s %s" % (a["id"], a["name"], a["status"]))


if __name__ == "__main__":
    sys.exit(main())
