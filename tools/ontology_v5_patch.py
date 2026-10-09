# -*- coding: utf-8 -*-
"""
第 5 轮本体增量：修正 remediation 里**不可执行的命令** —— 发布 ontology_v5。

## 这一轮要解决什么（由实测发现，不是推演出来的）

第 4 轮的评估口径 `remediation_contract` 只检查三件事：动作是否具体 /
urgency 是否合法 / 是否声明 needs_approval。**这三条都能在纸面上通过**，
而 `tools/remediation_efficacy.py`（注入真实故障 → 真跑诊断 → 执行 P0 命令）
实测出两类只有跑起来才会暴露的问题：

**① 命令指向不存在的指标**
    `rc:cpu-throttle` 的 P0 写的是 `rate(cc_container_cpu_cfs_throttled_periods_total[5m])`
    —— Prometheus 里**没有这个指标**（0 条 series）。正确名是
    `cc_container_cpu_throttled_seconds_total`。
    照着这条"建议"排查，操作人只会得到空结果：**看起来查过了，其实什么都没查到。**

**② 12 条把"散文"写进了 command 字段**
    例如 `按 app_instance 对比 P99`、`逐个副本 /health 探活`、`应用日志 / 错误码 1290`。
    界面按等宽命令渲染，既误导操作人，也无法被自动验证。

## 为什么值得单独走一轮（而不是"改几个字"）

本体是**版本化的事实来源**：内容改了就必须留下版本与成对评估，
否则"v4 里那 12 条不是命令、还有一条指标名是错的"这件事在演化史里就消失了。
更关键的是**把检查固化**：第 4 轮契约的漏洞（只验证纸面可用性）正是让这些错误溜过去的门。
本轮给契约补上第 ④⑤ 条后，同类错误会在 `accept` 前被拦住。

## 实现方式：程序化修正（按父版本匹配替换）

命令里的说明文字有 12 处、涉及 10 个术语，手抄整份 remediation 极易出错。
因此本补丁**读取父版本**，按"旧命令片段 → 新命令"的对照表**替换**，
只替换命中项，未命中的原样保留（并在返回值里报告未命中，便于发现父版本漂移）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

ROOT = Path(__file__).resolve().parent.parent
NOW = "2026-10-05T15:40:00Z"

#: 应用内各实例 P99（确认"只有某个副本慢"的标准查询）
_INSTANCE_P99 = ("max by (app_instance) (histogram_quantile(0.99, "
                 "sum by (le, app_instance) (rate(cc_http_request_duration_seconds_bucket[5m]))))")

#: 旧命令片段 → 新命令。键用"能唯一定位"的最短片段。
COMMAND_FIXES: Dict[str, str] = {
    # ① 指标名根本不存在（实测发现）
    "rate(cc_container_cpu_cfs_throttled_periods_total[5m])":
        "rate(cc_container_cpu_throttled_seconds_total[5m])",
    # ② 散文/含中文 → 真命令（或 ASCII 占位符）
    "按 app_instance 对比 P99": _INSTANCE_P99,
    "查看应用层实例级 QPS/P99 分布": _INSTANCE_P99,
    "逐个副本 /health 探活": 'probe_success{job="blackbox-app-replica"}',
    "docker inspect cc-app-gateway / nginx upstream 配置": "docker inspect cc-app-gateway",
    "probe_duration_seconds vs 应用内 P99": 'max(probe_duration_seconds{job="blackbox-app-replica"})',
    "cc_container_cpu_seconds / _memory_working_set_bytes":
        "max(cc_container_memory_working_set_bytes / cc_container_memory_limit_bytes)",
    "副本数 / cpus / mem_limit vs rate(cc_http_requests_total[5m])":
        'count(up{job="payment-app"} == 1)',
    "slow query top / performance_schema.events_statements_summary_by_digest":
        "SELECT DIGEST_TEXT, COUNT_STAR FROM "
        "performance_schema.events_statements_summary_by_digest "
        "ORDER BY SUM_TIMER_WAIT DESC LIMIT 5",
    "应用日志 / 错误码 1290": "",
    "对比主从同一主键的 updated_at": "SELECT MAX(txn_id) FROM t_txn",
    "EXPLAIN <慢SQL>（或 db_explain 工具）": "EXPLAIN <slow-sql>",
    "EXPLAIN <慢SQL>": "EXPLAIN <slow-sql>",
    "docker inspect -f '{{.State.Status}}' <replica> + /health 探活":
        "docker inspect -f '{{.State.Status}}' <replica>",
    "SHOW REPLICA STATUS\\G（Seconds_Behind_Source / GTID gap）": "SHOW REPLICA STATUS",
    "SHOW REPLICA STATUS\\G；exporter 抓取目标是否 down": "SHOW REPLICA STATUS",
    # 占位符统一 ASCII（契约第 ⑤ 条要求 command 不含 CJK）
    "docker restart <replica>": "docker restart <replica>",
}

#: 本轮不需要动的术语（只是记录：这些命令本来就是 ASCII 真命令）
_ALREADY_OK = {
    "rc:replica-loss", "rc:conn-exhaust", "rc:row-lock", "rc:tmp-disk",
    "rc:db-replica-loss", "rc:primary-readonly",
}


def _parent_terms() -> Dict[str, Dict[str, Any]]:
    """读取当前激活版本（即本轮父版本）的术语。"""
    act = json.loads((ROOT / ".evoontology" / "active.json").read_text(encoding="utf-8"))
    vdir = ROOT / ".evoontology" / "versions" / act["active_version"]
    terms = json.loads((vdir / "terms.json").read_text(encoding="utf-8"))
    return {t["id"]: t for t in terms if t.get("id")}


def build_patch(base_term_ids: Optional[Set[str]] = None,
                base_evidence_ids: Optional[Set[str]] = None,
                base_mapping_ids: Optional[Set[str]] = None,
                report: Optional[Dict[str, Any]] = None
                ) -> Dict[str, List[Dict[str, Any]]]:
    """
    按对照表修正 remediation 的 command 字段。

    只对**发生替换**的术语产出记录；命中数记进 `report`（供驱动打印与审计）。
    """
    base_term_ids = base_term_ids or set()
    parents = _parent_terms()
    terms: List[Dict[str, Any]] = []
    hits: Dict[str, List[str]] = {}
    missed = sorted(COMMAND_FIXES)

    for tid, t in sorted(parents.items()):
        if base_term_ids and tid not in base_term_ids:
            continue
        rem = [dict(x) for x in (t.get("remediation") or []) if isinstance(x, dict)]
        if not rem:
            continue
        changed: List[str] = []
        for a in rem:
            cmd = str(a.get("command") or "")
            if cmd in COMMAND_FIXES:
                new = COMMAND_FIXES[cmd]
                a["command"] = new
                changed.append("%s → %s" % (cmd[:34], new[:34] or "(空)"))
                if cmd in missed:
                    missed.remove(cmd)
        if changed:
            terms.append({"id": tid, "remediation": rem})
            hits[tid] = changed

    if report is not None:
        report["command_fixes"] = hits
        report["unmatched_fixes"] = missed
    return {"terms": terms, "mappings": [], "relations": [],
            "constraints": [], "evidence": []}
