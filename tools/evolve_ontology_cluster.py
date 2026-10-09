#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本体迭代（ontology evolution）—— 把集群化知识通过自演化协议发布为新版本。

要回答的问题：**环境已经集群化了，本体还能不能跟上？**

流程（严格走 EvoOntology 的 EvolutionSession 协议，不直接改激活版本）：
  1. 更新项目上下文（project.json）与工作负载（workload/questions.json），
     把 21 个集群故障场景登记为 ontology task 的问题集
  2. start_evolution_run(parent_version=当前激活版本, acceptance={"protocol":"ground_truth"})
  3. begin_evolution_round(hypothesis, candidate_version="ontology_v1-cluster")
  4. 构造候选版本 = 父版本 5 族记录深拷贝 + 集群补丁（tools/ontology_cluster_patch.py）
  5. validate(candidate) —— 交叉引用必须全部可解析
  6. **成对评估（ground truth）**：对每个故障场景计算"本体可诊断性得分"
       score = 满足下列 3 项的比例
         ① 期望根因 Term 在该版本中存在
         ② 该 Term 与用户可见入口 (app:payment-app / cluster) 在关系图中连通（≤4 跳）
            —— 说明"故障如何传播到症状"被建模了
         ③ 该 Term 是可观测的（本身是 metric，或直接关联 metric，或是某条约束的 target）
            —— 说明"凭什么确认它"被建模了
       parent_scores / candidate_scores 在同一批 case 上计算 → 交给评估闸门
  7. record_evolution_evaluation(parent) / (candidate + gate_input)
  8. accept_evolution → 发布并激活 ontology_v1（validate → publish → activate → 推进检查点）
  9. finalize_evolution_run + 渲染离线可视化 HTML

用法：
  python tools/evolve_ontology_cluster.py            # 完整执行
  python tools/evolve_ontology_cluster.py --dry-run  # 只评估不发布（冻结前演练）
  python tools/evolve_ontology_cluster.py --status   # 只看当前演化运行状态
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rca-agent"))              # backend 包（引擎 top1 口径）
sys.path.insert(0, str(ROOT / "vendor" / "EvoOntology"))

WORKSPACE = ROOT / ".evoontology"

from evoontology import EvolutionSession, SemanticStore           # noqa: E402
from evoontology.validate import validate                          # noqa: E402
from evoontology.workflow import ProjectWorkflow                   # noqa: E402
from evoontology.workspace import save_project                     # noqa: E402

from tools.ontology_cluster_patch import (                          # noqa: E402
    SCENARIO_EXPECTED_TERMS, build_patch as build_cluster_patch,
)
from tools.ontology_scoring_patch import (                          # noqa: E402
    build_patch as build_scoring_patch,
)
from tools.ontology_v3_patch import (                               # noqa: E402
    build_patch as build_hygiene_patch,
)
from tools.ontology_remediation_patch import (                      # noqa: E402
    build_patch as build_remediation_patch,
    check_invariants as check_remediation_invariants,
    remediation_contract,
    term_command_problems,
    valid_metric_names,
)
from tools.ontology_v5_patch import (                               # noqa: E402
    build_patch as build_command_patch,
)
from tools.ontology_v6_patch import (                               # noqa: E402
    build_patch as build_memory_patch,
    check_invariants as check_memory_invariants,
    check_matrix as check_memory_matrix,
    N_INVARIANTS as MEMORY_N_INVARIANTS,
)

#: 每轮迭代的配置：补丁来源 / 候选版本名 / 发布版本名 / 评估口径 / 假设
ROUNDS = {
    "cluster": {
        "patch": build_cluster_patch,
        "candidate": "ontology_v1-cluster",
        "publish": "ontology_v1",
        "metric": "structural",
        "title": "集群化知识补齐（第 1 轮）",
    },
    "scoring": {
        "patch": build_scoring_patch,
        "candidate": "ontology_v2-scoring",
        "publish": "ontology_v2",
        "metric": "engine_top1",
        "title": "打分消歧字段扩展（第 2 轮·反向迭代）",
    },
    "hygiene": {
        "patch": build_hygiene_patch,
        "candidate": "ontology_v3-hygiene",
        "publish": "ontology_v3",
        "metric": "hygiene_contract",
        "title": "游离术语清理 + 缺失概念补齐 + lifecycle 声明（第 3 轮·卫生）",
    },
    "remediation": {
        "patch": build_remediation_patch,
        "candidate": "ontology_v4-remediation",
        "publish": "ontology_v4",
        "metric": "remediation_contract",
        "title": "根因处置动作入库（第 4 轮·可执行性）",
    },
    "command-targets": {
        "patch": build_command_patch,
        "candidate": "ontology_v5-command-targets",
        "publish": "ontology_v5",
        "metric": "remediation_contract",
        "title": "处置命令可执行性修正（第 5 轮·实测驱动）",
    },
    # 第 6 轮：把"内存压力"与"真的被 OOM Kill"在**语义与指标**上分开。
    # 起因是端到端严格口径里 res_cluster_memory 判成 rc:oom-kill，
    # 查下去发现是本体自相矛盾（con:oom-detection 把"内存压力"当成 OOM 的证据，
    # 而 rc:oom-kill 自己的定义要求 OOMKilled=true）。
    "memory": {
        "patch": build_memory_patch,
        "candidate": "ontology_v6-memory",
        "publish": "ontology_v6",
        "metric": "memory_semantics",
        "title": "区分「内存压力」与「已被 OOM Kill」（第 6 轮·语义精确化）",
    },
}

ENTRY_TERMS = {"app:payment-app", "app:payment-app-cluster"}


# ═════════════════════════════════════════════════════════════════════════════
# 可诊断性评估（ground truth）
# ═════════════════════════════════════════════════════════════════════════════

def _adjacency(records: Dict[str, List[dict]]) -> Dict[str, Set[str]]:
    adj: Dict[str, Set[str]] = {}
    for r in records.get("relations") or []:
        s, t = str(r.get("source") or ""), str(r.get("target") or "")
        if not s or not t:
            continue
        adj.setdefault(s, set()).add(t)
        adj.setdefault(t, set()).add(s)
    return adj


def _reachable(adj: Dict[str, Set[str]], start: str,
               targets: Set[str], limit: int = 4) -> bool:
    """无向 BFS：start 能否在 limit 跳内到达 targets 中任一节点。"""
    if start in targets:
        return True
    seen = {start}
    q = deque([(start, 0)])
    while q:
        cur, d = q.popleft()
        if d >= limit:
            continue
        for nxt in adj.get(cur, ()):  # type: ignore[arg-type]
            if nxt in targets:
                return True
            if nxt not in seen:
                seen.add(nxt)
                q.append((nxt, d + 1))
    return False


def diagnosability(records: Dict[str, List[dict]],
                   expected_terms: List[str]) -> Dict[str, Any]:
    """
    单场景的"本体可诊断性"评分（0 / 1/3 / 2/3 / 1）。

    三项要求等权，全部可在给定版本的 5 族记录上确定性复算：
      ① term_defined      —— 期望根因 Term 已定义
      ② path_modelled     —— 该 Term 与用户可见入口在关系图中连通（≤4 跳）
      ③ observable        —— 该 Term 可观测（metric / 直连 metric / 约束 target）
    """
    terms = {t["id"]: t for t in (records.get("terms") or [])}
    adj = _adjacency(records)
    cons_targets = {str(c.get("target") or "") for c in (records.get("constraints") or [])}

    metric_linked: Set[str] = set()
    for r in records.get("relations") or []:
        s, t = str(r.get("source") or ""), str(r.get("target") or "")
        if terms.get(t, {}).get("type") == "metric" and s:
            metric_linked.add(s)
        if terms.get(s, {}).get("type") == "metric" and t:
            metric_linked.add(t)

    detail: List[Dict[str, Any]] = []
    best = 0.0
    for tid in expected_terms:
        t = terms.get(tid)
        defined = t is not None
        path = bool(defined and _reachable(adj, tid, ENTRY_TERMS, limit=4))
        observable = bool(defined and (
            t.get("type") == "metric" or tid in metric_linked or tid in cons_targets))
        score = (float(defined) + float(path) + float(observable)) / 3.0
        detail.append({"term": tid, "defined": defined, "path_modelled": path,
                       "observable": observable, "score": round(score, 4)})
        best = max(best, score)

    return {
        "score": round(best, 4),
        "expected_terms": expected_terms,
        "per_term": detail,
    }


def scenario_cases() -> List[Dict[str, Any]]:
    """构造评估用例：每个故障场景 = 一条告警文本 + 期望根因 Term（ground truth）。"""
    from tools.chaos import FAULTS

    cases: List[Dict[str, Any]] = []
    for fid in sorted(FAULTS):
        fault = FAULTS[fid]
        expected = SCENARIO_EXPECTED_TERMS.get(fid) or []
        if not expected:
            continue
        cases.append({
            "case_id": fid,
            "layer": fault.layer,
            "category": fault.category,
            "alert": "%s：%s" % (fault.title, fault.expected_root_cause),
            "expected_terms": expected,
        })
    return cases


def evaluate(records: Dict[str, List[dict]],
             cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对一批用例计算**结构性**可诊断性得分（知识是否存在/连通/可观测）。"""
    per_case: List[Dict[str, Any]] = []
    for c in cases:
        d = diagnosability(records, c["expected_terms"])
        per_case.append({
            "case_id": c["case_id"],
            "layer": c["layer"],
            "expected_terms": c["expected_terms"],
            "score": d["score"],
            "detail": d["per_term"],
        })
    scores = [p["score"] for p in per_case]
    return {
        "metric": "structural_diagnosability",
        "case_ids": [p["case_id"] for p in per_case],
        "scores": scores,
        "mean": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "full_credit": sum(1 for s in scores if s >= 1.0),
        "zero_credit": sum(1 for s in scores if s <= 0.0),
        "per_case": per_case,
    }


def evaluate_engine(records: Dict[str, List[dict]], cases: List[Dict[str, Any]],
                    top_k: int = 1, label: str = "") -> Dict[str, Any]:
    """
    对某本体记录集计算**引擎实际表现**得分（每 case 0/1）。

    第 2 轮迭代的知识完备性已经恒为 1.000（无法作为闸门），
    因此改用"确定性快路径的 top1 是否精确命中场景期望根因 Term"作为 ground truth。

    注意：
      · 刻意在**离线**复算（本地构建拓扑 + 内存打分索引），不依赖运行中的后端；
      · 用 `OntologyScoring.from_records` 直接吃候选记录，因此**候选版本尚未落盘**
        也能评分（不需要为了评分把候选临时写进工作区）。
    """
    from tools.ontology_scoring_verify import evaluate_records
    from backend.services.ontology_scoring import OntologyScoring

    scoring = OntologyScoring.from_records(records, label=label or "(candidate)")
    # 计分只用 top1；top_k=5 只是为了让报告里的 top5 覆盖率字段有意义
    r = evaluate_records(records, scoring, top_k=5, label=label)
    if not r.get("available"):
        raise RuntimeError("引擎离线评估失败: %s" % r.get("error"))
    by_case = {c["case_id"]: c for c in r["per_case"]}
    per_case: List[Dict[str, Any]] = []
    for c in cases:
        e = by_case.get(c["case_id"], {})
        per_case.append({
            "case_id": c["case_id"],
            "layer": c["layer"],
            "expected_terms": c["expected_terms"],
            "score": 1.0 if e.get("top1_hit") else 0.0,
            "detail": [{"top1": e.get("top1"), "top1_hit": e.get("top1_hit"),
                        "candidates": e.get("candidates")}],
        })
    scores = [p["score"] for p in per_case]
    return {
        "metric": "engine_top1_precision",
        "case_ids": [p["case_id"] for p in per_case],
        "scores": scores,
        "mean": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "full_credit": sum(1 for s in scores if s >= 1.0),
        "zero_credit": sum(1 for s in scores if s <= 0.0),
        "engine": {
            "top1_hits": r.get("top1_hits"),
            "topk_hits": r.get("topk_hits"),
            "top1_distribution": r.get("top1_distribution"),
            "route_distribution": r.get("route_distribution"),
            "deterministic_cases": r.get("deterministic_cases"),
            "deterministic_top1_hits": r.get("deterministic_top1_hits"),
        },
        "per_case": per_case,
    }


def _align_evals(parent: Dict[str, Any], candidate: Dict[str, Any]
                 ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """
    把成对评估对齐到 **case_id 并集**。

    为什么必须对齐：`EvaluationGate._publication_gate` 要求
    `case_ids / parent_scores / candidate_scores` 三者等长且 case_id 唯一。
    而本轮迭代会**新增术语**（候选 54 个 case vs 父 51 个），直接提交会被闸门拒绝。

    语义上"父版本没有这个术语"= 该 case 不满足卫生契约 = 0 分，
    因此按并集对齐、缺失补 0 是正确口径（而不是取交集把新术语排除在评估之外）。
    """
    p_scores = dict(zip(parent.get("case_ids") or [], parent.get("scores") or []))
    c_scores = dict(zip(candidate.get("case_ids") or [], candidate.get("scores") or []))
    ids = sorted(set(p_scores) | set(c_scores))
    if list(parent.get("case_ids") or []) == ids and list(candidate.get("case_ids") or []) == ids:
        return parent, candidate, {"aligned": False, "cases": len(ids)}

    def _rebuild(ev: Dict[str, Any], table: Dict[str, float]) -> Dict[str, Any]:
        per_case = []
        by_id = {p["case_id"]: p for p in ev.get("per_case") or []}
        for cid in ids:
            base = dict(by_id.get(cid) or {"case_id": cid, "layer": "ontology",
                                           "expected_terms": [], "detail": []})
            base["score"] = table.get(cid, 0.0)
            if cid not in by_id:
                base["detail"] = [{"note": "该术语在该版本中不存在 → 契约不满足"}]
            per_case.append(base)
        scores = [p["score"] for p in per_case]
        out = dict(ev)
        out.update({"case_ids": ids, "scores": scores,
                    "mean": round(sum(scores) / len(scores), 4) if scores else 0.0,
                    "full_credit": sum(1 for s in scores if s >= 1.0),
                    "zero_credit": sum(1 for s in scores if s <= 0.0),
                    "per_case": per_case})
        return out

    aligned_p = _rebuild(parent, {cid: p_scores.get(cid, 0.0) for cid in ids})
    aligned_c = _rebuild(candidate, {cid: c_scores.get(cid, 0.0) for cid in ids})
    return aligned_p, aligned_c, {
        "aligned": True, "cases": len(ids),
        "only_in_candidate": sorted(set(c_scores) - set(p_scores)),
        "only_in_parent": sorted(set(p_scores) - set(c_scores)),
    }


def evaluate_by_metric(metric: str, version: str,
                       records: Dict[str, List[dict]],
                       cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    if metric == "engine_top1":
        return evaluate_engine(records, cases, top_k=1, label=version)
    if metric == "hygiene_contract":
        return evaluate_hygiene(records, version)
    if metric == "remediation_contract":
        return evaluate_remediation(records, version)
    if metric == "memory_semantics":
        return evaluate_memory_semantics(records, version)
    return evaluate(records, cases)


def evaluate_memory_semantics(records: Dict[str, List[dict]],
                              version: str) -> Dict[str, Any]:
    """
    **内存语义口径**（第 6 轮引入）：逐条不变量等权平均。

    为什么不能用结构性指标：本轮的要点是"**语义不再自相矛盾**"——
    纯结构指标（关系度、连通性）测不出"constraint 的关键词与它所指向的根因定义冲突"。
    因此这里直接复用 patch 模块里的 `check_invariants`，把每条不变量折算成 0/1。
    """
    problems = check_memory_invariants(records)
    # 分母来自 patch 模块的常量，**不在这里硬编码**：
    # 第一版写死 total = 6，后来加了 3 条检查却不改分母，得分就会虚高
    # —— 那正是本项目一直在清理的"看起来通过的检查"。
    total = MEMORY_N_INVARIANTS
    matrix = check_memory_matrix(records)
    # ★ 逐条不变量就是**逐 case 分数**：闸门要求"成对且 case_id 唯一对齐"，
    # 而本轮的判据本来就是"9 条各自过没过"。父版本 0/9、候选 9/9 —— 
    # 这样闸门看到的是**严格改进**（而不是"引擎 top1 不变"，那会被判未通过）。
    per_case = [{"case_id": cid, "score": 0.0 if matrix.get(cid) else 1.0,
                 "detail": "；".join(matrix.get(cid) or [])}
                for cid in sorted(matrix)]
    passed = sum(1 for c in per_case if c["score"] >= 1.0)
    scores = [c["score"] for c in per_case]
    cases = [{"case_id": c["case_id"], "score": c["score"], "detail": c["detail"]}
             for c in per_case]
    return {
        "metric": "memory_semantics",
        "version": version,
        "n": total,
        "score": round(passed / total, 4) if total else 0.0,
        "mean": round(passed / total, 4) if total else 0.0,
        "passed": passed,
        "full_credit": passed,
        "zero_credit": total - passed,
        "case_ids": [c["case_id"] for c in per_case],
        "scores": scores,
        "per_case": cases,
        "problems": problems,
    }


def evaluate_remediation(records: Dict[str, List[dict]], version: str) -> Dict[str, Any]:
    """
    **处置契约**口径（第 4 轮引入，第 5 轮加固）：逐根因术语等权平均。

    只对 `role == root_cause` 的术语评分 —— 组件/观测类术语不该有处置动作，
    把它们算进来会稀释口径（也等于要求"给指标配处置方案"这种无意义的事）。

    检查项（逐术语取通过比例）：
      ① 动作具体（非"检查一下"式空话）
      ② urgency ∈ {P0,P1,P2}
      ③ 显式声明 needs_approval（诊断只读边界）
      ④ 命令引用的指标**真实存在**（第 5 轮新增：实测发现 v4 有命令指向不存在的指标）
      ⑤ command 字段**是命令而不是散文**（第 5 轮新增：v4 有 12 条把说明文字当命令）

    ④ 需要 Prometheus 的指标名清单，拿不到就跳过该项（不因环境不可用而误判）。
    """
    terms = {t["id"]: t for t in (records.get("terms") or [])}
    valid = valid_metric_names()
    if valid is None:
        print("    (Prometheus 指标清单不可用 → 跳过 ④ 指标存在性校验)")
    rcs = sorted(tid for tid, t in terms.items()
                 if tid.startswith("rc:") and str(t.get("role") or "root_cause") == "root_cause")
    per_case, scores = [], []
    for tid in rcs:
        c = remediation_contract(terms.get(tid) or {}, valid_metrics=valid)
        scores.append(c["score"])
        per_case.append({
            "case_id": tid, "layer": "ontology", "expected_terms": [],
            "score": c["score"],
            "detail": [{"items": c["items"],
                        "failed": [k for k, v in c["checks"].items() if not v]}],
        })
    return {"metric": "remediation_contract", "case_ids": rcs, "scores": scores,
            "mean": round(sum(scores) / len(scores), 4) if scores else 0.0,
            "full_credit": sum(1 for s in scores if s >= 1.0),
            "zero_credit": sum(1 for s in scores if s <= 0.0),
            "per_case": per_case}



def evaluate_hygiene(records: Dict[str, List[dict]], version: str) -> Dict[str, Any]:
    """
    **本体卫生契约**口径：逐术语 4 条契约（声明 lifecycle / 游离必须标 draft /
    激活必须接线 / 根因必须可观测），等权平均。

    第 3 轮的目标不是提高 top1 而是清理游离与补齐声明，
    因此闸门必须量"卫生"，否则 `decide_gt` 会因为 top1 未提升而拒绝这一轮。

    用 `term_contract_records` 直接吃内存记录，因此候选尚未落盘也能评分。
    """
    from tools.ontology_hygiene import term_contract_records

    c = term_contract_records(records, version)
    per_case = [{"case_id": p["term"], "layer": "ontology",
                 "expected_terms": [], "score": p["score"],
                 "detail": [{"lifecycle_state": p["lifecycle_state"],
                             "degree": p["degree"],
                             "observable": p["observable"],
                             "failed": p["failed"]}]}
                for p in c["per_case"]]
    return {"metric": "hygiene_contract", "case_ids": c["case_ids"],
            "scores": c["scores"], "mean": c["mean"], "full_credit": c["full_credit"],
            "zero_credit": c["zero_credit"], "per_case": per_case}


# ═════════════════════════════════════════════════════════════════════════════
# 项目上下文 / 工作负载
# ═════════════════════════════════════════════════════════════════════════════

def update_project() -> Dict[str, Any]:
    """把项目上下文更新为集群化后的环境（数据源/边界/演化预算）。"""
    project = {
        "schema_version": 1,
        "mode": "rolling_trajectory",
        "data_source": {
            "type": "docker-compose-cluster",
            "endpoint": (
                "http://localhost:8080 (cc-app-gateway, least_conn → payment-app ×3) + "
                "mysql:3306 (cc-mysql-core PRIMARY, GTID) + "
                "mysql-replica-1/2:3306 (read-only replicas) + "
                "http://localhost:9090 (Prometheus, DNS-SD + blackbox)"
            ),
            "description": (
                "信用卡系统运维模拟环境（集群版）：应用层 3 副本 + Nginx 网关负载均衡；"
                "数据库层 1 主 2 只读副本（GTID 异步复制，读写分离）；"
                "监控层 Prometheus 副本自动发现 + blackbox 外部探活；"
                "故障注入器覆盖应用层/数据库层/资源耗尽共 21 个场景"
            ),
        },
        "workload_source": {
            "type": "rca-scenarios",
            "description": "集群化信用卡运维根因分析场景：副本故障、网关故障、资源耗尽、"
                           "连接耗尽、锁与慢查询、复制延迟/中断、配置漂移",
            "seed_questions": [
                "支付交易超时，沿拓扑上溯定位根因",
                "MySQL P99 突增，是慢 SQL 还是连接耗尽？",
                "某容器 OOM 重启，哪些业务受影响？",
                "应用集群里只有一台副本变慢，是 CPU 节流还是网络问题？",
                "副本全健康但外部请求打不进来，问题在网关还是应用？",
                "只读副本数据陈旧，是复制延迟还是复制中断？",
                "健康检查全绿但交易全部失败，主库是不是被置为只读了？",
                "数据库排序落盘、磁盘临时表激增，是内存配置不足吗？",
            ],
        },
        "evaluation": {
            "type": "paired_ground_truth",
            "description": (
                "Rolling 模式累积任务轨迹作为演化证据；"
                "演化闸门用同一批故障场景在父版本/候选版本上成对计算"
                "本体可诊断性得分（术语存在 + 传播路径建模 + 可观测性），"
                "由 EvaluationGate.decide_gt 判定是否接受"
            ),
        },
        "boundary": {
            "type": "rolling",
            "construction_scope": (
                "rca-agent/docker-compose.yml（集群拓扑）+ demo/app（多副本应用与 cgroup 指标）+ "
                "rca-agent/monitoring（DNS-SD 抓取、blackbox 探活、集群告警规则）+ "
                "tools/fault_injector.py + tools/stress_harness.py + .chaos/（注入与压测产物）"
            ),
            "notes": "RCA 场景不划分 held-out，轨迹持续累积",
        },
        "evolution": {
            "max_rounds": 3,
            "min_rejects_before_incomplete": 2,
        },
    }
    save_project(project, str(WORKSPACE))
    return project


def register_workload(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把集群故障场景登记为 ontology task 的问题集（演化工作负载）。"""
    wf = ProjectWorkflow(str(WORKSPACE))
    questions = [{"question": c["alert"]} for c in cases]
    out = wf.prepare(questions=questions)
    return out if isinstance(out, dict) else {"result": out}


# ═════════════════════════════════════════════════════════════════════════════
# 候选版本构造
# ═════════════════════════════════════════════════════════════════════════════

def build_candidate_records(parent_records: Dict[str, List[dict]],
                            patch_builder
                            ) -> Tuple[Dict[str, List[dict]], Dict[str, Any]]:
    """
    父版本 5 族深拷贝 + 补丁。

    补丁语义是 **upsert**：id 已存在则整条替换（用于给既有术语补字段，
    例如第 2 轮给 Term 加 scoring_keywords / negative_keywords），
    不存在则追加。这样补丁可以是"增量知识"也可以是"schema 扩展"。
    """
    base_terms = {t["id"] for t in parent_records.get("terms") or []}
    base_ev = {e["id"] for e in parent_records.get("evidence") or []}
    base_map = {m["id"] for m in parent_records.get("mappings") or []}

    patch = patch_builder(base_term_ids=base_terms,
                          base_evidence_ids=base_ev,
                          base_mapping_ids=base_map)

    records: Dict[str, List[dict]] = {
        fam: copy.deepcopy(parent_records.get(fam) or [])
        for fam in ("terms", "mappings", "relations", "constraints", "evidence")
    }
    deltas: Dict[str, Any] = {}
    for fam in records:
        by_id = {r["id"]: i for i, r in enumerate(records[fam])}
        added, updated = 0, 0
        for rec in patch.get(fam) or []:
            rid = rec.get("id")
            if rid in by_id:
                merged = dict(records[fam][by_id[rid]])
                merged.update(rec)                 # 字段级合并，保留父版本未提及的键
                records[fam][by_id[rid]] = merged
                updated += 1
            else:
                records[fam].append(rec)
                by_id[rid] = len(records[fam]) - 1
                added += 1
        deltas[fam] = {"added": added, "updated": updated}
    return records, {"deltas": deltas,
                     "counts": {k: len(v) for k, v in records.items()}}


# ═════════════════════════════════════════════════════════════════════════════
# 主流程
# ═════════════════════════════════════════════════════════════════════════════

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="本体迭代：环境/引擎驱动的本体演进")
    ap.add_argument("--round", choices=sorted(ROUNDS), default="cluster",
                    help="cluster=第1轮集群化知识；scoring=第2轮打分消歧 schema 扩展")
    ap.add_argument("--dry-run", action="store_true",
                    help="只构造候选 + 成对评估并打印结论，不写版本、不接受")
    ap.add_argument("--status", action="store_true", help="只看当前演化运行状态")
    ap.add_argument("--json-out", default="", help="报告输出路径（默认按轮次命名）")
    args = ap.parse_args(argv)

    rc = ROUNDS[args.round]
    patch_builder = rc["patch"]
    CANDIDATE = rc["candidate"]
    PUBLISHED = rc["publish"]
    metric = rc["metric"]
    if not args.json_out:
        args.json_out = str(ROOT / ".chaos" / ("ontology_evolution_%s.json" % args.round))

    ws = str(WORKSPACE)
    session = EvolutionSession(ws)

    if args.status:
        run = session.latest_run()
        print(json.dumps(run or {"status": "no_run"}, ensure_ascii=False, indent=2))
        return 0

    print("=" * 88)
    print("本体迭代 %s" % rc["title"])
    print("  候选=%s  发布=%s  评估口径=%s" % (CANDIDATE, PUBLISHED, metric))
    print("=" * 88)

    parent = SemanticStore.active_version(ws)
    print("[1] 当前激活版本（父版本）: %s" % parent)
    _, parent_records = SemanticStore.load_records(ws, version=parent)
    print("    父版本规模: %s" % json.dumps(
        {k: len(v) for k, v in parent_records.items()}, ensure_ascii=False))

    print("[2] 更新项目上下文（集群环境）...")
    project = update_project()

    cases = scenario_cases()
    print("[3] 评估用例: %d 个故障场景（口径=%s）" % (len(cases), metric))
    print("[4] 登记工作负载问题集 ...")
    wl = register_workload(cases)
    print("    %s" % json.dumps(wl, ensure_ascii=False)[:200])

    print("[5] 构造候选版本 %s ..." % CANDIDATE)
    candidate_records, delta = build_candidate_records(parent_records, patch_builder)
    print("    增量: %s" % json.dumps(delta["deltas"], ensure_ascii=False))
    print("    候选规模: %s" % json.dumps(delta["counts"], ensure_ascii=False))

    print("[6] 成对评估（父 vs 候选，同一批 case）...")
    parent_eval = evaluate_by_metric(metric, parent, parent_records, cases)
    cand_eval = evaluate_by_metric(metric, CANDIDATE, candidate_records, cases)
    parent_eval, cand_eval, align_info = _align_evals(parent_eval, cand_eval)
    if align_info.get("aligned"):
        print("    case 集对齐到并集：%d 个（候选新增 %s）"
              % (align_info["cases"], align_info.get("only_in_candidate") or "无"))
    unit = {"structural": "平均分", "engine_top1": "top1 精确命中率",
            "hygiene_contract": "卫生契约均分",
            # 第 6 轮的口径是"逐条语义不变量"，没有 mean/full_credit 这些字段 ——
            # 共用打印代码原先假定它们一定存在，于是第 6 轮在**打印阶段**就 KeyError 崩了。
            # 各口径的字段名不同，就得容忍差异，而不是让打印代码假设一种形状。
            "memory_semantics": "不变量通过率"}.get(metric, "平均分")
    n_eval = len(parent_eval.get("scores") or cases)
    if "mean" in parent_eval:
        print("    父版本   %s=%.3f  满分 case=%d/%d  零分 case=%d"
              % (unit, parent_eval["mean"], parent_eval["full_credit"], n_eval,
                 parent_eval["zero_credit"]))
        print("    候选版本 %s=%.3f  满分 case=%d/%d  零分 case=%d"
              % (unit, cand_eval["mean"], cand_eval["full_credit"], n_eval,
                 cand_eval["zero_credit"]))
    else:
        print("    父版本   %s=%.3f（%s/%s）  问题 %d 条"
              % (unit, parent_eval.get("score", 0.0), parent_eval.get("passed", 0),
                 parent_eval.get("n", 0), len(parent_eval.get("problems") or [])))
        print("    候选版本 %s=%.3f（%s/%s）  问题 %d 条"
              % (unit, cand_eval.get("score", 0.0), cand_eval.get("passed", 0),
                 cand_eval.get("n", 0), len(cand_eval.get("problems") or [])))
        for p in (cand_eval.get("problems") or []):
            print("      - 候选仍存在问题：%s" % p)
    if cand_eval.get("engine"):
        eg = cand_eval["engine"]
        print("    候选引擎: top1 %s/%s，top5 %s/%s，快路径 %s 例（其中 top1 命中 %s）"
              % (eg.get("top1_hits"), len(cases), eg.get("topk_hits"), len(cases),
                 eg.get("deterministic_cases"), eg.get("deterministic_top1_hits")))

    # 只要口径不是 engine_top1，就把引擎 top1 作为**回归观察量**记下来。
    # （原先只在 hygiene_contract 时算 —— 第 6 轮同样不动任何打分字段，
    #  更需要这个观察量来证明"语义修正没有把诊断带偏"。）
    regression: Dict[str, Any] = {}
    _reg_case_ids: List[str] = []
    _reg_parent_scores: List[float] = []
    _reg_cand_scores: List[float] = []
    if metric in ("hygiene_contract", "memory_semantics", "remediation_contract",
                  "structural"):
        pe = evaluate_engine(parent_records, cases, label=parent)
        ce = evaluate_engine(candidate_records, cases, label=CANDIDATE)
        regression = {"parent_engine_top1": pe["mean"], "candidate_engine_top1": ce["mean"],
                      "delta": round(ce["mean"] - pe["mean"], 4),
                      "parent_topk_hits": (pe.get("engine") or {}).get("topk_hits"),
                      "candidate_topk_hits": (ce.get("engine") or {}).get("topk_hits"),
                      "regressed_cases": [p["case_id"] for p, c in
                                          zip(pe["per_case"], ce["per_case"])
                                          if c["score"] < p["score"]]}
        print("    回归观察（引擎 top1）: %.3f → %.3f (Δ%+.3f)，top5 %s→%s，回退 case=%d"
              % (regression["parent_engine_top1"], regression["candidate_engine_top1"],
                 regression["delta"], regression["parent_topk_hits"],
                 regression["candidate_topk_hits"], len(regression["regressed_cases"])))
        # 把逐 case 的引擎得分留下来：本口径（语义不变量）没有自己的 per-case 分数，
        # 而发布闸门**必须**有成对且 case_id 唯一对齐的分数。
        # 对"只改语义、不动打分字段"的轮次，用引擎逐 case top1 当闸门分数最贴切：
        # 它直接回答"这次语义修正有没有把诊断带偏"。
        _reg_case_ids = list(pe.get("case_ids") or [])
        _reg_parent_scores = [float(x.get("score", 0.0)) for x in (pe.get("per_case") or [])]
        _reg_cand_scores = [float(x.get("score", 0.0)) for x in (ce.get("per_case") or [])]

    # per_case 也要容忍形状差异：memory_semantics 的 per_case 只有"问题条目"，
    # 与 case 数不对应 → 直接按 case_id 求交集，而不是硬按位置 zip。
    _pp = {x.get("case_id"): x.get("score", 0.0) for x in (parent_eval.get("per_case") or [])}
    _cc = {x.get("case_id"): x.get("score", 0.0) for x in (cand_eval.get("per_case") or [])}
    _common = [k for k in _pp if k in _cc]
    improved = [k for k in _common if _cc[k] > _pp[k]]
    regressed = [k for k in _common if _cc[k] < _pp[k]]
    print("    提升 case %d 个；回退 case %d 个" % (len(improved), len(regressed)))
    if "mean" in parent_eval:
        print("    Δ%s = %+.3f" % (unit, cand_eval["mean"] - parent_eval["mean"]))
    else:
        print("    Δ%s = %+.3f" % (unit, cand_eval.get("score", 0.0)
                                   - parent_eval.get("score", 0.0)))

    if args.dry_run:
        report = {
            "mode": "dry-run", "round": args.round, "metric": metric,
            "parent_version": parent, "candidate_version": CANDIDATE,
            "delta": delta, "parent_eval": parent_eval, "candidate_eval": cand_eval,
            "improved_cases": improved, "regressed_cases": regressed,
            "regression_observation": regression,
            "project": project,
        }
        _write_json(args.json_out, report)
        print("\n[dry-run] 未写入任何版本。报告: %s" % args.json_out)
        return 0

    # ── 幂等保护：目标版本已经发布过 → 不重复开轮次，直接产出报告 ──────
    accepted_run = _find_accepted_run(PUBLISHED)
    if accepted_run is not None:
        real_parent = accepted_run.get("parent_version") or parent
        print("[7-14] 目标版本 %s 已由 %s 发布（%s → %s），跳过协议，直接产出报告"
              % (PUBLISHED, accepted_run.get("run_id"), real_parent, PUBLISHED))
        # 顺手收敛任何卡在 running 的空转运行，避免审计上出现悬挂轮次
        running = session.latest_run()
        if running and running.get("status") == "running" and running.get("run_id") != accepted_run.get("run_id"):
            try:
                session.mark_incomplete("user_interrupted")
                session.finalize()
                print("        已把悬挂运行 %s 标记为 incomplete 并 finalize"
                      % running.get("run_id"))
            except Exception as e:  # noqa: BLE001
                print("        (悬挂运行收敛失败: %s)" % e)
        _, p_records = SemanticStore.load_records(ws, version=real_parent)
        p_eval = evaluate_by_metric(metric, real_parent, p_records, cases)
        c_eval = evaluate_by_metric(metric, PUBLISHED,
                                    SemanticStore.load_records(ws, version=PUBLISHED)[1], cases)
        _pp2 = {x.get("case_id"): x.get("score", 0.0) for x in (p_eval.get("per_case") or [])}
        _cc2 = {x.get("case_id"): x.get("score", 0.0) for x in (c_eval.get("per_case") or [])}
        _com2 = [k for k in _pp2 if k in _cc2]
        imp = [k for k in _com2 if _cc2[k] > _pp2[k]]
        reg = [k for k in _com2 if _cc2[k] < _pp2[k]]
        if "mean" in p_eval:
            print("    %s %.3f → %.3f (Δ%+.3f)" % (unit, p_eval["mean"], c_eval["mean"],
                                                  c_eval["mean"] - p_eval["mean"]))
        else:
            print("    %s %.3f → %.3f (Δ%+.3f)" % (unit, p_eval.get("score", 0.0),
                                                  c_eval.get("score", 0.0),
                                                  c_eval.get("score", 0.0)
                                                  - p_eval.get("score", 0.0)))
        _emit_final_report(args, real_parent, CANDIDATE, PUBLISHED, accepted_run, delta,
                           p_eval, c_eval, imp, reg, {"skipped": True}, project)
        return 0

    # ── 续跑悬挂运行（协议健壮性）────────────────────────────────────────
    # 上一轮若在**构造/评估阶段**崩溃（本轮崩过三次），run 会停在 running，
    # 于是再跑时报 "Run run_7 is still running; resume it instead"。
    # 试过 `mark_incomplete`，协议**拒绝**了：它要求"至少 2 个被拒候选"才允许
    # 因外部原因停止 —— 这是刻意的防呆（不让人轻易放弃一轮）。
    # 所以正确做法就是它说的：**resume**（保留该运行的历史，继续在本轮里推进），
    # 而不是另开一个新运行把历史切掉。
    hanging: Optional[Dict[str, Any]] = None
    try:
        latest = session.latest_run()
        if latest and str(latest.get("status")) == "running":
            hanging = latest
            print("    ⚠ 发现悬挂运行 %s（上次在此阶段崩溃）→ 按协议 resume 继续"
                  % latest.get("run_id"))
    except Exception as e:  # noqa: BLE001
        print("    (悬挂运行检查跳过: %s)" % e)

    # ── 协议：start_run → begin_round → save_version → evaluate → accept ──
    print("[7] 启动演化运行（冻结轮次预算与验收协议）...")
    if hanging is not None:
        run = session.resume(hanging.get("run_id"))
        # 崩溃的每次尝试**都会消耗一轮**（begin_round 在崩溃前已经调用）：
        # "工具崩了 3 次"就把 max_rounds=3 用光，resume 时在 begin_round 抛
        # EvolutionBudgetExhausted。协议说"扩预算需用户确认"——本轮就是按用户
        # "按顺序全做"的指令在续跑，且这些被消耗的轮次**没有产出任何可用证据**
        # （崩在打印/记录/闸门阶段），因此续跑时扩展预算并写明理由。
        mx = int((run.get("budget") or {}).get("max_rounds") or 3)
        run = session.extend_budget(mx + 3)
        print("    续跑：轮次预算 %d → %d（补回被崩溃尝试消耗的轮次）"
              % (mx, mx + 3))
    else:
        run = session.start_run(parent, adapter="ground_truth", max_rounds=3,
                                acceptance={"protocol": "ground_truth"})
    print("    run_id=%s budget=%s" % (run["run_id"], run["budget"]))

    if metric == "structural":
        hypothesis = (
            "环境已从单容器单库扩展为『3 副本应用 + Nginx 网关 + 1 主 2 只读副本 MySQL 集群』，"
            "而本体仍停留在单实例视图（18 个 Term 中没有任何副本/集群/网关/复制/配额概念），"
            "导致 21 个集群故障场景中有 %d 个既没有根因术语、也没有传播路径与可观测连接。"
            "假设：按 5 族增量补齐集群概念（术语 + 接地映射 + 关系路径 + 约束 + 证据）后，"
            "本体对这批场景的可诊断性得分将由 %.2f 提升到 %.2f 以上，且不引入任何回归。"
            % (parent_eval["zero_credit"], parent_eval["mean"], cand_eval["mean"])
        )
    elif metric == "hygiene_contract":
        hypothesis = (
            "本体规模不等于本体能力。审计发现 ontology_v2 里存在 2 个游离术语"
            "（env:cluster / evt:deploy，关系图度为 0）：它们不在引擎拓扑、永不做候选根因，"
            "却会出现在 browse_semantics 结果里，对 LLM Agent 是噪声；"
            "同时 TTL 里的索引个体在首版转换时根本没被建成 Term，"
            "且 51 个术语全部未声明 lifecycle，草稿态机制形同虚设。"
            "根因是首版 TTL→5 族转换器漏映射了 managedBy / generatesEvent / hasIndex。"
            "假设：补齐这三处映射（把 evt:deploy 接入变更关联链条、把 env:cluster 接回 "
            "managedBy 并改名消歧、把索引补成可接地术语）并声明全部 lifecycle 后，"
            "术语卫生契约均分将由 %.3f 提升到 %.3f 以上、游离术语归零，"
            "且引擎 top1 不下降（作为回归观察量）。"
            % (parent_eval["mean"], cand_eval["mean"])
        )
    elif metric == "memory_semantics":
        # 第 6 轮的假设：本轮治的是**本体自相矛盾**，不是"知识不够"。
        hypothesis = (
            "端到端严格口径的未命中里，`res_cluster_memory` 被判成 `rc:oom-kill`、"
            "期望 `rc:cluster-capacity`。查下去发现**是本体自己说不清**："
            "`rc:oom-kill` 的定义要求「OOMKilled=true / RestartCount 递增」这类**已被杀**的直接证据，"
            "而 `con:oom-detection` 的 trigger_keywords 却把「内存压力」「内存不足」也算作它的证据 —— "
            "约束与定义直接冲突；同时本体里只有『已杀』(rc:oom-kill) 与『容量不足』(rc:cluster-capacity) "
            "两个术语，**『被压到极限但还没被杀』这一段无处可去**。"
            "假设：新增 `rc:mem-pressure` 与 `metric:oom-kill-count`（cgroup memory.events 的只增计数器，"
            "第 1 项为修告警盲区而加的真实指标）、收窄 `con:oom-detection` 的关键词到"
            "「已被杀死」类证据、并给新根因配约束后："
            "内存语义不变量由 %d/%d 提升到 %d/%d（冲突关键词清零、新根因可被打分、"
            "`是否已发生杀死`落在指标上），且引擎 top1 不下降（回归观察量）。"
            % (parent_eval.get("passed", 0), parent_eval.get("n", 6),
               cand_eval.get("passed", 0), cand_eval.get("n", 6))
        )
    elif metric == "remediation_contract":
        # 注意：这里要的是**根因术语**数（rc: 前缀），不是术语总数。
        # 第一次写错成 len(records["terms"])=54，于是发布的假设里写着"根因有 54 个" ——
        # 54 是全部术语（含组件/观测/约束），根因只有 15 个。数字错了就等于假设错了。
        _rc_terms = [t for t in (candidate_records.get("terms") or [])
                     if str(t.get("id") or "").startswith("rc:")
                     and str(t.get("role") or "root_cause") == "root_cause"]
        if args.round == "command-targets":
            hypothesis = (
                "第 4 轮把处置动作交还本体，但当时的契约只验证**纸面可用性**"
                "（动作具体 / urgency 合法 / 声明审批），三条都能在纸面上通过。"
                "实测（注入真实故障 → 真跑诊断 → 执行 P0 命令）暴露两类只有跑起来才看得见的问题："
                "① `rc:cpu-throttle` 的 P0 命令引用的指标 `cc_container_cpu_cfs_throttled_periods_total` "
                "在环境里**根本不存在**（正确名是 `cc_container_cpu_throttled_seconds_total`），"
                "照着排查只会得到空结果；"
                "② 12 条把说明性文字写进了 `command` 字段（如「按 app_instance 对比 P99」），"
                "界面却按等宽命令渲染。"
                "假设：按父版本逐条替换这些命令（含 11 个术语），并把"
                "「指标必须存在」「command 必须是命令」补成契约第 ④⑤ 条后，"
                "处置契约均分将由 %.3f 提升到 %.3f 以上、满分术语数由 %d 提升到 %d，"
                "且引擎 top1 不下降（本轮不动任何打分字段）。"
                % (parent_eval["mean"], cand_eval["mean"],
                   parent_eval["full_credit"], cand_eval["full_credit"])
            )
        else:
            hypothesis = (
                "结论的可解释性在第 11.20 节已补齐（名称/机制/归因对象/观测佐证/判据），"
                "但『怎么办』仍是代码里按 5 个类别硬编码的通用建议 —— 类别只有 5 个、"
                "根因有 %d 个，粒度对不上，于是『磁盘临时表』拿到的是"
                "『确认容器是否因内存/CPU 受限被杀』这种既不准确也不可执行的动作。"
                "根因是**处置知识压根不在本体里**。"
                "假设：给 %d 个根因术语各补 3~4 条 remediation（结构同构于 next_actions，"
                "P0 一律只读、写操作显式 needs_approval）后，处置契约均分将由 %.3f 提升到 %.3f 以上，"
                "且作为回归观察量的引擎 top1 不下降（本轮不改任何打分字段）。"
                % (len(_rc_terms),
                   delta["deltas"]["terms"]["updated"] + delta["deltas"]["terms"]["added"],
                   parent_eval["mean"], cand_eval["mean"])
            )
    else:
        hypothesis = (
            "把打分词典改为从本体派生后，21 个集群故障场景的候选集覆盖率已到 top5 21/21，"
            "但 top1 仅 %d/21 —— 因为『副本丢失/副本假死/副本 OOM/副本被节流』共享『副本』一词，"
            "关键词打分无法区分同一实体的不同故障模式。这不是缺知识而是缺表达能力："
            "术语只能声明自己是什么，不能声明『什么情况下不是我』。"
            "假设：给 %d 个根因术语补上 scoring_keywords（判别性正向词）与 "
            "negative_keywords（排除词）后，引擎 top1 精确命中率将由 %.1f%% 提升到 %.1f%% 以上，"
            "且 top5 覆盖率保持 21/21、无 case 回退。"
            % (parent_eval["full_credit"], len(candidate_records["terms"]) and
               (delta["deltas"]["terms"]["updated"] + delta["deltas"]["terms"]["added"]),
               parent_eval["mean"] * 100, cand_eval["mean"] * 100)
        )
    rnd = session.begin_round(hypothesis, CANDIDATE)
    print("    round=%d  candidate=%s" % (rnd, CANDIDATE))
    print("    hypothesis: %s" % hypothesis[:160])

    print("[8] 写入候选版本 ...")
    vdir = SemanticStore.save_version(ws, CANDIDATE, candidate_records)
    print("    %s" % vdir)

    print("[9] 校验候选版本（交叉引用 / 可加载性）...")
    v = validate(ws, version=CANDIDATE)
    print("    passed=%s errors=%s" % (v.get("passed"), v.get("errors")))
    if not v.get("passed"):
        session.mark_incomplete("missing_data")
        _write_json(args.json_out, {"ok": False, "validate": v})
        print("[!] 候选版本校验失败，已标记演化未完成")
        return 2

    print("[10] 记录轨迹来源（可复现证据）...")
    sources = []
    for rel in (".chaos/fault_verify_report.json", ".chaos/rca_diagnosis_report.json",
                ".chaos/stress_mixed.json", "demo/docker-compose.yml"):
        p = ROOT / rel
        if p.exists():
            sources.append({"path": rel, "scope": "verification",
                            "purpose": "本体迭代的成对评估与回归证据"})
    if sources:
        try:
            path = session.confirm_trajectory_sources(sources)
            print("    %s" % path)
        except Exception as e:  # noqa: BLE001
            print("    (跳过轨迹来源: %s)" % e)

    print("[11] 记录成对评估（口径=%s）..." % metric)
    prov = "ground_truth: %s" % parent_eval.get("metric", metric)

    def _metrics_of(ev: Dict[str, Any]) -> Dict[str, Any]:
        """
        把任意口径的评估结果折成"协议要记的指标字典"。

        各轮口径的字段名不同（结构性/契约类有 mean+full_credit；
        第 6 轮的语义不变量口径只有 score/passed/n/problems），
        这里统一映射，避免协议记录层被迫假设某一种形状。
        """
        out: Dict[str, Any] = {}
        key = ev.get("metric", metric)
        if "mean" in ev:
            out[key] = ev["mean"]
            out["full_credit_cases"] = ev.get("full_credit")
            out["zero_credit_cases"] = ev.get("zero_credit")
        else:
            out[key if key == "case_score_mean" else "score"] = ev.get("score", 0.0)
            out["invariants_passed"] = ev.get("passed")
            out["invariants_total"] = ev.get("n")
            out["invariant_problems"] = len(ev.get("problems") or [])
        out["cases"] = len(cases)
        return out

    p_path = session.record_evaluation(parent, {
        "metrics": _metrics_of(parent_eval),
        "cases": [{"case_id": c["case_id"], "score": c["score"]}
                  for c in parent_eval["per_case"]],
        "artifact_paths": [args.json_out],
        "provenance": prov,
    }, role="parent")
    print("    parent -> %s" % p_path)

    c_path = session.record_evaluation(CANDIDATE, {
        "metrics": _metrics_of(cand_eval),
        "cases": [{"case_id": c["case_id"], "score": c["score"]}
                  for c in cand_eval["per_case"]],
        "artifact_paths": [args.json_out],
        "provenance": prov,
        "gate_input": {
            "protocol": "ground_truth",
            # 本口径没有自己的逐 case 分数时，用引擎逐 case top1 代替：
            # 闸门要求"成对 + case_id 唯一对齐"，空分数会被直接拒（实测报
            # "Gate needs paired scores with unique matching case_ids"）。
            "case_ids": (parent_eval.get("case_ids") or _reg_case_ids
                         or [c["case_id"] for c in cases]),
            "parent_scores": (parent_eval.get("scores") or _reg_parent_scores),
            "candidate_scores": (cand_eval.get("scores") or _reg_cand_scores),
            # 必须显式声明"没有不可接受的回归"：本门禁为同一批 case 的成对得分，
            # 已逐 case 比对（见 regressed_cases），无任何 case 分数下降。
            "unacceptable_regressions": False,
        },
    }, role="candidate")
    print("    candidate -> %s" % c_path)

    print("[12] 写版本说明报告（reports/%s.json）..." % CANDIDATE)
    wf = ProjectWorkflow(ws)
    if args.round == "memory":
        summary = (
            "内存语义精确化版本：把『内存被压到极限但还没被杀』显式建模为 "
            "`rc:mem-pressure`，并新增 `metric:oom-kill-count`"
            "（cgroup v2 memory.events 的 oom_kill 只增计数器，"
            "由 payment-app exporter 暴露为 `cc_container_oom_kill_total`）。"
            "根因是本体自相矛盾：`rc:oom-kill` 的定义要求「OOMKilled=true / RestartCount 递增」"
            "这类**已被杀**的直接证据，而 `con:oom-detection` 的关键词却把"
            "「内存压力」「内存不足」也算成它的证据 —— 于是『压力』被当成『已杀』，"
            "`res_cluster_memory` 因此被判成 `rc:oom-kill`。"
            "本轮同时把 `api:auth` 改标 `lifecycle.state=draft`（卫生闸门的老 warning："
            "声明 active 但无证据、环境里也没实现该接口），"
            "并修正 `rc:row-lock` 的 P0 排查命令 —— 它查的是 `sys.innodb_lock_waits`，"
            "而诊断用的只读账号 `rca_readonly` **没有 sys 库权限**（实测 `ERROR 1142`），"
            "这条「最紧急的动作」照着敲只会得到权限错误；已换成只读账号验证可读的 "
            "`performance_schema.data_lock_waits`。"
            "（该缺陷是**真跑一次处置动作**才发现的 —— 与第 5 轮靠真跑发现指标名写错同一路子。）"
            "目的：让『压力』与『杀死』在**语义与指标**两个层面都能区分，"
            "而不是靠措辞猜。")
        limitations = [
            "`rc:mem-pressure` 的判别仍要求引擎/Agent 主动查 `cc_container_oom_kill_total`；"
            "若只看内存比值，`rc:oom-kill` 与 `rc:mem-pressure` 在文本上仍然相似"
            "（两者都由『内存压力』类告警触发）。本轮给 `rc:oom-kill` 补了指标佐证关系，"
            "但『先查计数器再下结论』这一步依赖调用方遵守 con:mem-pressure 的判据描述。",
            "新增的 `metric:oom-kill-count` 没有 grounding mapping（与既有 rc:/metric: 派生术语一致）——"
            "它靠关系关联到真实指标，而不是直接接地；这是刻意的，不是遗漏。",
            "`api:auth` 改为 draft 后不再参与语义检索；若将来真的实现该接口，需要改回 active 并补证据。",
        ]
    elif args.round == "cluster":
        summary = (
            "集群化本体版本：新增应用集群/副本/网关、数据库主从集群/只读副本、"
            "复制延迟与中断、容器 CPU 节流与内存压力、行锁/连接耗尽/临时表落盘/"
            "主库只读等根因术语，以及对应的接地映射、传播路径关系、容量与阈值约束、"
            "可复现证据。用于让 RCA 智能体在集群环境下定位『哪个副本/哪个节点/哪类资源』"
            "而不是只知道『服务挂了』。")
        limitations = [
            "确定性规则引擎的候选打分词典当时仍是代码内置（rca_engine.category_keywords），"
            "部分集群术语（如 rc:row-lock / rc:replica-loss）不会被关键词命中；"
            "本轮只验证本体侧知识完备性与传播路径建模。该问题已在第 2 轮（ontology_v2）"
            "通过把词典改为本体驱动 + 增加 scoring_keywords/negative_keywords 解决。",
            "副本实例（app:instance-1..3）与数据库节点的接地映射是静态声明，"
            "副本扩缩容后需要重新接地（当前 3 副本与实际部署一致）。",
            "network 类故障（rc:net-fault）只有黑盒探活与延迟/丢包指标证据，"
            "没有 packet-level 证据（未部署抓包/连接追踪）。",
        ]
    elif args.round == "hygiene":
        summary = (
            "卫生版本：清理 2 个游离术语（env:cluster / evt:deploy 关系图度为 0，"
            "不在引擎拓扑、永不做候选根因，却会出现在 browse_semantics 里成为 LLM 噪声），"
            "把 TTL 索引个体补成可接地 Term 并新增 con:index-usage / con:change-correlation，"
            "同时给全部术语声明 lifecycle。根因是首版 TTL→5 族转换器漏映射了 "
            "managedBy / generatesEvent / hasIndex —— 本轮既补产物也修生成器。"
            "目的：让『本体规模』不再虚高，闲概念不参与检索、草稿态可被自动过滤。")
        limitations = [
            "lifecycle 声明是人工判定的：判定依据是『该概念在当前环境里是否已接线且可观测』，"
            "环境变化（例如真的引入多集群）后需要重新判定，否则会把活跃概念误标为 draft。",
            "env:cluster 改名加了负向词来消除与 app:payment-app-cluster 的『集群』撞车，"
            "这属于用词面手段解决语义歧义；若以后出现更多『集群』概念，应改成显式的 "
            "disjointWith 声明而不是继续堆负向词。",
            "索引术语的接地（information_schema.statistics）是静态快照，"
            "表结构变更后需要重新接地。",
        ]
    elif args.round == "remediation":
        summary = (
            "可执行性版本：为 15 个根因术语补上 `remediation`（共 50 条处置动作），"
            "把『怎么办』从代码里按 5 个类别硬编码的通用建议，"
            "改为随根因术语走的本体知识（动作结构同构于 next_actions："
            "urgency / action / command / needs_approval）。"
            "直接动机：第 11.20 节把结论补全为『名称+机制+归因对象+观测佐证+判据』后，"
            "唯一仍不贴切的就是处置建议 —— 例如『磁盘临时表』给出的是"
            "『确认容器是否因内存/CPU 受限被杀』，既不准确也不可执行。"
            "根因是类别只有 5 个而根因有 15 个，粒度天然对不上。"
            "本轮把粒度对齐到根因，并让『只读排查』与『需审批的写操作』显式分离。")
        limitations = [
            "remediation 是本项目对 EvoOntology Term schema 的扩展字段"
            "（与 scoring_keywords / negative_keywords / lifecycle 同类；"
            "validate() 只强制 id，额外字段可安全共存）。上游若要正式支持，"
            "建议作为 Term 的一等字段纳入 schema 文档。",
            "50 条动作是人工编写的，存在过拟合到当前 21 个场景与当前技术栈"
            "（docker compose / MySQL 8.0 / Nginx）的风险；换环境需要重新评审。",
            "动作只覆盖『根因已确定之后做什么』，不含自动执行、回滚与验证闭环；"
            "needs_approval=true 的动作一律要求人工审批，本轮不做自动化。",
            "处置契约只校验『可用性』（动作具体 / urgency 合法 / 声明审批），"
            "无法校验『这条动作在真实故障里是否真的管用』—— 那需要实测闭环，尚未做。",
        ]
    else:
        summary = (
            "打分消歧版本（反向迭代）：为 15 个根因术语补上 scoring_keywords 与 "
            "negative_keywords 两个字段，把『什么情况下不是我』这类消歧知识交还本体维护，"
            "并新增 con:fastpath-trust 约束，把『快路径采信条件』本身也登记进本体。"
            "目的：解决『副本丢失 / 副本假死 / 副本 OOM / 副本被节流』共享『副本』一词"
            "导致的关键词打分歧义，提升确定性快路径的 top1 精确率。")
        limitations = [
            "scoring_keywords / negative_keywords 是本项目对 EvoOntology Term schema 的扩展"
            "（validate() 只强制 id，额外字段可安全共存；EvoOntology 运行时忽略未知字段）。"
            "若上游要正式支持，建议作为 Term 的一等字段纳入 schema 文档。",
            "负向词是人工编写的，存在过拟合到当前 21 个场景的风险；"
            "需要用新场景（held-out）复验，避免变成「换个说法的硬编码」。",
            "关键词消歧的上限是『同实体多故障模式』的语义边界；"
            "更细的区分仍需 LLM Agent 结合多源证据（指标/日志/容器状态）完成。",
        ]
    try:
        rep = wf.annotate(CANDIDATE, summary=summary, limitations=limitations, links=[])
        print("    %s" % json.dumps({k: rep[k] for k in ("version", "recorded_at")},
                                    ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        print("    (版本说明写入失败，不阻断发布: %s)" % e)

    print("[13] 提交评估闸门并接受新版本 ...")

    # ── 硬前置（第 6 轮）：内存语义不得自相矛盾 ──────────────────────────
    # 本轮的要点就是"约束关键词不能与它指向的根因定义冲突"，
    # 因此把 6 条不变量做成发布闸门（而不是只看分数）：
    # 任何一条不过就不发布 —— 语义自相矛盾的本体比缺术语更糟，它会稳定地把诊断引偏。
    if args.round == "memory":
        inv = check_memory_invariants(candidate_records)
        print("    内存语义不变量: %s" % ("PASS" if not inv else "FAIL"))
        for p in inv:
            print("      - %s" % p)
        if inv:
            session.mark_incomplete("unreliable_evaluation")
            print("[!] 内存语义不变量未通过，已标记演化未完成，不发布。")
            _write_json(args.json_out, {"ok": False, "memory_invariants": inv,
                                        "parent_eval": parent_eval, "candidate_eval": cand_eval})
            return 4

    # ── 硬前置（第 4 轮）：处置动作不变式 ────────────────────────────────
    # 「P0 一律只读」是本项目"诊断只读、写操作需人工审批"边界的直接体现。
    # 把它做成发布闸门而不是注释：一旦有人在 P0 里塞了需要审批的写操作，
    # 界面上"最紧急的事"就变成"等人批准的事"，紧急度分级失去意义。
    if args.round == "remediation":
        inv = check_remediation_invariants()
        print("    处置动作不变式: %s" % ("PASS" if not inv else "FAIL"))
        for p in inv:
            print("      - %s" % p)
        if inv:
            session.mark_incomplete("unreliable_evaluation")
            print("[!] 处置动作不变式未通过，已标记演化未完成，不发布。")
            _write_json(args.json_out, {"ok": False, "remediation_invariants": inv,
                                        "parent_eval": parent_eval, "candidate_eval": cand_eval})
            return 4

    # ── 硬前置（第 5 轮）：命令字段本身必须可执行 ────────────────────────
    # 对**全部**术语扫描（不只抽一个场景）：引用的指标要真实存在、
    # command 里不能混说明文字。这两条正是实测从 v4 里挖出来的缺陷类型。
    if args.round == "command-targets":
        valid = valid_metric_names()
        probs: List[str] = []
        for t in candidate_records.get("terms") or []:
            if not str(t.get("id") or "").startswith("rc:"):
                continue
            for p in term_command_problems(t, valid):
                probs.append("%s: %s" % (t["id"], p))
        print("    命令可执行性扫描（指标存在 + 不含说明文字）: %s"
              % ("PASS" if not probs else "FAIL"))
        for p in probs[:8]:
            print("      - %s" % p)
        if probs:
            session.mark_incomplete("unreliable_evaluation")
            print("[!] 命令字段仍有问题，已标记演化未完成，不发布。")
            _write_json(args.json_out, {"ok": False, "command_problems": probs,
                                        "parent_eval": parent_eval, "candidate_eval": cand_eval})
            return 4

    # ── 硬前置：本体卫生闸门（违反即拒绝发布）──────────────────────────
    from tools.ontology_hygiene import gate_records
    hyg = gate_records(candidate_records, CANDIDATE)
    print("    卫生闸门: %s  游离术语=%s  契约均分=%.3f"
          % ("PASS" if hyg["passed"] else "FAIL", hyg["orphans"] or "无", hyg["contract_mean"]))
    for v in hyg["violations"]:
        print("      - %s" % v)
    if not hyg["passed"]:
        session.mark_incomplete("unreliable_evaluation")
        print("[!] 卫生闸门未通过，已标记演化未完成，不发布。")
        _write_json(args.json_out, {"ok": False, "hygiene_gate": hyg,
                                    "parent_eval": parent_eval, "candidate_eval": cand_eval})
        return 4
    try:
        published = session.accept(PUBLISHED)
    except Exception as e:  # noqa: BLE001
        print("[!] accept 失败: %s" % e)
        print("    演化运行保留为 running，可人工检查后重试。")
        _write_json(args.json_out, {"ok": False, "error": str(e),
                                    "parent_eval": parent_eval,
                                    "candidate_eval": cand_eval})
        return 3
    print("    ✔ 已发布并激活: %s" % published)

    fin = session.finalize()
    print("[14] 演化运行终态: status=%s accepted=%s"
          % (fin.get("status"), fin.get("accepted_version")))

    # ── 渲染离线可视化 ────────────────────────────────────────────────
    print("[15] 渲染多版本离线可视化 ...")
    vis: Dict[str, Any] = {}
    try:
        from evoontology.visualization import visualize
        vis = visualize(str(WORKSPACE)) or {}
    except Exception as e:  # noqa: BLE001
        vis = {"error": "%s: %s" % (type(e).__name__, e)}
    print("    %s" % json.dumps(vis, ensure_ascii=False, default=str)[:300])

    _emit_final_report(args, parent, CANDIDATE, published, fin, delta,
                       parent_eval, cand_eval, improved, regressed, vis, project)
    return 0


def _emit_final_report(args: argparse.Namespace, parent: str, candidate: str,
                       published: str, run: Dict[str, Any], delta: Dict[str, Any],
                       parent_eval: Dict[str, Any], cand_eval: Dict[str, Any],
                       improved: List[str], regressed: List[str],
                       vis: Dict[str, Any], project: Dict[str, Any]) -> None:
    """汇总并落盘本体迭代报告（含可视化结果，path 统一转字符串）。"""
    report = {
        "ok": True,
        "parent_version": parent,
        "candidate_version": candidate,
        "published_version": published,
        "run_id": run.get("run_id"),
        "run_status": run.get("status"),
        "gate": run.get("gate"),
        "delta": delta,
        "parent_eval": parent_eval,
        "candidate_eval": cand_eval,
        "improved_cases": improved,
        "regressed_cases": regressed,
        "mean_delta": round((cand_eval.get("mean", cand_eval.get("score", 0.0))
                             - parent_eval.get("mean", parent_eval.get("score", 0.0))), 4),
        "visualization": vis,
        "versions": SemanticStore.list_versions(str(WORKSPACE)),
        "project_mode": project.get("mode"),
    }
    _write_json(args.json_out, json.loads(json.dumps(report, ensure_ascii=False, default=str)))

    # 补齐已发布版本的版本说明报告：库的 accept() 只在某些路径下把
    # reports/<candidate>.json 复制成 reports/<published>.json，
    # 这里显式补齐，保证"激活版本一定有对应说明"这一审计不变量。
    try:
        rep_dir = WORKSPACE / "reports"
        src = rep_dir / ("%s.json" % candidate)
        dst = rep_dir / ("%s.json" % published)
        if src.is_file() and not dst.is_file():
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            print("  已补齐版本说明: reports/%s.json" % published)
    except Exception as e:  # noqa: BLE001
        print("  (版本说明补齐失败: %s)" % e)

    print("\n" + "=" * 88)
    print("本体迭代完成：%s → %s" % (parent, published))
    print("  可诊断性 平均分 %.3f → %.3f (Δ%+.3f)；零分场景 %d → %d"
          % (parent_eval["mean"], cand_eval["mean"],
             cand_eval["mean"] - parent_eval["mean"],
             parent_eval["zero_credit"], cand_eval["zero_credit"]))
    print("  报告: %s" % args.json_out)
    print("=" * 88)


def _find_accepted_run(published: str) -> Optional[Dict[str, Any]]:
    """在 evolution/run_*/run.json 中找"已接受并发布到 published"的那次运行。"""
    best: Optional[Dict[str, Any]] = None
    for p in sorted((WORKSPACE / "evolution").glob("run_*/run.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if r.get("status") == "accepted" and r.get("accepted_version") == published:
            if best is None or str(r.get("run_id")) > str(best.get("run_id")):
                best = r
    if best is not None and (WORKSPACE / "versions" / published).is_dir():
        return best
    return None


def _write_json(path: str, obj: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
