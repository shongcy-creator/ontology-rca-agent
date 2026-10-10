# -*- coding: utf-8 -*-
"""
诊断编排器 — 双引擎协同的统一入口。

流程（Q2 决策 A）：
  1. 本体确定性快检（始终执行，毫秒级，零幻觉）
  2. 路由判定：
       · 高置信度命中已知约束/根因 → 快路径直接返回
       · 否则 → 启动 LLM Agent，把确定性结论作为先验注入
  3. 落库（incident + agent run + thoughts）
  4. 返回统一结构（含路由依据、推理轨迹、证据链）

调用方只需关心 `diagnose()`，不必了解双引擎细节。
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional

from .agent import RCAAgent, AgentResult, AgentStep
from .llm_config import llm_config
from .rca_engine import RCAEngine
from .router import decide as route_decide
from .tools import build_default_registry
from . import agent_store, incident_store

log = logging.getLogger("rca.orchestrator")


def _fastpath_steps(seed: dict, route: dict) -> list:
    """
    为**确定性快路径**合成推理轨迹。

    为什么需要：快路径没有 LLM 的 observe/act 循环，`steps` 一直是空列表 ——
    前端把推理搬进聊天框后就成了"只有结论、没有过程"。
    但引擎其实**已经算过**这些中间结果（本体匹配、候选打分、阈值核对、传播路径），
    这里只是把它们如实摆出来，不编造任何新信息。
    """
    steps: list = []
    parsed = seed.get("alert") or {}
    scoring = seed.get("scoring") or {}
    rc = seed.get("root_cause") or {}
    n = 0

    n += 1
    steps.append(AgentStep(
        step_no=n, phase="observe",
        reasoning="解析告警文本，在激活本体中匹配约束关键词并推导候选类别",
        observation="告警关键词：%s；候选类别：%s；打分词典来源：%s（%s，索引 %s 个术语）" % (
            "、".join(parsed.get("keywords") or []) or "-",
            "、".join(parsed.get("matched_categories") or []) or "-",
            scoring.get("source") or "本体不可用（退回内置词典）",
            scoring.get("ontology_version") or "-",
            scoring.get("terms_indexed") or "-"),
    ))

    for c in (seed.get("candidates") or [])[:4]:
        n += 1
        steps.append(AgentStep(
            step_no=n, phase="reason",
            reasoning="本体打分候选：%s（%s）" % (c.get("entity_id"),
                                              c.get("entity_name") or c.get("category")),
            observation="%s · 打分来源 %s · confidence=%.3f" % (
                c.get("reason") or "-", c.get("scoring_source") or "-",
                float(c.get("confidence") or 0)),
        ))

    for t in (seed.get("thresholds_triggered") or []):
        n += 1
        steps.append(AgentStep(
            step_no=n, phase="act",
            reasoning="核对阈值规则：%s" % (t.get("rule") or "-"),
            observation="实测 %s → %s（%s）" % (t.get("metric") or "-", t.get("value"),
                                              t.get("status")),
            observation_ok=True,
        ))

    path = seed.get("root_cause_path") or []
    n += 1
    steps.append(AgentStep(
        step_no=n, phase="reason",
        reasoning="综合本体先验与可观测证据，选定传播路径并定论",
        observation="定论 %s（%s），confidence=%.3f；到用户可见入口的传播路径 %d 跳：%s" % (
            rc.get("entity_id") or "-", rc.get("category") or "-",
            float(seed.get("confidence") or 0), len(path),
            (" → ".join([str(h.get("from")) for h in path] + [str(path[-1].get("to"))])
             if path else "-")),
    ))

    if route.get("reason"):
        n += 1
        steps.append(AgentStep(
            step_no=n, phase="reason",
            reasoning="路由判定：走确定性快路径，跳过 LLM Agent",
            observation=str(route.get("reason")),
        ))
    return steps


def _deterministic_result(seed: dict, route: dict) -> AgentResult:
    """把确定性引擎输出包装成与 Agent 一致的结果结构。"""
    # ── 无故障出口：路由侧已判定"无故障"，这里不拼装任何根因/证据/动作 ──
    _nf = (route.get("signals") or {}).get("no_fault")
    if _nf:
        rc = {"category": "无故障", "entity_id": "",
              "description": "未发现可报告的故障证据（依据：%s）。确定性引擎直接判定无故障，"
                             "未调用 LLM（零 token）。" % _nf}
        seed = dict(seed, confidence=0.95, thresholds_triggered=[], candidates=[],
                    topology=[], topology_edges=[], root_cause_path=[])
    else:
        rc = seed.get("root_cause") or {}
    thresholds = seed.get("thresholds_triggered") or []
    evidence = []
    for t in thresholds:
        evidence.append({
            "source": t.get("metric") or t.get("rule"),
            "finding": t.get("rule"),
            "value": t.get("value"),
        })
    for c in (seed.get("candidates") or [])[:3]:
        evidence.append({
            "source": "ontology_diagnose",
            "finding": "候选根因 [%s] %s" % (c.get("category"), c.get("entity_id")),
            "value": "confidence=%s" % c.get("confidence"),
        })

    # 建议动作：**优先用本体里该根因术语的 remediation**（ontology_v4 起）。
    #
    # 为什么必须优先用本体：类别只有 5 个（数据/资源/配置/依赖/代码）而根因有 15 个，
    # 粒度天然对不上 —— 于是"磁盘临时表"这类根因拿到的是
    # "确认容器是否因内存/CPU 受限被杀"这种既不准确也不可执行的动作。
    # 取不到（老版本本体 / 未登记动作的术语）才回落到按类别的通用建议。
    # 每条动作都带 `source`，界面可据此显示"这条建议来自本体还是兜底表"。
    if _nf:
        # 无故障时不建议任何处置动作（也避免按类别直查兜底表）
        actions = []
    else:
        actions = _ontology_actions(rc)
        if not actions:
            actions = _suggest_actions(rc.get("category", ""))
            actions = [dict(a, source="category") for a in actions]
    steps = [] if _nf else _fastpath_steps(seed, route)
    _summary = (("判定为无故障：%s。未给出根因，也未调用 LLM。" % _nf) if _nf else
                ("命中已知本体模式（%s），确定性引擎直接定论" % route.get("reason", "")))

    return AgentResult(
        run_id="",
        incident_id=seed.get("incident_id", ""),
        mode="deterministic",
        status="completed",
        root_cause=rc,
        confidence=float(seed.get("confidence") or 0),
        evidence=evidence,
        affected_entities=[n["id"] for n in (seed.get("topology") or [])][:8],
        # 拓扑必须带出来，否则前端「拓扑」面板永远是空的（见 AgentResult 的注释）
        topology=(seed.get("topology") or []),
        topology_edges=(seed.get("topology_edges") or []),
        root_cause_path=(seed.get("root_cause_path") or []),
        reasoning_summary=_summary,
        next_actions=actions,
        # 快路径也要有"推理过程"（供聊天框展示）；steps_used 要如实反映条数，
        # 否则界面上会出现"展示了 9 步推理、却写着 0 步"的割裂
        steps=steps,
        steps_used=len(steps),
        total_tokens=0,
        latency_ms=float(seed.get("elapsed_ms") or 0),
        seed_agreement=True,
        tools_used=[],
        model="",
    )


def _ontology_actions(rc: dict) -> list:
    """
    取**根因术语自带**的处置动作（ontology_v4 的 `remediation`）；没有则返回空列表。

    归一化到与 `_suggest_actions` 相同的结构（urgency/action/command/needs_approval），
    额外加 `source="ontology"` 以便界面区分出处。结构对齐的意义是：
    下游（AgentResult.next_actions、前端渲染、markdown）完全不用改就能吃本体动作。
    """
    out: list = []
    for a in (rc or {}).get("remediation") or []:
        if not isinstance(a, dict):
            continue
        act = str(a.get("action") or "").strip()
        if not act:
            continue
        out.append({
            "urgency": str(a.get("urgency") or "P1"),
            "action": act,
            "command": str(a.get("command") or ""),
            # 缺省按"需审批"处理：宁可多要一次确认，也不让未标注意图的写操作直接执行
            "needs_approval": bool(a.get("needs_approval", True)),
            "source": "ontology",
        })
    return out


def _suggest_actions(category: str) -> list:
    """按根因类别给出只读排查建议（需人工执行）。"""
    table = {
        "数据": [
            {"urgency": "P0", "action": "检查是否存在长事务/锁等待阻塞写入",
             "command": "见 db_processlist / db_innodb_status 工具输出", "needs_approval": False},
            {"urgency": "P1", "action": "审查 t_txn 上的慢查询并补充/优化索引",
             "command": "EXPLAIN <慢SQL>", "needs_approval": True},
            {"urgency": "P1", "action": "评估连接池上限与 innodb_lock_wait_timeout 配置",
             "command": "", "needs_approval": True},
        ],
        "资源": [
            {"urgency": "P0", "action": "确认容器是否因内存/CPU 受限被杀或重启",
             "command": "docker inspect <container>", "needs_approval": False},
            {"urgency": "P1", "action": "评估容器资源限额是否需上调",
             "command": "", "needs_approval": True},
        ],
        "配置": [
            {"urgency": "P1", "action": "核对超时/连接池/重试参数是否与容量匹配",
             "command": "", "needs_approval": True},
        ],
        "依赖": [
            {"urgency": "P0", "action": "确认下游数据库/数据源可用性",
             "command": "见 metrics_targets 工具输出", "needs_approval": False},
        ],
        "代码": [
            {"urgency": "P1", "action": "定位异常堆栈对应的代码路径",
             "command": "见 env_container_logs 工具输出", "needs_approval": False},
        ],
    }
    return table.get(category, [
        {"urgency": "P1", "action": "对照 Prometheus 指标趋势与近期变更记录",
         "command": "", "needs_approval": False},
    ])


async def diagnose(
    alert: str,
    severity: str = "P1",
    force_mode: Optional[str] = None,
    on_event: Optional[Callable[[str, dict], None]] = None,
    persist: bool = True,
    app_name: str = "payment-app",
) -> Dict[str, Any]:
    """
    执行完整诊断。

    Args:
        alert: 告警/故障描述
        severity: P0-P3
        force_mode: 强制 'deterministic' 或 'agentic'（跳过路由判定）
        on_event: 事件回调（SSE 复用）
        persist: 是否落库
        app_name: 拓扑查询的应用名
    """
    t0 = time.perf_counter()
    cfg = llm_config()

    # ── 1. 本体确定性快检 ─────────────────────────────────────────
    seed: Optional[dict] = None
    seed_error = ""
    try:
        seed = await asyncio.to_thread(
            RCAEngine().infer, alert, severity, app_name, False
        )
    except Exception as e:  # noqa: BLE001
        seed_error = "%s: %s" % (type(e).__name__, e)

    # ── 2. 路由判定 ───────────────────────────────────────────────
    route = route_decide(seed, cfg)
    mode = force_mode or route.mode
    if force_mode and force_mode != route.mode:
        route.reason = "调用方强制指定 mode=%s（路由原判定 %s：%s）" % (
            force_mode, route.mode, route.reason)
        route.mode = force_mode
    if seed_error:
        # ⚠ 确定性引擎**抛异常**和**跑了但没结论**是两件事，绝不能都显示成"无输出"。
        #
        # 踩过的坑：`score_candidates` 里一次 `AttributeError`（把嵌套函数当方法调用）
        # 让候选恒为 0；因为异常只存进局部变量 `seed_error`，而它仅在
        # "引擎失败且 LLM 未配置" 时才被使用，于是有 LLM 时**完全静默降级** ——
        # 接口照常返回一个看起来正常的 agentic 结论，RCA 质量已经坏了却没有任何信号。
        # 现在：① 打 warning 进日志；② 把异常写进 route.reason（界面会显示路由理由）。
        log.warning("确定性引擎异常，降级为 LLM Agent：%s", seed_error)
        route.reason = "确定性引擎异常（%s），已降级为 LLM Agent；原判定：%s" % (
            seed_error, route.reason)

    if on_event:
        on_event("route", {
            "mode": mode,
            "reason": route.reason,
            "seed_confidence": route.seed_confidence,
            "seed": {
                "root_cause": (seed or {}).get("root_cause"),
                "confidence": (seed or {}).get("confidence"),
            } if seed else None,
        })

    # ── 3. 执行 ───────────────────────────────────────────────────
    if mode == "deterministic" and seed:
        result = _deterministic_result(seed, route.to_dict())
    elif seed is None and not llm_config().get("api_key"):
        # 引擎失败且无 LLM：明确报错
        return {
            "ok": False,
            "error": "确定性引擎失败且 LLM 未配置：%s" % seed_error,
            "route": route.to_dict(),
        }
    else:
        agent = RCAAgent(
            alert=alert, severity=severity, seed=seed,
            registry=build_default_registry(), config=cfg, on_event=on_event,
        )
        result = await agent.run()
        # Agent 路径同样把本体拓扑带上：RCAEngine 已经算过一遍（seed），
        # 没必要让前端"因为走的是 Agent 就没有拓扑"。
        if seed and not getattr(result, "topology", None):
            result.topology = seed.get("topology") or []
            result.topology_edges = seed.get("topology_edges") or []
            result.root_cause_path = seed.get("root_cause_path") or []

    payload = result.to_dict()
    payload["route"] = route.to_dict()
    payload["seed"] = {
        "incident_id": (seed or {}).get("incident_id"),
        "root_cause": (seed or {}).get("root_cause"),
        "confidence": (seed or {}).get("confidence"),
        "candidates": (seed or {}).get("candidates", [])[:5],
        "topology_nodes": len((seed or {}).get("topology") or []),
        "thresholds_triggered": (seed or {}).get("thresholds_triggered") or [],
    } if seed else None
    payload["total_latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    payload["ok"] = result.status in ("completed", "timeout", "budget_exceeded")

    # ── 4. 落库 ───────────────────────────────────────────────────
    if persist:
        incident_id = payload.get("incident_id") or ""
        try:
            # 复用 RCA 结果结构写 rca_incident（保持与既有查询兼容）
            if seed:
                merged = dict(seed)
                merged["incident_id"] = incident_id or seed.get("incident_id")
                merged["root_cause"] = payload.get("root_cause") or seed.get("root_cause")
                merged["confidence"] = payload.get("confidence")
                incident_store.save(merged["incident_id"], merged)
                payload["incident_id"] = merged["incident_id"]
                incident_id = merged["incident_id"]
            agent_store.save_run(result, alert, severity,
                                 route_reason=route.reason, incident_id=incident_id)
            if result.steps:
                agent_store.save_thoughts(result.run_id, result.steps)
            payload["persisted"] = True
        except Exception as e:  # noqa: BLE001
            payload["persisted"] = False
            payload["persist_error"] = "%s: %s" % (type(e).__name__, e)

        # 4b. 回填 EvoOntology 轨迹（自演化数据源；失败不阻断诊断）
        try:
            from . import evolution_trigger
            traj = evolution_trigger.record_diagnosis_trajectory(
                alert=alert,
                severity=severity,
                steps=result.steps,
                final_answer=payload.get("root_cause"),
                run_id=result.run_id,
            )
            payload["trajectory"] = traj
        except Exception as e:  # noqa: BLE001
            payload["trajectory"] = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    return payload
