# -*- coding: utf-8 -*-
"""
知识层工具 — 历史事件、人工反馈、本体证据。

让 Agent 具备"经验"：
  · 检索相似历史 incident（同症状当时判成什么）
  · 查询历史结论的人工反馈（哪些是误判，需要修正）
  · 查询本体中已沉淀的证据记录
"""
from __future__ import annotations
from typing import Any, Dict, List

from .base import ToolResult, ToolSpec


def _history_search(args: Dict[str, Any]) -> ToolResult:
    from .. import incident_store

    keyword = (args.get("keyword") or "").strip().lower()
    category = (args.get("category") or "").strip()
    limit = int(args.get("limit", 10))

    all_inc = incident_store.list_all(limit=200)
    hits: List[dict] = []
    for inc in all_inc:
        if category and inc.get("category") != category:
            continue
        if keyword:
            hay = " ".join(str(inc.get(k, "")) for k in ("message", "entity_id", "category")).lower()
            if keyword not in hay:
                continue
        hits.append(inc)
        if len(hits) >= limit:
            break

    by_cat: Dict[str, int] = {}
    for h in hits:
        by_cat[h.get("category") or "?"] = by_cat.get(h.get("category") or "?", 0) + 1

    return ToolResult(
        ok=True,
        summary="历史事件命中 %d 条%s；类别分布 %s" % (
            len(hits),
            ("（关键字 '%s'）" % keyword) if keyword else "",
            by_cat or "无"),
        data={"incidents": hits, "category_distribution": by_cat,
              "total_in_store": len(all_inc)},
    )


def _history_feedback(args: Dict[str, Any]) -> ToolResult:
    """查询历史人工反馈 —— 用于避免重复误判。"""
    incident_id = (args.get("incident_id") or "").strip()

    try:
        from ..incident_store import _get_conn      # 复用已有连接
        conn = _get_conn()
    except Exception:
        conn = None

    if conn is None:
        return ToolResult(
            ok=True,
            summary="反馈库当前不可用（MySQL 未连接），无法提供历史纠正信息",
            data={"available": False},
        )

    try:
        with conn.cursor() as cur:
            if incident_id:
                cur.execute(
                    "SELECT incident_id, verdict, actual_cause, note, operator, created_at "
                    "FROM rca_feedback WHERE incident_id=%s ORDER BY created_at DESC",
                    (incident_id,),
                )
            else:
                cur.execute(
                    "SELECT incident_id, verdict, actual_cause, note, operator, created_at "
                    "FROM rca_feedback ORDER BY created_at DESC LIMIT %s",
                    (int(args.get("limit", 20)),),
                )
            rows = cur.fetchall() or []
    except Exception as e:
        return ToolResult.fail("查询反馈失败: %s" % e)

    # 按 verdict 统计
    stats: Dict[str, int] = {}
    corrected = []
    for r in rows:
        data = r if isinstance(r, dict) else {
            "incident_id": r[0], "verdict": r[1], "actual_cause": r[2],
            "note": r[3], "operator": r[4], "created_at": str(r[5]),
        }
        v = data.get("verdict") or "?"
        stats[v] = stats.get(v, 0) + 1
        if v in ("REJECTED", "PARTIAL"):
            corrected.append(data)

    return ToolResult(
        ok=True,
        summary="历史反馈 %d 条（%s）；其中被人工否定/部分否定的 %d 条%s" % (
            len(rows), stats or "无", len(corrected),
            ("：过往误判案例 " + "; ".join(
                "%s→%s" % (c.get("incident_id"), (c.get("actual_cause") or "")[:40])
                for c in corrected[:3])) if corrected else ""),
        data={"feedback": rows[:50], "stats": stats, "corrected_cases": corrected[:10]},
    )


def _ontology_evidence(args: Dict[str, Any]) -> ToolResult:
    """查询本体中已沉淀的证据记录。"""
    import json
    from pathlib import Path

    try:
        from ..evo_ontology import EvoOntologyClient
        evo = EvoOntologyClient()
        ws = Path(evo.workspace)
    except Exception as e:
        return ToolResult.fail("无法初始化本体客户端: %s" % e)

    active = ws / "active.json"
    if not active.is_file():
        return ToolResult.fail("本体工作区未初始化")

    ver = (json.loads(active.read_text(encoding="utf-8")).get("active_version")
           or "ontology_v0")
    ev_file = ws / "versions" / ver / "evidence.json"
    if not ev_file.is_file():
        return ToolResult(ok=True, summary="当前版本无证据记录", data=[])

    records = json.loads(ev_file.read_text(encoding="utf-8"))
    keyword = (args.get("keyword") or "").strip().lower()
    if keyword:
        records = [r for r in records
                   if keyword in json.dumps(r, ensure_ascii=False).lower()]

    limit = int(args.get("limit", 10))
    return ToolResult(
        ok=True,
        summary="本体证据共 %d 条%s" % (
            len(records), ("（过滤 '%s'）" % keyword) if keyword else ""),
        data=[{
            "id": r.get("id"),
            "source": r.get("source"),
            "query": (r.get("query") or "")[:200],
            "validation_method": r.get("validation_method"),
            "timestamp": r.get("timestamp"),
        } for r in records[:limit]],
    )


# ── 规格定义 ──────────────────────────────────────────────────────────────

HISTORY_TOOLS = [
    ToolSpec(
        name="history_search",
        layer="knowledge",
        description=(
            "检索历史 RCA 事件。用于回答'以前出现过吗''同类症状通常是什么原因'。"
            "若历史同类事件多且根因集中，可作为重要参考。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "关键字（匹配告警文本/实体/类别）", "default": ""},
                "category": {"type": "string", "description": "按根因类别过滤：数据/资源/配置/依赖/代码",
                             "default": ""},
                "limit": {"type": "integer", "default": 10},
            },
        },
        handler=_history_search,
        timeout_s=20,
    ),
    ToolSpec(
        name="history_feedback",
        layer="knowledge",
        description=(
            "查询运维人员对历史结论的反馈（CONFIRMED 确认 / REJECTED 否定 / PARTIAL 部分）。"
            "【重要】用于避免重复误判：若某类症状的历史结论被否定为 actual_cause=X，"
            "应优先考虑 X 而不是历史自动结论。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "incident_id": {"type": "string", "description": "指定事件 ID（留空=最近全部）", "default": ""},
                "limit": {"type": "integer", "default": 20},
            },
        },
        handler=_history_feedback,
        timeout_s=20,
    ),
    ToolSpec(
        name="ontology_evidence",
        layer="knowledge",
        description="查询本体中已沉淀的证据记录（历史采集数据与验证方法），作为事实依据参考。",
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "过滤关键字", "default": ""},
                "limit": {"type": "integer", "default": 10},
            },
        },
        handler=_ontology_evidence,
        timeout_s=20,
    ),
]
