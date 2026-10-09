# -*- coding: utf-8 -*-
"""
双路径路由（Q2 决策 A：本体优先 + Agent 增强）。

判定逻辑：
  1. 始终先跑本体确定性引擎（成本极低，毫秒级）
  2. 若规则引擎的结论**被本体约束支撑** 且置信度 ≥ 阈值 → 走快路径（确定性）
  3. 否则启动 LLM Agent，把确定性结论作为先验注入

## 第二次改造：白名单/门槛不再硬编码，改由本体驱动

第一次集群化本体迭代后暴露了一个缺陷：`KNOWN_ROOT_CAUSES` 是硬编码白名单
（含 `rc:slow-sql`），只要规则引擎对某个已知根因打了 0.75 以上，
`--mode auto` 就永远走快路径 —— 实测 4/4 集群故障全部返回同一个过期根因，
LLM Agent 从未获得机会。

现在：
  · `KNOWN_ROOT_CAUSES` 从激活本体派生（`rc:` 前缀的 entity 术语）
  · 新增 **约束支撑度** 信号：用告警文本匹配本体 `Constraint.trigger_keywords`，
    要求规则引擎给出的根因实体与"命中的约束的 target"在本体关系图上距离 ≤2 跳，
    或类别一致。
  · 未被约束支撑（且存在指向别处的约束命中）时 **强制走 Agent 复核**，
    并在 `reason` 里写清"哪条约束命中了、指向哪个 target"。

快路径仍有价值：
  · 已知故障模式无需 LLM，省成本、毫秒级、零幻觉
  · 可作为 Agent 的对照组（交叉验证）
  · LLM 不可用时是唯一的降级路径
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from .llm_config import llm_config
from .ontology_scoring import OntologyScoring


@dataclass
class RouteDecision:
    mode: str                        # deterministic | agentic
    reason: str
    threshold: float = 0.0
    seed_confidence: float = 0.0
    matched_constraint: bool = False
    known_root_cause: bool = False
    signals: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "threshold": self.threshold,
            "seed_confidence": self.seed_confidence,
            "matched_constraint": self.matched_constraint,
            "known_root_cause": self.known_root_cause,
            "signals": self.signals,
        }


#: 兜底白名单：本体不可读时沿用旧行为，保证不退化
FALLBACK_KNOWN_ROOT_CAUSES = {
    "rc:slow-sql",
    "incident:payment-timeout",
}

#: 命中"已知根因"且被约束支撑时放宽的门槛
#  （结构支撑强，不需要 LLM 再探索）
KNOWN_RC_RELAXED_THRESHOLD = 0.75


def ontology_known_root_causes(scoring: OntologyScoring) -> Set[str]:
    """已知根因 = 本体里 `rc:` 前缀的 entity 术语（由本体给出，不再硬编码）。"""
    if scoring.available and scoring.known_root_causes:
        return set(scoring.known_root_causes)
    return set(FALLBACK_KNOWN_ROOT_CAUSES)


def decide(seed: Optional[dict], cfg: Optional[dict] = None,
           scoring: Optional[OntologyScoring] = None) -> RouteDecision:
    """
    根据确定性引擎结果决定走哪条路径。

    Args:
        seed:    RCAEngine.infer() 的输出（可为 None，表示引擎失败）
        cfg:     LLM 配置（含 fast_path_confidence 阈值）
        scoring: 本体打分索引（默认取进程内缓存实例）
    """
    c = cfg or llm_config()
    threshold = float(c.get("fast_path_confidence", 0.85))
    sc = scoring or OntologyScoring.get()

    if not seed:
        return RouteDecision(
            mode="agentic",
            reason="确定性引擎无输出，必须依赖 LLM Agent",
            threshold=threshold,
            signals={"ontology_available": sc.available,
                     "ontology_version": sc.version or None},
        )

    rc = seed.get("root_cause") or {}
    category = str(rc.get("category") or "")
    entity_id = str(rc.get("entity_id") or "")
    conf = float(seed.get("confidence") or 0.0)
    message = str((seed.get("alert") or {}).get("message") or "")

    # ── 信号采集 ──────────────────────────────────────────────────
    matched_categories = (seed.get("alert") or {}).get("matched_categories") or []
    thresholds = seed.get("thresholds_triggered") or []
    triggered = [t for t in thresholds if t.get("status") == "TRIGGERED"]
    candidates = seed.get("candidates") or []
    cats = {c_.get("category") for c_ in candidates if c_.get("category")}
    consensus = len(cats) <= 1
    unknown_category = category in ("", "未知")

    known_rc_set = ontology_known_root_causes(sc)
    known_rc = entity_id in known_rc_set

    # ── 本体约束支撑度（新增核心信号）──────────────────────────────
    constraint_hits: list = []
    seed_supported = False
    category_aligned = False
    if sc.available and message:
        try:
            constraint_hits = sc.match_constraints(message)
            seed_supported = sc.supported_by_constraint(message, entity_id, max_hops=2)
            hit_cats = {h["target_category"] for h in constraint_hits if h.get("target_category")}
            category_aligned = bool(category and category in hit_cats)
        except Exception:  # noqa: BLE001
            constraint_hits = []

    matched_constraint = bool(triggered) or known_rc or seed_supported

    signals = {
        "category": category,
        "entity_id": entity_id,
        "matched_categories": matched_categories,
        "candidates": len(candidates),
        "category_consensus": consensus,
        "thresholds_triggered": len(triggered),
        "known_root_cause": known_rc,
        "known_root_causes_source": "ontology" if (sc.available and sc.known_root_causes)
                                    else "fallback",
        "constraint_hits": [{k: h.get(k) for k in
                             ("constraint_id", "target", "target_category", "matched_keywords")}
                            for h in constraint_hits[:4]],
        "seed_supported_by_constraint": seed_supported,
        "category_aligned_with_constraint": category_aligned,
        "ontology_available": sc.available,
        "ontology_version": sc.version or None,
    }

    # ── 判定 ──────────────────────────────────────────────────────
    if unknown_category:
        return RouteDecision(
            mode="agentic",
            reason="确定性引擎未能归类（类别为空/未知），需 LLM 探索",
            threshold=threshold, seed_confidence=conf,
            matched_constraint=matched_constraint,
            known_root_cause=known_rc, signals=signals,
        )

    # ★ 新增：存在本体约束命中，但规则引擎的结论不被这些约束支撑 → 强制 Agent 复核
    #   这是修掉"快路径过度自信"的关键分支：约束是人工确认过的语义接线，
    #   它的指向比规则引擎的自评分更可信。
    if constraint_hits and not (seed_supported or category_aligned):
        targets = ", ".join(sorted({h["target"] for h in constraint_hits[:3] if h.get("target")}))
        return RouteDecision(
            mode="agentic",
            reason=("告警命中本体约束 %s（指向 %s），但规则引擎给出的根因 %s[%s] "
                    "既不在其 2 跳邻域内、类别也不一致 → 快路径结论不可信，交 Agent 复核"
                    % (", ".join(h["constraint_id"] for h in constraint_hits[:3]),
                       targets or "?", entity_id or "?", category)),
            threshold=threshold, seed_confidence=conf,
            matched_constraint=False, known_root_cause=known_rc, signals=signals,
        )

    # 命中本体已显式建模的根因实体 **且被约束支撑** → 放宽门槛
    if known_rc and seed_supported:
        relaxed = min(threshold, KNOWN_RC_RELAXED_THRESHOLD)
        if conf >= relaxed:
            return RouteDecision(
                mode="deterministic",
                reason=("命中本体已建模根因 %s 且被约束 %s 支撑，置信度 %.2f ≥ 放宽门槛 %.2f，"
                        "直接采信确定性结论"
                        % (entity_id,
                           ",".join(h["constraint_id"] for h in constraint_hits[:2]) or "-",
                           conf, relaxed)),
                threshold=relaxed, seed_confidence=conf,
                matched_constraint=True, known_root_cause=True, signals=signals,
            )

    if conf >= threshold and matched_constraint:
        return RouteDecision(
            mode="deterministic",
            reason="置信度 %.2f ≥ 阈值 %.2f 且命中已知约束，直接返回确定性结论"
                   % (conf, threshold),
            threshold=threshold, seed_confidence=conf,
            matched_constraint=True, known_root_cause=known_rc, signals=signals,
        )

    if conf >= threshold and consensus:
        return RouteDecision(
            mode="deterministic",
            reason="置信度 %.2f ≥ 阈值 %.2f 且候选类别一致，采信确定性结论"
                   % (conf, threshold),
            threshold=threshold, seed_confidence=conf,
            matched_constraint=matched_constraint,
            known_root_cause=known_rc, signals=signals,
        )

    reasons = []
    if conf < threshold:
        reasons.append("置信度 %.2f < 阈值 %.2f" % (conf, threshold))
    if not matched_constraint:
        reasons.append("未命中已知约束/根因")
    if not consensus:
        reasons.append("候选根因类别分歧（%s）" % ", ".join(sorted(cats)))

    return RouteDecision(
        mode="agentic",
        reason="需 LLM 深入分析：" + "；".join(reasons),
        threshold=threshold, seed_confidence=conf,
        matched_constraint=matched_constraint,
        known_root_cause=known_rc, signals=signals,
    )
