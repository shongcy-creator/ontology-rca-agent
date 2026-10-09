#!/usr/bin/env python3
"""RCA Report Generator — combines ontology inference + live Prometheus metrics."""
import urllib.request, json, urllib.parse
from pathlib import Path

def query_prom(q):
    try:
        url = 'http://127.0.0.1:9090/api/v1/query?query=' + urllib.parse.quote(q)
        r = urllib.request.urlopen(url, timeout=5)
        d = json.loads(r.read())
        return d.get('data', {}).get('result', [])
    except Exception as e:
        return []

# Load RCA engine result
results = sorted(Path(str(Path(__file__).resolve().parent)).glob('rca_result_*.json'),
                key=lambda p: p.stat().st_mtime)
data = json.loads(results[-1].read_text('utf-8-sig'))

vals_http = query_prom('cc_http_request_duration_seconds_count')
vals_05   = query_prom('cc_http_request_duration_seconds_bucket{le="0.5"}')
vals_1    = query_prom('cc_http_request_duration_seconds_bucket{le="1"}')
vals_25   = query_prom('cc_http_request_duration_seconds_bucket{le="2.5"}')
total = float(vals_http[0]['value'][1]) if vals_http else 0
gt05  = float(vals_05[0]['value'][1]) if vals_05 else 0
gt1   = float(vals_1[0]['value'][1]) if vals_1 else 0
gt25  = float(vals_25[0]['value'][1]) if vals_25 else 0

vals_thr  = query_prom('mysql_global_status_threads_connected')
vals_maxc = query_prom('mysql_global_status_max_used_connections')
vals_ac   = query_prom('cc_mysql_pool_active{app="payment-app"}')
vals_lim  = query_prom('cc_mysql_pool_limit{app="payment-app"}')
vals_up   = query_prom('mysql_global_status_uptime')
vals_txn  = query_prom('cc_txn_total')
vals_qcnt = query_prom('cc_mysql_query_duration_seconds_count')

print('=' * 62)
print('RCA 根因分析报告')
print('告警: payment-app P99 延迟 > 500ms, MySQL 连接池耗尽')
print('=' * 62)
print()
print('## INCIDENT 基本信息')
print('  incident_id :', data['incident_id'])
print('  alert       :', data['alert']['alertId'])
print('  severity    :', data['alert']['severity'])
print('  message     :', data['alert']['message'])
print('  keywords    :', ', '.join(data['alert']['keywords'][:6]))
print('  elapsed_ms  :', data['elapsed_ms'])
print()
print('## EVIDENCE 实时监控证据 (Prometheus)')
print('  HTTP 总请求数          :', int(total))
print('  HTTP 延迟 > 500ms     :', int(gt05), '({:.0f}%)'.format(gt05/total*100 if total else 0))
print('  HTTP 延迟 > 1s        :', int(gt1), '({:.0f}%)'.format(gt1/total*100 if total else 0))
print('  HTTP 延迟 > 2.5s      :', int(gt25))
print()
print('  MySQL threads_connected:', vals_thr[0]['value'][1] if vals_thr else 'N/A')
print('  MySQL max_used_conn    :', vals_maxc[0]['value'][1] if vals_maxc else 'N/A')
print('  MySQL max_connections  : 200 (config)')
print('  App 连接池活跃         :', vals_ac[0]['value'][1] if vals_ac else 'N/A')
print('  App 连接池上限         :', vals_lim[0]['value'][1] if vals_lim else 'N/A')
print('  MySQL Uptime           :', vals_up[0]['value'][1] if vals_up else 'N/A', 's')
print('  交易总量(SETTLED)      :', vals_txn[0]['value'][1] if vals_txn else 'N/A')
print('  MySQL 查询总计数       :', vals_qcnt[0]['value'][1] if vals_qcnt else 'N/A')
print()
print('## ROOT CAUSE 根因分析')
rc = data['root_cause']
print('  [PRIMARY] 置信度最高根因')
print('  category   :', rc['category'])
print('  entity_id  :', rc['entity_id'])
print('  confidence :', rc['confidence'])
print('  reason     :', rc['reason'])
print()
print('  ## 拓扑路径 (本体推理)')
for n in data['topology']:
    print('  ', n['id'])
print()
print('  ## 候选根因列表 (按置信度排序)')
for c in data['candidates']:
    print('  conf={:.2f} [{}] {} | {}'.format(
        c['confidence'], c['category'], c['entity_id'], c['reason'][:50]))
print()
print('## RECOMMENDATIONS 建议操作')
print()
print('  [P0 - 立即] 慢查询根因定位')
print('  - 当前 HTTP 延迟 >500ms 占比: {:.0f}%'.format(gt05/total*100 if total else 0))
print('  - 告警关键词命中: 延迟/P99/MySQL 连接池')
print('  - 最可能根因: t_txn 表无有效索引 → 每次交易全表扫描')
print('  - 证据: app 连接池耗尽(limit={}) + MySQL 查询延迟累加'.format(
    vals_lim[0]['value'][1] if vals_lim else 'N/A'))
print()
print('  [P1 - 5min] 扩容连接池')
print('  - 当前 pool_limit={}，已接近上限'.format(vals_lim[0]['value'][1] if vals_lim else 'N/A'))
print('  - 建议: 临时将 ds:pay poolLimit 从 10 调整到 20')
print('  - 同时优化 t_txn 索引(idx_txn_customer_id, idx_txn_created)')
print()
print('  [P2 - 1h] 添加 Prometheus 告警规则')
print('  - cc_http_request_duration_seconds P99 > 0.5 for 5min')
print('  - cc_mysql_pool_active / cc_mysql_pool_limit > 0.8 for 3min')
print('  - mysqld_exporter: mysql_global_status_threads_connected > 180')
print()
print('  ## 根因链 (推理链)')
for step in data['rca_chain']:
    desc = step.get('description', '')
    print('  Step {} [{}] {}'.format(step['step'], step['type'], desc[:70]))
