#!/usr/bin/env python
# -*- coding: utf-8 -*-
import json, urllib.request, urllib.parse

for name in ["cc_mysql_pool_limit", "cc_mysql_pool_active", "cc_mysql_pool_idle"]:
    q = name + '{app="payment-app"}'
    url = "http://localhost:9090/api/v1/query?query=" + urllib.parse.quote(q)
    r = urllib.request.urlopen(url, timeout=10)
    d = json.loads(r.read())
    res = d.get("data", {}).get("result", [])
    if res:
        print("%-24s = %s" % (name, res[0]["value"][1]))
    else:
        print("%-24s = (无数据)" % name)
