# -*- coding: utf-8 -*-
"""
数据库层工具 — MySQL 只读取证。

这是"能真正定位问题"的关键增量：
  · db_processlist   当前连接与运行中的 SQL（查锁等待、长事务）
  · db_innodb_status InnoDB 事务与锁详情
  · db_slow_queries  慢查询统计
  · db_variables     关键配置项
  · db_status        全局状态（含行锁等待、死锁）
  · db_table_info    表结构与索引
  · db_explain       执行计划（验证索引是否生效）

安全：全部只读。SQL 语句白名单校验（仅允许 SELECT/SHOW/EXPLAIN/DESC）。
"""
from __future__ import annotations
import re
from typing import Any, Dict, List, Optional

from .base import ToolResult, ToolSpec, ToolSecurityError


# ── 连接管理 ──────────────────────────────────────────────────────────────

_conn = None


def _get_conn():
    """
    复用连接（失败后由调用方处理）。

    优先使用只读取证账号（RCA_DB_USER），未配置时回退到业务账号。
    取证账号拥有 PROCESS + performance_schema 只读权限，便于查看
    InnoDB 状态与 SQL 摘要，且不具备任何写权限。
    """
    global _conn
    if _conn is not None:
        try:
            _conn.ping(reconnect=True)
            return _conn
        except Exception:
            _conn = None
    import os
    import pymysql
    _conn = pymysql.connect(
        host=os.environ.get("DB_HOST", "mysql"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("RCA_DB_USER") or os.environ.get("DB_USER", "appuser"),
        password=os.environ.get("RCA_DB_PASSWORD") or os.environ.get("DB_PASSWORD", "apppass"),
        database=os.environ.get("DB_NAME", "creditcard"),
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=5,
        read_timeout=15,
        write_timeout=15,
        cursorclass=pymysql.cursors.DictCursor,
    )
    return _conn


def _rows(sql: str, params: Optional[tuple] = None,
          limit: Optional[int] = None) -> List[dict]:
    """
    执行只读查询。

    limit=None 表示不截断（SHOW GLOBAL STATUS 有 494 行，
    SHOW GLOBAL VARIABLES 有 631 行，截断会导致查不到目标变量）。
    """
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        rows = cur.fetchall()
    rows = list(rows) if isinstance(rows, tuple) else list(rows or [])
    return rows if limit is None else rows[:limit]


# ── SQL 白名单 ────────────────────────────────────────────────────────────

_READONLY_SQL = re.compile(
    r"^\s*(SELECT|SHOW|EXPLAIN|DESC|DESCRIBE|WITH)\b", re.IGNORECASE
)
_FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|REPLACE|"
    r"KILL|SET\s+GLOBAL|LOCK\s+TABLES|UNLOCK|COMMIT|ROLLBACK|CALL)\b",
    re.IGNORECASE,
)


def _guard_sql(sql: str) -> None:
    if not _READONLY_SQL.match(sql):
        raise ToolSecurityError("仅允许只读 SQL（SELECT/SHOW/EXPLAIN/DESC）")
    if _FORBIDDEN_SQL.search(sql):
        raise ToolSecurityError("SQL 含写操作关键字，已拒绝")
    if ";" in sql.rstrip().rstrip(";"):
        raise ToolSecurityError("不允许多条 SQL 语句")


# ── Handlers ──────────────────────────────────────────────────────────────

def _db_status(args: Dict[str, Any]) -> ToolResult:
    keys = [
        "Threads_connected", "Threads_running", "Max_used_connections",
        "Connections", "Aborted_connects", "Aborted_clients",
        "Slow_queries", "Queries", "Uptime",
        "Innodb_row_lock_waits", "Innodb_row_lock_time_avg", "Innodb_row_lock_current_waits",
        "Innodb_deadlocks", "Table_locks_waited",
        "Created_tmp_tables", "Created_tmp_disk_tables",
        "Select_full_join", "Select_scan",
    ]
    # 不截断：SHOW GLOBAL STATUS 约 494 行
    rows = _rows("SHOW GLOBAL STATUS")
    status = {r["Variable_name"]: r["Value"] for r in rows if "Variable_name" in r}
    picked = {k: status.get(k) for k in keys if k in status}

    variables = {r["Variable_name"]: r["Value"]
                 for r in _rows("SHOW GLOBAL VARIABLES") if "Variable_name" in r}

    # 关注项判定
    warnings = []
    try:
        tc = int(picked.get("Threads_connected") or 0)
        maxc = int(variables.get("max_connections") or 0)
        if maxc and tc / maxc > 0.8:
            warnings.append("连接数占用 %.0f%%（%d/%d），接近上限" % (tc / maxc * 100, tc, maxc))
    except (TypeError, ValueError):
        pass
    for var, label in [
        ("Innodb_row_lock_current_waits", "进行中的行锁等待"),
        ("Innodb_deadlocks", "累计死锁"),
        ("Slow_queries", "慢查询累计"),
    ]:
        try:
            v = int(picked.get(var) or 0)
            if v > 0:
                warnings.append("%s %s" % (label, v))
        except (TypeError, ValueError):
            pass

    return ToolResult(
        ok=True,
        summary=("MySQL 全局状态：连接 %s/%s、运行中 %s、慢查询 %s、行锁等待 %s（均 %sms）、死锁 %s%s" % (
            picked.get("Threads_connected"), variables.get("max_connections"),
            picked.get("Threads_running"), picked.get("Slow_queries"),
            picked.get("Innodb_row_lock_waits"), picked.get("Innodb_row_lock_time_avg"),
            picked.get("Innodb_deadlocks"),
            ("　⚠ " + "; ".join(warnings)) if warnings else "")),
        data={"status": picked, "key_variables": {
            k: variables.get(k) for k in (
                "max_connections", "wait_timeout", "long_query_time",
                "innodb_lock_wait_timeout", "transaction_isolation")
        }, "warnings": warnings},
    )


def _db_processlist(args: Dict[str, Any]) -> ToolResult:
    """当前连接与运行中的 SQL —— 查锁等待/长事务的核心工具。"""
    min_time = int(args.get("min_time", 0))
    only_active = bool(args.get("only_active", False))
    limit = int(args.get("limit", 50))

    sql = (
        "SELECT id, user, host, db, command, time, state, "
        "LEFT(COALESCE(info,''), 500) AS info "
        "FROM information_schema.processlist "
    )
    conds = []
    if min_time > 0:
        conds.append("time >= %s" % min_time)
    if only_active:
        conds.append("command <> 'Sleep'")
    if conds:
        sql += "WHERE " + " AND ".join(conds) + " "
    sql += "ORDER BY time DESC"

    rows = _rows(sql, limit=limit)

    # 关键判定
    long_running = [r for r in rows if (r.get("time") or 0) >= 5 and r.get("command") != "Sleep"]
    sleeping = [r for r in rows if r.get("command") == "Sleep"]
    waiting = [r for r in rows if (r.get("state") or "").lower().find("lock") >= 0]

    flags = []
    if waiting:
        flags.append("发现 %d 个锁等待会话（state 含 lock）" % len(waiting))
    if long_running:
        flags.append("发现 %d 个运行超过 5s 的语句" % len(long_running))

    # 只回传有价值的行：活跃/长时间/锁等待；Sleep 会话仅计数（控制 token）
    interesting = [r for r in rows if r.get("command") != "Sleep"]
    if min_time > 0 or only_active:
        shown = interesting[:20]
    else:
        shown = interesting[:20]

    return ToolResult(
        ok=True,
        summary=("进程列表 %d 条：活跃 %d，Sleep %d（活跃会话已列出）%s" % (
            len(rows), len(interesting), len(sleeping),
            ("　⚠ " + "; ".join(flags)) if flags else "")),
        data={
            "processes": shown,
            "sleep_count": len(sleeping),
            "long_running": long_running[:5],
            "lock_waiting": waiting[:5],
        },
    )


def _db_innodb_status(args: Dict[str, Any]) -> ToolResult:
    """
    InnoDB 事务与锁。

    SHOW ENGINE INNODB STATUS 需要 PROCESS 权限；若当前账号无权限，
    自动降级到 information_schema.processlist（无需 PROCESS）并明确说明限制，
    让 LLM 知道该换用哪个工具。
    """
    try:
        rows = _rows("SHOW ENGINE INNODB STATUS", limit=5)
    except Exception as e:
        # 降级：用 processlist 提取锁等待线索
        fallback: Dict[str, Any] = {"privilege_error": str(e)}
        try:
            procs = _rows(
                "SELECT id, user, time, state, LEFT(COALESCE(info,''),300) AS info "
                "FROM information_schema.processlist ORDER BY time DESC", limit=50)
            fallback["processlist"] = procs
            waiting = [p for p in procs if "lock" in str(p.get("state") or "").lower()]
            long_run = [p for p in procs if (p.get("time") or 0) >= 5 and p.get("state") != "Sleep"]
            fallback["lock_waiting"] = waiting
            fallback["long_running"] = long_run
        except Exception as e2:
            return ToolResult.fail(
                "无法获取 InnoDB 状态（缺 PROCESS 权限），且降级查询也失败: %s" % e2)

        return ToolResult(
            ok=True,
            summary=("⚠ 当前账号无 PROCESS 权限，无法读取 SHOW ENGINE INNODB STATUS。"
                     "已降级为 processlist 分析：会话 %d 个，锁等待 %d 个，长事务 %d 个。"
                     "建议用 db_processlist 获取详情；如需锁细节请授予 PROCESS 权限。" % (
                         len(fallback.get("processlist") or []),
                         len(fallback.get("lock_waiting") or []),
                         len(fallback.get("long_running") or []))),
            data=fallback,
        )

    raw = ""
    for r in rows:
        for k in ("Status", "status"):
            if k in r and r[k]:
                raw = r[k]
                break
    if not raw:
        return ToolResult.fail("无法解析 InnoDB 状态输出")

    def section(title: str, max_lines: int = 25) -> List[str]:
        out, capturing = [], False
        for line in raw.splitlines():
            if line.startswith("---") and title.upper() in line.upper():
                capturing = True
                continue
            if capturing:
                if line.startswith("---") and title.upper() not in line.upper():
                    break
                if line.strip():
                    out.append(line.rstrip())
                if len(out) >= max_lines:
                    break
        return out

    transactions = section("TRANSACTIONS", 18)
    locks = section("LATEST DETECTED DEADLOCK", 6)
    io_section = section("FILE I/O", 5)

    # 关键统计
    trx_active = 0
    lock_waits = 0
    for line in transactions:
        if "ACTIVE" in line:
            trx_active += 1
        if "LOCK WAIT" in line:
            lock_waits += 1

    flags = []
    if trx_active:
        flags.append("%d 个活跃事务" % trx_active)
    if lock_waits:
        flags.append("%d 个事务处于 LOCK WAIT" % lock_waits)
    if locks:
        flags.append("存在历史死锁记录")

    return ToolResult(
        ok=True,
        summary=("InnoDB：%s" % ("；".join(flags) if flags else "无活跃事务/锁等待")),
        data={
            "transactions": transactions,
            "lock_waits": lock_waits,
            "active_transactions": trx_active,
            "deadlock_section": locks,
            "file_io": io_section,
        },
    )


def _db_slow_queries(args: Dict[str, Any]) -> ToolResult:
    """
    慢查询摘要。

    优先 performance_schema.events_statements_summary_by_digest；
    无权限时降级为慢查询日志文件 + 全局计数器。
    """
    limit = int(args.get("limit", 20))
    out: Dict[str, Any] = {"source": None}

    # ── 首选：performance_schema digest 表 ────────────────────────
    try:
        rows = _rows(
            "SELECT SCHEMA_NAME AS db, COUNT_STAR AS cnt, "
            "ROUND(SUM_TIMER_WAIT/1000000000000,3) AS total_s, "
            "ROUND(AVG_TIMER_WAIT/1000000000,3) AS avg_ms, "
            "ROUND(MAX_TIMER_WAIT/1000000000,3) AS max_ms, "
            "SUM_ROWS_EXAMINED AS rows_examined, SUM_ROWS_SENT AS rows_sent, "
            "LEFT(DIGEST_TEXT,300) AS digest "
            "FROM performance_schema.events_statements_summary_by_digest "
            "WHERE DIGEST_TEXT IS NOT NULL "
            "ORDER BY SUM_TIMER_WAIT DESC", limit=min(limit, 12))
        out["source"] = "performance_schema"
        out["top_by_total_time"] = rows
    except Exception as e:
        out["performance_schema_error"] = str(e)
        out["source"] = "fallback"

    # ── 降级：慢查询日志 + 全局计数 ───────────────────────────────
    if out.get("source") == "fallback":
        try:
            status = {r["Variable_name"]: r["Value"]
                      for r in _rows("SHOW GLOBAL STATUS") if "Variable_name" in r}
            variables = {r["Variable_name"]: r["Value"]
                         for r in _rows("SHOW GLOBAL VARIABLES") if "Variable_name" in r}
            out["slow_queries_total"] = status.get("Slow_queries")
            out["long_query_time"] = variables.get("long_query_time")
            out["slow_query_log"] = variables.get("slow_query_log")
        except Exception:
            pass
        try:
            rows = _rows(
                "SELECT argument AS log_file FROM mysql.general_log LIMIT 1", limit=1)
        except Exception:
            pass

        total = out.get("slow_queries_total")
        return ToolResult(
            ok=True,
            summary=("⚠ 当前账号无 performance_schema 读权限，无法获取 SQL 摘要。"
                     "降级信息：慢查询累计 %s 次，long_query_time=%ss，slow_query_log=%s。"
                     "建议用 db_processlist 查看当前正在执行的语句。" % (
                         total, out.get("long_query_time"), out.get("slow_query_log"))),
            data=out,
        )

    # ── 全表扫描嫌疑 ──────────────────────────────────────────────
    suspects = []
    for r in out.get("top_by_total_time") or []:
        try:
            examined = float(r.get("rows_examined") or 0)
            sent = float(r.get("rows_sent") or 0)
            if examined > 100 and (sent == 0 or examined / max(sent, 1) > 50):
                suspects.append({
                    "digest": r.get("digest"),
                    "rows_examined": examined,
                    "rows_sent": sent,
                    "ratio": round(examined / max(sent, 1), 1),
                    "max_ms": r.get("max_ms"),
                })
        except (TypeError, ValueError):
            continue
    out["full_scan_suspects"] = suspects[:10]

    return ToolResult(
        ok=True,
        summary=("慢查询 Top%d 已获取；疑似全表扫描 %d 条%s" % (
            len(out.get("top_by_total_time") or []), len(suspects),
            ("：" + "; ".join((s["digest"] or "")[:60] for s in suspects[:3])) if suspects else "")),
        data=out,
    )


def _db_variables(args: Dict[str, Any]) -> ToolResult:
    names = args.get("names")
    rows = _rows("SHOW GLOBAL VARIABLES")
    allvars = {r["Variable_name"]: r["Value"] for r in rows}

    key = [
        "max_connections", "wait_timeout", "interactive_timeout", "slow_query_log",
        "long_query_time", "innodb_lock_wait_timeout", "innodb_buffer_pool_size",
        "innodb_flush_log_at_trx_commit", "version", "transaction_isolation",
        "table_open_cache", "thread_cache_size", "log_slow_queries",
    ]
    if names:
        wanted = [n.strip() for n in (names if isinstance(names, list) else str(names).split(",")) if n.strip()]
        selected = {k: allvars.get(k) for k in wanted}
    else:
        selected = {k: allvars.get(k) for k in key if k in allvars}

    return ToolResult(
        ok=True,
        summary="关键配置：max_connections=%s, wait_timeout=%s, slow_query_log=%s, long_query_time=%s, innodb_lock_wait_timeout=%s" % (
            selected.get("max_connections"), selected.get("wait_timeout"),
            selected.get("slow_query_log"), selected.get("long_query_time"),
            selected.get("innodb_lock_wait_timeout")),
        data=selected,
    )


def _db_table_info(args: Dict[str, Any]) -> ToolResult:
    table = args.get("table") or ""
    if not table:
        return ToolResult.fail("table 不能为空")

    info = _rows(
        "SELECT TABLE_NAME, TABLE_ROWS, ROUND(DATA_LENGTH/1024/1024,2) AS data_mb, "
        "ROUND(INDEX_LENGTH/1024/1024,2) AS index_mb, ENGINE, TABLE_COLLATION "
        "FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA='creditcard' AND TABLE_NAME=%s", (table,))

    indexes = _rows(
        "SELECT INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX, NON_UNIQUE, CARDINALITY "
        "FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA='creditcard' AND TABLE_NAME=%s "
        "ORDER BY INDEX_NAME, SEQ_IN_INDEX", (table,))

    idx_names = sorted({i["INDEX_NAME"] for i in indexes})
    return ToolResult(
        ok=True,
        summary="表 %s：约 %s 行，索引 %d 个（%s）" % (
            table, (info[0]["TABLE_ROWS"] if info else "?"),
            len(idx_names), ", ".join(idx_names)),
        data={"table": info, "indexes": indexes},
    )


def _db_explain(args: Dict[str, Any]) -> ToolResult:
    """执行计划校验（只读 SELECT 才能 EXPLAIN）。"""
    sql = (args.get("sql") or "").strip()
    if not sql:
        return ToolResult.fail("sql 不能为空")
    if not re.match(r"^\s*SELECT\b", sql, re.IGNORECASE):
        raise ToolSecurityError("EXPLAIN 仅支持 SELECT 语句")
    if _FORBIDDEN_SQL.search(sql):
        raise ToolSecurityError("SQL 含写操作关键字")

    rows = _rows("EXPLAIN " + sql, limit=30)
    problems = []
    for r in rows:
        t = (r.get("type") or "").upper()
        extra = (r.get("Extra") or "")
        if t == "ALL":
            problems.append("表 %s 全表扫描(type=ALL)" % r.get("table"))
        if "Using filesort" in extra:
            problems.append("表 %s 使用 filesort" % r.get("table"))
        if "Using temporary" in extra:
            problems.append("表 %s 使用临时表" % r.get("table"))

    return ToolResult(
        ok=True,
        summary=("执行计划 %d 行%s" % (
            len(rows), ("　⚠ " + "; ".join(problems)) if problems else "，未发现全表扫描/排序问题")),
        data={"plan": rows, "problems": problems},
    )


# ── 规格定义 ──────────────────────────────────────────────────────────────

DB_TOOLS = [
    ToolSpec(
        name="db_processlist",
        layer="env",
        description=(
            "【关键】查询 MySQL 当前连接与会话，包含正在执行的 SQL、已运行秒数、等待状态。"
            "定位'锁等待/长事务/连接堆积'必用：若 state 含 'lock' 或 time 很大，"
            "说明有事务持锁阻塞了其他请求。这是判断慢查询与连接池耗尽的直接证据。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "min_time": {"type": "integer", "description": "仅返回已运行≥N秒的会话", "default": 0},
                "only_active": {"type": "boolean", "description": "排除 Sleep 会话", "default": False},
                "limit": {"type": "integer", "default": 50},
            },
        },
        handler=_db_processlist,
        timeout_s=20,
    ),
    ToolSpec(
        name="db_innodb_status",
        layer="env",
        description=(
            "【关键】获取 InnoDB 引擎状态：活跃事务、LOCK WAIT 事务、最近死锁、文件 IO。"
            "当怀疑存在行锁/表锁竞争、长事务未提交时调用。"
        ),
        parameters={"type": "object", "properties": {}},
        handler=_db_innodb_status,
        timeout_s=25,
    ),
    ToolSpec(
        name="db_status",
        layer="env",
        description=(
            "MySQL 全局状态与关键告警项：连接数、运行中线程、慢查询累计、"
            "InnoDB 行锁等待次数/时长、死锁数、临时表、全表扫描计数。"
            "自动给出异常提示（如连接数接近上限、存在行锁等待）。"
        ),
        parameters={"type": "object", "properties": {}},
        handler=_db_status,
        timeout_s=20,
    ),
    ToolSpec(
        name="db_slow_queries",
        layer="env",
        description=(
            "从 performance_schema 获取最耗时的 SQL 摘要（按总耗时排序），"
            "并自动识别'疑似全表扫描'（扫描行数/返回行数 比例过高）。"
            "用于定位具体是哪条 SQL 拖慢系统。"
        ),
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
        handler=_db_slow_queries,
        timeout_s=30,
    ),
    ToolSpec(
        name="db_variables",
        layer="env",
        description=(
            "查询 MySQL 关键配置：max_connections、wait_timeout、slow_query_log、"
            "long_query_time、innodb_lock_wait_timeout、隔离级别等。"
            "用于判断是否为配置不当导致的问题。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "names": {"type": "array", "items": {"type": "string"},
                          "description": "指定变量名列表；留空返回关键项"},
            },
        },
        handler=_db_variables,
        timeout_s=20,
    ),
    ToolSpec(
        name="db_table_info",
        layer="env",
        description="查询表信息与索引结构（行数、数据/索引大小、索引列）。用于确认索引是否存在、是否被有效利用。",
        parameters={
            "type": "object",
            "properties": {"table": {"type": "string", "description": "表名，如 t_txn"}},
            "required": ["table"],
        },
        handler=_db_table_info,
        timeout_s=20,
    ),
    ToolSpec(
        name="db_explain",
        layer="env",
        description=(
            "对只读 SELECT 语句获取执行计划，自动标出全表扫描(type=ALL)、"
            "filesort、临时表等性能问题。用于验证索引是否生效。"
        ),
        parameters={
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "待分析的 SELECT 语句"}},
            "required": ["sql"],
        },
        handler=_db_explain,
        timeout_s=20,
        injection_exempt=["sql"],
    ),
]
