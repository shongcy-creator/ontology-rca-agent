# -*- coding: utf-8 -*-
from __future__ import annotations
from fastapi import APIRouter
from backend.models import TopologyRequest, TopologyResponse
from backend.services.evo_ontology import EvoOntologyClient

router = APIRouter()
_evo_client = None


def _get_client() -> EvoOntologyClient:
    global _evo_client
    if _evo_client is None:
        _evo_client = EvoOntologyClient()
    return _evo_client


@router.post("/", response_model=TopologyResponse)
async def get_topology(req: TopologyRequest) -> TopologyResponse:
    client = _get_client()
    nodes = client.get_topology(req.app_name or "payment-app")
    return TopologyResponse(nodes=nodes, session_id=req.session_id)
