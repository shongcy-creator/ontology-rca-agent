#!/usr/bin/env python3
import json
from pathlib import Path
data = json.loads(Path(r'D:\05_code\credit-card-sys-ops\tools\rca_result_ALERT-P99.json').read_text('utf-8-sig'))
print("=== topology ===")
for n in data['topology']:
    print("  [{type}] {id} = {name}".format(type=n['type'], id=n['id'], name=n['name'][:50]))
print("=== candidates ===")
for c in data['candidates']:
    print("  [{cat}] {eid} confidence={conf}".format(cat=c['category'], eid=c['entity_id'], conf=c['confidence']))
print("=== root_cause ===")
rc = data['root_cause']
print("  [{cat}] {eid}".format(cat=rc['category'], eid=rc['entity_id']))
print("  confidence={conf}".format(conf=rc['confidence']))
print("  reason={reason}".format(reason=rc['reason'][:80]))
print("elapsed_ms={ms}".format(ms=data['elapsed_ms']))
