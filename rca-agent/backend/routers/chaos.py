# -*- coding: utf-8 -*-
"""
故障注入控制台 API（人工手动注入）。

与 Agent 的边界：**诊断只读、注入只写，两者严格分离**。
本路由只做出题（制造故障），不出答案；Agent 的诊断接口拿不到这里的注入记录，
因此端到端验证里"智能体不知道答案"这一前提依然成立。

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/api/chaos/health`       | 注入引擎是否可用（挂载/开关/令牌） |
| GET  | `/api/chaos/scenarios`    | 21 个场景定义（含默认参数、参数说明、危险等级） |
| POST | `/api/chaos/preview`      | 预览某场景会打到哪些容器 |
| GET  | `/api/chaos/status`       | 激活故障 + 集群快照 + 引擎状态 |
| GET  | `/api/chaos/doctor`       | 环境体检（15 项） |
| GET  | `/api/chaos/signals/{id}` | 实时求值该场景的 PromQL 信号（信号灯） |
| POST | `/api/chaos/inject`       | **人工注入**（返回 job_id，后台执行） |
| POST | `/api/chaos/recover`      | **人工回滚**单个或全部 |
| POST | `/api/chaos/cleanup`      | 紧急清理（不依赖状态文件的兜底回滚） |
| POST | `/api/chaos/verify`       | 批量自动演练（机器跑全流程） |
| GET  | `/api/chaos/jobs`         | 最近任务列表 |
| GET  | `/api/chaos/jobs/{id}`    | 任务详情 + 实时日志（前端轮询） |

## 为什么所有写操作都是"任务 + 轮询"

注入要等 PromQL 信号成立（最长 75s），`db_replica_lag` 回滚要等复制回放（最长 150s）。
同步 HTTP 会超时、前端也没有进度可显示。因此写操作一律立即返回 `job_id`，
由前端轮询 `/jobs/{id}` 拿日志。同时**全局单飞**：同一时刻只允许一个任务，
避免两个注入互相污染信号、回滚互相踩。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.services import chaos as svc

router = APIRouter()


# ── 可选令牌校验 ────────────────────────────────────────────────────────────
def _guard(request: Request) -> None:
    if not svc.ENABLED:
        raise HTTPException(status_code=503,
                            detail="故障注入接口已被 CHAOS_UI_ENABLED=0 停用")
    if svc.API_TOKEN:
        tok = request.headers.get("x-chaos-token") or request.query_params.get("token") or ""
        if tok != svc.API_TOKEN:
            raise HTTPException(status_code=401, detail="缺少或错误的 X-Chaos-Token")


def _engine_or_503():
    try:
        return svc._chaos()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


# ── 只读接口 ────────────────────────────────────────────────────────────────

@router.get("/health")
async def health() -> Dict[str, Any]:
    return svc.available()


@router.get("/scenarios")
async def scenarios() -> Dict[str, Any]:
    _engine_or_503()
    items = svc.scenarios()
    by_layer: Dict[str, int] = {}
    for s in items:
        by_layer[s["layer"]] = by_layer.get(s["layer"], 0) + 1
    return {"total": len(items), "by_layer": by_layer,
            "layer_labels": svc.LAYER_LABELS, "scenarios": items}


@router.post("/preview")
async def preview(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    _engine_or_503()
    fid = str(payload.get("fault_id") or "")
    if not fid:
        raise HTTPException(status_code=400, detail="缺少 fault_id")
    return svc.resolve_targets(fid)


@router.get("/status")
async def status() -> Dict[str, Any]:
    _engine_or_503()
    return svc.cluster_status()


@router.get("/doctor")
async def doctor() -> Dict[str, Any]:
    _engine_or_503()
    return svc.doctor()


@router.get("/signals/{fault_id}")
async def signals(fault_id: str) -> Dict[str, Any]:
    _engine_or_503()
    return svc.signal_snapshot(fault_id)


# ── 写接口（人工手动注入 / 回滚）─────────────────────────────────────────────

@router.post("/inject")
async def inject(request: Request, payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    _guard(request)
    _engine_or_503()
    fid = str(payload.get("fault_id") or "")
    if not fid:
        raise HTTPException(status_code=400, detail="缺少 fault_id")

    sc = svc.scenario(fid)
    if not sc:
        raise HTTPException(status_code=404, detail="未知场景: %s" % fid)

    # 人工确认：危险等级为 critical/high 的场景要求显式确认，
    # 防止误点把整个集群打挂（例如 app_gateway_stop）。
    confirm = bool(payload.get("confirm"))
    if sc["risk"] in ("critical", "high") and not confirm:
        return JSONResponse(status_code=409, content={
            "ok": False, "need_confirm": True, "risk": sc["risk"],
            "fault_id": fid, "impact": sc["impact"],
            "error": "该场景影响面较大（%s：%s），请确认后再注入" % (sc["risk"], sc["impact"]),
        })

    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise HTTPException(status_code=400, detail="params 必须是对象")
    # 参数白名单 + 类型收敛：只接受场景声明的参数
    allowed = {p["name"]: p for p in sc["params_spec"]}
    clean: Dict[str, Any] = {}
    for k, v in params.items():
        if k not in allowed:
            raise HTTPException(status_code=400, detail="场景 %s 不支持参数 %s" % (fid, k))
        try:
            clean[k] = int(v) if allowed[k]["type"] == "int" else v
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="参数 %s 需要整数" % k) from None

    # 目标白名单：前端回传预览到的目标 → 必须校验，否则等于开放"向任意容器投递命令"
    targets = payload.get("targets") or []
    if targets:
        if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
            raise HTTPException(status_code=400, detail="targets 必须是字符串数组")
        ok_set = svc.allowed_targets()
        bad = [t for t in targets if t not in ok_set]
        if bad:
            raise HTTPException(status_code=400,
                                detail="目标不在允许列表内：%s（允许：%s）"
                                       % (bad, sorted(ok_set)))

    out = svc.MANAGER.submit("inject", svc.job_inject, fault_id=fid,
                             params=clean, targets=targets)
    return JSONResponse(status_code=200 if out.get("ok") else 409, content=out)


@router.post("/recover")
async def recover(request: Request, payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    _guard(request)
    _engine_or_503()
    fid = str(payload.get("fault_id") or "")
    all_flag = bool(payload.get("all")) or fid in ("", "all", "__all__")
    out = svc.MANAGER.submit("recover_all" if all_flag else "recover",
                             svc.job_recover, fault_id="all" if all_flag else fid)
    return JSONResponse(status_code=200 if out.get("ok") else 409, content=out)


@router.post("/cleanup")
async def cleanup(request: Request) -> JSONResponse:
    """
    紧急清理：不依赖状态文件的兜底回滚（进程被杀也能复原）。

    ⚠️ 如果此刻有任务在跑（例如批量演练），**先请求取消再清理** ——
    否则"紧急清理"只清一遍残留，演练下一秒又注入一个新故障，
    用户会以为按钮坏了（这正是"误点演练"那类陷阱的同款）。
    取消后由任务自身的收尾阶段执行清理，因此这里 busy 也是安全的结果。
    """
    _guard(request)
    _engine_or_503()
    cur = svc.MANAGER.current()
    cancelled = False
    if cur and cur.status in ("queued", "running"):
        svc.MANAGER.request_cancel()
        cancelled = True
        return JSONResponse(status_code=200, content={
            "ok": True, "cancel_requested": True, "cleanup_job": None,
            "cleanup_busy": True,
            "note": "检测到运行中的任务：已先请求停止；任务会在取消点自动回滚并在收尾阶段"
                    "执行完整清理（无需再点）。想立刻确认状态请看任务日志。",
        })
    out = svc.MANAGER.submit("cleanup", svc.job_cleanup)
    return JSONResponse(status_code=200 if out.get("ok") else 409,
                        content={**out, "cancel_requested": cancelled})


@router.post("/verify")
async def verify(request: Request, payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    """
    批量自动演练（注入→等信号→保持→回滚→校验恢复）。耗时较长，务必用 jobs 轮询。

    ⚠ **默认只跑代表性子集**（`?full=true` 才跑全部 21 个）。
    为什么改默认值：这是一键 20–40 分钟的**破坏性**操作，而界面上它只是一个按钮 ——
    误点代价太高（实测就有人误点后不知道怎么停）。子集覆盖各层次与各类别，
    几分钟就能给出"注入/回滚链路是否健康"的结论，足够日常自检。
    """
    _guard(request)
    _engine_or_503()
    full = bool(payload.get("full"))
    ids = payload.get("fault_ids") or []
    scope = "custom"
    if not ids:
        if full:
            ids, scope = [], "full"          # 空列表 = 全部（引擎语义）
        else:
            ids, scope = list(svc.REPRESENTATIVE_SUBSET), "subset"
    params = {
        "fault_ids": ids,
        "hold": payload.get("hold") or 20,
        "stress": payload.get("stress"),
    }
    out = svc.MANAGER.submit("verify", svc.job_verify, params=params)
    if out.get("ok"):
        out["scope"] = scope
        out["scenarios"] = ids or "全部"
        out["hint"] = ("子集演练（代表性场景）。要跑全部 21 个场景请传 full=true。"
                       if scope == "subset" else "")
    return JSONResponse(status_code=200 if out.get("ok") else 409, content=out)


# ── 压测台（stress_harness.py 的界面外壳）─────────────────────────────────

@router.get("/stress/options")
async def stress_options() -> Dict[str, Any]:
    """压测表单的唯一事实来源：模式/目标白名单/默认值/上限/预设/历史。"""
    _engine_or_503()
    return svc.stress_options()


@router.get("/stress/history")
async def stress_history(limit: int = 20) -> Dict[str, Any]:
    _engine_or_503()
    return {"history": svc.stress_history(limit=limit)}


@router.post("/stress/run")
async def stress_run(request: Request, payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    """
    发起压测（返回 `job_id`，用 `/jobs/{id}` 轮询实时日志与结果）。

    安全：目标必须是白名单 id（**不接受任意 URL**，否则这里就成了对任意主机
    打高并发的工具）；参数在服务端夹紧；危险组合要求 `confirm=true`。
    """
    _guard(request)
    _engine_or_503()
    params, err, need_confirm, why = svc.stress_validate(payload)
    if err:
        return JSONResponse(status_code=400, content={"ok": False, "error": err})
    if need_confirm and not payload.get("confirm"):
        return JSONResponse(status_code=409, content={
            "ok": False, "need_confirm": True, "impact": why,
            "error": "该压测参数影响面较大：%s，请确认后再开始" % why,
        })
    out = svc.MANAGER.submit("stress", svc.job_stress, params=params)
    return JSONResponse(status_code=200 if out.get("ok") else 409, content=out)


# ── 任务查询 ────────────────────────────────────────────────────────────────

@router.get("/jobs")
async def jobs(limit: int = 30) -> Dict[str, Any]:
    return {"busy": svc.MANAGER.busy(), "jobs": svc.MANAGER.list(limit=limit)}


@router.get("/jobs/{job_id}")
async def job_detail(job_id: str, since: int = 0) -> Dict[str, Any]:
    """
    任务详情。`since` 用于增量拉日志（只返回下标 >= since 的行），
    避免前端每次轮询都把整份日志拉一遍。
    """
    job = svc.MANAGER.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在: %s" % job_id)
    d = job.to_dict(with_logs=False)
    logs = job.logs[since:] if since > 0 else job.logs
    d["logs"] = logs
    d["log_offset"] = since + len(logs)
    return d


@router.post("/jobs/current/cancel")
async def cancel(request: Request) -> Dict[str, Any]:
    """
    停止当前任务（协作式取消；任务会在 ≤5s 内停止并**自动回滚**）。

    用途：误点了「批量演练」需要立刻中止。
    若想在任务已经结束后再彻底清一遍，用 `POST /api/chaos/cleanup`。
    """
    _guard(request)
    return svc.MANAGER.request_cancel()


@router.post("/jobs/current/abort")
async def abort_current(request: Request, payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
    """
    强制解除占用（危险出口）：把当前任务标记为失败并**释放单飞锁**。

    仅用于"任务卡死、协作式取消无效"的情况 —— 例如某条 `docker exec`
    长时间不返回（实测清理曾慢到 75.6s，期间所有操作都返回 409 busy，
    看起来像"停止按钮没用"）。**worker 线程可能仍在运行**，
    所以要求 `confirm=true`，响应里也会带 warning。
    """
    _guard(request)
    _engine_or_503()
    cur = svc.MANAGER.current()
    if cur is None or cur.status not in ("queued", "running"):
        return JSONResponse(status_code=200, content={"ok": True, "aborted": False,
                                                     "note": "没有占用中的任务"})
    if not payload.get("confirm"):
        return JSONResponse(status_code=409, content={
            "ok": False, "need_confirm": True,
            "impact": "强制解除占用会释放单飞锁，但原任务的线程**可能仍在收尾**，"
                      "之后可能与新任务短暂并发（例如清理还没跑完就又发起注入）。"
                      "建议先刷新任务状态、确认确实卡死再用。",
        })
    return JSONResponse(status_code=200, content=svc.MANAGER.force_abort(
        reason=str(payload.get("reason") or "被人工强制解除占用")))



@router.post("/stop_and_rollback")
async def stop_and_rollback(request: Request) -> Dict[str, Any]:
    """
    一键「停止并强制回滚」：
      ① 若有运行中的任务 → 请求取消（它自己会回滚）
      ② 立刻排队一次 cleanup（无状态兜底清理），等当前任务结束后执行

    这是给"误点演练"准备的紧急出口：不需要用户理解单飞锁与任务排队。
    """
    _guard(request)
    _engine_or_503()
    cur = svc.MANAGER.current()
    cancelled = False
    if cur and cur.status in ("queued", "running"):
        svc.MANAGER.request_cancel()
        cancelled = True
    # 无论有没有在跑的任务，都提交一次兜底清理；
    # 若有任务在跑，submit 会返回 busy —— 此时前端轮询到任务结束后再调一次即可，
    # 但因为 verify 任务自己会做收尾 cleanup，这里 busy 也是安全的。
    out = svc.MANAGER.submit("cleanup", svc.job_cleanup)
    return {"ok": True, "cancel_requested": cancelled,
            "cleanup_job": out.get("job_id") if out.get("ok") else None,
            "cleanup_busy": bool(out.get("busy")),
            "note": ("已请求停止，任务将自动回滚；"
                     if cancelled else "当前没有运行中的任务；")
                    + ("已排队执行兜底清理" if out.get("ok") else "清理将由当前任务的收尾阶段完成")}
