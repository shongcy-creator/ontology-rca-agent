# -*- coding: utf-8 -*-
"""
P0 + P1a：对慢查询结论做**实测**（EXPLAIN），再按证据加索引。

## 为什么 P0 必须先做

建议动作第 1 条（EXPLAIN 确认 type=ALL / Using filesort）不是走过场：
**没有它就无法判断第 2 条该给哪些列建索引**。实测结果确实改变了结论（见输出）：
建议里列了 `created_at`，但三条 SQL 里 `created_at` 只出现在 `MD5(CONCAT(...))` 内部，
**任何索引都用不上它** —— 照单全收地建 `created_at` 索引只会白占空间、拖慢写入。

## P1a 的做法

只建**实测能改善执行计划**的索引；全部用 `ALGORITHM=INPLACE, LOCK=NONE`
（在线 DDL，不阻塞读写），并打印回滚用的 `DROP INDEX` 语句。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from tools.chaos.core import MYSQL_PRIMARY, mysql_sql  # noqa: E402

USER, PWD, DB = "appuser", "apppass", "creditcard"

# 三条语句的标记（注入器用它们做清理，这里照抄以便 EXPLAIN 与线上完全一致）
SQL_SLOW = ("SELECT COUNT(*) FROM t_txn a JOIN t_seq s ON s.n <= 5 "
            "WHERE a.amount > 0 AND a.status <> 'VOID'")
SQL_TMP = ("SELECT customer_id, MD5(CONCAT(created_at, amount, status)) AS k, COUNT(*) "
           "FROM t_txn GROUP BY customer_id, k ORDER BY 3 DESC LIMIT 5")
SQL_MEM = ("SELECT a.status, MD5(CONCAT(a.txn_id, a.amount, b.n)) AS k, COUNT(*) "
           "FROM t_txn a JOIN t_seq b ON b.n <= 2 GROUP BY a.status, k ORDER BY 3 DESC")


def q(sql: str):
    rc, out, err = mysql_sql(MYSQL_PRIMARY, sql, user=USER, pwd=PWD, db=DB)
    return rc, (out or "").strip(), (err or "").strip()


def explain(label: str, sql: str) -> str:
    rc, out, err = q("EXPLAIN " + sql)
    print("\n  ── EXPLAIN %s" % label)
    if rc != 0:
        print("     失败:", err[:200])
        return ""
    for line in out.splitlines()[:6]:
        print("     " + line[:170])
    return out


def indexes() -> str:
    rc, out, _ = q("SHOW INDEX FROM t_txn")
    cols = []
    for line in out.splitlines()[1:]:
        f = line.split("\t")
        if len(f) > 4:
            cols.append("%s.%s" % (f[2], f[4]))
    return ", ".join(dict.fromkeys(cols))


print("=" * 96)
print("P0：只读排查（EXPLAIN）")
print("=" * 96)
rc, cnt, _ = q("SELECT COUNT(*) FROM t_txn")
print("  t_txn 行数 = %s" % cnt)
print("  现有索引   = %s" % indexes())
before = {k: explain(k, s) for k, s in
          (("慢查询注入 SQL", SQL_SLOW), ("磁盘临时表 SQL", SQL_TMP), ("DB内存 SQL", SQL_MEM))}

print("\n" + "=" * 96)
print("P1a：按实测证据加索引（在线 DDL，可回滚）")
print("=" * 96)
# 只建实测有用的：
#   (status, amount) → 让 SQL_SLOW 的 WHERE a.amount>0 AND a.status<>'VOID' 走索引范围扫描
#   (customer_id)    → 让 SQL_TMP 的 GROUP BY customer_id 前缀可用（减少排序/临时表）
PLAN = [
    ("idx_txn_status_amount", "ALTER TABLE t_txn ADD INDEX idx_txn_status_amount (status, amount)",
     "ALTER TABLE t_txn DROP INDEX idx_txn_status_amount"),
    ("idx_txn_customer_id", "ALTER TABLE t_txn ADD INDEX idx_txn_customer_id (customer_id)",
     "ALTER TABLE t_txn DROP INDEX idx_txn_customer_id"),
]
for name, ddl, rollback in PLAN:
    rc, out, err = q(ddl + ", ALGORITHM=INPLACE, LOCK=NONE")
    t0 = time.time()
    print("\n  ── %s" % name)
    print("     DDL: %s" % ddl)
    if rc == 0:
        print("     结果: OK（%.1fs）" % (time.time() - t0))
    else:
        print("     结果: 失败 → %s" % err[:200])
        print("     （若为 DDL 不支持 INPLACE，可去掉 ALGORITHM/LOCK 子句重试）")
    print("     回滚: %s" % rollback)

print("\n  ── 建索引后索引列表 = %s" % indexes())
print("\n" + "=" * 96)
print("P0 复测：同样的 EXPLAIN（看 type/rows/Extra 的变化）")
print("=" * 96)
after = {k: explain(k, s) for k, s in
         (("慢查询注入 SQL", SQL_SLOW), ("磁盘临时表 SQL", SQL_TMP), ("DB内存 SQL", SQL_MEM))}
