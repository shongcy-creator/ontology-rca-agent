#!/usr/bin/env python3
import urllib.request, json, urllib.parse

def query_prom(q):
    url = 'http://127.0.0.1:9090/api/v1/query?query=' + urllib.parse.quote(q)
    r = urllib.request.urlopen(url, timeout=5)
    d = json.loads(r.read())
    return d.get('data', {}).get('result', [])

queries = [
    ('MySQL 活跃连接数', 'cc_mysql_pool_active{app="payment-app"}'),
    ('MySQL 连接上限', 'cc_mysql_pool_limit{app="payment-app"}'),
    ('HTTP P99 延迟(s)', 'histogram_quantile(0.99, rate(cc_http_request_duration_seconds_bucket[5m]))'),
    ('MySQL P99 查询延迟(s)', 'histogram_quantile(0.99, rate(cc_mysql_query_duration_seconds_bucket[5m]))'),
    ('HTTP 总请求', 'cc_http_requests_total'),
    ('交易总量(SETTLED)', 'cc_txn_total{status="SETTLED"}'),
    ('MySQL Uptime(s)', 'mysql_global_status_uptime'),
    ('MySQL 最大连接数', 'mysql_variables_max_connections'),
    ('慢查询日志开启', 'mysql_info_schema_global_variables{var_name="slow_query_log"}'),
]

print('=== Prometheus 实时指标 ===')
for label, q in queries:
    vals = query_prom(q)
    if vals:
        v = vals[0]['value'][1]
        print(f'  {label}: {v}')
    else:
        print(f'  {label}: (no data)')

print()
print('=== MySQL 全局状态 ===')
for label, q in [
    ('Aborted connects', 'mysql_global_status_aborted_connects'),
    ('Threads connected', 'mysql_global_status_threads_connected'),
    ('Max used connections', 'mysql_global_status_max_used_connections'),
    ('Uptime', 'mysql_global_status_uptime'),
]:
    vals = query_prom(q)
    if vals:
        v = vals[0]['value'][1]
        print(f'  {label}: {v}')
