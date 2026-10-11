# -*- coding: utf-8 -*-
"""
指标平面判别器：**"内存压力"还是"真的被 OOM Kill"**。

## 为什么需要它（不是"再调一版词典"）

`ontology_v6` 已经把这个区分建进本体：`rc:oom-kill` 的定义要求"已被杀"的直接证据
（`OOMKilled=true` / `increase(cc_container_oom_kill_total[5m]) > 0`），并新增了
`rc:mem-pressure` 表示"被压到极限但还没被杀"，同时新增了 `metric:oom-kill-count`
（cgroup v2 `memory.events.oom_kill` 的只增计数器）与关系
`rel:rc-oom-kill-evidenced-counter`。

但**确定性快路径只看告警文本**，从不读这个指标。第 7 轮的候选轮次证明了后果：
在文本平面怎么调词典都分不开这两者 ——
把症状词从 `rc:oom-kill` 迁到 `rc:mem-pressure`，会让 `app_oom_kill`（**真被杀**）
改判成 `rc:mem-pressure`，三套固定输入里两套回退
（[`reports/round7_candidate_rejected.json`](../../../reports/round7_candidate_rejected.json)）。
原因：真实 kill 告警里"**内存超限**被内核杀死"与"cgroup **oom_kill 计数增加**"**同时出现** ——
症状词不是噪声。**所以判别量只能落在指标平面。**

## 规则（按顺序，第一条命中即定，全部写进返回值供审计）

  0. **不介入**：候选里没有同时出现 `rc:oom-kill` 与 `rc:mem-pressure`，或当前 top1 不是这两者之一。
  1. **文本硬证据**：告警文本里有"已被杀"的直接表述
     （`OOMKilled` / `oom_kill 计数增加` / `已被内核杀死` / `increase(cc_container_oom_kill_total`）
     → 判 `rc:oom-kill`。**不查指标**：这是已被杀的事实陈述。
  2. **指标裁决**：否则若指标可用 → `increase(cc_container_oom_kill_total[5m])`
     · > 0 → `rc:oom-kill`（真的被杀过）
     · == 0 → `rc:mem-pressure`（只有压力、没被杀）
  3. **指标不可用**：不介入，保持文本顺序，并标记 `metric_unavailable`
     —— 宁可回到"文本说什么就是什么"，也不猜。

为什么规则 1 优先于指标：计数器是**只增且跨场景粘性**的（手册 §11.21 2e 已记录：
`OOMKilled`/`RestartCount` 跨场景残留不自动清零），5 分钟窗口可能把上一场故障的杀进算进来。
文本里"已被杀"的陈述与指标冲突时，以陈述为准更稳；而"有风险/可能被"这类**假设性**表述
不含硬证据，正好落到规则 2 —— 那才是这个判别器要治的场景。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

OOM_KILL = "rc:oom-kill"
MEM_PRESSURE = "rc:mem-pressure"
OOM_COUNTER = "cc_container_oom_kill_total"
WINDOW = "5m"

#: 文本里"已被杀"的硬证据（小写匹配）
HARD_KILL_EVIDENCE: tuple[str, ...] = (
    "oomkilled",
    "oom_kill 计数增加",
    "oom_kill计数增加",
    "increase(cc_container_oom_kill_total",
    "已被内核杀死",
)

#: 假设性/症状措辞（只用于解释"为什么落到指标裁决"，不作为判据）
HYPOTHETICAL_HINTS: tuple[str, ...] = ("风险", "可能被", "逼近", "前兆", "随时")


def query_oom_metric(query_fn: Callable[[str], Dict[str, Any]],
                     window: str = WINDOW) -> Dict[str, Any]:
    """
    取 `increase(cc_container_oom_kill_total[<window>])` 的最大值与明细，同时取当前计数值。

    指标不可用（Prometheus 挂/无序列）时返回 `available=False`，而不是抛异常 ——
    判别器不介入即可，**不能因为查不到指标就把推理打挂**。
    """
    expr = "max(increase(%s[%s]))" % (OOM_COUNTER, window)
    raw_expr = "max(%s)" % OOM_COUNTER
    out: Dict[str, Any] = {"expr": expr, "window": window, "available": False}
    try:
        data = query_fn(expr)
    except Exception as e:  # noqa: BLE001
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out

    series: List[Dict[str, Any]] = []
    for r in data.get("result") or []:
        metric = r.get("metric") or {}
        try:
            val = float(r["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        series.append({
            "instance": metric.get("app_instance") or metric.get("instance") or "",
            "increase": val,
        })
    out["available"] = True
    out["series"] = series
    out["max_increase"] = max((s["increase"] for s in series), default=None)
    out["kill_observed"] = bool(out["max_increase"] and out["max_increase"] > 0)

    try:  # 当前计数值：只作审计上下文，不参与判定
        raw = query_fn(raw_expr)
        out["counter_now"] = max(
            (float(r["value"][1]) for r in (raw.get("result") or [])
             if r.get("value")), default=None)
    except Exception:  # noqa: BLE001
        out["counter_now"] = None
    return out


def decide(text: str, candidate_ids: Sequence[str], metric: Dict[str, Any]) -> Dict[str, Any]:
    """
    规则本体（纯函数，便于离线自测）：返回决策 + 依据，**不修改候选**。
    """
    low = (text or "").lower()
    hard = [k for k in HARD_KILL_EVIDENCE if k in low]
    both_present = (OOM_KILL in candidate_ids) and (MEM_PRESSURE in candidate_ids)
    top1 = candidate_ids[0] if candidate_ids else ""

    out: Dict[str, Any] = {
        "applicable": False,
        "rule": "",
        "winner": None,
        "text_hard_evidence": hard,
        "hypothetical_hints": [k for k in HYPOTHETICAL_HINTS if k in text],
        "metric": metric,
        "why": "",
    }

    if top1 not in (OOM_KILL, MEM_PRESSURE) or not both_present:
        out["rule"] = "0-not-ambiguous"
        out["why"] = ("候选里没有同时出现 %s / %s，或 top1 不在两者之中 → 文本打分说了算"
                      % (OOM_KILL, MEM_PRESSURE))
        return out

    out["applicable"] = True
    if hard:
        out["rule"] = "1-text-hard-evidence"
        out["winner"] = OOM_KILL
        out["why"] = "文本含『已被杀』的直接证据 %s → 判 %s（不查指标）" % (hard, OOM_KILL)
        return out

    if metric.get("available"):
        if metric.get("kill_observed"):
            out["rule"] = "2-metric-kill-observed"
            out["winner"] = OOM_KILL
            out["why"] = ("文本只有假设性/症状措辞，指标 increase(%s[%s]) = %s > 0 → 判 %s"
                          % (OOM_COUNTER, metric.get("window"), metric.get("max_increase"), OOM_KILL))
        else:
            out["rule"] = "2-metric-no-kill"
            out["winner"] = MEM_PRESSURE
            out["why"] = ("文本只有假设性/症状措辞，指标 increase(%s[%s]) = %s → 判 %s（被压到极限但未被杀）"
                          % (OOM_COUNTER, metric.get("window"),
                             metric.get("max_increase"), MEM_PRESSURE))
        return out

    out["rule"] = "3-metric-unavailable"
    out["why"] = ("文本只有假设性/症状措辞，且指标不可用（%s）→ 不介入，保持文本顺序"
                  % metric.get("error", "no series"))
    return out


def needs_metric(text: str, candidate_ids: Sequence[str]) -> bool:
    """
    是否需要**去查指标**。

    只在"文本自己判不了"时才查（省掉一次网络往返；Prometheus 不可用时也不会白等超时）：
      · 候选里必须同时有 `rc:oom-kill` 与 `rc:mem-pressure`，且 top1 是这两者之一；
      · 且文本里**没有**"已被杀"的硬证据（有的话规则 1 直接定案）。
    """
    ids = list(candidate_ids)
    top1 = ids[0] if ids else ""
    if top1 not in (OOM_KILL, MEM_PRESSURE):
        return False
    if not ((OOM_KILL in ids) and (MEM_PRESSURE in ids)):
        return False
    low = (text or "").lower()
    return not any(k in low for k in HARD_KILL_EVIDENCE)


def apply(text: str, candidates: List[Dict[str, Any]],
          query_fn: Callable[[str], Dict[str, Any]]) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    对候选集应用判别器：命中规则时把胜者提到第一位，并把理由追加进它的 `reason`。

    返回 `(新候选列表, 决策记录)`。决策记录会作为 `metric_discriminator` 字段进入结果，
    因此"为什么改判"永远可审计；未介入时也记录原因。
    """
    ids = [str(c.get("entity_id") or "") for c in candidates]
    # 只有"文本判不了"时才查指标（规则 1/0 都不需要网络）
    metric: Dict[str, Any] = query_oom_metric(query_fn) if needs_metric(text, ids) else {}
    decision = decide(text, ids, metric)

    if not (decision["applicable"] and decision["winner"]):
        return candidates, decision

    winner = decision["winner"]
    ordered = sorted(candidates, key=lambda c: 0 if str(c.get("entity_id")) == winner else 1)
    for c in ordered:
        if str(c.get("entity_id")) == winner:
            note = "；判据（指标平面）：%s" % decision["why"]
            if note not in str(c.get("reason") or ""):
                c["reason"] = str(c.get("reason") or "") + note
            break
    return ordered, decision
