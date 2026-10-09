# -*- coding: utf-8 -*-
"""
本体层工具 — 确定性知识的出口。

让 LLM 可以"咨询"本体引擎：拓扑结构、约束、确定性诊断结论。
这是双引擎架构的接口：LLM 负责探索，本体负责确定性判定。
"""
from __future__ import annotations
from typing import Any, Dict

from .base import ToolResult, ToolSpec


# ── 延迟初始化（避免 import 时拉起 MCP 子进程）────────────────────────────

_evo = None
_engine = None


def _get_evo():
    global _evo
    if _evo is None:
        from ..evo_ontology import EvoOntologyClient
        _evo = EvoOntologyClient()
    return _evo


def _get_engine():
    global _engine
    if _engine is None:
        from ..rca_engine import RCAEngine
        _engine = RCAEngine()
    return _engine


# ── Handlers ──────────────────────────────────────────────────────────────

def _ontology_topology(args: Dict[str, Any]) -> ToolResult:
    graph = _get_evo().get_topology_graph(args.get("app_name", "payment-app"))
    nodes, edges = graph.get("nodes", []), graph.get("edges", [])
    by_scope: Dict[str, int] = {}
    for n in nodes:
        by_scope[n.get("scope") or "未分层"] = by_scope.get(n.get("scope") or "未分层", 0) + 1
    return ToolResult(
        ok=True,
        summary="拓扑共 %d 个实体、%d 条关系；分层：%s" % (
            len(nodes), len(edges),
            ", ".join("%s=%d" % (k, v) for k, v in by_scope.items())),
        data={
            "nodes": [{"id": n["id"], "name": n.get("name", ""), "type": n.get("type", ""),
                       "scope": n.get("scope", "")} for n in nodes],
            "edges": [{"source": e["source"], "target": e["target"],
                       "relation": e.get("relation_type", ""),
                       "condition": e.get("condition", "")} for e in edges],
        },
    )


def _ontology_resolve(args: Dict[str, Any]) -> ToolResult:
    mentions = args.get("mentions") or []
    if isinstance(mentions, str):
        mentions = [m.strip() for m in mentions.split(",") if m.strip()]
    if not mentions:
        return ToolResult.fail("mentions 不能为空")

    raw = _get_evo().resolve_semantics(mentions, context=args.get("context", ""))
    results = raw.get("results") or []

    resolved, unresolved, relations = [], [], []
    for item in results:
        if not isinstance(item, dict):
            continue
        term = item.get("term") or {}
        tid = term.get("id")
        if tid:
            resolved.append({"id": tid, "name": term.get("name", ""), "type": term.get("type", "")})
        else:
            unresolved.append(item.get("mention") or item.get("input") or "(未识别)")
        for rel in (item.get("relations") or []):
            relations.append({
                "id": rel.get("id"), "type": rel.get("relation_type"),
                "source": rel.get("source"), "target": rel.get("target"),
                "condition": rel.get("connection_condition"),
            })

    return ToolResult(
        ok=True,
        summary="解析 %d 个术语：命中 %d，未知 %d；展开 %d 条关系" % (
            len(mentions), len(resolved), len(unresolved), len(relations)),
        data={"resolved": resolved, "unresolved": unresolved, "relations": relations},
    )


def _ontology_constraints(args: Dict[str, Any]) -> ToolResult:
    """列出本体约束 + 当前 Prometheus 实测值，判断是否满足。"""
    import json
    from pathlib import Path

    evo = _get_evo()
    ws = Path(evo.workspace)
    active = ws / "active.json"
    if not active.is_file():
        return ToolResult.fail("本体工作区未初始化: %s" % ws)

    ver = (json.loads(active.read_text(encoding="utf-8")).get("active_version")
           or "ontology_v0")
    cfile = ws / "versions" / ver / "constraints.json"
    if not cfile.is_file():
        return ToolResult.fail("未找到 constraints.json（版本 %s）" % ver)

    constraints = json.loads(cfile.read_text(encoding="utf-8"))

    # 附加实测值
    from ..prometheus import PrometheusClient
    prom = PrometheusClient()
    measured: Dict[str, Any] = {}
    for name, expr in [
        ("http_p99_s", 'histogram_quantile(0.99, sum by (le) (rate(cc_http_request_duration_seconds_bucket[5m])))'),
        ("pool_active", 'cc_mysql_pool_active{app="payment-app"}'),
        ("pool_limit", 'cc_mysql_pool_limit{app="payment-app"}'),
        ("threads_connected", "mysql_global_status_threads_connected"),
        ("max_connections", "mysql_global_variable_max_connections"),
    ]:
        try:
            res = prom.query(expr).get("result", [])
            measured[name] = float(res[0]["value"][1]) if res else None
        except Exception:
            measured[name] = None

    return ToolResult(
        ok=True,
        summary="本体定义 %d 条约束；实测：P99=%.3fs, 连接池=%s/%s, MySQL 连接=%s/%s" % (
            len(constraints),
            measured.get("http_p99_s") or 0,
            measured.get("pool_active"), measured.get("pool_limit"),
            measured.get("threads_connected"), measured.get("max_connections")),
        data={"constraints": constraints, "measured": measured},
    )


def _ontology_diagnose(args: Dict[str, Any]) -> ToolResult:
    """
    调用确定性推理引擎（快路径）。

    LLM 可以把它当作"专家意见"参考；当 LLM 探索出额外证据时，
    应以证据为准并说明与本体结论的差异。
    """
    message = (args.get("message") or "").strip()
    if not message:
        return ToolResult.fail("message 不能为空")

    result = _get_engine().infer(
        message=message,
        severity=args.get("severity", "P1"),
        app_name=args.get("app_name", "payment-app"),
        inject_evidence=False,          # 由 Agent 最终统一落库，避免中间态污染
    )
    rc = result.get("root_cause") or {}
    return ToolResult(
        ok=True,
        summary="确定性引擎结论：[%s] %s（置信度 %s，命中关键词 %s）" % (
            rc.get("category", "?"), rc.get("entity_id", "?"),
            rc.get("confidence", "?"),
            ", ".join((result.get("alert") or {}).get("keywords", [])[:6])),
        data={
            "root_cause": rc,
            "confidence": result.get("confidence"),
            "candidates": result.get("candidates", [])[:5],
            "root_cause_path": result.get("root_cause_path", []),
            "thresholds_triggered": result.get("thresholds_triggered", []),
            "matched_categories": (result.get("alert") or {}).get("matched_categories", []),
        },
    )


def _ontology_search(args: Dict[str, Any]) -> ToolResult:
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult.fail("query 不能为空")
    raw = _get_evo().browse_semantics(query, kind=args.get("kind", "all"),
                                      limit=int(args.get("limit", 15)))
    items = raw.get("items", [])
    return ToolResult(
        ok=True,
        summary="搜索 '%s' 命中 %d 条" % (query, len(items)),
        data=[{"id": i.get("id"), "name": i.get("name"), "type": i.get("type"),
               "scope": i.get("scope")} for i in items],
    )


# ── 规格定义 ──────────────────────────────────────────────────────────────

ONTOLOGY_TOOLS = [
    ToolSpec(
        name="ontology_topology",
        layer="ontology",
        description=(
            "获取信用卡系统的完整本体拓扑图（实体 + 关系）。"
            "包含应用层(app:payment-app)、运行环境层(env:container/env:host-*)、"
            "数据库层(db:mysql-core/ds:pay/table:t_txn 等)以及根因与指标实体。"
            "在需要理解'谁依赖谁'、判断故障传播方向时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "应用名，默认 payment-app",
                             "default": "payment-app"},
            },
        },
        handler=_ontology_topology,
        timeout_s=60,
    ),
    ToolSpec(
        name="ontology_resolve",
        layer="ontology",
        description=(
            "解析术语并展开其在本体中的关联边（关系/约束/证据）。"
            "用于确认某个实体（如 t_txn、ds:pay、env:container）的连接关系。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "mentions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要解析的术语列表，如 ['t_txn','db:mysql-core']",
                },
                "context": {"type": "string", "description": "解析上下文（可选）", "default": ""},
            },
            "required": ["mentions"],
        },
        handler=_ontology_resolve,
        timeout_s=60,
    ),
    ToolSpec(
        name="ontology_constraints",
        layer="ontology",
        description=(
            "列出本体中定义的系统约束（如 con:p99-threshold: P99<500ms、"
            "con:pool-exhaust: 连接池不得耗尽、con:db-maxconn: 连接数<max_connections），"
            "并附上 Prometheus 实测值。判断'是否违反已知约束'时调用。"
        ),
        parameters={"type": "object", "properties": {}},
        handler=_ontology_constraints,
        timeout_s=40,
    ),
    ToolSpec(
        name="ontology_diagnose",
        layer="ontology",
        description=(
            "调用本体的【确定性推理引擎】，基于拓扑+约束+关键词做规则化根因判定。"
            "返回根因类别、实体、置信度、传播路径。"
            "这是无幻觉的规则结论，可作为你的先验参考；"
            "但若你通过其他工具发现了它未覆盖的证据，应以证据为准并说明差异。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "告警/故障描述原文"},
                "severity": {"type": "string", "enum": ["P0", "P1", "P2", "P3"], "default": "P1"},
                "app_name": {"type": "string", "default": "payment-app"},
            },
            "required": ["message"],
        },
        handler=_ontology_diagnose,
        timeout_s=90,
    ),
    ToolSpec(
        name="ontology_search",
        layer="ontology",
        description="在本体中语义搜索概念。用于查找是否有已建模的实体/指标/根因模式。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "kind": {"type": "string", "enum": ["all", "term", "relation", "constraint", "evidence"],
                         "default": "all"},
                "limit": {"type": "integer", "default": 15},
            },
            "required": ["query"],
        },
        handler=_ontology_search,
        timeout_s=60,
    ),
]
