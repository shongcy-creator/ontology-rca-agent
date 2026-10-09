# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Dict, Optional
from fastapi import APIRouter, HTTPException
from backend.models import ChatRequest, ChatResponse, ChatMessage
from backend.services.rca_engine import RCAEngine
from backend.services.evo_ontology import EvoOntologyClient
from backend.services.prometheus import PrometheusClient
from backend.services import incident_store

router = APIRouter()

_engine = None
_evo_client = None
_history = {}

def get_engine():
    global _engine
    if _engine is None:
        _engine = RCAEngine()
    return _engine

def get_evo():
    global _evo_client
    if _evo_client is None:
        _evo_client = EvoOntologyClient()
    return _evo_client

def is_rca_request(text):
    keywords = [
        "延迟", "latency", "p99", "超时", "连接池", "pool",
        "故障", "告警", "问题", "慢", "down", "5xx", "根因", "原因", "分析",
        "502", "503", "504", "oom", "restart",
    ]
    t = text.lower()
    return any(k.lower() in t for k in keywords)

def detect_severity(text):
    for s in ["P0", "P1", "P2", "P3"]:
        if s in text:
            return s
    return "P1"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "rca_infer",
            "description": "执行 RCA 根因分析推理",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "告警描述"},
                    "severity": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_prometheus",
            "description": "查询 Prometheus 指标",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_topology",
            "description": "查询拓扑路径",
            "parameters": {
                "type": "object",
                "properties": {"app_name": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browse_ontology",
            "description": "搜索本体概念",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_incidents",
            "description": "获取历史记录",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

def build_rca_markdown(result):
    lines = []
    lines.append("## RCA 根因分析结果")
    lines.append("")
    lines.append("**incident_id**: `%s`  |  elapsed: %sms" % (
        result.get("incident_id", "?"), result.get("elapsed_ms", 0)))
    lines.append("")
    alert = result.get("alert", {})
    lines.append("**告警**: %s" % alert.get("message", ""))
    cats = ", ".join(alert.get("matched_categories", []))
    lines.append("**严重级别**: %s  |  命中类别: %s" % (alert.get("severity", "?"), cats))
    lines.append("")
    rc = result.get("root_cause")
    if rc:
        lines.append("### 根因结论")
        lines.append("")
        # 标题用**可读名称**；只给一个 `rc:xxx` 代号等于什么都没说
        if rc.get("entity_name"):
            lines.append("**%s**" % rc["entity_name"])
            lines.append("")
        lines.append("- **类别**: %s" % rc.get("category", ""))
        lines.append("- **实体**: `%s`" % rc.get("entity_id", ""))
        lines.append("- **置信度**: %s" % rc.get("confidence", ""))
        if rc.get("lifecycle"):
            lines.append("- **本体状态**: %s" % rc["lifecycle"])
        # 机制 / 归因对象 / 观测佐证 / 判据 —— 全部来自本体（OntologyScoring.explain）
        if rc.get("definition"):
            lines.append("")
            lines.append("**机制**: %s" % rc["definition"])
        for a in rc.get("affected") or []:
            lines.append("- **归因对象**: `%s`%s%s" % (
                a.get("id", ""),
                (" %s" % a["label"]) if a.get("label") and a["label"] != a.get("id") else "",
                (" —— %s" % a["description"]) if a.get("description") else ""))
        for e in rc.get("evidenced_by") or []:
            lines.append("- **观测佐证**: `%s`%s" % (
                e.get("id", ""), (" —— %s" % e["description"]) if e.get("description") else ""))
        for c in rc.get("constraints") or []:
            lines.append("- **判据 `%s`**%s%s: %s" % (
                c.get("id", ""),
                ("［%s］" % c["severity"]) if c.get("severity") else "",
                (" scope=%s" % c["scope"]) if c.get("scope") else "",
                c.get("description", "")))
        if rc.get("matched_keywords"):
            lines.append("- **命中关键词**: %s" % " · ".join(
                "`%s`" % k for k in rc["matched_keywords"]))
        if rc.get("description"):
            lines.append("")
            lines.append(rc["description"])
        if rc.get("reason") and rc.get("reason") != rc.get("description"):
            lines.append("")
            lines.append("**判别过程**: %s" % rc["reason"])
        lines.append("")
    candidates = result.get("candidates", [])
    if candidates:
        lines.append("### 候选根因（置信度排序）")
        lines.append("")
        for i, c in enumerate(candidates[:4], 1):
            lines.append("%d. [%s] `%s` -- %s (conf=%s)" % (
                i, c.get("category", ""), c.get("entity_id", ""),
                c.get("reason", "")[:50], c.get("confidence", "")))
        lines.append("")
    thresholds = result.get("thresholds_triggered", [])
    if thresholds:
        lines.append("### 告警阈值触发 (%d 项)" % len(thresholds))
        lines.append("")
        for t in thresholds:
            lines.append("- **%s**: `%s` -- %s" % (
                t.get("rule", ""), t.get("status", ""), t.get("value", "")))
        lines.append("")
    topo = result.get("topology", [])
    if topo:
        lines.append("### 拓扑路径")
        lines.append("")
        for n in topo[:8]:
            lines.append("- `%s` (%s)" % (n.get("id", ""), n.get("name", "")))
        lines.append("")
    lines.append("### 建议操作")
    lines.append("")
    if rc and rc.get("category") == "数据":
        lines.append("1. **立即**: SELECT * FROM information_schema.optimizer_trace LIMIT 10;")
        lines.append("2. **5min**: 临时扩容连接池 poolLimit=20")
        lines.append("3. **1h**: 为 t_txn.customer_id 添加索引 idx_txn_customer_id")
    elif rc and rc.get("category") == "资源":
        lines.append("1. **立即**: docker stats cc-credit-card-app")
        lines.append("2. **立即**: docker logs cc-credit-card-app --tail 50")
        lines.append("3. **5min**: 若 OOM，增加容器内存限制")
    else:
        lines.append("1. 查看 Prometheus 仪表板")
        lines.append("2. 检查最近变更记录")
    return "\n".join(lines)

@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages cannot be empty")

    hist = _history.setdefault(req.session_id, [])
    last = req.messages[-1]
    user_text = last.content or ""
    hist.append({"role": last.role, "content": user_text})

    if is_rca_request(user_text):
        engine = get_engine()
        severity = detect_severity(user_text)
        result = engine.infer(message=user_text, severity=severity)
        incident_id = result.get("incident_id", "")
        incident_store.save(incident_id, result)
        md = build_rca_markdown(result)
        msg = ChatMessage(role="assistant", content=md, incident_id=incident_id)
        hist.append({"role": "assistant", "content": md})
        return ChatResponse(message=msg, session_id=req.session_id, incident_id=incident_id)
    else:
        responses = [
            "我是 RCA Agent。请描述告警或故障，例如：payment-app P99 延迟超过 500ms，MySQL 连接池耗尽",
            "收到。请告诉我具体告警内容和时间。",
            "好的，正在分析。请提供更多细节。",
        ]
        content = responses[len(hist) % len(responses)]
        hist.append({"role": "assistant", "content": content})
        msg = ChatMessage(role="assistant", content=content)
        return ChatResponse(message=msg, session_id=req.session_id)

@router.post("/chat/tool")
async def tool_call(session_id: str, tool_name: str, arguments: dict):
    if tool_name == "rca_infer":
        engine = get_engine()
        result = engine.infer(
            message=arguments.get("message", ""),
            severity=arguments.get("severity", "P1"),
        )
        iid = result.get("incident_id", "")
        incident_store.save(iid, result)
        return {"tool_name": tool_name, "result": result, "incident_id": iid}
    elif tool_name == "query_prometheus":
        from backend.services.prometheus import PrometheusClient
        client = PrometheusClient()
        data = client.query(arguments.get("query", "up"))
        return {"tool_name": tool_name, "result": data}
    elif tool_name == "get_topology":
        evo = get_evo()
        nodes = evo.get_topology(arguments.get("app_name", "payment-app"))
        return {"tool_name": tool_name, "result": {"nodes": nodes}}
    elif tool_name == "browse_ontology":
        evo = get_evo()
        items = evo.browse_semantics(arguments.get("query", ""))
        return {"tool_name": tool_name, "result": items}
    elif tool_name == "get_incidents":
        hist = _history.get(session_id, [])
        msgs = [{"role": m["role"], "content": m["content"][:200]} for m in hist if m["role"] in ("user", "assistant")]
        return {"tool_name": tool_name, "result": {"messages": msgs}}
    else:
        return {"tool_name": tool_name, "error": "Unknown tool: " + tool_name}

@router.get("/chat/history/{session_id}")
async def get_history(session_id: str):
    return {"session_id": session_id, "messages": _history.get(session_id, [])}

@router.get("/chat/tools")
async def get_tools():
    return {"tools": TOOLS}
