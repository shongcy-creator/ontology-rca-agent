# -*- coding: utf-8 -*-
"""
Incident 存储 — MySQL 持久化 + 内存回退。

设计:
  - 优先写入 MySQL（rca_incident / rca_feedback / rca_trajectory）
  - MySQL 不可用时自动降级为内存存储，接口不变
  - 每次读取也优先走 MySQL，保证多实例一致
"""
from __future__ import annotations
import json
import os
import threading
from typing import Dict, List, Optional

# ── 内存回退存储 ──────────────────────────────────────────────────────────
_mem: Dict[str, dict] = {}
_order: List[str] = []
_lock = threading.Lock()

# ── MySQL 连接配置 ────────────────────────────────────────────────────────
DB_CONFIG = {
    "host":     os.environ.get("DB_HOST", "mysql"),
    "port":     int(os.environ.get("DB_PORT", "3306")),
    "user":     os.environ.get("DB_USER", "appuser"),
    "password": os.environ.get("DB_PASSWORD", "apppass"),
    "database": os.environ.get("DB_NAME", "creditcard"),
}

_conn = None
_conn_failed = False


def _get_conn():
    """获取 MySQL 连接（失败后不再重试，避免每次请求都超时）。"""
    global _conn, _conn_failed
    if _conn is not None:
        try:
            _conn.ping(reconnect=True)
            return _conn
        except Exception:
            _conn = None
    if _conn_failed:
        return None
    try:
        import pymysql
        _conn = pymysql.connect(
            host=DB_CONFIG["host"], port=DB_CONFIG["port"],
            user=DB_CONFIG["user"], password=DB_CONFIG["password"],
            database=DB_CONFIG["database"],
            charset="utf8mb4", autocommit=True,
            connect_timeout=3, read_timeout=5, write_timeout=5,
        )
        return _conn
    except Exception as e:
        print("[incident_store] MySQL unavailable, using memory store: %s" % e, flush=True)
        _conn_failed = True
        return None


def reset_mysql_state() -> None:
    """允许后续重试 MySQL 连接（用于健康检查恢复）。"""
    global _conn_failed
    _conn_failed = False


# ── 写入 ──────────────────────────────────────────────────────────────────

def save(incident_id: str, result: dict) -> None:
    """保存 RCA 结果。"""
    if not incident_id:
        return

    # 内存（始终写入，作为缓存）
    with _lock:
        if incident_id not in _mem:
            _order.append(incident_id)
        _mem[incident_id] = result

    # MySQL
    conn = _get_conn()
    if conn is None:
        return
    try:
        alert = result.get("alert") or {}
        rc = result.get("root_cause") or {}
        topology = result.get("topology") or []
        edges = result.get("topology_edges") or []

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rca_incident
                  (incident_id, alert_message, severity, root_category, root_entity,
                   confidence, elapsed_ms, topology_nodes, topology_edges, payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                  alert_message=VALUES(alert_message),
                  severity=VALUES(severity),
                  root_category=VALUES(root_category),
                  root_entity=VALUES(root_entity),
                  confidence=VALUES(confidence),
                  elapsed_ms=VALUES(elapsed_ms),
                  topology_nodes=VALUES(topology_nodes),
                  topology_edges=VALUES(topology_edges),
                  payload=VALUES(payload)
                """,
                (
                    incident_id,
                    str(alert.get("message", ""))[:60000],
                    str(alert.get("severity", "P1"))[:8],
                    str(rc.get("category", ""))[:32],
                    str(rc.get("entity_id", ""))[:128],
                    float(rc.get("confidence", 0) or 0),
                    float(result.get("elapsed_ms", 0) or 0),
                    len(topology),
                    len(edges),
                    json.dumps(result, ensure_ascii=False, default=str),
                ),
            )

            # 写入分析轨迹（供本体自演化使用）
            cur.execute("DELETE FROM rca_trajectory WHERE incident_id=%s", (incident_id,))
            for step in (result.get("rca_chain") or []):
                cur.execute(
                    """
                    INSERT INTO rca_trajectory
                      (incident_id, step_no, step_type, description, payload)
                    VALUES (%s,%s,%s,%s,%s)
                    """,
                    (
                        incident_id,
                        int(step.get("step", 0) or 0),
                        str(step.get("type", ""))[:32],
                        str(step.get("description", ""))[:60000],
                        json.dumps(step, ensure_ascii=False, default=str),
                    ),
                )
    except Exception as e:
        print("[incident_store] MySQL save failed, kept in memory: %s" % e, flush=True)


def add_feedback(incident_id: str, verdict: str, actual_cause: str = "",
                 note: str = "", operator: str = "") -> bool:
    """记录运维反馈（用于本体自演化）。"""
    conn = _get_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rca_feedback
                  (incident_id, verdict, actual_cause, note, operator)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (incident_id, verdict[:16], actual_cause[:256], note, operator[:64]),
            )
        return True
    except Exception as e:
        print("[incident_store] feedback failed: %s" % e, flush=True)
        return False


# ── 读取 ──────────────────────────────────────────────────────────────────

def get(incident_id: str) -> Optional[dict]:
    """读取单个 incident 的完整结果。"""
    # 内存优先（快）
    with _lock:
        if incident_id in _mem:
            return _mem[incident_id]

    conn = _get_conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM rca_incident WHERE incident_id=%s", (incident_id,))
            row = cur.fetchone()
            if not row:
                return None
            payload = row[0]
            result = json.loads(payload) if isinstance(payload, str) else payload
            with _lock:
                _mem[incident_id] = result
                if incident_id not in _order:
                    _order.append(incident_id)
            return result
    except Exception as e:
        print("[incident_store] MySQL get failed: %s" % e, flush=True)
        return None


def exists(incident_id: str) -> bool:
    return get(incident_id) is not None


def list_all(limit: int = 100) -> List[dict]:
    """列出 incident 摘要（倒序）。"""
    conn = _get_conn()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT incident_id, alert_message, severity, root_category,
                           root_entity, confidence, elapsed_ms, created_at
                    FROM rca_incident
                    ORDER BY created_at DESC, incident_id DESC
                    LIMIT %s
                    """,
                    (int(limit),),
                )
                rows = cur.fetchall()
                out = []
                for r in rows:
                    out.append({
                        "incident_id": r[0],
                        "message": r[1] or "",
                        "severity": r[2] or "",
                        "category": r[3] or "",
                        "entity_id": r[4] or "",
                        "confidence": float(r[5] or 0),
                        "elapsed_ms": float(r[6] or 0),
                        "created_at": str(r[7]) if r[7] else "",
                    })
                return out
        except Exception as e:
            print("[incident_store] MySQL list failed, fallback to memory: %s" % e, flush=True)

    # 内存回退
    out = []
    with _lock:
        for iid in reversed(_order[-limit:]):
            r = _mem.get(iid, {})
            rc = r.get("root_cause") or {}
            alert = r.get("alert") or {}
            out.append({
                "incident_id": iid,
                "message": alert.get("message", ""),
                "severity": alert.get("severity", ""),
                "category": rc.get("category", ""),
                "entity_id": rc.get("entity_id", ""),
                "confidence": rc.get("confidence", 0),
                "elapsed_ms": r.get("elapsed_ms", 0),
                "created_at": "",
            })
    return out


def count() -> int:
    """incident 总数。"""
    conn = _get_conn()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM rca_incident")
                return int(cur.fetchone()[0])
        except Exception:
            pass
    with _lock:
        return len(_mem)


def stats() -> dict:
    """聚合统计（按根因类别 / 严重级别）。"""
    conn = _get_conn()
    if conn is None:
        return {"backend": "memory", "total": count(), "by_category": {}, "by_severity": {}}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT root_category, COUNT(*) FROM rca_incident GROUP BY root_category")
            by_cat = {r[0] or "?": int(r[1]) for r in cur.fetchall()}
            cur.execute("SELECT severity, COUNT(*) FROM rca_incident GROUP BY severity")
            by_sev = {r[0] or "?": int(r[1]) for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) FROM rca_feedback")
            fb = int(cur.fetchone()[0])
        return {
            "backend": "mysql",
            "total": count(),
            "by_category": by_cat,
            "by_severity": by_sev,
            "feedback_count": fb,
        }
    except Exception as e:
        return {"backend": "mysql-error", "error": str(e), "total": count()}


def trajectories(incident_id: str) -> List[dict]:
    """读取某 incident 的分析轨迹。"""
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT step_no, step_type, description, payload FROM rca_trajectory "
                "WHERE incident_id=%s ORDER BY step_no",
                (incident_id,),
            )
            return [
                {"step": r[0], "type": r[1], "description": r[2],
                 "payload": json.loads(r[3]) if isinstance(r[3], str) else r[3]}
                for r in cur.fetchall()
            ]
    except Exception:
        return []
