# -*- coding: utf-8 -*-
"""慢 SQL 体检：当前是否还存在慢查询（多路取证，分清"历史残留"与"现在还在发生"）。"""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from tools.chaos.core import PROM, MYSQL_PRIMARY, MYSQL_REPLICAS, mysql_sql, prom_query  # noqa: E402

NODES = [MYSQL_PRIMARY] + list(MYSQL_REPLICAS)
USER, PWD = "root", "rootpass"


def q(node, sql, db=None):
    rc, out, err = mysql_sql(node, sql, user=USER, pwd=PWD, db=db)
    return (out or "").strip() if rc == 0 else "ERR:" + (err or "").strip()[:120]


print("=" * 100)
print("① 当前是否有故障注入在跑（慢查询可能是注入造成的）")
print("=" * 100)
try:
    st = json.loads(urllib.request.urlopen("http://localhost:8088/api/chaos/status",
                                           timeout=30).read().decode())
    act = st.get("active_faults") or []
    print("  激活故障 =", [a.get("fault_id") for a in act] or "无")
except Exception as e:  # noqa: BLE001
    print("  读取失败:", str(e)[:100])

print()
print("=" * 100)
print("② 慢查询开关与累计计数（主库）")
print("=" * 100)
for var in ("slow_query_log", "long_query_time", "log_output", "log_queries_not_using_indexes"):
    print("  %-32s = %s" % (var, q(MYSQL_PRIMARY, "SHOW GLOBAL VARIABLES LIKE '%s'" % var)
                            .split("\t")[-1]))
print("  %-32s = %s" % ("Slow_queries(累计)",
                        q(MYSQL_PRIMARY, "SHOW GLOBAL STATUS LIKE 'Slow_queries'").split("\t")[-1]))
print("  %-32s = %s" % ("Slow_queries 近10分钟增量",
                        (prom_query('increase(mysql_global_status_slow_queries{db_node="primary"}[10m])')
                         or [{}])[0].get("value", [None, "N/A"])[1]))

print()
print("=" * 100)
print("③ 此刻正在执行的语句（Time>0.5s 才算慢）")
print("=" * 100)
for node in NODES:
    out = q(node, "SELECT ID, TIME, STATE, LEFT(REPLACE(INFO,'\\n',' '),90) AS info "
                  "FROM information_schema.processlist "
                  "WHERE COMMAND<>'Sleep' AND TIME>0.5 ORDER BY TIME DESC LIMIT 5")
    lines = [l for l in out.splitlines() if l.strip()]
    print("  ── %s：%s" % (node, ("%d 条慢语句" % (len(lines) - 1)) if len(lines) > 1 else "无"))
    for l in lines[:5]:
        print("     " + l[:150])

print()
print("=" * 100)
print("④ 摘要表：最慢的语句 + 最后出现时间（区分「历史残留」与「现在还在跑」）")
print("=" * 100)
sql = ("SELECT LEFT(REPLACE(DIGEST_TEXT,'\\n',' '),78) AS digest, COUNT_STAR AS cnt, "
       "ROUND(AVG_TIMER_WAIT/1e9,1) AS avg_ms, ROUND(MAX_TIMER_WAIT/1e9,1) AS max_ms, "
       "LAST_SEEN AS last_seen, ROUND(SUM_ROWS_EXAMINED/NULLIF(COUNT_STAR,0)) AS rows_per_run "
       "FROM performance_schema.events_statements_summary_by_digest "
       "WHERE DIGEST_TEXT IS NOT NULL AND COUNT_STAR>0 "
       "ORDER BY SUM_TIMER_WAIT DESC LIMIT 8")
for node in (MYSQL_PRIMARY,):
    print("  ── %s（按累计耗时排序）" % node)
    print("     %-78s %6s %9s %10s %-20s %12s" % ("digest", "次数", "均耗时ms", "最大ms", "最后出现", "每次扫描行"))
    for l in q(node, sql, db="performance_schema").splitlines()[1:]:
        f = l.split("\t")
        if len(f) >= 6:
            print("     %-78s %6s %9s %10s %-20s %12s" % (f[0][:78], f[1], f[2], f[3], f[4][:19], f[5]))

print()
print("=" * 100)
print("⑤ 告警状态（监控口径）")
print("=" * 100)
try:
    body = json.loads(urllib.request.urlopen(PROM + "/api/v1/rules", timeout=20).read().decode())
    for g in (body.get("data") or {}).get("groups") or []:
        for r in g.get("rules") or []:
            if r.get("name") in ("MySQLSlowQueries", "MySQLDiskTempTables",
                                 "MySQLThreadsRunningHigh", "MySQLRowLockContention"):
                print("  %-26s state=%-9s health=%s" % (r.get("name"), r.get("state"),
                                                        r.get("health")))
except Exception as e:  # noqa: BLE001
    print("  读取失败:", str(e)[:100])
