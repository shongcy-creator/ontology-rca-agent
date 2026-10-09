# -*- coding: utf-8 -*-
"""
本体自演化触发（EvoOntology EvolutionTrigger 集成）。

机制：
  · 每次诊断落库后，累积"分析轨迹"（rca_trajectory 已由 incident_store 写入）
  · 当新轨迹数 ≥ 30 或距上次演化 ≥ 7 天，触发一轮自演化
  · 调用 EvoOntology MCP 的 evolution_status / start_evolution_run
  · 结果写入 evolution_log 便于审计

注意：自演化会修改本体（新增 Term/Mapping/Constraint/Evidence），
需谨慎；此处只做"触发条件检测 + 状态查询"，实际启动演化需人工确认
（通过 /api/agent/evolution 显式触发），符合"本体演进需审核"的原则。
"""
from __future__ import annotations
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

# 触发阈值（与 EvoOntology EvolutionTrigger 默认对齐）
MIN_NEW_TRAJECTORIES = int(os.environ.get("RCA_EVOLVE_MIN_TRAJECTORIES", "30"))
MIN_DAYS = int(os.environ.get("RCA_EVOLVE_MIN_DAYS", "7"))


def _trajectory_count() -> int:
    try:
        from .incident_store import _get_conn
        conn = _get_conn()
        if conn is None:
            return 0
        import pymysql
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS n FROM rca_trajectory")
            row = cur.fetchone()
            return int((row or {}).get("n", 0) or 0)
    except Exception as e:
        print("[evolution] trajectory count failed: %s" % e, flush=True)
        return 0


def _last_evolution_time() -> Optional[float]:
    """从本体工作区读取最近一次演化的时间戳（若有记录）。"""
    try:
        ws = os.environ.get("EVO_WORKSPACE", "")
        if not ws or not Path(ws).is_dir():
            return None
        marker = Path(ws) / "evolution_state.json"
        if marker.is_file():
            data = json.loads(marker.read_text(encoding="utf-8"))
            return float(data.get("last_evolution_ts", 0) or 0)
    except Exception:
        pass
    return None


def _evo_call(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """调用 EvoOntology MCP 工具（复用 evo_ontology 客户端）。"""
    try:
        from .evo_ontology import EvoOntologyClient
        c = EvoOntologyClient()
        # 自动注入 workspace（MCP 工具大多要求显式 workspace 参数）
        full_args = dict(args)
        full_args.setdefault("workspace", c.workspace)
        return c._call(tool, full_args)
    except Exception as e:
        return {"status": "error", "error": "%s: %s" % (type(e).__name__, e)}


def check_evolution_status() -> Dict[str, Any]:
    """查询自演化状态与触发条件是否满足。"""
    n_traj = _trajectory_count()
    last_ts = _last_evolution_time()
    days_since = (time.time() - last_ts) / 86400 if last_ts else float("inf")

    by_traj = n_traj >= MIN_NEW_TRAJECTORIES
    by_days = days_since >= MIN_DAYS

    # 从 EvoOntology 获取官方状态
    evo_status = _evo_call("evolution_status", {})

    return {
        "new_trajectories": n_traj,
        "threshold_trajectories": MIN_NEW_TRAJECTORIES,
        "trigger_by_trajectories": by_traj,
        "last_evolution_ts": last_ts,
        "days_since_last": round(days_since, 1) if last_ts else None,
        "threshold_days": MIN_DAYS,
        "trigger_by_days": by_days,
        "ready": bool(by_traj or by_days),
        "evoontology": evo_status,
    }


def trigger_evolution(reason: str = "manual") -> Dict[str, Any]:
    """
    显式触发一轮自演化（需人工调用确认）。

    返回演化运行 ID 与状态；本体修改由 EvoOntology 负责。
    """
    status = check_evolution_status()
    if not status["ready"]:
        return {
            "started": False,
            "reason": "触发条件未满足",
            "status": status,
        }

    result = _evo_call("start_evolution_run", {
        "workspace": os.environ.get("EVO_WORKSPACE", ""),
        "reason": reason,
    })

    # 记录演化时间戳（本地 marker，便于下次判断）
    if result.get("status") in ("ok", "started", "success"):
        try:
            ws = os.environ.get("EVO_WORKSPACE", "")
            if ws and Path(ws).is_dir():
                marker = Path(ws) / "evolution_state.json"
                marker.write_text(json.dumps({
                    "last_evolution_ts": time.time(),
                    "reason": reason,
                    "result": result,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    return {
        "started": result.get("status") in ("ok", "started", "success"),
        "reason": reason,
        "result": result,
        "status": status,
    }


# ── 轨迹记录（喂给自演化）────────────────────────────────────────────────

def record_diagnosis_trajectory(
    alert: str,
    severity: str,
    steps: list,
    final_answer: Any,
    run_id: str = "",
    version: str = "",
) -> Dict[str, Any]:
    """
    把一次 RCA 诊断记录为 EvoOntology 的 ontology task（轨迹）。

    自演化的数据来源是 EvoOntology 协议里的 trajectory（ontology task），
    而非我们的 MySQL 表。因此诊断完成后需通过本函数回填轨迹。

    用 ProjectWorkflow 直接（进程内，比 MCP 往返更可靠）：
      prepare(question) → start_task → record_event×N → finish_task

    失败不抛出异常，不影响诊断主流程。
    """
    import hashlib

    ws = os.environ.get("EVO_WORKSPACE", "")
    if not ws or not Path(ws).is_dir():
        return {"ok": False, "error": "EVO_WORKSPACE 未配置或目录不存在"}

    try:
        from evoontology.workflow import ProjectWorkflow, question_key
    except ImportError:
        # 容器内 PYTHONPATH 已含 /app/vendor/EvoOntology；本地测试需手动加
        import sys as _sys
        try:
            from .evo_ontology import EvoOntologyClient
            vendor = EvoOntologyClient().evo_path
        except Exception:
            vendor = ""
        if vendor and Path(vendor).is_dir() and vendor not in _sys.path:
            _sys.path.insert(0, vendor)
        from evoontology.workflow import ProjectWorkflow, question_key

    out: Dict[str, Any] = {"ok": False, "question_id": "", "task_id": ""}

    try:
        wf = ProjectWorkflow(ws)

        # 1. 准备问题（幂等，按文本去重）
        wf.prepare(questions=[{"question": (alert or "").strip()}])

        # 2. 确定性 question_id（与 workflow.question_key 一致）
        key = question_key((alert or "").strip())
        qid = "q_" + hashlib.sha256(key.encode()).hexdigest()[:16]
        out["question_id"] = qid

        # 3. 开始任务
        task = wf.start_task(qid, version="active", batch_id="rca-agent")
        task_id = task["task_id"]
        out["task_id"] = task_id

        # 4. 记录工具事件（真实轨迹：observe/act）
        recorded = 0
        for i, step in enumerate(steps or []):
            d = step.to_dict() if hasattr(step, "to_dict") else dict(step or {})
            phase = d.get("phase", "")
            tool = d.get("tool_name", "")
            if phase not in ("act", "observe") or not tool:
                continue
            wf.record_event(
                task_id, tool,
                d.get("tool_input") or {},
                {"observation": (d.get("observation") or "")[:2000]},
                "%s-%d" % (task_id, i),
                error=not bool(d.get("observation_ok", True)),
            )
            recorded += 1

        # 5. 确定性快路径无工具事件时，补记一条本体诊断观察（保证 completed 合法）
        if recorded == 0:
            wf.record_event(
                task_id, "ontology_diagnose", {},
                {"observation": "deterministic fast path: %s" % json.dumps(
                    final_answer or {}, ensure_ascii=False, default=str)[:1500]},
                "%s-observe" % task_id,
                error=False,
            )
            recorded = 1

        # 6. 完成任务（持久化轨迹）
        wf.finish_task(task_id, {"root_cause": final_answer or {}, "run_id": run_id},
                       status="completed")

        out.update({"ok": True, "recorded_steps": recorded})
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out
