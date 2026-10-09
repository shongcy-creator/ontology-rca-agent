# -*- coding: utf-8 -*-
from __future__ import annotations
from fastapi import APIRouter
from backend.services.prometheus import PrometheusClient

router = APIRouter()

@router.get("/summary")
async def summary():
    client = PrometheusClient()
    return client.all_metrics()

@router.get("/targets")
async def targets():
    client = PrometheusClient()
    return {"targets": client.targets()}
