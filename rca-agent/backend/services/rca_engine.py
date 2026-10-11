# -*- coding: utf-8 -*-
"""RCA 推理引擎 — 集成 Prometheus + EvoOntology + 本体驱动打分."""
from __future__ import annotations
import json, time, os
from typing import Any, Dict, List, Optional, Sequence
from pathlib import Path

# 服务依赖
from .prometheus import PrometheusClient
from .evo_ontology import EvoOntologyClient
from .ontology_scoring import ROLE_WEIGHT, OntologyScoring


# ── 告警关键词模式 ─────────────────────────────────────────────────────────────
# 仅用于 parse_alert 产出 matched_categories（兼容字段）与 LEGACY 兜底；
# 打分词典本身已改为从激活本体派生（见 ontology_scoring.py）。
KEYWORD_PATTERNS = {
    "延迟":    ["延迟", "latency", "p99", "P99", "超时", "timeout", "slow", "响应慢"],
    "连接":    ["连接", "pool", "conn", "exhaust", "max_connections", "连接池", "耗尽", "connection"],
    "数据库":  ["mysql", "MySQL", "数据库", "db", "sql", "慢查询", "索引", "database"],
    "容器":    ["容器", "container", "OOM", "重启", "restart", "内存", "cpu", "crash"],
    "部署":    ["部署", "deploy", "发布", "变更", "change", "upgrade", "上线"],
    "可用性":  ["不可用", "down", "5xx", "502", "503", "504", "unavailable"],
    "网络":    ["网络", "network", "超时", "timeout", "连接失败"],
    "存储":    ["磁盘", "storage", "disk", "空间不足", "disk full"],
}

# 根因类别 → 基线置信度
CATEGORY_BASE_SCORE = {
    "数据":    0.65,
    "资源":    0.55,
    "配置":    0.50,
    "依赖":    0.40,
    "代码":    0.30,
}

# 类别 → 关键证据字段（Prometheus metrics）
CATEGORY_EVIDENCE_FIELDS = {
    "数据":    ["mysql_pool_active", "mysql_query_duration", "慢查询"],
    "资源":    ["container_cpu", "container_memory", "OOM", "restart"],
    "配置":    ["timeout", "pool_limit", "max_connections"],
    "依赖":    ["mysql_up", "threads_connected"],
    "代码":    ["5xx", "error_rate"],
}

#: 告警关键词分组 → 打分类别（**已废弃**，仅在本体不可用时兜底）
LEGACY_ALERT_CAT_TO_SCORE_CAT = {
    "延迟":   {"数据", "配置"},
    "连接":   {"数据", "配置"},
    "数据库": {"数据", "依赖"},
    "容器":   {"资源"},
    "部署":   {"配置", "代码"},
    "可用性": {"依赖", "代码"},
    "网络":   {"依赖"},
    "存储":   {"资源"},
}

#: 硬编码打分词典（**已废弃**，仅作本体不可用时的兜底）
LEGACY_CATEGORY_KEYWORDS = {
    "数据": ["慢查询", "索引", "t_txn", "t_customer", "pool", "连接池", "max_connections", "mysql", "sql", "database"],
    "资源": ["容器", "container", "cpu", "内存", "oom", "restart", "replicas", "host"],
    "配置": ["timeout", "maxpool", "poollimit", "retry", "重试", "超时"],
    "依赖": ["下游", "downstream", "accesses", "datasource", "ds:"],
    "代码": ["bug", "空指针", "异常", "crash", "5xx"],
}


class RCAEngine:
    """
    RCA 推理引擎：
      1. 解析告警 → 关键词
      2. 并发查询：Prometheus 实时指标 + EvoOntology 拓扑
      3. 拓扑节点打分 → 候选根因
      4. 注入 Evidence → EvoOntology（可选）
      5. 返回结构化 RCA 结果
    """

    def __init__(self,
                 prometheus_url: str = "http://prometheus:9090",
                 evo_workspace: Optional[str] = None):
        self.prom = PrometheusClient(base_url=prometheus_url)
        self.evo  = EvoOntologyClient(workspace=evo_workspace)
        # 本体驱动的打分词典（缓存按 active 版本 + 目录 mtime 失效）
        self.scoring = OntologyScoring.get(getattr(self.evo, "workspace", None))
        self._last_edges: List[Dict[str, Any]] = []

    # ── 告警解析 ─────────────────────────────────────────────────────────────

    def parse_alert(self, message: str, severity: str = "P3") -> Dict[str, Any]:
        """从告警文本提取关键词和类别。"""
        keywords = []
        matched = []
        for cat, patterns in KEYWORD_PATTERNS.items():
            hit = [p for p in patterns if p.lower() in message.lower()]
            if hit:
                keywords.extend(hit)
                matched.append(cat)

        sev_boost = {"P0": 1.0, "P1": 0.7, "P2": 0.4, "P3": 0.2}.get(severity, 0.1)

        # 本体约束匹配：把告警文本接到本体的 trigger_keywords 上
        constraint_hits: List[Dict[str, Any]] = []
        if self.scoring.available:
            try:
                constraint_hits = self.scoring.match_constraints(message)
            except Exception:  # noqa: BLE001
                constraint_hits = []

        return {
            "message": message,
            "severity": severity,
            "keywords": keywords,
            "matched_categories": matched,
            "confidence_boost": sev_boost,
            "constraint_hits": constraint_hits[:6],
        }

    # ── Prometheus 指标查询 ──────────────────────────────────────────────────

    def gather_evidence(self) -> Dict[str, Any]:
        """并发查询所有关键监控指标。"""
        return self.prom.all_metrics()

    def check_alert_thresholds(self, evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        基于 Prometheus 指标判断阈值是否触发。

        注意: histogram bucket {le="X"} 是**累加计数**（≤X 的请求数），
        因此 "超过阈值" 的比例 = (总数 - le 桶计数) / 总数。
        """
        alerts = []

        # ── HTTP 延迟超过 500ms 的请求占比 ──────────────────────────
        try:
            raw = evidence.get("http_latency_buckets", {}) or {}
            buckets = {str(k): float(v) for k, v in raw.items()}
            total = float(evidence.get("http_request_total", 0) or 0)

            le_05 = buckets.get("0.5", 0.0)

            if total > 0:
                over_500ms = max(0.0, total - le_05)
                ratio = over_500ms / total
                status = "TRIGGERED" if ratio > 0.05 else "OK"
                alerts.append({
                    "rule": "HTTP 延迟 > 500ms 请求占比 > 5%",
                    "severity": "P1",
                    "metric": "cc_http_request_duration_seconds_bucket{le=\"0.5\"}",
                    "value": "{:.1f}% ({:.0f}/{:.0f})".format(ratio * 100, over_500ms, total),
                    "ratio": round(ratio, 4),
                    "status": status,
                })
        except (ValueError, TypeError):
            pass

        # ── 连接池使用率 ────────────────────────────────────────────
        try:
            pool = evidence.get("mysql_pool", {}) or {}
            active = float(pool.get("active", 0) or 0)
            limit = float(pool.get("limit", 0) or 0)
            if limit > 0:
                ratio = active / limit
                alerts.append({
                    "rule": "连接池使用率 > 80%",
                    "severity": "P1",
                    "metric": "cc_mysql_pool_active / cc_mysql_pool_limit",
                    "value": "{:.0f}/{:.0f} ({:.0f}%)".format(active, limit, ratio * 100),
                    "ratio": round(ratio, 4),
                    "status": "TRIGGERED" if ratio > 0.8 else "OK",
                })
        except (ValueError, TypeError):
            pass

        # ── MySQL 连接数 ────────────────────────────────────────────
        try:
            mg = evidence.get("mysql_global", {}) or {}
            tc = float(mg.get("threads_connected", 0) or 0)
            max_conn = float(evidence.get("mysql_max_connections", 200) or 200)
            ratio = tc / max_conn if max_conn > 0 else 0
            alerts.append({
                "rule": "MySQL threads_connected > 90% max_connections",
                "severity": "P2",
                "metric": "mysql_global_status_threads_connected",
                "value": "{:.0f}/{:.0f} ({:.0f}%)".format(tc, max_conn, ratio * 100),
                "ratio": round(ratio, 4),
                "status": "TRIGGERED" if ratio > 0.9 else "OK",
            })
        except (ValueError, TypeError):
            pass

        return alerts

    # ── 拓扑遍历 + 候选打分 ─────────────────────────────────────────────────

    def traverse_topology(self, app_name: str = "payment-app") -> List[Dict[str, Any]]:
        """从 EvoOntology 获取拓扑节点（BFS 图遍历）。"""
        graph = self.evo.get_topology_graph(app_name)
        self._last_edges = graph.get("edges", [])
        return graph.get("nodes", [])

    def traverse_topology_graph(self, app_name: str = "payment-app") -> Dict[str, Any]:
        """获取完整拓扑图 {nodes, edges}。"""
        graph = self.evo.get_topology_graph(app_name)
        self._last_edges = graph.get("edges", [])
        return graph

    def score_candidates(self, topology: List[Dict[str, Any]],
                         parsed_alert: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        对拓扑节点打分，生成候选根因。

        **打分词典来自激活本体**（`ontology_scoring`），且匹配方向是
        **告警文本 ↔ 本体术语**（不是术语 ↔ 自己的名字 —— 后者会必然自匹配，
        退化成"按类别排序"）：

          · 节点关键词 = Constraint.trigger_keywords（人工精修，全权）
            + Term.aliases / Term.id 词元（全权）
            + Term.name 的中文 n-gram（半权）
          · 文本贴合度 = min(命中权重, 3)/3，命中权重 = primary + 0.5 × secondary
          · 节点类别由本体结构推导（约束 target/scope ＞ Term.scope ＞ 邻居多数表决 ＞ id 前缀）
          · favoured = 告警命中的本体约束的 constraint_type/target 类别
        本体不可读时退回 LEGACY_CATEGORY_KEYWORDS / LEGACY_ALERT_CAT_TO_SCORE_CAT。

        加权评分:
          score = 0.25 × 类别基线
                + 0.50 × 文本贴合度
                + 0.15 × 类别对齐（节点类别 ∈ favoured）
                + 0.10 × 严重级别
                - 0.25（relation 边）/ -0.10（metric/alert/incident 等观测与规则节点）
        理论区间 ≈ [0.05, 0.99]
        """
        sev_boost = parsed_alert["confidence_boost"]
        message = str(parsed_alert.get("message") or "")
        msg_low = message.lower()
        use_ontology = bool(self.scoring.available and self.scoring.index)

        def _negative_hits(tid: str, text: str) -> List[str]:
            """本术语的 negative_keywords 在文本里命中的词（会被扣分，值得展示）。"""
            entry = self.scoring.index.get(tid) or {}
            return [str(k) for k in (entry.get("negative") or []) if str(k).lower() in text][:6]

        def _reason_sentence(cat: str, tid: str, matched: Sequence[Any],
                             exp: Dict[str, Any]) -> str:
            """
            把"命中 N 类关键词"这类流水话，写成一句**能读懂**的结论句子。

            仍然保留"命中关键词"这个事实（它是判别依据），但补上三个最要紧的问题：
            这是什么（可读名称）、机制是什么（definition）、落在哪个对象上（attributedTo）。
            全部素材来自本体，不在这里另编知识。
            """
            name = exp.get("name") or ""
            head = "告警文本命中本体已建模根因「%s」（`%s`）" % (name, tid) if name else \
                   "告警文本命中本体术语 `%s`" % tid
            kws = " / ".join(str(m) for m in list(matched)[:5])
            parts = [head + ("的打分关键词 %s" % kws if kws else "")]
            if exp.get("definition"):
                parts.append("机制：%s" % exp["definition"])
            aff = exp.get("affected") or []
            if aff:
                parts.append("归因对象：%s" % "、".join(
                    "%s（%s）" % (a.get("id"), a.get("label")) if a.get("label") and a.get("label") != a.get("id")
                    else str(a.get("id")) for a in aff[:2]))
            cons = exp.get("constraints") or []
            if cons:
                parts.append("判据：%s" % cons[0].get("description", ""))
            return "；".join(p for p in parts if p) + "。"


        if use_ontology:
            # ① 主信号：告警文本命中的本体约束 → 其类别
            favoured = self.scoring.favoured_categories(message)
            # ② 兜底：没有约束命中时，用"哪些术语的词典被文本命中"投票
            if not favoured:
                for tid in self.scoring.index:
                    if self.scoring.match_text(tid, msg_low)[0] > 0:
                        favoured.add(self.scoring.category_for(tid))
        else:
            favoured = set()
            for ac in set(parsed_alert.get("matched_categories") or []):
                favoured |= LEGACY_ALERT_CAT_TO_SCORE_CAT.get(ac, set())

        candidates: List[Dict[str, Any]] = []
        seen = set()

        for node in topology:
            entry_id = str(node.get("id", ""))
            if not entry_id or entry_id in seen:
                continue
            nid = entry_id.lower()
            ntype = str(node.get("type", "")).lower()
            is_relation = ntype == "relation" or nid.startswith("rel:")
            is_observation = nid.startswith(("metric:", "alert:", "incident:", "evt:", "con:"))

            best = None
            if use_ontology and entry_id in self.scoring.index:
                cat = self.scoring.category_for(entry_id)
                role = self.scoring.role_for(entry_id)
                _primary_hits, weighted, matched = self.scoring.match_text(entry_id, msg_low)
                # 必须至少命中一个词典词，否则"类别对齐"会把同类别术语全部灌进候选
                if weighted > 0:
                    base = CATEGORY_BASE_SCORE.get(cat, 0.5)
                    specificity = min(weighted, 3) / 3.0
                    alignment = 1.0 if cat in favoured else 0.0
                    score = (0.25 * base + 0.50 * specificity
                             + 0.15 * alignment + 0.10 * sev_boost)
                    if is_relation:
                        score -= 0.25
                    # 角色权重（本体显式声明）：根因 > 组件 > 观测/规则。
                    # 没有它，`metric:db-tmp-disk`（观测）会压过 `rc:tmp-disk`（根因），
                    # `db:mysql-replica`（组件）会压过 `rc:replica-lag`（根因）。
                    score *= ROLE_WEIGHT.get(role, 1.0)
                    best = (max(0.05, min(score, 0.99)), cat, matched, "ontology")
            else:
                combined = nid + " " + str(node.get("name", "")).lower()
                for cat, cat_kws in LEGACY_CATEGORY_KEYWORDS.items():
                    matched_kws = [kw for kw in cat_kws if kw in combined]
                    if not matched_kws:
                        continue
                    hit_count = len(matched_kws)
                    base = CATEGORY_BASE_SCORE[cat]
                    specificity = min(hit_count, 3) / 3.0
                    alignment = 1.0 if cat in favoured else 0.0
                    score = (0.35 * base + 0.20 * specificity
                             + 0.30 * alignment + 0.15 * sev_boost)
                    if is_relation:
                        score -= 0.25
                    score = max(0.05, min(score, 0.99))
                    if best is None or score > best[0]:
                        best = (score, cat, matched_kws, "legacy")

            if best is None:
                continue
            seen.add(entry_id)
            score, cat, matched, src = best
            exp = self.scoring.explain(entry_id) if src == "ontology" else {}
            # 结论不能只给 `类别 + rc:xxx`。这里把本体里**本来就有**的解释性字段
            # （机制 / 判别理由 / 归因对象 / 观测佐证 / 判据约束）一并带出去，
            # 由前端与 markdown 渲染成"能自解释"的结论。
            reason = _reason_sentence(cat, entry_id, matched, exp)
            candidates.append({
                "category": cat,
                "entity_id": node.get("id", ""),
                "entity_name": node.get("name", ""),
                "confidence": round(score, 2),
                "relation_type": node.get("relation_type", ""),
                "reason": reason,
                "scoring_source": "%s:%s" % (src, self.scoring.version if src == "ontology" else "builtin"),
                # ── 解释性字段（本体驱动）──
                "definition": exp.get("definition", ""),
                "rationale": exp.get("rationale", ""),
                "role": exp.get("role", ""),
                "lifecycle": exp.get("lifecycle", ""),
                "scope": exp.get("scope", ""),
                "matched_keywords": [str(m) for m in matched][:8],
                "affected": exp.get("affected", []),
                "evidenced_by": exp.get("evidenced_by", []),
                "constraints": exp.get("constraints", []),
                # ontology_v4：根因自带的处置动作（引擎据此替代按类别的通用建议）
                "remediation": exp.get("remediation", []),
                "negative_matched": _negative_hits(entry_id, msg_low),
            })

        # 按置信度降序；同分时非 relation 优先
        candidates.sort(key=lambda x: (
            -x["confidence"],
            1 if str(x["entity_id"]).startswith("rel:") else 0,
        ))
        # ★ 组件级结论 → 根因级结论（本体驱动，见方法注释）
        candidates = self._promote_component_to_root_cause(candidates)
        return candidates[:6]

    def _promote_component_to_root_cause(
            self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        把"落在组件上"的结论升级为"归因到该组件的根因"。

        ## 为什么需要它

        实测（验证手册 §11.26）：端到端严格口径的未命中里，有相当一部分不是"答错方向"，
        而是**答到了组件层**就停了 —— 例如 `app_pause_replica` 给
        `app:payment-app-cluster`、`db_replica_kill` 给 `db:mysql-replica`。
        这些是本体里的**合法术语**（所以宽松命中会算对），但不是根因，
        用户拿不到"到底是什么坏了、该怎么办"。

        根因不是靠猜的：本体里 `rc:X --attributedTo--> 组件` 这条关系
        **就是**"这个根因落在哪个组件上"的显式声明（ontology_v5 有 15 条），
        另有 `rc:X --evidencedBy--> metric:M` 说明它的观测佐证。
        于是升级判据完全来自本体，不在代码里另编知识。

        ## 判据（按优先级）

          1. 候选里 `role == root_cause` 且 `affected`（attributedTo 目标）包含当前 top1；
          2. 其中 `evidenced_by` 的指标**也在候选里**的优先 —— 说明告警文本还提到了
             印证它的那条指标，证据更强；
          3. 仍并列时按置信度。

        找不到锚定的根因时**保持原样**（绝不硬凑一个），只在真的升级时留下
        `promoted_from` / `promotion_basis` 等字段，保证这个动作可审计。
        """
        if len(candidates) < 2:
            return candidates
        # 度量开关：置 `RCA_DISABLE_COMPONENT_PROMOTION=1` 可关闭升级，
        # 用于**成对评估**"升级到底有没有用"（tools/rootcause_promotion_verify.py）。
        # 生产默认开启。
        if os.environ.get("RCA_DISABLE_COMPONENT_PROMOTION") == "1":
            return candidates
        top = candidates[0]
        if str(top.get("role") or "") == "root_cause":
            return candidates
        top_id = str(top.get("entity_id") or "")
        if not top_id:
            return candidates
        # ⚠ 只与**文本显著命中的前 6 个候选**比对，而不是"所有被打分的节点"。
        # 踩过的坑：升级函数拿到的是完整打分列表（54 个节点都算了分），
        # 于是一个根因只要有个别佐证指标在大列表里出现过就能凑到"证据重叠"，
        # 再靠更高置信度压过真正由证据召回的根因 —— 实测 `app_pause_replica`
        # 因此升级到了 `rc:cluster-capacity` 而不是 `rc:replica-hang`。
        # 前 6 个才是"告警文本确实点名了"的那些术语。
        prominent = candidates[:6]
        cand_ids = {str(c.get("entity_id")) for c in prominent}
        anchored: List[tuple] = []
        for c in prominent:
            if c is top:
                continue
            if str(c.get("role") or "") != "root_cause":
                continue
            aff = {str(a.get("id")) for a in (c.get("affected") or []) if isinstance(a, dict)}
            if top_id not in aff:
                continue
            ev = {str(e.get("id")) for e in (c.get("evidenced_by") or []) if isinstance(e, dict)}
            anchored.append((len(ev & cand_ids), float(c.get("confidence") or 0), c, sorted(ev & cand_ids)))

        # ── 按**证据**召回锚点：根因术语的关键词没命中，但它的观测佐证命中了 ──
        # 为什么需要这条路：实测 `app_pause_replica` 的告警文本是"单个副本健康探针失败…
        # 集群容量下降"，命中的指标是 `metric:replica-healthy-count`（观测角色），
        # 而期望的 `rc:replica-hang` **自己没能进候选**（关键词不同）。
        # 本体里 `rc:replica-hang --evidencedBy--> metric:replica-healthy-count`
        # 已经写明了这条关系，于是可以"从证据反推根因"：
        # 凡是以"候选里的观测术语"为佐证、且归因到当前组件的根因，都补进锚点。
        obs_cand = {str(c.get("entity_id")): float(c.get("confidence") or 0)
                    for c in prominent
                    if str(c.get("role") or "") == "observation"}
        have = {str(c.get("entity_id")) for c in prominent}
        for metric_id, metric_conf in obs_cand.items():
            for rc_id in self.scoring.root_causes_evidenced_by(metric_id):
                if rc_id in have or top_id not in self.scoring.attributed_to(rc_id):
                    continue
                exp = self.scoring.explain(rc_id)
                if not exp:
                    continue
                # 由证据推断出来的根因：置信度取"佐证指标的分数 × 0.95"，
                # 略低于该指标本身 —— 它是推断，不是直接命中。
                synth = {
                    "category": exp.get("category", ""),
                    "entity_id": rc_id,
                    "entity_name": exp.get("name", ""),
                    "confidence": round(metric_conf * 0.95, 2),
                    "relation_type": "",
                    # ⚠ 这里要说的是**观测**的名字（metric_id 的 label），不是根因的名字。
                    # 原实现误用了 `exp`（那是 rc_id 的 explain），于是 reason 里出现
                    # 「告警文本命中的观测『根因：应用副本丢失』」这种把根因名当观测名的句子，
                    # 读起来像本体把名称写错了 —— 实际是文案 bug（本体里
                    # metric:replica-healthy-count 的名字是"在册健康副本数"，没问题）。
                    "reason": ("告警文本命中的观测「%s」（`%s`）在本体里是 `%s` 的观测佐证"
                               "（evidencedBy），据此从证据反推根因。"
                               % (self.scoring.label_for(metric_id) or metric_id,
                                  metric_id, exp.get("name") or rc_id)),
                    "scoring_source": "%s:evidence" % (self.scoring.version or "ontology"),
                    "definition": exp.get("definition", ""),
                    "rationale": exp.get("rationale", ""),
                    "role": exp.get("role", "root_cause"),
                    "lifecycle": exp.get("lifecycle", ""),
                    "scope": exp.get("scope", ""),
                    "matched_keywords": [],
                    "affected": exp.get("affected", []),
                    "evidenced_by": exp.get("evidenced_by", []),
                    "constraints": exp.get("constraints", []),
                    "remediation": exp.get("remediation", []),
                    "negative_matched": [],
                    "recovered_by_evidence": metric_id,
                }
                anchored.append((1, float(synth["confidence"]), synth, [metric_id]))

        if not anchored:
            return candidates

        anchored.sort(key=lambda t: (-t[0], -t[1]))
        extra_ev, _conf, best, ev_hits = anchored[0]
        best["promoted_from"] = top_id
        best["promoted_from_name"] = top.get("entity_name") or ""
        best["promotion_basis"] = ("本体关系 attributedTo：`%s` 归因到组件 `%s`"
                                   % (best.get("entity_id"), top_id))
        if ev_hits:
            best["promotion_evidence"] = "同时候选里命中了它的观测佐证：%s" % "、".join(ev_hits)
        else:
            best["promotion_evidence"] = ""
        # 结论句子要说明"这不是凭空换的"，否则看起来像引擎改口
        promo = ("；该结论由**组件级**候选 `%s` 升级而来（依据：%s%s）"
                 % (top_id, best["promotion_basis"],
                    ("，" + best["promotion_evidence"]) if best["promotion_evidence"] else ""))
        best["reason"] = str(best.get("reason") or "") + promo
        return [best] + [c for c in candidates if c is not best]

    # ── 根因路径追踪 ────────────────────────────────────────────────────────

    def trace_path_to_root(self, edges: List[Dict[str, Any]],
                           root: Optional[Dict[str, Any]],
                           topology: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        从根因实体反向追踪到应用入口（app:payment-app）的拓扑路径。

        采用 BFS over 无向边，输出 [{from, relation, to, relation_type}] 序列。
        """
        if not root:
            return []

        target = "app:payment-app"
        start = root.get("entity_id", "")
        if not start:
            return []
        if start == target:
            return []

        # 构建无向邻接表
        adj: Dict[str, List[Dict[str, Any]]] = {}
        for e in edges:
            s, t = e.get("source"), e.get("target")
            if not s or not t:
                continue
            adj.setdefault(s, []).append(e)
            adj.setdefault(t, []).append({
                "id": e.get("id"),
                "source": t,
                "target": s,
                "relation_type": e.get("relation_type", ""),
                "condition": e.get("condition", ""),
            })

        # BFS 找最短路径
        from collections import deque
        prev: Dict[str, Any] = {start: None}
        q = deque([start])
        found = False
        while q:
            cur = q.popleft()
            if cur == target:
                found = True
                break
            for e in adj.get(cur, []):
                nxt = e.get("target")
                if nxt and nxt not in prev:
                    prev[nxt] = {"node": cur, "edge": e}
                    q.append(nxt)

        if not found:
            return []

        # 回溯路径
        path = []
        cur = target
        while prev.get(cur) is not None:
            step = prev[cur]
            e = step["edge"]
            path.append({
                "from": step["node"],
                "to": cur,
                "relation": e.get("id", ""),
                "relation_type": e.get("relation_type", ""),
                "condition": e.get("condition", ""),
            })
            cur = step["node"]
        path.reverse()
        return path

    # ── 推理链生成 ──────────────────────────────────────────────────────────

    def build_rca_chain(self, parsed_alert: Dict[str, Any],
                        topology: List[Dict[str, Any]],
                        candidates: List[Dict[str, Any]],
                        evidence: Dict[str, Any],
                        thresholds: List[Dict[str, Any]],
                        root_path: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """构建可解释的推理链。"""
        chain = []

        # Step 1: 告警信号
        chain.append({
            "step": 1,
            "type": "alert",
            "description": f"[{parsed_alert['severity']}] {parsed_alert['message']}",
            "keywords": parsed_alert["keywords"][:6],
            "matched_categories": parsed_alert["matched_categories"],
            "source": "user_input",
        })

        # Step 2: Prometheus 阈值检查
        if thresholds:
            triggered = [t for t in thresholds if t.get("status") == "TRIGGERED"]
            chain.append({
                "step": 2,
                "type": "monitoring",
                "description": f"Prometheus 阈值检查: {len(triggered)} 项触发",
                "alerts": triggered,
                "evidence": evidence,
                "source": "prometheus",
            })
        else:
            chain.append({
                "step": 2,
                "type": "monitoring",
                "description": "Prometheus 指标采集",
                "evidence": evidence,
                "source": "prometheus",
            })

        # Step 3: 拓扑节点
        chain.append({
            "step": 3,
            "type": "topology",
            "description": f"本体拓扑逆向遍历: 发现 {len(topology)} 个节点",
            "nodes": [{"id": n["id"], "name": n["name"], "type": n["type"]} for n in topology[:10]],
            "source": "evo_ontology",
        })

        # Step 4: 根因路径
        step_no = 4
        if root_path:
            hops = " → ".join(
                [root_path[0]["from"]] + [p["to"] for p in root_path]
            )
            chain.append({
                "step": step_no,
                "type": "path",
                "description": "根因传播路径: " + hops,
                "path": root_path,
                "source": "evo_ontology",
            })
            step_no += 1

        # Step 5+: 候选根因
        for c in candidates[:4]:
            chain.append({
                "step": step_no,
                "type": "candidate",
                "category": c["category"],
                "entity": c["entity_id"],
                "confidence": c["confidence"],
                "reason": c["reason"],
            })
            step_no += 1

        return chain

    # ── 主推理入口 ──────────────────────────────────────────────────────────

    def infer(self, message: str, severity: str = "P1",
              app_name: str = "payment-app",
              inject_evidence: bool = True) -> Dict[str, Any]:
        """
        执行完整 RCA 推理。

        Args:
            message: 告警/故障描述
            severity: P0/P1/P2/P3
            app_name: 应用名（用于拓扑查询）
            inject_evidence: 是否将结果写入 EvoOntology

        Returns:
            结构化 RCA 结果 dict
        """
        t0 = time.time()
        incident_id = f"INC-{int(time.time())}"

        # 1. 解析告警
        parsed_alert = self.parse_alert(message, severity)

        # 2. 并发采集 Prometheus 指标
        evidence = self.gather_evidence()
        thresholds = self.check_alert_thresholds(evidence)

        # 3. 查询本体拓扑（BFS 图遍历）
        graph = self.traverse_topology_graph(app_name)
        topology = graph.get("nodes", [])
        edges = graph.get("edges", [])

        # 4. 候选根因打分
        candidates = self.score_candidates(topology, parsed_alert)
        root = candidates[0] if candidates else None

        # 4b. 计算根因到告警的拓扑路径
        root_path = self.trace_path_to_root(edges, root, topology) if root else []

        # 5. 推理链
        rca_chain = self.build_rca_chain(
            parsed_alert, topology, candidates, evidence, thresholds, root_path
        )

        elapsed_ms = round((time.time() - t0) * 1000, 1)

        result = {
            "incident_id": incident_id,
            "alert": parsed_alert,
            "evidence": evidence,
            "thresholds_triggered": thresholds,
            "topology": topology,
            "topology_edges": edges,
            "root_cause": root,
            "root_cause_path": root_path,
            "candidates": candidates,
            "confidence": round(root["confidence"], 2) if root else 0.0,
            "rca_chain": rca_chain,
            "elapsed_ms": elapsed_ms,
            # 打分词典来源（本体版本 / 是否退化为内置表），便于审计与 A/B
            "scoring": {
                "source": "ontology" if (self.scoring.available and self.scoring.index) else "legacy",
                "ontology_version": self.scoring.version or None,
                "terms_indexed": len(self.scoring.index),
                "error": self.scoring.error or None,
            },
        }

        # 6. 注入 Evidence 到 EvoOntology（异步，不阻塞返回）
        if inject_evidence and root:
            self._inject_evidence(result, incident_id)

        return result

    # ── Evidence 注入 ───────────────────────────────────────────────────────

    def _inject_evidence(self, result: Dict[str, Any], incident_id: str) -> None:
        """
        把 RCA 结论沉淀为**证据记录** —— 默认**不改变激活本体**。

        ## 曾经的坑（实测，见 reports/host_path_regression.json 与 CHANGELOG）

        原实现是：

            version_name, records = SemanticStore.load_records(workspace, version="ontology_v0")
            records["evidence"].append(ev_record)
            SemanticStore.save_version(workspace, "ontology_v0-rca-agent", records)
            SemanticStore.set_active(workspace, "ontology_v0-rca-agent")

        两个问题叠在一起：

          ① 基线取的是 **ontology_v0**（最初版），不是当前知识；
          ② 随后 `set_active` —— 于是一次 `POST /api/rca/infer`
             （`infer()` 的 `inject_evidence` **默认 True**）就会把激活本体从
             `ontology_v6` 静默换成「v0 + 一条证据」，六轮迭代的知识当场消失。
             触发途径很日常：`tools/fault_drill.py`、任何走 `/api/rca/infer` 的调用。

        ## 现在的行为

          · 记录**追加**到 `<workspace>/rca_evidence_inbox.jsonl`（一行一条，带当时的本体版本）；
          · **不动 `active.json`、不写版本目录** —— "诊断用哪一版本体"只能由
            版本化轮次 + 发布闸门改变，不能由一次推理的副作用改变；
          · 需要旧行为（把证据并进本体**并**切换激活版本）时，显式设
            `RCA_EVIDENCE_ACTIVATE=1`；此时基线改为**当前激活版本**（而不是 v0），
            目标版本可用 `RCA_EVIDENCE_VERSION` 覆盖（默认 `ontology_v0-rca-agent`）。
        """
        try:
            from evoontology.ontology.store import SemanticStore
            import datetime

            workspace = Path(self.evo.workspace)
            try:
                active_version = SemanticStore.active_version(workspace)
            except Exception:  # noqa: BLE001
                active_version = ""

            ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            rc = result.get("root_cause", {})
            chain = result.get("rca_chain", [])
            ev_id = f"ev:rca-{incident_id.lower().replace('-','_')}"

            ev_record = {
                "id": ev_id,
                "source": "RCA Agent inference",
                "query": result["alert"]["message"],
                "result": json.dumps({
                    "category": rc.get("category", "unknown"),
                    "entity_id": rc.get("entity_id", ""),
                    "confidence": rc.get("confidence", 0),
                    "reason": rc.get("reason", ""),
                    "rca_chain": chain,
                    "topology": [n["id"] for n in result.get("topology", [])],
                    "candidates": [
                        {"entity_id": c["entity_id"], "category": c["category"], "confidence": c["confidence"]}
                        for c in result.get("candidates", [])
                    ],
                }, ensure_ascii=False),
                "validation_method": "RCA engine confidence scoring + Prometheus evidence",
                "timestamp": ts,
            }

            # ── 默认路径：只记录证据，不碰激活本体 ──────────────────────────
            if os.environ.get("RCA_EVIDENCE_ACTIVATE", "").strip().lower() not in ("1", "true", "yes"):
                inbox = Path(self.evo.workspace) / "rca_evidence_inbox.jsonl"
                inbox.parent.mkdir(parents=True, exist_ok=True)
                with inbox.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ontology_version": active_version, **ev_record},
                                        ensure_ascii=False) + "\n")
                print(f"[RCA] Evidence recorded: {ev_id} -> {inbox.name}"
                      f"（active 仍为 {active_version}；不切换本体）", flush=True)
                return

            # ── 显式选择旧行为（RCA_EVIDENCE_ACTIVATE=1）：基线用当前激活版本 ──
            _, records = SemanticStore.load_records(workspace,
                                                    version=active_version or "ontology_v0")
            records.setdefault("evidence", []).append(ev_record)
            target = os.environ.get("RCA_EVIDENCE_VERSION", "ontology_v0-rca-agent")
            SemanticStore.save_version(workspace, target, records)
            SemanticStore.set_active(workspace, target)
            print(f"[RCA] Evidence injected: {ev_id} -> {target}"
                  f"（ACTIVATED，因 RCA_EVIDENCE_ACTIVATE=1；原 active={active_version}）",
                  flush=True)
        except Exception as e:
            print(f"[RCA] Evidence injection failed: {e}")
