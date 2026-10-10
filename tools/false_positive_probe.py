# -*- coding: utf-8 -*-
"""
阴性探针 v2：**系统其实没坏时，诊断引擎会不会硬造一个根因？**

v1 的教训（已写进本文件的断言）：8 次请求全部 HTTP 422，而 v1 把它们算成了
"假阳性 0/8" —— 请求被拒 ≠ 引擎没有误报。**一个永远不可能失败的检查毫无价值**，
所以 v2 硬性要求：只要有任何一个请求没有拿到 200，探针**直接失败**。

请求体从后端自己的 OpenAPI 契约里动态构造（不再手写猜测字段）。
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8088"
API = BASE + "/api/agent/diagnose"

CASES = [
    ("healthy_plain", "系统当前状态正常，没有异常", {}),
    ("residual_alert", "MySQL 慢查询数偏高（该告警在本次变更前就已存在）",
     {"alert_name": "MySQLSlowQueries", "severity": "P2"}),
    ("single_transient", "应用副本 1 在 12:00:03 出现过一次 HTTP 500，之后 30 分钟无复现",
     {"alert_name": "AppHighErrorRate", "severity": "P2"}),
    ("already_recovered", "复制中断告警已于 5 分钟前恢复，当前 io/sql 线程均为 ON",
     {"alert_name": "MySQLReplicationBroken", "severity": "P2"}),
    ("informational", "例行配置变更：已把 long_query_time 从 0.1s 调整为 1s", {}),
    ("unrelated_alert", "Grafana 自身的面板渲染耗时偏高",
     {"alert_name": "GrafanaSlowRender", "severity": "P3"}),
    ("out_of_scope", "今天天气怎么样？", {}),
    ("metrics_only_ok", "t_txn 行数增长与往常一致，副本延迟为 0", {}),
]


def required_schema():
    d = json.load(urllib.request.urlopen(BASE + "/openapi.json", timeout=20))
    rb = d["paths"]["/api/agent/diagnose"]["post"]["requestBody"]
    ref = rb["content"]["application/json"]["schema"].get("$ref", "")
    name = ref.split("/")[-1]
    sch = (d.get("components", {}).get("schemas") or {}).get(name) or {}
    return sch.get("required") or [], (sch.get("properties") or {})


def build(req_fields, props, msg, extra):
    p = {}
    for f in req_fields:
        meta = props.get(f) or {}
        if f in ("alert", "alert_message", "message", "text", "question", "query"):
            p[f] = msg
        elif f in extra:
            p[f] = extra[f]
        else:
            t = meta.get("type")
            p[f] = [] if t == "array" else ({} if t == "object" else (0 if t in ("integer", "number") else ""))
    p.update({k: v for k, v in extra.items() if k in props})
    if "alert" not in p and any(k in props for k in ("alert", "alert_message", "message", "text")):
        for k in ("alert", "alert_message", "message", "text", "question"):
            if k in props:
                p[k] = msg
                break
    return p


DECLINE_MARKERS = ("无故障", "非真实故障", "无有效故障证据", "不是故障", "no fault",
                   "not a real fault", "no anomaly")


def root_cause_of(body):
    """返回**引擎断言的根因**；若引擎明确拒答（判定为无故障）则返回 None。

    注意：引擎把"无故障"这个判定也放在 root_cause 字段里（category/description），
    所以**不能**以"字段存在"作为判定依据 —— 那会把正确拒答误报成假阳性（我踩过）。
    """
    if not isinstance(body, dict):
        return None
    rc = body.get("root_cause")
    if isinstance(rc, dict):
        cat = str(rc.get("category") or "")
        desc = str(rc.get("description") or "")
        if any(m in cat or m in desc for m in DECLINE_MARKERS):
            return None                      # 正确拒答
        return rc.get("entity_id") or rc.get("name") or cat or "?"
    if isinstance(rc, str) and rc.strip():
        return None if any(m in rc for m in DECLINE_MARKERS) else rc.strip()
    for k in ("root_cause_id", "root_cause_name"):
        v = body.get(k)
        if isinstance(v, str) and v.strip():
            return None if any(m in v for m in DECLINE_MARKERS) else v.strip()
    m = re.search(r'"(rc:[a-z0-9\-]+)"', json.dumps(body, ensure_ascii=False))
    return m.group(1) if m else None


def main():
    req, props = required_schema()
    print("  接口必填字段 =", req)
    print("  可用字段     =", list(props)[:12])
    rows, fp, bad = [], 0, 0
    for cid, msg, extra in CASES:
        payload = build(req, props, msg, extra)
        rq = urllib.request.Request(API, data=json.dumps(payload).encode("utf-8"),
                                    headers={"Content-Type": "application/json"}, method="POST")
        body, err = None, None
        try:
            with urllib.request.urlopen(rq, timeout=180) as r:
                body = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:160])
        except Exception as e:  # noqa: BLE001
            err = "%s: %s" % (type(e).__name__, str(e)[:160])
        if err:
            bad += 1
        rc = root_cause_of(body)
        fp += 1 if rc else 0
        rows.append({"case": cid, "input": msg, "payload": payload, "error": err,
                     "mode": (body or {}).get("mode"),
                     "route_reason": (body or {}).get("route_reason"),
                     "root_cause": rc, "false_positive": bool(rc)})
        print("  %-18s %-10s 根因=%-26s %s" % (cid, (body or {}).get("mode") or "-",
                                               rc or "无", ("✘ " + err[:36]) if err else ""))

    out = {"probe": "false-positive (healthy inputs)", "cases": len(rows),
           "requests_failed": bad, "false_positives": fp,
           "false_positive_rate": round(fp / len(rows), 4) if rows else None,
           "definition": "a case counts as a false positive ONLY if the response actually names "
                         "an ontology root cause (rc:*). A rejected request is a probe failure, "
                         "never a pass.",
           "results": rows}
    (ROOT / ".chaos").mkdir(exist_ok=True)
    for d in (ROOT / ".chaos", ROOT / "reports"):
        (d / "false_positive_probe.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                                     encoding="utf-8")
    print("\n  请求失败 %d/%d，假阳性 %d/%d" % (bad, len(rows), fp, len(rows)))
    if bad:
        print("  ✘ 有请求失败 → 本次测量**无效**（不得当作假阳性率）")
        return 1
    print("  假阳性率 %.1f%%   → reports/false_positive_probe.json" % (100.0 * fp / len(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
