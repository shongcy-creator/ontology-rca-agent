#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断 MySQL SHOW 语句与权限问题。"""
import pymysql

conn = pymysql.connect(
    host="localhost", port=3306, user="appuser", password="apppass",
    database="creditcard", charset="utf8mb4", autocommit=True,
    cursorclass=pymysql.cursors.DictCursor,
)

print("=== SHOW GLOBAL STATUS ===")
with conn.cursor() as cur:
    cur.execute("SHOW GLOBAL STATUS")
    rows = cur.fetchall()
    print("type=%s len=%d" % (type(rows).__name__, len(rows)))
    if rows:
        print("first=%s" % rows[0])
        print("keys=%s" % list(rows[0].keys()))

print()
print("=== SHOW GLOBAL VARIABLES LIKE max_connections ===")
with conn.cursor() as cur:
    cur.execute("SHOW GLOBAL VARIABLES LIKE 'max_connections'")
    print(cur.fetchall())

print()
print("=== SHOW GLOBAL VARIABLES (plain, count) ===")
with conn.cursor() as cur:
    cur.execute("SHOW GLOBAL VARIABLES")
    rows = cur.fetchall()
    print("len=%d first=%s keys=%s" % (len(rows), rows[0] if rows else None,
                                       list(rows[0].keys()) if rows else None))

print()
print("=== 权限 ===")
with conn.cursor() as cur:
    cur.execute("SHOW GRANTS FOR CURRENT_USER()")
    for r in cur.fetchall():
        print("  ", list(r.values())[0])

print()
print("=== SHOW ENGINE INNODB STATUS ===")
try:
    with conn.cursor() as cur:
        cur.execute("SHOW ENGINE INNODB STATUS")
        rows = cur.fetchall()
        print("OK len=%d keys=%s" % (len(rows), list(rows[0].keys()) if rows else None))
except Exception as e:
    print("ERR:", type(e).__name__, e)
    print("-> 需要 PROCESS 权限")

print()
print("=== information_schema.innodb_trx 替代方案 ===")
try:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.innodb_trx")
        print("innodb_trx:", cur.fetchall())
except Exception as e:
    print("ERR:", type(e).__name__, e)

print()
print("=== performance_schema 访问 ===")
try:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM performance_schema.events_statements_summary_by_digest")
        print("digest table rows:", cur.fetchall())
except Exception as e:
    print("ERR:", type(e).__name__, e)
