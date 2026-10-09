# -*- coding: utf-8 -*-
"""Agent API — 双引擎诊断、SSE 流式推理、运行记录、工具目录、健康检查。"""
from __future__ import annotations
import asyncio
import json
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.services import agent_store
from backend.services.llm_client import LLMClient, health_check
from backend.services.llm_config import (
    PROVIDERS, available_providers, clear_runtime_config, llm_config, mask_key,
    resolve_api_key, runtime_config, set_runtime_config,
)
from backend.services.orchestrator import diagnose
from backend.services.tools import build_default_registry
from backend.services.rate_limiter import limiter, estimate_run_cost, MODEL_PRICE_PER_1M_TOKENS
from backend.services import evolution_trigger

router = APIRouter()


# ── 请求模型 ─────────────────────────────────────────────────────────────

class DiagnoseRequest(BaseModel):
    alert: str = Field(..., description="告警/故障描述")
    severity: str = Field(default="P1", description="P0/P1/P2/P3")
    mode: Optional[str] = Field(default=None, description="强制 deterministic|agentic")
    app_name: str = Field(default="payment-app")
    persist: bool = Field(default=True)


def _validate(req: DiagnoseRequest) -> None:
    if not req.alert.strip():
        raise HTTPException(status_code=400, detail="alert 不能为空")
    if req.mode and req.mode not in ("deterministic", "agentic"):
        raise HTTPException(status_code=400, detail="mode 必须是 deterministic 或 agentic")


def _sse(event: str, data: dict) -> str:
    """渲染一条 SSE 消息。"""
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False, default=str))


# ── 端点 ─────────────────────────────────────────────────────────────────

@router.post("/diagnose")
async def api_diagnose(req: DiagnoseRequest):
    """
    执行根因诊断（双引擎）。

    默认按路由判定：命中已知本体模式走确定性快路径（毫秒级），
    否则启动 LLM Agent 做观察→推理→行动循环。
    """
    _validate(req)

    # 限流
    rl = limiter.check()
    if not rl.allowed:
        raise HTTPException(
            status_code=429,
            detail={"error": rl.reason, "retry_after_s": rl.retry_after_s},
        )

    try:
        return await diagnose(
            alert=req.alert.strip(),
            severity=req.severity,
            force_mode=req.mode,
            persist=req.persist,
            app_name=req.app_name,
        )
    except Exception as e:  # noqa: BLE001
        import traceback
        err = traceback.format_exc()
        print("[AGENT ERROR]", err, flush=True)
        raise HTTPException(status_code=500, detail=err)


@router.post("/diagnose/stream")
async def api_diagnose_stream(req: DiagnoseRequest):
    """
    SSE 流式诊断。

    事件序列：
      route        路由判定结果
      run_start    开始（含 run_id、model、预算）
      step_start   每步开始（observe/reason/act）
      reasoning    模型推理内容（思维链）
      tool_call    工具调用
      observation  工具观察结果
      step_end     每步结束
      final        最终完整结果（JSON）
      error        出错

    前端据此渲染「推理过程」视图。
    """
    _validate(req)

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        def on_event(event: str, data: dict) -> None:
            # 同一事件循环内，直接入队
            queue.put_nowait({"event": event, "data": data})

        # 后台运行诊断
        task = asyncio.create_task(diagnose(
            alert=req.alert.strip(),
            severity=req.severity,
            force_mode=req.mode,
            persist=req.persist,
            app_name=req.app_name,
            on_event=on_event,
        ))

        # 先发送心跳，让前端确认连接
        yield _sse("connected", {"alert": req.alert.strip(), "severity": req.severity})

        while True:
            # 事件优先
            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.1)
                yield _sse(item["event"], item["data"])
                continue
            except asyncio.TimeoutError:
                pass

            # 任务完成？
            if task.done():
                break
            # 心跳（保活，避免长推理期间代理断开）
            yield ": ping\n\n"

        # 收尾：最终结果或异常
        try:
            final = await task
            yield _sse("final", final)
        except Exception as e:  # noqa: BLE001
            import traceback
            yield _sse("error", {"error": traceback.format_exc()[:2000]})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # 禁用 nginx 缓冲
        },
    )


@router.get("/runs")
async def api_runs(limit: int = 30, mode: Optional[str] = None):
    """列出历史 Agent 运行（可回放）。"""
    return {"runs": agent_store.list_runs(limit=limit, mode=mode)}


@router.get("/runs/{run_id}")
async def api_run_detail(run_id: str):
    """获取单次运行的完整推理轨迹。"""
    run = agent_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found: " + run_id)
    thoughts = agent_store.get_thoughts(run_id)
    return {"run": run, "thoughts": thoughts}


@router.get("/runs/{run_id}/thoughts")
async def api_run_thoughts(run_id: str):
    return {"run_id": run_id, "thoughts": agent_store.get_thoughts(run_id)}


@router.get("/tools")
async def api_tools(layer: Optional[str] = None):
    """工具目录（按层分组）。"""
    reg = build_default_registry()
    catalog = reg.catalog()
    if layer:
        catalog = [t for t in catalog if t["layer"] == layer]
    return {
        "total": len(catalog),
        "layers": reg.layers(),
        "tools": catalog,
        "policy": {"read_only": True, "writes_allowed": False},
    }


@router.get("/config")
async def api_config():
    """当前 Agent 配置（API Key 脱敏）+ 运行中覆盖状态。"""
    c = llm_config()
    return {
        "provider": c["provider"],
        "api": c["api"],
        "base_url": c["base_url"],
        "model": c["model"],
        "fallback_model": c["fallback_model"],
        "api_key": mask_key(c.get("api_key")),
        "api_key_present": bool(c.get("api_key")),
        "budgets": {
            "max_steps": c["max_steps"],
            "timeout_s": c["timeout_s"],
            "max_tokens": c["max_tokens"],
            "llm_timeout_s": c["llm_timeout_s"],
        },
        "fast_path_confidence": c["fast_path_confidence"],
        "providers": available_providers(),
        # 哪些字段是"运行中手动改的"（用于界面显示"手动覆盖中 · 重启后回到环境变量"）
        "runtime": runtime_config(),
    }


@router.post("/llm")
async def api_llm_switch(payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    """
    运行中手动切换 LLM provider / 模型 / base_url / key。

    · **只改内存、不落盘**：下一次诊断立即生效，进程重启后回到环境变量；
    · provider 必须在已注册表内（不允许任意 base_url + 任意 key，避免 SSRF）；
    · 返回值里的 key 一律脱敏。
    """
    try:
        changed = set_runtime_config(
            provider=payload.get("provider"),
            model=payload.get("model"),
            base_url=payload.get("base_url"),
            api_key=payload.get("api_key"),
            fallback_model=payload.get("fallback_model"),
        )
    except ValueError as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})

    c = llm_config()
    if not c.get("api_key"):
        # 切过去了但没有 key：明确告知（并保留切换，便于用户随后补 key）
        return JSONResponse(status_code=200, content={
            "ok": False, "changed": changed, "need_api_key": True,
            "provider": c["provider"], "model": c["model"],
            "error": "provider %s 没有可用 API Key：请在界面填入，或先在环境变量里配置 %s"
                     % (c["provider"], PROVIDERS.get(c["provider"], {}).get("api_key_env", "?")),
            "runtime": runtime_config(),
        })
    return JSONResponse(status_code=200, content={
        "ok": True, "changed": changed, "provider": c["provider"], "model": c["model"],
        "base_url": c["base_url"], "api_key": mask_key(c.get("api_key")),
        "runtime": runtime_config(),
        "note": "已在当前进程生效；重启后端会回到环境变量配置。",
    })


@router.post("/llm/reset")
async def api_llm_reset() -> Dict[str, Any]:
    """清掉运行中覆盖，回到环境变量 / compose 的配置。"""
    clear_runtime_config()
    c = llm_config()
    return {"ok": True, "provider": c["provider"], "model": c["model"],
            "base_url": c["base_url"], "api_key": mask_key(c.get("api_key")),
            "runtime": runtime_config(),
            "note": "已回到环境变量配置。"}


@router.post("/llm/test")
async def api_llm_test(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """
    **先测后切**：用候选配置试一次真实调用，但不修改当前生效配置。

    为什么需要"不保存也能测"：切换 LLM 是有后果的动作（可能连不上、模型名写错、
    额度耗尽）。让人先验证、再决定，比"切完发现不可用再切回来"好得多。
    """
    cand = dict(llm_config())
    provider = payload.get("provider")
    if provider:
        if provider not in PROVIDERS:
            return {"ok": False, "error": "未注册的 provider: %s" % provider}
        spec = PROVIDERS[provider]
        cand.update({"provider": provider, "api": spec["api"],
                     "base_url": spec["base_url"],
                     "model": spec["default_model"],
                     "fallback_model": spec["fallback_model"],
                     "api_key": resolve_api_key(provider)})
    for k in ("model", "base_url", "fallback_model"):
        if payload.get(k):
            cand[k] = str(payload[k]).strip()
    if payload.get("api_key"):
        cand["api_key"] = str(payload["api_key"]).strip()

    if not cand.get("api_key"):
        return {"ok": False, "provider": cand.get("provider"), "model": cand.get("model"),
                "error": "缺少 API Key（该 provider 环境变量未配置，且未在界面填入）"}

    t0 = time.time()
    try:
        async with LLMClient(cand) as cli:
            # strict_single=True：只试这一个 (provider, model)。
            # 否则容灾链会把失败兜到别的 provider 上，测试就变成假绿灯。
            r = await cli.chat(
                [{"role": "user", "content": "回复两个字：正常"}],
                max_retries=0, strict_single=True)
        # 如实报告"实际用的是哪个模型"；与请求不一致时要显式告警
        used = r.model or ""
        req_model = str(cand.get("model") or "")
        return {
            "ok": r.error is None,
            "provider": cand.get("provider"),
            "requested_model": req_model,
            "model": used,                       # 实际使用的模型
            "model_mismatch": bool(used and req_model and used != req_model),
            "base_url": cand.get("base_url"),
            "latency_ms": r.latency_ms or int((time.time() - t0) * 1000),
            "tokens": r.total_tokens,
            "sample": (r.content or "")[:60],
            "error": r.error,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "provider": cand.get("provider"), "model": cand.get("model"),
                "base_url": cand.get("base_url"),
                "latency_ms": int((time.time() - t0) * 1000),
                "error": "%s: %s" % (type(e).__name__, str(e)[:200])}


@router.get("/health")
async def api_health():
    """检查 LLM 连通性与存储状态。"""
    llm = await health_check()
    return {
        "llm": llm,
        "store": agent_store.stats(),
    }


@router.get("/stats")
async def api_stats():
    return agent_store.stats()


@router.get("/cost")
async def api_cost():
    """
    成本概览：总 token、按模型精算费用、价格表。

    **按模型精算**（§11.21 第 17 项）：输入与输出单价差 4~5 倍，
    因此用 per-run 记录的 `prompt_tokens` / `completion_tokens` × 各自单价求和，
    而不是"总 token × 单一价"。老数据没有这两列（默认 0）时会退化为总量估算，
    并在 `pricing_note` 里说明，避免把粗略值当精确值。
    """
    stats = agent_store.stats()
    total_tokens = stats.get("total_tokens", 0) or 0
    by_model = stats.get("by_model") or {}

    rows = []
    exact_total = 0.0
    exact_tokens = 0
    for model, agg in by_model.items():
        price = MODEL_PRICE_PER_1M_TOKENS.get(model) or {}
        p_tok = int(agg.get("prompt_tokens") or 0)
        c_tok = int(agg.get("completion_tokens") or 0)
        if price and (p_tok or c_tok):
            cost = p_tok / 1_000_000 * float(price.get("input", 0)) + \
                   c_tok / 1_000_000 * float(price.get("output", 0))
            exact = True
            exact_total += cost
            exact_tokens += p_tok + c_tok
        else:
            cost = None
            exact = False
        rows.append({
            "model": model,
            "runs": agg.get("runs", 0),
            "prompt_tokens": p_tok,
            "completion_tokens": c_tok,
            "total_tokens": int(agg.get("total_tokens") or 0),
            "priced": exact,
            "cost_usd": round(cost, 6) if cost is not None else None,
            "pricing": price or None,
        })
    rows.sort(key=lambda r: (r["cost_usd"] or 0), reverse=True)

    # 老数据（缺输入/输出拆分）按"总 token ÷ 1e6 × $1.0"的粗口径兜底，并明确标注
    unpriced_tokens = max(0, int(total_tokens) - exact_tokens)
    fallback_cost = unpriced_tokens / 1_000_000 * 1.0
    return {
        "store": stats,
        "total_tokens": total_tokens,
        "estimated_cost_usd": round(exact_total + fallback_cost, 6),
        "exact_cost_usd": round(exact_total, 6),
        "exact_tokens": exact_tokens,
        "unpriced_tokens": unpriced_tokens,
        "by_model": rows,
        "pricing_per_1m_tokens": MODEL_PRICE_PER_1M_TOKENS,
        "pricing_note": ("按模型精算：输入/输出 token 各自乘单价求和。"
                         "`unpriced_tokens` 是缺少输入/输出拆分的旧数据，"
                         "按总 token × $1/1M 粗估，混在 estimated_cost_usd 里。"),
    }


@router.get("/rate-limit")
async def api_rate_limit():
    """当前限流状态。"""
    return limiter.stats()


@router.get("/evolution")
async def api_evolution():
    """查询本体自演化触发条件与状态。"""
    return evolution_trigger.check_evolution_status()


@router.post("/evolution/trigger")
async def api_evolution_trigger(reason: str = "manual"):
    """显式触发一轮本体自演化（需人工确认）。"""
    result = evolution_trigger.trigger_evolution(reason)
    if not result.get("started"):
        return result
    return result
