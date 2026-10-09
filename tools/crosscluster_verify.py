# -*- coding: utf-8 -*-
"""
跨集群 / 跨命名空间**归属**验证：引擎能不能把根因落到正确的集群上。

## 为什么把它当作"多集群"的最小可行验证

真正的多集群（两套 compose / 两个命名空间）需要第二套环境，成本与风险都大；
而在**当前这套栈里其实已经有两个集群 + 一个运行环境**：

  · `app:payment-app-cluster`（3 副本应用集群，经网关负载均衡）
  · `db:mysql-cluster`（1 主 2 从数据库集群）
  · `env:container` / `env:cluster`（承载它们的运行环境）

"跨集群定位"要回答的就是一个**比 top1 更粗、但更可操作**的问题：
*根因到底落在哪个集群上？* 生产里这个信息决定"该找谁"（应用值班 / DBA / 平台）。
术语精确到 `rc:row-lock` 很好，但如果它把数据库问题说成应用问题，那比不精确更糟。

因此本工具用**真实告警文本**（端到端产物里的 `alert_text`）跑一遍，检查：
`root_cause` 在本体里 `attributedTo` 到哪个集群，是否与场景所在的层一致。

## 判据（全部来自本体，不另编映射）

  · 根因术语 → `attributedTo` 关系的 target（如 `rc:row-lock → db:primary`，
    再经 `memberOf` 到 `db:mysql-cluster`）；
  · 场景的期望集群 → 由期望术语的前缀（`db:` / `app:` / `env:`）推导。

真实多集群（第二套环境）仍未做 —— 这一条只验证**推理层的跨集群归属**，
不验证跨环境的操作面（见输出里的 boundary 字段）。

用法：
  python tools/crosscluster_verify.py                                  # 用最新端到端报告
  python tools/crosscluster_verify.py --report .chaos/rca_diagnosis_report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rca-agent"))
sys.path.insert(0, str(ROOT / "rca-agent" / "backend"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

#: 集群术语（本体里真实存在的三个"集群/环境"锚点）及其归属命名空间
CLUSTERS = {
    "app:payment-app-cluster": "应用集群",
    "db:mysql-cluster": "数据库集群",
    "env:container": "运行环境",
    "env:cluster": "运行环境",
    "env:gateway": "应用集群（入口）",
    "db:mysql-core": "数据库集群",
    "db:mysql-replica": "数据库集群",
    "app:payment-app": "应用集群",
}


def _neighbors(sc) -> Dict[str, List[str]]:
    """无向关系邻接表（任意 relation_type）—— 归属要能沿链条传递，而不只看一条边。"""
    adj: Dict[str, List[str]] = {}
    # 注意：关系在 `sc.relations`（list of dict）上，不是 `sc.records` ——
    # 第一版写成 `sc.records` 取到空表，于是所有归属都是 "-"，
    # 把 21 个场景全判成 MISS。取属性前先确认它到底是什么。
    rels = getattr(sc, "relations", None) or []
    for r in rels:
        if not isinstance(r, dict):
            continue
        s, t = str(r.get("source") or ""), str(r.get("target") or "")
        if not s or not t:
            continue
        adj.setdefault(s, []).append(t)
        adj.setdefault(t, []).append(s)
    return adj


def _cluster_of(term_id: str, sc, adj: Optional[Dict[str, List[str]]] = None) -> Optional[str]:
    """
    把一个术语归属到集群命名空间。

    做法是**在关系图上 BFS 到最近的集群锚点**，而不是只看 `attributedTo` 一跳：
    `rc:row-lock` 的归属边指向 `table:t_txn`（表级："是什么"），
    集群级（"该找谁"）要再沿 `managedBy`/`memberOf` 之类的边往上走才能到 `db:mysql-cluster`。
    只做一跳就会把 3 个数据库根因误判成"无法归属"。
    """
    if not term_id:
        return None
    if term_id in CLUSTERS:
        return CLUSTERS[term_id]
    for pre, name in (("app:", "应用集群"), ("db:", "数据库集群"), ("env:", "运行环境")):
        if str(term_id).startswith(pre):
            return name
    adj = adj if adj is not None else _neighbors(sc)
    seen = {term_id}
    frontier = [term_id]
    for _ in range(3):                      # 最多 3 跳：够到集群锚点即可，避免扯太远
        nxt: List[str] = []
        for node in frontier:
            for nb in adj.get(node, []):
                if nb in seen:
                    continue
                seen.add(nb)
                if nb in CLUSTERS:
                    return CLUSTERS[nb]
                if str(nb).startswith("app:"):
                    return "应用集群"
                if str(nb).startswith("db:"):
                    return "数据库集群"
                if str(nb).startswith("env:"):
                    return "运行环境"
                nxt.append(nb)
        frontier = nxt
        if not frontier:
            break
    # 本体里推不出来 → 按 id 前缀兜底（并会被 attribution 统计计为 MISS）
    for pre, name in (("table:", None), ("idx:", None)):
        if str(term_id).startswith(pre):
            return None
    return None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="跨集群归属验证（推理层）")
    ap.add_argument("--report", default=str(ROOT / ".chaos" / "rca_diagnosis_report.json"))
    ap.add_argument("--json-out", default=str(ROOT / ".chaos" / "crosscluster_report.json"))
    args = ap.parse_args(argv)

    from backend.services.ontology_scoring import OntologyScoring
    sc = OntologyScoring.get(None)
    adj = _neighbors(sc)

    rep = json.loads(Path(args.report).read_text(encoding="utf-8"))
    rows: List[Dict[str, Any]] = []
    for r in rep.get("results") or []:
        runs = r.get("diagnosis_runs") or []
        got = str((runs[0].get("root_cause_id") if runs else "") or "")
        exp_terms = r.get("expected_terms") or []
        # 期望集群：由期望术语（含 rc:/metric: 前缀）里的 app:/db:/env: 归属推导
        exp_cluster = None
        for t in exp_terms:
            c = _cluster_of(t, sc, adj)
            if c:
                exp_cluster = c
                break
        if not exp_cluster:
            exp_cluster = {"application": "应用集群", "database": "数据库集群",
                           "resource": "运行环境"}.get(r.get("layer"), None)
        got_cluster = _cluster_of(got, sc, adj)
        rows.append({
            "scenario": r.get("scenario"), "layer": r.get("layer"),
            "root_cause": got, "got_cluster": got_cluster,
            "expected_cluster": exp_cluster,
            "attribution_ok": bool(exp_cluster and got_cluster == exp_cluster),
            "strict_hit": got in exp_terms,
        })

    n = len(rows)
    attributed = sum(1 for r in rows if r["attribution_ok"])
    strict = sum(1 for r in rows if r["strict_hit"])
    # 反向：术语不精确、但集群归属正确的比例（说明"至少找对了人"）
    loose_right = sum(1 for r in rows if r["attribution_ok"] and not r["strict_hit"])

    print("=" * 96)
    print("跨集群归属验证（推理层）：根因是否落在正确的集群上")
    print("=" * 96)
    print("  %-24s %-14s %-26s %-12s %-12s %s"
          % ("场景", "层", "诊断根因", "归属集群", "期望集群", "结果"))
    for r in rows:
        print("  %-24s %-14s %-26s %-12s %-12s %s"
              % (r["scenario"], r["layer"], r["root_cause"][:26],
                 r["got_cluster"] or "-", r["expected_cluster"] or "-",
                 "OK" if r["attribution_ok"] else "MISS"))
    print()
    print("  **跨集群归属正确：%d/%d = %.0f%%**" % (attributed, n, 100.0 * attributed / n))
    print("  （对照：严格 top1 %d/%d = %.0f%%；其中 %d 个场景「术语不精确但集群对」）"
          % (strict, n, 100.0 * strict / n, loose_right))

    out = {
        "n": n, "attribution_ok": attributed, "strict_top1": strict,
        "loose_but_right_cluster": loose_right,
        "rows": rows,
        "boundary": ("本验证覆盖**推理层的跨集群归属**（本体能否表达、引擎能否落到正确的集群）；"
                     "**未覆盖**跨环境的操作面（第二套 compose / 另一命名空间的实际注入与隔离），"
                     "那需要真实的第二套环境。"),
    }
    Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    print("\n报告: %s" % args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
