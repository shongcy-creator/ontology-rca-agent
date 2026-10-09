# -*- coding: utf-8 -*-
"""
本体驱动的打分词典（ontology-driven scoring）。

## 为什么需要它

第一次集群化本体迭代（`ontology_v0-rca-agent` → `ontology_v1`）后，
本体的**知识完备性**从 0.048 提升到 1.000（21/21 故障场景都具备了
根因术语、到用户可见入口的传播路径、以及可观测连接），
但确定性规则引擎的候选命中只从 top5 1/21 提升到 4/21。

原因很明确：`rca_engine.category_keywords` 是一张**代码内置**的关键词表，
`rc:row-lock` / `rc:replica-loss` 这类新术语既不在表里、名字里也没有表中的关键词，
因此永远不会被打分命中。于是"本体迭代"只能让知识躺在库里，改不动引擎行为。

本模块把这张表**从激活本体派生**出来，闭环才真正闭上：

  打分词典(term) := {
      primary   : Constraint.trigger_keywords（人工精修，权重最高）
                + Term.aliases / Term.id 的 ASCII 词元
      secondary : Term.name 的 CJK n-gram（2~4 字）
      category  : 由本体结构推导出的 5 类根因类别（数据/资源/配置/依赖/代码）
  }

## 类别推导（确定性、可解释、可复算）

5 类根因类别是**下游契约**（`orchestrator._suggest_actions`、
`router` 的 `unknown_category` 判定、前端展示都依赖它），
因此不能改词表，只能改"怎么算出来"。按优先级链推导：

  1. 该 Term 是某条 Constraint 的 `target`
        → constraint_type 映射（capacity→资源、threshold→数据、
          business_rule→依赖、data_quality→数据、enum_semantics/unit/scope→配置）
          多条时按 severity(block>warn>info) 取最强
  2. Term.scope 命中 SCOPE_TO_CATEGORY
  3. `rc:` / `metric:` 前缀 → **沿本体的关系图回溯**：取它 `attributedTo`
     的实体（或引用它的生产者的）类别（递归深度 ≤3）
  4. id 前缀兜底（app:/env:/db:/ds:/table: …）
  5. 默认 "数据"

## 兜底

本体不可读（文件缺失/JSON 坏/版本未激活）时 `available` 为 False，
`rca_engine` 会退回原来的硬编码表，保证功能不退化。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ── 5 类根因类别（下游契约，不可改）────────────────────────────────────────
CATEGORIES = ("数据", "资源", "配置", "依赖", "代码")

#: Constraint.constraint_type → 根因类别
CONSTRAINT_TYPE_TO_CATEGORY = {
    "capacity":       "资源",
    "threshold":      "数据",
    "business_rule":  "依赖",
    "data_quality":   "数据",
    "enum_semantics": "配置",
    "unit":           "配置",
    "scope":          "配置",
}

#: Constraint.severity 强弱（取最强的一条决定类别）
SEVERITY_RANK = {"block": 3, "warn": 2, "warning": 2, "info": 1}

#: Term.scope → 根因类别
SCOPE_TO_CATEGORY = {
    "应用层":     "代码",
    "运行环境层": "资源",
    "数据库层":   "数据",
}

#: Term.id 前缀兜底 → 根因类别
ID_PREFIX_TO_CATEGORY = {
    "app:":   "代码",
    "api:":   "代码",
    "fn:":    "代码",
    "env:":   "资源",
    "host:":  "资源",
    "db:":    "数据",
    "table:": "数据",
    "ds:":    "配置",
    "con:":   "配置",
}

#: ASCII 词元里的通用词（不具区分度，会污染打分）
STOPWORDS = {
    "app", "core", "root", "cause", "the", "and", "for", "with",
    "com", "main", "new", "src",
}

#: 关系里表示"归因"的连接条件前缀（用于类别回溯）
ATTRIBUTION_HINTS = ("attributedto", "attributed_to", "归因")

#: Term.role —— 术语在 RCA 中的角色（ontology_v2 起由本体显式声明）
#: 缺失时按 id 前缀推导（渐进式 schema 扩展，不破坏旧版本）
ROLE_WEIGHT = {
    "root_cause":  1.15,   # 根因：告警要定位的目标
    "component":   1.00,   # 组件/实体：受影响的载体
    "observation": 0.85,   # 观测（指标/告警/事件）：是证据，不是根因
    "rule":        0.80,   # 约束/规则：是判据，不是根因
}
ROLE_BY_PREFIX = (
    ("rc:", "root_cause"),
    ("metric:", "observation"),
    ("alert:", "observation"),
    ("incident:", "observation"),
    ("evt:", "observation"),
    ("con:", "rule"),
)


def derive_role(term_id: str) -> str:
    for prefix, role in ROLE_BY_PREFIX:
        if term_id.startswith(prefix):
            return role
    return "component"

_CJK = re.compile(r"[\u4e00-\u9fff]")
_ASCII_TOKEN = re.compile(r"[a-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NAME_SPLIT = re.compile(r"[：:，,、（）()\[\]【】/／\s·\-—_]+")


def _ascii_tokens(*texts: str) -> List[str]:
    """从若干文本里抽 ASCII 词元（拆 camelCase / 分隔符，去停用词，长度 ≥3）。"""
    out: List[str] = []
    for t in texts:
        if not t:
            continue
        for part in _CAMEL.split(str(t)):
            for tok in _ASCII_TOKEN.findall(part.lower()):
                if len(tok) >= 3 and tok not in STOPWORDS and tok not in out:
                    out.append(tok)
    return out


def _cjk_ngrams(text: str, max_len: int = 4) -> List[str]:
    """从中文文本片段生成 2~max_len 字 n-gram（用于中文告警文本的子串匹配）。"""
    out: List[str] = []
    for seg in _NAME_SPLIT.split(text or ""):
        seg = _CJK.sub(lambda m: m.group(0), seg)
        chars = "".join(ch for ch in seg if _CJK.match(ch))
        if len(chars) < 2:
            continue
        for n in range(2, min(max_len, len(chars)) + 1):
            for i in range(0, len(chars) - n + 1):
                g = chars[i:i + n]
                if g not in out:
                    out.append(g)
    return out


class OntologyScoring:
    """
    从激活本体派生打分词典 + 类别 + 约束匹配。

    进程内缓存，缓存键 = (workspace, active_version, 版本目录 mtime)，
    本体演化（`accept()` 切换 active + 写入新版本目录）后自动失效。
    """

    _cache: Dict[str, "OntologyScoring"] = {}
    _lock = threading.Lock()

    def __init__(self, workspace: Optional[str] = None,
                 version: Optional[str] = None,
                 records: Optional[Dict[str, List[dict]]] = None) -> None:
        self.workspace = self._resolve_workspace(workspace)
        #: 显式指定版本时用于离线 A/B（不切换 active.json）；None = 跟随 active
        self.forced_version = version or None
        #: 直接用内存里的 5 族记录构建索引（演化候选版本尚未落盘时使用）
        self._records = records
        self.available = False
        self.version = ""
        self.error = ""
        self.index: Dict[str, Dict[str, Any]] = {}
        self.constraints: List[Dict[str, Any]] = []
        self.relations: List[Dict[str, Any]] = []
        #: 关系反查索引（`_build` 填充）：rc→它归因到的组件、metric→以它为观测佐证的根因。
        #: 这里给默认值，保证 `_build()` 抛异常（本体不可读）时查询方法不会 AttributeError。
        self._attributed: Dict[str, List[str]] = {}
        self._by_evidence: Dict[str, List[str]] = {}
        self.known_root_causes: Set[str] = set()
        self._build()

    # ── 定位与缓存 ────────────────────────────────────────────────────

    @staticmethod
    def _resolve_workspace(workspace: Optional[str]) -> str:
        import os
        cands = [workspace or "", os.environ.get("EVO_WORKSPACE", ""),
                 "/app/.evoontology"]
        here = Path(__file__).resolve()
        for up in range(3, 6):
            try:
                cands.append(str(here.parents[up] / ".evoontology"))
            except IndexError:
                break
        for c in cands:
            if c and Path(c).is_dir():
                return str(Path(c).resolve())
        return str(Path(cands[-1]).resolve()) if cands[-1] else ""

    def _cache_key(self) -> str:
        try:
            ws = Path(self.workspace)
            active = ws / "active.json"
            ver = self.forced_version or ""
            if not ver and active.is_file():
                data = json.loads(active.read_text(encoding="utf-8"))
                ver = data.get("active_version") or data.get("version") or ""
            vdir = ws / "versions" / ver if ver else ws
            mtime = int(vdir.stat().st_mtime) if vdir.exists() else 0
            return "%s|%s|%d" % (self.workspace, ver, mtime)
        except Exception:  # noqa: BLE001
            return self.workspace + "|unknown"

    @classmethod
    def for_version(cls, workspace: Optional[str], version: str) -> "OntologyScoring":
        """离线构造某**指定版本**的打分索引（不读/不改 active.json，用于 A/B）。"""
        return cls(workspace, version=version)

    @classmethod
    def from_records(cls, records: Dict[str, List[dict]],
                     label: str = "(in-memory)") -> "OntologyScoring":
        """
        用内存里的 5 族记录构建索引。

        用于评估"尚未落盘的候选版本"（本体演化在 save_version 之前就要算成对评分），
        避免为了评分把候选临时写进工作区再删掉。
        """
        return cls(version=label, records=records)

    @classmethod
    def get(cls, workspace: Optional[str] = None) -> "OntologyScoring":
        """取（并缓存）某工作区 **当前激活版本** 的打分索引。"""
        probe = cls.__new__(cls)
        probe.workspace = cls._resolve_workspace(workspace)
        probe.forced_version = None
        key = probe._cache_key()
        with cls._lock:
            hit = cls._cache.get(key)
            if hit is not None:
                return hit
        inst = cls(probe.workspace)
        with cls._lock:
            cls._cache = {k: v for k, v in cls._cache.items() if not k.startswith(inst.workspace + "|")}
            cls._cache[inst._cache_key()] = inst
        return inst

    # ── 加载本体 5 族记录 ─────────────────────────────────────────────

    def _load_records(self) -> Tuple[str, Dict[str, List[dict]]]:
        if self._records is not None:
            return (self.forced_version or "(in-memory)"), self._records
        ws = Path(self.workspace)
        if self.forced_version:
            version = self.forced_version
        else:
            data = json.loads((ws / "active.json").read_text(encoding="utf-8"))
            version = data.get("active_version") or data.get("version") or ""
        vdir = ws / "versions" / version
        records: Dict[str, List[dict]] = {}
        for fam in ("terms", "mappings", "relations", "constraints", "evidence"):
            f = vdir / ("%s.json" % fam)
            records[fam] = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else []
        return version, records

    # ── 构建 ──────────────────────────────────────────────────────────

    def _build(self) -> None:
        try:
            version, rec = self._load_records()
            self.version = version
            terms = {t["id"]: t for t in rec["terms"] if isinstance(t, dict) and t.get("id")}
            self.relations = [r for r in rec["relations"] if isinstance(r, dict)]
            self.constraints = [c for c in rec["constraints"] if isinstance(c, dict)]

            # 约束 target / scope → 约束列表
            cons_by_target: Dict[str, List[dict]] = {}
            cons_by_scope: Dict[str, List[dict]] = {}
            for c in self.constraints:
                tgt = str(c.get("target") or "")
                if tgt:
                    cons_by_target.setdefault(tgt, []).append(c)
                scp = str(c.get("scope") or "")
                if scp:
                    cons_by_scope.setdefault(scp, []).append(c)

            cats: Dict[str, str] = {}
            memo: Dict[str, str] = {}
            for tid in terms:
                cats[tid] = self._resolve_category(tid, terms, cons_by_target,
                                                   cons_by_scope, memo)

            # 关系反查索引（一次建好，供"组件级→根因级"升级与按证据召回使用）
            #   attributed:   rc:X  → [它归因到的组件]
            #   by_evidence:  metric:M → [以 M 为观测佐证的根因]
            self._attributed: Dict[str, List[str]] = {}
            self._by_evidence: Dict[str, List[str]] = {}
            for r in self.relations:
                src = str(r.get("source") or "")
                tgt = str(r.get("target") or "")
                cond = str(r.get("connection_condition") or "").lower()
                if not src or not tgt:
                    continue
                if cond.startswith("attributedto"):
                    self._attributed.setdefault(src, []).append(tgt)
                elif cond.startswith("evidencedby"):
                    self._by_evidence.setdefault(tgt, []).append(src)

            for tid, term in terms.items():
                primary = _ascii_tokens(tid, tid.split(":")[-1],
                                        " ".join(term.get("aliases") or []),
                                        term.get("name", ""))
                for c in cons_by_target.get(tid, []):
                    for kw in (c.get("trigger_keywords") or []):
                        kw = str(kw).strip()
                        if kw and kw not in primary:
                            primary.append(kw)
                # ontology_v2 起：术语自带判别性关键词（比 name/alias 更贴近告警措辞）
                for kw in (term.get("scoring_keywords") or []):
                    kw = str(kw).strip()
                    if kw and kw not in primary:
                        primary.append(kw)
                secondary = [g for g in _cjk_ngrams(term.get("name", ""))
                             if g not in primary]
                negatives = [str(k).strip() for k in (term.get("negative_keywords") or [])
                             if str(k).strip()]
                self.index[tid] = {
                    "primary": primary[:32],
                    "secondary": secondary[:32],
                    "negative": negatives,
                    "role": str(term.get("role") or derive_role(tid)),
                    "category": cats[tid],
                    "type": term.get("type", ""),
                    "scope": term.get("scope", ""),
                    "name": term.get("name", ""),
                    "source": "ontology:%s" % ("constraint" if cons_by_target.get(tid) else
                                               term.get("scope") or "id-prefix"),
                    # ── 结论解释所需字段 ──────────────────────────────────
                    # 「根因结论」不能只丢一个 `rc:xxx` 给用户。这些字段就是让结论
                    # 能自解释的原材料（机制 / 判据 / 生命周期 / 证据引用）。
                    "definition": term.get("definition", ""),
                    "rationale": term.get("scoring_rationale", ""),
                    "lifecycle": str((term.get("lifecycle") or {}).get("state") or ""),
                    "evidence_refs": list(term.get("evidence_refs") or []),
                    "aliases": list(term.get("aliases") or []),
                    # ontology_v4 起：术语自带**处置动作**（结构同构于 next_actions）。
                    # "怎么办"与"是什么/为什么/在哪/凭什么"一样由本体维护、可评审、可版本化；
                    # 引擎优先用它，取不到才回落到按类别硬编码的通用建议。
                    "remediation": [dict(x) for x in (term.get("remediation") or [])
                                    if isinstance(x, dict)],
                }

            # 已知根因：本体里 rc: 前缀的 entity 术语（不再硬编码白名单）
            self.known_root_causes = {
                tid for tid, t in self.index.items()
                if tid.startswith("rc:") and t.get("type") in ("entity", "concept", "")
            }
            self.available = bool(self.index)
        except Exception as e:  # noqa: BLE001
            self.error = "%s: %s" % (type(e).__name__, e)
            self.available = False

    # ── 类别推导 ──────────────────────────────────────────────────────

    def _resolve_category(self, tid: str, terms: Dict[str, dict],
                          cons_by_target: Dict[str, List[dict]],
                          cons_by_scope: Dict[str, List[dict]],
                          memo: Dict[str, str]) -> str:
        """
        推导术语的根因类别（确定性、无深递归、可复算）。

        规则优先级：
          1. 该 Term 是某条 Constraint 的 target（最强：约束显式声明了它管什么）
          2. 该 Term 是某条 Constraint 的 scope（约束作用域也是显式声明）
          3. Term.scope 命中 SCOPE_TO_CATEGORY
          4. `rc:` / `metric:` 等派生节点 → 对**直接相邻的非观测节点**的类别做多数表决
             （不做深递归：深递归会让类别随关系遍历顺序漂移，不可复算）
          5. id 前缀兜底
          6. 默认 "数据"
        """
        if tid in memo:
            return memo[tid]
        memo[tid] = "数据"        # 防环

        def strongest(cs: List[dict]) -> Optional[str]:
            if not cs:
                return None
            best = max(cs, key=lambda c: (SEVERITY_RANK.get(str(c.get("severity", "")).lower(), 0),
                                          str(c.get("id", ""))))
            return CONSTRAINT_TYPE_TO_CATEGORY.get(str(best.get("constraint_type") or ""))

        # ── 规则 1 / 2：约束的 target 或 scope ─────────────────────────
        cat = strongest(cons_by_target.get(tid) or []) or strongest(cons_by_scope.get(tid) or [])
        if cat:
            memo[tid] = cat
            return cat

        term = terms.get(tid) or {}

        # ── 规则 3：scope 直接映射 ────────────────────────────────────
        cat = SCOPE_TO_CATEGORY.get(str(term.get("scope") or ""))
        if cat:
            memo[tid] = cat
            return cat

        # ── 规则 4：派生节点（rc:/metric:/alert:/incident:）多数表决 ────
        if tid.startswith(("rc:", "metric:", "alert:", "incident:", "evt:")):
            votes: Dict[str, int] = {}
            for r in self.relations:
                src, tgt = str(r.get("source") or ""), str(r.get("target") or "")
                other = tgt if src == tid else (src if tgt == tid else "")
                if not other or other not in terms or other == tid:
                    continue
                otype = str(terms[other].get("type") or "")
                if otype in ("metric", "relation") or other.startswith(("metric:", "rel:")):
                    continue
                ocat = self._resolve_category(other, terms, cons_by_target, cons_by_scope, memo)
                votes[ocat] = votes.get(ocat, 0) + 1
            if votes:
                # 票数优先，同票按 CATEGORIES 顺序（确定性 tie-break）
                best = max(votes.items(), key=lambda kv: (kv[1], -CATEGORIES.index(kv[0])
                                                          if kv[0] in CATEGORIES else -99))
                memo[tid] = best[0]
                return best[0]

        # ── 规则 5：id 前缀兜底 ──────────────────────────────────────
        for prefix, c in ID_PREFIX_TO_CATEGORY.items():
            if tid.startswith(prefix):
                memo[tid] = c
                return c

        # ── 规则 6：默认 ─────────────────────────────────────────────
        memo[tid] = "数据"
        return "数据"

    # ── 对外查询 ──────────────────────────────────────────────────────

    def keywords_for(self, term_id: str) -> Tuple[List[str], List[str]]:
        e = self.index.get(term_id)
        if not e:
            return [], []
        return e["primary"], e["secondary"]

    def category_for(self, term_id: str) -> str:
        e = self.index.get(term_id)
        return e["category"] if e else "数据"

    def role_for(self, term_id: str) -> str:
        """术语在 RCA 中的角色（root_cause / component / observation / rule）。"""
        e = self.index.get(term_id)
        return e.get("role") if e else derive_role(term_id)

    def label_for(self, term_id: str) -> str:
        """术语的可读名称（拿不到就退回 id，绝不返回空串）。"""
        e = self.index.get(term_id)
        return (e or {}).get("name") or term_id

    def attributed_to(self, term_id: str) -> List[str]:
        """`term_id --attributedTo--> X` 的 X 列表（根因落在哪个组件上）。"""
        return list(self._attributed.get(term_id) or [])

    def root_causes_evidenced_by(self, metric_id: str) -> List[str]:
        """
        反向查询：**哪条指标是哪些根因的观测佐证**（`rc:X --evidencedBy--> metric:M`）。

        为什么要它：告警文本里提到的是**指标**（例如"健康探针失败"→ 副本健康数），
        而根因术语本身的关键词可能没被命中，于是根因连候选都进不去。
        有了这条反向索引，就能"从证据反推根因"，而且依据完全来自本体关系。
        """
        return list(self._by_evidence.get(metric_id) or [])

    def explain(self, term_id: str, limit: int = 4) -> Dict[str, Any]:
        """
        把一个本体术语展开成**能自解释的根因说明**。

        为什么需要它：结论原先只给 `类别 + rc:xxx`，用户看不出"是什么 / 为什么是它 / 影响到哪 / 凭什么判的"。
        这些信息本体里**本来就有**，只是没被取出来用：

          · `definition` / `scoring_rationale` → 机制与判别理由
          · 关系里带 `attributedTo:` 前缀的 → **归因对象**（根因落在哪个实例上）
          · 关系里带 `evidencedBy:` 前缀的 → **观测佐证**（哪条指标在印证）
          · 以本术语为 target 的 constraint → **判据**（阈值/容量口径、严重度、作用域）

        全部来自本体（版本化、可评审），不在代码里另写一份知识。
        """
        e = self.index.get(term_id)
        if not e:
            return {}
        affected: List[Dict[str, Any]] = []
        evidenced: List[Dict[str, Any]] = []
        related: List[Dict[str, Any]] = []
        for r in self.relations:
            if str(r.get("source") or "") != term_id:
                continue
            cond = str(r.get("connection_condition") or "")
            tgt = str(r.get("target") or "")
            item = {"id": tgt, "label": self.label_for(tgt),
                    "condition": cond, "description": r.get("description", "")}
            low = cond.lower()
            if low.startswith("attributedto"):
                affected.append(item)
            elif low.startswith("evidencedby"):
                evidenced.append(item)
            else:
                related.append(item)

        cons: List[Dict[str, Any]] = []
        for c in self.constraints:
            if str(c.get("target") or "") != term_id:
                continue
            cons.append({
                "id": c.get("id", ""),
                "description": c.get("description", ""),
                "severity": c.get("severity", ""),
                "scope": c.get("scope", ""),
                "constraint_type": c.get("constraint_type", ""),
                "trigger_keywords": list(c.get("trigger_keywords") or [])[:8],
            })

        return {
            "id": term_id,
            "name": e.get("name", ""),
            "category": e.get("category", ""),
            "role": e.get("role", ""),
            "scope": e.get("scope", ""),
            "type": e.get("type", ""),
            "lifecycle": e.get("lifecycle", ""),
            "definition": e.get("definition", ""),
            "rationale": e.get("rationale", ""),
            "keywords": list(e.get("primary") or [])[:12],
            "negative_keywords": list(e.get("negative") or [])[:8],
            "affected": affected[:limit],
            "evidenced_by": evidenced[:limit],
            "related": related[:limit],
            "constraints": cons[:limit],
            "evidence_refs": list(e.get("evidence_refs") or []),
            # 处置动作（ontology_v4）：让结论的"怎么办"也是本体知识
            "remediation": list(e.get("remediation") or []),
        }

    def match_text(self, term_id: str, text_lower: str) -> Tuple[int, float, List[str]]:
        """
        计算某术语与文本的匹配：返回 (primary_hits, weighted_hits, matched_keywords)。

        weighted = primary_hits + 0.5 × min(最长命中 n-gram 长度, 4) − |negative_hits|

        两个关键设计（都是被实测逼出来的）：

        · **中文 n-gram 按"最长命中"计权，不按命中个数计**。
          否则"应用副本"会同时命中 2/3/4-gram（应用、用副、副本、应用副、用副本、应用副本…），
          被算成 6 次命中 —— 同一段文字重复计分，把不相关的根因顶到第一。
          改成"最长命中 n-gram 长度 × 0.5"后，4 字连续命中 = 2.0 分，2 字 = 1.0 分，
          既保留"越长越可信"，又不会被切窗数量放大。

        · **primary 全权、secondary 半权且封顶**。
          primary 来自 Constraint.trigger_keywords / Term.aliases /
          Term.scoring_keywords（人工精修、单条目），是最可信的信号；
          secondary 只是名称切窗，只能当弱证据。

        · `negative_keywords` 是本体对"什么情况下**不是**我"的显式声明（ontology_v2 引入），
          命中即扣分 —— 这样"副本丢失 vs 副本假死 vs 副本 OOM"这种同实体多故障模式
          才能被关键词打分区分开，而不需要在代码里写特例。
        """
        e = self.index.get(term_id)
        if not e:
            return 0, 0.0, []
        hits = [k for k in e["primary"] if k.lower() in text_lower]
        sec_hits = [k for k in e["secondary"] if k in text_lower]
        neg_hits = [k for k in e.get("negative", ()) if k.lower() in text_lower]
        longest = max((len(g) for g in sec_hits), default=0)
        weighted = len(hits) + 0.5 * min(longest, 4) - len(neg_hits)
        if weighted < 0:
            weighted = 0.0
        shown = hits[:4] + sorted(sec_hits, key=len, reverse=True)[:2] \
            + ["!%s" % n for n in neg_hits[:2]]
        return len(hits), weighted, shown

    def match_constraints(self, text: str) -> List[Dict[str, Any]]:
        """
        用本体约束的 trigger_keywords 匹配告警文本 ——
        这就是"告警文本 → 本体约束 → 根因类别"的语义接线。
        """
        low = (text or "").lower()
        out: List[Dict[str, Any]] = []
        for c in self.constraints:
            kws = [str(k) for k in (c.get("trigger_keywords") or [])]
            hit = [k for k in kws if k.lower() in low]
            if not hit:
                continue
            tgt = str(c.get("target") or "")
            out.append({
                "constraint_id": str(c.get("id") or ""),
                "target": tgt,
                "target_category": self.category_for(tgt),
                "constraint_type": str(c.get("constraint_type") or ""),
                "severity": str(c.get("severity") or ""),
                "matched_keywords": hit,
                "hit_count": len(hit),
            })
        out.sort(key=lambda x: (-x["hit_count"],
                                -SEVERITY_RANK.get(x["severity"].lower(), 0),
                                x["constraint_id"]))
        return out

    def favoured_categories(self, text: str) -> Set[str]:
        """
        由匹配到的本体约束给出"更可能"的根因类别集合。

        两类来源并集（都来自本体，不依赖脆弱的派生节点类别）：
          · 约束自身的 `constraint_type` → 类别（capacity→资源、threshold→数据、
            business_rule→依赖 …）—— 这是人工写约束时就确定下来的语义
          · 约束 `target` 的类别（若可解析）
        """
        fav: Set[str] = set()
        for m in self.match_constraints(text):
            c = CONSTRAINT_TYPE_TO_CATEGORY.get(str(m.get("constraint_type") or ""))
            if c in CATEGORIES:
                fav.add(c)
            tc = m.get("target_category")
            if tc in CATEGORIES:
                fav.add(tc)
        return fav

    def supported_by_constraint(self, text: str, entity_id: str,
                                max_hops: int = 2) -> bool:
        """
        判断规则引擎给出的根因实体是否被"本体约束命中"支撑。

        判定：命中的约束，其 target 与本实体在关系图中距离 ≤ max_hops，
        或二者类别一致且实体就是该约束 target。
        这是 router 用来识别"快路径过度自信"的关键信号。
        """
        hits = self.match_constraints(text)
        if not hits or not entity_id:
            return False
        targets = {h["target"] for h in hits if h["target"]}
        if entity_id in targets:
            return True
        # 图距离
        adj: Dict[str, Set[str]] = {}
        for r in self.relations:
            s, t = str(r.get("source") or ""), str(r.get("target") or "")
            if not s or not t:
                continue
            adj.setdefault(s, set()).add(t)
            adj.setdefault(t, set()).add(s)
        frontier = {entity_id}
        seen = {entity_id}
        for _ in range(max_hops):
            nxt: Set[str] = set()
            for n in frontier:
                nxt |= adj.get(n, set())
            if nxt & targets:
                return True
            nxt -= seen
            seen |= nxt
            frontier = nxt
            if not frontier:
                break
        return False

    def summary(self) -> Dict[str, Any]:
        by_cat: Dict[str, int] = {}
        for e in self.index.values():
            by_cat[e["category"]] = by_cat.get(e["category"], 0) + 1
        return {
            "available": self.available,
            "workspace": self.workspace,
            "version": self.version,
            "terms_indexed": len(self.index),
            "constraints": len(self.constraints),
            "relations": len(self.relations),
            "known_root_causes": sorted(self.known_root_causes),
            "category_distribution": by_cat,
            "error": self.error or None,
        }
