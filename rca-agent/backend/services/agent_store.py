# -*- coding: utf-8 -*-
"""
Agent 运行记录持久化。

落库内容（对齐 Dify 的 MessageAgentThought）：
  · rca_agent_run     一次诊断的元信息与最终结论
  · rca_agent_thought 每一步的推理与工具观察 —— 支持完整回放与审计

MySQL 不可用时静默降级（不阻断诊断）。
"""
from __future__ import annotations
import json
from typing import Any, Dict, List, Optional


def _conn():
    try:
        from .incident_store import _get_conn
        return _get_conn()
    except Exception:
        return None


def _cursor(conn):
    """
    显式请求 DictCursor。

    incident_store 的连接使用默认（元组）游标，其内部按位置索引访问；
    本模块需要按列名访问，因此每个游标都显式指定 DictCursor，
    避免耦合到连接的 cursorclass 默认值。
    """
    import pymysql
    return conn.cursor(pymysql.cursors.DictCursor)


#: 运行表上"后来才需要"的列 → 建表语句。
#: 为什么要有这个：`04_agent_schema.sql` 只在**首次初始化**数据库时执行，
#: 已有环境的表不会自动长出新列 —— 而"按模型精算费用"需要输入/输出 token 分列
#: （两者单价差 4~5 倍）。所以这里做一次幂等迁移，避免"新代码 + 旧表"静默丢数据。
_LATE_COLUMNS: Dict[str, str] = {
    "prompt_tokens": "INT DEFAULT 0",
    "completion_tokens": "INT DEFAULT 0",
}
_migrated = False


def _ensure_columns(conn) -> None:
    """幂等补齐后加的列（已补过就跳过；失败不阻断诊断）。"""
    global _migrated
    if _migrated:
        return
    try:
        with _cursor(conn) as cur:
            cur.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'rca_agent_run'")
            have = {r["COLUMN_NAME"] for r in (cur.fetchall() or [])}
            for name, ddl in _LATE_COLUMNS.items():
                if name not in have:
                    cur.execute("ALTER TABLE rca_agent_run ADD COLUMN %s %s" % (name, ddl))
        _migrated = True
    except Exception:
        pass


def save_run(result: Any, alert: str, severity: str,
             route_reason: str = "", incident_id: str = "") -> bool:
    """保存一次 Agent 运行。result 为 AgentResult 或已 to_dict() 的 dict。"""
    conn = _conn()
    if conn is None:
        return False

    d = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    rc = d.get("root_cause") or {}
    _ensure_columns(conn)

    try:
        with _cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO rca_agent_run
                  (run_id, incident_id, alert_message, severity, mode, status,
                   route_reason, steps_used, tools_used, total_tokens, prompt_tokens,
                   completion_tokens, latency_ms,
                   confidence, model, seed_agreement, root_entity, root_category,
                   final_answer, error)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                  status=VALUES(status), steps_used=VALUES(steps_used),
                  tools_used=VALUES(tools_used), total_tokens=VALUES(total_tokens),
                  prompt_tokens=VALUES(prompt_tokens),
                  completion_tokens=VALUES(completion_tokens),
                  latency_ms=VALUES(latency_ms), confidence=VALUES(confidence),
                  model=VALUES(model),
                  root_entity=VALUES(root_entity), root_category=VALUES(root_category),
                  final_answer=VALUES(final_answer), error=VALUES(error)
                """,
                (
                    d.get("run_id", ""),
                    incident_id or d.get("incident_id", ""),
                    (alert or "")[:60000],
                    (severity or "P1")[:8],
                    (d.get("mode") or "agentic")[:16],
                    (d.get("status") or "completed")[:24],
                    (route_reason or "")[:60000],
                    int(d.get("steps_used") or 0),
                    ",".join(d.get("tools_used") or [])[:2000],
                    int(d.get("total_tokens") or 0),
                    int(d.get("prompt_tokens") or 0),
                    int(d.get("completion_tokens") or 0),
                    int(d.get("latency_ms") or 0),
                    float(d.get("confidence") or 0),
                    (d.get("model") or "")[:64],
                    (None if d.get("seed_agreement") is None
                     else (1 if d.get("seed_agreement") else 0)),
                    str(rc.get("entity_id") or "")[:128],
                    str(rc.get("category") or "")[:32],
                    json.dumps(d, ensure_ascii=False, default=str),
                    (d.get("error") or "")[:60000] or None,
                ),
            )
        return True
    except Exception as e:
        print("[agent_store] save_run failed: %s" % e, flush=True)
        return False


def save_thoughts(run_id: str, steps: List[Any]) -> bool:
    """保存推理/行动步骤（用于回放）。"""
    conn = _conn()
    if conn is None or not run_id:
        return False
    try:
        with _cursor(conn) as cur:
            cur.execute("DELETE FROM rca_agent_thought WHERE run_id=%s", (run_id,))
            for s in steps:
                d = s.to_dict() if hasattr(s, "to_dict") else dict(s)
                cur.execute(
                    """
                    INSERT INTO rca_agent_thought
                      (run_id, step_no, phase, reasoning, tool_name, tool_input,
                       observation, observation_ok, tokens, latency_ms)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        run_id,
                        int(d.get("step") or 0),
                        (d.get("phase") or "")[:16],
                        d.get("reasoning") or None,
                        (d.get("tool_name") or "")[:64] or None,
                        json.dumps(d.get("tool_input") or {}, ensure_ascii=False),
                        (d.get("observation") or "")[:60000] or None,
                        1 if d.get("observation_ok", True) else 0,
                        int(d.get("tokens") or 0),
                        int(d.get("latency_ms") or 0),
                    ),
                )
        return True
    except Exception as e:
        print("[agent_store] save_thoughts failed: %s" % e, flush=True)
        return False


def get_run(run_id: str) -> Optional[dict]:
    conn = _conn()
    if conn is None:
        return None
    try:
        with _cursor(conn) as cur:
            cur.execute(
                "SELECT run_id, incident_id, alert_message, severity, mode, status, "
                "route_reason, steps_used, tools_used, total_tokens, latency_ms, "
                "confidence, model, seed_agreement, root_entity, root_category, "
                "final_answer, error, created_at "
                "FROM rca_agent_run WHERE run_id=%s", (run_id,))
            row = cur.fetchone()
            if not row:
                return None
            d = row if isinstance(row, dict) else {}
            if isinstance(d.get("final_answer"), str):
                try:
                    d["final_answer"] = json.loads(d["final_answer"])
                except json.JSONDecodeError:
                    pass
            if d.get("created_at") is not None:
                d["created_at"] = str(d["created_at"])
            return d
    except Exception as e:
        print("[agent_store] get_run failed: %s" % e, flush=True)
        return None


def get_thoughts(run_id: str) -> List[dict]:
    conn = _conn()
    if conn is None:
        return []
    try:
        with _cursor(conn) as cur:
            cur.execute(
                "SELECT step_no, phase, reasoning, tool_name, tool_input, "
                "observation, observation_ok, tokens, latency_ms "
                "FROM rca_agent_thought WHERE run_id=%s ORDER BY thought_id",
                (run_id,))
            rows = cur.fetchall() or []
        out = []
        for r in rows:
            d = r if isinstance(r, dict) else {}
            tin = d.get("tool_input")
            if isinstance(tin, str):
                try:
                    d["tool_input"] = json.loads(tin)
                except json.JSONDecodeError:
                    pass
            out.append(d)
        return out
    except Exception as e:
        print("[agent_store] get_thoughts failed: %s" % e, flush=True)
        return []


def list_runs(limit: int = 30, mode: Optional[str] = None) -> List[dict]:
    conn = _conn()
    if conn is None:
        return []
    try:
        sql = ("SELECT run_id, incident_id, severity, mode, status, steps_used, "
               "total_tokens, latency_ms, confidence, model, root_entity, "
               "root_category, seed_agreement, created_at, alert_message "
               "FROM rca_agent_run ")
        params: List[Any] = []
        if mode:
            sql += "WHERE mode=%s "
            params.append(mode)
        sql += "ORDER BY created_at DESC LIMIT %s"
        params.append(int(limit))
        with _cursor(conn) as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall() or []
        out = []
        for r in rows:
            d = dict(r) if isinstance(r, dict) else {}
            if d.get("created_at") is not None:
                d["created_at"] = str(d["created_at"])
            if "alert_message" in d and d["alert_message"]:
                d["alert_message"] = d["alert_message"][:200]
            if d.get("seed_agreement") is not None:
                d["seed_agreement"] = bool(d["seed_agreement"])
            out.append(d)
        return out
    except Exception as e:
        print("[agent_store] list_runs failed: %s" % e, flush=True)
        return []


def stats() -> dict:
    conn = _conn()
    if conn is None:
        return {"backend": "unavailable"}
    # ⚠ 这里也必须先迁移：`stats()` 的按模型聚合会引用 `prompt_tokens` 等新列，
    # 列不存在时整条查询报错 → 整个 `/cost` 退化成 `{"backend":"error"}`、
    # token 显示为 0（踩过：迁移只在 save_run 里跑，于是重启后第一次读就出错）。
    _ensure_columns(conn)
    try:
        with _cursor(conn) as cur:
            cur.execute("SELECT COUNT(*), SUM(total_tokens), AVG(latency_ms), AVG(confidence) FROM rca_agent_run")
            row = cur.fetchone()
            row = row if isinstance(row, dict) else {}
            vals = list(row.values()) if row else [0, 0, 0, 0]
            cur.execute("SELECT mode, COUNT(*) AS n FROM rca_agent_run GROUP BY mode")
            by_mode = {r["mode"]: r["n"] for r in (cur.fetchall() or []) if isinstance(r, dict)}
            cur.execute("SELECT status, COUNT(*) AS n FROM rca_agent_run GROUP BY status")
            by_status = {r["status"]: r["n"] for r in (cur.fetchall() or []) if isinstance(r, dict)}
            cur.execute("SELECT COUNT(*) AS n FROM rca_agent_thought")
            thoughts = cur.fetchone()
            thoughts = list(thoughts.values())[0] if isinstance(thoughts, dict) and thoughts else 0
            # 按模型聚合（输入/输出分开）—— 按模型精算费用的依据（§11.21 第 17 项）
            cur.execute(
                "SELECT COALESCE(model,'') AS model, COUNT(*) AS n, "
                "COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
                "COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
                "COALESCE(SUM(total_tokens),0) AS total_tokens "
                "FROM rca_agent_run GROUP BY COALESCE(model,'') ORDER BY total_tokens DESC")
            by_model = {}
            for r in (cur.fetchall() or []):
                if not isinstance(r, dict):
                    continue
                by_model[r.get("model") or "(未记录)"] = {
                    "runs": int(r.get("n") or 0),
                    "prompt_tokens": int(r.get("prompt_tokens") or 0),
                    "completion_tokens": int(r.get("completion_tokens") or 0),
                    "total_tokens": int(r.get("total_tokens") or 0),
                }
        return {
            "backend": "mysql",
            "total_runs": int(vals[0] or 0),
            "total_tokens": int(vals[1] or 0),
            "avg_latency_ms": round(float(vals[2] or 0), 1),
            "avg_confidence": round(float(vals[3] or 0), 3),
            "by_mode": by_mode,
            "by_status": by_status,
            "by_model": by_model,
            "total_thoughts": int(thoughts or 0),
        }
    except Exception as e:
        return {"backend": "error", "error": str(e)}
