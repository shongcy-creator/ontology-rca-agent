# -*- coding: utf-8 -*-
"""
第 6 轮本体迭代的候选 patch：**区分"内存压力"与"真的被 OOM Kill"**。

## 为什么需要这一轮（问题来自实测，不是凭空加的）

端到端严格口径的剩余未命中里有 `res_cluster_memory`（应用集群内存全面耗尽）：
引擎给 `rc:oom-kill`，期望是 `rc:cluster-capacity`。查本体后发现**是本体自己说不清**：

| 记录 | 现状 | 矛盾点 |
|---|---|---|
| `rc:oom-kill` 的 definition | "容器 cgroup 内存用量触顶，内核 OOM Killer 终止容器内进程；**docker inspect 的 OOMKilled=true / RestartCount 递增是直接证据**" | 要求"已被杀"的直接证据 |
| `con:oom-detection` 的 trigger_keywords | 含 **"内存压力"**、`oom`、`oomkill`、`内存超限`、`内存不足` | 把"**压力**"也算成了 OOM Kill 的证据 → 与上面那条定义直接冲突 |
| `metric:mem-pressure` 的 definition | "逼近 1.0 时容器**随时**被 OOM Killer 杀死" | 压力 ≠ 已发生 |
| 现实有没有"压力但没被杀"的概念 | **没有** —— 只有 `rc:oom-kill`（已杀）与 `rc:cluster-capacity`（容量不足），中间那一段无处可去 | — |

于是"内存逼近限额但 cgroup `oom_kill` 计数为 0"这类故障，只能被判成 `rc:oom-kill`（过度断言）
或 `rc:cluster-capacity`（语义泛化）。**上界与下界都有术语，中间那段没有。**

## 这一轮做什么

1. **新增 `rc:mem-pressure`**（内存压力：逼近 cgroup 限额，但 `oom_kill` 计数未增加）
   —— 把"压力尚未变成杀死"这一状态显式建模；
2. **收窄 `con:oom-detection` 的关键词**：去掉"内存压力/内存不足"这类**症状词**，
   只保留"**已被杀死**"的证据词，使 `rc:oom-kill` 回到它自己定义的口径；
3. **新增 `con:mem-pressure`**（target = 新术语），给新根因提供打分关键词；
4. **新增 `metric:oom-kill-count`**（`cc_container_oom_kill_total`，cgroup `memory.events` 的
   只增计数器，第 1 项为修告警盲区而加的真实指标）并把它作为 `rc:oom-kill` 的观测佐证
   —— 这样"到底杀没杀"在**指标上可判别**，而不是靠措辞猜；
5. **`api:auth` 改 `lifecycle.state=draft`**（卫生闸门的老 warning：声明 active 但无证据、
   环境里也没实现该接口）；
6. 顺手补一条证据记录，供上面两个术语引用。

> 注意：`metric:replica-healthy-count` **不在本轮**。原先以为它名字写错了，
> 复查发现它的 name 是"在册健康副本数"（正确）—— 那句 `观测「根因：应用副本丢失」`
> 是我在"按证据召回锚点"的 reason 文案里把**根因名**当成了**观测名**，属代码 bug，
> 已在 rca_engine 里单独修（不需要走本体轮次）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

ROOT = Path(__file__).resolve().parent.parent

#: 新术语：内存压力（压力但未被杀）
NEW_TERMS: List[Dict[str, Any]] = [
    {
        "id": "rc:mem-pressure",
        "name": "根因：容器内存压力逼近上限（未被 OOM Kill）",
        "type": "entity",
        "scope": "RCA 支撑",
        "definition": (
            "容器 cgroup 内存用量持续逼近限额（working_set/limit → 1.0），"
            "但 **cgroup memory.events 的 oom_kill 计数未增加** —— 也就是"
            "内存被压到极限、随时可能被杀，却还没有真的被杀。"
            "与 rc:oom-kill 的区别是**是否已发生杀死**：判据看 "
            "`increase(cc_container_oom_kill_total[5m])`，为 0 即属本术语。"
        ),
        "aliases": ["RootMemPressure", "MemPressureNotKilled"],
        "evidence_refs": ["ev-cgroup-memory-events"],
        "lifecycle": {"state": "active"},
    },
    {
        "id": "metric:oom-kill-count",
        "name": "容器 OOM Kill 计数",
        "type": "metric",
        "scope": "RCA 支撑",
        "definition": (
            "`cc_container_oom_kill_total`：cgroup v2 `memory.events` 的 oom_kill 字段，"
            "**只增计数器**。`increase(...[5m]) > 0` 是"
            "「这个容器真的被内核 OOM Killer 杀过」的直接证据；恒为 0 则说明只有压力、没有杀死。"
        ),
        "aliases": ["OomKillTotal", "CgroupOomKillCounter"],
        "evidence_refs": ["ev-cgroup-memory-events"],
        "lifecycle": {"state": "active"},
    },
]

#: 对既有术语的字段级修正
TERM_FIXES: List[Dict[str, Any]] = [
    # 卫生闸门的 warning：声明 active 但无 evidence_refs，且模拟环境里并未实现该接口
    {"id": "api:auth", "lifecycle": {"state": "draft"}},
]

#: 新约束：给新根因提供打分关键词（引擎的关键词来自"以它为 target 的约束"）
NEW_CONSTRAINTS: List[Dict[str, Any]] = [
    {
        "id": "con:mem-pressure",
        "target": "rc:mem-pressure",
        "constraint_type": "business_rule",
        "trigger_keywords": [
            "内存压力", "内存逼近上限", "逼近 cgroup 限额", "memory pressure",
            "working set", "未 OOM", "未被杀死",
        ],
        "severity": "warn",
        "scope": "app:payment-app-cluster",
        "confidence": "high",
        "description": (
            "容器内存用量逼近 cgroup 限额但 oom_kill 计数未增加时，判为内存压力（尚未被杀）；"
            "这条约束要求引擎先看 `increase(cc_container_oom_kill_total[5m])` 再下结论。"
        ),
        "evidence_refs": ["ev-cgroup-memory-events"],
    },
]

#: 收窄既有约束的关键词：把"症状词"从 OOM Kill 的证据里拿掉
CONSTRAINT_FIXES: List[Dict[str, Any]] = [
    {
        "id": "con:oom-detection",
        # 原关键词含"内存压力/内存不足" —— 那是**压力**，不是**已被杀**，
        # 与 rc:oom-kill 自己的定义（要求 OOMKilled=true / RestartCount 递增）矛盾。
        "trigger_keywords": ["oom", "oomkill", "OOMKilled", "被内核杀死",
                             "oom_kill 计数增加", "容器重启"],
    },
]

#: 新关系：新术语必须"可归因 + 有观测佐证"（否则卫生检查会判不可确认）
NEW_RELATIONS: List[Dict[str, Any]] = [
    {
        "id": "rel:rc-mem-pressure-attr",
        "source": "rc:mem-pressure",
        "relation_type": "association",
        "target": "app:payment-app-cluster",
        "connection_condition": "attributedTo: 内存压力归因到副本所在容器/集群",
        "description": "内存压力落在承载副本的容器上（集群级现象）",
        "evidence_refs": ["ev-cgroup-memory-events"],
    },
    {
        "id": "rel:rc-mem-pressure-evidenced",
        "source": "rc:mem-pressure",
        "relation_type": "association",
        "target": "metric:mem-pressure",
        "connection_condition": "evidencedBy: 内存用量与限额之比佐证压力",
        "description": "working_set/limit 逼近 1.0 是压力的直接观测",
        "evidence_refs": ["ev-cgroup-memory-events"],
    },
    {
        "id": "rel:rc-oom-kill-evidenced-counter",
        "source": "rc:oom-kill",
        "relation_type": "association",
        "target": "metric:oom-kill-count",
        "connection_condition": "evidencedBy: oom_kill 只增计数器佐证「真的被杀过」",
        "description": "increase(cc_container_oom_kill_total[5m]) > 0 才能断言 OOM Kill 已发生",
        "evidence_refs": ["ev-cgroup-memory-events"],
    },
    {
        # 跨集群归属验证查出来的缺口：15 个根因里 14 个有 attributedTo 边，
        # 只有 rc:db-cpu-starve 没有 —— 于是"这个根因该找谁（应用值班/DBA/平台）"
        # 在本体里答不出来（BFS 能蹭到 db 节点，但那是**顺带**而不是**声明**）。
        # 归属必须是显式声明：它是"该找谁"的唯一依据。
        "id": "rel:rc-db-cpu-starve-attr",
        "source": "rc:db-cpu-starve",
        "relation_type": "association",
        "target": "db:mysql-cluster",
        "connection_condition": "attributedTo: 数据库 CPU 饥饿归因到数据库集群",
        "description": "并发查询把 MySQL 的 CPU 吃满 → 归属数据库集群（DBA）",
        "evidence_refs": ["ev-cluster-fault-catalog"],
    },
    {
        # 同上：rc:row-lock 只声明了**表级**归属（`table:t_txn`，"是什么"），
        # 集群级（"该找谁"）要靠 3 跳才蹭到 db 节点。归属是行动依据，必须显式。
        "id": "rel:rc-row-lock-attr",
        "source": "rc:row-lock",
        "relation_type": "association",
        "target": "db:mysql-cluster",
        "connection_condition": "attributedTo: HTTP 事务被行锁阻塞归因到数据库集群",
        "description": "持锁长事务在 MySQL 里 → 归属数据库集群（DBA）；表级归属见 table:t_txn",
        "evidence_refs": ["ev-cluster-fault-catalog"],
    },
]

#: 新证据：cgroup v2 memory.events（真实存在、本轮第 1 项就是靠它修的告警盲区）
NEW_EVIDENCE: List[Dict[str, Any]] = [
    {
        "id": "ev-cgroup-memory-events",
        "kind": "metric",
        "source": "cgroup v2 /sys/fs/cgroup/memory.events",
        "description": (
            "容器 cgroup v2 的 memory.events：`oom_kill` 为只增计数器，"
            "经 payment-app exporter 暴露为 `cc_container_oom_kill_total`。"
            "它把「内存压力」与「真的被 OOM Kill」在指标层面区分开。"
        ),
        "observed_at": "2026-10-08",
    },
]

#: 处置命令的**可执行性**修正（本轮由"真跑一次处置动作"发现）
#:
#: `rc:row-lock` 的 P0 命令原本查 `sys.innodb_lock_waits`，但诊断用的只读账号
#: （`rca_readonly`，权限只有 PROCESS + REPLICATION CLIENT + creditcard/performance_schema 的 SELECT）
#: **读不到 `sys` 库** → 实测 `ERROR 1142 (42000): SELECT command denied`。
#: 也就是说：这条"最紧急的排查动作"在纸面上完全合理，照着敲却只会得到权限错误。
#: （第 5 轮修的是"指标名不存在"，这次是"库没权限" —— 同一类问题的不同面。）
#:
#: 替代写法（已用只读账号逐个验证）：
#:   information_schema.innodb_trx          → OK
#:   performance_schema.data_locks          → OK
#:   performance_schema.data_lock_waits     → OK（MySQL 8 里 sys.innodb_lock_waits 的底层视图来源）
#:   sys.innodb_lock_waits                  → DENIED
REMEDIATION_FIXES: List[Dict[str, Any]] = [
    {
        "id": "rc:row-lock",
        "old_command": ("SELECT * FROM information_schema.innodb_trx; "
                        "SELECT * FROM sys.innodb_lock_waits"),
        "new_command": ("SELECT * FROM information_schema.innodb_trx; "
                        "SELECT * FROM performance_schema.data_lock_waits"),
    },
]


def _parent_records() -> Dict[str, List[dict]]:
    """读当前激活版本（本轮父版本）的 5 族记录。"""
    act = json.loads((ROOT / ".evoontology" / "active.json").read_text(encoding="utf-8"))
    vdir = ROOT / ".evoontology" / "versions" / act["active_version"]
    out: Dict[str, List[dict]] = {}
    for fam in ("terms", "mappings", "relations", "constraints", "evidence"):
        out[fam] = json.loads((vdir / (fam + ".json")).read_text(encoding="utf-8"))
    return out


def build_patch(base_term_ids: Optional[Set[str]] = None,
                base_evidence_ids: Optional[Set[str]] = None,
                base_mapping_ids: Optional[Set[str]] = None,
                report: Optional[Dict[str, Any]] = None
                ) -> Dict[str, List[Dict[str, Any]]]:
    """
    产出候选 patch（字段级 merge 语义：只写要改的字段）。

    只对**父版本里确实存在**的术语/约束产出修正记录；
    新增记录一律带上完整字段。命中数记进 `report` 供驱动打印与审计。
    """
    base_term_ids = base_term_ids or set()
    base_evidence_ids = base_evidence_ids or set()
    parent = _parent_records()
    p_terms = {t["id"]: t for t in parent["terms"] if isinstance(t, dict) and t.get("id")}
    p_cons = {c["id"]: c for c in parent["constraints"] if isinstance(c, dict) and c.get("id")}
    p_rels = {r["id"] for r in parent["relations"] if isinstance(r, dict) and r.get("id")}
    p_evi = {e["id"] for e in parent["evidence"] if isinstance(e, dict) and e.get("id")}

    terms: List[Dict[str, Any]] = []
    constraints: List[Dict[str, Any]] = []
    relations: List[Dict[str, Any]] = []
    evidence: List[Dict[str, Any]] = []
    audit: Dict[str, Any] = {"new_terms": [], "term_fixes": [], "new_constraints": [],
                             "constraint_fixes": [], "new_relations": [], "new_evidence": [],
                             "skipped": []}

    for t in NEW_TERMS:
        if t["id"] in p_terms:
            audit["skipped"].append("术语已存在: %s" % t["id"])
            continue
        if base_term_ids and t["id"] not in base_term_ids:
            # 演化框架给的是"允许出现的 id 集合"；新增 id 也应在其中（由 _apply_patch 处理）
            pass
        terms.append(t)
        audit["new_terms"].append(t["id"])

    for fx in TERM_FIXES:
        if fx["id"] not in p_terms:
            audit["skipped"].append("术语不存在: %s" % fx["id"])
            continue
        # 只保留"确实变化"的字段（避免产出空 patch）
        changed = {k: v for k, v in fx.items()
                   if k != "id" and p_terms[fx["id"]].get(k) != v}
        if not changed:
            audit["skipped"].append("无需修改: %s" % fx["id"])
            continue
        terms.append(dict({"id": fx["id"]}, **changed))
        audit["term_fixes"].append("%s: %s" % (fx["id"], ",".join(changed)))

    # ── 处置命令修正（逐条替换，不整段重写）────────────────────────────────
    for fx in REMEDIATION_FIXES:
        t = p_terms.get(fx["id"])
        if not t:
            audit["skipped"].append("术语不存在: %s" % fx["id"])
            continue
        rem = [dict(a) for a in (t.get("remediation") or []) if isinstance(a, dict)]
        hit = False
        for a in rem:
            if str(a.get("command") or "") == fx["old_command"]:
                a["command"] = fx["new_command"]
                hit = True
        if not hit:
            audit["skipped"].append("未找到待修命令: %s" % fx["id"])
            continue
        terms.append({"id": fx["id"], "remediation": rem})
        audit["remediation_fixes"] = audit.get("remediation_fixes", [])
        audit["remediation_fixes"].append(
            "%s: sys.innodb_lock_waits → performance_schema.data_lock_waits（只读账号读不到 sys）"
            % fx["id"])

    for c in NEW_CONSTRAINTS:
        if c["id"] in p_cons:
            audit["skipped"].append("约束已存在: %s" % c["id"])
            continue
        constraints.append(c)
        audit["new_constraints"].append(c["id"])

    for fx in CONSTRAINT_FIXES:
        if fx["id"] not in p_cons:
            audit["skipped"].append("约束不存在: %s" % fx["id"])
            continue
        changed = {k: v for k, v in fx.items()
                   if k != "id" and p_cons[fx["id"]].get(k) != v}
        if not changed:
            audit["skipped"].append("无需修改: %s" % fx["id"])
            continue
        constraints.append(dict({"id": fx["id"]}, **changed))
        audit["constraint_fixes"].append(
            "%s: %s（原 %s）" % (fx["id"], ",".join(changed),
                              str(p_cons[fx["id"]].get("trigger_keywords"))[:60]))

    for r in NEW_RELATIONS:
        if r["id"] in p_rels:
            audit["skipped"].append("关系已存在: %s" % r["id"])
            continue
        relations.append(r)
        audit["new_relations"].append(r["id"])

    for e in NEW_EVIDENCE:
        if e["id"] in p_evi:
            audit["skipped"].append("证据已存在: %s" % e["id"])
            continue
        if base_evidence_ids and e["id"] not in base_evidence_ids:
            pass
        evidence.append(e)
        audit["new_evidence"].append(e["id"])

    if report is not None:
        report.update(audit)
    return {"terms": terms, "mappings": [], "relations": relations,
            "constraints": constraints, "evidence": evidence}


# ═══════════════════════════════════════════════════════════════════════════
# 契约校验（本轮闸门的判据）
# ═══════════════════════════════════════════════════════════════════════════

#: 本轮不变量的**条数**（供驱动的分值分母使用）。
#: 刻意做成常量并导出：驱动里原先硬编码 `total = 6`，
#: 一旦这里加了检查项（本轮就加了），分母不同步就会**虚高**得分 —— 那正是本项目反复清理的
#: "看起来通过的检查"。
N_INVARIANTS = 9

#: 归属锚点：能回答"该找谁"的集群/环境术语
CLUSTER_ANCHORS = {
    "app:payment-app-cluster", "app:payment-app", "env:gateway",
    "db:mysql-cluster", "db:primary", "db:mysql-replica",
    "env:container", "env:cluster",
}


def _reachable_within(edges: Dict[str, List[str]], start: str, targets: set,
                      limit: int = 2) -> bool:
    """无向 BFS：start 能否在 limit 跳内到达 targets 中任一节点。"""
    seen = {start}
    frontier = [start]
    for _ in range(limit + 1):
        if any(n in targets for n in frontier):
            return True
        nxt = []
        for n in frontier:
            for m in edges.get(n, []):
                if m not in seen:
                    seen.add(m)
                    nxt.append(m)
        frontier = nxt
        if not frontier:
            break
    return False


def check_matrix(records: Dict[str, List[dict]]) -> Dict[str, List[str]]:
    """
    逐条不变量检查 → {检查编号: 问题列表}。

    为什么要有编号：发布闸门要求**成对且 case_id 唯一对齐的分数**，
    而本轮的判据是"9 条不变量逐条过没过" —— 于是 9 条不变量就是 9 个 case
    （父版本 0/9、候选 9/9）。没有编号就只能把问题堆成一个列表，
    既算不出逐条得分，闸门也会以 "Candidate did not pass the recorded evaluation gate" 拒收。
    """
    terms = {t["id"]: t for t in records.get("terms", []) if isinstance(t, dict) and t.get("id")}
    cons = {c["id"]: c for c in records.get("constraints", [])
            if isinstance(c, dict) and c.get("id")}
    rels = records.get("relations", [])
    out: Dict[str, List[str]] = {("C%d" % i): [] for i in range(1, 10)}

    # C1 新术语在场
    for tid in ("rc:mem-pressure", "metric:oom-kill-count"):
        if tid not in terms:
            out["C1"].append("候选里缺少 %s" % tid)
    # C2 新术语自带证据 + 有出边（否则会被卫生检查判为无证据/游离）
    for tid in ("rc:mem-pressure", "metric:oom-kill-count"):
        t = terms.get(tid)
        if t and not t.get("evidence_refs"):
            out["C2"].append("%s 没有 evidence_refs（会被卫生检查判为无证据）" % tid)
    srcs = {str(r.get("source")) for r in rels}
    if "rc:mem-pressure" not in srcs:
        out["C2"].append("rc:mem-pressure 没有任何出边（会是游离术语）")
    # C3 OOM Kill 的证据词里不能再出现"压力/不足"这类症状词
    oom = cons.get("con:oom-detection") or {}
    kws = [str(k) for k in (oom.get("trigger_keywords") or [])]
    bad = [k for k in kws if ("压力" in k or "内存不足" in k)]
    if bad:
        out["C3"].append("con:oom-detection 仍把症状词当作 OOM 证据：%s" % bad)
    # C4 必须有以新根因为 target 的约束（否则引擎拿不到打分关键词）
    if not any(str(c.get("target")) == "rc:mem-pressure" for c in cons.values()):
        out["C4"].append("没有以 rc:mem-pressure 为 target 的约束 → 引擎无法给它打分关键词")
    # C5 "是否已发生杀死"必须落在指标上
    if not any(str(r.get("source")) == "rc:oom-kill"
               and str(r.get("target")) == "metric:oom-kill-count" for r in rels):
        out["C5"].append("rc:oom-kill 没有连到 metric:oom-kill-count → 无法在指标上判别是否被杀")
    # C6 api:auth 必须已改 draft
    auth = terms.get("api:auth")
    if auth and str((auth.get("lifecycle") or {}).get("state")) != "draft":
        out["C6"].append("api:auth 的 lifecycle.state 仍是 %s（应为 draft）"
                         % (auth.get("lifecycle") or {}).get("state"))
    # C7 处置命令必须能被**诊断用的只读账号**执行
    rl = terms.get("rc:row-lock") or {}
    cmds = [str(a.get("command") or "") for a in (rl.get("remediation") or [])
            if isinstance(a, dict)]
    if any("sys.innodb_lock_waits" in c for c in cmds):
        out["C7"].append("rc:row-lock 的处置命令仍引用 sys.innodb_lock_waits ——"
                         "只读账号 rca_readonly 无 sys 库权限（实测 ERROR 1142）")
    if not any("performance_schema.data_lock_waits" in c for c in cmds):
        out["C7"].append("rc:row-lock 缺少基于 performance_schema.data_lock_waits 的可执行查锁命令")
    # C8 归属完整性：每个根因都必须能回答"该找谁"（≤2 跳到达集群/环境锚点）
    edges: Dict[str, List[str]] = {}
    for r in rels:
        if not isinstance(r, dict):
            continue
        s, t = str(r.get("source") or ""), str(r.get("target") or "")
        if s and t:
            edges.setdefault(s, []).append(t)
            edges.setdefault(t, []).append(s)
    no_attr = [tid for tid in terms if tid.startswith("rc:")
               and not _reachable_within(edges, tid, CLUSTER_ANCHORS, limit=2)]
    if no_attr:
        out["C8"].append("这些根因在 2 跳内到不了任何集群/环境锚点（答不出「该找谁」）：%s" % no_attr)
    # C9 判别量必须落在指标上
    if not any(str(r.get("target")) == "metric:oom-kill-count" for r in rels
               if isinstance(r, dict)):
        out["C9"].append("没有任何关系连到 metric:oom-kill-count → 「是否已被 OOM Kill」无法判别")
    return out


def check_invariants(records: Dict[str, List[dict]]) -> List[str]:
    """
    对本轮**候选**记录做语义不变量检查，返回问题列表（空 = 通过）。

    为什么不用"结构性指标"就够：本轮的要点是**语义不再自相矛盾**，
    纯结构指标（有没有关系、度是否 > 0）测不出"关键词与定义冲突"这类问题。
    """
    return [p for _cid, ps in sorted(check_matrix(records).items()) for p in ps]


def scene_semantics(records: Dict[str, List[dict]]) -> Dict[str, Any]:
    """给驱动/报告用的一页摘要：本轮"说清了什么"。"""
    cons = {c["id"]: c for c in records.get("constraints", [])
            if isinstance(c, dict) and c.get("id")}
    return {
        "new_root_cause": "rc:mem-pressure（压力但未被杀）",
        "discriminator": "increase(cc_container_oom_kill_total[5m]) > 0 → rc:oom-kill；== 0 → rc:mem-pressure",
        "oom_detection_keywords": cons.get("con:oom-detection", {}).get("trigger_keywords"),
        "mem_pressure_keywords": cons.get("con:mem-pressure", {}).get("trigger_keywords"),
    }
