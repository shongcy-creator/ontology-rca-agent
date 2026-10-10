#!/usr/bin/env python3
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent          # <repo>，不写死宿主绝对路径
p = ROOT / ".evoontology" / "visualizations" / "ontology-layer-explorer.html"
print('HTML size:', p.stat().st_size, 'bytes')
html = p.read_text('utf-8-sig')

checks = [
    'ev:rca-alert-p99',
    'ev:rca-topology-path',
    'ev:rca-root-cause',
    'app:payment-app',
    'rel:app-accesses-db',
    'db:mysql-core',
    'rc:slow-sql',
    'supported_by',
    'evidence',
]

print()
for item in checks:
    found = item in html
    print('  {}: {}'.format(item, 'YES' if found else 'NO'))

ev_refs = re.findall(r'ev:rca-[a-z0-9\-]+', html)
print()
print('Evidence refs in HTML:', sorted(set(ev_refs)))

# 检查版本信息
if 'ontology_v0-rca' in html:
    print()
    print('ontology_v0-rca version: PRESENT in HTML')
