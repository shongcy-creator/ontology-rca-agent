#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RCA 推理引擎 — 信用卡系统运维智能体

输入: 告警 (alert) JSON 或纯文本消息
处理流程:
  1. 解析告警 → 提取触发关键词
  2. 调用 EvoOntology browse/resolve → 获取拓扑路径
  3. 拓扑逆向遍历: 应用 → 容器 → 宿主机 / 数据库 → 表
  4. 候选根因打分: 资源/数据/配置/依赖 四类
  5. 输出: RootCause + attributedTo 实体 + evidencedBy 指标 + 置信度

用法:
  python tools/rca_engine.py --alert '{"alertId":"ALERT-P99","severity":"P1","message":"payment-app P99 latency > 500ms, MySQL pool exhausted"}'
  python tools/rca_engine.py --interactive
  python tools/rca_engine.py --demo
"""
from __future__ import annotations
import argparse, json, subprocess, sys, time
from pathlib import Path

MCP_STORE = Path(__file__).parent.parent / ".evoontology"
PYTHON = Path(sys.executable)
EVOONTOLOGY_PATH = Path(__file__).parent.parent / "vendor" / "EvoOntology"

# ---------------------------------------------------------------------------
# MCP client
# ---------------------------------------------------------------------------

def _env():
    env = {}
    for k, v in subprocess.os.environ.items():
        if any(s in k.upper() for s in ["KEY", "PASSWORD", "SECRET", "TOKEN"]):
            continue
        env[k] = v
    env["PYTHONPATH"] = str(EVOONTOLOGY_PATH)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _mcp_raw(tool: str, arguments: dict) -> dict:
    """调用 EvoOntology MCP server，返回内层 JSON 对象（已解析）。"""
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments}
    }).encode("utf-8")
    init = json.dumps({
        "jsonrpc": "2.0", "id": 0,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18"}
    }).encode("utf-8")
    proc = subprocess.Popen(
        [str(PYTHON), "-m", "evoontology.runtime.mcp_server",
         "--store", str(MCP_STORE)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=_env()
    )
    proc.stdin.write(init + b"\n")
    proc.stdin.write(payload + b"\n")
    proc.stdin.close()
    out, _ = proc.communicate(timeout=30)
    for line in out.decode("utf-8", errors="replace").strip().split("\n"):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            if r.get("id") == 1:
                result = r.get("result", {})
                # MCP content[0].text 是 JSON 字符串，需再解析一次
                content = result.get("content", [])
                if content and isinstance(content, list) and len(content) > 0:
                    raw_text = content[0].get("text", "{}")
                    try:
                        return json.loads(raw_text)
                    except json.JSONDecodeError:
                        return result
                return result
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return {}


def browse(query: str, kind: str = "all", limit: int = 20) -> dict:
    return _mcp_raw("browse_semantics", {
        "query": query,
        "workspace": str(MCP_STORE),
        "kind": kind,
        "limit": limit
    })


def resolve_sem(mentions: list, context: str = "") -> dict:
    return _mcp_raw("resolve_semantics", {
        "mentions": mentions,
        "context": context,
        "workspace": str(MCP_STORE)
    })


# ---------------------------------------------------------------------------
# 告警解析
# ---------------------------------------------------------------------------

KEYWORD_PATTERNS = {
    "延迟": ["延迟", "latency", "p99", "P99", "超时", "timeout", "slow"],
    "连接": ["连接", "pool", "conn", "exhaust", "max_connections", "连接池", "耗尽"],
    "数据库": ["mysql", "MySQL", "数据库", "db", "sql", "慢查询", "索引"],
    "容器": ["容器", "container", "OOM", "重启", "restart", "内存", "cpu"],
    "变更": ["部署", "deploy", "发布", "变更", "change", "upgrade"],
}


def parse_alert(raw: str | dict) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw.replace("'", '"'))
        except Exception:
            raw = {"message": raw}
    msg = str(raw.get("message", ""))
    sev = str(raw.get("severity", "P3"))
    keywords = []
    for cat, patterns in KEYWORD_PATTERNS.items():
        if any(p.lower() in msg.lower() for p in patterns):
            keywords.extend(patterns)
    sev_boost = {"P0": 1.0, "P1": 0.7, "P2": 0.4, "P3": 0.2}.get(sev, 0.1)
    return {
        "alertId": str(raw.get("alertId", "unknown")),
        "severity": sev,
        "message": msg,
        "keywords": keywords,
        "confidence_boost": sev_boost
    }


# ---------------------------------------------------------------------------
# 拓扑遍历
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS = {
    "资源": ["容器", "container", "cpu", "内存", "OOM", "restart", "replicas"],
    "数据": ["慢查询", "索引", "t_txn", "t_customer", "pool", "连接池", "max_connections"],
    "配置": ["timeout", "maxPool", "poolLimit", "retry", "重试", "超时"],
    "依赖": ["数据库", "MySQL", "mysql", "下游", "downstream", "accesses"],
    "代码": ["bug", "空指针", "异常", "crash", "5xx"],
}

CATEGORY_BASE_SCORE = {
    "资源": 0.55,
    "数据": 0.65,
    "配置": 0.50,
    "依赖": 0.40,
    "代码": 0.30,
}


def traverse_topology(app_name: str = "payment-app") -> list:
    """从 EvoOntology 查询 app 完整拓扑路径。"""
    raw = browse(f"{app_name} 容器 宿主机 MySQL 数据库 表 交易", kind="all", limit=30)
    items = raw.get("items", [])
    nodes = []
    seen = set()
    for item in items:
        nid = str(item.get("id", ""))
        if not nid or nid in seen:
            continue
        seen.add(nid)
        nodes.append({
            "id": nid,
            "name": str(item.get("name", "")),
            "type": str(item.get("type", "")),
            "scope": str(item.get("scope", "")),
            "relation_type": str(item.get("name", "")),
        })
    return nodes


def score_candidates(topology: list, alert: dict) -> list:
    """对拓扑节点按关键词匹配打分，返回候选根因列表。"""
    keywords_str = " ".join(alert["keywords"]).lower()
    sev_boost = alert["confidence_boost"]
    candidates = []
    for node in topology:
        nid = str(node.get("id", "")).lower()
        name = str(node.get("name", "")).lower()
        combined = nid + " " + name
        for cat, cat_kws in CATEGORY_KEYWORDS.items():
            hit_count = sum(1 for kw in cat_kws if kw.lower() in combined)
            if hit_count == 0:
                continue
            base = CATEGORY_BASE_SCORE[cat]
            confidence = min(base + hit_count * 0.1 + sev_boost, 0.99)
            candidates.append({
                "category": cat,
                "entity_id": node["id"],
                "entity_name": node.get("name", ""),
                "confidence": round(confidence, 2),
                "relation_type": node.get("relation_type", ""),
                "reason": f"命中关键词: {', '.join([kw for kw in cat_kws if kw.lower() in combined][:3])}",
            })
            break
    candidates.sort(key=lambda x: x["confidence"], reverse=True)
    return candidates[:6]


# ---------------------------------------------------------------------------
# 推理链生成
# ---------------------------------------------------------------------------

def build_rca_chain(alert: dict, topology: list, candidates: list) -> list:
    chain = []
    chain.append({
        "step": 1,
        "type": "alert",
        "description": f"[{alert['severity']}] {alert['message']}",
        "keywords": alert["keywords"],
        "source": "monitoring"
    })
    chain.append({
        "step": 2,
        "type": "topology",
        "description": f"拓扑逆向遍历，发现 {len(topology)} 个节点",
        "nodes": [{"id": n["id"], "name": n["name"], "type": n["type"]} for n in topology[:6]]
    })
    for i, c in enumerate(candidates[:4], start=3):
        chain.append({
            "step": i,
            "type": "candidate",
            "category": c["category"],
            "entity": c["entity_name"],
            "confidence": c["confidence"],
            "reason": c["reason"]
        })
    return chain


# ---------------------------------------------------------------------------
# 主推理入口
# ---------------------------------------------------------------------------

def infer(alert: str | dict) -> dict:
    t0 = time.time()
    alert_data = parse_alert(alert)
    print(f"[RCA] alertId={alert_data['alertId']} keywords={alert_data['keywords']}", flush=True)

    topology = traverse_topology()
    print(f"[RCA] topology nodes={len(topology)}", flush=True)

    candidates = score_candidates(topology, alert_data)
    print(f"[RCA] candidates={len(candidates)}", flush=True)

    root = candidates[0] if candidates else None
    chain = build_rca_chain(alert_data, topology, candidates)

    return {
        "incident_id": f"INC-{alert_data['alertId']}-{int(time.time())}",
        "alert": alert_data,
        "topology": topology,
        "candidates": candidates,
        "root_cause": root,
        "confidence": round(root["confidence"], 2) if root else 0.0,
        "rca_chain": chain,
        "elapsed_ms": round((time.time() - t0) * 1000, 1)
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="信用卡系统 RCA 推理引擎")
    parser.add_argument("--alert", type=str, help='JSON alert object or string')
    parser.add_argument("--demo", action="store_true", help="Run demo alert")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--output", type=str, help="Write result to file")
    args = parser.parse_args()

    if args.demo or (not args.alert and not args.interactive):
        # 默认示例
        result = infer({
            "alertId": "ALERT-P99",
            "severity": "P1",
            "message": "payment-app P99 latency > 500ms, MySQL connection pool exhausted"
        })
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.output:
            Path(args.output).write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"written: {args.output}")

    elif args.interactive:
        print("=== RCA Interactive Mode (Ctrl+C 退出) ===")
        while True:
            try:
                line = input("\n告警> ").strip()
                if not line:
                    continue
                result = infer(line)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                if args.output:
                    Path(args.output).write_text(
                        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            except (KeyboardInterrupt, EOFError):
                break

    elif args.alert:
        result = infer(args.alert)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.output:
            Path(args.output).write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"written: {args.output}")
