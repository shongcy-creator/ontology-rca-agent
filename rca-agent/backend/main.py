# -*- coding: utf-8 -*-
from __future__ import annotations
import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, FileResponse
from backend.routers.chat import router as chat_router
from backend.routers.rca import router as rca_router
from backend.routers.topology import router as topo_router
from backend.routers.metrics import router as metrics_router
from backend.routers.agent import router as agent_router
from backend.routers.chaos import router as chaos_router

ROOT = Path(__file__).parent.parent
SPA_DIST = ROOT / "frontend" / "dist"

app = FastAPI(title="RCA Agent", version="1.0.0")

origins = os.environ.get("RCA_FRONTEND_ORIGIN", "http://localhost:5173")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3001", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes first (before StaticFiles mount)
app.include_router(chat_router,   prefix="/api", tags=["Chat"])
app.include_router(rca_router,    prefix="/api/rca", tags=["RCA"])
app.include_router(agent_router,  prefix="/api/agent", tags=["Agent"])
app.include_router(topo_router,   prefix="/api/topology", tags=["Topology"])
app.include_router(metrics_router, prefix="/api/metrics", tags=["Metrics"])
# 故障注入控制台（人工手动注入）：诊断只读 / 注入只写，严格分离
app.include_router(chaos_router,  prefix="/api/chaos", tags=["Chaos"])

# SPA fallback at /ui/
SPA = SPA_DIST
if SPA.exists():
    @app.get("/ui", include_in_schema=False)
    async def ui_root():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/ui/")

    @app.get("/ui/", include_in_schema=False)
    async def ui_index():
        return FileResponse(str(SPA / "index.html"))

    @app.get("/ui/{rest:path}", include_in_schema=False)
    async def ui_spa(rest: str):
        fp = SPA / rest
        if fp.is_file():
            return FileResponse(str(fp))
        return FileResponse(str(SPA / "index.html"))

@app.get("/", include_in_schema=False)
async def root():
    from fastapi.responses import RedirectResponse
    if SPA.exists():
        return RedirectResponse(url="/ui/")
    return {"service": "rca-agent", "docs": "/docs"}

@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "rca-agent"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8088, reload=True)
