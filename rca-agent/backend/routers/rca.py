# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.models import RCARequest, RCAResult, RCAResponse
from backend.services.rca_engine import RCAEngine
from backend.services import incident_store

router = APIRouter()
_engine_singleton = None


def _get_engine() -> RCAEngine:
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = RCAEngine()
    return _engine_singleton


def _to_model(raw: dict) -> RCAResult:
    return RCAResult(
        incident_id=str(raw.get("incident_id", "")),
        alert=raw.get("alert") or {},
        evidence=raw.get("evidence") or {},
        thresholds_triggered=raw.get("thresholds_triggered") or [],
        topology=raw.get("topology") or [],
        topology_edges=raw.get("topology_edges") or [],
        root_cause_path=raw.get("root_cause_path") or [],
        candidates=raw.get("candidates") or [],
        root_cause=raw.get("root_cause"),
        confidence=float(raw.get("confidence", 0.0) or 0.0),
        rca_chain=raw.get("rca_chain") or [],
        elapsed_ms=float(raw.get("elapsed_ms", 0.0) or 0.0),
    )


class FeedbackRequest(BaseModel):
    incident_id: str
    verdict: str = "CONFIRMED"          # CONFIRMED | REJECTED | PARTIAL
    actual_cause: str = ""
    note: str = ""
    operator: str = ""


@router.post("/infer", response_model=RCAResponse)
async def rca_infer(req: RCARequest) -> RCAResponse:
    try:
        raw = _get_engine().infer(
            message=req.alert.message or "",
            severity=req.alert.severity or "P1",
        )
        incident_store.save(raw.get("incident_id", ""), raw)
        return RCAResponse(result=_to_model(raw), session_id=req.session_id)
    except Exception as exc:
        import traceback
        err = traceback.format_exc()
        print("[RCA ERROR]", err, flush=True)
        raise HTTPException(status_code=500, detail=err)


@router.get("/incidents")
async def list_incidents(limit: int = 100):
    return {
        "count": incident_store.count(),
        "incidents": incident_store.list_all(limit=limit),
    }


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: str):
    raw = incident_store.get(incident_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="incident not found: " + incident_id)
    return {"incident_id": incident_id, "result": _to_model(raw)}


@router.get("/incidents/{incident_id}/trajectory")
async def get_trajectory(incident_id: str):
    """返回该 incident 的分析轨迹（自演化输入）。"""
    return {"incident_id": incident_id, "trajectory": incident_store.trajectories(incident_id)}


@router.post("/feedback")
async def post_feedback(req: FeedbackRequest):
    """记录运维反馈，用于本体自演化。"""
    ok = incident_store.add_feedback(
        incident_id=req.incident_id,
        verdict=req.verdict,
        actual_cause=req.actual_cause,
        note=req.note,
        operator=req.operator,
    )
    if not ok:
        raise HTTPException(status_code=503, detail="feedback store unavailable")
    return {"ok": True, "incident_id": req.incident_id, "verdict": req.verdict}


@router.get("/stats")
async def stats():
    """聚合统计（根因分布 / 严重级别 / 反馈数）。"""
    return incident_store.stats()
