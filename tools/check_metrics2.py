#!/usr/bin/env python3
import urllib.request, json, urllib.parse

def query_prom(q):
    url = 'http://127.0.0.1:9090/api/v1/query?query=' + urllib.parse.quote(q)
    r = urllib.request.urlopen(url, timeout=5)
    d = json.loads(r.read())
    return d.get('data', {}).get('result', [])

queries = [
    ('t_txn 行数',         'mysql_info_schema_table_stats{table_name="t_txn"}'),
    ('t_customer 行数',    'mysql_info_schema_table_stats{table_name="t_customer"}'),
    ('max_connections',    'mysql_variables_max_connections'),
    ('wait_timeout',       'mysql_variables_wait_timeout'),
    ('threads_connected',  'mysql_global_status_threads_connected'),
    ('max_used_conn',      'mysql_global_status_max_used_connections'),
    ('aborted_connects',   'mysql_global_status_aborted_connects'),
    ('cc pool active',     'cc_mysql_pool_active{app="payment-app"}'),
    ('cc pool limit',      'cc_mysql_pool_limit{app="payment-app"}'),
    ('HTTP latency >0.5s', 'cc_http_request_duration_seconds_bucket{le="0.5"}'),
    ('HTTP latency >1s',   'cc_http_request_duration_seconds_bucket{le="1"}'),
    ('HTTP total req',     'cc_http_request_duration_seconds_count'),
]

print('=== Prometheus 实时指标 ===')
for label, q in queries:
    vals = query_prom(q)
    if vals:
        v = vals[0]['value'][1]
        print('  {}: {}'.format(label, v))
    else:
        print('  {}: (no data)'.format(label))
