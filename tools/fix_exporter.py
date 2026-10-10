#!/usr/bin/env python3
"""Fix the mysqld-exporter service block in docker-compose.yml."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # <repo>，不写死宿主绝对路径
path = str(ROOT / "demo" / "docker-compose.yml")
content = open(path, encoding="utf-8").read()

new_block = """  mysqld-exporter:
    image: prom/mysqld-exporter:v0.16.0
    container_name: cc-mysqld-exporter
    restart: unless-stopped
    ports:
      - "9104:9104"
    environment:
      DATA_SOURCE_NAME: "appuser:apppass@tcp(mysql:3306)/creditcard"
    networks:
      - ccdemo-net
    depends_on:
      - mysql
"""

# Replace from "  mysqld-exporter:" up to (but not including) the next top-level service key
# or up to "volumes:" / "networks:" / end of services section.
pattern = re.compile(r"  mysqld-exporter:.*?(?=\n  (?:[a-z][\w-]*:|volumes:|networks:))", re.DOTALL)
m = pattern.search(content)
if m:
    content = content[:m.start()] + new_block + content[m.end():]
    open(path, "w", encoding="utf-8").write(content)
    print("OK: mysqld-exporter block replaced")
    # print the fixed section
    idx = content.find("  mysqld-exporter:")
    print(content[idx:idx+400])
else:
    print("NO MATCH - block unchanged")
